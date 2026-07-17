# package-manifest.sh — PACKAGE_* contract for scripts/package.sh (sourced as
# shell vars; see that script's header for the full contract). Pre-filled so
# `just package` / `just release` work out of the box with zero hand-written
# release code — edit PACKAGE_NAME/PACKAGE_BINARY if you rename the crate.

PACKAGE_NAME="service-app"
PACKAGE_BINARY="target/release/service-app"
PACKAGE_BUILD_CMD="just build env=prod"
# Derive the version from Cargo.toml's [package] version (the same value
# `just release`'s multi-stack bump keeps in sync) so `just package` works
# standalone too, without requiring an explicit --version.
PACKAGE_VERSION_CMD='sed -n "s/^version *= *\"\([^\"]*\)\".*/\1/p" Cargo.toml | head -1'
