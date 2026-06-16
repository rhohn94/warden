---
name: release-phase-merge
description: Merge completed subagent branches into the version/{X.Y} staging branch, run tests after each merge, tick §5 ledger rows, and — when all phases are done — merge version/{X.Y} into dev and clean up. Use when the user says "merge agent X", "merge branch foo", "phase N is done, merge it", "integrate completed work", "all agents in phase N are done", or "final merge into dev". Always merges into version/{X.Y}, never directly into dev (until the final step).
---

# Release phase merge

Merges completed agent branches into `version/{X.Y}` in the order
dictated by §3's conflict map, runs the test suite after each, ticks the §5
ledger, and handles the final `version/{X.Y}` → `dev` merge when all
phases are complete.

---

## Before every merge run

> **Preferred interface — `merge_preflight` (grimoire-release MCP, v3.27).** When
> `mcp.enabled` and the server is registered (root `.mcp.json`), run
> **`merge_preflight`** with the staging ref (and optionally the candidate
> branches; it defaults to the `merge_queue` order) for a structured verdict
> `{head_ok, branches:[{branch,exists,ahead,ok}], blocked:[…]}` — the
> HEAD==staging check plus per-branch exists + commits-ahead assertions, computed
> deterministically. It is **read-only — it never merges**; act on the verdict.
> A `head_ok:false` is the HEAD-drift signal (do not merge — investigate per the
> stranded-branch recovery below). **CLI fallback** (no MCP / disabled): `python3
> .claude/skills/release-agent-tracker/release_plan.py merge-preflight --staging
> version/{X.Y}`. The numbered steps below are the fallback procedure. Design:
> `docs/design/grimoire-release-server-design.md`.

1. **HEAD-verification gate (#35).** Assert HEAD is exactly the staging branch
   before *every* merge:
   ```bash
   test "$(git symbolic-ref --short HEAD)" = "version/{X.Y}" \
     || echo "HEAD DRIFT — investigate before merging"
   ```
   If HEAD is **not** `version/{X.Y}`, do not blindly switch and proceed —
   confirm *why* it drifted first (a HEAD parked on a work-item branch can mean
   work was stranded there). Once you have confirmed nothing is stranded:
   ```bash
   git switch version/{X.Y}
   ```
   The `protected-branch-guard.sh` hook also fails closed if the master tries to
   commit/merge while HEAD is off a staging branch.

2. **Run `release-agent-tracker`** to confirm which branches are
   ☑ Implemented ☐ Merged and their dependency order. Do not guess.

---

## Per-branch merge procedure

Repeat for each branch in the merge queue, **in the order from §3's conflict
map** (dependencies before dependents):

### 1. Review the diff

```bash
git diff version/{X.Y}...{branch}
```

Spot-check for:
- Scope creep (changes outside the files listed in §2.{N})
- Missing tests for new public functions
- `TODO` / `FIXME` left in non-test code
- Any edits to `docs/release-planning-v{X.Y}.md` — flag and reject if found
  in §§1–4

If the diff looks wrong, stop and ask the user before merging.

### 2. Confirm before merging (Supervised gate)

Summarise what you found in the diff review and ask: "Merge `{branch}` into
`version/{X.Y}`?"

Wait for explicit confirmation before proceeding.

### 3. Merge

```bash
git merge --no-ff {branch}
```

If there are conflicts: resolve them, then `git merge --continue`. Never use
`git merge --strategy-option=theirs` to paper over conflicts.

### 4. Run tests

```bash
{test-command}
```

Replace `{test-command}` with your project's test command (see CLAUDE.md).

If tests fail: do **not** tick ☑ Merged. Instead:

1. Identify whether the failure is in the merged branch or a pre-existing
   regression.
2. For a new failure: open a fix branch off `version/{X.Y}`, fix,
   re-merge, re-test.
3. For a pre-existing failure: note it and decide with the user whether to fix
   before continuing.

### 5. Tick §5 ledger

Edit the branch's row in `docs/release-planning-v{X.Y}.md §5`:
- Tick ☑ Merged
- Append the merge commit SHA: `☑ \`{short-sha}\``

This edit is in §5, so the `release-plan-guard` hook allows it.

Commit the ledger update immediately:
```bash
git add docs/release-planning-v{X.Y}.md
git commit -m "docs(release-v{X.Y}): tick §5 — {branch} merged ({short-sha})"
```

---

## Phase completion check

After the last branch in a phase is merged and tested:

1. Run `{build-command}` to confirm the integrated build is clean.
2. Report the phase complete to the user.
3. Ask: "Proceed to Phase {N+1} prompts?" — run `release-phase` if yes.

---

## Final merge — `version/{X.Y}` → `dev`

When all phases are ☑ Merged and the user confirms readiness:

### Pre-merge checklist

- [ ] `{test-command}` green on `version/{X.Y}`
- [ ] `{build-command}` clean
- [ ] All §5 rows ☑ Merged
- [ ] `version-history.md` entry written on `version/{X.Y}` (required
       by `project-release` later)

### Confirm before merging (Supervised gate)

Ask: "Merge `version/{X.Y}` into `dev`?" and wait for explicit confirmation.

### The merge

```bash
git switch dev
git merge --no-ff version/{X.Y}
{test-command}
```

If tests pass, ask before deleting the staging branch (destructive op):

```bash
# Clean up staging branch (ask user — destructive op)
git branch -d version/{X.Y}
```

If tests fail: debug on `version/{X.Y}` first, do not leave `dev` in a
broken state.

### Post-merge

- Update `docs/roadmap.md`: change the `v{X.Y}` entry from
  `(planning in flight)` to `(implementation complete — pending release)`.
- The `project-release` skill handles the final `dev` → `main` promotion and
  tagging. Do not tag from this skill.
- **Branch + worktree cleanup is a post-release step, not this skill's job.**
  The just-merged work-item worktrees are cleaned up after the release tags and
  pushes — see `project-release` §Post-release cleanup, governed by
  `docs/integration-workflow.md` §Dead-worktree cleanup. Removing dead
  worktrees here, mid-release, is premature.

---

## Push to origin — not here

**This skill pushes nothing.** After the `version/{X.Y}` → `dev` integration,
`dev` stays local. Pushing now happens **once, at `project-release`** time, in
a single human-gated prompt that pushes `dev` + `main` + the version tag
together — see `docs/integration-workflow.md` §Pushing to origin and the
`project-release` skill. Do not propose a `dev` push from this skill.

---

## Anti-patterns

- Merging without asking first (Supervised gate — confirm before every merge).
- Merging into `dev` directly (bypassing `version/{X.Y}`) — the staging
  branch exists precisely to test the integrated set before touching `dev`.
- Ticking ☑ Merged before the test suite passes — the column means "merged
  and tested clean."
- Merging all branches in one batch without running tests between them —
  failures become impossible to bisect.
- Deleting `version/{X.Y}` before the user confirms the `dev` merge is
  correct. Require explicit confirmation each time (per CLAUDE.md
  "Destructive operations" rule — authorisation is per-action).
- Running `project-release` from this skill. Tag only after the user reviews
  `dev` and explicitly asks to release.
