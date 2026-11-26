"""Signed global Gaston engine updates and managed provider-hook refresh.

The updater changes only the global version store and active-version pointer.
When run from a central project, it may retarget recognized Gaston hook
commands in that project's Claude/Codex settings. No project state or
installed project files are migrated, archived or deleted; those two provider
settings are changed only to refresh recognized Gaston hook commands.
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
import subprocess
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


def current_version(folder: Path | None = None) -> str:
    from importlib import metadata
    installed = _read_pointer(folder)
    return installed["tag"] if installed else "v" + metadata.version("gaston-agentic-cli")


def suggest_update(*, output=print) -> None:
    """Best-effort release notice for interactive init; never install or migrate."""
    from updater import latest, version
    try:
        current = current_version()
        release = latest(OFFICIAL_REPOSITORY)
        if version(release["tag"]) > version(current):
            output(f'Gaston update available: {release["tag"]} (installed {current}). Run dev update to review.')
    except (OSError, RuntimeError, ValueError, KeyError):
        return


def _current_hook_script(folder: Path) -> Path:
    """Return the native hook script belonging to the active global engine."""
    active = active_path(folder)
    if active is not None:
        return active / "gaston_engine" / "native_hooks.py"
    from importlib import metadata
    return Path(metadata.distribution("gaston-agentic-cli").locate_file(
        "gaston_engine/native_hooks.py"
    )).resolve()


def _refresh_project_hooks(root: Path | None, engine_script: Path, *, output=print) -> dict:
    """Refresh only recognized Claude/Codex hook commands in a central project.

    The currently running updater may be the previous release. Temporarily point
    its hook manager at the newly activated engine script so refreshed commands
    never keep referencing the old version. All other project files, hook
    entries and provider trust records remain untouched.
    """
    unchanged = {"claude": False, "codex": False}
    if root is None:
        return unchanged
    project = Path(root).resolve()
    state_dir = project / ".gaston"
    marker = state_dir / "project.json"
    if state_dir.is_symlink() or marker.is_symlink() or not marker.is_file():
        return unchanged
    candidate = Path(engine_script)
    if candidate.is_symlink() or not candidate.is_file():
        output("Gaston hook refresh skipped: installed native hook runtime is unavailable.")
        return unchanged
    script = candidate.resolve()

    env_names = ("GASTON_ENGINE_MODE", "GASTON_ENGINE_PROJECT")
    previous = {name: os.environ.get(name) for name in env_names}
    try:
        os.environ["GASTON_ENGINE_MODE"] = "central"
        os.environ["GASTON_ENGINE_PROJECT"] = str(project)
        import importlib
        hooks = importlib.import_module("native_hooks")
        original_runtime_script = hooks.runtime_script
        hooks.runtime_script = lambda name: (
            script if name == "native_hooks.py" else original_runtime_script(name)
        )
        try:
            refreshed = hooks.refresh_central_provider_hooks(project)
        finally:
            hooks.runtime_script = original_runtime_script
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        output("Gaston hook refresh skipped: " + str(exc))
        return unchanged
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    for provider in ("claude", "codex"):
        if refreshed.get(provider):
            output(provider.capitalize() + " hooks refreshed to the active Gaston engine.")
    return refreshed


def command(args: list[str], *, root: Path | None = None, confirm=input, output=print) -> int:
    """Update the global engine; optionally refresh this central project's hooks."""
    parser = argparse.ArgumentParser(prog="dev update")
    parser.add_argument("--rollback", action="store_true")
    parsed = parser.parse_args(args)
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
            _refresh_project_hooks(root, _current_hook_script(folder), output=output)
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
        _refresh_project_hooks(
            root, previous_dir / "gaston_engine" / "native_hooks.py", output=output
        )
        return 0

    # Publisher identity and release contents are verified before the one
    # yes/no approval. Project context is used only for the narrow hook refresh.
    from updater import latest, signed_release_commit, _download, _github_url
    from updater import _validate_candidate, MAX_ARCHIVE, version
    from setup_wizard import yesno

    release = latest(OFFICIAL_REPOSITORY)
    tag = release["tag"]
    if version(tag) <= version(current):
        output("Gaston is current: " + current)
        _refresh_project_hooks(root, _current_hook_script(folder), output=output)
        return 0

    output("Gaston update available: " + current + " to " + tag)
    commit = signed_release_commit(OFFICIAL_REPOSITORY, tag)
    archive = _download(_github_url(OFFICIAL_REPOSITORY, "zipball/" + commit), MAX_ARCHIVE)
    manifest, payload = _validate_candidate(archive, tag)
    try:
        accepted = yesno(
            "Install verified Gaston update?", default=True, read=confirm, write=output
        )
    except EOFError:
        accepted = False
    if not accepted:
        output("Update cancelled; no changes made.")
        return 0

    stage_verified_release(folder, tag, payload, manifest)
    manifest_hash = _hash(payload[".agentic/manifest.json"][0])
    activate(folder, tag, expected_manifest_sha256=manifest_hash)
    output("Gaston engine installed globally: " + tag)
    _refresh_project_hooks(
        root, folder / "versions" / tag / "gaston_engine" / "native_hooks.py",
        output=output
    )
    return 0

def main(args: list[str], *, root: Path | None = None) -> int:
    try:
        return command(args, root=root)
    except (CentralUpdateError, OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print("Gaston global update: " + str(exc), file=sys.stderr)
        return 2
