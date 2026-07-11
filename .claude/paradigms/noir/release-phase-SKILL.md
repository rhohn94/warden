---
name: grm-release-phase
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
in `mergeAfter` order by `grm-release-phase-merge`. `Auto` reuses the existing
write-capable tier — no new machinery — and the execution variant still comes
from `workflow-variant`. See the integration-master §Write-capable Workflow
integration / §`release-phase-model` dial. That dial is a framework-internal
design — see the upstream Grimoire repository for that rationale.

---

## Step 1 — Locate the active plan and current phase

```bash
ls docs/release-planning/release-planning-v*.md
```

Pick the highest-version file with `status: agreed` (check first 15 lines).
Read §3 (pass structure + conflict map) and §5 (ledger) to determine:

- **Current phase** = the first pass whose rows are all ☐ Implemented.
- Only spawn the ☐ rows; skip any already ☑.
- If all passes are ☑, move to `grm-release-phase-merge` for the final
  `version/{X.Y}` → `dev` step.

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

## Step 2.5 — Choose the dispatch posture (execution-strategy)

Step 2 produced the *dependency-correct* batches. The **execution-strategy**
dial now sets the **dispatch posture** — fan-out width and isolation mode — over
those batches. This is independent of the tier resolved in Step 3 and the Noir
ceiling in Step 3a: see the orthogonality note below.

Read `workflow-variant.value` from `.claude/grimoire-config.json`
(absent/unset → `Efficient`; canonical preset set `{Fast, Efficient,
Cheap-Slow}`, matched case-insensitively; a legacy `Careful-Serial` value is
migrated to `Cheap-Slow` by `grm-workflow-variant-switch` — treat any residual
`Careful-Serial` here as `Cheap-Slow`). Apply the posture:

| execution-strategy | Dispatch posture (fan-out + isolation) |
|---|---|
| **Fast** | **Max fan-out** — dispatch every independent item in the current batch concurrently via isolated-worktree subagents. Minimum wall-clock. |
| **Efficient** | **Balanced — default behaviour (unchanged).** Keep Step 2's conflict-map–respecting batches as-is, dedup shared reads via the Step 4 shared-context brief, honour `mergeAfter` ordering. |
| **Cheap-Slow** | **Low fan-out, small batches.** Cap concurrent dispatches to a **small batch (≈2–3 items)**; if a Step 2 batch is wider, sub-split it into sequential small batches (merge each before dispatching the next). Pairs with the Eco-Budget tier profile for genuine cost reduction. NOT literal solo — see the regimes below. |

**Cheap-Slow regime selection** (per `execution-profiles-design.md` §C / §E):

- **Many light / mechanical items** → low fan-out, small parallel batches, tiered down (Eco-Budget).
- **Few (≤ ~10) large / dependent items (the small-heavy corner)** → small-batch parallel dispatch (still cheaper than wide fan-out or solo for this K range).
- **≤ 3 hard-sequential items** → literal solo-serial dispatch is acceptable.
- **Many heavy items** → parallel dispatch, NOT solo.

Record the chosen posture (and, for Cheap-Slow, the regime + whether you
sub-split) to surface in the Step 5 report.

> **Three dials, three independent reads.** This step reads **only**
> `workflow-variant.value` (fan-out / isolation). Step 3 reads **only**
> `model-effort-profile.value` (tier). Step 3a reads **only**
> `work-paradigm.value` (the Noir autonomy ceiling). They **compose** and never
> derive one from another.

---

## Step 3 — Assign model and effort

For each item, resolve `{model, effort}` through the **active model/effort
profile** — do not hard-code a table here. Use the single resolver documented in
the **`grm-repo-reference`** skill (§Subagent model & effort → The resolver):

1. Read `model-effort-profile.value` from `.claude/grimoire-config.json`
   (absent/unset → `Medium`).
2. Classify the item into a complexity **band** from its recorded token estimate
   + design/review flag (trivial ≤ 15 K · small 15–40 K · medium 40–80 K · large
   > 80 K · review = any planning/review/architecture, regardless of estimate).
3. Look up `profiles[<active>][<band>]` in
   `.claude/model-effort-profiles.json` → the `{model, effort}` pair. UX-pin
   items (`grm-design-language-adapt`, `grm-ux-demo-build`) resolve to their fixed pins.

This `{model, effort}` pair is the recommended tier. Err toward sonnet when uncertain.

### Step 3a — Noir dispatch ceiling (always active)

After the resolver yields `{model, effort}`, cap the model at **Sonnet** for
every item that is **neither a review item nor `opus-required`-flagged** (v1.9
audit recs D2/A3/B3):

- **Review items** (band `review` — any planning/review/architecture/security
  analysis) are exempt: keep the resolver's tier.
- **`opus-required` items** (see flag contract below) are exempt: keep the
  resolver's tier.
