# grm-release-phase — reference detail

> **Load on demand.** This file contains the deep detail for sections called out
> in the SKILL.md head. Load the relevant section when you need it; the lean
> head is sufficient for most dispatches.

---

## §Step 2.5 — Execution-strategy deep detail

### Cheap-Slow regime selection

Per `execution-profiles-design.md` §C / §E and evidence in
`execution-profile-spike-s1.md`:

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
sub-split) in the Step 4 batch preview.

### Three-dial orthogonality

**Three dials, three independent reads.** Step 2.5 reads **only**
`workflow-variant.value` (fan-out / isolation). Step 3 reads **only**
`model-effort-profile.value` (tier). Step 3a reads **only**
`work-paradigm.value` (the Noir autonomy ceiling). They **compose** and never
derive one from another: execution-strategy sets *how wide / how isolated*,
model-effort-profile sets *which tier*, work-paradigm sets *autonomy*. A
Cheap-Slow + High-Effort + Supervised config is legal (narrow fan-out on a
high tier); so is Fast + Eco-Budget + Noir (wide cheap fan-out, clamped to
Sonnet). Do not let one dial change another's read.

---

## §Step 3a — Noir dispatch ceiling + `opus-required` escape hatch

### Ceiling rule

The profile resolver sets the **default** tier per band. Under the **Noir**
work paradigm a dispatch-time **ceiling** applies *on top of* the resolver, to
cut Opus fan-out cost on mechanical/implementation work (v1.9 audit recs
D2/A3/B3). It is a guardrail, not a re-tiering: it can only lower a tier, never
raise one.

Read `work-paradigm.value` from `.claude/grimoire-config.json`. **If it is not
`Noir`, skip this section entirely** — the resolver's tier is final.

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

### The `opus-required` flag contract

A release plan may declare that a specific item needs Opus despite being
non-review work — the documented way to protect a quality-critical item from
the Noir ceiling.

- **Where declared:** in `docs/release-planning/release-planning-v{X.Y}.md`, on
  the item's §2.{N} entry and/or its §5 ledger row, as the literal token
  `opus-required` (e.g. an `opus-required: yes` field, or `opus-required` in the
  item's flags list). `grm-release-planning` / `grm-release-agreement` may set it
  when an item is scoped; the integration master honours it at dispatch.
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

## §Step 3.5 — Milestone label gate (full code examples)

```bash
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --state open \
  | grep -v "milestone:v{X.Y}"
```

Or inspect each planned issue's labels via the tracker abstraction:

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

**Error output when gate fails:**

```
ERROR: Milestone gate failed — the following issues lack a milestone:vX.Y label
and cannot be dispatched:

  - #{id}: {title}
  - #{id}: {title}

Action required: run the Triager with milestone-assignment scope to label these
issues before re-running release-phase.
```

This is a **hard gate**, not advisory. The dispatch does not proceed until all
planned issues are labeled. An issue carrying `milestone:backlog` is also
blocked — backlog items must not be dispatched in a release phase for vX.Y.

---

## §Anti-patterns (full detail)

- **Under Supervised/Weiss, spawning without user confirmation** — the
  Supervised gate always asks first.
- **Under Supervised/Weiss, handing the user raw copy-paste prompts** instead
  of calling `spawn_task` — the integration master spawns the sessions
  directly.
- **Including merge instructions in a spawned prompt** — work-item agents never
  merge; only the integration master merges via `grm-release-phase-merge`.
- **Batching items that share files** — check §3's conflict map carefully before
  placing items in the same batch.
- **Forgetting the leading `[{model}/{effort}]` tier tag** on the chip title —
  `spawn_task` can't set the model, so the tag is what makes the resolved tier
  reviewable at a glance and lets the user set it; the user can't size it for you.
- **Skipping or oversizing the shared context brief** — it must be ≤800 tokens,
  synthesized once by the master, and must contain standards / conflict-map /
  criteria / doc-pointers; it must **not** replace per-item §2.{N} scope or
  relax worktree isolation.
- **Spawning Batch 2 before Batch 1 is merged** — agents will hit merge
  conflicts that are hard to resolve headlessly.
- **Under Noir, dispatching non-review, non-`opus-required` implementation work
  to Opus** — the Step 3a ceiling clamps it to Sonnet; only review items and
  `opus-required`-flagged items keep Opus.
- **Treating `opus-required` as a promotion** — it only exempts an already-Opus
  item from the Noir clamp; it never raises a sub-Opus item (re-band the plan
  to promote an item to Opus).
- **Treating Cheap-Slow as literal solo** — S1 refuted "cheap = one big
  session" (solo cost is quadratic in K). Cheap-Slow is low fan-out + small
  batches + Eco tiers; literal solo is reserved for ≤3 hard-sequential items.
- **Letting execution-strategy change the tier** (or vice versa) — they are
  independent reads (Step 2.5 vs Step 3). Cheap-Slow does not lower the model;
  the Eco-Budget *profile* does (it is the natural partner, not the same dial).
- **Implementing in-session subagents** for Cheap-Slow's small-heavy corner —
  N1 is deferred. Use the small-batch `spawn_task` fallback and leave the
  documented in-session call-site for when N1 lands.

---

## §Shared-context dispatch (v1.29, #59)

When dispatching a batch of agents, minimize per-agent prompt size:

- Write the **shared brief once** — the design doc reference, the relevant
  standards, and the common acceptance criteria — as a compact preamble the
  batch shares. Do not paste the full context into every agent prompt.
- Give each agent only its **per-item delta**: its specific files, its branch,
  and its one acceptance criterion. Label by item, not by re-stating shared context.
- Result: materially smaller per-agent prompts with no loss of fidelity.
  Authority: `docs/design/context-efficiency-design.md`.
