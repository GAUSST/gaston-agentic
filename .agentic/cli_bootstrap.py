"""Self-register the `dev` command without pip, pipx, sudo, or system Python edits.

Invoked by `./dev` on Linux/macOS. Only installs a clearly marked per-user
launcher; it never overwrites unrelated executables or files. The repository
is located dynamically from the current directory, not hardwired at install.
"""
from __future__ import annotations
import os
from pathlib import Path
import shlex
import sys
import tempfile

MARKER = '# Gaston Agentic managed dev launcher v1'
SHELL_SHIM = r"""#!/bin/sh
# Gaston Agentic managed dev launcher v1
set -eu
root="${GASTON_ROOT:-$PWD}"
while :; do
    if [ -f "$root/.agentic/launcher.py" ]; then
        if command -v python3 >/dev/null 2>&1; then
            exec python3 "$root/.agentic/launcher.py" "$@"
        fi
        printf '%s\n' 'Gaston requires Python 3.10+.' >&2
        exit 2
    fi
    [ "$root" = / ] && break
    root=$(dirname "$root")
done
printf '%s\n' 'No Gaston workspace found in this directory or its parents.' >&2
exit 2
"""
PATH_MARKER = '# Gaston dev CLI (user-local executable path)'
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH" ' + PATH_MARKER


def ensure_path_profile(home: Path, shell: str) -> bool:
    """Append one harmless, idempotent user PATH line for *future* terminals."""
    if Path(shell).name == 'zsh':
        rc = home / '.zshrc'
    elif Path(shell).name in ('bash', 'sh'):
        rc = home / '.bashrc'
    else:
        return False
    if rc.is_symlink():
        return False
    try:
        existing = rc.read_text(encoding='utf-8') if rc.is_file() else ''
        if PATH_MARKER in existing:
            return False
        with rc.open('a', encoding='utf-8') as fh:
            if existing and not existing.endswith('\n'):
                fh.write('\n')
            fh.write('\n' + PATH_LINE + '\n')
        return True
    except OSError:
        return False


def register(*, home: Path | None = None, environ=None) -> str:
    env = os.environ if environ is None else environ
    home = home or Path.home()
    dest = home / '.local' / 'bin' / 'dev'
    if dest.is_symlink():
        return 'skipped: existing dev is a symlink'
    if dest.exists():
        if not dest.is_file():
            return 'skipped: dev is not a regular file'
        try:
            current = dest.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            return 'skipped: existing dev is not managed by Gaston'
        if MARKER not in current:
            return 'skipped: existing dev is not managed by Gaston'
        if current == SHELL_SHIM:
            return 'already installed'
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.gaston-dev-', dir=dest.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(SHELL_SHIM)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as exc:
        return f'skipped: {exc}'
    if str(dest.parent) not in env.get('PATH', '').split(os.pathsep):
        changed = ensure_path_profile(home, env.get('SHELL', ''))
        if changed:
            return 'installed: open a new terminal for the dev command; ./dev works now'
        return 'installed: add ~/.local/bin to PATH for future terminals; ./dev works now'
    return 'installed'


def main() -> int:
    if sys.platform == 'win32':
        return 0  # Windows has no POSIX ./dev entrypoint.
    outcome = register()
    if outcome.startswith('skipped') or outcome.startswith('installed'):
        print('Gaston dev CLI: ' + outcome, file=sys.stderr)
    return 0  # Do not block Gaston if the optional global alias could not be installed.

if __name__ == '__main__':
    raise SystemExit(main())
