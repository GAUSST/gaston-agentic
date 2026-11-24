"""Git tracking for a workspace with one repository or several nested repositories.

The coordinator can live above the application repositories. Only Git repositories
inside the configured workspace are used for safety checks and review evidence.
"""
from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

EXCLUDED = {'.git', '.agentic', '.gaston', '.claude', '.agents', '.dart_tool', 'node_modules',
            'build', 'dist', '.gradle', '.idea', 'target', '.venv', 'venv'}
MAX_UNTRACKED_BYTES = 15 * 1024 * 1024


def _run(args, *, cwd: Path, timeout=30):
    try:
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                              check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def repositories(root: Path, project: dict | None = None) -> list[Path]:
    """Discover Git roots within workspace; respects configured source directories.

    Also discovers top-level and second-level nested repositories, such as
    backend/ and services/payments/. Never traverses outside the workspace.
    """
    root = Path(root).resolve()
    if shutil.which('git') is None:
        return []
    project = project or {}
    candidates = [root]
    for name in ('java_root', 'flutter_root'):
        value = project.get(name)
        if isinstance(value, str) and value and value != '-':
            candidate = (root / value).resolve()
            if candidate.is_relative_to(root) and candidate.is_dir():
                candidates.append(candidate)
    # Include later-added nested project repos (e.g. apps/mobile, services/api).
    # Config is refreshed before work and contains paths discovered at task time.
    for group in (project.get('discovered_roots') or {}).values():
        if not isinstance(group, list):
            continue
        for relative in group:
            if not isinstance(relative, str):
                continue
            candidate = (root / relative).resolve()
            if candidate.is_relative_to(root) and candidate.is_dir():
                candidates.append(candidate)
    # Scan nested roots such as apps/mobile/android/ and services/api/backend/.
    # Only probe folders containing .git or configured source roots; never run
    # an expensive git command for every unrelated source directory.
    for current, dirs, _files in os.walk(root, followlinks=False):
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        if depth > 7:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in EXCLUDED and not d.startswith('.')
                   and not (here / d).is_symlink()]
        if (here / '.git').exists():
            candidates.append(here)
    output: set[Path] = set()
    for candidate in candidates:
        probe = _run(['rev-parse', '--show-toplevel'], cwd=candidate, timeout=6)
        if probe and probe.returncode == 0:
            actual = Path(os.fsdecode(probe.stdout.strip())).resolve()
            if actual.is_relative_to(root) and actual.is_dir():
                output.add(actual)
    return sorted(output, key=lambda p: str(p.relative_to(root)))


def tracked_roots(root: Path, project: dict | None = None) -> tuple[list[Path], list[str]]:
    """Return found repos and selected source roots without a covering repo."""
    root = Path(root).resolve()
    repos = repositories(root, project)
    missing = []
    for key in ('java_root', 'flutter_root'):
        value = (project or {}).get(key)
        if not value or value == '-':
            continue
        candidate = (root / value).resolve()
        if not candidate.is_relative_to(root) or not any(candidate.is_relative_to(repo) for repo in repos):
            missing.append(str(value))
    for group in (project or {}).get('discovered_roots', {}).values():
        if not isinstance(group, list):
            continue
        for relative in group:
            if not isinstance(relative, str):
                continue
            candidate = (root / relative).resolve()
            if (candidate.is_relative_to(root) and candidate.is_dir() and
                    not any(candidate.is_relative_to(repo) for repo in repos)):
                if relative not in missing:
                    missing.append(relative)
    return repos, missing


def baseline_ready(repos: list[Path]) -> bool:
    return bool(repos) and all(
        (p := _run(['rev-parse', '--verify', 'HEAD'], cwd=repo, timeout=10)) is not None
        and p.returncode == 0 for repo in repos)


def clean(root: Path, project: dict | None = None) -> bool:
    repos, missing = tracked_roots(root, project)
    if not repos or missing or not baseline_ready(repos):
        raise ValueError('No usable Git baseline for every configured application root; run ./dev init')
    for repo in repos:
        state = _run(['status', '--porcelain', '--untracked-files=normal'], cwd=repo)
        if state is None or state.returncode:
            raise ValueError(f'Cannot inspect Git working tree: {repo}')
        if state.stdout.strip():
            return False
    return True


