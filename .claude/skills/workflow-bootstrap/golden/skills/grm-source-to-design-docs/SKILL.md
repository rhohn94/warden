---
name: source-to-design-docs
description: Generate the docs/design/ file structure for an existing project by reading source code, READMEs, and any existing documentation. Produces populated design docs that follow the house layout. Use when onboarding an existing codebase into this workflow, or when design docs are missing for features that already exist. Triggers on "generate design docs from source", "bootstrap design docs", "document existing code", "onboard existing project", "create design docs for this codebase".
---

# Source-to-design-docs

Reads an existing project's source code and documentation to produce a
`docs/design/` folder populated with feature design docs. The goal is to
capture what the code *already does* into the shared design doc format so
agents can orient quickly and future features have a baseline to cross-link.

---

## Source of truth

When reading framework skill files or structural docs as input (e.g. to
understand how the workflow is organised), prefer reading from
`.grimoire-source/` at the **repo root** if it is present — it holds a clean,
unmodified copy of the framework source decoupled from any in-progress edits to
the live tree. The layout mirrors the normal relative paths
(`.grimoire-source/skills/<name>/SKILL.md`, etc.).

If `.grimoire-source/` is absent (e.g. before bootstrap has run), fall back to
the live tree and emit a one-line warning:
`[warn] .grimoire-source/ not found — reading from live tree; run workflow-bootstrap to populate it.`

---

## When to use this skill

- A project has code but no `docs/design/` folder (initial onboarding).
- A feature exists in code but lacks a design doc (partial onboarding).
- You are about to plan a release and want agents to have doc context.

---

## Step 1 — Survey the project

Read the following in order, building a mental map of what the project does:

1. **Top-level README** (or equivalent entry-point doc). Note: project
   purpose, target users, technology choices, major components.
2. **Existing docs** (`docs/`, `wiki/`, `ADRs/`, `rfcs/`, etc.). Inventory
   what already exists; avoid duplicating it.
3. **Directory structure**: `find . -type f | head -200` (or equivalent) to
   understand the module/package layout.
4. **Entrypoints**: the main binary, server, or library surface (e.g. `main`,
   `index`, `lib`, `cmd/`). These reveal the top-level features.
5. **Key source files**: for each major module/package identified above, skim
   the top-level file, its public API, and any inline doc-comments.

Do **not** read the entire codebase before writing. Read enough to identify
the major feature boundaries, then write and iterate.

---

## Step 2 — Identify design-doc candidates

From the survey, produce a list of features/components that warrant a design
doc. A good candidate:

- Is a named, cohesive subsystem with clear boundaries (e.g. "auth", "search",
  "billing", "data-pipeline").
