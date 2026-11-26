#!/usr/bin/env python3
"""Small, portable, stdlib-only Codex/Claude engineering pipeline.

Trust boundary: this script runs locally in a trusted repository. It deliberately
never enables unrestricted CLI permissions, deploys, merges or contacts production.
"""
from __future__ import annotations

import argparse
import hashlib
import queue
import re
import threading
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import platform
import tempfile
import subprocess
import sys
import time
import uuid
import secrets
sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_store import Store
import playwright_tools
import verification_planner
import workspace_checks
from model_menu import (recommended_model,
                        configured_effort, verify_codex_model_effort, claude_model_id,
                        advisor_id, normalize_models, valid_model_id, CLAUDE_ADVISORS,
                        pick_effort, pick_role_model, ModelCatalogUnavailable)
import workspace_git
import token_intelligence as ti
import serena_tools
from native_hooks import install_native_hooks, installed as native_hooks_installed, LOCAL as HOOK_LOCAL, session_active

from project_layout import project_root, project_state, project_state_name, runtime_script, central_mode
ROOT = project_root()
STATE = project_state(ROOT)
CFG_FILE = STATE / 'config.json'
RUNS = STATE / 'runs'
TASKS = STATE / 'tasks'
ACTIVE_RUN = None
SESSIONS = Store(ROOT)
LOCAL = STATE / 'local'
HANDOFFS = LOCAL / 'handoffs'
FEEDBACK_SCHEMA = {
  'type':'object', 'additionalProperties':False,
  'required':['status','kind','title','description','severity','reproduction','expected','observed','evidence','recommendation','questions'],
  'properties': {
    'status': {'type':'string','enum':['actionable','needs_input','none']},
    'kind': {'type':'string','enum':['feature','bug','policy','verification']},
    'title': {'type':'string'}, 'description': {'type':'string'},
    'severity': {'type':'string','enum':['critical','high','medium','low','unknown']},
    'reproduction': {'type':'array','items':{'type':'string'}},
    'expected': {'type':'string'}, 'observed': {'type':'string'},
    'evidence': {'type':'array','items':{'type':'string'}},
    'recommendation': {'type':'string'},
    'questions': {'type':'array','items':{'type':'string'}}
  }
}
ACK_SCHEMA = {
  'type':'object','additionalProperties':False,
  'required':['status','summary','questions','next_steps'],
  'properties': {
    'status': {'type':'string','enum':['received','needs_input','not_reproducible']},
    'summary': {'type':'string'},
    'questions': {'type':'array','items':{'type':'string'}},
    'next_steps': {'type':'array','items':{'type':'string'}}
  }
}
DEFAULT_POLICIES = []  # Domain-specific policies are opt-in for fresh projects.

PLAN_SCHEMA = {
  'type': 'object', 'additionalProperties': False,
  'required': ['status', 'questions', 'steps', 'risk_flags', 'verification_areas', 'verification_rationale', 'verification_checks'],
  'properties': {
    'status': {'type': 'string', 'enum': ['ready', 'needs_input']},
    'questions': {'type': 'array', 'items': {'type': 'string'}},
    'steps': {'type': 'array', 'items': {'type': 'string'}},
    'risk_flags': {'type': 'array', 'items': {'type': 'string'}},
    'verification_areas': {'type': 'array', 'items': {'type': 'string', 'enum': list(verification_planner.VALID_AREAS)}},
    'verification_rationale': {'type': 'array', 'items': {'type': 'string'}},
    'verification_checks': {'type': 'array', 'items': {'type': 'string'}}
  }
}
BUILD_SCHEMA = {
  'type': 'object', 'additionalProperties': False,
  'required': ['status', 'questions', 'summary', 'changed_files', 'remaining_risks'],
  'properties': {
    'status': {'type': 'string', 'enum': ['done', 'blocked']},
    'questions': {'type': 'array', 'items': {'type': 'string'}},
    'summary': {'type': 'string'},
    'changed_files': {'type': 'array', 'items': {'type': 'string'}},
    'remaining_risks': {'type': 'array', 'items': {'type': 'string'}}
  }
}
REVIEW_SCHEMA = {
  'type': 'object', 'additionalProperties': False,
  'required': ['verdict', 'findings', 'coverage_gaps', 'compliance_notes'],
  'properties': {
    'verdict': {'type': 'string', 'enum': ['pass', 'changes_requested', 'blocked']},
    'findings': {'type': 'array', 'items': {
      'type': 'object', 'additionalProperties': False,
      'required': ['severity', 'location', 'issue', 'fix'],
      'properties': {
        'severity': {'type': 'string', 'enum': ['critical', 'high', 'medium', 'low']},
        'location': {'type': 'string'}, 'issue': {'type': 'string'}, 'fix': {'type': 'string'}
      }}},
    'coverage_gaps': {'type': 'array', 'items': {'type': 'string'}},
    'compliance_notes': {'type': 'array', 'items': {'type': 'string'}}
  }
}


def fail(message: str, exit_code: int = 2):
    raise SystemExit(f'ERROR: {message}')


def load_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(obj, dict):
            fail(f'{path} must contain a JSON object')
        return obj
    except (OSError, json.JSONDecodeError) as ex:
        fail(f'Cannot read {path}: {ex}')


def write_json(path: Path, data: dict):
    # Atomically publish task and evidence records; a killed worker must not
    # leave partially written JSON that looks like a missing state transition.
    from orchestration import put
    put(path, data)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def require_setup_complete(*, require_models=True):
    """Block all daily commands until this workspace completed initialization."""
    status = LOCAL / 'setup-status.json'
    if not CFG_FILE.is_file() or not status.is_file():
        fail('Setup is incomplete. Run dev init first.')
    try:
        data = json.loads(status.read_text(encoding='utf-8'))
        cfg = json.loads(CFG_FILE.read_text(encoding='utf-8'))
        prefs = cfg.get('preferences', {})
        complete = (data.get('ready') is True and
                    Path(data.get('project', '')).resolve() == ROOT.resolve() and
                    prefs.get('builder') in ('codex','claude') and
                    prefs.get('reviewer') in ('codex','claude'))
        if complete and require_models:
            saved_models=cfg.get('models',{})
            complete=all(bool(saved_models.get(prefs[role]+'_'+role))
                         for role in ('builder','reviewer'))
    except (OSError, ValueError, TypeError):
        complete = False
    if not complete:
        fail('Setup is incomplete or a role model is missing. Run dev init first.')


def reasoning_for(provider: str, role: str) -> str:
    """Always use a configured or role-recommended effort; never silent account default."""
    return configured_effort(load_config(),provider,role)


def codex_effort_args(provider: str, role: str) -> list[str]:
    cfg=load_config()
    key=provider+'_'+role
    model=cfg.get('models',{}).get(key,'')
    effort=configured_effort(cfg,provider,role)
    ok,reason=verify_codex_model_effort(model,effort,verified=cfg.get('codex_verified_pairs'))
    if not ok:
        fail('Cannot start Codex with an unverified model/effort: '+reason)
    return ['-c',f'model_reasoning_effort={json.dumps(effort)}']


def claude_effort_args(provider: str, role: str) -> list[str]:
    effort=reasoning_for(provider,role)
    return ['--effort', effort] if effort else []


def claude_advisor_args(role: str, config: dict | None = None) -> list[str]:
    """Use native advisor only for the registered Claude Engineering Lead.

    No wizard prompt; saved explicit choices win over the default. 'off' is
    enforced for the Builder via CLAUDE_CODE_DISABLE_ADVISOR_TOOL in its process.
    The provider may reject Fable if account consent or access is unavailable.
    """
    if role != 'reviewer':
        return []
    cfg = config if config is not None else load_config()
    selected = advisor_id(cfg)
    if selected not in CLAUDE_ADVISORS and not valid_model_id('claude', selected):
        fail('Invalid Advisor ID. Choose a model in dev models.')
    return ['--advisor', selected] if selected != 'off' else []


def claude_advisor_env(role: str, config: dict | None = None) -> dict:
    """Scope Advisor and effort settings to THIS enrolled Claude process only.

    The global CLAUDE_CODE_EFFORT_LEVEL environment variable outranks --effort;
    remove it from child process so the explicitly selected role effort wins.
    An inherited DISABLE_ADVISOR_TOOL is an explicit opt-out and stops launch.
    """
    cfg = config if config is not None else load_config()
    env = dict(os.environ)
    env.pop('CLAUDE_CODE_EFFORT_LEVEL', None)
    if role == 'reviewer':
        # Git read-only Bash probes must not update the index opportunistically.
        env['GIT_OPTIONAL_LOCKS'] = '0'
        from task_runtime import MCP_TOOL_TIMEOUT_SECONDS
        try: previous_timeout=int(env.get('MCP_TOOL_TIMEOUT','0'))
        except ValueError: previous_timeout=0
        env['MCP_TOOL_TIMEOUT']=str(max(previous_timeout,MCP_TOOL_TIMEOUT_SECONDS*1000))
    if role == 'builder':
        env.pop('GASTON_LEAD_TOKEN', None)
        env.pop('GASTON_READONLY_DATABASE_URL', None)
        env.pop('GASTON_DATABASE_ALLOW_FIXTURE_DATA', None)
    if role == 'builder' or advisor_id(cfg) == 'off':
        env['CLAUDE_CODE_DISABLE_ADVISOR_TOOL'] = '1'
    elif env.get('CLAUDE_CODE_DISABLE_ADVISOR_TOOL') == '1':
        # An explicit user/organization disable must not be silently bypassed.
        fail('Claude Advisor is disabled by your environment. Unset '
             'CLAUDE_CODE_DISABLE_ADVISOR_TOOL for the enrolled Lead or choose '
             'changing Advisor to Off in dev models.')
    return env


def load_config():
    cfg = load_json(CFG_FILE)
    cfg.setdefault('policy_files', DEFAULT_POLICIES.copy())
    cfg.setdefault('preferences', {'builder':'codex','reviewer':'claude','mobile_e2e':'maestro'})
    # Earlier configurations used 'auto' for the default two-provider setup.
    if cfg['preferences'].get('builder')=='auto': cfg['preferences']['builder']='codex'
    if cfg['preferences'].get('reviewer')=='auto': cfg['preferences']['reviewer']='claude'
    return normalize_models(cfg)


def refresh_workspace_checks(cfg: dict, *, persist: bool = False) -> dict:
    """Re-detect code roots and test gates during normal agent work.

    This is local read-only discovery, apart from saving the updated config.
    No installation, human prompt, network call or extra developer CLI.
    """
    updated, changed = workspace_checks.refresh(ROOT, cfg)
    if persist and changed:
        write_json(CFG_FILE, updated)
    return updated


def task_path(task_id: str):
    if not task_id or not all(c.isalnum() or c in '-_' for c in task_id):
        fail('Task IDs may contain only letters, digits, hyphens, and underscores')
    return TASKS / f'{task_id}.json'


def load_task(task_id: str):
    path = task_path(task_id)
    if not path.exists():
        path = run_dir(task_id) / 'task.json'
    task = load_json(path)
    if task.get('id') != task_id:
        fail('Task id must match the filename')
    for field in ['goal', 'acceptance_criteria']:
        if not task.get(field):
            fail(f'Missing required task field: {field}')
    if not isinstance(task['acceptance_criteria'], list) or not task['acceptance_criteria']:
        fail('acceptance_criteria must be a nonempty list')
    risk = task.get('risk', 'normal')
    if risk not in ['normal', 'high', 'critical']:
        fail('risk must be normal, high or critical')
    return task


def run_dir(task_id: str) -> Path:
    return RUNS / task_id


def format_stream_line(line: str, *, ai: bool):
    """Emit useful progress rather than full prompts, chain-of-thought or log noise."""
    if not ai:
        line = line.strip()
        return (line[:240] + ('…' if len(line) > 240 else '')) if line else None
    try:
        event = json.loads(line)
        kind = event.get('type', '')
        item = event.get('item') or {}
        if kind == 'item.started':
            if item.get('type') == 'command_execution': return 'agent running a command'
            if item.get('type') in ('mcp_tool_call', 'web_search'): return 'agent using a tool'
        if kind == 'item.completed':
            if item.get('type') == 'file_change': return 'agent updated files'
            if item.get('type') == 'command_execution': return 'agent command finished'
        if kind == 'turn.completed':
            usage = event.get('usage') or {}
            return 'agent turn complete' + (f" (output tokens: {usage.get('output_tokens')})" if usage.get('output_tokens') is not None else '')
        if kind in ('turn.failed', 'error'): return 'agent reported an error; inspect the log'
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _central_prompt_paths(text: str) -> str:
    if project_state_name() != '.gaston':
        return text
    return (text.replace('python .agentic/builder_compile.py',
                         sys.executable + ' ' + str(runtime_script('builder_compile.py')))
                .replace('.agentic/local/', '.gaston/local/')
                .replace('.agentic/config.json', '.gaston/config.json')
                .replace('.agentic/policies/custom/', '.gaston/policies/custom/'))


def run_process(argv: list[str], output: Path, *, stdin: str | None = None,
                timeout: int = 1800, cwd: Path = ROOT, ai: bool = False, env: dict | None = None):
    """Stream progress, preserve separate stdout/stderr for structured CLI JSON."""
    if project_state_name() == '.gaston':
        argv = [_central_prompt_paths(x) if isinstance(x, str) else x for x in argv]
        if isinstance(stdin, str):
            stdin = _central_prompt_paths(stdin)
    output.parent.mkdir(parents=True, exist_ok=True)
    print('  Executing:', argv[0], argv[1] if len(argv) > 1 else '', flush=True)
    started = time.monotonic()
    from platform_compat import popen_kwargs, prepare_subprocess
    isolated_group = True
    try:
        argv, env, _ = prepare_subprocess(argv, env)
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1, env=env, **popen_kwargs())
    except (FileNotFoundError, OSError) as exc:
        output.write_text(f'EXECUTION ERROR: {exc}\n', encoding='utf-8')
        return subprocess.CompletedProcess(argv, 127, stdout='', stderr=str(exc))
    import task_runtime
    completed = False
    stopped = False
    try:
        if ai:
            SESSIONS.record_child(p.pid, isolated_group=isolated_group)
        if stdin is not None:
            def send_input():
                try:
                    p.stdin.write(stdin)
                    p.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
            threading.Thread(target=send_input, daemon=True).start()
        events = queue.Queue()
        def reader(stream, kind):
            try:
                for line in stream:
                    events.put((kind,line))
            finally:
                events.put((kind,None))
        threading.Thread(target=reader, args=(p.stdout,'out'), daemon=True).start()
        threading.Thread(target=reader, args=(p.stderr,'err'), daemon=True).start()
        stdout_lines, stderr_lines = [], []
        last_progress = started
        ended = set()
        tty = bool(getattr(sys.stdout, 'isatty', lambda: False)())
        spinner_frames = '|/-\\'
        spinner_index = 0
        spinner_visible = False
        def clear_spinner():
            nonlocal spinner_visible
            if spinner_visible:
                sys.stdout.write('\r' + ' ' * 110 + '\r')
                sys.stdout.flush()
                spinner_visible = False

        with output.open('w', encoding='utf-8') as log:
            while len(ended) < 2:
                now = time.monotonic()
                if now - started > timeout:
                    task_runtime.stop_owned_process(p, isolated_group=isolated_group)
                    stopped = True
                    log.write(f'\nTIMEOUT after {timeout}s\n')
                    clear_spinner()
                    print(f'  Timed out after {timeout}s', flush=True)
                    return subprocess.CompletedProcess(argv, 124, stdout=''.join(stdout_lines), stderr='Timed out')
                try:
                    kind,line = events.get(timeout=0.25 if tty else 1)
                except queue.Empty:
                    kind,line = None,''
                if line is None and kind:
                    ended.add(kind)
                elif line:
                    if kind == 'out': stdout_lines.append(line)
                    else: stderr_lines.append(line)
                    log.write(('[stderr] ' if kind=='err' else '') + line)
                    log.flush()
                    summary = format_stream_line(line, ai=ai)
                    if summary:
                        clear_spinner()
                        print('    '+summary, flush=True)
                        last_progress = now
                if tty:
                    sys.stdout.write('\r    Working ' + spinner_frames[spinner_index % len(spinner_frames)] +
                                     f' ({int(time.monotonic()-started)}s)' + ' ' * 45)
                    sys.stdout.flush()
                    spinner_index += 1
                    spinner_visible = True
                elif time.monotonic() - last_progress >= 12:
                    print(f'    Still working ({int(time.monotonic()-started)}s); see {output.relative_to(ROOT) if output.is_relative_to(ROOT) else output}', flush=True)
                    last_progress = time.monotonic()
        clear_spinner()
        p.wait(timeout=max(.01, timeout-(time.monotonic()-started)))
        completed = True
        print(f'  Finished in {time.monotonic()-started:.1f}s; exit {p.returncode}', flush=True)
        return subprocess.CompletedProcess(argv, p.returncode, stdout=''.join(stdout_lines), stderr=''.join(stderr_lines))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, stdout='', stderr='Timed out')
    finally:
        if not completed and not stopped:
            task_runtime.stop_owned_process(p, isolated_group=isolated_group)
        for stream in (p.stdin, p.stdout, p.stderr):
            if stream:
                stream.close()


