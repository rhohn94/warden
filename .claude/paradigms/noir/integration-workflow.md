# Integration workflow — Noir (Autonomous)

> **Up:** [↑ Docs](README.md)

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

### Single-integration-line invariant

At all times there is exactly **one** integration line per repository — the
branch where work is composed before it is published — and every change reaches
the published line (`main`) **only** by promotion from the integration line.
**No commit is ever authored directly on `main` out-of-band.** This is a
**hard rule, not a convention**. It applies to both supported branch models:

- **Default model**: `dev` is the integration
  line; `main` is the downstream published line. Promotion = `git merge --no-ff
  dev → main` via the `grm-project-release` skill only. Nothing is authored on
  `main` directly — not manual releases, not scaffolding syncs, not
  unreconciled hotfixes. Any such change must land on `dev` and reach `main`
  by promotion.

- **Re-branch model** (Noir-loop consumers): the loop re-branches the
  integration line from `main` at the start of each iteration
  (`git switch -c <integration> main`) and promotes back to `main` at release.
  The same rule applies: nothing lands on `main` out-of-band between
  iterations. Any out-of-band `main` commit breaks the ancestor relationship
  the next re-branch relies on.

BMI-4 (`protected-branch-guard.sh`) enforces this at commit time; BMI-3 enforces
it for sync skills. When a fork has already happened, see §Recovering from an
integration-branch fork (merge-forward) below — that is the only safe path.

**Criterion 2 reconciliation (#126, v3.67).** #126 literally asked for a
`git merge-base --is-ancestor main <integration>` check before promotion. The
divergence guard (`DivergenceGuard` in
`.claude/skills/grm-release-agent-tracker/release_plan.py`, BMI-2) instead uses
**tree-content reachability** — this is the accepted implementation of that
criterion, not a gap: it is strictly stricter (catches every real fork a literal
`is-ancestor` would) and avoids a false-positive `is-ancestor` trips on this
repo's own healthy `dev`/`main` (nine benign promotion-merge commits make
`main` a non-ancestor of `dev` with zero real divergence). Full justification
(§2) lives in the upstream Grimoire repository (framework-internal — not
shipped).

---

## Subagent delegation

Spawn `Agent` subagents for mechanical / read-only work autonomously.
Reserve `opus`/high for review and integration judgement per the
`grm-repo-reference` table.

## Filing issues with the Reporter (v1.12)

The **Reporter** is a third named agent role — alongside the task agent and the
integration master — available in all paradigms. Its sole job is to receive
feedback and file it via the `grm-feedback-to-issue` skill. It is an **optional
additional channel**: the integration master may file one item via
`grm-feedback-to-issue` directly; spawn the Reporter when filing multiple items
or when you want to keep the integration session focused on git operations.
Guide: `.claude/skills/grm-agent-reporter/SKILL.md`.

When a work-item session or the integration session discovers something
out-of-scope (scope creep, a follow-up bug, a deferred item worth tracking), do
not append bullets directly to `docs/roadmap.md ## Backlog`. Instead route the
flag through the issue-tracker abstraction:

- The integration master runs `grm-feedback-to-issue` directly for a single item.
- The integration master spawns the Reporter for multiple items or to keep
  filing separated from the current session context.

This keeps issue filing decoupled from the roadmap narrative and ensures items
land in the configured tracker — which may be GitHub Issues rather than the
roadmap when `grm-issue-tracker` is configured in `.claude/grimoire-config.json`.

### Agent-type taxonomy

| Role | Context type | Git writes | Issue writes | Invoked by |
|---|---|---|---|---|
| Task agent | Work-item session | Yes (own branch) | No | Integration master |
| Integration master | Orchestration session | Merge only | Via Reporter or direct | Human |
| **Reporter** | Focused filing session | No | Yes | Integration master / human / any |

The Reporter is **not** a paradigm role and has no associated worktree or
branch. It is a one-shot invocation: file all items, return issue number(s) and
URL(s), exit.

### Invocation

Under Noir, spawn the Reporter **chip-free** as an `Agent` (isolated context,
no `spawn_task` chip) — no per-spawn confirmation. Use this prompt template
verbatim:

