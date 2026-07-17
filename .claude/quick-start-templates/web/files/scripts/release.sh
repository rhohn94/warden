#!/usr/bin/env bash
# release.sh — the changelog-derived release ceremony (Grimoire #201 Phase 2).
#
# Reference implementation of the `release` build-recipe target (no args)
# (deploy-environment-design.md §Release ceremony, web-app-deployment-protocol.md,
# issue #201 §4). Called by `just release` (which the recipes.json `release`
# target invokes, so `recipe.py release` ≡ `just release`).
#
# The ceremony, in order (issue #201 §4):
#   1. Derive VERSION from the NEWEST heading in the project's changelog
#      (default docs/version-history.md — the single source of truth).
#   2. Guards: on the release branch (default `main`), clean tree, tag ABSENT,
#      a changelog entry present for the derived version.
#   3. Bump the project's version file(s) — generic across stacks (Cargo.toml /
#      package.json / a VERSION file), highest-precedence source wins.
#   4. `recipe.py test`, then `recipe.py build --release` (or the `just`
#      equivalents); on a Cargo (rust) project, asserts target/release/ actually
#      holds artifacts after a prod build (#442 — refuses to package a debug
#      binary as a release asset); archive (delegates to `just package` when
#      available), commit the version bump, and tag.
#   5. Milestone reconciliation (§Milestone reconciliation below): resolve the
#      issues carrying the `milestone:v{X.Y}` label for this version through the
#      grm-issue-tracker abstraction (NOT raw `gh`), fold their titles into the
#      release notes, and gate on `deploy_policy` if open stragglers remain.
#   6. Publish (§Publish below, #418): once the tag has reached the remote, run
#      the ASSERTING publisher (`publish_release.py`, already shipped at
#      `.claude/skills/grm-project-release/` — the same tool the framework's own
#      self-hosted `scripts/release.sh` calls) so `gh release create`/asset
#      upload/checksum verification happen as ONE guarded step inside this
#      entrypoint instead of scattered raw verbs the harness classifier would
#      gate individually. Skippable (`--no-publish` / `RELEASE_SKIP_PUBLISH`)
#      for tag-only releases or a project not publishing via GitHub Releases.
#
# PARAMETERIZED, not hardcoded to Grimoire's own doc layout. Every stack-specific
# knob comes from (highest wins):
#   1. CLI flags (--changelog / --release-branch / --version / --dry-run /
#      --no-verify / --no-publish)
#   2. a small manifest file (default scripts/release-manifest.sh, sourced as
#      shell vars) — see the RELEASE_* contract below.
#   3. built-in fallbacks (docs/version-history.md, `main`, auto-detected version files).
# The optional milestone-reconciliation hook degrades gracefully when the tracker
# backend has no milestone concept (e.g. the roadmap backend) — a clear note, not
# an error.
#
# This is a REFERENCE / TEMPLATE implementation. It cannot know a project's real
# release infra, so a missing-but-required precondition is a LOUD failure (e.g. no
# changelog heading, an already-present tag), never a silent no-op. `--dry-run`
# prints every mutating action and changes nothing (no bump, commit, tag, publish,
# or push). It NEVER pushes — pushing a release stays a human/integration-master
# action, and the publish step (§Publish) is itself gated on the tag having
# already reached the remote — a read-only check, this script never pushes to
# satisfy it.
#
# Self-test: `scripts/release.sh --self-test` runs an offline temp-dir round trip
# (a synthetic git repo + changelog + version files: assert version derivation,
# each guard, the multi-stack bump, dry-run inertness, a real bump+commit+tag, the
# milestone-notes fold, and the publish gate — blocked before the tag reaches a
# synthetic remote, proceeding once it does). No repo bash --self-test convention
# exists (the .sh hooks are python polyglots), so a temp-dir round trip is used.
set -euo pipefail

# ── manifest contract (RELEASE_* shell vars a project may set) ────────────────
#   RELEASE_CHANGELOG       changelog path (default docs/version-history.md)
#   RELEASE_BRANCH          release branch that guards run against (default main)
#   RELEASE_VERSION_FILES   newline-/space-separated version files to bump
#                           (default: auto-detect Cargo.toml / package.json / VERSION)
#   RELEASE_TEST_CMD        test command (default: recipe.py test)
#   RELEASE_BUILD_CMD       release build command (default: recipe.py build --env prod)
#   RELEASE_PACKAGE_CMD     archive command (default: `just package` if a justfile
#                           has a package recipe; else skipped with a note)
#   RELEASE_MILESTONE_PREFIX  label prefix for the milestone convention (default milestone:v)
#   RELEASE_REMOTE          git remote the tag must reach before publish runs (default origin)
#   RELEASE_PUBLISH_CMD     publish command run once the tag is confirmed on the
#                           remote (default: publish_release.py --tag v$VERSION,
#                           the ASSERTING publisher — every dist/ asset must land
#                           on the GitHub Release and its sha256 must match
#                           SHA256SUMS, else hard-fail; `just package`'s dist/
#                           output — release.json + SHA256SUMS + tarball — is
#                           exactly what it expects, so no extra wiring needed)
# Optional hooks (env, default-skip):
#   RELEASE_SKIP_VERIFY     when "1", skip test+build (a CI that ran them earlier)
#   RELEASE_SKIP_PUBLISH    when "1" (or --no-publish), skip the remote-tag check
#                           and the publish step entirely — tag-only releases, or
#                           a project that doesn't publish via GitHub Releases
DEFAULT_MANIFEST="scripts/release-manifest.sh"
DEFAULT_CHANGELOG="docs/version-history.md"
DEFAULT_BRANCH="main"
DEFAULT_MILESTONE_PREFIX="milestone:v"
DEFAULT_REMOTE="origin"
# The issue-tracker abstraction (routes across github + roadmap backends without
# per-provider branching in this script). Overridable for a relocated skill dir.
ISSUE_TRACKER="${ISSUE_TRACKER:-.claude/skills/grm-issue-tracker/issue_tracker.py}"

