"""Evaluator-owned metrics for navigation trajectories."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


Position = Mapping[str, float | int]


def _float_position(value: Position) -> dict[str, float]:
    return {axis: float(value[axis]) for axis in ("x", "y", "z")}


def distance_xz(left: Position, right: Position) -> float:
    return math.hypot(
        float(left["x"]) - float(right["x"]),
        float(left["z"]) - float(right["z"]),
    )


def distance_3d(left: Position, right: Position) -> float:
    return math.sqrt(
        (float(left["x"]) - float(right["x"])) ** 2
        + (float(left["y"]) - float(right["y"])) ** 2
        + (float(left["z"]) - float(right["z"])) ** 2
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


@dataclass(frozen=True)
class Checkpoint:
    waypoint_id: str
    position: dict[str, float]
    radius_3d: float
    radius_y: float


class CheckpointTracker:
    """Track required waypoints in task order."""

    def __init__(self, checkpoints: Iterable[Mapping[str, Any]]):
        self.checkpoints = [
            Checkpoint(
                waypoint_id=str(row["waypoint_id"]),
                position=_float_position(row["position"]),
                radius_3d=float(row.get("radius_3d", 3.5)),
                radius_y=float(row.get("radius_y", 1.5)),
            )
            for row in checkpoints
        ]
        self.next_index = 0
        self.reached: list[dict[str, Any]] = []

    def observe(self, position: Position, elapsed_sec: float) -> dict[str, Any] | None:
        if self.next_index >= len(self.checkpoints):
            return None
        checkpoint = self.checkpoints[self.next_index]
        if (
            distance_3d(position, checkpoint.position) < checkpoint.radius_3d
            and abs(float(position["y"]) - checkpoint.position["y"]) <= checkpoint.radius_y
        ):
            reached = {
                "waypoint_id": checkpoint.waypoint_id,
                "elapsed_sec": round(elapsed_sec, 6),
                "position": _float_position(position),
            }
            self.reached.append(reached)
            self.next_index += 1
            return reached
        return None

    def summary(self) -> dict[str, Any]:
        total = len(self.checkpoints)
        reached = len(self.reached)
        return {
            "ordered_total": total,
            "ordered_reached": reached,
            "coverage": 1.0 if total == 0 else round(reached / total, 6),
            "complete": reached == total,
            "reached": list(self.reached),
            "next_waypoint_id": (
                self.checkpoints[self.next_index].waypoint_id
                if self.next_index < total
                else None
            ),
        }


class NavigationMetricAccumulator:
    """Accumulate objective metrics from the external one-Hz state stream."""

    def __init__(
        self,
        *,
        start: Position,
        target: Position,
        required_waypoints: Iterable[Mapping[str, Any]] = (),
        reference_length_blocks: float | None = None,
    ):
        self.start = _float_position(start)
        self.target = _float_position(target)
        self.reference_length_blocks = (
            None if reference_length_blocks is None else float(reference_length_blocks)
        )
        self.checkpoints = CheckpointTracker(required_waypoints)
        self.samples = 0
        self.previous_position: dict[str, float] | None = None
        self.final_position: dict[str, float] | None = None
        self.initial_distance_xz: float | None = None
        self.initial_distance_3d: float | None = None
        self.final_distance_xz: float | None = None
        self.final_distance_3d: float | None = None
        self.min_distance_xz: float | None = None
        self.min_distance_xz_elapsed_sec: float | None = None
        self.min_distance_3d: float | None = None
        self.min_distance_3d_elapsed_sec: float | None = None
        self.path_length_xz = 0.0
        self.path_length_3d = 0.0
        self.last_elapsed_sec = 0.0

    def observe(self, position: Position, elapsed_sec: float) -> dict[str, Any]:
        current = _float_position(position)
        elapsed = max(0.0, float(elapsed_sec))
        current_xz = distance_xz(current, self.target)
        current_3d = distance_3d(current, self.target)
        if self.samples == 0:
            self.initial_distance_xz = current_xz
            self.initial_distance_3d = current_3d
        elif self.previous_position is not None:
            self.path_length_xz += distance_xz(self.previous_position, current)
            self.path_length_3d += distance_3d(self.previous_position, current)
        if self.min_distance_xz is None or current_xz < self.min_distance_xz:
            self.min_distance_xz = current_xz
            self.min_distance_xz_elapsed_sec = elapsed
        if self.min_distance_3d is None or current_3d < self.min_distance_3d:
            self.min_distance_3d = current_3d
            self.min_distance_3d_elapsed_sec = elapsed
        self.previous_position = current
        self.final_position = current
        self.final_distance_xz = current_xz
        self.final_distance_3d = current_3d
        self.last_elapsed_sec = elapsed
        self.samples += 1
        recorded_waypoint = self.checkpoints.observe(current, elapsed)
        return {
            "distance_xz": current_xz,
            "distance_3d": current_3d,
            "vertical_distance": abs(current["y"] - self.target["y"]),
            "recorded_waypoint": recorded_waypoint,
        }

    def finalize(
        self,
        *,
        completed: bool,
        duration_sec: float | None = None,
    ) -> dict[str, Any]:
        duration = self.last_elapsed_sec if duration_sec is None else max(0.0, float(duration_sec))
        progress_xz = (
            None
            if self.initial_distance_xz is None or self.final_distance_xz is None
            else self.initial_distance_xz - self.final_distance_xz
        )
        best_progress_xz = (
            None
            if self.initial_distance_xz is None or self.min_distance_xz is None
            else self.initial_distance_xz - self.min_distance_xz
        )
        static_spl: float | None
        if (
            not completed
            or self.reference_length_blocks is None
            or self.reference_length_blocks <= 0
        ):
            static_spl = 0.0 if self.reference_length_blocks is not None else None
        else:
            static_spl = self.reference_length_blocks / max(
                self.reference_length_blocks,
                self.path_length_3d,
            )
        net_displacement_xz = (
            None
            if self.final_position is None
            else distance_xz(self.start, self.final_position)
        )
        return {
            "schema_version": 1,
            "sample_count": self.samples,
            "duration_sec": _rounded(duration),
            "success": bool(completed),
            "start_position": self.start,
            "target_position": self.target,
            "final_position": self.final_position,
            "initial_distance_xz": _rounded(self.initial_distance_xz),
            "initial_distance_3d": _rounded(self.initial_distance_3d),
            "final_distance_xz": _rounded(self.final_distance_xz),
            "final_distance_3d": _rounded(self.final_distance_3d),
            "min_distance_xz": _rounded(self.min_distance_xz),
            "min_distance_xz_elapsed_sec": _rounded(self.min_distance_xz_elapsed_sec),
            "min_distance_3d": _rounded(self.min_distance_3d),
            "min_distance_3d_elapsed_sec": _rounded(self.min_distance_3d_elapsed_sec),
            "progress_xz": _rounded(progress_xz),
            "progress_ratio_xz": _rounded(
                _safe_ratio(progress_xz, self.initial_distance_xz)
            ),
            "best_progress_xz": _rounded(best_progress_xz),
            "best_progress_ratio_xz": _rounded(
                _safe_ratio(best_progress_xz, self.initial_distance_xz)
            ),
            "path_length_xz": _rounded(self.path_length_xz),
            "path_length_3d": _rounded(self.path_length_3d),
            "net_displacement_xz": _rounded(net_displacement_xz),
            "straightness_xz": _rounded(
                _safe_ratio(net_displacement_xz, self.path_length_xz)
            ),
            "average_speed_xz": _rounded(
                _safe_ratio(self.path_length_xz, duration)
            ),
            "reference_length_blocks": _rounded(self.reference_length_blocks),
            "static_spl": _rounded(static_spl),
            "checkpoints": self.checkpoints.summary(),
        }
