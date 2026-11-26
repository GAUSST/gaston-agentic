# Gaston v1.6 — centralized engine migration and release acceptance

## Architecture

- **Global engine:** Gaston's Python engine and native `dev` CLI are installed once through the `gaston-agentic-cli` distribution. Runtime modules, internal tests and release workflows are not copied into application projects.
- **Project adapter:** `.gaston/project.json` identifies the project. Gaston-owned project state stays under `.gaston/` (configuration, history, tasks, runs, evidence, custom policies, enrolled sessions, optional development DB credentials and migration backups). Sensitive state is covered by `.gaston/.gitignore`; project-owned `.gaston/context` documents may be versioned at the developer's discretion.
- **Provider adapters:** `.claude/settings.local.json` and `.codex/hooks.json` remain at the standard discovery paths required by Claude Code and Codex. They contain only provider-native hook registrations pointing to the globally installed engine; they are not a second copy of Gaston's runtime or project state. Optional `.serena/` metadata is generated/owned by Serena itself.
- **Legacy compatibility and consolidation:** `dev update` works for both an unmigrated legacy workspace and a project that already has `.gaston/project.json` but still contains old root-level files. It checks the legacy source recursively, but checks only destination paths that correspond to legacy entries; unrelated destination symlinks (for example Playwright links under `node_modules/.bin`) remain untouched and are never followed. In ordinary application workspaces it merges missing state without overwriting conflicts, archives the complete old `.agentic/` tree inside `.gaston/migration-backups/legacy-agentic/`, moves `.gaston-dev-db/` under `.gaston/local/dev-db/`, verifies copies and the central engine, then removes the old root-level folders. **Exception:** in the Gaston source checkout (identified by its package metadata and release workflow), `.agentic/` is the tracked Python package source and is never moved; only the legacy DB directory is consolidated. Conflicts, links or active sessions stop the operation without deleting the source.
- **Update ownership:** signed releases update the versioned global engine store; the same approval also covers safe legacy project consolidation when needed. The installed native CLI is a stable loader; new engine versions are activated through an atomic pointer, and earlier managed versions remain available for rollback.
- **Uninstall ownership:** `dev uninstall` removes the project adapter and preserves context by default. The separately confirmed **Erase** option removes only `.gaston/`, not application code, existing `.agentic` backup data or the global CLI.

## CLI

```sh
# Install the engine once.
python3 .agentic/central_install.py
# Windows PowerShell: py -3 .agentic/central_install.py
# Open a new terminal, then change to the application directory.

# Inside an existing application repository:
dev init
dev models
dev sessions
dev                     # Engineering Lead once setup is complete

# From a centrally managed project:
dev update              # Verify signed release and consolidate legacy state; default Yes
dev update --rollback   # Explicit version rollback
dev uninstall           # Keep Context (default) or confirmed Full Erase
```

Migration verifies the legacy manifest and engine sources when present. A valid central project marker also permits consolidation of a manifest-free directory containing only recognized project state, never unverified toolkit source. It merges project state without overwriting conflicting files, and archives the complete original `.agentic/` directory under `.gaston/migration-backups/legacy-agentic/`. The archive is checksummed before the root-level directory is removed. Optional DB credentials move to `.gaston/local/dev-db/credentials.json`, with the original directory preserved in a nested migration backup. Unrecognized, modified, linked, conflicting or application-owned data is never discarded. An unsuccessful migration leaves the original source available; migration cannot proceed while managed sessions appear active.

If `.gaston/project.json` already exists, `dev update` is still the correct consolidation command. It detects a previously migrated workspace and safely finishes archiving the leftover `.agentic/` directory. In the Gaston source repository, however, `.agentic/` is tracked package code and remains in place. `dev uninstall` with **Keep** is not a cleanup or migration operation; it preserves the project data.

## Release blockers / mandatory verification

The GitHub CI matrix must pass on Windows, macOS and Linux with Python 3.11 and 3.13. The suite must compile every engine module, run full Linux regressions and exercise source-free CLI initialization, installed-engine dispatch, session/model read paths, MCP and hook project identity, migration, uninstall and atomic version-store operations.

**A green matrix alone is insufficient** for full provider acceptance: before a signed v1.6.0 tag, manually verify actual Codex and Claude provider handoffs, enrollment, MCP tools and hooks, interactive setup, interrupted migration/rollback and current signed update integration on each supported host. Do not call a release production ready without evidence of these behaviors. Avoid silently falling back to project-local executable modules.

PR #9 had been merged as a foundation while these capabilities were incomplete. The integration branch must be assessed as a new cohesive release candidate, not as a cosmetic CI fix.