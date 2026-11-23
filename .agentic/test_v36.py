"""v36 coordination regression tests; model calls mocked, actual files and Git used."""
from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import eng
import orchestration as orch
import verification_mcp as vm
import acceptance_audit as audit
import task_runtime
import live_builder_worker as worker


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        self.runs=self.root/'.agentic/runs';self.runs.mkdir(parents=True)
        (self.root/'backend').mkdir();(self.root/'backend/pom.xml').write_text('<project/>')
        self.source='backend/src/main/java/example/IdentityService.java';self.source2='backend/src/main/java/example/OtherService.java';self.test='backend/src/test/java/example/IdentityTest.java'
        self.write(self.source,'class IdentityService {}');self.write(self.source2,'class OtherService {}')
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        for module,name,value in [(vm,'ROOT',self.root),(eng,'ROOT',self.root),(eng,'RUNS',self.runs),(eng,'SESSIONS',SimpleNamespace(get=lambda *a:{'id':'lead'}))]:
            self.stack.enter_context(patch.object(module,name,value))
        self.stack.enter_context(patch.object(vm,'_authorized_delegate',return_value=True))
        self.stack.enter_context(patch.object(eng,'require_setup_complete'))
        self.stack.enter_context(patch.object(eng,'load_config',return_value={'preferences':{'builder':'codex','reviewer':'claude'},'project':{},'checks':[]}))
        self.launch=Mock(return_value=SimpleNamespace(pid=os.getpid()))
        self.stack.enter_context(patch.object(vm,'subprocess',SimpleNamespace(Popen=self.launch,DEVNULL=subprocess.DEVNULL,STDOUT=subprocess.STDOUT)))
        self.stack.enter_context(contextlib.redirect_stderr(self.stack.enter_context(open(os.devnull,'w'))))

    def write(self,name,content):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content);return p

    def call(self,name,**args):return vm.CALLS[name](args)
    def item(self,name,owner,path=None,deps=None):
        return {'id':name,'owner':owner,'kind':'implement' if path else 'inspect','goal':name,'paths':[path] if path else [],'depends_on':deps or [],'acceptance_criteria':['Behavior meets contract'] if path else []}
    def plan(self,*items):return self.call('plan_work',goal='Identity consolidation',items=list(items))['plan_id']
    def delegate(self,pid=None,item='admin',path=None):
        if pid:return self.call('delegate_to_builder',goal='Implement authority slice',plan_id=pid,item_id=item,preflight='Read service and role boundary; tests authored by Lead')
        return self.call('delegate_to_builder',goal='Implement authority slice',paths=[path or self.source],acceptance_criteria=['Correct permissions'],preflight='Inspected implementation and test contract')
    def finish_worker(self,task,paths=None,stage='awaiting_lead_review',blocker=None):
        f=self.runs/task;orch.put(f/'state.json',{'stage':stage,'blocker':blocker});orch.put(f/'worker-finished.json',{'finished_at':time.time()});orch.put(f/'build.json',{'status':'done' if not blocker else 'blocked','changed_files':paths or [self.source],'questions':[]});return f
    def review_source(self,task):
        paths=self.call('task_code_review',task_id=task)['changed_source_files']
        notes={p:{'clarity':'Inspected fixture class name','difficult_logic':'No complicated logic in fixture','maintainability':'Single fixture responsibility','correctness':'Reviewed changed fixture structure'} for p in paths}
        self.assertEqual(self.call('record_code_review',task_id=task,reviews=notes,findings=[])['review']['status'],'pass')
    def record_backend(self,f,selector='IdentityTest'):
        audit.record(f,'backend',{'status':'pass','project':'backend','evidence':{'stdout_tail':'Tests run: 2, Failures: 0, Errors: 0, Skipped: 0'}},parameters={'project':'backend','test_filter':selector})
    def correction(self,task):return self.call('request_builder_revision',task_id=task,feedback='Fix specific boundary',failure_kind='implementation',root_cause='Wrong branch permits action',reproduction='IdentityTest fails unauthorized request')
    def test_plan_independent_inspection_and_test_edits_during_builder(self):
        pid=self.plan(self.item('admin','builder',self.source),self.item('inspect-keys','lead'),self.item('inspect-cleanup','lead'),self.item('later','builder',self.source2,['admin']))
        queued=self.delegate(pid);tid=queued['task_id'];self.assertEqual(queued['status'],'queued')
        ready=self.call('wait_for_builder',task_id=tid);self.assertEqual(ready['status'],'lead_work_available');self.assertFalse(ready['builder_finished']);self.assertEqual({i['id'] for i in ready['ready_lead_items']},{'inspect-keys','inspect-cleanup'})
        self.assertEqual(self.delegate(pid,'later')['status'],'blocked')
        report=self.call('inspect_workspace_files',files=[{'path':self.source,'contains':'class'},{'path':self.source2}]);self.assertEqual(report['files'][0]['matching_lines'],[1]);self.assertIsNotNone(report['files'][1]['sha256'])
        for i in ['inspect-keys','inspect-cleanup']:
            self.assertEqual(self.call('record_lead_inspection',plan_id=pid,item_id=i,evidence='Inspected concrete code and documented no gaps')['status'],'ready')
        edit=self.call('lead_edit_files',task_id=tid,purpose='test_setup',edits=[{'path':self.test,'before_sha256':None,'content':'class IdentityTest {}'}]);self.assertEqual(edit['status'],'pass',edit)
        self.assertEqual((self.root/self.test).read_text(),'class IdentityTest {}')
        self.assertEqual(self.call('lead_edit_files',task_id=tid,purpose='implementation',edits=[{'path':self.source,'before_sha256':orch.digest(self.root/self.source),'content':'bad'}])['status'],'blocked')
        self.assertEqual(self.launch.call_count,1)
        f=self.finish_worker(tid);self.assertIn(self.test,vm._sync_lead_build(f)['changed_files'])
        self.record_backend(f)
        self.review_source(tid)
        review=self.call('review_work_slice',task_id=tid,review_notes='Read changes and tests',documentation_reviews={},documentation_gaps=[]);self.assertEqual(review['status'],'pass',review)
        self.assertEqual(orch.read_record(f/'state.json')['stage'],'slice_reviewed');self.assertFalse(review['release_verified'])
        self.assertEqual(self.delegate(pid,'later')['status'],'queued')

    def test_direct_small_implementation_and_cas(self):
        pid=self.plan(self.item('small','lead',self.source2));start=self.call('start_lead_implementation',plan_id=pid,item_id='small');tid=start['task_id'];p=self.root/self.source2
        sha=orch.digest(p);self.assertEqual(self.call('lead_edit_files',task_id=tid,purpose='implementation',edits=[{'path':self.source2,'before_sha256':'wrong','content':'bad'}])['status'],'blocked');self.assertEqual(orch.digest(p),sha)
        change=self.call('lead_edit_files',task_id=tid,purpose='implementation',edits=[{'path':self.source2,'before_sha256':sha,'old_text':'{}','new_text':'{ int value = 1; }'}]);self.assertEqual(change['status'],'pass',change)
        self.assertIn('value',p.read_text());self.assertEqual(self.call('complete_lead_implementation',task_id=tid,summary='Small fix reviewed')['status'],'ready')
        self.launch.assert_not_called();self.assertEqual(orch.read_record(self.runs/tid/'build.json')['status'],'done')

    def test_two_corrections_takeover_and_stale_evidence(self):
        tid=self.delegate()['task_id'];f=self.finish_worker(tid);self.assertEqual(self.correction(tid)['correction_round'],1)
        self.finish_worker(tid);self.assertEqual(self.correction(tid)['correction_round'],2)
        self.finish_worker(tid);self.record_backend(f);self.assertEqual(len(audit.current(f)),1)
        third=self.correction(tid);self.assertEqual(third['status'],'lead_takeover_required',third);self.assertEqual(self.launch.call_count,3)
        p=self.root/self.source;edit=self.call('lead_edit_files',task_id=tid,purpose='implementation',edits=[{'path':self.source,'before_sha256':orch.digest(p),'content':'class IdentityService { int fixed = 1; }'}]);self.assertEqual(edit['status'],'pass',edit)
        self.assertEqual(audit.current(f),[]);self.assertEqual(self.correction(tid)['status'],'blocked')
        self.assertEqual(self.call('complete_lead_implementation',task_id=tid,summary='Fixed branch directly')['status'],'ready');self.assertEqual(orch.read_record(f/'build.json')['implementation_owner'],'lead')
        self.assertEqual(self.launch.call_count,3)

    def test_quota_is_not_correction(self):
        tid=self.delegate()['task_id'];f=self.finish_worker(tid,stage='blocked',blocker='provider_quota')
        resume=self.call('request_builder_revision',task_id=tid,feedback='Resume partial work');self.assertEqual(resume['correction_round'],0,resume)
        self.finish_worker(tid);self.assertEqual(self.correction(tid)['correction_round'],1)
        self.finish_worker(tid,stage='blocked',blocker='provider_quota');self.assertEqual(self.call('request_builder_revision',task_id=tid,feedback='Resume same correction')['correction_round'],1)

    def test_checkpoint_aggregate_requires_all_items_and_final_evidence(self):
        pid=self.plan(self.item('admin','builder',self.source),self.item('keys','lead',self.source2),self.item('inspect','lead'))
        tid=self.delegate(pid)['task_id'];f=self.finish_worker(tid);self.record_backend(f)
        self.assertEqual(vm.finalize_delegated_task(tid,True,'reviewed')['status'],'blocked')
        self.review_source(tid)
        self.assertEqual(self.call('review_work_slice',task_id=tid,review_notes='reviewed',documentation_reviews={},documentation_gaps=[])['status'],'pass')
        self.assertEqual(self.call('prepare_checkpoint_acceptance',plan_id=pid)['status'],'blocked')
        lead=self.call('start_lead_implementation',plan_id=pid,item_id='keys')['task_id'];p=self.root/self.source2
        self.assertEqual(self.call('lead_edit_files',task_id=lead,purpose='implementation',edits=[{'path':self.source2,'before_sha256':orch.digest(p),'content':'class OtherService { int k=1; }'}])['status'],'pass')
        self.call('complete_lead_implementation',task_id=lead,summary='reviewed');self.record_backend(self.runs/lead)
        self.review_source(lead)
        self.assertEqual(self.call('review_work_slice',task_id=lead,review_notes='reviewed',documentation_reviews={},documentation_gaps=[])['status'],'pass')
        self.call('record_lead_inspection',plan_id=pid,item_id='inspect',evidence='verified code')
        result=self.call('prepare_checkpoint_acceptance',plan_id=pid);self.assertEqual(result['status'],'ready',result);cid=result['task_id'];cf=self.runs/cid
        self.assertEqual(set(orch.read_record(cf/'build.json')['changed_files']),{self.source,self.source2});self.assertFalse(vm.delegation_status(cid)['all_passed']);self.assertEqual(audit.current(cf),[])
        plan=vm.task_verification_plan(cid);self.assertIn('backend_verify',plan['full_backend_gates']);self.assertIn('api_integration',plan['managed_api_gates'])
        self.record_backend(cf);self.assertEqual(vm.finalize_delegated_task(cid,True,'reviewed')['status'],'blocked')

    def test_reject_unsafe_inputs_and_overlapping_builder(self):
        self.assertEqual(self.call('delegate_to_builder',goal='Unscoped checkpoint')['status'],'blocked')
        self.assertEqual(self.call('plan_work',goal='large',items=[self.item('whole','builder','backend')])['status'],'blocked')
        self.assertEqual(self.call('plan_work',goal='cycle',items=[self.item('a','builder',self.source,['b']),self.item('b','lead',self.source2,['a'])])['status'],'blocked')
        tid=self.delegate()['task_id'];self.assertEqual(self.delegate(path=self.source2)['status'],'blocked')
        result=self.call('start_lead_implementation',goal='overlap',paths=[self.source],acceptance_criteria=['correct']);self.assertEqual(result['status'],'blocked')
        for path in ('.agentic/local/token','../outside','backend/.env','backend/keys/private.pem'):
            self.assertEqual(self.call('lead_edit_files',task_id=tid,purpose='test_setup',edits=[{'path':path,'before_sha256':None,'content':'unsafe'}])['status'],'blocked')
        self.assertEqual(self.launch.call_count,1)

    def test_followup_inspections_append_without_losing_scope(self):
        pid=self.plan(self.item('audit','lead'),self.item('admin','builder',self.source))
        report=self.call('record_lead_inspection',plan_id=pid,item_id='audit',evidence='Found missing key rotation',followup_items=[self.item('key-fix','lead',self.source2,['audit'])]);self.assertEqual(report['status'],'ready');self.assertEqual({i['id'] for i in report['items']},{'audit','admin','key-fix'});self.assertFalse(report['all_items_done'])

    def test_early_takeover_and_fixture_failure_stay_with_lead(self):
        tid=self.delegate()['task_id'];self.finish_worker(tid)
        failed=self.call('request_builder_revision',task_id=tid,feedback='Fix malformed fixture',failure_kind='test_fixture',root_cause='Wrong cases JSON',reproduction='Runner rejects the descriptor')
        self.assertEqual(failed['status'],'blocked');self.assertEqual(self.launch.call_count,1)
        takeover=self.call('take_over_builder_task',task_id=tid,reason='Single-line fix after inspecting the actual service',paths=[self.source]);self.assertEqual(takeover['status'],'ready',takeover)
        self.assertEqual(self.call('lead_edit_files',task_id=tid,purpose='implementation',edits=[{'path':self.source,'before_sha256':orch.digest(self.root/self.source),'content':'class IdentityService { int repaired = 1; }'}])['status'],'pass')
        self.assertEqual(self.call('complete_lead_implementation',task_id=tid,summary='Repaired local defect')['status'],'ready');self.assertEqual(self.launch.call_count,1)

    def test_real_git_scoped_attribution_and_test_reuse(self):
        repo=self.root/'backend'
        subprocess.run(['git','init','-q'],cwd=repo,check=True)
        subprocess.run(['git','add','.'],cwd=repo,check=True)
        subprocess.run(['git','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','baseline'],cwd=repo,check=True)
        tid=self.delegate()['task_id'];f=self.finish_worker(tid)
        orch.put(f/'preexisting-changes.json',{'files':eng.workspace_git.existing_changes(self.root,{})})
        self.write(self.source,'class IdentityService { int x=1; }');self.write(self.source2,'class OtherService { int independent=1; }')
        self.assertEqual(worker.changed_paths(self.root,f,{}),[self.source]);self.assertEqual(orch.read_record(f/'concurrent-observed-paths.json')['outside_builder_scope'],[self.source2])
        outcome={'status':'pass','project':'backend','evidence':{'stdout_tail':'Tests run: 2, Failures: 0, Errors: 0, Skipped: 0'}}
        with patch.object(vm,'run_backend_tests',return_value=outcome) as run:
            a=self.call('run_task_acceptance_check',task_id=tid,kind='backend',project='backend',test_filter='IdentityTest')
            b=self.call('run_task_acceptance_check',task_id=tid,kind='backend',project='backend',test_filter='IdentityTest')
            self.assertEqual(a['status'],'pass');self.assertTrue(b.get('reused'));self.assertEqual(run.call_count,1)
            self.write(self.source2,'class OtherService { int independent=2; }')
            c=self.call('run_task_acceptance_check',task_id=tid,kind='backend',project='backend',test_filter='IdentityTest')
            self.assertFalse(c.get('reused',False));self.assertEqual(run.call_count,2)

    def test_no_authorization(self):
        with patch.object(vm,'_authorized_delegate',return_value=False):
            self.assertEqual(self.call('plan_work',goal='no',items=[self.item('audit','lead')])['status'],'blocked')
            self.assertEqual(self.call('inspect_workspace_files',files=[{'path':self.source}])['status'],'blocked')

    def test_delta_prompt_no_repeated_full_requirements(self):
        tid=self.delegate()['task_id'];f=self.finish_worker(tid);task=orch.read_record(f/'task.json');task['goal']='UNIQUE-ORIGINAL-REQUIREMENT '*500;orch.put(f/'task.json',task)
        orch.put(f/'revision-request.json',{'number':1,'feedback':'FIX-THIS-ONE-BRANCH','correction_round':1,'root_cause':'Boundary'});prompts=[]
        def run(provider,prompt,schema,path,cfg,role):
            prompts.append(prompt);return {'status':'done','questions':[],'changed_files':[self.source],'summary':'fixed','remaining_risks':[]}
        with patch.object(eng,'load_task',return_value=task),patch.object(eng,'provider_structured',side_effect=run),patch.object(eng.workspace_git,'existing_changes',return_value={}),patch.object(eng.workspace_git,'changed_existing_files',return_value=[]):
            worker.process(tid)
        self.assertEqual(len(prompts),1);self.assertIn('FIX-THIS-ONE-BRANCH',prompts[0]);self.assertNotIn('UNIQUE-ORIGINAL-REQUIREMENT',prompts[0]);self.assertIn('task.json',prompts[0]);self.assertGreater(task_runtime.task_metrics(f)['handoff_prompt_bytes'],0)


if __name__=='__main__':unittest.main(verbosity=2)
