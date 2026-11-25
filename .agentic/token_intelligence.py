"""Local provider telemetry and bounded session lifecycle (no model requests).

Codex total_token_usage is a thread lifetime counter, not a task counter.
Snapshot it on both sides of a provider invocation under its role lease.
Only count a verified delta; unknown/missing telemetry stays unknown.
"""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode
import json
import os
from pathlib import Path
import re
import time
import uuid

BUILDER_CONTEXT_ROTATE = 120_000
LEAD_CONTEXT_REVIEW = 150_000
LEAD_CONTEXT_ROTATE = 200_000
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')


def _ints(values, fields=FIELDS):
    return {k: values[k] for k in fields if type(values.get(k)) is int and values[k] >= 0} if isinstance(values, dict) else {}


def _native_file(provider, sid):
    try:
        if str(uuid.UUID(str(sid))) != str(sid).lower():
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    if provider == 'codex':
        base = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
        roots = [base/'sessions', base/'archived_sessions']
        pattern = f'rollout-*-{sid}.jsonl'
    elif provider == 'claude':
        base = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home()/'.claude')))
        roots = [base/'projects']; pattern = f'{sid}.jsonl'
    else:
        return None
    candidates = []
    for root in roots:
        if root.is_dir():
            try: candidates.extend(p for p in root.rglob(pattern) if p.is_file() and not p.is_symlink())
            except OSError: pass
    return candidates[0] if len(candidates) == 1 else None


def _events(path):
    if path is None: return
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try: event = json.loads(line)
                except ValueError: continue
                if isinstance(event, dict): yield event
    except OSError: return


