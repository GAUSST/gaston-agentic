"""v35 verification regressions; no live provider or sample-project app assumed."""
from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import acceptance_audit as audit
import documentation_review as docs
import eng
import managed_backend as runtime
import task_runtime
import verification_brief
import verification_mcp as vm
import live_builder_worker as worker


def write_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data),encoding='utf8')


def make_fixture(project):
    project.mkdir(parents=True,exist_ok=True)
    (project/'pom.xml').write_text('<project/>')
    source=project/'src/test/java/example/E2eLauncher.java'
    source.parent.mkdir(parents=True,exist_ok=True)
    source.write_text('class E2eLauncher {}')
    suite=project/'src/test/http.json'
    write_json(suite,{'cases':[
        {'name':'actual readiness','path':'/health','expected_status':200,'expect_json':{'status':'UP','profile':'gaston-e2e','isolated':True}},
        {'name':'issue synthetic session','method':'POST','path':'/session','allow_local_mutation':True,'json_body':{'user':'test'},'expected_status':201,'capture_json':{'token':'accessToken'},'expect_json':{'user':'test'}},
        {'name':'denied','path':'/private','expected_status':401,'expect_json':{'error':'unauthorized'}},
        {'name':'authorized','path':'/private','expected_status':200,'headers':{'Authorization':'Bearer ${token}'},'expect_json':{'user':'test'}}]})
    write_json(project/'gaston-e2e.json',{'main_class':'example.E2eLauncher','suite':'src/test/http.json','readiness_path':'/health'})
    return suite


class ReviewAndAcceptanceTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup);self.root=Path(td.name)
        self.folder=self.root/'.agentic/runs/checkpoint-one-001';self.folder.mkdir(parents=True)
        make_fixture(self.root/'backend')
        self.source='backend/src/main/java/example/IdentityController.java';self.doc='backend/docs/API_IDENTITY.md';self.spec='first_data/REQUIREMENTS.md'
        for rel,value in [(self.source,'class IdentityController {}'),(self.doc,'identity API contract'),(self.spec,'requirement: independently rotatable secrets')]:
            path=self.root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
        write_json(self.folder/'task.json',{'goal':'Checkpoint 1 customer identity'})
        write_json(self.folder/'build.json',{'status':'done','changed_files':[self.source,self.doc]})
        write_json(self.folder/'state.json',{'stage':'awaiting_lead_review'})
        write_json(self.folder/'worker-finished.json',{'finished_at':time.time()-1})
        stack=contextlib.ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(vm,'ROOT',self.root))
        stack.enter_context(patch.object(vm,'_authorized_delegate',return_value=True))
        stack.enter_context(patch.object(eng,'load_config',return_value={'project':{},'checks':[]}))

    def backend(self,selector=None):
        audit.record(self.folder,'backend',{'status':'pass','project':'backend','evidence':{'stdout_tail':'Tests run: 64, Failures: 0, Errors: 0, Skipped: 0'}},parameters={'project':'backend','test_filter':selector})

    def http(self,managed=True):
        audit.record(self.folder,'api',{'status':'pass','project':'backend','managed_application':managed,'exit_code':0},parameters={'project':'backend'})

    def finish(self,notes=None,gaps=None):
        return vm.finalize_delegated_task(self.folder.name,reviewed=True,review_notes='Reviewed implementation and current assertions',documentation_reviews=notes,documentation_gaps=gaps)

    def test_checkpoint_needs_actual_http_full_suite_and_documents(self):
        self.backend('IdentityTest');self.http(False)
        self.assertEqual(self.finish()['status'],'blocked')
        plan=vm.task_verification_plan(self.folder.name)
        self.assertIn('api_integration',plan['managed_api_gates'])
        self.assertIn('backend_verify',plan['full_backend_gates'])
        result=self.finish({self.doc:'Read the contract and compared request/response'},[])
        self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(set(result['acceptance']['unverified_planned_gates']),{'backend_verify','api_integration'})
        self.backend();self.http()
        with patch.object(vm,'run_backend_tests',side_effect=AssertionError('No duplicate execution')):
            result=self.finish()
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(vm.delegation_status(self.folder.name)['all_passed'],True)

    def test_v38_real_gate_requires_current_source_review_in_addition_to_tests(self):
        task=task_runtime.read_record(self.folder/'task.json');task['code_review_policy']=38
        write_json(self.folder/'task.json',task)
        self.backend();self.http()
        docnotes={self.doc:'Read the complete identity contract'}
        blocked=self.finish(docnotes,[])
        self.assertEqual(blocked['status'],'blocked',blocked)
        self.assertEqual(blocked['code_review']['missing_files'],[self.source])
        notes={self.source:{'clarity':'IdentityController uses domain naming','difficult_logic':'Empty fixture, no tricky invariant','maintainability':'No redundant abstraction in fixture','correctness':'Inspected actual current fixture'}}
        self.assertEqual(vm.CALLS['record_code_review']({'task_id':self.folder.name,'reviews':notes,'findings':[]})['review']['status'],'pass')
        self.assertEqual(self.finish()['status'],'pass')
        self.assertEqual(vm.delegation_status(self.folder.name)['all_passed'],True)
        self.assertEqual(task_runtime.read_record(self.folder/'state.json')['acceptance_policy'],38)
        (self.root/self.source).write_text('class IdentityController { int changed; }')
        self.assertFalse(vm.delegation_status(self.folder.name)['all_passed'])

    def test_document_findings_and_stale_review_cannot_finalize(self):
        self.backend();self.http()
        notes={self.doc:'Read the full identity contract',self.spec:'Reviewed separate key requirement'}
        result=self.finish(notes,['OTP key rotation invalidates staff MFA encryption'])
        self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(docs.report(self.folder)['unresolved_gaps'],['OTP key rotation invalidates staff MFA encryption'])
        (self.root/self.source).write_text('class IdentityController { /* corrected */ }')
        self.assertEqual(audit.current(self.folder),[])
        self.assertEqual(docs.report(self.folder)['status'],'incomplete')
        self.assertEqual(self.finish({self.doc:'Updated contract reviewed'},[])['status'],'blocked')
        # Re-read both documents and verify the corrected revision instead.
        result=self.finish({self.doc:'Confirmed separate key configuration',self.spec:'Confirmed independent rotation against requirement'},[])
        self.assertEqual(result['status'],'blocked')
        self.backend();self.http()
        self.assertEqual(self.finish()['status'],'pass')
        (self.root/self.spec).write_text('Specification changed after final review')
        self.assertEqual(docs.report(self.folder)['status'],'incomplete')
        self.assertFalse(vm.delegation_status(self.folder.name)['all_passed'])

    def test_missing_document_updates_gap_even_when_builder_reports_no_docs(self):
        write_json(self.folder/'build.json',{'status':'done','changed_files':[self.source]})
        self.backend();self.http()
        result=self.finish({},['API contract missing required documentation update'])
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(self.finish()['status'],'blocked')

    def test_api_pass_invalidated_by_changed_source(self):
        self.http();self.assertEqual(len(audit.current(self.folder)),1)
        (self.root/self.source).write_text('changed')
        self.assertEqual(audit.current(self.folder),[])

    def test_unsafe_revision_fingerprint_never_reuses_evidence(self):
        self.backend()
        write_json(self.folder/'build.json',{'status':'done','changed_files':['../secret']})
        self.assertEqual(audit.current(self.folder),[])

    def test_pure_helper_does_not_automatically_require_http(self):
        plan=verification_brief.produce(self.root,{'goal':'Fix slug helper'},{'changed_files':['backend/src/main/java/example/util/SlugHelper.java']},{'checks':[],'project':{}})
        self.assertEqual(plan['managed_api_gates'],[])
        self.assertEqual([c['name'] for c in plan['selected_checks']],['backend_verify'])

    def test_interrupted_implementation_cannot_finalize(self):
        write_json(self.folder/'build.json',{'status':'blocked','interrupted':True,'changed_files':[self.source,self.doc]})
        write_json(self.folder/'state.json',{'stage':'blocked','resumable':True})
        self.backend();self.http()
        self.assertEqual(vm.verify_delegated_backend_task(self.folder.name,'backend','IdentityTest',True,'Looked at code',reviewed_files=[self.source])['status'],'blocked')
        self.assertEqual(self.finish({self.doc:'read'},[])['status'],'blocked')


