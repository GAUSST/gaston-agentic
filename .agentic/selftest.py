#!/usr/bin/env python3
"""Offline smoke tests for toolkit logic; no provider account or external install."""
from __future__ import annotations

import json
import contextlib
import os
import sys
import subprocess
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eng
import playwright_tools
import model_menu
import setup_wizard
import native_hooks
import verification_planner
import workspace_checks
import api_checks
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import shutil
import re


class AdvisorTests(unittest.TestCase):
    def test_lead_default_and_saved_setting(self):
        self.assertEqual(eng.claude_advisor_args('reviewer',{'advisor':{}}),['--advisor','claude-opus-5-5'])
        self.assertEqual(eng.claude_advisor_args('reviewer',{'advisor':{'claude_reviewer':'opus'}}),['--advisor','claude-opus-5-5'])
        self.assertEqual(eng.claude_advisor_args('reviewer',{'advisor':{'claude_reviewer':'off'}}),[])
        self.assertEqual(eng.claude_advisor_args('builder',{'advisor':{'claude_reviewer':'fable'}}),[])

    def test_disabled_advisor_scoped_to_builder_or_off_lead(self):
        cfg={'advisor':{'claude_reviewer':'fable'}}
        self.assertNotIn('CLAUDE_CODE_DISABLE_ADVISOR_TOOL',eng.claude_advisor_env('reviewer',cfg))
        self.assertEqual(eng.claude_advisor_env('builder',cfg)['CLAUDE_CODE_DISABLE_ADVISOR_TOOL'],'1')
        cfg['advisor']['claude_reviewer']='off'
        self.assertEqual(eng.claude_advisor_env('reviewer',cfg)['CLAUDE_CODE_DISABLE_ADVISOR_TOOL'],'1')


    def test_inherited_claude_overrides_do_not_defeat_explicit_role_settings(self):
        cfg={'advisor':{'claude_reviewer':'fable'}}
        with patch.dict('os.environ',{'CLAUDE_CODE_EFFORT_LEVEL':'high',
                                     'CLAUDE_CODE_DISABLE_ADVISOR_TOOL':'1'},clear=False):
            builder_env=eng.claude_advisor_env('builder',cfg)
            self.assertNotIn('CLAUDE_CODE_EFFORT_LEVEL',builder_env)
            # Explicit global opt-out must not be silently overridden.
            with self.assertRaises(SystemExit):
                eng.claude_advisor_env('reviewer',cfg)
            self.assertEqual(eng.claude_advisor_env('builder',cfg)['CLAUDE_CODE_DISABLE_ADVISOR_TOOL'],'1')

    def test_cli_advisor_change_preserves_other_settings(self):
        cfg={'preferences':{'builder':'codex','reviewer':'claude','models_configured':True},
             'models':{'codex_builder':'custom-builder','claude_reviewer':'sonnet'},
             'reasoning':{'codex_builder':'low','claude_reviewer':'medium'},
             'advisor':{'claude_reviewer':'opus'},'checks':[]}
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'config.json'
            path.write_text(json.dumps(cfg))
            with patch.object(eng,'CFG_FILE',path):
                eng.models_command('advisor','off')
                updated=json.loads(path.read_text())
                self.assertEqual(updated['advisor']['claude_reviewer'],'off')
                self.assertEqual(updated['models'],{**cfg['models'],'claude_reviewer':'claude-sonnet-5-5'})
                self.assertEqual(updated['reasoning'],cfg['reasoning'])
                eng.models_command('set','lead','custom-reviewer')
                again=json.loads(path.read_text())
                self.assertEqual(again['models']['claude_reviewer'],'custom-reviewer')
                self.assertEqual(again['advisor']['claude_reviewer'],'off')


    def test_real_review_command_contains_native_advisor(self):
        cfg={'preferences':{'builder':'codex','reviewer':'claude'},
             'models':{'claude_reviewer':'sonnet'},
             'reasoning':{'claude_reviewer':'medium'},
             'advisor':{'claude_reviewer':'fable'}}
        lease='test-lead-lease'
        session=SimpleNamespace(_held_leases={lease:('claude','reviewer')},
                                locked=lambda *a:contextlib.nullcontext(),
                                get=lambda *a:{'id':'00000000-0000-4000-8000-000000000001'},
                                mark=lambda *a,**kw:None)
        called={}
        def fake_run(cmd,output,**kwargs):
            called['cmd']=cmd
            called['env']=kwargs.get('env')
            return subprocess.CompletedProcess(cmd,0,stdout=json.dumps({'structured_output':{'status':'ok'}}))
        with tempfile.TemporaryDirectory() as td, \
             patch.object(eng,'SESSIONS',session), \
             patch.object(eng,'LOCAL',Path(td)/'local'), \
             patch.dict(os.environ,{'GASTON_SESSION_LEASE':lease}), \
             patch.object(eng,'command_exists',lambda name:True), \
             patch.object(eng,'load_config',lambda:cfg), \
             patch.object(eng,'run_process',fake_run):
            eng.claude_structured('inspect',{'type':'object'},Path(td)/'review.json','sonnet',role='review')
            verifier=json.loads((Path(td)/'local/lead-verification.mcp.json').read_text())['mcpServers']['gaston_verify']
            self.assertEqual(verifier['env']['GASTON_SESSION_LEASE'],lease)
            self.assertEqual(verifier['env']['GASTON_LEAD_PROVIDER'],'claude')
        args=called['cmd']
        self.assertEqual(args[args.index('--advisor')+1],'claude-opus-5-5')
        self.assertEqual(args[args.index('--effort')+1],'medium')
        self.assertNotIn('--tools',args)
        self.assertIn('mcp__gaston_verify__git_inspect',args)
        self.assertNotIn('mcp__*',args)
        self.assertNotIn('CLAUDE_CODE_DISABLE_ADVISOR_TOOL',called['env'])

    def test_builder_execution_disables_inherited_advisor(self):
        cfg={'preferences':{'builder':'claude','reviewer':'claude'},
             'models':{'claude_builder':'sonnet'},
             'reasoning':{'claude_builder':'low'},
             'advisor':{'claude_reviewer':'fable'}}
        session=SimpleNamespace(locked=lambda *a:contextlib.nullcontext(),
                                get=lambda *a:{'id':'00000000-0000-4000-8000-000000000001'},
                                mark=lambda *a,**kw:None)
        called={}
        def fake_run(cmd,output,**kwargs):
            called['cmd']=cmd
            called['env']=kwargs.get('env')
            return subprocess.CompletedProcess(cmd,0,stdout=json.dumps({'structured_output':{'status':'done'}}))
        with tempfile.TemporaryDirectory() as td, \
             patch.object(eng,'SESSIONS',session), \
             patch.object(eng,'load_config',lambda:cfg), \
             patch.object(eng,'run_process',fake_run):
            eng.provider_structured('claude','build',{'type':'object'},Path(td)/'build.json',cfg,'build')
        self.assertNotIn('--advisor',called['cmd'])
        self.assertEqual(called['cmd'][called['cmd'].index('--effort')+1],'low')
        self.assertEqual(called['env']['CLAUDE_CODE_DISABLE_ADVISOR_TOOL'],'1')
        self.assertNotIn('--tools',called['cmd'])
        self.assertNotIn('mcp__maestro__*',called['cmd'])
        self.assertNotIn('mcp__playwright__*',called['cmd'])
        self.assertIn('mcp__gaston_verify__*',called['cmd'][called['cmd'].index('--disallowedTools')+1:])


