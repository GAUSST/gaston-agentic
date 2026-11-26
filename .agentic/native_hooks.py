#!/usr/bin/env python3
"""Project-only native CLI hooks; NO unregistered conversations are acted upon.

Hook handlers do no model calls and take no action on application code.
The two registered session IDs in the local registry are the ONLY allowlist.
"""
from __future__ import annotations
from project_layout import project_root, project_state, runtime_script, central_mode
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
try:
    import tomllib
except ImportError:
    tomllib = None

ROOT = project_root()
LOCAL = project_state(ROOT) / 'local' / 'native-hooks'
REGISTRY = project_state(ROOT) / 'local' / 'sessions.json'
SIGNAL = 'AGENTIC_FEEDBACK_READY'
REVIEW_SIGNAL = 'AGENTIC_REVIEW_READY'


def atomic_json(path: Path, obj: dict):
    """Replace an entire hook/session record so readers never see partially written JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.hook-', dir=path.parent)
    try:
        if os.name != 'nt': os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def registered_role(provider: str, event: dict) -> str | None:
    """Resolve ONLY explicitly enrolled role+session+workspace combinations."""
    if provider not in ('codex','claude') or not isinstance(event,dict): return None
    if not REGISTRY.is_file(): return None
    try:
        from session_store import Store
        sid=event.get('session_id')
        cwd=event.get('cwd')
        if not isinstance(sid,str) or not isinstance(cwd,str): return None
        if Path(cwd).resolve()!=ROOT.resolve(): return None
        return Store(ROOT).enrolled_role(provider,sid)
    except (OSError,KeyError,ValueError,TypeError,RuntimeError):
        return None


def allowed(provider: str, event: dict) -> bool:
    return registered_role(provider,event) is not None


def claude_is_reviewer() -> bool:
    """Backward-compatible helper; event handlers use exact registered role."""
    try:
        cfg=json.loads((project_state(ROOT)/'config.json').read_text(encoding='utf8'))
        return cfg.get('preferences',{}).get('reviewer','claude')=='claude'
    except (OSError,ValueError,TypeError): return False


def active_file(provider: str, role: str | None = None) -> Path:
    return LOCAL / 'active' / (f'{provider}-{role}.json' if role else f'{provider}.json')


def session_active(provider: str, role: str | None = None) -> bool:
    """Reconcile dead owners before interpreting a hook marker as activity."""
    from session_store import Store
    store = Store(ROOT)
    return any(store.active(provider, r) for r in ((role,) if role else ('builder', 'reviewer')))


# Direct file-edit tools are the role boundary, not a list of all capabilities.
# This cannot sandbox arbitrary approved shell commands or third-party plugins.
_DOCUMENT_SUFFIXES = frozenset({'.md', '.rst', '.adoc', '.txt'})
_NATIVE_FILE_EDITS = frozenset({'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'apply_patch', 'ApplyPatch'})
_LOCAL_FILESYSTEM_WRITES = frozenset({'mcp__filesystem__write_file', 'mcp__filesystem__edit_file',
                                      'mcp__filesystem__move_file', 'mcp__filesystem__create_directory'})


def _documentation_target(value: object, root: Path) -> bool:
    """Allow plain documentation below a docs directory, never hidden or linked paths."""
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        return False
    try:
        root = root.resolve()
        supplied = Path(value)
        path = supplied if supplied.is_absolute() else root / supplied
        relative = path.relative_to(root)
        if any(part.startswith('.') for part in relative.parts):
            return False
        if 'docs' not in relative.parts[:-1] or path.suffix.lower() not in _DOCUMENT_SUFFIXES:
            return False
        if path.resolve() != path or any((root / parent).is_symlink() for parent in (*relative.parents, relative)):
            return False
        return not path.exists() or (path.is_file() and path.stat().st_nlink == 1)
    except (OSError, ValueError, RuntimeError):
        return False


def block_native_builder_tool(event):
    """Reject known direct file writes outside current scope; native shell is not a path sandbox."""
    from builder_compile import active_attempt
    from orchestration import read_record,path_for,within
    tool=str(event.get('tool_name',''))
    if tool not in _NATIVE_FILE_EDITS and tool not in _LOCAL_FILESYSTEM_WRITES:return
    active=active_attempt(ROOT)
    # A manually opened Builder conversation has no task grant.
    if not active:
        print('Gaston Builder: no active delegated write scope.',file=sys.stderr);raise SystemExit(2)
    folder,_=active;scope=read_record(folder/'task.json').get('write_scope') or []
    info=event.get('tool_input');paths=[]
    if tool in ('apply_patch','ApplyPatch'):
        # Codex PreToolUse sends the raw patch in tool_input.command, not
        # a shell command. Older providers/fixtures may use patch or input.
        # Never select one of conflicting representations of the mutation.
        patch = info if isinstance(info, str) else None
        if isinstance(info, dict):
            values = [info[key] for key in ('command', 'patch', 'input') if key in info]
            if values and all(isinstance(value, str) and value == values[0] for value in values):
                patch = values[0]
        if isinstance(patch, str):
            # The provider trims the patch envelope; preserve every operation,
            # including both sides of a move, and handle CRLF transport.
            paths = re.findall(r'^\*\*\* (?:Add File:|Update File:|Delete File:|Move to:) (.+)$', patch.strip().replace('\r\n', '\n'), re.M)
    elif isinstance(info,dict):
        if tool=='mcp__filesystem__move_file':paths=[info.get('source'),info.get('destination')]
        else:paths=[info.get('file_path') or info.get('path') or info.get('notebook_path')]
    try:
        if not paths:raise ValueError('Unrecognized direct edit input')
        for name in paths:
            if not isinstance(name,str):raise ValueError('Missing edit path')
            from orchestration import relative_path
            name=relative_path(ROOT,name)
            target=path_for(ROOT,name,scope=True)
            if not within(target.relative_to(ROOT.resolve()).as_posix(),scope):raise ValueError('Outside approved task scope: '+name)
    except (ValueError,OSError) as exc:
        print('Gaston Builder: '+str(exc)+'. Ask the Lead for a scope extension.',file=sys.stderr);raise SystemExit(2)


def block_native_reviewer_tool(event: dict):
    """Defer ordinary native/MCP capabilities to provider permission controls.

    Known direct file-edit tools may edit documentation only. Use the shared Lead scoped edit tool for source/test changes. The hook never
    emits an allow decision or grants permissions to an external connector.
    Shell, plugins and subagents must follow the Lead role contract; this is
    not an OS-level source-write sandbox.
    """
    tool = str(event.get('tool_name', ''))
    info = event.get('tool_input')
    if tool in _NATIVE_FILE_EDITS or tool in _LOCAL_FILESYSTEM_WRITES:
        if tool in ('Edit', 'Write', 'MultiEdit', 'mcp__filesystem__write_file', 'mcp__filesystem__edit_file'):
            path = info.get('file_path', info.get('path')) if isinstance(info, dict) else None
            if _documentation_target(path, ROOT):
                return
        print('Gaston Lead: use lead_edit_files for test setup, scoped small implementation and mandatory correction takeover. Direct native edits are limited to docs/ to keep parallel work and verification evidence traceable.', file=sys.stderr)
        raise SystemExit(2)
    # Unknown tools (including newly connected MCP servers), Bash, WebSearch,
    # WebFetch, etc. proceed to the provider's normal permission machinery.
    return


def claude_write_guard_configured(root: Path) -> bool:
    """Check whether the exact project's Claude PreToolUse hook is configured.

    This is configuration evidence, not a guarantee that provider hooks run.
    If missing, launch with native file mutation tools disabled instead.
    """
    root = Path(root).resolve()
    try:
        data = json.loads((root / '.claude/settings.local.json').read_text(encoding='utf8'))
        if data.get('disableAllHooks') is True:
            return False
        for group in data.get('hooks', {}).get('PreToolUse', []):
            if group.get('matcher') not in (None, '', '*', '.*'):
                continue
            for hook in group.get('hooks', []):
                from platform_compat import parse_command
                args = _hook_command_args(hook.get('command', ''), 'claude')
                expected = Path(runtime_script('native_hooks.py') if central_mode() else root / '.agentic/native_hooks.py')
                if hook.get('type') == 'command' and len(args) == 3 and Path(args[1]).resolve() == expected.resolve() and args[2] == 'claude':
                    return True
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return False


def refresh_central_provider_hooks(root: Path) -> dict:
    """Retarget only recognized Gaston hook paths for both native providers.

    Unrelated hooks and provider trust records are preserved. This function
    never creates or modifies Codex trust state.
    """
    if not central_mode():
        return {'claude':False,'codex':False}
    from central_update import home
    from importlib import metadata
    from platform_compat import parse_command
    root=Path(root).resolve()
    current=runtime_script('native_hooks.py').resolve()
    try:
        base=Path(metadata.distribution('gaston-agentic-cli').locate_file('gaston_engine/native_hooks.py')).resolve()
    except metadata.PackageNotFoundError:
        return {'claude':False,'codex':False}
    versions=home()/'versions'
    results={}
    for provider,relative in (('claude','.claude/settings.local.json'),('codex','.codex/hooks.json')):
        path=root/relative
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
            results[provider]=False
            continue
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if provider=='claude' and data.get('disableAllHooks'):
                results[provider]=False
                continue
            changed=False
            for event in ('SessionStart','SessionEnd','PreToolUse','Stop'):
                for group in data.get('hooks',{}).get(event,[]):
                    for hook in group.get('hooks',[]):
                        if hook.get('type')!='command':continue
                        args=_hook_command_args(hook.get('command',''),provider)
                        if len(args)!=3 or args[-1]!=provider:continue
                        old=Path(args[1])
                        if not old.is_absolute() or old.resolve()!=old or old==current:continue
                        known_base=old==base
                        known_version=False
                        if old.is_relative_to(versions):
                            parts=old.relative_to(versions).parts
                            known_version=(len(parts)==3 and bool(re.fullmatch(r'v\d+\.\d+\.\d+',parts[0]))
                                           and parts[1:]==('gaston_engine','native_hooks.py'))
                        # A previous immutable version may have been removed by
                        # cleanup or an interrupted repair. Its exact managed
                        # path is still recognizable and safe to retarget.
                        if not (known_base or known_version):continue
                        if known_base and not old.is_file():continue
                        hook['command']=hook_def(provider,event,str(current))['command']
                        changed=True
            if changed:atomic_json(path,data)
            results[provider]=changed
        except (OSError,ValueError,TypeError,AttributeError):
            results[provider]=False
    return results


def refresh_central_claude_hooks(root: Path) -> bool:
    """Backward-compatible Claude hook refresh entry point."""
    return refresh_central_provider_hooks(root).get('claude',False)


def receive(provider: str, event: dict) -> str:
    """Return a brief status for tests; hook CLI prints nothing to models."""
    role=registered_role(provider,event)
    if event.get('hook_event_name')=='PreToolUse' and role in (None,'builder') and os.environ.get('GASTON_BUILDER_TASK_ID') and event.get('cwd') and Path(event['cwd']).resolve()==ROOT.resolve():
        block_native_builder_tool(event)
    if role is None:
        if event.get('hook_event_name')=='PreToolUse' and event.get('cwd') and Path(event['cwd']).resolve()==ROOT.resolve():
            from session_store import Store
            store=Store(ROOT)
            for candidate in ('claude','codex'):
                with store.guard(candidate,'reviewer'):
                    _,_,owner,_=store._records(candidate,'reviewer')
                    if owner and os.environ.get('GASTON_SESSION_LEASE') and owner.get('lease')==os.environ.get('GASTON_SESSION_LEASE'):
                        print('Unregistered fork of the managed Lead: close this conversation; no tool execution.',file=sys.stderr)
                        raise SystemExit(2)
        return 'ignored'
    kind = event.get('hook_event_name')
    sid = event.get('session_id')
    if role == 'reviewer' and kind in ('SessionStart','PreToolUse','Stop'):
        from session_store import Store
        store = Store(ROOT)
        with store.lead_gate():
            # Managed roles must not overwrite an active Lead's marker. Native
            # sessions that were opened manually have no matching lease.
            for candidate in ('claude','codex'):
                with store.guard(candidate,'reviewer'):
                    _, _, owner, activity = store._records(candidate,'reviewer')
                    lease = os.environ.get('GASTON_SESSION_LEASE')
                    if owner and (candidate != provider or not lease or owner.get('lease') != lease):
                        if kind == 'PreToolUse':
                            print('Gaston Lead ownership conflict. Close the stale native chat; no task state was changed.',file=sys.stderr)
                            raise SystemExit(2)
                        return 'conflicting-lead-lease'
                    if activity and (candidate != provider or activity.get('session_id') != sid or activity.get('lease') != lease):
                        if kind == 'PreToolUse':
                            print('Gaston Lead activity conflict. Close the stale native chat.',file=sys.stderr)
                            raise SystemExit(2)
                        return 'conflicting-lead-lease'
    if kind == 'SessionStart':
        # Ignore compaction/replay start events to avoid accidental duplicate leases.
        source = event.get('source', 'startup')
        if source in ('startup', 'resume'):
            from session_store import Store
            with Store(ROOT).guard(provider, role):
                atomic_json(active_file(provider,role), {
                    'root': str(ROOT.resolve()), 'session_id': sid,
                    'lease': os.environ.get('GASTON_SESSION_LEASE'),
                    'started_at': dt.datetime.now(dt.timezone.utc).isoformat()})
        return 'registered'
    if kind == 'PreToolUse' and role == 'reviewer':
        if event.get('tool_name') in ('CronCreate','mcp__gaston_verify__record_quota_recovery_job'):
            print('Use Gaston quota_recovery_decision for consented Builder-only recovery; no native Lead fork.',file=sys.stderr)
            raise SystemExit(2)
        import wait_guard
        reason = wait_guard.shell_reason(ROOT, os.environ.get('GASTON_SESSION_LEASE'), event)
        if reason:
            print(reason, file=sys.stderr)
            raise SystemExit(2)
        block_native_reviewer_tool(event)
        return 'allowed-tool'
    if kind == 'Stop' and role in ('builder', 'reviewer'):
        message = event.get('last_assistant_message')
        signal = REVIEW_SIGNAL if role == 'builder' else SIGNAL
        if not isinstance(message, str) or not re.search(r'(?m)^\s*' + signal + r'\s*$', message):
            return 'ordinary-chat'
        if event.get('stop_hook_active'):
            return 'ignored-recursive'
        clean = re.sub(r'(?m)^\s*' + signal + r'\s*$', '', message).strip()[:12000]
        if not clean:
            return 'empty-feedback'
        # Store only the actionable summary, never an entire conversation.
        digest = hashlib.sha256((sid + '\0' + role + '\0' + clean).encode()).hexdigest()
        item = LOCAL / 'queue' / (digest + '.json')
        if item.exists() or (LOCAL / 'processed' / (digest + '.json')).exists():
            return 'duplicate'
        atomic_json(item, {
            'id': digest, 'session_id': sid, 'provider': provider, 'role': role,
            'source': provider+'-native', 'feedback': clean,
            'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'project': str(ROOT.resolve())})
        return 'queued'
    if kind == 'SessionEnd':
        lease = active_file(provider,role)
        try:
            from session_store import Store
            with Store(ROOT).guard(provider, role):
                if lease.exists():
                    data = json.loads(lease.read_text(encoding='utf-8'))
                    if data.get('session_id') == sid and data.get('lease') == os.environ.get('GASTON_SESSION_LEASE'):
                        lease.unlink()
        except (OSError, ValueError):
            # Fail closed: do not automatically clear unknown lease state.
            return 'lease-error'
        if list((LOCAL / 'queue').glob('*.json')):
            schedule_worker()
        return 'closed'
    return 'ignored-event'


def schedule_worker():
    """Never run a long model call in a provider's lifecycle hook."""
    LOCAL.mkdir(parents=True, exist_ok=True)
    logfile = LOCAL / 'worker.log'
    # No shell, no network or credential handling by this helper itself.
    with logfile.open('a', encoding='utf-8') as log:
        subprocess.Popen([sys.executable, str(runtime_script('hook_worker.py') if central_mode() else ROOT / '.agentic' / 'hook_worker.py')], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, **__import__('platform_compat').popen_kwargs())


