#!/usr/bin/env python3
"""standard_package_conformance.py — deterministic adoption probe for a
required-feature-catalog "standard package" entry (#434, v3.97 R6 Pass 5).

Entries 3 (token-bookkeeper), 4 (gatekeeper), and 5 (recordkeeper) all share
the exact same shape: a Rust standard package, consumed through the
Dependency Channel, with a "vendored + channel-conformant" sub-requirement
(TB-1/TB-4-5, GK-1/GK-4-5, RK-1/RK-5-6) that IS mechanically checkable, and a
"no in-tree fork" / "wired against the app's own code" sub-requirement
(TB-2/TB-3, GK-2/GK-3, RK-2/RK-4) that is NOT — verifying the app actually
calls the vendored crate instead of a hand-rolled equivalent requires reading
and understanding the app's own source, not a grep. This script is
deliberately scoped to the checkable half, exactly like Entry 2's
`changelog_conformance.py` scopes its route-convention check to
"informational only" (required-feature-catalog.md §Entry 2 Conformance
check) rather than pretending to verify something a static/offline script
cannot.

One script, parameterized by `--dep`, rather than three near-identical
copies (token_bookkeeper_conformance.py / gatekeeper_conformance.py /
recordkeeper_conformance.py) — the three entries differ only in which
`vendor.toml` dep name they probe.

Three outcomes:
  not-adopted        — the dep has no `[deps.<dep>]` entry in vendor.toml at
                        all. NOT a failure: per required-feature-catalog.md,
                        *implementing* a filed entry is the managed project's
                        own scope and timeline; the filed ticket is what
                        tracks adoption, not this probe. Reported so a caller
                        (install_doctor.py, grm-fleet-audit's reconciliation)
                        can tell "not adopted yet" apart from "adopted but
                        broken."
  adopted-conformant  — the dep is declared, vendored, and
                        `dependency_channel_conformance.py` reports no
                        WARN/ERROR finding for it.
  adopted-nonconformant — the dep is declared but
                        `dependency_channel_conformance.py` found a real
                        problem (lock/bytes drift, non-channel source,
                        unpublished/malformed release, unsigned). Findings are
                        re-classified here: lock/bytes/non-channel-source
                        findings are ERRORS (a real, offline-verifiable
                        drift); publish/signing findings (network-dependent)
                        stay WARNINGS, mirroring the network-degrades-softly
                        discipline `dependency_channel_conformance.py` itself
                        already applies to its own checks 3-5.

Reuses `dependency_channel_conformance.DependencyChannelConformance` as a
library (the vendoring/lock/publish logic already exists and is
self-tested there — this script does not re-derive it) rather than
duplicating the tree_sha256 / manifest / lock plumbing a third time.

CLI:
    python3 standard_package_conformance.py --root PATH --dep DEP [--offline]
    python3 standard_package_conformance.py --self-test

Design authority: required-feature-catalog.md Entries 3/4/5;
docs/grimoire/design/dependency-channel-design.md §5.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# grm-dependency-audit is a fixed sibling skill directory (mirrors the
# install_doctor.py -> issue_tracker.py / changelog_conformance.py ->
# config_validate.py sys.path-insert pattern already used across this repo) —
# reuse its vendoring/lock/publish conformance engine rather than duplicating
# tree_sha256 / manifest / lock parsing a third time.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-dependency-audit"))
import dependency_channel_conformance as dcc  # noqa: E402

# Findings from these checks are offline/static — a real, reproducible drift
# independent of network access — so they escalate to ERROR here. Publish/
# signing findings need a network probe to confirm and already degrade
# gracefully offline in dcc itself, so they stay WARN-tier.
_OFFLINE_ERROR_CHECKS = frozenset({
    dcc.CHECK_NON_CHANNEL_SOURCE,
    dcc.CHECK_LOCK_BYTES_MISMATCH,
})


# ── Finding collector (mirrors fleet_conformance.py / changelog_conformance.py
#    so every catalog-conformance script in this skill directory reads the
#    same way) ─────────────────────────────────────────────────────────────

class ConformanceResult:
    """Accumulates findings for one probe run."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.info.append(msg)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def report(self) -> str:
        lines = [f"[{self.label}]"]
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        for i in self.info:
            lines.append(f"  INFO:  {i}")
        status = "PASS" if self.passed else "FAIL"
        lines.append(f"  -> {status}")
        return "\n".join(lines)


# ── Probe entry point ────────────────────────────────────────────────────────

