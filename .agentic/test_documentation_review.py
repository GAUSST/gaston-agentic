"""Per-document review reuse and current-revision integration review regressions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import documentation_review as review


class DocumentationReviewReuseTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.root=Path(td.name).resolve()
        self.folder=self.root/'.agentic/runs/task-documentation'
        self.folder.mkdir(parents=True)
        (self.folder/'task.json').write_text('{}')
        (self.folder/'build.json').write_text('{}')
        (self.root/'docs').mkdir()
        self.api=self.root/'docs/api.md';self.api.write_text('# API v1')
        self.arch=self.root/'docs/architecture.md';self.arch.write_text('# Architecture v1')
        self.code=self.root/'src/service.py';self.code.parent.mkdir();self.code.write_text('VERSION=1')
        self.required=patch.object(review,'_required',return_value=['docs/api.md','docs/architecture.md'])
        self.required.start();self.addCleanup(self.required.stop)
        self.changed=patch.object(review,'_changed_documents',return_value=[])
        self.changed.start();self.addCleanup(self.changed.stop)
        self.fingerprint=patch.object(review.acceptance_audit,'revision_fingerprint',side_effect=lambda folder:self.code.read_text())
        self.fingerprint.start();self.addCleanup(self.fingerprint.stop)

    def test_unchanged_documents_are_reused_and_only_integration_review_repeats(self):
        first=review.record(self.folder,{
            'docs/api.md':'Reviewed API contract',
            'docs/architecture.md':'Reviewed architecture',
            '__integration__':'Checked both documents against implementation v1',
        },[])
        self.assertEqual(first['status'],'pass')
        self.code.write_text('VERSION=2')
        changed=review.report(self.folder)
        self.assertEqual(changed['missing_documents'],[])
        self.assertEqual(set(changed['reusable_documents']),{'docs/api.md','docs/architecture.md'})
        self.assertTrue(changed['integration_review_required'])
        self.assertEqual(changed['status'],'incomplete')
        second=review.record(self.folder,{
            '__integration__':'Checked implementation v2 against the unchanged API and architecture documents',
        },[])
        self.assertEqual(second['status'],'pass')
        self.assertFalse(second['integration_review_required'])
        self.assertEqual(second['missing_documents'],[])

    def test_only_changed_document_requires_a_new_per_file_review(self):
        review.record(self.folder,{
            'docs/api.md':'Reviewed API contract',
            'docs/architecture.md':'Reviewed architecture',
            '__integration__':'Checked documents against implementation v1',
        },[])
        self.api.write_text('# API v2')
        changed=review.report(self.folder)
        self.assertEqual(changed['missing_documents'],['docs/api.md'])
        self.assertEqual(changed['reusable_documents'],['docs/architecture.md'])
        self.assertTrue(changed['integration_review_required'])
        result=review.record(self.folder,{
            'docs/api.md':'Reviewed updated API contract',
            '__integration__':'Checked updated API contract against the current implementation',
        },[])
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['missing_documents'],[])


if __name__=='__main__':unittest.main()
