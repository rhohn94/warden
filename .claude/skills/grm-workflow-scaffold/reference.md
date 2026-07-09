# workflow-scaffold — reference

Deep-dives, decision matrices, the measured authoring lessons, the safety-rail
spec, the two inline skeleton templates, and the anti-pattern catalog for the
`grm-workflow-scaffold` skill. The operational head (tier contract + Noir gate +
placement + post-scaffold registration) lives in `SKILL.md`; read this file when
you need a specific lesson, the variant table, or a template to copy. The
fully-worked reference is `.claude/workflows/release-planning.js`; the rationale
is `docs/grimoire/design/release-planning-workflow-design.md`; the write-capable tier
design is `docs/grimoire/design/write-capable-workflow-design.md`.

## When to use — and when NOT

**Use a read-only Workflow when** the step is:
- **Read-heavy** — it reads files/sources and produces analysis, not edits.
- **Fan-out-able** — independent reads that can run in parallel (e.g. several
  source files, several candidate items), ending in a structured synthesis.
- **Opt-in and billed** — the user has explicitly asked to mechanise/parallelise
  this step. Workflows are Claude-Code-only and cost real tokens.

**Use a write-capable Workflow (Noir only) when:**
- The step produces file edits / commits that are **naturally parallel** (multiple
  independent work items that can run concurrently in isolated worktrees).
- The project is **actively running the Noir paradigm** (check
  `grimoire-config.json`).
- The integration master (not the Workflow itself) will own all merges into the
  staging ref via `grm-release-phase-merge`.

**Do NOT use a Workflow for:**
- **Write-heavy work under Supervised or Weiss.** Anything that edits files,
  commits, or creates branches under those paradigms uses `spawn_task`
  (worktree-isolated, interactive) instead.
- **A single-context read** — if one agent reading a couple of files suffices,
  just do it inline or with a single `Agent`. Fan-out overhead isn't worth it.
- **Direct staging-branch commits from agents.** Write-capable agents commit on
  their own per-agent branch, never on `dev`, `main`, or `version/*`. The master
  merges.
- **Pushing to origin.** No Workflow or agent pushes — push is the human
  operator's gate, even under Noir.
- **The Copilot flavor** — `Workflow` is a Claude-Code-only primitive; never
  mirror `.claude/workflows/` into `copilot/`.

The full decision matrix (`Workflow` vs. skill vs. `spawn_task` vs. `Agent`)
lives in the design doc's **§Workflow vs. skill vs. spawn_task vs. Agent** —
read it before scaffolding.

---

## Three execution variants

Every write-capable Workflow (and optionally read-only ones) exposes three named
execution variants. The caller selects the variant at invocation time:

```js
Workflow({ name: 'example-write-capable', args: { variant: 'Careful-Serial' } })
```

If no variant is passed the Workflow defaults to `Efficient`.

| Variant | Parallelism | Focus | When to choose |
|---------|------------|-------|---------------|
| **Efficient** | Parallel | Low wasted / repeated work | Default; most releases; overlapping file dependencies |
| **Fast** | Parallel | Minimum wall-clock time | Time-critical runs; independent items; cost is not a concern |
| **Careful-Serial** | Serial (one at a time) | Maximum control; minimum collision risk | Risky changes; highly entangled items; debugging a workflow |

**Efficient (parallel, low-waste):** Agents fan out in parallel, but the Workflow
batches shared reads, deduplicates overlapping file access, and respects the
conflict map to avoid agents touching the same file concurrently. An agent waits
on its `mergeAfter` dependencies before the master merges it.

**Fast (parallel, minimal time):** Maximum fan-out — all agents launch concurrently
regardless of file overlap. Duplicated reads are accepted. Conflict resolution is
expected and handled reactively by the master. Suitable when work items are
genuinely independent and the caller values speed over token efficiency.

**Careful-Serial (not parallel):** Agents execute one at a time in conflict-map
order. Each agent's branch is merged by the master before the next agent starts.
`maxConcurrency: 1` in the phase configuration. Lowest conflict risk; highest
control; wall-clock latency equals the sum of all agents.

### Encoding variant selection in scripts

Emit a `selectVariant(args)` helper and separate phase definitions per variant:

