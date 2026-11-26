"""Filesystem lock and installed CLI portability, independent of providers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import platform_compat as pc

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import gaston_cli

class NativePlatformTests(unittest.TestCase):
    def test_workspace_discovery_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();(root/'.agentic').mkdir();(root/'.agentic/launcher.py').write_text('# fixture')
            inner=root/'a'/'b';inner.mkdir(parents=True)
            with patch.dict(os.environ,{'GASTON_ROOT':''}):
                self.assertEqual(gaston_cli.workspace(inner),root)
            with patch.dict(os.environ,{'GASTON_ROOT':str(root)}):
                self.assertEqual(gaston_cli.workspace(inner),root)
            with patch.dict(os.environ,{'GASTON_ROOT':str(root/'invalid')}):
                with self.assertRaises(ValueError):gaston_cli.workspace(inner)

    def test_kernel_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'lock'
            with pc.file_lock(path):
                with self.assertRaises(BlockingIOError):
                    with pc.file_lock(path,nonblocking=True):pass
            with pc.file_lock(path,nonblocking=True):pass

    def test_lock_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'lock';target=Path(td)/'target';target.write_text('')
            try:path.symlink_to(target)
            except (OSError,NotImplementedError):self.skipTest('Symlink privilege unavailable')
            with self.assertRaises(ValueError):
                with pc.file_lock(path):pass

    def test_workspace_path_normalization_and_scope(self):
        import orchestration as orch
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            (root/'src').mkdir()
            self.assertEqual(orch.relative_path(root,str(root/'src'/'App.py')),'src/App.py')
            self.assertEqual(orch.relative_path(root,'src\\App.py'),'src/App.py')
            self.assertTrue(orch.within('src\\App.py',['src']))
            self.assertFalse(orch.within('src-old/App.py',['src']))
            with self.assertRaises(ValueError):
                orch.relative_path(root,str(root.parent/'outside.py'))
            with self.assertRaises(ValueError):
                orch.relative_path(root,'../outside.py')

    def test_feedback_worker_guard_uses_shared_cross_platform_lock(self):
        import runtime_recovery
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            with runtime_recovery.feedback_worker_guard(root) as first:
                self.assertTrue(first)
                with runtime_recovery.feedback_worker_guard(root) as second:
                    self.assertFalse(second)
            with runtime_recovery.feedback_worker_guard(root) as after:
                self.assertTrue(after)

    def test_claude_windows_hook_command_uses_git_bash_paths(self):
        import shlex
        import native_hooks
        with patch.object(native_hooks.os, 'name', 'nt'):
            with patch.object(native_hooks.sys, 'executable', r'C:\\Program Files\\Python\\python.exe'):
                command = native_hooks.hook_def(
                    'claude', 'SessionStart',
                    r'C:\\Users\\Dev User\\Gaston\\native_hooks.py')['command']
                args = shlex.split(command)
                self.assertEqual(args, [
                    '/c/Program Files/Python/python.exe',
                    '/c/Users/Dev User/Gaston/native_hooks.py',
                    'claude'
                ])
                parsed = native_hooks._hook_command_args(command, 'claude')
                self.assertEqual(parsed, [
                    r'C:\\Program Files\\Python\\python.exe',
                    r'C:\\Users\\Dev User\\Gaston\\native_hooks.py',
                    'claude'
                ])

    def test_windows_cmd_shim_uses_argument_array(self):
        import base64
        import json
        with patch.object(pc,'WINDOWS',True), patch('shutil.which',side_effect=lambda name: {
            r'C:\\Program Files\\nodejs\\codex.cmd':r'C:\\Program Files\\nodejs\\codex.cmd',
            'powershell.exe':r'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe'
        }.get(name)):
            args=[r'C:\\Program Files\\nodejs\\codex.cmd','exec','--model','model with spaces',r'C:\\work\\file.ts']
            command,env,wrapped=pc.prepare_subprocess(args,{'PATH':r'C:\\Windows'})
        self.assertTrue(wrapped)
        self.assertEqual(json.loads(base64.b64decode(env['GASTON_WINDOWS_BATCH_ARGS']).decode()),args)
        script=base64.b64decode(command[-1]).decode('utf-16le')
        self.assertIn('& $exe @cliArgs',script)
        self.assertNotIn('model with spaces',script)
        self.assertNotIn('GASTON_WINDOWS_BATCH_ARGS', {k for k in env if k != 'GASTON_WINDOWS_BATCH_ARGS'})

    @unittest.skipUnless(os.name == 'nt', 'Windows-only .cmd execution test')
    def test_windows_cmd_shim_executes_real_batch_file(self):
        with tempfile.TemporaryDirectory(prefix='gaston cmd ') as td:
            folder=Path(td)/'Program Files'/'Codex'
            folder.mkdir(parents=True)
            executable=folder/'codex.cmd'
            executable.write_text(
                '@echo off\r\n'
                'echo ARG1=%~1\r\n'
                'echo ARG2=%~2\r\n'
                'echo ARG3=%~3\r\n'
                'echo ARG4=%~4\r\n'
                'exit /b 23\r\n',
                encoding='utf-8')
            target=Path(td)/'project with spaces'/'source file.ts'
            args=[str(executable),'exec','--model','model with spaces',str(target)]
            command,child_env,wrapped=pc.prepare_subprocess(args,dict(os.environ))
            self.assertTrue(wrapped)
            result=subprocess.run(command,env=child_env,text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,23,result.stdout+'\\n'+result.stderr)
            self.assertIn('ARG1=exec',result.stdout)
            self.assertIn('ARG2=--model',result.stdout)
            self.assertIn('ARG3=model with spaces',result.stdout)
            self.assertIn('ARG4='+str(target),result.stdout)

    def test_central_hook_refresh_repairs_missing_old_version_paths(self):
        import json
        import native_hooks

        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            global_home=root/'global'
            versions=global_home/'versions'
            current=versions/'v1.6.8'/'gaston_engine'/'native_hooks.py'
            current.parent.mkdir(parents=True)
            current.write_text('# current hook',encoding='utf-8')
            old=versions/'v1.6.4'/'gaston_engine'/'native_hooks.py'
            base=root/'venv'/'Lib'/'site-packages'/'gaston_engine'/'native_hooks.py'
            base.parent.mkdir(parents=True)
            base.write_text('# base hook',encoding='utf-8')

            class Distribution:
                def locate_file(self,name):
                    return base

            events=('SessionStart','SessionEnd','PreToolUse','Stop')
            configs={}
            for provider,relative in (
                ('claude',Path('.claude')/'settings.local.json'),
                ('codex',Path('.codex')/'hooks.json')):
                path=root/relative
                path.parent.mkdir(parents=True)
                event_hooks={
                    event:[{'hooks':[native_hooks.hook_def(provider,event,str(old))]}]
                    for event in events
                }
                event_hooks['SessionStart'].append({
                    'hooks':[{'type':'command','command':'unrelated-provider-hook'}]
                })
                path.write_text(json.dumps({'hooks':event_hooks},indent=2),encoding='utf-8')
                configs[provider]=path

            with (
                patch.object(native_hooks,'central_mode',return_value=True),
                patch.object(native_hooks,'runtime_script',return_value=current),
                patch('central_update.home',return_value=global_home),
                patch('importlib.metadata.distribution',return_value=Distribution()),
            ):
                refreshed=native_hooks.refresh_central_provider_hooks(root)

            self.assertEqual(refreshed,{'claude':True,'codex':True})
            for provider,path in configs.items():
                data=json.loads(path.read_text(encoding='utf-8'))
                for event in events:
                    commands=[
                        hook['command']
                        for group in data['hooks'][event]
                        for hook in group.get('hooks',[])
                        if hook.get('type')=='command'
                    ]
                    managed=[command for command in commands
                             if (lambda args: len(args)==3 and args[-1]==provider)(
                                 pc.parse_command(command))]
                    self.assertEqual(len(managed),1,(provider,event,commands))
                    self.assertEqual(Path(pc.parse_command(managed[0])[1]),current)
                self.assertIn('unrelated-provider-hook',[
                    hook.get('command')
                    for group in data['hooks']['SessionStart']
                    for hook in group.get('hooks',[])
                ])
            self.assertFalse(old.exists())

    def test_subprocess_lifecycle(self):
        with subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
                              **pc.popen_kwargs()) as child:
            pc.stop_owned(child,isolated_group=True,grace_seconds=.1)
            self.assertIsNotNone(child.poll())


    def test_windows_orphaned_provider_child_is_detected_from_retained_parent_pid(self):
        import sys
        import types
        import session_store

        class NoSuchProcess(Exception): pass
        class AccessDenied(Exception): pass
        class ZombieProcess(Exception): pass
        class Error(Exception): pass

        class Candidate:
            def __init__(self, info): self.info=info; self.pid=info['pid']
        class Process:
            def __init__(self,pid):
                if pid==100: raise NoSuchProcess()
                self.pid=pid
            def create_time(self): return 200.0
            def ppid(self): return 1

        fake=types.SimpleNamespace(
            NoSuchProcess=NoSuchProcess,AccessDenied=AccessDenied,
            ZombieProcess=ZombieProcess,Error=Error,Process=Process,
            process_iter=lambda fields:[Candidate({'pid':200,'create_time':201.0,'ppid':100})]
        )
        with patch.object(session_store,'WINDOWS',True), patch.object(pc,'WINDOWS',True), patch.dict(sys.modules,{'psutil':fake}):
            self.assertTrue(session_store.provider_child_active({
                'pid':100,'start_ticks':str(200_000_000),'isolated_group':True
            }))

    def test_windows_orphan_detection_fails_closed_when_ancestry_is_inaccessible(self):
        import sys
        import types
        import session_store

        class NoSuchProcess(Exception): pass
        class AccessDenied(Exception): pass
        class ZombieProcess(Exception): pass
        class Error(Exception): pass

        class Candidate:
            pid=200
            info={'pid':200,'create_time':201.0,'ppid':300}
        class Process:
            def __init__(self,pid):
                if pid==100: raise NoSuchProcess()
                self.pid=pid
            def create_time(self): return 200.0
            def ppid(self): raise AccessDenied()
        fake=types.SimpleNamespace(
            NoSuchProcess=NoSuchProcess,AccessDenied=AccessDenied,
            ZombieProcess=ZombieProcess,Error=Error,Process=Process,
            process_iter=lambda fields:[Candidate()]
        )
        with patch.object(session_store,'WINDOWS',True), patch.object(pc,'WINDOWS',True), patch.dict(sys.modules,{'psutil':fake}):
            self.assertTrue(session_store.provider_child_active({
                'pid':100,'start_ticks':str(200_000_000),'isolated_group':True
            }))

if __name__=='__main__':unittest.main()