- Has public API surface that other modules depend on.
- Would be non-obvious to a new contributor from code alone (i.e. the "why"
  isn't written down).

For each candidate, note:
- **Name** (kebab-case slug for the filename)
- **Key source files** (2–6 files that contain the core logic)
- **What it does** (one sentence)
- **Dependencies** on other candidates (for cross-linking)

Output this list to the user and ask:
- "Does this match your mental model of the major features?"
- "Are there features I missed, or should I split / merge any of these?"
- "Which are highest priority to document first?"

Do not write any docs until the user confirms the candidate list.

---

## Step 3 — Write the overview docs first

Before feature docs, create the two anchor docs that everything cross-links:

### `docs/design/README.md` — design index

```markdown
# Design Docs

Overview of the design documents for {project name}.

## Index

| Document | Area |
|---|---|
| [architecture-design.md](architecture-design.md) | System architecture |
| [{feature}-design.md]({feature}-design.md) | {one-line description} |
| … | … |

## Conventions

- Each doc follows the standard layout: Motivation, Scope, Design,
  Acceptance, Open questions, Follow-ups.
- Cross-link sibling docs rather than restating them.
- Keep docs terse — a working agreement, not a spec.
```

### `docs/design/architecture-design.md` — system overview

> Captures *this* project's actual architecture. Generic architectural
> principles live separately in `docs/architecture-guidelines.md` — link to it,
> don't restate it.

Write this doc using the house layout (see **`design-doc-scaffold`** skill).
Populate it from the survey:

- **Motivation**: what problem does the project solve?
- **Scope**: what is in-scope / out-of-scope for the project overall?
- **Design**: major subsystems, how they connect, key tech choices. Use a
  diagram if it helps (ASCII or Mermaid). Name each subsystem exactly as you
  named it in Step 2 so feature docs can cross-link by name.
- **Acceptance**: the project's own definition of "working" — e.g. test suite
  passing, smoke test, integration test.
- **Open questions**: architectural unknowns worth surfacing now.

---

## Step 4 — Write feature design docs

For each confirmed candidate (highest-priority first), create
`docs/design/{feature}-design.md` using the house layout. Each generated
feature doc MUST include an up-link breadcrumb immediately after the heading:

```markdown
> **Up:** [↑ Design index](README.md)
```

### Populating each section from source

**Motivation** — answer from:
- README purpose statement
- Inline comments explaining "why" at the top of the module
- Commit messages that introduced the feature (`git log --oneline --follow
  {file}`)
- What breaks in the system if this component doesn't exist

**Scope** — answer from:
- Public API surface (what does this component export / expose?)
- What calls into it vs. what it calls
- What it explicitly does NOT do (look for TODOs, "not implemented", error
  messages that refuse input)

**Design** — answer from:
- Data structures / types / schemas at the module's core
- Key algorithms or flows (read the main functions; don't copy code verbatim —
  describe the approach)
- Dependency graph to sibling components (cross-link their design docs)
- Any notable tech choices with a one-line rationale ("uses X because Y")

**Acceptance** — answer from:
- Existing test file names and test case names (list the behaviours they cover)
- Any existing integration or smoke test
- Manual steps documented in the README (if any)

**Open questions** — include any:
- TODOs / FIXMEs in the source that represent unresolved design questions
- Places where the code does something surprising without an explanation
- Known limitations worth surfacing for future work

**Follow-ups** — include:
- Explicitly deferred work (TODOs tagged "later", "v2", "future")
- Features that exist partially (search for `unimplemented!`, `NotImplemented`,
  `stub`, `placeholder`)

### Writing guidelines

- Do **not** copy-paste code into the doc. Describe the approach in prose.
- Do **not** repeat content from `architecture-design.md` — link to it.
- Do **not** reverse-engineer missing context. If a section can't be
  populated from what you can read, leave a note like:
  `[Needs input from original author — not evident from source]`
- Keep each doc to ~1–2 pages. If it wants to be longer, split the feature.
- **Relative links only.** All cross-references to other docs must use
  relative markdown links, not bare backtick paths. Use `[foo design](foo.md)`
  rather than `` `docs/design/foo.md` `` — bare paths are not navigable and
  fail the enforcer's link check.

---

## Step 5 — Commit the docs

Create the docs on a dedicated branch:

```bash
git switch -c docs/design-bootstrap dev
```

Commit in two atomic commits:

```bash
# 1. Index + architecture
git add docs/design/README.md docs/design/architecture-design.md
git commit -m "docs(design): bootstrap design index and architecture overview"

# 2. Feature docs
git add docs/design/
git commit -m "docs(design): generate feature design docs from source code"
```

---

## Step 6 — Report back

Report to the user:
1. Which docs were created (list by path)
2. Which sections have `[Needs input from original author]` placeholders
3. Any significant open questions or gaps you surfaced

Suggest running `release-planning` next if a release is being planned, since
the new docs will inform the work-items report.

---

## Anti-patterns

- Writing docs that only describe the code, not the *why*. If all you can say
  is "this function adds two numbers", the doc isn't helping. Infer intent
  from context; note when you can't.
- Creating a design doc for every file. Aim for feature/subsystem boundaries,
  not file boundaries.
- Blocking on incomplete information. Write what you can, mark gaps explicitly,
  and move on.
- Creating docs on `main` or `dev` directly — use a dedicated branch.
- Skipping Step 2 user confirmation. The candidate list is cheap to revise;
  wrong docs written at scale waste time.
