"""Offline signed-payload staging, activation and rollback safety tests."""
from __future__ import annotations

from importlib import metadata
import hashlib
import io
from contextlib import redirect_stdout
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
                 patch("central_update._refresh_project_hooks") as refresh:
                # Simulate redirected Windows stdout on every OS. Strict
                # encoding catches unsupported CLI glyphs before activation.
                with io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict") as console:
                    with redirect_stdout(console):
                        self.assertEqual(central_update.command(
                            [], root=self.root, confirm=confirm
                        ), 0)
                    console.flush()
                    self.assertIn(b" to v7.8.9", console.buffer.getvalue())
            self.assertEqual(len(prompts), 1)
            self.assertIn("[default: Yes]", prompts[0])
            self.assertEqual(central_update.pointer_file(self.root).exists(), answer == "")
            if answer == "":
                self.assertEqual(refresh.call_args.args[0], self.root)
                self.assertEqual(
                    refresh.call_args.args[1],
                    self.root / "versions/v7.8.9/gaston_engine/native_hooks.py"
                )
            else:
                refresh.assert_not_called()

    def test_current_global_update_never_prompts_and_repairs_hooks(self):
        script = self.root / "gaston_engine/native_hooks.py"
        script.parent.mkdir(parents=True)
        script.write_text("# active hooks", encoding="utf-8")
        with patch.dict(os.environ, {"GASTON_GLOBAL_HOME":str(self.root)}), \
             patch("updater.latest", return_value={"tag":"v"+metadata.version("gaston-agentic-cli")}), \
             patch("central_update._current_hook_script", return_value=script), \
             patch("central_update._refresh_project_hooks") as refresh:
            self.assertEqual(central_update.command(
                [], root=self.root,
                confirm=lambda _: self.fail("unexpected prompt")
            ), 0)
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args, (self.root, script))

    def test_update_keeps_project_files_and_does_not_scan_project_state(self):
        project = self.root / "project"
        global_home = self.root / "global"
        (project / ".gaston").mkdir(parents=True)
        marker = project / ".gaston/project.json"
        marker.write_text('{"schema":1,"kind":"gaston-project"}', encoding="utf-8")
        legacy = project / ".agentic"
        legacy.mkdir()
        legacy_file = legacy / "launcher.py"
        legacy_file.write_text("keep legacy installed files", encoding="utf-8")
        local = project / ".gaston/local/playwright-tests/node_modules/.bin"
        local.mkdir(parents=True)
        outside = self.root / "playwright-core-target"
        outside.write_text("target remains unchanged", encoding="utf-8")
        link = local / "playwright-core"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("Symlinks unavailable: " + str(exc))
        manifest, payload = candidate()
        output = []
        with patch.dict(os.environ, {"GASTON_GLOBAL_HOME":str(global_home)}), \
             patch("updater.latest", return_value={"tag":"v7.8.9"}), \
             patch("updater.signed_release_commit", return_value="a"*40), \
             patch("updater._download", return_value=b"archive"), \
             patch("updater._validate_candidate", return_value=(manifest,payload)), \
             patch("central_update._refresh_project_hooks") as refresh:
            self.assertEqual(central_update.command(
                [], root=project, confirm=lambda _: "", output=output.append
            ), 0)
        self.assertTrue(legacy_file.is_file())
        self.assertEqual(legacy_file.read_text(encoding="utf-8"), "keep legacy installed files")
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"schema":1,"kind":"gaston-project"}')
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(strict=True), outside)
        self.assertEqual(outside.read_text(encoding="utf-8"), "target remains unchanged")
        refresh.assert_called_once()

    def test_project_hook_refresh_retargets_only_managed_claude_and_codex_hooks(self):
        import native_hooks
        project = self.root / "project"
        (project / ".gaston").mkdir(parents=True)
        (project / ".gaston/project.json").write_text("{}", encoding="utf-8")
        global_home = self.root / "global"
        old = global_home / "versions/v1.6.8/gaston_engine/native_hooks.py"
        current = global_home / "versions/v7.8.9/gaston_engine/native_hooks.py"
        base = global_home / "bootstrap/site-packages/gaston_engine/native_hooks.py"
        for path in (old, current, base):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# hook fixture", encoding="utf-8")

        events = ("SessionStart", "SessionEnd", "PreToolUse", "Stop")
        configs = {}
        for provider, relative in (
            ("claude", Path(".claude") / "settings.local.json"),
            ("codex", Path(".codex") / "hooks.json"),
        ):
            path = project / relative
            path.parent.mkdir(parents=True)
            hooks = {
                event: [{"hooks": [native_hooks.hook_def(provider, event, str(old))]}]
                for event in events
            }
            hooks["SessionStart"].append({
                "hooks": [{"type": "command", "command": "unrelated-provider-hook"}]
            })
            path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
            configs[provider] = path

        class Distribution:
            def locate_file(self, name):
                if name != "gaston_engine/native_hooks.py":
                    raise AssertionError(name)
                return base

        output = []
        previous_mode = os.environ.get("GASTON_ENGINE_MODE")
        previous_project = os.environ.get("GASTON_ENGINE_PROJECT")
        with patch.dict(os.environ, {"GASTON_GLOBAL_HOME":str(global_home)}), \
             patch("importlib.metadata.distribution", return_value=Distribution()):
            refreshed = central_update._refresh_project_hooks(
                project, current, output=output.append
            )
        self.assertEqual(refreshed, {"claude": True, "codex": True})
        self.assertEqual(os.environ.get("GASTON_ENGINE_MODE"), previous_mode)
        self.assertEqual(os.environ.get("GASTON_ENGINE_PROJECT"), previous_project)
        for provider, path in configs.items():
            data = json.loads(path.read_text(encoding="utf-8"))["hooks"]
            for event in events:
                commands = [
                    hook["command"]
                    for group in data[event]
                    for hook in group.get("hooks", [])
                    if hook.get("type") == "command"
                ]
                managed = [
                    command for command in commands
                    if (lambda args: len(args) == 3 and args[-1] == provider)(
                        native_hooks._hook_command_args(command, provider)
                    )
                ]
                self.assertEqual(len(managed), 1, (provider, event, commands))
                self.assertEqual(
                    Path(native_hooks._hook_command_args(managed[0], provider)[1]),
                    current
                )
            self.assertIn("unrelated-provider-hook", [
                hook.get("command")
                for group in data["SessionStart"]
                for hook in group.get("hooks", [])
            ])

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
