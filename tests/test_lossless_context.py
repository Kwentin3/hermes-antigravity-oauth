"""Canonical context through actual client/OS pipes; no credentials, network or models."""
import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from client import AntigravityClient
from prompt import AntigravityPromptBudgetExceeded, _format_delta_prompt, _format_messages_as_prompt

RECORDING = TASK_CONTEXT = None
WORKER = r'''
import json,os,pathlib,sys
sys.stdin.reconfigure(encoding='utf-8')
folder=pathlib.Path(sys.argv[1]);stream='--input-format' in sys.argv
print(json.dumps({'event':'init','conversation_id':'offline-lossless'}),flush=True)
requests=sys.stdin if stream else [sys.stdin.read()]
for number,line in enumerate(requests):
    text=json.loads(line)['message']['content'] if stream else line.removesuffix('\n')
    (folder/(str(os.getpid())+'-'+str(number)+'.json')).write_text(json.dumps({'content':text}),encoding='utf-8')
    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'captured','usage':{}}}),flush=True)
'''


def context_messages():
    if RECORDING:
        data=json.loads(Path(RECORDING).read_text(encoding='utf-8'))
        task=json.loads(Path(TASK_CONTEXT).read_text(encoding='utf-8'))
        state=task['frozen_state']
        frozen={'repository':state['target']['repository'],'base_sha':state['target']['base_sha'],
                'candidate_sha':state['target']['candidate_sha'],'objective':state['objective'],
                'pull_request':{'url':state['pull_request_url'],'title':state['pull_request_title'],
                                'body':state['pull_request_body']}}
        messages=[{'role':'system','content':'CONTEXT:\n'+json.dumps(frozen,ensure_ascii=False)},
                  {'role':'user','content':task['first_user']['20261020_105304_ec37fa']['content']}]
        for row in data['rows']:
            if row['role']=='tool':
                messages.append({'role':'tool','tool_call_id':row['tool_call_id'],'name':row['tool_name'],
                                 'content':row['content'] or ''})
            elif row.get('tool_calls'):
                # Only permitted tool calls; assistant text/reasoning is not part of this recording.
                messages.append({'role':'assistant','content':None,'tool_calls':json.loads(row['tool_calls'])})
        return messages
    messages=[{'role':'system','content':'PR title/body, acceptance, base=BASE candidate=HEAD; preserve this task'},
              {'role':'user','content':'Review the frozen Git source.'}]
    for number in range(110):
        call={'id':f'c{number}','function':{'name':'dual_review_source','arguments':json.dumps({'path':f'file{number}.py','action':'read'})}}
        messages.extend([{'role':'assistant','content':None,'tool_calls':[call]},
                         {'role':'tool','tool_call_id':call['id'],'content':f'HEAD{number}\n'+('source-line\n'*730)+f'MIDDLE{number}\nTAIL{number}'}])
    return messages


