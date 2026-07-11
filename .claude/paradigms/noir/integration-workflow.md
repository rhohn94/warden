# Integration workflow — Noir (Autonomous)

Audience: the **integration master** operating in Noir mode — autonomous
posture. The master drives the full release pipeline to the specified
milestone without per-step confirmation. Work-item agents do **not** need
this doc; their guide is `CLAUDE.md`.

**Push to origin remains human-gated.** This is the one mandatory stop in
Noir; it is never lifted in v1.6.

---

## Execution model

The user specifies a **milestone** (e.g. "ship v1.6", "complete Phase 2")
or an explicit stop signal ("stop", "pause"). The master runs unsupervised
between the start signal and the milestone/stop, pausing only for:

1. Merge conflict with ambiguous intent.
2. Test failure with unclear root cause.
3. Push trigger.
4. User says stop.
5. Milestone reached.

At any stop, the master reports: what was completed, what is blocked, what
the user needs to decide.

---

## Pipeline steps

1. **Plan scope** — `grm-release-planning` skill. Read docs, roadmap, carryovers;
   produce the work-items report. Proceed directly to lock.
2. **Lock scope** — `grm-release-agreement` skill. Lock immediately after
   planning; create `version/{X.Y}` off `dev`.
3. **Distribute work** — `grm-release-phase` skill. Apply §3 conflict map; dispatch
   the full current batch at once via isolated-worktree subagents (`Agent` with
   `isolation:"worktree"`, or a write-capable Workflow) — chip-free, no
   `spawn_task`. No per-item confirmation.
4. **Track** — `grm-release-agent-tracker` skill. Poll for ☑ Implemented
   branches. Proceed to merge as each batch completes.
5. **Integrate** — `grm-release-phase-merge` skill. Merge each completed branch
   autonomously: review diff, merge, test, tick §5, advance. Pause only on
   conflict/test-failure stop conditions or the push trigger.
6. **Release** — `grm-project-release` skill. Promote `dev` → `main` and tag.
   Propose push; wait for human instruction.

The integration master is the **only** role that merges into
`version/{X.Y}`, `dev`, or `main`. Work-item agents never do.

---

## Iterative release loop (`/loop`, #83)

For **long-horizon, multi-release** operation under Noir, run releases as
repeated **iterations** while keeping the orchestrating session's context flat.
Each iteration is delegated to a fresh subagent so the orchestrator accumulates
only a tiny summary per cycle. The full design and the `release-master` role
(§B.12) are framework-internal — see the upstream Grimoire repository for that
rationale. Helper: `.claude/skills/grm-noir-loop/`.

**The loop, per `/loop` firing.** Claude Code's `/loop` keeps the same
orchestrator session alive across firings (fixed-interval or self-paced
`ScheduleWakeup`). Each firing is **one orchestrator turn** that does exactly
three things:

1. **Spawn ONE `release-master` subagent** (via the `Agent` tool — chip-free, no
   `spawn_task`) at the active profile's **`orchestrate` band** — the
   `{model, effort}` pair from the `grm-repo-reference` resolver, Sonnet in
   every starter profile — with a self-contained prompt (the §C spawn contract). The prompt
   names the state file path; it does
   **not** inline release history. Inside its own fresh context the
   release-master *is* an integration master for that one iteration — it runs the
   full Pipeline steps above (plan → distribute → integrate → release).
2. **Wait** for its return — a **1–2 sentence summary only**. All heavy tool
   output, planning, and diffs stay in (and die with) the subagent's context.
3. **Log** that one sentence and end the turn. The orchestrator grows by ~a
   sentence per iteration.

**Cross-iteration continuity** lives in `.claude/cache/noir-loop-state.json`
(gitignored, **size-budgeted** — an over-budget write is refused, never
truncated, so each subagent reads it near-clean). The release-master
`--read`s it at the start of its iteration and `--advance`s it at the end:

```
python3 .claude/skills/grm-noir-loop/noir_loop_state.py --read     # iteration start
python3 .claude/skills/grm-noir-loop/noir_loop_state.py --advance --summary "…" --open "…" --next "…"  # iteration end
```

**No `/clear` / `/compact`.** Do **not** rely on `/clear` between iterations — it
would destroy the loop's own scheduling state; `/compact` cannot be self-invoked
by an agent anyway. The subagent + state-file pattern is what keeps context flat
**without** clearing.

