---
name: release-phase
description: Spawn work-item sessions (via spawn_task) for the next open phase of the in-flight release. Groups work by dependency, sizes each item by token estimate, and assigns model/effort per the `repo-reference` skill table. Use when the user says "start phase N", "spawn the tasks", "kick off phase", "what do agents need to do for phase N", or "distribute phase work". Run after release-agreement has locked the plan.
---

# Release phase — spawn work-item sessions (Supervised)

Reads the agreed release plan, identifies the next open phase, groups its work
items into parallel batches, and uses the **`spawn_task`** tool to open a new
session in an isolated worktree for each item. The integration master never
hands the user raw copy-paste prompts — it spawns the sessions directly.

---

## Step 1 — Locate the active plan and current phase

```bash
ls docs/release-planning-v*.md
```

Pick the highest-version file with `status: agreed` (check first 15 lines).
Read §3 (pass structure + conflict map) and §5 (ledger) to determine:

- **Current phase** = the first pass whose rows are all ☐ Implemented.
- If a phase is partially done (some ☑, some ☐), it is still the current
  phase — only spawn the ☐ rows.
- If all passes are ☑, there is nothing to spawn; move to
  `release-phase-merge` for the final `version/{X.Y}` → `dev` step.

---

## Step 2 — Group the phase into parallel batches

Read §3's conflict map. Items are in the same batch if:

1. Neither depends on the other's output (no shared files that must serialise),
   AND
2. They are marked as parallel in §3's merge order.

Each batch is a set of work items that can run concurrently in separate
worktrees. Within a batch, order doesn't matter. Across batches, earlier
batches must be merged before later ones start.

If §3 has no explicit conflict map, apply the conservative default: one item
per batch (fully serialised).

---

## Step 3 — Assign model and effort

For each item, use the token estimate recorded in the release plan:

| Est. tokens | Model  | Effort  |
|---|---|---|
| ≤ 15 K      | haiku  | low     |
| 15 K–80 K   | sonnet | inherit |
| > 80 K or architecture / design review | opus | high |

When in doubt, err toward sonnet. `spawn_task` cannot set the spawned session's
model, so the recommended model is named in the chip (title + prompt) for the
user to set when opening the session.

---

## Step 4 — Confirm before spawning (Supervised gate)

Before calling `spawn_task`, present the batch to the user:

- List each item: ID, title, recommended model, branch name.
- Ask: "Spawn these N items now?"

Wait for explicit confirmation. Do not spawn until the user says yes.

---

## Step 5 — Spawn each item with `spawn_task`

For each item in the current batch, call the **`spawn_task`** tool
(`mcp__ccd_session__spawn_task`). The spawned session has no memory of this
session, so the `prompt` must be self-contained.

- **title**: `{ITEM-ID}: {short title} — set model {model}/{effort}`
- **tldr**: one plain-English sentence on what the session will do.
- **prompt**: the self-contained block below.

```
## Task: {ITEM-ID} — {short title}
Recommended model: {model} | effort: {effort} — set this in your session before starting.

You are running in your own fresh, isolated worktree. Stay in it.

### Root your worktree on the release-staging ref
Run the `worktree-preflight` skill first. Your work must be rooted on
`version/{X.Y}` (the staging tip), not `main`. If the harness left HEAD
elsewhere, branch in place from the ref (name the REF, not ambient HEAD):

    git switch -c {branch-name} version/{X.Y}

Verify (must print ROOT-OK; if ROOT-BAD, stop and report):

    [ "$(git merge-base HEAD version/{X.Y})" = "$(git rev-parse version/{X.Y})" ] && echo ROOT-OK || echo ROOT-BAD

Do NOT `git worktree add`, do NOT `cd` to a canonical/other repo path, and do
NOT edit or git-operate on the integration worktree or any sibling worktree —
a repo guard hook blocks cross-worktree paths anyway.

### Context — read before touching code
- docs/release-planning-v{X.Y}.md §2.{N} — this item's scope + acceptance
- docs/design/{feature}-design.md — full feature design
- docs/coding-standards.md and docs/architecture-guidelines.md — standing rules
- {any other design doc cross-linked in the plan}

### Work
{Exact item description copied from the release plan §2.{N}.
Include acceptance criteria verbatim.}

### Constraints
- Scope strictly to the files listed in §2.{N}. Do not touch
  docs/release-planning-v{X.Y}.md.
- Write or extend the design doc for this item if §2.{N} flags one missing.
- Run `{test-command}` and `{build-command}` before finishing.
- Fix all errors and warnings introduced by your changes.
- Review your own diff against the acceptance criteria before reporting done.

### When done
Do NOT merge. Report back:
1. The branch name you worked on
2. Test result (pass / N failures)
3. One-paragraph summary of what was implemented
4. Any deferred follow-ups discovered (gaps left for a future item)
```

Replace `{test-command}` and `{build-command}` with your project's actual
commands (see CLAUDE.md §Project commands).

---

## Step 6 — Spawn the batch, then wait

Spawn every item in the current batch (one `spawn_task` call each), then stop
and tell the user:

- How many chips were dropped and which items they cover.
- To open each chip, set the named model, and let the session run.
- To say "agent {branch-name} is done" when a session reports back, so
  `release-agent-tracker` can mark it ☑ Implemented and queue it for merge.
- **Do not** spawn the next batch until the current batch is merged
  (`release-phase-merge`) — later batches build on earlier merges.

---

## Anti-patterns

- Spawning without user confirmation (Supervised gate — always ask first).
- Handing the user raw copy-paste prompts instead of calling `spawn_task`.
- Including merge instructions in a spawned prompt — work-item agents never merge.
- Batching items that share files (check §3's conflict map carefully).
- Forgetting to name the recommended model in the chip — the user can't size it
  for you.
- Forgetting to tell the spawned session to root on `version/{X.Y}` — a fresh
  worktree often lands on `main`'s tip.
- Spawning Batch 2 before Batch 1 is merged — agents will hit merge conflicts
  that are hard to resolve headlessly.

## Shared-context dispatch (v1.29, #59)

When dispatching a batch of agents, minimize per-agent prompt size:

- Write the **shared brief once** — the design doc reference, the relevant
  standards, and the common acceptance criteria — as a compact preamble the batch
  shares. Do not paste the full context into every agent prompt.
- Give each agent only its **per-item delta**: its specific files, its branch, and
  its one acceptance criterion. Label by item, not by re-stating shared context.
- Result: materially smaller per-agent prompts with no loss of fidelity.
  Authority: `docs/design/context-efficiency-design.md`.
