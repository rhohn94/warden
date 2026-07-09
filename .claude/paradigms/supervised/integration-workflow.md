# Integration workflow (integration / release master)

Audience: the **integration master** — the agent that owns release scope,
spawns work-item sessions, and integrates the results. Work-item agents do
**not** need this doc; their guide is `CLAUDE.md`.

Each step's authoritative procedure lives in the named skill; this doc is the
map between them.

1. **Plan scope** — `grm-release-planning` skill. Produces the work-items report
   for the next version (reads design docs, roadmap, carryovers).
2. **Lock scope** — `grm-release-agreement` skill. **Supervised gate: present the
   report and wait for explicit user approval before locking.** Freezes the
   report into `docs/release-planning-v{X.Y}.md` (§5 ledger) and creates the
   `version/{X.Y}` **integration branch** off `dev`.
3. **Distribute work** — `grm-release-phase` skill. **Supervised gate: list the
   batch and ask "Spawn now?" before calling `spawn_task`.** For each work item
   in the next open phase (grouped by the §3 conflict map, sized per the
   `grm-repo-reference` model/effort table), it calls **`spawn_task`** to drop a
   chip that opens a new session in its **own isolated worktree**. The
   recommended model rides along in the chip so you set it when you open the
   session.
4. **Track** — `grm-release-agent-tracker` skill. Reconciles the §5 ledger with
   live branches to determine what is ready to merge.
5. **Integrate** — `grm-release-phase-merge` skill. **Supervised gate: ask "Merge?"
   before each `git merge --no-ff`.** Merges completed work-item branches into
   `version/{X.Y}` in conflict-map order, runs tests after each, ticks §5
   (`grm-ledger-tick`). When all phases are ☑, asks user before the final
   `version/{X.Y}` → `dev` merge.
6. **Release** — `grm-project-release` skill. Promotes `dev` → `main` and tags.
   Full procedure: `docs/version-design.md` §Release procedure.

Companion docs: `docs/version-design.md` (versioning + release recipe).

The integration master is the **only** role that merges into
`version/{X.Y}`, `dev`, or `main`. Work-item agents never do.

## Branch model

```
version/<number>  ──►  dev  ──►  main
```

Work items are **not** a named branch tier. Each runs in its own isolated
worktree (spawned via `spawn_task`), commits on that worktree's branch rooted
at `version/{X.Y}`, and the integration master merges the completed branch in.
Only `version/*`, `dev`, and `main` are named, protected integration branches.

## Distributing work with `spawn_task`

