"""Signature transport must preserve state and tool pairing without text copies."""
import copy
import unittest
from unittest.mock import patch

from tests.test_auto_summarize import _build_agent


class GeminiSignatureRequestTests(unittest.TestCase):
    def setUp(self):
        self.agent = _build_agent()
        self.agent.model = "gcp/google/gemini-3.8-flash"

    def call(self, signature, *, field=True):
        call = {
            "id": "reused-prefix__thought__" + signature,
            "type": "function",
            "function": {"name": "minecraft_action", "arguments": '{"type":"skip"}'},
        }
        if field:
            call["provider_specific_fields"] = {"thought_signature": signature, "other": 1}
        return call

    def request(self, calls):
        return {"model": self.agent.model, "messages": [
            {"role": "assistant", "content": "visible answer", "tool_calls": calls,
             "reasoning_content": "visible reasoning",
             "thinking_blocks": [{"type": "thinking", "thinking": "keep this"}],
             "provider_specific_fields": {
                 "thought_signatures": [c["id"].split("__thought__", 1)[1] for c in calls],
                 "unrelated": True,
             }},
            *[{"role": "tool", "tool_call_id": c["id"], "content": "unchanged result"}
              for c in calls],
            {"role": "user", "content": [{"type": "image_url", "image_url": {
                "url": "data:image/png;base64,synthetic"}}]},
        ]}

    def test_parallel_calls_preserve_distinct_state_pairing_and_entire_source(self):
        req = self.request([self.call("a" * 200000), self.call("b" * 200000)])
        original = copy.deepcopy(req)
        result = self.agent._prepare_gemini_signature_request(req)
        self.assertEqual(req, original)
        self.assertEqual(result["messages"][0]["provider_specific_fields"], {"unrelated": True})
        for i, old in enumerate(original["messages"][0]["tool_calls"]):
            call = result["messages"][0]["tool_calls"][i]
            self.assertEqual(call["id"], self.agent._action_reference(old["id"]))
            self.assertEqual(result["messages"][i + 1]["tool_call_id"], call["id"])
            self.assertEqual(call["function"], old["function"])
            self.assertEqual(call["provider_specific_fields"], old["provider_specific_fields"])
            call["id"] = old["id"]
            result["messages"][i + 1]["tool_call_id"] = old["id"]
        result["messages"][0]["provider_specific_fields"] = original["messages"][0]["provider_specific_fields"]
        self.assertEqual(result, original)

    def test_id_only_signature_is_recovered_and_second_normalization_is_identical(self):
        req = self.request([self.call("id-only-state", field=False)])
        result = self.agent._prepare_gemini_signature_request(req)
        self.assertEqual(result["messages"][0]["tool_calls"][0]["provider_specific_fields"],
                         {"thought_signature": "id-only-state"})
        self.assertEqual(self.agent._prepare_gemini_signature_request(result), result)

    def test_text_only_and_unmatched_message_signatures_are_not_removed(self):
        req = self.request([self.call("call-state")])
        req["messages"][0]["provider_specific_fields"]["thought_signatures"].append("extra-state")
        text_message = {"role": "assistant", "content": "summary", "provider_specific_fields": {
            "thought_signatures": ["text-only-state"]}}
        req["messages"].append(text_message)
        result = self.agent._prepare_gemini_signature_request(req)
        self.assertEqual(result["messages"][0]["provider_specific_fields"],
                         req["messages"][0]["provider_specific_fields"])
        self.assertEqual(result["messages"][-1], text_message)

    def test_only_first_parallel_call_signed_and_unsigned_call_is_unchanged(self):
        req = self.request([self.call("first-call-state")])
        unsigned = {"id": "ordinary-id", "type": "function", "function": {
            "name": "minecraft_action", "arguments": '{"type":"skip"}'}}
        req["messages"][0]["tool_calls"].append(unsigned)
        req["messages"].append({"role": "tool", "tool_call_id": "ordinary-id", "content": "OK"})
        result = self.agent._prepare_gemini_signature_request(req)
        self.assertEqual(result["messages"][0]["tool_calls"][1], unsigned)
        self.assertEqual(result["messages"][-1], req["messages"][-1])

    def test_empty_and_conflicting_signatures_do_not_reach_provider(self):
        for signature, replacement in [("", ""), ("original", "different")]:
            with self.subTest(signature=signature):
                req = self.request([self.call(signature)])
                req["messages"][0]["tool_calls"][0]["provider_specific_fields"]["thought_signature"] = replacement
                with patch.object(self.agent, "_create_chat_completion") as provider:
                    with self.assertRaisesRegex(ValueError, "signature is empty or inconsistent"):
                        self.agent._create_model_completion(req)
                    provider.assert_not_called()

    def test_short_id_collision_does_not_reach_provider(self):
        req = self.request([self.call("original")])
        other = copy.deepcopy(req["messages"][0]["tool_calls"][0])
        other["id"] = self.agent._action_reference(other["id"])
        req["messages"][0]["tool_calls"].append(other)
        with patch.object(self.agent, "_create_chat_completion") as provider:
            with self.assertRaisesRegex(ValueError, "ID collision"):
                self.agent._create_model_completion(req)
            provider.assert_not_called()

    def test_other_models_and_responses_protocol_are_unchanged(self):
        for model in ["meta/muse-spark", "claude-opus-5", "gpt-5.6-sol", "deepseek", "glm", "grok"]:
            req = self.request([self.call("synthetic-state")])
            req["model"] = model
            self.assertIs(self.agent._prepare_gemini_signature_request(req), req)
        self.agent.api_protocol = "responses"
        req = self.request([self.call("synthetic-state")])
        with patch.object(self.agent, "_create_responses_completion") as provider:
            self.agent._create_model_completion(req)
            provider.assert_called_once_with(req, continue_chain=True)


if __name__ == "__main__":
    unittest.main()
