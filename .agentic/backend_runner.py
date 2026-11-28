#!/usr/bin/env python3
"""Allowlisted Java build verification for the private agent workflow.

Uses a writable project-local dependency cache, never modifies source files or
build configuration. Maven/Gradle builds can execute project-defined plugins;
only run against a trusted developer workspace, never an untrusted repository.
"""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import secrets
import xml.etree.ElementTree as ET

ALLOWED_MAVEN_GOALS = ('test','verify','test-compile')
SAFE_TEST = re.compile(r'[A-Za-z0-9_.$,*+-]{1,160}\Z')


def _within(root: Path, candidate: Path) -> bool:
    return candidate.is_relative_to(root)


def discover(root: Path) -> list[dict]:
    """Discover only build roots, skipping local/generated dependency trees."""
    root = Path(root).resolve()
    result = []
    ignore = {'.git','.agentic','.gaston','.m2','node_modules','target','build','.gradle','.dart_tool', '.venv'}
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        dirs[:] = sorted(x for x in dirs if x not in ignore and not (here/x).is_symlink()) if depth < 7 else []
        if 'pom.xml' in files:
            result.append({'path':here.relative_to(root).as_posix(),'system':'maven'})
        elif ('build.gradle' in files or 'build.gradle.kts' in files):
            result.append({'path':here.relative_to(root).as_posix(),'system':'gradle'})
    return result



def required_java_release(project: Path) -> int | None:
    """Read only explicit Java target settings; never infer a target from Spring Boot.

    Works with namespaced Maven POMs and Gradle's common Java toolchain DSL.
    Values expressed through unrelated Maven properties cannot be reliably
    resolved; those are handled by Maven's actual compiler diagnostic.
    """
    pom=project/'pom.xml'
    if pom.is_file():
        try:
            tree=ET.parse(pom)
            root=tree.getroot()
            properties={node.tag.rsplit('}',1)[-1]:(node.text or '').strip()
                        for props in root.iter() if props.tag.rsplit('}',1)[-1]=='properties'
                        for node in props}
            for key in ('maven.compiler.release','java.version','maven.compiler.target'):
                value=properties.get(key,'')
                if value.startswith('${') and value.endswith('}'):
                    value=properties.get(value[2:-1],'')
                if re.fullmatch(r'(?:1\.)?\d+',value):
                    number=int(value.split('.')[-1]);return number if 8<=number<=99 else None
            # Explicit compiler <release> can override properties.
            for node in root.iter():
                if node.tag.rsplit('}',1)[-1]!='release':continue
                value=(node.text or '').strip()
                if value.startswith('${') and value.endswith('}'):
                    value=properties.get(value[2:-1],'')
                if re.fullmatch(r'\d+',value) and 8<=int(value)<=99:
                    return int(value)
        except (ET.ParseError,OSError): pass
    for name in ('build.gradle','build.gradle.kts'):
        gradle=project/name
        if not gradle.is_file():continue
        try: content=gradle.read_text(encoding='utf-8')
        except OSError:continue
        for pattern in (r'JavaLanguageVersion\.of\(\s*(\d+)\s*\)',
                        r'(?:sourceCompatibility|targetCompatibility)\s*=\s*(?:JavaVersion\.VERSION_)?(\d+)'):
            match=re.search(pattern,content)
            if match and 8<=int(match.group(1))<=99:return int(match.group(1))
    return None


def _jdk_version(home: Path) -> int | None:
    javac=home/'bin'/'javac'
    java=home/'bin'/'java'
    if not javac.is_file() or not java.is_file():return None
    try:
        proc=subprocess.run([str(javac),'-version'],stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,text=True,timeout=5,check=False)
    except (OSError,subprocess.TimeoutExpired):return None
    if proc.returncode!=0:return None
    match=re.search(r'javac\s+(?:1\.)?(\d+)(?:[.\s+_-]|$)',proc.stdout+' '+proc.stderr)
    return int(match.group(1)) if match else None


