#!/usr/bin/env python3
"""sync_deps.py — the Dependency Channel consumer engine (DEP-CH-2).

Reconciles a repository's first-party dependencies from published GitHub
Release channels into committed `vendor/<dep>/` trees, then records the resolved
truth in a JSON `vendor.lock`. Build & runtime read **only** `vendor/<dep>/`; the
network is touched **only** at sync time.

Per dep, in order (design `dependency-channel-design.md` §4):

  1. Resolve  channel -> version. Pin by default (the `version` in vendor.toml is
     the pin). `--update` resolves latest-on-channel via `gh` and rewrites the pin.
  2. Download release.json + SHA256SUMS + the artifact to a fixed app-owned
     staging dir (never a server-derived path).
  3. Verify  the artifact's sha256 against its SHA256SUMS entry BEFORE any
     filesystem placement. Mismatch => HARD REFUSE (nonzero). Absent SHA256SUMS
     => LOUD DEGRADE (nonzero), never silent trust. (minisig is the deferred seam.)
  4. Stage + atomic-replace  vendor/<dep>/ (honoring strip_components / extract
     allowlist), then atomic os.replace into place.
  5. Write  vendor.lock via write-if-changed (deterministic, sorted keys) — a
     re-sync with an unchanged pin is a byte-identical no-op.

Two-hash lock model: `artifact_sha256` (wire — equals the SHA256SUMS entry,
verifies the download) + `tree_sha256` (offline drift — recomputed from the
placed vendor bytes by `--check` and the conformance gate).

Modes:
  (default)    Resolve + download + verify + vendor + write lock.
  --check      Recompute tree_sha256 of the vendored bytes vs the lock; exit
               nonzero on drift; WRITE NOTHING.
  --offline    Validate vendored bytes vs the lock with ZERO gh/network calls.
  --update     Resolve latest-on-channel (gh), rewrite the vendor.toml pin, sync.
  --self-test  Deterministic, stdlib-only, offline-fixture-based regression run.

Security (Ollama-RCE avoidance, design §11): asset-name allowlist, fixed app-owned
staging dir, verify-sha256-BEFORE-placement, checksum-before-signature, never
trust server-supplied names.

stdlib-only. `tomllib` requires Python 3.11+ (an explicit floor; a <3.11 fallback
parser is a NON-GOAL this release). vendor.lock + the read/gate path stay pure JSON.

Design: docs/design/dependency-channel-design.md §3-§4.
"""

import argparse
import os
import sys

# Allow running both as a module and as a bare script from the skill dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sync_deps_engine import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    SyncDepsEngine,
    SyncDepsError,
    run_self_test,
)


def build_parser():
    """Construct the argparse CLI surface."""
    p = argparse.ArgumentParser(
        prog="sync_deps.py",
        description="Dependency Channel consumer engine — sync vendored deps "
                    "from published release channels.",
    )
    p.add_argument("--root", default=".",
                   help="Repository root holding vendor.toml (default: cwd).")
    p.add_argument("--dep", default=None,
                   help="Reconcile only this dependency (default: all).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="Detect drift vs vendor.lock; write nothing; "
                           "exit nonzero on drift.")
    mode.add_argument("--offline", action="store_true",
                      help="Validate vendored bytes vs the lock with zero "
                           "gh/network calls.")
    mode.add_argument("--update", action="store_true",
                      help="Resolve latest-on-channel (gh) and rewrite the pin.")
    mode.add_argument("--self-test", action="store_true",
                      help="Run the deterministic offline self-test and exit.")
    return p


def main(argv=None):
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    if args.self_test:
        ok = run_self_test()
        print("sync_deps self-test: PASS" if ok else "sync_deps self-test: FAIL")
        return EXIT_OK if ok else EXIT_VIOLATION

    try:
        engine = SyncDepsEngine(root=args.root)
        if args.check:
            return engine.check(only=args.dep)
        if args.offline:
            return engine.offline_validate(only=args.dep)
        return engine.sync(only=args.dep, update=args.update)
    except SyncDepsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
