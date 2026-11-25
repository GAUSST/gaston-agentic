"""Central path and bounded same-turn continuation regressions."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import token_intelligence as ti
import efficiency_runtime as ef
import orchestration as orch
import project_continuity as pc

class ContextContinuationTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.root=Path(td.name).resolve()
        env=patch.dict(os.environ,{'GASTON_ENGINE_MODE':'central'})
        env.start();self.addCleanup(env.stop)
        self.plan=orch.create_plan(self.root,'Authorized migration',[{'id':'next','owner':'lead','kind':'inspect','goal':'Read approved specs'}])['plan_id']
        for mock in (patch.object(ti,'lead_context',return_value={'status':'rotate_at_safe_boundary','context_tokens':220000}),
                     patch.object(ti,'last_user_fingerprint',return_value='current-turn'),
                     patch.object(pc,'status',return_value={'status':'ready','sha256':'a'*64,'paths':['docs.md']})):
            mock.start();self.addCleanup(mock.stop)
    def checkpoint(self,proceed=True):
        return ti.record_lead_checkpoint(self.root,'claude','sid',summary='Continue migration',decisions=[],next_steps=['Read approved specs'],task_ids=[],plan_ids=[self.plan],safe_to_rotate=True,continue_work=proceed,stop_reason='Developer explicitly requested pause' if proceed is False else None)
    def test_central_checkpoint_written_and_restored(self):
        self.checkpoint()
        self.assertTrue((self.root/'.gaston/local/lead-checkpoint.json').is_file())
        self.assertFalse((self.root/'.agentic/local/lead-checkpoint.json').exists())
        self.assertIn('Continue migration',ti.checkpoint_context(self.root,'claude','sid'))
    def test_continue_once_only_for_same_authorized_turn(self):
        self.checkpoint()
        self.assertIn('next',ef.continuation_reason(self.root,'claude','sid'))
        self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))
        with patch.object(ti,'last_user_fingerprint',return_value='new-user-message'):
            self.assertEqual(ti.checkpoint_context(self.root,'claude','sid'),'')
            self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))
    def test_pause_and_completed_plan_never_force_continuation(self):
        self.checkpoint(False)
        self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))
        self.checkpoint()
        orch.inspection(self.root,self.plan,'next','Reviewed actual specs')
        self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))


    def test_default_checkpoint_continues_and_cannot_rotate_active_intent(self):
        result=self.checkpoint(None)
        self.assertFalse(result['rotation_eligible'])
        self.assertIn('next',ef.continuation_reason(self.root,'claude','sid'))

    def test_current_plan_is_bound_without_checkpoint_and_progress_rearms(self):
        pid=orch.create_plan(self.root,'Authorized complete migration',[
            {'id':'one','owner':'lead','kind':'inspect','goal':'One'},
            {'id':'two','owner':'lead','kind':'inspect','goal':'Two','depends_on':['one']}])['plan_id']
        ef.bind_plan(self.root,'claude','sid',pid)
        self.assertIn('/one',ef.continuation_reason(self.root,'claude','sid'))
        self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))
        orch.inspection(self.root,pid,'one','Reviewed first slice')
        self.assertIn('/two',ef.continuation_reason(self.root,'claude','sid'))
        orch.inspection(self.root,pid,'two','Reviewed second slice')
        self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))

    def test_new_developer_turn_does_not_restart_old_plan(self):
        ef.bind_plan(self.root,'claude','sid',self.plan)
        with patch.object(ti,'last_user_fingerprint',return_value='developer-pause'):
            self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))
            self.assertEqual(ef.bound_plan_context(self.root,'claude','sid'),'')

    def test_explicit_pause_suppresses_bound_plan(self):
        ef.bind_plan(self.root,'claude','sid',self.plan)
        self.checkpoint(False)
        self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))

    def test_review_and_integrated_gate_not_mistaken_for_completion(self):
        pid=orch.create_plan(self.root,'Implement app',[{'id':'module','owner':'builder','kind':'implement','goal':'Module','paths':['src/app.ts'],'acceptance_criteria':['Working module']}])['plan_id']
        plan=orch.get_plan(self.root,pid)
        tid='module-task-001'
        folder=self.root/'.gaston/runs'/tid
        orch.put(folder/'task.json',{'owner':'builder','work_plan_id':pid})
        orch.put(folder/'state.json',{'stage':'awaiting_lead_review'})
        plan['items']['module']['task_id']=tid
        orch.put(orch.plan_file(self.root,pid),plan)
        ef.bind_plan(self.root,'claude','sid',pid)
        self.assertIn('review '+tid,ef.continuation_reason(self.root,'claude','sid'))
        orch.put(folder/'state.json',{'stage':'slice_reviewed'})
        self.assertIn('integrated-acceptance '+pid,ef.continuation_reason(self.root,'claude','sid'))
        cp='checkpoint-task-001'
        orch.put(self.root/'.gaston/runs'/cp/'state.json',{'stage':'awaiting_lead_review'})
        plan['checkpoint_task_id']=cp
        orch.put(orch.plan_file(self.root,pid),plan)
        self.assertIn('review '+cp,ef.continuation_reason(self.root,'claude','sid'))
        orch.put(self.root/'.gaston/runs'/cp/'state.json',{'stage':'ready_for_human_release_review','lead_verified':True})
        self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))

    def test_no_implicit_stop_reason(self):
        with self.assertRaisesRegex(ValueError,'specific|requires'):
            ti.record_lead_checkpoint(self.root,'claude','sid',summary='Context large',decisions=[],next_steps=['Next'],task_ids=[],plan_ids=[self.plan],safe_to_rotate=True,continue_work=False)

    def test_compaction_summary_is_not_a_new_developer_turn(self):
        # Restore real fingerprint implementation despite fixture's mock.
        events=[{'type':'user','uuid':'original','message':{'content':'Finish migration'}},
                {'type':'user','isCompactSummary':True,'message':{'content':'Compacted conversation'}},
                {'type':'user','isMeta':True,'message':{'content':'Hook context'}},
                {'type':'user','message':{'content':[{'type':'tool_result','content':'output'}]}}]
        log=self.root/'native.jsonl'
        log.write_text('\n'.join(json.dumps(x) for x in events))
        # Exercise the real function through a separate import, avoiding the
        # other test's fixed fingerprint mock.
        import importlib.util,hashlib
        spec=importlib.util.spec_from_file_location('real_token_intelligence',ti.__file__)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module,'_native_file',return_value=log):
            expected=hashlib.sha256(json.dumps(events[0],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            self.assertEqual(module.last_user_fingerprint('claude','sid'),expected)
            events.append({'type':'user','uuid':'pause','message':{'content':'Stop now'}})
            log.write_text('\n'.join(json.dumps(x) for x in events))
            self.assertNotEqual(module.last_user_fingerprint('claude','sid'),expected)


    def test_idle_checkpoint_with_future_steps_retains_rotation_eligibility(self):
        result=ti.record_lead_checkpoint(self.root,'claude','sid',
            summary='Completed authorized work',decisions=[],next_steps=['Future phase after approval'],
            task_ids=[],plan_ids=[],safe_to_rotate=True)
        self.assertTrue(result['rotation_eligible'])
        self.assertIsNone(ef.continuation_reason(self.root,'claude','sid'))

    def test_unfinished_task_reference_defaults_to_continuation(self):
        tid='unfinished-task-001'
        folder=self.root/'.gaston/runs'/tid
        orch.put(folder/'task.json',{'owner':'lead'})
        orch.put(folder/'state.json',{'stage':'awaiting_lead_review'})
        result=ti.record_lead_checkpoint(self.root,'claude','sid',
            summary='Implementation awaiting review',decisions=[],next_steps=['Review changes'],
            task_ids=[tid],plan_ids=[],safe_to_rotate=True)
        self.assertFalse(result['rotation_eligible'])
        self.assertIn('review '+tid,ef.continuation_reason(self.root,'claude','sid'))
