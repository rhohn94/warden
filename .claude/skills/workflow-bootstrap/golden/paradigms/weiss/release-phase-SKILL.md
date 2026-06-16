---
name: release-phase
description: Spawn work-item sessions (via spawn_task) for the next open phase of the in-flight release, with per-item user confirmation. Use when the user says "start phase N", "spawn the tasks", "kick off phase", or "distribute phase work". Run after release-agreement has locked the plan.
---

# Release phase — spawn work-item sessions (Weiss)

Reads the agreed release plan, identifies the next open phase, and spawns
work-item sessions one at a time — each with explicit user approval before
the `spawn_task` call.

---

## Step 1 — Locate the active plan and current phase

```bash
ls docs/release-planning-v*.md
```

Pick the highest-version file with `status: agreed` (check first 15 lines).
Read §3 (pass structure + conflict map) and §5 (ledger) to determine the
current phase and the ☐ rows remaining.

---

## Step 2 — Present the dependency graph

Read §3's conflict map. Show the user:

- A list of all ☐ items in the current phase.
- Which items can run in parallel vs. must be serialised.
- Any unresolved conflict that would require a different grouping.

Ask the user to confirm the grouping before proceeding.

---

## Step 3 — Assign model and effort

For each item, apply the token estimate from the release plan:

| Est. tokens | Model  | Effort  |
|---|---|---|
| ≤ 15 K      | haiku  | low     |
| 15 K–80 K   | sonnet | inherit |
| > 80 K or architecture / design review | opus | high |

Present the assignment to the user; flag any items where the estimate is
uncertain and ask the user to confirm the model before spawning.

---

## Step 4 — Spawn one item at a time (per-item gate)

For each item in the approved grouping, present a brief:

```
Item:   {ITEM-ID} — {title}
Model:  {model} / {effort}
Branch: {branch-name}
```

Ask: "Spawn `{ITEM-ID}`?" and wait for explicit "yes" before calling
`spawn_task`. Do not batch-spawn: each item gets its own confirmation.

When confirmed, call `mcp__ccd_session__spawn_task`:

- **title**: `{ITEM-ID}: {short title} — set model {model}/{effort}`
- **tldr**: one plain-English sentence on what the session will do.
- **prompt**: self-contained task block (see Supervised `release-phase` for
  the full prompt template).

---

## Step 5 — After spawning each chip

Tell the user:
- The chip was dropped.
- To open it, set the named model, and let it run.
- To report back when the session is done so `release-agent-tracker` can
  mark it ☑ Implemented.

Then ask: "Ready to spawn the next item, or wait for this one to report back?"
Let the user pace the spawning.

---

## Anti-patterns

- Batch-spawning multiple items without per-item confirmation.
- Choosing a model assignment without surfacing uncertainty to the user.
- Deciding the conflict-map grouping without presenting the dependency graph.
- Proceeding to the next item before the user has confirmed.

## Shared-context dispatch (v1.29, #59)

When dispatching a batch of agents, minimize per-agent prompt size:

- Write the **shared brief once** — the design doc reference, the relevant
  standards, and the common acceptance criteria — as a compact preamble the batch
  shares. Do not paste the full context into every agent prompt.
- Give each agent only its **per-item delta**: its specific files, its branch, and
  its one acceptance criterion. Label by item, not by re-stating shared context.
- Result: materially smaller per-agent prompts with no loss of fidelity.
  Authority: `docs/design/context-efficiency-design.md`.
