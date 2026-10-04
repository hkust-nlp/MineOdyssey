"""A local scripted provider drives the actual original Agent in a real world.

This is an infrastructure regression, not a model-navigation result.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import re
import html
from pathlib import Path
import threading
import time
from eval.harbor_agents.original import OriginalNavigationAgent
from agent.main import build_navigation_system_prompt

ACTIONS = [
    '<action><type>exec</type><content>sleep 20; echo should-be-interrupted</content><observe_after_sec>1</observe_after_sec></action>',
    '<action><type>stop_execute</type></action>',
    '<action><type>exec</type><content>cd /tmp &amp;&amp; printf preserved &gt; parity.txt</content></action>',
    '<action><type>exec</type><content>cat /tmp/parity.txt</content></action>',
    '<action><type>claim_done</type></action>',
    '<action><type>claim_done</type></action>',
    '<action><type>claim_done</type></action>',
]

class OriginalSmokeAgent(OriginalNavigationAgent):
    fail_all=False
    @staticmethod
    def name():return 'original-parity-smoke'
    def load_model_config(self):
        return {'modelname':'parity-mock','api_key':'local-test-placeholder',
                'base_url':f'http://127.0.0.1:{self.provider_port}/v1',
                'model_params':{'max_tokens':32000,'reasoning_effort':'max','thinking':{'type':'enabled'}}}

    async def run(self,instruction,environment,context):
        import os
        import shlex
        import tempfile
        from eval.harbor_agents.transport import disable_exec_tty
        disable_exec_tty(environment)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='harbor-host-only-') as directory:
            marker = Path(directory) / 'host-marker'
            marker.write_text('host-only')
            hidden = await environment.exec('test ! -e ' + shlex.quote(str(marker)), timeout_sec=30)
            assert hidden.return_code == 0
            config = Path(directory) / 'provider.json'
            config.write_text(json.dumps({
                'fail_all': self.fail_all, 'actions': getattr(self, 'actions', ACTIONS),
                'delay': getattr(self, 'provider_delay', 0),
                'baseline': json.loads((Path(__file__).parent/'fixtures/navigation_formal_glm.json').read_text()),
                'receipt': str(self.environment_logs_dir/'mock-provider.jsonl'),
                'port_file': '/run/navigation-mock.port',
            }))
            await environment.upload_file(Path(__file__).with_name('harbor_mock_provider.py'), '/run/navigation-mock.py')
            await environment.upload_file(config, '/run/navigation-mock.json')
            started = await environment.exec(
                "nohup /opt/mcbots-venv/bin/python /run/navigation-mock.py /run/navigation-mock.json "
                "> /logs/agent/mock-provider.log 2>&1 < /dev/null &", timeout_sec=30)
            assert started.return_code == 0
            for _ in range(50):
                port = await environment.exec('cat /run/navigation-mock.port', timeout_sec=30)
                if port.return_code == 0:
                    self.provider_port = int(port.stdout)
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError('Container mock provider did not start')
            await super().run(instruction,environment,context)
            calls = [json.loads(line) for line in (self.logs_dir/'mock-provider.jsonl').read_text().splitlines()]
            receipt = json.loads((self.logs_dir/'parity-receipt.json').read_text())
            assert receipt['agent_location'] == 'main_container'
            assert receipt['python'] == '/opt/mcbots-venv/bin/python'
            for name, namespace in receipt['namespaces'].items():
                assert namespace != os.readlink('/proc/self/ns/' + name)
                assert receipt['agent_namespaces'][name] == namespace
            deleted = await environment.exec('test ! -e /run/navigation-agent/config.json', timeout_sec=30)
            assert deleted.return_code == 0
            action_checks = {}
            if not self.fail_all and not getattr(self, 'expect_transport_failure', False):
                messages = [json.loads(line)['message'] for line in
                            (self.logs_dir/'agent-record/messages.jsonl').read_text().splitlines()]
                feedback = []
                for message in messages:
                    if message.get('role') in {'system', 'assistant'}:
                        continue
                    content = message.get('content')
                    feedback.append(content if isinstance(content, str) else '\n'.join(
                        block.get('text', '') for block in content or [] if isinstance(block, dict)))
                actions = getattr(self, 'actions', ACTIONS)
                if ACTIONS[0] in actions:
                    action_id = f"call_smoke_{actions.index(ACTIONS[0]) + 2}"
                    stopped = next(text for text in feedback if 'Tool call ID: ' + action_id + '\n' in text
                                   and '[Command Result' in text)
                    assert 'Exit code: -15' in stopped or 'Exit code: -9' in stopped
                    action_checks['async_interruption_verified'] = True
                assert any('Stdout:\npreserved\n' in text for text in feedback)
                assert any((self.logs_dir/'agent-record/screenshots').glob('*.jpg'))
                action_checks['persistent_action_file_verified'] = True
                action_checks['screenshots_recorded'] = True
            (self.logs_dir/'smoke-summary.json').write_text(json.dumps({
                'provider_calls':len(calls), 'completion':self.completion,
                'agent_in_main_container': True, 'agent_namespaces_match_container': True,
                'host_marker_inaccessible': True, 'credential_file_deleted': True,
                **action_checks,
            },indent=2))
            if getattr(self, 'expect_transport_failure', False):
                assert self.completion['terminal_reason']=='agent_crash'
                assert self.completion['infrastructure_error'] is True
            elif self.fail_all:
                assert len(calls)==3
                assert self.completion['terminal_reason']=='llm_failure_limit'
                assert self.completion['infrastructure_error'] is True
            else:
                assert len(calls)>=8
                assert self.completion['terminal_reason']=='claim_attempts_exhausted'

class OriginalFailureSmokeAgent(OriginalSmokeAgent):
    fail_all=True
    @staticmethod
    def name():return 'original-failure-smoke'


class HarborBoundarySmokeAgent(OriginalFailureSmokeAgent):
    """Check the generic CLI and real unscored verification without model costs."""
    @staticmethod
    def name():return 'harbor-boundary-smoke'

    async def run(self, instruction, environment, context):
        from eval.harbor_agents.transport import disable_exec_tty
        disable_exec_tty(environment)
        rejected = await environment.exec("nav exec 'true' --timeout 300", timeout_sec=30)
        assert rejected.return_code == 2
        assert "at most 120 seconds" in (rejected.stdout or "") + (rejected.stderr or "")
        accepted = await environment.exec(
            "nav exec 'printf boundary-ok' --timeout 120", timeout_sec=660)
        assert accepted.return_code == 0
        assert json.loads(accepted.stdout)["stdout"] == "boundary-ok"
        expired = await environment.exec("nav exec 'sleep 2' --timeout 0.1", timeout_sec=30)
        assert expired.return_code == 1
        assert json.loads(expired.stdout)["error"]["error_code"] == "timeout"
        (self.logs_dir / "cli-boundary.json").write_text(json.dumps({
            "reject_300_before_action": True, "accept_120": True,
            "real_remote_bash_timeout": True,
        }, indent=2) + "\n")
        await super().run(instruction, environment, context)


class OriginalTransportFailureSmokeAgent(OriginalSmokeAgent):
    """Inject one undelivered action and require an unscored infrastructure end."""
    expect_transport_failure = True

    @staticmethod
    def name():return 'original-transport-failure-smoke'

    def runner_command(self):
        import shlex
        from eval.harbor_agents.original import CONTAINER_PYTHON
        script = """import eval.harbor_agents.container as runtime
