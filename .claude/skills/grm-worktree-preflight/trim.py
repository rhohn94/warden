#!/usr/bin/env python3
"""trim.py — human/master-invoked convenience wrapper around worktree_reap.py (#446).

`just trim` (every quick-start justfile template, and the framework's own
root justfile) calls this. #449's `worktree_reap.py` is deliberately
DESTRUCTIVE BY DEFAULT (its own --dry-run is opt-in) because every #449 call
site already wraps it in its own confirmation/reporting layer (see
docs/grimoire/design/disk-branch-hygiene-design.md §Scope). `trim` is
different: it is invoked directly by a human or the integration master with
no surrounding gate, so THIS wrapper inverts the posture — dry-run
(preview-only) unless the caller explicitly passes --confirm.

Auto-discovers reap targets rather than requiring the caller to enumerate
worktree paths by hand: walks `git worktree list --porcelain`, classifies
each worktree's checked-out branch with `agent_branch_namespace.is_agent_branch`
(#456 — a name-only check; protected refs like main/dev/version/*/home are
never agent branches by construction) and excludes the worktree the command
is currently running from (a `trim` invocation must never remove its own
cwd). Only agent-branch worktrees are ever considered — a human's own
feature branch/worktree is never touched, matching the #444 predicate's
"never auto-touch anything that isn't provably agent-created and landed"
posture that `is_agent_branch` was built for.

The actual delete-or-not decision still goes through the full #444 safety
predicate inside `worktree_reap.reap()` (remote-reachable AND landed) — this
script only adds the discovery + default-dry-run gate on top; it never
second-guesses or loosens that predicate.

Usage:
    trim.py [--landed-ref REF] [--confirm] [--self-test]

  --landed-ref REF   What "landed" means for the #444 predicate
                      (default: dev). Pass e.g. --landed-ref version/3.95
                      during an in-flight release.
  --confirm           Actually remove the targets that pass the predicate.
                      Default is dry-run: preview only, nothing is deleted.
  --self-test         Run this script's own discovery-logic self-test
                      (hermetic fixture repos; the predicate/removal path
                      itself is already covered by worktree_reap.py's own
                      --self-test, not re-tested here).

Exit code is always 0 for a normal run (informational skips — e.g. a
worktree that hasn't landed yet — are reported, not treated as a `trim`
failure; this is a convenience sweep, not a CI gate). Only an internal error
(bad arguments, a git command failing unexpectedly) exits non-zero.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from worktree_reap import _worktree_branch_map, reap  # noqa: E402
from agent_branch_namespace import is_agent_branch  # noqa: E402


def discover_targets(cwd: str | None = None) -> list:
    """Return worktree paths eligible for reap *consideration*: their
    checked-out branch matches the agent-branch namespace (#456), and the
    worktree is not the one this command is currently running from. This is
    discovery only — whether a discovered target is actually safe to delete
    is still decided by the #444 predicate inside `reap()`."""
    here = os.path.realpath(cwd or os.getcwd())
    wt_map = _worktree_branch_map(cwd=cwd)
    targets = []
    for path, branch in sorted(wt_map.items()):
        if branch is None:
            continue  # detached HEAD — nothing to classify
        if os.path.realpath(path) == here:
            continue  # never reap the worktree we're running from
        if is_agent_branch(branch):
            targets.append(path)
    return targets


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Human-triggered convenience sweep of dead agent-branch worktrees. "
            "DRY-RUN BY DEFAULT (inverts worktree_reap.py's own destructive-by-"
            "default posture — see module docstring). Pass --confirm to delete."
        )
    )
    ap.add_argument("--landed-ref", default="dev",
                     help="What 'landed' means for the #444 predicate (default: dev).")
    ap.add_argument("--confirm", action="store_true",
                     help="Actually remove. Default is dry-run preview only.")
    ap.add_argument("--dry-run", action="store_true",
                     help="Explicit spelling of the default (preview only, delete "
                          "nothing). Accepted for parity with worktree_reap.py's own "
                          "flag and the documented `just trim --dry-run` invocation; "
                          "always wins over --confirm if both are given.")
    ap.add_argument("--self-test", action="store_true",
                     help="Run this script's discovery-logic self-test and exit.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    # --dry-run always wins: an explicit preview request must never be
    # overridden by a --confirm also present on the command line.
    effective_confirm = args.confirm and not args.dry_run

    targets = discover_targets()
    if not targets:
        print("trim: no agent-branch worktrees found to consider (nothing to reap).")
        return 0

    report, _any_failed = reap(targets, [], args.landed_ref, dry_run=not effective_confirm)
    for line in report:
        print(line)

    if not effective_confirm:
        print("trim: DRY-RUN — pass --confirm to actually remove the targets above.")

    return 0


# --------------------------------------------------------------------------
# Self-test — hermetic fixture repo; covers discover_targets() only (the
# predicate/removal path is worktree_reap.py's own --self-test).
# --------------------------------------------------------------------------

def _self_test() -> int:
    import subprocess
    import tempfile

    def git(args, cwd):
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr))
        return r

    failures = []

    with tempfile.TemporaryDirectory() as base:
        repo = os.path.join(base, "repo")
        git(["init", "-b", "main", repo], base)
        git(["config", "user.email", "trim-selftest@example.com"], repo)
        git(["config", "user.name", "Trim Selftest"], repo)
        with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("hello\n")
        git(["add", "README.md"], repo)
        git(["commit", "-m", "initial commit"], repo)

        # An agent-branch worktree — should be discovered.
        git(["branch", "claude/r4-000-fixture"], repo)
        agent_wt = os.path.join(base, "wt-agent")
        git(["worktree", "add", agent_wt, "claude/r4-000-fixture"], repo)

        # A human-branch worktree — must NOT be discovered.
        git(["branch", "robs-feature"], repo)
        human_wt = os.path.join(base, "wt-human")
        git(["worktree", "add", human_wt, "robs-feature"], repo)

        targets = discover_targets(cwd=repo)
        target_names = {os.path.realpath(p) for p in targets}

        if os.path.realpath(agent_wt) not in target_names:
            failures.append("agent-branch worktree should be discovered")
        if os.path.realpath(human_wt) in target_names:
            failures.append("human-branch worktree must NOT be discovered")

        # The worktree we're "running from" must be excluded even though its
        # branch matches the agent namespace.
        targets_excluding_self = discover_targets(cwd=agent_wt)
        if os.path.realpath(agent_wt) in {os.path.realpath(p) for p in targets_excluding_self}:
            failures.append("the cwd worktree must never be discovered as its own target")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("trim self-test: OK (agent-branch discovery, human-branch exclusion, "
          "self-exclusion of the running worktree)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
