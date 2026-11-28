"""Run an actual project-owned Spring Boot test launcher and curl suite.

A domain-specific launcher must supply isolated database/provider fixtures. No
production credentials, arbitrary shell, or guessed application configuration.
The launch process and all its children belong to this one invocation.
"""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode
import json, os, re, shutil, signal, socket, subprocess, sys, tempfile, time
from pathlib import Path
import backend_runner

DESCRIPTOR='gaston-e2e.json'

def _inside(parent, value):
    if not isinstance(value,str) or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('Expected a project-relative file')
    path=(parent/value).resolve()
    if not path.is_relative_to(parent) or not path.is_file() or (parent/value).is_symlink():
        raise ValueError('Missing or unsafe project test file: '+value)
    return path

def descriptor(project):
    cfg=json.loads(_inside(project,DESCRIPTOR).read_text())
    if not isinstance(cfg,dict):raise ValueError('Invalid E2E descriptor')
    main=cfg.get('main_class','')
    if not isinstance(main,str) or not re.fullmatch(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+',main,re.ASCII):
        raise ValueError('A fully qualified test-source main_class is required')
    _inside(project,'src/test/java/'+main.replace('.','/')+'.java')
    suite=_inside(project,cfg.get('suite'))
    cases=json.loads(suite.read_text()).get('cases',[])
    if not isinstance(cases,list) or not cases or not any(isinstance(c,dict) and (c.get('expect_json') or c.get('capture_json')) for c in cases):
        raise ValueError('A real application suite with JSON assertions is required')
    path=cfg.get('readiness_path','')
    if not isinstance(path,str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',path) or path.startswith('//'):
        raise ValueError('Invalid readiness path')
    status=cfg.get('readiness_status',200)
    if type(status) is not int or not 200<=status<300:raise ValueError('Readiness must expect a 2xx response')
    driver=None
    if cfg.get('scenario_driver') is not None:
        driver=_inside(project,cfg['scenario_driver'])
        if not driver.relative_to(project).as_posix().startswith('src/test/') or driver.suffix!='.py':
            raise ValueError('Optional scenario_driver must be a reviewed Python test under src/test/')
    return main,suite,path,status,driver

def scenario_timeout(project, default=240):
    """Optional bounded, project-owned budget for the stateful scenario driver.

    The HTTP assertion suite still uses the runner's original suite timeout.
    Only real JSON integers are permitted; booleans and unbounded values
    cannot silently disable the managed verification gate.
    """
    cfg=json.loads(_inside(project,DESCRIPTOR).read_text(encoding='utf8'))
    value=cfg.get('scenario_driver_timeout_seconds',default)
    if type(value) is not int or not 1<=value<=600:
        raise ValueError('scenario_driver_timeout_seconds must be an integer from 1 to 600')
    return value

def _env(root,jdk):
    # Keep only ordinary process/build/Docker settings. No inherited app,
    # database, provider, auth tokens or production environment variables.
    allowed={'PATH','HOME','USER','LOGNAME','LANG','LC_ALL','LC_CTYPE','TMPDIR','XDG_RUNTIME_DIR','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH','SYSTEMROOT','WINDIR','SYSTEMDRIVE','HOMEDRIVE','HOMEPATH','USERPROFILE','TEMP','TMP','APPDATA','LOCALAPPDATA','PROGRAMFILES'}
    env={k:v for k,v in os.environ.items() if k in allowed}
    # Reject remote Docker overrides; the test launcher may use local Testcontainers.
    if env.get('DOCKER_HOST') and not (env['DOCKER_HOST'].startswith('unix://') or (os.name=='nt' and env['DOCKER_HOST']=='npipe:////./pipe/docker_engine')):
        raise ValueError('Managed E2E requires a local Docker socket, not a remote Docker host')
    env.pop('DOCKER_CONTEXT',None)
    if not env.get('DOCKER_HOST') and os.name!='nt':
        rootless=Path(env.get('XDG_RUNTIME_DIR','/nonexistent'))/'docker.sock'
        env['DOCKER_HOST']='unix://'+str(rootless if rootless.is_socket() else Path('/var/run/docker.sock'))
    cache=project_state(root)/'local/backend-dependencies';cache.mkdir(parents=True,exist_ok=True)
    env.update(MAVEN_USER_HOME=str(cache/'maven-user'),CI='true',SPRING_PROFILES_ACTIVE='gaston-e2e',GASTON_E2E='true')
    if jdk:
        env['JAVA_HOME']=jdk['home'];env['PATH']=str(Path(jdk['home'])/'bin')+os.pathsep+env.get('PATH','')
    return env,cache

def _stop(proc):
    # Never signal a PID taken from an untrusted file.
    from platform_compat import stop_owned
    stop_owned(proc, isolated_group=True, grace_seconds=10)

def run(root,relative_path,*,startup_timeout=180,suite_timeout=240,browser_check=None,
        allow_network=False,explicit_developer_approval=False,approval_quote=''):
    root=Path(root).resolve()
    network_allowed=bool(allow_network)
    if network_allowed and (explicit_developer_approval is not True or not isinstance(approval_quote,str) or not approval_quote.strip()):
        return {'status':'blocked','project':relative_path,'managed_application':False,
                'reason':'Online backend verification requires explicit developer approval',
                'network_allowed':False,'network_policy':'offline','network_usage':'not_used_by_build_tool'}
    try:
        if not isinstance(relative_path,str) or Path(relative_path).is_absolute() or '..' in Path(relative_path).parts:raise ValueError('Choose a workspace-relative Maven project')
        project=(root/relative_path).resolve()
        if not project.is_relative_to(root) or not (project/'pom.xml').is_file():raise ValueError('Choose an existing Maven Spring Boot project')
        main,suite,ready_path,ready_status,driver=descriptor(project)
        driver_budget=scenario_timeout(project,suite_timeout) if driver is not None else suite_timeout
        if not shutil.which('curl'):raise ValueError('curl is missing')
        jdk=backend_runner.select_jdk(backend_runner.required_java_release(project),backend_runner.installed_jdks())
        if not jdk:raise ValueError('Compatible JDK unavailable')
        env,cache=_env(root,jdk)
        wrapper=project/('mvnw.cmd' if os.name=='nt' else 'mvnw')
        binary=str(wrapper) if wrapper.is_file() else shutil.which('mvn')
        if not binary:raise ValueError('Maven unavailable')
        if not network_allowed and wrapper.is_file() and not backend_runner._wrapper_distribution_cached(
                project/'.mvn/wrapper/maven-wrapper.properties',Path(env['MAVEN_USER_HOME']),'maven'):
            return {'status':'blocked','project':relative_path,'managed_application':False,
                    'reason_code':'DEPENDENCY_RESOLUTION_UNAVAILABLE',
                    'reason':'Pinned Maven Wrapper distribution is not cached; online fallback requires explicit approval',
                    'network_allowed':False,'network_policy':'offline','network_usage':'not_used_by_build_tool'}
    except (ValueError,OSError,AttributeError) as exc:
        return {'status':'blocked','project':relative_path,'reason':str(exc),'managed_application':False,
                'network_allowed':network_allowed,'network_policy':'approved_online' if network_allowed else 'offline',
                'network_usage':'unknown' if network_allowed else 'not_used_by_build_tool',
                'network_scope':'Maven dependency resolution only; application and test code are not OS-network-isolated'}
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    env['AGENTIC_TEST_API_BASE_URL']=base
    cmd=[binary,'-B','-ntp',f'-Dmaven.repo.local={cache/"maven-repository"}']
    if not network_allowed:cmd.append('-o')
    cmd += ['-Dspring-boot.run.main-class='+main,'-Dspring-boot.run.profiles=gaston-e2e',
            f'-Dspring-boot.run.arguments=--server.address=127.0.0.1 --server.port={port}',
            'test-compile','spring-boot:test-run']
    local=project_state(root)/'local';local.mkdir(parents=True,exist_ok=True)
    result={'status':'blocked','project':relative_path,'managed_application':False,
            'network_allowed':network_allowed,'network_policy':'approved_online' if network_allowed else 'offline',
            'network_usage':'unknown' if network_allowed else 'not_used_by_build_tool',
            'network_scope':'Maven dependency resolution only; application and test code are not OS-network-isolated'}
    proc=None
    captured_stdout='';captured_stderr=''
    # Private fixture logs may include test OTPs. Never send raw logs through MCP.
    with tempfile.TemporaryDirectory(prefix='gaston-e2e-',dir=local) as td:
        try:
            with open(Path(td)/'startup.log','wb') as log:
                from platform_compat import popen_kwargs
                proc=subprocess.Popen(cmd,cwd=project,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,**popen_kwargs())
                deadline=time.monotonic()+startup_timeout;ready=False
                while time.monotonic()<deadline and proc.poll() is None:
                    check=subprocess.run(['curl','--disable','--silent','--noproxy','*','--max-time','2','--output',os.devnull,'--write-out','%{http_code}',base+ready_path],capture_output=True,text=True,timeout=4,env=env)
                    if check.returncode==0 and check.stdout==str(ready_status):ready=True;break
                    time.sleep(.5)
                if not ready:
                    result.update(reason='Application did not become HTTP-ready; inspect test launcher, profile, migrations and database',startup_exit_code=proc.poll())
                elif browser_check is not None:
                    # Fresh isolated application; do not consume one-time setup
                    # tokens with the API scenarios before the browser suite.
                    browser=browser_check(base)
                    result.update(status=browser['status'],managed_application=True,readiness_http_status=ready_status,browser=browser)
                else:
                    checked=subprocess.run([sys.executable,str(Path(__file__).with_name('api_checks.py')),str(suite)],cwd=project,env=env,capture_output=True,text=True,timeout=suite_timeout)
                    captured_stdout=checked.stdout or '';captured_stderr=checked.stderr or ''
                    result.update(status='pass' if checked.returncode==0 else 'blocked' if checked.returncode==2 else 'fail',managed_application=True,readiness_http_status=ready_status,exit_code=checked.returncode,summary=captured_stdout[-8000:],suite=suite.relative_to(project).as_posix())
                    if checked.returncode==0 and driver is not None:
                        # Allows real nonce signing/TOTP/other stateful protocol tests
                        # without inventing a generic authentication protocol. Only
                        # project-owned test code, reviewed by the Lead, is executed.
                        # The driver emits exit status and must never print secrets.
                        extra=subprocess.run([sys.executable,str(driver)],cwd=project,env=env,capture_output=True,text=True,timeout=driver_budget)
                        captured_stdout+='\nSCENARIO STDOUT\n'+(extra.stdout or '')
                        captured_stderr+='\nSCENARIO STDERR\n'+(extra.stderr or '')
                        result['scenario_driver_timeout_seconds']=driver_budget
                        result.update(scenario_driver=driver.relative_to(project).as_posix(),scenario_driver_exit_code=extra.returncode)
                        if extra.returncode!=0:
                            (Path(td)/'scenario.log').write_text(extra.stdout[-32000:]+'\n'+extra.stderr[-32000:],encoding='utf8')
                            result.update(status='fail',reason='Stateful application scenario driver failed')
        except (OSError,subprocess.TimeoutExpired) as exc:
            result.update(status='blocked',reason='Managed backend launch/check did not complete: '+type(exc).__name__)
        finally:
            try:
                startup_text=(Path(td)/'startup.log').read_text(encoding='utf8',errors='replace')
                result['network_usage']=backend_runner.dependency_network_usage(startup_text,'',network_allowed)
                from platform_compat import persist_verification_log
                log_path=persist_verification_log(local/'verification-logs',startup_text,captured_stdout+'\n'+captured_stderr)
                if log_path:result['log_path']=Path(log_path).resolve().relative_to(root).as_posix()
            except (OSError,ValueError):
                if network_allowed:result['network_usage']='unknown'
            if proc:
                try:_stop(proc);result['process_cleanup']='complete'
                except (OSError,subprocess.TimeoutExpired):result.update(status='blocked',process_cleanup='failed',reason='Could not confirm owned application termination')
            if result['status']!='pass':
                # Preserve private synthetic-test diagnostics for the Lead to
                # inspect locally; never dump raw fixture OTPs/tokens into MCP.
                try:
                    failures=local/'managed-backend-failures'
                    failures.mkdir(mode=0o700,exist_ok=True)
                    if failures.is_symlink() or not failures.resolve().is_relative_to(local.resolve()):
                        raise OSError('Unsafe log directory')
                    for name in ('startup.log','scenario.log'):
                        source=Path(td)/name
                        if not source.is_file():continue
                        with source.open('rb') as stream:
                            stream.seek(max(0,os.fstat(stream.fileno()).st_size-65536))
                            tail=stream.read(65536)
                        fd,target=tempfile.mkstemp(prefix=name.split('.')[0]+'-',suffix='.log',dir=failures)
                        with os.fdopen(fd,'wb') as dest:dest.write(tail)
                        result[name.split('.')[0]+'_diagnostic_log']=Path(target).relative_to(root).as_posix()
                except OSError:pass
    return result
