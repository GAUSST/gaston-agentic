"""Recovery regressions using disposable registries, real processes and local records."""
from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from session_store import Store, process_identity
import native_hooks
import orchestration as orch
import runtime_recovery as recovery
import verification_mcp as vm

SID = '00000000-0000-4000-8000-000000000001'


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root)
        self.store.bind('codex', SID, role='builder')
        self.lock = self.store.locks/'codex-builder'
        self.marker = self.store.local/'native-hooks/active/codex-builder.json'

    def dead_process(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        pid = child.pid
        child.wait()
        return pid

    def records(self, pid, *, marker=True, **extra):
        orch.put(self.lock/'owner.json', {'pid':pid,'host':socket.gethostname(),**extra})
        if marker:
            orch.put(self.marker, {'root':str(self.root),'session_id':SID,'started_at':'2026-11-19T16:43:00+00:00'})

    def test_reported_v38_defect_dead_lock_and_marker(self):
        # The old implementation checks this marker before trying stale recovery.
        self.records(self.dead_process())
        with patch.object(native_hooks, 'ROOT', self.root), patch.object(native_hooks, 'LOCAL', self.store.local/'native-hooks'):
            try:
                with self.store.locked('codex','builder'):
                    self.assertFalse(self.marker.exists())
                    self.assertEqual(json.loads((self.lock/'owner.json').read_text())['pid'],os.getpid())
            except RuntimeError as exc:
                self.fail('Proven-dead paired marker prevented Builder recovery: '+str(exc))
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.store.get('codex','builder')['id'],SID)
        self.assertEqual(self.store.reconcile('codex','builder'),'not locked')

    def test_live_and_unknown_owners_are_never_removed(self):
        self.records(os.getpid(), host=socket.gethostname())
        with self.assertRaises(RuntimeError):self.store.reconcile('codex','builder')
        self.assertTrue(self.lock.exists());self.assertTrue(self.marker.exists())
        self.records(self.dead_process(), host='elsewhere')
        with self.assertRaises(RuntimeError):self.store.reconcile('codex','builder')
        self.assertTrue(self.lock.exists());self.assertTrue(self.marker.exists())

    def test_orphan_native_provider_remains_protected(self):
        # Real external native-shaped process: same project and exact session.
        child = subprocess.Popen(['bash','-c','exec -a codex sleep 60'],cwd=self.root)
        try:
            self.records(self.dead_process(), child={'pid':child.pid,**process_identity(child.pid)})
            with self.assertRaises(RuntimeError):self.store.reconcile('codex','builder')
            self.assertTrue(self.marker.exists())
            child.terminate();child.wait(timeout=5)
            self.assertEqual(self.store.reconcile('codex','builder'),'cleared stale lock')
        finally:
            if child.poll() is None:child.kill();child.wait()

    def test_legacy_lock_without_child_metadata_detects_external_cli(self):
        child = subprocess.Popen(['bash','-c','exec -a codex sleep 60'],cwd=self.root)
        try:
            # Wait for exec rather than testing a shell-startup scheduling race.
            for _ in range(100):
                try:
                    if (Path('/proc')/str(child.pid)/'cmdline').read_bytes().startswith(b'codex\0'):break
                except OSError:pass
                time.sleep(.01)
            self.records(self.dead_process())
            with self.assertRaises(RuntimeError):self.store.reconcile('codex','builder')
            child.terminate();child.wait(timeout=5)
            self.assertEqual(self.store.reconcile('codex','builder'),'cleared stale lock')
        finally:
            if child.poll() is None:child.kill();child.wait()

    def test_pid_reuse_and_orphan_marker(self):
        identity=process_identity(os.getpid())
        self.assertTrue(identity)
        self.records(os.getpid(), **{**identity,'start_ticks':str(int(identity['start_ticks'])-1)})
        self.assertEqual(self.store.reconcile('codex','builder'),'cleared stale lock')
        orch.put(self.marker, {'root':str(self.root),'session_id':SID})
        self.assertEqual(self.store.reconcile('codex','builder'),'cleared stale activity')

    def test_mismatched_or_malformed_ownership_fails_closed(self):
        self.records(self.dead_process())
        orch.put(self.marker, {'root':str(self.root),'session_id':'not-registered'})
        with self.assertRaises(RuntimeError):self.store.reconcile('codex','builder')
        self.assertTrue(self.lock.exists());self.assertTrue(self.marker.exists())
        (self.lock/'owner.json').write_text('{invalid')
        with self.assertRaises(RuntimeError):self.store.reconcile('codex','builder')
        self.assertTrue(self.lock.exists())

    def test_hook_end_does_not_remove_newer_lease(self):
        with patch.object(native_hooks,'ROOT',self.root),patch.object(native_hooks,'LOCAL',self.store.local/'native-hooks'),patch.object(native_hooks,'REGISTRY',self.store.registry):
            with self.store.locked('codex','builder'):
                event={'session_id':SID,'cwd':str(self.root),'hook_event_name':'SessionStart'}
                self.assertEqual(native_hooks.receive('codex',event),'registered')
                with patch.dict(os.environ, {'GASTON_SESSION_LEASE':'old-invocation'}):
                    native_hooks.receive('codex',{**event,'hook_event_name':'SessionEnd'})
                self.assertTrue(self.marker.exists())
            self.assertFalse(self.marker.exists())

    def task(self, name='interrupted-task', *, stage='implementing', pid=None):
        folder = self.root/'.agentic/runs'/name
        orch.put(folder/'task.json', {'owner':'builder','write_scope':['src/a.py'],'goal':'Preserve this task'})
        orch.put(folder/'state.json', {'stage':stage,'revision':2,'builder':'codex'})
        orch.put(folder/'revision-request.json', {'number':2,'correction_round':1,'feedback':'Original correction'})
        orch.put(folder/'worker.json', {'pid':pid if pid is not None else self.dead_process(),'started_at':time.time()-3})
        orch.put(folder/'build.json', {'changed_files':['src/a.py'],'status':'blocked'})
        return folder

    def test_abandoned_worker_persists_resumable_state_without_cancel(self):
        folder=self.task();cancelled=self.task('cancelled-task',stage='cancelled')
        self.records(self.dead_process())
        result=recovery.recover(self.root)
        self.assertEqual([x['task_id'] for x in result],[folder.name])
        state=orch.read_record(folder/'state.json')
        self.assertEqual(state['stage'],'blocked');self.assertTrue(state['resumable'])
        self.assertEqual(state['revision'],2)
        self.assertTrue((folder/'worker-finished.json').is_file())
        self.assertEqual(orch.read_record(cancelled/'state.json')['stage'],'cancelled')
        self.assertEqual(recovery.recover(self.root),[])
        self.assertEqual(orch.active_builders(self.root),[])

    def test_recovery_does_not_release_live_orphan_child(self):
        folder=self.task()
        self.records(self.dead_process(),child={'pid':os.getpid(),**process_identity(os.getpid())})
        self.assertEqual(recovery.recover(self.root),[])
        self.assertEqual(orch.read_record(folder/'state.json')['stage'],'implementing')

    def test_launch_claim_cannot_run_a_superseded_or_cancelled_attempt(self):
        folder=self.task(stage='queued_for_builder')
        attempt='a'*32
        orch.put(folder/'worker-launch.json',{'attempt':attempt,'started_at':time.time(),'launcher':{'pid':self.dead_process()}})
        self.assertFalse(recovery.claim_worker(self.root,folder,'b'*32))
        self.assertEqual(len(recovery.recover(self.root)),1)
        self.assertFalse(recovery.claim_worker(self.root,folder,attempt))
        # The new attempt has to be explicitly queued before the child can claim it.
        orch.put(folder/'state.json',{'stage':'queued_for_builder'})
        self.assertTrue(recovery.claim_worker(self.root,folder,attempt))
        self.assertEqual(orch.read_record(folder/'worker.json')['pid'],os.getpid())

    def test_resume_and_known_start_retry_preserve_revision_and_task(self):
        import eng
        folder=self.task()
        orch.put(folder/'state.json',{'stage':'blocked','blocker':'worker_interrupted','resumable':True,'reason':'codex/builder native chat is active; cannot resume that session concurrently','revision':2})
        orch.put(folder/'worker-finished.json',{'finished_at':time.time()})
        self.records(self.dead_process())
        revision=(folder/'revision-request.json').read_bytes()
        def fake_launch(f, *, revision):
            self.assertEqual(f,folder);self.assertEqual(revision,2)
            return 1234
        def save(f, stage, **fields):orch.put(f/'state.json',{'stage':stage,**fields})
        with patch.object(vm,'ROOT',self.root),patch.object(vm,'_authorized_delegate',return_value=True),patch.object(vm,'_launch_builder',side_effect=fake_launch) as launch,patch.object(eng,'save_state',side_effect=save):
            got=vm._retry_known_startup_failure(folder)
            self.assertEqual(got['task_id'],folder.name);self.assertEqual(got['correction_round'],1)
            self.assertEqual((folder/'revision-request.json').read_bytes(),revision)
            orch.put(folder/'state.json',{'stage':'blocked','blocker':'session_start_blocked','resumable':True})
            self.assertIsNone(vm._retry_known_startup_failure(folder))
            self.assertEqual(launch.call_count,1)

    def test_internal_exception_dedup_and_external_error_excluded(self):
        def report():
            try:exec(compile('raise AttributeError("private example")',str(self.root/'.agentic/probe.py'),'exec'),{})
            except AttributeError as exc:return recovery.record_internal_error(self.root,'probe',exc,task_id='interrupted-task')
        first=report();second=report()
        self.assertEqual(first['incident_id'],second['incident_id'])
        self.assertEqual(second['occurrences'],2)
        self.assertNotIn('private example',json.dumps(second))
        self.assertEqual(len(recovery.incidents(self.root)),1)
        self.assertIsNone(recovery.record_internal_error(self.root,'probe',PermissionError('external denial')))


if __name__=='__main__':unittest.main()
