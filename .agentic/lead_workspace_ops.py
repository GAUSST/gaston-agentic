#!/usr/bin/env python3
"""Small, bounded operating capabilities for Gaston's Engineering Lead.

This module NEVER accepts shell commands, arbitrary executables, general SQL,
production URLs, or unrestricted file writes. All operations are workspace-local
and have explicit allowlists. This is an operational helper, not a general agent.
"""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode

import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

ROOT_DIR = project_root()
MAX_OUTPUT = 12000
SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', '.gradle', '.idea',
             '.dart_tool', '.next', 'dist', 'build', 'target', '__pycache__', '.gaston-dev-db'}
SECRET_NAMES = {'lead-verification-token', '.env', '.env.local', '.env.production',
                'id_rsa', 'id_ed25519', '.npmrc', '.pypirc', 'credentials.json',
                'secrets.json', 'service-account.json', 'config.json'}
SECRET_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.keystore', '.jks', '.sqlite', '.db'}
SAFE_LOG_SUFFIXES = {'.log', '.txt', '.xml', '.json', '.out'}
_PREVIEW_CHILDREN = {}  # Popen handles retained until cleanup to avoid zombie/resource warnings
STATIC_SUFFIXES = {'.html', '.htm', '.css', '.js', '.mjs', '.svg', '.png', '.jpg',
                   '.jpeg', '.webp', '.gif', '.woff', '.woff2', '.ico', '.map'}
REDACT = [
    # Handles both JSON (quoted keys) and log/environment key=value pairs.
    (re.compile(r"(?i)([\"']?(?:password|passwd|secret|token|api[_-]?key|authorization|access[_-]?key|private[_-]?key)[\"']?\s*[:=]\s*)([\"']?)[^\"'\r\n,}\]]+"), r'\1[REDACTED]'),
    (re.compile(r'(?i)(postgres(?:ql)?://)[^\s@]+@'), r'\1[REDACTED]@'),
    (re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}'), r'\1[REDACTED]'),
]


def response(status, **kwargs):
    return {'status': status, **kwargs}


def _scrub(value: str, *, limit=MAX_OUTPUT) -> str:
    result = str(value)
    for expression, substitute in REDACT:
        result = expression.sub(substitute, result)
    return result if limit is None else result[:limit]


def _path(root, relative, *, required=True, file=False):
    root = Path(root).resolve()
    if not isinstance(relative, str) or '\0' in relative or len(relative) > 500:
        raise ValueError('Invalid relative path')
    value = Path(relative or '.')
    if value.is_absolute() or '..' in value.parts:
        raise ValueError('Only workspace-relative paths are permitted')
    candidate = (root / value).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError('Path escapes workspace (including symlinks)')
    if file and not candidate.is_file():
        raise ValueError('Selected file does not exist')
    if required and not candidate.exists():
        raise ValueError('Selected workspace path does not exist')
    return candidate


def _safe_read_path(root, relative):
    f = _path(root, relative, file=True)
    root = Path(root).resolve()
    rel = f.relative_to(root)
    if f.name.lower() in SECRET_NAMES or f.suffix.lower() in SECRET_SUFFIXES:
        raise ValueError('Secret/configuration file is not readable through operational diagnostics')
    if '.git' in rel.parts or ('.agentic' in rel.parts and 'lead-verification-token' in rel.parts):
        raise ValueError('Git metadata and authorization secrets are restricted')
    if f.stat().st_size > 10*1024*1024:
        raise ValueError('File too large for bounded diagnostics')
    return f