def _untracked(repo: Path) -> list[tuple[bytes, Path]] | None:
    result = _run(['ls-files', '--others', '--exclude-standard', '-z'], cwd=repo)
    if result is None or result.returncode:
        return None
    items = []
    for name in sorted(x for x in result.stdout.split(b'\0') if x):
        target = (repo / os.fsdecode(name)).resolve()
        if not target.is_relative_to(repo.resolve()) or not target.is_file():
            return None
        if target.stat().st_size > MAX_UNTRACKED_BYTES:
            return None
        items.append((name, target))
    return items


def fingerprint(root: Path, project: dict | None = None, *, review_documents=()) -> str | None:
    """Hash changes from all nested repos, including previously untracked files."""
    repos, missing = tracked_roots(root, project)
    if not repos or missing or not baseline_ready(repos):
        return None
    digest = hashlib.sha256(b'agentic-git-workspace-v2\0')
    for repo in repos:
        digest.update(str(repo.relative_to(root.resolve())).encode() + b'\0')
        head = _run(['rev-parse', 'HEAD'], cwd=repo)
        excludes=[]
        if review_documents:
            from acceptance_audit import review_only_document
            names=_run(['diff','--no-renames','--name-only','-z','HEAD'],cwd=repo)
            if names is None or names.returncode:return None
            for raw in names.stdout.split(b'\0'):
                name=os.fsdecode(raw)
                if name and (repo/name).relative_to(root.resolve()).as_posix() in review_documents and review_only_document(name) and not (repo/name).is_symlink() and not (repo/name).is_dir():excludes.append(':(exclude,literal)'+name)
        diff = _run(['diff', '--no-ext-diff', '--binary', 'HEAD', *(['--','.',*excludes] if excludes else [])], cwd=repo)
        changes = _untracked(repo)
        if head is None or diff is None or head.returncode or diff.returncode or changes is None:
            return None
        digest.update(head.stdout)
        digest.update(diff.stdout)
        for name, path in changes:
            relative=(repo/os.fsdecode(name)).relative_to(root.resolve()).as_posix()
            if relative in review_documents and review_only_document(relative) and not (repo/os.fsdecode(name)).is_symlink():continue
            digest.update(b'\0untracked\0' + name)
            digest.update(path.read_bytes())
    return digest.hexdigest()


def existing_changes(root: Path, project: dict | None = None) -> dict[str, str]:
    """Snapshot existing tracked/untracked edits without requiring a clean tree.

    Values are content digests, or explicit markers for deleted/non-regular
    paths. This is diagnostic evidence, not an ownership or staging grant.
    """
    root = Path(root).resolve()
    repos, missing = tracked_roots(root, project)
    if not repos or missing or not baseline_ready(repos):
        raise ValueError('Cannot safely inspect selected Git repositories')
    baseline: dict[str, str] = {}
    for repo in repos:
        changed = _run(['diff', '--name-only', '-z', 'HEAD'], cwd=repo)
        others = _run(['ls-files', '--others', '--exclude-standard', '-z'], cwd=repo)
        if changed is None or others is None or changed.returncode or others.returncode:
            raise ValueError(f'Cannot inspect Git changes in {repo}')
        for raw in set(x for output in (changed.stdout, others.stdout) for x in output.split(b'\0') if x):
            path = repo / os.fsdecode(raw)
            # Never follow symlinks when reading pre-existing user content.
            if path.is_symlink():
                value = 'symlink:' + os.readlink(path)
            elif not path.exists():
                value = 'deleted'
            elif path.is_file():
                h = hashlib.sha256()
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        h.update(chunk)
                value = h.hexdigest()
            else:
                value = 'non-regular'
            baseline[str(path.relative_to(root))] = value
    return dict(sorted(baseline.items()))


def changed_existing_files(root: Path, baseline: dict[str, str]) -> list[str]:
    """Report pre-existing files modified/deleted by the new task, never block it."""
    root = Path(root).resolve()
    changed = []
    for relative, before in baseline.items():
        path = root / relative
        if path.is_symlink():
            after = 'symlink:' + os.readlink(path)
        elif not path.exists():
            after = 'deleted'
        elif path.is_file():
            h = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    h.update(chunk)
            after = h.hexdigest()
        else:
            after = 'non-regular'
        if before != after:
            changed.append(relative)
    return changed


