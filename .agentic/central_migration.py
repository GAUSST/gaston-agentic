"""Explicit, data-preserving consolidation into the central project directory.

Legacy .agentic data is merged without overwriting conflicts, then archived
inside .gaston before the scattered root directory is removed. A previously
migrated workspace can safely finish this cleanup on a later run.
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
BACKUP = Path("migration-backups/legacy-agentic")


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


def _tree_fingerprint(path: Path) -> tuple:
    _assert_tree_safe(path)
    entries = []
    for parent, dirs, files in os.walk(path, followlinks=False):
        dirs.sort()
        files.sort()
        folder = Path(parent)
        for name in dirs:
            rel = (folder / name).relative_to(path).as_posix()
            entries.append(("dir", rel))
        for name in files:
            item = folder / name
            rel = item.relative_to(path).as_posix()
            entries.append(("file", rel, _digest(item)))
    return tuple(entries)


def _copy_tree_verified(source: Path, destination: Path) -> None:
    source_fingerprint = _tree_fingerprint(source)
    if destination.exists() or destination.is_symlink():
        if _tree_fingerprint(destination) == source_fingerprint:
            return
        raise MigrationError("Existing migration backup differs; refusing to overwrite: " + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    if _tree_fingerprint(destination) != source_fingerprint:
        shutil.rmtree(destination)
        raise MigrationError("Migration backup checksum mismatch: " + str(destination))


def _merge_entry(source: Path, destination: Path) -> None:
    _assert_tree_safe(source)
    if destination.is_symlink():
        raise MigrationError("Refusing linked central state: " + str(destination))
    if source.is_dir():
        if destination.exists():
            if not destination.is_dir():
                raise MigrationError("State path type conflict: " + str(destination))
            for child in sorted(source.iterdir(), key=lambda p: p.name):
                _merge_entry(child, destination / child.name)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
            if _tree_fingerprint(source) != _tree_fingerprint(destination):
                shutil.rmtree(destination)
                raise MigrationError("State copy checksum mismatch: " + str(source))
        return
    if not source.is_file():
        raise MigrationError("Unsupported legacy state entry: " + str(source))
    if destination.exists():
        if not destination.is_file() or _digest(source) != _digest(destination):
            raise MigrationError("Conflicting project state; both copies were preserved: " + str(destination))
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if _digest(source) != _digest(destination):
        destination.unlink(missing_ok=True)
        raise MigrationError("State copy checksum mismatch: " + str(source))


def _check_state_merge(source: Path, destination: Path) -> None:
    for rel in STATE:
        old = source / rel
        if not old.exists() and not old.is_symlink():
            continue
        _assert_tree_safe(old)
        new = destination / rel
        if new.is_symlink():
            raise MigrationError("Refusing linked central state: " + str(new))
        if old.is_dir():
            if new.exists() and not new.is_dir():
                raise MigrationError("State path type conflict: " + str(new))
            if new.exists():
                _check_merge_tree(old, new)
        elif old.is_file() and new.exists():
            if not new.is_file() or _digest(old) != _digest(new):
                raise MigrationError("Conflicting project state; both copies were preserved: " + str(new))
        elif not old.is_file():
            raise MigrationError("Unsupported legacy state entry: " + str(old))


def _check_merge_tree(source: Path, destination: Path) -> None:
    _assert_tree_safe(source)
    _assert_tree_safe(destination)
    for child in source.iterdir():
        other = destination / child.name
        if other.is_symlink():
            raise MigrationError("Refusing linked central state: " + str(other))
        if child.is_dir():
            if other.exists() and not other.is_dir():
                raise MigrationError("State path type conflict: " + str(other))
            if other.exists():
                _check_merge_tree(child, other)
        elif child.is_file() and other.exists():
            if not other.is_file() or _digest(child) != _digest(other):
                raise MigrationError("Conflicting project state; both copies were preserved: " + str(other))


def _merge_state(source: Path, destination: Path) -> None:
    for rel in STATE:
        old = source / rel
        if old.exists() or old.is_symlink():
            _merge_entry(old, destination / rel)


def _read_manifest(old: Path) -> dict:
    path = old / "manifest.json"
    if old.is_symlink() or not old.is_dir() or path.is_symlink() or not path.is_file():
        raise MigrationError("Not a verified legacy Gaston workspace")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError("Invalid legacy workspace manifest") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    version = manifest.get("build") if isinstance(manifest, dict) else None
    if (not isinstance(files, dict) or not isinstance(version, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)):
        raise MigrationError("Unsupported legacy Gaston release manifest")
    return manifest


def _legacy_owned_sources(root: Path, manifest: dict) -> list[Path]:
    old = root / ".agentic"
    files = manifest.get("files")
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
        if (original.is_symlink() or not original.is_file()
                or not isinstance(expected, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected)):
            raise MigrationError("Legacy engine source is missing or linked: " + rel)
        if _digest(original) != expected:
            raise MigrationError("Modified Gaston engine file; migration leaves it untouched: " + rel)
        if not installed.is_file():
            raise MigrationError("Unrecognized legacy toolkit module: " + rel)
        selected.append(original)
    if not any(p.name == "launcher.py" for p in selected):
        raise MigrationError("Legacy launcher is not verifiably owned by Gaston")
    return selected


def _active(root: Path) -> bool:
    for state in (root / ".agentic", root / ".gaston"):
        for rel in ("local/locks", "local/native-hooks/active",
                    "local/native-hooks/feedback-worker.lock"):
            path = state / rel
            if path.is_symlink() or path.is_file() or (path.is_dir() and any(path.iterdir())):
                return True
    return False


def _ensure_backup_ignore(target: Path) -> None:
    path = target / ".gitignore"
    if path.is_symlink():
        raise MigrationError("Refusing linked Gaston ignore policy")
    if not path.exists():
        path.write_text("local/\nruns/\ntasks/\nconfig.json\npolicies/custom/\nmigration-backups/\n",
                        encoding="utf-8")
        return
    current = path.read_text(encoding="utf-8")
    if "migration-backups/" not in current.splitlines():
        path.write_text(current.rstrip("\n") + "\nmigration-backups/\n", encoding="utf-8")


def _archive_legacy(old: Path, target: Path) -> Path:
    backup = target / BACKUP
    _copy_tree_verified(old, backup)
    return backup


def _smoke_test(root: Path) -> None:
    environment = os.environ.copy()
    environment.update(GASTON_ENGINE_MODE="central", GASTON_ENGINE_PROJECT=str(root))
    engine = Path(__file__).resolve().parent / "eng.py"
    result = subprocess.run([sys.executable, str(engine), "--help"],
                            cwd=root, env=environment, capture_output=True,
                            text=True, timeout=30)
    if result.returncode != 0 or "Commands:" not in result.stdout:
        raise MigrationError("Installed central engine failed verification: " + result.stderr[:400])
    if _active(root):
        raise MigrationError("Managed session became active during migration")


def _write_marker(target: Path, marker: dict) -> None:
    path = target / "project.json"
    if path.is_symlink():
        raise MigrationError("Refusing linked Gaston project marker")
    marker = dict(marker)
    marker["migrated_from"] = "legacy"
    temp = target / ".project-migration.tmp"
    if temp.is_symlink():
        raise MigrationError("Refusing linked migration marker temporary file")
    temp.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _move_legacy_dev_db(root: Path) -> bool:
    # The DB module owns credential validation and its legacy-path migration.
    try:
        import development_db
        return bool(development_db.migrate_legacy_credentials(root))
    except ImportError:
        if (root / ".gaston-dev-db").exists() or (root / ".gaston-dev-db").is_symlink():
            raise MigrationError("Development DB migration helper is unavailable; legacy credentials were left untouched")
        return False


def _remove_archived_legacy(old: Path, backup: Path) -> None:
    if not old.exists():
        return
    if _tree_fingerprint(old) != _tree_fingerprint(backup):
        raise MigrationError("Legacy directory changed after backup; both copies were preserved")
    shutil.rmtree(old)


def migrate(root: Path, *, confirmation=input, output=print) -> bool:
    root = Path(root).resolve()
    old = root / ".agentic"
    target = root / ".gaston"
    if not old.exists() and not old.is_symlink():
        if target.exists():
            read_project_marker(root)
            legacy_db = root / ".gaston-dev-db"
            if legacy_db.exists() or legacy_db.is_symlink():
                if _active(root):
                    raise MigrationError("Close all Gaston sessions and managed workers before migration")
                output("GASTON CENTRAL — DEVELOPMENT DB DIRECTORY MIGRATION")
                output("  Credentials and any unknown files will be preserved under .gaston/local/dev-db/.")
                if confirmation("Type MIGRATE GASTON to approve: ").strip() != "MIGRATE GASTON":
                    output("Migration cancelled; no files changed.")
                    return False
                moved = _move_legacy_dev_db(root)
                output("Development DB directory consolidated under .gaston/local/dev-db." if moved
                       else "No legacy development DB files needed moving.")
                return moved
            output("Gaston project is already consolidated.")
            return False
        raise MigrationError("No legacy .agentic directory found")
    _assert_tree_safe(old)
    if _active(root):
        raise MigrationError("Close all Gaston sessions and managed workers before migration")

    target_exists = target.exists() or target.is_symlink()
    if target_exists:
        marker = read_project_marker(root)
        already_migrated = marker.get("migrated_from") == "legacy"
        if not already_migrated:
            manifest = _read_manifest(old)
            if (old / "launcher.py").is_file():
                _legacy_owned_sources(root, manifest)
            # A partially completed prior migration may already have removed
            # the launcher. Its entire source tree is preserved in the archive.
            _check_state_merge(old, target)
    else:
        manifest = _read_manifest(old)
        _legacy_owned_sources(root, manifest)

    output("GASTON CENTRAL — PROJECT CONSOLIDATION")
    output("  Project: " + str(root))
    output("  Canonical project state: .gaston/")
    output("  Legacy .agentic data will be preserved under .gaston/migration-backups/legacy-agentic/")
    output("  Existing central files are never overwritten; conflicting state stops the operation.")
    output("  Optional development DB credentials will move under .gaston/local/dev-db/.")
    output("  Provider-native .claude and .codex hook entrypoints are not moved.")
    if confirmation("Type MIGRATE GASTON to approve: ").strip() != "MIGRATE GASTON":
        output("Migration cancelled; no files changed.")
        return False

    if _active(root):
        raise MigrationError("Project state changed during confirmation")
    _assert_tree_safe(old)
    if target_exists:
        read_project_marker(root)
        if not already_migrated:
            _check_state_merge(old, target)
            _merge_state(old, target)
        _ensure_backup_ignore(target)
        backup = _archive_legacy(old, target)
        _move_legacy_dev_db(root)
        _smoke_test(root)
        if not already_migrated:
            marker = read_project_marker(root)
            _write_marker(target, marker)
        _remove_archived_legacy(old, backup)
    else:
        staging = Path(tempfile.mkdtemp(prefix=".gaston-migration-", dir=root))
        try:
            _merge_state(old, staging)
            marker = {
                "schema": 1, "kind": "gaston-project",
                "engine": "gaston-agentic-cli",
                "created_with": metadata.version("gaston-agentic-cli"),
                "migrated_from": "legacy"
            }
            (staging / "project.json").write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            (staging / ".gitignore").write_text(
                "local/\nruns/\ntasks/\nconfig.json\npolicies/custom/\nmigration-backups/\n",
                encoding="utf-8")
            _archive_legacy(old, staging)
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        _move_legacy_dev_db(root)
        _smoke_test(root)
        backup = target / BACKUP
        _remove_archived_legacy(old, backup)

    output("Consolidation completed. Legacy project files are archived inside .gaston; root-level .agentic was removed.")
    return True


def main(root: Path) -> int:
    try:
        migrate(root)
        return 0
    except (MigrationError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("Gaston migration: " + str(exc), file=sys.stderr)
        return 2
