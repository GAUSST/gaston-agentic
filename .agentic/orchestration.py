"""Provider-independent task ownership, scope and Lead-edit ledger.

This is a local coordination guard, not a filesystem sandbox for third-party
agents. The native CLIs retain their own permission controls.
"""
from __future__ import annotations
from project_layout import project_state, central_mode
import hashlib
import json
import os
import ntpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
import tempfile
import time
from task_runtime import read_record, RUNNING
from session_store import process_state
from contextlib import contextmanager
from threading import RLock, local
_MUTATION_LOCK=RLock()
_GUARD_STATE=local()

@contextmanager
def mutation_guard(root):
    """Serialize plan transitions and local writes across verifier processes on POSIX."""
    with _MUTATION_LOCK:
        root=Path(root).resolve()
        held=getattr(_GUARD_STATE,'roots',set())
        if root in held:
            yield
            return
        lock=project_state(root)/'local/orchestration.lock' 
        lock.parent.mkdir(parents=True,exist_ok=True)
        if lock.is_symlink():raise ValueError('Unsafe orchestration lock')
        from platform_compat import file_lock
        with file_lock(lock):
            _GUARD_STATE.roots=held | {root}
            try:yield
            finally:_GUARD_STATE.roots=held

SAFE_ID=re.compile(r'[a-z0-9][a-z0-9-]{0,79}\Z')
PRIVATE={'.gaston-dev-db','.agentic','.gaston','.claude','.agents','.codex','.git','.ssh','.env','node_modules','target','build','dist','.venv','venv'}
ROOT_PROTECTED={'dev','AGENTS.md','CLAUDE.md','.gitignore','README.md'}
KEY_EXT={'.pem','.key','.p12','.pfx','.jks','.keystore'}


def put(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.orchestration-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)



def _windows_long_path(value):
    """Expand 8.3 aliases for existing Windows ancestors without requiring the leaf."""
    raw=ntpath.abspath(str(value))
    missing=[]
    probe=raw
    while not os.path.exists(probe):
        parent,leaf=ntpath.split(probe)
        if not parent or parent==probe:
            break
        missing.insert(0,leaf)
        probe=parent
    if os.path.exists(probe):
        try:
            import ctypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            get_long=kernel.GetLongPathNameW
            get_long.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32]
            get_long.restype=ctypes.c_uint32
            size=get_long(probe,None,0)
            if size:
                buffer=ctypes.create_unicode_buffer(size+1)
                if get_long(probe,buffer,len(buffer)):
                    probe=buffer.value
        except (AttributeError,OSError,ValueError):
            pass
    return ntpath.normpath(ntpath.join(probe,*missing))


def relative_path(root, name):
    """Normalize native provider paths to safe workspace-relative POSIX paths.

    Accept relative paths from either provider and absolute paths only when
    they resolve inside this workspace. Resolve macOS /var aliases and Windows
    8.3 aliases before containment checks. The result is platform-neutral.
    """
    if not isinstance(name,str) or not name or '\x00' in name:
        raise ValueError('Expected a non-empty workspace path')
    root_path=Path(root).resolve()
    if os.name=='nt':
        supplied=PureWindowsPath(name)
        if '..' in supplied.parts:
            raise ValueError('Parent traversal is not a workspace path: '+name)
        root_abs=_windows_long_path(root_path)
        if supplied.is_absolute():
            candidate=_windows_long_path(supplied)
            try:
                common=ntpath.commonpath((root_abs,candidate))
            except ValueError as exc:
                raise ValueError('Path is outside workspace: '+name) from exc
            if ntpath.normcase(common)!=ntpath.normcase(root_abs):
                raise ValueError('Path is outside workspace: '+name)
            relative=ntpath.relpath(candidate,root_abs)
            value=PureWindowsPath(relative).as_posix()
        else:
            if supplied.drive or supplied.root:
                raise ValueError('Expected a workspace-relative path: '+name)
            value=supplied.as_posix()
    else:
        if re.match(r'^[A-Za-z]:[\\/]',name) or name.startswith('\\\\'):
            raise ValueError('A Windows absolute path cannot be resolved on this host: '+name)
        supplied=Path(name.replace('\\','/'))
        if '..' in supplied.parts:
            raise ValueError('Parent traversal is not a workspace path: '+name)
        if supplied.is_absolute():
            root_abs=os.path.realpath(os.fspath(root_path))
            candidate=os.path.realpath(os.fspath(supplied))
            try:
                relative=os.path.relpath(candidate,root_abs)
            except ValueError as exc:
                raise ValueError('Path is outside workspace: '+name) from exc
            if relative==os.curdir:
                value='.'
            elif relative==os.pardir or relative.startswith(os.pardir+os.sep):
                raise ValueError('Path is outside workspace: '+name)
            else:
                value=Path(relative).as_posix()
        else:
            value=supplied.as_posix()
    rel=PurePosixPath(value)
    if rel.is_absolute() or not rel.parts or '..' in rel.parts:
        raise ValueError('Private or unsafe path: '+name)
    return rel.as_posix()

