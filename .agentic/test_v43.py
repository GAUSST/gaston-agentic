"""Regression for host dependency consent and isolated PostgreSQL setup."""
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import host_setup as hs
import setup_wizard as sw


def result(code=0, out=''):
    return subprocess.CompletedProcess([],code,out,'')


class HostInstallTests(unittest.TestCase):
    def test_ubuntu_psql_only_not_server(self):
        with patch.object(hs.platform,'system',return_value='Linux'), \
             patch.object(hs.platform,'freedesktop_os_release',return_value={'ID':'ubuntu'}), \
             patch.object(hs.shutil,'which',return_value='/bin/tool'), patch.object(hs.os,'geteuid',return_value=1000):
            commands,manual=hs.plan(['psql'])
        self.assertEqual(commands,[['sudo','apt-get','update'],['sudo','apt-get','install','--no-remove','-y','postgresql-client']]);self.assertEqual(manual,[])

    def test_brew_uses_client_only_and_no_docker_desktop(self):
        with patch.object(hs.os,'geteuid',return_value=1000):
            cmds,manual=hs.plan(['psql','docker','node','npm'],package_manager='brew')
        self.assertEqual(cmds,[['brew','install','libpq','node@22']]);self.assertEqual(manual,['docker'])

    def test_fedora_package_selection(self):
        with patch.object(hs.os,'geteuid',return_value=1000),patch.object(hs.shutil,'which',return_value='/usr/bin/sudo'):
            cmds,manual=hs.plan(['psql','java','docker'],package_manager='dnf')
        self.assertEqual(cmds,[['sudo','dnf','install','-y','postgresql','java-17-openjdk-devel']]);self.assertEqual(manual,['docker'])

    def test_missing_sudo_no_execution_plan(self):
        with patch.object(hs.os,'geteuid',return_value=1000),patch.object(hs.shutil,'which',return_value=None):
            self.assertEqual(hs.plan(['psql'],package_manager='apt'),([],['psql']))

    def test_noninteractive_never_asks_or_installs(self):
        ask=Mock(side_effect=AssertionError('must not ask'))
        with patch.object(hs.subprocess,'run') as run:
            self.assertEqual(hs.install(['psql'],interactive=False,ask=ask),'not_attempted');run.assert_not_called()
        ask.assert_not_called()

    def test_decline_no_commands_and_fresh_approval(self):
        ask=Mock(return_value=False)
        commands=[['sudo','apt-get','update'],['sudo','apt-get','install','-y','postgresql-client']]
        out=io.StringIO()
        with patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs,'plan',return_value=(commands,[])),patch.object(hs.subprocess,'run') as run,contextlib.redirect_stdout(out):
            self.assertEqual(hs.install(['psql'],interactive=True,ask=ask),'declined');run.assert_not_called()
        self.assertIn('postgresql-client',out.getvalue());self.assertTrue(ask.call_args.kwargs['default'])

    def test_approved_commands_in_order_and_failure_stops(self):
        commands=[['sudo','apt-get','update'],['sudo','apt-get','install','-y','postgresql-client']]
        with patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs,'plan',return_value=(commands,[])),patch.object(hs.subprocess,'run',side_effect=[result(),result()]) as run:
            self.assertEqual(hs.install(['psql'],interactive=True,ask=Mock(return_value=True)),'installed')
            self.assertEqual([c.args[0] for c in run.call_args_list],commands)
        with patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs,'plan',return_value=(commands,[])),patch.object(hs.subprocess,'run',return_value=result(1)) as run:
            self.assertEqual(hs.install(['psql'],interactive=True,ask=Mock(return_value=True)),'failed');run.assert_called_once()

    def test_install_prompt_enter_yes_explicit_no_and_invalid_retry(self):
        commands=[['sudo','apt-get','install','-y','postgresql-client']]
        for lang, label, answers, expected in [
            ('en','[default: Yes]',[''],'installed'),
            ('en','[default: Yes]',['no'],'declined'),
            ('en','[default: Yes]',['invalid',''],'installed'),
            ('fr','[défaut : Oui]',[''],'installed'),
            ('fr','[défaut : Oui]',['non'],'declined'),
        ]:
            with self.subTest(lang=lang,answers=answers),patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs,'plan',return_value=(commands,[])),patch.object(hs.subprocess,'run',return_value=result()) as run,patch('builtins.input',side_effect=answers) as prompt,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hs.install(['psql'],interactive=True,ask=sw.yesno,lang=lang),expected)
                self.assertIn(label,prompt.call_args.args[0])
                self.assertEqual(run.call_count,int(expected=='installed'))

    def test_docker_start_prompt_enters_yes_or_respects_no(self):
        for answer,expected in [('',True),('no',False)]:
            with self.subTest(answer=answer),patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs.platform,'system',return_value='Linux'),patch.object(hs.shutil,'which',return_value='/usr/bin/tool'),patch.dict(hs.os.environ,{},clear=True),patch.object(hs,'local_docker',return_value=None),patch.object(hs.os,'geteuid',return_value=1000),patch.object(hs,'run',side_effect=[result(0,'default'),result(0,'LoadState=loaded\nActiveState=inactive')]),patch.object(hs.subprocess,'run',return_value=result()) as run,patch('builtins.input',return_value=answer) as prompt,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hs.offer_docker_start(interactive=True,ask=sw.yesno),expected)
                self.assertIn('[default: Yes]',prompt.call_args.args[0])
                if expected: run.assert_called_once_with(['sudo','systemctl','start','docker.service'],check=False)
                else: run.assert_not_called()

    def test_psql_is_really_executable(self):
        with patch.object(hs.shutil,'which',return_value='/usr/bin/psql'),patch.object(hs,'run',return_value=result(0,'psql (PostgreSQL) 16.4')):
            self.assertTrue(hs.psql_ready())
        with patch.object(hs.shutil,'which',return_value='/usr/bin/psql'),patch.object(hs,'run',return_value=result(1)):
            self.assertFalse(hs.psql_ready())

    def test_backend_offers_missing_client_and_not_running_cli_reinstall(self):
        state={'Java >= 17':True,'Java test runner':True,'curl':True,'PostgreSQL client (psql)':False,'Docker engine (disposable PostgreSQL prerequisite)':False}
        with tempfile.TemporaryDirectory() as td,patch.object(sw,'backend_test_readiness',return_value=state),patch.object(sw,'ok_bin',return_value=True),patch.object(hs,'install') as install,patch.object(hs,'local_docker',return_value=None),patch.object(hs,'offer_docker_start'):
            sw.prepare_backend_essentials(Path(td),'backend',approved=True,lang='en')
            self.assertEqual(install.call_args.args[0],['psql'])
            install.reset_mock();sw.prepare_backend_essentials(Path(td),'backend',approved=False,lang='en');install.assert_not_called()

    def test_remote_docker_host_rejected(self):
        with patch.object(hs.shutil,'which',return_value='/usr/bin/docker'),patch.dict(hs.os.environ,{'DOCKER_HOST':'tcp://remote:2375'}):
            self.assertIsNone(hs.local_docker())

    def test_docker_start_no_privilege_change_and_no_remote(self):
        with patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs.platform,'system',return_value='Linux'),patch.object(hs.shutil,'which',return_value='/usr/bin/tool'),patch.dict(hs.os.environ,{'DOCKER_HOST':'tcp://host:1'}),patch.object(hs,'run') as run:
            self.assertFalse(hs.offer_docker_start(interactive=True,ask=Mock(return_value=True)));run.assert_not_called()


