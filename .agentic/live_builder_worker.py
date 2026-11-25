#!/usr/bin/env python3
"""Private, on-demand implementation worker invoked ONLY by the Lead MCP.

The Lead stays interactive; this process uses a task-scoped Builder session,
records implementation evidence and returns control to the SAME Lead for review.
No second reviewer model request and no reliance on session-close hooks.
"""
from __future__ import annotations
import sys
import os
import json
import time
import re
from pathlib import Path
import eng
import context_brief
import orchestration as orch
import task_runtime


def changed_paths(root, rd, previous, reported=(), *, after=None):
    """Keep the whole task scope across revisions; include observed Git edits."""
    paths=set(previous.get('changed_files',[]) or [])
    for value in (reported or []):
        try:paths.add(orch.relative_path(root,value))
        except (ValueError,TypeError):continue
    try:
        before=eng.load_json(rd/'preexisting-changes.json').get('files',{})
        if after is None:after=eng.workspace_git.existing_changes(root,eng.load_config().get('project'))
        observed={p for p in set(before)|set(after) if before.get(p)!=after.get(p)}
        task=orch.read_record(rd/'task.json');scope=task.get('write_scope') or []
        # Other Lead work may proceed in parallel. Do not attribute disjoint
        # observed Git changes to this Builder; preserve scope diagnostics.
        if scope:
            orch.put(rd/'concurrent-observed-paths.json',{'outside_builder_scope':sorted(p for p in observed if not orch.within(p,scope)), 'note':'Concurrent edits are not automatically attributable to Builder'})
            observed={p for p in observed if orch.within(p,scope)}
        paths.update(observed)
    except (Exception,SystemExit):pass
    paths.update(orch.lead_paths(rd))
    normalized=set()
    for value in paths:
        try:normalized.add(orch.relative_path(root,value))
        except (ValueError,TypeError):continue
    return sorted(normalized)


def interrupted(task_id, exc):
    rd=eng.run_dir(task_id)
    prior=eng.load_json(rd/'build.json') if (rd/'build.json').is_file() else {}
    log=''
    logs=sorted(rd.glob('build*.log'),key=lambda p:p.stat().st_mtime)
    if logs:
        with logs[-1].open('rb') as fh:
            fh.seek(max(0,logs[-1].stat().st_size-32000));log=fh.read().decode('utf8','replace')
    session_blocked = 'native chat is active' in str(exc)
    signal_exit=isinstance(exc,SystemExit) and isinstance(exc.code,int) and exc.code>128
    quota=not signal_exit and not session_blocked and bool(re.search(r'usage limit|rate.limit|insufficient.quota|usage_limit_reached|quota exceeded|out of credits',log,re.I))
    reason='Provider usage/quota limit reached; partial edits preserved; retry this task after access is restored' if quota else task_runtime.interruption_reason(exc)
    built={**prior,'status':'blocked','changed_files':changed_paths(eng.ROOT,rd,prior),'questions':[],
           'summary':reason,'interrupted':True}
    eng.write_json(rd/'build.json',orch.merge_lead_paths(rd,built))
    revision=eng.load_json(rd/'revision-request.json').get('number',0) if (rd/'revision-request.json').is_file() else 0
    blocker='provider_quota' if quota else 'worker_interrupted'
    if session_blocked:blocker='session_start_blocked'
    import recovery_consent
    eng.save_state(rd,'blocked',reason=reason,blocker=blocker,revision=revision,resumable=True,
                   **({'provider_retry_hint':recovery_consent.retry_hint(log),'schedule_consent_required':True} if quota else {}))
    if not quota:
        try:
            import runtime_recovery
            runtime_recovery.record_internal_error(eng.ROOT,'builder_worker',exc,task_id=task_id)
        except (OSError,ValueError,RuntimeError,TypeError,KeyError):
            pass


