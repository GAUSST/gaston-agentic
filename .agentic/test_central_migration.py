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
                         self.settings.read_bytes())
        self.assertEqual(self.context.read_text(), "History and decisions\n")
        self.assertFalse((self.root / ".agentic/eng.py").exists())
        self.assertFalse((self.root / ".agentic/launcher.py").exists())
        self.assertEqual(self.project_file.read_text(), "print('mine')\n")
        ready = self.dev("", "--help")
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertFalse((self.root / ".agentic/eng.py").exists())

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
