---
name: grm-stop-point
description: Wind-down for a loop/release hitting a stop condition (blocked-on-human, cycle-budget, merge conflict, test failure, guard block, user stop) — merges what's ready, ticks §5, parks blocked items with an issue comment, writes one status report. Use when a loop stops, `blocked_on_human`/`cycle_budget_exceeded` fires, or the user says "wind down"/"stop the loop".
---

# Stop point — wind-down on a stop condition

The canonical "get everything merged, ledger-ticked, and reported, then park
cleanly" sequence — the one users kept typing from memory (#422: ~8 manual
aborts fleet-wide in 5 weeks, each followed by the same incantation). This
skill packages it into one call. It **orchestrates existing skills**; it does
not reimplement merging, ledger-ticking, or issue-commenting.

**Not a release finale.** This is narrower than `grm-end-session` — it does
**not** run `grm-project-release`, push, or clean up worktrees. It parks the
run in a safe, reportable state so a human (or a later resumed session) can
pick it up. Run `grm-end-session` separately when the release itself is
actually ready to ship.

## When to invoke

- **`noir_loop_state.py` reports `blocked_on_human: true`** (§Stop conditions,
  `grm-noir-loop`) — the progress-hash over open work + the current blocker
  repeated unchanged for `STALL_LIMIT` consecutive iterations.
- **`noir_loop_state.py` reports `cycle_budget_exceeded: true`** — `iteration`
  reached the configurable `max_cycles` cap, whether or not progress was
  being made.
- Any other `grm-orchestrate-release` stop condition where the right response
  is "wind down and report" rather than "fix and continue" (e.g. a merge
  conflict or test failure that needs a human decision before more work can
  land).
- The user explicitly says to wind down, stop the loop, or park the release.

## Steps

1. **Assess state.** Read what triggered the stop:
   - Loop context: `python3 .claude/skills/grm-noir-loop/noir_loop_state.py --read`
     (or the `read_loop_state` MCP tool) — note `blocked_on_human`,
     `cycle_budget_exceeded`, `blocker`, `stall_count`, `open_work`.
   - Release context: the active `docs/release-planning/release-planning-v{X.Y}.md`
     §5 ledger (`get_ledger` MCP tool or `grm-release-agent-tracker`'s
     `release_plan.py get-ledger`) — which branches are Implemented but not
     yet Merged.
   - `git symbolic-ref --short HEAD` + `git worktree list` — confirm you are
     on the staging branch, not a stray work-item worktree
     (`grm-worktree-preflight` if in doubt).

2. **Merge whatever's ready.** Delegate to **`grm-release-phase-merge`** for
   every branch that is genuinely mergeable (tests green, no unresolved
   conflict, not the blocked item itself). Do **not** attempt to force through
   the branch/item that caused the stop — that is precisely what a human
   needs to look at. If merging a ready branch itself hits a new stop
   condition (conflict, test failure), record it and continue with the
   remaining ready branches rather than aborting the whole wind-down.

3. **Tick the ledger.** Delegate to **`grm-ledger-tick`** for every branch
   Step 2 actually landed (the MCP `tick_rows` tool or the CLI fallback). One
   commit for the sweep, per that skill's own convention — this skill adds no
   ledger-editing logic of its own.

4. **Park anything blocked with a comment.** For each item still open because
   of the stop condition (the human-gated item, the unresolved conflict, the
   item hitting the cycle-budget cap with no clear next step):
   ```bash
   python3 .claude/skills/grm-issue-tracker/issue_tracker.py comment <id> \
     --body "Parked by grm-stop-point ({reason}): {one-sentence state + what's needed from a human}."
   ```
   (or the `comment_issue` MCP tool). Keep the comment factual and specific —
   what's done, what's blocking, and the exact human decision or action that
   unblocks it. This is issue-tracker-only; it makes no git commits.

5. **Write the status report.** One summary covering, in order:
   - What landed this wind-down (branches merged, §5 rows ticked).
   - What's still open, and **why** — name the stop condition explicitly
     (`blocked_on_human`, `cycle_budget_exceeded`, merge conflict, etc.) and
     the specific blocker text / item.
   - What a human needs to do to unblock it (the single concrete action —
     approve X, decide Y, clear the label, answer the pending question).
   - Where to resume: `grm-release-phase-merge` (more branches ready),
     `grm-orchestrate-release` (restart the loop once unblocked), or
     `grm-end-session` (release is actually ready to ship).

6. **Stop.** Do not spawn another loop iteration, do not release, do not
   push, do not clean up worktrees. The point of this skill is to leave a
   clean, resumable, human-reportable state — not to push through the
   condition that triggered it.

## Anti-patterns

- Forcing through the blocked item to "make the loop happy" — the whole
  point of `blocked_on_human` is that only a human can clear it.
- Re-running the loop immediately after `cycle_budget_exceeded` without
  raising `max_cycles` or addressing why the budget was hit — that just
  reproduces the runaway loop the cap exists to catch.
- Skipping Step 4 (the issue comment) — a stop with no tracker trace is
  indistinguishable from a session that simply vanished; the next
  human/session has nothing to resume from.
- Treating this skill as a release finale — it never runs
  `grm-project-release`, never pushes, never removes worktrees. Use
  `grm-end-session` for that.
- Re-implementing merge/ledger/comment logic inline instead of delegating to
  `grm-release-phase-merge` / `grm-ledger-tick` / `grm-issue-tracker` — this
  skill is an orchestrator, not a new engine.
