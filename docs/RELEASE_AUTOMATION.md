# Automatic signed releases

This is a repository-maintainer setup, not an installation or update step for Gaston users.

After successful native CLI validation of the current main commit, the publication workflow detects a new manifest version, signs its exact commit, checks GitHub's verified annotated tag and publishes the release. Pull-request runs, failed runs, stale main commits and already published versions do not publish. Existing tags are never replaced. The current client signature policy is unchanged.

## Configure once

In the official repository's Settings → Secrets and variables → Actions, configure:

- Repository secret `GASTON_RELEASE_SIGNING_KEY`: ASCII-armored private OpenPGP release-signing key (prefer a dedicated key, not your personal key).
- Repository secret `GASTON_RELEASE_SIGNING_PASSPHRASE`: key passphrase, if applicable.
- Repository variable `GASTON_RELEASE_SIGNING_FINGERPRINT`: full signing key fingerprint.
- Repository variable `GASTON_RELEASE_SIGNING_EMAIL`: GitHub-verified email associated with the signing key.

Register the corresponding public key with GitHub under the trusted maintainer/bot account. Protect main and the release workflow; restrict repository write access and signing-secret management. Never paste a private key into chat, commit it, or include it in a project. Rotate or revoke the dedicated key when needed.

The existing signed-tag workflow remains available for manually signed releases. Automatic publication runs directly after native validation because pushes made using the GitHub Actions token do not recursively trigger tag workflows.

After the one-time setup, merge a versioned PR with successful checks. The main validation and publication workflows handle the release. If setup was completed after the merge, manually run the existing Gaston native CLI validation workflow on main once to trigger publication.

Users only run `dev update` from their project after the release is published. They never need the source checkout or signing credentials.

These two repository-only files are intentionally outside the install manifest: old clients enforce a fixed allowed-path list, and the publication workflow is not part of the installed runtime.
