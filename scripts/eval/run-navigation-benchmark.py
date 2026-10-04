#!/usr/bin/env python3
"""Materialize and run the finalpool navigation benchmark."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_ID = "google/gemini-3-flash-preview"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.runner import materialize_task  # noqa: E402
import eval.navigation.runner as navigation_runner  # noqa: E402
from eval.navigation.schema import (  # noqa: E402
    atomic_write_json,
    find_task,
    load_benchmark,
    load_map,
    load_tasks,
)
from eval.navigation.snapshots import verify_snapshot  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("review", "pilot", "formal"), required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--task", dest="task_ids", action="append")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--vnc", action="store_true")
    parser.add_argument(
        "--record-video",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--results-root", type=Path)
    parser.add_argument("--allocated-cpus", type=float)
    parser.add_argument("--allocated-memory", default="")
    parser.add_argument("--allocated-gpu", default="")
    parser.add_argument("--eval-setting-overrides-json", default="{}")
    parser.add_argument("--materialize-only", action="store_true")
    parser.add_argument(
        "--allow-unverified-runtime",
        action="store_true",
        help="Review/pilot only: accept a prepared receipt without live smoke.",
    )
    parser.add_argument("--model-id", default="")
    parser.add_argument(
        "--api-protocol",
        choices=("chat_completions", "responses"),
        default="",
    )
    parser.add_argument("--model-parameters-json", default="{}")
    return parser.parse_args()


def _record_post_run_snapshot_check(run: dict[str, Any]) -> dict[str, Any]:
    checked_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "navigation-post-run-snapshot-check",
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "map_id": run["map_id"],
        "expected_source_fingerprint": run["source_map_fingerprint"],
        "expected_prepared_fingerprint": run["map_fingerprint"],
        "expected_preparation_digest": run["snapshot_preparation_digest"],
        "verified": False,
        "checked_at_utc": checked_at,
    }
    try:
        snapshot = verify_snapshot(load_map(str(run["map_id"])))
        actual_source = snapshot["source"]["fingerprint"]["value"]
        actual_prepared = snapshot["fingerprint"]["value"]
        actual_preparation_digest = snapshot["receipt"][
            "preparation_config_digest"
        ]
        payload["actual_source_fingerprint"] = actual_source
        payload["actual_prepared_fingerprint"] = actual_prepared
        payload["actual_preparation_digest"] = actual_preparation_digest
        payload["level"] = snapshot["level"]
        if actual_source != run["source_map_fingerprint"]:
            raise RuntimeError(
                f"source snapshot fingerprint changed: expected "
                f"{run['source_map_fingerprint']}, got {actual_source}"
            )
        if actual_prepared != run["map_fingerprint"]:
            raise RuntimeError(
                f"prepared snapshot fingerprint changed: expected "
                f"{run['map_fingerprint']}, got {actual_prepared}"
            )
        if actual_preparation_digest != run["snapshot_preparation_digest"]:
            raise RuntimeError(
                "prepared snapshot transform changed after materialization"
            )
        payload["verified"] = True
    except Exception as error:
        payload["error"] = f"{type(error).__name__}: {error}"
    atomic_write_json(
        Path(run["results_dir"]) / "snapshot-after-run.json",
        payload,
    )
    return payload


def main() -> int:
    args = parse_args()
    if args.runtime_root is not None:
        navigation_runner.RUNTIME_ROOT = args.runtime_root.expanduser().resolve()
    if args.results_root is not None:
        navigation_runner.RESULTS_ROOT = args.results_root.expanduser().resolve()
    benchmark = load_benchmark("finalpool-navigation-v1")
    if args.all:
        if args.mode == "review":
            raise SystemExit("review mode requires one or more explicit --task selections")
        selected = [
            (map_id, task)
            for map_id in benchmark["maps"]
            for task in load_tasks(map_id)
        ]
    else:
        selected = [
            find_task(task_id, benchmark["maps"])
            for task_id in (args.task_ids or [])
        ]
    selected_ids = [str(task["id"]) for _map_id, task in selected]
    if len(selected_ids) != len(set(selected_ids)):
        raise SystemExit("duplicate --task selection is not allowed")
    if args.mode == "formal" and args.allow_unverified_runtime:
        raise SystemExit("--allow-unverified-runtime is forbidden in formal mode")
    try:
        model_parameters = json.loads(args.model_parameters_json)
    except json.JSONDecodeError as error:
        raise SystemExit(f"invalid --model-parameters-json: {error}") from error
    if not isinstance(model_parameters, dict):
        raise SystemExit("--model-parameters-json must decode to an object")
    try:
        eval_setting_overrides = json.loads(args.eval_setting_overrides_json)
    except json.JSONDecodeError as error:
        raise SystemExit(f"invalid --eval-setting-overrides-json: {error}") from error
    if not isinstance(eval_setting_overrides, dict):
        raise SystemExit("--eval-setting-overrides-json must decode to an object")
    model_id = (
        args.model_id.strip()
        or (
            str(model_parameters.get("model_id", "")).strip()
            if model_parameters.get("model_id") is not None
            else ""
        )
        or os.getenv("MCBOTS_MODEL", "").strip()
        or DEFAULT_MODEL_ID
    )
    model_parameters["model_id"] = model_id
    api_protocol = (
        args.api_protocol
        or str(model_parameters.get("api_protocol", "")).strip()
        or "chat_completions"
    )
    if api_protocol not in {"chat_completions", "responses"}:
        raise SystemExit(
            "api_protocol must be chat_completions or responses"
        )
    model_parameters["api_protocol"] = api_protocol
    model_parameters.setdefault("action_protocol", "tool_calls")
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outcomes: list[dict[str, Any]] = []
    launcher = REPO_ROOT / "scripts" / "eval" / "run-navigation-task-inside.sh"
    for map_id, task in selected:
        worker_resources = (
            {
                "cpus": args.allocated_cpus,
                "memory": args.allocated_memory,
                **({"render_mode": "gpu"} if args.allocated_gpu else {}),
            }
            if args.allocated_cpus is not None
            or args.allocated_memory
            or args.allocated_gpu
            else None
        )
        run = materialize_task(
            run_id=run_id,
            map_id=map_id,
            task=task,
            mode=args.mode,
            benchmark=benchmark,
            model_parameters=model_parameters,
            require_smoke=not args.allow_unverified_runtime,
            vnc=args.vnc,
            record_video=args.record_video,
            worker_resources=worker_resources,
            runtime_allocation=(
                {"gpu_device": args.allocated_gpu} if args.allocated_gpu else None
            ),
            eval_setting_overrides=eval_setting_overrides,
        )
        outcome = {
            "task_id": task["id"],
            "runtime_dir": run["runtime_dir"],
            "results_dir": run["results_dir"],
            "returncode": None,
        }
        print(json.dumps(outcome, ensure_ascii=False), flush=True)
        if not args.materialize_only:
            try:
                completed = subprocess.run(
                    [str(launcher), "--run-dir", run["runtime_dir"]],
                    cwd=REPO_ROOT,
                )
                outcome["returncode"] = completed.returncode
            finally:
                snapshot_check = _record_post_run_snapshot_check(run)
                outcome["snapshots_verified_after_run"] = snapshot_check["verified"]
                if snapshot_check["verified"] is not True:
                    outcome["returncode"] = 3
        outcomes.append(outcome)
    print(
        json.dumps(
            {"run_id": run_id, "mode": args.mode, "tasks": outcomes},
            ensure_ascii=False,
            indent=2,
        )
    )
    return_codes = [
        int(outcome["returncode"])
        for outcome in outcomes
        if outcome["returncode"] is not None
    ]
    if any(code >= 3 or code < 0 for code in return_codes):
        return 3
    if any(code != 0 for code in return_codes):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
