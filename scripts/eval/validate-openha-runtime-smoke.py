#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class RuntimeSmokeCase:
    name: str
    task_name: str
    extra_env: Dict[str, str] | None = None


@dataclass
class RuntimeSmokeResult:
    name: str
    task_name: str
    family: str
    ok: bool
    rc: int
    duration_ms: int
    timeout: bool
    result_path_container: Optional[str] = None
    result_path_host: Optional[str] = None
    result_reason: Optional[str] = None
    result_success: Optional[bool] = None
    result_delta_score: Optional[int] = None
    failure_kind: Optional[str] = None
    output_tail: Optional[str] = None


DEFAULT_CASES: List[RuntimeSmokeCase] = [
    RuntimeSmokeCase(name="kill_sheep", task_name="kill_entity:sheep"),
    RuntimeSmokeCase(name="mine_dirt", task_name="mine_block:dirt"),
    RuntimeSmokeCase(name="interact_anvil", task_name="custom:interact_with_anvil"),
    RuntimeSmokeCase(name="craft_stick", task_name="craft_item:stick"),
    RuntimeSmokeCase(name="smelt_baked_potato", task_name="smelt_item:baked_potato"),
]

SUPPORTED_FAMILIES = ("kill_entity", "mine_block", "interact_block", "craft_item", "smelt_item")


