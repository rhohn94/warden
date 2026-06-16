---
name: repo-init
description: Initialize a new git repository with this project's branch model, commit discipline, and push protection. Sets up `main` + `dev`, documents the `version/*` → `dev` integration hierarchy, enforces atomic one-sentence commits with no Co-Authored-By trailer, and installs the no-push guard so only a human can push. Use when starting a brand-new repo, running `git init`, or setting up version control / the branch structure for a project. Triggers on "create a git repo", "initialize the repository", "git init", "set up version control", "set up the branch structure", "bootstrap git".
---

# Repo initialization

Stands up a new repository with the branch model, commit rules, and guard
hooks this project relies on. Run once, at the start of a project's life.
For installing/restoring the *workflow skill set* use `workflow-bootstrap`;
for promoting `dev` → `main` at release time use `project-release`. This
skill is the day-zero git setup that those assume.

---

## Branch model

A three-tier integration hierarchy. Work flows **up**; releases flow **out**.

```
version/<number>  ──►  dev  ──►  main
 (one release's        (single-      (releases,
  worth of work)        version         each tagged)
                        staging)
```

| Branch | Role | Merges into | Who merges |
|---|---|---|---|
| `main` | Production trunk. **Every commit is a release and is tagged.** | — | integration master only |
| `dev` | Staging trunk for final review, dev-env deploys, and last fixes before release. Holds **one version at a time** — it is not a long-lived multi-version trunk. | `main` (at release) | integration master only |
| `version/<number>` | Integration branch for **all work** going into a release (e.g. `version/1.4`). | `dev` | integration master only |

Rules:

- **`main` only ever receives release merges**, never direct work. Each such
  commit gets a version tag (`v<number>`) — see `project-release`.
- **`dev` is single-version.** Do not let two versions' worth of change pile
  up on `dev`; promote to `main` and start the next `version/*` clean.
- A `version/*` branch is cut when a release is scoped and merged to `dev`
  when its work is done.
- Work items run in their **own isolated worktrees** (spawned via `spawn_task`),
  branched in place from `version/<number>`. The integration master merges each
  completed worktree branch back into `version/*`. There is no long-lived
  per-item branch tier.
- Protected branches (`main`, `dev`, `version/*`) are mutated **only** by the
  integration master — the worktree carrying the
  `.claude/integration-allow.local` marker. Everyone else branches in place.

> These names are used consistently across the workflow: the release skills
> (`release-agreement`, `release-phase-merge`, …) cut, merge, and tear down
> `version/<number>` branches, work items run in spawned worktrees, and
> `protected-branch-guard.sh` protects `main` / `dev` / `version/*`.

---

## Initialization procedure

0. **Require an existing repo (fail-soft guard).** `repo-init` no longer
   creates a repository from nothing — it builds the branch model on top of one
   that already exists. Repo *creation* is owned by the `onboarding` skill
   (which detects a non-git dir, confirms with the user, and runs `git init` +
   an initial scaffold commit before calling `repo-init`). Defence in depth: if
   `repo-init` is invoked standalone in a non-git directory, stop with guidance
   and mutate **nothing**.

   ```bash
   if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
     echo "No git repository found. Run the 'onboarding' skill, or 'git init' first, then re-run repo-init."
     exit 1
   fi
   ```

   Only when this check passes does the branch model below run. (Onboarding has
   already produced a repo on `main` with one commit, so step 1's `git init` is
   a no-op there — see step 1.)

1. **Init on `main`** *(no-op when onboarding already created the repo)*.

   ```bash
   git init -b main
   ```

   (If `init.defaultBranch` is already `main`, plain `git init` is fine. Re-running
   `git init` on an existing repo is safe and idempotent.)

2. **Add a `.gitignore`** covering OS cruft (`.DS_Store`), build output, and
   local-only files — including the per-machine Claude settings and the
   integration marker:

   ```gitignore
   .DS_Store
   .claude/settings.local.json
   .claude/integration-allow.local
   ```

3. **Make an initial commit** of the scaffold (one sentence, no trailer — see
   Commit discipline). This bootstrap commit on `main` is the pre-history
   baseline; it may be tagged `v0.0` or left untagged. After it, `main`
   receives only release merges.

   This canonical (non-worktree) checkout is the integration master's home, so
   give it the marker that lets it commit/merge on protected branches:

   ```bash
   touch .claude/integration-allow.local   # gitignored; local-only
   ```

4. **Branch `dev` off `main`** — the staging trunk all work descends from:

   ```bash
   git branch dev
   ```

5. **Cut the release integration branch as work begins**, never up front:

   ```bash
   git switch -c version/1.0 dev           # release integration branch
   ```

   Name the **ref** you branch from (not ambient HEAD) so you root on the
   intended tip even across worktrees. Work items branch off `version/1.0` in
   their own spawned worktrees — you don't cut them here.