class PlaywrightTests(unittest.TestCase):
    def test_browser_gate_requires_real_spec(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.assertIsNone(playwright_tools.browser_gate(root))
            folder=root/playwright_tools.HARNESS/'tests'
            folder.mkdir(parents=True)
            (folder/'README.txt').write_text('not a test')
            self.assertIsNone(playwright_tools.browser_gate(root))
            (folder/'api.spec.ts').write_text('test placeholder for file-discovery test')
            gate=playwright_tools.browser_gate(root)
            self.assertEqual(gate['name'],'web_e2e')
            self.assertEqual(gate['command'],['npm','exec','--','playwright','test'])

    def test_no_silent_pass_without_test_package(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(playwright_tools.package_installed(Path(td)))
            self.assertNotEqual(playwright_tools.run_tests(Path(td)),0)



class ModelMenuTests(unittest.TestCase):
    def test_claude_models_use_explicit_ids_without_account_default(self):
        self.assertEqual([v for v,_ in model_menu.model_choices('claude','reviewer')],
                         ['claude-sonnet-5-5','claude-opus-5-5','claude-haiku-5-5'])
        self.assertEqual(model_menu.recommended_model('claude','reviewer'),'claude-sonnet-5-5')
        self.assertNotIn('',[v for v,_ in model_menu.model_choices('claude','builder')])

    def test_codex_model_family_catalog_has_no_frozen_versions(self):
        catalog=['gpt-6-luna','gpt-6.1-sol','gpt-6-astra','gpt-5.6-sol']
        self.assertEqual(model_menu.model_choices('codex','builder',catalog=catalog),
                         [('gpt-6-luna','gpt-6-luna'),
                          ('gpt-6.1-sol','gpt-6.1-sol'),
                          ('gpt-6-astra','gpt-6-astra')])
        self.assertEqual(model_menu.model_choices('codex','reviewer',catalog=catalog)[0],
                         ('gpt-6.1-sol','gpt-6.1-sol'))
        self.assertEqual(model_menu.recommended_model('codex','reviewer',catalog=catalog),'gpt-6.1-sol')
        self.assertEqual(model_menu.model_choices('codex','builder',catalog=[]),[])

    def test_model_picker_uses_new_exact_ids_without_pinning_versions(self):
        catalog=['gpt-7-luna','gpt-7.2-sol','gpt-7.1-astra']
        self.assertEqual(model_menu.model_choices('codex','builder',catalog=catalog),
                         [('gpt-7-luna','gpt-7-luna'),
                          ('gpt-7.2-sol','gpt-7.2-sol'),
                          ('gpt-7.1-astra','gpt-7.1-astra')])
        self.assertFalse(any(label in ('Luna','Sol','Astra')
                             for _,label in model_menu.model_choices('codex','builder',catalog=catalog)))

    def test_reasoning_levels_and_role_defaults(self):
        self.assertEqual(len(model_menu.effort_choices('codex')),5)
        self.assertEqual(len(model_menu.effort_choices('claude')),5)
        self.assertEqual(model_menu.configured_effort({},'codex','builder'),'low')
        self.assertEqual(model_menu.configured_effort({},'claude','reviewer'),'medium')
        self.assertEqual(model_menu.configured_effort({'reasoning':{'codex_builder':'ultra'}},'codex','builder'),'ultra')
        self.assertEqual(model_menu.configured_effort({'reasoning':{'claude_reviewer':'ultra'}},'claude','reviewer'),'medium')


    def test_codex_catalog_extracts_model_specific_efforts(self):
        raw={'models':[
            {'slug':'gpt-6.1-sol','visibility':'list',
             'supported_reasoning_levels':[{'effort':'low'},{'effort':'medium'},{'effort':'high'},
                                           {'effort':'xhigh'},{'effort':'max'},{'effort':'ultra'}]},
            {'slug':'gpt-6-luna','visibility':'list',
             'supported_reasoning_levels':[{'effort':'low'},{'effort':'medium'},{'effort':'high'}]},
            {'slug':'gpt-6-hidden','visibility':'hidden',
             'supported_reasoning_levels':[{'effort':'max'}]}]}
        details=model_menu.parse_codex_catalog(raw)
        self.assertEqual(model_menu.effort_choices('codex','gpt-6-luna',catalog=details),('low','medium','high'))
        self.assertEqual(len(model_menu.effort_choices('codex','gpt-6.1-sol',catalog=details)),5)
        self.assertNotIn('gpt-6-hidden',details)
        self.assertFalse(model_menu.verify_codex_model_effort('gpt-6-luna','ultra',catalog=details)[0])
        self.assertFalse(model_menu.verify_codex_model_effort('gpt-6.1-sol','ultra',catalog=details)[0])
        self.assertFalse(model_menu.verify_codex_model_effort('gpt-6-missing','low',catalog=details)[0])

    def test_codex_effort_preflight_rejects_unsupported_and_preserves_saved_level(self):
        cfg={'models':{'codex_builder':'gpt-6-luna'},'reasoning':{'codex_builder':'ultra'}}
        details={'gpt-6-luna':('low','medium','high')}
        self.assertEqual(model_menu.configured_effort(cfg,'codex','builder',catalog=details),'ultra')
        with patch.object(eng,'load_config',return_value=cfg),patch.object(
                model_menu,'codex_catalog_details',return_value=details):
            # Verify rejection directly against catalog: no real model credits.
            self.assertFalse(model_menu.verify_codex_model_effort('gpt-6-luna','ultra',catalog=details)[0])

    def test_codex_catalog_command_json_output(self):
        raw={'models':[{'slug':'gpt-6-luna','supported_reasoning_levels':[
            {'effort':'low'},{'effort':'medium'}]}]}
        fake=subprocess.CompletedProcess(['codex','debug','models'],0,stdout=json.dumps(raw))
        model_menu.codex_catalog_details.cache_clear()
        with patch.object(model_menu.shutil,'which',return_value='/usr/bin/codex'), \
             patch.object(model_menu.subprocess,'run',return_value=fake) as run:
            self.assertEqual(model_menu.codex_catalog_details(),{'gpt-6-luna':('low','medium')})
            run.assert_called_once()
        model_menu.codex_catalog_details.cache_clear()

    def test_codex_launch_args_verified_against_model_metadata(self):
        cfg={'models':{'codex_builder':'gpt-6-luna'},
             'reasoning':{'codex_builder':'medium'}}
        with patch.object(eng,'load_config',return_value=cfg), \
             patch.object(eng,'verify_codex_model_effort',return_value=(True,'valid')):
            self.assertEqual(eng.codex_effort_args('codex','builder'),
                             ['-c','model_reasoning_effort="medium"'])
        with patch.object(eng,'load_config',return_value=cfg), \
             patch.object(eng,'verify_codex_model_effort',return_value=(False,'unsupported effort')):
            with self.assertRaises(SystemExit):
                eng.codex_effort_args('codex','builder')

    def test_codex_catalog_parser_ignores_unrelated_strings(self):
        value={'models':[{'slug':'gpt-6-luna','description':'fast'},{'slug':'gpt-6.1-sol'}],
               'error':'gpt-6-astra is not available'}
        self.assertEqual(set(model_menu.parse_codex_catalog(value)),{'gpt-6-luna','gpt-6.1-sol'})



    def test_model_validation_runs_in_init_with_codex_catalog(self):
        cfg={'preferences':{'builder':'codex','reviewer':'claude'},
             'models':{'codex_builder':'gpt-6-luna','claude_reviewer':'sonnet'},
             'reasoning':{'codex_builder':'ultra','claude_reviewer':'medium'},
             'advisor':{'claude_reviewer':'fable'}}
        results=[]
        with patch.object(model_menu,'verify_codex_model_effort',return_value=(False,'unsupported effort')):
            setup_wizard.validate_selected_models(
                cfg,[('builder','codex'),('reviewer','claude')],
                lambda name,ok,action,**kwargs:results.append((name,ok,action)))
        self.assertIn(('codex builder model/effort',False,'unsupported effort'),results)
        self.assertTrue(any(name=='claude reviewer model/effort' and ok for name,ok,_ in results))

    def test_model_validation_in_init_catches_advisor_env_conflict(self):
        cfg={'models':{'claude_reviewer':'sonnet'},
             'reasoning':{'claude_reviewer':'medium'},
             'advisor':{'claude_reviewer':'fable'}}
        results=[]
        with patch.dict(os.environ,{'CLAUDE_CODE_DISABLE_ADVISOR_TOOL':'1'}):
            setup_wizard.validate_selected_models(
                cfg,[('reviewer','claude')],
                lambda name,ok,action,**kwargs:results.append((name,ok)))
        self.assertIn(('Claude Lead Advisor environment',False),results)

    def test_old_models_check_command_is_not_offered(self):
        from pathlib import Path
        path=Path(eng.__file__).resolve()
        result=subprocess.run([sys.executable,str(path),'models','check'],
                              text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,2)
        self.assertIn('unrecognized arguments',result.stderr)



class PermissionAndEvidenceTests(unittest.TestCase):
    def test_lead_allows_browser_mcp_but_not_file_writes(self):
        native_hooks.block_native_reviewer_tool({'tool_name':'mcp__playwright__browser_navigate'})
        native_hooks.block_native_reviewer_tool({'tool_name':'mcp__playwright__browser_click'})
        self.assertIsNone(native_hooks.block_native_reviewer_tool({'tool_name':'Bash'}))
        with self.assertRaises(SystemExit):
            native_hooks.block_native_reviewer_tool({'tool_name':'mcp__filesystem__write_file'})

    def test_database_and_api_need_actual_integration_gates(self):
        with tempfile.TemporaryDirectory() as td, patch.object(eng,'ROOT',Path(td)), \
             patch.object(eng.workspace_git, 'changed_paths', return_value=[]):
            with self.assertRaises(SystemExit):
                eng.load_verify_steps({'checks':[]}, {'scope':['api','database']})
            cfg={'checks':[{'name':'backend_verify','cwd':'.','command':['echo','build']}]}
            with self.assertRaises(SystemExit):
                eng.load_verify_steps(cfg,{'scope':['api','database']})
            cfg['checks'].extend([{'name':'api_integration','cwd':'.','command':['echo','api']},
                                  {'name':'postgres_integration','cwd':'.','command':['echo','db']}])
            self.assertEqual(len(eng.load_verify_steps(cfg,{'scope':['api','database']})),3)



class LeadPlanningTests(unittest.TestCase):
    def test_plan_schema_demands_targeted_coverage_with_rationale(self):
        self.assertIn('verification_areas', eng.PLAN_SCHEMA['required'])
        self.assertIn('verification_checks', eng.PLAN_SCHEMA['required'])
        self.assertIn('verification_rationale', eng.PLAN_SCHEMA['required'])

    def test_codex_lead_planning_is_reviewer_read_only(self):
        cfg={'models':{'codex_builder':'builder-model','codex_reviewer':'lead-model'}}
        with patch.object(eng,'codex_structured',return_value={'status':'ready'}) as mock:
            eng.provider_structured('codex','plan',eng.PLAN_SCHEMA,Path('/tmp/test-plan.json'),cfg,'lead_plan')
        args,kwargs=mock.call_args
        self.assertEqual(args[3],'lead-model')
        self.assertEqual(args[4],'read-only')
        self.assertEqual(kwargs['role'],'lead_plan')

    def test_claude_lead_planning_is_reviewer_read_only(self):
        cfg={'models':{'claude_builder':'builder-model','claude_reviewer':'sonnet'}}
        with patch.object(eng,'claude_structured',return_value={'status':'ready'}) as mock:
            eng.provider_structured('claude','plan',eng.PLAN_SCHEMA,Path('/tmp/test-plan.json'),cfg,'lead_plan')
        args,kwargs=mock.call_args
        self.assertEqual(args[3],'sonnet')
        self.assertEqual(kwargs['role'],'lead_plan')


class SmartVerificationTests(unittest.TestCase):
    def test_backend_only_does_not_repeat_unrelated_ui_or_api_checks(self):
        checks=[{'name':name,'cwd':'.','command':['echo',name]} for name in
                ('backend_verify','api_integration','postgres_integration','flutter_tests','web_e2e')]
        plan=verification_planner.select_checks({'goal':'Refactor pricing calculation'},checks,
                ['backend/src/main/java/com/example/PricingService.java'])
        self.assertEqual(plan['selected'],['backend_verify'])
        self.assertFalse(plan['missing'])

    def test_contract_changes_include_other_sections_when_configured(self):
        checks=[{'name':name,'cwd':'.','command':['echo',name]} for name in
                ('backend_verify','api_integration','flutter_tests','web_e2e')]
        plan=verification_planner.select_checks({'goal':'Change customer API'},checks,
                ['backend/src/main/java/com/example/CustomerController.java'])
        self.assertEqual(plan['selected'],['backend_verify','api_integration','flutter_tests','web_e2e'])
        self.assertIn('flutter_tests',plan['cross_component_checks'])

    def test_migration_requires_real_db_check_and_uses_api_if_available(self):
        checks=[{'name':'backend_verify','cwd':'.','command':['echo','build']},
                {'name':'api_integration','cwd':'.','command':['echo','api']}]
        plan=verification_planner.select_checks({'goal':'Add migration'},checks,
                ['backend/src/main/resources/db/migration/V2__add_index.sql'])
        self.assertIn('postgres_integration',plan['missing'])
        self.assertIn('api_integration',plan['selected'])

    def test_persistence_edit_requires_db_and_client_changes_check_api_when_configured(self):
        checks=[{'name':name,'cwd':'.','command':['echo',name]} for name in
                ('backend_verify','postgres_integration','api_integration','flutter_tests')]
        backend=verification_planner.select_checks({'goal':'Update user persistence'},checks,
                ['backend/src/main/java/com/sample/UserRepository.java'])
        self.assertEqual(backend['selected'],['backend_verify','postgres_integration','api_integration'])
        client=verification_planner.select_checks({'goal':'Update client request'},checks,
                ['mobile/lib/data/remote/api_client.dart'])
        self.assertEqual(client['selected'],['api_integration','flutter_tests'])

    def test_auth_and_money_movements_expand_risk_and_missing_gates_fail_closed(self):
        checks=[{'name':'backend_verify','cwd':'.','command':['echo','build']}]
        plan=verification_planner.select_checks({'goal':'Implement payment endpoint'},checks,
                ['backend/src/main/java/com/payments/PaymentController.java'])
        self.assertEqual(set(plan['missing']),{'api_integration','postgres_integration','security_checks'})
        self.assertFalse('mobile_e2e' in plan['selected'])

    def test_explicit_device_e2e_when_needed(self):
        checks=[{'name':name,'cwd':'.','command':['echo',name]} for name in
                ('flutter_analyze','flutter_tests','mobile_e2e')]
        normal=verification_planner.select_checks({'scope':['mobile']},checks,['mobile/lib/payment.dart'])
        e2e=verification_planner.select_checks({'scope':['mobile'],'required_checks':['mobile_e2e']},checks,['mobile/lib/payment.dart'])
        self.assertNotIn('mobile_e2e',normal['selected'])
        self.assertIn('mobile_e2e',e2e['selected'])

    def test_unavailable_cross_section_is_not_falsely_marked_passed(self):
        checks=[{'name':'backend_verify','cwd':'.','command':['echo','build']}]
        with tempfile.TemporaryDirectory() as td, patch.object(eng,'ROOT',Path(td)), \
             patch.object(eng.workspace_git,'changed_paths',return_value=[]):
            plan=eng.verification_plan({'checks':checks},{'scope':['api']})
            self.assertIn('api_integration',plan['missing'])
            self.assertIn('backend_verify',plan['selected'])

    def test_one_execution_for_duplicate_check_commands(self):
        with tempfile.TemporaryDirectory() as td, patch.object(eng,'ROOT',Path(td)), \
             patch.object(eng.workspace_git,'changed_paths',return_value=[]):
            checks=[{'name':'backend_verify','cwd':'.','command':[sys.executable,'-c','print("done")']},
                    {'name':'api_integration','cwd':'.','command':[sys.executable,'-c','print("done")']}]
            rundir=Path(td)/'runs'/'test'
            with patch.object(eng,'run_process',wraps=eng.run_process) as spy:
                report=eng.verify({'scope':['backend','api']},rundir,{'checks':checks})
            self.assertEqual(spy.call_count,1)
            self.assertTrue(report['all_passed'])
            self.assertEqual(report['checks'][1]['reused_from'],'backend_verify')
            self.assertIn('no independent coverage',report['checks'][1]['note'])
            self.assertTrue((rundir/'verification_plan.json').is_file())

    def test_missing_api_gate_records_blocked_not_pass(self):
        with tempfile.TemporaryDirectory() as td, patch.object(eng,'ROOT',Path(td)), \
             patch.object(eng.workspace_git,'changed_paths',return_value=[]):
            checks=[{'name':'backend_verify','cwd':'.','command':[sys.executable,'-c','print("ok")']}]
            result=eng.verify({'scope':['api']},Path(td)/'run',{'checks':checks})
            self.assertFalse(result['all_passed'])
            self.assertEqual(result['checks'][-1]['result'],'blocked')
            self.assertEqual(result['checks'][-1]['name'],'api_integration')


class CurlApiTests(unittest.TestCase):
    def test_spec_requires_loopback_url_and_nonempty_tests(self):
        with patch.dict(os.environ,{'AGENTIC_TEST_API_BASE_URL':'https://my-bank.example'},clear=False):
            with self.assertRaises(ValueError):api_checks.base_url()
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{'AGENTIC_TEST_API_BASE_URL':'http://127.0.0.1:9999'},clear=False):
            spec=Path(td)/'api.json'
            spec.write_text('{"cases":[]}',encoding='utf-8')
            self.assertEqual(api_checks.run(spec),2)

    @unittest.skipUnless(shutil.which('curl'), 'curl unavailable')
    def test_real_curl_http_assertions_and_no_secret_output(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                length=int(self.headers.get('Content-Length',0))
                payload=json.loads(self.rfile.read(length))
                if self.path=='/items' and payload.get('description')=='testing':
                    self.send_response(201)
                    self.send_header('Content-Type','application/json')
                    self.end_headers()
                    self.wfile.write(b'{"id":42,"status":"created"}')
                else:
                    self.send_response(422); self.end_headers()
            def do_GET(self):
                if self.path=='/health':
                    payload=b'{"status":"ok","details":{"mode":"test"}}'
                    self.send_response(200)
                else:
                    payload=b'{"error":"unauthorized"}'
                    self.send_response(401)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            spec={'cases':[
                {'name':'health','path':'/health','expected_status':200,'expect_json':{'details.mode':'test'}},
                {'name':'unauthenticated','path':'/secure','expected_status':401,'expect_json':{'error':'unauthorized'}},
                {'name':'create in disposable localhost','method':'POST','path':'/items',
                 'allow_local_mutation':True,'json_body':{'description':'testing'},
                 'expected_status':201,'expect_json':{'id':42,'status':'created'}}]}
            with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,
                    {'AGENTIC_TEST_API_BASE_URL':f'http://127.0.0.1:{server.server_port}'},clear=False):
                target=Path(td)/'api.json';target.write_text(json.dumps(spec))
                from io import StringIO
                with contextlib.redirect_stdout(StringIO()) as output:
                    rc=api_checks.run(target)
                self.assertEqual(rc,0,output.getvalue())
                self.assertIn('3/3 passed',output.getvalue())
                spec['cases'][0]['expected_status']=201
                target.write_text(json.dumps(spec))
                with contextlib.redirect_stdout(StringIO()) as output:
                    self.assertEqual(api_checks.run(target),1)
                    self.assertNotIn('test-secret-credential',output.getvalue())
        finally:
            server.shutdown();server.server_close();worker.join(timeout=2)

    @unittest.skipUnless(shutil.which('curl'), 'curl unavailable')
    def test_secret_auth_header_not_in_process_argv(self):
        spec={'cases':[{'name':'authorized-local', 'method':'GET','path':'/auth',
                        'expected_status':200,'header_env':{'Authorization':'AGENTIC_TEST_TOKEN'}}]}
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,
            {'AGENTIC_TEST_API_BASE_URL':'http://127.0.0.1:58888',
             'AGENTIC_TEST_TOKEN':'Bearer unique-secret-value'},clear=False):
            path=Path(td)/'cases.json';path.write_text(json.dumps(spec))
            fake=subprocess.CompletedProcess(['curl'],0,stdout='200')
            with patch.object(api_checks.subprocess,'run',return_value=fake) as run:
                self.assertEqual(api_checks.run(path),0)
                args, kwargs=run.call_args
                self.assertNotIn('unique-secret-value',' '.join(args[0]))
                self.assertIn('unique-secret-value',kwargs['input'])

    def test_non_get_requests_need_explicit_disposable_mutation_consent(self):
        with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,
                {'AGENTIC_TEST_API_BASE_URL':'http://127.0.0.1:39999'},clear=False):
            spec=Path(td)/'api.json'
            spec.write_text(json.dumps({'cases':[{'name':'must-not-run','method':'POST','path':'/payments',
                                                   'expected_status':201,'json_body':{'amount':5}}]}))
            self.assertEqual(api_checks.run(spec),1)

