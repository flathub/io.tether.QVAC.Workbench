"""Offline tests for update_beta.py.

No test touches the network (a socket guard fails any attempt) and none downloads
a real archive: publications are small synthetic tar.gz files served from memory.
The production manifest at the repository root is the human-authored input and is
asserted to stay unchanged.
"""

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
MASTER = REPO / "io.tether.QVAC.Workbench.yml"
WORKFLOWS = REPO / ".github" / "workflows"

spec = importlib.util.spec_from_file_location("update_beta", HERE.parent / "update_beta.py")
ub = importlib.util.module_from_spec(spec)
sys.modules["update_beta"] = ub
spec.loader.exec_module(ub)

ROOT = "https://static.tether.dev/qv.ac/"
APP_ID = ub.manifest_id(ub.split_manifest(MASTER.read_text(encoding="utf-8")))
V1 = "0.21.0-rc.1.100"
V2 = "0.21.0-rc.2.100"
V10 = "0.21.0-rc.10.5"
REPO_NAME = "flathub/io.tether.QVAC.Workbench"
FORK = "someone/io.tether.QVAC.Workbench"
BOT = {"login": "github-actions[bot]", "id": 41898282, "type": "Bot"}
HUMAN = {"login": "octocat", "id": 583231, "type": "User"}


def pull(version=V2, *, user=BOT, head_repo=REPO_NAME, base_ref="beta", state="open", ref=None):
    """A GitHub REST pull request object carrying the fields the updater reads."""
    return {
        "number": 7,
        "state": state,
        "head": {
            "ref": ref or f"beta-update/{version}",
            "repo": None if head_repo is None else {"full_name": head_repo},
        },
        "base": {"ref": base_ref, "repo": {"full_name": REPO_NAME}},
        "user": user,
    }


def base(version):
    return f"https://static.tether.dev/qv.ac/beta-{version}/flatpak/"


def listing(*names):
    return "<html><body>\n" + "".join(f'<a href="./{n}/">{n}/</a>\n' for n in names) + "</body></html>\n"


def make_archive(app_id=APP_ID, omit=()):
    names = [f"{app_id}.desktop", f"{app_id}.metainfo.xml", f"{app_id}.png", "run.sh", "main/app"]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        root = tarfile.TarInfo("./")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name in names:
            if name not in omit:
                data = name.encode()
                info = tarfile.TarInfo(f"./{name}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return gzip.compress(buffer.getvalue(), mtime=0)  # deterministic: tests compare digests


def publication(version, archive=None):
    """Files QVAC publishes under beta-<version>/flatpak/, written out literally."""
    files = {}
    for arch in ("x64", "arm64"):
        body = (archive if archive is not None else make_archive()) if arch == "x64" else f"{arch}".encode()
        digest = hashlib.sha512(body).hexdigest()
        name = f"QV.AC-{arch}-flatpak.tar.gz"
        files[base(version) + name] = body
        files[base(version) + name + ".sha512"] = f"{digest}  {name}\n".encode()
        files[base(version) + f"io.tether.qvac.source-{arch}.yml"] = (
            f"- type: archive\n  url: {base(version)}{name}\n  sha512: {digest}\n  strip-components: 0\n"
        ).encode()
    return files


class FakeFetcher:
    def __init__(self, files):
        self.files = files
        self.gets = []
        self.downloads = []

    def get(self, url, max_bytes):
        self.gets.append(url)
        if url not in self.files:
            raise ub.NotPublished(url)
        if len(self.files[url]) > max_bytes:
            raise ub.UpdateError("too large")
        return self.files[url]

    def download(self, url, dest, max_bytes):
        self.downloads.append(url)
        if url not in self.files:
            raise ub.NotPublished(url)
        if len(self.files[url]) > max_bytes:
            raise ub.UpdateError("too large")
        Path(dest).write_bytes(self.files[url])
        return hashlib.sha512(self.files[url]).hexdigest()


def site(listed, published=()):
    files = {ROOT: listing(*listed).encode()}
    for version in published:
        files.update(publication(version))
    return FakeFetcher(files)


class UpdaterCase(unittest.TestCase):
    def setUp(self):
        guard = mock.patch("socket.socket.connect", side_effect=AssertionError("network used in an offline test"))
        guard.start()
        self.addCleanup(guard.stop)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.beta = self.root / "beta"
        self.beta.mkdir()
        before = MASTER.read_bytes()
        self.addCleanup(lambda: self.assertEqual(MASTER.read_bytes(), before, "production manifest changed"))

    def propose(self, fetcher, **kwargs):
        return ub.propose(str(MASTER), str(self.beta), fetcher, ub.Deadline(60), **kwargs)

    def seed_beta(self, version, note=""):
        outcome = self.propose(site([f"beta-{version}"], [version]))
        (self.beta / MASTER.name).write_text(note + outcome.manifest, encoding="utf-8")
        return outcome.manifest

    def run_main(self, fetcher, *extra):
        argv = [
            "update",
            "--master", str(MASTER),
            "--beta-dir", str(self.beta),
            "--pr-body", str(self.root / "body.md"),
            "--github-output", str(self.root / "out.txt"),
            "--summary", str(self.root / "summary.md"),
            *extra,
        ]  # fmt: skip
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ub.main(argv, fetcher=fetcher)
        return code, out.getvalue(), err.getvalue()

    def outputs(self):
        lines = (self.root / "out.txt").read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)


