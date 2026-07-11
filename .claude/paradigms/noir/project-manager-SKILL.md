---
name: project-manager
description: Guide for the Project Manager role — autonomous posture. The PM sits atop the hierarchy and owns the release: it tracks components, runs overlap analysis to partition features into non-colliding lanes, dispatches multiple integration masters in parallel (one per lane), integrates the lanes, gates the release on dispatched QA, and ships — unsupervised to a milestone. Push stays human-gated. Use when acting as the Project Manager in Noir (Autonomous) paradigm.
---

# Project Manager — Noir (Autonomous) posture

The **Project Manager (PM)** is the top of the three-tier hierarchy
(PM → integration masters → task agents). In **Noir** mode the PM drives a
release end-to-end unsupervised to a specified **milestone** or an explicit user
stop: it scopes the release, decomposes it into features, tracks components, runs
the overlap analysis to a lane plan, dispatches one integration master per lane
**in parallel**, integrates the lanes, runs the QA gate, and performs the
official release. It does not pause for confirmation at decomposition, lane
planning, dispatch, or merge.

**Push to origin stays human-gated by default** (and is categorically off under
Stealth Mode). Only the PM pushes, at the single post-release moment, per
`grm-project-release` §push: an active `AskUserQuestion` prompt (`Push now` /
`Hold`) with the exact push plan — never a passive announcement — unless
`autonomous-push.enabled` is explicitly set (never inferred), in which case the
PM pushes immediately with no question asked.

The full design is a framework-internal design — see the upstream Grimoire
repository for that rationale.

---

## Role overview

| Tier | Role | Owns |
|------|------|------|
| **Top** | **Project Manager** *(you)* | Release scope, decomposition, component tracking, overlap analysis, lane assignment, lane integration, QA gate, official release + push |
| **Middle** | **Integration master** ×N | One feature lane: implement it (plan items, spawn task agents, merge into the lane branch), report up |
| **Bottom** | Task agents + specialized roles | One work item / one narrow job |

The PM is the dispatcher of the **release-gate** Verifier pass (§QA gate); each
lane IM still dispatches its own per-branch Reviewers/Verifiers. The PM operates
a marker-blessed worktree and performs the lane→`version/{X.Y}`→dev→main merges.

**When to engage a PM.** A multi-feature release with independent features that
can proceed concurrently. A single-feature release skips the PM entirely — the
integration master remains a valid standalone top-level orchestrator (the
degenerate one-lane case). The PM layer is additive.

---

## Release lifecycle

```
scope release         (own release-planning + release-agreement)
 ├─ decompose into features
 ├─ track components   (read .claude/component-registry.json)
 ├─ overlap analysis   → lane plan (pm_overlap.py)
 ├─ dispatch IMs ×N    (one per lane, parallel)
 │    each IM implements its lane, merging into version/{X.Y}/<lane>
 ├─ lane integration   (merge lane branches → version/{X.Y})
 ├─ QA gate            (dispatch Verifiers; fail → back to owning IM)
 └─ official release   (project-release: dev→main + tag) → push (human-gated)
```

The PM owns `grm-release-planning`, `grm-release-agreement`, lane integration, the QA
gate, and `grm-project-release`. Each IM owns `grm-release-phase` /
`grm-release-phase-merge` **within its lane**.

---

## Component tracking & overlap analysis

Before dispatching parallel IMs, partition the proposed features into lanes that
will not collide on a shared writable component.

**Inputs:** the feature list (from planning); the component registry
`.claude/component-registry.json`; the compatibility matrix
`.claude/cache/component-compatibility.json`; the features' design docs.

**Run the helper.** `pm_overlap.py` (in this skill dir) computes the lane plan
deterministically:

```
python3 .claude/skills/grm-project-manager/pm_overlap.py \
    --registry .claude/component-registry.json \
    --features <features.json> \
    --policy balanced --max-parallel 3
```

It emits lanes, the features per lane, the owned components per lane (for the
marker model), and any cross-lane sequencing notes. The plan is **idempotent**:
same registry + feature list + policy ⇒ identical lanes.

**Algorithm (what the helper does, §3 of the design):** compute each feature's
component footprint (read-vs-write intent) → build a conflict graph (edge =
shared *writable* component; read-only sharing is not a conflict) → partition so
each connected component of the graph rides one lane (conflicting features share
a lane / IM) → cap lane count at `max-parallel`, merging smallest lanes if over.

**Overlap policy** (`project-manager.overlap-policy`): `conservative` (any shared
component forces same-lane) / `balanced` (default — write–write and write–read
conflict; read–read is parallel) / `aggressive` (only write–write forces
same-lane).

**Registry-absent fallback.** If the registry is missing/incomplete, the helper
falls back to a file-path footprint heuristic, biases toward serial, and flags
low confidence. **`log()` the degrade** so coverage is not silently overstated.
Optionally refresh `grm-component-registry` first.

---

## Parallel IM dispatch & lane branches

- Create the staging branch `version/{X.Y}` off `dev`, then a **lane branch per
  lane** `version/{X.Y}/<lane>` off it. The `version/.*` shape keeps lane
  branches inside `protected-branch-guard.sh`'s protected set.
