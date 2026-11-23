"""OS-specific setup regression tests; never install host software or start real services."""
from __future__ import annotations
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import host_setup as hs
import launcher
import evolution
import platform_compat
import backend_runner
import setup_wizard as sw


def result(code=0,out=''):
    return subprocess.CompletedProcess([],code,out,'')


class WindowsSetupTests(unittest.TestCase):
    def test_winget_exact_packages_deduplicated_no_postgres_server(self):
        with patch.object(hs.platform,'system',return_value='Windows'),patch.object(hs.shutil,'which',return_value='winget.exe'):
            cmds,manual=hs.plan(['git','node','npm','java','psql','docker'])
        self.assertEqual(manual,['psql'])
        self.assertEqual([c[-1] for c in cmds],['Git.Git','OpenJS.NodeJS.LTS','EclipseAdoptium.Temurin.17.JDK','Docker.DockerDesktop'])
        self.assertTrue(all(c[:6]==['winget','install','--source','winget','--exact','--id'] for c in cmds))

    def test_named_pipe_whitelist(self):
        for endpoint,expected in [('npipe:////./pipe/dockerDesktopLinuxEngine',True),('npipe:////./pipe/docker_engine',True),('npipe:////remote/pipe/docker_engine',False),('tcp://127.0.0.1:2375',False),('ssh://remote',False)]:
            with self.subTest(endpoint=endpoint),patch.object(hs.platform,'system',return_value='Windows'),patch.object(hs.shutil,'which',return_value='docker.exe'),patch.dict(os.environ,{'DOCKER_HOST':endpoint},clear=True):
                self.assertEqual(hs.local_docker(),['docker','--host',endpoint] if expected else None)

    def test_windows_context_inspection_no_remote_fallback(self):
        with patch.object(hs.platform,'system',return_value='Windows'),patch.object(hs.shutil,'which',return_value='docker.exe'),patch.dict(os.environ,{'DOCKER_CONTEXT':'remote'},clear=True),patch.object(hs,'run',return_value=result(0,'ssh://other')):
            self.assertIsNone(hs.local_docker())
        with patch.object(hs.platform,'system',return_value='Windows'),patch.object(hs.shutil,'which',return_value='docker.exe'),patch.dict(os.environ,{},clear=True),patch.object(hs,'run',return_value=result(0,'npipe:////./pipe/dockerDesktopLinuxEngine')):
            self.assertEqual(hs.local_docker(),['docker','--host','npipe:////./pipe/dockerDesktopLinuxEngine'])

    def test_native_build_wrappers_are_not_replaced(self):
        # Inspection of the native runner must not modify an existing project wrapper.
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'pom.xml').write_text('<project/>')
            (root/'mvnw.cmd').write_text('wrapper')
            self.assertIn("'mvnw.cmd' if os.name=='nt'", Path(backend_runner.__file__).read_text())
            self.assertIn("'gradlew.bat' if os.name=='nt'", Path(backend_runner.__file__).read_text())
            self.assertEqual((root/'mvnw.cmd').read_text(),'wrapper')

    def test_windows_initialization_reaches_real_engine(self):
        with patch.object(evolution,'launch') as launch, \
             patch.object(hs,'refresh_tool_path') as refresh, \
             patch.object(platform_compat,'protect_workspace_local') as acl:
            self.assertEqual(launcher.main(['init'],system='Windows'),0)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[1],['init'])
            refresh.assert_called_once()
            acl.assert_called_once()

    def test_windows_lifecycle_commands_reach_same_engine(self):
        for args in ([], ['sessions'], ['models'], ['uninstall']):
            with self.subTest(args=args),patch.object(evolution,'launch') as launch, \
                 patch.object(hs,'refresh_tool_path'), \
                 patch.object(platform_compat,'protect_workspace_local'):
                self.assertEqual(launcher.main(args,system='Windows'),0)
                self.assertEqual(launch.call_args.args[1],args)

    def test_old_preview_is_not_a_supported_entrypoint(self):
        root=Path(__file__).resolve().parent.parent
        self.assertFalse((root/'.agentic/windows_setup.py').exists())
        self.assertFalse((root/'dev.cmd').exists())
        self.assertIn('dev = "gaston_cli:main"',(root/'pyproject.toml').read_text())


