# Beta updater

Flathub's hosted checker only scans the default branch. This runs the same tool,
the **official**
[flatpak-external-data-checker](https://github.com/flathub-infra/flatpak-external-data-checker),
against `beta` too. Two pull requests make it work:

1. **This workflow, on `master`.** Every 4 hours (and optionally by hand) it checks
   out `beta` as a named branch and runs the pinned official image with
   `--update --never-fork --filter-type archive`. Nobody has to run CI. The checker
   picks the newest RC, downloads and hashes the archive, rewrites the source url and
   sha512, commits to `update-beta-<tree>` and opens a `[beta]` PR for review. It looks
   up existing PRs for that branch, so an open, merged or declined PR is not
   duplicated. Download, hash and HTTP errors fail the run visibly. Nothing here merges.
2. **The beta manifest, on `beta`.** The actual packaging: the human production recipe
   plus `x-checker-data` for staging discovery (`beta-X.Y.Z-rc.N.B/` directories in the
   root listing `https://static.tether.dev/qv.ac/`, archive at
   `beta-<version>/flatpak/QV.AC-x64-flatpak.tar.gz`), a real pinned staging archive
   url and sha512, and `"automerge-flathubbot-prs": false` in `flathub.json`. The
   workflow does no bootstrap or migration; that PR carries it.

## Setup guard

Before the checker runs, a step checks that beta's manifest has the staging
`url-template` and that its `flathub.json` sets `automerge-flathubbot-prs` to
`false`. If either is missing the run **fails** with an explanation instead of
passing as a no-op, so an unconfigured beta cannot look ready. Merge the beta
manifest PR first; until then scheduled runs fail at this step, by design.

## First archive

That manifest PR pins an authentic CI staging archive, never a placeholder, so it
stays a draft until QVAC has published a matching archive and the website lists its
`beta-<version>/` directory. After it merges, a run with no listed `beta-*`
directory fails because the checker finds nothing to match.

## Limits

- The checker hashes whatever the newest listed `beta-*` directory serves, so QVAC
  must list a directory only when its archive is complete.
- Close superseded updater PRs by hand. PRs opened with `GITHUB_TOKEN` do not start
  other workflows, so start Flathub test builds the usual way. Actions must be
  allowed to create pull requests, and scheduled runs stop after 60 days without
  repository activity.
- The checker can push `update-beta-<hash>` and then fail to open the PR (for example
  when Actions may not create pull requests). A retry commits again with a new
  timestamp to the same branch name, and the plain push is rejected as
  non-fast-forward. Recovery: inspect the `update-beta-<hash>` branch that has no PR,
  delete that orphan with `git push origin --delete update-beta-<hash>`, and rerun.
- The guard uses `jq`, which the GitHub-hosted runners provide.

## Maintaining

- Bump the checker by changing the image digest in the workflow. The tests read the
  image and arguments from the workflow, so they run exactly what runs.
- Tests: `python3 -I -m unittest discover -s .github/beta-updater/tests -v`. They need
  `docker`, `jq` and `bash`, use `--edit-only` / `--commit-only` against a fixture
  server inside the container (no network, no token), run the workflow's guard step on
  fixtures, and are skipped without Docker unless `BETA_UPDATER_REQUIRE_DOCKER` is set
  (CI sets it).

## AI assistance

The workflows, the tests and this document were written with AI assistance and are
subject to maintainer review. They contain no packaging recipe.