def inspect(root, action='overview', relative_path='', pattern='', max_results=80):
    """Overview and bounded file discovery; Claude native Read/Glob/Grep remains primary."""
    try:
        folder = _path(root, relative_path)
        if not folder.is_dir():
            return response('blocked', reason='Choose a directory for overview/find')
        if '.git' in folder.relative_to(Path(root).resolve()).parts:
            return response('blocked',reason='Git internals are not available through overview')
        if action not in ('overview', 'find'):
            return response('blocked', reason='Unknown inspection action')
        if type(max_results) is not int or not 1 <= max_results <= 150:
            return response('blocked', reason='max_results must be 1–150')
        if action == 'overview':
            records=[]
            for child in sorted(folder.iterdir(), key=lambda f:f.name.lower()):
                if child.name in SKIP_DIRS or child.name in SECRET_NAMES or child.name.startswith('.env'):
                    continue
                records.append({'name':child.name,'type':'directory' if child.is_dir() else 'file' if child.is_file() else 'other'})
                if len(records)>=max_results: break
            return response('ready', path=folder.relative_to(Path(root).resolve()).as_posix(), entries=records,
                            truncated=len(records)>=max_results)
        if not isinstance(pattern,str) or not 2<=len(pattern)<=90 or '\0' in pattern:
            return response('blocked',reason='Provide 2–90 characters for a filename search')
        query=pattern.casefold(); records=[]; dirs_seen=0
        for base, dirs, files in os.walk(folder, followlinks=False):
            dirs[:]=[d for d in dirs if d not in SKIP_DIRS and d not in ('.agentic', '.gaston', '.claude', '.codex')
                     and not (Path(base)/d).is_symlink()]
            dirs_seen+=1
            if dirs_seen>800:break
            for file_name in files:
                if query in file_name.casefold() and file_name not in SECRET_NAMES and not file_name.startswith('.env'):
                    f=Path(base)/file_name
                    if not f.is_symlink() and f.suffix not in SECRET_SUFFIXES:
                        records.append(f.relative_to(Path(root).resolve()).as_posix())
                        if len(records)>=max_results:break
            if len(records)>=max_results:break
        return response('ready', matches=records, directories_scanned=dirs_seen,
                        truncated=dirs_seen>=800 or len(records)>=max_results)
    except (OSError, ValueError) as exc:
        return response('blocked',reason=str(exc)[:300])


def _run_version(binary, argv):
    executable=shutil.which(binary)
    if not executable:
        return {'installed':False}
    try:
        r=subprocess.run([executable,*argv],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                         text=True,timeout=6,check=False,stdin=subprocess.DEVNULL)
        line=(_scrub(r.stdout+'\n'+r.stderr)).splitlines()
        return {'installed':True,'path':executable,'version':next((i[:180] for i in line if i.strip()),''),
                'exit_code':r.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'installed':True,'status':'unavailable','reason':type(exc).__name__}


VERSION_PROBES = {'java':['-version'], 'javac':['-version'],'mvn':['-version'],
                  'gradle':['--version'], 'node':['--version'], 'npm':['--version'],
                  'flutter':['--version'],'dart':['--version'],'python3':['--version'],
                  'git':['--version'],'psql':['--version'],'docker':['--version'],'adb':['--version'],
                  'maestro':['--version'],'curl':['--version']}


def diagnostics(root, action='toolchain', project='', ports=None):
    try:
        if action == 'toolchain':
            return response('ready', tools={k:_run_version(k,v) for k,v in VERSION_PROBES.items()},
                            note='Discovery only; an installed executable is not evidence that tests passed.')
        if action == 'ports':
            if not isinstance(ports,list) or not 1<=len(ports)<=20 or any(type(p) is not int or not 1<=p<=65535 for p in ports):
                return response('blocked',reason='Provide 1–20 numeric loopback ports')
            result=[]
            for port in ports:
                with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
                    sock.settimeout(.15)
                    listening=(sock.connect_ex(('127.0.0.1',port))==0)
                result.append({'port':port,'loopback_accepting_connections':listening})
            return response('ready', ports=result,note='A responding port does not identify its owning process.')
        if action == 'maintenance':
            return housekeeping(root,action='inspect')
        if action == 'dependencies':
            folder=_path(root,project)
            if not folder.is_dir():return response('blocked',reason='Project must be a directory')
            expected={'Node.js':['package.json','package-lock.json','yarn.lock','pnpm-lock.yaml','node_modules'],
                      'Java Maven':['pom.xml','mvnw','.mvn'],
                      'Java Gradle':['build.gradle','build.gradle.kts','gradlew'],
                      'Flutter':['pubspec.yaml','pubspec.lock','.dart_tool/package_config.json'],
                      'Python':['pyproject.toml','requirements.txt','.venv']}
            found={group:[item for item in keys if (folder/item).exists()] for group,keys in expected.items()}
            guidance=[]
            if (folder/'package.json').is_file() and not (folder/'node_modules').is_dir():
                guidance.append('Node manifest found but local node_modules missing; do not infer installation succeeded')
            if (folder/'pubspec.yaml').is_file() and not (folder/'.dart_tool/package_config.json').is_file():
                guidance.append('Flutter pubspec found but package resolution metadata missing')
            if (folder/'pom.xml').is_file():
                guidance.append('Maven dependencies are resolved only by running a Maven goal; this is presence inspection')
            return response('ready',project=folder.relative_to(Path(root).resolve()).as_posix(),found=found,guidance=guidance)
        return response('blocked',reason='Unknown diagnostics action')
    except (OSError, ValueError) as exc:
        return response('blocked',reason=str(exc)[:320])


