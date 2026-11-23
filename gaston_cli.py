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


def main():
    try:
        root = workspace()
        sys.path.insert(0, str(root/'.agentic'))
        from launcher import main as launch
        return launch()
    except (ValueError, RuntimeError) as exc:
        print('Gaston: '+str(exc),file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
