#!/usr/bin/env bash
# package.sh — assemble a versioned, deployable release bundle (Grimoire #201 Phase 1).
#
# Reference implementation of the `package` build-recipe target
# (web-app-deployment-protocol.md §1 the deployable bundle, §2 release.json,
# §8 grimoire-build-info.json). Called by `just package` (which the recipes.json
# `package` target invokes, so `recipe.py package` ≡ `just package`).
#
# It resolves VERSION + TRIPLE, builds, stages a versioned dist dir
# `dist/<name>-v{VER}-{TRIPLE}/`, copies the binary + assets into it, emits
# `grimoire-build-info.json` (the §8 provenance stamp) + `release.json` (the §2
# manifest) + `SHA256SUMS` (integrity floor), and tars the stage
# DETERMINISTICALLY (zeroed mtime/uid/gid, sorted entries) — the same tar-
# ENCODING contract `build_distributables.py` uses (mtime=0, uid/gid=0,
# uname/gname="", sorted), so two runs over IDENTICAL staged bytes produce a
# byte-identical archive. NOTE: the staged bytes themselves are not invocation-
# invariant — `grimoire-build-info.json`'s `build-timestamp` field (§8, "the
# moment the package recipe ran") intentionally differs run-to-run by design,
# so the overall bundle is reproducible in STRUCTURE, not in content, across
# separate invocations at different times. The self-test's cross-run tarball
# comparison is therefore an informational note, never a hard failure.
#
# PARAMETERIZED, not hardcoded to one project. The app name / binary name / asset
# globs / build command come from (highest wins):
#   1. CLI flags (--name / --binary / --version / --triple / --build-cmd / --asset)
#   2. a small manifest file (default scripts/package-manifest.sh, sourced as
#      shell vars) — see the PACKAGE_* contract below.
#   3. built-in fallbacks (derived from the repo dir name / `uname`).
# The optional macOS codesign/notarize and migrations-dir hooks from #201 §2.1 are
# env-gated and default-skip (a non-macOS or unconfigured build never runs them).
#
# Self-test: `scripts/package.sh --self-test` runs an offline temp-dir round trip
# (stage a synthetic tree, emit release.json + SHA256SUMS, tar, and assert the
# archive is byte-identical across two runs). No repo bash --self-test convention
# exists (the .sh hooks are python polyglots), so a temp-dir round trip is used.
set -euo pipefail

# ── manifest contract (PACKAGE_* shell vars a project may set) ────────────────
#   PACKAGE_NAME        app/bundle name (default: repo dir basename)
#   PACKAGE_BINARY      built binary/entrypoint path, relative to repo root
#   PACKAGE_BUILD_CMD   command that produces PACKAGE_BINARY (default: `just build env=prod`)
#   PACKAGE_ASSETS      newline- or space-separated globs of extra files to bundle
#                       (static dir, config template, service template, install.sh, …)
#   PACKAGE_VERSION_CMD command whose stdout is the version (overrides auto-detect)
#   PACKAGE_MIN_DATA_SCHEMA  integer forward-compat gate for release.json (default 1)
# Optional hooks (env, default-skip):
#   CODESIGN_IDENTITY   when set on macOS, codesign the binary
#   NOTARIZE            when "1"/"true" on macOS, run the notarize hook (stub)
#   MIGRATIONS_DIR      when set, copy this dir into the bundle (DB-bearing apps)
DEFAULT_MANIFEST="scripts/package-manifest.sh"

die() { echo "package.sh: $*" >&2; exit 1; }
note() { echo "package.sh: $*" >&2; }

# Resolve the target triple. A project may override via --triple / PACKAGE_TRIPLE;
# otherwise derive a best-effort <arch>-<os> from uname (loud, never guessed silently).
resolve_triple() {
    if [ -n "${PACKAGE_TRIPLE:-}" ]; then printf '%s' "$PACKAGE_TRIPLE"; return; fi
    # Prefer rustc's host triple when a Rust toolchain is present (the canonical
    # familiar/goon-cave source of truth); else fall back to uname.
    if command -v rustc >/dev/null 2>&1; then
        rustc -vV 2>/dev/null | awk -F': ' '/^host:/{print $2; found=1} END{exit !found}' && return
    fi
    local arch os
    arch="$(uname -m)"; os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    printf '%s-%s' "$arch" "$os"
}

# sha256 of a file, portable across GNU coreutils (sha256sum) and macOS (shasum).
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
    else die "no sha256sum/shasum available to compute checksums"; fi
}

