---
name: grm-component-backfill
description: One-time, bounded sweep that authors component.json for reusable components lacking metadata — report-first with a token estimate, then apply; idempotent; low-confidence components stay uncataloged with an explicit reason, never guessed. Ends by rebuilding the component registry and reporting the diff. Use when a project has reusable components but little/no component.json coverage.
---

# Component backfill

`grm-component-registry` and `grm-component-catalog-export` are consumers of
component metadata — they discover and diff `component.json` files, they
never author one. On an existing project this leaves a chicken-and-egg gap:
the registry stays empty until *something* writes the first metadata files.
This skill is that something — a **one-time, bounded** pass, not a standing
process. Design: `docs/grimoire/design/component-backfill-design.md` +
`docs/grimoire/design/component-catalog-architecture-design.md` (the registry
this backfills into).

> **Preferred interface — `component_backfill.py`.** The classification and
> file-write mechanics below are a real, stdlib-only, self-tested script:
> `python3 .claude/skills/grm-component-backfill/component_backfill.py report
> --root .` / `... apply --root .`. It reuses `grm-component-registry`'s own
> `Discovery`/`RegistryEngine` (sibling-skill import — never a second
> discovery scanner) so "what counts as uncataloged" and "what the registry
> build reports" stay in lock-step by construction. The steps below are the
> conceptual model it implements; read them to understand *why* a candidate
> landed where it did, not to re-derive the logic by hand.

## Why a new skill, not an extension of `grm-source-to-design-docs`

Considered extending `grm-source-to-design-docs` (it already reads source to
author project documentation) instead of adding a new skill. Rejected:
that skill authors free-form `docs/design/{feature}-design.md` prose under a
house layout (Motivation/Scope/Design/…) meant for humans and agents to
orient by; this skill authors `component.json` under the fixed
`component_registry.py` `META_FIELDS` schema (id, summary, profiles,
provides, requires, compat, stability, source) meant for a machine
(`component_registry.py build`) to parse. Different output schema, different
consumer, different regeneration semantics (source-to-design-docs' "skip
existing / --regenerate to force" policy has no equivalent here — a
component.json this skill wrote is immediately real metadata, not a draft to
refresh). Folding the two together would mean either bloating
source-to-design-docs past its current scope or teaching it a second,
unrelated output format. A small, lean, single-purpose skill matches this
repo's skill-budget doctrine better than widening an existing large one.

## Step 1 — Resolve scan paths (delegates to `component_registry.py`)

Same resolution `grm-component-registry` uses: `components/`, `lib/`, plus
any paths in `.claude/grimoire-config.json` → `component-catalog.paths`.
`component_backfill.py` calls `component_registry.resolve_scan_paths()`
directly — it never re-derives this list.

## Step 2 — Discover uncataloged candidates (delegates to `Discovery`)

A directory under a scan path with **no** existing metadata (no
`component.json` at its root, no front-matter `component:` block anywhere
inside it) is a candidate. This is exactly `component_registry.py`'s
`Discovery.discover()` second return value — the same set the registry
itself would report under `uncataloged`. A directory that already has
metadata is never revisited (this is what makes a re-run idempotent).

## Step 3 — Classify each candidate: CONFIDENT vs stays UNCATALOGED

Deterministic, no invented content:

| Signal | Outcome |
|---|---|
| Directory basename is a generic/grab-bag name (`misc`, `utils`, `helpers`, `common`, `shared`, `tmp`, `scratch`, `legacy`, …) | **UNCATALOGED** — reason cites the name. Wins over any other signal. |
| > 6 non-hidden, non-test top-level files | **UNCATALOGED** — reason cites the file count ("no single clear entry point"). |
| A `README.md` with a non-empty first prose line, **or** a single primary source file (`__init__.py`/`index.js`/`index.ts`/`main.py`/one lone code file) with a leading docstring/block comment | **CONFIDENT** — summary sourced verbatim from that line. |
| None of the above | **UNCATALOGED** — reason: "no README.md or leading module docstring found to source a summary from." |

