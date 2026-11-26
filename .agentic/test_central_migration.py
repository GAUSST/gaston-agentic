"""Offline acceptance tests for explicit, lossless legacy workspace migration."""
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

CLI = Path(__file__).resolve().parent.parent / "gaston_cli.py"
ENGINE = Path(__file__).resolve().parent


class CentralMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project_file = self.root / "src" / "main.py"
        self.project_file.parent.mkdir()
        self.project_file.write_text("print('mine')\n", encoding="utf-8")
        old = self.root / ".agentic"
        old.mkdir()
        file_hashes = {}
        for filename in ("__init__.py", "launcher.py", "eng.py"):
            source = ENGINE / filename
            target = old / filename
            shutil.copy2(source, target)
            file_hashes[".agentic/" + filename] = hashlib.sha256(target.read_bytes()).hexdigest()
        (old / "manifest.json").write_text(json.dumps({
            "version": 1, "release": "v1.5.5", "build": "1.5.5",
            "files": file_hashes
        }), encoding="utf-8")
        self.context = old / "local" / "PROJECT_CONTEXT.md"
        self.context.parent.mkdir(parents=True)
        self.context.write_text("History and decisions\n", encoding="utf-8")
        self.settings = old / "config.json"
        self.settings.write_text(json.dumps({"preferences":{"builder":"codex","reviewer":"claude"}}))
        self.settings_bytes = self.settings.read_bytes()
        self.env = os.environ.copy()
        for key in ("GASTON_ROOT", "GASTON_ENGINE_MODE", "GASTON_ENGINE_PROJECT",
                    "GASTON_ACTIVE_VERSION"):
            self.env.pop(key, None)

    def dev(self, answer, *args):
        return subprocess.run([sys.executable, str(CLI), *args],
                              cwd=self.root, env=self.env, input=answer,
                              text=True, capture_output=True, timeout=35)

    def test_explicit_migration_preserves_history_and_application(self):
        result = self.dev("MIGRATE GASTON\n", "migrate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / ".gaston/project.json").is_file())
        self.assertEqual((self.root / ".gaston/local/PROJECT_CONTEXT.md").read_text(),
                         "History and decisions\n")
        self.assertEqual((self.root / ".gaston/config.json").read_bytes(),
                         self.settings_bytes)
        backup = self.root / ".gaston/migration-backups/legacy-agentic"
        self.assertFalse((self.root / ".agentic").exists())
        self.assertEqual((backup / "local/PROJECT_CONTEXT.md").read_text(),
                         "History and decisions\n")
        self.assertEqual((backup / "config.json").read_bytes(), self.settings_bytes)
        self.assertTrue((backup / "manifest.json").is_file())
        self.assertTrue((backup / "eng.py").is_file())
        self.assertEqual(self.project_file.read_text(), "print('mine')\n")
        ready = self.dev("", "--help")
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertFalse((self.root / ".agentic").exists())

    def test_existing_central_project_archives_legacy_backup_and_removes_root_folder(self):
        central = self.root / ".gaston"
        central.mkdir()
        (central / "project.json").write_text(json.dumps({
            "schema": 1, "kind": "gaston-project",
            "engine": "gaston-agentic-cli", "created_with": "1.6.10",
            "migrated_from": "legacy"
        }), encoding="utf-8")
        (central / ".gitignore").write_text("local/\n", encoding="utf-8")
        current = central / "local" / "CURRENT_CONTEXT.md"
        current.parent.mkdir(parents=True)
        current.write_text("New central state stays intact\n", encoding="utf-8")
        # Simulate v1.6.10's leftover compatibility directory after its first migration.
        (self.root / ".agentic/local/PROJECT_CONTEXT.md").write_text(
            "Legacy backup\n", encoding="utf-8")
        result = self.dev("MIGRATE GASTON\n", "migrate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / ".agentic").exists())
        self.assertEqual(current.read_text(), "New central state stays intact\n")
        self.assertEqual((central / "migration-backups/legacy-agentic/local/PROJECT_CONTEXT.md").read_text(),
                         "Legacy backup\n")
        self.assertIn("migration-backups/", (central / ".gitignore").read_text())

    def test_existing_central_conflict_stops_without_overwriting_either_copy(self):
        central = self.root / ".gaston"
        central.mkdir()
        (central / "project.json").write_text(json.dumps({
            "schema": 1, "kind": "gaston-project",
            "engine": "gaston-agentic-cli", "created_with": "1.6.10"
        }), encoding="utf-8")
        (central / "config.json").write_text('{"central":"keep"}\n', encoding="utf-8")
        result = self.dev("MIGRATE GASTON\n", "migrate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Conflicting project state", result.stderr)
        self.assertEqual((central / "config.json").read_text(), '{"central":"keep"}\n')
        self.assertEqual(self.settings.read_bytes(), (self.root / ".agentic/config.json").read_bytes())
        self.assertTrue((self.root / ".agentic/launcher.py").is_file())

    def test_legacy_development_db_moves_under_central_state_with_backup(self):
        folder = self.root / ".gaston-dev-db"
        folder.mkdir(mode=0o700)
        workspace = hashlib.sha256(str(self.root).encode()).hexdigest()[:20]
        credentials = {
            "workspace": workspace,
            "admin_password": "a" * 48,
            "development_password": "b" * 48,
            "readonly_password": "c" * 48
        }
        source = folder / "credentials.json"
        source.write_text(json.dumps(credentials), encoding="utf-8")
        if os.name != "nt":
            os.chmod(folder, 0o700)
            os.chmod(source, 0o600)
        result = self.dev("MIGRATE GASTON\n", "migrate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        destination = self.root / ".gaston/local/dev-db/credentials.json"
        self.assertEqual(json.loads(destination.read_text()), credentials)
        self.assertFalse(folder.exists())
        backup = self.root / ".gaston/local/dev-db/migration-backups/gaston-dev-db/credentials.json"
        self.assertEqual(json.loads(backup.read_text()), credentials)

    def test_dev_db_only_cleanup_works_on_already_central_project(self):
        central = self.root / ".gaston"
        central.mkdir()
        (central / "project.json").write_text(json.dumps({
            "schema": 1, "kind": "gaston-project",
            "engine": "gaston-agentic-cli", "created_with": "1.6.10"
        }), encoding="utf-8")
        folder = self.root / ".gaston-dev-db"
        folder.mkdir(mode=0o700)
        workspace = hashlib.sha256(str(self.root).encode()).hexdigest()[:20]
        source = folder / "credentials.json"
        source.write_text(json.dumps({
            "workspace": workspace, "admin_password": "a" * 48,
            "development_password": "b" * 48, "readonly_password": "c" * 48
        }), encoding="utf-8")
        if os.name != "nt":
            os.chmod(folder, 0o700)
            os.chmod(source, 0o600)
        result = self.dev("MIGRATE GASTON\n", "migrate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(folder.exists())
        self.assertTrue((central / "local/dev-db/credentials.json").is_file())

    def test_cancel_is_nondestructive(self):
        result = self.dev("not now\n", "migrate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / ".gaston").exists())
        self.assertTrue((self.root / ".agentic/launcher.py").exists())
        self.assertEqual(self.context.read_text(), "History and decisions\n")

    def test_modified_legacy_engine_is_rejected(self):
        (self.root / ".agentic/eng.py").write_text("user-modified source")
        result = self.dev("MIGRATE GASTON\n", "migrate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Modified Gaston engine", result.stderr)
        self.assertFalse((self.root / ".gaston").exists())
        self.assertTrue(self.project_file.is_file())

    def test_linked_state_is_rejected_without_following_it(self):
        outside = self.root / "private.txt"
        outside.write_text("outside")
        linked = self.root / ".agentic/local/outside"
        try:
            linked.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        result = self.dev("MIGRATE GASTON\n", "migrate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(outside.read_text(), "outside")
        self.assertFalse((self.root / ".gaston").exists())

    def test_managed_active_worker_blocks_migration(self):
        locks = self.root / ".agentic/local/locks"
        locks.mkdir(parents=True)
        (locks / "reviewer.lock").write_text("active")
        result = self.dev("MIGRATE GASTON\n", "migrate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Close all Gaston sessions", result.stderr)
        self.assertTrue((self.root / ".agentic/launcher.py").exists())


if __name__ == "__main__":
    unittest.main()
