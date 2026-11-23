# Live acceptance playbook

Offline tests prove toolkit mechanics, not authenticated provider or application behavior. Run these checks in a disposable project or an authorized local development workspace after a safe overlay upgrade. Preserve existing sessions, config, application source and unrelated changes.

## Runtime and session identity

Close managed sessions and wait for the Builder before overlaying files. Restart `./dev`; call `gaston_verify/environment_status` freshly and compare `toolkit_release`, `toolkit_build`, `server_path` and `manifest_sha256` with the installation. The server handshake must use the same manifest version. `./dev sessions` should retain existing bindings. Ask an architecture question: no Builder model request or additional session should be created. Inspect native hook trust through the provider UI if necessary; never infer approval from file presence.

## Role handoff and orchestration

In a disposable repository, ask the Lead to implement a harmless, bounded production-code change with an independent test and inspection. It should plan the slices, write the test itself using scoped edit tools, and delegate only the substantial implementation to Builder; disjoint Lead work proceeds while the Builder runs. A second substantial implementation reuses the persistent Builder ID and gets a distinct task record. Both providers retain native permission controls. Test quota interruption, graceful worker termination, hard-kill orphan protection, and cancellation separately from code defects; wait once for completion, resume the same task and preserve edits. After two unsuccessful defect corrections the Lead takes over. Confirm no overlapping writes and no automatic Git actions. Explicitly requested local commits include only requested paths; nested or unborn Git repositories should be recognized. Test deletion acceptance with real missing-path and current verification evidence.

## Backend and HTTP

Use an existing genuine test selector with `run_backend_tests`; verify actual exit code, JUnit tests and selected JDK. Dependency resolution or missing Java is BLOCKED. The isolated cache does not change system Java or project build files. See `BACKEND_VERIFICATION.md` for managed actual HTTP, real persistence, negative workflows and stateful tests; a generic smoke or mocked test alone never establishes service acceptance. Confirm review and test gates are current and missing evidence blocks acceptance. Inspect real API and decision documentation and all changed source/test paths with the code-review tools. A new source revision invalidates stale attestations. Final checkpoint acceptance uses integrated risk-selected coverage, not just slice passes.

## Browser and mobile

Run existing application Playwright Test specs against an explicitly authorized loopback app and check real assertions. Playwright-managed Chromium smoke/diagnostics are useful but cannot establish application behavior or original browser MCP repair. Visible mode uses an isolated profile and a real display. For Maestro, reuse a running Android device or start an existing AVD; check actual flow success and cleanup identity. A Gaston-owned emulator should stop after the test; an emulator started outside Gaston must remain running. Never use production accounts or real payment data.

## Self-improvement and maintenance

Reproduce a known toolkit defect across all affected call sites in a disposable candidate, add a failing baseline and passing candidate regression, review the exact changes and validate compatibility plus package integrity. Queue activation only for idle startup; verify changed files, manifest and regression survive, then test rollback. Preserve configured model, local learning, session and task evidence. Check the shared contract and docs contain no contradictory old instructions. Scanners identify candidates, not proof of dead code or semantic readability. Do not delete dynamic registrations or intentional historical evidence merely to improve metrics.

Use `task_metrics` and `work_plan_usage` to compare measured durations, correction rounds, actual provider tokens and tests for matched work. Missing provider usage is unknown, not zero. No efficiency guarantee follows from offline tests.
