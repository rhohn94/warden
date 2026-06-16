# Version design

## §1 Version scheme

Warden uses **semantic versioning** (`MAJOR.MINOR.PATCH`).

- MAJOR: breaking changes to the binary's CLI interface or behavior
- MINOR: new features, backward-compatible
- PATCH: bug fixes

## §2 Version file

**File:** `Cargo.toml`
**Field:** `[package] version = "X.Y.Z"`

The authoritative version is the `version` field in `[package]` of `Cargo.toml`.

## §3 Version file location and format

- Path: `Cargo.toml`
- Field name or format: `[package] version`

## §4 Release procedure

1. Ensure `docs/version-history.md` has an entry for the new version on `dev`.
2. Merge `dev` → `main`.
3. On `main`, bump `Cargo.toml` `[package] version`.
4. Run `cargo build --release`.
5. Tag the commit `v{MAJOR}.{MINOR}.{PATCH}`.

## §5 Tag convention

`v0.1.0`, `v0.2.0`, etc.
