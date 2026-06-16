---
name: workflow-snapshot
description: Re-capture the live workflow skills and hooks back into the workflow-bootstrap golden baseline. Use deliberately when you have improved a workflow skill or hook and want it to become the new restore baseline for this project. This is a manual, on-demand sync — there is no obligation to keep golden in lock-step with live skills. Triggers on "snapshot the skills", "update the golden copies", "re-baseline the workflow", "sync skills to bootstrap".
---

# Workflow-snapshot

The reverse of `workflow-bootstrap`. It copies the project's **live**
workflow skills and hooks into `workflow-bootstrap/golden/` so a future
restore reproduces the current state.

This exists so projects are **not** burdened with perpetual sync. You
re-baseline only when you choose to — after a deliberate improvement —
not on every edit.

---

## When to use this skill

- You improved a workflow skill/hook and want it to survive a future
  `workflow-bootstrap` restore.
- You are preparing this scaffolding to be copied into other projects and
  want `golden/` to reflect the latest generic version.

Do **not** use it for routine edits you might revert, or to snapshot a
copy that still has project-specific values baked in (see the warning
below).

---

## Step 1 — Genericise check (critical)

`golden/` must stay **project-agnostic**. Before snapshotting, scan each
live file for project-specific values that the interview is supposed to
fill (concrete test/build/release commands, real branch names other than
`dev`/`main`, real doc paths, a real version file).

For each such value, **re-insert the placeholder token** (per
`manifest.md`) in the snapshot — do not snapshot the concrete value.
If a file is irreversibly project-specific, report it and skip it rather
than poisoning the baseline.

The goal: a fresh project restoring from `golden/` gets clean
placeholders, not the previous project's commands.

---

## Step 2 — Diff preview

For every entry in `manifest.md` "Restorable" tables, `diff` the live
file against its `golden/` counterpart. Present a summary:

| File | Change |
|---|---|
| `skills/<name>/SKILL.md` | structural edit / placeholder re-inserted / unchanged |
| … | … |

List exactly what will be overwritten in `golden/`. Get explicit user
confirmation before writing.

---

## Step 3 — Capture

For each confirmed file, copy the (genericised) live content over its
`golden/` counterpart:

- `.claude/skills/<name>/SKILL.md` → `golden/skills/<name>/SKILL.md`
- `.claude/hooks/<file>`           → `golden/hooks/<file>`
- `.claude/settings.json`          → `golden/settings.json`

Never snapshot `workflow-bootstrap` or `workflow-snapshot` themselves
into `golden/` — the meta-skills are not self-restoring.

---

## Step 4 — Reconcile the manifest

If skills were added or removed, update `manifest.md`:
- New skill → add a row to "Restorable skills" + add its golden copy.
- Removed skill → remove the row and delete its `golden/` copy.
- New project-config placeholder → add it to the placeholder table.

---

## Step 5 — Report

- Files re-baselined (by path).
- Placeholders re-inserted (concrete value → token), so the user can
  confirm nothing project-specific leaked into `golden/`.
- Manifest changes.
- Skipped files and why.

No git operations. The user reviews and commits.

---

## Anti-patterns

- Snapshotting concrete project values into `golden/` — always re-insert
  placeholders first; a poisoned baseline misconfigures the next project.
- Auto-syncing on every edit — this is deliberate and on-demand.
- Snapshotting without the diff-preview confirmation.
- Snapshotting the meta-skills into their own golden tree.
