# Gaston CLI — stable contract

Gaston is **project-agnostic**: it detects the existing project and configures
the corresponding workflow. Nothing is hardwired to an example application.

The established commands are unchanged:

- `dev init`: explicit guided installation and initialization for a project.
- `dev`: start or resume the engineering Lead, **without** implicitly initializing.
- `dev models`: select model/provider settings.
- `dev sessions`: inspect and manage sessions.
- `dev uninstall`: remove Gaston from this project with confirmation.
- `dev update`: optional check/review/install of a signed official release.

On Linux/macOS the repository-local `./dev` shim remains available. It can
register a per-user `dev` launcher if none exists without installing Python
packages or modifying system Python; it does not run `dev init` automatically.
On Windows the Python console-script entry point maps to `dev.exe`, which runs
the same dispatcher. A standalone, no-Python Windows bootstrap is **not yet
implemented or validated**; do not claim it is. Real Windows/macOS release
validation is required before a cross-platform certification.

## Trusted updates

An official public source does not exist until the maintainer publishes it.
After creating the repository, each installation explicitly pins its publisher
with `dev update --set-repo OWNER/REPO` once. Until then `dev update` reports
that no repository is configured and never downloads a release.

`dev` only displays cached update notices and optionally starts an isolated,
non-blocking check (no more than daily); it never installs updates. `dev update
--check` fetches metadata only, while `dev update` verifies a signed annotated
Git tag, downloads its exact commit source snapshot, verifies each manifest-owned
file, checks active sessions and modifications, requests exact user consent, and
installs via a recovery journal with rollback. It does not alter the current
application repository, local history, or credentials. The updater requires an
**actual published GitHub release with a verified signed tag**, not merely a Git
tag, a ZIP attachment, or an unsigned `main` branch.
