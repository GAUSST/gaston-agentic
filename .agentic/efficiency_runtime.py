"""Bounded deterministic telemetry at natural engineering boundaries."""
from __future__ import annotations
from project_layout import project_state, project_state_name, project_root
from pathlib import Path
import time
import token_intelligence as ti
import orchestration as orch


def lead_boundary(root,provider=None,sid=None):
    if provider is None or sid is None:
        from session_store import Store
        store=Store(root);provider=store._configured_roles()[1];entry=store.get(provider,'reviewer')
        if not entry:return None
        sid=entry['id']
    budget=ti.lead_context(root,provider,sid)
    folder=project_state(root)/'local'
    import serena_tools
    snapshot={'provider':provider,'session_id':sid,'recorded_at':time.time(),**budget,'serena':serena_tools.usage_summary(root)}
    orch.put(folder/'lead-efficiency-latest.json',snapshot)
    if isinstance(budget.get('model_calls'),int):
        history_path=folder/'lead-efficiency-history.json'
        history=orch.read_record(history_path).get('snapshots',[])
        if not isinstance(history,list):history=[]
        if not history or any(history[-1].get(k)!=snapshot.get(k) for k in ('provider','session_id','model_calls')):
            orch.put(history_path,{'snapshots':(history+[snapshot])[-500:]})
    if budget.get('status')!='rotate_at_safe_boundary' or ti._rotation_blockers(root):return None
    import project_continuity as continuity
    roadmap=continuity.status(root)
    if roadmap['status']!='ready':
        return {'context_tokens':budget.get('context_tokens'),
                'action':'Do not rotate. Ask the developer which existing project documents are authoritative, register them with register_project_context_sources, or use a previously approved structured roadmap. Preserve decisions with checkpoint_lead_context. Never invent a plan.'}
    fingerprint=ti.last_user_fingerprint(provider,sid)
    if not fingerprint:return None
    checkpoint=orch.read_record(folder/'lead-checkpoint.json')
    if checkpoint.get('session_id')==sid and checkpoint.get('last_user_fingerprint')==fingerprint and checkpoint.get('rotation_eligible'):return None
    return {'context_tokens':budget.get('context_tokens'),'action':'Preserve factual outstanding user intent and state with checkpoint_lead_context now. For unfinished authorized work, checkpoint and continue immediately; safe_to_rotate is for completed or explicitly stopped work only. Prefer provider-native automatic compaction. Never stop solely because the context is large or a checkpoint was saved. No second Lead or forced interruption.'}


def stop_advisory(root,provider,sid):
    # Claude Stop can request one follow-up, only once for a given user turn.
    # Never parse a session on every tool call or spawn a model ourselves.
    context=ti.latest_context(provider,sid)
    if context is None or context<ti.LEAD_CONTEXT_ROTATE:return None
    advisory=lead_boundary(root,provider,sid)
    if not advisory:return None
    fingerprint=ti.last_user_fingerprint(provider,sid)
    path=project_state(root)/'local/lead-efficiency-notice.json'
    prior=orch.read_record(path)
    if prior.get('session_id')==sid and prior.get('fingerprint')==fingerprint:return None
    orch.put(path,{'session_id':sid,'fingerprint':fingerprint,'recorded_at':time.time()})
    return advisory['action']


def bound_plans(root,provider,sid,fingerprint):
    row=orch.read_record(project_state(root)/'local/lead-current-turn.json')
    if not fingerprint or (row.get('provider'),row.get('session_id'),row.get('fingerprint'))!=(provider,sid,fingerprint):
        return []
    return [p for p in row.get('plan_ids',[]) if isinstance(p,str)]


def bind_plan(root,provider,sid,plan_id):
    """Bind a successfully started/modified plan to the current native user turn.

    Reading old plans is not authorization. Do not bind historical plans merely
    because they exist on disk, or infer work from roadmap milestones.
    """
    fingerprint=ti.last_user_fingerprint(provider,sid)
    if not fingerprint:return
    orch.get_plan(root,plan_id)
    ids=list(dict.fromkeys([*bound_plans(root,provider,sid,fingerprint),plan_id]))
    orch.put(project_state(root)/'local/lead-current-turn.json',{
        'provider':provider,'session_id':sid,'fingerprint':fingerprint,'plan_ids':ids})