def command_exists(name: str):
    return shutil.which(name) is not None


def git_clean():
    try:
        return workspace_git.clean(ROOT, load_config().get('project'))
    except ValueError as exc:
        fail(str(exc))



def choose(preferred: str, fallback: str | None = None):
    if command_exists(preferred):
        return preferred
    if fallback and command_exists(fallback):
        return fallback
    return None


def extract_codex_thread(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try: event = json.loads(line)
        except ValueError: continue
        if event.get('type') in ('thread.started', 'thread.resumed'):
            session_id = event.get('thread_id') or event.get('session_id')
            if session_id and valid_session_id(session_id): return session_id
    return None


def valid_session_id(value):
    try: return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, TypeError): return False


def lead_verification_mcp_config(*, include_maestro=False) -> Path:
    """Private, role-scoped verification tools for the Engineering Lead.

    Never installs MCP tools globally or grants an unrestricted shell.
    """
    LOCAL.mkdir(parents=True,exist_ok=True)
    token_file = LOCAL / 'lead-verification-token'
    lease=os.environ.get('GASTON_SESSION_LEASE','')
    lease_file=LOCAL/'lead-token-lease'
    if not token_file.is_file() or (lease and (not lease_file.is_file() or lease_file.read_text()!=lease)):
        # Only the project-local MCP may delegate; this is not exposed as a CLI.
        token_file.write_text(secrets.token_hex(32), encoding='ascii')
        if os.name != 'nt': token_file.chmod(0o600)
        if lease:
            lease_file.write_text(lease)
            if os.name != 'nt':lease_file.chmod(0o600)
    token = token_file.read_text(encoding='ascii').strip()
    from task_runtime import MCP_TOOL_TIMEOUT_SECONDS
    servers={'gaston_verify':{'type':'stdio','command':sys.executable,
              'timeout':MCP_TOOL_TIMEOUT_SECONDS * 1000,
              'args':[str(runtime_script('verification_mcp.py'))],
              'env':{'GASTON_LEAD_TOKEN':token,'GASTON_SESSION_LEASE':lease,'GASTON_LEAD_PROVIDER':next((p for p in ('claude','codex') if SESSIONS._held_leases.get(lease)==(p,'reviewer')), 'claude')}}}
    # The default Playwright MCP selects system Chrome, which may crash on
    # mismatched GNOME settings. Prefer the Playwright-managed Chromium already
    # installed for the workspace, with an isolated, headless Lead-only server.
    try:
        folder=playwright_tools.test_workspace(ROOT)
        if (playwright_tools.package_installed(ROOT) and
                playwright_tools.chromium_installed(ROOT) and
                shutil.which('node') and shutil.which('npx')):
            found=subprocess.run(['node','-e',
                 "console.log(require('@playwright/test').chromium.executablePath())"],
                 cwd=folder,capture_output=True,text=True,timeout=12,check=False)
            executable=found.stdout.strip()
            if found.returncode == 0 and Path(executable).is_file():
                base=['@playwright/mcp@latest','--isolated','--executable-path',executable]
                servers['gaston_browser']={
                    'type':'stdio','command':'npx',
                    'args':[base[0],'--headless',*base[1:]],
                    'env':{'GSETTINGS_BACKEND':'memory'}}
                # A separate, isolated headed browser is available for visual
                # debugging on desktops. Merely registering it does not launch it.
                if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
                    servers['gaston_browser_visible']={
                        'type':'stdio','command':'npx','args':base,
                        'env':{'GSETTINGS_BACKEND':'memory'}}
    except (OSError,subprocess.TimeoutExpired):
        pass
    if include_maestro:
        maestro=detected_maestro()
        if maestro:
            servers['maestro']={'type':'stdio','command':maestro,'args':['mcp']}
    semantic=serena_tools.config(ROOT,'claude')
    if semantic:servers['gaston_serena']=semantic
    if project_state_name() == '.gaston':
        servers['gaston_verify']['env'].update({
            'GASTON_ENGINE_MODE':'central', 'GASTON_ENGINE_PROJECT':str(ROOT)})
    target=LOCAL/'lead-verification.mcp.json'
    write_json(target,{'mcpServers':servers})
    return target



def lead_safe_mcp_permission_rules() -> list[str]:
    """Individual Claude CLI permission rules, never a single CSV-like rule.

    Claude Code's --allowedTools consumes space-separated rules, while --tools
    uses comma-separated built-in names. Treating both flags the same can leave
    intended MCP permissions ineffective even though the server is configured.
    """
    from verification_mcp import TOOLS, CENTRAL_DISABLED_TOOLS
    excluded={'git_commit','workspace_preview','workspace_housekeeping','workspace_document','validate_evolution','export_toolkit','recover_verification_reservation'}
    if central_mode():excluded.update(CENTRAL_DISABLED_TOOLS)
    approved = ['mcp__gaston_verify__' + tool['name'] for tool in TOOLS
                if tool['name'] not in excluded]
    # Unknown native tools and MCPs keep Claude's own permissions; do not
    # preapprove broad shell patterns when the old syntactic guard is removed.
    approved += ['Bash(pwd)']
    if serena_tools.executable(ROOT):approved += ['mcp__gaston_serena__'+name for name in sorted(serena_tools.SEMANTIC_TOOLS)]
    return approved


