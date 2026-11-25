"""Source-free project adapter bootstrap and continuity boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI_SOURCE = Path(__file__).resolve().parent.parent / 'gaston_cli.py'


class CentralProjectBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.app = self.project / 'src' / 'app.py'
        self.app.parent.mkdir()
        self.app.write_text('print("application owned")\n', encoding='utf-8')
        self.env = dict(os.environ, GASTON_ENGINE_MODE='central')
        self.env.pop('GASTON_ROOT', None)
        self.env.pop('GASTON_ENGINE_PROJECT', None)

    def dev(self, *arguments, cwd=None):
        return subprocess.run([sys.executable, str(CLI_SOURCE), *arguments],
                              cwd=cwd or self.project, env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_explicit_init_does_not_install_tool_sources_or_touch_application(self):
        first = self.dev('init')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        marker = self.project / '.gaston/project.json'
        data = json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual(data['schema'], 1)
        self.assertEqual(data['kind'], 'gaston-project')
        self.assertEqual(data['engine'], 'gaston-agentic-cli')
        self.assertTrue(data['created_with'])
        self.assertEqual(self.app.read_text(encoding='utf-8'), 'print("application owned")\n')
        for unwanted in ('.agentic', '.github', 'AGENTS.md', 'CLAUDE.md', 'dev',
                         '.gaston/launcher.py', '.gaston/eng.py'):
            self.assertFalse((self.project / unwanted).exists(), unwanted)
        help_run = self.dev('--help')
        self.assertEqual(help_run.returncode, 0, help_run.stdout + help_run.stderr)
        self.assertIn('Commands:', help_run.stdout)
        self.assertFalse((self.project / '.agentic').exists())

    def test_repeated_init_is_idempotent_and_preserves_context(self):
        project_memory = self.project / '.gaston/context/DECISIONS.md'
        project_memory.parent.mkdir(parents=True)
        project_memory.write_text('Decision: keep architecture\n', encoding='utf-8')
        self.assertEqual(self.dev('init').returncode, 0)
        marker = self.project / '.gaston/project.json'
        original = marker.read_bytes()
        second = self.dev('init')
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('already exists', second.stdout)
        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(project_memory.read_text(encoding='utf-8'),
                         'Decision: keep architecture\n')

    def test_uninitialized_project_does_not_auto_initialize(self):
        result = self.dev('--help')
        self.assertEqual(result.returncode, 2)
        self.assertIn('No Gaston project found', result.stderr)
        self.assertFalse((self.project / '.gaston').exists())

    def test_setup_alias_is_rejected_with_native_init_guidance(self):
        result = self.dev('init', '--setup')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('Run `dev init` in an interactive terminal', result.stderr)
        self.assertFalse((self.project / '.gaston').exists())

    def test_invalid_existing_marker_is_not_overwritten(self):
        folder = self.project / '.gaston'
        folder.mkdir()
        marker = folder / 'project.json'
        marker.write_text('not json', encoding='utf-8')
        result = self.dev('init')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'not json')
        self.assertTrue(self.app.is_file())

    def test_nested_directory_reuses_parent_project(self):
        self.assertEqual(self.dev('init').returncode, 0)
        inside = self.project / 'src'
        second = self.dev('init', cwd=inside)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse((inside / '.gaston').exists())

    def test_existing_legacy_project_is_not_modified(self):
        legacy = self.project / '.agentic'
        legacy.mkdir()
        launcher = legacy / 'launcher.py'
        launcher.write_text('legacy', encoding='utf-8')
        result = self.dev('init')
        self.assertEqual(result.returncode, 2)
        self.assertIn('migration is not available yet', result.stderr)
        self.assertFalse((self.project / '.gaston').exists())
        self.assertEqual(launcher.read_text(), 'legacy')

    def test_preserved_old_context_is_never_deleted(self):
        state = self.project / '.agentic/local/PROJECT_CONTEXT.md'
        state.parent.mkdir(parents=True)
        state.write_text('product history', encoding='utf-8')
        self.assertEqual(self.dev('init').returncode, 0)
        self.assertEqual(state.read_text(encoding='utf-8'), 'product history')

    def test_default_dev_init_creates_source_free_adapter(self):
        self.env.pop('GASTON_ENGINE_MODE')
        self.assertEqual(self.dev('init').returncode, 0)
        self.assertTrue((self.project / '.gaston/project.json').is_file())
        self.assertFalse((self.project / '.agentic').exists())
        result = self.dev('--help')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_dev_preserves_legacy_project(self):
        self.env.pop('GASTON_ENGINE_MODE')
        legacy = self.project / '.agentic'
        legacy.mkdir()
        (legacy / 'launcher.py').write_text(
            'def main():\n    return 0\n', encoding='utf-8')
        result = self.dev('--help')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.project / '.gaston').exists())

    def test_linked_adapter_directory_is_rejected(self):
        target = self.project / 'somewhere'
        target.mkdir()
        try:
            (self.project / '.gaston').symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink not available on this host')
        result = self.dev('init')
        self.assertEqual(result.returncode, 2)
        self.assertFalse((target / 'project.json').exists())


if __name__ == '__main__':
    unittest.main()