**Composes with default Noir wakeup-scheduling (#13).** Wakeup decides *when* the
next firing happens (the §"Pushing to origin" gate and the #13 cadence are
unchanged); the loop decides *what* a firing does — spawn the next release-master,
which reads the state file for its starting point. It also stays within the token
budget (#28): bounded per-iteration cost, observable via `grm-cost-budget`.

**Noir only.** Supervised / Weiss run releases in-session via the integration
master and do not use this loop. **Push to origin stays human-gated** — the loop
never pushes; the release-master proposes the push at `grm-project-release` and waits.

---

## Branch model

```
version/<number>  ──►  dev  ──►  main
```

Work items run in isolated worktrees dispatched via `Agent` with
`isolation:"worktree"` (or a write-capable Workflow) — **not** `spawn_task`
chips, which require a human click and are reserved for the Supervised / Weiss
paradigms. The master dispatches the full batch without per-item gates and
queues merges as the subagents return their branches.

`.claude/skills/grm-integration-master/SKILL.md` (§top). Design rationale (§2)
lives in the upstream Grimoire repository (framework-internal — not shipped).

Destructive flags (`--force`, `--all`, etc.) are always denied.

## GitHub Release (distribution — authoritative, v3.23)

At the same single post-release moment, the master **always** publishes a GitHub
Release from the version tag, carrying the `version-history` notes and the
**per-flavor `.zip` distributables** — `dist/grimoire-<flavor>-v{X.Y}.zip`, one per
`.grimoire-flavor` directory, built deterministically by
`project-release/build_distributables.py` and attached to the Release. The
Release is the **authoritative artifact** downstream `grm-sync-from-upstream` consumes
(`UPSTREAM_TRANSPORT=release` downloads the flavor's zip). No longer optional — it
degrades only when `gh` is unavailable, and then **loudly**. Full procedure:
`grm-project-release` §GitHub Release. Design rationale lives in the upstream
Grimoire repository (framework-internal — not shipped).

## Lane model & multiple marked lane worktrees (v3.1)

When a **Project Manager** owns a multi-feature release (the PM role is a
framework-internal design — see the upstream Grimoire repository for that
rationale — and
`.claude/skills/grm-project-manager/SKILL.md`), the single `version/{X.Y}` staging
line is split into **parallel lanes**, each implemented by its own integration
master:

- **Lane branches.** The PM creates `version/{X.Y}` off `dev`, then a lane branch
  `version/{X.Y}/<lane>` off it per non-colliding lane (the lanes come from the
  overlap analysis — `pm_overlap.py`). The `version/.*` shape keeps lane branches
  inside the protected set, so the existing guards cover them unchanged.
- **One integration master per lane**, each in its own marker-blessed worktree,
  merging its task agents' branches into its lane branch via `grm-release-phase-merge`
  — exactly the single-master flow, scoped to the lane.
- **Lane integration.** As lanes complete, the PM merges each lane branch into
  `version/{X.Y}`, then promotes `version/{X.Y}` -> `dev` -> `main`. Lanes are
  component-disjoint by construction, so these merges are conflict-free in the
  common case; a real cross-lane conflict means the overlap analysis
  under-approximated — the PM serializes the offending lanes and records the miss
  (never a silent force-merge).

### Multiple marked lane worktrees

Today exactly one worktree carries `.claude/integration-allow.local`. With
parallel lanes there are now **several** marked integration worktrees — one per
lane IM, plus the PM. The existing guards already make this safe **without code
change**:

- Each **lane IM worktree** carries its own marker and may mutate history only
  while HEAD is on a staging branch — its lane branch `version/{X.Y}/<lane>`
  (matches `version/.*`, the master HEAD-drift guard). It **cannot** touch
  another lane's branch: the cross-worktree hijack guard refuses any `git -C` /
  `--git-dir` / `cd`-into-sibling op aimed at a different worktree.
- The **PM worktree** carries the marker and performs the lane->`version/{X.Y}`
  integration merges and the final `version/{X.Y}` -> `dev` -> `main` promotion.
- **Marker placement is per lane.** The PM (or the dispatch vehicle) provisions
  the marker in each lane worktree as it dispatches that lane — the documented
  operator action, extended from one worktree to N.

Push stays human-gated; lane IMs never push. Under **Stealth Mode** the parallel
`version/{X.Y}/<lane>` fan-out is suppressed (it is a fingerprint) — the PM falls
back to serial, in-place lane execution.

## Enforcement (guard hooks)

Same hooks as Supervised: `protected-branch-guard.sh`, `push-guard.sh`,
`release-plan-guard.sh`, `worktree-guard.sh`, `bundled-sync-guard.sh`. Noir
autonomy operates within — not around — these mechanical guards. Write-capable
Workflow agents are subject to the same hooks as isolated-worktree subagent
work-item agents: no marker means fail-closed on protected branches.

**Bundled-sync-commit guard (v3.67, #126 criterion 3).** `bundled-sync-guard.sh`
is a PreToolUse(Bash) hook matching `git commit`: it denies (`exit 2`) a single
commit whose STAGED changes span both `grm-sync-from-upstream`'s typical
touch-set (`.claude/`, `CLAUDE.md`, `AGENTS.md`, `docs/grimoire/`, the
`.github/` Copilot mirror) and `grm-design-language-adapt`'s typical touch-set
(`docs/design/ux/`, `vendor/aura/`, `static/aura/`, `templates/base.html`) at
once. This is the mechanical enforcement of BMI-3 Rule 3c — until v3.67 both
skills only *reminded* the operator, in prose, to keep framework-sync and Aura
vendoring in separate commits (never bundled, per design rationale (§3) that
lives in the upstream Grimoire repository, framework-internal); nothing
stopped a commit from ignoring the reminder. This closes the exact `24c73dd`
anti-pattern (a 660-file "Grimoire upstream + Aura v3.21" commit) at the
mechanical level, complementing — not replacing — each skill's own Rule
3a/3b branch- and release-boundary refusal (which the skills implement
themselves, since only they know their own touch-set and boundary context at
invocation time). Applies to every actor (no marker exemption): bundling the
two concerns is never legitimate, unlike the marked/unmarked distinctions the
other guards draw. Self-tested via `--self-test`.

**Cross-worktree branch hijack rule (v1.7).** A spawned/work-item agent (and a
write-capable Workflow agent) must git-operate **only on its own worktree**. An
**unmarked** actor that redirects a branch op (`switch` / `checkout` /
`branch`) at a **different** worktree — via `git -C <path>`,
`--git-dir`/`--work-tree`, or a `cd`/`pushd` into another worktree — is
**refused (`exit 2`)** by both guards; the refusal names the integration master
when the target carries the marker. The marker-blessed master on its own
worktree (or crossing boundaries for §Dead-worktree cleanup) is unaffected.
Agents branch in place from the staging ref
(`git switch -c <branch> version/{X.Y}`). Autonomy does not exempt an agent
from this rule — it is enforced mechanically.

**Clean release-boundary guard for marked commits on `main`.**
The (actor, branch-class) model's `marked + protected -> allow` cell is now
**conditional** when the protected branch is `main`: a marked integration
master's `commit`/`merge` on `main` is allowed only at a genuine
release-promotion boundary, closing the residual where a marked master could
previously commit to `main` at any time (the #126 failure mode — a manual
release and a scaffolding sync committed straight to `main` outside any
promotion flow). `dev` and `version/*` are unaffected — the conditional check
applies to `main` only. A boundary is clean when any of:

1. `dev` and `main` have identical trees (the BMI-2 tree-content predicate,
   §Recovering from an integration-branch fork above) — `dev` is already
   fully promoted.
2. The invocation IS the promotion merge itself (`git merge <dev-ref>` while
   HEAD is `main`).
3. The `.claude/release-in-progress.local` marker is present.

**`.claude/release-in-progress.local` marker convention.** Mirrors
`.claude/integration-allow.local`: a deliberate, local-only, git-ignored
marker file, never committed. `grm-project-release`'s promote step creates it
immediately before `git switch main && git merge dev` and removes it
immediately after tagging — bracketing the part of the promotion window that
conditions 1–2 above don't already cover on their own (most commonly a
version-bump commit landing on `main` directly, after the merge, when trees
have diverged again). If a release run fails or is aborted mid-promotion, the
marker must be removed before retrying — a stale marker would leave `main`
permanently boundary-exempt. Denied cases print a remediation message
pointing at `grm-project-release`.

## Git-protocol governance (branch-and-merge default; #84)

The git default is **branch-and-merge**. History-**rewriting** commands are
prohibited by default and permitted only as an explicit, human-confirmed last
resort:

| Prohibited (last-resort only) | Use instead (the default) |
|---|---|
| `git rebase` | `git switch -c <branch> <ref>` then `git merge --no-ff` |
| `git cherry-pick` | merge the source branch with `--no-ff` |
| `git reset --hard` | `git revert`, or a fresh branch from a known-good ref |
| force-push (`--force` / `--force-with-lease`) | push the specific ref without force |
| remote-ref deletion (`git push origin :ref`) | leave shared refs intact |

Protected-branch (`dev`, `main`) integration always goes through the established
`--no-ff` merge protocol — never a direct push, never a force-merge. The
`protected-branch-guard.sh` blocks `git rebase` / `git cherry-pick` /
`git reset --hard` on a protected branch for **every** actor (escape hatches and
soft/mixed resets exempt); `push-guard.sh` blocks force-push, force flags, and
remote-ref deletion. Autonomy does not exempt an agent — if history truly must be
rewritten (a genuine last resort), a human runs the command deliberately outside
the agent; the agent never does it autonomously.

## Write-capable workflow tier

Under Noir, write-capable workflows are available in addition to the
read-only tier. Declare the tier in `export const meta`:
```js
export const meta = { tier: 'write-capable', ... };
```
The script checks the active paradigm at runtime and fails closed if not Noir.

Each write-capable Workflow agent receives an isolated worktree and commits
on a per-agent branch (`<item-slug>-<short-uuid>`). The master collects the
branch list from the Workflow's structured output and merges via
`grm-release-phase-merge` in `mergeAfter` dependency order.

**Three execution variants** (default: `Efficient`):

| Variant | Parallelism | When to choose |
|---------|-------------|----------------|
| **Efficient** | Parallel, low-waste | Default; respects conflict map |
| **Fast** | Parallel, min wall-clock | Fully independent items |
| **Careful-Serial** | Sequential | Risky or entangled changes |

**Safety rails:** agents never push; agents never touch protected branches
(`dev`/`main`/`version/*`); agents are confined to their own worktree;
**push to origin remains human-gated even in Noir** (applies to all agents
and the master). Guard hooks enforce all of these mechanically.

The full design is a framework-internal design — see the upstream Grimoire
repository for that rationale.
