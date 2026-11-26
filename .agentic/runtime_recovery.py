"""Native interruption reconciliation and durable, deduplicated repair incidents.

Recovery never kills a process, restarts a deliberately stopped task, or edits
provider history. A repair incident is work for the enrolled Lead, not a new
background model invocation.
"""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode
from contextlib import contextmanager
import hashlib
import os
import json
from pathlib import Path
import time
import traceback

from session_store import Store, process_state


def reconcile_tasks(root):
    """Under the orchestration mutation guard, persist proven abandoned jobs."""
    import orchestration as orch
    root = Path(root).resolve()
    store = Store(root)
    outcomes = []
    for folder in (project_state(root)/'runs').glob('*'):
        if not folder.is_dir() or folder.is_symlink():
            continue
        task = orch.read_record(folder/'task.json')
        state = orch.read_record(folder/'state.json')
        worker = orch.read_record(folder/'worker.json')
        if not task or task.get('owner') == 'lead' or state.get('stage') not in ('queued_for_builder','implementing','repairing','verifying'):
            continue
        launch = orch.read_record(folder/'worker-launch.json')
        from task_runtime import worker_ownership
        ownership,_=worker_ownership(state,worker,launch)
        if ownership!='dead':
            continue
        # A dead Python wrapper does not imply its Codex/Claude child died.
        builder = state.get('builder') or store._configured_roles()[0]
        if builder not in ('claude', 'codex') or store.active(builder, 'builder'):
            continue
        prior = orch.read_record(folder/'build.json')
        orch.put(folder/'build.json', {**prior, 'status': 'blocked', 'interrupted': True,
                 'changed_files': sorted(set(prior.get('changed_files', [])) | set(orch.lead_paths(folder))),
                 'questions': []})
        reason = 'Worker exited without terminal evidence; recovered abandoned ownership. Resume the same task.'
        orch.put(folder/'state.json', {**state, 'stage': 'blocked', 'reason': reason,
                 'blocker': 'worker_interrupted', 'resumable': True, 'lead_verified': False,
                 'previous_stage': state.get('stage'), 'recovered_at': time.time()})
        orch.put(folder/'worker-finished.json', {'finished_at': time.time(), 'stage': 'blocked',
                 'revision': state.get('revision', 0), 'recovered': True})
        outcomes.append({'task_id': folder.name, 'action': 'recovered_abandoned_worker', 'resumable': True})
    return outcomes


def recover(root):
    """Run native repair before deciding a workspace is busy."""
    import orchestration as orch
    with orch.mutation_guard(root):
        return reconcile_tasks(root)



def _legacy_feedback_worker_visible(root):
    """Check exact old worker entry points in this Linux workspace, not names alone."""
    proc = Path('/proc')
    if not proc.is_dir():
        return True
    scripts = {runtime_script('hook_worker.py'), runtime_script('eng.py')} if central_mode() else {root/'.agentic/hook_worker.py', root/'.agentic/eng.py'}
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            if process_state({'pid':int(entry.name)}) == 'dead':
                continue
            argv = (entry/'cmdline').read_bytes().split(b'\0')
            for argument in argv[1:]:
                candidate = Path(os.fsdecode(argument))
                if candidate.name not in ('hook_worker.py', 'eng.py'):
                    continue
                if not candidate.is_absolute():
                    candidate = (entry/'cwd').resolve(strict=True)/candidate
                if candidate.resolve() in scripts:
                    return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return True
    return False


@contextmanager
def feedback_worker_guard(root):
    """Portable kernel lease; the OS releases it automatically on crash.

    Use the same guard from the hook worker, activation and uninstall checks.
    """
    from platform_compat import file_lock
    root=Path(root).resolve()
    local=project_state(root)/'local/native-hooks'
    local.mkdir(parents=True,exist_ok=True)
    old=local/'worker-lock'
    try:
        with file_lock(local/'feedback-worker.lock',nonblocking=True):
            if old.exists() or old.is_symlink():
                if old.is_symlink() or not old.is_dir() or any(old.iterdir()) or _legacy_feedback_worker_visible(root):
                    yield False
                    return
                old.rmdir()
            yield True
    except BlockingIOError:
        yield False


def feedback_worker_busy(root, *, settle=False):
    """Only settle feedback while this invocation holds the kernel lease."""
    with feedback_worker_guard(root) as acquired:
        if acquired and settle:
            recover_feedback_items(root)
        return not acquired


