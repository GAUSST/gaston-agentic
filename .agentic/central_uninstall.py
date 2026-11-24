"""Project-owned Gaston adapter removal for centrally installed runtime.

Uninstall never touches application files, legacy .agentic state, the global
Python distribution, user Git metadata or external provider transcripts.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil

from project_layout import read_project_marker


def _state(root: Path) -> Path:
    root = Path(root).resolve()
    read_project_marker(root)
    data = root / '.gaston'
    if data.is_symlink() or not data.is_dir():
        raise RuntimeError('Refusing a linked or invalid Gaston project directory')
    return data


def _assert_no_active_workers(data: Path) -> None:
    local = data / 'local'
    active = local / 'native-hooks' / 'active'
    locks = local / 'locks'
    worker = local / 'native-hooks' / 'feedback-worker.lock'
    for path in (active, locks):
        if path.is_symlink():
            raise RuntimeError('Refusing symbolic link in Gaston activity state')
        if path.is_dir() and any(path.iterdir()):
            raise RuntimeError('Managed Gaston session or worker is active; close it before uninstalling')
    if worker.exists() or worker.is_symlink():
        raise RuntimeError('Managed Gaston feedback worker may be active; close it before uninstalling')


def _assert_no_links(data: Path) -> None:
    """Avoid following or removing linked user data during complete erase."""
    for current, dirs, files in os.walk(data, followlinks=False):
        folder = Path(current)
        for item in dirs + files:
            if (folder / item).is_symlink():
                raise RuntimeError('Full erase refused: symbolic links exist inside .gaston')


def uninstall_project(root: Path, *, input_fn=input, output=print) -> bool:
    """Require explicit selection and confirmation; preserve context by default."""
    data = _state(root)
    _assert_no_active_workers(data)
    marker = data / 'project.json'
    output('GASTON CENTRAL — PROJECT UNINSTALL')
    output('  Keep (default): remove project adapter; retain ALL project context.')
    output('  Erase: delete local .gaston data after separate explicit approval.')
    output('  Application files, legacy .agentic, external provider transcripts')
    output('  and the globally installed engine/CLI will NOT be removed.')
    selection = input_fn('Mode [keep/erase] (default: keep; other answers cancel): ').strip().lower()
    if selection not in ('', 'keep', 'erase'):
        output('Cancelled. No files changed.')
        return False
    erase = selection == 'erase'
    if erase:
        _assert_no_links(data)
        output('  ERASE LOCAL DATA: ' + str(data))
        if input_fn('Type ERASE GASTON DATA: ').strip() != 'ERASE GASTON DATA':
            output('Cancelled. No files changed.')
            return False
    else:
        output('  KEEP PROJECT CONTEXT: ' + str(data))
    if input_fn('Type UNINSTALL to confirm: ').strip() != 'UNINSTALL':
        output('Cancelled. No files changed.')
        return False

    # Refuse unsafe ownership changes while the user answers prompts.
    _state(root)
    _assert_no_active_workers(data)
    if erase:
        _assert_no_links(data)
        shutil.rmtree(data)
        output('Local Gaston project adapter and context erased.')
    else:
        if marker.is_symlink():
            raise RuntimeError('Refusing linked Gaston project adapter')
        marker.unlink()
        try:
            data.rmdir()
        except OSError:
            pass
        output('Gaston project adapter removed. Project context preserved.')
    return True