def logs(root, relative_path, pattern='', lines=120):
    try:
        f=_safe_read_path(root,relative_path)
        if f.suffix.lower() not in SAFE_LOG_SUFFIXES:
            return response('blocked',reason='Only existing log, TXT, XML, JSON or OUT diagnostics are accepted')
        if type(lines) is not int or not 1<=lines<=300:return response('blocked',reason='lines must be 1–300')
        if not isinstance(pattern,str) or len(pattern)>120:return response('blocked',reason='Invalid search term')
        # Read at most the final 64 KiB, even for huge test logs.
        with f.open('rb') as stream:
            stream.seek(0,os.SEEK_END)
            size=stream.tell();stream.seek(max(0,size-65536))
            text=stream.read(65536).decode('utf-8','replace')
        values=text.splitlines()
        if pattern:
            values=[row for row in values if pattern.casefold() in row.casefold()]
        tail='\n'.join(values[-lines:])
        return response('ready',path=f.relative_to(Path(root).resolve()).as_posix(),
                        lines=_scrub(tail[-MAX_OUTPUT:]),file_bytes=size,
                        truncated=size>65536 or len(values)>lines,
                        note='Potential secret-like values are redacted; only tail of file inspected')
    except (OSError, ValueError) as exc:
        return response('blocked',reason=str(exc)[:320])


def _identity(pid):
    """Linux /proc process identity; starttime prevents PID-reuse kills."""
    if type(pid) is not int or pid<=0:return None
    if not sys.platform.startswith('linux'):
        import psutil
        try:
            process=psutil.Process(pid)
            argv=process.cmdline()
            if not process.is_running() or not argv:return None
            return {'pid':pid,'starttime':str(int(process.create_time()*1000000)),
                    'argv':argv,'cwd':str(Path(process.cwd()).resolve())}
        except (psutil.NoSuchProcess,psutil.AccessDenied,OSError):return None
    folder=Path('/proc')/str(pid)
    try:
        if folder.stat().st_uid != os.getuid():return None
        raw=(folder/'stat').read_text()
        stat_fields=raw.rsplit(')',1)[1].split()
        if stat_fields[0]=='Z':return None
        start=int(stat_fields[19])
        cmd=(folder/'cmdline').read_bytes().split(b'\0')
        argv=[arg.decode('utf-8','replace') for arg in cmd if arg]
        cwd=(folder/'cwd').resolve()
        return {'pid':pid,'starttime':start,'argv':argv,'cwd':str(cwd)}
    except (OSError, ValueError, IndexError):return None


def _preview_dir(root):
    return project_state(root)/'local'/'preview'


def _preview_owner(root):
    owner_file=_preview_dir(root)/'owner.json'
    if not owner_file.is_file() or owner_file.is_symlink():return {}
    try:
        record=json.loads(owner_file.read_text())
        return record if isinstance(record,dict) else {}
    except (ValueError,OSError):return {}


def _owned_preview(root):
    owner=_preview_owner(root)
    identity=_identity(owner.get('pid'))
    if not identity or identity['starttime'] != owner.get('starttime'):
        return None
    argv=identity['argv']
    module=Path(__file__).resolve()
    if (len(argv)!=6 or argv[1]!=str(module) or argv[2]!='--serve-preview'
            or argv[3]!=str((Path(root)/owner.get('directory','')).resolve())
            or argv[4]!=str(owner.get('requested_port',0))
            or argv[5]!=str(_preview_dir(root)/'ready.json')):
        return None
    if owner.get('workspace')!=str(Path(root).resolve()):return None
    return owner