```js
function selectVariant(args) {
  const v = args?.variant ?? 'Efficient'
  if (!['Efficient', 'Fast', 'Careful-Serial'].includes(v)) {
    throw new Error(`Unknown variant "${v}". Choose Efficient | Fast | Careful-Serial.`)
  }
  return v
}

// In the write phase:
const variant = selectVariant(args)
const concurrency = variant === 'Careful-Serial' ? 1 : items.length

const branches = (await parallel(
  items.map((it) => () => agent(/* … per-item prompt … */, {
    label: `item:${it.slug}`,
    isolation: 'worktree',
    phase: 'Execute',
  })),
  { maxConcurrency: concurrency }
)).map((result, i) => reattachByIndex(result, items[i]))
```

The `active workflow-variant.value` from `grimoire-config.json` may override the
default when that field is activated (future release).

---

## Authoring guidance — the measured lessons (read-only tier)

These are not style preferences; they are A/B-measured cost levers (v1 ~$14.5 →
v4 ~$1.7, ~88% cheaper). Encode them in every workflow you scaffold.

1. **Model tiering is the dominant cost lever.** Every agent pays a fixed
   ~45K-token context cost on entry, so token *volume* is nearly flat across
   tiers while the *rate* is not (opus ≈ 5× sonnet ≈ 15× haiku). Assign the
   cheapest tier that can do the job:
   - **haiku** — mechanical reads / extraction (no judgement).
   - **sonnet** — sizing, classification, calibrated judgement.
   - **session model** (omit `model`) — final user-facing synthesis only; its
     output is small, so there's nothing to gain by downgrading it.

   This was the single biggest measured saving — see the design doc's
   **Finding 1**. (`agent()` exposes `model` but not `effort`; model tier is the
   only cost knob inside a script.)

2. **Batch shared reads; fan out only past a threshold.** When several items
   read the same files, size them in ONE batched agent that reads each shared
   file once — don't spawn one agent per item, because each fan-out agent
   re-reads the overlapping files AND re-pays the ~45K overhead. Fan out only
   when the item count exceeds a threshold (the worked example uses
   `SIZE_FANOUT_THRESHOLD = 8`), where enough independent items make parallel
   wall-clock worth the duplicated reads. See the design doc's **Finding 2**.

3. **Tell each agent to read its named files in a single step.** Name the exact
   files and instruct "read these in one step; do not explore further." Fewer
   turns per agent means less cache-read churn — the dominant token volume after
   the fixed-context cost.

