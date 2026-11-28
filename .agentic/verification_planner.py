"""Targeted verification, including cross-component consequences.

Selects real configured gates; it never infers that a test *passed*. The Lead may
request additional areas; path signals add conservative coverage after a build.
"""
from __future__ import annotations

from pathlib import PurePosixPath

AREA_GATES = {
    'backend': ('backend_verify',),
    'api': ('api_integration',),
    'database': ('postgres_integration',),
    'security': ('security_checks',),
    'performance': ('performance_benchmark',),
    'mobile': ('flutter_analyze', 'flutter_tests', 'mobile_e2e'),
    'web': ('web_e2e',),
}
REQUIRED_GATES = {key: value[0] for key, value in AREA_GATES.items() if key != 'mobile'}
REQUIRED_GATES['mobile'] = 'flutter_tests'
VALID_AREAS = frozenset(AREA_GATES)


def _documentation_only(path: str) -> bool:
    p=PurePosixPath(path)
    tokens=set(path.replace('-', '_').replace('.', '/').replace('_', '/').split('/'))
    name=p.name.lower()
    if tokens & {'openapi','swagger','asyncapi','graphql','proto'}:
        return False
    if tokens & {'docs','documentation'}:
        return True
    if name in {'readme.md','changelog.md','changes.md','contributing.md','license.md','architecture.md'}:
        return True
    return p.suffix.lower() in {'.md','.rst','.adoc','.puml','.plantuml','.uml','.drawio','.mmd','.pdf'}


def _project_relevant_path(path: str, kind: str) -> bool:
    p=PurePosixPath(path)
    name=p.name.lower()
    suffix=p.suffix.lower()
    if _documentation_only(path):
        return False
    if kind=='backend':
        return suffix in {'.java','.kt','.groovy','.py','.go','.cs','.rb','.php','.rs','.js','.jsx','.ts','.tsx','.sql'} or (
            name in {'pom.xml','build.gradle','build.gradle.kts','gaston-e2e.json'}) or (
            suffix in {'.xml','.yml','.yaml','.properties','.conf','.toml'}
            and bool(set(path.replace('-', '_').replace('.', '/').replace('_', '/').split('/')) & {'backend','server'})
            and 'docs' not in path.lower().split('/'))
    if kind=='flutter':
        return suffix=='.dart' or name in {'pubspec.yaml','analysis_options.yaml'}
    if kind=='web':
        return suffix in {'.ts','.js','.tsx','.jsx','.vue','.svelte','.html','.css','.scss','.sass','.less'} or (
            name in {'package.json','vite.config.ts','vite.config.js','tsconfig.json'})
    return False


def infer_path_areas(paths: list[str]) -> tuple[set[str], set[str]]:
    """Infer affected areas from implementation paths, not documentation directory names."""
    areas, ripple = set(), set()
    for raw in paths:
        path = raw.replace('\\', '/').lower().strip('/')
        p=PurePosixPath(path)
        name = p.name
        suffix = p.suffix
        tokens = set(path.replace('-', '_').replace('.', '/').replace('_', '/').split('/'))
        documentation_only=_documentation_only(path)
        backend_context = bool(tokens & {'backend', 'server'})
        backend_source = suffix in {'.java','.kt','.groovy'} or (
            backend_context and suffix in {'.py','.go','.cs','.rb','.php','.rs','.js','.jsx','.ts','.tsx'})
        backend_config = backend_context and suffix in {'.xml','.yml','.yaml','.properties','.conf','.toml'} and not (tokens & {'docs','documentation'})
        if not documentation_only and (backend_source or backend_config):
            areas.add('backend')
        if (not documentation_only and (path.endswith(('.sql', '.hbm.xml')) or tokens & {'migration', 'migrations', 'flyway', 'liquibase', 'db'}
                or name.endswith(('repository.java', 'repository.kt', 'entity.java', 'entity.kt', 'dao.java')))):
            areas.update(('database', 'backend'))
            ripple.add('api_integration')
        # "controller" is common in Flutter widget/controller filenames.
        # Only server-side source code creates API coverage.
        controller_marker = bool(tokens & {'controller', 'controllers'})
        server_context = bool(tokens & {'backend', 'server', 'api'})
        server_language = suffix in {'.java', '.kt', '.py', '.go', '.cs', '.rb', '.php', '.rs'}
        js_ts_server_controller = server_context and suffix in {'.js', '.jsx', '.ts', '.tsx'}
        server_controller_name = name.endswith(('controller.java', 'controller.kt'))
        is_backend_controller = (
            server_controller_name
            or (controller_marker and (server_language or js_ts_server_controller))
        )
        if (not documentation_only and (tokens & {'endpoint', 'endpoints', 'openapi', 'swagger', 'asyncapi', 'graphql', 'proto', 'route', 'routes'}
                or is_backend_controller
                or any(name.endswith(suffix) for suffix in ('endpoint.java','endpoint.kt','openapi.yaml','openapi.json')))):
            areas.add('api')
            if suffix in ('.java', '.kt'): areas.add('backend')
            ripple.update(('flutter_tests', 'web_e2e'))
        if (not documentation_only and ((tokens & {'dto', 'contract', 'contracts', 'schema', 'schemas'} and ('api' in tokens or path.endswith(('.proto', '.graphql'))))
                or name.endswith(('dto.java','dto.kt','.proto','.graphql')))):
            areas.add('api')
            ripple.update(('flutter_tests', 'web_e2e'))
        if not documentation_only and (tokens & {'auth', 'authentication', 'authorization', 'security', 'jwt', 'oauth', 'permission', 'permissions'}
                or any(mark in name for mark in ('securityconfig', 'authorization', 'authentication', 'jwt', 'oauth', 'permission'))):
            areas.add('security')
            if suffix in ('.java', '.kt'): areas.add('backend')
            ripple.update(('api_integration', 'flutter_tests', 'web_e2e'))
        if not documentation_only and (suffix=='.dart' or 'flutter' in tokens or ('mobile' in tokens and suffix in ('.yaml', '.yml'))):
            areas.add('mobile')
            if name.endswith(('apiclient.dart','api_client.dart','remote_datasource.dart','apirepository.dart')) or 'remote' in tokens:
                ripple.add('api_integration')
        fixture_html=(path.endswith('.html') and
                      (any(part in tokens for part in ('test','tests','fixture','fixtures','resources','testdata'))
                       or '/src/test/' in path or '/src/testresources/' in path))
        if not documentation_only and not fixture_html and (suffix in ('.tsx', '.jsx', '.vue', '.svelte', '.html', '.css', '.scss', '.sass', '.less') or path.endswith(('.spec.ts', '.spec.js'))):
            areas.add('web')
        if not documentation_only and tokens & {'performance', 'benchmark', 'benchmarks', 'loadtest'}:
            areas.add('performance')
    return areas, ripple

