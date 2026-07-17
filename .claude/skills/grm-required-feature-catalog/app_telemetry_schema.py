#!/usr/bin/env python3
"""app_telemetry_schema.py — the pure, shared schema authority for the
required-feature catalog's Entry 9 (Standardized App Telemetry, #436, v3.99
R8 Pass 1). ONE small module, no I/O, imported by:

  - `app_telemetry_conformance.py` — validates a target repo's committed
    sample-event fixture against this schema (the mechanical half of Entry
    9's conformance check).
  - `app_telemetry_conformance.py --self-test` — proves this module's own
    fixture round trip (good event / missing field / bad type / unrecognized
    event / non-object props) independently of any target repo.

Schema authority: `docs/grimoire/design/app-telemetry-design.md` §1
(fields) and §2 (the three reference event types). This module does NOT
enforce §3's sampling rules or §4's privacy rule — both require reading
call-site *behavior* (a sampling gate present/absent, a props value's
provenance), not a single event object's shape, and stay code-review sub-
requirements (AT-3/AT-4 in the catalog entry) exactly as
`standard_package_conformance.py`'s own docstring draws the same
mechanically-checkable / not-mechanically-checkable line for its entries.

Six required fields, all present on every conforming event — no optional
fields in schema v1 (`app-telemetry-design.md` §1):
  ts        str   ISO-8601 UTC timestamp
  instance  str   stable instance identifier
  app       str   application identifier
  version   str   running app version
  event     str   event-type slug (REFERENCE_EVENTS or an app-specific value)
  props     dict  free-form event-specific payload (MAY be empty)
"""
from __future__ import annotations

# The six required fields (§1) and their expected Python types once a JSON
# value is parsed. Order matches the design doc's field table.
REQUIRED_FIELDS: dict[str, type] = {
    "ts": str,
    "instance": str,
    "app": str,
    "version": str,
    "event": str,
    "props": dict,
}

# The three reference event types this item's acceptance bar requires the
# web starter to emit (app-telemetry-design.md §2). NOT a closed set — an
# app MAY emit additional app-specific `event` values; this set is used only
# to report which of the three reference types a sample fixture actually
# demonstrates (AT-2's "all three actually emitted" check), never to reject
# an event whose `event` value falls outside it.
REFERENCE_EVENTS = frozenset({"boot", "request-summary", "error"})


def validate_event(obj) -> list[str]:
    """Validate one parsed JSON event object against the §1 schema.

    Returns a list of human-readable error strings; empty means conformant.
    Pure — no I/O, no network — so both the conformance probe and its own
    self-test call this exact function rather than two independently-
    maintained checks drifting apart.
    """
    errors: list[str] = []
    if not isinstance(obj, dict):
        return [f"event must be a JSON object; got {type(obj).__name__}"]

    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in obj:
            errors.append(f"missing required field {field!r}")
            continue
        value = obj[field]
        if not isinstance(value, expected_type):
            errors.append(
                f"field {field!r} must be a {expected_type.__name__}; "
                f"got {type(value).__name__}")

    # A field carrying a non-str/dict type already reported above; an empty
    # string is a separate, additional shape problem worth its own message
    # (an empty `ts`/`instance`/`app`/`version`/`event` is never meaningful).
    for field in ("ts", "instance", "app", "version", "event"):
        value = obj.get(field)
        if isinstance(value, str) and not value.strip():
            errors.append(f"field {field!r} must not be empty")

    unknown = sorted(set(obj.keys()) - set(REQUIRED_FIELDS.keys()))
    if unknown:
        errors.append(
            f"unrecognized field(s) {unknown} — schema v1 has exactly the "
            f"six required fields (app-telemetry-design.md §1), no others")

    return errors


def which_reference_events(events: list[dict]) -> set[str]:
    """Given a list of ALREADY-VALIDATED event dicts, return the subset of
    REFERENCE_EVENTS actually present. Used by the conformance probe's AT-2
    check ("all three reference event types actually emitted") — callers
    should validate() each event first; this function trusts `event` is
    present and does not re-validate."""
    return {e.get("event") for e in events if e.get("event") in REFERENCE_EVENTS}


# ── Self-test (offline, pure — no filesystem, no network) ──────────────────

def run_self_test() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("app_telemetry_schema.py --self-test")

    good_boot = {"ts": "2026-07-14T03:28:12Z", "instance": "i-1", "app": "familiar",
                 "version": "1.20.0", "event": "boot", "props": {}}
    good_req = {"ts": "2026-07-14T03:28:13Z", "instance": "i-1", "app": "familiar",
                "version": "1.20.0", "event": "request-summary",
                "props": {"route": "/api/widgets", "status": 200}}
    good_err = {"ts": "2026-07-14T03:28:14Z", "instance": "i-1", "app": "familiar",
                "version": "1.20.0", "event": "error",
                "props": {"kind": "db_timeout"}}

    for label, ev in (("boot", good_boot), ("request-summary", good_req),
                       ("error", good_err)):
        errs = validate_event(ev)
        check(errs == [], f"conformant {label!r} event should have no errors: {errs}")
        print(f"  OK: conformant {label!r} event validates clean")

    # Missing field.
    missing = dict(good_boot)
    del missing["instance"]
    errs = validate_event(missing)
    check(any("instance" in e and "missing" in e for e in errs),
          f"missing 'instance' should be flagged: {errs}")
    print("  OK (expected FAIL): missing field flagged")

    # Wrong type (props as a string instead of an object).
    bad_type = dict(good_boot)
    bad_type["props"] = "not-an-object"
    errs = validate_event(bad_type)
    check(any("props" in e and "dict" in e for e in errs),
          f"non-dict props should be flagged: {errs}")
    print("  OK (expected FAIL): wrong-type props flagged")

    # Empty required string field.
    empty_field = dict(good_boot)
    empty_field["app"] = "   "
    errs = validate_event(empty_field)
    check(any("app" in e and "empty" in e for e in errs),
          f"blank 'app' should be flagged: {errs}")
    print("  OK (expected FAIL): empty field flagged")

    # Unrecognized extra field.
    extra = dict(good_boot)
    extra["user_id"] = "should-not-be-here"
    errs = validate_event(extra)
    check(any("user_id" in e for e in errs),
          f"an unrecognized field should be flagged: {errs}")
    print("  OK (expected FAIL): unrecognized field flagged")

    # A non-object top-level value.
    errs = validate_event("not-an-object-at-all")
    check(errs != [] and "JSON object" in errs[0], f"non-object should be flagged: {errs}")
    print("  OK (expected FAIL): non-object event flagged")

    # An app-specific event value beyond the three reference types is NOT
    # rejected — the schema does not enumerate a closed set (§2).
    custom = dict(good_boot)
    custom["event"] = "cache-warmed"
    errs = validate_event(custom)
    check(errs == [], f"an app-specific event value should still validate: {errs}")
    print("  OK: app-specific 'event' value beyond the reference three still validates")

    # which_reference_events: all three present.
    found = which_reference_events([good_boot, good_req, good_err])
    check(found == REFERENCE_EVENTS,
          f"all three reference events should be detected: {found}")
    print("  OK: which_reference_events finds all three reference types")

    # which_reference_events: only two present, custom event ignored.
    found2 = which_reference_events([good_boot, good_err, custom])
    check(found2 == {"boot", "error"},
          f"only boot+error should be detected (custom event excluded): {found2}")
    print("  OK: which_reference_events ignores non-reference event values")

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} unexpected result(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELF-TEST PASSED.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run_self_test())
