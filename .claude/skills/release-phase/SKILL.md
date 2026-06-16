---
name: release-phase
description: Spawn work-item sessions (via spawn_task) for the next open phase of the in-flight release. Groups work by dependency, sizes each item by token estimate, and assigns model/effort per the `repo-reference` skill table. Use when the user says "start phase N", "spawn the tasks", "kick off phase", "what do agents need to do for phase N", or "distribute phase work". Run after release-agreement has locked the plan.
---

# Release phase — spawn work-item sessions

Reads the agreed release plan, identifies the next open phase, groups its work
items into parallel batches, and uses the **`spawn_task`** tool to open a new
session in an isolated worktree for each item. The integration master never
hands the user raw copy-paste prompts — it spawns the sessions directly.

> **Preferred interface — the `grimoire-release` MCP server (v3.27).** Phase
> detection + conflict-map batch grouping are now deterministic. When
> `mcp.enabled` and the server is registered (root `.mcp.json`), call
> **`plan_phase`** to get `{phase, batches, model_assignments}` (first
> all-unticked pass → batches per §3 + a per-band model default) instead of
> recomputing it in-context; use **`get_ledger`** to read §5 rows. The model
> resolver below still owns the final tier (the tool's assignment is a coarse
> default). **CLI fallback** (no MCP / disabled): `python3
> .claude/skills/release-agent-tracker/release_plan.py plan-phase`. Design:
> `docs/design/grimoire-release-server-design.md`.

**`release-phase-model` dial.** The master reads `release-phase-model.value`
live before dispatching. When it is `Default` (or absent), dispatch the phase
via the `spawn_task` flow below. When it is **`Auto`** (Noir only — otherwise
fall back to `Default` and log the downgrade), dispatch the phase's items
instead via a **write-capable Workflow**, whose isolated-worktree agents each
implement one item and return a branch; the returned branches are then merged
in `mergeAfter` order by `release-phase-merge`. `Auto` reuses the existing
write-capable tier — no new machinery — and the execution variant still comes
from `workflow-variant`. See the integration-master §`release-phase-model` dial
and `docs/design/release-phase-model-design.md`.

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

## Step 2.5 — Choose the dispatch posture (execution-strategy)

Step 2 produced the *dependency-correct* batches. The **execution-strategy**
dial now sets the **dispatch posture** — fan-out width and isolation mode — over
those batches. This is independent of the tier resolved in Step 3 and the Noir
ceiling in Step 3a: see the orthogonality note below.

Read `workflow-variant.value` from `.claude/grimoire-config.json`
(absent/unset → `Efficient`; canonical preset set `{Fast, Efficient,
Cheap-Slow}`, matched case-insensitively; a legacy `Careful-Serial` value is
migrated to `Cheap-Slow` by `workflow-variant-switch` — treat any residual
`Careful-Serial` here as `Cheap-Slow`). Apply the posture:

| execution-strategy | Dispatch posture (fan-out + isolation) |
|---|---|
| **Fast** | **Max fan-out** — spawn every independent item in the current batch concurrently. Do not sub-split for shared reads; accept duplicated cold reads / reactive conflict handling. Minimum wall-clock. |
| **Efficient** | **Balanced — today's default behaviour (unchanged).** Keep Step 2's conflict-map–respecting batches as-is, dedup shared reads via the Step 5 shared-context brief, honour `mergeAfter` ordering. Existing projects see no change. |
| **Cheap-Slow** | **Low fan-out, small batches.** Cap concurrent spawns to a **small batch (≈2–3 items)**; if a Step 2 batch is wider, sub-split it into sequential small batches (merge each before spawning the next). Pairs with the Eco-Budget tier profile for genuine cost (per S1 the cost driver is tier + output, not fan-out width). NOT literal solo — see the regimes below. |

**Cheap-Slow regime selection** (per `execution-profiles-design.md` §C / §E,
evidence in `execution-profile-spike-s1.md`):

- **Many light / mechanical items** → low fan-out, small parallel batches,
  tiered down (Eco-Budget). Solo loses at every K (S1 Finding 1); small-batch
  parallel is the cheap path.
- **Few (≤ ~10) large / dependent items (the small-heavy corner)** →
  *target* is in-session subagents (**N1**) to avoid K cold seeds (~27K
  tokens/spawn, S1 isolation-overhead) without inheriting a giant solo prefix.
  **N1 is deferred**: until it lands, **fall back to small-batch `spawn_task`**
  (still cheaper than wide fan-out or solo for this K range). Leave the
  in-session path as a documented future call-site — do **not** implement
  in-session execution here.
- **≤ 3 hard-sequential items** (a true sequential dependency chain) →
  **literal solo-serial** is acceptable (the only regime where solo wins).
- **Many heavy items** → **parallel dispatch, NOT solo** (solo's cost is
  quadratic in K and inverts past the ~K=14 crossover; at K=50 solo is 2.3×
  parallel-heavy).

