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


def _signal_owned_group(pgid, sig):
    """Signal a POSIX group; macOS may deny a signal to an exiting zombie group.

    Permission denied is never treated as successful cleanup unless an OS
    process snapshot proves that the group has no non-zombie members.
    """
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as denied:
        try:
            listing = subprocess.run(['/bin/ps', '-A', '-o', 'pgid=', '-o', 'stat='],
                                     stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, timeout=3, check=True)
            for line in listing.stdout.splitlines():
                if not line.strip():
                    continue
                fields = line.split()
                if len(fields) != 2 or not fields[0].isdigit() or not fields[1]:
                    raise ValueError('Unverifiable process-group snapshot')
                if int(fields[0]) == pgid and not fields[1].startswith('Z'):
                    raise denied
        except (OSError, ValueError, subprocess.SubprocessError):
            raise denied
        return False


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
    group_exists = False
    if isolated_group:
        group_exists = _signal_owned_group(proc.pid, signal.SIGTERM)
    elif proc.poll() is None:
        proc.terminate()
    try: proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired: pass
    if isolated_group:
        if group_exists:
            _signal_owned_group(proc.pid, signal.SIGKILL)
    elif proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)



def windows_session_id():
    """A Local job name is only meaningful in the same Windows session."""
    if not WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = kernel.ProcessIdToSessionId
    fn.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    fn.restype = wintypes.BOOL
    session = wintypes.DWORD()
    return session.value if fn(os.getpid(), ctypes.byref(session)) else None


def windows_verification_job_gone(name):
    """Check a private named job's namespace entry, not process termination.

    The name can disappear while termination is still asynchronous. Only a
    separate positive cleanup receipt can establish that the owned tree drained.
    """
    if not WINDOWS or not isinstance(name, str) or not re.fullmatch(r'Local\\GastonVerification-[0-9a-f]{32}', name):
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenJobObjectW(0x0004, False, name)  # JOB_OBJECT_QUERY
    if not handle:
        return ctypes.get_last_error() == 2  # ERROR_FILE_NOT_FOUND only
    kernel.CloseHandle(handle)
    return False


