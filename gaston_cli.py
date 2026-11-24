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
    if not (adapter.exists() or adapter.is_symlink()):
        if sys.argv[1:] != ['--help']:
            raise RuntimeError('Explicit legacy workspace migration is required for central execution')
        journal = root / '.agentic/local/update/journal.json'
        if journal.is_symlink() or journal.exists():
            raise RuntimeError('Interrupted update detected; use legacy mode to recover before central preview')

    spec = util.find_spec('gaston_engine')
    if spec is None or spec.origin is None:
        raise RuntimeError('Installed gaston_engine package not found')
    engine_dir = Path(spec.origin).resolve().parent
    if engine_dir.is_relative_to(root) or not (engine_dir / 'launcher.py').is_file() or not (engine_dir / 'eng.py').is_file():
        raise RuntimeError('Refusing to execute project-local or incomplete Gaston engine code')

    os.environ['GASTON_ENGINE_PROJECT'] = str(root)
    # Auto layout dispatch must propagate central mode to the installed launcher,
    # otherwise it attempts to execute a missing project-local .agentic/eng.py.
    os.environ['GASTON_ENGINE_MODE'] = 'central'
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
    if sys.stdin.isatty() or '--setup' in sys.argv[1:]:
        os.environ['GASTON_ENGINE_PROJECT'] = str(root)
        os.environ['GASTON_ENGINE_MODE'] = 'central'
        from importlib import util
        spec = util.find_spec('gaston_engine')
        if spec is None or spec.origin is None:
            raise RuntimeError('Installed engine unavailable for provider setup')
        directory = str(Path(spec.origin).resolve().parent)
        if directory not in sys.path:
            sys.path.insert(0, directory)
        from gaston_engine.eng import main as start_engine
        start_engine(['init', '--non-interactive'] if not sys.stdin.isatty() else ['init'])
    else:
        print('Project adapter initialized. Run dev init --setup to configure providers.')
    return 0


def _central_uninstall(root: Path) -> int:
    """Remove only the project's central adapter and optional Gaston data."""
    from importlib import util
    spec = util.find_spec('gaston_engine')
    if spec is None or spec.origin is None or Path(spec.origin).resolve().is_relative_to(root):
        raise RuntimeError('A trusted externally installed Gaston engine is required')
    from gaston_engine.central_uninstall import uninstall_project
    uninstall_project(root)
    return 0


def _central_module(name: str, root: Path | None = None):
    """Import trusted central runtime code, never a shadow project package."""
    from importlib import util
    spec = util.find_spec('gaston_engine')
    if spec is None or spec.origin is None:
        raise RuntimeError('Globally installed Gaston runtime is missing')
    engine_dir = Path(spec.origin).resolve().parent
    if root is not None and engine_dir.is_relative_to(root):
        raise RuntimeError('Refusing project-supplied Gaston runtime code')
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    import importlib
    return importlib.import_module('gaston_engine.' + name)


def _global_update(args: list[str], root: Path | None = None) -> int:
    module = _central_module('central_update', root)
    return module.main(args)


def _central_migrate(root: Path) -> int:
    module = _central_module('central_migration', root)
    return module.main(root)