def recover_feedback_items(root):
    """Quarantine interrupted fallback feedback; don't repeat possible partial edits.

    Call only while owning the feedback lease and with no live native sessions.
    Preserve the original JSON evidence in the blocked queue.
    """
    import orchestration as orch
    local = project_state(root)/'local/native-hooks'
    for item in (local/'queue').glob('*.processing'):
        if item.is_symlink() or not item.is_file():
            continue
        # Atomically record evidence before removing the interrupted queue entry.
        target = local/'blocked'/item.with_suffix('.json').name
        if not target.exists():
            original = orch.read_record(item)
            if not original:
                continue  # Never discard malformed evidence.
            orch.put(target, {**original, 'error':'Feedback worker interrupted; inspect partial work before retrying', 'interrupted':True})
        item.unlink()

def _store(root):
    return project_state(root)/'local/runtime-incidents'


def record_internal_error(root, operation, exc, *, task_id=None):
    """Escalate demonstrable implementation exceptions, not external failures.

    Keep exception type and toolkit stack locations, not exception text, input
    payloads, secrets, or arbitrary tracebacks. Deduplication prevents repair loops.
    """
    if not isinstance(exc, (AssertionError, AttributeError, IndexError, KeyError, NameError, TypeError, UnboundLocalError)):
        return None
    root = Path(root).resolve()
    from orchestration import put, read_record, mutation_guard
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        path = Path(frame.filename).resolve()
        if path.parent == root/'.agentic' and path.suffix == '.py':
            frames.append(f'{path.name}:{frame.lineno}:{frame.name}')
    if not frames:
        return None
    # The build dimension permits a new incident after a genuine upgrade.
    manifest = read_record((runtime_manifest() if central_mode() else root/'.agentic/manifest.json'))
    signature = [manifest.get('build'), operation, type(exc).__name__, frames[-1]]
    key = 'incident-' + hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:20]
    with mutation_guard(root):
        path = _store(root)/f'{key}.json'
        prior = read_record(path)
        case = {**prior, 'incident_id': key, 'operation': operation, 'exception_type': type(exc).__name__,
                'locations': frames[-6:], 'build': manifest.get('build'), 'occurrences': prior.get('occurrences', 0)+1,
                'first_seen_at': prior.get('first_seen_at', time.time()), 'last_seen_at': time.time(),
                'task_id': task_id or prior.get('task_id'), 'status': 'repair_required'}
        put(path, case)
    return {**case, 'next_action': 'Lead: inspect toolkit source and reproduce the defect; stage_evolution with incident_id, regression and source review; validate and queue. Do not retry blindly or delegate toolkit repair to Builder.'}


def incidents(root):
    from orchestration import read_record
    from evolution import status
    root = Path(root)
    # Correlate candidates using their actual lifecycle, never fabricate repair success.
    candidates = status(root)['candidates']
    cases = []
    for path in sorted(_store(root).glob('incident-*.json')):
        if path.is_symlink():
            continue
        case = read_record(path)
        if not case:
            continue
        linked = [c for c in candidates if c.get('incident_id') == case['incident_id']]
        case['candidates'] = [{'id': c['id'], 'state': c['state']} for c in linked]
        if any(c['state'] == 'applied' and (c.get('applied_at') or 0) >= case['last_seen_at'] for c in linked):
            case['status'] = 'candidate_applied'
        elif any(c['state'] in ('staged', 'validated') for c in linked):
            case['status'] = 'candidate_in_progress'
        elif linked:
            case['status'] = 'repair_needs_review'
        cases.append(case)
    return cases


def claim_worker(root, folder, attempt):
    """The child cannot invoke a provider after its launch was superseded."""
    import os
    import socket
    import orchestration as orch
    from session_store import process_identity
    with orch.mutation_guard(root):
        launch = orch.read_record(folder/'worker-launch.json')
        state = orch.read_record(folder/'state.json')
        worker = orch.read_record(folder/'worker.json')
        if launch.get('attempt') != attempt or state.get('stage') != 'queued_for_builder':
            return False
        if worker.get('attempt') == attempt and worker.get('pid') != os.getpid():
            return False
        orch.put(folder/'worker.json', {'pid':os.getpid(), 'attempt':attempt,
                 'started_at':launch['started_at'], 'claimed_at':time.time(),
                 'host':socket.gethostname(), **process_identity(os.getpid())})
        return True
