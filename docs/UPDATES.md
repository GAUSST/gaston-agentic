# Publishing and updating Gaston v1.5.4

## Trust and distribution

Gaston is **generic software**. Never publish application source, `.agentic/local/`,
`.agentic/config.json`, `.env`, credentials, transcripts, task evidence or
provider configuration from an installed project. Publish only the audited
source release tree provided here (not a working application repository).

The public source repository is **https://github.com/GAUSST/gaston-agentic**.
It is the only intended official update publisher. Before publishing a verified release:

1. Verify the published source contains only toolkit-owned files and no secrets.
2. Require CI and protect the `main` branch and release tags.
3. Create a **cryptographically signed annotated** tag `v1.5.4` pointing to the verified release commit, then push the tag.
   **Do not sign a stale local checkout:** `git fetch origin main` first;
   confirm `git rev-parse HEAD` matches `git rev-parse origin/main` and the
   committed manifest has `release: v1.5.4`. Sign that exact verified SHA
   (`git tag -s v1.5.4 VERIFIED_COMMIT_SHA -m 'Gaston v1.5.4'`).
   Configure signature verification on GitHub beforehand.
4. The `release.yml` workflow checks tag/build identity and audit/test gates
   across all three systems before publishing a GitHub Release. No ZIPs are
   manually created for subsequent updates; GitHub's exact-commit source
   snapshot is used by the updater.
5. On an existing installation, run `dev update --set-repo GAUSST/gaston-agentic` once.
   The installer requires the user to type the exact publisher name to trust
   it. `dev update --check` reports a new stable release; `dev update` verifies,
   asks for consent, applies, and records a recovery snapshot.

A public repository has no authentication requirement for reading its releases.
Trust is not established by a checksum alone: the updater pins a verified
signed tag, corresponding commit SHA and manifest hashes. Repository maintainers
and their signing keys must themselves be secured.

## Update detection

`dev` never runs `init` or installs updates. On each normal launch it may display
a *cached* notification, and if due it starts a detached network check. It never
blocks a session on GitHub availability. Successful checks are limited to once
per 24 hours; failure retries are throttled. `dev update --check` is explicit.

## Safety and rollback

A candidate is accepted only if its tag, build number, manifest file list,
sha256 hashes, and source archive agree. Before activation the updater checks
that installed toolkit-owned files match their manifest, no managed tasks are
active, and no local evolution is pending. It backs up the old toolkit and
records a journal before writing. A failed or interrupted activation is rolled
back before another `dev` command starts; a corrupt backup prevents further
activation rather than overwriting arbitrary content. Local Gaston state,
application source, application `.git/`, sessions, credentials and project
configuration are not part of the signed toolkit payload.

The updater does **not** fetch the moving `main` branch or change the
application Git working tree. If the publisher is unconfigured, or a signed
release has not yet been published, `dev update` does nothing.

## Compatibility

Do not rename or change the semantics of `dev init`, `dev`, `dev models`,
`dev sessions`, or `dev uninstall`. `dev update` is an addition only.
Releases must include migration testing from supported prior release trees
without deleting project-local data. Runtime parity must be validated on real
Windows and macOS runners; Linux-only tests are not a certification.
