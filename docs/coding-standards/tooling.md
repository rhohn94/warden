# Deterministic tooling tier

> Authority for the linter/formatter + pre-commit layer a managed project ships
> with (v1.27). Complements the agent-driven `grm-coding-practices-audit` with fast,
> deterministic, CI-runnable tools. Design: the managed-project tooling spec,
> maintained in the upstream Grimoire repo.

## Single source of truth

Each command is defined **once** (captured at `grm-workflow-bootstrap`) and reused by
the pre-commit hook, the v1.26 merge gate, and CI:

| Command | Placeholder | Captured at |
|---|---|---|
| Lint | `{lint-command}` | bootstrap (optional) |
| Format | (folded into lint or its own hook) | bootstrap (optional) |
| Type-check | `{typecheck-command}` | bootstrap (optional) |
| Coverage | `{coverage-command}` | bootstrap (optional) |

A blank answer leaves that gate off (the v1.26 `code-quality` dials default safe).

## Linter + formatter, per language

| Language | Linter | Formatter |
|---|---|---|
| Python | `ruff` | `black` (or `ruff format`) |
| JS / TS | `eslint` | `prettier` |
| Rust | `clippy` | `rustfmt` |
| Go | `go vet` / `staticcheck` | `gofmt` |

Quick-start templates ship a matching config file in their `files/` tree, so a
scaffolded project lints and formats from day one. `grm-quick-start-template` drops
the config in on apply.

## Pre-commit (opt-in)

A `.pre-commit-config.yaml` (or native git hook) that runs **format → lint →
fast tests**, reusing the same commands above — no command duplicated between
pre-commit and the merge gate. Installed on opt-in at bootstrap; never forced.

```yaml
# .pre-commit-config.yaml (illustrative)
repos:
  - repo: local
    hooks:
      - id: format
        name: format
        entry: <format-command>
        language: system
      - id: lint
        name: lint
        entry: <lint-command>
        language: system
      - id: tests-fast
        name: fast tests
        entry: <fast-test-command>
        language: system
```

## Relationship to the other quality skills

- `grm-dependency-audit` — vulnerability/advisory scan (not a linter; separate cadence).
- `grm-code-health` — dead code + duplication + complexity trend.
- `grm-coding-practices-audit` — agent-driven, hint-keyed adherence (nuance the linters
  can't express).

The deterministic tier catches mechanical issues cheaply; the agent audit catches
the rest.
