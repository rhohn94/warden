#!/usr/bin/env python3
"""catalog_conformance.py — the required-feature catalog's conformance
verification loop (#434, v3.97 R6 Pass 5).

The catalog's own SPEC concedes "implementing any catalog feature in a
managed app is out of scope for the catalog SPEC" (required-feature-
catalog.md, top matter) — `catalog_filing.py` files tickets, but nothing ever
re-checked whether a filed obligation was actually satisfied, and nothing
re-verified as the catalog grew. This module is that missing verification
half. It never files or closes a ticket itself (that stays
`catalog_filing.py` / the Reporter / `grm-fleet-audit`'s reconciliation job,
§Reconciliation below) — `plan()` is a pure, offline read+report, exactly
`catalog_filing.py`'s own `plan()` contract.

For each catalog entry this module:
  1. Evaluates the SAME `applies-when-family` / `applies-when` gates
     `catalog_filing.py` uses for filing applicability (reused directly, not
     re-derived — see `catalog_filing.eval_dial_predicate`), so "does this
     check even apply to this repo's profile" never drifts between the
     filing half and the conformance half.
  2. If the entry's catalog-declared `conformance-check:` field is the
     literal `exempt (...)` marker (spec-only / blocked-on-upstream entries
     — Entries 6/8 as of catalog-version 11), reports `exempt` — NOT silent
     omission; the exemption and its reason are always in the plan output.
  3. Otherwise dispatches to the entry's registered probe script (below) and
     normalizes its `ConformanceResult` into one of: `ok`, `warn`, `fail`,
     `degraded` (the probe script itself is unavailable/errored — a graceful
     degrade, not a crash; the honest reflection of the "root/claude-code
     drift" reality already flagged as a running pattern across this
     release's other follow-up notes).

`install_doctor.py` (both flavors) and `grm-fleet-audit`'s reconciliation
checklist item (§Step 4a, reference.md) are this module's two callers.

CLI:
    python3 catalog_conformance.py plan --root PATH [--family {cli,gui,lib,service,web}]
    python3 catalog_conformance.py --self-test

Design authority: required-feature-catalog.md (every entry's
`conformance-check:` field); catalog_filing.py (family/dial gating reused
verbatim).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog_filing  # noqa: E402
import admin_console_conformance as acc  # noqa: E402
import aura_channel_conformance as acnf  # noqa: E402
import standard_package_conformance as spc  # noqa: E402
import app_telemetry_conformance as atc  # noqa: E402
import logging_conformance as lgc  # noqa: E402

CATALOG_FILE = catalog_filing.CATALOG_FILE
CONFIG_FILE = catalog_filing.CONFIG_FILE
FAMILIES = catalog_filing.FAMILIES

# Actions a plan() entry can carry. Mirrors catalog_filing.py's action
# vocabulary shape (a small fixed set of strings) rather than a bespoke one.
ACTION_NOT_APPLICABLE = "not-applicable"
ACTION_EXEMPT = "exempt"
ACTION_OK = "ok"
ACTION_WARN = "warn"
ACTION_FAIL = "fail"
ACTION_DEGRADED = "degraded"
ACTION_UNREGISTERED = "unregistered"

# Statuses this module's OWN standalone `plan` CLI (main(), below) treats as
# a real problem for its exit code — deliberately narrower than
# install_doctor.py's own `_CATALOG_CONFORMANCE_PROBLEM_ACTIONS`, which
# additionally counts ACTION_WARN (install_doctor.py is what implements the
# WARN-by-default / --strict-escalates severity ramp this feature's
# acceptance criteria call for; this bare CLI has no --strict flag of its
# own, so ACTION_WARN — a soft finding — never fails it outright). The two
# sets answer different questions ("did anything definitively fail" here vs.
# "should this repo's health check flag it, and how loudly" there) and are
# kept as separate, independently-evolvable vocabularies on purpose — see
# install_doctor.py's own `_CATALOG_CONFORMANCE_PROBLEM_ACTIONS` docstring
# for its half of this split.
PROBLEM_ACTIONS = frozenset({ACTION_FAIL, ACTION_UNREGISTERED})


# ── Per-key dispatch registry ────────────────────────────────────────────────
#
# Each entry's probe returns an object exposing `.passed`, `.errors`,
# `.warnings`, `.report()` — the shape every conformance script in this skill
# directory (and fleet_conformance.py / dependency_channel_conformance.py)
# already shares. A key with no registered callable here is
# `unregistered` — the "#434 equivalent of #440's check-for-checks doctrine":
# a catalog entry that grows in the future without a paired conformance check
# (and without an explicit `exempt (...)` marker) is a loud finding, not a
# silent gap.

def _admin_console(root: Path):
    return acc.probe(root)


def _changelog_surface(root: Path):
    # grm-web-app-apply is a fixed sibling skill directory (mirrors
    # install_doctor.py's own sys.path-insert pattern for the same module).
    # Imported lazily/defensively: this module is deliberately NOT ported to
    # every flavor yet (a pre-existing, already-flagged root/claude-code gap
    # — see this file's module docstring §caller notes and
    # release-planning-v3.97.md §5's ITEM-10 follow-up), so an absent copy
    # must degrade gracefully here rather than crash the whole plan().
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "grm-web-app-apply"))
    import changelog_conformance  # noqa: E402
    return changelog_conformance.probe(root)


def _token_bookkeeper(root: Path):
    return spc.probe(root, "token-bookkeeper", offline=True)


def _gatekeeper(root: Path):
    return spc.probe(root, "gatekeeper", offline=True)


def _recordkeeper(root: Path):
    return spc.probe(root, "recordkeeper", offline=True)


def _aura(root: Path):
    return acnf.probe(root)


def _app_telemetry(root: Path):
    return atc.probe(root)


def _logging(root: Path):
    # Offline static call-site scan only — the live `--boot-probe CMD` leg
    # needs a spawnable entrypoint the caller supplies, exactly like
    # admin_console's `--url` leg is never auto-dispatched here either.
    return lgc.probe(root)


CHECK_REGISTRY: dict[str, Callable[[Path], object]] = {
    "admin-console": _admin_console,
    "changelog-surface": _changelog_surface,
    "adopt-token-bookkeeper": _token_bookkeeper,
    "adopt-gatekeeper": _gatekeeper,
    "adopt-recordkeeper": _recordkeeper,
    "aura-via-dependency-channel": _aura,
    "app-telemetry": _app_telemetry,
    "structured-logging": _logging,
    # "adopt-meta-updater" and "adopt-fleet-contract" are intentionally
    # ABSENT here — both carry an `exempt (...)` conformance-check marker in
    # the catalog itself (blocked-on-upstream, no published crate to probe),
    # handled by the `exempt` branch in plan() below before dispatch is ever
    # attempted. Their absence from this registry is therefore NOT an
    # unregistered gap.
}

_EXEMPT_KEYS = frozenset({"adopt-meta-updater", "adopt-fleet-contract"})


def _normalize(key: str, outcome_obj) -> tuple[str, str]:
    """(action, detail) from a probe's ConformanceResult-shaped object."""
    detail = "; ".join(outcome_obj.errors) if outcome_obj.errors else (
        "; ".join(outcome_obj.warnings) if outcome_obj.warnings else
        "; ".join(outcome_obj.info) if outcome_obj.info else "no findings")
    if outcome_obj.errors:
        return ACTION_FAIL, detail
    if outcome_obj.warnings:
        return ACTION_WARN, detail
    return ACTION_OK, detail


