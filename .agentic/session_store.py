"""Per-project persistent sessions, keyed by provider + engineering role.

A provider can have independent builder and reviewer sessions. Existing one-session
registries are migrated without losing their stored session IDs. Roles never swap
silently when a developer changes provider mode.
"""
from __future__ import annotations
from project_layout import project_state
from contextlib import contextmanager, nullcontext
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import platform
import subprocess
import shlex
from platform_compat import file_lock, identity as windows_identity, process_status as windows_process_status, WINDOWS
import socket
import stat
import tempfile
import uuid

PROVIDERS = ('codex', 'claude')
ROLES = ('builder', 'reviewer')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def valid_id(value):
    try:
        return str(uuid.UUID(value)) == str(value).lower()
    except (ValueError, TypeError, AttributeError):
        return False


def _ps(*args):
    try:
        return subprocess.check_output(['/bin/ps', *args], text=True, stderr=subprocess.DEVNULL, timeout=5, env={**os.environ,'LC_ALL':'C'}).strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def _mac_row(pid):
    output = _ps('-p', str(pid), '-o', 'ppid=,state=,comm=,args=')
    fields = output.split(None, 3)
    if len(fields) != 4: raise ValueError('Process ancestry unavailable')
    return int(fields[0]), fields[1], _provider_names(fields[2], fields[3])


def _provider_names(comm, args):
    try: words = shlex.split(args)[:3]
    except ValueError: words = args.split()[:3]
    return [Path(w).name for w in [comm,*words] if w]


def _mac_processes():
    output = _ps('-ax','-o','pid=,uid=,ppid=,state=,comm=,args=')
    if not output: return None
    result = {}
    for line in output.splitlines():
        fields = line.split(None, 5)
        if len(fields)!=6: continue
        try: pid,uid,parent = map(int,fields[:3])
        except ValueError: continue
        if uid==os.getuid() and not fields[3].startswith(('Z','X')):
            result[pid]=(parent,_provider_names(fields[4],fields[5]),fields[5])
    return result


def _mac_cwd(pid):
    try:
        output=subprocess.check_output(['/usr/sbin/lsof','-a','-p',str(pid),'-d','cwd','-Fn'],text=True,stderr=subprocess.DEVNULL,timeout=5)
        return next((Path(line[1:]).resolve(strict=True) for line in output.splitlines() if line.startswith('n')),None)
    except (OSError, subprocess.SubprocessError): return None


def provider_name(names):
    normalized=[Path(str(n).replace('\\','/')).name.lower().removesuffix('.exe').removesuffix('.cmd').removesuffix('.bat') for n in names]
    return next((p for p in PROVIDERS if any(n==p or n==p+'.js' or n.startswith(p+'-') for n in normalized)),None)


def parent_and_names(pid):
    if WINDOWS:
        import psutil
        p=psutil.Process(pid)
        return p.ppid(), [p.name(), *[Path(w).name for w in p.cmdline()[:3]]]
    if platform.system()=='Darwin':
        parent,_,names=_mac_row(pid)
        return parent,names
    proc=Path('/proc')/str(pid)
    argv=(proc/'cmdline').read_bytes().split(b'\0')
    names=[(proc/'comm').read_text().strip(),*[Path(os.fsdecode(a)).name for a in argv[:3] if a]]
    parent=int((proc/'stat').read_text().rsplit(')',1)[1].split()[1])
    return parent,names


def process_identity(pid):
    """Process birth identity prevents mistaking an unrelated reused PID for the owner."""
    if WINDOWS:
        return windows_identity(pid)
    if platform.system()=='Darwin':
        started=_ps('-p',str(pid),'-o','lstart=')
        return {'start_ticks':started} if started else {}
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'start_ticks': fields[19], 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError, ValueError):
        return {}


