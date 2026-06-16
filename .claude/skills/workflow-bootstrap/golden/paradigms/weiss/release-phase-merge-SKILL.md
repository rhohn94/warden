---
name: release-phase-merge
description: Merge completed subagent branches into version/{X.Y}, one at a time with explicit per-branch user confirmation. Use when the user says "merge agent X", "merge branch foo", or "integrate completed work". Always merges into version/{X.Y}, never directly into dev (until the final step, which also requires user confirmation).
---

# Release phase merge (Weiss)

Merges completed agent branches into `version/{X.Y}` one at a time, with
explicit user confirmation before each merge. The user controls the pace and
sequencing; the master surfaces the diff and waits.

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
   Must be `version/{X.Y}`. If not, do not blindly switch — surface the drift to
   the user first (a HEAD parked on a work-item branch can mean work was
   stranded there), then switch and confirm. The `protected-branch-guard.sh`
   hook also fails closed if the master tries to commit/merge while HEAD is off
   a staging branch.

2. **Run `release-agent-tracker`** to show the user which branches are
   ☑ Implemented ☐ Merged. Present the list and ask which branch to merge
   first — do not pick an order independently when multiple are ready.

---

## Per-branch merge procedure

### 1. Review the diff

```bash
git diff version/{X.Y}...{branch}
```

Summarise for the user:
- Files changed and what they do.
- Scope: within or outside §2.{N}?
- Any `TODO` / `FIXME` or missing tests.
- Any edits to `docs/release-planning-v{X.Y}.md` §§1–4 — flag and ask the
  user whether to proceed or reject.

### 2. Confirm before merging (per-branch gate)

Present the summary and ask: "Merge `{branch}` into `version/{X.Y}`?"

Wait for explicit "yes." Do not merge until confirmed.

### 3. Merge

```bash
git merge --no-ff {branch}
```

If there are conflicts: describe them to the user; resolve with their
guidance, then `git merge --continue`.

### 4. Run tests

```bash
{test-command}
```

Report result to the user. If tests fail, describe the failure and ask how
to proceed — do not decide unilaterally.

### 5. Tick §5 ledger (only after user sees the test result)

After the user acknowledges the test pass, tick ☑ Merged and commit the
ledger update:

```bash
git add docs/release-planning-v{X.Y}.md
git commit -m "docs(release-v{X.Y}): tick §5 — {branch} merged ({short-sha})"
```

---

## Phase completion check

After the last branch in a phase is merged:

1. Run `{build-command}` and report to the user.
2. Ask: "All items in this phase are merged. Proceed to the next phase, or
   would you like to review anything first?"

---

## Final merge — `version/{X.Y}` → `dev`

Present the pre-merge checklist to the user:

- [ ] `{test-command}` green on `version/{X.Y}`
- [ ] `{build-command}` clean
- [ ] All §5 rows ☑ Merged
- [ ] `version-history.md` entry written

Ask: "All checks pass. Merge `version/{X.Y}` into `dev`?" Wait for explicit
confirmation before each command:

```bash
git switch dev
git merge --no-ff version/{X.Y}
{test-command}
```

After tests pass, ask before deleting the staging branch:
"Delete `version/{X.Y}`?" — destructive op, requires explicit answer.

**Branch + worktree cleanup is a post-release step, not this skill's job.** The
just-merged work-item worktrees are cleaned up after the release tags and
pushes — see `project-release` §Post-release cleanup, governed by
`docs/integration-workflow.md` §Dead-worktree cleanup.

---

## Push to origin — not here

This skill pushes nothing. After the `version/{X.Y}` → `dev` integration, `dev`
stays local. Pushing happens **once, at `project-release`**, in a single
human-gated prompt that pushes `dev` + `main` + the version tag together (see
`docs/integration-workflow.md` §Pushing to origin). Do not propose a `dev` push
from this skill.

---

## Anti-patterns

- Deciding which branch to merge next without presenting the ready list to
  the user.
- Merging without per-branch "Merge?" confirmation.
- Ticking ☑ Merged without reporting the test result to the user.
- Making conflict-resolution decisions without user guidance.
- Pushing without explicit user instruction.
