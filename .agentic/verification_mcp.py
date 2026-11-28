#!/usr/bin/env python3
"""Lead-only workspace, Git, delegation and independent verification tools.

No unrestricted shell, arbitrary commands or production access. Lead may make scoped local source/test edits.
Temporary browser smoke fixtures are created under .agentic/local only.
MCP JSON-RPC stdio; stdout is *exclusively* protocol messages.
"""
from __future__ import annotations
from project_layout import project_root, project_state, project_state_name, runtime_script, runtime_manifest, central_mode
import json
import atexit
import signal
import hashlib
from contextlib import redirect_stdout
from contextvars import ContextVar
import secrets
import uuid
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread, Lock, Event
from task_runtime import BUILDER_WAIT_SECONDS
import documentation_review
import code_quality
import orchestration as orch

ROOT = project_root()
STATE = project_state(ROOT)
LOCAL = STATE / 'local'
# Snapshot identity at process startup, not from old task records or an overlay
# made while this server is still running. Every tool reports the same release.
_MANIFEST_BYTES = (Path(__file__).resolve().parent / 'manifest.json').read_bytes()
_RELEASE_MANIFEST = json.loads(_MANIFEST_BYTES)
TOOLKIT_RELEASE = _RELEASE_MANIFEST['release']
TOOLKIT_BUILD = _RELEASE_MANIFEST.get('build', TOOLKIT_RELEASE.removeprefix('v') + '.0.0')
MANIFEST_SHA256 = hashlib.sha256(_MANIFEST_BYTES).hexdigest()
SERVER_PATH = str(Path(__file__).resolve())

CENTRAL_DISABLED_TOOLS = frozenset({'stage_evolution','validate_evolution','evolution_status','queue_evolution','cancel_evolution','export_toolkit'})
CENTRAL_EVOLUTION_REASON = 'Central engine is immutable; use verified signed releases and `dev update`.'