def process_state(info):
    """Return dead, live or unknown; an unreadable owner is never proof of death."""
    if not isinstance(info, dict) or info.get('host', socket.gethostname()) != socket.gethostname():
        return 'unknown'
    pid = info.get('pid')
    if type(pid) is not int or pid <= 0:
        return 'unknown'
    if WINDOWS:
        status=windows_process_status(pid)
        if status!='live':return status
        current=process_identity(pid)
        if info.get('start_ticks') and current.get('start_ticks') and info['start_ticks']!=current['start_ticks']:
            return 'dead'
        if info.get('start_ticks') and not current.get('start_ticks'):
            return 'unknown'
        return 'live'
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'dead'
    except (PermissionError, OverflowError, OSError):
        return 'unknown'
    current = process_identity(pid)
    for key in ('boot_id', 'start_ticks'):
        if info.get(key) and current.get(key) and info[key] != current[key]:
            return 'dead'
    if platform.system()=='Darwin':
        state=_ps('-p',str(pid),'-o','state=')
        return 'dead' if state.startswith(('Z','X')) else 'live' if state else 'unknown'
    try:
        state = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[0]
        if state in ('Z', 'X'):
            return 'dead'
    except (OSError, IndexError):
        pass
    return 'live'


def isolated_group_active(pid):
    """Count live members of a managed POSIX group, not unreaped zombies."""
    if WINDOWS:
        # No POSIX groups on Windows; inspection happens in provider_child_active.
        return windows_process_status(pid) != 'dead'
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    proc = Path('/proc')
    if not proc.is_dir():
        return True  # No reliable way to distinguish a zombie-only group.
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields=(entry/'stat').read_text().rsplit(')',1)[1].split()
            if int(fields[2])==pid and fields[0] not in ('Z','X'):
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, IndexError, ValueError):
            return True  # Unverifiable membership is not proof of exit.
    return False


def provider_child_active(child):
    """A recorded isolated provider group may outlive its original CLI process."""
    if process_state(child) != 'dead':
        return True
    if not child.get('isolated_group'):
        return False
    if WINDOWS:
        import psutil
        pid=child.get('pid')
        if type(pid) is not int:return True
        try:
            # Follow the full ancestry chain: Codex may be nested below
            # cmd.exe/PowerShell/npm rather than being the direct child.
            # The start-time floor prevents matching a reused PID from before
            # the managed provider launch.
            started=int(child.get('start_ticks','0'))
            for candidate in psutil.process_iter(['pid','create_time']):
                try:
                    if candidate.info['create_time']*1000000 < started:continue
                    parent=candidate
                    seen=set()
                    while parent is not None and parent.pid not in seen:
                        if parent.pid==pid:return True
                        seen.add(parent.pid)
                        parent=parent.parent()
                except (psutil.NoSuchProcess,psutil.AccessDenied,psutil.ZombieProcess):
                    continue
            return False
        except (psutil.Error, OSError, ValueError):
            return True
    pid=child.get('pid')
    if type(pid) is not int or pid <= 0:
        return True
    if child.get('boot_id') and child['boot_id'] != process_identity(os.getpid()).get('boot_id'):
        return False
    current=process_identity(pid)
    if child.get('start_ticks') and current.get('start_ticks') and child['start_ticks'] != current['start_ticks']:
        return False  # The original group ID cannot be reused while members remain.
    return isolated_group_active(pid)


def possible_native_chat(root, provider, session_id):
    """Conservative Linux check for an orphaned or externally opened native CLI.

    Legacy locks have no child PID. Never infer their orphaned chat ended from
    the wrapper PID alone. An uninspectable matching provider is a blocker.
    """
    if WINDOWS:
        return any(p['provider']==provider for p in native_processes(root))
    proc = Path('/proc')
    if not proc.is_dir():
        if platform.system()!='Darwin': return True
        rows=_mac_processes()
        if rows is None: return True
        for pid,(_,names,args) in rows.items():
            if provider_name(names)!=provider: continue
            if session_id and session_id in args: return True
            cwd=_mac_cwd(pid)
            if cwd is None or cwd==root or root in cwd.parents: return True
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
            comm = (entry / 'comm').read_text().strip()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return True
        # Provider binaries can be native executables or Node CLI scripts.
        names = [Path(os.fsdecode(a)).name for a in argv[:3] if a]
        if not any(n == provider or n.startswith(provider+'-') or n == provider+'.js' for n in [comm, *names]):
            continue
        if process_state({'pid': int(entry.name)}) == 'dead':
            continue
        if session_id and os.fsencode(session_id) in argv:
            return True
        try:
            cwd = (entry / 'cwd').resolve(strict=True)
            if cwd == root or root in cwd.parents:
                return True
        except (FileNotFoundError, ProcessLookupError):
            if process_state({'pid': int(entry.name)}) != 'dead':
                return True
        except OSError:
            return True
    return False


