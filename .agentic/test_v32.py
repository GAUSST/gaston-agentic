"""v32: native capabilities and provider permission pass-through (offline)."""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import eng
import native_hooks

class NativeLeadPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        (self.root/'docs').mkdir()
        (self.root/'src').mkdir()
        (self.root/'backend/docs').mkdir(parents=True)
        self.patch=patch.object(native_hooks,'ROOT',self.root)
        self.patch.start();self.addCleanup(self.patch.stop)

    def call(self, name, **args):
        return native_hooks.block_native_reviewer_tool({'tool_name':name,'tool_input':args})

    def denied(self,name,**args):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as blocked:
            self.call(name,**args)
        self.assertEqual(blocked.exception.code,2)

    def test_miro_tools_and_new_mcp_servers_are_not_blocked(self):
        for tool in ['mcp__claude_ai_Miro__content_search','mcp__claude_ai_Miro__board_search_boards',
                     'mcp__claude_ai_Miro__board_show','mcp__claude_ai_Miro__canvas_read_as_svg',
                     'mcp__claude_ai_Miro__create_shape','mcp__future_connector__action','WebSearch','WebFetch','Agent','Skill','AskUserQuestion']:
            self.assertIsNone(self.call(tool),tool)

    def test_native_shell_pipeline_or_diagnostics_pass_to_provider(self):
        for cmd in ['git -C backend status --short | head','command -v codex','curl http://127.0.0.1:8080/health','sha256sum src/main.java','npm --version']:
            self.assertIsNone(self.call('Bash',command=cmd))

    def test_native_documentation_write(self):
        for tool in ('Edit','Write','MultiEdit'):
            for path in ('docs/architecture.md','backend/docs/design.rst',str(self.root/'docs'/'review.txt')):
                self.assertIsNone(self.call(tool,file_path=path))

    def test_code_test_config_and_toolkit_edits_blocked(self):
        for tool in ['Edit','Write','MultiEdit']:
            for path in ['src/Main.java','backend/src/test/FooTest.java','docs/example.py','docs/build.sh','pom.xml','.agentic/native_hooks.py','CLAUDE.md','docs/../src/Main.java','../outside/docs/report.md']:
                self.denied(tool,file_path=path)
        self.denied('NotebookEdit',notebook_path='docs/demo.ipynb')
        self.denied('Edit')
        self.denied('apply_patch',patch='*** Add File: main.py')

    def test_doc_path_symlinks_hardlinks_and_hidden_paths_blocked(self):
        app=self.root/'src/main.py';app.write_text('x')
        (self.root/'docs/link.md').symlink_to(app)
        os.link(app,self.root/'docs/hard.md')
        (self.root/'docs/alias').symlink_to(self.root/'src',target_is_directory=True)
        for path in ['docs/link.md','docs/hard.md','docs/alias/new.md','docs/.hidden.md','.agentic/docs/notes.md']:
            self.denied('Write',file_path=path)

    def test_known_local_filesystem_mcp_does_not_bypass_direct_edit_guard(self):
        self.denied('mcp__filesystem__write_file',path='src/main.py')
        self.denied('mcp__filesystem__move_file',source='src/main.py',destination='docs/main.md')
        self.assertIsNone(self.call('mcp__filesystem__write_file',path='docs/notes.md'))

    def test_enrolled_hook_passes_miro_and_nonlead_is_unaffected(self):
        event={'hook_event_name':'PreToolUse','tool_name':'mcp__claude_ai_Miro__board_search_boards','tool_input':{}}
        with patch.object(native_hooks,'registered_role',return_value='reviewer'):
            self.assertEqual(native_hooks.receive('claude',event),'allowed-tool')
        event.update(tool_name='Edit',tool_input={'file_path':'src/main.py'})
        with patch.object(native_hooks,'registered_role',return_value='builder'):
            self.assertEqual(native_hooks.receive('claude',event),'ignored-event')
        with patch.object(native_hooks,'registered_role',return_value=None):
            self.assertEqual(native_hooks.receive('claude',event),'ignored')

    def test_new_connector_never_preapproved_by_gaston(self):
        with patch.object(eng,'ROOT',self.root):
            args=eng.claude_lead_permission_args()
        self.assertNotIn('--tools',args)
        self.assertNotIn('--strict-mcp-config',args)
        self.assertIn('mcp__gaston_verify__git_inspect',args)
        self.assertNotIn('mcp__gaston_verify__git_commit',args)
        self.assertNotIn('mcp__*',args)
        self.assertFalse(any('Miro' in item for item in args))
        self.assertNotIn('Bash(*)',args)
        self.assertEqual(args[args.index('--permission-mode')+1],'default')
        self.assertIn('Edit',args[args.index('--disallowedTools')+1:])

    def test_configured_guard_enables_native_docs_and_missing_guard_fails_closed(self):
        cfg=self.root/'.claude/settings.local.json';cfg.parent.mkdir()
        script=str(self.root/'.agentic/native_hooks.py')
        cfg.write_text(json.dumps({'hooks':{'PreToolUse':[{'hooks':[{'type':'command','command':f'python3 "{script}" claude'}]}]}}))
        with patch.object(eng,'ROOT',self.root):
            self.assertTrue(native_hooks.claude_write_guard_configured(self.root))
            args=eng.claude_lead_permission_args()
            self.assertNotIn('--disallowedTools',args)
            cfg.write_text('{"disableAllHooks":true}')
            self.assertIn('--disallowedTools',eng.claude_lead_permission_args())

    def test_both_providers_share_enrolled_lead_policy(self):
        for provider in ('claude','codex'):
            with self.subTest(provider=provider), patch.object(native_hooks,'registered_role',return_value='reviewer'):
                event={'hook_event_name':'PreToolUse','tool_name':'mcp__new_connector__search','tool_input':{}}
                self.assertEqual(native_hooks.receive(provider,event),'allowed-tool')
                event.update(tool_name='apply_patch',tool_input={'patch':'*** Add File: source.py'})
                with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                    native_hooks.receive(provider,event)
            with patch.object(native_hooks,'registered_role',return_value='builder'):
                self.assertEqual(native_hooks.receive(provider,event),'ignored-event')

    def test_codex_policy_preserves_native_approvals_and_source_sandbox(self):
        import inspect
        self.assertEqual(eng.codex_lead_permission_args(interactive=True),['--sandbox','read-only'])
        self.assertEqual(eng.codex_lead_permission_args(interactive=False),['--sandbox','read-only','-c','approval_policy="never"'])
        self.assertIn('codex_lead_permission_args(interactive=True)',inspect.getsource(eng.chat_command))
        self.assertIn('codex_lead_permission_args(interactive=False)',inspect.getsource(eng.codex_structured))
        self.assertIn('lead_runtime_brief()',inspect.getsource(eng.codex_structured))

    def test_shared_launcher_policy(self):
        import inspect
        for function in [eng.claude_structured,eng.chat_command]:
            source=inspect.getsource(function)
            self.assertIn('claude_lead_permission_args()',source)
            self.assertIn('lead_runtime_brief()',source)
            self.assertNotIn("'Read,Glob,Grep,Bash'",source)

if __name__=='__main__':unittest.main()
