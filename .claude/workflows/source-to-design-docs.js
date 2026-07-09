export const meta = {
  name: 'source-to-design-docs',
  tier: 'read-only',   // all paradigms; no file writes, no branch ops, no commits
  description: 'Analysis fan-out version of the grm-source-to-design-docs skill: parallel per-module/subsystem readers → candidate manifest → structured design-doc content proposals. Stops at the user-confirmation gate; no files are written.',
  whenToUse: 'When onboarding an existing codebase that lacks design docs, or when you want broad, parallel coverage of all subsystems before confirming which docs to write. Produces a candidate manifest + per-area content proposals; the user reviews and confirms before the skill writes any file.',
  phases: [
    { title: 'Orient', detail: 'one haiku agent: survey top-level structure (README, docs/, directory tree, entrypoints) in one pass', model: 'haiku' },
    { title: 'Gather', detail: 'parallel haiku readers — one per identified module/subsystem; extract public API surface, key data structures, inline "why" comments', model: 'haiku' },
    { title: 'Classify', detail: 'sonnet; batch-classify all gathered areas into design-doc candidates with name/key-files/what-it-does/dependencies; fan out only past threshold', model: 'sonnet' },
    { title: 'Synthesize', detail: 'assemble the candidate manifest + per-area content proposals (inherits session model)' },
  ],
}

// ---------------------------------------------------------------------------
// This workflow mechanises the ANALYSIS PHASE of
// .claude/skills/grm-source-to-design-docs/SKILL.md (Steps 1-2 + the survey
// content for Step 4). It fans out parallel per-module reads, then classifies
// what warrants a design doc and drafts the content structure for each.
//
// READ-ONLY CONTRACT: This workflow writes no files and creates no branches.
// It returns a structured analysis report to the calling agent, who presents
// it to the user for confirmation (SKILL.md Step 2 gate) before any docs are
// written. All file-writing steps (SKILL.md Steps 3-6) remain outside this
// workflow — handled by the skill's interactive follow-on sequence.
//
// COST MODEL (follows the release-planning.js lessons; ratios apply here too):
//
//   1. MODEL TIER is the dominant cost lever. Each agent pays ~45K fixed
//      context on entry; haiku for mechanical extraction, sonnet for
//      classification/judgement, session model only for the final synthesis
//      that the user actually reads.
//   2. BATCH shared reads; fan out only past the threshold. Per-module
//      readers are genuinely independent (each reads its own files), so
//      fan-out is appropriate there. Classification, however, reads the
//      full gathered output once — batch it into one sonnet agent by default,
//      fan out only past CLASSIFY_FANOUT_THRESHOLD when many independent
//      modules make parallel wall-clock worth the duplicated context cost.
//   3. Read named files in a single step; do not explore beyond them.
//   4. Structured-output schemas everywhere; match results POSITIONALLY
//      by index, never by a paraphrasable name.
//   5. Forbid synthesis agent from echoing input data back into the report.
//
// Invoke:  Workflow({ name: 'source-to-design-docs' })
//          Workflow({ name: 'source-to-design-docs', args: { maxModules: 20 } })
//          Workflow({ name: 'source-to-design-docs', args: { classifyFanoutThreshold: 12 } })
// ---------------------------------------------------------------------------

// --- Schema definitions ----------------------------------------------------

const ORIENT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['projectName', 'purpose', 'techStack', 'topLevelDirs', 'entrypoints', 'existingDocs', 'modules'],
  properties: {
    projectName: { type: 'string', description: 'Project name from README or package manifest.' },
    purpose: { type: 'string', description: 'One-sentence project purpose from README.' },
    techStack: { type: 'string', description: 'Primary language(s) and key frameworks/runtimes.' },
    topLevelDirs: {
      type: 'array',
      items: { type: 'string' },
      description: 'Top-level source directories (src/, lib/, cmd/, etc.); exclude build artefacts and vendored deps.',
    },
    entrypoints: {
      type: 'array',
      items: { type: 'string' },
      description: 'Primary entry-point files (main binary, server index, library root, etc.).',
    },
    existingDocs: {
      type: 'array',
      items: { type: 'string' },
      description: 'Paths to existing documentation files (README, docs/, wiki/, ADRs/, rfcs/). Empty if none.',
    },
    modules: {
      type: 'array',
      description: 'Candidate subsystem/module areas to read in parallel — one reader per entry. Each entry names the area and the files a reader should inspect.',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['slug', 'label', 'files'],
        properties: {
          slug: { type: 'string', description: 'kebab-case identifier, e.g. "auth", "data-pipeline".' },
          label: { type: 'string', description: 'Human-readable name, e.g. "Auth subsystem".' },
          files: {
            type: 'array',
            items: { type: 'string' },
            description: '2-6 key files for this area. Name them specifically so a reader agent can open them directly without exploring.',
          },
        },
      },
    },
  },
}

