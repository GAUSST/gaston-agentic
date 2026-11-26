"""Offline signed-payload staging, activation and rollback safety tests."""
from __future__ import annotations

from importlib import metadata
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import central_update
import gaston_cli


def candidate(tag="v7.8.9"):
    files = {
        ".agentic/__init__.py": b"# package\n",
        ".agentic/eng.py": b"# engine\n",
        "gaston_cli.py": b"def main():\n    return 57\n"
    }
    manifest = {
        "version":1, "release":tag, "build":tag[1:],
        "files":{key:hashlib.sha256(value).hexdigest() for key,value in files.items()}
    }
    data = json.dumps(manifest, sort_keys=True).encode()
    return manifest, {**{name:(blob,0o644) for name,blob in files.items()},
                      ".agentic/manifest.json":(data,0o644)}


class CentralUpdaterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_stage_activate_and_dispatch_real_global_pointer(self):
        manifest, payload = candidate()
        target = central_update.stage_verified_release(self.root, "v7.8.9", payload, manifest)
        identity = hashlib.sha256(payload[".agentic/manifest.json"][0]).hexdigest()
        central_update.activate(self.root, "v7.8.9", expected_manifest_sha256=identity)
        self.assertEqual(central_update.active_path(self.root), target)
        with patch.dict(os.environ, {"GASTON_GLOBAL_HOME":str(self.root)}, clear=False):
            os.environ.pop("GASTON_ACTIVE_VERSION", None)
            self.assertEqual(gaston_cli._managed_bootstrap(), 57)
        self.assertTrue((target / "gaston_engine/eng.py").is_file())

    def test_corrupt_engine_fails_closed(self):
        manifest, payload = candidate()
        target=central_update.stage_verified_release(self.root, "v7.8.9", payload, manifest)
        central_update.activate(self.root,"v7.8.9",
             expected_manifest_sha256=hashlib.sha256(payload[".agentic/manifest.json"][0]).hexdigest())
        (target/"gaston_engine/eng.py").write_text("tampered")
        with patch.dict(os.environ, {"GASTON_GLOBAL_HOME":str(self.root)}, clear=False):
            os.environ.pop("GASTON_ACTIVE_VERSION", None)
            with self.assertRaises(RuntimeError):
                gaston_cli._managed_bootstrap()

    def test_refuses_unsafe_archive_paths(self):
        manifest,payload=candidate()
        manifest["files"][".agentic/../../escape.py"]="0"*64
        payload[".agentic/../../escape.py"]=(b"fake",0o644)
        with self.assertRaises(central_update.CentralUpdateError):
            central_update.stage_verified_release(self.root,"v7.8.9",payload,manifest)
        self.assertFalse((self.root/"escape.py").exists())

    def test_refuses_overwrite_installed_version(self):
        manifest,payload=candidate()
        central_update.stage_verified_release(self.root,"v7.8.9",payload,manifest)
        with self.assertRaises(central_update.CentralUpdateError):
            central_update.stage_verified_release(self.root,"v7.8.9",payload,manifest)

    def test_init_notice_only_when_published_release_is_newer(self):
        output = []
        with patch("central_update.current_version", return_value="v1.6.12"), \
             patch("updater.latest", return_value={"tag":"v1.6.13"}):
            central_update.suggest_update(output=output.append)
        self.assertIn("Run dev update", output[0])
        output.clear()
        with patch("central_update.current_version", return_value="v1.6.13"), \
             patch("updater.latest", return_value={"tag":"v1.6.13"}):
            central_update.suggest_update(output=output.append)
        self.assertEqual(output, [])
        with patch("updater.latest", side_effect=RuntimeError("offline")):
            central_update.suggest_update(output=output.append)
        self.assertEqual(output, [])

    def test_global_update_default_yes_and_no(self):
        manifest, payload = candidate()
        for answer in ("n", ""):
            prompts = []
            def confirm(prompt):
                prompts.append(prompt)
                return answer
            with patch.dict(os.environ, {"GASTON_GLOBAL_HOME":str(self.root)}), \
                 patch("updater.latest", return_value={"tag":"v7.8.9"}), \
                 patch("updater.signed_release_commit", return_value="a"*40), \
                 patch("updater._download", return_value=b"archive"), \
                 patch("updater._validate_candidate", return_value=(manifest,payload)), \
                 patch("central_migration.migrate") as migration:
                self.assertEqual(central_update.command([], confirm=confirm), 0)
                migration.assert_not_called()
            self.assertEqual(len(prompts), 1)
            self.assertIn("[default: Yes]", prompts[0])
            self.assertEqual(central_update.pointer_file(self.root).exists(), answer == "")

    def test_current_global_update_never_prompts(self):
        with patch.dict(os.environ, {"GASTON_GLOBAL_HOME":str(self.root)}), \
             patch("updater.latest", return_value={"tag":"v"+metadata.version("gaston-agentic-cli")}):
            self.assertEqual(central_update.command([], confirm=lambda _: self.fail("unexpected prompt")), 0)

    def test_check_only_flag_is_not_a_supported_update_mode(self):
        with self.assertRaises(SystemExit) as raised:
            central_update.command(["--check"])
        self.assertEqual(raised.exception.code, 2)

    def test_rollback_to_base_keeps_global_tool_installation(self):
        manifest,payload=candidate()
        central_update.stage_verified_release(self.root,"v7.8.9",payload,manifest)
        central_update.activate(self.root,"v7.8.9",
             expected_manifest_sha256=hashlib.sha256(payload[".agentic/manifest.json"][0]).hexdigest())
        pointer=central_update.pointer_file(self.root)
        state=json.loads(pointer.read_text())
        previous="v"+metadata.version("gaston-agentic-cli")
        state["previous"]=previous
        pointer.write_text(json.dumps(state))
        with patch.dict(os.environ, {"GASTON_GLOBAL_HOME":str(self.root)}):
            output=[]
            result=central_update.command(["--rollback"],
                  confirm=lambda prompt:"ROLLBACK "+previous,output=output.append)
        self.assertEqual(result,0)
        self.assertFalse(pointer.exists())
        self.assertTrue((self.root/"versions/v7.8.9/gaston_engine/eng.py").exists())


if __name__ == "__main__":
    unittest.main()
