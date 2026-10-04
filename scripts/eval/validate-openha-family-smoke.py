#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence


@dataclass(frozen=True)
class SmokeCase:
    name: str
    family: str
    task_name: str
    runner_relpath: str
    task_config_relpath: str
    expected_patterns: Sequence[str]
    forbidden_patterns: Sequence[str] = ()
    extra_args: Sequence[str] = ()


CASES: List[SmokeCase] = [
    SmokeCase(
        name="kill_sheep_generic",
        family="kill_entity",
        task_name="kill_entity:sheep",
        runner_relpath="eval/kill_entity_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/kill_entity.json",
        expected_patterns=[
            r"ensure objective: oha_kill_entity minecraft\.killed:minecraft\.sheep",
            r"summon minecraft:sheep\b",
        ],
    ),
    SmokeCase(
        name="kill_cow_generic",
        family="kill_entity",
        task_name="kill_entity:cow",
        runner_relpath="eval/kill_entity_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/kill_entity.json",
        expected_patterns=[
            r"ensure objective: oha_kill_entity minecraft\.killed:minecraft\.cow",
            r"summon minecraft:cow\b",
        ],
    ),
    SmokeCase(
        name="mine_dirt_generic",
        family="mine_block",
        task_name="mine_block:dirt",
        runner_relpath="eval/mine_block_family_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/mine_block.json",
        expected_patterns=[
            r"ensure objective: oha_mine_block minecraft\.mined:minecraft\.dirt",
        ],
    ),
    SmokeCase(
        name="mine_oak_log_generic",
        family="mine_block",
        task_name="mine_block:oak_log",
        runner_relpath="eval/mine_block_family_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/mine_block.json",
        expected_patterns=[
            r"ensure objective: oha_mine_block minecraft\.mined:minecraft\.oak_log",
        ],
    ),
    SmokeCase(
        name="interact_anvil",
        family="interact_block",
        task_name="custom:interact_with_anvil",
        runner_relpath="eval/interact_block_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/interact_block.json",
        expected_patterns=[
            r"ensure objective: oha_interact minecraft\.custom:minecraft\.interact_with_anvil",
            r"setblock \^0 \^0 \^5 minecraft:anvil",
        ],
    ),
    SmokeCase(
        name="craft_stick",
        family="craft_item",
        task_name="craft_item:stick",
        runner_relpath="eval/craft_item_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/craft_item.json",
        expected_patterns=[
            r"ensure objective: oha_craft_item minecraft\.crafted:minecraft\.stick",
        ],
    ),
    SmokeCase(
        name="smelt_baked_potato",
        family="smelt_item",
        task_name="smelt_item:baked_potato",
        runner_relpath="eval/smelt_item_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/smelt_item.json",
        expected_patterns=[
            r"ensure objective: oha_smelt_item minecraft\.crafted:minecraft\.baked_potato",
            r"setblock \^0 \^0 \^5 minecraft:furnace",
        ],
    ),
    SmokeCase(
        name="smelt_baked_potato_inventory_distraction",
        family="smelt_item",
        task_name="smelt_item:baked_potato",
        runner_relpath="eval/smelt_item_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/smelt_item.json",
        extra_args=[
            "--init-inventory-distraction-mode",
            "random",
            "--init-inventory-distraction-level",
            "one",
        ],
        expected_patterns=[
            r"ensure objective: oha_smelt_item minecraft\.crafted:minecraft\.baked_potato",
            r"setblock \^0 \^0 \^5 minecraft:furnace",
            r"item replace entity BotCPU (hotbar|inventory)\.\d+ with minecraft:[a-z0-9_]+ [1-9]\d*",
        ],
        forbidden_patterns=[
            r"item replace entity BotCPU weapon\.mainhand with ",
        ],
    ),
    SmokeCase(
        name="smelt_baked_potato_inventory_distraction_difficulty_alias",
        family="smelt_item",
        task_name="smelt_item:baked_potato",
        runner_relpath="eval/smelt_item_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/smelt_item.json",
        extra_args=[
            "--init-inventory-distraction-mode",
            "random",
            "--init-inventory-distraction-level",
            "difficulty",
        ],
        expected_patterns=[
            r"ensure objective: oha_smelt_item minecraft\.crafted:minecraft\.baked_potato",
            r"setblock \^0 \^0 \^5 minecraft:furnace",
        ],
    ),
    SmokeCase(
        name="kill_sheep_equip_distraction_one",
        family="kill_entity",
        task_name="kill_entity:sheep",
        runner_relpath="eval/kill_entity_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/kill_entity.json",
        extra_args=[
            "--init-equip-distraction-mode",
            "random",
            "--init-equip-distraction-level",
            "one",
        ],
        expected_patterns=[
            r"ensure objective: oha_kill_entity minecraft\.killed:minecraft\.sheep",
            r"item replace entity BotCPU (armor|weapon)\.[a-z]+ with minecraft:[a-z0-9_]+ 1",
        ],
    ),
    SmokeCase(
        name="interact_anvil_inventory_distraction",
        family="interact_block",
        task_name="custom:interact_with_anvil",
        runner_relpath="eval/interact_block_eval_runner.py",
        task_config_relpath="eval/openha_assets/imported/task_configs/interact_block.json",
        extra_args=[
            "--init-inventory-distraction-mode",
            "random",
            "--init-inventory-distraction-level",
            "one",
        ],
        expected_patterns=[
            r"ensure objective: oha_interact minecraft\.custom:minecraft\.interact_with_anvil",
            r"setblock \^0 \^0 \^5 minecraft:anvil",
            r"item replace entity BotCPU (hotbar|inventory)\.\d+ with minecraft:[a-z0-9_]+ [1-9]\d*",
        ],
        forbidden_patterns=[
            r"item replace entity BotCPU weapon\.mainhand with ",
        ],
    ),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run dry-run smoke validation across OpenHA family runners.")
    p.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--python", dest="python_exe", default=sys.executable)
    p.add_argument("--case", action="append", default=[], help="Run only selected case(s).")
    p.add_argument("--show-output", action="store_true", help="Print full command output for passing cases.")
    return p.parse_args()


def run_case(project_root: Path, python_exe: str, case: SmokeCase, show_output: bool) -> bool:
    runner = project_root / case.runner_relpath
    cfg = project_root / case.task_config_relpath
    if not runner.exists():
        print(f"[FAIL] {case.name}: missing runner {runner}")
        return False
    if not cfg.exists():
        print(f"[FAIL] {case.name}: missing task config {cfg}")
        return False

    cmd = [
        python_exe,
        str(runner),
        "--dry-run",
        "--task-config",
        str(cfg),
        "--task-name",
        case.task_name,
    ]
    cmd.extend(case.extra_args)
    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    out = proc.stdout or ""
    ok = proc.returncode == 0
    if ok and "Traceback" in out:
        ok = False
    missing = []
    for pat in case.expected_patterns:
        if not re.search(pat, out):
            ok = False
            missing.append(pat)
    unexpected = []
    for pat in case.forbidden_patterns:
        if re.search(pat, out):
            ok = False
            unexpected.append(pat)

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
    if unexpected:
        print("  forbidden patterns matched:")
        for pat in unexpected:
            print(f"    - {pat}")
    tail = "\n".join(out.splitlines()[-40:])
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
        if run_case(project_root, args.python_exe, case, args.show_output):
            passed += 1
    print(f"[done] passed={passed}/{len(cases)}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