class PgAdminTests(unittest.TestCase):
    def test_existing_gui_reused_silently(self):
        with patch.object(hs,'pgadmin_ready',return_value=True),patch.object(hs,'pgadmin_plan') as plan:
            self.assertEqual(hs.offer_pgadmin(interactive=True,ask=Mock(side_effect=AssertionError('must not ask'))),'ready');plan.assert_not_called()

    def test_noninteractive_never_installs(self):
        with patch.object(hs,'pgadmin_ready',return_value=False),patch.object(hs,'pgadmin_plan') as plan:
            self.assertEqual(hs.offer_pgadmin(interactive=False,ask=Mock(side_effect=AssertionError('must not ask'))),'skipped');plan.assert_not_called()

    def test_native_mac_and_windows_gui_commands(self):
        with patch.object(hs,'manager',return_value='brew'),patch.object(hs.os,'geteuid',return_value=1000):
            self.assertEqual(hs.pgadmin_plan(),[['brew','install','--cask','pgadmin4']])
        with patch.object(hs,'manager',return_value='winget'):
            self.assertEqual(hs.pgadmin_plan(),[['winget','install','--source','winget','--exact','--id','PostgreSQL.pgAdmin']])

    def test_ubuntu_existing_repo_desktop_only(self):
        with patch.object(hs,'manager',return_value='apt'),patch.object(hs,'is_wsl',return_value=False),patch.object(hs,'run',return_value=result(0,'  Candidate: 9.19\n')),patch.object(hs.os,'geteuid',return_value=1000),patch.object(hs.shutil,'which',return_value='/usr/bin/sudo'):
            self.assertEqual(hs.pgadmin_plan(),[['sudo','apt-get','update'],['sudo','apt-get','install','--no-remove','-y','pgadmin4-desktop']])

    def test_pgadmin_optional_default_no_and_decline(self):
        with patch.object(hs,'pgadmin_ready',return_value=False),patch.object(hs,'pgadmin_plan',return_value=[['brew','install','--cask','pgadmin4']]),patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs.subprocess,'run') as invoke,patch('builtins.input',return_value='') as prompt,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hs.offer_pgadmin(interactive=True,ask=sw.yesno),'declined');self.assertIn('[default: No]',prompt.call_args.args[0]);invoke.assert_not_called()

    def test_gui_install_requires_actual_gui_detection(self):
        for detected,expected in [(True,'ready'),(False,'unverified')]:
            with patch.object(hs,'pgadmin_ready',side_effect=[False,detected]),patch.object(hs,'pgadmin_plan',return_value=[['brew','install','--cask','pgadmin4']]),patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs.subprocess,'run',return_value=result()) as invoke,patch.object(hs,'refresh_tool_path'),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hs.offer_pgadmin(interactive=True,ask=Mock(return_value=True)),expected);invoke.assert_called_once()

    def test_new_repo_scoped_and_no_existing_file_overwrite(self):
        with patch.object(hs,'manager',return_value='apt'),patch.object(hs,'is_wsl',return_value=False),patch.object(hs.platform,'machine',return_value='x86_64'),patch.object(hs.platform,'freedesktop_os_release',return_value={'ID':'ubuntu','VERSION_CODENAME':'noble'}),patch.object(hs,'pgadmin_apt_source_present',return_value=False),patch.object(hs.os.path,'lexists',return_value=False),patch.object(hs,'plan_pgadmin_apt',return_value=[['apt-get']]),patch.object(hs.shutil,'which',return_value='/usr/bin/tool'):
            spec=hs.pgadmin_repo_spec();self.assertIn('signed-by=/etc/apt/keyrings/gaston-pgadmin4.gpg',spec['source']);self.assertIn('/noble pgadmin4 main',spec['source'])
            with patch.object(hs.os.path,'lexists',return_value=True):self.assertIsNone(hs.pgadmin_repo_spec())
            with patch.object(hs.platform,'machine',return_value='aarch64'):self.assertIsNone(hs.pgadmin_repo_spec())

    def test_new_vendor_repo_only_on_consent(self):
        spec={'source':'deb vendor\n','key_path':Path('/key'),'source_path':Path('/source')}
        for consent in (True,False):
            with patch.object(hs,'pgadmin_ready',side_effect=[False,True]),patch.object(hs,'pgadmin_plan',return_value=[]),patch.object(hs,'pgadmin_repo_spec',return_value=spec),patch.object(hs,'plan_pgadmin_apt',return_value=[['apt-get','install','pgadmin4-desktop']]),patch.object(hs,'install_pgadmin_repo',return_value=True) as add,patch.object(hs.subprocess,'run',return_value=result()) as install,patch.object(hs,'refresh_tool_path'),patch.object(hs.sys.stdin,'isatty',return_value=True),contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(hs.offer_pgadmin(interactive=True,ask=Mock(return_value=consent)),'ready' if consent else 'declined');self.assertIn('New system source:',out.getvalue());self.assertEqual(add.call_count,int(consent));self.assertEqual(install.call_count,int(consent))

    def test_repo_install_uses_vendor_download_scoped_commands(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);spec={'source':'deb [signed-by=/key] vendor\n','key_path':base/'key','source_path':base/'source'}
            def run(cmd,**kwargs):
                if cmd[0]=='curl':Path(cmd[cmd.index('--output')+1]).write_text('key')
                else:Path(cmd[cmd.index('--output')+1]).write_bytes(b'key-data')
                return result()
            with patch.object(hs,'pgadmin_repo_spec',return_value=spec),patch.object(hs,'run',side_effect=run) as calls,patch.object(hs.subprocess,'run',return_value=result()) as install,patch.object(hs.os,'geteuid',return_value=1000):
                self.assertTrue(hs.install_pgadmin_repo(spec));self.assertEqual(calls.call_args_list[0].args[0][-1],hs.PGADMIN_KEY_URL);self.assertEqual(install.call_count,2);self.assertTrue(all(c.args[0][:5]==['sudo','install','-D','-m','0644'] for c in install.call_args_list))


