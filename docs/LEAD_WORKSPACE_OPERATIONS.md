# Lead workspace operations

## Why

The Engineering Lead should not consume a second agent's context or tokens to perform routine diagnostics, local operational tasks or simple documentation work. The Builder continues to own scoped implementation; the Lead owns investigation, tests, review and the overall outcome.

## Direct capabilities

| MCP tool | Supported operation | Boundaries |
| --- | --- | --- |
| `workspace_overview` | Directory overview, bounded filename search | Workspace-relative, hidden/sensitive content excluded from searches |
| `workspace_diagnostics` | Known CLI version probes, dependency-marker inspection, explicit loopback ports, stale-temp candidate inspection | Fixed executable list, no arbitrary commands, no installation |
| `workspace_logs` | Existing .log/.txt/.json/.xml/.out tails, optional text filter | Last 64 KiB, 300 lines maximum, output limit and secret redaction (not infallible) |
| `workspace_processes` | Same-user processes with working directory inside workspace | Native Linux /proc and Windows/macOS psutil snapshot, read-only |
| `workspace_preview` | Start/status/stop local static site with index.html | 127.0.0.1 only, allowlisted static extensions, ownership verified via PID+starttime+argv, no app commands |
| `workspace_housekeeping` | Inspect and optionally delete stale Gaston `.sessions-*.tmp` and ended preview temp logs | Cleanup requires developer request, no directory recursive deletes, source and caches preserved |
| `workspace_note` | Create/append `.md` notes under `.agentic/local/lead-notes/` or an existing task's `lead-notes/` | Only owned note subdirectories, no application files |
| `workspace_document` | Create, append, or replace a uniquely matched passage in workspace `docs/` Markdown/txt files | Developer-requested; no AGENTS/CLAUDE.md or application/source edits |
| `workspace_database` | PostgreSQL readiness, metadata, constraints, indexes, statistics; opted-in synthetic table counts, duplicates and samples | `GASTON_READONLY_DATABASE_URL` required; loopback only, fixed SQL, enforced read-only transaction |
| `supersede_work_item` | Retire an unstarted duplicate/obsolete plan item in favour of an explicit replacement and reason | Old item and history are preserved; only pending unstarted dependencies are rewired; superseded is not done |

A superseded plan item remains in the audit history and is not counted as completed work. The replacement must pass its own acceptance gates before integrated checkpoint acceptance; pending dependents are rewired to the replacement, while already-started dependents block the operation.

The existing `git_inspect`, `git_commit`, `finalize_delegated_task`, backend runner, curl checks, Playwright and Maestro remain available. Claude's native `Read`, `Glob` and `Grep` should be used to read actual application source.

## Permission tiers

1. **Auto-approved in enrolled Claude Lead:** directory discovery, tools/dependencies/log/process/port metadata, local DB diagnostics if configured (fixture data requires developer opt-in), and notes in Gaston's private folders. Git inspection and existing test tools continue as before.
2. **Provider authorization on execution:** local Git commit, static preview lifecycle, stale temp cleanup, and `docs/` writes. Cleanup and document edits also require a specific user-request statement. A boolean/quote passed by a model does **not** independently prove consent; the agent must have a genuine request.
3. **Native permissions:** The Lead discovers and uses its provider’s native tools, shell and MCP connectors for non-coding work under native permissions. Gaston does not preapprove new connectors.
4. **Role boundary:** substantial implementation goes to the Builder; scoped tests and small fixes belong to the Lead; installations, destructive actions, Git writes and other sensitive operations require explicit developer authorization. The scoped verifier still cannot perform arbitrary shell, arbitrary SQL or production writes. Native tools are not a Gaston security sandbox.

## Local PostgreSQL inspection

PostgreSQL is the database engine; `psql` is its host command-line client. Docker/Testcontainers starts real temporary engines for isolated integration tests. Gaston does not install or depend on pgAdmin.

Call `workspace_database` with `action: status` to discover whether psql, the Docker CLI and a connection configuration are present. This does not prove Docker or PostgreSQL is running. Setup separately detects psql for Java backend work without silently installing host packages. If missing, arrange installation with the developer's authorization and recheck. No extra CLI command or global MCP registration is needed.

