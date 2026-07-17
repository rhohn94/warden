---
name: grm-release-phase
description: Spawn work-item sessions (via spawn_task) for the next open phase of the in-flight release. Groups work by dependency, sizes each item by token estimate, and assigns model/effort per the `grm-repo-reference` skill table. Use when the user says "start phase N", "kick off phase", or "distribute phase work". Run after release-agreement has locked the plan.
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
> .claude/skills/grm-release-agent-tracker/release_plan.py plan-phase`. Design:
> `docs/design/grimoire-release-server-design.md`.

**`release-phase-model` dial.** The master reads `release-phase-model.value`
live before dispatching. When it is `Default` (or absent), dispatch the phase
via the `spawn_task` flow below. When it is **`Auto`** (Noir only — otherwise
fall back to `Default` and log the downgrade), dispatch the phase's items
instead via a **write-capable Workflow**, whose isolated-worktree agents each
implement one item and return a branch; the returned branches are then merged
in `mergeAfter` order by `grm-release-phase-merge`. `Auto` reuses the existing
write-capable tier — no new machinery — and the execution variant still comes
from `workflow-variant`. See the integration-master §`release-phase-model` dial
and `docs/design/release-phase-model-design.md`.

---

## Step 1 — Locate the active plan and current phase

```bash
ls docs/release-planning/release-planning-v*.md
```

Pick the highest-version file with `status: agreed` (check first 15 lines).
Read §3 (pass structure + conflict map) and §5 (ledger) to determine:

- **Current phase** = the first pass whose rows are all ☐ Implemented.
- If a phase is partially done (some ☑, some ☐), it is still the current
  phase — only spawn the ☐ rows.
- If all passes are ☑, there is nothing to spawn; move to
  `grm-release-phase-merge` for the final `version/{X.Y}` → `dev` step.

---

## Step 2 — Group the phase into parallel batches

Read §3's conflict map. Items are in the same batch if:

1. Neither depends on the other's output (no shared files that must serialise), AND
2. They are marked as parallel in §3's merge order.

Each batch is a set of work items that can run concurrently in separate
worktrees. If §3 has no explicit conflict map, apply the conservative default:
one item per batch (fully serialised).

---

## Step 2.5 — Choose the dispatch posture (execution-strategy)

Read `workflow-variant.value` from `.claude/grimoire-config.json`
(absent/unset → `Efficient`; match case-insensitively; treat legacy
`Careful-Serial` as `Cheap-Slow`).

| execution-strategy | Dispatch posture |
|---|---|
| **Fast** | Max fan-out — spawn every independent item in the current batch concurrently. Minimum wall-clock. |
| **Efficient** | Balanced (today's default) — conflict-map batches, shared brief dedup, `mergeAfter` ordering. |
| **Cheap-Slow** | Low fan-out — cap concurrent spawns to ~2–3; sub-split wider batches into sequential small batches. |

**Cheap-Slow regime selection and the three-dial orthogonality rule** are in
`reference.md` §Step 2.5. Record the chosen posture in the Step 4 preview.

> **Three independent reads.** `workflow-variant` (fan-out/isolation) ·
> `model-effort-profile` (tier) · `work-paradigm` (Noir ceiling). They compose
> and never derive from one another.

---

## Step 3 — Assign model and effort

For each item, resolve `{model, effort}` through the **active model/effort
profile** — do not hard-code a table here. Use the resolver from the
**`grm-repo-reference`** skill (§Subagent model & effort → The resolver):

1. Read `model-effort-profile.value` from `.claude/grimoire-config.json`
   (absent/unset → `Medium`).
2. Classify into a complexity band from token estimate + design/review flag
   (trivial ≤ 15 K · small 15–40 K · medium 40–80 K · large > 80 K · review).
3. Look up `profiles[<active>][<band>]` in `.claude/model-effort-profiles.json`.
   UX-pin items (`grm-design-language-adapt`, `grm-ux-demo-build`) keep fixed pins.

When in doubt, err toward sonnet. `spawn_task` cannot set the spawned session's
model, so carry the resolved tier into the chip title + prompt.

**Step 3a — Noir dispatch ceiling and `opus-required` escape hatch:** load
`reference.md` §Step 3a only when `work-paradigm.value` is `Noir`.

---

## Step 3.5 — Validate milestone labels (hard gate)

Verify every planned issue for the current release carries a `milestone:vX.Y`
label before dispatching. Check via the issue-tracker abstraction; full code
examples in `reference.md` §Step 3.5.

**Gate behaviour:** if any planned issue is missing the label, **STOP** — output
a clear error listing each unlabeled issue and instruct the user to run the
Triager with milestone-assignment scope before re-running. `milestone:backlog`
also blocks. Do not dispatch until all planned issues are labeled.

---

## Step 4 — Confirm before spawning (Supervised gate)

Before calling `spawn_task`, present the batch to the user:

- **Lead with the dispatch posture** (Step 2.5): active execution-strategy and
  what it does to this batch (e.g. `Efficient → balanced, 3 items concurrent`).
- List each item: ID, title, recommended model, branch name.
- Ask: "Spawn these N items now?"

Wait for explicit confirmation. Do not spawn until the user says yes.

---

## Step 5 — Spawn each item with `spawn_task`

For each item in the current batch, call the **`spawn_task`** tool
(`mcp__ccd_session__spawn_task`). The spawned session has no memory of this
session, so the `prompt` must be self-contained.

**First, materialize the shared brief + per-item packs as files (once per
batch) — brief-as-file, not brief-as-prompt-text (#397):**

```bash
python3 .claude/skills/grm-release-phase/context_pack.py phase-brief \
  --plan docs/release-planning/release-planning-v{X.Y}.md \
  --version {X.Y} --phase {N} --items {ITEM-ID},{ITEM-ID},...

