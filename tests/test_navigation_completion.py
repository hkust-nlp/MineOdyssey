import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.navigation_completion import NavigationClaimClient
from eval.navigation.completion import (
    ArrivalPolicy,
    CompletionError,
    CompletionProtocol,
    MinimumHeightBoundaryMonitor,
    MinimumHeightBoundaryPolicy,
    atomic_write_json,
    load_claim_request,
    parse_position_state,
)
from eval.navigation.metrics import NavigationMetricAccumulator


def _protocol() -> CompletionProtocol:
    target = {"x": 10, "y": 4, "z": 20}
    required = [
        {
            "waypoint_id": "middle",
            "name": "Middle",
            "position": {"x": 5, "y": 4, "z": 10},
        }
    ]
    return CompletionProtocol(
        target=target,
        policy=ArrivalPolicy(
            radius_3d=3,
            radius_y=1.5,
            sample_interval_sec=1,
        ),
        claim_attempt_limit=3,
        feedback_limit=2,
        metrics=NavigationMetricAccumulator(
            start={"x": 0, "y": 4, "z": 0},
            target=target,
            required_waypoints=required,
            reference_length_blocks=25,
        ),
        required_waypoints=required,
        target_name="Target",
    )


class NavigationCompletionTest(unittest.TestCase):
    def test_minimum_height_boundary_requires_fifteen_continuous_seconds(self):
        monitor = MinimumHeightBoundaryMonitor(
            MinimumHeightBoundaryPolicy(at_or_below_y=-14, duration_sec=15)
        )
        self.assertFalse(monitor.observe(y=-14, elapsed_sec=10)["triggered"])
        self.assertFalse(monitor.observe(y=-20, elapsed_sec=24.9)["triggered"])
        result = monitor.observe(y=-14, elapsed_sec=25)
        self.assertTrue(result["newly_triggered"])
        self.assertTrue(result["triggered"])

    def test_minimum_height_boundary_resets_above_threshold(self):
        monitor = MinimumHeightBoundaryMonitor(
            MinimumHeightBoundaryPolicy(at_or_below_y=-14, duration_sec=15)
        )
        monitor.observe(y=-20, elapsed_sec=0)
        monitor.observe(y=-13.99, elapsed_sec=14)
        self.assertFalse(monitor.observe(y=-20, elapsed_sec=20)["triggered"])
        self.assertFalse(monitor.observe(y=-20, elapsed_sec=34.9)["triggered"])
        self.assertTrue(monitor.observe(y=-20, elapsed_sec=35)["triggered"])

    def test_required_waypoint_and_target_are_recorded_in_order(self):
        protocol = _protocol()
        early = protocol.observe(
            position={"x": 10, "y": 4, "z": 20},
            elapsed_sec=0,
        )
        self.assertFalse(early["oracle_arrived"])
        middle = protocol.observe(
            position={"x": 5, "y": 4, "z": 10},
            elapsed_sec=1,
        )
        self.assertEqual(
            middle["evaluator_events"][0]["kind"],
            "required_waypoint_recorded",
        )
        target = protocol.observe(
            position={"x": 10, "y": 4, "z": 20},
            elapsed_sec=2,
        )
        self.assertEqual(
            target["evaluator_events"][0]["kind"],
            "final_destination_recorded",
        )
        self.assertTrue(protocol.oracle_arrived)
        self.assertFalse(protocol.terminal)
        response = protocol.evaluate_claim(claim_id="accepted")
        self.assertTrue(response["accepted"])
        self.assertTrue(protocol.success)

    def test_wrong_claim_feedback_lists_remaining_route(self):
        protocol = _protocol()
        protocol.observe(
            position={"x": 0, "y": 5, "z": 0},
            elapsed_sec=0,
            sampled_at_monotonic=10,
        )
        first = protocol.evaluate_claim(claim_id="one", now_monotonic=10.1)
        second = protocol.evaluate_claim(claim_id="two", now_monotonic=10.2)
        third = protocol.evaluate_claim(claim_id="three", now_monotonic=10.3)
        self.assertEqual(first["feedback"]["remaining_locations"], ["Middle", "Target"])
        self.assertEqual(first["feedback"]["remaining_attempts"], 2)
        self.assertIsNotNone(second["feedback"])
        self.assertIsNone(third["feedback"])
        self.assertTrue(third["terminal"])
        self.assertEqual(protocol.terminal_reason, "claim_attempts_exhausted")

    def test_death_is_terminal_failure(self):
        protocol = _protocol()
        protocol.observe(
            position={"x": 0, "y": 4, "z": 0},
            elapsed_sec=0,
            health=0,
        )
        self.assertTrue(protocol.terminal)
        self.assertFalse(protocol.success)
        self.assertEqual(protocol.terminal_reason, "death")

    def test_agentbridge_state_parser(self):
        position, health, block = parse_position_state(
            {
                "data": {
                    "position": {"x": 1.5, "y": 2, "z": -3},
                    "health": 19.0,
                    "block_position": {"x": 1, "y": 2, "z": -3},
                }
            }
        )
        self.assertEqual(position, {"x": 1.5, "y": 2.0, "z": -3.0})
        self.assertEqual(health, 19.0)
        self.assertEqual(block, {"x": 1, "y": 2, "z": -3})

    def test_agent_claim_channel_cannot_write_success_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = root / "requests"
            responses = root / "responses"
            requests.mkdir()
            responses.mkdir()

            def evaluator():
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    paths = list(requests.glob("*.request.json"))
                    if paths:
                        request = json.loads(paths[0].read_text(encoding="utf-8"))
                        atomic_write_json(
                            responses / f"{request['claim_id']}.response.json",
                            {
                                "schema_version": 1,
                                "claim_id": request["claim_id"],
                                "accepted": True,
                            },
                        )
                        return
                    time.sleep(0.01)

            thread = threading.Thread(target=evaluator)
            thread.start()
            client = NavigationClaimClient(
                request_dir=requests,
                response_dir=responses,
                timeout_sec=2,
                poll_interval_sec=0.01,
            )
            result = client.claim()
            thread.join(timeout=2)
            self.assertTrue(result["accepted"])
            self.assertEqual(len(list(requests.glob("*.request.json"))), 1)
            self.assertEqual(len(list(responses.glob("*.response.json"))), 1)

    def test_agent_reads_evaluator_events_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.jsonl"
            events.write_text(
                json.dumps({"message": "waypoint recorded"}) + "\n",
                encoding="utf-8",
            )
            client = NavigationClaimClient(
                request_dir=root / "requests",
                response_dir=root / "responses",
                event_path=events,
            )
            self.assertEqual(client.read_events(), ["waypoint recorded"])
            self.assertEqual(client.read_events(), [])

    def test_claim_request_rejects_path_like_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.request.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "action": "claim_done",
                        "claim_id": "../completion",
                        "issued_at_unix_sec": time.time(),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CompletionError, "unsafe claim_id"):
                load_claim_request(path)


if __name__ == "__main__":
    unittest.main()