RESULT_PATH_RE = re.compile(r"^\[done\] result saved: (?P<path>.+)$", re.MULTILINE)


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def load_json_obj(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return data


def parse_kv_list(items: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE, got: {item}")
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"Invalid empty key in: {item}")
        out[k] = v
    return out


def canonical_task_slug(task_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_name.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "task"


def infer_family_from_task_name(task_name: str) -> str:
    if task_name.startswith("kill_entity:"):
        return "kill_entity"
    if task_name.startswith("mine_block:"):
        return "mine_block"
    if task_name.startswith("craft_item:"):
        return "craft_item"
    if task_name.startswith("smelt_item:"):
        return "smelt_item"
    if task_name.startswith("interact_block:") or task_name.startswith("custom:interact_with_"):
        return "interact_block"
    return "unknown"


def container_to_host_path(container_path: str, *, container_root: str, host_root: Path) -> Optional[Path]:
    p = PurePosixPath(container_path)
    croot = PurePosixPath(container_root)
    try:
        rel = p.relative_to(croot)
    except ValueError:
        return None
    return host_root / Path(*rel.parts)


def classify_failure(
    *,
    rc: int,
    timed_out: bool,
    output: str,
    result_path_host: Optional[Path],
    result_json: Optional[dict],
) -> str:
    lower = output.lower()
    if timed_out:
        return "process_timeout"
    if "rcon not ready within" in lower:
        if "operation not permitted" in lower:
            return "rcon_not_ready_op_not_permitted"
        return "rcon_not_ready"
    if "temporary failure in name resolution" in lower or "unable to resolve host address" in lower:
        return "server_bootstrap_dns_failure"
    if "server exited immediately" in output:
        return "server_exited_immediately"
    if "Traceback" in output:
        return "traceback"
    if rc != 0:
        return "nonzero_exit"
    if result_path_host is None:
        return "missing_result_path_line"
    if not result_path_host.exists():
        return "missing_result_file"
    if result_json is None:
        return "invalid_result_json"
    if result_json.get("dry_run") is True:
        return "unexpected_dry_run"
    return "unknown"


def resolve_result_host_path(
    *,
    result_path_str: str,
    execution_mode: str,
    host_project_root: Path,
    container_project_root: str,
) -> Optional[Path]:
    if execution_mode == "podman":
        return container_to_host_path(
            result_path_str,
            container_root=container_project_root,
            host_root=host_project_root,
        )
    p = Path(result_path_str)
    if p.is_absolute():
        return p
    return (host_project_root / p).resolve()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run container runtime smoke validation for OpenHA task wrappers (SKIP_AGENT=true)."
    )
    p.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--execution-mode", choices=["podman", "local"], default="podman")
    p.add_argument("--container-name", default="mc-unified")
    p.add_argument("--container-project-root", default="/workspace/mcbots")
    p.add_argument("--local-shell", default="bash")
    p.add_argument(
        "--local-server-data-template",
        type=Path,
        default=None,
        help="Local mode only: pre-copy this installed server-data template (with run.sh/NeoForge libs) into each case runtime dir to avoid network install.",
    )
    p.add_argument("--case", action="append", default=[], help="Run only selected runtime smoke case(s).")
    p.add_argument("--family", action="append", default=[], choices=list(SUPPORTED_FAMILIES))
    p.add_argument("--sample-from-imported", action="store_true", help="Build runtime smoke cases by sampling imported task configs.")
    p.add_argument("--imported-root", type=Path, default=Path("eval/openha_assets/imported"))
    p.add_argument("--sample-per-family", type=int, default=1, help="When --sample-from-imported, select N tasks per family.")
    p.add_argument("--shuffle", action="store_true", help="Shuffle imported tasks before sampling.")
    p.add_argument("--seed", type=int, default=0, help="Shuffle seed for imported sampling.")
    p.add_argument(
        "--include-non-overworld",
        action="store_true",
        help="When sampling imported tasks, include non-overworld tasks (nether/end).",
    )
    p.add_argument("--list-cases", action="store_true", help="List available default cases and exit.")
    p.add_argument("--task-timeout-sec", type=float, default=3.0, help="Judge timeout inside eval (SKIP_AGENT=true).")
    p.add_argument("--case-timeout-sec", type=float, default=420.0, help="Wall timeout per smoke case.")
    p.add_argument("--wait-rcon-timeout-sec", type=float, default=180.0)
    p.add_argument("--wait-player-timeout-sec", type=float, default=180.0)
    p.add_argument("--judge-interval-sec", type=float, default=0.5)
    p.add_argument("--max-failures", type=int, default=0, help="Stop early after N failures (0 = no limit).")
    p.add_argument("--progress", action="store_true", help="Print podman command and stream more info.")
    p.add_argument("--show-output-on-pass", action="store_true")
    p.add_argument("--output-json", type=Path, default=None)
    p.add_argument("--run-tag", default="", help="Suffix tag for result files/dirs (optional).")
    p.add_argument("--extra-env", action="append", default=[], help="Extra env to pass to wrapper as KEY=VALUE.")
    p.add_argument("--dry-run", action="store_true", help="Print commands only, do not execute podman.")
    return p.parse_args()


def infer_task_dimension(task_obj: Dict[str, Any]) -> str:
    task_dim = str(task_obj.get("dimension") or "").strip().lower()
    if task_dim:
        return task_dim
    seeds = task_obj.get("seeds")
    if isinstance(seeds, list):
        for seed in seeds:
            if isinstance(seed, dict):
                dim = str(seed.get("dimension") or "").strip().lower()
                if dim:
                    return dim
    return "overworld"


def build_cases_from_imported(args: argparse.Namespace, host_project_root: Path) -> List[RuntimeSmokeCase]:
    imported_root = args.imported_root
    if not imported_root.is_absolute():
        imported_root = (host_project_root / imported_root).resolve()
    task_configs_dir = imported_root / "task_configs"
    if not task_configs_dir.exists():
        raise RuntimeError(f"Imported task_configs not found: {task_configs_dir}")

    selected_families = list(args.family) if args.family else list(SUPPORTED_FAMILIES)
    if args.sample_per_family <= 0:
        raise RuntimeError("--sample-per-family must be >= 1")

    import random

    rnd = random.Random(args.seed)
    cases: List[RuntimeSmokeCase] = []
    for family in selected_families:
        cfg_path = task_configs_dir / f"{family}.json"
        if not cfg_path.exists():
            continue
        cfg = load_json_obj(cfg_path)
        items = []
        for task_name, task_obj in cfg.items():
            if not isinstance(task_obj, dict):
                continue
            dim = infer_task_dimension(task_obj)
            if not args.include_non_overworld and dim not in {"", "overworld"}:
                continue
            items.append((task_name, dim))
        if args.shuffle:
            rnd.shuffle(items)
        else:
            items.sort(key=lambda x: x[0])
        picked = items[: args.sample_per_family]
        for idx, (task_name, dim) in enumerate(picked, start=1):
            case_name = f"{family}_{idx:02d}_{canonical_task_slug(task_name)}"
            extra_env: Dict[str, str] = {}
            if dim and dim != "overworld":
                extra_env["TEMPLATE_WORLD_DIR"] = f"{args.container_project_root}/eval/templates/template_1_21_1/world"
            cases.append(RuntimeSmokeCase(name=case_name, task_name=task_name, extra_env=extra_env))
    return cases


def build_case_env(
    *,
    case: RuntimeSmokeCase,
    args: argparse.Namespace,
    execution_project_root: str,
    run_id: str,
    index: int,
    total: int,
) -> Dict[str, str]:
    tag = args.run_tag.strip()
    case_slug = canonical_task_slug(case.task_name)
    run_slug = f"{run_id}-{index:02d}-{case_slug}"
    if tag:
        run_slug = f"{run_slug}-{canonical_task_slug(tag)}"

    base_env: Dict[str, str] = {
        "MCBOTS_PROJECT_ROOT": execution_project_root,
        "TASK_NAME": case.task_name,
        "USE_IMPORTED_TASK_CONFIG": "true",
        "SKIP_AGENT": "true",
        "TASK_TIMEOUT_SEC": str(args.task_timeout_sec),
        "WAIT_RCON_TIMEOUT_SEC": str(args.wait_rcon_timeout_sec),
        "WAIT_PLAYER_TIMEOUT_SEC": str(args.wait_player_timeout_sec),
        "JUDGE_INTERVAL_SEC": str(args.judge_interval_sec),
        "EVAL_CLIENT_COORDINATOR_EXTRA_WAIT_SEC": "5",
        "START_EVAL_CLIENT": "true",
        "EVAL_CLIENT_ENABLE_VNC": "false",
        "EVAL_CLIENT_ENABLE_REMOTE_BASH": "false",
        "EVAL_CLIENT_DISPLAY_RESOLUTION": "640x360x24",
        "AUTO_LOAD_API_MODEL": "false",
        "INIT_WEATHER": "clear",
        "INIT_MOB_SPAWNING": "false",
        "INIT_CLEAR_EXISTING_HOSTILES": "true",
        "INIT_TIME": "day",
        "TASK_INSTRUCTION_MODE": "first",
        "OUTPUT_DIR": f"{execution_project_root}/eval/results/runtime_smoke",
        "TASK_RUNTIME_DIR": f"{execution_project_root}/eval/runtime/runtime_smoke/{run_slug}",
        # Keep log files small and easy to grep.
        "SERVER_LOG_FILE": f"{execution_project_root}/eval/runtime/runtime_smoke/{run_slug}/server.log",
        "EVAL_CLIENT_LOG_FILE": f"{execution_project_root}/eval/runtime/runtime_smoke/{run_slug}/client.log",
        "TASK_SLUG": f"runtime-smoke-{index:02d}-{canonical_task_slug(case.name)}",
        "RUN_TS": run_id.replace("_", ""),
    }
    if case.extra_env:
        base_env.update(case.extra_env)
    base_env.update(parse_kv_list(args.extra_env))
    # Ensure flags we depend on are not accidentally overridden to invalid values.
    base_env["SKIP_AGENT"] = "true"
    base_env["USE_IMPORTED_TASK_CONFIG"] = "true"
    return base_env


def build_podman_cmd(
    *,
    args: argparse.Namespace,
    envs: Dict[str, str],
) -> List[str]:
    cmd: List[str] = ["podman", "exec"]
    for k, v in sorted(envs.items()):
        cmd.extend(["--env", f"{k}={v}"])
    cmd.extend(
        [
            args.container_name,
            "bash",
            "-lc",
            f"cd {args.container_project_root} && scripts/eval/run-openha-task-eval-inside.sh",
        ]
    )
    return cmd


def build_local_cmd(*, args: argparse.Namespace, host_project_root: Path) -> List[str]:
    return [
        args.local_shell,
        "-lc",
        f"cd {host_project_root} && scripts/eval/run-openha-task-eval-inside.sh",
    ]


def preseed_local_server_data_if_needed(
    *,
    args: argparse.Namespace,
    envs: Dict[str, str],
) -> None:
    if args.execution_mode != "local" or args.local_server_data_template is None:
        return
    src = args.local_server_data_template
    if not src.is_absolute():
        src = (args.project_root.resolve() / src).resolve()
    if not src.exists():
        raise RuntimeError(f"local server-data template not found: {src}")
    if not src.is_dir():
        raise RuntimeError(f"local server-data template is not a directory: {src}")

    runtime_dir = Path(envs["TASK_RUNTIME_DIR"])
    server_data_dir = Path(envs.get("SERVER_DATA_DIR", runtime_dir / "server-data"))
    if server_data_dir.exists():
        shutil.rmtree(server_data_dir)
    server_data_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, server_data_dir)