def processes(root, action='list', port=0):
    try:
        if action=='preview_status':
            owner=_owned_preview(root)
            return response('ready',running=bool(owner),preview=owner or None)
        if action!='list':return response('blocked',reason='Unknown process inspection action')
        root=Path(root).resolve();found=[]
        if sys.platform.startswith('linux'):
            pids=[int(p.name) for p in Path('/proc').iterdir() if p.name.isdecimal()]
        else:
            import psutil
            pids=psutil.pids()
        for pid in pids:
            identity=_identity(pid)
            if not identity:continue
            # Only processes with their cwd under the workspace, not arbitrary user processes.
            if not Path(identity['cwd']).is_relative_to(root):continue
            args=identity['argv']
            found.append({'pid':identity['pid'],'command':Path(args[0]).name if args else '(unknown)',
                          'cwd':Path(identity['cwd']).relative_to(root).as_posix(),
                          'starttime':identity['starttime']})
            if len(found)>=100:break
        return response('ready',workspace_processes=found,preview_owned=_owned_preview(root) is not None,
                        truncated=len(found)>=100,note='Read-only snapshot. Processes without workspace cwd are omitted.')
    except (OSError,ValueError) as exc:
        return response('blocked',reason=str(exc)[:320])


def preview(root, action='status', relative_path='', port=0):
    """Start/stop only Gaston-owned loopback STATIC preview, not application code."""
    try:
        root=Path(root).resolve();directory=_preview_dir(root)
        if action=='status':return processes(root,'preview_status')
        if action=='stop':
            owner=_owned_preview(root)
            if not owner:return response('blocked',reason='No provably Gaston-owned preview; nothing stopped')
            pid=owner['pid'];os.kill(pid,signal.SIGTERM)
            for _ in range(30):
                if _identity(pid) is None:
                    child=_PREVIEW_CHILDREN.pop(pid,None)
                    if child:
                        try:child.wait(timeout=1)
                        except (OSError,subprocess.TimeoutExpired):pass
                    (_preview_dir(root)/'owner.json').unlink(missing_ok=True)
                    return response('pass',stopped_pid=pid)
                time.sleep(.1)
            return response('blocked',reason='SIGTERM sent; shutdown not yet confirmed',pid=pid)
        if action!='start':return response('blocked',reason='Unknown preview action')
        if _owned_preview(root):return response('ready',reason='Owned preview already running',preview=_owned_preview(root))
        prior=_preview_owner(root)
        if prior and _identity(prior.get('pid')):
            return response('blocked',reason='An unverified preview owner process exists; refusing a new launch or termination')
        if type(port) is not int or not 0<=port<=65535:return response('blocked',reason='Invalid port')
        selected=_path(root,relative_path)
        if not selected.is_dir() or not (selected/'index.html').is_file():
            return response('blocked',reason='Select a workspace directory containing index.html')
        if '.git' in selected.parts or any(n in selected.relative_to(root).parts for n in ('.agentic','.gaston')):
            return response('blocked',reason='Preview may not serve private toolkit or Git directories')
        directory.mkdir(parents=True,exist_ok=True)
        if not directory.resolve().is_relative_to(root) or directory.is_symlink() or (directory/'owner.json').is_symlink():
            return response('blocked',reason='Unsafe preview state path')
        state=directory/'ready.json';state.unlink(missing_ok=True)
        log=directory/'preview.log'
        with log.open('ab') as f:
            child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve-preview',str(selected),str(port),str(state)],
                                   cwd=root,stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,
                                   **__import__('platform_compat').popen_kwargs())
        owner=None
        for _ in range(45):
            identity=_identity(child.pid)
            if child.poll() is not None:break
            if identity and state.is_file():
                try:
                    ready=json.loads(state.read_text())
                    if ready.get('pid')==child.pid:
                        owner={'pid':child.pid,'starttime':identity['starttime'],'workspace':str(root),
                               'port':ready['port'],'requested_port':port,
                               'directory':selected.relative_to(root).as_posix()}
                        break
                except (OSError,ValueError):pass
            time.sleep(.1)
        if not owner:
            # No ambiguous process kill. If launch is live but untracked, warn.
            return response('blocked',reason='Preview start unverified; inspect preview.log before cleanup',pid=child.pid)
        (directory/'owner.json').write_text(json.dumps(owner,indent=2)+'\n')
        _PREVIEW_CHILDREN[child.pid]=child
        return response('pass',preview=owner,url=f'http://127.0.0.1:{owner["port"]}/',
                        note='Static preview on loopback only; no application build or production access')
    except (OSError,ValueError) as exc:return response('blocked',reason=str(exc)[:320])


