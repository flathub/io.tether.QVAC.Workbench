"""Tests for the beta updater wiring.

Version selection, download, checksums, commits and pull requests are done by the
OFFICIAL flatpak-external-data-checker. These tests run the exact image and arguments the
workflow pins, with --edit-only / --commit-only, against a fixture HTTP server started inside
the container (--network none, no token). The helper script and the workflow contract are
tested on the host. Docker is required for the checker tests; they are skipped without it
unless BETA_UPDATER_REQUIRE_DOCKER is set (CI sets it).
"""

import gzip
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SCRIPT = REPO / ".github" / "beta-updater" / "prepare-beta.sh"
WORKFLOW = (REPO / ".github" / "workflows" / "beta-updater.yml").read_text(encoding="utf-8")
MANIFEST = "io.tether.QVAC.Workbench.yml"
IMAGE = re.search(r"uses: docker://(\S+)", WORKFLOW).group(1)
CHECKER_ARGS = re.search(r"(?m)^\s+args: (.+)$", WORKFLOW).group(1).split()

LOCAL_ROOT = "http://127.0.0.1:8080/qv.ac/"
OBSOLETE_MANIFEST = "id: io.tether.QVAC.Workbench\nruntime: org.gnome.Platform\nruntime-version: '49'\nmodules: []\n"
SEED_PATTERN = r'          version-pattern: href="\./((?:\d+\.)+\d+)/"'
BETA_PATTERN = r'          version-pattern: href="\./beta-(\d+\.\d+\.\d+-rc\.\d+\.\d+)/"'
SEED_TEMPLATE = f"          url-template: {LOCAL_ROOT}$version/QV.AC-x64-flatpak.tar.gz"
BETA_TEMPLATE = f"          url-template: {LOCAL_ROOT}beta-$version/flatpak/QV.AC-x64-flatpak.tar.gz"

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


def snapshot(directory):
    return {p.name: p.read_bytes() for p in Path(directory).iterdir()}


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


class Fixture(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name).resolve()
        self.site, self.master, self.beta = (self.tmp / n for n in ("site", "master", "beta"))
        for directory in (self.site, self.master, self.beta):
            directory.mkdir()
        # The human production recipe, pointed at the fixture host instead of static.tether.dev.
        production = text(REPO / MANIFEST).replace("https://static.tether.dev/", "http://127.0.0.1:8080/")
        (self.master / MANIFEST).write_text(production, encoding="utf-8")
        shutil.copy(REPO / "flathub.json", self.master / "flathub.json")
        # Beta today: an obsolete recipe without any checker data.
        (self.beta / MANIFEST).write_text(OBSOLETE_MANIFEST, encoding="utf-8")
        (self.beta / "flathub.json").write_text('{"skip-icons-check": true}\n', encoding="utf-8")
        inputs, repo_manifest = snapshot(self.master), (REPO / MANIFEST).read_bytes()
        self.addCleanup(lambda: self.assertEqual(snapshot(self.master), inputs, "production inputs changed"))
        self.addCleanup(lambda: self.assertEqual((REPO / MANIFEST).read_bytes(), repo_manifest))

    def bootstrap(self):
        result = sh("bash", str(SCRIPT), "bootstrap", str(self.master), str(self.beta), MANIFEST)
        self.assertEqual(result.returncode, 0, result.stderr)

    def git_beta(self):
        git(self.beta, "init", "-q")
        git(self.beta, "checkout", "-q", "-b", "beta")
        git(self.beta, "add", "-A")
        git(self.beta, "commit", "-q", "-m", "obsolete beta")