def path_for(root, name, *, scope=False):
    root=Path(root).resolve()
    relative=relative_path(root,name)
    rel=PurePosixPath(relative)
    def folded(value):return value.casefold() if os.name=='nt' else value
    if any(folded(x) in {folded(item) for item in PRIVATE} or folded(x).startswith('.env') for x in rel.parts):
        raise ValueError('Private or unsafe path: '+name)
    if (len(rel.parts)==1 and folded(rel.name) in {folded(x) for x in ROOT_PROTECTED}) or rel.suffix.lower() in KEY_EXT:
        raise ValueError('Protected toolkit or key path: '+name)
    if re.search(r'(^|[-_.])(secret[s]?|credentials?|private[-_]?key)([-_.]|$)',rel.name,re.I) and rel.suffix.lower() not in {'.java','.kt','.py','.ts','.js','.go','.rs','.md','.sh'}:
        raise ValueError('Potential secret path: '+name)
    target=root.joinpath(*rel.parts)
    if target.resolve()!=target or any((root/Path(*p.parts)).is_symlink() for p in (PurePosixPath(*rel.parts[:i]) for i in range(1,len(rel.parts)+1))):
        raise ValueError('Linked path: '+name)
    if target.exists() and not target.is_dir() and (not target.is_file() or target.stat().st_nlink!=1):
        raise ValueError('Not a regular single-link path: '+name)
    if not scope and target.exists() and target.is_dir():
        raise ValueError('Expected a file path: '+name)
    return target


def normalize_scope(root, paths):
    if not isinstance(paths,list) or not paths:raise ValueError('Declare concrete, narrow workspace-relative file or module paths')
    out=[]
    for p in paths:
        if not isinstance(p,str) or any(c in p for c in '*?[]'):raise ValueError('Use exact paths, not globs')
        target=path_for(root,p,scope=True)
        if target.is_dir() and any((target/x).exists() for x in ('.git','pom.xml','package.json','pubspec.yaml','build.gradle','build.gradle.kts')):
            raise ValueError('Do not delegate the entire application root; name the affected module or files: '+p)
        out.append(target.relative_to(Path(root).resolve()).as_posix())
    return sorted(set(out))


def within(name, scopes):
    def key(value):
        normalized=str(value).replace('\\','/').rstrip('/')
        return normalized.casefold() if os.name=='nt' else normalized
    candidate=key(name)
    return bool(candidate) and any(candidate==key(scope) or candidate.startswith(key(scope)+'/') for scope in scopes)


def overlaps(a,b):
    return any(within(x,b) for x in a) or any(within(x,a) for x in b)


def active_builders(root, exclude=None):
    from runtime_recovery import recover
    recover(root)
    out=[]
    for folder in (project_state(root)/'runs').glob('*'):
        if folder.name==exclude or folder.is_symlink() or not folder.is_dir():continue
        task=read_record(folder/'task.json');state=read_record(folder/'state.json');worker=read_record(folder/'worker.json')
        if not task or task.get('owner')=='lead':continue
        begun=worker.get('started_at');end=read_record(folder/'worker-finished.json').get('finished_at')
        running=state.get('stage') in RUNNING or (type(worker.get('pid')) is int and process_state(worker) != 'dead' and isinstance(begun,(float,int)) and (not isinstance(end,(float,int)) or end<begun))
        if running:out.append({'task_id':folder.name,'scope':task.get('write_scope') or [],'stage':state.get('stage')})
    return out


def plan_file(root, plan_id):
    if not isinstance(plan_id,str) or not SAFE_ID.fullmatch(plan_id):raise ValueError('Invalid plan ID')
    p=project_state(root)/'local/work-plans'/f'{plan_id}.json'
    if p.is_symlink():raise ValueError('Unsafe plan path')
    return p


def create_plan(root, goal, items, *, existing=None):
    if not isinstance(goal,str) or not goal.strip() or not isinstance(items,list) or not items:raise ValueError('Goal and concrete work items required')
    normalized=dict(existing['items']) if existing else {}
    for entry in items:
        if not isinstance(entry,dict):raise ValueError('Invalid work item')
        name=entry.get('id');owner=entry.get('owner');kind=entry.get('kind');desc=entry.get('goal');deps=entry.get('depends_on',[])
        if not isinstance(name,str) or not SAFE_ID.fullmatch(name) or name in normalized:raise ValueError('Work item IDs must be unique simple identifiers')
        if (owner,kind) not in {('lead','inspect'),('lead','implement'),('builder','implement')}:raise ValueError('Inspection belongs to Lead; implementation can be Lead or Builder')
        if not isinstance(desc,str) or not desc.strip() or not isinstance(deps,list) or not all(isinstance(d,str) for d in deps):raise ValueError('Work item needs a goal and dependency IDs')
        paths=normalize_scope(root,entry.get('paths')) if kind=='implement' else []
        criteria=entry.get('acceptance_criteria',[])
        if not isinstance(criteria,list) or not all(isinstance(x,str) and x.strip() for x in criteria):raise ValueError('Invalid acceptance criteria')
        if kind=='implement' and not criteria:raise ValueError('Implementation slice needs measurable acceptance criteria')
        idle=entry.get('requires_idle_workspace',False)
        if type(idle) is not bool:raise ValueError('requires_idle_workspace must be boolean')
        normalized[name]={'id':name,'owner':owner,'kind':kind,'goal':desc.strip(),'depends_on':list(dict.fromkeys(deps)),'paths':paths,'acceptance_criteria':criteria,'requires_idle_workspace':idle}
    pending=set(normalized);done=set()
    while pending:
        ready={x for x in pending if set(normalized[x]['depends_on'])<=done}
        if not ready:raise ValueError('Missing or cyclic plan dependencies')
        done|=ready;pending-=ready
    key=existing['id'] if existing else 'plan-'+secrets.token_hex(8)
    record={**(existing or {}),'id':key,'goal':goal,'created_at':existing.get('created_at') if existing else time.time(),'items':normalized}
    if existing and record.get('checkpoint_task_id'):
        record.setdefault('previous_checkpoint_tasks',[]).append(record.pop('checkpoint_task_id'))
    put(plan_file(root,key),record);return plan_status(root,key)