Supply `GASTON_READONLY_DATABASE_URL` in the environment **before starting Gaston** with an authorized local development or synthetic test URL of the form `postgresql://readonly_user:password@127.0.0.1:5432/database_name`. Use a dedicated role with SELECT/metadata permissions only; never a production role, tunnel or administrative connection. Supply secrets securely via the local environment, not in chat, command arguments, committed configuration or logs. For synthetic fixture-data diagnostics only, the developer may also set `GASTON_DATABASE_ALLOW_FIXTURE_DATA=1`. Restart Gaston when these settings change. Clear the variables once finished. The Builder launch does not forward these variables. Native provider permissions are not a security sandbox.

| Action | Inputs beyond action | Meaning |
|---|---|---|
| `connectivity` | none | Actual connection and read-only setting |
| `tables`, `migrations` | schema (default public) | Table discovery, not successful migration evidence |
| `columns`, `constraints`, `indexes`, `table_stats` | schema, table | Metadata and estimated statistics; not an exact record count |
| `count` | schema, table | Exact count of visible fixture rows |
| `duplicates` | schema, table, columns (1–8) | Number of duplicate groups and excess rows; NULLs group together |
| `sample` | schema, table, columns (1–8), limit (1–50, default 20) | Unordered synthetic records, text values capped at 160 characters per field; credential-like column names blocked |

The last three actions require explicit fixture opt-in and a regular/partitioned table with no foreign descendants. Identifiers are validated and SQL is generated from fixed templates; there is no arbitrary SQL interface. The tool pins numeric loopback routing, clears inherited libpq environment overrides, uses read-only transactions, and imposes connection, statement, lock and subprocess timeouts. It never echoes passwords or raw SQL errors. Loopback alone cannot distinguish a local database from a tunnel: the developer must authorize the specific local test database. Sample filtering cannot guarantee removal of all sensitive data; **synthetic data only**. Results are bounded and diagnostic, not acceptance-test passes.

Testcontainers databases normally disappear at test completion. Gaston does not automatically discover or attach to random containers. For a live fixture that stays running, an explicitly configured read-only loopback role can be inspected with this tool. For short-lived fixtures, the Lead must author project-owned integration tests with persisted-state assertions (e.g. balances, idempotency, constraints, migrations and rollback) **before teardown**. Use SQL/JDBC in reviewed project tests for domain-specific comparisons, and never substitute a row sample or count for an assertion. If a live inspection connection is unavailable, record the limitation; do not guess credentials or access production.

## Test evidence and current limits

Offline regression tests: `PYTHONPATH=.agentic python -m unittest discover -s .agentic -p 'test_*.py'`. The workspace-operations tests use temporary directories, an isolated static loopback server, and mocked PostgreSQL CLI calls. No production or real application PostgreSQL connection is attempted in that suite.

This update does not prove lower token cost, a 4× acceleration, real app E2E acceptance, or that the developer's actual workstation tools work. Measure these with real tasks and compare task metrics and evidence over time.


## Native tools and permissions

The Claude launcher does not pass a restrictive `--tools` list. The enrolled Lead's project hook does not block Bash or unfamiliar MCP tools by name. Known direct file editing is limited to plain documentation under `docs/`; the launcher disables native edit tools if the exact project hook is not configured, and scoped `workspace_document` remains available. Claude's native permissions, user settings, organization rules and connector authentication apply normally. Only bounded verifier operations and `pwd` are preapproved by Gaston; default permission mode permits the user to authorize other actions as needed. Use current scoped edits for Lead tests and small repairs; delegate larger implementation. Do not bypass the scope guard. Explicit authorization is required for sensitive actions. A role contract and direct-edit guard do not provide mandatory isolation against arbitrary approved shell or external connector behavior.

The same launch policy applies to interactive and scripted Claude Lead runs; noninteractive runs may be blocked by permissions that require live approval. Existing valid sessions are retained; restart after upgrade to load the updated instructions. Verify live with a connected plugin (for example, search an authorized project board) and a non-coding native shell task. No changes to global connector configuration are made.

**Codex-only and Claude-only architectures:** the same Lead/Builder role boundaries apply independently of the CLI. Codex Lead keeps its native read-only sandbox and configured interactive approval policy, with role-local verifier tools for authorized local writes. Scripted Codex reviews retain noninteractive approvals disabled and receive the shared role brief. Codex direct patch calls are blocked by the enrolled Lead hook; documentation writes use the scoped verifier. Native connector availability depends on the provider configuration. No all-tools approval, global connector registration or sandbox bypass is introduced.
