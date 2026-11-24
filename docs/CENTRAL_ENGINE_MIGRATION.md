# Central Gaston engine — phased v1.6 migration

**Current stage: packaged engine and opt-in core dispatch preview.** Do not treat this as a completed global-runtime migration.

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

## This PR implements
- Python wheel includes all core modules under `gaston_engine` next to the native `dev` executable, so engine code can live outside application repositories.
- `project_layout` makes project versus engine path boundaries explicit, recognizes legacy and future central project markers and does not initialize projects implicitly.
- New layout and packaging tests; CI verifies that installed wheels contain engine modules on all three systems.
- The installed CLI retains legacy dispatch by default. For matching-version existing workspaces only, `GASTON_ENGINE_MODE=central dev --help` executes the installed launcher and `eng.py` instead of the project's copies. **Only `--help` is allowed in central preview**; all stateful operations are refused. The read-only preview bypasses workspace evolution locks (fixing Windows permission failures), refuses uninitialized projects and does not trigger local evolution.
- The Linux full-suite test now locates the actual installed engine package instead of reading source-relative `.egg-info` file lists, which incorrectly failed PR #6's post-merge CI.

**Do not manually delete `.agentic` or tag v1.6 until remaining gates pass.** The core execution preview does not yet repoint sidecar scripts, hooks, provider integrations, central updates or project state; it must not be used as evidence that source-free projects are functional.
