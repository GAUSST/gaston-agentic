# Backend validation playbook

This is a **plan and engineering contract**, not proof that any particular app has passed. Gaston Agentic's shipped toolkit does **not** include your Java project, database, APIs, test fixtures, credentials or running services. The Lead/Builder must inspect the real project and implement only justified checks.

## What can run today

`./dev init` detects a Java backend by Maven/Gradle files, then configures `backend_verify` for a Maven `verify` or Gradle `test` run. These commands execute in the project, when its build tools are installed. They say nothing about PostgreSQL integration coverage unless the app build itself includes those tests.

`./dev init` can, with independent approvals, register Playwright MCP for the Engineering Lead and prepare an isolated Playwright Test harness. The test harness is created under `.agentic/local/playwright-tests/` (local/ignored). **No fabricated tests** are created. Once real `.spec.js/.spec.ts` tests exist, `web_e2e` is automatically included in the verification steps. The Builder and Engineering Lead run meaningful tests through the normal development/review workflow; no developer test command is needed. Set `PLAYWRIGHT_BASE_URL` for a local or sandbox app.

Maestro remains responsible for **native mobile** E2E. Playwright tests may cover browser-based UI or sandbox API flows, not native Flutter screens.

## What a backend feature must prove

| Area | Concrete test/evidence | Implementation prerequisite |
| --- | --- | --- |
| Service/domain | Business invariant + failure-path unit tests | Java project + real requirements |
| API contract | Authn/authz, status/error and request/response compatibility | Running sandbox API or controller integration tests |
| PostgreSQL | Disposable Postgres test; constraints, migrations, rollback strategy | Testcontainers or authorized local DB |
| Concurrency | Simultaneous requests cannot double-post or violate uniqueness | Real DB + deterministic fixture |
| Idempotency | Duplicate requests and retry-after-timeout behave as specified | Business-defined idempotency contract |
| Security | Tenant isolation, secrets in logs, dependency scans | Real auth model and configured scanner |
| Performance | EXPLAIN ANALYZE on safe fixtures, measured throughput/latency | Approved sandbox + representative data |
| Compliance | Applicability mapping and expert review where needed | Official texts, legal owner and jurisdiction |

## Enabling project-specific gates

Only add a gate to `.agentic/config.json` once a **real executable** test command exists. Example for an *existing* Java integration test profile:

```json
{
  "name": "postgres_integration",
  "cwd": "backend",
  "command": ["./mvnw", "-B", "-Ppostgres-it", "verify"],
  "timeout_seconds": 1200
}
```

That command is **illustrative**; do not enable it unless the actual backend supports the `postgres-it` profile. For a database-scoped task Gaston Agentic requires a `postgres_integration` gate; for API tasks it requires `api_integration`, for security tasks `security_checks`, for performance tasks `performance_benchmark`, and for web tasks `web_e2e`. These named gates must map to real project commands. The toolkit never fabricates their success. The app must supply the corresponding real tests.

## Network policy and activity evidence

