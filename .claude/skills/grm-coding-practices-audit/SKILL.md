---
name: grm-coding-practices-audit
description: Audit a codebase against the project's coding standards and architecture guidelines and produce a structured adherence report; optionally file one Issue Tracker issue per gap. Agent-driven (no linter/AST). The audit surface is read from machine-readable audit-hints in the standards docs, so it grows as those docs evolve. Use when auditing adherence to coding standards or filing issues for standards gaps.
---

# Coding-practices audit

Reads the active `docs/coding-standards.md`, `docs/architecture-guidelines.md`,
and their per-language sub-docs (`docs/coding-standards/*.md`), assembles a
checklist from the **audit-hints** they carry, walks the project's code, and
reports adherence gaps. Two modes: a read-only **report** (default) and
**`--file-issues`**, which files one issue per gap via `grm-feedback-to-issue`.

This skill is **agent-driven**, not a linter — it reasons about the code; it does
not run static analysis or parse an AST. Design + the audit-hint convention:
`docs/design/coding-practices-audit-design.md`.

---

## Step 1 — Assemble the audit surface (data-driven)

Grep the standards docs for audit-hint markers and parse each into a check:

```bash
grep -rn 'audit: id=' docs/coding-standards.md docs/architecture-guidelines.md docs/coding-standards/ 2>/dev/null
```

Each marker has the form:

```
<!-- audit: id="…" check="…" severity="error|warn|info" applies="all|web,gui,api,service,cli,lib" -->
```

Build a flat list of `{id, check, severity, applies}`. A standard **without** a
hint is advisory only — do not invent checks for it.

> The active docs are the source of truth — read them at run time. As #31/#32/#15
> add hints, the surface grows with **no change to this skill**.

## Step 2 — Scope to the project type

Read the detected project type (the `grm-workflow-bootstrap` GUI/project-type signal,
or `.claude/grimoire-config.json`). Keep a check when `applies="all"` or when its
`applies` set intersects the project type. Record which checks were **skipped**
and why (out of scope) — never silently drop them.

## Step 3 — Walk the code and find gaps

For each in-scope check, inspect the relevant code and collect findings. Cover at
least the standing surface:
- OO design: shared behaviour in base classes; no duplicated logic.
- Error handling: all error conditions handled; no silent failures.
- Unit-test coverage: every function has at least one test.
- One file per class/module.
- No magic numbers.
- Telemetry (`telemetry-*` hints): startup + unhandled-error events present;
  project-type interaction surface instrumented.
- Architecture (`arch-*` hints): decoupled FE/BE, modularity, layer separation,
  genericity.

## Step 4 — Report (default)

Emit structured findings, grouped by severity, newest-actionable first:

```
### <severity> — <id>
- check:   <check text>
- file:    <path>:<line>
- finding: <what violates it>
- remedy:  <one-line remediation>
```

End with a **coverage summary**: checks evaluated, checks skipped (with reason),
and counts by severity. If you sampled or bounded the walk on a large repo, say
so explicitly (no silent caps). **Report mode performs no issue-tracker writes.**

## Step 5 — `--file-issues` (optional)

After the report, file **one issue per finding** via `grm-feedback-to-issue`
(audience: internal). The implied goal is 100 % adherence.

- **Title convention:** `coding-practices: <id> — <file>` (encodes the
  `(id, file)` pair for dedup).
- **Near-duplicate suppression:** before filing, check existing open issues; skip
  any finding whose `(id, file)` pair already has an open issue. Rely on
  `grm-feedback-to-issue`'s near-dup check plus the `id`-tagged title.
- **Batch:** for many findings, prefer one Reporter session (`spawn_task`) that
  files them all, then return.
- **No auto-remediation** — file issues only; never edit code.

## Invocation

```
coding-practices-audit                 # report mode (read-only)
coding-practices-audit --file-issues   # report, then file one issue per gap
```

## Safety / scope
- Read-only except the `--file-issues` tracker writes; **no git commits**.
- Routes through the issue-tracker abstraction (honors the configured backend).
- Not a CI gate, not a linter, not an auto-fixer (all out of scope — see design
  doc §6).

## Anti-patterns
- Hard-coding the checklist in this skill — the surface MUST come from the docs'
  audit-hints, so it stays in sync as standards evolve.
- Filing issues in report mode, or filing without the near-dup check.
- Silent truncation on large repos — always state coverage and any sampling.