def run_case(
    *,
    case: RuntimeSmokeCase,
    args: argparse.Namespace,
    host_project_root: Path,
    run_id: str,
    index: int,
    total: int,
) -> RuntimeSmokeResult:
    execution_project_root = (
        args.container_project_root if args.execution_mode == "podman" else str(host_project_root)
    )
    envs = build_case_env(
        case=case,
        args=args,
        execution_project_root=execution_project_root,
        run_id=run_id,
        index=index,
        total=total,
    )
    cmd = (
        build_podman_cmd(args=args, envs=envs)
        if args.execution_mode == "podman"
        else build_local_cmd(args=args, host_project_root=host_project_root)
    )

    if args.progress or args.dry_run:
        print(f"[case {index}/{total}] {case.name} task={case.task_name}")
        print(f"  command ({args.execution_mode}):")
        print("   ", " ".join(subprocess.list2cmdline([c]).strip('\"') if " " in c else c for c in cmd))
        if args.progress:
            print(f"  env overrides ({len(envs)}):")
            for k in sorted(envs):
                print(f"    {k}={envs[k]}")

    if args.dry_run:
        return RuntimeSmokeResult(
            name=case.name,
            task_name=case.task_name,
            family=infer_family_from_task_name(case.task_name),
            ok=True,
            rc=0,
            duration_ms=0,
            timeout=False,
        )

    preseed_local_server_data_if_needed(args=args, envs=envs)

    t0 = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=args.case_timeout_sec,
            env={**os.environ, **envs} if args.execution_mode == "local" else None,
        )
        rc = proc.returncode
        output = proc.stdout or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = 124
        captured = e.stdout or ""
        output = captured if isinstance(captured, str) else captured.decode("utf-8", errors="replace")
    duration_ms = int((time.time() - t0) * 1000)

    result_path_container: Optional[str] = None
    m = RESULT_PATH_RE.search(output)
    if m:
        result_path_container = m.group("path").strip()

    result_path_host: Optional[Path] = None
    result_json: Optional[dict] = None
    if result_path_container:
        result_path_host = resolve_result_host_path(
            result_path_str=result_path_container,
            execution_mode=args.execution_mode,
            host_project_root=host_project_root,
            container_project_root=args.container_project_root,
        )
        if result_path_host and result_path_host.exists():
            try:
                with result_path_host.open("r", encoding="utf-8") as f:
                    result_json = json.load(f)
            except Exception:
                result_json = None

    ok = (
        (not timed_out)
        and rc == 0
        and "Traceback" not in output
        and result_json is not None
        and result_json.get("task_name") == case.task_name
        and result_json.get("dry_run") is not True
    )
    # In SKIP_AGENT smoke, timeout is the expected judge reason.
    if ok and result_json.get("reason") not in {"timeout", "agent_exited"}:
        ok = False

    failure_kind = None
    if not ok:
        failure_kind = classify_failure(
            rc=rc,
            timed_out=timed_out,
            output=output,
            result_path_host=result_path_host,
            result_json=result_json,
        )

    result = RuntimeSmokeResult(
        name=case.name,
        task_name=case.task_name,
        family=infer_family_from_task_name(case.task_name),
        ok=ok,
        rc=rc,
        duration_ms=duration_ms,
        timeout=timed_out,
        result_path_container=result_path_container,
        result_path_host=str(result_path_host) if result_path_host else None,
        result_reason=result_json.get("reason") if isinstance(result_json, dict) else None,
        result_success=result_json.get("success") if isinstance(result_json, dict) else None,
        result_delta_score=result_json.get("delta_score") if isinstance(result_json, dict) else None,
        failure_kind=failure_kind,
            output_tail=None if ok and not args.show_output_on_pass else "\n".join(output.splitlines()[-60:]),
    )
    return result