class LosslessContextTests(unittest.TestCase):
    def assert_messages_present(self, messages, text):
        for message in messages:
            if message.get('content'):
                content=message['content']
                self.assertTrue(content in text,'Missing canonical content sha256='+hashlib.sha256(content.encode()).hexdigest())
            for call in message.get('tool_calls',[]):
                arguments=json.dumps(call['function']['arguments'],ensure_ascii=False)
                self.assertTrue(arguments in text,'Missing canonical arguments sha256='+hashlib.sha256(arguments.encode()).hexdigest())

    def test_full_and_delta_keep_historical_large_text_arguments_and_whitespace(self):
        messages=[{'role':'system','content':'  PR TASK \u041a\U0001f642  '},{'role':'user','content':'review'}]
        for number in range(12):
            args=json.dumps({'code':'argument-middle-'+str(number)+'X'*2000})
            messages.extend([{'role':'assistant','content':'Tool Result (quoted): literal text',
                              'tool_calls':[{'id':f'c{number}','function':{'name':'source','arguments':args}}]},
                             {'role':'tool','tool_call_id':f'c{number}','content':' \nHEAD'+str(number)+'Y'*25000+'MIDDLE\nTAIL\n '}])
        original=copy.deepcopy(messages)
        for render in (_format_messages_as_prompt,_format_delta_prompt):
            text=render(messages)
            self.assert_messages_present(messages,text)
            self.assertNotIn('tool output truncated',text)
        self.assertEqual(messages,original)

    def test_explicit_character_and_utf8_bounds_reject_instead_of_cutting(self):
        messages=[{'role':'user','content':'PRIVATE_PAYLOAD '+chr(0x1f642)*200}]
        for render in (_format_messages_as_prompt,_format_delta_prompt):
            text=render(messages);chars=len(text);size=len(text.encode('utf-8'))
            self.assertEqual(render(messages,max_prompt_chars=chars,max_prompt_bytes=size),text)
            for kwargs in ({'max_prompt_chars':chars-1},{'max_prompt_bytes':size-1}):
                with self.assertRaises(AntigravityPromptBudgetExceeded) as caught:render(messages,**kwargs)
                self.assertEqual(caught.exception.prompt_chars,chars)
                self.assertEqual(caught.exception.prompt_bytes,size)
                self.assertNotIn('PRIVATE_PAYLOAD',str(caught.exception))

    def test_large_canonical_history_has_no_implicit_budget(self):
        messages=context_messages()
        for render in (_format_messages_as_prompt,_format_delta_prompt):
            text=render(messages)
            self.assertGreater(len(text),160000)
            self.assert_messages_present(messages,text)

    def test_actual_client_pipes_full_delta_respawn_and_oneshot_keep_canonical_context(self):
        with tempfile.TemporaryDirectory(prefix='lossless-context-') as directory:
            folder=Path(directory);worker=folder/'worker.py';worker.write_text(WORKER,encoding='utf-8')
            captures=folder/'captures';captures.mkdir();auth=folder/'empty-auth';auth.mkdir()
            env={key:os.environ[key] for key in ('PATH','SystemRoot','COMSPEC') if key in os.environ}
            env.update(HOME=directory,USERPROFILE=directory,HERMES_HOME=directory,TEMP=directory,TMP=directory,
                       ANTIGRAVITY_CONFIG_DIR=str(auth),ANTIGRAVITY_WORKSPACE_RULES='0')
            real_popen=subprocess.Popen;spawned=[]
            def spawn(argv,*args,**kwargs):
                # Only adapt Python's argv entry point; protocol flags/client/reader/pipes remain real.
                if list(argv[:3])==[sys.executable,'--input-format','stream-json']:
                    argv=[argv[0],*argv[3:5],*argv[1:3],*argv[5:]]
                process=real_popen(argv,*args,**kwargs)
                if argv[0]==sys.executable:spawned.append(process)
                return process
            with patch.dict(os.environ,env,clear=True),patch('client.is_authenticated',return_value=True),patch('subprocess.Popen',side_effect=spawn):
                client=AntigravityClient(command=sys.executable,args=[str(worker),str(captures)])
                owned=[]
                try:
                    messages=context_messages()
                    def request(value):
                        result=client.chat.completions.create(model='gemini-4-argon-low',messages=value,timeout=10)
                        self.assertEqual(result.choices[0].message.content,'captured')
                    request(messages);first=client._worker_proc;owned.append(first)
                    first_payload=json.loads((captures/f'{first.pid}-0.json').read_text(encoding='utf-8'))['content']
                    self.assert_messages_present(messages,first_payload)
                    extra=[{'role':'assistant','content':'captured'},
                           {'role':'tool','tool_call_id':'continuation','content':'DELTA-MIDDLE'+('Z'*25000)+'DELTA-TAIL'}]
                    request(messages+extra)
                    self.assertIs(client._worker_proc,first)
                    delta=json.loads((captures/f'{first.pid}-1.json').read_text(encoding='utf-8'))['content']
                    self.assert_messages_present(extra,delta)
                    self.assertTrue(messages[0]['content'] not in delta,'Continuation unexpectedly replayed the system task')
                    first.terminate();first.wait(timeout=5)
                    request(messages+extra);replacement=client._worker_proc;owned.append(replacement)
                    self.assertNotEqual(replacement.pid,first.pid)
                    replay=json.loads((captures/f'{replacement.pid}-0.json').read_text(encoding='utf-8'))['content']
                    self.assert_messages_present(messages+extra,replay)
                    self.assertTrue(client._worker_lock.acquire(blocking=False))
                    try:request(messages)
                    finally:client._worker_lock.release()
                    oneshots=[p for p in captures.iterdir() if p.stem.split('-')[0] not in {str(p.pid) for p in owned}]
                    self.assertEqual(len(oneshots),1)
                    self.assertTrue(json.loads(oneshots[0].read_text(encoding='utf-8'))['content']==first_payload,
                                    'Oneshot canonical prompt differs')
                    if RECORDING:
                        print(json.dumps({'recorded_tool_result_chars':sum(len(m.get('content') or '') for m in messages if m['role']=='tool'),
                                          'full_prompt_chars':len(first_payload),'full_prompt_sha256':hashlib.sha256(first_payload.encode()).hexdigest(),
                                          'worker_reused':True,'worker_replaced':True,'oneshot_equal':True,'model_calls':0}))
                finally:
                    client.close()
                    # These are this fixture's pipe handles, not a provider cleanup assertion.
                    for process in spawned:
                        for pipe in (process.stdin,process.stdout,process.stderr):
                            if pipe and not pipe.closed:pipe.close()
                self.assertTrue(all(p.poll() is not None for p in owned))


if __name__=='__main__':
    parser=argparse.ArgumentParser(add_help=False);parser.add_argument('--recorded-input');parser.add_argument('--task-context')
    args,remaining=parser.parse_known_args();RECORDING=args.recorded_input;TASK_CONTEXT=args.task_context
    if bool(RECORDING)!=bool(TASK_CONTEXT):parser.error('recorded input and task context must be supplied together')
    unittest.main(argv=[sys.argv[0],*remaining])
