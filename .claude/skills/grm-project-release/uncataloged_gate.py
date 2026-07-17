#!/usr/bin/env python3
"""uncataloged_gate.py — merge-boundary/closeout gate: the component
registry's `uncataloged` count must not grow across a release (#459, v3.97).

Backs `grm-project-release`'s closeout sequence, wired immediately after the
"Refresh component registry" step (#458) — a DISJOINT, mechanical check, not
a duplicate of it: that step keeps `.claude/component-registry.json` fresh
against its current sources (a snapshot operation, `component_registry.py
build`); this script compares two snapshots — the registry as committed at
the previous release tag ("before") against the just-refreshed on-disk
registry ("after") — and reports whether the count of `uncataloged`
(metadata-less reusable units) grew. Growth is the mechanical signal that
the write-time `component.json` done-criteria (CLAUDE.md + all three
paradigm task-execution templates, #459) isn't holding in practice: new or
materially-reshaped components are landing without their own
`component.json` faster than authoring (or the one-time
`grm-component-backfill` sweep, #460) clears the backlog.

Report-only today (WARN, exit 0 regardless of verdict) — the same severity
ramp `install_doctor.py`'s `sig-mismatch` check (#433) established: WARN
everywhere by default, promote to a hard block only under `--strict`, once a
project has actually driven its `uncataloged` backlog down to a stable
baseline worth protecting. This is a deliberate initial posture, not a
placeholder for a "real" gate later — see release-planning-v3.97.md ITEM-8's
acceptance criteria ("report-only mode acceptable initially").

Degrade-gracefully contract (mirrors component_registry.py / reuse_gate.py):
  - No `.claude/component-registry.json` on disk at all (this repo's own
    real case today — no `components/`/`lib/` directory, confirmed out of
    scope for self-population per release-planning-v3.97.md §4) -> no-op,
    exit 0. Nothing to gate.
  - No previous release tag reachable from HEAD (the first-ever tagged
    release) -> no-op, exit 0. A first population is never itself "growth" —
    there is nothing to compare against yet.
  - A registry file that existed at the resolved baseline tag but is
    genuinely absent there (adopted the registry convention mid-release) ->
    no-op, exit 0, same reasoning.
  - A PRESENT-but-malformed registry (current on-disk OR the committed
    baseline snapshot) is a real authoring/parse bug, not a degrade path ->
    raises `GateError` (exit 2).

Standard: Python 3 stdlib-only (docs/grimoire/design/scripting-unification-design.md §3).

CLI:
  uncataloged_gate.py check   [--root DIR] [--baseline-ref REF] [--strict]
  uncataloged_gate.py compare --before FILE --after FILE [--strict]
  uncataloged_gate.py --self-test
Exit 0 unless `--strict` is given AND the uncataloged count grew (exit 1), or
a present registry snapshot (current or baseline) is malformed JSON (exit 2).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# ── Constants (no magic numbers / strings inline) ───────────────────────────
REGISTRY_PATH = os.path.join(".claude", "component-registry.json")
RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+(\.\d+)?$")
JSON_INDENT = 2


class GateError(Exception):
    """Raised only for a present-but-malformed registry snapshot (exit 2)."""


# ── Pure comparison (the core self-tested with fixture registries) ─────────
def _uncataloged(registry):
    return sorted(set((registry or {}).get("uncataloged") or []))


def compare(before_registry: dict | None, after_registry: dict | None) -> dict:
    """Compare two registry snapshots' `uncataloged` lists.

    Count-based, not set-identity-based: the gate cares whether the BACKLOG
    SIZE grew, matching the acceptance criteria's own wording ("the
    registry's `uncataloged` count must not grow"). `added`/`removed` are
    still reported for visibility even when the count itself is unchanged
    (membership can churn — e.g. one component gets catalogued while a
    different one newly appears uncataloged — without the count moving).
    """
    before = _uncataloged(before_registry)
    after = _uncataloged(after_registry)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    if len(after) > len(before):
        verdict = "grew"
    elif len(after) < len(before):
        verdict = "shrank"
    else:
        verdict = "stable"
    return {
        "before-count": len(before),
        "after-count": len(after),
        "added": added,
        "removed": removed,
        "verdict": verdict,
        "grew": verdict == "grew",
    }


def _load_registry_text(text: str, label: str) -> dict:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise GateError("%s is malformed JSON: %s" % (label, exc))


# ── git-backed baseline resolution (`check` mode only) ──────────────────────
def _git(args: list[str], root: str) -> str | None:
    try:
        res = subprocess.run(["git"] + args, cwd=root, capture_output=True,
                              text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return res.stdout.strip()


def _find_baseline_tag(root: str) -> str | None:
    """The previous release's tag — i.e. the SECOND-newest release tag
    reachable from HEAD, by version sort.

    `grm-project-release`'s closeout sequence runs strictly post-tag (both
    "Reconcile issues" and "Refresh component registry" are documented
    post-tag steps) — the newest reachable tag is ALWAYS this release's own
    just-cut tag by the time this gate runs, regardless of how many further
    commits (reconcile, registry-refresh) have since moved HEAD past it. So
    the baseline is unconditionally the second-newest tag, not "the newest
    tag not exactly at HEAD" (that weaker rule breaks the moment even one
    follow-up commit lands after the tag, which is the common case here).
    Fewer than two release tags reachable (the first-ever tagged release, or
    a repo/worktree with no tags at all) -> no baseline, caller degrades.
    """
    out = _git(["tag", "--merged", "HEAD", "--sort=-version:refname"], root)
    if not out:
        return None
    tags = [t.strip() for t in out.splitlines() if RELEASE_TAG_RE.match(t.strip())]
    if len(tags) < 2:
        return None
    return tags[1]


def _registry_at_ref(root: str, ref: str) -> dict | None:
    """Registry dict as committed at `ref`, or None if absent at that ref."""
    git_path = REGISTRY_PATH.replace(os.sep, "/")
    out = _git(["show", "%s:%s" % (ref, git_path)], root)
    if out is None:
        return None
    return _load_registry_text(out, "%s:%s" % (ref, git_path))


def _registry_on_disk(root: str) -> dict | None:
    path = os.path.join(root, REGISTRY_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    return _load_registry_text(text, REGISTRY_PATH)


def run_check(root: str = ".", baseline_ref: str | None = None) -> dict:
    """The closeout entry point: compare the on-disk (just-refreshed)
    registry against the previous release tag's committed registry.
    """
    after = _registry_on_disk(root)
    if after is None:
        return {"no-op": True, "note": "no .claude/component-registry.json "
                 "on disk — nothing to gate"}
    ref = baseline_ref or _find_baseline_tag(root)
    if not ref:
        return {"no-op": True, "note": "no previous release tag reachable "
                 "from HEAD — nothing to compare against yet (first tagged "
                 "release, or run outside a git repo)"}
    before = _registry_at_ref(root, ref)
    if before is None:
        return {"no-op": True, "note": "component-registry.json did not "
                 "exist at baseline ref %s — first population is never "
                 "itself growth" % ref}
    result = compare(before, after)
    result["no-op"] = False
    result["baseline-ref"] = ref
    return result


# ── Self-test (fixtures + a throwaway temp git repo; never this repo's real
#    sources or tags) ────────────────────────────────────────────────────
def _reg(uncataloged: list[str]) -> dict:
    return {"registry-version": 1, "generated-from": ["components/"],
            "components": {}, "uncataloged": uncataloged, "unknown-tags": []}


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _write_json(path: str, obj: dict) -> None:
    _write(path, json.dumps(obj))


def _self_test() -> int:
    import tempfile

    failures = []

    # ── compare(): pure fixture-registry snapshots (before/after) ──────────
    # 1) Growing count -> grew, added reported.
    res = compare(_reg(["a/", "b/"]), _reg(["a/", "b/", "c/"]))
    if not res["grew"] or res["verdict"] != "grew":
        failures.append("growing fixture: expected grew=True: %r" % res)
    if res["added"] != ["c/"]:
        failures.append("growing fixture: expected added=['c/']: %r" % res)
    if res["before-count"] != 2 or res["after-count"] != 3:
        failures.append("growing fixture: wrong counts: %r" % res)

    # 2) Shrinking count -> not grown, verdict shrank, removed reported.
    res = compare(_reg(["a/", "b/", "c/"]), _reg(["a/"]))
    if res["grew"] or res["verdict"] != "shrank":
        failures.append("shrinking fixture: expected grew=False/shrank: %r" % res)
    if res["removed"] != ["b/", "c/"]:
        failures.append("shrinking fixture: expected removed=['b/','c/']: %r" % res)

    # 3) Stable — identical membership.
    res = compare(_reg(["a/"]), _reg(["a/"]))
    if res["grew"] or res["verdict"] != "stable":
        failures.append("stable fixture: expected grew=False/stable: %r" % res)

    # 3b) Same COUNT, different membership -> still not "grew" (the gate is
    #     count-based per the acceptance criteria), but the churn is still
    #     surfaced via added/removed.
    res = compare(_reg(["a/"]), _reg(["b/"]))
    if res["grew"] or res["verdict"] != "stable":
        failures.append("swap fixture: same count, different membership "
                         "must not read as growth: %r" % res)
    if res["added"] != ["b/"] or res["removed"] != ["a/"]:
        failures.append("swap fixture: added/removed should still surface "
                         "the membership change: %r" % res)

    # 4) Both empty -> stable.
    res = compare(_reg([]), _reg([]))
    if res["grew"] or res["verdict"] != "stable":
        failures.append("empty fixture: expected stable: %r" % res)

    # ── run_check(): git-tag-baseline resolution + degrade paths, against a
    #    throwaway temp git repo (never this repo's real tags/history) ─────
    with tempfile.TemporaryDirectory() as root:
        _git(["init", "-q"], root)
        _git(["config", "user.email", "t@example.com"], root)
        _git(["config", "user.name", "t"], root)
        _write(os.path.join(root, "README.md"), "x\n")
        _git(["add", "-A"], root)
        _git(["commit", "-q", "-m", "init"], root)

        # 5) No registry on disk at all -> no-op.
        res = run_check(root)
        if not res.get("no-op"):
            failures.append("no-registry-on-disk: expected no-op: %r" % res)

        # 6) Registry present, zero release tags exist yet -> no-op (nothing
        #    to compare against).
        _write_json(os.path.join(root, REGISTRY_PATH), _reg(["a/"]))
        _git(["add", "-A"], root)
        _git(["commit", "-q", "-m", "seed registry"], root)
        res = run_check(root)
        if not res.get("no-op"):
            failures.append("zero-tags: expected no-op: %r" % res)

        # 7) First-ever tagged release (exactly ONE release tag reachable)
        #    -> still no-op, even though a tag now exists: there is no
        #    PREVIOUS release to compare against.
        _git(["tag", "-a", "v1.0", "-m", "v1.0"], root)
        res = run_check(root)
        if not res.get("no-op"):
            failures.append("first-tagged-release: expected no-op (no prior "
                             "release to compare against): %r" % res)

        # 8) Second release: uncataloged grows to 3, tag v1.1 cut. Closeout
        #    now runs post-tag with HEAD at v1.1 exactly — baseline must
        #    resolve to the PREVIOUS release (v1.0, uncataloged=["a/"]), not
        #    v1.1 itself, and the comparison must read as real growth.
        _write_json(os.path.join(root, REGISTRY_PATH), _reg(["a/", "b/", "c/"]))
        _git(["add", "-A"], root)
        _git(["commit", "-q", "-m", "second release work"], root)
        _git(["tag", "-a", "v1.1", "-m", "v1.1"], root)
        res = run_check(root)
        if res.get("no-op"):
            failures.append("post-v1.1-growth: expected a real comparison, "
                             "not a no-op: %r" % res)
        elif not res["grew"] or res["baseline-ref"] != "v1.0":
            failures.append("post-v1.1-growth: expected grew vs baseline "
                             "v1.0: %r" % res)

        # 9) Same baseline (still v1.0, no new tag yet), on-disk shrinks back
        #    to a single entry -> not grown.
        _write_json(os.path.join(root, REGISTRY_PATH), _reg(["a/"]))
        res = run_check(root)
        if res.get("no-op") or res["grew"] or res["baseline-ref"] != "v1.0":
            failures.append("post-v1.1-shrink: expected grew=False vs v1.0: "
                             "%r" % res)

        # 10) Third release cut (v1.2) -> baseline advances to the
        #     IMMEDIATELY previous tag (v1.1), not further back to v1.0.
        _write_json(os.path.join(root, REGISTRY_PATH), _reg(["a/", "d/"]))
        _git(["add", "-A"], root)
        _git(["commit", "-q", "-m", "third release work"], root)
        _git(["tag", "-a", "v1.2", "-m", "v1.2"], root)
        res = run_check(root)
        if res.get("no-op") or res["baseline-ref"] != "v1.1":
            failures.append("post-v1.2: expected baseline to advance to "
                             "v1.1, not v1.0: %r" % res)

        # 11) --baseline-ref override bypasses auto-detection.
        res = run_check(root, baseline_ref="v1.0")
        if res.get("no-op") or res["baseline-ref"] != "v1.0":
            failures.append("baseline-ref override ignored: %r" % res)

        # 12) Malformed on-disk registry -> raises, does not degrade.
        _write(os.path.join(root, REGISTRY_PATH), "{not json")
        try:
            run_check(root)
        except GateError:
            pass
        else:
            failures.append("malformed on-disk registry should raise, not degrade")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("uncataloged_gate self-test: OK (compare: growing/shrinking/stable/"
          "same-count-different-membership/empty fixtures; run_check: "
          "no-registry no-op, zero-tags no-op, first-tagged-release no-op, "
          "real growth vs the previous-release tag, shrink, baseline "
          "advances across a third release, --baseline-ref override, "
          "malformed-registry raise)")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mechanical closeout gate: the component registry's "
                     "`uncataloged` count must not grow across a release (#459).")
    ap.add_argument("verb", nargs="?", help="check|compare")
    ap.add_argument("--root", default=".")
    ap.add_argument("--baseline-ref", default=None,
                     help="override the auto-detected previous-release tag "
                          "(check mode only)")
    ap.add_argument("--before", help="baseline registry JSON file (compare mode)")
    ap.add_argument("--after", help="current registry JSON file (compare mode)")
    ap.add_argument("--strict", action="store_true",
                     help="exit 1 when uncataloged grew (default: report-only, exit 0)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.verb:
        ap.error("a verb is required (check|compare) or --self-test")

    try:
        if args.verb == "check":
            result = run_check(args.root, args.baseline_ref)
        elif args.verb == "compare":
            if not args.before or not args.after:
                ap.error("compare requires --before and --after")
            with open(args.before, encoding="utf-8") as fh:
                before = _load_registry_text(fh.read(), args.before)
            with open(args.after, encoding="utf-8") as fh:
                after = _load_registry_text(fh.read(), args.after)
            result = compare(before, after)
            result["no-op"] = False
        else:
            ap.error("unknown verb: %s" % args.verb)
    except GateError as exc:
        print("uncataloged_gate: %s" % exc, file=sys.stderr)
        return 2

    print(json.dumps(result, indent=JSON_INDENT, ensure_ascii=False))
    if args.strict and not result.get("no-op") and result.get("grew"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
