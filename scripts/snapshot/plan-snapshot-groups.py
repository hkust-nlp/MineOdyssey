#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PLAN_FORMAT_VERSION = "mcbots.snapshot-group-plan.v1"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-") or "value"


def load_json_obj(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def parse_int(value: Any, *, context: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{context}: boolean is not a valid integer")
    try:
        return int(value)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{context}: failed to parse integer from {value!r}") from e


def parse_position(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        return (int(value[0]), int(value[1]), int(value[2]))
    except Exception:
        return None


def normalize_dimension(raw: Any, default_dimension: str) -> str:
    if raw is None:
        return default_dimension
    if isinstance(raw, int):
        if raw == 0:
            return "overworld"
        if raw == -1:
            return "the_nether"
        if raw == 1:
            return "the_end"
        return str(raw)
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
        return aliases.get(s, s or default_dimension)
    return default_dimension


def infer_dimension_from_labels(labels: Sequence[str]) -> Optional[str]:
    normalized = {str(x).strip().lower() for x in labels if str(x).strip()}
    if any(x in {"nether", "the_nether", "minecraft:the_nether"} for x in normalized):
        return "the_nether"
    if any(x in {"end", "the_end", "minecraft:the_end"} for x in normalized):
        return "the_end"
    return None


def family_from_task_name(task_name: str) -> str:
    if task_name.startswith("craft item "):
        return "craft_item"
    if ":" in task_name:
        return task_name.split(":", 1)[0]
    return task_name


def extract_pregen_radius(
    task_spec: Dict[str, Any],
    seed_entry: Dict[str, Any],
    default_radius: int,
) -> int:
    for source in (seed_entry, task_spec):
        if not isinstance(source, dict):
            continue
        if "snapshot_pregen_radius_chunks" in source:
            return max(0, parse_int(source["snapshot_pregen_radius_chunks"], context="snapshot_pregen_radius_chunks"))
        if "pregen_radius_chunks" in source:
            return max(0, parse_int(source["pregen_radius_chunks"], context="pregen_radius_chunks"))
    return max(0, default_radius)


@dataclass(frozen=True)
class TaskSeedPoint:
    task_name: str
    family: str
    config_path: str
    seed_index: int
    seed: int
    seed_str: str
    dimension: str
    position: Optional[Tuple[int, int, int]]
    pregen_radius_chunks: int
    labels: List[str]


@dataclass(frozen=True)
class GroupKey:
    seed: int
    seed_str: str
    raw_version: str
    upgraded_version: str
    dimension: str

    def stable_id(self) -> str:
        return (
            f"seed_{self.seed_str}__{slugify(self.dimension)}__"
            f"{slugify(self.raw_version)}_to_{slugify(self.upgraded_version)}"
        )


def iter_task_seed_points(
    config_paths: Sequence[Path],
    *,
    selected_tasks: Optional[set[str]],
    default_dimension: str,
    default_pregen_radius_chunks: int,
    allow_missing_position: bool,
) -> Iterable[TaskSeedPoint]:
    for config_path in config_paths:
        cfg = load_json_obj(config_path)
        for task_name, task_spec in cfg.items():
            if selected_tasks and task_name not in selected_tasks:
                continue
            if not isinstance(task_spec, dict):
                eprint(f"[warn] skip invalid task spec (not object): {config_path}:{task_name}")
                continue
            seeds = task_spec.get("seeds")
            if not isinstance(seeds, list) or not seeds:
                eprint(f"[warn] skip task without seeds[]: {config_path}:{task_name}")
                continue
            labels = [str(x) for x in task_spec.get("label", [])] if isinstance(task_spec.get("label"), list) else []
            label_dim = infer_dimension_from_labels(labels)
            task_level_pos = parse_position(task_spec.get("position"))
            task_level_dim = task_spec.get("dimension")
            for seed_index, seed_entry in enumerate(seeds):
                if not isinstance(seed_entry, dict):
                    eprint(f"[warn] skip invalid seed entry: {config_path}:{task_name}:seeds[{seed_index}]")
                    continue
                if "seed" not in seed_entry:
                    eprint(f"[warn] skip seed entry without seed: {config_path}:{task_name}:seeds[{seed_index}]")
                    continue
                try:
                    seed = parse_int(seed_entry["seed"], context=f"{task_name}.seeds[{seed_index}].seed")
                except RuntimeError as e:
                    eprint(f"[warn] {e}")
                    continue

                position = parse_position(seed_entry.get("position"))
                if position is None:
                    position = task_level_pos
                if position is None and not allow_missing_position:
                    eprint(
                        f"[warn] skip seed entry without position (use --allow-missing-position to include): "
                        f"{config_path}:{task_name}:seeds[{seed_index}]"
                    )
                    continue

                dimension_source = seed_entry.get("dimension", task_level_dim)
                if dimension_source is None:
                    dimension_source = label_dim
                dimension = normalize_dimension(dimension_source, default_dimension)
                pregen_radius = extract_pregen_radius(task_spec, seed_entry, default_pregen_radius_chunks)
                yield TaskSeedPoint(
                    task_name=task_name,
                    family=family_from_task_name(task_name),
                    config_path=str(config_path),
                    seed_index=seed_index,
                    seed=seed,
                    seed_str=str(seed),
                    dimension=dimension,
                    position=position,
                    pregen_radius_chunks=pregen_radius,
                    labels=labels,
                )


def block_to_chunk(x: int, z: int) -> Tuple[int, int]:
    return (x // 16, z // 16)


def chunk_to_region(cx: int, cz: int) -> Tuple[int, int]:
    return (cx // 32, cz // 32)


def build_group_coverage(points: List[TaskSeedPoint]) -> Dict[str, Any]:
    coverage_points: List[Dict[str, Any]] = []
    unique_chunks: set[Tuple[int, int]] = set()
    unique_regions: set[Tuple[int, int]] = set()

    for p in points:
        if p.position is None:
            continue
        x, y, z = p.position
        cx, cz = block_to_chunk(x, z)
        r = max(0, int(p.pregen_radius_chunks))
        min_cx = cx - r
        max_cx = cx + r
        min_cz = cz - r
        max_cz = cz + r
        for ix in range(min_cx, max_cx + 1):
            for iz in range(min_cz, max_cz + 1):
                unique_chunks.add((ix, iz))
                unique_regions.add(chunk_to_region(ix, iz))
        coverage_points.append(
            {
                "task_name": p.task_name,
                "seed_index": p.seed_index,
                "position": [x, y, z],
                "center_chunk": [cx, cz],
                "pregen_radius_chunks": r,
                "chunk_box": {
                    "min_chunk_x": min_cx,
                    "max_chunk_x": max_cx,
                    "min_chunk_z": min_cz,
                    "max_chunk_z": max_cz,
                },
            }
        )

    region_list = [
        {"region_x": rx, "region_z": rz, "file": f"r.{rx}.{rz}.mca"}
        for (rx, rz) in sorted(unique_regions)
    ]

    chunk_bounds: Optional[Dict[str, int]] = None
    if unique_chunks:
        xs = [c[0] for c in unique_chunks]
        zs = [c[1] for c in unique_chunks]
        chunk_bounds = {
            "min_chunk_x": min(xs),
            "max_chunk_x": max(xs),
            "min_chunk_z": min(zs),
            "max_chunk_z": max(zs),
        }

    return {
        "points": coverage_points,
        "unique_chunk_count": len(unique_chunks),
        "unique_region_count": len(unique_regions),
        "regions": region_list,
        "chunk_bounds": chunk_bounds,
    }


def build_snapshot_group_plan(
    task_seed_points: Sequence[TaskSeedPoint],
    *,
    raw_version: str,
    upgraded_version: str,
) -> Dict[str, Any]:
    grouped: Dict[GroupKey, List[TaskSeedPoint]] = defaultdict(list)
    for p in task_seed_points:
        grouped[
            GroupKey(
                seed=p.seed,
                seed_str=p.seed_str,
                raw_version=raw_version,
                upgraded_version=upgraded_version,
                dimension=p.dimension,
            )
        ].append(p)

    groups_output: List[Dict[str, Any]] = []
    for key in sorted(grouped.keys(), key=lambda k: (k.dimension, k.seed_str)):
        points = grouped[key]
        points_sorted = sorted(points, key=lambda p: (p.task_name, p.seed_index, p.config_path))
        coverage = build_group_coverage(points_sorted)
        families = sorted({p.family for p in points_sorted})
        tasks = []
        for p in points_sorted:
            tasks.append(
                {
                    "task_name": p.task_name,
                    "family": p.family,
                    "config_path": p.config_path,
                    "seed_index": p.seed_index,
                    "seed": p.seed,
                    "seed_str": p.seed_str,
                    "dimension": p.dimension,
                    "position": list(p.position) if p.position is not None else None,
                    "pregen_radius_chunks": p.pregen_radius_chunks,
                    "labels": p.labels,
                }
            )

        groups_output.append(
            {
                "group_id": key.stable_id(),
                "group_key": asdict(key),
                "stats": {
                    "task_seed_entry_count": len(points_sorted),
                    "task_name_count": len({p.task_name for p in points_sorted}),
                    "family_count": len(families),
                    "families": families,
                    "task_entries_with_position": sum(1 for p in points_sorted if p.position is not None),
                    "task_entries_without_position": sum(1 for p in points_sorted if p.position is None),
                    "unique_chunk_count": coverage["unique_chunk_count"],
                    "unique_region_count": coverage["unique_region_count"],
                },
                "coverage": coverage,
                "tasks": tasks,
            }
        )

    return {
        "format_version": PLAN_FORMAT_VERSION,
        "generated_at_utc": utc_ts(),
        "groups": groups_output,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Plan shared seed snapshots by grouping task configs on seed/version/dimension."
    )
    p.add_argument(
        "--task-config",
        action="append",
        required=True,
        help="Task config JSON path (can be repeated). Format: mapping task_name -> task spec.",
    )
    p.add_argument(
        "--task",
        action="append",
        default=[],
        help="Limit to specific task name(s). Can be repeated; default is all tasks in configs.",
    )
    p.add_argument("--raw-version", default="1.16.5")
    p.add_argument("--upgraded-version", default="1.21.1")
    p.add_argument("--default-dimension", default="overworld")
    p.add_argument("--default-pregen-radius-chunks", type=int, default=3)
    p.add_argument(
        "--allow-missing-position",
        action="store_true",
        help="Include task seeds even if position is missing (coverage will not include those entries).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write plan JSON to file. Default prints to stdout.",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON (indent=2). Default emits compact JSON.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    config_paths = [Path(x).resolve() for x in args.task_config]
    selected_tasks = set(args.task) if args.task else None

    task_seed_points = list(
        iter_task_seed_points(
            config_paths=config_paths,
            selected_tasks=selected_tasks,
            default_dimension=args.default_dimension,
            default_pregen_radius_chunks=args.default_pregen_radius_chunks,
            allow_missing_position=bool(args.allow_missing_position),
        )
    )
    plan = build_snapshot_group_plan(
        task_seed_points=task_seed_points,
        raw_version=args.raw_version,
        upgraded_version=args.upgraded_version,
    )
    payload = json.dumps(plan, ensure_ascii=False, indent=2 if args.pretty else None)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + ("\n" if not payload.endswith("\n") else ""), encoding="utf-8")
        eprint(f"[info] wrote snapshot group plan: {args.output}")
        eprint(f"[info] groups={len(plan['groups'])} task_seed_entries={len(task_seed_points)}")
    else:
        print(payload)
        eprint(f"[info] groups={len(plan['groups'])} task_seed_entries={len(task_seed_points)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
