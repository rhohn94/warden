#!/usr/bin/env python3
"""app_telemetry_conformance.py — standalone, offline conformance probe for
the required-feature catalog's Entry 9 (Standardized App Telemetry, AT-1..
AT-4; #436, v3.99 R8 Pass 1).

Checks a TARGET REPO's checked-out source tree — never a running app, no
network:

  (a) **Emitter-module presence (informational, best-effort static scan)** —
      whether an app-telemetry emitter referencing all three reference event
      names (`boot` / `request-summary` / `error`) is discoverable in source.
      Mirrors `changelog_conformance.py`'s check (c) — best-effort, never a
      pass/fail input on its own, since detection is inherently
      language/convention-dependent.
  (b) **Sample-fixture schema validation (mechanical)** — whether a
      committed JSON Lines sample-event fixture exists, and if so, whether
      EVERY line validates against `app_telemetry_schema.validate_event`
      (required fields, uniform expected shape). THIS is the actual
      "verifies emitted events against the schema shape" half of Entry 9's
      acceptance bar — a real, reproducible check, not a grep.

Verdict is driven by (b) ONLY, mirroring `changelog_conformance.py`'s own
"verdict driven by the mechanically-checkable half, not the informational
one" discipline:
  - No fixture found at all: PASS (informational note) — Entry 9 has no
    `applies-when` dial (it is unconditional for `web`, same shape as Entry
    2), so "not yet adopted" is expected on a fresh scaffold, not a failure
    (required-feature-catalog.md's own SPEC framing: implementing a filed
    entry is the managed project's own scope/timeline).
  - Fixture found, every line validates AND all three reference event types
    are represented: PASS.
  - Fixture found but ANY line fails schema validation: FLAGGED (a real,
    offline-verifiable shape drift).
  - Fixture found, every line individually valid, but fewer than all three
    reference event types are represented: WARN (a soft finding — AT-2 asks
    for "all three actually emitted"; a fixture demonstrating only one or two
    is a partial adoption, not a shape defect).

This module is also the callable `catalog_conformance.py` dispatches to
(`CHECK_REGISTRY["app-telemetry"]`) — the same sibling-skill dispatch shape
Entries 1/2/3-5/7 already use.

CLI:
    python3 app_telemetry_conformance.py --root PATH   # probe PATH (default: cwd)
    python3 app_telemetry_conformance.py --self-test    # offline fixture round trip

Design authority: docs/grimoire/design/app-telemetry-design.md §1 (schema),
§2 (reference event types), §6 (this check); required-feature-catalog.md
Entry 9 (AT-1..AT-4).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_telemetry_schema as schema  # noqa: E402

CONFIG_FILE = ".claude/grimoire-config.json"

# Candidate fixture locations, checked in order — the web quick-start
# template ships its sample at the first path; a project MAY relocate it, so
# a couple of conventional fallbacks are also tried before reporting absent.
_FIXTURE_CANDIDATES = (
    "tests/fixtures/app_telemetry_sample.jsonl",
    "tests/app_telemetry_sample.jsonl",
)

# Source file-name/keyword markers used by check (a)'s best-effort static
# scan. Deliberately narrow (mirrors changelog_conformance.py's `.rs/.py/
# .ts/.js` glob) — a scaffolded Rust app is this item's only reference
# implementation today; other stacks are not force-fit.
_SRC_SUFFIXES = (".rs", ".py", ".ts", ".js")
_EMITTER_NAME_MARKERS = ("app_telemetry", "app-telemetry")


# ── Finding collector (mirrors changelog_conformance.py / fleet_conformance.py
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


# ── Check (a): emitter-module presence (informational) ──────────────────────

def detect_emitter_module(root: Path) -> tuple[str, str]:
    """Best-effort, static/offline detection of an app-telemetry emitter in
    source, referencing all three reference event names. INFORMATIONAL ONLY
    — never a pass/fail input (see module docstring). Returns
    (status, detail) with status in {"found", "not-found", "not-applicable"}.
    """
    src_dir = root / "src"
    if not src_dir.is_dir():
        return "not-applicable", "no src/ tree found to scan."

    candidates = [p for p in sorted(src_dir.rglob("*"))
                  if p.is_file() and p.suffix in _SRC_SUFFIXES
                  and any(m in p.name for m in _EMITTER_NAME_MARKERS)]
    if not candidates:
        return "not-found", ("src/ present but no app-telemetry-named "
                              "module found (looked for a file name "
                              "containing 'app_telemetry'/'app-telemetry' "
                              "under src/).")

    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        missing = [ev for ev in sorted(schema.REFERENCE_EVENTS) if ev not in text]
        rel = p.relative_to(root).as_posix()
        if not missing:
            return "found", (f"{rel} references all three reference event "
                              f"names ({sorted(schema.REFERENCE_EVENTS)}).")
        return "not-found", (f"{rel} found but does not reference all three "
                              f"reference event names (missing: {missing}).")
    return "not-found", "no candidate module was readable."


# ── Check (b): sample-fixture schema validation (mechanical) ────────────────

def find_fixture(root: Path) -> Path | None:
    for rel in _FIXTURE_CANDIDATES:
        p = root / rel
        if p.is_file():
            return p
    return None


def validate_fixture(path: Path, result: ConformanceResult) -> None:
    """Parse `path` as JSON Lines and validate every line against the
    schema. Blank lines are skipped. A line that is not valid JSON at all is
    an ERROR (a stronger problem than a schema-shape mismatch)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.error(f"could not read fixture {path}: {exc}")
        return

    events: list[dict] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            result.error(f"fixture line {lineno} is not valid JSON: {exc}")
            continue
        errs = schema.validate_event(obj)
        if errs:
            for e in errs:
                result.error(f"fixture line {lineno}: {e}")
        else:
            events.append(obj)

    if not events:
        result.error(f"fixture {path.name} contains no schema-conformant "
                      f"events at all.")
        return

    found_reference = schema.which_reference_events(events)
    missing_reference = schema.REFERENCE_EVENTS - found_reference
    if missing_reference:
        result.warn(
            f"fixture {path.name} is schema-conformant but demonstrates "
            f"only {sorted(found_reference)} of the three reference event "
            f"types (missing: {sorted(missing_reference)}) — AT-2 asks for "
            f"all three to actually be emitted.")
    else:
        result.note(f"fixture {path.name}: {len(events)} event(s), all "
                     f"schema-conformant, all three reference event types "
                     f"represented.")


