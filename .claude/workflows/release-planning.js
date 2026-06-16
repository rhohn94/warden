export const meta = {
  name: 'release-planning',
  description: 'Fan-out version of the release-planning skill: parallel source readers + per-item token sizing → a work-items report draft for the next release.',
  whenToUse: 'When planning the next release (vX.Y) and you want broad, parallel coverage of roadmap + carryovers + design docs and an independently-sized item list, rather than one context reading everything serially. Produces the same report shape as the release-planning skill; still a planning input, not a committed plan.',
  phases: [
    { title: 'Orient', detail: 'one haiku agent: resolve released/in-flight/target versions AND read version-history once for velocity', model: 'haiku' },
    { title: 'Gather', detail: 'parallel readers (all haiku): roadmap, carryovers, design docs, Grimoire-Requirement tracker issues', model: 'haiku' },
    { title: 'Size', detail: 'sonnet; all items sized in one agent (shared reads), fans out per item only past the threshold', model: 'sonnet' },
    { title: 'Synthesize', detail: 'assemble the work-items report in house format (inherits session model)' },
  ],
}

// ---------------------------------------------------------------------------
// This workflow mirrors .claude/skills/release-planning/SKILL.md. The skill is
// the authoritative description of WHAT a release plan must contain; this
// script is one mechanised WAY to produce the report's first draft by fanning
// the read-heavy steps across subagents. It is read-only: it writes no files
// and creates no branches. Hand the returned markdown back to the user for
// iteration, then proceed to design-doc-scaffold / release-agreement exactly
// as the skill prescribes.
//
// COST MODEL (measured by A/B on this repo, v1.4 plan; ratios robust, $ approx):
//
//   v1  all-opus, fan-out sizing ............... ~$14.5   baseline
//   v2  tiered models, batched sizing .......... ~$2.3    (~84% cheaper)
//   v3  tiered models, fan-out sizing .......... ~$3.4    (~76% cheaper)
//   v4  tiered + batched + orient/velocity merge
//       + read-in-one-step (THIS config) ....... ~$1.7    (~88% cheaper)
//
// Two findings drove the design:
//  1. MODEL TIER is the dominant lever. Per-agent fixed context (~45K of system
//     prompt + tool schemas) dwarfs each agent's ~1K output, so token VOLUME is
//     ~flat across tiers while the rate is not (opus ≈ 5× sonnet ≈ 15× haiku).
//     Hence haiku for mechanical orient + readers, sonnet for sizing, the
//     session model for synthesis. (`agent()` exposes `model` but not `effort`,
//     so model tier is the only cost knob inside a workflow.)
//  2. BATCHED sizing beats fan-out (v2 < v3, same tiers). Fan-out sizers each
//     RE-READ the same overlapping design docs and each pay full per-agent
//     overhead; one batched sizer reads the shared files once. So we batch by
//     default and fan out only past SIZE_FANOUT_THRESHOLD, where many genuinely
//     independent items make parallel wall-clock worth the duplicated reads.
//
// Further token trims applied here: (a) orient also does the velocity read, so
// version-history.md is read once instead of twice; (b) every agent is told to
// read its named files in a single step and not explore — fewer turns means
// less cache-read churn, which is the dominant token volume.
//
// v1.9 (E3) OUTPUT-TOKEN trim: the Synthesize step — this workflow's heaviest
// output emitter — was tiered Opus→sonnet (mechanical template-fill of already-
// structured JSON, no judgement) and told not to echo its input JSON. Output is
// the most expensive token class and worst on Opus; the synthesizer is exactly
// the "tier-down an output-heavy step" case. See docs/token-efficiency-baseline.md
// (the orchestrator/synthesis path was the costliest single operation measured).
//
// Invoke:  Workflow({ name: 'release-planning' })
//          Workflow({ name: 'release-planning', args: { target: '1.4' } })
//          Workflow({ name: 'release-planning', args: '1.4' })
//          Workflow({ name: 'release-planning', args: { target: '1.4', sizeFanoutThreshold: 12 } })
// ---------------------------------------------------------------------------