die() { echo "release.sh: $*" >&2; exit 1; }
note() { echo "release.sh: $*" >&2; }
# In dry-run, `act` prints the command; otherwise it runs it.
act() {
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "  [dry-run] $*"
    else
        note "running: $*"
        "$@"
    fi
}

# ── version derivation (changelog is the single source of truth) ──────────────
# Print the version from the NEWEST heading in the changelog. A heading is any
# markdown heading whose text starts with a version token — `## v3.68 — …`,
# `## [1.4.0] - …`, `## 1.4.0` — first one wins (newest-first is the house
# convention, cf. version-history.md's own "newest first" preamble). The bare
# `X.Y[.Z]` (no leading v) is returned.
derive_version() {
    local changelog="$1"
    [ -f "$changelog" ] || die "changelog not found: $changelog (set RELEASE_CHANGELOG or pass --changelog)"
    python3 - "$changelog" <<'PY'
import re, sys
changelog = sys.argv[1]
# Match a heading line whose first token after the #'s (optionally bracketed and
# optionally v-prefixed) is a dotted version number. Newest-first: first match wins.
ver_re = re.compile(r'^#{1,6}\s*\[?v?(\d+\.\d+(?:\.\d+)?)\]?', re.IGNORECASE)
for line in open(changelog, encoding="utf-8"):
    m = ver_re.match(line)
    if m:
        print(m.group(1))
        break
else:
    sys.stderr.write("release.sh: no version heading found in %s\n" % changelog)
    sys.exit(5)
PY
}

# True (0) iff the changelog has a heading section for exactly this version.
changelog_has_entry() {
    local changelog="$1" version="$2"
    python3 - "$changelog" "$version" <<'PY'
import re, sys
changelog, version = sys.argv[1], sys.argv[2]
ver_re = re.compile(r'^#{1,6}\s*\[?v?(\d+\.\d+(?:\.\d+)?)\]?', re.IGNORECASE)
for line in open(changelog, encoding="utf-8"):
    m = ver_re.match(line)
    if m and m.group(1) == version:
        sys.exit(0)
sys.exit(1)
PY
}

# ── guards ────────────────────────────────────────────────────────────────────
git_clean() { [ -z "$(git status --porcelain 2>/dev/null)" ]; }
git_branch() { git rev-parse --abbrev-ref HEAD 2>/dev/null; }
# True (0) iff a tag for this version already exists (checks both `vX.Y` and `X.Y`).
tag_exists() {
    local version="$1"
    git rev-parse -q --verify "refs/tags/v$version" >/dev/null 2>&1 \
        || git rev-parse -q --verify "refs/tags/$version" >/dev/null 2>&1
}

run_guards() {
    local branch="$1" version="$2" changelog="$3"
    local cur; cur="$(git_branch)"
    [ "$cur" = "$branch" ] || die "guard: must be on the release branch '$branch' (on '$cur'). Switch first, or set RELEASE_BRANCH / --release-branch."
    git_clean || die "guard: working tree is DIRTY — commit or stash before releasing."
    if tag_exists "$version"; then
        die "guard: a tag for v$version already exists — the version is already released. Bump the changelog first."
    fi
    changelog_has_entry "$changelog" "$version" \
        || die "guard: no changelog entry for v$version in $changelog — add the release section first."
    note "guards passed: branch=$branch clean tag-absent changelog-entry-present for v$version."
}

# ── remote-tag verification (#418) ─────────────────────────────────────────
# READ-ONLY: this script never pushes (see header). It only confirms the tag
# already reached the remote before letting publish run against it — mirrors
# the framework's own self-hosted `scripts/release.sh` (its #298 fix): a fresh
# tag can't be published yet (GitHub can't see it), so the expected flow is
# (1) this check fails fast with a "push it first" message, (2) the tag +
# branch get pushed via the human-gated / autonomous-push integration step,
# (3) re-run ONLY the publish command (not the whole ceremony — the tag
# already exists and guards would refuse a second full run anyway).
remote_configured() {
    git remote get-url "$1" >/dev/null 2>&1
}

tag_pushed() {
    local remote="$1" version="$2"
    [ -n "$(git ls-remote --tags "$remote" "refs/tags/v$version" 2>/dev/null)" ]
}

# ── multi-stack version bump ──────────────────────────────────────────────────
# Bump the version string in each detected version file. Generic across stacks:
#   Cargo.toml       — first `version = "..."` under [package]
#   package.json     — top-level "version"
#   a VERSION file   — whole-file bare version
#   version.rs / version.py — a `VERSION = "..."` / `pub const VERSION: &str = "..."`
# Files come from RELEASE_VERSION_FILES (space/newline list) or auto-detection.
# Prints the files it changed (one per line). In dry-run it only reports.
detect_version_files() {
    local -a found=()
    [ -f Cargo.toml ] && found+=("Cargo.toml")
    [ -f package.json ] && found+=("package.json")
    [ -f VERSION ] && found+=("VERSION")
    printf '%s\n' "${found[@]:-}"
}