def extend_plan(root,key,items):
    existing=get_plan(root,key)
    if existing.get('checkpoint_task_id') and read_record(project_state(root)/'runs'/existing['checkpoint_task_id']/'state.json').get('lead_verified'):
        raise ValueError('Checkpoint already accepted; open a new plan for new work')
    return create_plan(root,existing['goal'],items,existing=existing)

def get_plan(root, key):
    plan=read_record(plan_file(root,key))
    if not plan:raise ValueError('Unknown work plan')
    return plan


def item_status(root, item):
    if item.get('superseded_by'):return 'superseded'
    if item['kind']=='inspect':return 'done' if item.get('evidence') else 'pending'
    if not item.get('task_id'):return 'pending'
    folder=project_state(root)/'runs'/item['task_id'];state=read_record(folder/'state.json')
    if state.get('stage')=='slice_reviewed':return 'done'
    if state.get('lead_verified') and state.get('stage')=='ready_for_human_release_review':
        # Historical slice evidence remains historical. Final integrated pass is
        # required after all slices, not inferred from this status.
        return 'done'
    return state.get('stage','pending')


def plan_status(root,key):
    running=active_builders(root)
    from verification_coordination import active, overlaps as resource_overlaps
    verification=active(root)
    plan=get_plan(root,key);items=plan['items'];statuses={k:item_status(root,v) for k,v in items.items()}
    result=[]
    for k,v in items.items():
        ready=all(statuses[x]=='done' for x in v['depends_on']) and statuses[k]=='pending'
        blockers=[]
        if running and (v.get('requires_idle_workspace',False) or v['owner']=='builder'):
            blockers.append('active_builder')
        if running and v['owner']=='lead' and v['kind']=='implement' and any(not r.get('scope') or overlaps(v['paths'],r['scope']) for r in running):
            blockers.append('overlapping_builder_scope')
        if verification and v['kind']=='implement' and resource_overlaps(v.get('paths'),verification.get('read_scope')):
            blockers.append('overlapping_verification_scope')
        result.append({**v,'status':statuses[k],'ready':ready,'dependency_ready':ready,
                       'executable':ready and not blockers,'execution_blockers':blockers})
    resolved=all(x in ('done','superseded') for x in statuses.values())
    return {'status':'ready','plan_id':key,'goal':plan['goal'],'items':result,'all_items_done':all(x=='done' for x in statuses.values()),'all_items_resolved':resolved,'checkpoint_task_id':plan.get('checkpoint_task_id'),'checkpoint_stage':read_record(project_state(root)/'runs'/plan.get('checkpoint_task_id','_')/'state.json').get('stage') if plan.get('checkpoint_task_id') else None,'active_builder_tasks':running,'active_verification':verification,'note':'Superseded items are auditable retirements, not completed work. Every replacement item must still be completed before integrated checkpoint acceptance. Run executable independent Lead items while Builder works; dependency-ready items may be blocked. Use wait_for_builder once no independent work is executable. Done slices do not certify the combined result.'}


def select_item(root, key, name, owner, kind):
    plan=get_plan(root,key);item=plan['items'].get(name)
    if not item or item['owner']!=owner or item['kind']!=kind:raise ValueError('Work item owner or kind mismatch')
    if item_status(root,item)!='pending' or item.get('task_id'):raise ValueError('Work item already started; resume its existing task')
    if any(item_status(root,plan['items'][d])!='done' for d in item['depends_on']):raise ValueError('Complete prerequisite work items first')
    if item.get('requires_idle_workspace') and active_builders(root):raise ValueError('Work item requires an idle workspace; collect Builder completion first')
    return plan,item


