from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


@dataclass(frozen=True)
class StartReadinessPolicy:
    expected_x: float
    expected_y: float
    expected_z: float
    horizontal_tolerance: float = 1.5
    vertical_tolerance: float = 2.5
    stable_epsilon: float = 0.05
    consecutive_samples: int = 3


class StartReadinessTracker:
    """Require a live client to settle on the ground near the task start."""

    def __init__(self, policy: StartReadinessPolicy) -> None:
        if policy.consecutive_samples < 1:
            raise ValueError("consecutive_samples must be positive")
        self.policy = policy
        self._previous_position: dict[str, float] | None = None
        self._consecutive = 0

    def observe(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("success") is not True:
            raise ValueError(str(response.get("error") or "AgentBridge state failed"))
        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError("AgentBridge state is missing data")
        raw_position = data.get("position")
        if not isinstance(raw_position, dict):
            raise ValueError("AgentBridge state is missing position")
        position = {
            axis: _number(raw_position.get(axis), f"position.{axis}")
            for axis in ("x", "y", "z")
        }
        health = _number(data.get("health"), "health")
        on_ground = data.get("on_ground") is True
        near_start = (
            abs(position["x"] - self.policy.expected_x)
            <= self.policy.horizontal_tolerance
            and abs(position["z"] - self.policy.expected_z)
            <= self.policy.horizontal_tolerance
            and abs(position["y"] - self.policy.expected_y)
            <= self.policy.vertical_tolerance
        )
        stable = self._previous_position is not None and all(
            abs(position[axis] - self._previous_position[axis])
            <= self.policy.stable_epsilon
            for axis in ("x", "y", "z")
        )
        eligible = health > 0 and on_ground and near_start and stable
        self._consecutive = self._consecutive + 1 if eligible else 0
        self._previous_position = position
        return {
            "position": position,
            "health": health,
            "on_ground": on_ground,
            "near_start": near_start,
            "stable": stable,
            "eligible": eligible,
            "consecutive_ready_samples": self._consecutive,
            "ready": self._consecutive >= self.policy.consecutive_samples,
        }