// Orient also carries velocity (both derive from version-history.md — read once).
const ORIENT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['released', 'inFlight', 'target', 'notes', 'recent', 'velocityNote'],
  properties: {
    released: { type: 'string', description: 'Current released version, e.g. "1.2". "none" if no release yet.' },
    inFlight: { type: 'string', description: 'Highest release-planning-v*.md without a version-history entry, e.g. "1.3".' },
    target: { type: 'string', description: 'One MINOR bump beyond in-flight, e.g. "1.4".' },
    notes: { type: 'string', description: 'How each version value was determined; flag any ambiguity for the user.' },
    recent: {
      type: 'array',
      description: 'Last 2-3 versions from version-history.md, to calibrate scope/velocity. Empty if the file does not exist yet.',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['version', 'itemCount', 'scope'],
        properties: {
          version: { type: 'string' },
          itemCount: { type: 'number' },
          scope: { type: 'string', description: 'One line on what that release contained.' },
        },
      },
    },
    velocityNote: { type: 'string', description: 'Calibration takeaway: is the proposed target in line with recent velocity?' },
  },
}

const ROADMAP_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['theme', 'nonGoals', 'items'],
  properties: {
    theme: { type: 'string', description: 'Flagship theme paragraph: name + problem it solves.' },
    nonGoals: { type: 'array', items: { type: 'string' }, description: 'Explicit non-goals from the roadmap entry.' },
    items: {
      type: 'array',
      description: 'Flagship sub-items (decompose the flagship if the roadmap lists deliverables).',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['name', 'summary'],
        properties: {
          name: { type: 'string' },
          summary: { type: 'string', description: 'One line: what the item delivers.' },
        },
      },
    },
  },
}

const CARRYOVER_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['committed', 'candidates'],
  properties: {
    committed: {
      type: 'array',
      description: 'Items the in-flight plan §4 tags vX.Y+/later — committed rollovers, include all.',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['name', 'summary', 'origin'],
        properties: {
          name: { type: 'string' },
          summary: { type: 'string' },
          origin: { type: 'string', description: 'Where it came from, e.g. "v1.3 §4 Out of Scope".' },
        },
      },
    },
    candidates: {
      type: 'array',
      description: 'Pass follow-ups (§5) not clearly tagged for the target — surface for the user to decide.',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['name', 'summary', 'note'],
        properties: {
          name: { type: 'string' },
          summary: { type: 'string' },
          note: { type: 'string', description: 'Why it is a candidate rather than committed.' },
        },
      },
    },
  },
}

const DESIGN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['docs'],
  properties: {
    docs: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['document', 'status', 'needed'],
        properties: {
          document: { type: 'string', description: 'Path under docs/design/, e.g. "foo-design.md".' },
          status: {
            type: 'string',
            enum: ['exists-sufficient', 'exists-needs-extension', 'missing-blocks-impl'],
          },
          needed: { type: 'string', description: 'For extension/missing: the section to add or doc to scaffold.' },
        },
      },
    },
  },
}

// Grimoire-Requirement tracker issues (origin-D, always mandatory).
// A zero-result `issues` array is valid — the reader must still run.
const GRIMOIRE_REQUIREMENT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['issues'],
  properties: {
    issues: {
      type: 'array',
      description: 'Open issues tagged Grimoire-Requirement. Empty array when none exist.',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'title', 'summary'],
        properties: {
          id: { type: 'string', description: 'Issue id or slug.' },
          title: { type: 'string' },
          summary: { type: 'string', description: 'One-line description of what the issue requires.' },
        },
      },
    },
  },
}

