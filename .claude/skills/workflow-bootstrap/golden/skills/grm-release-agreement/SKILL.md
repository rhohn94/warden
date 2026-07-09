---
name: release-agreement
description: Finalize and lock a release plan after the user approves the work-items report. Creates docs/release-planning-v{X.Y}.md with status: draft then transitions it to status: agreed (the guard hook requires this two-step lock), creates the version/{X.Y} staging branch off dev, and sets up the §5 ledger. Use when the user says "agree on the plan", "lock the scope", "scope is confirmed", "finalize vX.Y", or "write the release plan". Run after release-planning has produced an approved report.
---

# Release agreement

Transitions a work-items report (from the `release-planning` skill) into a
locked `docs/release-planning-v{X.Y}.md` and a staging branch
`version/{X.Y}` off `dev`. Nothing is implemented yet — this step just
freezes the scope so all subagents work from the same contract.

---

## Step 1 — Confirm scope with the user

Before writing anything, do a final verbal check:

- "Is the feature list final, or are there items to add / remove?"
- "Is the pass structure (which items run in parallel) approved?"
- "Are the out-of-scope items agreed?"

Do not proceed until the user explicitly confirms. This is the last cheap
revision point.

---

## Step 2 — Write the planning doc with `status: draft`

Create `docs/release-planning-v{X.Y}.md` following the structure below. Use
`status: draft` — **not** `agreed` — so the release-plan-guard hook does not
block the initial write.

```markdown
# Release Planning — v{X.Y}

> status: draft
> Companion to `version-design.md` and `version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v{X.Y}.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v{X.Y}` |
| **Previous** | v{X.Y-1} ({one-line theme of previous release}) |
| **Theme** | "{Flagship name}" — {one sentence on the release theme}. |

---

## 2. Major Features

{One subsection per work item from the approved report. Each subsection:
  - Item ID + name
  - Description
  - Acceptance criteria
  - Branch name
  - Design doc reference}

---

## 3. Parallel Implementation Strategy

{Pass structure: which items run in which pass, merge order, conflict map
(which branches touch overlapping files).}

---

## 4. Out of Scope for v{X.Y}

{All items explicitly deferred, with a pointer to which future version they
target where known.}

---

## 5. Status Ledger

{One table per pass, each row ☐ for all columns initially.}

| Branch | Design doc | Implemented | Reviewed | Merged into version/{X.Y} |
|---|---|---|---|---|
| `{work-branch}` (ITEM-1) | ☐ | ☐ | ☐ | ☐ |

### Follow-ups discovered during implementation

{Empty at start; populated by release-phase-merge as branches land.}
```

---

## Step 3 — Locate dev (do NOT switch to it)

```bash
git rev-parse --short dev
git worktree list | grep -E '\[dev\]' || echo "dev not checked out anywhere"
```

This repo runs many concurrent worktrees; `dev` is almost always checked out
in a dedicated integration worktree. **Never `git switch dev` from here** —
git refuses a branch checked out elsewhere, and forcing it is the
cross-worktree drift the worktree-guard exists to prevent. The staging branch
is created *from* `dev`'s tip without checking `dev` out.

---

## Step 4 — Create the staging branch from dev's tip

```bash
git branch version/{X.Y} dev      # ref-only; no checkout of dev
git switch version/{X.Y}          # new branch, free to check out here
```

`version/{X.Y}` is now rooted on the current `dev` tip and is the
integration target for all phase merges. Work-item worktrees branch off this
ref, not off `dev` directly.

**If the planning doc was committed on a different branch** (e.g. the
harness-spawned worktree branch), bring it over with **branch-and-merge** — the
git default — not by rewriting history:

```bash
git merge --no-ff <source-branch>    # marker-blessed master, on version/{X.Y}
```

Merge (not `cherry-pick`) is the default: `git cherry-pick` is a
history-rewriting command **blocked on protected branches** by
`protected-branch-guard.sh` (#84), and `version/{X.Y}` is protected. If the
source branch is impractical to merge (e.g. it carries unrelated commits), the
simplest non-rewriting alternative is to re-create the small planning doc
directly on `version/{X.Y}` and commit it (Step 5). See
`docs/integration-workflow.md` §Git-protocol governance.

---

## Step 5 — Commit the planning doc on the staging branch

```bash
git add docs/release-planning-v{X.Y}.md
git commit -m "docs(release-v{X.Y}): create release plan (status: draft)"
```

---

## Step 6 — Lock the plan

Edit `status: draft` → `status: agreed` in the planning doc. Commit immediately:

```bash
git commit -m "docs(release-v{X.Y}): mark release plan as agreed"
```

After this commit the `release-plan-guard` hook protects §§1–4 from accidental
edits. Only §5 (the ledger) remains writeable.

---

## Step 7 — Update roadmap.md

In `docs/roadmap.md`, update the `v{X.Y}` entry to link the new plan:

```
Plan: [`release-planning-v{X.Y}.md`](release-planning-v{X.Y}.md).
```

Commit: `docs(roadmap): link release-planning-v{X.Y}.md`

---

## After agreement

- Run `release-phase` to spawn Phase 1 work-item sessions (via `spawn_task`).
- Each session works in its own isolated worktree rooted on `version/{X.Y}`,
  **not** on `dev`. The spawned prompt briefs this explicitly.
- The `worktree-preflight` skill checks the merge base; a worktree branched off
  `version/{X.Y}` passes because `version/{X.Y}` itself is rooted on `dev`.

---

## To revise agreed scope

If scope must change after locking (unusual — avoid):

1. Get explicit user confirmation.
2. Edit `status: agreed` → `status: revising` in the planning doc. The guard
   hook now allows §§1–4 edits.
3. Make the scope change; update §3 conflict map and §5 ledger rows to match.
4. Edit `status: revising` → `status: agreed` and commit.

---

## Scope-trimming rule for `Grimoire-Requirement` items

Items sourced from origin-D (open issues tagged `Grimoire-Requirement`, per the
`release-planning` skill Step 3) carry the **never-silently-trimmed** guarantee,
aligned with the `[framework-required]` baseline contract:

- They **may** be scheduled across versions — deferring a tagged item to a
  later release is allowed.
- They **may never be silently dropped** during scope-trimming. If a scope pass
  removes a `Grimoire-Requirement` item, the removal and its justification must
  be made **user-visible** in the plan's §4 "Out of Scope" section. Silent
  omission is prohibited (`web-app-support-design.md` §6.2).

Apply this check in Step 1's final verbal confirmation and when writing §4 of
the planning doc: any `Grimoire-Requirement` item absent from §2 features AND
absent from §4 with explicit justification is a scope error.

---

## Anti-patterns

- Creating `version/{X.Y}` off `main` — always off `dev`.
- Committing with `status: agreed` in the same commit that writes §§1–4
  content — if the initial write fails mid-way, the guard triggers before the
  content is finalised. Draft first, agree second.
- Skipping the user confirmation in Step 1 — scope revisions after agreement
  are expensive and protected by the hook.
- Silently omitting a `Grimoire-Requirement` item from §2 without a §4 entry —
  the never-trim rule requires user-visible justification for any such removal.
