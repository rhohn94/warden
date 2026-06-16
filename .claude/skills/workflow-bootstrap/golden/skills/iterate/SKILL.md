---
name: iterate
description: Drive systematic, repeated improvement on one project facet (UX, code quality, performance, security, backend, test coverage, documentation, …) through a quota-driven loop — critically audit the facet, file issues until a T-shirt-size quota floor is met, run a full release cycle on them, and repeat. Invoked as "iterate on {facet}" with an optional count ("iterate on UX x3" / "--iterations N", default 1; "--iterations until-clean"). Backed by iterate_quota.py for deterministic quota + resumable state. Push to origin stays human-gated. Triggers on "iterate on X", "iterate on UX", "iterate on code quality", "iterate on backend performance", "run an improvement loop on", "keep improving X until clean".
---

# Iterate on {facet} (IT1)

A structured improvement loop for one **facet** (a free-form lens — `ux`,
`code quality`, `performance`, `security`, `backend`, `test coverage`,
`documentation`, or anything project-specific). Each iteration **audits**
critically, **files** issues to a size quota, runs a **full release** on them,
and **repeats**. Improvement stops being ad-hoc and becomes measurable.

Design authority: `docs/design/iterate-on-facet-design.md`. The deterministic
quota + state live in `iterate_quota.py` (script-first, #75); the audit / filing
/ release are agent-driven, **composing existing skills** — `iterate` never
reimplements the release machinery.

## Invocation

`iterate on {facet} [x N | --iterations N | --iterations until-clean]`. Default
1 iteration. The facet string scopes the audit lens (§3) and is free-form.

## 1. The size quota (a floor)

Each iteration files issues until every T-shirt-size bucket is filled. Default
quota: **XXL 1 · XL 3 · L 5 · M 10 · SM 10 · XS 20** (size = estimated
implementation effort, assigned by the auditing agent). Configurable via
`grimoire-config.json` `iterate.quota` (+ a `per-facet` override) or inline.
Drive it with the helper — do **not** count by hand:

```
python3 .claude/skills/iterate/iterate_quota.py --init --facet "ux" --run-id <id> --iterations N
python3 .claude/skills/iterate/iterate_quota.py --record --size M --count 1   # after each filing
python3 .claude/skills/iterate/iterate_quota.py --status                       # remaining per size + quota_met
```

The quota is a **floor**: keep auditing/filing until `quota_met` is true. Do
**not** stop early because small items are easy, and do **not** inflate issues to
hit a bucket — if the audit surface is genuinely exhausted before quota, **note
the shortfall** (the until-clean signal, §4) rather than padding.

## 2. The iteration loop (one iteration)

1. **Audit** — inspect the project through the facet lens with a deliberately
   critical eye (§3 picks the strategy). Produce a raw findings list; file
   nothing yet. Run this as a **dispatched audit agent** (own-session, the QA
   agent role or a facet-scoped variant) so the master's context stays clean.
2. **File** — turn findings into GitHub issues via `feedback-to-issue` / a
   Reporter, assigning a `size:<SZ>` label to each; `--record` each. Continue
   until `quota_met`. Tag every issue with the run id / milestone so planning can
   scope to exactly this iteration. **Dedupe first** (§ cross-facet dedup).
3. **Release-planning** — `release-planning` scoped to the iteration's issues →
   `release-agreement` (locks scope, creates `version/{X.Y}`).
4. **Execute** — `release-phase` dispatches work-item agents. Port isolation
   (#77) and the recipe interface (#79) apply to any agent that builds/runs.
5. **Merge + release** — `release-phase-merge` then `project-release`
   (`dev → main`, tag). **Push to origin stays human-gated in every paradigm.**
6. **Cleanup** — dead-worktree cleanup of merged branches/worktrees.

Then, if `iterations_remaining > 0`, `--next-iteration` (resets the quota
buckets) and begin again at phase 1.

## 3. Facet audit strategies

| Facet | Strategy |
|---|---|
| `ux` / `design` | `ux-demo-regress`, visual inspection, design-language conformance |
| `code quality` | `coding-practices-audit`, `code-health`, lint/type-check output |
| `performance` | benchmark + profile, build-size, `recipe test --perf` |
| `security` | `security-review`, `dependency-audit` |
| `backend` | API coverage, error-handling paths, data integrity, missing migrations |
| `test coverage` | coverage-report gaps, untested paths, flaky-test detection |
| `documentation` | `doc-assurance`, doc-coverage gaps, stale cross-refs, missing design docs |
| *(unknown)* | general `code-review` + open-ended critical read; infer the tools |

Strategies are extensible — a project may register custom facet strategies.

## 4. Iteration count & stopping

- Default 1; `iterate on UX x3` / `--iterations 3` runs 3 loops.
- `--iterations until-clean`: repeat until an audit phase cannot fill the quota
  at **M and above** (fewer than `iterate.min-issues-floor` substantive findings
  remain). That is the facet's "done" signal — reported, not silently assumed.

## 5. Per-paradigm

- **Supervised** — pause at every phase boundary for explicit approval: audit
  findings → approve filing → approve plan → approve dispatch → approve merge →
  approve cleanup → proceed to next iteration?
- **Weiss (Collaborative)** — present a summary at each boundary and offer to
  proceed; the user may adjust the count/quota between iterations.
- **Noir (Autonomous)** — drive all phases without per-step confirmation. Pause
  only on: merge conflict with ambiguous intent, test failure with unclear cause,
  **push to origin (always human-gated)**, and quota shortfall (report it).
  Between iterations a `ScheduleWakeup` keeps the loop alive across compaction;
  the state file (§6) is the resume anchor.

## 6. Resumable state

`iterate_quota.py` persists `.claude/iterate-state.json` (gitignored): facet, run
id, iterations remaining, current iteration, quota, per-size filled counts, and
the floor. On a Noir wakeup/compaction, re-read it (`--status`) and continue from
the recorded place — never restart a half-done iteration.

## Constraints

- **Drives existing skills; never reimplements them** (release-planning →
  release-agreement → release-phase → release-phase-merge → project-release).
- **Push to origin is always human-gated** — in every paradigm.
- **Quota is a floor, not a target to game** — fill it honestly; report
  shortfalls rather than padding.
- **Audit is read-only**; fixes happen in the execution phase via dispatched
  work-item agents, not during the audit.
- **Deterministic parts go through `iterate_quota.py`** — quota math + state, not
  agent arithmetic.

## Anti-patterns

- Counting quota by hand instead of `iterate_quota.py --status`.
- Inflating trivial issues to hit a bucket (game the floor) — note the shortfall.
- Pushing to origin autonomously (always human-gated).
- Reimplementing release-planning/merge inside the loop instead of calling them.
- Restarting an interrupted iteration from scratch instead of resuming via state.
