#!/usr/bin/env bash
# smoke-visual.sh — scripted-screenshot pixel-diff smoke gate (Grimoire #289, RSS-9).
#
# Generalizes the pixel-diff harness the fleet had independently rebuilt three
# times (forge-engine tools/smoke-screens/, obsidian ux-demo capture,
# retro-game-player scripts/visual-inspect.mjs) into one reusable recipe: the
# app exposes a scripted screenshot mode (a headless-browser command the
# project declares); this harness captures each configured route, diffs it
# against a committed baseline under `tests/smoke-baselines/`, and hard-fails
# on ANY missing reference — closing the forge-engine#150 class of bug where a
# missing baseline silently no-opped instead of failing.
#
# Extends the curl-based `smoke` floor (build-recipe `smoke` target,
# docs/grimoire/design/runtime-verification-design.md) rather than replacing
# it: `just smoke` stays the fast boot+curl liveness floor; `just smoke-visual`
# is the slower, browser-based visual-regression layer. Called by
# `just smoke-visual` — a justfile-level recipe outside the versioned
# build-recipe INTERFACE vocabulary (no INTERFACE_VERSION bump), routed via the
# `.claude/recipes.json` `"extras"` block (informational — recipe.py's
# INTERFACE dispatch does not know about it; call `just smoke-visual` or
# `recipe.py smoke --visual` is NOT a real flag, use the just target directly).
#
# Called by `just smoke-visual`:
#     just smoke-visual [port] [--update-baselines]
#
# PARAMETERIZED, not hardcoded to one project. The routes to capture and the
# headless-browser capture command come from (highest wins):
#   1. CLI flags (--route name=path, repeatable; --capture-cmd)
#   2. a small manifest file (default scripts/smoke-visual-manifest.sh, sourced
#      as shell vars) — see the SMOKE_VISUAL_* contract below.
#   3. a single default route ("home=/") — capture command still REQUIRED (no
#      built-in browser invocation is safe to assume across stacks); its
#      absence is a loud, named error, never a silent skip.
#
# ── manifest contract (SMOKE_VISUAL_* shell vars a project may set) ──────────
#   SMOKE_VISUAL_ROUTES        space-separated "name=path" pairs, e.g.
#                              "home=/ pricing=/pricing"
#   SMOKE_VISUAL_CAPTURE_CMD   headless-browser screenshot command template;
#                              {url} and {out} are substituted. Examples:
#                                'npx playwright screenshot --viewport-size=1280,720 "{url}" "{out}"'
#                                'chromium --headless --screenshot="{out}" --window-size=1280,720 "{url}"'
#   SMOKE_VISUAL_BASE_URL      base URL prefix (default http://localhost:$port)
#   SMOKE_VISUAL_BASELINE_DIR  committed reference images (default tests/smoke-baselines)
#   SMOKE_VISUAL_DIFF_DIR      gitignored diff artifacts (default dist/smoke-diff)
#   SMOKE_VISUAL_THRESHOLD     max allowed % of differing pixels (default 0.5)
#   SMOKE_VISUAL_FUZZ          per-channel 0-255 tolerance absorbing anti-aliasing
#                              noise before a pixel counts as "differing" (default 32)
#
# Baseline-update workflow (intentional UI change): review the diff artifacts
# in SMOKE_VISUAL_DIFF_DIR, then re-run with --update-baselines to regenerate
# the committed references from the current capture — commit the updated PNGs
# alongside the UI change. See runtime-verification-design.md
# §Visual smoke gate for the full workflow.
#
# Self-test: `scripts/smoke-visual.sh --self-test` runs fully offline — no
# browser dependency. It supplies a fixture SMOKE_VISUAL_CAPTURE_CMD that
# copies pre-generated fixture PNGs (via smoke_visual_diff.py make-fixture)
# instead of invoking a real headless browser, exercising the missing-baseline
# hard-failure, PASS/FAIL verdicts, diff-artifact emission, and
# --update-baselines round trip.
set -euo pipefail

DIFF_ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke_visual_diff.py"
DEFAULT_MANIFEST="scripts/smoke-visual-manifest.sh"

die() { echo "smoke-visual.sh: $*" >&2; exit 1; }
note() { echo "smoke-visual.sh: $*" >&2; }

# Capture one route's screenshot via the configured command template.
capture() {
    local cmd_template="$1" url="$2" out="$3"
    local cmd="${cmd_template//\{url\}/$url}"
    cmd="${cmd//\{out\}/$out}"
    mkdir -p "$(dirname "$out")"
    eval "$cmd" || die "capture command failed for $url (command: $cmd_template)"
    [ -f "$out" ] || die "capture command reported success but did not write $out"
}

