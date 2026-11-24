"""Reserve managed verification resources without serializing disjoint repositories."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json, os, secrets, socket, time
import xml.etree.ElementTree as ET
from project_layout import project_state
from session_store import process_identity, process_state
import orchestration as orch

RECORD = 'verification-reservation.json'

class Busy(ValueError):
    def __init__(self, reason, *, blocker, workers=(), reservation=None):
        super().__init__(reason)
        self.fields={'blocker':blocker,'active_builder_tasks':list(workers),'active_verification':reservation}

def overlaps(left,right):
    return not left or not right or '.' in left or '.' in right or orch.overlaps(left,right)

def _path(root):
    return project_state(root)/'local'/RECORD

def active(root):
    p=_path(root)
    if p.is_symlink():raise ValueError('Unsafe verification reservation path')
    row=orch.read_record(p)
    if p.exists() and not row:raise ValueError('Unreadable verification reservation')
    return {**row,'owner_state':process_state(row.get('owner'))} if row else None

def assert_write_allowed(root,paths):
    row=active(root)
    if row and overlaps(paths,row.get('read_scope')):
        raise ValueError('Write scope overlaps active or unreconciled verification: '+str(row.get('project')))

def _safe_project(root,project):
    if not isinstance(project,str) or Path(project).is_absolute() or '..' in Path(project).parts:return None
    folder=(root/project).resolve()
    if folder==root or not folder.is_relative_to(root) or not folder.is_dir():return None
    if any((parent/'.git').exists() for parent in folder.parents if parent.is_relative_to(root)):return None
    if not (folder/'.git').is_dir():return None
    import workspace_git
    if workspace_git.repositories(folder)!=[folder] or not workspace_git.baseline_ready([folder]):return None
    return folder

def scope_for(root,kind,project,*,integrated=False):
    """Only proven standalone repositories; unknown and integrated gates are exclusive."""
    root=Path(root).resolve()
    if integrated or kind not in ('api','backend','web_verify'):return ['.']
    folder=_safe_project(root,project)
    if folder is None:return ['.']
    try:
        if kind in ('api','backend'):
            pom=folder/'pom.xml'
            if not pom.is_file() or (folder.parent/'pom.xml').is_file():return ['.']
            for element in ET.parse(pom).getroot().iter():
                tag=element.tag.split('}')[-1];value=(element.text or '').strip()
                if tag in ('module','systemPath') and (Path(value).is_absolute() or '..' in Path(value).parts or '$' in value):return ['.']
                if tag=='relativePath' and value and (folder/value).resolve().exists() and not (folder/value).resolve().is_relative_to(folder):return ['.']
        else:
            data=json.loads((folder/'package.json').read_text(encoding='utf-8'))
            if data.get('workspaces'):return ['.']
            deps={**(data.get('dependencies') or {}),**(data.get('devDependencies') or {})}
            if any(isinstance(x,str) and x.startswith(('file:','link:','workspace:')) for x in deps.values()):return ['.']
        descriptor=folder/('gaston-e2e.json' if kind=='api' else 'gaston-verification.json')
        if descriptor.is_symlink():return ['.']
        if kind=='api' and not descriptor.is_file():return ['.']
        extra=json.loads(descriptor.read_text(encoding='utf-8')).get('read_projects',[]) if descriptor.is_file() else []
        if not isinstance(extra,list) or any(_safe_project(root,x) is None for x in extra):return ['.']
        return sorted(set([folder.relative_to(root).as_posix(),*extra]))
    except (OSError,ValueError,ET.ParseError,TypeError,AttributeError):return ['.']

def fingerprints(root,scopes):
    import workspace_git
    root=Path(root).resolve()
    if not scopes or '.' in scopes:return None
    result={}
    try:
        for scope in scopes:
            folder=_safe_project(root,scope)
            if folder is None:return None
            digest=workspace_git.fingerprint(folder,{})
            if digest is None:return None
            result[scope]=digest
        return result
    except (OSError,ValueError,RuntimeError):return None

@contextmanager
def reserve(root,kind,project,*,integrated=False,task_id=None):
    """One managed verification operation at a time; release on every normal exit."""
    root=Path(root).resolve();token=secrets.token_hex(16)
    with orch.mutation_guard(root):
        prior=active(root)
        if prior:raise Busy('Another managed verification is active or unreconciled; collect its outcome.',blocker='verification_active',reservation=prior)
        scope=scope_for(root,kind,project,integrated=integrated)
        before=fingerprints(root,scope)
        if before is None:scope=['.']
        running=orch.active_builders(root)
        conflicts=[w for w in running if overlaps(scope,w.get('scope'))]
        for folder in (project_state(root)/'runs').glob('*'):
            if folder.is_symlink() or not folder.is_dir():continue
            state=orch.read_record(folder/'state.json')
            if state.get('stage') in ('lead_implementing','lead_takeover_required'):
                task=orch.read_record(folder/'task.json')
                if overlaps(scope,task.get('write_scope')):
                    conflicts.append({'task_id':folder.name,'scope':task.get('write_scope'),'stage':state['stage']})
        if conflicts:raise Busy('Verification scope overlaps active implementation, or isolation is unknown.',blocker='workspace_not_idle',workers=conflicts)
        orch.put(_path(root),{'token':token,'owner':{'pid':os.getpid(),'host':socket.gethostname(),**process_identity(os.getpid())},
                              'kind':kind,'project':project,'task_id':task_id,'read_scope':scope,'started_at':time.time()})
    try:yield {'read_scope':scope,'fingerprints':before,'parallel_with':[w['task_id'] for w in running]}
    finally:
        with orch.mutation_guard(root):
            current=active(root)
            if current and current.get('token')==token:_path(root).unlink(missing_ok=True)
