---
name: grm-end-session
description: Orchestrate the end-of-session cleanup: merge remaining ready branches, run grm-project-release to promote and tag, push, clean up dead worktrees and stale branches, and confirm dev/main match origin. Use when the user says "end the session", "wrap up", "get ready for a fresh session", or "merge push release and clean up".
---

# End session — get ready for a fresh start

Drives the complete merge → release → cleanup → handoff sequence. This skill
**delegates to existing skills** — it never reimplements their logic. Read each
delegated skill's SKILL.md for the mechanics; this skill tells you when to call
each one and in what order.

> **Scope note:** `copilot/` does not carry release-orchestration skills
> (`grm-release-phase-merge`, `grm-project-release`, etc.) and does not use this
> skill. It is `claude-code`-only.

---

## Step 1 — Assess state

Before acting, build a full picture of what is in flight.

1. Check the current branch and worktree: `git symbolic-ref --short HEAD` and
   `git worktree list`.
2. Identify the active `version/{X.Y}` staging branch (if any):
   `git branch | grep 'version/'`.
3. Read the §5 ledger in `docs/release-planning/release-planning-v{X.Y}.md` (consult
   `grm-release-agent-tracker` for the reconciled state table). Determine which
   work-item branches are Implemented but not yet Merged.
4. Check for drift: `git fetch origin && git log --oneline origin/dev..dev` and
   `git log --oneline origin/main..main`.

**Stop conditions — surface to the user before proceeding:**
- A merge conflict that requires human judgement.
- A test / gate failure with unclear cause.
- Ambiguous merge order (two branches that may conflict with each other and
  §3's conflict map is absent or unclear).
- No agreed release plan but unmerged work on `dev` that is not self-evidently
  clean — confirm intent before touching branches.

If state is unambiguous, proceed through Steps 2–5 autonomously under Noir.

---

## Step 2 — Merge everything

Delegate to **`grm-release-phase-merge`** for all in-flight merge work.

- Merge remaining ready work-item branches into `version/{X.Y}` in §3 conflict-map order.
- Tick the §5 ledger after each merge (the MCP `tick_rows` tool or the CLI fallback).
- Run the final `version/{X.Y}` → `dev` integration merge per `grm-release-phase-merge`'s
  final-merge checklist, including the before-promotion divergence gate.

If there is no active release but there is unmerged `dev` work (e.g. a
hotfix branch), ensure it is merged to `dev` cleanly before proceeding.

---

## Step 3 — Release (if a version is staged)

If `dev` carries an unreleased version (a `version-history.md` entry exists for
`{X.Y}` and the tag `v{X.Y}` has not yet been created), delegate to
**`grm-project-release`**:

1. Verify `version-history.md` and feature-manifest entries are present on `dev`.
2. Run `grm-project-release` — it handles the `dev` → `main` merge, version bump,
   tag, build of distributables and `release.json`, signing, and the GitHub Release.
3. Push: `git push origin dev main --follow-tags`. This is the **single** push
   moment for the release. Under `autonomous-push.enabled` (Noir) it is prompt-free;
   otherwise propose and wait for explicit user confirmation.

If nothing is staged for release, skip to Step 4 after ensuring `dev` is pushed
per the project's push policy.

---

## Step 4 — Clean up

Post-release cleanup per `grm-project-release` §Post-release cleanup and
`docs/grimoire/integration-workflow.md` §Dead-worktree cleanup. Only the
marker-blessed integration master runs this.

For each work-item branch/worktree of the just-shipped release:

1. Verify the branch is merged (an ancestor of the release tip) **and** the
   worktree is clean. Skip any branch that fails either check.
2. Preserve or explicitly report any uncommitted work — never discard silently.
3. `git worktree remove <path>` (refuses on a dirty tree — the safety net).
   `--force` only for disposable untracked artifacts and only with an explicit
   logged note of what is discarded.
4. `git worktree prune`.
5. `git branch -d` the merged feature branches and stale `worktree-*` placeholder
   branches (`-D` only with explicit user confirmation).

Report the tally: worktrees removed, branches deleted, work preserved/skipped.

---

## Step 5 — Handoff (ready for a fresh session)

Confirm the session is genuinely clean before declaring done.

1. **Clean tree check:** `git status` — must be clean on `dev` (or `main` if
   still there post-release). `git worktree list` — only the canonical repo
   and the integration-master worktree (if any) should remain.
2. **Origin parity:** `git log --oneline origin/dev..dev` and
   `git log --oneline origin/main..main` must both be empty.
3. **Structural health:** run `grm-doc-assurance` with the structural subset:
   ```bash
   python3 .claude/skills/grm-doc-assurance/doc_assurance.py \
     flavor-parity links docs-map description-cap --strict
   ```
   A new finding here is a blocker — resolve it before declaring done.
4. **Backlog memory:** update the project's persistent backlog/state memory
   (`.claude/projects/.../memory/MEMORY.md` or equivalent) with what shipped,
   any open follow-ups, and what is next. This is the cold-start context for the
   next session.
5. **Emit the end-state summary** — a short paragraph covering: version(s)
   shipped, open follow-ups (plain text, not chips), and the next-up item
   for the next session to pick up cold.

---

## Anti-patterns

- **Pushing without the integration-master marker.** `push-guard.sh` blocks
  this; do not attempt to work around it.
- **Guessing at ambiguous merge conflicts.** Stop and surface to the user; a
  wrong resolution is harder to recover from than a pause.
- **Force-removing a dirty worktree.** Uncommitted work is permanently lost.
  Always preserve or explicitly report before removing.
- **Skipping Step 1.** Acting on stale branch state leads to double-merges,
  missed branches, or releasing the wrong version.
- **Relabeling or closing tracker issues without authorization.** End-session
  cleanup touches git and docs only; issue-tracker writes require explicit user
  direction or the `grm-triager` role.