def claude_lead_permission_args() -> list[str]:
    """Use native tool discovery and permissions, with a file-edit fallback.

    No --tools list or strict MCP config: retain Claude's native tools,
    connectors, skills and user/project settings. In interactive default mode
    unapproved actions can ask the developer instead of being denied by Gaston.
    A noninteractive call must report any unavailable authorization honestly.
    """
    from native_hooks import claude_write_guard_configured
    args = ['--permission-mode', 'default', '--allowedTools', *lead_safe_mcp_permission_rules()]
    if not claude_write_guard_configured(ROOT):
        args += ['--disallowedTools', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit']
    return args


def claude_prompt_refresh_args() -> list[str]:
    """Rebuild resumed session prompts when supported by installed Claude CLI.

    Claude Code snapshots the first system prompt even across --resume; merely
    passing --append-system-prompt on resume is NOT enough (CLI docs). This
    flag was introduced in Claude Code 2.1.257. Avoid breaking older CLIs.
    """
    try:
        result = subprocess.run(['claude', '--version'], capture_output=True,
                                text=True, timeout=5, check=False)
        match = re.search(r'(?<!\d)(\d+)\.(\d+)\.(\d+)', result.stdout or '')
        if result.returncode == 0 and match and tuple(int(x) for x in match.groups()) >= (2,1,257):
            return ['--system-prompt-snapshot', 'off']
    except (OSError, subprocess.TimeoutExpired):
        pass
    return []


def lead_runtime_brief() -> str:
    """Provider-independent role contract and fresh workspace facts."""
    try:
        found = workspace_git.repositories(ROOT)
        names = [p.relative_to(ROOT.resolve()).as_posix() for p in found]
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        names = []
        state = 'Git detection unavailable (' + str(exc)[:120] + ')'
    else:
        state = ('Detected Git repositories: ' + ', '.join(names[:20])) if names else (
            'No Git roots detected by the startup scan; run git_inspect before concluding no repository exists')
    if central_mode():
        repair_guidance='Preserve incidents and report engine defects for correction through a verified signed release; do not stage project-local toolkit changes. '
        evolution_guidance='Central engine is immutable; use signed releases and `dev update`, not project-local evolution. '
    else:
        repair_guidance='Use stage_evolution with incident_id and verify the candidate. '
        evolution_guidance='Stage, review, validate and queue reproducible toolkit fixes for idle startup. '
    return ('Gaston Lead runtime capabilities: mcp__gaston_verify__git_inspect and '
            'workspace_overview are available through the project-scoped MCP. '
            'Native shell (Bash in Claude), research, planning, plugins and connected MCP tools use provider-native permissions; '
            'no Gaston allowlist blocks unfamiliar capabilities. Use them directly for non-coding work. '
            'Use the scoped Git commit tool only on explicit user request; obtain specific authorization before sensitive actions. '
            'Lead owns investigation, test setup and test code, routine local operations and small implementation fixes. Use plan_work for the entire currently authorized deliverable: narrow monitored slices, explicit dependencies and independent inspections. Do not add unrequested future milestones; extend the plan for newly authorized work. A successful plan mutation binds that plan to the current developer turn. Never delegate a whole checkpoint or analysis the Lead can perform. Use inspect_workspace_files for batched evidence and native Read/Grep for interpretation. Delegate one substantial implementation slice with concrete preflight, paths and criteria. While Builder works, complete independent Lead investigations and test setup; use lead_edit_files with current file hash and task scope (both providers), start_lead_implementation for small code changes. Do not edit overlapping Builder scope or bypass the recorded edit path via shell, plugins or subagents. After two unsuccessful correction rounds Lead must fix directly; quota/interruption is not a code failure. Record root cause and reproduction, send focused correction deltas only. Use review_work_slice for real targeted evidence, then prepare_checkpoint_acceptance for full integrated gates; do not repeat unchanged tests. '
            'Plan integrated or unknown-scope acceptance with requires_idle_workspace=true. Standalone backend/API and web_verify gates may run alongside disjoint repositories; inspect actual read scopes and blocker results. Do not serialize independent work merely because a Builder is active. Use inspect_web_application for actual Vite UI launch and responsive screenshots even before browser specs exist; for split apps configure a project-owned gaston-web-e2e.json backend relationship and API URL contract. Visual capture does not replace formal E2E specs. Use executable, not ready, from work_plan_status. If a pending Lead item cannot run while Builder edits, defer_work_item records its blocker without completing it. After each delegation, resume or correction, finish executable independent work and directly call mcp__gaston_verify__wait_for_builder with the task ID; the wait is never refused because of other plan items. Never substitute Bash, background shell, sleep or PID polling; native MCP client background execution is different. Collect the outcome before ending the turn; if the tool errors report its actual error. Short timeout overrides are ignored; a cancelled client wait is not completion. '
            'Use specification_files for existing large requirement documents; send full requirements without artificial truncation. Before planning or after compaction call project_roadmap_status, read developer-approved context documents at their actual project paths, and inspect recorded decisions. Project workflows and filenames are developer-defined. If context is not registered, inspect existing docs/history and ask which sources are approved; use register_project_context_sources to record them. The structured record_project_roadmap tool is optional. Never invent future work. For a fresh Lead inspect the active project state local/lead-checkpoint.json (if present), PROJECT_CONTEXT.md and outstanding task/plan records; never replay raw transcripts. '
            'For semantic symbol discovery, dependency or cross-file reference tracing, prefer Gaston Serena when available before broad file reads; use native tools for small reads, edits or fallback. Never use Serena for edits. Batch review findings; handle editorial fixes directly. At natural checkpoint boundaries call lead_context_budget; when high preserve current intent with checkpoint_lead_context, then immediately continue the authorized plan using provider-native automatic compaction. Do not end the turn at a milestone or ask for a wake-up message. Stop only when the authorized deliverable is fully accepted, the developer explicitly pauses, or a concrete blocker cannot be safely resolved. Record an actual pause/blocker with continue_work=false and specific stop_reason; not context size. If a provider hard limit prevents compaction, report the exact limitation. Eligible rollover occurs only on next idle launch after completed or explicitly stopped work, not mid-chat. Never initiate work beyond the current developer authorization. On provider quota ask whether to schedule one-time automatic recovery; quota_recovery_decision records explicit consent and schedules a Builder-only local wakeup. Never use CronCreate or fork a Lead for recovery. Never guess a timezone or schedule without consent. '
            'Before the FIRST Java Builder delegation in a project or after a build dependency change, use prepare_builder_dependencies on the trusted Lead-visible source to check the isolated cache (offline first). If dependencies are missing, ask for explicit developer approval before an online retry; do not delegate into an unprepared cache and rely on Lead takeover. The preflight is not acceptance and Builder must recompile its final changes. For Builder Java compile blocks caused by missing dependencies, ask the developer before authorizing network access. Only after explicit approval instruct the Builder to rerun python .agentic/builder_compile.py <project> --allow-network; this still tries offline first and retries online once only for dependency-resolution blocks. Never infer network consent from earlier tasks. '
            'For service-level changes, the Lead writes or configures a project-owned gaston-e2e.json and isolated test launcher with real HTTP contracts; delegate only complex implementation-dependent fixtures if genuinely necessary. Run task-linked backend and managed application API checks; finalize only when required gates pass for the current revision. Never substitute smoke fixtures or JUnit for live API evidence. Read every changed and relevant document in full, including API contracts, decision records and UML deviations, and compare with the implementation and original requirements. Resolve material code or documentation gaps in this same task before acceptance; record genuine non-blocking deferrals with reasons, owner and checkpoint; obtain approval before accepting material residual risk. Re-read corrections and record exact-path reviews with current evidence. Reuse recorded current passes. Never ask the developer for manual Git output '
            'without attempting available tools. '
            'Engineering quality first: prefer a robust, well-reasoned system architecture over the shortest or quickest implementation. For substantial changes, inspect module boundaries, domain models, data flow, contracts, dependencies, failure modes and integration points; compare viable approaches and document consequential tradeoffs. Respect existing project architecture and choose only complexity justified by real requirements, not decorative abstraction. Explain why and invariants near cryptography, concurrency, transactions, finance, retries and difficult algorithms; avoid comments that restate code. Before slice or final acceptance use task_code_review and record_code_review for each changed source/test, including deletion effects. Fix material findings and reread changed code; no comment-count or complexity quotas. Use code_health advisory hints when useful, no extra model or full scan by default. '
            'Runtime recovery: native stale lock and abandoned task reconciliation run automatically; call resume_builder for an interrupted resumable task, preserving its task ID, revision and correction count. If a tool reports repair_incident, investigate it directly before blind retries, ' + repair_guidance + 'Do not delete guard files or edit task state manually; preserve unknown or active owners. ' 
            'Continuous improvement: inspect learning_report after significant failures and acceptance; record verified reusable lessons; use code_health for concrete candidates, never automatic deletion. ' + evolution_guidance + 'Never edit live toolkit, protected recovery controller or baseline tests through shell or other tools. Keep private project context local; do not start speculative improvement jobs or extra model calls during active implementation. ' + state + '.')


def lead_safe_mcp_permissions() -> str:
    """Backward-compatible representation for documentation/legacy checks."""
    return ','.join(x for x in lead_safe_mcp_permission_rules() if x.startswith('mcp__'))


def builder_runtime_brief() -> str:
    """Both providers retain native implementation capabilities, not Lead authority."""
    return ('You are the Gaston Builder, not the Engineering Lead. Use native tools, skills, '
            'research, official documentation, connected MCP servers, read-only Git inspection, '
            'dependency inspection and necessary implementation commands (such as code generation) '
            'within the delegated task and provider-native permissions. Stay within the declared write scope, self-review actual diff, wiring, dependencies and interface/test contracts before returning. Optimize for production-quality design, correctness and maintainability, not minimum code or superficial simplicity. Examine architecture boundaries, domain models, data flow, failure handling and contractual compatibility; implement justified abstractions and explicitly report design tradeoffs. Explain non-obvious why and invariants near difficult logic, not obvious mechanics. Remove proven obsolete code in scope. For Java changes run python .agentic/builder_compile.py <project> from workspace root (offline-first, isolated shared dependency cache, compile-only). If blocked specifically by missing dependencies, report the exact blocker and ask the Lead; never use --allow-network unless the Lead explicitly confirms developer approval. With that approval, rerun with --allow-network; the helper retries online only after an offline dependency-resolution block. Do not retry cache-permission or source compilation failures. Do not run tests, '
            'acceptance checks or autonomous repair cycles; the Lead runs tests and decides corrections. '
            'Do not invoke other agents or the private Lead verifier to bypass this division. '
            'Do not alter Gaston session records, hooks, credentials or permission settings. '
            'Preserve unrelated changes; explicitly authorized Git writes and sensitive actions only. '
            'If Gaston Serena is available, use symbol overview, exact symbol and reference retrieval for complex code; prefer native tools for simple reads and edits. Avoid repeated unchanged reads and broad outputs. '
            'If a required operation is unavailable or needs approval, report the actual blocker to the Lead.')


def claude_builder_mcp_args():
    semantic=serena_tools.config(ROOT,'claude')
    if not semantic:return []
    target=LOCAL/'builder-semantic.mcp.json'
    write_json(target,{'mcpServers':{'gaston_serena':semantic}})
    return ['--mcp-config',str(target)]


def claude_builder_permission_args(*, planning: bool = False) -> list[str]:
    """Retain native tools and connector permissions. Planning stays read-only."""
    args=['--permission-mode', 'plan' if planning else 'acceptEdits', '--disallowedTools', 'mcp__gaston_verify__*']
    if serena_tools.executable(ROOT):args += ['--allowedTools', *('mcp__gaston_serena__'+name for name in sorted(serena_tools.SEMANTIC_TOOLS))]
    return args


def codex_builder_permission_args(sandbox: str, *, interactive: bool) -> list[str]:
    """Keep the native workspace sandbox and noninteractive approval boundary."""
    args = ['--sandbox', sandbox]
    if not interactive:
        args += ['-c', 'approval_policy="never"']
    return args


def codex_builder_env() -> dict:
    env = dict(os.environ)
    env.pop('GASTON_LEAD_TOKEN', None)
    env.pop('GASTON_READONLY_DATABASE_URL', None)
    env.pop('GASTON_DATABASE_ALLOW_FIXTURE_DATA', None)
    return env


def codex_lead_permission_args(*, interactive: bool) -> list[str]:
    """Keep direct application files read-only without filtering native tools.

    Interactive Codex retains its configured native approval policy. Scripted
    reviews cannot solicit permission and must report blocked operations.
    Scoped verifier operations remain available in both modes.
    """
    args = ['--sandbox', 'read-only']
    if not interactive:
        args += ['-c', 'approval_policy="never"']
    return args


def codex_lead_mcp_args() -> list[str]:
    """Role-local MCP override; never register the verifier for Codex Builder.

    -c accepts TOML literals. Avoid user-scope `codex mcp add`, which would
    expose the Lead-only delegate tool to unrelated sessions/projects.
    """
    conf = load_json(lead_verification_mcp_config())['mcpServers']['gaston_verify']
    return [
        '-c', 'mcp_servers.gaston_verify.command=' + json.dumps(conf['command']),
        '-c', 'mcp_servers.gaston_verify.args=' + json.dumps(conf['args']),
        '-c', 'mcp_servers.gaston_verify.tool_timeout_sec=' + str(conf['timeout'] // 1000),
        '-c', 'mcp_servers.gaston_verify.env.GASTON_LEAD_TOKEN=' + json.dumps(conf['env']['GASTON_LEAD_TOKEN']),
        '-c', 'mcp_servers.gaston_verify.env.GASTON_SESSION_LEASE=' + json.dumps(conf['env'].get('GASTON_SESSION_LEASE','')),
        '-c', 'mcp_servers.gaston_verify.env.GASTON_LEAD_PROVIDER=' + json.dumps(conf['env'].get('GASTON_LEAD_PROVIDER','codex')),
        '-c', 'mcp_servers.gaston_verify.env_vars=' + json.dumps(['GASTON_READONLY_DATABASE_URL','GASTON_DATABASE_ALLOW_FIXTURE_DATA']),
    ]


def recover_claude_session(role: str):
    """Called only under the role lock. Never delete provider transcripts.

    A failed startup can leave an enrolled UUID without a native conversation.
    Archive only a missing local binding; preserve any existing transcript and
    fail closed if the provider store cannot be inspected.
    """
    from session_store import claude_transcript_missing
    existing = SESSIONS.get('claude',role)
    if existing and existing.get('status') in ('initializing', 'attention', 'ready'):
        missing = claude_transcript_missing(existing['id'],
                    allow_absent_store=existing['status'] in ('initializing','attention'))
        if missing:
            SESSIONS.rotate('claude',role)
            print('Previous Claude conversation is missing locally; starting a new session.',flush=True)
            return None
    return existing


def claude_structured(prompt: str, schema: dict, path: Path, model: str, *, role: str):
    if not command_exists('claude'):
        fail('Claude Code CLI unavailable')
    with SESSIONS.locked('claude','reviewer'):
        existing = recover_claude_session('reviewer')
        if existing:
            session_args = ['--resume', existing['id']]
        else:
            session_id = str(uuid.uuid4())
            SESSIONS.bind('claude', session_id, status='initializing',role='reviewer')
            session_args = ['--session-id', session_id]
        # The Lead can execute independent, allowlisted verification via its
        # private MCP; native tools use Claude permissions and the Lead role contract.
        cmd = ['claude', '-p', *session_args, *(['--model', claude_model_id(model)] if model else []), '--max-turns', '16',
               *claude_effort_args('claude','reviewer'), *claude_advisor_args('reviewer'),
               '--mcp-config', str(lead_verification_mcp_config()),
               *claude_prompt_refresh_args(),
               *claude_lead_permission_args(),
               '--append-system-prompt', lead_runtime_brief(),
               '--json-schema', json.dumps(schema, separators=(',', ':')),
               '--output-format', 'json']
        result = run_process(cmd, path.with_suffix('.log'), stdin=prompt, ai=True, env=claude_advisor_env('reviewer'))
        if result.returncode:
            SESSIONS.mark('claude', 'attention', role='reviewer')
            fail(f'Claude {role} failed; existing session kept. Inspect {path.with_suffix(".log")}')
        try:
            envelope = json.loads(result.stdout)
            structured = envelope.get('structured_output')
            if structured is None: structured = json.loads(envelope['result'])
            if isinstance(structured, str): structured = json.loads(structured)
            if not isinstance(structured, dict): raise ValueError('Not a JSON object')
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            SESSIONS.mark('claude', 'attention', role='reviewer')
            fail(f'Claude returned no usable structured result: {exc}. Session preserved.')
        write_json(path, structured)
        SESSIONS.mark('claude', 'ready',role='reviewer')
        return structured


def codex_structured(prompt: str, schema: dict, path: Path, model: str,
                     sandbox: str, *, role: str):
    if not command_exists('codex'):
        fail('Codex CLI unavailable')
    schema_file = path.with_suffix('.schema.json')
    write_json(schema_file, schema)
    session_role = 'build' if role in ('build','plan') else 'reviewer'
    session_role = 'builder' if session_role == 'build' else session_role
    with SESSIONS.locked('codex',session_role):
        existing = SESSIONS.get('codex',session_role)
        existing,before,rotation=ti.begin_builder(SESSIONS,ROOT,'codex',existing,path,role=session_role)
        permissions = (codex_lead_permission_args(interactive=False) if session_role == 'reviewer'
                       else codex_builder_permission_args(sandbox, interactive=False))
        cmd = ['codex', 'exec', '--json', *permissions,
               *codex_effort_args('codex',session_role),
               *(codex_lead_mcp_args() if session_role=='reviewer' else []), *serena_tools.codex_args(ROOT)]
        # Codex exec requires an explicit opt-out when the workspace itself is not a Git repo.
        git_root = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--is-inside-work-tree'],
                                  capture_output=True, text=True, check=False)
        if git_root.returncode != 0 or git_root.stdout.strip() != 'true':
            cmd.append('--skip-git-repo-check')
        if model: cmd += ['--model', model]
        output_target=path.with_name(path.stem+'-pending-'+uuid.uuid4().hex+'.json') if rotation is not None else path
        cmd += ['--output-schema', str(schema_file), '--output-last-message', str(output_target)]
        if existing: cmd += ['resume', existing['id']]
        cmd += ['-']
        if session_role == 'reviewer':
            prompt = lead_runtime_brief() + '\n\n' + prompt
        else:
            prompt = builder_runtime_brief() + '\n\n' + prompt
        log=ti.attempt_log(path) if rotation is not None else path.with_suffix('.log')
        # Keep the previous durable implementation result on disk while the
        # provider writes a distinct pending output. An interrupted run cannot
        # lose prior paths or be mistaken for a successful structured result.
        if rotation is not None and path.is_file():
            shutil.copy2(path,path.with_name(path.stem+'-previous-'+uuid.uuid4().hex+'.json'))
        result=None; observed=None
        try:
            result = run_process(cmd, log, stdin=prompt, ai=True,
                                 env=codex_builder_env() if session_role == 'builder' else None)
            observed = extract_codex_thread(result.stdout)
        finally:
            if not observed and rotation is not None and log.is_file():
                try:
                    with log.open(encoding='utf8',errors='replace') as stream:
                        for line in stream:
                            observed=extract_codex_thread(line)
                            if observed:break
                except OSError:pass
            mismatch=bool(existing and observed and observed!=existing['id'])
            if not existing and observed:
                SESSIONS.bind('codex', observed, status='initializing',role=session_role)
            if rotation is not None and not mismatch:
                try:ti.finish_builder(ROOT,'codex',observed or (existing or {}).get('id'),path,before,rotation,returncode=result.returncode if result is not None else None)
                except (OSError,ValueError,TypeError,KeyError) as exc:print('Token telemetry unavailable: '+type(exc).__name__,flush=True)
        if mismatch:
            fail('Codex resumed a DIFFERENT session. Refusing to rebind; inspect CLI version and logs.')
        if result.returncode:
            if SESSIONS.get('codex',session_role): SESSIONS.mark('codex', 'attention',role=session_role)
            fail(f'Codex {role} failed. Session was not silently replaced; inspect {path.with_suffix(".log")}')
        if not SESSIONS.get('codex',session_role):
            fail('Codex produced no thread.started ID; cannot guarantee persistent reuse. Inspect CLI JSON log, then bind explicitly.')
        if not output_target.exists():
            SESSIONS.mark('codex', 'attention',role=session_role)
            fail(f'Codex {role} returned no structured output; session preserved')
        data = load_json(output_target)
        if output_target!=path:os.replace(output_target,path)
        SESSIONS.mark('codex', 'ready',role=session_role)
        return data


def provider_structured(provider: str, prompt: str, schema: dict, path: Path,
                        config: dict, role: str):
    if provider == 'codex':
        return codex_structured(prompt, schema, path, config.get('models',{}).get('codex_reviewer' if role in ('review','feedback','lead_plan') else 'codex_builder',''),
                                'read-only' if role in ('review','feedback','plan','lead_plan') else 'workspace-write', role=role)
    if provider == 'claude':
        if role in ('review','feedback','lead_plan'):
            return claude_structured(prompt, schema, path, config.get('models',{}).get('claude_reviewer',''), role=role)
        # Dedicated Claude builder session, used only when Claude is explicitly
        # selected as builder (not the registered read-only reviewer).
        with SESSIONS.locked('claude','builder'):
            existing = recover_claude_session('builder')
            existing,before,rotation=ti.begin_builder(SESSIONS,ROOT,'claude',existing,path,role='builder')
            if existing:
                session_args = ['--resume', existing['id']]
            else:
                session_id = str(uuid.uuid4())
                SESSIONS.bind('claude', session_id, status='initializing',role='builder')
                session_args = ['--session-id', session_id]
            model = config.get('models', {}).get('claude_builder', '')
            cmd = ['claude', '-p', *session_args,
                   *(['--model', claude_model_id(model)] if model else []),
                   *claude_builder_permission_args(planning=role=='plan'), *claude_builder_mcp_args(),
                   *claude_prompt_refresh_args(),
                   '--append-system-prompt', builder_runtime_brief(), '--max-turns', '24',
                   '--json-schema', json.dumps(schema, separators=(',', ':')),
                   '--output-format', 'json']
            cmd += claude_effort_args('claude','builder')
            log=ti.attempt_log(path) if rotation is not None else path.with_suffix('.log')
            if rotation is not None and path.is_file():shutil.copy2(path,path.with_name(path.stem+'-previous-'+uuid.uuid4().hex+'.json'))
            result=None
            try:
                result = run_process(cmd, log, stdin=prompt, ai=True, env=claude_advisor_env('builder'))
            finally:
                if rotation is not None:
                    try:ti.finish_builder(ROOT,'claude',SESSIONS.get('claude','builder')['id'],path,before,rotation,returncode=result.returncode if result is not None else None)
                    except (OSError,ValueError,TypeError,KeyError) as exc:print('Token telemetry unavailable: '+type(exc).__name__,flush=True)
            if result.returncode:
                SESSIONS.mark('claude', 'attention',role='builder')
                fail(f'Claude build failed; session preserved. Inspect {path.with_suffix(".log")}')
            try:
                envelope = json.loads(result.stdout)
                data = envelope.get('structured_output')
                if data is None: data = json.loads(envelope['result'])
                if isinstance(data, str): data = json.loads(data)
                if not isinstance(data, dict): raise ValueError('Not a JSON object')
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                SESSIONS.mark('claude', 'attention',role='builder')
                fail(f'Claude build did not produce structured output: {exc}')
            write_json(path, data)
            SESSIONS.mark('claude', 'ready',role='builder')
            return data
    fail(f'Unsupported provider {provider}')


def find_builder(requested: str):
    if requested == 'auto':
        return choose('codex', 'claude')
    return requested if command_exists(requested) else None


def find_reviewer(requested: str, builder: str):
    if requested == 'none': return None
    if requested == 'auto':
        other = 'claude' if builder == 'codex' else 'codex'
        return other if command_exists(other) else builder if command_exists(builder) else None
    # Two separately registered sessions make same-provider review possible.
    return requested if command_exists(requested) else None


MODEL_ROLES = ('codex_builder', 'codex_reviewer', 'claude_builder', 'claude_reviewer')


def models_command(operation='show', role=None, name=None):
    """Show or update role-specific models/efforts and the Claude Lead Advisor."""
    cfg=load_config()
    models=cfg.setdefault('models',{})
    prefs=cfg.get('preferences',{})
    assigned=[(r,prefs.get(r,'codex' if r=='builder' else 'claude')) for r in ('builder','reviewer')]
    if operation=='advisor':
        choice=(role or '').lower()
        if choice not in CLAUDE_ADVISORS and not valid_model_id('claude',choice):
            fail('Choose an exact Claude Advisor model ID or off')
        if prefs.get('reviewer')!='claude':
            fail('Native Claude Advisor is available only when Claude is Engineering Lead')
        cfg.setdefault('advisor',{})['claude_reviewer']=choice
        write_json(CFG_FILE,cfg)
        print(f'Claude Engineering Lead Advisor: {choice}. Builder Advisor remains disabled.')
        return
    if role=='lead': role='reviewer'
    if operation in ('set','reset'):
        if role in ('builder','reviewer'):
            role=prefs.get(role,'codex' if role=='builder' else 'claude')+'_'+role
        if role not in MODEL_ROLES:
            fail('Choose builder or reviewer (or a provider_role combination)')
        if operation=='set':
            if not name or not name.strip() or len(name)>128:
                fail('Supply a valid model ID')
            models[role]=name.strip()
        else:
            provider, assigned_role=role.split('_',1)
            model=recommended_model(provider,assigned_role)
            if not model:
                fail('Cannot determine a recommended Codex model. Run dev init to update the local Codex model catalog')
            models[role]=model
        provider,assigned_role=role.split('_',1)
        if provider=='codex':
            proposed=configured_effort(cfg,provider,assigned_role)
            ok,reason=verify_codex_model_effort(models[role],proposed,verified=cfg.get('codex_verified_pairs'))
            if not ok:
                fail('Model/effort combination rejected: '+reason)
            cfg.setdefault('reasoning',{})[role]=proposed
        write_json(CFG_FILE,cfg)
    print('Models')
    for assigned_role,provider in assigned:
        if provider not in ('codex','claude'):continue
        key=f'{provider}_{assigned_role}'
        extra=(' | Advisor: '+advisor_id(cfg)
               if provider=='claude' and assigned_role=='reviewer' else '')
        print(f'  {("Lead" if assigned_role=="reviewer" else "Builder"):<9} {provider.capitalize():<7} {models.get(key) or "NOT CONFIGURED"} | reasoning: {configured_effort(cfg,provider,assigned_role)}{extra}')
    if operation!='show' or not sys.stdin.isatty():
        if operation=='show':
            print('Change models by running dev models interactively.')
            print('Claude Lead Advisor is configurable in the interactive menu.')
        return
    while True:
        choice=input('Choose role (1 = Builder, 2 = Engineering Lead) [default: Keep current]: ').strip()
        if not choice: return
        if choice in ('1','2'): break
        print('Invalid role; please choose again.')
    assigned_role, provider=assigned[int(choice)-1]
    key=f'{provider}_{assigned_role}'
    print(f'\n{provider.capitalize()} {assigned_role} model:')
    from model_menu import codex_catalog_details
    catalog=codex_catalog_details() if provider=='codex' else None
    try:
        selected,direct_effort=pick_role_model(provider,assigned_role,models.get(key,''),catalog=catalog,config=cfg)
        effort=direct_effort if direct_effort is not None else pick_effort(provider,assigned_role,selected,cfg.get('reasoning',{}).get(key,''),catalog=catalog)
    except ModelCatalogUnavailable as exc:
        print('Settings not saved: '+str(exc))
        return
    models[key]=selected
    cfg.setdefault('reasoning',{})[key]=effort
    if provider=='claude' and assigned_role=='reviewer':
        saved=advisor_id(cfg)
        if saved!='off' and not valid_model_id('claude',saved): saved=CLAUDE_ADVISORS[0]
        print('Native Advisor (Engineering Lead only): '+ '  '.join(f'{i}. {m}' for i,m in enumerate(CLAUDE_ADVISORS,1)))
        choices={str(i):m for i,m in enumerate(CLAUDE_ADVISORS,1)}
        while True:
            raw=input(f'Choose Advisor (1–3 or exact ID) [default: {saved}]: ').strip()
            advisor=choices.get(raw,raw) if raw else saved
            if advisor=='off' or valid_model_id('claude',advisor):
                cfg.setdefault('advisor',{})['claude_reviewer']=advisor
                break
            print('Invalid Advisor choice; please choose again.')
    if provider=='codex':
        ok,reason=verify_codex_model_effort(selected,effort,catalog=catalog,verified=cfg.get('codex_verified_pairs'))
        if not ok:
            print('Settings NOT saved: '+reason)
            return
    write_json(CFG_FILE,cfg)
    print('Settings saved. Codex choices have catalog metadata or direct-check evidence; account access can change.')


def make_summary(rundir: Path) -> str:
    """Deterministic concise summary from AI structured results and actual gate evidence.

    No extra paid model invocation; do not present the AI's claims as test evidence.
    """
    def read(name: str) -> dict:
        path = rundir / name
        if not path.exists(): return {}
        try: return load_json(path)
        except SystemExit: return {}
    task, state = read('task.json'), read('state.json')
    build, verify_report, review_report = read('build.json'), read('verify.json'), read('review.json')
    stage = state.get('stage', 'unknown').replace('_', ' ')
    title = task.get('goal', rundir.name).strip()
    lines = [f'# {title}', f'**Status:** {stage}', '']
    if build.get('summary'):
        lines.append('**Implemented (agent-reported):** ' + ' '.join(build['summary'].split())[:320])
    selection = verify_report.get('selection', {})
    if selection.get('areas'):
        lines.append('**Verification coverage:** ' + ', '.join(selection['areas']))
    if selection.get('cross_component_checks'):
        lines.append('**Related sections:** ' + ', '.join(selection['cross_component_checks']))
    checks = verify_report.get('checks', [])
    if checks:
        passed = sum(c.get('result') == 'pass' for c in checks)
        lines.append(f'**Checks:** {passed}/{len(checks)} passed')
        failed = [c.get('name','unnamed') for c in checks if c.get('result') != 'pass']
        if failed: lines.append('**Failed/blocked:** ' + ', '.join(failed[:5]))
    elif stage not in ('planning', 'implementing'):
        lines.append('**Checks:** no completed test evidence')
    if review_report:
        lines.append('**Independent review:** ' + str(review_report.get('verdict','unknown')))
        problems = review_report.get('findings', [])
        if problems:
            lines.append('**Top findings:** ' + '; '.join(f"{f.get('severity','?')}: {f.get('issue','')}"[:160] for f in problems[:3]))
        gaps = review_report.get('coverage_gaps', [])
        if gaps: lines.append('**Coverage gaps:** ' + '; '.join(str(x)[:120] for x in gaps[:3]))
    elif stage in ('ready_for_human_release_review', 'checks_passed_review_missing', 'checks_failed', 'changes_requested'):
        lines.append('**Independent review:** not completed')
    risks = build.get('remaining_risks', [])
    if risks: lines.append('**Known risks:** ' + '; '.join(str(x)[:120] for x in risks[:3]))
    questions = state.get('questions', [])
    if questions: lines.append('**Needs your decision:** ' + '; '.join(str(q)[:120] for q in questions[:3]))
    if state.get('reason'): lines.append('**Blocker:** ' + str(state['reason'])[:250])
    lines.append('**Signoff:** human approval is still required for deployment or real money movement')
    return '\n'.join(lines) + '\n'


def write_summary(rundir: Path):
    path = rundir / 'SUMMARY.md'
    path.write_text(make_summary(rundir), encoding='utf-8')
    return path


def print_summary(task_id: str | None = None):
    task_id = task_id or latest_task_id()
    if not task_id: fail('No completed run found')
    folder = run_dir(task_id)
    if not folder.exists(): fail('No such task')
    print(make_summary(folder))
    print(f'Report: {folder / "SUMMARY.md"}')


def save_state(rundir: Path, stage: str, **attrs):
    data = {'stage': stage, 'updated_at': stamp(), **attrs}
    write_json(rundir / 'state.json', data)
    write_summary(rundir)
    friendly = stage.replace('_', ' ').upper()
    print(f'\n  [{friendly}]  {rundir.name}', flush=True)
    if stage in ('needs_input', 'risk_approval_required', 'checks_failed', 'checks_passed_review_missing',
                 'ready_for_human_release_review', 'changes_requested', 'blocked', 'plan_ready'):
        print('  Final report will be printed by this command.', flush=True)


def doctor():
    print('Repository:', ROOT)
    for binary in ['git', 'codex', 'claude', 'python', 'java', 'flutter', 'adb', 'maestro', 'node', 'npm', 'npx', 'docker', 'k6']:
        print(f'  {binary:>8}: {shutil.which(binary) or "not installed"}')
    print('Build and review models:', load_config().get('models', {}))
    print('Tip: codex login / claude auth login use your signed-in plans when supported.')


def active_policy_context(cfg: dict) -> str:
    files = cfg.get('policy_files', DEFAULT_POLICIES)
    if not isinstance(files, list):
        fail('policy_files must be a list')
    result = []
    for file in files:
        path = (ROOT / str(file)).resolve()
        if not path.is_relative_to(ROOT.resolve()):
            fail('Policy file must be inside repository')
        if not path.is_file():
            fail(f'Enabled policy file is missing: {file}')
        result.append(str(path.relative_to(ROOT)))
    return ', '.join(result) or '(no optional policies; core security constraints still apply)'


def planning_prompt(task: dict) -> str:
    return (
      'You are a staff engineer performing a READ-ONLY planning gate. Do not modify files. '
      'Inspect only relevant code, project instructions and dependencies. '
      'Ask questions ONLY when a missing requirement materially affects architecture, money movement, security, '
      'regulatory scope, the user-facing result, or acceptance criteria. '
      'Do not invent assumptions about payment providers, ledger posting, compliance, test credentials or workflows. '
      'For minor details use established repository conventions. For substantial work, reason deeply about architecture: responsibilities, interface/data contracts, concurrency, security, reliability, observability, migration and integration effects; evaluate alternatives and justify significant decisions. Do not choose a design merely because it is the smallest patch. Avoid speculative layers not justified by the project. '
      'Return schema-valid JSON. Set status needs_input when there are blocking questions. '
      'Consult .agentic/local/PROJECT_CONTEXT.md if present. '
      'Flag money movement, PII, auth, schema changes, production access and external integrations. '
      'Project roots and real test gates are rediscovered automatically at each task, including later-added Java, Flutter and web repos; never require the user to rerun init just to register tests. '
      'Choose verification_areas for the smallest sufficient suite; include other affected sections whenever a changed contract, DB schema, identity flow, shared data, or client integration can change their observable behavior. '
      'Use backend, api, database, security, performance, mobile, web areas only where justified. '
      'Explain the selected areas and significant excluded areas in verification_rationale. '
      'Request specific extra real test gates using verification_checks when needed, e.g. mobile_e2e for a changed native purchase flow or api_integration for an API dependency. Never invent a check name that is not implemented. '
      'Prefer meaningful existing tests and a few high-value negative/boundary cases over repetitive generic tests. '
      'Do not include confidential data in your output. ' 
      'Read only ENABLED rules named below and core AGENTS.md. Treat custom rules as human-provided requirements, '
      'not as proof of current applicable law. Do not assume legal applicability without an official source.\n'
      'ENABLED RULE FILES: ' + active_policy_context(load_config()) +
      '\n\nTASK:\n' + json.dumps(task, indent=2)
    )


def build_prompt(task: dict, plan: dict, *, repair: str = '') -> str:
    return (
      'You are the Builder: implement one requested scoped implementation slice. The Lead normally writes tests and sets up fixtures; only edit test files explicitly included in your write scope. '
      'Use native tools, skills, documentation research, connected MCP servers and necessary non-test implementation commands. '
      'The Engineering Lead owns the task outcome, review, execution of all tests, '
      'verification, and decisions about additional corrections. '
      'For Java changes use python .agentic/builder_compile.py <project> (offline-first compile-only). If missing dependencies block compilation, report the blocker and wait for Lead authorization; only then may you rerun with --allow-network. Do not run test or acceptance commands (Maven tests, Playwright tests, Maestro flows, or application API checks). '
      'Do not autonomously repair failures or invoke another agent. '
      'When the Lead asks for a correction, implement only that specific correction delta and return. Self-review the actual diff, API/fixture formats, dependency injection and integration contracts before returning; never claim a test pass. '
      'Read AGENTS.md and relevant scoped docs before editing. Deliver production-quality, architecturally coherent '
      'implementation that preserves boundaries and contracts. Do not optimize for the fewest lines, files or fastest apparent delivery. Use focused tests as source files when requested. Model domain responsibilities, failure scenarios and integration effects; justify material abstractions and design tradeoffs. Document non-obvious WHY, invariants, business rules, transaction/concurrency, security and rounding decisions near difficult code. Do not comment obvious statements. Remove proven obsolete code in scope. Self-review readability and maintainability before handoff. '
      'This handoff authorizes local implementation only, not deployment, use of production credentials, production data access or destructive operations. A mention of such topics in specifications is not authorization to perform them. '
      'Preserve unrelated user edits and report any overlaps. '
      'Never stage, commit, stash, create branches, reset, clean, merge, or push unless '
      'the developer explicitly requested that exact Git operation. '
      'Do not ask the developer to commit or stash as a prerequisite. '
      'Report only actual changed files and remaining implementation risks. '
      'For ambiguous or high-risk requirements, return blocked and questions; do not guess. '
      'Reply only with JSON conforming to the provided schema.\n\nTASK:\n'
      + json.dumps(task,indent=2) + '\nPLAN:\n' + json.dumps(plan,indent=2)
      + ('\nLEAD REQUESTED CORRECTION:\n' + repair if repair else '')
    )


def review_prompt(task: dict, rundir: Path, verify: dict) -> str:
    return (
      'You are a skeptical independent staff-level reviewer. NEVER edit files. '
      'Inspect the labeled per-repository Git diffs, changed source code, tests and relevant project requirements. For kind=policy or verification, also independently inspect .agentic/config.json and relevant .agentic/policies/custom/ files because the shared workspace itself may not be a Git repository. '
      'Do NOT trust implementer summary. Validate each acceptance criterion and flag unsupported claims. Assess architectural coherence, responsibility boundaries, coupling, data ownership, integration contracts, migration/compatibility and failure modes, not just local correctness or cosmetic readability. Challenge shortcuts that create long-term debt and abstractions without demonstrated value; request concrete design fixes. '
      'Review business invariants (amounts/precision, currency, idempotency, ledger invariants, concurrency), '
      'authz, API contracts, data migrations, privacy, failure handling, maintenance and performance regressions. '
      'Assess only the enabled policies: ' + active_policy_context(load_config()) + '. Never claim legal certification. '
      'Review .agentic/local/PROJECT_CONTEXT.md, the task changes.diff and changes_manifest.txt; '
      'the diff omits untracked file contents, so inspect them explicitly. '
      'Distinguish verified observations from checks requiring a running backend or device. '
      'An absent E2E device/test is a coverage gap, never a pass. '
      'Inspect verification_plan.json for scope and cross-component coverage; if changed API contracts, DB behavior, auth, or clients affect other sections, require meaningful tests for those sections too. '
      'Challenge unnecessary duplicate tests and gates that pass without checking behavior; demand actual curl/API responses and DB assertions when applicable. '
      'Provide precise file references, severity and actionable fixes. If any critical/high issue exists, '
      'verdict must be changes_requested. If unable to inspect evidence, verdict blocked. '
      'Return schema-valid JSON.\nTASK:\n' + json.dumps(task, indent=2) +
      '\nTEST RESULTS:\n' + json.dumps(verify, indent=2) +
      f'\nEVIDENCE FOLDER: {rundir.relative_to(ROOT)}\n'
    )


def check_cwd(step: dict) -> Path:
    directory = (ROOT / step.get('cwd', '.')).resolve()
    if not directory.is_relative_to(ROOT.resolve()):
        fail('Check cwd must remain within repository')
    return directory


def verification_plan(cfg: dict, task: dict) -> dict:
    """Select necessary gates from real changes and Lead-requested risk areas.

    An absent gate is a blocking coverage gap, never a fabricated pass.
    """
    cfg = refresh_workspace_checks(cfg)
    steps = list(cfg.get('checks', []))
    browser_step = playwright_tools.browser_gate(ROOT)
    if (browser_step and not cfg.get('project',{}).get('discovered_roots',{}).get('web')
            and not any(s.get('name') == 'web_e2e' for s in steps)):
        steps.append(browser_step)
    spec = LOCAL / 'api-checks.json'
    if spec.is_file() and not any(s.get('name') == 'api_integration' for s in steps):
        steps.append({'name':'api_integration', 'cwd':'.',
                      'command':[sys.executable,str(runtime_script('api_checks.py')), str(LOCAL/'api-checks.json')],
                      'timeout_seconds':180})
    active = []
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get('name'), str) or not step['name']:
            fail('Every verification gate must have a nonempty name')
        if not isinstance(step.get('command'), list) or not step['command']:
            fail('Each check.command must be a non-empty argument array (no shell commands)')
        cwd = check_cwd(step)
        if step.get('when_path_exists') and not (cwd / step['when_path_exists']).exists():
            continue
        if step.get('risk') and task.get('risk', 'normal') not in step['risk']:
            continue
        active.append(step)
    try:
        changes = workspace_git.changed_paths(ROOT, cfg.get('project'))
        result = verification_planner.select_checks(
            task, active, changes, cfg.get('project',{}).get('discovered_roots',{}))
    except ValueError as exc:
        fail(str(exc))
    # A disabled check with the same name cannot be treated as available.
    return result