def process(task_id):
    eng.require_setup_complete()
    cfg=eng.load_config(); prefs=cfg.get('preferences',{})
    builder, reviewer=prefs.get('builder'),prefs.get('reviewer')
    if builder not in ('codex','claude') or reviewer not in ('codex','claude'):
        raise RuntimeError('Roles not configured')
    if not eng.SESSIONS.get(reviewer,'reviewer'):
        raise RuntimeError('No registered Lead')
    task=eng.load_task(task_id)
    rd=eng.run_dir(task_id)
    # Dirty trees are ordinary development state, not a reason to block a task.
    # Do not stage, commit, stash or push. The user owns every Git-write decision;
    # this is also enforced by eng.build_prompt for the Builder.
    # Record existing changes so the Builder can preserve unrelated work and
    # the Lead can see overlaps. No git add/stash/commit is performed here.
    prior_files = eng.workspace_git.existing_changes(eng.ROOT, cfg.get('project'))
    # Keep the initial comparison baseline across crashes and revisions, so
    # edits made before a worker died remain attributable to the same task.
    if not (rd/'preexisting-changes.json').is_file():
        eng.write_json(rd/'preexisting-changes.json', {'files': prior_files})
    # Engineering Lead owns all verification and decisions. Builder implements using native tools.
    # A subsequent Lead-requested correction reuses this task and the same
    # task-scoped Builder session when within budget, without autonomous repair cycles.
    revision_file=rd/'revision-request.json'
    revision=eng.load_json(revision_file) if revision_file.is_file() else None
    plan={'status':'ready','steps':['Make the requested implementation edits'],
          'risk_flags':[],'verification_areas':[],
          'verification_rationale':[],'verification_checks':[]}
    eng.save_state(rd,'implementing',builder=builder,
                   revision=revision.get('number') if revision else 0)
    # One bounded, deterministic context handoff; no extra model request.
    cfg=eng.refresh_workspace_checks(cfg)
    brief=context_brief.make(task,cfg,prior_files)
    eng.write_json(rd/'context-brief.json',brief)
    previous=eng.load_json(rd/'build.json') if (rd/'build.json').is_file() else {}
    if revision:
        # Session may be fresh at a context boundary; reference complete immutable original task and
        # specs on disk. No second copy of original goal/criteria or old logs.
        prior_paths=previous.get('changed_files',[])
        task_prompt=('ENGINEERING LEAD REQUESTED A TARGETED CORRECTION IN THE SAME TASK.\n'
                     + 'Task ID: '+task_id+'\nOriginal complete task: '+str((rd/'task.json').relative_to(eng.ROOT))
                     + '\nPrior structured implementation result: '+str((rd/'build.json').relative_to(eng.ROOT))
                     + '\nRead the task/specification snapshots when needed to preserve all original requirements. '
                       'Only make the edits requested by the Lead. For Java changes run the compile-only helper; do not run tests or start additional repair work. '
                       'Inspect the relevant code, self-review wiring, readability and contract consistency; explain non-obvious invariants near difficult logic, before returning JSON. '
                     + '\nWrite scope: '+json.dumps(task.get('write_scope',[]),ensure_ascii=False)
                     + '\nPreviously changed paths: '+json.dumps(prior_paths,ensure_ascii=False)
                     + '\nCurrent correction and observed failure: '+revision['feedback'])
        clarifications=orch.read_record(rd/'clarifications.json')
        if clarifications:task_prompt+='\nRecorded Lead clarification answers: '+json.dumps(clarifications.get('answers',{}),ensure_ascii=False)
        if revision.get('root_cause'):task_prompt+='\nRoot cause: '+revision['root_cause']
        if revision.get('reproduction'):task_prompt+='\nReproduction/evidence: '+revision['reproduction']
        if revision.get('specifications'):
            task_prompt+='\nCORRECTION SPECIFICATIONS: Read each snapshot_path completely.\n'+json.dumps(revision['specifications'],ensure_ascii=False)
    else:
        task_prompt=eng.build_prompt(task,plan)+'\nTASK-SPECIFIC WORKSPACE MAP:\n'+context_brief.render(brief)
        if previous.get('interrupted'):
            task_prompt+='\nThis is the same interrupted task. Prior partial edits remain in the workspace. Inspect '+str((rd/'build.json').relative_to(eng.ROOT))+' and actual scoped changes before continuing; do not redo completed implementation.'
        if task.get('specifications'):
            task_prompt += ('\nTASK SPECIFICATIONS: Read each snapshot_path in TASK completely before implementation. '
                            'These task-local copies preserve the exact delegated documents; original_path is provenance.\n')
    prompt_bytes=len(task_prompt.encode('utf8'))
    attempt=orch.read_record(rd/'worker.json').get('attempt') or 'local'
    orch.put(rd/f"handoff-{revision['number'] if revision else 0}-{attempt}.json",{'prompt_bytes':prompt_bytes,'mode':'delta' if revision else 'initial','created_at':time.time()})
    build_file=rd/(f"build-revision-{revision['number']}.json" if revision else 'build.json')
    # Scope helper and native edit guard to this exact active worker attempt.
    keys={'GASTON_BUILDER_TASK_ID':task_id,'GASTON_BUILDER_ATTEMPT':attempt}
    old_env={key:os.environ.get(key) for key in keys};os.environ.update(keys)
    try:built=eng.provider_structured(builder,task_prompt,eng.BUILD_SCHEMA,build_file,cfg,'build')
    finally:
        for key,value in old_env.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
    reported=built.get('changed_files',[]) or [];scope=task.get('write_scope') or []
    outside=sorted(p for p in reported if isinstance(p,str) and scope and not orch.within(p,scope) and p not in orch.lead_paths(rd))
    try:after=eng.workspace_git.existing_changes(eng.ROOT,cfg.get('project'))
    except (OSError,ValueError):after=None
    built={**built,'revision_changed_files':reported,'changed_files':changed_paths(eng.ROOT,rd,previous,reported,after=after)}
    built=orch.merge_lead_paths(rd,built)
    # Read sandbox-local compile preflight receipts. Never execute newly written
    # project build plugins on the host before the Lead has reviewed them.
    if built.get('status')=='done' and not built.get('questions'):
        import builder_compile
        projects=builder_compile.projects_for_paths(eng.ROOT,built['changed_files'])
        if projects:
            checks=builder_compile.receipts(eng.ROOT,rd,attempt,projects)
            built['compile_preflight']=checks
            blockers=builder_compile.blocking_receipts(checks)
            if blockers:
                by_project={item['project']:item for item in checks}
                detail=', '.join(
                    f"{x['project']} ({x['status']}; "
                    f"reason={by_project.get(x['project'],{}).get('reason_code') or by_project.get(x['project'],{}).get('reason') or 'not_run'})"
                    for x in blockers)
                built.update(status='blocked',questions=[
                    'Compile preflight requires a fresh pass; blocked receipts: '+detail+'. '
                    'Inspect compile_preflight diagnostics. A later Lead test does not retroactively change the Builder receipt.'])
    observation=orch.scope_observation(eng.ROOT,rd,prior_files,after,scope,outside)
    if observation['pending_paths']:
        prior_questions=list(built.get('questions') or [])
        scope_question='Scope reconciliation needed: '+', '.join(observation['pending_paths'])
        built.update(scope_review_required=observation['pending_paths'],scope_prior_status=built['status'],scope_prior_questions=prior_questions,scope_question=scope_question,out_of_scope_reported=outside,status='blocked',questions=prior_questions+[scope_question])
    # Retain original revision output and cumulative scope for risk planning.
    eng.write_json(rd/'build.json',built)
    overlaps=eng.workspace_git.changed_existing_files(eng.ROOT,prior_files)
    eng.write_json(rd/'existing-change-overlaps.json',{'changed_by_builder':[p for p in overlaps if not scope or orch.within(p,scope)],'concurrent_outside_scope':[p for p in overlaps if scope and not orch.within(p,scope)]})
    if built.get('status')=='done' and not built.get('questions'):
        eng.save_state(rd,'awaiting_lead_review',
                       revision=revision.get('number') if revision else 0,
                       verification_owner='reviewer')
    else:
        eng.save_state(rd,'needs_lead_decision',
                       questions=built.get('questions',[]),
                       clarification_only=bool(built.get('questions') and not built.get('revision_changed_files') and not built.get('changed_files')), 
                       revision=revision.get('number') if revision else 0,
                       verification_owner='reviewer')
    # No eng.verify(), no build-worker tests, no automatic repair. The Lead
    # independently runs relevant tests and directs any further edits.
    eng.write_summary(rd)


