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

**First, synthesize the shared context brief (once per batch):** write a
**≤800-token digest** covering what agents would otherwise cold-read, and
embed it identically in every spawn prompt's `### Shared context (pre-digested)`
block. Each agent still reads its own §2.{N} scope in full.

**Brief contents** (agents do zero cold doc-reading on opening turns):

1. **Standards excerpt** — pointers to `docs/coding-standards.md` and
   `docs/architecture-guidelines.md` plus standing constraints (test/build
   commands, project-structure rules).
2. **Conflict-map slice** — the §3 rows for this batch: shared files, parallel
   items, `mergeAfter` ordering (so agents skip reading §3 themselves).
3. **Acceptance-criteria summary** — release theme + criteria common to the
   batch; unique-to-one-item criteria go in the per-item delta.
4. **Doc-location pointers** — `grm-repo-reference` paths for the relevant
   design docs (e.g. `docs/design/{feature}-design.md`).

Keep it compact (≤800 tokens total) — a cache-hit lever that does **not**
relax worktree isolation. Each agent gets its **per-item delta** separately.

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

### Shared context (pre-digested)
{The ≤800-token batch digest: standards excerpt, conflict-map slice for this batch, acceptance-criteria summary, grm-repo-reference doc-location pointers. Identical across the batch. Replaces cold doc-reading. You still read your own §2.{N} scope below.}

### Context — read before touching code
- docs/release-planning/release-planning-v{X.Y}.md §2.{N} — this item's scope + acceptance
- docs/design/{feature}-design.md — full feature design
- {any other design doc cross-linked in the plan}

### Work
{Exact item description copied from the release plan §2.{N}.
Include acceptance criteria verbatim.}

### Constraints
- Scope strictly to the files listed in §2.{N}. Do not touch
  docs/release-planning/release-planning-v{X.Y}.md.
- Write or extend the design doc for this item if §2.{N} flags one missing.
- Run `{test-command}` and `{build-command}` before finishing.
- Fix all errors and warnings introduced by your changes.
- Review your own diff against the acceptance criteria before reporting done.

### When done
Do NOT merge. Report back:
1. The branch name you worked on
2. Test result (pass / N failures)
3. One-paragraph summary of what was implemented
4. Any deferred follow-ups discovered (gaps left for a future item)
```

Replace `{test-command}` and `{build-command}` with your project's actual
commands (see CLAUDE.md §Project commands).

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

## Anti-patterns (summary — full detail in `reference.md` §Anti-patterns)

- Spawning without user confirmation; handing the user raw copy-paste prompts
  instead of calling `spawn_task`.
- Including merge instructions in a spawned prompt (agents never merge);
  batching items that share files (check §3's conflict map).
- Forgetting the leading `[{model}/{effort}]` tier tag, or oversizing/skipping
  the ≤800-token shared context brief, or missing the "set this model/effort"
  line in the prompt body.
- Spawning Batch 2 before Batch 1 is merged.
- Under Noir, dispatching non-review, non-`opus-required` work to Opus (Step 3a
  ceiling — see `reference.md`), or treating `opus-required` as a promotion.
- Treating Cheap-Slow as literal solo, or letting execution-strategy change the
  tier or vice versa — see `reference.md` §Step 2.5.
- Implementing in-session subagents for Cheap-Slow's small-heavy corner — N1 is
  deferred; use the small-batch `spawn_task` fallback.
