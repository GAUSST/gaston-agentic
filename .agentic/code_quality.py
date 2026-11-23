"""Lead semantic review for current source; static hints are not quality scores."""
from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import time
import tokenize

import acceptance_audit
import orchestration
import task_runtime

SOURCE_SUFFIXES = frozenset({'.py', '.java', '.kt', '.kts', '.js', '.jsx', '.ts', '.tsx', '.dart', '.go', '.rs', '.cs', '.c', '.h', '.cpp', '.hpp', '.rb', '.php', '.swift', '.sql', '.sh', '.bash', '.vue', '.svelte', '.scala', '.groovy', '.ex', '.exs', '.lua', '.r', '.m', '.mm', '.pl', '.ps1', '.html', '.css', '.scss'})
DIMENSIONS = ('clarity', 'difficult_logic', 'maintainability', 'correctness')
RECORD = 'lead-code-review.json'


def is_source(path):
    p = Path(path)
    return p.suffix.lower() in SOURCE_SUFFIXES or p.name in ('Dockerfile', 'Makefile', 'dev')


def validate_reviews(reviews, paths, *, complete=False):
    """Require meaningful notes on exact paths, optionally complete coverage."""
    if not isinstance(reviews, dict) or set(reviews) - set(paths) or (complete and set(reviews) != set(paths)):
        raise ValueError('Review exact required source paths: ' + ", ".join(sorted(paths)))
    for path, notes in reviews.items():
        if not isinstance(notes, dict) or set(notes) != set(DIMENSIONS) or not all(
                isinstance(v, str) and v.strip() for v in notes.values()):
            raise ValueError('Each file requires factual clarity, difficult_logic, maintainability and correctness findings: ' + path)


def source_paths(folder):
    """Include tests and removed source, not just Builder-reported production code."""
    changed = task_runtime.read_record(folder / 'build.json').get('changed_files', [])
    return sorted(p for p in set(changed) | orchestration.lead_paths(folder)
                  if isinstance(p, str) and is_source(p))


def required(folder):
    return task_runtime.read_record(folder / 'task.json').get('code_review_policy') == 38


def _boundary(folder):
    worker = task_runtime.read_record(folder / 'worker.json')
    finish = task_runtime.read_record(folder / 'worker-finished.json')
    return max((float(v) for v in (worker.get('started_at'), finish.get('finished_at'))
                if isinstance(v, (int, float))), default=0)


def report(folder):
    paths = source_paths(folder)
    prior = task_runtime.read_record(folder / RECORD)
    if not required(folder) and not prior:
        return {'status': 'not_required', 'required_files': [], 'note': 'Historical task; original policy preserved.'}
    gaps = prior.get('findings', [])
    if not paths and not gaps:
        return {'status': 'not_required', 'required_files': []}
    fingerprint = acceptance_audit.revision_fingerprint(folder,review_only=True)
    fresh = (fingerprint is not None and (prior.get('runtime_revision_fingerprint') == fingerprint if 'runtime_revision_fingerprint' in prior else prior.get('revision_fingerprint') == acceptance_audit.revision_fingerprint(folder))
             and isinstance(prior.get('reviewed_at'), (int, float))
             and prior['reviewed_at'] >= _boundary(folder))
    notes = prior.get('files', {})
    if not isinstance(notes, dict):
        notes = {}
    missing = [p for p in paths if not fresh or p not in notes or not isinstance(notes[p], dict)
               or set(notes[p]) != set(DIMENSIONS) or not all(isinstance(v, str) and v.strip() for v in notes[p].values())]
    return {'status': 'pass' if not missing and not gaps else 'incomplete',
            'required_files': paths, 'missing_files': missing, 'open_findings': gaps,
            'note': 'Lead review attestation, not automated proof of semantic quality. No mandatory comment count or complexity score.'}


def record(folder, reviews, findings, resolution=''):
    paths = source_paths(folder)
    validate_reviews(reviews, paths)
    if not isinstance(findings, list) or not all(isinstance(x, str) and x.strip() for x in findings):
        raise ValueError('List unresolved material findings, or [] after inspection')
    previous = task_runtime.read_record(folder / RECORD)
    if set(previous.get('findings', [])) - set(findings) and not (isinstance(resolution, str) and resolution.strip()):
        raise ValueError('Explain how previously open findings were resolved and rechecked')
    fingerprint = acceptance_audit.revision_fingerprint(folder,review_only=True)
    if fingerprint is None:
        raise ValueError('Cannot bind review to actual source revision')
    # Allow a large review to be recorded incrementally without repeating notes.
    # Once any task source or worker attempt changes, re-review the current revision.
    prior_notes = {}
    if ((previous.get('runtime_revision_fingerprint') == fingerprint if 'runtime_revision_fingerprint' in previous else previous.get('revision_fingerprint') == acceptance_audit.revision_fingerprint(folder)) and
            isinstance(previous.get('reviewed_at'), (int, float)) and previous['reviewed_at'] >= _boundary(folder)):
        prior_notes = {p: n for p, n in previous.get('files', {}).items() if p in paths}
    orchestration.put(folder / RECORD, {'revision_fingerprint': acceptance_audit.revision_fingerprint(folder), 'runtime_revision_fingerprint': fingerprint,
                       'reviewed_at': time.time(), 'files': {**prior_notes, **reviews},
                       'findings': findings, 'resolution': resolution,
                       'had_findings': bool(findings or previous.get('had_findings') or previous.get('findings'))})
    return report(folder)