def load_verify_steps(cfg: dict, task: dict):
    result = verification_plan(cfg, task)
    if result['missing']:
        fail('Required evidence checks are missing: ' + ', '.join(result['missing']) +
             '. Builder must configure executable checks using real project tests.')
    if not result['checks']:
        fail('No relevant executable checks configured. Cannot claim validation.')
    return result['checks']


def verify(task: dict, rundir: Path, cfg: dict):
    plan = verification_plan(cfg, task)
    # Do not try to label a missing test as successful. Persist selection and
    # rationale so independent review can challenge it.
    public_plan = {key: value for key, value in plan.items() if key != 'checks'}
    public_plan['lead_rationale'] = task.get('verification_rationale', [])
    write_json(rundir / 'verification_plan.json', public_plan)
    results = []
    cache = {}
    for step in plan['checks']:
        name = step['name']
        cwd = check_cwd(step)
        argv = [str(x) for x in step['command']]
        if argv and argv[0]=='flutter':
            from platform_compat import resolve_flutter
            try:
                argv[0]=resolve_flutter(cwd,workspace=ROOT)
            except (OSError,ValueError) as exc:
                results.append({'name':name,'result':'blocked','reason':str(exc)})
                continue
        # Execute the same command in the same cwd only once per verification
        # pass, even when separate gates refer to it. Shared results are
        # explicitly labeled, not represented as two independent suites.
        fingerprint = (str(cwd), tuple(argv))
        if fingerprint in cache:
            previous = cache[fingerprint]
            results.append({'name': name, 'result': previous['result'],
                            'reused_from': previous['name'],
                            'evidence': previous.get('evidence'),
                            'note':'Same command reused; no independent coverage implied'})
            continue
        executable = argv[0]
        ready = (cwd / executable).is_file() if executable.startswith('./') else command_exists(executable)
        runner_ok, runner_reason = workspace_checks.runner_preflight(ROOT, step)
        if not ready:
            entry = {'name': name, 'result':'blocked', 'reason':f'Missing executable: {executable}'}
        elif not runner_ok:
            entry = {'name': name, 'result':'blocked', 'reason':runner_reason}
        elif step.get('managed_by') == workspace_checks.MANAGED and name.startswith('backend_verify'):
            import backend_runner
            # Use the same isolated cache in Builder and Lead. The Builder
            # sandbox may still prohibit downloads; the Lead can retry via
            # its independent, bounded verifier without modifying source.
            goal='verify' if argv[-1]=='verify' or argv[-1]=='check' else 'test'
            outcome=backend_runner.run(ROOT,cwd.relative_to(ROOT).as_posix(),goal=goal,
                                       timeout=int(step.get('timeout_seconds',1200)))
            evidence=rundir/'checks'/f'{name}.log'
            evidence.parent.mkdir(parents=True,exist_ok=True)
            details=outcome.get('evidence',{})
            evidence.write_text(details.get('stdout_tail','')+'\n'+details.get('stderr_tail',''),encoding='utf-8')
            entry={'name':name,'result':outcome['status'],
                   'exit_code':outcome.get('exit_code'),
                   'evidence':f'checks/{name}.log',
                   'reason':outcome.get('reason',''),
                   'cache_path':outcome.get('cache_path')}
        else:
            p = run_process(argv, rundir / 'checks' / f'{name}.log',
                            timeout=int(step.get('timeout_seconds',1200)),cwd=cwd)
            entry = {'name':name, 'result':'pass' if p.returncode == 0 else 'fail',
                     'exit_code':p.returncode, 'evidence':f'checks/{name}.log'}
        results.append(entry)
        cache[fingerprint] = entry
    for missing in plan['missing']:
        results.append({'name':missing, 'result':'blocked',
                        'reason':'Required evidence check not configured for affected area'})
    if not results:
        results.append({'name':'verification', 'result':'blocked',
                        'reason':'No relevant executable checks selected'})
    outcome = {'checks': results, 'all_passed':bool(results) and all(x['result']=='pass' for x in results),
               'selection': public_plan}
    write_json(rundir / 'verify.json', outcome)
    return outcome


