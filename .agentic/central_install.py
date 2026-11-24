"""Explicit per-user installation; no application directory is modified.

Uses an isolated environment and the existing native console-script entry point.
The source checkout is only a build input, never a runtime dependency.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import venv

from cli_bootstrap import MARKER, ensure_path_profile
from central_update import home

ROOT = Path(__file__).resolve().parent.parent
OWNER = 'gaston-agentic-bootstrap-v1'


def source_snapshot(source: Path, destination: Path) -> str:
    manifest_path = source / '.agentic/manifest.json'
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    for rel, digest in manifest['files'].items():
        parts = PurePosixPath(rel)
        if parts.is_absolute() or '..' in parts.parts or '\\' in rel or ':' in rel:
            raise RuntimeError('Unsafe manifest path: ' + rel)
        path = source / rel
        if path.resolve() != path or not path.is_file():
            raise RuntimeError('Missing or linked release file: ' + rel)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('Release checksum mismatch: ' + rel)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    copied_manifest = destination / '.agentic/manifest.json'
    copied_manifest.parent.mkdir(parents=True, exist_ok=True)
    copied_manifest.write_bytes(raw)
    return manifest['build']


def native_bin(environment: Path) -> Path:
    return environment / ('Scripts' if os.name == 'nt' else 'bin')


def register_path(environment: Path) -> None:
    """Register the stable entry point without replacing unrelated commands."""
    scripts = native_bin(environment)
    if os.name == 'nt':
        import winreg
        existing = shutil.which('dev')
        if existing and Path(existing).resolve() != (scripts / 'dev.exe').resolve():
            raise RuntimeError('Another dev command is on PATH: ' + existing)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, 'Environment', 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            try:
                old, kind = winreg.QueryValueEx(key, 'Path')
            except FileNotFoundError:
                old, kind = '', winreg.REG_EXPAND_SZ
            if str(scripts).casefold() not in [x.casefold() for x in old.split(';')]:
                winreg.SetValueEx(key, 'Path', 0, kind, str(scripts) + (';' + old if old else ''))
        # Notify future shells that the user environment has changed.
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xffff, 0x001A, 0, 'Environment', 2, 1000, None)
        return
    folder = Path.home() / '.local/bin'
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / 'dev'
    target = scripts / 'dev'
    if destination.is_symlink():
        if destination.resolve() == target.resolve():
            if str(folder) not in os.environ.get('PATH', '').split(os.pathsep):
                ensure_path_profile(Path.home(), os.environ.get('SHELL', ''))
            return
        raise RuntimeError('Refusing to replace unrelated dev symlink: ' + str(destination))
    if destination.exists():
        if not destination.is_file() or MARKER not in destination.read_text(encoding='utf-8', errors='replace'):
            raise RuntimeError('Refusing to replace unrelated dev command: ' + str(destination))
    # A symlink to the native console script survives source checkout deletion.
    with tempfile.TemporaryDirectory(prefix='.gaston-link-', dir=folder) as temp:
        link = Path(temp) / 'dev'
        link.symlink_to(target)
        os.replace(link, destination)
    if str(folder) not in os.environ.get('PATH', '').split(os.pathsep):
        ensure_path_profile(Path.home(), os.environ.get('SHELL', ''))


def install(source: Path = ROOT, wheel: Path | None = None) -> Path:
    source = source.resolve()
    base = home()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = base / 'bootstrap'
    owner = environment / '.gaston-owner'
    if environment.is_symlink() or owner.is_symlink():
        raise RuntimeError('Refusing linked bootstrap environment')
    if environment.exists():
        if not owner.is_file() or owner.read_text().strip() != OWNER:
            raise RuntimeError('Existing bootstrap directory is not owned by Gaston')
        if not (native_bin(environment) / ('dev.exe' if os.name == 'nt' else 'dev')).is_file():
            raise RuntimeError('Incomplete installation; remove only the owned bootstrap directory and retry')
        register_path(environment)
        return environment
    # Use an exclusive directory creation as the installation lock. Venvs cannot
    # be relocated after creation because console-script shebangs are absolute.
    environment.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix='gaston-install-') as temp:
            snapshot = Path(temp) / 'source'
            version = source_snapshot(source, snapshot)
            venv.EnvBuilder(with_pip=True).create(environment)
            python = native_bin(environment) / ('python.exe' if os.name == 'nt' else 'python')
            env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','GASTON_ROOT','GASTON_ENGINE_PROJECT','GASTON_ACTIVE_VERSION','GASTON_ENGINE_MODE')}
            package = str(wheel.resolve()) if wheel else str(snapshot)
            subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--disable-pip-version-check', package], check=True, env=env, cwd=temp)
            subprocess.run([str(python), '-I', '-c', 'from importlib.metadata import version; import gaston_engine; assert version("gaston-agentic-cli") == '+repr(version)], check=True, env=env, cwd=temp)
            owner.write_text(OWNER + '\n', encoding='utf-8')
    except BaseException:
        shutil.rmtree(environment)
        raise
    register_path(environment)
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, help='Locally built wheel (CI/offline installation)')
    args = parser.parse_args()
    try:
        result = install(wheel=args.wheel)
        print('Gaston installed centrally:', result)
        print('Open a new terminal, enter your application directory and run dev init.')
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print('Gaston installation failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
