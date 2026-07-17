# lib-app

A real, working reusable library: one exemplar doc-tested module
(`version_compare`) with a real public API, unit tests, an integration test
that exercises the crate's public surface only, and runnable doctests — plus
the crate-producer `package`/`release` recipes from the release-automation
starter pack (#431), delegating to the already-shipped
`build_crate_artifact.py` (Dependency Channel producer contract).

This is a Grimoire quick-start scaffold for the `lib` profile. Replace this
README, the crate name (`Cargo.toml` + `publish.toml`, kept in sync), and the
`version_compare` module (`src/version_compare.rs`) with your project's real
public API — keep the doc-tested-example convention as you grow it. Run
`just --list` for every recipe; `just test` to run unit + integration tests +
doctests; `just package` to build a vendorable crate tarball in `dist/`.

See `template.json`'s `post-apply-notes` for exactly what runs out of the box
vs. what is a seam to fill in.