def supersede_work_item(root,key,name,replacement_name,reason):
    """Retire one unstarted plan item in favour of an explicit replacement."""
    plan=get_plan(root,key)
    if plan.get('checkpoint_task_id'):
        checkpoint=read_record(project_state(root)/'runs'/plan['checkpoint_task_id']/'state.json')
        if checkpoint.get('lead_verified') and checkpoint.get('stage')=='ready_for_human_release_review':
            raise ValueError('Checkpoint already accepted; create a new plan for new work')
    item=plan['items'].get(name)
    replacement=plan['items'].get(replacement_name)
    if not item or not replacement:raise ValueError('Both original and replacement work items must exist in this plan')
    if name==replacement_name:raise ValueError('A work item cannot supersede itself')
    if item.get('superseded_by'):raise ValueError('Work item is already superseded')
    if item.get('task_id') or item_status(root,item)!='pending':
        raise ValueError('Only an unstarted pending work item can be superseded; preserve started task history')
    if replacement.get('superseded_by'):raise ValueError('Replacement item is itself superseded')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('Record why the original item is a duplicate or obsolete')
    # A replacement cannot depend on the item it replaces. Do not rewrite
    # the dependency history of a replacement that has already started.
    if name in replacement.get('depends_on',[]):
        if replacement.get('task_id') or item_status(root,replacement)!='pending':
            raise ValueError('Replacement already started and depends on the original item')
        replacement['depends_on']=[dep for dep in replacement.get('depends_on',[]) if dep!=name]
    # Rewire only unstarted pending dependents. A completed dependent keeps its
    # historical dependency; a running dependent makes this operation unsafe.
    rewired=[]
    for other_name,other in plan['items'].items():
        if other_name==name or name not in other.get('depends_on',[]):continue
        status=item_status(root,other)
        if status=='done' or status=='superseded':continue
        if status!='pending' or other.get('task_id'):
            raise ValueError('Cannot supersede an item used by an already-started dependent: '+other_name)
        deps=[]
        for dep in other['depends_on']:
            dep=replacement_name if dep==name else dep
            if dep not in deps:deps.append(dep)
        other['depends_on']=deps
        rewired.append(other_name)
    item['superseded_by']=replacement_name
    item['superseded_reason']=reason.strip()
    item['superseded_at']=time.time()
    # Validate the dependency graph before persisting the audit record.
    pending=set(plan['items']);resolved=set()
    while pending:
        ready={current for current in pending if set(plan['items'][current].get('depends_on',[]))<=resolved}
        if not ready:raise ValueError('Supersession would create a missing or cyclic dependency')
        resolved.update(ready);pending.difference_update(ready)
    put(plan_file(root,key),plan)
    result=plan_status(root,key)
    result['superseded_item']=name
    result['replacement_item']=replacement_name
    result['rewired_dependents']=rewired
    result['note']='Original item retained as superseded; replacement work remains independently required.'
    return result


def defer_until_idle(root, key, name, reason):
    """Annotate an existing pending Lead item; never fabricate completion."""
    plan=get_plan(root,key)
    item=plan['items'].get(name)
    if not item or item['owner']!='lead' or item_status(root,item)!='pending':
        raise ValueError('Only a pending Lead work item may be deferred until idle')
    if not isinstance(reason,str) or not reason.strip():
        raise ValueError('Record the concrete execution blocker')
    item['requires_idle_workspace']=True
    item['idle_reason']=reason.strip()
    put(plan_file(root,key),plan)
    return plan_status(root,key)


def bind(root,plan,item,task_id):
    item['task_id']=task_id;put(plan_file(root,plan['id']),plan)


def inspection(root,key,name,evidence,followup_items=None):
    plan,item=select_item(root,key,name,'lead','inspect')
    if not isinstance(evidence,str) or not evidence.strip():raise ValueError('Record factual findings, inspected files and unresolved work')
    if followup_items:
        # Validate and add concrete remediation in the same plan before marking inspection closed.
        create_plan(root,plan['goal'],followup_items,existing=plan)
        plan=get_plan(root,key);item=plan['items'][name]
    item['evidence']=evidence.strip();item['inspected_at']=time.time();put(plan_file(root,key),plan)
    return plan_status(root,key)


def test_path(name):
    p=Path(name);parts={x.lower() for x in p.parts};n=p.name.lower()
    return bool(parts & {'test','tests','__tests__','e2e','integrationtest','integrationtests','integration-test','integration-tests','.maestro'}) or n=='gaston-e2e.json' or n.startswith('test_') or bool(re.search(r'(?:test|spec)\.(?:java|kt|js|ts|tsx|py|dart)$',n)) or ('scripts' in parts and ('test' in n or 'e2e' in n))


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def lead_paths(folder):
    value=read_record(Path(folder)/'lead-edits.json')
    return set(value.get('files',{}))


