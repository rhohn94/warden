---
name: grm-docs-migrate
description: Detect and migrate old-style docs to the wiki hierarchy — detect mode flags flat-tier/orphan/absolute-link/prose-link/missing-breadcrumb findings; --apply auto-resolves the breadcrumb and absolute-link cases (the rest are detection-only, manual fix). Downstream-safe. Use when migrating docs or fixing breadcrumbs / absolute links.
---

# docs-migrate

> **Up:** [↑ Skills index](../repo-reference/SKILL.md)

Detect and migrate old-style docs to the Grimoire wiki hierarchy (WH-5,
v3.37). Backed by `docs_migrate.py` — archive-first, idempotent, loud-fallback
on unresolvable refs. Downstream-safe: uses `.claude/grimoire-config.json` for
root detection, never requires `claude-code/`.

## When to run

- After `grm-sync-from-upstream` detects old-style docs and offers migration.
- When `grm-install-doctor` reports `DOCS_LEGACY_STYLE` findings.
- On demand, to check or fix a project's docs tree before a release closeout.
- In CI, as part of a `--strict` gate.

## Usage

### Detect mode (default — read-only)

```bash
python3 .claude/skills/grm-docs-migrate/docs_migrate.py
```

Classifies every `docs/**/*.md` file (excluding exempt files and `README.md`
index pages) into one or more finding codes:

| Code | Meaning | `--apply` |
|---|---|---|
| `FLAT_TIER` | File sits directly in `docs/` with no tier subdirectory | detection-only |
| `ORPHAN` | File not reachable from `docs/README.md` via index links | detection-only |
| `ABSOLUTE_LINK` | Contains an internal link starting with `/` | auto-rewritten |
| `PROSE_LINK` | Bare `` `filename.md` `` backtick ref to a known docs file | detection-only |
| `NO_BREADCRUMB` | Missing `> **Up:** [↑ ...]` breadcrumb within first ~10 lines | auto-inserted |

Exit 0 = no findings. Exit 1 = findings present. Exit 2 = error.

### --apply mode

```bash
python3 .claude/skills/grm-docs-migrate/docs_migrate.py --apply
```

1. Archives all affected files verbatim to `.grimoire-archive/<timestamp>/`
   + writes `MANIFEST.md` there.
2. Inserts breadcrumb up-links (canonical WH-0 form) as the first non-blank,
   non-heading content after the `# Title` line (resolves `NO_BREADCRUMB`).
3. Rewrites resolvable **absolute** internal links to relative paths
   (resolves `ABSOLUTE_LINK`). **`PROSE_LINK`, `ORPHAN`, and `FLAT_TIER` are
   detection-only — `--apply` does not rewrite or move them** (they need human
   judgement on anchor/section, index wiring, and physical placement).
4. Leaves `<!-- docs-migrate: UNRESOLVED <original> -->` for any ref that
   cannot be resolved — prints a loud banner and exits 1. **Never guesses,
   never deletes.**
5. Prints a summary of any detection-only categories
   (`PROSE_LINK`/`ORPHAN`/`FLAT_TIER`) it left untouched, so a clean `--apply`
   is never mistaken for zero remaining findings.
6. Idempotent — a second run on an already-migrated tree is a no-op.

### --self-test

```bash
python3 .claude/skills/grm-docs-migrate/docs_migrate.py --self-test
```

Runs 15 deterministic in-memory fixture tests covering detect, apply,
idempotency, archive, dry-run, fallback, and exemption behavior.
Exit 0 = all pass.

### --dry-run

```bash
python3 .claude/skills/grm-docs-migrate/docs_migrate.py --dry-run
python3 .claude/skills/grm-docs-migrate/docs_migrate.py --apply --dry-run
```

Shows what would change without writing any files.

### --docs-root override

```bash
python3 .claude/skills/grm-docs-migrate/docs_migrate.py --docs-root path/to/docs
```

Overrides the default `docs/` directory. Useful for non-standard layouts.

## Downstream trigger (sync-from-upstream)

After a clean `--apply` (Step 3), `grm-sync-from-upstream` checks for old-style
docs by running `docs_migrate.py` in detect mode. If findings exist:

1. Presents the finding count and a summary.
2. **Offers** to run `docs_migrate.py --apply` — asks the user explicitly.
3. **Never auto-runs**, even under Noir.

Under **Stealth Mode**: the offer is suppressed entirely (leave zero footprint).

## Exemptions

The following files are exempt from all classification checks:

- `release-planning-v*.md` — path-locked by `release-plan-guard.sh` and the
  release skill chain.
- `version-history.md` — operator-facing, stays at `docs/` top level.
- `qa-ledger.md` — ledger artifact, path-locked.
- `README.md` files — these ARE the index pages; they are never checked for
  breadcrumbs.

## Anti-patterns

- **Never auto-run `--apply` under any paradigm** (including Noir) — migration
  rewrites user-owned docs and must always be explicitly confirmed.
- **Never guess at unresolvable links** — always leave the UNRESOLVED marker
  and let the user fix the ref manually.
- **Never run inside the scaffolding repo itself** — this tool is for
  downstream projects. The scaffolding's own docs are managed differently.
- **Never delete a file** — archive-before-rewrite only; docs-migrate is
  additive (breadcrumbs in, relative paths in) never destructive.
