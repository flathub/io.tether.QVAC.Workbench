#!/usr/bin/env python3
"""Propose a beta update of the QV.AC Flatpak from a verified staging publication.

Standard library only. The trusted production manifest and the beta checkout are
read as plain text (never executed, never YAML-parsed); only the source url,
sha512 and x-checker-data of the qvac-workbench archive source are rewritten.
See README.md next to this file.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

HOST = "static.tether.dev"
ROOT_URL = f"https://{HOST}/qv.ac/"
ARCHES = ("x64", "arm64")  # QVAC publishes both; the manifest only consumes x64
MANIFEST_ARCH = "x64"
BRANCH_PREFIX = "beta-update/"
# Pull requests opened with the job's GITHUB_TOKEN are authored by this account (REST `user`).
BOT_LOGIN = "github-actions[bot]"
BOT_ID = 41898282

NUMBER = r"(?:0|[1-9]\d{0,8})"
VERSION = rf"{NUMBER}\.{NUMBER}\.{NUMBER}-rc\.{NUMBER}\.{NUMBER}"
LISTING_RE = re.compile(rf'href="\./beta-({VERSION})/"')
BRANCH_RE = re.compile(rf"{re.escape(BRANCH_PREFIX)}({VERSION})")
SHA = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
STAGING_URL = rf"https://{re.escape(HOST)}/qv\.ac/beta-({VERSION})/flatpak/QV\.AC-{MANIFEST_ARCH}-flatpak\.tar\.gz"

MAX_LISTING = 1 << 20
MAX_METADATA = 4096
MAX_MANIFEST = 256 << 10
MAX_PROPOSED = 64 << 10
MAX_PULLS = 32 << 20
GIT_TIMEOUT = 120
MAX_MEMBERS = 1_000_000
DEFAULT_MAX_ARCHIVE = 3 << 30
DEFAULT_TIME_BUDGET = 1200

CHECKER = [
    "        x-checker-data:",
    "          type: html",
    f"          url: {ROOT_URL}",
    r'          version-pattern: href="\./beta-(\d+\.\d+\.\d+-rc\.\d+\.\d+)/"',
    f"          url-template: {ROOT_URL}beta-$version/flatpak/QV.AC-{MANIFEST_ARCH}-flatpak.tar.gz",
]

REASONS = {
    "updated": "newer staging publication verified, beta manifest updated",
    "bootstrap": "staging publication verified, beta manifest rebuilt from the production manifest",
    "duplicate": "beta already points at the newest publication",
    "older": "newest publication is older than beta, not downgrading",
    "proposed": "an updater pull request for the newest publication already exists",
    "no-publication": "no beta-X.Y.Z-rc.N.B directory is listed",
    "incomplete": "newest publication is not fully uploaded yet, will retry",
}


class UpdateError(Exception):
    """Invalid manifest or publication: the run must fail visibly."""


class NotPublished(Exception):
    """A required URL does not exist (yet): the run is a no-op."""


@dataclasses.dataclass
class Outcome:
    reason: str
    version: str | None = None
    previous: str | None = None
    detail: str = ""
    manifest: str | None = None
    manifest_path: str | None = None
    body: str | None = None

    @property
    def changed(self):
        return self.manifest is not None


# --- publication layout (must match QVAC scripts/flatpak-publication-contract.cjs) ---


def version_key(version):
    return tuple(int(part) for part in re.split(r"\.|-rc\.", version))


def archive_name(arch):
    return f"QV.AC-{arch}-flatpak.tar.gz"


def snippet_name(arch):
    return f"io.tether.qvac.source-{arch}.yml"


def publication_url(version, name):
    return f"https://{HOST}/qv.ac/beta-{version}/flatpak/{name}"


def canonical_snippet(url, digest):
    return f"- type: archive\n  url: {url}\n  sha512: {digest}\n  strip-components: 0\n"


def required_members(app_id):
    return (f"{app_id}.desktop", f"{app_id}.metainfo.xml", f"{app_id}.png", "run.sh")


# --- bounded network access ---


def clean(message, limit=300):
    return re.sub(r"[^\x20-\x7e]", "?", message)[:limit]


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or parts.hostname != HOST
        or port not in (None, 443)
        or parts.username
        or parts.password
    ):
        raise UpdateError(f"refusing URL outside https://{HOST}/: {clean(url, 120)}")


class Deadline:
    def __init__(self, seconds, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def check(self):
        if self._clock() > self._end:
            raise UpdateError("time budget exceeded")


def copy_limited(source, sink, max_bytes, deadline, chunk=1 << 20):
    """Copy source to sink, returning (sha512 hex, size); fail past max_bytes or the deadline."""
    digest = hashlib.sha512()
    size = 0
    while True:
        deadline.check()
        block = source.read(chunk)
        if not block:
            return digest.hexdigest(), size
        size += len(block)
        if size > max_bytes:
            raise UpdateError(f"download exceeds {max_bytes} bytes")
        digest.update(block)
        sink.write(block)


class _BoundedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpFetcher:
    """GET restricted to the static host, with bounded redirects, size and time."""

    def __init__(self, deadline, timeout=30):
        self._deadline = deadline
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_BoundedRedirects)

    def _transfer(self, url, sink, max_bytes):
        check_url(url)
        self._deadline.check()
        request = urllib.request.Request(url, headers={"User-Agent": "flathub-qvac-beta-updater"})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                digest, size = copy_limited(response, sink, max_bytes, self._deadline)
                declared = response.headers.get("Content-Length", "")
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                raise NotPublished(url) from error
            raise UpdateError(f"GET {url}: HTTP {error.code}") from error
        except (OSError, http.client.HTTPException) as error:
            raise UpdateError(f"GET {url}: {type(error).__name__}") from error
        if declared.isdigit() and int(declared) != size:
            raise UpdateError(f"GET {url}: truncated response")
        return digest

    def get(self, url, max_bytes):
        sink = io.BytesIO()
        self._transfer(url, sink, max_bytes)
        return sink.getvalue()

    def download(self, url, dest, max_bytes):
        with open(dest, "wb") as sink:
            return self._transfer(url, sink, max_bytes)


# --- publication verification ---


def decode(data, what):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UpdateError(f"{what} is not UTF-8") from error


def verify_metadata(fetcher, version, arch):
    """Return the published sha512 of one arch after checking .sha512 and snippet agree."""
    name = archive_name(arch)
    url = publication_url(version, name)
    checksum = decode(fetcher.get(url + ".sha512", MAX_METADATA), f"{arch} .sha512")
    match = re.fullmatch(rf"([0-9a-f]{{128}})  {re.escape(name)}\n", checksum)
    if not match:
        raise UpdateError(f"{arch}: {name}.sha512 is not '<sha512>  {name}'")
    digest = match.group(1)
    snippet = decode(fetcher.get(publication_url(version, snippet_name(arch)), MAX_METADATA), f"{arch} snippet")
    if snippet != canonical_snippet(url, digest):
        raise UpdateError(f"{arch}: source snippet does not match its .sha512 and the staging layout")
    return digest


class _Capped:
    """Read-only wrapper that bounds decompressed bytes and time."""

    def __init__(self, source, max_bytes, deadline):
        self._source = source
        self._left = max_bytes
        self._deadline = deadline

    def read(self, size=-1):
        self._deadline.check()
        data = self._source.read(size)
        self._left -= len(data)
        if self._left < 0:
            raise UpdateError("archive expands beyond the allowed size")
        return data


def missing_members(path, app_id, max_unpacked, deadline):
    """List archive entry names only (nothing is extracted) and return required ones not found."""
    needed = set(required_members(app_id))
    has_main = False
    count = 0
    try:
        with open(path, "rb") as raw, gzip.GzipFile(fileobj=raw) as unpacked:
            with tarfile.open(fileobj=_Capped(unpacked, max_unpacked, deadline), mode="r|") as archive:
                for member in archive:
                    count += 1
                    if count > MAX_MEMBERS:
                        raise UpdateError("archive has too many entries")
                    name = member.name
                    while name.startswith("./"):
                        name = name[2:]
                    name = name.rstrip("/")
                    needed.discard(name)
                    has_main = has_main or name == "main" or name.startswith("main/")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as error:
        raise UpdateError(f"archive is not a readable tar.gz ({type(error).__name__})") from error
    return sorted(needed) + ([] if has_main else ["main/"])


def verify_publication(fetcher, version, app_id, deadline, max_archive_bytes):
    """Return the verified x64 sha512, raising NotPublished for any missing file."""
    digests = {arch: verify_metadata(fetcher, version, arch) for arch in ARCHES}
    expected = digests[MANIFEST_ARCH]
    url = publication_url(version, archive_name(MANIFEST_ARCH))
    with tempfile.TemporaryDirectory(prefix="beta-updater-") as scratch:
        path = os.path.join(scratch, archive_name(MANIFEST_ARCH))
        actual = fetcher.download(url, path, max_archive_bytes)
        if actual != expected:
            raise UpdateError(f"{MANIFEST_ARCH}: downloaded archive sha512 differs from the published sha512")
        missing = missing_members(path, app_id, 4 * max_archive_bytes, deadline)
    if missing:
        raise UpdateError("archive lacks files the manifest installs: " + ", ".join(missing))
    return expected


# --- manifest text handling ---


@dataclasses.dataclass(frozen=True)
class Source:
    url: str
    url_line: int
    sha512: str
    sha512_line: int
    checker_start: int
    checker_end: int


def split_manifest(text):
    if "\r" in text:
        raise UpdateError("manifest contains carriage returns")
    return text.split("\n")


def manifest_id(lines):
    found = [m.group(1) for m in (re.fullmatch(r"id: (\S+)\s*", line) for line in lines) if m]
    if len(found) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+", found[0]):
        raise UpdateError("manifest must declare exactly one top-level id")
    return found[0]


def locate_source(lines):
    """Find the url, sha512 and x-checker-data lines of the qvac-workbench archive source."""
    starts = [i for i, line in enumerate(lines) if line.rstrip() == "  - name: qvac-workbench"]
    if len(starts) != 1:
        raise UpdateError(f"expected exactly one qvac-workbench module, found {len(starts)}")
    module_end = next(
        (i for i in range(starts[0] + 1, len(lines)) if re.match(r"  - |\S", lines[i])), len(lines)
    )
    items = [i for i in range(starts[0], module_end) if lines[i].rstrip() == "      - type: archive"]
    if len(items) != 1:
        raise UpdateError(f"expected exactly one archive source in qvac-workbench, found {len(items)}")
    start = items[0]
    end = next(
        (i for i in range(start + 1, module_end) if lines[i].strip() and not lines[i].startswith("        ")),
        module_end,
    )

    def one(pattern, label):
        found = [(i, m) for i in range(start + 1, end) if (m := re.fullmatch(pattern, lines[i]))]
        if len(found) != 1:
            raise UpdateError(f"expected exactly one {label} in the archive source, found {len(found)}")
        return found[0]

    url_line, url = one(r"        url: (\S+)\s*", "url")
    sha_line, sha = one(r"        sha512: ([0-9a-f]{128})\s*", "sha512")
    checker_line, _ = one(r"        x-checker-data:\s*", "x-checker-data")
    checker_end = checker_line + 1
    while checker_end < end and lines[checker_end].startswith("         "):
        checker_end += 1
    return Source(url.group(1), url_line, sha.group(1), sha_line, checker_line, checker_end)


def staged_version(text):
    """Newest staging version referenced by an archive url in text, or None."""
    found = [
        m.group(1)
        for m in (re.fullmatch(rf"\s*url: {STAGING_URL}\s*", line) for line in text.split("\n"))
        if m
    ]
    return max(found, key=version_key) if found else None


def substitute(text, version, digest):
    """Replace only the archive url, sha512 and x-checker-data; verify by re-reading."""
    lines = split_manifest(text)
    source = locate_source(lines)
    url = publication_url(version, archive_name(MANIFEST_ARCH))
    lines[source.url_line] = f"        url: {url}"
    lines[source.sha512_line] = f"        sha512: {digest}"
    lines[source.checker_start : source.checker_end] = CHECKER
    result = "\n".join(lines)
    again = locate_source(split_manifest(result))
    if (again.url, again.sha512) != (url, digest):
        raise UpdateError("manifest substitution did not round-trip")
    return result


def check_manifest_filenames(text, app_id):
    """The manifest must install the id-named files the archive ships (guards app-ID drift)."""
    for name in required_members(app_id):
        if not re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text):
            raise UpdateError(f"manifest never references {name}: app id and packaged filenames disagree")


def pr_body(version, digest, previous, bootstrap, master_rev):
    url = publication_url(version, archive_name(MANIFEST_ARCH))
    before = f"`{previous}`" if previous else "none (bootstrap)"
    if bootstrap:
        scope = "This manifest was rebuilt from the human-authored production manifest"
        scope += f" on `master` ({master_rev})" if master_rev else " on `master`"
        scope += "; only the archive `url`, `sha512` and `x-checker-data` were replaced."
    else:
        scope = "Only the archive `url`, `sha512` and `x-checker-data` of the existing beta manifest were replaced."
    return "\n".join(
        [
            f"Update the QV.AC beta Flatpak to `{version}`.",
            "",
            f"- Source: {url}",
            f"- sha512: `{digest}`",
            f"- Previous beta version: {before}",
            "",
            scope,
            "",
            "Verified by the updater before this PR was opened:",
            "",
            "- the published `.sha512` and source snippet agree for x64 and arm64 and match the staging layout;",
            "- the x64 archive was downloaded and re-hashed (matches);",
            "- the archive lists the id-named files and `run.sh` the manifest installs (names only, nothing extracted or executed).",
            "",
            "Human review and merge required. The updater never merges and never enables auto-merge.",
            "Updater PRs are never closed automatically: close any older, superseded one by hand. See `.github/beta-updater/README.md` on `master`.",
            "",
        ]
    )


# --- orchestration ---


def read_regular(path, limit, required=True):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise UpdateError(f"missing file: {path}") from None
        return None
    if not stat.S_ISREG(info.st_mode):
        raise UpdateError(f"{path} is not a regular file")
    if info.st_size > limit:
        raise UpdateError(f"{path} is larger than {limit} bytes")
    with open(path, "rb") as handle:
        return decode(handle.read(limit + 1), path)


def propose(
    master_path,
    beta_dir,
    fetcher,
    deadline,
    *,
    proposed=frozenset(),
    max_archive_bytes=DEFAULT_MAX_ARCHIVE,
    master_rev=None,
):
    master = read_regular(master_path, MAX_MANIFEST)
    master_lines = split_manifest(master)
    locate_source(master_lines)
    manifest_id(master_lines)

    target = os.path.join(beta_dir, os.path.basename(master_path))
    beta = read_regular(target, MAX_MANIFEST, required=False)
    previous = staged_version(beta) if beta is not None else None
    bootstrap = previous is None
    base = master if bootstrap else beta

    try:
        listing = decode(fetcher.get(ROOT_URL, MAX_LISTING), "root listing")
    except NotPublished:
        raise UpdateError(f"root listing {ROOT_URL} is not available") from None
    candidates = set(LISTING_RE.findall(listing))
    if not candidates:
        return Outcome("no-publication", previous=previous)
    version = max(candidates, key=version_key)
    if previous is not None and version_key(version) <= version_key(previous):
        return Outcome("duplicate" if version == previous else "older", version, previous)
    if version in proposed:
        return Outcome("proposed", version, previous)

    base_id = manifest_id(split_manifest(base))
    check_manifest_filenames(base, base_id)
    try:
        digest = verify_publication(fetcher, version, base_id, deadline, max_archive_bytes)
    except NotPublished as missing:
        return Outcome("incomplete", version, previous, detail=f"missing {missing}")
    return Outcome(
        "bootstrap" if bootstrap else "updated",
        version,
        previous,
        manifest=substitute(base, version, digest),
        manifest_path=target,
        body=pr_body(version, digest, previous, bootstrap, master_rev),
    )


def read_proposed(path):
    if not path:
        return frozenset()
    text = read_regular(path, MAX_PROPOSED)
    return frozenset(line.strip() for line in text.split("\n") if re.fullmatch(VERSION, line.strip()))


# --- existing pull requests and update branches ---


def parse_pulls(text):
    try:
        pulls = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise UpdateError("pull request list is not valid JSON") from error
    if not isinstance(pulls, list):
        raise UpdateError("pull request list is not a JSON array")
    return pulls


def _field(value, *path):
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def trusted_proposals(pulls, repo):
    """Versions that already have an updater pull request opened by this repository's Actions bot.

    `pulls` are GitHub REST pull request objects. One counts, in any state, only when its head
    branch lives in `repo` itself (so never a fork, including a deleted one), it targets beta and
    its author is github-actions[bot]. A fork, or a human reusing the branch prefix, can neither
    suppress an update nor be mistaken for one.
    """
    versions = set()
    for pull in pulls:
        ref = _field(pull, "head", "ref")
        head_repo = _field(pull, "head", "repo", "full_name")
        match = BRANCH_RE.fullmatch(ref) if isinstance(ref, str) else None
        if (
            match
            and _field(pull, "base", "ref") == "beta"
            and isinstance(head_repo, str)
            and head_repo.lower() == repo.lower()
            and _field(pull, "user", "login") == BOT_LOGIN
            and _field(pull, "user", "type") == "Bot"
            and _field(pull, "user", "id") == BOT_ID
        ):
            versions.add(match.group(1))
    return versions


def verified_head_proposals(pulls, repo, branch):
    """Versions with a pull request on exactly `branch` in `repo` into beta, whoever opened it.

    Narrow by construction: `branch` must be `beta-update/<strict version>` and the pull request's
    head must be that very branch in `repo` itself (`head.repo.full_name`, so a fork with the same
    branch name never counts), into beta, in any state. The author is deliberately ignored, and
    that is only sound because it is called after `plan-branch` has verified the branch tip: by then
    the branch is exactly the verified update on the current beta head, so a pull request on it, by
    a maintainer after a failed bot run or closed to decline it, proposes or declines that very
    content and must neither be duplicated nor re-proposed. The bulk filter, `trusted_proposals`,
    stays strict because there nothing has been verified yet.
    """
    match = BRANCH_RE.fullmatch(branch)
    if not match:
        raise UpdateError(f"not an update branch name: {clean(branch, 80)}")
    for pull in pulls:
        head_repo = _field(pull, "head", "repo", "full_name")
        if (
            _field(pull, "head", "ref") == branch
            and _field(pull, "base", "ref") == "beta"
            and isinstance(head_repo, str)
            and head_repo.lower() == repo.lower()
        ):
            return {match.group(1)}
    return set()


def check_branch(branch, base, tree, tip, tip_line, tip_tree):
    """Allow reusing an existing remote update branch only if it is exactly the verified update.

    That means a single commit on the current beta head (`base`) whose tree is the tree the
    verified manifest produces (`tree`), so the PR body and patch describe the actual branch.
    Anything else fails with repair steps; nothing is ever force-pushed or rewritten.
    `tip_line` is `git rev-list --parents -n 1 <tip>`.
    """
    if not BRANCH_RE.fullmatch(branch):
        raise UpdateError(f"not an update branch name: {clean(branch, 80)}")
    words = tip_line.split()
    if not words or words[0] != tip or not all(re.fullmatch(SHA, w) for w in [base, tree, tip_tree, *words]):
        raise UpdateError("unexpected git output while inspecting the existing update branch")
    problems = []
    if words[1:] != [base]:
        problems.append(f"is not exactly one commit on the current beta head {base[:12]}")
    if tip_tree != tree:
        problems.append("does not contain exactly the verified update")
    if problems:
        raise UpdateError(
            f"remote branch {branch} already exists but {' and '.join(problems)}; refusing to push over it. "
            f"Repair: inspect it, delete it with 'git push origin --delete {branch}', then re-run"
        )
    return "reuse"


def run_git(beta_dir, *args):
    try:
        result = subprocess.run(
            ["git", "-C", beta_dir, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"git {args[0]} could not run: {type(error).__name__}") from error
    if result.returncode != 0:
        raise UpdateError(f"git {args[0]} failed: {clean(result.stderr.strip(), 200)}")
    return result.stdout.strip()


def plan_branch(beta_dir, manifest, branch, git=None):
    """Return 'create' when the update branch does not exist yet, 'reuse' when it already is
    exactly the verified update (a retry after a crash), and raise otherwise.

    Stages `manifest` in the beta checkout, so the expected tree is the one the commit gets.
    """
    git = git or (lambda *args: run_git(beta_dir, *args))
    if not BRANCH_RE.fullmatch(branch) or not re.fullmatch(r"[A-Za-z0-9_.-]+", manifest):
        raise UpdateError("invalid branch or manifest name")
    git("add", "--", manifest)
    base = git("rev-parse", "HEAD")
    tree = git("write-tree")
    ref = f"refs/heads/{branch}"
    tips = [sha for sha, _, name in (line.partition("\t") for line in git("ls-remote", "origin", ref).splitlines()) if name == ref]
    if not tips:
        return "create"
    git("fetch", "--no-tags", "--depth=2", "origin", ref)
    tip = git("rev-parse", "FETCH_HEAD")
    return check_branch(branch, base, tree, tip, git("rev-list", "--parents", "-n", "1", tip), git("rev-parse", f"{tip}^{{tree}}"))


def write_atomic(path, text):
    """Write via a temp file and rename, so a symlink at path is replaced, not followed."""
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".update-beta-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def write_outputs(path, outcome):
    values = {
        "changed": "true" if outcome.changed else "false",
        "reason": outcome.reason,
        "version": outcome.version or "",
        "branch": BRANCH_PREFIX + outcome.version if outcome.changed else "",
    }
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def summarize(outcome):
    parts = [f"beta-updater: {outcome.reason} - {REASONS[outcome.reason]}"]
    for label in ("version", "previous"):
        if getattr(outcome, label):
            parts.append(f"{label}={getattr(outcome, label)}")
    if outcome.detail:
        parts.append(outcome.detail)
    return clean(" ".join(parts))


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    update = commands.add_parser("update", help="verify the newest staging publication and rewrite the beta manifest")
    update.add_argument("--master", required=True, help="trusted production manifest (read only)")
    update.add_argument("--beta-dir", required=True, help="beta checkout; only the manifest file in it is written")
    update.add_argument("--proposed", help="file listing versions that already have a trusted updater PR (see proposals)")
    update.add_argument("--pr-body", help="where to write the pull request body")
    update.add_argument("--master-rev", help="40-hex commit of the trusted master checkout")
    update.add_argument("--github-output", help="GITHUB_OUTPUT file to append changed/reason/version/branch to")
    update.add_argument("--summary", help="file (GITHUB_STEP_SUMMARY) to append a one-line result to")
    update.add_argument("--max-archive-bytes", type=int, default=DEFAULT_MAX_ARCHIVE)
    update.add_argument("--time-budget", type=int, default=DEFAULT_TIME_BUDGET, help="seconds for all network and unpacking")
    update.add_argument("--dry-run", action="store_true", help="verify and report, write nothing but outputs/summary")

    proposals = commands.add_parser("proposals", help="print versions with a PR from this repository's Actions bot")
    proposals.add_argument("--pulls", required=True, help="JSON array of REST pull request objects (gh api .../pulls)")
    proposals.add_argument("--repo", required=True, help="OWNER/NAME of this repository")
    proposals.add_argument(
        "--verified-head",
        metavar="BRANCH",
        help="only after plan-branch verified BRANCH: print its version if a PR exists on that exact same-repo head, whoever opened it",
    )

    plan = commands.add_parser("plan-branch", help="print create or reuse for the update branch, or fail with repair steps")
    plan.add_argument("--beta-dir", required=True, help="beta checkout (its index is staged with the manifest)")
    plan.add_argument("--manifest", required=True, help="manifest file name inside the beta checkout")
    plan.add_argument("--branch", required=True, help="beta-update/<version>")

    args = parser.parse_args(argv)
    if args.command == "update":
        if args.max_archive_bytes <= 0 or args.time_budget <= 0:
            parser.error("--max-archive-bytes and --time-budget must be positive")
        if args.master_rev and not re.fullmatch(r"[0-9a-f]{40}", args.master_rev):
            parser.error("--master-rev must be a 40-hex commit")
    elif args.command == "proposals" and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be OWNER/NAME")
    return args


def cmd_update(args, fetcher):
    deadline = Deadline(args.time_budget)
    outcome = propose(
        args.master,
        args.beta_dir,
        fetcher or HttpFetcher(deadline),
        deadline,
        proposed=read_proposed(args.proposed),
        max_archive_bytes=args.max_archive_bytes,
        master_rev=args.master_rev,
    )
    if outcome.changed and not args.dry_run:
        write_atomic(outcome.manifest_path, outcome.manifest)
        if args.pr_body:
            write_atomic(args.pr_body, outcome.body)
    line = summarize(outcome)
    print(line)
    if args.github_output:
        write_outputs(args.github_output, outcome)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Beta updater\n\n{line}\n")
    return 0


def main(argv=None, fetcher=None):
    args = parse_args(argv)
    try:
        if args.command == "proposals":
            pulls = parse_pulls(read_regular(args.pulls, MAX_PULLS))
            if args.verified_head:
                found = verified_head_proposals(pulls, args.repo, args.verified_head)
            else:
                found = trusted_proposals(pulls, args.repo)
            for version in sorted(found, key=version_key):
                print(version)
            return 0
        if args.command == "plan-branch":
            print(plan_branch(args.beta_dir, args.manifest, args.branch))
            return 0
        return cmd_update(args, fetcher)
    except UpdateError as error:
        print(clean(f"error: {error}", 600), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