def apply_edits(root, folder, edits, purpose):
    """CAS-protected full-text or non-overlapping exact replacements per file.

    All groups are validated before writes; each file is replaced atomically.

    Must use declared scope for Lead-owned implementation/takeover; test setup
    can extend a Builder task, but only outside a running Builder's scope.
    """
    root=Path(root).resolve();folder=Path(folder);task=read_record(folder/'task.json');state=read_record(folder/'state.json')
    if state.get('lead_verified'):raise ValueError('Already verified; create a new task for new changes')
    if state.get('stage')=='slice_reviewed':raise ValueError('Slice already reviewed; create a new scoped remediation item in the plan')
    if not isinstance(edits,list) or not edits:raise ValueError('Supply exact file edits')
    own=task.get('owner')=='lead';takeover=state.get('stage')=='lead_takeover_required' or state.get('stage')=='lead_implementing' and state.get('takeover')
    if purpose=='implementation' and not own and not takeover:raise ValueError('Application implementation requires a Lead-owned slice or correction takeover')
    if purpose not in ('test_setup','implementation'):raise ValueError('Unknown edit purpose')
    running=active_builders(root)
    # Group by canonical path. Each group refers to one immutable pre-edit
    # snapshot, not to text created by another replacement in the batch.
    groups={}
    for edit in edits:
        if not isinstance(edit,dict):raise ValueError('Invalid edit')
        p=path_for(root,edit.get('path'));name=p.relative_to(root).as_posix()
        groups.setdefault(name,(p,[]))[1].append(edit)
    from verification_coordination import assert_write_allowed
    assert_write_allowed(root,list(groups))
    prepared=[]
    for name,(p,group) in groups.items():
        if purpose=='test_setup' and not test_path(name):raise ValueError('Test setup only permits test/fixture files; use a scoped Lead implementation for other local changes')
        if (own or purpose=='implementation') and not within(name,task.get('write_scope') or read_record(folder/'build.json').get('changed_files',[])):
            raise ValueError('File is outside the declared Lead/takeover scope: '+name)
        for worker in running:
            if not worker['scope'] or within(name,worker['scope']):raise ValueError('Builder may be editing '+name+'; use a disjoint scope or wait for completion')
        raw=p.read_bytes() if p.is_file() else None
        before=hashlib.sha256(raw).hexdigest() if raw is not None else None
        if any('before_sha256' not in edit or edit['before_sha256']!=before for edit in group):
            raise ValueError('File changed or hash missing: '+name+' (all edits must use the same original hash; null only for new file)')
        if any('content' in edit for edit in group):
            if len(group)!=1 or any(k in group[0] for k in ('old_text','new_text')):
                raise ValueError('Full content cannot be combined with other edits: '+name)
            text=group[0]['content']
        else:
            if raw is None:raise ValueError('New files require full content: '+name)
            original=raw.decode('utf8')
            # Native readers commonly normalize CRLF. For a consistently CRLF
            # file, accept normalized replacements without changing its endings.
            crlf='\r\n' in original and '\r' not in original.replace('\r\n','') and '\n' not in original.replace('\r\n','')
            spans=[]
            for edit in group:
                old,new=edit.get('old_text'),edit.get('new_text')
                if not isinstance(old,str) or not old or not isinstance(new,str) or '\x00' in old or '\x00' in new:
                    raise ValueError('Provide full content or a unique exact text replacement')
                if crlf:
                    old=old.replace('\r\n','\n').replace('\n','\r\n')
                    new=new.replace('\r\n','\n').replace('\n','\r\n')
                start=original.find(old)
                # Check overlapping occurrences too (e.g. "aa" in "aaa").
                if start<0 or original.find(old,start+1)>=0:
                    raise ValueError('Exact replacement is missing or ambiguous: '+name)
                spans.append((start,start+len(old),new))
            spans.sort(key=lambda span:span[0])
            if any(left[1]>right[0] for left,right in zip(spans,spans[1:])):
                raise ValueError('Overlapping replacements: '+name+' (use non-overlapping original snippets)')
            pieces=[];cursor=0
            for start,end,new in spans:
                pieces.extend((original[cursor:start],new));cursor=end
            pieces.append(original[cursor:]);text=''.join(pieces)
        if not isinstance(text,str) or '\x00' in text:raise ValueError('Only text source and test files are supported')
        # Validate encoding before any file is written, including other groups.
        prepared.append((name,p,text.encode('utf8'),before))
    # Preflight every group before committing any file. Repeat the CAS check
    # immediately before each atomic file replacement for external changes.
    for name,p,_,before in prepared:
        path_for(root,name)
        if digest(p)!=before:raise ValueError('Concurrent modification: '+name)
    ledger=read_record(folder/'lead-edits.json');files=ledger.setdefault('files',{});updated=[]
    for name,p,data,before in prepared:
        # Recheck immediately before atomic replacement, including symlink safety.
        path_for(root,name)
        if digest(p)!=before:raise ValueError('Concurrent modification: '+name)
        p.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix='.gaston-edit-',dir=p.parent)
        try:
            if os.name!='nt':
                if p.exists():os.fchmod(fd,p.stat().st_mode & 0o777)
                else:os.fchmod(fd,0o644)
            with os.fdopen(fd,'wb') as fh:fh.write(data)
            os.replace(tmp,p)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        files[name]={'before_sha256':files.get(name,{}).get('before_sha256',before),'after_sha256':digest(p),'purpose':purpose,'edited_at':time.time()}
        put(folder/'lead-edits.json',ledger);updated.append(name)
    return {'status':'pass','edited_files':updated,'note':'Local files changed by Lead; independent checks still required'}


def canonical_change_paths(paths):
    # Provider reports and old reconciliation records may mix native and POSIX
    # separators. Keep the evidence, but count each relative file only once.
    return sorted({value.replace('\\','/') for value in paths})


def merge_lead_paths(folder,build):
    return {**build,'changed_files':canonical_change_paths(set(build.get('changed_files',[]))|lead_paths(folder)),'lead_changed_files':canonical_change_paths(lead_paths(folder))}


def correction_round(previous,state):
    # A quota stop or tool interruption is not an unsuccessful code correction.
    count=int(previous.get('correction_round',0))
    if state.get('blocker') in ('provider_quota','worker_interrupted','worker_start_failed','session_start_blocked') or state.get('stage')=='needs_lead_decision' and state.get('clarification_only') is True and state.get('questions'):
        return count
    return count+1


def inspect_files(root, requests):
    """Batch explicit, non-secret file inspections without additional model turns.

    Optional literal search returns bounded matches with stable line numbers.
    Full files can be read with native tools; never summarize unseen content.
    """
    from concurrent.futures import ThreadPoolExecutor
    if not isinstance(requests,list) or not requests:raise ValueError('Specify files to inspect')
    paths=[]
    for entry in requests:
        if not isinstance(entry,dict):raise ValueError('Invalid inspection request')
        path=path_for(root,entry.get('path'));needle=entry.get('contains')
        if needle is not None and not isinstance(needle,str):raise ValueError('Use a literal search string')
        paths.append((entry,path,needle))
    def read(item):
        entry,path,needle=item
        if not path.is_file():return {'path':entry['path'],'exists':False,'sha256':None}
        sha=digest(path);result={'path':entry['path'],'exists':True,'sha256':sha,'bytes':path.stat().st_size}
        if needle:
            count=0;matches=[]
            with path.open(encoding='utf8',errors='replace') as f:
                for line_no,line in enumerate(f,1):
                    if needle.casefold() in line.casefold():
                        count+=1
                        if len(matches)<20:
                            # Return line numbers; native Read provides the exact code
                            # under its own permissions, without dumping secret values.
                            matches.append(line_no)
            result.update(match_count=count,matching_lines=matches,truncated=count>len(matches))
        return result
    with ThreadPoolExecutor(max_workers=min(8,len(paths))) as pool:results=list(pool.map(read,paths))
    return {'status':'ready','files':results,'note':'Evidence only; inspect matching source before making semantic claims.'}


