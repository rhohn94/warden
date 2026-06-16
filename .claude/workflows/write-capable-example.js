export const meta = {
  name: 'write-capable-example',
  tier: 'write-capable',   // Noir only — fails fast under Supervised / Weiss
  description: 'Canonical reference write-capable workflow: isolated-worktree parallel agents, per-agent feature branches, conflict-map output, and all three execution variants (Efficient / Fast / Careful-Serial). Safe to load under any paradigm; only acts when invoked under Noir.',
  whenToUse: 'When items are independently implementable, the project is actively running the Noir paradigm, and you want the integration master to collect the branches and merge them via release-phase-merge. Copy and adapt this script for any write-capable fan-out step.',
  phases: [
    { title: 'Orient',  detail: 'haiku: read work-item list + conflict dependencies; build the plan', model: 'haiku' },
    { title: 'Execute', detail: 'variant-aware fan-out: per-item agents in isolated worktrees (Efficient/Fast = parallel; Careful-Serial = maxConcurrency 1). Agents default to Sonnet; item.hard overrides to Opus.', model: 'sonnet' },
    { title: 'Report',  detail: 'return branch list + mergeAfter order to master for release-phase-merge handoff' },
  ],
}

// ---------------------------------------------------------------------------
// WRITE-CAPABLE WORKFLOW — REFERENCE IMPLEMENTATION
//
// This script demonstrates the full write-capable tier contract specified in
// docs/design/write-capable-workflow-design.md. It is the canonical reference
// for NW3 (isolated-worktree parallel execution + master-merge orchestration +
// the three execution variants).
//
// KEY INVARIANTS (enforced by hooks + this script):
//   1. AGENTS NEVER PUSH. push-guard.sh blocks any `git push` from agents.
//   2. AGENTS NEVER TOUCH dev / main / version/*. protected-branch-guard.sh
//      blocks commits/merges on protected refs in no-marker worktrees.
//   3. AGENTS STAY INSIDE THEIR OWN WORKTREE. worktree-guard.sh confines each
//      agent to its own isolated worktree path.
//   4. THE MASTER OWNS ALL MERGES. Only the marker-blessed integration worktree
//      may merge agent branches into the staging ref (release-phase-merge).
//   5. PUSH STAYS HUMAN. Even under Noir, pushing to origin is always
//      human-gated. This is a hard boundary; it is never lifted in v1.6.
//
// VARIANT SELECTION:
//   Efficient    (default) — parallel, low-waste. Respects the conflict map;
//                             batches shared reads; avoids redundant file access.
//   Fast                  — parallel, minimal time. All agents launch
//                             concurrently; conflicts resolved reactively.
//   Careful-Serial        — maxConcurrency: 1. Sequential in conflict-map order.
//                             Each agent's branch merges before the next starts.
//
// BRANCH NAMING:
//   <item-slug>-<short-uuid>
//   Generated before agents spawn; recorded in the plan so the master can
//   reference them in the merge sequence.
//
// CONFLICT MAP / MASTER HANDOFF:
//   The script returns:
//     { branches: [ { branch, mergeAfter, result }, … ] }
//   The master calls release-phase-merge with this list, following mergeAfter
//   ordering. See docs/design/write-capable-workflow-design.md §2.3–2.4.
//
// Invoke:
//   Workflow({ name: 'write-capable-example' })
//   Workflow({ name: 'write-capable-example', args: { variant: 'Fast' } })
//   Workflow({ name: 'write-capable-example', args: { variant: 'Careful-Serial' } })
//   Workflow({ name: 'write-capable-example', args: { items: [...], stagingRef: 'version/1.6' } })
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// NOIR GATE — fail closed if the active paradigm is not Noir.
//
// This check is explicit, early, and fail-closed. A write-capable workflow
// invoked under Supervised or Weiss fails immediately with a clear error rather
// than silently degrading or producing partial output.
// See docs/design/write-capable-workflow-design.md §1.2.
// ---------------------------------------------------------------------------
if (meta.tier === 'write-capable' && activeParadigm() !== 'Noir') {
  throw new Error(
    'write-capable workflows require the Noir paradigm. ' +
    'Switch paradigm (work-paradigm-switch skill) or use a read-only workflow.'
  )
}

