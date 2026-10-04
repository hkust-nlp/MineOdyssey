import unittest
from unittest.mock import AsyncMock

from harbor.environments.docker.docker import DockerEnvironment
from eval.harbor_agents.transport import disable_exec_tty


class ComposeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_disables_tty_and_preserves_payload_and_timeouts(self):
        environment = DockerEnvironment.__new__(DockerEnvironment)
        original = AsyncMock(return_value="result")
        environment._run_docker_compose_command = original
        self.assertTrue(disable_exec_tty(environment))
        command = ["exec", "-w", "/workspace", "main", "bash", "-c", "nav rpc events '{}'"]
        result = await environment._run_docker_compose_command(command, check=False, timeout_sec=330)
        self.assertEqual(result, "result")
        original.assert_awaited_once_with(["exec", "-T", *command[1:]], check=False, timeout_sec=330)
        self.assertEqual(command[1], "-w")

    async def test_sidecar_collection_and_existing_no_tty_are_idempotent(self):
        environment = DockerEnvironment.__new__(DockerEnvironment)
        original = AsyncMock()
        environment._run_docker_compose_command = original
        disable_exec_tty(environment)
        disable_exec_tty(environment)
        for command in (["exec", "world", "sh", "-c", "collect"],
                        ["exec", "-T", "world", "sh", "-c", "collect"]):
            await environment._run_docker_compose_command(command)
            self.assertEqual(original.call_args.args[0], ["exec", "-T", "world", "sh", "-c", "collect"])

    async def test_build_and_other_instances_are_unchanged(self):
        environment = DockerEnvironment.__new__(DockerEnvironment)
        original = AsyncMock()
        environment._run_docker_compose_command = original
        other = DockerEnvironment.__new__(DockerEnvironment)
        other_call = AsyncMock()
        other._run_docker_compose_command = other_call
        disable_exec_tty(environment)
        await environment._run_docker_compose_command(["build"])
        original.assert_awaited_once_with(["build"])
        self.assertIs(other._run_docker_compose_command, other_call)
        self.assertFalse(disable_exec_tty(object()))


if __name__ == "__main__":
    unittest.main()
