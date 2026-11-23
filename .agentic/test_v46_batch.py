"""Regression tests for Lead batch edits (real files, no AI calls)."""
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestration as orch
import verification_mcp as vm


class BatchEditsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        self.folder=self.root/'.agentic/runs/batch-edits';self.folder.mkdir(parents=True)
        self.a='backend/src/main/A.java';self.b='backend/src/main/B.java'
        self.c='backend/src/test/ATest.java'
        self.write(self.a,b'alpha beta gamma\n');self.write(self.b,b'one two\n')
        orch.put(self.folder/'task.json',{'owner':'lead','write_scope':[self.a,self.b,self.c]})
        orch.put(self.folder/'state.json',{'stage':'lead_implementing'})
        stack=contextlib.ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(vm,'ROOT',self.root))
        stack.enter_context(patch.object(vm,'_authorized_delegate',return_value=True))
        stack.enter_context(patch.object(orch,'active_builders',return_value=[]))

    def write(self,name,data):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)

    def edit(self,name,old,new):
        return {'path':name,'before_sha256':orch.digest(self.root/name),'old_text':old,'new_text':new}

    def call(self,*edits,purpose='implementation'):
        return vm.CALLS['lead_edit_files']({'task_id':'batch-edits','purpose':purpose,'edits':list(edits)})

    def unchanged(self,result,reason):
        self.assertEqual(result['status'],'blocked',result)
        self.assertIn(reason,result['reason']);self.assertEqual((self.root/self.a).read_bytes(),b'alpha beta gamma\n')
        self.assertEqual((self.root/self.b).read_bytes(),b'one two\n');self.assertFalse((self.folder/'lead-edits.json').exists())

    def test_batch_writes_each_file_once_and_records_ledger(self):
        before=orch.digest(self.root/self.a); real_replace=orch.os.replace
        targets=[]
        def track(src,dest):
            targets.append(Path(dest));return real_replace(src,dest)
        with patch.object(orch.os,'replace',side_effect=track):
            result=self.call(self.edit(self.a,'gamma','G'),self.edit(self.b,'two','2'),self.edit(self.a,'alpha','A'),self.edit(self.a,'beta','BETA'))
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(result['edited_files'],[self.a,self.b]);self.assertEqual(targets.count(self.root/self.a),1);self.assertEqual(targets.count(self.root/self.b),1)
        self.assertEqual((self.root/self.a).read_bytes(),b'A BETA G\n');self.assertEqual((self.root/self.b).read_bytes(),b'one 2\n')
        ledger=orch.read_record(self.folder/'lead-edits.json')['files']
        self.assertEqual(ledger[self.a]['before_sha256'],before);self.assertEqual(ledger[self.a]['after_sha256'],orch.digest(self.root/self.a))
        self.assertEqual(set(orch.read_record(self.folder/'build.json')['lead_changed_files']),{self.a,self.b})

    def test_replacements_match_original_not_new_text(self):
        result=self.call(self.edit(self.a,'alpha','beta'),self.edit(self.a,'beta','alpha'))
        self.assertEqual(result['status'],'pass',result);self.assertEqual((self.root/self.a).read_bytes(),b'beta alpha gamma\n')

    def test_overlapping_rejects_entire_batch(self):
        self.unchanged(self.call(self.edit(self.b,'one','1'),self.edit(self.a,'alpha beta','x'),self.edit(self.a,'beta gamma','y')),'Overlapping')

    def test_duplicate_snippets_rejected(self):
        self.unchanged(self.call(self.edit(self.a,'alpha','x'),self.edit(self.a,'alpha','y')),'Overlapping')

    def test_missing_match_in_later_file_rejects_all(self):
        self.unchanged(self.call(self.edit(self.a,'alpha','x'),self.edit(self.b,'absent','z')),'missing or ambiguous')

    def test_self_overlapping_occurrences_are_ambiguous(self):
        self.write(self.b,b'aaa');result=self.call(self.edit(self.a,'alpha','x'),self.edit(self.b,'aa','z'))
        self.assertEqual(result['status'],'blocked',result);self.assertIn('ambiguous',result['reason']);self.assertEqual((self.root/self.a).read_bytes(),b'alpha beta gamma\n');self.assertEqual((self.root/self.b).read_bytes(),b'aaa')

    def test_stale_hash_within_file_rejected(self):
        second=self.edit(self.a,'beta','b');second['before_sha256']='stale'
        self.unchanged(self.call(self.edit(self.a,'alpha','a'),second),'same original hash')

    def test_full_content_must_stand_alone(self):
        full={'path':self.a,'before_sha256':orch.digest(self.root/self.a),'content':'new'}
        self.unchanged(self.call(full,self.edit(self.a,'alpha','a')),'Full content')
        self.unchanged(self.call({**full,'old_text':'alpha','new_text':'a'}),'Full content')

    def test_new_file_requires_content_and_can_be_batched(self):
        self.unchanged(self.call(self.edit(self.c,'x','y'),self.edit(self.a,'alpha','a')),'New files require')
        r=self.call(self.edit(self.a,'alpha','a'),{'path':self.c,'before_sha256':None,'content':'test'})
        self.assertEqual(r['status'],'pass',r);self.assertEqual((self.root/self.c).read_text(),'test')

    def test_crlf_preserved_with_normalized_replacement(self):
        self.write(self.a,b'alpha\r\nbeta\r\ngamma\r\n')
        r=self.call(self.edit(self.a,'alpha\nbeta','A\nB'),self.edit(self.a,'gamma','G'))
        self.assertEqual(r['status'],'pass',r);self.assertEqual((self.root/self.a).read_bytes(),b'A\r\nB\r\nG\r\n')

    def test_scope_and_active_builder_preserved(self):
        with patch.object(orch,'active_builders',return_value=[{'task_id':'other','scope':[self.a]}]):
            self.unchanged(self.call(self.edit(self.b,'one','1'),self.edit(self.a,'alpha','a'),self.edit(self.a,'beta','b')),'Builder may be editing')
        self.unchanged(self.call(self.edit(self.a,'alpha','a'),purpose='test_setup'),'Test setup only')
        other='backend/src/main/C.java';self.write(other,b'outside')
        self.unchanged(self.call(self.edit(self.a,'alpha','a'),self.edit(other,'outside','oops')),'outside the declared')

    def test_canonical_path_alias_cannot_bypass_overlap(self):
        self.unchanged(self.call(self.edit(self.a,'alpha','a'),self.edit('backend/./src/main/A.java','alpha','z')),'Overlapping')

    def test_external_change_before_commit_fails_without_editing_other_file(self):
        real_digest=orch.digest
        edits=[self.edit(self.a,'alpha','a'),self.edit(self.b,'one','1')]
        def changed(path):
            if path==self.root/self.b:self.write(self.b,b'external')
            return real_digest(path)
        with patch.object(orch,'digest',side_effect=changed):r=self.call(*edits)
        self.assertEqual(r['status'],'blocked',r);self.assertIn('Concurrent modification',r['reason']);self.assertEqual((self.root/self.a).read_bytes(),b'alpha beta gamma\n');self.assertEqual((self.root/self.b).read_bytes(),b'external')

    def test_invalid_encoding_rejects_before_any_write(self):
        self.unchanged(self.call(self.edit(self.a,'alpha','a'),self.edit(self.b,'one','\ud800')),'surrogates')

    def test_audit_retains_first_before_hash_across_calls(self):
        first=orch.digest(self.root/self.a)
        self.assertEqual(self.call(self.edit(self.a,'alpha','a'),self.edit(self.a,'beta','b'))['status'],'pass')
        self.assertEqual(self.call(self.edit(self.a,'gamma','g'))['status'],'pass')
        entry=orch.read_record(self.folder/'lead-edits.json')['files'][self.a]
        self.assertEqual(entry['before_sha256'],first);self.assertEqual(entry['after_sha256'],orch.digest(self.root/self.a))


if __name__=='__main__':unittest.main()
