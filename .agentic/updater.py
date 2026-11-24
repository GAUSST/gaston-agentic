"""Verified, consent-based updates of Gaston's manifest-owned workspace toolkit.

Source: an explicitly trusted public GitHub repository, configured per workspace
with ``dev update --set-repo OWNER/REPO``. Only a GitHub-verified, signed,
annotated release tag is accepted. The GitHub-provided commit archive is pinned
to that tag's commit SHA; no manually assembled release ZIP is necessary.

No application files, provider credentials, .git repository or local Gaston state
are included in an update. Keep a recovery snapshot and journal any activation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import zipfile

VERSION = re.compile(r'^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
REPOSITORY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$')
SHA = re.compile(r'^[a-f0-9]{40}$', re.IGNORECASE)
MANIFEST = '.agentic/manifest.json'
CACHE_SECONDS = 24 * 3600
ERROR_RETRY_SECONDS = 60 * 60
MAX_JSON = 1024 * 1024
MAX_ARCHIVE = 40 * 1024 * 1024
MAX_UNPACKED = 80 * 1024 * 1024
AGENT = 'Gaston-Agentic-updater/1.5.5'


class UpdateError(RuntimeError):
    pass


def version(value: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise UpdateError('Expected a stable vMAJOR.MINOR.PATCH release tag')
    return tuple(map(int, match.groups()))


def store(root: Path) -> Path:
    from evolution import safe
    folder = safe(Path(root).resolve(), '.agentic/local/update')
    if folder.exists() and not folder.is_dir():
        raise UpdateError('Unsafe local update folder')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def _read_json(path: Path) -> dict:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
        raise UpdateError('Missing or linked update metadata')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise UpdateError(f'Invalid update metadata at {path.name}: {exc}') from exc
    if not isinstance(data, dict):
        raise UpdateError('Expected JSON object in update metadata')
    return data


def _atomic(path: Path, data: bytes):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise UpdateError('Refusing linked update target')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.gaston-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600 if path.name.endswith('.json') and 'local' in path.parts else 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _put_json(path: Path, data: dict):
    _atomic(path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())


def configured_repository(root: Path) -> str | None:
    path = store(root) / 'channel.json'
    if not path.exists():
        return None
    repository = _read_json(path).get('repository')
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository) or '..' in repository:
        raise UpdateError('Invalid configured GitHub repository')
    return repository


def set_repository(root: Path, repository: str, *, confirm=input, display=print) -> None:
    if not REPOSITORY.fullmatch(repository) or '..' in repository:
        raise UpdateError('Repository must be in OWNER/REPO format')
    saved = configured_repository(root)
    if saved == repository:
        display(f'Gaston update source already set to {repository}')
        return
    display('This public GitHub repository will be trusted to publish Gaston code: ' + repository)
    display('Updates are never applied automatically and require verified signed release tags.')
    if confirm(f'Type {repository} to trust this publisher: ').strip() != repository:
        raise UpdateError('Publisher confirmation declined; no changes made')
    _put_json(store(root) / 'channel.json', {'repository': repository, 'configured_at': time.time()})
    (store(root) / 'check.json').unlink(missing_ok=True)
    display('Trusted update source configured. Gaston commands are unchanged.')


def _github_url(repository: str, suffix: str) -> str:
    if not REPOSITORY.fullmatch(repository):
        raise UpdateError('Invalid trusted repository')
    return 'https://api.github.com/repos/' + repository + '/' + suffix


def _download(url: str, limit: int) -> bytes:
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'api.github.com':
        raise UpdateError('Only the GitHub HTTPS API is allowed')
    req = Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': AGENT,
                                'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urlopen(req, timeout=10) as reply:
            # GitHub's archive endpoint redirects to codeload.github.com.
            actual = urlsplit(reply.geturl())
            if actual.scheme != 'https' or actual.hostname not in ('api.github.com', 'codeload.github.com'):
                raise UpdateError('GitHub redirected to an unexpected host')
            if reply.headers.get('Content-Length') and int(reply.headers['Content-Length']) > limit:
                raise UpdateError('GitHub response exceeded size limit')
            payload = reply.read(limit + 1)
            if len(payload) > limit:
                raise UpdateError('GitHub response exceeded size limit')
            return payload
    except (HTTPError, URLError, TimeoutError) as exc:
        raise UpdateError(f'Could not reach trusted GitHub release endpoint: {exc}') from exc


def _fetch_json(url: str) -> dict:
    try:
        value = json.loads(_download(url, MAX_JSON))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError('GitHub returned invalid JSON') from exc
    if not isinstance(value, dict):
        raise UpdateError('GitHub returned invalid release metadata')
    return value


def latest(repository: str, *, fetch=_fetch_json) -> dict:
    data = fetch(_github_url(repository, 'releases/latest'))
    tag = data.get('tag_name')
    version(tag)
    if data.get('draft') or data.get('prerelease') or data.get('published_at') is None:
        raise UpdateError('No published stable GitHub release')
    return {'tag': tag, 'notes': str(data.get('body') or '')[:4000],
            'url': str(data.get('html_url') or ''), 'published_at': data['published_at']}


def signed_release_commit(repository: str, tag: str, *, fetch=_fetch_json) -> str:
    """Fail closed unless GitHub verifies a cryptographically signed tag object."""
    version(tag)
    ref = fetch(_github_url(repository, 'git/ref/tags/' + quote(tag)))
    obj = ref.get('object')
    if not isinstance(obj, dict) or obj.get('type') != 'tag' or not SHA.fullmatch(str(obj.get('sha', ''))):
        raise UpdateError('Release must use an annotated, signed Git tag')
    signed = fetch(_github_url(repository, 'git/tags/' + obj['sha']))
    verification = signed.get('verification') or {}
    commit = signed.get('object') or {}
    if signed.get('tag') != tag or verification.get('verified') is not True or verification.get('reason') != 'valid':
        raise UpdateError('GitHub has not verified the release tag signature')
    if commit.get('type') != 'commit' or not SHA.fullmatch(str(commit.get('sha', ''))):
        raise UpdateError('Verified tag must target a commit')
    return commit['sha']


def _check(root: Path, *, fetch=_fetch_json, now=None, force=False) -> dict | None:
    repo = configured_repository(root)
    if not repo:
        return None
    now = time.time() if now is None else now
    path = store(root) / 'check.json'
    if path.is_file() and not force:
        saved = _read_json(path)
        elapsed = now - saved.get('checked_at', 0) if isinstance(saved.get('checked_at'), (int, float)) else -1
        if saved.get('repository') == repo and 0 <= elapsed < (ERROR_RETRY_SECONDS if saved.get('error') else CACHE_SECONDS):
            return saved
    try:
        found = latest(repo, fetch=fetch)
        data = {'repository': repo, 'checked_at': now, **found}
    except UpdateError as exc:
        data = {'repository': repo, 'checked_at': now, 'error': str(exc)}
    _put_json(path, data)
    return data


def _show_notice(root: Path, cache: dict, display=print):
    if not cache or 'tag' not in cache:
        return
    import evolution
    current = evolution.manifest(root).get('build', '')
    try:
        if version(cache['tag']) > version('v' + current):
            display(f'Gaston update available: {cache["tag"]} (installed v{current}). Run dev update to review.')
    except UpdateError:
        return


def notice(root: Path):
    """Non-blocking launch notice: show cached result; refresh in detached worker.

    Network failures never stop engineering sessions. Check no more than daily.
    """
    try:
        repo = configured_repository(root)
        if not repo:
            return
        path = store(root) / 'check.json'
        cache = _read_json(path) if path.is_file() else {}
        if cache.get('repository') == repo:
            _show_notice(root, cache, display=lambda msg: print(msg, file=sys.stderr))
        ttl = ERROR_RETRY_SECONDS if cache.get('error') else CACHE_SECONDS
        elapsed = time.time() - cache['checked_at'] if isinstance(cache.get('checked_at'), (int, float)) else -1
        if cache.get('repository') == repo and 0 <= elapsed < ttl:
            return
        command = [sys.executable, str(Path(__file__).resolve()), '--refresh', str(Path(root).resolve())]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=flags, **({'start_new_session': True} if os.name != 'nt' else {}))
    except (OSError, ValueError, UpdateError):
        return


def _refresh(root: Path):
    from platform_compat import file_lock
    with file_lock(store(root) / 'refresh.lock', nonblocking=True):
        _check(root)


def _validated_manifest(root: Path, manifest: dict, expected_tag: str | None = None) -> dict[str, str]:
    import uninstall
    if not isinstance(manifest, dict):
        raise UpdateError('Invalid release manifest')
    build = manifest.get('build')
    release = manifest.get('release')
    if expected_tag is not None and (release != expected_tag or build != expected_tag.removeprefix('v')):
        raise UpdateError('Release tag and manifest version disagree')
    version(release)
    if build != release.removeprefix('v'):
        raise UpdateError('Manifest version is inconsistent')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise UpdateError('Missing manifest files')
    for rel, digest in files.items():
        if not isinstance(rel, str):
            raise UpdateError('Non-string manifest file name')
        path = PurePosixPath(rel)
        if (path.is_absolute() or '..' in path.parts or '\\' in rel
                or rel == MANIFEST or not uninstall._known_manifest_path(rel)
                or rel == '.agentic/config.json' or any(p in ('local', 'runs', 'tasks', 'custom') for p in path.parts)
                or not re.fullmatch('[0-9a-f]{64}', str(digest))):
            raise UpdateError('Unsafe or unexpected manifest entry: ' + str(rel))
    return files


def _archive_files(archive: bytes, expected: dict[str, str]) -> dict[str, tuple[bytes, int]]:
    output = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        entries = z.infolist()
        files = [info for info in entries if not info.is_dir()]
        if len(files) > 2000:
            raise UpdateError('Unreasonable archive entry count')
        roots = {PurePosixPath(info.filename).parts[0] for info in files}
        if len(roots) != 1:
            raise UpdateError('Unexpected GitHub archive structure')
        seen = set()
        needed = set(expected) | {MANIFEST}
        for info in files:
            parts = PurePosixPath(info.filename).parts
            if len(parts) < 2 or any(p in ('', '.', '..') for p in parts) or info.filename.startswith('/') or '\\' in info.filename:
                raise UpdateError('Unsafe path inside GitHub archive')
            rel = '/'.join(parts[1:])
            if rel.casefold() in seen:
                raise UpdateError('Case-colliding or repeated archive files')
            seen.add(rel.casefold())
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise UpdateError('Archive symlinks are forbidden')
            if rel not in needed:
                continue
            total += info.file_size
            if info.file_size > 4 * 1024 * 1024 or total > MAX_UNPACKED:
                raise UpdateError('Unreasonable toolkit payload size')
            with z.open(info) as fh:
                data = fh.read(info.file_size + 1)
            if len(data) != info.file_size:
                raise UpdateError('Corrupt archive entry')
            if rel in expected and hashlib.sha256(data).hexdigest() != expected[rel]:
                raise UpdateError('Release file hash does not match its manifest: ' + rel)
            mode = (info.external_attr >> 16) & 0o777
            output[rel] = (data, mode)
    if needed != set(output):
        raise UpdateError('Archive is missing toolkit files: ' + ', '.join(sorted(needed-set(output))[:3]))
    return output


def _validate_candidate(archive: bytes, tag: str):
    # First read only the manifest to know the exact files that may be installed.
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        manifests = [n for n in z.namelist() if n.endswith('/' + MANIFEST)]
        if len(manifests) != 1:
            raise UpdateError('Expected exactly one manifest in archive')
        if z.getinfo(manifests[0]).file_size > MAX_JSON:
            raise UpdateError('Manifest too large')
        try:
            manifest = json.loads(z.read(manifests[0]))
        except (ValueError, UnicodeError) as exc:
            raise UpdateError('Invalid manifest in release') from exc
    files = _validated_manifest(Path('.'), manifest, tag)
    payload = _archive_files(archive, files)
    # Protect against duplicate zip entries and misplaced manifest.
    if json.loads(payload[MANIFEST][0]) != manifest:
        raise UpdateError('Manifest payload mismatch')
    return manifest, payload


@contextmanager
def _exclusive_launch_lock(root: Path):
    from platform_compat import file_lock
    try:
        with file_lock(root / '.agentic/local/evolution-launch.lock', nonblocking=True):
            yield
    except BlockingIOError as exc:
        raise UpdateError('A Gaston session is active. Finish it before updating.') from exc


def _verify_workspace(root: Path, manifest: dict) -> dict:
    from evolution import digest, safe
    files = _validated_manifest(root, manifest)
    for rel, sha in files.items():
        path = safe(root, rel)
        if not path.is_file() or digest(path) != sha:
            raise UpdateError('Modified or missing Gaston toolkit file; update refused: ' + rel)
    return files


def _backup(root: Path, original: dict, next_files: dict, saved_manifest: bytes) -> tuple[str, Path]:
    from evolution import safe
    identifier = 'u-' + secrets.token_hex(12)
    folder = store(root) / 'backups' / identifier
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    for rel in original:
        src = safe(root, rel)
        dst = folder / 'toolkit' / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    dst = folder / 'toolkit' / MANIFEST
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(saved_manifest)
    _put_json(folder / 'record.json', {'old': original, 'next': next_files, 'created_at': time.time()})
    return identifier, folder


def _restore(root: Path, identifier: str) -> None:
    from evolution import atomic, digest, safe
    if not re.fullmatch(r'u-[0-9a-f]{24}', identifier):
        raise UpdateError('Invalid recovery snapshot ID')
    folder = store(root) / 'backups' / identifier
    record = _read_json(folder / 'record.json')
    old = record['old']
    new = record['next']
    _validated_manifest(root, {**_read_json(folder / 'toolkit' / MANIFEST), 'files': old})
    _validated_manifest(root, {**_read_json(folder / 'toolkit' / MANIFEST), 'files': new})
    for rel, sha in old.items():
        src = folder / 'toolkit' / rel
        if src.is_symlink() or not src.is_file() or digest(src) != sha:
            raise UpdateError('Recovery snapshot is corrupt: ' + rel)
    # A crash or intervening local edit must never make recovery delete an
    # unrelated pre-existing file. Preflight EVERY new-only target before
    # modifying even the first old toolkit file.
    for rel in set(new) - set(old):
        target = safe(root, rel)
        if target.exists() and (not target.is_file() or digest(target) != new[rel]):
            raise UpdateError('Unrecognized new-file content; manual recovery required: ' + rel)
    # Restore original files first, then remove only verified new-only entries.
    for rel in old:
        src = folder / 'toolkit' / rel
        atomic(safe(root, rel), src.read_bytes(), src.stat().st_mode)
    for rel in set(new)-set(old):
        dest = safe(root, rel)
        if dest.is_file():
            dest.unlink()  # already verified to match the installed release
    atomic(safe(root, MANIFEST), (folder / 'toolkit' / MANIFEST).read_bytes(), 0o644)
    _verify_workspace(root, _read_json(root / MANIFEST))


def recover(root: Path) -> bool:
    """Restore after a killed update before permitting any CLI command.

    Normalize the trusted workspace root before accessing update metadata.
    macOS aliases /var to /private/var; checking unnormalized ancestors would
    otherwise reject valid metadata despite using a canonical backup path.
    Symlinks *inside* the workspace remain rejected by evolution.safe.
    """
    root = Path(root).resolve()
    journal = store(root) / 'journal.json'
    if not journal.exists():
        return False
    with _exclusive_launch_lock(root):
        data = _read_json(journal)
        _restore(root, data['backup'])
        journal.unlink()
        print('Gaston: interrupted update rolled back; previous version restored.', file=sys.stderr)
        return True


def install(root: Path, manifest: dict, payload: dict, *, allow_recovery=True):
    """Write only manifest-owned files under an exclusive launcher lock."""
    import evolution
    root = Path(root).resolve()
    current = evolution.read(root / MANIFEST)
    old_files = _verify_workspace(root, current)
    next_files = _validated_manifest(root, manifest, manifest['release'])
    if version(manifest['release']) <= version(current['release']):
        raise UpdateError('Release is not newer than installed Gaston')
    if set(next_files) | {MANIFEST} != set(payload):
        raise UpdateError('Incomplete candidate')
    for rel in next_files:
        if hashlib.sha256(payload[rel][0]).hexdigest() != next_files[rel]:
            raise UpdateError('Candidate hash mismatch: ' + rel)
    with _exclusive_launch_lock(root):
        if not evolution.idle(root):
            raise UpdateError('Managed agents or tasks are active. Finish them before updating.')
        local_evolution = root / '.agentic/local/evolution'
        if any((local_evolution / key).exists() for key in ('pending.json', 'journal.json')):
            raise UpdateError('Pending local evolution must be resolved before updating')
        if (store(root) / 'journal.json').exists():
            raise UpdateError('Unrecovered previous update; restart dev to recover')
        # Recheck after lock acquisition to prevent simultaneous local edits.
        current = evolution.read(root / MANIFEST)
        old_files = _verify_workspace(root, current)
        # Do not take a backup or create a rollback journal until additions
        # are known not to collide with an app-owned or user-owned file.
        for rel in set(next_files) - set(old_files):
            if evolution.safe(root, rel).exists():
                raise UpdateError('Refusing to overwrite non-toolkit file: ' + rel)
        manifest_bytes = (root / MANIFEST).read_bytes()
        identifier, _ = _backup(root, old_files, next_files, manifest_bytes)
        journal = store(root) / 'journal.json'
        _put_json(journal, {'backup': identifier, 'target': manifest['release']})
        try:
            for rel in sorted(next_files):
                target = evolution.safe(root, rel)
                if rel not in old_files and target.exists():
                    raise UpdateError('Refusing to overwrite non-toolkit file: ' + rel)
                original_mode = target.stat().st_mode if target.exists() else payload[rel][1]
                mode = 0o755 if rel == 'dev' else (original_mode & 0o777 if rel in old_files else (payload[rel][1] or 0o644))
                evolution.atomic(target, payload[rel][0], mode)
            for rel in sorted(set(old_files)-set(next_files)):
                target = evolution.safe(root, rel)
                if not target.is_file() or evolution.digest(target) != old_files[rel]:
                    raise UpdateError('Obsolete toolkit file changed during update: ' + rel)
                target.unlink()
            evolution.atomic(root / MANIFEST, payload[MANIFEST][0], 0o644)
            _verify_workspace(root, manifest)
            from release_audit import audit
            audit_result = audit(root)
            if audit_result['status'] != 'pass':
                raise UpdateError('Release integrity audit failed: ' + repr(audit_result['failures'][:3]))
        except BaseException:
            if allow_recovery:
                _restore(root, identifier)
                journal.unlink(missing_ok=True)
            raise
        journal.unlink()
        # Keep the backup for explicit manual recovery; do not delete saved state.
        return identifier


def command(root: Path, argv: list[str], *, confirm=input, display=print,
            fetch=_fetch_json, download=_download):
    parser = argparse.ArgumentParser(prog='dev update', description='Review and install signed Gaston GitHub releases')
    parser.add_argument('--check', action='store_true', help='Check for an update without installing')
    parser.add_argument('--set-repo', metavar='OWNER/REPO', help='Trust the public official Gaston repository')
    arguments = parser.parse_args(argv)
    if arguments.set_repo:
        set_repository(root, arguments.set_repo, confirm=confirm, display=display)
        return 0
    repo = configured_repository(root)
    if not repo:
        display('No official public Gaston repository is configured yet.')
        display('Once it exists, run dev update --set-repo OWNER/REPO once. No update was downloaded.')
        return 2
    found = latest(repo, fetch=fetch)
    current = _read_json(Path(root) / MANIFEST)
    _validated_manifest(root, current)
    if version(found['tag']) <= version(current['release']):
        display(f'Gaston is current: {current["release"]} (trusted source: {repo}).')
        return 0
    display(f'Gaston: {current["release"]} → {found["tag"]} (publisher: {repo})')
    if found['notes']:
        display('Release notes:\n' + found['notes'])
    if arguments.check:
        return 0
    display('Checking the signed tag and candidate integrity before approval...')
    commit = signed_release_commit(repo, found['tag'], fetch=fetch)
    # Archive is pinned to the VERIFIED tag's commit, never a floating branch.
    raw = download(_github_url(repo, 'zipball/' + commit), MAX_ARCHIVE)
    manifest, payload = _validate_candidate(raw, found['tag'])
    if confirm(f'Type INSTALL {found["tag"]} to update only Gaston toolkit files: ').strip() != 'INSTALL ' + found['tag']:
        display('Update cancelled. No files changed.')
        return 0
    backup = install(root, manifest, payload)
    _put_json(store(root) / 'check.json', {'repository':repo,'checked_at':time.time(),**found})
    display(f'Gaston updated to {found["tag"]}. Recovery snapshot: .agentic/local/update/backups/{backup}')
    return 0


def main(root: Path, argv: list[str]) -> int:
    try:
        return command(root, argv)
    except (UpdateError, OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print('Gaston update: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--refresh':
        try:
            _refresh(Path(sys.argv[2]))
        except (OSError, UpdateError, BlockingIOError):
            pass
    else:
        raise SystemExit('Internal updater; use dev update')
