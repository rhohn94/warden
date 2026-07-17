---
name: grm-design-doc-placement
description: Scan docs/design/ (and docs/grimoire/design/, flat-forever) for placement per docs/design/README.md's Subtrees rule; --apply git-mvs and updates README index/breadcrumb links. Report-only by default. Use when auditing or fixing design-doc placement.
---

# design-doc-placement

Audit and (optionally) migrate design docs onto the correct home per
`docs/design/README.md`'s Subtrees rule: a topic with more than one associated
doc gets its own subdirectory with a README index; a single-doc topic stays
flat. Backed by `design_doc_placement.py` — stdlib-only, report-then-apply,
never destructive. Design context: `docs/design/README.md` (consumer tier) and
`docs/grimoire/design/README.md` (framework tier, different rule — see below).

## Scope boundary — read this first

Three skills touch `docs/design/` and none of them overlap:

- **This skill (`grm-design-doc-placement`)** — moves *existing* design docs
  between flat and subtree homes when the layout has drifted from convention.
  It never invents doc content and never touches non-design files.
- **`grm-design-doc-scaffold`** — creates a *brand-new*
  `docs/design/{feature}-design.md` with the house section layout. Once that
  doc (or a sibling) later needs promoting to a subtree, this skill is what
  detects and performs that move — scaffold never does placement itself.
- **`grm-structure-migrate`** — moves everything *else* (`vendor/`, `tests/`,
  top-level layout drift) per `docs/project-structure.md`. It explicitly
  excludes `docs/design/` internals; this skill is the design-doc-specific
  complement.

If you're creating a new doc, use scaffold. If you're relocating source,
config, or vendor trees, use structure-migrate. If an existing design doc (or
group of docs) sits in the wrong place, use this skill.

## The two tiers, two rules

- **`docs/design/`** (consumer-facing, your project's own features) —
  promotable: a topic gets its own subdirectory + README index once it
  accumulates more than one doc; a single-doc topic stays flat at
  `docs/design/{feature}-design.md`.
- **`docs/grimoire/design/`** (framework-internal specs) — **flat-forever**.
  However many docs accumulate, they stay flat; that tier's README groups them
  by hand-authored prose section headers instead of directory subtrees. This
  script never promotes anything there — a subdirectory found under this tier
  is always a finding, and it is left for a human to resolve (its README's
  categorized layout can't be safely auto-edited).

## Usage

### Detect mode (default — read-only)

```bash
python3 .claude/skills/grm-design-doc-placement/design_doc_placement.py --root .
```

Classifies the current layout into finding codes:

| Code | Tier | Meaning | `--apply` |
|---|---|---|---|
| `FLAT_SHOULD_BE_SUBTREE` | consumer | Two or more flat docs share a topic prefix (`auth-design.md` + `auth-flow-design.md`) | promotes to `{topic}/` + README |
| `SUBTREE_COULD_FLATTEN` | consumer | A subtree directory holds exactly one design doc | flattens to `{topic}-design.md` |
| `WRONG_TOPIC_SUBTREE` | consumer | A doc's filename topic matches a *different* existing subtree, not the one it's filed under | moves it to the matching subtree |
| `GRIMOIRE_SUBTREE_DISALLOWED` | framework | A subdirectory exists under `docs/grimoire/design/` at all | report-only forever |

Add `--json` for machine-readable output. Exit 0 = no findings, exit 1 =
findings present.

### --apply mode

```bash
python3 .claude/skills/grm-design-doc-placement/design_doc_placement.py --root . --apply
```

Performs, in order (later moves see the result of earlier ones in the same
run, so a doc relocated into a would-be-flattened subtree correctly cancels
that flatten):

1. `WRONG_TOPIC_SUBTREE` moves.
2. `FLAT_SHOULD_BE_SUBTREE` promotions (creates the subtree dir + a new
   `README.md` Contents index, rewrites each moved doc's breadcrumb from
   `README.md` to `../README.md`, adds a `### Subtrees` bullet to the parent
   README).
3. `SUBTREE_COULD_FLATTEN` flattens (re-checked at apply time — a subtree that
   gained a sibling doc from step 1 no longer qualifies and is skipped) —
   `git mv`s the doc up, rewrites its breadcrumb from `../README.md` to
   `README.md`, deletes the now-empty subtree + its README, removes the
   parent's `### Subtrees` bullet.
4. `GRIMOIRE_SUBTREE_DISALLOWED` is always left unresolved — printed as
   `SKIP (report-only)` — since that tier's README needs a human to re-slot
   the docs into its prose sections.

Every `git mv` is done in place (no archive step needed — nothing is deleted,
only relocated within the same repo history). **After `--apply`, always run**:

```bash
python3 .claude/skills/grm-doc-assurance/doc_assurance.py --write-design-index
```

to regenerate the generated `<!-- design-index:begin -->` table — this
script maintains the hand-authored `### Subtrees` / `## Contents` lists and
doc breadcrumbs, not that generated block.

### --self-test

```bash
python3 .claude/skills/grm-design-doc-placement/design_doc_placement.py --self-test
```

Runs against built-in, offline, synthetic fixture trees covering every finding
code, apply-time re-checking, and the cases that must produce **zero**
findings (a single-doc flat topic; a healthy multi-doc subtree whose docs
don't collide with another subtree's topic). Exit 0 = all pass.

## Anti-patterns

- Do not run `--apply` reflexively — read the report first; a `WRONG_TOPIC_SUBTREE`
  finding especially deserves a human glance (the topic-prefix heuristic is a
  filename match, not a content read).
- Never hand-promote/flatten a design doc without also fixing its breadcrumb
  and both README indexes — that's exactly what this skill exists to keep in
  sync; prefer running it over a manual `git mv`.
- Never treat `docs/grimoire/design/` findings as auto-fixable — see the
  flat-forever rule above.