// ---------------------------------------------------------------------------
// selectVariant — parse and validate the caller-supplied variant name.
//
// If no variant is passed, defaults to 'Efficient'. Unknown variant names
// throw immediately rather than silently falling back.
//
// The active workflow-variant.value from grimoire-config.json may override
// this default in a future release (currently in-development: true).
// ---------------------------------------------------------------------------
function selectVariant(args) {
  const v = (args && args.variant) ? args.variant : 'Efficient'
  if (!['Efficient', 'Fast', 'Careful-Serial'].includes(v)) {
    throw new Error(
      `Unknown variant "${v}". Choose one of: Efficient | Fast | Careful-Serial.\n` +
      '  Efficient     — parallel, low-waste (default; respects conflict map)\n' +
      '  Fast          — parallel, minimal time (all agents launch concurrently)\n' +
      '  Careful-Serial — maxConcurrency: 1; sequential in conflict-map order'
    )
  }
  return v
}

// ---------------------------------------------------------------------------
// generateShortUUID — produce a 4-char hex suffix for branch name uniqueness.
//
// Prevents collisions when the same item slug is used across workflow runs.
// The branch name is recorded BEFORE agents are spawned so the master can
// reference it in the merge sequence immediately.
// ---------------------------------------------------------------------------
function generateShortUUID() {
  return Math.floor(Math.random() * 0x10000).toString(16).padStart(4, '0')
}

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------

// Each planned work item: slug, description, dependencies, and the pre-assigned
// branch name. Generated before Execute so the master knows the branch names
// without waiting for agents to report back.
const PLAN_ITEM_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['slug', 'description', 'mergeAfter'],
  properties: {
    slug:        { type: 'string', description: 'Short kebab-case identifier, e.g. "update-config-parser".' },
    description: { type: 'string', description: 'One sentence: what this item implements.' },
    mergeAfter:  {
      type: 'array',
      items: { type: 'string' },
      description: 'Slugs this item depends on — must be merged before this one. Empty array if independent.',
    },
    hard:        {
      type: 'boolean',
      description: 'OPTIONAL escape hatch. true ONLY for genuinely hard items needing Opus-level ' +
                   'judgement (intricate algorithm, deep cross-module reasoning). Default false → ' +
                   'the Execute agent runs on Sonnet. Set sparingly; most implementation is mechanical.',
    },
  },
}

// Terse result schema for an Execute agent (v1.9 E3 output-token trim). Without
// a schema an implementation agent returns freeform prose (a recap of the diff,
// what it did, why) that flows wholesale into the workflow's return value and on
// to the master — pure output-token waste. The master needs only compact IDs to
// drive release-phase-merge: the branch, the commit SHA, and a one-line summary.
// Constraining the return to these fields caps generation and hands the master
// clean data, not prose. (Lever 1: terse structured output over free-text recap.)
const EXEC_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['branch', 'commit', 'summary'],
  properties: {
    branch:  { type: 'string', description: 'The branch committed on, e.g. "update-config-parser-a3f1".' },
    commit:  { type: 'string', description: 'Short commit SHA of the single commit, e.g. "a1b2c3d". Empty if nothing was committed.' },
    summary: { type: 'string', description: 'ONE sentence: what landed. No diff recap, no narration.' },
  },
}

const PLAN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['stagingRef', 'items'],
  properties: {
    stagingRef: { type: 'string', description: 'The staging ref agents branch from, e.g. "version/1.6".' },
    items: {
      type: 'array',
      items: PLAN_ITEM_SCHEMA,
      description: 'Work items in topological order (dependencies before dependents).',
    },
  },
}

// ---------------------------------------------------------------------------
// Phase: Orient
//
// A haiku agent reads the work-item list and their file-dependency graph to
// produce the ordered plan. Pre-assigns branch names here so they are known
// before any agent spawns.
//
// COST NOTE: haiku is sufficient for mechanical extraction of items + deps.
// Do not upgrade to sonnet unless the orient step requires judgement.
// ---------------------------------------------------------------------------
phase('Orient')

const stagingRef = (args && args.stagingRef) ? args.stagingRef : 'version/{X.Y}'