const MODULE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['slug', 'label', 'purpose', 'apiSurface', 'keyTypes', 'whyComments', 'dependencies', 'todos'],
  properties: {
    slug: { type: 'string' },
    label: { type: 'string' },
    purpose: { type: 'string', description: 'One sentence: what problem does this module solve?' },
    apiSurface: { type: 'string', description: 'What the module exports or exposes to callers (functions, classes, routes, events, types).' },
    keyTypes: { type: 'string', description: 'Core data structures or types at the heart of this module. "None evident" if purely functional.' },
    whyComments: { type: 'string', description: 'Any inline "why" explanations, design rationale, or intent comments in the source. "None found" if absent.' },
    dependencies: {
      type: 'array',
      items: { type: 'string' },
      description: 'Other internal module slugs this area depends on. Empty if none or unknown.',
    },
    todos: {
      type: 'array',
      items: { type: 'string' },
      description: 'TODO/FIXME comments that represent open design questions or deferred work. Empty if none.',
    },
  },
}

const CANDIDATE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['slug', 'label', 'warrants', 'reason', 'keyFiles', 'whatItDoes', 'dependencies', 'priorityHint'],
  properties: {
    slug: { type: 'string', description: 'kebab-case — becomes the design doc filename stem.' },
    label: { type: 'string', description: 'Human-readable subsystem name.' },
    warrants: { type: 'boolean', description: 'True if this area warrants a design doc.' },
    reason: { type: 'string', description: 'One sentence: why it does or does not warrant a doc.' },
    keyFiles: {
      type: 'array',
      items: { type: 'string' },
      description: '2-6 files that contain the core logic (for the grm-design-doc-scaffold skill to reference).',
    },
    whatItDoes: { type: 'string', description: 'One sentence: what this subsystem does.' },
    dependencies: {
      type: 'array',
      items: { type: 'string' },
      description: 'Other candidate slugs this area depends on (for cross-linking in design docs).',
    },
    priorityHint: {
      type: 'string',
      enum: ['high', 'medium', 'low'],
      description: 'Suggested documentation priority: high = non-obvious + widely depended-on; low = trivial or self-evident from code.',
    },
  },
}

const BATCH_CLASSIFY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['candidates'],
  properties: {
    candidates: { type: 'array', items: CANDIDATE_SCHEMA },
  },
}

// --- Configurable thresholds -----------------------------------------------

// Classify-fanout threshold: the cross-over at which fanning out one classifier
// per module becomes cheaper in wall-clock time than one batched classifier
// reading all gathered data. Derived from the same cost model as release-planning:
//   PER_AGENT_FIXED_CONTEXT (~45K) / CLASSIFY_PER_ITEM_WORK (~6K) ≈ 7.5 → 8
const PER_AGENT_FIXED_CONTEXT = 45_000
const CLASSIFY_PER_ITEM_WORK = 6_000
const DEFAULT_CLASSIFY_FANOUT_THRESHOLD = Math.round(
  PER_AGENT_FIXED_CONTEXT / CLASSIFY_PER_ITEM_WORK,
)

const argClassifyThreshold =
  args && typeof args === 'object' && Number.isFinite(args.classifyFanoutThreshold)
    ? args.classifyFanoutThreshold
    : null
const CLASSIFY_FANOUT_THRESHOLD =
  argClassifyThreshold && argClassifyThreshold > 0
    ? argClassifyThreshold
    : DEFAULT_CLASSIFY_FANOUT_THRESHOLD

// Cap the number of parallel module readers to keep cost bounded on very large repos.
const DEFAULT_MAX_MODULES = 20
const argMaxModules =
  args && typeof args === 'object' && Number.isFinite(args.maxModules)
    ? args.maxModules
    : null
const MAX_MODULES = argMaxModules && argMaxModules > 0 ? argMaxModules : DEFAULT_MAX_MODULES

// --- Orient -----------------------------------------------------------------
// One haiku agent reads the project's top-level surface in a single pass:
//   README, directory tree, entrypoints, existing docs.
// It returns the module list that drives the parallel Gather phase.
phase('Orient')

