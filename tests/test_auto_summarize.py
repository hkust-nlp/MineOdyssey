"""Unit tests for auto-summarize (token-threshold driven) in Agent.

Covers:
- Token extraction from usage objects (OpenAI-style and dict shapes)
- Threshold gating logic (enabled/disabled, awaiting flag, missing usage)
- _inject_summary_request state machine: conversation/full_history append, revision bump, flag flip
- _perform_summary_reset context rebuild: system preserved, eval pin preserved, summary landed as user msg, flag cleared
"""

import copy
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.agent import Action, Agent


def _build_agent(
    *,
    threshold: int = 10000,
    awaiting: bool = False,
    eval_mode: bool = False,
    initial_user_message: str = "",
    observe_enabled: bool = True,
    auto_summary_profile: str = "general",
):
    """Build a minimal Agent via object.__new__ with only the fields the tested
    methods touch. Disk I/O and capture methods are stubbed via MagicMock."""
    agent = object.__new__(Agent)
    agent.messages_lock = threading.RLock()
    agent.observe_lock = threading.Lock()

    agent.conversation_history = []
    agent.full_message_history = []
    agent.staging_buffer = []
    agent._staging_seq = 0
    agent.message_revision = 0

    agent.auto_summarize_token_threshold = threshold
    agent.auto_summarize_turn_threshold = 0
    agent.auto_summary_profile = auto_summary_profile
    agent.awaiting_summary = awaiting
    agent.last_prompt_tokens = 0
    agent.last_total_tokens = 0
    agent.summarize_count = 0
    agent.pending_auto_summarize_total_tokens = None
    agent.pending_auto_summarize_reason = None
    agent.max_images_in_context = 0  # disabled for summary tests; image-limit path not exercised

    agent.eval_mode = eval_mode
    agent.initial_user_message = initial_user_message
    agent._initial_user_message_id = "init-xyz" if initial_user_message else ""

    agent.observe_enabled = observe_enabled
    agent.last_exec_action_content = None
    agent.last_exec_action_ts = 0.0
    agent.record_dir = None
    agent.llm_inflight = False
    agent.current_exec_action = None
    agent.pending_exec_result = False
    agent.api_protocol = "chat_completions"
    agent.action_protocol = "xml"
    agent.previous_response_id = None
    agent.allow_model_observe_toggle = False

    # ROLL window/episode notifications: test default is disabled (no URL).
    agent.roll_notify_url = None

    # Self-reward: per-window grading — tests don't exercise it; keep disabled.
    agent.enable_self_reward = False
    agent.self_reward_in_flight = False
    agent.self_reward_window_count = 0
    agent.self_reward_history = []
    agent.state_snapshot_provider = None

    # Stub out side-effecting methods we don't want to exercise here.
    agent._save_messages = MagicMock()
    agent._capture_post_reset_snapshot = MagicMock()
    agent._capture_post_stop_execute_snapshot = MagicMock()
    agent._clear_paused_observe_after_snapshot = MagicMock()
    agent._arm_post_action_image_gate = MagicMock()

    return agent


def _make_usage_obj(prompt_tokens=0, completion_tokens=0, total_tokens=None):
    """Mimic OpenAI's CompletionUsage object shape (attribute access)."""
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


class _FakeAssistantMessage:
    def __init__(self, content: str):
        self.content = content
        self.usage = None

    def model_dump(self):
        payload = {
            "role": "assistant",
            "content": self.content,
        }
        if self.usage is not None:
            payload["usage"] = self.usage
        return payload


class _FakeResponse:
    def __init__(self, content: str, usage=None):
        self.choices = [SimpleNamespace(message=_FakeAssistantMessage(content))]
        self.usage = usage