def _serve_preview(directory,port,state_file):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import unquote,urlsplit
    import mimetypes
    directory=Path(directory).resolve()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed=urlsplit(self.path)
            path=unquote(parsed.path)
            parts=Path(path.lstrip('/')).parts
            if any(seg.startswith('.') or seg in ('.git','.agentic') for seg in parts):
                self.send_error(403);return
            candidate=(directory.joinpath(*parts)).resolve()
            if candidate.is_dir():candidate=candidate/'index.html'
            if not candidate.is_relative_to(directory) or not candidate.is_file() or candidate.suffix.lower() not in STATIC_SUFFIXES:
                self.send_error(404);return
            if candidate.stat().st_size>8*1024*1024:self.send_error(413);return
            data=candidate.read_bytes()
            self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers()
            self.wfile.write(data)
        def log_message(self,fmt,*args):return
    with ThreadingHTTPServer(('127.0.0.1',int(port)),Handler) as server:
        Path(state_file).write_text(json.dumps({'pid':os.getpid(),'port':server.server_address[1]}))
        server.serve_forever(poll_interval=.1)


def housekeeping(root, action='inspect', apply=False, requested_by_user=''):
    """Only old known-temp files, never entire directories or test caches."""
    try:
        base=project_state(root)/'local'
        if action not in ('inspect','cleanup') or (action=='cleanup' and (apply is not True or not isinstance(requested_by_user,str) or not requested_by_user.strip())):
            return response('blocked',reason='Choose inspect, or cleanup with explicit user request and apply=true')
        if not base.is_dir():return response('ready',eligible=[],removed=[])
        old=time.time()-3600
        candidates=[]
        for item in base.glob('.sessions-*.tmp'):
            if item.is_file() and not item.is_symlink() and item.stat().st_mtime<old:
                candidates.append(item)
        preview_base=base/'preview'
        if preview_base.is_dir() and not preview_base.is_symlink() and _owned_preview(root) is None:
            for name in ('ready.json','preview.log'):
                f=preview_base/name
                if f.is_file() and not f.is_symlink() and f.stat().st_mtime<old:
                    candidates.append(f)
        candidate_names=[str(c.relative_to(Path(root).resolve())) for c in candidates[:60]]
        if action=='inspect':return response('ready',eligible=candidate_names,eligible_count=len(candidates))
        removed=[]
        for candidate in candidates[:60]:
            # Symlink swaps cannot be defeated entirely without openat; never follow a link.
            if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_mtime>=old:continue
            candidate.unlink();removed.append(candidate.relative_to(Path(root).resolve()).as_posix())
        return response('pass',removed=removed,note='Only stale explicitly allowlisted temporary files removed; no project code or cache deleted')
    except (OSError,ValueError) as exc:
        return response('blocked',reason=str(exc)[:320])


def note(root, name, content, task_id='', mode='append'):
    try:
        if not isinstance(content,str) or not content.strip():
            return response('blocked',reason='Provide nonempty operational note text')
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{1,64}\.md',name):
            return response('blocked',reason='Use an ordinary .md filename with no directory component')
        if mode not in ('append','create'):return response('blocked',reason='Only append or create mode is allowed')
        root=Path(root).resolve()
        if task_id:
            if not re.fullmatch(r'[a-z0-9-]{8,120}',task_id):
                return response('blocked',reason='Invalid task ID')
            task=project_state(root)/'runs'/task_id
            if not (task/'task.json').is_file() or task.is_symlink():
                return response('blocked',reason='Unknown Builder task')
            dest=task/'lead-notes'
        else:dest=project_state(root)/'local'/'lead-notes'
        dest.mkdir(parents=True,exist_ok=True)
        if not dest.resolve().is_relative_to(root) or dest.is_symlink():
            return response('blocked',reason='Unsafe notes directory')
        path=dest/name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return response('blocked',reason='Unsafe note file')
        if mode=='create' and path.exists():return response('blocked',reason='Note exists; choose append explicitly')
        # Avoid surfacing secrets from diagnostic logs in notes; redact common patterns.
        with path.open('a' if mode=='append' else 'x',encoding='utf-8') as f:
            f.write(_scrub(content,limit=None)+'\n')
        return response('pass',path=path.relative_to(root).as_posix(),mode=mode,
                        note='Operational note only; application sources and repository docs untouched')
    except (OSError,ValueError) as exc:
        return response('blocked',reason=str(exc)[:320])