def _scope_conflicts(root, paths, exclude=None):
    from verification_coordination import assert_write_allowed
    assert_write_allowed(root,paths)
    for worker in active_builders(root,exclude=exclude):
        if not worker['scope'] or overlaps(paths,worker['scope']):raise ValueError('Scope overlaps active Builder '+worker['task_id'])
    for f in (project_state(root)/'runs').glob('*'):
        if f.name==exclude or f.is_symlink() or not f.is_dir():continue
        if read_record(f/'state.json').get('stage') in ('lead_implementing','lead_takeover_required'):
            scope=read_record(f/'task.json').get('write_scope') or []
            if not scope or overlaps(paths,scope):raise ValueError('Scope overlaps active Lead task '+f.name)


def extend_item_scope(root,plan,item,paths,*,reason):
    extra=[p for p in paths if not within(p,item['paths'])]
    if not extra:return item['paths']
    if not isinstance(reason,str) or not reason.strip():raise ValueError('Explain scope expansion')
    _scope_conflicts(root,extra,item.get('task_id'))
    item['paths']=sorted(set(item['paths'])|set(extra))
    item.setdefault('scope_extensions',[]).append({'paths':extra,'reason':reason,'recorded_at':time.time()})
    put(plan_file(root,plan['id']),plan)
    return item['paths']


def normalize_private_scope_state(root,folder):
    """Remove false scope blockers from reserved Gaston paths in older task records.

    This is a narrow compatibility repair, not task acceptance: it never removes
    application paths, runs tests, sets lead_verified, or closes a task.
    """
    from workspace_git import is_excluded_path
    folder=Path(folder)
    state=read_record(folder/'state.json')
    if state.get('stage') in RUNNING:
        return False
    worker=read_record(folder/'worker.json')
    if worker:
        started=worker.get('started_at')
        finished=read_record(folder/'worker-finished.json').get('finished_at')
        if (not isinstance(started,(int,float)) or not isinstance(finished,(int,float))
                or finished < started):
            return False
    build=read_record(folder/'build.json')
    if not build:
        return False

    removed=set()
    def filter_paths(value):
        if not isinstance(value,list):
            return value
        kept=[]
        for name in value:
            if isinstance(name,str) and is_excluded_path(name):
                removed.add(name)
            else:
                kept.append(name)
        return kept

    old_pending=build.get('scope_review_required') or []
    old_reported=build.get('out_of_scope_reported') or []
    pending=filter_paths(old_pending)
    reported=filter_paths(old_reported)
    changed=filter_paths(build.get('changed_files') or [])
    revision_paths=filter_paths(build.get('revision_changed_files') or [])
    lead_paths_reported=filter_paths(build.get('lead_changed_files') or [])
    if not removed:
        return False

    observation_path=folder/'scope-observation.json'
    observation=read_record(observation_path)
    if observation:
        for key in ('observed_outside_scope','reported_outside_scope','pending_paths','matching_lead_ledger'):
            if key in observation:
                observation[key]=filter_paths(observation[key])
        put(observation_path,observation)

    concurrent_path=folder/'concurrent-observed-paths.json'
    concurrent=read_record(concurrent_path)
    if concurrent and 'outside_builder_scope' in concurrent:
        concurrent['outside_builder_scope']=filter_paths(concurrent['outside_builder_scope'])
        put(concurrent_path,concurrent)

    build.update(changed_files=changed,scope_review_required=pending,
                 out_of_scope_reported=reported)
    if 'revision_changed_files' in build:
        build['revision_changed_files']=revision_paths
    if 'lead_changed_files' in build:
        build['lead_changed_files']=lead_paths_reported

    restored=False
    if old_pending or (old_reported and not reported):
        prior_questions=build.get('scope_prior_questions')
        if not isinstance(prior_questions,list):
            scope_question=build.get('scope_question')
            prior_questions=[q for q in (build.get('questions') or [])
                             if isinstance(q,str) and q!=scope_question]
        prior_status=build.get('scope_prior_status',build.get('status','blocked'))
        if pending:
            scope_question='Scope reconciliation needed: '+', '.join(pending)
            build['scope_question']=scope_question
            build['questions']=prior_questions+[scope_question]
            build['status']='blocked'
            state={**state,'stage':'needs_lead_decision','questions':build['questions'],
                   'clarification_only':False}
        else:
            build['questions']=prior_questions
            build['status']=prior_status
            build.pop('scope_question',None)
            state={**state,
                   'stage':'awaiting_lead_review' if prior_status=='done' and not prior_questions else 'needs_lead_decision',
                   'questions':prior_questions,'clarification_only':False}
        restored=True

    put(folder/'build.json',build)
    if restored:
        put(folder/'state.json',state)
    audit_path=folder/'scope-normalization.json'
    audit=read_record(audit_path)
    events=audit.get('events',[]) if isinstance(audit.get('events'),list) else []
    events.append({'policy':'reserved-private-paths-v1','removed_paths':sorted(removed),
                   'reason':'Reserved Gaston/runtime state is not application scope or Builder output.',
                   'recorded_at':time.time(),'restored_prior_stage':restored})
    put(audit_path,{'events':events})
    return True


