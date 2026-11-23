"""Full-input handoff and real completion-wait regression tests."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import eng
import lead_workspace_ops as ops
import task_runtime as runtime
import verification_mcp as vm


class LargeHandoffTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.runs=self.root/'.agentic/runs'; self.runs.mkdir(parents=True)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        for module,name,value in [(vm,'ROOT',self.root),(eng,'ROOT',self.root),(eng,'RUNS',self.runs),
                                  (eng,'SESSIONS',SimpleNamespace(get=lambda *a:{'id':'registered'}))]:
            self.stack.enter_context(patch.object(module,name,value))
        self.stack.enter_context(patch.object(vm,'_authorized_delegate',return_value=True))
        self.stack.enter_context(patch.object(eng,'require_setup_complete'))
        self.stack.enter_context(patch.object(eng,'load_config',return_value={'preferences':{'builder':'codex','reviewer':'claude'}}))
        self.launch=self.stack.enter_context(patch.object(vm.subprocess,'Popen',return_value=SimpleNamespace(pid=1234)))
        self.stack.enter_context(contextlib.redirect_stderr(self.stack.enter_context(open(os.devnull,'w'))))

    def test_full_keyword_rich_goal_criteria_and_snapshot(self):
        spec=self.root/'docs/checkpoint.md';spec.parent.mkdir()
        original=('Exact requirement ©\n'*19000).encode();spec.write_bytes(original)
        goal='Implement production-grade code; never deploy or use production credentials. '+('Unabridged instructions. '*900)+'LAST-REQUIREMENT'
        criteria=[str(i)+': '+('Detailed acceptance evidence. '*45)+'END' for i in range(25)]
        result=vm.delegate_to_builder(goal,criteria,specification_files=['docs/checkpoint.md'])
        self.assertEqual(result['status'],'queued',result)
        folder=self.runs/result['task_id'];task=json.loads((folder/'task.json').read_text())
        self.assertEqual(task['goal'],goal);self.assertEqual(task['acceptance_criteria'],criteria)
        entry=task['specifications'][0];snapshot=self.root/entry['snapshot_path']
        self.assertEqual(snapshot.read_bytes(),original);self.assertEqual(entry['sha256'],hashlib.sha256(original).hexdigest())
        spec.write_text('changed after delegation')
        self.assertEqual(snapshot.read_bytes(),original)
        prompt=eng.build_prompt(task,{})
        self.assertIn('LAST-REQUIREMENT',prompt)
        self.assertIn(criteria[-1],prompt)
        self.assertIn(entry['snapshot_path'],prompt)
        self.assertNotIn('Exact requirement ©',prompt)
        self.launch.assert_called_once()

    def test_high_risk_invalid_input_and_unsafe_path_never_launch(self):
        for call in [lambda:vm.delegate_to_builder('Deploy external environment',risk='high'),
                     lambda:vm.delegate_to_builder('  '),lambda:vm.delegate_to_builder('Add feature',acceptance_criteria=['']),
                     lambda:vm.delegate_to_builder('Add feature',specification_files=['../outside.md']),
                     lambda:vm.delegate_to_builder('Add feature',specification_files=['.agentic/local/secret.json'])]:
            self.assertEqual(call()['status'],'blocked')
        source=self.root/'regular.md';source.write_text('private')
        (self.root/'linked.md').symlink_to(source)
        self.assertEqual(vm.delegate_to_builder('Add feature',specification_files=['linked.md'])['status'],'blocked')
        self.launch.assert_not_called()

    def test_revision_preserves_long_feedback_and_additional_spec(self):
        task=vm.delegate_to_builder('Add feature');folder=self.runs/task['task_id']
        (folder/'build.json').write_text('{"status":"done"}')
        (folder/'state.json').write_text('{"stage":"awaiting_lead_review"}')
        # Mocked PID may belong to an unrelated real process. Simulate the worker's durable exit evidence.
        (folder/'worker-finished.json').write_text(json.dumps({'finished_at':time.time()+1,'stage':'awaiting_lead_review'}))
        feedback='Keep credentials out of production files. '+('Specific fix. '*1000)+'FINAL-FIX'
        source=self.root/'fixes.md';source.write_text('Specific acceptance change. '*900)
        self.assertEqual(vm.request_builder_revision(task['task_id'],feedback,risk='high')['status'],'blocked')
        result=vm.request_builder_revision(task['task_id'],feedback,specification_files=['fixes.md'])
        self.assertEqual(result['status'],'queued',result)
        record=json.loads((folder/'revision-request.json').read_text())
        self.assertEqual(record['feedback'],feedback)
        self.assertEqual((self.root/record['specifications'][0]['snapshot_path']).read_text(),source.read_text())
        self.assertIn('FINAL-FIX',eng.build_prompt({}, {},repair=feedback))

    def test_large_documents_and_notes_are_not_truncated(self):
        text='Unique start\n'+('Requirement café. '*18000)+'\nEND'
        out=ops.document(self.root,'docs/full.md','create',text,requested_by_user='write full spec')
        self.assertEqual(out['status'],'pass',out)
        target=self.root/out['path'];self.assertEqual(target.read_text(),text+'\n')
        self.assertEqual(ops.document(self.root,'docs/full.md','append','Append',requested_by_user='append spec')['status'],'pass')
        self.assertEqual(ops.document(self.root,'docs/full.md','replace_exact','Replacement',old_text=text,requested_by_user='replace spec')['status'],'pass')
        self.assertEqual(target.read_text(),'Replacement\n\nAppend\n')
        note=ops.note(self.root,'long.md',text, mode='create')
        self.assertEqual(note['status'],'pass')
        self.assertEqual((self.root/note['path']).read_text(),text+'\n')
        self.assertEqual(ops.note(self.root,'long.md','TAIL')['status'],'pass')
        self.assertTrue((self.root/note['path']).read_text().endswith('END\nTAIL\n'))
        self.assertEqual(ops.document(self.root,'src/main.java','create',text,requested_by_user='write')['status'],'blocked')

    def test_claude_long_structured_handoff_uses_stdin(self):
        session=SimpleNamespace(locked=lambda *a:contextlib.nullcontext(),mark=lambda *a,**kw:None)
        cfg={'models':{'claude_builder':'claude-sonnet-5-5'},'reasoning':{'claude_builder':'low'}}
        prompt='Complete prompt. '*30000+'END'
        with patch.object(eng,'SESSIONS',session),patch.object(eng,'recover_claude_session',return_value={'id':'abc'}), \
             patch.object(eng,'load_config',return_value=cfg),patch.object(eng,'claude_prompt_refresh_args',return_value=[]), \
             patch.object(eng,'run_process',return_value=subprocess.CompletedProcess([],0,stdout='{"structured_output":{"status":"done"}}')) as run:
            eng.provider_structured('claude',prompt,{'type':'object'},self.root/'build.json',cfg,'build')
        self.assertEqual(run.call_args.kwargs['stdin'],prompt)
        self.assertNotIn(prompt,run.call_args.args[0])


class CompletionTests(unittest.TestCase):
    def setup_worker(self,folder,*,delay=.4,stage='awaiting_lead_review',end_delay=0):
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'task.json').write_text('{}')
        (folder/'state.json').write_text('{"stage":"implementing"}')
        code=('import json,pathlib,time;time.sleep('+str(delay)+');'
              +'pathlib.Path("state.json").write_text(json.dumps({"stage":'+repr(stage)+'}));'
              +'time.sleep('+str(end_delay)+');'
              +'pathlib.Path("worker-finished.json").write_text(json.dumps({"finished_at":time.time()}))')
        started=time.time();p=subprocess.Popen([sys.executable,'-c',code],cwd=folder)
        self.addCleanup(lambda:(p.kill() if p.poll() is None else None,p.wait(timeout=5)))
        (folder/'worker.json').write_text(json.dumps({'pid':p.pid,'started_at':started}))
        return p

    def test_completion_wait_is_long_and_returns_only_after_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);p=self.setup_worker(folder,delay=.1,end_delay=.2)
            result=runtime.wait_worker(folder)
            self.assertEqual(result['status'],'ready',result)
            self.assertTrue(result['builder_finished']);self.assertTrue((folder/'worker-finished.json').exists())
            self.assertGreaterEqual(runtime.BUILDER_WAIT_SECONDS,1800)
            self.assertGreaterEqual(result['elapsed_wait_seconds'],.2)

    def test_timeout_and_cancellation_are_not_builder_completion(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);p=self.setup_worker(folder,delay=3)
            out=runtime.wait_worker(folder,timeout_seconds=1)
            self.assertEqual(out['status'],'pending');self.assertFalse(out['builder_finished'])
            e=threading.Event();e.set()
            out=runtime.wait_worker(folder,cancel_event=e)
            self.assertEqual(out['status'],'cancelled');self.assertFalse(out['builder_finished'])
            self.assertIsNone(p.poll())

    def test_worker_failure_is_terminal_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);self.setup_worker(folder,delay=.1,stage='blocked')
            result=runtime.wait_worker(folder)
            self.assertEqual(result['status'],'blocked');self.assertTrue(result['builder_finished'])

    def test_mcp_stdio_remains_responsive_during_wait(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'.agentic/runs/test-task-1';self.setup_worker(folder,delay=.8,end_delay=.1)
            local=root/'.agentic/local';local.mkdir();token='a'*64;(local/'lead-verification-token').write_text(token)
            # A live MCP requires a manifest matching its loaded release and an active Lead lease.
            from session_store import process_identity
            import socket
            (root/'.agentic/manifest.json').write_bytes((Path(vm.__file__).parent/'manifest.json').read_bytes())
            lease='b'*32; owner={'pid':os.getpid(),'host':socket.gethostname(),**process_identity(os.getpid())}
            lock=local/'locks/claude-reviewer';lock.mkdir(parents=True)
            (lock/'owner.json').write_text(json.dumps({**owner,'lease':lease,'child':owner}))
            code=('import sys;from pathlib import Path;sys.path.insert(0,'+repr(str(Path(vm.__file__).parent))+');'
                  +'import verification_mcp as vm;vm.ROOT=Path('+repr(str(root))+');vm.LOCAL=vm.ROOT/".agentic/local";vm.main()')
            p=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                               text=True,env={**os.environ,'GASTON_LEAD_TOKEN':token,'GASTON_SESSION_LEASE':lease,'GASTON_LEAD_PROVIDER':'claude'})
            q=queue.Queue()
            def reader():
                for line in p.stdout:q.put(json.loads(line))
            thread=threading.Thread(target=reader,daemon=True);thread.start()
            def send(request):p.stdin.write(json.dumps(request)+'\n');p.stdin.flush()
            try:
                send({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'wait_for_builder','arguments':{'task_id':'test-task-1'}}})
                send({'jsonrpc':'2.0','id':2,'method':'ping'})
                first=q.get(timeout=3);self.assertEqual(first['id'],2,first)
                finished=q.get(timeout=5);self.assertEqual(finished['id'],1,finished)
                data=json.loads(finished['result']['content'][0]['text'])
                self.assertTrue(data['builder_finished']);self.assertEqual(data['stage'],'awaiting_lead_review')
                self.assertFalse(finished['result']['isError'])
            finally:
                p.stdin.close();p.wait(timeout=5);p.stdout.close();p.stderr.close();thread.join(timeout=1)

    def test_provider_timeout_covers_completion_deadline(self):
        with tempfile.TemporaryDirectory() as td,patch.object(eng,'LOCAL',Path(td)):
            config=json.loads(eng.lead_verification_mcp_config().read_text())['mcpServers']['gaston_verify']
            self.assertGreater(config['timeout']/1000,runtime.BUILDER_WAIT_SECONDS)
            self.assertIn('mcp_servers.gaston_verify.tool_timeout_sec='+str(runtime.MCP_TOOL_TIMEOUT_SECONDS),eng.codex_lead_mcp_args())

if __name__=='__main__':unittest.main()
