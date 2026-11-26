"""Evidence-linked acceptance coverage across project components.

A unit test pass is not an API/mobile/web acceptance pass. Each check must
have real trusted runner output recorded by Gaston's restricted verifier.
"""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode
from pathlib import Path
import json
import time
import secrets
import hashlib

GATE_TYPES={'backend':'backend_verify','api':'api_integration',
            'web':'web_e2e','web_verify':'web_verify','mobile':'mobile_e2e','flutter_tests':'flutter_tests','flutter_analyze':'flutter_analyze'}


def read(folder: Path) -> list[dict]:
    path=folder/'acceptance-evidence.json'
    try:record=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return []
    return record if isinstance(record,list) else []


def review_only_document(name):
    """Classify ordinary prose only. Specs/contracts and build inputs stay material."""
    p=Path(name)
    if p.is_absolute() or '..' in p.parts or 'docs' not in p.parts[:-1] or p.suffix.lower() not in ('.md','.rst'):return False
    return not any(word in p.stem.lower() for word in ('spec','requirement','contract','openapi','swagger','acceptance','policy','checkpoint','schema'))


def specification_documents(folder):
    from task_runtime import read_record
    import re
    task=read_record(folder/'task.json');tasks=[task]
    for tid in task.get('source_tasks',[]):
        if isinstance(tid,str) and re.fullmatch(r'[a-z0-9-]{8,120}',tid):
            source=folder.parent/tid
            if not source.is_symlink():tasks.append(read_record(source/'task.json'))
    return {x.get('original_path') for t in tasks for x in t.get('specifications',[]) if isinstance(x,dict)}


def declared_review_documents(folder):
    """Exact paths attested by Lead as unrelated to executable behavior."""
    from task_runtime import read_record
    task=read_record(folder/'task.json');specs=specification_documents(folder)
    return frozenset(x for x in task.get('review_only_documents',[]) if isinstance(x,str) and review_only_document(x) and x not in specs)


def revision_fingerprint(folder: Path, *, review_only=False, input_hashes=None) -> str | None:
    """Bind evidence to actual task source/deletions, not just a running task ID."""
    if folder.parent.name!='runs' or folder.parent.parent.name!=project_state_name():return None
    root=folder.parent.parent.parent.resolve()
    try:build=json.loads((folder/'build.json').read_text(encoding='utf8'))
    except (OSError,ValueError):return None
    paths=build.get('changed_files',[])
    if not isinstance(paths,list):return None
    # Lead-authored tests and takeover edits invalidate earlier evidence too.
    from orchestration import lead_paths
    paths=list(set(paths)|lead_paths(folder))
    prose=declared_review_documents(folder) if review_only else frozenset()
    sha=hashlib.sha256(b'gaston-runtime-revision-v1\0' if review_only else b'')
    if review_only:
        # Changes in classification never retroactively make old evidence reusable.
        sha.update(json.dumps(sorted(prose)).encode()+b'\0')
    for name in sorted({p.replace('\\','/') for p in paths if isinstance(p,str)}):
        part=hashlib.sha256()
        def add(data):
            sha.update(data)
            if input_hashes is not None:part.update(data)
        path=root/name
        if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(root):return None
        if path.is_symlink():return None
        if review_only and name in prose and not path.is_dir():continue
        add(name.encode('utf8'));add(b'\0')
        if not path.exists():add(b'<deleted>')
        elif path.is_file():
            try:
                with path.open('rb') as f:
                    for chunk in iter(lambda:f.read(1024*1024),b''):add(chunk)
            except OSError:return None
        elif path.is_dir():
            # Builder reports may include new directories in changed_files.
            # Bind the review to *every* contained file so adding a new source
            # invalidates evidence just as editing an existing file does.
            add(b'<directory>\0')
            try:
                for child in sorted(path.rglob('*')):
                    if child.is_symlink() or not child.resolve().is_relative_to(root):return None
                    child_name=child.relative_to(root).as_posix()
                    add(child_name.encode('utf8'));add(b'\0')
                    if child.is_dir():add(b'<directory>')
                    elif child.is_file():
                        with child.open('rb') as f:
                            for chunk in iter(lambda:f.read(1024*1024),b''):add(chunk)
                    else:return None
                    add(b'\0')
            except OSError:return None
        else:return None
        add(b'\0')
        if input_hashes is not None:input_hashes[name]=part.hexdigest()
    return sha.hexdigest()