const SIZE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['name', 'tokens', 'band', 'rationale', 'designStatus'],
  properties: {
    name: { type: 'string' },
    tokens: { type: 'string', description: 'Point estimate, e.g. "~30K" — not a range.' },
    band: { type: 'string', enum: ['XS', 'S', 'M', 'L'] },
    rationale: { type: 'string', description: 'Name the files that dominate the read phase and the output type that dominates the write phase.' },
    designStatus: {
      type: 'string',
      enum: ['ready', 'needs-design', 'unknown'],
      description: 'Whether a design doc exists/suffices for this item to start.',
    },
  },
}

// Batched sizing returns one entry per item (used below the fan-out threshold).
const BATCH_SIZE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['items'],
  properties: {
    items: { type: 'array', items: SIZE_SCHEMA },
  },
}

// --- Orient (+ velocity) ----------------------------------------------------
// Folds the velocity read into orient: both derive from version-history.md, so
// reading it once here avoids a duplicate read in a separate gather agent.
phase('Orient')

const argTarget =
  typeof args === 'string' ? args : (args && args.target) ? args.target : null

// Size-strategy crossover (W4). Batching all items into one sizer is strictly
// cheaper in TOKENS than fanning out — every fanned agent re-pays the fixed
// per-agent context (PER_AGENT_FIXED_CONTEXT, ~45K of system prompt + tool
// schemas, the dominant cost per Finding 1) AND re-reads the overlapping design
// docs, so fan-out cost ≈ N×(batch cost). The only thing fan-out buys is
// WALL-CLOCK: the batch sizer works through items serially in one context
// (~SIZER_PER_ITEM_WORK tokens of incremental read+reason per item), while
// fanned agents run in parallel at ~constant wall-clock. Fanning out is worth
// its ~N× token premium only once the batch's serial per-item accumulation
// (N × SIZER_PER_ITEM_WORK) rivals a single agent's fixed context — i.e.
//   N ≳ PER_AGENT_FIXED_CONTEXT / SIZER_PER_ITEM_WORK.
// With ~45K fixed context and ~6K incremental work per sized item that crossover
// is ~7.5, which rounds to the shipped default of 8. Below it, batch (cheaper
// AND faster on the overlap); above it, fan out for parallel wall-clock.
const PER_AGENT_FIXED_CONTEXT = 45_000 // measured fixed per-agent context (Finding 1)
const SIZER_PER_ITEM_WORK = 6_000 // ~incremental read+reason a batched sizer spends per item
const DEFAULT_SIZE_FANOUT_THRESHOLD = Math.round(
  PER_AGENT_FIXED_CONTEXT / SIZER_PER_ITEM_WORK,
)

// Configurable override: Workflow({ args: { sizeFanoutThreshold: N } }). A caller
// (or, later, P4's profile-aware override lookup) may tune the crossover; absent
// an override we use the computed default. Guard against non-positive / non-finite.
const argThreshold =
  args && typeof args === 'object' && Number.isFinite(args.sizeFanoutThreshold)
    ? args.sizeFanoutThreshold
    : null
const SIZE_FANOUT_THRESHOLD =
  argThreshold && argThreshold > 0 ? argThreshold : DEFAULT_SIZE_FANOUT_THRESHOLD

const orient = await agent(
  `Determine release versions AND velocity for planning, per release-planning SKILL.md Steps 1-2. Work efficiently: read \`docs/version-history.md\` exactly ONCE and \`ls docs/release-planning-v*.md\` once; do not explore beyond these.
1. Current released version: from version-history.md (first heading / changelog). "none" if the file is absent.
2. In-flight release: the highest \`docs/release-planning-v*.md\` with NO matching version-history entry.
3. Target version: one MINOR bump beyond in-flight.${argTarget ? ` The user specified target "${argTarget}" — honour it and verify consistency.` : ''}
4. Velocity: from the same version-history.md, the last 2-3 versions' item counts + one-line scope, and a note on whether the target looks in line with recent velocity. If version-history.md is absent, return recent=[] and infer velocity from the release-planning-v*.md filenames in velocityNote.
Report how you derived each version value and flag ambiguity.`,
  { label: 'orient', model: 'haiku', schema: ORIENT_SCHEMA },
)