class TestExtractTotalTokens(unittest.TestCase):
    def setUp(self):
        self.agent = _build_agent()

    def test_none_usage_returns_none(self):
        self.assertIsNone(self.agent._extract_total_tokens(None))

    def test_obj_with_total_tokens(self):
        usage = _make_usage_obj(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        self.assertEqual(self.agent._extract_total_tokens(usage), 150)

    def test_obj_without_total_sums_prompt_and_completion(self):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50)
        # No total_tokens attribute — falls back to p + c
        self.assertEqual(self.agent._extract_total_tokens(usage), 150)

    def test_dict_with_total_tokens(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        self.assertEqual(self.agent._extract_total_tokens(usage), 150)

    def test_dict_without_total_sums(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        self.assertEqual(self.agent._extract_total_tokens(usage), 150)

    def test_zero_tokens_returns_none(self):
        usage = _make_usage_obj(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        self.assertIsNone(self.agent._extract_total_tokens(usage))


class TestShouldTriggerSummarize(unittest.TestCase):
    def test_threshold_disabled_returns_false(self):
        agent = _build_agent(threshold=0)
        usage = _make_usage_obj(total_tokens=99999)
        self.assertFalse(agent._should_trigger_summarize(usage))

    def test_below_threshold_returns_false(self):
        agent = _build_agent(threshold=1000)
        usage = _make_usage_obj(prompt_tokens=400, completion_tokens=400)  # total 800
        self.assertFalse(agent._should_trigger_summarize(usage))

    def test_at_threshold_returns_false(self):
        """Equal is NOT triggering — uses strict >."""
        agent = _build_agent(threshold=1000)
        usage = _make_usage_obj(total_tokens=1000)
        self.assertFalse(agent._should_trigger_summarize(usage))

    def test_above_threshold_returns_true(self):
        agent = _build_agent(threshold=1000)
        usage = _make_usage_obj(total_tokens=1001)
        self.assertTrue(agent._should_trigger_summarize(usage))

    def test_awaiting_summary_blocks_retrigger(self):
        """While a summary is being awaited, don't retrigger even if tokens very high."""
        agent = _build_agent(threshold=1000, awaiting=True)
        usage = _make_usage_obj(total_tokens=50000)
        self.assertFalse(agent._should_trigger_summarize(usage))

    def test_pending_summary_blocks_retrigger(self):
        agent = _build_agent(threshold=1000)
        agent.pending_auto_summarize_total_tokens = 1500
        usage = _make_usage_obj(total_tokens=50000)
        self.assertFalse(agent._should_trigger_summarize(usage))

    def test_missing_usage_returns_false(self):
        agent = _build_agent(threshold=1000)
        self.assertFalse(agent._should_trigger_summarize(None))

    def test_does_not_gate_on_exec_state(self):
        """Auto-summarize must be orthogonal to exec state (per design)."""
        agent = _build_agent(threshold=1000)
        # Simulate in-flight exec
        agent.current_exec_action = {"id": "a1", "finished": False}
        usage = _make_usage_obj(total_tokens=2000)
        self.assertTrue(agent._should_trigger_summarize(usage))


class TestInjectSummaryRequest(unittest.TestCase):
    def test_appends_to_both_histories(self):
        agent = _build_agent(threshold=1000)
        self.assertEqual(len(agent.conversation_history), 0)
        self.assertEqual(len(agent.full_message_history), 0)

        agent._inject_summary_request(trigger_count=1500)

        self.assertEqual(len(agent.conversation_history), 1)
        self.assertEqual(len(agent.full_message_history), 1)
        self.assertIs(
            agent.conversation_history[0]["role"], "user",
            "summary request message must be user-role",
        )

    def test_sets_awaiting_flag_and_bumps_revision(self):
        agent = _build_agent(threshold=1000)
        agent.message_revision = 5
        self.assertFalse(agent.awaiting_summary)
        agent._inject_summary_request(trigger_count=1500)
        self.assertTrue(agent.awaiting_summary)
        self.assertEqual(agent.message_revision, 6)

    def test_carries_diagnostic_flags(self):
        agent = _build_agent(threshold=1000)
        agent._inject_summary_request(trigger_count=1500)
        msg = agent.conversation_history[0]
        self.assertTrue(msg.get("mcbots_summary_request"))
        self.assertEqual(msg.get("mcbots_summary_reason"), "token_threshold")
        self.assertEqual(msg.get("mcbots_summary_trigger_count"), 1500)
        self.assertEqual(msg.get("mcbots_summary_threshold"), 1000)

    def test_content_mentions_no_action(self):
        """The request must clearly tell the model to not emit an action block."""
        agent = _build_agent(threshold=1000)
        agent._inject_summary_request(trigger_count=1500)
        text = agent.conversation_history[0]["content"][0]["text"]
        self.assertIn("summary", text.lower())
        self.assertIn("action", text.lower())

    def test_navigation_request_uses_tagged_optional_guidance(self):
        agent = _build_agent(threshold=1000, auto_summary_profile="navigation")
        agent._inject_summary_request(trigger_count=50, reason="turn_threshold")
        text = agent.conversation_history[0]["content"][0]["text"]
        self.assertIn("When useful, consider mentioning", text)
        self.assertIn("<summary>...</summary>", text)
        self.assertIn("last complete `<summary>` block", text)
        self.assertNotIn("summary text only", text)

    def test_navigation_extracts_only_last_complete_summary_block(self):
        agent = _build_agent(auto_summary_profile="navigation")
        response = (
            "Some preamble. <summary>old memory</summary> "
            "<action><type>skip</type></action> "
            "Final answer: <summary>current location and next step</summary> trailing text"
        )
        self.assertEqual(
            agent._extract_summary_text(response),
            "current location and next step",
        )

    def test_navigation_rejects_missing_or_malformed_final_summary(self):
        agent = _build_agent(auto_summary_profile="navigation")
        self.assertIsNone(agent._extract_summary_text("plain summary"))
        self.assertIsNone(agent._extract_summary_text("<summary>unfinished"))
        self.assertIsNone(
            agent._extract_summary_text(
                "<summary>valid earlier</summary><summary>unfinished final"
            )
        )
        self.assertIsNone(
            agent._extract_summary_text(
                "<summary><action><type>skip</type></action></summary>"
            )
        )

    def test_calls_save(self):
        agent = _build_agent(threshold=1000)
        agent._inject_summary_request(trigger_count=1500)
        agent._save_messages.assert_called_once()


class TestQueuedSummaryRequest(unittest.TestCase):
    def test_queue_sets_pending_without_awaiting(self):
        agent = _build_agent(threshold=1000)
        agent._queue_summary_request(1500)
        self.assertEqual(agent.pending_auto_summarize_total_tokens, 1500)
        self.assertFalse(agent.awaiting_summary)

    def test_inject_pending_summary_request_clears_queue(self):
        agent = _build_agent(threshold=1000)
        agent._queue_summary_request(1500)
        self.assertTrue(agent._inject_pending_summary_request())
        self.assertIsNone(agent.pending_auto_summarize_total_tokens)
        self.assertTrue(agent.awaiting_summary)

    def test_inflight_request_defers_pending_summary_injection(self):
        agent = _build_agent(threshold=1000)
        agent._queue_summary_request(1500)
        agent.llm_inflight = True

        self.assertFalse(agent._inject_pending_summary_request())
        self.assertEqual(agent.pending_auto_summarize_total_tokens, 1500)
        self.assertFalse(agent.awaiting_summary)
        self.assertFalse(
            any(msg.get("mcbots_summary_request") for msg in agent.conversation_history)
        )

        agent.llm_inflight = False
        self.assertTrue(agent._inject_pending_summary_request())
        self.assertTrue(agent.awaiting_summary)

    def test_action_shaped_summary_is_rejected_without_reset(self):
        agent = _build_agent(threshold=1000, awaiting=True)
        agent.conversation_history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "summarize now"},
        ]
        assistant = _FakeAssistantMessage(
            "<action><type>exec</type><content>mcapi state</content></action>"
        )

        self.assertFalse(agent._is_valid_summary_text(assistant.content))
        agent._reject_invalid_summary_response(assistant, assistant.content)

        self.assertTrue(agent.awaiting_summary)
        self.assertEqual(agent.summarize_count, 0)
        self.assertTrue(agent.conversation_history[-1]["mcbots_invalid_summary_feedback"])
        self.assertTrue(
            any(msg.get("mcbots_invalid_summary_response") for msg in agent.full_message_history)
        )

    def test_forced_summary_barrier_withholds_action_and_stops_active_exec(self):
        agent = _build_agent(threshold=1000)
        agent.action_sender = MagicMock()
        agent.current_exec_action = {
            "id": "old-action",
            "finished": False,
            "start_ts": 1.0,
        }
        withheld = Action(type="exec", content="mcapi press MOVE_FORWARD 30")

        agent._enter_forced_summary_barrier(
            50,
            reason="turn_threshold",
            withheld_action=withheld,
        )

        agent.action_sender.assert_called_once()
        self.assertEqual(agent.action_sender.call_args.args[0].type, "stop_execute")
        self.assertEqual(
            agent.action_sender.call_args.args[0].target_action_id,
            "old-action",
        )
        self.assertEqual(agent.pending_auto_summarize_total_tokens, 50)
        self.assertFalse(agent.awaiting_summary)
        self.assertTrue(agent.conversation_history[-1]["mcbots_summary_barrier"])

    def test_forced_summary_barrier_injects_immediately_when_idle(self):
        agent = _build_agent(threshold=1000)
        agent.action_sender = MagicMock()

        agent._enter_forced_summary_barrier(
            50,
            reason="turn_threshold",
            withheld_action=Action(type="skip"),
        )

        agent.action_sender.assert_not_called()
        agent._capture_post_stop_execute_snapshot.assert_called_once()
        self.assertTrue(agent.awaiting_summary)
        self.assertIsNone(agent.pending_auto_summarize_total_tokens)
        self.assertTrue(agent.conversation_history[-1]["mcbots_summary_request"])

    def test_awaiting_summary_stages_observations_before_summary_turn(self):
        agent = _build_agent(threshold=1000, awaiting=True)
        inserted = agent._append_or_stage_observation(
            {"role": "user", "content": [{"type": "text", "text": "late obs"}]},
            123.0,
        )
        self.assertFalse(inserted)
        self.assertEqual(len(agent.conversation_history), 0)
        self.assertEqual(len(agent.staging_buffer), 1)

    def test_exec_boundary_injects_summary_after_result_messages(self):
        agent = _build_agent(threshold=1000)
        agent.current_exec_action = {
            "id": "a1",
            "start_ts": 100.0,
            "start_event_emitted": True,
            "finished": False,
        }
        agent.pending_exec_result = True
        agent.pending_auto_summarize_total_tokens = 1500

        def _fake_post_exec_snapshot(anchor_ts=None):
            agent._smart_append_msg(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "[Screenshot at 1970-01-01 00:01:41.00] [post-exec snapshot]\n",
                        }
                    ],
                }
            )

        agent._capture_post_exec_snapshot = MagicMock(side_effect=_fake_post_exec_snapshot)

        state = SimpleNamespace(
            command="echo ok",
            stdout="ok\n",
            stderr="",
            exit_code=0,
            timestamp=101.0,
            action_id="a1",
            task_id="task-a1",
        )

        agent._insert_command_result_message(state)

        parts = []
        for msg in agent.conversation_history:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))

        post_exec_idx = next(i for i, text in enumerate(parts) if "[post-exec snapshot]" in text)
        command_result_idx = next(i for i, text in enumerate(parts) if "[Command Result" in text)
        summary_idx = next(i for i, text in enumerate(parts) if "[Summary Request" in text)

        self.assertLess(post_exec_idx, summary_idx)
        self.assertLess(command_result_idx, summary_idx)
        self.assertTrue(agent.awaiting_summary)
        self.assertIsNone(agent.pending_auto_summarize_total_tokens)