Record the chosen posture (and, for Cheap-Slow, the regime + whether you
sub-split) to surface in the Step 4 batch preview.

> **Three dials, three independent reads.** This step reads **only**
> `workflow-variant.value` (fan-out / isolation). Step 3 reads **only**
> `model-effort-profile.value` (tier). Step 3a reads **only**
> `work-paradigm.value` (the Noir autonomy ceiling). They **compose** and never
> derive one from another: execution-strategy sets *how wide / how isolated*,
> model-effort-profile sets *which tier*, work-paradigm sets *autonomy*. A
> Cheap-Slow + High-Effort + Supervised config is legal (narrow fan-out on a
> high tier); so is Fast + Eco-Budget + Noir (wide cheap fan-out, clamped to
> Sonnet). Do not let one dial change another's read.

---

## Step 3 — Assign model and effort

For each item, resolve `{model, effort}` through the **active model/effort
profile** — do not hard-code a table here. Use the single resolver documented in
the **`repo-reference`** skill (§Subagent model & effort → The resolver):

1. Read `model-effort-profile.value` from `.claude/grimoire-config.json`
   (absent/unset → `Medium`).
2. Classify the item into a complexity **band** from its recorded token estimate
   + design/review flag (trivial ≤ 15 K · small 15–40 K · medium 40–80 K · large
   > 80 K · review = any planning/review/architecture, regardless of estimate).
3. Look up `profiles[<active>][<band>]` in
   `.claude/model-effort-profiles.json` → the `{model, effort}` pair. UX-pin
   items (`design-language-adapt`, `ux-demo-build`) resolve to their fixed pins.

This `{model, effort}` pair is the recommended tier for the item. When in doubt,
err toward sonnet. `spawn_task` cannot set the spawned session's model, so the
resolved tier is carried into the chip (title + prompt) for the user to set when
opening the session.

### Step 3a — Noir dispatch ceiling (Noir only)

The profile resolver sets the **default** tier per band. Under the **Noir**
work paradigm a dispatch-time **ceiling** applies *on top of* the resolver, to
cut Opus fan-out cost on mechanical/implementation work (v1.9 audit recs
D2/A3/B3). It is a guardrail, not a re-tiering: it can only lower a tier, never
raise one.

Read `work-paradigm.value` from `.claude/grimoire-config.json`. **If it is not
`Noir`, skip this step entirely** — the resolver's tier is final.

When the paradigm **is** Noir, after the resolver yields `{model, effort}`,
cap the model at **Sonnet** for every item that is **neither a review item nor
`opus-required`-flagged**:

- **Review items** (band `review` — any planning/review/architecture/security
  analysis) are exempt: keep the resolver's tier (Opus stays Opus).
- **`opus-required` items** (see flag contract below) are exempt: keep the
  resolver's tier.
- **All other items** (trivial/small/medium/large implementation, mechanical):
  if the resolver returned `opus`, lower the model to `sonnet` and keep the
  resolver's `effort` (e.g. `opus/high` → `sonnet/high`). Items the resolver
  already put at Sonnet or Haiku are unchanged. UX-pin items keep their pin.

How it composes with the resolver: the active profile (e.g. the `Autonomous`
profile P2 installs for Noir projects) sets the **default tier per band**; the
Noir ceiling + `opus-required` flag are the **dispatch-time override** layered
after. The ceiling never reads the profile table — it only inspects the
already-resolved `{model, effort}` and the item's band/flag. Order is fixed:
resolve, then (Noir only) clamp.

### The `opus-required` escape hatch

A release plan may declare that a specific item needs Opus despite being
non-review work — the documented way to protect a quality-critical item from
the Noir ceiling.

**Flag contract:**

- **Where declared:** in `docs/release-planning-v{X.Y}.md`, on the item's
  §2.{N} entry and/or its §5 ledger row, as the literal token `opus-required`
  (e.g. an `opus-required: yes` field, or `opus-required` in the item's flags
  list). `release-planning` / `release-agreement` may set it when an item is
  scoped; the integration master honours it at dispatch.
- **Effect:** the item is exempt from the Step 3a ceiling — the resolver's
  tier stands as-is. It does **not** force-raise an item the resolver put
  below Opus (it is an exemption from the cap, not a promotion). To run a
  Sonnet-band item on Opus, the plan must size it into a higher band; the flag
  alone only prevents the Noir clamp from lowering an already-Opus tier.
- **Scope:** advisory only under Supervised/Weiss (no ceiling applies there, so
  the flag is a no-op); load-bearing only under Noir.
- **Audit:** when the ceiling lowers a tier, note it in the Step 4 batch list
  (e.g. `E5: opus/high → sonnet/high (Noir ceiling)`) so the user sees every
  clamp and can add `opus-required` to the plan if a clamp is wrong.

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

## Step 4 — Confirm before spawning (Supervised gate)

Before calling `spawn_task`, present the batch to the user:

