# Gaston v1.6 — centralized engine migration and release acceptance

## Architecture

- **Global engine:** Gaston's Python engine and native `dev` CLI are installed once through the `gaston-agentic-cli` distribution. Runtime modules, internal tests and release workflows are not copied into application projects.
- **Project adapter:** `.gaston/project.json` identifies the project. Other project-owned state stays under `.gaston/` (configuration, history, tasks, runs, evidence, custom policies and enrolled sessions). Sensitive state has a scoped `.gaston/.gitignore`; project-owned `.gaston/context` documents may be versioned at the developer's discretion.
- **Legacy compatibility:** projects still containing an original `.agentic/launcher.py` continue to run the old engine until the developer explicitly migrates them. Automatic initialization never deletes old sources or project documents.
- **Update ownership:** signed releases update the versioned global engine store, never an application repository. The installed native CLI is a stable loader; new engine versions are activated through an atomic pointer, and earlier managed versions remain available for rollback.
- **Uninstall ownership:** `dev uninstall` removes the project adapter and preserves context by default. The separately confirmed **Erase** option removes only `.gaston/`, not application code, existing `.agentic` backup data or the global CLI.

## CLI

```sh
# Install the engine once.
python3 .agentic/central_install.py
# Windows PowerShell: py -3 .agentic/central_install.py
# Open a new terminal, then change to the application directory.

# Inside an existing application repository:
dev init
dev init --setup       # Configure tools/providers explicitly, with approval
dev models
dev sessions
dev                     # Engineering Lead once setup is complete

# Existing legacy .agentic project:
dev migrate             # Explicit, reviewed legacy-to-central migration

# From a centrally managed project:
dev update --check
dev update              # Review cryptographically verified release; confirm installation
dev update --rollback   # Explicit version rollback
dev uninstall           # Keep Context (default) or confirmed Full Erase
```

Migration copies local context with checksum verification, verifies the centrally installed entrypoint, then removes only original engine sources whose hashes match the legacy manifest and whose module filenames are recognized by the installed toolkit. Original local data remains as a compatibility backup. Unrecognized, modified, linked, or application-owned files are never deleted automatically. An unsuccessful migration must leave original history available; migration cannot proceed while managed sessions appear active.

## Release blockers / mandatory verification

The GitHub CI matrix must pass on Windows, macOS and Linux with Python 3.11 and 3.13. The suite must compile every engine module, run full Linux regressions and exercise source-free CLI initialization, installed-engine dispatch, session/model read paths, MCP and hook project identity, migration, uninstall and atomic version-store operations.

**A green matrix alone is insufficient** for full provider acceptance: before a signed v1.6.0 tag, manually verify actual Codex and Claude provider handoffs, enrollment, MCP tools and hooks, interactive setup, interrupted migration/rollback and current signed update integration on each supported host. Do not call a release production ready without evidence of these behaviors. Avoid silently falling back to project-local executable modules.

PR #9 had been merged as a foundation while these capabilities were incomplete. The integration branch must be assessed as a new cohesive release candidate, not as a cosmetic CI fix.