# Deterministic tar of a staged directory. Detects GNU vs BSD tar and passes the
# right determinism flags; entries are fed in SORTED order from a manifest list so
# ordering is stable regardless of the tar implementation's own traversal.
deterministic_tar() {
    local stage_dir="$1" out_tar="$2" list
    list="$(cd "$stage_dir" && find . -type f | LC_ALL=C sort | sed 's|^\./||')"
    if tar --version 2>/dev/null | grep -qi 'gnu tar'; then
        # GNU tar: fully reproducible.
        printf '%s\n' "$list" | tar -C "$stage_dir" \
            --owner=0 --group=0 --numeric-owner --mtime='@0' \
            --format=gnu -T - -cf - | gzip -n > "$out_tar"
    else
        # BSD tar (libarchive): supports uid/gid/uname/gname + mtime clamping.
        printf '%s\n' "$list" | tar -C "$stage_dir" \
            --uid 0 --gid 0 --uname '' --gname '' \
            --options gzip:compression-level=9 \
            $(bsd_mtime_flag) -T - -czf "$out_tar" 2>/dev/null \
        || {
            note "WARNING: deterministic tar flags unavailable on this tar; the"
            note "         archive is still valid but NOT byte-reproducible. Install"
            note "         GNU tar (gtar) for byte-identical rebuilds."
            printf '%s\n' "$list" | tar -C "$stage_dir" -T - -czf "$out_tar"
        }
    fi
}

# BSD tar mtime flag (newer libarchive supports --mtime; older does not).
bsd_mtime_flag() {
    if tar --help 2>&1 | grep -q -- '--mtime'; then echo "--mtime 1970-01-01T00:00:00Z"; fi
}

# Write release.json (web-app-deployment-protocol.md §2). The bundle is an
# app-binary artifact: version, target_triple, binary_sha256, min_data_schema,
# assets[] of {name, sha256, bytes}. Deterministic (sorted keys via python3).
write_release_json() {
    local stage_dir="$1" version="$2" triple="$3" binary_name="$4" min_schema="$5"
    python3 - "$stage_dir" "$version" "$triple" "$binary_name" "$min_schema" <<'PY'
import hashlib, json, os, sys
stage, version, triple, binary_name, min_schema = sys.argv[1:6]
def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
assets = []
binary_sha256 = ""
for root, _, files in os.walk(stage):
    for f in sorted(files):
        if f in ("release.json", "SHA256SUMS"):
            continue  # manifests are not self-referential assets
        p = os.path.join(root, f)
        rel = os.path.relpath(p, stage)
        digest = sha256(p)
        assets.append({"name": rel, "sha256": digest, "bytes": os.path.getsize(p)})
        if rel == binary_name:
            binary_sha256 = digest
assets.sort(key=lambda e: e["name"])
manifest = {
    "version": version,
    "target_triple": triple,
    "binary_sha256": binary_sha256,
    "min_data_schema": int(min_schema),
    "assets": assets,
}
with open(os.path.join(stage, "release.json"), "w") as fh:
    fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY
}

# Write grimoire-build-info.json (docs/web-app-deployment-protocol.md §8) — the
# Grimoire PROVENANCE stamp, a SIBLING of release.json (deployment/integrity
# manifest), not a merge into it. Snapshots framework-version + a verbatim
# .claude/grimoire-config.json, a UTC build timestamp, and the source VCS ref
# (tag@sha when HEAD is exactly tagged, else the bare sha). `changelog` (§8,
# optional) is a build-time snapshot of the changelog file's raw text — included
# only when that file is resolvable, NEVER fabricated, so the Admin Console /
# changelog surface renders an honest empty state when it is absent. Called
# BEFORE write_release_json so the stamp is picked up by its assets[] walk (and
# is therefore covered by SHA256SUMS like any other bundled file, per §8).
write_build_info_json() {
    local stage_dir="$1"
    local config="${PACKAGE_GRIMOIRE_CONFIG:-.claude/grimoire-config.json}"
    local changelog="${PACKAGE_CHANGELOG:-docs/changelog.md}"
    local source_ref
    source_ref="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    if git describe --tags --exact-match HEAD >/dev/null 2>&1; then
        source_ref="$(git describe --tags --exact-match HEAD 2>/dev/null)@${source_ref}"
    fi
    python3 - "$stage_dir" "$config" "$changelog" "$source_ref" <<'PY'
import datetime, json, sys
stage, config_path, changelog_path, source_ref = sys.argv[1:5]

framework_version = None
grimoire_config = {}
try:
    with open(config_path, encoding="utf-8") as fh:
        grimoire_config = json.load(fh)
    framework_version = grimoire_config.get("framework-version")
except (OSError, ValueError):
    pass  # missing/malformed config degrades only this field — never fatal

info = {
    "framework-version": framework_version,
    "grimoire-config": grimoire_config,
    "build-timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source-ref": source_ref,
}
try:
    with open(changelog_path, encoding="utf-8") as fh:
        info["changelog"] = fh.read()
except OSError:
    pass  # optional per §8 — omitted (not fabricated) when unresolvable

with open(stage + "/grimoire-build-info.json", "w") as fh:
    fh.write(json.dumps(info, indent=2, sort_keys=True) + "\n")
PY
}

