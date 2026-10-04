#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence


@dataclass(frozen=True)
class WrapperCase:
    name: str
    task_name: str
    expected_patterns: Sequence[str]


CASES: List[WrapperCase] = [
    WrapperCase(
        name="kill_entity_cow",
        task_name="kill_entity:cow",
        expected_patterns=[
            r"python3\s+.+/eval/kill_entity_eval_runner\.py\b",
            r"--task-config\s+.+/eval/openha_assets/imported/task_configs/kill_entity\.json\b",
            r"--task-name\s+kill_entity:cow\b",
            r"--task-instruction-file\s+.+/eval/openha_assets/imported/instructions_by_task\.json\b",
        ],
    ),
    WrapperCase(
        name="smelt_item_baked_potato",
        task_name="smelt_item:baked_potato",
        expected_patterns=[
            r"python3\s+.+/eval/smelt_item_eval_runner\.py\b",
            r"--task-config\s+.+/eval/openha_assets/imported/task_configs/smelt_item\.json\b",
            r"--task-name\s+smelt_item:baked_potato\b",
            r"--task-instruction-file\s+.+/eval/openha_assets/imported/instructions_by_task\.json\b",
        ],
    ),
    WrapperCase(
        name="legacy_craft_item_key_canonicalized",
        task_name="craft item crafting_table",
        expected_patterns=[
            r"python3\s+.+/eval/craft_item_eval_runner\.py\b",
            r"--task-config\s+.+/eval/openha_assets/imported/task_configs/craft_item\.json\b",
            r"--task-name\s+craft_item:crafting_table\b",
            r"start task=craft_item:crafting_table\b",
        ],
    ),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smoke-test eval/run-openha-task-eval-inside.sh routing in DRY_RUN mode.")
    p.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--case", action="append", default=[], help="Run only selected case(s)")
    p.add_argument("--show-output", action="store_true")
    return p.parse_args()


def run_case(project_root: Path, case: WrapperCase, show_output: bool) -> bool:
    wrapper = project_root / "scripts" / "eval" / "run-openha-task-eval-inside.sh"
    env = os.environ.copy()
    env["DRY_RUN"] = "true"
    env["TASK_NAME"] = case.task_name
    # This smoke test validates wrapper routing/canonicalization only.
    # Disable runtime resource requirements so missing local snapshots/templates do not mask routing regressions.
    env["OPENHA_SNAPSHOT_TEMPLATE_MODE"] = "off"
    env["EVAL_CLIENT_TEMPLATE_MODE"] = "off"
    env["EVAL_SERVER_DATA_TEMPLATE_MODE"] = "off"
    env["START_EVAL_CLIENT"] = "false"
    proc = subprocess.run(
        [str(wrapper)],
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    out = proc.stdout or ""
    ok = proc.returncode == 0 and "Traceback" not in out
    missing = []
    for pat in case.expected_patterns:
        if not re.search(pat, out):
            ok = False
            missing.append(pat)

    if ok:
        print(f"[PASS] {case.name}")
        if show_output:
            print(out.rstrip())
        return True

    print(f"[FAIL] {case.name}: rc={proc.returncode}")
    if missing:
        print("  missing patterns:")
        for pat in missing:
            print(f"    - {pat}")
    tail = "\n".join(out.splitlines()[-60:])
    if tail:
        print("  output tail:")
        print(tail)
    return False


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    selected = set(args.case) if args.case else None
    cases = [c for c in CASES if not selected or c.name in selected]
    if selected:
        missing = sorted(selected - {c.name for c in cases})
        if missing:
            print(f"[error] unknown case(s): {missing}")
            return 2
    print(f"[info] project_root={project_root}")
    print(f"[info] cases={len(cases)}")
    passed = 0
    for case in cases:
        if run_case(project_root, case, args.show_output):
            passed += 1
    print(f"[done] passed={passed}/{len(cases)}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
