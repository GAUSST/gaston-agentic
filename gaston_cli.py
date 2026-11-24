"""Installed platform-neutral ``dev`` command. Operates on a Gaston workspace.

A console script creates the native Windows dev.exe automatically; no .cmd
script or shell emulation is required. The toolkit lives in its own checkout.
"""
from __future__ import annotations
import os
from pathlib import Path
import sys


def workspace(start=None):
    supplied = os.environ.get('GASTON_ROOT')
    if supplied:
        path = Path(supplied).expanduser().resolve()
        if not (path/'.agentic/launcher.py').is_file():
            raise ValueError('GASTON_ROOT is not a Gaston workspace: '+str(path))
        return path
    here = Path(start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory/'.agentic/launcher.py').is_file():
            return directory
    raise ValueError('No Gaston workspace found here or in parent directories; open a Gaston project directory.')


def _central_launch(root: Path) -> int:
    """Opt-in preview: execute an installed engine against an existing workspace.

    No migration, updater or uninstall of workspace-owned toolkit code here.
    Refuse a project-supplied package named gaston_engine.
    """
    from importlib import metadata, util
    import json

    adapter = root / '.gaston/project.json'
    if adapter.exists() or adapter.is_symlink():
        from gaston_engine.project_layout import read_project_marker
        read_project_marker(root)
    else:
        manifest_path = root / '.agentic/manifest.json'
        if manifest_path.is_symlink():
            raise RuntimeError('Refusing a linked workspace manifest')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        version = metadata.version('gaston-agentic-cli')
        if manifest.get('build') != version:
            raise RuntimeError('Central preview requires installed engine and workspace toolkit to have the same version')
    # Until hooks, MCP helpers and project state move to central ownership,
    # central preview is read-only. Never execute incomplete runtime flows.
    if sys.argv[1:] != ['--help']:
        raise RuntimeError('Central preview supports dev --help only; use GASTON_ENGINE_MODE=legacy for all other commands')

    spec = util.find_spec('gaston_engine')
    if spec is None or spec.origin is None:
        raise RuntimeError('Installed gaston_engine package not found')
    engine_dir = Path(spec.origin).resolve().parent
    if engine_dir.is_relative_to(root) or not (engine_dir / 'launcher.py').is_file() or not (engine_dir / 'eng.py').is_file():
        raise RuntimeError('Refusing to execute project-local or incomplete Gaston engine code')

    os.environ['GASTON_ENGINE_PROJECT'] = str(root)
    sys.path.insert(0, str(engine_dir))
    from gaston_engine.launcher import main as launch
    return launch()


def _central_init() -> int:
    """Opt-in: write only .gaston/project.json into an existing project."""
    from importlib import metadata, util
    target = Path(os.environ.get('GASTON_ROOT') or Path.cwd()).expanduser().resolve()
    spec = util.find_spec('gaston_engine')
    if spec is None or spec.origin is None or Path(spec.origin).resolve().is_relative_to(target):
        raise RuntimeError('A trusted, externally installed Gaston engine is required')
    from gaston_engine.project_layout import initialize_project
    root, created = initialize_project(target, engine_version=metadata.version('gaston-agentic-cli'))
    if created:
        print('Gaston central project adapter created: ' + str(root / '.gaston/project.json'))
    else:
        print('Gaston central project adapter already exists: ' + str(root / '.gaston/project.json'))
    print('Central engine setup is a preview. Hooks, sessions and migrations are not activated yet.')
    return 0


def main():
    try:
        mode = os.environ.get('GASTON_ENGINE_MODE', 'legacy').strip().lower()
        if mode not in ('legacy', 'central'):
            raise ValueError('GASTON_ENGINE_MODE must be legacy or central')
        if mode == 'central':
            if sys.argv[1:] == ['init']:
                return _central_init()
            from gaston_engine.project_layout import find_project
            start = Path(os.environ.get('GASTON_ROOT') or Path.cwd()).expanduser().resolve()
            root, _ = find_project(start)
            return _central_launch(root)
        root = workspace()
        sys.path.insert(0, str(root/'.agentic'))
        from launcher import main as launch
        return launch()
    except (ValueError, RuntimeError, FileNotFoundError, OSError) as exc:
        print('Gaston: '+str(exc),file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
