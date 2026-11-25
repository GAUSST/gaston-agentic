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

    def test_subprocess_lifecycle(self):
        with subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
                              **pc.popen_kwargs()) as child:
            pc.stop_owned(child,isolated_group=True,grace_seconds=.1)
            self.assertIsNotNone(child.poll())


    def test_posix_absolute_workspace_paths_remain_supported(self):
        import orchestration as orch
        self.assertEqual(orch.relative_path(ROOT,str(ROOT/'src'/'app.py')),'src/app.py')
        with self.assertRaises(ValueError):orch.relative_path(ROOT,'/tmp/outside/app.py')

    def test_windows_relative_and_absolute_paths_normalize_without_losing_scope(self):
        import orchestration as orch
        self.assertEqual(orch.relative_path('C:\\work\\app','C:\\work\\app\\src\\features\\Auth.tsx',windows=True),
                         'src/features/Auth.tsx')
        self.assertEqual(orch.relative_path('C:\\work\\app',r'src\features\Auth.tsx',windows=True),
                         'src/features/Auth.tsx')
        with self.assertRaises(ValueError):
            orch.relative_path('C:\\work\\app','D:\\other\\Auth.tsx',windows=True)
        with self.assertRaises(ValueError):
            orch.relative_path('C:\\work\\app','C:\\work\\app\\..\\secret.txt',windows=True)

    def test_windows_path_scope_comparison_is_case_insensitive_and_separator_safe(self):
        import orchestration as orch
        self.assertTrue(orch.within('src\\Features\\Auth.tsx',['src/features'],windows=True))
        self.assertTrue(orch.within('src/FEATURES/Auth.tsx',['src/features'],windows=True))
        self.assertFalse(orch.within('src/features-old/Auth.tsx',['src/features'],windows=True))

    def test_windows_cmd_shim_is_wrapped_without_interpolating_arguments(self):
        import base64
        import json
        from unittest.mock import patch
        args=['C:\\Program Files\\nodejs\\codex.cmd','exec','--model','model with spaces','C:\\work\\file.ts']
        with patch.object(pc,'WINDOWS',True),patch('shutil.which',side_effect=lambda name: {
            args[0]:args[0],'powershell.exe':'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe'
        }.get(name)):
            command,env,wrapped=pc.prepare_subprocess(args,{'PATH':'C:\\Windows'})
        self.assertTrue(wrapped)
        self.assertEqual(command[0],'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe')
        decoded=base64.b64decode(env['GASTON_WINDOWS_BATCH_ARGS']).decode('utf-8')
        self.assertEqual(json.loads(decoded),args)
        script=base64.b64decode(command[-1]).decode('utf-16le')
        self.assertIn('Remove-Item Env:GASTON_WINDOWS_BATCH_ARGS',script)
        self.assertIn('& $exe @cliArgs',script)
        self.assertNotIn('model with spaces',script)

    def test_windows_native_executable_is_not_wrapped(self):
        from unittest.mock import patch
        with patch.object(pc,'WINDOWS',True),patch('shutil.which',return_value='C:\\Tools\\codex.exe'):
            command,env,wrapped=pc.prepare_subprocess(['codex','exec'],None)
        self.assertEqual(command,['C:\\Tools\\codex.exe','exec'])
        self.assertIsNone(env)
        self.assertFalse(wrapped)

if __name__=='__main__':unittest.main()
