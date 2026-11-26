"""Gaston local evolution controller (trusted developer code, not an adversarial sandbox).

Stage toolkit-only candidates, retain immutable-by-workflow baseline tests, verify
regressions in disposable copies, and activate only at a quiescent launcher boundary.
Never copy project code, settings, provider transcripts, logs or secrets into a candidate.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

MANIFEST = '.agentic/manifest.json'
ID = re.compile(r'evo-[0-9a-f]{16}\Z')
# Only uncomplicated helpers can activate unattended. Everything else needs
# a real developer confirmation at launch. Controller/entry point/tests cannot
# be rewritten by the evolution workflow itself (manual release required).
AUTO = frozenset({'.agentic/context_brief.py', '.agentic/terminal_ui.py', '.agentic/setup_i18n.py'})
PROTECTED = frozenset({'dev', 'dev.cmd', MANIFEST, '.agentic/evolution.py', '.agentic/release_audit.py', '.agentic/updater.py'})
# Exact shipped hashes of retired toolkit files. Never remove user-edited files.
OBSOLETE_RELEASE_FILES = {
    '.agentic/lead_readonly_shell.py': frozenset({
        'f9a57e9c09839294995c30d9d918c59c8cb57e4d668c30d341ff60af99786ba7',
    }),
    'docs/TEST_PLAN_FROM_CURRENT_STATE.md': frozenset({
        '638cba4995c0800d1993d4d18aea0f86863b6d111388d4e3a73b06acc3e8db7c',
        '6dadbe6857ca9d6ddd0f03427a55671f25098947153e35b59711627f930a0f60',
        '6eae43d5dd92ef1911156371e4f752c71058315f5f1da49d5c458368b41accd3',
    }),
    'docs/RELEASE_V28.md': frozenset({
        '3642d4003e8aba7177292441c6f8cbf3d281328e407b66bd98466ce50e577fbd',
        'dd860f5eed6a314d8d878a493fd7d1df9a85f96f2358345991ec477ff16eb656',
    }),
}


def _require_legacy_toolkit():
    from project_layout import central_mode
    if central_mode(): raise RuntimeError('Central engine is immutable. Use a verified signed release and `dev update`.')


def cleanup_obsolete_release_files(root):
    """Remove only retired exact toolkit copies after an idle overlay upgrade."""
    _require_legacy_toolkit()
    m = read(safe(root, MANIFEST))
    removed = []
    for rel, hashes in OBSOLETE_RELEASE_FILES.items():
        # If a local evolution manifest still owns it, do not silently alter it.
        if rel in m.get('files', {}):
            continue
        try:
            p = safe(root, rel)
            if p.is_file() and digest(p) in hashes:
                p.unlink()
                removed.append(rel)
        except (ValueError, OSError):
            continue
    return removed



def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def read(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))


def put(p, data):
    p=Path(p); atomic(p, (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode(), 0o600)


def atomic(p, data, mode=0o644):
    """Persist the file and directory entry so interruption cannot expose a partial update."""
    p=Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    fd, name=tempfile.mkstemp(prefix='.evolution-', dir=p.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(name,mode & 0o777); os.replace(name,p)
        if os.name=='posix':
            d=os.open(p.parent,os.O_RDONLY)
            try: os.fsync(d)
            finally: os.close(d)
    finally:
        if os.path.exists(name):os.unlink(name)


def safe(root, rel):
    root=Path(root).resolve(); p=Path(rel)
    if not isinstance(rel,str) or not p.parts or p.is_absolute() or '..' in p.parts or '\\' in rel or '\x00' in rel:
        raise ValueError('Unsafe relative path')
    target=root/p
    if any((root/x).is_symlink() for x in (*p.parents,p)) or target.resolve()!=target:
        raise ValueError('Linked path refused: '+rel)
    if target.exists() and not target.is_dir() and (not target.is_file() or target.stat().st_nlink!=1):
        raise ValueError('Non-regular or multiply linked path: '+rel)
    return target


def store(root):
    _require_legacy_toolkit()
    p=safe(root,'.agentic/local/evolution'); p.mkdir(parents=True,exist_ok=True,mode=0o700);return p


@contextmanager
def locked(root):
    _require_legacy_toolkit()
    from platform_compat import file_lock
    p=safe(root,'.agentic/local/evolution/operations.lock')
    with file_lock(p):
        yield


def manifest(root):
    _require_legacy_toolkit()
    import uninstall
    m=read(safe(root,MANIFEST)); files=m.get('files')
    if not isinstance(files,dict) or not files:raise ValueError('Missing toolkit manifest')
    for rel in files:
        if not uninstall._known_manifest_path(rel) or rel==MANIFEST or any(x in Path(rel).parts for x in ('local','runs','tasks','custom')) or rel=='.agentic/config.json':
            raise ValueError('Manifest is not a distributable toolkit: '+rel)
        if not safe(root,rel).is_file():raise ValueError('Missing toolkit file: '+rel)
    return m


def snapshot(root, dest, m):
    dest.mkdir(parents=True)
    for rel in m['files']:
        p=safe(root,rel); q=safe(dest,rel);q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
    rebuild(dest,m)


def rebuild(root,m, *, remove=(), add=()):
    names=(set(m['files'])-set(remove))|set(add)
    candidate={**m,'files':{p:digest(safe(root,p)) for p in sorted(names)}}
    put(safe(root,MANIFEST),candidate);return candidate


def entry(root,key):
    _require_legacy_toolkit()
    if not isinstance(key,str) or not ID.fullmatch(key):raise ValueError('Invalid evolution candidate ID')
    p=safe(root,'.agentic/local/evolution/candidates/'+key)
    if not (p/'record.json').is_file():raise ValueError('Unknown evolution candidate')
    return p


def apply_replacements(path, replacements):
    """Exact, ordered, unambiguous replacements; preserve untouched UTF-8 bytes."""
    if not path.is_file() or not isinstance(replacements, list) or not replacements:
        raise ValueError('Replacements require an existing file and nonempty edit list')
    text = path.read_bytes().decode('utf-8')
    for edit in replacements:
        if not isinstance(edit, dict) or set(edit) != {'old_text', 'new_text'}:
            raise ValueError('Replacement requires old_text and new_text')
        old, new = edit['old_text'], edit['new_text']
        if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
            raise ValueError('Replacement requires nonempty old_text and different new_text')
        if text.count(old) != 1 or text.find(old, text.find(old)+1) != -1:
            raise ValueError('Replacement must match exactly once; include more context')
        text = text.replace(old, new, 1)
    return text


def stage(root, title, root_cause, edits, regression_test, *, kind='bug', expected_benefit='', source_reviews=None, incident_id=None):
    root=Path(root).resolve()
    if kind not in ('bug','improvement'):raise ValueError('Use bug or improvement')
    if not all(isinstance(x,str) and x.strip() for x in (title,root_cause,regression_test)) or not isinstance(edits,list) or not edits:
        raise ValueError('Title, root cause, scoped edits and a new executable regression test required')
    # A new regression must have a real test method. Invocation and outcome are checked later.
    import ast
    tree=ast.parse(regression_test)
    if not any(isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name.startswith('test_') for x in ast.walk(tree)):
        raise ValueError('Regression must define at least one test method')
    if incident_id is not None:
        if not isinstance(incident_id,str) or not re.fullmatch(r'incident-[0-9a-f]{20}',incident_id) or not (root/'.agentic/local/runtime-incidents'/f'{incident_id}.json').is_file():
            raise ValueError('Unknown runtime repair incident')
    with locked(root):
        m=manifest(root);changes={};base={}
        for e in edits:
            if not isinstance(e,dict) or 'before_sha256' not in e or ('content' in e) == ('replacements' in e):
                raise ValueError('Each edit needs path, before_sha256 and exactly one of content or replacements')
            rel=e.get('path');p=safe(root,rel)
            if rel in PROTECTED or (p.parent==root/'.agentic' and (p.name.startswith('test_') or p.name=='selftest.py')):
                raise ValueError('Protected controller or baseline tests; manual release required: '+rel)
            if rel not in m['files'] and not (re.fullmatch(r'\.agentic/[a-z][a-z0-9_]*\.py',rel) and not p.exists()):
                raise ValueError('Only manifest-owned toolkit files or new toolkit Python modules: '+rel)
            if rel in changes:raise ValueError('Duplicate edit: '+rel)
            if p.exists() and not p.is_file():raise ValueError('Not a file: '+rel)
            sha=digest(p) if p.is_file() else None
            if sha!=e['before_sha256']:raise ValueError('Stale base hash: '+rel)
            # Refuse to overwrite existing developer customizations, even if the model read them.
            if rel in m['files'] and sha!=m['files'][rel]:raise ValueError('Customized toolkit file; manual reconciliation required: '+rel)
            content = apply_replacements(p, e['replacements']) if 'replacements' in e else e['content']
            if content is not None and not isinstance(content,str):raise ValueError('content must be text or null')
            if content is None and sha is None:raise ValueError('Cannot delete nonexistent file')
            if content is not None and hashlib.sha256(content.encode()).hexdigest()==sha:raise ValueError('No change: '+rel)
            changes[rel]=content;base[rel]=sha
        # Every candidate requires an exact source and regression review, independent
        # of release labels. Candidate hashes are checked again before activation.
        import code_quality
        reviewed_paths = {rel for rel in changes if code_quality.is_source(rel)} | {'regression_test'}
        code_quality.validate_reviews(source_reviews, reviewed_paths, complete=True)
        key='evo-'+secrets.token_hex(8);folder=store(root)/'candidates'/key
        folder.mkdir(parents=True,mode=0o700)
        try:
            snapshot(root,folder/'baseline',m)
            atomic(folder/'original-manifest.json',safe(root,MANIFEST).read_bytes(),0o600)
            copy=folder/'candidate';shutil.copytree(folder/'baseline',copy)
            test='.agentic/test_evolved_'+key[4:]+'.py'
            for rel,content in changes.items():
                p=safe(copy,rel)
                if content is None:p.unlink()
                else:atomic(p,content.encode(),p.stat().st_mode if p.exists() else 0o644)
            atomic(safe(copy,test),regression_test.encode())
            rebuilt=rebuild(copy,m,remove=[p for p,v in changes.items() if v is None],add=[p for p in changes if p not in m['files']]+[test])
            rec={'id':key,'title':title,'root_cause':root_cause,'kind':kind,'expected_benefit':expected_benefit,'state':'staged','created_at':time.time(),'base_manifest_sha256':digest(safe(root,MANIFEST)),'base':{**base,test:None},'candidate':{**{p:rebuilt['files'].get(p) for p in changes},test:rebuilt['files'][test]},'regression_test':test,'requires_confirmation':not set(changes)<=AUTO,'baseline_tree':read(folder/'baseline'/MANIFEST)['files'],'candidate_tree':rebuilt['files']}
            if incident_id is not None:rec['incident_id']=incident_id
            if source_reviews is not None:
                rec['source_reviews'] = {test if p == 'regression_test' else p: notes for p, notes in source_reviews.items()}
            put(folder/'record.json',rec)
        except BaseException:
            shutil.rmtree(folder);raise
    return {'status':'ready','candidate_id':key,'state':'staged','requires_confirmation':rec['requires_confirmation'],'candidate_path':str((folder/'candidate').relative_to(root)),'next':'Review diff, then validate_evolution. No live source changed.'}


def verify_tree(root, expected):
    for p,sha in expected.items():
        f=safe(root,p)
        if not f.is_file() or digest(f)!=sha:raise ValueError('Snapshot changed: '+p)
    actual={p.relative_to(root).as_posix() for p in Path(root).rglob('*') if p.is_file() or p.is_symlink()}
    if actual!=set(expected)|{MANIFEST}:raise ValueError('Unexpected snapshot files')
    if read(safe(root,MANIFEST)).get('files')!=expected:raise ValueError('Snapshot manifest altered')


def run_check(root, argv, log, timeout=420, *, cancel_event=None, progress=None):
    # No inherited provider credentials or workspace configuration. This is
    # fixture isolation, not a network/OS sandbox against malicious code.
    with tempfile.TemporaryDirectory(prefix='gaston-evolve-home-') as home:
        env={'PATH':os.environ.get('PATH',os.defpath),'HOME':home,'USERPROFILE':home,'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','XDG_CONFIG_HOME':home,'CODEX_HOME':home,'CLAUDE_CONFIG_DIR':home}
        with Path(log).open('wb') as out:
            from platform_compat import popen_kwargs
            proc=subprocess.Popen([sys.executable,*argv],cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,**popen_kwargs())
            started=time.monotonic();last=started;code=None
            try:
                while True:
                    now=time.monotonic()
                    if (cancel_event is not None and cancel_event.is_set()) or now-started>=timeout:break
                    try:code=proc.wait(timeout=min(1,max(.05,timeout-(now-started))));break
                    except subprocess.TimeoutExpired:pass
                    if progress is not None and now-last>=20:
                        progress(round(now-started,1),Path(log).stem);last=now
            finally:
                if proc.poll() is None:
                    from platform_compat import stop_owned
                    stop_owned(proc, isolated_group=True, grace_seconds=1)
    text=Path(log).read_text(encoding='utf8',errors='replace'); match=re.search(r'Ran (\d+) tests? in ',text)
    return {'exit_code':code,'tests':int(match.group(1)) if match else None,'failures':bool(re.search(r'FAILED \([^\n]*failures=\d+',text)),'errors':bool(re.search(r'FAILED \([^\n]*errors=\d+',text)),'log':str(Path(log).name),'tail':text[-2500:]}


def validate(root,key,*,cancel_event=None,progress=None):
    root=Path(root).resolve()
    with locked(root):
        folder=entry(root,key); rec=read(folder/'record.json')
        if rec['state'] not in ('staged','failed','validated'):raise ValueError('Candidate cannot be validated in this state')
        if digest(safe(root,MANIFEST))!=rec['base_manifest_sha256']:raise ValueError('Installed toolkit changed; restage candidate')
        verify_tree(folder/'baseline',rec['baseline_tree']);verify_tree(folder/'candidate',rec['candidate_tree'])
        checks={};pattern=Path(rec['regression_test']).name
        began=time.monotonic()
        def check(p,args,name,timeout=420):
            if cancel_event is not None and cancel_event.is_set():return {'exit_code':None,'tests':None,'cancelled':True,'tail':'Validation cancelled'}
            if progress:progress(round(time.monotonic()-began,1),name)
            offset=time.monotonic()-began
            return run_check(p,args,folder/name,timeout,cancel_event=cancel_event,progress=(lambda elapsed,stage:progress(round(offset+elapsed,1),stage)) if progress else None)
        with tempfile.TemporaryDirectory(prefix='gaston-evolution-',dir=folder) as temp:
            temp=Path(temp);b=temp/'baseline';c=temp/'candidate';shutil.copytree(folder/'baseline',b);shutil.copytree(folder/'candidate',c)
            shutil.copy2(folder/'candidate'/rec['regression_test'],b/rec['regression_test'])
            rebuild(b,read(b/MANIFEST),add=[rec['regression_test']])
            args=['-m','unittest','discover','-s','.agentic','-p',pattern,'-v']
            if rec['kind']=='bug':checks['reproduction_before']=check(b,args,'before.log',90)
            checks['regression_after']=check(c,args,'after.log',90)
            before=checks.get('reproduction_before')
            after=checks['regression_after']
            passed=(before is None or (before['exit_code']==1 and before['tests'] and before['failures'] and not before['errors'])) and after['exit_code']==0 and after['tests'] and 'skipped=' not in after['tail']
            if passed:
                # Candidate regression cannot replace or edit shipped tests. Use a fresh copy for the full gate.
                shutil.rmtree(c);shutil.copytree(folder/'candidate',c)
                checks['compatibility']=check(c,['-m','unittest','discover','-s','.agentic','-p','test_*.py','-v'],'compatibility.log')
                if (c/'.agentic/selftest.py').is_file():
                    checks['legacy']=check(c,['.agentic/selftest.py'],'legacy.log')
                if (folder/'baseline/.agentic/release_audit.py').is_file():
                    checks['package_audit']=check(c,['.agentic/release_audit.py'],'package-audit.log',30)
                checks['startup']=check(c,['.agentic/eng.py','--help'],'startup.log',30)
                passed=checks['compatibility']['exit_code']==0 and bool(checks['compatibility']['tests']) and checks['startup']['exit_code']==0 and ('legacy' not in checks or (checks['legacy']['exit_code']==0 and bool(checks['legacy']['tests']))) and ('package_audit' not in checks or checks['package_audit']['exit_code']==0)
        rec['validation']={'at':time.time(),'checks':checks};rec['state']='validated' if passed else 'failed';put(folder/'record.json',rec)
        return {'status':'pass' if passed else 'fail','candidate_id':key,'state':rec['state'],'checks':checks,'requires_confirmation':rec['requires_confirmation'],'next':'Queue validated candidate for next safe ./dev startup' if passed else 'Inspect real logs; stage a corrected candidate. No live changes.'}



def archive_candidate_delta(folder, record):
    """Retain the proposed changes and regression as evidence before pruning trees."""
    import difflib
    destination=folder/'changes.diff'
    if destination.is_file():
        return
    chunks=[]
    for rel in sorted(record.get('base',{})):
        before=safe(folder/'baseline',rel);after=safe(folder/'candidate',rel)
        if (not before.exists() and record['base'][rel] is not None or
                not after.exists() and record.get('candidate',{}).get(rel) is not None):
            raise ValueError('Candidate evidence is incomplete: '+rel)
        old=before.read_bytes().decode('utf-8') if before.is_file() else ''
        new=after.read_bytes().decode('utf-8') if after.is_file() else ''
        chunks.extend(difflib.unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),fromfile='baseline/'+rel,tofile='candidate/'+rel))
    atomic(destination,''.join(chunks).encode(),0o600)


def reconcile_overlay(root):
    """Retire pending/active pointers from an externally installed newer build.

    Preserve all history, patch evidence and applied snapshots. Never replay old
    source over a newly installed audited release or an interrupted journal.
    """
    import release_audit
    with locked(root):
        path=store(root)
        if (path/'journal.json').exists():return []
        manifest_now=read(safe(root,MANIFEST));build=manifest_now.get('build')
        retired=[]
        for name in ('pending.json','active.json'):
            pointer=path/name
            if not pointer.is_file():continue
            request=read(pointer);folder=entry(root,request['id']);record=read(folder/'record.json')
            old_manifest=folder/'original-manifest.json'
            if not old_manifest.is_file():continue
            old_build=read(old_manifest).get('build')
            if not old_build or not build or old_build==build:continue
            if release_audit.audit(root)['status']!='pass':
                raise ValueError('New toolkit package failed audit; preserve old evolution history')
            # Mark the historical record before removing the stale pointer.
            record['superseded_by_build']=build
            if name=='pending.json' and record.get('state')=='validated':
                archive_candidate_delta(folder,record)
                record.update(state='discarded',discarded_from='validated',discarded_at=time.time())
            put(folder/'record.json',record)
            pointer.unlink()
            retired.append({'id':request['id'],'pointer':name,'old_build':old_build,'new_build':build})
        return retired

def prune_candidates(root):
    """Keep evidence; reclaim only failed or unactivatable candidate snapshots.

    Never prune queued work, the activation journal, or any applied rollback chain.
    Fresh staged/validated work remains usable until its base changes.
    """
    with locked(root):
        path = store(root)
        if (path/'journal.json').exists():
            return []
        pending = read(path/'pending.json').get('id') if (path/'pending.json').exists() else None
        active = read(path/'active.json').get('id') if (path/'active.json').exists() else None
        current = digest(safe(root, MANIFEST))
        removed = []
        for folder in sorted((path/'candidates').glob('evo-*')):
            if folder.is_symlink() or not folder.is_dir() or folder.name in (pending, active):
                continue
            if not ID.fullmatch(folder.name) or not (folder/'record.json').is_file():
                continue
            record = read(folder/'record.json')
            state = record.get('state')
            if state not in ('failed', 'discarded') and not (state in ('staged', 'validated') and record.get('base_manifest_sha256') != current):
                continue
            if (folder/'baseline').is_dir() and (folder/'candidate').is_dir():
                archive_candidate_delta(folder,record)
            # Write the terminal decision first so an interruption cannot make
            # a partially pruned candidate appear valid. Repeated pruning is safe.
            if state != 'discarded':
                record.update(state='discarded', discarded_from=state, discarded_at=time.time())
                put(folder/'record.json', record)
            had_payload = False
            for name in ('baseline', 'candidate'):
                target = safe(folder, name)
                if target.is_dir():
                    shutil.rmtree(target)
                    had_payload = True
            if had_payload:
                removed.append(folder.name)
        return removed


def status(root):
    path=store(root); records=[]
    for p in sorted((path/'candidates').glob('evo-*')):
        if p.is_dir() and not p.is_symlink() and (p/'record.json').is_file():
            r=read(p/'record.json');records.append({k:r.get(k) for k in ('id','title','state','kind','requires_confirmation','created_at','validation','applied_at','rollback_at','incident_id','discarded_at','discarded_from','superseded_by_build')})
    return {'status':'ready','candidates':records,'pending':read(path/'pending.json') if (path/'pending.json').is_file() else None,'active':read(path/'active.json') if (path/'active.json').is_file() else None,'note':'Only verified toolkit changes can activate on an idle launcher. Project state is never copied or rolled back.'}


def queue(root,key,*,rollback=False):
    with locked(root):
        path=store(root);folder=entry(root,key);r=read(folder/'record.json')
        if (path/'pending.json').exists():raise ValueError('An evolution action is already pending; inspect or cancel it')
        if rollback:
            active=read(path/'active.json') if (path/'active.json').exists() else {}
            if active.get('id')!=key or r['state']!='applied':raise ValueError('Only the current latest applied candidate can be rolled back')
        elif r['state']!='validated':raise ValueError('Candidate has not passed independent validation')
        put(path/'pending.json',{'id':key,'operation':'rollback' if rollback else 'apply','queued_at':time.time()})
    return {'status':'ready','candidate_id':key,'operation':'rollback' if rollback else 'apply','next':'Exit managed sessions and restart ./dev. No live source is changed now.'}


def cancel(root):
    with locked(root):
        p=store(root)/'pending.json'
        if p.exists():p.unlink();return {'status':'ready','cancelled':True}
    return {'status':'ready','cancelled':False}


def idle(root):
    _require_legacy_toolkit()
    # Reconcile proven abandoned leases; preserve unknown or active owners.
    local=Path(root)/'.agentic/local'
    from session_store import Store
    store_session=Store(root)
    for provider in ('codex','claude'):
        for role in ('builder','reviewer'):
            if store_session.active(provider,role):return False
    import runtime_recovery
    runtime_recovery.recover(root)
    if any((local/'locks').glob('*')) or any((local/'native-hooks/active').glob('*.json')) or runtime_recovery.feedback_worker_busy(root, settle=True):return False
    from orchestration import active_builders
    return not active_builders(root)


def current_matches(root,expected):
    for rel,sha in expected.items():
        p=safe(root,rel)
        if (sha is None and p.exists()) or (sha is not None and (not p.is_file() or digest(p)!=sha)):raise ValueError('Installed file changed since validation: '+rel)


def restore(root,folder,expected,manifest_bytes,source):
    for rel,sha in expected.items():
        p=safe(root,rel)
        if sha is None:
            if p.exists():p.unlink()
        else:
            src=safe(folder/source,rel)
            if digest(src)!=sha:raise ValueError('Rollback or candidate copy altered: '+rel)
            atomic(p,src.read_bytes(),src.stat().st_mode)
    atomic(safe(root,MANIFEST),manifest_bytes)


def recover(root):
    path=store(root);j=path/'journal.json'
    if not j.exists():return
    info=read(j);folder=entry(root,info['id']);rec=read(folder/'record.json')
    # Interrupted activation/rollback: restore the pre-operation version, never
    # overwrite a file changed to an unrelated value after the interrupted write.
    for rel in rec['base']:
        p=safe(root,rel);sha=digest(p) if p.is_file() else None
        if sha not in (rec['base'][rel],rec['candidate'][rel]):raise ValueError('Interrupted evolution has an externally changed file: '+rel)
    source='baseline' if info['operation']=='apply' else 'candidate'
    restore(root,folder,info['previous'],(folder/'journal-manifest.json').read_bytes(),source)
    rec['state']='validated' if info['operation']=='apply' else 'applied';rec['recovered_at']=time.time();put(folder/'record.json',rec)
    if info.get('previous_active') is None:(path/'active.json').unlink(missing_ok=True)
    else:put(path/'active.json',info['previous_active'])
    (path/'pending.json').unlink(missing_ok=True);j.unlink()
    print('Gaston evolution: recovered incomplete activation; original version restored.',file=sys.stderr)


def apply_pending(root, *, confirm=None):
    root=Path(root).resolve()
    with locked(root):
        path=store(root);j=path/'journal.json';pending=path/'pending.json'
        if not (j.exists() or pending.exists()):return None
        if not idle(root):return 'deferred: managed session or Builder active'
        if j.exists():recover(root);return 'recovered'
        request=read(pending);folder=entry(root,request['id']);rec=read(folder/'record.json');op=request['operation']
        if op not in ('apply','rollback'):raise ValueError('Invalid pending action')
        previous=rec['base'] if op=='apply' else rec['candidate'];target=rec['candidate'] if op=='apply' else rec['base'];source='candidate' if op=='apply' else 'baseline'
        if rec['state']!=('validated' if op=='apply' else 'applied'):raise ValueError('Candidate state changed')
        if digest(safe(root,MANIFEST))!=rec['base_manifest_sha256' if op=='apply' else 'applied_manifest_sha256']:raise ValueError('Installed manifest changed; cancel and restage')
        current_matches(root,previous)
        if op=='apply':current_matches(root,rec['baseline_tree'])
        verify_tree(folder/'baseline',rec['baseline_tree']);verify_tree(folder/'candidate',rec['candidate_tree'])
        if op=='apply' and rec['requires_confirmation']:
            if confirm is None or not confirm(rec):return 'deferred: developer review required; candidate not activated'
        m=read(safe(root,MANIFEST));old=safe(root,MANIFEST).read_bytes();new={**m,'files':dict(m['files'])}
        for rel,sha in target.items():
            if sha is None:new['files'].pop(rel,None)
            else:new['files'][rel]=sha
        new_bytes=(json.dumps(new,ensure_ascii=False,indent=2)+'\n').encode() if op=='apply' else (folder/'original-manifest.json').read_bytes()
        if op=='rollback' and hashlib.sha256(new_bytes).hexdigest()!=rec['base_manifest_sha256']:raise ValueError('Original manifest backup altered')
        active=read(path/'active.json') if (path/'active.json').exists() else None
        atomic(folder/'journal-manifest.json',old,0o600);put(j,{'id':rec['id'],'operation':op,'previous':previous,'previous_active':active})
        try:
            restore(root,folder,target,new_bytes,source)
            check=run_check(root,['.agentic/eng.py','--help'],folder/'activation-startup.log',30)
            if check['exit_code']!=0:raise RuntimeError('Installed startup check failed; reverting')
            if op=='apply':
                rec.update(state='applied',applied_at=time.time(),applied_manifest_sha256=digest(safe(root,MANIFEST)),previous_active=active);put(path/'active.json',{'id':rec['id'],'applied_at':rec['applied_at']})
            else:
                rec.update(state='rolled_back',rollback_at=time.time())
                if rec.get('previous_active'):put(path/'active.json',rec['previous_active'])
                else:(path/'active.json').unlink(missing_ok=True)
            put(folder/'record.json',rec);pending.unlink();j.unlink()
        except BaseException:
            recover(root);raise
        return 'applied '+rec['id'] if op=='apply' else 'rolled back '+rec['id']


def export_toolkit(root):
    """Explicit local export of exact installed toolkit assets, not observations."""
    import zipfile
    root=Path(root).resolve()
    with locked(root):
        m=manifest(root);current_matches(root,m['files']);base=store(root);folder=safe(root,'.agentic/local/evolution/exports');folder.mkdir(exist_ok=True)
        release=str(m.get('release','local'))
        if not re.fullmatch(r'[A-Za-z0-9._-]+',release):raise ValueError('Invalid release label')
        # No private state exported; human must still review any source content
        # before sharing the ZIP (tests/source can contain project-specific text).
        dest=folder/('gaston-agentic-'+release+'-local-'+secrets.token_hex(5)+'.zip')
        with zipfile.ZipFile(dest,'x',compression=zipfile.ZIP_DEFLATED) as z:
            for rel in sorted(set(m['files'])|{MANIFEST}):z.write(safe(root,rel),rel)
        with zipfile.ZipFile(dest) as z:
            if z.testzip() is not None:raise RuntimeError('Export ZIP integrity failed')
    return {'status':'ready','path':str(dest.relative_to(root)),'files':len(m['files'])+1,'note':'Manifest-owned toolkit only; no task history, local lessons, provider settings or credentials files. Review source and tests for project-specific contents before sharing. No upload or publication.'}


def launch(root,args):
    """Use the same native evolution gate across all three hosts.

    Windows cannot execv while retaining an inherited shared lock; the
    launcher holds that kernel lock while waiting on its child CLI instead.
    """
    root=Path(root).resolve()
    if os.environ.get('GASTON_ENGINE_MODE') == 'central':
        # Read-only preview: do not open or create workspace evolution locks.
        # On Windows, opening the legacy lock can fail with PermissionError
        # even when no tool update is requested. There is no evolution to
        # coordinate for --help; run the centrally packaged entrypoint only.
        if list(args) != ['--help']:
            raise RuntimeError('Central preview supports dev --help only')
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / 'eng.py'), '--help'],
            cwd=root, check=False)
        if completed.returncode:
            raise SystemExit(completed.returncode)
        return
    from platform_compat import open_lock, lock_fd, unlock_fd
    path=safe(root,'.agentic/local/evolution-launch.lock')
    fd=open_lock(path)
    try:
        if not args:
            try:
                lock_fd(fd,nonblocking=True)
            except BlockingIOError:
                pass
            else:
                try:
                    def confirm(rec):
                        if not sys.stdin.isatty():return False
                        print('Gaston validated candidate needs developer review: '+rec['id']+' — '+rec['title'],file=sys.stderr)
                        print('Review '+str(entry(root,rec['id'])/'candidate')+' against baseline. Type APPLY '+rec['id']+' to approve:',file=sys.stderr)
                        return input().strip()=='APPLY '+rec['id']
                    try:
                        if not (store(root)/'journal.json').exists() and idle(root):
                            for rel in cleanup_obsolete_release_files(root):
                                print('Gaston: removed obsolete shipped '+rel,file=sys.stderr)
                        if idle(root):
                            for record in reconcile_overlay(root):
                                print('Gaston evolution: preserved superseded '+record['pointer']+' for '+record['id'],file=sys.stderr)
                        result=apply_pending(root,confirm=confirm)
                        if result:print('Gaston evolution: '+result,file=sys.stderr)
                        if idle(root):
                            for key in prune_candidates(root):
                                print('Gaston evolution: removed abandoned candidate snapshots '+key,file=sys.stderr)
                    except (OSError,ValueError,RuntimeError) as exc:
                        if (store(root)/'journal.json').exists():raise
                        print('Gaston evolution: not activated: '+str(exc),file=sys.stderr)
                finally:
                    unlock_fd(fd)
        lock_fd(fd,shared=True)
        try:
            if (store(root)/'journal.json').exists():
                raise RuntimeError('Interrupted evolution requires recovery. Close managed sessions and restart dev.')
            command=[sys.executable,str(root/'.agentic/eng.py'),*args]
            if os.name=='nt':
                completed=subprocess.run(command,cwd=root)
                if completed.returncode:
                    raise SystemExit(completed.returncode)
            else:
                # Keep original POSIX semantics: the native CLI inherits our lock.
                os.set_inheritable(fd,True)
                os.execv(sys.executable,command)
        finally:
            unlock_fd(fd)
    finally:
        os.close(fd)


if __name__=='__main__':
    if len(sys.argv)<2 or sys.argv[1]!='--launch':raise SystemExit('Internal Gaston launcher; use dev')
    from host_setup import refresh_tool_path
    refresh_tool_path()
    launch(Path(__file__).resolve().parent.parent,sys.argv[2:])
