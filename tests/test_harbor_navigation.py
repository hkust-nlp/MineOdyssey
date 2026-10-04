import importlib.util
import argparse
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval.navigation.completion import ArrivalPolicy, CompletionProtocol
from eval.navigation.metrics import NavigationMetricAccumulator
from eval.navigation.schema import load_setting, load_tasks, resolved_task

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "eval/harbor/innopolis-006"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


gateway = module("harbor_gateway", TASK / "environment/world/gateway.py")
verifier = module("harbor_verifier", TASK / "tests/verify.py")
nav = module("harbor_nav", TASK / "environment/nav.py")


class HarborNavigationTests(unittest.TestCase):
    def protocol(self):
        task = next(t for t in load_tasks("innopolis") if t["id"] == "innopolis-006")
        self.task = resolved_task("innopolis", task)
        return CompletionProtocol(target=self.task["target"]["position"],
            metrics=NavigationMetricAccumulator(start=self.task["start"]["position"],
                target=self.task["target"]["position"],
                required_waypoints=self.task["required_waypoints"], reference_length_blocks=None),
            required_waypoints=self.task["required_waypoints"],
            target_name=self.task["target"]["name"],
            policy=ArrivalPolicy.from_setting(load_setting("final-navigation-v1")),
            claim_attempt_limit=3, feedback_limit=2)

    def completion(self, protocol):
        return {**protocol.result(), "task_id": "innopolis-006", "infrastructure_error": False}

    def test_all_visits_and_claim_receive_reward(self):
        protocol = self.protocol()
        for index, point in enumerate(self.task["required_waypoints"]):
            protocol.observe(position=point["position"], elapsed_sec=index + 1, health=20)
        protocol.observe(position=self.task["target"]["position"], elapsed_sec=10, health=20)
        protocol.evaluate_claim(claim_id="a" * 32)
        self.assertEqual(verifier.reward(self.completion(protocol)), 1)

    def test_unvisited_destination_rejected(self):
        protocol = self.protocol()
        protocol.observe(position=self.task["start"]["position"], elapsed_sec=1, health=20)
        for index in range(3):
            protocol.evaluate_claim(claim_id=str(index) * 32)
        result = self.completion(protocol)
        self.assertEqual(result["terminal_reason"], "claim_attempts_exhausted")
        self.assertEqual(verifier.reward(result), 0)

    def test_no_claim_no_reward(self):
        protocol = self.protocol()
        self.assertEqual(verifier.reward(self.completion(protocol)), 0)

    def test_final_destination_does_not_replace_required_visits(self):
        protocol = self.protocol()
        protocol.observe(position=self.task["target"]["position"], elapsed_sec=1, health=20)
        protocol.evaluate_claim(claim_id="a" * 32)
        self.assertEqual(verifier.reward(self.completion(protocol)), 0)

    def test_wrong_height_does_not_count_as_arrival(self):
        protocol = self.protocol()
        for index, point in enumerate(self.task["required_waypoints"]):
            position = {**point["position"], "y": point["position"]["y"] + 2}
            protocol.observe(position=position, elapsed_sec=index + 1, health=20)
        protocol.evaluate_claim(claim_id="a" * 32)
        self.assertEqual(verifier.reward(self.completion(protocol)), 0)

    def test_wrong_task_or_infrastructure_failure_no_reward(self):
        good = dict(task_id="innopolis-006", terminal=True, success=True,
                    terminal_reason="claim_done_arrived", infrastructure_error=False)
        self.assertEqual(verifier.reward(good), 1)
        self.assertEqual(verifier.reward({**good, "task_id": "other"}), 0)
        self.assertEqual(verifier.reward({**good, "infrastructure_error": True}), 0)
        self.assertEqual(verifier.reward({**good, "success": "true"}), 0)

    def test_unknown_endpoint_cannot_write_results(self):
        with self.assertRaises(ValueError):
            gateway.dispatch("write-result", {"success": True})

    def test_action_timeout_is_bounded(self):
        with patch.object(gateway, "start", return_value={"ready": True}):
            for timeout in (0, -1, 120.1, 300, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    gateway.dispatch("exec", {"command": "true", "timeout": timeout})

    def test_sync_timeout_matches_unchanged_remote_bash_limit(self):
        source = ROOT / "scripts/runtime/remote_bash_server.py"
        nodes = [node for node in ast.parse(source.read_text()).body
                 if (isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id in {
                         "DEFAULT_EXEC_TIMEOUT_SEC", "MAX_EXEC_TIMEOUT_SEC"}
                     for target in node.targets))
                 or (isinstance(node, ast.FunctionDef) and node.name == "normalize_timeout")]
        scope = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
        self.assertEqual(nav.MAX_EXEC_TIMEOUT_SEC, scope["MAX_EXEC_TIMEOUT_SEC"])
        self.assertEqual(gateway.MAX_EXEC_TIMEOUT_SEC, scope["MAX_EXEC_TIMEOUT_SEC"])
        for timeout in (0.1, 30, 120):
            with patch.object(gateway, "start", return_value={"ready": True}), \
                 patch.object(gateway, "remote", return_value={"exit_code": 0}) as remote:
                gateway.dispatch("exec", {"command": "true", "timeout": nav.exec_timeout(str(timeout))})
                self.assertEqual(scope["normalize_timeout"](remote.call_args.args[1]), timeout)

    def test_cli_rejects_unsupported_timeout_before_any_transport(self):
        with patch.object(nav, "request") as request, \
             patch("sys.argv", ["nav", "exec", "true", "--timeout", "300"]), \
             patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:
                nav.main()
            self.assertEqual(error.exception.code, 2)
            request.assert_not_called()
        for value in ("0", "-1", "120.1", "nan", "inf"):
            with self.assertRaises(argparse.ArgumentTypeError):
                nav.exec_timeout(value)

    def test_terminal_task_rejects_more_actions(self):
        with patch.object(gateway, "start", return_value={"terminal": True}):
            with self.assertRaises(RuntimeError):
                gateway.dispatch("exec", {"command": "true"})

    def test_never_started_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(gateway, "RESULT", Path(directory) / "missing"), patch.object(gateway, "PROCESS", None):
                self.assertEqual(verifier.reward(gateway.finish()), 0)

    def test_client_payload_cannot_supply_reward(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "completion.json"
            result.write_text(json.dumps({"success": False, "terminal": True}))
            with patch.object(gateway, "RESULT", result):
                self.assertFalse(gateway.dispatch("finish", {"success": True})["success"])

    def test_original_prompt_is_preserved(self):
        task = next(t for t in load_tasks("innopolis") if t["id"] == "innopolis-006")
        self.assertTrue((TASK / "instruction.md").read_text().startswith(task["prompt"]))


if __name__ == "__main__":
    unittest.main()