def probe(root: Path, dep: str, offline: bool = True) -> ConformanceResult:
    """Probe *dep*'s adoption state under *root*.

    `offline=True` (the default, and the ONLY mode `--self-test` ever
    exercises) skips `dependency_channel_conformance`'s network-dependent
    publish/signing checks 3-5 — this mirrors every other catalog-conformance
    script in this skill directory (fleet_conformance.py,
    changelog_conformance.py) never touching the network in self-test.
    `install_doctor.py` passes through its own `--no-network` flag here.
    """
    result = ConformanceResult(f"{dep}@{root}")
    checker = dcc.DependencyChannelConformance(str(root))

    if not checker.dep_declared(dep):
        result.note(
            f"{dep!r} is not declared in vendor.toml — not yet adopted. "
            f"This is NOT a failure: implementing a filed required-feature-"
            f"catalog entry is the managed project's own scope and timeline "
            f"(required-feature-catalog.md's own SPEC framing). Re-run once "
            f"the app has vendored {dep!r}.")
        return result

    dcc_result = checker.run(label=dep, offline=offline)
    dep_findings = [f for f in dcc_result.findings if f.dep == dep]
    if not dep_findings:
        result.note(f"{dep!r} is vendored and channel-conformant "
                     f"(no drift/publish findings).")
    for f in dep_findings:
        line = f"[{f.check}] {f.detail}"
        if f.check in _OFFLINE_ERROR_CHECKS:
            result.error(line)
        else:
            result.warn(line)
    for d in dcc_result.degradations:
        if dep in d:
            result.note(f"degraded (reported, not failed): {d}")
    result.note(
        f"NOTE: this probe verifies vendoring/channel-conformance only "
        f"(the mechanically-checkable half). It does NOT verify that {dep!r} "
        f"is actually wired into the app's own code / that no in-tree fork "
        f"remains — that requires reading the app's source and is out of "
        f"this script's reach; see required-feature-catalog.md's entry for "
        f"the full sub-requirement list.")
    return result


# ── Self-test (offline fixture round trip; no network — mirrors the
#    dependency_channel_conformance.py fixtures this reuses) ────────────────

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_not_adopted_repo(root: Path) -> None:
    """No vendor.toml at all — the common "not yet adopted" case."""
    pass  # empty repo


def _build_conformant_repo(root: Path, dep: str) -> None:
    dep_dir = root / "lib" / "third-party" / dep
    _write(dep_dir / "src" / "lib.rs", "// vendored\n")
    observed = dcc.tree_sha256(str(dep_dir))
    _write(root / "vendor.toml",
           f'schema_version = 1\n\n[deps.{dep}]\n'
           f'repo = "rhohn94/{dep}"\nchannel = "stable"\n'
           f'version = "0.1.0"\nkind = "vendored-crate"\n')
    lock = {"schema_version": 1, "deps": {
        dep: {"version": "0.1.0", "channel": "stable",
              "release_tag": "v0.1.0", "tree_sha256": observed}}}
    _write(root / "vendor.lock", json.dumps(lock, indent=2) + "\n")


def _build_drifted_repo(root: Path, dep: str) -> None:
    """Vendored bytes present but no matching vendor.lock entry (a real,
    offline-detectable drift — must ERROR)."""
    dep_dir = root / "lib" / "third-party" / dep
    _write(dep_dir / "src" / "lib.rs", "// vendored, unlocked\n")
    _write(root / "vendor.toml",
           f'schema_version = 1\n\n[deps.{dep}]\n'
           f'repo = "rhohn94/{dep}"\nchannel = "stable"\n'
           f'version = "0.1.0"\nkind = "vendored-crate"\n')
    _write(root / "vendor.lock",
           json.dumps({"schema_version": 1, "deps": {}}) + "\n")


def run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("standard_package_conformance.py --self-test")

    for dep in ("token-bookkeeper", "gatekeeper", "recordkeeper"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_not_adopted_repo(root)
            r = probe(root, dep, offline=True)
            check(r.passed, f"{dep}: not-adopted repo must PASS (not a failure)")
            check(any("not yet adopted" in n for n in r.info),
                  f"{dep}: not-adopted repo should note 'not yet adopted'")
            print(f"  OK: {dep} not-adopted -> PASS (informational)")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_conformant_repo(root, dep)
            r = probe(root, dep, offline=True)
            check(r.passed, f"{dep}: conformant repo must PASS: {r.report()}")
            print(f"  OK: {dep} adopted+conformant -> PASS")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_drifted_repo(root, dep)
            r = probe(root, dep, offline=True)
            check(not r.passed, f"{dep}: unlocked/drifted vendored bytes must FAIL")
            check(any(dcc.CHECK_LOCK_BYTES_MISMATCH in e for e in r.errors),
                  f"{dep}: drift finding should be lock-bytes-mismatch")
            print(f"  OK (expected FAIL): {dep} adopted+drifted -> FLAGGED")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_conformant_repo(root, dep)
            r_other = probe(root, "some-other-dep", offline=True)
            check(r_other.passed and any("not yet adopted" in n for n in r_other.info),
                  f"{dep} fixture: an unrelated dep name must read as its own "
                  f"not-adopted, never leak {dep}'s findings")
            print(f"  OK: {dep} fixture — an unrelated --dep name stays isolated")

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} unexpected result(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELF-TEST PASSED.")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic adoption probe for a required-feature-catalog "
            "standard-package entry (Entries 3/4/5). See "
            "required-feature-catalog.md."
        )
    )
    parser.add_argument("--root", metavar="PATH", default=".",
                         help="Target repo root to probe (default: cwd).")
    parser.add_argument("--dep", metavar="NAME",
                         help="vendor.toml dep name to probe (e.g. "
                              "gatekeeper, recordkeeper, token-bookkeeper).")
    parser.add_argument("--offline", action="store_true", default=True,
                         help="Skip network-dependent publish/signing checks "
                              "(default: on).")
    parser.add_argument("--network", dest="offline", action="store_false",
                         help="Allow the network-dependent publish/signing "
                              "checks (requires the `gh` CLI).")
    parser.add_argument("--self-test", action="store_true",
                         help="Run the offline fixture round trip; ignores "
                              "--root/--dep.")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if not args.dep:
        parser.error("--dep is required (unless --self-test)")

    root = Path(args.root).resolve()
    result = probe(root, args.dep, offline=args.offline)
    print(result.report())
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
