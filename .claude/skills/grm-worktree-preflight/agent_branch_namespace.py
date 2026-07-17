#!/usr/bin/env python3
"""agent_branch_namespace.py — canonical agent-branch namespace predicate (#456).

Grounds "agent-created, safe to reap once merged+landed" vs "human-created,
never auto-touch" in one mechanical, name-only check, so a reap tool (e.g.
issue #449's `worktree_reap.py`, planned alongside this file) never has to
regex-guess a branch's provenance.

Canonical convention (documented in `docs/grimoire/integration-workflow.md`
§Canonical agent-branch namespace and
`docs/grimoire/design/branch-topology-design.md` §4): every branch an agent
mints — task-agent work items, write-capable Workflow agents, release-master
loop iterations' own dispatches, Claude Code CLI session branches — carries
the single prefix `claude/`. Concrete shapes already observed in this fleet
(grounded via `git branch -a` and the skill/design docs, not invented):

  claude/r{N}-{issue}-{slug}        task-agent work-item branches
                                     e.g. claude/r4-449-worktree-reap-engine
  claude/<item-slug>-<short-uuid>   write-capable Workflow agent branches
  claude/<slug>-<hash>              Claude Code CLI session branches
                                     e.g. claude/grimoire-r3-orchestration-6e2ea3
                                     ("the harness already uses it" — reused
                                     rather than inventing a second prefix)

A **legacy fallback tier** additionally recognizes branches minted before
this convention landed (v3.95), so a reap tool does not misclassify
pre-convention agent branches as human-created. This tier is a drain-down
aid, not part of the canonical convention — nothing should mint new branches
matching only the fallback tier after v3.95.

Protected refs (`main`, `dev`, `version/*`, the parking branch `home`, #454)
are never agent-created work branches in this sense even though the master
mints `version/{X.Y}`; callers must exclude the protected set themselves
(see `worktree-reaping-design.md` §1) — this predicate answers only the
narrower "does the *name* look agent-minted" question.

No git writes, no network calls — pure string classification.

Usage as a library:
    from agent_branch_namespace import is_agent_branch
    is_agent_branch("claude/r4-456-agent-branch-namespace")  # True
    is_agent_branch("robs-branch")                            # False

Usage as a CLI:
    agent_branch_namespace.py --check <branch-name>   # prints True/False, exit 0/1
    agent_branch_namespace.py --self-test
"""
from __future__ import annotations

import argparse
import re
import sys

CANONICAL_PREFIX = "claude/"

# Pre-convention patterns that predate the `claude/` namespace but are still
# agent-minted. Drains over time; do not extend for new branch shapes — new
# agent branches must use CANONICAL_PREFIX instead of growing this list.
_LEGACY_AGENT_PATTERNS = (
    re.compile(r"^worktree-agent-[0-9a-f]{6,}$"),  # harness spawn stubs
    re.compile(r"^worker-.+$"),
    re.compile(r"^wf-.+$"),
)

# Never agent-created work branches, regardless of shape — the protected set
# (worktree-reaping-design.md §1) plus the parking branch (#454). Included so
# a careless caller passing a protected ref through this predicate gets the
# conservative False rather than a false-positive True.
_PROTECTED_NAMES = frozenset({"main", "dev", "home"})
_PROTECTED_PATTERN = re.compile(r"^version/")


def is_agent_branch(name: str) -> bool:
    """Return True iff `name` is agent-created by the canonical namespace
    convention (#456) or its documented legacy fallback tier.

    This is a name-only classification. `True` means "eligible for reap
    classification" (worktree-reaping-design.md's dead-ness predicate must
    still confirm merged + clean before anything is deleted) — never
    "safe to delete unconditionally". `False` means "never auto-touch",
    covering both human branches and the protected/staging set.
    """
    if not name:
        return False
    if name in _PROTECTED_NAMES or _PROTECTED_PATTERN.match(name):
        return False
    if name.startswith(CANONICAL_PREFIX):
        return True
    return any(pattern.match(name) for pattern in _LEGACY_AGENT_PATTERNS)


def _self_test() -> int:
    cases = [
        ("claude/r4-456-agent-branch-namespace", True),
        ("claude/r4-449-worktree-reap-engine", True),
        ("claude/grimoire-r3-orchestration-6e2ea3", True),
        ("claude/update-config-parser-a3f1", True),
        ("worktree-agent-af2a3964716393f04", True),
        ("worktree-agent-a1bb88126776ed97d", True),
        ("worker-1234", True),
        ("wf-sync-deps", True),
        ("robs-branch", False),
        ("main", False),
        ("dev", False),
        ("home", False),
        ("version/3.95", False),
        ("version/3.95/lane-a", False),
        ("", False),
        ("feature/human-branch", False),
    ]
    failures = []
    for name, expected in cases:
        actual = is_agent_branch(name)
        if actual != expected:
            failures.append((name, expected, actual))

    if failures:
        for name, expected, actual in failures:
            print(f"FAIL {name!r}: expected {expected}, got {actual}", file=sys.stderr)
        print(f"agent_branch_namespace self-test: {len(failures)} failure(s)", file=sys.stderr)
        return 1

    print(f"agent_branch_namespace self-test: OK ({len(cases)} cases)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Classify a branch name as agent-created vs human-created (#456)."
    )
    ap.add_argument("--check", metavar="BRANCH", help="branch name to classify")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    if args.check is not None:
        result = is_agent_branch(args.check)
        print(result)
        return 0 if result else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
