---
name: project-manager
description: Guide for the Project Manager role — supervised posture. The PM sits atop the hierarchy and owns the release: it tracks components, runs overlap analysis to partition features into non-colliding lanes, dispatches integration masters per lane, integrates the lanes, gates the release on QA, and ships — stopping for explicit user confirmation at decomposition, the lane plan, each dispatch, the QA verdict, and the release. Use when acting as the Project Manager in the Supervised (default) paradigm.
---

# Project Manager — Supervised posture

The **Project Manager (PM)** is the top of the three-tier hierarchy
(PM → integration masters → task agents). In **Supervised** mode the PM owns the
release but **stops for explicit user confirmation at every major gate**:
decomposition, the lane plan (overlap analysis), each IM dispatch, the QA
verdict, and the release. It scopes the release, decomposes it into features,
tracks components, partitions them into lanes, dispatches one integration master
per lane, integrates the lanes, runs the QA gate, and performs the official
release — pausing at each gate.

**Push to origin is human-gated** (propose the exact refs; wait for explicit
"push"). Only the PM pushes, at the single post-release moment.

Authority for the full design: `docs/design/project-manager-role-design.md`.

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
 ├─ dispatch IMs ×N    (one per lane)
 │    each IM implements its lane, merging into version/{X.Y}/<lane>
 ├─ lane integration   (merge lane branches → version/{X.Y})
 ├─ QA gate            (dispatch Verifiers; fail → back to owning IM)
 └─ official release   (project-release: dev→main + tag) → push (human-gated)
```

The PM owns `release-planning`, `release-agreement`, lane integration, the QA
gate, and `project-release`. Each IM owns `release-phase` /
`release-phase-merge` **within its lane**.

---

## Component tracking & overlap analysis

Before dispatching IMs, partition the proposed features into lanes that will not
collide on a shared writable component.

**Inputs:** the feature list (from planning); the component registry
`.claude/component-registry.json`; the compatibility matrix
`.claude/cache/component-compatibility.json`; the features' design docs.

**Run the helper.** `pm_overlap.py` (in this skill dir) computes the lane plan
deterministically:

```
python3 .claude/skills/project-manager/pm_overlap.py \
    --registry .claude/component-registry.json \
    --features <features.json> \
    --policy balanced --max-parallel 3
```

It emits lanes, the features per lane, the owned components per lane, and any
cross-lane sequencing notes. **Present the lane plan to the user and wait for
agreement before dispatching.** The plan is idempotent: same registry + feature
list + policy ⇒ identical lanes.

**Algorithm (what the helper does, §3 of the design):** compute each feature's
component footprint (read-vs-write intent) → build a conflict graph (edge =
shared *writable* component; read-only sharing is not a conflict) → partition so
each connected component of the graph rides one lane → cap lane count at
`max-parallel`, merging smallest lanes if over.

**Overlap policy** (`project-manager.overlap-policy`): `conservative` /
`balanced` (default) / `aggressive`.

**Registry-absent fallback.** If the registry is missing/incomplete, the helper
falls back to a file-path footprint heuristic, biases toward serial, and flags
low confidence. Surface the degrade to the user.

---

## Per-lane IM dispatch & lane branches

- Create the staging branch `version/{X.Y}` off `dev`, then a **lane branch per
  lane** `version/{X.Y}/<lane>` off it. The `version/.*` shape keeps lane
  branches inside `protected-branch-guard.sh`'s protected set.
- **One IM per lane.** Dispatch via `spawn_task` chips after user confirmation
  of the dispatch. Each IM runs on its lane branch and merges its task agents'
  work into it via `release-phase-merge`.
- **Lane ledger.** Track lane status in the plan (lane → features → IM status →
  integrated?) — the `release-agent-tracker` view, one tier up.
- **Lane integration.** As lanes complete, merge each lane branch into
  `version/{X.Y}`. Lanes are component-disjoint by construction, so these merges
  are conflict-free in the common case. A genuine cross-lane conflict means the
  overlap analysis under-approximated — **serialize the offending lanes and
  surface to the user; never silent force-merge.**

---

## QA gate

Before the official release, dispatch **Verifier** agents — one per shipped
feature — to check each feature against its **acceptance criteria** (run
tests/build/release commands; confirm criteria met) and return a structured
pass/fail report. Present the QA verdict to the user before the release.
Optionally also run a Reviewer sweep, `dependency-audit`, `code-health --gate`,
and `doc-assurance --strict` at the boundary.

**Gate semantics** (`project-manager.qa-gate`, reusing the v1.26 `code-quality`
vocabulary): `block` (default — any QA failure blocks the release; the failing
feature returns to its owning IM) / `warn` (surface, proceed) / `off`.

---

## Guard / marker model (multiple lane worktrees)

With multiple IMs there are **multiple marked integration worktrees** — one per
lane plus the PM. The existing guards already make this safe **without code
change**: each lane IM worktree carries its own `.claude/integration-allow.local`
and may mutate history only while HEAD is on its `version/{X.Y}/<lane>` branch
(the HEAD-drift guard); the cross-worktree hijack guard refuses any op aimed at a
sibling worktree. The PM worktree performs the lane→`version/{X.Y}`→dev→main
merges. Place the marker per lane as you dispatch it. Detail:
`docs/integration-workflow.md` §Multiple marked lane worktrees.

---

## Config — `project-manager` block

```json
"project-manager": {
  "max-parallel":   { "value": 3 },
  "overlap-policy": { "value": "balanced" },
  "qa-gate":        { "value": "block" }
}
```

Additive, **no schema-version bump**. Absent ⇒ no PM engaged ⇒ today's
single-master behavior. `config-validate` knows the block + enums.

---

## Decision gates (Supervised)

| Gate | What happens |
|------|-------------|
| **Decomposition** | Present the feature breakdown; wait for agreement. |
| **Lane plan** | Present the overlap analysis + lanes; wait for agreement before dispatch. |
| **IM dispatch** | List the lanes/IMs to spawn; ask before each dispatch. |
| **QA verdict** | Present the per-feature QA report; wait before releasing. |
| **Release + push** | Propose the exact refs; wait for explicit "push". |

Never skip a gate. A prior "go ahead" covers only the immediately pending gate.

---

## Stealth Mode interaction

Under Stealth Mode the parallel `version/{X.Y}/<lane>` fan-out is itself a
fingerprint. The PM **falls back to serial, in-place lane execution**: one
feature at a time on the host repo's own branch conventions, no `version/*`
lanes, reconciled per the no-dangling-branch rule. Overlap analysis still runs
(to *order* the serial work); only the parallel branch model is suppressed.

---

## Anti-patterns

- Dispatching IMs without running (and presenting) the overlap analysis first.
- Silent force-merge of a cross-lane conflict (means overlap under-approximated —
  serialize and surface instead).
- Skipping a decision gate because the user said "go ahead" earlier.
- Pushing without explicit confirmation.
- Engaging a PM for a single-feature release (use the standalone master).