class MacDockerTests(unittest.TestCase):
    def test_mac_docker_desktop_separate_explicit_consent(self):
        for consent in (True,False):
            with patch.object(hs.platform,'system',return_value='Darwin'),patch.object(hs,'manager',return_value='brew'),patch.object(hs.sys.stdin,'isatty',return_value=True),patch.object(hs,'docker_desktop_app',side_effect=[None,Path('/Applications/Docker.app')]),patch.object(hs.shutil,'which',return_value=None),patch.object(hs.os,'geteuid',return_value=1000),patch.object(hs.subprocess,'run',return_value=result()) as install,patch.object(hs,'refresh_tool_path'),contextlib.redirect_stdout(io.StringIO()):
                ask=Mock(return_value=consent);self.assertEqual(hs.offer_mac_docker_install(interactive=True,ask=ask),consent);self.assertTrue(ask.call_args.kwargs['default']);self.assertEqual(install.call_count,int(consent))

    def test_desktop_start_never_launches_for_remote_override(self):
        with patch.object(hs,'daemon_id',return_value=None),patch.object(hs,'local_docker',return_value=None),patch.dict(os.environ,{'DOCKER_HOST':'ssh://other'},clear=True),patch.object(hs.subprocess,'Popen') as spawn:
            self.assertFalse(hs.offer_desktop_start(ask=Mock(return_value=True)));spawn.assert_not_called()


if __name__=='__main__':unittest.main()
