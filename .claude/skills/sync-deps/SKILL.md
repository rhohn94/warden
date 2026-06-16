---
name: sync-deps
description: Reconcile a repository's first-party dependencies from published GitHub Release channels into committed vendor/<dep>/ trees, then record the resolved truth in a JSON vendor.lock. Build & runtime read only vendor/<dep>/; the network is touched only at sync time. Per dep — resolve channel→version (pin by default; --update bumps the pin via gh), download release.json + SHA256SUMS + the artifact, verify sha256 BEFORE placement (hard-refuse on mismatch, loud-degrade when SHA256SUMS is absent), stage + atomic-replace, write a two-hash lock (artifact_sha256 wire + tree_sha256 offline-drift). --check detects drift writing nothing; --offline validates with zero network. Triggers on "sync dependencies", "vendor a dependency", "run sync-deps", "update a vendored dep", "check vendor drift", "validate vendored bytes offline", "rebuild vendor.lock".
---

# sync-deps

The **Dependency Channel** *consumer* engine (design
`docs/design/dependency-channel-design.md` §3–§4). It reconciles every dep
declared in `vendor.toml` from a published GitHub Release **channel** into a
committed `vendor/<dep>/` tree, and records the resolved truth in a JSON
`vendor.lock`. **Build & runtime read only `vendor/<dep>/`** — the network is
touched **only** at sync time, never at build or run time.

> **Preferred interface — the `sync_deps.py` script.** The whole resolve →
> download → verify → atomic-replace → write-lock loop is a deterministic
> stdlib engine; don't re-derive it in prose. Call the script and interpret its
> exit code:
>
> ```bash
> python3 .claude/skills/sync-deps/sync_deps.py            # sync all deps (pin by default)
> python3 .claude/skills/sync-deps/sync_deps.py --dep aura # sync one dep
> python3 .claude/skills/sync-deps/sync_deps.py --update   # resolve latest-on-channel, rewrite the pin
> python3 .claude/skills/sync-deps/sync_deps.py --check    # detect drift; write nothing; nonzero on drift
> python3 .claude/skills/sync-deps/sync_deps.py --offline  # validate vendored bytes vs the lock, zero network
> python3 .claude/skills/sync-deps/sync_deps.py --self-test
> ```
>
> Also exposed as the **`recipe.py sync-deps`** verb (DEP-CH-3). Exit codes
> mirror `sync-from-upstream`'s 0/1/2 contract: **0** = ok, **1** = hard refuse
> (checksum mismatch / drift / bad input), **2** = loud degrade (`SHA256SUMS`
> absent or no entry — never a silent skip).

## Artifacts (design §3)

Three committed artifacts per repo. **`vendor/<dep>/` is committed**, not
gitignored — that is the "fully offline, committed" principle.

**`vendor.toml`** — human-authored intent (parsed with `tomllib`; **Python 3.11+**):

```toml
schema_version = 1

[deps.aura]
repo = "rhohn94/design-language"
channel = "stable"            # stable | beta — the producer's release channel
version = "3.20.0"            # the pin; sync fetches exactly tag v3.20.0
artifact = "aura-v3.20.0.tar.gz"
dest = "vendor/aura"
kind = "asset-bundle"         # asset-bundle | vendored-crate | app-binary
strip_components = 1          # optional — drop N leading path components
# extract = ["css/", "fonts/"]   # optional subset allowlist (path prefixes)
```

**`vendor.lock`** — auto-generated resolved truth, **JSON**, do-not-hand-edit.
Each dep entry carries the **two-hash model**: `artifact_sha256` (the wire hash,
equals the `SHA256SUMS` entry, verifies the download) + `tree_sha256` (a
deterministic hash over the placed `vendor/<dep>/` bytes — re-derivable offline,
recomputed by `--check` and the conformance gate to detect drift):

```json
{
  "schema_version": 1,
  "deps": {
    "aura": {
      "version": "3.20.0",
      "channel": "stable",
      "git_sha": "0a1b2c3…",
      "release_tag": "v3.20.0",
      "release_url": "https://github.com/rhohn94/design-language/releases/tag/v3.20.0",
      "artifact": "aura-v3.20.0.tar.gz",
      "artifact_sha256": "sha256:…",
      "tree_sha256": "sha256:…",
      "release_json_sha256": "sha256:…",
      "signature_verified": false,
      "synced_at": "2026-06-13T18:20:00Z"
    }
  }
}
```

