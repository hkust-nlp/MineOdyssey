"""Unit tests for image-limit summary triggering in Agent."""

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.agent import Agent


def _make_image_msg(role="user", num_images=1, tag=""):
    """Create a message with the given number of image_url items."""
    content = []
    if tag:
        content.append({"type": "text", "text": tag})
    for _ in range(num_images):
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,AAAA"}
        })
    return {"role": role, "content": content}


def _make_text_msg(role="assistant", text="ok"):
    return {"role": role, "content": text}


def _make_system_msg(text="You are helpful."):
    return {"role": "system", "content": text}


def _build_agent(max_images=10, eval_mode=False, initial_user_message=""):
    """Build a minimal Agent with mocked dependencies for image-limit testing."""
    agent = object.__new__(Agent)
    agent.messages_lock = threading.RLock()
    agent.observe_lock = threading.Lock()
    agent.max_conversation_rounds = 9999  # don't trigger round-based trim
    agent.keep_recent_rounds = 5
    agent.max_images_in_context = max_images
    agent.eval_mode = eval_mode
    agent.initial_user_message = initial_user_message
    agent._initial_user_message_id = "init123" if initial_user_message else ""
    agent.conversation_history = []
    agent.full_message_history = []
    agent.staging_buffer = []
    agent.llm_inflight = False
    agent.pending_exec_result = False
    agent.current_exec_action = None
    agent.roll_notify_url = None
    # Fields required by trim markers (observation mode, last-exec hint)
    agent.observe_enabled = True
    agent.last_exec_action_content = None
    agent.last_exec_action_ts = 0.0
    agent.record_dir = None
    # Auto-summarize plumbing: image-limit now routes through summary queue+inject.
    agent.auto_summarize_token_threshold = 0
    agent.auto_summary_profile = "general"
    agent.awaiting_summary = False
    agent.pending_auto_summarize_total_tokens = None
    agent.pending_auto_summarize_reason = None
    agent.message_revision = 0
    # Self-reward: disabled for these tests so the out-of-band call is skipped.
    agent.enable_self_reward = False
    agent.self_reward_in_flight = False
    agent.self_reward_window_count = 0
    agent.self_reward_history = []
    agent.state_snapshot_provider = None
    # Stub disk I/O
    from unittest.mock import MagicMock
    agent._save_messages = MagicMock()
    return agent


class TestCountImagesInMessage(unittest.TestCase):
    def test_no_images(self):
        self.assertEqual(Agent._count_images_in_message(_make_text_msg()), 0)

    def test_single_image(self):
        self.assertEqual(Agent._count_images_in_message(_make_image_msg(num_images=1)), 1)

    def test_multiple_images(self):
        self.assertEqual(Agent._count_images_in_message(_make_image_msg(num_images=3)), 3)

    def test_system_message(self):
        self.assertEqual(Agent._count_images_in_message(_make_system_msg()), 0)


class TestImageLimitSummary(unittest.TestCase):
    """Image-limit routes through the shared auto-summarize flow."""

    def test_no_trim_when_under_limit(self):
        agent = _build_agent(max_images=10)
        agent.conversation_history = [
            _make_system_msg(),
            _make_image_msg(tag="img1"),
            _make_text_msg(),
            _make_image_msg(tag="img2"),
        ]
        trigger = agent._should_trigger_summarize()
        self.assertIsNone(trigger)
        # All messages remain; no summary was queued or injected.
        non_system = [m for m in agent.conversation_history if m.get("role") != "system"]
        self.assertEqual(len(non_system), 3)
        self.assertIsNone(agent.pending_auto_summarize_reason)
        self.assertFalse(agent.awaiting_summary)

    def test_disabled_when_zero(self):
        """max_images_in_context=0 means image-limit is disabled."""
        agent = _build_agent(max_images=0)
        sys_msg = _make_system_msg()
        msgs = [sys_msg]
        for i in range(50):
            msgs.append(_make_image_msg(tag=f"img{i}"))
        agent.conversation_history = msgs

        trigger = agent._should_trigger_summarize()
        self.assertIsNone(trigger)
        non_system = [m for m in agent.conversation_history if m.get("role") != "system"]
        self.assertEqual(len(non_system), 50)
        self.assertIsNone(agent.pending_auto_summarize_reason)

    def test_image_limit_queues_summary_and_injects_request(self):
        """Exceeding the image limit queues a summary (reason=image_limit) and
        injects the request into conversation_history (no keep-last-N trim)."""
        agent = _build_agent(max_images=3)
        sys_msg = _make_system_msg()
        msgs = [sys_msg]
        for i in range(5):
            msgs.append(_make_image_msg(tag=f"img{i}"))
            msgs.append(_make_text_msg(text=f"reply{i}"))
        agent.conversation_history = msgs
        original_count = len(msgs)  # snapshot BEFORE trim mutates conversation_history

        trigger = agent._should_trigger_summarize()
        self.assertEqual(trigger, ("image_limit", 5))
        count = trigger[1]
        agent._queue_summary_request(count, reason="image_limit")
        result = agent._inject_pending_summary_request()
        # Returns True because a window boundary (image-limit summary) was triggered.
        self.assertTrue(result)

        # Summary request was injected at the tail; awaiting_summary flag is set.
        self.assertTrue(agent.awaiting_summary)
        last = agent.conversation_history[-1]
        self.assertTrue(last.get("mcbots_summary_request"))
        self.assertEqual(last.get("mcbots_summary_reason"), "image_limit")
        # Image count used as the trigger count.
        self.assertEqual(last.get("mcbots_summary_trigger_count"), 5)

        # No keep-last-N trim happened: all original messages are still in
        # conversation_history (only the summary request was appended).
        self.assertEqual(len(agent.conversation_history), original_count + 1)

        # No context-trim marker was produced (old behavior is gone).
        self.assertFalse(any(m.get("mcbots_context_trim") for m in agent.conversation_history))


if __name__ == "__main__":
    unittest.main()