def installed_jdks() -> list[dict]:
    """Discover local toolchains without downloads or changing global Java settings."""
    candidates=[]
    home=os.environ.get('JAVA_HOME')
    if home:candidates.append(Path(home))
    for command in ('javac','java'):
        path=shutil.which(command)
        if path:candidates.append(Path(path).resolve().parent.parent)
    for parent in (Path('/usr/lib/jvm'),Path('/opt/java'),Path('/opt/jdk'),
                   Path.home()/'.sdkman/candidates/java',Path.home()/'.jdks',
                   Path.home()/'.asdf/installs/java',Path.home()/'.local/share/mise/installs/java'):
        if parent.is_dir():
            candidates.extend(sorted(x for x in parent.iterdir() if x.is_dir() or x.is_symlink()))
    found={}
    for candidate in candidates:
        try: candidate=candidate.resolve(strict=True)
        except (OSError,RuntimeError):continue
        if str(candidate) in found:continue
        version=_jdk_version(candidate)
        if version:found[str(candidate)]={'home':str(candidate),'version':version}
    return sorted(found.values(),key=lambda item:(item['version'],item['home']))


def select_jdk(required: int | None, jdks: list[dict]) -> dict | None:
    if not jdks:return None
    if required is None:
        preferred=os.environ.get('JAVA_HOME')
        if preferred:
            matches=[j for j in jdks if Path(j['home'])==Path(preferred).resolve()]
            if matches:return matches[0]
        # Keep the effective PATH JDK if the project's target is not explicit.
        path=shutil.which('javac')
        if path:
            matches=[j for j in jdks if Path(j['home'])==Path(path).resolve().parent.parent]
            if matches:return matches[0]
        return jdks[-1]
    matching=[j for j in jdks if j['version']>=required]
    return min(matching,key=lambda item:(item['version'],item['home'])) if matching else None


def _wrapper_distribution_name(properties: Path) -> str | None:
    """Read the pinned distribution name without executing a wrapper script."""
    try:
        text=properties.read_text(encoding='utf-8')
    except OSError:
        return None
    match=re.search(r'^\s*distributionUrl\s*=\s*(.+?)\s*$',text,re.M)
    if not match:return None
    from urllib.parse import urlsplit
    name=Path(urlsplit(match.group(1).strip()).path).name
    for suffix in ('-bin.tar.gz','-bin.zip','.tar.gz','.zip'):
        if name.endswith(suffix):return name[:-len(suffix)].removesuffix('-bin')
    return None


def _wrapper_distribution_cached(properties: Path, user_home: Path, executable: str) -> bool:
    """Avoid an implicit wrapper download during an offline-only compile."""
    if not properties.is_file():return False
    name=_wrapper_distribution_name(properties)
    if not name:return False  # Unknown/custom distribution cannot be proven cached.
    distributions=user_home/'wrapper'/'dists'
    if not distributions.is_dir():return False
    binary='mvn' if executable=='maven' else 'gradle'
    try:
        return any(path.name==binary and path.parent.parent.name==name
                   for path in distributions.rglob(binary) if path.is_file())
    except OSError:
        return False


def dependency_network_usage(stdout: str, stderr: str, network_allowed: bool) -> str:
    """Report only dependency-manager activity visible in its own output.

    This is deliberately not described as process-wide network monitoring:
    trusted project test code is not OS-network-isolated by this runner.
    """
    output = (str(stdout or '') + '\n' + str(stderr or ''))
    if re.search(r'\bDownloaded from\b|\bDownloaded\b', output, re.I):
        return 'download_observed'
    if re.search(r'\bDownloading from\b|\bDownloading\s+https?://|Could not transfer artifact|Could not (?:GET|HEAD|POST|PUT)|HTTP response code:\s*\d{3}|status code:\s*\d{3}|401 Unauthorized|Unknown host|Connection timed out', output, re.I):
        return 'request_observed'
    return 'unknown' if network_allowed else 'not_used_by_build_tool'