Maven and Gradle dependency resolution for \`run_backend_tests\`, task-linked backend checks and managed API startup is **offline by default** (\`-o\` / \`--offline\`). Inherited HTTP proxy settings are removed for these offline dependency runs. If the wrapper distribution or a required dependency is missing from the local cache, the check is BLOCKED; it must not silently download anything.

An online dependency-resolution run requires all three fields on that specific call: \`allow_network=true\`, \`explicit_developer_approval=true\`, and a verbatim \`approval_quote\` from the developer. A generic request to update Gaston, run tests, or prepare a release is not network approval. \`network_allowed\` records permission; \`network_usage\` records only dependency-manager activity visible in its output (\`download_observed\`, \`request_observed\`, \`not_used_by_build_tool\`, or \`unknown\`). \`network_attempted\` is true, false or null based on that same evidence; null means unobserved. \`unknown\` is not evidence that no network was used.

**Important boundary:** this is offline enforcement for Maven/Gradle dependency resolution, not an OS-level network sandbox. Trusted project build plugins, application code, scenario drivers and arbitrary project tests may still open their own network connections. Gaston does not claim to have measured those connections. Use only reviewed trusted project code; do not interpret \`not_used_by_build_tool\` as proof that every child process was unable to access the network.

## Evidence standard

Record exact commands, exit codes and short results. State whether tests used mocks, in-memory databases, or real disposable PostgreSQL. A successful compilation is not a passed functional test; an LLM review is not a legal/security audit; a Playwright MCP walkthrough is not a repeatable Playwright Test suite.

## Playwright and backend limitations

The Lead's project-scoped read-only source hooks permit Playwright MCP browser navigation/click tools, but **browser actions can still modify the sandbox web app's data**. Do not point them at production or perform transactions without explicit developer authorization. Independent scripted source review can run without a browser; missing browser checks must be disclosed.

For Java projects, `mvn verify` or `gradle check` is a baseline, **not** proof of PostgreSQL, API, security or performance test coverage. Connect each required gate to an existing executable test command, preferably tests using disposable PostgreSQL, then verify actual logs and exit codes.

## Risk-based verification and related sections

The **Engineering Lead** now plans verification before delegation. For each task it selects only meaningful checks based on the requested behavior, changed files, and likely knock-on effects. It runs a configured check only when relevant, **but follows dependencies beyond the edited module when necessary**: a controller or shared contract change can also run Flutter/widget and browser contract tests; a migration also prompts API checks when present; security/auth changes prompt permission and client checks. A financial operation can require backend + API + PostgreSQL + security evidence even if only one class changed.

This avoids blanket reruns without mistaking a passing unit suite for proof that a multi-component workflow works. Existing configured checks remain the source of executable commands; duplicate commands in the same directory execute once per verification pass and the second gate is labeled as **shared evidence, not an independent test**. Important regression suites are retained and rerun when they can detect a relevant consequence. Unconfigured *required* checks become **blocked**, and the Lead authors or configures real tests/gates. The selection and rationale are stored in `.agentic/runs/<task-id>/verification_plan.json` and the final report.

### Focused curl API checks

The Lead should prefer existing HTTP integration tests, but may write `.agentic/local/api-checks.json` when real local black-box endpoint verification is needed. Gaston automatically discovers this file as `api_integration`; there is no extra setup menu or command. Example (replace the paths and response assertions with **real project contracts**):

```json
{
  "cases": [
    {"name":"health", "method":"GET", "path":"/actuator/health", "expected_status":200, "expect_json":{"status":"UP"}},
    {"name":"requires authentication", "method":"GET", "path":"/api/private", "expected_status":401}
  ]
}
```

To run, a real disposable/local API must be running and the Lead must set `AGENTIC_TEST_API_BASE_URL=http://127.0.0.1:<test-port>` in the process environment. The runner uses `curl` against **loopback only**; it checks actual HTTP codes and selected JSON values, refuses remote targets and redirects, and never prints response bodies or credentials in its evidence. For a test-only POST/PUT/PATCH/DELETE case, the Lead must explicitly set `"allow_local_mutation":true` *after* ensuring the local environment is disposable. For auth headers, `header_env` maps header names to preexisting environment variables rather than hardcoding tokens. No tests run against production.

A successful `curl` HTTP response **does not** independently prove database atomicity, idempotency, accounting invariants or performance. Those require actual appropriate project integration/concurrency tests, and the reviewer must inspect coverage and the persisted effects.

**Java backend verification:** The Lead can invoke `gaston_verify.run_backend_tests` for a named Maven/Gradle project and optional test class. It uses `.agentic/local/backend-dependencies` instead of the developer’s `~/.m2`, records real exit codes and output, and reports dependency resolution issues as BLOCKED. Maven/Gradle plugins execute the trusted project’s build code; no arbitrary command or `pom.xml` modification is permitted through the verifier.  Gaston detects the project Java release, discovers installed JDKs, selects a compatible JAVA_HOME for the test process only, and can retry once if an inherited compiler release is revealed by Maven. Missing compatible JDKs are reported as BLOCKED; Gaston never installs a JDK or changes pom.xml without approval. A successful browser/API smoke test does not prove Maven tests ran.

## Managed actual application HTTP checks

Use this workflow for task-linked service acceptance; standalone curl is diagnostic only. The toolkit cannot infer a production datasource or provider credentials, and must never substitute a demo server for the application. For Maven Spring Boot on POSIX, the Lead adds or reuses a reviewed **test-source launcher** that starts the actual application with isolated disposable PostgreSQL (for a persistence-dependent service), a `gaston-e2e` profile, test-only keys and provider stubs. Pass arguments through to Spring Boot; honor the runner's dynamically selected loopback port. The launcher must run real application code and migrations; never register test fixtures in production or use production data. App-specific configuration and protocol drivers belong to the project, not the toolkit.

Create `gaston-e2e.json` at the Maven project root. Example (names and paths are illustrative; use actual project contracts):

```json
{
  "main_class": "org.example.E2eLauncher",
  "suite": "src/test/e2e/http-cases.json",
  "readiness_path": "/actuator/health",
  "readiness_status": 200,
  "scenario_driver": "src/test/e2e/identity_scenarios.py",
  "scenario_driver_timeout_seconds": 600
}
```

The optional `scenario_driver_timeout_seconds` sets a bounded driver-only timeout (1–600 seconds). The default remains 240 seconds; the declarative HTTP suite is unchanged. No scenario, assertion, or polite external request delay may be removed to satisfy the budget.

