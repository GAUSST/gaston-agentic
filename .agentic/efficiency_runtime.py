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
    return {'context_tokens':budget.get('context_tokens'),'action':'Preserve factual outstanding user intent and state with checkpoint_lead_context now. Mark safe_to_rotate only if complete, then use native compaction or close and reopen ./dev. No second Lead or forced interruption.'}


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