- **Lead with the dispatch posture** (Step 2.5): the active execution-strategy
  and what it does to this batch — e.g. `Execution-strategy: Cheap-Slow → low
  fan-out, sub-split into 2 small batches (small-heavy corner; N1 in-session
  deferred → spawn fallback)`, or `Execution-strategy: Fast → max fan-out, all 6
  items concurrent`, or `Execution-strategy: Efficient → balanced (default)`. The
  user should see how wide the fan-out is and why before any spawn.
- List each item: ID, title, recommended model, branch name.
- Ask: "Spawn these N items now?"

Wait for explicit confirmation. Do not spawn until the user says yes.

---

## Step 5 — Spawn each item with `spawn_task`

For each item in the current batch, call the **`spawn_task`** tool
(`mcp__ccd_session__spawn_task`). The spawned session has no memory of this
session, so the `prompt` must be self-contained.

**First, synthesize the shared context brief (once per batch).** Before the
first `spawn_task` call, write a **≤800-token digest** of the background every
agent in this batch would otherwise read cold — release goal + phase theme, the
shared design overview (key decisions), the relevant §3 conflict-map rows, and
the standing constraints (test/build commands, worktree-root rule). Embed this
identical digest in every spawn prompt's `### Shared context (pre-digested)`
block so agents skip the cold re-read; it is a cache-hit lever and does **not**
relax worktree isolation. Full pattern + size/content rules:
`docs/integration-workflow.md` §Pre-digested context brief. Each agent still
reads its own §2.{N} scope in full.

- **title**: `[{model}/{effort}] {ITEM-ID}: {short title}` — lead with the
  resolved tier tag (the `{model, effort}` pair from Step 3's resolver, lowercase;
  `inherit` rendered literally, e.g. `[opus/high] E7: model/effort profiles`,
  `[sonnet/inherit] E3: output-minimization pass`). `spawn_task` cannot set the
  session's model, so the leading tag is the carrier; keep the "set this model/
  effort in your session" line in the prompt body as the instruction.
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

### Shared context (pre-digested)
{The ≤800-token batch digest synthesized above — release goal + phase theme,
shared design decisions, this batch's §3 conflict-map rows, standing
constraints. Identical across the batch. Read this first; it saves you the cold
re-read of the shared files. You still read your own §2.{N} scope below in full.}

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
- Run `cargo test` and `cargo build --release` before finishing.
- Fix all errors and warnings introduced by your changes.
- Review your own diff against the acceptance criteria before reporting done.

### When done
Do NOT merge. Report back:
1. The branch name you worked on
2. Test result (pass / N failures)
3. One-paragraph summary of what was implemented
4. Any deferred follow-ups discovered (gaps left for a future item)
```

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
- Forgetting the leading `[{model}/{effort}]` tier tag on the chip title —
  `spawn_task` can't set the model, so the tag is what makes the resolved tier
  reviewable at a glance and lets the user set it; the user can't size it for you.
- Forgetting to tell the spawned session to root on `version/{X.Y}` — a fresh
  worktree often lands on `main`'s tip.
- Spawning Batch 2 before Batch 1 is merged — agents will hit merge conflicts
  that are hard to resolve headlessly.
- Under Noir, dispatching non-review, non-`opus-required` implementation work
  to Opus — the Step 3a ceiling clamps it to Sonnet; only review items and
  `opus-required`-flagged items keep Opus.
- Treating `opus-required` as a promotion — it only exempts an already-Opus
  item from the Noir clamp; it never raises a sub-Opus item (re-band the plan
  for that).
- Skipping or oversizing the shared context brief — it must be ≤800 tokens,
  synthesized once by the master, and must not replace per-item §2.{N} scope or
  relax worktree isolation.
- Treating **Cheap-Slow as literal solo** — S1 refuted "cheap = one big
  session" (solo cost is quadratic in K). Cheap-Slow is low fan-out + small
  batches + Eco tiers; literal solo is reserved for ≤3 hard-sequential items.
- Letting **execution-strategy change the tier** (or vice versa) — they are
  independent reads (Step 2.5 vs Step 3). Cheap-Slow does not lower the model;
  the Eco-Budget *profile* does (it is the natural partner, not the same dial).
- **Implementing in-session subagents** for Cheap-Slow's small-heavy corner —
  N1 is deferred. Use the small-batch `spawn_task` fallback and leave the
  documented in-session call-site for when N1 lands.

## Shared-context dispatch (v1.29, #59)

When dispatching a batch of agents, minimize per-agent prompt size:

- Write the **shared brief once** — the design doc reference, the relevant
  standards, and the common acceptance criteria — as a compact preamble the batch
  shares. Do not paste the full context into every agent prompt.
- Give each agent only its **per-item delta**: its specific files, its branch, and
  its one acceptance criterion. Label by item, not by re-stating shared context.
- Result: materially smaller per-agent prompts with no loss of fidelity.
  Authority: `docs/design/context-efficiency-design.md`.
