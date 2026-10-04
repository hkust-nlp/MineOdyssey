"""Golden contract extracted independently from a historical formal GLM run."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.agent import Agent
from agent.navigation_prompt import build_navigation_system_prompt
from eval.navigation.schema import find_task, load_setting, resolve_task_eval_setting, resolved_task

ROOT = Path(__file__).resolve().parents[1]
FORMAL = json.loads((Path(__file__).parent / "fixtures/navigation_formal_glm.json").read_text())


class FormalNavigationParityTests(unittest.TestCase):
    def test_effective_policy_matches_actual_formal_run(self):
        setting = load_setting("final-navigation-v1")
        for key in ("agent", "limits", "runtime"):
            self.assertEqual(setting[key], FORMAL[key], key)
        self.assertEqual(resolve_task_eval_setting({}), FORMAL["eval_setting"])

    def test_actual_native_prompt_and_tool_schema_match(self):
        prompt = build_navigation_system_prompt(action_protocol="tool_calls")
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), FORMAL["system_prompt_sha256"])
        agent = object.__new__(Agent)
        agent.allow_model_observe_toggle = False
        agent.navigation_claim_client = object()
        self.assertEqual(agent._action_tools(), FORMAL["tools"])


    def run_monitor(self, elapsed, terminal_reason=None):
        spec = importlib.util.spec_from_file_location(
            "formal_monitor_test", ROOT / "scripts/eval/monitor-navigation-goal-inside.py")
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)
        map_id, task = find_task("innopolis-008", {"innopolis"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "control").mkdir()
            run = dict(results_dir=str(root), setting_id="final-navigation-v1", map_id=map_id,
                       task_id=task["id"], task=resolved_task(map_id, task),
                       reference_length_blocks=None, mode="review", ports={"agentbridge": 1})
            (root / "control/run.json").write_text(json.dumps(run))
            if terminal_reason:
                (root / "control/terminal-request.json").write_text(json.dumps({"reason": terminal_reason}))
            with patch.object(monitor, "parse_args", return_value=SimpleNamespace(run_dir=root)), \
                    patch.object(monitor.signal, "signal"), \
                    patch.object(monitor.time, "monotonic", side_effect=[0, elapsed, elapsed]), \
                    patch.object(monitor, "_fetch_json", return_value={
                        "success": False, "error": "Player or game mode not available"}):
                code = monitor.main()
            return code, json.loads((root / "completion.json").read_text())

    def test_watchdog_boundary_and_infrastructure_classification(self):
        for elapsed in (5400, 21599.9):
            with self.subTest(elapsed=elapsed):
                code, result = self.run_monitor(elapsed)
                self.assertEqual(result["terminal_reason"], "player_left_game")
                self.assertFalse(result["infrastructure_error"])
                self.assertEqual(code, 2)
        code, result = self.run_monitor(21600)
        self.assertEqual(result["terminal_reason"], "wall_clock_watchdog")
        self.assertTrue(result["infrastructure_error"])
        self.assertEqual(code, 3)

    def test_step_limit_is_a_scored_navigation_end(self):
        code, result = self.run_monitor(1, "step_limit")
        self.assertEqual(result["terminal_reason"], "step_limit")
        self.assertFalse(result["infrastructure_error"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