def safe_test_only_baseline(root: Path, project: dict | None = None) -> dict | None:
    """Capture narrow, pre-existing untracked tests without treating other edits as trusted.

    The Builder is instructed not to alter these files; comparing hashes after
    execution detects interference. Never auto-stage, stash, delete or commit.
    """
    root = Path(root).resolve()
    repos, missing = tracked_roots(root, project)
    if not repos or missing or not baseline_ready(repos):
        return None
    manifest = {}
    for repo in repos:
        tracked = _run(['diff', '--name-only', '-z', 'HEAD'], cwd=repo)
        if tracked is None or tracked.returncode or tracked.stdout:
            return None
        files = _untracked(repo)
        if files is None:
            return None
        for raw_name, path in files:
            relative = os.fsdecode(raw_name).replace('\\', '/')
            # Production code, unexpected test resources, and ambiguous files
            # always require explicit human review.
            parts = Path(relative).parts
            if (len(parts) < 5 or parts[:3] != ('src', 'test', 'java')
                    or path.suffix != '.java' or (repo / os.fsdecode(raw_name)).is_symlink()
                    or path.stat().st_size > 128 * 1024):
                return None
            manifest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest or None


def baseline_unchanged(root: Path, baseline: dict) -> bool:
    root = Path(root).resolve()
    for relative, original_hash in baseline.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != original_hash:
            return False
    return True


def review_evidence(root: Path, project: dict | None = None, *, max_diff=750000,
                    max_manifest=50000) -> tuple[str, str]:
    """Collect labeled per-repo changes, including bounded text untracked files."""
    root = Path(root).resolve()
    repos, missing = tracked_roots(root, project)
    if not repos or missing or not baseline_ready(repos):
        raise ValueError('Cannot collect review evidence: selected code roots need a Git baseline')
    patches, manifests = [], []
    for repo in repos:
        label = repo.relative_to(root).as_posix()
        diff = _run(['diff', '--no-ext-diff', '--binary', 'HEAD'], cwd=repo)
        status = _run(['status', '--porcelain', '--untracked-files=normal'], cwd=repo)
        files = _untracked(repo)
        if diff is None or status is None or diff.returncode or status.returncode or files is None:
            raise ValueError(f'Cannot collect changes in {repo}')
        patches.append(f'### REPOSITORY: {label}\n' + diff.stdout.decode('utf-8', 'replace'))
        manifests.append(f'### REPOSITORY: {label}\n' + status.stdout.decode('utf-8', 'replace'))
        # A standard git diff HEAD excludes new untracked files. Include text
        # sources in the bounded review artifact to avoid silently missing them.
        for name, path in files:
            if path.stat().st_size > 128 * 1024:
                continue
            raw = path.read_bytes()
            if b'\0' in raw or path.suffix.lower() in {'.env', '.pem', '.key', '.p12', '.pfx'}:
                continue
            rel = os.fsdecode(name)
            content = raw.decode('utf-8', 'replace').splitlines(keepends=True)
            patches.append(''.join(difflib.unified_diff([], content,
                            fromfile='/dev/null', tofile=f'{label}/{rel}')))
    return '\n'.join(patches)[:max_diff], '\n'.join(manifests)[:max_manifest]


def changed_paths(root: Path, project: dict | None = None) -> list[str]:
    """Tracked changes since HEAD plus untracked sources, labeled by repo.

    Failure to collect changes is an error, not proof that nothing changed.
    """
    root = Path(root).resolve()
    repos, missing = tracked_roots(root, project)
    if not repos or missing or not baseline_ready(repos):
        raise ValueError('Cannot select risk-based tests without usable Git baseline')
    changes = set()
    for repo in repos:
        diff = _run(['diff', '--name-only', '-z', 'HEAD'], cwd=repo)
        others = _run(['ls-files', '--others', '--exclude-standard', '-z'], cwd=repo)
        if diff is None or others is None or diff.returncode or others.returncode:
            raise ValueError(f'Cannot list source changes: {repo}')
        for value in (diff.stdout, others.stdout):
            for raw in value.split(b'\0'):
                if raw:
                    rel = (repo / os.fsdecode(raw)).resolve()
                    if rel.is_relative_to(root):
                        changes.add(rel.relative_to(root).as_posix())
    return sorted(changes)
