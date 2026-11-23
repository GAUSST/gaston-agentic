#!/usr/bin/env python3
"""Focused offline regressions for the four consolidated v25 improvements."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import task_runtime
import verification_brief
import context_brief
import acceptance_audit
import verification_mcp as vm


class EventDrivenTests(unittest.TestCase):
    def test_one_call_wait_for_real_worker(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);(folder/'state.json').write_text('{"stage":"implementing"}')
            child="import time,pathlib,json;time.sleep(.1);pathlib.Path('state.json').write_text(json.dumps({'stage':'awaiting_lead_review'}))"
            p=subprocess.Popen([sys.executable,'-c',child],cwd=folder)
            (folder/'worker.json').write_text(json.dumps({'pid':p.pid,'started_at':time.time()}))
            try:
                report=task_runtime.wait_worker(folder,timeout_seconds=3)
                self.assertEqual(report['status'],'ready');self.assertEqual(report['stage'],'awaiting_lead_review')
            finally:p.wait(timeout=3)

    def test_stale_worker_is_blocked_not_pass(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);(folder/'state.json').write_text('{"stage":"implementing"}')
            (folder/'worker.json').write_text('{"pid":99999999}')
            self.assertEqual(task_runtime.wait_worker(folder,timeout_seconds=1)['status'],'blocked')

    def test_terminal_state_finishes_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);(folder/'state.json').write_text('{"stage":"awaiting_lead_review"}')
            self.assertEqual(task_runtime.wait_worker(folder)['status'],'ready')

    def test_metrics_do_not_invent_tokens_or_time(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);(folder/'state.json').write_text('{"stage":"awaiting_lead_review"}')
            met=task_runtime.task_metrics(folder)
            self.assertIsNone(met['provider_usage']['tokens'])
            self.assertIsNone(met['end_to_end_elapsed_seconds'])
            self.assertIsNone(met['worker_elapsed_seconds'])

    def test_metrics_parse_only_real_turn_usage(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            (folder/'build.log').write_text('\n'.join((
                '{"type":"turn.completed","usage":{"input_tokens":500,"cached_input_tokens":100,"output_tokens":30}}',
                '{"type":"item.completed","usage":{"output_tokens":99999}}',
                '{"type":"turn.completed","usage":{"input_tokens":700,"output_tokens":20}}')))
            usage=task_runtime.codex_usage(folder)
            self.assertEqual(usage['turns_with_usage'],2)
            self.assertEqual(usage['tokens']['input_tokens'],1200)
            self.assertEqual(usage['tokens']['output_tokens'],50)
            self.assertIsNone(usage['claude_lead_tokens'])

    def test_end_to_end_time_only_after_verified_pass(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);(folder/'task.json').write_text('{"created_at":50}')
            (folder/'lead-backend-verification.json').write_text('{"verified_at":90,"outcome":{"status":"pass"}}')
            self.assertEqual(task_runtime.task_metrics(folder)['end_to_end_elapsed_seconds'],40.0)
            (folder/'lead-backend-verification.json').write_text('{"verified_at":90,"outcome":{"status":"fail"}}')
            self.assertIsNone(task_runtime.task_metrics(folder)['end_to_end_elapsed_seconds'])

    def test_metrics_worker_time_survives_later_lead_review(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            (folder/'worker.json').write_text('{"started_at":100}')
            (folder/'worker-finished.json').write_text('{"finished_at":112}')
            (folder/'state.json').write_text('{"stage":"ready_for_human_release_review"}')
            self.assertEqual(task_runtime.task_metrics(folder)['worker_elapsed_seconds'],12.0)


class PlanningTests(unittest.TestCase):
    def test_controller_proposes_backend_and_flags_missing_api_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo=root/'backend';repo.mkdir()
            (repo/'pom.xml').write_text('<project/>')
            cfg={'checks':[{'name':'backend_verify','cwd':'backend','command':['mvn','test'],'areas':['backend']}],
                 'project':{'discovered_roots':{'java':['backend'],'web':[],'flutter':[]}}}
            # Using an existing project root is important: the actual planner
            # refreshes its tests instead of trusting stale setup.
            output=verification_brief.produce(root,{'goal':'Add GET /api/health'},
                {'changed_files':['backend/src/main/java/example/HealthController.java']},cfg)
            self.assertIn('backend',output['areas']);self.assertIn('api',output['areas'])
            self.assertIn('api_integration',output['missing_gates'])
            self.assertNotIn('status',output.get('checks',[]))

    def test_no_test_is_reported_as_executed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            result=verification_brief.produce(root,{'goal':'Fix a bug'},
                                              {'changed_files':[]},{'checks':[],'project':{}})
            self.assertIn('No check executed', ' '.join(result['notes']))

    def test_absolute_and_parent_paths_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            result=verification_brief.produce(Path(td),{'goal':'Check backend'},
                {'changed_files':['../../secret.java','/etc/passwd','src/Test.java']},
                {'checks':[],'project':{}})
            self.assertEqual(result['changed_paths'],['src/Test.java'])


class ContextTests(unittest.TestCase):
    def test_handoff_is_bounded_for_dirty_workspace(self):
        paths={f'src/test/java/Test{i}.java':{} for i in range(200)}
        brief=context_brief.make({'goal':'Fix Foo.java','acceptance_criteria':['unit pass']},
                {'project':{'discovered_roots':{'java':['backend'],'web':[]}}},paths,max_paths=30)
        self.assertEqual(len(brief['existing_change_paths']),30)
        self.assertEqual(brief['existing_changes_not_listed'],170)
        self.assertLessEqual(len(context_brief.render(brief)),6500)
        self.assertIn('Foo.java',brief['named_file_hints'])

    def test_no_source_or_old_chat_dumped(self):
        brief=context_brief.make({'goal':'implement a GET endpoint','acceptance_criteria':[]},
                                  {'project':{}},{})
        self.assertNotIn('old_chats',brief)
        self.assertNotIn('source_content',brief)


class AcceptanceTests(unittest.TestCase):
    def test_unit_only_does_not_fake_api_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            acceptance_audit.record(folder,'backend',{'status':'pass','exit_code':0})
            plan={'selected_checks':[{'name':'backend_verify'}],'missing_gates':['api_integration']}
            result=acceptance_audit.summarize(plan,acceptance_audit.read(folder))
            self.assertEqual(result['status'],'incomplete')
            self.assertEqual(result['observed_gates']['backend_verify'],'pass')
            self.assertIn('api_integration',result['missing_configured_gates'])

    def test_all_configured_gates_require_real_pass(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            acceptance_audit.record(folder,'backend',{'status':'pass','exit_code':0})
            acceptance_audit.record(folder,'api',{'status':'pass','exit_code':0})
            plan={'selected_checks':[{'name':'backend_verify'},{'name':'api_integration'}],'missing_gates':[]}
            self.assertEqual(acceptance_audit.summarize(plan,acceptance_audit.read(folder))['status'],'pass')

    def test_failing_check_is_never_green(self):
        plan={'selected_checks':[{'name':'web_e2e'}],'missing_gates':[]}
        actual=[{'gate':'web_e2e','result':'fail'}]
        self.assertEqual(acceptance_audit.summarize(plan,actual)['status'],'fail')

    def test_empty_plan_is_not_success(self):
        self.assertEqual(acceptance_audit.summarize({'selected_checks':[],'missing_gates':[]},[])['status'],'incomplete')

    def test_ledger_refuses_invented_success(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                acceptance_audit.record(Path(td),'api',{'status':'done'})

    def test_task_acceptance_refuses_unenrolled_lead(self):
        with patch.object(vm,'_authorized_delegate',return_value=False):
            self.assertEqual(vm.run_task_acceptance_check('some-task-01','backend')['status'],'blocked')
            self.assertEqual(vm.task_verification_plan('some-task-01')['status'],'blocked')
            self.assertEqual(vm.wait_for_builder('some-task-01')['status'],'blocked')

    def test_runners_do_not_automatically_cover_other_repo(self):
        plan={'selected_checks':[{'name':'backend_verify','cwd':'backend-a'},
                                 {'name':'backend_verify__1','cwd':'backend-b'}],'missing_gates':[]}
        evidence=[{'gate':'backend_verify','result':'pass','parameters':{'project':'backend-a'},'evidence':{}}]
        result=acceptance_audit.summarize(plan,evidence)
        self.assertEqual(result['status'],'incomplete')
        self.assertIn('backend_verify__1',result['unverified_planned_gates'])

    def test_numbered_repo_gate_matches_own_evidence(self):
        plan={'selected_checks':[{'name':'backend_verify__1','cwd':'backend-b'}],'missing_gates':[]}
        evidence=[{'gate':'backend_verify','result':'pass','parameters':{'project':'backend-b'},'evidence':{}}]
        self.assertEqual(acceptance_audit.summarize(plan,evidence)['status'],'pass')

    def test_application_web_runner_ignores_gaston_smoke_harness(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo=root/'frontend'
            (repo/'node_modules/@playwright/test').mkdir(parents=True)
            (repo/'node_modules/@playwright/test/cli.js').write_text('// fake test runner for static discovery')
            (repo/'playwright.config.js').write_text('module.exports={}')
            (repo/'tests').mkdir()
            (repo/'tests/health.spec.js').write_text('test("health",()=>{})')
            (repo/'package.json').write_text('{"dependencies":{"@playwright/test":"1.0"}}')
            harness=root/'.agentic/local/playwright-tests'
            (harness/'tests').mkdir(parents=True)
            (harness/'tests/counter.spec.ts').write_text('test("counter",()=>{})')
            with patch.object(vm,'ROOT',root), patch.dict(os.environ,{'PLAYWRIGHT_BASE_URL':'http://127.0.0.1:3000'}), \
                 patch.object(vm,'execute',return_value={'exit_code':0,'stdout':'1 passed','stderr':''}) as runner, \
                 patch.object(vm.shutil,'which',return_value='/usr/bin/node'):
                result=vm.run_project_web_tests()
            self.assertEqual(result['status'],'pass');self.assertEqual(result['project'],'frontend')
            self.assertEqual(Path(runner.call_args.kwargs['cwd']),repo)

    def test_application_web_runner_blocks_unconfigured_target(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(vm,'ROOT',Path(td)), patch.dict(os.environ,{'PLAYWRIGHT_BASE_URL':'https://example.com'}):
                self.assertEqual(vm.run_project_web_tests()['status'],'blocked')

    def test_backend_acceptance_refuses_zero_tests(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'.agentic/runs/test-task-123';folder.mkdir(parents=True)
            (folder/'task.json').write_text('{}');(folder/'state.json').write_text('{"stage":"awaiting_lead_review"}')
            fake={'status':'pass','evidence':{'stdout_tail':'BUILD SUCCESS','stderr_tail':''}}
            with patch.object(vm,'ROOT',root), patch.object(vm,'_authorized_delegate',return_value=True), \
                 patch.object(vm,'run_backend_tests',return_value=fake):
                result=vm.run_task_acceptance_check('test-task-123','backend',project='backend')
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(acceptance_audit.read(folder)[0]['result'],'blocked')

    def test_no_unrelated_gates_claimed_as_pass(self):
        plan={'selected_checks':[{'name':'backend_verify'},{'name':'web_e2e'},{'name':'mobile_e2e'}],
              'missing_gates':['security_checks']}
        evidence=[{'gate':'backend_verify','result':'pass'}]
        report=acceptance_audit.summarize(plan,evidence)
        self.assertEqual(report['coverage'],'partial')
        self.assertEqual(report['unverified_planned_gates'],['mobile_e2e','web_e2e'])
        self.assertEqual(report['missing_configured_gates'],['security_checks'])


if __name__=='__main__':unittest.main(verbosity=2)

# v25 supplementary suite is intentionally separate from the stable v24 suite.
# Collect it via: python -m unittest discover -s .agentic -p 'test_v25.py'