// If the caller passed a pre-built item list (args.items), skip the Orient
// agent and use it directly. Otherwise, the Orient agent resolves the list
// from project files — adapt the prompt below to name the files to read.
let plan
if (args && args.items && Array.isArray(args.items) && args.items.length > 0) {
  log(`Orient: caller-supplied ${args.items.length} items; skipping orient agent.`)
  plan = {
    stagingRef,
    items: args.items.map((it) => ({
      slug:        it.slug        || it.name || 'item',
      description: it.description || it.summary || '(no description)',
      mergeAfter:  it.mergeAfter  || [],
      hard:        it.hard === true,   // default false → Sonnet; true → Opus override
    })),
  }
} else {
  plan = await agent(
    // -----------------------------------------------------------------------
    // ADAPT THIS PROMPT: name the exact files that define the work-item list
    // and dependency graph for your use case. Tell the agent to read them
    // in a single step and return EXACTLY one entry per item, same order.
    // -----------------------------------------------------------------------
    `Read the work-item list and dependency graph for this workflow run.
Read the relevant source files in ONE step; do not explore beyond what is named.
Staging ref: ${stagingRef}

Return a plan with:
  - stagingRef: "${stagingRef}"
  - items: each with slug (kebab-case), description (one sentence),
           mergeAfter (slugs this item depends on; [] if independent), and
           hard (boolean; set true ONLY for items needing Opus-level judgement —
           intricate algorithms or deep cross-module reasoning. Default false:
           most implementation is mechanical and runs on Sonnet).
Items must be in topological order (dependencies first).
If no items are found, return { stagingRef: "${stagingRef}", items: [] }.`,
    { label: 'orient', model: 'haiku', schema: PLAN_SCHEMA },
  )
}

if (!plan || !plan.items || plan.items.length === 0) {
  log('Orient returned no items — nothing to execute.')
  return { branches: [] }
}

// Assign branch names NOW, before any agent spawns, so the master has the
// full branch list in the conflict map even if some agents fail mid-run.
// Branch name format: <item-slug>-<short-uuid>
const plannedItems = plan.items.map((it) => ({
  ...it,
  branch: `${it.slug}-${generateShortUUID()}`,
}))

log(`Orient: ${plannedItems.length} items planned on ${plan.stagingRef}.`)
plannedItems.forEach((it) => log(`  ${it.branch}  (after: [${it.mergeAfter.join(', ')}])`))

// ---------------------------------------------------------------------------
// Phase: Execute
//
// Variant-aware fan-out. Each agent receives an isolated worktree (isolation:
// 'worktree'), implements its item, commits on its pre-assigned branch, and
// exits without merging.
//
// VARIANT CONCURRENCY:
//   Efficient     — parallel (concurrency = N items), but honours conflict map:
//                   items with mergeAfter wait on their deps before the MASTER
//                   merges them. Agent-level parallelism is still full; it is
//                   the MERGE ORDER that enforces deps, not agent launch order.
//   Fast          — same parallel concurrency, no deduplication of shared reads.
//                   Conflict resolution is expected reactively by the master.
//   Careful-Serial — maxConcurrency: 1; agents run one at a time in plan order.
//                    Each branch is merged before the next agent starts (the
//                    master drives this via release-phase-merge between spawns).
//
// Note: for Careful-Serial, the merge loop (the master calling release-phase-
// merge between agent runs) is orchestrated OUTSIDE this workflow. This
// workflow sets maxConcurrency: 1 so the harness runs agents sequentially;
// the master is responsible for merging each branch before the next agent in
// the sequence needs its deps to be present on the staging ref.
// ---------------------------------------------------------------------------
phase('Execute')

const variant    = selectVariant(args)
const concurrency = variant === 'Careful-Serial' ? 1 : plannedItems.length

log(`Executing ${plannedItems.length} agents — variant: ${variant}, concurrency: ${concurrency}.`)

// Agent prompt template. Each agent:
//   1. Branches from the staging ref at the pre-assigned branch name.
//   2. Implements the work item.
//   3. Commits with an atomic, one-sentence message.
//   4. DOES NOT push, merge, or touch dev / main / version/*.
//
// The guard hooks (protected-branch-guard.sh, worktree-guard.sh) enforce
// invariants 2-4 independently; the prompt reinforces them explicitly.
function agentPrompt(item) {
  return (
    `Implement work item: "${item.description}"

SETUP — branch from the staging ref first:
  git switch -c ${item.branch} ${plan.stagingRef}

IMPLEMENT the work item as described. Keep changes atomic and scoped to
this item only. Do not touch files outside your scope.

COMMIT when done:
  git add -p   # stage only the files this item touches
  git commit -m "<one-sentence summary of what this item did>"

CONSTRAINTS — these are hard, not advisory:
  - Do NOT git push (push-guard.sh will block it anyway).
  - Do NOT git merge or git rebase targeting dev / main / version/*.
  - Do NOT edit files outside this worktree.
  - Do NOT edit docs/release-planning-v*.md.

EXIT after committing. Return ONLY { branch, commit (short SHA), summary
(one sentence) } — no diff recap, no narration of what you did.
Branch: ${item.branch}
Staging ref: ${plan.stagingRef}
${item.mergeAfter.length > 0
  ? `Merge order note: this item depends on [${item.mergeAfter.join(', ')}]. ` +
    'Those branches will be merged by the master before this branch is merged. ' +
    'You do not need to wait; implement on the staging ref tip as-is.'
  : 'This item has no dependencies — it can merge in any order.'
}`
  )
}

