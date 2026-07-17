---
name: grm-component-registry
description: Build or update the versioned component registry .claude/component-registry.json from the project's component.json / front-matter sources — versions each component, diffs against the prior registry, and validates tags against the component-taxonomy authority (unknown tags surfaced, never dropped). Idempotent. Use when building, updating, or diffing the component registry.
---

# Component registry

Builds and maintains `.claude/component-registry.json`, the **versioned**,
diffable catalog of the project's reusable components. The scalable successor to
the `grm-component-catalog-export` flat-scan stopgap (which becomes a *view* over
this registry once it exists). The design rationale (Pillar 1) and the
vocabulary authority (`component-taxonomy.md`) are framework-internal design
specs — see the upstream Grimoire repository for that rationale; a project may
supply its own `docs/grimoire/design/component-taxonomy.md` to opt into tag
validation, otherwise it is skipped (see Step 4).

Unlike the stopgap, this **persists** per-component metadata + a version, so a
rebuild *diffs* against the prior state rather than re-deriving blind.

> **Preferred interface — the `component_registry.py` script (v3.28).** The whole
> discover / version / validate-taxonomy / diff / write-idempotently loop is now a
> deterministic stdlib engine — don't re-derive it in prose. **Call the script and
> interpret its result:**
>
> ```bash
> python3 .claude/skills/grm-component-registry/component_registry.py build     # build/update + write
> python3 .claude/skills/grm-component-registry/component_registry.py dry-run    # compute + print, write nothing
> #   add --stdout to either to also emit the full registry object
> ```
>
> It prints a JSON summary — `diff` (added/changed/removed/unchanged ids),
> `unknown-tags` (taxonomy misses, surfaced not dropped), `uncataloged`, and
> `written` (false ⇒ byte-identical no-op). The script owns the mechanics
> (sha256 content hashing, sorted-key serialization, content-derived build id,
> atomic temp+replace write); **you interpret** the diff/unknown-tags and decide
> follow-up (add a taxonomy term, fix a component, commit the registry). The
> script is **file-write-only** (writes only `.claude/component-registry.json`) —
> **you still commit** the result. Verify the engine with `… component_registry.py
> --self-test`. The Steps below are the conceptual model the script implements (and
> the contract it must honour). Design rationale lives in the upstream Grimoire
> repository (framework-internal — not shipped).

## Step 1 — Resolve scan paths

Identical to `grm-component-catalog-export`: defaults `components/`, `lib/`, plus any
paths in `.claude/grimoire-config.json` → `component-catalog.paths` (array). Skip
paths that don't exist (record them — reported as `paths-skipped`).

## Step 2 — Discover components

Reuse the **same discovery the stopgap uses** — do not invent a second scanner:
- a `component.json` at a component root, **or**
- YAML front-matter (with a `component:` block) in a single-file component.

Parse the metadata fields (design §2.1): `id`, `summary`, `profiles`,
`provides`, `requires`, `compat`, `stability`, `source`. A reusable unit
**without** metadata is recorded under `uncataloged` (path + "missing
component.json") — never silently dropped.

## Step 3 — Version each component

Per the design open-question decision (prefer declared, else hash):

1. If the component's `component.json` declares a `version`, use it verbatim and
   record `"version-source": "declared"`.
2. Otherwise compute a **content hash** of the component's normalized metadata
   entry (the canonical-JSON of its fields below, key-sorted) and use
   `"sha256:<hex>"`, recording `"version-source": "content-hash"`.

Recording `version-source` per entry keeps diffs stable either way (a component
that later declares a version flips cleanly from hash to semver as a *change*).

## Step 4 — Validate tags against the taxonomy

Read the allowed term sets from `docs/grimoire/design/component-taxonomy.md` —
§2 for `profiles`, §3 for the shared `provides`/`requires` capability
vocabulary — **if present**. This doc is framework-internal and not shipped by
default; when absent, validation degrades to a no-op (every tag is accepted,
`unknown-tags` stays empty) rather than failing the build. A project that wants
tag validation supplies its own copy at that path. When present, for each
component check every tag against the matching set:

- A recognized tag is recorded normally.
- An **unknown tag is surfaced**, not silently accepted into a clean entry and
  **not silently dropped**: list it under the registry's top-level
  `unknown-tags` block as `{ "component": "<id>", "field": "profiles|provides|requires", "tag": "<value>" }`, and echo the same list in the build output. The component is still recorded (with the offending tag retained in its entry) so the maintainer can either add the term to the taxonomy or fix the component.

## Step 5 — Build the registry object

Assemble the schema (design Pillar 1). `generated-from` is the resolved scan-path
list; `last-seen` is the build id (use a stable build id — see idempotency note).

```json
{
  "registry-version": 1,
  "generated-from": ["components/", "lib/"],
  "components": {
    "auth-jwt": {
      "version": "v1.2.0",
      "version-source": "declared",
      "summary": "JWT auth middleware + token service.",
      "profiles": ["api", "service"],
      "provides": ["auth"],
      "requires": ["http-server"],
      "compat": { "language": ["python", "typescript"], "min-framework": "v1.20" },
      "stability": "stable",
      "source": "components/auth-jwt/",
      "last-seen": "build-2026-06-01"
    }
  },
  "uncataloged": ["lib/legacy-thing/"],
  "unknown-tags": []
}
```

## Step 6 — Diff against the prior registry

If `.claude/component-registry.json` already exists, load it and compare by
component `id` + `version`:

- **added** — id present now, absent before.
- **removed** — id absent now, present before.
- **changed** — id in both but `version` differs.
- **unchanged** — id in both, same `version`.

Report the four buckets (counts + ids). On a first build (no prior registry),
every component is `added`.

## Step 7 — Write idempotently

Serialize with **sorted keys** and a fixed indent so output is deterministic.
Re-running with **unchanged sources must produce a byte-identical file** — so
`last-seen` / build-id must be **derived from content, not wall-clock time**
(e.g. `build-<short content hash of the components object>`); a time-based id
would break byte-identity. If the freshly built object is byte-identical to the
on-disk registry, it is a **no-op** (report "unchanged: no write needed").

Write to `.claude/component-registry.json` (committed — it is the source of
truth and a recognized sync artifact in C2).

## Invocation

Script-first (the engine; preferred):

```
python3 .claude/skills/grm-component-registry/component_registry.py build     # build/update; print the diff summary
python3 .claude/skills/grm-component-registry/component_registry.py dry-run    # compute + print the diff and unknown-tags; write nothing
python3 .claude/skills/grm-component-registry/component_registry.py --self-test
```

Skill-level aliases (the script underneath either way):

```
component-registry            # → component_registry.py build
component-registry --dry-run  # → component_registry.py dry-run
```

## Safety / scope

- Writes only `.claude/component-registry.json`. No git commits. Does not modify
  component code — it records declared metadata only.
- The compatibility matrix and distribution-over-sync are **C2** — out of scope
  here (this skill emits the registry the matrix is later derived from).

## Anti-patterns

- Using a wall-clock timestamp as `last-seen` / build-id — breaks byte-identical
  idempotency. Derive the id from content.
- Re-deriving the whole catalog and overwriting blind instead of diffing against
  the prior registry — defeats the point of versioning.
- Silently accepting or dropping an unknown tag — always surface it in
  `unknown-tags` and keep the component recorded.
- Inventing metadata for a component that lacks `component.json` — record it
  under `uncataloged`.
- Writing a second discovery scanner — reuse the `grm-component-catalog-export`
  discovery (Step 2) so the two stay in lock-step.
