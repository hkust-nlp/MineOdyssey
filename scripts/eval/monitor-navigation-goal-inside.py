#!/usr/bin/env python3
"""Externally sample navigation state and arbitrate evaluator-owned claims."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.completion import (  # noqa: E402
    ArrivalPolicy,
    CompletionProtocol,
    MinimumHeightBoundaryMonitor,
    MinimumHeightBoundaryPolicy,
    append_jsonl,
    atomic_write_json,
    load_claim_request,
    parse_position_state,
)
from eval.navigation.metrics import NavigationMetricAccumulator  # noqa: E402
from eval.navigation.schema import load_map, load_setting  # noqa: E402


STOP_REASON: str | None = None


def _request_stop(signum: int, _frame: object) -> None:
    global STOP_REASON
    STOP_REASON = f"signal_{signum}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--max-consecutive-state-errors", type=int, default=10)
    return parser.parse_args()


def _fetch_json(url: str, timeout_sec: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_sec) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"non-object response from {url}")
    return payload


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    run_dir = args.run_dir.resolve()
    run = json.loads((run_dir / "control" / "run.json").read_text(encoding="utf-8"))
    results = Path(run["results_dir"])
    setting = load_setting(run["setting_id"])
    map_payload = load_map(run["map_id"])
    boundary_policy = MinimumHeightBoundaryPolicy.from_map(map_payload)
    boundary_monitor = (
        MinimumHeightBoundaryMonitor(boundary_policy)
        if boundary_policy is not None
        else None
    )
    task = run["task"]
    target = task["target"]["position"]
    accumulator = NavigationMetricAccumulator(
        start=task["start"]["position"],
        target=target,
        required_waypoints=task["required_waypoints"],
        reference_length_blocks=run["reference_length_blocks"],
    )
    protocol = CompletionProtocol(
        target=target,
        policy=ArrivalPolicy.from_setting(setting),
        claim_attempt_limit=int(setting["completion"]["claim_attempt_limit"]),
        feedback_limit=int(setting["completion"]["claim_attempt_limit"]) - 1,
        metrics=accumulator,
        required_waypoints=task["required_waypoints"],
        target_name=str(task["target"]["name"]),
    )
    interval = float(setting["arrival"]["sample_interval_sec"])
    watchdog_timeout = float(setting["limits"]["watchdog_timeout_sec"])
    base_url = f"http://127.0.0.1:{run['ports']['agentbridge']}"
    request_dir = run_dir / "control" / "claim-requests"
    response_dir = run_dir / "control" / "claim-responses"
    event_path = run_dir / "control" / "evaluator-events.jsonl"
    terminal_request = run_dir / "control" / "terminal-request.json"
    processed: set[str] = set()
    started = time.monotonic()
    state_errors = 0
    consecutive_state_errors = 0

    while not protocol.terminal:
        now = time.monotonic()
        elapsed = now - started
        if terminal_request.is_file():
            try:
                request = json.loads(terminal_request.read_text(encoding="utf-8"))
                reason = request.get("reason")
            except (OSError, json.JSONDecodeError):
                reason = "invalid_terminal_request"
            protocol.terminate(
                reason if isinstance(reason, str) and reason else "external_stop"
            )
            break
        if STOP_REASON is not None:
            protocol.terminate(STOP_REASON)
            break
        if elapsed >= watchdog_timeout:
            protocol.terminate("wall_clock_watchdog")
            break
        try:
            state = _fetch_json(base_url + "/api/state")
            if state.get("success") is not True:
                error = str(state.get("error") or "unknown state error")
                if "Player or game mode not available" in error:
                    protocol.terminate("player_left_game")
                    break
                raise RuntimeError(f"AgentBridge state failure: {error}")
            position, health, block_position = parse_position_state(state)
            sample = protocol.observe(
                position=position,
                elapsed_sec=elapsed,
                health=health,
                block_position=block_position,
                sampled_at_monotonic=now,
            )
            if boundary_monitor is not None:
                boundary_sample = boundary_monitor.observe(
                    y=position["y"], elapsed_sec=elapsed
                )
                sample["out_of_bounds"] = boundary_sample
                if boundary_sample["newly_triggered"] and not protocol.terminal:
                    event = {
                        "kind": "out_of_bounds",
                        "message": (
                            "[Navigation evaluator] The player remained at or below "
                            f"Y={boundary_policy.at_or_below_y:g} for "
                            f"{boundary_policy.duration_sec:g} seconds."
                        ),
                    }
                    sample["evaluator_events"].append(event)
                    protocol.terminate("out_of_bounds")
            append_jsonl(
                results / "positions.jsonl",
                {"sampled_at_utc": _now(), **sample},
            )
            for event in sample["evaluator_events"]:
                append_jsonl(
                    event_path,
                    {"created_at_utc": _now(), **event},
                )
            consecutive_state_errors = 0
        except (
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ) as error:
            state_errors += 1
            consecutive_state_errors += 1
            append_jsonl(
                results / "positions.jsonl",
                {
                    "sampled_at_utc": _now(),
                    "elapsed_sec": round(elapsed, 6),
                    "state_error": str(error),
                },
            )
            if consecutive_state_errors >= args.max_consecutive_state_errors:
                protocol.terminate("agentbridge_state_unavailable")
                break

        for request_path in sorted(request_dir.glob("*.request.json")):
            if request_path.name in processed:
                continue
            request = load_claim_request(request_path)
            claim_id = request["claim_id"]
            response = protocol.evaluate_claim(claim_id=claim_id)
            atomic_write_json(
                response_dir / f"{claim_id}.response.json",
                response,
            )
            append_jsonl(
                results / "claims.jsonl",
                {
                    "received_at_utc": _now(),
                    "request": request,
                    "response": response,
                },
            )
            processed.add(request_path.name)
            if protocol.terminal:
                break
        if not protocol.terminal:
            time.sleep(max(0.0, interval - (time.monotonic() - now)))

    duration = time.monotonic() - started
    completion = protocol.result(duration_sec=duration)
    completion["out_of_bounds"] = (
        None
        if boundary_monitor is None
        else {
            "at_or_below_y": boundary_monitor.policy.at_or_below_y,
            "duration_sec": boundary_monitor.policy.duration_sec,
            "triggered": boundary_monitor.triggered,
        }
    )
    infrastructure_error = completion["terminal_reason"] in {
        "agentbridge_state_unavailable",
        "server_crash",
        "agent_crash",
        "llm_failure_limit",
        "wall_clock_watchdog",
    }
    completion.update(
        {
            "task_id": run["task_id"],
            "map_id": run["map_id"],
            "mode": run["mode"],
            "state_error_count": state_errors,
            "infrastructure_error": infrastructure_error,
            "ended_at_utc": _now(),
        }
    )
    atomic_write_json(results / "completion.json", completion)
    atomic_write_json(results / "metrics.json", completion["metrics"])
    atomic_write_json(
        results / "supervisor.json",
        {
            "schema_version": 1,
            "task_id": run["task_id"],
            "terminal_reason": completion["terminal_reason"],
            "infrastructure_error": infrastructure_error,
            "success": completion["success"],
            "ended_at_utc": completion["ended_at_utc"],
        },
    )
    return 0 if completion["success"] else (3 if infrastructure_error else 2)


if __name__ == "__main__":
    raise SystemExit(main())