def run(root: Path, relative_path: str, *, goal: str = 'test', test_filter: str | None = None,
        timeout: int = 300, offline: bool = True, allow_network: bool = False) -> dict:
    """Execute a bounded Java test within the exact discovered project root.

    Maven/Gradle dependency resolution is offline by default. An online retry
    is possible only through a caller that has separately validated explicit
    developer approval. Project build/test code remains trusted code, not an
    OS-level network sandbox; its network activity is therefore not inferred.
    """
    root = Path(root).resolve()
    if not isinstance(relative_path,str) or Path(relative_path).is_absolute():
        return {'status':'blocked','reason':'Choose an existing workspace Java project'}
    directory = (root/relative_path).resolve()
    if not _within(root,directory) or directory.is_symlink() or not directory.is_dir():
        return {'status':'blocked','reason':'Backend path outside workspace or missing'}
    targets = {entry['path']:entry['system'] for entry in discover(root)}
    rel = directory.relative_to(root).as_posix()
    if rel not in targets:
        return {'status':'blocked','reason':'Not a discovered Java project'}
    if goal not in ALLOWED_MAVEN_GOALS:
        return {'status':'blocked','reason':'Only test, verify and compile-only Java goals are supported'}
    if goal=='test-compile' and test_filter is not None:return {'status':'blocked','reason':'Compile-only gate has no test selector'}
    if test_filter is not None and (not isinstance(test_filter,str) or not SAFE_TEST.fullmatch(test_filter)):
        return {'status':'blocked','reason':'Test selector must be a safe Java test-class pattern'}
    cache = project_state(root)/'local'/'backend-dependencies'
    try:
        cache.mkdir(parents=True,exist_ok=True)
    except OSError as exc:
        return {'status':'blocked','reason_code':'CACHE_UNAVAILABLE',
                'reason':f'Project-local dependency cache is not writable: {exc}'}
    env = os.environ.copy()
    if goal=='test-compile':
        for key in tuple(env):
            if key.startswith(('GASTON_LEAD_','GASTON_SESSION_','GASTON_READONLY_DATABASE','GASTON_DATABASE_ALLOW_','SPRING_DATASOURCE_','DATABASE_URL','PGPASSWORD','PGUSER','PGHOST','PGPORT','PGDATABASE')):env.pop(key,None)
    env['MAVEN_USER_HOME'] = str(cache/'maven-user')
    env['GRADLE_USER_HOME'] = str(cache/'gradle-home')
    env['CI'] = 'true'
    if offline or not allow_network:
        # Do not let inherited proxy settings silently re-enable dependency traffic.
        for key in tuple(env):
            if key.lower() in {'http_proxy','https_proxy','all_proxy','no_proxy'}:
                env.pop(key, None)
    required=required_java_release(directory)
    available=installed_jdks()
    selected=select_jdk(required,available)
    if required is not None and selected is None:
        return {'status':'blocked','reason_code':'JDK_UNAVAILABLE',
                'reason':f'Java {required}+ JDK required but not installed',
                'required_java':required,'installed_jdks':available,
                'remedy':'Install a compatible JDK with approval; no pom.xml changes or downloads attempted'}
    if selected:
        env['JAVA_HOME']=selected['home']
        env['PATH']=str(Path(selected['home'])/'bin')+os.pathsep+env.get('PATH','')
    build = targets[rel]
    if build == 'maven':
        wrapper=directory/('mvnw.cmd' if os.name=='nt' else 'mvnw')
        # Use the project's pinned Maven distribution whenever its POSIX
        # wrapper is executable. A system Maven can have different artifact
        # origin metadata and fail offline against the same local repository.
        wrapper_ready=wrapper.is_file() and (os.name=='nt' or os.access(wrapper,os.X_OK))
        binary=str(wrapper) if wrapper_ready else shutil.which('mvn')
        if not binary:return {'status':'blocked','reason_code':'RUNNER_UNAVAILABLE','reason':'Executable Maven wrapper or system Maven not found'}
        if (offline or not allow_network) and wrapper_ready and not _wrapper_distribution_cached(directory/'.mvn/wrapper/maven-wrapper.properties',Path(env['MAVEN_USER_HOME']),'maven'):
            return {'status':'blocked','reason_code':'DEPENDENCY_RESOLUTION_UNAVAILABLE',
                    'reason':'Pinned Maven Wrapper distribution cannot be verified as cached; online fallback requires explicit approval',
                    'runner':'mvnw','project':rel,'build_system':build,'goal':goal,
                    'cache_path':str(cache.relative_to(root)),'network_allowed':False,
                    'network_policy':'offline','network_usage':'not_used_by_build_tool'}
        repo=cache/'maven-repository'
        try:repo.mkdir(parents=True,exist_ok=True)
        except OSError as exc:
            return {'status':'blocked','reason_code':'CACHE_UNAVAILABLE',
                    'reason':f'Project-local Maven repository is not writable: {exc}',
                    'project':rel,'build_system':build,'goal':goal}
        args=[binary,'-B','-ntp',f'-Dmaven.repo.local={repo}']
        if offline or not allow_network:args+=['-o']
        if test_filter:args += [f'-Dtest={test_filter}']
        args += [goal]
    else:
        wrapper=directory/('gradlew.bat' if os.name=='nt' else 'gradlew')
        binary = str(wrapper) if wrapper.is_file() else shutil.which('gradle')
        if not binary:return {'status':'blocked','reason':'Gradle binary or wrapper not found'}
        wrapper_ready=wrapper.is_file()
        if (offline or not allow_network) and wrapper_ready and not _wrapper_distribution_cached(directory/'.gradle/wrapper/gradle-wrapper.properties',Path(env['GRADLE_USER_HOME']),'gradle'):
            return {'status':'blocked','reason_code':'DEPENDENCY_RESOLUTION_UNAVAILABLE',
                    'reason':'Pinned Gradle Wrapper distribution cannot be verified as cached; online fallback requires explicit approval',
                    'runner':'gradlew','project':rel,'build_system':build,'goal':goal,
                    'cache_path':str(cache.relative_to(root)), 'network_allowed':False,
                    'network_policy':'offline','network_usage':'not_used_by_build_tool'}
        args=[binary,'--no-daemon','--console=plain']
        if offline or not allow_network:args+=['--offline']
        args += ['testClasses' if goal=='test-compile' else 'test' if goal=='test' else 'check']
        if test_filter:args += ['--tests',test_filter]
    timeout = max(1,min(int(timeout),1200))
    started=time.monotonic()
    from platform_compat import prepare_subprocess
    try:
        command,run_env,_=prepare_subprocess(args,env)
        p=subprocess.run(command,cwd=directory,env=run_env,text=True,stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,timeout=timeout,check=False)
        stdout=p.stdout[-12000:];stderr=p.stderr[-8000:];code=p.returncode
    except subprocess.TimeoutExpired as exc:
        return {'status':'blocked','reason':'Backend test runner timed out','timeout_seconds':timeout,
                'elapsed_seconds':round(time.monotonic()-started,2)}
    except OSError as exc:
        return {'status':'blocked','reason':f'Build command could not start: {exc}'}
    # A parent POM or compiler plugin can set a release not visible in the
    # project's own POM. Retry once, only with an already-installed toolchain.
    compiler_target=re.search(r'error:\s*release version\s+(\d+)\s+not supported',stdout+'\n'+stderr,re.I)
    if code!=0 and compiler_target:
        actual_required=int(compiler_target.group(1))
        alternate=select_jdk(actual_required,available)
        if alternate and (not selected or alternate['home']!=selected['home']):
            env['JAVA_HOME']=alternate['home']
            env['PATH']=str(Path(alternate['home'])/'bin')+os.pathsep+os.environ.get('PATH','')
            try:
                command,run_env,_=prepare_subprocess(args,env)
                p=subprocess.run(command,cwd=directory,env=run_env,text=True,stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,timeout=timeout,check=False)
                stdout=p.stdout[-12000:];stderr=p.stderr[-8000:];code=p.returncode
                selected=alternate
            except (subprocess.TimeoutExpired,OSError) as exc:
                return {'status':'blocked','reason':f'Java {actual_required} retry could not execute: {exc}',
                        'required_java':actual_required,'installed_jdks':available}
        elif not alternate:
            return {'status':'blocked','reason':f'Java {actual_required}+ JDK required but not installed; tests did not execute',
                    'required_java':actual_required,'installed_jdks':available,
                    'exit_code':code,'evidence':{'stdout_tail':stdout[-6000:],'stderr_tail':stderr[-4000:]}}
    # Keep the complete test evidence outside the model conversation. These
    # local, ignored logs are mode 0600; no application source is modified.
    logs=None
    try:
        directory_logs=project_state(root)/'local/backend-logs'
        directory_logs.mkdir(parents=True,exist_ok=True,mode=0o700)
        log_file=directory_logs/(secrets.token_hex(12)+'.log')
        fd=os.open(log_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8',errors='replace') as handle:
            handle.write('STDOUT\n');handle.write(p.stdout or '')
            handle.write('\nSTDERR\n');handle.write(p.stderr or '')
        logs=str(log_file.relative_to(root))
    except OSError:
        logs=None  # Never turn a successful test into a false failure.
    combined=(stdout+'\n'+stderr).lower()
    cache_failure=any(x in combined for x in (
        'read-only file system','could not create local repository',
        'failed to create local repository','permission denied while writing',
        'permission denied','access is denied'))
    dependency_failure=any(x in combined for x in (
        'could not resolve dependencies','could not resolve plugin','could not find artifact',
        'non-resolvable parent pom','could not transfer artifact','unknown host',
        'nodename nor servname provided','no address associated with hostname',
        'unable to access','failed to collect dependencies','cannot access central',
        'cannot resolve external dependency','no cached version available',
        'could not resolve all files','pluginresolutionexception',
        'was not found in','has not been downloaded from it before',
        'offline mode','network is unreachable','connection timed out'))
    # No stdout supplied to the user should include Maven env credentials;
    # short evidence snippets are still from normal compiler/test output.
    status = 'pass' if code==0 else 'blocked' if cache_failure or dependency_failure else 'fail'
    reason_code=('CACHE_UNAVAILABLE' if cache_failure else
                 'DEPENDENCY_RESOLUTION_UNAVAILABLE' if dependency_failure else None)
    network_allowed = bool(allow_network and not offline)
    return {'status':status,'reason_code':reason_code,'project':rel,'build_system':build,'goal':goal,
            'test_filter':test_filter,'exit_code':code,'runner':Path(binary).name,
            'elapsed_seconds':round(time.monotonic()-started,2),
            'cache_path':str(cache.relative_to(root)),
            'log_path':logs,
            'network_allowed':network_allowed,
            'network_policy':'approved_online' if network_allowed else 'offline',
            'network_usage':dependency_network_usage(p.stdout or '',p.stderr or '',network_allowed),
            'network_scope':'Maven/Gradle dependency resolution only; project build/test code is not OS-network-isolated',
            'required_java':required,'selected_jdk':selected,'installed_jdks':available,
            'evidence':{'stdout_tail':stdout[-6000:],'stderr_tail':stderr[-4000:]},
            **({'reason':'Dependency cache is not writable; build did not execute' if reason_code=='CACHE_UNAVAILABLE' else 'Dependency resolution unavailable; build did not complete' } if status=='blocked' else {})}
