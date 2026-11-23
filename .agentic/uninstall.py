"""Uninstall only Gaston Agentic's workspace assets.

No deletion outside the workspace. Application repositories, provider binaries,
accounts, global MCP registrations and third-party hooks are never removed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

# Added at packaging time; proves which shipped files were not edited by the user.
MANIFEST = '.agentic/manifest.json'

# Explicit root-level and documentation files shipped with Gaston. Do not
# authorize arbitrary docs/ content: it may belong to the application.
KNOWN_ROOTS = frozenset({
    '.github/workflows/native-cli-validation.yml', '.github/workflows/release.yml', 'dev', 'gaston_cli.py', 'pyproject.toml', 'README.md', 'AGENTS.md', 'CLAUDE.md', '.gitignore',
    'docs/LESSONS.md', 'docs/UPDATES.md', 'docs/BACKEND_VERIFICATION.md',
    'docs/TEST_PLAN_FROM_CURRENT_STATE.md', 'docs/VERIFICATION.md',
    'docs/LEAD_WORKSPACE_OPERATIONS.md', 'docs/RELEASE_V28.md',
})
KNOWN_PREFIXES = ('.agentic/', '.agents/skills/', '.claude/skills/')


def _known_manifest_path(relative: str) -> bool:
    return (isinstance(relative, str) and '..' not in Path(relative).parts
            and not Path(relative).is_absolute()
            and (relative in KNOWN_ROOTS or relative.startswith(KNOWN_PREFIXES)))


def _has_symlink_parent(root: Path, relative: str) -> bool:
    # A regular file under a symlinked directory is unsafe to unlink even
    # when the file's own is_symlink() returns False.
    return any((root / parent).is_symlink()
               for parent in Path(relative).parents if parent != Path('.'))


def _toolkit_bytecode(root: Path, files: dict) -> list[Path]:
    import re
    cache = root / '.agentic' / '__pycache__'
    if cache.is_symlink() or not cache.is_dir():
        return []
    modules = {Path(rel).stem for rel in files
               if Path(rel).parent == Path('.agentic') and rel.endswith('.py')}
    return sorted(path for path in cache.iterdir()
                  if not path.is_symlink() and path.is_file()
                  and path.name.split('.', 1)[0] in modules
                  and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_-]+(?:\.opt-\d+)?\.pyc', path.name))


def _owned_hook(command: object, root: Path, provider: str) -> bool:
    """Detect only hooks installed by this toolkit, not third-party hooks."""
    if not isinstance(command, str):
        return False
    import shlex
    try:
        from platform_compat import parse_command
        args = parse_command(command)
    except ValueError:
        return False
    script = str(root / '.agentic' / 'native_hooks.py')
    return len(args) >= 3 and args[-2:] == [script, provider]


def _remove_hooks(root: Path, *, apply: bool) -> list[str]:
    changes = []
    for provider, relative in (('codex', '.codex/hooks.json'),
                               ('claude', '.claude/settings.local.json')):
        path = root / relative
        if _has_symlink_parent(root, relative) or path.is_symlink() or not path.is_file():
            continue
        try:
            config = json.loads(path.read_text(encoding='utf8'))
        except (OSError, ValueError):
            changes.append(f'KEEP {relative} (unreadable settings)')
            continue
        hooks = config.get('hooks', {})
        if not isinstance(hooks, dict):
            changes.append(f'KEEP {relative} (unexpected settings)')
            continue
        found = False
        for event, groups in list(hooks.items()):
            if not isinstance(groups, list):
                continue
            retained=[]
            for group in groups:
                if not isinstance(group,dict) or not isinstance(group.get('hooks'),list):
                    retained.append(group)
                    continue
                original=group['hooks']
                kept=[x for x in original if not (isinstance(x,dict) and
                        _owned_hook(x.get('command'),root,provider))]
                if len(kept)!=len(original): found=True
                if kept: retained.append({**group, 'hooks':kept})
            if retained: hooks[event] = retained
            else: hooks.pop(event,None)
        if not found:
            continue
        if set(config.keys()) == {'hooks'} and not hooks:
            changes.append(f'DELETE {relative} (Gaston-only hooks)')
            if apply: path.unlink()
        else:
            changes.append(f'EDIT {relative} (remove Gaston hooks only)')
            if apply:
                from native_hooks import atomic_json
                atomic_json(path, config)
    return changes


def _provider_session_files(root: Path, home: Path | None = None) -> tuple[list[Path],list[str]]:
    """Locate only exact registered Gaston sessions, never other chats.

    Provider storage is not a stable public API. Unknown formats are reported
    and deliberately retained. All matching records require project identity.
    """
    import re
    import uuid
    home=Path(home) if home is not None else Path.home()
    registry=root/'.agentic/local/sessions.json'
    if registry.is_symlink() or not registry.is_file():
        return [],['Session registry missing or untrusted; provider sessions retained']
    try:
        data=json.loads(registry.read_text(encoding='utf8'))
    except (OSError,ValueError):
        return [],['Session registry unreadable; provider sessions retained']
    if data.get('root')!=str(root):
        return [],['Session registry belongs to another checkout; provider sessions retained']
    ids={'claude':set(),'codex':set()}
    def add(provider, entry):
        sid=entry.get('id') if isinstance(entry,dict) else None
        try:
            if provider in ids and str(uuid.UUID(sid))==sid.lower():ids[provider].add(sid.lower())
        except (ValueError,TypeError,AttributeError):pass
    for provider,roles in data.get('sessions',{}).items():
        if isinstance(roles,dict):
            for entry in roles.values():add(provider,entry)
    for old in data.get('archive',[]):
        if isinstance(old,dict):add(old.get('provider'),old)
    found=[];notes=[]
    # CLAUDE_CONFIG_DIR may be set to a dedicated tree. Never walk arbitrary
    # project folders: only inspect project directories with exact cwd identity.
    claude_base=Path(os.environ.get('CLAUDE_CONFIG_DIR') or home/'.claude')
    encoded=re.sub(r'[^A-Za-z0-9]', '-',str(root))
    claude_proj=claude_base/'projects'/encoded
    if claude_proj.is_dir() and not claude_proj.is_symlink():
        for sid in sorted(ids['claude']):
            path=claude_proj/(sid+'.jsonl')
            if path.is_symlink() or not path.is_file():continue
            try:
                # Require actual session or cwd evidence, not just a filename.
                with path.open(encoding='utf8') as f:
                    lines=[f.readline() for _ in range(12)]
                evidence=False
                for line in lines:
                    if not line.strip():continue
                    item=json.loads(line)
                    if isinstance(item,dict) and item.get('sessionId')==sid and item.get('cwd')==str(root):
                        evidence=True;break
                if evidence:found.append(path)
                else:notes.append('KEEP '+str(path)+' (unverified transcript identity)')
            except (OSError,ValueError):
                notes.append('KEEP '+str(path)+' (cannot inspect transcript)')
    # Codex stores sessions by date. Match UUID suffix AND session_meta.cwd.
    codex_base=Path(os.environ.get('CODEX_HOME') or home/'.codex')
    for store in ('sessions','archived_sessions'):
        base=codex_base/store
        if not base.is_dir() or base.is_symlink():continue
        for path in base.rglob('*.jsonl'):
            if path.is_symlink() or not path.is_file():continue
            sid=next((sid for sid in ids['codex'] if path.name.endswith('-'+sid+'.jsonl')),None)
            if not sid:continue
            try:
                first=json.loads(path.open(encoding='utf8').readline())
                meta=first.get('payload',{})
                if first.get('type')=='session_meta' and meta.get('id')==sid and meta.get('cwd')==str(root):
                    found.append(path)
                else:notes.append('KEEP '+str(path)+' (unverified session metadata)')
            except (OSError,ValueError):notes.append('KEEP '+str(path)+' (cannot inspect metadata)')
    # Claude Desktop metadata is optional and may use its own storage directory;
    # it is intentionally left untouched unless a future provider API supports
    # exact-session deletion. This avoids modifying unrelated UI state.
    return sorted(set(found)),notes


def uninstall(root: Path, *, input_fn=input, output=print) -> bool:
    """Preview, confirm, and delete only known toolkit files and state."""
    root=Path(root).resolve()
    manifest_file=root/MANIFEST
    if _has_symlink_parent(root, MANIFEST) or manifest_file.is_symlink() or not manifest_file.is_file():
        raise RuntimeError('Missing Gaston Agentic manifest; refusing untracked removal.')
    manifest=json.loads(manifest_file.read_text(encoding='utf8'))
    files=manifest.get('files', {})
    if not isinstance(files,dict) or not files:
        raise RuntimeError('Invalid Gaston Agentic manifest.')
    # Never uninstall while a managed CLI chat or handoff worker is active.
    local = root / '.agentic' / 'local'
    active = list((local / 'native-hooks' / 'active').glob('*.json'))
    active += list((local / 'locks').glob('*'))
    from runtime_recovery import feedback_worker_busy
    if feedback_worker_busy(root):
        active.append(local / 'native-hooks' / 'feedback-worker.lock')
    if active:
        raise RuntimeError('Managed AI session or worker is active. Close it before uninstalling.')
    # A badly modified manifest must not expand deletion to application code.
    invalid = sorted(p for p in files if not _known_manifest_path(p))
    if invalid:
        raise RuntimeError('Manifest contains paths outside the toolkit; refusing removal: '
                           + ', '.join(repr(p) for p in invalid))
    unsafe = sorted(p for p in files if _has_symlink_parent(root, p))
    if unsafe:
        raise RuntimeError('Manifest contains paths under symbolic links; refusing removal: '
                           + ', '.join(repr(p) for p in unsafe))
    pristine=[]; altered=[]
    for rel, sha in sorted(files.items()):
        path=root/rel
        if path.is_symlink():
            altered.append(rel);continue
        if not path.is_file():continue
        actual=hashlib.sha256(path.read_bytes()).hexdigest()
        (pristine if actual==sha else altered).append(rel)
    local_paths=['.agentic/local', '.agentic/runs', '.agentic/tasks', '.agentic/policies/custom', '.agentic/config.json']
    for rel in local_paths:
        path=root/rel
        if _has_symlink_parent(root, rel) or path.is_symlink():
            raise RuntimeError(f'Refusing to remove symbolic link or linked parent: {rel}')
    state=[rel for rel in local_paths if (root/rel).exists()]
    bytecode = _toolkit_bytecode(root, files)
    provider_files,provider_notes=_provider_session_files(root)
    hooks=_remove_hooks(root, apply=False)
    output('GASTON AGENTIC — UNINSTALL')
    output(f'  Toolkit files: {len(pristine)}')
    output(f'  Modified toolkit files: {len(altered)}')
    output(f'  Local data locations: {len(state)} (sessions, logs, screenshots, custom rules)')
    output(f'  Generated toolkit bytecode files: {len(bytecode)}')
    output(f'  Hook settings changes: {len(hooks)}')
    output(f'  Verified Gaston provider transcripts: {len(provider_files)}')
    for item in provider_files: output('  DELETE SESSION: '+str(item))
    for note in provider_notes: output('  '+note)
    for rel in altered: output('  MODIFIED: '+rel)
    for line in hooks: output('  '+line)
    output('  Application repositories, global CLI installations and accounts will be kept.')
    remove_modified=False
    if altered:
        while True:
            answer=input_fn('Remove modified toolkit files too? (yes/no) [default: No]: ').strip().lower()
            if answer in ('', 'no', 'n'):
                break
            if answer in ('yes', 'y'):
                remove_modified=True
                break
            output('  Please answer yes/no, or press Enter for the stated default.')
    answer=input_fn('Type UNINSTALL to remove Gaston Agentic: ').strip()
    if answer!='UNINSTALL':
        output('Cancelled. No files changed.')
        return False
    _remove_hooks(root,apply=True)
    for path in provider_files:
        if path.is_file() and not path.is_symlink():path.unlink()
    for rel in state:
        path=root/rel
        if path.is_dir(): shutil.rmtree(path)
        elif path.is_file(): path.unlink()
    for rel in pristine+(altered if remove_modified else []):
        path=root/rel
        if path.is_symlink():
            # No symlink is ever followed or removed.
            continue
        if path.is_file(): path.unlink()
    for path in bytecode:
        if not path.is_symlink() and path.is_file():
            path.unlink()
    manifest_file.unlink()
    # Remove empty toolkit directories, never other users' content.
    for directory in ('.agentic/policies/custom', '.agentic/policies', '.agentic/__pycache__', '.agentic',
                      '.agents/skills/backend-assurance', '.agents/skills/e2e-feature', '.agents/skills/fintech-invariants',
                      '.agents/skills', '.agents', '.claude/skills/independent-review',
                      '.claude/skills', '.claude', '.codex', 'docs'):
        d=root/directory
        if not _has_symlink_parent(root, directory) and d.is_dir() and not d.is_symlink():
            try:d.rmdir()
            except OSError:pass
    if altered and not remove_modified:
        output('Preserved modified files: '+', '.join(altered))
    if provider_notes:output('  Some provider history was preserved because ownership could not be verified.')
    output('Gaston Agentic uninstalled from this workspace.')
    return True
