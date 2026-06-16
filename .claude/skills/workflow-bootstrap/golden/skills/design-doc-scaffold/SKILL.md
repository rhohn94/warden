---
name: design-doc-scaffold
description: Scaffold a new docs/design/{feature}-design.md with the house section layout. Use when a feature task lacks a design doc. Triggers on "new feature", "draft a design doc", "scaffold X-design", "design for X", "design doc".
---

# Design-doc scaffold

Features live under `docs/design/{feature}-design.md` with a consistent
section layout. This skill creates the file and wires it into the design index.

## Source of truth

When reading the house-layout section template or sibling design docs as
reference, prefer `.grimoire-source/` at the **repo root** if it is present —
it holds clean, unmodified copies of framework sources (`.grimoire-source/skills/
design-doc-scaffold/SKILL.md`, structural docs, etc.) decoupled from any in-
progress edits to the live tree.

If `.grimoire-source/` is absent (e.g. before bootstrap has run), fall back to
the live tree and emit a one-line warning:
`[warn] .grimoire-source/ not found — reading from live tree; run workflow-bootstrap to populate it.`

## Before you write

1. Confirm the feature name in kebab-case (e.g. `user-auth`, not `UserAuth`
   or `user_auth`).
2. Check the file does not already exist: `ls docs/design/{feature}-design.md`.
   If it does, this is an *enhancement* not a *new feature* — read the existing
   doc, don't overwrite it.
3. Confirm you are on a work branch off `dev` / `version/{X.Y}`, not on `dev`
   or `main` directly. Run the **`worktree-preflight`** skill first if you are
   unsure.
4. Skim 1–2 sibling docs in `docs/design/` so the new doc cross-links them
   where appropriate (the overview doc, any architectural doc, plus any feature
   doc the new work depends on).
5. Skim `docs/architecture-guidelines.md` and `docs/coding-standards.md` so the
   design aligns with the project's standing principles and standards.

## House layout

Create `docs/design/{feature}-design.md` with this structure. Keep it terse —
the doc is a working agreement, not a spec.

```markdown
# {Feature title}

> **Up:** [↑ Design index](README.md)

## Motivation
Why are we building this? What problem does it solve, and for whom?
What changes if we don't ship it?

## Scope
What this feature does and does not cover. List explicit non-goals so
follow-ups don't bleed in.

## Design
The approach. Data model, key types/modules/components, interaction with
neighbouring systems. Cross-link sibling design docs rather than restating
them.

## Acceptance
Checklist of testable behaviours the implementation must satisfy.
One bullet per behaviour; each should be verifiable from a test or a
screenshot.

## Open questions
Things to decide before or during implementation. Resolve and prune as the
doc evolves; don't leave stale items.

## Follow-ups
Out-of-scope items deferred to a later branch or release. Move landed work
into the relevant `release-planning-v{X.Y}.md` ledger.
```

## After writing

* Stage and commit the doc on your work branch:
  `docs(design): add {feature}-design.md`. Atomic commit — design doc only.
* If the feature is targeted at a specific release, add a line to that
  release's `docs/release-planning-v{X.Y}.md` ledger pointing at the doc.
  (The **`ledger-tick`** skill handles that update.)
* **Mandatory:** Update `docs/design/README.md` to index the new doc with a
  RELATIVE LINK — e.g. `- [{feature}-design.md]({feature}-design.md) — Description`.
  A bare filename mention is not sufficient; the entry must be a markdown
  relative link so it is navigable and enforcer-visible.

## Anti-patterns

* Do *not* create the doc on `dev` or `main`. Always on a work branch (your
  spawned worktree).
* Do *not* duplicate content from sibling design docs. Link, don't copy.
* Do *not* leave the **Open questions** section empty as a placeholder —
  delete it if there are none.
* **Do not use bare backtick paths** (`docs/design/foo.md`) to cross-reference
  other docs — always use a markdown relative link (`[foo design](foo.md)`).