original = runtime.socket_request
injected = False
def failing_request(action, payload):
    global injected
    if not injected and action == 'rpc' and payload.get('method') == 'POST' and payload.get('path') == '/exec_async':
        injected = True
        raise RuntimeError('Simulated undelivered container-local action')
    return original(action, payload)
runtime.socket_request = failing_request
runtime.main()
"""
        return CONTAINER_PYTHON + ' -c ' + shlex.quote(script)


class OriginalTransportStressAgent(OriginalSmokeAgent):
    provider_delay = 12
    actions = [
        '<action><type>exec</type><content>mcapi state</content><observe_after_sec>1</observe_after_sec></action>',
        '<action><type>exec</type><content>xdo key m &amp;&amp; sleep 3</content><observe_after_sec>1</observe_after_sec></action>',
        '<action><type>exec</type><content>for i in $(seq 1 12); do xdo click 5; sleep 0.2; done; sleep 1</content><observe_after_sec>1</observe_after_sec></action>',
        '<action><type>exec</type><content>for i in $(seq 1 15); do xdo click 5; sleep 0.2; done; sleep 1</content><observe_after_sec>1</observe_after_sec></action>',
        '<action><type>exec</type><content>for i in $(seq 1 6); do xdo click 4; sleep 0.3; done; sleep 1</content><observe_after_sec>1</observe_after_sec></action>',
        '<action><type>exec</type><content>for i in $(seq 1 8); do xdo click 4; sleep 0.3; done; sleep 1</content><observe_after_sec>1</observe_after_sec></action>',
        *ACTIONS[2:]
    ]
    @staticmethod
    def name():return 'original-transport-stress'


class FormalCoordinateSmokeAgent(OriginalSmokeAgent):
    """Exercise the same native Agent while probing real Xaero UI restrictions."""
    provider_delay = 2
    actions = [
        '<action><type>exec</type><content>xdo key m; sleep 2; mcapi state</content></action>',
        '<action><type>exec</type><content>xdo key b; sleep 2; mcapi state</content></action>',
        '<action><type>exec</type><content>xdo key Escape; xdo key b; sleep 2; mcapi state</content></action>',
        '<action><type>exec</type><content>xdo key Escape; mcapi state</content></action>',
        *ACTIONS,
    ]
    @staticmethod
    def name(): return 'formal-coordinate-smoke'
