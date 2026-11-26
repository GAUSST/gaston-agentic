# Publishing and updating Gaston

## Central installations (v1.6+)

Close active Gaston sessions, then run `dev update` from an application workspace
or an ordinary directory outside any project. Interactive `dev init` checks for a newer published release and suggests `dev update`; it never installs or migrates. The official
publisher is pinned to GAUSST/gaston-agentic. No publisher configuration is
required for a central installation. The updater checks for a newer published
stable release, verifies the signed annotated tag and exact-commit manifest,
and uses the normal yes/no prompt (default Yes). It stages the verified engine in
the global version store and activates it using a version pointer.

Inside a project, the same confirmation also authorizes safe legacy state consolidation, even when the engine is already current. Existing state is never overwritten; active sessions and conflicts stop migration, and legacy files are archived and verified before removal. Outside a project the update only activates the engine. Application source remains untouched. `dev update --rollback` restores a recorded previous version when
available. An ordinary launch never installs an update automatically. Explicit legacy mode retains its own updater and publisher trust configuration. When upgrading from v1.6.12 or earlier, the old running updater cannot execute the new lifecycle: run `dev update` again inside the project after installing the new version if consolidation is pending.

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