bump_version_file() {
    local file="$1" version="$2"
    [ -f "$file" ] || { note "version file '$file' not found — skipping."; return 0; }
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "  [dry-run] bump $file → $version"
        return 0
    fi
    python3 - "$file" "$version" <<'PY'
import json, re, sys
path, version = sys.argv[1], sys.argv[2]
name = path.rsplit("/", 1)[-1]
if name == "package.json":
    data = json.load(open(path))
    data["version"] = version
    open(path, "w").write(json.dumps(data, indent=2) + "\n")
elif name == "Cargo.toml":
    txt = open(path).read()
    # Only the first `version = "..."` (the [package] one), not dependency pins.
    txt, n = re.subn(r'(?m)^(version\s*=\s*")[^"]*(")',
                     lambda m: m.group(1) + version + m.group(2), txt, count=1)
    if n == 0:
        sys.stderr.write("release.sh: no version key in %s\n" % path); sys.exit(6)
    open(path, "w").write(txt)
elif name == "VERSION":
    open(path, "w").write(version + "\n")
else:
    # version.rs / version.py / version.go-style constant. Anchor on a
    # VERSION-named identifier's quoted value (`VERSION = "..."`,
    # `pub const VERSION: &str = "..."`, `__version__ = "..."`), NOT the first
    # dotted number in the file — otherwise a version mentioned in a comment or a
    # `python_requires`/edition would be clobbered instead of the real constant.
    txt = open(path).read()
    pat = re.compile(
        r'(?im)((?:pub\s+)?(?:const\s+)?[A-Za-z_]*VERSION[A-Za-z_]*'
        r'(?:\s*:\s*&?\w+)?\s*[=:]\s*")([^"]*)(")')
    txt, n = pat.subn(lambda m: m.group(1) + version + m.group(3), txt, count=1)
    if n == 0:
        sys.stderr.write(
            "release.sh: no VERSION constant found in %s — set RELEASE_VERSION_FILES "
            "to a file with a `VERSION = \"...\"`-style key, or bump it manually.\n"
            % path)
        sys.exit(6)
    open(path, "w").write(txt)
PY
    note "bumped $file → $version"
}

# ── milestone reconciliation (routed through the issue-tracker abstraction) ────
# Resolve the issues carrying `${prefix}{X.Y}` (default milestone:vX.Y) for this
# version and print two things:
#   1. a markdown "## Milestone" note block (closed issues → release notes) on stdout
#   2. the count of still-OPEN issues under that label (as the function's exit
#      status via a global) — so the caller can gate on deploy_policy.
# It goes through `issue_tracker.py --json list` so a github or roadmap backend
# both work with NO per-provider branching here. A backend with no milestone
# concept (roadmap) returns an empty set → a clear "no milestone concept" note.
# Reduce a release version to its X.Y milestone form: keep the first two dotted
# components, dropping a trailing patch (.Z). `3.69` → `3.69`; `3.69.1` → `3.69`.
# (Grimoire's milestone labels are minor-grained: milestone:v3.69.)
milestone_xy() {
    printf '%s' "$1" | awk -F. '{ if (NF>=2) printf "%s.%s", $1, $2; else printf "%s", $0 }'
}
# Emit the milestone reconciliation result on stdout as:
#   line 1:  __OPEN_COUNT__=<n>     (still-open stragglers under the label)
#   rest:    the "## Milestone" notes block (closed issues), possibly empty
# A single line-1 sentinel lets the caller capture BOTH the gating count and the
# notes from ONE command-substitution — a plain global would be lost across the
# `$(...)` subshell. Routed through the issue-tracker abstraction so github +
# roadmap both work with NO per-provider branching here; a backend with no
# milestone concept (roadmap) returns empty sets → count 0 + empty notes.
milestone_notes() {
    local version="$1" prefix="$2" tracker_py="$3"
    local mm; mm="$(milestone_xy "$version")"
    local label="${prefix}${mm}"
    if [ ! -f "$tracker_py" ]; then
        note "issue-tracker abstraction not found at $tracker_py — skipping milestone reconciliation (label $label)."
        echo "__OPEN_COUNT__=0"
        return 0
    fi
    # closed issues → notes; open issues → straggler count. Both via the same
    # abstraction (github: server-side --label + --state; roadmap: free-form).
    local closed_json open_json
    closed_json="$(python3 "$tracker_py" --json list --labels "$label" --state closed --limit 30 2>/dev/null || echo '[]')"
    open_json="$(python3 "$tracker_py" --json list --labels "$label" --state open --limit 30 2>/dev/null || echo '[]')"
    python3 - "$label" "$closed_json" "$open_json" <<'PY'
import json, sys
label, closed_raw, open_raw = sys.argv[1:4]
def load(s):
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []
closed, opened = load(closed_raw), load(open_raw)
lines = ["__OPEN_COUNT__=%d" % len(opened)]
if closed:
    lines.append("## Milestone %s" % label)
    lines.append("")
    for it in closed:
        num = it.get("number") or it.get("id")
        lines.append("- #%s %s" % (num, it.get("title", "").strip()))
# No closed issues → notes block stays empty (either no milestone concept in this
# backend, or an empty/all-open milestone; the caller notes the degradation).
sys.stdout.write("\n".join(lines))
PY
}

# ── rust-stack release-profile assertion (#442 fix suggestion 3) ──────────────
# Cargo always writes a plain `cargo build` to target/debug/ and only writes
# target/release/ for a `--release` (or `--profile release`) invocation. If
# the recipe.py `build` env param's ${env}->cargo-profile wiring is ever
# broken again (the exact #442 failure mode), a "prod" build silently stays a
# debug build while this ceremony reports success. When Cargo.toml is present
# and the build step actually ran with env=prod, assert target/release/ holds
# real output — refuse to package/tag a debug binary as a release asset.
# RELEASE_PROFILE_ASSERT_CMD overrides with a project-specific check (e.g. a
# workspace with a non-default target-dir); unset + no Cargo.toml == skipped.
assert_release_profile() {
    local build_cmd="$1"
    [ -f Cargo.toml ] || return 0
    case "$build_cmd" in
        *--env\ prod*|*--env=prod*) ;;
        *) return 0 ;;  # not a prod build — nothing to assert
    esac
    if [ -n "${RELEASE_PROFILE_ASSERT_CMD:-}" ]; then
        note "release-profile assert: $RELEASE_PROFILE_ASSERT_CMD"
        act bash -c "$RELEASE_PROFILE_ASSERT_CMD"
        return $?
    fi
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "  [dry-run] assert target/release/ holds built artifacts (rust release-profile check)"
        return 0
    fi
    if [ ! -d target/release ] || [ -z "$(find target/release -maxdepth 1 -type f 2>/dev/null)" ]; then
        die "release-profile assert: target/release/ is missing or empty after a" \
            "prod build — the build likely produced a DEBUG binary instead of" \
            "--release (see #442). Set RELEASE_PROFILE_ASSERT_CMD in the manifest" \
            "to customize this check, or fix the build command's \${env}->cargo" \
            "profile wiring."
    fi
    note "release-profile assert: target/release/ contains built artifacts — OK."
}

