"""Reserve managed verification resources without serializing disjoint repositories."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json, os, re, secrets, socket, time
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

def _archive_interrupted(root, row, *, proof):
    archive=project_state(root)/'local'/'interrupted-verifications'/('reservation-'+secrets.token_hex(8)+'.json')
    orch.put(archive,{**row,'recovered_at':time.time(),'outcome':'interrupted_no_acceptance','recovery_proof':proof})
    _path(root).unlink()


def _recover_proven_windows_job(root, row):
    """Call only under mutation_guard. Never clear a live or unknown owner.

    Only single-command Flutter checks use this marker. Mobile, browser and
    backend checks can own other resources, so retain their manual recovery.
    Old releases did not record a named job; do not infer safety for them.
    """
    if not row or row.get('owner_state')!='dead' or row.get('kind') not in ('flutter_analyze','flutter_tests'):
        return False
    owner=row.get('owner') or {}
    token=row.get('token')
    if (not owner.get('start_ticks') or owner.get('host')!=socket.gethostname()
            or not isinstance(token,str) or not re.fullmatch(r'[0-9a-f]{32}',token)):
        return False
    from platform_compat import windows_session_id, windows_verification_job_gone
    session=windows_session_id()
    name='Local\\GastonVerification-'+token
    if (session is None or row.get('windows_session')!=session
            or row.get('windows_job')!=name
            or row.get('windows_cleanup') not in ('not_started','confirmed')
            or not windows_verification_job_gone(name)):
        return False
    # Disappearance of a name is NOT proof of termination. Recovery needs
    # a durable receipt from the runner or proof it never began.
    _archive_interrupted(root,row,proof='dead_owner_with_positive_windows_cleanup_receipt')
    return True


def _transition_windows_cleanup(root, name, *, previous, following):
    with orch.mutation_guard(root):
        row=active(root)
        if (not row or row.get('windows_job')!=name
                or (row.get('owner') or {}).get('pid')!=os.getpid()
                or row.get('owner_state')!='live'
                or row.get('windows_cleanup')!=previous):
            raise ValueError('Cannot establish ownership of Windows verification reservation')
        row.pop('owner_state',None)
        row['windows_cleanup']=following
        orch.put(_path(root),row)


def begin_windows_command(root, name):
    # Persist before any command process can be launched.
    _transition_windows_cleanup(root,name,previous='not_started',following='running')


def confirm_windows_cleanup(root, name):
    # Called only after the actual owned job/process wait succeeded.
    _transition_windows_cleanup(root,name,previous='running',following='confirmed')


def recover_proven_interruption(root):
    with orch.mutation_guard(root):
        return _recover_proven_windows_job(root,active(root))


def recover_reservation(root,*,developer_confirmed=False):
    if developer_confirmed is not True:raise ValueError('Developer confirmation of terminated owned runtime processes is required')
    with orch.mutation_guard(root):
        row=active(root)
        if not row:return {'status':'ready','note':'No stale verification reservation'}
        if row['owner_state']!='dead':raise ValueError('Verification owner is live or cannot be verified dead; do not clear')
        _archive_interrupted(root,row,proof='developer_confirmed_terminated_processes')
        return {'status':'ready','note':'Archived interrupted reservation; rerun the actual gate. No evidence passed.'}


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
    if integrated or kind not in ('api','backend','web_verify','web','visual_web'):return ['.']
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
        descriptor=folder/('gaston-e2e.json' if kind=='api' else 'gaston-web-e2e.json' if kind in ('web','visual_web') else 'gaston-verification.json')
        if descriptor.is_symlink():return ['.']
        if kind in ('api','web','visual_web') and not descriptor.is_file():return ['.']
        data=json.loads(descriptor.read_text(encoding='utf-8')) if descriptor.is_file() else {}
        extra=data.get('read_projects',[])
        if kind in ('web','visual_web'):
            # Explicit managed relationship, never parallelize unknown external URLs.
            backend=data.get('backend_project')
            if not isinstance(backend,str) or not isinstance(extra,list):return ['.']
            extra=[backend,*extra]
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
        if prior and _recover_proven_windows_job(root,prior):prior=None
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
        job={}
        if kind in ('flutter_analyze','flutter_tests'):
            from platform_compat import windows_session_id
            session=windows_session_id()
            if session is not None:
                job={'windows_job':'Local\\GastonVerification-'+token,'windows_session':session,
                     'windows_cleanup':'not_started'}
        orch.put(_path(root),{'token':token,'owner':{'pid':os.getpid(),'host':socket.gethostname(),**process_identity(os.getpid())},
                              'kind':kind,'project':project,'task_id':task_id,'read_scope':scope,'started_at':time.time(),**job})
    try:yield {'read_scope':scope,'fingerprints':before,'parallel_with':[w['task_id'] for w in running],**job}
    finally:
        with orch.mutation_guard(root):
            current=active(root)
            if current and current.get('token')==token:
                # The job's namespace entry may disappear before children exit.
                # Keep an interrupted/failed cleanup reserved until positive proof.
                if job:
                    from platform_compat import windows_verification_job_gone
                    if (current.get('windows_cleanup') not in ('not_started','confirmed')
                            or not windows_verification_job_gone(job['windows_job'])):
                        raise ValueError('Windows verification job cleanup is unconfirmed; reservation preserved')
                _path(root).unlink(missing_ok=True)