class SelectionTests(UpdaterCase):
    def test_newest_by_numeric_rc_order_wins(self):
        listed = [
            f"beta-{V2}", f"beta-{V10}", "beta-0.21.0-rc.9.99999", "beta-0.20.9-rc.30.1",
            "beta-0.21.0-rc.10.05", "beta-0.21.0-rc.10", "beta-0.21.0", "0.22.0", "0.20.1", "beta", "latest",
        ]  # fmt: skip
        fetcher = site(listed, [V10])
        outcome = self.propose(fetcher)
        self.assertEqual((outcome.reason, outcome.version), ("bootstrap", V10))
        self.assertEqual(fetcher.downloads, [base(V10) + "QV.AC-x64-flatpak.tar.gz"])

    def test_version_key_is_numeric_not_lexical(self):
        self.assertLess(ub.version_key("0.21.0-rc.9.99"), ub.version_key("0.21.0-rc.10.1"))
        self.assertLess(ub.version_key("0.21.0-rc.2.9"), ub.version_key("0.21.0-rc.2.10"))
        self.assertLess(ub.version_key("0.9.0-rc.50.1"), ub.version_key("0.21.0-rc.0.1"))

    def test_missing_publication_is_a_noop(self):
        outcome = self.propose(site(["0.20.1", "beta", "latest"]))
        self.assertEqual(outcome.reason, "no-publication")
        self.assertFalse(outcome.changed)

    def test_duplicate_is_a_noop_without_downloads(self):
        self.seed_beta(V2)
        fetcher = site([f"beta-{V2}"], [V2])
        outcome = self.propose(fetcher)
        self.assertEqual((outcome.reason, outcome.previous), ("duplicate", V2))
        self.assertEqual((fetcher.gets, fetcher.downloads), ([ROOT], []))

    def test_older_publication_never_downgrades_beta(self):
        self.seed_beta(V10)
        fetcher = site([f"beta-{V2}"], [V2])
        outcome = self.propose(fetcher)
        self.assertEqual(outcome.reason, "older")
        self.assertEqual(fetcher.downloads, [])

    def test_already_proposed_version_is_a_noop_without_downloads(self):
        fetcher = site([f"beta-{V2}"], [V2])
        outcome = self.propose(fetcher, proposed=frozenset({V2}))
        self.assertEqual(outcome.reason, "proposed")
        self.assertEqual((fetcher.gets, fetcher.downloads), ([ROOT], []))


class ManifestTests(UpdaterCase):
    def assert_only_source_changed(self, outcome):
        old = MASTER.read_text(encoding="utf-8").split("\n")
        new = outcome.manifest.split("\n")
        marker = old.index("      - type: archive")
        self.assertEqual(new[: marker + 1], old[: marker + 1], "runtime/wrapper must be untouched")
        digest = hashlib.sha512(make_archive()).hexdigest()
        for line in (
            f"        url: {base(outcome.version)}QV.AC-x64-flatpak.tar.gz",
            f"        sha512: {digest}",
            "        strip-components: 0",
            "        x-checker-data:",
            "          type: html",
            "          url: https://static.tether.dev/qv.ac/",
            r'          version-pattern: href="\./beta-(\d+\.\d+\.\d+-rc\.\d+\.\d+)/"',
            "          url-template: https://static.tether.dev/qv.ac/beta-$version/flatpak/QV.AC-x64-flatpak.tar.gz",
        ):
            self.assertIn(line, new)
        self.assertNotIn("qv.ac/0.20.1/", outcome.manifest)
        self.assertTrue(outcome.manifest.endswith("\n"))

    def test_bootstrap_copies_master_and_replaces_only_the_source(self):
        fetcher = site([f"beta-{V2}", "0.20.1"], [V2])
        outcome = self.propose(fetcher, master_rev="a" * 40)
        self.assertEqual((outcome.reason, outcome.previous), ("bootstrap", None))
        self.assert_only_source_changed(outcome)
        self.assertEqual(fetcher.downloads, [base(V2) + "QV.AC-x64-flatpak.tar.gz"], "arm64 is never downloaded")
        self.assertFalse(list(self.beta.iterdir()), "propose alone writes nothing")
        self.assertIn("a" * 40, outcome.body)

    def test_bootstrap_ignores_obsolete_beta_packaging(self):
        (self.beta / MASTER.name).write_text("id: io.example.Obsolete\nmodules: []\n", encoding="utf-8")
        outcome = self.propose(site([f"beta-{V2}"], [V2]))
        self.assertEqual(outcome.reason, "bootstrap")
        self.assert_only_source_changed(outcome)

    def test_steady_state_keeps_beta_edits_and_replaces_only_the_source(self):
        self.seed_beta(V1, note="# beta-only human note\n")
        outcome = self.propose(site([f"beta-{V1}", f"beta-{V2}"], [V1, V2]))
        self.assertEqual((outcome.reason, outcome.previous, outcome.version), ("updated", V1, V2))
        self.assertTrue(outcome.manifest.startswith("# beta-only human note\n"))
        self.assertIn(base(V2) + "QV.AC-x64-flatpak.tar.gz", outcome.manifest)
        self.assertNotIn(base(V1), outcome.manifest)

    def test_substitute_refuses_unexpected_structure(self):
        text = MASTER.read_text(encoding="utf-8")
        digest = "0" * 128
        doubled = text.replace("      - type: archive\n", "      - type: archive\n        url: https://x/y\n", 1)
        for broken in (
            doubled,
            text.replace("        x-checker-data:\n", "        x-checker-extra:\n"),
            text.replace("  - name: qvac-workbench", "  - name: renamed"),
            text.replace("\n", "\r\n"),
        ):
            with self.assertRaises(ub.UpdateError):
                ub.substitute(broken, V2, digest)

    def test_manifest_must_reference_the_packaged_filenames(self):
        text = MASTER.read_text(encoding="utf-8")
        ub.check_manifest_filenames(text, APP_ID)
        with self.assertRaises(ub.UpdateError):
            ub.check_manifest_filenames(text.replace(f"{APP_ID}.png", "other.png"), APP_ID)
        with self.assertRaises(ub.UpdateError):
            ub.check_manifest_filenames(text, APP_ID + "2")

    def test_symlinked_beta_manifest_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        elsewhere = self.root / "elsewhere.yml"
        elsewhere.write_text("id: x\n", encoding="utf-8")
        os.symlink(elsewhere, self.beta / MASTER.name)
        with self.assertRaises(ub.UpdateError):
            self.propose(site([f"beta-{V2}"], [V2]))
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "id: x\n")


