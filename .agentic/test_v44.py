"""Owner, quota, persistent DB and platform regressions; no installs or external services."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

import development_db as db
import host_setup as hs
import orchestration as orch
import quota_worker as qw
import recovery_consent as rc
import session_store as ss
import verification_mcp as vm


def result(code=0, out=''):
    return subprocess.CompletedProcess([],code,out,'')


class PlatformTests(unittest.TestCase):
    def test_wsl_never_auto_installs_second_docker_engine(self):
        with patch.object(hs.platform,'system',return_value='Linux'),patch.object(hs.platform,'release',return_value='5.15-microsoft-standard-WSL2'),patch.object(hs.os,'geteuid',return_value=1000),patch.object(hs.shutil,'which',return_value='/usr/bin/sudo'):
            commands,manual=hs.plan(['docker','psql'],package_manager='apt')
            self.assertEqual(manual,['docker']);self.assertIn('postgresql-client',commands[-1]);self.assertNotIn('docker.io',commands[-1]);self.assertIn('WSL integration',hs.manual_guidance(manual))

    def test_native_windows_and_unknown_distribution_no_package_plan(self):
        with patch.object(hs.platform,'system',return_value='Windows'):
            self.assertEqual(hs.plan(['psql']),([],['psql']))
        with patch.object(hs.platform,'system',return_value='Linux'),patch.object(hs.platform,'freedesktop_os_release',return_value={'ID':'arch'}):
            self.assertEqual(hs.plan(['psql']),([],['psql']))

    def test_macos_homebrew_paths_dont_touch_profiles(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); lib=root/'opt/libpq/bin';lib.mkdir(parents=True)
            with patch.object(hs.platform,'system',return_value='Darwin'),patch.object(hs.shutil,'which',return_value='/fake/brew'),patch.object(hs,'run',return_value=result(0,str(root))),patch.dict(os.environ,{'PATH':'/existing'},clear=True):
                hs.refresh_tool_path();self.assertEqual(os.environ['PATH'].split(os.pathsep)[0],'/existing');self.assertIn(str(lib),os.environ['PATH'].split(os.pathsep))
                self.assertIn('Docker Desktop',hs.manual_guidance(['docker']))

    def test_explicit_remote_docker_context_never_falls_back(self):
        with patch.object(hs.shutil,'which',return_value='/bin/docker'),patch.dict(os.environ,{'DOCKER_CONTEXT':'remote'},clear=True),patch.object(hs,'run',return_value=result(0,'ssh://remote')):
            self.assertIsNone(hs.local_docker())

    def test_mac_process_discovery_excludes_managed_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();rows={10:(1,['claude'],'claude'),11:(10,['codex'],'codex'),12:(1,['codex'],'codex')}
            with patch.object(ss.platform,'system',return_value='Darwin'),patch.object(ss.Path,'is_dir',return_value=False),patch.object(ss,'_mac_processes',return_value=rows),patch.object(ss,'_mac_cwd',return_value=root):
                self.assertEqual(ss.native_processes(root,exclude=[10]),[{'pid':12,'provider':'codex'}])


class OwnershipTests(unittest.TestCase):
    def test_workspace_lead_is_unique_across_providers(self):
        with tempfile.TemporaryDirectory() as td:
            a=ss.Store(Path(td));b=ss.Store(Path(td))
            with a.locked('claude','reviewer'):
                with self.assertRaises(RuntimeError):
                    with b.locked('codex','reviewer'): self.fail('duplicate Lead')
            with b.locked('codex','reviewer'): pass

    def test_unmanaged_lead_without_lock_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            store=ss.Store(Path(td))
            with patch.object(store,'unmanaged_native',return_value=[{'pid':55,'provider':'claude'}]):
                with self.assertRaisesRegex(RuntimeError,'Unmanaged native AI chat'):
                    with store.locked('codex','reviewer'): pass
                self.assertFalse(store.locks.exists())

    def test_mcp_rejects_fork_with_inherited_lease(self):
        with tempfile.TemporaryDirectory() as td:
            store=ss.Store(Path(td))
            with store.locked('claude','reviewer'):
                lease=os.environ['GASTON_SESSION_LEASE'];store.record_child(os.getpid())
                with patch.object(ss,'parent_and_names',return_value=(os.getpid(),['python'])):
                    self.assertTrue(store.lead_caller(lease,'claude',123456))
                with patch.object(ss,'parent_and_names',return_value=(os.getpid(),['claude'])):
                    self.assertFalse(store.lead_caller(lease,'claude',123456))
                self.assertFalse(store.lead_caller('bad','claude',os.getpid()))

    def test_live_server_rejects_old_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'.agentic').mkdir();(root/'.agentic/manifest.json').write_text('{}')
            with patch.object(vm,'ROOT',root),patch.object(vm,'LIVE_SERVER',True):
                reply=vm.handle({'method':'tools/call','params':{'name':'workspace_overview','arguments':{}}})
                self.assertTrue(reply['isError']);self.assertIn('outdated toolkit',reply['content'][0]['text'])


class QuotaTests(unittest.TestCase):
    def test_local_wakeup_resumes_exact_task_only_and_rejects_revocation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'.agentic/runs/test-quota';folder.mkdir(parents=True)
            manifest=root/'.agentic/manifest.json';manifest.write_text('{}');digest=hashlib.sha256(manifest.read_bytes()).hexdigest()
            orch.put(folder/'task.json',{'goal':'existing'});orch.put(folder/'state.json',{'stage':'blocked','blocker':'provider_quota','resumable':True});orch.put(folder/'worker.json',{'attempt':'a'*32})
            record={'decision':'approved','job_id':'local-id','attempt':'a'*32,'retry_epoch':time.time()-1,'recovery_token':'b'*48}
            orch.put(folder/'quota-recovery.json',record)
            with patch.object(vm,'ROOT',root),patch.object(vm,'_resume_builder',return_value={'status':'queued'}) as resume,patch.object(ss.Store,'unmanaged_native',return_value=[]):
                self.assertEqual(qw.execute(root,folder,'local-id',digest),'queued')
                resume.assert_called_once_with({'task_id':folder.name,'recovery_token':'b'*48})
                resume.reset_mock();orch.put(folder/'quota-recovery.json',{**record,'decision':'declined'});self.assertIn('cancelled',qw.execute(root,folder,'local-id',digest));resume.assert_not_called()

    def test_schedule_creates_one_python_worker_not_lead(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'.agentic/runs/task';folder.mkdir(parents=True);(root/'.agentic/manifest.json').write_text('{}')
            orch.put(folder/'quota-recovery.json',{'decision':'approved','recovery_token':'token','retry_at':'later'})
            def register(folder,token,job):
                rec=orch.read_record(folder/'quota-recovery.json');orch.put(folder/'quota-recovery.json',{**rec,'job_id':job})
            with patch.object(qw.subprocess,'Popen',return_value=Mock(pid=42)) as spawn,patch.object(rc,'record_job',side_effect=register),patch.object(qw,'process_state',return_value='live'):
                a=qw.schedule(root,folder,{'recovery_token':'token'});b=qw.schedule(root,folder,{'recovery_token':'token'})
                self.assertEqual(a['job_id'],b['job_id']);spawn.assert_called_once();self.assertIn('quota_worker.py',spawn.call_args.args[0][1])
                self.assertNotIn('GASTON_LEAD_TOKEN',spawn.call_args.kwargs['env'])


class PersistentDatabaseTests(unittest.TestCase):
    def test_fixture_credentials_private_and_workspace_bound(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);c=db.credentials(root,create=True);self.assertEqual(c,db.credentials(root));self.assertNotEqual(c['development_password'],c['readonly_password']);self.assertEqual((root/'.gaston-dev-db/credentials.json').stat().st_mode&0o777,0o600)
            (root/'.gaston-dev-db/credentials.json').chmod(0o644)
            with self.assertRaises(ValueError):db.credentials(root)

    def test_owned_container_must_be_labelled_loopback_volume(self):
        obj={'Config':{'Labels':{db.LABEL:'key'}},'Mounts':[{'Type':'volume','Name':'volume','Destination':'/var/lib/postgresql/data'}],'HostConfig':{'PortBindings':{'5432/tcp':[{'HostIp':'127.0.0.1'}]}}}
        db.owned_container(obj,'key','volume')
        obj['HostConfig']['PortBindings']['5432/tcp'][0]['HostIp']='0.0.0.0'
        with self.assertRaises(ValueError):db.owned_container(obj,'key','volume')

    def test_existing_foreign_volume_never_modified(self):
        with tempfile.TemporaryDirectory() as td,patch.object(hs,'daemon_id',return_value='local'),patch.object(hs,'psql_ready',return_value=True),patch.object(db,'inspect',side_effect=[None,{'Labels':{}}]),patch.object(hs,'docker_run') as docker:
            state=db.provision(Path(td),['docker']);self.assertEqual(state['status'],'blocked');docker.assert_not_called();self.assertFalse((Path(td)/'.gaston-dev-db').exists())

    def test_provision_named_volume_loopback_and_distinct_logins(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);key,name,volume=db.identity(root);commands=[]
            obj={'Config':{'Labels':{db.LABEL:key}},'Mounts':[{'Type':'volume','Name':volume,'Destination':'/var/lib/postgresql/data'}],'HostConfig':{'PortBindings':{'5432/tcp':[{'HostIp':'127.0.0.1'}]}},'NetworkSettings':{'Ports':{'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'54321'}]}},'State':{'Running':True}}
            def run(prefix,args,**kw):
                commands.append(args)
                if args[0]=='run':self.assertTrue(Path(args[args.index('--env-file')+1]).exists())
                return result()
            def query(port,user,password,sql):return result(0,f'{db.DB}|{user}\n')
            with patch.object(hs,'daemon_id',return_value='local'),patch.object(hs,'psql_ready',return_value=True),patch.object(db,'inspect',side_effect=[None,None,obj]),patch.object(hs,'docker_run',side_effect=run),patch.object(db,'host_query',side_effect=query) as login:
                state=db.provision(root,['docker'])
            self.assertEqual(state['status'],'ready',state);self.assertEqual(state['port'],54321);self.assertEqual(login.call_count,2)
            launch=next(c for c in commands if c[0]=='run');self.assertIn('127.0.0.1::5432',launch);self.assertNotIn('--rm',launch);self.assertTrue(any(volume in a for a in launch))
            self.assertFalse(list((root/'.gaston-dev-db').glob('.postgres-env-*')))


if __name__=='__main__':unittest.main()
