#!/usr/bin/env python3
"""post_commit_gate.py — post-commit test + coverage force-correction gate (#361).

"Tests pass before a branch is done or merged" was guaranteed only by agent
procedure, not by a mechanical interlock — the 8 registered guard hooks are
all git-protocol guards (worktree/branch/push/stealth/sync), none inspect
test results. This module is the shared engine behind two real git hooks
(`.claude/hooks/post-commit`, `.claude/hooks/pre-commit`) that close that gap:

  - `post-commit` (mandatory install path once opted in): runs `recipe.py
    unit-test` (#360) plus a project-declared coverage command, and — because
    a post-commit hook cannot block a commit that already happened — emits a
    loud, deterministic "MUST FIX" signal on a red suite or sub-threshold
    coverage. This is FORCE-CORRECTION, not blocking.
  - `pre-commit` (OPTIONAL, opt-in via `mode: block`): runs the same check
    and refuses the commit (nonzero exit) on red, for projects that want a
    hard block. Git's own `--no-verify` is the human escape hatch for this
    variant (the existing repo convention — see `release.sh`'s
    `--no-verify`/`RELEASE_SKIP_VERIFY`).

Coverage floor is read from the EXISTING `code-quality.coverage-threshold`
config field (docs/coding-standards.md §Merge-gate quality enforcement) —
reused here, not reinvented. `null` = advisory only, never blocks or forces
correction on coverage (tests still gate independently).

Coverage MEASUREMENT is project-declared, not hardcoded per-stack here: a
project opts in by adding an `extras.coverage` entry to `.claude/recipes.json`
(the same informational-`extras` convention `smoke-visual` uses — never part
of the versioned build-recipe INTERFACE, never dispatched by `recipe.py`
itself) naming a `command` and a `parser` (one of `PARSERS`, matching the
issue's per-stack runner list: `pytest --cov` / `vitest --coverage` /
`cargo llvm-cov` / `go test -cover`). No `extras.coverage` entry ⇒ coverage
measurement is skipped (advisory note in the report), matching `recipe.py`'s
own "unimplemented ⇒ advisory, never a silent full-success claim" contract.

Design: docs/grimoire/design/runtime-verification-design.md §Post-commit test
+ coverage gate.

Usage:
  post_commit_gate.py [--mode postcommit|precommit] [--root PATH] [--self-test]
Exit: 0 pass/advisory/disabled; 1 force-correct-or-block failure (mode/step
dependent — see `evaluate()`); never silently swallows a real failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = ".claude/grimoire-config.json"
RECIPES_FILE = ".claude/recipes.json"
RECIPE_DISPATCHER = ".claude/skills/grm-build-recipe/recipe.py"
BASELINE_CACHE = ".claude/cache/coverage-baseline.json"

DEFAULT_ENABLED = False
DEFAULT_MODE = "force-correct"
VALID_MODES = {"block", "force-correct", "advisory"}

# Escape hatch, consistent with the existing --no-verify / RELEASE_SKIP_VERIFY
# convention (release.sh) — human-set in the shell, never defaulted on, never
# set by automation. `pre-commit`'s own escape hatch is git's native
# `--no-verify` (skips pre-commit entirely); this env var is for `post-commit`,
# which git provides no built-in skip for.
ESCAPE_HATCH_ENV = "GRIMOIRE_SKIP_POST_COMMIT_GATE"


def _scalar(v):
    """Unwrap a config value that may be a bare scalar or a {"value": ...} block.

    Mirrors `.claude/hooks/_hook_common.py::_scalar` — kept as a separate tiny
    copy rather than a cross-package import (same rationale install_doctor.py
    gives for its own copy: not worth a new import for one helper).
    """
    return v.get("value") if isinstance(v, dict) else v


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def gate_settings(cfg: dict) -> tuple[bool, str]:
    """Read code-quality.post-commit-test-gate.{enabled,mode}.

    Additive, absence-as-default (same idiom as web-app.auth / environments):
    a project with no block at all, or a pre-existing code-quality block that
    predates this field, reads as disabled — never forced on by a migration.
    """
    block = (cfg.get("code-quality") or {}).get("post-commit-test-gate")
    if not isinstance(block, dict):
        return DEFAULT_ENABLED, DEFAULT_MODE
    enabled = _scalar(block.get("enabled"))
    mode = _scalar(block.get("mode"))
    enabled = enabled if isinstance(enabled, bool) else DEFAULT_ENABLED
    mode = mode if mode in VALID_MODES else DEFAULT_MODE
    return enabled, mode


def coverage_threshold(cfg: dict):
    """Read code-quality.coverage-threshold (None / 0-100 / "delta"). Reused,
    not reinvented — the same field the v1.26 merge-gate quality dial reads."""
    cq = cfg.get("code-quality")
    if not isinstance(cq, dict):
        return None
    return _scalar(cq.get("coverage-threshold"))


# ---------------------------------------------------------------------------
# unit-test step
# ---------------------------------------------------------------------------

@dataclass
class UnitTestResult:
    ran: bool
    passed: bool | None
    exit_code: int
    output: str
    failed_tests: list = field(default_factory=list)


_FAILED_TEST_PATTERNS = (
    # pytest: "FAILED tests/test_foo.py::test_bar - AssertionError: ..."
    re.compile(r"^FAILED\s+(\S+)", re.MULTILINE),
    # vitest: "FAIL  src/foo.test.ts > suite > bar"
    re.compile(r"^\s*(?:×|FAIL)\s+(\S.*?)(?:\s{2,}|$)", re.MULTILINE),
    # cargo test: "test foo::bar ... FAILED"
    re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", re.MULTILINE),
    # go test: "--- FAIL: TestFoo (0.00s)"
    re.compile(r"^--- FAIL:\s+(\S+)", re.MULTILINE),
)


def _parse_failed_tests(output: str) -> list:
    """Best-effort, tool-agnostic extraction of failed test names, deduped in
    first-seen order (deterministic — every consumer must be able to act
    without re-running discovery)."""
    seen, out = set(), []
    for pat in _FAILED_TEST_PATTERNS:
        for m in pat.finditer(output):
            name = m.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def run_unit_tests(root: Path, runner=subprocess.run) -> UnitTestResult:
    dispatcher = root / RECIPE_DISPATCHER
    if not dispatcher.is_file():
        return UnitTestResult(ran=False, passed=None, exit_code=2, output="")
    proc = runner([sys.executable, str(dispatcher), "unit-test"],
                  cwd=str(root), capture_output=True, text=True)
    if proc.returncode == 2:
        # recipe.py's own "target not implemented" contract — advisory, not a
        # real red suite. Never claim a MUST-FIX over an unwired target.
        return UnitTestResult(ran=False, passed=None, exit_code=2,
                              output=proc.stdout + proc.stderr)
    output = proc.stdout + proc.stderr
    return UnitTestResult(ran=True, passed=(proc.returncode == 0),
                          exit_code=proc.returncode, output=output,
                          failed_tests=_parse_failed_tests(output))


# ---------------------------------------------------------------------------
# coverage step — project-declared command + one of these built-in parsers
# ---------------------------------------------------------------------------

@dataclass
class CoverageResult:
    ran: bool
    pct: float | None = None
    low_files: list = field(default_factory=list)
    note: str = ""
    exit_code: int = 0


PARSERS = {}


def _register(name):
    def deco(fn):
        PARSERS[name] = fn
        return fn
    return deco


@_register("pytest-term-missing")
def _parse_pytest(output: str):
    """`pytest --cov --cov-report=term-missing` TOTAL + per-file rows."""
    m = re.search(r"^TOTAL\s+\d+\s+\d+\s+(\d+)%", output, re.MULTILINE)
    pct = float(m.group(1)) if m else None
    low = []
    for line in output.splitlines():
        m2 = re.match(r"^(\S+\.py)\s+\d+\s+\d+\s+(\d+)%", line)
        if m2:
            low.append((m2.group(1), float(m2.group(2))))
    return pct, low


@_register("vitest-text")
def _parse_vitest(output: str):
    """`vitest --coverage` text-reporter summary table."""
    m = re.search(r"^All files\s*\|\s*([\d.]+)", output, re.MULTILINE)
    pct = float(m.group(1)) if m else None
    low = []
    for line in output.splitlines():
        m2 = re.match(r"^\s*([\w./-]+\.[jt]sx?)\s*\|\s*([\d.]+)", line)
        if m2 and not line.strip().startswith("All files"):
            low.append((m2.group(1), float(m2.group(2))))
    return pct, low


@_register("cargo-llvm-cov")
def _parse_cargo(output: str):
    """`cargo llvm-cov` summary TOTAL row (line-coverage %)."""
    m = re.search(r"^TOTAL\b.*?([\d.]+)%\s*$", output, re.MULTILINE)
    return (float(m.group(1)) if m else None), []


@_register("go-cover")
def _parse_go(output: str):
    """`go test -cover` / `go tool cover -func` total-statements line."""
    m = re.search(r"total:\s*\(statements\)\s*([\d.]+)%", output)
    return (float(m.group(1)) if m else None), []


def run_coverage(root: Path, runner=subprocess.run) -> CoverageResult:
    recipes = load_json(root / RECIPES_FILE)
    entry = (recipes.get("extras") or {}).get("coverage")
    if not isinstance(entry, dict) or not entry.get("implemented") or not entry.get("command"):
        return CoverageResult(ran=False,
                              note="no extras.coverage command configured in "
                                   f"{RECIPES_FILE} — coverage check skipped "
                                   "(advisory; unit-tests still gate)")
    parser = PARSERS.get(entry.get("parser"))
    if parser is None:
        return CoverageResult(ran=False,
                              note=f"extras.coverage.parser {entry.get('parser')!r} "
                                   f"unrecognized — valid: {sorted(PARSERS)}")
    proc = runner(entry["command"], shell=True, cwd=str(root),
                  capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    pct, low = parser(output)
    return CoverageResult(ran=True, pct=pct, low_files=low, exit_code=proc.returncode,
                          note="" if pct is not None else
                               "coverage command ran but no percentage could be parsed "
                               "from its output — check extras.coverage.parser matches "
                               "the actual command")


def evaluate_coverage(root: Path, pct: float | None, threshold, *, write_baseline=True):
    """Compare measured coverage against the floor. Returns (ok, message)."""
    if threshold is None:
        return True, "advisory (coverage-threshold=null)"
    if pct is None:
        return True, "advisory (coverage % could not be measured)"
    if threshold == "delta":
        cache_path = root / BASELINE_CACHE
        prev = load_json(cache_path).get("pct") if cache_path.exists() else None
        if prev is None:
            if write_baseline:
                _write_baseline(cache_path, pct)
            return True, f"{pct}% (delta mode: no prior baseline — recorded as new baseline)"
        ok = pct >= prev
        if ok and write_baseline:
            _write_baseline(cache_path, pct)
        return ok, f"{pct}% vs prior baseline {prev}% (delta mode)"
    # numeric 0-100 floor
    ok = pct >= float(threshold)
    return ok, f"{pct}% vs required {threshold}%"


def _write_baseline(cache_path: Path, pct: float) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pct": pct}), encoding="utf-8")
    os.replace(tmp, cache_path)


# ---------------------------------------------------------------------------
# reporting — deterministic, machine-parseable MUST-FIX block
# ---------------------------------------------------------------------------

MUST_FIX_MARKER = "MUST FIX"


def format_report(unit: UnitTestResult, cov: CoverageResult, threshold,
                  cov_ok: bool, cov_msg: str, gate_mode: str) -> str:
    lines = ["=== GRIMOIRE POST-COMMIT GATE (#361) ===", f"mode: {gate_mode}"]
    if not unit.ran:
        lines.append("unit-tests: SKIPPED (recipe.py unit-test unimplemented — advisory)")
    else:
        lines.append(f"unit-tests: {'PASS' if unit.passed else 'FAIL'} (exit {unit.exit_code})")
        if not unit.passed and unit.failed_tests:
            lines.append("  failed:")
            for t in unit.failed_tests:
                lines.append(f"    - {t}")
    if cov.ran:
        lines.append(f"coverage: {cov_msg}" + ("" if cov_ok else "  <- BELOW THRESHOLD"))
        if cov.low_files and not cov_ok:
            lines.append("  under-threshold files:")
            for fname, pct in cov.low_files:
                lines.append(f"    - {fname}: {pct}%")
    else:
        lines.append(f"coverage: SKIPPED ({cov.note})")
    unit_failed = unit.ran and not unit.passed
    if unit_failed or not cov_ok:
        lines.append("")
        lines.append(f"{MUST_FIX_MARKER}: red suite or sub-threshold coverage detected.")
        lines.append("  Noir: auto-correct now (amend or follow-up commit) before continuing.")
        lines.append("  Supervised/Weiss: surface this to the user before continuing.")
        lines.append(f"  Escape hatch (human-confirmed, rare intentional WIP commit): "
                     f"{ESCAPE_HATCH_ENV}=1 git commit ...")
    lines.append("=========================================")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def evaluate(root: Path, *, unit_runner=subprocess.run, cov_runner=subprocess.run) -> tuple:
    """Run the full gate once. Returns (exit_code, report_text, disabled: bool)."""
    cfg = load_json(root / CONFIG_FILE)
    enabled, gate_mode = gate_settings(cfg)
    if not enabled:
        return 0, "", True
    if os.environ.get(ESCAPE_HATCH_ENV) == "1":
        return 0, f"post_commit_gate: skipped via {ESCAPE_HATCH_ENV}=1 (escape hatch)", True

    unit = run_unit_tests(root, runner=unit_runner)
    threshold = coverage_threshold(cfg)
    cov = run_coverage(root, runner=cov_runner)
    cov_ok, cov_msg = (evaluate_coverage(root, cov.pct, threshold) if cov.ran
                       else (True, "skipped"))

    report = format_report(unit, cov, threshold, cov_ok, cov_msg, gate_mode)
    unit_failed = unit.ran and not unit.passed
    failed = unit_failed or not cov_ok

    if not failed:
        return 0, report, False
    # force-correct/block both surface the failure with a nonzero exit code
    # (post-commit: git ignores it, but any wrapper/CI watching does not, and
    # it is the "loud" half of force-correction); advisory always exits 0.
    return (1 if gate_mode in ("force-correct", "block") else 0), report, False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("postcommit", "precommit"), default="postcommit",
                    help="postcommit: force-correct/advisory per config, never blocks "
                         "the commit itself. precommit: blocks (nonzero exit) only when "
                         "mode=block; a no-op degrade otherwise (documented opt-in only).")
    ap.add_argument("--root", default=None, help="repo root (default: git rev-parse --show-toplevel)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.root:
        root = Path(args.root)
    else:
        try:
            out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=5)
            root = Path(out.stdout.strip()) if out.returncode == 0 else Path(".")
        except (OSError, subprocess.SubprocessError):
            root = Path(".")

    exit_code, report, disabled = evaluate(root)
    if disabled:
        if report:
            print(report, file=sys.stderr)
        return 0
    print(report, file=sys.stderr)
    if args.mode == "precommit":
        # The optional blocking variant only ever refuses the commit in
        # mode=block (documented opt-in) — a force-correct/advisory config
        # accidentally wired into pre-commit degrades to a warning, never a
        # surprise block.
        cfg = load_json(root / CONFIG_FILE)
        _, gate_mode = gate_settings(cfg)
        return exit_code if gate_mode == "block" else 0
    return exit_code


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    import tempfile
    failures = []

    # --- gate_settings: absence-as-default, valid/invalid mode coercion ----
    en, mode = gate_settings({})
    if (en, mode) != (False, "force-correct"):
        failures.append("gate_settings: absent block should default (False, force-correct)")
    en, mode = gate_settings({"code-quality": {"post-commit-test-gate":
                             {"enabled": True, "mode": "block"}}})
    if (en, mode) != (True, "block"):
        failures.append("gate_settings: explicit enabled/mode not honored")
    en, mode = gate_settings({"code-quality": {"post-commit-test-gate":
                             {"enabled": {"value": True}, "mode": {"value": "advisory"}}}})
    if (en, mode) != (True, "advisory"):
        failures.append("gate_settings: {value:...} wrapper form not honored")
    en, mode = gate_settings({"code-quality": {"post-commit-test-gate":
                             {"enabled": True, "mode": "bogus"}}})
    if mode != "force-correct":
        failures.append("gate_settings: invalid mode should fall back to force-correct")

    # --- coverage_threshold: null / int / delta -----------------------------
    if coverage_threshold({}) is not None:
        failures.append("coverage_threshold: absent code-quality should be None")
    if coverage_threshold({"code-quality": {"coverage-threshold": 80}}) != 80:
        failures.append("coverage_threshold: numeric not honored")
    if coverage_threshold({"code-quality": {"coverage-threshold": "delta"}}) != "delta":
        failures.append("coverage_threshold: delta not honored")

    # --- failed-test parsing (deterministic, tool-agnostic) -----------------
    pytest_out = "FAILED tests/test_foo.py::test_bar - AssertionError\n"
    if _parse_failed_tests(pytest_out) != ["tests/test_foo.py::test_bar"]:
        failures.append("_parse_failed_tests: pytest pattern failed: %r"
                        % _parse_failed_tests(pytest_out))
    cargo_out = "test foo::bar_case ... FAILED\n"
    if _parse_failed_tests(cargo_out) != ["foo::bar_case"]:
        failures.append("_parse_failed_tests: cargo pattern failed: %r"
                        % _parse_failed_tests(cargo_out))
    go_out = "--- FAIL: TestFoo (0.00s)\n"
    if _parse_failed_tests(go_out) != ["TestFoo"]:
        failures.append("_parse_failed_tests: go pattern failed: %r"
                        % _parse_failed_tests(go_out))

    # --- coverage parsers -----------------------------------------------------
    pyt = ("Name       Stmts   Miss  Cover\n"
          "src/a.py      10      2    80%\n"
          "src/b.py      10      6    40%\n"
          "TOTAL         20      8    60%\n")
    pct, low = _parse_pytest(pyt)
    if pct != 60.0 or ("src/b.py", 40.0) not in low:
        failures.append("_parse_pytest: got pct=%r low=%r" % (pct, low))

    vit = ("File          | % Stmts |\n"
          "All files     |   72.50 |\n"
          " src/a.ts     |   90.00 |\n"
          " src/b.ts     |   55.00 |\n")
    pct, low = _parse_vitest(vit)
    if pct != 72.5 or ("src/b.ts", 55.0) not in low:
        failures.append("_parse_vitest: got pct=%r low=%r" % (pct, low))

    cargo_cov = "filename    Regions  Missed  Cover\nTOTAL       100      12      88.00%\n"
    pct, low = _parse_cargo(cargo_cov)
    if pct != 88.0:
        failures.append("_parse_cargo: got pct=%r" % pct)

    go_cov = "ok  	pkg/foo	0.014s	coverage: 0.0% of statements\ntotal:\t(statements)\t76.4%\n"
    pct, low = _parse_go(go_cov)
    if pct != 76.4:
        failures.append("_parse_go: got pct=%r" % pct)

    # --- evaluate_coverage: numeric / delta (first-run, pass, fail) ---------
    ok, msg = evaluate_coverage(Path("/nonexistent"), 85.0, 80)
    if not ok:
        failures.append("evaluate_coverage: 85 >= 80 should pass")
    ok, msg = evaluate_coverage(Path("/nonexistent"), 70.0, 80)
    if ok:
        failures.append("evaluate_coverage: 70 < 80 should fail")
    ok, msg = evaluate_coverage(Path("/nonexistent"), 90.0, None)
    if not ok or "advisory" not in msg:
        failures.append("evaluate_coverage: null threshold should always pass advisory")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ok, msg = evaluate_coverage(root, 50.0, "delta")
        if not ok or "new baseline" not in msg:
            failures.append("evaluate_coverage: delta first-run should pass and record baseline")
        cache = root / BASELINE_CACHE
        if not cache.exists() or load_json(cache).get("pct") != 50.0:
            failures.append("evaluate_coverage: delta first-run should write baseline pct")
        ok, msg = evaluate_coverage(root, 55.0, "delta")
        if not ok:
            failures.append("evaluate_coverage: delta 55 >= prior 50 should pass")
        ok, msg = evaluate_coverage(root, 40.0, "delta")
        if ok:
            failures.append("evaluate_coverage: delta 40 < prior 55 should fail")
        # a failing delta comparison must NOT advance the baseline.
        if load_json(cache).get("pct") != 55.0:
            failures.append("evaluate_coverage: a failing delta check should not move the baseline")

    # --- run_unit_tests: injected runner, unimplemented / pass / fail -------
    class _FakeProc:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def _runner_unimplemented(*a, **k):
        return _FakeProc(2, "", "recipe: target 'unit-test' is not implemented\n")

    def _runner_pass(*a, **k):
        return _FakeProc(0, "5 passed\n", "")

    def _runner_fail(*a, **k):
        return _FakeProc(1, "", "FAILED tests/test_x.py::test_y - boom\n")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ".claude" / "skills" / "grm-build-recipe").mkdir(parents=True)
        (root / RECIPE_DISPATCHER).write_text("# stub\n", encoding="utf-8")

        r = run_unit_tests(root, runner=_runner_unimplemented)
        if r.ran or r.exit_code != 2:
            failures.append("run_unit_tests: exit-2 unimplemented should set ran=False")
        r = run_unit_tests(root, runner=_runner_pass)
        if not (r.ran and r.passed):
            failures.append("run_unit_tests: exit-0 should be ran+passed")
        r = run_unit_tests(root, runner=_runner_fail)
        if not (r.ran and not r.passed) or r.failed_tests != ["tests/test_x.py::test_y"]:
            failures.append("run_unit_tests: exit-1 should be ran+failed with parsed names: %r"
                            % r.failed_tests)

        # dispatcher absent entirely -> ran=False, never a crash.
        (root / RECIPE_DISPATCHER).unlink()
        r = run_unit_tests(root, runner=_runner_pass)
        if r.ran:
            failures.append("run_unit_tests: missing dispatcher should be ran=False")

    # --- run_coverage: no extras.coverage / bad parser / real parse ---------
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ".claude").mkdir(parents=True)
        r = run_coverage(root)
        if r.ran:
            failures.append("run_coverage: absent recipes.json should not run")

        (root / RECIPES_FILE).write_text(json.dumps({
            "extras": {"coverage": {"command": "echo", "implemented": True,
                                    "parser": "no-such-parser"}}}), encoding="utf-8")
        r = run_coverage(root)
        if r.ran:
            failures.append("run_coverage: unrecognized parser should not run")

        (root / RECIPES_FILE).write_text(json.dumps({
            "extras": {"coverage": {"command": "cat", "implemented": True,
                                    "parser": "pytest-term-missing"}}}), encoding="utf-8")

        def _cov_runner(cmd, **k):
            return _FakeProc(0, "TOTAL   20   5   75%\n", "")

        r = run_coverage(root, runner=_cov_runner)
        if not r.ran or r.pct != 75.0:
            failures.append("run_coverage: real parse path failed: %r" % r)

        # extras present but not implemented -> advisory skip, never a crash.
        (root / RECIPES_FILE).write_text(json.dumps({
            "extras": {"coverage": {"command": None, "implemented": False}}}), encoding="utf-8")
        r = run_coverage(root)
        if r.ran:
            failures.append("run_coverage: implemented=false should not run")

    # --- format_report: MUST-FIX marker present iff a real failure ----------
    pass_unit = UnitTestResult(ran=True, passed=True, exit_code=0, output="")
    fail_unit = UnitTestResult(ran=True, passed=False, exit_code=1, output="",
                               failed_tests=["a::b"])
    no_cov = CoverageResult(ran=False, note="skip")
    rep = format_report(pass_unit, no_cov, None, True, "skipped", "force-correct")
    if MUST_FIX_MARKER in rep:
        failures.append("format_report: a passing gate must not carry MUST FIX")
    rep = format_report(fail_unit, no_cov, None, True, "skipped", "force-correct")
    if MUST_FIX_MARKER not in rep or "a::b" not in rep:
        failures.append("format_report: a failing gate must carry MUST FIX + failed test name")

    # --- evaluate(): disabled / escape-hatch / pass / force-correct fail ----
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ".claude" / "skills" / "grm-build-recipe").mkdir(parents=True)
        (root / RECIPE_DISPATCHER).write_text("# stub\n", encoding="utf-8")

        # disabled by default (no config at all).
        code, report, disabled = evaluate(root)
        if not disabled or code != 0:
            failures.append("evaluate: no config should be disabled, exit 0")

        (root / CONFIG_FILE).write_text(json.dumps({
            "code-quality": {"post-commit-test-gate": {"enabled": True, "mode": "force-correct"}}}),
            encoding="utf-8")

        code, report, disabled = evaluate(root, unit_runner=_runner_pass)
        if disabled or code != 0 or MUST_FIX_MARKER in report:
            failures.append("evaluate: enabled + passing suite should exit 0, no MUST FIX")

        code, report, disabled = evaluate(root, unit_runner=_runner_fail)
        if disabled or code != 1 or MUST_FIX_MARKER not in report:
            failures.append("evaluate: enabled + red suite (force-correct) should exit 1 + MUST FIX")

        # advisory mode never fails the exit code even on red.
        (root / CONFIG_FILE).write_text(json.dumps({
            "code-quality": {"post-commit-test-gate": {"enabled": True, "mode": "advisory"}}}),
            encoding="utf-8")
        code, report, disabled = evaluate(root, unit_runner=_runner_fail)
        if code != 0 or MUST_FIX_MARKER not in report:
            failures.append("evaluate: advisory mode must report but never fail the exit code")

        # escape hatch skips entirely, regardless of mode/suite state.
        (root / CONFIG_FILE).write_text(json.dumps({
            "code-quality": {"post-commit-test-gate": {"enabled": True, "mode": "force-correct"}}}),
            encoding="utf-8")
        os.environ[ESCAPE_HATCH_ENV] = "1"
        try:
            code, report, disabled = evaluate(root, unit_runner=_runner_fail)
        finally:
            del os.environ[ESCAPE_HATCH_ENV]
        if not disabled or code != 0:
            failures.append("evaluate: escape hatch should skip entirely (disabled, exit 0)")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("post_commit_gate self-test: OK (gate_settings absence-as-default, "
         "coverage_threshold null/int/delta, failed-test parsing (pytest/cargo/go), "
         "4 coverage parsers, evaluate_coverage numeric+delta incl. non-advancing "
         "baseline on fail, run_unit_tests unimplemented/pass/fail/missing-dispatcher, "
         "run_coverage no-extras/bad-parser/real-parse/not-implemented, "
         "format_report MUST-FIX gating, evaluate() disabled/escape-hatch/pass/"
         "force-correct/advisory)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
