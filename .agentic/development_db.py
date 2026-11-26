"""Optional workspace-owned persistent development DB, separate from test containers.

Only local Docker, a named labelled volume, loopback publication and generated
local credentials. Never attach to existing application databases, automatically
modify application configuration, or delete development data.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time

import host_setup as hs
from orchestration import put

LABEL='com.gaston.development-db'
DB='gaston_dev'
ADMIN='gaston_admin'
READONLY='gaston_readonly'


def identity(root):
    key=hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:20]
    return key,'gaston-dev-'+key,'gaston-dev-data-'+key


def private_dir(root):
    root=Path(root).resolve()
    gaston=root/'.gaston'
    local=gaston/'local'
    folder=local/'dev-db'
    if any(p.is_symlink() for p in (gaston,local,folder)):
        raise ValueError('Refusing linked central development credential directory')
    return folder


def _tree_fingerprint(path):
    if path.is_symlink():raise ValueError('Refusing linked legacy development DB directory')
    entries=[]
    for parent,dirs,files in os.walk(path,followlinks=False):
        dirs.sort();files.sort();base=Path(parent)
        for name in dirs+files:
            item=base/name
            if item.is_symlink():raise ValueError('Refusing symbolic link in legacy development DB directory')
            rel=item.relative_to(path).as_posix()
            if item.is_dir():entries.append(('dir',rel))
            else:entries.append(('file',rel,hashlib.sha256(item.read_bytes()).hexdigest()))
    return tuple(entries)


def migrate_legacy_credentials(root):
    """Move the old root-level DB directory under .gaston without losing a byte."""
    root=Path(root).resolve()
    legacy=root/'.gaston-dev-db'
    if not legacy.exists() and not legacy.is_symlink():return False
    if legacy.is_symlink() or not legacy.is_dir():
        raise ValueError('Refusing linked or invalid legacy development DB directory')
    if not (root/'.gaston/project.json').is_file():
        return False
    source_fingerprint=_tree_fingerprint(legacy)
    folder=private_dir(root)
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    if os.name!='nt':
        os.chmod(folder,0o700)
    source=legacy/'credentials.json'
    destination=folder/'credentials.json'
    if source.exists() or source.is_symlink():
        if source.is_symlink() or not source.is_file():
            raise ValueError('Refusing linked or invalid legacy development credentials')
        if os.name!='nt' and (source.stat().st_uid!=os.getuid() or source.stat().st_mode&0o077):
            raise ValueError('Legacy development credentials are not private; inspect before migration')
        if destination.is_symlink():
            raise ValueError('Refusing linked central development credentials')
        if destination.exists():
            if not destination.is_file() or hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256(destination.read_bytes()).digest():
                raise ValueError('Legacy and central development credentials conflict; both copies were preserved')
        else:
            shutil.copy2(source,destination)
            if os.name!='nt':os.chmod(destination,0o600)
            if hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256(destination.read_bytes()).digest():
                destination.unlink(missing_ok=True)
                raise ValueError('Development credential migration checksum mismatch')
    backup=folder/'migration-backups'/'gaston-dev-db'
    if backup.exists() or backup.is_symlink():
        if _tree_fingerprint(backup)!=source_fingerprint:
            raise ValueError('Existing development DB backup differs; legacy directory was preserved')
    else:
        backup.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(legacy,backup)
        if _tree_fingerprint(backup)!=source_fingerprint:
            shutil.rmtree(backup)
            raise ValueError('Development DB backup checksum mismatch')
    shutil.rmtree(legacy)
    return True


def credentials(root, *, create=False):
    root=Path(root).resolve()
    migrate_legacy_credentials(root)
    folder=private_dir(root); path=folder/'credentials.json'
    if create:folder.mkdir(mode=0o700,exist_ok=True)
    if not folder.exists():return None
    if os.name=='nt':
        from platform_compat import protect_private_path
        protect_private_path(folder)
    elif folder.stat().st_uid!=os.getuid() or folder.stat().st_mode&0o077:
        raise ValueError('Development credential directory must be private (0700)')
    if path.is_symlink():raise ValueError('Refusing linked development credentials')
    if not path.exists():
        if not create:return None
        put(path,{'workspace':identity(root)[0], 'admin_password':secrets.token_hex(24),'development_password':secrets.token_hex(24),'readonly_password':secrets.token_hex(24)})
    if os.name=='nt':
        from platform_compat import protect_private_path
        protect_private_path(path)
    elif path.stat().st_uid!=os.getuid() or path.stat().st_mode&0o077:
        raise ValueError('Development credentials must be private (0600)')
    data=json.loads(path.read_text())
    if data.get('workspace')!=identity(root)[0] or any(not re.fullmatch('[a-f0-9]{48}',str(data.get(k,''))) for k in ('admin_password','development_password','readonly_password')):raise ValueError('Invalid development credentials; preserve and investigate')
    return data


def inspect(prefix, kind, name):
    result=hs.docker_run(prefix,[kind,'inspect',name],timeout=10)
    if result and result.returncode==0:
        parsed=json.loads(result.stdout)
        if not isinstance(parsed,list) or len(parsed)!=1:raise ValueError('Unclear Docker object identity')
        return parsed[0]
    # A daemon/permission error is not proof that a named object is absent.
    listing=hs.docker_run(prefix,[kind,'ls','--format','{{.Name}}'] if kind=='volume' else [kind,'ls','-a','--format','{{.Names}}'],timeout=10)
    if not listing or listing.returncode!=0 or name in (listing.stdout or '').splitlines():raise ValueError('Docker object ownership unavailable')
    return None


def owned_container(obj, key, volume):
    if not obj or obj.get('Config',{}).get('Labels',{}).get(LABEL)!=key:raise ValueError('Container name already in use or not Gaston-owned')
    mounts=obj.get('Mounts',[])
    if not any(m.get('Type')=='volume' and m.get('Name')==volume and m.get('Destination')=='/var/lib/postgresql/data' for m in mounts):raise ValueError('Unexpected development data mount')
    # Exactly one published address, IPv4 loopback only.
    configured=obj.get('HostConfig',{}).get('PortBindings') or {}
    bindings=configured.get('5432/tcp') or []
    if set(configured)!={'5432/tcp'} or len(bindings)!=1 or bindings[0].get('HostIp')!='127.0.0.1':raise ValueError('Development database is not strictly loopback-bound')
    if obj.get('HostConfig',{}).get('NetworkMode')=='host':raise ValueError('Unsafe development network mode')


def port(obj):
    bindings=(obj.get('NetworkSettings',{}).get('Ports') or {}).get('5432/tcp') or []
    if len(bindings)!=1 or bindings[0].get('HostIp')!='127.0.0.1':raise ValueError('Development database port is not loopback-bound')
    n=int(bindings[0]['HostPort'])
    if not 1<=n<=65535:raise ValueError('Invalid development database port')
    return n


def safe_details(root,obj):
    return {'status':'ready','host':'127.0.0.1','port':port(obj),'database':DB,'development_user':DB,'readonly_user':READONLY,'credentials_file':str(private_dir(root)/'credentials.json'),'note':'Development only. Configure the application separately; automated tests must use disposable databases.'}


def probe(root):
    """Read-only discovery; never provision/start a database from an AI tool."""
    migrate_legacy_credentials(root)
    if not private_dir(root).exists():return {'status':'not_configured'}
    prefix=hs.local_docker()
    if not prefix or not hs.daemon_id(prefix):return {'status':'blocked','reason':'Local Docker unavailable'}
    key,name,volume=identity(root)
    try:
        creds=credentials(root)
        if not creds:return {'status':'blocked','reason':'Development credentials missing'}
        obj=inspect(prefix,'container',name)
        if not obj:return {'status':'blocked','reason':'Development container missing; rerun ./dev init'}
        owned_container(obj,key,volume)
        vol=inspect(prefix,'volume',volume)
        if not vol or (vol.get('Labels') or {}).get(LABEL)!=key:raise ValueError('Development volume ownership mismatch')
        if not obj.get('State',{}).get('Running'):return {'status':'stopped','reason':'Development container stopped; rerun ./dev init'}
        return safe_details(root,obj)
    except (OSError,ValueError,TypeError,KeyError):return {'status':'blocked','reason':'Development database ownership or configuration is uncertain'}


def readonly_url(root):
    state=probe(root)
    if state.get('status')!='ready':return ''
    data=credentials(root)
    return f'postgresql://{READONLY}:{data["readonly_password"]}@127.0.0.1:{state["port"]}/{DB}'


def host_query(port_number, user, password, query):
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    env.update(PGPASSWORD=password,PGCONNECT_TIMEOUT='5',PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=5000')
    return hs.run(['psql','-X','-w','-h','127.0.0.1','-p',str(port_number),'-U',user,'-d',DB,'-At','-v','ON_ERROR_STOP=1','-c',query],timeout=10,env=env)


def provision(root,prefix, *, poll_seconds=90):
    """Caller has explicit interactive consent. Preserve every persistent resource."""
    root=Path(root).resolve(); key,name,volume=identity(root)
    try:
        if not prefix or not hs.daemon_id(prefix) or not hs.psql_ready():return {'status':'blocked','reason':'Local Docker and host psql are required'}
        obj=inspect(prefix,'container',name); vol=inspect(prefix,'volume',volume)
        if vol and (vol.get('Labels') or {}).get(LABEL)!=key:raise ValueError('Refusing existing unrelated volume')
        if obj:
            owned_container(obj,key,volume)
            if not vol:raise ValueError('Development volume missing; refusing to create replacement data')
        data=credentials(root,create=not (obj or vol))
        if not data:raise ValueError('Existing data has no matching credentials; restore original credentials')
        if not vol:
            result=hs.docker_run(prefix,['volume','create','--label',f'{LABEL}={key}',volume],timeout=20)
            if not result or result.returncode!=0:raise ValueError('Could not create development volume')
        if not obj:
            fd,filename=tempfile.mkstemp(prefix='.postgres-env-',dir=private_dir(root))
            try:
                with os.fdopen(fd,'w') as f:f.write(f'POSTGRES_USER={ADMIN}\nPOSTGRES_DB={DB}\nPOSTGRES_PASSWORD={data["admin_password"]}\n')
                result=hs.docker_run(prefix,['run','--detach','--pull=missing','--name',name,'--label',f'{LABEL}={key}','--publish','127.0.0.1::5432','--mount',f'type=volume,source={volume},target=/var/lib/postgresql/data','--env-file',filename,hs.IMAGE],timeout=300)
            finally:Path(filename).unlink(missing_ok=True)
            if not result or result.returncode!=0:raise ValueError('Could not create development container; data and credentials preserved')
        elif not obj.get('State',{}).get('Running'):
            result=hs.docker_run(prefix,['start',name],timeout=60)
            if not result or result.returncode!=0:raise ValueError('Could not restart existing development container')
        deadline=time.monotonic()+poll_seconds
        while time.monotonic()<deadline:
            ready=hs.docker_run(prefix,['exec',name,'pg_isready','-h','127.0.0.1','-U',ADMIN,'-d',DB],timeout=8)
            if ready and ready.returncode==0:break
            time.sleep(1)
        else:raise ValueError('Development server did not become ready')
        obj=inspect(prefix,'container',name);owned_container(obj,key,volume)
        details=safe_details(root,obj)
        # Fixed SQL identifiers and 48-character hex secrets only. No user SQL.
        # Execution uses standard input, not command-line or model-visible logs.
        sql=f"""DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{DB}') THEN CREATE ROLE {DB} LOGIN PASSWORD '{data['development_password']}' NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF;
IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{READONLY}') THEN CREATE ROLE {READONLY} LOGIN PASSWORD '{data['readonly_password']}' NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF;
END $$;
ALTER DATABASE {DB} OWNER TO {DB};
GRANT pg_read_all_data TO {READONLY};
ALTER ROLE {READONLY} SET default_transaction_read_only=on;
"""
        result=hs.docker_run(prefix,['exec','-i',name,'psql','-X','-U',ADMIN,'-d',DB,'-v','ON_ERROR_STOP=1'],timeout=20,input_text=sql)
        if not result or result.returncode!=0:raise ValueError('Development roles could not be verified; data preserved')
        # Exercise both separate credentials through the actual host port.
        for user,pw in [(DB,data['development_password']),(READONLY,data['readonly_password'])]:
            query=host_query(details['port'],user,pw,"SELECT current_database(), current_user;")
            if not query or query.returncode!=0 or query.stdout.strip()!=f'{DB}|{user}':raise ValueError('Host psql connectivity verification failed')
        return details
    except (ValueError,OSError,TypeError,KeyError) as exc:
        return {'status':'blocked','reason':str(exc) if type(exc) is ValueError else 'Development database setup failed; all persistent resources preserved'}
