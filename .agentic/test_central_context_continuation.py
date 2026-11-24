"""Central path and bounded same-turn continuation regressions."""
import os
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
        return ti.record_lead_checkpoint(self.root,'claude','sid',summary='Continue migration',decisions=[],next_steps=['Read approved specs'],task_ids=[],plan_ids=[self.plan],safe_to_rotate=True,continue_work=proceed)
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
