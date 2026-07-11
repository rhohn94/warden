# Grm-sync-deps — reference
Loaded on demand by `SKILL.md`.

## Signing — artifact provenance

The release-trio spec (`dependency-channel-design.md` §2) always emits
`SHA256SUMS` (the integrity floor) and, when a producer has minisign configured,
a detached `SHA256SUMS.minisig` sidecar plus a `release.json.signature`
*declaration* (`{"algo": "minisign", "file": "SHA256SUMS.minisig"}`). This is an
**additive provenance layer** on top of the always-mandatory sha256 check — it
answers "did this specific key sign these bytes," not "are the bytes intact."

**Opt in per dependency** by pinning the producer's minisign public key in
`vendor.toml`:

```toml
[deps.aura]
repo = "rhohn94/design-language"
channel = "stable"
version = "3.20.0"
artifact = "aura-v3.20.0.tar.gz"
dest = "vendor/aura"
kind = "asset-bundle"
pubkey = "untrusted comment: minisign public key ...\nRWQ...=="   # optional
```

No `pubkey` => the dep stays exactly as before (`signature_verified: "unsigned"`,
nothing changes). This is deliberate: signing is a **soft, producer-by-producer
migration** — the fleet does not need every producer to sign before any consumer
can start pinning, and no existing `vendor.toml` needs an edit to keep working.

**Verification is pure-Python, stdlib-only** — `minisign_verify.py` re-implements
the Ed25519 verify equation (RFC 8032) and the minisign wire format directly
(base64 + `hashlib.sha512`), so `grm-sync-deps` never needs the `minisign`
binary installed to *verify* a signature (producers still use the real
`minisign` CLI to *sign*, unchanged in `build_distributables.py`). This keeps
the consumer's runtime dependency surface at zero non-stdlib packages.

**Outcomes recorded in `vendor.lock`** (`signature_verified`, tri-state):

| Pubkey pinned? | Producer signed? | Verifies? | `signature_verified` | Blocks sync? |
|---|---|---|---|---|
| no | — | — | `"unsigned"` | no |
| yes | no (`signature: null`) | — | `"unsigned"` | no |
| yes | yes | yes | `true` | no |
| yes | yes | **no** (bad/tampered sig) | `false` | **no** (soft-fail — sha256 already holds) |

A `false` or `"unsigned"` outcome is never a hard refuse — only a sha256
mismatch (the integrity floor) hard-refuses. A failed/absent signature is
surfaced by the **conformance gate** (`grm-dependency-audit`'s
`dependency_channel_conformance.py`, `recipe.py vendor-check`): a dep with a
pinned `pubkey` whose `signature_verified` is not `true` raises a WARN-severity
`unsigned-dependency` finding — advisory only, same posture as every other
Dependency Channel gate finding this release.

**Key distribution.** One fleet key to start: the framework holds the minisign
secret key (`MINISIGN_SECRET_KEY` env, read only by `build_distributables.py`
at release time — never committed), and its public counterpart is pinned
per-dep, explicitly, in each consumer's `vendor.toml` (no discovery magic, no
well-known-URL fetch). **Rotation:** publish a release signed with the new key,
update the `pubkey` pin in each consumer's `vendor.toml` in the same PR that
bumps the dep version (the pin and the key move together — a stale pin simply
reverts to `signature_verified: "unsigned"` against the new key's releases
until updated, never a hard failure). There is no automatic key-rollover
protocol; a compromised key is retired by cutting a new keypair and updating
pins fleet-wide, tracked the same way any other pin bump is.

**Not implemented (later work, per the design's Follow-ups):** provisioning the
fleet's actual signing keypair as a CI secret is an operational step, not
framework code; escalating the conformance gate's `unsigned-dependency` finding
to `block` is a future per-repo dial flip; the token-bookkeeper ↔ mission-control
cross-repo signed round-trip pilot is out of scope for this framework-side
change (tracked as a follow-up, not blocking this feature).

## Provenance verification — `--verify` (#315)

A distinct, read-only pass — `vendor_verify.py` — checks whether the
provenance metadata a consumer *trusts* (its `vendor.toml` pin, `vendor.lock`
resolved truth, an optional `VENDOR.md` front-matter claim) actually matches
the bytes on disk, rather than trusting it blindly. Zero network calls —
everything is a local filesystem comparison, so it is fully self-test-able
offline. Four finding classes, normalized to
`{check, dep, severity, detail, locked, observed}`:

| `check` | Severity | Flags |
|---|---|---|
| `LOCAL-FORK` | error | the vendored tree's recomputed `tree_sha256` disagrees with the `vendor.lock` pin — reported with a per-file added/removed/changed diff summary; a `VENDOR.md` front-matter `claim:` is cross-checked and folded into the finding when it contradicts the observed drift |
| `DEAD-VENDOR` | error | a `vendor.toml` dep's declared `dest` is missing or holds zero regular files, **or** a `.gitmodules`-declared git submodule is uninitialized/empty on disk |
| `VERSION-CONTRADICTION` | error | a version string embedded in the vendored tree itself (`Cargo.toml` `[package] version`, `package.json` `"version"`, or `VENDOR.md`'s `pinned_version:` front matter) disagrees with the `vendor.toml` pin |
| `STUB-VENDOR-MANIFEST` | **warn-only** | heuristic: `vendor.toml` is absent or declares zero deps, yet the repo's own source/docs reference vendoring (`vendor/`, `vendor.toml`, `lib/third-party/`) elsewhere — the "inert manifest" smell. Never fails the run on its own |

```bash
python3 .claude/skills/grm-sync-deps/sync_deps.py --verify              # human-readable report
python3 .claude/skills/grm-sync-deps/sync_deps.py --verify --json       # machine JSON
python3 .claude/skills/grm-sync-deps/sync_deps.py --verify --dep aura   # one dep only
python3 .claude/skills/grm-sync-deps/vendor_verify.py --self-test       # offline self-test (also run via sync_deps.py --self-test)
```

Exit 0 = clean (zero `error`-severity findings — `STUB-VENDOR-MANIFEST` alone
never elevates the exit code); nonzero = at least one `LOCAL-FORK` /
`DEAD-VENDOR` / `VERSION-CONTRADICTION` finding. Surfaced alongside the
conformance pass in `grm-dependency-audit`'s `vendor-check` verb and wired into
the release gate next to `grm-doc-assurance` (`docs/grimoire/integration-workflow.md`
§Release gate). Design: `docs/grimoire/design/dependency-channel-design.md`
§Provenance verification.

