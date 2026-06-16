# Integration workflow — Weiss (Collaborative)

Audience: the **integration master** operating in Weiss mode — researcher and
assistant posture. Claude surfaces information and options; the user makes
every design and structural decision. Work-item agents do **not** need this
doc; their guide is `CLAUDE.md`.

Each step's authoritative procedure lives in the named skill; this doc is the
map between them.

1. **Plan scope** — `release-planning` skill. Read docs, roadmap, carryovers.
   Present a draft work-items report **with open design questions highlighted**.
   Do not lock anything until the user has resolved each question.
2. **Lock scope** — `release-agreement` skill. Present the report to the user;
   walk through each open item; wait for the user to say "agree" or "lock" for
   each. Only then freeze into `docs/release-planning-v{X.Y}.md` and create
   the `version/{X.Y}` integration branch.
3. **Distribute work** — `release-phase` skill. Present the dependency graph
   and model assignments. **Spawn one item at a time** — each with an explicit
   "Spawn `{ITEM-ID}`?" confirmation before calling `spawn_task`. Let the user
   pace the spawning.
4. **Track** — `release-agent-tracker` skill. Reconcile §5 ledger with live
   branches. Present the ready-to-merge list to the user; ask which to merge
   first.
5. **Integrate** — `release-phase-merge` skill. For each branch: show the diff
   summary, ask "Merge `{branch}`?", wait for confirmation, merge, report test
   result. Repeat per branch.
6. **Release** — `project-release` skill. Present the pre-release checklist;
   wait for the user to say "release." Promotes `dev` → `main` and tags.

The integration master is the **only** role that merges into
`version/{X.Y}`, `dev`, or `main`. Work-item agents never do.

## Branch model

```
version/<number>  ──►  dev  ──►  main
```

Work items run in isolated worktrees spawned via `spawn_task`. Only
`version/*`, `dev`, and `main` are named, protected integration branches.

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

## Workflow-based orchestration

Use `Workflow` only when the user explicitly requests multi-agent
orchestration. Always describe what the workflow will do and ask for
confirmation before running. Present results to the user before taking any
file-writing or branch-creating next step.

**Write-capable workflows are Noir-only.** In Weiss the read-only convention
is enforced; write-capable workflows (`meta.tier = 'write-capable'`) require
the Noir paradigm and will fail closed if invoked here. See
`docs/design/write-capable-workflow-design.md`.

## Dead-worktree cleanup

Verify dead-ness (see Supervised `integration-workflow.md` §Dead-worktree
cleanup for the full procedure). Always report what you find and ask before
removing any worktree, even a clean one.

**Post-release cleanup step.** Cleanup is also a named, ordered release step,
run once after `project-release` tags the version and the push completes
(`project-release` §Post-release cleanup drives it; `release-phase-merge`
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
2. **Open the PR** (idempotent): `github-pr` skill /
   `python3 .claude/skills/github-pr/github_pr.py open --base <B> --head <H> --plan <plan>`.
   On `degraded` (no `gh` / remote), fall back to the local merge and log it.
3. **Dispatch a Reviewer in PR mode** (if `review.auto-dispatch`): it reads the
   PR diff, runs `code-review`, and posts findings per `review.post-comments`
   (`off` / `comment` / `request-changes`). See the `reviewer` skill §2.5.
4. **Merge via the PR**: `github_pr.py merge --pr N --method <merge-method>` —
   **skip the local `--no-ff` merge at this boundary**. Do not merge while
   `reviewDecision == CHANGES_REQUESTED`. Boundaries not in `boundary` merge
   locally as today.

`github-pr` does **not** imply autonomous push — open/merge stay governed by the
existing push gate. Full design: `docs/design/github-pr-integration-design.md`.

## Pushing to origin

A single trigger moment, once per release: after `project-release` promotes
`dev` → `main` and tags, propose pushing `dev`, `main`, and the version tag
**together** and wait for explicit user instruction — never push automatically.
The earlier `version/{X.Y}` → `dev` integration no longer prompts a push (`dev`
stays local until release). This adjusts only how often the push is prompted
(once); it stays human-gated and marker-gated.

Destructive flags (`--force`, `--all`, etc.) are always denied; have the
human run them if genuinely needed.

## Lane model & multiple marked lane worktrees (v3.1)

When a **Project Manager** owns a multi-feature release (see
`docs/design/project-manager-role-design.md` and
`.claude/skills/project-manager/SKILL.md`), the single `version/{X.Y}` staging
line is split into **parallel lanes**, each implemented by its own integration
master:

- **Lane branches.** The PM creates `version/{X.Y}` off `dev`, then a lane branch
  `version/{X.Y}/<lane>` off it per non-colliding lane (the lanes come from the
  overlap analysis — `pm_overlap.py`). The `version/.*` shape keeps lane branches
  inside the protected set, so the existing guards cover them unchanged.
- **One integration master per lane**, each in its own marker-blessed worktree,
  merging its task agents' branches into its lane branch via `release-phase-merge`
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
`release-plan-guard.sh`, `worktree-guard.sh`. The Weiss posture adds
user-confirmation requirements on top of the mechanical guards; it does not
relax any hook.

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
