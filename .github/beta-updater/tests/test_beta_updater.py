"""Tests for the beta and production updater workflows.

Version selection, download, checksums, commits and pull requests are done by the OFFICIAL
flatpak-external-data-checker, so only the wiring is tested here. Everything runs in the exact
image the workflows pin, with the exact arguments and helper they use: the checker with
--edit-only / --commit-only against a fixture HTTP server started inside the container, and
prepare-beta.sh itself. All containers run with --network none and no token. The workflow
contracts are checked on the host. Docker is required; the image tests are skipped without it
unless BETA_UPDATER_REQUIRE_DOCKER is set (CI sets it).
"""

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
HELPER = REPO / ".github" / "beta-updater" / "prepare-beta.sh"
REVIEW = REPO / ".tools" / "official-review"
BETA = (WORKFLOWS / "beta-updater.yml").read_text(encoding="utf-8")
PRODUCTION = (WORKFLOWS / "production-updater.yml").read_text(encoding="utf-8")
MANIFEST = "io.tether.QVAC.Workbench.yml"
TRUSTED_STEP = "Check out the trusted master commit (code and production recipe)"
PREPARE_STEP = "Prepare beta for the official checker (first run only)"
CHECK_STEP = "Run the official flatpak-external-data-checker"


def step(workflow, name):
    start = workflow.index(f"- name: {name}")
    end = workflow.find("\n      - name:", start + 1)
    return workflow[start:] if end < 0 else workflow[start:end]


def step_input(workflow, name, key):
    return re.search(rf"(?m)^\s+{key}: (.+)$", step(workflow, name)).group(1)


def code(workflow):
    """The workflow without its comment lines."""
    return "\n".join(line for line in workflow.splitlines() if not line.strip().startswith("#"))


IMAGE = re.search(r"uses: docker://(\S+)", BETA).group(1)
HELPER_ENTRYPOINT = step_input(BETA, PREPARE_STEP, "entrypoint")
HELPER_ARGS = step_input(BETA, PREPARE_STEP, "args").split()
BETA_ARGS = step_input(BETA, CHECK_STEP, "args").split()
PRODUCTION_ARGS = step_input(PRODUCTION, CHECK_STEP, "args").split()

LOCAL_ROOT = "http://127.0.0.1:8080/qv.ac/"
PRODUCTION_PATTERN = r'          version-pattern: href="\./((?:\d+\.)+\d+)/"'
BETA_PATTERN = r'          version-pattern: href="\./beta-(\d+\.\d+\.\d+-rc\.\d+\.\d+)/"'
PRODUCTION_TEMPLATE = f"          url-template: {LOCAL_ROOT}$version/QV.AC-x64-flatpak.tar.gz"
BETA_TEMPLATE = f"          url-template: {LOCAL_ROOT}beta-$version/flatpak/QV.AC-x64-flatpak.tar.gz"
OBSOLETE_FLATHUB = '{"only-arches": ["x86_64"]}\n'
# Today's live beta: a GNOME 49 recipe with no checker data. The real snapshot is used when
# present (.tools is not in Git); this is the same shape, with the live url and checksum.
OBSOLETE_BETA = """\
id: io.tether.QVAC.Workbench
runtime: org.gnome.Platform
runtime-version: '49'
sdk: org.gnome.Sdk
command: qvac_workbench
base: org.electronjs.Electron2.BaseApp
base-version: '25.08'
modules:
  - name: qvac_workbench-x64
    only-arches:
      - x86_64
    buildsystem: simple
    build-commands:
      - install -Dm755 qvac_workbench $FLATPAK_DEST/bin/qvac_workbench
    sources:
      - type: archive
        url: https://static.tether.dev/qvac/workbench/flatpak-assets-1.0.0.tar.gz
        sha512: bce96c60c1c49124fc83fda9034466691e12c8afbf26cf6ce0e24b44e3581665bd7123a06b4f0cc7b32e34d0f959ac219cd48fe62b78a68c315c236da1eb5273
        strip-components: 0
"""
# Serve the fixture website from inside the container, then run the checker: no network at all.
SERVE_AND_RUN = textwrap.dedent(
    """
    python3 -m http.server 8080 --bind 127.0.0.1 --directory /work/site > /dev/null 2>&1 &
    for i in 1 2 3 4 5 6 7 8 9 10; do
      python3 -c "import socket; socket.create_connection(('127.0.0.1', 8080), 1)" 2> /dev/null && break
      sleep 0.5
    done
    exec /app/flatpak-external-data-checker "$@"
    """
)
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def sh(*cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=900)


