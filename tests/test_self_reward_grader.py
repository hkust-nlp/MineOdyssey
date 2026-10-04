"""Unit tests for self-reward grader request construction."""

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.agent import Agent
from agent.grading_prompt import GRADER_SYSTEM_PROMPT


def _build_agent():
    agent = object.__new__(Agent)
    agent.messages_lock = threading.RLock()
    agent.conversation_history = [
        {"role": "system", "content": "ORIGINAL_AGENT_SYSTEM_PROMPT"},
        {"role": "user", "content": [{"type": "text", "text": "Task"}]},
        {"role": "assistant", "content": "I will move forward."},
    ]

    agent.enable_self_reward = True
    agent.self_reward_in_flight = False
    agent.self_reward_window_count = 0
    agent.self_reward_history = []
    agent.spec_criteria_history = []
    agent.record_dir = None
    agent.state_snapshot_provider = None
    agent._window_state_record = None
    agent.model = "test-model"
    agent.api_protocol = "chat_completions"
    agent.sampling_params = {}
    agent.request_extra_body = None

    return agent


class TestSelfRewardGraderRequest(unittest.TestCase):
    def test_uses_grader_system_prompt_without_agent_system_prompt(self):
        agent = _build_agent()
        captured = {}

        def fake_create_chat_completion(request_kwargs):
            captured["messages"] = request_kwargs["messages"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"specific_criteria": null, '
                                '"rewards": [{"turn": 0, "score": 0, "reason": "ok"}], '
                                '"overall": {"score": 0, "reason": "ok"}}'
                            ),
                            reasoning_content=None,
                        )
                    )
                ]
            )

        agent._create_chat_completion = fake_create_chat_completion

        self.assertTrue(agent._run_out_of_band_self_reward("test"))

        messages = captured["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": GRADER_SYSTEM_PROMPT})
        self.assertFalse(any(m.get("role") == "system" for m in messages[1:]))
        self.assertNotIn("ORIGINAL_AGENT_SYSTEM_PROMPT", repr(messages))
        self.assertEqual(agent.self_reward_history[0]["n_responses"], 1)


if __name__ == "__main__":
    unittest.main()