const target = argTarget || orient.target
const inFlight = orient.inFlight
const velocity = { recent: orient.recent, note: orient.velocityNote }
log(`Planning v${target} (in-flight v${inFlight}, released v${orient.released}).`)

// --- Gather (parallel readers) ----------------------------------------------
// Barrier: the full candidate item list isn't known until every reader returns,
// so sizing cannot start before all complete. All three readers are mechanical
// SCHEMA-CONSTRAINED extraction → haiku. The design reader was sonnet ("light
// judgement") but DESIGN_SCHEMA bounds it to a fixed 3-value status enum + short
// path/needed strings — classification into an enum, not free-text reasoning — so
// haiku suffices at ~3× lower rate (v1.9 audit rec C4). Each reader is told to
// read only its named files in one step (fewer turns → less cache-read).
// The fourth reader (grimoire-requirement) runs the issue-tracker CLI for
// origin-D items; it is always mandatory (zero result is valid).
phase('Gather')

const [roadmap, carry, design, grimreq] = await parallel([
  () =>
    agent(
      `Read docs/roadmap.md §v${target} (read that one file in a single step; do not explore further). Extract the flagship theme, explicit non-goals, and the named deliverables. If the roadmap lists concrete deliverables, return them as items; otherwise propose a sensible decomposition of the flagship into 2-5 sub-items. Do NOT pull items from v${target}'s successor — stay one version at a time.`,
      { label: 'read:roadmap', model: 'haiku', phase: 'Gather', schema: ROADMAP_SCHEMA },
    ),
  () =>
    agent(
      `Read docs/release-planning-v${inFlight}.md (the in-flight plan — read that one file in a single step). From §4 "Out of Scope", collect every item tagged v${target}+/later as a COMMITTED carryover. From §5 follow-up sections, collect bullets: those clearly tagged for v${target} are committed, the rest are candidates for the user to decide. Carryovers are the most commonly missed items — be exhaustive.`,
      { label: 'read:carryovers', model: 'haiku', phase: 'Gather', schema: CARRYOVER_SCHEMA },
    ),
  () =>
    agent(
      `Survey design-doc readiness for v${target}. Read docs/design/README.md plus any feature design doc named in docs/roadmap.md §v${target} — read those specific files directly, do not crawl the whole design tree. For each feature implied by the roadmap entry, classify its design doc as exists-sufficient, exists-needs-extension (name the section), or missing-blocks-impl (must be scaffolded before implementation).`,
      { label: 'read:design', model: 'haiku', phase: 'Gather', schema: DESIGN_SCHEMA },
    ),
  () =>
    agent(
      `Run the following command and return its output as a list of issues: \`python3 .claude/skills/issue-tracker/issue_tracker.py list --state open --labels Grimoire-Requirement\`. A zero result ("(no issues)") is valid — return an empty issues array. These are framework-required tracker issues (origin-D) and are never optional context.`,
      { label: 'read:grimoire-requirement', model: 'haiku', phase: 'Gather', schema: GRIMOIRE_REQUIREMENT_SCHEMA },
    ),
])

// --- Assemble the candidate item list (plain code, no agent) ----------------
// Tag each item with origin so the report's section grouping is preserved.
const grimreqItems = (grimreq?.issues || []).map((i) => ({
  name: i.title,
  summary: i.summary,
  origin: `framework-required (${i.id})`,
}))
if (grimreqItems.length > 0) {
  log(`${grimreqItems.length} Grimoire-Requirement tracker issue(s) found (origin-D).`)
} else {
  log('No open Grimoire-Requirement tracker issues (origin-D empty — valid).')
}

