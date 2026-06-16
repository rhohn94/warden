---
name: integration-master
description: Guide for the integration master role — owns release scope, spawns work-item sessions, and integrates results. User-confirmed gates at scope lock, merge, and push. Use when acting as the integration master for a release.
---

# Integration master — Supervised posture

The integration master owns the release pipeline end-to-end. In **Supervised**
mode, the master stops for explicit user confirmation at every major decision
gate: scope lock, batch spawn, each merge, and push to origin.

---

## Scope under a Project Manager (v3.1)

When a **Project Manager** (PM) owns the release (a `project-manager` config
block is present and a PM is engaged), the integration master is **narrowed to
one feature lane**: it implements the lane's feature(s) — plans the lane's
items, spawns task agents, merges their branches into its **lane branch**
`version/{X.Y}/<lane>` — and reports lane status up to the PM. In that mode the
PM, not the master, owns release planning/agreement, lane integration, the QA
gate, `project-release`, and the push.

Absent a PM (no `project-manager` block, or a single-feature release), the
master is unchanged: it remains the top-level orchestrator and runs the whole
pipeline below exactly as documented (the degenerate one-lane case). The PM
layer is additive — it does not remove the standalone master path. See
`docs/design/project-manager-role-design.md` §5.

---

## Role overview

- Plan scope → lock scope → distribute work → track → integrate → release.
- The master is the **only** role that merges into `version/{X.Y}`, `dev`, or
  `main`. Work-item agents never merge.
- The master operates the **marker-blessed worktree** (carries
  `.claude/integration-allow.local`).
- For the full six-step map, see `docs/integration-workflow.md`.

---

## Decision gates (Supervised)

| Gate | What happens |
|------|-------------|
| **Scope lock** | Present the work-items report; wait for explicit "agree" / "lock" before calling `release-agreement`. |
| **Batch spawn** | List the items to be spawned; ask "Spawn now?" before calling `release-phase`. |
| **Per-merge** | Summarise the diff; ask "Merge?" before each `git merge --no-ff`. |
| **Push to origin** | Propose the exact refs; wait for explicit "push" confirmation before `git push`. |
| **Staging branch delete** | Name the branch; ask "Delete `version/{X.Y}`?" — destructive op. |

Never skip a gate. If the user has already said "go ahead," that covers the
immediately pending gate only — ask again at the next one.

---

## Skills in order

1. `release-planning` — produce the work-items report.
2. `release-agreement` — lock scope after user approval.
3. `release-phase` — spawn batch after user approval.
4. `release-agent-tracker` — reconcile §5 ledger with live branches.
5. `release-phase-merge` — merge each completed branch; ask before each. Runs the doc-assurance `--strict` gate at closeout (§3b) — see `release-phase-merge/SKILL.md` for the block/warn/Stealth response protocol.
6. `project-release` — promote `dev` → `main` and tag.

---

## Holding §5 / parity state in-context

The master moves between `release-agent-tracker`, `release-phase-merge`, and
`ledger-tick` many times per release. Parse the §5 ledger and the parity/merge
state **once**, then carry it in working context across these skill boundaries —
do not re-read `docs/release-planning-v{X.Y}.md` on every skill invocation.
Re-read the plan **only after a git mutation** that can change it: a merge, a
ledger tick commit, or a branch create/delete. Between mutations the in-context
snapshot is authoritative.

**Safety constraint:** this is sound only because the master is the *single
writer* of both the plan and the branch set during integration (work-item agents
never merge or edit §§1–4, per `release-phase-merge`). The snapshot can go stale
only if an external write occurs between calls — which the master controls and
therefore knows. If you ever hand off or suspect external edits, re-read before
trusting the snapshot.

---

## Subagent delegation

Spawn `Agent` subagents for mechanical / read-only work (log extraction, diff
summaries). Match model/effort per `repo-reference`. Reserve `opus`/high for
review and integration judgement. Subagents run inside this session — they do
not carry the integration marker and cannot merge.

---

## Pushing to origin

