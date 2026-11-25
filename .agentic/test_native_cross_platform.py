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

    def test_subprocess_lifecycle(self):
        with subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
                              **pc.popen_kwargs()) as child:
            pc.stop_owned(child,isolated_group=True,grace_seconds=.1)
            self.assertIsNotNone(child.poll())

if __name__=='__main__':unittest.main()