def document(root, relative_path, action, text='', old_text='', requested_by_user=''):
    """Complete explicit documentation edits, never application source or agent policy."""
    try:
        root=Path(root).resolve()
        if action not in ('create','append','replace_exact'):
            return response('blocked',reason='Choose create, append or replace_exact')
        if not isinstance(requested_by_user,str) or not requested_by_user.strip():
            return response('blocked',reason='Direct documentation modifications require an explicit user request')
        if not isinstance(text,str) or (action!='replace_exact' and not text):
            return response('blocked',reason='Provide document text')
        if not isinstance(relative_path,str) or len(relative_path)>450:
            return response('blocked',reason='Invalid relative document path')
        rel=Path(relative_path)
        if (rel.is_absolute() or '..' in rel.parts or 'docs' not in rel.parts or
            any(part.startswith('.') for part in rel.parts) or any(part in SKIP_DIRS for part in rel.parts) or
            rel.suffix.lower() not in ('.md','.txt') or rel.name.upper() in ('AGENTS.MD','CLAUDE.MD')):
            return response('blocked',reason='Only .md/.txt files in a workspace docs/ subtree may be edited')
        # Reject symlinks in any original document path component, not just escapes.
        for n in range(1,len(rel.parts)+1):
            if (root.joinpath(*rel.parts[:n])).is_symlink():
                return response('blocked',reason='Symlink document paths are not writable')
        target=_path(root,relative_path,required=False)
        if target.exists() and (not target.is_file() or target.is_symlink()):
            return response('blocked',reason='Unsafe document target')
        if action!='create' and not target.is_file():
            return response('blocked',reason='Document must already exist')
        if action=='create' and target.exists():
            return response('blocked',reason='Document already exists')
        if action=='replace_exact':
            if not isinstance(old_text,str) or not old_text:
                return response('blocked',reason='Provide exact nonempty old_text')
            source=target.read_text(encoding='utf-8')
            if source.count(old_text)!=1:
                return response('blocked',reason='Replacement must match exactly once in the document')
            updated=source.replace(old_text,text,1)
        elif action=='append':
            source=target.read_text(encoding='utf-8')
            updated=source.rstrip('\n')+'\n\n'+text+'\n'
        else:
            updated=text+'\n'
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.parent.resolve().is_relative_to(root):
            return response('blocked',reason='Document path escapes workspace')
        # atomic replacement for existing files; no general shell or app modification.
        if action=='create':
            with target.open('x',encoding='utf-8') as dest:dest.write(updated)
        else:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=target.parent,
                                             prefix='.gaston-doc-',suffix='.tmp',delete=False) as temp:
                temp.write(updated);tmp=Path(temp.name)
            os.replace(tmp,target)
        return response('pass',path=target.relative_to(root).as_posix(),action=action,
                        note='Only requested documentation edited; no application code or agent policy touched')
    except (OSError,ValueError,UnicodeError) as exc:
        return response('blocked',reason=str(exc)[:320])


DB_METADATA_ACTIONS = ('connectivity','tables','columns','migrations','constraints','indexes','table_stats')
DB_FIXTURE_ACTIONS = ('count','duplicates','sample')
DB_ACTIONS = ('status',) + DB_METADATA_ACTIONS + DB_FIXTURE_ACTIONS


