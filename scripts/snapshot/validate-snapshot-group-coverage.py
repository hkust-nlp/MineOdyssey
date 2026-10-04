#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def load_json_obj(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return data


def version_slug(version: str) -> str:
    return version.strip().replace(".", "_")


def normalize_dimension(raw: Any) -> str:
    s = str(raw or "overworld").strip().lower()
    aliases = {
        "0": "overworld",
        "overworld": "overworld",
        "minecraft:overworld": "overworld",
        "-1": "the_nether",
        "nether": "the_nether",
        "the_nether": "the_nether",
        "minecraft:the_nether": "the_nether",
        "1": "the_end",
        "end": "the_end",
        "the_end": "the_end",
        "minecraft:the_end": "the_end",
    }
    return aliases.get(s, s or "overworld")


def region_dir_for_dimension(world_dir: Path, dimension: str) -> Path:
    dim = normalize_dimension(dimension)
    if dim == "overworld":
        return world_dir / "region"
    if dim == "the_nether":
        return world_dir / "DIM-1" / "region"
    if dim == "the_end":
        return world_dir / "DIM1" / "region"
    return world_dir / "region"


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    group_id: str
    message: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Validate built snapshot worlds cover planner-declared region files for each snapshot group."
    )
    p.add_argument("--plan", type=Path, required=True, help="snapshot-group plan JSON")
    p.add_argument(
        "--snapshot-root",
        type=Path,
        default=Path("eval/snapshots"),
        help="Root used by scripts/snapshot/build-seed-snapshot.py / scripts/snapshot/build-snapshot-groups.py",
    )
    p.add_argument("--group-id", action="append", default=[], help="Validate only selected group_id(s)")
    p.add_argument(
        "--world-kind",
        choices=["raw", "upgraded", "both"],
        default="upgraded",
        help="Which snapshot world(s) to validate",
    )
    p.add_argument(
        "--allow-missing-snapshots",
        action="store_true",
        help="Warn instead of fail when snapshot world path is missing",
    )
    p.add_argument(
        "--check-meta",
        action="store_true",
        help="Also validate snapshot_meta.json target_dimension and target_positions length (if present).",
    )
    return p.parse_args()


def iter_selected_groups(plan: Dict[str, Any], selected_ids: Optional[set[str]]) -> List[Dict[str, Any]]:
    raw_groups = plan.get("groups")
    if not isinstance(raw_groups, list):
        raise RuntimeError("Invalid plan: missing groups[]")
    out: List[Dict[str, Any]] = []
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("group_id", ""))
        if selected_ids and gid not in selected_ids:
            continue
        out.append(g)
    if selected_ids:
        missing = sorted(selected_ids - {str(g.get("group_id", "")) for g in out})
        if missing:
            raise RuntimeError(f"group_id not found in plan: {missing}")
    return out