const orient = await agent(
  `Survey this project's top-level structure to map the major subsystems. Work efficiently — read everything in as few steps as possible:
1. Read the top-level README (or equivalent: README.md, readme.md, README.rst, README.txt — whichever exists).
2. Run \`find . -maxdepth 3 -type f | grep -v -E '(node_modules|\.git|dist|build|__pycache__|\.venv|vendor)/|\.min\.' | head -200\` to get the file/module layout.
3. Read up to 3 existing docs from docs/, wiki/, rfcs/, or ADRs/ if they exist.
4. Identify the primary entrypoint file(s) and skim them.

From these reads, produce:
- The project name and one-sentence purpose.
- Tech stack (language, framework, runtime).
- Top-level source directories.
- Primary entrypoint files.
- A list of candidate MODULE AREAS to analyse in parallel — each named with a slug, label, and 2-6 key files a reader should open. Aim for cohesive subsystem boundaries (e.g. "auth", "data-pipeline"), not per-file boundaries. Cap at ${MAX_MODULES} modules. A good candidate has clear boundaries and non-obvious design that wouldn't be self-evident from code alone.

Do not explore beyond what is needed to fill the schema.`,
  { label: 'orient', model: 'haiku', schema: ORIENT_SCHEMA },
)

const modules = (orient?.modules || []).slice(0, MAX_MODULES)
log(`Project: ${orient?.projectName || '(unknown)'}. Found ${modules.length} module area(s) to analyse.`)

// --- Gather -----------------------------------------------------------------
// One haiku reader per module area — all independent, all parallel.
// Each reader opens only the files the orient agent named; no exploration.
phase('Gather')

const reattachModule = (result, mod) =>
  result
    ? { ...result, slug: mod.slug, label: mod.label }
    : {
        slug: mod.slug,
        label: mod.label,
        purpose: 'Reader failed — analyse manually.',
        apiSurface: '',
        keyTypes: '',
        whyComments: '',
        dependencies: [],
        todos: [],
      }

let gathered
if (modules.length === 0) {
  log('No module areas identified — skipping Gather phase.')
  gathered = []
} else {
  gathered = (
    await parallel(
      modules.map(
        (mod) => () =>
          agent(
            `Read the source files for the "${mod.label}" subsystem (slug: "${mod.slug}"). Read THESE FILES in one step; do not explore further:
${mod.files.map((f) => `  - ${f}`).join('\n')}

Extract:
- One-sentence purpose: what problem does this module solve?
- Public API surface: what does it export or expose to callers?
- Core data structures or types.
- Any inline "why" / design-rationale comments.
- Internal dependencies on other modules (list slugs if known).
- TODO/FIXME comments that represent open design questions.

Be concise — you are feeding a classifier, not writing the doc.`,
            { label: `read:${mod.slug}`.slice(0, 60), model: 'haiku', phase: 'Gather', schema: MODULE_SCHEMA },
          ),
      ),
    )
  ).map((result, idx) => reattachModule(result, modules[idx]))
}

log(`Gathered ${gathered.length} module report(s).`)

// --- Classify ---------------------------------------------------------------
// Sonnet classifies each gathered area into a design-doc candidate entry.
// Batched by default (shared reads, lower cost); fan out only past threshold.
phase('Classify')

log(`Classify-fanout threshold: ${CLASSIFY_FANOUT_THRESHOLD}${argClassifyThreshold ? ' (override)' : ' (computed default)'}.`)

const reattachCandidate = (result, mod) =>
  result
    ? { ...result, slug: mod.slug, label: mod.label }
    : {
        slug: mod.slug,
        label: mod.label,
        warrants: false,
        reason: 'Classifier returned no result — assess manually.',
        keyFiles: mod.files || [],
        whatItDoes: '',
        dependencies: [],
        priorityHint: 'low',
      }

