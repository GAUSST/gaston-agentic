"""Deterministic task waiting and honest per-task measurements.

No LLM poll loop and no guessed token/cost figures. Linux pidfds wake when a
specific worker exits; other platforms use a bounded local process check.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import select
import signal
import time

# Default wait covers the existing 30-minute provider-run deadline plus
# startup and evidence writing; an explicit shorter wait is diagnostic only.
BUILDER_WAIT_SECONDS = 3600
MCP_TOOL_TIMEOUT_SECONDS = BUILDER_WAIT_SECONDS + 120

RUNNING = frozenset({'queued_for_builder','implementing','repairing','verifying'})


def read_record(path: Path) -> dict:
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError,UnicodeError):return {}


def alive(pid: object) -> bool:
    from session_store import process_state
    return type(pid) is int and pid > 0 and process_state({'pid':pid}) != 'dead'


def install_termination_handlers():
    """Unwind worker cleanup on interruption; a second signal is a hard stop."""
    signals = tuple(getattr(signal, name) for name in ('SIGTERM', 'SIGHUP', 'SIGINT') if hasattr(signal, name))
    def handler(signum, frame):
        for item in signals:
            signal.signal(item, signal.SIG_DFL)
        raise SystemExit(128 + signum)
    for item in signals:
        signal.signal(item, handler)


def interruption_reason(exc):
    code = getattr(exc, 'code', None)
    if isinstance(exc, SystemExit) and isinstance(code, int) and code > 128:
        try:
            return f'Worker stopped by {signal.Signals(code-128).name}; partial edits preserved; resume this task'
        except ValueError:
            pass
    return str(exc)


def stop_owned_process(proc, *, isolated_group=False, grace_seconds=3):
    """Stop an owned subprocess and its descendants using native host rules."""
    from platform_compat import stop_owned
    stop_owned(proc, isolated_group=isolated_group, grace_seconds=grace_seconds)


def worker_ownership(state, worker, launch):
    """One launch/worker identity decision shared by waiting and reconciliation.

    The old worker is not authoritative between queue publication and launch
    claim. Never use a stale finish marker as evidence for that new attempt.
    """
    from session_store import process_state
    if state.get('stage')=='queued_for_builder':
        queued_at=state.get('queued_at');started_at=launch.get('started_at')
        if isinstance(queued_at,(int,float)) and (not isinstance(started_at,(int,float)) or started_at<queued_at):
            return process_state(state.get('queued_launcher')), True
        if launch.get('attempt') and launch['attempt']!=worker.get('attempt'):
            return process_state(launch.get('launcher')), True
    return process_state(worker), False

def wait_worker(folder: Path, *, timeout_seconds: int=BUILDER_WAIT_SECONDS, cancel_event=None, progress=None) -> dict:
    """One bounded wait; reread attempt identity and reconcile before reporting exit."""
    from session_store import process_state
    folder=Path(folder)
    root=folder.parent.parent.parent if folder.parent.name=='runs' and folder.parent.parent.name=='.agentic' else None
    timeout_seconds=max(1,min(int(timeout_seconds),BUILDER_WAIT_SECONDS))
    started=time.monotonic(); last_progress=started
    fd=None; watched=None
    try:
        while True:
            state=read_record(folder/'state.json')
            stage=state.get('stage','queued_for_builder')
            worker=read_record(folder/'worker.json'); launch=read_record(folder/'worker-launch.json')
            pid=worker.get('pid'); identity=(pid,worker.get('attempt'),worker.get('start_ticks'),worker.get('boot_id'))
            if identity!=watched:
                if fd is not None:os.close(fd);fd=None
                watched=identity
                if process_state(worker)=='live' and type(pid) is int and hasattr(os,'pidfd_open'):
                    try:fd=os.pidfd_open(pid,0)
                    except OSError:pass
            if cancel_event is not None and cancel_event.is_set():
                return {'status':'cancelled','stage':stage,'builder_finished':False,
                        'reason':'Completion subscription cancelled; Builder was not stopped'}
            # A pending launch supersedes the previous worker and completion record.
            ownership,launching=worker_ownership(state,worker,launch)
            launch_pending=launching and ownership!='dead'
            exited=ownership=='dead' or (not launching and fd is not None and bool(select.select([fd],[],[],0)[0]))
            finished=read_record(folder/'worker-finished.json').get('finished_at'); begun=worker.get('started_at')
            recorded_finish=not launching and isinstance(finished,(int,float)) and isinstance(begun,(int,float)) and finished>=begun
            if stage in RUNNING and (exited or launching and not launch_pending) and root is not None:
                from runtime_recovery import recover
                recover(root)
                state=read_record(folder/'state.json');stage=state.get('stage',stage)
            if stage not in RUNNING and not launch_pending and (exited or recorded_finish or not pid):
                return {'status':'blocked' if stage=='blocked' else 'ready','stage':stage,'builder_finished':True,
                        'reason':state.get('reason',''),'questions':state.get('questions',[]),
                        'elapsed_wait_seconds':round(time.monotonic()-started,2),'worker_pid':pid}
            if exited and stage in RUNNING:
                # An orphan provider may still be editing. Never equate wrapper
                # death with a confirmed safe handoff. Leave the task protected.
                return {'status':'blocked','stage':stage,'builder_finished':False,
                        'reason':'Worker exited but task ownership is not reconciled; inspect active provider and recovery status',
                        'elapsed_wait_seconds':round(time.monotonic()-started,2)}
            now=time.monotonic()
            if progress is not None and now-last_progress>=20:
                progress(round(now-started,1),stage);last_progress=now
            remaining=timeout_seconds-(now-started)
            if remaining<=0:
                return {'status':'pending','stage':stage,'builder_finished':False,'reason':'Wait deadline reached; Builder not confirmed finished. Do not mistake this for a completion notification.',
                        'elapsed_wait_seconds':round(time.monotonic()-started,2)}
            interval=min(1.,remaining)
            if fd is not None and not launch_pending:
                select.select([fd],[],[],interval)
            else:time.sleep(interval)
    finally:
        if fd is not None:os.close(fd)


def _legacy_cli_usage(folder: Path) -> dict:
    """Legacy logs only: CLI usage can be a resumed thread lifetime counter."""
    counts={'input_tokens':0,'cached_input_tokens':0,'output_tokens':0}
    turns=0; ambiguous=False
    for path in sorted(folder.glob('build*.log')):
        if path.with_suffix('.usage.json').is_file():continue
        try:
            with path.open(encoding='utf-8',errors='replace') as stream:
                for raw in stream:
                    line=raw.strip().removeprefix('[stderr] ')
                    if not line.startswith('{'):continue
                    try:event=json.loads(line)
                    except ValueError:continue
                    if event.get('type') in ('thread.started','thread.resumed'):ambiguous=True
                    if event.get('type')!='turn.completed':continue
                    usage=event.get('usage')
                    if not isinstance(usage,dict):continue
                    turns+=1
                    for field in counts:
                        if isinstance(usage.get(field),int) and usage[field]>=0:counts[field]+=usage[field]
        except OSError:pass
    return {'source':'legacy_cli_unverified' if ambiguous else 'codex_cli_turn_completed','turns_with_usage':turns,
            'tokens':counts if turns and not ambiguous else None,
            'legacy_reported_tokens':counts if turns and ambiguous else None,
            'claude_lead_tokens':None,
            'note':'Legacy thread-wide counts may be cumulative; never aggregate as measured task usage.'}


def codex_usage(folder: Path) -> dict:
    """Per-attempt native counter deltas; no double-count of archived retries."""
    folder=Path(folder)
    records=[read_record(p) for p in sorted(folder.glob('build*.usage.json')) if not p.is_symlink()]
    if not records:return _legacy_cli_usage(folder)
    from collections import Counter
    total=Counter();tools=Counter();known=0;calls=0;compactions=0;chars=0;contexts=[];providers=set();tools_known=0
    for record in records:
        measured=record.get('usage');providers.add(record.get('provider','unknown'))
        if not isinstance(measured,dict) or not isinstance(measured.get('tokens'),dict):continue
        known+=1;total.update(measured['tokens']);calls+=measured.get('model_calls',0);compactions+=measured.get('compaction_markers',0)
        if isinstance(measured.get('tool_calls'),dict):tools.update(measured['tool_calls']);tools_known+=1
        chars+=measured.get('tool_output_chars') or 0
        if type(measured.get('last_context_tokens')) is int:contexts.append(measured['last_context_tokens'])
    legacy=_legacy_cli_usage(folder)
    # Report any pre-upgrade or crashed log without a matching snapshot as an
    # unknown observation; never add its possibly cumulative CLI tokens.
    unmatched=sum(not p.with_suffix('.usage.json').is_file() for p in folder.glob('build*.log'))
    return {'source':'native_counter_delta','providers':sorted(providers),'tokens':dict(total) if known else None,
            'attempts_with_usage':known,'attempts_without_usage':len(records)-known+unmatched,
            'model_calls':calls if known else None,'compaction_markers':compactions if known else None,
            'tool_calls':dict(tools) if tools_known else None,'tool_output_chars':chars if tools_known else None,
            'max_attempt_end_context_tokens':max(contexts,default=None),
            'rotations':dict(Counter(r.get('rotation','unknown') for r in records)),
            'turns_with_usage':known,'claude_lead_tokens':None,
            'legacy_reported_tokens':legacy.get('legacy_reported_tokens') or legacy.get('tokens'),
            'note':'Measured subset only. Missing or historical attempts stay unknown; no billing or quota assumptions.'}


def task_metrics(folder: Path) -> dict:
    folder=Path(folder); worker=read_record(folder/'worker.json')
    state=read_record(folder/'state.json')
    started=worker.get('started_at')
    ended=read_record(folder/'worker-finished.json').get('finished_at')
    duration=(round(max(0,ended-started),2) if isinstance(started,(int,float))
              and isinstance(ended,(int,float)) else None)
    lead=read_record(folder/'lead-backend-verification.json')
    task=read_record(folder/'task.json')
    beginning=task.get('created_at')
    verified_at=lead.get('verified_at') if lead.get('outcome',{}).get('status')=='pass' else None
    end_to_end=(round(max(0,verified_at-beginning),2)
                if isinstance(beginning,(int,float)) and isinstance(verified_at,(int,float)) else None)
    handoffs=[read_record(p) for p in folder.glob('handoff-*.json')]
    attempts=[read_record(p) for p in folder.glob('attempt-*.json')]
    try:
        import acceptance_audit
        checks=acceptance_audit.read(folder)
    except (OSError,ValueError,ImportError):checks=[]
    return {'task_id':folder.name,'stage':state.get('stage'),
            'handoff_count':len(handoffs),'handoff_prompt_bytes':sum(h.get('prompt_bytes',0) for h in handoffs if isinstance(h.get('prompt_bytes'),int)),
            'builder_attempts':len(attempts),'delta_handoffs':sum(h.get('mode')=='delta' for h in handoffs),'quota_interruptions_observed':sum(a.get('blocker')=='provider_quota' for a in attempts),
            'correction_rounds':read_record(folder/'revision-request.json').get('correction_round',0),
            'lead_takeover':(folder/'takeover.json').is_file(),'recorded_test_checks':len(checks),
            'lead_edited_file_count':len(read_record(folder/'lead-edits.json').get('files',{})),
            'scope_reported_count':len(read_record(folder/'scope-observation.json').get('reported_outside_scope',read_record(folder/'build.json').get('out_of_scope_reported',[]))),
            'scope_pending_count':len(read_record(folder/'build.json').get('scope_review_required',[])),
            'scope_observed_not_attributed_count':len(read_record(folder/'concurrent-observed-paths.json').get('outside_builder_scope',[])),
            'compile_preflight':read_record(folder/'build.json').get('compile_preflight'),
            'full_backend_checks':sum(c.get('kind')=='backend' and (c.get('parameters') or {}).get('test_filter') is None for c in checks),
            'managed_http_checks':sum(bool(c.get('kind')=='api' and (c.get('evidence') or {}).get('managed_application')) for c in checks),
            'worker_elapsed_seconds':duration,
            'builder_revision':state.get('revision',0),
            'lead_verification':lead.get('outcome',{}).get('status'),
            'provider_usage':codex_usage(folder),
            'human_interventions':None,
            'end_to_end_elapsed_seconds':end_to_end,
            'note':'Durations and token counts are observational; no 4x claim without a comparable baseline.'}