const candidateItems = [
  ...(roadmap?.items || []).map((i) => ({ ...i, origin: 'flagship' })),
  ...(carry?.committed || []).map((i) => ({ name: i.name, summary: i.summary, origin: `carryover (${i.origin})` })),
  ...(carry?.candidates || []).map((i) => ({ name: i.name, summary: i.summary, origin: 'follow-up candidate' })),
  ...grimreqItems,
]
log(`${candidateItems.length} candidate work items to size.`)

// --- Size (adaptive: batch by default, fan out only for large plans) --------
// Measured: batching all items into one sonnet agent (shared files read once)
// is cheaper AND faster than one agent per item (which re-reads overlapping
// docs and pays per-agent overhead N times). Fan out only past
// SIZE_FANOUT_THRESHOLD (derived above from the cost/wall-clock crossover, or
// overridden via args), where enough independent items make parallel wall-clock
// outweigh the duplicated reads.
phase('Size')

log(`Size-fanout threshold: ${SIZE_FANOUT_THRESHOLD}${argThreshold ? ' (override)' : ' (computed default)'}.`)
const sizeBands =
  'Bands: XS 5-15K (single-file flag), S 15-40K (small sub-feature + design update), ' +
  'M 40-80K (new module, 3-5 files), L 80-200K (multi-file architecture change). ' +
  'Give a POINT estimate (e.g. "~30K", not a range), name the files dominating the read ' +
  'phase and the output type dominating the write phase, and judge whether a design doc ' +
  'already lets the item start (designStatus).'

// Reattach canonical identity (name/summary/origin from the candidate item, never
// the sizer's possibly-paraphrased echo) onto the sizer's estimate, matched
// POSITIONALLY by the caller. A missing estimate (sizer failure or count mismatch)
// yields a visible "unsized" row rather than being dropped — the planner must never
// silently lose a work item.
const reattach = (s, item) =>
  s
    ? { ...s, name: item.name, summary: item.summary, origin: item.origin }
    : {
        name: item.name,
        summary: item.summary,
        origin: item.origin,
        tokens: 'unsized',
        band: 'unknown',
        rationale: 'Sizer returned no estimate for this item (agent failure or count mismatch) — size manually before lock.',
        designStatus: 'unknown',
      }

let sized
if (candidateItems.length === 0) {
  log('No candidate work items found — skipping the size phase.')
  sized = []
} else if (candidateItems.length > SIZE_FANOUT_THRESHOLD) {
  log(`${candidateItems.length} items > ${SIZE_FANOUT_THRESHOLD}: fanning out one sizer per item.`)
  // parallel() preserves order and resolves any failed thunk to null, so the
  // result aligns 1:1 with candidateItems by index — match positionally and let
  // reattach turn any null into a visible "unsized" row instead of dropping it.
  sized = (
    await parallel(
      candidateItems.map((item) => () =>
        agent(
          `Estimate the subagent token budget for this work item, per release-planning SKILL.md Step 4.
Item: "${item.name}" — ${item.summary}
Origin: ${item.origin}
Read only the design docs / source files this item would touch, in as few steps as possible. ${sizeBands}`,
          { label: `size:${item.name}`.slice(0, 60), model: 'sonnet', phase: 'Size', schema: SIZE_SCHEMA },
        ),
      ),
    )
  ).map((s, idx) => reattach(s, candidateItems[idx]))
} else {
  log(`${candidateItems.length} items ≤ ${SIZE_FANOUT_THRESHOLD}: sizing all in one batched agent (shared reads).`)
  const batch = await agent(
    `Estimate the subagent token budget for EACH work item below, per release-planning SKILL.md Step 4.
Several items touch the same design/source files — read each shared file at most ONCE, in as few steps as possible. ${sizeBands}
Return EXACTLY one entry per item, in the SAME ORDER as listed below (entry N sizes item N); echo each given name in \`name\`. Do not add, drop, reorder, or merge entries.

ITEMS:
${candidateItems.map((it, i) => `${i + 1}. "${it.name}" — ${it.summary} [origin: ${it.origin}]`).join('\n')}`,
    { label: 'size:batch', model: 'sonnet', phase: 'Size', schema: BATCH_SIZE_SCHEMA },
  )
  // Match the sizer's ordered array to candidates BY INDEX, not by name: a
  // paraphrased name would miss a name lookup and the item would vanish via the
  // old `.filter(Boolean)`. A count mismatch is surfaced, and any unfilled slot
  // becomes a flagged "unsized" row (reattach) rather than a silent drop.
  const estimates = batch?.items || []
  if (estimates.length !== candidateItems.length) {
    log(`⚠ Sizer returned ${estimates.length} estimate(s) for ${candidateItems.length} item(s) — matching by position; any unmatched item is flagged "unsized".`)
  }
  sized = candidateItems.map((it, idx) => reattach(estimates[idx], it))
}

