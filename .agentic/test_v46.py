"""Efficiency regressions for narrowly scoped changes; no live providers required."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import acceptance_audit as audit
import backend_runner
import builder_compile as compile_gate
import code_quality as quality
import efficiency_runtime as efficiency
import native_hooks as hooks
import orchestration as orch
import serena_tools as serena
import token_intelligence as ti
import verification_mcp as vm
import workspace_git

SID='00000000-0000-4000-8000-000000000046'
ATTEMPT='a'*32

class Fixture(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.folder=self.root/'.agentic/runs/task-example-46';self.folder.mkdir(parents=True)
        self.code='backend/src/A.java';self.doc='backend/docs/ARCHITECTURE.md'
        for name,content in [(self.code,'class A {}'),(self.doc,'Architecture'),('backend/pom.xml','<project/>')]:
            f=self.root/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(content)
        orch.put(self.folder/'task.json',{'owner':'builder','write_scope':[self.code,self.doc],'code_review_policy':38})
        orch.put(self.folder/'build.json',{'status':'done','changed_files':[self.code,self.doc],'questions':[]})
        orch.put(self.folder/'state.json',{'stage':'awaiting_lead_review'})
    def git(self,*args):
        subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
    def init_git(self):
        self.git('init','-q');self.git('config','user.name','Test');self.git('config','user.email','test@example.invalid');self.git('add','backend');self.git('commit','-qm','initial');(self.root/'.gitignore').write_text('.agentic/\n');self.git('add','.gitignore');self.git('commit','-qm','ignore runtime')
    def declare(self):
        return orch.declare_review_only_documents(self.root,self.folder.name,[self.doc],'Inspected build and runtime: ordinary architecture prose is not an input')

class EvidenceTests(Fixture):
    def test_declared_doc_preserves_only_backend_and_code_review(self):
        self.init_git();self.declare()
        ws=workspace_git.fingerprint(self.root,review_documents=audit.declared_review_documents(self.folder))
        self.assertIsNotNone(ws)
        audit.record(self.folder,'backend',{'status':'pass'},parameters={'workspace_fingerprint':ws})
        audit.record(self.folder,'api',{'status':'pass'})
        quality.record(self.folder,{self.code:{k:'Reviewed source' for k in quality.DIMENSIONS}},[])
        (self.root/self.doc).write_text('Revised architecture wording')
        self.assertEqual([e['kind'] for e in audit.current(self.folder)],['backend'])
        self.assertEqual(quality.report(self.folder)['status'],'pass')
        (self.root/'backend/pom.xml').write_text('<project><name>Changed</name></project>')
        self.assertEqual(audit.current(self.folder),[])
        (self.root/self.code).write_text('class A { int x; }')
        self.assertEqual(quality.report(self.folder)['status'],'incomplete')
    def test_builder_directory_is_hashed_recursively(self):
        directory=self.root/'sample-frontend/src/shared/ui'
        directory.mkdir(parents=True)
        component=directory/'ConfirmDialog.tsx'
        component.write_text('export const Dialog = () => null;')
        build=orch.read_record(self.folder/'build.json')
        build['changed_files']=['sample-frontend/src/shared/ui','sample-frontend/src/shared/icons']
        orch.put(self.folder/'build.json',build)
        first=audit.revision_fingerprint(self.folder)
        self.assertIsNotNone(first)
        component.write_text('export const Dialog = () => true;')
        self.assertNotEqual(first,audit.revision_fingerprint(self.folder))
        second=audit.revision_fingerprint(self.folder)
        (directory/'Another.tsx').write_text('export default 1;')
        self.assertNotEqual(second,audit.revision_fingerprint(self.folder))
        third=audit.revision_fingerprint(self.folder)
        (directory/'Another.tsx').unlink()
        self.assertNotEqual(third,audit.revision_fingerprint(self.folder))

    def test_builder_directory_refuses_untrusted_symlinks(self):
        directory=self.root/'sample-frontend/src/shared/ui';directory.mkdir(parents=True)
        (directory/'Component.tsx').write_text('safe')
        build=orch.read_record(self.folder/'build.json')
        build['changed_files']=['sample-frontend/src/shared/ui']
        orch.put(self.folder/'build.json',build)
        self.assertIsNotNone(audit.revision_fingerprint(self.folder))
        try:(directory/'linked.tsx').symlink_to(self.root/self.code)
        except (NotImplementedError,OSError):self.skipTest('Host forbids symlinks')
        self.assertIsNone(audit.revision_fingerprint(self.folder))

    def test_undeclared_or_spec_documents_remain_material(self):
        before=audit.revision_fingerprint(self.folder,review_only=True)
        (self.root/self.doc).write_text('New requirement')
        self.assertNotEqual(before,audit.revision_fingerprint(self.folder,review_only=True))
        task=orch.read_record(self.folder/'task.json');task['specifications']=[{'original_path':self.doc}];orch.put(self.folder/'task.json',task)
        with self.assertRaises(ValueError):self.declare()
    def test_declaring_after_run_does_not_retroactively_reuse(self):
        self.init_git();ws=workspace_git.fingerprint(self.root)
        audit.record(self.folder,'backend',{'status':'pass'},parameters={'workspace_fingerprint':ws})
        self.declare();(self.root/self.doc).write_text('Editorial correction')
        self.assertEqual(audit.current(self.folder),[])
    def test_no_git_baseline_no_relaxed_backend_reuse(self):
        self.declare();audit.record(self.folder,'backend',{'status':'pass'});(self.root/self.doc).write_text('Edit')
        self.assertEqual(audit.current(self.folder),[])
    def test_new_untracked_prose_excluded_exactly(self):
        self.init_git();name='backend/docs/NOTES.md';target=self.root/name;target.write_text('new')
        before=workspace_git.fingerprint(self.root,review_documents=[name]);target.write_text('edit')
        self.assertEqual(before,workspace_git.fingerprint(self.root,review_documents=[name]))
        (self.root/'backend/docs/OTHER.md').write_text('not declared')
        self.assertNotEqual(before,workspace_git.fingerprint(self.root,review_documents=[name]))

class ScopeLifecycleTests(Fixture):
    def setup_plan(self,owner='builder'):
        plan=orch.create_plan(self.root,'Task',[{'id':'slice','owner':owner,'kind':'implement','goal':'Edit A','paths':[self.code],'acceptance_criteria':['A works']}])
        record,item=orch.select_item(self.root,plan['plan_id'],'slice',owner,'implement')
        return record,item
    def test_explicit_delegation_paths_extend_authoritative_plan(self):
        record,item=self.setup_plan()
        with patch.object(vm,'ROOT',self.root),patch.object(vm,'delegate_to_builder',return_value={'status':'queued'}) as delegate:
            result=vm._orchestration_action('delegate',{'goal':'Edit A and docs','plan_id':record['id'],'item_id':'slice','preflight':'Reviewed existing A','paths':[self.doc]})
        self.assertEqual(result['status'],'queued');delegate.assert_called_once()
        self.assertEqual(orch.get_plan(self.root,record['id'])['items']['slice']['paths'],sorted([self.code,self.doc]))
    def test_scope_extension_updates_bound_item_and_task(self):
        record,item=self.setup_plan();orch.bind(self.root,record,item,self.folder.name)
        task=orch.read_record(self.folder/'task.json');task.update(work_plan_id=record['id'],work_item_id='slice',write_scope=[self.code]);orch.put(self.folder/'task.json',task)
        result=orch.extend_task_scope(self.root,self.folder.name,[self.doc],'Docs needed');self.assertEqual(result['write_scope'],sorted([self.code,self.doc]))
        self.assertEqual(orch.get_plan(self.root,record['id'])['items']['slice']['paths'],result['write_scope'])
    def test_scope_extension_rejects_active_lead_overlap(self):
        other=self.root/'.agentic/runs/other-task-46';orch.put(other/'task.json',{'owner':'lead','write_scope':[self.doc]});orch.put(other/'state.json',{'stage':'lead_implementing'})
        record,item=self.setup_plan()
        with self.assertRaises(ValueError):orch.extend_item_scope(self.root,record,item,[self.doc],reason='overlap')
    def test_questions_can_be_answered_without_fake_edits(self):
        build=orch.read_record(self.folder/'build.json');build['questions']=['Confirm wording?'];orch.put(self.folder/'build.json',build);orch.put(self.folder/'state.json',{'stage':'needs_lead_decision','questions':build['questions']})
        result=orch.resolve_questions(self.root,self.folder.name,{'Confirm wording?':'Inspected requirement and current code; current wording is correct'})
        self.assertFalse(result['requires_revision']);self.assertEqual(orch.read_record(self.folder/'state.json')['stage'],'awaiting_lead_review');self.assertFalse((self.folder/'lead-edits.json').exists())
    def test_blocked_build_not_silently_completed(self):
        build=orch.read_record(self.folder/'build.json');build.update(status='blocked',questions=['Missing logic']);orch.put(self.folder/'build.json',build);orch.put(self.folder/'state.json',{'stage':'needs_lead_decision'})
        result=orch.resolve_questions(self.root,self.folder.name,{'Missing logic':'Use the documented contract'});self.assertTrue(result['requires_revision']);self.assertEqual(orch.read_record(self.folder/'build.json')['status'],'blocked')
    def test_abandon_empty_lead_preserves_history_not_acceptance(self):
        plan,item=self.setup_plan('lead');orch.bind(self.root,plan,item,self.folder.name)
        orch.put(self.folder/'task.json',{'owner':'lead','work_plan_id':plan['id'],'work_item_id':'slice','write_scope':[self.code]});orch.put(self.folder/'build.json',{'status':'in_progress','changed_files':[]});orch.put(self.folder/'state.json',{'stage':'lead_implementing'})
        result=orch.abandon_empty_lead_task(self.root,self.folder.name,'Already addressed by previous slice')
        self.assertEqual(result['stage'],'abandoned');self.assertTrue(self.folder.exists());self.assertFalse(orch.plan_status(self.root,plan['id'])['all_items_done'])
    def test_abandon_with_edits_blocked(self):
        task=orch.read_record(self.folder/'task.json');task['owner']='lead';orch.put(self.folder/'task.json',task);orch.put(self.folder/'state.json',{'stage':'lead_implementing'})
        with self.assertRaises(ValueError):orch.abandon_empty_lead_task(self.root,self.folder.name,'discard')
    def test_observed_scope_is_not_automatically_attributed(self):
        outside='backend/pom.xml';orch.put(self.folder/'worker.json',{'attempt':ATTEMPT,'started_at':100})
        before={self.code:'a',outside:'b'};after={self.code:'c',outside:'d'}
        result=orch.scope_observation(self.root,self.folder,before,after,[self.code],[])
        self.assertEqual(result['pending_paths'],[outside]);self.assertIn('not proof',result['note'])
        other=self.root/'.agentic/runs/other-task-46';orch.put(other/'lead-edits.json',{'files':{outside:{'edited_at':101,'after_sha256':'d'}}})
        result=orch.scope_observation(self.root,self.folder,before,after,[self.code],[])
        self.assertEqual(result['pending_paths'],[]);self.assertEqual(result['matching_lead_ledger'],[outside])
    def test_scope_reconciliation_requires_exact_scope_and_facts(self):
        outside='backend/pom.xml';build=orch.read_record(self.folder/'build.json');build.update(status='blocked',scope_review_required=[outside],out_of_scope_reported=[outside],scope_prior_status='done',scope_prior_questions=[]);orch.put(self.folder/'build.json',build);orch.put(self.folder/'state.json',{'stage':'needs_lead_decision'})
        decision={outside:{'attribution':'builder','evidence':'Reviewed the actual change and original task diff'}}
        with self.assertRaises(ValueError):orch.reconcile_scope(self.root,self.folder.name,decision)
        orch.extend_task_scope(self.root,self.folder.name,[outside],'Required shared build configuration')
        result=orch.reconcile_scope(self.root,self.folder.name,decision)
        self.assertEqual(result['stage'],'awaiting_lead_review');self.assertIn(outside,orch.read_record(self.folder/'build.json')['changed_files'])
        self.assertFalse(orch.read_record(self.folder/'state.json').get('lead_verified'))
    def test_scope_reconciliation_preserves_unrelated_blockers(self):
        outside='backend/pom.xml';build=orch.read_record(self.folder/'build.json');build.update(status='blocked',scope_review_required=[outside],scope_prior_status='blocked',scope_prior_questions=['Compilation failed'],compile_preflight=[{'status':'fail'}]);orch.put(self.folder/'build.json',build);orch.put(self.folder/'state.json',{'stage':'needs_lead_decision'})
        result=orch.reconcile_scope(self.root,self.folder.name,{outside:{'attribution':'external','evidence':'Compared independent edit ledger and exact diff'}})
        self.assertTrue(result['requires_revision']);self.assertEqual(orch.read_record(self.folder/'build.json')['questions'],['Compilation failed'])
        self.assertEqual(orch.read_record(self.folder/'build.json')['compile_preflight'],[{'status':'fail'}])
    def test_native_direct_edit_guard(self):
        orch.put(self.folder/'worker.json',{'attempt':ATTEMPT});orch.put(self.folder/'state.json',{'stage':'implementing'})
        with patch.object(hooks,'ROOT',self.root),patch.dict(os.environ,{'GASTON_BUILDER_TASK_ID':self.folder.name,'GASTON_BUILDER_ATTEMPT':ATTEMPT}),contextlib.redirect_stderr(io.StringIO()):
            hooks.block_native_builder_tool({'tool_name':'Write','tool_input':{'file_path':str(self.root/self.code)}})
            with self.assertRaises(SystemExit):hooks.block_native_builder_tool({'tool_name':'Write','tool_input':{'file_path':'backend/pom.xml'}})
            with self.assertRaises(SystemExit):hooks.block_native_builder_tool({'tool_name':'apply_patch','tool_input':{'patch':'*** Begin Patch\n*** Update File: backend/pom.xml\n*** End Patch'}})
            hooks.block_native_builder_tool({'tool_name':'Bash'}) # not falsely claimed path sandbox

class CompileTests(Fixture):
    def test_compile_only_uses_isolated_offline_system_maven(self):
        def execute(argv,**kw):
            self.assertEqual(argv[-1],'test-compile');self.assertIn('-o',argv);self.assertNotIn('test',argv)
            self.assertTrue(any(x.startswith('-Dmaven.repo.local=') and 'backend-dependencies' in x for x in argv));self.assertIn('backend-dependencies',kw['env']['MAVEN_USER_HOME']);self.assertNotIn('GASTON_LEAD_TOKEN',kw['env'])
            return subprocess.CompletedProcess(argv,0,stdout='BUILD SUCCESS',stderr='')
        with patch.object(backend_runner,'installed_jdks',return_value=[]),patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn'),patch.object(backend_runner.subprocess,'run',side_effect=execute),patch.dict(os.environ,{'GASTON_LEAD_TOKEN':'private'}):
            result=compile_gate.check(self.root,'backend')
        self.assertEqual(result['status'],'pass');self.assertEqual(result['goal'],'test-compile');self.assertEqual(compile_gate.projects_for_paths(self.root,[self.code]),['backend'])
        self.assertEqual(vm.run_backend_tests('backend','test-compile')['status'],'blocked')
    def test_read_only_cache_error_reported_blocked(self):
        with patch.object(backend_runner,'installed_jdks',return_value=[]),patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn'),patch.object(backend_runner.subprocess,'run',return_value=subprocess.CompletedProcess([],1,stdout='Read-only file system',stderr='')):
            self.assertEqual(compile_gate.check(self.root,'backend')['status'],'blocked')
    def test_compile_receipt_is_attempt_and_material_bound(self):
        self.init_git();digest=compile_gate.material_fingerprint(self.root,'backend')
        orch.put(compile_gate.receipt_path(self.folder,ATTEMPT,'backend'),{'attempt':ATTEMPT,'project':'backend','fingerprint':digest,'result':{'status':'pass'}})
        self.assertEqual(compile_gate.receipts(self.root,self.folder,ATTEMPT,['backend'])[0]['status'],'pass')
        self.assertEqual(compile_gate.receipts(self.root,self.folder,'b'*32,['backend'])[0]['status'],'not_run')
        (self.root/self.code).write_text('class A { int newValue; }')
        self.assertEqual(compile_gate.receipts(self.root,self.folder,ATTEMPT,['backend'])[0]['status'],'stale')

class EfficiencyTests(Fixture):
    def test_automatic_boundary_prompt_and_no_repeat_stop(self):
        with patch.object(ti,'lead_context',return_value={'context_tokens':220000,'status':'rotate_at_safe_boundary'}),patch.object(ti,'latest_context',return_value=220000),patch.object(ti,'last_user_fingerprint',return_value='turn1'):
            advisory=efficiency.lead_boundary(self.root,'claude',SID);self.assertIn('checkpoint_lead_context',advisory['action'])
            self.assertIsNotNone(efficiency.stop_advisory(self.root,'claude',SID));self.assertIsNone(efficiency.stop_advisory(self.root,'claude',SID))
            orch.put(self.folder/'state.json',{'stage':'implementing'});self.assertIsNone(efficiency.lead_boundary(self.root,'claude',SID))
    def test_tail_probe_ignores_bad_lines_and_counts_cache(self):
        p=self.root/'log.jsonl';p.write_text(json.dumps({'type':'assistant','message':{'usage':{'input_tokens':10,'cache_read_input_tokens':200000,'cache_creation_input_tokens':200}}})+'\ninvalid\n')
        self.assertEqual(ti.latest_context('claude',SID,path=p),200210)
    def test_serena_usage_counts_only_metadata(self):
        serena.record_usage(self.root,'claude','find_symbol','returned',123,0.2);serena.record_usage(self.root,'codex','find_symbol','error',34,0.3)
        serena.record_usage(self.root,'claude','replace_symbol_body','error',0,0.2)
        result=serena.usage_summary(self.root);self.assertEqual(result['calls_observed'],2);self.assertEqual(result['returned_output_bytes'],157);self.assertEqual(result['outcomes']['error'],1)

if __name__=='__main__':unittest.main()