def _idle_task(root,tid):
    if not isinstance(tid,str) or not re.fullmatch(r'[a-z0-9-]{8,120}',tid):raise ValueError('Invalid task ID')
    folder=project_state(root)/'runs'/tid
    task=read_record(folder/'task.json');state=read_record(folder/'state.json')
    if folder.is_symlink() or not task or not state:raise ValueError('Unknown task')
    if state.get('stage') in RUNNING or any(x['task_id']==tid for x in active_builders(root)) or state.get('lead_verified') or state.get('stage') in ('slice_reviewed','abandoned','ready_for_human_release_review'):
        raise ValueError('Task is active or already closed')
    normalize_private_scope_state(root,folder)
    task=read_record(folder/'task.json');state=read_record(folder/'state.json')
    return folder,task,state


def extend_task_scope(root,tid,paths,reason):
    folder,task,state=_idle_task(root,tid)
    if not isinstance(reason,str) or not reason.strip():raise ValueError('Explain why scope must change')
    extras=normalize_scope(root,paths);_scope_conflicts(root,extras,tid)
    scope=sorted(set(task.get('write_scope') or [])|set(extras))
    if task.get('work_plan_id') and task.get('work_item_id'):
        plan=get_plan(root,task['work_plan_id']);item=plan['items'][task['work_item_id']]
        if item.get('task_id')!=tid:raise ValueError('Task no longer owns plan item')
        scope=extend_item_scope(root,plan,item,scope,reason=reason)
    task['write_scope']=scope
    task.setdefault('scope_extensions',[]).append({'paths':extras,'reason':reason,'recorded_at':time.time()})
    put(folder/'task.json',task)
    return {'status':'ready','task_id':tid,'write_scope':scope,'note':'Scope updated; implementation and independent checks still required.'}


def resolve_questions(root,tid,answers,implementation_complete=False,completion_evidence=''):
    folder,task,state=_idle_task(root,tid);build=read_record(folder/'build.json')
    questions=build.get('questions') or []
    if state.get('stage')!='needs_lead_decision' or not questions:raise ValueError('No pending Builder questions')
    if not isinstance(answers,dict) or set(answers)!=set(questions) or not all(isinstance(a,str) and a.strip() for a in answers.values()):raise ValueError('Answer every exact recorded question')
    if type(implementation_complete) is not bool:raise ValueError('implementation_complete must be boolean')
    if implementation_complete and (not isinstance(completion_evidence,str) or not completion_evidence.strip()):raise ValueError('Describe independently inspected, already-complete implementation; never waive unfinished work')
    # A clarification may be answered without pretending unfinished work is done.
    put(folder/'clarifications.json',{'questions':questions,'answers':answers,'implementation_complete':implementation_complete,'completion_evidence':completion_evidence,'prior_status':build.get('status'),'recorded_at':time.time()})
    # Do not erase interrupted work, scope or compilation findings. Revision must use recorded answers.
    import builder_compile
    compile_blockers=builder_compile.blocking_receipts(build.get('compile_preflight',[]))
    hard_block=build.get('interrupted') or build.get('out_of_scope_reported') or build.get('scope_review_required') or bool(compile_blockers)
    if hard_block or not build.get('changed_files') or (build.get('status')!='done' and not implementation_complete):
        return {'status':'ready','task_id':tid,'requires_revision':True,'note':'Answers recorded; unfinished or blocked implementation must be corrected, not accepted.'}
    build.update(status='done',questions=[]);put(folder/'build.json',build)
    put(folder/'state.json',{**state,'stage':'awaiting_lead_review','questions':[],'clarification_only':False})
    return {'status':'ready','task_id':tid,'requires_revision':False,'note':'Questions resolved; independent review and tests still required.'}


def abandon_empty_lead_task(root,tid,reason):
    folder,task,state=_idle_task(root,tid);build=read_record(folder/'build.json')
    if task.get('owner')!='lead' or state.get('stage')!='lead_implementing' or lead_paths(folder) or build.get('changed_files') or task.get('checkpoint_acceptance'):
        raise ValueError('Only an empty, unverified Lead implementation task can be abandoned')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('Record abandonment reason')
    if task.get('work_plan_id') and task.get('work_item_id'):
        plan=get_plan(root,task['work_plan_id']);item=plan['items'][task['work_item_id']]
        if item.get('task_id')!=tid:raise ValueError('Task no longer owns plan item')
        item.pop('task_id');item.setdefault('abandoned_tasks',[]).append(tid);put(plan_file(root,plan['id']),plan)
    put(folder/'state.json',{**state,'stage':'abandoned','reason':reason,'abandoned_at':time.time()})
    return {'status':'ready','task_id':tid,'stage':'abandoned','note':'Task retained; no files removed or work marked done.'}


