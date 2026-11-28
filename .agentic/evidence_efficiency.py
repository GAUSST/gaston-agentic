"""Conservative, bounded acceptance reuse and compact results.

Never synthesizes a successful check. Reuse is restricted to actual recorded
backend passes with identical workspace and toolchain/environment fingerprints.
Managed HTTP and integrated checkpoint checks are deliberately never shared.
"""
from __future__ import annotations
from project_layout import project_state, project_state_name, project_root

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

CACHE_MAX_AGE_SECONDS = 30 * 60
MAX_FOCUSED_TESTS = 8
MAX_TOOL_DIAGNOSTIC = 1800


def _file_identity(path: Path | None):
    if not path:
        return None
    try:
        resolved = path.resolve(strict=True)
        st = resolved.stat()
        return [str(resolved), st.st_size, st.st_mtime_ns]
    except (OSError, RuntimeError):
        return None


def environment_fingerprint(root: Path, project: str) -> str | None:
    """Hash build context without storing secrets. Fail closed if not discoverable."""
    root = Path(root).resolve()
    if not isinstance(project, str) or not project or Path(project).is_absolute() or '..' in Path(project).parts:
        return None
    directory = (root / project).resolve()
    if not directory.is_relative_to(root) or not directory.is_dir() or not (directory/'pom.xml').is_file() and not any((directory/x).is_file() for x in ('build.gradle','build.gradle.kts')):
        return None
    is_maven=(directory/'pom.xml').is_file()
    wrapper = directory / (('mvnw.cmd' if is_maven else 'gradlew.bat') if os.name=='nt' else ('mvnw' if is_maven else 'gradlew'))
    default_binary = 'mvn' if is_maven else 'gradle'
    runner = wrapper if wrapper.is_file() and (os.name=='nt' or os.access(wrapper,os.X_OK)) else Path(shutil.which(default_binary) or '')
    if not runner or str(runner)=='.' or not _file_identity(runner):
        return None
    java_home = Path(os.environ['JAVA_HOME']) if os.environ.get('JAVA_HOME') else None
    java = java_home / 'bin' / 'java' if java_home is not None else Path(shutil.which('java') or '')
    if not java or not _file_identity(java):
        return None
    # Never persist environment values: the whole canonical payload is hashed.
    # Include runtime settings that may affect compiled behavior and tests.
    env_names=('JAVA_HOME','JAVA_TOOL_OPTIONS','JDK_JAVA_OPTIONS','MAVEN_OPTS','GRADLE_OPTS',
               'SPRING_PROFILES_ACTIVE','SPRING_DATASOURCE_URL','SPRING_DATASOURCE_USERNAME',
               'SPRING_DATASOURCE_PASSWORD','DATABASE_URL','DB_HOST','DB_PORT','DB_NAME',
               'PGHOST','PGPORT','PGDATABASE','PGUSER','PGPASSWORD','DOCKER_HOST',
               'TESTCONTAINERS_HOST_OVERRIDE','CI','TZ')
    cache = project_state(root)/'local/backend-dependencies'
    docker_context=None
    build_files = [directory/x for x in ('pom.xml','build.gradle','build.gradle.kts')]
    uses_testcontainers=False
    for build_file in build_files:
        try:
            if build_file.is_file() and 'testcontainers' in build_file.read_text(encoding='utf8').lower():
                uses_testcontainers=True
        except (OSError,UnicodeError):return None
    if uses_testcontainers:
        docker_binary=shutil.which('docker')
        if not docker_binary:return None  # Docker-dependent tests cannot be cached blindly.
        try:
            proc=subprocess.run([docker_binary,'info','--format','{{.ID}}'],text=True,
                                capture_output=True,timeout=4,check=False)
            if proc.returncode or not proc.stdout.strip():return None
            # Include the daemon identity and local image IDs. No remote pulls.
            images=subprocess.run([docker_binary,'image','ls','--quiet','--no-trunc'],text=True,
                                  capture_output=True,timeout=4,check=False)
            if images.returncode:return None
            docker_context=[proc.stdout.strip(),sorted(set(images.stdout.splitlines()))]
        except (OSError,subprocess.TimeoutExpired):return None
    payload={
        'kind':'gaston-backend-environment-v1','os':os.name,'platform':platform.system(),
        'docker_test_environment':docker_context,
        'machine':platform.machine(),'python':sys.version_info[:3],
        'runner':_file_identity(runner), 'java':_file_identity(java),
        'project':str(directory.relative_to(root)),
        'env':{k:os.environ.get(k) for k in env_names},
        'path':os.environ.get('PATH'),
        'cache_root':str(cache.resolve()),
        # Changes to the project-local cache invalidate old evidence. Directory
        # mtime is a cheap conservative signal, not an assertion of cache state.
        'cache_mtime_ns':cache.stat().st_mtime_ns if cache.exists() else None,
    }
    return hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()