def main() -> int:
    args = parse_args()
    host_project_root = args.project_root.resolve()

    if args.sample_from_imported:
        try:
            cases = build_cases_from_imported(args, host_project_root)
        except Exception as e:
            print(f"[error] failed to build imported sample cases: {e}")
            return 2
        source_mode = "imported_sample"
    else:
        cases = list(DEFAULT_CASES)
        source_mode = "default"

    if args.list_cases:
        for case in cases:
            print(f"{case.name}\t{case.task_name}")
        return 0

    selected = set(args.case) if args.case else None
    cases = [c for c in cases if not selected or c.name in selected]
    if selected:
        known = {c.name for c in cases}
        missing = sorted(selected - known)
        if missing:
            print(f"[error] unknown case(s): {missing}")
            return 2
    if not cases:
        print("[error] no runtime smoke cases selected")
        return 2

    out_json = args.output_json
    if out_json is None:
        out_json = host_project_root / "runtime" / f"runtime_smoke_{utc_now_compact()}.json"
    elif not out_json.is_absolute():
        out_json = (host_project_root / out_json).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)

    run_id = utc_now_compact()
    print(f"[info] host_project_root={host_project_root}")
    print(f"[info] execution_mode={args.execution_mode}")
    if args.execution_mode == "podman":
        print(f"[info] container={args.container_name} root={args.container_project_root}")
    else:
        print(f"[info] local_shell={args.local_shell}")
    print(f"[info] cases={len(cases)} run_id={run_id} source={source_mode}")
    if args.dry_run:
        print("[info] dry_run=true (podman commands will not execute)")

    t0 = time.time()
    results: List[RuntimeSmokeResult] = []
    passed = 0
    failures = 0
    for idx, case in enumerate(cases, start=1):
        res = run_case(
            case=case,
            args=args,
            host_project_root=host_project_root,
            run_id=run_id,
            index=idx,
            total=len(cases),
        )
        results.append(res)
        if res.ok:
            passed += 1
            print(
                f"[PASS] {case.name} rc={res.rc} reason={res.result_reason} "
                f"dur={res.duration_ms}ms"
            )
        else:
            failures += 1
            print(
                f"[FAIL] {case.name} rc={res.rc} failure={res.failure_kind} "
                f"reason={res.result_reason} dur={res.duration_ms}ms"
            )
            if res.output_tail:
                print("  output tail:")
                print(res.output_tail)
            if args.max_failures > 0 and failures >= args.max_failures:
                print(f"[info] max failures reached ({args.max_failures}); stopping early")
                break

    elapsed_sec = time.time() - t0
    by_family: Dict[str, Dict[str, int]] = {}
    reason_counts: Dict[str, int] = {}
    failure_kind_counts: Dict[str, int] = {}
    for r in results:
        fam = r.family
        fam_stats = by_family.setdefault(fam, {"total": 0, "passed": 0, "failed": 0})
        fam_stats["total"] += 1
        if r.ok:
            fam_stats["passed"] += 1
        else:
            fam_stats["failed"] += 1
        if r.result_reason:
            reason_counts[r.result_reason] = reason_counts.get(r.result_reason, 0) + 1
        if r.failure_kind:
            failure_kind_counts[r.failure_kind] = failure_kind_counts.get(r.failure_kind, 0) + 1

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host_project_root": str(host_project_root),
        "container_name": args.container_name,
        "container_project_root": args.container_project_root,
        "execution_mode": args.execution_mode,
        "cases": len(cases),
        "case_source": source_mode,
        "passed": passed,
        "failed": len(results) - passed,
        "executed": len(results),
        "aborted_early": len(results) < len(cases),
        "pass_rate": round((passed / len(results)) if results else 0.0, 4),
        "elapsed_sec": round(elapsed_sec, 3),
        "dry_run": bool(args.dry_run),
        "task_timeout_sec": args.task_timeout_sec,
        "case_timeout_sec": args.case_timeout_sec,
        "by_family": by_family,
        "reason_counts": reason_counts,
        "failure_kind_counts": failure_kind_counts,
        "results": [asdict(r) for r in results],
    }
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=True)
        f.write("\n")

    print(f"[done] summary saved: {out_json}")
    if by_family:
        print("[done] by_family:")
        for fam in sorted(by_family):
            stats = by_family[fam]
            print(
                f"  - {fam}: passed={stats['passed']}/{stats['total']} "
                f"failed={stats['failed']}"
            )
    print(f"[done] passed={passed}/{len(results)} (selected={len(cases)}) elapsed={elapsed_sec:.1f}s")
    return 0 if passed == len(results) and len(results) == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
