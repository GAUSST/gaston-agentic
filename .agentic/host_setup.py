"""Consent-gated host installation and isolated PostgreSQL prerequisite smoke test.

Only known distribution packages are offered. No shell interpolation, remote
Docker daemon, application credentials, global PATH edits or privilege grants.
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import tempfile

IMAGE = 'docker.io/library/postgres:16'
LABEL = 'com.gaston.setup-smoke'
PACKAGES = {
    'apt': {'git':'git', 'java':'openjdk-17-jdk', 'curl':'curl',
            'psql':'postgresql-client', 'docker':'docker.io', 'maven':'maven',
            'gradle':'gradle', 'node':'nodejs', 'npm':'npm'},
    'dnf': {'git':'git', 'java':'java-17-openjdk-devel', 'curl':'curl',
            'psql':'postgresql', 'maven':'maven', 'gradle':'gradle',
            'node':'nodejs', 'npm':'npm'},
    'brew': {'git':'git', 'java':'openjdk@17', 'curl':'curl',
             'psql':'libpq', 'maven':'maven', 'gradle':'gradle',
             'node':'node@22', 'npm':'node@22'},
    'winget': {'git':'Git.Git','java':'EclipseAdoptium.Temurin.17.JDK',
               'docker':'Docker.DockerDesktop','maven':'Apache.Maven','gradle':'Gradle.Gradle',
               'node':'OpenJS.NodeJS.LTS','npm':'OpenJS.NodeJS.LTS'},
}


def is_wsl():
    return platform.system() == 'Linux' and ('microsoft' in platform.release().lower() or bool(os.environ.get('WSL_DISTRO_NAME')))


def host_description():
    system = platform.system()
    if is_wsl():
        return 'Windows Subsystem for Linux (WSL)'
    if system == 'Darwin':
        return 'macOS (' + platform.machine() + ')'
    if system == 'Linux':
        try:
            return platform.freedesktop_os_release().get('PRETTY_NAME', 'Linux')
        except (AttributeError, OSError):
            return 'Linux'
    return system


def refresh_tool_path():
    """Use installed Homebrew keg-only tools without editing shell profiles.

    Called at launch and after an approved install; existing PATH takes priority.
    """
    if platform.system() == 'Windows':
        refresh_windows_path()
        return
    if platform.system() != 'Darwin':
        return
    roots = [Path('/opt/homebrew'), Path('/usr/local')]
    brew = shutil.which('brew')
    if brew:
        result = run([brew, '--prefix'], timeout=5)
        if result and result.returncode == 0:
            candidate = Path((result.stdout or '').strip())
            if candidate.is_absolute() and candidate != Path('/'):
                roots.insert(0, candidate)
    paths = os.environ.get('PATH', '').split(os.pathsep)
    bundled = Path('/Applications/Docker.app/Contents/Resources/bin')
    if bundled.is_dir() and str(bundled) not in paths: paths.append(str(bundled))
    for base in roots:
        for candidate in [base/'bin', *(base/'opt'/name/'bin' for name in ('libpq','openjdk@17','node@22'))]:
            if candidate.is_dir() and str(candidate) not in paths:
                paths.append(str(candidate))
    os.environ['PATH'] = os.pathsep.join(paths)


def manual_guidance(names):
    if platform.system() == 'Windows':
        hints = []
        if 'psql' in names: hints.append('Install only Command Line Tools from https://www.postgresql.org/download/windows/ (deselect server, pgAdmin and Stack Builder). Restart the terminal if needed.')
        if 'docker' in names: hints.append('Install/start Docker Desktop for Windows in Linux-container mode.')
        if 'curl' in names: hints.append('Windows 11 includes curl.exe; repair Windows or its PATH if missing.')
        return ' '.join(hints)
    if 'docker' not in names:
        return ''
    if is_wsl():
        return 'Start Docker Desktop on Windows and enable WSL integration for this distribution (or configure a local Linux Docker engine explicitly).'
    if platform.system() == 'Darwin':
        return 'Install/start Docker Desktop or a compatible local Unix-socket Docker engine; Docker Desktop installation requires separate approval.'
    return 'Install/start a supported local Docker engine using your distribution instructions; remote engines are not used.'


def manager():
    if platform.system() == 'Windows':
        return 'winget' if shutil.which('winget') else None
    if platform.system() == 'Darwin' and shutil.which('brew'):
        return 'brew'
    if platform.system() != 'Linux':
        return None
    try:
        os_release = platform.freedesktop_os_release()
    except (AttributeError, OSError):
        # Python 3.10 supports this API; no guess on unknown hosts.
        return None
    ids = {os_release.get('ID', ''), *os_release.get('ID_LIKE', '').split()}
    if ids & {'debian', 'ubuntu'} and shutil.which('apt-get'):
        return 'apt'
    if ids & {'fedora', 'rhel', 'centos'} and shutil.which('dnf'):
        return 'dnf'
    return None


def plan(missing, *, package_manager=None):
    pm = package_manager or manager()
    if pm not in PACKAGES:
        return [], list(dict.fromkeys(missing))
    names = list(dict.fromkeys(missing))
    mapping = PACKAGES[pm]
    if is_wsl():
        mapping = {key: value for key, value in mapping.items() if key != 'docker'}
    manual = [key for key in names if key not in mapping]
    packages = list(dict.fromkeys(mapping[key] for key in names if key in mapping))
    if not packages:
        return [], manual
    if pm == 'winget':
        return [['winget','install','--source','winget','--exact','--id',pkg] for pkg in packages], manual
    if pm == 'brew':
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            return [], names
        return [['brew', 'install', *packages]], manual
    prefix = [] if hasattr(os, 'geteuid') and os.geteuid() == 0 else ['sudo']
    if prefix and not shutil.which('sudo'):
        return [], names
    if pm == 'apt':
        return [prefix+['apt-get','update'], prefix+['apt-get','install','--no-remove','-y',*packages]], manual
    return [prefix+['dnf','install','-y',*packages]], manual


def install(missing, *, interactive, ask, lang='en'):
    """Return attempt outcome. Final readiness is always independently re-probed."""
    names = list(dict.fromkeys(missing))
    if not names:
        return 'not_needed'
    if not interactive or not sys.stdin.isatty():
        return 'not_attempted'
    french = lang == 'fr'
    commands, manual = plan(names)
    if manual:
        print('    '+('Installation manuelle nécessaire : ' if french else 'Manual installation needed: ')+', '.join(manual))
    if manual:
        guidance = manual_guidance(manual)
        if guidance: print('    '+guidance)
    if not commands:
        return 'unsupported'
    print('    '+('Paquets système proposés : ' if french else 'Proposed system package installation: ')+', '.join(n for n in names if n not in manual))
    for cmd in commands:
        print('      '+shlex.join(cmd))
    windows=commands[0][0]=='winget'
    question=('Installer les paquets manquants (Windows peut demander une autorisation administrateur) ?' if french else 'Install the missing packages (Windows may request administrator approval)?') if windows else ('Autoriser ces commandes d’installation système (mot de passe sudo si nécessaire) ?' if french else 'Install the missing system packages using these commands (sudo may prompt for your password)?')
    if not ask(question,
               default=True, interactive=True, lang=lang):
        return 'declined'
    # Inherit the controlling terminal for sudo/password and package manager
    # output. No detached process group or captured passwords. Debian's package
    # transaction must not be forcibly killed by a setup timeout.
    for cmd in commands:
        try:
            if subprocess.run(cmd, check=False).returncode != 0:
                print('    '+('Installation échouée ; vérification de l’état réel.' if french else
                              'Installation failed; checking actual tool readiness.'))
                return 'failed'
        except OSError as exc:
            print(f'    {exc}')
            return 'failed'
    if commands[0][0] in ('brew','winget'):
        refresh_tool_path()
    if commands[0][0] == 'brew' and set(names) & {'psql','java','node','npm'}:
        print('    '+('Si un outil reste absent, ajoutez le dossier bin de la formule Homebrew à votre PATH puis relancez init.' if french else
                      'If a keg-only tool is still missing, add its Homebrew formula bin directory to PATH and rerun init.'))
    return 'installed'


def offer_docker_start(*, interactive, ask, lang='en'):
    """Offer starting a known installed but inactive system Docker service.

    Never enable at boot, change group memberships/socket modes, use sudo for
    Docker itself, or start a daemon when a custom host/context was selected.
    """
    if not interactive or not sys.stdin.isatty():
        return False
    if platform.system() in ('Darwin','Windows'):
        return offer_desktop_start(ask=ask,lang=lang)
    if platform.system() != 'Linux' or is_wsl():
        return False
    if not shutil.which('docker') or not shutil.which('systemctl') or os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT'):
        return False
    if local_docker():
        return False  # Existing socket may be denied; starting won't fix access.
    context = run(['docker','context','show'],timeout=5)
    if not context or context.returncode or (context.stdout or '').strip() != 'default':
        return False
    state = run(['systemctl','show','docker.service','--property=LoadState,ActiveState'],timeout=8)
    if not state or state.returncode or 'LoadState=loaded' not in (state.stdout or '').splitlines() or 'ActiveState=inactive' not in (state.stdout or '').splitlines():
        return False
    prefix = [] if hasattr(os,'geteuid') and os.geteuid() == 0 else ['sudo']
    if prefix and not shutil.which('sudo'):
        return False
    command = prefix+['systemctl','start','docker.service']
    print('      '+shlex.join(command))
    if not ask('Démarrer le service Docker local maintenant ?' if lang=='fr' else 'Start the installed local Docker service now?',default=True,interactive=True,lang=lang):
        return False
    try:
        return subprocess.run(command,check=False).returncode == 0
    except OSError as exc:
        print(f'    {exc}')
        return False


def psql_ready():
    if not shutil.which('psql'):
        return False
    result = run(['psql','--version'], timeout=8)
    return bool(result and result.returncode == 0 and re.search(r'\bpsql\b',result.stdout or '',re.I))


def run(argv, *, timeout=15, env=None, input_text=None):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=env, **({'input':input_text} if input_text is not None else {}))
    except (OSError, subprocess.TimeoutExpired):
        return None


def local_docker():
    """Managed verification uses local Unix sockets or allowlisted Windows named pipes only."""
    if not shutil.which('docker'):
        return None
    host = os.environ.get('DOCKER_HOST')
    context = os.environ.get('DOCKER_CONTEXT')
    if context:
        result = run(['docker','context','inspect',context,'--format','{{.Endpoints.docker.Host}}'],timeout=5)
        if not result or result.returncode != 0:
            return None
        host = (result.stdout or '').strip()
        if not host:
            return None
    if platform.system() == 'Windows':
        allowed = ('npipe:////./pipe/docker_engine','npipe:////./pipe/dockerDesktopLinuxEngine')
        if not host:
            result = run(['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],timeout=5)
            host = (result.stdout or '').strip() if result and result.returncode == 0 else ''
        return ['docker','--host',host] if host in allowed else None
    if host:
        if not host.startswith('unix:///'):
            return None
        candidates = [Path(host.removeprefix('unix://'))]
    else:
        candidates = []
        if os.environ.get('XDG_RUNTIME_DIR'):
            candidates.append(Path(os.environ['XDG_RUNTIME_DIR'])/'docker.sock')
        candidates.extend([Path('/var/run/docker.sock'), Path.home()/'.docker/run/docker.sock', Path.home()/'.docker/desktop/docker.sock', Path.home()/'.colima/default/docker.sock'])
    for path in candidates:
        if path.is_socket():
            return ['docker','--host','unix://'+str(path)]
    return None


def docker_run(prefix, args, *, timeout=15, input_text=None):
    # Explicit host plus clean Docker overrides. Do not pass application secrets
    # to the container (only explicit --env values are passed).
    env = {k:v for k,v in os.environ.items() if k not in {'DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH','DOCKER_TLS'}}
    return run([*prefix,*args],timeout=timeout,env=env,**({'input_text':input_text} if input_text is not None else {}))


def daemon_id(prefix):
    if not prefix:
        return None
    result = docker_run(prefix,['info','--format','{{.ID}}'],timeout=8)
    value = (result.stdout or '').strip() if result and result.returncode == 0 else ''
    return value or None


def image_id(prefix):
    result = docker_run(prefix,['image','inspect','--format','{{.Id}}',IMAGE],timeout=8)
    value = (result.stdout or '').strip() if result and result.returncode == 0 else ''
    return value if re.fullmatch(r'sha256:[a-f0-9]{64}',value) else None


def fingerprint(prefix):
    engine = daemon_id(prefix)
    image = image_id(prefix) if engine else None
    return {'daemon':engine,'image':image} if engine and image else None


def postgres_smoke(prefix, *, poll_seconds=60):
    """Start official disposable PostgreSQL, execute SELECT 1, confirm cleanup.

    No published port, mounts, application config or credentials. TCP readiness
    waits for the final server, not the entrypoint's temporary init server.
    A unique name/label scopes cleanup even if docker run times out.
    """
    token = uuid.uuid4().hex
    name = 'gaston-pg-smoke-'+token
    result = {'status':'failed','container':name,'cleanup_confirmed':False}
    try:
        created = docker_run(prefix,['run','--detach','--rm','--pull=missing','--name',name,
            '--label',f'{LABEL}={token}','--network','none','--env','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE],timeout=300)
        if not created or created.returncode != 0:
            result['reason'] = 'Docker run failed or timed out (image download or container startup).'
            return result
        deadline = time.monotonic()+poll_seconds
        while time.monotonic() < deadline:
            ready = docker_run(prefix,['exec',name,'pg_isready','-h','127.0.0.1','-U','postgres'],timeout=8)
            if ready and ready.returncode == 0:
                query = docker_run(prefix,['exec',name,'psql','-X','-h','127.0.0.1','-U','postgres','-d','postgres','-At','-v','ON_ERROR_STOP=1','-c','SELECT 1'],timeout=10)
                if query and query.returncode == 0 and (query.stdout or '').strip() == '1':
                    result['status'] = 'pass'
                    result['reason'] = 'Disposable PostgreSQL executed SELECT 1.'
                    return result
            time.sleep(1)
        result['reason'] = 'PostgreSQL readiness or SQL query failed.'
        return result
    finally:
        # A Docker CLI timeout can leave a container behind. Check the exact
        # label before removing; never remove an unrelated container.
        inspected = docker_run(prefix,['container','inspect','--format',f'{{{{index .Config.Labels "{LABEL}"}}}}',name],timeout=8)
        if inspected and inspected.returncode == 0 and (inspected.stdout or '').strip() == token:
            docker_run(prefix,['rm','-f','-v',name],timeout=20)
        listed = docker_run(prefix,['container','ls','-a','--filter',f'name=^/?{name}$','--format','{{.Names}}'],timeout=8)
        result['cleanup_confirmed'] = bool(listed and listed.returncode == 0 and not (listed.stdout or '').strip())
        if not result['cleanup_confirmed']:
            result['status'] = 'failed'
            result['reason'] = f'Cleanup unconfirmed. Inspect only Gaston container {name} before retrying.'


PGADMIN_URL = 'https://www.pgadmin.org/download/'


def refresh_windows_path():
    """Discover machine/user PATH changes in this process, not shell profiles."""
    if platform.system() != 'Windows':
        return
    try:
        import winreg
    except ImportError:
        return
    paths = os.environ.get('PATH','').split(os.pathsep)
    for hive,key in ((winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
                     (winreg.HKEY_CURRENT_USER,r'Environment')):
        try:
            with winreg.OpenKey(hive,key) as reg:
                value,_ = winreg.QueryValueEx(reg,'Path')
            paths.extend(os.path.expandvars(value).split(os.pathsep))
        except OSError:
            pass
    for var in ('ProgramFiles','ProgramW6432'):
        if os.environ.get(var):
            base=Path(os.environ[var])
            paths.extend(str(p.parent) for p in sorted((base/'PostgreSQL').glob('*/bin/psql.exe'),reverse=True))
            paths.extend(str(p) for p in (base/'Docker/Docker/resources/bin',base/'Git/cmd',base/'nodejs') if p.is_dir())
    if os.environ.get('APPDATA'):
        paths.append(str(Path(os.environ['APPDATA'])/'npm'))
    os.environ['PATH']=os.pathsep.join(dict.fromkeys(p for p in paths if p and Path(p).is_absolute()))


def docker_desktop_app():
    system=platform.system()
    if system=='Darwin':
        p=Path('/Applications/Docker.app')
        return p if p.is_dir() else None
    if system=='Windows' and os.environ.get('ProgramFiles'):
        p=Path(os.environ['ProgramFiles'])/'Docker/Docker/Docker Desktop.exe'
        return p if p.is_file() else None
    return None


def offer_desktop_start(*,ask,lang='en'):
    if daemon_id(local_docker()) or os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT'):
        return False
    app=docker_desktop_app()
    if not app:return False
    cmd=['open','-a',str(app)] if platform.system()=='Darwin' else [str(app)]
    print('      '+shlex.join(cmd))
    if not ask('Démarrer Docker Desktop local ?' if lang=='fr' else 'Start local Docker Desktop now?',default=True,interactive=True,lang=lang):
        return False
    try:
        subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except OSError:
        return False
    for _ in range(15):
        if daemon_id(local_docker()):return True
        time.sleep(2)
    return False


def pgadmin_ready():
    system=platform.system()
    if system=='Darwin':
        return any((base/'pgAdmin 4.app').is_dir() for base in (Path('/Applications'),Path.home()/'Applications'))
    if system=='Windows':
        for var in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):
            if os.environ.get(var):
                base=Path(os.environ[var])
                for pattern in ('pgAdmin 4/*/runtime/pgAdmin4.exe','pgAdmin 4/runtime/pgAdmin4.exe','Programs/pgAdmin 4/*/runtime/pgAdmin4.exe','PostgreSQL/*/pgAdmin 4/runtime/pgAdmin4.exe'):
                    if any(base.glob(pattern)):return True
        if shutil.which('winget'):
            result=run(['winget','list','--id','PostgreSQL.pgAdmin','--exact','--source','winget','--disable-interactivity'],timeout=15)
            return bool(result and not result.returncode and 'PostgreSQL.pgAdmin' in (result.stdout or ''))
        return False
    if shutil.which('pgadmin4') or Path('/usr/pgadmin4/bin/pgadmin4').is_file():return True
    if shutil.which('dpkg-query'):
        result=run(['dpkg-query','-W','-f=${Status}','pgadmin4-desktop'],timeout=5)
        if result and result.returncode==0 and (result.stdout or '').strip()=='install ok installed':return True
    if shutil.which('snap'):
        result=run(['snap','list','pgadmin4'],timeout=5)
        if result and result.returncode==0:return True
    return False


def pgadmin_plan():
    pm=manager()
    if pm=='brew':
        if hasattr(os,'geteuid') and os.geteuid()==0:return []
        return [['brew','install','--cask','pgadmin4']]
    if pm=='winget':return [['winget','install','--source','winget','--exact','--id','PostgreSQL.pgAdmin']]
    if pm=='apt' and not is_wsl():
        candidate=run(['apt-cache','policy','pgadmin4-desktop'],timeout=5)
        if candidate and candidate.returncode==0 and re.search(r'^\s*Candidate:\s+(?!\(none\))\S+',candidate.stdout or '',re.M):
            return plan_pgadmin_apt()
        if pgadmin_apt_source_present(): return plan_pgadmin_apt()
    return []


def plan_pgadmin_apt():
    prefix=[] if hasattr(os,'geteuid') and os.geteuid()==0 else ['sudo']
    if prefix and not shutil.which('sudo'):return []
    return [prefix+['apt-get','update'],prefix+['apt-get','install','--no-remove','-y','pgadmin4-desktop']]


def offer_pgadmin(*,interactive,ask,lang='en'):
    """Optional GUI; no account or database registration. New APT source needs explicit consent."""
    if pgadmin_ready():return 'ready'
    if not interactive or not sys.stdin.isatty():return 'skipped'
    commands=pgadmin_plan()
    repo=pgadmin_repo_spec() if not commands else None
    if repo:
        print('      Official pgAdmin APT source: '+repo['source'].strip())
        print('      Signing key: '+PGADMIN_KEY_URL+' -> '+str(repo['key_path']))
        print('      New system source: '+str(repo['source_path']))
        for command in plan_pgadmin_apt(): print('      '+shlex.join(command))
    elif commands:
        for command in commands:print('      '+shlex.join(command))
    else:
        print('      '+PGADMIN_URL)
    if not ask('Installer pgAdmin pour explorer visuellement les bases (facultatif) ?' if lang=='fr' else 'Install pgAdmin for visual database exploration (optional)?',default=False,interactive=True,lang=lang):
        return 'declined'
    if repo:
        if not install_pgadmin_repo(repo):return 'failed'
        commands=plan_pgadmin_apt()
        if not commands:return 'failed'
    if not commands:
        # When repository setup is unsupported, show the vendor's signed
        # repository setup instructions, then let the next init retry the install.
        print('      '+('Configure the official pgAdmin APT repository, then rerun init: https://www.pgadmin.org/download/pgadmin-4-apt/' if manager()=='apt' else 'Install from the official download page, then rerun init: '+PGADMIN_URL))
        return 'manual'
    for command in commands:
        try:
            if subprocess.run(command,check=False).returncode:return 'failed'
        except OSError:return 'failed'
    refresh_tool_path()
    return 'ready' if pgadmin_ready() else 'unverified'


PGADMIN_KEY_URL='https://www.pgadmin.org/static/packages_pgadmin_org.pub'
PGADMIN_REPO_URL='https://ftp.postgresql.org/pub/pgadmin/pgadmin4/apt/'
PGADMIN_KEY_PATH=Path('/etc/apt/keyrings/gaston-pgadmin4.gpg')
PGADMIN_SOURCE_PATH=Path('/etc/apt/sources.list.d/gaston-pgadmin4.list')


def pgadmin_apt_source_present():
    sources=[Path('/etc/apt/sources.list'),*Path('/etc/apt/sources.list.d').glob('*.list'),*Path('/etc/apt/sources.list.d').glob('*.sources')]
    for source in sources:
        try:
            if any(PGADMIN_REPO_URL in line and not line.lstrip().startswith('#') for line in source.read_text(encoding='utf-8').splitlines()):return True
        except (OSError,UnicodeError):pass
    return False


def pgadmin_repo_spec():
    """The official desktop repo is supported only on known Ubuntu x86_64 releases.

    Do not add a duplicate source or overwrite user files. Never trust the key
    globally; Signed-By scopes it to the vendor's repository.
    """
    if manager()!='apt' or is_wsl() or platform.machine().lower() not in ('x86_64','amd64'):
        return None
    try: release=platform.freedesktop_os_release()
    except (AttributeError,OSError):return None
    codename=release.get('VERSION_CODENAME')
    if release.get('ID')!='ubuntu' or codename not in ('jammy','noble','resolute'):return None
    if pgadmin_apt_source_present() or any(os.path.lexists(p) for p in (PGADMIN_KEY_PATH,PGADMIN_SOURCE_PATH)):return None
    if not plan_pgadmin_apt() or not all(shutil.which(name) for name in ('curl','gpg','install')):return None
    return {'key_path':PGADMIN_KEY_PATH,'source_path':PGADMIN_SOURCE_PATH,
            'source':f'deb [signed-by={PGADMIN_KEY_PATH}] {PGADMIN_REPO_URL}{codename} pgadmin4 main\n'}


def install_pgadmin_repo(spec):
    """Only call after developer approves the displayed vendor key and source."""
    if pgadmin_repo_spec()!=spec:return False
    prefix=[] if hasattr(os,'geteuid') and os.geteuid()==0 else ['sudo']
    with tempfile.TemporaryDirectory(prefix='gaston-pgadmin-') as td:
        folder=Path(td);key=folder/'key.pub';binary=folder/'key.gpg';source=folder/'pgadmin4.list'
        try:
            download=run(['curl','--fail','--silent','--show-error','--proto','=https','--max-filesize','1048576','--output',str(key),PGADMIN_KEY_URL],timeout=45)
            if not download or download.returncode or not key.is_file() or key.stat().st_size>1048576:return False
            decoded=run(['gpg','--batch','--no-options','--homedir',str(folder),'--dearmor','--output',str(binary),str(key)],timeout=20)
            if not decoded or decoded.returncode or not binary.is_file() or binary.stat().st_size==0:return False
            source.write_text(spec['source'],encoding='utf-8')
            if pgadmin_repo_spec()!=spec:return False
            # Explicit, known root-owned destinations; no shell or broad key trust.
            for src,dest in ((binary,spec['key_path']),(source,spec['source_path'])):
                if os.path.lexists(dest):return False
                if subprocess.run(prefix+['install','-D','-m','0644','--',str(src),str(dest)],check=False).returncode:return False
            return True
        except OSError:return False


def offer_mac_docker_install(*,interactive,ask,lang='en'):
    """Separate approval for Docker Desktop; never install a competing CLI/daemon."""
    if platform.system()!='Darwin' or manager()!='brew' or not interactive or not sys.stdin.isatty() or docker_desktop_app() or shutil.which('docker'):
        return False
    if hasattr(os,'geteuid') and os.geteuid()==0:return False
    cmd=['brew','install','--cask','docker-desktop']
    print('      '+shlex.join(cmd))
    if not ask('Installer Docker Desktop pour les tests isolés ?' if lang=='fr' else 'Install Docker Desktop for isolated testing?',default=True,interactive=True,lang=lang):return False
    try:
        outcome=subprocess.run(cmd,check=False).returncode==0
    except OSError:return False
    refresh_tool_path()
    return outcome and bool(docker_desktop_app())
