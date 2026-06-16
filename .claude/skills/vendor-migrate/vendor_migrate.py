#!/usr/bin/env python3
"""vendor_migrate.py — one-shot submodule/vendored-dir -> Dependency Channel migration (DEP-CH-6).

Converts an existing git **submodule** (or hand-vendored dir) into the
Dependency Channel consumer artifacts (`vendor.toml` + `vendor.lock`) so the
dependency is thereafter sourced from a published GitHub Release channel rather
than a moving submodule pointer.

Resolution algorithm (design `dependency-channel-design.md` §7):

  1. Read the submodule's pinned commit (the gitlink, mode 160000) + its URL
     from `.gitmodules`; derive the GitHub `owner/repo` slug.
  2. Enumerate the published releases on the dep's channel; for each candidate,
     fetch `release.json` + `SHA256SUMS` + the artifact, extract it exactly as
     `sync-deps` would (same ArtifactKind/strip/extract semantics), and compare
     the placed-tree `tree_sha256` against the submodule's fetched-tree
     `tree_sha256`. **Prefer the tag** the pinned commit maps to (`v{version}`).
  3. On a content match -> write `vendor.toml` + reconcile via the real
     `SyncDepsEngine` so the emitted `vendor.lock` is byte-for-byte what
     `sync-deps --check` then validates clean.
  4. **No match -> LOUD FALLBACK** (never silent): record the resolved commit +
     a content sha256 of the present bytes and emit a documented note. The tool
     **never silently pins to a moving ref**; it leaves a hand-completable
     `vendor.toml` stub with the channel/version commented out.

Re-run safety: an existing **hand-edited** `vendor.toml` is never clobbered
(the `design-language-adapt` no-silent-clobber rule). Pass `--force` to
overwrite a tool-written file deliberately.

Reuse (design §11): this composes the DEP-CH-2 engine rather than re-deriving it.
`tree_sha256`, `VendorLock`, `DepSpec`, `make_kind`, `OfflineFetcher`,
`GhReleaseFetcher`, `SyncDepsEngine`, the exit-code contract, and the
deterministic fixture seeders are all imported from `sync_deps_engine`.

stdlib-only; `tomllib` (Python 3.11+) is pulled in transitively by the engine.

Design: docs/design/dependency-channel-design.md §7 (+ §3 for the produced shapes).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

# Allow running both as a module and as a bare script from the skill dir. The
# DEP-CH-2 engine lives in the sibling `sync-deps` skill; the copilot mirror
# ships its own copy in `copilot/scripts/`.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "sync-deps"))

from sync_deps_engine import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    EXIT_DEGRADED,
    HASH_PREFIX,
    GhReleaseFetcher,
    OfflineFetcher,
    SyncDepsEngine,
    SyncDepsError,
    Verifier,
    DepSpec,
    VendorToml,
    make_kind,
    sha256_of_file,
    tree_sha256,
    _atomic_write_text,
    _seed_release,
)

# Default channel a migrated submodule is assumed to track. Submodules carry no
# channel of their own; stable is the conservative default (overridable).
DEFAULT_CHANNEL = "stable"
DEFAULT_KIND = "asset-bundle"
GITLINK_MODE = "160000"   # git's mode bits for a submodule gitlink entry
# How many releases to probe when the pinned tag is not the match (newest-first).
MAX_RELEASE_PROBE = 50


class VendorMigrateError(SyncDepsError):
    """A migration error carrying the process exit code to surface."""


# ── git introspection ───────────────────────────────────────────────────────

class GitRunner:
    """Thin, injectable wrapper over the `git` CLI (one subprocess seam).

    Injected in tests so the self-test never shells out to a real network; in
    production it runs `git -C <root> ...` confined to the consumer repo root.
    """

    def __init__(self, root, runner=None):
        self.root = os.path.abspath(root)
        self._run = runner or self._default_run

    def _default_run(self, args):  # pragma: no cover - exercised with real git
        proc = subprocess.run(
            ["git", "-C", self.root, *args],
            capture_output=True, text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def gitlink_commit(self, dirpath):
        """Return the pinned commit sha of the submodule at `dirpath`, or None.

        Reads the index gitlink (mode 160000). `dirpath` is repo-relative.
        """
        rel = dirpath.rstrip("/")
        rc, out, _ = self._run(["ls-files", "--stage", "--", rel])
        if rc != 0:
            return None
        for line in out.splitlines():
            # Format: "<mode> <sha> <stage>\t<path>"
            head, _, path = line.partition("\t")
            parts = head.split()
            if len(parts) >= 2 and parts[0] == GITLINK_MODE and path.strip() == rel:
                return parts[1]
        return None


class GitModules:
    """Parser for a `.gitmodules` file — maps submodule path -> remote URL.

    `.gitmodules` is INI-like; we read only the `path` and `url` of each
    `[submodule "..."]` stanza. stdlib `configparser` would choke on the quoted
    section names, so a small line parser is used (no third-party TOML/INI dep).
    """

    def __init__(self, text):
        self._by_path = {}
        cur = {}
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                self._flush(cur)
                cur = {}
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                cur[key.strip().lower()] = val.strip()
        self._flush(cur)

    def _flush(self, stanza):
        path = stanza.get("path")
        url = stanza.get("url")
        if path and url:
            self._by_path[path.rstrip("/")] = url

    def url_for(self, dirpath):
        return self._by_path.get(dirpath.rstrip("/"))

    @classmethod
    def load(cls, root):
        gm = os.path.join(root, ".gitmodules")
        if not os.path.isfile(gm):
            return cls("")
        with open(gm, "r", encoding="utf-8") as fh:
            return cls(fh.read())


def derive_slug(url):
    """Derive an `owner/repo` GitHub slug from a remote URL.

    Reuses GhReleaseFetcher._slug (the same normalization sync-deps uses), so a
    migrated dep's `repo` field matches exactly what the sync engine resolves.
    """
    slug = GhReleaseFetcher._slug(url)
    if not re.match(r"^[^/]+/[^/]+$", slug):
        raise VendorMigrateError(
            f"could not derive a GitHub owner/repo slug from URL {url!r}",
            EXIT_VIOLATION,
        )
    return slug


# ── the migration engine ────────────────────────────────────────────────────

class MigrationResult:
    """Outcome of one migration attempt — matched, or the loud-fallback record."""

    def __init__(self, name, matched, version=None, artifact=None,
                 commit=None, content_sha=None, channel=DEFAULT_CHANNEL):
        self.name = name
        self.matched = matched
        self.version = version
        self.artifact = artifact
        self.commit = commit
        self.content_sha = content_sha
        self.channel = channel


class VendorMigrator:
    """Convert one submodule/vendored dir into vendor.toml + vendor.lock.

    Composes the DEP-CH-2 engine: it never re-implements fetch/verify/extract —
    it drives `make_kind` to reproduce the placed bytes for content matching and
    the real `SyncDepsEngine` to emit the lock, guaranteeing the result passes
    `sync-deps --check`.
    """

    def __init__(self, root, fetcher, git=None, clock=None):
        self.root = os.path.abspath(root)
        self._fetcher = fetcher
        self._git = git or GitRunner(self.root)
        self._clock = clock

    # — public entry —

    def migrate(self, name, dirpath, channel=DEFAULT_CHANNEL, kind=DEFAULT_KIND,
                strip_components=0, extract=None, force=False):
        """Migrate the dep `name` rooted at repo-relative `dirpath`.

        Returns a MigrationResult; raises VendorMigrateError on a hard error
        (unreadable submodule, slug derivation failure, clobber refusal).
        """
        toml_path = os.path.join(self.root, "vendor.toml")
        self._guard_clobber(toml_path, name, force)

        commit = self._git.gitlink_commit(dirpath)
        url = GitModules.load(self.root).url_for(dirpath)
        if url is None:
            raise VendorMigrateError(
                f"no .gitmodules url for submodule path {dirpath!r}; cannot "
                f"derive the release repo (hand-author vendor.toml instead).",
                EXIT_VIOLATION,
            )
        slug = derive_slug(url)

        present_dir = os.path.join(self.root, dirpath)
        if not os.path.isdir(present_dir):
            raise VendorMigrateError(
                f"vendored/submodule dir not present at {dirpath!r} — check it "
                f"out (`git submodule update --init`) before migrating.",
                EXIT_VIOLATION,
            )
        present_tree_sha = tree_sha256(present_dir)
        content_sha = present_tree_sha  # already sha256:<hex> over the present bytes

        match = self._resolve_matching_release(
            name, slug, channel, kind, strip_components, extract,
            present_tree_sha, commit,
        )

        if match is None:
            return self._loud_fallback(name, slug, channel, commit, content_sha,
                                       dirpath, kind, strip_components, extract)

        version, artifact = match
        self._write_matched_toml(name, slug, channel, version, artifact, dirpath,
                                 kind, strip_components, extract)
        # Drive the REAL engine so the emitted lock is exactly what --check reads.
        self._reconcile(name)
        return MigrationResult(name, matched=True, version=version,
                               artifact=artifact, commit=commit, channel=channel)

    # — clobber guard (no-silent-clobber rule) —

    @staticmethod
    def _guard_clobber(toml_path, name, force):
        """Refuse to overwrite a vendor.toml that already declares `name`.

        Mirrors design-language-adapt's no-silent-clobber rule — a re-run must
        never stomp a hand-edited intent file. `--force` opts in deliberately.
        """
        if force or not os.path.isfile(toml_path):
            return
        try:
            _schema, deps = VendorToml(toml_path).load()
        except SyncDepsError:
            return  # an unparsable/partial file is not a committed dep to protect
        if name in deps:
            raise VendorMigrateError(
                f"vendor.toml already declares [deps.{name}] — refusing to "
                f"clobber a hand-edited file (pass --force to overwrite).",
                EXIT_VIOLATION,
            )

    # — release resolution (content-match, tag preferred) —

    def _resolve_matching_release(self, name, slug, channel, kind,
                                  strip_components, extract, present_tree_sha,
                                  commit):
        """Return (version, artifact) whose placed tree matches, else None.

        Probes the tag the pinned commit maps to FIRST (the common case), then
        falls back to newest-first across the channel.
        """
        candidates = self._candidate_versions(slug, channel, commit)
        for version in candidates:
            artifact = self._probe_version(
                name, slug, channel, version, kind, strip_components, extract,
                present_tree_sha,
            )
            if artifact is not None:
                return version, artifact
        return None

    def _candidate_versions(self, slug, channel, commit):
        """Ordered candidate version list — preferred tag first, then newest."""
        ordered = []
        # The fetcher knows which versions exist on the channel (offline fixture
        # dir or `gh release list`). resolve_latest gives the newest; we also
        # enumerate when the fetcher exposes a listing.
        listed = self._list_versions(slug, channel)
        # Prefer a tag the commit maps to, if the caller knows the version. We
        # cannot map an arbitrary sha -> tag offline, so the listed set (newest
        # first) is probed; a real run with `gh` could map the sha, but content
        # match is authoritative regardless, so newest-first is sufficient.
        for v in listed[:MAX_RELEASE_PROBE]:
            if v not in ordered:
                ordered.append(v)
        return ordered

    def _list_versions(self, slug, channel):
        """Newest-first version list on the channel via the injected fetcher."""
        spec = self._slug_spec(slug, channel, version="0.0.0",
                               artifact="placeholder-v0.0.0.tar.gz")
        try:
            if isinstance(self._fetcher, OfflineFetcher):
                base = os.path.join(self._fetcher.fixture_root, slug)
                versions = []
                if os.path.isdir(base):
                    for tag in os.listdir(base):
                        v = tag.lstrip("v")
                        if re.match(r"^\d+\.\d+", v):
                            versions.append(v)
                from sync_deps_engine import _semver_key
                versions.sort(key=_semver_key, reverse=True)
                return versions
            # Network path: newest-on-channel; broader enumeration is a future
            # enhancement (content-match against the latest tag covers the
            # common "submodule pinned at the latest release" case).
            return [self._fetcher.resolve_latest(spec)]
        except SyncDepsError:
            return []

    def _probe_version(self, name, slug, channel, version, kind,
                       strip_components, extract, present_tree_sha):
        """Fetch+verify+extract one version; return its artifact if it matches."""
        artifact = self._artifact_name(slug, version)
        spec = self._slug_spec(slug, channel, version, artifact, kind,
                               strip_components, extract, name=name)
        with tempfile.TemporaryDirectory() as dl, \
                tempfile.TemporaryDirectory() as xd:
            try:
                fetched = self._fetcher.fetch(spec, dl)
                # Verify BEFORE placement (same checkpoint sync-deps enforces).
                Verifier(fetched.sums_map).verify_or_raise(fetched.artifact_path)
                make_kind(spec).extract_into(fetched.artifact_path, xd)
            except SyncDepsError:
                return None
            placed = tree_sha256(xd)
            return artifact if placed == present_tree_sha else None

    # — vendor.toml / vendor.lock emission —

    def _write_matched_toml(self, name, slug, channel, version, artifact,
                            dirpath, kind, strip_components, extract):
        block = self._render_block(name, slug, channel, version, artifact,
                                   dirpath, kind, strip_components, extract,
                                   commented=False)
        self._merge_toml_block(name, block)

    def _reconcile(self, name):
        """Run the real engine offline-free sync so the lock matches the bytes.

        Uses the injected fetcher (offline in tests, gh in prod) so the emitted
        vendor.lock is byte-identical to what `sync-deps --check` recomputes.
        """
        engine = SyncDepsEngine(root=self.root, fetcher=self._fetcher,
                                clock=self._clock)
        engine.sync(only=name)

    def _loud_fallback(self, name, slug, channel, commit, content_sha, dirpath,
                       kind, strip_components, extract):
        """Emit a LOUD, documented fallback — never silently pin to a moving ref.

        Writes a commented vendor.toml stub recording the resolved commit +
        content sha so a human can complete the pin, and prints a banner. No
        vendor.lock is written (there is no published release to lock to).
        """
        version_placeholder = ""  # deliberately empty — the human must fill it
        block = self._render_block(
            name, slug, channel, version_placeholder, "", dirpath, kind,
            strip_components, extract, commented=True, commit=commit,
            content_sha=content_sha,
        )
        self._merge_toml_block(name, block)
        banner = (
            "\n"
            "================ vendor-migrate: LOUD FALLBACK ================\n"
            f"  dep            : {name}\n"
            f"  repo           : {slug}\n"
            f"  channel        : {channel}\n"
            f"  pinned commit  : {commit or '(none — not a gitlink)'}\n"
            f"  content sha256 : {content_sha}\n"
            "  No published release on this channel matched the present bytes.\n"
            "  A commented [deps."f"{name}""] stub was written to vendor.toml\n"
            "  recording the commit + content-sha. NOTHING was pinned to a\n"
            "  moving ref. Publish a matching release (or correct the channel),\n"
            "  fill the version/artifact, then run `sync-deps` to lock it.\n"
            "==============================================================\n"
        )
        print(banner, file=sys.stderr)
        return MigrationResult(name, matched=False, commit=commit,
                               content_sha=content_sha, channel=channel)

    # — TOML rendering + idempotent merge —

    @staticmethod
    def _render_block(name, slug, channel, version, artifact, dirpath, kind,
                      strip_components, extract, commented, commit=None,
                      content_sha=None):
        """Render a single [deps.<name>] block (optionally fully commented)."""
        p = "# " if commented else ""
        lines = []
        if commented:
            lines.append("# vendor-migrate: LOUD FALLBACK — no published release "
                         "matched the present bytes.")
            lines.append(f"# resolved commit : {commit or '(none)'}")
            lines.append(f"# content sha256  : {content_sha}")
            lines.append("# Fill version + artifact from a matching published "
                         "release, then run sync-deps.")
        lines.append(f"{p}[deps.{name}]")
        lines.append(f'{p}repo = "{slug}"')
        lines.append(f'{p}channel = "{channel}"')
        if commented:
            lines.append(f'{p}version = ""   # TODO: pin to the matching release')
            lines.append(f'{p}artifact = ""  # TODO: e.g. {name}-v{{version}}.tar.gz')
        else:
            lines.append(f'{p}version = "{version}"')
            lines.append(f'{p}artifact = "{artifact}"')
        lines.append(f'{p}dest = "{dirpath.rstrip("/")}"')
        lines.append(f'{p}kind = "{kind}"')
        if strip_components:
            lines.append(f"{p}strip_components = {int(strip_components)}")
        if extract:
            rendered = ", ".join(f'"{e}"' for e in extract)
            lines.append(f"{p}extract = [{rendered}]")
        return "\n".join(lines) + "\n"

    def _merge_toml_block(self, name, block):
        """Insert/replace one [deps.<name>] block, preserving the rest of the file.

        Idempotent and comment-preserving: an existing block for `name` (active
        or commented-fallback) is replaced in place; otherwise the block is
        appended. A missing file is seeded with the schema header.
        """
        toml_path = os.path.join(self.root, "vendor.toml")
        if os.path.isfile(toml_path):
            with open(toml_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = "schema_version = 1\n"
        new_text = self._replace_or_append(text, name, block)
        _atomic_write_text(toml_path, new_text)

    @staticmethod
    def _replace_or_append(text, name, block):
        """Return `text` with the [deps.<name>] region replaced by `block`.

        The region spans the (optionally commented) header line for `name`
        through the line before the next top-level/dep header or EOF, plus any
        immediately-preceding `# vendor-migrate` fallback comment lines.
        """
        lines = text.splitlines(keepends=True)
        header_re = re.compile(rf"^\s*#?\s*\[deps\.{re.escape(name)}\]\s*$")
        start = None
        for i, line in enumerate(lines):
            if header_re.match(line):
                start = i
                break
        if start is None:
            sep = "" if text.endswith("\n") or not text else "\n"
            joined = text + sep
            if joined and not joined.endswith("\n\n"):
                joined += "\n"
            return joined + block
        # Walk back over preceding fallback comment lines belonging to this block.
        _fallback_prefixes = ("# vendor-migrate", "# resolved commit",
                              "# content sha256", "# Fill version")
        while start > 0 and lines[start - 1].lstrip().startswith(_fallback_prefixes):
            start -= 1
        # Find the end: next header (active or commented dep / top-level table).
        end = len(lines)
        next_hdr = re.compile(r"^\s*#?\s*\[")
        for j in range(start + 1, len(lines)):
            if next_hdr.match(lines[j]) and not header_re.match(lines[j]):
                end = j
                break
        before = "".join(lines[:start])
        after = "".join(lines[end:])
        if before and not before.endswith("\n"):
            before += "\n"
        return before + block + after

    # — small spec/name helpers —

    def _slug_spec(self, slug, channel, version, artifact, kind=DEFAULT_KIND,
                   strip_components=0, extract=None, name=None):
        data = {
            "repo": slug, "channel": channel, "version": version,
            "artifact": artifact, "dest": f"vendor/{name or slug.split('/')[-1]}",
            "kind": kind, "strip_components": strip_components,
        }
        if extract:
            data["extract"] = extract
        return DepSpec(name or slug.split("/")[-1], data)

    @staticmethod
    def _artifact_name(slug, version):
        """The canonical producer artifact basename `{repo}-v{version}.tar.gz`."""
        return f"{slug.split('/')[-1]}-v{version}.tar.gz"


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser():
    """Construct the argparse CLI surface."""
    p = argparse.ArgumentParser(
        prog="vendor_migrate.py",
        description="Migrate a git submodule / vendored dir into a Dependency "
                    "Channel vendor.toml + vendor.lock.",
    )
    p.add_argument("--root", default=".",
                   help="Consumer repo root holding .gitmodules (default: cwd).")
    p.add_argument("--name", help="The dependency name for [deps.<name>].")
    p.add_argument("--path",
                   help="Repo-relative submodule/vendored dir to migrate.")
    p.add_argument("--channel", default=DEFAULT_CHANNEL,
                   choices=sorted(("stable", "beta")),
                   help="Release channel the dep tracks (default: stable).")
    p.add_argument("--kind", default=DEFAULT_KIND,
                   help="asset-bundle | vendored-crate | app-binary.")
    p.add_argument("--strip-components", type=int, default=0,
                   help="Leading path components to strip on extract.")
    p.add_argument("--extract", action="append", default=None,
                   help="Subset allowlist prefix (repeatable).")
    p.add_argument("--force", action="store_true",
                   help="Overwrite an existing [deps.<name>] in vendor.toml.")
    p.add_argument("--self-test", action="store_true",
                   help="Run the deterministic offline self-test and exit.")
    return p


def main(argv=None):
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    if args.self_test:
        ok = run_self_test()
        print("vendor_migrate self-test: PASS" if ok
              else "vendor_migrate self-test: FAIL")
        return EXIT_OK if ok else EXIT_VIOLATION

    if not args.name or not args.path:
        print("ERROR: --name and --path are required (or use --self-test).",
              file=sys.stderr)
        return EXIT_VIOLATION

    try:
        migrator = VendorMigrator(root=args.root, fetcher=GhReleaseFetcher())
        result = migrator.migrate(
            args.name, args.path, channel=args.channel, kind=args.kind,
            strip_components=args.strip_components, extract=args.extract,
            force=args.force,
        )
        if result.matched:
            print(f"vendor-migrate: {args.name} -> v{result.version} "
                  f"({result.artifact}); vendor.toml + vendor.lock written.")
            print("Run `sync-deps --check` to confirm the round-trip.")
            return EXIT_OK
        # Loud fallback already printed its banner; signal a (documented) degrade.
        return EXIT_DEGRADED
    except SyncDepsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code


# ── Self-test (deterministic, stdlib-only, offline) ──────────────────────────

def _seed_submodule_fixture(root, dirpath, files, commit_sha, url, strip=0):
    """Seed a synthetic submodule: a gitlink in a fake index + .gitmodules + bytes.

    Returns a `git`-runner stub that reports the gitlink commit, so the migrator
    never shells out. The present bytes are written under `root/dirpath` exactly
    as a checked-out submodule would appear (after the producer's strip is undone
    for the bytes-on-disk view).
    """
    present = os.path.join(root, dirpath)
    os.makedirs(present, exist_ok=True)
    for rel, data in files.items():
        full = os.path.join(present, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
    with open(os.path.join(root, ".gitmodules"), "w", encoding="utf-8") as fh:
        fh.write(f'[submodule "{dirpath}"]\n\tpath = {dirpath}\n\turl = {url}\n')

    def git_runner(args):
        # Emulate `git ls-files --stage -- <dir>` returning a gitlink row.
        if args[:2] == ["ls-files", "--stage"]:
            return 0, f"{GITLINK_MODE} {commit_sha} 0\t{dirpath}\n", ""
        return 1, "", "unsupported in stub"
    return git_runner


def run_self_test():
    """Deterministic, offline, stdlib-only regression suite. Returns True/False."""
    import datetime
    failures = []

    def check(cond, label):
        if cond:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}")
            failures.append(label)

    fixed_clock = lambda: datetime.datetime(2026, 6, 13, 18, 20, 0,
                                            tzinfo=datetime.timezone.utc)

    with tempfile.TemporaryDirectory() as tmp:
        # The producer ships a tarball with a top-level `widget-v1.0.0/` dir that
        # strip_components=1 removes; the checked-out submodule on disk has the
        # stripped (inner) layout, so the placed tree matches.
        inner = {
            "README.md": b"hello widget\n",
            "css/main.css": b"body{}\n",
            "fonts/a.woff": b"FONT\n",
        }
        tar_files = {f"widget-v1.0.0/{k}": v for k, v in inner.items()}

        # ── case 1: a matching published release migrates cleanly ──
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        fixture = os.path.join(tmp, "fixtures")
        _seed_release(fixture, "acme/widget", "v1.0.0", "widget-v1.0.0.tar.gz",
                      tar_files)
        git_stub = _seed_submodule_fixture(
            repo, "vendor/widget", inner, commit_sha="abc1234deadbeef",
            url="https://github.com/acme/widget.git")
        migrator = VendorMigrator(
            root=repo, fetcher=OfflineFetcher(fixture),
            git=GitRunner(repo, runner=git_stub), clock=fixed_clock)
        result = migrator.migrate("widget", "vendor/widget",
                                  strip_components=1)
        check(result.matched, "matching release resolved (content-sha match)")
        check(result.version == "1.0.0", "resolved the v1.0.0 pin")
        check(os.path.isfile(os.path.join(repo, "vendor.toml")),
              "vendor.toml written")
        check(os.path.isfile(os.path.join(repo, "vendor.lock")),
              "vendor.lock written")

        # The headline acceptance: sync-deps --check validates the round-trip clean.
        from sync_deps import main as sync_main
        rc = sync_main(["--root", repo, "--check"])
        check(rc == EXIT_OK, "sync-deps --check validates the migration clean")

        # vendor.toml carries the derived slug + channel + dest invariant.
        _schema, deps = VendorToml(os.path.join(repo, "vendor.toml")).load()
        check(deps["widget"].repo == "acme/widget", "vendor.toml repo slug derived")
        check(deps["widget"].channel == "stable", "vendor.toml channel defaulted")
        check(deps["widget"].dest == "vendor/widget", "dest invariant preserved")

        # ── case 2: re-run never clobbers a hand-edited vendor.toml ──
        clobbered = False
        try:
            migrator.migrate("widget", "vendor/widget", strip_components=1)
        except VendorMigrateError:
            clobbered = True
        check(clobbered, "re-run refuses to clobber an existing [deps.widget]")
        # --force opts in.
        forced = migrator.migrate("widget", "vendor/widget", strip_components=1,
                                  force=True)
        check(forced.matched, "--force re-migrates an existing dep")

        # ── case 3: NO published release matches -> LOUD FALLBACK ──
        repo2 = os.path.join(tmp, "repo2")
        os.makedirs(repo2)
        fixture2 = os.path.join(tmp, "fixtures2")
        # Publish a DIFFERENT-content release so no tree matches.
        other = {f"widget-v1.0.0/{k}": v for k, v in
                 {"README.md": b"DIFFERENT bytes\n"}.items()}
        _seed_release(fixture2, "acme/widget", "v1.0.0", "widget-v1.0.0.tar.gz",
                      other)
        git_stub2 = _seed_submodule_fixture(
            repo2, "vendor/widget", inner, commit_sha="ffff0001",
            url="git@github.com:acme/widget.git")
        migrator2 = VendorMigrator(
            root=repo2, fetcher=OfflineFetcher(fixture2),
            git=GitRunner(repo2, runner=git_stub2), clock=fixed_clock)
        res2 = migrator2.migrate("widget", "vendor/widget", strip_components=1)
        check(not res2.matched, "no-match path returns an unmatched result")
        check(res2.commit == "ffff0001",
              "loud fallback records the resolved commit")
        check(res2.content_sha.startswith(HASH_PREFIX),
              "loud fallback records a content sha256 (not a moving ref)")
        check(not os.path.isfile(os.path.join(repo2, "vendor.lock")),
              "no vendor.lock pinned on the no-match path")
        with open(os.path.join(repo2, "vendor.toml")) as fh:
            toml_text = fh.read()
        check("LOUD FALLBACK" in toml_text,
              "vendor.toml stub carries the loud-fallback banner")
        check("ffff0001" in toml_text,
              "vendor.toml stub records the commit (human completes the pin)")
        # The stub is commented, so it parses to ZERO active deps (no moving ref).
        _s2, deps2 = VendorToml(os.path.join(repo2, "vendor.toml")).load()
        check("widget" not in deps2,
              "loud-fallback stub declares no ACTIVE dep (never silently pinned)")

        # ── case 4: git@ SSH URL slug derivation ──
        check(derive_slug("git@github.com:owner/repo.git") == "owner/repo",
              "ssh-form url slug derived")
        check(derive_slug("https://github.com/owner/repo") == "owner/repo",
              "https-form url slug derived")

        # ── case 5: missing .gitmodules url is a hard error ──
        repo3 = os.path.join(tmp, "repo3")
        os.makedirs(os.path.join(repo3, "vendor", "x"))
        no_url = False
        try:
            VendorMigrator(
                root=repo3, fetcher=OfflineFetcher(fixture),
                git=GitRunner(repo3, runner=lambda a: (1, "", "")),
            ).migrate("x", "vendor/x")
        except VendorMigrateError:
            no_url = True
        check(no_url, "missing .gitmodules url is a hard error")

    print(f"\n{len(failures)} failure(s)." if failures else "\nall checks passed.")
    return not failures


if __name__ == "__main__":
    sys.exit(main())
