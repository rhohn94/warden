---
name: release-phase
description: Dispatch work-item subagents for the next open phase autonomously — no per-item confirmation, no chips. Groups work by dependency, sizes each item by token estimate, and dispatches the full batch at once via isolated-worktree subagents. Use when the user says "start phase N", "dispatch the tasks", or "kick off phase". Run after release-agreement has locked the plan.
---

# Release phase — spawn work-item sessions (Noir)

Reads the agreed release plan, identifies the next open phase, groups its
work items into parallel batches per §3's conflict map, and dispatches the full
current batch at once — no per-item confirmation.

> **Noir is chip-free.** Under Noir the master never drops `spawn_task` chips
> for work-item dispatch — chips require a human click and break the autonomous
> posture. Dispatch is always via **isolated-worktree subagents** (`Agent` with
> `isolation:"worktree"`) or a **write-capable Workflow**. The chip-based path
> belongs to the Supervised / Weiss paradigms only.

**This is the Noir default execution path.** Once a plan reaches
`status: agreed` with a `version/{X.Y}` staging branch, the master enters this
skill **by default** — it dispatches the phase's work items as separate
isolated-worktree agents rather than implementing them inline in its own
session. Dispatching is what "execute the plan" means under Noir; building the
items solo is the anti-pattern (see the integration-master §Default execution
path and its soft guard).

**`release-phase-model` dial.** The master reads `release-phase-model.value`
live before dispatching. When it is `Default` (or absent), dispatch the phase
via the isolated-worktree subagent flow below (`Agent` with
`isolation:"worktree"`). When it is **`Auto`** (Noir only — otherwise
fall back to `Default` and log the downgrade), dispatch the phase's items
instead via a **write-capable Workflow**, whose isolated-worktree agents each
implement one item and return a branch; the returned branches are then merged
in `mergeAfter` order by `release-phase-merge`. `Auto` reuses the existing
write-capable tier — no new machinery — and the execution variant still comes
from `workflow-variant`. See the integration-master §Write-capable Workflow
integration / §`release-phase-model` dial and
`docs/design/release-phase-model-design.md`.

---

## Step 1 — Locate the active plan and current phase

```bash
ls docs/release-planning-v*.md
```

Pick the highest-version file with `status: agreed` (check first 15 lines).
Read §3 (pass structure + conflict map) and §5 (ledger) to determine:

- **Current phase** = the first pass whose rows are all ☐ Implemented.
- Only spawn the ☐ rows; skip any already ☑.
- If all passes are ☑, move to `release-phase-merge` for the final
  `version/{X.Y}` → `dev` step.

---

## Step 2 — Group the phase into parallel batches

Apply §3's conflict map. Items go in the same batch if:

1. Neither depends on the other's output (no shared files that must
   serialise), AND
2. They are marked parallel in §3's merge order.

If §3 has no explicit conflict map, use conservative default: one item per
batch.

---

## Step 3 — Assign model and effort

Apply the token estimate from the release plan:

| Est. tokens | Model  | Effort  |
|---|---|---|
| ≤ 15 K      | haiku  | low     |
| 15 K–80 K   | sonnet | inherit |
| > 80 K or architecture / design review | opus | high |

Err toward sonnet when uncertain.

---

## Step 3.5 — Validate milestone labels (hard gate)

Before dispatching any work item, verify that every planned issue for the current
release carries a `milestone:vX.Y` label matching the release version in the
active plan file.

**How to check:**

```bash
python3 .claude/skills/issue-tracker/issue_tracker.py list --state open \
  | grep -v "milestone:v{X.Y}"
```

Or inspect each planned issue's labels via the issue-tracker abstraction:

```python
for item in planned_issues:
    issue = tracker.get(item.id)
    has_milestone = any(
        lbl.startswith("milestone:v") and lbl == f"milestone:v{release_version}"
        for lbl in issue.labels
    )
    if not has_milestone:
        unlabeled.append(item)
```

**Gate behavior:**

- If **all** planned issues carry the correct `milestone:vX.Y` label → proceed
  to Step 4 (dispatch).
- If **any** planned issue is missing the label → **STOP**. Do not dispatch any
  items. Output a clear error listing every unlabeled issue:

  ```
  ERROR: Milestone gate failed — the following issues lack a milestone:vX.Y label
  and cannot be dispatched:

    - #{id}: {title}
    - #{id}: {title}

  Action required: run the Triager with milestone-assignment scope to label these
  issues before re-running release-phase.
  ```

This is a **hard gate**, not advisory. The dispatch does not proceed until all
planned issues are labeled. An issue carrying `milestone:backlog` is also blocked —
backlog items should not be dispatched in a release phase for version vX.Y.

---

## Step 4 — Dispatch the full batch (chip-free)

Dispatch **every item in the current batch** as an isolated-worktree subagent
without pausing between calls — no `spawn_task` chips. Use the `Agent` tool with
`isolation:"worktree"` (or, when `release-phase-model` is `Auto`, a write-capable
Workflow). Each subagent receives its own worktree and short-lived branch,
implements one item, and returns its branch for merge.

- **model/effort**: set directly on the dispatch per the Step 3 table — the
  master sizes each subagent; there is no chip for a human to size.
- **label/description**: `{ITEM-ID}: {short title}`.
- **prompt**: self-contained task block (same task template as Supervised
  `release-phase`), built per the §Shared-context dispatch guidance below.

> **Noir no-chip clause (mandatory).** Every Noir task-agent prompt MUST
> include the following verbatim — copy it word-for-word into every dispatched
> agent's prompt, as a top-level requirement before the work description:
>
> > "Report all out-of-scope follow-ups as plain text in your final report.
> > Never call `spawn_task`, never create chips, never ask the user; you are
> > running unattended."
>
> **Rationale:** the dispatched subagent carries the full tool set, including
> `spawn_task`. Without an explicit prohibition, it may call `spawn_task` to
> flag out-of-scope discoveries, which creates a chip requiring a human click
> — stalling the unattended run. This clause is the primary prompt-side guard
> (see `integration-master/SKILL.md` §Dispatch is chip-free for the
> master-side re-routing layer).

---

## Step 5 — Report and proceed to merge

After dispatching the batch, report to the user:

- How many subagents were dispatched and which items they cover.
- The model/effort assigned to each (the master sizes them — no human action
  is required to start them).
- That the master will proceed to merging as subagents return their branches.

**Do not dispatch Batch 2 until Batch 1 is merged** — merge conflicts are hard
to resolve headlessly.

---

## Anti-patterns

- Dropping `spawn_task` chips for work-item dispatch — chips are a
  Supervised / Weiss mechanism; Noir dispatches via isolated-worktree subagents.
- Pausing between items for per-item confirmation (use Supervised posture
  for that).
- Including merge instructions in a dispatched prompt — work-item agents never
  merge.
- Batching items that share files — check §3 carefully.
- Dispatching Batch 2 before Batch 1 is merged.
- Implementing the phase's work items inline in the master's own session
  instead of dispatching them — dispatch is the Noir default (see the
  integration-master §Default execution path soft guard).

## Shared-context dispatch (v1.29, #59)

When dispatching a batch of agents, minimize per-agent prompt size:

- Write the **shared brief once** — the design doc reference, the relevant
  standards, and the common acceptance criteria — as a compact preamble the batch
  shares. Do not paste the full context into every agent prompt.
- Give each agent only its **per-item delta**: its specific files, its branch, and
  its one acceptance criterion. Label by item, not by re-stating shared context.
- Result: materially smaller per-agent prompts with no loss of fidelity.
  Authority: `docs/design/context-efficiency-design.md`.
