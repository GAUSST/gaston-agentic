"""Offline-by-default regression coverage for managed backend verification."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backend_runner
import managed_backend
import verification_mcp as vm


class BackendRunnerOfflinePolicyTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.root=Path(td.name).resolve()
        self.project=self.root/'backend';self.project.mkdir()
        (self.project/'pom.xml').write_text('<project/>')
        self.calls=[]
        def run(argv,**kwargs):
            self.calls.append((argv,kwargs))
            return subprocess.CompletedProcess(argv,0,stdout='Tests run: 1, Failures: 0, Errors: 0, Skipped: 0',stderr='')
        self.stack=patch.object(backend_runner,'project_state',side_effect=lambda root:Path(root)/'.agentic')
        self.stack.start();self.addCleanup(self.stack.stop)
        self.jdks=patch.object(backend_runner,'installed_jdks',return_value=[])
        self.jdks.start();self.addCleanup(self.jdks.stop)
        import platform_compat
        self.subproc=patch.object(platform_compat,'prepare_subprocess',side_effect=lambda argv,env:(argv,env,{}))
        self.subproc.start();self.addCleanup(self.subproc.stop)
        self.run=patch.object(backend_runner.subprocess,'run',side_effect=run)
        self.run.start();self.addCleanup(self.run.stop)
        self.maven=patch.object(backend_runner.shutil,'which',return_value='/usr/bin/mvn')
        self.maven.start();self.addCleanup(self.maven.stop)
        self.run_env=patch.dict(os.environ,{'HTTP_PROXY':'http://proxy.invalid:8080','HTTPS_PROXY':'http://proxy.invalid:8080','NO_PROXY':'*'})
        self.run_env.start();self.addCleanup(self.run_env.stop)

    def test_backend_runner_defaults_to_offline_and_strips_proxy_environment(self):
        result=backend_runner.run(self.root,'backend')
        self.assertEqual(result['status'],'pass',result)
        argv,kwargs=self.calls[-1]
        self.assertIn('-o',argv)
        self.assertFalse(result['network_allowed'])
        self.assertEqual(result['network_policy'],'offline')
        self.assertEqual(result['network_usage'],'not_used_by_build_tool')
        self.assertNotIn('HTTP_PROXY',kwargs['env'])
        self.assertNotIn('HTTPS_PROXY',kwargs['env'])
        self.assertNotIn('NO_PROXY',kwargs['env'])

    def test_dependency_activity_is_reported_separately_from_permission(self):
        self.assertEqual(backend_runner.dependency_network_usage('Downloaded from central','',True),'download_observed')
        self.assertEqual(backend_runner.dependency_network_usage('Downloading from central','',True),'request_observed')
        self.assertEqual(backend_runner.dependency_network_usage('BUILD SUCCESS','',True),'unknown')
        self.assertEqual(backend_runner.dependency_network_usage('BUILD SUCCESS','',False),'not_used_by_build_tool')


class ExplicitApprovalTests(unittest.TestCase):
    def test_online_backend_run_requires_verbatim_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(vm,'ROOT',root),patch.object(backend_runner,'discover',return_value=[{'path':'backend'}]),patch.object(backend_runner,'run') as run:
                denied=vm.run_backend_tests('backend',allow_network=True)
                self.assertEqual(denied['status'],'blocked')
                run.assert_not_called()
                run.return_value={'status':'pass','network_usage':'download_observed'}
                approved=vm.run_backend_tests('backend',allow_network=True,explicit_developer_approval=True,
                                              approval_quote='I approve dependency access for this check')
                self.assertEqual(approved['status'],'pass')
                self.assertTrue(approved['network_allowed'])
                self.assertEqual(approved['network_policy'],'approved_online')
                self.assertEqual(approved['network_usage'],'download_observed')
                self.assertFalse(run.call_args.kwargs['offline'])
                self.assertTrue(run.call_args.kwargs['allow_network'])

    def test_task_acceptance_rejects_unapproved_online_run_before_execution(self):
        with patch.object(vm,'_authorized_delegate',return_value=True),patch.object(vm,'_task_folder',return_value=Path('/unused')):
            result=vm.run_task_acceptance_check('task-12345678','backend',allow_network=True)
        self.assertEqual(result['status'],'blocked')
        self.assertFalse(result['network_allowed'])
        self.assertFalse(result['network_attempted'])


class ManagedApiOfflinePolicyTests(unittest.TestCase):
    def _project(self,root):
        project=root/'backend';project.mkdir()
        (project/'pom.xml').write_text('<project/>')
        (project/'gaston-e2e.json').write_text(json.dumps({
            'main_class':'com.example.TestApp','suite':'src/test/e2e/suite.json',
            'readiness_path':'/health','readiness_status':200,
        }))
        java=project/'src/test/java/com/example/TestApp.java'
        java.parent.mkdir(parents=True);java.write_text('class TestApp {}')
        suite=project/'src/test/e2e/suite.json'
        suite.parent.mkdir(parents=True);suite.write_text(json.dumps({'cases':[{'expect_json':{}}]}))
        return project

    def test_managed_api_launches_maven_offline_and_persists_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();self._project(root)
            state=root/'.agentic'
            env={'PATH':os.environ.get('PATH',''),'MAVEN_USER_HOME':str(state/'local/backend-dependencies/maven-user')}
            cache=state/'local/backend-dependencies'
            calls=[]
            class Proc:
                def poll(self):return None
            def popen(argv,**kwargs):
                calls.append(argv);return Proc()
            results=[
                subprocess.CompletedProcess(['curl'],0,stdout='200',stderr=''),
                subprocess.CompletedProcess(['api_checks.py'],0,stdout='API assertions passed',stderr=''),
            ]
            with patch.object(managed_backend,'project_state',side_effect=lambda root:state),\
                 patch.object(managed_backend,'_env',return_value=(env,cache)),\
                 patch.object(managed_backend.shutil,'which',side_effect=lambda name:{'curl':'/usr/bin/curl','mvn':'/usr/bin/mvn'}.get(name)),\
                 patch.object(backend_runner,'select_jdk',return_value={'home':'/fake-jdk'}),\
                 patch.object(backend_runner,'required_java_release',return_value=None),\
                 patch.object(backend_runner,'installed_jdks',return_value=[]),\
                 patch.object(managed_backend.subprocess,'Popen',side_effect=popen),\
                 patch.object(managed_backend.subprocess,'run',side_effect=results),\
                 patch.object(managed_backend,'_stop',return_value=None):
                result=managed_backend.run(root,'backend')
            self.assertEqual(result['status'],'pass',result)
            self.assertIn('-o',calls[0])
            self.assertFalse(result['network_allowed'])
            self.assertEqual(result['network_policy'],'offline')
            self.assertEqual(result['network_usage'],'not_used_by_build_tool')
            self.assertTrue(result['log_path'])
            log=root/result['log_path']
            self.assertTrue(log.is_file())
            self.assertIn('API assertions passed',log.read_text(encoding='utf8'))

    def test_managed_api_online_run_requires_explicit_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(managed_backend.subprocess,'Popen') as popen:
                result=managed_backend.run(root,'backend',allow_network=True)
            self.assertEqual(result['status'],'blocked')
            self.assertFalse(result['network_allowed'])
            popen.assert_not_called()


if __name__=='__main__':unittest.main()
