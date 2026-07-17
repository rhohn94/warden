#!/usr/bin/env python3
"""worktree_reap.py — one canonical reaper engine for worktrees + branches (#449).

Disk hit 100% twice in one release week because dead worktrees and their
branches piled up with nobody applying a consistent safety check before
removal. The manual 2026-07-12 cleanup proved the predicate below by hand;
this script mechanizes it into a single reusable engine so every disk-hygiene
work item in a release (#445, #450, #451, #452, #446, #326) calls the same
logic instead of re-deriving it.

The #444 safety predicate — a worktree/branch is safe to reap only if BOTH:
  1. `git rev-list <ref> --not --remotes` is empty: every commit on `<ref>` is
     reachable from SOME remote-tracking ref. Deliberately NOT `<ref>@{u}`,
     which is unreliable/absent when a branch has no configured upstream.
  2. `git merge-base --is-ancestor <ref> <landed-ref>` succeeds: `<ref>`'s tip
     is already an ancestor of the ref it was supposed to land in (e.g.
     `version/3.95`, `dev`, `main` — passed by the caller, never hardcoded).

Both the CLI and the plain-Python predicate are exposed. Sibling work-item
scripts import the predicate directly rather than shelling out:

    sys.path.insert(0, os.path.join(REPO_ROOT, ".claude", "skills", "grm-worktree-preflight"))
    from worktree_reap import is_safe_to_reap

CLI usage:
  worktree_reap.py --worktree PATH [--worktree PATH ...]
                    --branch NAME [--branch NAME ...]
                    --landed-ref REF [--dry-run] [--self-test]

At least one of --worktree / --branch is required (unless --self-test).
--landed-ref is required (unless --self-test) — this script never assumes
what "landed" means; the caller always names the ref.

**Destructive by default.** Without --dry-run, every requested target that
passes the safety predicate is REMOVED immediately: `git worktree remove`
(never `--force`), `git worktree prune`, then `git branch -d` (never `-D`).
Pass --dry-run first to preview. A target that fails the predicate is never
force-removed — it is skipped and the reason is reported.

Exit 0: every requested target was reaped (or reap was unnecessary — nothing
to do). Exit 1: at least one requested target failed the safety predicate, a
worktree could not be resolved to a branch, or a removal command itself
failed (git refused the delete). Never partially forces past a failure.

No issue writes. Git reads always; git writes only in the real removal path,
gated by the predicate above.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


class GitCommandError(RuntimeError):
    """A git command needed by the reaper failed unexpectedly."""


def _run_git(args: list, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run `git <args>`, raising GitCommandError on a non-zero exit."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitCommandError(
            "git %s failed (exit %d): %s"
            % (" ".join(args), result.returncode, result.stderr.strip())
        )
    return result


def _unreached_commits(ref: str, cwd: str | None = None) -> list:
    """Commit SHAs on `ref` NOT reachable from any remote-tracking ref."""
    result = _run_git(["rev-list", ref, "--not", "--remotes"], cwd=cwd)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _is_ancestor(ref: str, landed_ref: str, cwd: str | None = None) -> bool:
    """True iff `ref`'s tip is an ancestor of `landed_ref` (already landed)."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ref, landed_ref],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise GitCommandError(
            "git merge-base --is-ancestor %s %s failed unexpectedly (exit %d): %s"
            % (ref, landed_ref, result.returncode, result.stderr.strip())
        )
    return result.returncode == 0


def is_safe_to_reap(ref: str, landed_ref: str, cwd: str | None = None) -> bool:
    """The #444 predicate. True only if `ref` is fully remote-reachable AND
    already an ancestor of `landed_ref`. See module docstring for rationale."""
    if _unreached_commits(ref, cwd=cwd):
        return False
    return _is_ancestor(ref, landed_ref, cwd=cwd)


def _unsafe_reason(ref: str, landed_ref: str, cwd: str | None = None) -> str:
    """Human-readable reason `ref` failed the predicate (for reporting)."""
    unreached = _unreached_commits(ref, cwd=cwd)
    if unreached:
        return "%d commit(s) not reachable from any remote-tracking ref" % len(unreached)
    return "tip is not yet an ancestor of '%s' (not landed there)" % landed_ref


