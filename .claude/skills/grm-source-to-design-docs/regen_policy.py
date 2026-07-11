#!/usr/bin/env python3
"""regen_policy.py — skip-vs-regenerate decision for grm-source-to-design-docs (#309, BF-5, v3.80).

The skill defaulted to silently skipping regeneration of any design-doc
candidate whose `docs/design/{slug}-design.md` already existed, with no way
for a caller to force a refresh after significant source changes. This module
makes that per-candidate decision deterministic and testable rather than left
to ad-hoc agent judgement, and gives the skill's invocation an explicit,
clearly-named control (`--regenerate` / `--force`).

Decision policy (see the skill's SKILL.md "Regeneration control" section):
  - No existing doc for a candidate  -> always "write" (regardless of the flag;
    there is nothing to skip or overwrite).
  - Existing doc, flag NOT passed    -> "skip" (default; preserves current safe
    behaviour — never silently overwrite without being asked).
  - Existing doc, flag passed        -> "regenerate" (explicit opt-in refresh).

No git writes, no file writes — read-only classification; the skill itself
performs the actual file writes per the returned action.

Usage:
  regen_policy.py --design-dir PATH --candidates SLUG[,SLUG...]
                  [--regenerate|--force] [--self-test]
Stdout: a JSON report with per-candidate actions and a write/skip/regenerate
summary. Exit 0 always (advisory classification; no gate to fail).
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def doc_path(design_dir: str, slug: str) -> str:
    """Path to the design doc a candidate slug would occupy."""
    return os.path.join(design_dir, "%s-design.md" % slug)


def classify_one(design_dir: str, slug: str, regenerate: bool) -> dict:
    """Classify a single candidate as write / skip / regenerate."""
    path = doc_path(design_dir, slug)
    existed = os.path.isfile(path)
    if not existed:
        action = "write"
    elif regenerate:
        action = "regenerate"
    else:
        action = "skip"
    return {"slug": slug, "path": path, "existed": existed, "action": action}


def classify(design_dir: str, slugs: list, regenerate: bool = False) -> list:
    """Classify every candidate slug; returns one result dict per slug, in order."""
    return [classify_one(design_dir, slug, regenerate) for slug in slugs]


def summarize(results: list) -> dict:
    """Group classified results into {"write": [...], "skip": [...], "regenerate": [...]}."""
    summary = {"write": [], "skip": [], "regenerate": []}
    for r in results:
        summary[r["action"]].append(r["slug"])
    return summary


def _self_test():
    import tempfile
    failures = []

    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "auth-design.md"), "w").close()

        # Default (no flag): an existing doc is skipped.
        r = classify_one(d, "auth", regenerate=False)
        if r["action"] != "skip" or not r["existed"]:
            failures.append("default should skip an existing doc: %r" % r)

        # Explicit regenerate: an existing doc is forced to regenerate.
        r2 = classify_one(d, "auth", regenerate=True)
        if r2["action"] != "regenerate" or not r2["existed"]:
            failures.append("--regenerate should force regeneration of an existing doc: %r" % r2)

        # A candidate with no existing doc is always written, flag or not.
        r3 = classify_one(d, "billing", regenerate=False)
        if r3["action"] != "write" or r3["existed"]:
            failures.append("missing doc should always be written by default: %r" % r3)
        r4 = classify_one(d, "billing", regenerate=True)
        if r4["action"] != "write" or r4["existed"]:
            failures.append("missing doc should always be written under --regenerate too: %r" % r4)

        # doc_path builds the same relative path the skill writes to.
        expected = os.path.join(d, "billing-design.md")
        if doc_path(d, "billing") != expected:
            failures.append("doc_path built wrong path: %r != %r" % (doc_path(d, "billing"), expected))

        # Mixed batch, default: only the pre-existing doc is skipped.
        open(os.path.join(d, "search-design.md"), "w").close()
        results = classify(d, ["auth", "billing", "search"], regenerate=False)
        summary = summarize(results)
        if summary != {"write": ["billing"], "skip": ["auth", "search"], "regenerate": []}:
            failures.append("mixed-candidate default summary wrong: %r" % summary)

        # Mixed batch, --regenerate: existing docs regenerate, missing still write.
        results_force = classify(d, ["auth", "billing", "search"], regenerate=True)
        summary_force = summarize(results_force)
        if summary_force != {"write": ["billing"], "skip": [], "regenerate": ["auth", "search"]}:
            failures.append("mixed-candidate --regenerate summary wrong: %r" % summary_force)

        # Empty candidate list is a no-op, not an error.
        if classify(d, [], regenerate=True) != []:
            failures.append("empty candidate list should classify to an empty list")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("regen_policy self-test: OK (default-skip, explicit-regenerate, "
          "missing-doc-always-write, doc_path, mixed-batch summaries, empty-input)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Classify source-to-design-docs candidates as write/skip/regenerate.")
    ap.add_argument("--design-dir", default=os.path.join("docs", "design"))
    ap.add_argument("--candidates", default="",
                    help="comma-separated candidate slugs to classify")
    ap.add_argument("--regenerate", "--force", dest="regenerate", action="store_true",
                    help="force regeneration of design docs that already exist "
                         "(default: skip existing docs)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    slugs = [s.strip() for s in args.candidates.split(",") if s.strip()]
    results = classify(args.design_dir, slugs, regenerate=args.regenerate)
    print(json.dumps({
        "design_dir": args.design_dir,
        "regenerate": args.regenerate,
        "results": results,
        "summary": summarize(results),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