4. **Prefer structured-output schemas over free-text parsing.** Give each agent
   a `schema` (the worked example's `*_SCHEMA` objects); validation happens at
   the tool-call layer and forces a retry on mismatch, so synthesis receives
   clean data instead of parsing prose. **When matching results back to inputs,
   match POSITIONALLY by index with a count check — never by a paraphrasable
   name.** Name-matching silently drops items when an agent paraphrases the name
   (the W3 hardening lesson); `parallel()` preserves order, so align by index and
   turn any missing slot into a visible "unsized"/"unmatched" row rather than a
   silent `.filter(Boolean)` drop.

5. **Minimize generated output — it's the most expensive token class, worst on
   Opus** (v1.9 lever 1; see `docs/grimoire/design/token-efficiency-design.md`). Output is
   the one volume an agent fully controls, and the v1.9 baseline measured the
   workflow synthesis/orchestrator path as the single costliest operation. Apply,
   in impact order:
   - **Diffs / IDs over full rewrites.** If a step's job is to change or report
     on something, return the delta (a patch, a branch + SHA, a row id) — never
     regenerate a whole document or recap a diff in prose. A write-capable agent
     returns `{branch, commit, summary}`, not a narration of what it did.
   - **Schemas cap generation.** Every fan-out agent gets a `schema` (lever 4
     above) — this also bounds output, not just parsing cleanliness. Constrain
     even implementation-agent returns to a terse result schema.
   - **Forbid echoing inputs.** Tell synthesis agents to emit ONLY the deliverable
     sections and never restate the JSON they were handed or narrate their steps.
   - **Tier *down* the output-heavy step.** The synthesis/assembly step is often
     mechanical template-fill of already-structured data (no judgement) — tier it
     to **sonnet**, not the session model. Output volume is unchanged but the
     ~5× Opus rate is exactly where the tier multiplier bites hardest. (The
     worked `release-planning.js` synthesizer is tiered to sonnet for this reason;
     leave a step at the session model only if it genuinely needs Opus judgement.)

6. **Keep read-only workflows truly read-only.** No file writes, no branch ops,
   no commits. This is what keeps a read-only workflow clear of the
   worktree-isolation and protected-branch hooks — it runs in the master's shared
   session, outside any worktree boundary. Return the result to the master, who
   takes any file-writing next step through the normal skills. See the design
   doc's **§Read-only safety contract**.

---

## Safety rails for write-capable workflows

Write-capable Workflow agents operate under these invariants (from
`docs/grimoire/design/write-capable-workflow-design.md §3`):

1. **Agents never push.** No agent calls `git push`. Push is exclusively the
   human operator's action. `push-guard.sh` enforces this at the tool level.

2. **Agents never touch dev / main / version/\*.** Agents commit only on their
   own per-agent branch (see branch naming below). `protected-branch-guard.sh`
   blocks any commit/merge/rebase on protected refs from within an agent worktree
   (no `integration-allow.local` marker present).

3. **Agents stay inside their own worktree.** Each agent's `CLAUDE_PROJECT_DIR`
   is set to its isolated worktree path. `worktree-guard.sh` blocks paths
   resolving outside the active worktree root.

4. **The master owns all merges.** Only the integration master (the marker-blessed
   worktree) merges agent branches into the staging ref. Agents cannot merge
   because they lack the marker.

5. **Push stays human.** Even under Noir, pushing to `origin` requires an
   explicit human action. This is a hard boundary for v1.6.

### Model tiering — default Execute agents to Sonnet

Write-capable Execute (implementation) agents **default to `model: 'sonnet'`**,
not the session model. Under Noir the session model is Opus, and an Execute agent
inheriting it pays the ~5× Opus rate (Opus ≈ 5× Sonnet) **multiplied by the
fan-out width** — the most expensive tier in any workflow. Most implementation is
mechanical, where Sonnet is the workhorse with no quality loss (v1.9 audit rec D2;
[`docs/grimoire/token-efficiency-audit.md`](../../../docs/grimoire/token-efficiency-audit.md) §D2).

Provide an **`item.hard`** escape hatch on each plan item: `model: it.hard ?
'opus' : 'sonnet'`. The Orient agent (or caller) sets `hard: true` only for
genuinely hard items needing Opus-level judgement (intricate algorithms, deep
cross-module reasoning); it defaults to `false`. **Never `haiku`** for write-capable
agents — too weak, risks rework that costs more than it saves. Set `hard: true`
sparingly: it forfeits the 5× saving for that item.

### Branch naming convention

Per-agent branches follow the work-item naming convention from `grm-release-phase`:

```
<item-slug>-<short-uuid>
```

Examples: `update-config-parser-a3f1`, `add-retry-logic-b7c2`.

The `short-uuid` suffix prevents collisions when the same item slug is reused
across runs. Generate and record branch names before spawning agents so the
master can reference them in the merge sequence.

### Conflict map and master handoff

The Workflow script must emit a **merge order** (conflict map) alongside the
branch list:

```js
return {
  branches: [
    { branch: 'update-config-parser-a3f1', mergeAfter: [] },
    { branch: 'add-retry-logic-b7c2',      mergeAfter: ['update-config-parser-a3f1'] },
  ]
}
```

The integration master follows `mergeAfter` dependencies when calling
`grm-release-phase-merge`. On unresolvable conflict, the master surfaces a summary
to the user and pauses for resolution. See
`docs/grimoire/design/write-capable-workflow-design.md §2.3`.

---

## Minimal template — read-only tier

A tiered Orient→Gather→Size→Synthesize skeleton. Keep the inline version small —
copy the real structure (schemas, adaptive batch/fan-out, reattach-by-index)
from `.claude/workflows/release-planning.js`.

```js
export const meta = {
  name: '{name}',
  tier: 'read-only',   // default; safe for all paradigms
  description: 'One line: what read-heavy step this mechanises and what it returns.',
  whenToUse: 'When to reach for this workflow over the equivalent skill.',
  phases: [
    { title: 'Orient',     detail: 'resolve scope; read shared context once', model: 'haiku' },
    { title: 'Gather',     detail: 'parallel readers (haiku; judgement readers sonnet)', model: 'haiku' },
    { title: 'Size',       detail: 'sonnet; batched by default, fan out past threshold', model: 'sonnet' },
    { title: 'Synthesize', detail: 'assemble the deliverable (inherits session model)' },
  ],
}

// Structured schema → clean data, no free-text parsing.
const ITEM_SCHEMA = { type: 'object', additionalProperties: false,
  required: ['name', 'finding'],
  properties: { name: { type: 'string' }, finding: { type: 'string' } } }

phase('Orient')                                    // haiku: mechanical lookup, read named files once
const orient = await agent('Resolve scope; read <files> ONCE, do not explore.',
  { label: 'orient', model: 'haiku', schema: /* ORIENT_SCHEMA */ undefined })

phase('Gather')                                    // barrier: synthesis needs all readers
const [a, b] = await parallel([
  () => agent('Read <fileA> in one step; extract …', { label: 'read:a', model: 'haiku', phase: 'Gather', schema: ITEM_SCHEMA }),
  () => agent('Read <fileB> in one step; classify …', { label: 'read:b', model: 'sonnet', phase: 'Gather', schema: ITEM_SCHEMA }),
])

phase('Size')                                      // batch shared reads; fan out only past threshold
const FANOUT_THRESHOLD = 8
const items = [/* assembled in plain code, no agent */]
let sized
if (items.length > FANOUT_THRESHOLD) {
  sized = (await parallel(items.map((it) => () =>
    agent(`Size "${it.name}" …`, { label: `size:${it.name}`.slice(0, 60), model: 'sonnet', phase: 'Size', schema: ITEM_SCHEMA }))
  )).map((s, i) => reattachByIndex(s, items[i]))   // match POSITIONALLY, never by name
} else {
  const batch = await agent(`Size EACH item; read shared files ONCE. Return EXACTLY one entry per item, SAME ORDER.\n${items.map((it, i) => `${i + 1}. ${it.name}`).join('\n')}`,
    { label: 'size:batch', model: 'sonnet', phase: 'Size', schema: /* BATCH_SCHEMA */ undefined })
  sized = items.map((it, i) => reattachByIndex((batch?.items || [])[i], it))  // count-check + flag, don't drop
}

phase('Synthesize')                                // tier-down: mechanical template-fill → sonnet
const report = await agent(`Assemble the deliverable from the data below. Emit ONLY the report sections; do NOT echo the input JSON or narrate. ${JSON.stringify({ orient, a, b, sized })}`,
  { label: 'synthesize', model: 'sonnet' })        // output-heavy + no judgement → sonnet, not Opus
return report   // read-only: hand back to the master; no file writes, no branches
```

---

## Minimal template — write-capable tier (Noir only)

A tiered Orient→Plan→Execute→Report skeleton with isolated-worktree agents,
variant selection, and conflict-map output. Requires Noir paradigm.

```js
export const meta = {
  name: '{name}',
  tier: 'write-capable',   // Noir only — fails fast under Supervised / Weiss
  description: 'One line: what parallel-commit step this mechanises.',
  whenToUse: 'When items are independently implementable and Noir is active.',
  phases: [
    { title: 'Orient',  detail: 'resolve work-item list and conflict map', model: 'haiku' },
    { title: 'Execute', detail: 'parallel per-item agents in isolated worktrees (variant-aware); default Sonnet, item.hard → Opus', model: 'sonnet' },
    { title: 'Report',  detail: 'return branch list + merge order to master' },
  ],
}

// Noir gate — fail closed if paradigm is wrong
if (meta.tier === 'write-capable' && activeParadigm() !== 'Noir') {
  throw new Error(
    'write-capable workflows require the Noir paradigm. ' +
    'Switch paradigm or use a read-only workflow.'
  );
}

function selectVariant(args) {
  const v = args?.variant ?? 'Efficient'
  if (!['Efficient', 'Fast', 'Careful-Serial'].includes(v)) {
    throw new Error(`Unknown variant "${v}". Choose Efficient | Fast | Careful-Serial.`)
  }
  return v
}

const BRANCH_SCHEMA = { type: 'object', additionalProperties: false,
  required: ['branch', 'mergeAfter'],
  properties: {
    branch:     { type: 'string' },
    mergeAfter: { type: 'array', items: { type: 'string' } },
    hard:       { type: 'boolean' },   // optional: true → Execute agent runs on Opus (default Sonnet)
  } }

phase('Orient')                                        // haiku: resolve items + conflict map
const plan = await agent('Read <files> ONCE; resolve work items + their dependencies.',
  { label: 'orient', model: 'haiku', schema: /* PLAN_SCHEMA */ undefined })

phase('Execute')                                       // variant-aware fan-out
const variant  = selectVariant(args)
const concurrency = variant === 'Careful-Serial' ? 1 : plan.items.length

const results = (await parallel(
  plan.items.map((it) => () =>
    agent(
      `Implement work item "${it.slug}". Branch: ${it.branch}.\n` +
      `1. git switch -c ${it.branch} version/{X.Y}\n` +
      `2. Implement the item.\n` +
      `3. git add -p && git commit.\n` +
      `4. Do NOT push, merge, or touch dev/main/version/*.`,
      {
        label:     `item:${it.slug}`.slice(0, 60),
        isolation: 'worktree',     // harness creates a fresh git worktree
        phase:     'Execute',
        model:     it.hard ? 'opus' : 'sonnet',   // default Sonnet; item.hard → Opus. Never haiku for writes.
      }
    )
  ),
  { maxConcurrency: concurrency }
)).map((r, i) => reattachByIndex(r, plan.items[i]))

phase('Report')                                        // hand branch list to master
return {
  branches: plan.items.map((it, i) => ({
    branch:     it.branch,
    mergeAfter: it.mergeAfter ?? [],
    result:     results[i],
  }))
  // Master calls release-phase-merge with this list, following mergeAfter order.
}
```

---

## Anti-patterns

* **Write-capable workflow under Supervised or Weiss.** The Noir gate throws
  immediately — by design. Use `spawn_task` for write-heavy work in those
  paradigms.
* **Agents committing directly on dev / main / version/\*.** Write-capable agents
  commit only on their own per-agent branch. `protected-branch-guard.sh` blocks
  attempts to commit on protected refs from worktrees without the integration
  marker.
* **Agents pushing.** No Workflow agent pushes. `push-guard.sh` enforces this.
  Push is a human-gated action.
* **Agents merging their own branches.** Only the integration master (marker-
  blessed worktree) merges agent branches into the staging ref. Agents must not
  call `git merge` targeting `dev`, `main`, or `version/*`.
* **All-opus agents.** The cost blowout the cost model exists to prevent
  (~8× the shipped config). Tier every agent down to the cheapest tier that
  works.
* **Write-capable Execute agents left on the session model (Opus).** Under Noir
  the session model is Opus; an Execute agent inheriting it pays ~5× Sonnet × the
  fan-out width. Default Execute to `model: 'sonnet'` and gate Opus behind the
  `item.hard` flag (v1.9 audit rec D2).
* **Free-text parsing instead of schemas.** Brittle and forces the synthesis
  agent to clean prose. Use `schema`.
* **Output-heavy synthesis left on Opus.** A step that regenerates a whole
  document from already-structured data is mechanical template-fill — tier it to
  sonnet. Output is the costliest token class and worst on Opus (v1.9 lever 1);
  leaving the heaviest emitter at the session model wastes the ~5× multiplier.
* **Regenerating whole documents / recapping diffs in prose.** Return the delta —
  a patch, a branch + SHA, a flagged row — not a full rewrite or a narration of
  what changed. Tell synthesis agents not to echo the input data back.
* **Name-based result matching.** A paraphrased name silently drops the item.
  Match positionally by index with a count check.
* **Fanning out below the batch threshold (read-only).** Re-reads shared files
  and re-pays per-agent overhead N times. Batch first; fan out only past the
  threshold.
* **Forgetting the read-only contract for read-only workflows.** No writes, no
  branches, no commits — return the result and let the master take the next step
  through the skills.
* **Skipping registration.** An un-manifested, un-goldened workflow isn't
  restorable — it'll vanish on the next `grm-workflow-bootstrap` restore.
* **Omitting the Noir gate in write-capable scripts.** Without the explicit
  `activeParadigm() !== 'Noir'` check, a write-capable workflow silently runs
  under the wrong paradigm instead of failing fast.
* **Skipping the conflict map / mergeAfter output.** The integration master needs
  the merge order to call `grm-release-phase-merge` correctly. Always emit
  `mergeAfter` for every branch, even if it's `[]`.
