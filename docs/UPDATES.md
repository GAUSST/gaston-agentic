# Publishing and updating Gaston

## Central installations (v1.6+)

Run `dev update` from anywhere. It always updates the globally installed Gaston engine: it verifies the latest signed annotated release and exact-commit manifest, asks one yes/no question (default Yes), stages the verified engine in the global version store, and activates it through the version pointer.

When invoked inside a central project, `dev update` also refreshes only recognized Gaston hook commands in `.claude/settings.local.json` and `.codex/hooks.json` so they target the active engine. Unrelated provider hooks and trust records are preserved. The updater does not scan project state or recursively inspect project folders. It reads only the central project marker and provider hook settings needed for that refresh. It never moves, migrates, archives, overwrites or deletes project files or folders. Existing `.agentic/` and `.gaston/` contents remain untouched. Running the command outside a project updates only the global engine. `dev update --rollback` restores a recorded previous version and refreshes managed hooks to that version. `dev migrate` is disabled. An ordinary Gaston launch never installs an update automatically. Explicit legacy mode retains its own updater and publisher trust configuration.

## Maintainer release process

1. Merge a release PR only after CI passes on the exact PR head commit.
2. Keep `.agentic/manifest.json` release/build, `pyproject.toml` version and
   release notes synchronized. Refresh every manifest SHA-256 entry for each
   changed manifest-owned file. Run `python .agentic/release_audit.py` and
   the full native CLI validation workflow.
3. Merge the release PR. The `Publish verified main release` workflow runs
   after successful validation of the current main commit. It checks that the
   manifest version is newer than the latest published release, signs the
   exact verified commit with the configured maintainer key, verifies the
   GitHub-verified annotated tag, and publishes the GitHub Release.
4. Confirm the publisher workflow succeeded and the new signed GitHub Release
   is visible before asking users to update. A merged PR or a green PR check
   alone does not make an update available.
5. On an existing central installation, close active Gaston sessions and run
   `dev update`. Approve the verified release, restart Gaston, and verify the
   loaded engine version and preserved project context.

The publisher workflow skips a release when the manifest version is not newer
than the latest published release. If signing configuration is missing, the
publisher fails rather than creating an unsigned release. Never replace or
force-move an existing release tag.

## Integrity

The updater uses GitHub's exact-commit source snapshot, never the moving main
branch. A checksum alone is not proof of publisher authenticity. Secure the
repository and its signing keys. No update may implicitly initialize projects,
overwrite application files or silently discard project context. Do not rename
or change the established CLI commands. Cross-platform CI does not establish
authenticated provider or application behavior; certify those separately.
