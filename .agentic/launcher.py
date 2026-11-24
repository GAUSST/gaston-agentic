"""One native dispatcher for Linux, macOS and Windows installed CLIs.

The installed ``dev`` console command is identical on all three platforms.
A repository-local ``./dev`` shell shim remains available on Unix.
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from project_layout import project_root
ROOT = project_root()
MINIMUM_PYTHON = (3, 10)


def main(argv: list[str] | None = None, *, system: str | None = None) -> int:
    """Dispatch to the same launch controller on every supported operating system.

    No implicit project creation, migrations, session deletion, provider
    registration, or environment changes. Only the selected subsystem runs.
    """
    if sys.version_info < MINIMUM_PYTHON:
        print('Gaston requires Python 3.10 or newer.', file=sys.stderr)
        return 2
    arguments = list(sys.argv[1:] if argv is None else argv)
    target = platform.system() if system is None else system
    if target in ('Linux', 'Darwin', 'Windows'):
        from host_setup import refresh_tool_path
        import updater
        # Central --help preview is read-only. recover() creates
        # .agentic/local/update even if there is nothing to recover.
        # An interrupted update instead blocks the preview until the
        # ordinary legacy launcher can safely restore its transaction.
        preview = os.environ.get('GASTON_ENGINE_MODE') == 'central'
        if preview:
            journal = ROOT / '.agentic/local/update/journal.json'
            if journal.is_symlink() or journal.exists():
                raise RuntimeError('Interrupted update detected; use legacy mode to recover before central preview')
        else:
            updater.recover(ROOT)
        if arguments and arguments[0] == 'update':
            return updater.main(ROOT, arguments[1:])
        from evolution import launch
        if not arguments:
            updater.notice(ROOT)
        if target == 'Windows' and not preview:
            from platform_compat import protect_workspace_local
            protect_workspace_local(ROOT)
        if not preview:
            refresh_tool_path()
        launch(ROOT, arguments)
        return 0
    print(f'Gaston does not support {target}. Use Ubuntu, macOS or Windows.',
          file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
