"""Evidence-based local learning and code-health candidates; no extra AI calls."""
from __future__ import annotations
import ast
import hashlib
import os
from pathlib import Path
import re
import time
from collections import Counter, defaultdict
import evolution as evo
import code_quality
from task_runtime import read_record, task_metrics


RULES={
 'repeated_corrections':'Split implementation by dependency and verify the narrow failing behavior before another broad change. Lead takes over at the existing two-correction cutoff.',
 'provider_quota':'Resume the same task and preserve partial edits; do not treat provider quota as an implementation defect.',
 'test_fixture':'Check the executable runner contract and reproduce fixture failure before revising application code.',
 'environment':'Diagnose prerequisites and isolate the environment before requesting implementation changes.',
 'specification':'Resolve conflicting requirements and record a precise contract before implementing.',
 'architecture':'Check module boundaries and business invariants before implementation; test the violated boundary.',
 'implementation':'Add a targeted regression for the reproduced fault; independently verify the affected workflow.',
 'regression':'Add a regression reproducing the previous passing behavior and verify the changed integration.',
 'live_api_failure':'Exercise the real running service and negative cases; a green unit suite alone is not sufficient.',
 'documentation_gap':'Read relevant final documents against actual code and resolve material inconsistency before acceptance.',
 'code_quality_gap':'Inspect actual changed code; simplify unnecessarily complex logic, clarify naming and document non-obvious invariants before acceptance.',
}


