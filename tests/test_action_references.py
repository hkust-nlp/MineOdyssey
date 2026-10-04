"""Offline checks for short event references and intact native tool protocols."""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from openai import OpenAI
from openai.types.chat import ChatCompletionMessage

from agent.agent import Agent
from tests.test_auto_summarize import _build_agent


class ActionReferenceTests(unittest.TestCase):
    def agent(self):
        agent = _build_agent()
        agent.action_protocol = "tool_calls"
        agent.navigation_claim_client = None
        agent._capture_post_exec_snapshot = MagicMock()
        agent._mark_action_observation = MagicMock()
        agent._inject_pending_summary_request = MagicMock()
        return agent

    def call(self, signature="opaque-synthetic-" * 10000):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-shared-prefix__thought__" + signature,
                "type": "function",
                "function": {
                    "name": "minecraft_action",
                    "arguments": '{"type":"exec","content":"mcapi state"}',
                },
                "provider_specific_fields": {"thought_signature": signature},
            }],
        }

    def result(self, call_id, *, timed_out=False):
        return SimpleNamespace(
            command="mcapi state", command_status="timed_out" if timed_out else "completed",
            timestamp=2.0, stdout="state output", stderr="timeout" if timed_out else None,
            exit_code=124 if timed_out else 0, action_id=call_id, task_id="task-1",
        )

    def user_text(self, messages):
        return "\n".join(
            part["text"] for message in messages if message.get("role") == "user"
            for part in message["content"] if part.get("type") == "text"
        )

    def test_receipt_start_end_and_result_share_reference_with_raw_audit_ids(self):
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                agent = self.agent()
                message = self.call()
                original = copy.deepcopy(message)
                action, error = agent._parse_action_tool_calls(message)
                self.assertEqual(error, "")
                call_id = message["tool_calls"][0]["id"]
                self.assertEqual(action.action_id, call_id)
                agent._smart_append_msg(message)
                agent._append_action_tool_results(message, accepted=True)
                receipt = agent.conversation_history[-1]
                reference = json.loads(receipt["content"])["action_ref"]
                self.assertRegex(reference, r"^action_[0-9a-f]{24}$")
                self.assertEqual(receipt["tool_call_id"], call_id)
                agent.current_exec_action = {"id": call_id, "start_ts": 1.0, "finished": False}
                agent.pending_exec_result = True
                agent._insert_command_result_message(self.result(call_id, timed_out=timed_out))
                self.assertIsNone(agent.current_exec_action)
                self.assertFalse(agent.pending_exec_result)
                text = self.user_text(agent.conversation_history)
                self.assertIn("Action Start", text)
                self.assertIn("Action Timeout" if timed_out else "Action End", text)
                self.assertIn("state output", text)
                self.assertEqual(text.count("Action reference: " + reference), 3)
                self.assertNotIn("__thought__", text)
                self.assertNotIn("opaque-synthetic", text)
                self.assertLess(len(text), 1000)
                self.assertEqual(sum(
                    m.get("mcbots_action_id") == call_id for m in agent.full_message_history
                ), 3)
                self.assertEqual(message, original)

    def test_interruption_and_late_result_stay_bound_across_summary_reset(self):
        agent = self.agent()
        old, new = self.call("old-signature"), self.call("new-signature")
        old_id, new_id = (m["tool_calls"][0]["id"] for m in (old, new))
        agent._append_action_tool_results(old, accepted=True)
        old_ref = json.loads(agent.conversation_history[-1]["content"])["action_ref"]
        agent._append_action_tool_results(new, accepted=True)
        new_ref = json.loads(agent.conversation_history[-1]["content"])["action_ref"]
        self.assertNotEqual(old_ref, new_ref)
        agent._append_action_event("Action Interrupted", 1.0, action_id=old_id, related_action_id=new_id)
        text = self.user_text(agent.conversation_history)
        self.assertIn("Action reference: " + old_ref, text)
        self.assertIn("Related action reference: " + new_ref, text)
        self.assertNotIn("__thought__", text)
        agent.current_exec_action = {"id": new_id, "start_ts": 1.5, "finished": False}
        agent.pending_exec_result = True
        agent._perform_summary_reset("The earlier action was interrupted.")
        agent._insert_command_result_message(self.result(old_id))
        self.assertEqual(agent.current_exec_action["id"], new_id)
        self.assertFalse(agent.current_exec_action["finished"])
        self.assertTrue(agent.pending_exec_result)
        text = self.user_text(agent.conversation_history)
        self.assertIn("Earlier Interrupted Command Result", text)
        self.assertIn("Action reference: " + old_ref, text)
        self.assertNotIn("Action End", text)
        self.assertNotIn("__thought__", text)

    def test_rejected_unsigned_long_call_retains_native_pairing(self):
        agent = self.agent()
        message = self.call()
        call_id = "unsigned-" + "x" * 1000
        message["tool_calls"][0]["id"] = call_id
        agent._append_action_tool_results(message, accepted=False, error="invalid action")
        receipt = agent.conversation_history[-1]
        self.assertEqual(receipt["tool_call_id"], call_id)
        payload = json.loads(receipt["content"])
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"], "invalid action")
        self.assertRegex(payload["action_ref"], r"^action_[0-9a-f]{24}$")

    def test_sdk_wire_separates_signature_from_ids_and_preserves_archive(self):
        agent = self.agent()
        message = ChatCompletionMessage.model_validate(self.call()).model_dump()
        call_id = message["tool_calls"][0]["id"]
        agent._smart_append_msg(message)
        agent._append_action_tool_results(message, accepted=True)
        agent._append_action_event("Action Start", 1.0, action_id=call_id)
        original = copy.deepcopy(agent.conversation_history)
        agent.model = "google/gemini-offline-test"
        agent.llm_request_total = 1
        agent.llm_watchdog_interval_sec = 0
        agent.llm_request_timeout_sec = 1
        agent.llm_max_retries = 0
        agent.llm_429_max_retries = 0
        agent.llm_request_gate = None
        agent.sampling_params = {}
        captured = []

        def receive(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "chat-test", "object": "chat.completion", "created": 1,
                "model": agent.model,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": "test response",
                }}],
            })

        with OpenAI(api_key="offline-test", base_url="https://example.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(receive))) as client:
            agent.client = client
            for summary in (False, True):
                request = agent._apply_action_request_protocol({
                    "model": agent.model,
                    "messages": [agent._sanitize_message_for_llm(m) for m in agent.conversation_history],
                    "max_tokens": 100,
                }, is_summary_request=summary)
                agent._create_model_completion(request)
        self.assertEqual(len(captured), 2)
        for request in captured:
            messages = request["messages"]
            expected_call = copy.deepcopy(message["tool_calls"][0])
            expected_call["id"] = agent._action_reference(call_id)
            self.assertEqual(messages[0]["tool_calls"], [expected_call])
            self.assertEqual(messages[1]["tool_call_id"], expected_call["id"])
            reference = json.loads(messages[1]["content"])["action_ref"]
            self.assertIn("Action reference: " + reference, self.user_text(messages))
            self.assertNotIn("__thought__", self.user_text(messages))
            self.assertTrue(all(not k.startswith("mcbots_") for m in messages for k in m))
        self.assertEqual(agent.conversation_history, original)


if __name__ == "__main__":
    unittest.main()
