"""Offline regressions for bounded contexts, native counters and optional semantic MCP."""
import contextlib
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import eng
import orchestration as orch
import recovery_consent as rc
import serena_tools as serena
from session_store import Store
import task_runtime as runtime
import token_intelligence as ti
import verification_mcp as vm

SID='00000000-0000-4000-8000-000000000001'
SID2='00000000-0000-4000-8000-000000000002'


def event(total,last):
    values=lambda n:{'input_tokens':n,'cached_input_tokens':n//2,'output_tokens':n//10,'reasoning_output_tokens':n//20}
    return {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':values(total),'last_token_usage':values(last),'model_context_window':258400}}}


def write_lines(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')


class Fixture(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.folder=self.root/'.agentic/runs/test-task-0001';self.folder.mkdir(parents=True)
        orch.put(self.folder/'task.json',{'goal':'Change fixture'});self.store=Store(self.root)


class UsageTests(Fixture):
    def test_cumulative_delta_and_duplicate_events(self):
        path=self.root/'native.jsonl';write_lines(path,[event(1000,100),event(1000,100)])
        before=ti.snapshot('codex',SID,path=path);self.assertEqual(before['model_calls'],1)
        write_lines(path,[event(1000,100),event(1000,100),event(1400,400),event(1700,300)])
        after=ti.snapshot('codex',SID,path=path);d=ti.delta(before,after)
        self.assertEqual(d['tokens']['input_tokens'],700);self.assertEqual(d['tokens']['uncached_input_tokens'],350);self.assertEqual(d['model_calls'],2)
        self.assertIsNone(ti.delta(None,after));self.assertIsNone(ti.delta(after,before))
        self.assertEqual(ti.delta(None,after,new_session=True)['tokens']['input_tokens'],1700)

    def test_claude_streaming_usage_deduplicates(self):
        path=self.root/'claude.jsonl'
        e=lambda mid,usage:{'type':'assistant','message':{'id':mid,'usage':usage}}
        write_lines(path,[e('msg-a',{'input_tokens':20,'cache_read_input_tokens':100,'cache_creation_input_tokens':30,'output_tokens':1}),e('msg-a',{'output_tokens':10}),e('msg-b',{'input_tokens':10,'cache_read_input_tokens':200,'output_tokens':5})])
        d=ti.snapshot('claude',SID,path=path)
        self.assertEqual(d['tokens']['input_tokens'],360);self.assertEqual(d['tokens']['output_tokens'],15);self.assertEqual(d['model_calls'],2)

    def test_retry_log_archive_and_no_legacy_double_count(self):
        result=self.folder/'build.json';log=result.with_suffix('.log')
        log.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':900000}})+'\n')
        orch.put(result.with_suffix('.usage.json'),{'provider':'codex','rotation':'new_task','usage':{'tokens':{'input_tokens':100,'cached_input_tokens':60,'output_tokens':5},'model_calls':1}})
        ti.attempt_log(result);self.assertFalse(log.exists())
        log.write_text('retry');orch.put(result.with_suffix('.usage.json'),{'provider':'codex','rotation':'same_task','usage':{'tokens':{'input_tokens':150,'cached_input_tokens':100,'output_tokens':8},'model_calls':2}})
        r=runtime.codex_usage(self.folder)
        self.assertEqual(r['tokens']['input_tokens'],250);self.assertEqual(r['attempts_with_usage'],2);self.assertEqual(r['model_calls'],3)
        self.assertEqual(len(list(self.folder.glob('build*.log'))),2)

    def test_historical_thread_total_is_not_task_usage(self):
        write_lines(self.folder/'build.log',[{'type':'thread.started','thread_id':SID},{'type':'turn.completed','usage':{'input_tokens':143000000,'cached_input_tokens':139000000,'output_tokens':2000}}])
        r=runtime.codex_usage(self.folder);self.assertIsNone(r['tokens']);self.assertEqual(r['legacy_reported_tokens']['input_tokens'],143000000)

    def test_missing_partial_attempt_is_visible(self):
        (self.folder/'build.log').write_text('limit reached')
        orch.put(self.folder/'build.usage.json',{'provider':'codex','rotation':'new_task','usage':None})
        r=runtime.codex_usage(self.folder);self.assertIsNone(r['tokens']);self.assertEqual(r['attempts_without_usage'],1)

    def test_tool_counts_only_and_bounded_report(self):
        path=self.root/'native.jsonl';write_lines(path,[event(100,100),{'type':'response_item','payload':{'type':'function_call','name':'mcp__gaston_serena__find_symbol'}},{'type':'response_item','payload':{'type':'function_call_output','output':'secret-sensitive-text'}},event(200,100)])
        data=ti.snapshot('codex',SID,path=path)
        self.assertEqual(data['tool_calls']['mcp__gaston_serena__find_symbol'],1);self.assertNotIn('secret-sensitive-text',json.dumps(data))


class LifecycleTests(Fixture):
    def test_new_task_rotates_same_task_is_reused_until_budget(self):
        self.store.bind('codex',SID,role='builder');self.store.bind('claude',SID2,role='reviewer')
        entry=self.store.get('codex','builder')
        self.assertEqual(ti.builder_rotation(self.root,'codex',entry,self.folder.name),'new_task')
        orch.put(self.root/'.agentic/local/builder-context-codex.json',{'session_id':SID,'task_id':self.folder.name})
        self.assertEqual(ti.builder_rotation(self.root,'codex',entry,self.folder.name,current={'last_context_tokens':60000}),'same_task')
        self.assertEqual(ti.builder_rotation(self.root,'codex',entry,self.folder.name,current={'last_context_tokens':130000}),'context_budget')
        with self.store.locked('codex','builder'),patch.object(ti,'snapshot',return_value={'last_context_tokens':130000}):
            existing,_,reason=ti.begin_builder(self.store,self.root,'codex',entry,self.folder/'build.json',role='builder')
        self.assertIsNone(existing);self.assertEqual(reason,'context_budget');self.assertEqual(self.store.get('claude','reviewer')['id'],SID2);self.assertEqual(self.store.load()['archive'][0]['id'],SID)

    def test_actual_codex_invocation_fresh_then_same_task_and_preserves_previous_result(self):
        self.store.bind('codex',SID,role='builder');calls=[]
        cfg={'models':{},'reasoning':{},'preferences':{}}
        def execute(cmd,log,**kwargs):
            calls.append(cmd);out=Path(cmd[cmd.index('--output-last-message')+1]);out.write_text('{"status":"done","changed_files":["backend/A.java"]}')
            log.write_text(json.dumps({'type':'thread.started','thread_id':SID2})+'\n')
            return subprocess.CompletedProcess(cmd,0,stdout=log.read_text())
        with patch.object(eng,'ROOT',self.root),patch.object(eng,'SESSIONS',self.store),patch.object(eng,'command_exists',return_value=True),patch.object(eng,'load_config',return_value=cfg),patch.object(eng,'run_process',side_effect=execute),patch.object(eng,'codex_effort_args',return_value=[]),patch.object(ti,'snapshot',return_value=None),patch.object(serena,'codex_args',return_value=[]):
            eng.codex_structured('work',{'type':'object'},self.folder/'build.json','model','workspace-write',role='build')
            eng.codex_structured('continue',{'type':'object'},self.folder/'build.json','model','workspace-write',role='build')
        self.assertNotIn('resume',calls[0]);self.assertIn(SID2,calls[1]);self.assertEqual(len(list(self.folder.glob('build*.log'))),2)
        self.assertEqual(len(list(self.folder.glob('build*.usage.json'))),2)

    def test_lead_checkpoint_never_rotates_with_new_user_turn_or_active_task(self):
        import project_continuity as continuity
        continuity.record(self.root, milestones=[{'id':'slice-next','title':'Integrate approved slice','scope':'Run final integration acceptance','status':'planned','source':'Developer-approved task specification'}], approved_decisions=[{'id':'policy','text':'Keep API','source':'Developer-approved contract'}], next_milestone_id='slice-next', developer_confirmed=True)
        self.store.bind('claude',SID,role='reviewer')
        kwargs={'summary':'Accepted slice, next integrate','decisions':['Keep API'],'next_steps':['Run next slice'],'task_ids':[self.folder.name],'plan_ids':[],'safe_to_rotate':True}
        with patch.object(ti,'lead_context',return_value={'status':'rotate_at_safe_boundary','context_tokens':230000}),patch.object(ti,'last_user_fingerprint',return_value='a'):
            self.assertTrue(ti.record_lead_checkpoint(self.root,'claude',SID,**kwargs)['rotation_eligible'])
        with patch.object(ti,'last_user_fingerprint',return_value='b'):
            self.assertFalse(ti.maybe_rotate_lead(self.root,self.store,'claude'))
        orch.put(self.folder/'state.json',{'stage':'implementing'})
        with patch.object(ti,'last_user_fingerprint',return_value='a'):
            self.assertFalse(ti.maybe_rotate_lead(self.root,self.store,'claude'))
        orch.put(self.folder/'state.json',{'stage':'slice_reviewed'})
        with patch.object(ti,'last_user_fingerprint',return_value='a'):
            self.assertTrue(ti.maybe_rotate_lead(self.root,self.store,'claude'))
        self.assertIsNone(self.store.get('claude','reviewer'));self.assertEqual(self.store.load()['archive'][0]['id'],SID)

    def test_registry_concurrent_roles_do_not_overwrite(self):
        errors=[];barrier=threading.Barrier(2)
        def bind(provider,role,sid):
            try:
                barrier.wait(timeout=2)
                for _ in range(15):self.store.bind(provider,sid,role=role)
            except Exception as exc:errors.append(exc)
        a=threading.Thread(target=bind,args=('codex','builder',SID));b=threading.Thread(target=bind,args=('claude','reviewer',SID2));a.start();b.start();a.join(5);b.join(5)
        self.assertEqual(errors,[]);self.assertFalse(a.is_alive() or b.is_alive());self.assertEqual(self.store.get('codex','builder')['id'],SID);self.assertEqual(self.store.get('claude','reviewer')['id'],SID2)


class RecoveryTests(Fixture):
    def setUp(self):
        super().setUp();orch.put(self.folder/'state.json',{'stage':'blocked','blocker':'provider_quota','resumable':True});orch.put(self.folder/'worker.json',{'attempt':'a'*32})
    def approve(self):return rc.decision(self.folder,True,user_request='Yes, resume automatically',retry_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
    def test_no_consent_no_automatic_resume(self):
        self.assertEqual(rc.current(self.folder)['decision'],'pending')
        with self.assertRaises(ValueError):rc.check_resume(self.folder,'anything')
        rc.decision(self.folder,False,user_request='No')
        self.assertEqual(rc.current(self.folder)['decision'],'declined')
        with self.assertRaises(ValueError):rc.check_resume(self.folder,'anything')
    def test_one_approved_job_not_before_retry_not_after_consumption(self):
        r=self.approve();token=r['recovery_token']
        with self.assertRaises(ValueError):rc.check_resume(self.folder,token)
        rc.record_job(self.folder,token,'native-job-1')
        with self.assertRaises(ValueError):rc.record_job(self.folder,token,'another-job')
        with self.assertRaises(ValueError):rc.check_resume(self.folder,token)
        with patch.object(rc.time,'time',return_value=time.time()+1200):
            record=rc.check_resume(self.folder,token);rc.consume(self.folder,record)
            with self.assertRaises(ValueError):rc.check_resume(self.folder,token)
    def test_previous_approval_does_not_authorize_new_interruption(self):
        r=self.approve();rc.record_job(self.folder,r['recovery_token'],'job')
        orch.put(self.folder/'worker.json',{'attempt':'b'*32})
        self.assertEqual(rc.current(self.folder)['decision'],'pending')
        with self.assertRaises(ValueError):rc.check_resume(self.folder,r['recovery_token'])
    def test_requires_explicit_timezone_and_records_only_time_hint(self):
        with self.assertRaises(ValueError):rc.decision(self.folder,True,user_request='yes',retry_at='2099-11-20T00:21')
        self.assertEqual(rc.retry_hint('quota reached; try again at 12:21 AM. private content'),'12:21 AM')


class SemanticAndOutputTests(Fixture):
    def test_serena_only_scoped_read_tools(self):
        source=self.root/'backend/A.java';source.parent.mkdir();source.write_text('class A {}')
        self.assertTrue(serena.safe_call(self.root,{'name':'find_symbol','arguments':{'relative_path':'backend/A.java'}}))
        for name,path in [('rename_symbol','backend/A.java'),('find_symbol','../escape'),('find_symbol','.agentic/runs'),('find_symbol',str(source)),('find_symbol','.')]:
            self.assertFalse(serena.safe_call(self.root,{'name':name,'arguments':{'relative_path':path}}))
        (self.root/'link').symlink_to('/tmp',target_is_directory=True)
        self.assertFalse(serena.safe_call(self.root,{'name':'find_symbol','arguments':{'relative_path':'link'}}))
        tools=serena.filter_response({'result':{'tools':[{'name':'find_symbol'},{'name':'replace_symbol_body'},{'name':'activate_project'},{'name':'write_memory'}]}},'tools/list')
        self.assertEqual(tools['result']['tools'],[{'name':'find_symbol'}])
        self.assertTrue(serena.filter_response({'result':{'content':[{'text':'x'*25000}]}},'tools/call')['result']['isError'])
    def test_provider_configs_share_restricted_server_without_global_registration(self):
        with patch.object(serena,'executable',return_value='/usr/local/bin/serena'):
            self.assertIn('serena_tools.py',' '.join(serena.config(self.root,'claude')['args']))
            self.assertIn('mcp_servers.gaston_serena.command',' '.join(serena.codex_args(self.root)))
            args=serena.server_args(self.root,'codex','/serena')
            self.assertIn('no-memories',args);self.assertEqual(args[args.index('--project')+1],str(self.root))
        with patch.object(serena,'executable',return_value=None):self.assertEqual(serena.codex_args(self.root),[])
    def test_serena_stdio_round_trip(self):
        # Simulated upstream, real proxy subprocess and wire protocol.
        # No network, language server or provider credentials required.
        fake=self.root/'serena'
        fake.write_text('#!'+sys.executable+'\n'+"""import json,sys
if '--help' in sys.argv:
    print('--project --context --mode --enable-web-dashboard --open-web-dashboard');sys.exit(0)
for line in sys.stdin:
    msg=json.loads(line);method=msg.get('method');ident=msg.get('id')
    if ident is None:continue
    if method=='initialize':result={'protocolVersion':'2025-03-26','capabilities':{'tools':{},'resources':{}},'serverInfo':{'name':'fixture','version':'1'},'instructions':'untrusted'}
    elif method=='tools/list':result={'tools':[{'name':'find_symbol'},{'name':'replace_symbol_body'}]}
    else:result={'content':[{'type':'text','text':'symbol A'}]}
    print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result}),flush=True)
""")
        fake.chmod(0o755)
        def request(ident,method,params=None):
            return {'jsonrpc':'2.0','id':ident,'method':method,'params':params or {}}
        calls=[request(1,'initialize'),request(2,'tools/list'),request(3,'tools/call',{'name':'find_symbol','arguments':{'relative_path':'README.md','name_path_pattern':'A'}}),request(4,'tools/call',{'name':'replace_symbol_body','arguments':{'relative_path':'README.md'}})]
        result=subprocess.run([sys.executable,str(Path(serena.__file__)),'codex',str(fake)],input=''.join(json.dumps(c)+'\n' for c in calls),capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        results={x['id']:x for x in map(json.loads,result.stdout.splitlines())}
        self.assertEqual(set(results),{1,2,3,4});self.assertEqual(results[1]['result']['capabilities'],{'tools':{}})
        self.assertEqual(results[2]['result']['tools'],[{'name':'find_symbol'}]);self.assertEqual(results[3]['result']['content'][0]['text'],'symbol A');self.assertIn('error',results[4])

    def test_failed_invocation_without_native_session_keeps_unknown(self):
        path=self.folder/'build.json';ti.finish_builder(self.root,'codex',None,path,None,'new_session',returncode=1)
        self.assertEqual(runtime.codex_usage(self.folder)['attempts_without_usage'],1)
        self.assertFalse((self.root/'.agentic/local/builder-context-codex.json').exists())

    def test_mutating_plan_response_keeps_status_not_all_old_evidence(self):
        plan={'status':'ready','plan_id':'p','items':[{'id':'done','owner':'lead','kind':'inspect','status':'done','ready':False,'evidence':'x'*20000},{'id':'todo','owner':'builder','kind':'implement','status':'pending','ready':True,'goal':'implement','paths':['a'],'acceptance_criteria':['correct']}]}
        compact=vm.compact_tool_response('record_lead_inspection',plan,{'item_id':'done'})
        self.assertLess(len(json.dumps(compact)),1000);self.assertEqual(compact['items'][1]['acceptance_criteria'],['correct']);self.assertIn('evidence',plan['items'][0])
        self.assertEqual(vm.compact_tool_response('work_plan_status',plan,{'detailed':True}),plan)
    def test_quality_response_dedup_does_not_drop_findings(self):
        data={'status':'ready','changed_source_files':['a','b'],'review':{'required_files':['a','b'],'missing_files':['a','b'],'open_findings':['fix']}}
        o=vm.compact_tool_response('task_code_review',data,{})
        self.assertTrue(o['review']['all_required_files_missing_review']);self.assertEqual(o['review']['open_findings'],['fix']);self.assertNotIn('changed_source_files',o)


if __name__=='__main__':unittest.main(verbosity=2)
