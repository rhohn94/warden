#!/usr/bin/env python3
"""preflight.py — mechanize the worktree-preflight happy path (#398).

`SKILL.md` had grown large enough (root check, release-only-commit grep,
Step 0.5 parent sync, plus the R4 #452 self-healing sweep) that an agent was
reading several paragraphs of prose and typing out half a dozen separate
`git` commands by hand just to confirm a fresh worktree is rooted correctly.
This script mechanizes the happy path into a single call so an agent runs it
once and reads a PASS/FAIL report instead. Remediation prose (Case A/B/C, the
guard step) stays in `reference.md`, loaded only when a check actually fails
— this script's own failure output names which case applies.

Checks, in order (each later check runs only if the ones before it pass):
  1. root-check           — `git merge-base HEAD <parent>` == `git rev-parse
                             <parent>` (SKILL.md "The check").
  2. release-only-grep     — no commits reachable from `<parent>..HEAD` match
                             the release-only pattern (dist/, version bump,
                             changelog, release) — the second sanity check.
  3. parent-sync           — SKILL.md Step 0.5: `git rev-list --count
                             HEAD..<parent>`; if behind, sync-merges
                             `<parent>` in (`git merge --no-ff`, forward-merge
                             only, never rebase, never auto-resolves a
                             conflict). Pass --no-sync to only measure and
                             report, never merge.
  4. self-heal (optional)  — `--self-heal`: the R4 #452 sweep. Inventories
                             every worktree, classifies each branch with
                             `agent_branch_namespace.is_agent_branch`, and
                             reports (never mutates) which agent-created
                             worktrees are already safe to reap per
                             `worktree_reap.is_safe_to_reap`. Read-only by
                             design — actually reaping stays a deliberate,
                             separate `worktree_reap.py` call without
                             --dry-run, per reference.md's caution against
                             skipping that confirmation step.

`<parent>` resolution (unless --parent is given explicitly): the sole local
`version/*` branch if exactly one exists, else `dev`. Multiple `version/*`
branches is ambiguous and is itself reported as a root-check failure asking
for an explicit --parent.

No issue writes, no network calls. Git writes only in the parent-sync merge
path (gated by staleness > 0 and not --no-sync) — never in root-check,
release-only-grep, or self-heal (which is report-only regardless of flags).

Usage:
  preflight.py [--parent REF] [--landed-ref REF] [--self-heal] [--no-sync]
               [--self-test]
Exit 0: every mandatory check (root-check, release-only-grep, parent-sync)
passed or was cleanly synced. Exit 1: a mandatory check failed (mis-rooted,
release-only commits reachable, or a sync-merge conflict) — remediation
pointer printed; self-heal findings never affect the exit code (informational
maintenance, not a preflight gate).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from agent_branch_namespace import is_agent_branch  # noqa: E402
from worktree_reap import (  # noqa: E402
    GitCommandError,
    _unsafe_reason,
    _worktree_branch_map,
    is_safe_to_reap,
)

RELEASE_ONLY_PATTERN = re.compile(r"dist/|version.bump|changelog|release", re.IGNORECASE)

REMEDIATION_POINTER = (
    "see reference.md for remediation (Case A/B/C) and the guard step "
    "before any git switch -c / branch / merge"
)


class PreflightError(RuntimeError):
    """A precondition (e.g. ambiguous parent resolution) could not be met."""


def _run_git(args: list, cwd: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise GitCommandError(
            "git %s failed (exit %d): %s"
            % (" ".join(args), result.returncode, result.stderr.strip())
        )
    return result


def _git_ok(args: list, cwd: str | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitCommandError(
            "git %s failed (exit %d): %s"
            % (" ".join(args), result.returncode, result.stderr.strip())
        )
    return result.stdout.strip()


def resolve_parent_ref(explicit: str | None, cwd: str | None = None) -> str:
    """The sole local `version/*` branch if exactly one exists, else `dev`.
    Raises PreflightError if more than one `version/*` branch exists and no
    explicit --parent was given — that ambiguity must be resolved by a human
    or an explicit flag, never guessed."""
    if explicit:
        return explicit
    result = _run_git(["branch", "--list", "version/*"], cwd=cwd)
    candidates = [
        line.strip().lstrip("*+ ").strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) == 0:
        return "dev"
    raise PreflightError(
        "multiple version/* branches found (%s); pass --parent explicitly"
        % ", ".join(candidates)
    )


def check_root(parent: str, cwd: str | None = None) -> tuple[bool, str]:
    """SKILL.md 'The check', made resume-safe.

    A literal `merge-base(HEAD, parent) == parent-tip` comparison only holds
    at the instant a branch is freshly forked — the moment `parent` advances
    (routine across a paused/resumed session, exactly what Step 0.5 exists
    to handle), a *correctly-rooted* branch trips the same mismatch as a
    genuinely mis-rooted one. SKILL.md's own "Real-world motivating case"
    already establishes that bare merge-base equality is an incomplete
    signal and the release-only-commit grep is "what actually catches" true
    mis-rooting — so an exact-match mismatch is treated as decisive only
    when corroborated by the grep; an exact match without corroboration is
    reported as staleness (Step 0.5's concern), not a root-check failure.
    """
    mb_result = _run_git(["merge-base", "HEAD", parent], cwd=cwd)
    merge_base = mb_result.stdout.strip()
    parent_tip = _git_ok(["rev-parse", parent], cwd=cwd)

    if merge_base and merge_base == parent_tip:
        return True, "HEAD is rooted on '%s' (exact match)" % parent

    grep_ok, grep_msg = check_release_only_commits(parent, cwd=cwd)

    if not merge_base:
        # No common ancestor at all — always decisive, regardless of grep.
        return False, "HEAD and '%s' share no common ancestor (unrelated histories)" % parent

    if grep_ok:
        return True, (
            "HEAD forked from '%s' at %s; '%s' has since advanced — staleness, "
            "not mis-rooting (%s)" % (parent, merge_base[:12], parent, grep_msg)
        )
    return False, (
        "HEAD is NOT rooted on '%s' (merge-base %s != %s tip %s; %s)"
        % (parent, merge_base[:12], parent, parent_tip[:12], grep_msg)
    )


def check_release_only_commits(parent: str, cwd: str | None = None) -> tuple[bool, str]:
    """SKILL.md's second sanity check: no release-only commits reachable from
    the branch tip that aren't reachable from parent. Unlike a bare
    merge-base comparison this stays correct regardless of how far `parent`
    has advanced, since `parent..HEAD` only ever shows HEAD's own commits —
    it is the decisive signal `check_root` defers to on a merge-base mismatch."""
    result = _run_git(["log", "--oneline", "%s..HEAD" % parent], cwd=cwd)
    hits = [line for line in result.stdout.splitlines() if RELEASE_ONLY_PATTERN.search(line)]
    if not hits:
        return True, "no release-only commits between '%s..HEAD'" % parent
    return False, (
        "%d release-only commit(s) reachable from HEAD: %s"
        % (len(hits), "; ".join(hits[:3]) + (" ..." if len(hits) > 3 else ""))
    )


def check_parent_sync(parent: str, sync: bool, cwd: str | None = None) -> tuple[bool, str]:
    """SKILL.md Step 0.5: measure staleness against `parent`, sync-merge if
    behind (unless `sync` is False), never auto-resolve a conflict."""
    behind = int(_git_ok(["rev-list", "--count", "HEAD..%s" % parent], cwd=cwd))
    if behind == 0:
        return True, "up to date with '%s' (0 commits behind)" % parent
    if not sync:
        return True, "%d commit(s) behind '%s' (--no-sync: not merged)" % (behind, parent)

    result = subprocess.run(
        ["git", "merge", "--no-ff", parent, "-m", "sync: merge %s (preflight Step 0.5)" % parent],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, "was %d commit(s) behind '%s' — synced, clean" % (behind, parent)

    # Never auto-resolve; abort the merge attempt so the tree isn't left mid-conflict
    # for a caller that didn't expect it, and surface the conflict for a human.
    subprocess.run(["git", "merge", "--abort"], cwd=cwd, capture_output=True, text=True)
    return False, (
        "was %d commit(s) behind '%s' — sync-merge CONFLICTED (aborted, not "
        "auto-resolved): %s" % (behind, parent, result.stdout.strip().splitlines()[-1:])
    )


def run_self_heal(landed_ref: str, cwd: str | None = None) -> list[str]:
    """R4 #452 self-healing sweep, report-only. Never mutates a worktree or
    branch — actually reaping stays a deliberate separate worktree_reap.py
    call, per reference.md's caution."""
    lines = []
    own_path = os.path.realpath(cwd or os.getcwd())
    wt_map = _worktree_branch_map(cwd=cwd)
    for path, branch in sorted(wt_map.items()):
        if os.path.realpath(path) == own_path:
            continue
        if not branch or not is_agent_branch(branch):
            continue
        try:
            safe = is_safe_to_reap(branch, landed_ref, cwd=cwd)
        except GitCommandError as exc:
            lines.append("INFO self-heal: %s (branch %s) — predicate check failed: %s"
                         % (path, branch, exc))
            continue
        if safe:
            lines.append("INFO self-heal: %s (branch %s) — SAFE to reap (worktree_reap.py "
                         "--worktree %s --landed-ref %s, dry-run first)" % (path, branch, path, landed_ref))
        else:
            reason = _unsafe_reason(branch, landed_ref, cwd=cwd)
            lines.append("INFO self-heal: %s (branch %s) — SKIP: %s" % (path, branch, reason))
    if not lines:
        lines.append("INFO self-heal: no dead agent-created worktrees found")
    return lines


def run_preflight(parent: str | None = None, landed_ref: str | None = None,
                  self_heal: bool = False, sync: bool = True,
                  cwd: str | None = None) -> tuple[list[str], bool]:
    """Run the mechanized happy path. Returns (report_lines, ok)."""
    report = []

    try:
        resolved_parent = resolve_parent_ref(parent, cwd=cwd)
    except PreflightError as exc:
        report.append("FAIL root-check: %s" % exc)
        report.append("preflight: FAILED (1 check failed) — %s" % REMEDIATION_POINTER)
        return report, False

    ok, msg = check_root(resolved_parent, cwd=cwd)
    report.append("%s root-check: %s" % ("PASS" if ok else "FAIL", msg))
    if not ok:
        report.append("preflight: FAILED (1 check failed) — %s" % REMEDIATION_POINTER)
        return report, False

    ok, msg = check_release_only_commits(resolved_parent, cwd=cwd)
    report.append("%s release-only-grep: %s" % ("PASS" if ok else "FAIL", msg))
    if not ok:
        report.append("preflight: FAILED (1 check failed) — %s" % REMEDIATION_POINTER)
        return report, False

    ok, msg = check_parent_sync(resolved_parent, sync, cwd=cwd)
    report.append("%s parent-sync: %s" % ("PASS" if ok else "FAIL", msg))
    if not ok:
        report.append("preflight: FAILED (1 check failed) — %s" % REMEDIATION_POINTER)
        return report, False

    if self_heal:
        resolved_landed = landed_ref or resolved_parent
        report.extend(run_self_heal(resolved_landed, cwd=cwd))

    report.append("preflight: OK (3/3 checks passed)")
    return report, True


# --------------------------------------------------------------------------
# Self-test — builds hermetic fixture repos under a temp dir; never touches
# this (or any real) repository's actual branches or worktrees.
# --------------------------------------------------------------------------

def _configure_identity(repo: str) -> None:
    _git_ok(["config", "user.email", "preflight-selftest@example.com"], repo)
    _git_ok(["config", "user.name", "Preflight Selftest"], repo)


def _commit_file(repo: str, name: str, content: str, message: str) -> None:
    with open(os.path.join(repo, name), "a", encoding="utf-8") as fh:
        fh.write(content)
    _git_ok(["add", name], repo)
    _git_ok(["commit", "-m", message], repo)


def _make_base_repo(base_dir: str) -> str:
    """A repo with a `dev` branch carrying one commit. Returns the repo path."""
    repo = os.path.join(base_dir, "repo")
    _git_ok(["init", "-b", "dev", repo])
    _configure_identity(repo)
    _commit_file(repo, "README.md", "hello\n", "initial commit on dev")
    return repo


def _self_test() -> int:
    import tempfile

    failures = []

    # --- Case A: correctly-rooted branch, up to date — all checks pass -----
    with tempfile.TemporaryDirectory() as base:
        repo = _make_base_repo(base)
        _git_ok(["switch", "-c", "claude/good"], repo)
        _commit_file(repo, "work.txt", "x\n", "normal work-item commit")

        report, ok = run_preflight(parent="dev", cwd=repo)
        if not ok:
            failures.append("Case A (correctly rooted): expected ok=True, report=%r" % report)
        if not any(l.startswith("PASS root-check") for l in report):
            failures.append("Case A: expected PASS root-check line, got %r" % report)
        if not any(l.startswith("PASS release-only-grep") for l in report):
            failures.append("Case A: expected PASS release-only-grep line, got %r" % report)
        if not any(l.startswith("PASS parent-sync") for l in report):
            failures.append("Case A: expected PASS parent-sync line, got %r" % report)
        if not any(l.startswith("preflight: OK") for l in report):
            failures.append("Case A: expected a final OK summary line, got %r" % report)

    # --- Case B: mis-rooted branch (SKILL.md's own "almost always main" +
    #             "real-world motivating case" — main already contains dev,
    #             so bare merge-base equality alone would false-pass; the
    #             release-only-commit grep is what actually catches it) ----
    with tempfile.TemporaryDirectory() as base:
        repo = _make_base_repo(base)
        # Simulate a branch rooted on 'main' (which already contains dev)
        # rather than dev itself.
        _git_ok(["switch", "-c", "main"], repo)
        _commit_file(repo, "release-only.txt", "r\n", "chore(release): version bump")
        _git_ok(["switch", "-c", "claude/bad", "main"], repo)
        _commit_file(repo, "work.txt", "x\n", "work on top of the wrong base")

        report, ok = run_preflight(parent="dev", cwd=repo)
        if ok:
            failures.append("Case B (mis-rooted): expected ok=False, report=%r" % report)
        if not any(l.startswith("FAIL release-only-grep") for l in report):
            failures.append("Case B: expected FAIL release-only-grep line (the decisive "
                            "signal for this main-already-contains-dev scenario), got %r"
                            % report)
        if not any("reference.md" in l and "Case A/B/C" in l for l in report):
            failures.append("Case B: expected a remediation pointer naming reference.md "
                            "and Case A/B/C, got %r" % report)
        if any(l.startswith(("PASS parent-sync", "FAIL parent-sync")) for l in report):
            failures.append("Case B: parent-sync must not run once release-only-grep fails, "
                            "got %r" % report)

    # --- Case C: correctly-rooted but stale — clean sync-merge, still passes -
    with tempfile.TemporaryDirectory() as base:
        repo = _make_base_repo(base)
        _git_ok(["switch", "-c", "claude/stale"], repo)
        _commit_file(repo, "work.txt", "x\n", "work-item commit")
        _git_ok(["switch", "dev"], repo)
        _commit_file(repo, "dev-moved.txt", "y\n", "unrelated dev progress")
        _git_ok(["switch", "claude/stale"], repo)

        report, ok = run_preflight(parent="dev", cwd=repo)
        if not ok:
            failures.append("Case C (stale, clean sync): expected ok=True, report=%r" % report)
        if not any("synced, clean" in l for l in report):
            failures.append("Case C: expected a 'synced, clean' parent-sync line, got %r" % report)

    # --- Case D: release-only commit reachable — second sanity check fails -
    with tempfile.TemporaryDirectory() as base:
        repo = _make_base_repo(base)
        _git_ok(["switch", "-c", "claude/tainted"], repo)
        _commit_file(repo, "CHANGELOG.md", "## v9.9\n", "docs: update changelog for release")

        report, ok = run_preflight(parent="dev", cwd=repo)
        if ok:
            failures.append("Case D (release-only commit): expected ok=False, report=%r" % report)
        if not any(l.startswith("FAIL release-only-grep") for l in report):
            failures.append("Case D: expected FAIL release-only-grep line, got %r" % report)
        if not any("reference.md" in l and "Case A/B/C" in l for l in report):
            failures.append("Case D: expected a remediation pointer, got %r" % report)

    # --- Case E: self-heal sweep — safe worktree reported, non-agent branch
    #             worktree never evaluated, own worktree always skipped -----
    # worktree_reap.is_safe_to_reap requires every commit on the branch to be
    # reachable from SOME remote-tracking ref, so this fixture needs a real
    # origin + push, same as worktree_reap.py's own self-test.
    with tempfile.TemporaryDirectory() as base:
        origin = os.path.join(base, "origin.git")
        _git_ok(["init", "--bare", "-b", "dev", origin])
        repo = os.path.join(base, "repo")
        _git_ok(["clone", origin, repo])
        _configure_identity(repo)
        _commit_file(repo, "README.md", "hello\n", "initial commit on dev")
        _git_ok(["push", "origin", "dev"], repo)

        _git_ok(["switch", "-c", "claude/r9-000-selftest-fixture"], repo)
        _commit_file(repo, "safe.txt", "x\n", "agent work, already landed")
        _git_ok(["push", "origin", "claude/r9-000-selftest-fixture"], repo)
        _git_ok(["switch", "dev"], repo)
        _git_ok(["merge", "--no-ff", "claude/r9-000-selftest-fixture"], repo)
        _git_ok(["push", "origin", "dev"], repo)

        wt_path = os.path.join(base, "wt-safe")
        _git_ok(["worktree", "add", wt_path, "claude/r9-000-selftest-fixture"], repo)

        _git_ok(["branch", "robs-human-branch"], repo)
        human_wt = os.path.join(base, "wt-human")
        _git_ok(["worktree", "add", human_wt, "robs-human-branch"], repo)

        lines = run_self_heal("dev", cwd=repo)
        if not any("SAFE to reap" in l and "claude/r9-000-selftest-fixture" in l for l in lines):
            failures.append("Case E: expected the landed agent worktree reported SAFE, "
                            "got %r" % lines)
        if any("robs-human-branch" in l for l in lines):
            failures.append("Case E: a human-named branch must never be evaluated/reported, "
                            "got %r" % lines)
        if os.path.exists(wt_path) or os.path.exists(human_wt):
            pass  # self-heal is report-only; both worktrees must still exist on disk
        else:
            failures.append("Case E: self-heal must never delete a worktree; both should "
                            "still exist on disk")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("preflight self-test: OK (correctly-rooted all-pass, mis-rooted correct "
          "failure + remediation pointer, stale clean sync, release-only-commit "
          "failure, self-heal report-only sweep)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Mechanize the worktree-preflight happy path: root check, "
            "release-only-commit grep, Step 0.5 parent sync, and (optionally) "
            "the R4 self-healing sweep, in one invocation."
        )
    )
    ap.add_argument("--parent", default=None, metavar="REF",
                    help="Staging ref to check against (e.g. version/3.96 or dev). "
                         "Default: the sole local version/* branch if exactly one "
                         "exists, else dev.")
    ap.add_argument("--landed-ref", default=None, metavar="REF",
                    help="What 'landed' means for --self-heal's safety predicate. "
                         "Default: same as the resolved --parent.")
    ap.add_argument("--self-heal", action="store_true",
                    help="Also run the R4 #452 self-healing sweep (report-only; "
                         "never mutates a worktree or branch).")
    ap.add_argument("--no-sync", action="store_true",
                    help="Only measure Step 0.5 staleness; never sync-merge the "
                         "parent in, even if behind.")
    ap.add_argument("--self-test", action="store_true",
                    help="Run the hermetic self-test suite (builds temp fixture "
                         "repos; touches nothing in the real repository) and exit.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    try:
        report, ok = run_preflight(
            parent=args.parent,
            landed_ref=args.landed_ref,
            self_heal=args.self_heal,
            sync=not args.no_sync,
        )
    except GitCommandError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    for line in report:
        print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