def git(cwd, *args):
    result = sh("git", *args, cwd=cwd, env=GIT_ENV)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def text(path):
    return Path(path).read_text(encoding="utf-8")


def tree_bytes(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(Path(directory).rglob("*")) if p.is_file()}


def obsolete_beta():
    snapshot = REVIEW / "beta-base-io.tether.QVAC.Workbench.yml"
    return text(snapshot) if snapshot.exists() else OBSOLETE_BETA


def production_recipe():
    """The human production recipe, pointed at the fixture host instead of static.tether.dev."""
    return text(REPO / MANIFEST).replace("https://static.tether.dev/", "http://127.0.0.1:8080/")


def fixture_archive(version):
    return gzip.compress(f"fixture archive {version}\n".encode(), mtime=0)


def beta_url(version):
    return f"{LOCAL_ROOT}beta-{version}/flatpak/QV.AC-x64-flatpak.tar.gz"


def stable_url(version):
    return f"{LOCAL_ROOT}{version}/QV.AC-x64-flatpak.tar.gz"


def publish(site, stable=(), beta=()):
    """What the website serves: a root listing, stable <version>/ and staging beta-<version>/flatpak/."""
    root = site / "qv.ac"
    for version in stable:
        directory = root / version
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "QV.AC-x64-flatpak.tar.gz").write_bytes(fixture_archive(version))
    for version in beta:
        directory = root / f"beta-{version}" / "flatpak"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "QV.AC-x64-flatpak.tar.gz").write_bytes(fixture_archive(version))
    root.mkdir(parents=True, exist_ok=True)
    names = ["beta", *stable, *[f"beta-{v}" for v in beta]]  # "beta" is the managed link
    links = "".join(f'<a href="./{n}/">{n}/</a>\n' for n in names)
    (root / "index.html").write_text(f"<html><body>\n{links}</body></html>\n", encoding="utf-8")


def qvac_source(recipe):
    module = next(m for m in recipe["modules"] if m["name"] == "qvac-workbench")
    return next(s for s in module["sources"] if s["type"] == "archive")


def docker_problem():
    if not shutil.which("docker"):
        return "docker is not installed"
    if sh("docker", "image", "inspect", IMAGE).returncode != 0:
        pulled = sh("docker", "pull", IMAGE)
        if pulled.returncode != 0:
            return f"cannot pull {IMAGE}: {pulled.stderr.strip()[-300:]}"
    return None