```
Reporter: file the following feedback via grm-feedback-to-issue.
Audience: <internal|external>.
Feedback:
<paste feedback text here>
```

For multiple items:

```
Reporter: file the following feedback items via grm-feedback-to-issue, one issue per item.
Audience: <internal|external> (applies to all unless overridden per item).
Items:
1. <first feedback item>
2. <second feedback item>
```

The Reporter targets the **configured issue tracker** only — it makes no git
commits, never reads or writes any `version/*` branch, and is therefore safe to
run during an in-flight integration session or phase merge. If the configured
tracker is `roadmap`, the Reporter appends to `docs/roadmap.md ## Backlog` on
`dev` only — it stops and reports a conflict rather than appending on a
`version/*` or `main` branch. The Reporter never pushes to origin — that
remains human-gated even under Noir. Full role definition, spawn mechanics, and
anti-patterns: `grm-agent-reporter` §1–§7.

## Workflow-based orchestration

Use `Workflow` for read-heavy analysis steps autonomously when appropriate.
Two tiers are available under Noir: the **read-only tier** (analysis/synthesis,
no file mutations — all paradigms) and the **write-capable tier** (each agent
commits on an isolated branch; master merges — Noir only). See
§Write-capable workflow tier below for the full spec.

## Dead-worktree cleanup

Run the dead-worktree check and removal autonomously after each merge step
(see Supervised `integration-workflow.md` §Dead-worktree cleanup for the
full procedure). Stop and surface if a worktree is not clean.

**Post-release cleanup step.** Cleanup is also a named, ordered release step,
run once after `grm-project-release` tags the version and the human-gated push
completes (`grm-project-release` §Post-release cleanup drives it;
`grm-release-phase-merge` cross-references it). Only the **marker-blessed master**
may run it — per the cross-worktree branch hijack rule (§Enforcement). For each
work-item branch/worktree: verify merged + clean, **preserve or report** any
uncommitted work (never silent `--force`), `unlock` then `git worktree remove`,
`git worktree prune`, and safe-delete merged + leftover `worktree-*` branches
with `-d`. Report the tally.

## Run teardown (end-of-run)

When the master **finishes** — the milestone is reached, or the user says stop
and no work is outstanding — it runs teardown as its **final ordered step**,
after §Post-release cleanup. (A master that is *pausing* with work still
queued checkpoints and schedules a resume instead — it does **not** tear down.)
Design rationale lives in the upstream Grimoire repository (framework-internal
— not shipped). Ordered:

1. **Confirm durability.** All intended commits made, §5 ledger ticked, release
   tagged + pushed (or pause-state checkpointed); nothing keep-worthy left
   uncommitted.