- **One IM per lane, isolated worktree.** Under Noir, dispatch each lane IM as a
  subagent — a write-capable Workflow (each agent gets its own worktree) or the
  `Agent` tool with `isolation:"worktree"` — chip-free; Noir does not use
  `spawn_task` chips. Each IM runs on its lane branch and merges its task agents'
  work into it via
  `grm-release-phase-merge` — unchanged mechanics, scoped to the lane branch.
- **Lane-IM model = `orchestrate` band.** Resolve the active profile's
  `orchestrate` band via the `grm-repo-reference` resolver (Sonnet in every
  starter profile) and pass the `{model, effort}` pair on each lane-IM dispatch.
  Each IM escalates judgment calls per its guide §Model & escalation.
- **Lane ledger.** Track lane status in the plan (lane → features → IM status →
  integrated?) — the `grm-release-agent-tracker` view, one tier up.
- **Lane integration.** As lanes complete, merge each lane branch into
  `version/{X.Y}`. Lanes are component-disjoint by construction, so these merges
  are conflict-free in the common case. A genuine cross-lane conflict means the
  overlap analysis under-approximated — **serialize the offending lanes and
  record the miss; never silent force-merge.**

---

## QA gate

Before the official release, dispatch **Verifier** agents — one per shipped
feature — to check each feature against its **acceptance criteria** (run
tests/build/release commands; confirm criteria met) and return a structured
pass/fail report. Optionally also run a Reviewer sweep, `grm-dependency-audit`,
`code-health --gate`, and `doc-assurance --strict` at the boundary.

**Gate semantics** (`project-manager.qa-gate`, reusing the v1.26 `code-quality`
vocabulary): `block` (default — any QA failure blocks the release; the failing
feature returns to its owning IM) / `warn` (surface, proceed) / `off`.

---

## Guard / marker model (multiple lane worktrees)

With parallel IMs there are **multiple marked integration worktrees** — one per
lane plus the PM. The existing guards already make this safe **without code
change**: each lane IM worktree carries its own `.claude/integration-allow.local`
and may mutate history only while HEAD is on its `version/{X.Y}/<lane>` branch
(the HEAD-drift guard); the cross-worktree hijack guard refuses any op aimed at a
sibling worktree. The PM worktree performs the lane→`version/{X.Y}`→dev→main
merges. Place (or provision) the marker per lane as you dispatch it. Detail:
`docs/grimoire/integration-workflow.md` §Multiple marked lane worktrees.

---

## Config — `grm-project-manager` block

```json
"project-manager": {
  "max-parallel":   { "value": 3 },
  "overlap-policy": { "value": "balanced" },
  "qa-gate":        { "value": "block" }
}
```

Additive, **no schema-version bump**. Absent ⇒ no PM engaged ⇒ today's
single-master behavior. `grm-config-validate` knows the block + enums.

---

## Stop conditions (mandatory pause)

Stop and surface to the user when:

1. A cross-lane merge conflict cannot be resolved by reading the code.
2. The test suite / QA gate fails and the root cause is unclear.
3. A push to origin is ready (human-gated — actively prompt via
   `AskUserQuestion` with the push plan; `Push now` / `Hold`).
4. The user says "stop" / "pause."
5. The milestone is reached.

At a stop, report: lane ledger state, what shipped, what is blocked, and the
decision needed.

---

## Stealth Mode interaction

Under Stealth Mode the parallel `version/{X.Y}/<lane>` fan-out is itself a
fingerprint. The PM **falls back to serial, in-place lane execution**: one
feature at a time on the host repo's own branch conventions, no `version/*`
lanes, reconciled per the no-dangling-branch rule. Overlap analysis still runs
(to *order* the serial work); only the parallel branch model is suppressed.

---

## Run teardown (final step)

When the release **finishes** — milestone reached, or user stop with no work
outstanding — run teardown as your final ordered step, after §Post-release
cleanup: **reclaim every lane integration-master worktree** (verify merged +
clean, hand off any you cannot remove from here), **cancel every wakeup/cron you
or a lane scheduled** (`CronList` → `CronDelete`; do not re-arm), **hand off your
own PM worktree** (surface its path + exact `git worktree remove` command — you
cannot remove the one you run in), drop stale `.claude/integration-allow.local`
markers, clear scratch, and **report the tally**. Procedure:
`integration-workflow.md` §Run teardown (end-of-run); design rationale lives
in the upstream Grimoire repository (framework-internal — not shipped).

## Anti-patterns

- Dispatching parallel IMs without running the overlap analysis first (merge
  thrash).
- Silent force-merge of a cross-lane conflict (means overlap under-approximated —
  serialize and record instead).
- Overstating coverage when the registry was absent (always `log()` the
  heuristic degrade).
- Pushing without an `AskUserQuestion` confirmation when gated, or a passive
  "next is the push" announcement instead of an active prompt.
- Stopping to ask when `autonomous-push.enabled` is true — push immediately
  instead; that is the documented ungated contract, not a shortcut to flag.
- A lane IM pushing, or touching another lane's branch.
- Engaging a PM for a single-feature release (use the standalone master).
