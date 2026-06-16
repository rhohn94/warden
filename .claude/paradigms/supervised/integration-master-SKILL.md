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
5. `release-phase-merge` — merge each completed branch; ask before each.
6. `project-release` — promote `dev` → `main` and tag.

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

## Anti-patterns

- Locking scope without showing the user the report.
- Spawning a batch without asking first.
- Auto-merging: every `git merge --no-ff` needs a "Merge?" confirmation.
- Pushing without the user's explicit "push" instruction.
- Running `project-release` without user sign-off on the `dev` state.

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
