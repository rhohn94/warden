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
- **Inlining the shared brief or an item's plan prose into a prompt** instead
  of writing/committing `context_pack.py`'s brief + pack files and pointing
  the prompt at them (#397) — see §Shared-context dispatch below. The brief
  must still contain standards / conflict-map / theme; it must **not**
  replace an item's own pack (its verbatim §2.{N} block) or relax worktree
  isolation.
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

## §Shared-context dispatch (v1.29 #59, brief-as-file v3.96 #397)

When dispatching a batch of agents, minimize per-agent prompt size — and,
since v3.96, minimize the **master's own output tokens** spent regenerating
that content once per dispatched agent.

### Why prompt text, not just prompt count, was the problem

The pre-#397 mechanism had the master **synthesize a ≤800-token digest once,
then paste it verbatim into every spawn prompt's `### Shared context
(pre-digested)` block**, and separately **copy each item's plan description +
acceptance criteria into that item's `### Work` block**. "Synthesized once"
only meant the master composed the text once in its own reasoning; the text
still had to be re-emitted as literal output tokens on every one of the N
`spawn_task` calls in the batch, and the per-item copy duplicated content that
a `### Context — read before touching code` bullet was *already* pointing the
agent at (`docs/release-planning/release-planning-v{X.Y}.md §2.{N}`). For an
N-item batch this cost scaled with N, not with 1.

### The brief-as-file fix (#397)

`context_pack.py` (`.claude/skills/grm-release-phase/context_pack.py`)
materializes both pieces as small, **git-tracked** files instead of prompt
text:

- `phase-brief` — writes `.claude/release-dispatch/v{X.Y}/phase{N}/brief.md`:
  the standards excerpt, the plan's full `## 3. Parallel Implementation
  Strategy` section (verbatim, via `grm-doc-section`'s `extract_section()` —
  never re-typed), and the release theme. Written **once per batch**
  regardless of N.
- `context-pack` — writes one
  `.claude/release-dispatch/v{X.Y}/phase{N}/{ITEM-ID}.md` per item: that
  item's exact `### {ITEM-ID}` block (description, acceptance criteria,
  branch), extracted verbatim from the plan. Written once per item, never
  copied into a prompt.

Every dispatched prompt then carries only a short pointer block (see the Step
5 template's `### Shared context` / `### Your item context`) — a path
reference, not the content itself.

**Why these must be tracked (committed) files, not `.claude/cache/`.** Each
dispatched agent runs in a genuinely separate `git worktree` — a distinct
working directory sharing only the `.git` object store with the master's
worktree. An untracked/gitignored file the master writes (e.g. under
`.claude/cache/`, which is globally gitignored) is invisible in a freshly
spawned sibling worktree; nothing copies working-directory content across
worktrees. Committing the brief + packs to `version/{X.Y}` — the master's own
current branch, per `grm-integration-master` SKILL.md — solves this for free:
every dispatched agent's `git switch -c {branch} version/{X.Y}` (Step 5's
template, first thing the agent does) inherits the committed tree, packs
included, with zero extra prompt tokens and zero cold read of the full plan
file.

**Lifecycle.** The master commits the brief + that batch's packs right before
Step 5's `spawn_task` calls. `grm-release-phase-merge` is expected to `git rm
-r .claude/release-dispatch/v{X.Y}/phase{N}/` once every branch in that
phase's batch has merged, so the tracked-but-transient files don't
accumulate forever on `version/{X.Y}` / `dev` (not yet wired as of #397 — a
follow-up on `grm-release-phase-merge`'s own SKILL.md).

**Result:** on a real 4-item batch (v3.96 Pass 2: ITEM-6/7/8/9), the old
inlined-per-prompt model cost ~11.4 KB (~2.85K tokens) of repeated prompt
text across the batch; the brief-as-file model costs ~1.5 KB (~386 tokens) of
pointer text across the same batch (the ~5.7 KB / ~1.4K-token brief+packs
content is paid once, via file write, not per prompt) — an ~86% reduction in
what the master re-emits per batch. Reproduce via `context_pack.py measure`.
Authority: `docs/design/context-efficiency-design.md`.
