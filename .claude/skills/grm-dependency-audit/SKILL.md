---
name: grm-dependency-audit
description: Run the language-appropriate dependency vulnerability / advisory scanner (pip-audit, npm audit, cargo audit, govulncheck) behind one abstraction and emit a normalized findings report. Read-only; never edits manifests or lockfiles. With --file-issues, routes each finding through feedback-to-issue; an optional pre-release gate fails at or above a configured severity. Use when auditing dependencies.
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

**Four finding classes.** Each finding is normalized to
`{check, dep, channel, severity, detail, locked_sha, observed_sha}`:

| `check` | Flags |
|---|---|
| `non-channel-source` | a dep sourced from a git submodule (`.gitmodules`) instead of a release channel |
| `lock-bytes-mismatch` | vendored bytes with no `vendor.lock` entry, or whose recomputed `tree_sha256` ≠ the locked one |
| `unpublished-release` | a `vendor.toml` pin whose release **tag does not exist** on its channel (network; degrades gracefully offline) |
| `malformed-release` | the pinned tag **exists but is not a conformant producer** — its `release.json` + `SHA256SUMS` + primary-artifact trio is missing an asset or self-inconsistent (a checksum disagrees, `primary_artifact_sha256` ≠ the tarball's real hash, `artifact_kind` ≠ the pinned `kind`, …) |

The last two are the network surface: the publish probe **downloads and verifies
the trio** (`evaluate_trio`), not merely that a tag exists, so a published-but-broken
producer is caught. The byte-level verification is pure and exercised fully offline
by `--self-test`.

**Invocation:**

```bash
# Offline self-test (no network):
python3 .claude/skills/grm-dependency-audit/dependency_channel_conformance.py --self-test

# Audit a repo (checks 1 & 2 offline; checks 3 & 4 need `gh` + network):
python3 .claude/skills/grm-dependency-audit/dependency_channel_conformance.py --root . --json
python3 .claude/skills/grm-dependency-audit/dependency_channel_conformance.py --root . --offline
```

Exit 0 = conformant, nonzero = at least one violation (the loud-exit contract).
**Warn-only this release:** the merge-gate (`grm-release-phase-merge` §3.5 step 3a)
runs it advisory — reads the live `code-quality.audit-gate` dial, files findings
via `grm-feedback-to-issue` (labels `security` + `dependency-channel`), and proceeds.
The CLI entrypoint the `vendor-check` recipe verb calls is `main()`; programmatic
callers use `DependencyChannelConformance(root).run(offline=...)`.

## Vendor provenance integrity check (`--verify`, #315)

A fourth, read-only pass — `.claude/skills/grm-sync-deps/vendor_verify.py`,
invoked here for discoverability alongside `vendor-check` — checks whether the
provenance metadata a consumer *trusts* actually matches the vendored bytes on
disk, rather than trusting it blindly. Distinct from the conformance pass
above: conformance asks "is this dep sourced correctly and published?"; this
pass asks "did the vendored tree silently drift, go dead, or lie about its own
version?" Zero network calls — fully offline and self-test-able.

**Four finding classes**, normalized to `{check, dep, severity, detail,
locked, observed}`:

| `check` | Severity | Flags |
|---|---|---|
| `LOCAL-FORK` | error | vendored tree's recomputed `tree_sha256` disagrees with the `vendor.lock` pin — reported with a per-file added/removed/changed diff summary; a `VENDOR.md` front-matter `claim:` is cross-checked when present |
| `DEAD-VENDOR` | error | a `vendor.toml` dep's declared `dest` is missing/empty, **or** a `.gitmodules` submodule is uninitialized/empty on disk |
| `VERSION-CONTRADICTION` | error | a version string inside the vendored tree (`Cargo.toml`, `package.json`, `VENDOR.md` front matter) disagrees with the `vendor.toml` pin |
| `STUB-VENDOR-MANIFEST` | **warn-only** | heuristic: an all-stub `vendor.toml` (zero deps) while the repo references vendoring elsewhere — never fails the run alone |

**Invocation:**

```bash
python3 .claude/skills/grm-sync-deps/sync_deps.py --verify --root .
python3 .claude/skills/grm-sync-deps/sync_deps.py --verify --root . --json
python3 .claude/skills/grm-sync-deps/vendor_verify.py --self-test   # offline
```

Exit 0 = clean; nonzero = at least one `LOCAL-FORK` / `DEAD-VENDOR` /
`VERSION-CONTRADICTION` finding (`STUB-VENDOR-MANIFEST` alone never elevates
the exit code). Wired into the release gate alongside `grm-doc-assurance`
(`docs/grimoire/integration-workflow.md` §Release gate); full detail in
`grm-sync-deps` SKILL.md §Provenance verification and
`docs/grimoire/design/dependency-channel-design.md` §Provenance verification.

## Dependency-chain architecture diagram

A third, read-only pass — `architecture_diagram.py` — renders the *shape* of a
project's dependency chain from the same `vendor.toml`/`vendor.lock` surface:
one node per `[deps.<name>]` block plus this project's own node (metadata
reused from `grm-agent-status-broker/project_status.py`, not re-derived), one edge
per pin labeled with its version + channel. Design:
`docs/grimoire/design/dependency-architecture-diagram-design.md`.

**Invocation:**

```bash
# Offline self-test (no network):
python3 .claude/skills/grm-dependency-audit/architecture_diagram.py --self-test

# Diagram a real repo root, Graphviz DOT to stdout (zero-dependency — plain text):
python3 .claude/skills/grm-dependency-audit/architecture_diagram.py --root .

# Same graph as structured {nodes, edges} JSON:
python3 .claude/skills/grm-dependency-audit/architecture_diagram.py --root . --json

# Recursive walk into a dependency's own vendor.toml (bounded, explicit; default depth=1):
python3 .claude/skills/grm-dependency-audit/architecture_diagram.py --root . --depth 2

# Mark pins behind the producer's latest release as stale edges (network; reuses
# the conformance probe's latest_release_tag rather than a forced extra round-trip):
python3 .claude/skills/grm-dependency-audit/architecture_diagram.py --root . --with-conformance
```

- `--depth N` (default 1, capped at 8) recurses into a dependency's own
  `vendor.toml` via `ChannelProbe` (reused from `dependency_channel_conformance.py`
  rather than a second network client) — degrades to single-level with a loud
  warning when the network/`gh` is unavailable, mirroring the conformance
  script's offline-degradation contract exactly.
- Determinism: sorted node/edge iteration means unchanged input produces
  byte-identical DOT/JSON output.
- `--with-conformance` is the only path that touches the network to compute
  staleness; without it, staleness is a pure offline read of `vendor.lock`.

## Safety & idempotency

- Never writes to manifests/lockfiles; `--file-issues` is the only write path and
  is deduped + audience-routed through the issue-tracker abstraction.
- Re-running with unchanged dependencies + advisory DB is deterministic.
- No hosted service; uses the ecosystem's standard scanner only.
- The conformance pass is read-only and offline-deterministic in `--self-test` /
  `--offline`; only the publish/trio checks touch the network and degrade loudly.
- The architecture-diagram pass is read-only; only `--depth 2+` and
  `--with-conformance` touch the network, and both degrade loudly (never a
  hard fail) when unreachable.
- The provenance-verify pass (`--verify`) is fully read-only and offline —
  zero network calls, deterministic in `--self-test`.
