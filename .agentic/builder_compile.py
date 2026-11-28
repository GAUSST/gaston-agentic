"""Sandbox-local compile-only Java preflight; never independent acceptance evidence."""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import backend_runner


def projects_for_paths(root, paths):
    targets=backend_runner.discover(root)
    selected=set()
    for name in paths:
        path=Path(name)
        if path.suffix not in ('.java','.kt','.kts','.groovy','.scala') and path.name not in ('pom.xml','build.gradle','settings.gradle','gradle.properties'):continue
        matches=[x['path'] for x in targets if x['path']=='.' or name.startswith(x['path']+'/') or name==x['path']]
        selected.update(matches)
    return sorted(selected)


def _attempt_summary(result):
    return {key:result.get(key) for key in ('status','reason_code','runner','exit_code','elapsed_seconds','reason',
        'network_allowed','network_usage','network_attempted','network_scope') if result.get(key) is not None}


def check(root, project, *, allow_network=False, approval_quote=''):
    """Compile offline first; retry once online only with explicit authorization."""
    offline_result=backend_runner.run(root,project,goal='test-compile',offline=True,allow_network=False,timeout=300)
    offline_result={**offline_result,'resolution_mode':'offline_cache','network_authorized':False}
    if offline_result.get('status')!='blocked' or offline_result.get('reason_code')!='DEPENDENCY_RESOLUTION_UNAVAILABLE':
        return offline_result
    if not allow_network:
        return {**offline_result,
                'network_authorization_required':True,
                'offline_blocker':offline_result.get('reason'),
                'reason':'Offline dependency cache is incomplete. Ask the Lead to authorize a one-time dependency fetch.'}
    if not isinstance(approval_quote,str) or not approval_quote.strip():
        return {**offline_result,'network_authorization_required':True,
                'reason':'Online dependency resolution requires a verbatim developer approval quote.'}
    online_result=backend_runner.run(root,project,goal='test-compile',offline=False,allow_network=True,timeout=300)
    online_result={**online_result,
                   'resolution_mode':'online_fallback',
                   'network_authorized':True,
                   'network_approval_quote':approval_quote,
                   'offline_attempt':_attempt_summary(offline_result)}
    if online_result.get('status')=='blocked' and online_result.get('reason_code')=='DEPENDENCY_RESOLUTION_UNAVAILABLE':
        online_result['reason']='Authorized online dependency resolution was attempted but remains unavailable; the Builder sandbox or configured repositories may not permit access.'
    return online_result


def active_attempt(root):
    from orchestration import read_record
    tid=os.environ.get('GASTON_BUILDER_TASK_ID','');attempt=os.environ.get('GASTON_BUILDER_ATTEMPT','')
    if not re.fullmatch(r'[a-z0-9-]{8,120}',tid) or not re.fullmatch(r'[0-9a-f]{32}',attempt):return None
    folder=project_state(root)/'runs'/tid
    if folder.is_symlink() or read_record(folder/'worker.json').get('attempt')!=attempt or read_record(folder/'state.json').get('stage')!='implementing':return None
    return folder,attempt


def material_fingerprint(root,project):
    # Bind a compile to all inputs in the detected Git workspace (including
    # untracked source). No Git baseline means no trusted freshness claim.
    import workspace_git
    from orchestration import read_record
    return workspace_git.fingerprint(root,read_record(project_state(root)/'config.json').get('project'))


def receipt_path(folder,attempt,project):
    key=hashlib.sha256(project.encode()).hexdigest()[:24]
    return folder/'compile-preflight'/f'{attempt}-{key}.json'


def blocking_receipts(checks):
    """Only a current, explicit pass can satisfy the Builder compile gate."""
    if not isinstance(checks,list):
        return [{'project':'unknown','status':'not_run'}]
    return [
        {'project':item.get('project','unknown') if isinstance(item,dict) else 'unknown',
         'status':(item.get('status') or 'not_run') if isinstance(item,dict) else 'not_run'}
        for item in checks
        if not isinstance(item,dict) or item.get('status')!='pass'
    ]


def receipts(root,folder,attempt,projects):
    from orchestration import read_record
    if not re.fullmatch(r'[0-9a-f]{32}',attempt):return [{'project':p,'status':'not_run'} for p in projects]
    result=[]
    current=material_fingerprint(root,None)
    for project in projects:
        record=read_record(receipt_path(folder,attempt,project));check=record.get('result',{})
        status=check.get('status') if record.get('attempt')==attempt and record.get('project')==project else None
        if status=='pass' and (not current or current!=record.get('fingerprint')):status='stale'
        result.append({'project':project,'status':status or 'not_run',
                       'reason':check.get('reason'),
                       'reason_code':check.get('reason_code'),
                       'offline_blocker':check.get('offline_blocker'),
                       'cache_path':check.get('cache_path'),
                       'runner':check.get('runner'),
                       'network_authorization_required':check.get('network_authorization_required',False),
                       'log_path':check.get('log_path'),
                       'note':'Builder-side compile receipt only; independent Lead tests still required.'})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project',help='discovered workspace-relative Java build root')
    args=parser.parse_args();root=project_root()
    active=active_attempt(root)
    if not active:
        print(json.dumps({'status':'blocked','reason':'Compile preflight is available inside the active delegated Builder attempt only.'}));return 1
    folder,attempt=active
    from orchestration import read_record,overlaps,put
    scopes=read_record(folder/'task.json').get('write_scope') or []
    if args.project not in {x['path'] for x in backend_runner.discover(root)} or not (args.project=='.' or overlaps(scopes,[args.project])):
        print(json.dumps({'status':'blocked','reason':'Select a discovered Java project intersecting task scope.'}));return 1
    before=material_fingerprint(root,args.project)
    result=check(root,args.project,allow_network=False)
    after=material_fingerprint(root,args.project)
    put(receipt_path(folder,attempt,args.project),{'attempt':attempt,'project':args.project,'recorded_at':time.time(),'fingerprint':before if before and before==after else None,'result':result})
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result.get('status')=='pass' else 1

if __name__=='__main__':raise SystemExit(main())
