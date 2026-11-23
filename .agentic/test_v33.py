"""Offline regression checks for provider-independent Builder capabilities."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import eng
import native_hooks
import verification_mcp as vm

SID='00000000-0000-4000-8000-000000000001'


class BuilderAutonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        # Offline compatibility tests must not depend on a developer's PATH.
        discovery=patch.object(eng.serena_tools,'executable',return_value=None)
        discovery.start();self.addCleanup(discovery.stop)
        local=patch.object(eng,'LOCAL',self.root/'.agentic/local')
        local.start();self.addCleanup(local.stop)
        self.session=SimpleNamespace(locked=lambda *args:contextlib.nullcontext(),
                                     get=lambda *args:{'id':SID},mark=lambda *args,**kw:None)

    def test_claude_structured_native_tools_both_modes_and_resume(self):
        for role,mode in [('build','acceptEdits'),('plan','plan')]:
            with self.subTest(role=role):
                cfg={'models':{'claude_builder':'claude-sonnet-5-5'},'reasoning':{'claude_builder':'low'}}
                with patch.object(eng,'SESSIONS',self.session),patch.object(eng,'recover_claude_session',return_value={'id':SID}),\
                     patch.object(eng,'load_config',return_value=cfg),patch.object(eng,'claude_prompt_refresh_args',return_value=['--system-prompt-snapshot','off']),\
                     patch.dict(os.environ,{'GASTON_LEAD_TOKEN':'private','USER_PLUGIN_SETTING':'retained'}),\
                     patch.object(eng,'run_process',return_value=subprocess.CompletedProcess([],0,stdout='{"structured_output":{"status":"done"}}')) as run:
                    eng.provider_structured('claude','implement',{'type':'object'},self.root/'build.json',cfg,role)
                args=run.call_args.args[0];env=run.call_args.kwargs['env']
                self.assertEqual(args[args.index('--permission-mode')+1],mode)
                self.assertEqual(args[args.index('--resume')+1],SID)
                self.assertIn('--system-prompt-snapshot',args)
                self.assertNotIn('--tools',args)
                self.assertNotIn('--strict-mcp-config',args)
                self.assertNotIn('--mcp-config',args)
                self.assertEqual(args[args.index('--disallowedTools')+1],'mcp__gaston_verify__*')
                self.assertNotIn('mcp__maestro__*',args);self.assertNotIn('mcp__playwright__*',args)
                self.assertIn('connected MCP servers',args[args.index('--append-system-prompt')+1])
                self.assertNotIn('GASTON_LEAD_TOKEN',env)
                self.assertEqual(env['USER_PLUGIN_SETTING'],'retained')
                self.assertEqual(env['CLAUDE_CODE_DISABLE_ADVISOR_TOOL'],'1')

    def test_codex_structured_retains_native_configuration_and_uses_role_brief(self):
        for role,sandbox in [('build','workspace-write'),('plan','read-only')]:
            with self.subTest(role=role):
                out=self.root/f'{role}.json'
                def execute(args,path,**kwargs):
                    out.write_text('{"status":"done"}')
                    return subprocess.CompletedProcess(args,0,stdout=json.dumps({'type':'thread.started','thread_id':SID}))
                with patch.object(eng,'SESSIONS',self.session),patch.object(eng,'command_exists',return_value=True),\
                     patch.object(eng,'codex_effort_args',return_value=['-c','model_reasoning_effort="low"']),\
                     patch.object(eng.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='true')),\
                     patch.dict(os.environ,{'GASTON_LEAD_TOKEN':'private','USER_PLUGIN_SETTING':'retained'}),\
                     patch.object(eng,'run_process',side_effect=execute) as run:
                    eng.codex_structured('implement',{'type':'object'},out,'gpt-6-astra',sandbox,role=role)
                args=run.call_args.args[0];kw=run.call_args.kwargs
                self.assertEqual(args[args.index('--sandbox')+1],sandbox)
                self.assertIn('approval_policy="never"',args)
                self.assertEqual(args[args.index('resume')+1],SID)
                self.assertNotIn('--ignore-user-config',args)
                self.assertFalse(any('mcp_servers.gaston_verify' in a for a in args))
                self.assertIn('Do not run tests',kw['stdin'])
                self.assertNotIn('GASTON_LEAD_TOKEN',kw['env'])
                self.assertEqual(kw['env']['USER_PLUGIN_SETTING'],'retained')

    def test_claude_interactive_builder_restores_discovery(self):
        cfg={'preferences':{'builder':'claude','reviewer':'claude'},'models':{'claude_builder':'claude-sonnet-5-5'},'reasoning':{'claude_builder':'low'}}
        with patch.object(eng,'SESSIONS',self.session),patch.object(eng,'recover_claude_session',return_value={'id':SID}),\
             patch.object(eng,'load_config',return_value=cfg),patch.object(eng,'command_exists',return_value=True),\
             patch.object(eng,'ensure_local_context'),patch.object(eng,'native_hooks_installed',return_value=False),\
             patch.object(eng,'claude_prompt_refresh_args',return_value=['--system-prompt-snapshot','off']),\
             patch.object(eng,'run_interactive_provider',return_value=0) as call:
            eng.chat_command('claude',handoff_after=False,role='builder')
        args=call.call_args.args[0]
        self.assertNotIn('--tools',args);self.assertNotIn('--mcp-config',args)
        self.assertNotIn('mcp__maestro__*',args);self.assertNotIn('mcp__playwright__*',args)
        self.assertIn('connected MCP servers',args[args.index('--append-system-prompt')+1])

    def assert_serena_config(self,args):
        self.assertIn('--mcp-config',args)
        self.assertNotIn('--strict-mcp-config',args)
        self.assertNotIn('--tools',args)
        config=Path(args[args.index('--mcp-config')+1])
        self.assertEqual(config.parent,self.root/'.agentic/local')
        servers=json.loads(config.read_text())['mcpServers']
        self.assertEqual(set(servers),{'gaston_serena'})
        self.assertEqual(servers['gaston_serena']['args'][-2:],['claude','/test/serena'])
        allowed={a for a in args if a.startswith('mcp__gaston_serena__')}
        self.assertEqual(allowed,{'mcp__gaston_serena__'+tool for tool in eng.serena_tools.SEMANTIC_TOOLS})
        self.assertEqual(args[args.index('--disallowedTools')+1],'mcp__gaston_verify__*')

    def test_claude_structured_with_serena_keeps_native_and_lead_boundaries(self):
        for role,mode in [('build','acceptEdits'),('plan','plan')]:
            with self.subTest(role=role), patch.object(eng.serena_tools,'executable',return_value='/test/serena'),\
                 patch.object(eng,'SESSIONS',self.session),patch.object(eng,'recover_claude_session',return_value={'id':SID}),\
                 patch.object(eng,'claude_prompt_refresh_args',return_value=[]),\
                 patch.object(eng,'run_process',return_value=subprocess.CompletedProcess([],0,stdout='{"structured_output":{"status":"done"}}')) as run:
                cfg={'models':{'claude_builder':'claude-sonnet-5-5'},'reasoning':{'claude_builder':'low'}}
                with patch.object(eng,'load_config',return_value=cfg):
                    eng.provider_structured('claude','implement',{'type':'object'},self.root/'build.json',cfg,role)
                args=run.call_args.args[0]
                self.assertEqual(args[args.index('--permission-mode')+1],mode)
                self.assert_serena_config(args)

    def test_claude_interactive_with_serena_keeps_native_and_lead_boundaries(self):
        cfg={'preferences':{'builder':'claude','reviewer':'claude'},'models':{'claude_builder':'claude-sonnet-5-5'},'reasoning':{'claude_builder':'low'}}
        with patch.object(eng.serena_tools,'executable',return_value='/test/serena'),\
             patch.object(eng,'SESSIONS',self.session),patch.object(eng,'recover_claude_session',return_value={'id':SID}),\
             patch.object(eng,'load_config',return_value=cfg),patch.object(eng,'command_exists',return_value=True),\
             patch.object(eng,'ensure_local_context'),patch.object(eng,'native_hooks_installed',return_value=False),\
             patch.object(eng,'claude_prompt_refresh_args',return_value=[]),\
             patch.object(eng,'run_interactive_provider',return_value=0) as call:
            eng.chat_command('claude',handoff_after=False,role='builder')
        self.assert_serena_config(call.call_args.args[0])

    def test_builder_hook_does_not_filter_native_plugins_or_edits(self):
        for provider in ('codex','claude'):
            for tool in ('mcp__claude_ai_Miro__board_show','WebFetch','Skill','Bash','Edit','apply_patch'):
                with patch.object(native_hooks,'registered_role',return_value='builder'):
                    self.assertEqual(native_hooks.receive(provider,{'hook_event_name':'PreToolUse','tool_name':tool}),'ignored-event')

    def test_worker_environment_strips_only_lead_token(self):
        with patch.dict(os.environ,{'GASTON_LEAD_TOKEN':'private','OTHER_SETTING':'keep'}):
            env=vm._builder_worker_env()
            self.assertNotIn('GASTON_LEAD_TOKEN',env)
            self.assertEqual(env['OTHER_SETTING'],'keep')
            self.assertEqual(os.environ['GASTON_LEAD_TOKEN'],'private')

    def test_implementation_contract_no_autonomous_verification(self):
        prompt=eng.build_prompt({'goal':'Implement payment integration'},{'steps':[]})
        for text in ('necessary non-test implementation commands','Do not run test or acceptance commands','Do not autonomously repair','Never stage, commit, stash'):
            self.assertIn(text,prompt)
        self.assertIn('Do not run tests',eng.builder_runtime_brief())
        self.assertIn('private Lead verifier',eng.builder_runtime_brief())


if __name__=='__main__':unittest.main()
