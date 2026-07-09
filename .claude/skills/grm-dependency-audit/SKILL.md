---
name: grm-dependency-audit
description: Run the language-appropriate dependency vulnerability / advisory scanner (pip-audit, npm audit, cargo audit, govulncheck) behind one abstraction and emit a normalized findings report (package, advisory id, severity, fixed-in). Read-only; never edits manifests or lockfiles. With --file-issues, routes each finding through feedback-to-issue; an optional pre-release gate fails at or above a configured severity. Use when auditing dependencies.
---

# dependency-audit

Deterministic dependency vulnerability scan for a managed project. One skill,
language-dispatched. Read-only — it reports; it never mutates manifests or
lockfiles. Design: `docs/grimoire/design/managed-project-tooling-design.md`.

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
   `grm-feedback-to-issue` (audience internal; label = `security` + severity). Dedupe
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

## Dependency-Channel conformance pass (`vendor-check`)

A second, deterministic pass — `dependency_channel_conformance.py` — validates a
repo's **vendored** dependencies against the Dependency Channel contract
(`docs/design/dependency-channel-design.md` §5). It is the implementation behind
the **`recipe.py vendor-check`** verb. Distinct from the vulnerability scan
above: it checks *sourcing/integrity*, not CVEs.

**Three checks.** Each finding is normalized to
`{check, dep, channel, severity, detail, locked_sha, observed_sha}`:

| `check` | Flags |
|---|---|
| `non-channel-source` | a dep sourced from a git submodule (`.gitmodules`) instead of a release channel |
| `lock-bytes-mismatch` | vendored bytes with no `vendor.lock` entry, or whose recomputed `tree_sha256` ≠ the locked one |
| `unpublished-release` | a `vendor.toml` pin that is not a published release on its channel (network; degrades gracefully offline) |

**Invocation:**

```bash
# Offline self-test (no network):
python3 .claude/skills/grm-dependency-audit/dependency_channel_conformance.py --self-test

# Audit a repo (checks 1 & 2 offline; check 3 needs `gh`):
python3 .claude/skills/grm-dependency-audit/dependency_channel_conformance.py --root . --json
python3 .claude/skills/grm-dependency-audit/dependency_channel_conformance.py --root . --offline
```

Exit 0 = conformant, nonzero = at least one violation (the loud-exit contract).
**Warn-only this release:** the merge-gate (`grm-release-phase-merge` §3.5 step 3a)
runs it advisory — reads the live `code-quality.audit-gate` dial, files findings
via `grm-feedback-to-issue` (labels `security` + `dependency-channel`), and proceeds.
The CLI entrypoint the `vendor-check` recipe verb calls is `main()`; programmatic
callers use `DependencyChannelConformance(root).run(offline=...)`.

## Safety & idempotency

- Never writes to manifests/lockfiles; `--file-issues` is the only write path and
  is deduped + audience-routed through the issue-tracker abstraction.
- Re-running with unchanged dependencies + advisory DB is deterministic.
- No hosted service; uses the ecosystem's standard scanner only.
- The conformance pass is read-only and offline-deterministic in `--self-test` /
  `--offline`; only the publish check touches the network and degrades loudly.
