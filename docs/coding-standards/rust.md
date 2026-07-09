# Rust Standards

Per-technology coding standards for Rust. Read alongside the cross-language
[standard practices](../coding-standards.md).

## Style & formatting

- Run `rustfmt` on every save (or in CI); the formatter is authoritative — do
  not fight it with manual overrides unless a `#[rustfmt::skip]` is genuinely
  justified and commented.
- Follow Rust naming conventions exactly: `snake_case` for functions, methods,
  variables, and modules; `CamelCase` for types and traits; `SCREAMING_SNAKE_CASE`
  for constants and statics.
- Keep lines within 100 characters (the `rustfmt` default); prefer shorter.
- Use trailing commas in multi-line enum variants, struct literals, and function
  argument lists so diffs stay clean.

## Linting

- Run `cargo clippy -- -D warnings` in CI; fix every warning before merging.
- Enable `#![deny(missing_docs)]` on library crates — every public item must
  carry a doc-comment.
- Common clippy groups to enable project-wide:
  `clippy::pedantic`, `clippy::unwrap_used`, `clippy::panic`.
  Suppress individual lints with `#[allow(...)]` plus a brief comment explaining
  why, never blanket-suppress a whole group.

## Error handling

- Prefer `Result<T, E>` over panicking for recoverable errors; never use
  `.unwrap()` or `.expect()` in library code.
- In application (binary) code, `.expect("…")` is acceptable at startup only,
  where failure truly is unrecoverable; document the invariant in the message.
- Use the `thiserror` crate to define typed, derive-based error enums for
  libraries; use `anyhow` in application entry points for ergonomic context
  wrapping.
- Propagate errors with `?` — never manually `match`/`unwrap` where `?` suffices.

## Testing

- Framework: Rust's built-in `#[test]` / `#[cfg(test)]` (`cargo test`).
- Unit tests live in a `#[cfg(test)] mod tests { … }` block at the bottom of
  the same file as the code under test.
- Integration tests go in `tests/` at the crate root; each file covers one
  integration boundary.
- Property-based testing with `proptest` or `quickcheck` is encouraged for
  functions with non-trivial input spaces.
- Name tests descriptively: `given_empty_input_returns_error`, not `test1`.

## Module & package structure

- One logical concern per module; split large modules into a directory with
  `mod.rs` (or the `module_name.rs` + `module_name/` pattern for Rust 2018+).
- Re-export the public API surface from the crate root (`lib.rs`) so consumers
  never need to know internal module paths.
- Keep `main.rs` thin — wire up dependencies and hand off to library code
  immediately.
- Prefer workspace layout (`[workspace]` in root `Cargo.toml`) for multi-crate
  repos; avoid deep nesting of crates.

## Dependency hygiene

- Pin crates to a minor version (`^x.y`) in `Cargo.toml`; `Cargo.lock` is
  committed for binaries, omitted for libraries (standard convention).
- Audit new dependencies with `cargo deny` (license, security, duplicates) before
  merging.
- Prefer `std` or well-maintained ecosystem crates (tokio, serde, rayon) over
  niche alternatives; document the reason for any non-obvious choice in a comment
  next to the dependency.
- Prune unused dependencies (`cargo machete`) regularly.

## Quality enforcement (the `lint` recipe)

Rust projects drive quality through the recipe `lint` target so every Grimoire
consumer invokes one stable name. The canonical command set, cheap-to-expensive:

| Dimension | Command | Gate |
|---|---|---|
| Format | `cargo fmt --all -- --check` | hard fail on unformatted code |
| Lint | `cargo clippy --all-targets --all-features -- -D warnings` | hard fail on any warning |
| Unused deps | `cargo machete` | warn on a dead dependency |
| Complexity | `cargo clippy -- -W clippy::cognitive_complexity` | warn over threshold |

Run format and lint as hard gates; collect unused-deps and complexity as
warn-level findings. Keep individual functions under ~50 lines and modules under
~400; split before they grow past that. Design:
`../design/rust-quality-enforcement-design.md`.

## Telemetry hooks (where they integrate)

See `../coding-standards.md` §Telemetry for the project-type surface. In Rust,
use the `tracing` ecosystem behind one telemetry init function:
- **Errors:** `std::panic::set_hook` to emit a fatal-error event on panic;
  return-and-log `Result` errors at the boundary (don't instrument deep in the
  domain).
- **Startup:** emit a start span/event from `main` with the crate version.
- **API/service:** `tracing` spans per request with latency fields; an
  HTTP-client middleware layer for downstream-call traces.
- **CLI:** record the subcommand, flags, and exit code at the top-level handler.


## Audit hints

<!-- audit: id="rs-no-unwrap" check="no .unwrap()/.expect() on fallible paths in library code; propagate with ?" severity="warn" applies="rust" -->
<!-- audit: id="rs-error-enum" check="library errors modeled as an enum/thiserror, not String" severity="info" applies="rust" -->
<!-- audit: id="rs-clippy-clean" check="clippy --all-targets --all-features -- -D warnings passes with no warnings" severity="warn" applies="rust" -->
<!-- audit: id="rs-pin-deps" check="Cargo.lock committed for binaries" severity="info" applies="rust" -->
<!-- audit: id="rs-rustfmt-check" check="cargo fmt --all -- --check is clean; no manual formatting fights without a commented #[rustfmt::skip]" severity="warn" applies="rust" -->
<!-- audit: id="rs-no-blanket-allow" check="no blanket #[allow(...)] of a whole lint group; per-lint allows carry a why-comment" severity="warn" applies="rust" -->
<!-- audit: id="rs-fn-length" check="functions stay under ~50 lines; long fns split into helpers" severity="info" applies="rust" -->
<!-- audit: id="rs-module-size" check="modules stay under ~400 lines; large modules split into a directory" severity="info" applies="rust" -->
<!-- audit: id="rs-cognitive-complexity" check="no function trips clippy::cognitive_complexity over threshold" severity="warn" applies="rust" -->
<!-- audit: id="rs-root-reexport" check="public API re-exported from lib.rs; consumers never import internal module paths" severity="info" applies="rust" -->
<!-- audit: id="rs-thin-main" check="main.rs is thin — wires deps and hands off to library code immediately" severity="info" applies="rust" -->
<!-- audit: id="rs-unused-deps" check="cargo machete reports no unused dependencies" severity="warn" applies="rust" -->
<!-- audit: id="rs-no-anyhow-in-lib" check="anyhow confined to binary entry points; libraries expose typed errors" severity="info" applies="rust" -->