python3 .claude/skills/grm-release-phase/context_pack.py context-pack \
  --plan docs/release-planning/release-planning-v{X.Y}.md \
  --version {X.Y} --phase {N} --item {ITEM-ID}
# repeat context-pack once per item in the batch
```

This writes `.claude/release-dispatch/v{X.Y}/phase{N}/brief.md` (the shared
digest — standards excerpt, §3 conflict-map, release theme — written **once**
regardless of batch size) and one `.claude/release-dispatch/v{X.Y}/phase{N}/{ITEM-ID}.md`
per item (that item's `### {ITEM-ID}` block extracted verbatim from the plan
via `grm-doc-section`, never re-typed). **Commit both to `version/{X.Y}`**
(the master's own current branch) before dispatching — each spawned agent's
`git switch -c {branch} version/{X.Y}` then inherits the files for free, no
prompt-text inlining and no cold read of the full plan file required. Full
rationale + the cross-worktree-visibility reason this must be a tracked
commit (not `.claude/cache/`): `reference.md` §Shared-context dispatch.

- **title**: `[{model}/{effort}] {ITEM-ID}: {short title}` — lead with the
  resolved tier tag (lowercase; e.g. `[opus/high] E7: …`, `[sonnet/inherit]
  E3: …`). `spawn_task` cannot set the session's model, so the tag is the
  carrier; keep the "set this model/effort" line in the prompt body too.
- **tldr**: one plain-English sentence on what the session will do.
- **prompt**: the self-contained block below.

```
## Task: {ITEM-ID} — {short title}
Recommended model: {model} | effort: {effort} — set this in your session before starting.

You are running in your own fresh, isolated worktree. Stay in it.
Worktree + git protocol: read CLAUDE.md §Worktree isolation and §Commits.

### Root your worktree on the release-staging ref
Run the `grm-worktree-preflight` skill first, then:

    git switch -c {branch-name} version/{X.Y}

Verify (must print ROOT-OK):
    [ "$(git merge-base HEAD version/{X.Y})" = "$(git rev-parse version/{X.Y})" ] && echo ROOT-OK || echo ROOT-BAD

`grm-worktree-preflight`'s Step 0.5 (parent sync) runs right after ROOT-OK — if
your branch is behind `version/{X.Y}`, sync-merge it in now, before touching
code. Re-run the whole preflight, Step 0.5 included, if this session is
**resumed** later rather than freshly spawned.

### Shared context
Read `.claude/release-dispatch/v{X.Y}/phase{N}/brief.md` for the standards
excerpt, this batch's §3 conflict-map slice, and the release theme — shared
across every item in this batch, already in your worktree (committed to
`version/{X.Y}` before you were spawned).

### Your item context
Read `.claude/release-dispatch/v{X.Y}/phase{N}/{ITEM-ID}.md` for this item's
full scope and acceptance criteria — the exact `### {ITEM-ID}` block from the
release plan, extracted verbatim. Also cross-linked:
- docs/design/{feature}-design.md — full feature design
- {any other design doc cross-linked in the plan}

### Constraints
- Scope strictly to the files listed in §2.{N}. Do not touch
  docs/release-planning/release-planning-v{X.Y}.md or the
  `.claude/release-dispatch/` pack files — they're dispatch scaffolding, not
  part of your deliverable. Exception: adding a `vendor.toml` entry and
  running `recipe.py sync-deps` for a cataloged capability is always in
  scope — it's the alternative to writing new files, not scope creep.
  Nothing else about scope loosens.
- Write or extend the design doc for this item if §2.{N} flags one missing.
- Run `recipe.py test` and `recipe.py build` (the `grm-build-recipe`
  dispatcher: `python3 .claude/skills/grm-build-recipe/recipe.py <target>`)
  before finishing.
- Fix all errors and warnings introduced by your changes.
- Attach verify-evidence appropriate to this item's type (see Step 5.5) —
  green tests/build alone are not sufficient done-criteria.
- Review your own diff against the acceptance criteria before reporting done.

### When done
Do NOT merge. Report back:
1. The branch name you worked on
2. Test result (pass / N failures)
3. Verify-evidence per Step 5.5's item-type map (or "docs-only, no runtime
   surface" if exempt)
4. One-paragraph summary of what was implemented
5. Any deferred follow-ups discovered (gaps left for a future item)
```

No manual substitution needed — the dispatcher resolves both commands from
`.claude/recipes.json` at run time. `context_pack.py` resolves `{ITEM-ID}` /
`{X.Y}` / `{N}` from the arguments it's called with; nothing to hand-edit in
the generated pack files themselves (they're marked generated, regenerate
instead of editing).

---

## Step 5.5 — Verify-evidence done-criteria (per item type, #428)

Green tests and a clean build are **necessary but not sufficient** — transcript
evidence shows agents repeatedly reporting "done" against a surface that was
never actually exercised (a served route that 404s, a UI flow that never
mounted). A work item's done bar therefore includes runtime verify-evidence
scaled to its item type, embedded in the Step 5 spawn prompt's "### When done"
block (item 3, above):

| Item type | Required verify-evidence |
|---|---|
| Served route / API endpoint | Probe the actual changed route with realistic input (`curl` or `recipe.py smoke`) and paste the response (status + body excerpt) into the completion report. |
| UI / served page (GUI feature, #362) | Run `recipe.py gui-test` — for web, that verb documents the agent's own Preview-driven interaction pass (`preview_start`/`navigate`/`read_page`/`computer`/`read_console_messages`) as the actual evidence, attached to the completion report; for desktop, `gui-test` must exit 0 against a real committed baseline. `just smoke-visual` (or the project's headless-browser smoke) remains the branch-level pixel-diff floor underneath it. See `docs/grimoire/design/runtime-verification-design.md` §GUI testing. |
| CLI command | Run the command against a fixture; paste the observed output showing the fixed/new behavior. |
| Library / internal API | Add or run a consumer-shaped example/integration test (not a unit test of the function in isolation) and cite it. |
| Design-doc-only / docs-only | **Exempt** — nothing to run. State "docs-only, no runtime surface" instead of an evidence excerpt. |

Full contract and rationale: `docs/grimoire/design/runtime-verification-design.md`
§Per-item-type verify-evidence.

**Proportionality.** The requirement scales with the live `code-quality.audit-gate`
dial (`.claude/grimoire-config.json`): `block` makes missing verify-evidence a
hard failure at `grm-release-phase-merge` (refuse to merge without it); `warn`
logs the gap without blocking; `off` makes the row advisory only. Docs-only
items stay exempt regardless of the dial.

**Sampling, not re-verification.** The **QA agent** (`grm-agent-qa`) samples
attached verify-evidence at phase/release level rather than the master
re-running every check per item — see `grm-agent-qa/SKILL.md` §2.

---

## Step 6 — Spawn the batch, then wait

Spawn every item in the current batch (one `spawn_task` call each), then stop
and tell the user:

- How many chips were dropped and which items they cover.
- To open each chip, set the named model, and let the session run.
- To say "agent {branch-name} is done" when a session reports back, so
  `grm-release-agent-tracker` can mark it ☑ Implemented and queue it for merge.
- **Do not** spawn the next batch until the current batch is merged
  (`grm-release-phase-merge`) — later batches build on earlier merges.

---

## Step 6.5 — Mandatory post-dispatch assertion (batch gate, #423)

Before any branch in the batch is merged, run the combined post-dispatch
assertion **automatically** — this is a required step of every batch, not an
optional manual re-check:

```bash
python3 .claude/skills/grm-integration-master/verify_isolation.py \
  --batch-manifest <batch-manifest.json> \
  --staging-branch version/{X.Y}
```

`<batch-manifest.json>` is a JSON list with one entry per item in the batch,
`{"branch": "<feature-branch>", "result_file": "<path-or-null>"}` (`result_file`
is the saved raw agent-result text if captured, or `null` for items dispatched
via the serial-in-place fallback). The gate checks, in one pass:

1. **Footer presence** — each `result_file`'s agent result carries the
   `worktreePath:`/`worktreeBranch:` footer (footerless ⇒ probable in-place run).
2. **Master HEAD unchanged** — the integration master's HEAD is still on
   `version/{X.Y}`, checked once for the whole batch.
3. **Branch advanced** — each expected branch carries at least one commit
   beyond `version/{X.Y}` (don't trust the agent's self-reported "done").

Exit 0 = all checks passed, safe to proceed to `grm-release-phase-merge`. Exit
nonzero = **do NOT merge anything in this batch** — the command prints every
violation by name; repair per `docs/grimoire/design/dispatch-hardening-design.md`
§7 (footer/HEAD-drift recovery) before retrying.

---

## Anti-patterns (summary — full detail in `reference.md` §Anti-patterns)

- Spawning without user confirmation; handing the user raw copy-paste prompts
  instead of calling `spawn_task`.
- Including merge instructions in a spawned prompt (agents never merge);
  batching items that share files (check §3's conflict map).
- Forgetting the leading `[{model}/{effort}]` tier tag, or missing the "set
  this model/effort" line in the prompt body.
- Inlining the shared-context digest or an item's plan prose directly into a
  prompt instead of writing/committing `context_pack.py`'s brief + pack files
  and referencing them by path (#397) — the whole point is to pay for that
  content once, not N times.
- Spawning Batch 2 before Batch 1 is merged.
- Under Noir, dispatching non-review, non-`opus-required` work to Opus (Step 3a
  ceiling — see `reference.md`), or treating `opus-required` as a promotion.
- Treating Cheap-Slow as literal solo, or letting execution-strategy change the
  tier or vice versa — see `reference.md` §Step 2.5.
- Implementing in-session subagents for Cheap-Slow's small-heavy corner — N1 is
  deferred; use the small-batch `spawn_task` fallback.
- Accepting a "done" report with green tests/build but no verify-evidence for a
  non-docs-only item (Step 5.5), or merging before Step 6.5's post-dispatch
  assertion gate has exited 0.