def focused_test_selector(root: Path, folder: Path, project: str | None) -> str | None:
    """Only explicitly changed existing Java tests, not guessed source-to-test mappings."""
    if not isinstance(project,str) or not project:return None
    from task_runtime import read_record
    task = read_record(folder/'task.json')
    if task.get('checkpoint_acceptance'):return None
    build = read_record(folder/'build.json')
    paths = build.get('changed_files')
    if not isinstance(paths,list):return None
    root=Path(root).resolve(); project_path=Path(project)
    names=set()
    for raw in paths:
        if not isinstance(raw,str):continue
        p=Path(raw.replace('\\','/'))
        if p.is_absolute() or '..' in p.parts:continue
        try:relative=p.relative_to(project_path).as_posix()
        except ValueError:continue
        if '/src/test/java/' in '/'+relative and re.fullmatch(r'(?:[^/]+/)*[A-Za-z_$][A-Za-z0-9_$]*(?:Test|Tests|IT)\.java', relative.removeprefix('src/test/java/')):
            target=root/p
            if target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(root):names.add(target.stem)
    if not names or len(names)>MAX_FOCUSED_TESTS:return None
    return ','.join(sorted(names))


def matching_backend_evidence(root:Path, target_folder:Path, project:str, selector:str|None,
                              workspace_digest:str|None, environment_digest:str|None):
    """Find most recent genuine backend pass on exact context, never a checkpoint."""
    if not workspace_digest or not environment_digest:return None
    from acceptance_audit import read
    from task_runtime import read_record
    root=Path(root).resolve(); now=time.time(); runs=project_state(root)/'runs'
    if not runs.is_dir() or target_folder.parent != runs:return None
    task=read_record(target_folder/'task.json')
    if task.get('checkpoint_acceptance'):return None
    winner=None
    for folder in runs.iterdir():
        if not folder.is_dir() or folder.is_symlink() or folder==target_folder:continue
        for entry in read(folder):
            if not isinstance(entry,dict) or entry.get('kind')!='backend' or entry.get('result')!='pass':continue
            recorded=entry.get('recorded_at')
            if not isinstance(recorded,(float,int)) or not 0<=now-recorded<=CACHE_MAX_AGE_SECONDS:continue
            params=entry.get('parameters') or {}
            if (params.get('project'),params.get('test_filter')) != (project,selector):continue
            if params.get('workspace_fingerprint')!=workspace_digest or params.get('environment_fingerprint')!=environment_digest:continue
            evidence=entry.get('evidence')
            if not isinstance(evidence,dict) or evidence.get('status')!='pass':continue
            if entry.get('reused_from'):
                # No reuse chains: only evidence generated by an actual runner.
                continue
            # Only trusted records of executed tests, not unverified outcomes.
            if evidence.get('exit_code')!=0 or evidence.get('goal','test') not in ('test','verify'):
                continue
            if winner is None or recorded>winner[0]:winner=(recorded,folder,entry)
    return (winner[1],winner[2]) if winner else None


