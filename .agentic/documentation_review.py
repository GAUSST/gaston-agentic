"""Current-revision document/review-gap attestations (not semantic proof)."""
from pathlib import Path, PurePosixPath
import hashlib,json,time
import acceptance_audit
import task_runtime


def required_paths(paths):
    result=[]
    for value in paths or []:
        if not isinstance(value,str):continue
        p=PurePosixPath(value.replace('\\','/')); name=p.name.lower()
        if p.is_absolute() or '..' in p.parts:continue
        if p.suffix.lower() in ('.md','.rst','.adoc','.puml','.plantuml','.uml','.drawio','.mmd') or (
                ('docs' in p.parts or 'documentation' in p.parts) and p.suffix.lower() in ('.txt','.yaml','.yml','.json','.xml','.pdf','.svg','.html')) or (
                any(x in name for x in ('openapi','swagger','asyncapi')) and p.suffix.lower() in ('.json','.yaml','.yml')):
            result.append(p.as_posix())
    return sorted(set(result))


def _changed_documents(folder):
    from orchestration import lead_paths
    return required_paths(list(set(task_runtime.read_record(folder/'build.json').get('changed_files',[]))|lead_paths(folder)))

def _required(folder):
    task=task_runtime.read_record(folder/'task.json')
    return sorted(set(_changed_documents(folder))|set(required_paths(task.get('required_documentation_paths',[]))))


def _document_digest(folder, name, *, allow_deleted=False):
    root=folder.parent.parent.parent.resolve()
    raw=Path(name)
    if not name or raw.is_absolute() or '..' in raw.parts or any(part.startswith('.') for part in raw.parts):
        raise ValueError('Unsafe documentation path: '+name)
    if raw.suffix.lower() not in ('.md','.rst','.adoc','.puml','.plantuml','.uml','.drawio','.mmd','.txt','.yaml','.yml','.json','.xml','.pdf','.svg','.html'):
        raise ValueError('Choose a real documentation or contract file: '+name)
    path=root/raw
    if not path.resolve().is_relative_to(root) or any((root/parent).is_symlink() for parent in (raw,*raw.parents)):
        raise ValueError('Unsafe documentation path: '+name)
    if allow_deleted and not path.exists():return '<deleted>'
    if not path.is_file():raise ValueError('Relevant document not found: '+name)
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def report(folder):
    required=_required(folder)
    previous=task_runtime.read_record(folder/'lead-documentation-review.json')
    prior_notes=previous.get('documents',{})
    if not isinstance(prior_notes,dict):prior_notes={}
    # Relevant unchanged specifications and previously found gaps remain review
    # obligations across revisions. Do not silently discard an open finding.
    required=sorted(set(required)|set(prior_notes))
    gaps=previous.get('unresolved_gaps',[])
    if not isinstance(gaps,list):gaps=['Invalid prior gap record; re-review required']
    if not required and not gaps:return {'status':'not_required','required_documents':[],'missing_documents':[],'unresolved_gaps':[]}
    fingerprint=acceptance_audit.revision_fingerprint(folder)
    worker=task_runtime.read_record(folder/'worker.json'); finish=task_runtime.read_record(folder/'worker-finished.json')
    boundary=max((float(x) for x in (worker.get('started_at'),finish.get('finished_at')) if isinstance(x,(int,float))),default=0)
    fresh=(fingerprint is not None and previous.get('revision_fingerprint')==fingerprint and isinstance(previous.get('reviewed_at'),(int,float)) and previous['reviewed_at']>=boundary)
    missing=[]
    for p in required:
        try:digest=_document_digest(folder,p,allow_deleted=p in _changed_documents(folder))
        except (OSError,ValueError):digest=None
        if not fresh or not isinstance(prior_notes.get(p),str) or not prior_notes[p].strip() or digest is None or previous.get('document_digests',{}).get(p)!=digest:
            missing.append(p)
    return {'status':'pass' if not missing and not gaps else 'incomplete','required_documents':required,
            'missing_documents':missing,'unresolved_gaps':gaps,
            'note':'Lead per-document review attestation for this revision; not automated semantic validation.'}


def record(folder,documents=None,gaps=None):
    required=_required(folder)
    if documents is None and gaps is None:return report(folder)
    if not isinstance(documents,dict) or not all(isinstance(k,str) and isinstance(v,str) and v.strip() for k,v in documents.items()):
        raise ValueError('documentation_reviews must map exact changed and relevant documentation paths to specific review findings')
    if not isinstance(gaps,list) or not all(isinstance(v,str) and v.strip() for v in gaps):
        raise ValueError('documentation_gaps must list unresolved material gaps, or [] after checking all relevant documentation and implementation')
    previous=task_runtime.read_record(folder/'lead-documentation-review.json')
    # A final review replaces evidence, but must not silently drop relevant
    # documents from an earlier review. Keep them in the obligation set.
    prior=previous.get('documents',{})
    if not isinstance(prior,dict):prior={}
    omitted=set(prior)-set(documents)
    if omitted:raise ValueError('Re-review previously reviewed relevant documents: '+', '.join(sorted(omitted)))
    fingerprint=acceptance_audit.revision_fingerprint(folder)
    if (required or documents) and fingerprint is None:raise ValueError('Cannot bind documentation review to current changed files')
    digests={p:_document_digest(folder,p,allow_deleted=p in _changed_documents(folder)) for p in documents}
    saved={'revision_fingerprint':fingerprint,'reviewed_at':time.time(),'documents':documents,'document_digests':digests,'unresolved_gaps':gaps}
    (folder/'lead-documentation-review.json').write_text(json.dumps(saved,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    return report(folder)
