#!/usr/bin/env bash
# api_types.sh — typed API boundary: codegen + drift gate (Grimoire #290, RSS-10).
#
# Reference implementation of the `api-types` / `api-types-check` recipes
# (docs/grimoire/design/typed-api-boundary-design.md). Canonizes the pattern
# proven in the issue-tracker fleet member's Dream re-platform: a Rust
# `api-types` crate holds the wire types (serde), an optional `ts-rs` feature
# derives matching `.ts` files, and this script's `check` mode regenerates
# into a scratch dir and diffs against the committed frontend copy — nonzero
# exit on drift.
#
# Called by `just api-types` (codegen) / `just api-types-check` (drift gate).
# These are `.claude/recipes.json` "extras", NOT build-recipe INTERFACE
# targets — invoke via `just`, not `recipe.py` (see the design doc's
# §Recipe-layer integration for why).
#
# OPT-IN / GRACEFUL NO-OP: most `web`-template projects are not SPA-shaped.
# When the pattern is not adopted (no API_TYPES_CRATE_DIR and no
# scripts/api-types-manifest.sh), both `gen` and `check` print a note and
# exit 0 — this is what makes it safe to wire `check` into `test`'s
# prerequisite chain unconditionally.
#
# PARAMETERIZED via scripts/api-types-manifest.sh (sourced shell vars) — the
# same manifest-file convention package.sh/deploy.sh use. See the API_TYPES_*
# contract below.
#
# Self-test: `scripts/api_types.sh --self-test` runs a fully offline round
# trip in a temp dir using a synthetic (non-cargo) generator, covering:
# not-adopted no-op, clean gen+check, committed-file drift, and source drift.
set -euo pipefail

# ── manifest contract (API_TYPES_* shell vars a project may set) ─────────────
#   API_TYPES_CRATE_DIR   the Rust crate holding the wire types (default: api-types)
#   API_TYPES_GEN_CMD     codegen command (default: cd "$API_TYPES_CRATE_DIR" &&
#                          cargo test --features export-bindings — ts-rs's own
#                          convention: codegen runs as a side effect of a test)
#   API_TYPES_OUT_DIR     committed, frontend-facing directory — GENERATED ONLY,
#                          never hand-edited (default: frontend/src/api-types)
#   API_TYPES_EXPORT_ENV  name of the env var the gen command reads to redirect
#                          its output directory (default: TS_RS_EXPORT_DIR —
#                          ts-rs's own override toggle)
DEFAULT_MANIFEST="scripts/api-types-manifest.sh"

die() { echo "api_types.sh: $*" >&2; exit 1; }
note() { echo "api_types.sh: $*" >&2; }

load_manifest() {
    local manifest="${1:-$DEFAULT_MANIFEST}"
    if [ -f "$manifest" ]; then
        # shellcheck disable=SC1090
        . "$manifest"
        note "loaded manifest $manifest"
    fi
}

resolve_vars() {
    API_TYPES_CRATE_DIR="${API_TYPES_CRATE_DIR:-api-types}"
    API_TYPES_GEN_CMD="${API_TYPES_GEN_CMD:-cd \"$API_TYPES_CRATE_DIR\" && cargo test --features export-bindings}"
    API_TYPES_OUT_DIR="${API_TYPES_OUT_DIR:-frontend/src/api-types}"
    API_TYPES_EXPORT_ENV="${API_TYPES_EXPORT_ENV:-TS_RS_EXPORT_DIR}"
    # Exported so the codegen command (run in a child `bash -c`, e.g. via
    # `env NAME=VALUE bash -c "$API_TYPES_GEN_CMD"`) can see them — the
    # default command references $API_TYPES_CRATE_DIR itself.
    export API_TYPES_CRATE_DIR API_TYPES_GEN_CMD API_TYPES_OUT_DIR API_TYPES_EXPORT_ENV
}

# Adopted iff the manifest exists (an explicit opt-in) or the crate dir exists
# (an implicit opt-in — the project already has the crate).
is_adopted() {
    [ -f "$DEFAULT_MANIFEST" ] || [ -d "$API_TYPES_CRATE_DIR" ]
}

# Run the codegen command with its export dir redirected to $1.
run_gen_into() {
    local target_dir="$1"
    mkdir -p "$target_dir"
    env "${API_TYPES_EXPORT_ENV}=$(cd "$target_dir" && pwd)" bash -c "$API_TYPES_GEN_CMD"
}

cmd_gen() {
    load_manifest; resolve_vars
    if ! is_adopted; then
        note "typed-api-boundary pattern not adopted (no $API_TYPES_CRATE_DIR/, no $DEFAULT_MANIFEST) — skipping gen."
        return 0
    fi
    note "regenerating $API_TYPES_OUT_DIR from $API_TYPES_CRATE_DIR"
    rm -rf "$API_TYPES_OUT_DIR"
    mkdir -p "$API_TYPES_OUT_DIR"
    run_gen_into "$API_TYPES_OUT_DIR"
    local n; n="$(find "$API_TYPES_OUT_DIR" -type f | wc -l | tr -d ' ')"
    note "wrote $n file(s) to $API_TYPES_OUT_DIR"
}

