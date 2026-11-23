"""Portable regressions contributed by a local installation and consolidated here.

The original local tests used a second lock classifier and a standalone settling
API. These tests exercise the shared lease/worker reconciler instead; no local
project paths, transcripts or provider credentials are needed.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import eng
import evolution as evo
import orchestration as orch
import runtime_recovery as recovery
import session_store as sessions
import task_runtime as runtime
import verification_mcp as vm

HERE = Path(__file__).resolve().parent
SID = '00000000-0000-4000-8000-000000000003'


def dead_pid():
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait(timeout=5)
    return proc.pid


class MergedRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = sessions.Store(self.root)
        self.store.bind('codex', SID, role='builder')
        self.lock = self.store.locks/'codex-builder'
        self.marker = self.store.marker('codex','builder')

    def owner(self, pid, **fields):
        orch.put(self.lock/'owner.json', {'pid':pid,'host':socket.gethostname(),**fields})

    def task(self, name='interrupted-task', **state):
        folder = self.root/'.agentic/runs'/name
        orch.put(folder/'task.json', {'owner':'builder','goal':'Preserve','write_scope':['src']})
        orch.put(folder/'state.json', {'stage':'implementing','builder':'codex','revision':2,**state})
        return folder

    def worker(self, folder, pid, **fields):
        orch.put(folder/'worker.json', {'pid':pid,'started_at':time.time()-5,'host':socket.gethostname(),**fields})

    @unittest.skipUnless(sys.platform.startswith('linux'), 'requires proc identity')
    def test_birth_identity_and_zombie(self):
        self.assertEqual(sessions.process_state({'pid':os.getpid(),**sessions.process_identity(os.getpid())}),'live')
        self.assertEqual(sessions.process_state({'pid':os.getpid(),'start_ticks':'0'}),'dead')
        child=subprocess.Popen([sys.executable,'-c','pass'])
        try:
            deadline=time.monotonic()+3
            while sessions.process_state({'pid':child.pid})!='dead' and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertFalse(runtime.alive(child.pid))
        finally:child.wait(timeout=5)

    def test_live_and_other_host_lock_remain_protected(self):
        self.owner(os.getpid())
        self.assertTrue(self.store.active('codex','builder'))
        self.owner(dead_pid(),host='unverifiable-host')
        self.assertTrue(self.store.active('codex','builder'))
        self.assertTrue(self.lock.exists())

    def test_unknown_ownerless_role_lock_is_not_guessed_dead(self):
        self.lock.mkdir(parents=True)
        os.utime(self.lock,(1,1))
        self.assertTrue(self.store.active('codex','builder'))
        self.assertTrue(self.lock.exists())

    def test_fresh_marker_without_owner_is_protected(self):
        import datetime
        orch.put(self.marker,{'root':str(self.root),'session_id':SID,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
        self.assertTrue(self.store.active('codex','builder'))
        self.assertTrue(self.marker.exists())

    def test_exception_releases_atomic_lease_with_birth_identity(self):
        with self.assertRaises(KeyError):
            with self.store.locked('codex','builder'):
                self.assertEqual(orch.read_record(self.lock/'owner.json')['start_ticks'],sessions.process_identity(os.getpid())['start_ticks'])
                raise KeyError('interruption')
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.store.unlock_stale('codex','builder'),'not locked')

    def test_dead_worker_is_reconciled_by_wait(self):
        folder=self.task();self.worker(folder,dead_pid())
        result=runtime.wait_worker(folder,timeout_seconds=1)
        self.assertEqual(result['status'],'blocked');self.assertTrue(result['builder_finished'])
        state=orch.read_record(folder/'state.json')
        self.assertEqual(state['blocker'],'worker_interrupted');self.assertTrue(state['resumable'])
        self.assertEqual(state['revision'],2)
        self.assertEqual(recovery.recover(self.root),[])

    def test_unrecorded_worker_protected_not_claimed_complete(self):
        folder=self.task()
        result=runtime.wait_worker(folder,timeout_seconds=1)
        self.assertFalse(result['builder_finished'])
        self.assertEqual(orch.read_record(folder/'state.json')['stage'],'implementing')
        self.assertEqual(len(orch.active_builders(self.root)),1)

    def test_queued_gap_after_dead_launcher_is_recoverable(self):
        folder=self.task(stage='queued_for_builder',queued_at=time.time(),queued_launcher={'pid':dead_pid()})
        self.assertEqual(len(recovery.recover(self.root)),1)
        self.assertEqual(orch.read_record(folder/'state.json')['blocker'],'worker_interrupted')

    def test_live_queued_launcher_not_overwritten_by_old_worker(self):
        folder=self.task(stage='queued_for_builder',queued_at=time.time(),queued_launcher={'pid':os.getpid()})
        self.worker(folder,dead_pid())
        self.assertEqual(recovery.recover(self.root),[])
        self.assertFalse(runtime.wait_worker(folder,timeout_seconds=1)['builder_finished'])

    def test_pending_launch_does_not_report_old_worker_exit(self):
        folder=self.task(stage='queued_for_builder')
        self.worker(folder,dead_pid(),attempt='old')
        orch.put(folder/'worker-launch.json',{'attempt':'new','started_at':time.time(),'launcher':{'pid':os.getpid()}})
        result=runtime.wait_worker(folder,timeout_seconds=1)
        self.assertEqual(result['status'],'pending');self.assertFalse(result['builder_finished'])

    def test_terminal_launch_failure_does_not_wait_for_live_launcher(self):
        folder=self.task(stage='blocked',blocker='worker_start_failed',resumable=True)
        orch.put(folder/'worker-launch.json',{'attempt':'failed','launcher':{'pid':os.getpid()}})
        result=runtime.wait_worker(folder,timeout_seconds=1)
        self.assertEqual(result['status'],'blocked');self.assertTrue(result['builder_finished'])

    def test_direct_revision_settles_interruption_without_correction_charge(self):
        folder=self.task();self.worker(folder,dead_pid())
        orch.put(folder/'revision-request.json',{'number':2,'correction_round':1,'feedback':'Old correction'})
        def save(f,stage,**fields):orch.put(f/'state.json',{'stage':stage,**fields})
        with patch.object(vm,'ROOT',self.root),patch.object(vm,'_authorized_delegate',return_value=True),patch.object(vm,'_launch_builder',return_value=1234),patch.object(eng,'save_state',side_effect=save):
            result=vm.request_builder_revision(folder.name,'Continue the interrupted edit')
        self.assertEqual(result['status'],'queued');self.assertEqual(result['correction_round'],1)

    def test_direct_status_reconciles_before_reporting(self):
        folder=self.task();self.worker(folder,dead_pid())
        with patch.object(vm,'ROOT',self.root),patch.object(vm,'_authorized_delegate',return_value=True):
            result=vm.delegation_status(folder.name)
        self.assertEqual(result['stage'],'blocked');self.assertTrue(result['resumable'])

    def test_live_and_unrecorded_workers_remain_after_dead_worker_settles(self):
        dead=self.task('dead-worker');self.worker(dead,dead_pid())
        live=self.task('live-worker');self.worker(live,os.getpid(),**sessions.process_identity(os.getpid()))
        self.task('unrecorded-worker')
        self.assertEqual({x['task_id'] for x in orch.active_builders(self.root)},{'live-worker','unrecorded-worker'})
        self.assertTrue(orch.read_record(dead/'state.json')['resumable'])

    def test_orphan_provider_keeps_wait_from_claiming_completion(self):
        folder=self.task();self.worker(folder,dead_pid())
        self.owner(dead_pid(),child={'pid':os.getpid(),**sessions.process_identity(os.getpid())})
        result=runtime.wait_worker(folder,timeout_seconds=1)
        self.assertFalse(result['builder_finished']);self.assertEqual(result['stage'],'implementing')
        self.assertTrue(self.lock.exists())

    @unittest.skipUnless(sys.platform.startswith('linux'),'requires Linux process groups')
    def test_real_wrapper_sigterm_stops_owned_provider_and_grandchild(self):
        child_log=self.root/'child.json'
        provider_code=('import json,os,pathlib,subprocess,time; '
            'c=subprocess.Popen(["sleep","60"]); '
            'pathlib.Path('+repr(str(child_log))+').write_text(json.dumps([os.getpid(),c.pid])); time.sleep(60)')
        script=('import sys,pathlib;sys.path.insert(0,'+repr(str(HERE))+'); '
                'import eng,task_runtime,session_store; '
                'root=pathlib.Path('+repr(str(self.root))+'); eng.SESSIONS=session_store.Store(root); '
                'task_runtime.install_termination_handlers();\n'
                'with eng.SESSIONS.locked("codex","builder"):\n'
                '    eng.run_process([sys.executable,"-c",'+repr(provider_code)+'],root/"log",cwd=root,ai=True)\n')
        worker=subprocess.Popen([sys.executable,'-c',script],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+10
            while not child_log.exists() and worker.poll() is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(child_log.exists(),'provider never started')
            pids=json.loads(child_log.read_text())
            worker.send_signal(signal.SIGTERM)
            self.assertEqual(worker.wait(timeout=12),143)
            for pid in pids:self.assertFalse(runtime.alive(pid),f'owned process survived: {pid}')
            self.assertFalse(self.lock.exists())
        finally:
            if worker.poll() is None:worker.kill();worker.wait(timeout=5)
            if child_log.exists():
                for pid in json.loads(child_log.read_text()):
                    if runtime.alive(pid):
                        try:os.kill(pid,signal.SIGKILL)
                        except ProcessLookupError:pass

    def test_signal_reason_is_not_a_quota_message(self):
        self.assertIn('SIGTERM',runtime.interruption_reason(SystemExit(143)))
        self.assertEqual(runtime.interruption_reason(RuntimeError('plain')),'plain')

    def test_feedback_kernel_lease_released_when_worker_crashes(self):
        local=self.root/'.agentic/local/native-hooks';local.mkdir(parents=True)
        script=('import sys,time,pathlib;sys.path.insert(0,'+repr(str(HERE))+'); import runtime_recovery;\n'
                'with runtime_recovery.feedback_worker_guard(pathlib.Path('+repr(str(self.root))+')) as got:\n'
                '    print(got,flush=True);time.sleep(60)\n')
        worker=subprocess.Popen([sys.executable,'-c',script],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(worker.stdout.readline().strip(),'True')
            self.assertTrue(recovery.feedback_worker_busy(self.root))
            worker.kill();worker.wait(timeout=5)
            self.assertFalse(recovery.feedback_worker_busy(self.root))
        finally:
            if worker.poll() is None:worker.kill();worker.wait(timeout=5)
            worker.stdout.close()

    def test_old_feedback_lock_migrates_and_orphans_quarantined(self):
        local=self.root/'.agentic/local/native-hooks'
        old=local/'worker-lock';old.mkdir(parents=True)
        item=local/'queue/old.processing';orch.put(item,{'feedback':'preserve original evidence'})
        self.assertFalse(recovery.feedback_worker_busy(self.root,settle=True))
        self.assertFalse(old.exists());self.assertFalse(item.exists())
        record=orch.read_record(local/'blocked/old.json')
        self.assertTrue(record['interrupted']);self.assertEqual(record['feedback'],'preserve original evidence')

    def test_live_legacy_feedback_worker_not_migrated(self):
        old=self.root/'.agentic/local/native-hooks/worker-lock';old.mkdir(parents=True)
        script=self.root/'.agentic/hook_worker.py';script.write_text('import time;print("ready",flush=True);time.sleep(60)')
        child=subprocess.Popen([sys.executable,str(script)],cwd=self.root,stdout=subprocess.PIPE,text=True)
        try:
            # Popen returning is not proof that the command line is visible to ps.
            # Wait for the child to execute its entrypoint before scanning it.
            self.assertEqual(child.stdout.readline().strip(),'ready')
            self.assertTrue(recovery.feedback_worker_busy(self.root));self.assertTrue(old.exists())
        finally:
            child.terminate();child.wait(timeout=5);child.stdout.close()
        self.assertFalse(recovery.feedback_worker_busy(self.root));self.assertFalse(old.exists())


class EvolutionPatchTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.source=self.root/'.agentic/context_brief.py';self.source.parent.mkdir()
        self.source.write_bytes(b'def total(a,b):\r\n    return a-b\r\n')
        evo.put(self.root/evo.MANIFEST,{'release':'v40','build':'40.0.0','files':{'.agentic/context_brief.py':evo.digest(self.source)}})

    def test_small_exact_replacement_preserves_unmodified_bytes(self):
        result=evo.apply_replacements(self.source,[{'old_text':'a-b','new_text':'a+b'}])
        self.assertEqual(result.encode(),b'def total(a,b):\r\n    return a+b\r\n')
        for old in ('a','absent',''):
            with self.subTest(old=old),self.assertRaises(ValueError):evo.apply_replacements(self.source,[{'old_text':old,'new_text':'x'}])

    def test_patch_stage_keeps_live_source_and_hash_review(self):
        from code_quality import DIMENSIONS
        review={k:'Verified exact arithmetic behavior and regression contract' for k in DIMENSIONS}
        edits=[{'path':'.agentic/context_brief.py','before_sha256':evo.digest(self.source),'replacements':[{'old_text':'a-b','new_text':'a+b'}]}]
        regression='import unittest\nclass Test(unittest.TestCase):\n    def test_total(self):\n        import context_brief\n        self.assertEqual(context_brief.total(3,2),5)\n'
        result=evo.stage(self.root,'Fix sum','Wrong operator',edits,regression,source_reviews={'.agentic/context_brief.py':review,'regression_test':review})
        self.assertIn(b'a-b',self.source.read_bytes())
        self.assertIn(b'a+b',(self.root/result['candidate_path']/'.agentic/context_brief.py').read_bytes())
        edits[0]['before_sha256']='wrong'
        with self.assertRaises(ValueError):evo.stage(self.root,'Fix sum','Wrong operator',edits,regression,source_reviews={'.agentic/context_brief.py':review,'regression_test':review})

    def test_abandoned_snapshots_pruned_but_rollback_and_evidence_retained(self):
        root=evo.store(self.root);base=evo.digest(self.root/evo.MANIFEST)
        def candidate(n,state,sha):
            key='evo-'+f'{n:016x}';folder=root/'candidates'/key
            for leaf in ('baseline','candidate'):(folder/leaf).mkdir(parents=True)
            evo.put(folder/'record.json',{'id':key,'state':state,'base_manifest_sha256':sha})
            (folder/'before.log').write_text('retain regression evidence')
            return key,folder
        failed,f=candidate(1,'failed',base);stale,s=candidate(2,'validated','old')
        active,a=candidate(3,'applied','old');valid,v=candidate(4,'staged',base)
        queued,q=candidate(5,'validated','old');evo.put(root/'pending.json',{'id':queued})
        evo.put(root/'active.json',{'id':active})
        self.assertEqual(set(evo.prune_candidates(self.root)),{failed,stale})
        for folder in (f,s):
            self.assertFalse((folder/'candidate').exists());self.assertEqual((folder/'before.log').read_text(),'retain regression evidence')
            self.assertEqual(evo.read(folder/'record.json')['state'],'discarded')
        for folder in (a,v,q):self.assertTrue((folder/'candidate').is_dir())
        self.assertEqual(evo.prune_candidates(self.root),[])

    def test_old_queued_evolution_is_archived_after_audited_overlay(self):
        from code_quality import DIMENSIONS
        reviews={k:'Inspected arithmetic and compatibility' for k in DIMENSIONS}
        before=evo.digest(self.source)
        reg='import unittest\nclass Test(unittest.TestCase):\n    def test_one(self): self.assertTrue(True)\n'
        evo.put(self.root/evo.MANIFEST,{'release':'v39','build':'39.0.0','files':{'.agentic/context_brief.py':before}})
        key=evo.stage(self.root,'Proposal','Old behavior',[{'path':'.agentic/context_brief.py','before_sha256':before,'replacements':[{'old_text':'a-b','new_text':'a+b'}]}],reg,source_reviews={'.agentic/context_brief.py':reviews,'regression_test':reviews})['candidate_id']
        folder=evo.entry(self.root,key);record=evo.read(folder/'record.json');record['state']='validated';evo.put(folder/'record.json',record);evo.queue(self.root,key)
        evo.put(self.root/evo.MANIFEST,{'release':'v40','build':'40.0.0','files':{'.agentic/context_brief.py':before}})
        with patch('release_audit.audit',return_value={'status':'pass'}):out=evo.reconcile_overlay(self.root)
        self.assertEqual(out[0]['id'],key);self.assertFalse((evo.store(self.root)/'pending.json').exists());self.assertIn('a+b',(folder/'changes.diff').read_text())
        self.assertEqual(evo.prune_candidates(self.root),[key]);self.assertIn('a+b',(folder/'changes.diff').read_text());self.assertIn(b'a-b',self.source.read_bytes())


if __name__=='__main__':unittest.main()
