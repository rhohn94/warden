# Triager — reference
Loaded on demand by `SKILL.md`.

## §Label assignment

When labelling issues (step 3 of the grooming pass), the Triager assigns four
triage dimensions in addition to the standard `type` and `area` labels. Full
label definitions and allowed values: `docs/grimoire/design/issue-label-taxonomy.md`.

**Size** (`size:*`) — always assign. Use the token-band estimates from
`grm-release-planning` skill output when available. Default to `size:m` when size
is unknown. Maps to the token band used for model/effort selection at dispatch.

| Label | Token band |
|---|---|
| `size:xs` | ≤ 15K tokens |
| `size:s` | 15K–40K tokens |
| `size:m` | 40K–80K tokens (default) |
| `size:l` | 80K–200K tokens |
| `size:xl` | > 200K tokens |

**Complexity** (`complexity:*`) — assign when the solution approach is
reasonably clear. Default to `complexity:moderate` when uncertain. Maps to the
model tier selected at dispatch.

| Label | Description |
|---|---|
| `complexity:simple` | Clear scope, well-understood implementation |
| `complexity:moderate` | Some design judgment needed (default) |
| `complexity:complex` | Architectural tradeoffs or multi-system impact |
| `complexity:research` | Problem/solution not yet clear; requires spike first |

**Component** (`component:<name>`) — assign based on which system areas the
issue touches. Use the project's configured component labels (seeded at
onboarding). Assign multiple component labels if the issue spans more than one
area — an issue touching two components gets both labels. Used by the
integration master at parallel-dispatch planning to detect file-set overlaps.

**Priority** (`priority:*`) — always assign. Default is `priority:normal`.
Escalate to `priority:critical` or `priority:high` only with explicit
justification stated in a label comment or triage note.

| Label | Description |
|---|---|
| `priority:critical` | Blocks release; immediate attention |
| `priority:high` | Important; current sprint |
| `priority:normal` | Default; next available slot |
| `priority:low` | Nice-to-have; backlog |
| `priority:very-low` | Someday/maybe; lowest priority |

Labels from this section are subject to the same anti-patterns as all triage
labels (§8): apply only labels from the project's existing label set; never
invent new labels without the spawn prompt authorizing it. The protected-label
carve-out in §9 applies equally — `Grimoire-Requirement` issues must never be
downgraded in priority.

---

## §Epic creation

When a set of **3 or more related issues** is identified during a triage pass,
consider creating an Epic to group them under a shared goal.

**Decision rule:** create an Epic when the issues share a clear common goal that
a single umbrella entry would clarify for planning. Below 3 issues, shared labels
are sufficient.

**How to create the Epic:**

```python
epic = tracker.create(
    title="[EPIC] <Goal name>",
    body="## Overview\n...\n## Requirements\n...\n## Acceptance Criteria\n...",
    issue_type="epic",
)
```

The `epic` label is auto-applied. The Epic itself should carry:
- The **milestone label** (same as its child issues, or the milestone of the
  first/majority child).
- The relevant **type/area labels** shared by the child issues.
- A **priority label** (`priority:high` or above if any child is high-priority).

**Linking child issues:** after creating the Epic, update each child issue to
record its relationship by setting `parent_epic_id` to the Epic's ID. This is
done via `tracker.update()` or by flagging the links in the grooming summary for
the integration master to apply at dispatch time.

**One-level rule:** Epics cannot be children of other Epics. Never set
`parent_epic_id` on an Epic — the abstraction enforces this and raises a
validation error. Child issues (plain type) may have `parent_epic_id` set.

**Report:** list any Epics created in the grooming summary under "Epics
created — links to establish".

---

## §Milestone assignment

Immediately after §Label assignment and §Epic creation, and **before any issue is
moved to Ready state**, the Triager must assign a milestone label to every triaged
issue.

**Rule:** every triaged issue MUST receive a `milestone:vX.Y` label before it is
considered Ready for dispatch. If no milestone is clearly applicable, assign
`milestone:backlog`.

**How to pick the target milestone:**

1. Read `docs/roadmap.md` §v{X.Y} for the currently in-flight release version to
   understand that release's scope.
2. Ask: does this issue fit the current version's scope and capacity? If yes →
   assign `milestone:v{X.Y}` (e.g. `milestone:v3.36`).
3. If the issue is out of scope for the current release but a future version is
   clear → assign `milestone:v{NEXT}`.
4. If the issue is undated or has no clear version target → assign
   `milestone:backlog`.

**Label format:** the label name is exactly `milestone:vX.Y` using the full
version number (e.g. `milestone:v3.36`, `milestone:v3.37`). For undated items
use `milestone:backlog`. Never use partial versions or freeform strings.

**Enforcement:** issues lacking a milestone label MUST NOT be dispatched by the
integration master. The `release-phase/SKILL.md` pre-dispatch step enforces this
as a hard gate — the integration master checks every planned issue for a
`milestone:vX.Y` label matching the current release before dispatching any item,
and stops with an error if any issue is unlabeled.

