"""Per-document content-hash review reuse plus current-revision integration review."""
from pathlib import Path, PurePosixPath
import hashlib,json,time
import acceptance_audit
import task_runtime

INTEGRATION_KEY='__integration__'


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
    previous=task_runtime.read_record(folder/'lead-documentation-review.json')
    prior_notes=previous.get('documents',{})
    if not isinstance(prior_notes,dict):prior_notes={}
    required=sorted(set(_required(folder))|set(prior_notes))
    if not isinstance(prior_notes,dict):prior_notes={}
    prior_digests=previous.get('document_digests',{})
    if not isinstance(prior_digests,dict):prior_digests={}
    gaps=previous.get('unresolved_gaps',[])
    if not isinstance(gaps,list):gaps=['Invalid prior gap record; re-review required']
    if not required and not gaps:
        return {'status':'not_required','required_documents':[],'missing_documents':[],
                'reusable_documents':[],'integration_review_required':False,'unresolved_gaps':[]}
    fingerprint=acceptance_audit.revision_fingerprint(folder)
    missing=[];reusable=[]
    for p in required:
        try:digest=_document_digest(folder,p,allow_deleted=p in _changed_documents(folder))
        except (OSError,ValueError):digest=None
        if (not isinstance(prior_notes.get(p),str) or not prior_notes[p].strip()
                or digest is None or prior_digests.get(p)!=digest):
            missing.append(p)
        else:
            reusable.append(p)
    integration_required=bool(required) and (
        not isinstance(previous.get('integration_review'),str)
        or not previous.get('integration_review','').strip()
        or previous.get('integration_revision_fingerprint')!=fingerprint
    )
    return {'status':'pass' if not missing and not gaps and not integration_required else 'incomplete',
            'required_documents':required,'missing_documents':missing,
            'reusable_documents':reusable,'integration_review_required':integration_required,
            'unresolved_gaps':gaps,
            'note':'Unchanged per-document reviews are reused by content hash. __integration__ is a separate Lead review of how the current implementation revision interacts with the relevant documents; neither attestation is automated semantic validation.'}


def record(folder,documents=None,gaps=None):
    if documents is None and gaps is None:return report(folder)
    if not isinstance(documents,dict) or not all(isinstance(k,str) and isinstance(v,str) and v.strip() for k,v in documents.items()):
        raise ValueError('documentation_reviews must map exact document paths and optional __integration__ to specific review findings')
    if not isinstance(gaps,list) or not all(isinstance(v,str) and v.strip() for v in gaps):
        raise ValueError('documentation_gaps must list unresolved material gaps, or [] after checking all relevant documentation and implementation')
    supplied=dict(documents)
    integration=supplied.pop(INTEGRATION_KEY,None)
    previous=task_runtime.read_record(folder/'lead-documentation-review.json')
    prior_notes=previous.get('documents',{})
    if not isinstance(prior_notes,dict):prior_notes={}
    prior_digests=previous.get('document_digests',{})
    if not isinstance(prior_digests,dict):prior_digests={}
    prior_reviewed=previous.get('document_reviewed_at',{})
    if not isinstance(prior_reviewed,dict):prior_reviewed={}
    required=sorted(set(_required(folder))|set(prior_notes))
    unknown=set(supplied)-set(required)
    if unknown:raise ValueError('Review paths outside the current documentation obligations: '+', '.join(sorted(unknown)))
    fingerprint=acceptance_audit.revision_fingerprint(folder)
    if required and fingerprint is None:raise ValueError('Cannot bind documentation review to current changed files')
    merged_notes=dict(prior_notes);merged_digests=dict(prior_digests);reviewed_at=dict(prior_reviewed)
    for p in required:
        digest=_document_digest(folder,p,allow_deleted=p in _changed_documents(folder))
        if p in supplied:
            merged_notes[p]=supplied[p].strip()
            merged_digests[p]=digest
            reviewed_at[p]=time.time()
    saved={
        'revision_fingerprint':fingerprint,
        'reviewed_at':time.time(),
        'documents':merged_notes,
        'document_digests':merged_digests,
        'document_reviewed_at':reviewed_at,
        'integration_review':integration.strip() if isinstance(integration,str) else previous.get('integration_review',''),
        'integration_revision_fingerprint':fingerprint if isinstance(integration,str) else previous.get('integration_revision_fingerprint'),
        'integration_reviewed_at':time.time() if isinstance(integration,str) else previous.get('integration_reviewed_at'),
        'unresolved_gaps':gaps,
    }
    (folder/'lead-documentation-review.json').write_text(json.dumps(saved,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    return report(folder)