def review(task: dict, rundir: Path, cfg: dict, provider: str, verify_out: dict):
    # Review changes across ALL application Git repositories in the workspace.
    try:
        diff, changes = workspace_git.review_evidence(ROOT, cfg.get('project'))
    except ValueError as exc:
        fail(str(exc))
    (rundir/'changes.diff').write_text(diff, encoding='utf-8')
    (rundir/'changes_manifest.txt').write_text(changes, encoding='utf-8')
    prompt = review_prompt(task, rundir, verify_out)
    path = rundir / 'review.json' 
    if provider == 'claude':
        return claude_structured(prompt, REVIEW_SCHEMA, path, cfg.get('models',{}).get('claude_reviewer',''), role='review')
    if provider == 'codex':
        # Independent *session*, but not independent model family when Codex built it.
        return codex_structured(prompt, REVIEW_SCHEMA, path, cfg.get('models',{}).get('codex_reviewer',''),
                                'read-only', role='review')
    fail('Review provider not available')


# --- Friendly developer interface (all actions remain local and opt-in). ---
def java_major_version() -> int | None:
    if not command_exists('java'): return None
    proc = subprocess.run(['java','-version'],capture_output=True,text=True,timeout=10,check=False)
    m = re.search(r'\bversion ["\']?(\d+)', proc.stderr + proc.stdout)
    if not m:
        m = re.search(r'\b(?:openjdk|java) (\d+)(?:[.\s]|$)', proc.stderr + proc.stdout)
    if not m:return None
    v = int(m.group(1))
    return (int(re.search(r'1\.(\d+)', proc.stderr + proc.stdout).group(1)) if v==1 and re.search(r'1\.(\d+)', proc.stderr + proc.stdout) else v)


def detected_maestro():
    from platform_compat import maestro_binary
    return maestro_binary()


def maestro_setup(*, install=False, yes=False, dry_run=False, check_only=False, language='en',
                  register_mcp=True, agents=None):
    """Explicitly approved official installer + idempotent MCP registration.

    Does not install the entire Android SDK or accept SDK licences automatically.
    Never runs a remote script in dry-run or check-only modes.
    """
    def msg(en: str, fr: str) -> str:
        return fr if language == 'fr' else en
    from terminal_ui import section, result
    section(msg('Maestro mobile testing', 'Tests mobiles Maestro'))
    print(msg('  OS:', '  Système :'), platform.system())
    major = java_major_version()
    if major is None or major < 17:
        print(msg('  Java 17+: missing or incompatible',
                  '  Java 17+ : absent ou incompatible'))
    current = detected_maestro()
    print('  Maestro:', current or msg('(not installed)', '(non installé)'))
    if not current and (install or dry_run):
        if platform.system().lower() not in ('linux','darwin'):
            print(msg('  Windows: use the official GitHub ZIP installer, then rerun this command.',
                      '  Windows : utilisez le ZIP officiel sur GitHub puis relancez la commande.'))
            return False
        if major is None or major < 17:
            print(msg('  Install/configure Java 17+ first, then rerun this command.',
                      '  Installez/configurez Java 17+ avant de relancer cette commande.'))
            return False
        url = 'https://get.maestro.mobile.dev'
        print(msg('  Proposed official installer:', '  Installateur officiel proposé :'), url)
        if dry_run:
            print(msg('  DRY RUN: would download the official installer and ask before executing it.',
                      '  SIMULATION : téléchargerait l’installateur officiel et demanderait l’autorisation.'))
            return True
        if not yes and (not sys.stdin.isatty() or ask(msg('Download and execute Maestro official installer? (yes/no)',
                                                          'Télécharger et lancer l’installateur officiel Maestro ? (oui/non)'),
                                                      msg('no','non')).lower() not in ('yes','y','oui','o')):
            print(msg('  Installer not approved; no download or execution performed.',
                      '  Installation non autorisée ; aucun téléchargement ni exécution.'))
            return False
        with tempfile.TemporaryDirectory(prefix='agentic-maestro-') as tmp:
            installer=Path(tmp)/'install-maestro.sh'
            print(msg('  Downloading installer to a temporary file (no shell piping).',
                      '  Téléchargement de l’installateur vers un fichier temporaire sécurisé.'))
            from setup_wizard import visible_run
            got=visible_run(['curl','--fail','--location','--silent','--show-error',url,'--output',str(installer)],label='Download Maestro installer',timeout=90,capture_output=True)
            if got.returncode or not installer.is_file() or installer.stat().st_size<100:
                print(msg('  Installer download failed or incomplete. See official Maestro docs.',
                          '  Téléchargement incomplet/échoué. Consultez la documentation officielle.'))
                return False
            print(msg('  Executing downloaded official Maestro installer.',
                      '  Exécution de l’installateur officiel Maestro téléchargé.'))
            installed=visible_run(['bash',str(installer)], label='Install Maestro',
                                  timeout=3600, inactivity_timeout=240)
            if installed.returncode:
                print(msg('  Maestro installation incomplete. Check your connection and rerun dev init.',
                          '  Installation de Maestro incomplète. Vérifiez la connexion et relancez dev init.'))
                return False
        current=detected_maestro()
        if not current:
            print(msg('  Install completed but maestro executable was not found. Check ~/.maestro/bin on PATH.',
                      '  Installation terminée mais binaire Maestro introuvable. Vérifiez ~/.maestro/bin dans PATH.'))
            return False
    if not current:
        print(msg('  Not installed. Run dev init (asks before downloading).',
                  '  Non installé. Lancez dev init (autorisation demandée avant téléchargement).'))
        return False
    if dry_run:
        print(msg('  DRY RUN: would inspect the Lead-only MCP registration, then add it if missing.',
                  '  SIMULATION : vérifierait l’intégration MCP du pilote et l’ajouterait si absente.'))
        return True
    ready=subprocess.run([current,'--help'],capture_output=True,text=True,timeout=20,check=False)
    if ready.returncode != 0:
        print(msg('  Installed Maestro failed its CLI smoke test.',
                  '  Le test de démarrage du CLI Maestro a échoué.'))
        return False
    result('Maestro CLI', 'OK', indent=4)
    if check_only:
        if command_exists('adb'):
            devices=subprocess.run(['adb','devices'],capture_output=True,text=True,timeout=20,check=False)
            count=sum(1 for x in devices.stdout.splitlines()[1:] if '\tdevice' in x)
            print(msg(f'  Android connected devices: {count}', f'  Appareils Android connectés : {count}'))
        else:
            print(msg('  adb not found; mobile E2E needs Android SDK/device (or macOS iOS simulator).',
                      '  adb absent ; les tests mobiles nécessitent un appareil/SDK Android (ou simulateur iOS).'))
        return True
    if not register_mcp:
        return True
    if agents is None:
        from setup_wizard import lead_mcp_agents
        agents = lead_mcp_agents(load_config().get('preferences', {}))
    if not agents:
        print(msg('  No role-scoped MCP registration needed; Maestro CLI remains available for testing.',
                  '  Aucun MCP de rôle à enregistrer ; Maestro CLI reste disponible pour les tests.'))
    for agent in agents:
        if agent != 'claude':
            print(msg('  Skipping Codex Maestro MCP: registration would be user-wide, not Lead-only.',
                      '  MCP Maestro Codex ignoré : la portée serait globale, pas limitée au pilote.'))
            continue
        if not command_exists(agent):
            print(msg(f'  {agent}: not installed, skipping MCP configuration',
                      f'  {agent} : absent, configuration MCP reportée'))
            continue
        try:
            found=subprocess.run([agent,'mcp','get','maestro'],cwd=ROOT,capture_output=True,text=True,timeout=25,check=False)
        except subprocess.TimeoutExpired:
            print(msg(f'  {agent}: MCP inspection timed out; skipping',
                      f'  {agent} : délai de vérification MCP dépassé, étape ignorée'))
            continue
        if found.returncode == 0:
            print(msg(f'  {agent}: Maestro MCP already configured; leaving existing entry unchanged',
                      f'  {agent} : Maestro MCP déjà configuré, aucune modification'))
            continue
        command = ['claude','mcp','add','--scope','local','maestro','--',current,'mcp']
        print(msg('  Registering Maestro MCP for Claude Engineering Lead (workspace-local)',
                  '  Enregistrement MCP Maestro pour le pilote Claude (projet local)'))
        if not yes and (not sys.stdin.isatty() or ask(msg('Approve this configuration change? (yes/no)',
                                                        'Autoriser cette modification ? (oui/non)'),
                                                    msg('no','non')).lower() not in ('y','yes','o','oui')):
            print(msg('  Skipped by developer', '  Ignoré par le développeur'))
            continue
        from setup_wizard import visible_run
        result=visible_run(command,label=f'Register {agent} Maestro MCP',timeout=40,cwd=ROOT)
        print(f'  {agent}: '+(msg('registered','enregistré') if result.returncode == 0 else msg('registration failed','échec de l’enregistrement')))
    # No device is started during setup; the Lead handles devices for real tests.
    return True


def ask(label: str, default: str) -> str:
    """Safe yes/no prompt with an explicit, readable default."""
    default = default.lower()
    if default not in ('yes', 'no', 'oui', 'non'):
        raise ValueError('Approval default must be yes or no')
    default_choice = 'yes' if default in ('yes', 'oui') else 'no'
    french = '(oui/non)' in label
    clean_label = label.replace(' (yes/no)', '').replace(' (oui/non)', '').strip()
    allowed = ('oui/non', 'Oui' if default_choice=='yes' else 'Non') if french else ('yes/no', 'Yes' if default_choice=='yes' else 'No')
    while True:
        suffix = f'[défaut : {allowed[1]}]' if french else f'[default: {allowed[1]}]'
        response = input(f'  {clean_label} ({allowed[0]}) {suffix}: ').strip().lower()
        if not response:
            return default_choice
        if response in ('yes', 'y', 'oui', 'o'):
            return 'yes'
        if response in ('no', 'n', 'non'):
            return 'no'
        print('  Répondez oui/non, ou appuyez sur Entrée pour la valeur par défaut.' if french else
              '  Please answer yes/no, or press Enter for the stated default.')


