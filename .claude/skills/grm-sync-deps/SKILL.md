---
name: grm-sync-deps
description: Reconcile a repository's first-party dependencies from published GitHub Release channels into committed vendor/<dep>/ trees, recording the resolved truth in vendor.lock. Build & runtime read only vendor/<dep>/; the network is touched only at sync time. Modes: default pin-and-sync (--update bumps the pin via gh), --check detects drift, --offline validates bytes offline. Verifies sha256 before placement. Use when vendoring or checking a dep.
---

# sync-deps

The **Dependency Channel** *consumer* engine (design
`docs/design/dependency-channel-design.md` §3–§4). It reconciles every dep
declared in `vendor.toml` from a published GitHub Release **channel** into the
dep's committed `dest` tree, and records the resolved truth in a JSON
`vendor.lock`. **Build & runtime read only the vendored `dest` bytes** — the
network is touched **only** at sync time, never at build or run time.

> **Standard destination.** Vendored deps live under `lib/third-party/<dep>/`
> per `docs/project-structure.md` — declare `dest = "lib/third-party/<dep>"`.
> `dest` may be any repo-relative path; a legacy `vendor/<dep>` still syncs.
> Relocate a legacy `vendor/` tree with **`grm-structure-migrate`**.

> **Preferred interface — the `sync_deps.py` script.** The whole resolve →
> download → verify → atomic-replace → write-lock loop is a deterministic
> stdlib engine; don't re-derive it in prose. Call the script and interpret its
> exit code:
>
> ```bash
> python3 .claude/skills/grm-sync-deps/sync_deps.py            # sync all deps (pin by default)
> python3 .claude/skills/grm-sync-deps/sync_deps.py --dep aura # sync one dep
> python3 .claude/skills/grm-sync-deps/sync_deps.py --update   # resolve latest-on-channel, rewrite the pin
> python3 .claude/skills/grm-sync-deps/sync_deps.py --check    # detect drift; write nothing; nonzero on drift
> python3 .claude/skills/grm-sync-deps/sync_deps.py --offline  # validate vendored bytes vs the lock, zero network
> python3 .claude/skills/grm-sync-deps/sync_deps.py --verify   # provenance integrity check (#315) — see below
> python3 .claude/skills/grm-sync-deps/sync_deps.py --self-test
> ```
>
> Also exposed as the **`recipe.py sync-deps`** verb (DEP-CH-3). Exit codes
> mirror `grm-sync-from-upstream`'s 0/1/2 contract: **0** = ok, **1** = hard refuse
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
dest = "lib/third-party/aura"
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
      "signature_verified": "unsigned",
      "synced_at": "2026-06-13T18:20:00Z"
    }
  }
}
```

**`signature_verified`** is a **tri-state** (`true` | `false` | `"unsigned"` —
v3.79, §Signing / #318): `"unsigned"` when no `pubkey` is pinned for the dep in
`vendor.toml`, or the producer's own `release.json.signature` is still `null`
(the fleet-default today, and always a soft-fail — sync proceeds either way);
`true` when a pinned `pubkey` verified the producer's `SHA256SUMS.minisig`
sidecar; `false` when a pinned pubkey's verification **failed** (a bad or
tampered signature) — also soft-fail (the sha256 integrity floor above already
guarantees byte integrity; a bad signature is a provenance-layer problem the
conformance gate surfaces, not a placement-blocking one). See §Signing below.

The lock is JSON so the verifier and the conformance gate (§5) stay pure-stdlib
on the read/gate path — no TOML parse needed to *read* the lock. The write-side
tools (`grm-sync-deps`, `grm-vendor-migrate`) own the TOML floor. Writes are
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
   degrade** (exit 2), never silent trust. When a `pubkey` is pinned, the
   `SHA256SUMS.minisig` sidecar (if the producer shipped one) is additionally
   verified pure-Python (`minisign_verify.py`, no `minisign` binary needed) — see
   §Signing below; a signature outcome never blocks placement (soft-fail).
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
| `--verify` | provenance integrity check (LOCAL-FORK / DEAD-VENDOR / VERSION-CONTRADICTION / STUB-VENDOR-MANIFEST) — see below | none |
| `--self-test` | deterministic, offline-fixture-based regression run (covers `sync_deps` + `vendor_verify`) | none |

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
engine and then **open a PR via the `grm-github-pr` path** rather than committing
directly to a protected branch — the engine itself only writes files.

## Coverage

`--self-test` (deterministic, stdlib-only, offline-fixture-based) exercises:
sync + correct two-hash lock; byte-identical re-sync no-op; `--check` clean →
drift (nonzero, writes nothing); `--offline` clean validation with zero network;
**tampered artifact hard-refused with `vendor/<dep>/` left untouched and no lock
entry written**; absent `SHA256SUMS` loud-degrade; `--update` pin rewrite +
re-vendor; `vendored-crate` strip + extract allowlist; asset-name allowlist and
tar-traversal refusals. `vendor_verify.py --self-test` additionally exercises:
`LOCAL-FORK` on a drifted tree (with a contradicted `VENDOR.md` claim and a
diff summary naming the new file); `DEAD-VENDOR` on both an empty declared
`dest` and an uninitialized git submodule; `VERSION-CONTRADICTION` on a
`Cargo.toml` version disagreeing with the pin; `STUB-VENDOR-MANIFEST` firing
WARN-only on an all-stub `vendor.toml` with dep references elsewhere; and zero
false positives on a healthy consumer / absent `vendor.toml`.

Design: `docs/design/dependency-channel-design.md` §3–§4.

## Reference (load on demand)

- `Signing — artifact provenance` — see `reference.md`
- `Provenance verification — --verify` — see `reference.md`
