---
name: architecture-audit
description: Evaluate a managed project's architecture as deterministic fitness functions — read the declarative .claude/architecture-rules.json (layers, allowed dependency edges, forbidden imports, no-cycles) and report every violation (file:line — rule-id) over the project's import graph. The deterministic complement to coding-practices-audit's narrative architecture pass. Read-only report by default; an optional --gate escalates per the v1.26 code-quality dials. Degrades clean when no rules file is declared. Triggers on "audit the architecture", "check architecture fitness", "architecture-audit", "are the layer boundaries respected", "check dependency direction", "find architecture violations", "is anything importing across layers".
---

# architecture-audit

Deterministic architecture fitness functions for a managed project. Reads a
declarative ruleset and reports dependency-direction, layering, and
module-boundary violations over the project's import statements. This is the
**deterministic complement** to `coding-practices-audit`'s narrative
architecture pass — it mechanically checks what the narrative pass can only
reason about. Design: `docs/design/architecture-fitness-design.md`.

Read-only; never edits source.

## Step 1 — Load the ruleset

Read `.claude/architecture-rules.json`. **If it is absent, report
`architecture-audit: no rules declared` and exit clean** — never fail a project
that has not opted in. The file declares (see the design doc for the full
schema):

- `layers` — name → glob(s) of files belonging to each layer.
- `allowed-edges` — directed allow-list of layer→layer dependencies; any edge
  not listed is forbidden.
- `forbidden-imports` — explicit deny rules (`id`, optional `from` layer,
  `pattern`, `severity`).
- `forbid-cycles` — when true, any import cycle across layers/modules is a
  violation.

## Step 2 — Resolve layers and extract imports

For each source file, resolve its layer by matching the `layers` globs. Extract
its import/use statements with a language-appropriate scan:

| Language | Import scan |
|---|---|
| Python | `^\s*(from\s+\S+\s+import|import\s+\S+)` |
| JS/TS | `^\s*import .* from|require\(` |
| Rust | `^\s*use\s+\S+` |
| Go | import blocks / `^\s*import` |

Map each imported symbol/path back to a layer (by the same globs / module roots).

## Step 3 — Evaluate the fitness functions

1. **Disallowed edge** — for every cross-layer import, if `(from,to)` is not in
   `allowed-edges`, emit a violation.
2. **Forbidden import** — for each `forbidden-imports` rule, flag any matching
   import (respecting an optional `from` layer scope).
3. **Cycles** — if `forbid-cycles`, build the layer/module edge set from the
   actual imports and report any cycle.

Each finding: `file:line — rule-id — message` plus the offending import.

## Step 4 — Report

Emit a machine block + a human table:

```
architecture-audit — 2 violation(s): 1 disallowed-edge (error), 1 forbidden-import (warn)
  src/ui/Cart.tsx:4   no-sql-in-view (error)   presentation imports prisma
  src/services/x.ts:9 allowed-edge   (error)   application → presentation not allowed
```

## Step 5 — Gate (optional)

With `--gate`, escalate per the v1.26 `code-quality` `audit-gate` dial: `warn`
reports (default), `block` stops the merge. Severity comes from each rule's
`severity` (or `error` for a disallowed edge). Default is report-only.

## Notes

- Fitness functions are evaluated over **import statements**, not an AST/type
  graph — cheap, repeatable, language-agnostic. For deeper analysis, defer to a
  language-native tool and record it in the ruleset's notes.
- Cite the same rule ids as `architecture-guidelines.md`
  (`arch-dependency-direction`, `arch-public-surface`, …) so the narrative and
  deterministic passes share one vocabulary.
