"""Bootstrap tests, including opt-in installation of the shipped distribution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import central_install as installer


class CentralInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.destination = self.root / 'snapshot'
        self.manifest = self.source / '.agentic/manifest.json'
        self.manifest.parent.mkdir()
        (self.source / 'owned.py').write_bytes(b'print(1)\n')
        (self.source / 'private.txt').write_text('not shipped')
        self.payload = {'build': '1.6.0', 'files': {
            'owned.py': hashlib.sha256(b'print(1)\n').hexdigest()}}
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.payload), encoding='utf-8')

    def test_snapshot_only_copies_manifest_owned_files(self):
        self.assertEqual(installer.source_snapshot(self.source, self.destination), '1.6.0')
        self.assertEqual((self.destination / 'owned.py').read_bytes(), b'print(1)\n')
        self.assertFalse((self.destination / 'private.txt').exists())

    def test_snapshot_rejects_checksum_mismatch(self):
        (self.source / 'owned.py').write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            installer.source_snapshot(self.source, self.destination)

    def test_snapshot_rejects_traversal_and_drive_paths(self):
        for path in ('../outside.py', 'C:/outside.py', '/outside.py', 'a\\b'):
            with self.subTest(path=path):
                self.payload['files'] = {path: '0' * 64}
                self.write_manifest()
                with self.assertRaisesRegex(RuntimeError, 'Unsafe manifest path'):
                    installer.source_snapshot(self.source, self.destination)

    def test_does_not_remove_unowned_bootstrap(self):
        base = self.root / 'global'
        existing = base / 'bootstrap'
        existing.mkdir(parents=True)
        protected = existing / 'my-data.txt'
        protected.write_text('keep')
        with patch.object(installer, 'home', return_value=base):
            with self.assertRaisesRegex(RuntimeError, 'not owned'):
                installer.install(self.source)
        self.assertEqual(protected.read_text(), 'keep')

    def test_install_failure_removes_only_new_environment(self):
        base = self.root / 'global'
        base.mkdir()
        protected = base / 'my-data.txt'
        protected.write_text('keep')
        with patch.object(installer, 'home', return_value=base), \
             patch.object(installer.venv.EnvBuilder, 'create', side_effect=OSError('failed')):
            with self.assertRaisesRegex(OSError, 'failed'):
                installer.install(self.source)
        self.assertFalse((base / 'bootstrap').exists())
        self.assertEqual(protected.read_text(), 'keep')

    @unittest.skipIf(os.name == 'nt', 'POSIX path registration')
    def test_register_path_preserves_unrelated_command(self):
        home = self.root / 'home'
        command = home / '.local/bin/dev'
        command.parent.mkdir(parents=True)
        command.write_text('unrelated')
        with patch.object(installer.Path, 'home', return_value=home):
            with self.assertRaisesRegex(RuntimeError, 'unrelated dev'):
                installer.register_path(self.root / 'env')
        self.assertEqual(command.read_text(), 'unrelated')

    @unittest.skipUnless(os.environ.get('GASTON_INSTALL_SMOKE') == '1', 'opt-in distribution smoke')
    def test_real_install_and_two_projects_without_checkout(self):
        # The copy is intentionally deleted before the native entry point runs.
        shutil.rmtree(self.source)
        version = installer.source_snapshot(installer.ROOT, self.source)
        home = self.root / 'home'
        home.mkdir()
        base = home / 'engine'
        env = {k: v for k, v in os.environ.items() if k not in (
            'PYTHONPATH', 'PYTHONHOME', 'GASTON_ROOT', 'GASTON_ENGINE_PROJECT',
            'GASTON_ENGINE_MODE', 'GASTON_ACTIVE_VERSION', 'VIRTUAL_ENV')}
        env.update(HOME=str(home), USERPROFILE=str(home), GASTON_GLOBAL_HOME=str(base), SHELL='/bin/bash')
        bootstrap = self.source / '.agentic/central_install.py'
        args = [sys.executable, str(bootstrap)]
        wheel = os.environ.get('GASTON_INSTALL_SMOKE_WHEEL')
        if wheel:
            args += ['--wheel', str(Path(wheel).resolve())]
        if os.name == 'nt':
            # A regression test must not write the real user's registry.
            # Exercise the native executable, but isolate PATH registration.
            code = ('import sys; from pathlib import Path; '
                    'sys.path.insert(0, sys.argv.pop(1)); import central_install as i; '
                    'i.register_path = lambda env: None; raise SystemExit(i.main())')
            args = [sys.executable, '-c', code, str(bootstrap.parent), *args[2:]]
        for _ in range(2):
            result = subprocess.run(args, cwd=self.source, env=env, text=True,
                                    capture_output=True, timeout=240)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shutil.rmtree(self.source)
        command = (base / 'bootstrap/Scripts/dev.exe' if os.name == 'nt'
                   else home / '.local/bin/dev')
        for name in ('project-one', 'project-two'):
            project = self.root / name
            project.mkdir()
            app = project / 'app.txt'
            app.write_text('user owned')
            def dev(*arguments):
                return subprocess.run([str(command), *arguments], cwd=project, env=env,
                                      text=True, capture_output=True, timeout=30)
            self.assertNotEqual(dev().returncode, 0)
            self.assertFalse((project / '.gaston').exists())
            result = dev('init')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            marker = project / '.gaston/project.json'
            self.assertEqual(json.loads(marker.read_text())['created_with'], version)
            self.assertEqual(dev('init').returncode, 0)
            self.assertEqual(dev('--help').returncode, 0)
            self.assertFalse((project / '.agentic').exists())
            self.assertEqual(app.read_text(), 'user owned')


if __name__ == '__main__':
    unittest.main()
