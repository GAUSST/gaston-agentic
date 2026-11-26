"""Managed Android acceptance for the current Flutter application build."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import time


def apk_tool(adb):
    sdk = Path(adb).resolve().parent.parent
    candidates = []
    for folder in (sdk / 'build-tools').glob('*'):
        if not re.fullmatch(r'\d+(?:\.\d+){1,3}', folder.name):
            continue
        for name in ('aapt', 'aapt.exe'):
            binary = folder / name
            if binary.is_file():
                candidates.append((tuple(map(int, folder.name.split('.'))), str(binary)))
    return max(candidates)[1] if candidates else shutil.which('aapt')


def apk_identity(output):
    package = re.search(r"(?m)^package: name='([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)'", output)
    activity = re.search(r"(?m)^launchable-activity: name='([A-Za-z_.$][A-Za-z0-9_.$]*)'", output)
    if not package or not activity:
        return None
    app_id, name = package.group(1), activity.group(1)
    if name.startswith('.'):
        name = app_id + name
    elif '.' not in name:
        name = app_id + '.' + name
    return app_id, app_id + '/' + name


def flow_path(root, app, relative_path):
    candidate = root / relative_path if relative_path else app / '.maestro'
    if not candidate.resolve().is_relative_to(root) or candidate.is_symlink():
        return None
    if candidate.is_file() and candidate.suffix.lower() in ('.yaml', '.yml'):
        return candidate.resolve()
    if candidate.is_dir() and any(
            p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(candidate.resolve())
            for p in candidate.rglob('*') if p.suffix.lower() in ('.yaml', '.yml')):
        return candidate.resolve()
    return None


def run(root, project, relative_path=None, *, execute, sdk_tool, avd_names,
        start_emulator, finish_emulator, maestro_run, screenshot=None,
        clock=time.monotonic, sleep=time.sleep):
    """Build before installing, pin device commands, and clean up owned AVDs."""
    root = Path(root).resolve()
    app = (root / project).resolve()
    stages = {}
    details = {'project': project, 'stages': stages}
    device = None
    used_emulator = False
    adb = sdk_tool('adb')

    def outcome(status, reason=None):
        return {'status': status, **details, **({'reason': reason} if reason else {})}

    def command(stage, argv, timeout):
        value = execute(argv, cwd=app, timeout=timeout)
        stages[stage] = {'command': list(map(str, argv)), **value}
        return value

    def failed(value, reason):
        return outcome('blocked' if value.get('exit_code') is None else 'fail', reason)

    def emulators():
        listing = execute([adb, 'devices'], cwd=root, timeout=15)
        if listing.get('exit_code') != 0:
            stages['adb_devices'] = listing
            return None
        return [(m.group(1), m.group(2)) for line in listing.get('stdout', '').splitlines()
                if (m := re.fullmatch(r'(emulator-\d+)\s+(\S+)', line.strip()))]

    def pipeline():
        nonlocal device, used_emulator
        if (not app.is_relative_to(root) or not (app / 'pubspec.yaml').is_file()
                or not (app / 'android').is_dir()):
            return outcome('blocked', 'Select a local Flutter application with an Android target')
        if relative_path and (Path(relative_path).is_absolute() or '..' in Path(relative_path).parts):
            return outcome('blocked', 'Maestro flow must be workspace-relative')
        flow = flow_path(root, app, relative_path)
        if not flow:
            return outcome('blocked', 'No existing Maestro YAML flow for this application')
        from platform_compat import resolve_flutter, maestro_binary
        try:
            flutter = resolve_flutter(app, workspace=root)
        except (OSError, ValueError) as exc:
            return outcome('blocked', str(exc))
        if not adb:
            return outcome('blocked', 'Android adb is unavailable')
        if not maestro_binary():
            return outcome('blocked', 'Maestro CLI is unavailable')
        aapt = apk_tool(adb)
        if not aapt:
            return outcome('blocked', 'Android SDK build-tools (aapt) are unavailable')
        listing = emulators()
        if listing is None or len(listing) > 1:
            return outcome('blocked', 'Cannot identify a single Android emulator')
        available = avd_names() if not listing else []
        if not listing and len(available) != 1:
            return outcome('blocked', 'Start a test emulator or configure exactly one AVD')
        if listing:
            device = listing[0][0]

        built = command('build', [flutter, 'build', 'apk', '--debug', '--no-pub'], 900)
        if built.get('exit_code') != 0:
            return failed(built, 'Flutter debug APK build did not complete successfully')
        relative_apk = 'build/app/outputs/flutter-apk/app-debug.apk'
        apk = app / relative_apk
        # Require the current build to identify the artifact. A failed or
        # custom build must never silently install a previously built APK.
        output = built.get('stdout', '') + '\n' + built.get('stderr', '')
        if (not re.search(r'\bBuilt\s+(?:\./)?' + re.escape(relative_apk) + r'(?:\s|$)',
                          output.replace('\\', '/'))
                or not apk.is_file() or apk.resolve() != apk):
            return outcome('blocked', 'Build did not identify the standard debug APK; custom flavors/paths require a project-specific runner')
        with apk.open('rb') as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        details.update(apk=apk.relative_to(root).as_posix(), apk_sha256=digest.hexdigest())
        metadata = execute([aapt, 'dump', 'badging', str(apk)], cwd=app,
                           timeout=30, output_limit=65536)
        if metadata.get('exit_code') != 0:
            stages['apk_identity'] = metadata
            return failed(metadata, 'Cannot inspect the built APK')
        parsed = apk_identity(metadata.get('stdout', ''))
        if not parsed:
            return outcome('blocked', 'APK has no identifiable package and launchable activity')
        app_id, component = parsed
        details.update(app_id=app_id, launch_component=component)

        # An offline emulator is still an emulator: never start a duplicate.
        listing = emulators()
        if listing is None or len(listing) > 1:
            return outcome('blocked', 'Android emulator selection changed during build')
        if device and (not listing or listing[0][0] != device):
            return outcome('blocked', 'Selected Android emulator changed during build')
        if not listing:
            used_emulator = True
            started = start_emulator(available[0])
            stages['emulator_start'] = started
            if started.get('status') not in ('starting', 'ready', 'pass'):
                return outcome('blocked', 'Existing AVD could not be started')
            if started.get('cleanup_tracking') == 'unverified':
                return outcome('blocked', 'Emulator ownership is unverified; no app installed')
        else:
            used_emulator = True
            device = listing[0][0]
        deadline = clock() + 180
        while clock() < deadline:
            listing = emulators()
            if listing is None or len(listing) > 1:
                return outcome('blocked', 'Android emulator became ambiguous or unavailable')
            if listing:
                serial, state = listing[0]
                if device and serial != device:
                    return outcome('blocked', 'Selected Android emulator changed')
                device = serial
                if state == 'device':
                    boot = execute([adb, '-s', device, 'shell', 'getprop', 'sys.boot_completed'],
                                   cwd=root, timeout=15)
                    if boot.get('exit_code') == 0 and boot.get('stdout', '').strip() == '1':
                        break
            sleep(2)
        else:
            return outcome('blocked', 'Android emulator did not finish booting before deadline')
        details['device'] = device

        installed = command('install', [adb, '-s', device, 'install', '-r', str(apk)], 180)
        if installed.get('exit_code') != 0 or not re.search(r'(?m)^Success\s*$', installed.get('stdout', '')):
            return failed(installed, 'Current APK was not confirmed installed')
        # Stop only the selected package before starting its resolved launcher.
        launched = command('launch', [adb, '-s', device, 'shell', 'am', 'start', '-W', '-S',
                                      '-n', component], 60)
        if launched.get('exit_code') != 0 or not re.search(r'(?m)^Status:\s*ok\s*$', launched.get('stdout', '')):
            return failed(launched, 'Android did not confirm successful application launch')
        alive = command('process', [adb, '-s', device, 'shell', 'pidof', app_id], 15)
        if alive.get('exit_code') != 0 or not re.fullmatch(r'\d+(?:\s+\d+)*', alive.get('stdout', '').strip()):
            return failed(alive, 'Application process is not confirmed running after launch')
        details['app_started'] = True
        tested = maestro_run(flow.relative_to(root).as_posix(), expected_device=device, cwd=app)
        stages['maestro'] = tested
        return outcome(tested.get('status') if tested.get('status') in ('pass', 'fail', 'blocked') else 'blocked',
                       tested.get('reason'))

    result = None
    try:
        result = pipeline()
    except (OSError, ValueError, RuntimeError) as exc:
        result = outcome('blocked', 'Mobile verification could not complete: ' + str(exc))
    finally:
        if used_emulator:
            if device and screenshot and result and 'launch' in stages and result['status'] != 'pass':
                try:
                    artifact = screenshot(device, adb)
                    if artifact:
                        details['failure_screenshot'] = artifact
                except (OSError, ValueError, RuntimeError):
                    pass
            try:
                cleanup = finish_emulator()
            except (OSError, ValueError, RuntimeError) as exc:
                cleanup = {'status': 'blocked', 'reason': str(exc)}
            if result is not None:
                result.update(details, emulator_cleanup=cleanup)
                if cleanup.get('status') == 'blocked' and result['status'] == 'pass':
                    result.update(status='blocked', reason='Tests passed but emulator cleanup is unconfirmed')
    return result