def snapshot(provider, sid, *, path=None):
    """Count native records, deduplicate Claude streaming message snapshots.

    No prompts, source, paths or raw tool output are returned. Counts for Claude
    use the last usage for each distinct message ID and avoid duplicate streams.
    """
    path = Path(path) if path is not None else _native_file(provider, sid)
    last = {}; total = {}; calls = 0; compactions = 0; peak = 0; window = None
    messages = {}; latest_id = None; tool_calls = {}; output_chars = 0
    last_fingerprint = None
    for event in _events(path):
        payload = event.get('payload')
        if not isinstance(payload,dict):payload={}
        if provider == 'codex':
            if event.get('type') == 'compacted' or payload.get('type') in ('context_compacted', 'compacted'):
                # Some versions write both event and summary. Count native
                # markers, not a claimed unique compaction across formats.
                compactions += 1
            if event.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                info = payload.get('info')
                if not isinstance(info,dict):continue
                new = _ints(info.get('total_token_usage'))
                inputs = _ints(info.get('last_token_usage'))
                if not new or not inputs: continue
                fingerprint = (new.get('input_tokens'), new.get('output_tokens'), new.get('cached_input_tokens'), new.get('reasoning_output_tokens'))
                if fingerprint == last_fingerprint: continue
                last_fingerprint = fingerprint; total = new; last = inputs; calls += 1
                peak = max(peak, last.get('input_tokens',0)); window = info.get('model_context_window') or window
            if event.get('type') == 'response_item':
                typ = payload.get('type')
                if typ in ('function_call', 'custom_tool_call') and isinstance(payload.get('name'),str):
                    name = payload['name'][:100]; tool_calls[name] = tool_calls.get(name,0)+1
                if typ in ('function_call_output','custom_tool_call_output'):
                    raw = payload.get('output',''); output_chars += len(raw) if isinstance(raw,str) else len(json.dumps(raw,ensure_ascii=False))
        elif provider == 'claude':
            if event.get('type') == 'system' and event.get('subtype') == 'compact_boundary':compactions+=1
            if event.get('type') != 'assistant':continue
            msg = event.get('message')
            if not isinstance(msg,dict):continue
            mid = msg.get('id'); usage = msg.get('usage')
            if not isinstance(mid,str) or not isinstance(usage,dict):continue
            observed = _ints(usage, ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens','output_tokens'))
            if not observed:continue
            # Streaming records can contain partial usage; preserve each field's
            # latest reported value without counting the same message twice.
            messages[mid] = {**messages.get(mid,{}), **observed}; latest_id = mid
    if provider == 'claude' and messages:
        def normalize(u):
            read = u.get('cache_read_input_tokens',0); created = u.get('cache_creation_input_tokens',0)
            return {'input_tokens':u.get('input_tokens',0)+read+created,'cached_input_tokens':read,'cache_creation_input_tokens':created,'output_tokens':u.get('output_tokens',0)}
        normalized = [normalize(u) for u in messages.values()]
        total = {k:sum(u[k] for u in normalized) for k in normalized[0]}
        last = normalize(messages[latest_id]); calls = len(messages)
        peak = max(u['input_tokens'] for u in normalized)
    if not total or not calls:return None
    return {'provider':provider,'session_id':sid,'tokens':total,'model_calls':calls,
            'last_context_tokens':last.get('input_tokens'),'peak_context_tokens':peak,
            'context_window':window,'compaction_markers':compactions,
            'tool_calls':tool_calls if provider=='codex' else None,
            'tool_output_chars':output_chars if provider=='codex' else None}


def delta(before, after, *, new_session=False):
    if not after: return None
    if before is None:
        if not new_session:return None
        before = {'session_id':after['session_id'],'tokens':{k:0 for k in after['tokens']},'model_calls':0,'compaction_markers':0,'tool_calls':{},'tool_output_chars':0}
    if before['session_id']!=after['session_id']:return None
    common = before['tokens'].keys() & after['tokens'].keys()
    tokens = {k:after['tokens'][k]-before['tokens'][k] for k in common}
    if not all(k in tokens for k in ('input_tokens','cached_input_tokens','output_tokens')) or any(v<0 for v in tokens.values()):return None
    if tokens['cached_input_tokens']>tokens['input_tokens']:return None
    tokens['uncached_input_tokens']=tokens['input_tokens']-tokens['cached_input_tokens']
    calls=after['model_calls']-before['model_calls']; compact=after['compaction_markers']-before['compaction_markers']
    if calls<0 or compact<0:return None
    tools=None
    if isinstance(before.get('tool_calls'),dict) and isinstance(after.get('tool_calls'),dict):
        tools={k:v-before['tool_calls'].get(k,0) for k,v in after['tool_calls'].items() if v>before['tool_calls'].get(k,0)}
    chars=None
    if type(before.get('tool_output_chars')) is int and type(after.get('tool_output_chars')) is int:chars=max(0,after['tool_output_chars']-before['tool_output_chars'])
    return {'tokens':tokens,'model_calls':calls,'compaction_markers':compact,'tool_calls':tools,'tool_output_chars':chars,
            'last_context_tokens':after['last_context_tokens'],'session_peak_context_tokens':after['peak_context_tokens'],'context_window':after['context_window']}


def builder_rotation(root, provider, existing, task_id, *, current=None):
    """Fresh execution context per independent task, same task for corrections.

    Missing ownership is legacy state, so rotate once on upgrade. A high
    context inside one task is rotated only before a new invocation (never
    while active); original spec, prior result, current source remain on disk.
    """
    if not existing:return 'new_session'
    from task_runtime import read_record
    binding=read_record(project_state(root)/'local'/f'builder-context-{provider}.json')
    if binding.get('session_id')!=existing['id'] or binding.get('task_id')!=task_id:
        return 'new_task'
    if current is not None and (current.get('last_context_tokens') or 0)>=BUILDER_CONTEXT_ROTATE:
        return 'context_budget'
    return 'same_task'


def begin_builder(store, root, provider, existing, path, *, role):
    """Called inside the provider's role lock. Limit changes to live tasks."""
    path=Path(path); root=Path(root)
    try: relative=path.resolve().relative_to((project_state(root)/'runs').resolve())
    except ValueError: return existing, None, None
    if role!='builder' or len(relative.parts)!=2 or not path.name.startswith('build'):return existing,None,None
    before=snapshot(provider,existing['id']) if existing else None
    decision=builder_rotation(root,provider,existing,relative.parts[0],current=before)
    if existing and decision!='same_task':
        store.rotate(provider,'builder'); existing=None;before=None
    return existing,before,decision


def attempt_log(path):
    """Archive an existing exact log rather than overwriting a retry."""
    path=Path(path); log=path.with_suffix('.log')
    if log.is_file():
        archived=log.with_name(f'{log.stem}-attempt-{uuid.uuid4().hex}.log')
        os.replace(log, archived)
        # Move companion telemetry consistently if the same revision is retried.
        record=log.with_suffix('.usage.json')
        if record.is_file():os.replace(record,archived.with_suffix('.usage.json'))
    return log


def finish_builder(root, provider, sid, path, before, decision, *, returncode=None):
    if decision is None:return
    import orchestration
    after=snapshot(provider,sid) if sid else None
    measured=delta(before,after,new_session=decision!='same_task')
    orchestration.put(Path(path).with_suffix('.usage.json'),{'provider':provider,'session_id':sid,'rotation':decision,'returncode':returncode,'recorded_at':time.time(),'usage':measured,'source':'native_counter_delta' if measured else 'unavailable'})
    if not sid:return
    orchestration.put(project_state(root)/'local'/f'builder-context-{provider}.json',{'session_id':sid,'task_id':Path(path).parent.name,'last_context_tokens':after.get('last_context_tokens') if after else None,'recorded_at':time.time()})


def lead_context(root, provider, sid):
    current=snapshot(provider,sid)
    if current is None:return {'status':'unavailable','context_tokens':None}
    context=current.get('last_context_tokens') or 0
    return {'status':'rotate_at_safe_boundary' if context>=LEAD_CONTEXT_ROTATE else 'review' if context>=LEAD_CONTEXT_REVIEW else 'within_budget','context_tokens':context,'session_usage':current['tokens'],'model_calls':current['model_calls'],'compaction_markers':current['compaction_markers'],'note':'Session-wide Lead usage; not attributed to individual tasks.'}


def last_user_fingerprint(provider, sid):
    """Identify the last developer message without copying its text into memory."""
    import hashlib
    last=None
    path=_native_file(provider,sid)
    for event in _events(path):
        if provider=='codex':
            payload=event.get('payload')
            if not isinstance(payload,dict):continue
            if event.get('type')=='event_msg' and payload.get('type')=='user_message':last=event
        elif provider=='claude' and event.get('type')=='user' and not any(event.get(k) for k in ('isSidechain','isMeta','isCompactSummary','isSynthetic')):
            message=event.get('message')
            if not isinstance(message,dict):continue
            content=message.get('content')
            if isinstance(content,str) or (isinstance(content,list) and any(isinstance(b,dict) and b.get('type')=='text' for b in content) and not any(isinstance(b,dict) and b.get('type')=='tool_result' for b in content)):
                last=event
    if last is None:return None
    return hashlib.sha256(json.dumps(last,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _rotation_blockers(root):
    """Don't interrupt an in-flight worker or provider-native scheduled recovery."""
    from task_runtime import read_record,RUNNING
    blockers=[]
    for folder in (project_state(root)/'runs').glob('*'):
        if not folder.is_dir() or folder.is_symlink():continue
        st=read_record(folder/'state.json'); recovery=read_record(folder/'quota-recovery.json')
        if st.get('stage') in RUNNING or st.get('stage')=='lead_implementing':blockers.append(folder.name)
        elif st.get('blocker')=='provider_quota' and recovery.get('decision')=='approved':blockers.append(folder.name)
    from verification_coordination import active
    if active(root):blockers.append('managed-verification')
    return blockers


def record_lead_checkpoint(root,provider,sid,*,summary,decisions,next_steps,task_ids,plan_ids,safe_to_rotate,continue_work=None,stop_reason=None):
    """Durable bounded state, not a transcript or permission override."""
    from orchestration import put,plan_file
    root=Path(root); local=project_state(root)/'local'
    if not isinstance(summary,str) or not summary.strip() or len(summary)>8000:raise ValueError('Provide factual checkpoint summary, at most 8000 characters')
    for label,value in (('decisions',decisions),('next_steps',next_steps),('task_ids',task_ids),('plan_ids',plan_ids)):
        if not isinstance(value,list) or len(value)>30 or not all(isinstance(v,str) and 0<len(v)<=1200 for v in value):raise ValueError('Invalid '+label)
    if continue_work is not None and type(continue_work) is not bool:raise ValueError('continue_work must be boolean')
    if stop_reason is not None and (not isinstance(stop_reason,str) or not stop_reason.strip() or len(stop_reason)>1200):raise ValueError('Provide a specific stop reason, at most 1200 characters')
    if continue_work is False and not stop_reason:raise ValueError('Explicitly stopping requires the developer pause or concrete blocker; a context checkpoint is not a stop reason')
    if stop_reason and continue_work is not False:raise ValueError('stop_reason requires continue_work=false')
    if type(safe_to_rotate) is not bool:raise ValueError('Specify whether all material user intent has been preserved and it is safe to rotate after closing')
    for tid in task_ids:
        if not re.fullmatch(r'[a-z0-9-]{8,120}',tid) or not (project_state(root)/'runs'/tid/'task.json').is_file():raise ValueError('Unknown task ID: '+tid)
    for pid in plan_ids:
        if not plan_file(root,pid).is_file():raise ValueError('Unknown plan ID: '+pid)
    import project_continuity as continuity
    budget=lead_context(root,provider,sid);fingerprint=last_user_fingerprint(provider,sid);blockers=_rotation_blockers(root)
    roadmap=continuity.status(root)
    # Inherit current-turn authorized plan IDs; a checkpoint need not enumerate
    # them a second time. A prior user turn cannot grant authority.
    from efficiency_runtime import bound_plans
    if fingerprint:
        plan_ids=list(dict.fromkeys([*bound_plans(root,provider,sid,fingerprint),*plan_ids]))
    if continue_work is None:
        # An idle checkpoint can contain next steps for a future user turn and
        # historical task references. Neither constitutes an active assignment.
        # Preserve legacy safe_to_rotate semantics when there is no live plan
        # or known unfinished task. Current-turn plans still default to continue.
        from task_runtime import read_record, RUNNING
        outstanding=RUNNING | {'awaiting_lead_review','lead_implementing','lead_takeover_required',
                               'needs_lead_decision','blocked','checks_failed','changes_requested'}
        unfinished=any(read_record(project_state(root)/'runs'/tid/'state.json').get('stage') in outstanding for tid in task_ids)
        continue_work=bool(plan_ids or unfinished or (next_steps and not safe_to_rotate))
    eligible=bool(safe_to_rotate and roadmap['status']=='ready' and budget['status']=='rotate_at_safe_boundary' and fingerprint and not blockers and not continue_work)
    record={'provider':provider,'session_id':sid,'recorded_at':time.time(),'summary':summary.strip(),'decisions':decisions,'next_steps':next_steps,'task_ids':task_ids,'plan_ids':plan_ids,'last_user_fingerprint':fingerprint,'rotation_eligible':eligible,'continue_work':continue_work,'stop_reason':stop_reason,
            'roadmap_sha256':roadmap.get('sha256') if roadmap['status']=='ready' else None,
            'next_milestone_id':roadmap.get('next_milestone_id') if roadmap['status']=='ready' else None,
            'roadmap_paths':roadmap.get('paths') if roadmap['status']=='ready' else None}
    put(local/'lead-checkpoint.json',record)
    reason = ('Project context not approved: ' + roadmap.get('reason','Select authoritative project documents')) if roadmap['status']!='ready' else None
    return {'status':'ready','rotation_eligible':eligible,'context_tokens':budget.get('context_tokens'),'blocking_tasks':blockers,'roadmap':roadmap,'rotation_blocked_reason':reason,
            'note':'Checkpoint saved; this is not a completion or pause decision. Continue the authorized plan, using provider-native compaction when available. Idle rotation requires approved context, matching SHA256 and no newer user message or active work.'}


def maybe_rotate_lead(root,store,provider):
    """Called under the idle Lead role lease; preserve the native transcript."""
    from task_runtime import read_record
    from orchestration import put
    path=project_state(root)/'local/lead-checkpoint.json';record=read_record(path)
    existing=store.get(provider,'reviewer')
    if not existing or not record.get('rotation_eligible') or record.get('provider')!=provider or record.get('session_id')!=existing['id']:return False
    if _rotation_blockers(root):return False
    # A checkpoint is not sufficient if future milestones or approved decisions
    # have disappeared, become invalid, or changed since checkpoint recording.
    import project_continuity as continuity
    roadmap=continuity.status(root)
    if (roadmap.get('status')!='ready' or not record.get('roadmap_sha256') or
            roadmap['sha256']!=record['roadmap_sha256'] or
            roadmap.get('next_milestone_id')!=record.get('next_milestone_id') or
            (record.get('roadmap_paths') is not None and roadmap.get('paths')!=record['roadmap_paths'])):return False
    fingerprint=last_user_fingerprint(provider,existing['id'])
    if not fingerprint or fingerprint!=record.get('last_user_fingerprint'):return False
    # Persist state before archiving; if interrupted, the previous transcript and
    # checkpoint remain available and the next launch is still recoverable.
    store.rotate(provider,'reviewer')
    put(path,{**record,'rotation_eligible':False,'rotated_at':time.time()})
    return True


def latest_context(provider,sid,*,path=None):
    """Cheap Stop-hook probe: scan a bounded transcript tail, not the lifetime log."""
    path=Path(path) if path is not None else _native_file(provider,sid)
    if path is None:return None
    try:
        with path.open('rb') as stream:
            size=path.stat().st_size;start=max(0,size-4*1024*1024);stream.seek(start)
            data=stream.read()
    except OSError:return None
    lines=data.splitlines()
    if start:lines=lines[1:]
    for line in reversed(lines):
        try:event=json.loads(line)
        except (ValueError,UnicodeError):continue
        if not isinstance(event,dict):continue
        if provider=='claude' and event.get('type')=='assistant':
            message=event.get('message') or {};usage=message.get('usage') or {}
            if isinstance(usage,dict) and all(type(usage.get(k,0)) is int for k in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens')) and any(k in usage for k in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens')):
                return sum(usage.get(k,0) for k in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens'))
        if provider=='codex' and event.get('type')=='event_msg':
            payload=event.get('payload') or {};info=payload.get('info') or {};last=info.get('last_token_usage') or {}
            if payload.get('type')=='token_count' and type(last.get('input_tokens')) is int:return last['input_tokens']
    return None


def checkpoint_context(root,provider,sid):
    """Restore only the current turn's durable continuation, never old consent."""
    from task_runtime import read_record
    row=read_record(project_state(root)/'local/lead-checkpoint.json')
    if not row and project_state(root).name=='.gaston':
        legacy=Path(root)/'.agentic/local/lead-checkpoint.json'
        if not legacy.is_symlink() and not legacy.parent.is_symlink():row=read_record(legacy)
    if row.get('provider')!=provider or row.get('session_id')!=sid:
        return ''
    fingerprint=last_user_fingerprint(provider,sid)
    if not fingerprint or fingerprint!=row.get('last_user_fingerprint'):
        return ''
    payload={key:row.get(key) for key in ('summary','decisions','next_steps','task_ids','plan_ids','continue_work','stop_reason')}
    return ('Durable checkpoint (reconcile current state and developer intent; not new authority). '
            + ('Respect the recorded pause/blocker; do not resume without resolving it or a newer developer instruction. ' if row.get('continue_work') is False and row.get('stop_reason') else 'If authorized work remains and no genuine blocker/user pause exists, continue now. ')
            +json.dumps(payload,ensure_ascii=False))[:12000]