class PublicationTests(UpdaterCase):
    def test_half_uploaded_publication_is_a_noop(self):
        for victim in (
            "QV.AC-x64-flatpak.tar.gz",
            "QV.AC-x64-flatpak.tar.gz.sha512",
            "io.tether.qvac.source-x64.yml",
            "QV.AC-arm64-flatpak.tar.gz.sha512",
            "io.tether.qvac.source-arm64.yml",
        ):
            with self.subTest(victim):
                fetcher = site([f"beta-{V2}"], [V2])
                del fetcher.files[base(V2) + victim]
                outcome = self.propose(fetcher)
                self.assertEqual(outcome.reason, "incomplete")
                self.assertFalse(outcome.changed)
                self.assertIn(victim, outcome.detail)

    def test_inconsistent_publication_is_an_error(self):
        archive = base(V2) + "QV.AC-x64-flatpak.tar.gz"
        digest = hashlib.sha512(make_archive()).hexdigest()
        other = "0" * 128
        snippet = base(V2) + "io.tether.qvac.source-x64.yml"
        cases = {
            "sha512 file disagrees with snippet": (archive + ".sha512", f"{other}  QV.AC-x64-flatpak.tar.gz\n"),
            "sha512 file names another arch": (archive + ".sha512", f"{digest}  QV.AC-arm64-flatpak.tar.gz\n"),
            "sha512 file lacks newline": (archive + ".sha512", f"{digest}  QV.AC-x64-flatpak.tar.gz"),
            "sha512 file uppercase": (archive + ".sha512", f"{digest.upper()}  QV.AC-x64-flatpak.tar.gz\n"),
            "snippet points at production": (
                snippet,
                f"- type: archive\n  url: https://static.tether.dev/qv.ac/0.20.1/QV.AC-x64-flatpak.tar.gz\n  sha512: {digest}\n  strip-components: 0\n",
            ),
            "snippet has an extra key": (
                snippet,
                f"- type: archive\n  url: {archive}\n  sha512: {digest}\n  strip-components: 0\n  extra: 1\n",
            ),
            "snippet is not utf-8": (snippet, b"\xff\xfe"),
            "arm64 sha512 file disagrees": (
                base(V2) + "QV.AC-arm64-flatpak.tar.gz.sha512",
                f"{other}  QV.AC-arm64-flatpak.tar.gz\n",
            ),
            "archive bytes differ from published sha512": (archive, make_archive() + b"tamper"),
            "archive is not a tar.gz": (archive, b"not an archive"),
        }
        for label, (url, body) in cases.items():
            with self.subTest(label):
                fetcher = site([f"beta-{V2}"], [V2])
                fetcher.files[url] = body if isinstance(body, bytes) else body.encode()
                if label == "archive is not a tar.gz":
                    fetcher.files.update(publication(V2, archive=b"not an archive"))
                with self.assertRaises(ub.UpdateError):
                    self.propose(fetcher)

    def test_archive_must_ship_the_files_the_manifest_installs(self):
        cases = (
            (make_archive("io.example.Other"), f"{APP_ID}.desktop"),
            (make_archive(omit=("run.sh",)), "run.sh"),
            (make_archive(omit=("main/app",)), "main/"),
            (make_archive(omit=(f"{APP_ID}.png",)), f"{APP_ID}.png"),
        )
        for archive, expected in cases:
            with self.subTest(expected):
                fetcher = FakeFetcher({ROOT: listing(f"beta-{V2}").encode(), **publication(V2, archive=archive)})
                with self.assertRaisesRegex(ub.UpdateError, re.escape(expected)):
                    self.propose(fetcher)

    def test_oversized_download_is_an_error(self):
        with self.assertRaises(ub.UpdateError):
            self.propose(site([f"beta-{V2}"], [V2]), max_archive_bytes=10)

    def test_archive_that_expands_too_far_is_an_error(self):
        path = self.root / "a.tar.gz"
        path.write_bytes(make_archive())
        with self.assertRaisesRegex(ub.UpdateError, "expands"):
            ub.missing_members(str(path), APP_ID, 100, ub.Deadline(60))
        self.assertEqual(ub.missing_members(str(path), APP_ID, 1 << 20, ub.Deadline(60)), [])