run_smoke_visual() {
    local port="3000" manifest="$DEFAULT_MANIFEST" update_baselines="0"
    local -a cli_routes=()
    local cli_capture_cmd=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --port) port="$2"; shift 2;;
            --route) cli_routes+=("$2"); shift 2;;
            --capture-cmd) cli_capture_cmd="$2"; shift 2;;
            --manifest) manifest="$2"; shift 2;;
            --update-baselines) update_baselines="1"; shift;;
            *) die "unknown argument: $1 (see the header for usage)";;
        esac
    done

    if [ -f "$manifest" ]; then
        # shellcheck disable=SC1090
        . "$manifest"
        note "loaded manifest $manifest"
    fi

    local capture_cmd="${cli_capture_cmd:-${SMOKE_VISUAL_CAPTURE_CMD:-}}"
    [ -n "$capture_cmd" ] || die "no capture command: pass --capture-cmd, or set " \
        "SMOKE_VISUAL_CAPTURE_CMD in $manifest (the app's headless-browser screenshot " \
        "command — see the header contract). Never silently skipped."

    local base_url="${SMOKE_VISUAL_BASE_URL:-http://localhost:${GRIMOIRE_APP_PORT:-$port}}"
    local baseline_dir="${SMOKE_VISUAL_BASELINE_DIR:-tests/smoke-baselines}"
    local diff_dir="${SMOKE_VISUAL_DIFF_DIR:-dist/smoke-diff}"
    local threshold="${SMOKE_VISUAL_THRESHOLD:-0.5}"
    local fuzz="${SMOKE_VISUAL_FUZZ:-32}"

    local -a routes=()
    if [ "${#cli_routes[@]}" -gt 0 ]; then
        routes=("${cli_routes[@]}")
    elif [ -n "${SMOKE_VISUAL_ROUTES:-}" ]; then
        # shellcheck disable=SC2206
        routes=($SMOKE_VISUAL_ROUTES)
    else
        routes=("home=/")
    fi

    rm -rf "$diff_dir"; mkdir -p "$diff_dir" "$baseline_dir"
    local current_dir; current_dir="$(mktemp -d)"
    trap 'rm -rf "$current_dir"' RETURN

    local -a names=() verdicts=() pcts=()
    local overall_rc=0
    local route name path url current baseline
    for route in "${routes[@]}"; do
        name="${route%%=*}"; path="${route#*=}"
        [ -n "$name" ] && [ -n "$path" ] && [ "$name" != "$route" ] || \
            die "malformed route '$route' (expected name=/path)"
        url="${base_url%/}${path}"
        current="$current_dir/${name}.png"
        baseline="$baseline_dir/${name}.png"

        note "capturing $name ($url)"
        capture "$capture_cmd" "$url" "$current"

        if [ "$update_baselines" = "1" ]; then
            cp "$current" "$baseline"
            note "updated baseline: $baseline"
            names+=("$name"); verdicts+=("UPDATED"); pcts+=("-")
            continue
        fi

        if [ ! -f "$baseline" ]; then
            # Hard failure — the forge-engine#150 class of bug (missing
            # reference silently no-op'd instead of failing) is closed here.
            echo "smoke-visual: MISSING baseline for '$name' ($baseline) — run" \
                 "'just smoke-visual --update-baselines' after reviewing the" \
                 "capture, then commit the baseline." >&2
            names+=("$name"); verdicts+=("MISSING-BASELINE"); pcts+=("-")
            overall_rc=1
            continue
        fi

        local diff_out="$diff_dir/${name}-diff.png"
        local report rc
        report="$(python3 "$DIFF_ENGINE" diff "$baseline" "$current" "$diff_out" \
                  --threshold "$threshold" --fuzz "$fuzz")" && rc=0 || rc=$?
        if [ "$rc" -eq 2 ]; then
            die "diff engine error for '$name': $report"
        fi
        local pct verdict
        pct="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pct"])' "$report")"
        verdict="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["verdict"])' "$report")"
        names+=("$name"); verdicts+=("$verdict"); pcts+=("$pct%")
        [ "$rc" -eq 0 ] || overall_rc=1
        if [ "$verdict" = "FAIL" ]; then
            note "FAIL: $name differs by ${pct}% (threshold ${threshold}%) — diff: $diff_out"
        fi
    done

    echo "smoke-visual report (threshold ${threshold}%, fuzz ${fuzz}):"
    local i
    for i in "${!names[@]}"; do
        printf '  %-20s %-18s %s\n' "${names[$i]}" "${verdicts[$i]}" "${pcts[$i]}"
    done
    if [ "$update_baselines" = "1" ]; then
        echo "smoke-visual: baselines updated — review + commit $baseline_dir."
        return 0
    fi
    if [ "$overall_rc" -eq 0 ]; then
        echo "smoke-visual: PASS"
    else
        echo "smoke-visual: FAIL"
    fi
    return "$overall_rc"
}