def _managed_bootstrap() -> int | None:
    """Dispatch through a verified, immutable global engine release when present.

    This stable entry point is installed once via pip/pipx. Later signed updates
    activate a separate version folder without rewriting a running package or
    touching a project. A corrupt pointer fails closed instead of falling back
    to unverified source.
    """
    if os.environ.get('GASTON_ACTIVE_VERSION'):
        return None
    import hashlib
    import importlib.util
    import json
    import re
    folder = Path(os.environ.get('GASTON_GLOBAL_HOME',
                                 str(Path.home()/'.local/share/gaston'))).expanduser()
    if not folder.is_absolute():
        raise RuntimeError('Invalid global Gaston runtime directory')
    pointer = folder/'current.json'
    if pointer.is_symlink():
        raise RuntimeError('Refusing linked engine-version pointer')
    if not pointer.exists():
        return None
    if pointer.stat().st_size > 4096:
        raise RuntimeError('Oversized engine-version pointer')
    try:
        state = json.loads(pointer.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Corrupt global Gaston engine pointer') from exc
    tag = state.get('tag') if isinstance(state, dict) else None
    if not isinstance(tag, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise RuntimeError('Invalid global Gaston release pointer')
    base = folder.resolve()
    version = base/'versions'/tag
    engine = version/'gaston_engine'
    cli = version/'gaston_cli.py'
    if any(p.is_symlink() for p in (version, engine, cli, engine/'manifest.json')):
        raise RuntimeError('Linked engine release paths are forbidden')
    if not version.is_dir() or not cli.is_file():
        raise RuntimeError('Active Gaston release is missing')
    raw = (engine/'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != state.get('manifest_sha256'):
        raise RuntimeError('Active Gaston release manifest checksum mismatch')
    manifest = json.loads(raw)
    if manifest.get('release') != tag or manifest.get('build') != tag.removeprefix('v'):
        raise RuntimeError('Active Gaston release version mismatch')
    for rel, expected in manifest.get('files', {}).items():
        if rel == 'gaston_cli.py':
            file = cli
        elif isinstance(rel, str) and rel.startswith('.agentic/') and rel.endswith(('.py', '.md')):
            file = engine/rel[len('.agentic/'):]
        else:
            continue
        if file.is_symlink() or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Active Gaston engine integrity failure: ' + str(rel))
    # Resolve only from verified, user-local versioned source, never from
    # application files. Existing CLI commands are delegated unchanged.
    sys.path.insert(0, str(version))
    os.environ['GASTON_ACTIVE_VERSION'] = tag
    spec = importlib.util.spec_from_file_location('_gaston_active_cli', cli)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load verified Gaston CLI')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main()


def main():
    try:
        managed = _managed_bootstrap()
        if managed is not None:
            return managed
        mode = os.environ.get('GASTON_ENGINE_MODE', 'auto').strip().lower()
        if mode not in ('auto', 'legacy', 'central'):
            raise ValueError('GASTON_ENGINE_MODE must be auto, legacy or central')

        # The source-free project adapter is authoritative only for the central
        # installation. Legacy installations preserve their normal dispatch.
        if mode in ('central', 'auto'):
            from gaston_engine.project_layout import find_project
            start = Path(os.environ.get('GASTON_ROOT') or Path.cwd()).expanduser().resolve()
            arguments = sys.argv[1:]
            # A central update is global and may be invoked outside any
            # project. Existing legacy workspaces retain their older updater.
            if arguments and arguments[0] == 'update':
                try:
                    update_root, update_kind = find_project(start)
                except ValueError:
                    update_root, update_kind = start, 'new'
                if update_kind != 'legacy':
                    return _global_update(arguments[1:], update_root)
                if mode == 'central':
                    raise RuntimeError('Explicit legacy workspace migration is required for central execution')
            if mode == 'central':
                if arguments in (['init'], ['init', '--setup']):
                    return _central_init()
                root, kind = find_project(start)
                if kind == 'legacy' and arguments == ['migrate']:
                    return _central_migrate(root)
                if kind == 'central' and arguments == ['uninstall']:
                    return _central_uninstall(root)
                return _central_launch(root)

            root, kind = find_project(start, allow_new=arguments in (['init'], ['init', '--setup']))
            if kind == 'new' and arguments in (['init'], ['init', '--setup']):
                return _central_init()
            if kind == 'central':
                if arguments in (['init'], ['init', '--setup']):
                    return _central_init()
                if arguments == ['uninstall']:
                    return _central_uninstall(root)
                return _central_launch(root)
            if kind == 'legacy' and arguments == ['migrate']:
                return _central_migrate(root)

            # A legacy project stays on its established runtime and is never
            # silently initialized or migrated into the new project layout.
            root = workspace(start)
        else:
            root = workspace()
        sys.path.insert(0, str(root/'.agentic'))
        from launcher import main as launch
        return launch()
    except (ValueError, RuntimeError, FileNotFoundError, OSError) as exc:
        print('Gaston: '+str(exc),file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
