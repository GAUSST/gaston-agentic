"""Native unified CLI dispatcher tests; full Windows validation runs in CI."""
from __future__ import annotations
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import launcher
import evolution
import host_setup
import platform_compat
import sys
ROOT=Path(__file__).resolve().parent.parent

class LauncherTests(unittest.TestCase):
    def test_all_three_hosts_reach_same_runtime(self):
        for system in ('Linux','Darwin','Windows'):
            with self.subTest(system=system), \
                    patch.object(evolution,'launch') as launch, \
                    patch.object(host_setup,'refresh_tool_path') as refresh, \
                    patch.object(platform_compat,'protect_workspace_local') as protect:
                self.assertEqual(launcher.main(['init','--non-interactive'],system=system),0)
                launch.assert_called_once_with(ROOT,['init','--non-interactive'])
                refresh.assert_called_once_with()
                if system=='Windows':protect.assert_called_once_with(ROOT)
                else:protect.assert_not_called()

    def test_unknown_platform_does_not_touch_runtime(self):
        with patch.object(evolution,'launch') as engine:
            self.assertEqual(launcher.main(['init'],system='Plan9'),2)
            engine.assert_not_called()

    def test_only_one_installed_console_command(self):
        pyproject=(ROOT/'pyproject.toml').read_text()
        self.assertIn('dev = "gaston_cli:main"',pyproject)
        self.assertFalse((ROOT/'dev.cmd').exists())
        self.assertIn('.agentic/launcher.py',(ROOT/'dev').read_text())
        self.assertNotIn('windows_setup', (ROOT/'.agentic/launcher.py').read_text())

    @unittest.skipIf(sys.platform=='win32','Unix shim test runs on Unix only')
    def test_unix_repository_shim_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            fake=Path(tmp)
            shutil.copytree(ROOT/'.agentic',fake/'.agentic',ignore=shutil.ignore_patterns('__pycache__','local'))
            shutil.copy2(ROOT/'dev',fake/'dev')
            result=subprocess.run([str(fake/'dev'),'--help'],cwd=fake,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('init',result.stdout)

if __name__=='__main__':unittest.main()
