#!/usr/bin/env python3
"""Validate and aggregate one formal navigation run."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.runner import RESULTS_ROOT  # noqa: E402
from eval.navigation.schema import (  # noqa: E402
    SchemaError,
    atomic_write_json,
    canonical_json,
    digest_json,
    load_benchmark,
    load_map,
    load_profile,
    load_setting,
    load_tasks,
    map_preparation_digest,
    task_digest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--results-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--require-all",
        action="store_true",
        help="Require exactly the benchmark's complete formal task set.",
    )
    return parser.parse_args()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SchemaError(f"missing result artifact: {path}") from error
    except json.JSONDecodeError as error:
        raise SchemaError(f"invalid result JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise SchemaError(f"result artifact must be an object: {path}")
    return payload


def _number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{field} must be numeric or null")
    result = float(value)
    if not math.isfinite(result):
        raise SchemaError(f"{field} must be finite")
    return result


def _mean(values: Iterable[float | int | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return None if not present else round(sum(present) / len(present), 6)


def _sum(values: Iterable[float | int | None]) -> float:
    return round(sum(float(value) for value in values if value is not None), 6)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if not row["infrastructure_error"]]
    successes = [row for row in scored if row["success"]]
    total = len(rows)
    scored_count = len(scored)
    checkpoint_total = sum(int(row["checkpoint_total"]) for row in scored)
    checkpoint_reached = sum(int(row["checkpoint_reached"]) for row in scored)
    return {
        "task_runs": total,
        "infrastructure_errors": total - scored_count,
        "scored_task_runs": scored_count,
        "successes": len(successes),
        "success_rate": (
            None if scored_count == 0 else round(len(successes) / scored_count, 6)
        ),
        "attempt_success_rate_including_infrastructure": (
            None if total == 0 else round(len(successes) / total, 6)
        ),
        "oracle_arrivals": sum(bool(row["oracle_arrived"]) for row in scored),
        "duration_sec_total": _sum(row["duration_sec"] for row in scored),
        "duration_sec_mean": _mean(row["duration_sec"] for row in scored),
        "decision_count_total": int(
            sum(int(row["decision_count"]) for row in scored)
        ),
        "decision_count_mean": _mean(row["decision_count"] for row in scored),
        "path_length_xz_total": _sum(row["path_length_xz"] for row in scored),
        "path_length_xz_mean": _mean(row["path_length_xz"] for row in scored),
        "path_length_3d_total": _sum(row["path_length_3d"] for row in scored),
        "path_length_3d_mean": _mean(row["path_length_3d"] for row in scored),
        "progress_ratio_xz_mean": _mean(
            row["progress_ratio_xz"] for row in scored
        ),
        "best_progress_ratio_xz_mean": _mean(
            row["best_progress_ratio_xz"] for row in scored
        ),
        "checkpoint_reached": checkpoint_reached,
        "checkpoint_total": checkpoint_total,
        "checkpoint_coverage": (
            1.0
            if checkpoint_total == 0
            else round(checkpoint_reached / checkpoint_total, 6)
        ),
        "claim_count_total": int(sum(int(row["claim_count"]) for row in scored)),
        "claim_count_mean": _mean(row["claim_count"] for row in scored),
        "static_spl_mean": _mean(row["static_spl"] for row in scored),
    }


def _load_formal_row(task_dir: Path) -> dict[str, Any] | None:
    run = _load_object(task_dir / "run.json")
    if run.get("mode") != "formal":
        return None
    if run.get("formal_eligible") is not True:
        raise SchemaError(f"{task_dir.name}: formal run is not marked formal_eligible")
    task_id = str(run.get("task_id", ""))
    if not task_id or task_id != task_dir.name:
        raise SchemaError(f"{task_dir}: task ID does not match its directory")

    completion = _load_object(task_dir / "completion.json")
    metrics = _load_object(task_dir / "metrics.json")
    supervisor = _load_object(task_dir / "supervisor.json")
    agent_result = _load_object(task_dir / "agent-result.json")
    snapshot_check = _load_object(task_dir / "snapshot-after-run.json")
    success = completion.get("success")
    infrastructure_error = completion.get("infrastructure_error")
    if not isinstance(success, bool) or not isinstance(infrastructure_error, bool):
        raise SchemaError(f"{task_id}: invalid completion outcome")
    if supervisor.get("success") != success:
        raise SchemaError(f"{task_id}: supervisor/completion success mismatch")
    if supervisor.get("infrastructure_error") != infrastructure_error:
        raise SchemaError(f"{task_id}: supervisor/completion infrastructure mismatch")
    if metrics.get("success") != success:
        raise SchemaError(f"{task_id}: metrics/completion success mismatch")
    if completion.get("mode") != "formal":
        raise SchemaError(f"{task_id}: completion mode is not formal")
    if snapshot_check.get("artifact_kind") != "navigation-post-run-snapshot-check":
        raise SchemaError(f"{task_id}: invalid post-run snapshot check")
    if snapshot_check.get("verified") is not True:
        raise SchemaError(f"{task_id}: snapshots were not verified after the run")
    if (
        snapshot_check.get("task_id") != task_id
        or snapshot_check.get("run_id") != run.get("run_id")
        or snapshot_check.get("map_id") != run.get("map_id")
    ):
        raise SchemaError(f"{task_id}: post-run snapshot check identity mismatch")
    if (
        snapshot_check.get("expected_source_fingerprint")
        != run.get("source_map_fingerprint")
        or snapshot_check.get("actual_source_fingerprint")
        != run.get("source_map_fingerprint")
        or snapshot_check.get("expected_prepared_fingerprint")
        != run.get("map_fingerprint")
        or snapshot_check.get("actual_prepared_fingerprint")
        != run.get("map_fingerprint")
        or snapshot_check.get("expected_preparation_digest")
        != run.get("snapshot_preparation_digest")
        or snapshot_check.get("actual_preparation_digest")
        != run.get("snapshot_preparation_digest")
    ):
        raise SchemaError(f"{task_id}: post-run snapshot binding mismatch")

    runtime_readback_path = task_dir / "runtime-readback.json"
    runtime_readback = (
        _load_object(runtime_readback_path)
        if runtime_readback_path.is_file()
        else None
    )
    if runtime_readback is None and not infrastructure_error:
        raise SchemaError(f"{task_id}: scored result lacks runtime-readback.json")
    if runtime_readback is not None and runtime_readback.get("success") is not True:
        raise SchemaError(f"{task_id}: runtime readback is not healthy")

    checkpoints = metrics.get("checkpoints")
    if not isinstance(checkpoints, dict):
        raise SchemaError(f"{task_id}: metrics lacks checkpoint summary")
    decision_count = agent_result.get("decision_count")
    if (
        isinstance(decision_count, bool)
        or not isinstance(decision_count, int)
        or decision_count < 0
    ):
        raise SchemaError(f"{task_id}: invalid decision_count")
    claim_count = completion.get("claim_count")
    if isinstance(claim_count, bool) or not isinstance(claim_count, int) or claim_count < 0:
        raise SchemaError(f"{task_id}: invalid claim_count")

    return {
        "task_id": task_id,
        "map_id": str(run["map_id"]),
        "success": success,
        "infrastructure_error": infrastructure_error,
        "terminal_reason": completion.get("terminal_reason"),
        "oracle_arrived": bool(completion.get("oracle_arrived")),
        "claim_count": claim_count,
        "decision_count": decision_count,
        "duration_sec": _number(metrics.get("duration_sec"), f"{task_id}.duration_sec"),
        "path_length_xz": _number(
            metrics.get("path_length_xz"), f"{task_id}.path_length_xz"
        ),
        "path_length_3d": _number(
            metrics.get("path_length_3d"), f"{task_id}.path_length_3d"
        ),
        "progress_ratio_xz": _number(
            metrics.get("progress_ratio_xz"), f"{task_id}.progress_ratio_xz"
        ),
        "best_progress_ratio_xz": _number(
            metrics.get("best_progress_ratio_xz"),
            f"{task_id}.best_progress_ratio_xz",
        ),
        "checkpoint_reached": int(checkpoints.get("ordered_reached", 0)),
        "checkpoint_total": int(checkpoints.get("ordered_total", 0)),
        "static_spl": _number(metrics.get("static_spl"), f"{task_id}.static_spl"),
        "_run": run,
        "_runtime_readback": runtime_readback,
    }


def aggregate(
    run_id: str,
    *,
    require_all: bool,
    results_root: Path | None = None,
) -> dict[str, Any]:
    if not run_id or run_id != Path(run_id).name or run_id in {".", ".."}:
        raise SchemaError(f"unsafe run ID: {run_id!r}")
    effective_results_root = (
        results_root.expanduser().resolve()
        if results_root is not None
        else RESULTS_ROOT
    )
    run_root = effective_results_root / run_id
    if not run_root.is_dir():
        raise SchemaError(f"navigation result run does not exist: {run_root}")

    rows: list[dict[str, Any]] = []
    ignored_nonformal: list[str] = []
    for task_dir in sorted(path for path in run_root.iterdir() if path.is_dir()):
        row = _load_formal_row(task_dir)
        if row is None:
            ignored_nonformal.append(task_dir.name)
        else:
            rows.append(row)
    if not rows:
        raise SchemaError(f"{run_id}: no complete formal task results")

    task_ids = [row["task_id"] for row in rows]
    if len(task_ids) != len(set(task_ids)):
        raise SchemaError(f"{run_id}: duplicate formal task IDs")
    runs = [row["_run"] for row in rows]
    if any(run.get("run_id") != run_id for run in runs):
        raise SchemaError(f"{run_id}: embedded run ID mismatch")

    identity_fields = (
        "benchmark_id",
        "profile_id",
        "profile_digest",
        "runtime_receipt_digest",
        "setting_id",
        "setting_digest",
    )
    identity: dict[str, Any] = {}
    for field in identity_fields:
        values = {canonical_json(run.get(field)) for run in runs}
        if len(values) != 1:
            raise SchemaError(f"{run_id}: mixed {field} values")
        identity[field] = runs[0].get(field)
    runtime_receipt_digest = identity["runtime_receipt_digest"]
    if (
        not isinstance(runtime_receipt_digest, str)
        or len(runtime_receipt_digest) != 64
        or any(char not in "0123456789abcdef" for char in runtime_receipt_digest)
    ):
        raise SchemaError(f"{run_id}: invalid runtime receipt digest")
    parameter_values = {canonical_json(run.get("model_parameters")) for run in runs}
    if len(parameter_values) != 1:
        raise SchemaError(f"{run_id}: mixed model parameters")
    model_parameters = runs[0].get("model_parameters")
    if not isinstance(model_parameters, dict):
        raise SchemaError(f"{run_id}: model_parameters must be an object")
    resource_values = {
        canonical_json(run.get("worker_resources", {})) for run in runs
    }
    if len(resource_values) != 1:
        raise SchemaError(f"{run_id}: mixed worker resources")
    worker_resources = runs[0].get("worker_resources", {})
    if not isinstance(worker_resources, dict):
        raise SchemaError(f"{run_id}: worker_resources must be an object")
    video_values = {bool(run.get("record_video", False)) for run in runs}
    if len(video_values) != 1:
        raise SchemaError(f"{run_id}: mixed video settings")
    override_values = {
        canonical_json(run.get("eval_setting_overrides", {})) for run in runs
    }
    if len(override_values) != 1:
        raise SchemaError(f"{run_id}: mixed eval setting overrides")
    eval_setting_overrides = runs[0].get("eval_setting_overrides", {})
    if not isinstance(eval_setting_overrides, dict):
        raise SchemaError(f"{run_id}: eval_setting_overrides must be an object")

    benchmark = load_benchmark(str(identity["benchmark_id"]))
    profile = load_profile(str(identity["profile_id"]))
    setting = load_setting(str(identity["setting_id"]))
    if identity["profile_digest"] != digest_json(profile):
        raise SchemaError(f"{run_id}: profile digest is stale")
    if identity["setting_digest"] != digest_json(setting):
        raise SchemaError(f"{run_id}: setting digest is stale")
    if benchmark["profile_id"] != identity["profile_id"]:
        raise SchemaError(f"{run_id}: benchmark/profile mismatch")
    if benchmark["setting_id"] != identity["setting_id"]:
        raise SchemaError(f"{run_id}: benchmark/setting mismatch")

    map_fingerprints: dict[str, str] = {}
    source_map_fingerprints: dict[str, str] = {}
    snapshot_preparation_digests: dict[str, str] = {}
    map_payloads = {
        map_id: load_map(map_id)
        for map_id in benchmark["maps"]
    }
    tasks_by_map = {
        map_id: {str(task["id"]): task for task in load_tasks(map_id)}
        for map_id in benchmark["maps"]
    }
    for row in rows:
        run = row["_run"]
        map_id = row["map_id"]
        task_id = row["task_id"]
        if map_id not in tasks_by_map or task_id not in tasks_by_map[map_id]:
            raise SchemaError(f"{task_id}: task is outside the tracked benchmark")
        task = tasks_by_map[map_id][task_id]
        if run.get("task_digest") != task_digest(map_id, task):
            raise SchemaError(f"{task_id}: run task digest is stale")
        reference_digest_value = run.get("reference_digest")
        reference_length = run.get("reference_length_blocks")
        if (reference_digest_value is None) != (reference_length is None):
            raise SchemaError(f"{task_id}: incomplete optional reference metadata")
        fingerprint = str(run.get("map_fingerprint", ""))
        previous = map_fingerprints.setdefault(map_id, fingerprint)
        if previous != fingerprint:
            raise SchemaError(f"{run_id}: mixed fingerprints for map {map_id}")
        map_payload = map_payloads[map_id]
        expected = map_payload["world"]["expected_prepared_fingerprint"]
        if fingerprint != expected:
            raise SchemaError(f"{run_id}: stale prepared fingerprint for map {map_id}")
        source_fingerprint = str(run.get("source_map_fingerprint", ""))
        previous_source = source_map_fingerprints.setdefault(
            map_id,
            source_fingerprint,
        )
        if previous_source != source_fingerprint:
            raise SchemaError(
                f"{run_id}: mixed source fingerprints for map {map_id}"
            )
        if source_fingerprint != map_payload["world"]["expected_source_fingerprint"]:
            raise SchemaError(f"{run_id}: stale source fingerprint for map {map_id}")
        preparation_digest = str(run.get("snapshot_preparation_digest", ""))
        previous_preparation = snapshot_preparation_digests.setdefault(
            map_id,
            preparation_digest,
        )
        if previous_preparation != preparation_digest:
            raise SchemaError(
                f"{run_id}: mixed snapshot preparation digests for map {map_id}"
            )
        if preparation_digest != map_preparation_digest(map_payload):
            raise SchemaError(
                f"{run_id}: stale snapshot preparation for map {map_id}"
            )
        if run.get("validation_receipt_digest") is not None:
            raise SchemaError(
                f"{task_id}: validation receipt metadata is not part of this benchmark"
            )
        readback = row["_runtime_readback"]
        if readback is not None:
            if readback.get("minecraft_version") != profile["minecraft"]["version"]:
                raise SchemaError(
                    f"{row['task_id']}: Minecraft runtime readback mismatch"
                )
            if readback.get("neoforge_version") != profile["neoforge"]["version"]:
                raise SchemaError(
                    f"{row['task_id']}: NeoForge runtime readback mismatch"
                )

    expected_task_ids = {
        str(task["id"])
        for map_id in benchmark["maps"]
        for task in load_tasks(map_id)
    }
    unexpected = set(task_ids) - expected_task_ids
    if unexpected:
        raise SchemaError(
            f"{run_id}: tasks outside benchmark: {', '.join(sorted(unexpected))}"
        )
    if require_all and set(task_ids) != expected_task_ids:
        missing = sorted(expected_task_ids - set(task_ids))
        raise SchemaError(
            f"{run_id}: --require-all is missing: {', '.join(missing)}"
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    public_rows: list[dict[str, Any]] = []
    for row in rows:
        grouped[row["map_id"]].append(row)
        public_rows.append({key: value for key, value in row.items() if not key.startswith("_")})
    return {
        "schema_version": 1,
        "artifact_kind": "navigation-formal-aggregate",
        "run_id": run_id,
        **identity,
        "model_parameters": model_parameters,
        "worker_resources": worker_resources,
        "record_video": video_values.pop(),
        "eval_setting_overrides": eval_setting_overrides,
        "map_fingerprints": dict(sorted(map_fingerprints.items())),
        "source_map_fingerprints": dict(sorted(source_map_fingerprints.items())),
        "snapshot_preparation_digests": dict(
            sorted(snapshot_preparation_digests.items())
        ),
        "formal_task_ids": sorted(task_ids),
        "ignored_nonformal_task_ids": ignored_nonformal,
        "complete_benchmark": set(task_ids) == expected_task_ids,
        "summary": _summary(public_rows),
        "by_map": {
            map_id: _summary(
                [
                    {key: value for key, value in row.items() if not key.startswith("_")}
                    for row in map_rows
                ]
            )
            for map_id, map_rows in sorted(grouped.items())
        },
        "tasks": sorted(public_rows, key=lambda row: row["task_id"]),
        "aggregated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def main() -> int:
    args = parse_args()
    results_root = (
        args.results_root.expanduser().resolve()
        if args.results_root is not None
        else RESULTS_ROOT
    )
    payload = aggregate(
        args.run_id,
        require_all=args.require_all,
        results_root=results_root,
    )
    destination = (
        args.output.resolve()
        if args.output
        else results_root / args.run_id / "aggregate.json"
    )
    atomic_write_json(destination, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"wrote {destination}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
