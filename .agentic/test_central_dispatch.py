"""Regression tests for opt-in installed-engine execution, without migration."""
from __future__ import annotations

from importlib import metadata, util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import gaston_cli

CLI_SOURCE = Path(__file__).resolve().parent.parent / 'gaston_cli.py'


class CentralDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        toolkit = self.project / '.agentic'
        toolkit.mkdir()
        # These are poison files: central core must never execute either.
        (toolkit / 'launcher.py').write_text(
            "raise RuntimeError('PROJECT LOCAL LAUNCHER EXECUTED')\n", encoding='utf-8')
        (toolkit / 'eng.py').write_text(
            "raise RuntimeError('PROJECT LOCAL ENGINE EXECUTED')\n", encoding='utf-8')
        self.manifest = toolkit / 'manifest.json'
        self.version = metadata.version('gaston-agentic-cli')
        self.manifest.write_text(json.dumps({'build': self.version, 'files': {}}), encoding='utf-8')

    def cli(self, *args):
        env = os.environ.copy()
        env['GASTON_ENGINE_MODE'] = 'central'
        env.pop('GASTON_ROOT', None)
        env.pop('GASTON_ENGINE_PROJECT', None)
        result = subprocess.run(
            [sys.executable, str(CLI_SOURCE), *args],
            cwd=self.project, env=env, text=True, capture_output=True, timeout=30)
        return result

    def test_explicit_opt_in_uses_installed_core_not_project_engine(self):
        spec = util.find_spec('gaston_engine')
        if spec is None or spec.origin is None:
            self.skipTest('Install CLI distribution before testing central mode')
        result = self.cli('--help')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Commands:', result.stdout)
        self.assertNotIn('PROJECT LOCAL', result.stdout + result.stderr)
        self.assertFalse((self.project / '.agentic/evolution.py').exists())
        self.assertFalse((self.project / '.agentic/local').exists(),
                         'read-only preview must never create a legacy evolution lock')
        self.assertFalse((self.project / '.gaston').exists())

    def test_version_mismatch_fails_closed(self):
        self.manifest.write_text('{"build": "0.0.0", "files": {}}', encoding='utf-8')
        result = self.cli('--help')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('same version', result.stderr)
        self.assertFalse((self.project / '.gaston').exists())

    def test_preview_only_allows_help(self):
        for arguments in (('update',), ('uninstall',), ('models',),
                          ('sessions',), ('init',), ()):
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('supports dev --help only', result.stderr)
                self.assertTrue(self.manifest.is_file())
                self.assertFalse((self.project / '.agentic/local').exists())
                self.assertFalse((self.project / '.gaston').exists())

    def test_uninitialized_project_not_auto_initialized(self):
        (self.project / '.agentic/launcher.py').unlink()
        result = self.cli('--help')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('No Gaston workspace', result.stderr)
        self.assertFalse((self.project / '.gaston').exists())

    def test_default_path_remains_legacy(self):
        with patch.dict(os.environ, {'GASTON_ENGINE_MODE': 'central'}), \
             patch.object(gaston_cli, 'workspace', return_value=self.project), \
             patch.object(gaston_cli, '_central_launch', return_value=17) as central:
            self.assertEqual(gaston_cli.main(), 17)
            central.assert_called_once_with(self.project)


if __name__ == '__main__':
    unittest.main()