def detect_project() -> dict:
    folders = ['.', 'backend', 'server', 'api', 'services/backend', 'mobile', 'app', 'frontend', 'flutter']
    java = next((f for f in folders if (ROOT/f/'pom.xml').exists() or (ROOT/f/'build.gradle').exists()
                 or (ROOT/f/'build.gradle.kts').exists()), None)
    flutter = next((f for f in folders if (ROOT/f/'pubspec.yaml').exists()), None)
    return {'java': java, 'flutter': flutter}


def setup_project(non_interactive: bool = False, yes: bool = False, language: str | None = None):
    """Repeatable guided setup. --yes never authorizes downloads or system changes."""
    from setup_wizard import run_setup
    current = load_config() if CFG_FILE.exists() else {
      'models': {'codex_builder':'', 'codex_reviewer':'',
                 'claude_builder':'','claude_reviewer':''},
      'preferences': {'builder':'codex','reviewer':'claude', 'mobile_e2e':'maestro'},
      'policy_files':DEFAULT_POLICIES.copy(), 'checks':[]}
    with SESSIONS.lead_gate():
        SESSIONS.assert_idle_for_setup()
        return run_setup(ROOT,current, interactive=not non_interactive,
                         maestro_setup=maestro_setup, playwright_setup=playwright_tools.configure, write_json=write_json,
                         config_file=CFG_FILE, local=LOCAL, language=language)


def latest_task_id() -> str | None:
    if not RUNS.exists(): return None
    paths = [p for p in RUNS.iterdir() if p.is_dir() and (p/'state.json').exists()]
    return max(paths, key=lambda x: (x/'state.json').stat().st_mtime).name if paths else None


def new_task(goal: str, *, risk: str = 'normal', accept: list[str] | None = None) -> str:
    if not goal.strip(): fail('Provide a feature or bug description')
    slug = re.sub(r'[^a-z0-9]+', '-', goal.lower()).strip('-')[:36].rstrip('-') or 'task'
    digest = hashlib.sha1(goal.encode('utf-8')).hexdigest()[:5]
    task_id = f'{slug}-{int(time.time())}-{digest}'
    if (RUNS/task_id).exists():task_id += '-'+uuid.uuid4().hex[:6]
    # Generated tasks live in ignored run records, not in the source tree.
    task = {'id':task_id, 'created_at':time.time(), 'goal':goal, 'risk':risk,
            'acceptance_criteria':accept or [
                'Demonstrate the requested behavior against the configured running test environment.',
                'Verify relevant failures, access controls and persisted state when applicable.'
            ],
            'constraints':['No production environment, live funds, real customer data or irreversible operations.'],
            'scope':[], 'code_review_policy':38}
    write_json(run_dir(task_id)/'task.json', task)
    print(f'  Feature: {goal}\n  Task: {task_id}')
    return task_id


def print_status(task_id: str | None = None):
    task_id = task_id or latest_task_id()
    if not task_id:
        print('No runs yet. Start with dev.'); return
    rundir = run_dir(task_id)
    st = rundir/'state.json'
    if not st.exists(): print(f'No run recorded for {task_id}'); return
    data = load_json(st)
    print(f'\n  TASK    {task_id}\n  STATUS  {data.get("stage", "unknown").replace("_", " ").upper()}')
    print(f'  UPDATED {data.get("updated_at", "unknown")}')
    if data.get('questions'):
        print('  NEEDS YOUR INPUT:')
        for q in data['questions']: print('    -', q)
    if data.get('reason'): print('  REASON ', data['reason'])
    checks = rundir/'verify.json'
    if checks.exists():
        for check in load_json(checks).get('checks', []):
            print('  CHECK  ', check['name'], check['result'].upper())
    rev = rundir/'review.json'
    if rev.exists():
        report = load_json(rev)
        print('  REVIEW ', report.get('verdict','unknown').upper())
        for finding in report.get('findings', []):
            print('    -', finding.get('severity'), finding.get('issue'))
    print('  FILES  ', rundir.relative_to(ROOT))


def print_logs(task_id: str | None = None):
    task_id = task_id or latest_task_id()
    if not task_id: fail('No run yet')
    folder = run_dir(task_id)
    if not folder.exists(): fail('Unknown run')
    for path in sorted(folder.rglob('*.log')):
        print(f'  {path.relative_to(ROOT)} ({path.stat().st_size} bytes)')
    print('  Logs are local and may contain confidential data; inspect them in your editor.')


def manage_rules(operation: str, target: str | None = None, *, replace: bool = False):
    cfg = load_config()
    enabled = cfg.get('policy_files', DEFAULT_POLICIES.copy())
    if operation == 'list':
        print('POLICIES (enabled = consulted by both agents)')
        for path in sorted((STATE/'policies').rglob('*.md')):
            rel = path.relative_to(ROOT).as_posix()
            print('  [x]' if rel in enabled else '  [ ]', rel)
        return
    if not target: fail('Specify a policy file or name')
    if operation == 'add':
        source = Path(target).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() not in ('.md','.txt'):
            fail('Policy must be a readable Markdown or text file')
        dest = STATE / 'policies/custom' / (source.stem + '.md')
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not replace: fail(f'Already exists: {dest}; pass --replace to overwrite')
        dest.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
        rel = dest.relative_to(ROOT).as_posix()
        if rel not in enabled: enabled.append(rel)
        print('  Added and enabled:', rel)
    else:
        try:
            rel = next(p.relative_to(ROOT).as_posix() for p in (STATE/'policies').rglob('*.md')
                       if p.stem == target or p.relative_to(ROOT).as_posix() == target)
        except StopIteration: fail(f'No such policy: {target}')
        if operation == 'enable':
            if rel not in enabled: enabled.append(rel)
        elif operation == 'disable':
            if rel in enabled: enabled.remove(rel)
        else: fail('Unrecognized policy operation')
        print('  Policy', operation + 'd:', rel)
    cfg['policy_files'] = enabled
    write_json(CFG_FILE, cfg)


def ensure_local_context():
    """Deterministic project memory snapshot, no additional model call.

    Source docs/LESSONS.md remains human-editable and version controlled. Task logs
    and structured evidence-derived local learning stay private.
    """
    LOCAL.mkdir(parents=True, exist_ok=True)
    lessons = ROOT / 'docs' / 'LESSONS.md'
    import project_continuity as continuity
    lines = ['# Agent engineering context (generated; local)', '',
             'Authoritative guidance: AGENTS.md, CLAUDE.md, enabled policy files and docs/LESSONS.md.',
             'This is an index, not a substitute for inspecting the latest repository state.', '',
             '## Developer-approved project context', continuity.startup_context(ROOT), '']
    if lessons.exists():
        entries = lessons.read_text(encoding='utf-8')
        lines += ['## Developer-approved lessons', entries[-9000:], '']
    if RUNS.exists():
        folders = sorted([x for x in RUNS.iterdir() if (x/'state.json').exists()],
                         key=lambda x: (x/'state.json').stat().st_mtime, reverse=True)[:5]
        for folder in folders:
            lines += [f'## Recent task: {folder.name}', make_summary(folder)[:1800], '']
    try:
        import learning_engine
        learning_engine.collect(ROOT,limit=40)
        lines += [learning_engine.context(ROOT), '']
        import runtime_recovery
        pending = [x for x in runtime_recovery.incidents(ROOT) if x.get('status') != 'candidate_applied']
        if pending:
            lines += ['## Outstanding toolkit repair incidents', json.dumps([{'incident_id': x['incident_id'], 'operation': x['operation'], 'status': x['status']} for x in pending]), '']
    except (OSError,ValueError,TypeError,KeyError) as exc:
        lines += ['Local learning unavailable: '+type(exc).__name__, '']
    import token_intelligence as ti
    checkpoint=ti._checkpoint_read_path(ROOT)
    if checkpoint is not None and checkpoint.is_file():
        lines += ['## Durable Lead checkpoint', 'See '+str(checkpoint.relative_to(ROOT))+' for exact decisions, next steps and referenced task/plan IDs. Reconcile with current source and records.', '']
    result = LOCAL / 'PROJECT_CONTEXT.md'
    result.write_text('\n'.join(lines)[:18000] + '\n', encoding='utf-8')
    return result


def context_command(operation: str, message: str | None = None):
    if operation in ('refresh', 'show'):
        path = ensure_local_context() if operation == 'refresh' else LOCAL/'PROJECT_CONTEXT.md'
        if not path.exists(): path = ensure_local_context()
        if operation == 'refresh': print('Context refreshed:', path.relative_to(ROOT))
        else: print(path.read_text(encoding='utf-8'))
        return
    if operation == 'note':
        if not message or not message.strip(): fail('Tell the Engineering Lead the lesson to preserve')
        path = ROOT/'docs'/'LESSONS.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as file:
            file.write(f'\n- {stamp()[:10]} — {message.strip()}\n')
        ensure_local_context()
        print('Added developer-approved note to docs/LESSONS.md (review before committing).')
        return
    fail('Unsupported context command')


def show_sessions():
    prefs = load_config().get('preferences', {})
    print('GASTON AGENTIC')
    print('Workspace:', ROOT)
    print('')
    hooks_ok = native_hooks_installed(ROOT)
    print('Sessions')
    for role in ('builder','reviewer'):
        provider = prefs.get(role, 'codex' if role=='builder' else 'claude')
        if provider not in ('codex','claude'):
            continue
        entry = SESSIONS.get(provider,role)
        label = f'  {("Lead" if role=="reviewer" else "Builder"):<9} {provider.capitalize():<7}'
        if not entry:
            print(label+' Not created — open dev' if role=='reviewer' else label+' Not created — created on first delegation')
            continue
        locked = (SESSIONS.locks/f'{provider}-{role}').exists()
        active = session_active(provider,role)
        state = 'active' if active else 'busy' if locked else entry.get('status', 'ready')
        print(label + f' {state} | {entry["id"]}')
        print(' '*19 + ('Registered' if hooks_ok else 'Hooks not configured') + ' | '+
              f'Last used: {entry.get("last_used_at","unknown")}')
    print('')
    for conflict in SESSIONS.unmanaged_native():
        print(f"  WARNING: unmanaged {conflict['provider']} PID {conflict['pid']} in this workspace. Close or investigate; never kill automatically.")
    for provider in ('claude','codex'):
        if provider != prefs.get('reviewer') and SESSIONS.active(provider,'reviewer'):
            print(f'  WARNING: inactive-provider Lead {provider} still owns a session or its ownership is uncertain.')
    import development_db
    database=development_db.probe(ROOT)
    if database.get('status')=='ready':
        print(f"  Persistent PostgreSQL: {database['host']}:{database['port']} / {database['database']} (user {database['development_user']}); credentials in {database['credentials_file']}")
    elif database.get('status')!='not_configured':print('  Persistent PostgreSQL:',database.get('reason',database['status']))
    print('Native hooks:', 'configured (activation not verified)' if hooks_ok else 'not configured')
    if hooks_ok and 'codex' in (prefs.get('builder'),prefs.get('reviewer')):
        from native_hooks import codex_trust_status
        state=codex_trust_status(ROOT)
        print('  Codex trust:', ('recorded, runtime not verified' if state['status']=='trust_recorded'
                                else 'pending/unknown — revisit dev init'))
    for folder in ('queue','processed','blocked'):
        print(f'  Feedback {folder}:', len(list((HOOK_LOCAL/folder).glob('*.json'))))
    task_id = latest_task_id()
    print('')
    print('Recent results')
    if task_id:
        rd = run_dir(task_id)
        state_file = rd/'state.json'
        state = load_json(state_file) if state_file.exists() else {}
        print(f'  {task_id}: {state.get("stage", "unknown").replace("_", " ")}')
        summary_path = rd/'SUMMARY.md'
        if summary_path.is_file():
            for line in summary_path.read_text(encoding='utf-8').splitlines():
                if line.startswith(('**Implemented', '**Checks:', '**Failed/', '**Independent review:', '**Top findings:', '**Needs your decision:', '**Blocker:')):
                    print('  '+line.replace('**','')[:230])
        logs = sorted(rd.glob('*.log'))
        if logs:
            print('  Logs: '+', '.join(str(f.relative_to(ROOT)) for f in logs[-3:]))
    else:
        print('  No recorded automated runs')
    worker_log = HOOK_LOCAL/'worker.log'
    if worker_log.is_file():
        print('  Feedback log:',worker_log.relative_to(ROOT))

def sessions_command(operation: str, agent: str | None, session_id: str | None, yes: bool, role: str | None = None):
    if operation == 'show': show_sessions(); return
    if agent not in ('codex','claude'): fail('Choose codex or claude')
    if operation == 'unlock':
        print(SESSIONS.unlock_stale(agent,role)); return
    with SESSIONS.locked(agent,role):
        if operation == 'bind':
            if not session_id or not valid_session_id(session_id): fail('Provide an existing UUID session ID')
            SESSIONS.bind(agent,session_id,role=role)
            print(f'Bound {agent}/{role or "default"} session {session_id}')
        elif operation == 'rotate':
            if not yes: fail('Rotation requires --yes')
            ensure_local_context()
            old=SESSIONS.rotate(agent,role)
            print(f'Rotated {agent}/{role or "default"} (old ID: {old["id"] if old else "none"})')
        else: fail('Unsupported session operation')


def repository_fingerprint():
    """Hash code changes across tracked application repositories in a workspace."""
    return workspace_git.fingerprint(ROOT, load_config().get('project'))


def remember_verified_tree(task_id):
    digest=repository_fingerprint()
    if digest:
        write_json(LOCAL/'last-run-tree.json',{'digest':digest,'task_id':task_id,'saved_at':stamp()})


def matches_previous_tree():
    path=LOCAL/'last-run-tree.json'
    if not path.exists():return False
    try: known=load_json(path)
    except SystemExit:return False
    current=repository_fingerprint()
    return bool(current and known.get('digest')==current)


def issue_fingerprint(issue):
    # Excludes timestamps, paths and model-generated narrative recommendations.
    fields=['kind','title','observed','expected','description']
    cleaned='|'.join(' '.join(str(issue.get(k,'')).lower().split()) for k in fields)
    return hashlib.sha256(cleaned.encode()).hexdigest()


def issue_risk(issue):
    content=' '.join(str(issue.get(k,'')) for k in ('title','description','observed','expected')).lower()
    financial = ('payment','transfer','ledger','balance','debit','credit','withdraw',
                 'deposit','transaction','idempotenc','authorization','credential',
                 'kyc','settlement','payout','money','reconciliation','financial')
    if issue.get('severity') in ('critical','high') or any(x in content for x in financial):
        return 'high'
    return 'normal'