if __name__=='__main__':
    if len(sys.argv)!=3 or not sys.argv[1].replace('-','').isalnum() or not re.fullmatch(r'[0-9a-f]{32}',sys.argv[2]):
        raise SystemExit('Expected valid internal task and launch IDs')
    import runtime_recovery
    if not runtime_recovery.claim_worker(eng.ROOT,eng.run_dir(sys.argv[1]),sys.argv[2]):
        raise SystemExit('Superseded Builder launch; no provider invoked')
    task_runtime.install_termination_handlers()
    try:process(sys.argv[1])
    except (SystemExit,Exception) as exc:
        interrupted(sys.argv[1],exc)
        raise
    finally:
        # Independent worker-exit timestamp survives subsequent Lead review
        # state transitions; the Builder's own duration is never inflated.
        folder=eng.run_dir(sys.argv[1]);state=orch.read_record(folder/'state.json')
        finished={'finished_at':time.time(),'stage':state.get('stage'),'blocker':state.get('blocker'),'revision':state.get('revision',0)}
        eng.write_json(folder/'worker-finished.json',finished)
        orch.put(folder/f"attempt-{finished['revision']}-{sys.argv[2]}.json",finished)
        orch.put(folder/'worker-attempts'/f"{sys.argv[2]}-finished.json",finished)
