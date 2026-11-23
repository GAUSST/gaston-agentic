#!/usr/bin/env python3
"""Offline regression tests for direct Lead Git and deletion task-finalization."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import lead_git
import verification_mcp as vm
import acceptance_audit
import task_runtime


def git(repo,*args):
    result=subprocess.run(['git',*args],cwd=repo,capture_output=True,text=True,timeout=10)
    if result.returncode:raise AssertionError((args,result.stderr))
    return result.stdout.strip()


class GitCapabilities(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.backend=self.root/'services'/'payments'/'backend'
        self.backend.mkdir(parents=True)
        git(self.backend,'init','-q');git(self.backend,'config','user.name','Test')
        git(self.backend,'config','user.email','test@example.com')
        self.repo='services/payments/backend'

    def commit(self,paths,message='test: a scoped change',explicit=True):
        return lead_git.commit(self.root,repository=self.repo,paths=paths,message=message,
                               explicit_user_request=explicit,requested_by_user='Please commit those exact files')

    def test_nested_unborn_git_detected_without_initial_commit(self):
        result=lead_git.inspect(self.root)
        self.assertEqual(result['repository_count'],1)
        self.assertEqual(result['repositories'][0]['repository'],self.repo)
        self.assertFalse(result['repositories'][0]['head_exists'])

    def test_commit_requires_user_request_and_succeeds_on_untracked(self):
        (self.backend/'first.txt').write_text('first')
        result=self.commit(['first.txt'],explicit=False)
        self.assertEqual(result['status'],'blocked')
        self.assertFalse(lead_git.inspect(self.root)['repositories'][0]['head_exists'])
        result=self.commit(['first.txt'])
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['committed_files'],['first.txt'])
        self.assertTrue(result['commit'])
        self.assertEqual(git(self.backend,'show','--format=','--name-only','HEAD'),'first.txt')

    def test_preexisting_staged_file_blocks_without_mutation(self):
        (self.backend/'a.txt').write_text('a');self.assertEqual(self.commit(['a.txt'])['status'],'pass')
        (self.backend/'b.txt').write_text('b');git(self.backend,'add','b.txt')
        (self.backend/'a.txt').write_text('aa')
        result=self.commit(['a.txt'])
        self.assertEqual(result['status'],'blocked')
        self.assertIn('b.txt',result['staged_paths'])
        self.assertEqual(git(self.backend,'show','--format=','--name-only','HEAD'),'a.txt')

    def test_already_staged_selected_file_can_be_committed(self):
        (self.backend/'one.txt').write_text('first')
        git(self.backend,'add','one.txt')
        result=self.commit(['one.txt'])
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['committed_files'],['one.txt'])

    def test_commit_one_file_leaves_unrelated_untracked_untouched(self):
        (self.backend/'only.txt').write_text('1');(self.backend/'ignore.txt').write_text('2')
        result=self.commit(['only.txt'])
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['committed_files'],['only.txt'])
        self.assertTrue((self.backend/'ignore.txt').exists())
        self.assertIn('ignore.txt',git(self.backend,'ls-files','--others','--exclude-standard'))

    def test_path_escape_and_directory_commit_denied(self):
        (self.backend/'a').mkdir();(self.backend/'a'/'safe').write_text('a')
        self.assertEqual(self.commit(['../outside'])['status'],'blocked')
        self.assertEqual(self.commit(['a'])['status'],'blocked')

    def test_status_detects_modified_added_deleted_and_untracked(self):
        (self.backend/'existing.txt').write_text('exists')
        (self.backend/'deleted.txt').write_text('delete')
        git(self.backend,'add','existing.txt','deleted.txt');git(self.backend,'commit','-m','seed')
        (self.backend/'existing.txt').write_text('modified')
        (self.backend/'deleted.txt').unlink()
        (self.backend/'untracked.txt').write_text('new')
        item=lead_git.inspect(self.root,diff=True)['repositories'][0]
        self.assertEqual(item['counts']['modified'],1)
        self.assertEqual(item['counts']['deleted'],1)
        self.assertEqual(item['counts']['untracked'],1)
        self.assertIn('existing.txt',item['diff'])

    def test_inspection_does_not_change_git_state(self):
        (self.backend/'a.txt').write_text('a')
        before=git(self.backend,'status','--porcelain')
        self.assertEqual(lead_git.inspect(self.root,diff=True)['status'],'ready')
        after=git(self.backend,'status','--porcelain')
        self.assertEqual(before,after)


class DeletionFinalization(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.tasks=self.root/'.agentic'/'runs'
        self.tasks.mkdir(parents=True)
        self.folder=self.tasks/'remove-old-info-001'
        self.folder.mkdir()
        self.deleted=['backend/src/main/java/example/InfoController.java',
                      'backend/src/test/java/example/InfoControllerTest.java']
        (self.folder/'task.json').write_text(json.dumps({'goal':'Delete GET /api/info endpoint'}))
        (self.folder/'build.json').write_text(json.dumps({'status':'done','changed_files':self.deleted}))
        (self.folder/'state.json').write_text(json.dumps({'stage':'awaiting_lead_review'}))
        (self.folder/'worker-finished.json').write_text(json.dumps({'finished_at':time.time()-30}))
        self.patched=patch.object(vm,'ROOT',self.root)
        self.patched.start()
        self.addCleanup(self.patched.stop)
        self.auth=patch.object(vm,'_authorized_delegate',return_value=True)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        # This fixture models an already-reviewed actual API runner outcome;
        # v35 cannot finalize endpoint deletions from JUnit evidence alone.
        import eng
        self.config=patch.object(eng,'load_config',return_value={'project':{},'checks':[]})
        self.config.start();self.addCleanup(self.config.stop)
        backend=self.root/'backend';backend.mkdir();(backend/'pom.xml').write_text('<project/>')
        launcher=backend/'src/test/java/example/E2eLauncher.java';launcher.parent.mkdir(parents=True);launcher.write_text('class E2eLauncher {}')
        (backend/'http.json').write_text(json.dumps({'cases':[{'path':'/health','expected_status':200,'expect_json':{'ok':True}}]}))
        (backend/'gaston-e2e.json').write_text(json.dumps({'main_class':'example.E2eLauncher','suite':'http.json','readiness_path':'/health'}))

    def record_api(self):
        acceptance_audit.record(self.folder,'api',{'status':'pass','project':'backend','managed_application':True,'exit_code':0},parameters={'project':'backend'})

    def done(self,**kwargs):
        return vm.finalize_delegated_task('remove-old-info-001',reviewed=True,
            review_notes='Confirmed both info files are absent; remaining endpoint tests passed.',
            deleted_paths=self.deleted,**kwargs)

    def test_no_task_test_evidence_is_not_a_pass(self):
        result=self.done()
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(task_runtime.read_record(self.folder/'state.json')['stage'],'awaiting_lead_review')

    def test_post_builder_real_recorded_pass_completes_deletion(self):
        acceptance_audit.record(self.folder,'backend',{'status':'pass','evidence':{'stdout_tail':'Tests run: 5'}},
                                parameters={'project':'backend','test_filter':'HealthControllerTest,PingControllerTest'})
        self.record_api()
        result=self.done(project='backend',test_filter='HealthControllerTest,PingControllerTest')
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['verification_source'],'recorded_task_acceptance')
        self.assertEqual(task_runtime.read_record(self.folder/'state.json')['stage'],'ready_for_human_release_review')
        self.assertEqual(task_runtime.read_record(self.folder/'lead-backend-verification.json')['confirmed_deleted_paths'],self.deleted)

    def test_missing_task_record_can_run_focused_backend_check(self):
        output={'status':'pass','evidence':{'stdout_tail':'Tests run: 3, Failures: 0, Errors: 0, Skipped: 0',
                                         'stderr_tail':''}}
        self.record_api()
        with patch.object(vm,'run_backend_tests',return_value=output) as test_call:
            result=self.done(project='backend',test_filter='HealthControllerTest,PingControllerTest')
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['verification_source'],'fresh_targeted_backend_tests')
        self.assertEqual(len(acceptance_audit.read(self.folder)),2)
        test_call.assert_called_once_with(project='backend',goal='test',test_filter='HealthControllerTest,PingControllerTest')

    def test_success_without_executed_junit_is_blocked(self):
        with patch.object(vm,'run_backend_tests',return_value={'status':'pass','evidence':{'stdout_tail':'BUILD SUCCESS'}}):
            result=self.done(project='backend',test_filter='HealthControllerTest')
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(task_runtime.read_record(self.folder/'state.json')['stage'],'awaiting_acceptance')

    def test_reported_deletion_still_exists_blocks(self):
        target=self.root/self.deleted[0];target.parent.mkdir(parents=True)
        target.write_text('not deleted')
        acceptance_audit.record(self.folder,'backend',{'status':'pass'},parameters={'project':'backend'})
        self.assertEqual(self.done()['status'],'blocked')

    def test_old_pass_before_builder_finished_is_not_reused(self):
        acceptance_audit.record(self.folder,'backend',{'status':'pass'},parameters={'project':'backend'})
        (self.folder/'worker-finished.json').write_text(json.dumps({'finished_at':time.time()+10}))
        self.assertEqual(self.done()['status'],'blocked')

    def test_superseded_prior_task_keeps_audit_evidence(self):
        prior=self.tasks/'add-old-info-001';prior.mkdir()
        (prior/'task.json').write_text(json.dumps({'goal':'Create the old info endpoint'}))
        (prior/'build.json').write_text(json.dumps({'status':'done','changed_files':self.deleted}))
        (prior/'state.json').write_text(json.dumps({'stage':'ready_for_human_release_review'}))
        (prior/'lead-backend-verification.json').write_text('{"outcome":{"status":"pass"}}')
        acceptance_audit.record(self.folder,'backend',{'status':'pass'},parameters={'project':'backend'})
        self.record_api()
        result=self.done(project='backend',supersedes_task_id='add-old-info-001')
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['superseded_task'],'add-old-info-001')
        self.assertEqual(task_runtime.read_record(prior/'state.json')['stage'],'superseded')
        self.assertTrue((prior/'lead-backend-verification.json').exists())


if __name__=='__main__':unittest.main()