@unittest.skipUnless(os.name=='posix' and shutil.which('curl'), 'POSIX and curl required')
class ManagedBackendTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup);self.root=Path(td.name)
        self.project=self.root/'backend';self.suite=make_fixture(self.project)
        # Deliberate fake Maven executable starts an actual ephemeral HTTP
        # server. Proves lifecycle/curl behavior, not Spring/JVM compatibility.
        script='''#!/usr/bin/env python3
import os, json
from urllib.parse import urlparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
base=urlparse(os.environ['AGENTIC_TEST_API_BASE_URL'])
Path('fixture-pid').write_text(str(os.getpid()))
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def reply(self,code,body):
  self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(body).encode())
 def do_GET(self):
  if self.path=='/health':self.reply(200,{'status':'UP','profile':os.environ['SPRING_PROFILES_ACTIVE'],'isolated':'JWT_SIGNING_JWK' not in os.environ})
  elif self.headers.get('Authorization')=='Bearer synthetic-only':self.reply(200,{'user':'test'})
  else:self.reply(401,{'error':'unauthorized'})
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length',0)))
  self.reply(201,{'accessToken':'synthetic-only','user':'test'})
ThreadingHTTPServer(('127.0.0.1',base.port),H).serve_forever()
'''
        launcher=self.project/'mvnw';launcher.write_text(script);launcher.chmod(0o755)
        stack=contextlib.ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(runtime.backend_runner,'installed_jdks',return_value=[{'home':'/usr','version':25}]))
        stack.enter_context(patch.dict(os.environ,{'JWT_SIGNING_JWK':'must-not-inherit','DOCKER_HOST':'unix:///var/run/docker.sock'}))

    def run_check(self):
        return runtime.run(self.root,'backend',startup_timeout=4,suite_timeout=8)

    def test_live_curl_state_and_owned_cleanup(self):
        result=self.run_check();self.assertEqual(result['status'],'pass',result)
        self.assertEqual(result['process_cleanup'],'complete');self.assertTrue(result['managed_application'])
        self.assertIn('4/4 passed',result['summary']);self.assertNotIn('synthetic-only',result['summary'])
        pid=int((self.project/'fixture-pid').read_text())
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    def test_failed_assertion_blocks_and_cleans_up(self):
        cases=json.loads(self.suite.read_text());cases['cases'][-1]['expected_status']=403;write_json(self.suite,cases)
        result=self.run_check();self.assertEqual(result['status'],'fail',result)
        self.assertEqual(result['process_cleanup'],'complete')
        self.assertTrue((self.root/result['startup_diagnostic_log']).is_file())

    def test_stateful_driver_failure_blocks(self):
        driver=self.project/'src/test/scenario.py';driver.write_text('raise SystemExit(1)')
        cfg=json.loads((self.project/'gaston-e2e.json').read_text());cfg['scenario_driver']='src/test/scenario.py';write_json(self.project/'gaston-e2e.json',cfg)
        result=self.run_check();self.assertEqual(result['status'],'fail',result)
        self.assertEqual(result['scenario_driver_exit_code'],1);self.assertEqual(result['process_cleanup'],'complete')

    def test_stateful_driver_uses_bounded_project_budget(self):
        driver=self.project/'src/test/scenario.py'
        driver.write_text('raise SystemExit(0)')
        cfg=json.loads((self.project/'gaston-e2e.json').read_text())
        cfg.update(scenario_driver='src/test/scenario.py', scenario_driver_timeout_seconds=600)
        write_json(self.project/'gaston-e2e.json',cfg)
        original_run=runtime.subprocess.run
        observed=[]
        def observed_run(cmd,*args,**kwargs):
            if isinstance(cmd,list) and len(cmd)>1 and str(cmd[1])==str(driver):
                observed.append(kwargs.get('timeout'))
            return original_run(cmd,*args,**kwargs)
        with patch.object(runtime.subprocess,'run',side_effect=observed_run):
            result=self.run_check()
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(observed,[600])
        self.assertEqual(result['scenario_driver_timeout_seconds'],600)

    def test_stateful_driver_invalid_budget_is_rejected(self):
        driver=self.project/'src/test/scenario.py';driver.write_text('raise SystemExit(0)')
        cfg=json.loads((self.project/'gaston-e2e.json').read_text())
        cfg['scenario_driver']='src/test/scenario.py'
        for bad in (-1,0,601,True,'600',None,1.5):
            with self.subTest(budget=bad):
                cfg['scenario_driver_timeout_seconds']=bad
                write_json(self.project/'gaston-e2e.json',cfg)
                result=self.run_check()
                self.assertEqual(result['status'],'blocked',result)
                self.assertIn('scenario_driver_timeout_seconds',result['reason'])
                self.assertFalse(result['managed_application'])

    def test_stateful_driver_default_budget_remains_unchanged(self):
        driver=self.project/'src/test/scenario.py';driver.write_text('raise SystemExit(0)')
        cfg=json.loads((self.project/'gaston-e2e.json').read_text());cfg['scenario_driver']='src/test/scenario.py'
        write_json(self.project/'gaston-e2e.json',cfg)
        self.assertEqual(runtime.scenario_timeout(self.project),240)
        self.assertEqual(runtime.scenario_timeout(self.project,8),8)

    def test_missing_test_launcher_blocks(self):
        (self.project/'src/test/java/example/E2eLauncher.java').unlink()
        result=self.run_check();self.assertEqual(result['status'],'blocked');self.assertFalse(result['managed_application'])

    def test_remote_docker_blocked(self):
        with patch.dict(os.environ,{'DOCKER_HOST':'tcp://remote.example:2376'}):
            self.assertEqual(self.run_check()['status'],'blocked')