class ReadyGuardTests(unittest.TestCase):
    def serve(self, status):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/beta/flatpak/QV.AC-x64-flatpak.tar.gz"

    def ready(self, url):
        return sh("bash", str(SCRIPT), "ready", url)

    def test_an_exposed_archive_is_ready(self):
        result = self.ready(self.serve(200))
        self.assertEqual((result.returncode, result.stdout), (0, "ready=true\n"))

    def test_only_a_404_means_nothing_is_published_yet(self):
        result = self.ready(self.serve(404))
        self.assertEqual((result.returncode, result.stdout), (0, "ready=false\n"))
        self.assertIn("no beta archive", result.stderr)

    def test_other_http_statuses_fail_without_output(self):
        for status in (301, 403, 410, 500, 503):
            with self.subTest(status):
                result = self.ready(self.serve(status))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_transport_errors_fail_without_output(self):
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
        result = self.ready(f"http://127.0.0.1:{port}/beta/flatpak/QV.AC-x64-flatpak.tar.gz")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class BootstrapTests(Fixture):
    def test_obsolete_beta_is_reseeded_from_production_changing_only_the_checker_lines(self):
        self.bootstrap()
        old, new = text(self.master / MANIFEST).splitlines(), text(self.beta / MANIFEST).splitlines()
        self.assertEqual(len(old), len(new))
        self.assertEqual(
            [(a, b) for a, b in zip(old, new) if a != b],
            [(SEED_PATTERN, BETA_PATTERN), (SEED_TEMPLATE, BETA_TEMPLATE)],
        )
        # The seed keeps production's real url and checksum: nothing is invented or published.
        self.assertIn(f"        url: {LOCAL_ROOT}0.20.1/QV.AC-x64-flatpak.tar.gz", new)
        self.assertEqual(
            json.loads(text(self.beta / "flathub.json")),
            {"skip-icons-check": True, "automerge-flathubbot-prs": False},
        )

    def test_a_missing_flathub_json_is_seeded_from_production(self):
        (self.beta / "flathub.json").unlink()
        self.bootstrap()
        self.assertEqual(
            json.loads(text(self.beta / "flathub.json")),
            {"only-arches": ["x86_64"], "automerge-flathubbot-prs": False},
        )

    def test_bootstrap_happens_once_and_beta_recipe_edits_survive(self):
        self.bootstrap()
        edited = "# beta-only note\n" + text(self.beta / MANIFEST)
        (self.beta / MANIFEST).write_text(edited, encoding="utf-8")
        (self.beta / "flathub.json").write_text('{"automerge-flathubbot-prs": true, "skip-icons-check": true}', encoding="utf-8")
        self.bootstrap()
        self.assertEqual(text(self.beta / MANIFEST), edited)
        self.assertEqual(
            json.loads(text(self.beta / "flathub.json")),
            {"automerge-flathubbot-prs": False, "skip-icons-check": True},
        )
        settled = snapshot(self.beta)
        self.bootstrap()
        self.assertEqual(snapshot(self.beta), settled)

    def test_unexpected_beta_checker_data_is_refused_not_overwritten(self):
        (self.beta / MANIFEST).write_text(text(self.master / MANIFEST), encoding="utf-8")  # production discovery
        before = snapshot(self.beta)
        result = sh("bash", str(SCRIPT), "bootstrap", str(self.master), str(self.beta), MANIFEST)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fix it by hand", result.stderr)
        self.assertEqual(snapshot(self.beta), before)

    def test_a_production_recipe_with_unexpected_checker_shape_is_refused(self):
        odd = self.tmp / "odd-master"
        shutil.copytree(self.master, odd)
        (odd / MANIFEST).write_text(
            text(odd / MANIFEST).replace("          url-template:", "          url-template: x\n          url-template:"),
            encoding="utf-8",
        )
        before = snapshot(self.beta)
        result = sh("bash", str(SCRIPT), "bootstrap", str(odd), str(self.beta), MANIFEST)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one", result.stderr)
        self.assertEqual(snapshot(self.beta), before)