def _validate_snapshot_meta(
    meta_path: Path,
    *,
    expected_dimension: str,
    expected_points_count: int,
    group_id: str,
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if not meta_path.exists():
        issues.append(ValidationIssue("warn", group_id, f"snapshot meta missing: {meta_path}"))
        return issues
    try:
        meta = load_json_obj(meta_path)
    except Exception as e:  # noqa: BLE001
        issues.append(ValidationIssue("error", group_id, f"failed to read snapshot meta {meta_path}: {e}"))
        return issues
    got_dim = normalize_dimension(meta.get("target_dimension"))
    if got_dim != normalize_dimension(expected_dimension):
        issues.append(
            ValidationIssue(
                "error",
                group_id,
                f"snapshot meta target_dimension mismatch: got={got_dim} expected={expected_dimension}",
            )
        )
    raw_positions = meta.get("target_positions")
    if isinstance(raw_positions, list) and len(raw_positions) < expected_points_count:
        issues.append(
            ValidationIssue(
                "error",
                group_id,
                f"snapshot meta target_positions too few: got={len(raw_positions)} expected>={expected_points_count}",
            )
        )
    return issues


def validate_group_world(
    *,
    group: Dict[str, Any],
    snapshot_root: Path,
    world_kind: str,
    allow_missing_snapshots: bool,
    check_meta: bool,
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    gid = str(group.get("group_id", "group"))
    group_key = group.get("group_key")
    if not isinstance(group_key, dict):
        return [ValidationIssue("error", gid, "missing group_key")]

    seed = group_key.get("seed")
    raw_version = str(group_key.get("raw_version", "1.16.5"))
    upgraded_version = str(group_key.get("upgraded_version", "1.21.1"))
    dimension = normalize_dimension(group_key.get("dimension"))
    if seed is None:
        return [ValidationIssue("error", gid, "missing group_key.seed")]

    coverage = group.get("coverage")
    if not isinstance(coverage, dict):
        return [ValidationIssue("error", gid, "missing coverage")]
    regions = coverage.get("regions")
    if not isinstance(regions, list):
        return [ValidationIssue("error", gid, "missing coverage.regions[]")]
    points = coverage.get("points")
    expected_points_count = 0
    if isinstance(points, list):
        seen_positions: set[tuple[int, int, int]] = set()
        for item in points:
            if not isinstance(item, dict):
                continue
            pos = item.get("position")
            if not isinstance(pos, list) or len(pos) < 3:
                continue
            try:
                xyz = (int(pos[0]), int(pos[1]), int(pos[2]))
            except Exception:
                continue
            seen_positions.add(xyz)
        expected_points_count = len(seen_positions)

    targets: List[tuple[str, Path]] = []
    if world_kind in {"raw", "both"}:
        targets.append(
            (
                "raw",
                snapshot_root / f"seed_raw_{version_slug(raw_version)}" / gid / str(seed) / "world",
            )
        )
    if world_kind in {"upgraded", "both"}:
        targets.append(
            (
                "upgraded",
                snapshot_root / f"seed_upgraded_{version_slug(upgraded_version)}" / gid / str(seed) / "world",
            )
        )

    for kind_name, world_dir in targets:
        if not world_dir.exists():
            level = "warn" if allow_missing_snapshots else "error"
            issues.append(
                ValidationIssue(level, gid, f"{kind_name} snapshot world missing: {world_dir}")
            )
            continue

        rdir = region_dir_for_dimension(world_dir, dimension)
        if not rdir.exists():
            issues.append(
                ValidationIssue("error", gid, f"{kind_name} region dir missing for {dimension}: {rdir}")
            )
            continue

        for item in regions:
            if not isinstance(item, dict):
                continue
            fname = item.get("file")
            if not isinstance(fname, str) or not fname:
                continue
            if not (rdir / fname).exists():
                issues.append(
                    ValidationIssue(
                        "error",
                        gid,
                        f"{kind_name} missing region file for {dimension}: {rdir / fname}",
                    )
                )

        if check_meta:
            meta_path = world_dir.parent / "snapshot_meta.json"
            issues.extend(
                _validate_snapshot_meta(
                    meta_path,
                    expected_dimension=dimension,
                    expected_points_count=expected_points_count,
                    group_id=gid,
                )
            )

    return issues


def main() -> int:
    args = parse_args()
    plan_path = args.plan.resolve()
    plan = load_json_obj(plan_path)
    snapshot_root = args.snapshot_root.resolve()
    selected = set(args.group_id) if args.group_id else None
    groups = iter_selected_groups(plan, selected)

    print(f"[info] plan={plan_path}")
    print(f"[info] snapshot_root={snapshot_root}")
    print(f"[info] groups={len(groups)} world_kind={args.world_kind}")

    issues: List[ValidationIssue] = []
    for group in groups:
        issues.extend(
            validate_group_world(
                group=group,
                snapshot_root=snapshot_root,
                world_kind=args.world_kind,
                allow_missing_snapshots=args.allow_missing_snapshots,
                check_meta=args.check_meta,
            )
        )

    warn_count = sum(1 for i in issues if i.level == "warn")
    err_count = sum(1 for i in issues if i.level == "error")

    for i in issues:
        print(f"[{i.level.upper()}] {i.group_id}: {i.message}")

    if err_count == 0:
        print(f"[done] ok groups={len(groups)} warnings={warn_count}")
        return 0
    print(f"[done] failed groups={len(groups)} errors={err_count} warnings={warn_count}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
