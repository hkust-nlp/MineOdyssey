"""Scripted provider for container integration tests; no external model calls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import time
from agent.navigation_prompt import build_navigation_system_prompt

class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls+=1
        count=self.server.calls
        assert request['messages'][0]['content']==build_navigation_system_prompt(action_protocol='tool_calls')
        baseline=self.server.baseline
        prompt_digest=hashlib.sha256(request['messages'][0]['content'].encode()).hexdigest()
        assert prompt_digest==baseline['system_prompt_sha256']
        assert request.get('tools') == baseline['tools']
        assert request['max_tokens'] == 32000
        assert request['reasoning_effort'] == 'max'
        assert request['thinking'] == {'type': 'enabled'}
        time.sleep(getattr(self.server,"delay",0))
        failed=self.server.fail_all or count==1
        with self.server.receipt.open('a') as output:
            output.write(json.dumps({'call':count,'simulated_status':500 if failed else 200,
                                     'system_prompt_sha256':prompt_digest,
                                     'tools_present':bool(request.get('tools'))})+'\n')
        if failed:
            body={'error':{'message':'Simulated transient failure','type':'server_error'}}
        else:
            actions=self.server.actions
            content=actions[min(count-2,len(actions)-1)]
            action = {'type': re.search(r'<type>(.*?)</type>', content).group(1)}
            command = re.search(r'<content>(.*?)</content>', content, re.S)
            delay = re.search(r'<observe_after_sec>(.*?)</observe_after_sec>', content)
            if command: action['content'] = html.unescape(command.group(1))
            if delay: action['observe_after_sec'] = float(delay.group(1))
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': f'call_smoke_{count}', 'type': 'function',
                'function': {'name': 'minecraft_action', 'arguments': json.dumps(action)}}]}
            body={'id':f'mock-{count}','object':'chat.completion','created':1,'model':'parity-mock',
                  'choices':[{'index':0,'finish_reason':'tool_calls','message':message}],
                  'usage':{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130}}
        encoded=json.dumps(body).encode();self.send_response(500 if failed else 200)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)))
        self.end_headers();self.wfile.write(encoded)
    def log_message(self,*args):pass

if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.calls = 0
    for key in ("fail_all", "actions", "delay", "baseline"):
        setattr(server, key, config[key])
    server.receipt = Path(config["receipt"])
    Path(config["port_file"]).write_text(str(server.server_port))
    server.serve_forever()
