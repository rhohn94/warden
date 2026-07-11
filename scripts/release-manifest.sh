# release-manifest.sh — warden's knobs for scripts/release.sh (sourced as shell vars).
# Contract: scripts/release.sh header (RELEASE_* vars, highest-precedence CLI flags).
RELEASE_CHANGELOG="docs/version-history.md"
RELEASE_BRANCH="main"
# Cargo.lock is NOT listed: cargo rewrites it during the verify build and the
# ceremony stages it alongside the bump commit (see scripts/release.sh step 6).
RELEASE_VERSION_FILES="Cargo.toml"
RELEASE_TEST_CMD="cargo test"
RELEASE_BUILD_CMD="cargo build --release"
# Assemble the GitHub Release asset trio (tarball + release.json + SHA256SUMS)
# into dist/ — consumed post-push by publish_release.py (grm-project-release).
RELEASE_PACKAGE_CMD="just package"
