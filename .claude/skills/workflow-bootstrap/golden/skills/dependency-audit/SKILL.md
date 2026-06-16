---
name: dependency-audit
description: Run the language-appropriate dependency vulnerability / advisory scanner (pip-audit, npm audit, cargo audit, govulncheck) behind one abstraction and emit a normalized findings report (package, advisory id, severity, fixed-in). Read-only by default; never edits manifests or lockfiles. With --file-issues, routes each finding through feedback-to-issue (severity → label). Optional pre-release gate fails on findings at or above a configured severity. Triggers on "audit dependencies", "check for vulnerable packages", "dependency audit", "security audit the deps", "scan dependencies for CVEs", "run pip-audit / npm audit / cargo audit".
---

# dependency-audit

Deterministic dependency vulnerability scan for a managed project. One skill,
language-dispatched. Read-only — it reports; it never mutates manifests or
lockfiles. Design: `docs/design/managed-project-tooling-design.md`.

## Detect the stack

Pick the scanner from the project's manifest files (first match wins; run all
that apply in a polyglot repo):

| Manifest present | Scanner | Invocation |
|---|---|---|
| `requirements*.txt` / `pyproject.toml` / `poetry.lock` | `pip-audit` | `pip-audit -f json` |
| `package.json` / `package-lock.json` / `pnpm-lock.yaml` | `npm audit` | `npm audit --json` |
| `Cargo.toml` / `Cargo.lock` | `cargo audit` | `cargo audit --json` |
| `go.mod` | `govulncheck` | `govulncheck -json ./...` |

If the scanner binary is absent, **report that** (do not fail silently) and name
the install command; skip that ecosystem.

## Steps

1. Detect ecosystem(s) from manifests.
2. Run each scanner; capture JSON.
3. Normalize every finding to: `{package, version, advisory, severity, fixed-in,
   ecosystem}`. Severity normalized to `low|moderate|high|critical`.
4. Emit the report (machine block + human table), sorted by severity desc.
5. **`--file-issues`** (optional): file one issue per finding via
   `feedback-to-issue` (audience internal; label = `security` + severity). Dedupe
   against open issues by `{ecosystem}:{package}:{advisory}` key.
6. **Gate (optional)**: with `--fail-at <severity>`, exit non-zero if any finding
   is at or above that severity. The v1.26 merge gate / a pre-release step may
   invoke this; default is report-only.

## Output (report shape)

```
dependency-audit — <n> finding(s) across <m> ecosystem(s)
| severity | ecosystem | package | version | advisory | fixed-in |
|----------|-----------|---------|---------|----------|----------|
| critical | pypi      | urllib3 | 1.26.4  | GHSA-... | 1.26.18  |
```

## Safety & idempotency

- Never writes to manifests/lockfiles; `--file-issues` is the only write path and
  is deduped + audience-routed through the issue-tracker abstraction.
- Re-running with unchanged dependencies + advisory DB is deterministic.
- No hosted service; uses the ecosystem's standard scanner only.
