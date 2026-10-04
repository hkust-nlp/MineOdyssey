#!/usr/bin/env python3
"""Send a guide-only Baritone route to the Ground Navigation evaluation mod."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import time
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def coordinates(record: dict[str, Any]) -> dict[str, int]:
    raw = record.get("position") or record.get("mc") or record
    if not isinstance(raw, dict) or not all(key in raw for key in ("x", "y", "z")):
        raise ValueError(f"waypoint has no x/y/z coordinates: {record!r}")
    return {key: int(raw[key]) for key in ("x", "y", "z")}


def task_points(
    tasks_path: Path,
    waypoints_path: Path,
    task_id: str,
    *,
    expected_map_id: str | None = None,
) -> list[dict[str, Any]]:
    tasks = read_json(tasks_path).get("tasks")
    waypoint_rows = read_json(waypoints_path).get("waypoints")
    if not isinstance(tasks, list) or not isinstance(waypoint_rows, list):
        raise ValueError("tasks.json and waypoints.json use an unsupported schema")

    task = next(
        (
            row
            for row in tasks
            if (row.get("task_id") or row.get("id")) == task_id
        ),
        None,
    )
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    if expected_map_id is not None and task.get("map_id") != expected_map_id:
        raise ValueError(
            f"task {task_id} belongs to map {task.get('map_id')!r}, "
            f"not {expected_map_id!r}"
        )
    by_id = {str(row["id"]): row for row in waypoint_rows}

    if isinstance(task.get("waypoints"), list):
        task_waypoints = task["waypoints"]
        if len(task_waypoints) < 2:
            raise ValueError(f"task {task_id} must contain a start and target")
        ordered_ids = [
            str(row["id"] if isinstance(row, dict) else row)
            for row in task_waypoints[1:]
        ]
    else:
        ordered_ids = [
            str(row["waypoint_id"])
            for row in task.get("required_waypoints", [])
        ]
        ordered_ids.append(str(task["target"]["waypoint_id"]))

    result = []
    for waypoint_id in ordered_ids:
        if waypoint_id not in by_id:
            raise ValueError(f"task {task_id} references missing waypoint: {waypoint_id}")
        result.append({"id": waypoint_id, **coordinates(by_id[waypoint_id])})
    return result


def direct_points(values: list[str]) -> list[dict[str, Any]]:
    result = []
    for index, value in enumerate(values, 1):
        parts = value.replace(",", " ").split()
        if len(parts) != 3:
            raise ValueError(f"--point expects x,y,z, got: {value!r}")
        x, y, z = (int(part) for part in parts)
        result.append({"id": f"point-{index}", "x": x, "y": y, "z": z})
    return result


def run_points(run_path: Path) -> list[dict[str, Any]]:
    run = read_json(run_path)
    task = run.get("task")
    if not isinstance(task, dict):
        raise ValueError(f"{run_path} lacks a resolved task")
    rows = list(task.get("required_waypoints") or [])
    target = task.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"{run_path} lacks a resolved target")
    rows.append(target)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"{run_path} contains an invalid route point")
        result.append(
            {
                "id": str(row.get("waypoint_id") or f"point-{index}"),
                **coordinates(row),
            }
        )
    if not result:
        raise ValueError(f"{run_path} contains no guide targets")
    return result


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def wait_for_status(status_path: Path, request_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = read_json(status_path)
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if status.get("request_id") == request_id:
            if status.get("guide_only") is not True:
                raise RuntimeError("Ground Navigation acknowledged a non-guide-only route")
            if status.get("baritone_controls_locked") is not True:
                raise RuntimeError("Ground Navigation did not lock Baritone agent controls")
            if status.get("state") in {"error", "disabled"}:
                raise RuntimeError(f"Ground Navigation rejected the route: {status}")
            return status
        time.sleep(0.1)
    raise TimeoutError(f"Ground Navigation did not acknowledge {request_id} within {timeout:g}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=Path, required=True, help="Minecraft game directory")
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--waypoints", type=Path)
    parser.add_argument("--run-json", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--map-id")
    parser.add_argument("--point", action="append", default=[], help="Direct target x,y,z; repeat for multiple stops")
    parser.add_argument("--arrival-radius", type=float, default=1.5)
    parser.add_argument("--request-id")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=0.0)
    args = parser.parse_args()

    if args.clear:
        points: list[dict[str, Any]] = []
    elif args.run_json:
        points = run_points(args.run_json)
    elif args.task_id:
        if args.tasks is None or args.waypoints is None:
            parser.error("--task-id requires --tasks and --waypoints")
        points = task_points(
            args.tasks,
            args.waypoints,
            args.task_id,
            expected_map_id=args.map_id,
        )
    elif args.point:
        points = direct_points(args.point)
    else:
        parser.error("provide --run-json, --task-id, at least one --point, or --clear")

    request_id = args.request_id or f"groundnav-{time.time_ns()}"
    payload = {
        "id": request_id,
        "enabled": not args.clear,
        "guide_only": True,
        "arrival_radius": args.arrival_radius,
        "points": points,
    }
    control_directory = args.instance.expanduser().resolve() / "ground-navigation"
    request_path = control_directory / "request.json"
    write_atomic(request_path, payload)

    output: dict[str, Any] = {
        "success": True,
        "request": payload,
        "request_path": str(request_path),
    }
    if args.wait_seconds > 0:
        output["status"] = wait_for_status(
            control_directory / "status.json",
            request_id,
            args.wait_seconds,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
