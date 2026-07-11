#!/usr/bin/env python3
"""workspace_clean.py — tiered, report-then-apply cleanup of Grimoire workspace
artifacts (v3.52, #185).

A consumer project root accumulates Grimoire working-state directories. Some are
load-bearing (deleting them breaks a future operation), some are regenerable
caches, and some are transient leftovers. This tool CLASSIFIES each known
artifact and removes ONLY the safe-delete set — never the load-bearing ones.

Classification (the authority is CLASSIFICATION below):

  KEEP        load-bearing — never deleted; on a HARD DENYLIST so it is refused
              even if explicitly named. (.claude/, .scaffold-base/)
  SAFE-DELETE re-clonable / regenerable / transient — removed on --apply.
              (.design-language-source/, .grimoire-golden/, .grimoire-source/,
               .scaffold-sync-backup/)

Safety contract (house style, modelled on grm-structure-migrate):
  * Report-only by DEFAULT. Nothing is deleted without --apply.
  * --apply deletes ONLY the SAFE-DELETE set.
  * The HARD DENYLIST (.claude/, .scaffold-base/) is refused unconditionally —
    it is never in the delete set and a request to delete it is denied loudly.
  * Idempotent — a second run on a clean workspace is a no-op.

Stdlib-only. Run --self-test to verify the classifier + apply against tempdir
fixtures (no real workspace is touched).

Usage: workspace_clean.py [--root P] [--apply] [--self-test]
Exit: 0 = no safe-delete findings (clean) OR --apply succeeded OR self-test
          passed; 1 = safe-delete findings present in report mode (or self-test
          failed). Mirrors grm-structure-migrate / grm-docs-migrate.
"""
import argparse
import shutil
import sys
from pathlib import Path

# Verdicts.
KEEP = "KEEP"
SAFE_DELETE = "SAFE-DELETE"

# The single classification authority. Each entry: dir-name -> (verdict, why).
# Order is report order.
CLASSIFICATION = {
    ".claude": (KEEP, "the entire Grimoire install — NEVER delete (hard denylist)"),
    ".scaffold-base": (KEEP, "3-way-merge base tree for sync-from-upstream — load-bearing (hard denylist)"),
    ".design-language-source": (SAFE_DELETE, "cached clone for design-language-adapt — re-clonable"),
    ".grimoire-golden": (SAFE_DELETE, "generated golden restore baseline — regenerable cache"),
    ".grimoire-source": (SAFE_DELETE, "source/staging clone — transient"),
    ".scaffold-sync-backup": (SAFE_DELETE, "rollback backup from a sync — transient (post-successful-sync)"),
}

# HARD DENYLIST — these are refused even if a caller explicitly asks to delete
# them. Derived from CLASSIFICATION (every KEEP entry) plus the names are pinned
# here so the refusal is independent of any future classification edit.
HARD_DENYLIST = frozenset({".claude", ".scaffold-base"})


def classify(root: Path) -> list[tuple[str, str, str]]:
    """Return [(name, verdict, why)] for every known artifact PRESENT under root,
    in report order. Absent artifacts are omitted."""
    found = []
    for name, (verdict, why) in CLASSIFICATION.items():
        if (root / name).is_dir():
            found.append((name, verdict, why))
    return found


def safe_delete_targets(root: Path) -> list[str]:
    """Names present under root that are classified SAFE-DELETE and NOT on the
    hard denylist. This is the ONLY set --apply may remove."""
    return [
        name
        for name, verdict, _ in classify(root)
        if verdict == SAFE_DELETE and name not in HARD_DENYLIST
    ]


def apply_clean(root: Path) -> list[str]:
    """Remove the safe-delete set. Idempotent; returns names actually removed.
    Hard-denylisted names are never in the target set, so they cannot be removed
    here even by accident."""
    removed = []
    for name in safe_delete_targets(root):
        assert name not in HARD_DENYLIST, f"refused: {name} is on the hard denylist"
        target = root / name
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            removed.append(name)
    return removed


