"""Filesystem lock and installed CLI portability, independent of providers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import platform_compat as pc

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import gaston_cli

class NativePlatformTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows venv redirector integration')
    def test_real_venv_worker_claims_reserved_launcher_pid(self):
        import json
        import socket
        import time
        import venv
        import orchestration as orch
        import runtime_recovery as recovery
        from project_layout import project_state
        from session_store import process_identity

        with tempfile.TemporaryDirectory(prefix='gaston worker claim ') as td:
            root = Path(td).resolve()
            env_dir = root / 'bootstrap'
            venv.EnvBuilder(with_pip=False, system_site_packages=True).create(env_dir)
            python = env_dir / 'Scripts/python.exe'
            folder = project_state(root) / 'runs' / 'venv-worker-claim'
            folder.mkdir(parents=True)
            attempt = 'a' * 32
            started = time.time()
            orch.put(folder / 'state.json', {'stage': 'queued_for_builder'})
            orch.put(folder / 'worker-launch.json', {
                'attempt': attempt, 'started_at': started,
                'launcher': {'pid': os.getpid(), 'host': socket.gethostname(),
                             **process_identity(os.getpid())},
            })
            script = (
                "import sys, os, json; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); import runtime_recovery as r; "
                "ok=r.claim_worker(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]); "
                "print(json.dumps(dict(ok=ok,pid=os.getpid(),parent=os.getppid()))); "
                "sys.exit(0 if ok else 1)"
            )
            with orch.mutation_guard(root):
                proc = subprocess.Popen(
                    [str(python), '-c', script, str(ROOT / '.agentic'),
                     str(root), str(folder), attempt],
                    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, **pc.popen_kwargs()
                )
                try:
                    identity = process_identity(proc.pid)
                    self.assertTrue(identity.get('start_ticks'), identity)
                    orch.put(folder / 'worker.json', {
                        'pid': proc.pid, 'attempt': attempt, 'started_at': started,
                        'host': socket.gethostname(), **identity,
                    })
                except BaseException:
                    proc.kill()
                    proc.communicate()
                    raise
            try:
                stdout, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            self.assertEqual(proc.returncode, 0, stdout + stderr)
            result = json.loads(stdout.strip().splitlines()[-1])
            self.assertTrue(result['ok'])
            self.assertIn(proc.pid, (result['pid'], result['parent']))
            claimed = orch.read_record(folder / 'worker.json')
            self.assertEqual(claimed['pid'], result['pid'])
            self.assertEqual(claimed['attempt'], attempt)
            self.assertTrue(claimed['claimed_at'])
            # A separate process may not take over the already-claimed attempt.
            self.assertFalse(recovery.claim_worker(root, folder, attempt))

    def test_dead_unclaimed_worker_does_not_wait_for_live_lead(self):
        import time
        import task_runtime
        started = time.time() - task_runtime.WORKER_STARTUP_GRACE_SECONDS - 1
        worker = {'pid': 4321, 'attempt': 'a'*32, 'started_at': started,
                  'start_ticks': '12345'}
        launch = {'attempt': 'a'*32, 'started_at': started, 'launcher': {'pid': 1234}}
        state = {'stage': 'queued_for_builder', 'queued_at': started}
        with patch('session_store.process_state', side_effect=lambda record:
                   'dead' if record.get('pid') == 4321 else 'live'):
            self.assertEqual(task_runtime.worker_ownership(state, worker, launch),
                             ('dead', True))
            worker['started_at'] = time.time()
            self.assertEqual(task_runtime.worker_ownership(state, worker, launch),
                             ('live', True))

    def test_windows_certificate_error_retries_with_native_transport_only(self):
        import ssl
        from urllib.error import URLError
        import updater
        url = 'https://api.github.com/repos/publisher/gaston/releases/latest'
        error = URLError(ssl.SSLCertVerificationError(1, 'issuer unavailable'))
        with patch.object(updater, 'urlopen', side_effect=error), \
             patch.object(updater, '_download_windows_native', return_value=b'ok') as native:
            with patch.object(updater.os, 'name', 'nt'):
                self.assertEqual(updater._download(url, 16), b'ok')
            native.assert_called_once_with(url, 16)
            native.reset_mock()
            with patch.object(updater.os, 'name', 'posix'):
                with self.assertRaises(updater.UpdateError):
                    updater._download(url, 16)
            native.assert_not_called()
        with patch.object(updater, 'urlopen', side_effect=URLError('offline')), \
             patch.object(updater, '_download_windows_native') as native:
            with patch.object(updater.os, 'name', 'nt'):
                with self.assertRaises(updater.UpdateError):
                    updater._download(url, 16)
            native.assert_not_called()

    def test_windows_native_transport_validates_redirects_and_limits(self):
        import updater
        from types import SimpleNamespace
        url = 'https://api.github.com/repos/publisher/gaston/zipball/abc'
        redirect = 'https://codeload.github.com/publisher/gaston/legacy.zip/abc'
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            self.assertEqual(args[1], '--disable')
            self.assertIn('--max-filesize', args)
            self.assertIn('=https', args)
            self.assertNotIn('--insecure', args)
            self.assertNotIn('--location', args)
            self.assertNotIn('--cacert', args)
            self.assertNotIn('CURL_CA_BUNDLE', kwargs['env'])
            body = Path(args[args.index('--output') + 1])
            if args[-1] == url:
                body.write_bytes(b'')
                return SimpleNamespace(returncode=0, stdout=('302\r\n' + redirect).encode())
            self.assertEqual(args[-1], redirect)
            body.write_bytes(b'archive')
            return SimpleNamespace(returncode=0, stdout=b'200\r\n')

        with patch.object(updater, '_windows_system_curl', return_value=Path('system-curl.exe')), \
             patch.object(updater.subprocess, 'run', side_effect=run), \
             patch.dict(os.environ, {'CURL_CA_BUNDLE': 'untrusted.pem'}):
            self.assertEqual(updater._download_windows_native(url, 20), b'archive')
            self.assertEqual(len(calls), 2)
            calls.clear()
            redirect = 'https://evil.example/archive'
            with self.assertRaises(updater.UpdateError):
                updater._download_windows_native(url, 20)
            self.assertEqual(len(calls), 1)
            redirect = 'http://codeload.github.com/archive'
            with self.assertRaises(updater.UpdateError):
                updater._download_windows_native(url, 20)
            redirect = 'https://codeload.github.com/publisher/gaston/legacy.zip/abc'
            with self.assertRaisesRegex(updater.UpdateError, 'size limit'):
                updater._download_windows_native(url, 3)
        with patch.object(updater, '_windows_system_curl', return_value=Path('system-curl.exe')), \
             patch.object(updater.subprocess, 'run',
                          return_value=SimpleNamespace(returncode=60, stdout=b'')) as run:
            with self.assertRaisesRegex(updater.UpdateError, 'curl exit 60'):
                updater._download_windows_native(url, 20)
            self.assertEqual(run.call_count, 1)

    def test_windows_curl_write_out_parser_accepts_lf_and_crlf(self):
        import updater
        url = 'https://codeload.github.com/publisher/gaston/legacy.zip/abc'
        for sep in (b'\n', b'\r\n'):
            self.assertEqual(updater._parse_windows_http_response(b'200' + sep), (200, ''))
            self.assertEqual(updater._parse_windows_http_response(b'302' + sep + url.encode()),
                             (302, url))
        for bad in (b'200', b'20\n', b'200\r\r\n', b'200\n\xff'):
            with self.assertRaises(updater.UpdateError):
                updater._parse_windows_http_response(bad)
        _, bad_url = updater._parse_windows_http_response(b'302\r\n' + url.encode() + b'\r\n')
        with self.assertRaises(updater.UpdateError):
            updater._github_download_url(bad_url)

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows curl write-out bytes')
    def test_windows_system_curl_write_out_against_local_http_fixture(self):
        import updater
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from threading import Thread

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/ok')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'ok')
            def log_message(self, *_args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = 'http://127.0.0.1:' + str(server.server_port)
                for path, expected in (('/ok', (200, '')), ('/redirect', (302, base + '/ok'))):
                    result = subprocess.run(
                        [str(updater._windows_system_curl()), '--disable', '--silent',
                         '--show-error', '--noproxy', '*', '--output', os.devnull,
                         '--write-out', '%{http_code}\n%{redirect_url}', base + path],
                        capture_output=True, timeout=10, check=True)
                    self.assertEqual(updater._parse_windows_http_response(result.stdout),
                                     expected, repr(result.stdout))
            finally:
                server.shutdown()
                thread.join(timeout=5)

    @unittest.skipUnless(os.name == 'nt', 'Windows system HTTPS executable')
    def test_windows_system_curl_is_native_tls_client(self):
        import updater
        curl = updater._windows_system_curl()
        self.assertTrue(curl.is_absolute())
        result = subprocess.run([str(curl), '--disable', '--version'],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertIn('Schannel', result.stdout)
        self.assertIn('https', result.stdout.lower())

    def test_flutter_fvm_pin_precedes_path_and_missing_pin_blocks(self):
        import json
        with tempfile.TemporaryDirectory(prefix='gaston fvm fixture ') as td:
            root = Path(td).resolve()
            app = root / 'workspace' / 'mobile'
            app.mkdir(parents=True)
            cache = root / 'fvm cache'
            binary = 'flutter.bat' if os.name == 'nt' else 'flutter'
            sdk = cache / 'versions' / '3.50.1' / 'bin' / binary
            sdk.parent.mkdir(parents=True)
            sdk.write_text('fixture', encoding='utf-8')
            (app / '.fvmrc').write_text(json.dumps({'flutter': '3.50.1'}))
            with patch.dict(os.environ, {'FVM_CACHE_PATH': str(cache)}), \
                 patch('shutil.which', return_value='wrong-system-flutter') as fallback:
                self.assertEqual(pc.resolve_flutter(app, workspace=root), str(sdk.resolve()))
                fallback.assert_not_called()
                sdk.unlink()
                with self.assertRaisesRegex(FileNotFoundError, '3.50.1'):
                    pc.resolve_flutter(app, workspace=root)
                fallback.assert_not_called()
                (app / '.fvmrc').unlink()
                (app.parent / '.fvmrc').write_text(json.dumps({'flutter': '3.50.1'}))
                with self.assertRaisesRegex(FileNotFoundError, '3.50.1'):
                    pc.resolve_flutter(app, workspace=root)
                (app.parent / '.fvmrc').unlink()
                self.assertEqual(pc.resolve_flutter(app, workspace=root), 'wrong-system-flutter')

    @unittest.skipUnless(os.name == 'nt', 'Native Windows Flutter batch integration')
    def test_windows_mcp_runs_fvm_flutter_batch_for_analyze_test_and_build(self):
        import json
        import verification_mcp as vm
        with tempfile.TemporaryDirectory(prefix='gaston flutter windows ') as td:
            root = Path(td).resolve()
            app = root / 'app'
            app.mkdir()
            cache = root / 'fvm cache'
            sdk = cache / 'versions' / '3.50.1' / 'bin' / 'flutter.bat'
            sdk.parent.mkdir(parents=True)
            sdk.write_text('@echo off\n'
                           'echo SDK=3.50.1\n'
                           'echo ARGS=%*\n'
                           'exit /b 0\n', encoding='ascii')
            (app / '.fvmrc').write_text(json.dumps({'flutter': '3.50.1'}))
            with patch.object(vm, 'ROOT', root), \
                 patch.dict(os.environ, {'FVM_CACHE_PATH': str(cache)}):
                for args in (['analyze'], ['test'], ['build', 'apk', '--debug', '--no-pub']):
                    result = vm.execute(['flutter', *args], cwd=app, timeout=30)
                    self.assertEqual(result['exit_code'], 0, result)
                    self.assertIn('SDK=3.50.1', result['stdout'])
                    self.assertIn('ARGS=' + ' '.join(args), result['stdout'])
                sdk.unlink()
                result = vm.execute(['flutter', 'analyze'], cwd=app)
                self.assertIsNone(result['exit_code'])
                self.assertIn('3.50.1', result['stderr'])

    def test_reconciled_change_paths_merge_native_and_posix_aliases(self):
        import orchestration as orch
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            orch.put(folder / 'lead-edits.json', {'files': {'lib/theme.dart': {}}})
            built = orch.merge_lead_paths(folder, {'changed_files': [
                r'lib\theme.dart', 'lib/theme.dart', r'lib\other.dart']})
            self.assertEqual(built['changed_files'], ['lib/other.dart', 'lib/theme.dart'])
            self.assertEqual(built['lead_changed_files'], ['lib/theme.dart'])

    @unittest.skipIf(os.name == 'nt', 'POSIX process groups')
    def test_denied_group_signal_requires_proof_no_live_members(self):
        import signal
        from unittest.mock import patch
        with patch.object(pc.os, 'killpg', side_effect=PermissionError(1, 'denied')):
            for snapshot, gone in [('123 Z+\n456 S\n', True), ('456 S\n', True),
                                   ('123 S\n', False), ('bad snapshot\n', False)]:
                with patch.object(pc.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 0, stdout=snapshot, stderr='')):
                    if gone:
                        self.assertFalse(pc._signal_owned_group(123, signal.SIGKILL))
                    else:
                        with self.assertRaises(PermissionError):
                            pc._signal_owned_group(123, signal.SIGKILL)
            with patch.object(pc.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ps', 3)):
                with self.assertRaises(PermissionError):
                    pc._signal_owned_group(123, signal.SIGKILL)

    def test_verification_runner_stdin_is_eof_and_preserves_exit_code(self):
        import verification_mcp as vm
        result = vm.execute([sys.executable, '-c',
                             "import sys; print('EOF', sys.stdin.read() == ''); "
                             "print('diagnostic', file=sys.stderr); sys.exit(7)"], timeout=15)
        self.assertEqual(result['exit_code'], 7, result)
        self.assertIn('EOF True', result['stdout'])
        self.assertIn('diagnostic', result['stderr'])

    def test_verification_runner_finishes_when_descendant_inherits_output(self):
        import time
        import verification_mcp as vm
        code = ("import subprocess, sys; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)']); "
                "print(p.pid, flush=True)")
        started = time.monotonic()
        result = vm.execute([sys.executable, '-c', code], timeout=15)
        self.assertEqual(result['exit_code'], 0, result)
        self.assertLess(time.monotonic()-started, 12, result)
        pid = int(result['stdout'].strip())
        if os.name == 'nt':
            self.assertEqual(pc.process_status(pid), 'dead', result)

    def test_verification_runner_timeout_and_cancellation_release_owned_tree(self):
        import time
        from threading import Event, Timer
        import verification_mcp as vm
        code = ("import subprocess, sys, time; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)']); "
                "print(p.pid, flush=True); time.sleep(90)")
        for cancel in (False, True):
            event = Event()
            timer = Timer(3, event.set) if cancel else None
            token = vm._VERIFICATION_CANCEL.set(event)
            try:
                if timer:
                    timer.start()
                started = time.monotonic()
                result = vm.execute([sys.executable, '-c', code], timeout=15 if cancel else 3)
                self.assertIsNone(result['exit_code'], result)
                self.assertEqual(result['reason'], 'cancelled' if cancel else 'timeout', result)
                self.assertLess(time.monotonic()-started, 12, result)
                pid = int(result['stdout'].strip())
                if os.name == 'nt':
                    self.assertEqual(pc.process_status(pid), 'dead', result)
            finally:
                vm._VERIFICATION_CANCEL.reset(token)
                if timer:
                    timer.cancel()
                    timer.join()

    @unittest.skipUnless(os.name == 'nt', 'Windows named-job interrupted verification')
    def test_interrupted_flutter_reservation_recovers_only_after_owned_job_gone(self):
        import json
        import time
        import verification_coordination as coordination
        from project_layout import project_state
        with tempfile.TemporaryDirectory(prefix='gaston interrupted check ') as td:
            root = Path(td).resolve()
            marker = root / 'running.json'
            code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import verification_coordination as c
import platform_compat as p
root = Path(sys.argv[2])
marker = str(root / 'running.json')
child = 'import json,os,time; from pathlib import Path; Path(' + repr(marker) + ').write_text(json.dumps(os.getpid())); time.sleep(90)'
with c.reserve(root, 'flutter_analyze', '.', task_id='flutter-interrupted') as r:
    p.run_verification_process([sys._base_executable, '-I', '-c', child], cwd=root, timeout=90, job_name=r['windows_job'])
"""
            proc = subprocess.Popen([sys._base_executable, '-I', '-c', code,
                                     str(ROOT / '.agentic'), str(root)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child_pid = None
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not marker.is_file():
                    if proc.poll() is not None:break
                    time.sleep(.05)
                self.assertTrue(marker.is_file(), 'Native verification did not start')
                child_pid = json.loads(marker.read_text())
                row = coordination.active(root)
                self.assertEqual(row['owner_state'], 'live')
                self.assertFalse(pc.windows_verification_job_gone(row['windows_job']))
                self.assertFalse(coordination.recover_proven_interruption(root))
                proc.kill()
                proc.wait(timeout=10)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not pc.windows_verification_job_gone(row['windows_job']):
                    time.sleep(.05)
                self.assertTrue(pc.windows_verification_job_gone(row['windows_job']))
                self.assertEqual(pc.process_status(child_pid), 'dead')
                self.assertTrue(coordination.recover_proven_interruption(root))
                self.assertIsNone(coordination.active(root))
                archived = list((project_state(root)/'local/interrupted-verifications').glob('*.json'))
                self.assertEqual(len(archived), 1)
                self.assertEqual(json.loads(archived[0].read_text())['outcome'], 'interrupted_no_acceptance')
                with coordination.reserve(root,'flutter_tests','.') as r:
                    self.assertIn('windows_job',r)
                self.assertIsNone(coordination.active(root))
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)

    def test_workspace_discovery_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();(root/'.agentic').mkdir();(root/'.agentic/launcher.py').write_text('# fixture')
            inner=root/'a'/'b';inner.mkdir(parents=True)
            with patch.dict(os.environ,{'GASTON_ROOT':''}):
                self.assertEqual(gaston_cli.workspace(inner),root)
            with patch.dict(os.environ,{'GASTON_ROOT':str(root)}):
                self.assertEqual(gaston_cli.workspace(inner),root)
            with patch.dict(os.environ,{'GASTON_ROOT':str(root/'invalid')}):
                with self.assertRaises(ValueError):gaston_cli.workspace(inner)

    def test_kernel_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'lock'
            with pc.file_lock(path):
                with self.assertRaises(BlockingIOError):
                    with pc.file_lock(path,nonblocking=True):pass
            with pc.file_lock(path,nonblocking=True):pass

    def test_lock_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'lock';target=Path(td)/'target';target.write_text('')
            try:path.symlink_to(target)
            except (OSError,NotImplementedError):self.skipTest('Symlink privilege unavailable')
            with self.assertRaises(ValueError):
                with pc.file_lock(path):pass

    def test_workspace_path_normalization_and_scope(self):
        import orchestration as orch
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            (root/'src').mkdir()
            self.assertEqual(orch.relative_path(root,str(root/'src'/'App.py')),'src/App.py')
            self.assertEqual(orch.relative_path(root,'src\\App.py'),'src/App.py')
            self.assertTrue(orch.within('src\\App.py',['src']))
            self.assertFalse(orch.within('src-old/App.py',['src']))
            with self.assertRaises(ValueError):
                orch.relative_path(root,str(root.parent/'outside.py'))
            with self.assertRaises(ValueError):
                orch.relative_path(root,'../outside.py')

    def test_feedback_worker_guard_uses_shared_cross_platform_lock(self):
        import runtime_recovery
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            with runtime_recovery.feedback_worker_guard(root) as first:
                self.assertTrue(first)
                with runtime_recovery.feedback_worker_guard(root) as second:
                    self.assertFalse(second)
            with runtime_recovery.feedback_worker_guard(root) as after:
                self.assertTrue(after)

    def test_claude_windows_hook_command_uses_git_bash_paths_and_round_trips(self):
        import shlex
        import native_hooks
        with patch.object(native_hooks.os, 'name', 'nt'):
            with patch.object(native_hooks.sys, 'executable', r'C:\Program Files\Python\python.exe'):
                command = native_hooks.hook_def(
                    'claude', 'SessionStart',
                    r'C:\Users\Dev User\Gaston\native_hooks.py')['command']
                args = shlex.split(command)
                self.assertEqual(args, [
                    '/c/Program Files/Python/python.exe',
                    '/c/Users/Dev User/Gaston/native_hooks.py',
                    'claude'
                ])
                parsed = native_hooks._hook_command_args(command, 'claude')
                self.assertEqual(parsed, [
                    r'C:\Program Files\Python\python.exe',
                    r'C:\Users\Dev User\Gaston\native_hooks.py',
                    'claude'
                ])

                # v1.6.8 and earlier stored native Windows paths in Claude
                # Bash hooks. POSIX shlex removes their backslashes.
                legacy = (
                    r'C:\Users\LENOVO.local\share\gaston\bootstrap\Scripts\python.exe '
                    r'C:\Users\LENOVO.local\share\gaston\versions\v1.6.8\gaston_engine\native_hooks.py claude'
                )
                self.assertEqual(native_hooks._hook_command_args(legacy, 'claude'), [
                    r'C:\Users\LENOVO.local\share\gaston\bootstrap\Scripts\python.exe',
                    r'C:\Users\LENOVO.local\share\gaston\versions\v1.6.8\gaston_engine\native_hooks.py',
                    'claude'
                ])
                quoted_legacy = (
                    r'"C:\Program Files\Gaston\bootstrap\Scripts\python.exe" '
                    r'"C:\Program Files\Gaston\versions\v1.6.8\gaston_engine\native_hooks.py" claude'
                )
                self.assertEqual(native_hooks._hook_command_args(quoted_legacy, 'claude'), [
                    r'C:\Program Files\Gaston\bootstrap\Scripts\python.exe',
                    r'C:\Program Files\Gaston\versions\v1.6.8\gaston_engine\native_hooks.py',
                    'claude'
                ])


    @unittest.skipUnless(os.name == 'nt', 'Windows-only end-to-end hook repair')
    def test_central_refresh_repairs_legacy_v168_windows_claude_hooks(self):
        import json
        import native_hooks

        with tempfile.TemporaryDirectory(prefix='gaston hook refresh ') as td:
            root = Path(td).resolve()
            global_home = root / 'global'
            versions = global_home / 'versions'
            old = versions / 'v1.6.8' / 'gaston_engine' / 'native_hooks.py'
            current = versions / 'v1.6.12' / 'gaston_engine' / 'native_hooks.py'
            base = global_home / 'bootstrap' / 'Lib' / 'site-packages' / 'gaston_engine' / 'native_hooks.py'
            python = global_home / 'bootstrap' / 'Scripts' / 'python.exe'
            for path in (old, current, base):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# fixture', encoding='utf-8')

            config = root / '.claude' / 'settings.local.json'
            config.parent.mkdir(parents=True)
            events = ('SessionStart', 'SessionEnd', 'PreToolUse', 'Stop')
            legacy_command = subprocess.list2cmdline([str(python), str(old), 'claude'])
            config.write_text(json.dumps({
                'hooks': {
                    event: [{'hooks': [{
                        'type': 'command', 'command': legacy_command, 'timeout': 10
                    }]}] for event in events
                }
            }), encoding='utf-8')

            class Distribution:
                def locate_file(self, name):
                    if name != 'gaston_engine/native_hooks.py':
                        raise AssertionError(name)
                    return base

            with (
                patch.object(native_hooks, 'central_mode', return_value=True),
                patch.object(native_hooks, 'runtime_script', return_value=current),
                patch('central_update.home', return_value=global_home),
                patch('importlib.metadata.distribution', return_value=Distribution()),
                patch.object(native_hooks.sys, 'executable', str(python)),
            ):
                refreshed = native_hooks.refresh_central_provider_hooks(root)

            self.assertEqual(refreshed, {'claude': True, 'codex': False})
            repaired = json.loads(config.read_text(encoding='utf-8'))['hooks']
            for event in events:
                command = repaired[event][0]['hooks'][0]['command']
                self.assertEqual(
                    native_hooks._hook_command_args(command, 'claude'),
                    [str(python), str(current), 'claude']
                )
                self.assertNotIn('C:', command)

    def test_windows_cmd_shim_uses_argument_array(self):
        import base64
        import json
        with patch.object(pc,'WINDOWS',True), patch('shutil.which',side_effect=lambda name: {
            r'C:\\Program Files\\nodejs\\codex.cmd':r'C:\\Program Files\\nodejs\\codex.cmd',
            'powershell.exe':r'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe'
        }.get(name)):
            args=[r'C:\\Program Files\\nodejs\\codex.cmd','exec','--model','model with spaces',r'C:\\work\\file.ts']
            command,env,wrapped=pc.prepare_subprocess(args,{'PATH':r'C:\\Windows'})
        self.assertTrue(wrapped)
        self.assertEqual(json.loads(base64.b64decode(env['GASTON_WINDOWS_BATCH_ARGS']).decode()),args)
        script=base64.b64decode(command[-1]).decode('utf-16le')
        self.assertIn('& $exe @cliArgs',script)
        self.assertNotIn('model with spaces',script)
        self.assertNotIn('GASTON_WINDOWS_BATCH_ARGS', {k for k in env if k != 'GASTON_WINDOWS_BATCH_ARGS'})

    @unittest.skipUnless(os.name == 'nt', 'Windows-only .cmd execution test')
    def test_windows_cmd_shim_executes_real_batch_file(self):
        with tempfile.TemporaryDirectory(prefix='gaston cmd ') as td:
            folder=Path(td)/'Program Files'/'Codex'
            folder.mkdir(parents=True)
            executable=folder/'codex.cmd'
            executable.write_text(
                '@echo off\r\n'
                'echo ARG1=%~1\r\n'
                'echo ARG2=%~2\r\n'
                'echo ARG3=%~3\r\n'
                'echo ARG4=%~4\r\n'
                'exit /b 23\r\n',
                encoding='utf-8')
            target=Path(td)/'project with spaces'/'source file.ts'
            args=[str(executable),'exec','--model','model with spaces',str(target)]
            command,child_env,wrapped=pc.prepare_subprocess(args,dict(os.environ))
            self.assertTrue(wrapped)
            result=subprocess.run(command,env=child_env,text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,23,result.stdout+'\\n'+result.stderr)
            self.assertIn('ARG1=exec',result.stdout)
            self.assertIn('ARG2=--model',result.stdout)
            self.assertIn('ARG3=model with spaces',result.stdout)
            self.assertIn('ARG4='+str(target),result.stdout)

    def test_central_hook_refresh_repairs_missing_old_version_paths(self):
        import json
        import native_hooks

        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            global_home=root/'global'
            versions=global_home/'versions'
            current=versions/'v1.6.8'/'gaston_engine'/'native_hooks.py'
            current.parent.mkdir(parents=True)
            current.write_text('# current hook',encoding='utf-8')
            old=versions/'v1.6.4'/'gaston_engine'/'native_hooks.py'
            base=root/'venv'/'Lib'/'site-packages'/'gaston_engine'/'native_hooks.py'
            base.parent.mkdir(parents=True)
            base.write_text('# base hook',encoding='utf-8')

            class Distribution:
                def locate_file(self,name):
                    return base

            events=('SessionStart','SessionEnd','PreToolUse','Stop')
            configs={}
            for provider,relative in (
                ('claude',Path('.claude')/'settings.local.json'),
                ('codex',Path('.codex')/'hooks.json')):
                path=root/relative
                path.parent.mkdir(parents=True)
                event_hooks={
                    event:[{'hooks':[native_hooks.hook_def(provider,event,str(old))]}]
                    for event in events
                }
                event_hooks['SessionStart'].append({
                    'hooks':[{'type':'command','command':'unrelated-provider-hook'}]
                })
                path.write_text(json.dumps({'hooks':event_hooks},indent=2),encoding='utf-8')
                configs[provider]=path

            with (
                patch.object(native_hooks,'central_mode',return_value=True),
                patch.object(native_hooks,'runtime_script',return_value=current),
                patch('central_update.home',return_value=global_home),
                patch('importlib.metadata.distribution',return_value=Distribution()),
            ):
                refreshed=native_hooks.refresh_central_provider_hooks(root)

            self.assertEqual(refreshed,{'claude':True,'codex':True})
            for provider,path in configs.items():
                data=json.loads(path.read_text(encoding='utf-8'))
                for event in events:
                    commands=[
                        hook['command']
                        for group in data['hooks'][event]
                        for hook in group.get('hooks',[])
                        if hook.get('type')=='command'
                    ]
                    managed=[command for command in commands
                             if (lambda args: len(args)==3 and args[-1]==provider)(
                                 native_hooks._hook_command_args(command,provider))]
                    self.assertEqual(len(managed),1,(provider,event,commands))
                    self.assertEqual(
                        Path(native_hooks._hook_command_args(managed[0],provider)[1]),
                        current
                    )
                self.assertIn('unrelated-provider-hook',[
                    hook.get('command')
                    for group in data['hooks']['SessionStart']
                    for hook in group.get('hooks',[])
                ])
            self.assertFalse(old.exists())

    def test_subprocess_lifecycle(self):
        with subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
                              **pc.popen_kwargs()) as child:
            pc.stop_owned(child,isolated_group=True,grace_seconds=.1)
            self.assertIsNotNone(child.poll())


    def test_windows_orphaned_provider_child_is_detected_from_retained_parent_pid(self):
        import sys
        import types
        import session_store

        class NoSuchProcess(Exception): pass
        class AccessDenied(Exception): pass
        class ZombieProcess(Exception): pass
        class Error(Exception): pass

        class Candidate:
            def __init__(self, info): self.info=info; self.pid=info['pid']
        class Process:
            def __init__(self,pid):
                if pid==100: raise NoSuchProcess()
                self.pid=pid
            def create_time(self): return 200.0
            def ppid(self): return 1

        fake=types.SimpleNamespace(
            NoSuchProcess=NoSuchProcess,AccessDenied=AccessDenied,
            ZombieProcess=ZombieProcess,Error=Error,Process=Process,
            process_iter=lambda fields:[Candidate({'pid':200,'create_time':201.0,'ppid':100})]
        )
        with patch.object(session_store,'WINDOWS',True), patch.object(pc,'WINDOWS',True), patch.dict(sys.modules,{'psutil':fake}):
            self.assertTrue(session_store.provider_child_active({
                'pid':100,'start_ticks':str(200_000_000),'isolated_group':True
            }))

    def test_windows_orphan_detection_fails_closed_when_ancestry_is_inaccessible(self):
        import sys
        import types
        import session_store

        class NoSuchProcess(Exception): pass
        class AccessDenied(Exception): pass
        class ZombieProcess(Exception): pass
        class Error(Exception): pass

        class Candidate:
            pid=200
            info={'pid':200,'create_time':201.0,'ppid':300}
        class Process:
            def __init__(self,pid):
                if pid==100: raise NoSuchProcess()
                self.pid=pid
            def create_time(self): return 200.0
            def ppid(self): raise AccessDenied()
        fake=types.SimpleNamespace(
            NoSuchProcess=NoSuchProcess,AccessDenied=AccessDenied,
            ZombieProcess=ZombieProcess,Error=Error,Process=Process,
            process_iter=lambda fields:[Candidate()]
        )
        with patch.object(session_store,'WINDOWS',True), patch.object(pc,'WINDOWS',True), patch.dict(sys.modules,{'psutil':fake}):
            self.assertTrue(session_store.provider_child_active({
                'pid':100,'start_ticks':str(200_000_000),'isolated_group':True
            }))

if __name__=='__main__':unittest.main()