def command_line():
    parser = argparse.ArgumentParser()
    parser.add_argument('provider', choices=['codex','claude'])
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(512*1024 + 1)
        if len(raw) > 512*1024:
            return
        event = json.loads(raw)
        if isinstance(event, dict):
            result=receive(args.provider, event)
            # Claude emits SessionStart also after native compaction. Reinject
            # a bounded, verified project fact; do not rely on compacted prose.
            if (args.provider=='claude' and event.get('hook_event_name')=='SessionStart' and
                    result=='registered' and event.get('source','startup') in ('startup','resume','compact') and
                    registered_role(args.provider,event)=='reviewer'):
                import project_continuity as continuity
                print(json.dumps({'hookSpecificOutput':{
                    'hookEventName':'SessionStart',
                    'additionalContext':continuity.startup_context(ROOT)+'\n'+__import__('token_intelligence').checkpoint_context(ROOT,args.provider,event['session_id'])+'\n'+__import__('efficiency_runtime').bound_plan_context(ROOT,args.provider,event['session_id'])}}))
            if (args.provider=='claude' and event.get('hook_event_name')=='Stop' and result=='ordinary-chat'
                    and registered_role(args.provider,event)=='reviewer'
                    and os.environ.get('GASTON_SESSION_LEASE')):
                from session_store import Store
                if not Store(ROOT).lead_caller(os.environ.get('GASTON_SESSION_LEASE'),args.provider,os.getpid()):return
                from wait_guard import stop_reason
                from efficiency_runtime import stop_advisory, continuation_reason
                # Only the progress-deduplicated plan guard may re-enter a
                # recursive Stop. One-shot wait/context nudges remain bounded.
                recursive=event.get('stop_hook_active')
                reason=(None if recursive else stop_reason(ROOT,os.environ.get('GASTON_SESSION_LEASE'))) or continuation_reason(ROOT,args.provider,event['session_id'])
                if not reason and not recursive:reason=stop_advisory(ROOT,args.provider,event['session_id'])
                if reason:print(json.dumps({'decision':'block','reason':reason}))
    except (OSError, ValueError, TypeError):
        return


