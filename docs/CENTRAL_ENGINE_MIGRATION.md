# Central Gaston engine — phased v1.6 migration

**Current stage: packaged engine, read-only core dispatch and opt-in source-free project bootstrap.** Do not treat this as a completed global-runtime migration.

## Ownership
- Engine: centrally installed `gaston_engine` package (runtime, orchestration, hooks, verification, internal tests and policies), outside application repositories
- Project adapter: future `.gaston/project.json`, created only via explicit `dev init`
- Durable project memory: sessions, context documents, decisions, tasks, history, evidence, configuration and user-defined policies — preserved even when Gaston is uninstalled in *keep* mode
- Application code, existing developer documents, Git history and credentials: never Gaston-owned

## Steps required before v1.6 release
1. Dual-mode native `dev` entry: preserve old workspaces, use installed engine for newly initialized projects without copying its source.
2. Repoint every executed helper, MCP server and Claude/Codex hook to the installed engine. Never execute untrusted project-supplied modules as engine code.
3. Migrate old `.agentic` state transactionally into an explicit per-project integration with backups and reversible recovery. Preserve all session IDs, approved context, task evidence and custom policies.
4. Centrally update the trusted signed engine with version pinning, fail-closed verification and rollback; updates never write application code or user context. No silent updates.
5. Preserve exactly `dev`, `dev init`, `dev models`, `dev sessions`, `dev uninstall`, `dev update`. Uninstall keeps context by default, offers independently confirmed full erase and removes only verified Gaston-owned assets.
6. Test fresh installs, nested projects, path/symlink abuse, interrupted migration, rollback, provider hooks and local sessions on Windows/macOS/Linux (Python 3.11/3.13) before switching dispatch.

## Minimal source-free project adapter (next stage)

- Explicit `GASTON_ENGINE_MODE=central dev init` creates only `.gaston/project.json` in an **existing** project; it never copies engine source, internal tests, workflows or top-level provider instructions. Re-running init is idempotent and preserves context.
- `GASTON_ENGINE_MODE=central dev --help` works in the newly initialized, source-free project via the centrally installed engine. No implicit project initialization occurs.
- Unknown or linked adapter markers are rejected without overwriting data. Nested directories reuse the nearest existing project marker. Old `.agentic/local` context is left untouched.
- This bootstrap **does not yet make central-mode models, sessions, agents, hooks, update or uninstall operational**. Real legacy-to-central migration must remain a separate explicit and reversible action.
- All six CI platforms run dedicated bootstrap tests.

## Earlier phases implement
- Python wheel includes all core modules under `gaston_engine` next to the native `dev` executable, so engine code can live outside application repositories.
- `project_layout` makes project versus engine path boundaries explicit, recognizes legacy and future central project markers and does not initialize projects implicitly.
- New layout and packaging tests; CI verifies that installed wheels contain engine modules on all three systems.
- The installed CLI retains legacy dispatch by default. For matching-version existing workspaces only, `GASTON_ENGINE_MODE=central dev --help` executes the installed launcher and `eng.py` instead of the project's copies. **Only `--help` is allowed in central preview**; all stateful operations are refused. The read-only preview bypasses workspace evolution locks and the updater's write-producing recovery-store initialization. It refuses to run when an interrupted-update journal exists, leaving recovery to the legacy dispatcher. It refuses uninitialized projects and does not trigger local evolution.
- The Linux full-suite test now locates the actual installed engine package instead of reading source-relative `.egg-info` file lists, which incorrectly failed PR #6's post-merge CI.

**Do not manually delete `.agentic` or tag v1.6 until remaining gates pass.** The core execution preview does not yet repoint sidecar scripts, hooks, provider integrations, central updates or project state; it must not be used as evidence that source-free projects are functional.

## PR #9 — single final v1.6 delivery branch (DRAFT)

All remaining implementation and stabilization work belongs to **one PR #9**, including repairs discovered by CI. There is **no separate PR #10**. The first commit adds automatic project-mode selection: ordinary `dev init` in a fresh application creates a source-free `.gaston/project.json`, while `dev` in an existing legacy workspace continues on the legacy engine. Existing central projects select installed-engine dispatch automatically. Until the full runtime refactor is complete, most central commands remain preview-gated.

### Release-blocking acceptance contract

1. **Clean install:** A global `dev` installation is enough; running `dev init` in a clean application creates `.gaston/` context and adapter only, no copied `.agentic/*.py`, internal tests, source workflows, or root Gaston source documents. Central `dev`, `models`, `sessions` and approved provider integration work end-to-end.
2. **Core and workers:** All agents, tools, MCP servers, native hooks, verifier/Builder workers, background hooks, provider CLI handoffs and standalone Python helper executions load the **installed trusted** engine. Never resolve executable core modules from user-controlled project files.
3. **State:** Project sessions, tasks, evidence, custom policies, approved context sources, configuration, decisions and history live under project-owned `.gaston/`; no sensitive state moves to a global shared directory. Re-initialization is non-destructive. No automatic migrations.
4. **Legacy migration:** Explicit preview and approval; copy or move each managed state location with verification, journal and rollback. Preserve existing session IDs and developer-owned documentation. Failed migration must leave legacy `dev` usable, without deleting or rewriting application code or original history.
5. **Global updates:** Signed annotated release verification and SHA-256 integrity, transactional activation, version selection, rollback, and per-project compatibility checks. No release updates that overwrite project files or mix different engine versions. No silent upgrades.
6. **Uninstall:** Project uninstall defaults to keeping durable context; separately confirmed full erase deletes **only clearly owned Gaston state**. Global engine/CLI removal is independent. Protect unrelated provider transcripts, application files, git repository, custom docs and shared configurations.
7. **Cross-platform:** Native `dev` Python 3.11 and 3.13 on Windows/macOS/Linux. Tests cover nested projects, invalid/symlinked paths, concurrent active sessions, interrupted installs/migrations/updates, no-source projects and all CLI commands, plus representative real provider/MCP/hook integration with the user's approval.
8. **Release:** Bump version and sign/tag v1.6.0 **only after all gates pass**. Passing the current 6-job regression matrix without functional acceptance is insufficient.

**Status:** Release contract and automatic project layout detection are only the first implementation step inside PR #9. The other gates must be implemented and verified on the same branch before this draft can be merged.

## PR #9: central project uninstall ownership (implemented on branch, awaiting review)

A source-free project supports `dev uninstall` (and `GASTON_ENGINE_MODE=central dev uninstall`). The default **Keep** option removes only `.gaston/project.json` and preserves project context, local task history, sessions, configuration and user-defined rules. Re-running `dev init` restores the adapter without replacing the kept data.

**Erase** requires an exact `ERASE GASTON DATA` confirmation followed by `UNINSTALL`; it removes the project's `.gaston` data directory only, and rejects symlinks and managed active workers. The project source tree, any old `.agentic` context, global engine, Git history and external provider sessions are excluded from deletion.

This is project-level uninstall only. The independent global engine uninstall and signed updater are still release blockers, and the feature is not sufficient to mark PR #9 ready to merge.
