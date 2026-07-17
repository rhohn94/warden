#!/usr/bin/env python3
"""logging_conformance.py — conformance probe for the required-feature
catalog's Entry 9 (Structured Logging; #435, v3.99 R8 Pass 1).

Modeled on `admin_console_conformance.py`'s two-mode shape (an offline
self-test mode and a live mode that spawns a real process — never a network
call, so unlike the `--url` live legs elsewhere in this skill directory, this
one IS exercised end-to-end by --self-test via local fixture scripts):

  Offline, static (no running app required):
    A best-effort, informational-only scan of the target repo's source tree
    for a call site invoking the standardized logging init module
    (`logging_init::init(` for the Rust starter module, `init_logging(` for
    the Python one) — the same "informational only, never a pass/fail input"
    discipline `admin_console_conformance.py`'s static route scan already
    established, for the identical reason: a static per-family call-site scan
    is inherently best-effort (it cannot tell "init call present" from "init
    call present AND actually reached before the first log line"). The
    offline mode's ONE pass/fail input is a coarse presence signal, exactly
    admin_console's: a repo with a `src/` (or top-level `.py` files) tree and
    zero reference to either init call is WARNed — not proof of absence, but
    a real, cheap smell.

  Live (`--boot-probe CMD [ARGS...]`):
    The ONE mechanically-clean sub-requirement this script CAN verify without
    deep knowledge of the app's internals: spawn the given command, capture
    its FIRST line of stdout, and validate it is a JSON object carrying
    exactly the standard field contract (docs/coding-standards.md §Logging):
    `ts` (int, ms since Unix epoch), `level` (one of
    trace/debug/info/warn/error), `target`, `msg`, `correlation_id`,
    `instance`, `version` (all strings). This is the catalog entry's own
    "cheap check: emit one line at boot, validate shape" — nothing about log
    CONTENT, rotation, or downstream consumption (the Admin Console log
    viewer, AC-4) is probed here; that is a human/agent review the same way
    Entry 1's AC-2..AC-9 are.

    Unlike the `--url` live legs (admin_console_conformance.py,
    aura_channel_conformance.py — both require a running network service and
    are therefore NEVER exercised by --self-test), spawning a short-lived
    local process touches no network at all. `--self-test` exercises this
    leg directly against two local fixture scripts (one conformant, one
    not) — the "demonstrably passes / demonstrably fails" acceptance
    criterion for this entry.

CLI:
    python3 logging_conformance.py --root PATH
    python3 logging_conformance.py [--timeout SECONDS] --boot-probe CMD [ARG ...]
    python3 logging_conformance.py --self-test

Design authority: required-feature-catalog.md §Entry 9;
docs/coding-standards.md §Logging (field contract).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# The standard field contract (docs/coding-standards.md §Logging) — the
# exact key set every conforming boot line MUST carry. Kept as a single
# source of truth here rather than re-typed in the self-test fixtures below.
REQUIRED_FIELDS = frozenset({
    "ts", "level", "target", "msg", "correlation_id", "instance", "version",
})

# The cross-language level vocabulary both starter modules (Rust
# logging_init.rs / Python logging_init.py) normalize onto — see
# docs/coding-standards.md §Logging.
VALID_LEVELS = frozenset({"trace", "debug", "info", "warn", "error"})

# Best-effort static call-site markers, one per starter-module language.
_RUST_MARKER = "logging_init::init("
_PYTHON_MARKER = "init_logging("
_DEFAULT_TIMEOUT_SECONDS = 5.0


# ── Finding collector (mirrors admin_console_conformance.py's
#    ConformanceResult so every conformance script in this skill directory
#    reads the same way) ──────────────────────────────────────────────────

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


# ── Offline mode: static, informational-only call-site scan ────────────────

def scan_logging_init_reference(root: Path) -> tuple[str, str]:
    """Best-effort, static scan for a `logging_init` call site in source.

    Returns (status, detail) with status in {"found", "not-found",
    "not-applicable"} — mirrors admin_console_conformance.py's
    scan_admin_console_reference() shape/contract exactly (informational
    input, never itself the sole pass/fail driver)."""
    scan_dirs = [root / "src"]
    py_files = list(root.glob("*.py"))
    existing_dirs = [d for d in scan_dirs if d.is_dir()]
    if not existing_dirs and not py_files:
        return "not-applicable", (
            "no src/ tree or top-level *.py files found — nothing to scan.")

    candidates: list[Path] = list(py_files)
    for d in existing_dirs:
        candidates.extend(p for p in sorted(d.rglob("*")) if p.is_file())

    for p in candidates:
        if p.suffix not in (".rs", ".py"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _RUST_MARKER in text or _PYTHON_MARKER in text:
            return "found", (
                f"found a logging-init call site in "
                f"{p.relative_to(root).as_posix()}.")
    return "not-found", (
        "no logging-init call site ('logging_init::init(' or "
        "'init_logging(') found under src/ or top-level *.py files — Entry 9 "
        "(Structured Logging) expects ONE call at process start; see "
        "required-feature-catalog.md §Entry 9.")


def probe(root: Path) -> ConformanceResult:
    """Offline probe — static source scan only, no running app required."""
    result = ConformanceResult(str(root))
    status, detail = scan_logging_init_reference(root)
    result.note(detail)
    if status == "not-found":
        result.warn(
            "no static logging-init call site detected — this is a "
            "best-effort heuristic (informational-grade), not proof of "
            "absence. Run with --boot-probe CMD against the app's real "
            "entrypoint for the mechanically-checkable shape check.")
    result.note(
        "This offline probe only checks for a static call-site reference. "
        "It does NOT verify the emitted line's shape — run with "
        "--boot-probe CMD [ARGS...] against the app's real entrypoint for "
        "that check.")
    return result


# ── Live mode (spawns a local process — no network, so --self-test DOES
#    exercise this leg directly) ─────────────────────────────────────────

def validate_boot_line(line: str) -> ConformanceResult:
    """Pure verdict logic, separated from the process spawn (mirrors
    admin_console_conformance.py's evaluate_live_probe/fetch split) so the
    shape-validation itself is directly self-testable against a literal
    string, independent of subprocess plumbing."""
    result = ConformanceResult("boot-line")
    line = line.strip()
    if not line:
        result.error("no output captured on stdout — expected exactly one "
                      "JSON boot line before anything else.")
        return result

    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        result.error(f"first stdout line is not valid JSON ({exc}): {line!r}")
        return result

    if not isinstance(payload, dict):
        result.error(f"first stdout line decodes to a JSON {type(payload).__name__}, "
                      f"not an object: {line!r}")
        return result

    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        result.error(
            f"boot line is missing required field(s) "
            f"{sorted(missing)} — the standard contract is "
            f"{sorted(REQUIRED_FIELDS)} (docs/coding-standards.md §Logging).")

    extra = payload.keys() - REQUIRED_FIELDS
    if extra:
        result.warn(f"boot line carries extra field(s) {sorted(extra)} "
                     f"beyond the standard contract — allowed, but note it.")

    if "ts" in payload and not isinstance(payload["ts"], int):
        result.error(f"'ts' must be an int (ms since Unix epoch), got "
                      f"{type(payload['ts']).__name__}: {payload['ts']!r}")

    if "level" in payload:
        level = payload["level"]
        if not isinstance(level, str) or level not in VALID_LEVELS:
            result.error(
                f"'level' must be one of {sorted(VALID_LEVELS)}, got "
                f"{level!r}")

    for key in ("target", "msg", "correlation_id", "instance", "version"):
        if key in payload and not isinstance(payload[key], str):
            result.error(f"'{key}' must be a string, got "
                         f"{type(payload[key]).__name__}: {payload[key]!r}")

    if result.passed:
        result.note("boot line conforms to the standard field contract.")
    return result


def probe_boot_line(cmd: list[str],
                     timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> ConformanceResult:
    """Spawn *cmd*, capture ONLY its first stdout line, and validate its
    shape — then terminate the process regardless of whether it exits on
    its own. Never touches the network — a local process only.

    Deliberately does NOT wait for the command to exit (unlike a plain
    `subprocess.run(..., timeout=...)`, which would time out — a false
    negative — against a real starter-template server/GUI process that
    blocks forever after printing its boot line, e.g. the service quick-start
    template's HTTP listener or the gui template's native window). A
    background thread reads one line off stdout; the wall-clock timeout below
    bounds only "how long to wait for that first line", not the process's
    total lifetime.
    """
    import threading

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        result = ConformanceResult("boot-probe")
        result.error(f"failed to run boot-probe command {cmd!r}: {exc}")
        return result

    line_holder: dict[str, str] = {}

    def _read_first_line() -> None:
        line_holder["line"] = proc.stdout.readline()

    reader = threading.Thread(target=_read_first_line, daemon=True)
    reader.start()
    reader.join(timeout)
    first_line = line_holder.get("line", "")

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)

    result = validate_boot_line(first_line)
    result.label = f"boot-probe: {' '.join(cmd)}"
    return result


def run_boot_probe(cmd: list[str], timeout: float) -> int:
    result = probe_boot_line(cmd, timeout=timeout)
    print(result.report())
    return 0 if result.passed else 1


# ── Self-test (offline scan + the boot-probe leg via local fixture scripts —
#    no network anywhere) ────────────────────────────────────────────────

# A starter-template-shaped conformant boot line (mirrors the actual output
# of both logging_init.rs and logging_init.py).
_CONFORMANT_LINE = (
    '{"ts": 1784000221894, "level": "info", "target": "cli-app", '
    '"msg": "boot: cli-app starting", "correlation_id": "", '
    '"instance": "local", "version": "0.1.0"}'
)

# A non-conformant fixture — the OLD env_logger-shaped plain-text line this
# entry exists to retire (docs/coding-standards.md §Logging's own rationale).
_NONCONFORMANT_LINE = "[2026-07-13T00:00:00Z INFO  cli_app] cli-app listening on 127.0.0.1:3000"


def _write_fixture_script(path: Path, line: str) -> None:
    path.write_text(
        "import sys\n"
        f"print({line!r})\n"
        "sys.stdout.flush()\n",
        encoding="utf-8")


def run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("logging_conformance.py --self-test")

    # 1. Pure shape-validation logic (no subprocess involved).
    r = validate_boot_line(_CONFORMANT_LINE)
    check(r.passed, f"conformant line must PASS: {r.report()}")
    print("  OK: conformant boot line -> PASS")

    r = validate_boot_line(_NONCONFORMANT_LINE)
    check(not r.passed, "the old env_logger-shaped plain-text line must FAIL")
    print("  OK (expected FAIL): plain-text (non-JSON) line -> FLAGGED")

    r = validate_boot_line("")
    check(not r.passed, "empty stdout must FAIL")
    print("  OK (expected FAIL): empty stdout -> FLAGGED")

    r = validate_boot_line('{"msg": "boot", "level": "info"}')
    check(not r.passed, "a JSON object missing required fields must FAIL")
    check(any("missing required field" in e for e in r.errors),
          "missing-field finding should name the gap")
    print("  OK (expected FAIL): JSON missing required fields -> FLAGGED")

    r = validate_boot_line(
        '{"ts": 1, "level": "information", "target": "x", "msg": "y", '
        '"correlation_id": "", "instance": "local", "version": "0.1.0"}')
    check(not r.passed, "an out-of-vocabulary level must FAIL")
    print("  OK (expected FAIL): out-of-vocabulary level -> FLAGGED")

    r = validate_boot_line(
        '{"ts": "not-a-number", "level": "info", "target": "x", "msg": "y", '
        '"correlation_id": "", "instance": "local", "version": "0.1.0"}')
    check(not r.passed, "a non-int ts must FAIL")
    print("  OK (expected FAIL): non-int 'ts' -> FLAGGED")

    r = validate_boot_line(
        '{"ts": 1, "level": "info", "target": "x", "msg": "y", '
        '"correlation_id": "", "instance": "local", "version": "0.1.0", '
        '"extra": "field"}')
    check(r.passed and any("extra field" in w for w in r.warnings),
          "an unknown extra field should WARN, not FAIL")
    print("  OK: extra field beyond the contract -> WARN, still PASS")

    # 2. The boot-probe leg — spawns a local fixture script (no network), so
    #    this DOES exercise the full subprocess path in --self-test, unlike
    #    the --url live legs elsewhere in this skill directory.
    with tempfile.TemporaryDirectory() as td:
        conformant = Path(td) / "conformant_fixture.py"
        _write_fixture_script(conformant, _CONFORMANT_LINE)
        r = probe_boot_line([sys.executable, str(conformant)])
        check(r.passed, f"boot-probe against a conformant fixture must "
                        f"PASS: {r.report()}")
        print("  OK: boot-probe against a conformant fixture script -> PASS")

        nonconformant = Path(td) / "nonconformant_fixture.py"
        _write_fixture_script(nonconformant, _NONCONFORMANT_LINE)
        r = probe_boot_line([sys.executable, str(nonconformant)])
        check(not r.passed, "boot-probe against a non-conformant fixture "
                            "must FAIL")
        print("  OK (expected FAIL): boot-probe against a non-conformant "
              "fixture script -> FLAGGED")

        r = probe_boot_line([sys.executable, str(Path(td) / "does-not-exist.py")])
        check(not r.passed, "boot-probe against an unrunnable command must "
                            "FAIL, not crash")
        print("  OK (expected FAIL): boot-probe against a missing script "
              "-> FLAGGED, no crash")

    # 3. Offline static scan.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        r = probe(root)
        check(r.passed, "missing call-site ref is a WARN, not a hard "
                        "failure (offline heuristic, not proof of absence)")
        check(any("no static logging-init" in w for w in r.warnings),
              "should warn about the missing static reference")
        print("  OK: src/ present, no init call site -> WARN (still passes)")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "src" / "main.rs").write_text(
            "mod logging_init;\n"
            "fn main() { logging_init::init(\"info\", \"local\", \"0.1.0\"); }\n",
            encoding="utf-8")
        r = probe(root)
        check(r.passed and not r.warnings,
              "a real logging_init::init( call site should produce no warning")
        print("  OK: Rust init call site present -> clean PASS")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "app.py").write_text(
            "from logging_init import init_logging\n"
            "init_logging(level='info')\n",
            encoding="utf-8")
        r = probe(root)
        check(r.passed and not r.warnings,
              "a real init_logging( call site should produce no warning")
        print("  OK: Python init call site present -> clean PASS")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        r = probe(root)
        check(r.passed and not r.warnings,
              "no src/ tree and no top-level *.py files -> not-applicable, "
              "never warns")
        print("  OK: no source tree at all -> not-applicable, no warning")

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} unexpected result(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELF-TEST PASSED.")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Conformance probe for required-feature catalog Entry 9 "
            "(Structured Logging). See required-feature-catalog.md §Entry 9."
        )
    )
    parser.add_argument("--root", metavar="PATH", default=".",
                         help="Target repo root for the offline probe "
                              "(default: cwd).")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS,
                         help=f"Boot-probe timeout in seconds "
                              f"(default: {_DEFAULT_TIMEOUT_SECONDS}). MUST "
                              f"be given BEFORE --boot-probe (see below).")
    parser.add_argument("--boot-probe", metavar="CMD", nargs=argparse.REMAINDER,
                         help="Command (and args) to spawn for the live "
                              "boot-line probe, e.g. --boot-probe "
                              "./target/debug/cli-app. MUST be the LAST "
                              "argument — argparse.REMAINDER consumes "
                              "everything after it (including a trailing "
                              "--timeout), so put --timeout earlier.")
    parser.add_argument("--self-test", action="store_true",
                         help="Run the offline + boot-probe fixture round "
                              "trip.")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if args.boot_probe:
        return run_boot_probe(args.boot_probe, args.timeout)

    root = Path(args.root).resolve()
    result = probe(root)
    print(result.report())
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