# Gate on deploy_policy when open stragglers remain under the milestone.
#   auto    → proceed with a loud warning.
#   pr_gate/manual → refuse unless RELEASE_ALLOW_OPEN_MILESTONE=1 (explicit override).
gate_open_milestone() {
    local open_count="$1" policy="$2" label="$3"
    [ "$open_count" -gt 0 ] || return 0
    case "$policy" in
        auto|"")
            note "WARNING: $open_count issue(s) still OPEN under $label — releasing anyway (deploy_policy=auto)."
            ;;
        pr_gate|manual)
            if [ "${RELEASE_ALLOW_OPEN_MILESTONE:-0}" = "1" ]; then
                note "WARNING: $open_count issue(s) OPEN under $label — proceeding via RELEASE_ALLOW_OPEN_MILESTONE override (policy=$policy)."
            else
                die "policy=$policy: $open_count issue(s) still OPEN under $label — close them or set RELEASE_ALLOW_OPEN_MILESTONE=1 to override."
            fi
            ;;
        *) die "unknown deploy_policy: $policy" ;;
    esac
}

# Read a release-gating deploy_policy: the strictest policy across declared
# environments (pr_gate > manual > auto), so the milestone gate is as strict as
# the project's most-guarded environment. Absent config → auto.
resolve_release_policy() {
    local config="${GRIMOIRE_CONFIG:-.claude/grimoire-config.json}"
    [ -f "$config" ] || { echo "auto"; return 0; }
    python3 - "$config" <<'PY'
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    print("auto"); raise SystemExit
rank = {"pr_gate": 3, "manual": 2, "auto": 1, "": 0}
best, best_rank = "auto", 0
for env in (cfg.get("environments") or {}).values():
    p = env.get("deploy_policy") or "auto"
    if rank.get(p, 0) > best_rank:
        best, best_rank = p, rank.get(p, 0)
print(best)
PY
}

