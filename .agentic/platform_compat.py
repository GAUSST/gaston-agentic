"""Native host primitives shared by Gaston on Windows, macOS and Linux.

Do not treat an uninspectable process as dead. Lock files are never used for
arbitrary workspace paths; callers must validate their scope and ownership.
"""
from __future__ import annotations
from project_layout import project_state, project_state_name, project_root
from contextlib import contextmanager
import os
from pathlib import Path, PureWindowsPath
import re
import signal
import subprocess
import time

WINDOWS = os.name == 'nt'


def windows_to_msys_path(value: str) -> str:
    """Convert an absolute Windows path to the POSIX form accepted by Git Bash."""
    raw = str(value)
    if raw.startswith('\\\\?\\') or raw.startswith('\\\\.\\'):
        raw = raw[4:]
    path = PureWindowsPath(raw)
    if len(path.drive) == 2 and path.drive[1] == ':' and path.root:
        tail = '/'.join(path.parts[1:])
        return '/' + path.drive[0].lower() + '/' + tail
    if path.drive.startswith('\\\\'):
        tail = '/'.join(path.parts[1:])
        share = path.drive.lstrip('\\').replace('\\', '/')
        return '//' + share + (('/' + tail) if tail else '')
    raise ValueError('Claude Bash hooks require an absolute drive or UNC Windows path: ' + raw)


def msys_to_windows_path(value: str) -> str:
    """Convert a Git-Bash /<drive>/ path back to a native Windows path."""
    raw = str(value)
    match = re.fullmatch(r'/([A-Za-z])(?:/(.*))?', raw)
    if match:
        tail = (match.group(2) or '').replace('/', '\\')
        return match.group(1).upper() + ':\\' + tail
    if raw.startswith('//'):
        parts = [part for part in raw[2:].split('/') if part]
        if len(parts) >= 2:
            return '\\\\' + '\\'.join(parts)
    return raw


def lock_fd(fd: int, *, shared: bool = False, nonblocking: bool = False) -> None:
    """OS-backed kernel lock on the same first byte for every participant.

    Windows LockFileEx supports both shared and exclusive locks, unlike the
    msvcrt.locking shortcut. The caller retains both the fd and OVERLAPPED
    object until the unlock operation.
    """
    if not WINDOWS:
        import fcntl
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, flags | (fcntl.LOCK_NB if nonblocking else 0))
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
    kernel.LockFileEx.restype = wintypes.BOOL
    overlap = OVERLAPPED()
    flags = (0 if shared else 2) | (1 if nonblocking else 0)
    if not kernel.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(overlap)):
        err = ctypes.get_last_error()
        if nonblocking and err in (32, 33, 158):
            raise BlockingIOError(err, 'Gaston lock is in use')
        raise ctypes.WinError(err)
    _WINDOWS_LOCKS[fd] = (kernel, overlap)


_WINDOWS_LOCKS = {}


def unlock_fd(fd: int) -> None:
    if not WINDOWS:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel, overlap = _WINDOWS_LOCKS.pop(fd)
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.c_void_p]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    if not kernel.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(overlap)):
        raise ctypes.WinError(ctypes.get_last_error())