def render_report(root: Path) -> tuple[str, int]:
    """Return (text, n_safe_delete). Report-only — nothing is removed."""
    found = classify(root)
    deletable = [n for n in safe_delete_targets(root)]
    lines = []
    if not found:
        lines.append("workspace-clean — no known Grimoire artifacts present (clean).")
        return "\n".join(lines), 0
    n_keep = sum(1 for _, v, _ in found if v == KEEP)
    lines.append(
        f"workspace-clean — {len(found)} artifact(s): "
        f"{len(deletable)} safe-delete, {n_keep} keep"
    )
    for name, verdict, why in found:
        mark = "→ delete on --apply" if (verdict == SAFE_DELETE and name in deletable) else "(kept)"
        lines.append(f"  {name + '/':<26} {verdict:<12} {mark}  {why}")
    if deletable:
        lines.append("")
        lines.append("Re-run with --apply to remove the safe-delete set. The hard denylist")
        lines.append("(.claude/, .scaffold-base/) is never removed.")
    return "\n".join(lines), len(deletable)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tiered workspace cleanup for Grimoire artifacts.")
    parser.add_argument("--root", default=".", help="project root (default: cwd)")
    parser.add_argument("--apply", action="store_true",
                        help="delete the safe-delete set (default: report only)")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run offline tempdir self-tests and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return _run_self_test()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: root '{root}' is not a directory.", file=sys.stderr)
        return 1

    if args.apply:
        removed = apply_clean(root)
        if removed:
            print("workspace-clean --apply: removed " + ", ".join(n + "/" for n in removed))
        else:
            print("workspace-clean --apply: nothing to remove (already clean).")
        # always report what remains for transparency
        text, _ = render_report(root)
        print()
        print(text)
        return 0

    text, n = render_report(root)
    print(text)
    return 1 if n > 0 else 0


# ---------------------------------------------------------------------------
# Self-test (offline tempdir fixtures — never touches the real workspace)
# ---------------------------------------------------------------------------


def _run_self_test() -> int:
    import tempfile

    fails = 0

    def check(label, cond):
        nonlocal fails
        if cond:
            print(f"OK   [{label}]")
        else:
            print(f"FAIL [{label}]")
            fails += 1

    # ---- classifier ----
    print("--- classification ---")
    # every KEEP entry must be on the hard denylist
    keep_names = {n for n, (v, _) in CLASSIFICATION.items() if v == KEEP}
    check("every KEEP is hard-denylisted", keep_names == set(HARD_DENYLIST))
    # the hard denylist is never in the safe-delete target set
    check("denylist never safe-deletable",
          HARD_DENYLIST.isdisjoint({n for n, (v, _) in CLASSIFICATION.items() if v == SAFE_DELETE}))

    # ---- apply: removes only safe-delete, keeps load-bearing ----
    print("\n--- apply (full fixture) ---")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "ws"
        for name in CLASSIFICATION:
            (root / name / "child").mkdir(parents=True)
        (root / "src").mkdir()  # an unrelated project dir — must be untouched
        # report mode removes nothing
        before = sorted(p.name for p in root.iterdir())
        text, n = render_report(root)
        after_report = sorted(p.name for p in root.iterdir())
        check("report mode deletes nothing", before == after_report)
        check("report counts the 4 safe-delete", n == 4)
        # apply removes exactly the safe-delete set
        removed = apply_clean(root)
        check("apply removes exactly safe-delete set",
              set(removed) == {".design-language-source", ".grimoire-golden",
                               ".grimoire-source", ".scaffold-sync-backup"})
        check("apply kept .claude (denylist)", (root / ".claude").is_dir())
        check("apply kept .scaffold-base (denylist)", (root / ".scaffold-base").is_dir())
        check("apply kept unrelated src/", (root / "src").is_dir())
        check("safe-delete dirs gone", not any(
            (root / n).exists() for n in
            (".design-language-source", ".grimoire-golden", ".grimoire-source", ".scaffold-sync-backup")))
        # idempotent: a second apply removes nothing
        check("apply idempotent (no-op second run)", apply_clean(root) == [])

    # ---- denylist refusal: even when present, never targeted ----
    print("\n--- denylist refusal ---")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "only-keep"
        (root / ".claude").mkdir(parents=True)
        (root / ".scaffold-base").mkdir(parents=True)
        check("denylist-only: nothing safe-deletable", safe_delete_targets(root) == [])
        check("denylist-only: apply removes nothing", apply_clean(root) == [])
        check("denylist-only: .claude survives", (root / ".claude").is_dir())
        check("denylist-only: .scaffold-base survives", (root / ".scaffold-base").is_dir())

    # ---- clean workspace: report says clean, exits 0 ----
    print("\n--- clean workspace ---")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "empty"
        root.mkdir(parents=True)
        text, n = render_report(root)
        check("clean workspace: 0 findings", n == 0)
        check("clean workspace: 'clean' message", "clean" in text)

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} — {fails} failure(s)")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