class TestPerformSummaryReset(unittest.TestCase):
    def _seed(self, agent, num_msgs=6):
        """Seed conversation_history with system + alternating user/assistant."""
        sys_msg = {"role": "system", "content": "You are a bot."}
        agent.conversation_history = [sys_msg]
        for i in range(num_msgs):
            role = "user" if i % 2 == 0 else "assistant"
            agent.conversation_history.append({"role": role, "content": f"msg-{i}"})

    def test_rebuilds_to_system_plus_summary(self):
        """Non-eval path: new active context = system + summary marker."""
        agent = _build_agent(threshold=1000, eval_mode=False)
        agent.awaiting_summary = True
        self._seed(agent, num_msgs=8)

        agent._perform_summary_reset(summary_text="I mined 3 dirt. Next: chop wood.")

        # 1 system + 1 summary marker
        self.assertEqual(len(agent.conversation_history), 2)
        self.assertEqual(agent.conversation_history[0]["role"], "system")
        marker = agent.conversation_history[1]
        self.assertEqual(marker["role"], "user")
        self.assertTrue(marker.get("mcbots_summary_reset"))
        self.assertEqual(marker.get("mcbots_summary_count"), 1)
        self.assertIn("I mined 3 dirt. Next: chop wood.", marker["content"][0]["text"])

    def test_clears_awaiting_flag_and_increments_count(self):
        agent = _build_agent(threshold=1000)
        agent.awaiting_summary = True
        agent.summarize_count = 2
        self._seed(agent)
        agent._perform_summary_reset(summary_text="x")
        self.assertFalse(agent.awaiting_summary)
        self.assertEqual(agent.summarize_count, 3)

    def test_eval_mode_pins_initial_user_message(self):
        agent = _build_agent(threshold=1000, eval_mode=True, initial_user_message="Mine dirt.")
        sys_msg = {"role": "system", "content": "You are a bot."}
        initial_user = {
            "role": "user",
            "content": "Mine dirt.",
            "mcbots_initial_user_message": True,
            "mcbots_initial_user_message_id": agent._initial_user_message_id,
        }
        agent.conversation_history = [sys_msg, initial_user]
        for i in range(4):
            agent.conversation_history.append({"role": "assistant", "content": f"step-{i}"})

        agent._perform_summary_reset(summary_text="summary")

        # Expect: system, pinned_initial_user, summary_marker
        roles = [m["role"] for m in agent.conversation_history]
        self.assertEqual(roles, ["system", "user", "user"])
        self.assertTrue(agent.conversation_history[1].get("mcbots_initial_user_message"))
        self.assertTrue(agent.conversation_history[2].get("mcbots_summary_reset"))

    def test_marker_contains_observe_mode(self):
        agent = _build_agent(threshold=1000, observe_enabled=False)
        self._seed(agent)
        agent._perform_summary_reset(summary_text="s")
        marker = agent.conversation_history[-1]
        self.assertEqual(marker.get("mcbots_observe_mode"), "event_only")
        self.assertIn("event_only", marker["content"][0]["text"])

    def test_empty_summary_does_not_crash(self):
        agent = _build_agent(threshold=1000)
        self._seed(agent)
        agent._perform_summary_reset(summary_text="")
        # Marker still emitted, with sentinel text
        marker = agent.conversation_history[-1]
        self.assertIn("empty summary", marker["content"][0]["text"])

    def test_full_history_gets_the_marker(self):
        agent = _build_agent(threshold=1000)
        self._seed(agent)
        agent._perform_summary_reset(summary_text="s")
        # full_history should contain the summary marker (archive copy)
        self.assertTrue(
            any(
                isinstance(m, dict) and m.get("mcbots_summary_reset")
                for m in agent.full_message_history
            ),
            "summary reset marker missing from full_message_history",
        )

    def test_staging_buffer_is_replayed_after_summary(self):
        agent = _build_agent(threshold=1000)
        self._seed(agent)
        # Tuple shape is (arrival timestamp, sequence, message).
        fake_msg_a = {"role": "user", "content": [{"type": "text", "text": "staged a"}]}
        fake_msg_b = {"role": "user", "content": [{"type": "text", "text": "staged b"}]}
        agent.staging_buffer = [
            (100.0, 1, fake_msg_a),
            (101.0, 2, fake_msg_b),
        ]

        agent._perform_summary_reset(summary_text="s")

        # Staging drained
        self.assertEqual(len(agent.staging_buffer), 0)
        # Both messages are archived AND visible after the new memory.
        tagged = [
            m for m in agent.full_message_history
            if isinstance(m, dict) and m.get("mcbots_staged_replayed_after_summary_reset")
        ]
        self.assertEqual(len(tagged), 2)
        self.assertEqual(agent.conversation_history[-2:], tagged)

    def test_calls_post_reset_snapshot(self):
        agent = _build_agent(threshold=1000)
        self._seed(agent)
        agent._perform_summary_reset(summary_text="s")
        agent._capture_post_reset_snapshot.assert_called_once()


