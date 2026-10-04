import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.env import Environment, StateItem


class EnvironmentStateQueueTests(unittest.TestCase):
    def _make_env(self, maxlen: int = 3) -> Environment:
        env = Environment(container_name="test-bot", state_queue_maxlen=maxlen)
        self.addCleanup(env.http.close)
        return env

    @staticmethod
    def _append_chat(env: Environment, idx: int) -> None:
        env._append_state(
            StateItem(
                type="chat_message",
                timestamp=float(idx),
                chat_content=f"msg-{idx}",
                chat_type="system",
            )
        )

    def test_get_new_states_keeps_absolute_cursor_across_rollover(self):
        env = self._make_env(maxlen=3)

        for idx in range(3):
            self._append_chat(env, idx)

        for idx in range(3, 5):
            self._append_chat(env, idx)

        new_states = env.get_new_states(3)

        self.assertEqual([state.chat_content for state in new_states], ["msg-3", "msg-4"])

    def test_get_new_states_returns_current_buffer_when_cursor_falls_behind(self):
        env = self._make_env(maxlen=3)

        for idx in range(5):
            self._append_chat(env, idx)

        new_states = env.get_new_states(0)

        self.assertEqual([state.chat_content for state in new_states], ["msg-2", "msg-3", "msg-4"])

    def test_start_skips_optional_chat_thread_when_disabled(self):
        started_targets = []

        class FakeThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                started_targets.append(self.target.__name__)

        env = Environment(container_name="test-bot", chat_monitor_enabled=False)
        self.addCleanup(env.http.close)
        with patch("agent.env.threading.Thread", FakeThread):
            env.start()

        self.assertEqual(started_targets, ["_screenshot_loop", "_action_loop"])

    def test_start_uses_action_thread_only_when_background_monitors_are_disabled(self):
        started_targets = []

        class FakeThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                started_targets.append(self.target.__name__)

        env = Environment(
            container_name="test-bot",
            chat_monitor_enabled=False,
            periodic_screenshot_enabled=False,
        )
        self.addCleanup(env.http.close)
        with patch("agent.env.threading.Thread", FakeThread):
            env.start()

        self.assertEqual(started_targets, ["_action_loop"])


if __name__ == "__main__":
    unittest.main()
