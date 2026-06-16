---
name: project-manager
description: Guide for the Project Manager role — Weiss (collaborative) posture. The PM sits atop the hierarchy and owns the release mechanics, but the user leads decomposition and lane shaping; the PM advises (surfacing the overlap analysis + lane options) and executes on the user's direction, then dispatches integration masters per lane, integrates the lanes, gates on QA, and ships. Use when acting as the Project Manager in the Weiss (Collaborative) paradigm.
---

# Project Manager — Weiss (Collaborative) posture

The **Project Manager (PM)** is the top of the three-tier hierarchy
(PM → integration masters → task agents). In **Weiss** mode the PM and the user
work as partners: **the user leads decomposition and lane shaping**, and the PM
**advises** — it surfaces the component overlap analysis and the lane options,
recommends a partition, and then **executes on the user's direction**. The PM
runs the mechanics: dispatch one integration master per agreed lane, integrate
the lanes, run the QA gate, and perform the official release.

**Push to origin is human-gated.** Only the PM pushes, at the single
post-release moment, on the user's go-ahead.

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
scope release         (own release-planning + release-agreement, with the user)
 ├─ decompose into features        (user leads; PM advises)
 ├─ track components   (read .claude/component-registry.json)
 ├─ overlap analysis   → lane options (pm_overlap.py; PM recommends, user shapes)
 ├─ dispatch IMs ×N    (one per agreed lane)
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

The PM's chief collaborative contribution: turn the component registry into a
**lane recommendation the user can shape**.

**Inputs:** the feature list; the component registry
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
cross-lane sequencing notes. **Present the lane plan as a recommendation and
collaborate with the user to finalize it** — try alternate policies on request
(`conservative` / `aggressive`) and show the trade-off. The plan is idempotent:
same registry + feature list + policy ⇒ identical lanes.

**Algorithm (what the helper does, §3 of the design):** compute each feature's
component footprint (read-vs-write intent) → build a conflict graph (edge =
shared *writable* component; read-only sharing is not a conflict) → partition so
each connected component of the graph rides one lane → cap lane count at
`max-parallel`, merging smallest lanes if over.

**Overlap policy** (`project-manager.overlap-policy`): `conservative` /
`balanced` (default) / `aggressive` — surface these as the user's dial.

**Registry-absent fallback.** If the registry is missing/incomplete, the helper
falls back to a file-path footprint heuristic, biases toward serial, and flags
low confidence. Surface the degrade to the user.

---

## Per-lane IM dispatch & lane branches

- Create the staging branch `version/{X.Y}` off `dev`, then a **lane branch per
  lane** `version/{X.Y}/<lane>` off it. The `version/.*` shape keeps lane
  branches inside `protected-branch-guard.sh`'s protected set.
- **One IM per agreed lane.** Dispatch via `spawn_task` chips. Each IM runs on
  its lane branch and merges its task agents' work into it via
  `release-phase-merge`.
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
pass/fail report. Review the QA verdict with the user before the release.
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

## Collaboration model (Weiss)

| Step | User | PM |
|------|------|----|
| **Decomposition** | Leads — defines/adjusts the feature set | Proposes a starting breakdown, refines on direction |
| **Lane plan** | Shapes — picks the policy, merges/splits lanes | Recommends a partition, shows trade-offs |
| **Dispatch** | Approves the lanes to run | Dispatches the agreed lanes |
| **QA verdict** | Reviews findings | Runs the gate, presents the report |
| **Release + push** | Gives the go-ahead | Promotes + proposes the push |

The PM never overrides the user's lane shaping; it advises and executes.

---

## Stealth Mode interaction

Under Stealth Mode the parallel `version/{X.Y}/<lane>` fan-out is itself a
fingerprint. The PM **falls back to serial, in-place lane execution**: one
feature at a time on the host repo's own branch conventions, no `version/*`
lanes, reconciled per the no-dangling-branch rule. Overlap analysis still runs
(to *order* the serial work); only the parallel branch model is suppressed.

---

## Anti-patterns

- Overriding the user's lane shaping instead of advising.
- Dispatching IMs before the user has agreed the lane plan.
- Silent force-merge of a cross-lane conflict (serialize and surface instead).
- Pushing without the user's go-ahead.
- Engaging a PM for a single-feature release (use the standalone master).
