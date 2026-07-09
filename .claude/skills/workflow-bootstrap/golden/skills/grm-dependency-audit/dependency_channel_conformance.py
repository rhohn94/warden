#!/usr/bin/env python3
"""dependency_channel_conformance.py — Dependency Channel conformance gate.

Validates a repo's vendored dependencies against the Dependency Channel
contract (`docs/design/dependency-channel-design.md` §5): every dependency must
be pulled from a release channel (not a git submodule), its vendored bytes must
match the `vendor.lock` `tree_sha256`, and its pinned release must be published
on its channel.

This is the implementation behind the **`recipe.py vendor-check`** verb
(DEP-CH-3 registers the verb; this module provides the logic it calls). Modeled
on `fleet_conformance.py` — a `ConformanceResult` finding collector, FIXTURE
constants, and a deterministic offline `--self-test`.

Three checks (kickoff §4; design §5). Each finding is normalized to:

    {check, dep, channel, severity, detail, locked_sha, observed_sha}

  1. non-channel-source  — a dep is a git submodule (`.gitmodules` entry) or is
                           otherwise not sourced from a release channel.
  2. lock-bytes-mismatch — vendored bytes under `vendor/<dep>/` lack a matching
                           lock entry, OR their recomputed `tree_sha256` differs
                           from the locked `tree_sha256`.
  3. unpublished-release — a `vendor.toml` dep is not a published release on its
                           channel (the only network-dependent check; it
                           **degrades gracefully and reports** when the channel
                           is unreachable — never a hard fail offline).

**Warn-only (advisory) this release.** The merge-gate reads the existing
`code-quality.audit-gate` dial live ({off,warn,block}); this script merely emits
findings and a process exit code (0 = conformant, nonzero = at least one
violation — the loud-exit-never-no-op contract). It never mutates the repo and
adds nothing to the config schema.

Usage:
    # Offline self-test (CI / no network):
    python3 dependency_channel_conformance.py --self-test

    # Audit a real repo (offline checks 1 & 2; check 3 needs `gh`):
    python3 dependency_channel_conformance.py --root /path/to/repo
    python3 dependency_channel_conformance.py --root . --offline   # skip network

Design: docs/design/dependency-channel-design.md §5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Optional


# ── Constants ──────────────────────────────────────────────────────────────────

#: Severity levels a finding can carry (sorted weakest → strongest).
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"

#: The three check identifiers (stable keys; used in the dedupe key downstream).
CHECK_NON_CHANNEL_SOURCE = "non-channel-source"
CHECK_LOCK_BYTES_MISMATCH = "lock-bytes-mismatch"
CHECK_UNPUBLISHED_RELEASE = "unpublished-release"

#: Sentinel used in a finding's sha slots when the value is unknown / absent.
SHA_ABSENT = None

#: Network timeout for the published-release probe (seconds).
PUBLISH_PROBE_TIMEOUT_S = 10


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
    merge-gate can route each finding through `feedback-to-issue`.
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

    Byte-identical to `sync-deps`'s `tree_sha256` (the producer of the
    `vendor.lock` `tree_sha256` this gate recomputes): symlinks and non-regular
    files are skipped; each sorted relpath contributes `"<relpath>\\0<filesha>\\n"`
    (trailing newline included). The two implementations MUST agree or every
    conformant dep would trip a false lock/bytes-drift WARN.

    Returns the `sha256:<hex>` convention string (matching `component-registry`).
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


# ── Channel probe (the only network-dependent surface) ─────────────────────────

class ChannelProbe:
    """Resolves whether a dep's pinned release is published on its channel.

    The base probe shells out to `gh` (the GitHub CLI). It is isolated behind
    this class so the offline `--self-test` can inject a deterministic stub
    (`StubChannelProbe`) — no network in self-test.
    """

    def is_published(self, repo: str, release_tag: str) -> bool:
        """Return True iff *release_tag* is a published release of *repo*.

        Raises `ChannelUnreachable` when the channel cannot be reached (no `gh`,
        no network, auth failure) so the caller can degrade gracefully rather
        than treating an unreachable channel as "unpublished".
        """
        if shutil.which("gh") is None:
            raise ChannelUnreachable("the `gh` CLI is not installed")
        try:
            proc = subprocess.run(
                ["gh", "release", "view", release_tag, "--repo", repo, "--json", "tagName"],
                capture_output=True,
                text=True,
                timeout=PUBLISH_PROBE_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ChannelUnreachable(f"`gh` invocation failed: {exc}") from exc
        if proc.returncode == 0:
            return True
        stderr = (proc.stderr or "").lower()
        # A clean "not found" means reachable-but-unpublished; anything else
        # (auth, rate-limit, network) is an unreachable degrade, not a verdict.
        if "release not found" in stderr or "not found" in stderr:
            return False
        raise ChannelUnreachable(
            f"`gh release view` did not yield a verdict: {proc.stderr.strip()!r}"
        )


class ChannelUnreachable(RuntimeError):
    """The release channel could not be reached to render a publish verdict."""


# ── The checker ────────────────────────────────────────────────────────────────

class DependencyChannelConformance:
    """Runs the three Dependency Channel conformance checks over a repo root.

    Layout assumptions (design §3):
      <root>/vendor.toml   — human intent (TOML; parsed via tomllib on 3.11+).
      <root>/vendor.lock   — resolved truth (JSON).
      <root>/vendor/<dep>/ — committed vendored bytes.
      <root>/.gitmodules   — submodule registry (a non-channel source signal).

    All findings default to WARN severity (advisory this release); the merge-gate
    decides block/warn/off via the live `code-quality.audit-gate` dial.
    """

    VENDOR_TOML = "vendor.toml"
    VENDOR_LOCK = "vendor.lock"
    VENDOR_DIR = "vendor"
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

    def _vendored_dep_dirs(self) -> dict[str, str]:
        """Return {dep_name: abs_dir} for each immediate child of `vendor/`."""
        vendor_root = os.path.join(self.root, self.VENDOR_DIR)
        result: dict[str, str] = {}
        if not os.path.isdir(vendor_root):
            return result
        for name in sorted(os.listdir(vendor_root)):
            abs_dir = os.path.join(vendor_root, name)
            if os.path.isdir(abs_dir):
                result[name] = abs_dir
        return result

    # ── Individual checks ───────────────────────────────────────────────────

    def check_non_channel_source(self, result: ConformanceResult) -> None:
        """Check 1 — flag a dep sourced from a submodule (non-channel)."""
        manifest = self._load_manifest()
        submodule_paths = self._load_submodule_paths()
        if not submodule_paths:
            return
        # Map each manifest dep to its declared dest (defaults to vendor/<dep>).
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
        # Also flag a submodule landing directly under vendor/ even if the
        # manifest does not name it (a stray submodule-sourced vendored dir).
        for sub_path in sorted(submodule_paths):
            norm = sub_path.replace("\\", "/")
            if norm.startswith(f"{self.VENDOR_DIR}/"):
                dep = norm[len(self.VENDOR_DIR) + 1:].split("/", 1)[0]
                if dep not in manifest:
                    result.add(
                        Finding(
                            check=CHECK_NON_CHANNEL_SOURCE,
                            dep=dep,
                            channel=None,
                            severity=SEVERITY_WARN,
                            detail=(
                                f"a git submodule is mounted under vendor/ at "
                                f"{sub_path!r} but is not declared in vendor.toml; "
                                f"vendored deps must come from a release channel"
                            ),
                        )
                    )

    def check_lock_bytes_mismatch(self, result: ConformanceResult) -> None:
        """Check 2 — flag vendored bytes with no/incorrect lock entry."""
        lock = self._load_lock()
        manifest = self._load_manifest()
        submodule_paths = self._load_submodule_paths()
        for dep, abs_dir in self._vendored_dep_dirs().items():
            dest = f"{self.VENDOR_DIR}/{dep}"
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

    def check_unpublished_release(
        self, result: ConformanceResult, offline: bool = False
    ) -> None:
        """Check 3 — flag a pinned release that is not published on its channel.

        Network-dependent. Degrades gracefully: when offline or the channel is
        unreachable, it records a degradation and renders no verdict (never a
        hard fail).
        """
        manifest = self._load_manifest()
        if not manifest:
            return
        if offline:
            result.degrade(
                "unpublished-release check skipped (--offline): "
                "release publication is not verifiable without network access"
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
            try:
                published = self.probe.is_published(repo, release_tag)
            except ChannelUnreachable as exc:
                result.degrade(
                    f"unpublished-release check for dep {dep!r} degraded: {exc} "
                    f"(channel unreachable — reported, not failed)"
                )
                continue
            if not published:
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

    # ── Orchestration ────────────────────────────────────────────────────────

    def run(self, label: str = "vendor-check", offline: bool = False) -> ConformanceResult:
        """Run all three checks and return the aggregated result."""
        result = ConformanceResult(label)
        self.check_non_channel_source(result)
        self.check_lock_bytes_mismatch(result)
        self.check_unpublished_release(result, offline=offline)
        return result


# ── Offline self-test fixtures ─────────────────────────────────────────────────

class StubChannelProbe(ChannelProbe):
    """Offline probe stub for `--self-test`.

    Drives the three publish outcomes deterministically with no network:
      - a tag in `published`         → published (True)
      - a tag in `unpublished`       → not published (False)
      - a tag in `unreachable`       → raises ChannelUnreachable (degrade path)
    """

    def __init__(
        self,
        published: Optional[set[str]] = None,
        unreachable: Optional[set[str]] = None,
    ) -> None:
        self.published = published or set()
        self.unreachable = unreachable or set()

    def is_published(self, repo: str, release_tag: str) -> bool:
        if release_tag in self.unreachable:
            raise ChannelUnreachable(f"stub: {release_tag} unreachable")
        return release_tag in self.published


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _build_conformant_repo(root: str) -> None:
    """A fully conformant repo: one channel dep, locked, bytes match."""
    dep_dir = os.path.join(root, "vendor", "aura")
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
        'dest = "vendor/aura"\n'
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
    """A repo whose vendored dep is sourced from a git submodule (check 1)."""
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
    """A repo with vendored bytes but no lock entry (check 2: unlocked dir)."""
    dep_dir = os.path.join(root, "vendor", "ollama")
    _write(os.path.join(dep_dir, "bin", "ollama"), "#!/bin/sh\n")
    _write(
        os.path.join(root, "vendor.toml"),
        'schema_version = 1\n\n'
        '[deps.ollama]\n'
        'repo = "ollama/ollama"\n'
        'channel = "stable"\n'
        'version = "0.3.12"\n'
        'dest = "vendor/ollama"\n'
        'kind = "asset-bundle"\n',
    )
    _write(os.path.join(root, "vendor.lock"), json.dumps({"schema_version": 1, "deps": {}}) + "\n")


def _build_drifted_repo(root: str) -> None:
    """A repo whose vendored bytes drifted from the locked tree_sha256 (check 2)."""
    dep_dir = os.path.join(root, "vendor", "aura")
    _write(os.path.join(dep_dir, "css", "aura.css"), "body{color:#FFF}\n")  # changed bytes
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
        h_a = tree_sha256(os.path.join(d1, "vendor", "aura"))
        h_b = tree_sha256(os.path.join(d1, "vendor", "aura"))
        if h_a != h_b or not h_a.startswith("sha256:"):
            failures.append(f"tree_sha256 not deterministic / malformed: {h_a} vs {h_b}")
        else:
            print(f"  OK: tree_sha256 stable across two runs ({h_a[:18]}…)")

        # ── Conformant repo: no violations ───────────────────────────────────
        print("\nGroup: conformant repo (no violations)")
        probe_ok = StubChannelProbe(published={"v3.20.0"})
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

        # ── Check 3: unpublished release → WARN (with network stub) ──────────
        print("\nGroup: check 3 — unpublished release (stubbed network)")
        d_unpub = os.path.join(tmp, "unpub")
        _build_conformant_repo(d_unpub)
        probe_unpub = StubChannelProbe(published=set())  # v3.20.0 NOT published
        r_unpub = DependencyChannelConformance(d_unpub, probe=probe_unpub).run(
            "unpublished-release", offline=False
        )
        expect_finding(r_unpub, CHECK_UNPUBLISHED_RELEASE)

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
            "implementation). See docs/design/dependency-channel-design.md §5."
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
        help="Repo root to audit (expects vendor.toml / vendor.lock / vendor/).",
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
