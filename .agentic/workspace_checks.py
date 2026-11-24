"""Discover project verification checks at setup AND at agent execution time.

Discovery is read-only. It never installs packages or assumes that a command
returning zero proves a business invariant. Customized checks are preserved.
"""
from __future__ import annotations
from project_layout import project_state, project_state_name, project_root

import json
import os
from pathlib import Path
import re

from setup_wizard import discover_stacks, SKIP_SCAN_DIRS

MANAGED = 'workspace-discovery'
GENERATED = {'backend_verify', 'flutter_analyze', 'flutter_tests', 'mobile_e2e', 'web_e2e'}
CONFIGS = ('playwright.config.ts', 'playwright.config.js', 'playwright.config.mjs',
           'playwright.config.cjs', 'playwright.config.mts', 'playwright.config.cts')
TEST_PATTERN = re.compile(r'\.(?:spec|test)\.(?:ts|tsx|js|jsx|mts|cts|mjs|cjs)$')


def web_projects(root: Path, *, max_depth=7, max_dirs=10000) -> list[str]:
    """Find web projects with package.json, without traversing dependencies."""
    root = Path(root).resolve()
    found: set[str] = set()
    count = 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        count += 1
        if count > max_dirs:
            break
        dirs[:] = sorted(d for d in dirs if d not in SKIP_SCAN_DIRS and
                         not (here/d).is_symlink()) if depth < max_depth else []
        if 'package.json' not in files:
            continue
        # package.json by itself is not proof of a web UI or Playwright tests.
        try:
            metadata = json.loads((here/'package.json').read_text(encoding='utf-8')[:100000])
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        deps = {**(metadata.get('dependencies') or {}), **(metadata.get('devDependencies') or {})}
        if (any((here/config).is_file() for config in CONFIGS) or
                set(deps) & {'@playwright/test','playwright','next','vite','react',
                             'react-dom','@angular/core','vue','svelte','@sveltejs/kit'} or
                'test:e2e' in (metadata.get('scripts') or {})):
            found.add(here.relative_to(root).as_posix())
    return sorted(found, key=lambda s:(len(Path(s).parts),s))


def _test_files(here: Path) -> bool:
    """Require a real test filename, not just a blank config or directory."""
    for name in ('tests','e2e','playwright'):
        folder=here/name
        if folder.is_dir():
            for current, dirs, files in os.walk(folder,followlinks=False):
                dirs[:] = [d for d in dirs if d not in SKIP_SCAN_DIRS and
                            not (Path(current)/d).is_symlink()]
                if any(TEST_PATTERN.search(f) for f in files):
                    return True
    return False


def _legacy_generated(check: dict) -> bool:
    """Replace only known installer-generated entries; preserve custom gates."""
    name=check.get('name')
    command=check.get('command')
    if check.get('managed_by') == MANAGED:
        return True
    if name == 'backend_verify' and isinstance(command,list):
        return command[:2] in (["mvn","-B"],["./mvnw","-B"]) or command[:1] in (["gradle"],["./gradlew"])
    return ((name in ('flutter_analyze','flutter_tests','mobile_e2e') and
             command in (['flutter','analyze'],['flutter','test'],
                         ['maestro','test','.maestro'],['patrol','test'],
                         ['flutter','test','integration_test'])) or
            (name == 'web_e2e' and command == ['npm','exec','--','playwright','test']))


def _gate(name: str, cwd: str, command: list[str], timeout: int, *, area: str):
    return {'name':name,'cwd':cwd,'command':command,'timeout_seconds':timeout,
            'managed_by':MANAGED,'areas':[area]}


def _unique_name(base: str, index: int, cwd: str) -> str:
    return base if index == 0 else f'{base}__{index}'