def perform_auto_fix(handoff, stub):
    """Route reviewer-approved bugs, policy updates, and verification setup to Codex.

    The reviewer remains read-only; policies are not assumed to be binding law.
    All paths use the existing checked builder/test/independent-review pipeline.
    """
    prefs=load_config().get('preferences',{})
    builder, reviewer = prefs.get('builder'), prefs.get('reviewer')
    if builder not in ('codex','claude') or reviewer not in ('codex','claude'):
        fail('Automatic repair requires registered builder and reviewer sessions.')
    issue=handoff['issue']
    kind=issue.get('kind','bug')
    if kind=='feature':
        goal='Implement requested feature: '+(issue.get('title') or 'new feature')[:200]
        criteria=[issue.get('expected') or 'Implement the developer-requested observable behavior.',
                  'Verify acceptance criteria and actual API/UI/database effects where applicable.',
                  'Test relevant failure and authorization cases; preserve existing behavior.',
                  'Report any missing verification evidence rather than claiming success.']
        risk=issue_risk(issue)
    elif kind=='policy':
        goal='Configure developer-provided rules: '+(issue.get('title') or 'policy update')[:180]
        criteria=[
          'Preserve the actual developer-supplied text, jurisdiction, source and applicability; never invent missing provisions.',
          'Store rules in versioned .agentic/policies/custom/ files and enable them in .agentic/config.json policy_files; do not silently overwrite existing rules.',
          'Separate developer policy from legal claims; flag unclear authority, conflicting requirements, and effective dates for developer decision.',
          'Update appropriate verification gates/tests only when evidence supports them; never claim a legal certification.',
          'Have Claude independently inspect the saved policy contents, enabled configuration and any resulting code or test changes.',
        ]
        risk='normal'  # Adding rules is not permission to alter production payments or compliance obligations.
    elif kind=='verification':
        goal='Configure project verification: '+(issue.get('title') or 'tests')[:180]
        criteria=[
          'Inspect the real project, implement appropriate executable PostgreSQL/Testcontainers or other integration/E2E tests as applicable.',
          'Register only existing commands that actually run the tests; never invent test results or skip missing gates.',
          'Ensure failure cases and financial/business invariants are checked; preserve existing settings and tests.',
        ]
        risk=issue_risk(issue)
    else:
        goal='Fix reviewer-reported bug: '+(issue.get('title') or 'untitled bug')[:200]
        criteria=[issue.get('expected') or 'Resolve the observed bug without changing intended behavior.',
                  'Reproduce the reported failure using the provided evidence where possible.',
                  'Add a regression test and run the configured project verification gates.',
                  'Preserve existing financial, privacy and security invariants.']
        risk=issue_risk(issue)
    criteria.extend(['Developer request: '+str(issue.get('description','')),
                     'Relevant source/evidence: '+('; '.join(issue.get('evidence',[])))])
    task_id=new_task(goal,risk=risk,accept=criteria)
    taskfile=run_dir(task_id)/'task.json'
    task=load_json(taskfile)
    task['kind']=kind
    task['feedback_source']=(stub.with_suffix('.json')).relative_to(ROOT).as_posix()
    if kind=='bug':
        task['acceptance_criteria'].extend([
            'Observed: '+str(issue.get('observed','')),
            'Reproduction: '+('; '.join(issue.get('reproduction',[]))),
        ])
    write_json(taskfile,task)
    handoff['fix_task_id']=task_id
    handoff['status']='fixing'
    write_json(stub.with_suffix('.json'),handoff)
    print(f'  {builder} implementation → tests → independent {reviewer} session review',flush=True)
    cmd=[sys.executable,str(runtime_script('pipeline_worker.py')),
         task_id,builder,reviewer]
    result=subprocess.run(cmd,cwd=ROOT,check=False)
    state_file=run_dir(task_id)/'state.json'
    state=load_json(state_file) if state_file.exists() else {}
    stage=state.get('stage','blocked')
    handoff['fix_outcome']=stage
    handoff['status']='verified' if stage=='ready_for_human_release_review' else 'blocked'
    handoff['completed_at']=stamp()
    write_json(stub.with_suffix('.json'),handoff)
    print(f'  Feedback loop finished: {stage}. Report: '+str(run_dir(task_id)/'SUMMARY.md'),flush=True)
    return handoff


def claude_session_display_name(role: str, session_id: str, project: Path | None = None) -> str:
    """A unique human-readable label for a *new* native Claude Code session.

    Naming is cosmetic: Gaston always resumes by UUID, never by a possibly
    duplicated display name. Existing sessions keep their name and history.
    """
    project = ROOT if project is None else project
    label = re.sub(r'[^A-Za-z0-9._-]+', '-', project.name).strip('-._') or 'project'
    return f'Gaston {"Lead" if role == "reviewer" else "Builder"} - {label} - {session_id[:8]}'


def run_interactive_provider(cmd, **kwargs):
    """Track the native child separately from its wrapper for crash recovery."""
    from platform_compat import prepare_subprocess
    cmd,child_env,_=prepare_subprocess(cmd,kwargs.pop('env',None))
    if child_env is not None:kwargs['env']=child_env
    with subprocess.Popen(cmd, **kwargs) as child:
        SESSIONS.record_child(child.pid)
        return child.wait()


def chat_command(agent: str, handoff_after: bool=True, visual: bool=False, role: str | None = None):
    cfg=load_config(); prefs=cfg.get('preferences',{})
    if not command_exists(agent): fail(agent+' CLI unavailable; install/authenticate first')
    if role is None:
        role='builder' if prefs.get('builder')==agent else 'reviewer'
    if role not in ('builder','reviewer') or prefs.get(role)!=agent:
        fail(f'{agent}/{role} is not an enrolled role in this project')
    ensure_local_context()
    # Refresh version-pinned project hooks before any provider process starts,
    # including the first Codex bootstrap session after an engine update.
    if central_mode():
        from native_hooks import refresh_central_provider_hooks
        refresh_central_provider_hooks(ROOT)
    if role=='reviewer' and ti._checkpoint_read_path(ROOT) is not None and ti._checkpoint_read_path(ROOT).is_file():
        with SESSIONS.locked(agent,role):
            if ti.maybe_rotate_lead(ROOT,SESSIONS,agent):print('Archived oversized Lead conversation at a safe checkpoint; continuing from saved project memory.',flush=True)
    existing=SESSIONS.get(agent,role)
    if role=='reviewer':
        import project_continuity as continuity
        roadmap=continuity.status(ROOT)
        if roadmap['status']=='ready':
            title=roadmap.get('next_milestone_title')
            label=('; next: '+title) if title else ''
            print('Approved project context: '+(', '.join(roadmap.get('paths',[])) or '(no external plan selected)')+label,flush=True)
        else:
            print('Project context not registered; inspect existing developer-owned documents. Automatic Lead rotation requires approved sources.',flush=True)
    model=cfg.get('models',{}).get(agent+'_'+role,'')
    if agent=='codex' and not existing:
        # Bootstrap outside the interactive role lock: codex_structured locks itself.
        path=LOCAL/(f'bootstrap-{agent}-{role}.json')
        prompt=f'Read AGENTS.md. You are the persistent {role} for this project. Do not change files. Return status=received, summary=ready, questions=[], next_steps=[].'
        codex_structured(prompt,ACK_SCHEMA,path,model,'read-only',role='review' if role=='reviewer' else 'plan')
    with SESSIONS.locked(agent,role):
        existing=SESSIONS.get(agent,role)
        if agent=='codex':
            cmd=['codex',*(codex_lead_permission_args(interactive=True) if role=='reviewer' else codex_builder_permission_args('workspace-write',interactive=True)),
                 *codex_effort_args('codex',role),
                 *(codex_lead_mcp_args() if role=='reviewer' else []), *serena_tools.codex_args(ROOT),
                 *(['--model',model] if model else []),'resume',existing['id']]
        else:
            existing=recover_claude_session(role)
            if existing: cmd=['claude','--resume',existing['id']]
            else:
                sid=str(uuid.uuid4()); SESSIONS.bind('claude',sid,'initializing',role=role)
                # Interactive Claude Code sessions are visible in the CLI
                # resume picker. Name only new sessions; do not rename or
                # rotate an existing Lead just to improve discoverability.
                cmd=['claude','--session-id',sid,
                     '--name',claude_session_display_name(role,sid)]
                existing=SESSIONS.get('claude',role)
            if model: cmd += ['--model',claude_model_id(model)]
            cmd += claude_effort_args('claude',role)
            cmd += claude_advisor_args(role, cfg)
            if role=='builder':
                cmd += [*claude_builder_permission_args(), *claude_builder_mcp_args(), *claude_prompt_refresh_args(),
                        '--append-system-prompt',builder_runtime_brief()]
            if role=='reviewer':
                # Source remains read-only, but verification can perform
                # predefined browser tests and start an existing emulator.
                cmd += [*claude_lead_permission_args(),
                        *claude_prompt_refresh_args(),
                        '--append-system-prompt',lead_runtime_brief(),
                        '--mcp-config',str(lead_verification_mcp_config(include_maestro=visual))]
        if visual and role!='reviewer':
            fail('Device verification is managed by the Engineering Lead, not the Builder.')
        if role=='reviewer':
            budget=ti.lead_context(ROOT,agent,existing['id'])
            if budget.get('status') in ('review','rotate_at_safe_boundary'):
                print('Lead context: '+str(budget['context_tokens'])+' tokens. At the next checkpoint preserve outstanding decisions/task IDs and compact the native conversation; do not restart active work.',flush=True)
        print(f'Opening {agent}/{"lead" if role=="reviewer" else "builder"}: {existing["id"]}',flush=True)
        returncode=run_interactive_provider(cmd,cwd=ROOT, env=claude_advisor_env(role,cfg) if agent=='claude' else (codex_builder_env() if role=='builder' else None))
        if returncode:
            SESSIONS.mark(agent,'attention',role=role)
            fail(f'{agent}/{role} chat ended with exit code {returncode}')
        SESSIONS.mark(agent,'ready',role=role)
    if native_hooks_installed(ROOT):
        if list((HOOK_LOCAL/'queue').glob('*.json')):
            # A CLI wrapper may have held the role lock when the native
            # SessionEnd hook fired. Retry once the wrapper has released it.
            from native_hooks import schedule_worker
            schedule_worker()
        if role=='reviewer' and handoff_after and list((HOOK_LOCAL/'queue').glob('*.json')):
            print('Lead request queued. View progress with dev sessions.')
    elif role=='reviewer' and handoff_after:
        print('Native hooks are unavailable. Run dev init to repair setup.')


def screenshot_file(source: str):
    """Copy a user-supplied image to an ignored local directory; verify magic and size."""
    path=Path(source).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > 15*1024*1024 or path.stat().st_size < 8:
        fail('Screenshot must be a readable image smaller than 15 MB')
    with path.open('rb') as src:
        head=src.read(16)
    kind = ('png' if head.startswith(b'\x89PNG\r\n\x1a\n') else
            'jpg' if head[:3]==b'\xff\xd8\xff' else
            'webp' if head[:4]==b'RIFF' and head[8:12]==b'WEBP' else None)
    if not kind: fail('Screenshots must be PNG, JPEG or WebP images')
    folder=LOCAL/'feedback-images'
    folder.mkdir(parents=True,exist_ok=True)
    dest=folder/(str(uuid.uuid4())+'.'+kind)
    shutil.copyfile(path,dest)
    return dest


def handoff_from_reviewer(description: str='',image: str | None=None, *, auto_fix=True):
    cfg=load_config()
    reviewer=cfg.get('preferences',{}).get('reviewer','claude')
    if reviewer not in ('codex','claude') or not SESSIONS.get(reviewer,'reviewer'):
        fail('No registered Engineering Lead session yet. Run dev.')
    attach = screenshot_file(image) if image else None
    context = f' Screenshot file: {attach.relative_to(ROOT)}. Read and inspect it.' if attach else ''
    prompt = (
        'You are continuing YOUR existing persistent, READ-ONLY review session. '
        'Extract ONLY the latest NEW explicit developer request: a feature implementation, a bug fix, a policy/rules update, or verification/test setup. '
        'If this was a discussion-only question or no configuration/fix was requested, return status=none. '
        'Never fabricate a request or expected behavior. Return kind=feature, bug, policy, or verification. '
        'For rules, preserve the user-supplied wording/source; if source, jurisdiction, intended applicability, or content is essential and missing, return needs_input. '
        'If actionable, produce a detailed self-contained structured handoff suitable for Codex, including exact provided rules or references to existing shared workspace files. '
        'Do not edit files, run commands, authorize payments or claim tests you did not execute. '
        'Keep screenshots as evidence, not as proof of backend state. '
        'Developer feedback: '+description + context +
        ' Project context: .agentic/local/PROJECT_CONTEXT.md')
    HANDOFFS.mkdir(parents=True,exist_ok=True)
    stub=HANDOFFS/(stamp().replace(':','').replace('+','-')+'-'+uuid.uuid4().hex[:8])
    report=(claude_structured(prompt,FEEDBACK_SCHEMA,stub.with_suffix('.reviewer.json'),
              cfg.get('models',{}).get('claude_reviewer',''),role='feedback') if reviewer=='claude'
            else codex_structured(prompt,FEEDBACK_SCHEMA,stub.with_suffix('.reviewer.json'),
              cfg.get('models',{}).get('codex_reviewer',''),'read-only',role='feedback'))
    if report['status'] == 'none':
        print('No new actionable feedback. No handoff created.'); return None
    data={'id':stub.name,'status':'needs_input' if report['status']=='needs_input' else 'open',
          'created_at':stamp(),'source':reviewer,'description':description,
          'screenshot':str(attach.relative_to(ROOT)) if attach else None,
          'issue':report}
    write_json(stub.with_suffix('.json'),data)
    if report['status']=='needs_input':
        print('Claude requires clarification; no Codex handoff sent:')
        for question in report.get('questions',[]): print('  -',question)
        return data
    fingerprint=issue_fingerprint(report)
    data['fingerprint']=fingerprint
    for old in sorted(HANDOFFS.glob('*.json')):
        if old.name.endswith(('.claude.json','.codex.json','.reviewer.json')) or old==stub.with_suffix('.json'):
            continue
        try: previous=load_json(old)
        except SystemExit:continue
        if previous.get('fingerprint')==fingerprint:
            print(f'  Identical issue already recorded ({previous["id"]}, {previous.get("status")}). No duplicate fix triggered.')
            stub.with_suffix('.json').unlink(missing_ok=True)
            return previous
    if not command_exists(cfg.get('preferences',{}).get('builder','codex')):
        print('Builder unavailable; feedback safely queued:',data['id'])
        return data
    if not auto_fix:
        print('Feedback saved without automatic fix:',data['id'])
        return data
    return perform_auto_fix(data,stub)


# Keep the old import name for existing scripts; reviewer provider is selected by config.
handoff_from_claude = handoff_from_reviewer


def handoff_command(operation: str, issue_id: str|None=None):
    if operation in ('claude','reviewer'):
        ensure_local_context()
        return handoff_from_claude('Please extract the latest actionable feedback from our manual discussion.')
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    if operation=='list':
        for f in sorted(HANDOFFS.glob('*.json')):
            if f.name.endswith(('.claude.json','.codex.json','.reviewer.json')): continue
            data=load_json(f)
            print(f"{data['id']} — {data['status']} — {data['issue']['title']}")
        return
    if operation=='show':
        if not issue_id or not re.fullmatch(r'[A-Za-z0-9T_+\-]+',issue_id): fail('Provide a valid issue ID')
        path=HANDOFFS/(issue_id+'.json')
        if not path.exists(): fail('Handoff not found')
        print(json.dumps(load_json(path),ensure_ascii=False,indent=2))
        return
    fail('Unknown handoff command')


