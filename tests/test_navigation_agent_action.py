import unittest
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from openai.types.chat import ChatCompletionMessage

from agent.agent import Agent
from agent.main import (
    NAVIGATION_SYSTEM_PROMPT,
    build_navigation_system_prompt,
    build_system_prompt,
)


class NavigationAgentActionTest(unittest.TestCase):
    def _agent(self, claim_enabled: bool):
        agent = Agent.__new__(Agent)
        agent.navigation_claim_client = object() if claim_enabled else None
        agent.allow_model_observe_toggle = False
        return agent

    def test_claim_action_is_conditionally_parseable(self):
        text = "<action><type>claim_done</type></action>"
        self.assertIsNone(self._agent(False)._parse_action(text))
        action = self._agent(True)._parse_action(text)
        self.assertIsNotNone(action)
        self.assertEqual(action.type, "claim_done")

    def test_navigation_prompt_is_separate_and_minimal(self):
        ordinary = build_system_prompt(
            "/workspace",
            "/missing",
            "/missing",
            navigation_claim_enabled=False,
        )
        self.assertNotIn("`claim_done`", ordinary)
        self.assertIn("`claim_done`", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("mcapi state", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("mcapi right-click-block", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("standard `xdotool` command syntax", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("maximum runtime of 300 seconds", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("within 2.5m (blocks)", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("displayed distance is 2.5m or less", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("for the full 3 seconds", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("makes it impossible to return", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("Actively identify boundaries", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("stop and carefully inspect the ground", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("never walk, run, or jump blindly", NAVIGATION_SYSTEM_PROMPT)
        normalized_prompt = " ".join(NAVIGATION_SYSTEM_PROMPT.split())
        self.assertIn(
            "Visit each required waypoint in the order stated by the user",
            normalized_prompt,
        )
        self.assertNotIn("Focus on navigation", normalized_prompt)
        self.assertNotIn("narrative details are context", normalized_prompt)
        self.assertIn("action_id", NAVIGATION_SYSTEM_PROMPT)
        self.assertNotIn("tool_call_id", NAVIGATION_SYSTEM_PROMPT)
        self.assertIn("random 8-character", NAVIGATION_SYSTEM_PROMPT)
        self.assertNotIn("do not perform or verify", normalized_prompt)
        self.assertNotIn("Do not spend time searching", normalized_prompt)
        self.assertNotIn("within 3m (blocks)", NAVIGATION_SYSTEM_PROMPT)
        self.assertNotIn("Navigation Strategy Hints", NAVIGATION_SYSTEM_PROMPT)
        self.assertNotIn("minimap", NAVIGATION_SYSTEM_PROMPT.lower())
        self.assertNotIn("HUD", NAVIGATION_SYSTEM_PROMPT)
        self.assertNotIn("Baritone", NAVIGATION_SYSTEM_PROMPT)
        self.assertNotIn("mcapi chat", NAVIGATION_SYSTEM_PROMPT)

    def test_navigation_prompt_optional_sections_follow_task_settings(self):
        prompt = build_navigation_system_prompt(
            hints_enabled=True,
            panorama_enabled=True,
        )
        self.assertIn("Navigation Strategy Hints", prompt)
        self.assertIn("hotbar slot 2", prompt)
        self.assertIn("Look around frequently", prompt)
        self.assertIn("world map can be zoomed and panned", prompt)
        self.assertIn("three-dimensional environment", prompt)
        self.assertIn("Six-view Observation", prompt)
        self.assertNotIn("minimap", prompt.lower())
        self.assertNotIn("coordinate/orientation HUD", prompt)
        self.assertNotIn("__NAVIGATION_", prompt)

    def test_navigation_tool_call_prompt_removes_xml_and_dsml_formats(self):
        prompt = build_navigation_system_prompt(action_protocol="tool_calls")
        self.assertIn("call the provided `minecraft_action` function", prompt)
        self.assertNotIn("action_id", prompt)
        self.assertNotIn("tool_call_id", prompt)
        self.assertNotIn("legacy action format", prompt)
        self.assertNotIn("<action>", prompt)
        self.assertNotIn("DSML", prompt)
        self.assertNotIn("XML action block", prompt)
        self.assertNotIn("__NAVIGATION_", prompt)

    def test_native_action_tool_schema_and_valid_call(self):
        agent = self._agent(claim_enabled=True)
        agent.action_protocol = "tool_calls"
        tools = agent._action_tools()
        function = tools[0]["function"]
        self.assertEqual(function["name"], "minecraft_action")
        self.assertEqual(
            function["parameters"]["properties"]["type"]["enum"],
            ["exec", "skip", "stop_execute", "claim_done"],
        )
        message = SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    id="call-1",
                    type="function",
                    function=SimpleNamespace(
                        name="minecraft_action",
                        arguments=(
                            '{"type":"exec","content":"mcapi state",'
                            '"observe_after_sec":0.5}'
                        ),
                    ),
                )
            ]
        )
        action, error = agent._parse_action_tool_calls(message)
        self.assertEqual(error, "")
        self.assertEqual(action.type, "exec")
        self.assertEqual(action.content, "mcapi state")
        self.assertEqual(action.observe_after_sec, 1.0)
        self.assertEqual(action.action_id, "call-1")

        decision_request = agent._apply_action_request_protocol(
            {"model": "deepseek-v4-flash-vision-exp", "messages": []},
            is_summary_request=False,
        )
        self.assertEqual(decision_request["tools"], tools)
        self.assertNotIn("tool_choice", decision_request)
        summary_request = agent._apply_action_request_protocol(
            {"model": "deepseek-v4-flash-vision-exp", "messages": []},
            is_summary_request=True,
        )
        self.assertNotIn("tools", summary_request)
        self.assertNotIn("tool_choice", summary_request)

        bedrock_summary_request = agent._apply_action_request_protocol(
            {
                "model": "aws/anthropic/bedrock-claude-opus-5",
                "messages": [],
            },
            is_summary_request=True,
        )
        self.assertEqual(bedrock_summary_request["tools"], tools)
        self.assertNotIn("tool_choice", bedrock_summary_request)

    def test_native_action_tool_call_is_fail_closed(self):
        agent = self._agent(claim_enabled=True)
        malformed = SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    id="call-1",
                    type="function",
                    function=SimpleNamespace(
                        name="minecraft_action",
                        arguments='{"type":"exec","content":42}',
                    ),
                )
            ]
        )
        action, error = agent._parse_action_tool_calls(malformed)
        self.assertIsNone(action)
        self.assertEqual(error, "content must be a string")

        duplicate = SimpleNamespace(tool_calls=malformed.tool_calls * 2)
        action, error = agent._parse_action_tool_calls(duplicate)
        self.assertIsNone(action)
        self.assertIn("exactly one tool call", error)

        missing_id = SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    id=None,
                    type="function",
                    function=SimpleNamespace(
                        name="minecraft_action",
                        arguments='{"type":"skip"}',
                    ),
                )
            ]
        )
        action, error = agent._parse_action_tool_calls(missing_id)
        self.assertIsNone(action)
        self.assertEqual(error, "tool call id must be a non-empty string")

    def test_native_tool_result_closes_provider_history(self):
        agent = self._agent(claim_enabled=True)
        agent.messages_lock = threading.RLock()
        agent.conversation_history = []
        agent.full_message_history = []
        agent.message_revision = 0
        message = SimpleNamespace(
            tool_calls=[SimpleNamespace(id="call-1")]
        )
        agent._append_action_tool_results(message, accepted=True)
        stored = agent.conversation_history[0]
        self.assertEqual(stored["role"], "tool")
        self.assertEqual(stored["tool_call_id"], "call-1")
        self.assertEqual(json.loads(stored["content"]), {"status": "received"})

    def test_native_sdk_tool_call_history_normalizes_null_content_for_next_turn(self):
        agent = self._agent(claim_enabled=True)
        agent.action_protocol = "tool_calls"
        agent.messages_lock = threading.RLock()
        agent.conversation_history = []
        agent.full_message_history = []
        agent.message_revision = 0
        message = ChatCompletionMessage.model_validate(
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "reasoning-marker",
                "tool_calls": [
                    {
                        "id": "call-sdk-1",
                        "type": "function",
                        "function": {
                            "name": "minecraft_action",
                            "arguments": '{"type":"skip"}',
                        },
                    }
                ],
            }
        )
        sdk_record = message.model_dump()
        self.assertIsNone(sdk_record["content"])
        original_reasoning = sdk_record["reasoning_content"]
        original_tool_calls = sdk_record["tool_calls"]

        stored_record = agent._normalize_assistant_tool_call_history(sdk_record)
        agent._smart_append_msg(stored_record)
        agent._append_action_tool_results(message, accepted=True)
        agent._smart_append_msg({"role": "user", "content": "next turn"})

        next_turn_messages = [
            agent._sanitize_message_for_llm(item)
            for item in agent.conversation_history
        ]
        replayed_assistant = next_turn_messages[0]
        self.assertEqual(replayed_assistant["content"], "")
        self.assertEqual(replayed_assistant["reasoning_content"], original_reasoning)
        self.assertEqual(replayed_assistant["tool_calls"], original_tool_calls)
        self.assertEqual(next_turn_messages[1]["role"], "tool")
        self.assertEqual(next_turn_messages[1]["tool_call_id"], "call-sdk-1")

        # Repair histories written before insertion-time normalization too.
        legacy_replay = agent._sanitize_message_for_llm(sdk_record)
        self.assertEqual(legacy_replay["content"], "")
        self.assertEqual(legacy_replay["reasoning_content"], original_reasoning)
        self.assertEqual(legacy_replay["tool_calls"], original_tool_calls)

    def test_responses_native_tool_call_maps_to_existing_action_protocol(self):
        agent = self._agent(claim_enabled=True)
        agent.api_protocol = "responses"
        agent.action_protocol = "tool_calls"
        response_payload = {
            "id": "resp-1",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "summary": [],
                    "content": None,
                    "encrypted_content": None,
                },
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "minecraft_action",
                    "arguments": '{"type":"skip"}',
                    "status": "completed",
                },
            ],
        }
        response = SimpleNamespace(
            output_text="",
            model_dump=lambda **_kwargs: response_payload,
        )

        message = agent._assistant_message_from_response(response)
        action, error = agent._parse_action_tool_calls(message)

        self.assertEqual(error, "")
        self.assertEqual(action.type, "skip")
        self.assertEqual(action.action_id, "call-1")
        self.assertEqual(message.mcbots_response_id, "resp-1")
        self.assertEqual(message.tool_calls[0]["mcbots_responses_item_id"], "fc-1")

    def test_responses_history_replays_reasoning_call_and_tool_output(self):
        agent = self._agent(claim_enabled=True)
        agent.previous_response_id = None
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": "",
                "responses_api_response": {
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "rs-1",
                            "summary": [],
                            "content": None,
                        },
                        {
                            "type": "function_call",
                            "id": "fc-1",
                            "call_id": "call-1",
                            "name": "minecraft_action",
                            "arguments": '{"type":"skip"}',
                            "status": "completed",
                        },
                    ]
                },
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"status":"received"}',
            },
            {"role": "user", "content": "next"},
        ]

        prepared = agent._prepare_responses_input(
            messages,
            use_stored_boundary=False,
        )

        self.assertEqual(
            [item.get("type", item.get("role")) for item in prepared],
            ["system", "reasoning", "function_call", "function_call_output", "user"],
        )
        self.assertNotIn("content", prepared[1])
        self.assertEqual(prepared[2]["call_id"], "call-1")
        self.assertEqual(prepared[3]["call_id"], "call-1")

    def test_responses_zdr_falls_back_to_stateless_full_history(self):
        agent = self._agent(claim_enabled=True)
        agent.model = "openai/openai/gpt-5.6-luna"
        agent.previous_response_id = "resp-1"
        agent.responses_previous_response_id_supported = True
        agent.llm_request_total = 2
        agent.llm_429_max_retries = 0
        agent.llm_request_gate = None
        response = SimpleNamespace(id="resp-2")
        agent.client = SimpleNamespace(
            responses=SimpleNamespace(
                create=MagicMock(
                    side_effect=[
                        RuntimeError(
                            "HTTP 400 unsupported_parameter previous_response_id: "
                            "Previous response cannot be used for this organization "
                            "due to Zero Data Retention."
                        ),
                        response,
                    ]
                )
            )
        )
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": "previous answer",
                "mcbots_response_id": "resp-1",
            },
            {"role": "user", "content": "next"},
        ]

        actual = agent._create_responses_completion(
            {
                "model": agent.model,
                "messages": messages,
                "tools": agent._action_tools(),
            },
            continue_chain=True,
        )

        self.assertIs(actual, response)
        self.assertFalse(agent.responses_previous_response_id_supported)
        self.assertIsNone(agent.previous_response_id)
        calls = agent.client.responses.create.call_args_list
        self.assertEqual(len(calls), 2)
        first_request = calls[0].kwargs
        self.assertEqual(first_request["previous_response_id"], "resp-1")
        self.assertEqual(first_request["input"], [{"role": "user", "content": "next"}])
        second_request = calls[1].kwargs
        self.assertNotIn("previous_response_id", second_request)
        self.assertFalse(second_request["store"])
        self.assertEqual(
            [item.get("role") for item in second_request["input"]],
            ["system", "assistant", "user"],
        )
        self.assertEqual(second_request["tools"][0]["name"], "minecraft_action")
        self.assertNotIn("function", second_request["tools"][0])

    def test_consecutive_llm_failure_limit_resets_after_success(self):
        agent = Agent.__new__(Agent)
        agent.max_llm_request_successes = 0
        agent.max_llm_request_failures = 0
        agent.max_consecutive_llm_failures = 3
        agent.llm_request_success = 0
        agent.llm_request_failed = 3
        agent.consecutive_llm_failures = 3
        self.assertTrue(agent._has_reached_llm_limit())
        agent.consecutive_llm_failures = 0
        self.assertFalse(agent._has_reached_llm_limit())

    def test_bash_timeout_clears_pending_action_and_notifies_model(self):
        agent = Agent.__new__(Agent)
        agent.action_protocol = "tool_calls"
        agent.current_exec_action = {
            "id": "deadbeef",
            "start_ts": 1.0,
            "start_event_emitted": True,
            "finished": False,
        }
        agent.pending_exec_result = True
        agent._arm_post_action_image_gate = MagicMock()
        agent._append_action_event = MagicMock()
        agent._clear_paused_observe_after_snapshot = MagicMock()
        agent._capture_post_exec_snapshot = MagicMock()
        agent._mark_action_observation = MagicMock()
        agent._append_or_stage_observation = MagicMock(return_value=True)
        agent._save_messages = MagicMock()
        agent._inject_pending_summary_request = MagicMock()
        agent._format_timestamp = MagicMock(return_value="now")
        state = SimpleNamespace(
            command="sleep 999",
            command_status="timed_out",
            timestamp=2.0,
            stdout=None,
            stderr="Command timed out after 300 seconds.",
            exit_code=124,
            action_id="deadbeef",
            task_id="task-1",
        )

        agent._insert_command_result_message(state)

        self.assertFalse(agent.pending_exec_result)
        self.assertIsNone(agent.current_exec_action)
        agent._clear_paused_observe_after_snapshot.assert_called_once_with()
        agent._append_action_event.assert_called_once_with(
            "Action Timeout",
            2.0,
            action_id="deadbeef",
        )
        message = agent._append_or_stage_observation.call_args.args[0]
        self.assertIn("Command timed out after 300 seconds", message["content"][0]["text"])
        self.assertIn("Exit code: 124", message["content"][0]["text"])
        self.assertIn("Tool call ID: deadbeef", message["content"][0]["text"])
        self.assertNotIn("Command: sleep 999", message["content"][0]["text"])
        self.assertEqual(message["mcbots_action_id"], "deadbeef")
        self.assertEqual(message["mcbots_remote_task_id"], "task-1")
        self.assertTrue(message["mcbots_matches_current_action"])

    def test_stale_command_result_cannot_finish_current_action(self):
        agent = Agent.__new__(Agent)
        agent.action_protocol = "tool_calls"
        agent.current_exec_action = {
            "id": "call-new",
            "start_ts": 10.0,
            "start_event_emitted": True,
            "finished": False,
        }
        agent.pending_exec_result = True
        agent._arm_post_action_image_gate = MagicMock()
        agent._append_action_event = MagicMock()
        agent._clear_paused_observe_after_snapshot = MagicMock()
        agent._capture_post_exec_snapshot = MagicMock()
        agent._mark_action_observation = MagicMock()
        agent._append_or_stage_observation = MagicMock(return_value=True)
        agent._save_messages = MagicMock()
        agent._inject_pending_summary_request = MagicMock()
        agent._format_timestamp = MagicMock(return_value="now")
        state = SimpleNamespace(
            command="mcapi press MOVE_FORWARD 30",
            command_status="stopped",
            timestamp=11.0,
            stdout=None,
            stderr="terminated",
            exit_code=-15,
            action_id="call-old",
            task_id="task-old",
        )

        agent._insert_command_result_message(state)

        self.assertEqual(agent.current_exec_action["id"], "call-new")
        self.assertTrue(agent.pending_exec_result)
        agent._append_action_event.assert_not_called()
        agent._clear_paused_observe_after_snapshot.assert_not_called()
        agent._capture_post_exec_snapshot.assert_not_called()
        agent._mark_action_observation.assert_not_called()
        message = agent._append_or_stage_observation.call_args.args[0]
        self.assertEqual(message["mcbots_action_id"], "call-old")
        self.assertFalse(message["mcbots_matches_current_action"])
        result_text = message["content"][0]["text"]
        self.assertIn("[Earlier Interrupted Command Result at now]", result_text)
        self.assertIn("does not finish the current command", result_text)
        self.assertIn("Tool call ID: call-old", result_text)
        self.assertNotIn("Command: mcapi press MOVE_FORWARD 30", result_text)

    def test_xml_command_result_retains_command_without_tool_call_id(self):
        agent = Agent.__new__(Agent)
        agent.action_protocol = "xml"
        agent.current_exec_action = None
        agent.pending_exec_result = False
        agent._append_or_stage_observation = MagicMock(return_value=True)
        agent._save_messages = MagicMock()
        agent._inject_pending_summary_request = MagicMock()
        agent._format_timestamp = MagicMock(return_value="now")
        state = SimpleNamespace(
            command="mcapi state",
            command_status="completed",
            timestamp=2.0,
            stdout="ok",
            stderr=None,
            exit_code=0,
            action_id="legacy-action",
            task_id="task-legacy",
        )

        agent._insert_command_result_message(state)

        result_text = agent._append_or_stage_observation.call_args.args[0]["content"][0]["text"]
        self.assertIn("Command: mcapi state", result_text)
        self.assertNotIn("Tool call ID:", result_text)

    def test_tool_call_action_event_exposes_id_and_keeps_local_metadata(self):
        agent = Agent.__new__(Agent)
        agent.action_protocol = "tool_calls"
        agent._format_timestamp = MagicMock(return_value="now")
        agent._append_or_stage_observation = MagicMock(return_value=True)

        agent._append_action_event(
            "Action Start",
            2.0,
            action_id="call-current",
            related_action_id="call-related",
        )

        message = agent._append_or_stage_observation.call_args.args[0]
        self.assertEqual(
            message["content"][0]["text"],
            "[Action Event at now] Action Start\n"
            "Tool call ID: call-current\n"
            "Related tool call ID: call-related\n",
        )
        self.assertEqual(message["mcbots_action_id"], "call-current")
        self.assertEqual(message["mcbots_related_action_id"], "call-related")
        sanitized = Agent._sanitize_message_for_llm(message)
        self.assertIn("Tool call ID: call-current", sanitized["content"][0]["text"])
        self.assertNotIn("mcbots_action_id", sanitized)
        self.assertNotIn("mcbots_related_action_id", sanitized)

    def test_xml_action_events_match_original_lifecycle_format(self):
        agent = Agent.__new__(Agent)
        agent.action_protocol = "xml"
        agent._format_timestamp = MagicMock(return_value="now")
        agent._append_or_stage_observation = MagicMock(return_value=True)

        for text, related, expected in (
            ("Action Start", None, "Action deadbeef Start"),
            ("Action End (exit_code=0)", None, "Action deadbeef End (exit_code=0)"),
            ("Action Timeout", None, "Action deadbeef Timeout"),
            ("Action Interrupted because a new `exec` action is starting", "1234abcd",
             "Action deadbeef Interrupted (by Action 1234abcd)"),
        ):
            with self.subTest(event=text):
                agent._append_action_event(text, 2.0, action_id="deadbeef", related_action_id=related)
                message = agent._append_or_stage_observation.call_args.args[0]
                self.assertEqual(message["content"][0]["text"], f"[Action Event at now] {expected}\n")
                self.assertEqual(message["mcbots_action_id"], "deadbeef")

    def test_mcapi_state_detection_handles_compound_shell_commands(self):
        state_commands = (
            "mcapi state",
            "mcapi look --yaw 180 --pitch 0 --mode absolute && mcapi state",
            "mcapi press MOVE_FORWARD 2; sleep 0.2; mcapi state",
            "xdo key Escape && mcapi state",
        )
        for command in state_commands:
            with self.subTest(command=command):
                self.assertTrue(Agent._command_requests_mcapi_state(command))

        non_state_commands = (
            "mcapi look --yaw 180 --pitch 0 --mode absolute",
            'mcapi chat "the text mcapi state is not a command"',
        )
        for command in non_state_commands:
            with self.subTest(command=command):
                self.assertFalse(Agent._command_requests_mcapi_state(command))

    def test_compound_mcapi_state_result_is_inserted(self):
        agent = Agent.__new__(Agent)
        agent.current_exec_action = None
        agent._format_timestamp = MagicMock(return_value="now")
        agent._mark_action_observation = MagicMock()
        agent._append_or_stage_observation = MagicMock(return_value=True)
        agent._save_messages = MagicMock()
        agent._inject_pending_summary_request = MagicMock()
        state = SimpleNamespace(
            command=(
                "mcapi look --yaw 180 --pitch 0 --mode absolute "
                "&& mcapi state"
            ),
            stdout='{"data":{"position":{"x":1,"y":2,"z":3}}}',
            stderr=None,
            exit_code=0,
            timestamp=2.0,
        )

        agent._insert_command_result_message(state)

        message = agent._append_or_stage_observation.call_args.args[0]
        text = message["content"][0]["text"]
        self.assertIn("[Command Result at now]", text)
        self.assertIn("mcapi state", text)
        self.assertIn('"position"', text)
        agent._save_messages.assert_called_once_with()

    def test_silent_successful_mcapi_action_result_remains_suppressed(self):
        agent = Agent.__new__(Agent)
        agent.current_exec_action = None
        agent._append_or_stage_observation = MagicMock()
        agent._inject_pending_summary_request = MagicMock()
        state = SimpleNamespace(
            command="mcapi press MOVE_FORWARD 2",
            stdout=None,
            stderr=None,
            exit_code=0,
            timestamp=2.0,
        )

        agent._insert_command_result_message(state)

        agent._append_or_stage_observation.assert_not_called()
        agent._inject_pending_summary_request.assert_called_once_with()

    def test_mcapi_query_and_compound_outputs_are_preserved(self):
        # These commands lost their output in the live Muse smoke despite
        # completing successfully. CLI action acknowledgements are already silent.
        cases = (
            ("mcapi endpoint", '{"host":"127.0.0.1"}', None),
            ("mcapi health", '{"status":"ok"}', None),
            ("mcapi --help", "usage: mcapi ...", None),
            ("mcapi --verbose health; mcapi endpoint; ls -la /workspace", "health and directory output", None),
            ("mcapi endpoint\necho DONE", "endpoint\nDONE", None),
            ("mcapi press MOVE_FORWARD 2; echo DONE", "DONE", None),
            ("mcapi look --yaw 10; true", None, "first command failed"),
        )
        for command, stdout, stderr in cases:
            with self.subTest(command=command):
                agent = Agent.__new__(Agent)
                agent.current_exec_action = None
                agent._format_timestamp = MagicMock(return_value="now")
                agent._append_or_stage_observation = MagicMock(return_value=True)
                agent._save_messages = MagicMock()
                agent._inject_pending_summary_request = MagicMock()
                state = SimpleNamespace(command=command, stdout=stdout, stderr=stderr,
                                        exit_code=0, timestamp=2.0)
                agent._insert_command_result_message(state)
                message = agent._append_or_stage_observation.call_args.args[0]
                text = message["content"][0]["text"]
                self.assertIn(stdout or stderr, text)
                self.assertIn("Exit code: 0", text)

if __name__ == "__main__":
    unittest.main()