def _hook_command_args(command: str, provider: str) -> list[str]:
    """Parse a hook using the shell grammar of the provider that executes it."""
    from platform_compat import parse_command, msys_to_windows_path
    if os.name == 'nt' and provider == 'claude':
        args = shlex.split(command)
    else:
        args = parse_command(command)
    if os.name == 'nt' and provider == 'claude' and len(args) == 3:
        args = [msys_to_windows_path(value) if value.startswith('/') else value for value in args]
    return args


def _managed_hook_command(command: str, provider: str, script: Path) -> bool:
    try:
        args = _hook_command_args(command, provider)
        return len(args) == 3 and Path(args[1]).resolve() == Path(script).resolve() and args[2] == provider
    except (OSError, ValueError, TypeError):
        return False


def hook_def(provider: str, event: str, script: str):
    if os.name == 'nt' and provider == 'claude':
        from platform_compat import windows_to_msys_path
        python = windows_to_msys_path(sys.executable)
        target = windows_to_msys_path(script)
        cmd = shlex.quote(python) + ' ' + shlex.quote(target) + ' ' + provider
    elif os.name == 'nt':
        cmd = subprocess.list2cmdline([sys.executable,script,provider])
    else:
        cmd = shlex.quote(sys.executable) + ' ' + shlex.quote(script) + ' ' + provider
    return {'type':'command','command':cmd,'timeout': 2 if event == 'SessionEnd' else 10}


