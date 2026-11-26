"""Offline regression coverage for the managed mobile acceptance lifecycle."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mobile_acceptance as mobile


class MobileAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / 'app'
        (self.app / 'android').mkdir(parents=True)
        (self.app / 'pubspec.yaml').write_text('name: demo\n')
        (self.app / '.maestro').mkdir()
        (self.app / '.maestro/smoke.yaml').write_text('appId: com.example.app\n---\n- launchApp\n')
        self.apk = self.app / 'build/app/outputs/flutter-apk/app-debug.apk'
        self.apk.parent.mkdir(parents=True)
        self.commands = []
        self.devices = 'emulator-5554\tdevice\n'
        self.build_code = 0
        self.build_output = 'Built build/app/outputs/flutter-apk/app-debug.apk (12MB)'
        self.install_code = 0
        self.launch_output = 'Status: ok\n'
        self.maestro_status = 'pass'
        self.cleanup_status = 'ready'
        self.maestro_calls = []
        self.starts = []
        self.cleanups = []
        self.screenshots = []
        self.now = 0

    @staticmethod
    def output(stdout='', exit_code=0):
        return {'exit_code': exit_code, 'stdout': stdout, 'stderr': ''}

    def execute(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[:2] == ['flutter', 'build']:
            if self.build_code == 0:
                self.apk.write_bytes(b'new current build')
            return self.output(self.build_output, self.build_code)
        if argv[:2] == ['aapt', 'dump']:
            return self.output("package: name='com.example.app' versionCode='1'\n"
                               "launchable-activity: name='com.example.app.MainActivity'\n")
        if argv == ['adb', 'devices']:
            return self.output('List of devices attached\n' + self.devices)
        if 'getprop' in argv:
            return self.output('1\n')
        if 'install' in argv:
            return self.output('Success\n' if self.install_code == 0 else 'Failure\n', self.install_code)
        if 'am' in argv:
            return self.output(self.launch_output)
        if 'pidof' in argv:
            return self.output('1234\n')
        raise AssertionError(argv)

    def start(self, name):
        self.starts.append(name)
        self.devices = 'emulator-5554\tdevice\n'
        return {'status': 'starting', 'cleanup_tracking': 'enabled'}

    def finish(self):
        self.cleanups.append(True)
        return {'status': self.cleanup_status}

    def maestro(self, path, **kwargs):
        self.maestro_calls.append((path, kwargs))
        return {'status': self.maestro_status}

    def screenshot(self, device, adb):
        self.screenshots.append((device, adb))
        return '.gaston/local/mobile-evidence/test.png'

    def sleep(self, seconds):
        self.now += seconds

    def run_check(self, relative_path=None):
        with patch('platform_compat.resolve_flutter', return_value='flutter'), \
                patch('platform_compat.maestro_binary', return_value='maestro'), \
                patch.object(mobile, 'apk_tool', return_value='aapt'):
            return mobile.run(self.root, 'app', relative_path, execute=self.execute,
                              sdk_tool=lambda name: name, avd_names=lambda: ['Test_AVD'],
                              start_emulator=self.start, finish_emulator=self.finish,
                              maestro_run=self.maestro, screenshot=self.screenshot,
                              clock=lambda: self.now, sleep=self.sleep)

    def test_build_install_launch_then_maestro_on_same_device(self):
        self.apk.write_bytes(b'old build')
        result = self.run_check()
        self.assertEqual(result['status'], 'pass', result)
        self.assertTrue(result['app_started'])
        self.assertEqual(result['app_id'], 'com.example.app')
        self.assertEqual(result['device'], 'emulator-5554')
        self.assertEqual(self.apk.read_bytes(), b'new current build')
        self.assertEqual(list(result['stages']), ['build', 'install', 'launch', 'process', 'maestro'])
        self.assertEqual(self.maestro_calls[0][0], 'app/.maestro')
        self.assertEqual(self.maestro_calls[0][1]['expected_device'], 'emulator-5554')
        for command in self.commands:
            if 'install' in command or 'am' in command or 'pidof' in command:
                self.assertEqual(command[:3], ['adb', '-s', 'emulator-5554'])
        self.assertEqual(len(result['apk_sha256']), 64)
        self.assertEqual(self.cleanups, [True])
        self.assertEqual(self.starts, [])
        self.assertEqual(self.screenshots, [])

    def test_failed_build_never_installs_old_apk(self):
        self.apk.write_bytes(b'old build')
        self.build_code = 1
        result = self.run_check()
        self.assertEqual(result['status'], 'fail')
        self.assertFalse(any('install' in c for c in self.commands))
        self.assertFalse(self.maestro_calls)
        self.assertEqual(self.apk.read_bytes(), b'old build')

    def test_custom_artifact_is_blocked(self):
        self.build_output = 'Built build/app/outputs/flutter-apk/app-other-debug.apk'
        self.assertEqual(self.run_check()['status'], 'blocked')
        self.assertFalse(self.maestro_calls)

    def test_starts_avd_when_only_physical_device_is_connected(self):
        self.devices = 'physical-device\tdevice\n'
        result = self.run_check()
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(self.starts, ['Test_AVD'])
        self.assertEqual(self.cleanups, [True])

    def test_ambiguous_emulators_block_before_build(self):
        self.devices += 'emulator-5556\toffline\n'
        self.assertEqual(self.run_check()['status'], 'blocked')
        self.assertFalse(any('build' in c for c in self.commands))
        self.assertFalse(self.starts)

    def test_offline_emulator_times_out_without_starting_duplicate(self):
        self.devices = 'emulator-5554\toffline\n'
        self.assertEqual(self.run_check()['status'], 'blocked')
        self.assertFalse(self.starts)
        self.assertFalse(self.maestro_calls)
        self.assertEqual(self.cleanups, [True])

    def test_install_failure_stops_sequence(self):
        self.install_code = 1
        result = self.run_check()
        self.assertEqual(result['status'], 'fail')
        self.assertNotIn('launch', result['stages'])
        self.assertFalse(self.maestro_calls)
        self.assertEqual(self.cleanups, [True])

    def test_launch_failure_stops_and_records_screenshot(self):
        self.launch_output = 'Error: Activity not started'
        result = self.run_check()
        self.assertEqual(result['status'], 'fail')
        self.assertFalse(self.maestro_calls)
        self.assertEqual(self.screenshots, [('emulator-5554', 'adb')])
        self.assertIn('failure_screenshot', result)
        self.assertEqual(self.cleanups, [True])

    def test_maestro_failure_and_cleanup_are_recorded(self):
        self.maestro_status = 'fail'
        result = self.run_check()
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['stages']['maestro']['status'], 'fail')
        self.assertEqual(self.cleanups, [True])

    def test_cleanup_uncertainty_cannot_be_green(self):
        self.cleanup_status = 'blocked'
        result = self.run_check()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['stages']['maestro']['status'], 'pass')

    def test_missing_flow_and_escape_are_blocked(self):
        self.assertEqual(self.run_check('missing.yaml')['status'], 'blocked')
        self.assertEqual(self.run_check('../outside.yaml')['status'], 'blocked')
        self.assertFalse(self.commands)

    def test_task_dispatch_uses_discovered_flutter_project(self):
        import verification_mcp as vm
        with patch('workspace_checks.discover', return_value=([], {'flutter': ['app']})), \
                patch.object(vm, 'ROOT', self.root), \
                patch.object(mobile, 'run', return_value={'status': 'pass', 'project': 'app'}) as run:
            self.assertEqual(vm.run_project_mobile_tests()['status'], 'pass')
            self.assertEqual(run.call_args.args[:3], (self.root, 'app', None))
            self.assertEqual(vm.run_project_mobile_tests('wrong')['status'], 'blocked')
            self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