def native_processes(root, *, exclude=()):
    """Same-workspace Linux provider processes, excluding proven managed trees.

    Only report PID/provider (never argv, prompts or credentials). A native CLI
    started outside Gaston may not have a lock or registered session. On hosts
    without procfs the external-process check is unavailable; role locks still
    apply. Do not kill processes or infer that an unreadable process is stale.
    """
    root = Path(root).resolve()
    if WINDOWS:
        import psutil
        result=[]; excluded=set(exclude)
        for p in psutil.process_iter(['pid','name','cmdline','username']):
            try:
                if p.pid==os.getpid():continue
                names=[p.info['name'] or '', *[Path(w).name for w in (p.info['cmdline'] or [])[:3]]]
                provider=provider_name(names)
                if provider is None:continue
                parent=p
                while parent is not None:
                    if parent.pid in excluded:break
                    parent=parent.parent()
                else:
                    pass
                if parent is not None and parent.pid in excluded:continue
                cwd=Path(p.cwd()).resolve()
                if cwd==root or root in cwd.parents:result.append({'pid':p.pid,'provider':provider})
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                # An unreadable external process cannot establish workspace ownership.
                continue
        return sorted(result,key=lambda row:row['pid'])
    proc = Path('/proc')
    if not proc.is_dir():
        if platform.system()!='Darwin': return []
        rows=_mac_processes()
        if rows is None: return []
        excluded=set(exclude)
        result=[]
        for pid,(_,names,_) in rows.items():
            provider=provider_name(names)
            if not provider: continue
            seen=set(); ancestor=pid
            while ancestor in rows and ancestor not in seen and ancestor not in excluded:
                seen.add(ancestor); ancestor=rows[ancestor][0]
            if ancestor in excluded: continue
            cwd=_mac_cwd(pid)
            if cwd is not None and (cwd==root or root in cwd.parents): result.append({'pid':pid,'provider':provider})
        return sorted(result,key=lambda row:row['pid'])
    excluded = set(exclude)
    def in_managed_tree(pid):
        seen = set()
        while pid > 1 and pid not in seen:
            if pid in excluded:
                return True
            seen.add(pid)
            try:
                fields = (proc/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
                pid = int(fields[1])
            except (OSError, IndexError, ValueError):
                return False
        return False
    result = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = (entry/'cmdline').read_bytes().split(b'\0')
            names = [(entry/'comm').read_text().strip(), *[Path(os.fsdecode(a)).name for a in argv[:3] if a]]
            provider = next((p for p in PROVIDERS if any(n == p or n == p+'.js' or n.startswith(p+'-') for n in names)), None)
            if not provider or process_state({'pid':pid}) == 'dead' or in_managed_tree(pid):
                continue
            cwd = (entry/'cwd').resolve(strict=True)
            if cwd == root or root in cwd.parents:
                result.append({'pid':pid,'provider':provider})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            # No positive workspace identity; avoid blocking unrelated projects.
            continue
    return sorted(result,key=lambda row:row['pid'])


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.local = project_state(self.root) / 'local'
        self.registry = self.local / 'sessions.json'
        self.locks = self.local / 'locks'
        self._held_leases = {}

    def _configured_roles(self):
        try:
            cfg = json.loads((project_state(self.root) / 'config.json').read_text(encoding='utf8'))
            prefs = cfg.get('preferences', {})
            builder=prefs.get('builder','codex')
            reviewer=prefs.get('reviewer','claude')
            return (builder if builder in PROVIDERS else 'codex',
                    reviewer if reviewer in PROVIDERS else 'claude')
        except (OSError, ValueError, TypeError):
            return 'codex', 'claude'

    def load(self):
        if not self.registry.exists():
            return {'root': str(self.root), 'sessions': {p: {r: None for r in ROLES} for p in PROVIDERS}, 'archive': []}
        data = json.loads(self.registry.read_text(encoding='utf-8'))
        if data.get('root') != str(self.root):
            raise RuntimeError('Session registry belongs to another checkout. Rebind sessions explicitly.')
        if 'sessions' not in data:
            # Legacy v0.9.x registry stored one primary session per provider.
            # Preserve identity under its *previous* role, not the current selection.
            old = data.get('roles', {})
            previous = data.get('assignments') or {'codex': 'builder', 'claude': 'reviewer'}
            data['sessions'] = {p:{r:None for r in ROLES} for p in PROVIDERS}
            for p in PROVIDERS:
                entry = old.get(p)
                if entry:
                    role = previous.get(p, 'builder' if p=='codex' else 'reviewer')
                    if role in ROLES: data['sessions'][p][role] = entry
        for p in PROVIDERS:
            data['sessions'].setdefault(p, {})
            for role in ROLES:
                entry = data['sessions'][p].setdefault(role, None)
                if entry and not valid_id(entry.get('id')):
                    raise RuntimeError(f'Invalid saved {p}/{role} session ID')
        data.setdefault('archive', [])
        return data

    def save(self, data):
        self.local.mkdir(parents=True, exist_ok=True)
        if data.get('root') != str(self.root):
            raise RuntimeError('Registry root mismatch')
        fd, tmp = tempfile.mkstemp(prefix='.sessions-', suffix='.tmp', dir=self.local)
        try:
            if os.name != 'nt': os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=2)
                file.write('\n'); file.flush(); os.fsync(file.fileno())
            os.replace(tmp, self.registry)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def _role(self, provider, role):
        if provider not in PROVIDERS: raise ValueError('Unknown provider')
        if role is None:
            builder, reviewer = self._configured_roles()
            role = 'builder' if builder == provider else 'reviewer' if reviewer == provider else 'builder'
        if role not in ROLES: raise ValueError('Unknown role')
        return role

    def get(self, provider, role=None):
        role = self._role(provider, role)
        return self.load()['sessions'][provider][role]

    @contextmanager
    def registry_write(self):
        """Serialize short registry RMW operations, not provider execution."""
        with file_lock(self.local/'sessions.lock'):
            yield

    def bind(self, provider, session_id, status='ready', role=None):
        role = self._role(provider,role)
        if not valid_id(session_id): raise ValueError('UUID session ID required')
        with self.registry_write():
            data = self.load()
            old = data['sessions'][provider][role]
            if old and old['id'] != session_id:
                raise RuntimeError(f'{provider}/{role} already has a session; rotate explicitly')
            # One ID cannot be enrolled into both roles or two providers.
            for p in PROVIDERS:
                for r in ROLES:
                    other = data['sessions'][p][r]
                    if other and other['id'] == session_id and (p,r)!=(provider,role):
                        raise RuntimeError('Session ID already registered for another role')
            data['sessions'][provider][role] = {
                'id':session_id,'status':status,
                'created_at':(old or {}).get('created_at',now()),'last_used_at':now()}
            self.save(data)

    def mark(self, provider, status, role=None):
        role = self._role(provider, role)
        with self.registry_write():
            data = self.load()
            entry = data['sessions'][provider][role]
            if not entry: raise RuntimeError(f'No {provider}/{role} session saved')
            entry['status'] = status; entry['last_used_at'] = now()
            self.save(data)

    def rotate(self, provider, role=None):
        role = self._role(provider, role)
        with self.registry_write():
            data = self.load()
            old = data['sessions'][provider][role]
            if old: data['archive'].append({'provider':provider,'role':role,**old,'rotated_at':now()})
            data['sessions'][provider][role] = None
            self.save(data)
            return old

    def enrolled_role(self, provider, session_id):
        if provider not in PROVIDERS or not valid_id(session_id): return None
        data = self.load()
        builder, reviewer = self._configured_roles()
        for role, selected in (('builder',builder),('reviewer',reviewer)):
            if selected == provider:
                entry = data['sessions'][provider][role]
                if entry and entry['id']==session_id: return role
        return None

    def selected(self):
        builder, reviewer = self._configured_roles()
        return [(builder,'builder'),(reviewer,'reviewer')] if builder in PROVIDERS and reviewer in PROVIDERS else []

    def marker(self, provider, role):
        return self.local / 'native-hooks' / 'active' / f'{provider}-{role}.json'

    @contextmanager
    def guard(self, provider, role):
        """Serialize ownership checks, marker writes and stale cleanup across processes."""
        role = self._role(provider, role)
        path = self.local / 'session-guards' / f'{provider}-{role}.lock'
        with file_lock(path):
            yield

    def _records(self, provider, role):
        lock = self.locks / f'{provider}-{role}'
        marker = self.marker(provider, role)
        for path in (lock, marker, lock/'owner.json'):
            if path.is_symlink():
                raise RuntimeError('Unknown linked session ownership record')
        try:
            owner = json.loads((lock/'owner.json').read_text()) if lock.exists() else None
            activity = json.loads(marker.read_text()) if marker.exists() else None
        except (OSError, ValueError):
            raise RuntimeError('Unknown session owner; preserve records')
        if ((lock.exists() and not isinstance(owner, dict)) or
                (marker.exists() and not isinstance(activity, dict))):
            raise RuntimeError('Invalid session ownership record')
        return lock, marker, owner, activity

    def _recover(self, provider, role):
        """Caller holds guard. Remove only records proven abandoned, never a chat."""
        lock, marker, owner, activity = self._records(provider, role)
        if owner is None and activity is None:
            return 'not locked'
        if owner is not None:
            if not owner.get('host') or process_state(owner) != 'dead':
                raise RuntimeError('Session owner is live or unknown')
            if owner.get('child') and provider_child_active(owner['child']):
                raise RuntimeError('Native provider child is live or unknown')
        binding = self.get(provider, role)
        sid = (binding or {}).get('id')
        if activity is not None:
            if owner is None and activity.get('started_at'):
                try:
                    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(activity['started_at'])).total_seconds()
                except (TypeError, ValueError):
                    raise RuntimeError('Unknown activity timestamp')
                if age < 30:
                    raise RuntimeError('Native hook startup may still be in progress')
            if activity.get('root') != str(self.root) or not sid or activity.get('session_id') != sid:
                raise RuntimeError('Activity marker does not match registered session')
            if activity.get('lease') and owner and activity['lease'] != owner.get('lease'):
                raise RuntimeError('Activity marker belongs to a different native lease')
            if activity.get('native_process') and process_state(activity['native_process']) != 'dead':
                raise RuntimeError('Native activity process is live or unknown')
        if possible_native_chat(self.root, provider, sid):
            raise RuntimeError('A native provider chat may still be active')
        # The lock and marker are reconciled as one operation under the guard.
        if activity is not None:
            marker.unlink()
        if owner is not None:
            shutil.rmtree(lock)
        return 'cleared stale lock' if owner is not None else 'cleared stale activity'

    def reconcile(self, provider, role=None):
        role = self._role(provider, role)
        with self.guard(provider, role):
            return self._recover(provider, role)

    def active(self, provider, role):
        try:
            self.reconcile(provider, role)
            return False
        except (RuntimeError, OSError, ValueError):
            return True

    @contextmanager
    def lead_gate(self):
        """Serialize Lead claims across providers; retain legacy role guards."""
        path = self.local/'session-guards'/'workspace-lead.lock'
        with file_lock(path):
            yield

    def unmanaged_native(self, *, include_managed=False):
        known = []
        if not include_managed:
            for provider in PROVIDERS:
                for role in ROLES:
                    try:
                        _, _, owner, _ = self._records(provider,role)
                        child = (owner or {}).get('child')
                        if child and process_state(child) == 'live':
                            known.append(child['pid'])
                    except (RuntimeError, OSError, ValueError):
                        continue
        return native_processes(self.root,exclude=known)

    def lead_caller(self, lease, provider, pid):
        """Check lease and nearest provider ancestor; an inherited token is insufficient."""
        if not lease or provider not in PROVIDERS:return False
        try:
            with self.guard(provider,'reviewer'):
                _,_,owner,_=self._records(provider,'reviewer')
                if not owner or owner.get('lease')!=lease or process_state(owner)!='live':return False
                child=owner.get('child')
                if not child or process_state(child)!='live':return False
                seen=set()
                while pid>1 and pid not in seen:
                    seen.add(pid)
                    if pid==child['pid']:return True
                    pid,names=parent_and_names(pid)
                    if provider_name(names):return False
        except (OSError,ValueError,RuntimeError,IndexError,KeyError):return False
        return False

    def assert_idle_for_setup(self):
        # Caller owns lead_gate. Never reset/kill a live owner or unknown record.
        for provider in PROVIDERS:
            for role in ROLES:
                if self.active(provider,role):
                    raise RuntimeError(f'{provider}/{role} is active or ownership is uncertain. Close it before setup.')
        self.assert_no_unmanaged_native()

    def assert_no_unmanaged_native(self):
        conflicts = self.unmanaged_native()
        if conflicts:
            detail = ', '.join(f"{r['provider']} PID {r['pid']}" for r in conflicts)
            raise RuntimeError('Unmanaged native AI chat in this workspace: '+detail+'. Close it normally or investigate via dev sessions; no process was terminated.')

    @contextmanager
    def locked(self, provider, role=None):
        role = self._role(provider, role)
        lock = self.locks / f'{provider}-{role}'
        lease = uuid.uuid4().hex
        with (self.lead_gate() if role == 'reviewer' else nullcontext()), self.guard(provider, role):
            try:
                self._recover(provider, role)
                if role == 'reviewer':
                    for other in PROVIDERS:
                        if other != provider:
                            with self.guard(other,role):
                                self._recover(other,role)
                    self.assert_no_unmanaged_native()
            except RuntimeError as exc:
                raise RuntimeError(f'{provider}/{role} native chat is active or ownership is uncertain: {exc}') from exc
            self.locks.mkdir(parents=True, exist_ok=True)
            info = {'pid': os.getpid(), 'host': socket.gethostname(), 'since': now(), 'lease': lease, **process_identity(os.getpid())}
            # Publish a fully written lock directory atomically: a crash before
            # publication cannot leave an ambiguous ownerless live lock.
            temporary = Path(tempfile.mkdtemp(prefix='.lease-', dir=self.local/'session-guards'))
            try:
                (temporary/'owner.json').write_text(json.dumps(info))
                os.rename(temporary, lock)
            finally:
                if temporary.exists(): shutil.rmtree(temporary)
        self._held_leases[lease] = (provider, role)
        # Only this invocation's provider children and native hooks receive the lease.
        old = os.environ.get('GASTON_SESSION_LEASE')
        os.environ['GASTON_SESSION_LEASE'] = lease
        try:
            yield
        finally:
            self._held_leases.pop(lease, None)
            if old is None:
                os.environ.pop('GASTON_SESSION_LEASE', None)
            else:
                os.environ['GASTON_SESSION_LEASE'] = old
            with self.guard(provider, role):
                _, marker, owner, activity = self._records(provider, role)
                if owner and owner.get('lease') == lease:
                    # Never discard records if an orphan child outlived the wrapper.
                    if not owner.get('child') or not provider_child_active(owner['child']):
                        if activity and activity.get('lease') == lease:
                            marker.unlink()
                        shutil.rmtree(lock)

    def record_child(self, pid, *, isolated_group=False):
        lease = os.environ.get('GASTON_SESSION_LEASE')
        if not lease or type(pid) is not int:
            return
        held = self._held_leases.get(lease)
        if held is None:
            return
        provider, role = held
        with self.guard(provider, role):
            lock, _, owner, _ = self._records(provider, role)
            if not owner or owner.get('lease') != lease or owner.get('pid') != os.getpid():
                raise RuntimeError('Managed native lease changed during startup')
            owner['child'] = {'pid': pid, 'host': socket.gethostname(), **process_identity(pid)}
            if isolated_group:
                owner['child']['isolated_group'] = True
            fd, tmp = tempfile.mkstemp(dir=lock, prefix='.owner-')
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(owner, file)
                os.replace(tmp, lock/'owner.json')
            finally:
                if os.path.exists(tmp): os.unlink(tmp)

    def unlock_stale(self, provider, role=None):
        return self.reconcile(provider, role)


def claude_transcript_missing(session_id, *, allow_absent_store=False):
    """Check the standard local Claude Code store for an exact UUID.

    Check directory entries only, not conversation content. An inaccessible
    or symlinked store is unknown, not missing. A failed first launch can
    legitimately leave no projects directory at all.
    """
    if not valid_id(session_id):
        return False
    store = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude').expanduser()/'projects'
    try:
        if store.is_symlink():
            return False
        try:
            projects = list(store.iterdir())
        except FileNotFoundError:
            return bool(allow_absent_store)
        for project in projects:
            mode = project.lstat().st_mode
            if stat.S_ISLNK(mode):
                return False  # Unknown alternate storage, do not rotate.
            if not stat.S_ISDIR(mode):
                continue
            # A transcript may have been placed under a differently encoded cwd.
            try:
                (project/(session_id+'.jsonl')).lstat()
                return False
            except FileNotFoundError:
                continue
        return True
    except OSError:
        return False