def open_lock(path: Path) -> int:
    """Validate ownership record before locking. Never follow a known symlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ValueError('Unsafe lock file: ' + str(path))
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    if os.fstat(fd).st_nlink != 1:
        os.close(fd)
        raise ValueError('Untrusted lock file')
    return fd


@contextmanager
def file_lock(path: Path, *, shared=False, nonblocking=False):
    fd = open_lock(path)
    try:
        lock_fd(fd, shared=shared, nonblocking=nonblocking)
        try:
            yield
        finally:
            unlock_fd(fd)
    finally:
        os.close(fd)


def popen_kwargs(*, isolate=True):
    if not isolate:
        return {}
    if WINDOWS:
        # New process group allows CTRL_BREAK; descendant cleanup uses psutil.
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def _psutil():
    if WINDOWS:
        try:
            import psutil
            return psutil
        except ImportError as exc:
            raise RuntimeError('Windows runtime requires psutil; reinstall Gaston with pip') from exc
    return None


def identity(pid: int) -> dict:
    if WINDOWS:
        psutil = _psutil()
        try:
            p = psutil.Process(pid)
            return {'start_ticks': str(int(p.create_time() * 1000000))}
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return {}
    return {}


def process_status(pid: int) -> str:
    """Windows process existence with no false-dead verdict on access denial."""
    psutil = _psutil()
    try:
        p = psutil.Process(pid)
        return 'dead' if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE else 'live'
    except psutil.NoSuchProcess:
        return 'dead'
    except (psutil.AccessDenied, OSError):
        return 'unknown'


def stop_owned(proc: subprocess.Popen, *, isolated_group=False, grace_seconds=3):
    """Terminate only the captured Popen and its descendants, never an arbitrary PID."""
    if WINDOWS:
        psutil = _psutil()
        try:
            parent = psutil.Process(proc.pid)
            if abs(parent.create_time() - getattr(proc, '_gaston_start', parent.create_time())) > .01:
                raise RuntimeError('Process identity changed; refusing termination')
            children = parent.children(recursive=True)
            targets = children + [parent]
            for child in reversed(targets):
                try: child.terminate()
                except psutil.NoSuchProcess: pass
            _, still = psutil.wait_procs(targets, timeout=grace_seconds)
            for child in still:
                try: child.kill()
                except psutil.NoSuchProcess: pass
            psutil.wait_procs(still, timeout=5)
            proc.wait(timeout=5)
        except psutil.NoSuchProcess:
            proc.wait(timeout=5)
        return
    if isolated_group:
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: pass
    elif proc.poll() is None:
        proc.terminate()
    try: proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired: pass
    if isolated_group:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    elif proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)



def prepare_subprocess(argv, env=None):
    """Resolve native executables and safely invoke Windows .cmd/.bat shims.

    PowerShell receives a base64-encoded argument array, not interpolated
    command text. The temporary payload is removed before the child starts.
    """
    args=[str(value) for value in argv]
    if not args:raise ValueError('Cannot launch an empty command')
    if not WINDOWS:return args,env,False
    import base64
    import json
    import shutil
    executable=shutil.which(args[0]) or args[0]
    args[0]=executable
    if Path(executable).suffix.lower() not in ('.cmd','.bat'):
        return args,env,False
    powershell=shutil.which('powershell.exe') or shutil.which('powershell')
    if not powershell:
        system_root=os.environ.get('SystemRoot') or os.environ.get('WINDIR')
        if system_root:
            candidate=Path(system_root)/'System32/WindowsPowerShell/v1.0/powershell.exe'
            if candidate.is_file():powershell=str(candidate)
    if not powershell:raise RuntimeError('Windows PowerShell is required to launch this command shim')
    payload=base64.b64encode(json.dumps(args,ensure_ascii=False).encode('utf-8')).decode('ascii')
    script=(
        "$ErrorActionPreference='Stop';"
        "$raw=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:GASTON_WINDOWS_BATCH_ARGS));"
        "Remove-Item Env:GASTON_WINDOWS_BATCH_ARGS;"
        "$items=ConvertFrom-Json -InputObject $raw;"
        "$exe=[string]$items[0];$cliArgs=@();"
        "if($items.Count -gt 1){$cliArgs=@($items|Select-Object -Skip 1|ForEach-Object{[string]$_})};"
        "& $exe @cliArgs;$code=$LASTEXITCODE;if($null -eq $code){$code=0};exit [int]$code"
    )
    encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    child_env=dict(os.environ if env is None else env)
    child_env['GASTON_WINDOWS_BATCH_ARGS']=payload
    return [powershell,'-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',encoded],child_env,True

def protect_workspace_local(root: Path) -> None:
    """Ensure sensitive runtime state cannot inherit broad Windows ACLs.

    This is a fail-closed prerequisite for *all* Windows Gaston commands and
    hooks. We do not claim Linux chmod protects Windows credential files.
    """
    if not WINDOWS:return
    import csv
    from io import StringIO
    local=project_state(root)/'local'
    if local.is_symlink():raise RuntimeError('Linked Gaston runtime state is unsafe')
    local.mkdir(parents=True,exist_ok=True)
    try:
        result=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,timeout=10,check=True)
        rows=list(csv.reader(StringIO(result.stdout.strip())))
        sid=rows[0][1] if len(rows)==1 and len(rows[0])==2 else ''
        if not sid.startswith('S-1-5-'):raise RuntimeError('Invalid current Windows SID')
        principal='*'+sid+':(OI)(CI)F'
        allowed=['icacls',str(local),'/grant:r',principal,'*S-1-5-18:(OI)(CI)F',
                 '*S-1-5-32-544:(OI)(CI)F','/T','/C']
        subprocess.run(allowed,check=True,capture_output=True,text=True,timeout=120)
        subprocess.run(['icacls',str(local),'/inheritance:r','/T','/C'],
                       check=True,capture_output=True,text=True,timeout=120)
        # All state created after this inherits the hardened directory DACL.
    except (OSError,subprocess.CalledProcessError,subprocess.TimeoutExpired,IndexError,ValueError) as exc:
        raise RuntimeError('Cannot secure Gaston runtime state using native Windows ACLs; refusing to start') from exc


def protect_private_path(path: Path) -> None:
    """Harden an existing Windows credential file or folder, never links."""
    if not WINDOWS:return
    path=Path(path)
    if path.is_symlink() or not path.exists():raise RuntimeError('Untrusted Windows credential path')
    import csv
    from io import StringIO
    try:
        result=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,timeout=10,check=True)
        rows=list(csv.reader(StringIO(result.stdout.strip())))
        sid=rows[0][1] if len(rows)==1 and len(rows[0])==2 else ''
        if not sid.startswith('S-1-5-'):raise RuntimeError('Cannot identify Windows owner')
        suffix=':(OI)(CI)F' if path.is_dir() else ':F'
        subprocess.run(['icacls',str(path),'/grant:r','*'+sid+suffix,
                        '*S-1-5-18'+suffix,'*S-1-5-32-544'+suffix,'/T','/C'],
                       check=True,capture_output=True,text=True,timeout=120)
        subprocess.run(['icacls',str(path),'/inheritance:r','/T','/C'],
                       check=True,capture_output=True,text=True,timeout=120)
    except (OSError,subprocess.CalledProcessError,subprocess.TimeoutExpired,IndexError,ValueError) as exc:
        raise RuntimeError('Unable to restrict Windows credential ACLs; refusing to use file') from exc


def parse_command(command: str) -> list[str]:
    """Parse a provider hook string according to its native process grammar."""
    if not WINDOWS:
        import shlex
        return shlex.split(command)
    import ctypes
    from ctypes import wintypes
    shell=ctypes.WinDLL('shell32',use_last_error=True)
    shell.CommandLineToArgvW.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype=ctypes.POINTER(ctypes.c_wchar_p)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[wintypes.HLOCAL]
    kernel.LocalFree.restype=wintypes.HLOCAL
    count=ctypes.c_int()
    argv=shell.CommandLineToArgvW(command,ctypes.byref(count))
    if not argv:raise ValueError('Cannot parse Windows hook command')
    try:return [argv[i] for i in range(count.value)]
    finally:kernel.LocalFree(ctypes.cast(argv,wintypes.HLOCAL))
