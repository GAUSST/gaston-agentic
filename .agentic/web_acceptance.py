"""Project-local web checks and isolated browser application lifecycle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urlparse

import workspace_checks


def _environment():
    allowed = {'PATH','HOME','USER','LOGNAME','LANG','LC_ALL','LC_CTYPE',
               'TMPDIR','XDG_RUNTIME_DIR','SYSTEMROOT','WINDIR','SYSTEMDRIVE',
               'HOMEDRIVE','HOMEPATH','USERPROFILE','TEMP','TMP','APPDATA',
               'LOCALAPPDATA','PROGRAMFILES','PLAYWRIGHT_BROWSERS_PATH',
               'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY'}
    env = {k:v for k,v in os.environ.items() if k in allowed}
    env.update(CI='true', GSETTINGS_BACKEND='memory', GASTON_E2E='true')
    return env


def _project(root, project):
    root = Path(root).resolve()
    if project not in workspace_checks.web_projects(root):
        raise ValueError('Select an existing discovered web project')
    folder = (root / project).resolve()
    if not folder.is_relative_to(root):
        raise ValueError('Web project escapes workspace')
    return folder


def local_scripts(folder):
    data = json.loads((folder / 'package.json').read_text(encoding='utf-8'))
    scripts = data.get('scripts') or {}
    if not isinstance(scripts, dict):
        return []
    # Explicitly unit-oriented scripts first; never substitute browser E2E.
    test = next((name for name in ('test:unit','test:ci','test') if isinstance(scripts.get(name),str)
                 and scripts[name].strip() and not re.search(r'\b(?:playwright|cypress|maestro)\b',scripts[name])), None)
    checks = [name for name in ('typecheck','lint','format:check','build')
              if isinstance(scripts.get(name), str) and scripts[name].strip()]
    if not test or not checks:
        return []
    return [*checks[:3], test, *[name for name in checks[3:] if name != test]] if len(checks)>3 else checks+[test]


def run_local(root, project, *, execute):
    try:
        folder = _project(root, project)
        names = local_scripts(folder)
        npm = shutil.which('npm')
        if not npm or not names:
            raise ValueError('Require installed npm and project unit test plus typecheck/lint/build scripts')
        if not (folder/'node_modules').is_dir():
            raise ValueError('Project dependencies are missing; prepare them explicitly')
        scripts = json.loads((folder/'package.json').read_text(encoding='utf-8'))['scripts']
    except (OSError, ValueError, TypeError) as exc:
        return {'status':'blocked','project':project,'reason':str(exc)}
    results = {}
    for name in names:
        args = [npm,'run',name]
        if name.startswith('test') and re.fullmatch(r'vitest(?:\s+watch)?', scripts[name].strip()):
            args += ['--','run']
        result = execute(args, cwd=folder, env=_environment(), timeout=600)
        results[name] = result
        if result.get('exit_code') != 0:
            return {'status':'blocked' if result.get('exit_code') is None else 'fail',
                    'project':project,'checks':results,'reason':'Project script failed: '+name}
        if name.startswith('test') and re.search(r'no tests (?:found|to run)|0 tests passed',result.get('stdout','')+'\n'+result.get('stderr',''),re.I):
            return {'status':'blocked','project':project,'checks':results,'reason':'No actual unit tests executed'}
    return {'status':'pass','project':project,'checks':results,'note':'Executed current project scripts; not browser acceptance'}


def _backend_for(root, folder):
    # Explicit association for separate frontend and backend repositories.
    descriptor = folder/'gaston-web-e2e.json'
    if descriptor.is_file():
        if descriptor.is_symlink():
            raise ValueError('Linked web E2E descriptor')
        cfg = json.loads(descriptor.read_text(encoding='utf-8'))
        rel = cfg.get('backend_project')
        if not isinstance(rel,str) or Path(rel).is_absolute() or '..' in Path(rel).parts:
            raise ValueError('backend_project must be workspace-relative')
        candidate = (root/rel).resolve()
        if not candidate.is_relative_to(root) or not (candidate/'gaston-e2e.json').is_file():
            raise ValueError('Select a backend with an existing isolated gaston-e2e.json launcher')
        return candidate.relative_to(root).as_posix()
    # Legacy embedded suites belong to their enclosing backend only. Never
    # guess that an unrelated backend is the target of a sibling frontend.
    for parent in (folder, *folder.parents):
        if not parent.is_relative_to(root):
            break
        if (parent/'pom.xml').is_file() and (parent/'gaston-e2e.json').is_file():
            return parent.relative_to(root).as_posix()
    return None


def run_browser(root, project, *, execute, external_url=None):
    root = Path(root).resolve()
    try:
        folder = _project(root, project)
        configs = [folder/name for name in workspace_checks.CONFIGS if (folder/name).is_file()]
        runner = folder/'node_modules/@playwright/test/cli.js'
        node = shutil.which('node')
        if len(configs) != 1 or not runner.is_file() or not node or not workspace_checks._test_files(folder):
            raise ValueError('Require one Playwright config, installed local runner and real browser specs')
        config = configs[0]
        source = config.read_text(encoding='utf-8')
        backend = _backend_for(root, folder)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {'status':'blocked','project':project,'reason':str(exc)}
    def run(url=None):
        env = _environment()
        if url:
            env.update(PLAYWRIGHT_BASE_URL=url, AGENTIC_TEST_API_BASE_URL=url)
        result = execute([node,str(runner),'test','--config',str(config)],cwd=folder,env=env,timeout=600)
        return {'status':'pass' if result.get('exit_code')==0 else 'blocked' if result.get('exit_code') is None else 'fail',
                'project':project,'evidence':result}
    # Prefer Playwright's own lifecycle for a self-contained test config.
    # Config and tests are trusted project code, not arbitrary remote URLs.
    if re.search(r'\bwebServer\s*:', source) and backend is None:
        result = run()
        return {**result,'lifecycle':'playwright_webServer'}
    if backend is not None:
        import managed_backend
        result = managed_backend.run(root,backend,browser_check=run)
        return {**result,'project':project,'backend_project':backend,'lifecycle':'managed_backend'}
    if external_url:
        try:
            parsed=urlparse(external_url)
            if parsed.scheme not in ('http','https') or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password:
                raise ValueError('Only a loopback external test app is supported')
        except ValueError as exc:
            return {'status':'blocked','project':project,'reason':str(exc)}
        return {**run(external_url),'lifecycle':'external_loopback'}
    return {'status':'blocked','project':project,
            'reason':'No isolated startup contract: configure Playwright webServer or gaston-web-e2e.json with backend_project. Do not restart Lead or export variables to repair this.'}