The lock is JSON so the verifier and the conformance gate (§5) stay pure-stdlib
on the read/gate path — no TOML parse needed to *read* the lock. The write-side
tools (`sync-deps`, `vendor-migrate`) own the TOML floor. Writes are
**idempotent**: an unchanged pin yields a byte-identical lock (a re-sync is a
no-op).

## Pipeline (design §4) — per dep, in order

1. **Resolve** channel → version. **Pin by default** — `version` in `vendor.toml`
   is the pin; sync fetches exactly that release tag. `--update` resolves
   *latest-on-channel* via `gh` (`gh api …/releases` filtered by prerelease for
   `beta`/`stable`), picks the highest semver, and **rewrites the `vendor.toml`
   pin** (line-oriented, comment-preserving).
2. **Download** `release.json` + `SHA256SUMS` + the artifact into a **fixed,
   app-owned staging dir** (`.sync-deps-staging/` under the repo root) — never a
   path derived from server metadata.
3. **Verify** — recompute the artifact's sha256 and require an exact match
   against its `SHA256SUMS` entry **before any filesystem placement**. A mismatch
   is a **hard refuse** (exit 1); an absent `SHA256SUMS`/missing entry is a **loud
   degrade** (exit 2), never silent trust. (`{artifact}.minisig` verification is
   the deferred-signing seam — `signature_verified` stays `false` this release.)
4. **Stage + atomic-replace** `vendor/<dep>/` — extract into staging (honoring
   `strip_components` / `extract` allowlist, with tar path-traversal hardening),
   then atomic `os.replace` into place. On any failure the existing tree is rolled
   back; **`vendor/<dep>/` is never left half-written**.
5. **Write `vendor.lock`** via write-if-changed (deterministic, sorted keys).

## Modes

| Flag | Behaviour | Network |
|---|---|---|
| *(none)* | resolve (pin) → fetch → verify → vendor → write lock | yes (`gh`) |
| `--update` | resolve latest-on-channel, rewrite the pin, then sync | yes (`gh`) |
| `--check` | recompute `tree_sha256` vs the lock; **write nothing**; nonzero on drift | no |
| `--offline` | assert vendored bytes match the lock; the "build with network disabled" gate | **none** |
| `--self-test` | deterministic, offline-fixture-based regression run | none |

## `vendored-crate` semantics

For `kind = "vendored-crate"` the producer prepends a `<dep>/` top-level
component and ships an include-subset; the consumer **strips one leading
component by default** (override with explicit `strip_components`) and honors the
`extract` allowlist (e.g. `["src/", "Cargo.toml"]`, excluding
`tests/`/`benches/`/`examples/`/`target/`). Downstream `Cargo.toml` points a path
dependency at the placed tree (`<dep> = { path = "vendor/<dep>" }`), resolvable
with `cargo build --offline` post-fetch. `cargo` is invoked only by the managed
project — never by this engine.

## Security invariants (Ollama-RCE avoidance, design §11)

- **Asset-name allowlist** — only `release.json`, `SHA256SUMS`, and a validated
  archive basename are ever read/written; traversal, absolute, and non-archive
  names are refused. Server-supplied names are never trusted.
- **Fixed, app-owned staging dir** — never a server-derived path.
- **Verify sha256 BEFORE placement** — the checksum gate runs before any extract
  or rename (checksum-before-signature).
- **Atomic rename into place** — write-to-staging → verify → atomic `os.replace`.
- **Tar hardening** — traversal/absolute members and special files (symlinks,
  devices) are rejected/skipped on extract.

## Push posture (autonomous contexts)

The engine **never pushes**. Vendoring + lock writes are local and unguarded;
landing them to a protected branch is push-class (human-gated unless
`autonomous-push.enabled`). In an autonomous context, drive the writes with this
engine and then **open a PR via the `github-pr` path** rather than committing
directly to a protected branch — the engine itself only writes files.

## Coverage

`--self-test` (deterministic, stdlib-only, offline-fixture-based) exercises:
sync + correct two-hash lock; byte-identical re-sync no-op; `--check` clean →
drift (nonzero, writes nothing); `--offline` clean validation with zero network;
**tampered artifact hard-refused with `vendor/<dep>/` left untouched and no lock
entry written**; absent `SHA256SUMS` loud-degrade; `--update` pin rewrite +
re-vendor; `vendored-crate` strip + extract allowlist; asset-name allowlist and
tar-traversal refusals.

Design: `docs/design/dependency-channel-design.md` §3–§4.