def install_native_hooks(root: Path, *, approve: bool = False) -> bool:
    """Merge *local* per-project files without clobbering existing user hooks.

    Unregistered sessions invoke the hook handler but receive immediate no-op.
    """
    root = Path(root).resolve()
    if root != ROOT.resolve():
        raise ValueError('Hook installer must run from the kit project root')
    if not approve:
        return False
    script = str(runtime_script('native_hooks.py') if central_mode() else ROOT / '.agentic' / 'native_hooks.py')
    selected = selected_providers(root)
    for provider, target in (
        ('codex', ROOT / '.codex' / 'hooks.json'),
        ('claude', ROOT / '.claude' / 'settings.local.json')):
        if provider not in selected:
            continue
        if target.exists():
            obj = json.loads(target.read_text(encoding='utf-8'))
            if not isinstance(obj, dict) or not isinstance(obj.get('hooks', {}), dict):
                raise ValueError(f'Cannot merge hooks into {target}: unexpected JSON shape')
        else: obj = {}
        hooks = obj.setdefault('hooks', {})
        events = ['SessionStart', 'SessionEnd', 'Stop']
        events.append('PreToolUse')
        for evt in events:
            entries = hooks.setdefault(evt, [])
            if not isinstance(entries, list):
                raise ValueError(f'Cannot merge {evt} into {target}')
            descriptor = hook_def(provider, evt, script)
            # Replace only our own hook command; preserve unrelated handlers.
            for entry in entries:
                if not isinstance(entry, dict): continue
                sub = entry.get('hooks', [])
                if not isinstance(sub, list): continue
                managed_script = Path(runtime_script('native_hooks.py') if central_mode() else ROOT / '.agentic' / 'native_hooks.py')
                sub[:] = [h for h in sub if not (isinstance(h, dict) and
                    _managed_hook_command(str(h.get('command','')), provider, managed_script))]
            # Remove empty groups created from older kit hook entries.
            entries[:] = [e for e in entries if isinstance(e, dict) and e.get('hooks')]
            entries.append({'hooks':[descriptor]})
        atomic_json(target, obj)
    return True