# Hints use syntax where available, otherwise lexical approximation. Do not turn
# these heuristics into rejection thresholds: a long state machine can be clearer
# than splitting every branch into a different method.
RISK_WORDS = re.compile(r'(?i)(?:concurren|lock|lease|transaction|atomic|crypt|cipher|sign(?:ature|ing)|token|round(?:ing)?|fee|decimal|idempoten|retry|backoff|replay|settlement)')
BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match, ast.With, ast.AsyncWith)


def python_hints(path, text, tree):
    try:
        comments = [t.start[0] for t in tokenize.generate_tokens(io.StringIO(text).readline)
                    if t.type == tokenize.COMMENT and t.string[1:].strip()]
    except (tokenize.TokenError, IndentationError):
        comments = []
    hints = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start, end = fn.lineno, fn.end_lineno or fn.lineno
        branches = sum(isinstance(n, BRANCHES) for n in ast.walk(fn))
        # Evidence of interactions, not size alone. Comments merely help surface
        # candidates; a Lead still verifies that they explain the actual invariant.
        if branches >= 7:
            hints.append({'kind': 'complex_control_flow_candidate', 'path': path, 'line': start,
                          'symbol': fn.name, 'branch_nodes': branches})
        if branches >= 2 and RISK_WORDS.search(fn.name) and not ast.get_docstring(fn) and not any(start <= n <= end for n in comments):
            hints.append({'kind': 'difficult_logic_comment_review', 'path': path, 'line': start,
                          'symbol': fn.name, 'reason': 'Risk-sensitive name and branching without local explanation; inspect actual behavior.'})
    return hints


LEXICAL = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')


def brace_hints(path, text):
    """Approximate Java/JS/TS/Dart block scanning, ignoring comments and strings."""
    clean = LEXICAL.sub(lambda m: ''.join('\n' if c == '\n' else ' ' for c in m.group()), text)
    hints = []
    # Restrict this heuristic to named callable blocks with a locally balanced body.
    for m in re.finditer(r'\b([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*(?:throws\s+[\w., ]+\s*)?\{', clean):
        name = m.group(1)
        if name in ('if', 'for', 'while', 'switch', 'catch', 'synchronized', 'with', 'when'):
            continue
        depth, end = 1, m.end()
        for token in re.finditer(r'[{}]', clean[m.end():]):
            depth += 1 if token.group() == '{' else -1
            if depth == 0:
                end = m.end() + token.end()
                break
        else:
            continue
        body = clean[m.end():end]
        branches = len(re.findall(r'\b(?:if|for|while|switch|catch|case)\b|&&|\|\|', body))
        line = clean.count('\n', 0, m.start()) + 1
        if branches >= 7:
            hints.append({'kind': 'complex_control_flow_candidate', 'path': path, 'line': line,
                          'symbol': name, 'branch_tokens': branches, 'approximate': True})
        original = text[m.start():end]
        commented = any(x.group().startswith(('//', '/*')) for x in LEXICAL.finditer(original))
        if branches >= 2 and RISK_WORDS.search(name) and not commented:
            hints.append({'kind': 'difficult_logic_comment_review', 'path': path, 'line': line,
                          'symbol': name, 'approximate': True, 'reason': 'Inspect invariant and nearby documentation; lexical hint only.'})
    return hints


RELEASE_DOC = re.compile(r'(?i)^release[_-]v?(\d+)(?:[._-]\d+)*\.(?:md|rst|txt)$')


def obsolete_document_hints(root, paths):
    """Versioned release documents are review candidates, never generic deletion targets."""
    import evolution
    manifest = task_runtime.read_record(root / '.agentic/manifest.json')
    version = re.fullmatch(r'v(\d+)', str(manifest.get('release', '')))
    results = []
    for p in paths:
        m = RELEASE_DOC.fullmatch(p.name)
        if not m:
            continue
        rel = p.relative_to(root).as_posix()
        owned = rel in manifest.get('files', {})
        known = rel in evolution.OBSOLETE_RELEASE_FILES
        if known or not owned or not version or int(m.group(1)) < int(version.group(1)):
            results.append({'kind': 'historical_release_document_candidate', 'path': rel,
                            'manifest_owned': owned, 'known_retired_toolkit_path': known,
                            'reason': 'Check current applicability, references, historical retention and ownership before removal.'})
    return results