def _safe_diagnostic_excerpt(value: str) -> str:
    """Avoid returning common credential/OTP formats in tool diagnostics."""
    value=re.sub(r'(?i)\bBearer\s+\S+','Bearer [REDACTED]',value)
    value=re.sub(r'(?i)\b(password|passwd|secret|token|api[_-]?key|otp|authorization)\b(\s*[:=]\s*)(["\']?)[^\s,;"\']+\3',
                 r'\1\2[REDACTED]',value)
    value=re.sub(r'(?i)(https?://)[^/\s:@]+:[^@\s]+@',r'\1[REDACTED]@',value)
    value=re.sub(r'\bAKIA[0-9A-Z]{16}\b','[REDACTED_AWS_KEY]',value)
    return value


def compact_outcome(outcome:dict, *, failure:bool=False)->dict:
    """Recursively remove raw subprocess output while preserving concise evidence.

    Logs remain in their local evidence files. A failure gets a short diagnostic
    excerpt; successful checks return counts, status, provenance and log paths.
    """
    if not isinstance(outcome,dict):return outcome
    log_fields={'stdout_tail','stderr_tail','stdout','stderr','output','log','logs','summary'}
    collected=[]

    def visit(value, failed=False):
        if isinstance(value,dict):
            local_failed=failed or value.get('status') in ('blocked','fail') or (
                isinstance(value.get('exit_code'),int) and value.get('exit_code')!=0)
            compact={}
            for key,item in value.items():
                if key in log_fields:
                    if isinstance(item,str):
                        collected.append(item)
                        if local_failed and item:
                            compact[key+'_excerpt']=_safe_diagnostic_excerpt(item[-MAX_TOOL_DIAGNOSTIC:])
                    continue
                if isinstance(item,dict):
                    compact[key]=visit(item,local_failed)
                elif isinstance(item,list):
                    compact[key]=[visit(child,local_failed) if isinstance(child,(dict,list)) else child for child in item]
                else:
                    compact[key]=item
            return compact
        if isinstance(value,list):
            return [visit(child,failed) if isinstance(child,(dict,list)) else child for child in value]
        return value

    value=visit(outcome,failure)
    text='\n'.join(collected)
    reports=re.findall(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)',text)
    if reports:
        n,failed_count,errors,skipped=map(int,reports[-1])
        value['last_test_summary']={'run':n,'failed':failed_count,'errors':errors,'skipped':skipped}
    else:
        passed=re.findall(r'(\d+)\s+tests?\s+passed',text,re.I)
        failed_reports=re.findall(r'(\d+)\s+tests?\s+failed',text,re.I)
        if passed or failed_reports:
            value['last_test_summary']={'passed':int(passed[-1]) if passed else 0,
                                        'failed':int(failed_reports[-1]) if failed_reports else 0}
    return value

def record_tool_telemetry(root:Path, name:str, task_id:str|None, duration_seconds:float, output:dict)->None:
    """Log size/timing only, never arguments, prompts, code, logs or credentials."""
    try:
        import os
        import time
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,80}',name):return
        if not isinstance(task_id,str) or not re.fullmatch(r'[a-z0-9-]{8,120}',task_id):task_id=None
        local=project_state(root)/'local';local.mkdir(parents=True,exist_ok=True,mode=0o700)
        row={'time':int(time.time()),'tool':name,'task_id':task_id,
             'duration_ms':max(0,int(1000*duration_seconds)),
             'output_bytes':len(json.dumps(output,ensure_ascii=False).encode('utf-8')),
             'status':str(output.get('status','unknown'))[:32]}
        path=local/'mcp-tool-usage.jsonl'
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
        with os.fdopen(fd,'a',encoding='utf8') as handle:handle.write(json.dumps(row,sort_keys=True)+'\n')
    except (OSError,TypeError,ValueError,OverflowError):
        pass  # Measurement must never prevent actual engineering work.
