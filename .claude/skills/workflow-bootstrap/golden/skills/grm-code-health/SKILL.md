---
name: code-health
description: Emit a code-health report for a managed project in two sections — dead code + duplication (vulture / ts-prune / cargo-udeps + a jscpd-style duplication pass) and complexity + maintainability (radon / ts-complexity / gocyclo) with a delta against a stored baseline (.claude/cache/code-health-baseline.json). Read-only report by default; an optional gate warns/blocks on a regression or a new dead-code/duplication finding (reuses the v1.26 code-quality dials). Triggers on "check code health", "find dead code", "detect duplication", "complexity report", "maintainability metrics", "code health scan", "is this getting more complex".
---

# code-health

Deterministic code-health scan for a managed project. Two report sections from
language-appropriate tools; an optional regression gate. Design:
`docs/design/managed-project-tooling-design.md`.

## Detect the stack + tools

| Language | Dead code / unused | Duplication | Complexity |
|---|---|---|---|
| Python | `vulture` | `jscpd`/`pylint --duplicate` | `radon cc` / `radon mi` |
| JS/TS | `ts-prune` | `jscpd` | `ts-complexity` / eslint-complexity |
| Rust | `cargo-udeps` / `cargo machete` (+ `dead_code` lint) | `jscpd` | clippy `cognitive_complexity` + fn/module line budget |
| Go | `deadcode` / `staticcheck U1000` | `jscpd` | `gocyclo` |
| HTML/CSS | dead-CSS (PurgeCSS-style dry-run) / `stylelint` unused | `jscpd` | stylelint specificity / nesting-depth |

If a tool is absent, **report it** and name the install command; skip that
metric (never fail silently).

## Steps

1. Detect language(s) + available tools.
2. **Section A — dead code + duplication.** Run the unused-symbol scanner +
   duplication pass. Report each unused symbol (`file:line symbol`) and each
   duplicated block (`> N lines across M sites`). Cross-file duplication is a
   **first-class, gate-able** finding: for each block, name the remediation —
   *lift → generalize → register in the component-registry* (see
   `docs/coding-standards.md` §DRY & duplication remediation) — not just the
   sites. The duplication threshold (block size × site count) is governed by the
   v1.26 `audit-gate` dial under `--gate`.
3. **Section B — complexity + maintainability.** Run the complexity tool; collect
   per-unit cyclomatic/cognitive complexity + a maintainability index. Also
   compute **module-coupling metrics** per module (directory/package) from the
   import scan (the same scan `architecture-audit` uses): afferent coupling
   (Ca), efferent coupling (Ce), instability `I = Ce/(Ca+Ce)`, and module size.
   Flag any module that is both widely depended-upon (high Ca) **and** unstable
   (high I) — the painful-to-change hotspot — and any module over its size
   budget. See `docs/architecture-guidelines.md` §Modularization metrics.
4. **Baseline delta.** Read `.claude/cache/code-health-baseline.json` (a derived,
   regenerable, gitignorable cache). Report current values **and** the delta vs
   baseline. `--accept` writes the current values as the new baseline.
5. Emit the combined report (machine block + human tables).
6. **Gate (optional).** With `--gate`, treat a **regression** (complexity up past
   a threshold, or a new dead-code/duplication finding vs baseline) as a
   warn/block per the v1.26 `code-quality` dials. Default is report-only.

## Output (report shape)

```
code-health — Section A: dead code 3, duplication 1 block (42 lines, 2 sites)
code-health — Section B: avg CC 4.1 (Δ +0.3 vs baseline), MI 78 (Δ -2)
```

## Idempotency & safety

- Read-only except `--accept` (writes the baseline cache) and `--file-issues`
  (routes through `feedback-to-issue`, deduped).
- Re-running with unchanged sources + tools is deterministic.
- The baseline lives under `.claude/cache/` (regenerable); the source tree is
  the only source of truth for the metrics.
