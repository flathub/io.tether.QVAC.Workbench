"""Tests for the beta updater workflow.

Version selection, download, checksums, commits and pull requests are done by the OFFICIAL
flatpak-external-data-checker, so there is no updater code to test, only its wiring. The checker
tests run the exact image and arguments the workflow pins, with --edit-only / --commit-only,
against a fixture HTTP server started inside the container (--network none, no token). The
workflow's own inline guard script is run on fixtures, and the workflow contract is checked.
Docker is required for the checker tests; they are skipped without it unless
BETA_UPDATER_REQUIRE_DOCKER is set (CI sets it).
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
WORKFLOW = (REPO / ".github" / "workflows" / "beta-updater.yml").read_text(encoding="utf-8")
MANIFEST = "io.tether.QVAC.Workbench.yml"
GUARD_STEP = "Check that beta is configured for the official checker"
IMAGE = re.search(r"uses: docker://(\S+)", WORKFLOW).group(1)
CHECKER_ARGS = re.search(r"(?m)^\s+args: (.+)$", WORKFLOW).group(1).split()

LOCAL_ROOT = "http://127.0.0.1:8080/qv.ac/"
BETA_PATTERN = r'          version-pattern: href="\./beta-(\d+\.\d+\.\d+-rc\.\d+\.\d+)/"'
BETA_TEMPLATE = f"          url-template: {LOCAL_ROOT}beta-$version/flatpak/QV.AC-x64-flatpak.tar.gz"
FLATHUB_BETA = '{"only-arches": ["x86_64"], "automerge-flathubbot-prs": false}\n'

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


def step_script(name):
    """The `run: |` script of a workflow step, as the runner would execute it."""
    lines = WORKFLOW.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def beta_recipe():
    """The beta manifest as its own pull request carries it: the human production recipe plus
    staging checker data, pointed at the fixture host. The source is still an older archive."""
    lines = []
    for line in text(REPO / MANIFEST).replace("https://static.tether.dev/", "http://127.0.0.1:8080/").splitlines():
        if line.startswith("          version-pattern: "):
            line = BETA_PATTERN
        elif line.startswith("          url-template: "):
            line = BETA_TEMPLATE
        lines.append(line)
    return "\n".join(lines) + "\n"


def fixture_archive(version):
    return gzip.compress(f"fixture archive {version}\n".encode(), mtime=0)


def publish(site, *versions):
    """What the website serves: a root listing plus one archive per beta-<version>/flatpak/."""
    root = site / "qv.ac"
    root.mkdir(parents=True, exist_ok=True)
    for version in versions:
        directory = root / f"beta-{version}" / "flatpak"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "QV.AC-x64-flatpak.tar.gz").write_bytes(fixture_archive(version))
    names = ["0.20.1", "beta", *[f"beta-{v}" for v in versions]]
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


class GuardTests(unittest.TestCase):
    """The workflow's inline guard step, run exactly as written against fixture files."""

    def guard(self, manifest, flathub):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        directory = Path(scratch.name)
        for name, content in ((MANIFEST, manifest), ("flathub.json", flathub)):
            if content is not None:
                (directory / name).write_text(content, encoding="utf-8")
        env = {**os.environ, "GITHUB_STEP_SUMMARY": str(directory / "summary.md")}
        result = sh("bash", "-e", "-c", step_script(GUARD_STEP), cwd=directory, env=env)
        errors = [line for line in result.stdout.splitlines() if line.startswith("::error::")]
        summary = text(directory / "summary.md") if (directory / "summary.md").exists() else ""
        return result, errors, summary

    def test_a_configured_beta_passes(self):
        result, errors, summary = self.guard(beta_recipe(), FLATHUB_BETA)
        self.assertEqual((result.returncode, errors, summary), (0, [], ""))

    def test_the_production_recipe_is_refused_with_instructions(self):
        result, errors, summary = self.guard(text(REPO / MANIFEST), text(REPO / "flathub.json"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(errors), 2)
        self.assertIn("x-checker-data", errors[0])
        self.assertIn("flathub.json", errors[1])
        self.assertIn("Merge the beta manifest pull request first", summary)

    def test_each_missing_piece_is_refused_on_its_own(self):
        cases = {
            "staging checker data missing": (text(REPO / MANIFEST), FLATHUB_BETA, "x-checker-data"),
            "automerge not mentioned": (beta_recipe(), '{"only-arches": ["x86_64"]}\n', "flathub.json"),
            "automerge enabled": (beta_recipe(), '{"automerge-flathubbot-prs": true}\n', "flathub.json"),
            "flathub.json is not JSON": (beta_recipe(), "not json", "flathub.json"),
            "flathub.json is missing": (beta_recipe(), None, "flathub.json"),
        }
        for label, (manifest, flathub, culprit) in cases.items():
            with self.subTest(label):
                result, errors, summary = self.guard(manifest, flathub)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(errors), 1)
                self.assertIn(culprit, errors[0])
                self.assertIn("nothing was checked", summary)


