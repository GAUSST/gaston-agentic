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

    manifest_path = root / '.agentic/manifest.json'
    if manifest_path.is_symlink():
        raise RuntimeError('Refusing a linked workspace manifest')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    version = metadata.version('gaston-agentic-cli')
    if manifest.get('build') != version:
        raise RuntimeError('Central preview requires installed engine and workspace toolkit to have the same version')
    if len(sys.argv) > 1 and sys.argv[1] in ('update', 'uninstall'):
        raise RuntimeError('Central preview cannot update or uninstall a legacy workspace; use GASTON_ENGINE_MODE=legacy')

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


def main():
    try:
        root = workspace()
        mode = os.environ.get('GASTON_ENGINE_MODE', 'legacy').strip().lower()
        if mode == 'central':
            return _central_launch(root)
        if mode != 'legacy':
            raise ValueError('GASTON_ENGINE_MODE must be legacy or central')
        sys.path.insert(0, str(root/'.agentic'))
        from launcher import main as launch
        return launch()
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print('Gaston: '+str(exc),file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