def _worktree_branch_map(cwd: str | None = None) -> dict:
    """Map each worktree path (as git reports it) to its checked-out branch's
    short name, from `git worktree list --porcelain`. A detached worktree
    maps to None."""
    result = _run_git(["worktree", "list", "--porcelain"], cwd=cwd)
    mapping = {}
    path = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            if path is not None:
                mapping[path] = branch
        elif line == "detached":
            if path is not None:
                mapping[path] = None
    return mapping


def _resolve_worktree_targets(worktrees: list, cwd: str | None = None):
    """Resolve each requested worktree path to its checked-out branch.
    Returns (targets, unresolved) — unresolved holds (path, reason) pairs
    for paths git doesn't know about or that are detached (no branch to
    check against the predicate)."""
    wt_map = _worktree_branch_map(cwd=cwd) if worktrees else {}
    real_map = {os.path.realpath(p): (p, b) for p, b in wt_map.items()}
    targets = []
    unresolved = []
    for wt in worktrees:
        hit = real_map.get(os.path.realpath(wt))
        if hit is None:
            unresolved.append((wt, "not a known worktree of this repository"))
            continue
        _, branch = hit
        if branch is None:
            unresolved.append((wt, "worktree HEAD is detached (no branch to check)"))
            continue
        targets.append({"kind": "worktree", "path": wt, "ref": branch})
    return targets, unresolved


def reap(worktrees: list, branches: list, landed_ref: str, dry_run: bool,
         cwd: str | None = None):
    """Apply the #444 predicate to every requested target and, unless
    `dry_run`, remove the ones that pass. Returns (report_lines, any_failed)
    — `any_failed` is True iff at least one target failed the predicate,
    could not be resolved, or a removal command itself failed. Never forces
    past a failed predicate."""
    report = []
    any_failed = False

    worktree_targets, unresolved = _resolve_worktree_targets(worktrees, cwd=cwd)
    for path, reason in unresolved:
        report.append("SKIP  worktree %s — %s" % (path, reason))
        any_failed = True

    branch_targets = [{"kind": "branch", "path": None, "ref": name} for name in branches]
    targets = worktree_targets + branch_targets

    removed_a_worktree = False
    for target in targets:
        ref = target["ref"]
        label = target["path"] or ref
        try:
            safe = is_safe_to_reap(ref, landed_ref, cwd=cwd)
        except GitCommandError as exc:
            report.append("SKIP  %s '%s' — predicate check failed: %s"
                           % (target["kind"], label, exc))
            any_failed = True
            continue

        if not safe:
            reason = _unsafe_reason(ref, landed_ref, cwd=cwd)
            report.append("SKIP  %s '%s' (branch '%s') — unsafe to reap: %s"
                           % (target["kind"], label, ref, reason))
            any_failed = True
            continue

        if dry_run:
            report.append("DRY-RUN would remove %s '%s' (branch '%s')"
                           % (target["kind"], label, ref))
            continue

        try:
            if target["kind"] == "worktree":
                _run_git(["worktree", "remove", target["path"]], cwd=cwd)
                removed_a_worktree = True
            _run_git(["branch", "-d", ref], cwd=cwd)
        except GitCommandError as exc:
            report.append("FAILED %s '%s' — removal command failed (never forced): %s"
                           % (target["kind"], label, exc))
            any_failed = True
            continue

        report.append("REMOVED %s '%s' (branch '%s' deleted)"
                       % (target["kind"], label, ref))

    if removed_a_worktree:
        _run_git(["worktree", "prune"], cwd=cwd)
        report.append("worktree prune: done")

    if not targets and not unresolved:
        report.append("nothing to reap (no targets requested)")

    return report, any_failed


# --------------------------------------------------------------------------
# Self-test — builds hermetic fixture repos under a temp dir; never touches
# this (or any real) repository's actual worktrees or branches.
# --------------------------------------------------------------------------

def _git_ok(args: list, cwd: str) -> None:
    _run_git(args, cwd=cwd)


