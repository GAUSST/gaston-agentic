"""Risk-based post-edit plan with enforceable, project-specific acceptance gates."""
from __future__ import annotations
from project_layout import project_state, project_state_name, project_root
from pathlib import Path
import verification_planner
import workspace_checks
from documentation_review import required_paths


def produce(root: Path, task: dict, build: dict, cfg: dict) -> dict:
    root=Path(root).resolve()
    refreshed,_=workspace_checks.refresh(root,cfg)
    changes=build.get('changed_files',[])
    if not isinstance(changes,list):changes=[]
    paths=list(dict.fromkeys(value.replace('\\','/') for value in changes
              if isinstance(value,str) and not Path(value).is_absolute() and '..' not in Path(value.replace('\\','/')).parts))
    projects=refreshed.get('project',{}).get('discovered_roots',{})
    selected=verification_planner.select_checks(task,refreshed.get('checks',[]),paths,projects)
    explicit=set(task.get('scope',[]))|set(task.get('verification_areas',[]))
    explicit_checks=set(task.get('required_checks',[]))
    checks=[];missing=set();advisory=set();managed=[];full_backend=[];areas=set(selected['areas'])
    def add(name,cwd,command=None):
        if not any(c['name']==name for c in checks):checks.append({'name':name,'cwd':cwd,'command':command or []})
    def in_project(path,rel):return rel=='.' or path==rel or path.startswith(rel.rstrip('/')+'/')
    def local_path(path,rel):return path if rel=='.' else path[len(rel.rstrip('/'))+1:]
    # Every affected Java project has its own backend gate. Changes to main
    # application behavior, migrations, build/runtime config and auth require
    # application HTTP checks, not a MockMvc or a generic smoke fixture.
    java=projects.get('java',[])
    for i,rel in enumerate(java):
        local=[local_path(p,rel) for p in paths if in_project(p,rel)]
        relevant=bool(local) or (len(java)==1 and bool(explicit&{'backend','api','database','security'}))
        if not relevant:continue
        suffix='' if i==0 else f'__{i}'
        name='backend_verify'+suffix
        add(name,rel,next((s['command'] for s in refreshed.get('checks',[]) if s.get('name')==name),[]))
        main=[p.lower() for p in local if p.lower().startswith('src/main/')]
        pure=bool(main) and all(p.endswith( ('util.java','utils.java','helper.java')) or '/util/' in p or '/utils/' in p for p in main)
        inferred,_=verification_planner.infer_path_areas([p for p in local if not p.startswith('src/test/')])
        api=bool(explicit&{'api','database','security'} or inferred&{'api','database','security'} or
                 ('api_integration'+suffix) in explicit_checks or
                 (main and not pure) or any(p in ('pom.xml','build.gradle','build.gradle.kts','gaston-e2e.json') for p in local))
        if 'checkpoint' in str(task.get('goal','')).lower() or inferred&{'database','security'} or len(main)>10:
            full_backend.append(name)
        if api:
            areas.add('api');gate='api_integration'+suffix;managed.append(gate)
            add(gate,rel,['managed_backend','curl'])
            try:
                import managed_backend
                managed_backend.descriptor(root/rel)
            except (OSError,ValueError,AttributeError,TypeError):missing.add(gate)
    slice_only=bool(task.get('work_plan_id') and not task.get('checkpoint_acceptance'))
    web_roots=projects.get('web',[])
    all_roots=set(rel for group in projects.values() for rel in group)
    def owns(path,rel):
        return in_project(path,rel) and not any(other!=rel and in_project(other,rel) and in_project(path,other) for other in all_roots)
    affected_web={rel for rel in web_roots if any(owns(p,rel) for p in paths)}
    affected_java=any(any(owns(p,rel) for p in paths) for rel in java)
    direct_web=affected_web or (set(web_roots) if 'web' in explicit and not paths else set())
    browser_required=set(explicit_checks)
    # Real client changes demand actual client checks. Backend changes alone
    # do not demand nonexistent mobile/web projects. Existing connected tests
    # are included when they are selected by the risk planner.
    for step in selected['checks']:
        name=step['name'];cwd=step.get('cwd','.')
        if name.startswith('backend_verify'):continue
        if name.startswith('api_integration') and managed:continue
        if name.startswith('web_e2e') and name not in browser_required:
            if slice_only or (direct_web and cwd not in direct_web and not affected_java):continue
        if name.startswith(('web_e2e','mobile_e2e','flutter_tests','flutter_analyze')):
            if (cwd.startswith('.agentic/') or cwd.startswith('.gaston/')):continue # a harness smoke is not an app
            add(name,cwd,step.get('command'))
        elif name in explicit_checks:
            add(name,cwd,step.get('command'))
    # Slice verification uses actual project-local scripts; integration still
    # requires browser coverage for affected web apps (not unrelated suites).
    for i,rel in enumerate(web_roots):
        if rel not in direct_web:continue
        if slice_only:
            add('web_verify'+('' if i==0 else f'__{i}'),rel,['npm','run','project-local-checks'])
        elif not any(c['name'].startswith('web_e2e') and c['cwd']==rel for c in checks):
            missing.add('web_e2e:'+rel)
    # Require real client gates even if the project has not authored tests yet.
    for kind,gate in (('flutter','flutter_tests'),):
        for i,rel in enumerate(projects.get(kind,[])):
            if not any(in_project(p,rel) for p in paths) and not (len(projects[kind])==1 and (kind=='web' and 'web' in explicit or kind=='flutter' and 'mobile' in explicit)):continue
            name=gate+('' if i==0 else f'__{i}')
            if not any(c['name']==name for c in checks):missing.add(name)
    names={c['name'] for c in checks}
    for gate in explicit_checks:
        if gate not in names:missing.add(gate)
    # Separate unconfigured specialist coverage from the enforceable gates.
    # DB/security requirements are exercised by actual task-specific JUnit and
    # live HTTP scenarios; do not claim a separate audit/benchmark ran.
    for gate in selected['missing']:
        if gate in ('postgres_integration','security_checks','performance_benchmark') and gate not in explicit_checks:
            advisory.add(gate)
        elif gate=='api_integration' and managed:continue
        elif gate=='web_e2e' and direct_web and slice_only and gate not in explicit_checks:continue
        elif gate=='web_e2e' and direct_web and gate not in explicit_checks:continue # exact project coverage checked above
        elif gate in ('backend_verify','web_e2e','flutter_tests') and any(c['name'].startswith(gate) for c in checks):continue
        else:missing.add(gate)
    return {'status':'coverage_gap' if missing else 'ready','policy_version':35,
            'changed_paths':paths,'required_documentation_review':required_paths(paths),'areas':sorted(areas),'selected_checks':checks,
            'missing_gates':sorted(missing),'managed_api_gates':managed,'full_backend_gates':full_backend,'advisory_gaps':sorted(advisory),
            'cross_component_checks':selected['cross_component_checks'],
            'notes':['Proposed verification only. No check executed or passed.',
                     'Every selected gate must pass on this Builder revision. Lead writes and reviews isolated test launcher and HTTP scenarios; delegate only complex implementation-dependent fixtures.',
                     'Review scenario adequacy, including negative paths. Specialist audits/performance checks are separate, never implicitly passed.',
                     'When mobile_e2e is selected, run the APK build/device check before Flutter analyze and tests for that project. Build-generated input changes can expire earlier evidence; inspect expired_checks and rerun only missing or expired gates. Never reuse a pass after material input changes.'],
            'project_roots':projects,'task_goal':str(task.get('goal',''))[:280]}