def plan(root: str, family: str, catalog_path: str = CATALOG_FILE) -> list[dict]:
    """Evaluate every catalog entry's conformance state against (root,
    family). One result dict per entry:
    {key, action, detail}. Never mutates anything — pure read+report,
    mirroring catalog_filing.plan()'s own contract."""
    if family not in FAMILIES:
        raise catalog_filing.CatalogError(
            f"unknown family {family!r}; expected one of {FAMILIES}")

    _version, entries = catalog_filing.load_catalog(catalog_path)
    config = catalog_filing.load_json_or_empty(
        os.path.join(root, CONFIG_FILE))
    root_path = Path(root)

    results: list[dict] = []
    for entry in entries:
        if family not in entry.applies_when_family:
            results.append({"key": entry.key, "action": ACTION_NOT_APPLICABLE,
                             "detail": f"family {family!r} not in "
                                       f"{entry.applies_when_family}"})
            continue

        # Exemption is checked BEFORE the dial gate, deliberately: this
        # module's own contract (see docstring point 2) is that an exempt
        # entry's exemption is ALWAYS in the plan output once its family
        # gate passes, never silently reported as merely not-applicable
        # because its (irrelevant, since there's no check to gate anyway)
        # config dial happens to be unset. A reader scanning for "which
        # entries are exempt" must see every exempt entry every time its
        # family applies, independent of dial state.
        conformance_field = (entry.conformance_check or "").strip()
        if conformance_field.startswith("exempt") or entry.key in _EXEMPT_KEYS:
            reason = conformance_field or (
                f"{entry.key} carries status: {entry.status} with no "
                f"conformance-check field")
            results.append({"key": entry.key, "action": ACTION_EXEMPT,
                             "detail": reason})
            continue

        # Entry 7's applies-when is a repo-state predicate, not a dial —
        # eval_dial_predicate() returns None for it (not machine-evaluable
        # for FILING purposes). For CONFORMANCE purposes this is fine: the
        # dispatched probe script IS the repo-state detector, so a None gate
        # result falls through to dispatch rather than being treated as
        # not-applicable (unlike catalog_filing.plan()'s manual-review).
        if entry.applies_when:
            gate = catalog_filing.eval_dial_predicate(entry.applies_when, config)
            if gate is False:
                results.append({"key": entry.key,
                                 "action": ACTION_NOT_APPLICABLE,
                                 "detail": f"applies-when false: "
                                           f"{entry.applies_when!r}"})
                continue

        probe_fn = CHECK_REGISTRY.get(entry.key)
        if probe_fn is None:
            results.append({
                "key": entry.key, "action": ACTION_UNREGISTERED,
                "detail": (
                    f"entry {entry.key!r} has no exempt marker and no "
                    f"registered conformance probe in catalog_conformance.py "
                    f"— a check-for-checks-style gap (doctrine: ITEM-1/#440)."
                )})
            continue

        try:
            outcome_obj = probe_fn(root_path)
        except ImportError as exc:
            results.append({
                "key": entry.key, "action": ACTION_DEGRADED,
                "detail": f"conformance probe module unavailable in this "
                          f"flavor ({exc}) — degraded, not failed."})
            continue
        except Exception as exc:  # pragma: no cover - defensive
            results.append({
                "key": entry.key, "action": ACTION_DEGRADED,
                "detail": f"conformance probe raised {exc!r} — degraded, "
                          f"not failed."})
            continue

        action, detail = _normalize(entry.key, outcome_obj)
        results.append({"key": entry.key, "action": action, "detail": detail})

    return results