class BoundsTests(unittest.TestCase):
    def test_copy_limited_enforces_size_and_returns_digest(self):
        sink = io.BytesIO()
        digest, size = ub.copy_limited(io.BytesIO(b"12345"), sink, 5, ub.Deadline(60))
        self.assertEqual((digest, size), (hashlib.sha512(b"12345").hexdigest(), 5))
        with self.assertRaises(ub.UpdateError):
            ub.copy_limited(io.BytesIO(b"123456"), io.BytesIO(), 5, ub.Deadline(60))

    def test_expired_deadline_stops_work(self):
        with self.assertRaises(ub.UpdateError):
            ub.copy_limited(io.BytesIO(b"x"), io.BytesIO(), 5, ub.Deadline(-1))

    def test_only_https_on_the_static_host_is_allowed(self):
        ub.check_url("https://static.tether.dev/qv.ac/")
        for url in (
            "http://static.tether.dev/qv.ac/",
            "https://evil.example/qv.ac/",
            "https://static.tether.dev.evil.example/",
            "https://user@static.tether.dev/",
            "https://static.tether.dev:8443/",
            "file:///etc/passwd",
        ):
            with self.subTest(url), self.assertRaises(ub.UpdateError):
                ub.check_url(url)

    def test_redirects_stay_on_host_and_are_capped(self):
        handler = ub._BoundedRedirects()
        self.assertLessEqual(handler.max_redirections, 3)
        request = urllib.request.Request("https://static.tether.dev/qv.ac/a")
        followed = handler.redirect_request(request, None, 302, "Found", {}, "https://static.tether.dev/qv.ac/b")
        self.assertEqual(followed.full_url, "https://static.tether.dev/qv.ac/b")
        for target in ("https://evil.example/x", "http://static.tether.dev/x"):
            with self.subTest(target), self.assertRaises(ub.UpdateError):
                handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_output_is_single_line_and_bounded(self):
        cleaned = ub.clean("a\n::error::b\x00" + "x" * 1000)
        self.assertNotIn("\n", cleaned)
        self.assertLessEqual(len(cleaned), 300)


class CliTests(UpdaterCase):
    def test_end_to_end_update_then_duplicate(self):
        fetcher = site([f"beta-{V2}"], [V2])
        code, out, err = self.run_main(fetcher)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("bootstrap", out)
        self.assertEqual(
            self.outputs(),
            {"changed": "true", "reason": "bootstrap", "version": V2, "branch": f"beta-update/{V2}"},
        )
        written = (self.beta / MASTER.name).read_text(encoding="utf-8")
        self.assertIn(base(V2) + "QV.AC-x64-flatpak.tar.gz", written)
        body = (self.root / "body.md").read_text(encoding="utf-8")
        self.assertIn(hashlib.sha512(make_archive()).hexdigest(), body)
        self.assertIn("Human review and merge required", body)
        self.assertIn("never merges", body)
        self.assertIn("never closed automatically", body)
        self.assertEqual(sorted(p.name for p in self.beta.iterdir()), [MASTER.name], "no stray temp files")

        (self.root / "out.txt").unlink()
        code, out, _ = self.run_main(fetcher)
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs(), {"changed": "false", "reason": "duplicate", "version": V2, "branch": ""})
        self.assertEqual((self.beta / MASTER.name).read_text(encoding="utf-8"), written)
        self.assertIn("Beta updater", (self.root / "summary.md").read_text(encoding="utf-8"))

    def test_half_uploaded_publication_writes_nothing_and_succeeds(self):
        fetcher = site([f"beta-{V2}"], [V2])
        del fetcher.files[base(V2) + "QV.AC-arm64-flatpak.tar.gz.sha512"]
        code, out, _ = self.run_main(fetcher)
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs()["reason"], "incomplete")
        self.assertEqual(self.outputs()["changed"], "false")
        self.assertEqual(list(self.beta.iterdir()), [])
        self.assertFalse((self.root / "body.md").exists())

    def test_verification_failure_exits_nonzero_and_writes_nothing(self):
        fetcher = site([f"beta-{V2}"], [V2])
        fetcher.files[base(V2) + "QV.AC-x64-flatpak.tar.gz"] += b"tamper"
        code, out, err = self.run_main(fetcher)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error:"))
        self.assertEqual(list(self.beta.iterdir()), [])

    def test_dry_run_verifies_but_writes_no_manifest_or_body(self):
        code, _, _ = self.run_main(site([f"beta-{V2}"], [V2]), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs()["changed"], "true")
        self.assertEqual(list(self.beta.iterdir()), [])
        self.assertFalse((self.root / "body.md").exists())

    def test_proposed_file_skips_known_versions_and_ignores_junk(self):
        proposed = self.root / "proposed.txt"
        proposed.write_text(f"junk\n\n{V2}\n../../etc\n", encoding="utf-8")
        fetcher = site([f"beta-{V2}"], [V2])
        code, _, _ = self.run_main(fetcher, "--proposed", str(proposed))
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs()["reason"], "proposed")
        self.assertEqual(fetcher.downloads, [])


