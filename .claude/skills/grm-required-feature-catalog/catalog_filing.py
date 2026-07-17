#!/usr/bin/env python3
"""catalog_filing.py — deterministic, offline planning engine for the
family-neutral required-feature catalog (#413, v3.97).

Through catalog-version 8 the catalog fired exactly once (onboarding /
`grm-web-app-apply`, web-app projects only) and its idempotency relied
entirely on an issue-tracker search at file-time (still the *authoritative*
dedupe — see required-feature-catalog.md's Filing contract). This module adds
a second, offline-checkable layer in front of that: a persisted per-project
state file lets `plan()` compute, without any network/issue-tracker call,
exactly which catalog entries are new-or-changed since the project's last run.
This is what makes the re-run behavior self-testable (component_registry.py's
"diff against the prior persisted artifact" pattern, applied to a filing
ledger instead of a registry).

This module never files a ticket itself — filing stays the Reporter's job
(grm-agent-reporter -> grm-feedback-to-issue), exactly as the pre-existing
filing contract already specifies. `plan()` only decides WHAT the caller
should do per entry; `record()` persists the outcome so the next `plan()` call
sees it.

Two independent gates, evaluated in order, decide whether an entry applies to
a given (root, family):

  1. `applies-when-family` — one or more of the five project families
     (cli/gui/lib/service/web, the same vocabulary
     `.claude/quick-start-templates/<family>/template.json`'s `profile` field
     uses). Absence-as-default is `web` (preserves every catalog-version-8
     entry's exact prior behavior — the catalog was invoked only for
     `web-app.value == "yes"` projects through that version).
  2. `applies-when` — the pre-existing single-equality config-dial predicate
     (unchanged grammar). A predicate that does not match the
     `<dot.path> == "<value>"` grammar (e.g. Entry 7's repo-state "detect"
     prose) is not machine-evaluable; `plan()` reports it as `manual-review`
     rather than guessing.

An entry may additionally declare `status: blocked-on-upstream` with an
`activation-event` (human prose) and an `activation-check`
(`vendor.toml:deps.<name>` — a plain-text scan for a `[deps.<name>]` table,
the same shape `dependency_channel_conformance.py` already checks). Such an
entry still gets filed (so the managed project's own planning can account for
the intended future adoption) but is planned as `file-blocked` rather than
`file`, and transitions to `activate` once its activation-check starts
passing.

CLI:
    python3 catalog_filing.py plan --root PATH --family {cli,gui,lib,service,web}
    python3 catalog_filing.py record --root PATH --key KEY --status {filed,blocked-on-upstream}
    python3 catalog_filing.py --self-test

Design authority: required-feature-catalog.md (this skill's sibling file)
§Re-running, §Conditional applicability, §Status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# grm-config-validate is a fixed sibling skill directory (mirrors the
# changelog_conformance.py -> config_validate.py pattern in grm-web-app-apply)
# — reuse its dialval() so the dotted-path / {"value": ...}-unwrap semantics
# never drift into a third copy.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-config-validate"))
import config_validate  # noqa: E402  (sys.path set immediately above)

CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "required-feature-catalog.md")
CONFIG_FILE = ".claude/grimoire-config.json"
STATE_FILE = ".claude/required-feature-catalog-state.json"
VENDOR_TOML = "vendor.toml"

FAMILIES = ("cli", "gui", "lib", "service", "web")
DEFAULT_FAMILY = "web"  # absence-as-default; preserves catalog-version-8 behavior

_ENTRY_HEADING_RE = re.compile(r"^###\s+Entry\s+\d+.*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```\n(.*?)\n```", re.DOTALL)
_FIELD_RE = re.compile(r"^([a-z][a-z-]*):\s*(.*)$")
_DIAL_PREDICATE_RE = re.compile(r'^([\w.-]+)\s*==\s*"([^"]*)"$')
_ACTIVATION_CHECK_RE = re.compile(r"^vendor\.toml:deps\.(\S+)$")


class CatalogError(Exception):
    pass


class Entry:
    """One parsed catalog entry (one '### Entry N — ...' section)."""

    def __init__(self, heading: str, block_text: str, fields: dict):
        self.heading = heading.strip()
        self.key = fields.get("key", "")
        self.name = fields.get("name", "")
        self.tag = fields.get("tag", "Grimoire-Requirement")
        family_raw = fields.get("applies-when-family", "")
        self.applies_when_family = (
            [f.strip() for f in family_raw.split(",") if f.strip()]
            if family_raw else [DEFAULT_FAMILY]
        )
        self.applies_when = fields.get("applies-when") or None
        self.status = fields.get("status", "filed")
        self.activation_event = fields.get("activation-event") or None
        self.activation_check = fields.get("activation-check") or None
        # Deterministic conformance-verification command for this entry
        # (#434, v3.97 R6 Pass 5) — either the standalone-runnable script/CLI
        # a caller invokes to verify adoption, or the literal string
        # "exempt (<reason>)" for a spec-only / blocked-on-upstream entry with
        # no upstream artifact to probe yet. Consumed by
        # `catalog_conformance.py`, never by this module's own plan()/record()
        # (filing applicability is unaffected by this field).
        self.conformance_check = fields.get("conformance-check") or None
        # Content hash covers the whole entry block (heading through the next
        # entry heading or EOF) — any wording change anywhere in the entry
        # (spec, sub-requirements, issue template) counts as "changed" for
        # re-run purposes. Deliberately conservative/simple over precise.
        # .strip() so a neighbor's block-boundary whitespace (the blank line
        # separating this entry from the next one, present only when a
        # following entry exists) never counts as a content change.
        self.content_hash = hashlib.sha256(
            block_text.strip().encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "tag": self.tag,
            "applies-when-family": self.applies_when_family,
            "applies-when": self.applies_when, "status": self.status,
            "activation-event": self.activation_event,
            "activation-check": self.activation_check,
            "conformance-check": self.conformance_check,
            "content-hash": self.content_hash,
        }


def _parse_fields(fenced_block: str) -> dict:
    """Parse 'key: value' lines from the first fenced code block of an entry.
    A continuation line (no 'field-name:' prefix) is appended, space-joined,
    to the previous field's value — this is how a wrapped `activation-event`
    prose paragraph is authored in the catalog."""
    fields: dict[str, str] = {}
    last_key = None
    for line in fenced_block.splitlines():
        m = _FIELD_RE.match(line)
        if m:
            last_key = m.group(1)
            fields[last_key] = m.group(2).strip()
        elif line.strip() and last_key:
            fields[last_key] = (fields[last_key] + " " + line.strip()).strip()
    return fields


def parse_catalog(text: str) -> tuple[int, list[Entry]]:
    """Parse catalog-version (line 1) + every '### Entry N' section."""
    version_match = re.search(r"^catalog-version:\s*(\d+)", text, re.MULTILINE)
    if not version_match:
        raise CatalogError("catalog missing 'catalog-version: N' on line 1")
    version = int(version_match.group(1))

    headings = list(_ENTRY_HEADING_RE.finditer(text))
    entries = []
    for i, m in enumerate(headings):
        start = m.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        block_text = text[start:end]
        fence = _FENCE_RE.search(block_text)
        fields = _parse_fields(fence.group(1)) if fence else {}
        if not fields.get("key"):
            raise CatalogError(f"entry {m.group().strip()!r} has no 'key:' field")
        entries.append(Entry(m.group(), block_text, fields))
    return version, entries


def load_catalog(path: str = CATALOG_FILE) -> tuple[int, list[Entry]]:
    if not os.path.exists(path):
        raise CatalogError(f"catalog not found: {path}")
    return parse_catalog(open(path, encoding="utf-8").read())


def load_json_or_empty(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except (json.JSONDecodeError, OSError):
        return {}


def load_state(root: str) -> dict:
    state = load_json_or_empty(os.path.join(root, STATE_FILE))
    state.setdefault("entries", {})
    return state


def save_state(root: str, state: dict) -> None:
    path = os.path.join(root, STATE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def eval_dial_predicate(predicate: str, config: dict):
    """Returns True/False for a machine-evaluable '<dot.path> == "<value>"'
    predicate (absence-as-default False), or None if the predicate text does
    not match that grammar (a manual/repo-state predicate, e.g. Entry 7's
    'repo consumes Aura by any detectable mechanism').

    Public (no leading underscore) so `catalog_conformance.py` (#434) can
    reuse the exact same dial-evaluation semantics for its own
    applicability gate rather than a second, potentially-drifting copy."""
    m = _DIAL_PREDICATE_RE.match(predicate.strip())
    if not m:
        return None
    path, expected = m.group(1), m.group(2)
    value, present = config_validate.dialval(config, path)
    if not present:
        return False
    return value == expected


def _activation_satisfied(entry: Entry, vendor_toml_text: str) -> bool:
    if not entry.activation_check:
        return False
    m = _ACTIVATION_CHECK_RE.match(entry.activation_check.strip())
    if not m:
        return False
    dep_name = m.group(1)
    return bool(re.search(r"^\[deps\." + re.escape(dep_name) + r"\]",
                           vendor_toml_text, re.MULTILINE))


def plan(root: str, family: str, catalog_path: str = CATALOG_FILE) -> list[dict]:
    """Evaluate every catalog entry against (root, family); never mutates
    state. One result dict per entry: {key, action, reason}. `action` is one
    of: not-applicable, manual-review, file, skip-already-filed,
    file-blocked, skip-already-blocked, activate."""
    if family not in FAMILIES:
        raise CatalogError(f"unknown family {family!r}; expected one of {FAMILIES}")

    _version, entries = load_catalog(catalog_path)
    config = load_json_or_empty(os.path.join(root, CONFIG_FILE))
    state = load_state(root)
    vendor_toml_text = ""
    vendor_toml_path = os.path.join(root, VENDOR_TOML)
    if os.path.exists(vendor_toml_path):
        vendor_toml_text = open(vendor_toml_path, encoding="utf-8").read()

    results = []
    for entry in entries:
        if family not in entry.applies_when_family:
            results.append({"key": entry.key, "action": "not-applicable",
                             "reason": f"family {family!r} not in "
                                       f"{entry.applies_when_family}"})
            continue

        if entry.applies_when:
            gate = eval_dial_predicate(entry.applies_when, config)
            if gate is None:
                results.append({"key": entry.key, "action": "manual-review",
                                 "reason": "applies-when predicate is not "
                                           "machine-evaluable: "
                                           f"{entry.applies_when!r}"})
                continue
            if not gate:
                results.append({"key": entry.key, "action": "not-applicable",
                                 "reason": f"applies-when false: "
                                           f"{entry.applies_when!r}"})
                continue

        prior = state["entries"].get(entry.key)

        if entry.status == "blocked-on-upstream":
            if _activation_satisfied(entry, vendor_toml_text):
                if prior and prior.get("status") == "filed" and \
                        prior.get("content-hash") == entry.content_hash:
                    results.append({"key": entry.key,
                                     "action": "skip-already-filed",
                                     "reason": "activated + already filed"})
                else:
                    results.append({"key": entry.key, "action": "activate",
                                     "reason": "activation-check now "
                                               "satisfied: "
                                               f"{entry.activation_check}"})
                continue
            if prior and prior.get("status") == "blocked-on-upstream" and \
                    prior.get("content-hash") == entry.content_hash:
                results.append({"key": entry.key,
                                 "action": "skip-already-blocked",
                                 "reason": "already filed as "
                                           "blocked-on-upstream, unchanged"})
            else:
                results.append({"key": entry.key, "action": "file-blocked",
                                 "reason": "spec-only entry, not yet "
                                           "activated: "
                                           f"{entry.activation_event}"})
            continue

        if prior and prior.get("status") == "filed" and \
                prior.get("content-hash") == entry.content_hash:
            results.append({"key": entry.key, "action": "skip-already-filed",
                             "reason": "already filed, unchanged"})
        else:
            reason = "changed since last filed" if prior else "new entry"
            results.append({"key": entry.key, "action": "file",
                             "reason": reason})
    return results


def record(root: str, key: str, status: str,
           catalog_path: str = CATALOG_FILE) -> dict:
    """Persist the outcome of acting on a `plan()` result so the next `plan()`
    call sees this entry as satisfied. `status` is the OUTCOME status
    ('filed' or 'blocked-on-upstream'), not necessarily the catalog's own
    `status:` field (an `activate` action records 'filed' even though the
    catalog entry's static `status:` line still literally reads
    'blocked-on-upstream' until a maintainer edits it)."""
    if status not in ("filed", "blocked-on-upstream"):
        raise CatalogError(f"unknown status {status!r}")
    _version, entries = load_catalog(catalog_path)
    by_key = {e.key: e for e in entries}
    if key not in by_key:
        raise CatalogError(f"no such catalog entry: {key!r}")
    state = load_state(root)
    state["entries"][key] = {"status": status,
                              "content-hash": by_key[key].content_hash}
    save_state(root, state)
    return state["entries"][key]


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _write_catalog(path: str, version: int, entries_text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"catalog-version: {version}\n\n# Fixture catalog\n\n")
        fh.write(entries_text)


def _self_test() -> int:
    cases = []

    # --- Regression: the real web-app entries still resolve correctly ---
    real_version, real_entries = load_catalog()
    cases.append(("real catalog has 10 entries", len(real_entries) == 10))
    by_key = {e.key: e for e in real_entries}
    web_only_keys = {"admin-console", "adopt-token-bookkeeper",
                      "adopt-gatekeeper", "adopt-recordkeeper",
                      "adopt-meta-updater", "adopt-fleet-contract",
                      "app-telemetry"}
    cases.append(("web-only entries default/declare family=[web]",
                  all(by_key[k].applies_when_family == ["web"]
                      for k in web_only_keys)))
    cases.append(("changelog-surface spans web/gui/cli",
                  by_key["changelog-surface"].applies_when_family ==
                  ["web", "gui", "cli"]))
    cases.append(("aura entry spans all 5 families",
                  set(by_key["aura-via-dependency-channel"]
                      .applies_when_family) == set(FAMILIES)))
    cases.append(("meta-updater is blocked-on-upstream with an activation "
                  "event",
                  by_key["adopt-meta-updater"].status == "blocked-on-upstream"
                  and bool(by_key["adopt-meta-updater"].activation_event)))
    cases.append(("fleet-contract is blocked-on-upstream with an activation "
                  "event",
                  by_key["adopt-fleet-contract"].status == "blocked-on-upstream"
                  and bool(by_key["adopt-fleet-contract"].activation_event)))

    tmp_root = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp_root, ".claude"), exist_ok=True)
        # web family: all 10 web-app entries apply (not not-applicable);
        # config left empty so agentic/value-gated entries resolve
        # not-applicable at the config-dial step, but the FAMILY gate itself
        # must let all 10 through. structured-logging (Entry 10) adds one
        # to every family except lib (a pure library has no process to log
        # from); app-telemetry (Entry 9) is web-only.
        for fam, expect_count in (("web", 10), ("gui", 3), ("lib", 1),
                                   ("service", 2), ("cli", 3)):
            results = plan(tmp_root, fam)
            not_family_gated = sum(1 for r in results
                                    if r["action"] == "not-applicable"
                                    and "family" in r["reason"])
            applicable = 10 - not_family_gated
            cases.append((f"family={fam}: {expect_count} entries pass the "
                          f"family gate",
                          applicable == expect_count))
        cases.append(("Entry 7 (repo-state predicate) is manual-review for "
                      "web family",
                      any(r["key"] == "aura-via-dependency-channel"
                          and r["action"] == "manual-review"
                          for r in plan(tmp_root, "web"))))
    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)

    # --- Re-run: only new/changed entries get filed; unblocked-on-upstream
    # entries are never re-filed once satisfied ---
    tmp2 = tempfile.mkdtemp()
    try:
        cat_v1 = os.path.join(tmp2, "catalog-v1.md")
        _write_catalog(cat_v1, 1, (
            "### Entry 1 — Alpha\n\n```\n"
            "key:  alpha\nname: Alpha\ntag:  Grimoire-Requirement\n"
            "applies-when-family: web\n```\n\n"
            "### Entry 2 — Beta\n\n```\n"
            "key:  beta\nname: Beta\ntag:  Grimoire-Requirement\n"
            "applies-when-family: web, cli\n```\n"
        ))
        r1 = plan(tmp2, "web", catalog_path=cat_v1)
        cases.append(("first run: both entries file",
                      {r["key"]: r["action"] for r in r1} ==
                      {"alpha": "file", "beta": "file"}))
        record(tmp2, "alpha", "filed", catalog_path=cat_v1)
        record(tmp2, "beta", "filed", catalog_path=cat_v1)
        r2 = plan(tmp2, "web", catalog_path=cat_v1)
        cases.append(("second run (unchanged catalog): both already-filed, "
                      "no re-file",
                      all(r["action"] == "skip-already-filed" for r in r2)))

        # v2: alpha's spec text changes, gamma is a brand-new entry
        cat_v2 = os.path.join(tmp2, "catalog-v2.md")
        _write_catalog(cat_v2, 2, (
            "### Entry 1 — Alpha\n\n```\n"
            "key:  alpha\nname: Alpha (revised)\n"
            "tag:  Grimoire-Requirement\n"
            "applies-when-family: web\n```\n\n"
            "### Entry 2 — Beta\n\n```\n"
            "key:  beta\nname: Beta\ntag:  Grimoire-Requirement\n"
            "applies-when-family: web, cli\n```\n\n"
            "### Entry 3 — Gamma\n\n```\n"
            "key:  gamma\nname: Gamma\ntag:  Grimoire-Requirement\n"
            "applies-when-family: web\n```\n"
        ))
        r3 = plan(tmp2, "web", catalog_path=cat_v2)
        by = {r["key"]: r["action"] for r in r3}
        cases.append(("third run (alpha changed, gamma new): only alpha+"
                      "gamma file, beta stays satisfied",
                      by == {"alpha": "file", "beta": "skip-already-filed",
                             "gamma": "file"}))
    finally:
        import shutil
        shutil.rmtree(tmp2, ignore_errors=True)

    # --- blocked-on-upstream lifecycle: file-blocked -> skip -> activate ---
    tmp3 = tempfile.mkdtemp()
    try:
        cat = os.path.join(tmp3, "catalog.md")
        _write_catalog(cat, 1, (
            "### Entry 1 — Delta\n\n```\n"
            "key:               delta\nname:              Delta\n"
            "tag:               Grimoire-Requirement\n"
            "applies-when-family: web\n"
            "status:            blocked-on-upstream\n"
            "activation-event:  delta-crate is published\n"
            "activation-check:  vendor.toml:deps.delta-crate\n"
            "```\n"
        ))
        r1 = plan(tmp3, "web", catalog_path=cat)
        cases.append(("blocked entry with no vendor.toml files as "
                      "file-blocked",
                      r1[0]["action"] == "file-blocked"))
        record(tmp3, "delta", "blocked-on-upstream", catalog_path=cat)
        r2 = plan(tmp3, "web", catalog_path=cat)
        cases.append(("blocked entry, already recorded, no re-file",
                      r2[0]["action"] == "skip-already-blocked"))
        with open(os.path.join(tmp3, "vendor.toml"), "w") as fh:
            fh.write("[deps.delta-crate]\nchannel = \"stable\"\n")
        r3 = plan(tmp3, "web", catalog_path=cat)
        cases.append(("blocked entry activates once vendor.toml has the dep",
                      r3[0]["action"] == "activate"))
        record(tmp3, "delta", "filed", catalog_path=cat)
        r4 = plan(tmp3, "web", catalog_path=cat)
        cases.append(("activated entry settles to skip-already-filed",
                      r4[0]["action"] == "skip-already-filed"))
    finally:
        import shutil
        shutil.rmtree(tmp3, ignore_errors=True)

    # --- config-dial gate: absence-as-default false ---
    tmp4 = tempfile.mkdtemp()
    try:
        cat = os.path.join(tmp4, "catalog.md")
        _write_catalog(cat, 1, (
            "### Entry 1 — Epsilon\n\n```\n"
            "key:          epsilon\nname:         Epsilon\n"
            "tag:          Grimoire-Requirement\n"
            "applies-when-family: web\n"
            "applies-when: web-app.agentic == \"yes\"\n"
            "```\n"
        ))
        r_absent = plan(tmp4, "web", catalog_path=cat)
        cases.append(("config-dial predicate: absent dial -> not-applicable",
                      r_absent[0]["action"] == "not-applicable"))
        os.makedirs(os.path.join(tmp4, ".claude"), exist_ok=True)
        with open(os.path.join(tmp4, CONFIG_FILE), "w") as fh:
            json.dump({"web-app": {"agentic": {"value": "yes"}}}, fh)
        r_present = plan(tmp4, "web", catalog_path=cat)
        cases.append(("config-dial predicate: dial set to matching value -> "
                      "file",
                      r_present[0]["action"] == "file"))
    finally:
        import shutil
        shutil.rmtree(tmp4, ignore_errors=True)

    # --- Finer catalog dials (v3.97, #464): Entries 4/5/8 (gatekeeper/
    # recordkeeper/fleet-contract) now gate on web-app.auth /
    # web-app.persistence / web-app.fleet-participant instead of the coarse
    # web-app.value == "yes" -- verified against the REAL catalog. ---
    tmp5 = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp5, ".claude"), exist_ok=True)
        # web-app.value alone (no finer dial declared) no longer files any
        # of the three -- the coarse gate was replaced, not merely narrowed.
        with open(os.path.join(tmp5, CONFIG_FILE), "w") as fh:
            json.dump({"web-app": {"value": "yes"}}, fh)
        results = {r["key"]: r["action"] for r in plan(tmp5, "web")}
        cases.append(("web-app.value alone no longer files gatekeeper "
                      "(finer web-app.auth dial required)",
                      results["adopt-gatekeeper"] == "not-applicable"))
        cases.append(("web-app.value alone no longer files recordkeeper "
                      "(finer web-app.persistence dial required)",
                      results["adopt-recordkeeper"] == "not-applicable"))
        cases.append(("web-app.value alone no longer files fleet-contract "
                      "(finer web-app.fleet-participant dial required)",
                      results["adopt-fleet-contract"] == "not-applicable"))

        # Declaring each finer dial files (or file-blocks) the matching entry.
        with open(os.path.join(tmp5, CONFIG_FILE), "w") as fh:
            json.dump({"web-app": {
                "value": "yes",
                "auth": {"value": "yes"},
                "persistence": {"value": "yes"},
                "fleet-participant": {"value": "yes"},
            }}, fh)
        results = {r["key"]: r["action"] for r in plan(tmp5, "web")}
        cases.append(("web-app.auth=yes files gatekeeper",
                      results["adopt-gatekeeper"] == "file"))
        cases.append(("web-app.persistence=yes files recordkeeper",
                      results["adopt-recordkeeper"] == "file"))
        cases.append(("web-app.fleet-participant=yes files fleet-contract "
                      "as file-blocked (still spec-only)",
                      results["adopt-fleet-contract"] == "file-blocked"))
    finally:
        import shutil
        shutil.rmtree(tmp5, ignore_errors=True)

    lines, passed, failed = [], 0, 0
    for label, ok in cases:
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    print("\n".join(lines))
    print(f"\n{passed}/{passed + failed} self-test cases passed")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic planning engine for the required-feature "
                    "catalog (#413).")
    ap.add_argument("verb", nargs="?", help="plan|record")
    ap.add_argument("--root", default=".")
    ap.add_argument("--family", choices=FAMILIES)
    ap.add_argument("--key")
    ap.add_argument("--status", choices=("filed", "blocked-on-upstream"))
    ap.add_argument("--catalog", default=CATALOG_FILE)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.verb:
        ap.error("a verb is required (plan|record) or --self-test")

    try:
        if args.verb == "plan":
            if not args.family:
                ap.error("plan requires --family")
            results = plan(args.root, args.family, catalog_path=args.catalog)
            print(json.dumps(results, indent=2, ensure_ascii=False))
        elif args.verb == "record":
            if not args.key or not args.status:
                ap.error("record requires --key and --status")
            outcome = record(args.root, args.key, args.status,
                              catalog_path=args.catalog)
            print(json.dumps(outcome, indent=2, ensure_ascii=False))
        else:
            ap.error(f"unknown verb: {args.verb}")
    except CatalogError as exc:
        print(f"catalog_filing: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