2. **Cancel self-created schedules.** Cancel every wakeup/cron this run created
   to resume itself (`CronList` → `CronDelete`; do not re-arm `ScheduleWakeup`) —
   the de-scheduling counterpart to default-on resume scheduling (#13). Cancel
   only this run's schedules; leave unrelated operator schedules alone. A
   self-scheduling agent that finishes without de-scheduling leaks timers (wakes
   to a finished campaign, burning tokens).
3. **Reclaim dispatched worktrees.** Cross-reference §Post-release cleanup —
   already done for the work items; confirm the tally is clean.
4. **Hand off your own worktree.** The marker-blessed master cannot
   `git worktree remove` the worktree it is running in (checkout busy; HEAD
   held). So: (a) confirm its branch is merged and the tree clean; (b)
   switch/detach off any branch slated for deletion to release the lock; (c)
   **surface a one-line handoff** naming the surviving worktree path and the
   exact `git worktree remove <path>` command for the operator (or the parent
   PM) to run from another checkout. Never abandon it silently.
5. **Drop the stale marker.** A surviving worktree's
   `.claude/integration-allow.local` is now stale — note it (gitignored/local);
   removing it keeps an idle worktree from looking like an active master.
6. **Clear scratch.** Remove temp/scratch artifacts the run created (e.g.
   `/tmp/notes-*.md`); gitignored `.claude/cache/` is machine-local, leave or
   prune.
7. **Report the teardown tally:** schedules cancelled, worktrees removed /
   handed-off, marker disposition, scratch cleared, own-worktree disposition.

## Recovering from a stranded-branch / HEAD-drift incident

**Symptom.** A dispatched `Agent` (`isolation: "worktree"`) ran in-place in the
master's worktree (no `worktreePath:`/`worktreeBranch:` footer); its
`git switch -c <branch>` relocated the master's HEAD onto that work-item branch.
Subsequent merges/commits piled onto the stray branch, so `version/{X.Y}` (or
`dev`/`main`) never advanced and a release shipped empty or partial. This is the
v1.15 incident; design rationale for the fix work lives in the upstream
Grimoire repository (framework-internal — not shipped).

**Detection.** The HEAD-verification gate (`grm-release-phase-merge` §Before every
merge run) and the `protected-branch-guard.sh` HEAD-drift block both fire when
the master is off-staging. Confirm with `git symbolic-ref --short HEAD` and
`git log --oneline version/{X.Y}..<stray-branch>` (the stranded commits).

**Recovery (each `git branch -f`/`git reset --hard`/`git tag -d` needs explicit
user confirmation — they are destructive):**

1. **Locate the work.** Identify the stray branch holding the phase's commits
   (`git branch --contains <known-stranded-commit>`).
2. **Re-point the staging branch** at the stranded tip if it holds the intended
   work: `git branch -f version/{X.Y} <stray-branch>` (confirm first).
3. **Restore HEAD:** `git switch version/{X.Y}`; re-verify
   `git symbolic-ref --short HEAD`.
4. **If a bad release already promoted/tagged** an empty `dev`/`main`: rewind
   the affected refs to their pre-release tips (`git reset --hard <good-sha>`,
   per-action confirmation), delete the premature tag (`git tag -d <X.Y>`), then
   redo `grm-release-phase-merge` (`version/{X.Y}` → `dev`) and `grm-project-release`
   (`dev` → `main`, re-tag) cleanly.
5. **Re-verify** the staging/dev/main tips carry the expected commits before any
   push. Push only at the normal human-gated post-release moment.

**Prevention.** Run the HEAD-verification gate and the branch-content assertion
on every batch (see `integration-master-SKILL.md` §Dispatch isolation), and
treat a missing isolation footer as an isolation failure — re-dispatch rather
than merge.

## Recovering from an integration-branch fork (merge-forward)

**Distinct from** §Recovering from a stranded-branch / HEAD-drift incident
(that section addresses a HEAD that wandered off-staging inside a single repo
run). This section addresses a **structural fork** — where `main` and the
integration line have diverged because real work was authored on `main`
out-of-band and the integration line continued forward without it. The result:
`git merge-base main <integration>` returns a stale ancestor, and the two
lines carry disjoint commits. This is the canonical fork-recovery case.

**Detection.** BMI-2's divergence predicate fires before promotion:
`git diff --quiet <integration> main` exits 1 (trees differ) and at least one
commit in `<integration>..main` introduces tree content not reachable from the
integration line. The guard HALTs with a readable report:

```
DIVERGENCE: 'main' carries N commit(s) of work not on integration line '<INT>':
  <sha> <message>
  ...
Promotion BLOCKED. Reconcile by merging 'main' INTO '<INT>' (merge-forward);
do NOT reset across the fork (data loss).
```

**Do — merge-forward (the only safe procedure).** Bring the `main`-only
commits into the integration line and resolve there:

```bash
git switch <integration>          # e.g. dev, or the loop's integration branch
git merge --no-ff main            # pull main-only work forward onto the integration line
# ... resolve conflicts on the integration line — both lines' work is preserved;
#     every commit from both lines remains reachable in the resulting history ...
git commit                        # record the reconciliation merge
# Re-run the BMI-2 divergence check; trees now reconcile → promotion proceeds.
```

This is **non-destructive**: the merge commit's history reaches both parents, so
every main-only commit and every integration-line-only commit survives. Conflicts
are resolved once, on the integration line, and the result promotes cleanly.

**Do NOT — `reset --hard` across a fork.** Never resolve a fork by resetting
either tip onto the other:

```bash
git reset --hard main          # FORBIDDEN — silently destroys every integration-line-only commit
git reset --hard <integration> # FORBIDDEN — silently destroys every main-only commit
```

A reset across a real fork **silently deletes all commits unique to the losing
line**. This is data loss, not a fix. *(A `reset --hard` on a protected branch
is also independently blocked by `protected-branch-guard.sh` (#84) — but the
operative lesson here is: never reset across a fork, on any branch.)*

**Worked example.** Consider an integration line that diverged from `main`
when an entire shipped release plus a large dependency sync — several commits —
were authored **only on `main`** out-of-band, while the integration line kept
moving forward independently. The naive "just unblock it" move —
`git reset --hard main` onto the integration tip (or vice-versa) — would
**silently discard every commit unique to the losing line, including the entire
shipped release, with no trace**. The destructive-op confirmation gate is what
stops this; absent that gate, a shipped release would vanish. The correct
recovery is merge-forward: `git merge --no-ff main` into the integration line,
resolve the conflicts (including any semantic decision a human/master must make)
on the integration line, and promote the reconciled result.

**Under Noir autonomy**, the master never resolves a fork without surfacing to
the user first — a structural fork is one of the explicit stop conditions (merge
conflict with ambiguous intent). Pause, report the DIVERGENCE output, and wait
for human direction on which semantic choices to make in the merge resolution.

## GitHub PR boundary flow (github-pr, v3.5)

When `github-pr.enabled` is `true` (GitHub-hosted repo), the boundary merge is
performed **via a pull request** instead of a local `git merge --no-ff`. Read the
dial live: `github-pr.{enabled, boundary, merge-method, review.auto-dispatch,
review.post-comments}`. Absent/`false` ⇒ today's local-merge flow, unchanged.
**Suppressed under Stealth Mode** (a PR + branch push is a fingerprint).

At the configured `boundary` (`version-to-dev` default / `dev-to-main` / `both`;
under a PM, also lane `version/{X.Y}/<lane>` -> `version/{X.Y}`):

1. **Push the head branch** — a push-class action: propose-and-wait (human-gated)
   unless `autonomous-push.enabled`. `push-guard.sh` permits the `version/*` head
   **only because** `github-pr.enabled`; marker + destructive-flag rules unchanged.
2. **Open the PR** (idempotent): `grm-github-pr` skill /
   `python3 .claude/skills/grm-github-pr/github_pr.py open --base <B> --head <H> --plan <plan>`.
   On `degraded` (no `gh` / remote), fall back to the local merge and log it.
3. **Dispatch a Reviewer in PR mode** (if `review.auto-dispatch`): it reads the
   PR diff, runs `code-review`, and posts findings per `review.post-comments`
   (`off` / `comment` / `request-changes`). See the `grm-agent-reviewer` skill §2.5.
4. **Merge via the PR**: `github_pr.py merge --pr N --method <merge-method>` —
   **skip the local `--no-ff` merge at this boundary**. Do not merge while
   `reviewDecision == CHANGES_REQUESTED`. Boundaries not in `boundary` merge
   locally as today.

`grm-github-pr` does **not** imply autonomous push — open/merge stay governed by the
existing push gate. The full design is a framework-internal design — see the
upstream Grimoire repository for that rationale.

## Pushing to origin

**Human-gated by default.** A single trigger moment, once per release: after
`grm-project-release` promotes `dev` → `main` and tags, propose pushing `dev`,
`main`, and the version tag **together** and wait for explicit user
instruction. The earlier `version/{X.Y}` → `dev` integration no longer prompts
a push (`dev` stays local until release).

**Opt-in exception (#16):** if `grimoire-config.json` contains
`autonomous-push: { enabled: true }` (an explicit, never-inferred project
setting; default **false**), the master MAY push at that release moment
without waiting — the `push-guard.sh` mechanical rails still apply
(blessed-worktree marker required; only allowlisted refs — `dev`, `main`, and
the version tag; destructive flags always denied). With the flag absent or
`false`, behaviour is unchanged: propose and wait. See
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
