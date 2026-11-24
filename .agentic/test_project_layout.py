"""Regression tests for global engine and project layout separation."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
import project_layout as layout


class ProjectLayoutTests(unittest.TestCase):
    def test_uninitialized_project_does_not_create_any_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.assertEqual(layout.find_project(root, allow_new=True), (root, 'new'))
            with self.assertRaises(ValueError):
                layout.find_project(root)
            self.assertFalse((root / '.gaston').exists())

    def test_central_and_legacy_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();child=root/'src'/'app';child.mkdir(parents=True)
            (root/'.gaston').mkdir()
            (root/'.gaston/project.json').write_text('{"layout":1}')
            self.assertEqual(layout.find_project(child),(root,'central'))
            (root/'.gaston/project.json').unlink()
            (root/'.agentic').mkdir()
            (root/'.agentic/launcher.py').write_text('')
            self.assertEqual(layout.find_project(child),(root,'legacy'))

    def test_linked_marker_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();outside=root/'other.json';outside.write_text('{}')
            (root/'.gaston').mkdir()
            try:
                (root/'.gaston/project.json').symlink_to(outside)
            except (OSError,NotImplementedError):
                self.skipTest('symlinks not available')
            with self.assertRaises(ValueError):
                layout.find_project(root)

    def test_explicit_workspace_root_and_module_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            with patch.dict(os.environ,{'GASTON_ENGINE_PROJECT':str(root)}):
                self.assertEqual(layout.project_root(),root)
            with patch.dict(os.environ,{'GASTON_ENGINE_PROJECT':'relative'}):
                with self.assertRaises(ValueError):layout.project_root()
        self.assertEqual(layout.engine_file('eng').name,'eng.py')
        for value in ('../eng','eng.py','_private',''):
            with self.subTest(value=value),self.assertRaises(ValueError):
                layout.engine_file(value)

    def test_installed_wheel_contains_core_modules(self):
        from importlib import metadata
        try:dist=metadata.distribution('gaston-agentic-cli')
        except metadata.PackageNotFoundError:self.skipTest('Installed wheel not present')
        names={str(f).replace('\\','/') for f in (dist.files or ())}
        self.assertIn('gaston_engine/eng.py',names)
        self.assertIn('gaston_engine/native_hooks.py',names)
        self.assertIn('gaston_engine/project_layout.py',names)


if __name__=='__main__':
    unittest.main()