# ── the release flow ──────────────────────────────────────────────────────────
run_release() {
    local manifest="$DEFAULT_MANIFEST"
    local cli_changelog="" cli_branch="" cli_version="" cli_no_verify="" cli_no_publish=""
    DRY_RUN=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --changelog) cli_changelog="$2"; shift 2;;
            --release-branch) cli_branch="$2"; shift 2;;
            --version) cli_version="$2"; shift 2;;
            --no-verify) cli_no_verify=1; shift;;
            --no-publish) cli_no_publish=1; shift;;
            --dry-run) DRY_RUN=1; shift;;
            --manifest) manifest="$2"; shift 2;;
            *) die "unknown argument: $1 (see the header for usage)";;
        esac
    done

    if [ -f "$manifest" ]; then
        # shellcheck disable=SC1090
        . "$manifest"
        note "loaded manifest $manifest"
    fi

    local changelog branch prefix test_cmd build_cmd package_cmd
    changelog="${cli_changelog:-${RELEASE_CHANGELOG:-$DEFAULT_CHANGELOG}}"
    branch="${cli_branch:-${RELEASE_BRANCH:-$DEFAULT_BRANCH}}"
    prefix="${RELEASE_MILESTONE_PREFIX:-$DEFAULT_MILESTONE_PREFIX}"
    test_cmd="${RELEASE_TEST_CMD:-python3 .claude/skills/grm-build-recipe/recipe.py test}"
    build_cmd="${RELEASE_BUILD_CMD:-python3 .claude/skills/grm-build-recipe/recipe.py build --env prod}"
    package_cmd="${RELEASE_PACKAGE_CMD:-}"

    # 1. Derive the version (CLI override wins for testability; else changelog).
    local version
    version="${cli_version:-$(derive_version "$changelog")}"
    version="${version#v}"
    note "releasing v$version (changelog $changelog, branch $branch)"

    # 2. Guards.
    run_guards "$branch" "$version" "$changelog"

    # 3. Bump version file(s).
    local -a version_files=()
    if [ -n "${RELEASE_VERSION_FILES:-}" ]; then
        # shellcheck disable=SC2206
        version_files=($RELEASE_VERSION_FILES)
    else
        while IFS= read -r f; do [ -n "$f" ] && version_files+=("$f"); done < <(detect_version_files)
    fi
    if [ "${#version_files[@]}" -eq 0 ]; then
        note "no version files detected (Cargo.toml / package.json / VERSION) — skipping bump. Set RELEASE_VERSION_FILES to bump a stack-specific file."
    fi
    local vf
    for vf in "${version_files[@]:-}"; do
        [ -n "$vf" ] || continue
        bump_version_file "$vf" "$version"
    done

    # 4. Verify (test + release build), then archive.
    if [ -n "$cli_no_verify" ] || [ "${RELEASE_SKIP_VERIFY:-0}" = "1" ]; then
        note "skipping test+build (--no-verify / RELEASE_SKIP_VERIFY)."
    else
        note "test: $test_cmd"
        act bash -c "$test_cmd"
        note "build: $build_cmd"
        act bash -c "$build_cmd"
        assert_release_profile "$build_cmd"
    fi
    # Archive: prefer the manifest's package cmd, else `just package` when present.
    if [ -z "$package_cmd" ] && command -v just >/dev/null 2>&1 \
       && just --summary 2>/dev/null | tr ' ' '\n' | grep -qx package; then
        package_cmd="just package"
    fi
    if [ -n "$package_cmd" ]; then
        note "archive: $package_cmd"
        act bash -c "$package_cmd"
    else
        note "no package/archive command (no RELEASE_PACKAGE_CMD, no just package recipe) — skipping archive."
    fi

    # 5. Milestone reconciliation (routed through the issue-tracker abstraction).
    #    milestone_notes emits `__OPEN_COUNT__=<n>` as line 1, then the notes block.
    local policy label raw_notes open_count notes
    policy="$(resolve_release_policy)"
    label="${prefix}$(milestone_xy "$version")"
    raw_notes="$(milestone_notes "$version" "$prefix" "$ISSUE_TRACKER")"
    open_count="$(printf '%s\n' "$raw_notes" | sed -n '1s/^__OPEN_COUNT__=//p')"
    open_count="${open_count:-0}"
    notes="$(printf '%s\n' "$raw_notes" | tail -n +2)"
    if [ -n "$notes" ]; then
        note "milestone $label — folding $(printf '%s' "$notes" | grep -c '^- ') closed issue(s) into release notes."
    elif [ "$open_count" -eq 0 ]; then
        note "milestone $label — no issues resolved (no milestone concept in this tracker backend, or an empty milestone). Proceeding."
    fi
    gate_open_milestone "$open_count" "$policy" "$label"

    # 6. Commit the bump + tag. (Never push — that stays a human action.)
    # Only stage version files that actually exist on disk — a declared-but-missing
    # RELEASE_VERSION_FILES entry is skipped by bump_version_file (a note, not a
    # failure), so `git add`-ing it would exit 128 and abort AFTER we'd already
    # bumped the present files.
    local -a present_files=()
    for vf in "${version_files[@]:-}"; do
        [ -n "$vf" ] && [ -f "$vf" ] && present_files+=("$vf")
    done
    if [ "${#present_files[@]}" -gt 0 ]; then
        act git add "${present_files[@]}"
        act git commit -m "release(v$version): bump version to v$version"
    else
        note "no version files to commit — tagging the current HEAD."
    fi
    act git tag -a "v$version" -m "v$version"
    note "tagged v$version. NOTE: this script does NOT push — push the tag + branch"
    note "      via the integration master / your human-gated release step."

    # 7. Publish (#418) — once the tag has reached the remote, run the
    #    ASSERTING publisher as ONE guarded step, replacing scattered raw
    #    `gh release create`/upload/checksum verbs. Skippable for tag-only
    #    releases. On a fresh tag (not pushed yet) this fails fast with an
    #    actionable message instead of letting the publish command fail on a
    #    tag GitHub can't see yet — re-run ONLY the publish command after the
    #    tag is pushed, not the whole ceremony (guards would refuse a second
    #    full run: the tag now exists).
    local remote; remote="${RELEASE_REMOTE:-$DEFAULT_REMOTE}"
    local publish_cmd
    publish_cmd="${RELEASE_PUBLISH_CMD:-python3 .claude/skills/grm-project-release/publish_release.py --tag v$version}"
    if [ -n "$cli_no_publish" ] || [ "${RELEASE_SKIP_PUBLISH:-0}" = "1" ]; then
        note "skipping remote-tag check + publish (--no-publish / RELEASE_SKIP_PUBLISH)."
    elif [ "$DRY_RUN" = "1" ]; then
        echo "  [dry-run] git ls-remote --tags $remote refs/tags/v$version   (verify pushed before publish)"
        echo "  [dry-run] $publish_cmd"
    else
        note "verifying v$version reached remote '$remote' before publish..."
        remote_configured "$remote" \
            || die "remote '$remote' is not configured — cannot verify v$version was pushed. Configure it (or set RELEASE_REMOTE), push v$version, then re-run ONLY the publisher: \`$publish_cmd\`."
        tag_pushed "$remote" "$version" \
            || die "tag v$version not found on remote '$remote' — push it first (e.g. \`git push $remote v$version\`), then re-run ONLY the publisher: \`$publish_cmd\`."
        note "v$version confirmed on remote '$remote' — publishing: $publish_cmd"
        bash -c "$publish_cmd" \
            || die "publish FAILED. The tag v$version exists and is fine: do NOT delete/re-tag; fix the cause, then re-run ONLY the publisher: \`$publish_cmd\`."
    fi

    # Emit the assembled milestone notes to stdout (useful for a customized
    # RELEASE_PUBLISH_CMD that wants to pass them to `gh release create
    # --notes-file`; the default publish_release.py assembles its own Release
    # body from the changelog and does not need this).
    if [ -n "$notes" ]; then
        printf '%s\n' "$notes"
    fi
    note "release ceremony complete for v$version."
}

