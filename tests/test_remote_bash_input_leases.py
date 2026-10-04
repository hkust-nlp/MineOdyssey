import unittest
from unittest.mock import patch
from scripts.runtime import remote_bash_server as server


class InputLeaseTests(unittest.TestCase):
    def setUp(self):
        server._input_owners.clear()
        server._live_input_tasks.clear()
        server._input_cleanup_pending.clear()
        self.bridge = patch.object(server, '_bridge_request', return_value={'success': True}).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(server._input_owners.clear)
        self.addCleanup(server._live_input_tasks.clear)
        self.addCleanup(server._input_cleanup_pending.clear)

    def send(self, owner, pressed):
        return server._leased_bridge_request('http://localhost',
            '/api/input/MOVE_FORWARD/' + str(pressed).lower(), None, 8, owner)

    def test_supervisor_releases_after_sandbox_dies(self):
        server._live_input_tasks.add('old')
        self.send('old', True)
        server._retire_input_lease('old')
        self.assertNotIn('old', server._live_input_tasks)
        self.assertEqual(server._input_owners, {})
        self.assertEqual(self.bridge.call_args.args[1], '/api/input/MOVE_FORWARD/false')

    def test_late_old_release_cannot_release_new_action(self):
        server._live_input_tasks.update(['old', 'new'])
        self.send('old', True)
        with self.assertRaises(RuntimeError):
            self.send('new', True)
        server._retire_input_lease('old')
        self.send('new', True)
        calls = self.bridge.call_count
        with self.assertRaises(ValueError):
            self.send('old', False)
        self.assertEqual(self.bridge.call_count, calls)
        self.assertEqual(server._input_owners['MOVE_FORWARD'], 'new')

    def test_cleanup_failure_blocks_new_input_and_can_be_retried(self):
        server._live_input_tasks.update(['old', 'new'])
        self.send('old', True)
        self.bridge.return_value = {'success': False}
        with self.assertRaises(RuntimeError):
            server._retire_input_lease('old')
        with self.assertRaises(RuntimeError):
            self.send('new', True)
        self.bridge.return_value = {'success': True}
        server._retire_input_lease('old')
        self.send('new', True)

    def test_press_timeout_still_has_cleanup_owner(self):
        server._live_input_tasks.add('old')
        self.bridge.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.send('old', True)
        self.assertEqual(server._input_owners['MOVE_FORWARD'], 'old')
        self.bridge.side_effect = None
        server._retire_input_lease('old')

    def test_state_does_not_require_input_lease(self):
        server._leased_bridge_request('http://localhost', '/api/state', None, 8, None)
        self.bridge.assert_called_once()


if __name__ == '__main__':
    unittest.main()
