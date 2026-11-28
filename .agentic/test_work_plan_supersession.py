"""Auditable work-plan duplicate retirement and dependency rewiring tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import orchestration as orch


class WorkPlanSupersessionTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.root=Path(td.name).resolve()
        self.state=self.root/'.agentic'
        (self.state/'local/work-plans').mkdir(parents=True)
        (self.state/'runs').mkdir(parents=True)
        self.patch_state=patch.object(orch,'project_state',side_effect=lambda root:self.state)
        self.patch_state.start();self.addCleanup(self.patch_state.stop)

    def _plan(self):
        return orch.create_plan(self.root,'Consolidate implementation work',[
            {'id':'legacy','owner':'builder','kind':'implement','goal':'Implement old version',
             'paths':['backend/legacy.py'],'acceptance_criteria':['Old behavior works']},
            {'id':'replacement','owner':'builder','kind':'implement','goal':'Implement consolidated version',
             'paths':['backend/current.py'],'acceptance_criteria':['Consolidated behavior works']},
            {'id':'followup','owner':'lead','kind':'inspect','goal':'Review consolidated implementation',
             'depends_on':['legacy']},
        ])['plan_id']

    def test_supersession_preserves_history_rewires_dependents_and_requires_replacement(self):
        plan_id=self._plan()
        report=orch.supersede_work_item(self.root,plan_id,'legacy','replacement',
                                        'The replacement slice fully covers the old implementation scope')
        plan=orch.get_plan(self.root,plan_id)
        self.assertEqual(plan['items']['legacy']['superseded_by'],'replacement')
        self.assertIn('fully covers',plan['items']['legacy']['superseded_reason'])
        self.assertEqual(plan['items']['followup']['depends_on'],['replacement'])
        self.assertEqual(next(x for x in report['items'] if x['id']=='legacy')['status'],'superseded')
        self.assertFalse(report['all_items_resolved'])

        replacement_task='task-replacement-0001'
        replacement_folder=self.state/'runs'/replacement_task
        replacement_folder.mkdir()
        (replacement_folder/'state.json').write_text(json.dumps({'stage':'slice_reviewed'}))
        plan=orch.get_plan(self.root,plan_id)
        orch.bind(self.root,plan,plan['items']['replacement'],replacement_task)
        orch.inspection(self.root,plan_id,'followup','Reviewed the replacement implementation and found no new gap.')
        final=orch.plan_status(self.root,plan_id)
        self.assertTrue(final['all_items_resolved'])
        self.assertFalse(final['all_items_done'])
        self.assertEqual(final['checkpoint_task_id'],None)

    def test_started_dependent_blocks_supersession(self):
        plan_id=self._plan()
        active='task-followup-0001'
        folder=self.state/'runs'/active;folder.mkdir()
        (folder/'state.json').write_text(json.dumps({'stage':'lead_implementing'}))
        plan=orch.get_plan(self.root,plan_id)
        orch.bind(self.root,plan,plan['items']['followup'],active)
        with self.assertRaisesRegex(ValueError,'already-started dependent'):
            orch.supersede_work_item(self.root,plan_id,'legacy','replacement','Duplicate work')

    def test_missing_replacement_and_empty_reason_are_rejected(self):
        plan_id=self._plan()
        with self.assertRaisesRegex(ValueError,'must exist'):
            orch.supersede_work_item(self.root,plan_id,'legacy','missing','Duplicate')
        with self.assertRaisesRegex(ValueError,'Record why'):
            orch.supersede_work_item(self.root,plan_id,'legacy','replacement','  ')


if __name__=='__main__':unittest.main()
