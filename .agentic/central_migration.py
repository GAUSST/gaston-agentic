"""Explicit, data-preserving migration from legacy workspace Gaston to global engine.

Legacy source is never removed until the project context has been copied,
checksummed and the new engine has passed a read-only start check. Unknown,
modified, linked or user-owned files are never automatically removed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata

from project_layout import read_project_marker

STATE = ("config.json", "local", "tasks", "runs", "policies/custom")


class MigrationError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_tree_safe(path: Path):
    if path.is_symlink():
        raise MigrationError("Refusing linked project state: " + str(path))
    if path.is_dir():
        for parent, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                item = Path(parent) / name
                if item.is_symlink():
                    raise MigrationError("Refusing symbolic link inside project state: " + str(item))


def _copy_state(source: Path, destination: Path) -> None:
    for rel in STATE:
        old = source / rel
        if not old.exists():
            continue
        _assert_tree_safe(old)
        new = destination / rel
        new.parent.mkdir(parents=True, exist_ok=True)
        if old.is_dir():
            shutil.copytree(old, new, symlinks=False)
            for item in old.rglob("*"):
                if item.is_file() and _digest(item) != _digest(new / item.relative_to(old)):
                    raise MigrationError("Context copy checksum mismatch: " + rel)
        elif old.is_file():
            shutil.copy2(old, new)
            if _digest(old) != _digest(new):
                raise MigrationError("Context copy checksum mismatch: " + rel)
        else:
            raise MigrationError("Unknown project data type: " + rel)


def _legacy_owned_sources(root: Path, manifest: dict) -> list[Path]:
    old = root / ".agentic"
    if old.is_symlink() or not old.is_dir():
        raise MigrationError("Missing or linked legacy Gaston workspace")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise MigrationError("Legacy manifest is invalid")
    legacy_version = manifest.get("build")
    if not isinstance(legacy_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", legacy_version):
        raise MigrationError("Unsupported legacy Gaston release manifest")
    from project_layout import runtime_script
    selected = []
    for rel, expected in files.items():
        if not isinstance(rel, str) or not rel.startswith(".agentic/"):
            continue
        suffix = rel[len(".agentic/"):]
        if suffix == "__init__.py":
            installed = Path(__file__).resolve().parent / "__init__.py"
        elif "/" not in suffix and suffix.endswith(".py"):
            installed = runtime_script(suffix)
        elif suffix.startswith("policies/") and not suffix.startswith("policies/custom/"):
            installed = Path(__file__).resolve().parent / suffix
        else:
            continue
        original = root / rel
        if (original.is_symlink() or not original.is_file() or
                not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
            raise MigrationError("Legacy engine source is missing or linked: " + rel)
        if _digest(original) != expected:
            raise MigrationError("Modified Gaston engine file; migration leaves it untouched: " + rel)
        # Source removal is restricted to known installed engine module names.
        # Older signed legacy builds legitimately differ from the current
        # installed release. Their own manifest hash must still match the file.
        if not installed.is_file():
            raise MigrationError("Unrecognized legacy toolkit module: " + rel)
        selected.append(original)
    if not any(p.name == "launcher.py" for p in selected):
        raise MigrationError("Legacy launcher is not verifiably owned by Gaston")
    return selected


def _active(root: Path) -> bool:
    for rel in ("local/locks", "local/native-hooks/active", "local/native-hooks/feedback-worker.lock"):
        path = root / ".agentic" / rel
        if path.is_symlink() or (path.is_file()) or (path.is_dir() and any(path.iterdir())):
            return True
    return False


def migrate(root: Path, *, confirmation=input, output=print) -> bool:
    root = Path(root).resolve()
    old = root / ".agentic"
    target = root / ".gaston"
    if target.exists() or target.is_symlink():
        read_project_marker(root)
        output("Central project adapter already exists; context was not modified.")
        return False
    if not (old / "launcher.py").is_file() or (old / "manifest.json").is_symlink():
        raise MigrationError("Not a verified legacy Gaston workspace")
    if _active(root):
        raise MigrationError("Close all Gaston sessions and managed workers before migration")
    try:
        manifest = json.loads((old / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError("Invalid legacy workspace manifest") from exc
    to_remove = _legacy_owned_sources(root, manifest)
    output("GASTON CENTRAL — LEGACY WORKSPACE MIGRATION")
    output("  Project: " + str(root))
    output("  Durable state locations preserved: " + ", ".join(STATE))
    output("  Verified engine files to remove after smoke test: " + str(len(to_remove)))
    output("  Existing application source, Git, root documents and unknown files are untouched.")
    output("  Legacy state remains as a local compatibility backup until explicitly removed.")
    if confirmation("Type MIGRATE GASTON to approve: ").strip() != "MIGRATE GASTON":
        output("Migration cancelled; no files changed.")
        return False
    # Preflight again after confirmation, before the first filesystem mutation.
    if target.exists() or _active(root):
        raise MigrationError("Project state changed during confirmation")
    selected = _legacy_owned_sources(root, manifest)
    if selected != to_remove:
        raise MigrationError("Legacy toolkit changed; migration cancelled")
    staging = Path(tempfile.mkdtemp(prefix=".gaston-migration-", dir=root))
    published = False
    try:
        _copy_state(old, staging)
        marker = {
            "schema":1, "kind":"gaston-project", "engine":"gaston-agentic-cli",
            "created_with":metadata.version("gaston-agentic-cli"),
            "migrated_from":"legacy"
        }
        (staging / "project.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        (staging / ".gitignore").write_text(
            "local/\nruns/\ntasks/\nconfig.json\npolicies/custom/\n",
            encoding="utf-8")
        os.replace(staging, target)
        published = True
        environment = os.environ.copy()
        environment.update(GASTON_ENGINE_MODE="central", GASTON_ENGINE_PROJECT=str(root))
        engine = Path(__file__).resolve().parent / "eng.py"
        # This probes installed entrypoint without running provider setup or tasks.
        result = subprocess.run([sys.executable, str(engine), "--help"],
                                cwd=root, env=environment, capture_output=True,
                                text=True, timeout=30)
        if result.returncode != 0 or "Commands:" not in result.stdout:
            raise MigrationError("Installed central engine failed verification: " + result.stderr[:400])
        if _active(root):
            raise MigrationError("Managed session became active during migration")
        originals = [(p, p.read_bytes(), p.stat().st_mode) for p in selected]
        try:
            for file, original, _mode in originals:
                # No deletion without final ownership recheck.
                if file.is_file() and _digest(file) == manifest["files"][file.relative_to(root).as_posix()]:
                    file.unlink()
                else:
                    raise MigrationError("Legacy toolkit was modified; manual recovery required")
        except BaseException:
            # A partial filesystem failure restores the old engine, allowing
            # the outer guard to discard the not-yet-committed adapter.
            for file, original, mode in originals:
                if not file.exists():
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(original)
                    os.chmod(file, mode)
            raise
        output("Central engine migration completed. Original project context preserved.")
        return True
    except BaseException:
        # Never discard the copied context if source removal has started.
        if published and all(p.is_file() for p in to_remove) and target.exists():
            shutil.rmtree(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(root: Path) -> int:
    try:
        migrate(root)
        return 0
    except (MigrationError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("Gaston migration: " + str(exc), file=sys.stderr)
        return 2