cmd_check() {
    load_manifest; resolve_vars
    if ! is_adopted; then
        note "typed-api-boundary pattern not adopted (no $API_TYPES_CRATE_DIR/, no $DEFAULT_MANIFEST) — skipping check (no-op, exit 0)."
        return 0
    fi
    if [ ! -d "$API_TYPES_OUT_DIR" ]; then
        note "drift: $API_TYPES_OUT_DIR does not exist yet — run 'just api-types' to generate it."
        return 1
    fi
    local tmp; tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    run_gen_into "$tmp"
    if diff -rq "$API_TYPES_OUT_DIR" "$tmp" >/dev/null 2>&1; then
        note "no drift — $API_TYPES_OUT_DIR matches a fresh regeneration."
        return 0
    fi
    note "DRIFT DETECTED — $API_TYPES_OUT_DIR is stale relative to $API_TYPES_CRATE_DIR:"
    diff -ru "$API_TYPES_OUT_DIR" "$tmp" >&2 || true
    note "run 'just api-types' and commit the result to fix."
    return 1
}

# ── self-test (offline, synthetic generator — no cargo/ts-rs required) ───────
self_test() {
    local fail=0
    local td; td="$(mktemp -d)"
    trap 'rm -rf "$td"' RETURN
    local script_path; script_path="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

    # Fake "backend crate": a single file whose content is the "source of
    # truth" the fake generator reads (stands in for the real Rust crate +
    # cargo test --features export-bindings).
    mkdir -p "$td/api-types"
    printf 'field_count=2\n' > "$td/api-types/spec.txt"
    mkdir -p "$td/scripts"
    cat > "$td/scripts/api-types-manifest.sh" <<EOF
API_TYPES_CRATE_DIR="api-types"
API_TYPES_OUT_DIR="frontend/src/api-types"
API_TYPES_EXPORT_ENV="FAKE_EXPORT_DIR"
API_TYPES_GEN_CMD='python3 - "\$API_TYPES_CRATE_DIR" <<PY
import os
spec = open(os.path.join("\$API_TYPES_CRATE_DIR", "spec.txt")).read().strip()
n = int(spec.split("=")[1])
out = os.environ["FAKE_EXPORT_DIR"]
os.makedirs(out, exist_ok=True)
for i in range(n):
    with open(os.path.join(out, "Type%d.ts" % i), "w") as fh:
        fh.write("export interface Type%d { id: number }\\n" % i)
PY'
EOF

    rc=0; ( cd "$td" && bash "$script_path" check >/dev/null 2>err.log ) || rc=$?
    if [ "$rc" -ne 1 ]; then
        echo "self-test: expected check to report drift (missing OUT_DIR) before first gen, got rc=$rc" >&2; fail=1
    fi

    ( cd "$td" && bash "$script_path" gen >/dev/null 2>gen.log ) || { echo "self-test: gen failed" >&2; fail=1; }
    [ -f "$td/frontend/src/api-types/Type0.ts" ] || { echo "self-test: gen did not produce Type0.ts" >&2; fail=1; }
    [ -f "$td/frontend/src/api-types/Type1.ts" ] || { echo "self-test: gen did not produce Type1.ts" >&2; fail=1; }

    rc=0; ( cd "$td" && bash "$script_path" check >/dev/null 2>check.log ) || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "self-test: expected check to pass with no drift, got rc=$rc" >&2
        cat "$td/check.log" >&2
        fail=1
    fi

    # Committed-file drift: hand-edit a generated file.
    printf 'export interface Type0 { id: number; extra: string }\n' > "$td/frontend/src/api-types/Type0.ts"
    rc=0; ( cd "$td" && bash "$script_path" check >/dev/null 2>drift1.log ) || rc=$?
    [ "$rc" -eq 1 ] || { echo "self-test: expected drift after hand-editing a committed type, got rc=$rc" >&2; fail=1; }
    grep -q "DRIFT DETECTED" "$td/drift1.log" || { echo "self-test: drift log missing DRIFT DETECTED" >&2; fail=1; }

    # Regenerate to clear that drift, then introduce SOURCE drift (spec grows
    # a field) — check must catch a newly-added generated file too.
    ( cd "$td" && bash "$script_path" gen >/dev/null 2>&1 )
    printf 'field_count=3\n' > "$td/api-types/spec.txt"
    rc=0; ( cd "$td" && bash "$script_path" check >/dev/null 2>drift2.log ) || rc=$?
    [ "$rc" -eq 1 ] || { echo "self-test: expected drift after a source change added a type, got rc=$rc" >&2; fail=1; }

    # Not-adopted no-op: a fresh dir with neither the crate dir nor the manifest.
    local td2; td2="$(mktemp -d)"
    ( cd "$td2" && bash "$script_path" gen >/dev/null 2>na_gen.log; echo $? > gen_rc )
    [ "$(cat "$td2/gen_rc")" = "0" ] || { echo "self-test: expected not-adopted gen to no-op with rc=0" >&2; fail=1; }
    ( cd "$td2" && bash "$script_path" check >/dev/null 2>na_check.log; echo $? > check_rc )
    [ "$(cat "$td2/check_rc")" = "0" ] || { echo "self-test: expected not-adopted check to no-op with rc=0" >&2; fail=1; }
    rm -rf "$td2"

    if [ "$fail" -ne 0 ]; then
        echo "api_types.sh self-test: FAILED" >&2
        return 1
    fi
    echo "api_types.sh self-test: OK (not-adopted no-op, gen, clean check, committed-file drift, source drift)"
    return 0
}

case "${1:-}" in
    gen) shift; cmd_gen "$@" ;;
    check) shift; cmd_check "$@" ;;
    --self-test) self_test ;;
    -h|--help)
        sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) die "usage: $(basename "$0") {gen|check|--self-test}" ;;
esac