class _WindowsVerificationJob:
    """Own the complete verifier process tree, even after its parent exits.

    The Python gate starts without launching the command. Assign its process
    to a kill-on-close job before allowing it to create descendants.
    """
    def __init__(self, job_name=None):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IOCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ('TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                            (name, wintypes.DWORD) for name in
                            ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, args, result in (
            ('CreateJobObjectW', [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ('SetInformationJobObject', [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ('AssignProcessToJobObject', [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ('TerminateJobObject', [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ('QueryInformationJobObject', [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL),
        ):
            fn = getattr(kernel, name)
            fn.argtypes = args
            fn.restype = result
        self.kernel, self.ctypes, self.accounting = kernel, ctypes, Accounting
        ctypes.set_last_error(0)
        self.handle = kernel.CreateJobObjectW(None, job_name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            self.close()
            raise RuntimeError('Verification job name already exists; refusing to reuse')
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, proc):
        # Use the original Popen process handle, not a potentially recycled PID.
        if not self.kernel.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def stop(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        deadline = time.monotonic() + 5
        while True:
            info = self.accounting()
            if not self.kernel.QueryInformationJobObject(
                    self.handle, 1, self.ctypes.byref(info), self.ctypes.sizeof(info), None):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            if not info.ActiveProcesses:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned Windows verification processes did not terminate')
            time.sleep(.05)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def persist_verification_log(log_dir, stdout, stderr):
    """Persist complete private subprocess output with restrictive local permissions."""
    import secrets
    import shutil
    if log_dir is None:return None
    try:
        directory=Path(log_dir)
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        if directory.is_symlink():return None
        if os.name!='nt':os.chmod(directory,0o700)
        target=directory/('verify-'+secrets.token_hex(12)+'.log')
        flags=os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(target,flags,0o600)
        with os.fdopen(fd,'wb') as dest:
            def write_part(value):
                if hasattr(value,'seek') and hasattr(value,'read'):
                    value.seek(0)
                    shutil.copyfileobj(value,dest)
                elif isinstance(value,bytes):
                    dest.write(value)
                else:
                    dest.write(str(value or '').encode('utf8',errors='replace'))
            dest.write(b'STDOUT\n')
            write_part(stdout)
            dest.write(b'\nSTDERR\n')
            write_part(stderr)
        return str(target)
    except (OSError,ValueError,TypeError):
        return None


def run_verification_process(argv, *, cwd, env=None, timeout=60, output_limit=9000, cancel_event=None, job_name=None, log_dir=None):
    """Bound execution and cleanup without waiting for inherited output-pipe EOF.

    Disk-backed private output handles survive a descendant retaining stdout
    without making the MCP thread wait for EOF. Standard input is never the
    MCP protocol. Windows uses an assigned Job Object; POSIX uses an isolated
    process group. The real command exit code is collected before cleanup.
    """
    import json
    import locale
    import sys
    import tempfile

    started = time.monotonic()
    deadline = started + timeout
    result = {'exit_code': None}
    proc = job = None
    assigned = False
    encoding = locale.getpreferredencoding(False)
    with tempfile.TemporaryFile(mode='w+b') as out, tempfile.TemporaryFile(mode='w+b') as err:
        try:
            if cancel_event is not None and cancel_event.is_set():
                result['reason'] = 'cancelled'
            else:
                kwargs = dict(cwd=cwd, env=env, stdout=out, stderr=err, close_fds=True, **popen_kwargs())
                if WINDOWS:
                    job = _WindowsVerificationJob(job_name)
                    # The base interpreter avoids a venv redirector spawning an
                    # unassigned intermediary. The gate cannot launch before
                    # the parent explicitly releases it after job assignment.
                    gate = ("import json, subprocess, sys\n"
                            "if sys.stdin.buffer.read(1) != b'1': sys.exit(125)\n"
                            "try:\n"
                            "    sys.exit(subprocess.call(json.loads(sys.argv[1]), stdin=subprocess.DEVNULL))\n"
                            "except OSError as exc:\n"
                            "    print(str(exc), file=sys.stderr); sys.exit(127)\n")
                    proc = subprocess.Popen(
                        [sys._base_executable, '-I', '-c', gate, json.dumps(argv)],
                        stdin=subprocess.PIPE, **kwargs)
                    job.assign(proc)
                    assigned = True
                    proc.stdin.write(b'1')
                    proc.stdin.close()
                else:
                    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **kwargs)
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        result['reason'] = 'cancelled'
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        result.update(reason='timeout', timeout_seconds=timeout)
                        break
                    try:
                        result['exit_code'] = proc.wait(timeout=min(.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
        except (OSError, RuntimeError, ValueError) as exc:
            result.update(exit_code=None, reason='execution_failed', detail=str(exc))
        finally:
            try:
                if job and assigned:
                    # Includes children whose original parent already exited.
                    job.stop()
                elif proc and WINDOWS:
                    # Failed assignment: the unreleased gate never ran the tool.
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                    if proc.poll() is None:
                        proc.kill()
                elif proc:
                    stop_owned(proc, isolated_group=True, grace_seconds=1)
                if proc:
                    proc.wait(timeout=5)
                if WINDOWS:
                    # A successful stop queried the actual job's active
                    # process count before close; the name alone proves nothing.
                    result['owned_processes_terminated'] = True
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                result.update(exit_code=None, reason='cleanup_failed', detail=str(exc))
            finally:
                if job:
                    job.close()
        def tail(stream):
            if not output_limit:
                return ''
            end = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, end - output_limit * 4))
            return stream.read(output_limit * 4).decode(encoding, errors='replace')[-output_limit:]
        log_path=persist_verification_log(log_dir,out,err)
        result.update(stdout=tail(out), stderr=tail(err), elapsed_seconds=round(time.monotonic()-started, 3))
        if log_path:result['log_path']=log_path
        if result.get('detail'):
            result['stderr'] = (result['stderr'] + '\n' + result['detail'])[-output_limit:]
        return result


def resolve_flutter(cwd: Path, *, workspace: Path | None = None) -> str:
    """Select an installed project-pinned Flutter SDK without installing anything.

    FVM's current .fvmrc and legacy fvm_config.json are supported. Never use
    PATH for a project with a declared but missing SDK. Restrict ancestor
    discovery to the current Gaston workspace.
    """
    import json
    import shutil
    app = Path(cwd).resolve()
    boundary = Path(workspace).resolve() if workspace is not None else app
    if not app.is_relative_to(boundary):
        raise ValueError('Flutter project escapes workspace')
    binary = 'flutter.bat' if WINDOWS else 'flutter'
    locations = [app, *(p for p in app.parents if p.is_relative_to(boundary))]
    for location in locations:
        config = location / '.fvmrc'
        legacy = location / '.fvm/fvm_config.json'
        pin = None
        if config.is_file() or legacy.is_file():
            source, key = (config, 'flutter') if config.is_file() else (legacy, 'flutterSdkVersion')
            try:
                data = json.loads(source.read_text(encoding='utf-8-sig'))
                pin = data.get(key) if isinstance(data, dict) else None
            except (OSError, ValueError) as exc:
                raise ValueError('Invalid FVM configuration: ' + str(source)) from exc
            if (not isinstance(pin, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', pin)
                    or '..' in pin):
                raise ValueError('Invalid or unsupported Flutter SDK pin in ' + str(source))
        linked = location / '.fvm/flutter_sdk'
        if (linked / 'bin' / binary).is_file():
            actual = linked.resolve()
            # A stale link must not override the configured version. FVM
            # normally points at versions/<pin>; older SDKs also have version.
            version = ''
            try:
                version = (actual / 'version').read_text(encoding='utf-8').strip()
            except (OSError, UnicodeError):
                pass
            if not pin or actual.name == pin or version == pin:
                return str(actual / 'bin' / binary)
        if pin:
            custom = os.environ.get('FVM_CACHE_PATH') or os.environ.get('FVM_HOME')
            cache = Path(custom).expanduser() if custom else Path.home() / 'fvm'
            if not cache.is_absolute():
                raise ValueError('FVM cache must be an absolute path')
            candidate = cache / 'versions' / pin / 'bin' / binary
            if candidate.is_file():
                return str(candidate.resolve())
            raise FileNotFoundError('Project requires FVM Flutter ' + pin
                                    + '; installed SDK not found at ' + str(candidate))
        if linked.exists() or linked.is_symlink():
            raise FileNotFoundError('Project FVM Flutter SDK link is unavailable: ' + str(linked))
    custom = os.environ.get('FVM_CACHE_PATH') or os.environ.get('FVM_HOME')
    cache = Path(custom).expanduser() if custom else Path.home() / 'fvm'
    default = cache / 'default' / 'bin' / binary
    if cache.is_absolute() and default.is_file():
        return str(default.resolve())
    found = shutil.which(binary)
    if not found:
        raise FileNotFoundError('Flutter SDK is unavailable on PATH')
    return found


def maestro_binary() -> str | None:
    import shutil
    names = ('maestro.bat', 'maestro.cmd', 'maestro.exe', 'maestro') if WINDOWS else ('maestro',)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        local = Path.home() / '.maestro' / 'bin' / name
        if local.is_file():
            return str(local)
    return None


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
