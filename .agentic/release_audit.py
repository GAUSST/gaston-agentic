"""Deterministic audit of the complete manifest-owned toolkit, not project state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

MANIFEST = '.agentic/manifest.json'
HISTORICAL_HEADING = re.compile(r'^#{1,6}\s+.*\bv\d{2,}\b', re.M | re.I)


def audit(root):
    """Check every declared file; semantic and provider verification remain separate."""
    import uninstall

    root = Path(root).resolve()
    manifest = json.loads((root / MANIFEST).read_text(encoding='utf-8'))
    release, build = manifest.get('release'), manifest.get('build')
    files = manifest.get('files', {})
    failures = []
    python_count = docs_count = 0
    if not isinstance(release, str) or not re.fullmatch(r'v\d+\.\d+\.\d+', release):
        failures.append('Invalid canonical release')
    if not isinstance(build, str) or not re.fullmatch(r'\d+\.\d+\.\d+', build) or not isinstance(release, str) or build != release.removeprefix('v'):
        failures.append('Canonical build does not match release')
    if not isinstance(files, dict) or not files:
        raise ValueError('Missing or invalid manifest file map')
    for rel, expected in sorted(files.items()):
        path = root / rel
        if (not uninstall._known_manifest_path(rel) or rel == MANIFEST or rel == '.agentic/config.json'
                or any(part in {'local', 'runs', 'tasks', 'custom', '__pycache__'} for part in Path(rel).parts)
                or path.resolve() != path or path.is_symlink() or not path.is_file()):
            failures.append('Missing or non-distributable path: ' + rel)
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            failures.append('Manifest hash mismatch: ' + rel)
        if path.suffix == '.py':
            python_count += 1
            try:
                compile(data, rel, 'exec')
            except (SyntaxError, ValueError) as exc:
                failures.append(f'{rel}: {exc}')
        elif path.suffix == '.md':
            docs_count += 1
            if rel != 'docs/LESSONS.md' and HISTORICAL_HEADING.search(data.decode('utf-8')):
                failures.append('Historical release heading in operational guidance: ' + rel)
    if 'AGENTS.md' not in files or 'CLAUDE.md' not in files:
        failures.append('Missing shared or provider-specific engineering contract')
    elif (root / 'CLAUDE.md').read_text(encoding='utf-8').splitlines().count('@AGENTS.md') != 1:
        failures.append('Claude must import the canonical shared contract exactly once')
    for retired in ('docs/RELEASE_V28.md', 'docs/TEST_PLAN_FROM_CURRENT_STATE.md', '.agentic/lead_readonly_shell.py'):
        if retired in files:
            failures.append('Retired toolkit file still distributed: ' + retired)
    return {'status': 'pass' if not failures else 'fail', 'release': release, 'build': build,
            'manifest_files': len(files), 'python_files': python_count, 'markdown_files': docs_count,
            'failures': failures}


if __name__ == '__main__':
    result = audit(Path(__file__).resolve().parent.parent)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'pass' else 1)