class SetupUxAndRolesTests(unittest.TestCase):
    def test_approval_prompt_default_is_explicit_and_invalid_answer_reprompts(self):
        questions=[]
        def answer(question):
            questions.append(question)
            return ['maybe', ''][len(questions)-1]
        with patch('builtins.input',side_effect=answer):
            self.assertEqual(eng.ask('Approve this configuration change? (yes/no)','no'),'no')
        self.assertEqual(len(questions),2)
        self.assertIn('(yes/no) [default: No]:',questions[0])
        self.assertNotIn('[no]',questions[0])
        with patch('builtins.input',return_value='yes'):
            self.assertEqual(eng.ask('Approve this configuration change? (yes/no)','no'),'yes')

    def test_invalid_confirmation_cannot_approve_external_changes(self):
        prompts=[]
        def answer(msg):
            prompts.append(msg)
            return ']' if len(prompts)==1 else 'no'
        with patch('builtins.input',side_effect=answer):
            self.assertFalse(setup_wizard.yesno('Install external tool?',default=True,interactive=True))
        self.assertEqual(len(prompts),2)
        self.assertIn('[default: Yes]',prompts[0])

    def test_all_setup_approvals_show_explicit_default_and_reject_invalid(self):
        for language, indicator in [('en', '(yes/no) [default: No]:'),
                                    ('fr', '(oui/non) [défaut : Non]:')]:
            prompts=[]
            token=setup_wizard.CURRENT_LANGUAGE.set(language)
            try:
                with patch('builtins.input',side_effect=lambda p: prompts.append(p) or ''):
                    self.assertFalse(setup_wizard.yesno('Approve installation?', interactive=True))
            finally:
                setup_wizard.CURRENT_LANGUAGE.reset(token)
            self.assertIn(indicator,prompts[0])
        prompts=[]
        with patch('builtins.input',side_effect=lambda p: prompts.append(p) or ('maybe' if len(prompts)==1 else 'yes')):
            self.assertTrue(setup_wizard.yesno('Approve?',default=False,interactive=True))
        self.assertEqual(len(prompts),2)

    def test_language_and_text_defaults_have_consistent_labels(self):
        for selected, expected in [('en','[default: English]'),('fr','[défaut : Français]')]:
            with patch('builtins.input',side_effect=lambda q: self.assertIn(expected,q) or ''):
                self.assertEqual(setup_wizard.choose_language(
                    {'preferences':{'setup_language':selected}},interactive=True),selected)
        for language, expected in [('en','[optional; Enter to skip]'),
                                   ('fr','[facultatif ; Entrée pour ignorer]')]:
            token=setup_wizard.CURRENT_LANGUAGE.set(language)
            try:
                with patch('builtins.input',side_effect=lambda q: self.assertIn(expected,q) or ''):
                    self.assertEqual(setup_wizard.prompt('Optional path',optional=True), '')
            finally:
                setup_wizard.CURRENT_LANGUAGE.reset(token)

    def test_french_approval_from_engine_validates_default(self):
        questions=[]
        with patch('builtins.input',side_effect=lambda q: questions.append(q) or ''):
            self.assertEqual(eng.ask('Autoriser cette modification ? (oui/non)','non'),'no')
        self.assertIn('(oui/non) [défaut : Non]:',questions[0])

    def test_user_facing_messages_never_recommend_removed_commands(self):
        root=Path(__file__).parent.parent
        paths=[root/'README.md',root/'.agentic/eng.py',root/'.agentic/setup_wizard.py',
               root/'.agentic/playwright_tools.py',root/'.agentic/setup_i18n.py']
        for path in paths:
            body=path.read_text()
            self.assertNotRegex(body,r'\./dev (?:maestro|ui|playwright|doctor|hooks|logs|run|verify|status|test)(?:\s|$)')
        for path in paths[1:]:
            body=path.read_text()
            self.assertNotRegex(body, r'\(yes/no, default|\(oui/non, par défaut|Enter preserves|Enter keeps')

    def test_setup_menu_selection_displays_actual_default(self):
        # A choice's fallback is always visible and corresponds to Enter behavior.
        src=(Path(__file__).parent/'setup_wizard.py').read_text()
        self.assertIn('Select AI setup (1/2/3) [default:',src)
        picker=(Path(__file__).parent/'model_menu.py').read_text()
        self.assertIn('Choose model (1–{manual} or exact ID)',picker)
        self.assertIn('Choose reasoning (1–{len(levels)} or exact level) [default:',picker)

    def test_setup_never_enumerates_or_launches_emulator(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(setup_wizard,'discover_stacks',return_value={'java':[],'flutter':[]}), \
             patch.object(setup_wizard,'ok_bin',return_value=False), \
             patch.object(setup_wizard,'android_avds',side_effect=AssertionError('should not inspect emulators')), \
             patch('builtins.input',side_effect=AssertionError('setup preview must not prompt')):
            self.assertTrue(setup_wizard.setup_preview(Path(td),{},'en',interactive=False))

    def test_mobile_preparation_never_launches_device(self):
        with patch.object(setup_wizard,'installed_java_major',return_value=17), \
             patch.object(setup_wizard,'mobile_device_running',side_effect=AssertionError('no device query')), \
             patch.object(setup_wizard,'android_avds',side_effect=AssertionError('no emulator list')), \
             patch.object(setup_wizard.subprocess,'Popen',side_effect=AssertionError('no emulator start')), \
             patch.object(eng,'detected_maestro',return_value='/tmp/maestro'):
            state=setup_wizard.mobile_test_readiness('maestro',flutter=None,lead_agents=())
        self.assertTrue(state['Maestro CLI'])
        self.assertNotIn('Connected Android/iOS emulator/device',state)


    def test_mcp_agent_selection_follows_lead_role_not_builder(self):
        self.assertEqual(setup_wizard.lead_mcp_agents({'builder':'codex','reviewer':'claude'}),('claude',))
        self.assertEqual(setup_wizard.lead_mcp_agents({'builder':'claude','reviewer':'claude'}),('claude',))
        self.assertEqual(setup_wizard.lead_mcp_agents({'builder':'codex','reviewer':'codex'}),())

    def test_maestro_skips_codex_user_wide_registration(self):
        commands=[]
        with patch.object(eng,'detected_maestro',return_value='/tmp/fake-maestro'), \
             patch.object(eng,'command_exists',return_value=True), \
             patch.object(eng,'java_major_version',return_value=21), \
             patch.object(eng,'ask',return_value='yes'), \
             patch.object(setup_wizard,'visible_run',side_effect=lambda cmd,**kw: commands.append(cmd) or subprocess.CompletedProcess(cmd,0)), \
             patch.object(eng.subprocess,'run',side_effect=lambda cmd,**kw: subprocess.CompletedProcess(cmd,
                 0 if '--help' in cmd else 1,stdout='',stderr='')):
            self.assertTrue(eng.maestro_setup(agents=('codex','claude'),yes=True))
        self.assertEqual(len(commands),1)
        self.assertEqual(commands[0][:5],['claude','mcp','add','--scope','local'])

    def test_playwright_mcp_registration_only_after_one_category_approval(self):
        commands=[]
        def run(cmd,**kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd,0)
        with tempfile.TemporaryDirectory() as td, \
             patch.object(playwright_tools,'describe'), \
             patch.object(playwright_tools,'node_major',return_value=22), \
             patch.object(playwright_tools.shutil,'which',return_value='/fake'), \
             patch.object(playwright_tools,'mcp_registered',return_value=False), \
             patch.object(playwright_tools,'environment_ready',return_value=False), \
             patch.object(setup_wizard,'visible_run',side_effect=run), \
             patch.object(setup_wizard,'yesno',side_effect=AssertionError('nested prompt forbidden')):
            playwright_tools.configure(Path(td),active_agents=('codex','claude'),
                                       allow_test_setup=False,interactive=True,approved=True)
        self.assertEqual(len(commands),1)
        self.assertEqual(commands[0][:5],['claude','mcp','add','--scope','local'])


class ResumableSetupTests(unittest.TestCase):
    def test_language_is_reused_without_prompt_and_can_be_explicitly_changed(self):
        cfg={'preferences':{'setup_language':'fr'}}
        with patch('builtins.input',side_effect=AssertionError('saved language must not re-prompt')):
            self.assertEqual(setup_wizard.choose_language(cfg,interactive=True),'fr')
        with patch('builtins.input',return_value='1') as response:
            self.assertEqual(setup_wizard.choose_language(cfg,interactive=True,force_prompt=True),'en')
            self.assertEqual(response.call_count,1)

    def test_go_remains_mandatory_on_repeated_setup(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(setup_wizard,'discover_stacks',return_value={'java':[],'flutter':[]}), \
             patch.object(setup_wizard,'ok_bin',return_value=False), \
             patch('builtins.input',side_effect=['not GO','GO']) as response:
            self.assertFalse(setup_wizard.setup_preview(Path(td),{},'en',interactive=True))
            self.assertTrue(setup_wizard.setup_preview(Path(td),{},'en',interactive=True))
            self.assertEqual(response.call_count,2)

    def test_mode_saved_immediately_and_reused_after_interrupt(self):
        from workspace_git import tracked_roots, baseline_ready
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            cfg_file=root/'.agentic/config.json'
            local=root/'.agentic/local'
            prefs={}
            def interrupt_install(*args,**kwargs):
                raise KeyboardInterrupt('simulate interrupted provider installation')
            common=[
                patch.object(setup_wizard,'setup_preview',return_value=True),
                patch.object(setup_wizard,'discover_stacks',return_value={'java':[],'flutter':[]}),
                patch.object(setup_wizard,'ok_bin',return_value=False),
                patch.object(setup_wizard,'install_agent',side_effect=interrupt_install),
                patch('workspace_git.tracked_roots',return_value=([],[])),
                patch('workspace_git.baseline_ready',return_value=False),
                patch.object(setup_wizard,'project_test_commands',return_value=[]),
                patch.object(setup_wizard,'choose_stack_root',return_value=None),
            ]
            def run(cfg):
                return setup_wizard.run_setup(root,cfg,interactive=True,
                    maestro_setup=lambda **kw:None,playwright_setup=lambda *a,**kw:None,
                    write_json=eng.write_json,config_file=cfg_file,local=local)
            with contextlib.ExitStack() as stack:
                for item in common: stack.enter_context(item)
                with patch('builtins.input',side_effect=['1','3']):
                    with self.assertRaises(KeyboardInterrupt): run({'preferences':prefs})
                saved=json.loads(cfg_file.read_text())
                self.assertEqual(saved['preferences']['setup_language'],'en')
                self.assertEqual(saved['preferences']['provider_mode'],'both')
                self.assertEqual(saved['preferences']['builder'],'codex')
                self.assertEqual(saved['preferences']['reviewer'],'claude')
                progress=json.loads((local/'setup-progress.json').read_text())
                self.assertEqual(progress['steps']['ai_architecture'],'saved')
                self.assertFalse(json.loads((local/'setup-status.json').read_text())['ready'])
                with patch.object(setup_wizard,'yesno',return_value=False) as change, \
                     patch('builtins.input',side_effect=AssertionError('saved choices should not re-prompt')):
                    with self.assertRaises(KeyboardInterrupt): run(saved)
                self.assertEqual(change.call_count,1)  # optional reconfiguration, not repeated choices
                self.assertEqual(saved['preferences']['provider_mode'],'both')

    def test_interrupted_model_selection_resumes_at_missing_reasoning(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); config=root/'.agentic/config.json'; local=root/'.agentic/local'
            current={'preferences':{'setup_language':'en','provider_mode':'claude',
                                    'builder':'claude','reviewer':'claude'},
                     'models':{},'reasoning':{},'checks':[]}
            common=[
                patch.object(setup_wizard,'setup_preview',return_value=True),
                patch.object(setup_wizard,'discover_stacks',return_value={'java':[],'flutter':[]}),
                patch.object(setup_wizard,'choose_stack_root',return_value=None),
                patch.object(setup_wizard,'ok_bin',return_value=True),
                patch.object(setup_wizard,'check_auth',return_value=True),
                patch.object(model_menu,'model_choices',return_value=[('claude-sonnet-5-5','claude-sonnet-5-5'),('claude-opus-5-5','claude-opus-5-5')]),
                patch.object(setup_wizard,'effort_choices',return_value=('low','medium','high')),
                patch.object(setup_wizard,'validate_selected_models',return_value=None),
                patch.object(setup_wizard,'project_test_commands',return_value=[]),
                patch('workspace_git.tracked_roots',return_value=([],[])),
                patch('workspace_git.baseline_ready',return_value=False),
                patch.object(setup_wizard,'yesno',return_value=False),
            ]
            def run(settings):
                return setup_wizard.run_setup(root,settings,interactive=True,
                    maestro_setup=lambda **kwargs:None,
                    playwright_setup=lambda *args,**kwargs:None,
                    write_json=eng.write_json,config_file=config,local=local)
            with contextlib.ExitStack() as stack:
                for item in common:stack.enter_context(item)
                # Both model choices accepted; stop before the first effort answer.
                with patch('builtins.input',side_effect=['claude-sonnet-5-5','2',KeyboardInterrupt()]):
                    with self.assertRaises(KeyboardInterrupt):run(current)
                saved=json.loads(config.read_text())
                self.assertEqual(saved['models']['claude_builder'],'claude-sonnet-5-5')
                self.assertEqual(saved['models']['claude_reviewer'],'claude-opus-5-5')
                self.assertNotIn('claude_builder',saved['reasoning'])
                # On resume, ask for the two missing efforts; do not repeat models.
                with patch.object(setup_wizard,'testing_category_approval',
                                  side_effect=KeyboardInterrupt()), \
                     patch('builtins.input',side_effect=['1','2']) as answers:
                    with self.assertRaises(KeyboardInterrupt):run(saved)
                self.assertEqual(answers.call_count,2)
                saved=json.loads(config.read_text())
                self.assertEqual(saved['reasoning']['claude_builder'],'low')
                self.assertEqual(saved['reasoning']['claude_reviewer'],'medium')
                self.assertTrue(saved['preferences']['models_configured'])
                with patch.object(setup_wizard,'testing_category_approval',
                                  side_effect=KeyboardInterrupt()), \
                     patch('builtins.input',side_effect=AssertionError('models should not be asked again')):
                    with self.assertRaises(KeyboardInterrupt):run(saved)

    def test_architecture_change_keeps_existing_role_model_settings(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); config=root/'.agentic/config.json'; local=root/'.agentic/local'
            original={'preferences':{'setup_language':'en','provider_mode':'both',
                                     'builder':'codex','reviewer':'claude','models_configured':True},
                      'models':{'codex_builder':'gpt-custom','claude_reviewer':'sonnet'},
                      'reasoning':{'codex_builder':'low','claude_reviewer':'medium'},
                      'checks':[]}
            common=[
                patch.object(setup_wizard,'setup_preview',return_value=True),
                patch.object(setup_wizard,'discover_stacks',return_value={'java':[],'flutter':[]}),
                patch.object(setup_wizard,'choose_stack_root',return_value=None),
                patch.object(setup_wizard,'ok_bin',return_value=False),
                patch.object(setup_wizard,'install_agent',side_effect=KeyboardInterrupt()),
                patch.object(setup_wizard,'project_test_commands',return_value=[]),
                patch('workspace_git.tracked_roots',return_value=([],[])),
                patch('workspace_git.baseline_ready',return_value=False),
            ]
            with contextlib.ExitStack() as stack:
                for item in common:stack.enter_context(item)
                with patch.object(setup_wizard,'yesno',return_value=True), \
                     patch('builtins.input',side_effect=['','1']):
                    with self.assertRaises(KeyboardInterrupt):
                        setup_wizard.run_setup(root,original,interactive=True,
                            maestro_setup=lambda **kwargs:None,
                            playwright_setup=lambda *a,**kw:None,
                            write_json=eng.write_json,config_file=config,local=local)
            saved=json.loads(config.read_text())
            self.assertEqual(saved['preferences']['provider_mode'],'codex')
            self.assertEqual(saved['preferences']['reviewer'],'codex')
            self.assertEqual(saved['models']['codex_builder'],'gpt-custom')
            self.assertEqual(saved['models']['claude_reviewer'],'claude-sonnet-5-5')
            self.assertFalse(json.loads((local/'setup-status.json').read_text())['ready'])

    def test_category_approval_is_reused_only_interactively(self):
        approved=[]
        with patch.object(setup_wizard,'yesno',return_value=True) as questions:
            self.assertTrue(setup_wizard.testing_category_approval(
                'Web E2E',{'Chromium':False},interactive=True,lang='en',
                on_approve=lambda:approved.append(True)))
            self.assertEqual(questions.call_count,1)
        self.assertEqual(approved,[True])
        with patch.object(setup_wizard,'yesno',side_effect=AssertionError('already approved')):
            self.assertTrue(setup_wizard.testing_category_approval(
                'Web E2E',{'Chromium':False},interactive=True,lang='en',resume_approved=True))
        with patch('builtins.input',side_effect=AssertionError('noninteractive no prompts')):
            self.assertFalse(setup_wizard.testing_category_approval(
                'Web E2E',{'Chromium':False},interactive=False,lang='en',resume_approved=True))

    def test_progress_is_workspace_scoped_and_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            x=setup_wizard._load_progress(p,p/'workspace-a')
            x['steps']['language']='saved'
            setup_wizard._atomic_json(p/'setup-progress.json',x)
            self.assertEqual(setup_wizard._load_progress(p,p/'workspace-a')['steps']['language'],'saved')
            self.assertEqual(setup_wizard._load_progress(p,p/'workspace-b')['steps'],{})
            self.assertEqual(list(p.glob('.setup-*.tmp')),[])


class TestingEssentialsTests(unittest.TestCase):
    def test_full_environment_auto_skips_with_zero_questions(self):
        for name in ('Web E2E','Mobile testing','Backend testing'):
            with self.subTest(name=name),patch.object(setup_wizard,'yesno',side_effect=AssertionError('must not ask')):
                self.assertFalse(setup_wizard.testing_category_approval(name,{'a':True,'b':True},
                                                                         interactive=True,lang='en'))

    def test_incomplete_category_asks_exactly_once_default_yes(self):
        for name in ('Web E2E','Mobile testing','Backend testing'):
            with self.subTest(name=name),patch('builtins.input',return_value='') as answer:
                self.assertTrue(setup_wizard.testing_category_approval(name,{'existing':True,'missing':False},
                                                                        interactive=True,lang='en'))
                self.assertEqual(answer.call_count,1)
                self.assertIn('[default: Yes]',answer.call_args.args[0])

    def test_deny_and_noninteractive_do_not_authorize(self):
        with patch('builtins.input',return_value='no') as answer:
            self.assertFalse(setup_wizard.testing_category_approval('Web E2E',{'missing':False},
                                                                     interactive=True,lang='en'))
            self.assertEqual(answer.call_count,1)
        with patch('builtins.input',side_effect=AssertionError('noninteractive prompt forbidden')):
            self.assertFalse(setup_wizard.testing_category_approval('Mobile testing',{'missing':False},
                                                                     interactive=False,lang='en'))

    def test_no_project_skips_backend_prompt(self):
        with patch.object(setup_wizard,'yesno',side_effect=AssertionError('should skip')):
            self.assertFalse(setup_wizard.testing_category_approval('Backend testing',{},
                                                                     interactive=True,lang='en'))

    def test_existing_project_playwright_environment_is_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            project=root/'frontend'
            (project/'node_modules/@playwright/test').mkdir(parents=True)
            (project/'node_modules/@playwright/test/package.json').write_text('{}')
            (project/'playwright.config.ts').write_text('export default {}')
            (project/'e2e').mkdir()
            (project/'e2e/login.spec.ts').write_text('test("login", ()=>{})')
            self.assertTrue(playwright_tools.package_installed(root))
            self.assertEqual(playwright_tools.test_workspace(root),project)
            self.assertEqual(playwright_tools.browser_gate(root)['cwd'],'frontend')
            # An already configured project must not create a duplicate toolkit harness.
            self.assertFalse((root/playwright_tools.HARNESS).exists())

    def test_web_partial_install_reuses_test_and_adds_only_browser(self):
        runs=[]
        with tempfile.TemporaryDirectory() as td, \
             patch.object(playwright_tools,'describe'), \
             patch.object(playwright_tools,'node_major',return_value=22), \
             patch.object(playwright_tools.shutil,'which',return_value='/fake'), \
             patch.object(playwright_tools,'mcp_registered',return_value=True), \
             patch.object(playwright_tools,'package_installed',return_value=True), \
             patch.object(playwright_tools,'chromium_installed',return_value=False), \
             patch.object(playwright_tools,'environment_ready',return_value=False), \
             patch.object(setup_wizard,'visible_run',side_effect=lambda cmd,**kwargs: (runs.append(cmd) or subprocess.CompletedProcess(cmd,0))), \
             patch.object(setup_wizard,'yesno',side_effect=AssertionError('nested prompt')):
            playwright_tools.configure(Path(td),active_agents=('claude',),interactive=True,approved=True)
        self.assertEqual(runs,[['npm','exec','--','playwright','install','chromium']])

    def test_java_backend_is_not_falsely_marked_ready_without_docker(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'backend').mkdir();(root/'backend/pom.xml').write_text('<project/>')
            with patch.object(setup_wizard,'installed_java_major',return_value=21), \
                 patch.object(setup_wizard,'ok_bin',side_effect=lambda name:name in ('mvn','curl')):
                state=setup_wizard.backend_test_readiness(root,'backend')
            self.assertTrue(state['Java >= 17'])
            self.assertTrue(state['Java test runner'])
            self.assertTrue(state['curl'])
            self.assertFalse(state['Docker engine (disposable PostgreSQL prerequisite)'])
            self.assertFalse(state['PostgreSQL client (psql)'])




class ManagedSessionsSetupTests(unittest.TestCase):
    def test_no_manual_session_id_questions_and_lazy_creation(self):
        from session_store import Store
        import io
        import uuid
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td))
            output=io.StringIO()
            with contextlib.redirect_stdout(output), patch('builtins.input',side_effect=AssertionError('setup must not ask for IDs')):
                setup_wizard.report_registered_sessions(store, 'codex', 'claude', 'en')
            self.assertIn('created automatically on first use', output.getvalue())
            self.assertIn('No session IDs to enter', output.getvalue())
            self.assertIsNone(store.get('codex','builder'))
            self.assertIsNone(store.get('claude','reviewer'))
            first=str(uuid.uuid4()); second=str(uuid.uuid4())
            store.bind('codex',first,role='builder')
            store.bind('claude',second,role='reviewer')
            out2=io.StringIO()
            with contextlib.redirect_stdout(out2), patch('builtins.input',side_effect=AssertionError('setup must not ask for IDs')):
                setup_wizard.report_registered_sessions(store,'codex','claude','en')
            self.assertIn(first, out2.getvalue())
            self.assertIn(second, out2.getvalue())
            self.assertNotIn('created automatically', out2.getvalue())
            self.assertEqual(store.get('codex','builder')['id'],first)
            self.assertEqual(store.get('claude','reviewer')['id'],second)

    def test_same_provider_roles_have_independent_lifecycle(self):
        from session_store import Store
        import io
        import uuid
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td))
            builder_id=str(uuid.uuid4())
            store.bind('codex',builder_id,role='builder')
            output=io.StringIO()
            with contextlib.redirect_stdout(output), patch('builtins.input',side_effect=AssertionError('No manual ID prompts')):
                setup_wizard.report_registered_sessions(store,'codex','codex','en')
            self.assertEqual(store.get('codex','builder')['id'],builder_id)
            self.assertIsNone(store.get('codex','reviewer'))
            self.assertIn('codex/reviewer: will be created automatically',output.getvalue())
            self.assertIn(builder_id,output.getvalue())

    def test_setup_source_has_no_prompt_to_bind_existing_sessions(self):
        import inspect
        source=inspect.getsource(setup_wizard.run_setup)
        self.assertNotIn('Existing {agent}', source)
        self.assertNotIn('session_prompted', source)
        self.assertNotIn('registry.bind', source)


