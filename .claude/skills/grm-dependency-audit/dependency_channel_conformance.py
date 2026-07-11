#!/usr/bin/env python3
"""dependency_channel_conformance.py — Dependency Channel conformance gate.

Validates a repo's vendored dependencies against the Dependency Channel
contract (`docs/grimoire/design/dependency-channel-design.md` §5): every dependency must
be pulled from a release channel (not a git submodule), its vendored bytes must
match the `vendor.lock` `tree_sha256`, and its pinned release must be published
on its channel.

This is the implementation behind the **`recipe.py vendor-check`** verb
(DEP-CH-3 registers the verb; this module provides the logic it calls). Modeled
on `fleet_conformance.py` — a `ConformanceResult` finding collector, FIXTURE
constants, and a deterministic offline `--self-test`.

Five finding classes (kickoff §4; design §5 + §2b). Each finding is normalized to:

    {check, dep, channel, severity, detail, locked_sha, observed_sha}

  1. non-channel-source  — a dep is a git submodule (`.gitmodules` entry) or is
                           otherwise not sourced from a release channel.
  2. lock-bytes-mismatch — vendored bytes under `lib/third-party/<dep>/` (or the
                           legacy `vendor/<dep>/` root) lack a matching
                           lock entry, OR their recomputed `tree_sha256` differs
                           from the locked `tree_sha256`.
  3. unpublished-release — a `vendor.toml` dep's pinned release tag does not exist
                           on its channel.
  4. malformed-release   — the pinned release tag EXISTS but is not a conformant
                           producer: its artifact trio (`<artifact>.tar.gz` +
                           `release.json` + `SHA256SUMS`) is missing an asset or
                           is not self-consistent (a checksum disagrees, the
                           manifest's `primary_artifact_sha256` ≠ the tarball's
                           real hash, `artifact_kind` ≠ the pinned `kind`, a
                           `vendored-crate` tarball not having exactly one
                           top-level dir, …).
  5. producer-unpublished-release — THIS repo declares itself a channel PRODUCER
                           (a `publish.toml`), but its OWN latest release on the
                           declared channel does not carry a self-consistent
                           artifact trio (the token-bookkeeper-v0.1.0 assetless-
                           release condition, design §2b). The producer-side
                           inverse of checks 3 & 4: the same trio verification,
                           pointed at this repo's own release.

Checks 3 & 4 (consumer pins) and check 5 (this repo as producer) are the network
surface: the publish probe verifies *attachment and self-consistency of the
trio*, not merely that a tag exists. It **degrades gracefully and reports** when
the channel is unreachable (never a hard fail offline). The byte-level trio
verification is factored into the pure `evaluate_trio()` function (which also
enforces the `vendored-crate` one-top-dir shape), exercised entirely offline by
`--self-test`.

**Warn-only (advisory) this release.** The merge-gate reads the existing
`code-quality.audit-gate` dial live ({off,warn,block}); this script merely emits
findings and a process exit code (0 = conformant, nonzero = at least one
violation — the loud-exit-never-no-op contract). It never mutates the repo and
adds nothing to the config schema.

Usage:
    # Offline self-test (CI / no network):
    python3 dependency_channel_conformance.py --self-test

    # Audit a real repo (offline checks 1 & 2; checks 3 & 4 need `gh` + network):
    python3 dependency_channel_conformance.py --root /path/to/repo
    python3 dependency_channel_conformance.py --root . --offline   # skip network

Design: docs/grimoire/design/dependency-channel-design.md §5
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Optional


# ── Constants ──────────────────────────────────────────────────────────────────

#: Severity levels a finding can carry (sorted weakest → strongest).
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"

#: The check identifiers (stable keys; used in the dedupe key downstream).
CHECK_NON_CHANNEL_SOURCE = "non-channel-source"
CHECK_LOCK_BYTES_MISMATCH = "lock-bytes-mismatch"
CHECK_UNPUBLISHED_RELEASE = "unpublished-release"
CHECK_MALFORMED_RELEASE = "malformed-release"
#: Producer-side check (design §2b): this repo declares itself a channel producer
#: (a publish.toml), but its own latest release does not carry a self-consistent
#: artifact trio (the token-bookkeeper-v0.1.0 assetless-release condition).
CHECK_PRODUCER_UNPUBLISHED = "producer-unpublished-release"
#: Signing check (v3.79, dependency-channel-design.md §Signing / #318): a dep
#: pins a `pubkey` in vendor.toml (the consumer has opted into verification) but
#: its vendor.lock `signature_verified` is not `true` — either the producer
#: hasn't started signing yet ("unsigned") or a signature was present but failed
#: to verify (`false`). Always WARN — signing is a soft, producer-by-producer
#: migration; this never blocks a merge or release.
CHECK_UNSIGNED = "unsigned-dependency"

#: Sentinel used in a finding's sha slots when the value is unknown / absent.
SHA_ABSENT = None

#: Network timeout for the published-release probe (seconds).
PUBLISH_PROBE_TIMEOUT_S = 10
#: Network timeout for downloading a release asset during trio verification (seconds).
DOWNLOAD_TIMEOUT_S = 60

#: The fixed names of the two always-present trio members (design §2). The third
#: member — the primary artifact tarball — is named by the manifest / pin.
RELEASE_MANIFEST = "release.json"
CHECKSUMS_FILE = "SHA256SUMS"

#: The release.json `schema_version` this gate understands (design §2).
KNOWN_MANIFEST_SCHEMA = 1

#: The `artifact_kind` whose tarball must have exactly one top-level dir so the
#: consumer's default 1-component strip yields the crate root (design §2b).
KIND_VENDORED_CRATE = "vendored-crate"

#: Producer publish-manifest filename — its presence marks a repo as a channel
#: PRODUCER (design §2b). The producer-side check keys off this file.
PUBLISH_MANIFEST_FILE = "publish.toml"

#: Publish-verdict statuses returned by the channel probe / `evaluate_trio`.
PUBLISH_CONFORMANT = "conformant"    # tag exists + trio attached + self-consistent
PUBLISH_UNPUBLISHED = "unpublished"  # tag does not exist on the channel
PUBLISH_MALFORMED = "malformed"      # tag exists but the trio is missing / inconsistent


# ── Finding model ──────────────────────────────────────────────────────────────

class Finding:
    """One normalized conformance finding.

    Shape is fixed by the design contract (§5):
    `{check, dep, channel, severity, detail, locked_sha, observed_sha}`.
    """

    def __init__(
        self,
        check: str,
        dep: str,
        channel: Optional[str],
        severity: str,
        detail: str,
        locked_sha: Optional[str] = SHA_ABSENT,
        observed_sha: Optional[str] = SHA_ABSENT,
    ) -> None:
        self.check = check
        self.dep = dep
        self.channel = channel
        self.severity = severity
        self.detail = detail
        self.locked_sha = locked_sha
        self.observed_sha = observed_sha

    def to_dict(self) -> dict[str, Any]:
        """Return the normalized finding dict (the wire shape)."""
        return {
            "check": self.check,
            "dep": self.dep,
            "channel": self.channel,
            "severity": self.severity,
            "detail": self.detail,
            "locked_sha": self.locked_sha,
            "observed_sha": self.observed_sha,
        }

    def dedupe_key(self) -> str:
        """Stable `{channel}:{dep}:{check}` key for issue dedupe (design §5)."""
        return f"{self.channel}:{self.dep}:{self.check}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.dedupe_key()}, {self.severity})"


class ConformanceResult:
    """Accumulates findings for one conformance run.

    Mirrors `fleet_conformance.ConformanceResult` but carries structured
    `Finding` objects (the normalized §5 shape) rather than free strings, so the
    merge-gate can route each finding through `grm-feedback-to-issue`.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.findings: list[Finding] = []
        #: Non-fatal degradations (e.g. network unreachable) — reported, never
        #: counted as violations.
        self.degradations: list[str] = []

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def degrade(self, msg: str) -> None:
        """Record a graceful degradation (reported, never a violation)."""
        self.degradations.append(msg)

    @property
    def violations(self) -> list[Finding]:
        """Findings at WARN or ERROR severity (the ones that count)."""
        return [f for f in self.findings if f.severity in (SEVERITY_WARN, SEVERITY_ERROR)]

    @property
    def passed(self) -> bool:
        """True when there are no WARN/ERROR findings (degradations are OK)."""
        return len(self.violations) == 0

    def report(self) -> str:
        lines = [f"[{self.label}]"]
        if not self.findings and not self.degradations:
            lines.append("  PASS — no findings.")
        for f in self.findings:
            lines.append(
                f"  {f.severity.upper()}: [{f.check}] dep={f.dep} "
                f"channel={f.channel} — {f.detail}"
            )
            if f.locked_sha or f.observed_sha:
                lines.append(
                    f"         locked_sha={f.locked_sha} observed_sha={f.observed_sha}"
                )
        for d in self.degradations:
            lines.append(f"  DEGRADE: {d}")
        status = "PASS" if self.passed else "FAIL"
        lines.append(
            f"  → {status} ({len(self.violations)} violation(s), "
            f"{len(self.degradations)} degradation(s))"
        )
        return "\n".join(lines)


