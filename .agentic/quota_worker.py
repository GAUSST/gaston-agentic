"""One consented local wakeup; no native scheduler, no Lead process or model calls."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import orchestration as orch
import recovery_consent as consent
from session_store import process_identity, process_state

ROOT = Path(__file__).resolve().parent.parent


def schedule(root, folder, approval):
    """Caller holds orchestration guard; record actual spawn before releasing it."""
    root, folder = Path(root), Path(folder)
    old = orch.read_record(folder/'quota-recovery.json')
    if old.get('job_id'):
        proc = old.get('scheduler_process')
        if proc and process_state(proc) == 'live':
            return {'status':'ready','decision':'approved','job_id':old['job_id'],'retry_at':old['retry_at'],'note':'Existing local wakeup is active.'}
        raise ValueError('Previous wakeup is not confirmed active. Revoke approval before scheduling again.')
    job = 'local-'+uuid.uuid4().hex
    manifest = hashlib.sha256((root/'.agentic/manifest.json').read_bytes()).hexdigest()
    env = {k:v for k,v in os.environ.items() if k not in ('GASTON_LEAD_TOKEN','GASTON_SESSION_LEASE','GASTON_LEAD_PROVIDER','GASTON_READONLY_DATABASE_URL','GASTON_DATABASE_ALLOW_FIXTURE_DATA')}
    with (folder/'quota-wakeup.log').open('a',encoding='utf8') as log:
        p = subprocess.Popen([sys.executable,str(root/'.agentic/quota_worker.py'),folder.name,job,manifest],cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,**__import__('platform_compat').popen_kwargs())
    consent.record_job(folder,approval['recovery_token'],job)
    record = orch.read_record(folder/'quota-recovery.json')
    orch.put(folder/'quota-recovery.json',{**record,'scheduler_process':{'pid':p.pid,**process_identity(p.pid)},'scheduler':'gaston-local'})
    return {'status':'ready','decision':'approved','job_id':job,'retry_at':record['retry_at'],'note':'Local wakeup registered. Only the existing Builder task will resume; the original Lead reviews its result. Requires this host and process to remain running.'}


def execute(root, folder, job, manifest, *, sleep=time.sleep):
    root,folder=Path(root),Path(folder)
    while True:
        with orch.mutation_guard(root):
            record=orch.read_record(folder/'quota-recovery.json')
            if record.get('job_id')!=job or record.get('decision')!='approved' or record.get('attempt')!=consent.interruption(folder):return 'cancelled or superseded'
            if hashlib.sha256((root/'.agentic/manifest.json').read_bytes()).hexdigest()!=manifest:return 'toolkit changed; manual resume required'
            left=record['retry_epoch']-time.time()
            if left<=0:
                import verification_mcp as vm
                # This is a local deterministic worker, not an MCP client. Only
                # the persisted consent for this exact job permits the resume.
                if vm.ROOT.resolve()!=root.resolve():return 'workspace mismatch'
                from session_store import Store
                if Store(root).unmanaged_native():return 'unmanaged native chat; manual resume required'
                result=vm._resume_builder({'task_id':folder.name,'recovery_token':record['recovery_token']})
                if result.get('status')!='queued':
                    latest=orch.read_record(folder/'quota-recovery.json')
                    if latest.get('job_id')==job and latest.get('decision')=='approved':
                        orch.put(folder/'quota-recovery.json',{**latest,'decision':'wake_failed','wake_reason':str(result.get('reason','resume blocked'))[:300]})
                return str(result.get('status','unknown'))
        sleep(min(left,15))


if __name__=='__main__':
    if len(sys.argv)!=4 or not re.fullmatch(r'[a-z0-9-]{8,120}',sys.argv[1]):raise SystemExit(2)
    folder=ROOT/'.agentic/runs'/sys.argv[1]
    if folder.is_symlink() or not (folder/'task.json').is_file():raise SystemExit(2)
    print(execute(ROOT,folder,sys.argv[2],sys.argv[3]),flush=True)