Instead of handing the user copy-paste prompts, the integration master calls
the **`spawn_task`** tool once per work item. Each call drops a chip the user
clicks to open a new session in a fresh isolated worktree, pre-loaded with the
item's self-contained prompt. `grm-release-phase` builds these calls; it sizes each
item per the `grm-repo-reference` table and names the recommended model in the chip
(`spawn_task` cannot set a session's model, so the user picks it when opening).

**Supervised gate:** before calling `spawn_task`, list the batch and ask the
user for confirmation. Do not spawn until the user approves.

## Delegating to subagents

The integration master may also spawn **`Agent`** subagents to work more
efficiently — e.g. a `haiku` subagent for mechanical / read-only work (log
extraction, diff summaries) or a `sonnet` subagent for mid-complexity edits and
test runs. Match model/effort to the work per the `grm-repo-reference` table;
reserve `opus`/high for review and integration judgement. These two mechanisms
are distinct: `spawn_task` opens **new work-item sessions** in their own
worktrees; `Agent` spawns **helper subagents inside** the integration master's
own session.

## Workflow-based orchestration (read-only analysis)

A third mechanism, **`Workflow`**, runs a deterministic JavaScript script that
fans subagents out (`parallel`/`pipeline`) and collects their structured
results — autonomously, with no human-clicked chip and no per-agent worktree.
It is the right tool for the **read-heavy, analysis** seams *around* the
release pipeline; it is **not** a replacement for `spawn_task`, which exists
precisely because work items are interactive, worktree-isolated, and mutate
code on a branch.

Use the right mechanism for the shape of the work:

| Mechanism | Human in loop | Isolation | Mutates code | Best for |
|---|---|---|---|---|
| `spawn_task` | yes (chip) | worktree per item | yes (commits) | distributing work items (`grm-release-phase`) |
| `Agent` | no | shared session | via tools | helper subagents inside the master's session |
| `Workflow` | no | shared, read-mostly | no (by convention) | parallel read/verify/synthesis fan-out |

**`Workflow` is opt-in and billed** — it can spawn many agents. Only the
integration master runs one, and only when the user has asked for multi-agent
orchestration (or invoked a skill/workflow that does). Workflow scripts are
**read-only by convention here**: they write no files and create no branches,
so they never collide with the worktree-isolation or protected-branch hooks.
A workflow returns its result to the master, who reviews it and takes any
file-writing / branch-creating next step through the normal skills.

Saved workflows live in `.claude/workflows/<name>.js` (filename = the name you
invoke). The first one shipped is **`grm-release-planning`** — a fan-out variant of
the `grm-release-planning` skill.

> **Flavor note.** `Workflow` is a Claude Code feature; the `copilot/` flavor
> has no equivalent and does not mirror `.claude/workflows/`. Keep workflow
> orchestration in the `claude-code/` (and root) flavors only.

## Dead-worktree cleanup

A work-item worktree is **dead** once its branch is merged in and its working
tree is clean. The integration master may remove dead worktrees as
housekeeping after the merge step — `grm-release-phase-merge` ticks §5; this
removes the now-orphaned worktree directory and branch ref.

**Verify dead-ness first:**

1. **Branch is fully merged** — `git log <protected>..<branch>` returns
   nothing (where `<protected>` is `version/{X.Y}`, `dev`, or `main` —
   whichever the branch was meant to land on).
2. **Working tree is clean** — `git -C <worktree-path> status --porcelain`
   is empty.

If (2) is false, **never silently force-remove**. Try to preserve first:

- Tag a stash on the worktree:
  `git -C <path> stash push -u -m "wip-rescue: <branch>"`, or
- Save the diff aside to a gitignored location (outside the repo, or a path
  you've added to `.gitignore`):

  ```bash
  git -C <path> diff > <rescue>/<branch>.patch
  git -C <path> diff --cached >> <rescue>/<branch>.patch
  ```

Then **report what was preserved and where** before proceeding. If
preservation isn't reliable — untracked binaries, files you can't classify —
**stop and surface to the user** rather than guess.

**Removal procedure** (only once dead-ness is verified):

```bash
git worktree remove <path>      # refuses if not clean — that's the safety net
git branch -d <branch>          # safe-delete; refuses if not merged
```

Use `-D` (force-delete the branch) only with explicit user confirmation, per
`CLAUDE.md` §Commits.

The `worktree-guard.sh` hook honors the `integration-allow.local` marker
symmetrically with `protected-branch-guard.sh`, so the blessed worktree may
cross worktree boundaries for this housekeeping. A non-blessed worktree
still fails closed.

### Post-release cleanup step (named release-flow step)

The cleanup above is also a **named, ordered step of the release flow**, run
once per release after `grm-project-release` tags the version and the human-gated
push completes (`grm-project-release` §Post-release cleanup drives it;
`grm-release-phase-merge` cross-references it). The **marker-blessed integration
master is the only actor** that may run it — per the cross-worktree branch
hijack rule (§Enforcement), only the blessed worktree may touch sibling
worktrees; a non-blessed agent fails closed.

For **each** work-item branch/worktree of the just-shipped release, in order:

1. **Verify dead-ness** using the two checks above — the branch is an ancestor
   of the release tip (merged) AND the worktree is clean. Skip any branch that
   is not both.
2. **Preserve or report** any uncommitted work (stash/patch per above);
   **never discard silently**.
3. **Unlock** a locked worktree before removing it:
   ```bash
   git worktree unlock <path>      # only if locked
   git worktree remove <path>      # refuses if not clean
   ```
   If the only thing keeping a tree "dirty" is disposable untracked artifacts
   (e.g. `__pycache__`, build caches), `git worktree remove --force <path>` is
   allowed **only with an explicit logged note** of exactly what is discarded —
   never a silent `--force`.
4. **Prune** stale administrative entries, then **safe-delete** the merged
   feature branches and any leftover `worktree-*` placeholder branches with
   `-d` (merge-safe; refuses unmerged):
   ```bash
   git worktree prune
   git branch -d <feature-branch> <worktree-*placeholder>
   ```
   `-D` is reserved for the explicit-confirmation destructive path (§Commits in
   `CLAUDE.md`).

Report the final tally: worktrees removed, branches deleted, and any work
preserved (with its rescue location) or skipped.

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
   (`off` / `comment` / `request-changes`). See the `grm-reviewer` skill §2.5.
4. **Merge via the PR**: `github_pr.py merge --pr N --method <merge-method>` —
   **skip the local `--no-ff` merge at this boundary**. Do not merge while
   `reviewDecision == CHANGES_REQUESTED`. Boundaries not in `boundary` merge
   locally as today.

`grm-github-pr` does **not** imply autonomous push — open/merge stay governed by the
existing push gate. Full design: `docs/design/github-pr-integration-design.md`.

## Pushing to origin

Pushing is **integration-master-only** and restricted to whitelisted refs.
`push-guard.sh` enforces both.

**When.** A single trigger moment, once per release: after `grm-project-release`
promotes `dev` → `main` and creates the release tag. At that one prompt the
integration master pushes `dev`, `main`, and the version tag **together**.
Outside that moment, don't push.

The earlier `version/{X.Y}` → `dev` integration (end of `grm-release-phase-merge`)
**no longer prompts a push** — `dev` stays local until release, and its commits
ride to origin alongside `main` and the tag at the single post-release push.
This consolidates a multi-phase release to exactly one push prompt.

**Supervised gate:** always propose the push (`git push origin <ref>`) and
wait for explicit user confirmation before running the command. The push
remains human-gated and marker-gated — this change adjusts only how many
times the push is prompted (once), not who authorises or runs it.

**What.** Default allowlist: `main`, `dev`, and any version tag matching
`v?\d+(\.\d+){0,3}(-...)?`. Project additions live in
`.claude/push-allowlist` (tracked, one ref per line).

**Always denied** (even with the marker):

- Destructive flags: `--force`, `--force-with-lease`, `--all`, `--mirror`,
  `--delete`, `--prune`.
- Remote-ref deletion (`git push origin :branch`).
- Refs not on the allowlist.

If a denied push is genuinely needed, have the human run it from their
terminal — the hook gates only the agent's tool calls.

## UX design language

A **project-init concern**, not a per-release concern. See
`docs/design/ux-design-language-design.md` for the full spec.

**`grm-design-language-adapt`** has two trigger moments:

1. **Initial adoption** — `grm-repo-init` Step 6, at day zero for GUI projects.
2. **On-demand refresh** — re-run when the upstream source has changed; the
   skill diffs upstream against the recorded `source-sha:` and surfaces
   changes for selective review.

**`grm-ux-demo-build`** is opt-in. Use it when the user wants to verify the
current adaptation against the project's own stack. It is never triggered
automatically by `grm-design-language-adapt`.

The per-project stub lives at `docs/design/ux/design-language.md`.

**Non-GUI projects.** Projects without a GUI yet defer via a `## Backlog` row
in `docs/roadmap.md` (`- UX design language: deferred until v{X.Y}.`).
Headless projects (no GUI planned) skip entirely — `grm-workflow-bootstrap` marks
both skills N/A in the manifest.

## Lane model & multiple marked lane worktrees (v3.1)

When a **Project Manager** owns a multi-feature release (see
`docs/design/project-manager-role-design.md` and
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

The integration master operates the single **marker-blessed worktree** —
the one carrying an untracked `.claude/integration-allow.local` file. Four
`PreToolUse` Bash hooks back the discipline so a stray agent commit, edit,
or push cannot land on a protected branch:

- `protected-branch-guard.sh` — **deny-by-default**: blocks git
  history-mutation (`commit`/`merge`/`rebase`/`cherry-pick`/`revert`) on
  `dev` / `main` / `version/*` from any worktree without the
  `integration-allow.local` marker. Work-item / fresh worktrees fail **closed**
  (no marker ⇒ blocked); only the integration worktree is allowed. It also
  enforces the **git-protocol governance** rule (#84): history-**rewriting**
  commands — `git rebase`, `git cherry-pick`, `git reset --hard` — are blocked
  on a protected branch for **every** actor (the marker-blessed master
  included), since rewriting shared history is a last-resort op, not routine.
  Escape hatches (`--abort`/`--quit`/`--skip`) and soft/mixed resets stay
  allowed.
- `push-guard.sh` — **deny-by-default**: blocks `git push` from any worktree
  without the marker; with the marker, restricts pushes to allowlisted refs
  (`main`, `dev`, version tags, plus `.claude/push-allowlist`). Destructive
  flags and remote-ref deletion deny outright. See §Pushing to origin.
- `release-plan-guard.sh` — blocks edits to §§1–4 of an agreed release plan,
  allowing only §5 (ledger) updates.
- `worktree-guard.sh` — blocks tool calls targeting paths outside the current
  worktree, **unless** the worktree carries the `integration-allow.local`
  marker (the blessed worktree may cross boundaries for housekeeping — see
  §Dead-worktree cleanup). Symmetric with `protected-branch-guard.sh`.

**Cross-worktree branch hijack rule (v1.7).** A spawned/work-item agent must
git-operate **only on its own worktree**. The v1.6 vet caught a spawned
agent's `git switch -c` running against the integration master's worktree —
silently switching the master off `version/{X.Y}`. The rule, enforced
fail-closed by both guards: an **unmarked** actor that redirects a branch op
(`switch` / `checkout` / `branch`) at a **different** worktree — via
`git -C <path>`, `--git-dir`/`--work-tree`, or a `cd`/`pushd` into another
worktree — is **refused (`exit 2`)**, and the refusal names the integration
master explicitly when the target carries the marker. The marker-blessed
master operating on its own worktree (or crossing boundaries for the
housekeeping in §Dead-worktree cleanup) is unaffected. Agents must verify
`cwd` and branch in place from the staging ref (`git switch -c <branch>
version/{X.Y}`), never `git switch` an existing worktree.

Escape hatches (`--abort`/`--quit`/`--skip`) stay allowed so a recovery is
never trapped. To legitimately mutate a protected branch in a worktree (e.g.
the final `version/{X.Y} → dev` handoff or the `dev → main` promotion),
create the marker there deliberately:

```bash
touch .claude/integration-allow.local
```

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
`--no-ff` merge protocol — never a direct push, never a force-merge. These rules
are **enforced mechanically**, not merely documented:

- `protected-branch-guard.sh` blocks `git rebase` / `git cherry-pick` /
  `git reset --hard` on a protected branch (every actor; escape hatches and
  soft/mixed resets exempt).
- `push-guard.sh` blocks force-push, force flags, broad flags, and remote-ref
  deletion (see §Pushing to origin).

If history truly must be rewritten (a genuine last resort), a human runs the
command deliberately outside the agent — the agent never does it autonomously.
The recovery procedures in §Recovering from a stranded-branch / HEAD-drift
incident are the sanctioned exception, and each destructive step there requires
explicit per-action confirmation.