# ── tree_sha256 (design §3 two-hash model) ─────────────────────────────────────

def _sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex sha256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def tree_sha256(tree_root: str) -> str:
    """Deterministic content hash over the placed file set under *tree_root*.

    Per design §3: sorted relpaths, each paired with its file sha256, hashed
    together. Re-derivable offline from the vendored bytes; this is what
    `sync-deps --check` and the conformance gate recompute to detect drift.

    Byte-identical to `grm-sync-deps`'s `tree_sha256` (the producer of the
    `vendor.lock` `tree_sha256` this gate recomputes): symlinks and non-regular
    files are skipped; each sorted relpath contributes `"<relpath>\\0<filesha>\\n"`
    (trailing newline included). The two implementations MUST agree or every
    conformant dep would trip a false lock/bytes-drift WARN.

    Returns the `sha256:<hex>` convention string (matching `grm-component-registry`).
    """
    entries: list = []
    for dirpath, dirnames, filenames in os.walk(tree_root):
        dirnames.sort()
        for name in sorted(filenames):
            abs_path = os.path.join(dirpath, name)
            if os.path.islink(abs_path) or not os.path.isfile(abs_path):
                continue
            rel = os.path.relpath(abs_path, tree_root)
            # Normalize separators so the hash is platform-stable.
            rel = rel.replace(os.sep, "/")
            with open(abs_path, "rb") as fh:
                file_hash = _sha256_bytes(fh.read())
            entries.append((rel, file_hash))
    entries.sort()
    h = hashlib.sha256()
    for rel, file_hash in entries:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(file_hash.encode("utf-8"))
        h.update(b"\n")
    return f"sha256:{h.hexdigest()}"


# ── Publish verdict + pure trio verification ───────────────────────────────────

class PublishVerdict:
    """The outcome of probing a pinned release on its channel.

    A richer verdict than a bare bool: a release tag can EXIST yet fail to be a
    conformant producer (its artifact trio missing an asset or self-inconsistent).
    `status` is one of `PUBLISH_CONFORMANT` / `PUBLISH_UNPUBLISHED` /
    `PUBLISH_MALFORMED`; `detail` explains a non-conformant verdict; `primary_sha256`
    carries the verified bare-hex sha256 of the primary artifact when known.
    """

    def __init__(
        self,
        status: str,
        detail: str = "",
        primary_sha256: Optional[str] = None,
    ) -> None:
        self.status = status
        self.detail = detail
        self.primary_sha256 = primary_sha256

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PublishVerdict({self.status}, {self.detail!r})"