// parallel() preserves order so results align 1:1 with plannedItems by index.
// A failed agent resolves to null (handled in Report via reattachByIndex).
const results = (await parallel(
  plannedItems.map((item) => () =>
    agent(agentPrompt(item), {
      label:     `item:${item.slug}`.slice(0, 60),
      isolation: 'worktree',     // harness creates a fresh git worktree for this agent
      phase:     'Execute',
      schema:    EXEC_RESULT_SCHEMA,   // terse {branch,commit,summary} — no prose recap
      // MODEL TIER (v1.9 audit rec D2): default Execute agents to SONNET, not the
      // session model (Opus under Noir). Each agent pays a fixed ~45K context cost,
      // so volume is near-flat across tiers while the rate is not (Opus ≈ 5× Sonnet)
      // — and that 5× multiplies by the fan-out width. Most implementation is
      // mechanical; Sonnet is the implementation workhorse. The `item.hard` escape
      // hatch overrides to Opus for genuinely hard items (intricate algorithm, deep
      // cross-module reasoning). Never 'haiku' for write-capable agents — too weak,
      // risks rework. Set hard:true sparingly; it forfeits the 5× saving for that item.
      model:     item.hard ? 'opus' : 'sonnet',
    })
  ),
  { maxConcurrency: concurrency },
)).map((result, i) => {
  // Match POSITIONALLY by index, never by name.
  // A null result (agent failure) becomes a visible 'failed' entry so the
  // master can surface it, rather than silently disappearing from the list.
  if (result === null || result === undefined) {
    return {
      branch:     plannedItems[i].branch,
      slug:       plannedItems[i].slug,
      mergeAfter: plannedItems[i].mergeAfter,
      status:     'failed',
      result:     null,
    }
  }
  return {
    branch:     plannedItems[i].branch,
    slug:       plannedItems[i].slug,
    mergeAfter: plannedItems[i].mergeAfter,
    status:     'completed',
    result,
  }
})

const completed = results.filter((r) => r.status === 'completed')
const failed    = results.filter((r) => r.status === 'failed')

log(`Execute: ${completed.length} completed, ${failed.length} failed.`)
if (failed.length > 0) {
  log(`Failed branches: ${failed.map((r) => r.branch).join(', ')}`)
  log('Master: review failed items before calling release-phase-merge.')
}

// ---------------------------------------------------------------------------
// Phase: Report
//
// Return the branch list and merge order to the integration master. The master
// calls release-phase-merge with this output, following mergeAfter ordering.
//
// Output schema (matches release-phase-merge input contract):
//   {
//     variant:  string,            — which variant was used
//     branches: [
//       {
//         branch:     string,      — git branch name (<slug>-<uuid>)
//         mergeAfter: string[],    — slugs whose branches must be merged first
//         status:     'completed' | 'failed',
//         result:     any,         — agent output (null if failed)
//       },
//       …
//     ]
//   }
//
// The master follows mergeAfter as a dependency DAG:
//   - Branches with empty mergeAfter are merged first (independent).
//   - A branch with mergeAfter: ['a', 'b'] is merged only after 'a' and 'b'
//     have been successfully merged into the staging ref.
//   - On conflict: master attempts auto-resolve; stops and surfaces to user
//     on ambiguous conflict.
//   - Failed agents: master surfaces a summary before starting the merge run.
//
// Push to origin remains human-gated throughout. This workflow and all its
// agents are inside the local repository; nothing reaches the remote.
// ---------------------------------------------------------------------------
phase('Report')

log(`Report: returning ${results.length} branch entries to master.`)
log('Master next step: run release-phase-merge with this branch list, following mergeAfter order.')
log('Push to origin remains human-gated — propose the push and wait for user confirmation.')

return {
  variant,
  branches: results.map((r) => ({
    branch:     r.branch,
    mergeAfter: r.mergeAfter,
    status:     r.status,
    result:     r.result,
  })),
}
