# Updaters

Flathub's hosted checker only scans the default branch. These two workflows run the
same tool, the **official**
[flatpak-external-data-checker](https://github.com/flathub-infra/flatpak-external-data-checker),
pinned by image digest, so that `beta` is checked too and `master` can be checked by
hand. The checker alone chooses versions, downloads and hashes archives, rewrites the
source url and sha512, commits, and opens the pull requests. Nothing here selects a
version, computes a hash or merges.

## Beta updater

`.github/workflows/beta-updater.yml` runs **every hour** and by hand, only from `master`.
The workflow code comes from the trusted `master` commit; `beta` is checked out beside it
as a named branch (the checker bases its PR on the current branch) and is only data.

1. **Prepare** (`prepare-beta.sh`, run in the same pinned image). Only if beta's manifest
   has no `x-checker-data`, as today's obsolete recipe: the beta manifest becomes the
   human-authored production manifest from `master` with **only** its checker's
   `version-pattern` and `url-template` switched to staging discovery
   (`beta-X.Y.Z-rc.N.B/` in the root listing, archive at
   `beta-<version>/flatpak/QV.AC-x64-flatpak.tar.gz`), and beta's `flathub.json` gets
   `"automerge-flathubbot-prs": false` (manual review and merge) unless beta already sets it
   to an explicit `true` or `false`, which is kept as it is. A `flathub.json` that is not a
   JSON object, or whose setting is not a boolean, stops the run before anything is changed.
   This is an edit of the working tree. The source url and sha512 stay production's real
   ones, never invented, until the checker verifies a staged RC. Beta that already has
   staging checker data is not touched, so human edits to its recipe, comments and
   `flathub.json` survive; checker data without the staging template stops the run for a
   human.
2. **Check** with `--update --never-fork --filter-type archive`. The checker picks the
   newest RC, downloads and hashes it, rewrites url and sha512, commits to
   `update-beta-<hash>` and opens a `[beta]` PR. It looks up PRs for that branch, so an
   open, merged or declined one is not duplicated.

**First beta.** No staged archive is published yet, so every run fails visibly because the
listing has no `beta-*` directory to match: nothing is committed and no PR is opened, and
the prepared edit is never committed on its own. Once QVAC lists a real RC, the first run
opens one PR that carries everything: the production recipe, the staging checker data, the
`flathub.json` setting and the real archive url and hash. No separate setup PR is needed.

## Production updater

`.github/workflows/production-updater.yml` is manual only, for when Flathub's central bot
is not running. It checks out `master` as a named branch and runs the same checker with
`--update --never-fork --filter-type archive`, so the PR is based on `master` and the branch
is `update-master-<hash>`, like the hosted checker's. The production manifest and
`flathub.json` are used exactly as they are: no preparation, no config edit, no automerge
override. The current `flathub.json` omits `automerge-flathubbot-prs`, so production update
PRs are reviewed and merged by hand.

## Merging: manual by default

Beta PRs are reviewed and merged by hand. When beta's `flathub.json` does not set
`automerge-flathubbot-prs` (the live beta has none), the preparation sets it to `false`, so
the checker never merges an updater PR on beta.

**Which setting applies.** The checker reads `automerge-flathubbot-prs` from the
`flathub.json` in the beta checkout it runs on, that is the base branch `beta`, not from a
PR. Editing only an open PR's `flathub.json` therefore does **not** control whether that PR
is merged. The preparation keeps an explicit `true` or `false` already on beta, even before
the first update, and only a missing setting becomes `false`. When beta has no
`flathub.json` at all, production's is only a starting point: its automerge value, whatever
it is, is replaced by `false`.

**Turning automerge on later.** Get `"automerge-flathubbot-prs": true` onto `beta` itself (a
direct commit or a merged PR); every later run keeps it. The checker may then merge a beta
PR, but only precisely like this: opening a PR never merges it. On a **later** hourly or
manual run that finds the same PR still open (by its `update-beta-<hash>` branch), it merges
it, with a rebase and deleting its branch, if the PR head commit's combined **commit status**
is `success` and the PR is mergeable. Commit statuses are what Flathub's build bot posts;
Actions check runs do not count, and a commit without statuses never counts. Merging also
needs what the repository provides outside this code: the built-in `GITHUB_TOKEN` (no other
token is used), no branch protection that blocks it, and Flathub's builds running.

**This needs Flathub's exception.** Flathub's linter (`flatpak-builder-lint`,
`flathub-json-automerge-enabled`) reports an error whenever `automerge-flathubbot-prs` is
`true`, and QVAC has no exception, so enabling it requires an exception from Flathub first.
Until one exists, builds of a beta with the flag on do not pass and nothing merges. This
setup does not bypass, hide or disable that check. To go back to manual merging, set `false`
on `beta`; to stop one PR that already exists, do that first, or close the PR (a closed
updater PR is not reopened).

## Limits

- The checker hashes whatever the newest listed `beta-*` directory serves, so QVAC must
  list a directory only when its archive is complete.
- While a beta PR stays open, each hourly run downloads the same archive again and finds
  the same PR. Close superseded updater PRs by hand.
- The checker can push `update-beta-<hash>` and then fail to open the PR (for example
  when Actions may not create pull requests). A retry commits again with a new timestamp
  to the same branch name, and the plain push is rejected as non-fast-forward. Recovery:
  inspect the `update-beta-<hash>` branch that has no PR, delete that orphan with
  `git push origin --delete update-beta-<hash>`, and rerun. The same applies to
  `update-master-<hash>`.
- PRs opened with `GITHUB_TOKEN` do not start other workflows, so start Flathub test
  builds the usual way. Actions must be allowed to create pull requests, and scheduled
  runs stop after 60 days without repository activity.

## Maintaining

- Bump the checker by changing the image digest in the workflows. The tests read the
  image, helper and arguments from the workflows, so they run exactly what runs.
- Tests: `python3 -I -m unittest discover -s .github/beta-updater/tests -v`. They need
  `docker`, `git` and Python only. Every container runs with `--network none`, no token, and
  a fixture website served inside it, using `--edit-only` and `--commit-only` and the real
  `prepare-beta.sh`. Without Docker they are skipped unless `BETA_UPDATER_REQUIRE_DOCKER`
  is set (CI sets it). If `.tools/official-review/` holds a snapshot of the live beta
  recipe, the preparation test uses it; otherwise a small stand-in of the same shape.

## AI assistance

The workflows, the helper, the tests and this document were written with AI assistance and
are subject to maintainer review. No packaging recipe is authored here: the beta recipe is
the human production manifest with two checker lines changed, and the source url and hash
come from the official checker.
