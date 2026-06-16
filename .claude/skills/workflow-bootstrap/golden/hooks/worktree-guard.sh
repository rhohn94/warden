#!/usr/bin/env python3
"""Worktree-discipline guard (deny-by-default).

Blocks tool calls whose target path escapes the active worktree into the
canonical project checkout or a sibling worktree. No-op when not running
inside a worktree (i.e. CLAUDE_PROJECT_DIR does not contain
/.claude/worktrees/), so the canonical repo and non-worktree sessions are
unaffected.

Also a no-op when the active worktree carries the integration-allow marker
(`.claude/integration-allow.local`). That worktree is the blessed
integration master, trusted to cross worktree boundaries for housekeeping
like removing a dead sibling worktree after verifying it's safe (see
`docs/integration-workflow.md` §Dead-worktree cleanup). Symmetric with
`protected-branch-guard.sh`.

Covers Edit / Write / NotebookEdit (structured file_path) and Bash (scan
absolute-path tokens in the command). Read is intentionally not gated;
over-blocking reads breaks too many harness internals.

Write-capable workflow agent safety contract (v1.6+)
=====================================================
Write-capable Workflow agents (Noir paradigm, isolation: 'worktree') each
receive their own isolated worktree rooted at a unique path under
/.claude/worktrees/<id>/. This guard confines each agent to its own
worktree automatically, with no additional configuration:

  AGENT worktrees (no marker):
    - May freely Edit / Write / Bash within their own worktree root.
    - Are DENIED any Edit / Write / Bash that resolves to the canonical
      checkout root or to a sibling worktree path.
    - Multiple concurrent agent worktrees are each confined independently;
      no agent can mutate another agent's files or the canonical tree.

  MASTER worktree (has marker):
    - Is exempt from this guard (marker = trusted for cross-boundary ops).
    - This is required so the master can perform dead-worktree cleanup and
      post-merge housekeeping across worktree paths.

  Together with protected-branch-guard.sh, these two guards enforce the
  full write-capable-agent safety contract:
    - Agents stay inside their own worktree (this guard).
    - Agents cannot touch protected branches (protected-branch-guard.sh).
    - Only the marker-blessed master may merge to staging (both guards).
  See docs/design/write-capable-workflow-design.md §3 (Safety rails) and
  §5.3 (Guard model implications).

Cross-worktree branch hijack (v1.7, item A3)
============================================
This guard already blocks any Bash command token that resolves under the
canonical root — which INCLUDES sibling worktree paths, since they live at
<canonical>/.claude/worktrees/<id>/. So an unmarked agent that tries
`git -C /…/.claude/worktrees/<other> switch -c …` or `cd /…/<other> && git …`
is denied here because the absolute target path is an out-of-worktree token.
The companion protected-branch-guard.sh adds a branch-op-aware layer that
names the integration master explicitly and refuses redirected branch
creation/switching even when this guard's path scan is evaded. The two are
defence-in-depth for the same v1.6 hijack finding.

Test/check note: to verify the contract holds for concurrent agent
worktrees, confirm that:
  1. An Edit/Write with a path inside the agent's own worktree is allowed.
  2. An Edit/Write with a path under the canonical root is blocked (exit 2).
  3. An Edit/Write with a path under a sibling worktree is blocked (exit 2).
  4. A Bash `git -C <sibling-worktree> …` token is blocked (exit 2).
  5. In the master worktree (marker present), all paths are allowed.
"""
import json
import os
import re
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if "/.claude/worktrees/" not in proj:
        sys.exit(0)

    # Blessed integration worktree — trusted to cross worktree boundaries
    # (e.g. dead-worktree cleanup). Symmetric with protected-branch-guard.sh;
    # see docs/integration-workflow.md §Dead-worktree cleanup.
    marker = os.path.join(proj, ".claude", "integration-allow.local")
    if os.path.isfile(marker):
        sys.exit(0)

    try:
        worktree_root = os.path.realpath(proj)
    except OSError:
        sys.exit(0)

    canonical_root = worktree_root.split("/.claude/worktrees/")[0]
    if not canonical_root:
        sys.exit(0)

    wt_lc = worktree_root.lower()
    canon_lc = canonical_root.lower()

    def resolve(p: str) -> str:
        try:
            return os.path.realpath(p)
        except OSError:
            return os.path.normpath(p)

    def is_escape(path: str) -> bool:
        if not path or not path.startswith("/"):
            return False
        rp = resolve(path).lower()
        if rp.startswith(wt_lc + "/") or rp == wt_lc:
            return False
        return rp.startswith(canon_lc + "/") or rp == canon_lc

    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input", {}) or {}

    bad: str | None = None
    if tool in ("Edit", "Write", "NotebookEdit"):
        fp = tin.get("file_path") or tin.get("notebook_path")
        if is_escape(fp or ""):
            bad = fp
    elif tool == "Bash":
        cmd = tin.get("command", "") or ""
        for tok in re.findall(r"(?:^|[\s'\"=:])(/[A-Za-z0-9._/\-]+)", cmd):
            if is_escape(tok):
                bad = tok
                break

    if bad:
        # Name the sibling-worktree case explicitly: a path that resolves
        # under .claude/worktrees/ but is not this worktree is another
        # agent's (or the integration master's) tree — the v1.6 hijack
        # target. Make the refusal unambiguous about that.
        bad_rp = resolve(bad).lower()
        in_siblings = "/.claude/worktrees/" in bad_rp and not (
            bad_rp.startswith(wt_lc + "/") or bad_rp == wt_lc
        )
        detail = (
            "  This is ANOTHER worktree (a sibling agent's or the "
            "integration master's).\n"
            "  Stay in your own worktree and branch in place from the "
            "staging ref;\n  never git-operate on another worktree.\n"
            if in_siblings else
            "Restrict edits/commands to the worktree. If you genuinely need\n"
            "to touch the canonical repo or a sibling worktree, ask the user.\n"
        )
        sys.stderr.write(
            f"worktree-guard: blocked path '{bad}' — outside this worktree.\n"
            f"  worktree:  {worktree_root}\n"
            f"  canonical: {canonical_root}\n"
            + detail
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
