---
name: grm-component-catalog-export
description: Scan a project's reusable components and emit a machine- and human-readable catalog report (each component's id, summary, profile tags, provides/requires, compatibility signals, stability). For downstream Grimoire consumers to discover what's available and author quick-start templates. Read-only. Use when exporting the component catalog or listing available components.
---

# Component-catalog export

Produces a catalog of the project's reusable components — the input a
`grm-quick-start-template` author consumes, and a discovery aid for anyone bootstrapping
a new project. **Read-only** (writes only the report artifact). Conventions and the
long-term direction: `docs/design/quick-start-templates-design.md`.

This export is a deliberate **stopgap** — a flat on-demand scan. The scalable
versioned successor is the `grm-component-registry` skill (`.claude/component-registry.json`,
designed in `docs/design/component-catalog-architecture-design.md`). When that
registry is present this export becomes a **view** over it; absent, it falls back
to the live scan below. Either way the report is fully back-compatible.

## Step 0 — Source from the registry when present (back-compat view)

> **Preferred interface — the `component_registry.py` script (v3.28).** The
> registry build is a deterministic stdlib engine; this export is a *view* over
> it, so when it can be the source, **call the script rather than re-deriving the
> scan in prose:**
>
> ```bash
> python3 .claude/skills/grm-component-registry/component_registry.py dry-run --stdout
> ```
>
> `dry-run` computes the registry **without writing** (this export is read-only —
> it never mutates `.claude/component-registry.json`; the `grm-component-registry`
> skill owns that file). `--stdout` adds the full `registry` object to the JSON
> summary. Project each entry in its `components` map and the `uncataloged` list
> into the report forms of Step 3 (skip the manual Steps 1–2). The engine owns
> discovery, hashing, and taxonomy validation identically to a real build, so the
> view stays in lock-step with the registry by construction. The Step text below
> is the conceptual model. Design: `docs/design/scripting-unification-design.md` §5.

If `.claude/component-registry.json` already exists on disk, you may instead
**render the report directly from it** (read its `components` map and
`uncataloged` list and project each component's `id`, `summary`, `profiles`,
`provides`, `requires`, `compat`, `stability`, `source` into Step 3, skipping
Steps 1–2). The registry is the source of truth; this view does not rebuild or
mutate it.

If no registry exists or you must scan a tree the registry does not cover, run
the live scan (Steps 1–3) exactly as before.

## Step 1 — Resolve scan paths

Defaults: `components/`, `lib/`. Plus any paths in `.claude/grimoire-config.json`
→ `component-catalog.paths` (array). Skip paths that don't exist (report which).

## Step 2 — Discover components

Find every component declaration:
- a `component.json` at a component root, **or**
- YAML front-matter (with a `component:` block) in a single-file component.

Parse the metadata fields (see design §2.1): `id`, `summary`, `profiles`,
`provides`, `requires`, `compat`, `stability`, `source`.

A reusable unit **without** metadata is listed under **Uncataloged** (path +
"missing component.json") — never silently dropped.

## Step 3 — Emit the report (two forms)

**Machine-readable JSON** (default to `.claude/cache/component-catalog.json`; also
print to stdout on request):

```json
{ "generated-from": ["components/","lib/"],
  "components": [ { "id": "...", "summary": "...", "profiles": [...],
                    "provides": [...], "requires": [...], "compat": {...},
                    "stability": "...", "source": "..." } ],
  "uncataloged": [ "path/without/metadata" ] }
```

**Human-readable Markdown** table: `id | summary | profiles | provides | requires
| compat | stability`, followed by an Uncataloged list and a coverage line
(N cataloged, M uncataloged, paths scanned/skipped).

## Invocation

```
component-catalog-export            # write JSON to cache + print Markdown table
component-catalog-export --stdout   # also print the JSON to stdout
```

## Safety / scope
- Read-only except the report artifact under `.claude/cache/`. No git commits.
- Does not modify or validate component code — it reports declared metadata only.

## Anti-patterns
- Inventing metadata for components that lack `component.json` — list them as
  Uncataloged instead.
- Silently skipping a configured path that's missing — report it.
- Treating the export as the catalog system — it's a stopgap / view; the
  versioned source of truth is `grm-component-registry` (build it with that skill).
- Rebuilding or mutating `.claude/component-registry.json` from this export — it
  is read-only here; the `grm-component-registry` skill owns the registry.