class DynamicProjectDiscoveryTests(unittest.TestCase):
    def _make_fltr(self, root, name='apps/mobile'):
        folder=root/name
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'pubspec.yaml').write_text('name: sample\nflutter:\n  uses-material-design: true\n')
        return folder

    def _make_web(self, root, name='apps/web'):
        folder=root/name
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'package.json').write_text(json.dumps({'name':'ui',
              'devDependencies':{'@playwright/test':'^1.0.0'},
              'scripts':{'test:e2e':'playwright test'}}))
        (folder/'playwright.config.ts').write_text('export default {};')
        (folder/'tests').mkdir()
        (folder/'tests/login.spec.ts').write_text('test("login",()=>{})')
        return folder

    def test_new_repos_appear_after_initial_setup_and_stale_gates_go_away(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            backend=root/'backend'
            backend.mkdir()
            (backend/'pom.xml').write_text('<project/>')
            cfg={'project':{'java_root':'backend','flutter_root':None},
                 'preferences':{'mobile_e2e':'maestro'},'checks':[]}
            initial,changed=workspace_checks.refresh(root,cfg)
            self.assertTrue(changed)
            self.assertEqual([s['name'] for s in initial['checks']],['backend_verify'])
            self.assertEqual(initial['project']['discovered_roots']['web'],[])
            mobile=self._make_fltr(root)
            web=self._make_web(root)
            refreshed,changed=workspace_checks.refresh(root,initial)
            self.assertTrue(changed)
            names={s['name'] for s in refreshed['checks']}
            self.assertTrue({'backend_verify','flutter_analyze','flutter_tests','mobile_e2e','web_e2e'}<=names)
            self.assertEqual(refreshed['project']['discovered_roots']['flutter'],['apps/mobile'])
            self.assertEqual(refreshed['project']['discovered_roots']['web'],['apps/web'])
            self.assertFalse(workspace_checks.refresh(root,refreshed)[1])
            (web/'tests/login.spec.ts').unlink()
            latest,_=workspace_checks.refresh(root,refreshed)
            self.assertNotIn('web_e2e',{s['name'] for s in latest['checks']})
            self.assertIn('apps/web',latest['project']['discovered_roots']['web'])
            (mobile/'pubspec.yaml').unlink()
            latest,_=workspace_checks.refresh(root,latest)
            self.assertNotIn('flutter_tests',{s['name'] for s in latest['checks']})

    def test_project_specific_web_and_flutter_changes_select_relevant_checks(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self._make_fltr(root)
            self._make_web(root)
            cfg={'preferences':{'mobile_e2e':'maestro'},'project':{},'checks':[]}
            discovered,_=workspace_checks.refresh(root,cfg)
            with patch.object(eng,'ROOT',root),patch.object(eng.workspace_git,'changed_paths',
                       return_value=['apps/web/src/api/client.ts']):
                planned=eng.verification_plan(cfg,{'goal':'Fix web API client'})
            self.assertIn('web_e2e',planned['selected'])
            self.assertNotIn('flutter_tests',planned['selected'])
            with patch.object(eng,'ROOT',root),patch.object(eng.workspace_git,'changed_paths',
                       return_value=['apps/mobile/lib/screen.dart']):
                planned=eng.verification_plan(cfg,{'goal':'Repair mobile screen'})
            self.assertIn('flutter_analyze',planned['selected'])
            self.assertIn('flutter_tests',planned['selected'])
            self.assertNotIn('web_e2e',planned['selected'])

    def test_missing_web_tests_not_reported_as_ready(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            web=self._make_web(root)
            (web/'tests/login.spec.ts').unlink()
            cfg={'preferences':{},'project':{},'checks':[]}
            discovered,_=workspace_checks.refresh(root,cfg)
            self.assertNotIn('web_e2e',{s['name'] for s in discovered['checks']})
            with patch.object(eng,'ROOT',root),patch.object(eng.workspace_git,'changed_paths',
                      return_value=['apps/web/src/main.ts']):
                planned=eng.verification_plan(cfg,{'goal':'Add web checkout'})
            self.assertIn('web_e2e',planned['missing'])

    def test_custom_checks_and_nested_git_roots_preserved(self):
        import workspace_git
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self._make_fltr(root,'apps/mobile')
            self._make_web(root,'apps/portal')
            custom={'name':'security_checks','cwd':'.','command':['python','-V']}
            cfg={'preferences':{'mobile_e2e':'maestro'},'project':{},'checks':[custom]}
            updated,_=workspace_checks.refresh(root,cfg)
            self.assertIn(custom,updated['checks'])
            # A separately nested app Git repo is in the review scope.
            app=root/'apps/mobile'
            subprocess.run(['git','init','-q'],cwd=app,check=True)
            self.assertIn(app.resolve(),workspace_git.repositories(root,updated['project']))

    def test_setup_step_four_explicitly_describes_current_readiness(self):
        import inspect
        source=inspect.getsource(setup_wizard.run_setup)
        self.assertIn('ui.step(4',source)
        self.assertIn('Project testing — current readiness',source)
        self.assertIn('New repositories and tests are discovered automatically',source)
        self.assertNotIn('STEP 4/5  Available test commands',source)

    def test_missing_web_dependency_is_blocked_not_misreported_as_runnable(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            web=self._make_web(root)
            cfg={'preferences':{},'project':{},'checks':[]}
            discovered,_=workspace_checks.refresh(root,cfg)
            gate=next(c for c in discovered['checks'] if c['name']=='web_e2e')
            self.assertFalse(workspace_checks.runner_preflight(root,gate)[0])
            modules=web/'node_modules/@playwright/test'
            modules.mkdir(parents=True)
            (modules/'package.json').write_text('{}')
            self.assertTrue(workspace_checks.runner_preflight(root,gate)[0])

    def test_second_web_app_gets_separate_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self._make_web(root,'apps/web')
            self._make_web(root,'services/portal')
            cfg={'preferences':{},'project':{},'checks':[]}
            discovered,_=workspace_checks.refresh(root,cfg)
            gates=[s for s in discovered['checks'] if s['name'].startswith('web_e2e')]
            self.assertEqual(len(gates),2)
            self.assertEqual({s['cwd'] for s in gates},{'apps/web','services/portal'})


class PublicCLIContractTests(unittest.TestCase):
    def test_only_agreed_five_commands_present(self):
        root=Path(__file__).parent.parent
        help_result=subprocess.run(['sh','dev','--help'],cwd=root,capture_output=True,text=True,timeout=15)
        self.assertEqual(help_result.returncode,0,help_result.stderr)
        displayed=help_result.stdout
        match=re.search(r'\{([^}]+)\}',displayed)
        self.assertIsNotNone(match,displayed)
        self.assertEqual(set(match.group(1).split(',')),{'init','sessions','models','uninstall'})
        self.assertIn('./dev',displayed)

    def test_non_public_commands_are_rejected_before_setup(self):
        root=Path(__file__).parent.parent
        for command in ('maestro','playwright','ui','hooks','doctor','logs','rules',
                        'handoff','chat','status','summary','context','feedback','do',
                        'run','verify','review','continue','lead'):
            with self.subTest(command=command):
                r=subprocess.run(['sh','dev',command],cwd=root,
                                 capture_output=True,text=True,timeout=15)
                self.assertEqual(r.returncode,2)
                self.assertIn('invalid choice',r.stderr)
        for command in (('models','advisor','fable'),('sessions','show'),('models','check')):
            with self.subTest(command=command):
                r=subprocess.run(['sh','dev',*command],cwd=root,
                                 capture_output=True,text=True,timeout=15)
                self.assertEqual(r.returncode,2)
                self.assertIn('unrecognized arguments',r.stderr)

    def test_registered_native_hook_starts_private_worker(self):
        import inspect
        source=inspect.getsource(native_hooks.schedule_worker)
        self.assertIn('hook_worker.py',source)
        self.assertNotIn("'hooks', 'process'",source)
        src=inspect.getsource(eng.perform_auto_fix)
        self.assertIn('pipeline_worker.py',src)
        self.assertNotIn("'run',task_id",src)

class MinimalTerminalUiTests(unittest.TestCase):
    def test_plain_output_never_contains_ansi_when_redirected(self):
        import io
        import terminal_ui as ui
        output = io.StringIO()
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}, clear=True), \
             contextlib.redirect_stdout(output):
            ui.heading('Gaston Agentic', 'Setup')
            ui.step(4, 'Project testing — current readiness')
            ui.result('Git installed', 'OK')
            ui.result('Docker missing', 'WARNING', detail='Install Docker locally')
            ui.row('Flutter', '', state='Not detected')
            ui.note('New repositories and tests are discovered automatically.')
        rendered = output.getvalue()
        self.assertNotIn('\x1b[', rendered)
        self.assertIn('STEP 4/5  Project testing — current readiness', rendered)
        self.assertIn('OK', rendered)
        self.assertIn('Not detected', rendered)

    def test_palette_requires_real_terminal_and_respects_no_color(self):
        import io
        import terminal_ui as ui
        class Tty(io.StringIO):
            def isatty(self): return True
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}, clear=True):
            self.assertIn('\x1b[', ui.paint('STEP 1/5', 'blue', stream=Tty()))
            self.assertEqual(ui.paint('STEP 1/5', 'blue', stream=io.StringIO()), 'STEP 1/5')
        for extra in ({'NO_COLOR': ''}, {'CLICOLOR': '0'}, {'TERM': 'dumb'}):
            with patch.dict(os.environ, {'TERM': 'xterm-256color', **extra}, clear=True):
                self.assertEqual(ui.paint('OK', 'green', stream=Tty()), 'OK')

    def test_summary_is_concise_and_report_path_hidden(self):
        import inspect
        source = inspect.getsource(setup_wizard.run_setup)
        self.assertIn("'Setup complete.'", source)
        self.assertIn("'Run ./dev to start.'", source)
        self.assertNotIn("'  Report: .agentic/local/setup-status.json", source)
        self.assertIn("_atomic_json(local/'setup-status.json', report)", source)

    def test_french_step_layout_and_rows(self):
        import io
        import terminal_ui as ui
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            ui.step(5, 'Résumé')
            ui.row('Backend', 'sample-backend', state='Détecté')
            ui.row('Flutter', '', state='Non détecté')
        self.assertIn('STEP 5/5  Résumé', output.getvalue())
        self.assertIn('sample-backend', output.getvalue())
        self.assertIn('Non détecté', output.getvalue())


