#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SUPPORTED_FAMILY_RUNNERS = {
    "kill_entity": "eval/kill_entity_eval_runner.py",
    "mine_block": "eval/mine_block_family_eval_runner.py",
    "interact_block": "eval/interact_block_eval_runner.py",
    "craft_item": "eval/craft_item_eval_runner.py",
    "smelt_item": "eval/smelt_item_eval_runner.py",
}


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json_obj(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return data


@dataclass(frozen=True)
class TaskEntry:
    family: str
    task_name: str
    config_path: str


@dataclass
class TaskResult:
    family: str
    task_name: str
    config_path: str
    ok: bool
    rc: int
    duration_ms: int
    failure_kind: Optional[str] = None
    ensure_objective_line: Optional[str] = None
    output_tail: Optional[str] = None


def classify_failure(output: str, rc: int) -> str:
    if "seed entry without position" in output:
        return "missing_position"
    if "seed entry without seed" in output:
        return "missing_seed"
    if "No task instruction found in instruction file" in output:
        return "missing_instruction"
    if "FileNotFoundError" in output and "task config" in output.lower():
        return "missing_task_config"
    if "task instruction file not found" in output:
        return "missing_instruction_file"
    if "unsupported" in output.lower() and "distraction" in output.lower():
        return "bad_distraction_arg"
    if "Traceback" in output:
        return "traceback"
    if rc != 0:
        return "nonzero_exit"
    return "unknown"


def iter_task_entries(
    imported_root: Path,
    *,
    selected_families: Optional[set[str]],
    selected_tasks: Optional[set[str]],
) -> Iterable[TaskEntry]:
    task_configs_dir = imported_root / "task_configs"
    for family, runner in SUPPORTED_FAMILY_RUNNERS.items():
        _ = runner
        if selected_families and family not in selected_families:
            continue
        cfg_path = task_configs_dir / f"{family}.json"
        if not cfg_path.exists():
            continue
        cfg = load_json_obj(cfg_path)
        for task_name in sorted(cfg.keys()):
            if selected_tasks and task_name not in selected_tasks:
                continue
            yield TaskEntry(family=family, task_name=task_name, config_path=str(cfg_path))


def run_one(
    *,
    project_root: Path,
    python_exe: str,
    imported_root: Path,
    entry: TaskEntry,
    extra_args: List[str],
) -> TaskResult:
    runner_rel = SUPPORTED_FAMILY_RUNNERS[entry.family]
    runner_path = project_root / runner_rel
    instructions_path = imported_root / "instructions_by_task.json"
    cmd = [
        python_exe,
        str(runner_path),
        "--dry-run",
        "--task-config",
        entry.config_path,
        "--task-name",
        entry.task_name,
    ]
    if instructions_path.exists():
        cmd += ["--task-instruction-file", str(instructions_path)]
    cmd += extra_args

    t0 = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    out = proc.stdout or ""
    ok = proc.returncode == 0 and "Traceback" not in out

    ensure_line = None
    for line in out.splitlines():
        if "ensure objective:" in line:
            ensure_line = line.strip()
            break

    if ok:
        return TaskResult(
            family=entry.family,
            task_name=entry.task_name,
            config_path=entry.config_path,
            ok=True,
            rc=proc.returncode,
            duration_ms=elapsed_ms,
            ensure_objective_line=ensure_line,
        )

    return TaskResult(
        family=entry.family,
        task_name=entry.task_name,
        config_path=entry.config_path,
        ok=False,
        rc=proc.returncode,
        duration_ms=elapsed_ms,
        failure_kind=classify_failure(out, proc.returncode),
        ensure_objective_line=ensure_line,
        output_tail="\n".join(out.splitlines()[-40:]),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run dry-run matrix validation for imported OpenHA tasks.")
    p.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--imported-root", type=Path, default=Path("eval/openha_assets/imported"))
    p.add_argument("--python", dest="python_exe", default=sys.executable)
    p.add_argument("--family", action="append", default=[], choices=sorted(SUPPORTED_FAMILY_RUNNERS))
    p.add_argument("--task", action="append", default=[])
    p.add_argument("--limit", type=int, default=0, help="Limit number of tasks (0 = all)")
    p.add_argument("--shuffle", action="store_true")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--progress-every", type=int, default=25)
    p.add_argument("--max-failures", type=int, default=0, help="Stop early after N failures (0 = no limit)")
    p.add_argument("--output-json", type=Path, default=None)
    p.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra arg passed through to every runner (repeatable, e.g. --extra-arg=--init-inventory-distraction-mode --extra-arg=random)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    imported_root = args.imported_root
    if not imported_root.is_absolute():
        imported_root = (project_root / imported_root).resolve()

    selected_families = set(args.family) if args.family else None
    selected_tasks = set(args.task) if args.task else None
    entries = list(
        iter_task_entries(
            imported_root,
            selected_families=selected_families,
            selected_tasks=selected_tasks,
        )
    )
    if args.shuffle:
        rnd = random.Random(args.seed)
        rnd.shuffle(entries)
    if args.limit > 0:
        entries = entries[: args.limit]

    if not entries:
        print("[error] no tasks selected")
        return 2

    out_json = args.output_json
    if out_json is None:
        out_json = project_root / "runtime" / f"dryrun_matrix_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    elif not out_json.is_absolute():
        out_json = (project_root / out_json).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)

    print(f"[info] project_root={project_root}")
    print(f"[info] imported_root={imported_root}")
    print(f"[info] tasks={len(entries)}")

    results: List[TaskResult] = []
    t0 = time.time()
    family_counts: Dict[str, int] = {}
    pass_counts: Dict[str, int] = {}
    failure_counts: Dict[str, int] = {}

    for idx, entry in enumerate(entries, start=1):
        family_counts[entry.family] = family_counts.get(entry.family, 0) + 1
        res = run_one(
            project_root=project_root,
            python_exe=args.python_exe,
            imported_root=imported_root,
            entry=entry,
            extra_args=list(args.extra_arg),
        )
        results.append(res)
        if res.ok:
            pass_counts[entry.family] = pass_counts.get(entry.family, 0) + 1
        else:
            kind = res.failure_kind or "unknown"
            failure_counts[kind] = failure_counts.get(kind, 0) + 1

        if (idx % max(1, args.progress_every) == 0) or (not res.ok):
            print(
                f"[progress] {idx}/{len(entries)} "
                f"pass={sum(1 for r in results if r.ok)} fail={sum(1 for r in results if not r.ok)} "
                f"last={entry.task_name} {'PASS' if res.ok else 'FAIL:' + str(res.failure_kind)}"
            )

        if args.max_failures > 0 and sum(1 for r in results if not r.ok) >= args.max_failures:
            print(f"[stop] reached max failures={args.max_failures}")
            break

    elapsed_sec = time.time() - t0
    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed

    summary = {
        "generated_at_utc": utc_ts(),
        "project_root": str(project_root),
        "imported_root": str(imported_root),
        "total_selected": len(entries),
        "total_executed": len(results),
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / len(results)) if results else 0.0,
        "elapsed_sec": elapsed_sec,
        "family_counts": family_counts,
        "family_pass_counts": pass_counts,
        "failure_kind_counts": failure_counts,
        "results": [asdict(r) for r in results],
    }
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[done] passed={passed}/{len(results)} failed={failed} pass_rate={summary['pass_rate']:.3f}")
    print(f"[done] elapsed_sec={elapsed_sec:.1f}")
    print(f"[done] summary={out_json}")
    if failed:
        print("[done] failure kinds:", failure_counts)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