class SmokeTests(unittest.TestCase):
    def fake(self, *, query='1', cleanup=True, created=True, wrong_label=False):
        commands=[]; token=''
        def call(prefix,args,**kw):
            nonlocal token
            commands.append(args)
            if args[0]=='run':
                token=args[args.index('--label')+1].split('=',1)[1]
                return result(0 if created else 1,'container')
            if args[:2]==['container','inspect']: return result(0,'other' if wrong_label else token)
            if args[:2]==['container','ls']: return result(0,'' if cleanup else 'gaston-pg-smoke')
            if args[0]=='rm': return result(0 if cleanup else 1)
            if args[0]=='exec': return result(0,query if 'psql' in args else 'ready')
            raise AssertionError(args)
        return commands,call

    def test_isolated_sql_and_cleanup(self):
        commands,call=self.fake()
        with patch.object(hs,'docker_run',side_effect=call):
            state=hs.postgres_smoke(['docker','--host','unix:///var/run/docker.sock'])
        self.assertEqual(state['status'],'pass');self.assertTrue(state['cleanup_confirmed'])
        create=commands[0];self.assertEqual(create[create.index('--network')+1],'none');self.assertIn('--rm',create);self.assertNotIn('-p',create);self.assertNotIn('-v',create);self.assertEqual(create[-1],hs.IMAGE)
        self.assertTrue(any('SELECT 1' in c for c in commands));self.assertTrue(any(c[0]=='rm' for c in commands))

    def test_failed_cleanup_never_passes(self):
        commands,call=self.fake(cleanup=False)
        with patch.object(hs,'docker_run',side_effect=call):
            state=hs.postgres_smoke(['docker'])
        self.assertEqual(state['status'],'failed');self.assertFalse(state['cleanup_confirmed']);self.assertIn(state['container'],state['reason'])

    def test_failed_start_cleans_owned_container(self):
        commands,call=self.fake(created=False)
        with patch.object(hs,'docker_run',side_effect=call):
            state=hs.postgres_smoke(['docker'])
        self.assertEqual(state['status'],'failed');self.assertTrue(any(c[0]=='rm' for c in commands))

    def test_never_removes_wrong_label(self):
        commands,call=self.fake(created=False,wrong_label=True,cleanup=False)
        with patch.object(hs,'docker_run',side_effect=call):
            state=hs.postgres_smoke(['docker'])
        self.assertEqual(state['status'],'failed');self.assertFalse(any(c[0]=='rm' for c in commands))

    def test_noninteractive_never_runs_smoke(self):
        with patch.object(hs,'local_docker',return_value=['docker']),patch.object(hs,'daemon_id',return_value='daemon'),patch.object(hs,'fingerprint',return_value=None),patch.object(hs,'postgres_smoke') as smoke:
            ok,attempted,_=sw.postgres_setup_check(interactive=False,lang='en',progress={},save_progress=Mock())
        self.assertFalse(ok or attempted);smoke.assert_not_called()

    def test_decline_and_reuse_matching_evidence(self):
        fp={'daemon':'daemon','image':'sha256:'+'a'*64}
        with patch.object(hs,'local_docker',return_value=['docker']),patch.object(hs,'daemon_id',return_value='daemon'),patch.object(hs,'fingerprint',return_value=fp),patch.object(sw.sys.stdin,'isatty',return_value=True),patch.object(sw,'yesno',return_value=False) as asks,patch.object(hs,'postgres_smoke') as smoke:
            self.assertEqual(sw.postgres_setup_check(interactive=True,lang='en',progress={},save_progress=Mock())[:2],(False,False))
            asks.reset_mock()
            self.assertEqual(sw.postgres_setup_check(interactive=True,lang='en',progress={'postgres_smoke':{'status':'pass','fingerprint':fp}},save_progress=Mock())[:2],(True,False))
            asks.assert_not_called();smoke.assert_not_called()

    def test_new_image_revalidates_and_saves_evidence(self):
        fp={'daemon':'daemon','image':'sha256:'+'b'*64};progress={'postgres_smoke':{'status':'pass','fingerprint':{'daemon':'daemon','image':'old'}}};save=Mock()
        with patch.object(hs,'local_docker',return_value=['docker']),patch.object(hs,'daemon_id',return_value='daemon'),patch.object(hs,'fingerprint',return_value=fp),patch.object(sw.sys.stdin,'isatty',return_value=True),patch.object(sw,'yesno',return_value=True),patch.object(hs,'postgres_smoke',return_value={'status':'pass','reason':'SQL successful','cleanup_confirmed':True}) as smoke:
            self.assertEqual(sw.postgres_setup_check(interactive=True,lang='en',progress=progress,save_progress=save)[:2],(True,True))
        self.assertEqual(progress['postgres_smoke']['fingerprint'],fp);save.assert_called_once();smoke.assert_called_once()


if __name__=='__main__':unittest.main(verbosity=2)
