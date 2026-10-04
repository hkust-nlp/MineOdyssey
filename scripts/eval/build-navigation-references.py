#!/usr/bin/env python3
"""Recompute static reference summaries on the original 1.21.11 worlds."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.references import PLANNER_VERSION, plan_task_reference  # noqa: E402
from eval.navigation.schema import (  # noqa: E402
    MAPS_ROOT,
    atomic_write_json,
    load_benchmark,
    load_map,
    load_references,
    load_setting,
    load_tasks,
    resolved_task,
    task_digest,
)
from eval.navigation.snapshots import verify_snapshot  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", dest="map_ids", action="append")
    parser.add_argument("--task", dest="task_ids", action="append")
    parser.add_argument("--max-nodes", type=int, default=750_000)
    parser.add_argument(
        "--margins",
        default="24,48,96,192,384",
        help="Comma-separated expanding horizontal search margins.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Write map references.json files here instead of tracked map packages.",
    )
    return parser.parse_args()


def _endpoint(position: dict[str, Any], radius_3d: float, radius_y: float) -> dict[str, Any]:
    return {
        "position": [position["x"], position["y"], position["z"]],
        "radius_3d": radius_3d,
        "radius_y": radius_y,
    }


def main() -> int:
    args = parse_args()
    benchmark = load_benchmark("finalpool-navigation-v1")
    setting = load_setting(benchmark["setting_id"])
    map_ids = args.map_ids or benchmark["maps"]
    margins = tuple(int(value) for value in args.margins.split(",") if value.strip())
    if not margins or any(value <= 0 for value in margins):
        raise SystemExit("--margins must contain positive integers")
    selected_task_ids = set(args.task_ids or [])
    seen_task_ids: set[str] = set()
    summaries: list[dict[str, Any]] = []

    for map_id in map_ids:
        map_payload = load_map(map_id)
        snapshot = verify_snapshot(map_payload)
        world_dir = Path(snapshot["world_dir"])
        fingerprint = snapshot["fingerprint"]["value"]
        map_tasks = load_tasks(map_id)
        map_task_ids = {task["id"] for task in map_tasks}
        selected_map_task_ids = map_task_ids & selected_task_ids
        rebuilding_entire_map = not selected_task_ids or (
            selected_map_task_ids == map_task_ids
        )
        rows: dict[str, dict[str, Any]] = (
            {} if rebuilding_entire_map else dict(load_references(map_id))
        )

        route_cache = world_dir.parent / "references"
        route_cache.mkdir(parents=True, exist_ok=True)
        for task in map_tasks:
            task_id = task["id"]
            if selected_task_ids and task_id not in selected_task_ids:
                continue
            seen_task_ids.add(task_id)
            resolved = resolved_task(map_id, task)
            start = resolved["start"]["position"]
            endpoints = [
                _endpoint(
                    waypoint["position"],
                    float(setting["arrival"]["radius_3d"]),
                    float(setting["arrival"]["radius_y"]),
                )
                for waypoint in [
                    *resolved["required_waypoints"],
                    resolved["target"],
                ]
            ]
            result = plan_task_reference(
                world_dir,
                [start["x"], start["y"], start["z"]],
                endpoints,
                max_nodes=args.max_nodes,
                margins=margins,
            )
            local_result = {
                "schema_version": 1,
                "map_id": map_id,
                "map_fingerprint": fingerprint,
                "task_id": task_id,
                "task_digest": task_digest(map_id, task),
                "checkpoint_count": len(resolved["required_waypoints"]),
                **result,
            }
            atomic_write_json(route_cache / f"{task_id}.json", local_result)
            row = {
                "task_id": task_id,
                "task_digest": task_digest(map_id, task),
                "status": result["status"],
                "expanded_nodes": result.get("expanded_nodes", 0),
                "checkpoint_count": len(resolved["required_waypoints"]),
            }
            for key in (
                "length_blocks",
                "route_digest",
                "snapped_start",
                "goal_cell",
                "reason",
                "failed_segment",
            ):
                if key in result:
                    row[key] = result[key]
            rows[task_id] = row
            print(
                f"{task_id}: {row['status']}"
                + (
                    f" ({row['length_blocks']:.3f} blocks)"
                    if "length_blocks" in row
                    else ""
                ),
                flush=True,
            )

        ordered_tasks = map_tasks
        output = {
            "schema_version": 1,
            "map_id": map_id,
            "planner_version": PLANNER_VERSION,
            "map_fingerprint": fingerprint,
            "references": [rows[task["id"]] for task in ordered_tasks],
        }
        destination = (
            args.output_dir / map_id / "references.json"
            if args.output_dir
            else MAPS_ROOT / map_id / "references.json"
        )
        atomic_write_json(destination, output)
        summaries.append(
            {
                "map_id": map_id,
                "output": str(destination),
                "status_counts": {
                    status: sum(row["status"] == status for row in rows.values())
                    for status in ("reachable", "unreachable", "planner_limit", "pending")
                },
            }
        )

    missing = selected_task_ids - seen_task_ids
    if missing:
        raise SystemExit(f"unknown or unselected task IDs: {', '.join(sorted(missing))}")
    print(json.dumps({"maps": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
