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
