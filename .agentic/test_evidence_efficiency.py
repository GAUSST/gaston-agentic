"""Regression coverage for compact acceptance, conservative cross-task reuse and targeted slices."""
from __future__ import annotations
import contextlib
import json
import os
import stat
import subprocess
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import acceptance_audit as audit
import evidence_efficiency as efficient
import verification_mcp as vm
import verification_planner
import backend_runner
REAL_ENV_FINGERPRINT=efficient.environment_fingerprint


class EvidenceEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.runs=self.root/'.agentic/runs';self.runs.mkdir(parents=True)
        self.project=self.root/'backend';self.project.mkdir()
        (self.project/'pom.xml').write_text('<project/>')
        self.src=self.project/'src/main/java/example/PricingService.java'
        self.src.parent.mkdir(parents=True)
        self.src.write_text('class PricingService {}')
        self.folders={}
        for tid in ('task-one-0001','task-two-0002','checkpoint-0003'):
            f=self.runs/tid;f.mkdir()
            task={'goal':'Implement'}
            if tid=='checkpoint-0003':task.update(checkpoint_acceptance=True,work_plan_id='plan-one')
            else:task['work_plan_id']='plan-one'
            (f/'task.json').write_text(json.dumps(task))
            (f/'build.json').write_text(json.dumps({'changed_files':['backend/src/main/java/example/PricingService.java'],'status':'done'}))
            (f/'state.json').write_text(json.dumps({'stage':'awaiting_lead_review'}))
            self.folders[tid]=f
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(vm,'ROOT',self.root))
        self.stack.enter_context(patch.object(vm,'_authorized_delegate',return_value=True))
        self.stack.enter_context(patch.object(vm.orch,'active_builders',return_value=[]))
        import eng, workspace_git
        self.stack.enter_context(patch.object(eng,'load_config',return_value={'project':{}}))
        self.stack.enter_context(patch.object(workspace_git,'fingerprint',side_effect=lambda *a,**k:__import__('hashlib').sha256(self.src.read_bytes()).hexdigest()))
        self.env=self.stack.enter_context(patch.object(efficient,'environment_fingerprint',return_value='jdk25-mvnw'))
        self.calls=[]
        def run(**kwargs):
            self.calls.append(kwargs)
            return {'status':'pass','exit_code':0,'goal':'test','project':'backend',
                    'runner':'mvnw','elapsed_seconds':2.5,
                    'evidence':{'stdout_tail':'Tests run: 2, Failures: 0, Errors: 0, Skipped: 0','stderr_tail':''}}
        self.stack.enter_context(patch.object(vm,'run_backend_tests',side_effect=run))

    def test_flutter_evidence_expiration_identifies_changed_inputs(self):
        folder=self.folders['task-one-0001']
        build=json.loads((folder/'build.json').read_text())
        flutter=self.root/'mobile'
        flutter.mkdir()
        (flutter/'pubspec.yaml').write_text('name: demo')
        build['changed_files']=[r'mobile\pubspec.yaml','mobile/pubspec.yaml']
        (folder/'build.json').write_text(json.dumps(build))
        row=audit.record(folder,'flutter_analyze',{'status':'pass'},parameters={'project':'mobile'})
        self.assertEqual(list(row['revision_inputs']),['mobile/pubspec.yaml'])
        self.assertEqual(audit.expired_checks(folder,audit.current(folder)),[])
        (flutter/'build').mkdir()
        (flutter/'build/app.apk').write_bytes(b'generated')
        self.assertEqual(len(audit.current(folder)),1)
        (flutter/'pubspec.yaml').write_text('name: changed')
        self.assertEqual(audit.current(folder),[])
        stale=audit.expired_checks(folder,[])
        self.assertEqual(stale[0]['reason'],'task_inputs_changed')
        self.assertEqual(stale[0]['changed_inputs'],['mobile/pubspec.yaml'])

    def test_cross_task_reuses_exact_verified_runner_once(self):
        a=vm.run_task_acceptance_check('task-one-0001','backend','backend','PricingServiceTest')
        b=vm.run_task_acceptance_check('task-two-0002','backend','backend','PricingServiceTest')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(a['status'],'pass');self.assertTrue(b['reused'])
        records=audit.read(self.folders['task-two-0002'])
        self.assertEqual(records[-1]['reused_from']['task_id'],'task-one-0001')
        self.assertEqual(len(audit.current(self.folders['task-two-0002'])),1)

    def test_fresh_integrated_check_never_reuses_other_task(self):
        vm.run_task_acceptance_check('task-one-0001','backend','backend')
        result=vm.run_task_acceptance_check('checkpoint-0003','backend','backend')
        self.assertEqual(len(self.calls),2)
        self.assertNotIn('reused',result)

    def test_force_bypasses_caching(self):
        vm.run_task_acceptance_check('task-one-0001','backend','backend','PricingServiceTest')
        vm.run_task_acceptance_check('task-two-0002','backend','backend','PricingServiceTest',force=True)
        self.assertEqual(len(self.calls),2)

    def test_code_change_invalidates_shared_evidence(self):
        vm.run_task_acceptance_check('task-one-0001','backend','backend','PricingServiceTest')
        self.src.write_text('class PricingService { int x; }')
        result=vm.run_task_acceptance_check('task-two-0002','backend','backend','PricingServiceTest')
        self.assertEqual(len(self.calls),2)
        self.assertNotIn('reused',result)

    def test_environment_change_invalidates_shared_evidence(self):
        vm.run_task_acceptance_check('task-one-0001','backend','backend','PricingServiceTest')
        self.env.return_value='different-java-or-maven'
        result=vm.run_task_acceptance_check('task-two-0002','backend','backend','PricingServiceTest')
        self.assertEqual(len(self.calls),2)
        self.assertNotIn('reused',result)

    def test_test_selector_mismatch_no_reuse(self):
        vm.run_task_acceptance_check('task-one-0001','backend','backend','PricingServiceTest')
        vm.run_task_acceptance_check('task-two-0002','backend','backend','OtherTest')
        self.assertEqual(len(self.calls),2)

    def test_borrowed_evidence_cannot_become_source(self):
        vm.run_task_acceptance_check('task-one-0001','backend','backend','PricingServiceTest')
        vm.run_task_acceptance_check('task-two-0002','backend','backend','PricingServiceTest')
        # A cache entry without actual runner proof is never a new source.
        self.assertTrue(audit.read(self.folders['task-two-0002'])[-1]['reused_from'])
        direct=efficient.matching_backend_evidence(self.root,self.folders['checkpoint-0003'],
            'backend','PricingServiceTest',__import__('hashlib').sha256(self.src.read_bytes()).hexdigest(),'jdk25-mvnw')
        self.assertIsNone(direct)  # Checkpoint cannot reuse any external evidence.

    def test_failed_or_unproven_outcome_never_cached(self):
        first=self.folders['task-one-0001']
        audit.record(first,'backend',{'status':'pass','exit_code':None},parameters={
            'project':'backend','test_filter':'PricingServiceTest',
            'workspace_fingerprint':__import__('hashlib').sha256(self.src.read_bytes()).hexdigest(),
            'environment_fingerprint':'jdk25-mvnw'})
        vm.run_task_acceptance_check('task-two-0002','backend','backend','PricingServiceTest')
        self.assertEqual(len(self.calls),1)

    def test_unchanged_web_and_api_are_not_cached(self):
        with patch.object(vm,'run_project_web_tests',return_value={'status':'pass','runner':'playwright'}) as run:
            vm.run_task_acceptance_check('task-one-0001','web','backend')
            vm.run_task_acceptance_check('task-two-0002','web','backend')
            self.assertEqual(run.call_count,2)

    def test_focused_slice_uses_changed_test_class(self):
        test=self.project/'src/test/java/example/PricingServiceTest.java'
        test.parent.mkdir(parents=True);test.write_text('class PricingServiceTest {}')
        folder=self.folders['task-one-0001']
        (folder/'build.json').write_text(json.dumps({'status':'done','changed_files':[
            'backend/src/main/java/example/PricingService.java',
            'backend/src/test/java/example/PricingServiceTest.java']}))
        result=vm.run_task_acceptance_check('task-one-0001','backend','backend')
        self.assertEqual(result['status'],'pass')
        self.assertEqual(self.calls[0]['test_filter'],'PricingServiceTest')
        self.assertEqual(audit.read(folder)[-1]['parameters']['test_filter'],'PricingServiceTest')

    def test_checkpoint_never_autofocuses_tests(self):
        test=self.project/'src/test/java/example/PricingServiceTest.java'
        test.parent.mkdir(parents=True);test.write_text('class PricingServiceTest {}')
        folder=self.folders['checkpoint-0003']
        (folder/'build.json').write_text(json.dumps({'status':'done','changed_files':[
            'backend/src/test/java/example/PricingServiceTest.java']}))
        vm.run_task_acceptance_check('checkpoint-0003','backend','backend')
        self.assertIsNone(self.calls[0]['test_filter'])

    def test_over_eight_or_missing_test_files_falls_back_to_full(self):
        folder=self.folders['task-one-0001']
        changed=[]
        for i in range(9):
            test=self.project/f'src/test/java/example/Class{i}Test.java'
            test.parent.mkdir(parents=True,exist_ok=True);test.write_text('class A {}')
            changed.append('backend/'+test.relative_to(self.project).as_posix())
        (folder/'build.json').write_text(json.dumps({'changed_files':changed}))
        self.assertIsNone(efficient.focused_test_selector(self.root,folder,'backend'))

    def test_success_compaction_keeps_evidence_but_drops_log_tail(self):
        raw={'status':'pass','task_id':'t','check_id':'c',
            'evidence':{'status':'pass','runner':'mvnw','log_path':'.agentic/local/backend-logs/x.log',
                        'evidence':{'stdout_tail':'big'*3000,'stderr_tail':'errors'*1000},
                        'exit_code':0}}
        compacted=vm.compact_tool_response('run_task_acceptance_check',raw,{})
        self.assertNotIn('stdout_tail',compacted['evidence']['evidence'])
        self.assertEqual(compacted['evidence']['runner'],'mvnw')
        self.assertEqual(compacted['evidence']['exit_code'],0)
        self.assertIn('stdout_tail',raw['evidence']['evidence'])
        self.assertLess(len(json.dumps(compacted)),len(json.dumps(raw))//4)

    def test_failure_returns_bounded_excerpt(self):
        raw={'status':'fail','reason':'Compile failed','evidence':{'stdout_tail':'x'*8000,'stderr_tail':'y'*10000}}
        result=vm.compact_tool_response('run_backend_tests',raw,{})
        self.assertEqual(result['reason'],'Compile failed')
        self.assertNotIn('stderr_tail',result['evidence'])
        self.assertEqual(len(result['evidence']['stderr_tail_excerpt']),1800)

    def test_tool_telemetry_contains_no_arguments_or_credentials(self):
        efficient.record_tool_telemetry(self.root,'run_task_acceptance_check','task-one-0001',0.25,
                                       {'status':'pass','message':'PRIVATE-PASSWORD'})
        rows=(self.root/'.agentic/local/mcp-tool-usage.jsonl').read_text()
        self.assertNotIn('PRIVATE-PASSWORD',rows)
        row=json.loads(rows.strip())
        self.assertEqual(row['task_id'],'task-one-0001')
        self.assertEqual(row['tool'],'run_task_acceptance_check')
        if os.name=='posix':self.assertEqual(stat.S_IMODE((self.root/'.agentic/local/mcp-tool-usage.jsonl').stat().st_mode),0o600)

    def test_evidence_detail_retrieves_bounded_log_on_request(self):
        folder=self.folders['task-one-0001']
        log=self.root/'.agentic/local/backend-logs/test.log';log.parent.mkdir(parents=True)
        log.write_text('H'*12000)
        entry=audit.record(folder,'backend',{'status':'pass','log_path':log.relative_to(self.root).as_posix(),
                                              'exit_code':0,'goal':'test','evidence':{'stdout_tail':'D'*7000}},
                           parameters={'project':'backend'})
        detail=vm.acceptance_evidence_detail('task-one-0001',entry['id'],False)
        self.assertNotIn('log_excerpt',detail)
        self.assertNotIn('stdout_tail',detail['evidence']['evidence'])
        expanded=vm.acceptance_evidence_detail('task-one-0001',entry['id'],True)
        self.assertEqual(len(expanded['log_excerpt']),8000)
        self.assertEqual(expanded['status'],'ready')

    def test_runner_retains_complete_logs_securely(self):
        maven=self.project/'mvnw'
        maven.write_text('#!/bin/sh\nexit 0\n');maven.chmod(0o755)
        output='Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\n'+'X'*20000
        with patch.object(backend_runner,'installed_jdks',return_value=[]), \
             patch.object(backend_runner.subprocess,'run',return_value=subprocess.CompletedProcess(['mvn'],0,output,'')):
            result=backend_runner.run(self.root,'backend',goal='test',offline=False,allow_network=False)
        self.assertEqual(result['status'],'pass')
        self.assertLess(len(result['evidence']['stdout_tail']),len(output))
        log_path=self.root/result['log_path']
        self.assertTrue(log_path.is_file())
        self.assertIn('X'*20000,log_path.read_text())
        if os.name=='posix':self.assertEqual(stat.S_IMODE(log_path.stat().st_mode),0o600)

    def test_environment_fingerprint_detects_toolchain_change(self):
        wrapper=self.project/'mvnw';wrapper.write_text('v1');wrapper.chmod(0o755)
        java=self.root/'java';java.write_text('java');java.chmod(0o755)
        with patch.object(efficient.shutil,'which',side_effect=lambda cmd:str(java) if cmd=='java' else None):
            initial=REAL_ENV_FINGERPRINT(self.root,'backend')
            self.assertIsNotNone(initial)
            wrapper.write_text('v2-compiler')
            changed=REAL_ENV_FINGERPRINT(self.root,'backend')
            self.assertNotEqual(initial,changed)

    def test_backend_html_fixture_is_not_web_app(self):
        areas,_=verification_planner.infer_path_areas(['backend/src/test/resources/fixtures/sample.html'])
        self.assertNotIn('web',areas)
        self.assertIn('backend',areas)
        areas,_=verification_planner.infer_path_areas(['website/src/app/index.html'])
        self.assertIn('web',areas)

    def test_nested_web_check_logs_are_compacted_for_success_and_failure(self):
        raw={
            'status':'fail',
            'project':'frontend',
            'checks':{
                'test':{'exit_code':1,'stdout':'x'*5000,'stderr':'specific failure '+('y'*4000)+' token=secretvalue',
                        'log_path':'.agentic/local/verification-logs/test.log'},
                'build':{'exit_code':0,'stdout':'z'*5000,'stderr':''},
            },
        }
        compacted=vm.compact_tool_response('run_project_web_tests',raw,{})
        test=compacted['checks']['test']
        build=compacted['checks']['build']
        self.assertNotIn('stdout',test)
        self.assertLessEqual(len(test['stderr_excerpt']),efficient.MAX_TOOL_DIAGNOSTIC)
        self.assertIn('[REDACTED]',test['stderr_excerpt'])
        self.assertNotIn('secretvalue',test['stderr_excerpt'])
        self.assertEqual(test['log_path'],'.agentic/local/verification-logs/test.log')
        self.assertNotIn('stdout',build)
        self.assertNotIn('stderr_excerpt',build)
        self.assertLess(len(json.dumps(compacted)),len(json.dumps(raw))//3)


if __name__=='__main__':unittest.main()