class OfficialCheckerTests(unittest.TestCase):
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
        self.tmp = Path(scratch.name).resolve()
        self.site, self.repo = self.tmp / "site", self.tmp / "repo"  # the website, and the beta checkout
        self.site.mkdir()
        self.repo.mkdir()
        (self.repo / MANIFEST).write_text(beta_recipe(), encoding="utf-8")
        (self.repo / "flathub.json").write_text(FLATHUB_BETA, encoding="utf-8")
        production = (REPO / MANIFEST).read_bytes()
        self.addCleanup(lambda: self.assertEqual((REPO / MANIFEST).read_bytes(), production))

    def checker(self, mode):
        """The pinned official image with the workflow's own arguments, --update swapped for `mode`."""
        self.assertEqual(CHECKER_ARGS[-1], MANIFEST)
        args = [mode if arg == "--update" else arg for arg in CHECKER_ARGS]
        # Serve the fixture site from inside the container, so no network is needed at all.
        inner = textwrap.dedent(
            """
            python3 -m http.server 8080 --bind 127.0.0.1 --directory /work/site > /dev/null 2>&1 &
            for i in 1 2 3 4 5 6 7 8 9 10; do
              python3 -c "import socket; socket.create_connection(('127.0.0.1', 8080), 1)" 2> /dev/null && break
              sleep 0.5
            done
            exec /app/flatpak-external-data-checker "$@"
            """
        )
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
            *flags, "-v", f"{self.tmp}:/work", "--workdir", "/work/repo", "--entrypoint", "/bin/sh",
            IMAGE, "-c", inner, "sh", *args,
        )  # fmt: skip

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
        result = sh(
            "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{self.tmp}:/work:ro", "--entrypoint", "python3", IMAGE, "-c", program, *documents,
        )  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_only_source_moved(self, before, after, version):
        """The official checker may reformat YAML (for example fold long scalars). Compare the parsed
        recipes: they may differ only by the QVAC archive's url and sha512 values."""
        parsed = self.parse_yaml(before=before, after=after)
        expected = parsed["before"]
        source = qvac_source(expected)
        new_url = f"{LOCAL_ROOT}beta-{version}/flatpak/QV.AC-x64-flatpak.tar.gz"
        new_sha512 = hashlib.sha512(fixture_archive(version)).hexdigest()
        self.assertNotEqual(source["url"], new_url, "the source must actually move")
        self.assertNotEqual(source["sha512"], new_sha512)
        source["url"], source["sha512"] = new_url, new_sha512
        self.assertEqual(parsed["after"], expected)

    def git_beta(self):
        git(self.repo, "init", "-q")
        git(self.repo, "checkout", "-q", "-b", "beta")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "beta")

    def test_the_source_moves_to_the_newest_rc_numerically_with_the_real_hash(self):
        publish(self.site, "0.21.0-rc.9.171301", "0.21.0-rc.10.5")
        before = text(self.repo / MANIFEST)
        run = self.checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assert_only_source_moved(before, text(self.repo / MANIFEST), "0.21.0-rc.10.5")

    def test_repeated_updates_keep_the_human_beta_recipe_and_only_move_the_source(self):
        publish(self.site, "0.21.0-rc.10.5")
        self.assertEqual(self.checker("--edit-only").returncode, 0)
        merged = text(self.repo / MANIFEST)  # the first update, merged by a human
        edited = "# beta-only note\n" + merged.replace("  - --persist=.qvac\n", "  - --persist=.qvac\n  - --filesystem=xdg-download\n")
        self.assertIn("--filesystem=xdg-download", edited)
        (self.repo / MANIFEST).write_text(edited, encoding="utf-8")

        run = self.checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(text(self.repo / MANIFEST), edited, "the newest RC is already in place")

        publish(self.site, "0.21.0-rc.10.5", "0.21.0-rc.11.1")
        run = self.checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        updated = text(self.repo / MANIFEST)
        self.assert_only_source_moved(edited, updated, "0.21.0-rc.11.1")
        self.assertIn("# beta-only note", updated, "the human comment survives the official rewrite")
        self.assertIn("--filesystem=xdg-download", updated)

    def test_an_update_is_committed_on_a_branch_based_on_named_beta(self):
        publish(self.site, "0.21.0-rc.10.5")
        self.git_beta()
        base = git(self.repo, "rev-parse", "HEAD")
        run = self.checker("--commit-only")
        self.assertEqual(run.returncode, 0, run.stderr)

        # The branch name carries the current branch, "beta": the same base_branch that open_pr
        # uses as the pull request base (pull request creation itself needs a token and is not run).
        branches = git(self.repo, "branch", "--list", "update-beta-*", "--format=%(refname:short)").split()
        self.assertEqual(len(branches), 1)
        branch = branches[0]
        self.assertRegex(branch, r"^update-beta-[0-9a-f]{7}$")
        self.assertEqual(git(self.repo, "rev-parse", f"{branch}^"), base)
        self.assertEqual(git(self.repo, "rev-parse", "beta"), base)
        self.assertEqual(
            git(self.repo, "log", "-1", "--format=%s", branch),
            "qvac-workbench: Update QV.AC-x64-flatpak.tar.gz to 0.21.0-rc.10.5",
        )
        self.assertEqual(git(self.repo, "diff", "--name-only", f"{base}..{branch}").split(), [MANIFEST])
        committed = git(self.repo, "show", f"{branch}:{MANIFEST}")
        self.assertIn(f"{LOCAL_ROOT}beta-0.21.0-rc.10.5/flatpak/QV.AC-x64-flatpak.tar.gz", committed)
        self.assertIn(hashlib.sha512(fixture_archive("0.21.0-rc.10.5")).hexdigest(), committed)

    def test_nothing_is_committed_when_no_rc_is_listed(self):
        publish(self.site)  # the listing has no beta-* directory
        self.git_beta()
        base = git(self.repo, "rev-parse", "HEAD")
        run = self.checker("--commit-only")
        self.assertNotEqual(run.returncode, 0, "the checker reports that nothing matched")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), base)
        self.assertEqual(git(self.repo, "branch", "--format=%(refname:short)"), "beta")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_a_listed_but_unavailable_archive_fails_visibly(self):
        publish(self.site, "0.21.0-rc.10.5")
        (self.site / "qv.ac" / "beta-0.21.0-rc.10.5" / "flatpak" / "QV.AC-x64-flatpak.tar.gz").unlink()
        before = text(self.repo / MANIFEST)
        run = self.checker("--edit-only")
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(text(self.repo / MANIFEST), before)