# ── Self-test ─────────────────────────────────────────────────────────────

def _self_test() -> int:
    import tempfile
    import shutil

    cases: list[tuple[str, bool]] = []

    # --- Every real catalog entry is either registered or explicitly
    # exempt — the check-for-checks regression guard this module exists to
    # enforce on itself. ---
    _version, real_entries = catalog_filing.load_catalog()
    by_key = {e.key: e for e in real_entries}
    cases.append(("real catalog has 10 entries", len(real_entries) == 10))
    for entry in real_entries:
        field = (entry.conformance_check or "")
        registered = entry.key in CHECK_REGISTRY
        exempt = field.strip().startswith("exempt") or entry.key in _EXEMPT_KEYS
        cases.append((
            f"entry {entry.key!r} is registered XOR explicitly exempt",
            registered != exempt))
    cases.append(("adopt-meta-updater is the exempt marker (blocked-on-upstream)",
                  (by_key["adopt-meta-updater"].conformance_check or "")
                  .strip().startswith("exempt")))
    cases.append(("adopt-fleet-contract is the exempt marker (blocked-on-upstream)",
                  (by_key["adopt-fleet-contract"].conformance_check or "")
                  .strip().startswith("exempt")))

    # --- plan() against a synthetic empty repo: family gate + exemptions
    # resolve correctly, no crash across all 5 families. ---
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, ".claude"), exist_ok=True)
        for fam in FAMILIES:
            results = plan(tmp, fam)
            by_key_r = {r["key"]: r for r in results}
            cases.append((f"family={fam}: plan() returns all 10 entries",
                          len(results) == 10))
            cases.append((f"family={fam}: exempt entries always report exempt "
                          "when their family gate passes",
                          all(by_key_r[k]["action"] in
                              (ACTION_EXEMPT, ACTION_NOT_APPLICABLE)
                              for k in _EXEMPT_KEYS)))
            cases.append((f"family={fam}: no entry is ever 'unregistered' "
                          "against the real catalog",
                          all(r["action"] != ACTION_UNREGISTERED
                              for r in results)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- web family, empty repo: admin-console + changelog-surface + aura
    # dispatch (not gated by a config dial); the three standard-package
    # entries (3/4/5) resolve not-applicable (their dials are unset). ---
    tmp2 = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp2, ".claude"), exist_ok=True)
        with open(os.path.join(tmp2, CONFIG_FILE), "w") as fh:
            json.dump({"web-app": {"value": "yes", "fleet-participant":
                       {"value": "yes"}}}, fh)
        # Empty src/ dir (present but with nothing in it) so admin-console's
        # static scan resolves "not-found" (a real WARN input) rather than
        # "not-applicable" (no src/templates tree at all — informational,
        # never warns).
        os.makedirs(os.path.join(tmp2, "src"), exist_ok=True)
        results = {r["key"]: r for r in plan(tmp2, "web")}
        cases.append(("web-app.value alone: gatekeeper is not-applicable "
                      "(finer web-app.auth dial unset)",
                      results["adopt-gatekeeper"]["action"] ==
                      ACTION_NOT_APPLICABLE))
        cases.append(("web-app.value alone: recordkeeper is not-applicable "
                      "(finer web-app.persistence dial unset)",
                      results["adopt-recordkeeper"]["action"] ==
                      ACTION_NOT_APPLICABLE))
        cases.append(("web-app.value alone: token-bookkeeper is "
                      "not-applicable (web-app.agentic dial unset)",
                      results["adopt-token-bookkeeper"]["action"] ==
                      ACTION_NOT_APPLICABLE))
        cases.append(("admin-console dispatches (unconditional for web) and "
                      "warns on a repo with an empty src/ (no static route "
                      "ref found)",
                      results["admin-console"]["action"] == ACTION_WARN))
        cases.append(("aura dispatches (family-spanning, no dial) and passes "
                      "on a repo with no Aura consumption at all",
                      results["aura-via-dependency-channel"]["action"] ==
                      ACTION_OK))
        cases.append(("structured-logging dispatches (unconditional for web) "
                      "and warns on a repo with an empty src/ (no "
                      "logging-init call site found)",
                      results["structured-logging"]["action"] == ACTION_WARN))
        cases.append(("adopt-meta-updater is exempt even though "
                      "web-app.value == yes matches its own applies-when",
                      results["adopt-meta-updater"]["action"] == ACTION_EXEMPT))
        cases.append(("adopt-fleet-contract is exempt even though its own "
                      "applies-when (fleet-participant == yes) matches — "
                      "exemption wins over an otherwise-applicable gate",
                      results["adopt-fleet-contract"]["action"] == ACTION_EXEMPT))

        # The other direction of the same precedence rule: exemption must
        # ALSO win when the dial gate would otherwise resolve
        # not-applicable (dial unset) — an exempt entry is always exempt in
        # the output, never silently downgraded to not-applicable just
        # because there was no real check to gate anyway.
        with open(os.path.join(tmp2, CONFIG_FILE), "w") as fh:
            json.dump({"web-app": {"value": "yes"}}, fh)  # fleet-participant unset
        results_no_dial = {r["key"]: r for r in plan(tmp2, "web")}
        cases.append(("adopt-fleet-contract is STILL exempt (not "
                      "not-applicable) when its own applies-when dial is "
                      "unset — exemption precedes the dial gate",
                      results_no_dial["adopt-fleet-contract"]["action"] ==
                      ACTION_EXEMPT))
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    # --- Declaring the finer dials makes gatekeeper/recordkeeper dispatch
    # (rather than not-applicable) — proves the reused dial-gate wiring
    # actually connects through to dispatch, not just to filing. ---
    tmp3 = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp3, ".claude"), exist_ok=True)
        with open(os.path.join(tmp3, CONFIG_FILE), "w") as fh:
            json.dump({"web-app": {
                "value": "yes", "auth": {"value": "yes"},
                "persistence": {"value": "yes"}}}, fh)
        results = {r["key"]: r for r in plan(tmp3, "web")}
        cases.append(("web-app.auth=yes: gatekeeper dispatches (not "
                      "not-applicable) on an empty repo -> not-adopted, "
                      "still an 'ok' informational outcome",
                      results["adopt-gatekeeper"]["action"] == ACTION_OK))
        cases.append(("web-app.persistence=yes: recordkeeper dispatches",
                      results["adopt-recordkeeper"]["action"] == ACTION_OK))
    finally:
        shutil.rmtree(tmp3, ignore_errors=True)

    lines, passed, failed = [], 0, 0
    for label, ok in cases:
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    print("catalog_conformance.py --self-test")
    print("\n".join(lines))
    print(f"\n{passed}/{passed + failed} self-test cases passed")
    return 0 if failed == 0 else 1


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Conformance-verification loop for the required-feature "
                    "catalog (#434).")
    ap.add_argument("verb", nargs="?", help="plan")
    ap.add_argument("--root", default=".")
    ap.add_argument("--family", choices=FAMILIES)
    ap.add_argument("--catalog", default=CATALOG_FILE)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.verb:
        ap.error("a verb is required (plan) or --self-test")

    if args.verb == "plan":
        if not args.family:
            ap.error("plan requires --family")
        try:
            results = plan(args.root, args.family, catalog_path=args.catalog)
        except catalog_filing.CatalogError as exc:
            print(f"catalog_conformance: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(results, indent=2, ensure_ascii=False))
        problems = [r for r in results if r["action"] in PROBLEM_ACTIONS]
        return 1 if problems else 0
    ap.error(f"unknown verb: {args.verb}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