def review_completed_builder_work(description: str):
    """Check completed native builder work using real tests and the reviewer session.

    This path never converts absent verification evidence into a pass.
    """
    cfg = refresh_workspace_checks(load_config(), persist=True)
    prefs = cfg.get('preferences', {})
    reviewer = prefs.get('reviewer', 'claude')
    if reviewer not in ('codex', 'claude') or not SESSIONS.get(reviewer, 'reviewer'):
        raise RuntimeError('Engineering Lead session not registered. Run dev first.')
    task_id = new_task('Review builder changes: ' + description.splitlines()[0][:100],
                       accept=['Compare implementation against the builder report and current source code.',
                               'Inspect changed code, tests and applicable business/security rules.',
                               'Report gaps, findings and actual verification evidence.'])
    rd = run_dir(task_id)
    task = load_json(rd / 'task.json')
    task['kind'] = 'native_builder_review'
    text = description.lower()
    scopes = []
    if any(term in text for term in ('flutter', 'mobile', 'android', 'ios', 'screen', 'widget')):
        scopes.append('mobile')
    if any(term in text for term in ('postgres', 'database', 'migration', 'sql', 'ledger')):
        scopes.append('database')
    if any(term in text for term in ('api', 'endpoint', 'rest', 'controller', 'webhook')):
        scopes.append('api')
    if any(term in text for term in ('authz', 'authorization', 'security', 'permission')):
        scopes.append('security')
    task['scope'] = scopes
    task['acceptance_criteria'].append('Builder-reported work: ' + description[:10000])
    write_json(rd / 'task.json', task)
    write_json(rd / 'build.json', {'status':'done', 'summary':description[:3500],
                                  'questions':[], 'changed_files':[], 'remaining_risks':[]})
    save_state(rd,'verifying', builder=prefs.get('builder','codex'))
    try:
        evidence = verify(task,rd,cfg)
    except (SystemExit, RuntimeError, OSError) as exc:
        evidence = {'all_passed':False, 'checks':[{'name':'required_verification',
                     'result':'blocked', 'reason':str(exc)}]}
        write_json(rd/'verify.json', evidence)
    save_state(rd,'reviewing', reviewer=reviewer)
    assessment = review(task,rd,cfg,reviewer,evidence)
    passed = evidence.get('all_passed') and assessment.get('verdict')=='pass'
    stage = ('ready_for_human_release_review' if passed
             else 'verification_incomplete' if assessment.get('verdict')=='pass'
             else 'changes_requested')
    save_state(rd, stage, reviewer=reviewer, findings=assessment.get('findings',[]),
               coverage_gaps=assessment.get('coverage_gaps',[]))
    print(f'  Builder review: {stage}. See dev sessions.')
    return {'status':stage,'task_id':task_id}


def hooks_command(operation: str, *, yes=False, agent: str | None = None):
    """Install/inspect registered-only native hooks or safely process pending issues."""
    from native_hooks import active_file
    if operation == 'install':
        if not yes:
            fail('Hook installation modifies local Codex/Claude settings. Review and pass --yes.')
        install_native_hooks(ROOT, approve=True)
        print('Installed project-local hooks. Trust them in the CLIs (/hooks) as prompted.')
        print('Only registered Builder/Lead sessions can trigger automation.')
        return
    if operation == 'status':
        print('Native hooks:', 'installed' if native_hooks_installed(ROOT) else 'MISSING')
        for name in ('codex','claude'):
            for role in ('builder','reviewer'):
                entry=SESSIONS.get(name,role)
                if entry:
                    print(f'  {name}/{role}:',entry['id'], '| native active' if session_active(name,role) else '| idle')
        for folder in ('queue','processed','blocked'):
            print(f'  {folder}:',len(list((HOOK_LOCAL/folder).glob('*.json'))))
        print('  Worker log:',HOOK_LOCAL/'worker.log')
        return
    if operation == 'release':
        if agent not in ('codex','claude') or not yes:
            fail('Release requires exact provider name and --yes after verifying chat has ended.')
        role=load_config().get('preferences',{}).get('reviewer')==agent and 'reviewer' or 'builder'
        lease=active_file(agent,role)
        if not lease.exists():
            print('No native lease present'); return
        lease.unlink()
        print(f'Cleared {agent} native active lease (manual confirmation).')
        return
    if operation != 'process':fail('Unknown native hook action')
    from runtime_recovery import feedback_worker_guard, recover_feedback_items
    with feedback_worker_guard(ROOT) as acquired:
        if not acquired:
            print('Another feedback worker is active or ownership is uncertain; leaving queue untouched.')
            return
        # Do not interfere with native reviewer or builder chat sessions.
        selected=SESSIONS.selected()
        if (session_active('claude') or session_active('codex') or
                any((SESSIONS.locks/f'{provider}-{role}').exists() for provider, role in selected)):
            print('Managed session is active; issues remain queued until it closes.')
            return
        recover_feedback_items(ROOT)
        pending=sorted((HOOK_LOCAL/'queue').glob('*.json'))
        if not pending:
            print('No pending native feedback.'); return
        for item in pending:
            # Per-reviewer queue only; never bind/accept a foreign session.
            try:
                job=load_json(item)
                provider=job.get('provider','claude')
                role=job.get('role','reviewer')
                cfg=load_config().get('preferences', {})
                registered=(SESSIONS.get(provider,role) if provider in ('codex','claude')
                            and role in ('builder','reviewer') else None)
                if not registered or job.get('session_id')!=registered['id'] or job.get('project')!=str(ROOT.resolve()) or cfg.get(role)!=provider:
                    raise RuntimeError('Job does not belong to an enrolled role/session')
                desc=str(job.get('feedback','')).strip()
                if not desc: raise RuntimeError('Empty feedback payload')
                # Delete queue item before calling Claude: the review session's
                # own SessionEnd hook must not schedule duplicate model work.
                processing=item.with_suffix('.processing')
                os.replace(item,processing)
                try:
                    ensure_local_context()
                    if role == 'builder':
                        result=review_completed_builder_work(desc)
                        job['outcome']=result.get('status','none')
                        job['task_id']=result.get('task_id')
                    else:
                        result=handoff_from_claude(
                            'Native reviewer confirmed actionable feedback:\n'+desc,
                            auto_fix=True)
                        job['outcome']=result.get('status','none') if result else 'none'
                        job['handoff_id']=result.get('id') if result else None
                    job['handled_at']=stamp()
                    write_json(HOOK_LOCAL/'processed'/item.name,job)
                    print('Native feedback processed:',job['outcome'], job.get('handoff_id') or job.get('task_id') or '')
                except (Exception, SystemExit) as exc:
                    # Fail closed: never blindly retry a partially executed fix.
                    job['error']=str(exc)
                    job['handled_at']=stamp()
                    write_json(HOOK_LOCAL/'blocked'/item.name,job)
                    print('Native feedback needs attention:',str(exc),file=sys.stderr)
                finally:
                    processing.unlink(missing_ok=True)
            except (Exception,SystemExit) as exc:
                print('Invalid native feedback item:',str(exc),file=sys.stderr)
                job={'error':str(exc),'source':item.name,'handled_at':stamp()}
                write_json(HOOK_LOCAL/'blocked'/item.name,job)
                item.unlink(missing_ok=True)


def ui_session(task_id: str | None, agent: str = 'auto'):
    if not command_exists('maestro'):
        fail('Maestro CLI not installed. Run dev init')
    reviewer=load_config().get('preferences',{}).get('reviewer','claude')
    if agent=='auto': agent=reviewer
    if not agent: fail('No reviewer configured')
    print('Opening the SAME persistent agent chat. Attach or refer to a sandbox screenshot there.')
    print('Maestro MCP must be registered. A running emulator is required for on-screen taps.')
    chat_command(agent, visual=True,role='reviewer')

def run_automated_task(task_id: str, *, builder: str, reviewer: str) -> None:
    """Internal Lead->Builder->Lead pipeline; never a user-facing dev command.

    Called by pipeline_worker.py for a queued and registered task. The user
    interacts with the Engineering Lead; tools and checks execute under roles.
    """
    from types import SimpleNamespace
    global ACTIVE_RUN
    require_setup_complete()
    args = SimpleNamespace(command='run', task_id=task_id, builder=builder,
                           reviewer=reviewer, approve_risk=False, max_repairs=1)
    cfg = refresh_workspace_checks(load_config(), persist=True); task = load_task(args.task_id)
    rundir = run_dir(args.task_id)
    ACTIVE_RUN = rundir
    rundir.mkdir(parents=True, exist_ok=True)
    if not (rundir/'task.json').exists():
        write_json(rundir/'task.json',task)
    prefs = cfg.get('preferences',{})
    if args.command in ('plan','run'):
        args.builder = args.builder or prefs.get('builder','auto')
    if args.command in ('run','review'):
        args.reviewer = args.reviewer or prefs.get('reviewer','auto')
    if args.command in ('plan', 'run'):
        builder = find_builder(args.builder)
        if not builder: fail('No requested builder CLI found. Run dev init.')
        if args.command == 'run':
            existing = workspace_git.existing_changes(ROOT, cfg.get('project'))
            write_json(rundir / 'preexisting-changes.json', {'files': existing})
            if existing:
                print('  Preserving existing uncommitted changes; proceeding without Git cleanup or commit.')
        ensure_local_context()
        save_state(rundir, 'planning', builder=builder)
        lead = find_reviewer(prefs.get('reviewer', 'auto'), builder)
        if not lead:
            fail('Engineering Lead unavailable; cannot choose independent verification coverage')
        plan = provider_structured(lead, planning_prompt(task), PLAN_SCHEMA,
                                   rundir / 'plan.json', cfg, 'lead_plan')
        if plan.get('questions') or plan.get('status') != 'ready':
            save_state(rundir, 'needs_input', questions=plan.get('questions', []))
            print('Clarify these questions in the task specification:')
            for q in plan.get('questions', []): print('  -', q)
            return
        task['verification_areas'] = plan.get('verification_areas', [])
        task['verification_rationale'] = plan.get('verification_rationale', [])
        task['required_checks'] = sorted(set(task.get('required_checks', [])) | set(plan.get('verification_checks', [])))
        write_json(rundir / 'task.json', task)
        if args.command == 'plan':
            save_state(rundir, 'plan_ready', steps=plan.get('steps', [])); return
        if (task.get('risk', 'normal') != 'normal' or plan.get('risk_flags')) and not args.approve_risk:
            print('  Risk flags:', plan.get('risk_flags', []), '| Task risk:', task.get('risk','normal'))
            if sys.stdin.isatty() and ask('Approve local implementation (not production deployment)? (yes/no)', 'no').lower() in ('y','yes'):
                print('  Local implementation approved; production changes remain forbidden.')
            else:
                save_state(rundir, 'risk_approval_required', risk_flags=plan.get('risk_flags', []))
                print('  Risk approval required. Discuss the scope with the Engineering Lead using dev.')
                return
        save_state(rundir, 'implementing', builder=builder)
        built = provider_structured(builder, build_prompt(task, plan), BUILD_SCHEMA,
                                    rundir / 'build.json', cfg, 'build')
        if built.get('questions') or built.get('status') != 'done':
            save_state(rundir, 'needs_input', questions=built.get('questions', [])); return
        save_state(rundir, 'verifying')
        cfg = refresh_workspace_checks(load_config(), persist=True)
        tested = verify(task, rundir, cfg)
        if not tested['all_passed'] and args.max_repairs == 1:
            failures = [x for x in tested['checks'] if x['result'] != 'pass']
            snippets = []
            snippets.append('Verification plan and missing gates: ' + json.dumps(tested.get('selection', {})))
            for item in failures:
                path = rundir / str(item.get('evidence', ''))
                snippets.append(path.read_text(encoding='utf-8')[-3500:] if path.is_file() else str(item))
            save_state(rundir, 'repairing', failed=[x['name'] for x in failures])
            fixed = provider_structured(builder, build_prompt(task, plan, repair='\n'.join(snippets)),
                                       BUILD_SCHEMA, rundir / 'repair.json', cfg, 'build')
            if fixed.get('status') == 'done' and not fixed.get('questions'):
                save_state(rundir, 'verifying_after_repair')
                cfg = refresh_workspace_checks(load_config(), persist=True)
                tested = verify(task, rundir, cfg)
        if not tested['all_passed']:
            save_state(rundir, 'checks_failed', results=tested); return
        reviewer = find_reviewer(args.reviewer, builder)
        if not reviewer:
            save_state(rundir, 'checks_passed_review_missing', results=tested)
            print('No reviewer available; not declaring this release-ready.')
            return
        save_state(rundir, 'reviewing', reviewer=reviewer)
        assessment = review(task, rundir, cfg, reviewer, tested)
        stage = 'ready_for_human_release_review' if assessment.get('verdict') == 'pass' else 'changes_requested'
        save_state(rundir, stage, reviewer=reviewer, findings=assessment.get('findings', []),
                   coverage_gaps=assessment.get('coverage_gaps', []))
        return
    if args.command == 'verify':
        tested = verify(task, rundir, cfg)
        save_state(rundir, 'checks_passed' if tested['all_passed'] else 'checks_failed', results=tested)
        return
    if args.command == 'review':
        reviewer = find_reviewer(args.reviewer, 'codex')
        if not reviewer: fail('No reviewer CLI found')
        evidence = load_json(rundir / 'verify.json')
        assessment = review(task, rundir, cfg, reviewer, evidence)
        save_state(rundir, 'ready_for_human_release_review' if assessment.get('verdict') == 'pass' else 'changes_requested',
                   reviewer=reviewer, findings=assessment.get('findings', []))



def main(argv: list[str] | None = None) -> None:
    """Preserve the five established commands; `dev update` is dispatched by the launcher."""
    parser=argparse.ArgumentParser(
        description='Gaston Agentic — dev opens the Engineering Lead; tools run through the agents.',
        epilog='Commands: dev, dev init, dev sessions, dev models, dev uninstall, dev update')
    sub=parser.add_subparsers(dest='command')
    init=sub.add_parser('init',help='Configure this workspace and its tools')
    init.add_argument('--non-interactive',action='store_true')
    init.add_argument('--yes',action='store_true')
    init.add_argument('--lang',choices=['en','fr'],default=None)
    sub.add_parser('sessions',help='View enrolled sessions and recent activity')
    sub.add_parser('models',help='Interactively choose model, effort and Claude Advisor')
    sub.add_parser('uninstall',help='Remove Gaston Agentic from this workspace')
    args=parser.parse_args(argv)
    if args.command=='init':
        ready=setup_project(args.non_interactive,args.yes,args.lang)
        if ready is False: raise SystemExit(2)
        return
    if args.command=='uninstall':
        from uninstall import uninstall
        uninstall(ROOT)
        return
    if args.command=='models':
        if not CFG_FILE.is_file():
            fail('Setup has not started. Run dev init first.')
        models_command('show')
        return
    require_setup_complete()
    if args.command=='sessions':
        sessions_command('show',None,None,False,None)
        return
    cfg=load_config()
    lead=cfg.get('preferences',{}).get('reviewer')
    if lead not in ('codex','claude'):
        fail('Engineering Lead is not configured. Run dev init.')
    chat_command(lead,role='reviewer')

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError) as exc:
        if ACTIVE_RUN is not None:
            save_state(ACTIVE_RUN,'blocked', reason=str(exc))
        print('ERROR:',exc,file=sys.stderr)
        raise SystemExit(2)
    except SystemExit as exc:
        if ACTIVE_RUN is not None and exc.code not in (0, None):
            if not isinstance(exc.code,int):
                save_state(ACTIVE_RUN, 'blocked', reason=str(exc.code))
        raise
    finally:
        if ACTIVE_RUN is not None and (ACTIVE_RUN/'task.json').exists():
            try:
                write_summary(ACTIVE_RUN)
                # Only establish a baseline after code has undergone checks and review.
                state_path=ACTIVE_RUN/'state.json'
                if state_path.exists() and load_json(state_path).get('stage') in (
                    'ready_for_human_release_review','changes_requested','checks_failed',
                    'checks_passed_review_missing'):
                    remember_verified_tree(ACTIVE_RUN.name)
                ensure_local_context()
                print('\n'+'='*55+'\nFINAL DEVELOPMENT SUMMARY\n'+'='*55,flush=True)
                print(make_summary(ACTIVE_RUN),flush=True)
                print('Detailed evidence:',(ACTIVE_RUN/'SUMMARY.md').relative_to(ROOT),flush=True)
            except Exception as exc:
                print(f'Could not produce final summary: {exc}',file=sys.stderr)
