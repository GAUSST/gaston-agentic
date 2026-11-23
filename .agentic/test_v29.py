"""Regression: explicit model selection and dangling Claude session recovery."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import eng
import model_menu
import session_store


class ModelIDs(unittest.TestCase):
    def test_legacy_migration_preserves_pinned_ids_and_codex_models(self):
        config={'preferences':{'reviewer':'claude'},
                'models':{'claude_builder':'haiku','claude_reviewer':'sonnet',
                          'codex_builder':'gpt-6-astra'},'advisor':{'claude_reviewer':'fable'}}
        model_menu.normalize_models(config)
        self.assertEqual(config['models'],{'claude_builder':'claude-haiku-5-5',
                         'claude_reviewer':'claude-sonnet-5-5','codex_builder':'gpt-6-astra'})
        self.assertEqual(config['advisor']['claude_reviewer'],'claude-opus-5-5')
        for choice in ('claude-fable-5','claude-fable-5-2','off'):
            config['advisor']['claude_reviewer']=choice
            self.assertEqual(model_menu.normalize_models(config)['advisor']['claude_reviewer'],choice)

    def test_exact_ids_only_in_menu(self):
        self.assertTrue(all(m==label and m.startswith('claude-')
                            for m,label in model_menu.model_choices('claude','reviewer')))
        for value in ('opus','sonnet','luna','default','1',''):
            self.assertFalse(model_menu.valid_model_id('claude',value))
            self.assertFalse(model_menu.valid_model_id('codex',value))
        self.assertTrue(model_menu.valid_model_id('claude','claude-fable-5'))
        self.assertTrue(model_menu.valid_model_id('codex','gpt-6-astra'))

    def test_direct_codex_model_entry_persists_catalog_verified_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            cfg={'preferences':{'builder':'codex','reviewer':'claude'},
                 'models':{'codex_builder':'gpt-5.6-luna','claude_reviewer':'claude-sonnet-5-5'},
                 'reasoning':{'codex_builder':'low','claude_reviewer':'medium'}}
            path.write_text(json.dumps(cfg))
            with patch.object(eng,'CFG_FILE',path),patch.object(eng.sys.stdin,'isatty',return_value=True),\
                 patch('builtins.input',side_effect=['1','gpt-6-astra','']),\
                 patch.object(model_menu,'codex_catalog_details',return_value={'gpt-5.6-luna':('low','medium'),'gpt-6-astra':('low','medium')}):
                eng.models_command()
            self.assertEqual(json.loads(path.read_text())['models']['codex_builder'],'gpt-6-astra')

    def test_direct_claude_and_advisor_ids_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            path.write_text(json.dumps({'preferences':{'builder':'codex','reviewer':'claude'},
                'models':{'codex_builder':'gpt-6-luna','claude_reviewer':'sonnet'},
                'reasoning':{'codex_builder':'low','claude_reviewer':'medium'}}))
            with patch.object(eng,'CFG_FILE',path),patch.object(eng.sys.stdin,'isatty',return_value=True),\
                 patch('builtins.input',side_effect=['2','claude-opus-5-5','','claude-opus-5-5']):
                eng.models_command()
            cfg=json.loads(path.read_text())
            self.assertEqual(cfg['models']['claude_reviewer'],'claude-opus-5-5')
            self.assertEqual(cfg['advisor']['claude_reviewer'],'claude-opus-5-5')

    def test_codex_id_absent_from_catalog_reprompts_without_saving_invalid_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            path.write_text(json.dumps({'preferences':{'builder':'codex','reviewer':'claude'},
                'models':{'codex_builder':'gpt-5.6-luna','claude_reviewer':'claude-sonnet-5-5'}}))
            with patch.object(eng,'CFG_FILE',path),patch.object(eng.sys.stdin,'isatty',return_value=True),\
                 patch('builtins.input',side_effect=['1','gpt-6-astra','gpt-5.6-luna','']),\
                 patch.object(model_menu.shutil,'which',return_value=None),\
                 patch.object(model_menu,'codex_catalog_details',return_value={'gpt-5.6-luna':('low','medium')}):
                eng.models_command()
            self.assertEqual(json.loads(path.read_text())['models']['codex_builder'],'gpt-5.6-luna')


class ClaudeRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'workspace';self.root.mkdir()
        self.home=Path(self.tmp.name)/'claude'
        self.project=self.home/'projects'/'encoded-workspace';self.project.mkdir(parents=True)
        self.store=session_store.Store(self.root)
        self.config={'preferences':{'builder':'codex','reviewer':'claude'},
                     'models':{'claude_reviewer':'claude-sonnet-5-5'},
                     'reasoning':{'claude_reviewer':'medium'},'advisor':{'claude_reviewer':'off'}}
        self.sid=str(uuid.uuid4())
        for p in (patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(self.home)}),
                  patch.object(eng,'SESSIONS',self.store),patch.object(eng,'ROOT',self.root),
                  patch.object(eng,'LOCAL',self.store.local),patch.object(eng,'load_config',return_value=self.config),
                  patch.object(eng,'command_exists',return_value=True),patch.object(eng,'ensure_local_context'),
                  patch.object(eng,'native_hooks_installed',return_value=False),
                  patch.object(eng,'lead_verification_mcp_config',return_value=self.root/'verify.json'),
                  patch.object(eng,'lead_runtime_brief',return_value='test brief'),
                  patch.object(eng,'claude_prompt_refresh_args',return_value=[]),
                  patch('native_hooks.session_active',return_value=False)):
            p.start();self.addCleanup(p.stop)

    def test_failed_startup_then_next_run_starts_new_session(self):
        # First start fails before any conversation is created (e.g. billing).
        with patch.object(eng,'run_interactive_provider',return_value=1) as first:
            with self.assertRaises(SystemExit):eng.chat_command('claude',False,role='reviewer')
        failed=self.store.get('claude','reviewer')
        self.assertEqual(failed['status'],'attention')
        self.assertIn('--session-id',first.call_args.args[0])
        def launch(cmd,**kwargs):
            self.assertNotIn('--resume',cmd)
            new=cmd[cmd.index('--session-id')+1]
            self.assertNotEqual(new,failed['id'])
            (self.project/(new+'.jsonl')).write_text('{}\n')
            return 0
        with patch.object(eng,'run_interactive_provider',side_effect=launch):
            eng.chat_command('claude',False,role='reviewer')
        self.assertEqual(self.store.get('claude','reviewer')['status'],'ready')
        self.assertEqual(self.store.load()['archive'][0]['id'],failed['id'])
        with patch.object(eng,'run_interactive_provider',return_value=0) as resumed:
            eng.chat_command('claude',False,role='reviewer')
        self.assertEqual(resumed.call_args.args[0][1:3],['--resume',self.store.get('claude','reviewer')['id']])

    def test_ready_but_deleted_transcript_recovers(self):
        self.store.bind('claude',self.sid,'ready',role='reviewer')
        with patch.object(eng,'run_interactive_provider',return_value=0) as launched:
            eng.chat_command('claude',False,role='reviewer')
        self.assertIn('--session-id',launched.call_args.args[0])
        self.assertNotEqual(self.store.get('claude','reviewer')['id'],self.sid)

    def test_existing_transcript_preserved_even_after_failure(self):
        self.store.bind('claude',self.sid,'attention',role='reviewer')
        transcript=self.project/(self.sid+'.jsonl');transcript.write_text('existing transcript')
        with patch.object(eng,'run_interactive_provider',return_value=0) as launched:
            eng.chat_command('claude',False,role='reviewer')
        self.assertEqual(launched.call_args.args[0][1:3],['--resume',self.sid])
        self.assertEqual(transcript.read_text(),'existing transcript')
        self.assertFalse(self.store.load()['archive'])

    def test_inaccessible_store_does_not_rotate(self):
        self.store.bind('claude',self.sid,'attention',role='reviewer')
        with patch.object(Path,'iterdir',side_effect=PermissionError('unreadable')):
            self.assertEqual(eng.recover_claude_session('reviewer')['id'],self.sid)
        self.assertFalse(self.store.load()['archive'])

    def test_no_provider_store_after_initialization_failure(self):
        self.store.bind('claude',self.sid,'initializing',role='reviewer')
        self.project.rmdir();self.project.parent.rmdir()
        self.assertIsNone(eng.recover_claude_session('reviewer'))
        self.assertEqual(self.store.load()['archive'][0]['id'],self.sid)

    def test_builder_and_other_provider_bindings_unchanged(self):
        self.store.bind('claude',self.sid,'attention',role='reviewer')
        other=str(uuid.uuid4());self.store.bind('codex',other,'ready',role='builder')
        self.assertIsNone(eng.recover_claude_session('reviewer'))
        self.assertEqual(self.store.get('codex','builder')['id'],other)

if __name__=='__main__': unittest.main()
