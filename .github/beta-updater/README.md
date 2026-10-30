# Beta updater

Proposes a pull request against `beta` when QVAC publishes a newer, verified
staging Flatpak. A human reviews and merges it. The updater never merges, never
enables auto-merge and never closes pull requests. The production manifest on
`master` is only read.

## How it works

`.github/workflows/beta-updater.yml` runs every 4 hours and on manual dispatch,
and only for `flathub/io.tether.QVAC.Workbench` on `refs/heads/master`. Code is
taken from the trusted `master` commit; the `beta` checkout is data (read as
text, never executed or YAML-parsed). The only credential is the job's
`GITHUB_TOKEN` (`contents: write`, `pull-requests: write`); no secrets are used.

`update_beta.py` (standard library only) has three subcommands:

### `update`

1. Lists `https://static.tether.dev/qv.ac/` (the same listing the production
   checker reads) and keeps `beta-X.Y.Z-rc.N.B/` directories. The newest by
   numeric order wins (`rc.10` > `rc.9`); `beta/`, `latest/` and stable
   `X.Y.Z/` are ignored, so stable discovery is untouched.
2. Does nothing (exit 0) when that version is already in beta (`duplicate`),
   older than beta (`older`), already has a trusted updater PR (`proposed`), is
   not listed (`no-publication`), or any required file is still missing
   (`incomplete`, retried next run). Only the newest version is considered.
3. Verifies `beta-<v>/flatpak/` for x64 and arm64: `QV.AC-<arch>-flatpak.tar.gz.sha512`
   is `<sha512>  <name>` and `io.tether.qvac.source-<arch>.yml` is the canonical
   snippet pointing at that immutable URL with the same digest. Then it downloads
   the x64 archive, re-hashes it, and lists entry names (nothing is extracted)
   to confirm `<app-id>.desktop`, `.metainfo.xml`, `.png`, `run.sh` and `main/`
   exist. The manifest must reference the same names, so an app-ID change that
   is not matched in the archive stops the update. The arm64 archive is not
   downloaded or consumed.
4. Rewrites only the `qvac-workbench` archive `url`, `sha512` and `x-checker-data`
   (line-based, verified by re-reading). A verification failure exits 1 and the
   run is red; nothing is written.

**Bootstrap:** if the beta manifest has no `beta-<v>/flatpak/` source URL yet
(today's obsolete packaging), the base is the production manifest on `master`
(runtime, wrapper and everything else unchanged). Afterwards the base is the
beta manifest itself, so beta-only human edits survive.

### `proposals`: which pull requests count

Before `update`, the workflow reads the newest 100 pull requests into `beta`
(`gh api repos/<repo>/pulls`, GET only) and `proposals` keeps the versions that
already have one. A pull request counts, in any state (open, closed or merged),
only if all of these hold:

- its head branch is `beta-update/<X.Y.Z-rc.N.B>` in **this repository**
  (`head.repo.full_name`), so never a fork, including a closed or deleted one;
- it targets `beta`;
- its author is `github-actions[bot]` (`user.login`, `user.type == "Bot"`, and
  id `41898282`), which is who `GITHUB_TOKEN` pull requests are authored by.

A fork, a human on a same-prefix branch, or another bot can neither suppress an
update nor be touched by it. REST objects are used rather than `gh pr list
--json author` because the REST `user` format is documented and the gh CLI's
rendering of bot logins could not be verified offline. A closed bot PR still
counts, so a version a maintainer declined is not proposed again; the next RC
gets a new PR.

### `plan-branch`: branch handling and crash retries

After a verified update the workflow runs `plan-branch`, which stages only the
manifest and inspects `beta-update/<version>` on the remote:

- **absent:** prints `create`; the workflow commits and pushes it (never forced).
- **present and exactly the verified update**, meaning one commit whose parent is
  the current `beta` head and whose tree equals the verified manifest: prints
  `reuse`. This is a retry after a crash between push and PR creation; the PR
  body and patch are built from that same verified manifest, so they describe the
  actual branch.
- **present but anything else** (other content, extra commits, a different or
  older `beta` base, a merge): fails with the reason and repair steps. Nothing is
  pushed, forced or rewritten. Inspect the branch, delete it
  (`git push origin --delete beta-update/<version>`), and re-run.

The pull request is opened only if no pull request exists on that exact head
(looked up by head, so a flood of other PRs cannot hide it). This second lookup
is `proposals --verified-head <branch>` and is deliberately looser than the bulk
filter above: it counts any author, open or closed, but only a PR whose head is
that exact `beta-update/<strict version>` branch in **this repository** and that
targets `beta`. A fork with the same branch name never counts, since
`head.repo.full_name` differs. Ignoring the author is sound only here, after
`plan-branch` has verified the branch tip is exactly the verified update on the
current `beta` head: a PR on that head, whoever opened it, proposes or declines
that very content. So a maintainer who opens the PR by hand after a failed bot
run is not duplicated, and one who closes it to decline is not overridden. The
bulk filter stays bot-only because nothing is verified when it runs; a manual PR
is therefore invisible to it, and scheduled runs keep re-verifying that version
(including the x64 download) without opening anything until the next RC. The
diff is always uploaded as a `beta-update-<version>` artifact.

### Superseded pull requests

Nothing is closed automatically. When a newer RC is proposed, close older
updater PRs by hand. Both edit the same manifest lines, so GitHub normally
reports a conflict on the second one merged instead of silently reverting beta.
If a PR is closed rather than merged, that version is not proposed again.

## Operating

- Repository settings: Actions workflow permissions must allow creating pull
  requests. PRs opened with `GITHUB_TOKEN` do not trigger other workflows, so
  start Flathub test builds the usual way. Scheduled workflows are disabled after
  60 days without repository activity.
- Run now: Actions > Beta updater > Run workflow. Choose `patch` to get only the
  artifact (no branch, no PR). Set the repository variable `BETA_UPDATER_MODE=patch`
  to make scheduled runs patch-only, e.g. if Flathub policy objects to bot PRs.
- Local check, no writes: `python3 -I .github/beta-updater/update_beta.py update --master io.tether.QVAC.Workbench.yml --beta-dir <beta checkout> --dry-run`
- Tests: `python3 -I -m unittest discover -s .github/beta-updater/tests -v`.
  They need no network and download no real archive. The branch tests run real
  `git` against throwaway local repositories and are skipped if `git` is missing.

## Contract with QVAC

QVAC uploads immutable `qv.ac/beta-<X.Y.Z-rc.N.B>/flatpak/` with, per arch,
`QV.AC-<arch>-flatpak.tar.gz`, its `.sha512` and `io.tether.qvac.source-<arch>.yml`,
then promotes the managed `beta` link. Changing any name or format means changing
`update_beta.py` and its tests together.

## AI assistance

The updater, its tests, the workflows and this document were written with AI
assistance and are subject to maintainer review in the pull request that adds
them. No manifest content is AI-authored: the beta manifest is the human-authored
production manifest with only `url`, `sha512` and `x-checker-data` replaced by
deterministic code from a verified publication. No manifest or template is
committed here.
