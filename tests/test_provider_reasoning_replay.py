"""Exercise provider replay without changing the baseline image/context policy."""
import copy
import json
import unittest

import httpx
from openai import OpenAI
from openai.types.chat import ChatCompletionMessage

from agent.agent import Agent


class ProviderReasoningReplayTests(unittest.TestCase):
    def message(self):
        return ChatCompletionMessage.model_validate({
            "role": "assistant", "content": None,
            "reasoning": "provider-visible summary",
            "reasoning_details": [
                {"type": "reasoning.encrypted", "data": "opaque-first", "format": "meta-responses-v1", "id": "rs-first", "index": 0},
                {"type": "reasoning.encrypted", "data": "opaque-second", "format": "meta-responses-v1", "id": "rs-second", "index": 1},
            ],
            "tool_calls": [{"id": "call-first", "type": "function", "function": {
                "name": "minecraft_action", "arguments": '{"type":"exec","content":"mcapi state"}',
            }}],
            "usage": {"total_tokens": 123}, "mcbots_local_metadata": "must-not-send",
        }).model_dump()

    def test_preserves_opaque_sequence_without_mutating_archive(self):
        message = self.message()
        original = copy.deepcopy(message)
        sent = Agent._sanitize_message_for_llm(message)
        self.assertEqual(sent["reasoning_details"], original["reasoning_details"])
        self.assertEqual(sent["reasoning"], original["reasoning"])
        self.assertEqual(sent["content"], "")
        self.assertNotIn("usage", sent)
        self.assertNotIn("mcbots_local_metadata", sent)
        sent["reasoning_details"][0]["data"] = "wire-only-change"
        self.assertEqual(message, original)

    def test_runtime_sender_serializes_reasoning_for_decisions_and_summaries(self):
        for summary in (False, True):
            for image_count in (1, 51):
                with self.subTest(summary=summary, image_count=image_count):
                    captured = []

                    def receive(request):
                        captured.append(json.loads(request.content))
                        return httpx.Response(200, json={
                            "id": "chat-test", "object": "chat.completion", "created": 1,
                            "model": "meta/muse-spark-1.3-contributor",
                            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                "role": "assistant", "content": "test response",
                            }}],
                        })

                    agent = Agent.__new__(Agent)
                    agent.model = "meta/muse-spark-1.3-contributor"
                    agent.api_protocol = "chat_completions"
                    agent.action_protocol = "tool_calls"
                    agent.allow_model_observe_toggle = False
                    agent.navigation_claim_client = None
                    agent.llm_request_total = 1
                    agent.llm_watchdog_interval_sec = 0
                    agent.llm_request_timeout_sec = 1
                    agent.llm_max_retries = 0
                    agent.llm_429_max_retries = 0
                    agent.llm_request_gate = None
                    agent.sampling_params = {}
                    raw = self.message()
                    history = [raw, {"role": "tool", "tool_call_id": "call-first", "content": "received"}, {
                        "role": "user", "content": [{"type": "image_url", "image_url": {
                            "url": f"https://example.invalid/frame-{index}.jpg",
                        }} for index in range(image_count)],
                    }]
                    before = copy.deepcopy(history)
                    request = agent._apply_action_request_protocol({
                        "model": agent.model,
                        "messages": [agent._sanitize_message_for_llm(m) for m in history],
                        "max_tokens": 32000, "reasoning_effort": "xhigh",
                    }, is_summary_request=summary)
                    with OpenAI(api_key="offline-test", base_url="https://example.invalid/v1",
                                http_client=httpx.Client(transport=httpx.MockTransport(receive))) as client:
                        agent.client = client
                        agent._create_model_completion(request)
                    self.assertEqual(len(captured), 1)
                    sent = captured[0]
                    self.assertEqual(sent["messages"][0]["reasoning_details"], raw["reasoning_details"])
                    self.assertEqual(sent["messages"][0]["reasoning"], raw["reasoning"])
                    self.assertEqual(sent["messages"][0]["tool_calls"], raw["tool_calls"])
                    self.assertEqual(sent["messages"][1], history[1])
                    self.assertNotIn("usage", sent["messages"][0])
                    self.assertNotIn("mcbots_local_metadata", sent["messages"][0])
                    self.assertEqual("tools" in sent, not summary)
                    self.assertEqual(sum(agent._count_images_in_message(m) for m in sent["messages"]), image_count)
                    self.assertEqual(history, before)


if __name__ == "__main__":
    unittest.main()
