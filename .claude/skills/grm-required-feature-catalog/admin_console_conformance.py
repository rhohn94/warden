#!/usr/bin/env python3
"""admin_console_conformance.py — conformance probe for the required-feature
catalog's Entry 1 (Administrator Console, AC-1..AC-10; #434, v3.97 R6 Pass 5).

Modeled on `fleet_conformance.py`'s two-mode shape (an offline self-test mode
and a live-endpoint mode that is NEVER exercised by --self-test):

  Offline, static (no running app required):
    A best-effort, informational-only scan of the target repo's source tree
    for an `/admin-console` route reference — the same "informational only,
    never a pass/fail input" discipline `changelog_conformance.py`'s check (c)
    already established for Entry 2 (required-feature-catalog.md §Entry 2
    Conformance check), for the identical reason: no fleet app ships a
    reference implementation of this surface yet, and static per-family route
    detection is inherently best-effort. The offline mode's ONE pass/fail
    input is a coarse presence signal: a repo that declares `web-app.value:
    yes` (so Entry 1 unconditionally applies) with zero `/admin-console`
    string reference anywhere in `src/`/`templates/` is WARNed — not proof of
    absence, but a real, cheap, gameable-only-by-actually-adding-the-string
    smell.

  Live (`--url`, optional `--token`):
    The two mechanically-clean sub-requirements this script CAN verify against
    a running app without deep knowledge of its internals:
      AC-1: an unauthenticated GET /admin-console MUST NOT succeed (expects
            401/403 — "no other role can access it").
      AC-10: an authenticated GET /admin-console (with --token) MUST return
            200 ("always reachable at /admin-console").
    AC-2 through AC-9 (telemetry content, config editability, log search,
    update/restart controls, the Grimoire section's specific fields) require
    interpreting page content/behavior, not a status code — genuinely out of
    a deterministic script's reach the same way Entry 2's route-content check
    is. They are NOT probed here; the catalog entry's own sub-requirement
    table remains the source of truth for a human/agent review of those.

CLI:
    python3 admin_console_conformance.py --root PATH
    python3 admin_console_conformance.py --url URL [--token TOKEN]
    python3 admin_console_conformance.py --self-test

Design authority: required-feature-catalog.md §Entry 1 (AC-1..AC-10);
docs/grimoire/design/web-app-support-design.md §5.3.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

CONFIG_FILE = ".claude/grimoire-config.json"
ADMIN_PATH = "/admin-console"


# ── Finding collector (mirrors fleet_conformance.py's ConformanceResult) ────

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


# ── Config helper ─────────────────────────────────────────────────────────

def _read_config(root: Path) -> dict:
    cfg_path = root / CONFIG_FILE
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _web_app_declared(cfg: dict) -> bool:
    web_app = cfg.get("web-app")
    if isinstance(web_app, dict):
        val = web_app.get("value")
        if isinstance(val, dict):
            val = val.get("value")
        return isinstance(val, str) and val.strip().lower() == "yes"
    return isinstance(web_app, str) and web_app.strip().lower() == "yes"


# ── Offline mode: static, informational-only route scan ────────────────────

def scan_admin_console_reference(root: Path) -> tuple[str, str]:
    """Best-effort, static scan for an `/admin-console` reference in source.

    Returns (status, detail) with status in {"found", "not-found",
    "not-applicable"} — mirrors changelog_conformance.py's
    detect_route_convention() shape/contract exactly (informational input,
    never itself the sole pass/fail driver; the caller in `probe()` decides
    whether "not-found" rises to a WARN).
    """
    scan_dirs = [root / "src", root / "templates"]
    existing = [d for d in scan_dirs if d.is_dir()]
    if not existing:
        return "not-applicable", (
            "no src/ or templates/ tree found — nothing to scan.")
    for d in existing:
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix not in (".rs", ".py", ".ts", ".js", ".html", ".jinja",
                                 ".jinja2", ".askama"):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if ADMIN_PATH in text:
                return "found", (
                    f"found an {ADMIN_PATH!r} reference in "
                    f"{p.relative_to(root).as_posix()}.")
    return "not-found", (
        f"no {ADMIN_PATH!r} reference found under "
        f"{'/'.join(d.name for d in existing)}/ — Entry 1 (Administrator "
        f"Console) is a MUST for every Grimoire web app; see "
        f"required-feature-catalog.md §Entry 1.")


def probe(root: Path) -> ConformanceResult:
    """Offline probe — static source scan only, no running app required."""
    result = ConformanceResult(str(root))
    cfg = _read_config(root)
    if not _web_app_declared(cfg):
        result.note("web-app.value != 'yes' — catalog Entry 1 does not "
                     "apply to this repo.")
        return result

    status, detail = scan_admin_console_reference(root)
    result.note(detail)
    if status == "not-found":
        result.warn(
            f"no static {ADMIN_PATH!r} reference detected — this is a "
            f"best-effort heuristic (informational-grade), not proof of "
            f"absence. Run with --url [--token] against a live instance "
            f"for a real AC-1/AC-10 check.")
    result.note(
        "This offline probe only checks for a static route reference. It "
        "does NOT verify AC-1 through AC-10 (role gating, telemetry, config "
        "editability, log search, update/restart controls, the Grimoire "
        "section). Run with --url [--token] against a live instance for the "
        "two mechanically-checkable sub-requirements (AC-1, AC-10); the rest "
        "require a human/agent content review.")
    return result


# ── Live mode (network — never called by --self-test) ──────────────────────

def _fetch_status(url: str, token: Optional[str] = None) -> int:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # pragma: no cover - network edge case
        raise RuntimeError(f"request to {url!r} failed: {exc}") from exc


def evaluate_live_probe(unauth_status: int, auth_status: Optional[int],
                         has_token: bool) -> ConformanceResult:
    """Pure verdict logic, separated from the network fetch (mirrors
    fleet_conformance.py's validate_full/validate_minimal split) so the
    status-code evaluation itself IS self-testable even though the fetch
    that produces those codes never runs offline."""
    result = ConformanceResult("live-probe")
    if unauth_status in (401, 403):
        result.note(f"AC-1: unauthenticated GET {ADMIN_PATH} -> "
                     f"{unauth_status} (correctly gated).")
    else:
        result.error(
            f"AC-1: unauthenticated GET {ADMIN_PATH} returned "
            f"{unauth_status}, expected 401 or 403 — the console MUST be "
            f"gated to the Application Administrator role only.")
    if has_token:
        if auth_status == 200:
            result.note(f"AC-10: authenticated GET {ADMIN_PATH} -> 200 "
                         f"(reachable).")
        else:
            result.error(
                f"AC-10: authenticated GET {ADMIN_PATH} returned "
                f"{auth_status}, expected 200 — the console MUST always be "
                f"reachable at {ADMIN_PATH} for the Administrator.")
    else:
        result.note("no --token supplied; AC-10 (authenticated reachability) "
                     "not checked.")
    result.note(
        "AC-2 through AC-9 (telemetry content, config editability, log "
        "search, update/restart controls, the Grimoire section) are NOT "
        "checked by this probe — they require interpreting page content, "
        "out of a status-code check's reach.")
    return result


def run_live(base_url: str, token: Optional[str]) -> int:
    endpoint = base_url.rstrip("/") + ADMIN_PATH
    print(f"Probing (unauthenticated): GET {endpoint}")
    unauth_status = _fetch_status(endpoint)
    auth_status = None
    if token:
        print(f"Probing (authenticated): GET {endpoint}")
        auth_status = _fetch_status(endpoint, token=token)
    result = evaluate_live_probe(unauth_status, auth_status, has_token=bool(token))
    print(result.report())
    return 0 if result.passed else 1


# ── Self-test (offline only — the live network path is NEVER exercised) ────

def _write_config(root: Path, cfg: dict) -> None:
    cfg_path = root / CONFIG_FILE
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


def run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("admin_console_conformance.py --self-test")

    # 1. Not a web app: PASS, not-applicable.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "no"}})
        r = probe(root)
        check(r.passed, "non-web-app repo should PASS (Entry 1 does not apply)")
        print("  OK: non-web-app repo -> PASS")

    # 2. Web app, no /admin-console reference anywhere: WARN (not ERROR).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "yes"}})
        (root / "src").mkdir()
        (root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        r = probe(root)
        check(r.passed, "missing route ref is a WARN, not a hard failure "
                        "(offline heuristic, not proof of absence)")
        check(any("no static" in w for w in r.warnings),
              "should warn about the missing static reference")
        print("  OK: web-app, no route ref -> WARN (still passes)")

    # 3. Web app, /admin-console referenced in source: no warn.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_config(root, {"web-app": {"value": "yes"}})
        (root / "src").mkdir()
        (root / "src" / "routes.rs").write_text(
            'router.route("/admin-console", get(admin_handler))\n',
            encoding="utf-8")
        r = probe(root)
        check(r.passed and not r.warnings,
              "route ref present should produce no warning")
        print("  OK: web-app, route ref present -> clean PASS")

    # 4. Live-probe verdict logic (pure function) — AC-1 correctly gated,
    #    AC-10 correctly reachable.
    r = evaluate_live_probe(401, 200, has_token=True)
    check(r.passed, f"401 unauth + 200 auth should PASS: {r.report()}")
    print("  OK: 401 unauth + 200 auth -> PASS")

    # 5. Live-probe verdict logic — AC-1 violated (200 unauth, should be 401/403).
    r = evaluate_live_probe(200, None, has_token=False)
    check(not r.passed, "200 on an unauthenticated request MUST fail AC-1")
    print("  OK (expected FAIL): 200 unauthenticated -> AC-1 violation flagged")

    # 6. Live-probe verdict logic — AC-10 violated (403 even with a token).
    r = evaluate_live_probe(401, 403, has_token=True)
    check(not r.passed, "403 on an authenticated request MUST fail AC-10")
    print("  OK (expected FAIL): 403 authenticated -> AC-10 violation flagged")

    # 7. Live-probe verdict logic — no token supplied: AC-10 not checked, still
    #    passes on AC-1 alone.
    r = evaluate_live_probe(403, None, has_token=False)
    check(r.passed, "no token: AC-10 skipped, AC-1 alone should PASS")
    print("  OK: no --token -> AC-10 skipped, AC-1-only PASS")

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
            "Conformance probe for required-feature catalog Entry 1 "
            "(Administrator Console). See required-feature-catalog.md §Entry 1."
        )
    )
    parser.add_argument("--root", metavar="PATH", default=".",
                         help="Target repo root for the offline probe "
                              "(default: cwd).")
    parser.add_argument("--url", metavar="URL",
                         help="Base URL of a running instance for the live "
                              "probe (e.g. http://localhost:3000).")
    parser.add_argument("--token", metavar="TOKEN",
                         help="Administrator bearer token for the "
                              "authenticated live-probe leg.")
    parser.add_argument("--self-test", action="store_true",
                         help="Run the offline fixture round trip.")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if args.url:
        return run_live(args.url, args.token)

    root = Path(args.root).resolve()
    result = probe(root)
    print(result.report())
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