class TestDecisionLoopIntegration(unittest.TestCase):
    """Light integration: inject request, then simulate the summary response arriving,
    driving through _perform_summary_reset directly. Validates end-to-end state flow."""

    def test_full_cycle(self):
        agent = _build_agent(threshold=1000, eval_mode=False)
        agent.conversation_history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "do stuff"}]},
            {"role": "assistant", "content": "<action><type>skip</type></action>"},
        ]

        # Step 1: threshold exceeded -> inject request
        big_usage = _make_usage_obj(prompt_tokens=900, completion_tokens=200)  # 1100
        self.assertTrue(agent._should_trigger_summarize(big_usage))
        agent._inject_summary_request(trigger_count=1100)

        self.assertTrue(agent.awaiting_summary)
        # Don't retrigger now
        self.assertFalse(agent._should_trigger_summarize(big_usage))

        # Step 2: simulate summary response arriving -> reset
        agent._perform_summary_reset(summary_text="progress so far: dug 3 blocks")

        # Active context is system + summary marker (non-eval mode)
        self.assertEqual(len(agent.conversation_history), 2)
        self.assertEqual(agent.conversation_history[0]["role"], "system")
        marker = agent.conversation_history[1]
        self.assertTrue(marker.get("mcbots_summary_reset"))
        self.assertFalse(agent.awaiting_summary)
        self.assertEqual(agent.summarize_count, 1)
        self.assertIn("dug 3 blocks", marker["content"][0]["text"])

    def test_summary_reset_triggers_followup_decision(self):
        """After consuming a summary reply, the rebuilt context must trigger one
        immediate follow-up LLM turn instead of stalling at the reset marker."""
        agent = _build_agent(threshold=1000, eval_mode=False)
        agent.conversation_history = [{"role": "system", "content": "sys"}]
        agent.full_message_history = [{"role": "system", "content": "sys"}]
        agent.message_revision = 1

        agent.committed_image_seq = 1
        agent.pending_exec_result = False
        agent.pending_exec_observe_deadline = 0.0
        agent.pending_post_exec_image = False
        agent.pending_paused_observe_after_deadline = 0.0
        agent.pending_paused_observe_after_action_id = None
        agent.current_exec_action = None
        agent.post_exec_result_ts = 0.0
        agent.post_exec_seen_frames = 0
        agent.post_exec_kept_frames = 0
        agent.post_exec_last_dropped_bytes = None
        agent.post_exec_last_dropped_ts = 0.0
        agent.default_observe_after_sec = 2.0

        agent.running = True
        agent.model = "test-model"
        agent.sampling_params = {}
        agent.request_extra_body = None
        agent.llm_inflight = False
        agent.llm_request_total = 0
        agent.llm_request_success = 0
        agent.llm_request_failed = 0
        agent.max_llm_request_successes = 2
        agent.max_llm_request_failures = 0

        agent.action_sender = MagicMock()
        agent._drain_new_states = MagicMock(return_value=0)
        agent._flush_staging_buffer_locked = MagicMock(return_value=0)
        agent._capture_paused_observe_after_snapshot_if_due = MagicMock(return_value=False)
        agent._pending_paused_observe_after_deadline = MagicMock(return_value=0.0)
        agent._append_fallback_post_exec_screenshot = MagicMock(return_value=True)
        agent._arm_post_action_image_gate = MagicMock()
        agent._capture_post_skip_snapshot = MagicMock()
        agent._capture_post_stop_execute_snapshot = MagicMock()
        agent._emit_observe_mode_notice = MagicMock()
        agent._clear_paused_observe_after_snapshot = MagicMock()
        agent._capture_pre_action_snapshot = MagicMock()
        agent.last_llm_request_sent_ts = 0.0

        agent._inject_summary_request(trigger_count=1500)

        responses = iter(
            [
                _FakeResponse("summary so far: opened F3 and checked the scene"),
                _FakeResponse("<action><type>skip</type></action>"),
            ]
        )
        requests = []

        def respond(req):
            requests.append(copy.deepcopy(req["messages"]))
            if len(requests) == 1:
                # Arrives while the summary HTTP request is in flight.
                agent._append_or_stage_observation(
                    {"role": "user", "content": "UNREAD_STATE_AFTER_SUMMARY_REQUEST"},
                    123.0,
                )
            return next(responses)

        agent._create_chat_completion = MagicMock(side_effect=respond)

        with patch("agent.agent.time.sleep", lambda *_: None):
            thread = threading.Thread(target=agent._decision_loop, daemon=True)
            thread.start()
            thread.join(timeout=1.0)
            if thread.is_alive():
                agent.running = False
                thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive(), "decision loop should terminate in the test harness")
        self.assertEqual(
            agent._create_chat_completion.call_count,
            2,
            "summary reset should trigger a fresh follow-up LLM turn",
        )
        self.assertFalse(agent.awaiting_summary)
        self.assertNotIn("UNREAD_STATE_AFTER_SUMMARY_REQUEST", str(requests[0]))
        self.assertIn("UNREAD_STATE_AFTER_SUMMARY_REQUEST", str(requests[1]))
        self.assertEqual(
            agent.llm_request_success,
            1,
            "the summary response must not consume an assistant decision step",
        )
        self.assertTrue(
            any(
                isinstance(m, dict) and m.get("mcbots_summary_reset")
                for m in agent.conversation_history
            )
        )
        agent.action_sender.assert_called_once()
        self.assertEqual(agent.action_sender.call_args[0][0].type, "skip")


if __name__ == "__main__":
    unittest.main()