def codex_trust_status(root: Path = ROOT, *, home: Path | None = None) -> dict:
    """Read-only inspection of Codex's OWN persisted hook trust decisions.

    Never edits trust state or calculates/forges provider trust hashes. A
    recorded hash is evidence of a decision, not a proof the running Codex
    instance has loaded it; Codex itself makes that final determination.
    """
    root = Path(root).resolve()
    path = root/'.codex/hooks.json'
    if not path.is_file():
        return {'status':'not_installed','trusted_recorded':0,'expected':4}
    cfg = (home or Path.home())/'.codex/config.toml'
    if tomllib is None or not cfg.is_file():
        return {'status':'needs_provider_review','trusted_recorded':0,'expected':4}
    try:
        data=tomllib.loads(cfg.read_text(encoding='utf8'))
        states=data.get('hooks',{}).get('state',{})
        # Key formats can evolve. Restrict this read-only detection to the
        # exact project hooks filename, and never count unrelated hooks.
        relevant={str(k).lower():v for k,v in states.items()
                  if str(path).lower() in str(k).lower() and isinstance(v,dict)}
        expected_labels=('pre_tool_use','session_start','session_end','stop')
        recorded=sum(any((':'+event+':') in k and isinstance(v.get('trusted_hash'),str)
                         and bool(v['trusted_hash']) and v.get('enabled',True) is not False
                         for k,v in relevant.items()) for event in expected_labels)
        return {'status':'trust_recorded' if recorded==4 else 'needs_provider_review',
                'trusted_recorded':recorded,'expected':4}
    except (OSError,ValueError,TypeError,AttributeError):
        return {'status':'unknown','trusted_recorded':0,'expected':4}