The main class must exist under `src/test/java`. The JSON cases use `path`, `method`, `expected_status`, `expect_json`, `json_body`, and `allow_local_mutation` for mutations of disposable state. `capture_json` maps a variable name to a JSON path (e.g. `{"access":"accessToken"}`); use `${access}` in a later test authorization header. Assertions can compare captured values. Never put credentials in URLs, case names or output. The optional Python `scenario_driver` under `src/test/` uses the provided `AGENTIC_TEST_API_BASE_URL` for real stateful HTTP assertions (e.g. dynamic signatures or TOTP), exits nonzero on any failed test, and must be reviewed for coverage and isolation. No hardcoded real user data. The Lead reviews test assertions and calls `gaston_verify.run_task_acceptance_check` with `kind=api` and the project path, after executing the relevant backend suite.

The runner selects an installed compatible JDK, runs Maven `test-compile spring-boot:test-run` with the reviewed test main and profile, waits for curl readiness, executes the actual curl suite and optional driver, then terminates its own application process group. The launched Java test fixture is responsible for disposable database cleanup, including Testcontainers lifecycle. Inherited application secret environment variables are excluded; a local Docker socket is selected. This is a trusted-workspace test runner, not a security sandbox against project code or Maven plugins. Failed launch diagnostics are retained privately under `.agentic/local/managed-backend-failures`, not printed to the MCP response; they must contain synthetic fixtures only. The Lead inspects failures and corrects the fixture or directs scoped application corrections as appropriate. A missing fixture, unavailable runtime, readiness failure, scenario failure, or unconfirmed cleanup does not pass. The declarative suite must include JSON assertions and use actual endpoints; readiness or a toy response alone does not establish business coverage.

`task_acceptance_report` distinguishes planned gates from executed current-revision passes. Finalization refuses missing checks or documentation review. For complex checkpoints the Lead must demonstrate the actual business workflow, not just HTTP reachability. After upgrading, earlier test records remain historical; reopen security-sensitive checkpoints for the new checks if acceptance was incomplete. Do not retroactively fabricate evidence.


## Database inspection versus acceptance

A Docker availability check only identifies a possible prerequisite. During interactive setup, Gaston offers approved package installation for missing `psql` and other host tools on supported systems. It does not install a host PostgreSQL server. A separately approved disposable `docker.io/library/postgres:16` smoke check starts a container without exposed ports or app data, waits for PostgreSQL, executes `SELECT 1`, and verifies cleanup. Cached success is identified by the local daemon and image ID and is historical evidence only. This smoke test does not prove application integration coverage. Use the project's real disposable PostgreSQL tests for migrations, constraints, transactional invariants and persisted-state assertions before fixture teardown. `workspace_database` provides bounded read-only `psql` diagnostics on an explicitly configured local development/test connection; opt-in synthetic data actions include row count, duplicate groups and a small projected sample. It does not automatically attach to arbitrary Testcontainers instances. The graphical pgAdmin client is not needed. See `docs/LEAD_WORKSPACE_OPERATIONS.md` for setup and boundaries.


**Builder compile-only preflight:** The active Builder uses `python3 .agentic/builder_compile.py <project>` inside its native sandbox; Maven `test-compile` or Gradle `testClasses`, offline-first, with the isolated project dependency cache. Maven uses the executable project wrapper when present. If offline resolution is blocked specifically by missing dependencies or an uncached Maven/Gradle Wrapper distribution, the helper reports the blocker without allowing an implicit wrapper download. Only after explicit developer approval may the Lead authorize one online fallback with `--allow-network`; that command retries offline first and then attempts online once. It does not retry cache-permission or source-compilation failures, and provider sandbox/network policy still applies. Attempt-specific receipts distinguish pass, fail, blocked, stale, and not run; only a fresh `pass` permits Builder handoff. This does not execute tests or replace independent Lead acceptance; changed project plugins must not be executed on the host by a pre-review worker.


## Compact acceptance and evidence reuse

`run_task_acceptance_check` returns compact backend summaries, not raw Maven console output. Full backend output is kept in ignored, access-restricted `.agentic/local/backend-logs/` files. `acceptance_evidence_detail(task_id, check_id, include_logs=true)` returns a bounded diagnostic excerpt on explicit request. A failed check never becomes a pass by hiding its logs.

Changed existing JUnit test classes can supply an exact selector for planned slice checks, with a conservative maximum of eight classes. No test-class guess is inferred from production code. Checks remain full when the selector cannot be established. Evidence is shared across tasks only for real passing backend runs with an identical committed/uncommitted workspace, project, test selector, and available toolchain/environment signature within 30 minutes; source task/check provenance is retained. Missing Git, environment or executed-test proof disables cross-task caching. Full integrated checkpoint backend checks and managed live HTTP checks are always fresh, even if earlier slices passed. Tool-result size and elapsed time telemetry is recorded without storing prompt arguments or source text.