class ProposalTests(UpdaterCase):
    def test_this_repos_actions_bot_counts_in_every_state(self):
        pulls = [pull(V1, state="open"), pull(V2, state="closed"), pull(V10, state="closed")]
        self.assertEqual(ub.trusted_proposals(pulls, REPO_NAME), {V1, V2, V10})
        self.assertEqual(ub.trusted_proposals([pull(V1, head_repo=REPO_NAME.upper())], REPO_NAME), {V1})

    def test_nothing_else_counts(self):
        cases = {
            "fork by a human": pull(head_repo=FORK, user=HUMAN),
            "closed fork by a human": pull(head_repo=FORK, user=HUMAN, state="closed"),
            "deleted fork": pull(head_repo=None, user=HUMAN, state="closed"),
            "bot account on a fork": pull(head_repo=FORK),
            "human on this repo with the same prefix": pull(user=HUMAN),
            "closed human pull request on this repo": pull(user=HUMAN, state="closed"),
            "user that only looks like the bot": pull(user={"login": BOT["login"], "id": 1, "type": "User"}),
            "bot login without the suffix": pull(user={"login": "github-actions", "id": BOT["id"], "type": "Bot"}),
            "right login, wrong id": pull(user={"login": BOT["login"], "id": 99, "type": "Bot"}),
            "other bot": pull(user={"login": "dependabot[bot]", "id": 49699333, "type": "Bot"}),
            "targets another base": pull(base_ref="master"),
            "other branch prefix": pull(ref="update-master-4e59a0e"),
            "branch with an invalid version": pull(ref="beta-update/0.21.0-rc.2"),
            "no author": {**pull(), "user": None},
            "not an object": "beta-update/" + V2,
        }
        for label, case in cases.items():
            with self.subTest(label):
                self.assertEqual(ub.trusted_proposals([case], REPO_NAME), set())

    def test_untrusted_pulls_never_hide_or_replace_a_trusted_one(self):
        untrusted = [
            pull(V2, head_repo=FORK, user=HUMAN),
            pull(V2, head_repo=None, user=HUMAN, state="closed"),
            pull(V2, user=HUMAN),
        ]
        self.assertEqual(ub.trusted_proposals(untrusted, REPO_NAME), set())
        self.assertEqual(ub.trusted_proposals([*untrusted, pull(V2)], REPO_NAME), {V2})

    def test_untrusted_pulls_do_not_suppress_the_verified_update(self):
        untrusted = [
            pull(V2, head_repo=FORK, user=HUMAN),
            pull(V2, head_repo=None, user=HUMAN, state="closed"),
            pull(V2, user=HUMAN, state="closed"),
        ]
        fetcher = site([f"beta-{V2}"], [V2])
        outcome = self.propose(fetcher, proposed=ub.trusted_proposals(untrusted, REPO_NAME))
        self.assertEqual(outcome.reason, "bootstrap")
        self.assertEqual(len(fetcher.downloads), 1)

    def test_a_trusted_pull_suppresses_the_update_even_when_closed(self):
        for state in ("open", "closed"):
            with self.subTest(state):
                fetcher = site([f"beta-{V2}"], [V2])
                outcome = self.propose(fetcher, proposed=ub.trusted_proposals([pull(V2, state=state)], REPO_NAME))
                self.assertEqual(outcome.reason, "proposed")
                self.assertEqual(fetcher.downloads, [])

    def test_a_pull_for_another_version_does_not_suppress_the_newest(self):
        fetcher = site([f"beta-{V1}", f"beta-{V2}"], [V2])
        outcome = self.propose(fetcher, proposed=ub.trusted_proposals([pull(V1)], REPO_NAME))
        self.assertEqual((outcome.reason, outcome.version), ("bootstrap", V2))

    def test_malformed_pull_lists_are_rejected(self):
        for text in ("not json", '{"head": {}}', "[" * 100000):
            with self.subTest(text[:12]), self.assertRaises(ub.UpdateError):
                ub.parse_pulls(text)
        self.assertEqual(ub.parse_pulls("[]"), [])

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ub.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_proposals_command_prints_trusted_versions_in_numeric_order(self):
        pulls = self.root / "pulls.json"
        pulls.write_text(
            json.dumps([pull(V10), pull(V2), pull(V1, head_repo=FORK, user=HUMAN), pull(V1, user=HUMAN)]),
            encoding="utf-8",
        )
        code, out, err = self.run_cli("proposals", "--pulls", str(pulls), "--repo", REPO_NAME)
        self.assertEqual((code, out, err), (0, f"{V2}\n{V10}\n", ""))

    def test_proposals_command_fails_closed_on_bad_input(self):
        pulls = self.root / "pulls.json"
        pulls.write_text("<html>rate limited</html>", encoding="utf-8")
        code, out, err = self.run_cli("proposals", "--pulls", str(pulls), "--repo", REPO_NAME)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error:"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_cli("proposals", "--pulls", str(pulls), "--repo", "not a repo")

    def test_verified_head_counts_any_author_in_any_state_on_the_exact_head(self):
        branch = f"beta-update/{V2}"
        cases = {
            "human, open": pull(V2, user=HUMAN),
            "human, closed (a declined proposal)": pull(V2, user=HUMAN, state="closed"),
            "bot, open": pull(V2),
            "bot, closed": pull(V2, state="closed"),
            "other bot": pull(V2, user={"login": "dependabot[bot]", "id": 49699333, "type": "Bot"}),
            "no author": {**pull(V2), "user": None},
            "repository name in another case": pull(V2, head_repo=REPO_NAME.upper()),
        }
        for label, case in cases.items():
            with self.subTest(label):
                self.assertEqual(ub.verified_head_proposals([case], REPO_NAME, branch), {V2})

    def test_verified_head_ignores_other_heads_forks_and_other_bases(self):
        branch = f"beta-update/{V2}"
        cases = {
            "another version's branch": pull(V1, user=HUMAN),
            "fork with the same branch name, open": pull(V2, head_repo=FORK, user=HUMAN),
            "fork with the same branch name, closed": pull(V2, head_repo=FORK, user=HUMAN, state="closed"),
            "bot account on a fork": pull(V2, head_repo=FORK),
            "deleted fork": pull(V2, head_repo=None, user=HUMAN, state="closed"),
            "same branch into another base": pull(V2, base_ref="master", user=HUMAN),
            "other prefix": pull(V2, ref=f"feature/{V2}", user=HUMAN),
            "branch name that only starts like ours": pull(V2, ref=f"beta-update/{V2}-extra", user=HUMAN),
            "not an object": "beta-update/" + V2,
        }
        for label, case in cases.items():
            with self.subTest(label):
                self.assertEqual(ub.verified_head_proposals([case], REPO_NAME, branch), set())

    def test_verified_head_only_accepts_strict_update_branch_names(self):
        names = (
            "",
            "beta",
            "beta-update/",
            "beta-update/0.21.0-rc.2",
            "beta-update/0.21.0-rc.2.05",
            f"beta-update/{V2}/x",
            f"../beta-update/{V2}",
            f"Beta-update/{V2}",
        )
        for name in names:
            with self.subTest(name), self.assertRaises(ub.UpdateError):
                ub.verified_head_proposals([pull(V2, user=HUMAN)], REPO_NAME, name)

    def test_bulk_filter_stays_strict_while_the_exact_head_lookup_is_not(self):
        pulls = [pull(V2, user=HUMAN), pull(V2, user=HUMAN, state="closed")]
        self.assertEqual(ub.trusted_proposals(pulls, REPO_NAME), set())
        self.assertEqual(ub.verified_head_proposals(pulls, REPO_NAME, f"beta-update/{V2}"), {V2})

    def test_manual_pull_after_a_failed_bot_run_is_neither_duplicated_nor_reproposed(self):
        # The bot pushed the verified branch, PR creation failed, then a maintainer opened a PR
        # on it by hand, and possibly closed it to decline.
        for state in ("open", "closed"):
            with self.subTest(state):
                pulls = [pull(V2, user=HUMAN, state=state)]
                outcome = self.propose(
                    site([f"beta-{V2}"], [V2]), proposed=ub.trusted_proposals(pulls, REPO_NAME)
                )
                self.assertEqual(outcome.reason, "bootstrap", "the strict bulk list does not see it, so verification re-runs")
                branch = ub.BRANCH_PREFIX + outcome.version
                self.assertEqual(ub.verified_head_proposals(pulls, REPO_NAME, branch), {outcome.version})

    def test_proposals_command_verified_head_mode(self):
        branch = f"beta-update/{V2}"
        pulls = self.root / "pulls.json"
        pulls.write_text(
            json.dumps([pull(V2, user=HUMAN, state="closed"), pull(V2, head_repo=FORK, user=HUMAN)]),
            encoding="utf-8",
        )
        args = ("proposals", "--pulls", str(pulls), "--repo", REPO_NAME)
        self.assertEqual(self.run_cli(*args), (0, "", ""), "bulk mode still ignores the human pull request")
        self.assertEqual(self.run_cli(*args, "--verified-head", branch), (0, f"{V2}\n", ""))

        pulls.write_text(json.dumps([pull(V2, head_repo=FORK, user=HUMAN), pull(V1, user=HUMAN)]), encoding="utf-8")
        self.assertEqual(self.run_cli(*args, "--verified-head", branch), (0, "", ""))

        code, out, err = self.run_cli(*args, "--verified-head", "beta-update/0.21.0-rc.2")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error:"))