6. **UX design language (optional).** Choose the branch that matches the
   project's GUI status:

   - **GUI project (has a GUI now):** run `design-language-adapt` to establish
     `docs/design/ux/design-language.md` and produce the initial adaptation
     from the upstream design language. Also ensure `.design-language-source/`
     is listed in `.gitignore` (the skill appends it if absent). `ux-demo-build`
     is the natural next step — it is opt-in and user-initiated.

   - **GUI deferred (will have a GUI later):** add one row under `## Backlog`
     in `docs/roadmap.md` (or file it as an issue via the configured tracker
     — see note below):

     ```
     - UX design language: deferred until v{X.Y}
     ```

     This entry surfaces during release planning so the integration master can
     schedule the work for the target version. The deferral is never a hidden
     marker file — the roadmap is the canonical visible state.

     **Configured tracker note:** if `.claude/grimoire-config.json` already
     contains an `issue-tracker` block with a non-roadmap provider (e.g.
     `"github"`), do **not** seed a `## Backlog` section in `docs/roadmap.md`
     for issue tracking. Instead:
     - Either seed a stub line:
       `- Issues tracked in <provider> (see \`issue-tracker-switch list\`)`
     - Or omit the Backlog section entirely and leave `docs/roadmap.md`
       with only the `## Roadmap` and `## Framework-required` narrative
       sections.
     When the block is absent (roadmap default), the Backlog section is seeded
     as normal — this is the zero-change path for all existing projects.

   - **Headless / never-GUI:** skip this step entirely. Note in `repo-reference`
     that the `docs/design/ux/` tier is N/A for this project;
     `design-language-adapt` and `ux-demo-build` are not applicable.

---

## Commit discipline

- **Atomic.** One logical change per commit; only commit code that builds.
- **One sentence, max.** The commit message never exceeds a single sentence.
- **No `Co-Authored-By` trailer.** Do not add "Co-Authored-By: Claude" (or any
  co-author line) to commit messages in this project.

---

## Push protection (`push-guard`)

Pushing is restricted by formal policy and enforced by
`.claude/hooks/push-guard.sh`, a `PreToolUse` Bash hook on every `git push`
/ `git send-pack`. Without the integration marker
(`.claude/integration-allow.local`), pushes deny outright — task agents and
fresh worktrees never push. With the marker, pushes deny unless every ref
is on the allowlist: `main`, `dev`, version tags, plus any refs in
`.claude/push-allowlist` (tracked, one ref per line). Destructive or broad
flags (`--force`, `--all`, `--mirror`, `--delete`, `--prune`, remote-ref
deletion) deny even with the marker — have the human run them themselves
if truly intended.

The integration master considers a push at a single moment, once per release:
the end of `project-release` (pushing `dev` + `main` + tag together).
`release-phase-merge` no longer pushes. Outside that moment, don't push. Full
policy: `docs/integration-workflow.md` §Pushing to origin.

To install in a new repo:

1. Copy `push-guard.sh` into `.claude/hooks/`.
2. Copy `.claude/push-allowlist` (empty / comment-only is fine) to the repo.
3. Wire `push-guard.sh` as a `PreToolUse` hook with matcher `Bash` in
   `.claude/settings.json`.
4. **`chmod +x` it.** The harness runs hook commands as bare paths; a
   non-executable hook fails with "Permission denied" (exit 126) and is
   treated as a *non-blocking* error — i.e. it silently does not guard
   anything. Every hook in `.claude/hooks/` must be executable.

---

## Verify

```bash
git symbolic-ref --short HEAD          # → main, then create dev
git branch                             # main, dev present
ls -l .claude/hooks/*.sh               # all -rwx (executable)
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"git push"}}' \
  | .claude/hooks/push-guard.sh; echo "exit=$?"   # → exit=2 (blocked, no marker)
```

---

## Anti-patterns

- Running `repo-init` in a non-git directory expecting it to bootstrap a repo
  from scratch — it no longer does. The fail-soft guard (step 0) stops and
  defers repo *creation* to the `onboarding` skill; `repo-init` only builds the
  branch model on top of an existing repo.
- Committing work directly to `main` or `dev` — `main` takes only tagged
  release merges; `dev` takes only `version/*` merges.
- Letting `dev` accumulate more than one version's worth of change.
- Creating `version/*` branches up front — cut them as the work starts, from
  `dev`.
- Leaving hooks non-executable — they silently no-op and enforce nothing.
- Adding a `Co-Authored-By` trailer, or a multi-sentence commit message.
- A non-integration worktree (no marker) running `git push` — only the
  integration master pushes, and only allowlisted refs at the two trigger
  moments.
- Skipping UX design language for a GUI project without recording the deferral
  as a `## Backlog` row in `docs/roadmap.md` (or filing it via the configured
  tracker when a non-roadmap provider is active).
- Seeding a `## Backlog` issue-tracking section in `docs/roadmap.md` when a
  non-roadmap `issue-tracker` provider is already configured — seed a stub or
  omit the section; new issues go to the configured tracker, not the roadmap.
