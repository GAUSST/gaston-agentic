"""Signed, versioned, non-project Gaston engine activation.

Managed versions live under the user's private application-data directory;
the native dev entry point remains a stable bootstrap. Updates never edit a
workspace, its state, sessions, application code, or the running engine tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import time

VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
OFFICIAL_REPOSITORY = "GAUSST/gaston-agentic"


class CentralUpdateError(RuntimeError):
    pass


def home() -> Path:
    folder = Path(os.environ.get("GASTON_GLOBAL_HOME", str(Path.home() / ".local/share/gaston"))).expanduser()
    if not folder.is_absolute():
        raise CentralUpdateError("Gaston global home must be an absolute path")
    folder = folder.resolve()
    if folder == Path(folder.anchor):
        raise CentralUpdateError("Refusing the filesystem root as Gaston home")
    return folder


def _safe_child(path: Path, base: Path) -> Path:
    base = base.resolve()
    if not path.is_relative_to(base) or path == base:
        raise CentralUpdateError("Global engine path escapes the version store")
    for ancestor in (path, *path.parents):
        if ancestor == base:
            break
        if ancestor.is_symlink():
            raise CentralUpdateError("Linked global engine path is forbidden")
    return path


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pointer_file(folder: Path | None = None) -> Path:
    return (folder or home()) / "current.json"


def _atomic_json(path: Path, value: dict):
    if path.is_symlink():
        raise CentralUpdateError("Refusing linked update pointer")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".gaston-pointer-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _read_pointer(folder: Path | None = None) -> dict | None:
    path = pointer_file(folder)
    if not path.exists():
        return None
    if path.is_symlink():
        raise CentralUpdateError("Refusing linked update pointer")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CentralUpdateError("Invalid active engine pointer") from exc
    if not isinstance(value, dict) or not VERSION.fullmatch(str(value.get("tag", ""))):
        raise CentralUpdateError("Malformed version pointer")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("manifest_sha256", ""))):
        raise CentralUpdateError("Missing engine integrity identity")
    return value


def active_path(folder: Path | None = None) -> Path | None:
    base = folder or home()
    state = _read_pointer(base)
    if state is None:
        return None
    version = _safe_child(base / "versions" / state["tag"], base)
    manifest = version / "gaston_engine" / "manifest.json"
    cli = version / "gaston_cli.py"
    if (not version.is_dir() or manifest.is_symlink() or not manifest.is_file()
            or cli.is_symlink() or not cli.is_file()
            or _hash(manifest.read_bytes()) != state["manifest_sha256"]):
        raise CentralUpdateError("Installed Gaston engine is corrupt; refusing execution")
    return version


def stage_verified_release(folder: Path, tag: str, payload: dict, manifest: dict) -> Path:
    """Stage an already signature- and checksum-verified candidate in isolation."""
    if not VERSION.fullmatch(tag) or manifest.get("release") != tag:
        raise CentralUpdateError("Manifest and requested version do not match")
    base = folder.resolve()
    versions = base / "versions"
    versions.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = _safe_child(versions / tag, base)
    if destination.exists() or destination.is_symlink():
        raise CentralUpdateError("Version already installed; refusing overwrite")
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=versions))
    try:
        files = manifest["files"]
        for rel, expected_sha in files.items():
            if (not isinstance(rel, str) or PurePosixPath(rel).is_absolute()
                    or '..' in PurePosixPath(rel).parts or '\\' in rel):
                raise CentralUpdateError("Unsafe global engine archive path")
            if rel not in payload:
                raise CentralUpdateError("Incomplete signed release: " + rel)
            data = payload[rel][0]
            if _hash(data) != expected_sha:
                raise CentralUpdateError("Hash mismatch for " + rel)
            if rel.startswith(".agentic/") and rel.endswith((".py", ".md")):
                target = staging / "gaston_engine" / rel[len(".agentic/"):]
            elif rel == "gaston_cli.py":
                target = staging / "gaston_cli.py"
            else:
                continue
            _safe_child(target, staging)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        engine = staging / "gaston_engine"
        if not (engine / "__init__.py").is_file() or not (engine / "eng.py").is_file():
            raise CentralUpdateError("Incomplete installed runtime distribution")
        if not (staging / "gaston_cli.py").is_file():
            raise CentralUpdateError("Missing native CLI bootstrap")
        (engine / "manifest.json").write_bytes(payload[".agentic/manifest.json"][0])
        # Source is verified. Do not execute candidate code before activation.
        os.replace(staging, destination)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def activate(folder: Path, tag: str, *, expected_manifest_sha256: str):
    if not VERSION.fullmatch(tag):
        raise CentralUpdateError("Invalid engine version")
    base = folder.resolve()
    target = _safe_child(base / "versions" / tag, base)
    manifest = target / "gaston_engine" / "manifest.json"
    if not manifest.is_file() or _hash(manifest.read_bytes()) != expected_manifest_sha256:
        raise CentralUpdateError("Refusing engine activation: manifest verification failed")
    old = _read_pointer(base)
    previous = old["tag"] if old else None
    _atomic_json(pointer_file(base), {
        "tag":tag, "previous":previous, "manifest_sha256":expected_manifest_sha256,
        "activated_at":time.time()
    })


def command(args: list[str], *, confirm=input, output=print) -> int:
    parser = argparse.ArgumentParser(prog="dev update")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parsed = parser.parse_args(args)
    if parsed.check and parsed.rollback:
        raise CentralUpdateError("Choose only one update operation")
    folder = home()
    installed = _read_pointer(folder)
    from importlib import metadata
    fallback = metadata.version("gaston-agentic-cli")
    current = "v" + fallback if installed is None else installed["tag"]
    if parsed.rollback:
        previous = installed.get("previous") if installed else None
        if not previous or not VERSION.fullmatch(previous):
            raise CentralUpdateError("No previous installed engine version to restore")
        if previous == "v" + fallback:
            if confirm("Type ROLLBACK " + previous + ": ").strip() != "ROLLBACK " + previous:
                output("Rollback cancelled.")
                return 0
            pointer = pointer_file(folder)
            if pointer.is_symlink():
                raise CentralUpdateError("Refusing linked global pointer")
            pointer.unlink()
            output("Gaston reverted to base installation " + previous + ".")
            return 0
        previous_dir = _safe_child(folder / "versions" / previous, folder)
        manifest_file = previous_dir / "gaston_engine" / "manifest.json"
        if not manifest_file.is_file() or manifest_file.is_symlink():
            raise CentralUpdateError("Previous version cannot be verified")
        wanted = _hash(manifest_file.read_bytes())
        if confirm("Type ROLLBACK " + previous + ": ").strip() != "ROLLBACK " + previous:
            output("Rollback cancelled.")
            return 0
        activate(folder, previous, expected_manifest_sha256=wanted)
        output("Gaston reverted to " + previous + ".")
        return 0

    # Existing legacy updater already enforces signed annotated tags, repo
    # identity, bounded downloads, strict ZIP traversal checks and SHA-256.
    from updater import latest, signed_release_commit, _download, _github_url
    from updater import _validate_candidate, MAX_ARCHIVE, version
    release = latest(OFFICIAL_REPOSITORY)
    if version(release["tag"]) <= version(current):
        output("Gaston is current: " + current)
        return 0
    output("Signed Gaston update available: " + current + " → " + release["tag"])
    if parsed.check:
        return 0
    commit = signed_release_commit(OFFICIAL_REPOSITORY, release["tag"])
    archive = _download(_github_url(OFFICIAL_REPOSITORY, "zipball/" + commit), MAX_ARCHIVE)
    manifest, payload = _validate_candidate(archive, release["tag"])
    if confirm("Type INSTALL " + release["tag"] + ": ").strip() != "INSTALL " + release["tag"]:
        output("Update cancelled; no changes made.")
        return 0
    # Immutable version directory; active pointers swap atomically, and the
    # previously installed engine stays available for explicit rollback.
    stage_verified_release(folder, release["tag"], payload, manifest)
    manifest_hash = _hash(payload[".agentic/manifest.json"][0])
    activate(folder, release["tag"], expected_manifest_sha256=manifest_hash)
    output("Gaston engine installed globally: " + release["tag"])
    return 0


def main(args: list[str]) -> int:
    try:
        return command(args)
    except (CentralUpdateError, OSError, RuntimeError, ValueError, KeyError) as exc:
        print("Gaston global update: " + str(exc), file=sys.stderr)
        return 2