class IndependentVerifierTests(unittest.TestCase):
    def test_mcp_handshake_and_tool_list_use_jsonrpc(self):
        import verification_mcp as vm
        info=vm.handle({'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        self.assertEqual(info['serverInfo']['name'],'gaston-verify')
        names={x['name'] for x in vm.handle({'method':'tools/list'})['tools']}
        self.assertIn('run_web_smoke',names)
        self.assertIn('start_existing_emulator',names)
        self.assertNotIn('run_shell',names)

    def test_unrecognized_mcp_tools_do_not_execute(self):
        import verification_mcp as vm
        with patch.object(vm,'execute',side_effect=AssertionError('must not execute')):
            r=vm.handle({'method':'tools/call','params':{'name':'run_shell','arguments':{'cmd':'rm -rf /'}}})
        self.assertTrue(r['isError'])

    def test_remote_browser_tests_blocked(self):
        import verification_mcp as vm
        with patch.dict(os.environ, {'PLAYWRIGHT_BASE_URL':'https://example.org'}, clear=False), \
             patch.object(vm,'playwright_folder',return_value=Path('/tmp/fake')):
            r=vm.run_project_web_tests()
        self.assertEqual(r['status'],'blocked')

    def test_emulator_name_must_match_installed_avd(self):
        import verification_mcp as vm
        with patch.object(vm,'sdk_tool',return_value='/fake/emulator'), \
             patch.object(vm,'avd_names',return_value=['Small_Phone']), \
             patch.object(vm,'connected_emulators',return_value=[]), \
             patch.object(vm.subprocess,'Popen',side_effect=AssertionError('unexpected launch')):
            r=vm.start_existing_emulator('not_available')
        self.assertEqual(r['status'],'blocked')

    def test_emulator_starts_existing_avd_without_shell(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(vm,'ROOT',Path(temp)), \
             patch.object(vm,'LOCAL',Path(temp)), \
             patch.object(vm,'sdk_tool',return_value='/fake/emulator'), \
             patch.object(vm,'avd_names',return_value=['Small_Phone']), \
             patch.object(vm,'connected_emulators',return_value=[]), \
             patch.object(vm.time,'sleep',return_value=None), \
             patch.object(vm.subprocess,'Popen',return_value=SimpleNamespace(pid=77, poll=lambda:None)) as start:
            r=vm.start_existing_emulator('Small_Phone')
        self.assertEqual(r['status'],'starting')
        self.assertEqual(start.call_args.args[0][:3],['/fake/emulator','-avd','Small_Phone'])
        self.assertEqual(start.call_args.kwargs['stdin'],subprocess.DEVNULL)

    def test_no_physical_device_for_maestro(self):
        import verification_mcp as vm
        with patch.object(vm,'connected_emulators',return_value=[]), \
             patch.object(vm,'execute',side_effect=AssertionError('no test should run')):
            result=vm.run_maestro_tests()
        self.assertEqual(result['status'],'blocked')

    def test_maestro_targets_existing_emulator_with_documented_device_flag(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            flow=folder/'.maestro/smoke.yaml'
            flow.parent.mkdir()
            flow.write_text('appId: com.android.settings\n---\n- launchApp\n')
            with patch.object(vm,'ROOT',folder), \
                 patch.object(vm,'connected_emulators',return_value=['emulator-5554']), \
                 patch.object(vm.shutil,'which',return_value='/bin/maestro'), \
                 patch.object(vm,'execute',return_value={'exit_code':0,'stdout':'passed','stderr':''}) as run:
                result=vm.run_maestro_tests()
            self.assertEqual(result['status'],'pass')
            self.assertEqual(run.call_args.args[0][:5],
                             ['/bin/maestro','--device','emulator-5554','test',str(flow.parent)])

    def test_lead_mcp_config_is_private_to_role_and_native_hooks_allow_it(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(eng,'LOCAL',Path(tmp)):
            config=eng.lead_verification_mcp_config()
            data=json.loads(config.read_text())
        self.assertIn('gaston_verify',data['mcpServers'])
        self.assertNotIn('maestro',data['mcpServers'])
        self.assertTrue(data['mcpServers']['gaston_verify']['args'][0].endswith('verification_mcp.py'))
        self.assertIsNone(native_hooks.block_native_reviewer_tool({'tool_name':'mcp__gaston_verify__run_web_smoke'}))
        self.assertIsNone(native_hooks.block_native_reviewer_tool({'tool_name':'mcp__gaston_browser__browser_navigate'}))
        self.assertIsNone(native_hooks.block_native_reviewer_tool({'tool_name':'Bash'}))

    def test_managed_browser_mcp_avoids_system_chrome_and_is_headless(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(eng,'LOCAL',Path(temp)), \
             patch.object(eng.playwright_tools,'test_workspace',return_value=Path(temp)), \
             patch.object(eng.playwright_tools,'package_installed',return_value=True), \
             patch.object(eng.playwright_tools,'chromium_installed',return_value=True), \
             patch.object(eng.shutil,'which',return_value='/usr/bin/npx'), \
             patch.object(eng.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=__file__+'\n')):
            cfg=json.loads(eng.lead_verification_mcp_config().read_text())
        browser=cfg['mcpServers']['gaston_browser']
        self.assertIn('--headless',browser['args'])
        self.assertIn('--executable-path',browser['args'])
        self.assertEqual(browser['env']['GSETTINGS_BACKEND'],'memory')

    def test_legacy_review_command_exposes_verification_and_guarded_bash(self):
        import inspect
        source=inspect.getsource(eng.claude_structured)
        self.assertIn('lead_verification_mcp_config()',source)
        self.assertIn('claude_lead_permission_args()',source)
        self.assertNotIn("'--tools'",source)

    def test_project_chromium_probe_reuses_detected_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); app=root/'web'
            (app/'node_modules/@playwright/test').mkdir(parents=True)
            (app/'node_modules/@playwright/test/package.json').write_text('{}')
            (app/'playwright.config.js').write_text('module.exports={}')
            binary=app/'chromium'
            binary.write_text('binary')
            with patch.object(playwright_tools.shutil,'which',return_value='/usr/bin/node'), \
                 patch.object(playwright_tools.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=str(binary)+'\n')) as probe:
                self.assertTrue(playwright_tools.chromium_installed(root))
            self.assertEqual(probe.call_args.kwargs['cwd'],app)

    def test_browser_smoke_cleans_temporary_fixtures(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'browser'
            (folder/'node_modules/@playwright/test').mkdir(parents=True)
            with patch.object(vm,'LOCAL',Path(tmp)), \
                 patch.object(vm,'playwright_folder',return_value=folder), \
                 patch.object(vm.shutil,'which',return_value='/usr/bin/node'), \
                 patch.object(vm,'execute',return_value={'exit_code':0,'stdout':'1 passed','stderr':''}) as run:
                result=vm.run_web_smoke()
            self.assertEqual(result['status'],'pass')
            self.assertFalse(list((Path(tmp)/'verification-scratch').iterdir()))
            self.assertIn('playwright',str(run.call_args.args[0]).lower())



class LiveDelegationAndTrustTests(unittest.TestCase):
    def test_reviewer_mcp_requires_role_scoped_token(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td); token=folder/'lead-verification-token'; token.write_text('a'*64)
            with patch.object(vm,'LOCAL',folder), patch.dict(os.environ,{'GASTON_LEAD_TOKEN':'b'*64}):
                self.assertFalse(vm._authorized_delegate())
            with patch.object(vm,'LOCAL',folder), patch.dict(os.environ,{'GASTON_LEAD_TOKEN':'a'*64}):
                self.assertTrue(vm._authorized_delegate())

    def test_delegation_submitted_without_extra_model_call_or_chat_close(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td); runs=folder/'.agentic/runs'; runs.mkdir(parents=True)
            rd=runs/'smoke-12345-abcde'; rd.mkdir()
            (rd/'task.json').write_text(json.dumps({'goal':'Add a safe isolated regression test','acceptance_criteria':[]}),encoding='utf8')
            config={'preferences':{'builder':'codex','reviewer':'claude'}}
            with patch.object(vm,'_authorized_delegate',return_value=True), \
                 patch.object(vm,'ROOT',folder), \
                 patch.object(eng,'require_setup_complete'), \
                 patch.object(eng,'load_config',return_value=config), \
                 patch.object(eng,'SESSIONS',SimpleNamespace(get=lambda *a:{'id':'registered'})), \
                 patch.object(eng,'new_task',return_value='smoke-12345-abcde') as create, \
                 patch.object(eng,'run_dir',return_value=rd), \
                 patch.object(eng,'save_state'), \
                 patch.object(vm.subprocess,'Popen',return_value=SimpleNamespace(pid=51)) as start:
                outcome=vm.delegate_to_builder('Add a safe isolated regression test')
            self.assertEqual(outcome['status'],'queued')
            self.assertEqual(outcome['task_id'],'smoke-12345-abcde')
            self.assertTrue(start.called)
            self.assertEqual(start.call_args.kwargs['stdin'],subprocess.DEVNULL)
            self.assertNotIn('GASTON_LEAD_TOKEN',start.call_args.kwargs['env'])
            self.assertIn('live_builder_worker.py',str(start.call_args.args[0]))
            self.assertEqual(create.call_count,1)

    def test_high_risk_and_missing_lead_never_delegate(self):
        import verification_mcp as vm
        with patch.object(vm,'_authorized_delegate',return_value=True), \
             patch.object(eng,'require_setup_complete'), \
             patch.object(eng,'load_config',return_value={'preferences':{'builder':'codex','reviewer':'claude'}}), \
             patch.object(eng,'SESSIONS',SimpleNamespace(get=lambda *a:None)):
            self.assertEqual(vm.delegate_to_builder('Update an ordinary file')['status'],'blocked')
        with patch.object(vm,'_authorized_delegate',return_value=True), \
             patch.object(eng,'require_setup_complete'), \
             patch.object(eng,'load_config',return_value={'preferences':{'builder':'codex','reviewer':'claude'}}), \
             patch.object(eng,'SESSIONS',SimpleNamespace(get=lambda *a:{'id':'ok'})), \
             patch.object(vm.subprocess,'Popen',side_effect=AssertionError('Should not launch')):
            self.assertEqual(vm.delegate_to_builder('Delete production customer data',risk='high')['status'],'blocked')
            self.assertEqual(vm.delegate_to_builder('Deploy to production right now',risk='high')['status'],'blocked')

    def test_delegation_status_never_invents_a_review_pass(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); task=root/'.agentic/runs/demo-task'; task.mkdir(parents=True)
            (task/'task.json').write_text('{}')
            (task/'state.json').write_text('{"stage":"awaiting_lead_review"}')
            (task/'verify.json').write_text('{"all_passed":true,"checks":[{"name":"unit","result":"pass"}]}')
            with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True):
                report=vm.delegation_status('demo-task')
            self.assertFalse(report['all_passed'])
            self.assertTrue(report['lead_must_review'])
            self.assertEqual(report['stage'],'awaiting_lead_review')

    def test_codex_hook_trust_stored_in_provider_only(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'project';home=Path(td)/'home'
            (root/'.codex').mkdir(parents=True)
            (root/'.codex/hooks.json').write_text('{"hooks":{}}')
            config=home/'.codex/config.toml';config.parent.mkdir(parents=True)
            self.assertEqual(native_hooks.codex_trust_status(root,home=home)['status'],'needs_provider_review')
            hook=str(root/'.codex/hooks.json')
            lines=[]
            for key in ('pre_tool_use','session_start','session_end','stop'):
                lines.append(f'[hooks.state."file:{hook}:{key}:0:0"]')
                lines.append('trusted_hash = "sha256:user-approved"')
            config.write_text('\n'.join(lines)+'\n')
            self.assertEqual(native_hooks.codex_trust_status(root,home=home)['status'],'trust_recorded')
            self.assertEqual(native_hooks.codex_trust_status(root,home=home)['trusted_recorded'],4)

    def test_different_roles_of_same_provider_do_not_block(self):
        from session_store import Store
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td))
            with store.locked('codex','reviewer'):
                with store.locked('codex','builder'):
                    self.assertTrue((store.locks/'codex-builder').exists())
                with self.assertRaisesRegex(RuntimeError,'native chat is active'):
                    with store.locked('codex','reviewer'):pass

    def test_stale_pid_lock_is_auto_recovered_and_live_lock_retained(self):
        from session_store import Store
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)); path=store.locks/'codex-builder';path.mkdir(parents=True)
            (path/'owner.json').write_text(json.dumps({'pid':999999,'host':__import__('socket').gethostname()}))
            with patch.object(native_hooks,'session_active',return_value=False), \
                 patch('session_store.os.kill',side_effect=ProcessLookupError):
                with store.locked('codex','builder'):
                    self.assertTrue((path/'owner.json').exists())
            self.assertFalse(path.exists())
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)); path=store.locks/'codex-builder';path.mkdir(parents=True)
            (path/'owner.json').write_text(json.dumps({'pid':os.getpid(),'host':__import__('socket').gethostname()}))
            with patch.object(native_hooks,'session_active',return_value=False):
                with self.assertRaises(RuntimeError):
                    with store.locked('codex','builder'):pass
            self.assertTrue(path.exists())

    def test_codex_lead_mcp_is_per_invocation_not_global(self):
        lease='test-codex-lead-lease'
        session=SimpleNamespace(_held_leases={lease:('codex','reviewer')})
        with tempfile.TemporaryDirectory() as td, patch.object(eng,'LOCAL',Path(td)), \
             patch.object(eng,'SESSIONS',session), patch.dict(os.environ,{'GASTON_SESSION_LEASE':lease}):
            args=eng.codex_lead_mcp_args()
        self.assertEqual(args.count('-c'),7)
        self.assertTrue(any(a.startswith('mcp_servers.gaston_verify.command=') for a in args))
        self.assertTrue(any(a.startswith('mcp_servers.gaston_verify.env.GASTON_LEAD_TOKEN=') for a in args))
        self.assertEqual(len(args),14)
        self.assertIn('mcp_servers.gaston_verify.env.GASTON_SESSION_LEASE='+json.dumps(lease),args)
        self.assertIn('mcp_servers.gaston_verify.env.GASTON_LEAD_PROVIDER='+json.dumps('codex'),args)
        self.assertIn('mcp_servers.gaston_verify.env_vars='+json.dumps(['GASTON_READONLY_DATABASE_URL','GASTON_DATABASE_ALLOW_FIXTURE_DATA']),args)
        self.assertTrue(any(a.startswith('mcp_servers.gaston_verify.tool_timeout_sec=') for a in args))

    def test_hook_review_reuses_registered_codex_session_when_possible(self):
        from session_store import Store
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);store=Store(root)
            sid='00000000-0000-4000-8000-000000000001'
            store.bind('codex',sid,role='builder')
            process=SimpleNamespace(returncode=0, poll=lambda: 0)
            with patch.object(native_hooks,'session_active',return_value=False), \
                 patch.object(setup_wizard.subprocess,'Popen',return_value=process) as called:
                self.assertEqual(setup_wizard.open_codex_hook_review(root),0)
            self.assertEqual(called.call_args.args[0],['codex','resume',sid])
            self.assertEqual(store.get('codex','builder')['id'],sid)

    def test_hook_review_returns_after_provider_records_trust(self):
        with tempfile.TemporaryDirectory() as td:
            process=unittest.mock.Mock()
            process.poll.side_effect=[None,None]
            process.wait.return_value=0
            with patch.object(setup_wizard.subprocess,'Popen',return_value=process), \
                 patch.object(native_hooks,'codex_trust_status',return_value={'status':'trust_recorded'}):
                self.assertEqual(setup_wizard.open_codex_hook_review(Path(td)),0)
            process.terminate.assert_called_once()
            process.wait.assert_called_once()

    def test_hook_review_does_not_invent_approval_after_user_exits(self):
        with tempfile.TemporaryDirectory() as td:
            process=SimpleNamespace(returncode=0, poll=lambda: 0)
            with patch.object(setup_wizard.subprocess,'Popen',return_value=process), \
                 patch.object(native_hooks,'codex_trust_status',side_effect=AssertionError('no approval')):
                self.assertEqual(setup_wizard.open_codex_hook_review(Path(td)),0)

    def test_workflow_mcp_protocol_has_live_delegation_without_shell(self):
        import verification_mcp as vm
        listing=vm.handle({'method':'tools/list'})['tools']
        names={t['name'] for t in listing}
        self.assertIn('delegate_to_builder',names)
        self.assertIn('delegation_status',names)
        self.assertNotIn('execute_shell',names)
        with patch.object(vm,'_authorized_delegate',return_value=False):
            result=vm.handle({'method':'tools/call','params':{'name':'delegate_to_builder',
                              'arguments':{'goal':'Safely add a single test'}}})
        self.assertTrue(result['isError'])
        self.assertIn('blocked',result['content'][0]['text'])

    def test_idle_questions_do_not_start_workers(self):
        import inspect
        source=inspect.getsource(eng.chat_command)
        self.assertNotIn('live_builder_worker.py',source)
        self.assertNotIn('run_automated_task(',source)

    def test_live_builder_worker_has_no_fresh_lead_model_call(self):
        import inspect
        import live_builder_worker
        source=inspect.getsource(live_builder_worker.process)
        self.assertIn("'awaiting_lead_review'",source)
        self.assertNotIn('review(',source)
        self.assertNotIn('claude_structured(',source)
        self.assertNotIn('run_automated_task(',source)
        self.assertIn('provider_structured(builder',source)