# ── self-test (offline temp-dir round trip in a synthetic git repo) ───────────
self_test() {
    local fail=0
    local td; td="$(mktemp -d)"
    trap 'rm -rf "$td"' RETURN
    local script_path; script_path="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

    ( cd "$td"
      git init -q
      git config user.email t@t; git config user.name t
      # A stub issue-tracker abstraction: emits [] for both states (roadmap-like:
      # no milestone concept), so reconciliation degrades gracefully offline.
      mkdir -p .claude/skills/grm-issue-tracker
      cat > .claude/skills/grm-issue-tracker/issue_tracker.py <<'EOF'
import sys
print("[]")
EOF
      mkdir -p docs
      cat > docs/version-history.md <<'EOF'
# Version History

## v9.9 — Test release

- A synthetic release entry.

## v9.8 — Prior

- Older entry.
EOF
      # multi-stack version files.
      cat > package.json <<'EOF'
{
  "name": "demo",
  "version": "0.0.0"
}
EOF
      cat > Cargo.toml <<'EOF'
[package]
name = "demo"
version = "0.0.0"

[dependencies]
serde = { version = "1.0" }
EOF
      printf '0.0.0\n' > VERSION
      # A manifest that skips verify (no real test/build in the sandbox).
      mkdir -p scripts
      cat > scripts/release-manifest.sh <<'EOF'
RELEASE_SKIP_VERIFY="1"
RELEASE_SKIP_PUBLISH="1"
EOF
      git add -A; git commit -qm "seed"
    )

    # (Version derivation from the newest heading is asserted via the dry-run log
    #  in step 3, which prints "releasing v9.9" — v9.9 is newer than v9.8.)

    # 2. dry-run: exits 0, mutates NOTHING (no tag, no commit, version files unchanged).
    if ! ( cd "$td" && bash "$script_path" --dry-run ) >/dev/null 2>&1; then
        echo "self-test: dry-run should succeed" >&2; fail=1
    fi
    if ( cd "$td" && git rev-parse -q --verify refs/tags/v9.9 ) >/dev/null 2>&1; then
        echo "self-test: dry-run must not create a tag" >&2; fail=1
    fi
    if [ "$( cd "$td" && python3 -c 'import json;print(json.load(open("package.json"))["version"])' )" != "0.0.0" ]; then
        echo "self-test: dry-run must not bump version files" >&2; fail=1
    fi

    # 3. dry-run output reports the derived version + the guards + the no-milestone note.
    local out
    out="$( cd "$td" && bash "$script_path" --dry-run 2>&1 )"
    echo "$out" | grep -q 'releasing v9.9' || { echo "self-test: version not derived from newest heading" >&2; fail=1; }
    echo "$out" | grep -q 'guards passed' || { echo "self-test: guards not reported" >&2; fail=1; }
    echo "$out" | grep -q 'no milestone concept\|no issues resolved' || { echo "self-test: milestone degradation note missing" >&2; fail=1; }

    # 4. guard: a dirty tree is refused.
    ( cd "$td" && echo dirt > dirt.txt )
    if ( cd "$td" && bash "$script_path" --dry-run ) >/dev/null 2>&1; then
        echo "self-test: dirty tree should be refused" >&2; fail=1
    fi
    ( cd "$td" && rm -f dirt.txt )

    # 5. guard: wrong branch is refused.
    ( cd "$td" && git checkout -q -b feature )
    if ( cd "$td" && bash "$script_path" --dry-run ) >/dev/null 2>&1; then
        echo "self-test: non-release-branch should be refused" >&2; fail=1
    fi
    ( cd "$td" && git checkout -q - 2>/dev/null || git checkout -q main 2>/dev/null || git checkout -q master )

    # 6. guard: an absent changelog entry is refused (ask for a version with no heading).
    if ( cd "$td" && bash "$script_path" --version 1.2.3 --dry-run ) >/dev/null 2>&1; then
        echo "self-test: version with no changelog entry should be refused" >&2; fail=1
    fi

    # 7. a REAL run bumps all three files, commits, and tags (verify skipped via manifest).
    ( cd "$td" && bash "$script_path" ) >/dev/null 2>&1 || { echo "self-test: real release failed" >&2; fail=1; }
    if [ "$( cd "$td" && python3 -c 'import json;print(json.load(open("package.json"))["version"])' )" != "9.9" ]; then
        echo "self-test: package.json not bumped" >&2; fail=1
    fi
    if ! ( cd "$td" && grep -q '^version = "9.9"' Cargo.toml ); then
        echo "self-test: Cargo.toml not bumped (or bumped a dependency pin)" >&2; fail=1
    fi
    # the [dependencies] serde pin must be untouched.
    if ! ( cd "$td" && grep -q 'serde = { version = "1.0" }' Cargo.toml ); then
        echo "self-test: Cargo.toml dependency pin was clobbered" >&2; fail=1
    fi
    if [ "$( cd "$td" && cat VERSION )" != "9.9" ]; then
        echo "self-test: VERSION file not bumped" >&2; fail=1
    fi
    if ! ( cd "$td" && git rev-parse -q --verify refs/tags/v9.9 ) >/dev/null 2>&1; then
        echo "self-test: tag v9.9 not created" >&2; fail=1
    fi

    # 8. re-running is refused (tag now exists → the version is already released).
    if ( cd "$td" && bash "$script_path" --dry-run ) >/dev/null 2>&1; then
        echo "self-test: releasing an already-tagged version should be refused" >&2; fail=1
    fi

    # 9. milestone notes fold: a stub tracker that returns a closed issue produces
    #    a "## Milestone" notes block. (Fresh temp repo to avoid the tag guard.)
    local td2; td2="$(mktemp -d)"
    ( cd "$td2"
      git init -q; git config user.email t@t; git config user.name t
      mkdir -p .claude/skills/grm-issue-tracker docs scripts
      cat > .claude/skills/grm-issue-tracker/issue_tracker.py <<'EOF'
import sys
# Emit a closed issue for the --state closed query, nothing for open.
argv = sys.argv
if "closed" in argv:
    print('[{"number": 42, "title": "Fixed the widget", "id": "42"}]')
else:
    print("[]")
EOF
      cat > docs/version-history.md <<'EOF'
# Version History

## v9.9 — Test release

- entry
EOF
      cat > scripts/release-manifest.sh <<'EOF'
RELEASE_SKIP_VERIFY="1"
RELEASE_SKIP_PUBLISH="1"
EOF
      git add -A; git commit -qm seed )
    out="$( cd "$td2" && bash "$script_path" 2>/dev/null )"
    echo "$out" | grep -q '## Milestone milestone:v9.9' || { echo "self-test: milestone notes block missing" >&2; fail=1; }
    echo "$out" | grep -q '#42 Fixed the widget' || { echo "self-test: closed issue not folded into notes" >&2; fail=1; }
    rm -rf "$td2"

    # 10. open-milestone gate: an OPEN straggler under a pr_gate policy is REFUSED
    #     (and the RELEASE_ALLOW_OPEN_MILESTONE=1 override lets it proceed).
    local td3; td3="$(mktemp -d)"
    ( cd "$td3"
      git init -q; git config user.email t@t; git config user.name t
      mkdir -p .claude/skills/grm-issue-tracker docs scripts
      cat > .claude/skills/grm-issue-tracker/issue_tracker.py <<'EOF'
import sys
# One OPEN straggler; nothing closed.
print('[{"number": 7, "title": "Still open", "id": "7"}]' if "open" in sys.argv else "[]")
EOF
      cat > .claude/grimoire-config.json <<'EOF'
{"environments": {"production": {"deploy_policy": "pr_gate"}}}
EOF
      cat > docs/version-history.md <<'EOF'
# Version History

## v9.9 — Test release

- entry
EOF
      cat > scripts/release-manifest.sh <<'EOF'
RELEASE_SKIP_VERIFY="1"
RELEASE_SKIP_PUBLISH="1"
EOF
      git add -A; git commit -qm seed )
    if ( cd "$td3" && bash "$script_path" --dry-run ) >/dev/null 2>&1; then
        echo "self-test: pr_gate + open straggler should be REFUSED" >&2; fail=1
    fi
    if ! ( cd "$td3" && RELEASE_ALLOW_OPEN_MILESTONE=1 bash "$script_path" --dry-run ) >/dev/null 2>&1; then
        echo "self-test: RELEASE_ALLOW_OPEN_MILESTONE=1 override should proceed" >&2; fail=1
    fi
    rm -rf "$td3"

    # 11. version.py-style constant bump anchors on the VERSION key, NOT the first
    #     dotted number in the file (a version in a comment must be left alone).
    local td4; td4="$(mktemp -d)"
    ( cd "$td4"
      git init -q; git config user.email t@t; git config user.name t
      mkdir -p .claude/skills/grm-issue-tracker docs scripts src
      cat > .claude/skills/grm-issue-tracker/issue_tracker.py <<'EOF'
import sys
print("[]")
EOF
      cat > docs/version-history.md <<'EOF'
# Version History

## v9.9 — Test release

- entry
EOF
      # A dotted number in the docstring PRECEDES the real VERSION constant; the
      # naive "first dotted number" bumper would clobber the docstring instead.
      cat > src/version.py <<'EOF'
"""Version module, compatible with the demo 2.0 protocol."""
VERSION = "0.0.0"
EOF
      cat > scripts/release-manifest.sh <<'EOF'
RELEASE_SKIP_VERIFY="1"
RELEASE_SKIP_PUBLISH="1"
RELEASE_VERSION_FILES="src/version.py"
EOF
      git add -A; git commit -qm seed )
    ( cd "$td4" && bash "$script_path" ) >/dev/null 2>&1 || { echo "self-test: version.py release failed" >&2; fail=1; }
    if ! ( cd "$td4" && grep -q 'VERSION = "9.9"' src/version.py ); then
        echo "self-test: version.py VERSION constant not bumped" >&2; fail=1
    fi
    if ! ( cd "$td4" && grep -q 'demo 2.0 protocol' src/version.py ); then
        echo "self-test: version.py docstring version was clobbered (bumped wrong number)" >&2; fail=1
    fi
    rm -rf "$td4"

    # 12. a declared-but-MISSING RELEASE_VERSION_FILES entry does not abort the
    #     release: bump_version_file skips it, and the commit stages only present
    #     files (a missing pathspec would otherwise exit 128 after tagging).
    local td5; td5="$(mktemp -d)"
    ( cd "$td5"
      git init -q; git config user.email t@t; git config user.name t
      mkdir -p .claude/skills/grm-issue-tracker docs scripts
      cat > .claude/skills/grm-issue-tracker/issue_tracker.py <<'EOF'
import sys
print("[]")
EOF
      cat > docs/version-history.md <<'EOF'
# Version History

## v9.9 — Test release

- entry
EOF
      cat > VERSION <<'EOF'
0.0.0
EOF
      cat > scripts/release-manifest.sh <<'EOF'
RELEASE_SKIP_VERIFY="1"
RELEASE_SKIP_PUBLISH="1"
RELEASE_VERSION_FILES="VERSION does/not/exist.py"
EOF
      git add -A; git commit -qm seed )
    ( cd "$td5" && bash "$script_path" ) >/dev/null 2>&1 || { echo "self-test: release with a missing declared version file should NOT abort" >&2; fail=1; }
    if ! ( cd "$td5" && git rev-parse -q --verify refs/tags/v9.9 ) >/dev/null 2>&1; then
        echo "self-test: tag not created (missing-file git add likely aborted the run)" >&2; fail=1
    fi
    if [ "$( cd "$td5" && cat VERSION )" != "9.9" ]; then
        echo "self-test: present VERSION file not bumped" >&2; fail=1
    fi
    rm -rf "$td5"

    # 13. #442 release-profile assertion: a non-Cargo build is a no-op (nothing
    #     to assert); a Cargo project's prod build with an empty/missing
    #     target/release/ is refused; one with real artifacts passes; a dev
    #     (non-prod) build is skipped even with Cargo.toml present.
    local td6; td6="$(mktemp -d)"
    # no Cargo.toml at all — always a no-op regardless of build_cmd.
    if ! ( cd "$td6" && bash "$script_path" --assert-release-profile "recipe.py build --env prod" ); then
        echo "self-test: assert_release_profile should no-op without Cargo.toml" >&2; fail=1
    fi
    ( cd "$td6" && : > Cargo.toml )
    # a non-prod build_cmd is skipped even with Cargo.toml present.
    if ! ( cd "$td6" && bash "$script_path" --assert-release-profile "recipe.py build --env dev" ); then
        echo "self-test: assert_release_profile should skip a non-prod build" >&2; fail=1
    fi
    # prod build, no target/release/ at all — refused.
    if ( cd "$td6" && bash "$script_path" --assert-release-profile "recipe.py build --env prod" ) >/dev/null 2>&1; then
        echo "self-test: assert_release_profile should refuse a missing target/release/" >&2; fail=1
    fi
    # prod build, target/release/ exists but is empty — still refused.
    ( cd "$td6" && mkdir -p target/release )
    if ( cd "$td6" && bash "$script_path" --assert-release-profile "recipe.py build --env prod" ) >/dev/null 2>&1; then
        echo "self-test: assert_release_profile should refuse an empty target/release/" >&2; fail=1
    fi
    # prod build, target/release/ holds a real artifact — passes.
    ( cd "$td6" && touch target/release/demo )
    if ! ( cd "$td6" && bash "$script_path" --assert-release-profile "recipe.py build --env prod" ); then
        echo "self-test: assert_release_profile should pass with real target/release/ artifacts" >&2; fail=1
    fi
    # a project-supplied override always wins, bypassing the target/release/ check.
    ( cd "$td6" && rm -rf target )
    if ! ( cd "$td6" && RELEASE_PROFILE_ASSERT_CMD="true" bash "$script_path" --assert-release-profile "recipe.py build --env prod" ); then
        echo "self-test: RELEASE_PROFILE_ASSERT_CMD override should be honored" >&2; fail=1
    fi
    rm -rf "$td6"

    # A bare local repo as a synthetic "origin" (#418: the remote-tag check
    # needs a real remote to query — no network, just a second local repo).
    # push_tag=1 pre-pushes an annotated v9.9 to it (then drops the local ref,
    # so the run's own tag-exists guard still sees a fresh, untagged repo) to
    # model "the tag already reached the remote". push_tag=0 leaves the remote
    # tag-less, modeling "not pushed yet". Echoes the bare repo path.
    _remote_setup() {
        local td="$1" push_tag="$2"
        local bare; bare="$(mktemp -d)"
        git init -q --bare "$bare"
        ( cd "$td" && git remote add origin "$bare" )
        if [ "$push_tag" = "1" ]; then
            ( cd "$td" && git tag -a v9.9 -m pre \
                && git push -q origin v9.9 \
                && git tag -d v9.9 ) >/dev/null 2>&1
        fi
        echo "$bare"
    }

    # 14. #418 publish gate (a): the tag is NOT yet on the remote — the real
    #     run still tags v9.9 (tagging happens before the publish check), but
    #     fails fast BEFORE running RELEASE_PUBLISH_CMD, with an actionable
    #     "push it first, then re-run ONLY the publisher" message.
    local td7; td7="$(mktemp -d)"
    ( cd "$td7"
      git init -q; git config user.email t@t; git config user.name t
      mkdir -p .claude/skills/grm-issue-tracker .claude/skills/stub docs scripts
      cat > .claude/skills/grm-issue-tracker/issue_tracker.py <<'EOF'
import sys
print("[]")
EOF
      cat > docs/version-history.md <<'EOF'
# Version History

## v9.9 — Test release

- entry
EOF
      # Records whether it actually ran (touches a sentinel next to itself),
      # so the assertions below can prove publish was/wasn't reached.
      cat > .claude/skills/stub/publish_sentinel.py <<'EOF'
import pathlib
pathlib.Path(__file__).with_name("publish_ran.marker").touch()
EOF
      cat > scripts/release-manifest.sh <<'EOF'
RELEASE_SKIP_VERIFY="1"
RELEASE_PUBLISH_CMD="python3 .claude/skills/stub/publish_sentinel.py"
EOF
      git add -A; git commit -qm seed )
    local bare7; bare7="$(_remote_setup "$td7" 0)"   # remote configured, v9.9 NOT pushed
    local out rc
    if out="$( cd "$td7" && bash "$script_path" 2>&1 )"; then rc=0; else rc=$?; fi
    [ "$rc" -eq 0 ] && { echo "self-test: run must fail when the tag is not on the remote" >&2; fail=1; }
    ( cd "$td7" && git rev-parse -q --verify refs/tags/v9.9 ) >/dev/null 2>&1 \
        || { echo "self-test: tag v9.9 should still be created before the publish gate runs" >&2; fail=1; }
    echo "$out" | grep -q "not found on remote" \
        || { echo "self-test: missing not-found-on-remote message" >&2; fail=1; }
    echo "$out" | grep -q "push it first" \
        || { echo "self-test: missing push-first guidance" >&2; fail=1; }
    [ -f "$td7/.claude/skills/stub/publish_ran.marker" ] \
        && { echo "self-test: publish must NOT run when the tag is unpushed" >&2; fail=1; }
    rm -rf "$td7" "$bare7"

    # 15. #418 publish gate (b): the tag IS already on the remote — publish
    #     proceeds (the gate is a precondition check, not a permanent block).
    local td8; td8="$(mktemp -d)"
    ( cd "$td8"
      git init -q; git config user.email t@t; git config user.name t
      mkdir -p .claude/skills/grm-issue-tracker .claude/skills/stub docs scripts
      cat > .claude/skills/grm-issue-tracker/issue_tracker.py <<'EOF'
import sys
print("[]")
EOF
      cat > docs/version-history.md <<'EOF'
# Version History

## v9.9 — Test release

- entry
EOF
      cat > .claude/skills/stub/publish_sentinel.py <<'EOF'
import pathlib
pathlib.Path(__file__).with_name("publish_ran.marker").touch()
EOF
      cat > scripts/release-manifest.sh <<'EOF'
RELEASE_SKIP_VERIFY="1"
RELEASE_PUBLISH_CMD="python3 .claude/skills/stub/publish_sentinel.py"
EOF
      git add -A; git commit -qm seed )
    local bare8; bare8="$(_remote_setup "$td8" 1)"   # v9.9 pre-pushed to the remote
    ( cd "$td8" && bash "$script_path" ) >/dev/null 2>&1 \
        || { echo "self-test: run should succeed once the tag is on the remote" >&2; fail=1; }
    [ -f "$td8/.claude/skills/stub/publish_ran.marker" ] \
        || { echo "self-test: publish must run once the tag is confirmed pushed" >&2; fail=1; }
    rm -rf "$td8" "$bare8"

    if [ "$fail" -ne 0 ]; then
        echo "release.sh self-test: FAILED" >&2; return 1
    fi
    echo "release.sh self-test: OK (version derivation, guards, multi-stack bump, dry-run inertness, real bump+commit+tag, already-released refusal, milestone notes fold, open-milestone gate + override, version.py constant-anchor bump, missing-declared-file tolerance, #442 rust release-profile assertion, #418 publish gate [blocks unpushed / proceeds once pushed])"
    return 0
}

case "${1:-}" in
    --self-test) shift; self_test ;;
    # test-only entry point (#442): exercise assert_release_profile in
    # isolation without running the full ceremony. Not part of the public CLI.
    --assert-release-profile) shift; assert_release_profile "$1" ;;
    -h|--help)
        sed -n '2,84p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) run_release "$@" ;;
esac
