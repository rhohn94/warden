---
name: grm-worktree-preflight
description: Verify a fresh / spawned worktree is rooted on its staging ref (version/{X.Y} or dev), not main, before any commit or merge. Triggers on "fresh worktree", "spawned session", "before I commit", "new branch", "merge into dev", "rebase onto dev", or unexplained release-only diffs.
---

# Worktree preflight

Harness-spawned worktrees frequently check out at `main`'s tip. `main` carries
release-only commits (version bumps, build artifacts, changelog entries) that
must **never** flow back through a work branch into `dev`.

Run this skill at the start of a spawned work-item session, and before any
`git switch -c`, first commit on a fresh worktree, or `git merge` / `git
rebase`.

## The check

```bash
git merge-base HEAD dev
git rev-parse dev
```

Both SHAs must match. If they do, the branch is rooted on `dev` — proceed.
If they don't, the branch was rooted elsewhere (almost always `main`).

A second sanity check, especially before merging back to `dev`:

```bash
git log --oneline dev..HEAD | grep -Ei 'dist/|version.bump|changelog|release' || echo OK
```

Any hit means release-only commits are reachable from the branch tip — stop
and remediate. Adjust the pattern to match your project's release commit
conventions.

## Remediation

Pick the case that matches.

> **Git-protocol governance.** The default is branch-and-merge;
> history-rewriting commands (`git reset --hard`, `git rebase`) are a
> **last resort**, used here only to re-root a *wrong-based work-item branch*
> (an unprotected branch — never `dev` / `main` / `version/*`, where the guard
> blocks them outright). Each destructive step **requires explicit per-action
> user confirmation**. If you can avoid rewriting — e.g. by re-creating a fresh
> branch from the correct ref and re-applying changes — prefer that.

### Case A — wrong base, **no commits yet** on the branch

No work to lose, so the simplest fix is to **re-create the branch from the
correct ref** (no history rewrite at all):

```bash
git switch -c <branch> dev   # branch in place from the staging ref
```

If you must keep the same branch name in place, the last-resort form is a hard
reset — **destructive; requires explicit user confirmation each time** (per
CLAUDE.md "Commits" rule). Authorisation is per-action, not per-session:

```bash
git fetch
git reset --hard dev   # LAST RESORT — confirm with the user first
```

### Case B — wrong base, **commits exist** on the branch

Preserve the branch's own commits onto `dev`. The non-rewriting default is to
create a fresh branch from `dev` and re-apply (merge or re-commit) the work.
Where that is impractical, the last-resort rewrite is a scoped rebase —
**confirm with the user first**, as it rewrites the branch's history:

```bash
git rebase --onto dev <old-base> HEAD   # LAST RESORT — confirm first
```

`<old-base>` is what `git merge-base HEAD dev` *currently* reports (i.e. the
wrong base). Resolve conflicts as they appear; never use `--no-edit` with
rebase (not a valid flag) and never `-i` (interactive, unsupported in this
harness). This operates on the *work-item* branch only — never rebase a
protected branch (the guard blocks it).

### Case C — about to merge a wrong-based branch into `dev`

Do **not** paper over it by merging `dev` *into* the work branch first.
That pulls release commits along with `dev` into the eventual back-merge.
Run Case B remediation first, then merge.

## Merge preflight

Branch state drifts between commands, especially across worktrees. Never trust
an earlier `git checkout`.

1. `git symbolic-ref --short HEAD` → confirm output matches the intended target.
2. State explicitly: "Merging `<source>` into `<target>`".
3. If `<target>` is `main`, stop — the only path onto `main` is the
   `grm-project-release` skill.
4. Use the atomic form: `git switch <target> && git merge --no-ff <source>`.

## Worktree isolation (spawned sessions & subagents)

A spawned work-item session or subagent already runs in its own isolated
worktree. It must stay there. Root the work branch *in place*, from the ref:

```bash
git switch -c <branch> version/<X.Y>   # or `dev` — name the REF
```

Branching from the ref name (not ambient HEAD) roots you on the current staging
tip wherever the harness left HEAD, and is safe even though the staging branch
is checked out in the integration worktree — you create a new branch *at* that
commit, you do not check out the staging branch itself.