def _parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a coreutils-style `SHA256SUMS` body into `{name: lowercase-hex}`.

    Each non-empty line is `"<hex>  <name>"`; the binary-mode `*` marker on the
    name (`"<hex> *<name>"`) is tolerated and stripped. Malformed lines are
    skipped (the caller's required-entry checks surface any resulting gap).
    """
    sums: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        hex_digest, name = parts
        name = name.lstrip("*").strip()
        if name:
            sums[name] = hex_digest.lower()
    return sums


def _vendored_crate_top_dirs(tar_bytes: bytes) -> Optional[int]:
    """Return the number of distinct top-level directories in a `.tar.gz`.

    Used to assert the `vendored-crate` one-top-dir shape (design §2b): a
    conformant crate tarball has exactly one, so the consumer's default
    1-component strip yields the crate root. Reads the gzip'd tar from memory
    (no extraction, no disk touch). Returns the count, or `None` when the bytes
    are not a readable `.tar.gz` (the caller treats that as malformed).
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
            top = set()
            for member in tf.getmembers():
                # Normalize and take the first path component of each entry.
                norm = os.path.normpath(member.name)
                if norm in (".", "", os.sep) or norm.startswith(("/", "..")):
                    continue
                top.add(norm.split(os.sep)[0])
            return len(top)
    except (tarfile.TarError, OSError, EOFError):
        return None


def evaluate_trio(
    assets: dict[str, bytes],
    expected_artifact: Optional[str] = None,
    expected_kind: Optional[str] = None,
) -> PublishVerdict:
    """Decide whether the fetched release *assets* form a conformant trio.

    Pure and offline: *assets* is `{asset_name: raw_bytes}` already fetched from
    the release. This is the heart of the publish hardening — it asserts the
    `release.json` + `SHA256SUMS` + primary-artifact trio is **attached** and
    **self-consistent** (design §2), so a published-but-broken release is caught
    rather than passed. Returns a `PUBLISH_MALFORMED` verdict (with a specific
    detail) on the first contract breach, else `PUBLISH_CONFORMANT`.

    `expected_artifact` / `expected_kind` come from the consumer's `vendor.toml`
    pin (`artifact` / `kind`); when given they are cross-checked against the
    manifest so a pin pointing at the wrong asset or kind is flagged.
    """
    def malformed(detail: str) -> PublishVerdict:
        return PublishVerdict(PUBLISH_MALFORMED, detail)

    # 1. The two fixed-name members must be attached.
    if RELEASE_MANIFEST not in assets:
        return malformed(f"release manifest {RELEASE_MANIFEST!r} is not attached to the release")
    if CHECKSUMS_FILE not in assets:
        return malformed(f"checksums file {CHECKSUMS_FILE!r} is not attached to the release")

    # 2. The manifest must be valid JSON of a known schema.
    try:
        manifest = json.loads(assets[RELEASE_MANIFEST].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return malformed(f"{RELEASE_MANIFEST} is not valid JSON: {exc}")
    if not isinstance(manifest, dict):
        return malformed(f"{RELEASE_MANIFEST} is not a JSON object")
    schema = manifest.get("schema_version")
    if schema != KNOWN_MANIFEST_SCHEMA:
        return malformed(
            f"{RELEASE_MANIFEST} schema_version {schema!r} is not the understood "
            f"version {KNOWN_MANIFEST_SCHEMA}"
        )

    # 3. artifact_kind must match the pin (when the pin declares one).
    kind = manifest.get("artifact_kind")
    if expected_kind is not None and kind != expected_kind:
        return malformed(
            f"{RELEASE_MANIFEST} artifact_kind {kind!r} != pinned kind {expected_kind!r}"
        )

    # 4. The manifest must name a primary artifact, and the pin (if any) must agree.
    primary = manifest.get("primary_artifact")
    if not primary or not isinstance(primary, str):
        return malformed(f"{RELEASE_MANIFEST} does not name a primary_artifact")
    if expected_artifact is not None and primary != expected_artifact:
        return malformed(
            f"{RELEASE_MANIFEST} primary_artifact {primary!r} != pinned artifact "
            f"{expected_artifact!r}"
        )

    # 5. The primary artifact must itself be attached.
    if primary not in assets:
        return malformed(f"primary artifact {primary!r} named by the manifest is not attached")

    # 6. SHA256SUMS must list — and correctly hash — the trio members we hold.
    sums = _parse_sha256sums(assets[CHECKSUMS_FILE].decode("utf-8", errors="replace"))
    for required in (primary, RELEASE_MANIFEST):
        if required not in sums:
            return malformed(f"{CHECKSUMS_FILE} has no entry for {required!r}")
    for name, claimed_hex in sums.items():
        if name in assets:
            actual_hex = _sha256_bytes(assets[name])
            if actual_hex != claimed_hex.lower():
                return malformed(
                    f"{CHECKSUMS_FILE} hash for {name!r} ({claimed_hex[:12]}…) does not "
                    f"match the attached bytes ({actual_hex[:12]}…)"
                )

    # 7. The manifest's own claim about the primary must match the real bytes.
    primary_sha = _sha256_bytes(assets[primary])
    claimed_primary = manifest.get("primary_artifact_sha256")
    if claimed_primary != primary_sha:
        return malformed(
            f"{RELEASE_MANIFEST} primary_artifact_sha256 ({str(claimed_primary)[:12]}…) "
            f"does not match the real {primary!r} hash ({primary_sha[:12]}…)"
        )

    # 8. Kind-specific shape: a `vendored-crate` tarball MUST have exactly one
    #    top-level directory, so the consumer's default 1-component strip yields
    #    the crate root (design §2b). A crate that ships a bare root or many top
    #    dirs would round-trip wrong through VendoredCrateKind — catch it here as
    #    the producer's inverse of that consumer contract.
    if kind == KIND_VENDORED_CRATE:
        shape = _vendored_crate_top_dirs(assets[primary])
        if shape is None:
            return malformed(
                f"vendored-crate primary {primary!r} is not a readable .tar.gz"
            )
        if shape != 1:
            return malformed(
                f"vendored-crate primary {primary!r} must have exactly one top-level "
                f"directory (found {shape}); the consumer's default 1-component strip "
                f"would not yield the crate root"
            )

    return PublishVerdict(PUBLISH_CONFORMANT, "trio attached and self-consistent", primary_sha)


# ── Channel probe (the only network-dependent surface) ─────────────────────────

class ChannelProbe:
    """Resolves whether a dep's pinned release is a conformant producer.

    The base probe shells out to `gh` (the GitHub CLI). It is isolated behind
    this class so the offline `--self-test` can inject a deterministic stub
    (`StubChannelProbe`) — no network in self-test. The pure verdict logic lives
    in `evaluate_trio`; this class only adds the network fetch around it.
    """

    def latest_release_tag(self, repo: str) -> Optional[str]:
        """Return *repo*'s latest published release tag, or None if it has none.

        A second, narrower network surface alongside `verify_release` — reused
        by `architecture_diagram.py` to mark a pin "stale" (behind latest)
        without a second network client, and by the producer check (§2b) to find
        this repo's own latest release. Raises `ChannelUnreachable` when the
        channel cannot be reached (no `gh`, no network, auth failure), so the
        caller degrades gracefully rather than manufacturing a false verdict —
        the same offline-degradation contract as `verify_release`.
        """
        if shutil.which("gh") is None:
            raise ChannelUnreachable("the `gh` CLI is not installed")
        try:
            proc = subprocess.run(
                ["gh", "release", "view", "--repo", repo, "--json", "tagName"],
                capture_output=True,
                text=True,
                timeout=PUBLISH_PROBE_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ChannelUnreachable(f"`gh` invocation failed: {exc}") from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or "").lower()
            if "release not found" in stderr or "not found" in stderr:
                return None
            raise ChannelUnreachable(
                f"`gh release view` did not yield a verdict: {proc.stderr.strip()!r}"
            )
        try:
            view = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ChannelUnreachable(f"`gh release view` returned unparseable JSON: {exc}") from exc
        return view.get("tagName") if isinstance(view, dict) else None

    def verify_release(
        self,
        repo: str,
        release_tag: str,
        expected_artifact: Optional[str] = None,
        expected_kind: Optional[str] = None,
    ) -> PublishVerdict:
        """Probe *release_tag* of *repo* and verify its artifact trio.

        Verdict semantics:
          - tag absent on the channel            → `PUBLISH_UNPUBLISHED`
          - tag present, trio missing/inconsistent → `PUBLISH_MALFORMED`
          - tag present, trio attached & consistent → `PUBLISH_CONFORMANT`

        Raises `ChannelUnreachable` when the channel cannot be reached (no `gh`,
        no network, auth failure, or a download error) so the caller degrades
        gracefully rather than manufacturing a false verdict.
        """
        if shutil.which("gh") is None:
            raise ChannelUnreachable("the `gh` CLI is not installed")
        # 1. Existence + asset inventory.
        try:
            proc = subprocess.run(
                ["gh", "release", "view", release_tag, "--repo", repo,
                 "--json", "tagName,assets"],
                capture_output=True,
                text=True,
                timeout=PUBLISH_PROBE_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ChannelUnreachable(f"`gh` invocation failed: {exc}") from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or "").lower()
            # A clean "not found" means reachable-but-unpublished; anything else
            # (auth, rate-limit, network) is an unreachable degrade, not a verdict.
            if "release not found" in stderr or "not found" in stderr:
                return PublishVerdict(PUBLISH_UNPUBLISHED, f"release {release_tag!r} not found")
            raise ChannelUnreachable(
                f"`gh release view` did not yield a verdict: {proc.stderr.strip()!r}"
            )
        try:
            view = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ChannelUnreachable(f"`gh release view` returned unparseable JSON: {exc}") from exc
        asset_names = {
            a.get("name") for a in view.get("assets", []) if isinstance(a, dict) and a.get("name")
        }
        # Short-circuit: without the two fixed members we cannot even read the
        # manifest — that is a malformed (published-but-broken) release.
        if RELEASE_MANIFEST not in asset_names or CHECKSUMS_FILE not in asset_names:
            missing = [n for n in (RELEASE_MANIFEST, CHECKSUMS_FILE) if n not in asset_names]
            return PublishVerdict(
                PUBLISH_MALFORMED,
                f"release {release_tag!r} is published but missing trio asset(s): "
                f"{', '.join(missing)}",
            )
        # 2. Fetch the trio and hand the bytes to the pure verifier.
        tmp = tempfile.mkdtemp(prefix="dep-ch-verify-")
        try:
            assets: dict[str, bytes] = {}
            for name in (RELEASE_MANIFEST, CHECKSUMS_FILE):
                assets[name] = self._download_asset(repo, release_tag, name, tmp)
            # Discover the primary artifact from the manifest and fetch it too
            # (only when it is actually attached — else evaluate_trio flags it).
            try:
                manifest = json.loads(assets[RELEASE_MANIFEST].decode("utf-8"))
                primary = manifest.get("primary_artifact") if isinstance(manifest, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                primary = None
            if isinstance(primary, str) and primary in asset_names:
                assets[primary] = self._download_asset(repo, release_tag, primary, tmp)
            return evaluate_trio(assets, expected_artifact, expected_kind)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _download_asset(self, repo: str, release_tag: str, name: str, dest_dir: str) -> bytes:
        """Download a single named release asset and return its raw bytes.

        A failure here means the channel could not be read reliably, so it raises
        `ChannelUnreachable` (degrade) rather than letting a half-fetched trio
        masquerade as malformed.
        """
        try:
            proc = subprocess.run(
                ["gh", "release", "download", release_tag, "--repo", repo,
                 "--pattern", name, "--dir", dest_dir, "--clobber"],
                capture_output=True,
                text=True,
                timeout=DOWNLOAD_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ChannelUnreachable(f"downloading asset {name!r} failed: {exc}") from exc
        if proc.returncode != 0:
            raise ChannelUnreachable(
                f"downloading asset {name!r} of {release_tag!r} failed: "
                f"{proc.stderr.strip()!r}"
            )
        path = os.path.join(dest_dir, name)
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError as exc:
            raise ChannelUnreachable(f"reading downloaded asset {name!r} failed: {exc}") from exc


class ChannelUnreachable(RuntimeError):
    """The release channel could not be reached to render a publish verdict."""


# ── The checker ────────────────────────────────────────────────────────────────

class DependencyChannelConformance:
    """Runs the three Dependency Channel conformance checks over a repo root.

    Layout assumptions (design §3):
      <root>/vendor.toml             — human intent (TOML; parsed via tomllib on 3.11+).
      <root>/vendor.lock             — resolved truth (JSON).
      <root>/lib/third-party/<dep>/  — committed vendored bytes (current default).
      <root>/vendor/<dep>/           — committed vendored bytes (legacy root).
      <root>/.gitmodules             — submodule registry (a non-channel source signal).

    Vendored bytes are dual-rooted: the orphan-scan walks BOTH `lib/third-party/`
    (the current default) and the legacy `vendor/` tree, so a mid-migration repo
    that still carries a `vendor/` tree keeps auditing correctly. An absent
    `vendor.toml` `dest` defaults to `lib/third-party/<dep>`; an explicit `dest`
    (e.g. a legacy `vendor/<dep>`) is always honored verbatim.

    All findings default to WARN severity (advisory this release); the merge-gate
    decides block/warn/off via the live `code-quality.audit-gate` dial.
    """

    VENDOR_TOML = "vendor.toml"
    VENDOR_LOCK = "vendor.lock"
    # The current default vendoring root; an absent `dest` defaults under it.
    VENDOR_DIR = "lib/third-party"
    # The legacy root, still scanned so a mid-migration repo audits correctly.
    LEGACY_VENDOR_DIR = "vendor"
    GITMODULES = ".gitmodules"

    def __init__(self, root: str, probe: Optional[ChannelProbe] = None) -> None:
        self.root = root
        self.probe = probe if probe is not None else ChannelProbe()

    # ── Loaders (tolerant: a missing file is "no deps", not a crash) ────────

    def _load_manifest(self) -> dict[str, Any]:
        """Parse `vendor.toml` → its `deps` table. Empty dict when absent."""
        path = os.path.join(self.root, self.VENDOR_TOML)
        if not os.path.exists(path):
            return {}
        import tomllib  # stdlib on 3.11+ (design §3 declares the 3.11 floor)

        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        deps = data.get("deps")
        return deps if isinstance(deps, dict) else {}

    def _load_lock(self) -> dict[str, Any]:
        """Parse `vendor.lock` → its `deps` map. Empty dict when absent."""
        path = os.path.join(self.root, self.VENDOR_LOCK)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
            if not text:
                return {}
            data = json.loads(text)
        except (json.JSONDecodeError, OSError):
            return {}
        deps = data.get("deps")
        return deps if isinstance(deps, dict) else {}

    def _load_submodule_paths(self) -> set[str]:
        """Return the set of `path =` entries declared in `.gitmodules`."""
        path = os.path.join(self.root, self.GITMODULES)
        if not os.path.exists(path):
            return set()
        paths: set[str] = set()
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("path"):
                    _, _, value = stripped.partition("=")
                    value = value.strip()
                    if value:
                        paths.add(value.rstrip("/"))
        return paths

    def _vendored_roots(self) -> list[str]:
        """Vendoring roots to scan, current default first then legacy."""
        return [self.VENDOR_DIR, self.LEGACY_VENDOR_DIR]

    def _vendored_dep_dirs(self) -> dict[str, tuple[str, str]]:
        """Return {dep_name: (abs_dir, dest)} for each immediate child of every
        vendoring root (`lib/third-party/` then legacy `vendor/`).

        `dest` is the repo-relative path the dir was found at (e.g.
        `lib/third-party/aura` or `vendor/aura`) so check 2 reports and the
        submodule-skip test reflect the dir's real location. When the same dep
        name appears under both roots, the current default wins (scanned first).
        """
        result: dict[str, tuple[str, str]] = {}
        for rel_root in self._vendored_roots():
            vendor_root = os.path.join(self.root, rel_root)
            if not os.path.isdir(vendor_root):
                continue
            for name in sorted(os.listdir(vendor_root)):
                if name in result:
                    continue  # earlier root (current default) takes precedence
                abs_dir = os.path.join(vendor_root, name)
                if os.path.isdir(abs_dir):
                    result[name] = (abs_dir, f"{rel_root}/{name}")
        return result

    # ── Individual checks ───────────────────────────────────────────────────

    def check_non_channel_source(self, result: ConformanceResult) -> None:
        """Check 1 — flag a dep sourced from a submodule (non-channel)."""
        manifest = self._load_manifest()
        submodule_paths = self._load_submodule_paths()
        if not submodule_paths:
            return
        # Map each manifest dep to its declared dest (absent → lib/third-party/<dep>).
        for dep, spec in manifest.items():
            channel = spec.get("channel") if isinstance(spec, dict) else None
            dest = None
            if isinstance(spec, dict):
                dest = spec.get("dest")
            dest = (dest or f"{self.VENDOR_DIR}/{dep}").rstrip("/")
            if dest in submodule_paths:
                result.add(
                    Finding(
                        check=CHECK_NON_CHANNEL_SOURCE,
                        dep=dep,
                        channel=channel,
                        severity=SEVERITY_WARN,
                        detail=(
                            f"dep is sourced from a git submodule at {dest!r}; "
                            f"the Dependency Channel forbids `.gitmodules` deps "
                            f"(pull from a release channel via sync-deps instead)"
                        ),
                    )
                )
        # Also flag a submodule landing directly under a vendoring root
        # (lib/third-party/ or legacy vendor/) even if the manifest does not name
        # it (a stray submodule-sourced vendored dir).
        for sub_path in sorted(submodule_paths):
            norm = sub_path.replace("\\", "/")
            for rel_root in self._vendored_roots():
                prefix = f"{rel_root}/"
                if norm.startswith(prefix):
                    dep = norm[len(prefix):].split("/", 1)[0]
                    if dep not in manifest:
                        result.add(
                            Finding(
                                check=CHECK_NON_CHANNEL_SOURCE,
                                dep=dep,
                                channel=None,
                                severity=SEVERITY_WARN,
                                detail=(
                                    f"a git submodule is mounted under {prefix} at "
                                    f"{sub_path!r} but is not declared in vendor.toml; "
                                    f"vendored deps must come from a release channel"
                                ),
                            )
                        )
                    break

    def check_lock_bytes_mismatch(self, result: ConformanceResult) -> None:
        """Check 2 — flag vendored bytes with no/incorrect lock entry."""
        lock = self._load_lock()
        manifest = self._load_manifest()
        submodule_paths = self._load_submodule_paths()
        for dep, (abs_dir, dest) in self._vendored_dep_dirs().items():
            # A submodule-mounted dir is handled by check 1; do not double-flag
            # it here (it has no vendored-bytes contract).
            if dest in submodule_paths:
                continue
            spec = manifest.get(dep) if isinstance(manifest.get(dep), dict) else {}
            channel = spec.get("channel")
            observed = tree_sha256(abs_dir)
            lock_entry = lock.get(dep)
            if not isinstance(lock_entry, dict):
                result.add(
                    Finding(
                        check=CHECK_LOCK_BYTES_MISMATCH,
                        dep=dep,
                        channel=channel,
                        severity=SEVERITY_WARN,
                        detail=(
                            f"vendored bytes present under {dest}/ but vendor.lock "
                            f"has no entry for this dep (unlocked vendored dir)"
                        ),
                        locked_sha=SHA_ABSENT,
                        observed_sha=observed,
                    )
                )
                continue
            locked = lock_entry.get("tree_sha256")
            if locked != observed:
                result.add(
                    Finding(
                        check=CHECK_LOCK_BYTES_MISMATCH,
                        dep=dep,
                        channel=lock_entry.get("channel", channel),
                        severity=SEVERITY_WARN,
                        detail=(
                            f"vendored bytes under {dest}/ do not match the locked "
                            f"tree_sha256 (lock/bytes drift)"
                        ),
                        locked_sha=locked,
                        observed_sha=observed,
                    )
                )

    def check_unsigned(self, result: ConformanceResult) -> None:
        """Signing check (§Signing / #318) — WARN when a pinned pubkey isn't
        backed by a verified signature.

        Purely local/offline: reads `vendor.toml` (for the `pubkey` pin) and
        `vendor.lock` (for the `signature_verified` tri-state `grm-sync-deps`
        recorded at sync time) — no network probe, unlike checks 3/4. A dep with
        no `pubkey` pinned is untouched by this check (the consumer hasn't opted
        into verification yet, which is not itself a finding — pinning is
        optional and producer-by-producer). This is the gate's "the producer
        advertises signing" WARN the design calls for: pinning a pubkey IS the
        advertisement-consumption signal, since a consumer only pins one once a
        producer has published its public key.
        """
        lock = self._load_lock()
        for dep, spec in self._load_manifest().items():
            if not isinstance(spec, dict) or not spec.get("pubkey"):
                continue
            lock_entry = lock.get(dep)
            verified = lock_entry.get("signature_verified") if isinstance(lock_entry, dict) else None
            if verified is True:
                continue
            if verified is False:
                detail = (
                    f"pubkey pinned for {dep} but its last-synced signature "
                    f"FAILED verification (signature_verified: false) — the "
                    f"SHA256SUMS integrity floor still holds; re-sync or "
                    f"investigate the producer's signing key"
                )
            else:
                detail = (
                    f"pubkey pinned for {dep} but its last-synced release is "
                    f"unsigned (signature_verified: "
                    f"{'unsigned' if verified is None else verified!r}) — "
                    f"the producer hasn't started signing this dep's releases yet"
                )
            result.add(
                Finding(
                    check=CHECK_UNSIGNED,
                    dep=dep,
                    channel=spec.get("channel"),
                    severity=SEVERITY_WARN,
                    detail=detail,
                    locked_sha=SHA_ABSENT,
                    observed_sha=SHA_ABSENT,
                )
            )

    def check_published_release(
        self, result: ConformanceResult, offline: bool = False
    ) -> None:
        """Checks 3 & 4 — verify each pinned release is a conformant producer.

        The probe renders a three-way verdict (`evaluate_trio`): the tag may be
        unpublished (check 3), published-but-malformed — trio missing or
        self-inconsistent (check 4) — or conformant (no finding). Network-
        dependent. Degrades gracefully: when offline or the channel is
        unreachable, it records a degradation and renders no verdict (never a
        hard fail).
        """
        manifest = self._load_manifest()
        if not manifest:
            return
        if offline:
            result.degrade(
                "publish/trio check skipped (--offline): release publication and "
                "artifact conformance are not verifiable without network access"
            )
            return
        lock = self._load_lock()
        for dep, spec in manifest.items():
            if not isinstance(spec, dict):
                continue
            repo = spec.get("repo")
            channel = spec.get("channel")
            version = spec.get("version")
            if not repo or not version:
                continue
            # Prefer the lock's resolved release_tag; fall back to v<version>.
            lock_entry = lock.get(dep) if isinstance(lock.get(dep), dict) else {}
            release_tag = lock_entry.get("release_tag") or f"v{version}"
            expected_artifact = spec.get("artifact")
            expected_kind = spec.get("kind")
            try:
                verdict = self.probe.verify_release(
                    repo, release_tag, expected_artifact, expected_kind
                )
            except ChannelUnreachable as exc:
                result.degrade(
                    f"publish/trio check for dep {dep!r} degraded: {exc} "
                    f"(channel unreachable — reported, not failed)"
                )
                continue
            if verdict.status == PUBLISH_UNPUBLISHED:
                result.add(
                    Finding(
                        check=CHECK_UNPUBLISHED_RELEASE,
                        dep=dep,
                        channel=channel,
                        severity=SEVERITY_WARN,
                        detail=(
                            f"pinned release {release_tag!r} is not a published "
                            f"release of {repo!r} on channel {channel!r}"
                        ),
                    )
                )
            elif verdict.status == PUBLISH_MALFORMED:
                result.add(
                    Finding(
                        check=CHECK_MALFORMED_RELEASE,
                        dep=dep,
                        channel=channel,
                        severity=SEVERITY_WARN,
                        detail=(
                            f"pinned release {release_tag!r} of {repo!r} exists but is "
                            f"not a channel-conformant producer: {verdict.detail}"
                        ),
                        observed_sha=verdict.primary_sha256,
                    )
                )

    # ── Producer-side check (design §2b) ───────────────────────────────────────

    def _is_producer(self) -> bool:
        """True when this repo declares itself a channel PRODUCER.

        Detected by the presence of a producer publish manifest (`publish.toml`)
        at the repo root — the seam workflow-bootstrap seeds for a library crate.
        Absence means "not a producer" (the common case): the producer check is
        then a silent no-op.
        """
        return os.path.exists(os.path.join(self.root, PUBLISH_MANIFEST_FILE))

    def _load_publish_manifest(self) -> dict[str, Any]:
        """Parse `publish.toml` → its `[publish]` table. Empty dict when absent."""
        path = os.path.join(self.root, PUBLISH_MANIFEST_FILE)
        if not os.path.exists(path):
            return {}
        try:
            import tomllib  # stdlib on 3.11+ (design §3 declares the 3.11 floor)

            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, ValueError):
            return {}
        pub = data.get("publish")
        return pub if isinstance(pub, dict) else {}

    def _own_repo_slug(self) -> Optional[str]:
        """Best-effort `owner/repo` slug for THIS repo (the producer probes itself).

        Prefers the `origin` git remote; falls back to the issue-tracker `repo`
        recorded in `.claude/grimoire-config.json`. Returns None when neither is
        resolvable — the caller then degrades (never manufactures a verdict).
        """
        # 1. git remote get-url origin
        try:
            proc = subprocess.run(
                ["git", "-C", self.root, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=PUBLISH_PROBE_TIMEOUT_S,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                url = proc.stdout.strip()
                import re
                slug = re.sub(r"\.git$", "",
                              re.sub(r"^.*github\.com[:/]+", "", url)).strip("/")
                if slug and "/" in slug:
                    return slug
        except (OSError, subprocess.SubprocessError):
            pass
        # 2. grimoire-config.json issue-tracker repo
        cfg_path = os.path.join(self.root, ".claude", "grimoire-config.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            for tracker in (cfg.get("issue-tracker", {}).get("trackers") or []):
                repo = tracker.get("repo")
                if repo and "/" in repo:
                    return repo
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def check_producer_release(
        self, result: ConformanceResult, offline: bool = False
    ) -> None:
        """Producer check (§2b) — if this repo publishes for others, its OWN
        latest release on the declared channel must carry a self-consistent trio.

        Closes the token-bookkeeper-v0.1.0 gap: a producer that cut a clean tagged
        release carrying ZERO vendorable assets read as "published" only because a
        tag existed. Here `verify_release` renders the same three-way verdict used
        for consumer pins — attachment + self-consistency (+ the vendored-crate
        one-top-dir shape), not mere tag existence — but pointed at THIS repo's own
        latest release. Warn-only; degrades gracefully offline / when unreachable.
        """
        if not self._is_producer():
            return  # not a producer — silent no-op
        pub = self._load_publish_manifest()
        channel = pub.get("channel", "stable")
        expected_kind = pub.get("artifact_kind")
        if offline:
            result.degrade(
                "producer publish check skipped (--offline): a producer's own "
                "latest release is not verifiable without network access"
            )
            return
        slug = self._own_repo_slug()
        if not slug:
            result.degrade(
                "producer publish check degraded: could not resolve this repo's "
                "own owner/repo slug (no git origin, no config repo) — reported, "
                "not failed"
            )
            return
        try:
            latest_tag = self.probe.latest_release_tag(slug)
        except ChannelUnreachable as exc:
            result.degrade(
                f"producer publish check degraded: {exc} "
                f"(channel unreachable — reported, not failed)"
            )
            return
        if not latest_tag:
            result.add(
                Finding(
                    check=CHECK_PRODUCER_UNPUBLISHED,
                    dep=pub.get("name") or slug,
                    channel=channel,
                    severity=SEVERITY_WARN,
                    detail=(
                        f"this repo is a channel producer ({PUBLISH_MANIFEST_FILE} "
                        f"present) but {slug!r} has no published release to carry an "
                        f"artifact trio"
                    ),
                )
            )
            return
        try:
            verdict = self.probe.verify_release(
                slug, latest_tag, expected_kind=expected_kind
            )
        except ChannelUnreachable as exc:
            result.degrade(
                f"producer publish check for {slug!r} degraded: {exc} "
                f"(channel unreachable — reported, not failed)"
            )
            return
        if verdict.status != PUBLISH_CONFORMANT:
            result.add(
                Finding(
                    check=CHECK_PRODUCER_UNPUBLISHED,
                    dep=pub.get("name") or slug,
                    channel=channel,
                    severity=SEVERITY_WARN,
                    detail=(
                        f"this repo is a channel producer but its latest release "
                        f"{latest_tag!r} of {slug!r} is not vendorable: {verdict.detail}"
                    ),
                    observed_sha=verdict.primary_sha256,
                )
            )

    # ── Orchestration ────────────────────────────────────────────────────────

    def run(self, label: str = "vendor-check", offline: bool = False) -> ConformanceResult:
        """Run all checks (consumer 1-4 + §Signing + the producer §2b check)."""
        result = ConformanceResult(label)
        self.check_non_channel_source(result)
        self.check_lock_bytes_mismatch(result)
        self.check_unsigned(result)
        self.check_published_release(result, offline=offline)
        self.check_producer_release(result, offline=offline)
        return result


# ── Offline self-test fixtures ─────────────────────────────────────────────────

class StubChannelProbe(ChannelProbe):
    """Offline probe stub for `--self-test`.

    Drives every publish verdict deterministically with no network:
      - a tag in `conformant`   → `PUBLISH_CONFORMANT`
      - a tag in `malformed`    → `PUBLISH_MALFORMED` (detail from the map value)
      - a tag in `unreachable`  → raises ChannelUnreachable (degrade path)
      - any other tag           → `PUBLISH_UNPUBLISHED`
    """

    def __init__(
        self,
        conformant: Optional[set[str]] = None,
        malformed: Optional[dict[str, str]] = None,
        unreachable: Optional[set[str]] = None,
        latest: Optional[dict[str, Optional[str]]] = None,
    ) -> None:
        self.conformant = conformant or set()
        self.malformed = dict(malformed or {})
        self.unreachable = unreachable or set()
        # `latest` maps a repo slug -> its latest tag (or None for "no release"),
        # driving the producer check's self-probe deterministically offline.
        self.latest = dict(latest or {})

    def latest_release_tag(self, repo: str) -> Optional[str]:
        if repo in self.unreachable:
            raise ChannelUnreachable(f"stub: {repo} unreachable")
        return self.latest.get(repo)

    def verify_release(
        self,
        repo: str,
        release_tag: str,
        expected_artifact: Optional[str] = None,
        expected_kind: Optional[str] = None,
    ) -> PublishVerdict:
        if release_tag in self.unreachable:
            raise ChannelUnreachable(f"stub: {release_tag} unreachable")
        if release_tag in self.malformed:
            return PublishVerdict(PUBLISH_MALFORMED, self.malformed[release_tag])
        if release_tag in self.conformant:
            return PublishVerdict(
                PUBLISH_CONFORMANT, "stub: conformant", primary_sha256="00" * 32
            )
        return PublishVerdict(PUBLISH_UNPUBLISHED, f"stub: {release_tag} unpublished")


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _build_conformant_repo(root: str) -> None:
    """A fully conformant repo: one channel dep, locked, bytes match.

    Exercises the current default root (`lib/third-party/`) with NO explicit
    `dest` in vendor.toml, so the absent-dest default is what's audited.
    """
    dep_dir = os.path.join(root, "lib", "third-party", "aura")
    _write(os.path.join(dep_dir, "css", "aura.css"), "body{color:#000}\n")
    _write(os.path.join(dep_dir, "README.md"), "Aura bundle\n")
    observed = tree_sha256(dep_dir)
    _write(
        os.path.join(root, "vendor.toml"),
        'schema_version = 1\n\n'
        '[deps.aura]\n'
        'repo = "rhohn94/design-language"\n'
        'channel = "stable"\n'
        'version = "3.20.0"\n'
        'artifact = "aura-v3.20.0.tar.gz"\n'
        'kind = "asset-bundle"\n',
    )
    lock = {
        "schema_version": 1,
        "deps": {
            "aura": {
                "version": "3.20.0",
                "channel": "stable",
                "release_tag": "v3.20.0",
                "tree_sha256": observed,
            }
        },
    }
    _write(os.path.join(root, "vendor.lock"), json.dumps(lock, indent=2) + "\n")


def _build_submodule_repo(root: str) -> None:
    """A repo whose vendored dep is sourced from a git submodule (check 1).

    Deliberately uses the LEGACY `vendor/` root with an explicit `dest` to keep
    legacy-root detection covered under dual-rooting (a mid-migration repo).
    """
    dep_dir = os.path.join(root, "vendor", "aura")
    _write(os.path.join(dep_dir, "css", "aura.css"), "body{color:#000}\n")
    _write(
        os.path.join(root, "vendor.toml"),
        'schema_version = 1\n\n'
        '[deps.aura]\n'
        'repo = "rhohn94/design-language"\n'
        'channel = "stable"\n'
        'version = "3.20.0"\n'
        'dest = "vendor/aura"\n'
        'kind = "asset-bundle"\n',
    )
    _write(
        os.path.join(root, ".gitmodules"),
        '[submodule "vendor/aura"]\n'
        '\tpath = vendor/aura\n'
        '\turl = https://github.com/rhohn94/design-language.git\n',
    )
    _write(os.path.join(root, "vendor.lock"), json.dumps({"schema_version": 1, "deps": {}}) + "\n")


def _build_unlocked_repo(root: str) -> None:
    """A repo with vendored bytes but no lock entry (check 2: unlocked dir).

    Bytes under the default `lib/third-party/` root with NO explicit `dest`, so
    the default-root orphan scan is what surfaces the unlocked dir.
    """
    dep_dir = os.path.join(root, "lib", "third-party", "ollama")
    _write(os.path.join(dep_dir, "bin", "ollama"), "#!/bin/sh\n")
    _write(
        os.path.join(root, "vendor.toml"),
        'schema_version = 1\n\n'
        '[deps.ollama]\n'
        'repo = "ollama/ollama"\n'
        'channel = "stable"\n'
        'version = "0.3.12"\n'
        'kind = "asset-bundle"\n',
    )
    _write(os.path.join(root, "vendor.lock"), json.dumps({"schema_version": 1, "deps": {}}) + "\n")


def _build_drifted_repo(root: str) -> None:
    """A repo whose vendored bytes drifted from the locked tree_sha256 (check 2).

    Bytes under the default `lib/third-party/` root with NO explicit `dest`.
    """
    dep_dir = os.path.join(root, "lib", "third-party", "aura")
    _write(os.path.join(dep_dir, "css", "aura.css"), "body{color:#FFF}\n")  # changed bytes
    _write(
        os.path.join(root, "vendor.toml"),
        'schema_version = 1\n\n'
        '[deps.aura]\n'
        'repo = "rhohn94/design-language"\n'
        'channel = "stable"\n'
        'version = "3.20.0"\n'
        'kind = "asset-bundle"\n',
    )
    lock = {
        "schema_version": 1,
        "deps": {
            "aura": {
                "version": "3.20.0",
                "channel": "stable",
                "release_tag": "v3.20.0",
                "tree_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            }
        },
    }
    _write(os.path.join(root, "vendor.lock"), json.dumps(lock, indent=2) + "\n")


def _build_unsigned_repo(root: str, signature_verified) -> None:
    """A repo with a `pubkey` pinned for its dep, at the given signature state.

    `signature_verified` is the exact `vendor.lock` value to seed — `True`
    (verified — no finding), `False` (verify failed), or `"unsigned"` (producer
    hasn't signed yet) — exercising `check_unsigned` (§Signing / #318).
    """
    dep_dir = os.path.join(root, "lib", "third-party", "aura")
    _write(os.path.join(dep_dir, "css", "aura.css"), "body{color:#000}\n")
    observed = tree_sha256(dep_dir)
    _write(
        os.path.join(root, "vendor.toml"),
        'schema_version = 1\n\n'
        '[deps.aura]\n'
        'repo = "rhohn94/design-language"\n'
        'channel = "stable"\n'
        'version = "3.20.0"\n'
        'artifact = "aura-v3.20.0.tar.gz"\n'
        'kind = "asset-bundle"\n'
        'pubkey = "untrusted comment: test\\nRWQBAgMEBQYHCA=="\n',
    )
    lock = {
        "schema_version": 1,
        "deps": {
            "aura": {
                "version": "3.20.0",
                "channel": "stable",
                "release_tag": "v3.20.0",
                "tree_sha256": observed,
                "signature_verified": signature_verified,
            }
        },
    }
    _write(os.path.join(root, "vendor.lock"), json.dumps(lock, indent=2) + "\n")


def _make_trio(
    artifact: str = "aura-v3.20.0.tar.gz",
    kind: str = "asset-bundle",
    tar_bytes: bytes = b"fake tarball bytes\n",
) -> dict[str, bytes]:
    """Build a fully conformant in-memory trio for `evaluate_trio` self-tests.

    Returns `{artifact: bytes, "release.json": bytes, "SHA256SUMS": bytes}` with
    a manifest and checksums that are mutually consistent. Tests mutate a copy to
    synthesize each malformed shape.
    """
    tar_sha = _sha256_bytes(tar_bytes)
    manifest = {
        "schema_version": KNOWN_MANIFEST_SCHEMA,
        "artifact_kind": kind,
        "name": "aura",
        "version": "3.20.0",
        "channel": "stable",
        "primary_artifact": artifact,
        "primary_artifact_sha256": tar_sha,
        "assets": [{"name": artifact, "bytes": len(tar_bytes), "sha256": tar_sha}],
        "signature": None,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_sha = _sha256_bytes(manifest_bytes)
    sums_bytes = (
        f"{tar_sha}  {artifact}\n{manifest_sha}  {RELEASE_MANIFEST}\n"
    ).encode("utf-8")
    return {
        artifact: tar_bytes,
        RELEASE_MANIFEST: manifest_bytes,
        CHECKSUMS_FILE: sums_bytes,
    }


def _build_producer_repo(root: str, slug: str = "acme/mycrate") -> None:
    """A repo that declares itself a channel producer (publish.toml present).

    Seeds publish.toml + a grimoire-config.json carrying the repo slug, so the
    producer check's `_own_repo_slug` resolves it (a temp dir has no git origin,
    so the config-repo fallback is exercised). No vendor.toml, so only the
    producer check fires.
    """
    _write(
        os.path.join(root, "publish.toml"),
        '[publish]\n'
        'name = "mycrate"\n'
        'artifact_kind = "vendored-crate"\n'
        'channel = "stable"\n',
    )
    _write(
        os.path.join(root, ".claude", "grimoire-config.json"),
        json.dumps({
            "issue-tracker": {"trackers": [{"name": "default", "repo": slug}]}
        }) + "\n",
    )


def _make_crate_tarball_bytes(top_dirs: int) -> bytes:
    """Build an in-memory `.tar.gz` with `top_dirs` distinct top-level dirs.

    Feeds the `vendored-crate` one-top-dir shape checks in `evaluate_trio`
    self-tests: `top_dirs == 1` is conformant; anything else must be flagged.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        for i in range(max(1, top_dirs)):
            name = f"crate{i}/src/lib.rs" if top_dirs != 1 else "mycrate/src/lib.rs"
            data = b"pub fn x() {}\n"
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _make_crate_trio(top_dirs: int = 1) -> dict[str, bytes]:
    """A self-consistent `vendored-crate` trio whose tarball has `top_dirs` tops."""
    tar_bytes = _make_crate_tarball_bytes(top_dirs)
    return _make_trio(
        artifact="mycrate-v0.1.0.tar.gz", kind=KIND_VENDORED_CRATE,
        tar_bytes=tar_bytes,
    )


def run_self_test() -> int:
    """Run conformance checks against built-in offline fixtures. Returns exit code."""
    failures: list[str] = []

    def expect_pass(result: ConformanceResult) -> None:
        if not result.passed:
            failures.append(f"UNEXPECTED FAIL — {result.label}:\n{result.report()}")
        else:
            print(f"  OK: {result.label}")

    def expect_finding(
        result: ConformanceResult, check: str, min_count: int = 1
    ) -> None:
        hits = [f for f in result.violations if f.check == check]
        if len(hits) < min_count:
            failures.append(
                f"UNEXPECTED — {result.label}: expected >= {min_count} "
                f"{check!r} finding(s), got {len(hits)}:\n{result.report()}"
            )
        else:
            print(f"  OK (expected {check}): {result.label} — {len(hits)} finding(s)")

    def expect_degrade(result: ConformanceResult, min_count: int = 1) -> None:
        if len(result.degradations) < min_count:
            failures.append(
                f"UNEXPECTED — {result.label}: expected >= {min_count} "
                f"degradation(s), got {len(result.degradations)}:\n{result.report()}"
            )
        else:
            print(f"  OK (expected degrade): {result.label} — "
                  f"{len(result.degradations)} degradation(s)")

    print("dependency_channel_conformance.py --self-test")
    print()

    tmp = tempfile.mkdtemp(prefix="dep-ch-conformance-")
    try:
        # ── tree_sha256 determinism ──────────────────────────────────────────
        print("Group: tree_sha256 determinism")
        d1 = os.path.join(tmp, "tree-a")
        _build_conformant_repo(d1)
        h_a = tree_sha256(os.path.join(d1, "lib", "third-party", "aura"))
        h_b = tree_sha256(os.path.join(d1, "lib", "third-party", "aura"))
        if h_a != h_b or not h_a.startswith("sha256:"):
            failures.append(f"tree_sha256 not deterministic / malformed: {h_a} vs {h_b}")
        else:
            print(f"  OK: tree_sha256 stable across two runs ({h_a[:18]}…)")

        # ── evaluate_trio: pure offline trio verification ────────────────────
        print("\nGroup: evaluate_trio (pure trio verification)")

        def expect_verdict(
            label: str, verdict: PublishVerdict, status: str
        ) -> None:
            if verdict.status != status:
                failures.append(
                    f"evaluate_trio[{label}]: expected {status!r}, got "
                    f"{verdict.status!r} ({verdict.detail})"
                )
            else:
                print(f"  OK: {label} → {status}")

        # (a) A well-formed trio is conformant, and surfaces the primary sha.
        good = _make_trio()
        v_good = evaluate_trio(good, expected_artifact="aura-v3.20.0.tar.gz",
                               expected_kind="asset-bundle")
        expect_verdict("conformant", v_good, PUBLISH_CONFORMANT)
        if v_good.primary_sha256 != _sha256_bytes(good["aura-v3.20.0.tar.gz"]):
            failures.append("evaluate_trio[conformant]: primary_sha256 not surfaced correctly")

        # (b) Missing SHA256SUMS → malformed.
        no_sums = dict(_make_trio()); no_sums.pop(CHECKSUMS_FILE)
        expect_verdict("missing-SHA256SUMS", evaluate_trio(no_sums), PUBLISH_MALFORMED)

        # (c) Missing release.json → malformed.
        no_manifest = dict(_make_trio()); no_manifest.pop(RELEASE_MANIFEST)
        expect_verdict("missing-release.json", evaluate_trio(no_manifest), PUBLISH_MALFORMED)

        # (d) Primary artifact not attached → malformed.
        no_primary = dict(_make_trio()); no_primary.pop("aura-v3.20.0.tar.gz")
        expect_verdict("missing-primary", evaluate_trio(no_primary), PUBLISH_MALFORMED)

        # (e) Tarball bytes drift from the SHA256SUMS entry → malformed.
        bad_bytes = dict(_make_trio()); bad_bytes["aura-v3.20.0.tar.gz"] = b"tampered\n"
        expect_verdict("checksum-mismatch", evaluate_trio(bad_bytes), PUBLISH_MALFORMED)

        # (f) Manifest's primary_artifact_sha256 lies about the tarball → malformed.
        #     (Rebuild SHA256SUMS so only the manifest's self-claim is wrong.)
        lying = _make_trio()
        m = json.loads(lying[RELEASE_MANIFEST].decode())
        m["primary_artifact_sha256"] = "ff" * 32
        lying[RELEASE_MANIFEST] = (json.dumps(m, indent=2, sort_keys=True) + "\n").encode()
        man_sha = _sha256_bytes(lying[RELEASE_MANIFEST])
        tar_sha = _sha256_bytes(lying["aura-v3.20.0.tar.gz"])
        lying[CHECKSUMS_FILE] = (
            f"{tar_sha}  aura-v3.20.0.tar.gz\n{man_sha}  {RELEASE_MANIFEST}\n".encode()
        )
        expect_verdict("manifest-sha-lie", evaluate_trio(lying), PUBLISH_MALFORMED)

        # (g) artifact_kind disagrees with the pinned kind → malformed.
        v_kind = evaluate_trio(_make_trio(kind="asset-bundle"),
                               expected_kind="vendored-crate")
        expect_verdict("kind-mismatch", v_kind, PUBLISH_MALFORMED)

        # (h) Pinned artifact name disagrees with the manifest → malformed.
        v_name = evaluate_trio(_make_trio(), expected_artifact="other-v9.tar.gz")
        expect_verdict("artifact-name-mismatch", v_name, PUBLISH_MALFORMED)

        # (i) release.json not valid JSON → malformed.
        bad_json = dict(_make_trio()); bad_json[RELEASE_MANIFEST] = b"{not json"
        expect_verdict("bad-json", evaluate_trio(bad_json), PUBLISH_MALFORMED)

        # (j) Unknown schema_version → malformed.
        bad_schema = _make_trio()
        ms = json.loads(bad_schema[RELEASE_MANIFEST].decode()); ms["schema_version"] = 99
        bad_schema[RELEASE_MANIFEST] = (json.dumps(ms, sort_keys=True) + "\n").encode()
        # Re-sum so only schema is wrong (checksums stay self-consistent).
        man_sha = _sha256_bytes(bad_schema[RELEASE_MANIFEST])
        tar_sha = _sha256_bytes(bad_schema["aura-v3.20.0.tar.gz"])
        bad_schema[CHECKSUMS_FILE] = (
            f"{tar_sha}  aura-v3.20.0.tar.gz\n{man_sha}  {RELEASE_MANIFEST}\n".encode()
        )
        expect_verdict("unknown-schema", evaluate_trio(bad_schema), PUBLISH_MALFORMED)

        # (k) vendored-crate with exactly one top-level dir → conformant.
        expect_verdict("crate-one-top-dir",
                       evaluate_trio(_make_crate_trio(1), expected_kind=KIND_VENDORED_CRATE),
                       PUBLISH_CONFORMANT)

        # (l) vendored-crate with MANY top-level dirs → malformed (1-strip would
        #     not yield the crate root). This is the producer inverse of
        #     VendoredCrateKind's default 1-component strip.
        expect_verdict("crate-many-top-dirs",
                       evaluate_trio(_make_crate_trio(3), expected_kind=KIND_VENDORED_CRATE),
                       PUBLISH_MALFORMED)

        # (m) An asset-bundle with many top dirs is fine — the one-top-dir rule is
        #     vendored-crate-specific, not applied to asset-bundle.
        multi_bundle = _make_trio(kind="asset-bundle",
                                  tar_bytes=_make_crate_tarball_bytes(3))
        expect_verdict("asset-bundle-many-top-dirs-ok",
                       evaluate_trio(multi_bundle), PUBLISH_CONFORMANT)

        # ── Conformant repo: no violations ───────────────────────────────────
        print("\nGroup: conformant repo (no violations)")
        probe_ok = StubChannelProbe(conformant={"v3.20.0"})
        expect_pass(
            DependencyChannelConformance(d1, probe=probe_ok).run(
                "conformant", offline=False
            )
        )

        # ── Check 1: submodule-sourced dep → WARN, no block ──────────────────
        print("\nGroup: check 1 — non-channel source (submodule)")
        d_sub = os.path.join(tmp, "submodule")
        _build_submodule_repo(d_sub)
        r_sub = DependencyChannelConformance(d_sub, probe=probe_ok).run(
            "submodule-dep", offline=True
        )
        expect_finding(r_sub, CHECK_NON_CHANNEL_SOURCE)
        if any(f.severity == SEVERITY_ERROR for f in r_sub.violations):
            failures.append("submodule finding must be WARN, not ERROR (warn-only release)")
        else:
            print("  OK: submodule finding is WARN severity (advisory, non-blocking)")

        # ── Check 2: unlocked vendored dir → WARN ────────────────────────────
        print("\nGroup: check 2 — unlocked vendored dir")
        d_unlocked = os.path.join(tmp, "unlocked")
        _build_unlocked_repo(d_unlocked)
        r_unlocked = DependencyChannelConformance(d_unlocked, probe=probe_ok).run(
            "unlocked-dir", offline=True
        )
        expect_finding(r_unlocked, CHECK_LOCK_BYTES_MISMATCH)

        # ── Check 2: drifted bytes vs lock → WARN ────────────────────────────
        print("\nGroup: check 2 — lock/bytes drift")
        d_drift = os.path.join(tmp, "drift")
        _build_drifted_repo(d_drift)
        r_drift = DependencyChannelConformance(d_drift, probe=probe_ok).run(
            "lock-bytes-drift", offline=True
        )
        expect_finding(r_drift, CHECK_LOCK_BYTES_MISMATCH)
        drift_hit = next(
            f for f in r_drift.violations if f.check == CHECK_LOCK_BYTES_MISMATCH
        )
        if drift_hit.locked_sha == drift_hit.observed_sha:
            failures.append("drift finding must carry distinct locked/observed shas")
        else:
            print("  OK: drift finding carries distinct locked_sha / observed_sha")

        # ── Signing check: pinned pubkey + verified → no finding ─────────────
        print("\nGroup: signing — verified signature (no finding)")
        d_sig_ok = os.path.join(tmp, "sig-ok")
        _build_unsigned_repo(d_sig_ok, signature_verified=True)
        r_sig_ok = DependencyChannelConformance(d_sig_ok, probe=probe_ok).run(
            "sig-verified", offline=True
        )
        if any(f.check == CHECK_UNSIGNED for f in r_sig_ok.violations):
            failures.append("verified signature must NOT raise CHECK_UNSIGNED")
        else:
            print("  OK: verified signature raises no signing finding")

        # ── Signing check: pinned pubkey + producer hasn't signed → WARN ─────
        print("\nGroup: signing — unsigned (producer hasn't signed yet)")
        d_sig_unsigned = os.path.join(tmp, "sig-unsigned")
        _build_unsigned_repo(d_sig_unsigned, signature_verified="unsigned")
        r_sig_unsigned = DependencyChannelConformance(d_sig_unsigned, probe=probe_ok).run(
            "sig-unsigned", offline=True
        )
        expect_finding(r_sig_unsigned, CHECK_UNSIGNED)
        if any(f.severity == SEVERITY_ERROR for f in r_sig_unsigned.violations):
            failures.append("unsigned-dependency finding must be WARN, not ERROR")
        else:
            print("  OK: unsigned-dependency finding is WARN severity (soft-fail)")

        # ── Signing check: pinned pubkey + verification FAILED → WARN ────────
        print("\nGroup: signing — verification failed")
        d_sig_bad = os.path.join(tmp, "sig-bad")
        _build_unsigned_repo(d_sig_bad, signature_verified=False)
        r_sig_bad = DependencyChannelConformance(d_sig_bad, probe=probe_ok).run(
            "sig-bad", offline=True
        )
        expect_finding(r_sig_bad, CHECK_UNSIGNED)

        # ── Signing check: no pubkey pinned at all → no finding ──────────────
        print("\nGroup: signing — no pubkey pinned (untouched by this check)")
        r_no_pubkey = DependencyChannelConformance(d1, probe=probe_ok).run(
            "no-pubkey", offline=True
        )
        if any(f.check == CHECK_UNSIGNED for f in r_no_pubkey.violations):
            failures.append("a dep with no pubkey pinned must not raise CHECK_UNSIGNED")
        else:
            print("  OK: no pubkey pinned => no signing finding")

        # ── Check 3: unpublished release → WARN (with network stub) ──────────
        print("\nGroup: check 3 — unpublished release (stubbed network)")
        d_unpub = os.path.join(tmp, "unpub")
        _build_conformant_repo(d_unpub)
        probe_unpub = StubChannelProbe()  # v3.20.0 in no set → unpublished
        r_unpub = DependencyChannelConformance(d_unpub, probe=probe_unpub).run(
            "unpublished-release", offline=False
        )
        expect_finding(r_unpub, CHECK_UNPUBLISHED_RELEASE)

        # ── Check 4: published-but-malformed release → WARN (distinct check) ──
        print("\nGroup: check 4 — malformed release (published, broken trio)")
        d_malformed = os.path.join(tmp, "malformed")
        _build_conformant_repo(d_malformed)
        probe_malformed = StubChannelProbe(
            malformed={"v3.20.0": "SHA256SUMS hash for the tarball does not match"}
        )
        r_malformed = DependencyChannelConformance(d_malformed, probe=probe_malformed).run(
            "malformed-release", offline=False
        )
        expect_finding(r_malformed, CHECK_MALFORMED_RELEASE)
        # A malformed release must NOT be miscounted as merely unpublished.
        if any(f.check == CHECK_UNPUBLISHED_RELEASE for f in r_malformed.violations):
            failures.append("malformed release must not also raise unpublished-release")
        else:
            print("  OK: malformed release is distinct from unpublished-release")

        # ── Check 3: offline degrades gracefully (no hard fail) ──────────────
        print("\nGroup: check 3 — offline graceful degrade")
        d_off = os.path.join(tmp, "offline")
        _build_conformant_repo(d_off)
        r_off = DependencyChannelConformance(d_off, probe=probe_ok).run(
            "offline-degrade", offline=True
        )
        expect_pass(r_off)  # offline must not manufacture a violation
        expect_degrade(r_off)

        # ── Check 3: unreachable channel degrades (not "unpublished") ────────
        print("\nGroup: check 3 — unreachable channel degrade")
        d_unreach = os.path.join(tmp, "unreach")
        _build_conformant_repo(d_unreach)
        probe_unreach = StubChannelProbe(unreachable={"v3.20.0"})
        r_unreach = DependencyChannelConformance(d_unreach, probe=probe_unreach).run(
            "unreachable-degrade", offline=False
        )
        expect_pass(r_unreach)  # unreachable ≠ unpublished
        expect_degrade(r_unreach)

        # ── Finding shape contract ───────────────────────────────────────────
        print("\nGroup: finding shape contract")
        sample = r_sub.violations[0].to_dict()
        expected_keys = {
            "check", "dep", "channel", "severity", "detail",
            "locked_sha", "observed_sha",
        }
        if set(sample.keys()) != expected_keys:
            failures.append(
                f"finding shape mismatch: got {sorted(sample.keys())}, "
                f"expected {sorted(expected_keys)}"
            )
        else:
            print("  OK: finding shape matches {check,dep,channel,severity,"
                  "detail,locked_sha,observed_sha}")

        # ── Producer check (§2b): this repo publishes for others ─────────────
        print("\nGroup: producer check — own release must carry a trio")

        # (a) Not a producer (no publish.toml) → the producer check is a no-op.
        d_np = os.path.join(tmp, "not-producer")
        _build_conformant_repo(d_np)  # consumer-only repo, no publish.toml
        r_np = DependencyChannelConformance(d_np, probe=StubChannelProbe(
            conformant={"v3.20.0"})).run("not-a-producer", offline=False)
        if any(f.check == CHECK_PRODUCER_UNPUBLISHED for f in r_np.findings):
            failures.append("non-producer repo must not raise a producer finding")
        else:
            print("  OK: non-producer repo raises no producer finding")

        # (b) Producer whose repo has NO release at all → WARN
        #     (the token-bookkeeper assetless condition: nothing to vendor).
        d_prod_none = os.path.join(tmp, "producer-no-release")
        _build_producer_repo(d_prod_none, slug="acme/mycrate")
        probe_none = StubChannelProbe(latest={"acme/mycrate": None})
        r_prod_none = DependencyChannelConformance(
            d_prod_none, probe=probe_none).run("producer-no-release", offline=False)
        expect_finding(r_prod_none, CHECK_PRODUCER_UNPUBLISHED)

        # (c) Producer whose latest release is malformed (no trio) → WARN.
        d_prod_bad = os.path.join(tmp, "producer-malformed")
        _build_producer_repo(d_prod_bad, slug="acme/mycrate")
        probe_bad = StubChannelProbe(
            latest={"acme/mycrate": "v0.1.0"},
            malformed={"v0.1.0": "release carries no artifact trio (assets: [])"})
        r_prod_bad = DependencyChannelConformance(
            d_prod_bad, probe=probe_bad).run("producer-malformed", offline=False)
        expect_finding(r_prod_bad, CHECK_PRODUCER_UNPUBLISHED)

        # (d) Producer whose latest release is conformant → no producer finding.
        d_prod_ok = os.path.join(tmp, "producer-ok")
        _build_producer_repo(d_prod_ok, slug="acme/mycrate")
        probe_prod_ok = StubChannelProbe(
            latest={"acme/mycrate": "v0.1.0"}, conformant={"v0.1.0"})
        r_prod_ok = DependencyChannelConformance(
            d_prod_ok, probe=probe_prod_ok).run("producer-ok", offline=False)
        if any(f.check == CHECK_PRODUCER_UNPUBLISHED for f in r_prod_ok.violations):
            failures.append("conformant producer must not raise a producer finding")
        else:
            print("  OK: conformant producer raises no producer finding")

        # (e) Producer check degrades gracefully offline (no hard fail).
        d_prod_off = os.path.join(tmp, "producer-offline")
        _build_producer_repo(d_prod_off, slug="acme/mycrate")
        r_prod_off = DependencyChannelConformance(
            d_prod_off, probe=probe_prod_ok).run("producer-offline", offline=True)
        expect_pass(r_prod_off)
        expect_degrade(r_prod_off)

        # ── Empty repo (no vendor files) is a clean pass ─────────────────────
        print("\nGroup: empty repo (no vendor artifacts)")
        d_empty = os.path.join(tmp, "empty")
        os.makedirs(d_empty, exist_ok=True)
        expect_pass(
            DependencyChannelConformance(d_empty, probe=probe_ok).run(
                "empty-repo", offline=False
            )
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} unexpected result(s):")
        for f in failures:
            print(f"\n{f}")
        return 1
    print("SELF-TEST PASSED.")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────────

def run_audit(root: str, offline: bool, as_json: bool) -> int:
    """Audit a real repo root. Returns 0 = conformant, nonzero = violation."""
    checker = DependencyChannelConformance(root)
    result = checker.run(label="vendor-check", offline=offline)
    if as_json:
        print(json.dumps(
            {
                "label": result.label,
                "passed": result.passed,
                "findings": [f.to_dict() for f in result.findings],
                "degradations": result.degradations,
            },
            indent=2,
        ))
    else:
        print(result.report())
    return 0 if result.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dependency Channel conformance gate (the `vendor-check` verb's "
            "implementation). See docs/grimoire/design/dependency-channel-design.md §5."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="Run against built-in offline fixtures (no network calls).",
    )
    mode.add_argument(
        "--root",
        metavar="DIR",
        help="Repo root to audit (expects vendor.toml / vendor.lock / "
             "lib/third-party/ — legacy vendor/ also scanned).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the network publish check (checks 1 & 2 still run).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit findings as JSON (for the merge-gate / recipe consumer).",
    )
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    return run_audit(args.root, offline=args.offline, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
