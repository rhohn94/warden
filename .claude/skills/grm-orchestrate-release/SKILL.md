---
name: grm-orchestrate-release
description: Drive one full release end-to-end autonomously — planning, work dispatch, testing, merging, releasing, push, and cleanup — with zero permission prompts and zero per-step confirmations. Noir-only; composes the existing pipeline skills rather than reimplementing them. Use when the user says "orchestrate a release", "run a full release", or wants a hands-off release cycle.
---

# Orchestrate release — autonomous end-to-end driver

One invocation owns one complete release: **plan → lock → dispatch → merge →
release → push → cleanup → report**. The skill is a *driver*, not a
re-implementation — every stage is an existing skill, invoked in order, with
the autonomy dials verified up front so nothing stops for a permission prompt
or a per-step confirmation mid-flight.

**Posture (Noir-only, fail-closed).** Off the Noir paradigm this skill stops
at preflight. Autonomy is delivered mechanically, not by asking the model to
be brave: the deny guards (`protected-branch-guard.sh`, `worktree-guard.sh`,
`push-guard.sh`, `stealth-guard.sh`) stay fully active, and the
`autonomy-allow.sh` hook auto-approves only guard-vetted pipeline commands
under Noir. Design:
`docs/grimoire/design/orchestrate-release-design.md`.

## Step 0 — Preflight (deterministic, run first, always)

```bash
python3 .claude/skills/grm-orchestrate-release/orchestrate_preflight.py .
```

- **FAIL** on any line → stop and surface the fix (wrong paradigm, missing
  integration marker, missing/unwired hooks, no `dev` branch). Do not
  improvise around a FAIL.
- **WARN** lines → proceed, but announce the gates that will remain (e.g.
  `autonomous-push.enabled` false ⇒ the pipeline will actively prompt at the
  push gate via `AskUserQuestion` — see stage 6 — rather than push silently).
- All **PASS** → announce the milestone ("orchestrating v{X.Y} end to end;
  will report at push/cleanup or on a stop condition") and proceed without
  further confirmation requests.

## Pipeline — invoke, don't reimplement

| Stage | Skill | Autonomous behaviour |
|---|---|---|
| 1. Plan | `grm-release-planning` | Produce the work-items report from roadmap + carryovers + design docs. Under this skill, do **not** wait for report iteration — proceed with the defensible scope. |
| 2. Lock | `grm-release-agreement` | Write the planning doc (draft → agreed), create `version/{X.Y}` off `dev`, initialize the §5 ledger. |
| 3. Dispatch | `grm-release-phase` | Per open phase: batch by the §3 conflict map, dispatch isolated-worktree subagents, chip-free. |
| 4. Merge | `grm-release-phase-merge` | Merge returned branches in conflict-map order; tests after each merge; tick §5; final `version/{X.Y}` → `dev`. |
| 5. Release | `grm-project-release` | Preflight docs (version-history, feature-manifest), promote `dev` → `main`, bump, test, tag, build artifacts. |
| 6. Push | `grm-project-release` §push | **Ungated** (Noir + `autonomous-push.enabled: true`): push runs immediately, no question asked — push-guard suppresses the permission prompt itself. **Gated** (`autonomous-push.enabled` false): actively prompt via `AskUserQuestion` (`Push now` / `Hold`) with the exact push plan (refs, tag, remote) in the body; a stage-6 pause is expected here, not a failure — resume with `Push now` when the user answers. |
| 7. Cleanup | `grm-workspace-clean` + dead-worktree cleanup | Remove merged agent worktrees/branches per `docs/grimoire/integration-workflow.md` §Dead-worktree cleanup; confirm `dev`/`main` match origin. |
| 8. Report | — | One summary: version shipped, items landed/deferred, test state, follow-ups filed. |

Between stages, verify the previous stage's postcondition before continuing
(plan doc `status: agreed`; every §5 row Merged before release; tag exists
before push; worktrees gone after cleanup). A postcondition miss is a stop
condition, not something to patch around silently.

## Stop conditions (the only interruptions)

Pause and surface — never push through:

- **Merge conflict** the conflict-map ordering didn't prevent (ambiguous
  intent) — stop per `grm-release-phase-merge`.
- **Test failure** with unclear cause after a merge.
- **Guard block** (any hook exit 2): the guard is right until proven
  otherwise — investigate, never work around it.
- **Isolation failure**: missing `worktreePath:`/`worktreeBranch:` footer from
  a dispatched agent, or master HEAD drift — follow
  `docs/grimoire/design/dispatch-hardening-design.md` recovery.
- **Doc/config gate failure** at release preflight (doc-assurance `--strict`,
  config-validate).
- **User stop** at any time.
- **Gated push prompt** (stage 6, `autonomous-push.enabled` false): the
  `AskUserQuestion` pause here is expected, not a failure — it is the single
  designed interruption of an otherwise autonomous run.
- **Blocked on human** (#422): under `grm-noir-loop`, if `noir_loop_state.py`'s
  `blocked_on_human` flag is true (the progress-hash over open work + the
  current blocker repeated unchanged for `STALL_LIMIT` consecutive
  iterations — the same human-gated item, cycle after cycle) — stop and hand
  off to `grm-stop-point` rather than spinning on a blocker only a human can
  clear.
- **Cycle budget exceeded** (#422): if `noir_loop_state.py`'s
  `cycle_budget_exceeded` flag is true (`iteration` reached the configurable
  `max_cycles` cap), stop and hand off to `grm-stop-point` — a backstop
  against runaway loops even when each cycle is making real progress.

On a stop, report state precisely (what landed, what's pending, which §5 rows
are ticked) so the session can resume with `grm-release-phase-merge` or
`grm-end-session` rather than restarting. For the two `#422` conditions above,
resume by running **`grm-stop-point`** instead — it packages the wind-down
(merge what's ready, tick the ledger, report, park blocked items) in one call.

## Why there are no permission prompts

- **Bash**: `autonomy-allow.sh` (PreToolUse) auto-approves the whitelisted,
  guard-vetted pipeline commands under Noir. Deny hooks take precedence, so
  nothing a guard blocks today is newly allowed.
- **Push**: `push-guard.sh` auto-approves guard-passed pushes only with the
  explicit `autonomous-push.enabled` opt-in (never inferred).
- **Everything else** (history rewrites, force flags, `rm`, redirections,
  non-framework scripts) still prompts — those are last-resort ops the
  pipeline never needs routinely.

## Token discipline

- Dispatch work through isolated-worktree subagents with the ≤800-token
  shared brief (`grm-release-phase` step 5); never inline design docs into
  dispatch prompts.
- Keep orchestrator context lean: read ledgers and reports, not diffs; use
  `grm-agent-reviewer` / `grm-agent-qa` in their own sessions when review depth is
  needed.
- Prefer the deterministic helpers (preflight script, `release_plan.py`,
  `recipe.py`) over re-deriving state in prose.

## Relationship to neighbouring skills

- `grm-integration-master` — the *role guide* (posture, judgment calls); this
  skill is the *procedure* that role executes for one release.
- `grm-noir-loop` — iterates releases across `/loop` firings; each iteration's
  release-master MAY use this skill to run its single release.
- `grm-end-session` — the recovery/wind-down finale when a release is already
  mid-flight; this skill starts from zero instead.
- `grm-stop-point` — the wind-down to run on a **stop condition** specifically
  (blocked-on-human, cycle-budget, or any other stop above): merge what's
  ready, tick the ledger, report, park anything blocked with an issue
  comment. Narrower than `grm-end-session` (which also releases/pushes/cleans
  up worktrees) — use it when you need to park cleanly, not finish the release.
