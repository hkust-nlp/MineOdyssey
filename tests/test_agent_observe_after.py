import sys
import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.agent import Agent


class AgentObserveAfterTests(unittest.TestCase):
    def _make_agent(self, capture_states, *, observe_enabled: bool = False) -> Agent:
        capture_iter = iter(capture_states)
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        original_proxy_env = {
            key: os.environ.get(key)
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        }
        for key in original_proxy_env:
            os.environ[key] = ""

        def restore_proxy_env():
            for key, value in original_proxy_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore_proxy_env)

        agent = Agent(
            env_getter=lambda after_index: [],
            action_sender=lambda action: None,
            api_key="test",
            base_url="http://localhost",
            model="test-model",
            servername="test-server",
            agentname="test-agent",
            enable_video_recording=False,
            enable_frame_dedup=True,
            enable_approx_frame_dedup=False,
            default_observe_enabled=observe_enabled,
            capture_now_getter=lambda: next(capture_iter, None),
        )
        agent.record_dir = Path(temp_dir.name)
        agent.screenshot_dir = agent.record_dir / "screenshots"
        agent.messages_file = agent.record_dir / "messages.json"
        agent.screenshot_dir.mkdir(parents=True, exist_ok=True)
        return agent

    @staticmethod
    def _screenshot_state(timestamp: float, payload: bytes):
        return SimpleNamespace(type="screenshot", timestamp=timestamp, screenshot=payload)

    def test_paused_observe_after_keeps_distinct_frame(self):
        agent = self._make_agent(
            [self._screenshot_state(2.0, b"observe-after-frame")],
            observe_enabled=False,
        )

        agent.frame_filter.force_keep_next(1, event_type="post_exec_snapshot", timestamp=1.0)
        agent._insert_screenshot_message(
            self._screenshot_state(1.0, b"post-exec-frame"),
            label="post-exec snapshot",
        )
        agent._schedule_paused_observe_after_snapshot(action_id="action-1", deadline_ts=1.5)

        attempted = agent._capture_paused_observe_after_snapshot_if_due(now_ts=2.0)

        self.assertTrue(attempted)
        self.assertEqual(agent._pending_paused_observe_after_deadline(), 0.0)
        image_msgs = [msg for msg in agent.full_message_history if agent._message_has_image(msg)]
        self.assertEqual(len(image_msgs), 2)
        self.assertIn("observe_after snapshot", image_msgs[-1]["content"][0]["text"])

    def test_paused_observe_after_drops_exact_duplicate_frame(self):
        agent = self._make_agent(
            [self._screenshot_state(2.0, b"post-exec-frame")],
            observe_enabled=False,
        )

        agent.frame_filter.force_keep_next(1, event_type="post_exec_snapshot", timestamp=1.0)
        agent._insert_screenshot_message(
            self._screenshot_state(1.0, b"post-exec-frame"),
            label="post-exec snapshot",
        )
        agent._schedule_paused_observe_after_snapshot(action_id="action-1", deadline_ts=1.5)

        attempted = agent._capture_paused_observe_after_snapshot_if_due(now_ts=2.0)

        self.assertTrue(attempted)
        self.assertEqual(agent._pending_paused_observe_after_deadline(), 0.0)
        image_msgs = [msg for msg in agent.full_message_history if agent._message_has_image(msg)]
        self.assertEqual(len(image_msgs), 1)

    def test_command_completion_cancels_pending_observe_after_snapshot(self):
        agent = self._make_agent(
            [self._screenshot_state(2.0, b"post-exec-frame")],
            observe_enabled=False,
        )
        agent.current_exec_action = {
            "id": "action-1",
            "start_ts": 1.0,
            "start_event_emitted": True,
            "finished": False,
        }
        agent.pending_exec_result = True
        agent._schedule_paused_observe_after_snapshot(
            action_id="action-1",
            deadline_ts=5.0,
        )

        agent._insert_command_result_message(
            SimpleNamespace(
                command="mcapi state",
                stdout="{}",
                stderr=None,
                exit_code=0,
                command_status="completed",
                timestamp=2.0,
                action_id="action-1",
                task_id="task-1",
            )
        )

        self.assertEqual(agent._pending_paused_observe_after_deadline(), 0.0)
        image_msgs = [msg for msg in agent.full_message_history if agent._message_has_image(msg)]
        self.assertEqual(len(image_msgs), 1)
        self.assertIn("post-exec snapshot", image_msgs[0]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