**Anti-patterns:**
- Assigning `milestone:backlog` to an issue that clearly belongs to the in-flight
  release — prefer the specific version label.
- Leaving an issue without any milestone label at triage completion — this will
  cause the integration master to halt dispatch.
- Inventing milestone labels that do not follow the `milestone:vX.Y` /
  `milestone:backlog` format.

---

## §5 — Taxonomy placement

The Triager is the **fourth narrow-write role** in the agent taxonomy:

| Role | Session type | Context width | Git writes | Issue writes | Spawned by |
|---|---|---|---|---|---|
| Task agent | Work-item session | Medium–large | Yes (own branch) | No (flags via master) | Integration master |
| Integration master | Orchestration session | Medium | Merge only | Via Reporter or direct | Human / Noir |
| Reporter | Feedback-filing session | Narrow | No | Yes — configured tracker | Integration master / human / any |
| **Triager** | Grooming session | Narrow | No | Yes — configured tracker | Integration master / human / any |
| Reviewer | Review session | Narrow | No | No (findings → Reporter) | Integration master, pre-merge |
| Verifier | Verification session | Narrow | No | No (failures → Reporter) | Integration master, pre-merge |
| Scout | Investigation session | Narrow | No | No | Integration master / task agent |
| Researcher | Research + filing session | Medium | No | Yes — one scoped item | Integration master (or from Reporter) |

Full taxonomy: `docs/grimoire/design/agent-roles-design.md` §A.

The Triager is **not** a paradigm role — it is available in Supervised, Weiss,
and Noir. Under Noir it auto-spawns when the master detects tracker drift;
under Supervised and Weiss it is always user-confirmed. Full model: §6.

---

## §8 — Anti-patterns

- **Closing items that merely appear idle.** Age alone is not evidence of
  staleness. Closing requires positive evidence: the feature shipped, the bug
  was confirmed fixed, the item is an exact duplicate. Never close "I haven't
  seen anyone mention this in a while."

- **Inventing new labels.** Apply only labels from the project's existing label
  set (or labels explicitly authorized in the spawn prompt). Creating new labels
  changes the tracker's schema; that is an integration-master decision, not a
  grooming action.

- **Grooming on a `version/*` branch when the tracker is `roadmap`.** The
  roadmap backend writes `docs/roadmap.md`; writing it on a release staging
  branch creates merge conflicts. Run the Triager with HEAD on `dev` when the
  roadmap backend is configured.

- **Running unbounded under Noir.** A Noir Triager must have explicit bounds in
  the spawn prompt. An unbounded Noir Triager that closes any item it judges
  stale, at any time, can destroy legitimate in-flight work. Always state the
  allowed action set and any exclusion predicates.

- **Keeping the Triager alive between unrelated grooming runs.** The Triager is
  one-shot. Perform the pass, flush, return the summary, exit. Idling between
  unrelated grooming requests wastes tokens on a stale context.

- **Re-querying the tracker after every write.** The session-snapshot cache is
  invalidated *post-flush*, not per queued write. Load once, queue mutations,
  flush once, done.

- **Duplicating issue-tracker logic.** The Triager is a *consumer* of the
  issue-tracker abstraction. It never reimplements routing, caching, dedup key
  logic, or `gh`-call construction. Those are the abstraction's concern.

---

## §6 — Per-paradigm behaviour

**Supervised:** each Triager spawn is confirmed by the user via the standard
`spawn_task` confirmation gate. Within the session the Triager **proposes** each
grooming action (close, label, priority update) and presents a diff-style summary
before flushing writes. No mutations are applied until the Triager presents its
full proposed changeset to the integration master's return channel and the human
reviews it.

**Weiss (Collaborative):** the integration master *offers* to spawn a Triager
and waits for user confirmation. The user decides when grooming runs. Within
the session the Triager **applies** grooming actions that fall within clearly
mechanical scope (e.g. labelling items that have no labels, closing exact
duplicates) and **proposes** anything requiring judgement (e.g. priority
assignments, stale-close decisions).

**Noir (Autonomous):** the integration master spawns the Triager autonomously —
no per-spawn confirmation — typically at the end of a release phase or as a
scheduled grooming cadence (once Daily Routines ship). Within the session the
Triager **applies all grooming actions within configured bounds** stated in the
spawn prompt: close confirmed-stale items, apply labels from the allowed set,
apply priority labels where signal is clear. Actions outside the configured
bounds are listed in the summary as "out-of-bounds — deferred" rather than
applied. The Triager under Noir must be explicitly *bounded* in the spawn
prompt — an unbounded Noir Triager that closes or labels anything it judges
fit is an anti-pattern.

**In all paradigms:** the Triager never pushes to origin — that stays
human-gated. The Triager is **not** a release-phase agent; it does not own
release scope, merge branches, or tag commits.

---

