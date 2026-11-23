"""Regression for validated model selection and retry-on-invalid setup prompts."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eng
import model_menu as mm
import setup_wizard as sw

CATALOG={'gpt-5.6-luna':('low','medium'), 'gpt-6-astra':('high','xhigh'),
         'gpt-no-effort':()}


class ModelPickerTests(unittest.TestCase):
    def test_invalid_id_retry_and_no_unverified_default(self):
        with patch('builtins.input',side_effect=['nonsense','gpt-6.2-astra','gpt-no-effort','gpt-6-astra']) as asks:
            self.assertEqual(mm.pick_model('codex','builder','gpt-6.2-astra',catalog=CATALOG),'gpt-6-astra')
        self.assertEqual(asks.call_count,4)
        self.assertTrue(all('default: gpt-5.6-luna' in c.args[0] for c in asks.call_args_list))

    def test_custom_invalid_blank_then_retry(self):
        with patch('builtins.input',side_effect=['3','','3','gpt-6.2-astra','2']) as asks:
            self.assertEqual(mm.pick_model('codex','builder',catalog=CATALOG),'gpt-6-astra')
        self.assertEqual(asks.call_count,5)

    def test_effort_retries_and_default_is_supported(self):
        with patch('builtins.input',side_effect=['low','9','']) as asks:
            self.assertEqual(mm.pick_effort('codex','builder','gpt-6-astra','low',catalog=CATALOG),'high')
        self.assertEqual(asks.call_count,3)
        self.assertTrue(all('default: high' in c.args[0] for c in asks.call_args_list))

    def test_missing_metadata_blocks_without_question(self):
        with patch('builtins.input',side_effect=AssertionError('no valid choices')):
            for catalog in ({},{'gpt-no-effort':()}):
                with self.assertRaises(mm.ModelCatalogUnavailable):
                    mm.pick_model('codex','builder',catalog=catalog)

    def test_catalog_with_no_family_still_offers_exact_id(self):
        with patch('builtins.input',return_value=''):
            self.assertEqual(mm.pick_model('codex','builder',catalog={'gpt-other':('medium',)}),'gpt-other')

    def test_language_retries(self):
        with patch('builtins.input',side_effect=['7','2']) as asks:
            self.assertEqual(sw.choose_language({},interactive=True),'fr')
        self.assertEqual(asks.call_count,2)

    def test_project_path_retries_and_intentional_skip(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name in ('a','b'):
                (root/name).mkdir(); (root/name/'pom.xml').touch()
            with patch('builtins.input',side_effect=['9','../other','2']) as asks:
                self.assertEqual(sw.choose_stack_root('Java',['a','b'],None,root,interactive=True),'b')
            self.assertEqual(asks.call_count,3)
            with patch('builtins.input',side_effect=['missing','']):
                self.assertIsNone(sw.choose_stack_root('Java',[],None,root,interactive=True))

    def test_models_menu_retries_role_model_effort_and_advisor(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'config.json'
            cfg={'preferences':{'builder':'codex','reviewer':'claude'},'models':{'codex_builder':'gpt-6.2-astra','claude_reviewer':'claude-sonnet-5-5'},'reasoning':{'codex_builder':'low','claude_reviewer':'medium'}}
            path.write_text(json.dumps(cfg))
            with patch.object(eng,'CFG_FILE',path),patch.object(eng.sys.stdin,'isatty',return_value=True),patch.object(mm,'codex_catalog_details',return_value=CATALOG):
                with patch('builtins.input',side_effect=['9','1','gpt-6.2-astra','gpt-6-astra','low','xhigh']), patch.object(mm.shutil,'which',return_value=None):
                    eng.models_command()
                saved=json.loads(path.read_text()); self.assertEqual(saved['models']['codex_builder'],'gpt-6-astra'); self.assertEqual(saved['reasoning']['codex_builder'],'xhigh')
                with patch('builtins.input',side_effect=['2','nope','','9','medium','bad','off']):
                    eng.models_command()
                self.assertEqual(json.loads(path.read_text())['advisor']['claude_reviewer'],'off')

    def test_interrupt_before_effort_does_not_save_model_menu_partial_change(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'config.json'; cfg={'preferences':{'builder':'codex','reviewer':'claude'},'models':{'codex_builder':'gpt-5.6-luna'},'reasoning':{'codex_builder':'low'}};path.write_text(json.dumps(cfg))
            with patch.object(eng,'CFG_FILE',path),patch.object(eng.sys.stdin,'isatty',return_value=True),patch.object(mm,'codex_catalog_details',return_value=CATALOG),patch('builtins.input',side_effect=['1','gpt-6-astra',KeyboardInterrupt()]):
                with self.assertRaises(KeyboardInterrupt): eng.models_command()
            self.assertEqual(json.loads(path.read_text()),cfg)


class SetupRecoveryTests(unittest.TestCase):
    def setup_run(self, root, cfg, answers, catalog=CATALOG, change=False, codex_bin=None):
        local=root/'.agentic/local';path=root/'.agentic/config.json'
        with contextlib.ExitStack() as stack:
            for p in (patch.object(sw,'setup_preview',return_value=True),patch.object(sw,'discover_stacks',return_value={'java':[],'flutter':[]}),patch.object(sw,'choose_stack_root',return_value=None),patch.object(sw,'ok_bin',return_value=True),patch.object(sw,'check_auth',return_value=True),patch.object(sw,'yesno',return_value=change),patch.object(sw,'project_test_commands',return_value=[]),patch('workspace_git.tracked_roots',return_value=([],[])),patch('workspace_git.baseline_ready',return_value=False),patch.object(sw,'testing_category_approval',side_effect=KeyboardInterrupt()),patch.object(mm,'codex_catalog_details',return_value=catalog),patch.object(mm.shutil,'which',return_value=codex_bin),patch('builtins.input',side_effect=answers)):
                stack.enter_context(p)
            try:
                sw.run_setup(root,cfg,interactive=True,maestro_setup=lambda **kw:None,playwright_setup=lambda *a,**kw:None,write_json=eng.write_json,config_file=path,local=local)
            except KeyboardInterrupt: pass
        return json.loads(path.read_text()),json.loads((local/'setup-status.json').read_text())

    def saved(self,model='gpt-6.2-astra',effort='low'):
        return {'preferences':{'setup_language':'en','provider_mode':'both','builder':'codex','reviewer':'claude','models_configured':True},'models':{'codex_builder':model,'claude_reviewer':'claude-sonnet-5-5'},'reasoning':{'codex_builder':effort,'claude_reviewer':'medium'},'checks':[]}

    def test_saved_wrong_id_forces_repick_even_when_models_configured(self):
        with tempfile.TemporaryDirectory() as td:
            cfg,status=self.setup_run(Path(td),self.saved(),['gpt-6.2-astra','gpt-6-astra','low','xhigh'])
            self.assertEqual(cfg['models']['codex_builder'],'gpt-6-astra')
            self.assertEqual(cfg['reasoning']['codex_builder'],'xhigh')
            self.assertEqual(cfg['reasoning']['claude_reviewer'],'medium')
            self.assertTrue(cfg['preferences']['models_configured'])
            self.assertFalse(status['ready'])

    def test_saved_bad_effort_only_reprompts_effort(self):
        with tempfile.TemporaryDirectory() as td:
            cfg,_=self.setup_run(Path(td),self.saved('gpt-6-astra','low'),['low','high'])
            self.assertEqual(cfg['reasoning']['codex_builder'],'high')
            self.assertTrue(cfg['preferences']['models_configured'])

    def test_valid_saved_settings_do_not_reprompt(self):
        with tempfile.TemporaryDirectory() as td:
            cfg,_=self.setup_run(Path(td),self.saved('gpt-6-astra','high'),AssertionError('must reuse valid settings'))
            self.assertTrue(cfg['preferences']['models_configured'])

    def test_no_catalog_stops_setup_and_preserves_saved_choices(self):
        with tempfile.TemporaryDirectory() as td:
            cfg,status=self.setup_run(Path(td),self.saved(),AssertionError('must not prompt for unverifiable model'),catalog={})
            self.assertFalse(cfg['preferences']['models_configured'])
            self.assertFalse(status['ready']); self.assertEqual(status['state'],'incomplete')
            self.assertEqual(cfg['models']['codex_builder'],'gpt-6.2-astra')

    def test_explicit_reconfigure_retries_architecture_and_picks_roles(self):
        with tempfile.TemporaryDirectory() as td:
            cfg,_=self.setup_run(Path(td),self.saved('gpt-5.6-luna','low'),['bad','1','9','3','gpt-6-astra','','high','medium'],change=True)
            self.assertEqual(cfg['preferences']['provider_mode'],'both')
            self.assertEqual(cfg['models']['codex_builder'],'gpt-6-astra')
            self.assertEqual(cfg['reasoning']['codex_builder'],'high')

if __name__=='__main__': unittest.main()
