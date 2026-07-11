# Integration workflow — Weiss (Collaborative)

> **Up:** [↑ Docs](README.md)

Audience: the **integration master** operating in Weiss mode — researcher and
assistant posture. Claude surfaces information and options; the user makes
every design and structural decision. Work-item agents do **not** need this
doc; their guide is `CLAUDE.md`.

Each step's authoritative procedure lives in the named skill; this doc is the
map between them.

1. **Plan scope** — `grm-release-planning` skill. Read docs, roadmap, carryovers.
   Present a draft work-items report **with open design questions highlighted**.
   Do not lock anything until the user has resolved each question.
2. **Lock scope** — `grm-release-agreement` skill. Present the report to the user;
   walk through each open item; wait for the user to say "agree" or "lock" for
   each. Only then freeze into `docs/release-planning/release-planning-v{X.Y}.md` and create
   the `version/{X.Y}` integration branch.
3. **Distribute work** — `grm-release-phase` skill. Present the dependency graph
   and model assignments. **Spawn one item at a time** — each with an explicit
   "Spawn `{ITEM-ID}`?" confirmation before calling `spawn_task`. Let the user
   pace the spawning.
4. **Track** — `grm-release-agent-tracker` skill. Reconcile §5 ledger with live
   branches. Present the ready-to-merge list to the user; ask which to merge
   first.
5. **Integrate** — `grm-release-phase-merge` skill. For each branch: show the diff
   summary, ask "Merge `{branch}`?", wait for confirmation, merge, report test
   result. Repeat per branch.
6. **Release** — `grm-project-release` skill. Present the pre-release checklist;
   wait for the user to say "release." Promotes `dev` → `main` and tags.

The integration master is the **only** role that merges into
`version/{X.Y}`, `dev`, or `main`. Work-item agents never do.

## Branch model

```
version/<number>  ──►  dev  ──►  main
```

Work items run in isolated worktrees spawned via `spawn_task`. Only
`version/*`, `dev`, and `main` are named, protected integration branches.

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

## Researcher/assistant posture

Claude's role in Weiss is to inform and execute — not to lead.

- Surface the dependency graph; ask the user to choose the grouping.
- Flag token-estimate uncertainty; ask the user to confirm the model.
- Summarise each diff; ask before merging.
- List conflicts; resolve with user guidance.
- Propose each push; wait for instruction.

**Decision log:** maintain a brief running log of user decisions during the
session (scope resolutions, model overrides, sequencing choices). Reference
it when questions recur so you don't re-ask what the user already decided.

## Delegating to subagents

Spawn `Agent` subagents for mechanical / read-only work when it helps you
gather information faster for the user. Present the subagent's findings; do
not act on them without user direction.

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

Under Weiss, the integration master **offers** to spawn a Reporter via
`spawn_task` and waits for user confirmation — it does not auto-spawn. Once
confirmed, use this prompt template verbatim:

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
`version/*` or `main` branch. Full role definition, spawn mechanics, and
anti-patterns: `grm-agent-reporter` §1–§7.

## Workflow-based orchestration

Use `Workflow` only when the user explicitly requests multi-agent
orchestration. Always describe what the workflow will do and ask for
confirmation before running. Present results to the user before taking any
file-writing or branch-creating next step.

**Write-capable workflows are Noir-only.** In Weiss the read-only convention
is enforced; write-capable workflows (`meta.tier = 'write-capable'`) require
the Noir paradigm and will fail closed if invoked here. The full design is a
framework-internal design — see the upstream Grimoire repository for that
rationale.

## Dead-worktree cleanup

Verify dead-ness (see Supervised `integration-workflow.md` §Dead-worktree
cleanup for the full procedure). Always report what you find and ask before
removing any worktree, even a clean one.

**Post-release cleanup step.** Cleanup is also a named, ordered release step,
run once after `grm-project-release` tags the version and the push completes
(`grm-project-release` §Post-release cleanup drives it; `grm-release-phase-merge`
cross-references it). Only the **marker-blessed master** may run it. For each
work-item branch/worktree: verify merged + clean, **preserve or report** any
uncommitted work (never silent `--force`), `unlock` then `git worktree remove`,
`git worktree prune`, and safe-delete merged + leftover `worktree-*` branches
with `-d`. Present the tally to the user.

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

A single trigger moment, once per release: after `grm-project-release` promotes
`dev` → `main` and tags, propose pushing `dev`, `main`, and the version tag
**together** and wait for explicit user instruction — never push automatically.
The earlier `version/{X.Y}` → `dev` integration no longer prompts a push (`dev`
stays local until release). This adjusts only how often the push is prompted
(once); it stays human-gated and marker-gated.

Destructive flags (`--force`, `--all`, etc.) are always denied; have the
human run them if genuinely needed.

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
`release-plan-guard.sh`, `worktree-guard.sh`, `bundled-sync-guard.sh`. The
Weiss posture adds user-confirmation requirements on top of the mechanical
guards; it does not relax any hook.

**Bundled-sync-commit guard (v3.67, #126 criterion 3).** `bundled-sync-guard.sh`
denies (`exit 2`) a `git commit` whose staged changes span BOTH
`grm-sync-from-upstream`'s typical touch-set (`.claude/`, `CLAUDE.md`,
`AGENTS.md`, `docs/grimoire/`, the `.github/` Copilot mirror) and
`grm-design-language-adapt`'s typical touch-set (`docs/design/ux/`,
`vendor/aura/`, `static/aura/`, `templates/base.html`) at once — the mechanical
enforcement of BMI-3 Rule 3c (previously a reference.md reminder only),
closing the exact `24c73dd` "660-file framework + Aura in one commit"
anti-pattern from #126. Applies to every actor; no marker exemption.

**Cross-worktree branch hijack rule (v1.7).** A spawned/work-item agent must
git-operate **only on its own worktree**. An **unmarked** actor that redirects
a branch op (`switch` / `checkout` / `branch`) at a **different** worktree —
via `git -C <path>`, `--git-dir`/`--work-tree`, or a `cd`/`pushd` into another
worktree — is **refused (`exit 2`)** by both guards; the refusal names the
integration master when the target carries the marker. The marker-blessed
master on its own worktree (or crossing boundaries for §Dead-worktree cleanup)
is unaffected. Agents branch in place from the staging ref
(`git switch -c <branch> version/{X.Y}`).

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
remote-ref deletion. If history truly must be rewritten (a genuine last resort),
a human runs the command deliberately outside the agent.

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
line**. This is data loss, not a fix.

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