class CompletionAndRecoveryTests(unittest.TestCase):
    def test_public_short_override_does_not_wake_lead(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'.agentic/runs/wait-task-001';folder.mkdir(parents=True)
            write_json(folder/'task.json',{});write_json(folder/'state.json',{'stage':'implementing'})
            child=subprocess.Popen([sys.executable,'-c',"import time,pathlib;time.sleep(1.3);pathlib.Path('state.json').write_text('{\"stage\":\"awaiting_lead_review\"}')"],cwd=folder)
            try:
                write_json(folder/'worker.json',{'pid':child.pid,'started_at':time.time()})
                with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True):
                    result=vm.wait_for_builder(folder.name,timeout_seconds=1)
                self.assertTrue(result['builder_finished'],result);self.assertEqual(result['toolkit_release'],vm.TOOLKIT_RELEASE)
                self.assertGreater(result['elapsed_wait_seconds'],1)
                first=json.loads((folder/'wait-events.jsonl').read_text().splitlines()[0]);self.assertEqual(first['effective_timeout'],3600)
                schema=next(t for t in vm.TOOLS if t['name']=='wait_for_builder')['inputSchema']
                self.assertNotIn('timeout_seconds',schema['properties'])
            finally:child.wait(timeout=5)

    def test_claude_timeout_env_matches_server(self):
        with patch.dict(os.environ,{'MCP_TOOL_TIMEOUT':'120000'}):
            env=eng.claude_advisor_env('reviewer',{'models':{'claude_advisor':'off'}})
        self.assertGreaterEqual(int(env['MCP_TOOL_TIMEOUT']),task_runtime.MCP_TOOL_TIMEOUT_SECONDS*1000)

    def test_quota_failure_preserves_reported_partial_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'.agentic/runs/interrupted-001';folder.mkdir(parents=True)
            write_json(folder/'build.json',{'status':'done','changed_files':['backend/a.java']})
            (folder/'build.log').write_text('ERROR usage limit reached')
            with patch.object(eng,'run_dir',return_value=folder),patch.object(eng,'ROOT',root),patch.object(worker,'changed_paths',return_value=['backend/a.java','backend/b.java']):
                worker.interrupted(folder.name,RuntimeError('provider exit'))
            state=task_runtime.read_record(folder/'state.json');built=task_runtime.read_record(folder/'build.json')
            self.assertTrue(state['resumable']);self.assertEqual(state['blocker'],'provider_quota')
            self.assertEqual(built['changed_files'],['backend/a.java','backend/b.java']);self.assertTrue(built['interrupted'])


if __name__=='__main__':unittest.main()