def discover(root: Path, cfg: dict) -> tuple[list[dict], dict]:
    """Generate deterministic gates for every discovered application repository."""
    root=Path(root).resolve()
    stacks=discover_stacks(root)
    web=web_projects(root)
    e2e=cfg.get('preferences',{}).get('mobile_e2e','maestro')
    generated=[]
    for idx, rel in enumerate(stacks['java']):
        here=root/rel
        if (here/'mvnw').is_file(): command=['./mvnw','-B','verify']
        elif (here/'pom.xml').is_file(): command=['mvn','-B','verify']
        elif (here/'gradlew').is_file(): command=['./gradlew','check']
        elif (here/'build.gradle').is_file() or (here/'build.gradle.kts').is_file(): command=['gradle','check']
        else: continue
        generated.append(_gate(_unique_name('backend_verify',idx,rel),rel,command,1200,area='backend'))
    for idx,rel in enumerate(stacks['flutter']):
        generated.extend((
            _gate(_unique_name('flutter_analyze',idx,rel),rel,['flutter','analyze'],600,area='mobile'),
            _gate(_unique_name('flutter_tests',idx,rel),rel,['flutter','test'],1200,area='mobile'),
        ))
        if e2e in ('maestro','patrol','integration_test'):
            commands={'maestro':['maestro','test','.maestro'],
                      'patrol':['patrol','test'],
                      'integration_test':['flutter','test','integration_test']}
            generated.append(_gate(_unique_name('mobile_e2e',idx,rel),rel,commands[e2e],1800,area='mobile'))
    for rel in web:
        here=root/rel
        # Don't register a browser gate if no tests exist; the Lead must flag
        # coverage as missing, and the Lead should author meaningful tests.
        if not any((here/name).is_file() for name in CONFIGS) or not _test_files(here):
            continue
        # --offline blocks npm from downloading or installing at verification time.
        generated.append(_gate('web_e2e' if not any(g['name']=='web_e2e' for g in generated)
                               else f'web_e2e__{len([g for g in generated if g["name"].startswith("web_e2e")])}',
                               rel,['npm','exec','--offline','--','playwright','test'],1200,area='web'))
    # Keep the existing sandbox harness, but don't let it replace real app tests.
    harness=project_state(root)/'local/playwright-tests'
    if not web and not any(g['name']=='web_e2e' for g in generated) and _test_files(harness):
        generated.append(_gate('web_e2e', project_state_name() + '/local/playwright-tests',
                              ['npm','exec','--offline','--','playwright','test'],1200,area='web'))
    project={'java':stacks['java'], 'flutter':stacks['flutter'], 'web':web}
    return generated, project


def refresh(root: Path, cfg: dict) -> tuple[dict, bool]:
    """Refresh managed gates and project-root metadata, preserving every user gate."""
    generated, discovered=discover(root,cfg)
    updated=dict(cfg)
    old=cfg.get('checks',[])
    # User-created checks take precedence over identically named generated gates.
    custom=[c for c in old if not _legacy_generated(c)]
    names={c.get('name') for c in custom}
    updated['checks']=custom + [g for g in generated if g['name'] not in names]
    project=dict(cfg.get('project',{}))
    project['discovered_roots']=discovered
    updated['project']=project
    return updated, updated.get('project')!=cfg.get('project') or updated['checks']!=old


def runner_preflight(root: Path, check: dict) -> tuple[bool, str]:
    """Check that discovered web gates have a project-local test runner.

    CLI lookup alone is insufficient: npm can be installed even when the
    application's Playwright dependency is absent. Never install at test time.
    """
    if check.get('managed_by') != MANAGED or not check.get('name','').startswith('web_e2e'):
        return True, ''
    root=Path(root).resolve()
    here=(root/check.get('cwd','.')).resolve()
    if not here.is_relative_to(root):
        return False, 'Playwright test directory escapes workspace'
    current=here
    while current.is_relative_to(root):
        modules=current/'node_modules'
        if ((modules/'@playwright/test/package.json').is_file() or
                (modules/'.bin/playwright').is_file()):
            return True, ''
        if current == root: break
        current=current.parent
    return False, ('Playwright Test dependency not installed for this project; '
                   'Builder must set up executable project tests')
