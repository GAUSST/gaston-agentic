"""Hidden/undocumented exact Codex IDs, persisted pair evidence, no paid tests."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import eng
import model_menu as mm
import test_v30

VISIBLE={'gpt-5.6-luna':('low','medium')}
CUSTOM='gpt-6-astra'


def reply(*, stderr='', code=0, events=None):
    if events is None:
        events=[{'type':'thread.started','thread_id':'probe-only'},
                {'type':'item.completed','item':{'type':'agent_message','text':'GASTON_MODEL_OK'}},
                {'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':4}}]
    return {'exit_code':code,'stdout':'\n'.join(map(json.dumps,events)),'stderr':stderr}


class ExactModelTests(unittest.TestCase):
    def test_hidden_catalog_metadata_retained_but_not_recommended(self):
        raw={'models':[{'slug':'gpt-5.6-luna','visibility':'list','supported_reasoning_levels':['low']},
                       {'slug':CUSTOM,'show_in_picker':False,'supported_reasoning_levels':['high','xhigh']}]}
        catalog=mm.parse_codex_catalog(raw,include_hidden=True)
        self.assertEqual(catalog[CUSTOM],('high','xhigh'))
        self.assertNotIn(CUSTOM,dict(mm.model_choices('codex','builder',catalog=catalog)))
        with patch('builtins.input',return_value=CUSTOM),patch.object(mm,'direct_codex_check',side_effect=AssertionError('metadata requires no probe')):
            self.assertEqual(mm.pick_role_model('codex','builder',catalog=catalog),(CUSTOM,None))
        self.assertTrue(mm.verify_codex_model_effort(CUSTOM,'high',catalog=catalog)[0])
        self.assertFalse(mm.verify_codex_model_effort(CUSTOM,'low',catalog=catalog)[0])

    def test_exact_unlisted_model_probe_persists_only_pair_and_reuses(self):
        cfg={'reasoning':{'codex_builder':'low'}}
        with patch.object(mm.shutil,'which',return_value='/usr/bin/codex'), \
             patch.object(mm,'direct_codex_check',return_value=(True,'accepted')) as probe, \
             patch('builtins.input',side_effect=[CUSTOM,'bad','high',CUSTOM,'high']):
            self.assertEqual(mm.pick_role_model('codex','builder',catalog=VISIBLE,config=cfg),(CUSTOM,'high'))
            self.assertEqual(mm.pick_role_model('codex','builder',CUSTOM,catalog=VISIBLE,config=cfg),(CUSTOM,'high'))
        probe.assert_called_once_with(CUSTOM,'high')
        evidence=cfg['codex_verified_pairs']
        self.assertEqual(evidence,{CUSTOM:['high']})
        self.assertTrue(mm.verify_codex_model_effort(CUSTOM,'high',catalog=VISIBLE,verified=evidence)[0])
        self.assertFalse(mm.verify_codex_model_effort(CUSTOM,'low',catalog=VISIBLE,verified=evidence)[0])
        self.assertFalse(mm.verify_codex_model_effort(CUSTOM,'high',catalog={CUSTOM:('low',)},verified=evidence)[0])

    def test_rejected_exact_model_reprompts_and_does_not_store(self):
        cfg={}
        with patch.object(mm.shutil,'which',return_value='/usr/bin/codex'), \
             patch.object(mm,'direct_codex_check',return_value=(False,'rejected')) as probe, \
             patch('builtins.input',side_effect=['gpt-6.2-astra','low','gpt-5.6-luna']):
            self.assertEqual(mm.pick_role_model('codex','builder',catalog=VISIBLE,config=cfg),('gpt-5.6-luna',None))
        probe.assert_called_once_with('gpt-6.2-astra','low')
        self.assertNotIn('codex_verified_pairs',cfg)

    def test_missing_catalog_allows_exact_pair(self):
        with patch.object(mm.shutil,'which',return_value='/usr/bin/codex'), \
             patch.object(mm,'direct_codex_check',return_value=(True,'accepted')), \
             patch('builtins.input',side_effect=[CUSTOM,'high']):
            self.assertEqual(mm.pick_role_model('codex','builder',catalog={},config={}),(CUSTOM,'high'))

    def test_live_probe_args_and_completion_not_just_exit_code(self):
        def run(cmd,**kwargs):
            self.assertEqual(cmd[0],'exec')
            self.assertIn('--ephemeral',cmd); self.assertIn('--ignore-user-config',cmd)
            self.assertIn('read-only',cmd); self.assertIn("approval_policy='never'",cmd)
            self.assertEqual(cmd[cmd.index('--model')+1],CUSTOM)
            self.assertIn("model_reasoning_effort='high'",cmd)
            self.assertTrue(Path(kwargs['cwd']).is_dir())
            self.assertEqual(kwargs['timeout'],mm._CODEX_DIRECT_TIMEOUT)
            self.assertNotIn('shell',kwargs)
            return reply(stderr='Model metadata not found. Using fallback metadata.')
        with patch.object(mm.shutil,'which',return_value='/usr/bin/codex'),patch.object(mm,'_run_codex_probe',side_effect=run):
            self.assertTrue(mm.direct_codex_check(CUSTOM,'high')[0])
        for result in (reply(code=1),reply(events=[]),reply(events=[{'type':'turn.completed'}]),
                       reply(stderr='falling back to model gpt-5.6-luna'),reply(stderr='reasoning effort not supported'),
                       reply(events=[{'type':'turn.failed','error':{'message':'not supported'}}])):
            with patch.object(mm.shutil,'which',return_value='/usr/bin/codex'),patch.object(mm,'_run_codex_probe',return_value=result):
                self.assertFalse(mm.direct_codex_check(CUSTOM,'high')[0])
        for reason, expected in [('timeout','timed out'),('execution_failed','could not launch'),
                                 ('cleanup_failed','cleanup'),('output_limit','output exceeded')]:
            with patch.object(mm.shutil,'which',return_value='/usr/bin/codex'), \
                 patch.object(mm,'_run_codex_probe',return_value={'exit_code':None,'reason':reason}):
                ok, message = mm.direct_codex_check(CUSTOM,'high')
                self.assertFalse(ok)
                self.assertIn(expected,message)

    def test_catalog_and_direct_use_shared_native_runner(self):
        from unittest.mock import Mock
        import platform_compat as pc
        fixture={'models':[{'slug':CUSTOM,'supported_reasoning_levels':['high']}]}
        runner=Mock(side_effect=[{'exit_code':0,'stdout':json.dumps(fixture),'stderr':''},reply()])
        with patch.object(mm.shutil,'which',return_value='/usr/bin/codex'), \
             patch.object(pc,'prepare_subprocess',return_value=(['native-launcher'],{'TEST':'yes'},True)) as prepare, \
             patch.object(pc,'run_verification_process',runner):
            mm.codex_catalog_details.cache_clear()
            try:
                self.assertEqual(mm.codex_catalog_details()[CUSTOM],('high',))
                self.assertTrue(mm.direct_codex_check(CUSTOM,'high')[0])
                self.assertEqual(prepare.call_args_list[0].args[0],['/usr/bin/codex','debug','models'])
                self.assertEqual(prepare.call_args_list[1].args[0][:2],['/usr/bin/codex','exec'])
                self.assertEqual(runner.call_args_list[0].args[0],['native-launcher'])
                self.assertEqual(runner.call_args_list[0].kwargs['env'],{'TEST':'yes'})
                self.assertEqual(runner.call_args_list[1].kwargs['timeout'],mm._CODEX_DIRECT_TIMEOUT)
            finally:
                mm.codex_catalog_details.cache_clear()

    def test_menu_save_then_launch_uses_direct_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'config.json'; path.write_text(json.dumps({'preferences':{'builder':'codex','reviewer':'claude'},'models':{'codex_builder':'gpt-5.6-luna'},'reasoning':{'codex_builder':'low'}}))
            with patch.object(eng,'CFG_FILE',path),patch.object(eng.sys.stdin,'isatty',return_value=True), \
                 patch.object(mm,'codex_catalog_details',return_value=VISIBLE),patch.object(mm.shutil,'which',return_value='/usr/bin/codex'), \
                 patch.object(mm,'direct_codex_check',return_value=(True,'accepted')) as probe, \
                 patch('builtins.input',side_effect=['1',CUSTOM,'high']):
                eng.models_command()
                self.assertEqual(eng.codex_effort_args('codex','builder'),['-c','model_reasoning_effort="high"'])
            self.assertEqual(json.loads(path.read_text())['models']['codex_builder'],CUSTOM)
            probe.assert_called_once_with(CUSTOM,'high')

    def test_wizard_accepts_then_reuses_custom_pair(self):
        fixture=test_v30.SetupRecoveryTests()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(mm,'direct_codex_check',return_value=(True,'accepted')) as probe:
                cfg,_=fixture.setup_run(Path(td),fixture.saved(),[CUSTOM,'high'],catalog=VISIBLE,codex_bin='/usr/bin/codex')
                self.assertTrue(cfg['preferences']['models_configured'])
                self.assertEqual(cfg['models']['codex_builder'],CUSTOM)
                self.assertEqual(cfg['reasoning']['codex_builder'],'high')
                cfg,_=fixture.setup_run(Path(td),cfg,AssertionError('must reuse pair'),catalog=VISIBLE,codex_bin='/usr/bin/codex')
                self.assertTrue(cfg['preferences']['models_configured'])
                probe.assert_called_once_with(CUSTOM,'high')

class ExcludedEffortTests(unittest.TestCase):
    excluded = ('none', 'minimal', 'ultra')
    permitted = ('low', 'medium', 'high', 'xhigh', 'max')

    def test_excluded_filtered_from_catalog_and_cached_evidence(self):
        raw={'models':[{'slug':CUSTOM,'supported_reasoning_levels':list(self.excluded+self.permitted)}]}
        parsed=mm.parse_codex_catalog(raw)
        self.assertEqual(parsed[CUSTOM],self.permitted)
        self.assertEqual(mm.effort_choices('codex'),self.permitted)
        self.assertEqual(mm.effort_choices('codex',CUSTOM,catalog={CUSTOM:('low',*self.excluded)}),('low',))
        self.assertEqual(mm.verified_efforts({CUSTOM:['high',*self.excluded]},CUSTOM),('high',))
        for level in self.excluded:
            with self.subTest(level=level):
                self.assertFalse(mm.verify_codex_model_effort(CUSTOM,level,catalog={CUSTOM:(level,)},verified={CUSTOM:[level]})[0])

    def test_manual_level_menu_only_offers_five_and_never_probes_excluded(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch.object(mm.shutil,'which',return_value='/usr/bin/codex'), \
             patch.object(mm,'direct_codex_check',return_value=(True,'accepted')) as probe, \
             patch('builtins.input',side_effect=[CUSTOM,*self.excluded,'6','5']):
            self.assertEqual(mm.pick_role_model('codex','builder',catalog=VISIBLE,config={}),(CUSTOM,'max'))
        shown=[line.strip() for line in output.getvalue().splitlines() if line.startswith('      ') and any(line.strip().endswith(e.title()) for e in self.permitted+self.excluded)]
        self.assertEqual(shown,[f'{i}. {e.title()}' for i,e in enumerate(self.permitted,1)])
        self.assertEqual(output.getvalue().count('Invalid reasoning level'),4)
        probe.assert_called_once_with(CUSTOM,'max')
        with patch.object(mm,'_run_codex_probe',side_effect=AssertionError('must not execute')):
            for level in self.excluded:
                self.assertFalse(mm.direct_codex_check(CUSTOM,level)[0])

    def test_saved_excluded_cannot_launch_until_reselected(self):
        for level in self.excluded:
            with self.subTest(level=level):
                cfg={'models':{'codex_builder':CUSTOM},'reasoning':{'codex_builder':level},'codex_verified_pairs':{CUSTOM:[level]}}
                with patch.object(eng,'load_config',return_value=cfg),patch.object(mm,'codex_catalog_details',return_value={CUSTOM:('low','max',level)}):
                    with self.assertRaises(SystemExit):
                        eng.codex_effort_args('codex','builder')
                with patch('builtins.input',side_effect=[level,'max']):
                    self.assertEqual(mm.pick_effort('codex','builder',CUSTOM,level,catalog={CUSTOM:('low','max',level)}),'max')

    def test_setup_reselects_saved_excluded_without_changing_model(self):
        fixture=test_v30.SetupRecoveryTests()
        for level in self.excluded:
            with self.subTest(level=level),tempfile.TemporaryDirectory() as td:
                cfg,_=fixture.setup_run(Path(td),fixture.saved(CUSTOM,level),['max'],catalog={CUSTOM:('low','max')},codex_bin='/usr/bin/codex')
                self.assertEqual(cfg['models']['codex_builder'],CUSTOM)
                self.assertEqual(cfg['reasoning']['codex_builder'],'max')
                self.assertTrue(cfg['preferences']['models_configured'])

if __name__=='__main__':unittest.main()