def declare_review_only_documents(root,tid,paths,reason):
    import acceptance_audit
    folder,task,state=_idle_task(root,tid)
    if not isinstance(paths,list) or not paths or not isinstance(reason,str) or not reason.strip():raise ValueError('Provide exact prose paths and inspected non-runtime rationale')
    specs=acceptance_audit.specification_documents(folder)
    build=read_record(folder/'build.json')
    for name in paths:
        target=path_for(root,name)
        if not acceptance_audit.review_only_document(name) or name in specs or not target.is_file():raise ValueError('Not an eligible ordinary prose file: '+name)
        if not within(name,task.get('write_scope',[])) and name not in build.get('changed_files',[]):raise ValueError('Prose file is outside this task: '+name)
    task['review_only_documents']=sorted(set(task.get('review_only_documents',[]))|set(paths))
    task.setdefault('review_only_document_declarations',[]).append({'paths':paths,'reason':reason,'recorded_at':time.time()})
    put(folder/'task.json',task)
    return {'status':'ready','task_id':tid,'review_only_documents':task['review_only_documents'],'note':'Only source review and matching backend evidence may be reused after prose edits; document and all other acceptance reviews remain required.'}


def validate_task_scope(root,folder):
    task=read_record(folder/'task.json');scope=task.get('write_scope') or []
    if task.get('work_plan_id') and task.get('work_item_id'):
        item=get_plan(root,task['work_plan_id'])['items'][task['work_item_id']]
        if item.get('task_id')!=folder.name or set(item['paths'])!=set(scope):raise ValueError('Plan/task scope mismatch; reconcile with extend_task_scope before resuming')
    if scope:_scope_conflicts(root,scope,folder.name)


def scope_observation(root,folder,before,after,scope,reported):
    """Compare application paths only; Gaston's own state is never app-scope work."""
    from workspace_git import is_excluded_path
    clean_reported={name for name in (reported or [])
                    if isinstance(name,str) and not is_excluded_path(name)}
    clean_before=({name:value for name,value in before.items()
                   if isinstance(name,str) and not is_excluded_path(name)}
                  if isinstance(before,dict) else before)
    clean_after=({name:value for name,value in after.items()
                  if isinstance(name,str) and not is_excluded_path(name)}
                 if isinstance(after,dict) else after)
    pending=set(clean_reported);observed=[];known=[]
    if isinstance(clean_before,dict) and isinstance(clean_after,dict) and scope:
        observed=sorted(name for name in set(clean_before)|set(clean_after)
                        if clean_before.get(name)!=clean_after.get(name) and not within(name,scope))
        begun=read_record(folder/'worker.json').get('started_at',0)
        if not isinstance(begun,(int,float)):begun=float('inf')
        for run in (project_state(root)/'runs').glob('*'):
            if run.is_symlink() or not run.is_dir():continue
            for name,entry in read_record(run/'lead-edits.json').get('files',{}).items():
                if name in observed and isinstance(entry,dict) and isinstance(entry.get('edited_at'),(int,float)) and entry['edited_at']>=begun and entry.get('after_sha256') and entry['after_sha256']==clean_after.get(name):known.append(name)
        pending.update(set(observed)-set(known))
    record={'attempt':read_record(folder/'worker.json').get('attempt'),
            'observed_outside_scope':observed,'matching_lead_ledger':sorted(set(known)),
            'reported_outside_scope':sorted(clean_reported),'pending_paths':sorted(pending),
            'git_observation_available':isinstance(clean_after,dict),'recorded_at':time.time(),
            'note':'Unattributed application changes alone are not proof of Builder misconduct; private Gaston/runtime state is excluded.'}
    put(folder/'scope-observation.json',record)
    return record

def reconcile_scope(root,tid,decisions):
    folder,task,state=_idle_task(root,tid);build=read_record(folder/'build.json')
    paths=build.get('scope_review_required') or []
    if not paths or state.get('stage')!='needs_lead_decision':raise ValueError('No pending scope reconciliation')
    if not isinstance(decisions,dict) or set(decisions)!=set(paths):raise ValueError('Classify each exact pending path')
    changed=set(build.get('changed_files',[]));scope=task.get('write_scope') or []
    for name,entry in decisions.items():
        path_for(root,name)
        if not isinstance(entry,dict) or entry.get('attribution') not in ('builder','external') or not isinstance(entry.get('evidence'),str) or not entry['evidence'].strip():raise ValueError('Supply factual independent attribution evidence, or leave scope unresolved')
        if entry['attribution']=='builder':
            if not within(name,scope):raise ValueError('Extend the plan/task scope first or correct the unintended change: '+name)
            changed.add(name)
        elif name not in lead_paths(folder) and name not in scope:
            changed.discard(name)
    prior_status=build.get('scope_prior_status','blocked');questions=build.get('scope_prior_questions') or []
    put(folder/'scope-reconciliation.json',{'decisions':decisions,'observation':read_record(folder/'scope-observation.json'),'recorded_at':time.time()})
    built={**build,'status':prior_status,'questions':questions,'changed_files':canonical_change_paths(changed|lead_paths(folder)),'scope_review_required':[],'out_of_scope_reported':[]}
    put(folder/'build.json',built)
    stage='awaiting_lead_review' if prior_status=='done' and not questions else 'needs_lead_decision'
    put(folder/'state.json',{**state,'stage':stage,'questions':questions,'clarification_only':False})
    return {'status':'ready','task_id':tid,'stage':stage,'requires_revision':stage!='awaiting_lead_review','note':'Scope reconciled; no tests or acceptance implied.'}