def _configure_identity(repo: str) -> None:
    _git_ok(["config", "user.email", "reap-selftest@example.com"], repo)
    _git_ok(["config", "user.name", "Reap Selftest"], repo)


def _commit_file(repo: str, name: str, content: str, message: str) -> None:
    with open(os.path.join(repo, name), "a", encoding="utf-8") as fh:
        fh.write(content)
    _git_ok(["add", name], repo)
    _git_ok(["commit", "-m", message], repo)


def _make_repo_with_origin(base_dir: str) -> str:
    """Bare 'origin' + a clone with one commit on `main`, remote wired up.
    Returns the clone's path — every fixture branch is grown from there."""
    origin = os.path.join(base_dir, "origin.git")
    _git_ok(["init", "--bare", "-b", "main", origin], base_dir)
    clone = os.path.join(base_dir, "clone")
    _git_ok(["clone", origin, clone], base_dir)
    _configure_identity(clone)
    _commit_file(clone, "README.md", "hello\n", "initial commit")
    _git_ok(["push", "origin", "main"], clone)
    return clone


def _self_test() -> int:
    import tempfile

    failures = []

    with tempfile.TemporaryDirectory() as base:
        clone = _make_repo_with_origin(base)

        # --- Case 1: safe to reap (pushed AND merged into landed-ref) -----
        _git_ok(["switch", "-c", "feature-safe"], clone)
        _commit_file(clone, "safe.txt", "x\n", "feature work")
        _git_ok(["push", "origin", "feature-safe"], clone)
        _git_ok(["switch", "main"], clone)
        _git_ok(["merge", "--no-ff", "feature-safe"], clone)
        if is_safe_to_reap("feature-safe", "main", cwd=clone) is not True:
            failures.append("safe-to-reap case should return True")

        # --- Case 2: unmerged commits (nothing pushed anywhere) -----------
        _git_ok(["switch", "-c", "feature-unpushed"], clone)
        _commit_file(clone, "unpushed.txt", "y\n", "local-only work")
        if is_safe_to_reap("feature-unpushed", "main", cwd=clone) is not False:
            failures.append("unmerged-commits case should return False")
        # refuse-to-remove check: reap() must skip it, never delete
        report, failed = reap([], ["feature-unpushed"], "main", dry_run=False, cwd=clone)
        if not failed:
            failures.append("reap() should report failure for unmerged-commits branch")
        branches_after = _run_git(["branch", "--list", "feature-unpushed"], cwd=clone).stdout
        if "feature-unpushed" not in branches_after:
            failures.append("reap() deleted an unsafe (unmerged-commits) branch")

        # --- Case 3: pushed but NOT yet landed (not an ancestor) ----------
        _git_ok(["switch", "main"], clone)
        _git_ok(["switch", "-c", "feature-not-landed"], clone)
        _commit_file(clone, "notlanded.txt", "z\n", "pushed, not merged")
        _git_ok(["push", "origin", "feature-not-landed"], clone)
        _git_ok(["switch", "main"], clone)
        if is_safe_to_reap("feature-not-landed", "main", cwd=clone) is not False:
            failures.append("pushed-but-not-landed case should return False")
        report, failed = reap([], ["feature-not-landed"], "main", dry_run=False, cwd=clone)
        if not failed:
            failures.append("reap() should report failure for pushed-but-not-landed branch")
        branches_after = _run_git(["branch", "--list", "feature-not-landed"], cwd=clone).stdout
        if "feature-not-landed" not in branches_after:
            failures.append("reap() deleted an unsafe (not-landed) branch")

        # --- Case 4: --dry-run never deletes, even when predicate is True -
        _git_ok(["switch", "main"], clone)
        _git_ok(["switch", "-c", "feature-safe2"], clone)
        _commit_file(clone, "safe2.txt", "x2\n", "more feature work")
        _git_ok(["push", "origin", "feature-safe2"], clone)
        _git_ok(["switch", "main"], clone)
        _git_ok(["merge", "--no-ff", "feature-safe2"], clone)
        wt_path = os.path.join(base, "wt-safe2")
        _git_ok(["worktree", "add", wt_path, "feature-safe2"], clone)

        report, failed = reap([wt_path], [], "main", dry_run=True, cwd=clone)
        if failed:
            failures.append("dry-run over a safe target should not report failure")
        if not any("DRY-RUN" in line for line in report):
            failures.append("dry-run should report a DRY-RUN line: %r" % report)
        if not os.path.isdir(wt_path):
            failures.append("--dry-run removed the worktree directory")
        branches_after = _run_git(["branch", "--list", "feature-safe2"], cwd=clone).stdout
        if "feature-safe2" not in branches_after:
            failures.append("--dry-run deleted the branch")

        # --- Case 5: real removal path, once dry-run is lifted ------------
        report, failed = reap([wt_path], [], "main", dry_run=False, cwd=clone)
        if failed:
            failures.append("real removal of a safe target should not report failure: %r" % report)
        if os.path.isdir(wt_path):
            failures.append("real removal (no --dry-run) left the worktree directory in place")
        branches_after = _run_git(["branch", "--list", "feature-safe2"], cwd=clone).stdout
        if "feature-safe2" in branches_after:
            failures.append("real removal (no --dry-run) left the branch in place")
        if not any(line.startswith("REMOVED") for line in report):
            failures.append("real removal should report a REMOVED line: %r" % report)
        if not any("worktree prune" in line for line in report):
            failures.append("real removal of a worktree should report a prune step: %r" % report)

        # --- Case 6: unresolved worktree path is reported, not crashed ----
        report, failed = reap([os.path.join(base, "does-not-exist")], [], "main",
                               dry_run=True, cwd=clone)
        if not failed:
            failures.append("an unresolvable worktree path should report failure")
        if not any("SKIP" in line for line in report):
            failures.append("an unresolvable worktree path should be reported as SKIP: %r" % report)

        # --- Case 7: nothing-to-reap is success, not failure --------------
        report, failed = reap([], [], "main", dry_run=True, cwd=clone)
        if failed:
            failures.append("no requested targets should not be a failure")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("worktree_reap self-test: OK (safe-to-reap, unmerged-commits refuse, "
          "pushed-but-not-landed refuse, dry-run never deletes, real removal "
          "path, unresolved-worktree reporting, nothing-to-reap success)")
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Reap (remove) worktrees/branches that pass the #444 safety predicate: "
            "every commit reachable from some remote-tracking ref, AND the ref's tip "
            "already an ancestor of --landed-ref. DESTRUCTIVE BY DEFAULT: without "
            "--dry-run, every target that passes the predicate is removed immediately "
            "via 'git worktree remove' + 'git worktree prune' + 'git branch -d' (never "
            "--force, never -D). Pass --dry-run to preview first."
        )
    )
    ap.add_argument("--worktree", action="append", default=[], metavar="PATH",
                     help="Worktree path to reap (repeatable). Its checked-out branch "
                          "is what the safety predicate is evaluated against.")
    ap.add_argument("--branch", action="append", default=[], metavar="NAME",
                     help="Branch name to reap directly, no worktree involved (repeatable).")
    ap.add_argument("--landed-ref", default=None,
                     help="What 'landed' means for this call, e.g. version/3.95, dev, "
                          "or main. Required unless --self-test.")
    ap.add_argument("--dry-run", action="store_true",
                     help="Preview only: report what would be removed and why targets "
                          "are skipped, but remove nothing. Without this flag real "
                          "removal proceeds for every target that passes the predicate.")
    ap.add_argument("--self-test", action="store_true",
                     help="Run the hermetic self-test suite (builds temp fixture repos; "
                          "touches nothing in the real repository) and exit.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.worktree and not args.branch:
        print("error: at least one of --worktree / --branch is required", file=sys.stderr)
        return 1
    if not args.landed_ref:
        print("error: --landed-ref is required", file=sys.stderr)
        return 1

    try:
        report, any_failed = reap(args.worktree, args.branch, args.landed_ref, args.dry_run)
    except GitCommandError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    for line in report:
        print(line)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