def collect(root, *, limit=100):
    """Stable one-record-per-task evidence, no raw logs/goals/secret contents stored."""
    root=Path(root); base=evo.store(root); path=base/'observations.json'
    with evo.locked(root):
        old=read_record(path);obs=old.get('tasks',{}) if isinstance(old.get('tasks'),dict) else {}
        folders=[f for f in (root/'.agentic/runs').glob('*') if re.fullmatch(r'[a-z0-9-]{8,120}',f.name) and not f.is_symlink() and f.is_dir() and (f/'state.json').is_file()]
        folders.sort(key=lambda p:(p/'state.json').stat().st_mtime,reverse=True)
        for f in folders[:limit]:
            inputs=[f/n for n in ('state.json','task.json','worker.json','worker-finished.json','takeover.json','lead-backend-verification.json','acceptance-evidence.json','lead-documentation-review.json','lead-code-review.json','lead-edits.json','build.json','scope-observation.json','scope-reconciliation.json')]
            for pattern in ('revision-request*.json','attempt-*.json','handoff-*.json','build*.log','build*.usage.json'):inputs.extend(f.glob(pattern))
            stamp=hashlib.sha256(('v46-native-delta:'+repr(sorted((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in set(inputs) if p.is_file() and not p.is_symlink()))).encode()).hexdigest()
            if obs.get(f.name,{}).get('source_stamp')==stamp:continue
            state=read_record(f/'state.json');metrics=task_metrics(f); signal=set()
            revs=[read_record(p) for p in f.glob('revision-request*.json') if not p.is_symlink()]
            for rev in revs:
                kind=rev.get('failure_kind')
                if kind in RULES:signal.add(kind)
            if (metrics.get('correction_rounds') or 0)>=2 or metrics.get('lead_takeover'):signal.add('repeated_corrections')
            if metrics.get('quota_interruptions_observed') or state.get('blocker')=='provider_quota':signal.add('provider_quota')
            try:
                import acceptance_audit as audit
                checks=audit.read(f)
            except (ValueError,OSError,TypeError):checks=[]
            if any(isinstance(x,dict) and x.get('kind')=='api' and x.get('result')=='fail' for x in checks):signal.add('live_api_failure')
            docs=read_record(f/'lead-documentation-review.json')
            if docs.get('gaps') or docs.get('unresolved_gaps'):signal.add('documentation_gap')
            quality=read_record(f/'lead-code-review.json')
            if quality.get('findings') or quality.get('had_findings'):signal.add('code_quality_gap')
            metric_fields=('handoff_count','builder_attempts','handoff_prompt_bytes','delta_handoffs','correction_rounds','quota_interruptions_observed','recorded_test_checks','full_backend_checks','managed_http_checks','worker_elapsed_seconds','provider_usage','lead_takeover','lead_edited_file_count','scope_reported_count','scope_pending_count','scope_observed_not_attributed_count','compile_preflight')
            previous=obs.get(f.name,{})
            obs[f.name]={'source_stamp':stamp,'stage':state.get('stage'),'signals':sorted(set(previous.get('signals',[]))|signal),'metrics':{k:metrics.get(k) for k in metric_fields},'accepted':state.get('stage')=='ready_for_human_release_review' and bool(state.get('lead_verified'))}
        evo.put(path,{'version':1,'tasks':obs})
    counts=Counter(s for value in obs.values() for s in value.get('signals',[]));tokens=Counter();usage_tasks=0
    for value in obs.values():
        record=value.get('metrics',{}).get('provider_usage') or {}
        usage=record.get('tokens') if record.get('source')!='legacy_cli_unverified' else None
        if isinstance(usage,dict):usage_tasks+=1;tokens.update({k:v for k,v in usage.items() if isinstance(v,int)})
    return {'status':'ready','observed_tasks':len(obs),'accepted_tasks':sum(bool(v.get('accepted')) for v in obs.values()),'signals':dict(counts),'recommendations':[{'signal':s,'observations':n,'prevention':RULES[s]} for s,n in counts.most_common() if s in RULES], 'usage':{'tasks_with_reported_codex_usage':usage_tasks,'tokens':dict(tokens) if usage_tasks else None,'tasks_without_reported_codex_usage':len(obs)-usage_tasks,'note':'Observed provider counters, not subscription billing or savings. Historical ambiguous and missing usage are excluded.'},'note':'Observations are signals, not validated root causes. Confirm evidence and add a regression before changing policy.'}


def lesson(root, *, trigger, cause, prevention, evidence, scope='project', candidate_id=None):
    """Deduped local verified lessons anchored to finalized task/evolution evidence."""
    root=Path(root).resolve()
    if scope not in ('project','toolkit') or not all(isinstance(s,str) and s.strip() for s in (trigger,cause,prevention,evidence)):
        raise ValueError('Specify trigger, root cause, concrete prevention, evidence and scope')
    if scope=='toolkit':
        if not candidate_id:raise ValueError('Toolkit lesson needs a validated evolution candidate')
        rec=evo.read(evo.entry(root,candidate_id)/'record.json')
        if rec['state'] not in ('validated','applied') or not rec.get('validation'):raise ValueError('Candidate must pass validation')
        anchor=candidate_id
    else:
        if not re.fullmatch(r'[a-z0-9-]{8,120}',evidence):raise ValueError('Project evidence must be a finalized task ID')
        f=evo.safe(root,'.agentic/runs/'+evidence);state=read_record(f/'state.json')
        if state.get('stage')!='ready_for_human_release_review' or not state.get('lead_verified'):raise ValueError('Task has no final acceptance record')
        anchor=evidence
    key=hashlib.sha256((' '.join(cause.lower().split())+'\n'+' '.join(prevention.lower().split())+'\n'+scope).encode()).hexdigest()[:20]
    with evo.locked(root):
        p=evo.store(root)/'lessons.json';data=read_record(p);items=data.get('lessons',{})
        row=items.get(key,{'id':key,'scope':scope,'trigger':trigger,'root_cause':cause,'prevention':prevention,'evidence_ids':[],'created_at':time.time()})
        row['evidence_ids']=sorted(set(row['evidence_ids'])|{anchor});items[key]=row;evo.put(p,{'version':1,'lessons':items})
    return {'status':'ready','lesson_id':key,'scope':scope,'evidence_ids':row['evidence_ids'],'note':'Local lesson, not an automatic permission or policy change. Do not store secrets or private contents; no automatic sharing.'}


def context(root):
    p=evo.store(root);obs=read_record(p/'observations.json').get('tasks',{});lessons=read_record(p/'lessons.json').get('lessons',{})
    counts=Counter(s for x in obs.values() for s in x.get('signals',[]))
    # Deterministically compact, no source code or raw transcripts. Lessons are
    # evidence notes, not instructions overriding developer policy.
    lines=['## Observed engineering signals (local; assess rather than blindly obey)']
    lines += [f'- {s}: {n} task(s). {RULES[s]}' for s,n in counts.most_common() if s in RULES]
    lines += ['','## Recorded verified lessons (local, advisory)']
    for x in sorted(lessons.values(),key=lambda x:x.get('created_at',0),reverse=True)[:12]:
        lines.append('- '+x['scope']+': '+x['prevention'][:350]+' [evidence: '+', '.join(x['evidence_ids'][:3])+']')
    return '\n'.join(lines)


EXCLUDE={'.git','.agentic','.agents','.claude','.codex','.venv','venv','node_modules','target','build','dist','__pycache__','.dart_tool','vendor'}

def health(root, relative_path='.agentic', max_files=300):
    """Conservative static hints, not proof of dead code. Python AST + Java private methods."""
    root=Path(root).resolve();target=root if relative_path=='.' else evo.safe(root,relative_path)
    if not target.exists():raise ValueError('Unknown code health path')
    if any(p in {'local','runs','tasks','.git','.env'} or p.startswith('.env') for p in Path(relative_path).parts):raise ValueError('Private/generated path')
    cap=max(1,min(2000,int(max_files)));found=[];truncated=False;document_paths=[]
    brace_suffixes={'.java','.kt','.js','.jsx','.ts','.tsx','.dart','.cs','.go','.rs','.cpp'}
    analyzed_suffixes=brace_suffixes|{'.py'}
    if target.is_file():
        paths=[target] if target.suffix.lower() in analyzed_suffixes else []
        if code_quality.RELEASE_DOC.fullmatch(target.name):document_paths.append(target)
    else:
        paths=[]
        for folder,dirs,files in os.walk(target,followlinks=False):
            dirs[:]=sorted(d for d in dirs if d not in EXCLUDE and not d.startswith('.') and d not in ('local','runs','tasks') and not (Path(folder)/d).is_symlink() and not (Path(folder)==root/'.agentic' and d in ('local','runs','tasks')))
            for name in sorted(files):
                p=Path(folder)/name
                if p.is_symlink():continue
                if code_quality.RELEASE_DOC.fullmatch(name):document_paths.append(p);continue
                if p.suffix.lower() not in analyzed_suffixes or name.startswith('test_') or name=='selftest.py':continue
                if len(paths)>=cap:truncated=True;break
                paths.append(p)
            if truncated:break
    # The default toolkit scan includes historical release docs in its docs directory.
    if relative_path=='.agentic':
        try:
            docs=evo.safe(root,'docs')
            document_paths.extend(p for p in docs.glob('*') if p.is_file() and not p.is_symlink() and code_quality.RELEASE_DOC.fullmatch(p.name))
        except ValueError:pass
    found.extend(code_quality.obsolete_document_hints(root,document_paths))
    parsed=[];all_text=[];skipped=[];total_bytes=0
    for p in paths:
        rel=p.relative_to(root).as_posix()
        try:
            size=p.stat().st_size
            if size>512_000 or total_bytes+size>8_000_000:skipped.append(rel);continue
            total_bytes+=size
            txt=p.read_text(encoding='utf-8')
            all_text.append(txt)
            if p.suffix=='.py':parsed.append((rel,txt,ast.parse(txt)))
            else:parsed.append((rel,txt,None))
        except (OSError,UnicodeError,SyntaxError):skipped.append(rel)
    corpus=Counter(re.findall(r'\b[A-Za-z_]\w*\b','\n'.join(all_text)));duplicates=defaultdict(list)
    for rel,txt,tree in parsed:
        if tree is None:
            found.extend(code_quality.brace_hints(rel,txt))
            if Path(rel).suffix.lower()!='.java':continue
            # Java framework/reflection entrypoints require human inspection.
            for m in re.finditer(r'\bprivate\s+(?:(?:static|final|synchronized)\s+)*[\w<>\[\], ?]+\s+(\w+)\s*\([^;{}]*\)\s*\{',txt):
                name=m.group(1)
                if corpus[name]==1:
                    found.append({'kind':'possibly_unused_private_method','path':rel,'line':txt.count('\n',0,m.start())+1,'symbol':name})
            continue
        found.extend(code_quality.python_hints(rel,txt,tree))
        uses=Counter(n.id for n in ast.walk(tree) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load));strings={n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)}
        for node in tree.body:
            if isinstance(node,(ast.Import,ast.ImportFrom)) and not (isinstance(node,ast.ImportFrom) and node.module=='__future__'):
                for alias in node.names:
                    name=alias.asname or alias.name.split('.')[0]
                    if name!='*' and name not in uses and name not in strings:
                        found.append({'kind':'possibly_unused_import','path':rel,'line':node.lineno,'symbol':name})
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                if node.name.startswith('_') and not node.name.startswith('__') and not node.decorator_list and corpus[node.name]==1:
                    found.append({'kind':'possibly_unused_private_function','path':rel,'line':node.lineno,'symbol':node.name})
                if len(node.body)>=4:
                    key=hashlib.sha256(ast.dump(ast.Module(body=node.body,type_ignores=[]),include_attributes=False).encode()).hexdigest()
                    duplicates[key].append({'path':rel,'line':node.lineno,'symbol':node.name})
        for node in ast.walk(tree):
            for body in (getattr(node,'body',None),getattr(node,'orelse',None),getattr(node,'finalbody',None)):
                if not isinstance(body,list):continue
                for i,n in enumerate(body[:-1]):
                    if isinstance(n,(ast.Return,ast.Raise,ast.Break,ast.Continue)):
                        found.append({'kind':'unreachable_statement_candidate','path':rel,'line':body[i+1].lineno});break
    for group in duplicates.values():
        if len(group)>1:found.append({'kind':'identical_function_body','locations':group})
    return {'status':'ready','scanned_files':len(parsed),'skipped_files':skipped,'truncated':truncated,'candidates':found[:250],'candidate_count':len(found),'document_candidates_examined':len(document_paths),'note':'Static hints only, not deletion authorization. Confirm callers including reflection, framework registrations, external clients, test-only and dynamic usage. Verify a removal with regressions. No files changed.'}