# ── Probe entry point ────────────────────────────────────────────────────────

def probe(root: Path) -> ConformanceResult:
    """Run the full Entry-9 conformance probe against a target repo (offline,
    no running app required)."""
    result = ConformanceResult(str(root))

    status_a, detail_a = detect_emitter_module(root)
    result.note(f"emitter-module scan: {detail_a}")

    fixture = find_fixture(root)
    if fixture is None:
        result.note(
            "no committed app-telemetry sample fixture found at any of "
            f"{_FIXTURE_CANDIDATES} — not yet adopted. This is NOT a "
            "failure: implementing a filed required-feature-catalog entry "
            "is the managed project's own scope and timeline "
            "(required-feature-catalog.md's own SPEC framing). Re-run once "
            "the app has committed a sample fixture (or wire this check "
            "against a real emitted-event capture).")
        return result

    validate_fixture(fixture, result)
    return result


# ── Self-test (offline fixture round trip) ──────────────────────────────────

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_GOOD_BOOT = {"ts": "2026-07-14T03:28:12Z", "instance": "i-1", "app": "familiar",
              "version": "1.20.0", "event": "boot", "props": {}}
_GOOD_REQ = {"ts": "2026-07-14T03:28:13Z", "instance": "i-1", "app": "familiar",
             "version": "1.20.0", "event": "request-summary",
             "props": {"route": "/api/widgets", "status": 200}}
_GOOD_ERR = {"ts": "2026-07-14T03:28:14Z", "instance": "i-1", "app": "familiar",
             "version": "1.20.0", "event": "error", "props": {"kind": "db_timeout"}}