# ── self-test (offline — fixture capture command, no browser) ───────────────
self_test() {
    local fail=0
    local td; td="$(mktemp -d)"
    trap 'rm -rf "$td"' RETURN
    local engine="$DIFF_ENGINE"
    local self; self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

    # A fixture "capture command" that stands in for a real headless browser:
    # it copies a pre-generated fixture PNG named after the LAST path segment
    # of the URL onto the requested output path.
    local fixture_capture='bash -c '"'"'src="'"$td"'/fixtures/$(basename "$1").png"; cp "$src" "$2"'"'"' _ "{url}" "{out}"'

    mkdir -p "$td/fixtures"
    python3 "$engine" make-fixture "$td/fixtures/home.png" base >/dev/null
    python3 "$engine" make-fixture "$td/fixtures/drifted.png" big-drift >/dev/null

    ( cd "$td"
      mkdir -p tests/smoke-baselines
      cp fixtures/home.png tests/smoke-baselines/home.png

      # 1. Baseline present, capture identical -> PASS, exit 0.
      bash "$self" --port 3000 --route "home=/home" --capture-cmd "$fixture_capture" \
          >"$td/out-pass.txt" 2>&1
      grep -q "smoke-visual: PASS" "$td/out-pass.txt" || {
          echo "self-test: expected PASS output" >&2; cat "$td/out-pass.txt" >&2; fail=1; }

      # 2. Missing baseline for a second route -> hard FAIL, never a silent no-op.
      set +e
      bash "$self" --port 3000 --route "unseen=/drifted" --capture-cmd "$fixture_capture" \
          >"$td/out-missing.txt" 2>&1
      rc=$?
      set -e
      [ "$rc" -ne 0 ] || { echo "self-test: missing baseline should fail (nonzero exit)" >&2; fail=1; }
      grep -q "MISSING-BASELINE" "$td/out-missing.txt" || {
          echo "self-test: expected MISSING-BASELINE in report" >&2; cat "$td/out-missing.txt" >&2; fail=1; }

      # 3. Baseline present but current drifts heavily -> FAIL, diff artifact written.
      cp fixtures/drifted.png tests/smoke-baselines/regressed.png
      # capture command must return the *base* fixture (so it differs from the
      # 'regressed' baseline we just seeded with the drifted fixture).
      set +e
      bash "$self" --port 3000 --route "regressed=/home" --capture-cmd "$fixture_capture" \
          >"$td/out-fail.txt" 2>&1
      rc=$?
      set -e
      [ "$rc" -ne 0 ] || { echo "self-test: a real pixel drift should fail" >&2; fail=1; }
      grep -q "FAIL" "$td/out-fail.txt" || {
          echo "self-test: expected FAIL verdict in report" >&2; cat "$td/out-fail.txt" >&2; fail=1; }
      [ -f dist/smoke-diff/regressed-diff.png ] || {
          echo "self-test: expected a diff artifact for the failing route" >&2; fail=1; }

      # 4. --update-baselines regenerates the reference from the current capture.
      rm -f tests/smoke-baselines/newroute.png
      bash "$self" --port 3000 --route "newroute=/home" --capture-cmd "$fixture_capture" \
          --update-baselines >"$td/out-update.txt" 2>&1
      [ -f tests/smoke-baselines/newroute.png ] || {
          echo "self-test: --update-baselines should write the new baseline" >&2; fail=1; }

      # 5. Absent capture command is a loud, named error (exit nonzero), not a no-op.
      set +e
      bash "$self" --port 3000 --route "home=/home" >"$td/out-nocmd.txt" 2>&1
      rc=$?
      set -e
      [ "$rc" -ne 0 ] || { echo "self-test: missing capture command should fail loud" >&2; fail=1; }
      grep -q "no capture command" "$td/out-nocmd.txt" || {
          echo "self-test: expected a named 'no capture command' error" >&2; fail=1; }
    )

    if [ "$fail" -ne 0 ]; then
        echo "smoke-visual.sh self-test: FAILED" >&2
        return 1
    fi
    echo "smoke-visual.sh self-test: OK (PASS/FAIL verdicts, missing-baseline hard" \
         "failure, diff-artifact emission, --update-baselines, absent-capture-cmd" \
         "loud error — all offline, no browser)"
    return 0
}

case "${1:-}" in
    --self-test) self_test ;;
    -h|--help)
        sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) run_smoke_visual "$@" ;;
esac
