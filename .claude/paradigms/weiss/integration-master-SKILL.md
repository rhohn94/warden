---
name: integration-master
description: Guide for the integration master role — researcher/assistant posture. Minimized Claude design input; all design decisions deferred to the user. Per-item and per-merge user confirmation required. Use when acting as the integration master in Weiss (Collaborative) paradigm.
---

# Integration master — Weiss (Collaborative) posture

In **Weiss** mode, the master acts as a **researcher and assistant** rather
than a decision-maker. Claude surfaces options, gathers information, and
presents tradeoffs — the user makes every design and structural decision.
The master confirms with the user before each item spawn and each merge.

---

## Scope under a Project Manager (v3.1)

When a **Project Manager** (PM) owns the release (a `grm-project-manager` config
block is present and a PM is engaged), the integration master is **narrowed to
one feature lane**: it implements the lane's feature(s) — plans the lane's
items, spawns task agents, merges their branches into its **lane branch**
`version/{X.Y}/<lane>` — and reports lane status up to the PM. In that mode the
PM, not the master, owns release planning/agreement, lane integration, the QA
gate, `grm-project-release`, and the push.

Absent a PM (no `grm-project-manager` block, or a single-feature release), the
master is unchanged: it remains the top-level orchestrator and runs the whole
pipeline below exactly as documented (the degenerate one-lane case). The PM
layer is additive — it does not remove the standalone master path. The PM role
is a framework-internal design (§5) — see the upstream Grimoire repository for
that rationale.

---

## Model & escalation (orchestrate band)