Never `git worktree add`, never `cd` to a canonical/other repo path, never
`git switch` an existing worktree to another branch, and never edit or
git-operate on the integration worktree or a sibling worktree.

### Guard step — refuse cross-worktree git ops (run before any branch op)

The v1.6 vet caught a spawned agent whose `git switch -c` landed in the
**integration master's** worktree instead of its own, silently switching the
master off `version/{X.Y}`. Before any `git switch -c`, `git branch`, or
`git switch`, run this guard:

1. **Confirm your cwd is your own worktree.** `pwd` — it must be the path
   the harness spawned you in (contains `/.claude/worktrees/<your-id>/`).
2. **Never redirect git at another path.** Do not pass `git -C <path>`,
   `--git-dir`, or `--work-tree`, and do not `cd`/`pushd` into another
   worktree first. Every git command must act on your own cwd.
3. **Refuse if the target carries the integration marker.** If the worktree a
   command would act on contains `.claude/integration-allow.local` and that
   worktree is **not your own**, STOP — that is the integration master's
   worktree. Branching/switching there is the exact hijack the guard hooks
   block. Branch in place in your own worktree instead.
4. **Branch in place from the staging ref** (`git switch -c <branch>
   version/<X.Y>`); never `git switch` to an existing branch that may be
   checked out in another worktree (git will refuse, or worse relocate it).

These rules are enforced fail-closed by `protected-branch-guard.sh` and
`worktree-guard.sh`: an unmarked actor that targets another (especially the
marked integration) worktree is refused with `exit 2`. The guard step here is
the human-readable counterpart — follow it so you never trip the hook.

## Port claim (before building / running the app in this worktree)

When parallel worktree agents each **build and run** the project, they collide
on a default port (3000/8080): the second launch fails, or worse, an agent's
traffic silently hits a sibling's running instance — false passes / misleading
failures. Before launching anything, claim a unique, verified-free port with the
**`claim_port.py`** helper — a single deterministic call, not ad-hoc `lsof`
reasoning (scripting-unification #75):

```bash
# default os-assign strategy (kernel picks a free port); idempotent per worktree
export GRIMOIRE_APP_PORT=$(python3 .claude/skills/grm-worktree-preflight/claim_port.py --worktree-id "$(basename "$PWD")")
```

- The script **probes and reports** — it does not hold the port bound; your app
  binds it. It is **idempotent per worktree-id** (a repeat call returns the same
  port if still free) via the gitignored `.claude/cache/port-claims.json`.
- Strategies (config `worktree-ports.strategy`): **os-assign** (default,
  recommended), **random-probe** (within `range-start`–`range-end`), **index**
  (deterministic `range-start + index` — pass `--index` the worktree's dispatch
  index). Exit 1 = no free port → **abort the launch**, do not fall back to a
  default port.
- The launching skill (e.g. `grm-environment-manager`) reads the env var
  (`worktree-ports.env-var`, default `GRIMOIRE_APP_PORT`) instead of hardcoding a
  port. When the worktree is removed after merge, its cache rows are cleared with
  the worktree.

The integration master / write-capable Workflow tier claims the port **before**
the spawn and passes it to the agent (env var or `args`), so every dispatched
agent runs identical claim logic regardless of how it was prompted.

## Why this matters

If a work-branch → `dev` merge shows unrelated release-only files in the
diff, the branch was main-rooted. Catching it pre-merge costs a rebase;
catching it post-merge means rewriting `dev` history, which is much worse.

## Anti-patterns

* Trusting a previous `git checkout` from earlier in the session — branch
  state drifts across worktrees. Re-run the check before each commit / merge.
* Using `git reset --hard` without explicit user confirmation.
* Calling this "fixed" because `dev` was merged *into* the branch. That does
  not fix the root; it deepens the problem.
* `git worktree add` / `cd`-ing to a canonical or sibling path from a
  spawned session or subagent — stay in your own worktree and branch in place
  from the staging ref.
* `git -C <other-worktree>` / `--git-dir` / `--work-tree` redirecting a
  branch op into another worktree — this is the v1.6 hijack vector; the
  guard hooks refuse it for unmarked actors. Always operate on your own cwd.
