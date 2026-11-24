"""Explicit engine/project path boundary, without automatic migrations."""
from __future__ import annotations

import os
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