# Write SHA256SUMS over every bundled file except itself, canonical `<hex>  <name>`
# form (sorted) so `sha256sum -c` verifies it from inside the stage dir.
write_checksums() {
    local stage_dir="$1"
    ( cd "$stage_dir"
      find . -type f ! -name SHA256SUMS | LC_ALL=C sort | sed 's|^\./||' | while read -r f; do
          printf '%s  %s\n' "$(sha256_of "$f")" "$f"
      done > SHA256SUMS )
}

# ── the package flow ─────────────────────────────────────────────────────────
run_package() {
    local manifest="$DEFAULT_MANIFEST"
    local cli_name="" cli_binary="" cli_version="" cli_triple="" cli_build_cmd=""
    local -a cli_assets=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --name) cli_name="$2"; shift 2;;
            --binary) cli_binary="$2"; shift 2;;
            --version) cli_version="$2"; shift 2;;
            --triple|--target) cli_triple="$2"; shift 2;;
            --build-cmd) cli_build_cmd="$2"; shift 2;;
            --asset) cli_assets+=("$2"); shift 2;;
            --manifest) manifest="$2"; shift 2;;
            *) die "unknown argument: $1 (see the header for usage)";;
        esac
    done

    # Source the manifest if present (sets PACKAGE_* vars); absence is fine.
    if [ -f "$manifest" ]; then
        # shellcheck disable=SC1090
        . "$manifest"
        note "loaded manifest $manifest"
    fi

    local name binary version triple build_cmd min_schema
    name="${cli_name:-${PACKAGE_NAME:-$(basename "$(pwd)")}}"
    binary="${cli_binary:-${PACKAGE_BINARY:-}}"
    build_cmd="${cli_build_cmd:-${PACKAGE_BUILD_CMD:-just build env=prod}}"
    min_schema="${PACKAGE_MIN_DATA_SCHEMA:-1}"

    # Version: CLI > manifest cmd > env > loud failure (never fabricate one).
    if [ -n "$cli_version" ]; then version="$cli_version"
    elif [ -n "${PACKAGE_VERSION_CMD:-}" ]; then version="$(eval "$PACKAGE_VERSION_CMD")"
    elif [ -n "${PACKAGE_VERSION:-}" ]; then version="$PACKAGE_VERSION"
    else die "no version: pass --version, or set PACKAGE_VERSION / PACKAGE_VERSION_CMD in $manifest"; fi
    version="${version#v}"  # normalize a leading v

    PACKAGE_TRIPLE="${cli_triple:-${PACKAGE_TRIPLE:-}}"
    triple="$(resolve_triple)"

    [ -n "$binary" ] || die "no binary: pass --binary, or set PACKAGE_BINARY in $manifest"

    note "packaging $name v$version for $triple"

    # 1. Build (delegates to the project's real build; skipped if binary exists
    #    and PACKAGE_SKIP_BUILD is set — e.g. a CI that built earlier).
    if [ "${PACKAGE_SKIP_BUILD:-0}" != "1" ]; then
        note "building: $build_cmd"
        eval "$build_cmd"
    fi
    [ -f "$binary" ] || die "build did not produce the binary at '$binary'"

    # optional macOS codesign hook (env-gated, default-skip).
    if [ -n "${CODESIGN_IDENTITY:-}" ] && [ "$(uname -s)" = "Darwin" ]; then
        note "codesigning $binary with identity $CODESIGN_IDENTITY"
        codesign --force --sign "$CODESIGN_IDENTITY" "$binary" || die "codesign failed"
    fi
    if [ "${NOTARIZE:-0}" = "1" ] || [ "${NOTARIZE:-}" = "true" ]; then
        note "NOTARIZE requested — run your notarytool submit here (hook stub)."
    fi

    # 2. Stage dist/<name>-v{VER}-{TRIPLE}/ (removed + recreated → idempotent).
    local bundle="${name}-v${version}-${triple}"
    local stage="dist/${bundle}"
    rm -rf "$stage"; mkdir -p "$stage"

    # 3. Copy the binary (as its basename) + declared assets.
    local binary_name; binary_name="$(basename "$binary")"
    cp "$binary" "$stage/$binary_name"
    # assets: CLI --asset globs first, then manifest PACKAGE_ASSETS.
    # (${arr[@]:-} guards the empty-array-under-`set -u` expansion.)
    local -a assets=()
    [ "${#cli_assets[@]}" -gt 0 ] && assets=("${cli_assets[@]}")
    if [ -n "${PACKAGE_ASSETS:-}" ]; then
        # shellcheck disable=SC2206
        assets+=($PACKAGE_ASSETS)
    fi
    local a
    for a in "${assets[@]:-}"; do
        [ -n "$a" ] || continue
        # Copy each glob match preserving its relative path under the stage.
        local m
        for m in $a; do
            [ -e "$m" ] || { note "asset '$m' not found — skipping"; continue; }
            mkdir -p "$stage/$(dirname "$m")"
            cp -R "$m" "$stage/$m"
        done
    done
    # optional migrations dir (DB-bearing apps).
    if [ -n "${MIGRATIONS_DIR:-}" ] && [ -d "$MIGRATIONS_DIR" ]; then
        mkdir -p "$stage/$(dirname "$MIGRATIONS_DIR")"
        cp -R "$MIGRATIONS_DIR" "$stage/$MIGRATIONS_DIR"
    fi

    # 4. Emit grimoire-build-info.json (§8, provenance) BEFORE release.json so its
    #    assets[] walk covers it, then release.json (§2) + SHA256SUMS (integrity floor).
    write_build_info_json "$stage"
    write_release_json "$stage" "$version" "$triple" "$binary_name" "$min_schema"
    write_checksums "$stage"

    # 5. Deterministic tarball beside the stage dir.
    local out_tar="dist/${bundle}.tar.gz"
    deterministic_tar "$stage" "$out_tar"

    note "wrote $out_tar"
    echo "$out_tar"
}

