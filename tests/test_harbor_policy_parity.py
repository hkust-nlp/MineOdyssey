"""Harbor-specific transport and instruction checks against the shared policy."""
import json
from pathlib import Path
import tempfile
import unittest

from agent.navigation_prompt import build_navigation_system_prompt
from eval.navigation.schema import load_setting
from eval.harbor_agents.original import child_environment, terminal_reason
from eval.harbor_agents.instructions import TASK, check_instruction, expected_instruction

FORMAL = json.loads((Path(__file__).parent / "fixtures/navigation_formal_glm.json").read_text())


class HarborPolicyParityTests(unittest.TestCase):
    def test_container_launch_passes_formal_limits_and_native_parameters(self):
        setting = load_setting("final-navigation-v1")
        session = dict(setting, prompt="unchanged task", display=":1", record_video=False,
                       eval_setting=setting["task_eval_defaults"])
        params = dict(FORMAL["model_parameters"])
        model = params.pop("model_id")
        config = dict(api_key="test", base_url="http://localhost", modelname=model, model_params=params)
        values = child_environment(session, config, Path("/tmp/formal-parity"), 1234)
        self.assertEqual(values["MCBOTS_ACTION_PROTOCOL"], "tool_calls")
        self.assertEqual(values["MCBOTS_MAX_LLM_REQUEST_SUCCESSES"], "500")
        self.assertEqual(values["MCBOTS_AUTO_SUMMARIZE_TURN_THRESHOLD"], "100")
        self.assertEqual(values["MCBOTS_LLM_MAX_RETRIES"], "0")
        self.assertEqual(values["MCBOTS_MAX_CONSECUTIVE_LLM_FAILURES"], "3")
        expected = {k: v for k, v in params.items() if k not in {"action_protocol", "api_protocol"}}
        self.assertEqual(json.loads(values["MCBOTS_MODEL_PARAMS_JSON"]), expected)
        self.assertEqual(terminal_reason({"stop_reason": "max_llm_request_successes"}, 0), "step_limit")

    def test_harbor_preserves_complete_objective_and_route_policy(self):
        instruction = (TASK / "instruction.md").read_text()
        baseline = build_navigation_system_prompt(action_protocol="tool_calls")
        policy = baseline.split("## Objective\n\n", 1)[1].split("## Actions\n\n", 1)[0].strip()
        actual = instruction.split("## Objective\n\n", 1)[1].split("## Harbor interface\n\n", 1)[0].strip()
        self.assertEqual(actual.replace("`nav claim-done`", "`claim_done`"), policy)
        self.assertNotIn("3.5", instruction)
        self.assertNotIn("5400", instruction)
        self.assertNotIn("samples actual player positions", instruction)
        self.assertEqual(instruction, expected_instruction())
        check_instruction()

    def test_export_check_rejects_divergent_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "environment/world/task-spec.json"
            spec.parent.mkdir(parents=True)
            spec.write_text('{"task_id":"innopolis-006","map_id":"innopolis"}')
            (root / "instruction.md").write_text(expected_instruction().replace("2.5m", "3.5m"))
            with self.assertRaisesRegex(ValueError, "instruction is stale"):
                check_instruction(root)


if __name__ == "__main__":
    unittest.main()