class BranchDecisionTests(unittest.TestCase):
    BRANCH = f"beta-update/{V2}"
    BASE, TREE, TIP, OTHER = "a" * 40, "b" * 40, "c" * 40, "d" * 40

    def check(self, tip_line, tip_tree=None):
        return ub.check_branch(self.BRANCH, self.BASE, self.TREE, self.TIP, tip_line, tip_tree or self.TREE)

    def test_exactly_the_verified_update_on_the_current_beta_head_is_reused(self):
        self.assertEqual(self.check(f"{self.TIP} {self.BASE}"), "reuse")

    def test_anything_else_fails_with_repair_steps_and_no_force(self):
        cases = {
            "extra commit on top": (f"{self.TIP} {self.OTHER}", None),
            "beta moved on": (f"{self.TIP} {self.OTHER}", self.TREE),
            "root commit": (self.TIP, None),
            "merge commit": (f"{self.TIP} {self.BASE} {self.OTHER}", None),
            "different content": (f"{self.TIP} {self.BASE}", self.OTHER),
            "both": (f"{self.TIP} {self.OTHER}", self.OTHER),
        }
        for label, (line, tree) in cases.items():
            with self.subTest(label), self.assertRaises(ub.UpdateError) as raised:
                self.check(line, tree)
            message = str(raised.exception)
            self.assertIn(self.BRANCH, message)
            self.assertIn(f"git push origin --delete {self.BRANCH}", message)
            self.assertIn("Repair", message)
            self.assertNotIn("force", message)

    def test_unexpected_git_output_is_an_error(self):
        for line in ("", f"{self.OTHER} {self.BASE}", f"{self.TIP} zz", f"{self.TIP} {'a' * 7}"):
            with self.subTest(line), self.assertRaisesRegex(ub.UpdateError, "unexpected git output"):
                self.check(line)

    def test_only_update_branch_names_are_accepted(self):
        for name in ("beta", "beta-update/", "beta-update/0.21.0-rc.2", "../beta-update/" + V2):
            with self.subTest(name), self.assertRaises(ub.UpdateError):
                ub.check_branch(name, self.BASE, self.TREE, self.TIP, f"{self.TIP} {self.BASE}", self.TREE)

    def test_only_the_exact_remote_ref_counts(self):
        calls = []
        # ls-remote patterns match by tail, so a longer ref can be listed for our branch name.
        answers = {
            "rev-parse": self.BASE,
            "write-tree": self.TREE,
            "ls-remote": f"{self.OTHER}\trefs/heads/other/{self.BRANCH}",
        }

        def fake_git(*args):
            calls.append(args[0])
            return answers.get(args[0], "")

        self.assertEqual(ub.plan_branch("unused", "manifest.yml", self.BRANCH, git=fake_git), "create")
        self.assertNotIn("fetch", calls)


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitBranchTests(unittest.TestCase):
    """plan_branch against real local repositories: an `origin` and shallow clones of its beta."""

    BRANCH = f"beta-update/{V2}"
    NAME = "manifest.yml"

    def setUp(self):
        environment = mock.patch.dict(os.environ, GIT_ENV)
        environment.start()
        self.addCleanup(environment.stop)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.origin = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", str(self.origin))
        self.seed = self.root / "seed"
        self.git(self.root, "init", "-q", str(self.seed))
        self.git(self.seed, "checkout", "-q", "-b", "beta")
        (self.seed / self.NAME).write_text("old\n", encoding="utf-8")
        (self.seed / "flathub.json").write_text("{}\n", encoding="utf-8")
        self.git(self.seed, "add", "-A")
        self.git(self.seed, "commit", "-q", "-m", "seed")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.git(self.seed, "push", "-q", "origin", "beta")

    def git(self, cwd, *args):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"git {' '.join(args)}: {result.stderr}")
        return result.stdout.strip()

    def clone(self, name):
        destination = self.root / name
        self.git(self.root, "clone", "-q", "--depth", "1", "--branch", "beta", self.origin.as_uri(), str(destination))
        return destination

    def plan(self, clone, text, branch=None):
        (clone / self.NAME).write_text(text, encoding="utf-8")
        return ub.plan_branch(str(clone), self.NAME, branch or self.BRANCH)

    def commit_and_push(self, clone, branch=None):
        """What the workflow does after a 'create' plan."""
        branch = branch or self.BRANCH
        self.git(clone, "switch", "-q", "-c", branch)
        self.git(clone, "commit", "-q", "-m", "update")
        self.git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    def remote_tip(self, branch=None):
        return self.git(self.origin, "rev-parse", f"refs/heads/{branch or self.BRANCH}")

    def publish(self, text="new\n"):
        first = self.clone("first")
        self.assertEqual(self.plan(first, text), "create")
        self.commit_and_push(first)
        return first, self.remote_tip()

    def test_first_run_stages_only_the_manifest_and_asks_to_create(self):
        clone = self.clone("first")
        self.assertEqual(self.plan(clone, "new\n"), "create")
        self.assertEqual(self.git(clone, "diff", "--cached", "--name-only"), self.NAME)

    def test_retry_after_a_crash_reuses_the_branch_without_touching_it(self):
        _, tip = self.publish()  # the crash happens here: pushed, but no PR was opened
        self.assertEqual(self.plan(self.clone("retry"), "new\n"), "reuse")
        self.assertEqual(self.remote_tip(), tip)

    def test_other_versions_do_not_interfere(self):
        self.publish()
        self.assertEqual(self.plan(self.clone("other"), "new\n", f"beta-update/{V10}"), "create")

    def test_branch_with_other_content_is_refused_and_left_alone(self):
        _, tip = self.publish("new\n")
        with self.assertRaisesRegex(ub.UpdateError, "does not contain exactly the verified update") as raised:
            self.plan(self.clone("retry"), "newer\n")
        self.assertIn(f"git push origin --delete {self.BRANCH}", str(raised.exception))
        self.assertEqual(self.remote_tip(), tip)

    def test_branch_with_extra_history_is_refused_even_if_the_content_matches(self):
        first, _ = self.publish()
        self.git(first, "commit", "-q", "--allow-empty", "-m", "noop")
        self.git(first, "push", "-q", "origin", f"HEAD:refs/heads/{self.BRANCH}")
        tip = self.remote_tip()
        with self.assertRaisesRegex(ub.UpdateError, "not exactly one commit on the current beta head"):
            self.plan(self.clone("retry"), "new\n")
        self.assertEqual(self.remote_tip(), tip)

    def test_branch_based_on_an_older_beta_is_refused(self):
        _, tip = self.publish()
        (self.seed / "flathub.json").write_text('{"only-arches": ["x86_64"]}\n', encoding="utf-8")
        self.git(self.seed, "commit", "-q", "-am", "beta moved on")
        self.git(self.seed, "push", "-q", "origin", "beta")
        with self.assertRaisesRegex(ub.UpdateError, "current beta head"):
            self.plan(self.clone("retry"), "new\n")
        self.assertEqual(self.remote_tip(), tip)

    def test_git_failures_are_update_errors(self):
        with self.assertRaisesRegex(ub.UpdateError, "git add failed"):
            ub.plan_branch(str(self.root / "missing"), self.NAME, self.BRANCH)

    def test_cli_prints_the_plan_and_fails_closed(self):
        argv = ["plan-branch", "--manifest", self.NAME, "--branch", self.BRANCH]
        first = self.clone("first")
        (first / self.NAME).write_text("new\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(ub.main([*argv, "--beta-dir", str(first)]), 0)
        self.assertEqual(out.getvalue(), "create\n")
        self.commit_and_push(first)

        retry = self.clone("retry")
        (retry / self.NAME).write_text("new\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ub.main([*argv, "--beta-dir", str(retry)]), 0)
        self.assertEqual(out.getvalue(), "reuse\n")

        changed = self.clone("changed")
        (changed / self.NAME).write_text("newer\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(ub.main([*argv, "--beta-dir", str(changed)]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Repair", err.getvalue())


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.text = (WORKFLOWS / "beta-updater.yml").read_text(encoding="utf-8")

    def test_only_scheduled_and_manual_triggers(self):
        self.assertRegex(self.text, r"(?m)^  schedule:$")
        self.assertRegex(self.text, r"(?m)^  workflow_dispatch:$")
        self.assertNotRegex(self.text, r"(?m)^  (push|pull_request\w*|workflow_run|repository_dispatch|issue_comment):")

    def test_runs_only_from_master_of_the_flathub_repo(self):
        self.assertIn("github.repository == 'flathub/io.tether.QVAC.Workbench'", self.text)
        self.assertIn("github.ref == 'refs/heads/master'", self.text)
        self.assertNotIn("default_branch", self.text, "the guard must not depend on the event payload shape")

    def test_code_is_trusted_and_beta_is_data(self):
        self.assertIn("ref: ${{ github.sha }}\n          path: trusted", self.text)
        self.assertIn("ref: beta\n          path: beta", self.text)
        self.assertEqual(self.text.count("persist-credentials: false"), 2)
        interpreters = re.findall(r"python3 (\S+) (\S+) (\S+)", self.text)
        self.assertEqual(
            [command for *_, command in interpreters],
            ["proposals", "update", "plan-branch", "proposals"],
        )
        for flag, script, _ in interpreters:
            self.assertEqual((flag, script), ("-I", "trusted/.github/beta-updater/update_beta.py"))
        self.assertNotIn("git -C trusted", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*(bash|sh|source|\.|node|python3?)\s+(\./)?beta/")

    def test_least_privilege_and_no_extra_credentials(self):
        self.assertIn("\npermissions: {}\n", self.text)
        self.assertEqual(re.findall(r"(?m)^      (\w[\w-]*): (read|write)$", self.text),
                         [("contents", "write"), ("pull-requests", "write")])  # fmt: skip
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("id-token", self.text)

    def test_never_merges_and_never_closes(self):
        for needle in ("pr merge", "pr close", "--auto", "--admin", "auto-merge", "automerge", "AutoMerge"):
            self.assertNotIn(needle, self.text)
        self.assertIn("gh pr create", self.text)
        self.assertIn("--base beta", self.text)

    def test_only_the_lookup_after_plan_branch_uses_verified_head_mode(self):
        invocations = re.findall(r"update_beta\.py proposals \\\n\s*(--pulls [^\n]*)", self.text)
        self.assertEqual(len(invocations), 2)
        self.assertNotIn("--verified-head", invocations[0], "the bulk list must stay strict")
        self.assertIn('--verified-head "$BRANCH"', invocations[1])
        self.assertLess(self.text.index("plan-branch"), self.text.index('--verified-head "$BRANCH"'))

    def test_gh_api_is_only_used_to_read_pull_requests(self):
        calls = re.findall(r"(?m)^.*\bgh api\b.*$", self.text)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertRegex(call, r'gh api "repos/\$GITHUB_REPOSITORY/pulls\?state=all&')
            self.assertNotRegex(call, r"\s(-X|-f|-F|--method|--field|--raw-field|--input)(\s|=)")

    def test_only_the_beta_manifest_is_staged_and_committed(self):
        # plan-branch stages exactly --manifest (a test above proves that); nothing else is added.
        self.assertIn('--manifest "$MANIFEST"', self.text)
        self.assertNotRegex(self.text, r"git -C beta (add|commit -a|commit --all)")

    def test_actions_are_pinned_to_commits(self):
        for path in WORKFLOWS.glob("beta-updater*.yml"):
            uses = re.findall(r"uses:\s*(\S+)", path.read_text(encoding="utf-8"))
            self.assertTrue(uses)
            for reference in uses:
                self.assertRegex(reference, r"@[0-9a-f]{40}$", f"{path.name}: {reference}")

    def test_test_workflow_is_read_only(self):
        text = (WORKFLOWS / "beta-updater-tests.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read\n", text)
        self.assertNotIn("write", text)
        self.assertNotIn("secrets.", text)


if __name__ == "__main__":
    unittest.main()