def select_checks(task: dict, steps: list[dict], changed_paths: list[str],
                  project_roots: dict | None = None) -> dict:
    """Return selected checks and actionable coverage gaps, never green guesses."""
    explicit = set(task.get('scope', [])) | set(task.get('verification_areas', []))
    bad = explicit - VALID_AREAS
    if bad:
        raise ValueError('Unknown verification areas: ' + ', '.join(sorted(bad)))
    inferred, ripple = infer_path_areas(changed_paths)
    # Project-aware detection handles ordinary .ts/.js changes and new nested
    # repositories without relying on filename guesses or a one-time init.
    for raw in changed_paths:
        path = raw.replace('\\', '/').strip('/')
        for kind, area in (('java','backend'), ('flutter','mobile'), ('web','web')):
            if not _project_relevant_path(path,kind):
                continue
            if any(path == repo or path.startswith(repo.rstrip('/') + '/')
                   for repo in (project_roots or {}).get(kind, []) if repo != '.'):
                inferred.add(area)
            if '.' in (project_roots or {}).get(kind, []) and kind == 'web':
                inferred.add(area)
    areas = explicit | inferred
    # Payment semantics demand DB/API/security behavior even if the edit was
    # confined to one service. Scope is still driven by a specific task goal.
    goal = str(task.get('goal', '')).lower()
    money_terms = ('payment', 'payout', 'transfer', 'ledger', 'refund', 'balance', 'settlement')
    if any(term in goal for term in money_terms) and ('backend' in areas or 'database' in areas or 'api' in areas):
        areas.update(('backend', 'api', 'database', 'security'))
    by_name = {s['name']: s for s in steps}
    # If there is no reliable path or task scope, fall back to safe baseline
    # checks, rather than silently skipping everything or running all E2E.
    unknown = not areas
    wanted: set[str] = set(task.get('required_checks', []))
    if unknown:
        wanted.update(name for name in ('backend_verify', 'flutter_analyze', 'flutter_tests') if name in by_name)
    else:
        for area in areas:
            wanted.update(AREA_GATES[area])
        wanted.update(ripple & by_name.keys())
        # Changes in mobile UI need static/widget tests by default. Device E2E
        # is included only when explicitly required or configured and the Lead
        # selects native E2E through the task.
        if 'mobile' in areas and 'mobile_e2e' not in task.get('required_checks', []) and not task.get('require_device_e2e'):
            wanted.discard('mobile_e2e')
        # Web is an explicit E2E area when selected; ripple is optional and
        # only selected when real tests already exist.
    required = set(task.get('required_checks', []))
    for area in areas:
        required.add(REQUIRED_GATES[area])
    if not unknown and areas & {'backend', 'api', 'database', 'security'} and 'backend_verify' in by_name:
        wanted.add('backend_verify')
    # User-defined gates retain backwards compatibility unless associated
    # with explicit areas. An optional area tag supports project-specific gates.
    known_names = {n for names in AREA_GATES.values() for n in names}
    for step in steps:
        custom_areas = set(step.get('areas', []))
        if custom_areas:
            if custom_areas & areas: wanted.add(step['name'])
        elif step['name'] not in known_names or step.get('always') is True:
            wanted.add(step['name'])
    missing = sorted(required - by_name.keys())
    chosen = [step for step in steps if step['name'] in wanted]
    return {'areas': sorted(areas), 'explicit_areas': sorted(explicit),
            'inferred_areas': sorted(inferred), 'cross_component_checks': sorted(ripple & by_name.keys()),
            'changed_paths': changed_paths, 'selected': [x['name'] for x in chosen],
            'missing': missing, 'checks': chosen,
            'reason': ('No reliable impact scope: conservative baseline checks' if unknown
                       else 'Task scope + changed code + related interfaces, persistence and authorization')}
