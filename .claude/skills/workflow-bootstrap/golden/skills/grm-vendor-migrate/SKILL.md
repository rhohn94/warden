---
name: vendor-migrate
description: One-shot migration of an existing git submodule (or hand-vendored dir) into the Dependency Channel consumer artifacts — vendor.toml + vendor.lock. Reads the submodule gitlink + .gitmodules URL, derives the GitHub owner/repo slug, fetches each published release on the channel, and matches the one whose extracted bytes equal the present tree (tree_sha256) — preferring the pinned tag. On a match it writes vendor.toml and reconciles through the real sync-deps engine so sync-deps --check then validates clean. When NO published release matches it emits a LOUD fallback — records the resolved commit + a content sha256 and writes a commented stub, never silently pinning to a moving ref. Re-run never clobbers a hand-edited vendor.toml (--force overrides). Triggers on "migrate a submodule", "convert submodule to vendor.toml", "vendor-migrate", "migrate vendored dir to the dependency channel", "turn a git submodule into a vendored dep".
---

# vendor-migrate

The **Dependency Channel** migration helper (design
`docs/design/dependency-channel-design.md` §7). It converts a legacy
**git submodule** (or a hand-vendored dir) into the committed Dependency Channel
artifacts — `vendor.toml` (intent) + `vendor.lock` (resolved truth) — so the
dependency is thereafter sourced from a published GitHub Release **channel**
instead of a moving submodule pointer.

> **Preferred interface — the `vendor_migrate.py` script.** The whole
> read-gitlink → derive-slug → resolve-release → content-match → emit loop is a
> deterministic stdlib engine that **reuses the DEP-CH-2 `sync-deps` engine**;
> don't re-derive it in prose. Call the script and interpret its exit code:
>
> ```bash
> python3 .claude/skills/vendor-migrate/vendor_migrate.py \
>     --name aura --path vendor/aura            # migrate one submodule
> python3 .claude/skills/vendor-migrate/vendor_migrate.py \
>     --name aura --path vendor/aura --channel stable --strip-components 1
> python3 .claude/skills/vendor-migrate/vendor_migrate.py \
>     --name aura --path vendor/aura --force    # overwrite a prior [deps.aura]
> python3 .claude/skills/vendor-migrate/vendor_migrate.py --self-test
> ```
>
> Exit codes mirror the `sync-deps` 0/1/2 contract: **0** = migrated (a release
> matched; `vendor.toml` + `vendor.lock` written), **1** = hard error (no
> `.gitmodules` URL, undrivable slug, clobber refused), **2** = **loud
> fallback** (no published release matched — a commented stub was written, the
> commit + content-sha recorded, and **nothing pinned to a moving ref**).

## What it produces (design §3)

On a successful match it writes exactly the DEP-CH-2 contract:

- **`vendor.toml`** — an active `[deps.<name>]` block (`repo`, `channel`,
  `version`, `artifact`, `dest`, `kind`, optional `strip_components` / `extract`).
- **`vendor.lock`** — the JSON lock, **written by the real `SyncDepsEngine`** so
  it is byte-for-byte what `sync-deps --check` then recomputes (two-hash model:
  `artifact_sha256` + `tree_sha256`).

## Resolution algorithm (design §7)

1. **Read the pin.** The submodule's gitlink (index mode `160000`) gives the
   pinned commit; `.gitmodules` gives the remote URL.
2. **Derive the slug.** `owner/repo` is derived from the URL with the **same**
   normalization `sync-deps` uses (`GhReleaseFetcher._slug`) — https or `git@`
   SSH forms both resolve — so the migrated `repo` field matches what the sync
   engine later resolves.
3. **Content-match a release.** For each published release on the channel
   (preferring the tag the commit maps to, then newest-first), fetch
   `release.json` + `SHA256SUMS` + the artifact, **verify the sha256 before any
   placement**, extract exactly as `sync-deps` would (same `ArtifactKind` /
   `strip_components` / `extract`), and compare the placed-tree `tree_sha256`
   against the present bytes. The **content hash is authoritative** — a tag is
   only a probe-order preference.
4. **Emit.** On a match, write `vendor.toml` and **reconcile via the real
   engine** (`SyncDepsEngine.sync(only=…)`) so the lock matches the bytes.

## Loud fallback (never silent — design §7)

When **no published release matches** the present bytes, the tool:

- records the **resolved commit** + a **content sha256** (`tree_sha256` over the
  present bytes — a fixed content hash, **not** a moving ref);
- writes a **fully-commented** `[deps.<name>]` stub to `vendor.toml` carrying the
  commit + content-sha so a human can complete the pin (it parses to **zero
  active deps** — nothing is silently pinned);
- writes **no `vendor.lock`** (there is no published release to lock to);
- prints a **LOUD banner** to stderr and exits **2**.

This is the design's hard rule: *never silently pin to a moving ref.* Publish a
matching release (or correct the channel), fill the version/artifact, then run
`sync-deps` to lock it.

## Re-run safety (no-silent-clobber)

A re-run **refuses to overwrite** a `vendor.toml` that already declares
`[deps.<name>]` (the `design-language-adapt` no-silent-clobber rule) — pass
`--force` to overwrite deliberately. The single TOML merge is idempotent and
comment-preserving: it replaces only the named block (active or commented
fallback) and leaves the rest of the file — schema header, other deps,
comments — untouched.

## Reuse (design §11)

This helper **composes** the DEP-CH-2 engine rather than re-deriving it — it
imports `tree_sha256`, `VendorLock`, `DepSpec`, `make_kind`, `Verifier`,
`OfflineFetcher` / `GhReleaseFetcher`, `SyncDepsEngine`, the exit-code contract,
and the deterministic fixture seeders from `sync_deps_engine`. The only new
surface is git introspection (gitlink + `.gitmodules`), slug derivation, the
content-match loop, and the idempotent `vendor.toml` block merge.

## Coverage

`--self-test` (deterministic, stdlib-only, **offline** via `OfflineFetcher`)
seeds a **synthetic submodule fixture** — a `.gitmodules` + a stubbed gitlink in
a temp repo + the checked-out bytes + a matching offline release — and asserts:
a matching release migrates and **`sync-deps --check` validates the round-trip
clean**; the derived slug / channel / `dest` invariant; a re-run refuses to
clobber (and `--force` overrides); the **no-match path emits the loud fallback**
(records commit + content-sha, writes no lock, the stub declares no active dep);
https + `git@` slug derivation; a missing `.gitmodules` URL is a hard error.

Design: `docs/design/dependency-channel-design.md` §7 (+ §3 for the produced shapes).