def selected_providers(root: Path) -> set[str]:
    try:
        cfg = json.loads((project_state(root) / 'config.json').read_text(encoding='utf-8'))
        prefs = cfg.get('preferences', {})
        return {p for p in (prefs.get('builder','codex'), prefs.get('reviewer','claude'))
                if p in ('codex', 'claude')}
    except (OSError, ValueError, TypeError):
        return {'codex', 'claude'}


def installed(root: Path) -> bool:
    script = str(runtime_script('native_hooks.py') if central_mode() else Path(root).resolve() / '.agentic' / 'native_hooks.py')
    selected = selected_providers(root)
    for provider, name in (('codex', '.codex/hooks.json'), ('claude', '.claude/settings.local.json')):
        if provider not in selected:
            continue
        p = Path(root) / name
        if not p.is_file(): return False
        try:
            hooks = json.loads(p.read_text(encoding='utf-8')).get('hooks', {})
            needed = {'SessionStart', 'SessionEnd', 'Stop'}
            needed.add('PreToolUse')
            for event in needed:
                if not any(
                    x.get('type') == 'command' and _managed_hook_command(
                        str(x.get('command','')),
                        'codex' if name == '.codex/hooks.json' else 'claude',
                        Path(script))
                    for group in hooks.get(event, [])
                    for x in group.get('hooks', [])
                ):
                    return False
        except (OSError, ValueError, TypeError, AttributeError): return False
    return True


if __name__ == '__main__':
    command_line()
