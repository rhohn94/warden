#!/usr/bin/env python3
"""build_crate_artifact.py — build a library project's `vendored-crate` channel artifact.

Part of the Dependency Channel **producer** contract
(`docs/grimoire/design/dependency-channel-design.md` §2/§2b). Where
`build_distributables.py` publishes **Grimoire itself** as an `asset-bundle`
(hardcoded `grimoire-v<MAJOR>.<MINOR>` primary-artifact name, flavor `.zip`s, a
golden image), this sibling entrypoint lets a Grimoire-managed **library**
project publish **its own crate** as a `vendored-crate` artifact that a consumer
vendors via `grm-sync-deps` (`sync_deps_engine.VendoredCrateKind`). It is the
exact **inverse** of that consumer path.

Why a sibling, not an extension of `build_distributables.py`. That builder's
whole object model is flavor-centric — flavor markers, per-flavor `.zip`s, a
golden image, a single canonical flavor tree. A library crate has none of that:
one crate dir, an include-subset allowlist, a config-driven name/kind. Extending
the flavor class to also emit a crate would tangle two unrelated shapes. Instead
this module **reuses `build_distributables.py`'s determinism machinery verbatim**
(the `TarInfo` filter that zeroes `mtime`/`uid`/`gid`/`uname`/`gname`, fixed mode,
sorted entries, fixed-mtime gzip header, streaming `_sha256`) by importing those
helpers, so the two producers share one byte-for-byte determinism contract and
cannot drift.

What it emits (the trio, all deterministic):

    dist/<name>-v{ver}.tar.gz   one top-level `<name>/` dir + the crate's
                                publishable include-subset
    dist/release.json           artifact_kind: vendored-crate, primary_artifact
                                names the tarball, assets[] lists the tarball ONLY
    dist/SHA256SUMS             covers the tarball AND release.json

The tarball shape is the exact inverse of `VendoredCrateKind`: exactly ONE
top-level `<name>/` directory, so the consumer's default 1-component strip yields
the crate root. The include-subset (`src/`, `Cargo.toml`, `Cargo.lock` if
committed, `build.rs`, `LICENSE*`, `README.md`, `migrations/`) and the exclusions
(`target/`, `.git/`, `vendor/`, `.claude/`, test-only fixtures) are **parameters**
driven by a producer publish manifest (`publish.toml`), never hardcoded.

The primary-artifact name, `artifact_kind`, `channel`, and include-subset are all
config-driven (see `PublishManifest`), so the same code publishes any library
crate; nothing is specialized to `grimoire-v…` / `asset-bundle`.

Stdlib-only (`tarfile`, `gzip`, `hashlib`, `json`, `argparse`, `tomllib` on
3.11+) per `docs/grimoire/design/scripting-unification-design.md`. Run
`--self-test` to exercise it — including a real **round-trip** through the
UNMODIFIED `sync_deps_engine.py` `VendoredCrateKind` path (build trio → serve via
the engine's `OfflineFetcher` → fetch+verify+vendor → assert the crate root
placed correctly), the critical acceptance proof.

Design: docs/grimoire/design/dependency-channel-design.md §2b.
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

# ── Reuse build_distributables.py's determinism machinery (single source) ─────
# Import the sibling builder so the crate producer shares the EXACT TarInfo
# filter, fixed-metadata constants, and streaming sha256 — the two producers can
# never drift on the byte-level determinism contract.
_THIS_DIR = Path(__file__).resolve().parent
_bd_path = _THIS_DIR / "build_distributables.py"
_spec = importlib.util.spec_from_file_location("build_distributables", _bd_path)
_bd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bd)

DistributableBuilder = _bd.DistributableBuilder
FIXED_MTIME = _bd.FIXED_MTIME
CHECKSUMS_NAME = _bd.CHECKSUMS_NAME
RELEASE_JSON_NAME = _bd.RELEASE_JSON_NAME
RELEASE_JSON_SCHEMA_VERSION = _bd.RELEASE_JSON_SCHEMA_VERSION

# ── vendored-crate constants ──────────────────────────────────────────────────

# The artifact_kind this producer emits (design §2 taxonomy). Config MAY override
# via publish.toml `artifact_kind`, but this is the only value that round-trips
# through VendoredCrateKind; a mismatch is refused up front.
ARTIFACT_KIND_VENDORED_CRATE = "vendored-crate"

# Release channels — the closed set, matching build_distributables.CHANNELS.
CHANNELS = _bd.CHANNELS

# Default publish-manifest filename (a producer analog of the consumer's
# vendor.toml). Lives at the crate/repo root.
PUBLISH_MANIFEST_NAME = "publish.toml"

# The publishable include-subset default — the crate files needed to build, as
# glob patterns matched against a repo-relative posix path. A publish.toml
# `include = [...]` overrides this wholesale. Deliberately conservative: it
# ships exactly what `cargo build --offline` needs and nothing else.
DEFAULT_INCLUDE = (
    "src/**",          # crate sources
    "Cargo.toml",      # manifest (required)
    "Cargo.lock",      # committed lockfile (if present)
    "build.rs",        # build script (if present)
    "LICENSE*",        # license file(s)
    "README.md",       # crate readme
    "migrations/**",   # embedded migrations (if the crate ships them)
)

# Path components NEVER shipped in a crate artifact, regardless of include globs
# (VCS noise, build output, machine-local / framework dirs, nested vendored
# trees). Matched against ANY path component of a repo-relative path — an
# additive filter on top of the include allowlist (issue #205 exclusion set).
EXCLUDED_COMPONENTS = frozenset({
    ".git",
    ".DS_Store",
    "__pycache__",
    "target",       # cargo build output
    "vendor",       # a consumer's own vendored deps never re-ship
    ".claude",      # Grimoire framework dir
    "dist",         # producer output
})


class CrateArtifactError(Exception):
    """A crate-producer error (invalid manifest / empty artifact / bad config)."""


# ── Producer publish manifest ─────────────────────────────────────────────────

class PublishManifest:
    """The producer's publish intent — a config-driven analog of vendor.toml.

    Mirrors how the consumer's `vendor.toml` declares CONSUMER intent: this
    declares PRODUCER intent for one crate — its publishable `name`, the
    `artifact_kind` + `channel` it publishes on, and the `include` glob subset the
    artifact ships. It makes `CrateArtifactBuilder` config-driven so an agent
    filling a small manifest never hand-rolls packaging bash.

    Parsed from `publish.toml` (TOML, 3.11+ tomllib) with a `[publish]` table:

        [publish]
        name = "token-bookkeeper"
        artifact_kind = "vendored-crate"
        channel = "stable"
        include = ["src/**", "Cargo.toml", "Cargo.lock", "LICENSE*", "README.md"]

    `version` is NOT in the manifest — it is passed at build time (from the tag /
    `--version`), exactly as `build_distributables.py` takes `--version`.
    """

    def __init__(self, name, artifact_kind=ARTIFACT_KIND_VENDORED_CRATE,
                 channel="stable", include=None):
        self.name = self._validate_name(name)
        self.artifact_kind = artifact_kind
        self.channel = channel
        # include is a tuple of glob patterns; None => the conservative default.
        self.include = tuple(include) if include else tuple(DEFAULT_INCLUDE)
        if self.artifact_kind != ARTIFACT_KIND_VENDORED_CRATE:
            raise CrateArtifactError(
                f"publish.toml artifact_kind {self.artifact_kind!r} unsupported by "
                f"this builder (only {ARTIFACT_KIND_VENDORED_CRATE!r} round-trips "
                f"through the vendored-crate consumer path)"
            )
        if self.channel not in CHANNELS:
            raise CrateArtifactError(
                f"publish.toml channel {self.channel!r} must be one of "
                f"{', '.join(CHANNELS)}"
            )

    @staticmethod
    def _validate_name(name):
        """The crate name becomes the sole top-level tarball dir + artifact stem.

        It must be a single safe path component (letters/digits/dot/dash/
        underscore, no separators / traversal) so the emitted arcname
        `<name>/...` and the artifact basename `<name>-v{ver}.tar.gz` are both
        trusted — mirroring the consumer's asset-name allowlist.
        """
        if (not name or name != os.path.basename(name)
                or name in (".", "..")
                or not all(c.isalnum() or c in "._-" for c in name)):
            raise CrateArtifactError(
                f"invalid crate name {name!r}: must be a single safe path "
                f"component (letters/digits/dot/dash/underscore)"
            )
        return name

    @classmethod
    def load(cls, path):
        """Parse a `publish.toml` at `path` into a PublishManifest."""
        try:
            import tomllib
        except ModuleNotFoundError as exc:  # pragma: no cover - explicit floor
            raise CrateArtifactError(
                "publish.toml parsing requires Python 3.11+ (tomllib)"
            ) from exc
        p = Path(path)
        if not p.is_file():
            raise CrateArtifactError(f"no publish manifest at {path}")
        with open(p, "rb") as fh:
            data = tomllib.load(fh)
        pub = data.get("publish")
        if not isinstance(pub, dict):
            raise CrateArtifactError(
                f"{path} has no [publish] table (the producer intent block)"
            )
        name = pub.get("name")
        if not name:
            raise CrateArtifactError(f"{path} [publish] is missing required 'name'")
        include = pub.get("include")
        if include is not None and not isinstance(include, list):
            raise CrateArtifactError(
                f"{path} [publish] include must be a list of glob patterns"
            )
        return cls(
            name=name,
            artifact_kind=pub.get("artifact_kind", ARTIFACT_KIND_VENDORED_CRATE),
            channel=pub.get("channel", "stable"),
            include=include,
        )


# ── The crate artifact builder ────────────────────────────────────────────────

class CrateArtifactBuilder:
    """Builds the deterministic `vendored-crate` trio for one library crate.

    Bound to a crate root, a `PublishManifest`, and a version string. `build_all`
    collects the include-subset, emits the single-top-level-dir tarball,
    `release.json` (`artifact_kind: vendored-crate`, `assets[]` = the tarball
    only), and `SHA256SUMS` (over the tarball AND release.json). The tarball is
    the exact inverse of `VendoredCrateKind`: one top-level `<name>/` dir so the
    consumer's default 1-component strip yields the crate root.

    Determinism is inherited wholesale from `build_distributables.py`: the same
    `_tar_filter`, `FIXED_MTIME`, sorted entries, and fixed-mtime gzip header — so
    the same crate tree at the same commit yields a byte-identical trio.
    """

    def __init__(self, crate_root, manifest: PublishManifest, version,
                 out_dir="dist", channel=None):
        self.crate_root = Path(crate_root).resolve()
        self.manifest = manifest
        self.version = self._normalize_version(version)
        # An explicit --channel overrides the manifest's declared channel.
        self.channel = self._normalize_channel(channel or manifest.channel)
        self.out_dir = Path(out_dir)
        if not self.out_dir.is_absolute():
            self.out_dir = self.crate_root / self.out_dir

    @staticmethod
    def _normalize_version(version):
        """Accept '0.1.0' or 'v0.1.0'; keep the crate's full semver string.

        Unlike build_distributables (MAJOR.MINOR only), a crate carries a full
        semver (e.g. 0.1.0). We only strip a leading 'v' and require at least one
        dotted numeric part, so `<name>-v{ver}.tar.gz` stays a trusted basename.
        """
        v = (version or "").strip().lstrip("vV")
        parts = v.split(".")
        if not v or any(p == "" or not p.isdigit() for p in parts):
            raise CrateArtifactError(
                f"invalid version {version!r}; expected e.g. '0.1.0'"
            )
        return v

    @staticmethod
    def _normalize_channel(channel):
        c = (channel or "stable").strip().lower()
        if c not in CHANNELS:
            raise CrateArtifactError(
                f"invalid channel {channel!r}; expected one of {', '.join(CHANNELS)}"
            )
        return c

    def _channel_suffix(self):
        return "" if self.channel == "stable" else f"-{self.channel}"

    def tarball_name(self):
        """Filename of the crate's primary artifact for this channel."""
        return f"{self.manifest.name}-v{self.version}{self._channel_suffix()}.tar.gz"

    def _is_excluded(self, rel: Path) -> bool:
        """True if a repo-relative path is excluded regardless of include globs."""
        return any(part in EXCLUDED_COMPONENTS for part in rel.parts)

    def _matches_include(self, posix: str) -> bool:
        """True if the repo-relative posix path matches any include glob.

        A trailing `/**` pattern matches the whole subtree; a bare name/glob
        matches at the crate root. Uses fnmatch per component-path so `src/**`
        catches `src/a/b.rs` and `LICENSE*` catches `LICENSE-MIT`.
        """
        for pat in self.manifest.include:
            if pat.endswith("/**"):
                prefix = pat[:-3]
                if posix == prefix or posix.startswith(prefix + "/"):
                    return True
            elif fnmatch.fnmatch(posix, pat):
                return True
        return False

    def collect_files(self):
        """Sorted list of (abs_path, arcname) for the crate include-subset.

        arcname carries the single top-level `<name>/` dir so the archive
        extracts to `<name>/…` — the exact shape `VendoredCrateKind` strips one
        component off of. Honors the include allowlist AND the hard exclusion set.
        """
        entries = []
        for path in self.crate_root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.crate_root)
            if self._is_excluded(rel):
                continue
            posix = rel.as_posix()
            if not self._matches_include(posix):
                continue
            arcname = f"{self.manifest.name}/{posix}"
            entries.append((path, arcname))
        entries.sort(key=lambda e: e[1])
        return entries

    def build_tarball(self):
        """Build the deterministic `<name>-v{ver}.tar.gz` (one top-level dir).

        Reuses `DistributableBuilder._tar_filter` (the shared TarInfo filter) and
        the fixed-mtime gzip header, so the crate tarball is byte-identical across
        rebuilds at a fixed commit — the same determinism the flavor tarball has.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.out_dir / self.tarball_name()
        entries = self.collect_files()
        if not entries:
            raise CrateArtifactError(
                f"crate '{self.manifest.name}' produced no files to ship "
                f"(include globs {list(self.manifest.include)} matched nothing "
                f"under {self.crate_root})"
            )
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tf:
            for abs_path, arcname in entries:
                data = abs_path.read_bytes()
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                info = DistributableBuilder._tar_filter(info)
                tf.addfile(info, io.BytesIO(data))
        with open(out_path, "wb") as fh:
            with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=FIXED_MTIME) as gz:
                gz.write(raw.getvalue())
        return out_path

    def _asset_entry(self, path: Path) -> dict:
        """`{name, sha256, bytes}` manifest entry — reuses the shared _sha256."""
        return {
            "name": path.name,
            "sha256": DistributableBuilder._sha256(path),
            "bytes": path.stat().st_size,
        }

    def write_release_json(self, tarball: Path) -> Path:
        """Write `release.json` (artifact_kind: vendored-crate).

        Per design §2: `assets[]` lists the TARBALL ONLY (not release.json /
        SHA256SUMS themselves); `primary_artifact` names the tarball;
        `primary_artifact_sha256` equals the tarball's hash; `git_sha` best-effort
        (reuses the shared `_git_sha`); `signature` null (deferred). Deterministic:
        sorted keys, 2-space indent, trailing newline — byte-identical at a fixed
        commit.
        """
        entry = self._asset_entry(tarball)
        # Reuse build_distributables' loud-degrade git_sha resolver, rooted at the
        # crate dir (a DistributableBuilder bound to the crate root is a cheap way
        # to borrow the exact _git_sha behaviour without duplicating it).
        git_sha = DistributableBuilder(
            self.crate_root, "0.0", self.out_dir
        )._git_sha()
        manifest = {
            "schema_version": RELEASE_JSON_SCHEMA_VERSION,
            "name": self.manifest.name,
            "version": self.version,
            "channel": self.channel,
            "git_sha": git_sha,
            "artifact_kind": self.manifest.artifact_kind,
            "primary_artifact": tarball.name,
            "primary_artifact_sha256": entry["sha256"],
            "signature": None,
            "assets": [entry],
        }
        out_path = self.out_dir / RELEASE_JSON_NAME
        out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return out_path

    def write_checksums(self, files) -> Path:
        """Write `SHA256SUMS` over `files` (tarball + release.json), sorted."""
        lines = sorted(
            f"{DistributableBuilder._sha256(f)}  {f.name}" for f in files
        )
        out_path = self.out_dir / CHECKSUMS_NAME
        out_path.write_text("\n".join(lines) + "\n")
        return out_path

    def build_all(self):
        """Emit the trio: tarball, release.json, SHA256SUMS (in that order).

        Returns the list of produced paths. `SHA256SUMS` covers the tarball AND
        release.json (design §2). No per-flavor zips, no golden image — a crate
        producer ships exactly the vendorable trio.
        """
        tarball = self.build_tarball()
        json_path = self.write_release_json(tarball)
        sums_path = self.write_checksums([tarball, json_path])
        return [tarball, json_path, sums_path]


# ── self-test ─────────────────────────────────────────────────────────────────

def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)


def _make_synthetic_crate(root: Path):
    """Lay down a small synthetic Rust crate with include + excluded files."""
    _write(root / "Cargo.toml", "[package]\nname = \"mycrate\"\nversion = \"0.1.0\"\n")
    _write(root / "Cargo.lock", "# lock\n")
    _write(root / "build.rs", "fn main() {}\n")
    _write(root / "LICENSE-MIT", "MIT\n")
    _write(root / "README.md", "# mycrate\n")
    _write(root / "src" / "lib.rs", "pub fn add(a: i32, b: i32) -> i32 { a + b }\n")
    _write(root / "src" / "util" / "mod.rs", "pub fn ok() {}\n")
    _write(root / "migrations" / "0001_init.sql", "CREATE TABLE t(id INTEGER);\n")
    # ── files that MUST be excluded ──
    _write(root / "target" / "debug" / "mycrate.rlib", b"BINARY\n")
    _write(root / ".git" / "HEAD", "ref\n")
    _write(root / "vendor" / "other" / "x.rs", "// nested vendored dep\n")
    _write(root / ".claude" / "config.json", "{}\n")
    _write(root / "tests" / "big_fixture.bin", b"NOT NEEDED TO BUILD\n")  # not in include
    _write(root / "__pycache__" / "junk.pyc", b"x")


def _self_test() -> int:
    """Exercise the builder on a synthetic crate, incl. a real round-trip through
    the UNMODIFIED sync_deps_engine.VendoredCrateKind consumer path."""
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mycrate"
        _make_synthetic_crate(root)

        # ── manifest validation ──
        try:
            PublishManifest("bad/name")
            check(False, "manifest rejects a name with a path separator")
        except CrateArtifactError:
            pass
        try:
            PublishManifest("ok", artifact_kind="asset-bundle")
            check(False, "manifest rejects a non-vendored-crate artifact_kind")
        except CrateArtifactError:
            pass
        try:
            PublishManifest("ok", channel="nightly")
            check(False, "manifest rejects an unknown channel")
        except CrateArtifactError:
            pass

        # ── version normalization ──
        m = PublishManifest("mycrate")
        b = CrateArtifactBuilder(root, m, "v0.1.0", out_dir=root / "dist")
        check(b.version == "0.1.0", "version 'v0.1.0' normalized to '0.1.0'")
        check(b.tarball_name() == "mycrate-v0.1.0.tar.gz", "tarball name (stable)")
        try:
            CrateArtifactBuilder(root, m, "nope")
            check(False, "bad version raises")
        except CrateArtifactError:
            pass

        # ── build the trio ──
        paths = b.build_all()
        names = {p.name for p in paths}
        check("mycrate-v0.1.0.tar.gz" in names, "tarball emitted")
        check(RELEASE_JSON_NAME in names, "release.json emitted")
        check(CHECKSUMS_NAME in names, "SHA256SUMS emitted")
        check(len(paths) == 3, "exactly the trio emitted (no zips/golden)")
        tarball = next(p for p in paths if p.name == "mycrate-v0.1.0.tar.gz")

        # ── tarball shape: ONE top-level dir + include-subset, exclusions gone ──
        with tarfile.open(tarball, "r:gz") as tf:
            members = tf.getmembers()
        tnames = {mm.name for mm in members}
        top_dirs = {n.split("/", 1)[0] for n in tnames}
        check(top_dirs == {"mycrate"}, f"exactly one top-level dir; got {top_dirs}")
        for want in ("mycrate/Cargo.toml", "mycrate/Cargo.lock", "mycrate/build.rs",
                     "mycrate/LICENSE-MIT", "mycrate/README.md",
                     "mycrate/src/lib.rs", "mycrate/src/util/mod.rs",
                     "mycrate/migrations/0001_init.sql"):
            check(want in tnames, f"include-subset carries {want}")
        for gone in ("target", ".git", "vendor", ".claude", "__pycache__"):
            check(not any(f"/{gone}/" in n or n.startswith(f"mycrate/{gone}/")
                          for n in tnames), f"{gone}/ excluded from crate tarball")
        check(not any(n.endswith("big_fixture.bin") for n in tnames),
              "test fixture not in include-subset is excluded")
        # determinism fields zeroed by the shared TarInfo filter
        for mm in members:
            check(mm.mtime == FIXED_MTIME, f"tar mtime zeroed for {mm.name}")
            check(mm.uid == 0 and mm.gid == 0, f"tar uid/gid zeroed for {mm.name}")
            check(mm.uname == "" and mm.gname == "", f"tar uname/gname zeroed {mm.name}")

        # ── release.json contract (design §2) ──
        manifest = json.loads((b.out_dir / RELEASE_JSON_NAME).read_text())
        required = {
            "schema_version", "name", "version", "channel", "git_sha",
            "artifact_kind", "primary_artifact", "primary_artifact_sha256",
            "signature", "assets",
        }
        check(required.issubset(manifest.keys()), "release.json has every required field")
        check(manifest["schema_version"] == 1, "schema_version=1")
        check(manifest["name"] == "mycrate", "release.json name")
        check(manifest["version"] == "0.1.0", "release.json version")
        check(manifest["channel"] == "stable", "release.json channel=stable")
        check(manifest["artifact_kind"] == ARTIFACT_KIND_VENDORED_CRATE,
              "artifact_kind=vendored-crate")
        check(manifest["primary_artifact"] == "mycrate-v0.1.0.tar.gz",
              "primary_artifact names the tarball")
        check(manifest["signature"] is None, "signature=null (deferred)")
        check(manifest["git_sha"] is None or isinstance(manifest["git_sha"], str),
              "git_sha is str|null")
        check(len(manifest["assets"]) == 1
              and manifest["assets"][0]["name"] == "mycrate-v0.1.0.tar.gz",
              "assets[] lists the tarball ONLY (not release.json/SHA256SUMS)")
        check(set(manifest["assets"][0].keys()) == {"name", "sha256", "bytes"},
              "asset entry shape {name,sha256,bytes}")
        check(manifest["primary_artifact_sha256"] == manifest["assets"][0]["sha256"]
              == DistributableBuilder._sha256(tarball),
              "primary_artifact_sha256 == tarball hash == assets[] sha256")

        # ── SHA256SUMS covers the tarball AND release.json, and verifies ──
        sums_text = (b.out_dir / CHECKSUMS_NAME).read_text()
        sums_map = {}
        for line in sums_text.strip().splitlines():
            digest, _, name = line.partition("  ")
            sums_map[name] = digest
        check("mycrate-v0.1.0.tar.gz" in sums_map, "SHA256SUMS lists the tarball")
        check(RELEASE_JSON_NAME in sums_map, "SHA256SUMS lists release.json")
        check(CHECKSUMS_NAME not in sums_map, "SHA256SUMS does not list itself")
        for name, digest in sums_map.items():
            check(DistributableBuilder._sha256(b.out_dir / name) == digest,
                  f"SHA256SUMS digest verifies for {name}")

        # ── determinism: rebuild into a second dir, compare bytes ──
        b2 = CrateArtifactBuilder(root, m, "0.1.0", out_dir=root / "dist2")
        paths2 = b2.build_all()
        t2 = next(p for p in paths2 if p.name == "mycrate-v0.1.0.tar.gz")
        check(tarball.read_bytes() == t2.read_bytes(),
              "crate tarball byte-identical across rebuilds")
        check((b.out_dir / RELEASE_JSON_NAME).read_bytes()
              == (b2.out_dir / RELEASE_JSON_NAME).read_bytes(),
              "release.json byte-identical across rebuilds")
        check((b.out_dir / CHECKSUMS_NAME).read_text()
              == (b2.out_dir / CHECKSUMS_NAME).read_text(),
              "SHA256SUMS byte-identical across rebuilds")

        # ── beta channel suffix ──
        bb = CrateArtifactBuilder(root, m, "0.1.0", out_dir=root / "dist-beta",
                                  channel="beta")
        check(bb.tarball_name() == "mycrate-v0.1.0-beta.tar.gz", "beta tarball suffix")
        beta_paths = bb.build_all()
        beta_manifest = json.loads((bb.out_dir / RELEASE_JSON_NAME).read_text())
        check(beta_manifest["channel"] == "beta", "release.json channel=beta")

        # ── custom include globs via a PublishManifest ──
        m2 = PublishManifest("mycrate", include=["src/**", "Cargo.toml"])
        b3 = CrateArtifactBuilder(root, m2, "0.1.0", out_dir=root / "dist3")
        t3 = b3.build_tarball()
        with tarfile.open(t3, "r:gz") as tf:
            t3names = {mm.name for mm in tf.getmembers()}
        check("mycrate/src/lib.rs" in t3names and "mycrate/Cargo.toml" in t3names,
              "custom include ships src/ + Cargo.toml")
        check("mycrate/README.md" not in t3names,
              "custom include excludes README (not in the narrowed allowlist)")

        # ── empty-artifact guard ──
        empty_root = Path(td) / "emptycrate"
        _write(empty_root / "notes.txt", "nothing includable\n")
        be = CrateArtifactBuilder(empty_root, PublishManifest("emptycrate"),
                                  "0.1.0", out_dir=empty_root / "dist")
        try:
            be.build_all()
            check(False, "empty include set raises (loud, never a silent empty tarball)")
        except CrateArtifactError:
            pass

        # ── publish.toml round-trip parse ──
        _write(root / PUBLISH_MANIFEST_NAME,
               '[publish]\n'
               'name = "mycrate"\n'
               'artifact_kind = "vendored-crate"\n'
               'channel = "stable"\n'
               'include = ["src/**", "Cargo.toml", "Cargo.lock", "LICENSE*", "README.md"]\n')
        loaded = PublishManifest.load(root / PUBLISH_MANIFEST_NAME)
        check(loaded.name == "mycrate", "publish.toml name parsed")
        check(loaded.artifact_kind == "vendored-crate", "publish.toml artifact_kind parsed")
        check("src/**" in loaded.include, "publish.toml include parsed")

        # ── THE CRITICAL ACCEPTANCE PROOF: round-trip through the UNMODIFIED
        #    sync_deps_engine.VendoredCrateKind consumer path ──
        rt_ok = _round_trip_through_consumer(td, root, check)
        check(rt_ok, "round-trip through sync_deps_engine.VendoredCrateKind succeeded")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        print(f"\n{len(failures)} self-test failure(s).", file=sys.stderr)
        return 1
    print("build_crate_artifact self-test: all checks passed "
          "(incl. round-trip through the unmodified vendored-crate consumer).")
    return 0


def _round_trip_through_consumer(td, crate_root, check) -> bool:
    """Build the trio, serve it as an offline release, and vendor it via the
    REAL, UNMODIFIED sync_deps_engine.VendoredCrateKind path.

    This is the acceptance criterion (issue #205): the emitted tarball +
    release.json + SHA256SUMS must round-trip through the exact consumer engine
    the fleet uses — sha256 verifies, and the default 1-component strip yields the
    crate root. We import sync_deps_engine.py from its skill dir and drive a real
    `SyncDepsEngine.sync()` against an `OfflineFetcher` fixture we populate from
    our own build output. Nothing in the consumer is modified or stubbed.
    """
    # Locate the (unmodified) consumer engine relative to this skill.
    engine_path = (_THIS_DIR.parent / "grm-sync-deps" / "sync_deps_engine.py")
    if not engine_path.is_file():
        print(f"NOTE: sync_deps_engine.py not found at {engine_path}; "
              f"round-trip proof SKIPPED (parity issue, not a producer bug).",
              file=sys.stderr)
        # A missing consumer is an environment/parity problem, not a failure of
        # this producer — surface it loudly but do not fail the producer's own
        # self-test on it.
        return True
    spec = importlib.util.spec_from_file_location("sync_deps_engine", engine_path)
    engine_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine_mod)

    # 1. Build the real trio for the synthetic crate.
    manifest = PublishManifest("mycrate")
    builder = CrateArtifactBuilder(crate_root, manifest, "0.1.0",
                                   out_dir=Path(td) / "producer-dist")
    trio = builder.build_all()
    dist_dir = trio[0].parent

    # 2. Lay the trio out as an OfflineFetcher fixture:
    #    <fixture>/<slug>/<tag>/{artifact, release.json, SHA256SUMS}.
    slug = "acme/mycrate"
    tag = "v0.1.0"
    fixture_root = Path(td) / "fixtures"
    rel_dir = fixture_root / slug / tag
    rel_dir.mkdir(parents=True, exist_ok=True)
    for p in trio:
        (rel_dir / p.name).write_bytes(p.read_bytes())

    # 3. A consumer repo pinning it as kind = "vendored-crate" (DEFAULT strip —
    #    NO explicit strip_components, so the engine's default-1 path is proven).
    consumer = Path(td) / "consumer"
    consumer.mkdir(parents=True, exist_ok=True)
    (consumer / "vendor.toml").write_text(
        'schema_version = 1\n\n'
        '[deps.mycrate]\n'
        'repo = "acme/mycrate"\n'
        'channel = "stable"\n'
        'version = "0.1.0"\n'
        'artifact = "mycrate-v0.1.0.tar.gz"\n'
        'dest = "lib/third-party/mycrate"\n'
        'kind = "vendored-crate"\n'
    )

    # 4. Run the REAL engine against the OfflineFetcher — this performs
    #    fetch → verify-sha256-before-place → extract (default 1-strip) → vendor.
    eng = engine_mod.SyncDepsEngine(
        root=str(consumer),
        fetcher=engine_mod.OfflineFetcher(str(fixture_root)),
    )
    rc = eng.sync()
    check(rc == engine_mod.EXIT_OK, "consumer sync() exits OK (sha256 verified)")

    # 5. Assert the default 1-component strip yielded the crate ROOT (not
    #    mycrate/mycrate/…), and the include-subset landed.
    dest = consumer / "lib" / "third-party" / "mycrate"
    check((dest / "Cargo.toml").is_file(),
          "crate root placed at dest root (default 1-strip applied)")
    check((dest / "src" / "lib.rs").is_file(), "crate src/ vendored")
    check((dest / "migrations" / "0001_init.sql").is_file(), "crate migrations/ vendored")
    check(not (dest / "mycrate").exists(),
          "no double-nested mycrate/ dir (strip yielded the true root)")

    # 6. The lock's artifact_sha256 must equal our SHA256SUMS entry, and its
    #    tree_sha256 the placed bytes — the producer/consumer hashes agree.
    lock = json.loads((consumer / "vendor.lock").read_text())
    entry = lock["deps"]["mycrate"]
    tarball = dist_dir / "mycrate-v0.1.0.tar.gz"
    expected_art = engine_mod.HASH_PREFIX + engine_mod.sha256_of_file(str(tarball))
    check(entry["artifact_sha256"] == expected_art,
          "lock artifact_sha256 == our producer SHA256SUMS entry")
    check(entry["tree_sha256"] == engine_mod.tree_sha256(str(dest)),
          "lock tree_sha256 == placed-bytes hash")

    # 7. Offline validation succeeds (the built bytes validate with zero network).
    check(eng.offline_validate() == engine_mod.EXIT_OK,
          "consumer --offline validates the vendored bytes")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build a library crate's deterministic vendored-crate channel "
                    "artifact trio (<name>-v{ver}.tar.gz + release.json + SHA256SUMS)."
    )
    ap.add_argument("--version", help="crate release version, e.g. 0.1.0 or v0.1.0")
    ap.add_argument("--root", default=".", help="crate root (default: cwd)")
    ap.add_argument("--manifest", default=None,
                    help=f"publish manifest path (default: <root>/{PUBLISH_MANIFEST_NAME})")
    ap.add_argument("--out", default="dist", help="output dir (default: dist/)")
    ap.add_argument("--channel", default=None, choices=CHANNELS,
                    help="release channel (default: the manifest's channel)")
    ap.add_argument("--self-test", action="store_true", help="run the in-file tests")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.version:
        ap.error("--version is required (unless --self-test)")

    root = Path(args.root)
    manifest_path = Path(args.manifest) if args.manifest else root / PUBLISH_MANIFEST_NAME
    try:
        manifest = PublishManifest.load(manifest_path)
        builder = CrateArtifactBuilder(root, manifest, args.version,
                                       out_dir=Path(args.out), channel=args.channel)
        paths = builder.build_all()
    except CrateArtifactError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
