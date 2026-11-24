"""Attempt-bound Lead completion handoff; no model calls or process control."""
from __future__ import annotations

import os
from pathlib import Path
import re
import time

from project_layout import project_state
from task_runtime import read_record
from session_store import process_identity, process_state
from native_hooks import atomic_json

RECORD = 'lead-completion-wait.json'
SAFE_ID = re.compile(r'[a-z0-9-]{8,120}')


def _record(folder):
    if folder.is_symlink() or not folder.is_dir() or (folder / RECORD).is_symlink():
        return {}
    return read_record(folder / RECORD)


def begin(folder, attempt, lease):
    """A new correction supersedes the previous completion obligation."""
    if not lease or not isinstance(attempt, str) or not re.fullmatch(r'[a-f0-9]{32}', attempt):
        return
    if folder.is_symlink() or (folder / RECORD).is_symlink():
        return
    atomic_json(folder / RECORD, {'attempt': attempt, 'lease': lease,
                'phase': 'pending', 'created_at': time.time(), 'nudged': False})


def _current(folder, lease):
    row = _record(folder)
    if not lease or row.get('lease') != lease or not row.get('attempt'):
        return {}
    if row['attempt'] != read_record(folder / 'worker-launch.json').get('attempt'):
        return {}
    return row


def attach(folder, lease):
    row = _current(folder, lease)
    if not row:
        return None
    row.update(phase='waiting', waiter={'pid': os.getpid(), **process_identity(os.getpid())})
    atomic_json(folder / RECORD, row)
    return row['attempt']


def finish(folder, lease, attempt, outcome, *, cancelled=False):
    row = _current(folder, lease)
    if not row or row.get('attempt') != attempt:
        return
    # Cancellation does not settle an attempt; an actual reported blocker
    # is actionable, rather than a reason to force an infinite wait.
    if cancelled or outcome.get('status') in ('cancelled', 'pending', 'lead_work_available'):
        phase = 'pending'
    elif outcome.get('builder_finished') is True or outcome.get('status') == 'blocked':
        phase = 'settled'
    else:
        phase = 'pending'
    row.update(phase=phase, result=outcome.get('status'), finished_at=time.time())
    row.pop('waiter', None)
    atomic_json(folder / RECORD, row)


def pending(root, lease):
    """Only outstanding attempts launched by this enrolled Lead lease."""
    if not lease:
        return []
    runs = project_state(root) / 'runs'
    if runs.is_symlink():
        return []
    found = []
    for folder in runs.iterdir() if runs.is_dir() else ():
        if not SAFE_ID.fullmatch(folder.name):
            continue
        row = _current(folder, lease)
        if not row or row.get('phase') == 'settled':
            continue
        waiter = row.get('waiter')
        if row.get('phase') == 'waiting' and isinstance(waiter, dict) and process_state(waiter) == 'live':
            continue
        state = read_record(folder / 'state.json')
        if state.get('lead_verified') or state.get('stage') in ('slice_reviewed', 'ready_for_human_release_review'):
            continue
        found.append((folder, row))
    return found


def stop_reason(root, lease):
    """One native follow-up per attempt; never create an infinite Stop loop."""
    for folder, row in pending(root, lease):
        if row.get('nudged'):
            continue
        row['nudged'] = True
        atomic_json(folder / RECORD, row)
        return (f'Gaston: task {folder.name} has an uncollected Builder outcome. '
                'Complete any ready independent Lead work, then call '
                f'mcp__gaston_verify__wait_for_builder(task_id="{folder.name}") directly. '
                'Do not substitute Bash, sleep, process polling, a background shell or a progress-only final response. '
                'If the tool is unavailable or fails, report its exact error; do not pretend to be waiting.')
    return None


def shell_reason(root, lease, event):
    """Block identifiable worker polling; preserve ordinary native Bash use."""
    if event.get('tool_name') not in ('Bash', 'shell_command', 'exec_command'):
        return None
    info = event.get('tool_input')
    if not isinstance(info, dict):
        return None
    command = info.get('command')
    if not isinstance(command, str):
        return None
    if not re.search(r'\b(?:sleep|wait|tail|watch|ps|pgrep|while|until)\b|kill\s+-0', command):
        return None
    for folder, row in pending(root, lease):
        pid = read_record(folder / 'worker.json').get('pid')
        if folder.name in command or (type(pid) is int and re.search(r'\b' + str(pid) + r'\b', command)):
            return (f'Gaston: use mcp__gaston_verify__wait_for_builder with task_id={folder.name}. '
                    'Native shell worker polling is not a completion subscription.')
    return None
