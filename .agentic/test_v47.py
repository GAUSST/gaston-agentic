"""Offline-first Builder dependency resolution and strict compile receipt gates."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backend_runner
import builder_compile
import orchestration


class BuilderNetworkPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.project=self.root/'backend';self.project.mkdir()
        (self.project/'pom.xml').write_text('<project/>')

    def test_executable_project_wrapper_wins_over_system_maven(self):
        self._add_maven_wrapper_properties()
        wrapper=self.project/'mvnw'
        wrapper.write_text('#!/bin/sh\nexit 0\n');wrapper.chmod(0o755)
        dist=(self.root/'.agentic/local/backend-dependencies/maven-user/wrapper/dists/'
              'apache-maven-3.9.9-bin/hash/apache-maven-3.9.9/bin/mvn')
        dist.parent.mkdir(parents=True);dist.write_text('#!/bin/sh\nexit 0\n')
        seen={}
        def run(argv,**kwargs):
            seen['argv']=argv
            return subprocess.CompletedProcess(argv,0,stdout='BUILD SUCCESS',stderr='')
        with patch.object(backend_runner,'installed_jdks',return_value=[]), \
             patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn'), \
             patch.object(backend_runner.subprocess,'run',side_effect=run):
            result=backend_runner.run(self.root,'backend',goal='test-compile',offline=True,allow_network=False)
        self.assertEqual(result['status'],'pass')
        self.assertEqual(seen['argv'][0],str(wrapper))
        self.assertEqual(result['runner'],'mvnw')

    def _add_maven_wrapper_properties(self):
        properties=self.project/'.mvn/wrapper/maven-wrapper.properties'
        properties.parent.mkdir(parents=True)
        properties.write_text('distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.zip\n')
        return properties

    def test_unconfigured_wrapper_is_blocked_offline(self):
        wrapper=self.project/'mvnw';wrapper.write_text('#!/bin/sh\nexit 0\n');wrapper.chmod(0o755)
        with patch.object(backend_runner,'installed_jdks',return_value=[]), patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn'), patch.object(backend_runner.subprocess,'run') as run:
            result=backend_runner.run(self.root,'backend',goal='test-compile',offline=True,allow_network=False)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason_code'],'DEPENDENCY_RESOLUTION_UNAVAILABLE')
        run.assert_not_called()

    def test_cached_wrapper_distribution_allows_offline_execution(self):
        self._add_maven_wrapper_properties()
        wrapper=self.project/'mvnw';wrapper.write_text('#!/bin/sh\nexit 0\n');wrapper.chmod(0o755)
        dist=(self.root/'.agentic/local/backend-dependencies/maven-user/wrapper/dists/'
              'apache-maven-3.9.9-bin/hash/apache-maven-3.9.9/bin/mvn')
        dist.parent.mkdir(parents=True);dist.write_text('#!/bin/sh\nexit 0\n')
        seen={}
        def execute(argv,**kwargs):
            seen['argv']=argv
            return subprocess.CompletedProcess(argv,0,stdout='BUILD SUCCESS',stderr='')
        with patch.object(backend_runner,'installed_jdks',return_value=[]), \
             patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn'), \
             patch.object(backend_runner.subprocess,'run',side_effect=execute):
            result=backend_runner.run(self.root,'backend',goal='test-compile',offline=True,allow_network=False)
        self.assertEqual(result['status'],'pass')
        self.assertIn('-o',seen['argv'])

    def test_missing_wrapper_distribution_is_not_downloaded_without_authorization(self):
        self._add_maven_wrapper_properties()
        wrapper=self.project/'mvnw';wrapper.write_text('#!/bin/sh\nexit 0\n');wrapper.chmod(0o755)
        with patch.object(backend_runner,'installed_jdks',return_value=[]), \
             patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn'), \
             patch.object(backend_runner.subprocess,'run') as run:
            result=backend_runner.run(self.root,'backend',goal='test-compile',offline=True,allow_network=False)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason_code'],'DEPENDENCY_RESOLUTION_UNAVAILABLE')
        run.assert_not_called()

    def test_authorized_fallback_can_download_missing_wrapper_and_compile(self):
        self._add_maven_wrapper_properties()
        wrapper=self.project/'mvnw';wrapper.write_text('#!/bin/sh\nexit 0\n');wrapper.chmod(0o755)
        seen={}
        def execute(argv,**kwargs):
            seen['argv']=argv
            return subprocess.CompletedProcess(argv,0,stdout='BUILD SUCCESS',stderr='')
        with patch.object(backend_runner,'installed_jdks',return_value=[]), \
             patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn'), \
             patch.object(backend_runner.subprocess,'run',side_effect=execute):
            result=builder_compile.check(self.root,'backend',allow_network=True,approval_quote='Approved for this test')
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['resolution_mode'],'online_fallback')
        self.assertEqual(seen['argv'][0],str(wrapper))
        self.assertNotIn('-o',seen['argv'])

    def test_default_compile_is_offline_and_never_authorizes_network(self):
        blocked={'status':'blocked','reason_code':'DEPENDENCY_RESOLUTION_UNAVAILABLE',
                 'reason':'artifact missing in offline cache','runner':'mvnw'}
        with patch.object(backend_runner,'run',return_value=blocked) as run:
            result=builder_compile.check(self.root,'backend')
        run.assert_called_once_with(self.root,'backend',goal='test-compile',offline=True,
                                    allow_network=False,timeout=300)
        self.assertEqual(result['status'],'blocked')
        self.assertTrue(result['network_authorization_required'])
        self.assertFalse(result['network_authorized'])
        self.assertEqual(result['resolution_mode'],'offline_cache')

    def test_explicit_authorization_retries_online_once_after_dependency_block(self):
        offline={'status':'blocked','reason_code':'DEPENDENCY_RESOLUTION_UNAVAILABLE',
                 'reason':'parent POM not cached','runner':'mvnw','elapsed_seconds':1.2}
        online={'status':'pass','runner':'mvnw','exit_code':0,'elapsed_seconds':8.4,'network_allowed':True,
                'network_usage':'download_observed','network_attempted':True,
                'network_scope':'Maven dependency resolution only'}
        with patch.object(backend_runner,'run',side_effect=[offline,online]) as run:
            result=builder_compile.check(self.root,'backend',allow_network=True,approval_quote='Approved for this test')
        self.assertEqual(run.call_count,2)
        self.assertEqual(run.call_args_list[0].kwargs,{'goal':'test-compile','offline':True,'allow_network':False,'timeout':300})
        self.assertEqual(run.call_args_list[1].kwargs,{'goal':'test-compile','offline':False,'allow_network':True,'timeout':300})
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['resolution_mode'],'online_fallback')
        self.assertTrue(result['network_authorized'])
        self.assertTrue(result['network_attempted'])
        self.assertEqual(result['offline_attempt']['status'],'blocked')

    def test_network_fallback_does_not_retry_cache_permission_failure(self):
        blocked={'status':'blocked','reason_code':'CACHE_UNAVAILABLE','reason':'read-only cache'}
        with patch.object(backend_runner,'run',return_value=blocked) as run:
            result=builder_compile.check(self.root,'backend',allow_network=True,approval_quote='Approved for this test')
        run.assert_called_once()
        self.assertEqual(result['reason_code'],'CACHE_UNAVAILABLE')
        self.assertNotIn('network_attempted',result)

    def test_network_fallback_does_not_retry_source_compile_failure(self):
        failed={'status':'fail','reason_code':None,'reason':'Java source compilation error'}
        with patch.object(backend_runner,'run',return_value=failed) as run:
            result=builder_compile.check(self.root,'backend',allow_network=True,approval_quote='Approved for this test')
        run.assert_called_once()
        self.assertEqual(result['status'],'fail')

    def test_online_fallback_stays_blocked_when_sandbox_denies_network(self):
        offline={'status':'blocked','reason_code':'DEPENDENCY_RESOLUTION_UNAVAILABLE','reason':'not cached'}
        online={'status':'blocked','reason_code':'DEPENDENCY_RESOLUTION_UNAVAILABLE','reason':'unknown host',
                'network_allowed':True,'network_usage':'request_observed','network_attempted':True}
        with patch.object(backend_runner,'run',side_effect=[offline,online]):
            result=builder_compile.check(self.root,'backend',allow_network=True,approval_quote='Approved for this test')
        self.assertEqual(result['status'],'blocked')
        self.assertTrue(result['network_attempted'])
        self.assertIn('sandbox',result['reason'])

    def test_maven_offline_origin_metadata_error_is_dependency_block(self):
        wrapper=self.project/'mvnw';wrapper.write_text('#!/bin/sh\nexit 1\n');wrapper.chmod(0o755)
        message='Cannot access central in offline mode and the artifact org.example:parent:pom:1 has not been downloaded from it before.'
        with patch.object(backend_runner,'installed_jdks',return_value=[]), \
             patch.object(backend_runner.subprocess,'run',return_value=subprocess.CompletedProcess([],1,stdout=message,stderr='')):
            result=backend_runner.run(self.root,'backend',goal='test-compile',offline=True,allow_network=False)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason_code'],'DEPENDENCY_RESOLUTION_UNAVAILABLE')

    def test_read_only_repository_is_not_misclassified_as_networkable(self):
        with patch.object(backend_runner,'installed_jdks',return_value=[]), \
             patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn'), \
             patch.object(backend_runner.subprocess,'run',return_value=subprocess.CompletedProcess([],1,stdout='Read-only file system',stderr='')):
            result=backend_runner.run(self.root,'backend',goal='test-compile',offline=True,allow_network=False)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason_code'],'CACHE_UNAVAILABLE')


class StrictCompileReceiptTests(unittest.TestCase):
    def test_only_fresh_pass_is_non_blocking(self):
        checks=[{'project':'a','status':'pass'}, {'project':'b','status':'blocked'},
                {'project':'c','status':'stale'}, {'project':'d','status':'fail'},
                {'project':'e','status':'not_run'}, {'project':'f','status':'mystery'}, {}]
        blockers=builder_compile.blocking_receipts(checks)
        self.assertEqual([x['status'] for x in blockers],
                         ['blocked','stale','fail','not_run','mystery','not_run'])

    def test_question_resolution_cannot_waive_blocked_or_stale_compile(self):
        for status in ('blocked','stale','fail','not_run','unknown'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);folder=root/'.agentic/runs/task-compile-gate';folder.mkdir(parents=True)
                orchestration.put(folder/'task.json',{'owner':'builder','write_scope':['backend/src/A.java']})
                orchestration.put(folder/'state.json',{'stage':'needs_lead_decision'})
                orchestration.put(folder/'build.json',{
                    'status':'done','questions':['Confirm?'],'changed_files':['backend/src/A.java'],
                    'compile_preflight':[{'project':'backend','status':status}],
                })
                result=orchestration.resolve_questions(root,folder.name,{'Confirm?':'Confirmed'},
                    implementation_complete=True,completion_evidence='Reviewed completed implementation')
                self.assertTrue(result['requires_revision'])
                self.assertEqual(orchestration.read_record(folder/'state.json')['stage'],'needs_lead_decision')

    def test_live_builder_handoff_blocks_non_pass_receipt(self):
        import context_brief, eng, live_builder_worker
        for status in ('blocked','stale','fail','not_run','unknown'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);backend=root/'backend';(backend/'src').mkdir(parents=True)
                (backend/'pom.xml').write_text('<project/>');(backend/'src/A.java').write_text('class A {}')
                folder=root/'.agentic/runs/demo-task';folder.mkdir(parents=True)
                orchestration.put(folder/'task.json',{'write_scope':['backend/src/A.java']})
                orchestration.put(folder/'worker.json',{'attempt':'a'*32})
                states=[]
                config={'preferences':{'builder':'codex','reviewer':'claude'},'project':str(root)}
                def implementation(*args,**kwargs):
                    return {'status':'done','questions':[],'summary':'implemented','changed_files':['backend/src/A.java'],'remaining_risks':[]}
                with patch.object(eng,'ROOT',root),patch.object(eng,'require_setup_complete'), \
                     patch.object(eng,'load_config',return_value=config), \
                     patch.object(eng,'SESSIONS',type('Sessions',(),{'get':lambda *a:{'id':'lead'}})()), \
                     patch.object(eng,'load_task',return_value={'goal':'Edit A','write_scope':['backend/src/A.java']}), \
                     patch.object(eng,'run_dir',return_value=folder), \
                     patch.object(eng,'save_state',side_effect=lambda rd,stage,**kw:states.append(stage)), \
                     patch.object(eng,'refresh_workspace_checks',return_value=config), \
                     patch.object(context_brief,'make',return_value={}), \
                     patch.object(context_brief,'render',return_value='workspace'), \
                     patch.object(eng,'provider_structured',side_effect=implementation), \
                     patch.object(eng.workspace_git,'existing_changes',return_value={}), \
                     patch.object(eng.workspace_git,'changed_existing_files',return_value=[]), \
                     patch.object(builder_compile,'receipts',return_value=[{'project':'backend','status':status}]), \
                     patch.object(eng,'write_summary'):
                    live_builder_worker.process('demo-task')
                build=orchestration.read_record(folder/'build.json')
                self.assertEqual(build['status'],'blocked')
                self.assertIn(status,build['questions'][0])
                self.assertEqual(states,['implementing','needs_lead_decision'])

    def test_builder_and_lead_prompts_require_explicit_network_authorization(self):
        import eng
        self.assertNotIn('--allow-network',eng.builder_runtime_brief())
        self.assertIn('prepare_builder_dependencies',eng.builder_runtime_brief())
        self.assertIn('developer approval',eng.builder_runtime_brief())
        self.assertIn('Never infer network consent',eng.lead_runtime_brief())


if __name__=='__main__':
    unittest.main()
