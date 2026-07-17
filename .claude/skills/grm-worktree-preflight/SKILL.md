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

## Run `preflight.py`

```bash
python3 .claude/skills/grm-worktree-preflight/preflight.py
```

One call mechanizes the happy path so you read a PASS/FAIL report instead of
running several separate `git` commands by hand:

1. **root-check** — is `HEAD` rooted on the staging ref (`version/{X.Y}` if
   exactly one exists, else `dev`)? A bare `git merge-base HEAD dev ==
   rev-parse dev` only holds at the instant of forking, so a merge-base
   mismatch alone is *not* decisive — it defers to check 2 (an exact match
   with no release-only commits is a fresh/synced pass; a mismatch is only a
   real failure once release-only commits are also reachable, distinguishing
   genuine mis-rooting from ordinary staleness that Step 0.5 will sync away).
2. **release-only-grep** — no commit reachable from `<parent>..HEAD` matches
   `dist/|version.bump|changelog|release` (case-insensitive). This is the
   decisive signal: it stays correct no matter how far the parent has since
   advanced, which is why it's what actually catches the "worktree spawned at
   `main`'s tip, and `main` already contains `dev`" case a bare merge-base
   equality would otherwise false-pass (SKILL.md's own real-world motivating
   case, in `reference.md`).
3. **parent-sync** (Step 0.5) — `git rev-list --count HEAD..<parent>`; if
   behind, sync-merges the parent in (`git merge --no-ff`, forward-merge only,
   never rebase). A clean merge passes; a conflict aborts the merge, fails the
   check, and surfaces the conflict — never auto-resolved. Pass `--no-sync` to
   only measure staleness without merging. Run this at the start of every
   session, including resumed ones — a session paused for hours or days is
   exactly when the parent has moved furthest.
4. **self-heal** (optional, `--self-heal`, integration master session-start
   only) — the R4 #452 sweep: inventories every worktree, classifies branches
   via `agent_branch_namespace.is_agent_branch`, and reports (never mutates)
   which agent-created worktrees are already safe to reap. Never affects the
   exit code — informational maintenance, not a preflight gate.

`--parent <ref>` overrides ref resolution (needed if more than one
`version/*` branch exists — ambiguous, so preflight.py refuses to guess and
fails with that explicit case). Exit 0 = every mandatory check passed or
synced cleanly. Exit 1 = a mandatory check failed.

**Any FAIL** → see `reference.md` for remediation (pick the matching Case
A/B/C) and the guard step, before any `git switch -c` / `branch` / `merge`.

`preflight.py --self-test` runs the hermetic self-test suite (temp fixture
repos; touches nothing real).

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
git-operate on the integration worktree or a sibling worktree. Full rule (the
cross-worktree branch hijack rule) is canonical in
`docs/grimoire/integration-workflow.md` §Enforcement (guard hooks). Before any
`git switch -c` / `branch` / `switch`, run the **guard step** in
`reference.md` — it catches the v1.6 hijack vector where a spawned agent's
branch op silently landed in the integration master's worktree instead of its
own.

## Why this matters

If a work-branch → `dev` merge shows unrelated release-only files in the
diff, the branch was main-rooted. Catching it pre-merge costs a rebase;
catching it post-merge means rewriting `dev` history, which is much worse.

## Anti-patterns

* Trusting a previous `git checkout` from earlier in the session — branch
  state drifts across worktrees. Re-run `preflight.py` before each commit / merge.
* Using `git reset --hard` without explicit user confirmation.
* Calling this "fixed" because `dev` was merged *into* the branch. That does
  not fix the root; it deepens the problem.
* `git worktree add` / `cd`-ing to a canonical or sibling path from a
  spawned session or subagent — stay in your own worktree and branch in place
  from the staging ref.
* `git -C <other-worktree>` / `--git-dir` / `--work-tree` redirecting a
  branch op into another worktree — this is the v1.6 hijack vector; the
  guard hooks refuse it for unmarked actors. Always operate on your own cwd.

## Reference (load on demand)

- Remediation `Case A / B / C` and the cross-worktree `Guard step` — see
  `reference.md`, loaded only once `preflight.py` (or a manual check) fails.
- `Self-healing sweep (integration master, session start only) (#452)` — see
  `reference.md`.
- `Port claim (before building / running the app in this worktree)` — see
  `reference.md`.
