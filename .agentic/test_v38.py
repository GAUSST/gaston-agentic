"""Readability gate, advisory analysis and safe overlay cleanup regressions."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import code_quality as quality
import evolution
import learning_engine
import orchestration as orch
import verification_mcp as vm
import acceptance_audit as audit


class QualityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / '.agentic/runs/quality-test-001'
        self.folder.mkdir(parents=True)
        self.code = 'backend/Identity.java'
        self.test = 'backend/IdentityTest.java'
        (self.root / 'backend').mkdir()
        (self.root / self.code).write_text('class Identity {}')
        orch.put(self.folder / 'task.json', {'code_review_policy': 38})
        orch.put(self.folder / 'build.json', {'status': 'done', 'changed_files': [self.code, self.test], 'questions': []})
        orch.put(self.folder / 'state.json', {'stage': 'awaiting_lead_review'})
        self.notes = dict(clarity='Identity names the domain object', difficult_logic='No branching in the fixture', maintainability='Single fixture responsibility', correctness='Inspected declaration and deletion effect')

    def record(self, paths=None, findings=None, resolution=''):
        return quality.record(self.folder, {p: self.notes for p in (paths or [self.code, self.test])}, findings or [], resolution)

    def test_current_review_includes_removed_source_and_lead_tests(self):
        self.assertEqual(quality.report(self.folder)['missing_files'], [self.code, self.test])
        self.assertEqual(self.record([self.code])['status'], 'incomplete')
        self.assertEqual(self.record([self.test])['status'], 'pass')
        (self.root / self.code).write_text('class Identity { int value; }')
        self.assertEqual(quality.report(self.folder)['status'], 'incomplete')
        self.assertEqual(self.record()['status'], 'pass')
        orch.put(self.folder / 'worker-finished.json', {'finished_at': time.time() + 1})
        self.assertEqual(quality.report(self.folder)['status'], 'incomplete')

    def test_findings_require_fix_explanation_and_exact_files(self):
        self.assertEqual(self.record(findings=['Misleading name'])['status'], 'incomplete')
        with self.assertRaises(ValueError): self.record()
        with self.assertRaises(ValueError): quality.record(self.folder, {'unrelated.py': self.notes}, [])
        with self.assertRaises(ValueError): quality.record(self.folder, {self.code: {'clarity': 'OK'}}, [])
        self.assertEqual(self.record(resolution='Renamed and rechecked the affected caller')['status'], 'pass')
        self.assertTrue(orch.read_record(self.folder / quality.RECORD)['had_findings'])

    def test_legacy_task_is_not_reopened(self):
        orch.put(self.folder / 'task.json', {})
        self.assertEqual(quality.report(self.folder)['status'], 'not_required')

    def test_mcp_requires_auth_and_review_before_finalization(self):
        with patch.object(vm, 'ROOT', self.root), patch.object(vm, '_authorized_delegate', return_value=False):
            self.assertEqual(vm.CALLS['record_code_review']({'task_id': self.folder.name})['status'], 'blocked')
        with patch.object(vm, 'ROOT', self.root), patch.object(vm, '_authorized_delegate', return_value=True):
            missing = vm.finalize_delegated_task(self.folder.name, True, 'Reviewed code')
            self.assertEqual(missing['status'], 'blocked')
            self.assertIn('code_review', missing)
            result = vm.CALLS['record_code_review']({'task_id': self.folder.name, 'reviews': {p: self.notes for p in [self.code, self.test]}, 'findings': []})
            self.assertEqual(result['review']['status'], 'pass')
            # A code review does not fabricate test evidence or release readiness.
            self.assertEqual(audit.current(self.folder), [])
            self.assertNotEqual(orch.read_record(self.folder / 'state.json').get('stage'), 'ready_for_human_release_review')

    def test_advisory_hints_do_not_edit_code(self):
        py = self.root / 'backend/fees.py'
        py.write_text('def calculate_fee(amount):\n    if amount > 1:\n        if amount > 2:\n            return 1\n    return 0\n')
        js = self.root / 'backend/worker.dart'
        js.write_text('void retryDelivery() { if (x) { if (y) { return; } } }\n')
        doc = self.root / 'backend/RELEASE_V9.md'
        doc.write_text('Historical release')
        before = {p: evolution.digest(p) for p in (py, js, doc)}
        scan = learning_engine.health(self.root, 'backend')
        found = {(x['kind'], x.get('path')) for x in scan['candidates']}
        self.assertIn(('difficult_logic_comment_review', 'backend/fees.py'), found)
        self.assertIn(('difficult_logic_comment_review', 'backend/worker.dart'), found)
        self.assertIn(('historical_release_document_candidate', 'backend/RELEASE_V9.md'), found)
        self.assertEqual(before, {p: evolution.digest(p) for p in before})

    def test_evolution_requires_candidate_source_and_regression_review(self):
        agent = self.root / '.agentic'
        helper = agent / 'context_brief.py'
        helper.write_text('def total(a, b):\n    return a - b\n')
        entry = agent / 'eng.py'
        entry.write_text('print("ready")\n')
        orch.put(agent / 'manifest.json', {'release': 'v38', 'files': {
            '.agentic/context_brief.py': evolution.digest(helper), '.agentic/eng.py': evolution.digest(entry)}})
        edits = [{'path': '.agentic/context_brief.py', 'before_sha256': evolution.digest(helper), 'content': 'def total(a, b):\n    return a + b\n'}]
        test = 'import unittest\nfrom context_brief import total\nclass Regression(unittest.TestCase):\n    def test_add(self): self.assertEqual(total(2, 1), 3)\n'
        with self.assertRaises(ValueError): evolution.stage(self.root, 'Fix addition', 'Subtracted instead', edits, test)
        with self.assertRaises(ValueError): evolution.stage(self.root, 'Fix addition', 'Subtracted instead', edits, test, source_reviews={'.agentic/context_brief.py': self.notes})
        result = evolution.stage(self.root, 'Fix addition', 'Subtracted instead', edits, test, source_reviews={'.agentic/context_brief.py': self.notes, 'regression_test': self.notes})
        rec = evolution.read(evolution.entry(self.root, result['candidate_id']) / 'record.json')
        self.assertIn(rec['regression_test'], rec['source_reviews'])
        self.assertEqual(evolution.validate(self.root, result['candidate_id'])['status'], 'pass')
        self.assertIn('a - b', helper.read_text())

    def test_cleanup_preserves_modified_owned_and_linked_docs(self):
        release = self.root / 'docs/RELEASE_V28.md'
        release.parent.mkdir()
        release.write_text('shipped fixture')
        mapping = {'docs/RELEASE_V28.md': {evolution.digest(release)}}
        with patch.object(evolution, 'OBSOLETE_RELEASE_FILES', mapping):
            manifest = self.root / evolution.MANIFEST
            orch.put(manifest, {'files': {}})
            self.assertEqual(evolution.cleanup_obsolete_release_files(self.root), ['docs/RELEASE_V28.md'])
            release.write_text('modified by user')
            self.assertEqual(evolution.cleanup_obsolete_release_files(self.root), [])
            release.write_text('shipped fixture')
            orch.put(manifest, {'files': {'docs/RELEASE_V28.md': evolution.digest(release)}})
            self.assertEqual(evolution.cleanup_obsolete_release_files(self.root), [])
            orch.put(manifest, {'files': {}})
            outside = self.root / 'other.md'
            release.rename(outside)
            release.symlink_to(outside)
            self.assertEqual(evolution.cleanup_obsolete_release_files(self.root), [])
            self.assertTrue(outside.exists())


if __name__ == '__main__':
    unittest.main()
