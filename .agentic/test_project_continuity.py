"""Project-neutral durable context and safe Lead rotation/compaction tests."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

import eng
import native_hooks as hooks
import orchestration as orch
import project_continuity as pc
from session_store import Store
import token_intelligence as ti

SID = '00000000-0000-4000-8000-000000000009'


def roadmap():
    return dict(milestones=[
        {'id': 'phase-setup', 'title': 'Establish foundations', 'scope': 'Configure environment', 'status': 'completed', 'source': 'Approved brief'},
        {'id': 'phase-delivery', 'title': 'Deliver application feature', 'scope': 'Develop and validate feature', 'status': 'planned', 'source': 'Developer decision'}],
        approved_decisions=[{'id': 'd1', 'text': 'Use read-only external test data', 'source': 'Developer decision'}],
        next_milestone_id='phase-delivery', developer_confirmed=True)


class DurableProjectContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / '.agentic/local').mkdir(parents=True)
        self.store = Store(self.root)
        self.store.bind('claude', SID, role='reviewer')

    def doc(self, relative='custom/launch-notes.md', content=None):
        file = self.root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content or '# Our release plan\n\nSprint Orbit: review\nSprint Atlas: delivery\nDecision: never expose private user data\n', encoding='utf-8')
        return file

    def register(self, *paths):
        return pc.register_sources(self.root, paths=list(paths), developer_confirmed=True)

    def checkpoint(self):
        return ti.record_lead_checkpoint(self.root, 'claude', SID, summary='Ready for next phase',
                                         decisions=['Keep current contracts'], next_steps=['Continue approved work'],
                                         task_ids=[], plan_ids=[], safe_to_rotate=True)

    def test_no_filename_or_checkpoint_convention_for_fresh_project(self):
        self.doc('plans/workflow.md')
        self.assertEqual(pc.status(self.root)['status'], 'missing')
        outcome = self.register('plans/workflow.md')
        self.assertEqual(outcome['status'], 'ready')
        self.assertEqual(outcome['path'], 'plans/workflow.md')
        self.assertNotIn('next_milestone_id', outcome)
        self.assertIn('Sprint Atlas', pc.startup_context(self.root))
        self.assertFalse((self.root / pc.RELATIVE_PATH).exists())

    def test_multiple_source_documents_for_different_developers(self):
        self.doc('team/notes/development.rst', 'Delivery approach\nCustom team workflow\n')
        self.doc('releases/milestones.yaml', 'future_release: go-live\n')
        value = self.register('team/notes/development.rst', 'releases/milestones.yaml')
        self.assertEqual(value['paths'], ['team/notes/development.rst', 'releases/milestones.yaml'])
        startup = pc.startup_context(self.root)
        self.assertIn('Custom team workflow', startup)
        self.assertIn('future_release: go-live', startup)
        self.assertNotIn('checkpoint-', startup.lower())

    def test_changed_or_removed_approved_source_blocks_rotation(self):
        file = self.doc('docs/team-plan.md')
        self.register('docs/team-plan.md')
        with patch.object(ti, 'lead_context', return_value={'status': 'rotate_at_safe_boundary', 'context_tokens': 240000}), patch.object(ti, 'last_user_fingerprint', return_value='turn-a'):
            self.assertTrue(self.checkpoint()['rotation_eligible'])
            file.write_text(file.read_text() + '\nNew decision\n')
            self.assertFalse(ti.maybe_rotate_lead(self.root, self.store, 'claude'))
            self.assertTrue(self.checkpoint()['rotation_eligible'])
            file.unlink()
            self.assertEqual(pc.status(self.root)['status'], 'invalid')
            self.assertFalse(ti.maybe_rotate_lead(self.root, self.store, 'claude'))

    def test_multiple_source_digest_protects_source_selection(self):
        a = self.doc('spec/a.md', 'First source')
        b = self.doc('spec/b.md', 'Second source')
        initial = self.register('spec/a.md', 'spec/b.md')
        snapshot = (self.root / pc.REGISTRY_PATH).read_bytes()
        with self.assertRaises(ValueError):
            pc.register_sources(self.root, paths=['spec/b.md'], developer_confirmed=True)
        self.assertEqual(snapshot, (self.root / pc.REGISTRY_PATH).read_bytes())
        updated = pc.register_sources(self.root, paths=['spec/b.md', 'spec/a.md'], developer_confirmed=True,
                                      expected_sha256=hashlib.sha256(snapshot).hexdigest())
        self.assertNotEqual(initial['sha256'], updated['sha256'])

    def test_registration_never_modifies_project_documents(self):
        a = self.doc('planning/team-roadmap.md')
        old = a.read_bytes()
        self.register('planning/team-roadmap.md')
        self.assertEqual(a.read_bytes(), old)
        self.assertFalse((self.root / pc.RELATIVE_PATH).exists())

    def test_non_developer_approved_source_is_rejected(self):
        self.doc()
        with self.assertRaises(ValueError):
            pc.register_sources(self.root, paths=['custom/launch-notes.md'], developer_confirmed=False)
        self.assertFalse((self.root / pc.REGISTRY_PATH).exists())

    def test_outside_paths_credentials_and_symlinks_rejected(self):
        self.doc('docs/approved.md')
        for candidate in ('../outside.md', '.agentic/local/lead-checkpoint.json', '/tmp/anything.md',
                          '.env', 'docs/../docs/approved.md', 'docs/approved.py'):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                pc.register_sources(self.root, paths=[candidate], developer_confirmed=True)
        other = self.root.parent / (self.root.name + '-outside.md')
        other.write_text('Outside project', encoding='utf-8')
        self.addCleanup(other.unlink)
        (self.root / 'docs/redirect.md').symlink_to(other)
        with self.assertRaises(ValueError):
            pc.register_sources(self.root, paths=['docs/redirect.md'], developer_confirmed=True)

    def test_no_mandatory_plan_does_not_invent_milestone(self):
        self.assertEqual(pc.status(self.root)['status'], 'missing')
        self.assertIn('do not invent', pc.startup_context(self.root).lower())
        with patch.object(ti, 'lead_context', return_value={'status': 'rotate_at_safe_boundary', 'context_tokens': 230000}), patch.object(ti, 'last_user_fingerprint', return_value='turn-a'):
            self.assertFalse(self.checkpoint()['rotation_eligible'])
            self.assertFalse(ti.maybe_rotate_lead(self.root, self.store, 'claude'))

    def test_no_assumed_title_for_custom_plan(self):
        self.doc('notes/epics.txt', 'Initiative Alpha then Beta\n')
        result = self.register('notes/epics.txt')
        self.assertNotIn('next_milestone_title', result)
        self.assertIn('Initiative Alpha', pc.startup_context(self.root))

    def test_optional_explicit_next_milestone_is_preserved_without_hardcoding(self):
        self.doc('docs/approved-release-notes.md', '''# Approved delivery sequence
| Stage | Name | Status |
|---|---|---|
| 12 | Reliability improvements | planned |

Next checkpoint: 12
''')
        result = self.register('docs/approved-release-notes.md')
        self.assertEqual(result['next_milestone_title'], 'Reliability improvements')

    def test_register_sources_overrides_old_legacy_json_format_without_mutation(self):
        structured = pc.record(self.root, **roadmap())
        before = (self.root / pc.RELATIVE_PATH).read_bytes()
        self.doc('decisions/accepted.txt', 'This is the current decision register')
        newer = self.register('decisions/accepted.txt')
        self.assertEqual(newer['path'], 'decisions/accepted.txt')
        self.assertEqual(before, (self.root / pc.RELATIVE_PATH).read_bytes())

    def test_structured_legacy_roadmap_remains_supported(self):
        result = pc.record(self.root, **roadmap())
        self.assertEqual(result['next_milestone_title'], 'Deliver application feature')
        self.assertIn('Developer decision', pc.startup_context(self.root))

    def test_structured_roadmap_needs_explicit_approval_and_cas(self):
        data = roadmap(); data['developer_confirmed'] = False
        with self.assertRaises(ValueError): pc.record(self.root, **data)
        first = pc.record(self.root, **roadmap())
        with self.assertRaises(ValueError): pc.record(self.root, **roadmap())
        data = roadmap(); data['milestones'][1]['scope'] = 'Updated scope'
        value = pc.record(self.root, **data, expected_sha256=first['sha256'])
        self.assertNotEqual(first['sha256'], value['sha256'])

    def test_registered_docs_prevent_competing_structured_roadmap(self):
        self.doc()
        self.register('custom/launch-notes.md')
        with self.assertRaises(ValueError): pc.record(self.root, **roadmap())

    def test_legacy_sha_source_recovery_not_filename_specific(self):
        path = self.doc('docs/team-planning-2027.md', '# Approved roadmap\nReview first\n')
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        orch.put(self.root / '.agentic/local/lead-checkpoint.json', {'roadmap_sha256': sha})
        recovered = pc.status(self.root)
        self.assertEqual(recovered['status'], 'ready')
        self.assertEqual(recovered['source_kind'], 'legacy_verified_document')
        self.assertEqual(recovered['path'], 'docs/team-planning-2027.md')

    def test_compaction_reinjects_approved_project_source(self):
        self.doc('workflows/delivery.md', '# Team practice\nUse a three-week delivery cycle\n')
        self.register('workflows/delivery.md')
        event = {'hook_event_name': 'SessionStart', 'source': 'compact', 'session_id': SID, 'cwd': str(self.root)}
        out = io.StringIO()
        with (patch.object(hooks, 'ROOT', self.root), patch.object(hooks, 'receive', return_value='registered'),
              patch.object(hooks, 'registered_role', return_value='reviewer'),
              patch('sys.stdin', io.StringIO(json.dumps(event))),
              patch('sys.argv', ['native_hooks.py', 'claude']), contextlib.redirect_stdout(out)):
            hooks.command_line()
        data = json.loads(out.getvalue())
        self.assertIn('three-week delivery cycle', data['hookSpecificOutput']['additionalContext'])

    def test_compaction_does_not_inject_unregistered_session(self):
        self.doc(); self.register('custom/launch-notes.md')
        event = {'hook_event_name': 'SessionStart', 'source': 'compact', 'session_id': SID, 'cwd': str(self.root)}
        out = io.StringIO()
        with (patch.object(hooks, 'ROOT', self.root), patch.object(hooks, 'receive', return_value='ignored'),
              patch.object(hooks, 'registered_role', return_value=None),
              patch('sys.stdin', io.StringIO(json.dumps(event))),
              patch('sys.argv', ['native_hooks.py', 'claude']), contextlib.redirect_stdout(out)):
            hooks.command_line()
        self.assertFalse(out.getvalue())

    def test_fresh_lead_gets_project_doc_context(self):
        self.doc('docs/release-guide.md')
        self.register('docs/release-guide.md')
        with patch.object(eng, 'ROOT', self.root), patch.object(eng, 'LOCAL', self.root / '.agentic/local'), patch.object(eng, 'RUNS', self.root / '.agentic/runs'):
            body = eng.ensure_local_context().read_text()
        self.assertIn('docs/release-guide.md', body)
        self.assertIn('Sprint Orbit', body)

    def test_rotation_uses_fingerprint_and_optional_milestone(self):
        self.doc('docs/delivery.md')
        initial = self.register('docs/delivery.md')
        with patch.object(ti, 'lead_context', return_value={'status': 'rotate_at_safe_boundary', 'context_tokens': 250000}), patch.object(ti, 'last_user_fingerprint', return_value='a'):
            self.assertTrue(self.checkpoint()['rotation_eligible'])
            record = orch.read_record(self.root / '.agentic/local/lead-checkpoint.json')
            self.assertEqual(record['roadmap_sha256'], initial['sha256'])
            self.assertIsNone(record['next_milestone_id'])
            self.assertTrue(ti.maybe_rotate_lead(self.root, self.store, 'claude'))
            self.assertIsNone(self.store.get('claude', 'reviewer'))

    def test_deleted_registry_blocks_rotation(self):
        self.doc('docs/delivery.md'); self.register('docs/delivery.md')
        with patch.object(ti, 'lead_context', return_value={'status': 'rotate_at_safe_boundary', 'context_tokens': 250000}), patch.object(ti, 'last_user_fingerprint', return_value='a'):
            self.assertTrue(self.checkpoint()['rotation_eligible'])
            (self.root / pc.REGISTRY_PATH).unlink()
            self.assertFalse(ti.maybe_rotate_lead(self.root, self.store, 'claude'))

    def test_developer_can_explicitly_use_no_external_plan(self):
        status = pc.register_sources(self.root, paths=[], developer_confirmed=True)
        self.assertEqual(status['status'], 'ready')
        self.assertEqual(status['paths'], [])
        self.assertEqual(status['source_kind'], 'developer_confirmed_no_external_plan')
        self.assertIn('an external roadmap', pc.startup_context(self.root))
        with patch.object(ti, 'lead_context', return_value={'status': 'rotate_at_safe_boundary', 'context_tokens': 240000}), patch.object(ti, 'last_user_fingerprint', return_value='turn-z'):
            self.assertTrue(self.checkpoint()['rotation_eligible'])
            self.assertTrue(ti.maybe_rotate_lead(self.root, self.store, 'claude'))

    def test_fresh_project_policy_defaults_are_domain_neutral(self):
        self.assertEqual(eng.DEFAULT_POLICIES, [])

    def test_registry_path_approval_not_based_on_filename(self):
        doc = self.doc('protocol/iteration.txt', 'An alternative workflow with no milestones')
        result = self.register('protocol/iteration.txt')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(doc.read_text(), 'An alternative workflow with no milestones')


if __name__ == '__main__':
    unittest.main()