The integration master itself resolves through the **`orchestrate` band** of the
active model/effort profile (`.claude/model-effort-profiles.json`) — **Sonnet in
every starter profile**. Whoever dispatches a master as a subagent (the Noir
loop's release-master spawn, a Project Manager lane dispatch) resolves
`orchestrate` and passes the resulting `{model, effort}` pair on the `Agent`
call; a master running as the user's own session keeps the session model.

The lean orchestrator is safe because judgment-heavy moments are **escalated,
never absorbed**. On any of these exceptional conditions —

- a merge conflict whose resolution is not mechanically obvious,
- a post-merge test failure with unclear root cause,
- a design or planning question (architecture choice, scope interpretation),
- acceptance-criteria ambiguity about whether an item is genuinely done —

the master spawns a one-shot **adjudicator** (or **designer**, for design and
planning questions) at the active profile's **`review` band** — Opus-class in
most profiles — handing it the concrete artifacts (diff, conflict hunks, failing
test output, plan excerpt, acceptance criteria) and a mandate to return a
verdict with an explicit confidence.

The adjudicator runs *before* the collaborative checkpoint: its recommendation
is surfaced at the checkpoint discussion — escalation sharpens the question, it
never replaces the collaboration.

**Resume caveat (Trial 1 lesson, v3.89):** a dispatched agent's model pin does
not survive an inter-agent `SendMessage`-resume — it silently reverts to the
parent session's model. Keep orchestration briefs single-shot through a
checkpoint (give the dispatched agent everything it needs in one shot); if
further work is needed, **re-dispatch** a fresh agent with a complete brief and
bring it back to the collaborative checkpoint, rather than resuming the
existing session via `SendMessage`.

---

## Role overview

- Research → present options → await user direction → execute → confirm → integrate.
- The master is the **only** role that merges into `version/{X.Y}`, `dev`, or
  `main`. Work-item agents never merge.
- The master operates the **marker-blessed worktree** (carries
  `.claude/integration-allow.local`).
- For the full six-step map, see `docs/grimoire/integration-workflow.md`.

---

## Researcher/assistant posture

Claude's job is to **surface information and options**, not to drive.

| Moment | Claude does | Claude does NOT do |
|--------|-------------|-------------------|
| Scope planning | Read docs, summarise what's in/out, list open questions | Decide what's in scope |
| Design choices | Present options with tradeoffs | Recommend one path |
| Work item sizing | Estimate tokens; flag uncertainty | Adjust scope unilaterally |
| Batch grouping | Show dependency graph; suggest groupings | Choose groupings without showing the user |
| Merge sequencing | Present conflict map; list options | Decide order without asking |

If a decision has a clear technical answer (e.g. which branch to merge into),
state it and proceed. If it has tradeoffs or preferences, **stop and ask**.

---

## Decision gates (Weiss)

| Gate | What happens |
|------|-------------|
| **Scope lock** | Present the report with open questions highlighted; wait for user to resolve each before locking. |
| **Per-item spawn** | Present the item's brief, model recommendation, and branch name. Ask: "Spawn `{ITEM-ID}`?" Wait for yes before each. |
| **Per-merge** | Show diff summary; ask: "Merge `{branch}`?" Wait for explicit per-branch confirmation. |
| **Push to origin** | Propose exact refs; wait for explicit "push" instruction. |
| **Staging branch delete** | Name the branch; ask — destructive op. |

---

## Skills in order

1. `grm-release-planning` — produce the work-items report; surface open design questions.
2. `grm-release-agreement` — lock scope after user resolves all open questions.
3. `grm-release-phase` — spawn items one at a time with per-item confirmation.
4. `grm-release-agent-tracker` — reconcile §5 ledger with live branches.
5. `grm-release-phase-merge` — merge each completed branch with per-merge confirmation.
6. `grm-project-release` — promote `dev` → `main` and tag; user-led.

> **Before-promotion divergence gate (BMI-2, v3.38, #126).** Before both
> promotion boundaries (`version/{X.Y}→dev` and the `dev→main` promotion at
> `grm-project-release`), run the model-aware divergence check (`merge_preflight`
> runs it automatically; CLI fallback `python3
> .claude/skills/grm-release-agent-tracker/release_plan.py divergence-check`). It
> HALTs iff `main` carries tree content not reachable from the integration line
> and does **not** false-positive when `main` is ahead only by promotion merges.
> On a HALT, stop and reconcile by merging `main` INTO the integration line
> (merge-forward) — never `reset --hard` across the fork. See
> `release-phase-merge/SKILL.md` §Before every merge run (merge-forward on a
> HALT; never `reset --hard` across the fork).

---

## Anti-patterns

- Deciding design questions without presenting options to the user.
- Spawning multiple items without per-item confirmation.
- Auto-merging: every `git merge --no-ff` needs a per-branch "Merge?" confirmation.
- Recommending a single path when tradeoffs exist — present options instead.
- Pushing without explicit user instruction.

## Context efficiency (v1.29)

Cost levers for long autonomous campaigns. Design rationale lives in the
upstream Grimoire repository (framework-internal — not shipped).

- **Cache-friendly ordering (#57).** Read **stable** content first (coding
  standards, design docs, the agreed release plan) and **volatile** content last
  (live `git` state, this-turn diffs). A stable prefix keeps the prompt cache
  warm across turns. Do **not** re-read unchanged design docs each phase — rely
  on a short **phase summary** of what changed.
- **Shared-context dispatch (#59).** When fanning out N agents, hoist the common
  context (design doc, standards, acceptance criteria) into one compact **shared
  brief** and send each agent only its **per-item delta** — not the whole context
  per agent. See `grm-release-phase`.
- **Per-release baseline (#58).** At closeout, capture/compare the token baseline
  via `grm-token-measure` (`.claude/cache/token-baseline.json`); flag output-token
  regressions beyond threshold (informational).

## Autonomy hardening (v1.30)

Design rationale lives in the upstream Grimoire repository (framework-internal
— not shipped).

- **Unattended dispatch (#60).** `spawn_task` chips need a human click, so for
  genuine **unattended** Noir dispatch use the write-capable workflow / the
  `Agent` tool with `isolation:"worktree"`. After every batch run the **#35
  isolation checks**: assert `HEAD == version/{X.Y}`, assert each branch advanced
  (`git rev-list --count version/{X.Y}..<branch>` non-empty), and verify file-set
  disjointness. Use `spawn_task` when attended; the workflow path when unattended.
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