- **All other items** (trivial/small/medium/large implementation, mechanical):
  if the resolver returned `opus`, lower the model to `sonnet` and keep the
  resolver's `effort` (e.g. `opus/high` → `sonnet/high`). Items the resolver
  already put at Sonnet or Haiku are unchanged.

Note each clamp in the Step 5 report (e.g. `E5: opus/high → sonnet/high (Noir
ceiling)`) so the plan can add `opus-required` if a clamp is wrong.

### The `opus-required` escape hatch

A release plan may declare that a specific item needs Opus despite being
non-review work.

**Flag contract:**

- **Where declared:** in `docs/release-planning/release-planning-v{X.Y}.md`, on the
  item's §2.{N} entry and/or its §5 ledger row, as the literal token
  `opus-required` (e.g. an `opus-required: yes` field, or `opus-required` in the
  item's flags list).
- **Effect:** the item is exempt from the Step 3a ceiling — the resolver's tier
  stands as-is. It does **not** force-raise an item the resolver put below Opus.
- **Scope:** load-bearing only under Noir (this paradigm); advisory under
  Supervised/Weiss.

---

## Step 3.5 — Validate milestone labels (hard gate)

Before dispatching any work item, verify that every planned issue for the current
release carries a `milestone:vX.Y` label matching the release version in the
active plan file.

**How to check:**

```bash
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --state open \
  | grep -v "milestone:v{X.Y}"
```

**Gate behavior:**

- If **all** planned issues carry the correct `milestone:vX.Y` label → proceed
  to Step 4 (dispatch).
- If **any** planned issue is missing the label → **STOP**. Do not dispatch any
  items. Output a clear error listing every unlabeled issue.

This is a **hard gate**, not advisory.

---

## Step 4 — Dispatch the full batch (chip-free)

Dispatch **every item in the current batch** as an isolated-worktree subagent
without pausing between calls — no `spawn_task` chips. Apply the posture chosen
in Step 2.5: `Fast` = max fan-out; `Efficient` = balanced batches (default);
`Cheap-Slow` = sub-split into small sequential batches. Use the `Agent` tool with
`isolation:"worktree"` (or, when `release-phase-model` is `Auto`, a write-capable
Workflow). Each subagent receives its own worktree and short-lived branch,
implements one item, and returns its branch for merge.

- **model/effort**: set directly on the dispatch per the Step 3 resolver result
  (after Step 3a ceiling) — the master sizes each subagent; there is no chip for
  a human to size.
- **label/description**: `{ITEM-ID}: {short title}`.
- **prompt**: self-contained task block (same task template as Supervised
  `grm-release-phase`), built per the §Shared-context dispatch guidance below.
  That template's root check is immediately followed by
  `grm-worktree-preflight`'s Step 0.5 (parent sync) — the subagent syncs
  against `version/{X.Y}` before touching code, and re-runs it on session
  resume, not just at spawn.

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

- The dispatch posture applied (Step 2.5) and what it did to this batch.
- How many subagents were dispatched and which items they cover.
- The model/effort assigned to each (noting any Step 3a Noir ceiling clamps —
  e.g. `E5: opus/high → sonnet/high (Noir ceiling)`); no human action required
  to start them.
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
- Batching items that share files — check §3's conflict map carefully.
- Dispatching Batch 2 before Batch 1 is merged.
- Implementing the phase's work items inline in the master's own session
  instead of dispatching them — dispatch is the Noir default (see the
  integration-master §Default execution path soft guard).
- Dispatching non-review, non-`opus-required` implementation work to Opus —
  the Step 3a ceiling clamps it to Sonnet; only review items and
  `opus-required`-flagged items keep Opus.
- Treating `opus-required` as a promotion — it only exempts an already-Opus
  item from the Noir clamp; it never raises a sub-Opus item (re-band the plan
  for that).
- Letting **execution-strategy change the tier** (or vice versa) — they are
  independent reads (Step 2.5 vs Step 3). Cheap-Slow does not lower the model;
  the Eco-Budget *profile* does.
- **Treating Cheap-Slow as literal solo** — solo cost is quadratic in K.
  Cheap-Slow is low fan-out + small batches; literal solo is reserved for
  ≤3 hard-sequential items.
- Skipping or oversizing the shared context brief — it must be ≤800 tokens,
  synthesized once by the master, and must not replace per-item §2.{N} scope
  or relax worktree isolation.

## Shared-context dispatch (v1.29, #59)

When dispatching a batch of agents, minimize per-agent prompt size:

- Write the **shared brief once** — the design doc reference, the relevant
  standards, and the common acceptance criteria — as a compact preamble the batch
  shares. Do not paste the full context into every agent prompt.
- Give each agent only its **per-item delta**: its specific files, its branch, and
  its one acceptance criterion. Label by item, not by re-stating shared context.
- Result: materially smaller per-agent prompts with no loss of fidelity.
  Design rationale lives in the upstream Grimoire repository (framework-internal
  — not shipped).
