import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from scripts.runtime import remote_bash_server


class RemoteBashProcessTests(unittest.TestCase):
    def test_sandbox_cd_uses_namespace_boundary(self):
        with mock.patch.object(remote_bash_server, "SANDBOX_ENABLED", True):
            self.assertEqual(remote_bash_server.check_cd_command("cd /tmp && pwd"), (True, ""))
            self.assertEqual(remote_bash_server.check_cd_command("cd /workspace"), (True, ""))

    def test_sandbox_tmp_is_private_and_persistent(self):
        first = remote_bash_server.sandbox_tmp_dir()
        with open(os.path.join(first, "persist"), "w") as output:
            output.write("value")
        second = remote_bash_server.sandbox_tmp_dir()
        self.assertEqual(first, second)
        with open(os.path.join(second, "persist")) as source:
            self.assertEqual(source.read(), "value")
        self.assertEqual(os.stat(first).st_mode & 0o777, 0o700)

    def test_terminate_process_group_stops_child_process(self) -> None:
        process = subprocess.Popen(
            ["bash", "-c", "sleep 60 & child=$!; echo $child; wait"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        assert process.stdout is not None
        child_pid = int(process.stdout.readline().strip())
        process.stdout.close()

        remote_bash_server.terminate_process_group(process, grace_sec=0.2)

        self.assertIsNotNone(process.poll())
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"child process {child_pid} survived group termination")

    def test_network_disabled_sandbox_uses_net_namespace_and_unix_relay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            action_bin = os.path.join(tmp, "bin")
            os.mkdir(action_bin)
            runtime_config = os.path.join(tmp, "runtime.json")
            with open(runtime_config, "w", encoding="utf-8") as handle:
                handle.write('{"agentbridge":{"host":"127.0.0.1","port":12345}}')
            with (
                mock.patch.object(remote_bash_server, "SANDBOX_ENABLED", True),
                mock.patch.object(remote_bash_server, "SANDBOX_DISABLE_NETWORK", True),
                mock.patch.object(remote_bash_server, "SANDBOX_ACTION_BIN", action_bin),
                mock.patch.object(
                    remote_bash_server,
                    "SANDBOX_RUNTIME_CONFIG",
                    runtime_config,
                ),
                mock.patch.object(
                    remote_bash_server.shutil,
                    "which",
                    return_value="/usr/bin/bwrap",
                ),
                mock.patch.object(
                    remote_bash_server,
                    "_ensure_agentbridge_relay",
                    return_value="/run/mcbots/agentbridge-relay.sock",
                ),
            ):
                argv, cwd, _env = remote_bash_server.build_execution(
                    "mcapi state", tmp
                )
        self.assertIsNone(cwd)
        self.assertIn("--unshare-net", argv)
        self.assertIn("--ro-bind", argv)
        self.assertIn("MCBOTS_AGENTBRIDGE_RELAY_SOCKET", argv)
        self.assertLess(
            len(remote_bash_server._AGENTBRIDGE_RELAY_HOST_PATH.encode()),
            108,
        )

    def test_agentbridge_relay_rejects_external_or_unknown_endpoints(self) -> None:
        self.assertTrue(
            remote_bash_server._is_allowed_agentbridge_endpoint("/api/state")
        )
        self.assertTrue(
            remote_bash_server._is_allowed_agentbridge_endpoint(
                "/api/input/MOVE_FORWARD/true"
            )
        )
        self.assertFalse(
            remote_bash_server._is_allowed_agentbridge_endpoint(
                "https://example.com/api/state"
            )
        )
        self.assertFalse(
            remote_bash_server._is_allowed_agentbridge_endpoint("/api/unknown")
        )


if __name__ == "__main__":
    unittest.main()