class ImageCase(unittest.TestCase):
    """The pinned official image with fixtures only: --network none and no token."""

    @classmethod
    def setUpClass(cls):
        problem = docker_problem()
        if problem and os.environ.get("BETA_UPDATER_REQUIRE_DOCKER"):
            raise AssertionError(problem)
        if problem:
            raise unittest.SkipTest(problem)

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name).resolve()  # mounted at /work
        self.site = self.tmp / "site"
        self.site.mkdir()
        production = {name: (REPO / name).read_bytes() for name in (MANIFEST, "flathub.json")}
        self.addCleanup(
            lambda: self.assertEqual(
                {name: (REPO / name).read_bytes() for name in production},
                production,
                "the production manifest or flathub.json changed",
            )
        )

    def docker(self, *tail, entrypoint, workdir="/work"):
        environment = {
            "HOME": "/tmp",
            "USER": "tester",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
        flags = [flag for key, value in environment.items() for flag in ("-e", f"{key}={value}")]
        return sh(
            "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            *flags, "-v", f"{self.tmp}:/work", "--workdir", workdir, "--entrypoint", entrypoint,
            IMAGE, *tail,
        )  # fmt: skip

    def checker(self, mode, args, workdir="/work"):
        """The workflow's own checker arguments, with --update swapped for `mode`."""
        args = [mode if arg == "--update" else arg for arg in args]
        return self.docker("-c", SERVE_AND_RUN, "sh", *args, entrypoint="/bin/sh", workdir=workdir)

    def parse_yaml(self, **documents):
        """Parse YAML texts with the image's own ruamel.yaml, so the host needs no YAML library."""
        for name, content in documents.items():
            (self.tmp / f"{name}.yml").write_text(content, encoding="utf-8")
        program = (
            "import json, sys\n"
            "from ruamel.yaml import YAML\n"
            "yaml = YAML(typ='safe')\n"
            "print(json.dumps({n: yaml.load(open(f'/work/{n}.yml', encoding='utf-8')) for n in sys.argv[1:]}))\n"
        )
        result = self.docker("-c", program, *documents, entrypoint="python3")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_only_source_moved(self, before, after, url, version):
        """The official checker may reformat YAML (for example fold long scalars). Compare the parsed
        recipes: they may differ only by the QVAC archive's url and sha512 values."""
        parsed = self.parse_yaml(before=before, after=after)
        expected = parsed["before"]
        source = qvac_source(expected)
        sha512 = hashlib.sha512(fixture_archive(version)).hexdigest()
        self.assertNotEqual(source["url"], url, "the source must actually move")
        self.assertNotEqual(source["sha512"], sha512)
        source["url"], source["sha512"] = url, sha512
        self.assertEqual(parsed["after"], expected)

    def git_repo(self, directory, branch):
        git(directory, "init", "-q")
        git(directory, "checkout", "-q", "-b", branch)
        git(directory, "add", "-A")
        git(directory, "commit", "-q", "-m", branch)
        return git(directory, "rev-parse", "HEAD")

    def update_branch(self, directory, prefix):
        branches = git(directory, "branch", "--list", f"{prefix}*", "--format=%(refname:short)").split()
        self.assertEqual(len(branches), 1)
        self.assertRegex(branches[0], rf"^{prefix}[0-9a-f]{{7}}$")
        return branches[0]


class BetaCase(ImageCase):
    """A trusted master tree (helper and production recipe) and an obsolete beta checkout."""

    def setUp(self):
        super().setUp()
        self.trusted = self.tmp / "trusted"
        (self.trusted / ".github" / "beta-updater").mkdir(parents=True)
        shutil.copy(HELPER, self.trusted / ".github" / "beta-updater" / HELPER.name)
        (self.trusted / MANIFEST).write_text(production_recipe(), encoding="utf-8")
        shutil.copy(REPO / "flathub.json", self.trusted / "flathub.json")
        inputs = tree_bytes(self.trusted)
        self.addCleanup(lambda: self.assertEqual(tree_bytes(self.trusted), inputs, "the trusted inputs changed"))
        self.beta = self.tmp / "beta"
        self.beta.mkdir()
        (self.beta / MANIFEST).write_text(obsolete_beta(), encoding="utf-8")
        (self.beta / "flathub.json").write_text(OBSOLETE_FLATHUB, encoding="utf-8")

    def prepare(self, trusted="trusted"):
        """prepare-beta.sh exactly as the workflow runs it, in the pinned image."""
        return self.docker(HELPER_ARGS[0], trusted, *HELPER_ARGS[2:], entrypoint=HELPER_ENTRYPOINT)

    def prepare_ok(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_checker(self, mode):
        return self.checker(mode, BETA_ARGS)


class HelperTests(BetaCase):
    def test_the_obsolete_beta_is_reseeded_from_production_changing_only_discovery(self):
        self.prepare_ok()
        old, new = text(self.trusted / MANIFEST).splitlines(), text(self.beta / MANIFEST).splitlines()
        self.assertEqual(len(old), len(new))
        # The source url and sha512 stay production's real ones: nothing is invented.
        self.assertEqual(
            [(a, b) for a, b in zip(old, new) if a != b],
            [(PRODUCTION_PATTERN, BETA_PATTERN), (PRODUCTION_TEMPLATE, BETA_TEMPLATE)],
        )
        self.assertEqual(
            json.loads(text(self.beta / "flathub.json")),
            {"only-arches": ["x86_64"], "automerge-flathubbot-prs": True},
        )

    def test_a_missing_flathub_json_is_seeded_from_production_with_automerge_true(self):
        (self.beta / "flathub.json").unlink()
        self.prepare_ok()
        expected = {**json.loads(text(self.trusted / "flathub.json")), "automerge-flathubbot-prs": True}
        self.assertEqual(json.loads(text(self.beta / "flathub.json")), expected)

    def test_a_configured_beta_is_left_alone_including_a_humans_automerge_choice(self):
        self.prepare_ok()
        (self.beta / MANIFEST).write_text("# beta-only note\n" + text(self.beta / MANIFEST), encoding="utf-8")
        (self.beta / "flathub.json").write_text(
            '{"only-arches": ["x86_64"], "automerge-flathubbot-prs": false}\n', encoding="utf-8"
        )
        settled = tree_bytes(self.beta)
        self.prepare_ok()
        self.assertEqual(tree_bytes(self.beta), settled)

    def test_checker_data_that_is_not_staging_is_refused_not_overwritten(self):
        (self.beta / MANIFEST).write_text(text(self.trusted / MANIFEST), encoding="utf-8")  # production discovery
        before = tree_bytes(self.beta)
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fix it by hand", result.stderr)
        self.assertEqual(tree_bytes(self.beta), before)

    def test_an_unexpected_production_recipe_is_refused_and_beta_stays_untouched(self):
        odd = self.tmp / "odd"
        shutil.copytree(self.trusted, odd)
        (odd / MANIFEST).write_text(
            text(odd / MANIFEST).replace("          url-template:", "          url-template: x\n          url-template:"),
            encoding="utf-8",
        )
        before = tree_bytes(self.beta)
        result = self.prepare(trusted="odd")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one", result.stderr)
        self.assertEqual(tree_bytes(self.beta), before)


class OfficialCheckerTests(BetaCase):
    def test_first_run_seeds_beta_then_the_checker_moves_the_source_to_the_newest_rc(self):
        publish(self.site, stable=("99.0.0",), beta=("0.21.0-rc.9.171301", "0.21.0-rc.10.5"))
        self.prepare_ok()
        seeded = text(self.beta / MANIFEST)
        run = self.run_checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assert_only_source_moved(seeded, text(self.beta / MANIFEST), beta_url("0.21.0-rc.10.5"), "0.21.0-rc.10.5")

    def test_later_runs_keep_the_human_beta_recipe_and_only_move_the_source(self):
        publish(self.site, beta=("0.21.0-rc.10.5",))
        self.prepare_ok()
        self.assertEqual(self.run_checker("--edit-only").returncode, 0)
        merged = text(self.beta / MANIFEST)  # the first update, merged by a human
        edited = "# beta-only note\n" + merged.replace("  - --persist=.qvac\n", "  - --persist=.qvac\n  - --filesystem=xdg-download\n")
        self.assertIn("--filesystem=xdg-download", edited)
        (self.beta / MANIFEST).write_text(edited, encoding="utf-8")
        flathub = '{"only-arches": ["x86_64"], "automerge-flathubbot-prs": false}\n'
        (self.beta / "flathub.json").write_text(flathub, encoding="utf-8")

        self.prepare_ok()
        self.assertEqual(text(self.beta / MANIFEST), edited, "the preparation must not run again")
        run = self.run_checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(text(self.beta / MANIFEST), edited, "the newest RC is already in place")

        publish(self.site, beta=("0.21.0-rc.10.5", "0.21.0-rc.11.1"))
        run = self.run_checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        updated = text(self.beta / MANIFEST)
        self.assert_only_source_moved(edited, updated, beta_url("0.21.0-rc.11.1"), "0.21.0-rc.11.1")
        self.assertIn("# beta-only note", updated, "the human comment survives the official rewrite")
        self.assertIn("--filesystem=xdg-download", updated)
        self.assertEqual(text(self.beta / "flathub.json"), flathub)

    def test_the_first_update_commit_carries_the_beta_packaging_and_automerge_true(self):
        publish(self.site, beta=("0.21.0-rc.10.5",))
        base = self.git_repo(self.beta, "beta")
        self.prepare_ok()
        run = self.run_checker("--commit-only")
        self.assertEqual(run.returncode, 0, run.stderr)

        # The branch name carries the current branch, "beta": the base_branch that open_pr uses
        # as the pull request base (pull request creation itself needs a token and is not run).
        branch = self.update_branch(self.beta, "update-beta-")
        self.assertEqual(git(self.beta, "rev-parse", f"{branch}^"), base)
        self.assertEqual(git(self.beta, "rev-parse", "beta"), base, "beta itself is never touched")
        self.assertEqual(
            git(self.beta, "log", "-1", "--format=%s", branch),
            "qvac-workbench: Update QV.AC-x64-flatpak.tar.gz to 0.21.0-rc.10.5",
        )
        self.assertEqual(sorted(git(self.beta, "diff", "--name-only", f"{base}..{branch}").split()), ["flathub.json", MANIFEST])
        self.assertEqual(
            json.loads(git(self.beta, "show", f"{branch}:flathub.json")),
            {"only-arches": ["x86_64"], "automerge-flathubbot-prs": True},
        )
        committed = self.parse_yaml(committed=git(self.beta, "show", f"{branch}:{MANIFEST}"))["committed"]
        source = qvac_source(committed)
        self.assertEqual(source["url"], beta_url("0.21.0-rc.10.5"))
        self.assertEqual(source["sha512"], hashlib.sha512(fixture_archive("0.21.0-rc.10.5")).hexdigest())
        self.assertEqual(source["x-checker-data"]["url-template"], BETA_TEMPLATE.split(": ", 1)[1])

    def test_a_flathub_json_that_beta_lacks_is_committed_as_a_new_file(self):
        (self.beta / "flathub.json").unlink()
        publish(self.site, beta=("0.21.0-rc.10.5",))
        base = self.git_repo(self.beta, "beta")
        self.prepare_ok()
        run = self.run_checker("--commit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        branch = self.update_branch(self.beta, "update-beta-")
        self.assertEqual(sorted(git(self.beta, "diff", "--name-only", f"{base}..{branch}").split()), ["flathub.json", MANIFEST])
        self.assertEqual(
            json.loads(git(self.beta, "show", f"{branch}:flathub.json")),
            {"only-arches": ["x86_64"], "automerge-flathubbot-prs": True},
        )

    def test_no_listed_rc_fails_visibly_and_the_seed_is_never_committed(self):
        publish(self.site, stable=("99.0.0",))  # the listing has no beta-* directory
        base = self.git_repo(self.beta, "beta")
        self.prepare_ok()
        run = self.run_checker("--commit-only")
        self.assertNotEqual(run.returncode, 0, "the checker reports that nothing matched")
        self.assertEqual(git(self.beta, "rev-parse", "HEAD"), base)
        self.assertEqual(git(self.beta, "branch", "--format=%(refname:short)"), "beta")
        # The seed is only an uncommitted working-tree edit.
        self.assertEqual(sorted(git(self.beta, "diff", "--name-only").split()), ["flathub.json", MANIFEST])

    def test_a_listed_but_unavailable_archive_fails_visibly(self):
        publish(self.site, beta=("0.21.0-rc.10.5",))
        (self.site / "qv.ac" / "beta-0.21.0-rc.10.5" / "flatpak" / "QV.AC-x64-flatpak.tar.gz").unlink()
        self.prepare_ok()
        before = text(self.beta / MANIFEST)
        run = self.run_checker("--edit-only")
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(text(self.beta / MANIFEST), before)


class ProductionTests(ImageCase):
    def test_the_newest_stable_is_selected_on_a_named_master_with_recipe_and_config_untouched(self):
        master = self.tmp / "master"
        master.mkdir()
        (master / MANIFEST).write_text(production_recipe(), encoding="utf-8")
        shutil.copy(REPO / "flathub.json", master / "flathub.json")
        base = self.git_repo(master, "master")
        # Versions above anything production will reach; numeric order makes 99.10.0 beat 99.9.0.
        publish(self.site, stable=("0.9.0", "0.20.1", "99.9.0", "99.10.0"), beta=("99.11.0-rc.1.1",))

        run = self.checker("--commit-only", PRODUCTION_ARGS, workdir="/work/master")
        self.assertEqual(run.returncode, 0, run.stderr)

        # "master" as the current branch is the base_branch that open_pr uses as the PR base.
        branch = self.update_branch(master, "update-master-")
        self.assertEqual(git(master, "rev-parse", f"{branch}^"), base)
        self.assertEqual(git(master, "rev-parse", "master"), base)
        self.assertEqual(
            git(master, "log", "-1", "--format=%s", branch),
            "qvac-workbench: Update QV.AC-x64-flatpak.tar.gz to 99.10.0",
        )
        self.assertEqual(git(master, "diff", "--name-only", f"{base}..{branch}").split(), [MANIFEST])
        self.assertEqual(json.loads(git(master, "show", f"{branch}:flathub.json")), json.loads(text(REPO / "flathub.json")))
        committed = git(master, "show", f"{branch}:{MANIFEST}")
        self.assert_only_source_moved(production_recipe(), committed, stable_url("99.10.0"), "99.10.0")
        self.assertNotIn("beta-$version", committed)


class WorkflowContractTests(unittest.TestCase):
    GUARD = "github.repository == 'flathub/io.tether.QVAC.Workbench' && github.ref == 'refs/heads/master'"

    def test_beta_runs_hourly_and_by_hand_from_master_of_the_flathub_repo(self):
        self.assertRegex(BETA, r"(?m)^  schedule:$")
        self.assertIn('- cron: "17 * * * *"', BETA, "beta is checked every hour")
        self.assertRegex(BETA, r"(?m)^  workflow_dispatch:$")
        self.assertNotRegex(BETA, r"(?m)^  (push|pull_request\w*|workflow_run|repository_dispatch|issue_comment):")
        self.assertIn(self.GUARD, BETA)
        self.assertIn("group: beta-updater", BETA)

    def test_production_is_manual_only_on_a_named_master_with_its_own_concurrency(self):
        self.assertRegex(PRODUCTION, r"(?m)^  workflow_dispatch:$")
        self.assertNotRegex(PRODUCTION, r"(?m)^  (schedule|push|pull_request\w*|workflow_run|repository_dispatch|issue_comment):")
        self.assertIn(self.GUARD, PRODUCTION)
        self.assertIn("group: production-updater", PRODUCTION)
        self.assertIn("ref: master\n          persist-credentials: false", PRODUCTION)
        self.assertEqual(PRODUCTION.count("uses: actions/checkout@"), 1)
        self.assertEqual(PRODUCTION.count("uses: docker://"), 1)
        # The production manifest and config are used as they are: no helper, no config edit,
        # no automerge override, nothing shared with beta.
        for needle in ("cron", "path:", "beta-updater", "prepare-beta", "flathub.json", "jq", "entrypoint", "automerge"):
            self.assertNotIn(needle, code(PRODUCTION))

    def test_both_run_the_same_pinned_official_checker_with_least_privilege_and_the_built_in_token(self):
        self.assertRegex(IMAGE, r"^ghcr\.io/flathub/flatpak-external-data-checker@sha256:[0-9a-f]{64}$")
        self.assertEqual(BETA_ARGS, ["--update", "--never-fork", "--filter-type", "archive", f"beta/{MANIFEST}"])
        self.assertEqual(PRODUCTION_ARGS, ["--update", "--never-fork", "--filter-type", "archive", MANIFEST])
        for name, workflow in (("beta", BETA), ("production", PRODUCTION)):
            with self.subTest(name):
                self.assertEqual(set(re.findall(r"uses: docker://(\S+)", workflow)), {IMAGE})
                self.assertIn("\npermissions: {}\n", workflow)
                self.assertEqual(
                    re.findall(r"(?m)^      (\w[\w-]*): (read|write)$", workflow),
                    [("contents", "write"), ("pull-requests", "write")],
                )
                self.assertEqual(re.findall(r"secrets\.\w+", workflow), ["secrets.GITHUB_TOKEN"])
                for flag in ("--always-fork", "--commit-to-current-branch", "--unsafe", "--commit-only", "--edit-only"):
                    self.assertNotIn(flag, workflow)

    def test_beta_code_comes_from_trusted_master_and_beta_is_data_for_the_helper_and_checker(self):
        self.assertIn("ref: ${{ github.sha }}\n          path: trusted", BETA)
        self.assertIn("ref: beta\n          path: beta", BETA)
        self.assertEqual(BETA.count("persist-credentials: false"), 2)
        self.assertEqual(HELPER_ENTRYPOINT, "/bin/bash")
        self.assertEqual(HELPER_ARGS, [f"trusted/.github/beta-updater/{HELPER.name}", "trusted", "beta", MANIFEST])
        self.assertTrue(HELPER.exists())
        order = [BETA.index(f"- name: {name}") for name in (TRUSTED_STEP, "Check out beta", PREPARE_STEP, CHECK_STEP)]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("if: steps.", BETA)
        self.assertNotIn("GITHUB_TOKEN", step(BETA, PREPARE_STEP), "only the checker step gets the token")

    def test_actions_are_pinned_and_the_test_workflow_covers_every_updater_workflow(self):
        for path in WORKFLOWS.glob("*updater*.yml"):
            for reference in re.findall(r"uses:\s*(\S+)", path.read_text(encoding="utf-8")):
                pinned = r"@[0-9a-f]{40}$" if not reference.startswith("docker://") else r"@sha256:[0-9a-f]{64}$"
                self.assertRegex(reference, pinned, f"{path.name}: {reference}")
        tests = (WORKFLOWS / "beta-updater-tests.yml").read_text(encoding="utf-8")
        self.assertIn('".github/workflows/*updater*.yml"', tests)
        self.assertIn("permissions:\n  contents: read\n", tests)
        self.assertIn("BETA_UPDATER_REQUIRE_DOCKER", tests)
        self.assertNotIn("secrets.", tests)


if __name__ == "__main__":
    unittest.main()
