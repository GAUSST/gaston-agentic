"""Offline security, compatibility, and transaction tests for official GitHub updates."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import updater
import evolution


def digest(data):
    return hashlib.sha256(data).hexdigest()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.files = {
            '.agentic/example.py': b'VALUE = "old"\n',
            'AGENTS.md': b'Generic project instructions\n',
            'CLAUDE.md': b'@AGENTS.md\n',
            'README.md': b'Generic user documentation\n',
        }
        self.manifest = self.make_manifest('v1.5.1', self.files)
        for name, data in self.files.items():
            self.write(name, data)
        self.write('.agentic/manifest.json', self.serialize(self.manifest))
        self.write('application/src/main.py', b'PROJECT_CODE = True\n')
        self.write('.git/config', b'[core]\n  bare = false\n')
        self.write('.agentic/local/session.json', b'{"valuable": true}\n')
        self.write('.env', b'PRIVATE_VALUE=keep_this\n')

    @staticmethod
    def serialize(value):
        return (json.dumps(value, indent=2) + '\n').encode()

    @staticmethod
    def make_manifest(tag, files):
        return {'version': 1, 'release': tag, 'build': tag[1:],
                'files': {name: digest(content) for name, content in files.items()}}

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def payload(self, tag='v1.5.2'):
        changed = {**self.files, '.agentic/example.py': b'VALUE = "new"\n'}
        manifest = self.make_manifest(tag, changed)
        contents = {name: (data, 0o644) for name, data in changed.items()}
        contents['.agentic/manifest.json'] = (self.serialize(manifest), 0o644)
        return manifest, contents

    def test_semver_and_manifest_validation(self):
        self.assertLess(updater.version('v1.5.1'), updater.version('v1.5.2'))
        for bad in ('v46', '46.0.10', 'v01.2.3', 'v1.2', 'v1.2.3-rc1'):
            with self.subTest(bad=bad), self.assertRaises(updater.UpdateError):
                updater.version(bad)
        invalid = dict(self.manifest)
        invalid['files'] = {'../steal': digest(b'x')}
        with self.assertRaises(updater.UpdateError):
            updater._validated_manifest(self.root, invalid)
        invalid['files'] = {123: digest(b'x')}
        with self.assertRaises(updater.UpdateError):
            updater._validated_manifest(self.root, invalid)

    def test_repository_explicitly_trusted_no_implicit_download(self):
        self.assertIsNone(updater.configured_repository(self.root))
        output = []
        with patch.object(updater, '_fetch_json', side_effect=AssertionError('network used')):
            self.assertEqual(updater.command(self.root, [], display=output.append), 2)
        self.assertIn('No official public', output[0])
        with self.assertRaises(updater.UpdateError):
            updater.set_repository(self.root, 'owner/repo', confirm=lambda _: 'no')
        self.assertIsNone(updater.configured_repository(self.root))
        updater.set_repository(self.root, 'owner/repo', confirm=lambda _: 'owner/repo', display=output.append)
        self.assertEqual(updater.configured_repository(self.root), 'owner/repo')
        with self.assertRaises(updater.UpdateError):
            updater.set_repository(self.root, 'owner/../repo', confirm=lambda _: '')

    def test_local_metadata_rejects_symlinked_parents(self):
        external = self.root / 'external'
        external.mkdir()
        local = self.root / '.agentic/local'
        # The updater must not follow a linked local-state parent to a
        # separate path, even if that path is writable.
        if local.is_dir():
            import shutil
            shutil.rmtree(local)
        try:
            local.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Host does not permit symlinks')
        with self.assertRaises((ValueError, updater.UpdateError)):
            updater.store(self.root)
        self.assertFalse((external / 'update').exists())

    def test_version_check_and_24h_cache(self):
        updater.set_repository(self.root, 'owner/repo', confirm=lambda _: 'owner/repo', display=lambda _: None)
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return {'tag_name': 'v1.5.2', 'body': 'one fix', 'draft': False,
                    'prerelease': False, 'published_at': '2026-11-24T00:00:00Z',
                    'html_url': 'https://github.com/owner/repo/releases/tag/v1.5.2'}
        initial = updater._check(self.root, fetch=mock_fetch, now=100_000)
        self.assertEqual(initial['tag'], 'v1.5.2')
        self.assertEqual(updater._check(self.root, fetch=mock_fetch, now=100_060), initial)
        self.assertEqual(len(calls), 1)
        self.assertEqual(updater._check(self.root, fetch=mock_fetch, now=100_000+86401)['tag'], 'v1.5.2')
        self.assertEqual(len(calls), 2)
        msg = []
        updater._show_notice(self.root, initial, display=msg.append)
        self.assertEqual(len(msg), 1)
        self.assertIn('dev update', msg[0])
        # Cache corruption must never install anything or stop a launch.
        self.write('.agentic/local/update/check.json', b'{"checked_at": "invalid", "repository": "owner/repo"}')
        with patch.object(updater.subprocess, 'Popen') as spawn:
            updater.notice(self.root)
            spawn.assert_called_once()

    def test_signed_annotated_tag_required(self):
        repo = 'publisher/gaston-agentic'
        ref = 'https://api.github.com/repos/' + repo + '/git/ref/tags/v1.5.2'
        signed = 'https://api.github.com/repos/' + repo + '/git/tags/' + 'a'*40
        def fetch(url):
            if url == ref:
                return {'object': {'type': 'tag', 'sha': 'a'*40}}
            if url == signed:
                return {'tag': 'v1.5.2', 'verification': {'verified': True, 'reason': 'valid'},
                        'object': {'type': 'commit', 'sha': 'b'*40}}
            raise AssertionError(url)
        self.assertEqual(updater.signed_release_commit(repo, 'v1.5.2', fetch=fetch), 'b'*40)
        with self.assertRaises(updater.UpdateError):
            updater.signed_release_commit(repo, 'v1.5.2', fetch=lambda _: {'object': {'type': 'commit', 'sha': 'b'*40}})
        def unsigned(url):
            result = fetch(url)
            if url == signed:
                return {**result, 'verification': {'verified': False, 'reason': 'unsigned'}}
            return result
        with self.assertRaises(updater.UpdateError):
            updater.signed_release_commit(repo, 'v1.5.2', fetch=unsigned)

    def test_release_archive_exact_hashes_and_paths(self):
        manifest, contents = self.payload()
        def archive(add=None):
            out = BytesIO()
            with zipfile.ZipFile(out, 'w') as z:
                for name, (value, _) in contents.items():
                    z.writestr('source-rev123/' + name, value)
                if add:
                    z.writestr('source-rev123/' + add[0], add[1])
            return out.getvalue()
        self.assertEqual(updater._validate_candidate(archive(), 'v1.5.2')[0], manifest)
        with self.assertRaises(updater.UpdateError):
            updater._validate_candidate(archive(), 'v1.5.3')
        with self.assertRaises(updater.UpdateError):
            updater._validate_candidate(archive(('../hijack', b'evil')), 'v1.5.2')
        with self.assertRaises(updater.UpdateError):
            updater._validate_candidate(archive(('README.MD', b'case-collision')), 'v1.5.2')
        altered = dict(contents)
        altered['README.md'] = (b'TAMPER\n', 0o644)
        with zipfile.ZipFile(BytesIO(), 'w'):
            pass
        source = contents['README.md']
        contents['README.md'] = altered['README.md']
        try:
            with self.assertRaises(updater.UpdateError):
                updater._validate_candidate(archive(), 'v1.5.2')
        finally:
            contents['README.md'] = source

    def test_confirmed_transaction_preserves_all_local_and_git(self):
        next_manifest, payload = self.payload()
        before = {name: (self.root / name).read_bytes() for name in (
            'application/src/main.py', '.git/config', '.agentic/local/session.json', '.env')}
        with patch.object(evolution, 'idle', return_value=True):
            recovery_id = updater.install(self.root, next_manifest, payload)
        self.assertTrue(recovery_id.startswith('u-'))
        self.assertEqual((self.root/'.agentic/example.py').read_text(), 'VALUE = "new"\n')
        self.assertEqual(json.loads((self.root/'.agentic/manifest.json').read_text())['release'], 'v1.5.2')
        for name, value in before.items():
            self.assertEqual((self.root / name).read_bytes(), value, name)
        self.assertEqual(updater._verify_workspace(self.root, next_manifest), next_manifest['files'])

    def test_modified_owned_file_blocks_update_without_touching_app(self):
        next_manifest, payload = self.payload()
        self.write('.agentic/example.py', b'LOCAL CUSTOMIZATION\n')
        with patch.object(evolution, 'idle', return_value=True), self.assertRaises(updater.UpdateError):
            updater.install(self.root, next_manifest, payload)
        self.assertEqual((self.root/'.agentic/example.py').read_bytes(), b'LOCAL CUSTOMIZATION\n')
        self.assertEqual((self.root/'application/src/main.py').read_bytes(), b'PROJECT_CODE = True\n')

    def test_failed_install_rolls_back_old_toolkit(self):
        import release_audit
        next_manifest, payload = self.payload()
        previous = (self.root/'.agentic/manifest.json').read_bytes()
        with patch.object(evolution, 'idle', return_value=True), patch.object(release_audit, 'audit', return_value={'status': 'fail', 'failures': ['simulated']}):
            with self.assertRaises(updater.UpdateError):
                updater.install(self.root, next_manifest, payload)
        self.assertEqual((self.root/'.agentic/example.py').read_bytes(), self.files['.agentic/example.py'])
        self.assertEqual((self.root/'.agentic/manifest.json').read_bytes(), previous)
        self.assertFalse((self.root/'.agentic/local/update/journal.json').exists())

    def test_new_release_path_collision_preserves_user_file(self):
        next_manifest, payload = self.payload()
        new_name = '.agentic/special_user_file.py'
        self.write(new_name, b'USER CONTENT MUST SURVIVE\n')
        next_manifest['files'][new_name] = digest(b'RELEASE NEW FILE\n')
        payload[new_name] = (b'RELEASE NEW FILE\n', 0o644)
        with patch.object(evolution, 'idle', return_value=True), self.assertRaises(updater.UpdateError):
            updater.install(self.root, next_manifest, payload)
        self.assertEqual((self.root/new_name).read_bytes(), b'USER CONTENT MUST SURVIVE\n')
        self.assertEqual((self.root/'.agentic/example.py').read_bytes(), self.files['.agentic/example.py'])
        self.assertFalse((updater.store(self.root)/'journal.json').exists())

    def test_recovery_refuses_modified_new_file_without_partial_restore(self):
        next_manifest, payload = self.payload()
        added = '.agentic/added.py'
        next_manifest['files'][added] = digest(b'RELEASE FILE\n')
        payload[added] = (b'RELEASE FILE\n', 0o644)
        identifier, _ = updater._backup(self.root, self.manifest['files'], next_manifest['files'],
                                        (self.root/'.agentic/manifest.json').read_bytes())
        self.write('.agentic/example.py', payload['.agentic/example.py'][0])
        self.write(added, b'USER EDITED NEW FILE\n')
        updater._put_json(updater.store(self.root) / 'journal.json', {'backup': identifier, 'target': 'v1.5.2'})
        with self.assertRaises(updater.UpdateError):
            updater.recover(self.root)
        self.assertEqual((self.root/added).read_bytes(), b'USER EDITED NEW FILE\n')
        self.assertEqual((self.root/'.agentic/example.py').read_bytes(), b'VALUE = "new"\n')
        self.assertTrue((updater.store(self.root)/'journal.json').exists())

    def test_recovery_accepts_canonical_workspace_alias(self):
        # macOS tempfile roots may arrive as /var while the OS resolves them
        # to /private/var. An aliased workspace root must not break recovery.
        with tempfile.TemporaryDirectory() as other:
            alias = Path(other) / 'workspace-alias'
            try:
                alias.symlink_to(self.root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('Directory symlinks not supported')
            next_manifest, payload = self.payload()
            identifier, _ = updater._backup(
                self.root, self.manifest['files'], next_manifest['files'],
                (self.root / '.agentic/manifest.json').read_bytes())
            self.write('.agentic/example.py', payload['.agentic/example.py'][0])
            updater._put_json(updater.store(self.root) / 'journal.json',
                              {'backup': identifier, 'target': 'v1.5.2'})
            self.assertTrue(updater.recover(alias))
            self.assertEqual((self.root / '.agentic/example.py').read_bytes(),
                             self.files['.agentic/example.py'])
            self.assertFalse((updater.store(self.root) / 'journal.json').exists())

    def test_interrupted_update_recovered_before_next_command(self):
        next_manifest, payload = self.payload()
        identifier, _ = updater._backup(self.root, self.manifest['files'], next_manifest['files'],
                                        (self.root/'.agentic/manifest.json').read_bytes())
        self.write('.agentic/example.py', payload['.agentic/example.py'][0])
        updater._put_json(updater.store(self.root) / 'journal.json', {'backup': identifier, 'target':'v1.5.2'})
        self.assertTrue(updater.recover(self.root))
        self.assertEqual((self.root/'.agentic/example.py').read_bytes(), self.files['.agentic/example.py'])
        self.assertFalse((updater.store(self.root)/'journal.json').exists())


if __name__ == '__main__':
    unittest.main()
