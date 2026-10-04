import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval.harbor_agents.original import child_environment, terminal_reason, ROOT
from tests.test_harbor_navigation import gateway

class OriginalAgentTransportTests(unittest.TestCase):
    def test_effective_setting_and_original_prompt_are_used(self):
        setting = json.loads((ROOT/'eval/navigation/settings/final-navigation-v1.json').read_text())
        session = dict(agent=setting['agent'], limits=setting['limits'], prompt='original task',
                       display=':7', record_video=False, eval_setting=setting['task_eval_defaults'])
        config = dict(api_key='test', base_url='http://127.0.0.1', modelname='test', model_params={'thinking': {'type': 'enabled'}})
        with patch.dict(os.environ, {'MCBOTS_MAX_IMAGES_IN_CONTEXT': '7', 'MCBOTS_ACTION_PROTOCOL': 'tool_calls'}):
            env = child_environment(session, config, Path('/tmp/test'), 1234)
        self.assertEqual(env['MCBOTS_ACTION_PROTOCOL'], 'tool_calls')
        self.assertEqual(env['MCBOTS_LLM_MAX_RETRIES'], '0')
        self.assertEqual(env['MCBOTS_MAX_CONSECUTIVE_LLM_FAILURES'], '3')
        self.assertEqual(env['MCBOTS_EXEC_TIMEOUT'], '300')
        self.assertEqual(env['MCBOTS_MAX_IMAGES_IN_CONTEXT'], '100')
        self.assertEqual(env['MCBOTS_AUTO_SUMMARIZE_TURN_THRESHOLD'], '100')
        self.assertEqual(env['MCBOTS_AUTO_SUMMARIZE_TOKEN_THRESHOLD'], '200000')
        self.assertEqual(env['MCBOTS_INITIAL_USER_INPUT'], 'original task')
        self.assertEqual(env['MCBOTS_SYSTEM_PROMPT_PROFILE'], 'navigation')

    def test_termination_causes_preserved(self):
        for reason, expected in [('max_consecutive_llm_failures','llm_failure_limit'),
                ('max_llm_request_successes','step_limit'), ('exception:RuntimeError','agent_crash')]:
            self.assertEqual(terminal_reason({'stop_reason': reason}, 0), expected)
        self.assertEqual(terminal_reason({}, 1), 'agent_crash')

    def test_rpc_cannot_reach_arbitrary_network_or_files(self):
        for path in ['http://example.invalid/', '/api/command', '/../exec', '/health', '/task/x', '/exec?url=x']:
            with self.assertRaises(ValueError):
                gateway.remote_rpc({'method':'POST', 'path':path,'data':{'command':'true'}})
        for command in ['', [], 'x'*65537]:
            with self.assertRaises(ValueError):
                gateway.remote_rpc({'method':'POST','path':'/exec_async','data':{'command':command}})
        with self.assertRaises(ValueError):
            gateway.dispatch('agent-terminal', {'reason':'llm_failure_limit'})

    def test_events_forward_only_public_messages_with_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'control').mkdir()
            (p/'control/evaluator-events.jsonl').write_text(json.dumps({'message':'Waypoint recorded','private':'hidden'})+'\n')
            with patch.object(gateway,'RUN',p):
                first=gateway.events({'offset':0})
                self.assertEqual(first,{'offset':1,'events':[{'message':'Waypoint recorded'}]})
                self.assertEqual(gateway.events({'offset':1})['events'],[])
                with self.assertRaises(ValueError):gateway.events({'offset':-1})

    def test_claim_rejects_path_injection(self):
        with self.assertRaises(ValueError):gateway.claim('../escape')


class FeedbackRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_client_receives_transported_public_event_and_claim(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from eval.harbor_agents.container import ContainerRunner
        from agent.navigation_completion import NavigationClaimClient
        with tempfile.TemporaryDirectory() as d:
            logs=Path(d)
            requests=logs/'claims/requests';responses=logs/'claims/responses'
            requests.mkdir(parents=True);responses.mkdir(parents=True)
            claim_id='a'*32
            (requests/(claim_id+'.request.json')).write_text(json.dumps({'claim_id':claim_id}))
            async def rpc(action,payload):
                if action=='events':return {'offset':1,'events':[{'message':'[Navigation evaluator] Recorded.'}]}
                self.assertEqual(payload,{'claim_id':claim_id})
                return {'schema_version':1,'claim_id':claim_id,'accepted':False,'terminal':False,
                        'feedback':{'remaining_locations':['A'],'remaining_attempts':2}}
            bridge=SimpleNamespace(logs_dir=logs,event_offset=0,rpc=rpc)
            await ContainerRunner.pump_feedback(bridge)
            client=NavigationClaimClient(request_dir=requests,response_dir=responses,
                                         event_path=logs/'evaluator-events.jsonl')
            self.assertEqual(client.read_events(),['[Navigation evaluator] Recorded.'])
            self.assertEqual(client.read_events(),[])
            reply=json.loads((responses/(claim_id+'.response.json')).read_text())
            self.assertEqual(reply['feedback']['remaining_attempts'],2)


class InfrastructureGradingTests(unittest.TestCase):
    def test_infrastructure_failure_preserves_evidence_without_reward(self):
        from tests.test_harbor_navigation import verifier
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)
            with self.assertRaises(SystemExit):
                verifier.write_result({'infrastructure_error':True,'terminal_reason':'llm_failure_limit'},output)
            self.assertTrue((output/'completion.json').is_file())
            self.assertFalse((output/'reward.txt').exists())


class ModelMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_metadata_omits_credentials_and_private_endpoint(self):
        import shlex
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from eval.harbor_agents.original import OriginalNavigationAgent
        bridge = SimpleNamespace(environment=SimpleNamespace(service_exec=AsyncMock(
            return_value=SimpleNamespace(return_code=0))))
        config = {'modelname': 'actual-model', 'api_key': 'private-key', 'base_url': 'private-endpoint'}
        values = {'MCBOTS_MODEL_PARAMS_JSON': json.dumps({'max_tokens': 32000, 'api_key': 'private-key',
                   'base_url': 'private-endpoint', 'reasoning_effort': 'max'}),
                  'MCBOTS_API_PROTOCOL': 'chat_completions', 'MCBOTS_ACTION_PROTOCOL': 'xml'}
        await OriginalNavigationAgent.record_model_metadata(bridge, config, values)
        command = bridge.environment.service_exec.call_args.args[0]
        self.assertNotIn('private-key', command)
        self.assertNotIn('private-endpoint', command)
        payload = json.loads(shlex.split(command)[-1])
        self.assertEqual(payload['model_id'], 'actual-model')
        self.assertEqual(payload['max_tokens'], 32000)

    def test_collection_uses_actual_external_model_and_preserves_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                gateway, 'MODEL_METADATA', Path(directory)/'model.json'):
            profile = {'model_parameters': {'model_id': 'review-placeholder'}, 'task_id': 'example-001'}
            without = gateway.collected_run_metadata(profile)
            self.assertEqual(without['model_parameters'], {})
            self.assertEqual(without['model_parameters_source'], 'external_agent_unreported')
            metadata = {'model_id': 'actual-model', 'api_protocol': 'chat_completions', 'action_protocol': 'xml'}
            gateway.write_model_metadata(metadata)
            with_model = gateway.collected_run_metadata(profile)
            self.assertEqual(with_model['model_parameters'], metadata)
            self.assertEqual(with_model['world_profile_model_parameters'], profile['model_parameters'])
            self.assertEqual(with_model['task_id'], profile['task_id'])
            with self.assertRaises(ValueError):gateway.write_model_metadata({**metadata, 'api_key': 'secret'})
            with self.assertRaises(ValueError):gateway.dispatch('model-metadata', metadata)

class RPCRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_retries_transient_exec_failure(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from eval.harbor_agents.original import OriginalNavigationAgent
        bridge=SimpleNamespace(environment=SimpleNamespace(exec=AsyncMock(side_effect=[
            SimpleNamespace(return_code=125,stdout='',stderr='transient engine failure'),
            SimpleNamespace(return_code=0,stdout='{"terminal":false}',stderr='')
        ])),transport_event=Mock())
        reply=await OriginalNavigationAgent.rpc(bridge,'result')
        self.assertEqual(reply,{'terminal':False})
        self.assertEqual(bridge.environment.exec.await_count,2)
        self.assertFalse(hasattr(bridge, 'transport_failure'))

    async def test_action_and_claim_are_not_replayed(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from eval.harbor_agents.original import OriginalNavigationAgent
        for action,payload in [('claim-done',{}),('rpc',{'method':'POST','path':'/exec_async','data':{'command':'xdo key m'}})]:
            bridge=SimpleNamespace(environment=SimpleNamespace(exec=AsyncMock(return_value=
                SimpleNamespace(return_code=125,stdout='',stderr='unknown delivery'))),transport_event=Mock())
            with self.assertRaises(RuntimeError):await OriginalNavigationAgent.rpc(bridge,action,payload)
            self.assertEqual(bridge.environment.exec.await_count,1)
            self.assertEqual(bridge.transport_failure['action'], action)
            with self.assertRaisesRegex(RuntimeError, 'infrastructure failure'):
                OriginalNavigationAgent.check_transport(bridge)


if __name__ == '__main__':
    unittest.main()
