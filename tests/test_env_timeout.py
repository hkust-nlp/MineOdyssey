import threading
import unittest
from unittest.mock import MagicMock, patch

from agent.env import Environment


class EnvironmentTimeoutTest(unittest.TestCase):
    def test_timeout_emits_command_result(self):
        environment = Environment.__new__(Environment)
        environment.running = True
        environment.exec_timeout = 0
        environment.remote_bash_url = "http://127.0.0.1:1"
        environment.task_lock = threading.Lock()
        environment.current_task_id = "task-1"
        environment.current_action_id = "call-1"
        environment._stop_task = MagicMock()
        environment._append_state = MagicMock()
        response = MagicMock()
        response.json.return_value = {"status": "running"}

        with patch("agent.env.requests.get", return_value=response):
            environment._monitor_task("task-1", "sleep 999", "call-1")

        state = environment._append_state.call_args.args[0]
        self.assertEqual(state.command_status, "timed_out")
        self.assertEqual(state.exit_code, 124)
        self.assertIn("timed out", state.stderr)
        self.assertEqual(state.action_id, "call-1")
        self.assertEqual(state.task_id, "task-1")
        environment._stop_task.assert_called_once_with("task-1")

    def test_rejected_exec_emits_terminal_failure(self):
        environment = Environment.__new__(Environment)
        environment.remote_bash_url = "http://127.0.0.1:1"
        environment.task_lock = threading.Lock()
        environment.current_task_id = None
        environment.current_action_id = None
        environment._stop_current_task = MagicMock()
        environment._append_state = MagicMock()
        response = MagicMock()
        response.json.return_value = {"success": False, "error": "busy"}

        with patch("agent.env.requests.post", return_value=response):
            environment._execute_command("mcapi state", action_id="call-2")

        state = environment._append_state.call_args.args[0]
        self.assertEqual(state.command, "mcapi state")
        self.assertEqual(state.command_status, "failed")
        self.assertEqual(state.exit_code, -1)
        self.assertEqual(state.stderr, "busy")
        self.assertEqual(state.action_id, "call-2")

    def test_monitor_network_errors_still_reach_terminal_timeout(self):
        environment = Environment.__new__(Environment)
        environment.running = True
        environment.exec_timeout = 0.5
        environment.remote_bash_url = "http://127.0.0.1:1"
        environment._stop_task = MagicMock()
        environment._append_state = MagicMock()

        with (
            patch("agent.env.time.time", side_effect=[0.0, 0.0, 1.0, 1.0]),
            patch("agent.env.time.sleep"),
            patch("agent.env.requests.get", side_effect=RuntimeError("offline")),
        ):
            environment._monitor_task("task-1", "sleep 999", "call-3")

        state = environment._append_state.call_args.args[0]
        self.assertEqual(state.command_status, "timed_out")
        self.assertEqual(state.exit_code, 124)
        self.assertEqual(state.action_id, "call-3")
        self.assertEqual(state.task_id, "task-1")
        environment._stop_task.assert_called_once_with("task-1")

    def test_completed_result_carries_action_and_remote_task_ids(self):
        environment = Environment.__new__(Environment)
        environment.running = True
        environment.exec_timeout = 30
        environment.remote_bash_url = "http://127.0.0.1:1"
        environment.task_lock = threading.Lock()
        environment.current_task_id = "task-4"
        environment.current_action_id = "call-4"
        environment._append_state = MagicMock()
        response = MagicMock()
        response.json.return_value = {
            "status": "completed",
            "stdout": "ok",
            "stderr": "",
            "exit_code": 0,
        }

        with patch("agent.env.requests.get", return_value=response):
            environment._monitor_task("task-4", "echo ok", "call-4")

        state = environment._append_state.call_args.args[0]
        self.assertEqual(state.action_id, "call-4")
        self.assertEqual(state.task_id, "task-4")
        self.assertIsNone(environment.current_task_id)
        self.assertIsNone(environment.current_action_id)

    def test_scoped_stop_cannot_stop_a_newer_action(self):
        environment = Environment.__new__(Environment)
        environment.task_lock = threading.Lock()
        environment.current_task_id = "task-new"
        environment.current_action_id = "call-new"
        environment._stop_task = MagicMock()

        environment._stop_current_task(expected_action_id="call-old")

        environment._stop_task.assert_not_called()
        self.assertEqual(environment.current_task_id, "task-new")
        self.assertEqual(environment.current_action_id, "call-new")


if __name__ == "__main__":
    unittest.main()