def _actions(root,plans,task_ids):
    """Return actual outstanding actions, not context-budget or rotation status."""
    actions=set()
    seen=set()
    # An active verification has its own native completion notification. Do
    # not manufacture another invocation while it owns a reservation.
    from verification_coordination import active
    verification=active(root)
    def task_action(tid):
        if not tid or tid in seen:return
        seen.add(tid)
        if not isinstance(tid,str) or not __import__('re').fullmatch(r'[a-z0-9-]{8,120}',tid):return
        folder=project_state(root)/'runs'/tid
        if folder.is_symlink():return
        state=orch.read_record(folder/'state.json')
        stage=state.get('stage')
        if stage in ('slice_reviewed','ready_for_human_release_review') and (stage=='slice_reviewed' or state.get('lead_verified')):return
        if stage=='awaiting_lead_review' and not verification:
            actions.add(('review',tid))
        elif stage in ('lead_implementing','lead_takeover_required') and not verification:
            actions.add(('finish-lead-edit',tid))
        elif stage=='needs_lead_decision' and not verification:
            # The Lead must distinguish a code/scope correction from a real
            # decision requiring the developer, not auto-accept or blindly retry.
            actions.add(('resolve-or-report-blocker',tid))
        elif stage in ('blocked','checks_failed','changes_requested') and not verification:
            actions.add(('resolve-or-report-blocker',tid))
    for pid in plans:
        try:plan=orch.plan_status(root,pid)
        except (ValueError,OSError,KeyError):continue
        for item in plan.get('items',[]):
            if item.get('status')=='done':continue
            if item.get('executable') and item.get('status')=='pending' and not item.get('task_id'):
                actions.add(('execute',pid+'/'+item['id']))
            else:task_action(item.get('task_id'))
        if plan.get('all_items_done'):
            checkpoint=plan.get('checkpoint_task_id')
            if checkpoint:task_action(checkpoint)
            elif any(x.get('kind')=='implement' for x in plan.get('items',[])) and not verification and not plan.get('active_builder_tasks'):
                actions.add(('integrated-acceptance',pid))
    for tid in task_ids:task_action(tid)
    return sorted(actions)


def continuation_reason(root,provider,sid):
    """Allow a stop retry on each distinct actionable progress boundary.

    The same action set is nudged only once. A real new user message, explicit
    recorded pause/blocker or native in-flight wait is respected. No provider
    process is spawned and no extra authority is granted by a checkpoint.
    """
    fingerprint=ti.last_user_fingerprint(provider,sid)
    if not fingerprint:return None
    local=project_state(root)/'local'
    row=orch.read_record(local/'lead-checkpoint.json')
    current=(row.get('provider'),row.get('session_id'),row.get('last_user_fingerprint'))==(provider,sid,fingerprint)
    if current and row.get('continue_work') is False and row.get('stop_reason'):
        return None
    plans=bound_plans(root,provider,sid,fingerprint)
    tasks=[]
    if current and row.get('continue_work') is not False:
        plans=list(dict.fromkeys([*plans,*row.get('plan_ids',[])]))
        tasks=row.get('task_ids',[])
    if not plans and not tasks:return None
    actions=_actions(root,plans,tasks)
    if not actions:return None
    path=local/'lead-continuation-notice.json'
    prior=orch.read_record(path)
    key=[list(x) for x in actions]
    same=(prior.get('session_id'),prior.get('fingerprint'))==(sid,fingerprint)
    history=prior.get('action_history',[]) if same else []
    if key in history:return None
    # A hard upper bound prevents a broken or malicious model from endlessly
    # inventing new action sets. Normal work may continue without any Stop hook.
    if len(history)>=32:return None
    orch.put(path,{'session_id':sid,'fingerprint':fingerprint,'action_history':history+[key],'recorded_at':time.time()})
    return ('Gaston: the current developer-authorized plan is unfinished. Next actions: '
            +'; '.join(kind+' '+target for kind,target in actions[:8])+
            '. Continue execution now; use native automatic compaction if needed. '
            'A milestone, large context or saved checkpoint is not completion. '
            'If genuinely blocked or explicitly paused by the developer, record checkpoint_lead_context with continue_work=false and a specific stop_reason. '
            'Do not retry a failed gate blindly, bypass authorization, or request a wake-up message.')


def bound_plan_context(root,provider,sid):
    fingerprint=ti.last_user_fingerprint(provider,sid)
    ids=bound_plans(root,provider,sid,fingerprint)
    if not ids:return ''
    return ('Current-turn authorized plan IDs: '+', '.join(ids[:30])+
            '. Inspect work_plan_status and continue outstanding work unless explicitly paused or blocked. '
            'Do not expand this scope from unrelated roadmap items.')