class WorkflowContractTests(unittest.TestCase):
    def test_only_scheduled_and_manual_runs_from_master_of_the_flathub_repo(self):
        self.assertRegex(WORKFLOW, r"(?m)^  schedule:$")
        self.assertIn('- cron: "17 */4 * * *"', WORKFLOW, "the schedule is every 4 hours")
        self.assertRegex(WORKFLOW, r"(?m)^  workflow_dispatch:$")
        self.assertNotRegex(WORKFLOW, r"(?m)^  (push|pull_request\w*|workflow_run|repository_dispatch|issue_comment):")
        self.assertIn("github.repository == 'flathub/io.tether.QVAC.Workbench' && github.ref == 'refs/heads/master'", WORKFLOW)

    def test_least_privilege_and_only_the_builtin_token(self):
        self.assertIn("\npermissions: {}\n", WORKFLOW)
        self.assertEqual(
            re.findall(r"(?m)^      (\w[\w-]*): (read|write)$", WORKFLOW),
            [("contents", "write"), ("pull-requests", "write")],
        )
        self.assertEqual(re.findall(r"secrets\.\w+", WORKFLOW), ["secrets.GITHUB_TOKEN"])

    def test_runs_the_pinned_official_checker_and_nothing_custom(self):
        self.assertRegex(IMAGE, r"^ghcr\.io/flathub/flatpak-external-data-checker@sha256:[0-9a-f]{64}$")
        self.assertEqual(
            CHECKER_ARGS,
            ["--update", "--never-fork", "--filter-type", "archive", MANIFEST],
        )
        for needle in ("python", "curl", "prepare-beta", "gh pr", "gh api", "pr merge", "--always-fork", "--commit-to-current-branch"):
            self.assertNotIn(needle, WORKFLOW.lower())

    def test_beta_is_checked_out_as_a_named_branch_and_guarded_before_the_checker_runs(self):
        self.assertIn("ref: beta\n          persist-credentials: false", WORKFLOW)
        self.assertEqual(WORKFLOW.count("uses: actions/checkout@"), 1)
        self.assertNotIn("path:", WORKFLOW)
        order = [WORKFLOW.index(f"- name: {name}") for name in ("Check out beta", GUARD_STEP, "Run the official flatpak-external-data-checker")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("if: steps.", WORKFLOW, "no step is skipped: an unready beta must fail, not pass")

    def test_actions_are_pinned_and_the_test_workflow_is_read_only(self):
        for path in (REPO / ".github" / "workflows").glob("beta-updater*.yml"):
            for reference in re.findall(r"uses:\s*(\S+)", path.read_text(encoding="utf-8")):
                pinned = r"@[0-9a-f]{40}$" if not reference.startswith("docker://") else r"@sha256:[0-9a-f]{64}$"
                self.assertRegex(reference, pinned, f"{path.name}: {reference}")
        tests = (REPO / ".github" / "workflows" / "beta-updater-tests.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read\n", tests)
        self.assertIn("BETA_UPDATER_REQUIRE_DOCKER", tests)
        self.assertNotIn("secrets.", tests)


if __name__ == "__main__":
    unittest.main()