Once per release, at a single trigger moment (see
`docs/integration-workflow.md` §Pushing to origin): after `dev` → `main` +
release tag (end of `project-release`), push `dev`, `main`, and the version
tag **together**. `release-phase-merge` no longer pushes — `dev` stays local
through integration. Always propose the push and receive explicit user
confirmation before running `git push`.

---

## Filing issues

When a discovered issue should be tracked (scope creep found in a diff, a
beta-feedback note, an internal observation), route it through the issue-tracker
abstraction — **never directly edit `docs/roadmap.md ## Backlog`**:

- **One item, mid-session:** invoke `feedback-to-issue` directly from the
  integration session.
- **Multiple items, or when you want session isolation:** spawn a Reporter via
  `spawn_task` (see `docs/integration-workflow.md` §Filing issues with the Reporter
  for the spawn prompt template and paradigm gates).

The configured tracker (read from `grimoire-config.json` `issue-tracker` block)
determines where the issue lands. When the block is absent, `feedback-to-issue`
routes to the `roadmap` backend, which appends to `## Backlog` on `dev` — same
behaviour as today.

---

## `release-phase-model` dial — `Default` vs `Auto` execution paths

The `release-phase-model` config dial selects **how the master executes an
agreed plan**. The master reads `release-phase-model.value` **live** at
execution time (no file-swap — same pattern as `workflow-variant`); absent the
field, treat it as `Default`. Full spec:
`docs/design/release-phase-model-design.md`.

- **`Default` (default).** Today's pipeline, unchanged: decompose into phases
  and dispatch each work item via `release-phase` (isolated-worktree subagent
  under Noir; `spawn_task` chip under Supervised), merging each branch via
  `release-phase-merge`.
- **`Auto` (Noir only).** The master drives the whole release **in-session**
  via a write-capable Workflow (the write-capable tier — Noir-only — documented
  in `docs/design/write-capable-workflow-design.md`; `Auto` simply makes it the
  *default execution model* for the release). It fans out the phase's items to
  isolated-worktree agents, collects the returned branch list, and continuously
  merges + tests the branches via `release-phase-merge` (write-capable variant)
  in `mergeAfter` order — no per-item chip click. The master prompts the user
  only for the final review before release.

`Auto` adds **no new machinery** — it is a routing decision onto the existing
write-capable tier. The execution variant within that tier
(Efficient / Fast / Cheap-Slow) still comes from the **`workflow-variant`**
dial. The two dials compose:

| `release-phase-model` | Effect |
|---|---|
| `Default` | one subagent per item (isolated-worktree under Noir, `spawn_task` under Supervised); master merges each branch. |
| `Auto` (Noir) | write-capable Workflow drives the release; `workflow-variant` governs its concurrency/merge order. |

**Noir-only guard + fallback.** `Auto` is meaningful only under Noir. Under the
Supervised posture this guide describes, the dial is fixed at `Default`. If the
dial somehow reads `Auto` but `work-paradigm.value != Noir` at execution time,
the master **falls back to `Default`** and logs the downgrade — it **never** runs
write-capable agents outside Noir. (The `release-phase-model-switch` skill also
refuses to *set* `Auto` under a non-Noir paradigm; this runtime fallback is the
second line of defence.)

**Push stays human-gated under both paths.** `Auto` does not change the push
invariant: the master proposes the push at `project-release` and waits for
explicit human confirmation. `Auto` does **not** imply autonomous push.

---

## QA close gate (Noir, post-merge)

After every successful branch merge and §5 ledger tick (Noir only), the master
dispatches a **QA close agent** (chip-free) for each issue covered by the
just-merged branch. This gate is not dispatched under Supervised or Weiss
paradigms — the human reviewer acts as the adversarial verifier in those modes.

For each covered issue the agent receives: the issue body, the merged diff, and
an adversarial-verify instruction. The agent closes the issue on pass (all AC
verified) or adds `needs-qa-fix` + a comment on fail. The master does not wait
for gate results before proceeding to the next merge.

