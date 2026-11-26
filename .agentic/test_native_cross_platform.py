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
    @unittest.skipUnless(os.name == 'nt', 'Windows venv redirector integration')
    def test_real_venv_worker_claims_reserved_launcher_pid(self):
        import json
        import socket
        import time
        import venv
        import orchestration as orch
        import runtime_recovery as recovery
        from project_layout import project_state
        from session_store import process_identity

        with tempfile.TemporaryDirectory(prefix='gaston worker claim ') as td:
            root = Path(td).resolve()
            env_dir = root / 'bootstrap'
            venv.EnvBuilder(with_pip=False, system_site_packages=True).create(env_dir)
            python = env_dir / 'Scripts/python.exe'
            folder = project_state(root) / 'runs' / 'venv-worker-claim'
            folder.mkdir(parents=True)
            attempt = 'a' * 32
            started = time.time()
            orch.put(folder / 'state.json', {'stage': 'queued_for_builder'})
            orch.put(folder / 'worker-launch.json', {
                'attempt': attempt, 'started_at': started,
                'launcher': {'pid': os.getpid(), 'host': socket.gethostname(),
                             **process_identity(os.getpid())},
            })
            script = (
                "import sys, os, json; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); import runtime_recovery as r; "
                "ok=r.claim_worker(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]); "
                "print(json.dumps(dict(ok=ok,pid=os.getpid(),parent=os.getppid()))); "
                "sys.exit(0 if ok else 1)"
            )
            with orch.mutation_guard(root):
                proc = subprocess.Popen(
                    [str(python), '-c', script, str(ROOT / '.agentic'),
                     str(root), str(folder), attempt],
                    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, **pc.popen_kwargs()
                )
                try:
                    identity = process_identity(proc.pid)
                    self.assertTrue(identity.get('start_ticks'), identity)
                    orch.put(folder / 'worker.json', {
                        'pid': proc.pid, 'attempt': attempt, 'started_at': started,
                        'host': socket.gethostname(), **identity,
                    })
                except BaseException:
                    proc.kill()
                    proc.communicate()
                    raise
            try:
                stdout, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            self.assertEqual(proc.returncode, 0, stdout + stderr)
            result = json.loads(stdout.strip().splitlines()[-1])
            self.assertTrue(result['ok'])
            self.assertIn(proc.pid, (result['pid'], result['parent']))
            claimed = orch.read_record(folder / 'worker.json')
            self.assertEqual(claimed['pid'], result['pid'])
            self.assertEqual(claimed['attempt'], attempt)
            self.assertTrue(claimed['claimed_at'])
            # A separate process may not take over the already-claimed attempt.
            self.assertFalse(recovery.claim_worker(root, folder, attempt))

    def test_dead_unclaimed_worker_does_not_wait_for_live_lead(self):
        import time
        import task_runtime
        started = time.time() - task_runtime.WORKER_STARTUP_GRACE_SECONDS - 1
        worker = {'pid': 4321, 'attempt': 'a'*32, 'started_at': started,
                  'start_ticks': '12345'}
        launch = {'attempt': 'a'*32, 'started_at': started, 'launcher': {'pid': 1234}}
        state = {'stage': 'queued_for_builder', 'queued_at': started}
        with patch('session_store.process_state', side_effect=lambda record:
                   'dead' if record.get('pid') == 4321 else 'live'):
            self.assertEqual(task_runtime.worker_ownership(state, worker, launch),
                             ('dead', True))
            worker['started_at'] = time.time()
            self.assertEqual(task_runtime.worker_ownership(state, worker, launch),
                             ('live', True))

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

    def test_claude_windows_hook_command_uses_git_bash_paths_and_round_trips(self):
        import shlex
        import native_hooks
        with patch.object(native_hooks.os, 'name', 'nt'):
            with patch.object(native_hooks.sys, 'executable', r'C:\Program Files\Python\python.exe'):
                command = native_hooks.hook_def(
                    'claude', 'SessionStart',
                    r'C:\Users\Dev User\Gaston\native_hooks.py')['command']
                args = shlex.split(command)
                self.assertEqual(args, [
                    '/c/Program Files/Python/python.exe',
                    '/c/Users/Dev User/Gaston/native_hooks.py',
                    'claude'
                ])
                parsed = native_hooks._hook_command_args(command, 'claude')
                self.assertEqual(parsed, [
                    r'C:\Program Files\Python\python.exe',
                    r'C:\Users\Dev User\Gaston\native_hooks.py',
                    'claude'
                ])

                # v1.6.8 and earlier stored native Windows paths in Claude
                # Bash hooks. POSIX shlex removes their backslashes.
                legacy = (
                    r'C:\Users\LENOVO.local\share\gaston\bootstrap\Scripts\python.exe '
                    r'C:\Users\LENOVO.local\share\gaston\versions\v1.6.8\gaston_engine\native_hooks.py claude'
                )
                self.assertEqual(native_hooks._hook_command_args(legacy, 'claude'), [
                    r'C:\Users\LENOVO.local\share\gaston\bootstrap\Scripts\python.exe',
                    r'C:\Users\LENOVO.local\share\gaston\versions\v1.6.8\gaston_engine\native_hooks.py',
                    'claude'
                ])
                quoted_legacy = (
                    r'"C:\Program Files\Gaston\bootstrap\Scripts\python.exe" '
                    r'"C:\Program Files\Gaston\versions\v1.6.8\gaston_engine\native_hooks.py" claude'
                )
                self.assertEqual(native_hooks._hook_command_args(quoted_legacy, 'claude'), [
                    r'C:\Program Files\Gaston\bootstrap\Scripts\python.exe',
                    r'C:\Program Files\Gaston\versions\v1.6.8\gaston_engine\native_hooks.py',
                    'claude'
                ])


    @unittest.skipUnless(os.name == 'nt', 'Windows-only end-to-end hook repair')
    def test_central_refresh_repairs_legacy_v168_windows_claude_hooks(self):
        import json
        import native_hooks

        with tempfile.TemporaryDirectory(prefix='gaston hook refresh ') as td:
            root = Path(td).resolve()
            global_home = root / 'global'
            versions = global_home / 'versions'
            old = versions / 'v1.6.8' / 'gaston_engine' / 'native_hooks.py'
            current = versions / 'v1.6.12' / 'gaston_engine' / 'native_hooks.py'
            base = global_home / 'bootstrap' / 'Lib' / 'site-packages' / 'gaston_engine' / 'native_hooks.py'
            python = global_home / 'bootstrap' / 'Scripts' / 'python.exe'
            for path in (old, current, base):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# fixture', encoding='utf-8')

            config = root / '.claude' / 'settings.local.json'
            config.parent.mkdir(parents=True)
            events = ('SessionStart', 'SessionEnd', 'PreToolUse', 'Stop')
            legacy_command = subprocess.list2cmdline([str(python), str(old), 'claude'])
            config.write_text(json.dumps({
                'hooks': {
                    event: [{'hooks': [{
                        'type': 'command', 'command': legacy_command, 'timeout': 10
                    }]}] for event in events
                }
            }), encoding='utf-8')

            class Distribution:
                def locate_file(self, name):
                    if name != 'gaston_engine/native_hooks.py':
                        raise AssertionError(name)
                    return base

            with (
                patch.object(native_hooks, 'central_mode', return_value=True),
                patch.object(native_hooks, 'runtime_script', return_value=current),
                patch('central_update.home', return_value=global_home),
                patch('importlib.metadata.distribution', return_value=Distribution()),
                patch.object(native_hooks.sys, 'executable', str(python)),
            ):
                refreshed = native_hooks.refresh_central_provider_hooks(root)

            self.assertEqual(refreshed, {'claude': True, 'codex': False})
            repaired = json.loads(config.read_text(encoding='utf-8'))['hooks']
            for event in events:
                command = repaired[event][0]['hooks'][0]['command']
                self.assertEqual(
                    native_hooks._hook_command_args(command, 'claude'),
                    [str(python), str(current), 'claude']
                )
                self.assertNotIn('C:', command)

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
                                 native_hooks._hook_command_args(command,provider))]
                    self.assertEqual(len(managed),1,(provider,event,commands))
                    self.assertEqual(
                        Path(native_hooks._hook_command_args(managed[0],provider)[1]),
                        current
                    )
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