A CONFIDENT candidate gets `id` (slugified directory basename), `summary`
(the sourced line), `stability: "experimental"` (backfilled metadata is
unverified until a human reviews it), and `source`. Nothing else —
`profiles`/`provides`/`requires`/`compat`/`version` are **never guessed**;
they are simply absent from the written `component.json` (the schema treats
absent optional fields as valid). An operator or a follow-up agent MAY read
further and hand-author richer tags for a specific component afterward; that
is explicitly out of this skill's one-time, bounded scope.

## Step 4 — Report first (read-only)

```
component_backfill.py report --root . [--stdout]
```

Prints every candidate's classification (`confident` or `uncataloged` +
reason), the confident/low-confidence counts, and a **token estimate** for
the apply step (chars read to source each confident candidate's summary,
plus a small fixed per-candidate read/write overhead, all `// 4` — the same
rough chars-per-token proxy `grm-token-measure/footprint.py` uses). Nothing
is written in this mode. Show this report to the user/operator before
applying — "one-time and bounded" means a deliberate, visible commitment,
not a silent background sweep.

## Step 5 — Apply (writes `component.json`, then rebuilds the registry)

```
component_backfill.py apply --root . [--stdout]
```

For every CONFIDENT candidate only: writes `<candidate>/component.json`
(sorted-key JSON, trailing newline — same serialization discipline as
`component_registry.py`). Then calls
`component_registry.RegistryEngine(root).build(write=True)` directly (no
second `build` invocation needed) and reports:

- `written` — ids just authored this run.
- `registry_diff` — `added`/`changed`/`removed`/`unchanged` from the fresh
  registry build; the ids in `written` should appear under `added` on a
  first run.
- `uncataloged` — the registry's own remaining-uncataloged list (the
  low-confidence candidates, untouched).
- `low_confidence` — this skill's own reason strings per still-uncataloged
  candidate (the registry file itself has no reason field; this is the
  skill's report, not a registry schema extension).

**Idempotent:** a directory that received a `component.json` this run has
metadata now, so the next `report`/`apply` no longer sees it as a candidate
at all (Step 2). A second `apply` with no source changes writes nothing new
and the registry diff's `added`/`changed` are empty.

## Commit

Stage the newly-written `component.json` files plus the rebuilt
`.claude/component-registry.json` in one commit, e.g.:

```
git add components/*/component.json lib/*/component.json .claude/component-registry.json
git commit -m "chore(components): backfill metadata for N reusable components"
```

## Anti-patterns

- **Inventing a summary, profile, provides, or requires tag** for a
  low-confidence candidate — leave it `uncataloged` with the reason string
  instead. Never guess to make the count look better.
- **Running this as a standing/repeated process.** It is a one-time backfill
  pass for existing debt; a work item that creates or reshapes a component
  going forward should author its own `component.json` at write time (a
  separate concern — see #459 in `docs/release-planning/release-planning-v3.97.md`).
- **Re-deriving discovery or registry-build logic** instead of importing
  `component_registry.py`'s `Discovery`/`RegistryEngine` — the two skills
  must never drift on "what counts as a component."
- **Applying without first showing the report** — the token estimate and
  candidate list are the point of the report-first design; skipping straight
  to `apply` defeats it.

## See also

- **`grm-component-registry`** — owns `.claude/component-registry.json`;
  this skill's `apply` step calls its engine directly rather than
  reimplementing the build.
- **`grm-component-catalog-export`** — the read-only report view over the
  registry (or a live scan); unaffected by this skill beyond having more
  `component.json` files to find on its next run.
- **`docs/grimoire/design/component-taxonomy.md`** — the controlled
  vocabulary a follow-up hand-authored `profiles`/`provides`/`requires`
  should draw from; this skill deliberately never writes those fields
  itself.