class AutonomousVerificationTests(unittest.TestCase):
    def test_curl_smoke_proves_real_request(self):
        import verification_mcp as vm
        if not shutil.which('curl'): self.skipTest('curl unavailable')
        out=vm.run_http_smoke()
        self.assertEqual(out['status'],'pass',out)
        self.assertEqual(out['evidence']['http_status'],'200')
        self.assertTrue(out['evidence']['json_assertions_passed'])

    def test_api_suite_requires_existing_local_spec(self):
        import verification_mcp as vm
        self.assertEqual(vm.run_local_api_checks('../outside.json')['status'],'blocked')
        self.assertEqual(vm.run_local_api_checks('/etc/passwd')['status'],'blocked')
        self.assertEqual(vm.run_local_api_checks('README.md')['status'],'blocked')

    def test_mcp_exposes_bounded_http_tools_not_shell(self):
        import verification_mcp as vm
        names={t['name'] for t in vm.TOOLS}
        self.assertIn('run_http_smoke',names)
        self.assertIn('run_local_api_checks',names)
        self.assertNotIn('execute_shell',names)
        self.assertEqual(vm.handle({'method':'tools/call','params':{'name':'run_http_smoke','arguments':{}}})['isError'],False)

    def test_lead_preapproval_is_scoped_to_verifier(self):
        from pathlib import Path
        content=(Path(__file__).parent/'eng.py').read_text()
        self.assertIn("'--allowedTools', *lead_safe_mcp_permission_rules()",content)
        self.assertIn('claude_lead_permission_args()',content)
        approved=eng.lead_safe_mcp_permissions()
        self.assertIn('mcp__gaston_verify__git_inspect',approved)
        self.assertIn('mcp__gaston_verify__finalize_delegated_task',approved)
        self.assertIn('mcp__gaston_verify__delegate_to_builder',approved)
        self.assertNotIn('mcp__gaston_verify__git_commit',approved)
        self.assertNotIn("'--permission-mode','bypassPermissions'",content)
        self.assertNotIn("'--allowedTools','mcp__maestro__*'",content)

