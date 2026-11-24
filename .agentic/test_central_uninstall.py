"""Uninstall choices for source-free projects, across native platforms."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parent.parent / 'gaston_cli.py'


class CentralUninstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.app = self.root / 'src' / 'Main.java'
        self.app.parent.mkdir()
        self.app.write_text('class Main {}\n', encoding='utf8')
        adapter = self.root / '.gaston'
        adapter.mkdir()
        (adapter / 'project.json').write_text(
            json.dumps({'schema':1,'kind':'gaston-project',
                        'engine':'gaston-agentic-cli','created_with':'1.5.5'}),
            encoding='utf8')
        self.memory = adapter / 'context' / 'DECISIONS.md'
        self.memory.parent.mkdir()
        self.memory.write_text('Product decision retained\n',encoding='utf8')
        self.legacy = self.root / '.agentic/local/PROJECT_CONTEXT.md'
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text('Old project context\n',encoding='utf8')
        self.env = os.environ.copy()
        self.env.pop('GASTON_ENGINE_MODE', None)
        self.env.pop('GASTON_ROOT', None)
        self.env.pop('GASTON_ENGINE_PROJECT', None)

    def dev(self, choices, *args):
        return subprocess.run([sys.executable, str(CLI), *args],
                              input=choices, text=True, capture_output=True,
                              cwd=self.root, env=self.env, timeout=25)

    def test_keep_is_default_and_retains_every_context_file(self):
        result = self.dev('\nUNINSTALL\n','uninstall')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((self.root/'.gaston/project.json').exists())
        self.assertEqual(self.memory.read_text(),'Product decision retained\n')
        self.assertEqual(self.legacy.read_text(),'Old project context\n')
        self.assertEqual(self.app.read_text(),'class Main {}\n')
        self.assertEqual(self.dev('','init').returncode,0)
        self.assertEqual(self.memory.read_text(),'Product decision retained\n')

    def test_explicit_erase_requires_two_approvals(self):
        result = self.dev('erase\nERASE GASTON DATA\nUNINSTALL\n','uninstall')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((self.root/'.gaston').exists())
        self.assertTrue(self.legacy.is_file())
        self.assertTrue(self.app.is_file())

    def test_cancel_keep_or_erase_is_nondestructive(self):
        for answer in ('cancel\n','erase\nWRONG\n','keep\nWRONG\n'):
            with self.subTest(answer=answer):
                result=self.dev(answer,'uninstall')
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('Cancelled',result.stdout)
                self.assertTrue((self.root/'.gaston/project.json').is_file())
                self.assertTrue(self.memory.is_file())

    def test_active_worker_blocks_both_modes(self):
        active=self.root/'.gaston/local/locks'
        active.mkdir(parents=True)
        (active/'reviewer').write_text('active')
        for answer in ('\nUNINSTALL\n','erase\nERASE GASTON DATA\nUNINSTALL\n'):
            with self.subTest(answer=answer):
                result=self.dev(answer,'uninstall')
                self.assertEqual(result.returncode,2)
                self.assertIn('session or worker is active',result.stderr)
                self.assertTrue(self.memory.is_file())

    def test_linked_internal_data_blocks_erase(self):
        outside=self.root/'outside'
        outside.mkdir()
        (outside/'secret').write_text('not Gaston')
        try:
            (self.root/'.gaston/context/external').symlink_to(
                outside, target_is_directory=True)
        except (OSError,NotImplementedError):
            self.skipTest('Symlinks unavailable')
        result=self.dev('erase\nERASE GASTON DATA\nUNINSTALL\n','uninstall')
        self.assertEqual(result.returncode,2)
        self.assertIn('symbolic links',result.stderr)
        self.assertTrue((outside/'secret').exists())
        self.assertTrue((self.root/'.gaston/project.json').exists())

    def test_invalid_marker_is_never_deleted(self):
        marker=self.root/'.gaston/project.json'
        marker.write_text('not json')
        result=self.dev('\nUNINSTALL\n','uninstall')
        self.assertEqual(result.returncode,2)
        self.assertTrue(marker.is_file())
        self.assertTrue(self.app.is_file())

    def test_explicit_central_mode_same_uninstall(self):
        self.env['GASTON_ENGINE_MODE']='central'
        result=self.dev('\nUNINSTALL\n','uninstall')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.memory.read_text(),'Product decision retained\n')


if __name__ == '__main__':
    unittest.main()
