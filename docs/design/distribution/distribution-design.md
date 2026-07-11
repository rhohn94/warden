# Distribution

> **Up:** [↑ Distribution](README.md)

## Motivation
Every warden GitHub Release through v1.3.3 shipped with **zero assets** — the
tag existed, the Release page existed, but nothing installable was attached
(the fleet-wide "silent publish" failure class, grimoire#286). This doc pins
warden's distribution model so a tagged commit always yields a verifiable,
downloadable artifact set.

## Scope
Build artifacts, the release ceremony entry point, versioning, and the GitHub
Release delivery transport. The local-install path (`just deploy` into
`~/Projects/deployed-apps/warden/`) is unchanged and out of scope here — it
remains warden's day-to-day deployment convention alongside published releases.

## Design

### Artifact set (the conformant trio, grimoire#286)
`just package` → [`scripts/build_dist.py`](../../../scripts/build_dist.py)
assembles into gitignored `dist/`:

| Asset | Contents |
|---|---|
| `warden-v{X.Y.Z}-{target}.tar.gz` | release binary + `README.md` (primary artifact) |
| `release.json` | kind-discriminated manifest (`schema_version: 1`, `artifact_kind: binary`, primary-artifact sha256, asset list) |
| `SHA256SUMS` | coreutils-format digests of both files above |

The tarball is byte-reproducible for a given commit (entry mtimes pinned to
the commit timestamp, owner ids zeroed, gzip mtime zeroed). `{target}` is the
host platform label (e.g. `macos-arm64`); warden currently ships the single
platform it is developed and deployed on.

### Release ceremony
`just release` → [`scripts/release.sh`](../../../scripts/release.sh) (the
Grimoire changelog-derived ceremony, adapted for warden via
[`scripts/release-manifest.sh`](../../../scripts/release-manifest.sh)):

1. Derive the version from the newest `docs/version-history.md` heading.
2. Guards: on `main`, clean tree, tag absent, changelog entry present.
3. Bump `Cargo.toml`; `cargo test`; `cargo build --release`; `just package`.
4. Commit the bump (staging `Cargo.lock`, which cargo rewrites during the
   verify build — warden's one adaptation to the reference script) and create
   the annotated tag `v{X.Y.Z}`. **Never pushes.**

### Publish + assert (post-push)
After the human/Noir-gated push, the shared asserted publisher
`.claude/skills/grm-project-release/publish_release.py` publishes `dist/*` to
the GitHub Release for the tag and **re-fetches to assert** every asset in
`SHA256SUMS` landed with matching digests — the loud gate that kills the
silent-publish class. Its `--check` mode is the skipped-publish gate (newest
tag must carry the trio, or its changelog section must be annotated
`<!-- release: notes-only -->`).

### Versioning & channels
Three-part `v{MAJOR}.{MINOR}.{PATCH}` tags on `main`, versions sourced from
`docs/version-history.md` (newest-first). Channel is `stable`; the builder
accepts `--channel beta` should a staging-branch prerelease ever be needed.

## Acceptance
- `just release` on `main` produces a tagged commit and a `dist/` trio;
  re-running for the same version is refused (tag-exists guard).
- `publish_release.py` after the push leaves a GitHub Release whose assets
  match `SHA256SUMS` byte-for-byte; a missing asset fails the run loudly.
- `scripts/build_dist.py --self-test` and `scripts/release.sh --self-test`
  pass offline.

## Open questions
- Cross-platform artifacts (linux/windows) if warden ever runs off this Mac —
  would need CI runners; single-host builds cover current usage.

## Follow-ups
- Wire `publish_release.py --check` into a pre-release gate once the fleet's
  shared-publisher conformance checker recognizes warden's output.