def record(folder: Path, kind: str, outcome: dict, *, parameters: dict | None=None, reused_from: dict | None=None) -> dict:
    if kind not in GATE_TYPES:raise ValueError('Unsupported acceptance type')
    if not isinstance(outcome,dict) or outcome.get('status') not in ('pass','fail','blocked'):
        raise ValueError('Real verification-tool outcome required')
    inputs={}
    fingerprint=revision_fingerprint(folder,input_hashes=inputs)
    entry={'id':secrets.token_hex(8),'recorded_at':time.time(),
           'kind':kind,'gate':GATE_TYPES[kind],
           'result':outcome['status'],'parameters':parameters or {},
           'evidence':outcome,'revision_fingerprint':fingerprint,
           **({'revision_inputs':inputs} if fingerprint and len(inputs)<=512 else {}),
           **({'reused_from':reused_from} if reused_from else {}),
           **({'runtime_revision_fingerprint':revision_fingerprint(folder,review_only=True)} if kind=='backend' else {})}
    path=folder/'acceptance-evidence.json'
    existing=read(folder)
    existing.append(entry)
    path.write_text(json.dumps(existing,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    return entry


def changed_inputs(before, after):
    """Report changed explicit task paths; directories are aggregate hashes."""
    if not isinstance(before,dict) or not isinstance(after,dict):return []
    return sorted(p for p in set(before)|set(after) if before.get(p)!=after.get(p))[:40]


def expired_checks(folder, current_rows):
    """Explain stale gate evidence without reclassifying or accepting it."""
    current_ids={row.get('id') for row in current_rows}
    latest={}
    for row in read(folder):
        if isinstance(row,dict) and row.get('kind') in GATE_TYPES:
            latest[(row['kind'],str((row.get('parameters') or {}).get('project')))]=row
    stale=[row for row in latest.values() if row.get('id') not in current_ids]
    if not stale:return []
    from task_runtime import read_record
    worker=read_record(folder/'worker.json')
    finished=read_record(folder/'worker-finished.json').get('finished_at')
    boundary=max((float(x) for x in (worker.get('started_at'),finished) if isinstance(x,(int,float))),default=0)
    inputs={}
    fingerprint=revision_fingerprint(folder,input_hashes=inputs)
    out=[]
    for row in stale:
        detail={'kind':row['kind'],'check_id':row.get('id'),'project':(row.get('parameters') or {}).get('project')}
        stamp=row.get('recorded_at')
        if isinstance(stamp,(int,float)) and stamp<boundary:
            detail['reason']='newer_builder_attempt'
        elif fingerprint is None:
            detail['reason']='task_inputs_unverifiable'
        elif row.get('revision_fingerprint')!=fingerprint:
            detail['reason']='task_inputs_changed'
            if isinstance(row.get('revision_inputs'),dict):
                detail['changed_inputs']=changed_inputs(row['revision_inputs'],inputs)
        else:
            detail['reason']='verification_scope_changed_or_evidence_unverifiable'
        out.append(detail)
    return out[:20]


def summarize(plan: dict, evidence: list[dict]) -> dict:
    planned={c.get('name') for c in plan.get('selected_checks',[]) if isinstance(c,dict)}
    missing=set(plan.get('missing_gates',[]))
    latest={}
    checked=[c for c in plan.get('selected_checks',[]) if isinstance(c,dict)]
    for e in evidence:
        if not isinstance(e,dict) or e.get('gate') not in GATE_TYPES.values():continue
        gate=e['gate']
        # Multi-repository workspaces number discovered gate names. A pass for
        # backend A must never be counted as a pass for backend B.
        target=(e.get('parameters') or {}).get('project') or (e.get('evidence') or {}).get('project')
        if target:
            match=[c.get('name') for c in checked if
                   (c.get('name')==gate or str(c.get('name','')).startswith(gate+'__'))
                   and c.get('cwd','.')==target]
            if len(match)==1:gate=match[0]
            elif checked:continue
        elif any(c.get('name')==gate and c.get('cwd','.')!='.' for c in checked):
            continue
        if gate not in planned and planned:
            # Unknown execution evidence does not satisfy a different gate.
            continue
        if gate.startswith('api_integration') and plan.get('managed_api_gates') and gate in plan['managed_api_gates'] and e.get('result')=='pass' and not (e.get('evidence') or {}).get('managed_application'):
            continue
        if gate in plan.get('full_backend_gates',[]) and e.get('result')=='pass' and (e.get('parameters') or {}).get('test_filter') is not None:
            continue
        latest[gate]=e.get('result')
    unexecuted=sorted(name for name in planned if latest.get(name)!='pass')
    failed=sorted(name for name,status in latest.items() if status=='fail')
    blocked=sorted(name for name,status in latest.items() if status=='blocked')
    gaps=sorted(missing)
    status=('fail' if failed else 'incomplete' if (gaps or unexecuted or blocked or not planned)
            else 'pass')
    return {'status':status,'coverage':'complete' if status=='pass' else 'partial',
            'planned_gates':sorted(planned),'observed_gates':latest,
            'unverified_planned_gates':unexecuted,'missing_configured_gates':gaps,
            'failed_gates':failed,'blocked_gates':blocked,'advisory_unconfigured_gates':plan.get('advisory_gaps',[]),'full_backend_gates':plan.get('full_backend_gates',[]),
            'note':'Only real runner records count as passing checks. Other project components are not implicitly tested.'}


def current(folder: Path) -> list[dict]:
    """Ignore all evidence from earlier Builder attempts, even successful ones."""
    import task_runtime
    worker=task_runtime.read_record(folder/'worker.json')
    finished=task_runtime.read_record(folder/'worker-finished.json').get('finished_at')
    started=worker.get('started_at')
    boundary=max((float(x) for x in (started,finished) if isinstance(x,(int,float))),default=0)
    expected=revision_fingerprint(folder)
    if expected is None:return []
    runtime=revision_fingerprint(folder,review_only=True)
    rows=[];workspace=None;checked=False
    for e in read(folder):
        if not isinstance(e,dict) or not isinstance(e.get('recorded_at'),(int,float)) or e['recorded_at']<boundary:continue
        scoped=e.get('parameters',{}).get('verification_scope_fingerprints')
        if scoped is not None:
            from verification_coordination import fingerprints
            if not isinstance(scoped,dict) or fingerprints(folder.parent.parent.parent, list(scoped))!=scoped:continue
        if e.get('revision_fingerprint')==expected:rows.append(e);continue
        # Relaxed backend reuse only with a matching full non-document Git fingerprint.
        # No Git baseline, stale configuration or unclassified documents means rerun.
        if e.get('kind')!='backend' or runtime is None or e.get('runtime_revision_fingerprint')!=runtime or not e.get('parameters',{}).get('workspace_fingerprint'):continue
        if not checked:
            checked=True
            try:
                import workspace_git
                root=folder.parent.parent.parent
                project=task_runtime.read_record(project_state(root)/'config.json').get('project')
                workspace=workspace_git.fingerprint(root,project,review_documents=declared_review_documents(folder))
            except (OSError,ValueError,TypeError):pass
        if workspace and workspace==e['parameters']['workspace_fingerprint']:rows.append(e)
    return rows
