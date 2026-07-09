---
name: grm-config-validate
description: Validate .claude/grimoire-config.json against the declared schema (known blocks + value sets, cross-rules like Auto-requires-Noir), report unknown/missing fields, and run an idempotent migration that fills additive defaults. Read-only by default; --migrate writes atomically. Called by install-doctor as part of the health audit. Use when validating or migrating the config.
---

# config-validate

Schema validation + idempotent migration for `.claude/grimoire-config.json`.
Design: `docs/design/defaults-quickstart-design.md`.

## Run

```bash
python3 .claude/skills/grm-config-validate/config_validate.py            # validate (read-only)
python3 .claude/skills/grm-config-validate/config_validate.py --migrate  # fill additive defaults, then validate
python3 .claude/skills/grm-config-validate/config_validate.py --path <p> # validate another config
```

Exit 0 = valid (after optional migrate); exit 1 = errors remain.

## What it checks

- Required fields (`schema-version`, `name`).
- Value-set enums for the dials (`work-paradigm`, `workflow-variant`,
  `release-phase-model`, the `code-quality` dials).
- Cross-rules (e.g. `release-phase-model=Auto` requires `work-paradigm=Noir`).
- Unknown top-level fields → **warning** (surfaced, not silently accepted).
- `--migrate` fills additive default blocks (e.g. a missing `code-quality`
  block ⇒ defaults), writing atomically (temp + validate + replace) so a write
  never corrupts the file.

## Integration

`grm-install-doctor` calls `grm-config-validate` as part of its read-only health audit,
so a malformed or stale config is surfaced early instead of failing late.
