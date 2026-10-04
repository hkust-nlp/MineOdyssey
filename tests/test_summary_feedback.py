"""Regression tests for unread feedback at summary boundaries (no provider/game)."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_auto_summarize import _build_agent, _FakeResponse


def text_messages(messages):
    return "\n".join(
        msg["content"] if isinstance(msg.get("content"), str) else
        "\n".join(block.get("text", "") for block in msg.get("content", [])
                  if isinstance(block, dict))
        for msg in messages
    )


def make_agent():
    agent = _build_agent()
    agent.conversation_history = [{"role": "system", "content": "system"}]
    agent.action_sender = MagicMock()
    agent._clear_paused_observe_after_snapshot = MagicMock()
    agent._capture_post_exec_snapshot = MagicMock()
    agent._mark_action_observation = MagicMock()
    agent.committed_image_seq = 0
    agent.context_reset_count = 0
    agent.previous_response_id = "old-response"
    return agent


def command_result(action_id="active"):
    return SimpleNamespace(
        command="echo ENDPOINT_STATE",
        stdout="ENDPOINT_STATE x=12 y=4 z=8",
        stderr="BLOCKED_DOOR",
        exit_code=1,
        timestamp=101.0,
        action_id=action_id,
        task_id="remote-task",
    )


class SummaryFeedbackTests(unittest.TestCase):
    def test_decision_loop_waits_for_stop_output_before_sending_summary(self):
        agent = make_agent()
        for key, value in {
            "message_revision": 1, "committed_image_seq": 1,
            "pending_exec_result": True, "pending_exec_observe_deadline": 0.0,
            "pending_post_exec_image": False, "pending_paused_observe_after_deadline": 0.0,
            "pending_paused_observe_after_action_id": None,
            "post_exec_result_ts": 0.0, "post_exec_seen_frames": 0,
            "post_exec_kept_frames": 0, "post_exec_last_dropped_bytes": None,
            "post_exec_last_dropped_ts": 0.0, "default_observe_after_sec": 2.0,
            "running": True, "model": "test-model", "sampling_params": {},
            "request_extra_body": None, "llm_request_total": 0,
            "llm_request_success": 0, "llm_request_failed": 0,
            "max_llm_request_successes": 1, "max_llm_request_failures": 1,
            "last_llm_request_sent_ts": 0.0, "api_protocol": "chat_completions",
        }.items():
            setattr(agent, key, value)
        for method, result in [
            ("_capture_paused_observe_after_snapshot_if_due", False),
            ("_pending_paused_observe_after_deadline", 0.0),
            ("_append_fallback_post_exec_screenshot", True),
            ("_capture_post_skip_snapshot", None),
            ("_capture_post_stop_execute_snapshot", None),
            ("_capture_pre_action_snapshot", None),
        ]:
            setattr(agent, method, MagicMock(return_value=result))
        agent.current_exec_action = {
            "id": "active", "finished": False, "start_ts": 100.0,
            "start_event_emitted": True,
        }
        agent._queue_summary_request(50, reason="image_limit")
        delivered = False
        requests = []

        def drain():
            nonlocal delivered
            if agent.action_sender.called and not delivered:
                # The stop is asynchronous: no model request may overtake it.
                self.assertEqual(requests, [])
                delivered = True
                agent._insert_command_result_message(command_result())
                return 1
            return 0

        def respond(req):
            requests.append(copy.deepcopy(req["messages"]))
            return _FakeResponse("summary after stop" if len(requests) == 1
                                 else "<action><type>skip</type></action>")

        agent._drain_new_states = MagicMock(side_effect=drain)
        agent._create_chat_completion = MagicMock(side_effect=respond)
        with patch("agent.agent.time.sleep", lambda *_: None):
            thread = threading.Thread(target=agent._decision_loop, daemon=True)
            thread.start()
            thread.join(timeout=2)
            if thread.is_alive():
                agent.running = False
                thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(delivered)
        self.assertEqual(len(requests), 2)
        sent = text_messages(requests[0])
        self.assertLess(sent.index("ENDPOINT_STATE"), sent.index("[Summary Request"))
        self.assertEqual([c.args[0].type for c in agent.action_sender.call_args_list],
                         ["stop_execute", "skip"])

    def test_evaluator_arrival_during_summary_is_also_replayed(self):
        agent = make_agent()
        agent.awaiting_summary = True
        agent.navigation_claim_client = MagicMock()
        agent.navigation_claim_client.read_events.return_value = ["FIRST_WAYPOINT_CONFIRMED"]
        self.assertEqual(agent._drain_navigation_events(), 1)
        self.assertNotIn("FIRST_WAYPOINT_CONFIRMED", text_messages(agent.conversation_history))
        agent._perform_summary_reset("First waypoint not confirmed yet.")
        texts = text_messages(agent.conversation_history)
        self.assertLess(texts.index("not confirmed yet"), texts.index("FIRST_WAYPOINT_CONFIRMED"))
        self.assertEqual(texts.count("FIRST_WAYPOINT_CONFIRMED"), 1)

    def test_unread_command_result_survives_summary_with_stdout_stderr(self):
        agent = make_agent()
        agent._inject_summary_request(50, reason="image_limit")
        request = copy.deepcopy(agent.conversation_history)
        agent.llm_inflight = True
        agent.current_exec_action = {
            "id": "active", "finished": False, "start_ts": 100.0,
            "start_event_emitted": True,
        }
        agent.pending_exec_result = True
        agent._insert_command_result_message(command_result())
        self.assertNotIn("ENDPOINT_STATE", text_messages(request))

        agent.llm_inflight = False
        agent._perform_summary_reset("Earlier position, before the action ended.")
        outgoing = [agent._sanitize_message_for_llm(m) for m in agent.conversation_history]
        texts = text_messages(outgoing)
        self.assertIn("ENDPOINT_STATE x=12 y=4 z=8", texts)
        self.assertIn("BLOCKED_DOOR", texts)
        self.assertIn("Exit code: 1", texts)
        self.assertLess(texts.index("Earlier position"), texts.index("ENDPOINT_STATE"))
        self.assertEqual(texts.count("ENDPOINT_STATE x=12 y=4 z=8"), 1)
        self.assertEqual(text_messages(agent.full_message_history).count("ENDPOINT_STATE x=12 y=4 z=8"), 1)
        self.assertFalse(agent.staging_buffer)
        self.assertFalse(any(m.get("mcbots_staged_dropped_by_summary_reset") for m in agent.full_message_history))
        self.assertIsNone(agent.previous_response_id)
        self.assertFalse(agent.pending_exec_result)

    def test_replay_orders_messages_and_commits_images_once(self):
        agent = make_agent()
        agent.awaiting_summary = True
        image = {"role": "user", "content": [
            {"type": "text", "text": "NEW_FRAME"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}
        agent._append_or_stage_observation(image, 12.0)
        agent._append_or_stage_observation({"role": "user", "content": "EARLIER_RESULT"}, 11.0)
        agent._append_or_stage_observation({"role": "user", "content": "SAME_TIME_RESULT"}, 12.0)
        agent._perform_summary_reset("memory")
        outgoing = text_messages(agent.conversation_history)
        self.assertLess(outgoing.index("EARLIER_RESULT"), outgoing.index("NEW_FRAME"))
        self.assertLess(outgoing.index("NEW_FRAME"), outgoing.index("SAME_TIME_RESULT"))
        self.assertEqual(agent.committed_image_seq, 1)
        agent._flush_staging_buffer_locked(False)
        self.assertEqual(agent.committed_image_seq, 1)
        self.assertEqual(text_messages(agent.full_message_history).count("NEW_FRAME"), 1)
        marker = next(m for m in agent.conversation_history if m.get("mcbots_summary_reset"))
        self.assertEqual(marker["mcbots_summary_staged_recovered"], 3)

    def test_failed_summary_hard_reset_keeps_unread_results(self):
        agent = make_agent()
        agent.awaiting_summary = True
        agent._append_or_stage_observation(
            {"role": "user", "content": "UNREAD_AFTER_FAILED_SUMMARY"}, 11.0)
        agent._reset_context_after_llm_request_failure(RuntimeError("context overflow"))
        self.assertIn("UNREAD_AFTER_FAILED_SUMMARY", text_messages(agent.conversation_history))
        self.assertEqual(text_messages(agent.full_message_history).count("UNREAD_AFTER_FAILED_SUMMARY"), 1)
        self.assertFalse(agent.awaiting_summary)
        self.assertFalse(agent.staging_buffer)

    def test_pending_summary_stops_once_and_waits_for_terminal_result(self):
        agent = make_agent()
        agent.current_exec_action = {
            "id": "active", "finished": False, "start_ts": 100.0,
            "start_event_emitted": True,
        }
        agent.pending_exec_result = True
        agent._queue_summary_request(50, reason="image_limit")
        self.assertFalse(agent._inject_pending_summary_request())
        self.assertFalse(agent._inject_pending_summary_request())
        self.assertFalse(agent.awaiting_summary)
        self.assertEqual(agent.action_sender.call_count, 1)
        self.assertEqual(agent.action_sender.call_args.args[0].type, "stop_execute")

        agent._insert_command_result_message(command_result())
        self.assertTrue(agent.awaiting_summary)
        texts = text_messages(agent.conversation_history)
        self.assertLess(texts.index("ENDPOINT_STATE"), texts.index("[Summary Request"))
        self.assertIsNone(agent.pending_auto_summarize_total_tokens)

    def test_post_exec_snapshot_cannot_freeze_context_before_stdout_is_inserted(self):
        agent = make_agent()
        agent.current_exec_action = {
            "id": "active", "finished": False, "start_ts": 100.0,
            "start_event_emitted": True,
        }
        agent.pending_exec_result = True
        agent._queue_summary_request(50, reason="image_limit")
        checks = []
        agent._capture_post_exec_snapshot.side_effect = lambda **kwargs: checks.append(
            agent._inject_pending_summary_request())
        agent._insert_command_result_message(command_result())
        self.assertEqual(checks, [False])
        texts = text_messages(agent.conversation_history)
        self.assertLess(texts.index("ENDPOINT_STATE"), texts.index("[Summary Request"))

    def test_summary_is_not_injected_into_an_inflight_navigation_request(self):
        agent = make_agent()
        agent.llm_inflight = True
        agent._queue_summary_request(50, reason="image_limit")
        self.assertFalse(agent._inject_pending_summary_request())
        self.assertFalse(agent.awaiting_summary)
        agent.llm_inflight = False
        self.assertTrue(agent._inject_pending_summary_request())


if __name__ == "__main__":
    unittest.main()
