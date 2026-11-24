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

1. Verify the release PR and require passing CI on the exact commit to release.
   Publish only toolkit files, never application code, credentials, transcripts,
   or private project state.
2. Fetch the verified commit from main. Check that the manifest release and
   build agree with pyproject.toml and the intended version.
3. Using the maintainer's registered signing key, create a cryptographically
   signed annotated tag for that exact commit (example for v1.6.1):

   ```sh
   git fetch origin main
   git tag -s v1.6.1 VERIFIED_COMMIT_SHA -m "Gaston v1.6.1"
   git push origin refs/tags/v1.6.1
   ```

   Replace VERIFIED_COMMIT_SHA with the exact reviewed commit SHA. Do not sign
   a stale checkout, move existing release tags, or create unsigned tags.
4. The existing release.yml workflow validates tag identity, the full manifest,
   regression tests and installation smoke tests on Linux, macOS and Windows.
   It verifies the signature before publishing a GitHub Release.
5. On an existing v1.6.0 central installation, run `dev update` and approve
   v1.6.1 only after the signed release has been published. Restart Gaston
   and verify the loaded engine version and existing project context.
   A merged PR alone is not an available update.

## Integrity

The updater uses GitHub's exact-commit source snapshot, never the moving main
branch. A checksum alone is not proof of publisher authenticity. Secure the
repository and its signing keys. No update may implicitly initialize projects,
overwrite application files or silently discard project context. Do not rename
or change the established CLI commands. Cross-platform CI does not establish
authenticated provider or application behavior; certify those separately.
