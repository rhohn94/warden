#!/usr/bin/env python3
"""changelog_conformance.py — standalone, offline conformance probe for the
required-feature catalog's Entry 2 (Changelog Surface, CL-1..CL-6; #437).

Checks a TARGET REPO's checked-out source tree — never a running app:

  (a) the `changelog.user-facing` dial's presence/shape in
      `.claude/grimoire-config.json` (required-feature-catalog.md §Entry 2).
  (b) whether a packaged `grimoire-build-info.json` exists under `dist/`, i.e.
      whether the `package` recipe target has ever been run so a real
      changelog snapshot exists to render (web-app-deployment-protocol.md §8).
  (c) whether an admin-section/changelog-route convention is discoverable in
      source, per family — best-effort, static/offline detection only.
      Reports "not-applicable" gracefully when no web-app source structure is
      present (there is no reference implementation shipping in THIS repo yet
      — epic #395 owns the web-aura-starter reference, out of scope here).

Pass/fail verdict is driven by (a)+(b) ONLY: a `changelog.user-facing` dial
that is `on` with no build-info snapshot is FLAGGED — the `/changelog` surface
the dial promises would have nothing real to render. Dial `off` (or absent,
the default), or dial `on` with a real snapshot present, both PASS. A
malformed dial shape is also flagged. Check (c) is always informational only
— never a pass/fail input, since no fleet app implements the surface yet and
static per-family detection is inherently best-effort
(required-feature-catalog.md §Entry 2 Conformance check).

This module is also imported by `grm-install-doctor/install_doctor.py`
(`audit_changelog_surface`) as the standalone probe wired into the health
audit — the same sibling-skill-import pattern `install_doctor.py` already uses
for `grm-issue-tracker/issue_tracker.py`.

CLI:
    python3 changelog_conformance.py --root PATH   # probe PATH (default: cwd)
    python3 changelog_conformance.py --self-test    # offline fixture round trip

Design authority: required-feature-catalog.md §Entry 2 (CL-1..CL-6);
docs/grimoire/design/changelog-surface-design.md;
docs/web-app-deployment-protocol.md §8.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# grm-config-validate is a fixed sibling skill directory (mirrors the
# install_doctor.py -> issue_tracker.py pattern) — reuse its dialval() so the
# dotted-path / {"value": ...}-unwrap semantics never drift into a second copy.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-config-validate"))
import config_validate  # noqa: E402  (sys.path set immediately above)

CONFIG_FILE = ".claude/grimoire-config.json"
BUILD_INFO_NAME = "grimoire-build-info.json"
DIST_DIR = "dist"

# Cargo dependency names that mark the gui (egui/eframe) family — used only to
# report check (c) as "not-applicable" with an accurate reason, never to gate
# pass/fail.
_GUI_CARGO_MARKERS = ("egui", "eframe")


# ── Finding collector (mirrors fleet_conformance.py's ConformanceResult so the
#    two catalog-conformance scripts read the same way) ─────────────────────

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


# ── Config helpers ───────────────────────────────────────────────────────────

def _read_config(root: Path) -> dict:
    """Parse .claude/grimoire-config.json, or {} if absent/unreadable."""
    cfg_path = root / CONFIG_FILE
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _web_app_declared(cfg: dict) -> bool:
    val, present = config_validate.dialval(cfg, "web-app.value")
    return present and isinstance(val, str) and val.strip().lower() == "yes"


# ── Check (a): dial presence/shape ───────────────────────────────────────────

def check_dial(cfg: dict, result: ConformanceResult) -> tuple[bool, bool]:
    """Check (a): the `changelog.user-facing` dial's presence/shape.

    Returns (dial_on, shape_ok).
    """
    raw = cfg.get("changelog")
    if raw is None:
        result.note("changelog.user-facing absent — reads as default 'off' "
                     "(operator-only Admin Console changelog; no /changelog "
                     "route).")
        return False, True
    if not isinstance(raw, dict):
        result.error(f"`changelog` must be an object (e.g. "
                      f"{{\"user-facing\": {{\"value\": \"off\"}}}}); got {raw!r}")
        return False, False
    val, present = config_validate.dialval(cfg, "changelog.user-facing")
    if not present:
        result.note("changelog block present but no `user-facing` key — "
                     "reads as default 'off'.")
        return False, True
    if val not in ("on", "off"):
        result.error(f"changelog.user-facing must be 'on' or 'off'; got {val!r}")
        return False, False
    result.note(f"changelog.user-facing = {val!r}.")
    return val == "on", True


# ── Check (b): build-info snapshot presence ─────────────────────────────────

def find_build_info(root: Path) -> tuple[bool, str]:
    """Check (b): does a packaged grimoire-build-info.json exist under dist/?
    (i.e. has the `package` recipe target ever been run — web-app-deployment-
    protocol.md §8.) A root-level copy is also accepted (a project that
    inspects a staged tree directly rather than the dist/<bundle>/ archive
    layout)."""
    dist_dir = root / DIST_DIR
    if dist_dir.is_dir():
        matches = sorted(dist_dir.glob(f"**/{BUILD_INFO_NAME}"))
        if matches:
            rel = matches[0].relative_to(root).as_posix()
            return True, f"found {rel} (package has been run)."
    root_level = root / BUILD_INFO_NAME
    if root_level.is_file():
        return True, f"found {BUILD_INFO_NAME} at repo root."
    return False, (f"no {BUILD_INFO_NAME} found under {DIST_DIR}/ — the "
                    "`package` recipe target has not been run yet "
                    "(web-app-deployment-protocol.md §8).")


# ── Check (c): route/section convention (informational only) ───────────────

def detect_route_convention(root: Path) -> tuple[str, str]:
    """Check (c): best-effort, static/offline detection of an admin-section /
    changelog-route convention in source, per family. INFORMATIONAL ONLY —
    never a pass/fail input (see module docstring: no fleet reference
    implementation ships in this repo yet). Returns (status, detail) with
    status in {"found", "not-found", "not-applicable"}.
    """
    templates_dir = root / "templates"
    if templates_dir.is_dir():
        # web family: look for a changelog-named template, or a source file
        # that references the stable "/changelog" path.
        for p in sorted(templates_dir.rglob("*")):
            if p.is_file() and "changelog" in p.name.lower():
                return "found", (f"web family: found a changelog-named "
                                  f"template ({p.relative_to(root).as_posix()}).")
        src_dir = root / "src"
        if src_dir.is_dir():
            for p in sorted(src_dir.rglob("*")):
                if p.is_file() and p.suffix in (".rs", ".py", ".ts", ".js"):
                    try:
                        text = p.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    if "/changelog" in text:
                        return "found", (f"web family: found a '/changelog' "
                                          f"route reference in "
                                          f"{p.relative_to(root).as_posix()}.")
        return "not-found", ("web family (templates/ present) but no "
                              "changelog-named template or '/changelog' route "
                              "reference found — see required-feature-"
                              "catalog.md §Entry 2 (no reference "
                              "implementation ships in this repo; epic #395 "
                              "owns the web-aura-starter reference).")
    cargo_toml = root / "Cargo.toml"
    if cargo_toml.is_file():
        try:
            text = cargo_toml.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        if any(dep in text for dep in _GUI_CARGO_MARKERS):
            return "not-applicable", ("gui family (egui/eframe dependency "
                                       "detected) — the egui-panel convention "
                                       "is documented guidance, not "
                                       "statically detectable; see "
                                       "required-feature-catalog.md §Entry 2 "
                                       "Per-family guidance.")
    return "not-applicable", ("no web-app source structure (templates/) "
                               "detected to check for a changelog route.")


# ── Probe entry point ────────────────────────────────────────────────────────

def probe(root: Path) -> ConformanceResult:
    """Run the full Entry-2 conformance probe against a target repo (offline,
    no running app required)."""
    result = ConformanceResult(str(root))
    cfg = _read_config(root)
    if not _web_app_declared(cfg):
        result.note("web-app.value != 'yes' — catalog Entry 2 does not apply "
                     "to this repo.")
        return result

    dial_on, _shape_ok = check_dial(cfg, result)
    build_info_present, build_info_detail = find_build_info(root)
    result.note(build_info_detail)
    _route_status, route_detail = detect_route_convention(root)
    result.note(route_detail)

    if dial_on and not build_info_present:
        result.error(
            "changelog.user-facing is 'on' but no grimoire-build-info.json "
            "snapshot exists — the /changelog surface (and the Admin "
            "Console's Changelog section) would have nothing real to render. "
            "Run the `package` recipe target first.")
    return result


# ── Self-test (offline fixture round trip) ──────────────────────────────────

def _write_config(root: Path, cfg: dict) -> None:
    cfg_path = root / CONFIG_FILE
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


def _write_build_info(root: Path, bundle: str = "demo-v1.0.0-x86_64-linux") -> Path:
    stage = root / DIST_DIR / bundle
    stage.mkdir(parents=True, exist_ok=True)
    info_path = stage / BUILD_INFO_NAME
    info_path.write_text(json.dumps({
        "framework-version": "v3.94",
        "grimoire-config": {},
        "build-timestamp": "2026-07-13T00:00:00Z",
        "source-ref": "deadbeef",
        "changelog": "## v1.0.0\n- initial release\n",
    }), encoding="utf-8")
    return info_path


def run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("changelog_conformance.py --self-test")

    # 1. Not a web app at all: PASS, no claims made, no errors.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "no"}})
        r = probe(root)
        check(r.passed, "non-web-app repo should PASS (Entry 2 does not apply)")
        check(not r.errors, "non-web-app repo should have zero errors")
        print(f"  OK: non-web-app repo -> PASS\n{r.report()}")

    # 2. web-app, dial absent (default off), no build-info: PASS (operator
    #    surface only; empty-state is honest per CL-2, not a failure).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "yes"}})
        r = probe(root)
        check(r.passed, "dial absent (default off) should PASS")
        print(f"  OK: dial absent -> PASS")

    # 3. web-app, dial explicitly off, no build-info: PASS.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "yes"},
                              "changelog": {"user-facing": {"value": "off"}}})
        r = probe(root)
        check(r.passed, "dial off should PASS regardless of build-info")
        print(f"  OK: dial off -> PASS")

    # 4. THE ACCEPTANCE CASE — web-app, dial ON, no build-info: FLAGGED (FAIL).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "yes"},
                              "changelog": {"user-facing": {"value": "on"}}})
        r = probe(root)
        check(not r.passed,
              "dial ON with no grimoire-build-info.json MUST be flagged (FAIL)")
        check(any("grimoire-build-info.json" in e for e in r.errors),
              "the flagged error should name the missing build-info snapshot")
        print(f"  OK (expected FAIL): dial on + no build-info -> FLAGGED\n{r.report()}")

    # 5. THE ACCEPTANCE CASE — web-app, dial ON, build-info present: PASS.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "yes"},
                              "changelog": {"user-facing": {"value": "on"}}})
        _write_build_info(root)
        r = probe(root)
        check(r.passed,
              "dial ON with a real grimoire-build-info.json snapshot must PASS")
        print(f"  OK: dial on + build-info present -> PASS\n{r.report()}")

    # 6. Malformed dial shape (a bare string instead of an object): FLAGGED.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "yes"}, "changelog": "on"})
        r = probe(root)
        check(not r.passed, "a non-object changelog block should be flagged")
        print(f"  OK (expected FAIL): malformed changelog shape -> FLAGGED")

    # 7. Check (c) — web family with a changelog-named template: "found",
    #    reported informationally, never flips the pass/fail verdict.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "yes"}})
        (root / "templates" / "pages").mkdir(parents=True)
        (root / "templates" / "pages" / "changelog.html").write_text(
            "<h1>Changelog</h1>", encoding="utf-8")
        status, detail = detect_route_convention(root)
        check(status == "found", f"a changelog-named template should be 'found', got {status}")
        r = probe(root)
        check(r.passed, "check (c) alone must never fail the probe (informational only)")
        print(f"  OK: web family with changelog template -> (c) found, probe PASS")

    # 8. Check (c) — no web-app source structure at all: "not-applicable",
    #    reported gracefully, never an error.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        status, detail = detect_route_convention(root)
        check(status == "not-applicable",
              f"no source structure should report 'not-applicable', got {status}")
        check("not detected" in detail or "no web-app source" in detail,
              "the not-applicable detail should explain why gracefully")
        print(f"  OK: no source structure -> (c) not-applicable")

    # 9. Check (c) — gui family (egui/eframe Cargo dep): "not-applicable" with
    #    a family-specific reason, never a static-detection attempt.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Cargo.toml").write_text(
            "[dependencies]\neframe = \"0.28\"\n", encoding="utf-8")
        status, detail = detect_route_convention(root)
        check(status == "not-applicable" and "gui family" in detail,
              f"an eframe Cargo.toml should report gui-family not-applicable, got {status}: {detail}")
        print(f"  OK: gui family (eframe) -> (c) not-applicable, family-specific reason")

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
            "catalog Entry 2 (Changelog Surface). See "
            "required-feature-catalog.md §Entry 2."
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
