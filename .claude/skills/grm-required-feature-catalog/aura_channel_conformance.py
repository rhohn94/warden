#!/usr/bin/env python3
"""aura_channel_conformance.py — conformance probe for the required-feature
catalog's Entry 7 (One Sanctioned Aura Consumption Mechanism; #434, v3.97 R6
Pass 5).

Entry 7's own "Detect" section (required-feature-catalog.md §Entry 7) already
specifies a deterministic repo-state predicate in prose: a repo matches when
it carries Aura bytes/derived output WITHOUT a corresponding `vendor.toml
[deps.aura]` + matching `vendor.lock` pairing. This script is that predicate,
made executable, plus straggler-mechanism naming (submodule / frozen
token-vendoring snapshot / committed codegen from an untracked clone) so a
finding names WHICH of the three known stragglers (issue-tracker /
music-collection / obsidian, per the entry's Per-straggler guidance table)
the target resembles, rather than a generic "aura present, unlabeled."

Deliberately offline-only — no live-probe leg. Unlike Entries 1/2 (a running
app's HTTP surface) or Entries 3-5 (a release channel's network-reachable
publish state), Entry 7's subject is the repo's OWN COMMITTED TREE, not a
running app or a remote channel — there is nothing for a "live" mode to add
here. This is a deliberate, documented scope choice, not an oversight: see
the module-level exemption note in required-feature-catalog.md §Entry 7's
`conformance-check` field.

Reuses `standard_package_conformance.probe()` for the channel-conformant path
(Entry 7's "target" state — `[deps.aura]` vendored and conformant — is
structurally identical to Entries 3-5's adopted-conformant check; no reason
to re-derive it) and adds straggler-mechanism detection on top for the
non-conformant path.

CLI:
    python3 aura_channel_conformance.py --root PATH
    python3 aura_channel_conformance.py --self-test

Design authority: required-feature-catalog.md §Entry 7 (Detect / Adopt /
Per-straggler guidance).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standard_package_conformance as spc  # noqa: E402

# grm-dependency-audit is a fixed sibling skill directory (same pattern
# standard_package_conformance.py itself already uses) — reuse
# DependencyChannelConformance.dep_declared() directly for the "is [deps.aura]
# declared at all" question rather than string-matching spc.probe()'s
# human-readable note text (fragile: a reworded note would silently break
# detection).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-dependency-audit"))
import dependency_channel_conformance as dcc  # noqa: E402

AURA_DEP = "aura"


class ConformanceResult:
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


# ── Straggler-mechanism detection ────────────────────────────────────────────

def _gitmodules_has_aura(root: Path) -> bool:
    gm = root / ".gitmodules"
    if not gm.is_file():
        return False
    text = gm.read_text(encoding="utf-8", errors="ignore")
    return "aura" in text.lower() or "design-language" in text.lower()


def _has_frozen_snapshot(root: Path) -> bool:
    return (root / "tools" / "aura").is_dir()


def _has_untracked_codegen(root: Path) -> bool:
    for pattern in ("aura_generated.*", "*aura_generated*"):
        if list(root.glob(f"**/{pattern}")):
            return True
    return False


def _has_unlabeled_vendored_bytes(root: Path) -> bool:
    for candidate in (root / "lib" / "third-party" / AURA_DEP,
                       root / "vendor" / AURA_DEP):
        if candidate.is_dir() and any(candidate.iterdir()):
            return True
    return False


def detect_straggler_mechanism(root: Path) -> tuple[str, str]:
    """Returns (mechanism, detail). mechanism is one of: "none",
    "git-submodule", "frozen-snapshot", "untracked-codegen",
    "unlabeled-vendored-bytes" — mirrors the entry's Per-straggler guidance
    table order (issue-tracker / music-collection / obsidian), falling back
    to the generic "bytes present, no pairing" case for anything else."""
    if _gitmodules_has_aura(root):
        return "git-submodule", (
            "a .gitmodules entry references Aura/design-language — the "
            "issue-tracker-shaped straggler (submodule -> channel "
            "asset-bundle; follow retro-game-player's "
            "dependency-channel-conformance.md as the worked example).")
    if _has_frozen_snapshot(root):
        return "frozen-snapshot", (
            "a tools/aura/ directory exists — the music-collection-shaped "
            "straggler (frozen token-vendoring snapshot -> channel-sourced "
            "tokens.resolved.json).")
    if _has_untracked_codegen(root):
        return "untracked-codegen", (
            "a committed *aura_generated* file exists — the obsidian-shaped "
            "straggler (codegen stays; its input becomes a channel-pinned "
            "tokens.resolved.json instead of an untracked clone).")
    if _has_unlabeled_vendored_bytes(root):
        return "unlabeled-vendored-bytes", (
            "Aura-shaped vendored bytes exist under lib/third-party/aura/ or "
            "vendor/aura/ with no vendor.toml [deps.aura] entry — a generic, "
            "unlabeled straggler.")
    return "none", "no Aura bytes/derived output detected at all."


# ── Probe entry point ────────────────────────────────────────────────────────

def probe(root: Path) -> ConformanceResult:
    result = ConformanceResult(str(root))

    dep_declared = dcc.DependencyChannelConformance(str(root)).dep_declared(AURA_DEP)

    if dep_declared:
        # `[deps.aura]` IS declared — get the vendoring verdict from the
        # shared probe (re-running dep_declared() there is a cheap, harmless
        # duplicate lookup, not a second source of truth: both calls read
        # the same vendor.toml via the same DependencyChannelConformance
        # loader).
        channel_result = spc.probe(root, AURA_DEP, offline=True)
        # `[deps.aura]` exists — this IS the channel-conformant target state
        # (or a declared-but-drifted one); defer straight to the vendoring
        # verdict, exactly Entries 3-5's own logic.
        if channel_result.passed:
            result.note("Aura is consumed via the sanctioned Dependency "
                         "Channel ([deps.aura] declared, vendored, "
                         "conformant) — no straggler.")
        else:
            for e in channel_result.errors:
                result.error(f"aura declared in vendor.toml but not "
                              f"channel-conformant: {e}")
            for w in channel_result.warnings:
                result.warn(w)
        return result

    # No [deps.aura] entry — check whether a straggler mechanism is present
    # anyway (the entry's actual trigger condition: bytes/derived output
    # WITHOUT the channel pairing).
    mechanism, detail = detect_straggler_mechanism(root)
    if mechanism == "none":
        result.note("no Aura consumption detected at all — Entry 7 does not "
                     "apply to this repo.")
        return result

    result.error(
        f"Aura consumption detected via {mechanism} with no vendor.toml "
        f"[deps.aura] channel pairing — this is a STRAGGLER "
        f"(required-feature-catalog.md §Entry 7). {detail} Migrate via "
        f"grm-vendor-migrate.")
    return result


# ── Self-test (offline only, per the module's own "no live leg" design) ────

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("aura_channel_conformance.py --self-test")

    # 1. No Aura at all -> PASS, not-applicable.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        r = probe(root)
        check(r.passed, "no Aura at all should PASS (not-applicable)")
        print("  OK: no Aura consumption -> PASS")

    # 2. Channel-conformant ([deps.aura] vendored, bytes match lock) -> PASS.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        spc._build_conformant_repo(root, AURA_DEP)
        r = probe(root)
        check(r.passed, f"channel-conformant aura should PASS: {r.report()}")
        print("  OK: channel-conformant [deps.aura] -> PASS")

    # 3. Straggler — git submodule (issue-tracker shape) -> FLAGGED.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / ".gitmodules",
               '[submodule "lib/third-party/aura"]\n'
               '\tpath = lib/third-party/aura\n'
               '\turl = https://github.com/rhohn94/design-language.git\n')
        r = probe(root)
        check(not r.passed, "submodule straggler must FAIL")
        check(any("git-submodule" in e for e in r.errors),
              "submodule straggler should name the git-submodule mechanism")
        print("  OK (expected FAIL): git-submodule straggler -> FLAGGED")

    # 4. Straggler — frozen tools/aura/ snapshot (music-collection shape).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "tools" / "aura" / "vendor_tokens.py", "# frozen\n")
        r = probe(root)
        check(not r.passed, "frozen-snapshot straggler must FAIL")
        check(any("frozen-snapshot" in e for e in r.errors),
              "frozen-snapshot straggler should name its mechanism")
        print("  OK (expected FAIL): frozen-snapshot straggler -> FLAGGED")

    # 5. Straggler — committed codegen from untracked clone (obsidian shape).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "src" / "aura_generated.rs", "// generated\n")
        r = probe(root)
        check(not r.passed, "untracked-codegen straggler must FAIL")
        check(any("untracked-codegen" in e for e in r.errors),
              "untracked-codegen straggler should name its mechanism")
        print("  OK (expected FAIL): untracked-codegen straggler -> FLAGGED")

    # 6. Straggler — unlabeled vendored bytes with no vendor.toml entry.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "lib" / "third-party" / "aura" / "tokens.json", "{}")
        r = probe(root)
        check(not r.passed, "unlabeled vendored bytes must FAIL")
        check(any("unlabeled-vendored-bytes" in e for e in r.errors),
              "unlabeled bytes straggler should name its mechanism")
        print("  OK (expected FAIL): unlabeled vendored bytes -> FLAGGED")

    # 7. Declared but drifted [deps.aura] -> FAIL, routed through the
    #    vendoring-drift path (not the straggler path).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        spc._build_drifted_repo(root, AURA_DEP)
        r = probe(root)
        check(not r.passed, "declared-but-drifted aura must FAIL")
        check(any("not channel-conformant" in e for e in r.errors),
              "drifted aura should route through the vendoring-drift "
              "message, not the straggler message")
        print("  OK (expected FAIL): declared-but-drifted [deps.aura] "
              "-> FLAGGED (vendoring drift, not straggler)")

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
            "Conformance probe for required-feature catalog Entry 7 "
            "(Sanctioned Aura Consumption Mechanism). See "
            "required-feature-catalog.md §Entry 7."
        )
    )
    parser.add_argument("--root", metavar="PATH", default=".",
                         help="Target repo root to probe (default: cwd).")
    parser.add_argument("--self-test", action="store_true",
                         help="Run the offline fixture round trip.")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    root = Path(args.root).resolve()
    result = probe(root)
    print(result.report())
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
