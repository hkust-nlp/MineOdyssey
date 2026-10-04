"""External arrival and bounded ``claim_done`` completion protocol."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .metrics import NavigationMetricAccumulator


class CompletionError(RuntimeError):
    """Raised when the evaluator-side completion protocol is inconsistent."""


@dataclass(frozen=True)
class ArrivalPolicy:
    radius_3d: float
    radius_y: float
    sample_interval_sec: float

    @classmethod
    def from_setting(cls, setting: Mapping[str, Any]) -> "ArrivalPolicy":
        arrival = setting["arrival"]
        return cls(
            radius_3d=float(arrival["radius_3d"]),
            radius_y=float(arrival["radius_y"]),
            sample_interval_sec=float(arrival["sample_interval_sec"]),
        )


@dataclass(frozen=True)
class MinimumHeightBoundaryPolicy:
    """Fail a run after continuously observing a player at or below a map floor."""

    at_or_below_y: float
    duration_sec: float

    @classmethod
    def from_map(cls, map_payload: Mapping[str, Any]) -> "MinimumHeightBoundaryPolicy | None":
        payload = map_payload.get("out_of_bounds")
        if payload is None:
            return None
        return cls(
            at_or_below_y=float(payload["at_or_below_y"]),
            duration_sec=float(payload["duration_sec"]),
        )


class MinimumHeightBoundaryMonitor:
    """Track uninterrupted time at or below a configured map-specific Y threshold."""

    def __init__(self, policy: MinimumHeightBoundaryPolicy):
        if policy.duration_sec <= 0:
            raise CompletionError("minimum-height boundary duration must be positive")
        self.policy = policy
        self.below_since_elapsed_sec: float | None = None
        self.triggered = False

    def observe(self, *, y: float, elapsed_sec: float) -> dict[str, Any]:
        at_or_below = float(y) <= self.policy.at_or_below_y
        if at_or_below:
            if self.below_since_elapsed_sec is None:
                self.below_since_elapsed_sec = float(elapsed_sec)
        else:
            self.below_since_elapsed_sec = None

        continuous_sec = (
            0.0
            if self.below_since_elapsed_sec is None
            else max(0.0, float(elapsed_sec) - self.below_since_elapsed_sec)
        )
        newly_triggered = (
            not self.triggered
            and at_or_below
            and continuous_sec >= self.policy.duration_sec
        )
        if newly_triggered:
            self.triggered = True
        return {
            "at_or_below_y": self.policy.at_or_below_y,
            "duration_sec": self.policy.duration_sec,
            "currently_at_or_below": at_or_below,
            "continuous_sec": round(continuous_sec, 6),
            "triggered": self.triggered,
            "newly_triggered": newly_triggered,
        }


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class CompletionProtocol:
    """Own arrival state and arbitrate every model completion claim."""

    def __init__(
        self,
        *,
        target: Mapping[str, float | int],
        policy: ArrivalPolicy,
        claim_attempt_limit: int,
        feedback_limit: int,
        metrics: NavigationMetricAccumulator,
        required_waypoints: list[Mapping[str, Any]] | None = None,
        target_name: str = "final destination",
    ):
        if claim_attempt_limit != feedback_limit + 1 or claim_attempt_limit <= 0:
            raise CompletionError("claim_attempt_limit must equal feedback_limit + 1")
        self.target = {axis: float(target[axis]) for axis in ("x", "y", "z")}
        self.policy = policy
        self.claim_attempt_limit = claim_attempt_limit
        self.feedback_limit = feedback_limit
        self.metrics = metrics
        self.required_waypoints = list(required_waypoints or [])
        self.required_names = [
            str(row.get("name") or row["waypoint_id"])
            for row in self.required_waypoints
        ]
        self.target_name = target_name
        self.oracle_arrived = False
        self.oracle_arrived_at_elapsed_sec: float | None = None
        self.last_sample: dict[str, Any] | None = None
        self.claim_count = 0
        self.terminal = False
        self.success = False
        self.terminal_reason: str | None = None

    def observe(
        self,
        *,
        position: Mapping[str, float | int],
        elapsed_sec: float,
        health: float | None = None,
        block_position: Mapping[str, int] | None = None,
        sampled_at_monotonic: float | None = None,
    ) -> dict[str, Any]:
        metric_sample = self.metrics.observe(position, elapsed_sec)
        vertical = abs(float(position["y"]) - self.target["y"])
        inside = (
            metric_sample["distance_3d"] < self.policy.radius_3d
            and vertical <= self.policy.radius_y
        )
        events: list[dict[str, Any]] = []
        recorded_waypoint = metric_sample.get("recorded_waypoint")
        if isinstance(recorded_waypoint, Mapping):
            index = len(self.metrics.checkpoints.reached) - 1
            name = self.required_names[index]
            events.append(
                {
                    "kind": "required_waypoint_recorded",
                    "waypoint_id": recorded_waypoint["waypoint_id"],
                    "message": (
                        "[Navigation evaluator] Confirmed that required waypoint "
                        f'"{name}" has been visited and recorded.'
                    ),
                }
            )
        route_complete = self.metrics.checkpoints.summary()["complete"]
        if route_complete and inside and not self.oracle_arrived:
            self.oracle_arrived = True
            self.oracle_arrived_at_elapsed_sec = float(elapsed_sec)
            events.append(
                {
                    "kind": "final_destination_recorded",
                    "waypoint_id": "target",
                    "message": (
                        "[Navigation evaluator] Confirmed that final destination "
                        f'"{self.target_name}" has been reached and recorded. '
                        "Submit claim_done to complete the task."
                    ),
                }
            )
        sample = {
            "elapsed_sec": round(float(elapsed_sec), 6),
            "position": {axis: float(position[axis]) for axis in ("x", "y", "z")},
            "block_position": (
                {axis: int(block_position[axis]) for axis in ("x", "y", "z")}
                if block_position is not None
                else None
            ),
            "health": None if health is None else float(health),
            "distance_xz": round(float(metric_sample["distance_xz"]), 6),
            "distance_3d": round(float(metric_sample["distance_3d"]), 6),
            "vertical_distance": round(vertical, 6),
            "inside_arrival_region": inside,
            "oracle_arrived": self.oracle_arrived,
            "evaluator_events": events,
        }
        self.last_sample = sample
        _ = sampled_at_monotonic
        if health is not None and health <= 0 and not self.terminal:
            self.terminal = True
            self.success = False
            self.terminal_reason = "death"
        return sample

    def evaluate_claim(
        self,
        *,
        claim_id: str,
        now_monotonic: float | None = None,
    ) -> dict[str, Any]:
        if not claim_id:
            raise CompletionError("claim_id must not be empty")
        _ = now_monotonic
        if self.terminal:
            return {
                "schema_version": 1,
                "claim_id": claim_id,
                "accepted": False,
                "terminal": True,
                "success": self.success,
                "reason": "already_terminal",
                "terminal_reason": self.terminal_reason,
                "attempt": self.claim_count,
            }
        self.claim_count += 1
        if self.oracle_arrived:
            self.terminal = True
            self.success = True
            self.terminal_reason = "claim_done_arrived"
            return {
                "schema_version": 1,
                "claim_id": claim_id,
                "accepted": True,
                "terminal": True,
                "success": True,
                "reason": "arrived",
                "attempt": self.claim_count,
            }

        terminal_rejection = self.claim_count >= self.claim_attempt_limit
        if terminal_rejection:
            self.terminal = True
            self.success = False
            self.terminal_reason = "claim_attempts_exhausted"
        feedback: dict[str, Any] | None = None
        if self.claim_count <= self.feedback_limit:
            next_index = self.metrics.checkpoints.next_index
            remaining = self.required_names[next_index:]
            if not self.oracle_arrived:
                remaining.append(self.target_name)
            feedback = {
                "remaining_locations": remaining,
                "remaining_attempts": self.claim_attempt_limit - self.claim_count,
            }
        return {
            "schema_version": 1,
            "claim_id": claim_id,
            "accepted": False,
            "terminal": terminal_rejection,
            "success": False,
            "reason": (
                "claim_attempts_exhausted"
                if terminal_rejection
                else "not_arrived"
            ),
            "attempt": self.claim_count,
            "attempt_limit": self.claim_attempt_limit,
            "feedback": feedback,
        }

    def terminate(self, reason: str, *, success: bool = False) -> None:
        if self.terminal:
            return
        if not reason:
            raise CompletionError("terminal reason must not be empty")
        self.terminal = True
        self.success = bool(success)
        self.terminal_reason = reason

    def result(self, *, duration_sec: float | None = None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "terminal": self.terminal,
            "success": self.success,
            "terminal_reason": self.terminal_reason,
            "claim_count": self.claim_count,
            "oracle_arrived": self.oracle_arrived,
            "oracle_arrived_at_elapsed_sec": self.oracle_arrived_at_elapsed_sec,
            "last_sample": self.last_sample,
            "arrival": {
                "radius_3d": self.policy.radius_3d,
                "radius_y": self.policy.radius_y,
                "sample_interval_sec": self.policy.sample_interval_sec,
            },
            "metrics": self.metrics.finalize(
                completed=self.success,
                duration_sec=duration_sec,
            ),
        }


def parse_position_state(payload: Mapping[str, Any]) -> tuple[dict[str, float], float | None, dict[str, int] | None]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise CompletionError("AgentBridge state response lacks data")
    raw_position = data.get("position")
    if not isinstance(raw_position, Mapping):
        raise CompletionError("AgentBridge state response lacks data.position")
    position: dict[str, float] = {}
    for axis in ("x", "y", "z"):
        value = raw_position.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CompletionError(f"invalid AgentBridge position axis {axis}")
        number = float(value)
        if not math.isfinite(number):
            raise CompletionError(f"non-finite AgentBridge position axis {axis}")
        position[axis] = number
    health_raw = data.get("health")
    health = (
        float(health_raw)
        if isinstance(health_raw, (int, float)) and not isinstance(health_raw, bool)
        else None
    )
    block_raw = data.get("block_position")
    block_position = None
    if isinstance(block_raw, Mapping) and all(
        isinstance(block_raw.get(axis), int) and not isinstance(block_raw.get(axis), bool)
        for axis in ("x", "y", "z")
    ):
        block_position = {axis: int(block_raw[axis]) for axis in ("x", "y", "z")}
    return position, health, block_position


def load_claim_request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompletionError(f"invalid claim request {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CompletionError(f"unsupported claim request: {path}")
    claim_id = payload.get("claim_id")
    if not isinstance(claim_id, str) or re.fullmatch(r"[0-9a-f]{32}", claim_id) is None:
        raise CompletionError(f"claim request has an unsafe claim_id: {path}")
    if path.name != f"{claim_id}.request.json":
        raise CompletionError(f"claim request filename/ID mismatch: {path}")
    if payload.get("action") != "claim_done":
        raise CompletionError(f"claim request has the wrong action: {path}")
    issued_at = payload.get("issued_at_unix_sec")
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, (int, float))
        or not math.isfinite(float(issued_at))
    ):
        raise CompletionError(f"claim request has an invalid timestamp: {path}")
    return payload
