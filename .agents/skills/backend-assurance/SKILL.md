---
name: backend-assurance
description: End-to-end Java/PostgreSQL/API backend engineering with explicit tests, database safety and measured performance. Use when building or reviewing backend features, persistence, payments or integrations.
---

# Backend assurance — evidence-first workflow

1. Trace actual API endpoint → authentication/authorization → domain/service → SQL/ORM → migration → events/provider integration. Read only affected code and its contracts. Do not introduce an architecture unrelated to the request.
2. Translate acceptance criteria into tests: ordinary path, invalid input, unauthorized and cross-tenant access, retry/idempotency, concurrent requests, exceptions, partial failures, timeouts, and data consistency. Use real HTTP calls or contract tests where appropriate, never mocked HTTP/controller success alone.
3. For PostgreSQL, prefer real disposable PostgreSQL integration tests (existing Testcontainers or project-supported local test DB), versioned migrations and constraints. Test fresh database startup and migration upgrade where applicable. Never run test fixtures, truncate statements or migrations against production/staging without permission.
4. For financial state, specify exact currency/units/rounding, atomicity, transaction boundaries, isolation/locking, idempotent retries, uniqueness and audit trail. Verify two concurrent requests cannot double-post. Require an authorized owner for real-money semantics.
5. Check performance only for affected hot spots: baseline/correctness first, then EXPLAIN (ANALYZE, BUFFERS) on safe disposable data, SQL query counts/N+1, bounded pagination and useful indexes. Use load tests only against an explicitly approved local/sandbox target; record test data and concurrency.
6. Security review: object-level auth, secrets and PII in logs, parameterized SQL, dependency vulnerabilities, rate limits, webhook authenticity and request replay. Run existing dependency/static checks if installed; suggest new tools as project-specific optional gates with approval, never pretend scans ran.
7. Record executed Maven/Gradle, API, DB-integration and security commands, their exit codes and coverage gaps. Distinguish Unit, Integration, Contract, Browser (Playwright), and Mobile (Maestro); one is not a substitute for another.
8. A feature is not release-ready if relevant database/API/contract tests have not actually run. Report exactly which environment or credential is missing and whether the change requires compliance review. Do not deploy, push, merge or use live customer data automatically.

## Tooling hierarchy

- **Java build:** `./mvnw -B verify` or Gradle wrapper test, as detected by the workspace. Respect existing build/test conventions.
- **PostgreSQL:** Testcontainers + JUnit 5 if supported by the actual build, migration verification (Flyway/Liquibase when used), explicit concurrency tests for relevant invariants.
- **API:** JUnit/RestAssured/WebTestClient or existing project tests; optionally Playwright Test APIRequestContext for black-box sandbox API checks. Do not introduce a second framework without rationale.
- **Browser:** Playwright MCP for exploration, Playwright Test for repeatable web/API E2E evidence. Neither substitutes for a physical mobile emulator.
- **Mobile:** Maestro + Flutter tests when the mobile app and device exist.
- **Security:** SAST/dependency scanning and OpenAPI compatibility only if actually configured/run. Never claim regulatory certification.

## Stop conditions

Undocumented financial invariants, migration/data loss, unknown API provider finality, production-impacting operations, and ambiguous authentication/ownership semantics require human decisions before implementation.

## Efficient verification and dependencies

Choose the smallest **meaningful** suite, not the smallest module boundary. Reuse actual existing tests where they prove an invariant. For each affected API, inspect real routes/permissions, and add only coherent positive, negative and state-consistency cases. If appropriate, write `.agentic/local/api-checks.json` for the local `curl` runner. Do not invent endpoints or claim `curl` proves persistence without a DB assertion. For related Flutter/browser clients, shared contracts, or migrations, request the corresponding checks when their behavior could change. Do not run unrelated tests by default, but retain regression coverage of shared critical flows. Document why each test was selected or omitted. Separate verified behavior from unavailable test environments.
