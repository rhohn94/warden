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

A **project-init concern**, not a per-release concern. The full spec is a
framework-internal design — see the upstream Grimoire repository for that
rationale.

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

The integration master operates the single **marker-blessed worktree** —
the one carrying an untracked `.claude/integration-allow.local` file. Five
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
- `bundled-sync-guard.sh` — **(v3.67, #126 criterion 3)** denies a `git commit`
  whose staged changes span BOTH `grm-sync-from-upstream`'s typical touch-set
  (`.claude/`, `CLAUDE.md`, `AGENTS.md`, `docs/grimoire/`, the `.github/`
  Copilot mirror) and `grm-design-language-adapt`'s typical touch-set
  (`docs/design/ux/`, `vendor/aura/`, `static/aura/`, `templates/base.html`) at
  once — the mechanical enforcement of BMI-3 Rule 3c (previously a
  reference.md reminder only), closing the exact `24c73dd` "660-file
  framework + Aura in one commit" anti-pattern from #126. Applies to every
  actor; no marker exemption.

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