// --- Synthesize the report --------------------------------------------------
// OUTPUT-TOKEN LEVER (v1.9 E3): synthesis is the single largest output emitter
// in this workflow — it regenerates a multi-section markdown report (the v1.9
// baseline measured the orchestrator path as the costliest operation, output-
// dominated on the session model). The step is mechanical: it fills a FIXED
// template from already-structured JSON; no judgement, no new data. So tier it
// DOWN to sonnet (lever 1 "tier-down output-heavy steps" + lever 3) — output
// volume is unchanged but the per-token rate drops ~5× off Opus where it bites
// hardest. The prompt also forbids echoing the input JSON back into the report
// (terse structured output): emit only the tables, never restate the data given.
phase('Synthesize')

const report = await agent(
  `Assemble a v${target} work-items report in EXACTLY the house format from release-planning SKILL.md "Report structure". Do not invent items — use only the data below. Emit ONLY the report sections; do NOT echo or restate the input JSON, and do not narrate what you are doing.

ORIENT: ${JSON.stringify(orient)}
ROADMAP: ${JSON.stringify(roadmap)}
CARRYOVERS: ${JSON.stringify(carry)}
GRIMOIRE_REQUIREMENT: ${JSON.stringify(grimreq)}
DESIGN: ${JSON.stringify(design)}
VELOCITY: ${JSON.stringify(velocity)}
SIZED ITEMS: ${JSON.stringify(sized)}

Produce markdown with these sections in order:
## v${target} Work Items Report
### Theme — one paragraph (flagship name, problem, non-goals).
### 1. Flagship — {name}  → table: # | Item | Tokens | Rationale, then a subtotal line. (origin === 'flagship')
### 2. Carryovers from v${inFlight}  → group by theme; table per group: # | Item | Tokens | Rationale. (carryover + follow-up candidate origins; mark candidates as "(candidate — user to decide)")
### 3. Framework-Required Issues (origin-D)  → table: # | Issue | Tokens | Rationale, then a note. (origin starts with 'framework-required'; if empty, include the section with "No open Grimoire-Requirement issues." so it is visible.) These items are never optional — they appear in every report.
### 4. Work Items Summary  → master table: # | Area | Item | Est. Tokens, then a Total line. Include all origins A–D.
### 5. Design Work Required  → table: Document | Status | What's needed (from DESIGN + any item with designStatus needs-design).
### 6. Observations for Iteration  → 3-6 bullets: scope risks, spike recommendations, velocity calibration, and any item the user must decide before lock.
Close with a one-line reminder that this is a planning input — next steps are design-doc-scaffold for §5 gaps, then release-agreement to lock scope.`,
  // Tiered to sonnet: mechanical template-fill of structured JSON, no judgement.
  // This is the workflow's heaviest output step; the ~5× Opus→Sonnet rate cut
  // applies directly to the costliest token class on the costliest operation.
  { label: 'synthesize', model: 'sonnet', schema: undefined },
)

log('Report draft assembled — hand to user for iteration before release-agreement.')
return report
