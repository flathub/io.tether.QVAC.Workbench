# Beta updater

Keeps `beta` current with QVAC staging Flatpaks using the **official**
[flatpak-external-data-checker](https://github.com/flathub-infra/flatpak-external-data-checker)
(Flathub's hosted run only checks the default branch). It opens pull requests
against `beta` for human review; nothing here merges.

## Flow

`.github/workflows/beta-updater.yml` runs every 4 hours and on manual dispatch,
only from `master` of this repository:

1. **Wait for the first archive.** `prepare-beta.sh ready` sends a HEAD request for
   `https://static.tether.dev/qv.ac/beta/flatpak/QV.AC-x64-flatpak.tar.gz`. A 404
   means nothing is published yet: the run succeeds and stops. A 200 continues.
   Any other status or a transport error fails the run.
2. **Check out `beta`** as a named local branch (the checker bases its PR on it).
3. **Prepare beta** (`prepare-beta.sh bootstrap`):
   - If the beta manifest has no `x-checker-data` (today's obsolete recipe), it is
     replaced by the human-authored production manifest from `master`, changing only
     the checker's `version-pattern` and `url-template` to staging discovery:
     `beta-X.Y.Z-rc.N.B/` directories in the same root listing, archive at
     `beta-<version>/flatpak/QV.AC-x64-flatpak.tar.gz`. The seed still carries
     production's real url and sha512; nothing is invented.
   - If it already has checker data, the recipe is left alone, so human beta edits
     survive. Checker data without the staging template stops the run for a human.
   - `automerge-flathubbot-prs` is forced to `false` in `flathub.json` every run.
4. **Run the official checker**, pinned by image digest, with
   `--update --never-fork --filter-type archive`. It picks the newest RC, downloads
   and hashes the archive, rewrites the url and sha512, commits to
   `update-beta-<tree>` and opens a `[beta]` PR. It looks up existing PRs for that
   branch, so an open, merged or declined PR is never duplicated.

## First beta

Until QVAC exposes the first beta, every run is a green no-op. After that, the first
run opens one PR: the production recipe, the staging checker config, the real
archive url and hash, and the `flathub.json` setting. The seed edits are committed
only together with a real update, never alone. While that PR is open, later runs
repeat the same download and find the same PR; once merged, runs only move the
source on a newer RC.

## Limits

- The checker hashes whatever the newest listed `beta-*` directory serves, and the
  guard only sees the promoted `beta` link. QVAC must list a directory only when its
  archive is complete (upload, then promote).
- The guard needs the static host to answer HEAD.
- Only the `automerge-flathubbot-prs` key of beta's `flathub.json` is managed; check
  its other settings (such as `only-arches`) in the first PR.
- Close superseded updater PRs by hand. PRs opened with `GITHUB_TOKEN` do not start
  other workflows, so start Flathub test builds the usual way. Actions must be allowed
  to create pull requests, and scheduled runs stop after 60 days without activity.

## Maintaining

- Bump the checker by changing the image digest in the workflow; the tests read the
  image and arguments from the workflow, so they exercise exactly what runs.
- Tests: `python3 -I -m unittest discover -s .github/beta-updater/tests -v`. They need
  `docker`, `jq`, `curl` and `bash`, use `--edit-only` / `--commit-only` against a
  fixture server inside the container (no network, no token), and are skipped without
  Docker unless `BETA_UPDATER_REQUIRE_DOCKER` is set (CI sets it).

## AI assistance

The workflows, `prepare-beta.sh`, the tests and this document were written with AI
assistance and are subject to maintainer review. No manifest content is AI-authored:
the beta recipe is the human-authored production manifest with only the two checker
lines changed, and the url and hash come from the official checker.