let classified
if (gathered.length === 0) {
  log('No gathered data — skipping Classify phase.')
  classified = []
} else if (gathered.length > CLASSIFY_FANOUT_THRESHOLD) {
  log(`${gathered.length} areas > ${CLASSIFY_FANOUT_THRESHOLD}: fanning out one classifier per area.`)
  classified = (
    await parallel(
      gathered.map(
        (area) => () =>
          agent(
            `Classify this subsystem area as a design-doc candidate per source-to-design-docs SKILL.md Step 2 criteria.
A good candidate: (a) cohesive subsystem with clear boundaries, (b) public API surface other modules depend on, (c) non-obvious to a new contributor from code alone.

AREA DATA:
${JSON.stringify(area)}

Return EXACTLY ONE candidate entry. Set warrants=false if it is too trivial, too small, or entirely self-evident from code. Emit ONLY the schema fields; do not narrate.`,
            { label: `classify:${area.slug}`.slice(0, 60), model: 'sonnet', phase: 'Classify', schema: CANDIDATE_SCHEMA },
          ),
      ),
    )
  ).map((result, idx) => reattachCandidate(result, gathered[idx]))
} else {
  log(`${gathered.length} areas ≤ ${CLASSIFY_FANOUT_THRESHOLD}: classifying all in one batched agent.`)
  const batch = await agent(
    `Classify EACH subsystem area below as a design-doc candidate per source-to-design-docs SKILL.md Step 2 criteria.
A good candidate: (a) cohesive subsystem with clear boundaries, (b) public API surface other modules depend on, (c) non-obvious to a new contributor from code alone.

Return EXACTLY one entry per area, in the SAME ORDER as listed (entry N classifies area N). Echo each given slug in \`slug\`. Do not add, drop, reorder, or merge entries. Emit ONLY the schema fields; do not narrate.

AREAS:
${gathered.map((a, i) => `${i + 1}. slug="${a.slug}" label="${a.label}"\n${JSON.stringify(a)}`).join('\n\n')}`,
    { label: 'classify:batch', model: 'sonnet', phase: 'Classify', schema: BATCH_CLASSIFY_SCHEMA },
  )
  const estimates = batch?.candidates || []
  if (estimates.length !== gathered.length) {
    log(`Classifier returned ${estimates.length} result(s) for ${gathered.length} area(s) — matching by position; any unmatched area flagged.`)
  }
  classified = gathered.map((area, idx) => reattachCandidate(estimates[idx], area))
}

const warranted = classified.filter((c) => c.warrants)
log(`${warranted.length} of ${classified.length} area(s) warrant a design doc.`)

// --- Synthesize -------------------------------------------------------------
// Assemble the candidate manifest + per-area content proposals.
// Tiered to sonnet: mechanical template-fill of already-structured data,
// no new judgement. Output-heavy step → tier down to avoid Opus rate on the
// costliest token class (v1.9 E3 lesson; workflow-scaffold guidance §5).
phase('Synthesize')

const report = await agent(
  `Assemble a source-to-design-docs analysis report from the structured data below. Emit ONLY the report sections; do NOT echo or restate the input JSON, and do not narrate what you are doing.

PROJECT: ${JSON.stringify({ name: orient?.projectName, purpose: orient?.purpose, techStack: orient?.techStack, entrypoints: orient?.entrypoints, existingDocs: orient?.existingDocs })}
CLASSIFIED AREAS: ${JSON.stringify(classified)}

Produce markdown with these sections in order:

## Source-to-Design-Docs Analysis — {projectName}

### Project overview
One paragraph: name, purpose, tech stack, entrypoints.

### Design-doc candidates
Table of areas where warrants=true:
| Priority | Slug | What it does | Key files | Depends on |
|---|---|---|---|---|
(Sort by priorityHint: high first, then medium, then low.)

### Areas not warranting a doc
Table of areas where warrants=false:
| Slug | Reason |
|---|---|

### Per-candidate content proposals
For EACH candidate where warrants=true, one subsection:
#### {label} (\`docs/design/{slug}-design.md\`)
- **What it does:** {whatItDoes}
- **Key files:** {keyFiles joined as inline list}
- **Suggested Motivation:** (inferred from purpose + whyComments)
- **Suggested Scope:** (inferred from apiSurface — what's in, what's out)
- **Suggested Design notes:** (key data structures, API shape, cross-links to dependent slugs)
- **Suggested Acceptance:** (what tests / smoke behaviours would confirm this works)
- **Open questions / TODOs from source:** (from todos — "None found" if empty)

### Existing docs inventory
List of existing documentation files found: ${JSON.stringify(orient?.existingDocs || [])}
Note any that could be linked from the design docs rather than duplicated.

### Recommended next steps
1. Review the candidate list and confirm, split, or merge areas.
2. Confirm documentation priority order.
3. Run \`source-to-design-docs\` skill Steps 3-6 to write \`docs/design/README.md\`, \`architecture-design.md\`, and each feature doc.
4. If a release is being planned, run \`release-planning\` — the new docs will inform the work-items report.

Close with a one-line reminder that this is a read-only analysis — no files have been written. The user must confirm the candidate list before the skill writes any docs.`,
  // Tiered to sonnet: mechanical template-fill of structured JSON, no judgement.
  // Output-heavy step; the ~5× Opus→Sonnet rate reduction applies directly.
  { label: 'synthesize', model: 'sonnet', schema: undefined },
)

log('Analysis complete — present to user for confirmation before writing any docs.')
return report
