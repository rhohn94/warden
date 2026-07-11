---
name: grm-recipe-migrate
description: Migrate an existing project onto Grimoire's standard justfile recipe vocabulary — inventory Makefile/npm-script/scripts/recipes.json entry points, map onto build/run/test/.../release, write a delegating justfile, rewire recipes.json to `just <recipe>`. Report-first / --apply, idempotent, never deletes an entry point. Use when adopting the justfile contract on a project with its own build system.
---

# recipe-migrate

Adopting Grimoire's standard justfile recipe contract
(`docs/design/justfile-standard-design.md`) on an existing project is today a
manual procedure (§8 there, and `grm-build-recipe/reference.md` §Adoption
path): diagnose with `grm-install-doctor`, hand-add each recipe, hand-edit
`.claude/recipes.json`. This skill automates the mechanical parts for a project
that arrived with its own bespoke build system — a `Makefile`, `package.json`
scripts, hand-rolled `scripts/*` executables, or raw (non-`just`) commands
already sitting in `.claude/recipes.json`. It mirrors what `grm-structure-migrate`
does for directory layout: **inventory → map → write/rewire → report leftovers**.
Report-only by default; `--apply` performs the mechanical writes. Design:
`docs/grimoire/design/recipe-migrate-design.md`.

> **Preferred interface — `recipe_migrate.py` (#323).** The algorithm below is
> a real, stdlib-only script: `python3
> .claude/skills/grm-recipe-migrate/recipe_migrate.py --root .` (add `--apply`
> to perform the remedies, `--json` for machine output). The sections below are
> the conceptual model it implements.

## When to run

- Onboarding an existing project (fleet standardization) that has a `Makefile`,
  `package.json` scripts, bespoke `scripts/*`, or raw commands in
  `.claude/recipes.json`, but no conformant root `justfile` yet.
- After `grm-install-doctor` reports MISSING/PARTIAL required recipes in its
  "Justfile contract (full recipe vocabulary)" section and the project already
  has a working, non-`just` way to build/run/test.
- To extend a partially-adopted justfile (some vocabulary recipes real, some
  still `# grimoire:placeholder` stubs) once new entry points appear.

## Detect mode (default — read-only)

1. **Inventory** existing entry points from four sources: `Makefile` targets
   (top-level `name:` lines, excluding `.PHONY`/`all`/`help`/`default`),
   `package.json` `scripts`, executables directly under `scripts/` (by
   extension or the executable bit), and any **raw** (non-`just`) command
   already marked `implemented: true` in `.claude/recipes.json`.
2. **Map** each entry point's name onto the standard vocabulary — `build run
   test seed migrate lint clean package deploy smoke release` (`stop` is a
   separate work item, #322 — always emitted as a placeholder stub, never a
   mapping target) — via disjoint keyword-alias sets (`build`↔`compile`,
   `run`↔`start`/`serve`/`dev`, `test`↔`check`, `deploy`↔`publish`, …). A
   `.claude/recipes.json` entry already named for its target wins over a
   same-named guess from another source (it's already authoritative).
3. Classify and report:

| Code | Meaning |
|---|---|
| `UNMAPPED_ENTRY_POINT` | an inventoried entry point's name matches no vocabulary alias — report only, left in place |
| `AMBIGUOUS_MAPPING` | ≥2 entry points map to the same target (e.g. Makefile has both `build` and `compile`) — report only, neither auto-picked |
| `MISSING_IMPLEMENTATION` | a vocabulary target has no candidate entry point and no existing real justfile recipe — stays a `# grimoire:placeholder` stub |

```
recipe-migrate — 3 finding(s): 1 ambiguous-mapping, 1 missing-implementation, 1 unmapped-entry-point
  build            AMBIGUOUS_MAPPING        2 candidates map to 'build': Makefile:build, Makefile:compile
  deploy           MISSING_IMPLEMENTATION   no entry point maps to 'deploy' (required)
  Makefile:docs    UNMAPPED_ENTRY_POINT     no standard-vocabulary match for 'docs'
```

Exit 0 = no findings. Exit 1 = findings present. (Mirrors `grm-structure-migrate`.)

## --apply mode (explicit, confirmed)

For every vocabulary target (plus the `stop` stub), in order:

1. **Already a real recipe** (no `# grimoire:placeholder` in its body) — never
   touched, whatever its source.
2. **A high-confidence mapping exists** (exactly one candidate, or a
   `recipes.json`-authoritative one) — write a recipe with the standard
   signature whose body **delegates** to the existing entry point (`just build`
   calls `make build`; the underlying `Makefile`/`package.json`/`scripts/*` file
   is never modified, reimplemented, or deleted). Fills in a pre-existing
   `# grimoire:placeholder` stub in place, or appends a new recipe block if the
   target is missing from the justfile entirely.
3. **No mapping** (ambiguous or no candidate) — leave a pre-existing placeholder
   alone, or append a fresh `# grimoire:placeholder` stub if the target is
   missing.
4. **Rewire `.claude/recipes.json`**: every target that resolved to a real
   justfile recipe gets `{"command": "just <recipe> ...", "implemented": true,
   "params": {...}}` (the `run` target routes under the historical `server`
   key, §2.1 of the justfile standard); every target still a placeholder gets
   `{"command": null, "implemented": false}` if no entry existed yet — an
   existing entry is never downgraded.

Idempotent: a second `--apply` makes no further changes to an already-migrated
justfile (already-real recipes are skipped; already-placeholder recipes with no
new mapping stay placeholders).

**Known simplification:** delegating recipes call the underlying command
as-is (`make build`, not `make build ENV={{env}}`) — the legacy build system
may not support the standard parameter names at all. Threading `env`/`port`/…
through to an arbitrary inherited command is left as a follow-up manual
refinement; this tool's job is making every target addressable under one name,
not perfecting each one's argument plumbing.

**Out of scope:** `sync-deps` / `vendor-check` — the two "universal
(delegates)" recipes that always route to the same two framework scripts
regardless of project (justfile-standard-design.md §2). There is never an
existing project entry point to migrate for them; wire them by hand or via
`grm-sync-from-upstream`.

## Anti-patterns

- **Never delete or rewrite an inherited entry point** — `Makefile` targets,
  `package.json` scripts, and `scripts/*` files are the implementation; the
  justfile recipe only calls them.
- **Never auto-resolve an `AMBIGUOUS_MAPPING`** — guessing which of two
  same-named candidates is authoritative risks wiring the wrong command into a
  required recipe.
- **Never run this against the Grimoire scaffolding repo's own root as a live
  migration** — like `grm-structure-migrate`, this tool is for downstream
  projects; `--self-test` uses in-memory fixtures only.
- **Never downgrade an already-`implemented: true` `.claude/recipes.json`
  entry** — extend-only, mirroring the sync-from-upstream convention.

## See also

- `docs/design/justfile-standard-design.md` — the contract this migrates
  toward (§2 vocabulary, §2.2 the `recipes.json` → `just` routing convention,
  §8 the manual adoption path this skill automates the mechanical half of).
- `docs/grimoire/design/recipe-migrate-design.md` — this skill's design doc.
- **`grm-structure-migrate`** — the parallel migration for directory layout;
  same report-first/`--apply`/idempotent shape.
- **`grm-install-doctor`** — diagnoses the justfile contract findings this
  skill resolves.
- **`grm-build-recipe`** — the dispatcher (`recipe.py`) that calls the
  justfile recipes this skill writes.