def run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("app_telemetry_conformance.py --self-test")

    # 1. No fixture at all: PASS, informational only.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        r = probe(root)
        check(r.passed, "no fixture present should PASS (not yet adopted)")
        check(any("not yet adopted" in n for n in r.info),
              "should note 'not yet adopted'")
        print(f"  OK: no fixture -> PASS (informational)")

    # 2. Conformant fixture, all three reference events: PASS.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lines = "\n".join(json.dumps(e) for e in (_GOOD_BOOT, _GOOD_REQ, _GOOD_ERR))
        _write(root / "tests" / "fixtures" / "app_telemetry_sample.jsonl", lines + "\n")
        r = probe(root)
        check(r.passed, f"conformant fixture (all 3 types) should PASS: {r.report()}")
        check(not r.warnings, f"conformant fixture should have no warnings: {r.warnings}")
        print(f"  OK: conformant fixture, all 3 reference types -> PASS\n{r.report()}")

    # 3. THE ACCEPTANCE CASE — a malformed event (missing field): FLAGGED.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bad = dict(_GOOD_BOOT)
        del bad["instance"]
        lines = "\n".join(json.dumps(e) for e in (bad, _GOOD_REQ, _GOOD_ERR))
        _write(root / "tests" / "fixtures" / "app_telemetry_sample.jsonl", lines + "\n")
        r = probe(root)
        check(not r.passed, "a fixture with a schema-invalid line must be FLAGGED")
        check(any("instance" in e for e in r.errors),
              f"the flagged error should name the missing field: {r.errors}")
        print(f"  OK (expected FAIL): missing-field event -> FLAGGED\n{r.report()}")

    # 4. Fixture with only one reference event type: WARN (soft), still PASS.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "tests" / "fixtures" / "app_telemetry_sample.jsonl",
               json.dumps(_GOOD_BOOT) + "\n")
        r = probe(root)
        check(r.passed, "a schema-conformant but partial fixture should still PASS")
        check(any("only" in w for w in r.warnings),
              f"a partial fixture should WARN about missing reference types: {r.warnings}")
        print(f"  OK: partial fixture (1/3 reference types) -> PASS with WARN\n{r.report()}")

    # 5. Not-valid-JSON line: FLAGGED.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "tests" / "fixtures" / "app_telemetry_sample.jsonl",
               "{not valid json\n" + json.dumps(_GOOD_BOOT) + "\n")
        r = probe(root)
        check(not r.passed, "an unparseable line must be FLAGGED")
        check(any("not valid JSON" in e for e in r.errors),
              f"the error should name the JSON parse failure: {r.errors}")
        print(f"  OK (expected FAIL): unparseable line -> FLAGGED")

    # 6. Blank lines are skipped, not flagged.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lines = "\n".join(json.dumps(e) for e in (_GOOD_BOOT, _GOOD_REQ, _GOOD_ERR))
        _write(root / "tests" / "fixtures" / "app_telemetry_sample.jsonl",
               "\n" + lines + "\n\n")
        r = probe(root)
        check(r.passed, "blank lines around real events should not be flagged")
        print(f"  OK: blank lines skipped, fixture still PASSes")

    # 7. Fallback fixture location (tests/app_telemetry_sample.jsonl) is also
    #    discovered.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lines = "\n".join(json.dumps(e) for e in (_GOOD_BOOT, _GOOD_REQ, _GOOD_ERR))
        _write(root / "tests" / "app_telemetry_sample.jsonl", lines + "\n")
        r = probe(root)
        check(r.passed, "the fallback fixture path should also be discovered and PASS")
        print(f"  OK: fallback fixture path discovered -> PASS")

    # 8. Check (a) — emitter module referencing all three events is
    #    detected, informationally, alongside a fixture-driven verdict.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "src" / "app_telemetry.rs",
               "// emits boot, request-summary, error\n")
        status, detail = detect_emitter_module(root)
        check(status == "found", f"emitter module should be 'found': {status}: {detail}")
        print(f"  OK: check (a) detects an emitter module referencing all 3 events")

    # 9. Check (a) — no src/ tree at all: not-applicable, never errors.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        status, detail = detect_emitter_module(root)
        check(status == "not-applicable",
              f"no src/ tree should report 'not-applicable', got {status}")
        print(f"  OK: check (a) with no src/ tree -> not-applicable")

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
            "Standalone, offline conformance probe for required-feature "
            "catalog Entry 9 (Standardized App Telemetry). See "
            "required-feature-catalog.md §Entry 9."
        )
    )
    parser.add_argument("--root", metavar="PATH", default=".",
                         help="Target repo root to probe (default: cwd).")
    parser.add_argument("--self-test", action="store_true",
                         help="Run the offline fixture round trip; ignores --root.")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    root = Path(args.root).resolve()
    result = probe(root)
    print(result.report())
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
