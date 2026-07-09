#!/usr/bin/env python3
"""build_distributables.py — build a release's distributable assets.

Part of the v3.23 release-distribution protocol, extended in v3.27 with release
**channels** and a **signing contract**, and again in v3.29 (the Dependency
Channel — `docs/design/dependency-channel-design.md` §2) with a canonical
deterministic **`.tar.gz`** primary artifact and the generalized **`release.json`**
manifest. The integration master runs this at release time, after the tag is on
origin, to produce the GitHub Release assets.

A *flavor* is any top-level directory under the repo root that carries a
`.grimoire-flavor` marker file (today: `claude-code/`, `copilot/`; future flavors
join automatically by adding the marker). For each flavor this builds a
deterministic, reproducible archive:

    dist/grimoire-<flavor>-v<MAJOR>.<MINOR>.zip          (stable channel)
    dist/grimoire-<flavor>-v<MAJOR>.<MINOR>-beta.zip     (beta channel)

containing the flavor directory WITH its top-level name (entries are
`claude-code/.claude/…`), so unzipping into a temp dir T yields `T/<flavor>/` —
exactly the `"$SRC/$FLAVOR"` shape `sync-from-upstream.sh` consumes. The
per-flavor `.zip`s stay (the `sync-from-upstream` transport selects + verifies
them by filename glob against `SHA256SUMS`).

Canonical tarball + manifest (v3.29). Alongside the `.zip`s the build emits one
canonical primary artifact `grimoire-v<MAJOR>.<MINOR>[-beta].tar.gz` — the
canonical `claude-code` flavor tree, the `asset-bundle` primary artifact —
produced via stdlib `tarfile` plus a `TarInfo` filter that zeroes
`mtime`/`uid`/`gid`/`uname`/`gname` and fixes the mode, so the same tree at the
same commit yields byte-identical bytes. It also emits **`release.json`**, the
generalized kind-discriminated manifest (schema in
`dependency-channel-design.md` §2). `release.json` **replaces** the retired
`RELEASE-META.json` as the single manifest / channel-of-record (it is a strict
superset of the v3.27 `{schema,channel,version,prerelease,assets}` fields,
`prerelease` being derivable from `channel`).

Channels (v3.27). `--channel stable|beta` (default stable) selects the release
channel. `stable` is the normal `v{X.Y}` release; `beta` is a `--prerelease`
published off the `version/{X.Y}` staging branch. The channel is encoded three
ways for redundancy: (a) the asset filename suffix (`-beta`), (b) the `channel`
field in `release.json`, and (c) — set by the release recipe, not this builder —
the GitHub `--prerelease` flag + a `channel:<name>` release label. A consumer
pins a channel via `UPSTREAM_CHANNEL` (see `sync-from-upstream.sh`).

Signing contract (v3.27, seam preserved v3.29). Every build emits `dist/SHA256SUMS`
covering ALL top-level release assets (the `.zip`s, the canonical `.tar.gz`, and
`release.json`) — ALWAYS, unconditionally. When the `minisign` tool AND a secret
key are available, the build additionally emits `dist/SHA256SUMS.minisig`; when
either is absent the build prints a LOUD, documented degradation notice (never a
silent skip) and continues, since the checksum file is the always-present
integrity floor. Signing is DEFERRED (`release.json` `signature` = null in v1).

Reproducible by construction: entries are sorted, timestamps and permissions are
fixed, so the same source tree yields byte-identical archives, a byte-identical
canonical tarball, a byte-identical `release.json` (at a fixed commit), and a
byte-identical `SHA256SUMS`.

Stdlib-only for the core (`zipfile`, `tarfile`, `pathlib`, `hashlib`, `json`,
`argparse`) per `docs/design/scripting-unification-design.md`; `minisign` is an
optional external tool invoked via `subprocess` only when present; `git` is
invoked best-effort for `git_sha` and degrades loudly to null. Run `--self-test`
to exercise it.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

# Marker file that designates a top-level directory as a shippable flavor.
FLAVOR_MARKER = ".grimoire-flavor"

# The canonical flavor whose tree becomes the canonical `.tar.gz` primary
# artifact (the `asset-bundle` per `dependency-channel-design.md` §2). This is
# the gold-standard copy of the workflow (CLAUDE.md "claude-code is canonical").
CANONICAL_FLAVOR = "claude-code"

# Release channels. 'stable' = normal v{X.Y} tag; 'beta' = --prerelease off the
# version/{X.Y} staging branch. The set is closed; an unknown channel is rejected.
CHANNELS = ("stable", "beta")

# Files the build always emits alongside the per-flavor archives.
CHECKSUMS_NAME = "SHA256SUMS"
RELEASE_JSON_NAME = "release.json"
MINISIG_NAME = "SHA256SUMS.minisig"

# release.json wire-format constants (dependency-channel-design.md §2).
RELEASE_JSON_SCHEMA_VERSION = 1
# This repo's builder only ever emits the asset-bundle kind; the vendored-crate
# and app-binary kinds are produced by managed-project builders that conform to
# the same schema (the framework / managed-project boundary).
ARTIFACT_KIND_ASSET_BUNDLE = "asset-bundle"
# The dependency's stable name (matches a consumer's `[deps.<name>]`).
DEPENDENCY_NAME = "grimoire"

# Env var naming the minisign secret-key file. Absent => signing degrades loudly.
MINISIGN_KEY_ENV = "MINISIGN_SECRET_KEY"

# Path segments never included in a distributable (VCS noise + machine-local /
# derived dirs). Matched against any path component, relative to the flavor dir.
EXCLUDED_COMPONENTS = frozenset({
    ".git",
    ".DS_Store",
    "__pycache__",
    ".scaffold-base",
    ".scaffold-sync-backup",
    "dist",
})

# Fixed metadata for reproducible archives (no real mtimes / host permissions).
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)   # zip epoch; the lowest legal value.
FIXED_MTIME = 0                            # tar epoch; zeroed for determinism.
FILE_PERMISSIONS = 0o644
DIR_PERMISSIONS = 0o755

# Streaming hash chunk size (constant memory).
_HASH_CHUNK = 65536


class DistributableBuilder:
    """Builds deterministic release assets for a release.

    One instance is bound to a repo root, a version string, and a release channel.
    `build_all` discovers the flavors, writes one `.zip` per flavor, emits the
    canonical `grimoire-v{X.Y}[-beta].tar.gz` primary artifact, the generalized
    `release.json` manifest, and the always-present `SHA256SUMS` over the whole
    asset set, then (when minisign is available) the `SHA256SUMS.minisig`
    signature. `release.json` is the single manifest / channel-of-record (the
    retired `RELEASE-META.json` is a strict subset of it).
    """

    def __init__(self, root: Path, version: str, out_dir: Path, channel: str = "stable"):
        self.root = Path(root).resolve()
        self.version = self._normalize_version(version)
        self.channel = self._normalize_channel(channel)
        self.out_dir = Path(out_dir)
        if not self.out_dir.is_absolute():
            self.out_dir = self.root / self.out_dir

    @staticmethod
    def _normalize_version(version: str) -> str:
        """Accept '3.23' or 'v3.23'; return the bare 'MAJOR.MINOR' form."""
        v = version.strip().lstrip("vV")
        if not v or any(part == "" or not part.isdigit() for part in v.split(".")):
            raise ValueError(f"invalid version {version!r}; expected e.g. '3.23'")
        return v

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        """Validate the channel against the closed set; default-friendly."""
        c = (channel or "stable").strip().lower()
        if c not in CHANNELS:
            raise ValueError(
                f"invalid channel {channel!r}; expected one of {', '.join(CHANNELS)}"
            )
        return c

    def _channel_suffix(self) -> str:
        """Asset filename suffix that encodes the channel ('' for stable)."""
        return "" if self.channel == "stable" else f"-{self.channel}"

    def _is_excluded(self, rel_parts) -> bool:
        """True if any path component is in the exclusion set."""
        return any(part in EXCLUDED_COMPONENTS for part in rel_parts)

    def discover_flavors(self):
        """Return the sorted list of flavor dir names (those carrying the marker)."""
        flavors = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and (child / FLAVOR_MARKER).is_file():
                flavors.append(child.name)
        return flavors

    def _collect_files(self, flavor_dir: Path):
        """Sorted list of (abs_path, arcname) for the flavor, honouring exclusions.

        arcname carries the flavor's top-level name so the archive extracts to
        `<flavor>/...`.
        """
        entries = []
        flavor_name = flavor_dir.name
        for path in flavor_dir.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            rel = path.relative_to(flavor_dir)
            if self._is_excluded(rel.parts):
                continue
            arcname = f"{flavor_name}/{rel.as_posix()}"
            entries.append((path, arcname))
        entries.sort(key=lambda e: e[1])
        return entries

    def build_flavor(self, flavor_name: str) -> Path:
        """Build one flavor's `.zip` archive; return the output path."""
        flavor_dir = self.root / flavor_name
        if not (flavor_dir / FLAVOR_MARKER).is_file():
            raise ValueError(f"'{flavor_name}' is not a flavor (no {FLAVOR_MARKER})")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = (
            self.out_dir
            / f"grimoire-{flavor_name}-v{self.version}{self._channel_suffix()}.zip"
        )
        entries = self._collect_files(flavor_dir)
        # Open in 'w' to overwrite deterministically (idempotent re-run).
        with zipfile.ZipFile(
            out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zf:
            for abs_path, arcname in entries:
                info = zipfile.ZipInfo(filename=arcname, date_time=FIXED_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = FILE_PERMISSIONS << 16
                zf.writestr(info, abs_path.read_bytes())
        return out_path

    # ── canonical tarball (v3.29) ────────────────────────────────────────────
    @staticmethod
    def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
        """Zero all host-/time-varying TarInfo fields for reproducible tarballs.

        Strips `mtime`/`uid`/`gid`/`uname`/`gname` and fixes the mode so the same
        tree yields byte-identical archive bytes regardless of when or where the
        build runs (dependency-channel-design.md §2).
        """
        info.mtime = FIXED_MTIME
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mode = DIR_PERMISSIONS if info.isdir() else FILE_PERMISSIONS
        return info

    def tarball_name(self) -> str:
        """Filename of the canonical `.tar.gz` primary artifact for this channel."""
        return f"grimoire-v{self.version}{self._channel_suffix()}.tar.gz"

    def build_tarball(self) -> Path:
        """Build the canonical deterministic `grimoire-v{X.Y}[-beta].tar.gz`.

        The asset-bundle primary artifact: the canonical `claude-code` flavor
        tree, emitted via stdlib `tarfile` with `_tar_filter` and a fixed-mtime
        gzip header (`mtime=0`), so the same tree at the same commit is
        byte-identical across rebuilds. Reuses `_collect_files()` (sorted, same
        exclusion set) for the entry list.
        """
        flavor_dir = self.root / CANONICAL_FLAVOR
        if not (flavor_dir / FLAVOR_MARKER).is_file():
            raise ValueError(
                f"canonical flavor '{CANONICAL_FLAVOR}' not found "
                f"(no {FLAVOR_MARKER} under {flavor_dir})"
            )
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.out_dir / self.tarball_name()
        entries = self._collect_files(flavor_dir)
        # Write the uncompressed tar into a buffer, then gzip with a fixed mtime
        # header — gzip.GzipFile(mtime=0) keeps the .gz wrapper deterministic
        # (the default writes the current time into the gzip header).
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tf:
            for abs_path, arcname in entries:
                data = abs_path.read_bytes()
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                info = self._tar_filter(info)
                tf.addfile(info, io.BytesIO(data))
        with open(out_path, "wb") as fh:
            with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=FIXED_MTIME) as gz:
                gz.write(raw.getvalue())
        return out_path

    def build_all(self, flavors=None):
        """Build the requested (or all) flavors + the canonical assets, then sign.

        Emits, in order: the per-flavor `.zip`s, the canonical `.tar.gz` primary
        artifact, `release.json`, and the always-present `SHA256SUMS` over the
        whole top-level asset set, plus — when minisign is available —
        `SHA256SUMS.minisig`. The signing step degrades loudly when minisign or
        its key is absent; it never silently skips. `release.json` is the single
        manifest / channel-of-record (the retired `RELEASE-META.json` is no longer
        emitted).
        """
        if flavors is None:
            flavors = self.discover_flavors()
            if not flavors:
                raise RuntimeError(
                    f"no flavors found under {self.root} "
                    f"(a flavor is a top-level dir with a {FLAVOR_MARKER} marker)"
                )
        archives = [self.build_flavor(name) for name in flavors]
        tarball = self.build_tarball()
        # All published top-level assets EXCEPT release.json (which records their
        # hashes) and SHA256SUMS (which is computed over them). Hash each one once
        # and feed the same list to both integrity surfaces so they can't drift.
        published = archives + [tarball]
        asset_entries = [self._asset_entry(p) for p in published]
        json_path = self.write_release_json(tarball, asset_entries)
        sums_path = self.write_checksums(published + [json_path])
        outputs = archives + [tarball, json_path, sums_path]
        sig_path = self.sign_checksums(sums_path)
        if sig_path is not None:
            outputs.append(sig_path)
        return outputs

    # ── integrity + manifest (v3.27, generalized v3.29) ──────────────────────
    @staticmethod
    def _sha256(path: Path) -> str:
        """Stream a file through SHA-256 (constant memory); return the hex digest."""
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    def _asset_entry(self, path: Path) -> dict:
        """Compute the `{name, sha256, bytes}` manifest entry for one asset.

        Computed once per asset and reused for both `release.json` `assets[]` and
        `SHA256SUMS`, so the two integrity surfaces can never drift.
        """
        return {
            "name": path.name,
            "sha256": self._sha256(path),
            "bytes": path.stat().st_size,
        }

    def _git_sha(self):
        """Best-effort full commit SHA the build was cut from; None if unavailable.

        Runs `git rev-parse HEAD` with `cwd=self.root`. Degrades **loudly** to
        `None` (a LOUD stderr notice, never a silent skip) when git is absent, the
        root is not a work tree, or the command fails — the build stays
        publishable from a detached / exported tree, with determinism then
        narrowed to same-tree-at-same-commit (dependency-channel-design.md §2).
        """
        tool = shutil.which("git")
        if tool is None:
            self._loud_git_sha_skip("the 'git' tool is not installed / not on PATH")
            return None
        try:
            result = subprocess.run(
                [tool, "rev-parse", "HEAD"],
                cwd=str(self.root),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (subprocess.CalledProcessError, OSError) as e:
            self._loud_git_sha_skip(f"git rev-parse HEAD failed: {e}")
            return None
        sha = result.stdout.strip()
        if not sha:
            self._loud_git_sha_skip("git rev-parse HEAD returned no commit")
            return None
        return sha

    def write_release_json(self, tarball: Path, asset_entries) -> Path:
        """Write the generalized `release.json` manifest (schema §2).

        `release.json` is the single manifest / channel-of-record; it generalizes
        and replaces the retired `RELEASE-META.json` (a strict superset). This
        repo's builder always emits `artifact_kind: asset-bundle` with the
        canonical `.tar.gz` as the primary artifact. `signature` is `null` (signing
        deferred in v1). Deterministic: sorted keys + sorted `assets[]` + a fixed
        2-space indent + a trailing newline, so the same inputs at a fixed commit
        produce byte-identical bytes.
        """
        primary = tarball.name
        primary_sha256 = next(
            e["sha256"] for e in asset_entries if e["name"] == primary
        )
        manifest = {
            "schema_version": RELEASE_JSON_SCHEMA_VERSION,
            "name": DEPENDENCY_NAME,
            "version": self.version,
            "channel": self.channel,
            "git_sha": self._git_sha(),
            "artifact_kind": ARTIFACT_KIND_ASSET_BUNDLE,
            "primary_artifact": primary,
            "primary_artifact_sha256": primary_sha256,
            "signature": None,
            "assets": sorted(asset_entries, key=lambda e: e["name"]),
        }
        out_path = self.out_dir / RELEASE_JSON_NAME
        out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return out_path

    def write_checksums(self, files) -> Path:
        """Write `SHA256SUMS` over `files` in the canonical `<hex>  <name>` form.

        Names are basenames (assets sit beside the checksum file in `dist/`) and
        sorted, so the output is deterministic and re-verifiable with the standard
        `sha256sum -c SHA256SUMS` run from within `dist/`.
        """
        lines = sorted(f"{self._sha256(f)}  {f.name}" for f in files)
        out_path = self.out_dir / CHECKSUMS_NAME
        out_path.write_text("\n".join(lines) + "\n")
        return out_path

    def sign_checksums(self, sums_path: Path):
        """Sign `SHA256SUMS` with minisign when available; else degrade LOUDLY.

        Returns the signature path on success, or None when signing was skipped.
        A skip is never silent: it prints exactly which precondition was missing
        (tool absent vs. key absent) and that the build proceeds with the
        always-present `SHA256SUMS` as the integrity floor. (Signing is deferred
        in v1 — `release.json` `signature` stays null — but the seam is preserved.)
        """
        tool = shutil.which("minisign")
        key = os.environ.get(MINISIGN_KEY_ENV, "").strip()
        if tool is None:
            self._loud_signing_skip(
                "the 'minisign' tool is not installed / not on PATH"
            )
            return None
        if not key:
            self._loud_signing_skip(
                f"no signing key (env {MINISIGN_KEY_ENV} is unset/empty)"
            )
            return None
        if not Path(key).is_file():
            self._loud_signing_skip(
                f"signing key file does not exist: {key}"
            )
            return None
        sig_path = self.out_dir / MINISIG_NAME
        try:
            subprocess.run(
                [tool, "-S", "-s", key, "-m", str(sums_path), "-x", str(sig_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except (subprocess.CalledProcessError, OSError) as e:
            self._loud_signing_skip(f"minisign invocation failed: {e}")
            return None
        print(f"Signed {CHECKSUMS_NAME} -> {sig_path.name} (minisign).")
        return sig_path

    @staticmethod
    def _loud_signing_skip(reason: str) -> None:
        """Emit the documented, unmissable degradation notice for an unsigned build."""
        print(
            "WARNING: release assets are NOT minisign-signed — " + reason + ".\n"
            "         SHA256SUMS is still emitted (checksum integrity floor), but\n"
            "         consumers cannot cryptographically verify provenance for this\n"
            "         build. Install minisign and set "
            + MINISIGN_KEY_ENV
            + " to sign.",
            file=sys.stderr,
        )

    @staticmethod
    def _loud_git_sha_skip(reason: str) -> None:
        """Emit the documented, unmissable degradation notice for a null git_sha."""
        print(
            "WARNING: release.json git_sha degraded to null — " + reason + ".\n"
            "         The build stays publishable, but provenance cannot pin the\n"
            "         exact commit and determinism narrows to same-tree-at-same-commit.",
            file=sys.stderr,
        )


# ── self-test ───────────────────────────────────────────────────────────────
def _self_test() -> int:
    """Exercise every method on a synthetic two-flavor tree; no network/git."""
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Two synthetic flavors (one named the canonical flavor) + a non-flavor
        # dir + exclusions.
        for name in (CANONICAL_FLAVOR, "flavor-b"):
            d = root / name
            (d / ".claude" / "skills").mkdir(parents=True)
            (d / FLAVOR_MARKER).write_text("")
            (d / "CLAUDE.md").write_text(f"# {name}\n")
            (d / ".claude" / "skills" / "x.md").write_text("hi\n")
            # exclusions that must NOT appear in the archive
            (d / "__pycache__").mkdir()
            (d / "__pycache__" / "junk.pyc").write_text("x")
            (d / ".git").mkdir()
            (d / ".git" / "HEAD").write_text("ref")
        (root / "not-a-flavor").mkdir()
        (root / "not-a-flavor" / "f.txt").write_text("x")

        b = DistributableBuilder(root, "9.9", root / "dist")

        # version normalization
        check(DistributableBuilder._normalize_version("v1.2") == "1.2", "vN.N normalize")
        check(DistributableBuilder._normalize_version("1.2") == "1.2", "N.N normalize")
        try:
            DistributableBuilder._normalize_version("nope")
            check(False, "bad version should raise")
        except ValueError:
            pass

        # channel normalization
        check(DistributableBuilder._normalize_channel("stable") == "stable", "stable channel")
        check(DistributableBuilder._normalize_channel("BETA") == "beta", "beta channel (case-insensitive)")
        check(DistributableBuilder._normalize_channel("") == "stable", "empty channel defaults stable")
        try:
            DistributableBuilder._normalize_channel("nightly")
            check(False, "unknown channel should raise")
        except ValueError:
            pass

        # discovery (sorted, marker-gated, excludes non-flavor dir)
        check(b.discover_flavors() == [CANONICAL_FLAVOR, "flavor-b"], "discover flavors")

        # build all (stable): .zips + canonical .tar.gz + release.json + SHA256SUMS,
        # no minisig unless minisign + key are present (not configured here).
        os.environ.pop(MINISIGN_KEY_ENV, None)
        paths1 = b.build_all()
        out_names1 = {p.name for p in paths1}
        check(len([p for p in paths1 if p.name.endswith(".zip")]) == 2, "two archives built")
        check(f"grimoire-{CANONICAL_FLAVOR}-v9.9.zip" in out_names1, "stable archive naming (no suffix)")
        check("grimoire-v9.9.tar.gz" in out_names1, "canonical tarball naming (stable, no suffix)")
        check(RELEASE_JSON_NAME in out_names1, "release.json emitted")
        check(CHECKSUMS_NAME in out_names1, "SHA256SUMS always emitted")
        check("RELEASE-META.json" not in out_names1, "RELEASE-META.json no longer emitted")
        check(MINISIG_NAME not in out_names1, "no signature when minisign/key absent")
        archive_a = next(p for p in paths1 if p.name == f"grimoire-{CANONICAL_FLAVOR}-v9.9.zip")
        tarball = next(p for p in paths1 if p.name == "grimoire-v9.9.tar.gz")

        with zipfile.ZipFile(archive_a) as zf:
            names = set(zf.namelist())
        check(f"{CANONICAL_FLAVOR}/CLAUDE.md" in names, "top-level-name layout")
        check(f"{CANONICAL_FLAVOR}/.claude/skills/x.md" in names, "nested entry present")
        check(not any("__pycache__" in n for n in names), "__pycache__ excluded")
        check(not any("/.git/" in n or n.endswith("/.git") for n in names), ".git excluded")
        check(all(n.startswith(f"{CANONICAL_FLAVOR}/") for n in names), "all entries under flavor dir")

        # canonical tarball: contents mirror the canonical flavor tree, exclusions
        # honoured, and a deterministic TarInfo (zeroed mtime/uid/gid/uname/gname).
        with tarfile.open(tarball, "r:gz") as tf:
            members = tf.getmembers()
        tnames = {m.name for m in members}
        check(f"{CANONICAL_FLAVOR}/CLAUDE.md" in tnames, "tarball carries canonical tree")
        check(not any("__pycache__" in n for n in tnames), "tarball excludes __pycache__")
        check(not any("/.git/" in n or n.endswith("/.git") for n in tnames), "tarball excludes .git")
        for m in members:
            check(m.mtime == FIXED_MTIME, f"tar mtime zeroed for {m.name}")
            check(m.uid == 0 and m.gid == 0, f"tar uid/gid zeroed for {m.name}")
            check(m.uname == "" and m.gname == "", f"tar uname/gname zeroed for {m.name}")

        # SHA256SUMS covers every .zip + the tarball + release.json, and verifies.
        sums_path = b.out_dir / CHECKSUMS_NAME
        sums_text = sums_path.read_text()
        check(f"grimoire-{CANONICAL_FLAVOR}-v9.9.zip" in sums_text, "SHA256SUMS lists flavor zip")
        check("grimoire-v9.9.tar.gz" in sums_text, "SHA256SUMS lists the tarball")
        check(RELEASE_JSON_NAME in sums_text, "SHA256SUMS lists release.json")
        check("RELEASE-META.json" not in sums_text, "SHA256SUMS no longer lists RELEASE-META.json")
        sums_map = {}
        for line in sums_text.strip().splitlines():
            digest, _, name = line.partition("  ")
            sums_map[name] = digest
            check(
                DistributableBuilder._sha256(b.out_dir / name) == digest,
                f"SHA256SUMS digest verifies for {name}",
            )

        # release.json: every required field present + conformant + assets[] match.
        manifest = json.loads(tarball.with_name(RELEASE_JSON_NAME).read_text())
        required = {
            "schema_version", "name", "version", "channel", "git_sha",
            "artifact_kind", "primary_artifact", "primary_artifact_sha256",
            "signature", "assets",
        }
        check(required.issubset(manifest.keys()), "release.json has every required field")
        check(manifest["schema_version"] == RELEASE_JSON_SCHEMA_VERSION, "schema_version=1")
        check(manifest["name"] == DEPENDENCY_NAME, "release.json name")
        check(manifest["version"] == "9.9", "release.json version")
        check(manifest["channel"] == "stable", "release.json channel=stable")
        check(manifest["artifact_kind"] == ARTIFACT_KIND_ASSET_BUNDLE, "release.json artifact_kind=asset-bundle")
        check(manifest["primary_artifact"] == "grimoire-v9.9.tar.gz", "primary_artifact names the tarball")
        check(manifest["signature"] is None, "release.json signature=null (signing deferred)")
        # git_sha is nullable; in this temp (non-git) tree it must be a str-or-None.
        check(manifest["git_sha"] is None or isinstance(manifest["git_sha"], str), "git_sha is str|null")
        # primary_artifact_sha256 must equal the tarball's SHA256SUMS entry.
        check(
            manifest["primary_artifact_sha256"] == sums_map["grimoire-v9.9.tar.gz"],
            "primary_artifact_sha256 == SHA256SUMS entry",
        )
        # every assets[] entry's sha256+bytes must match SHA256SUMS + on-disk size.
        for entry in manifest["assets"]:
            check(set(entry.keys()) == {"name", "sha256", "bytes"}, f"asset entry shape {entry.get('name')}")
            check(entry["sha256"] == sums_map.get(entry["name"]), f"asset sha256 matches SHA256SUMS: {entry['name']}")
            check(
                entry["bytes"] == (b.out_dir / entry["name"]).stat().st_size,
                f"asset bytes matches on-disk size: {entry['name']}",
            )
        # release.json itself is hashed in SHA256SUMS but is NOT a self-referential
        # assets[] entry (it records the others' hashes).
        check(
            all(e["name"] != RELEASE_JSON_NAME for e in manifest["assets"]),
            "release.json is not a self-referential assets[] entry",
        )

        # reproducibility: rebuild and compare bytes (zip, tarball, release.json,
        # SHA256SUMS) — byte-identical across rebuilds at a fixed commit/tree.
        b2 = DistributableBuilder(root, "9.9", root / "dist2")
        paths2 = b2.build_all()
        archive_a2 = next(p for p in paths2 if p.name == f"grimoire-{CANONICAL_FLAVOR}-v9.9.zip")
        tarball2 = next(p for p in paths2 if p.name == "grimoire-v9.9.tar.gz")
        check(archive_a.read_bytes() == archive_a2.read_bytes(), "archives byte-identical across rebuilds")
        check(tarball.read_bytes() == tarball2.read_bytes(), "canonical tarball byte-identical across rebuilds")
        check(
            tarball.with_name(RELEASE_JSON_NAME).read_bytes()
            == tarball2.with_name(RELEASE_JSON_NAME).read_bytes(),
            "release.json byte-identical across rebuilds",
        )
        check(
            (b.out_dir / CHECKSUMS_NAME).read_text() == (b2.out_dir / CHECKSUMS_NAME).read_text(),
            "SHA256SUMS byte-identical across rebuilds",
        )

        # beta channel: -beta asset + tarball suffix + prerelease-equivalent channel.
        bb = DistributableBuilder(root, "9.9", root / "dist-beta", channel="beta")
        paths_beta = bb.build_all()
        beta_names = {p.name for p in paths_beta}
        check(f"grimoire-{CANONICAL_FLAVOR}-v9.9-beta.zip" in beta_names, "beta archive suffix")
        check("grimoire-v9.9-beta.tar.gz" in beta_names, "beta tarball suffix")
        manifest_beta = json.loads((bb.out_dir / RELEASE_JSON_NAME).read_text())
        check(manifest_beta["channel"] == "beta", "release.json channel=beta")
        check(manifest_beta["primary_artifact"] == "grimoire-v9.9-beta.tar.gz", "beta primary_artifact")

        # signing degrades to None (loud) when no key is configured.
        check(bb.sign_checksums(bb.out_dir / CHECKSUMS_NAME) is None, "unsigned build returns None")

        # build_flavor rejects a non-flavor
        try:
            b.build_flavor("not-a-flavor")
            check(False, "non-flavor build should raise")
        except ValueError:
            pass

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        print(f"\n{len(failures)} self-test failure(s).", file=sys.stderr)
        return 1
    print("build_distributables self-test: all checks passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the per-flavor .zip + canonical .tar.gz + release.json distributables for a release."
    )
    ap.add_argument("--version", help="release version, e.g. 3.23 or v3.23")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--out", default="dist", help="output dir (default: dist/)")
    ap.add_argument(
        "--channel",
        default="stable",
        choices=CHANNELS,
        help="release channel (default: stable; beta => -beta asset suffix + beta release.json channel)",
    )
    ap.add_argument(
        "--flavor",
        action="append",
        default=None,
        help="build only this flavor (repeatable); default: all discovered",
    )
    ap.add_argument("--self-test", action="store_true", help="run the in-file tests")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.version:
        ap.error("--version is required (unless --self-test)")

    try:
        builder = DistributableBuilder(
            Path(args.root), args.version, Path(args.out), args.channel
        )
        # build_all emits the canonical tarball + release.json, checksums + signs
        # the whole set; an explicit --flavor list is passed through so SHA256SUMS
        # still covers exactly what was built.
        paths = builder.build_all(flavors=args.flavor)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
