"""Check the container boundary without changing the original Agent logic."""
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from eval.harbor_agents.container import ContainerRunner, child_environment, read_config
from eval.harbor_agents.original import OriginalNavigationAgent, CONFIG_PATH


class ContainerBoundaryTests(unittest.TestCase):
    def test_host_error_log_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = SimpleNamespace(logs_dir=Path(directory), transport_redactions=('test-secret', 'private-url'))
            OriginalNavigationAgent.transport_event(bridge, error='test-secret at private-url', phase='supervision')
            event = json.loads((Path(directory) / 'transport-events.jsonl').read_text())
            self.assertEqual(event['error'], '[redacted] at [redacted]')
            self.assertIsInstance(event['time_unix'], float)

    def test_private_config_is_consumed_and_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{"api_key":"test-secret"}')
            path.chmod(0o600)
            self.assertEqual(read_config(path), {'api_key': 'test-secret'})
            self.assertFalse(path.exists())
            path.write_text('{}')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_config(path)
            self.assertFalse(path.exists())

    def test_unrelated_credentials_do_not_enter_agent_environment(self):
        from eval.navigation.schema import load_setting
        setting = load_setting('final-navigation-v1')
        session = dict(setting, prompt='task', display=':1', record_video=False,
                       eval_setting=setting['task_eval_defaults'])
        config = dict(api_key='model-key', base_url='http://provider', modelname='model')
        with patch.dict(os.environ, {'UNRELATED_TOKEN': 'private', 'AWS_SECRET_ACCESS_KEY': 'private',
                                    'MCBOTS_SCREENSHOT_RETRIES': '2'}):
            env = child_environment(session, config, Path('/logs/agent'), 1234)
        self.assertNotIn('UNRELATED_TOKEN', env)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
        self.assertEqual(env['MCBOTS_API_KEY'], 'model-key')
        self.assertEqual(env['MCBOTS_SCREENSHOT_RETRIES'], '2')


class ContainerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_socket_read_retry_and_uncertain_write_no_replay(self):
        bridge = SimpleNamespace(transport_event=Mock(), transport_failure=None)
        with patch('eval.harbor_agents.container.socket_request', side_effect=[OSError('transient'), {'terminal': False}]) as call:
            self.assertEqual(await ContainerRunner.rpc(bridge, 'result'), {'terminal': False})
            self.assertEqual(call.call_count, 2)
        for action, payload in [('claim-done', {}), ('rpc', {'method': 'POST', 'path': '/exec_async'})]:
            with patch('eval.harbor_agents.container.socket_request', side_effect=OSError('unknown delivery')) as call:
                with self.assertRaises(OSError):
                    await ContainerRunner.rpc(bridge, action, payload)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(bridge.transport_failure['action'], action)

    async def test_credentials_use_private_upload_not_command_arguments(self):
        observed = []
        async def upload(source, destination):
            self.assertEqual(destination, CONFIG_PATH)
            self.assertEqual(source.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(source.read_text())['api_key'], 'test-secret')
            observed.append(source)
        bridge = SimpleNamespace(environment=SimpleNamespace(
            exec=AsyncMock(return_value=SimpleNamespace(return_code=0)), upload_file=upload))
        await OriginalNavigationAgent.deliver_config(bridge, {'api_key': 'test-secret'})
        self.assertFalse(observed[0].exists())
        self.assertTrue(all('test-secret' not in call.args[0] for call in bridge.environment.exec.call_args_list))

    async def test_host_launch_uses_container_exec_and_never_spawns_agent_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = object.__new__(OriginalNavigationAgent)
            agent.logs_dir = Path(directory)
            agent.environment_logs_dir = PurePosixPath('/logs/agent')
            agent.load_model_config = Mock(return_value=dict(api_key='test-secret', base_url='http://provider', modelname='model'))
            agent.verify_container_source = AsyncMock()
            agent.record_model_metadata = AsyncMock()
            agent.deliver_config = AsyncMock()
            agent.collect_agent_state = AsyncMock(return_value={'reason': 'agent_stopped_without_completion'})
            agent.rpc = AsyncMock(side_effect=[{}, {'terminal': True, 'success': False}])
            environment = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(return_code=0)),
                                          capabilities=SimpleNamespace(mounted=True))
            context = SimpleNamespace()
            with patch('asyncio.create_subprocess_exec', side_effect=AssertionError('host process forbidden')):
                await agent.run('unused CLI instruction', environment, context)
            commands = [call.args[0] for call in environment.exec.call_args_list]
            launch = next(command for command in commands if ' --config ' in command)
            self.assertIn('/opt/mcbots-venv/bin/python -m eval.harbor_agents.container', launch)
            self.assertNotIn('test-secret', launch)
            self.assertEqual(context.metadata['agent_location'], 'main_container')
            self.assertTrue(any(' --stop' in command for command in commands))


if __name__ == '__main__':
    unittest.main()
