# Publishing and updating Gaston

## Central installations (v1.6+)

Close active Gaston sessions, then run `dev update` from an application workspace
or an ordinary directory outside legacy toolkit checkouts. The official
publisher is pinned to GAUSST/gaston-agentic. No publisher configuration is
required for a central installation. The updater checks for a newer published
stable release, verifies the signed annotated tag and exact-commit manifest,
and asks for explicit installation approval. It stages the verified engine in
the global version store and activates it using a version pointer.

Project-owned .gaston state, sessions and application source are not part of
the update. `dev update --rollback` restores a recorded previous version when
available. An ordinary launch never installs an update automatically. Legacy
project-local installations retain their own updater and explicit publisher
trust configuration; do not apply legacy overlay instructions to central
installations.

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
