---
name: grm-architecture-audit
description: Evaluate a managed project's architecture and standard structure as deterministic fitness functions from .claude/architecture-rules.json (layers, allowed edges, forbidden imports, no-cycles, structure block) — report each violation (file:line — rule-id) over the import graph and directory layout. Read-only by default; optional --gate escalates. Use when auditing architecture fitness, layer boundaries, or standard-structure conformance.
---

# architecture-audit

Deterministic architecture fitness functions for a managed project. Reads a
declarative ruleset and reports dependency-direction, layering, and
module-boundary violations over the project's import statements. This is the
**deterministic complement** to `grm-coding-practices-audit`'s narrative
architecture pass — it mechanically checks what the narrative pass can only
reason about. Design: `docs/grimoire/design/architecture-fitness-design.md`.

Read-only; never edits source.

> **Preferred interface — `architecture_fitness.py` (#212).** The algorithm
> below is now a real, stdlib-only script: `python3
> .claude/skills/grm-architecture-audit/architecture_fitness.py --root .`
> (add `--gate` to escalate per the live `code-quality.audit-gate` dial, `--json`
> for machine output). It implements Steps 1–4 exactly — regex import
> extraction, layer/edge resolution, cycle detection, and `structure` block
> conformance — deterministically and without burning tokens re-deriving the
> import graph each run. `grm-code-health` imports its `build_import_graph()` /
> `module_coupling()` for its own module-coupling section, and **`grm-structure-migrate`**
> imports its `check_structure()` for the migrate-mode detection engine (#320) —
> one shared scan, not three implementations. The Steps below are the fallback procedure (and the
> conceptual model the script implements) for when the script can't run in the
> current environment.

## Step 1 — Load the ruleset

Read `.claude/architecture-rules.json`. **If it is absent, emit a visible WARN
pointing at adoption** (a per-family starter shipped by
`grm-quick-start-template` — `.claude/quick-start-templates/{service,web,gui,lib}/`
— or `.claude/architecture-rules.example.json`) **and exit clean** — never fail
a project that has not opted in; the absence is surfaced, not silent (#314). A
project may explicitly decline by committing a rules file with `"opt_out":
true` (+ `"opt_out-reason"`) — this is reported as an explicit opt-out, exits
clean, and runs no fitness checks. The file declares (see the design doc for
the full schema):

- `layers` — name → glob(s) of files belonging to each layer.
- `allowed-edges` — directed allow-list of layer→layer dependencies; any edge
  not listed is forbidden.
- `forbidden-imports` — explicit deny rules (`id`, optional `from` layer,
  `pattern`, `severity`).
- `forbid-cycles` — when true, any import cycle across layers/modules is a
  violation.
- `structure` — the standard project layout (full contract:
  `docs/project-structure.md`): `required` (top-level dirs that must exist),
  `aliases` (nonstandard dir name → its standard home), `gitignored` (dirs that
  must not be tracked by git). Absent → skip structure conformance (Step 3a).
- `opt_out` (+ optional `opt_out-reason`) — a project's explicit, tracked
  decision to decline architecture fitness enforcement. Surfaced distinctly
  from an absent rules file; runs no checks.

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

## Step 3a — Structure conformance (optional)

If the ruleset has a `structure` block, evaluate the standard project layout as
fitness functions over the top-level directory listing (and `git ls-files` for
the tracked check). If absent, skip — report nothing.

1. **`structure-required`** — for each name in `required` not present as a
   top-level directory, emit a violation (`error`): `missing required directory`.
2. **`structure-nonstandard`** — for each top-level directory whose name is a key
   in `aliases`, emit a violation (`warn`): `rename <dir>/ → <aliases[dir]>/`.
   (Covers `vendor/`→`lib/third-party/`, `test/`→`tests/`, …)
3. **`structure-tracked-output`** — for each name in `gitignored` that git is
   tracking (appears in `git ls-files`), emit a violation (`warn`):
   `<dir>/ is build output and must not be committed`.

Findings use the same `path — rule-id — message` shape. Remediation for
nonstandard / output findings is the **`grm-structure-migrate`** skill.

## Step 4 — Report

Emit a machine block + a human table:

```
architecture-audit — 3 violation(s): 1 disallowed-edge (error), 1 forbidden-import (warn), 1 nonstandard-dir (warn)
  src/ui/Cart.tsx:4   no-sql-in-view (error)         presentation imports prisma
  src/services/x.ts:9 allowed-edge   (error)         application → presentation not allowed
  vendor/             structure-nonstandard (warn)   rename vendor/ → lib/third-party/
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
  (`arch-dependency-direction`, `arch-public-surface`, `arch-standard-layout`, …)
  so the narrative and deterministic passes share one vocabulary.
- Structure conformance (Step 3a) checks *top-level names and placement* against
  `docs/project-structure.md`; in-`src/` layout is the job of
  `coding-standards/*.md`, not this audit.
