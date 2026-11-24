"""Regression coverage for the shipped package and uninstall safety boundary."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import uninstall

SOURCE = Path(__file__).resolve().parent.parent


class UninstallPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home),
                        CLAUDE_CONFIG_DIR=str(self.home / '.claude'),
                        CODEX_HOME=str(self.home / '.codex'))
        self.manifest = json.loads((SOURCE / uninstall.MANIFEST).read_text())
        self.files = self.manifest['files']
        for relative in self.files:
            src, dest = SOURCE / relative, self.root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        shutil.copy2(SOURCE / uninstall.MANIFEST, self.root / uninstall.MANIFEST)
        self.application = self.root / 'sample-backend' / 'src' / 'Main.java'
        self.application.parent.mkdir(parents=True)
        self.application.write_text('class Main {}')
        self.project_doc = self.root / 'docs' / 'APPLICATION.md'
        self.project_doc.write_text('Application-specific documentation')
        self.state = self.root / '.agentic/local'
        self.state.mkdir()
        (self.state / 'task.json').write_text('{}')

    def cli(self, text):
        return subprocess.run([str(self.root / 'dev'), 'uninstall'], cwd=self.root,
                              env=self.env, input=text, capture_output=True,
                              text=True, timeout=20)

    def test_manifest_all_paths_supported_and_exact_hashes(self):
        for rel, sha in self.files.items():
            with self.subTest(rel=rel):
                self.assertTrue(uninstall._known_manifest_path(rel))
                self.assertEqual(hashlib.sha256((SOURCE / rel).read_bytes()).hexdigest(), sha)

    def test_real_cli_clean_uninstall(self):
        result = self.cli('\nUNINSTALL\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Gaston Agentic uninstalled', result.stdout)
        self.assertTrue(self.application.is_file())
        self.assertTrue(self.project_doc.is_file())
        for rel in self.files:
            self.assertFalse((self.root / rel).exists(), rel)
        for rel in ('.agents', '.claude', 'dev'):
            self.assertFalse((self.root / rel).exists(), rel)
        self.assertTrue((self.root / '.agentic/local/task.json').exists())

    def test_full_erase_needs_separate_confirmation_and_removes_local_data(self):
        task_dir = self.root / '.agentic/tasks'
        task_dir.mkdir()
        (task_dir / 'plan.json').write_text('{}')
        result = self.cli('erase\nERASE GASTON DATA\nUNINSTALL\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / '.agentic/local').exists())
        self.assertFalse(task_dir.exists())
        self.assertFalse((self.root / '.agentic').exists())
        self.assertTrue(self.application.exists())
        self.assertTrue(self.project_doc.exists())

    def test_full_erase_wrong_confirmation_does_nothing(self):
        result = self.cli('erase\nwrong\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Cancelled', result.stdout)
        self.assertTrue((self.root / 'dev').exists())
        self.assertTrue((self.state / 'task.json').exists())

    def test_keeps_history_and_project_context_on_default(self):
        registry = self.state / 'project-context-sources.json'
        registry.write_text('{"version":1,"paths":["docs/APPLICATION.md"]}')
        runs = self.root / '.agentic/runs'
        runs.mkdir()
        (runs / 'evidence.json').write_text('preserve')
        result = self.cli('\nUNINSTALL\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(registry.read_text(), '{"version":1,"paths":["docs/APPLICATION.md"]}')
        self.assertEqual((runs / 'evidence.json').read_text(), 'preserve')
        self.assertFalse((self.root / '.agentic/eng.py').exists())
        self.assertTrue(self.project_doc.exists())

    def test_cancel_preserves_project_and_toolkit(self):
        result = self.cli('cancel\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Cancelled', result.stdout)
        for rel in self.files:
            self.assertTrue((self.root / rel).is_file(), rel)
        self.assertTrue(self.state.is_dir())

    def test_modified_docs_preserved_by_default(self):
        readme = self.root / 'README.md'
        readme.write_text('User-modified docs')
        result = self.cli('\n\nUNINSTALL\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(readme.read_text(), 'User-modified docs')
        self.assertTrue(self.project_doc.exists())
        self.assertTrue((self.root / '.agentic/local/task.json').exists())

    def test_unrelated_docs_and_application_paths_rejected(self):
        for rel in ('docs/APPLICATION.md', 'sample-backend/src/Main.java',
                    '../outside.txt', '/tmp/outside.txt', '.agentic/../README.md'):
            with self.subTest(rel=rel):
                forged = {**self.manifest, 'files': {**self.files, rel: '0' * 64}}
                (self.root / uninstall.MANIFEST).write_text(json.dumps(forged))
                result = self.cli('\nUNINSTALL\n')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('Manifest contains paths outside the toolkit', result.stderr)
                self.assertIn(rel, result.stderr)
                self.assertTrue(self.application.exists())
                self.assertTrue(self.project_doc.exists())
                self.assertTrue(self.state.exists())

    def test_linked_document_parent_refused(self):
        target = self.base / 'outside-docs'
        (self.root / 'docs').rename(target)
        (self.root / 'docs').symlink_to(target, target_is_directory=True)
        result = self.cli('\nUNINSTALL\n')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('under symbolic links', result.stderr)
        self.assertTrue((target / 'VERIFICATION.md').exists())
        self.assertTrue(self.state.exists())

    def test_unknown_cache_files_preserved(self):
        cache = self.root / '.agentic/__pycache__'
        cache.mkdir()
        unknown = cache / 'custom.cpython-311.pyc'
        unknown.write_bytes(b'not Gaston')
        result = self.cli('\nUNINSTALL\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(unknown.read_bytes(), b'not Gaston')
        self.assertEqual(list(cache.iterdir()), [unknown])

    def test_active_session_refuses_uninstall(self):
        lock = self.state / 'locks' / 'claude-reviewer'
        lock.mkdir(parents=True)
        result = self.cli('\nUNINSTALL\n')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('session or worker is active', result.stderr)
        self.assertTrue((self.root / 'dev').exists())


if __name__ == '__main__':
    unittest.main()
