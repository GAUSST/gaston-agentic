"""Confirm native Bash visibility, Git discovery and scoped MCP wiring."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import eng
import native_hooks
import lead_git

class NativeLeadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.repo=self.root/'sample-backend'
        self.repo.mkdir()
        subprocess.run(['git','init','-q'],cwd=self.repo,check=True)

    def test_hook_defers_native_shell_to_provider(self):
        with patch.object(native_hooks,'ROOT',self.root):
            for cmd in ['git -C sample-backend status --short', 'git status | head',
                        'git commit -am test', 'curl https://example.com', 'npm install', 'python3 script.py']:
                self.assertIsNone(native_hooks.block_native_reviewer_tool({'tool_name':'Bash','tool_input':{'command':cmd}}))

    def test_git_mcp_detects_unborn_nested_repo(self):
        result=lead_git.inspect(self.root)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['repository_count'],1)
        self.assertEqual(result['repositories'][0]['repository'],'sample-backend')
        self.assertFalse(result['repositories'][0]['head_exists'])

    def test_cli_permission_rules_are_individual_and_scoped(self):
        rules=eng.lead_safe_mcp_permission_rules()
        self.assertIn('mcp__gaston_verify__git_inspect',rules)
        self.assertNotIn('Bash(git status *)',rules)
        self.assertNotIn('Bash(ls *)',rules)
        self.assertIn('Bash(pwd)',rules)
        self.assertNotIn('mcp__gaston_verify__git_commit',rules)
        self.assertTrue(all(',' not in rule for rule in rules))

    def test_resumed_lead_receives_fresh_repo_and_tool_facts(self):
        with patch.object(eng,'ROOT',self.root):
            brief=eng.lead_runtime_brief()
        self.assertIn('sample-backend',brief)
        self.assertIn('git_inspect',brief)
        self.assertIn('Bash',brief)

class LauncherContractTests(unittest.TestCase):
    def test_existing_lead_session_gets_live_mcp_bash_and_updated_prompt(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from contextlib import nullcontext
        import eng
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            backend=root/'sample-backend';backend.mkdir()
            subprocess.run(['git','init','-q'],cwd=backend,check=True)
            session=SimpleNamespace(get=lambda provider,role:{'id':'34e29cc8-88de-4801-ac52-1fa148b9b7c5'},
                                    locked=lambda provider,role:nullcontext(),
                                    mark=lambda *a,**kw:None)
            argv=[]
            def launch(cmd,**kwargs):
                argv.extend(cmd)
                return 0
            cfg={'preferences':{'builder':'codex','reviewer':'claude'},'models':{'claude_reviewer':'sonnet'},
                 'reasoning':{'claude_reviewer':'medium'},'advisor':{'claude_reviewer':'off'}}
            with patch.object(eng,'ROOT',root),patch.object(eng,'SESSIONS',session),\
                 patch.object(eng,'LOCAL',root/'.agentic/local'),\
                 patch.object(eng,'load_config',return_value=cfg),\
                 patch.object(eng,'command_exists',return_value=True),\
                 patch.object(eng,'ensure_local_context'),\
                 patch.object(eng,'native_hooks_installed',return_value=False),\
                 patch.object(eng,'run_interactive_provider',side_effect=launch),\
                 patch.object(eng,'lead_verification_mcp_config',return_value=root/'verify.json'):
                eng.chat_command('claude',handoff_after=False,role='reviewer')
            self.assertEqual(argv[0:3],['claude','--resume','34e29cc8-88de-4801-ac52-1fa148b9b7c5'])
            self.assertNotIn('--tools',argv)
            self.assertEqual(argv[argv.index('--permission-mode')+1],'default')
            self.assertIn('mcp__gaston_verify__git_inspect',argv)
            self.assertNotIn('Bash(git status *)',argv)
            self.assertIn('sample-backend',argv[argv.index('--append-system-prompt')+1])
            self.assertNotIn('Bash',argv[argv.index('--disallowedTools')+1:argv.index('--append-system-prompt')])
            self.assertNotIn('mcp__gaston_verify__git_commit',argv[argv.index('--allowedTools')+1:argv.index('--disallowedTools')])

class PromptResumeCompatibilityTests(unittest.TestCase):
    def test_supported_claude_version_refreshes_resumed_prompt(self):
        import eng
        from types import SimpleNamespace
        with patch.object(eng.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='2.1.300 (Claude Code)')):
            self.assertEqual(eng.claude_prompt_refresh_args(),['--system-prompt-snapshot','off'])

    def test_older_claude_versions_remain_compatible(self):
        import eng
        from types import SimpleNamespace
        with patch.object(eng.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='2.1.240 (Claude Code)')):
            self.assertEqual(eng.claude_prompt_refresh_args(),[])


if __name__ == "__main__":
    unittest.main()
