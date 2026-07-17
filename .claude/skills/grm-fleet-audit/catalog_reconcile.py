#!/usr/bin/env python3
"""catalog_reconcile.py — grm-fleet-audit's catalog-conformance reconciliation
checklist item (#434, v3.97 R6 Pass 5, reference.md §Step 4a).

The required-feature catalog's filing flow already tags every issue it files
with a dedupe key in the title — `[key: <key>]` (required-feature-catalog.md
§Filing contract). This module is the reconciliation half: given (a) a fleet
repo's current `catalog_conformance.py plan()` output and (b) that repo's open
+ closed `Grimoire-Requirement`-labeled issues, decide which filed tickets
should be CLOSED (their obligation is now verifiably satisfied) and which
should be REOPENED (closed once, but the deterministic check now fails again —
a regression). This is the "verified-or-reopened" reconciliation this release
item (#434) exists to build, mirroring `grm-issue-reconcile`'s own
evidence-and-marker discipline rather than inventing a new one.

Pure logic, no tracker I/O — `reconcile()` takes already-fetched issue and
conformance data and returns a plan of actions; nothing here calls
`issue_tracker.py` directly (mirrors `capability_overlap.py`'s own
extraction-ticket-draft pattern: this script computes WHAT to do, the SKILL.md
procedure — reference.md §Step 4a — is what actually calls
`grm-issue-tracker` to do it, going through the standard abstraction like
every other write in `grm-fleet-audit`). This split is exactly what makes the
reconciliation logic self-testable via fixtures without a live fleet or a
real issue tracker (release-planning-v3.97.md §4's engagement-scope
constraint — same pattern ITEM-6/#412's `capability_overlap.py` already
established in this release).

Five possible outcomes per (key, issue) pair:
  close-as-verified — issue is OPEN, the deterministic check now passes. The
                       filed obligation is satisfied; close it.
  reopen            — issue is CLOSED, the deterministic check now FAILS. A
                       regression: something that was verified once is broken
                       again (or was closed prematurely without ever having
                       been true). Reopen it.
  already-verified  — issue is CLOSED, check passes. No-op — nothing to do.
  still-open        — issue is OPEN, check still fails/warns. No-op — working
                       as intended, nothing to reconcile yet.
  no-op-exempt      — the catalog entry is exempt/not-applicable/degraded for
                       this repo's profile (blocked-on-upstream, wrong family,
                       or a probe unavailable in this flavor) — reconciliation
                       never touches a ticket it cannot deterministically
                       judge.
Plus one entry-level (no issue) outcome:
  unfiled           — the catalog key applies and its check ran, but no
                       matching `[key: ...]`-titled issue exists at all. Out
                       of this reconciliation's scope (filing is
                       catalog_filing.py / the Reporter's job, not this
                       module's) — reported for visibility only.

CLI:
    python3 catalog_reconcile.py --self-test

There is deliberately no live-fleet CLI entry point here (unlike
capability_overlap.py's `scan --repo ...`) — reconciliation always needs BOTH
a live conformance run (catalog_conformance.py, needs a real repo checkout)
AND a live issue-tracker query (grm-issue-tracker, needs tracker credentials),
so the actual live invocation is a short SKILL.md procedure (reference.md
§Step 4a) composing those two abstractions and this module's pure
`reconcile()`, not a single self-contained script call.

Design authority: required-feature-catalog.md §Filing contract (the
`[key: ...]` convention this module parses); grm-fleet-audit/reference.md
§Step 4a; grm-issue-reconcile (the evidence-and-marker discipline this
mirrors).
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Optional

# Matches the filing flow's exact dedupe-key convention:
# "[key: adopt-gatekeeper] Adopt the gatekeeper standard package (...)"
_KEY_RE = re.compile(r"\[key:\s*([a-z0-9][a-z0-9-]*)\]")

# catalog_conformance.py actions that mean "the deterministic check actually
# ran and rendered a real verdict" — the only actions reconciliation acts on.
_VERDICT_OK = "ok"
_VERDICT_PROBLEM = frozenset({"warn", "fail", "unregistered"})
# Actions where reconciliation must stay hands-off (no deterministic verdict
# to reconcile against): not-applicable / exempt / degraded.
_VERDICT_NO_OP = frozenset({"not-applicable", "exempt", "degraded"})

ACTION_CLOSE_AS_VERIFIED = "close-as-verified"
ACTION_REOPEN = "reopen"
ACTION_ALREADY_VERIFIED = "already-verified"
ACTION_STILL_OPEN = "still-open"
ACTION_NO_OP_EXEMPT = "no-op-exempt"
ACTION_UNFILED = "unfiled"

STATE_OPEN = "open"
STATE_CLOSED = "closed"


def parse_key(title: str) -> Optional[str]:
    """Extract the `[key: <key>]` dedupe key from an issue title, or None."""
    m = _KEY_RE.search(title or "")
    return m.group(1) if m else None


def reconcile(issues: list[dict], conformance: dict[str, str]) -> list[dict]:
    """Compute the reconciliation plan.

    `issues`: a list of {"number": int, "title": str, "state": "open"|"closed"}
    dicts — the shape `issue_tracker.py search`/`list` already returns
    (trimmed to the fields this function needs).

    `conformance`: {key: action} — the per-entry `action` string from
    `catalog_conformance.plan()`'s result list (e.g. "ok", "warn", "fail",
    "exempt", "not-applicable", "degraded", "unregistered").

    Returns one dict per (key, issue) pair actually reasoned about:
    {"key": str, "issue_number": int | None, "action": str, "reason": str}.
    Never mutates either input; never calls a tracker. `action` in
    ACTION_UNFILED carries issue_number=None (no matching issue found).
    """
    by_key: dict[str, list[dict]] = {}
    for issue in issues:
        key = parse_key(issue.get("title", ""))
        if key:
            by_key.setdefault(key, []).append(issue)

    plan: list[dict] = []
    for key, verdict in conformance.items():
        matching = by_key.get(key, [])

        if verdict in _VERDICT_NO_OP:
            for issue in matching:
                plan.append({
                    "key": key, "issue_number": issue["number"],
                    "action": ACTION_NO_OP_EXEMPT,
                    "reason": f"catalog entry {key!r} is {verdict!r} for this "
                              f"repo's profile — no deterministic verdict to "
                              f"reconcile against."})
            continue

        if not matching:
            plan.append({
                "key": key, "issue_number": None, "action": ACTION_UNFILED,
                "reason": f"catalog entry {key!r} applies and its check ran "
                          f"({verdict!r}) but no [key: {key}]-titled issue "
                          f"was found — filing is catalog_filing.py's job, "
                          f"out of this reconciliation's scope."})
            continue

        for issue in matching:
            state = issue.get("state", STATE_OPEN)
            if verdict == _VERDICT_OK:
                if state == STATE_OPEN:
                    plan.append({
                        "key": key, "issue_number": issue["number"],
                        "action": ACTION_CLOSE_AS_VERIFIED,
                        "reason": f"deterministic conformance check for "
                                  f"{key!r} now passes — obligation "
                                  f"satisfied."})
                else:
                    plan.append({
                        "key": key, "issue_number": issue["number"],
                        "action": ACTION_ALREADY_VERIFIED,
                        "reason": f"already closed and {key!r} still "
                                  f"passes — no-op."})
            else:  # verdict in _VERDICT_PROBLEM
                if state == STATE_CLOSED:
                    plan.append({
                        "key": key, "issue_number": issue["number"],
                        "action": ACTION_REOPEN,
                        "reason": f"issue is closed but the deterministic "
                                  f"conformance check for {key!r} now FAILS "
                                  f"({verdict!r}) — regression, reopen."})
                else:
                    plan.append({
                        "key": key, "issue_number": issue["number"],
                        "action": ACTION_STILL_OPEN,
                        "reason": f"still open, check still {verdict!r} — "
                                  f"working as intended, no-op."})
    return plan


# ── Self-test ─────────────────────────────────────────────────────────────

def _self_test() -> int:
    cases: list[tuple[str, bool]] = []

    def issue(number: int, key: str, state: str, extra_title: str = "") -> dict:
        return {"number": number, "state": state,
                "title": f"[key: {key}] {extra_title or key}"}

    # --- parse_key: the exact filing-flow convention, plus non-matches. ---
    cases.append(("parse_key extracts a well-formed key",
                  parse_key("[key: adopt-gatekeeper] Adopt the gatekeeper "
                            "standard package") == "adopt-gatekeeper"))
    cases.append(("parse_key returns None for a title with no key marker",
                  parse_key("Some unrelated issue title") is None))
    cases.append(("parse_key returns None for an empty title",
                  parse_key("") is None))

    # --- THE ACCEPTANCE CASE 1: "a filed ticket that got fixed" — open
    # issue, check now passes -> close-as-verified. ---
    issues = [issue(101, "adopt-gatekeeper", STATE_OPEN)]
    conformance = {"adopt-gatekeeper": "ok"}
    plan = reconcile(issues, conformance)
    cases.append(("fixed ticket (open + now passing) -> close-as-verified",
                  len(plan) == 1 and plan[0]["action"] == ACTION_CLOSE_AS_VERIFIED
                  and plan[0]["issue_number"] == 101))

    # --- THE ACCEPTANCE CASE 2: "a filed ticket that's still broken" — open
    # issue, check still fails -> still-open (correct no-op, not reopened —
    # it was never closed). ---
    issues = [issue(102, "adopt-recordkeeper", STATE_OPEN)]
    conformance = {"adopt-recordkeeper": "fail"}
    plan = reconcile(issues, conformance)
    cases.append(("still-broken ticket (open + still failing) -> still-open",
                  len(plan) == 1 and plan[0]["action"] == ACTION_STILL_OPEN
                  and plan[0]["issue_number"] == 102))

    # --- Regression: closed issue, check now fails -> reopen. ---
    issues = [issue(103, "adopt-token-bookkeeper", STATE_CLOSED)]
    conformance = {"adopt-token-bookkeeper": "warn"}
    plan = reconcile(issues, conformance)
    cases.append(("regression (closed but now failing) -> reopen",
                  len(plan) == 1 and plan[0]["action"] == ACTION_REOPEN
                  and plan[0]["issue_number"] == 103))

    # --- Steady state: closed issue, check still passes -> already-verified
    # (no-op — must never re-comment/re-close every run). ---
    issues = [issue(104, "admin-console", STATE_CLOSED)]
    conformance = {"admin-console": "ok"}
    plan = reconcile(issues, conformance)
    cases.append(("steady state (closed + still passing) -> already-verified",
                  len(plan) == 1 and plan[0]["action"] == ACTION_ALREADY_VERIFIED))

    # --- Exempt/not-applicable/degraded entries are NEVER reconciled, even
    # with a matching issue in any state. ---
    for verdict in ("exempt", "not-applicable", "degraded"):
        issues = [issue(105, "adopt-meta-updater", STATE_OPEN)]
        conformance = {"adopt-meta-updater": verdict}
        plan = reconcile(issues, conformance)
        cases.append((f"verdict={verdict!r}: exempt entry -> no-op-exempt, "
                      f"never touched",
                      len(plan) == 1 and plan[0]["action"] == ACTION_NO_OP_EXEMPT))

    # --- unfiled: check ran with a real verdict but no matching issue at
    # all — reported, not silently dropped. ---
    conformance = {"aura-via-dependency-channel": "fail"}
    plan = reconcile([], conformance)
    cases.append(("no matching issue at all -> unfiled, issue_number=None",
                  len(plan) == 1 and plan[0]["action"] == ACTION_UNFILED
                  and plan[0]["issue_number"] is None))

    # --- A synthetic multi-entry, multi-issue fleet-shaped batch — proves
    # the reconciliation logic scales across a realistic mixed set in one
    # call, each pair resolved independently. ---
    issues = [
        issue(201, "adopt-gatekeeper", STATE_OPEN),      # -> close-as-verified
        issue(202, "adopt-recordkeeper", STATE_CLOSED),  # -> reopen (regressed)
        issue(203, "admin-console", STATE_OPEN),         # -> still-open
        issue(204, "changelog-surface", STATE_CLOSED),   # -> already-verified
    ]
    conformance = {
        "adopt-gatekeeper": "ok",
        "adopt-recordkeeper": "fail",
        "admin-console": "warn",
        "changelog-surface": "ok",
        "adopt-meta-updater": "exempt",       # no issue filed — not in `issues`
        "aura-via-dependency-channel": "not-applicable",
    }
    plan = reconcile(issues, conformance)
    by_num = {p["issue_number"]: p["action"] for p in plan if p["issue_number"]}
    cases.append(("mixed batch: gatekeeper closes",
                  by_num.get(201) == ACTION_CLOSE_AS_VERIFIED))
    cases.append(("mixed batch: recordkeeper reopens",
                  by_num.get(202) == ACTION_REOPEN))
    cases.append(("mixed batch: admin-console stays open",
                  by_num.get(203) == ACTION_STILL_OPEN))
    cases.append(("mixed batch: changelog-surface stays closed (no-op)",
                  by_num.get(204) == ACTION_ALREADY_VERIFIED))
    cases.append(("mixed batch: exempt/not-applicable entries with no "
                  "matching issue produce no plan rows at all (never "
                  "'unfiled' — that status is exempt from unfiled too)",
                  not any(p["key"] in ("adopt-meta-updater",
                                       "aura-via-dependency-channel")
                          for p in plan)))
    cases.append(("mixed batch: exactly 4 plan rows for 4 matched issues",
                  len(plan) == 4))

    lines, passed, failed = [], 0, 0
    for label, ok in cases:
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    print("catalog_reconcile.py --self-test")
    print("\n".join(lines))
    print(f"\n{passed}/{passed + failed} self-test cases passed")
    return 0 if failed == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fixture-testable verified-or-reopened reconciliation "
                    "logic for required-feature-catalog tickets (#434). See "
                    "grm-fleet-audit/reference.md §Step 4a for the live "
                    "invocation procedure (issue-tracker + "
                    "catalog_conformance.py composed by the SKILL.md).")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    ap.error("this module has no live CLI entry point — see the module "
             "docstring. Use --self-test to verify the reconciliation logic.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
