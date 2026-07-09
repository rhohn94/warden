"""
verify_isolation.py — post-dispatch isolation assertion helper (stdlib only).

Provides three assertions for the chip-free Noir era (post v3.32):
  - parse_isolation_footer: detect the worktreePath/worktreeBranch footer in
    an agent result; absence signals in-place execution.
  - check_head_on_staging: assert HEAD matches the expected staging branch.
  - assert_branch_advanced: assert a feature branch carries new commits beyond
    the staging tip.

CLI: python3 verify_isolation.py --result-file <path> --staging-branch <ref>
     Exit 0 = footer present and HEAD on expected staging branch.
     Exit nonzero = footer absent or HEAD drifted (do not merge).

Design reference: docs/design/dispatch-hardening-design.md §7
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_isolation_footer(result_text: str) -> Optional[dict]:
    """Extract worktreePath and worktreeBranch from an agent result footer.

    A correctly-isolated Agent result ends with lines of the form:
        worktreePath: /some/path
        worktreeBranch: some-branch-name

    The lines may appear anywhere in the final portion of the text; we scan
    the whole result so that minor trailing whitespace does not cause false
    negatives.

    Returns:
        dict with keys 'worktreePath' and 'worktreeBranch' if both are present,
        None if either is absent (footerless — treat as in-place execution).
    """
    path_match = re.search(r"^worktreePath:\s*(.+)$", result_text, re.MULTILINE)
    branch_match = re.search(r"^worktreeBranch:\s*(.+)$", result_text, re.MULTILINE)

    if path_match and branch_match:
        return {
            "worktreePath": path_match.group(1).strip(),
            "worktreeBranch": branch_match.group(1).strip(),
        }
    return None


def check_head_on_staging(expected_branch: str) -> bool:
    """Assert that git HEAD is on the expected staging branch.

    Runs `git symbolic-ref --short HEAD` and compares the output to
    expected_branch (exact match, stripped).

    Returns:
        True if HEAD matches expected_branch exactly.
        False if HEAD is on a different branch, is detached, or git is
        unavailable / the working directory is not a git repo.
    """
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Detached HEAD or not a git repo.
            return False
        actual = result.stdout.strip()
        return actual == expected_branch
    except FileNotFoundError:
        # git not available in PATH.
        return False


def assert_branch_advanced(staging_branch: str, feature_branch: str) -> bool:
    """Assert that feature_branch carries at least one commit beyond staging_branch.

    Runs `git rev-list --count {staging_branch}..{feature_branch}` and checks
    whether the count is greater than zero.

    Returns:
        True if the feature branch is ahead of the staging branch by at least
        one commit (i.e. contains new work).
        False if the count is zero (branch not advanced), either ref is missing,
        or git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{staging_branch}..{feature_branch}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        count_str = result.stdout.strip()
        if not count_str.isdigit():
            return False
        return int(count_str) > 0
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> int:
    """CLI entry point; returns exit code."""
    parser = argparse.ArgumentParser(
        description="Post-dispatch isolation assertion helper for Noir Grimoire releases.",
    )
    parser.add_argument(
        "--result-file",
        metavar="PATH",
        help="Path to a file containing the raw agent result text.",
    )
    parser.add_argument(
        "--staging-branch",
        metavar="REF",
        help="Expected HEAD staging branch (e.g. version/3.34).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run offline unit tests using inline fixtures (no git required).",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return _run_self_test()

    if not args.result_file or not args.staging_branch:
        parser.error("--result-file and --staging-branch are required (unless --self-test).")

    try:
        with open(args.result_file, "r", encoding="utf-8") as fh:
            result_text = fh.read()
    except OSError as exc:
        print(f"ERROR: cannot read result file: {exc}", file=sys.stderr)
        return 2

    exit_code = 0

    # Check 1: footer presence.
    footer = parse_isolation_footer(result_text)
    if footer is None:
        print(
            "FAIL: footerless agent result — worktreePath/worktreeBranch absent. "
            "Treat as in-place execution; do NOT merge.",
            file=sys.stderr,
        )
        exit_code = 1
    else:
        print(f"OK: footer present — worktreePath={footer['worktreePath']!r}, "
              f"worktreeBranch={footer['worktreeBranch']!r}")

    # Check 2: HEAD on staging.
    if check_head_on_staging(args.staging_branch):
        print(f"OK: HEAD is on expected staging branch {args.staging_branch!r}.")
    else:
        print(
            f"FAIL: HEAD is NOT on {args.staging_branch!r}. "
            "HEAD may have drifted — do NOT merge.",
            file=sys.stderr,
        )
        exit_code = 1

    return exit_code


# ---------------------------------------------------------------------------
# Self-test (offline, no git required)
# ---------------------------------------------------------------------------


def _run_self_test() -> int:
    """Run offline tests against inline fixtures. Returns 0 if all pass."""
    failures: list[str] = []

    # --- parse_isolation_footer ---

    # Case 1: both fields present at end of result.
    good_result = (
        "Some agent output.\n"
        "Implementation complete.\n"
        "worktreePath: /tmp/wt/abc123\n"
        "worktreeBranch: feat/vh1-test\n"
    )
    got = parse_isolation_footer(good_result)
    if got != {"worktreePath": "/tmp/wt/abc123", "worktreeBranch": "feat/vh1-test"}:
        failures.append(f"parse_isolation_footer: expected dict, got {got!r}")
    else:
        print("PASS: parse_isolation_footer — both fields present")

    # Case 2: missing worktreeBranch.
    partial_result = "Some output.\nworktreePath: /tmp/wt/abc123\n"
    got = parse_isolation_footer(partial_result)
    if got is not None:
        failures.append(f"parse_isolation_footer: expected None for missing branch, got {got!r}")
    else:
        print("PASS: parse_isolation_footer — missing worktreeBranch returns None")

    # Case 3: missing worktreePath.
    partial_result2 = "Some output.\nworktreeBranch: feat/test\n"
    got = parse_isolation_footer(partial_result2)
    if got is not None:
        failures.append(f"parse_isolation_footer: expected None for missing path, got {got!r}")
    else:
        print("PASS: parse_isolation_footer — missing worktreePath returns None")

    # Case 4: completely footerless.
    footerless = "Agent ran in-place and produced no footer.\nDone.\n"
    got = parse_isolation_footer(footerless)
    if got is not None:
        failures.append(f"parse_isolation_footer: expected None for footerless, got {got!r}")
    else:
        print("PASS: parse_isolation_footer — footerless result returns None")

    # Case 5: fields embedded mid-text (should still be found).
    mid_result = "worktreePath: /some/path\nOther content.\nworktreeBranch: my-branch\nMore text.\n"
    got = parse_isolation_footer(mid_result)
    if got != {"worktreePath": "/some/path", "worktreeBranch": "my-branch"}:
        failures.append(f"parse_isolation_footer: expected dict for mid-text fields, got {got!r}")
    else:
        print("PASS: parse_isolation_footer — fields found mid-text")

    # --- check_head_on_staging (offline stub) ---
    # We cannot call git in --self-test mode, so we exercise the return-type contract
    # by checking that the function returns a bool in the git-unavailable path.
    # We monkeypatch subprocess.run to simulate "not a git repo".
    import subprocess as _subprocess

    original_run = _subprocess.run

    def _fake_run_error(cmd, **kwargs):  # noqa: ANN001
        class FakeResult:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a git repository"
        return FakeResult()

    _subprocess.run = _fake_run_error  # type: ignore[assignment]
    result_bool = check_head_on_staging("version/3.34")
    _subprocess.run = original_run
    if result_bool is not False:
        failures.append(f"check_head_on_staging: expected False on git error, got {result_bool!r}")
    else:
        print("PASS: check_head_on_staging — returns False when git unavailable")

    # Simulate HEAD on correct branch.
    def _fake_run_ok(cmd, **kwargs):  # noqa: ANN001
        class FakeResult:
            returncode = 0
            stdout = "version/3.34\n"
            stderr = ""
        return FakeResult()

    _subprocess.run = _fake_run_ok  # type: ignore[assignment]
    result_bool = check_head_on_staging("version/3.34")
    _subprocess.run = original_run
    if result_bool is not True:
        failures.append(f"check_head_on_staging: expected True on matching branch, got {result_bool!r}")
    else:
        print("PASS: check_head_on_staging — returns True when HEAD matches")

    # Simulate HEAD on wrong branch.
    def _fake_run_wrong(cmd, **kwargs):  # noqa: ANN001
        class FakeResult:
            returncode = 0
            stdout = "some-feature-branch\n"
            stderr = ""
        return FakeResult()

    _subprocess.run = _fake_run_wrong  # type: ignore[assignment]
    result_bool = check_head_on_staging("version/3.34")
    _subprocess.run = original_run
    if result_bool is not False:
        failures.append(f"check_head_on_staging: expected False on wrong branch, got {result_bool!r}")
    else:
        print("PASS: check_head_on_staging — returns False when HEAD is on wrong branch")

    # --- assert_branch_advanced (offline stub) ---

    # Simulate count > 0.
    def _fake_rev_list_advanced(cmd, **kwargs):  # noqa: ANN001
        class FakeResult:
            returncode = 0
            stdout = "3\n"
            stderr = ""
        return FakeResult()

    _subprocess.run = _fake_rev_list_advanced  # type: ignore[assignment]
    result_bool = assert_branch_advanced("version/3.34", "feat/test")
    _subprocess.run = original_run
    if result_bool is not True:
        failures.append(f"assert_branch_advanced: expected True for count>0, got {result_bool!r}")
    else:
        print("PASS: assert_branch_advanced — returns True when branch is ahead")

    # Simulate count == 0 (branch not advanced).
    def _fake_rev_list_zero(cmd, **kwargs):  # noqa: ANN001
        class FakeResult:
            returncode = 0
            stdout = "0\n"
            stderr = ""
        return FakeResult()

    _subprocess.run = _fake_rev_list_zero  # type: ignore[assignment]
    result_bool = assert_branch_advanced("version/3.34", "feat/test")
    _subprocess.run = original_run
    if result_bool is not False:
        failures.append(f"assert_branch_advanced: expected False for count==0, got {result_bool!r}")
    else:
        print("PASS: assert_branch_advanced — returns False when branch is not ahead")

    # Simulate git error (unknown branch).
    def _fake_rev_list_err(cmd, **kwargs):  # noqa: ANN001
        class FakeResult:
            returncode = 128
            stdout = ""
            stderr = "fatal: unknown revision"
        return FakeResult()

    _subprocess.run = _fake_rev_list_err  # type: ignore[assignment]
    result_bool = assert_branch_advanced("version/3.34", "no-such-branch")
    _subprocess.run = original_run
    if result_bool is not False:
        failures.append(f"assert_branch_advanced: expected False on git error, got {result_bool!r}")
    else:
        print("PASS: assert_branch_advanced — returns False on git error")

    # Summary.
    if failures:
        print(f"\nFAIL: {len(failures)} test(s) failed:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\nAll self-tests PASSED.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(_run_cli(sys.argv[1:]))
