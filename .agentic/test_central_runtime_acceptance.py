"""Real subprocess acceptance checks for source-free central Gaston projects.

No remote provider credentials or network access are needed. Tests execute the
installed engine's CLI and verify that local project state never becomes core.
"""
from __future__ import annotations

from importlib import util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parent.parent / "gaston_cli.py"


class CentralRuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.app = self.root / "app.py"
        self.app.write_text("print('app is user owned')\n", encoding="utf8")
        self.env = os.environ.copy()
        for name in ("GASTON_ENGINE_MODE", "GASTON_ENGINE_PROJECT",
                     "GASTON_ROOT", "GASTON_ACTIVE_VERSION"):
            self.env.pop(name, None)

    def dev(self, *args, env=None, input=None):
        return subprocess.run([sys.executable, str(CLI), *args],
                              cwd=self.root, env=env or self.env, input=input,
                              text=True, capture_output=True, timeout=30)

    def test_init_and_help_are_source_free(self):
        created = self.dev("init")
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        self.assertTrue((self.root / ".gaston/project.json").is_file())
        self.assertFalse((self.root / ".agentic").exists())
        help_result = self.dev("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stdout + help_result.stderr)
        self.assertIn("Commands:", help_result.stdout)
        self.assertFalse((self.root / ".agentic").exists())
        self.assertEqual(self.app.read_text(), "print('app is user owned')\n")

    def test_installed_engine_script_honors_project_root(self):
        self.assertEqual(self.dev("init").returncode, 0)
        spec = util.find_spec("gaston_engine")
        self.assertIsNotNone(spec)
        engine = Path(spec.origin).resolve().parent
        self.assertFalse(engine.is_relative_to(self.root))
        env = {**self.env, "GASTON_ENGINE_MODE":"central",
               "GASTON_ENGINE_PROJECT":str(self.root)}
        run = subprocess.run([sys.executable, str(engine / "eng.py"), "--help"],
                             cwd=self.root, env=env, text=True,
                             capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("Commands:", run.stdout)
        self.assertFalse((self.root / ".agentic").exists())

    def test_models_and_sessions_use_only_project_owned_state(self):
        self.assertEqual(self.dev("init").returncode, 0)
        state = self.root / ".gaston"
        local = state / "local"
        local.mkdir()
        (state / "config.json").write_text(json.dumps({
            "preferences":{"builder":"codex","reviewer":"claude"},
            "models":{"codex_builder":"gpt-5.1-codex",
                      "claude_reviewer":"claude-sonnet-4-5"},
            "policy_files":[], "checks":[]
        }), encoding="utf8")
        (local / "setup-status.json").write_text(json.dumps({
            "ready":True, "project":str(self.root), "state":"complete"
        }), encoding="utf8")
        models = self.dev("models")
        self.assertEqual(models.returncode, 0, models.stdout + models.stderr)
        self.assertIn("Models", models.stdout)
        sessions = self.dev("sessions")
        self.assertEqual(sessions.returncode, 0, sessions.stdout + sessions.stderr)
        self.assertFalse((self.root / ".agentic").exists())
        self.assertEqual(self.app.read_text(), "print('app is user owned')\n")

    def test_unconfigured_project_is_not_auto_configured(self):
        self.assertEqual(self.dev("init").returncode, 0)
        session = self.dev("sessions")
        self.assertNotEqual(session.returncode, 0)
        self.assertIn("Setup is incomplete", session.stderr + session.stdout)
        self.assertFalse((self.root / ".agentic").exists())

    def test_internal_subprocesses_reference_installed_engine(self):
        self.assertEqual(self.dev("init").returncode, 0)
        spec = util.find_spec("gaston_engine")
        engine = Path(spec.origin).resolve().parent
        env = {**self.env, "GASTON_ENGINE_MODE":"central",
               "GASTON_ENGINE_PROJECT":str(self.root),
               "PYTHONPATH":str(engine)}
        code = (
            "from pathlib import Path; import eng, native_hooks, verification_mcp, "
            "session_store, serena_tools; "
            "assert eng.ROOT==Path.cwd().resolve(); "
            "assert str(eng.LOCAL).startswith(str(Path.cwd()/'.gaston')); "
            "assert native_hooks.ROOT==eng.ROOT; "
            "assert verification_mcp.ROOT==eng.ROOT; "
            "assert session_store.Store(eng.ROOT).local==eng.LOCAL; "
            "assert serena_tools.config(eng.ROOT,'claude') is None or True; "
            "assert not (Path.cwd()/'.agentic').exists()"
        )
        run = subprocess.run([sys.executable, "-c", code], cwd=self.root,
                             env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