class OfficialCheckerTests(Fixture):
    @classmethod
    def setUpClass(cls):
        problem = docker_problem()
        if problem and os.environ.get("BETA_UPDATER_REQUIRE_DOCKER"):
            raise AssertionError(problem)
        if problem:
            raise unittest.SkipTest(problem)

    def checker(self, mode):
        """The pinned official image with the workflow's own arguments, --update swapped for `mode`."""
        self.assertEqual(CHECKER_ARGS[-1], f"beta/{MANIFEST}")
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
            *flags, "-v", f"{self.tmp}:/work", "--workdir", "/work", "--entrypoint", "/bin/sh",
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

    def test_first_bootstrap_updates_to_the_newest_rc_numerically_with_the_real_hash(self):
        publish(self.site, "0.21.0-rc.9.171301", "0.21.0-rc.10.5")
        self.bootstrap()
        seeded = text(self.beta / MANIFEST)
        run = self.checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assert_only_source_moved(seeded, text(self.beta / MANIFEST), "0.21.0-rc.10.5")

    def test_later_updates_keep_the_beta_recipe_and_only_move_the_source(self):
        publish(self.site, "0.21.0-rc.10.5")
        self.bootstrap()
        self.assertEqual(self.checker("--edit-only").returncode, 0)
        merged = text(self.beta / MANIFEST)  # the first pull request, merged by a human
        edited = "# beta-only note\n" + merged.replace("  - --persist=.qvac\n", "  - --persist=.qvac\n  - --filesystem=xdg-download\n")
        self.assertIn("--filesystem=xdg-download", edited)
        (self.beta / MANIFEST).write_text(edited, encoding="utf-8")

        self.bootstrap()
        self.assertEqual(text(self.beta / MANIFEST), edited, "bootstrap must not run again")
        run = self.checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(text(self.beta / MANIFEST), edited, "the newest RC is already in place")

        publish(self.site, "0.21.0-rc.10.5", "0.21.0-rc.11.1")
        run = self.checker("--edit-only")
        self.assertEqual(run.returncode, 0, run.stderr)
        updated = text(self.beta / MANIFEST)
        self.assert_only_source_moved(edited, updated, "0.21.0-rc.11.1")
        self.assertIn("# beta-only note", updated, "the human comment survives the official rewrite")
        self.assertIn("--filesystem=xdg-download", updated)

    def test_a_seeded_recipe_is_never_committed_without_a_real_update(self):
        publish(self.site)  # no beta-* directory is listed
        self.git_beta()
        base = git(self.beta, "rev-parse", "HEAD")
        self.bootstrap()
        run = self.checker("--commit-only")
        self.assertNotEqual(run.returncode, 0, "the checker reports that nothing matched")
        self.assertEqual(git(self.beta, "rev-parse", "HEAD"), base)
        self.assertEqual(git(self.beta, "branch", "--format=%(refname:short)"), "beta")
        self.assertIn(MANIFEST, git(self.beta, "status", "--porcelain"), "the seed stays uncommitted")

    def test_a_real_update_is_committed_on_a_branch_based_on_named_beta(self):
        (self.beta / "flathub.json").unlink()  # also covers a flathub.json git does not track yet
        publish(self.site, "0.21.0-rc.10.5")
        self.git_beta()
        base = git(self.beta, "rev-parse", "HEAD")
        self.bootstrap()
        run = self.checker("--commit-only")
        self.assertEqual(run.returncode, 0, run.stderr)

        branches = git(self.beta, "branch", "--list", "update-beta-*", "--format=%(refname:short)").split()
        self.assertEqual(len(branches), 1)
        branch = branches[0]
        self.assertRegex(branch, r"^update-beta-[0-9a-f]{7}$")
        self.assertEqual(git(self.beta, "rev-parse", f"{branch}^"), base)
        self.assertEqual(git(self.beta, "rev-parse", "beta"), base)
        self.assertEqual(
            git(self.beta, "log", "-1", "--format=%s", branch),
            "qvac-workbench: Update QV.AC-x64-flatpak.tar.gz to 0.21.0-rc.10.5",
        )
        self.assertEqual(sorted(git(self.beta, "diff", "--name-only", f"{base}..{branch}").split()), ["flathub.json", MANIFEST])
        self.assertEqual(
            json.loads(git(self.beta, "show", f"{branch}:flathub.json")),
            {"only-arches": ["x86_64"], "automerge-flathubbot-prs": False},
        )
        committed = git(self.beta, "show", f"{branch}:{MANIFEST}")
        self.assertIn(f"{LOCAL_ROOT}beta-0.21.0-rc.10.5/flatpak/QV.AC-x64-flatpak.tar.gz", committed)
        self.assertIn(hashlib.sha512(fixture_archive("0.21.0-rc.10.5")).hexdigest(), committed)


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

    def test_runs_the_pinned_official_checker_without_forks_or_extra_publishing(self):
        self.assertRegex(IMAGE, r"^ghcr\.io/flathub/flatpak-external-data-checker@sha256:[0-9a-f]{64}$")
        self.assertEqual(
            CHECKER_ARGS,
            ["--update", "--never-fork", "--filter-type", "archive", f"beta/{MANIFEST}"],
        )
        for needle in ("gh pr", "gh api", "pr merge", "--always-fork", "--commit-to-current-branch"):
            self.assertNotIn(needle, WORKFLOW)

    def test_beta_is_a_named_data_checkout_and_every_beta_step_waits_for_the_archive(self):
        self.assertIn("ref: ${{ github.sha }}\n          path: trusted", WORKFLOW)
        self.assertIn("ref: beta\n          path: beta", WORKFLOW)
        self.assertEqual(WORKFLOW.count("persist-credentials: false"), 2)
        self.assertEqual(WORKFLOW.count("if: steps.archive.outputs.ready == 'true'"), 3)
        self.assertIn("beta/flatpak/QV.AC-x64-flatpak.tar.gz >> \"$GITHUB_OUTPUT\"", WORKFLOW)
        self.assertIn("prepare-beta.sh bootstrap trusted beta io.tether.QVAC.Workbench.yml", WORKFLOW)

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