def database(root, action='connectivity', schema='public', table='', columns=None, limit=20):
    """Bounded local PostgreSQL diagnostics. No arbitrary SQL or application writes."""
    from urllib.parse import unquote
    url=os.environ.get('GASTON_READONLY_DATABASE_URL','')
    managed=None
    if not url:
        import development_db
        try:
            managed=development_db.probe(root)
            if managed.get('status')=='ready':url=development_db.readonly_url(root)
        except (OSError,ValueError,TypeError):managed={'status':'blocked'}
    allow_fixture=os.environ.get('GASTON_DATABASE_ALLOW_FIXTURE_DATA')=='1'
    executable=shutil.which('psql')
    if action=='status':
        return response('ready',psql_installed=bool(executable),docker_cli_installed=bool(shutil.which('docker')),
                        readonly_url_configured=bool(url),fixture_data_enabled=allow_fixture,managed_development_status=(managed or {}).get('status'),
                        note='Discovery only: use connectivity to test the configured connection. Docker CLI presence does not prove a running database; pgAdmin is optional.')
    if action not in DB_ACTIONS:
        return response('blocked',reason='Only approved read-only PostgreSQL diagnostics are supported')
    if not url:
        return response('blocked',reason='Set GASTON_READONLY_DATABASE_URL for an authorized local development/test PostgreSQL role')
    try:
        parsed=urlparse(url)
        if parsed.query or parsed.fragment or parsed.params or parsed.scheme not in ('postgres','postgresql') or parsed.hostname not in ('127.0.0.1','localhost','::1'):
            return response('blocked',reason='Only loopback PostgreSQL connections are allowed; no remote or production hosts')
        port=parsed.port if parsed.port is not None else 5432
        user=unquote(parsed.username or '')
        db=unquote(parsed.path[1:]) if parsed.path.startswith('/') else ''
        password=unquote(parsed.password or '')
        # Prevent decoded dbname from being interpreted as a libpq URI/conninfo string.
        if not all(re.fullmatch(r'[A-Za-z0-9_.-]{1,63}',v) for v in (user,db)) or not 1<=port<=65535 or '\0' in password:
            raise ValueError('Invalid local connection parameters')
    except (ValueError, UnicodeError):
        return response('blocked',reason='Invalid local PostgreSQL URL, user, database or port')
    if not executable:
        return response('blocked',reason='PostgreSQL psql client not installed; install the host client with developer approval (pgAdmin is not required)')
    def identifier(value):
        return isinstance(value,str) and bool(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',value))
    if not identifier(schema) or (table!='' and not identifier(table)):
        return response('blocked',reason='Invalid schema or table name (use exact simple PostgreSQL identifiers)')
    needs_table=('columns','constraints','indexes','table_stats')+DB_FIXTURE_ACTIONS
    if action in needs_table and not table:
        return response('blocked',reason='Choose an exact table for this diagnostic')
    if type(limit) is not int or not 1<=limit<=50:
        return response('blocked',reason='limit must be an integer from 1 to 50')
    if columns is None: columns=[]
    if not isinstance(columns,list) or len(columns)>8 or any(not identifier(c) for c in columns) or len(set(columns))!=len(columns):
        return response('blocked',reason='Provide at most eight unique, exact column identifiers')
    if action in ('duplicates','sample') and not columns:
        return response('blocked',reason='Choose explicit columns for duplicates or sample (no wildcard)')
    if columns and action not in ('duplicates','sample'):
        return response('blocked',reason='columns is only supported for duplicates and sample')
    if action in DB_FIXTURE_ACTIONS and not allow_fixture:
        return response('blocked',reason='Data diagnostics require GASTON_DATABASE_ALLOW_FIXTURE_DATA=1 set by the developer for a synthetic fixture database; metadata is still available')
    if action=='sample' and any(re.search(r'password|passwd|secret|token|api_?key|private_?key|credential|authorization',c,re.I) for c in columns):
        return response('blocked',reason='Credential-like columns are not eligible for sample output')

    # Fixed SQL templates; strings/identifiers contain only strictly validated ASCII.
    qualified=f'"{schema}"."{table}"'
    condition=f"table_schema='{schema}' AND table_name='{table}'"
    group=', '.join(f'"{c}"' for c in columns)
    sample=', '.join(f"'{c}', pg_catalog.left(\"{c}\"::text, 160)" for c in columns)
    query={
        'connectivity':"SELECT current_database(), current_user, current_setting('transaction_read_only');",
        'tables':f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' AND table_type='BASE TABLE' ORDER BY table_name LIMIT 100;",
        'columns':f"SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE {condition} ORDER BY ordinal_position LIMIT 150;",
        'migrations':f"SELECT table_name FROM information_schema.tables WHERE table_schema='{schema}' AND (table_name LIKE '%flyway%' OR table_name LIKE '%changelog%' OR table_name LIKE '%migration%') ORDER BY table_name LIMIT 30;",
        'constraints':f"SELECT con.conname, con.contype, pg_catalog.pg_get_constraintdef(con.oid) FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class c ON c.oid=con.conrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='{schema}' AND c.relname='{table}' ORDER BY con.conname LIMIT 100;",
        'indexes':f"SELECT indexname, indexdef FROM pg_catalog.pg_indexes WHERE schemaname='{schema}' AND tablename='{table}' ORDER BY indexname LIMIT 100;",
        'table_stats':f"SELECT n_live_tup AS estimated_live_rows, n_dead_tup AS estimated_dead_rows, seq_scan, idx_scan FROM pg_catalog.pg_stat_user_tables WHERE schemaname='{schema}' AND relname='{table}';",
        'count':f'SELECT count(*) AS visible_rows FROM {qualified};',
        'duplicates':f'SELECT count(*) AS duplicate_groups, COALESCE(sum(n-1),0) AS extra_rows FROM (SELECT count(*) AS n FROM {qualified} GROUP BY {group} HAVING count(*)>1) AS duplicate_sets;',
        'sample':f'SELECT pg_catalog.json_build_object({sample})::text FROM {qualified} LIMIT {limit};',
    }[action]
    # Do not inherit libpq routing, credentials, service files, or startup options.
    env={k:v for k,v in os.environ.items() if not k.upper().startswith('PG') and k not in ('GASTON_READONLY_DATABASE_URL','GASTON_DATABASE_ALLOW_FIXTURE_DATA')}
    host='::1' if parsed.hostname=='::1' else '127.0.0.1'
    env.update(PGHOST=host,PGHOSTADDR=host,PGPORT=str(port),PGUSER=user,PGDATABASE=db,
               PGPASSWORD=password,PGPASSFILE=os.devnull,PGCONNECT_TIMEOUT='3',
               PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=1000 -c search_path=pg_catalog')
    def run(sql):
        return subprocess.run([executable,'-X','--no-psqlrc','-w','-q','-A','-t','-F',' | ','-v','ON_ERROR_STOP=1','-c','BEGIN READ ONLY;','-c',sql,'-c','ROLLBACK;'],
                              cwd=Path(root).resolve(),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              text=True,timeout=9,check=False,stdin=subprocess.DEVNULL)
    try:
        if action in DB_FIXTURE_ACTIONS:
            # Refuse views, foreign tables, and partition/inheritance trees containing foreign relations.
            # Project databases must still be trusted, synthetic fixtures (loopback is not provenance).
            guard=run(f"WITH RECURSIVE tree(oid,kind) AS (SELECT c.oid,c.relkind FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='{schema}' AND c.relname='{table}' UNION SELECT c.oid,c.relkind FROM tree t JOIN pg_catalog.pg_inherits i ON i.inhparent=t.oid JOIN pg_catalog.pg_class c ON c.oid=i.inhrelid) SELECT COALESCE(bool_and(kind IN ('r','p')),false) FROM tree;")
            if guard.returncode or guard.stdout.strip()!='t':
                return response('blocked',reason='Data diagnostics require an existing ordinary or partitioned local table without foreign descendants')
        result=run(query)
        if result.returncode:
            # PostgreSQL errors can contain credentials or row values. Do not echo stderr.
            return response('blocked',reason='Local PostgreSQL diagnostic query failed (check connection, SELECT privileges, exact identifiers and timeout)',exit_code=result.returncode)
        if action=='connectivity' and (not result.stdout.strip() or result.stdout.strip().rsplit(' | ',1)[-1]!='on'):
            return response('blocked',reason='Could not confirm read-only transaction setting')
        labels={'connectivity':['database','user','transaction_read_only'],'tables':['table_name'],
                'columns':['column_name','data_type','is_nullable'],'migrations':['table_name'],
                'constraints':['name','type','definition'],'indexes':['name','definition'],
                'table_stats':['estimated_live_rows','estimated_dead_rows','seq_scan','idx_scan'],
                'count':['visible_rows'],'duplicates':['duplicate_groups','extra_rows'],'sample':columns}
        notes={'status':'Discovery only','table_stats':'Statistics are estimates and may be stale, not an exact count.',
               'count':'Exact count of rows visible to this role at query time (including row-level security).',
               'duplicates':'Groups with more than one visible row; NULL values group together. No duplicate values returned.',
               'sample':'Unordered synthetic fixture sample only. Values are text or null; each non-null value is capped at 160 characters.',
               'migrations':'Migration-table discovery only, not proof migrations ran successfully.'}
        safe=_scrub(result.stdout,limit=None)
        return response('ready',action=action,rows=safe[:MAX_OUTPUT],row_count=len(result.stdout.splitlines()),
                        output_truncated=len(safe)>MAX_OUTPUT,read_only=True,fields=labels[action],
                        note=notes.get(action,'Metadata inspection only; no data or migrations modified.')+' Diagnostic evidence, not an acceptance test.')
    except (OSError,UnicodeError,subprocess.TimeoutExpired) as exc:
        return response('blocked',reason='Local database inspection unavailable',details=type(exc).__name__)

if __name__=='__main__' and len(sys.argv)==5 and sys.argv[1]=='--serve-preview':
    _serve_preview(sys.argv[2],sys.argv[3],sys.argv[4])
