"""Explicit engine/project path boundary, without automatic migrations."""
from __future__ import annotations

import os
import json
import re
from pathlib import Path

CENTRAL_MARKER = Path('.gaston/project.json')
LEGACY_MARKER = Path('.agentic/launcher.py')


def project_root(fallback: Path | None = None) -> Path:
    """Workspace root may differ from the installed engine's source directory."""
    supplied = os.environ.get('GASTON_ENGINE_PROJECT')
    if supplied:
        path = Path(supplied)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError('GASTON_ENGINE_PROJECT must be an existing absolute directory')
        return path.resolve()
    return Path(fallback or Path(__file__).resolve().parent.parent).resolve()


def engine_file(name: str) -> Path:
    """Locate trusted engine code in the installed runtime, never in application code."""
    if not name.isidentifier() or name.startswith('_'):
        raise ValueError('Expected a public engine module name')
    return Path(__file__).resolve().parent / (name + '.py')


def find_project(start: Path, *, allow_new: bool = False) -> tuple[Path, str]:
    """Find a central marker or a legacy checkout; never initialize implicitly."""
    here = Path(start).expanduser().resolve()
    for root in (here, *here.parents):
        marker = root / CENTRAL_MARKER
        if marker.parent.is_symlink() or marker.is_symlink():
            raise ValueError('Refusing linked Gaston project marker')
        if marker.is_file():
            return root, 'central'
        if (root / LEGACY_MARKER).is_file():
            return root, 'legacy'
    if allow_new:
        return here, 'new'
    raise ValueError('No Gaston project found. Run dev init in the project directory.')

PROJECT_MARKER_KIND = 'gaston-project'
ENGINE_NAME = 'gaston-agentic-cli'
ENGINE_VERSION = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')


def read_project_marker(root: Path) -> dict:
    """Validate the tiny, user-owned project adapter without writing state."""
    root = Path(root).resolve()
    directory = root / '.gaston'
    marker = directory / 'project.json'
    if directory.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValueError('Missing or linked Gaston project marker')
    if marker.stat().st_size > 4096:
        raise ValueError('Gaston project marker is unexpectedly large')
    try:
        payload = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Invalid Gaston project marker') from exc
    if not isinstance(payload, dict) or payload.get('schema') != 1 or (
            payload.get('kind') != PROJECT_MARKER_KIND or
            payload.get('engine') != ENGINE_NAME or
            not isinstance(payload.get('created_with'), str) or
            not ENGINE_VERSION.fullmatch(payload['created_with'])):
        raise ValueError('Unsupported Gaston project marker')
    return payload


def initialize_project(start: Path, *, engine_version: str) -> tuple[Path, bool]:
    """Explicit, idempotent adapter creation; never copy tools into application.

    Does not activate sessions, register hooks, or move legacy .agentic state.
    Neither existing application paths nor user-managed context are overwritten.
    """
    if not isinstance(engine_version, str) or not ENGINE_VERSION.fullmatch(engine_version):
        raise ValueError('Expected installed Gaston engine version')
    requested = Path(start).expanduser().resolve()
    if not requested.is_dir():
        raise ValueError('Run dev init inside an existing project directory')
    root, kind = find_project(requested, allow_new=True)
    if kind == 'legacy':
        raise ValueError('Central preview supports dev --help only for legacy workspaces; migration is not available yet')
    if kind == 'central':
        read_project_marker(root)
        return root, False

    directory = root / '.gaston'
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError('Refusing linked or non-directory .gaston')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('Refusing linked .gaston')
    marker = directory / 'project.json'
    if marker.is_symlink() or marker.exists():
        raise ValueError('Refusing to overwrite existing Gaston project marker')
    payload = {
        'schema': 1,
        'kind': PROJECT_MARKER_KIND,
        'engine': ENGINE_NAME,
        'created_with': engine_version,
    }
    # Exclusive creation; interruption cannot silently replace user content.
    with marker.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return root, True
)


def read_project_marker(root: Path) -> dict:
    """Validate the tiny, user-owned project adapter without writing state."""
    root = Path(root).resolve()
    directory = root / '.gaston'
    marker = directory / 'project.json'
    if directory.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValueError('Missing or linked Gaston project marker')
    if marker.stat().st_size > 4096:
        raise ValueError('Gaston project marker is unexpectedly large')
    try:
        payload = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Invalid Gaston project marker') from exc
    if not isinstance(payload, dict) or payload.get('schema') != 1 or (
            payload.get('kind') != PROJECT_MARKER_KIND or
            payload.get('engine') != ENGINE_NAME or
            not isinstance(payload.get('created_with'), str) or
            not ENGINE_VERSION.fullmatch(payload['created_with'])):
        raise ValueError('Unsupported Gaston project marker')
    return payload


def initialize_project(start: Path, *, engine_version: str) -> tuple[Path, bool]:
    """Explicit, idempotent adapter creation; never copy tools into application.

    Does not activate sessions, register hooks, or move legacy .agentic state.
    Neither existing application paths nor user-managed context are overwritten.
    """
    if not isinstance(engine_version, str) or not ENGINE_VERSION.fullmatch(engine_version):
        raise ValueError('Expected installed Gaston engine version')
    requested = Path(start).expanduser().resolve()
    if not requested.is_dir():
        raise ValueError('Run dev init inside an existing project directory')
    root, kind = find_project(requested, allow_new=True)
    if kind == 'legacy':
        raise ValueError('Central preview supports dev --help only for legacy workspaces; migration is not available yet')
    if kind == 'central':
        read_project_marker(root)
        return root, False

    directory = root / '.gaston'
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError('Refusing linked or non-directory .gaston')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('Refusing linked .gaston')
    marker = directory / 'project.json'
    if marker.is_symlink() or marker.exists():
        raise ValueError('Refusing to overwrite existing Gaston project marker')
    payload = {
        'schema': 1,
        'kind': PROJECT_MARKER_KIND,
        'engine': ENGINE_NAME,
        'created_with': engine_version,
    }
    # Exclusive creation; interruption cannot silently replace user content.
    with marker.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return root, True