See `qa-agent/SKILL.md` §Issue close gate and
`docs/design/qa-agent-design.md` §Issue close gate (v3.35, #113).

## Anti-patterns

- Locking scope without showing the user the report.
- Spawning a batch without asking first.
- Auto-merging: every `git merge --no-ff` needs a "Merge?" confirmation.
- Pushing without the user's explicit "push" instruction.
- Running `project-release` without user sign-off on the `dev` state.
- Directly appending a bullet to `docs/roadmap.md ## Backlog` to record an
  issue — use `feedback-to-issue` (or spawn a Reporter) so the issue routes to
  the configured tracker.
- Closing an issue from the implementing agent (or the master itself) — issue
  closes must go through the QA close gate agent only (Noir) or the human
  reviewer (Supervised/Weiss).

## Context efficiency (v1.29)

Cost levers for long autonomous campaigns. Authority:
`docs/design/context-efficiency-design.md`.

- **Cache-friendly ordering (#57).** Read **stable** content first (coding
  standards, design docs, the agreed release plan) and **volatile** content last
  (live `git` state, this-turn diffs). A stable prefix keeps the prompt cache
  warm across turns. Do **not** re-read unchanged design docs each phase — rely
  on a short **phase summary** of what changed.
- **Shared-context dispatch (#59).** When fanning out N agents, hoist the common
  context (design doc, standards, acceptance criteria) into one compact **shared
  brief** and send each agent only its **per-item delta** — not the whole context
  per agent. See `release-phase`.
- **Per-release baseline (#58).** At closeout, capture/compare the token baseline
  via `token-measure` (`.claude/cache/token-baseline.json`); flag output-token
  regressions beyond threshold (informational).

## Autonomy hardening (v1.30)

Authority: `docs/design/autonomy-hardening-design.md`.

- **Unattended dispatch (#60).** `spawn_task` chips need a human click, so for
  genuine **unattended** Noir dispatch use the write-capable workflow / the
  `Agent` tool with `isolation:"worktree"`. After every batch run the **#35
  isolation checks**: assert `HEAD == version/{X.Y}`, assert each branch advanced
  (`git rev-list --count version/{X.Y}..<branch>` non-empty), and verify file-set
  disjointness. Use `spawn_task` when attended; the workflow path when unattended.
- **Subagent spawn_task guard (#91).** A dispatched Noir subagent may call
  `spawn_task` anyway — for out-of-scope discoveries — creating a chip that
  requires a human click and stalls the autonomous run.
  - *Fix layer 1 — prompt-side:* every Noir task-agent prompt must carry the
    no-chip clause verbatim: "Report all out-of-scope follow-ups as plain text
    in your final report. Never call `spawn_task`, never create chips, never ask
    the user; you are running unattended." Applied in `release-phase/SKILL.md`
    §Step 4 (Noir no-chip clause).
  - *Fix layer 2 — master-side:* if a subagent result mentions "spawned task",
    "created chip", or "filed background task", log the finding to §5 follow-ups
    and continue merging — do not pause for a human.
  - *Residual risk:* any chip that fires anyway is benign (UI-only; non-blocking)
    and auditable via `.claude/cache/` chip records.
- **Branch cleanup (#61).** Use `branch_cleanup.py` (in this skill dir) — it
  selects the safe `git branch -d` for merged branches and lists throwaway
  `-D` candidates for ONE batched human confirmation; it never auto-force-deletes.
  Resolves the classifier-blocked-`-D` stall without bypassing confirmation.
- **Retry/backoff (#63).** On a **transient** tool/model failure (timeout,
  "temporarily unavailable", rate limit) retry with backoff — up to **3 attempts**
  at 20s / 60s / 120s — before pausing for the human. **Persistent** failures
  (auth, not-found, syntax) do not retry. Record each retry so the run is auditable.
- **Push audit (#64).** `push-guard.sh` appends each permitted push to
  `.claude/cache/push-audit.log` (append-only, best-effort). All rails unchanged;
  push stays human-gated by default.