# ── self-test (offline temp-dir round trip) ──────────────────────────────────
self_test() {
    local fail=0
    local td; td="$(mktemp -d)"
    trap 'rm -rf "$td"' RETURN
    ( cd "$td"
      mkdir -p bin static config scripts
      printf 'binary-bytes\n' > bin/app
      printf 'body{}\n' > static/app.css
      printf 'PORT=8080\n' > config/app.env.example
      # A manifest that skips the build (binary already staged) and declares assets.
      # Placed at the default manifest path (scripts/package-manifest.sh).
      cat > scripts/package-manifest.sh <<'EOF'
PACKAGE_NAME="demo"
PACKAGE_BINARY="bin/app"
PACKAGE_ASSETS="static/app.css config/app.env.example"
PACKAGE_VERSION="1.2.3"
PACKAGE_SKIP_BUILD="1"
PACKAGE_TRIPLE="x86_64-linux"
EOF
    )
    # Run the packager twice against the same tree; capture output tarballs.
    local script_path; script_path="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    ( cd "$td" && bash "$script_path" >/dev/null 2>&1 ) || { echo "self-test: first package run failed" >&2; fail=1; }
    local tar1="$td/dist/demo-v1.2.3-x86_64-linux.tar.gz"
    [ -f "$tar1" ] || { echo "self-test: expected tarball not produced" >&2; fail=1; }
    # release.json present with the required fields.
    if [ -f "$td/dist/demo-v1.2.3-x86_64-linux/release.json" ]; then
        python3 - "$td/dist/demo-v1.2.3-x86_64-linux/release.json" <<'PY' || fail=1
import json, sys
m = json.load(open(sys.argv[1]))
req = {"version", "target_triple", "binary_sha256", "min_data_schema", "assets"}
assert req.issubset(m), "release.json missing fields: %s" % (req - set(m))
assert m["version"] == "1.2.3", m["version"]
assert m["target_triple"] == "x86_64-linux", m["target_triple"]
assert m["binary_sha256"], "binary_sha256 empty"
assert isinstance(m["min_data_schema"], int), "min_data_schema not int"
names = {a["name"] for a in m["assets"]}
assert "app" in names and "static/app.css" in names, names
assert all(set(a) == {"name", "sha256", "bytes"} for a in m["assets"]), "bad asset shape"
PY
    else
        echo "self-test: release.json not produced" >&2; fail=1
    fi
    # grimoire-build-info.json (§8) present, valid, required fields — and
    # covered by SHA256SUMS + listed in release.json's assets[] (both files are
    # siblings bundled into the SAME stage, not merged).
    local bi="$td/dist/demo-v1.2.3-x86_64-linux/grimoire-build-info.json"
    if [ -f "$bi" ]; then
        python3 - "$bi" <<'PY' || fail=1
import json, sys
m = json.load(open(sys.argv[1]))
req = {"framework-version", "grimoire-config", "build-timestamp", "source-ref"}
assert req.issubset(m), "grimoire-build-info.json missing fields: %s" % (req - set(m))
assert m["source-ref"], "source-ref empty"
PY
    else
        echo "self-test: grimoire-build-info.json not produced" >&2; fail=1
    fi
    if ! grep -q '  grimoire-build-info.json$' "$td/dist/demo-v1.2.3-x86_64-linux/SHA256SUMS" 2>/dev/null; then
        echo "self-test: SHA256SUMS does not cover grimoire-build-info.json" >&2; fail=1
    fi
    if [ -f "$td/dist/demo-v1.2.3-x86_64-linux/release.json" ]; then
        python3 - "$td/dist/demo-v1.2.3-x86_64-linux/release.json" <<'PY' || fail=1
import json, sys
m = json.load(open(sys.argv[1]))
names = {a["name"] for a in m["assets"]}
assert "grimoire-build-info.json" in names, "grimoire-build-info.json missing from release.json assets[]: %s" % names
PY
    fi
    # SHA256SUMS present and lists the binary.
    if ! grep -q '  app$' "$td/dist/demo-v1.2.3-x86_64-linux/SHA256SUMS" 2>/dev/null; then
        echo "self-test: SHA256SUMS missing or does not list the binary" >&2; fail=1
    fi

    # Provenance round trip: a real git repo + a real grimoire-config.json
    # populate framework-version + a tag@sha source-ref (not the "unknown"
    # fallback from the git-less scenario above).
    local td2; td2="$(mktemp -d)"
    ( cd "$td2"
      git init -q; git config user.email t@t; git config user.name t
      mkdir -p bin .claude scripts
      printf 'binary-bytes\n' > bin/app
      cat > .claude/grimoire-config.json <<'EOF'
{"framework-version": "v9.9", "work-paradigm": {"value": "Noir"}}
EOF
      cat > scripts/package-manifest.sh <<'EOF'
PACKAGE_NAME="demo2"
PACKAGE_BINARY="bin/app"
PACKAGE_VERSION="1.0.0"
PACKAGE_SKIP_BUILD="1"
PACKAGE_TRIPLE="x86_64-linux"
EOF
      git add -A; git commit -qm seed
      git tag -a v1.0.0 -m v1.0.0 )
    ( cd "$td2" && bash "$script_path" >/dev/null 2>&1 ) || { echo "self-test: provenance-scenario package run failed" >&2; fail=1; }
    local bi2="$td2/dist/demo2-v1.0.0-x86_64-linux/grimoire-build-info.json"
    if [ -f "$bi2" ]; then
        python3 - "$bi2" <<'PY' || fail=1
import json, sys
m = json.load(open(sys.argv[1]))
assert m["framework-version"] == "v9.9", m["framework-version"]
assert m["grimoire-config"]["work-paradigm"]["value"] == "Noir", m["grimoire-config"]
assert m["source-ref"].startswith("v1.0.0@"), "expected a tag@sha source-ref: %s" % m["source-ref"]
PY
    else
        echo "self-test: provenance-scenario grimoire-build-info.json not produced" >&2; fail=1
    fi
    rm -rf "$td2"

    # Determinism: a second run over the same tree yields a byte-identical tar
    # (only meaningful when the tar supports the determinism flags — GNU tar or a
    # libarchive new enough for --mtime).
    local first_sum second_sum
    first_sum="$(sha256_of "$tar1")"
    ( cd "$td" && bash "$script_path" >/dev/null 2>&1 )
    second_sum="$(sha256_of "$tar1")"
    if [ "$first_sum" != "$second_sum" ]; then
        note "self-test: tar not byte-identical across runs (expected on a tar"
        note "           without determinism flags; treated as a soft check)."
    fi
    if [ "$fail" -ne 0 ]; then
        echo "package.sh self-test: FAILED" >&2; return 1
    fi
    echo "package.sh self-test: OK (stage + grimoire-build-info.json + release.json + SHA256SUMS + tar round trip, provenance from a real git+config repo)"
    return 0
}

case "${1:-}" in
    --self-test) shift; self_test ;;
    -h|--help)
        sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) run_package "$@" ;;
esac
