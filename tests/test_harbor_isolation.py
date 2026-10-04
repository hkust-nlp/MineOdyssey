import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from tests.test_harbor_navigation import gateway, verifier

class IsolationTests(unittest.TestCase):
    def test_http_denial_keeps_policy_message(self):
        error = HTTPError('http://localhost/exec', 403, 'Forbidden', {},
                          io.BytesIO(b'{"error":"policy detail"}'))
        with patch.object(gateway, 'read_json', return_value={'ports': {'remote_bash': 1}}), \
             patch.object(gateway.urllib.request, 'urlopen', side_effect=error):
            result = gateway.remote('cd /tmp')
        self.assertEqual(result['http_status'], 403)
        self.assertEqual(result['error']['error'], 'policy detail')
        self.assertEqual(result['exit_code'], 1)

    def test_image_path_is_argument_not_code(self):
        with patch.object(gateway, 'start', return_value={'ready': True}), \
             patch.object(gateway, 'remote', return_value={'exit_code': 1, 'stderr': 'denied'}) as remote:
            with self.assertRaises(ValueError):
                gateway.dispatch('read-image', {'path': '/workspace/$(touch /tmp/injected)'} )
        import shlex
        argv = shlex.split(remote.call_args.args[0])
        self.assertEqual(argv[-1], '/workspace/$(touch /tmp/injected)')
        self.assertEqual(argv[:2], ['python3', '-c'])

    def test_image_reader_rejects_symlink_and_fifo(self):
        with patch.object(gateway, 'start', return_value={'ready': True}), \
             patch.object(gateway, 'remote', return_value={'exit_code': 1}) as remote:
            with self.assertRaises(ValueError):
                gateway.dispatch('read-image', {'path': '/tmp/example'})
        import shlex, os
        script = shlex.split(remote.call_args.args[0])[2]
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            (root/'escape').symlink_to('/etc/passwd')
            os.mkfifo(root/'fifo')
            (root/'image').write_bytes(b'\x89PNG\r\n\x1a\nabc')
            for name in ('escape', 'fifo'):
                result = subprocess.run(['python3', '-c', script, str(root/name)], capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
            result = subprocess.run(['python3', '-c', script, str(root/'image')], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)

if __name__ == '__main__':
    unittest.main()
