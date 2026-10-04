#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def load_json_obj(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def normalize_dimension(raw: Any) -> str:
    if raw is None:
        return "overworld"
    if isinstance(raw, int):
        return {0: "overworld", -1: "the_nether", 1: "the_end"}.get(raw, str(raw))
    if isinstance(raw, str):
        s = raw.strip().lower()
        aliases = {
            "0": "overworld",
            "-1": "the_nether",
            "1": "the_end",
            "overworld": "overworld",
            "minecraft:overworld": "overworld",
            "nether": "the_nether",
            "the_nether": "the_nether",
            "minecraft:the_nether": "the_nether",
            "end": "the_end",
            "the_end": "the_end",
            "minecraft:the_end": "the_end",
        }
        return aliases.get(s, s or "overworld")
    return "overworld"


def canonicalize_task_name_compat(task_name: str) -> str:
    if task_name.startswith("craft item "):
        suffix = task_name[len("craft item ") :].strip()
        if suffix:
            return f"craft_item:{suffix.replace(' ', '_')}"
    return task_name.strip()


def task_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    slug = slug.strip("._-")
    return slug or "task"


def version_slug(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", version).strip("_") or "version"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Resolve upgraded/raw snapshot world path for an OpenHA task from snapshot-group plan."
    )
    p.add_argument("--task-name", required=True)
    p.add_argument("--task-config", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--snapshot-root", type=Path, default=Path("eval/snapshots"))
    p.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--world-kind", choices=["raw", "upgraded"], default="upgraded")
    p.add_argument("--raw-version", default="1.16.5")
    p.add_argument("--upgraded-version", default="1.21.1")
    p.add_argument(
        "--allow-missing-world",
        action="store_true",
        help="Resolve and print path even if world dir does not exist yet.",
    )
    p.add_argument("--json", action="store_true", help="Print JSON payload instead of plain path.")
    return p.parse_args()


def load_task_seed_identity(task_config_path: Path, task_name: str) -> Tuple[int, str]:
    cfg_all = load_json_obj(task_config_path)
    if task_name not in cfg_all:
        raise KeyError(f"Task not found in config: {task_name}")
    task_cfg = cfg_all[task_name]
    if not isinstance(task_cfg, dict):
        raise RuntimeError(f"Task config is not object: {task_name}")
    seeds = task_cfg.get("seeds")
    if not isinstance(seeds, list) or not seeds or not isinstance(seeds[0], dict):
        raise RuntimeError(f"Task has invalid seeds[0]: {task_name}")
    seed0 = seeds[0]
    try:
        seed = int(seed0.get("seed"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Task seed is invalid: {task_name}: {seed0.get('seed')!r}") from e
    dim = normalize_dimension(seed0.get("dimension", task_cfg.get("dimension")))
    return seed, dim


def group_matches_task_name(group: Dict[str, Any], task_name: str) -> bool:
    raw_tasks = group.get("tasks")
    if not isinstance(raw_tasks, list):
        return False
    target = canonicalize_task_name_compat(task_name)
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        candidate = item.get("task_name")
        if not isinstance(candidate, str):
            continue
        if canonicalize_task_name_compat(candidate) == target:
            return True
    return False


def group_matches_seed_dimension(group: Dict[str, Any], seed: int, dimension: str) -> bool:
    gk = group.get("group_key")
    if not isinstance(gk, dict):
        return False
    try:
        g_seed = int(gk.get("seed"))
    except Exception:
        return False
    g_dim = normalize_dimension(gk.get("dimension"))
    return g_seed == seed and g_dim == dimension


def choose_group(plan: Dict[str, Any], task_name: str, seed: int, dimension: str) -> Tuple[Dict[str, Any], str]:
    groups = plan.get("groups")
    if not isinstance(groups, list):
        raise RuntimeError("Plan missing groups[]")

    exact_name = [g for g in groups if isinstance(g, dict) and group_matches_task_name(g, task_name)]
    if exact_name:
        narrowed = [g for g in exact_name if group_matches_seed_dimension(g, seed, dimension)]
        if len(narrowed) == 1:
            return narrowed[0], "task_name+seed+dimension"
        if len(exact_name) == 1:
            return exact_name[0], "task_name"
        if narrowed:
            raise RuntimeError(f"Ambiguous snapshot groups for task (seed+dimension narrowed to {len(narrowed)} groups)")
        raise RuntimeError(f"Ambiguous snapshot groups for task_name={task_name!r}: {len(exact_name)} groups")

    by_seed_dim = [g for g in groups if isinstance(g, dict) and group_matches_seed_dimension(g, seed, dimension)]
    if len(by_seed_dim) == 1:
        return by_seed_dim[0], "seed+dimension_fallback"
    if not by_seed_dim:
        raise RuntimeError(f"No snapshot group matches seed={seed} dimension={dimension}")
    raise RuntimeError(f"Ambiguous snapshot groups for seed={seed} dimension={dimension}: {len(by_seed_dim)} groups")


def expected_world_paths(
    *,
    project_root: Path,
    snapshot_root: Path,
    group_id: str,
    seed: int,
    raw_version: str,
    upgraded_version: str,
) -> Dict[str, Path]:
    root = snapshot_root if snapshot_root.is_absolute() else (project_root / snapshot_root)
    slug = task_slug(group_id)
    return {
        "raw_world": root / f"seed_raw_{version_slug(raw_version)}" / slug / str(seed) / "world",
        "upgraded_world": root / f"seed_upgraded_{version_slug(upgraded_version)}" / slug / str(seed) / "world",
        "raw_meta": root / f"seed_raw_{version_slug(raw_version)}" / slug / str(seed) / "snapshot_meta.json",
        "upgraded_meta": root / f"seed_upgraded_{version_slug(upgraded_version)}" / slug / str(seed) / "snapshot_meta.json",
    }


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    task_name = canonicalize_task_name_compat(args.task_name)
    task_config_path = args.task_config if args.task_config.is_absolute() else (project_root / args.task_config)
    plan_path = args.plan if args.plan.is_absolute() else (project_root / args.plan)

    seed, dimension = load_task_seed_identity(task_config_path.resolve(), task_name)
    plan = load_json_obj(plan_path.resolve())
    group, matched_by = choose_group(plan, task_name, seed, dimension)
    group_id = str(group.get("group_id", "group"))

    paths = expected_world_paths(
        project_root=project_root,
        snapshot_root=args.snapshot_root,
        group_id=group_id,
        seed=seed,
        raw_version=args.raw_version,
        upgraded_version=args.upgraded_version,
    )
    world_path = paths[f"{args.world_kind}_world"]
    world_exists = world_path.is_dir()
    if not world_exists and not args.allow_missing_world:
        eprint(f"snapshot world not found: {world_path}")
        return 1

    if args.json:
        payload = {
            "success": True,
            "task_name": task_name,
            "seed": seed,
            "dimension": dimension,
            "matched_by": matched_by,
            "group_id": group_id,
            "world_kind": args.world_kind,
            "world_path": str(world_path),
            "world_exists": world_exists,
            "meta_path": str(paths[f'{args.world_kind}_meta']),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(str(world_path))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
