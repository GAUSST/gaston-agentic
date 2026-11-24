"""Optional, explicitly approved Playwright MCP + Playwright Test bootstrap.

Playwright MCP enables interactive AI browser inspection. Playwright Test runs
repeatable tests. Neither establishes that a project has a test suite. All
installations require one category-level approval; previously ready toolsets are skipped.
"""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode

import json
from pathlib import Path
import re
import shutil
import subprocess
from setup_i18n import CURRENT_LANGUAGE


def speak(english: str, french: str) -> str:
    return french if CURRENT_LANGUAGE.get() == 'fr' else english


HARNESS = Path(project_state_name()) / 'local/playwright-tests'


def node_major():
    if not shutil.which('node'):
        return None
    try:
        p=subprocess.run(['node','--version'], capture_output=True, text=True,
                         timeout=10, check=False)
        match=re.match(r'v?(\d+)',p.stdout.strip())
        return int(match.group(1)) if p.returncode==0 and match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def mcp_registered(provider: str, *, root: Path) -> bool:
    if provider not in ('codex','claude') or not shutil.which(provider):
        return False
    try:
        result=subprocess.run([provider,'mcp','get','playwright'], cwd=root,
                              capture_output=True,text=True,timeout=20,check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def test_workspace(root: Path) -> Path:
    """Reuse a configured existing web test workspace before creating a new one.

    Search bounded, conventional locations only: never scan application node_modules.
    An application setup must have both Playwright Test installed and an actual
    Playwright configuration to be treated as ready.
    """
    root=Path(root).resolve()
    local=root/HARNESS
    if (local/'node_modules/@playwright/test/package.json').is_file():
        return local
    for relative in ('.','frontend','web','client','app','apps/web','apps/frontend'):
        folder=(root/relative).resolve()
        if not folder.is_relative_to(root):
            continue
        if not (folder/'node_modules/@playwright/test/package.json').is_file():
            continue
        if any((folder/('playwright.config.'+ext)).is_file()
               for ext in ('js','cjs','mjs','ts','mts','cts')):
            return folder
    return local


def package_installed(root: Path) -> bool:
    folder=test_workspace(root)
    return (folder/'node_modules/@playwright/test/package.json').is_file()


def test_files(root: Path) -> list[Path]:
    folder=test_workspace(root)
    if not folder.is_dir():
        return []
    locations=[folder/'tests'] if folder == Path(root).resolve()/HARNESS else [folder/'tests',folder/'e2e',folder/'playwright']
    results=[]
    for location in locations:
        if location.is_dir():
            results.extend(f for f in location.rglob('*') if f.is_file() and
                           re.search(r'\.(spec|test)\.(js|mjs|cjs|ts|mts|cts)$',f.name))
    return sorted(set(results))


def browser_gate(root: Path) -> dict | None:
    """Only genuine test files become a gate, using the owning workspace."""
    files=test_files(root)
    if not files:
        return None
    folder=test_workspace(root)
    return {'name':'web_e2e', 'cwd':str(folder.relative_to(Path(root).resolve())),
            'command':['npm','exec','--','playwright','test'],
            'timeout_seconds':1200}


def chromium_installed(root: Path) -> bool:
    """Check the executable selected by the existing/new test workspace."""
    folder=test_workspace(root)
    if not package_installed(root) or not shutil.which('node'):
        return False
    try:
        result = subprocess.run(
            ['node', '-e', "console.log(require('@playwright/test').chromium.executablePath())"],
            cwd=folder, capture_output=True, text=True, timeout=12, check=False)
        if result.returncode:
            return False
        return Path(result.stdout.strip()).is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False


def environment_ready(root: Path, active_agents=()) -> bool:
    """Only skip when Test, Chromium, and Lead MCP are all ready."""
    major = node_major()
    return (major is not None and major >= 20 and
            bool(shutil.which('npm')) and bool(shutil.which('npx')) and
            package_installed(root) and chromium_installed(root) and
            all(mcp_registered(a, root=Path(root).resolve()) for a in active_agents))


def describe(root: Path, active_agents: tuple[str,...]):
    node=node_major()
    print(speak('  Node.js:', '  Node.js :'), f'v{node}' if node else speak('missing', 'absent'),
          speak('(20+ needed for Playwright MCP)', '(20+ requis pour Playwright MCP)'))
    print('  npm:', speak('available','disponible') if shutil.which('npm') else speak('missing','absent'))
    print('  Playwright Test:', speak('installed','installé') if package_installed(root) else speak('not installed','non installé'))
    print(speak('  Executable Playwright tests:', '  Tests Playwright exécutables :'), len(test_files(root)))
    for agent in active_agents:
        print(f'  {agent} Playwright MCP:', speak('registered','enregistré') if mcp_registered(agent,root=root) else speak('not registered','non enregistré'))


def configure(root: Path, *, active_agents=(), interactive=True,
              allow_test_setup=True, allow_browser_download=True, approved=False):
    """Perform a single category-approved setup. No nested yes/no prompts."""
    from setup_wizard import visible_run
    root=Path(root).resolve()
    active_agents=tuple(a for a in active_agents if a in ('codex','claude'))
    from terminal_ui import section, result as display_result
    section(speak('Playwright web testing', 'Tests web Playwright'))
    describe(root, active_agents)
    if not interactive or not approved or environment_ready(root, active_agents):
        return
    version=node_major()
    if version is None or version < 20 or not shutil.which('npx') or not shutil.which('npm'):
        print(speak('  Node.js 20+, npm and npx must be installed before web E2E setup can finish.',
                    '  Node.js 20+, npm et npx sont nécessaires pour terminer la configuration web E2E.'))
        return
    for agent in active_agents:
        if agent != 'claude':
            print(speak('  Codex user-scoped MCP registration skipped to protect Builder isolation.',
                        '  MCP global Codex ignoré pour préserver l’isolation du développeur IA.'))
            continue
        if not shutil.which(agent) or mcp_registered(agent, root=root):
            continue
        cmd=['claude','mcp','add','--scope','local','playwright','--','npx','@playwright/mcp@latest']
        result=visible_run(cmd,label='Register Claude Playwright MCP',timeout=60,cwd=root)
        if result.returncode or not mcp_registered(agent,root=root):
            print(speak('  Claude Playwright MCP registration could not be verified.',
                        '  Enregistrement du MCP Playwright Claude non vérifié.'))
    if allow_test_setup and not package_installed(root):
        folder=root/HARNESS
        folder.mkdir(parents=True,exist_ok=True)
        package=folder/'package.json'
        if not package.exists():
            package.write_text(json.dumps({'name':'gaston-agentic-browser-tests','private':True,
                                           'type':'module','scripts':{'test':'playwright test'}},indent=2)+'\n')
        (folder/'tests').mkdir(exist_ok=True)
        config=folder/'playwright.config.mjs'
        if not config.exists():
            config.write_text("import { defineConfig } from '@playwright/test';\n"
                              "export default defineConfig({\n"
                              "  testDir: './tests',\n"
                              "  reporter: [['list']],\n"
                              "  use: { baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3000', trace: 'retain-on-failure' },\n"
                              "});\n",encoding='utf-8')
        result=visible_run(['npm','install','--save-dev','--no-audit','--no-fund','@playwright/test'],
                           label='Install Playwright Test',timeout=600,cwd=folder)
        if result.returncode or not package_installed(root):
            print(speak('  Playwright Test is not installed; rerun ./dev init after fixing the prerequisites.',
                        '  Playwright Test est absent ; relancez ./dev init après correction des prérequis.'))
            return
    if allow_browser_download and package_installed(root) and not chromium_installed(root):
        folder=test_workspace(root)
        result=visible_run(['npm','exec','--','playwright','install','chromium'],
                           label='Install Chromium for Playwright',timeout=900,cwd=folder)
        if result.returncode or not chromium_installed(root):
            print(speak('  Chromium installation could not be verified; browser tests are not ready.',
                        '  Installation de Chromium non vérifiée ; tests navigateur non prêts.'))
    display_result(speak('Web E2E tools configured', 'Outils web E2E configurés') if environment_ready(root, active_agents)
           else speak('Web E2E setup incomplete', 'Configuration web E2E incomplète'),
           'OK' if environment_ready(root, active_agents) else 'WARNING', indent=4)


def run_tests(root: Path) -> int:
    root=Path(root).resolve()
    if not package_installed(root):
        print('Playwright Test is unavailable. Ask the Engineering Lead to configure missing test prerequisites, or rerun ./dev init.')
        return 2
    specs=test_files(root)
    if not specs:
        print('No Playwright test specs found. Add real tests under .agentic/local/playwright-tests/tests/.')
        return 2
    try:
        p=subprocess.run(['npm','exec','--','playwright','test'],cwd=test_workspace(root),
                         timeout=1200,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc:
        print('Playwright Test could not complete:',exc)
        return 2
    return p.returncode