class LeadVisualBrowserAndAutoPermissionsTests(unittest.TestCase):
    def test_both_claude_lead_launch_paths_use_native_permission_policy(self):
        import inspect
        source1=inspect.getsource(eng.claude_structured)
        source2=inspect.getsource(eng.chat_command)
        self.assertIn('claude_lead_permission_args()', source1)
        self.assertIn('claude_lead_permission_args()', source2)
        self.assertIn("'--permission-mode', 'default'",inspect.getsource(eng.claude_lead_permission_args))

    def test_visible_browser_only_with_display_and_isolated_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); executable=root/'chrome';executable.write_text('fixture')
            with patch.object(eng,'LOCAL',root), \
                 patch.object(eng.playwright_tools,'test_workspace',return_value=root), \
                 patch.object(eng.playwright_tools,'package_installed',return_value=True), \
                 patch.object(eng.playwright_tools,'chromium_installed',return_value=True), \
                 patch.object(eng.shutil,'which',return_value='/usr/bin/node'), \
                 patch.object(eng.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=str(executable)+'\n')), \
                 patch.dict(os.environ,{'DISPLAY':':1'},clear=False):
                cfg=json.loads(eng.lead_verification_mcp_config().read_text())['mcpServers']
            self.assertIn('gaston_browser_visible',cfg)
            self.assertIn('--isolated',cfg['gaston_browser']['args'])
            self.assertIn('--headless',cfg['gaston_browser']['args'])
            self.assertIn('--isolated',cfg['gaston_browser_visible']['args'])
            self.assertNotIn('--headless',cfg['gaston_browser_visible']['args'])
            self.assertEqual(cfg['gaston_browser_visible']['env']['GSETTINGS_BACKEND'],'memory')
            with patch.object(eng,'LOCAL',root), \
                 patch.object(eng.playwright_tools,'test_workspace',return_value=root), \
                 patch.object(eng.playwright_tools,'package_installed',return_value=True), \
                 patch.object(eng.playwright_tools,'chromium_installed',return_value=True), \
                 patch.object(eng.shutil,'which',return_value='/usr/bin/node'), \
                 patch.object(eng.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=str(executable)+'\n')), \
                 patch.dict(os.environ,{'DISPLAY':'','WAYLAND_DISPLAY':''},clear=False):
                cfg=json.loads(eng.lead_verification_mcp_config().read_text())['mcpServers']
            self.assertNotIn('gaston_browser_visible',cfg)

    def test_visible_test_requires_display_and_does_not_create_fixture(self):
        import verification_mcp as vm
        with patch.dict(os.environ,{'DISPLAY':'','WAYLAND_DISPLAY':''}), \
             patch.object(vm,'playwright_folder',side_effect=AssertionError('should not run')):
            self.assertEqual(vm.run_web_smoke(visible=True)['status'],'blocked')
        self.assertEqual(vm.run_web_smoke(visible='yes')['status'],'blocked')

    def test_visible_test_changes_only_headless_option_and_cleans_up(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'browser'
            (folder/'node_modules/@playwright/test').mkdir(parents=True)
            captured={}
            def fake_execute(*a,**kw):
                config=list((Path(tmp)/'verification-scratch').glob('web-*/playwright.config.cjs'))[0]
                captured['text']=config.read_text()
                return {'exit_code':0,'stdout':'1 passed','stderr':''}
            with patch.object(vm,'LOCAL',Path(tmp)), \
                 patch.object(vm,'playwright_folder',return_value=folder), \
                 patch.object(vm.shutil,'which',return_value='/usr/bin/node'), \
                 patch.object(vm,'execute',side_effect=fake_execute), \
                 patch.dict(os.environ,{'DISPLAY':':1'},clear=False):
                self.assertEqual(vm.run_web_smoke(visible=True)['status'],'pass')
            self.assertIn('headless:false',captured['text'])
            self.assertFalse(list((Path(tmp)/'verification-scratch').iterdir()))




class EmulatorOwnershipTests(unittest.TestCase):
    def test_preexisting_emulator_never_recorded_or_stopped(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(vm,'LOCAL',Path(tmp)), \
             patch.object(vm,'sdk_tool',return_value='/fake/emulator'), \
             patch.object(vm,'avd_names',return_value=['Small_Phone']), \
             patch.object(vm,'connected_emulators',return_value=['emulator-5554']), \
             patch.object(vm.subprocess,'Popen',side_effect=AssertionError('no launch')), \
             patch.object(vm.os,'kill',side_effect=AssertionError('no stop')):
            r=vm.start_existing_emulator('Small_Phone')
            self.assertEqual(r['status'],'ready')
            self.assertFalse((Path(tmp)/'emulator-runs/owned.json').exists())
            self.assertEqual(vm.finish_mobile_verification()['status'],'ready')

    def test_new_emulator_record_has_process_identity(self):
        import verification_mcp as vm
        identity={'starttime':'12345','argv':['/fake/emulator','-avd','Small_Phone']}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(vm,'LOCAL',Path(tmp)), \
             patch.object(vm,'ROOT',Path(tmp)), \
             patch.object(vm,'sdk_tool',return_value='/fake/emulator'), \
             patch.object(vm,'avd_names',return_value=['Small_Phone']), \
             patch.object(vm,'connected_emulators',return_value=[]), \
             patch.object(vm,'_proc_identity',return_value=identity), \
             patch.object(vm.time,'sleep'), \
             patch.object(vm.subprocess,'Popen',return_value=SimpleNamespace(pid=8192,poll=lambda:None)):
            result=vm.start_existing_emulator('Small_Phone')
            self.assertEqual(result['cleanup_tracking'],'enabled')
            record=json.loads((Path(tmp)/'emulator-runs/owned.json').read_text())
            self.assertEqual(record['pid'],8192)
            self.assertEqual(record['avd'],'Small_Phone')

    def test_verified_emulator_shutdown(self):
        import verification_mcp as vm
        identity={'starttime':'11','argv':['/fake/emulator','-avd','Small_Phone']}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(vm,'LOCAL',Path(tmp)), \
                 patch.object(vm,'avd_names',return_value=['Small_Phone']), \
                 patch.object(vm,'_proc_identity',side_effect=[identity,None]), \
                 patch.object(vm.os,'kill') as kill:
                vm._write_emulator_owner({'version':1,'avd':'Small_Phone','pid':8192,'starttime':'11',
                                          'verifier_pid':os.getpid(),'verifier_starttime':'fake'})
                result=vm.finish_mobile_verification()
                self.assertEqual(result['status'],'pass',result)
                kill.assert_called_once_with(8192,vm.signal.SIGTERM)
                self.assertFalse(vm._owned_emulator_file().exists())

    def test_pid_reuse_does_not_kill_process(self):
        import verification_mcp as vm
        identity={'starttime':'DIFFERENT','argv':['/fake/emulator','-avd','Small_Phone']}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(vm,'LOCAL',Path(tmp)), \
             patch.object(vm,'_proc_identity',return_value=identity), \
             patch.object(vm.os,'kill',side_effect=AssertionError('unsafe stop')):
            vm._write_emulator_owner({'version':1,'avd':'Small_Phone','pid':8192,'starttime':'11',
                                      'verifier_pid':os.getpid(),'verifier_starttime':'fake'})
            result=vm.finish_mobile_verification()
            self.assertEqual(result['status'],'blocked')
            self.assertIn('ownership',result['reason'])

    def test_other_active_verifier_is_not_interrupted(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(vm,'LOCAL',Path(tmp)), \
             patch.object(vm,'_verifier_still_owns',return_value=True), \
             patch.object(vm.os,'kill',side_effect=AssertionError('unsafe stop')):
            vm._write_emulator_owner({'version':1,'avd':'Small_Phone','pid':8192,'starttime':'11',
                                      'verifier_pid':os.getpid()+1,'verifier_starttime':'5'})
            result=vm.finish_mobile_verification()
            self.assertEqual(result['status'],'ready')
            self.assertTrue(vm._owned_emulator_file().exists())

    def test_android_qemu_exec_still_owned_when_sdk_path_matches(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as td:
            sdk=Path(td)/'emulator';(sdk/'qemu/linux-x86_64').mkdir(parents=True)
            executable=sdk/'qemu/linux-x86_64/qemu-system-x86_64'
            executable.touch()
            identity={'starttime':'777','argv':[str(executable),'-avd','Small_Phone','-no-window']}
            original_resolve=Path.resolve
            def fake_resolve(path,*args,**kwargs):
                if str(path)=='/proc/8192/exe':return executable
                return original_resolve(path,*args,**kwargs)
            owner={'version':1,'avd':'Small_Phone','pid':8192,'starttime':'777',
                   'verifier_pid':os.getpid(),'verifier_starttime':'1'}
            with patch.object(vm,'LOCAL',Path(td)), \
                 patch.object(vm,'sdk_tool',return_value=str(sdk/'emulator')), \
                 patch.object(vm,'_proc_identity',side_effect=[identity,None]), \
                 patch.object(Path,'resolve',fake_resolve), \
                 patch.object(vm.os,'kill') as kill:
                vm._write_emulator_owner(owner)
                result=vm.finish_mobile_verification()
                self.assertEqual(result['status'],'pass',result)
                kill.assert_called_once_with(8192,vm.signal.SIGTERM)

    def test_unrelated_qemu_process_not_owned(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as td:
            owner={'version':1,'avd':'Small_Phone','pid':8192,'starttime':'777',
                   'verifier_pid':os.getpid(),'verifier_starttime':'1'}
            identity={'starttime':'777','argv':['/usr/bin/qemu-system-x86_64','-avd','Small_Phone']}
            with patch.object(vm,'LOCAL',Path(td)), \
                 patch.object(vm,'sdk_tool',return_value='/opt/sdk/emulator/emulator'), \
                 patch.object(vm,'_proc_identity',return_value=identity), \
                 patch.object(vm.os,'kill',side_effect=AssertionError('unsafe stop')):
                vm._write_emulator_owner(owner)
                result=vm.finish_mobile_verification()
                self.assertEqual(result['status'],'blocked',result)
                self.assertTrue(vm._owned_emulator_file().exists())

    def test_cleanup_runs_even_after_failed_maestro_validation(self):
        import verification_mcp as vm
        with patch.object(vm,'connected_emulators',return_value=[]), \
             patch.object(vm,'finish_mobile_verification',return_value={'status':'pass'}) as cleanup:
            result=vm.run_maestro_tests()
            self.assertEqual(result['status'],'blocked')
            cleanup.assert_called_once()

    def test_cleanup_after_maestro_success_and_failure(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.maestro').mkdir();(root/'.maestro'/'demo.yaml').write_text('appId: com.android.settings\n---\n- launchApp\n')
            with patch.object(vm,'ROOT',root), \
                 patch.object(vm,'connected_emulators',return_value=['emulator-5554']), \
                 patch.object(vm.shutil,'which',return_value='/fake/maestro'), \
                 patch.object(vm,'finish_mobile_verification',return_value={'status':'pass'}) as cleanup:
                for code in (0,1):
                    with patch.object(vm,'execute',return_value={'exit_code':code,'stdout':'','stderr':''}):
                        result=vm.run_maestro_tests()
                    self.assertEqual(result['status'],'pass' if code==0 else 'fail')
                self.assertEqual(cleanup.call_count,2)

    def test_restart_recovers_abandoned_owned_emulator_only(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(vm,'LOCAL',Path(tmp)), \
             patch.object(vm,'_verifier_still_owns',return_value=False), \
             patch.object(vm,'finish_mobile_verification',return_value={'status':'pass'}) as cleanup:
            vm._write_emulator_owner({'version':1,'avd':'Small_Phone','pid':8192,'starttime':'11',
                                      'verifier_pid':999999,'verifier_starttime':'6'})
            result=vm._recover_abandoned_emulator()
            self.assertEqual(result['status'],'pass')
            cleanup.assert_called_once_with(abandoned=True)

    def test_http_smoke_returns_auditable_local_assertions(self):
        import verification_mcp as vm
        if not shutil.which('curl'):self.skipTest('curl missing')
        result=vm.run_http_smoke()
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['evidence']['assertions'],{
            'curl_exit_zero':True,'http_status_200':True,'json_equals_expected':True})
        self.assertEqual(json.loads(result['evidence']['response_body']),
                         {'ok':True,'service':'gaston-smoke'})

    def test_completion_is_private_mcp_not_extra_cli_command(self):
        import verification_mcp as vm
        self.assertIn('finish_mobile_verification',{t['name'] for t in vm.TOOLS})
        self.assertIn('finish_mobile_verification',vm.CALLS)
        self.assertNotIn('run_shell',vm.CALLS)


class BackendVerifierRegressionTests(unittest.TestCase):
    def test_real_writable_cache_and_selected_test(self):
        import backend_runner
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'service';project.mkdir()
            (project/'pom.xml').write_text('<project/>')
            wrapper=project/'mvnw'
            wrapper.write_text('#!/bin/sh\n'
                               'echo "HOME=$MAVEN_USER_HOME"\n'
                               'echo "ARGS=$*"\n'
                               'exit 0\n')
            wrapper.chmod(0o755)
            with patch.object(backend_runner.shutil,'which',return_value=None):
                result=backend_runner.run(root,'service',test_filter='SlugHelperTest',offline=True)
            self.assertEqual(result['status'],'pass',result)
            self.assertIn('-Dtest=SlugHelperTest',result['evidence']['stdout_tail'])
            self.assertIn('-Dmaven.repo.local=',result['evidence']['stdout_tail'])
            self.assertTrue((root/'.agentic/local/backend-dependencies/maven-repository').is_dir())
            self.assertNotIn(str(Path.home()/'.m2'),result['evidence']['stdout_tail'])

    def test_explicit_java_25_selects_installed_jdk_without_global_change(self):
        import backend_runner
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'service';project.mkdir()
            (project/'pom.xml').write_text('<project><properties><java.version>25</java.version></properties></project>')
            wrapper=project/'mvnw'
            wrapper.write_text('#!/bin/sh\nprintf "JAVA_HOME=%s\\n" "$JAVA_HOME"\nexit 0\n')
            wrapper.chmod(0o755)
            jdks=[{'home':'/jdk/21','version':21},{'home':'/jdk/25','version':25}]
            with patch.object(backend_runner,'installed_jdks',return_value=jdks):
                result=backend_runner.run(root,'service',test_filter='SlugHelperTest')
            self.assertEqual(result['status'],'pass',result)
            self.assertEqual(result['required_java'],25)
            self.assertEqual(result['selected_jdk']['version'],25)
            self.assertIn('JAVA_HOME=/jdk/25',result['evidence']['stdout_tail'])

    def test_missing_compatible_jdk_blocks_without_running_maven(self):
        import backend_runner
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'service';project.mkdir()
            (project/'pom.xml').write_text('<project><properties><maven.compiler.release>25</maven.compiler.release></properties></project>')
            wrapper=project/'mvnw';wrapper.write_text('#!/bin/sh\nexit 99\n');wrapper.chmod(0o755)
            with patch.object(backend_runner,'installed_jdks',return_value=[{'home':'/jdk/21','version':21}]):
                result=backend_runner.run(root,'service')
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(result['required_java'],25)
            self.assertNotIn('exit_code',result)

    def test_inherited_java_target_retry_with_existing_jdk(self):
        import backend_runner
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'service';project.mkdir()
            (project/'pom.xml').write_text('<project/>')
            wrapper=project/'mvnw'
            wrapper.write_text('#!/bin/sh\nif [ "$JAVA_HOME" != "/jdk/25" ]; then echo "error: release version 25 not supported" >&2; exit 1; fi\necho "Tests run: 2, Failures: 0"\nexit 0\n')
            wrapper.chmod(0o755)
            jdks=[{'home':'/jdk/21','version':21},{'home':'/jdk/25','version':25}]
            with patch.object(backend_runner,'installed_jdks',return_value=jdks), \
                 patch.dict(os.environ,{'JAVA_HOME':'/jdk/21'}):
                result=backend_runner.run(root,'service')
            self.assertEqual(result['status'],'pass',result)
            self.assertEqual(result['selected_jdk']['version'],25)

    def test_missing_snapshot_is_blocked_not_test_failure(self):
        import backend_runner
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'backend';project.mkdir()
            (project/'pom.xml').write_text('<project/>')
            wrapper=project/'mvnw'
            wrapper.write_text('#!/bin/sh\n'
                               'echo "Non-resolvable parent POM: Unknown host repo.spring.io" >&2\n'
                               'exit 1\n')
            wrapper.chmod(0o755)
            result=backend_runner.run(root,'backend',goal='verify')
            self.assertEqual(result['status'],'blocked',result)
            self.assertIn('Dependency resolution',result['reason'])
            self.assertEqual(result['exit_code'],1)

    def test_maven_real_test_failure_is_not_blocked(self):
        import backend_runner
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'backend';project.mkdir()
            (project/'pom.xml').write_text('<project/>')
            wrapper=project/'mvnw'
            wrapper.write_text('#!/bin/sh\necho "Tests run: 1, Failures: 1" >&2\nexit 1\n')
            wrapper.chmod(0o755)
            result=backend_runner.run(root,'backend')
            self.assertEqual(result['status'],'fail',result)

    def test_reject_invalid_projects_and_selectors(self):
        import backend_runner
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'backend';project.mkdir()
            (project/'pom.xml').write_text('<project/>')
            self.assertEqual(backend_runner.run(root,'../outside')['status'],'blocked')
            self.assertEqual(backend_runner.run(root,'backend',test_filter='Thing;rm -rf /')['status'],'blocked')
            self.assertEqual(backend_runner.run(root,'backend',goal='deploy')['status'],'blocked')

    def test_lead_mcp_backend_is_available_without_shell(self):
        import verification_mcp as vm
        self.assertIn('run_backend_tests',{t['name'] for t in vm.TOOLS})
        self.assertIn('run_backend_tests',vm.CALLS)
        with patch.object(vm,'ROOT',Path('/tmp/empty-root-which-does-not-exist')):
            result=vm.run_backend_tests()
        self.assertEqual(result['status'],'blocked')

    def test_restricted_lead_executes_named_backend_test(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'service';project.mkdir()
            (project/'pom.xml').write_text('<project/>')
            wrapper=project/'mvnw';wrapper.write_text('#!/bin/sh\necho "Tests run: 2, Failures: 0"\nexit 0\n')
            wrapper.chmod(0o755)
            with patch.object(vm,'ROOT',root):
                outcome=vm.handle({'method':'tools/call','params':{
                    'name':'run_backend_tests','arguments':{'project':'service','goal':'test',
                                                          'test_filter':'SlugHelperTest','offline':True}}})
            self.assertFalse(outcome['isError'],outcome)
            payload=json.loads(outcome['content'][0]['text'])
            self.assertEqual(payload['status'],'pass')
            self.assertNotIn('stdout_tail',payload['evidence'])
            self.assertIn('Tests run: 2',(root/payload['log_path']).read_text())

    def test_backend_gate_is_implemented_as_shared_runner(self):
        import eng
        code=Path(eng.__file__).read_text()
        self.assertIn('backend_runner.run(ROOT',code)
        code=Path(__file__).parent.joinpath('live_builder_worker.py').read_text()
        self.assertNotIn("eng.verify(task,rd,cfg)",code)
        self.assertNotIn("eng.provider_structured(builder,repair_prompt",code)

class ClaudeNativeHistoryTests(unittest.TestCase):
    def test_names_are_readable_distinct_and_safe(self):
        import uuid
        one=str(uuid.uuid4()); two=str(uuid.uuid4())
        lead=eng.claude_session_display_name('reviewer',one,Path('/work/sample-project'))
        builder=eng.claude_session_display_name('builder',two,Path('/work/sample-project'))
        self.assertEqual(lead,f'Gaston Lead - sample-project - {one[:8]}')
        self.assertEqual(builder,f'Gaston Builder - sample-project - {two[:8]}')
        self.assertNotEqual(lead,builder)
        self.assertNotIn('/',eng.claude_session_display_name('reviewer',one,Path('/work/My App')))

    def test_new_session_is_named_and_existing_session_is_resumed_unmodified(self):
        import inspect
        source=inspect.getsource(eng.chat_command)
        self.assertIn("cmd=['claude','--resume',existing['id']]",source)
        self.assertIn("cmd=['claude','--session-id',sid,",source)
        self.assertIn("'--name',claude_session_display_name(role,sid)",source)
        self.assertNotIn("'--name',claude_session_display_name(role,existing",source)
        self.assertNotIn("claude','-p'",source)

class SafeDirtyTestContinuation(unittest.TestCase):
    def test_untracked_java_tests_allowed_but_other_changes_blocked(self):
        import subprocess
        import workspace_git
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            repo=root/'backend';repo.mkdir()
            subprocess.run(['git','init','-q'],cwd=repo,check=True)
            subprocess.run(['git','config','user.name','Test'],cwd=repo,check=True)
            subprocess.run(['git','config','user.email','test@example.com'],cwd=repo,check=True)
            (repo/'README.md').write_text('baseline')
            subprocess.run(['git','add','README.md'],cwd=repo,check=True)
            subprocess.run(['git','commit','-q','-m','initial'],cwd=repo,check=True)
            test_file=repo/'src/test/java/org/example/FixtureTest.java'
            test_file.parent.mkdir(parents=True)
            test_file.write_text('class FixtureTest {}')
            baseline=workspace_git.safe_test_only_baseline(root)
            self.assertIsNotNone(baseline)
            self.assertEqual(len(baseline),1)
            self.assertTrue(workspace_git.baseline_unchanged(root,baseline))
            test_file.write_text('class FixtureTest { int x; }')
            self.assertFalse(workspace_git.baseline_unchanged(root,baseline))
            test_file.write_text('class FixtureTest {}')
            production=repo/'src/main/java/org/example/Controller.java'
            production.parent.mkdir(parents=True)
            production.write_text('class Controller {}')
            self.assertIsNone(workspace_git.safe_test_only_baseline(root))
            production.unlink()
            (repo/'README.md').write_text('changed')
            self.assertIsNone(workspace_git.safe_test_only_baseline(root))

    def test_live_worker_continues_with_any_existing_changes(self):
        source=Path(__file__).with_name('live_builder_worker.py').read_text()
        self.assertIn('existing_changes',source)
        self.assertIn('changed_existing_files',source)
        self.assertIn('preexisting-changes.json',source)
        self.assertIn('existing-change-overlaps.json',source)
        self.assertNotIn('safe_test_only_baseline',source)
        self.assertNotIn('Review or stash them',source)
        self.assertNotIn('baseline_unchanged',source)

    def test_dirty_tracked_and_untracked_production_files_do_not_block(self):
        import subprocess
        import workspace_git
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repo=root/'backend';repo.mkdir()
            subprocess.run(['git','init','-q'],cwd=repo,check=True)
            subprocess.run(['git','config','user.name','Test'],cwd=repo,check=True)
            subprocess.run(['git','config','user.email','test@example.com'],cwd=repo,check=True)
            tracked=repo/'README.md';tracked.write_text('baseline')
            subprocess.run(['git','add','README.md'],cwd=repo,check=True)
            subprocess.run(['git','commit','-q','-m','initial'],cwd=repo,check=True)
            tracked.write_text('user edit')
            untracked=repo/'src/main/java/org/example/Health.java'
            untracked.parent.mkdir(parents=True)
            untracked.write_text('class Health {}')
            baseline=workspace_git.existing_changes(root)
            self.assertEqual(len(baseline),2)
            self.assertEqual(workspace_git.changed_existing_files(root,baseline),[])
            untracked.write_text('class Health { int x; }')
            self.assertEqual(workspace_git.changed_existing_files(root,baseline),
                             ['backend/src/main/java/org/example/Health.java'])
            self.assertEqual(tracked.read_text(),'user edit')
            self.assertFalse((repo/'.git/index.lock').exists())
            self.assertEqual(subprocess.run(['git','status','--porcelain'],cwd=repo,
                                           capture_output=True,text=True).stdout.count('??'),1)

    def test_existing_deleted_files_and_unchanged_files_are_tracked(self):
        import subprocess
        import workspace_git
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repo=root/'backend';repo.mkdir()
            subprocess.run(['git','init','-q'],cwd=repo,check=True)
            subprocess.run(['git','config','user.name','Test'],cwd=repo,check=True)
            subprocess.run(['git','config','user.email','test@example.com'],cwd=repo,check=True)
            tracked=repo/'README.md';tracked.write_text('baseline')
            subprocess.run(['git','add','README.md'],cwd=repo,check=True)
            subprocess.run(['git','commit','-q','-m','initial'],cwd=repo,check=True)
            tracked.unlink()
            baseline=workspace_git.existing_changes(root)
            self.assertEqual(baseline,{'backend/README.md':'deleted'})
            tracked.write_text('restored by agent')
            self.assertEqual(workspace_git.changed_existing_files(root,baseline),['backend/README.md'])

    def test_no_automatic_git_mutations_in_workflow(self):
        eng_source=Path(__file__).with_name('eng.py').read_text()
        worker=Path(__file__).with_name('live_builder_worker.py').read_text()
        self.assertNotIn('Commit/stash, use a worktree',eng_source)
        self.assertNotIn('Unrecognized working tree changes',eng_source)
        self.assertIn('Never stage, commit, stash',eng_source)
        self.assertIn('Do not stage, commit, stash',worker)


class V23WorkflowTests(unittest.TestCase):
    def _task(self,root,task='feature-123'):
        folder=root/'.agentic/runs'/task
        folder.mkdir(parents=True)
        (folder/'task.json').write_text('{}')
        (folder/'state.json').write_text('{"stage":"checks_failed"}')
        (folder/'build.json').write_text('{"status":"done","questions":[]}')
        return folder

    def test_lead_verification_binds_real_success_to_task(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=self._task(root)
            # A pure helper change can be accepted using focused backend evidence;
            # a controller change now also requires managed real HTTP evidence.
            source=root/'backend/src/main/java/example/util/SlugHelper.java';source.parent.mkdir(parents=True);source.write_text('class SlugHelper {}')
            (root/'backend/pom.xml').write_text('<project/>')
            (folder/'build.json').write_text(json.dumps({'status':'done','changed_files':[str(source.relative_to(root))]}))
            evidence={'status':'pass','exit_code':0,'evidence':{'stdout_tail':'Tests run: 1, Failures: 0, Errors: 0, Skipped: 0'}}
            with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True),patch.object(vm,'run_backend_tests',return_value=evidence),patch.object(eng,'ROOT',root),patch.object(eng,'load_config',return_value={'project':{},'checks':[]}):
                result=vm.verify_delegated_backend_task('feature-123','backend','SlugHelperTest',True,'Reviewed pure helper and assertions')
                report=vm.delegation_status('feature-123')
            self.assertEqual(result['status'],'pass')
            self.assertEqual(report['stage'],'ready_for_human_release_review')
            self.assertFalse(report['lead_must_review'])
            self.assertTrue(report['all_passed'])
            self.assertTrue((folder/'lead-backend-verification.json').exists())
            self.assertEqual(json.loads((folder/'state.json').read_text())['lead_verified'],True)

    def test_lead_verification_does_not_forge_pass(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=self._task(root)
            with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True),patch.object(vm,'run_backend_tests',return_value={'status':'blocked','reason':'no JDK'}):
                self.assertEqual(vm.verify_delegated_backend_task('feature-123','backend','HealthControllerTest',True,'Independently reviewed all files')['status'],'blocked')
            self.assertEqual(json.loads((folder/'state.json').read_text())['stage'],'awaiting_acceptance')
            self.assertEqual(json.loads((folder/'lead-backend-verification.json').read_text())['outcome']['status'],'blocked')
            with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True),patch.object(vm,'run_backend_tests',return_value={'status':'pass','exit_code':0,'evidence':{'stdout_tail':'BUILD SUCCESS'}}):
                self.assertEqual(vm.verify_delegated_backend_task('feature-123','backend','HealthControllerTest',True,'Independently reviewed all files')['status'],'blocked')
            self.assertNotEqual(json.loads((folder/'state.json').read_text())['stage'],'ready_for_human_release_review')

    def test_lead_verification_requires_review_and_completed_builder(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=self._task(root)
            with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True),patch.object(vm,'run_backend_tests',side_effect=AssertionError('Must not run')):
                self.assertEqual(vm.verify_delegated_backend_task('feature-123','backend','Test',False,'')['status'],'blocked')
                (folder/'build.json').write_text('{"status":"blocked"}')
                self.assertEqual(vm.verify_delegated_backend_task('feature-123','backend','Test',True,'Reviewed source and diff')['status'],'blocked')

    def test_blocked_builder_can_be_recovered_by_real_lead_test(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=self._task(root)
            (folder/'build.json').write_text('{"status":"blocked","questions":["No network"]}')
            source=root/'sample-backend/src/main/java/example/HealthController.java'
            source.parent.mkdir(parents=True);source.write_text('class HealthController {}')
            (root/'sample-backend/pom.xml').write_text('<project/>')
            with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True),patch.object(vm,'run_backend_tests',return_value={'status':'pass','exit_code':0,'evidence':{'stdout_tail':'Tests run: 1, Failures: 0'}}),patch.object(eng,'ROOT',root),patch.object(eng,'load_config',return_value={'project':{},'checks':[]}):
                result=vm.verify_delegated_backend_task('feature-123','sample-backend','HealthControllerTest',True,'Reviewed controller and test contract',reviewed_files=[str(source.relative_to(root))])
            self.assertEqual(result['status'],'blocked',result)
            self.assertEqual(result['backend_test_status'],'pass')
            self.assertIn('api_integration',result['acceptance']['unverified_planned_gates'])
            self.assertEqual(json.loads((folder/'state.json').read_text())['stage'],'awaiting_acceptance')

    def test_uninstall_session_exact_identity_and_other_projects_preserved(self):
        import uninstall
        import uuid
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'workspace';root.mkdir();home=base/'home';home.mkdir()
            cid=str(uuid.uuid4());bid=str(uuid.uuid4());other=str(uuid.uuid4())
            loc=root/'.agentic/local';loc.mkdir(parents=True)
            (loc/'sessions.json').write_text(json.dumps({'root':str(root),'sessions':{'claude':{'reviewer':{'id':cid}},'codex':{'builder':{'id':bid}}},'archive':[]}))
            cdir=home/'.claude/projects'/re.sub(r'[^A-Za-z0-9]','-',str(root));cdir.mkdir(parents=True)
            claimed=cdir/(cid+'.jsonl');claimed.write_text(json.dumps({'cwd':str(root),'sessionId':cid})+'\n')
            unrelated=cdir/(other+'.jsonl');unrelated.write_text(json.dumps({'cwd':str(root),'sessionId':other})+'\n')
            cdir2=home/'.codex/sessions/2026/11/16';cdir2.mkdir(parents=True)
            owned=cdir2/('rollout-2026-11-16T01-00-00-'+bid+'.jsonl')
            owned.write_text(json.dumps({'type':'session_meta','payload':{'id':bid,'cwd':str(root)}})+'\n')
            imposter=cdir2/('rollout-2026-11-16T01-00-00-'+other+'.jsonl')
            imposter.write_text(json.dumps({'type':'session_meta','payload':{'id':other,'cwd':str(root)}})+'\n')
            with patch.dict(os.environ,{'CODEX_HOME':str(home/'.codex'),'CLAUDE_CONFIG_DIR':str(home/'.claude')}):
                results,notes=uninstall._provider_session_files(root,home)
            self.assertEqual(set(results),{claimed,owned});self.assertFalse(notes)
            self.assertNotIn(unrelated,results);self.assertNotIn(imposter,results)
            # Even a registered UUID cannot authorize a different cwd.
            owned.write_text(json.dumps({'type':'session_meta','payload':{'id':bid,'cwd':'/different'}})+'\n')
            with patch.dict(os.environ,{'CODEX_HOME':str(home/'.codex'),'CLAUDE_CONFIG_DIR':str(home/'.claude')}):
                results,notes=uninstall._provider_session_files(root,home)
            self.assertNotIn(owned,results);self.assertTrue(notes)

    def test_full_uninstall_removes_verified_sessions_without_touching_others(self):
        import uninstall
        import hashlib,uuid
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'project';root.mkdir();home=base/'home';home.mkdir()
            loc=root/'.agentic/local';loc.mkdir(parents=True)
            sid=str(uuid.uuid4()); other=str(uuid.uuid4())
            (loc/'sessions.json').write_text(json.dumps({'root':str(root),'sessions':{'claude':{'reviewer':{'id':sid}}}}))
            cdir=home/'.claude/projects'/re.sub(r'[^A-Za-z0-9]','-',str(root));cdir.mkdir(parents=True)
            owned=cdir/(sid+'.jsonl');owned.write_text(json.dumps({'sessionId':sid,'cwd':str(root)})+'\n')
            extra=cdir/(other+'.jsonl');extra.write_text(json.dumps({'sessionId':other,'cwd':str(root)})+'\n')
            readme=root/'README.md';readme.write_text('owned')
            (root/'.agentic/manifest.json').write_text(json.dumps({'files':{'README.md':hashlib.sha256(b'owned').hexdigest()}}))
            (root/'sample-backend').mkdir();(root/'sample-backend/pom.xml').write_text('<project/>')
            with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(home/'.claude'),'CODEX_HOME':str(home/'.codex')}):
                logs=[];result=uninstall.uninstall(root,input_fn=lambda prompt:('erase' if prompt.startswith('Mode ') else 'ERASE GASTON DATA' if prompt.startswith('Type ERASE ') else 'UNINSTALL'),output=logs.append)
            self.assertTrue(result)
            self.assertFalse(owned.exists());self.assertTrue(extra.exists())
            self.assertFalse(loc.exists());self.assertTrue((root/'sample-backend/pom.xml').exists())

class V24LeadOwnsOutcomeTests(unittest.TestCase):
    def test_builder_no_automatic_tests_and_repair(self):
        import inspect,live_builder_worker
        code=inspect.getsource(live_builder_worker.process)
        self.assertNotIn('checks=eng.verify(',code)
        self.assertNotIn("eng.save_state(rd,'repairing'",code)
        self.assertEqual(code.count('eng.provider_structured('),1)
        prompt=eng.build_prompt({'goal':'Add version endpoint'}, {'steps':[]})
        self.assertIn('Do not run test or acceptance commands',prompt)
        self.assertIn('The Engineering Lead owns',prompt)
        self.assertIn('Never stage, commit, stash',prompt)

    def test_builder_stops_at_lead_review_without_verification(self):
        import live_builder_worker
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            folder=root/'.agentic/runs/test-task';folder.mkdir(parents=True)
            (folder/'task.json').write_text('{}')
            states=[];calls=[]
            def save(rd,stage,**kw):states.append(stage)
            def impl(provider,prompt,schema,path,cfg,role):
                calls.append((provider,prompt,path))
                return {'status':'done','questions':[],'changed_files':['backend/Test.java'],'remaining_risks':[]}
            with patch.object(eng,'ROOT',root),patch.object(eng,'require_setup_complete'), \
                 patch.object(eng,'load_config',return_value={'preferences':{'builder':'codex','reviewer':'claude'}}), \
                 patch.object(eng,'SESSIONS',SimpleNamespace(get=lambda *a:{'id':'lead'})), \
                 patch.object(eng,'load_task',return_value={'goal':'Add a basic endpoint'}), \
                 patch.object(eng,'run_dir',return_value=folder), \
                 patch.object(eng,'save_state',side_effect=save), \
                 patch.object(eng,'provider_structured',side_effect=impl), \
                 patch.object(eng.workspace_git,'existing_changes',return_value={}), \
                 patch.object(eng.workspace_git,'changed_existing_files',return_value=[]), \
                 patch.object(eng,'write_summary'):
                live_builder_worker.process('test-task')
            self.assertEqual(states,['implementing','awaiting_lead_review'])
            self.assertEqual(len(calls),1)
            self.assertIn('Do not run test or acceptance commands',calls[0][1])

    def test_lead_can_request_correction_same_task(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'.agentic/runs/demo-task';folder.mkdir(parents=True)
            (folder/'task.json').write_text('{}')
            (folder/'build.json').write_text('{"status":"done"}')
            (folder/'state.json').write_text('{"stage":"awaiting_lead_review"}')
            with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True), \
                 patch.object(eng,'save_state'),patch.object(eng,'write_json',side_effect=lambda p,d:p.write_text(json.dumps(d))), \
                 patch.object(vm.subprocess,'Popen',return_value=SimpleNamespace(pid=1234)) as launch:
                result=vm.request_builder_revision('demo-task','Correct the wrong JSON status field')
                again=vm.request_builder_revision('demo-task','Correct the service name string now')
            self.assertEqual(result['status'],'queued')
            self.assertEqual(result['revision'],1)
            self.assertEqual(again['revision'],2)
            self.assertEqual(launch.call_count,2)
            self.assertNotIn('GASTON_LEAD_TOKEN',launch.call_args.kwargs['env'])
            self.assertEqual(json.loads((folder/'revision-request.json').read_text())['number'],2)
            self.assertIn('live_builder_worker.py',str(launch.call_args.args[0]))

    def test_correction_cannot_interrupt_worker_or_verified_task(self):
        import verification_mcp as vm
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'.agentic/runs/demo-task';folder.mkdir(parents=True)
            (folder/'task.json').write_text('{}')
            (folder/'build.json').write_text('{"status":"done"}')
            with patch.object(vm,'ROOT',root),patch.object(vm,'_authorized_delegate',return_value=True), \
                 patch.object(vm.subprocess,'Popen',side_effect=AssertionError('must not launch')):
                (folder/'state.json').write_text('{"stage":"implementing"}')
                self.assertEqual(vm.request_builder_revision('demo-task','Correct the JSON response field')['status'],'blocked')
                (folder/'state.json').write_text('{"stage":"ready_for_human_release_review","lead_verified":true}')
                self.assertEqual(vm.request_builder_revision('demo-task','Correct the JSON response field')['status'],'blocked')

    def test_tool_registered_for_lead_and_no_git_mutations(self):
        import verification_mcp as vm
        self.assertIn('request_builder_revision',vm.CALLS)
        self.assertIn('request_builder_revision',[t['name'] for t in vm.TOOLS])
        source=Path(vm.__file__).read_text()
        self.assertNotIn('git add .',source)

if __name__=='__main__':
    unittest.main(verbosity=2)
