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

    def test_installed_engine_contains_core_modules(self):
        # GitHub Actions also runs in a checkout with local .egg-info metadata;
        # its file list can be source-relative even when the installed wheel is
        # correct. Inspect the actual imported package instead.
        from importlib import util
        spec=util.find_spec('gaston_engine')
        if spec is None or spec.origin is None:
            self.skipTest('Installed Gaston runtime not present')
        directory=Path(spec.origin).resolve().parent
        self.assertTrue((directory/'eng.py').is_file())
        self.assertTrue((directory/'native_hooks.py').is_file())
        self.assertTrue((directory/'project_layout.py').is_file())


if __name__=='__main__':
    unittest.main()