TOOLS = [
    {'name':'workspace_overview','description':'Bounded workspace directory overview or filename search. Claude native Read/Glob/Grep remain available; never accesses private Git internals or secrets.',
     'inputSchema':{'type':'object','properties':{'action':{'type':'string','enum':['overview','find']},'relative_path':{'type':'string'},'pattern':{'type':'string'},'max_results':{'type':'integer','minimum':1,'maximum':150}},'additionalProperties':False}},
    {'name':'workspace_diagnostics','description':'Check installed developer toolchain versions, local dependency readiness, explicitly named loopback ports, or stale temporary-file candidates. No install, downloads, or arbitrary commands.',
     'inputSchema':{'type':'object','properties':{'action':{'type':'string','enum':['toolchain','dependencies','ports','maintenance']},'project':{'type':'string'},'ports':{'type':'array','items':{'type':'integer'},'maxItems':20}},'additionalProperties':False}},
    {'name':'workspace_logs','description':'Read bounded tail or matching lines of an existing workspace log/report with conservative credential redaction. No general file editing.',
     'inputSchema':{'type':'object','properties':{'relative_path':{'type':'string'},'pattern':{'type':'string'},'lines':{'type':'integer','minimum':1,'maximum':300}},'required':['relative_path'],'additionalProperties':False}},
    {'name':'workspace_processes','description':'Read-only list of current-user processes whose working directory is inside workspace, and whether Gaston owns a static preview. No process termination.',
     'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
    {'name':'workspace_preview','description':'Start, inspect, or stop only a Gaston-owned static localhost preview of an existing index.html. Does not launch application commands or touch unrelated processes.',
     'inputSchema':{'type':'object','properties':{'action':{'type':'string','enum':['start','status','stop']},'relative_path':{'type':'string'},'port':{'type':'integer','minimum':0,'maximum':65535}},'additionalProperties':False}},
    {'name':'workspace_housekeeping','description':'Inspect or delete only old, allowlisted Gaston temporary files; never delete source, dependency caches, sessions, commits, or unknown locks. Cleanup requires explicit user request.',
     'inputSchema':{'type':'object','properties':{'action':{'type':'string','enum':['inspect','cleanup']},'apply':{'type':'boolean'},'requested_by_user':{'type':'string'}},'additionalProperties':False}},
    {'name':'workspace_note','description':'Create or append a scoped Lead Markdown operational note in toolkit-local lead-notes or a known task lead-notes folder. Not arbitrary repository documentation or application code.',
     'inputSchema':{'type':'object','properties':{'name':{'type':'string'},'content':{'type':'string'},'task_id':{'type':'string'},'mode':{'type':'string','enum':['append','create']}},'required':['name','content'],'additionalProperties':False}},
    {'name':'workspace_document','description':'Create, append or make one exact replacement in a developer-requested Markdown/text document under a docs/ subtree. Cannot edit application code, repository root instructions or hidden agent configurations.',
     'inputSchema':{'type':'object','properties':{'relative_path':{'type':'string'},'action':{'type':'string','enum':['create','append','replace_exact']},'text':{'type':'string'},'old_text':{'type':'string'},'requested_by_user':{'type':'string'}},'required':['relative_path','action','text','requested_by_user'],'additionalProperties':False}},
    {'name':'workspace_database','description':'Bounded local PostgreSQL diagnostics via psql: status, metadata (tables, columns, migration-table discovery, constraints, indexes, estimated statistics), and developer-enabled synthetic fixture count, duplicates and sample. No arbitrary SQL, remote or production databases, writes, or acceptance-test pass claims. Fixture actions need a separately authorized local read-only role and GASTON_DATABASE_ALLOW_FIXTURE_DATA=1.',
     'inputSchema':{'type':'object','properties':{'action':{'type':'string','enum':['status','connectivity','tables','columns','migrations','constraints','indexes','table_stats','count','duplicates','sample']},'schema':{'type':'string'},'table':{'type':'string'},'columns':{'type':'array','items':{'type':'string'},'maxItems':8,'uniqueItems':True},'limit':{'type':'integer','minimum':1,'maximum':50}},'additionalProperties':False}},
    {'name':'git_inspect','description':'Read-only Git status for every workspace repository (including nested repos and unborn branches), with optional bounded diff and Builder-task reported-vs-observed changes. No Git writes, no Codex delegation.',
     'inputSchema':{'type':'object','properties':{'repository':{'type':'string','description':'Optional workspace-relative repository path returned by this tool'},'diff':{'type':'boolean','description':'Return bounded tracked-file diffs (untracked paths listed separately)'},'task_id':{'type':'string','description':'Optional Builder task for changed-path attribution diagnostics'}},'additionalProperties':False}},
    {'name':'git_commit','description':'Create ONE local commit in a selected discovered repository using only explicitly named changed files. Requires an explicit developer request; never automatic, no push/reset/rebase, no arbitrary shell. Refuses existing staged changes.',
     'inputSchema':{'type':'object','properties':{'repository':{'type':'string','description':'Exact workspace-relative Git repository, e.g. sample-backend'},'paths':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':75,'description':'Exact repository-relative file paths to commit, no directories or globs'},'message':{'type':'string','description':'One-line user-authorized commit message'},'explicit_user_request':{'type':'boolean','description':'True ONLY if developer expressly asked for this Git commit in chat'},'requested_by_user':{'type':'string','description':'Brief verbatim quote of the developer commit request for audit; not independent proof of authorization'}},'required':['repository','paths','message','explicit_user_request','requested_by_user'],'additionalProperties':False}},
    {'name':'finalize_delegated_task','description':'Finalize only after independent review and every required current-revision application acceptance gate passes. Reuse task-linked evidence; missing or failing gates prevent completion. documentation_reviews may include __integration__ for a current cross-file impact review; unchanged per-document reviews are reused by content hash. Optionally mark a superseded deletion record.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'},'reviewed':{'type':'boolean'},'review_notes':{'type':'string','description':'Specific code/deletion review evidence'},'deleted_paths':{'type':'array','items':{'type':'string'},'description':'Builder-reported workspace-relative files confirmed absent after the deletion'},'project':{'type':'string','description':'Discovered Java project if targeted backend tests need to run'},'test_filter':{'type':'string','description':'Real remaining JUnit test selector, e.g. HealthControllerTest,PingControllerTest'},'supersedes_task_id':{'type':'string','description':'Optional earlier verified task replaced by this deletion; only when its entire file set matches deleted_paths'},'documentation_reviews':{'type':'object','additionalProperties':{'type':'string'},'description':'Exact changed or relevant documentation path -> factual review findings. Unchanged documents are reused by content hash; include reserved key __integration__ with a fresh cross-file impact review for the current implementation revision.'},'documentation_gaps':{'type':'array','items':{'type':'string'},'description':'All unresolved material implementation or documentation findings, including missing required updates; [] only after actual review. Resolve in-scope gaps with same-task Builder revision before acceptance. Track genuinely deferred non-blocking work, with owner/checkpoint and rationale; obtain explicit approval for material risk.'}},'required':['task_id','reviewed','review_notes'],'additionalProperties':False}},
    {'name':'delegate_to_builder','description':'Submit one scoped development task to the persistent Builder without closing the Lead chat; report a task ID. Only use for developer-authorized local changes. High-risk changes need explicit human approval outside this tool.',
     'inputSchema':{'type':'object','properties':{'goal':{'type':'string','description':'Complete authorized local implementation instructions'},'acceptance_criteria':{'type':'array','items':{'type':'string'}},'specification_files':{'type':'array','items':{'type':'string'},'description':'Optional existing workspace-relative specification documents; snapshot once for the Builder without repeating their contents'},'risk':{'type':'string','enum':['normal','high'],'description':'Classify the requested operation, not words in the spec; high-risk external/destructive actions are not supported by this local handoff'}},'required':['goal'],'additionalProperties':False}},
    {'name':'request_builder_revision','description':'Engineering Lead directs a focused correction to the same persistent Builder task. Builder edits files only, with no automatic tests or repair loop. Does not stage, commit or stash.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'},'feedback':{'type':'string','description':'Complete reviewed implementation defects and requested edits'},'specification_files':{'type':'array','items':{'type':'string'},'description':'Optional additional workspace-relative specification documents'},'risk':{'type':'string','enum':['normal','high']}},'required':['task_id','feedback'],'additionalProperties':False}},
    {'name':'verify_delegated_backend_task','description':'Record independent backend test results and code review, reusing current results when available. A backend pass alone never finalizes a task with incomplete required gates. Run other gates and finalize separately.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'},'project':{'type':'string'},'test_filter':{'type':'string'},'reviewed':{'type':'boolean','description':'True only after independently inspecting the Builder diff and finding no defects'},'review_notes':{'type':'string','description':'Factual independent review findings'},'reviewed_files':{'type':'array','items':{'type':'string'},'description':'Exact workspace-relative implementation files independently inspected by Lead; required if Builder reported blocked after writing code'},'documentation_reviews':{'type':'object','additionalProperties':{'type':'string'},'description':'Exact changed or relevant documentation path -> factual review findings. Unchanged documents are reused by content hash; include reserved key __integration__ with a fresh cross-file impact review for the current implementation revision.'},'documentation_gaps':{'type':'array','items':{'type':'string'},'description':'All unresolved material implementation or documentation findings, including missing required updates; [] only after actual review. Resolve in-scope gaps with same-task Builder revision before acceptance. Track genuinely deferred non-blocking work, with owner/checkpoint and rationale; obtain explicit approval for material risk.'}},'required':['task_id','project','test_filter','reviewed','review_notes'],'additionalProperties':False}},
    {'name':'wait_for_builder','description':'Register one completion wait after delegation or revision; default waits for the actual Builder outcome (up to one hour, covering the normal provider execution deadline). Check executable independent Lead work first. An explicit call always subscribes to actual Builder completion, even if dependency-ready items remain. The native client may manage its own background execution. Never create a Bash watcher, sleep loop or background shell. Do not end with a progress-only response instead of this call. Does not start tests or repair.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False}},
    {'name':'run_task_acceptance_check','description':'Run task-linked tests. Backend and managed API dependency resolution are offline by default. An online run requires allow_network=true plus explicit_developer_approval=true and a verbatim approval_quote for this specific check. network_usage reports dependency-manager evidence only; project test code is not OS-network-isolated. kind=web_verify executes selected project unit/typecheck/lint/build scripts; kind=web executes browser acceptance; kind=api launches the isolated Spring Boot app from gaston-e2e.json and executes loopback assertions; kind=mobile runs Flutter and Maestro checks. Missing prerequisites are blocked, never passed. Reuse unchanged evidence.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'},'kind':{'type':'string','enum':['backend','api','web','web_verify','mobile','flutter_tests','flutter_analyze']},'project':{'type':'string'},'test_filter':{'type':'string'},'relative_path':{'type':'string'},'allow_network':{'type':'boolean','description':'Allow Maven dependency network access for this check; requires explicit developer approval and a verbatim approval_quote'},'explicit_developer_approval':{'type':'boolean','description':'True only after the developer explicitly approved network access for this exact check'},'approval_quote':{'type':'string','description':'Verbatim developer approval; never invent or infer it'}},'required':['task_id','kind'],'additionalProperties':False}},
    {'name':'task_acceptance_report','description':'Compare real recorded backend/API/web/mobile checks against the task risk-based plan; expose missing checks and any untested cross-component paths.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False}},
    {'name':'task_verification_plan','description':'Prepare a risk-based test plan from actual Builder changed files, discovered project test gates, and cross-component impacts. Reports missing coverage; does not run or pass tests.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False}},
    {'name':'task_metrics','description':'Read actual worker duration, CLI-reported Codex token usage if exposed, and recorded verification evidence. Missing usage remains unknown.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False}},
    {'name':'delegation_status','description':'Read task status as a diagnostic snapshot. Prefer wait_for_builder for waiting rather than repetitive model-driven polling.',
     'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False}},
    {'name':'environment_status','description':'Inspect existing Playwright Test, local browser, Android emulators, Maestro and connected devices. No changes.',
     'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
    {'name':'browser_diagnose','description':'Attempt to launch the Playwright-managed Chromium headlessly, capture exact failure, no system Chrome or production access.',
     'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
    {'name':'run_web_smoke','description':'Run an isolated temporary Playwright counter test; default headless. Set visible=true to show a dedicated browser window for observation on a graphical desktop; clean up temporary fixtures.',
     'inputSchema':{'type':'object','properties':{'visible':{'type':'boolean','description':'Show a dedicated isolated Chromium window for visual debugging (default false). Requires a desktop display.'}},'additionalProperties':False}},
    {'name':'run_project_web_tests','description':'Execute existing Playwright tests using native webServer or an isolated project-owned backend launcher, with an explicit loopback external URL as fallback. Never create or edit application tests.',
     'inputSchema':{'type':'object','properties':{'project':{'type':'string','description':'Web project relative path; required if multiple valid web apps exist'},'visible':{'type':'boolean','description':'Run actual project specs in a headed browser for visual review; default false'}},'additionalProperties':False}},
    {'name':'start_existing_emulator','description':'Start an already installed Android AVD (not a physical device). Does not install system packages.',
     'inputSchema':{'type':'object','properties':{'name':{'type':'string','description':'Exact name returned by environment_status. Optional when just one AVD exists.'}},'additionalProperties':False}},
    {'name':'finish_mobile_verification','description':'Finish the mobile verification session and shut down only the Android emulator started by this Gaston verification process. Never stop pre-existing devices.',
     'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
    {'name':'prepare_builder_dependencies','description':'Before delegating Java work, check or prepare the shared project-local Maven/Gradle cache via a bounded compile-only preflight. Offline by default; authorized online fallback only after explicit developer approval. This is readiness, never task acceptance.',
     'inputSchema':{'type':'object','properties':{'project':{'type':'string','description':'Exact discovered Java build root; optional only when one is found'},'allow_network':{'type':'boolean','description':'Retry online if offline dependencies are missing; requires explicit developer authorization'},'explicit_developer_approval':{'type':'boolean','description':'True only after developer has explicitly approved a dependency download for this project'},'approval_quote':{'type':'string','description':'Actual developer authorization for dependency fetching; do not fabricate'}},'additionalProperties':False}},
    {'name':'run_backend_tests','description':'Independently run bounded Maven/Gradle tests in an existing Java repository. Dependency resolution is offline by default. Online dependency access requires allow_network=true, explicit_developer_approval=true and a verbatim approval_quote. Records distinguish permission from observed dependency-manager activity; project test code is not OS-network-isolated.',
     'inputSchema':{'type':'object','properties':{'project':{'type':'string','description':'Relative Java project path (omit if exactly one discovered project)'},'goal':{'type':'string','enum':['test','verify']},'test_filter':{'type':'string','description':'Optional exact Maven/JUnit test selector (e.g. SlugHelperTest)'},'offline':{'type':'boolean','description':'Force offline mode even when network is approved'},'allow_network':{'type':'boolean','description':'Permit dependency downloads for this run; requires explicit developer approval'},'explicit_developer_approval':{'type':'boolean','description':'True only after explicit developer approval for this project/run'},'approval_quote':{'type':'string','description':'Verbatim developer approval; never invent or infer it'}},'additionalProperties':False}},
    {'name':'run_http_smoke','description':'Run a real curl HTTP/JSON assertion against a disposable loopback service. No application changes, external hosts or shell access.',
     'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
    {'name':'run_local_api_checks','description':'Run an existing declarative API assertion suite using curl against AGENTIC_TEST_API_BASE_URL (loopback only). No arbitrary commands.',
     'inputSchema':{'type':'object','properties':{'relative_path':{'type':'string','description':'Existing workspace-relative JSON assertion suite.'}},'required':['relative_path'],'additionalProperties':False}},
    {'name':'run_maestro_tests','description':'Run existing Maestro YAML test flows against a connected Android emulator; no physical or production devices.',
     'inputSchema':{'type':'object','properties':{'relative_path':{'type':'string','description':'Workspace-relative flow file/directory (default .maestro).'}},'additionalProperties':False}},
]


def response(status: str, **fields) -> dict:
    return {'status':status, **fields}


# Provider-independent orchestration, exposed through the same Lead-only server.
_item_schema={'type':'object','properties':{'id':{'type':'string'},'owner':{'type':'string','enum':['lead','builder']},'kind':{'type':'string','enum':['inspect','implement']},'goal':{'type':'string'},'paths':{'type':'array','items':{'type':'string'}},'depends_on':{'type':'array','items':{'type':'string'}},'acceptance_criteria':{'type':'array','items':{'type':'string'}},'requires_idle_workspace':{'type':'boolean','description':'True for runtime/API/browser acceptance or other tasks unsafe during active Builder edits'}},'required':['id','owner','kind','goal'],'additionalProperties':False}
_str={'type':'string'};_strings={'type':'array','items':_str}
def _schema(name,description,properties,required):
    TOOLS.append({'name':name,'description':description,'inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False}})
_schema('plan_work','Decompose a substantial task into small coherent implementation slices and independent Lead inspections. Record real dependencies and narrow workspace-relative write paths. Inspection belongs to Lead. Set requires_idle_workspace=true for runtime acceptance and other exclusive checks. Do executable independent work while Builder implements. Avoid a single broad checkpoint delegation.',{'goal':_str,'items':{'type':'array','items':_item_schema}},['goal','items'])
_schema('extend_work_plan','Add concrete newly discovered remediation slices to an unaccepted plan; preserve all existing work and dependencies.',{'plan_id':_str,'items':{'type':'array','items':_item_schema}},['plan_id','items'])
_schema('defer_work_item','Mark an existing pending Lead item as requiring an idle workspace after an actual execution blocker. Does not close the item, alter dependencies or mark evidence passed.',{'plan_id':_str,'item_id':_str,'reason':_str},['plan_id','item_id','reason'])
_schema('supersede_work_item','Retire an unstarted duplicate or obsolete plan item with an auditable replacement item and reason. Rewires only pending, unstarted dependents; preserves the old item and history. Never marks work done.',{'plan_id':_str,'item_id':_str,'replacement_item_id':_str,'reason':_str},['plan_id','item_id','replacement_item_id','reason'])
_schema('inspect_web_application','Launch the actual local Vite application and associated isolated backend if configured; capture private screenshots at responsive widths, with optional project-owned authentication fixture. Does not substitute for formal E2E acceptance. No user environment exports or application dependency installs.',{'project':_str,'routes':_strings,'widths':{'type':'array','items':{'type':'integer'}},'height':{'type':'integer'},'visible':{'type':'boolean'}},['project'])
_schema('verification_reservation_status','Inspect the current managed verification reservation and process ownership; do not poll repeatedly.',{},[])
_schema('recover_verification_reservation','Clear an interrupted reservation only after developer explicitly confirms all owned test processes have terminated. Never terminate arbitrary processes or mark evidence passed.',{'developer_confirmed':{'type':'boolean'}},['developer_confirmed'])
_schema('work_plan_usage','Show observed per-slice and checkpoint usage, handoff bytes, corrections, tests and provider interruptions. Missing usage remains unknown.',{'plan_id':_str},['plan_id'])
_schema('work_plan_status','Return compact plan state with dependency_ready, executable and execution_blockers. Only executable independent Lead work can run concurrently; detailed=true returns full goals, criteria and evidence when necessary. Do not repeatedly poll live jobs.',{'plan_id':_str,'detailed':{'type':'boolean'}},['plan_id'])
_schema('inspect_workspace_files','Batch concurrent read-only checks for explicit files; returns existence, SHA256, matching line numbers for optional literal contains; no model calls. Read relevant source using native tools before interpretation.',{'files':{'type':'array','items':{'type':'object','properties':{'path':_str,'contains':_str},'required':['path'],'additionalProperties':False}}},['files'])
_schema('record_lead_inspection','Record independent Lead evidence (exact files, findings, decisions and gaps) for an inspection item; do not delegate inspection to Builder.',{'plan_id':_str,'item_id':_str,'evidence':_str,'followup_items':{'type':'array','items':_item_schema,'description':'Required for newly discovered implementation gaps not already planned; add specific remediation work now'}},['plan_id','item_id','evidence'])
_schema('start_lead_implementation','Open a small Lead-owned local implementation/test setup slice, no Builder. Use plan_id/item_id or goal, exact paths and acceptance_criteria. Use only authorized development scope.',{'plan_id':_str,'item_id':_str,'goal':_str,'paths':_strings,'acceptance_criteria':_strings},[])
_edit_schema={'type':'object','properties':{'path':_str,'before_sha256':{'type':['string','null'],'description':'Hash from latest inspection; repeat the same original hash for all replacements in a file; null only for a new file'},'content':_str,'old_text':_str,'new_text':_str},'required':['path','before_sha256'],'additionalProperties':False}
_schema('take_over_builder_task','Take over a narrow existing Builder bug directly before or after two unsuccessful corrections. Record findings and explicit local repair paths; only when Builder is stopped. No additional model invocation.',{'task_id':_str,'reason':_str,'paths':_strings},['task_id','reason','paths'])
_schema('lead_edit_files','Lead writes actual local test code/fixtures directly, including while Builder edits disjoint code. For small Lead-owned implementation or mandatory takeover after two unsuccessful Builder corrections use purpose=implementation. Batch related edits, including multiple entries for the same path with the same original SHA256 and unique non-overlapping old_text/new_text pairs. All pairs match the original file, not earlier replacements. Full content must stand alone for its path. No production or secret writes. Changes recorded against task; verification still required.',{'task_id':_str,'purpose':{'type':'string','enum':['test_setup','implementation']},'edits':{'type':'array','items':_edit_schema}},['task_id','purpose','edits'])
_schema('complete_lead_implementation','Finish Lead-owned implementation or takeover after actual edits; marks awaiting independent review, not verified.',{'task_id':_str,'summary':_str},['task_id','summary'])
_schema('review_work_slice','Record provisional slice review based on real current targeted verification evidence and document review; allows dependent slices to proceed. Unchanged document reviews are reused by content hash; include __integration__ for current cross-file impact review. Not complete checkpoint acceptance. All combined final gates still required.',{'task_id':_str,'review_notes':_str,'documentation_reviews':{'type':'object','additionalProperties':_str},'documentation_gaps':_strings},['task_id','review_notes'])
_schema('prepare_checkpoint_acceptance','After all planned inspections and implementation slices are reviewed, create one integrated verification task from actual changed paths. Use existing required verification and finalization gates; no extra Builder invocation.',{'plan_id':_str},['plan_id'])
# Evidence-backed local learning and isolated toolkit evolution.
_schema('learning_report','Refresh local evidence signals and observed token usage from executed tasks. No extra model calls, no speculative savings. Inspect causes before proposing corrections.',{},[])
_schema('record_engineering_lesson','Record deduplicated evidence-backed prevention after a finalized project task or a validated toolkit candidate. Keep project facts local; never record secrets. Not a permission or policy override.',{'trigger':_str,'cause':_str,'prevention':_str,'evidence':_str,'scope':{'type':'string','enum':['project','toolkit']},'candidate_id':_str},['trigger','cause','prevention','evidence'])
_schema('code_health','Read-only advisory static hints: Python and Java dead-code candidates, complexity and difficult-logic comment review (Python/brace languages), historical release documents. No automatic deletion or quality score. Inspect runtime usage and actual code.',{'relative_path':_str,'max_files':{'type':'integer'}},[])
_schema('task_code_review','List exact changed source files requiring independent Lead review, and current revision status. Inspect each actual file (including tests and deletion effects); static hints do not replace semantic review.',{'task_id':_str},['task_id'])
_quality_notes={'type':'object','properties':{k:_str for k in code_quality.DIMENSIONS},'required':list(code_quality.DIMENSIONS),'additionalProperties':False}
_schema('record_code_review','Record per-source-file evidence: clarity and naming; difficult logic and why-comments/invariants; simplicity and maintainability; correctness including failure/security/concurrency as relevant. List unresolved material findings; resolve and re-review after edits. Required on new tasks before slice and checkpoint acceptance. No new model required.',{'task_id':_str,'reviews':{'type':'object','additionalProperties':_quality_notes},'findings':_strings,'resolution':_str},['task_id','reviews','findings'])
_evo_edit={'type':'object','properties':{'path':_str,'before_sha256':{'type':['string','null']},'content':{'type':['string','null'],'description':'Full new file text; null removes an existing manifest-owned toolkit file'},'replacements':{'type':'array','items':{'type':'object','properties':{'old_text':_str,'new_text':_str},'required':['old_text','new_text'],'additionalProperties':False},'description':'Ordered unique exact text replacements against the hash-verified existing file; use instead of content'}},'required':['path','before_sha256'],'oneOf':[{'required':['content']},{'required':['replacements']}],'additionalProperties':False}
_schema('stage_evolution','Stage a toolkit-only improvement without editing live files. Use exact current SHA256, concrete root cause, and a new unittest regression. Prefer small exact replacements over full-file text for existing files; fix all affected call sites. For bugs reproduce a failure on the old source and pass on the candidate; no baseline test weakening. Review candidate source and regression with the four quality dimensions first. Protected recovery/validation files require a manual release.',{'title':_str,'root_cause':_str,'edits':{'type':'array','items':_evo_edit},'regression_test':_str,'kind':{'type':'string','enum':['bug','improvement']},'expected_benefit':_str,'incident_id':_str,'source_reviews':{'type':'object','additionalProperties':_quality_notes,'description':'Exact edited source paths plus key regression_test: clarity, difficult_logic, maintainability and correctness. Resolve material findings before staging.'}},['title','root_cause','edits','regression_test','source_reviews'])
_schema('validate_evolution','Run real baseline reproduction, candidate regression, unchanged full compatibility suites, package audit, and startup checks in disposable copies. Executes trusted local candidate code; provider-native approval applies. Not an OS sandbox; no provider credentials inherited. Long-running, register one wait; inspect failure logs.',{'candidate_id':_str},['candidate_id'])
_schema('runtime_recovery_status','Inspect native recovery and durable toolkit repair incidents. Recover only proven abandoned worker records. No model call, process termination or task restart.',{},[])
_schema('resume_builder','Resume an existing interrupted Builder task with original specification, revision and correction count. Manual resume only by developer request; scheduled callback must pass approved recovery_token. Never restart cancelled or live work.',{'task_id':_str,'recovery_token':_str},['task_id'])
_schema('quota_recovery_decision','ONLY after asking developer whether to schedule one-time recovery for the current quota interruption. Record explicit yes/no; if approved provide verified future timezone-aware ISO retry timestamp. On approval schedules exactly one local deterministic Builder-only wakeup; no provider-native cron or Lead fork.',{'task_id':_str,'approved':{'type':'boolean'},'user_request':_str,'retry_at':_str},['task_id','approved','user_request'])
_schema('record_quota_recovery_job','Legacy record lookup only; new recoveries are scheduled directly by quota_recovery_decision. Never create a provider-native job.',{'task_id':_str,'recovery_token':_str,'job_id':_str},['task_id','recovery_token','job_id'])
_schema('declare_review_only_documents','Before verification, declare exact scoped docs/*.md or .rst prose paths inspected as not read by application/build/tests or requirements. Only these paths may retain backend/source review evidence after an editorial change. Other docs and all API checks remain strict.',{'task_id':_str,'paths':_strings,'reason':_str},['task_id','paths','reason'])
_schema('extend_task_scope','Extend an inactive task and its plan item with exact necessary local paths; journal reason and check conflicting task ownership. Does not approve edits or clear findings.',{'task_id':_str,'paths':_strings,'reason':_str},['task_id','paths','reason'])
_schema('resolve_builder_questions','Respond to every recorded clarification with explicit inspected answers. If code was blocked only by unanswered intent but independently inspected implementation is already complete, provide implementation_complete and completion_evidence. Never waive scope, compilation, interruption or unfinished work. Review and tests remain required.',{'task_id':_str,'answers':{'type':'object','additionalProperties':_str},'implementation_complete':{'type':'boolean'},'completion_evidence':_str},['task_id','answers'])
_schema('reconcile_builder_scope','Classify every pending observed/reported path after independent inspection. Builder changes need prior scope extension; external changes require factual provenance; no automatic attribution, rollback or acceptance.',{'task_id':_str,'decisions':{'type':'object','additionalProperties':{'type':'object','properties':{'attribution':{'type':'string','enum':['builder','external']},'evidence':_str},'required':['attribution','evidence'],'additionalProperties':False}}},['task_id','decisions'])
_schema('abandon_empty_lead_task','Abandon an idle Lead task only if no edits or implementation evidence exist. Preserve record; unbind plan item for later work. No files are removed.',{'task_id':_str,'reason':_str},['task_id','reason'])
_schema('acceptance_evidence_detail','Retrieve exact saved acceptance evidence by task/check ID; detailed logs remain local and are returned only on explicit request, bounded.',{'task_id':_str,'check_id':_str,'include_logs':{'type':'boolean'}},['task_id','check_id'])
_schema('project_roadmap_status','Read registered developer-owned project context sources, optional structured roadmap or discovery candidates. No fixed filename or workflow is assumed.',{},[])
_schema('register_project_context_sources','Approve existing project documents, at any safe workspace-relative text paths, as durable context for Lead startup and compaction. No document is changed. Provide current registry SHA256 when replacing an approved source list.',{'paths':_strings,'developer_confirmed':{'type':'boolean'},'expected_sha256':{'type':['string','null']}},['paths','developer_confirmed'])
_milestone={'type':'object','properties':{'id':_str,'title':_str,'scope':_str,'status':{'type':'string','enum':['planned','in_progress','completed','deferred']},'source':_str},'required':['id','title','scope','status','source'],'additionalProperties':False}
_decision={'type':'object','properties':{'id':_str,'text':_str,'source':_str},'required':['id','text','source'],'additionalProperties':False}
_schema('record_project_roadmap','Save only explicitly developer-approved future milestone titles, scopes and decisions to project-owned docs/PROJECT_ROADMAP.json. On updates provide the exact previous SHA256. Do not infer absent roadmap items from a checkpoint summary.',
        {'milestones':{'type':'array','items':_milestone},'approved_decisions':{'type':'array','items':_decision},'next_milestone_id':_str,'developer_confirmed':{'type':'boolean'},'expected_sha256':{'type':['string','null']}},
        ['milestones','approved_decisions','next_milestone_id','developer_confirmed'])
_schema('lead_context_budget','Read native Lead context and counters at a natural checkpoint. No new model call or conversation deletion.',{},[])
_schema('checkpoint_lead_context','Persist factual decisions, next steps and exact task/plan IDs once at a natural checkpoint. Safe idle rollover is eligible only above context budget, without active work or newer developer messages. Do not record secrets.',{'summary':_str,'decisions':_strings,'next_steps':_strings,'task_ids':_strings,'plan_ids':_strings,'safe_to_rotate':{'type':'boolean'},'continue_work':{'type':'boolean','description':'Defaults to continuing current unfinished authorized plan; false only for explicit developer pause or concrete unresolved blocker, with stop_reason'},'stop_reason':{'type':'string','description':'Required if continue_work=false: exact developer pause or actual unresolved blocker, never context size or milestone completion alone'}},['summary','decisions','next_steps','task_ids','plan_ids','safe_to_rotate'])
_schema('evolution_status','Show local improvement candidates, actual test outcomes and queued activation/rollback; current toolkit stays unchanged during active work.',{},[])
_schema('queue_evolution','Queue one validated candidate or rollback of latest applied candidate for an idle ./dev startup. Ordinary helper fixes activate after validation; runtime/policy changes require actual developer confirmation at startup. No live writes, no Git changes.',{'candidate_id':_str,'rollback':{'type':'boolean'}},['candidate_id'])
_schema('cancel_evolution','Cancel pending activation or rollback without changing installed toolkit.',{},[])
_schema('export_toolkit','On developer request export the exact manifest-owned installed toolkit to a local root-layout ZIP. Never include private task/learning state; review source for project-specific content before sharing. No publication.',{},[])
for _tool in TOOLS:
    if _tool['name']=='delegate_to_builder':
        _tool['description']='Delegate ONE narrow implementation slice after Lead preflight; use plan_id/item_id for substantial work or exact paths and criteria for a single small Builder task. Lead owns investigations, test setup, small changes and review. Do not hand off a whole multi-part checkpoint. Continue independent Lead work concurrently.'
        _tool['inputSchema']['properties'].update(plan_id=_str,item_id=_str,paths=_strings,preflight={'type':'string','description':'Specific Lead-inspected files, decisions, constraints and existing test/runner contracts; identify independent work to retain'})
        _tool['inputSchema']['required']=['goal','preflight']
    if _tool['name']=='run_task_acceptance_check':
        _tool['inputSchema']['properties']['force']={'type':'boolean','description':'Intentional rerun instead of reusing a matching actual backend pass on an unchanged Git workspace'}
    if _tool['name']=='request_builder_revision':
        _tool['description']='Send one root-cause-based focused correction delta; after two unsuccessful correction rounds the Lead must repair directly. Quota/interruption resumes do not consume a correction round. Environment diagnosis belongs to Lead.'
        _tool['inputSchema']['properties'].update(failure_kind={'type':'string','enum':['implementation','architecture','specification','test_fixture','environment','regression']},root_cause=_str,reproduction=_str)

_VERIFICATION_CANCEL = ContextVar('verification_cancel', default=None)
_VERIFICATION_JOB = ContextVar('verification_job', default=None)


def execute(argv, *, cwd=None, env=None, timeout=60, output_limit=9000):
    try:
        from platform_compat import prepare_subprocess, resolve_flutter, run_verification_process
        cwd = Path(cwd or ROOT)
        argv = list(argv)
        if argv and argv[0] == 'flutter':
            argv[0] = resolve_flutter(cwd, workspace=ROOT)
        argv, env, _ = prepare_subprocess(argv, env)
        job_name = _VERIFICATION_JOB.get()
        if job_name:
            import verification_coordination as coordination
            coordination.begin_windows_command(ROOT,job_name)
        result = run_verification_process(argv, cwd=cwd, env=env, timeout=timeout,
                                          output_limit=output_limit, cancel_event=_VERIFICATION_CANCEL.get(),
                                          job_name=job_name,log_dir=LOCAL/'verification-logs')
        if isinstance(result.get('log_path'),str):
            try:result['log_path']=Path(result['log_path']).resolve().relative_to(ROOT.resolve()).as_posix()
            except (OSError,ValueError):result.pop('log_path',None)
        if job_name and result.get('owned_processes_terminated') is True:
            coordination.confirm_windows_cleanup(ROOT,job_name)
        return result
    except (OSError, ValueError, RuntimeError) as exc:
        return {'exit_code':None, 'reason':'execution_failed', 'stderr':str(exc)[-1000:]}


_DEFAULT_EXECUTE = execute


def _execute_browser(argv, *, cwd, env, timeout):
    """Use the owned process-group runner in production, retaining Lead test injection."""
    if execute is not _DEFAULT_EXECUTE:
        return execute(argv,cwd=cwd,env=env,timeout=timeout)
    import web_acceptance
    result=web_acceptance.execute_owned(argv,cwd=cwd,env=env,timeout=timeout,log_dir=LOCAL/'verification-logs')
    if isinstance(result.get('log_path'),str):
        try:result['log_path']=Path(result['log_path']).resolve().relative_to(ROOT.resolve()).as_posix()
        except (OSError,ValueError):result.pop('log_path',None)
    return result


def sdk_tool(name):
    executable = name + '.exe' if os.name == 'nt' else name
    for key in ('ANDROID_HOME','ANDROID_SDK_ROOT'):
        sdk = os.environ.get(key)
        if sdk:
            candidate = Path(sdk)/('emulator' if name == 'emulator' else 'platform-tools')/executable
            if candidate.is_file(): return str(candidate)
    home = Path.home()
    roots = [home/'Android/Sdk', home/'Library/Android/sdk']
    if os.environ.get('LOCALAPPDATA'):
        roots.append(Path(os.environ['LOCALAPPDATA'])/'Android/Sdk')
    for root in roots:
        candidate = root/('emulator' if name == 'emulator' else 'platform-tools')/executable
        if candidate.is_file(): return str(candidate)
    return shutil.which(name)


def avd_names():
    binary=sdk_tool('emulator')
    if not binary: return []
    result=execute([binary,'-list-avds'],timeout=15)
    if result['exit_code'] != 0: return []
    return [line.strip() for line in result['stdout'].splitlines()
            if re.fullmatch(r'[a-zA-Z0-9_.-]{1,120}',line.strip())]


def connected_emulators():
    adb=sdk_tool('adb')
    if not adb: return []
    result=execute([adb,'devices'],timeout=15)
    if result['exit_code'] != 0: return []
    return [line.split('\t')[0] for line in result['stdout'].splitlines()[1:]
            if re.fullmatch(r'emulator-\d+\tdevice',line.strip())]


def playwright_folder():
    import playwright_tools
    folder=playwright_tools.test_workspace(ROOT)
    return folder if (folder/'node_modules/@playwright/test/package.json').is_file() else None


def status():
    import playwright_tools
    folder=playwright_folder()
    from platform_compat import maestro_binary
    return response('ready', toolkit_release=TOOLKIT_RELEASE, toolkit_build=TOOLKIT_BUILD, manifest_sha256=MANIFEST_SHA256, server_path=SERVER_PATH, server_pid=os.getpid(), completion_wait_seconds=BUILDER_WAIT_SECONDS, playwright_test=bool(folder),
                    playwright_workspace=str(folder.relative_to(ROOT)) if folder else None,
                    chromium_installed=bool(folder and playwright_tools.chromium_installed(ROOT)),
                    maestro_binary=maestro_binary(),
                    configured_avds=avd_names(), connected_emulators=connected_emulators())


def browser_diagnose():
    folder=playwright_folder()
    if not folder: return response('blocked', reason='Playwright Test is not installed in a detected test workspace')
    script=("const { chromium } = require('@playwright/test');\n"
            "(async()=>{const browser=await chromium.launch({headless:true});"
            "const page=await browser.newPage();await page.goto('about:blank');"
            "console.log('PLAYWRIGHT_CHROMIUM_LAUNCHED');await browser.close();})()"
            ".catch(e=>{console.error(e.message);process.exit(1)});\n")
    env=os.environ.copy();env['GSETTINGS_BACKEND']='memory'
    res=execute(['node','-e',script],cwd=folder,env=env,timeout=45)
    return response('pass' if res['exit_code']==0 and 'PLAYWRIGHT_CHROMIUM_LAUNCHED' in res['stdout'] else 'blocked',
                    runner='playwright-managed-chromium', evidence=res)


def run_web_smoke(visible=False):
    if not isinstance(visible,bool): return response('blocked',reason='visible must be a boolean')
    if visible and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return response('blocked',reason='Visible browser requires a desktop display; headless remains available')
    folder=playwright_folder()
    if not folder: return response('blocked',reason='Playwright Test not installed; no test executed')
    if not shutil.which('node') or not shutil.which('npm'):
        return response('blocked',reason='Node.js or npm missing; no test executed')
    scratch=LOCAL/'verification-scratch'
    scratch.mkdir(parents=True,exist_ok=True)
    # Only generated local fixtures, never application code or an external URL.
    with tempfile.TemporaryDirectory(prefix='web-',dir=scratch) as temp:
        temp=Path(temp)
        page=temp/'counter.html'
        page.write_text('<!doctype html><html><body><button id="count" onclick="this.textContent=String(Number(this.textContent)+1)">0</button></body></html>',encoding='utf-8')
        module=str((folder/'node_modules/@playwright/test').resolve())
        spec=temp/'counter.spec.cjs'
        spec.write_text("const { test, expect } = require("+json.dumps(module)+");\n"
                        "test('counter increments twice',async ({page})=>{await page.goto("+json.dumps(page.as_uri())+");"
                        "await page.locator('#count').click();await page.locator('#count').click();"
                        "await expect(page.locator('#count')).toHaveText('2');});\n",encoding='utf-8')
        config=temp/'playwright.config.cjs'
        config.write_text("const { defineConfig } = require("+json.dumps(module)+");\n"
                          "module.exports=defineConfig({testDir:__dirname,reporter:'line',"
                          "use:{browserName:'chromium',headless:"+ ("false" if visible else "true") + "},workers:1,retries:0});\n",encoding='utf-8')
        env=os.environ.copy();env['GSETTINGS_BACKEND']='memory';env['CI']='1'
        result=execute(['node',str((folder/'node_modules/@playwright/test/cli.js').resolve()),
                        'test','--config',str(config)],cwd=temp,env=env,timeout=120)
        return response('pass' if result['exit_code']==0 else 'blocked',
                        note='Temporary HTML/spec cleaned up; application files untouched',evidence=result)


def inspect_web_application(project,routes=None,widths=None,height=900,visible=False):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    import visual_web
    return _standalone_verification('visual_web',project,lambda:visual_web.run(ROOT,project,routes=routes,widths=widths,height=height,visible=visible))


def run_project_web_tests(project=None,visible=False):
    """Run the selected app's real Playwright specs with a managed lifecycle."""
    import workspace_checks
    import web_acceptance
    candidates=[]
    for rel in workspace_checks.web_projects(ROOT):
        candidate=(ROOT/rel).resolve()
        if candidate.is_relative_to(ROOT) and any((candidate/c).is_file() for c in workspace_checks.CONFIGS) and workspace_checks._test_files(candidate):
            candidates.append(rel)
    if project is None and len(candidates)==1:project=candidates[0]
    if project not in candidates:
        return response('blocked',reason='Select exactly one app with real Playwright specs',projects=candidates)
    return web_acceptance.run_browser(ROOT,project,execute=_execute_browser,external_url=os.environ.get('PLAYWRIGHT_BASE_URL'),visible=visible)



def run_backend_tests(project=None, goal='test', test_filter=None, offline=None, allow_network=False,
                      explicit_developer_approval=False, approval_quote=''):
    if goal not in ('test','verify'):return response('blocked',reason='Lead test verification requires test or verify, not compile-only')
    if allow_network and (explicit_developer_approval is not True or not isinstance(approval_quote,str) or not approval_quote.strip()):
        return response('blocked',reason='Online dependency access requires explicit developer approval for this run',
                        network_allowed=False,network_policy='offline',network_usage='not_used_by_build_tool',network_attempted=False,
                        network_scope='Maven/Gradle dependency resolution only; project build/test code is not OS-network-isolated')
    import backend_runner
    options=backend_runner.discover(ROOT)
    if not options:
        return response('blocked',reason='No Maven/Gradle project detected in workspace',
                        network_allowed=False,network_policy='offline',network_usage='not_used_by_build_tool',network_attempted=False,
                        network_scope='Maven/Gradle dependency resolution only; project build/test code is not OS-network-isolated')
    if project is None:
        if len(options)!=1:
            return response('blocked',reason='Multiple Java projects; provide exact project path',projects=options,
                            network_allowed=False,network_policy='offline',network_usage='not_used_by_build_tool',network_attempted=False,
                            network_scope='Maven/Gradle dependency resolution only; project build/test code is not OS-network-isolated')
        project=options[0]['path']
    network_allowed=bool(allow_network and offline is not True)
    effective_offline=bool(offline) if offline is not None else not network_allowed
    if not network_allowed:effective_offline=True
    # Maven/Gradle are always offline unless this call carries explicit approval.
    result=backend_runner.run(ROOT,project,goal=goal,test_filter=test_filter,
                               offline=effective_offline,allow_network=network_allowed,timeout=600)
    usage=result.get('network_usage','unknown' if network_allowed else 'not_used_by_build_tool')
    return {**result,'network_allowed':network_allowed,
            'network_policy':'approved_online' if network_allowed else 'offline',
            'network_usage':usage,'network_attempted':result.get('network_attempted',backend_runner.dependency_network_attempted(usage)),
            'network_scope':result.get('network_scope','Maven/Gradle dependency resolution only; project build/test code is not OS-network-isolated')}


def prepare_builder_dependencies(project=None, allow_network=False, explicit_developer_approval=False, approval_quote=''):
    """Compile trusted project state *before* delegating; never a Builder acceptance pass.

    Uses exactly the same workspace-local dependency path as the Builder helper.
    The Builder must still compile its final edits in its own sandbox/attempt.
    """
    import backend_runner
    import builder_compile
    projects=backend_runner.discover(ROOT)
    if not projects:
        return response('blocked',reason='No Maven/Gradle project detected')
    if project is None:
        if len(projects)!=1:
            return response('blocked',reason='Specify the Java project',projects=projects)
        project=projects[0]['path']
    if project not in {item['path'] for item in projects}:
        return response('blocked',reason='Project is not a discovered Java build root',projects=projects)
    if allow_network and (explicit_developer_approval is not True or not isinstance(approval_quote,str) or not approval_quote.strip()):
        return response('blocked',reason='Online dependency fetching requires explicit, project-specific developer approval; offline preflight is available without it',
                        project=project,network_allowed=False,network_policy='offline',network_usage='not_used_by_build_tool',
                        network_attempted=False,network_scope='Maven/Gradle dependency resolution only; project build/test code is not OS-network-isolated')
    result=builder_compile.check(ROOT,project,allow_network=bool(allow_network),approval_quote=approval_quote)
    return {'status':result.get('status','blocked'),'project':project,'goal':'test-compile',
            'cache_scope':'.agentic/local/backend-dependencies',
            'network_allowed':result.get('network_allowed',False),
            'network_policy':'approved_online' if result.get('network_allowed') else 'offline',
            'network_usage':result.get('network_usage','not_used_by_build_tool'),
            'network_attempted':result.get('network_attempted'),
            'network_scope':result.get('network_scope','Maven/Gradle dependency resolution only; project build/test code is not OS-network-isolated'),
            'network_approval_quote':result.get('network_approval_quote'),
            'preflight':result,
            'ready_for_delegation':result.get('status')=='pass',
            'builder_compile_still_required':True,
            'note':'Runs against the current Lead-visible trusted source. Builder must compile changed source again and produce its own fresh receipt. No network without explicit approval.'}


def run_http_smoke():
    """Prove real curl execution against a disposable, local, assertion-friendly service."""
    if not shutil.which('curl'):
        return response('blocked', reason='curl executable missing; no HTTP request sent')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/health':
                self.send_error(404)
                return
            payload=b'{"ok":true,"service":"gaston-smoke"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    try:
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    except OSError as exc:
        return response('blocked',reason='Cannot bind disposable loopback HTTP service',error=str(exc))
    thread=Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        outcome=execute(['curl','--silent','--show-error','--noproxy','*',
                         '--max-redirs','0','--max-time','5','--write-out','\n%{http_code}',
                         f'http://127.0.0.1:{server.server_port}/health'],timeout=10)
        text=outcome.get('stdout','')
        body,sep,code=text.rpartition('\n')
        try: valid=(outcome['exit_code']==0 and sep and code.strip()=='200' and
                    json.loads(body)=={'ok':True,'service':'gaston-smoke'})
        except ValueError:valid=False
        return response('pass' if valid else 'fail',
                        evidence={'curl_exit_code':outcome['exit_code'],
                                  'http_status':code.strip() if sep else None,
                                  'json_assertions_passed':bool(valid),
                                  'assertions':{'curl_exit_zero':outcome['exit_code']==0,
                                                'http_status_200':bool(sep and code.strip()=='200'),
                                                'json_equals_expected':bool(valid)},
                                  'response_body':body[:500],
                                  'stderr':outcome.get('stderr','')[-300:]},
                        cleanup='Disposable HTTP server stopped')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def run_local_api_checks(relative_path):
    """Execute the existing assertion runner with its strict loopback protections."""
    if not isinstance(relative_path,str) or not relative_path or Path(relative_path).is_absolute():
        return response('blocked',reason='Provide a workspace-relative JSON API test suite')
    spec=(ROOT/relative_path).resolve()
    if not spec.is_relative_to(ROOT) or not spec.is_file() or spec.is_symlink() or spec.suffix!='.json':
        return response('blocked',reason='API suite must be an existing JSON file within the workspace')
    if not os.environ.get('AGENTIC_TEST_API_BASE_URL'):
        return response('blocked',reason='No loopback API base URL configured; no request sent')
    # api_checks.py rejects non-loopback origins, redirects, and missing assertions.
    import api_checks
    try: api_checks.base_url()
    except ValueError as exc:return response('blocked',reason=str(exc))
    outcome=execute([sys.executable,str(runtime_script('api_checks.py')),str(spec)],timeout=180)
    code=outcome['exit_code']
    return response('pass' if code==0 else 'blocked' if code in (None,2) else 'fail',
                    evidence={'exit_code':code,'summary':outcome.get('stdout','')[-3500:],
                              'stderr':outcome.get('stderr','')[-300:]})



# Local emulator ownership is explicit: no pre-existing emulator is ever adopted.
# Process identity uses Linux /proc starttime so a recycled PID cannot be signalled.
def _proc_identity(pid):
    if os.name=='nt' or sys.platform=='darwin':
        import psutil
        try:
            process=psutil.Process(int(pid))
            if not process.is_running():return None
            argv=process.cmdline()
            return {'starttime':str(int(process.create_time()*1000000)),'argv':argv} if argv else None
        except (psutil.Error,TypeError,ValueError,OSError):return None
    try:
        raw=(Path('/proc')/str(int(pid))/'stat').read_text(encoding='utf-8')
        fields=raw.rsplit(')',1)[1].split()
        if fields[0]=='Z': return None  # zombie is already stopped
        started=fields[19]  # /proc/<pid>/stat field 22
        argv=(Path('/proc')/str(int(pid))/'cmdline').read_bytes().split(b'\x00')
        argv=[arg.decode('utf-8','replace') for arg in argv if arg]
        return {'starttime':started,'argv':argv} if argv else None
    except (OSError,ValueError,IndexError):
        return None


def _owned_emulator_file():
    return LOCAL/'emulator-runs'/'owned.json'


def _read_emulator_owner():
    path=_owned_emulator_file()
    if path.is_symlink(): return None
    try:
        obj=json.loads(path.read_text(encoding='utf-8'))
        return obj if isinstance(obj,dict) and obj.get('version')==1 else None
    except (ValueError,OSError): return None


def _write_emulator_owner(owner):
    path=_owned_emulator_file();path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('owned-'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('x',encoding='utf-8') as file:
            os.chmod(tmp,0o600)
            json.dump(owner,file)
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


def _forget_emulator_owner():
    path=_owned_emulator_file()
    if not path.is_symlink():path.unlink(missing_ok=True)


def _valid_owned_emulator(owner):
    """Verify the original process, including Android emulator's qemu exec form.

    The Linux Android emulator launcher may exec qemu-system-* in place, so
    checking only argv[0].startswith('emulator') loses legitimate ownership.
    PID + /proc starttime + exact AVD argument must *all* still match.
    """
    if not isinstance(owner,dict):return False
    pid=owner.get('pid')
    if type(pid) is not int or pid<=1:return False
    identity=_proc_identity(pid)
    if not identity or identity['starttime']!=owner.get('starttime'):return False
    argv=identity['argv'];avd=owner.get('avd')
    if not isinstance(avd,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,120}',avd):return False
    if '-avd' not in argv or argv.index('-avd')+1>=len(argv):return False
    if argv[argv.index('-avd')+1]!=avd:return False
    command=Path(argv[0]).name
    if command.lower().startswith('emulator'):return True
    if not re.fullmatch(r'qemu-system-[a-zA-Z0-9_-]+',command):return False
    # Android's qemu binary must belong to the same installed SDK emulator
    # directory, not a user-supplied unrelated qemu process.
    binary=sdk_tool('emulator')
    if not binary:return False
    try:
        sdk_dir=Path(binary).resolve().parent
        actual=Path('/proc')/str(pid)/'exe'
        actual_exe=actual.resolve(strict=True)
        return actual_exe.is_relative_to(sdk_dir)
    except (OSError,RuntimeError):return False


def _verifier_still_owns(owner):
    origin=owner.get('verifier_pid')
    if type(origin) is not int:return False
    identity=_proc_identity(origin)
    return bool(identity and identity['starttime']==owner.get('verifier_starttime'))


def finish_mobile_verification(*, abandoned=False):
    """Stop only a verified Gaston-launched AVD; never stop a user-launched AVD.

    An active different verifier is not interrupted. After unexpected termination,
    the next verifier may recover an abandoned emulator after checking PID identity.
    """
    owner=_read_emulator_owner()
    if not owner:return response('ready',cleanup='No Gaston-owned emulator to stop')
    if owner.get('verifier_pid')!=os.getpid() and _verifier_still_owns(owner):
        return response('ready',cleanup='Another active Gaston verifier owns this emulator; unchanged')
    if not _valid_owned_emulator(owner):
        # Never signal if identity is uncertain. Preserve a live record for
        # diagnosis; a future invocation may see the process after it execs.
        current=_proc_identity(owner.get('pid')) if type(owner.get('pid')) is int else None
        if current is None:
            _forget_emulator_owner()
            return response('ready',cleanup='Gaston-owned emulator process no longer exists')
        return response('blocked',reason='Emulator ownership could not be verified; no process signalled',
                        recorded_pid=owner.get('pid'),recorded_avd=owner.get('avd'),
                        recorded_starttime=owner.get('starttime'),
                        observed_starttime=current.get('starttime'),
                        observed_command=Path(current['argv'][0]).name)
    pid=owner['pid']
    try:
        if os.name=='nt':
            import psutil
            psutil.Process(pid).terminate()
        else:os.kill(pid,signal.SIGTERM)
    except ProcessLookupError:
        _forget_emulator_owner()
        return response('ready',cleanup='Gaston-owned emulator already stopped')
    except PermissionError:
        return response('blocked',reason='Cannot terminate verified Gaston-owned emulator',pid=pid)
    for _ in range(100):
        if _proc_identity(pid) is None:
            _forget_emulator_owner()
            return response('pass',cleanup='Gaston-owned emulator stopped',pid=pid,avd=owner['avd'])
        time.sleep(.1)
    # Avoid SIGKILL and avoid false success. Keep ownership for a safe retry.
    return response('blocked',reason='SIGTERM sent but emulator shutdown not confirmed',pid=pid,avd=owner['avd'])


def _cleanup_on_exit():
    try:finish_mobile_verification()
    except Exception:pass
    try:
        import lead_workspace_ops
        lead_workspace_ops.preview(ROOT,action='stop')
    except Exception:pass


def _recover_abandoned_emulator():
    owner=_read_emulator_owner()
    if owner and not _verifier_still_owns(owner):
        return finish_mobile_verification(abandoned=True)
    return response('ready',cleanup='No abandoned emulator to recover')


def start_existing_emulator(name=''):
    binary=sdk_tool('emulator'); known=avd_names()
    if not binary: return response('blocked',reason='Android emulator executable missing')
    if not known: return response('blocked',reason='No already configured Android AVD found')
    if not name and len(known)==1: name=known[0]
    if name not in known: return response('blocked',reason='Select an exact installed AVD name',configured_avds=known)
    running=connected_emulators()
    if running: return response('ready', reason='An Android emulator is already connected',devices=running)
    owner=_read_emulator_owner()
    if owner and _proc_identity(owner.get('pid')) is not None and not _valid_owned_emulator(owner):
        return response('blocked',reason='Existing emulator launch record is unverifiable; cannot safely start another',
                        recorded_pid=owner.get('pid'))
    if owner and _valid_owned_emulator(owner):
        return response('starting',avd=owner['avd'],pid=owner['pid'],cleanup_tracking='enabled',
                        note='Previously launched Gaston emulator is still booting; do not launch a duplicate')
    folder=LOCAL/'emulator-runs';folder.mkdir(parents=True,exist_ok=True)
    logfile=folder/(re.sub('[^a-zA-Z0-9_.-]','_',name)+'.log')
    argv=[binary,'-avd',name,'-no-snapshot-save']
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')): argv+=['-no-window']
    # Background startup does not grant shell access to the reviewer.
    try:
        with logfile.open('ab') as handle:
            process=subprocess.Popen(argv,stdout=handle,stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,start_new_session=True)
    except OSError as exc:
        return response('blocked',reason='Emulator could not start',error=str(exc))
    time.sleep(1)
    if process.poll() is not None:
        return response('blocked', reason='Emulator exited during startup',exit_code=process.returncode,
                        logfile=str(logfile.relative_to(ROOT)))
    identity=_proc_identity(process.pid)
    verifier=_proc_identity(os.getpid())
    ownership=bool(identity and verifier and '-avd' in identity['argv'] and
                   identity['argv'].index('-avd')+1<len(identity['argv']) and
                   identity['argv'][identity['argv'].index('-avd')+1]==name)
    if ownership:
        _write_emulator_owner({'version':1,'avd':name,'pid':process.pid,
                               'starttime':identity['starttime'],'verifier_pid':os.getpid(),
                               'verifier_starttime':verifier['starttime']})
    return response('starting',avd=name,pid=process.pid,logfile=str(logfile.relative_to(ROOT)),
                    cleanup_tracking='enabled' if ownership else 'unverified',
                    note='Call environment_status to confirm adb connects before Maestro runs')


def _run_maestro_tests_impl(relative_path='.maestro', *, expected_device=None, cwd=None):
    devices=connected_emulators()
    if len(devices) != 1:
        return response('blocked', reason='Exactly one connected Android emulator is required; physical devices are not used',devices=devices)
    if expected_device is not None and devices != [expected_device]:
        return response('blocked', reason='Selected Android emulator changed', devices=devices)
    from platform_compat import maestro_binary
    maestro=maestro_binary()
    if not maestro:
        return response('blocked',reason='Maestro CLI not found')
    relative_path=str(relative_path or '.maestro')
    candidate=(ROOT/relative_path).resolve()
    if (not candidate.is_relative_to(ROOT) or not candidate.exists() or candidate.is_symlink() or
            candidate.is_file() and candidate.suffix not in ('.yaml','.yml')):
        return response('blocked',reason='Maestro flow must be an existing YAML file or directory inside the workspace')
    # Maestro's documented global --device flag must precede `test`.
    result=execute([maestro,'--device',devices[0],'test',str(candidate)],
                   cwd=cwd or ROOT,timeout=300)
    return response('pass' if result['exit_code']==0 else 'blocked' if result['exit_code'] is None else 'fail',device=devices[0],evidence=result)

def run_maestro_tests(relative_path='.maestro'):
    try:
        result=_run_maestro_tests_impl(relative_path)
    finally:
        # Cleanup also runs if test validation or execution unexpectedly raises.
        cleanup=finish_mobile_verification()
    return {**result, 'emulator_cleanup':cleanup}


def _mobile_failure_screenshot(device, adb):
    """Save only a bounded emulator screenshot in private Gaston state."""
    if not adb or not re.fullmatch(r'emulator-\d+', device) or connected_emulators() != [device]:
        return None
    try:
        capture = subprocess.run([adb, '-s', device, 'exec-out', 'screencap', '-p'],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = capture.stdout
    if capture.returncode or not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 16 * 1024 * 1024:
        return None
    folder = LOCAL / 'mobile-evidence'
    if folder.resolve() != folder.absolute():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid.uuid4().hex + '.png')
    with path.open('xb') as stream:
        stream.write(data)
    return path.relative_to(ROOT).as_posix()


def run_project_mobile_tests(project=None, relative_path=None):
    import workspace_checks
    import mobile_acceptance
    _, discovered = workspace_checks.discover(ROOT, {})
    projects = discovered.get('flutter', [])
    if not projects:
        # Preserve the non-Flutter Maestro workflow.
        return run_maestro_tests(relative_path or ((project+'/.maestro') if project and project!='.' else '.maestro'))
    if project is None and len(projects) == 1:
        project = projects[0]
    if project not in projects:
        return response('blocked', reason='Select a discovered Flutter application', projects=projects)
    return mobile_acceptance.run(ROOT, project, relative_path, execute=execute,
        sdk_tool=sdk_tool, avd_names=avd_names, start_emulator=start_existing_emulator,
        finish_emulator=finish_mobile_verification, maestro_run=_run_maestro_tests_impl,
        screenshot=_mobile_failure_screenshot)


LIVE_SERVER = False

def _authorized_delegate():
    """Only the registered Lead's project-local MCP process has the secret.

    This is a guard against incidental local tool misuse, not an OS sandbox:
    the logged-in developer retains control of files and processes.
    """
    secret_file=LOCAL/'lead-verification-token'
    try:
        expected=secret_file.read_text(encoding='ascii').strip()
        supplied=os.environ.get('GASTON_LEAD_TOKEN','')
        if len(expected)<40 or not secrets.compare_digest(supplied,expected):return False
        if not LIVE_SERVER:return True
        from session_store import Store
        return Store(ROOT).lead_caller(os.environ.get('GASTON_SESSION_LEASE'),os.environ.get('GASTON_LEAD_PROVIDER'),os.getpid())
    except OSError:return False


def _builder_worker_env():
    """Do not pass the private Lead MCP credential to child implementation processes."""
    env = dict(os.environ)
    env.pop('GASTON_LEAD_TOKEN', None)
    env.pop('GASTON_SESSION_LEASE', None)
    env.pop('GASTON_LEAD_PROVIDER', None)
    return env


def _mark_builder_queued(folder, **fields):
    """Publish the queue owner before spawning; a crash in that gap is recoverable."""
    import eng
    import socket
    from session_store import process_identity
    owner={'pid':os.getpid(),'host':socket.gethostname(),**process_identity(os.getpid())}
    eng.save_state(folder,'queued_for_builder',queued_at=time.time(),queued_launcher=owner,**fields)


def _launch_builder(folder, *, revision=0):
    """Reserve and spawn one Builder attempt under the workspace launch lock.

    The child claims the same lock before provider invocation. Holding it from
    reservation through publication of worker.json prevents a concurrent
    Lead/tool request from replacing worker-launch.json while the first child
    is starting (the source of false "Superseded Builder launch" exits).
    """
    import eng
    from session_store import process_identity
    import socket
    with orch.mutation_guard(ROOT):
        state = orch.read_record(folder/'state.json')
        if state.get('stage') != 'queued_for_builder':
            return None
        prior = orch.read_record(folder/'worker.json')
        if prior and type(prior.get('pid')) is int:
            owner_state = orch.process_state(prior)
            finished = orch.read_record(folder/'worker-finished.json').get('finished_at')
            begun = prior.get('started_at')
            if owner_state != 'dead' and isinstance(begun,(float,int)) and (not isinstance(finished,(float,int)) or finished < begun):
                # Another launch already owns this queued task. Never supersede it.
                return None
        started_at = time.time()
        if prior:
            # Preserve the exact old attempt even when its task and revision are reused.
            orch.put(folder/'worker-attempts'/f"{uuid.uuid4().hex}.json", prior)
        attempt = uuid.uuid4().hex
        launch = {'attempt':attempt, 'started_at':started_at, 'launcher':{'pid':os.getpid(), 'host':socket.gethostname(), **process_identity(os.getpid())}}
        orch.put(folder/'worker-launch.json', launch)
        with (folder/'worker.log').open('ab') as out:
            try:
                proc = subprocess.Popen([sys.executable, str(runtime_script('live_builder_worker.py')), folder.name, attempt],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                    **__import__('platform_compat').popen_kwargs(), env=_builder_worker_env())
            except OSError as exc:
                with redirect_stdout(sys.stderr):
                    eng.save_state(folder, 'blocked', reason=f'Cannot start Builder worker: {exc}', blocker='worker_start_failed', resumable=True, revision=revision)
                orch.put(folder/'worker-finished.json', {'finished_at':time.time(), 'stage':'blocked','revision':revision})
                return None
        orch.put(folder/'worker.json', {'pid':proc.pid, 'started_at':started_at, 'attempt':attempt, 'host':socket.gethostname(), **process_identity(proc.pid)})
        import wait_guard
        wait_guard.begin(folder, attempt, os.environ.get('GASTON_SESSION_LEASE'))
        return proc.pid


def _resume_builder(args):
    import eng
    folder = _task_folder(args.get('task_id'))
    if not folder:
        return response('blocked', reason='Unknown task')
    state = orch.read_record(folder/'state.json')
    if state.get('stage') != 'blocked' or not state.get('resumable') or state.get('blocker') not in ('provider_quota','worker_interrupted','worker_start_failed','session_start_blocked'):
        return response('blocked', reason='Only a recorded interrupted/resumable task can resume')
    if orch.active_builders(ROOT) or orch.read_record(folder/'takeover.json'):
        return response('blocked', reason='A Builder is active or Lead owns the repair')
    import recovery_consent
    scheduled=None
    if 'recovery_token' in args:
        try:scheduled=recovery_consent.check_resume(folder,args['recovery_token'])
        except ValueError as exc:return response('blocked',reason=str(exc))
    elif state.get('blocker')=='provider_quota':
        # Manual resume invalidates a pending native wakeup for the same attempt.
        current=orch.read_record(folder/'quota-recovery.json')
        if current.get('attempt')==recovery_consent.interruption(folder) and current.get('decision')=='approved':scheduled=current
    from session_store import Store
    store = Store(ROOT)
    provider = store._configured_roles()[0]
    try:
        recovered = store.reconcile(provider, 'builder')
    except (RuntimeError, OSError) as exc:
        return response('blocked', reason=str(exc), task_id=folder.name, resumable=True)
    try:orch.validate_task_scope(ROOT,folder)
    except ValueError as exc:return response('blocked',reason=str(exc),task_id=folder.name)
    revision = orch.read_record(folder/'revision-request.json')
    number = revision.get('number', 0)
    # Reuse the immutable original goal, specification and correction request.
    with redirect_stdout(sys.stderr):
        _mark_builder_queued(folder, revision=number, verification_owner='reviewer')
    pid = _launch_builder(folder, revision=number)
    if scheduled:recovery_consent.consume(folder,scheduled)
    if pid is None:
        return response('blocked', reason='Builder start failed; task preserved', task_id=folder.name)
    return response('queued', task_id=folder.name, revision=number, correction_round=revision.get('correction_round',0), worker_pid=pid, recovery=recovered, toolkit_release=TOOLKIT_RELEASE, toolkit_build=TOOLKIT_BUILD)


def _spec_paths(specification_files):
    """Validate explicit local document inputs, not hidden configs or credentials."""
    if specification_files is None: return []
    if not isinstance(specification_files,list):
        raise ValueError('specification_files must be a list of workspace-relative document paths')
    from lead_workspace_ops import SECRET_NAMES, SECRET_SUFFIXES
    root=ROOT.resolve(); paths=[]; seen=set()
    for item in specification_files:
        if not isinstance(item,str) or not item.strip() or '\0' in item:
            raise ValueError('Invalid specification document path')
        rel=Path(item)
        if (rel.is_absolute() or '..' in rel.parts or any(part.startswith('.') for part in rel.parts)
                or rel.name.lower() in SECRET_NAMES or rel.suffix.lower() in SECRET_SUFFIXES
                or rel.suffix.lower() not in ('.md','.txt','.rst','.pdf','.json','.yaml','.yml','.csv')):
            raise ValueError('Specifications must be ordinary workspace documents, not hidden or sensitive configuration files')
        if any((root.joinpath(*rel.parts[:i])).is_symlink() for i in range(1,len(rel.parts)+1)):
            raise ValueError('Symbolic-link specification paths are not supported')
        path=(root/rel).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Specification document must be a regular file inside the workspace')
        if path not in seen:
            paths.append(path);seen.add(path)
    return paths


def _snapshot_specs(folder, paths, revision=0):
    """Stable, task-local copies; only paths and hashes enter the model prompt."""
    if not paths:return []
    root=ROOT.resolve()
    destination=folder/'specifications'/f'revision-{revision}'
    if not destination.resolve().is_relative_to(root) or any(
            (folder/part).is_symlink() for part in ('specifications',f'specifications/revision-{revision}')):
        raise ValueError('Unsafe task specification directory')
    destination.mkdir(parents=True,exist_ok=False)
    records=[]
    for number,source in enumerate(paths,1):
        target=destination/f'{number:02d}-{source.name}'
        digest=hashlib.sha256()
        with source.open('rb') as stream, target.open('xb') as copy:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                digest.update(chunk);copy.write(chunk)
        records.append({'original_path':source.relative_to(root).as_posix(),
                        'snapshot_path':target.relative_to(root).as_posix(),
                        'sha256':digest.hexdigest(),'bytes':target.stat().st_size})
    return records


def delegate_to_builder(goal, acceptance_criteria=None, risk='normal', specification_files=None, *, plan_id=None, item_id=None, preflight=None):
    if not _authorized_delegate():return response('blocked',reason='Only the enrolled Engineering Lead MCP may delegate')
    import eng
    try:eng.require_setup_complete()
    except SystemExit as exc:return response('blocked',reason=str(exc))
    cfg=eng.load_config(); prefs=cfg.get('preferences',{})
    builder=prefs.get('builder'); reviewer=prefs.get('reviewer')
    if builder not in ('codex','claude') or reviewer not in ('codex','claude'):
        return response('blocked',reason='No configured Lead/Builder roles')
    if not eng.SESSIONS.get(reviewer,'reviewer'):
        return response('blocked',reason='No enrolled Lead session; open ./dev')
    if risk != 'normal':
        return response('blocked',reason='High-risk change: obtain explicit developer authorization before proceeding')
    if not isinstance(goal,str) or not goal.strip():
        return response('blocked',reason='A concrete local implementation goal is required')
    if acceptance_criteria is not None and (not isinstance(acceptance_criteria,list) or
            not all(isinstance(a,str) and a.strip() for a in acceptance_criteria)):
        return response('blocked',reason='Acceptance criteria must be a list of nonempty strings')
    # Task content is descriptive, not an execution command. The declared risk
    # and local implementation contract govern delegation; mentioning production
    # readiness or credentials in a requirement is not a production operation.
    try:spec_paths=_spec_paths(specification_files)
    except (OSError,ValueError) as exc:return response('blocked',reason=str(exc))
    if orch.active_builders(ROOT):return response('blocked',reason='One Builder at a time in this shared workspace. Continue independent Lead work.',active_builder_tasks=orch.active_builders(ROOT))
    plan=item=None
    if plan_id is not None:
        try:plan,item=orch.select_item(ROOT,plan_id,item_id,'builder','implement')
        except ValueError as exc:return response('blocked',reason=str(exc))
        if not isinstance(preflight,str) or not preflight.strip():return response('blocked',reason='Lead must inspect existing contracts and record concrete preflight findings before handoff')
        # Never silently omit planned criteria when providing a more precise brief.
        acceptance_criteria=list(dict.fromkeys(item['acceptance_criteria']+(acceptance_criteria or [])))
        try:orch._scope_conflicts(ROOT,item['paths'])
        except ValueError as exc:return response('blocked',reason=str(exc))
    with redirect_stdout(sys.stderr):
        task_id=eng.new_task(goal.strip(),accept=acceptance_criteria or None)
        folder=eng.run_dir(task_id)
        try:
            snapshots=_snapshot_specs(folder,spec_paths)
            task=eng.load_json(folder/'task.json')
            if snapshots:task['specifications']=snapshots
            if item:task.update(owner='builder',work_plan_id=plan_id,work_item_id=item_id,planned_goal=item['goal'],write_scope=item['paths'],preflight=preflight)
            eng.write_json(folder/'task.json',task)
        except (OSError,ValueError) as exc:
            eng.save_state(folder,'blocked',reason='Cannot snapshot specifications: '+str(exc))
            return response('blocked',task_id=task_id,reason='Cannot snapshot specification documents')
        _mark_builder_queued(folder)
        if item:orch.bind(ROOT,plan,item,task_id)
    pid = _launch_builder(folder)
    if pid is None:
        return response('blocked',task_id=task_id,reason='Could not start the Builder worker; resume same task after diagnosing environment')
    return response('queued',task_id=task_id,worker_pid=pid,
                    toolkit_release=TOOLKIT_RELEASE,toolkit_build=TOOLKIT_BUILD,next_step='Perform independent ready Lead inspections and disjoint test setup now; call wait_for_builder with this task ID exactly once; never use a shell watcher or end the turn before collecting the outcome. Review then verify this slice.',work_plan=orch.plan_status(ROOT,plan_id) if plan_id else None)


def request_builder_revision(task_id, feedback, specification_files=None, risk='normal', *, failure_kind=None, root_cause=None, reproduction=None):
    """Only the enrolled Lead can request another local editing pass."""
    if not _authorized_delegate():
        return response('blocked',reason='Only enrolled Lead may request Builder corrections')
    if not isinstance(task_id,str) or not re.fullmatch(r'[a-z0-9-]{8,120}',task_id):
        return response('blocked',reason='Invalid task ID')
    if risk!='normal':
        return response('blocked',reason='High-risk changes are outside the automatic local editing workflow')
    if not isinstance(feedback,str) or not feedback.strip():
        return response('blocked',reason='Specify concrete local correction instructions')
    try:spec_paths=_spec_paths(specification_files)
    except (OSError,ValueError) as exc:return response('blocked',reason=str(exc))
    folder=project_state(ROOT)/'runs'/task_id
    if folder.is_symlink() or not (folder/'task.json').is_file():
        return response('blocked',reason='Unknown task')
    import runtime_recovery
    runtime_recovery.recover(ROOT)
    try:
        state=json.loads((folder/'state.json').read_text(encoding='utf8'))
        previous=json.loads((folder/'revision-request.json').read_text(encoding='utf8')) if (folder/'revision-request.json').is_file() else {}
    except (OSError,ValueError):
        return response('blocked',reason='Missing or invalid task records')
    if state.get('stage') in ('queued_for_builder','implementing','repairing','verifying'):
        return response('blocked',reason='Builder already running')
    if state.get('stage') in ('lead_takeover_required','lead_implementing') or orch.read_record(folder/'takeover.json'):
        return response('blocked',reason='Lead owns this correction now; use lead_edit_files and complete_lead_implementation, then verify')
    if orch.active_builders(ROOT):return response('blocked',reason='Another Builder is active in this shared workspace; continue Lead work')
    if not (folder/'build.json').is_file():
        if state.get('stage')!='blocked' or not (folder/'worker-finished.json').is_file():
            return response('blocked',reason='No prior completed or interrupted Builder attempt')
        # Older workers may have exited on quota before writing structured output.
        # Retain original task and files; retry uses the same persistent session.
        from live_builder_worker import changed_paths
        with redirect_stdout(sys.stderr):__import__('eng').write_json(folder/'build.json',{'status':'blocked','changed_files':changed_paths(ROOT,folder,{}),'interrupted':True,'questions':[]})
    if state.get('lead_verified'):
        return response('blocked',reason='Task already verified; start a new task for additional changes')
    if orch.read_record(folder/'build.json').get('scope_review_required'):
        return response('blocked',reason='Reconcile observed/reported scope changes before revising; use reconcile_builder_scope')
    try:orch.validate_task_scope(ROOT,folder)
    except ValueError as exc:return response('blocked',reason=str(exc))
    import eng
    number=int(previous.get('number',0))+1
    correction=orch.correction_round(previous,state)
    if correction>2:
        orch.put(folder/'takeover.json',{'reason':'Two unsuccessful Builder correction rounds; Lead must repair directly','previous_revision':previous.get('number'),'root_cause':root_cause,'reproduction':reproduction,'created_at':time.time()})
        with redirect_stdout(sys.stderr):eng.save_state(folder,'lead_takeover_required',revision=previous.get('number',0),verification_owner='reviewer',correction_rounds=2,reason='Two corrections exhausted; Lead investigates and fixes directly')
        return response('lead_takeover_required',task_id=task_id,reason='Two unsuccessful Builder correction rounds. Inspect root cause, fix locally using lead_edit_files, then complete_lead_implementation and rerun affected independent checks. Do not start a new Builder task to bypass this.')
    revision={'number':number,'feedback':feedback.strip(),'requested_at':time.time(),'correction_round':correction}
    if failure_kind:revision.update(failure_kind=failure_kind,root_cause=root_cause,reproduction=reproduction)

    try:
        snapshots=_snapshot_specs(folder,spec_paths,revision=number)
        if snapshots:revision['specifications']=snapshots
    except (OSError,ValueError) as exc:
        return response('blocked',reason='Cannot snapshot revision specifications: '+str(exc))
    with redirect_stdout(sys.stderr):
        if previous:orch.put(folder/f"revision-request-{previous.get('number',0)}.json",previous)
        eng.write_json(folder/'revision-request.json',revision)
        _mark_builder_queued(folder,revision=number,verification_owner='reviewer')
    pid = _launch_builder(folder, revision=number)
    if pid is None:
        return response('blocked',task_id=task_id,reason='Could not start correction worker; same task preserved')
    return response('queued',task_id=task_id,revision=number,worker_pid=pid,
                    toolkit_release=TOOLKIT_RELEASE,toolkit_build=TOOLKIT_BUILD,correction_round=correction,next_step='Complete independent Lead work; call wait_for_builder with this task ID, not Bash or sleep; collect the outcome, independently review and verify the focused correction')


def _executed_backend(result):
    if result.get('status')!='pass':return result
    out=result.get('evidence',{})
    text=out.get('stdout_tail','')+'\n'+out.get('stderr_tail','')
    counts=re.findall(r'Tests run:\s*(\d+)(?:, Failures:\s*\d+, Errors:\s*\d+, Skipped:\s*(\d+))?',text)
    if not any(int(n)>int(skipped or 0) for n,skipped in counts):
        return {**result,'status':'blocked','reason':'No executed non-skipped JUnit tests observed'}
    return result


def _backend_evidence(folder,project,test_filter,*,allow_run=True):
    import acceptance_audit,evidence_efficiency
    current=acceptance_audit.current(folder)
    candidates=[e for e in current if e.get('kind')=='backend' and (e.get('parameters') or {}).get('project')==project]
    if candidates:
        last=candidates[-1]
        # Full-suite evidence covers a requested subset, not the reverse.
        selector=(last.get('parameters') or {}).get('test_filter')
        if last.get('result')=='pass' and (selector is None or selector==test_filter):
            return last['evidence'],'recorded_task_acceptance'
    if not allow_run:return None,'missing'
    before=acceptance_audit.revision_fingerprint(folder)
    env_digest=evidence_efficiency.environment_fingerprint(ROOT,project)
    workspace_before=None
    try:
        import eng,workspace_git
        workspace_before=workspace_git.fingerprint(ROOT,eng.load_config().get('project'),review_documents=acceptance_audit.declared_review_documents(folder))
    except (OSError,ValueError,SystemExit):pass
    if before and workspace_before and env_digest:
        previous=evidence_efficiency.matching_backend_evidence(ROOT,folder,project,test_filter,workspace_before,env_digest)
        if previous:
            source,entry=previous
            acceptance_audit.record(folder,'backend',entry['evidence'],
                parameters={'project':project,'test_filter':test_filter,'workspace_fingerprint':workspace_before,
                            'environment_fingerprint':env_digest},
                reused_from={'task_id':source.name,'check_id':entry['id']})
            return entry['evidence'],'reused_verified_backend_evidence'
    result=_executed_backend(run_backend_tests(project=project,goal='test',test_filter=test_filter))
    if not before or acceptance_audit.revision_fingerprint(folder)!=before:
        result={**result,'status':'blocked','reason':'Cannot bind the test to unchanged task files; re-check current revision'}
    params={'project':project or result.get('project'),'test_filter':test_filter}
    if env_digest:params['environment_fingerprint']=env_digest
    if workspace_before:
        try:
            if workspace_git.fingerprint(ROOT,eng.load_config().get('project'),review_documents=acceptance_audit.declared_review_documents(folder))==workspace_before:params['workspace_fingerprint']=workspace_before
        except (OSError,ValueError,SystemExit):pass
    acceptance_audit.record(folder,'backend',result,parameters=params)
    return result,'fresh_targeted_backend_tests'


def _checkpoint_current(folder):
    task=orch.read_record(folder/'task.json')
    if not task.get('checkpoint_acceptance'):return True
    report=orch.plan_status(ROOT,task['work_plan_id'])
    return report.get('checkpoint_task_id')==folder.name and report.get('all_items_resolved') is True

def _requires_integrated_checkpoint(folder):
    task=orch.read_record(folder/'task.json')
    if not task.get('work_plan_id') or task.get('checkpoint_acceptance'):return False
    return len(orch.get_plan(ROOT,task['work_plan_id'])['items'])>1

def _acceptance_state(folder,task_id,record):
    import eng,task_runtime
    coverage=task_acceptance_report(task_id)
    assessment=coverage.get('acceptance',{})
    complete=record.get('outcome',{}).get('status')=='pass' and assessment.get('status')=='pass'
    prior=task_runtime.read_record(folder/'state.json')
    # Never modify historical records solely because the toolkit was upgraded.
    # Explicit new verification records, however, must obey the new gate policy.
    status='pass' if complete else 'fail' if assessment.get('status')=='fail' or record.get('outcome',{}).get('status')=='fail' else 'blocked'
    stage='ready_for_human_release_review' if complete else 'awaiting_acceptance'
    with redirect_stdout(sys.stderr):
        eng.write_json(folder/'lead-backend-verification.json',record)
        eng.save_state(folder,stage,lead_verified=complete,reviewer='lead',verification='lead-backend-verification.json',revision=prior.get('revision',0),acceptance=assessment,acceptance_policy=38 if code_quality.required(folder) else 36)
    return status,stage,assessment


def verify_delegated_backend_task(task_id, project, test_filter, reviewed=False, review_notes='', reviewed_files=None, documentation_reviews=None, documentation_gaps=None):
    """Bind a real Lead-side test execution to an existing implementation task.

    The Lead must have inspected code independently. No arbitrary shell or
    human-supplied PASS claims; runner output determines verification status.
    """
    if not _authorized_delegate():
        return response('blocked', reason='Lead MCP authorization missing')
    if not isinstance(task_id,str) or not re.fullmatch(r'[a-z0-9-]{8,120}',task_id):
        return response('blocked',reason='Invalid task ID')
    folder=project_state(ROOT)/'runs'/task_id
    if folder.is_symlink() or not (folder/'task.json').is_file():
        return response('blocked',reason='Unknown task')
    if reviewed is not True or not isinstance(review_notes,str) or not review_notes.strip():
        return response('blocked',reason='Independent code review and meaningful review_notes required')
    try:
        build=_sync_lead_build(folder)
        state=json.loads((folder/'state.json').read_text(encoding='utf8'))
    except (OSError,ValueError):
        return response('blocked',reason='Missing Builder execution evidence')
    if build.get('scope_review_required'):return response('blocked',reason='Reconcile pending scope observations before verification')
    # A Builder may write valid source, then report 'blocked' solely because
    # its sandbox cannot download Maven dependencies. Permit Lead-side
    # independent recovery only with an explicit list of inspected real files.
    if build.get('status')!='done':
        if build.get('interrupted') or state.get('resumable'):
            return response('blocked',reason='Interrupted Builder must finish the same task before acceptance; request_builder_revision')
        if build.get('status')!='blocked' or not isinstance(reviewed_files,list) or not reviewed_files:
            return response('blocked',reason='Builder has not completed implementation; supply reviewed_files for a recoverable blocked build')
        for rel in reviewed_files:
            if not isinstance(rel,str) or not rel.strip() or len(rel)>350:
                return response('blocked',reason='Invalid reviewed file path')
            candidate=(ROOT/rel).resolve()
            if not candidate.is_relative_to(ROOT.resolve()) or not candidate.is_file() or (ROOT/rel).is_symlink():
                return response('blocked',reason='Reviewed implementation file unavailable or outside project')
    elif build.get('questions'):
        return response('blocked',reason='Builder implementation remains ambiguous')
    if state.get('stage') in ('queued_for_builder','implementing','repairing','verifying','lead_implementing','lead_takeover_required') or orch.active_builders(ROOT):
        return response('blocked',reason='Implementation is not finished')
    if not _checkpoint_current(folder):return response('blocked',reason='Checkpoint has new or unfinished items; prepare the current integrated acceptance task')
    if _requires_integrated_checkpoint(folder):return response('blocked',reason='Use run_task_acceptance_check and review_work_slice for focused slice checks, then prepare_checkpoint_acceptance for integrated finalization')
    # A recovered old blocked implementation must supply actual paths for risk planning.
    if reviewed_files and not build.get('changed_files'):
        build={**build,'changed_files':reviewed_files}
        with redirect_stdout(sys.stderr):__import__('eng').write_json(folder/'build.json',build)
    try:docs= documentation_review.record(folder,documentation_reviews,documentation_gaps)
    except (ValueError,OSError) as exc:return response('blocked',reason=str(exc))
    if docs['status']=='incomplete':return response('blocked',reason='Review final documentation and resolve gaps before acceptance',documentation_review=docs)
    quality=code_quality.report(folder)
    if quality['status']=='incomplete':return response('blocked',reason='Complete current source code review before acceptance',code_review=quality)
    result,evidence_source=_backend_evidence(folder,project,test_filter)
    record={'task_id':task_id,'verified_at':time.time(),'project':project,'test_filter':test_filter,
            'review_notes':review_notes.strip(),'reviewed_files':reviewed_files or build.get('changed_files',[]),
            'outcome':result,'evidence_source':evidence_source}
    status,stage,coverage=_acceptance_state(folder,task_id,record)
    return response(status,task_id=task_id,task_stage=stage,backend_test_status=result.get('status'),
                    evidence=result,evidence_path=str((folder/'lead-backend-verification.json').relative_to(ROOT)),
                    acceptance=coverage,reason='Required acceptance incomplete; run missing checks and finalize without rerunning current passes' if status=='blocked' else '')


def git_inspect(repository=None,diff=False,task_id=None):
    if not _authorized_delegate():
        return response('blocked',reason='Lead MCP authorization missing')
    import lead_git,eng
    try:cfg=eng.load_config()
    except SystemExit:return response('blocked',reason='Workspace configuration missing')
    return lead_git.inspect(ROOT,cfg.get('project'),repository=repository,diff=diff is True,task_id=task_id)


def git_commit(repository,paths,message,explicit_user_request=False,requested_by_user=''):
    if not _authorized_delegate():
        return response('blocked',reason='Lead MCP authorization missing')
    import lead_git,eng
    try:cfg=eng.load_config()
    except SystemExit:return response('blocked',reason='Workspace configuration missing')
    return lead_git.commit(ROOT,cfg.get('project'),repository=repository,paths=paths,
                           message=message,explicit_user_request=explicit_user_request,
                           requested_by_user=requested_by_user)


def finalize_delegated_task(task_id,reviewed=False,review_notes='',deleted_paths=None,
                            project=None,test_filter=None,supersedes_task_id=None,documentation_reviews=None,documentation_gaps=None):
    """Record actual Lead review for a deletion or other completed Builder task.

    Reuses only task-linked, post-Builder passing verification or runs a focused
    backend test. A prior task may be superseded but its evidence remains intact.
    """
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(task_id)
    if not folder:return response('blocked',reason='Unknown task')
    if reviewed is not True or not isinstance(review_notes,str) or not review_notes.strip():
        return response('blocked',reason='Explicit independent review and factual review_notes required')
    import task_runtime,acceptance_audit,eng
    build=_sync_lead_build(folder)
    state=task_runtime.read_record(folder/'state.json')
    if build.get('scope_review_required'):return response('blocked',reason='Reconcile pending scope observations before finalization')
    if state.get('stage')=='ready_for_human_release_review' and state.get('lead_verified') and state.get('acceptance_policy') in (35,36,38):
        coverage=task_acceptance_report(task_id)
        if coverage.get('acceptance',{}).get('status')=='pass' and documentation_reviews is None and documentation_gaps is None:
            return response('ready',task_id=task_id,task_stage=state['stage'],note='Current required gates already verified; no tests rerun')
    if state.get('stage') in task_runtime.RUNNING or state.get('stage') in ('lead_implementing','lead_takeover_required') or orch.active_builders(ROOT):
        return response('blocked',reason='Implementation not finished; complete implementation first')
    if not _checkpoint_current(folder):return response('blocked',reason='Checkpoint has new or unfinished items; prepare the current integrated acceptance task')
    if _requires_integrated_checkpoint(folder):return response('blocked',reason='Use review_work_slice for this slice; finalize the integrated checkpoint after all items')
    if build.get('status')!='done' or build.get('questions'):
        return response('blocked',reason='Builder has no completed unambiguous implementation')
    reported=build.get('changed_files')
    if not isinstance(reported,list) or not reported:
        return response('blocked',reason='Builder reported no changed files to review')
    deleted_paths=deleted_paths or []
    if not isinstance(deleted_paths,list):
        return response('blocked',reason='Invalid deleted_paths list')
    for item in deleted_paths:
        if not isinstance(item,str) or item not in reported:
            return response('blocked',reason='Each deleted path must occur in the Builder change report')
        path=Path(item)
        resolved=(ROOT/path).resolve()
        if path.is_absolute() or '..' in path.parts or not resolved.is_relative_to(ROOT.resolve()):
            return response('blocked',reason='Deletion path outside workspace')
        if (ROOT/path).exists() or (ROOT/path).is_symlink():
            return response('blocked',reason=f'Deleted path still exists: {item}')
    if len(set(deleted_paths)) != len(deleted_paths):
        return response('blocked',reason='Duplicate deleted paths')
    try:docs= documentation_review.record(folder,documentation_reviews,documentation_gaps)
    except (ValueError,OSError) as exc:return response('blocked',reason=str(exc))
    if docs['status']=='incomplete':return response('blocked',reason='Review final documentation and resolve gaps before acceptance',documentation_review=docs)
    quality=code_quality.report(folder)
    if quality['status']=='incomplete':return response('blocked',reason='Complete current source code review before acceptance',code_review=quality)
    # A deletion-only task may legitimately leave no source file to inspect.
    # Review notes assert the Lead inspected the remaining sources and paths.
    if project is not None and (not isinstance(project,str) or Path(project).is_absolute() or '..' in Path(project).parts):
        return response('blocked',reason='Project path must be workspace-relative')
    if test_filter is not None:
        result,evidence_source=_backend_evidence(folder,project,test_filter)
    else:
        result,evidence_source=None,'recorded_task_acceptance'
    coverage=task_acceptance_report(task_id)
    report=coverage.get('acceptance',{})
    if result is None:
        current=acceptance_audit.current(folder)
        result=next((e['evidence'] for e in reversed(current) if e.get('result')=='pass'),None)
    if result is None:
        return response('blocked',reason='No current passing task-linked check; run required acceptance gates first',acceptance=report)
    record={'task_id':task_id,'verified_at':time.time(),'project':project,
            'test_filter':test_filter,'review_notes':review_notes.strip(),
            'reviewed_files':reported,'confirmed_deleted_paths':deleted_paths,
            'evidence_source':evidence_source,'outcome':result}
    status,stage,report=_acceptance_state(folder,task_id,record)
    if status!='pass':
        return response(status,task_id=task_id,task_stage=stage,acceptance=report,
                        reason='Required acceptance gates not all passed; run missing or failed gates and finalize again',
                        verification_source=evidence_source)
    superseded=None
    if supersedes_task_id is not None:
        prior=_task_folder(supersedes_task_id)
        if not prior or prior==folder:
            return response('pass',task_id=task_id,task_stage='ready_for_human_release_review',
                            note='Verified, but prior task not identified; no supersession applied')
        old_build=task_runtime.read_record(prior/'build.json')
        old_paths=old_build.get('changed_files',[])
        old_state=task_runtime.read_record(prior/'state.json')
        if (isinstance(old_paths,list) and old_paths and set(old_paths)==set(deleted_paths)
                and old_state.get('stage')=='ready_for_human_release_review'):
            with redirect_stdout(sys.stderr):
                eng.save_state(prior,'superseded',lead_verified=True,superseded_by=task_id,
                               reason='Verified deletion task removed all files from this earlier task')
            superseded=supersedes_task_id
    coverage=task_acceptance_report(task_id)
    return response('pass',task_id=task_id,task_stage='ready_for_human_release_review',
                    verification_source=evidence_source,confirmed_deleted_paths=deleted_paths,
                    superseded_task=superseded,acceptance=coverage.get('acceptance',{}),
                    note='Independent review and all selected current-revision acceptance gates passed; human release approval remains separate')


def _task_folder(task_id):
    if not isinstance(task_id,str) or not re.fullmatch(r'[a-z0-9-]{8,120}',task_id):return None
    folder=project_state(ROOT)/'runs'/task_id
    if folder.is_symlink() or not (folder/'task.json').is_file():return None
    state=orch.read_record(folder/'state.json')
    worker=orch.read_record(folder/'worker.json')
    if state.get('stage') not in orch.RUNNING:
        orch.normalize_private_scope_state(ROOT,folder)
    return folder


def _retry_known_startup_failure(folder):
    """One safe retry for a pre-provider lease refusal, not an interrupted edit."""
    with orch.mutation_guard(ROOT):
        state = orch.read_record(folder/'state.json')
        reason = str(state.get('reason', ''))
        if state.get('stage') != 'blocked' or not state.get('resumable'):
            return None
        if state.get('blocker') != 'session_start_blocked' and not (
            state.get('blocker') == 'worker_interrupted' and
            'native chat is active; cannot resume that session concurrently' in reason
        ):
            return None
        rev = orch.read_record(folder/'revision-request.json').get('number', 0)
        if type(rev) is not int or rev < 0:
            return None
        marker = folder/f'native-start-retry-{rev}.json'
        if marker.exists():
            return None
        result = _resume_builder({'task_id':folder.name})
        if result.get('status') == 'queued':
            orch.put(marker, {'worker_pid':result['worker_pid'], 'retried_at':time.time()})
            return result
        return None


def wait_for_builder(task_id, timeout_seconds=BUILDER_WAIT_SECONDS, *, cancel_event=None, progress=None):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(task_id)
    if not folder:return response('blocked',reason='Unknown task')
    import task_runtime
    import runtime_recovery
    runtime_recovery.recover(ROOT)
    if cancel_event is None or not cancel_event.is_set():
        _retry_known_startup_failure(folder)
    # Explicit completion subscriptions are never refused based on plan labels.
    # Independent work is advertised through work_plan_status, not a hard
    # prerequisite: legacy/misclassified inspections can themselves be blocked.
    # This guarantees the safe escape path even if a plan item is misclassified.
    def trace(event, **details):
        try:
            with (folder/'wait-events.jsonl').open('a',encoding='utf8') as f:
                f.write(json.dumps({'event':event,'time':time.time(),'release':TOOLKIT_RELEASE,'build':TOOLKIT_BUILD,**details})+'\n')
        except OSError:pass
    import wait_guard
    lease = os.environ.get('GASTON_SESSION_LEASE')
    attempt = wait_guard.attach(folder, lease)
    trace('registered',requested_timeout=timeout_seconds,effective_timeout=BUILDER_WAIT_SECONDS,attempt=attempt)
    started=time.monotonic()
    waited=task_runtime.wait_worker(folder,timeout_seconds=BUILDER_WAIT_SECONDS,cancel_event=cancel_event,progress=progress)
    if not _authorized_delegate():return response('blocked',reason='Lead ownership changed during wait; no settlement performed')
    if waited.get('builder_finished'):
        recovered = runtime_recovery.recover(ROOT)
        if any(row['task_id'] == task_id for row in recovered):
            waited.update(stage='blocked',status='blocked',resumable=True,next_action='resume_builder')
    remaining=int(BUILDER_WAIT_SECONDS-(time.monotonic()-started))
    if waited.get('status')=='blocked' and remaining>0 and (cancel_event is None or not cancel_event.is_set()):
        retried=_retry_known_startup_failure(folder)
        if retried:
            attempt = wait_guard.attach(folder, lease)
            trace('native_retry',worker_pid=retried['worker_pid'],attempt=attempt)
            waited=task_runtime.wait_worker(folder,timeout_seconds=remaining,cancel_event=cancel_event,progress=progress)
            if waited.get('builder_finished'):
                runtime_recovery.recover(ROOT)
    if waited.get('status')=='blocked':
        state=orch.read_record(folder/'state.json')
        pending=[x for x in runtime_recovery.incidents(ROOT) if x.get('task_id')==task_id and x.get('status')!='candidate_applied']
        if pending:waited.update(repair_incidents=pending,next_action='Investigate toolkit incident before retrying Builder')
        elif state.get('blocker')=='provider_quota':
            import recovery_consent
            recovery=recovery_consent.current(folder)
            waited.update(resumable=True,provider_retry_hint=state.get('provider_retry_hint'),recovery=recovery,
                          next_action='Ask developer whether to schedule one-time recovery. Do not schedule without explicit yes; confirm timezone. If declined preserve task for manual resume.' if not recovery or recovery.get('decision')=='pending' else 'Respect recorded recovery decision; do not schedule again.')
        elif state.get('resumable'):waited.update(resumable=True,next_action='resume_builder')
    wait_guard.finish(folder, lease, attempt, waited, cancelled=cancel_event is not None and cancel_event.is_set())
    trace('settled',status=waited.get('status'),builder_finished=waited.get('builder_finished'),elapsed=waited.get('elapsed_wait_seconds'),attempt=attempt)
    return {**waited,'task_id':task_id,'toolkit_release':TOOLKIT_RELEASE,'toolkit_build':TOOLKIT_BUILD,'metrics':task_runtime.task_metrics(folder)}


def get_task_metrics(task_id):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(task_id)
    if not folder:return response('blocked',reason='Unknown task')
    import task_runtime
    return response('ready',**task_runtime.task_metrics(folder))


def task_verification_plan(task_id):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(task_id)
    if not folder:return response('blocked',reason='Unknown task')
    import eng, verification_brief
    task=eng.load_json(folder/'task.json')
    build=_sync_lead_build(folder)
    if not build:return response('blocked',reason='No implementation file changes to plan from')
    result=verification_brief.produce(ROOT,task,build,eng.load_config())
    with redirect_stdout(sys.stderr):eng.write_json(folder/'verification-plan-lead.json',result)
    return response(result['status'],**{k:v for k,v in result.items() if k!='status'})


def run_task_acceptance_check(task_id,kind,project=None,test_filter=None,relative_path=None,force=False,
                              allow_network=False,explicit_developer_approval=False,approval_quote='',*,cancel_event=None):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(task_id)
    if not folder:return response('blocked',reason='Unknown task')
    import acceptance_audit,evidence_efficiency,task_runtime
    if kind not in acceptance_audit.GATE_TYPES:return response('blocked',reason='Unsupported acceptance check')
    if allow_network and kind not in ('backend','api'):
        return response('blocked',reason='Network approval applies only to backend dependency resolution')
    if allow_network and (explicit_developer_approval is not True or not isinstance(approval_quote,str) or not approval_quote.strip()):
        return response('blocked',reason='Online dependency access requires explicit developer approval for this exact check',
                        network_allowed=False,network_policy='offline',network_usage='not_used_by_build_tool',network_attempted=False,
                        network_scope='Maven/Gradle dependency resolution only; project build/test code is not OS-network-isolated')
    state=__import__('task_runtime').read_record(folder/'state.json')
    if state.get('stage') in ('queued_for_builder','implementing','repairing','verifying','lead_implementing','lead_takeover_required'):
        return response('blocked',blocker='workspace_not_idle',requires_idle_workspace=True,active_builder_tasks=orch.active_builders(ROOT),
                        reason='This acceptance task is still being implemented; collect completion first')
    task=task_runtime.read_record(folder/'task.json')
    if project is None and kind in ('api','backend'):
        import backend_runner
        options=backend_runner.discover(ROOT)
        if len(options)==1:project=options[0]['path']
    import verification_coordination as coordination
    if cancel_event is not None and cancel_event.is_set():return response('blocked',reason='Verification cancelled before execution')
    try:
        with coordination.reserve(ROOT,kind,project,integrated=bool(task.get('checkpoint_acceptance')),task_id=task_id) as reservation:
            token = _VERIFICATION_CANCEL.set(cancel_event)
            job_token = _VERIFICATION_JOB.set(reservation.get('windows_job'))
            try:
                return _run_reserved_acceptance(task_id,kind,project,test_filter,relative_path,force,folder,task,reservation,cancel_event,
                                                allow_network=allow_network,explicit_developer_approval=explicit_developer_approval,
                                                approval_quote=approval_quote)
            finally:
                _VERIFICATION_JOB.reset(job_token)
                _VERIFICATION_CANCEL.reset(token)
    except coordination.Busy as exc:
        return response('blocked',reason=str(exc),requires_idle_workspace=exc.fields['blocker']=='workspace_not_idle',**exc.fields)
    except (OSError,ValueError) as exc:return response('blocked',reason='Verification coordination: '+str(exc))


def _run_reserved_acceptance(task_id,kind,project,test_filter,relative_path,force,folder,current_task,reservation,cancel_event,
                             *,allow_network=False,explicit_developer_approval=False,approval_quote=''):
    import acceptance_audit,evidence_efficiency,task_runtime
    # Slice checks select concrete changed JUnit test classes automatically;
    # integrated acceptance retains full-suite verification.
    if kind=='backend' and test_filter is None and current_task.get('work_plan_id') and not current_task.get('checkpoint_acceptance'):
        test_filter=evidence_efficiency.focused_test_selector(ROOT,folder,project)
    before_inputs={}
    before=acceptance_audit.revision_fingerprint(folder,input_hashes=before_inputs)
    # Reuse only a current actual backend pass, same selector and explicit project,
    # with a verifiable unchanged Git workspace fingerprint. External/runtime
    # checks deliberately run fresh; never infer them from a previous smoke test.
    workspace_before=None
    env_digest=None
    if kind=='backend' and not reservation['parallel_with']:
        env_digest=evidence_efficiency.environment_fingerprint(ROOT,project)
        try:
            import eng,workspace_git
            workspace_before=workspace_git.fingerprint(ROOT,eng.load_config().get('project'),review_documents=acceptance_audit.declared_review_documents(folder))
        except (OSError,ValueError,SystemExit):pass
        if workspace_before and before and project is not None and not force:
            candidates=[e for e in acceptance_audit.current(folder) if e.get('kind')=='backend' and (e.get('parameters') or {}).get('project')==project and (e.get('parameters') or {}).get('test_filter')==test_filter]
            if candidates:
                last=candidates[-1]
                if (last.get('result')=='pass' and last.get('parameters',{}).get('workspace_fingerprint')==workspace_before
                        and (not env_digest or last.get('parameters',{}).get('environment_fingerprint')==env_digest)):
                    return response('pass',task_id=task_id,check_id=last['id'],kind=kind,evidence=last['evidence'],reused=True,note='Reused actual passing check on unchanged Git workspace and environment.')
            if env_digest and not current_task.get('checkpoint_acceptance'):
                previous=evidence_efficiency.matching_backend_evidence(ROOT,folder,project,test_filter,workspace_before,env_digest)
                if previous:
                    source,prior=previous
                    prior_parameters=prior.get('parameters') or {}
                    prior_evidence=prior.get('evidence') or {}
                    reused_parameters={
                        'project':project,'test_filter':test_filter,'relative_path':relative_path,
                        'workspace_fingerprint':workspace_before,'environment_fingerprint':env_digest,
                        'network_allowed':prior_parameters.get('network_allowed',prior_evidence.get('network_allowed')),
                        'network_policy':prior_parameters.get('network_policy',prior_evidence.get('network_policy','unknown')),
                        'network_usage':prior_parameters.get('network_usage',prior_evidence.get('network_usage','unknown')),
                        'network_attempted':prior_parameters.get('network_attempted',prior_evidence.get('network_attempted')),
                        'network_scope':prior_parameters.get('network_scope',prior_evidence.get('network_scope','unknown')),
                    }
                    if prior_parameters.get('network_approval_quote'):
                        reused_parameters['network_approval_quote']=prior_parameters['network_approval_quote']
                    entry=acceptance_audit.record(folder,'backend',prior['evidence'],parameters=reused_parameters,
                        reused_from={'task_id':source.name,'check_id':prior['id']})
                    return response('pass',task_id=task_id,check_id=entry['id'],kind=kind,
                                    evidence=prior['evidence'],reused=True,reused_from=entry['reused_from'],
                                    note='Identical real backend check reused; final integrated acceptance remains fresh.')
    if kind=='backend':result=_executed_backend(run_backend_tests(project=project,goal='test',test_filter=test_filter,
        allow_network=allow_network,explicit_developer_approval=explicit_developer_approval,approval_quote=approval_quote))
    elif kind=='api':
        if project is None:
            import backend_runner
            options=backend_runner.discover(ROOT)
            if len(options)==1:project=options[0]['path']
        if project is None:result=response('blocked',reason='Choose the backend project for managed live HTTP verification')
        else:
            import managed_backend
            result=managed_backend.run(ROOT,project,allow_network=allow_network,
                explicit_developer_approval=explicit_developer_approval,approval_quote=approval_quote)
    elif kind=='web':result=run_project_web_tests(project)
    elif kind=='web_verify':
        import web_acceptance
        result=web_acceptance.run_local(ROOT,project,execute=execute)
    elif kind in ('flutter_tests','flutter_analyze'):
        import workspace_checks
        _,discovered=workspace_checks.discover(ROOT,{})
        paths=discovered.get('flutter',[])
        if project is None and len(paths)==1:project=paths[0]
        if project not in paths:result=response('blocked',reason='Select a discovered Flutter application')
        else:
            evidence=execute(['flutter','test' if kind=='flutter_tests' else 'analyze'],cwd=ROOT/project,timeout=1200 if kind=='flutter_tests' else 600)
            result=response('pass' if evidence['exit_code']==0 else 'blocked' if evidence['exit_code'] is None else 'fail',project=project,evidence=evidence)
    else:
        result=run_project_mobile_tests(project, relative_path)
    params={'project':project or result.get('project'),'test_filter':test_filter,'relative_path':relative_path}
    if kind in ('backend','api'):
        params.update(network_allowed=bool(allow_network),
                      network_policy='approved_online' if allow_network else 'offline',
                      network_usage=result.get('network_usage','unknown'),
                      network_attempted=result.get('network_attempted'),
                      network_scope=result.get('network_scope','Maven/Gradle dependency resolution only; project test code is not OS-network-isolated'))
        if allow_network:params['network_approval_quote']=approval_quote
    if env_digest:params['environment_fingerprint']=env_digest
    if workspace_before:
        try:
            if workspace_git.fingerprint(ROOT,eng.load_config().get('project'),review_documents=acceptance_audit.declared_review_documents(folder))==workspace_before:params['workspace_fingerprint']=workspace_before
        except (OSError,ValueError,SystemExit):pass
    if reservation['fingerprints'] is not None:
        import verification_coordination as coordination
        params['verification_scope_fingerprints']=reservation['fingerprints']
        if coordination.fingerprints(ROOT,reservation['read_scope'])!=reservation['fingerprints']:
            result={**result,'status':'blocked','reason':'Verification source changed during execution; rerun on stable inputs'}
    if cancel_event is not None and cancel_event.is_set():result={**result,'status':'blocked','reason':'Client cancelled the verification subscription; no acceptance claim'}
    after_inputs={}
    after=acceptance_audit.revision_fingerprint(folder,input_hashes=after_inputs)
    if not _authorized_delegate() or not before or after!=before:
        result={**result,'status':'blocked','reason':'Cannot bind the check to unchanged task files and current Lead; re-check current revision'}
        if before and after and after!=before:
            result['changed_verification_inputs']=acceptance_audit.changed_inputs(before_inputs,after_inputs)
    params['verification_read_scope']=reservation['read_scope']
    entry=acceptance_audit.record(folder,kind,result,parameters=params)
    return response(result.get('status','blocked'),task_id=task_id,
                    check_id=entry['id'],kind=kind,evidence=result,parallel_with=reservation['parallel_with'],verification_read_scope=reservation['read_scope'],
                    note='Evidence recorded. Use task_acceptance_report for cross-component gaps; only Lead may decide acceptance.')


def _sync_lead_build(folder):
    prior=orch.read_record(folder/'build.json')
    if prior:
        merged=orch.merge_lead_paths(folder,prior)
        if not orch.lead_paths(folder) and 'lead_changed_files' not in prior:
            merged.pop('lead_changed_files',None)
        if merged!=prior and not any(x['task_id']==folder.name for x in orch.active_builders(ROOT)):
            orch.put(folder/'build.json',merged)
    return orch.read_record(folder/'build.json')

def acceptance_evidence_detail(task_id,check_id,include_logs=False):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(task_id)
    if not folder:return response('blocked',reason='Unknown task')
    import acceptance_audit,evidence_efficiency
    matches=[row for row in acceptance_audit.read(folder) if row.get('id')==check_id]
    if len(matches)!=1:return response('blocked',reason='Unknown acceptance evidence ID')
    entry=matches[0]
    result={'status':'ready','task_id':task_id,'check_id':check_id,'kind':entry.get('kind'),
            'recorded_at':entry.get('recorded_at'),'result':entry.get('result'),
            'parameters':entry.get('parameters'), 'reused_from':entry.get('reused_from'),
            'evidence':evidence_efficiency.compact_outcome(entry.get('evidence',{}),failure=entry.get('result')!='pass')}
    if include_logs:
        rel=(entry.get('evidence') or {}).get('log_path')
        if isinstance(rel,str) and rel.startswith(project_state_name()+'/local/backend-logs/'):
            path=(ROOT/rel).resolve()
            allowed=(project_state(ROOT)/'local/backend-logs').resolve()
            if path.is_relative_to(allowed) and path.is_file() and not path.is_symlink():
                import evidence_efficiency
                result['log_excerpt']=evidence_efficiency._safe_diagnostic_excerpt(path.read_text(encoding='utf8',errors='replace')[-8000:])
        else:
            result['log_excerpt']='Full log unavailable for this check'
    return result


def task_acceptance_report(task_id):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(task_id)
    if not folder:return response('blocked',reason='Unknown task')
    import task_runtime,acceptance_audit,eng,verification_brief
    task=task_runtime.read_record(folder/'task.json')
    build=_sync_lead_build(folder)
    if not build:return response('blocked',reason='No implementation output yet')
    try:cfg=eng.load_config()
    except SystemExit:
        return response('blocked',reason='Workspace configuration unavailable; cannot infer cross-component coverage')
    plan=verification_brief.produce(ROOT,task,build,cfg)
    with redirect_stdout(sys.stderr):eng.write_json(folder/'verification-plan-lead.json',plan)
    results=acceptance_audit.current(folder)
    summary=acceptance_audit.summarize(plan,results)
    expired=acceptance_audit.expired_checks(folder,results)
    if expired:summary['expired_checks']=expired
    docs=documentation_review.report(folder)
    summary['documentation_review']=docs
    quality=code_quality.report(folder)
    summary['code_review']=quality
    if (docs['status']=='incomplete' or quality['status']=='incomplete') and summary['status']!='fail':
        summary.update(status='incomplete',coverage='partial')
    if build.get('scope_review_required'):summary.update(status='incomplete',coverage='partial',scope_review_required=build['scope_review_required'])
    if not _checkpoint_current(folder):summary.update(status='incomplete',coverage='partial',checkpoint_superseded_or_unfinished=True)
    return response('ready',task_id=task_id,acceptance=summary,recorded_checks=len(results),
                    lead_backend_verification=task_runtime.read_record(folder/'lead-backend-verification.json').get('outcome',{}).get('status'),toolkit_release=TOOLKIT_RELEASE,toolkit_build=TOOLKIT_BUILD,historical_verification_policy=task_runtime.read_record(folder/'state.json').get('acceptance_policy'))


def delegation_status(task_id):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    if not isinstance(task_id,str) or not re.fullmatch(r'[a-z0-9-]{8,120}',task_id):
        return response('blocked',reason='Invalid task ID')
    folder=project_state(ROOT)/'runs'/task_id
    if not (folder/'task.json').is_file():return response('blocked',reason='Unknown task')
    def read(name):
        try:return json.loads((folder/name).read_text(encoding='utf8'))
        except (OSError,ValueError):return {}
    state=read('state.json'); verification=read('verify.json'); build=read('build.json')
    import runtime_recovery
    if state.get('stage') in ('queued_for_builder','implementing','repairing','verifying'):
        runtime_recovery.recover(ROOT)
        state=read('state.json'); build=read('build.json')
    lead_verification=read('lead-backend-verification.json')
    currently_verified=False
    if state.get('lead_verified') and state.get('acceptance_policy') in (35,36,38) and state.get('stage')=='ready_for_human_release_review':
        currently_verified=task_acceptance_report(task_id).get('acceptance',{}).get('status')=='pass'
    return response('ready',task_id=task_id,stage=state.get('stage','queued_for_builder'),
                    summary=build.get('summary','')[:800],checks=verification.get('checks',[])[:24],
                    all_passed=currently_verified,
                    historical_lead_verified=bool(state.get('lead_verified')),toolkit_release=TOOLKIT_RELEASE,toolkit_build=TOOLKIT_BUILD,
                    reason=state.get('reason',''),
                    lead_must_review=not currently_verified,
                    blocker=state.get('blocker'),resumable=state.get('resumable',False),
                    scope_review_required=build.get('scope_review_required',[]),
                    lead_verification=lead_verification.get('outcome',{}).get('status'),
                    evidence_paths=[str(x.relative_to(ROOT)) for x in (folder/'task.json',folder/'SUMMARY.md',folder/'verify.json',folder/'lead-backend-verification.json',folder/'verification-plan-lead.json',folder/'acceptance-evidence.json',folder/'lead-documentation-review.json',folder/'wait-events.jsonl',folder/'context-brief.json') if x.exists()])


def _workspace_op(name, *args, **kwargs):
    if not _authorized_delegate():
        return response('blocked',reason='Lead MCP authorization missing')
    import lead_workspace_ops
    return getattr(lead_workspace_ops,name)(ROOT,*args,**kwargs)


def _orchestrate(action, args):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    if action in ('status','usage','inspect'):
        return _orchestration_action(action,args)
    try:
        with orch.mutation_guard(ROOT):
            if action in ('delegate','resume','revision','takeover','lead_start'):
                import runtime_recovery
                runtime_recovery.reconcile_tasks(ROOT)
            return _orchestration_action(action,args)
    except (ValueError,OSError,KeyError,TypeError) as exc:return response('blocked',reason=str(exc))

def _orchestration_action(action, args):
    try:
        if action=='resume':return _resume_builder(args)
        if action=='plan':return orch.create_plan(ROOT,args.get('goal'),args.get('items'))
        if action=='status':return orch.plan_status(ROOT,args.get('plan_id'))
        if action=='usage':
            import task_runtime
            report=orch.plan_status(ROOT,args.get('plan_id'))
            ids=list(dict.fromkeys([x['task_id'] for x in report['items'] if x.get('task_id')]+([report['checkpoint_task_id']] if report.get('checkpoint_task_id') else [])))
            data=[task_runtime.task_metrics(project_state(ROOT)/'runs'/tid) for tid in ids]
            return response('ready',plan_id=report['plan_id'],tasks=data,codex_usage_reported_tasks=sum(x['provider_usage']['tokens'] is not None for x in data),note='Only CLI-reported token counts. Unreported provider/Lead usage and savings remain unknown.')
        if action=='extend':return orch.extend_plan(ROOT,args.get('plan_id'),args.get('items'))
        if action=='defer':return orch.defer_until_idle(ROOT,args.get('plan_id'),args.get('item_id'),args.get('reason'))
        if action=='supersede':return orch.supersede_work_item(ROOT,args.get('plan_id'),args.get('item_id'),args.get('replacement_item_id'),args.get('reason'))
        if action=='inspect':return orch.inspect_files(ROOT,args.get('files'))
        if action=='record_inspection':return orch.inspection(ROOT,args.get('plan_id'),args.get('item_id'),args.get('evidence'),args.get('followup_items'))
        if action=='delegate':
            if args.get('risk','normal')!='normal':raise ValueError('Only authorized local low-risk operations may be delegated')
            goal=args.get('goal');criteria=args.get('acceptance_criteria');pid=args.get('plan_id');iid=args.get('item_id');preflight=args.get('preflight')
            if not isinstance(preflight,str) or not preflight.strip():raise ValueError('Perform Lead preflight first; record relevant code, decisions and test contract findings')
            if not pid:
                # One small implementation can be planned and dispatched in one call.
                item={'id':'implementation','owner':'builder','kind':'implement','goal':goal,'paths':args.get('paths'),'acceptance_criteria':criteria}
                pid=orch.create_plan(ROOT,goal,[item])['plan_id'];iid='implementation'
            if args.get('paths') is not None:
                plan,item=orch.select_item(ROOT,pid,iid,'builder','implement')
                additions=orch.normalize_scope(ROOT,args['paths'])
                orch.extend_item_scope(ROOT,plan,item,additions,reason='Explicit delegation paths: '+preflight)
            return delegate_to_builder(goal,criteria,args.get('risk','normal'),args.get('specification_files'),plan_id=pid,item_id=iid,preflight=preflight)
        if action=='revision':
            f=_task_folder(args.get('task_id'))
            if not f:raise ValueError('Unknown task')
            state=orch.read_record(f/'state.json')
            if state.get('blocker') not in ('provider_quota','worker_interrupted','worker_start_failed','session_start_blocked') and not (state.get('stage')=='needs_lead_decision' and state.get('clarification_only') is True and state.get('questions')):
                if args.get('failure_kind') not in ('implementation','architecture','specification','test_fixture','environment','regression') or not all(isinstance(args.get(k),str) and args[k].strip() for k in ('root_cause','reproduction')):
                    raise ValueError('Investigate actual failure first: failure_kind, root_cause and reproduction/evidence required; do not blindly retry')
                if args.get('failure_kind')=='environment':raise ValueError('Lead owns environment diagnosis and repair. Do not burn a Builder correction on environment failures.')
                if args.get('failure_kind')=='test_fixture':raise ValueError('Lead owns test fixtures. Repair them directly using lead_edit_files (test_setup) and rerun the affected check; delegate only actual application implementation defects.')
            return request_builder_revision(args.get('task_id'),args.get('feedback'),args.get('specification_files'),args.get('risk','normal'),failure_kind=args.get('failure_kind'),root_cause=args.get('root_cause'),reproduction=args.get('reproduction'))
        if action=='review_only_docs':return orch.declare_review_only_documents(ROOT,args.get('task_id'),args.get('paths'),args.get('reason'))
        if action=='extend_scope':return orch.extend_task_scope(ROOT,args.get('task_id'),args.get('paths'),args.get('reason'))
        if action=='resolve_questions':return orch.resolve_questions(ROOT,args.get('task_id'),args.get('answers'),args.get('implementation_complete',False),args.get('completion_evidence',''))
        if action=='reconcile_scope':return orch.reconcile_scope(ROOT,args.get('task_id'),args.get('decisions'))
        if action=='abandon':return orch.abandon_empty_lead_task(ROOT,args.get('task_id'),args.get('reason'))
        if action=='lead_start':return start_lead_implementation(args)
        if action=='takeover':return take_over_builder_task(args)
        if action=='lead_edit':return lead_edit_files(args)
        if action=='lead_finish':return complete_lead_implementation(args)
        if action=='slice_review':return review_work_slice(args)
        if action=='checkpoint':return prepare_checkpoint_acceptance(args)
    except (ValueError,OSError,KeyError,TypeError) as exc:return response('blocked',reason=str(exc))
    return response('blocked',reason='Unknown orchestration action')


def start_lead_implementation(args):
    import eng
    pid=args.get('plan_id');iid=args.get('item_id')
    if pid:
        plan,item=orch.select_item(ROOT,pid,iid,'lead','implement')
    else:
        item={'id':'lead-implementation','owner':'lead','kind':'implement','goal':args.get('goal'),'paths':args.get('paths'),'acceptance_criteria':args.get('acceptance_criteria')}
        pid=orch.create_plan(ROOT,args.get('goal'),[item])['plan_id'];iid='lead-implementation'
        plan,item=orch.select_item(ROOT,pid,iid,'lead','implement')
    orch._scope_conflicts(ROOT,item['paths'])
    with redirect_stdout(sys.stderr):
        tid=eng.new_task(item['goal'],accept=item['acceptance_criteria']);folder=eng.run_dir(tid)
        task=eng.load_json(folder/'task.json');task.update(owner='lead',work_plan_id=pid,work_item_id=iid,write_scope=item['paths']);eng.write_json(folder/'task.json',task)
        orch.put(folder/'build.json',{'status':'in_progress','changed_files':[],'questions':[],'implementation_owner':'lead'})
        eng.save_state(folder,'lead_implementing',verification_owner='reviewer');orch.bind(ROOT,plan,item,tid)
    return response('ready',task_id=tid,plan_id=pid,next_step='Lead implements the scoped files directly with lead_edit_files; then complete_lead_implementation and run verification')


def take_over_builder_task(args):
    """Choose a direct small fix earlier than the mandatory two-correction limit."""
    import eng
    folder=_task_folder(args.get('task_id'))
    if not folder:raise ValueError('Unknown task')
    state=orch.read_record(folder/'state.json');task=orch.read_record(folder/'task.json');build=orch.read_record(folder/'build.json')
    if build.get('scope_review_required'):raise ValueError('Reconcile pending scope observations before takeover')
    if task.get('owner')=='lead' or state.get('lead_verified') or state.get('stage')=='slice_reviewed':raise ValueError('Task already owned by Lead or accepted; open a new scoped remediation item if needed')
    if orch.active_builders(ROOT) or not build:raise ValueError('Wait for active Builder to settle; preserve its work')
    reason=args.get('reason')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('Record the inspected root cause and why this is a focused Lead repair')
    paths=orch.normalize_scope(ROOT,args.get('paths'))
    prior_scope=task.get('write_scope') or build.get('changed_files',[])
    orch.extend_task_scope(ROOT,folder.name,sorted(set(prior_scope)|set(paths)),reason)
    old=orch.read_record(folder/'takeover.json')
    orch.put(folder/'takeover.json',{**old,'reason':old.get('reason') or reason,'lead_fix_reason':reason,'approved_local_fix_paths':paths,'created_at':old.get('created_at',time.time())})
    with redirect_stdout(sys.stderr):eng.save_state(folder,'lead_takeover_required',revision=state.get('revision',0),verification_owner='reviewer',reason=reason)
    return response('ready',task_id=folder.name,next_step='Lead repairs the reported bug directly with lead_edit_files; complete and independently verify. Do not launch another Builder correction.')


def lead_edit_files(args):
    folder=_task_folder(args.get('task_id'))
    if not folder:raise ValueError('Unknown task')
    purpose=args.get('purpose');out=orch.apply_edits(ROOT,folder,args.get('edits'),purpose)
    task=orch.read_record(folder/'task.json');state=orch.read_record(folder/'state.json')
    # The worker merges the ledger at finish. Avoid overwriting its live result.
    if not any(x['task_id']==folder.name for x in orch.active_builders(ROOT)):
        prior=orch.read_record(folder/'build.json')
        orch.put(folder/'build.json',orch.merge_lead_paths(folder,prior))
    if state.get('stage')=='lead_takeover_required' or task.get('owner')=='lead' and state.get('stage')!='lead_implementing':
        with redirect_stdout(sys.stderr):__import__('eng').save_state(folder,'lead_implementing',takeover=state.get('stage')=='lead_takeover_required' or state.get('takeover',False),revision=state.get('revision',0),verification_owner='reviewer')
    return {**out,'task_id':folder.name}


def complete_lead_implementation(args):
    import eng
    folder=_task_folder(args.get('task_id'))
    if not folder:raise ValueError('Unknown task')
    task=orch.read_record(folder/'task.json');state=orch.read_record(folder/'state.json')
    if state.get('stage')!='lead_implementing' or not (task.get('owner')=='lead' or state.get('takeover')):raise ValueError('Not a Lead-owned implementation or takeover')
    if not orch.lead_paths(folder):raise ValueError('No recorded Lead edits; inspect and correct actual files first')
    if not isinstance(args.get('summary'),str) or not args['summary'].strip():raise ValueError('Describe completed implementation and self-review')
    if any(x['task_id']==folder.name for x in orch.active_builders(ROOT)):raise ValueError('Builder still active')
    prior=orch.read_record(folder/'build.json')
    if prior.get('scope_review_required'):raise ValueError('Reconcile pending scope observations before completing implementation')
    built=orch.merge_lead_paths(folder,{**prior,'status':'done','questions':[],'interrupted':False,'implementation_owner':'lead','summary':args['summary'],'takeover':bool(state.get('takeover'))})
    orch.put(folder/'build.json',built)
    with redirect_stdout(sys.stderr):eng.save_state(folder,'awaiting_lead_review',verification_owner='reviewer',revision=state.get('revision',0),takeover=bool(state.get('takeover')))
    return response('ready',task_id=folder.name,next_step='Review actual changes, run required current task checks, reconcile documents and finalize; no acceptance inferred from edits')


def review_work_slice(args):
    """Provisional slice closure: real targeted evidence, never release approval."""
    import eng,acceptance_audit
    f=_task_folder(args.get('task_id'))
    if not f:raise ValueError('Unknown task')
    task=orch.read_record(f/'task.json');state=orch.read_record(f/'state.json');build=_sync_lead_build(f)
    if not task.get('work_plan_id') or task.get('checkpoint_acceptance'):raise ValueError('Only planned implementation slices may be provisionally reviewed')
    if build.get('scope_review_required'):raise ValueError('Reconcile pending scope observations before slice review')
    if state.get('stage') in ('lead_implementing','lead_takeover_required') or orch.active_builders(ROOT) or build.get('status')!='done' or build.get('questions'):raise ValueError('Finish implementation before review')
    if not args.get('review_notes') or not str(args['review_notes']).strip():raise ValueError('Actual code review findings required')
    if not build.get('changed_files'):raise ValueError('No changed implementation files to review')
    doc= documentation_review.record(f,args.get('documentation_reviews'),args.get('documentation_gaps'))
    if doc['status']=='incomplete':return response('blocked',reason='Resolve documentation or material findings',documentation_review=doc)
    quality=code_quality.report(f)
    if quality['status']=='incomplete':return response('blocked',reason='Review current source clarity, difficult logic, maintainability and correctness; resolve findings',code_review=quality)
    evidence=acceptance_audit.current(f)
    plan=task_verification_plan(f.name)
    if plan.get('status') not in ('ready','coverage_gap'):return response('blocked',reason='No risk-based test plan')
    # Require real relevant tool evidence for each touched component. For a backend
    # slice the targeted JUnit check may precede later full HTTP checkpoint checks.
    checks=plan.get('selected_checks',[])
    needed={(c.get('cwd','.'),str(c.get('name','')).split('__')[0]) for c in checks if str(c.get('name','')).split('__')[0] in ('backend_verify','flutter_tests','flutter_analyze','web_e2e','web_verify','mobile_e2e')}
    latest={}
    for e in evidence:
        target=(e.get('parameters') or {}).get('project') or (e.get('evidence') or {}).get('project') or '.'
        latest[(target,e.get('gate'))]=e.get('result')
    if not needed or not needed<= {k for k,v in latest.items() if v=='pass'} or any(v=='fail' for v in latest.values()):return response('blocked',reason='Run relevant current targeted checks; resolve recorded failures before slice review',needed_checks=sorted(needed),observed=[{'project':k[0],'gate':k[1],'status':v} for k,v in latest.items()])
    orch.put(f/'slice-review.json',{'reviewed_at':time.time(),'review_notes':args['review_notes'],'evidence_ids':[e['id'] for e in evidence],'revision_fingerprint':acceptance_audit.revision_fingerprint(f),'release_verified':False})
    with redirect_stdout(sys.stderr):eng.save_state(f,'slice_reviewed',lead_verified=False,revision=state.get('revision',0))
    return response('pass',task_id=f.name,slice_only=True,release_verified=False,work_plan=orch.plan_status(ROOT,task['work_plan_id']),note='Provisional slice checked. Combined checkpoint acceptance must pass after all slices.')


def prepare_checkpoint_acceptance(args):
    import eng
    pid=args.get('plan_id');plan=orch.get_plan(ROOT,pid);report=orch.plan_status(ROOT,pid)
    if not report.get('all_items_resolved',report['all_items_done']):return response('blocked',reason='Complete every replacement Lead investigation and implementation slice first',work_plan=report)
    if orch.active_builders(ROOT):raise ValueError('No integrated acceptance while a Builder is active')
    if plan.get('checkpoint_task_id'):return response('ready',task_id=plan['checkpoint_task_id'],work_plan=report)
    ids=[x['task_id'] for x in report['items'] if x.get('task_id')]
    if not ids:return response('ready',work_plan=report,note='Inspection-only plan; no implementation or acceptance claim')
    if len(ids)==1 and len(report['items'])==1 and delegation_status(ids[0]).get('all_passed'):
        plan['checkpoint_task_id']=ids[0];orch.put(orch.plan_file(ROOT,pid),plan)
        return response('ready',task_id=ids[0],work_plan=orch.plan_status(ROOT,pid),note='One fully verified slice; reuse its current acceptance without rerun')
    paths=set()
    for tid in ids:paths.update(orch.read_record(project_state(ROOT)/'runs'/tid/'build.json').get('changed_files',[]))
    if not paths:raise ValueError('No recorded changed files for integrated verification')
    reviewed_docs=set()
    for source_id in ids:
        source_folder=project_state(ROOT)/'runs'/source_id
        reviewed_docs.update(orch.read_record(source_folder/'lead-documentation-review.json').get('documents',{}))
        reviewed_docs.update(x['original_path'] for x in orch.read_record(source_folder/'task.json').get('specifications',[]) if isinstance(x,dict) and isinstance(x.get('original_path'),str))
    reviewed_docs=set(documentation_review.required_paths(reviewed_docs))
    with redirect_stdout(sys.stderr):
        tid=eng.new_task('Checkpoint acceptance: '+plan['goal'],accept=[c for x in report['items'] for c in x.get('acceptance_criteria',[])])
        f=eng.run_dir(tid);task=eng.load_json(f/'task.json');task.update(owner='lead',work_plan_id=pid,checkpoint_acceptance=True,source_tasks=ids,write_scope=sorted(paths),required_documentation_paths=sorted(reviewed_docs));eng.write_json(f/'task.json',task)
        orch.put(f/'build.json',{'status':'done','changed_files':sorted(paths),'questions':[],'summary':'Integrated checkpoint acceptance across recorded slices; not a new Builder execution'})
        eng.save_state(f,'awaiting_lead_review',verification_owner='reviewer')
        plan['checkpoint_task_id']=tid;orch.put(orch.plan_file(ROOT,pid),plan)
    return response('ready',task_id=tid,work_plan=orch.plan_status(ROOT,pid),next_step='Run all current risk-based checkpoint gates, including live HTTP for service changes; review final code and documents; finalize only on complete evidence')



def _code_review(action,args):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(args.get('task_id'))
    if not folder:return response('blocked',reason='Unknown task')
    build=_sync_lead_build(folder)
    if action=='status':return response('ready',task_id=folder.name,review=code_quality.report(folder),changed_source_files=code_quality.source_paths(folder))
    if (build.get('status')!='done' or build.get('questions') or orch.active_builders(ROOT)):
        return response('blocked',reason='Finish implementation before recording source review')
    state=orch.read_record(folder/'state.json')
    if state.get('stage') in ('lead_implementing','lead_takeover_required'):
        return response('blocked',reason='Finish Lead implementation before recording source review')
    try:return response('ready',task_id=folder.name,review=code_quality.record(folder,args.get('reviews'),args.get('findings'),args.get('resolution','')))
    except (ValueError,OSError,TypeError) as exc:return response('blocked',reason=str(exc))


def _evolution(action,args):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    if central_mode() and action in ('stage','validate','status','queue','cancel','export'):
        return response('blocked',reason=CENTRAL_EVOLUTION_REASON)
    import evolution as evo
    import learning_engine as learning
    if action=='learning':return learning.collect(ROOT)
    if action=='lesson':return learning.lesson(ROOT,**args)
    if action=='health':return learning.health(ROOT,**args)
    if action=='stage':return evo.stage(ROOT,**args)
    if action=='validate':return evo.validate(ROOT,args.get('candidate_id'))
    if action=='status':return evo.status(ROOT)
    if action=='queue':return evo.queue(ROOT,args.get('candidate_id'),rollback=args.get('rollback',False))
    if action=='cancel':return evo.cancel(ROOT)
    if action=='export':return evo.export_toolkit(ROOT)
    raise ValueError('Unknown evolution action')

def _runtime_recovery_status():
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    import runtime_recovery
    return response('ready', recovered=runtime_recovery.recover(ROOT), incidents=runtime_recovery.incidents(ROOT))


def quota_decision_tool(args, *, job=False):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    folder=_task_folder(args.get('task_id'))
    if not folder:return response('blocked',reason='Unknown task')
    import recovery_consent
    try:
        with orch.mutation_guard(ROOT):
            if job:return response('blocked',reason='Native scheduling retired. Use quota_recovery_decision; no additional Lead or native job.')
            result=recovery_consent.decision(folder,args.get('approved'),user_request=args.get('user_request'),retry_at=args.get('retry_at'))
            if result.get('decision')=='approved':
                from quota_worker import schedule
                return schedule(ROOT,folder,result)
            return result
    except (ValueError,OSError) as exc:return response('blocked',reason=str(exc))


def lead_context_status():
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    from session_store import Store
    import token_intelligence as ti
    store=Store(ROOT);provider=store._configured_roles()[1];entry=store.get(provider,'reviewer')
    if not entry:return response('unavailable',context_tokens=None)
    import serena_tools
    return {**ti.lead_context(ROOT,provider,entry['id']),'serena':serena_tools.usage_summary(ROOT)}


def register_approved_project_context(args):
    import project_continuity as continuity
    with orch.mutation_guard(ROOT):
        return continuity.register_sources(ROOT,paths=args.get('paths'),
            developer_confirmed=args.get('developer_confirmed'),expected_sha256=args.get('expected_sha256'))


def record_approved_roadmap(args):
    import project_continuity as continuity
    with orch.mutation_guard(ROOT):
        return continuity.record(ROOT, milestones=args.get('milestones'),
            approved_decisions=args.get('approved_decisions'), next_milestone_id=args.get('next_milestone_id'),
            developer_confirmed=args.get('developer_confirmed'), expected_sha256=args.get('expected_sha256'))


def _standalone_verification(kind,project,run):
    import verification_coordination as coordination
    if project is None and kind=='backend':
        import backend_runner
        options=backend_runner.discover(ROOT)
        if len(options)==1:project=options[0]['path']
    try:
        with coordination.reserve(ROOT,kind,project) as reservation:
            result=run()
            if reservation['fingerprints'] is not None and coordination.fingerprints(ROOT,reservation['read_scope'])!=reservation['fingerprints']:
                return {**result,'status':'blocked','reason':'Verification source changed during execution'}
            return {**result,'parallel_with':reservation['parallel_with'],'verification_read_scope':reservation['read_scope']}
    except coordination.Busy as exc:return response('blocked',reason=str(exc),**exc.fields)
    except (OSError,ValueError) as exc:return response('blocked',reason='Verification coordination: '+str(exc))


def lead_checkpoint_tool(args):
    if not _authorized_delegate():return response('blocked',reason='Lead MCP authorization missing')
    from session_store import Store
    import token_intelligence as ti
    store=Store(ROOT);provider=store._configured_roles()[1];entry=store.get(provider,'reviewer')
    if not entry:return response('blocked',reason='No registered Lead')
    try:
        with orch.mutation_guard(ROOT):return ti.record_lead_checkpoint(ROOT,provider,entry['id'],**args)
    except (ValueError,OSError,TypeError) as exc:return response('blocked',reason=str(exc))


CALLS={
       'declare_review_only_documents':lambda args:_orchestrate('review_only_docs',args),
       'extend_task_scope':lambda args:_orchestrate('extend_scope',args),
       'resolve_builder_questions':lambda args:_orchestrate('resolve_questions',args),
       'reconcile_builder_scope':lambda args:_orchestrate('reconcile_scope',args),
       'abandon_empty_lead_task':lambda args:_orchestrate('abandon',args),
       'verification_reservation_status':lambda args:response('ready',active_verification=__import__('verification_coordination').active(ROOT)),
       'recover_verification_reservation':lambda args:__import__('verification_coordination').recover_reservation(ROOT,developer_confirmed=args.get('developer_confirmed')) if _authorized_delegate() else response('blocked',reason='Lead authorization missing'),
       'checkpoint_lead_context':lambda args:lead_checkpoint_tool(args),
       'project_roadmap_status':lambda args:__import__('project_continuity').status(ROOT),
       'register_project_context_sources':lambda args:register_approved_project_context(args),
       'record_project_roadmap':lambda args:record_approved_roadmap(args),
       'quota_recovery_decision':lambda args:quota_decision_tool(args),
       'record_quota_recovery_job':lambda args:quota_decision_tool(args,job=True),
       'lead_context_budget':lambda args:lead_context_status(),
       'runtime_recovery_status':lambda args:_runtime_recovery_status(),
       'resume_builder':lambda args:_orchestrate('resume',args),
       'task_code_review':lambda args:_code_review('status',args),
       'record_code_review':lambda args:_code_review('record',args),
       **{name:(lambda args, action=action:_evolution(action,args)) for name,action in {'learning_report':'learning','record_engineering_lesson':'lesson','code_health':'health','stage_evolution':'stage','validate_evolution':'validate','evolution_status':'status','queue_evolution':'queue','cancel_evolution':'cancel','export_toolkit':'export'}.items()},
       **{name:(lambda args, action=action:_orchestrate(action,args)) for name,action in {'plan_work':'plan','extend_work_plan':'extend','defer_work_item':'defer','supersede_work_item':'supersede','work_plan_status':'status','work_plan_usage':'usage','inspect_workspace_files':'inspect','record_lead_inspection':'record_inspection','start_lead_implementation':'lead_start','take_over_builder_task':'takeover','lead_edit_files':'lead_edit','complete_lead_implementation':'lead_finish','review_work_slice':'slice_review','prepare_checkpoint_acceptance':'checkpoint'}.items()},'workspace_overview':lambda args:_workspace_op('inspect',args.get('action','overview'),args.get('relative_path',''),args.get('pattern',''),args.get('max_results',80)),
       'workspace_diagnostics':lambda args:_workspace_op('diagnostics',args.get('action','toolchain'),args.get('project',''),args.get('ports')),
       'workspace_logs':lambda args:_workspace_op('logs',args.get('relative_path'),args.get('pattern',''),args.get('lines',120)),
       'workspace_processes':lambda args:_workspace_op('processes'),
       'workspace_preview':lambda args:_workspace_op('preview',args.get('action','status'),args.get('relative_path',''),args.get('port',0)),
       'workspace_housekeeping':lambda args:_workspace_op('housekeeping',args.get('action','inspect'),args.get('apply',False),args.get('requested_by_user','')),
       'workspace_note':lambda args:_workspace_op('note',args.get('name'),args.get('content'),args.get('task_id',''),args.get('mode','append')),
       'workspace_document':lambda args:_workspace_op('document',args.get('relative_path'),args.get('action'),args.get('text',''),args.get('old_text',''),args.get('requested_by_user','')),
       'workspace_database':lambda args:_workspace_op('database',args.get('action','connectivity'),args.get('schema','public'),args.get('table',''),args.get('columns'),args.get('limit',20)),
       'git_inspect':lambda args:git_inspect(args.get('repository'),args.get('diff',False),args.get('task_id')),
       'git_commit':lambda args:git_commit(args.get('repository'),args.get('paths'),args.get('message'),args.get('explicit_user_request',False),args.get('requested_by_user','')),
       'finalize_delegated_task':lambda args:finalize_delegated_task(args.get('task_id'),args.get('reviewed',False),args.get('review_notes',''),args.get('deleted_paths'),args.get('project'),args.get('test_filter'),args.get('supersedes_task_id'),args.get('documentation_reviews'),args.get('documentation_gaps')),
       'run_task_acceptance_check':lambda args:run_task_acceptance_check(args.get('task_id'),args.get('kind'),args.get('project'),args.get('test_filter'),args.get('relative_path'),args.get('force',False)),
       'task_acceptance_report':lambda args:task_acceptance_report(args.get('task_id')),
       'task_verification_plan':lambda args:task_verification_plan(args.get('task_id')),
       'wait_for_builder':lambda args:wait_for_builder(args.get('task_id'),args.get('timeout_seconds',BUILDER_WAIT_SECONDS)),
       'task_metrics':lambda args:get_task_metrics(args.get('task_id')),
       'delegate_to_builder':lambda args:_orchestrate('delegate',args),
       'delegation_status':lambda args:delegation_status(args.get('task_id')),
       'request_builder_revision':lambda args:_orchestrate('revision',args),
       'verify_delegated_backend_task':lambda args:verify_delegated_backend_task(args.get('task_id'),args.get('project'),args.get('test_filter'),args.get('reviewed',False),args.get('review_notes',''),args.get('reviewed_files'),args.get('documentation_reviews'),args.get('documentation_gaps')),
       'environment_status':lambda args:status(),
       'browser_diagnose':lambda args:browser_diagnose(),
       'run_web_smoke':lambda args:run_web_smoke(args.get('visible',False)),
       'inspect_web_application':lambda args:inspect_web_application(args.get('project'),args.get('routes'),args.get('widths'),args.get('height',900),args.get('visible',False)),
       'run_project_web_tests':lambda args:_standalone_verification('web',args.get('project'),lambda:run_project_web_tests(args.get('project'),args.get('visible',False))),
       'start_existing_emulator':lambda args:start_existing_emulator(args.get('name','')),
       'run_maestro_tests':lambda args:run_maestro_tests(args.get('relative_path','.maestro')),
       'finish_mobile_verification':lambda args:finish_mobile_verification(),
       'acceptance_evidence_detail':lambda args:acceptance_evidence_detail(args.get('task_id'),args.get('check_id'),args.get('include_logs',False)),
       'prepare_builder_dependencies':lambda args:prepare_builder_dependencies(args.get('project'),args.get('allow_network',False),args.get('explicit_developer_approval',False),args.get('approval_quote','')),
       'run_backend_tests':lambda args:_standalone_verification('backend',args.get('project'),lambda:run_backend_tests(args.get('project'),args.get('goal','test'),args.get('test_filter'),args.get('offline'),args.get('allow_network',False),args.get('explicit_developer_approval',False),args.get('approval_quote',''))),
       'run_http_smoke':lambda args:run_http_smoke(),
       'run_local_api_checks':lambda args:run_local_api_checks(args.get('relative_path'))}


def compact_tool_response(name,out,args):
    if not isinstance(out,dict):return out
    compact_tools={
        'run_task_acceptance_check','verify_delegated_backend_task','run_backend_tests',
        'run_project_web_tests','inspect_web_application','run_maestro_tests',
        'run_http_smoke','run_local_api_checks','run_web_smoke',
    }
    if name in compact_tools:
        import evidence_efficiency
        out=evidence_efficiency.compact_outcome(out,failure=out.get('status') in ('blocked','fail'))
    def compact(plan, *, detail_ids=()):
        if not isinstance(plan,dict) or not isinstance(plan.get('items'),list):return plan
        items=[]
        for item in plan['items']:
            row={k:item[k] for k in ('id','owner','kind','status','ready','dependency_ready','executable','execution_blockers','requires_idle_workspace','idle_reason','task_id','depends_on','superseded_by','superseded_reason') if k in item}
            if item.get('ready') or item.get('id') in detail_ids:
                row.update({k:item[k] for k in ('goal','paths','acceptance_criteria') if k in item})
            items.append(row)
        return {**plan,'items':items,'detail_available':'work_plan_status(detailed=true); full evidence retained on disk'}
    result=dict(out)
    if name in ('record_lead_inspection','extend_work_plan','defer_work_item','supersede_work_item','work_plan_status') and not args.get('detailed'):
        result=compact(result,detail_ids=[args.get('item_id')])
    if isinstance(result.get('work_plan'),dict):result['work_plan']=compact(result['work_plan'])
    if name in ('task_code_review','record_code_review') and isinstance(result.get('review'),dict):
        # required_files is the authoritative list; only emit missing subset if
        # it differs. Never lose open findings or historical changed paths.
        review=dict(result['review']);changed=result.get('changed_source_files')
        if changed==review.get('required_files'):result.pop('changed_source_files',None)
        if review.get('missing_files')==review.get('required_files'):
            review.pop('missing_files',None);review['all_required_files_missing_review']=True
        result['review']=review
    return result

def handle(request, *, cancel_event=None, progress=None):
    method=request.get('method')
    if method=='initialize':
        offered=request.get('params',{}).get('protocolVersion','2025-06-18')
        return {'protocolVersion':offered,'capabilities':{'tools':{'listChanged':False}},
                'serverInfo':{'name':'gaston-verify','version':TOOLKIT_BUILD}}
    if method=='ping':return {}
    if method=='tools/list':
        if central_mode():return {'tools':[x for x in TOOLS if x.get('name') not in CENTRAL_DISABLED_TOOLS]}
        return {'tools':TOOLS}
    if method=='tools/call':
        tool_started=time.monotonic()
        params=request.get('params',{})
        name=params.get('name');args=params.get('arguments') or {}
        if name not in CALLS or not isinstance(args,dict):
            return {'content':[{'type':'text','text':'Unknown tool or invalid input'}],'isError':True}
        try:
            if LIVE_SERVER and name!='environment_status' and hashlib.sha256((runtime_manifest() if central_mode() else ROOT/'.agentic/manifest.json').read_bytes()).hexdigest()!=MANIFEST_SHA256:
                out=response('blocked',reason='This MCP instance loaded an outdated toolkit. Close the stale session and reopen ./dev.')
            elif LIVE_SERVER and os.environ.get('GASTON_LEAD_TOKEN') and not _authorized_delegate():
                out=response('blocked',reason='This MCP instance no longer belongs to the active Engineering Lead.')
            elif central_mode() and name in CENTRAL_DISABLED_TOOLS:
                out=response('blocked',reason=CENTRAL_EVOLUTION_REASON)
            elif name=='wait_for_builder' and cancel_event is not None:
                out=wait_for_builder(args.get('task_id'),args.get('timeout_seconds',BUILDER_WAIT_SECONDS),cancel_event=cancel_event,progress=progress)
            elif name=='run_task_acceptance_check' and cancel_event is not None:
                out=run_task_acceptance_check(args.get('task_id'),args.get('kind'),args.get('project'),args.get('test_filter'),args.get('relative_path'),args.get('force',False),args.get('allow_network',False),args.get('explicit_developer_approval',False),args.get('approval_quote',''),cancel_event=cancel_event)
            elif name in ('prepare_builder_dependencies','verify_delegated_backend_task','finalize_delegated_task','prepare_checkpoint_acceptance','review_work_slice','validate_evolution') and __import__('verification_coordination').active(ROOT):
                out=response('blocked',blocker='verification_active',reason='Collect active verification before starting another gate or integrated review')
            elif name=='validate_evolution' and cancel_event is not None:
                if not _authorized_delegate():out=response('blocked',reason='Lead MCP authorization missing')
                else:
                    import evolution
                    out=evolution.validate(ROOT,args.get('candidate_id'),cancel_event=cancel_event,progress=progress)
            else:out=CALLS[name](args)
            if name in ('review_work_slice','finalize_delegated_task','verify_delegated_backend_task'):
                try:
                    if _authorized_delegate():
                        import learning_engine
                        learning_engine.collect(ROOT,limit=12)
                except (OSError,ValueError,TypeError,KeyError):
                    pass  # Learning telemetry must never override real verification outcomes.
        except Exception as exc:
            out=response('blocked',reason=f'Verification tool error: {type(exc).__name__}: {exc}')
            if _authorized_delegate():
                try:
                    import runtime_recovery
                    incident=runtime_recovery.record_internal_error(ROOT,name,exc,task_id=args.get('task_id'))
                    if incident:out.update(repair_incident=incident)
                except (OSError,ValueError,RuntimeError,TypeError,KeyError):
                    pass  # Preserve the original failure if local telemetry cannot be written.
        if LIVE_SERVER and name in ('plan_work','extend_work_plan','delegate_to_builder','resume_builder','request_builder_revision','start_lead_implementation','take_over_builder_task','lead_edit_files','complete_lead_implementation','record_lead_inspection','review_work_slice','prepare_checkpoint_acceptance','run_task_acceptance_check','finalize_delegated_task') and out.get('status') not in ('blocked','fail','cancelled') and _authorized_delegate():
            try:
                from session_store import Store
                import efficiency_runtime
                store=Store(ROOT);provider=store._configured_roles()[1];entry=store.get(provider,'reviewer')
                pid=out.get('plan_id') or args.get('plan_id') or (out.get('work_plan') or {}).get('plan_id')
                folder=_task_folder(out.get('task_id') or args.get('task_id'))
                if not pid and folder:pid=orch.read_record(folder/'task.json').get('work_plan_id')
                if pid and entry:
                    with orch.mutation_guard(ROOT):efficiency_runtime.bind_plan(ROOT,provider,entry['id'],pid)
            except (ValueError,OSError,TypeError,KeyError,RuntimeError):
                pass  # Never conceal the actual operation result.
        out=compact_tool_response(name,out,args)
        if LIVE_SERVER and _authorized_delegate():
            try:
                import evidence_efficiency
                evidence_efficiency.record_tool_telemetry(ROOT,name,args.get('task_id'),time.monotonic()-tool_started,out)
            except (OSError,RuntimeError,ValueError,TypeError):pass
        if LIVE_SERVER and name in ('review_work_slice','finalize_delegated_task','prepare_checkpoint_acceptance','verify_delegated_backend_task') and isinstance(out,dict) and out.get('status') in ('pass','ready','ready_for_human_release_review'):
            try:
                if _authorized_delegate():
                    import efficiency_runtime
                    advisory=efficiency_runtime.lead_boundary(ROOT)
                    if advisory:out['context_checkpoint']=advisory
            except (OSError,ValueError,TypeError,RuntimeError):pass
        return {'content':[{'type':'text','text':json.dumps(out,ensure_ascii=False,separators=(',',':'))}],
                'isError':out.get('status') in ('blocked','fail')}
    return {'error':{'code':-32601,'message':f'Method not found: {method}'}}


def main():
    global LIVE_SERVER
    LIVE_SERVER=True
    def _terminate(signum, frame):
        raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,_terminate)
    _recover_abandoned_emulator()
    try:
        if _authorized_delegate():
            __import__('verification_coordination').recover_proven_interruption(ROOT)
    except (OSError,ValueError,RuntimeError):
        pass  # Unverifiable reservations remain blocked, never silently erased.
    atexit.register(_cleanup_on_exit)
    protocol_out=sys.stdout  # Only send() writes JSON-RPC; concurrent tool diagnostics go to stderr.
    sys.stdout=sys.stderr
    output_lock=Lock(); waits_lock=Lock(); waits={}

    def send(result):
        with output_lock:
            protocol_out.write(json.dumps(result,separators=(',',':'))+'\n')
            protocol_out.flush()

    def reply(req, cancel_event=None):
        token=req.get('params',{}).get('_meta',{}).get('progressToken')
        def progress(elapsed, stage):
            if token is not None and (cancel_event is None or not cancel_event.is_set()):
                send({'jsonrpc':'2.0','method':'notifications/progress','params':{'progressToken':token,'progress':elapsed,'total':BUILDER_WAIT_SECONDS,'message':('Evolution validation: ' if req.get('params',{}).get('name')=='validate_evolution' else 'Builder working: ')+stage}})
        out=handle(req,cancel_event=cancel_event,progress=progress if token is not None else None)
        if 'error' in out:result={'jsonrpc':'2.0','id':req['id'],**out}
        else:result={'jsonrpc':'2.0','id':req['id'],'result':out}
        if cancel_event is None or not cancel_event.is_set():send(result)

    def background_wait(req, event):
        """Release the subscription slot even on cancellation, without stopping the worker."""
        try:reply(req,event)
        finally:
            with waits_lock:waits.pop(req['id'],None)

    try:
        for line in sys.stdin:
            try:
                req=json.loads(line)
                if not isinstance(req,dict):continue
                if req.get('method')=='notifications/cancelled':
                    with waits_lock:
                        event=waits.get(req.get('params',{}).get('requestId'))
                        if event:event.set()
                    continue
                if 'id' not in req:continue
                if req.get('method')=='tools/call' and req.get('params',{}).get('name') in ('wait_for_builder','validate_evolution','run_task_acceptance_check','run_project_web_tests','run_backend_tests','inspect_web_application'):
                    event=Event()
                    with waits_lock:waits[req['id']]=event
                    Thread(target=background_wait,args=(req,event),daemon=req.get('params',{}).get('name')=='wait_for_builder').start()
                else:reply(req)
            except (ValueError,TypeError) as exc:
                send({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':str(exc)}})
    finally:
        # A closed Lead session drops its subscriptions, not implementation work.
        with waits_lock:
            for event in waits.values():event.set()


if __name__=='__main__':main()
