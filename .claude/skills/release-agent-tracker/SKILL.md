---
name: release-agent-tracker
description: Track the status of all subagent branches for the in-flight release, mark agents done when the user reports back, and determine what is ready to merge. Use when the user says "agent X is done", "what's ready to merge", "check agent status", "which branches are finished", "mark foo as done", or any time you need to know the current merge queue without user input. Source of truth is the §5 ledger in the agreed release plan.
---

# Release agent tracker

> **Model/effort:** schema-constrained mechanical reconciliation (read §5, diff
> against `git branch`, flip `☐`→`☑`). Pin it to **Sonnet / inherit** — it needs
> table-following capacity but not Opus judgement; inheriting session Opus is waste.

The §5 ledger in `docs/release-planning-v{X.Y}.md` is the canonical source of
truth for agent status. This skill reads it, cross-references the live git
branch list, and updates it when the user reports an agent done.

> **Preferred interface — the `grimoire-release` MCP server (v3.27).** This whole
> read-§5 / diff-vs-git / compute-merge-queue / tick loop is now a deterministic
> engine. When `mcp.enabled` and the server is registered (root `.mcp.json`),
> prefer its native tools instead of hand-parsing and hand-editing: **`get_ledger`**
> (§5 → JSON: passes, rows, tri-state checkboxes, branches, item ids),
> **`diff`** (ledger vs `git branch` → per-row state), **`merge_queue`**
> (toposort over §3's conflict map), **`tick_rows`** (atomic + idempotent cell
> flips — never overwrites `n/a`). The server is **file-write-only**; **you still
> commit** the ticked ledger. **CLI fallback** (no MCP / disabled): `python3
> .claude/skills/release-agent-tracker/release_plan.py {get-ledger|diff|merge-queue}`
> and `… tick --branch B --column implemented --value true` — identical engine.
> The Steps below are the fallback procedure (and the conceptual model the tools
> implement). Design: `docs/design/grimoire-release-server-design.md`.

---

## Step 1 — Locate the active plan

```bash
ls docs/release-planning-v*.md
```

Pick the highest-version file with `status: agreed`. Read §5 entirely.

**Read it once per merge sweep, then reuse it.** When invoked to reconcile a
batch of branches, parse §5 a single time and hold the parsed table in working
memory for every branch in the sweep — do not re-`ls`/re-read the plan per
branch. **Safety constraint:** the cached §5 is valid only until the next write
to the plan (a tick) or a git mutation; re-read after either. Since the
integration master owns all writes to the plan and the branch set during a
sweep, no external write can stale the cache mid-sweep.

---

## Step 2 — Cross-reference with git

```bash
git branch          # local branches
git branch -r       # remote branches (if agents pushed)
```

For each branch row in §5, determine its actual state:

| §5 Implemented | §5 Merged | Branch exists? | State |
|---|---|---|---|
| ☐ | ☐ | No | **Not started** |
| ☐ | ☐ | Yes | **In progress** (agent is working) |
| ☑ | ☐ | Yes | **Done — ready to merge** |
| ☑ | ☑ | — | **Merged** |

---

## Step 3 — When the user says "agent X is done"

1. Identify the branch from the user's message (exact name or item ID).
2. Confirm the branch exists: `git branch | grep {branch-name}`.
3. Edit the §5 row: tick ☑ in the **Implemented** column.
   - This edit is in §5, so the `release-plan-guard` hook allows it.
4. Report the updated state table.

Do not mark ☑ Implemented until the user explicitly confirms the agent has
finished and passed tests. "The agent started" is not "done."

---

## Step 4 — Determine the merge queue

After updating, output the current merge queue:

```
Phase N merge queue (dependency order from §3 conflict map):
  1. foo  — ready ✓
  2. bar  — ready ✓
  3. baz  — waiting on foo (conflict map dependency)

Not yet done:
  qux   — in progress
  quux  — not started
```

If all branches in the current phase are ☑ Implemented, say so clearly and
recommend running `release-phase-merge`.

---

## Step 5 — Status table format

Always end with a full status snapshot in table form, grouped by phase:

```
### Phase 1

| Branch       | Item   | Implemented | Merged |
|---|---|---|---|
| foo     | ITEM-1 | ☑           | ☐      |
| bar     | ITEM-2 | ☑           | ☐      |
| baz     | ITEM-3 | ☐           | ☐      |

### Phase 2

| Branch       | Item   | Implemented | Merged |
|---|---|---|---|
| qux     | ITEM-4 | ☐           | ☐      |
```

---

## What this skill does NOT do

- It does not merge branches. Use `release-phase-merge` for that.
- It does not run tests. Tests run during `release-phase-merge`.
- It does not tick ☑ Merged — that happens inside `release-phase-merge` after
  a successful test run.

---

## Anti-patterns

- Marking ☑ Implemented speculatively ("the agent probably finished by now").
  Wait for the user's explicit confirmation.
- Marking ☑ Merged manually without running tests. The whole point of the
  separate Merged column is that Implemented ≠ merged + tested.
- Forgetting to check §3's conflict map when determining merge order — two
  "ready" branches may still need to merge sequentially.
