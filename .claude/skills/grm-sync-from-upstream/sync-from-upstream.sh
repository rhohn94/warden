#!/usr/bin/env bash
#
# sync-from-upstream.sh — pull workflow updates FROM the published upstream
# scaffolding distribution INTO this project, without destroying local
# customizations.
#
# Mirror image of sync-from-source.sh (project -> scaffolding). Here the
# direction is  <upstream-repo>/<FLAVOR>/  ->  this project.
#
# NON-DESTRUCTIVE by design:
#   * dry-run unless --apply (writes nothing; just reports).
#   * 3-way merge against a recorded base (.scaffold-base/): clean upstream
#     changes auto-apply; collisions with your edits are written as git
#     conflict markers and reported (both sides preserved, nothing lost).
#   * FALLBACK: when no base exists yet for a file that differs (first sync),
#     it does NOT overwrite — it reports REVIEW and keeps your local copy.
#   * additive only — never deletes local-only files.
#   * backs up every file it rewrites to .scaffold-sync-backup/<timestamp>/.
#   * refuses --apply on a dirty git tree unless --force.
#
# RECOGNIZED ARTIFACT — .claude/component-registry.json (#REG-4, Pillar 4
#   distribution): the versioned component registry is carried by the normal
#   file-merge walk below (it is NOT in is_excluded), so NEW/in-sync/UPDATE/
#   local classification is unchanged. ONLY the "both sides changed since
#   base" step is special-cased: a plain textual `git merge-file` 3-way merge
#   — the mechanism every other file uses — FALSE-conflicts on two disjoint
#   component additions (both diffs touch the same closing-brace/trailing-
#   comma region; verified empirically, see component_registry_merge.py's
#   --self-test). merge_component_registry() below routes this one recognized
#   artifact to that structural, per-component-id merge engine instead:
#   local-only and upstream-only components are preserved/added by id, a
#   genuine same-id collision surfaces as CONFLICT (local left untouched, no
#   embedded diff3 markers — a conflict report is written instead), and
#   nothing is ever silently dropped. The derived matrix
#   (.claude/cache/component-compatibility.json) is gitignored and
#   regenerable, so it is intentionally NOT distributed.
#
# Config — .scaffold-upstream.conf at the project root (or env vars / flags):
#   UPSTREAM_REPO=<git url or local path>   (required)
#   UPSTREAM_REF=<branch | tag | sha>       (optional; default branch if unset)
#   FLAVOR=claude-code|copilot              (optional; auto-detected from the
#                                            project's own layout — set only to
#                                            override an ambiguous detection)
#   UPSTREAM_TRANSPORT=auto|local|release|git (optional; default auto). How the
#                                            upstream tree is obtained — see the
#                                            transport-resolution block below.
#                                            'release' downloads the per-flavor
#                                            grimoire-<FLAVOR>-*.zip from the
#                                            UPSTREAM_REF GitHub Release via gh;
#                                            'auto' uses it when gh is present and
#                                            UPSTREAM_REF is a version tag, else
#                                            falls back to a shallow git clone.
#   UPSTREAM_CHANNEL=stable|beta            (optional; default stable; v3.27). The
#                                            release channel to consume on the
#                                            release transport. 'stable' downloads
#                                            grimoire-<FLAVOR>-v*.zip; 'beta'
#                                            downloads grimoire-<FLAVOR>-v*-beta.zip
#                                            (the --prerelease assets published off
#                                            the version/{X.Y} staging branch). The
#                                            channel only scopes which asset the
#                                            release transport fetches; git/local
#                                            transports are channel-agnostic.
#
# RELEASE-ASSET INTEGRITY (v3.27): on the release transport, the downloaded
# flavor .zip is verified against the release's SHA256SUMS asset before it is
# unzipped and merged. When SHA256SUMS is present and the digest matches, the
# sync proceeds; when it is present and MISMATCHES, the release path fails (and
# falls back to git). When SHA256SUMS is ABSENT from the release, the sync does
# NOT silently trust the asset — it prints a LOUD degradation notice and proceeds
# unverified only because the asset still came from the authenticated gh download.
# Never a silent skip. (minisign signature verification is a managed-project
# updater concern — see docs/grimoire/design/release-distribution-design.md §meta-updater.)
#
# Usage:
#   ./sync-from-upstream.sh [--apply] [--diff] [--adopt-base] [--force]
#                           [--mark-resolved <file>] [--all-resolved]
#
#   (no flag)      dry-run: report what each file would do.
#   --diff         also print per-file diffs for would-be changes.
#   --apply        write changes (merges, new files), with backups.
#   --adopt-base   record the current upstream as the base for every managed
#                  file WITHOUT touching local files. Use once on an existing
#                  customized project to establish provenance for future syncs.
#   --force        allow --apply on a dirty git tree (tracked changes). Untracked
#                  files never block --apply and need no flag (#143).
#   --mark-resolved <file>
#                  advance the recorded base for a SINGLE file to the current
#                  upstream content, so a future sync no longer re-conflicts it
#                  (#181). Use after you have hand-resolved one CONFLICT file (or
#                  for a file that is permanently diverged by design). Unlike
#                  --adopt-base it does NOT touch any other file's provenance, and
#                  it refuses if the file still contains conflict markers. The
#                  path may be project-relative or absolute; writes only
#                  .scaffold-base/<file>.
#   --all-resolved batch form of --mark-resolved (#420): advances the base for
#                  EVERY file currently classified CONFLICT in one invocation,
#                  instead of one --mark-resolved per file. Per file, the same
#                  rule applies as the single-file form: a file whose LOCAL copy
#                  still contains conflict markers is reported and SKIPPED, never
#                  force-resolved; only files that look already hand-resolved
#                  (no markers) get their base advanced. Prints a resolved/skipped
#                  summary; writes only .scaffold-base/.
#
# WARNINGS the merge walk can emit (#180):
#   * MISSING-SYMBOL (#180): after a 3-way merge, the result references a symbol
#     UPSTREAM defines but that is NOT defined anywhere in the merged output —
#     a "call-site without definition" the merge produced with no conflict marker
#     (typically LOCAL deleted a helper UPSTREAM still calls, in a non-overlapping
#     region). Best-effort, language-agnostic-ish. Never blocks; warns loudly.
#   * A file that re-`CONFLICT`s on --apply but whose LOCAL copy carries NO
#     conflict markers (#181) — a prior round was already hand-resolved (or
#     resolved via --mark-resolved) and looks it — has its base AUTO-ADVANCED
#     (#420) instead of being overwritten with fresh markers every sync; LOCAL is
#     left untouched. Reported as RESOLVED, not CONFLICT.
#
set -euo pipefail

# Directory this script lives in — used to locate its sibling
# component_registry_merge.py (Pillar 4 distribution engine) regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGISTRY_MERGE_ENGINE="$SCRIPT_DIR/component_registry_merge.py"

# --------------------------------------------------------------------------
# Upstream transport resolution (v3.23 release-distribution)
# Decide HOW to obtain the upstream flavor tree:
#   local   — UPSTREAM_REPO is a local checkout that already contains <FLAVOR>/
#   release — download grimoire-<FLAVOR>-*.zip from a GitHub Release tag (gh)
#   git     — shallow clone the repo at UPSTREAM_REF (the historical default)
# UPSTREAM_TRANSPORT (conf/env) forces a mode; default 'auto' picks per the
# rules below. The release path ALWAYS falls back to git on any failure, so
# behaviour never regresses. Design: docs/grimoire/design/release-distribution-design.md
# --------------------------------------------------------------------------
_looks_like_version_tag() {
  # v3.23 / 3.23 / v1.2.3 — a tag we publish release assets under.
  printf '%s' "$1" | grep -Eq '^v?[0-9]+(\.[0-9]+){1,3}$'
}

_channel_asset_pattern() {
  # Args: <flavor> <channel>. Echoes the gh --pattern for that channel's asset.
  # stable => grimoire-<flavor>-v*.zip (excluding -beta, applied by caller);
  # beta   => grimoire-<flavor>-v*-beta.zip. Closed channel set; unknown => 2.
  local flavor="$1" channel="$2"
  case "$channel" in
    stable) printf 'grimoire-%s-v*.zip\n' "$flavor" ;;
    beta)   printf 'grimoire-%s-v*-beta.zip\n' "$flavor" ;;
    *) echo "ERROR: UPSTREAM_CHANNEL must be stable|beta (got '$channel')." >&2; return 2 ;;
  esac
}

_gh_available() {
  # Overridable in --self-test via _GH_AVAILABLE_OVERRIDE (0/1).
  if [ -n "${_GH_AVAILABLE_OVERRIDE:-}" ]; then
    [ "$_GH_AVAILABLE_OVERRIDE" = "1" ]; return
  fi
  command -v gh >/dev/null 2>&1
}

resolve_transport() {
  # Args: <transport> <repo> <ref> <flavor>. Echoes one of local|release|git.
  local t="$1" repo="$2" ref="$3" flavor="$4"
  case "$t" in
    local|release|git) printf '%s\n' "$t"; return 0 ;;
    auto) ;;
    *) echo "ERROR: UPSTREAM_TRANSPORT must be auto|local|release|git (got '$t')." >&2; return 2 ;;
  esac
  if [ -d "$repo/$flavor" ]; then printf 'local\n'; return 0; fi
  if _gh_available && _looks_like_version_tag "$ref" \
       && printf '%s' "$repo" | grep -q 'github\.com'; then
    printf 'release\n'; return 0
  fi
  printf 'git\n'
}

# --------------------------------------------------------------------------
# Self-update helpers (#443, v3.91) — pure functions consulted by the
# self-update step below, before the BMI-3 boundary guard runs.
# --------------------------------------------------------------------------
self_update_rel_path() {
  # Arg: <flavor>. Echoes this script's own path relative to a flavor's tree.
  if [ "$1" = "copilot" ]; then
    printf 'scripts/sync-from-upstream.sh\n'
  else
    printf '.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh\n'
  fi
}

self_update_raw_url() {
  # Args: <repo> <ref> <flavor>. Echoes the raw.githubusercontent.com URL for
  # this script's newest upstream bytes, or "" when <repo> isn't a GitHub URL
  # (self-update is a GitHub-raw-content / local-path best-effort only, never
  # a hard requirement — any other transport falls through silently).
  local repo="$1" ref="$2" flavor="$3" owner_repo
  case "$repo" in
    https://github.com/*|http://github.com/*) ;;
    *) printf ''; return 0 ;;
  esac
  owner_repo="$(printf '%s' "$repo" | sed -E 's#^https?://github\.com/##; s#\.git$##; s#/$##')"
  printf 'https://raw.githubusercontent.com/%s/%s/%s/%s\n' \
    "$owner_repo" "$ref" "$flavor" "$(self_update_rel_path "$flavor")"
}

_sha256_of() {
  # Echo the hex SHA-256 of file $1 using whatever tool is present.
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"
  fi
}

verify_against_sha256sums() {
  # Args: <zip_path> <sums_file_or_empty>. Verifies the zip's digest against the
  # named line in SHA256SUMS. Returns: 0 = verified; 1 = checksum mismatch (hard
  # fail); 2 = no SHA256SUMS available (LOUD degradation, caller proceeds).
  local zipf="$1" sums="$2" want got base
  base="$(basename "$zipf")"
  if [ -z "$sums" ] || [ ! -f "$sums" ]; then
    echo "WARNING: release has no SHA256SUMS asset — cannot verify $base." >&2
    echo "         Proceeding with the gh-downloaded asset UNVERIFIED (no silent skip;" >&2
    echo "         publish SHA256SUMS with the release to enable integrity checks)." >&2
    return 2
  fi
  want="$(awk -v n="$base" '$2==n{print $1}' "$sums" | head -1)"
  if [ -z "$want" ]; then
    echo "WARNING: SHA256SUMS present but lists no entry for $base — UNVERIFIED." >&2
    return 2
  fi
  got="$(_sha256_of "$zipf")"
  if [ "$want" != "$got" ]; then
    echo "ERROR: checksum MISMATCH for $base (want $want, got $got) — refusing asset." >&2
    return 1
  fi
  echo "Verified $base against SHA256SUMS (sha256 ok)."
  return 0
}

fetch_release_asset() {
  # Args: <dest_dir>. Downloads the UPSTREAM_CHANNEL flavor zip from the
  # UPSTREAM_REF release, verifies it against the release's SHA256SUMS (loud
  # degradation when absent), then unzips into dest_dir so dest_dir/<FLAVOR>/
  # exists. Returns 0 on success; any non-zero triggers the caller's git fallback.
  local dest="$1" slug zipdir zipf sumsf pattern
  slug="$(printf '%s' "$UPSTREAM_REPO" | sed -E 's#^.*github\.com[:/]+##; s#\.git$##; s#/+$##')"
  [ -n "$slug" ] || return 1
  pattern="$(_channel_asset_pattern "$FLAVOR" "$UPSTREAM_CHANNEL")" || return 1
  echo "Fetching release asset: $pattern from $slug@$UPSTREAM_REF (channel: $UPSTREAM_CHANNEL) ..."
  zipdir="$(mktemp -d)"
  # Pull the flavor zip AND SHA256SUMS in one go; SHA256SUMS may be absent on
  # older releases (handled by verify_against_sha256sums's degradation path).
  if ! gh release download "$UPSTREAM_REF" --repo "$slug" \
        --pattern "$pattern" --pattern "SHA256SUMS" --dir "$zipdir" >/dev/null 2>&1; then
    # Retry without SHA256SUMS in case it genuinely does not exist on the release.
    if ! gh release download "$UPSTREAM_REF" --repo "$slug" \
          --pattern "$pattern" --dir "$zipdir" >/dev/null 2>&1; then
      rm -rf "$zipdir"; return 1
    fi
  fi
  # For stable, exclude any -beta asset the glob may have also matched.
  if [ "$UPSTREAM_CHANNEL" = "stable" ]; then
    zipf="$(find "$zipdir" -name "grimoire-$FLAVOR-v*.zip" -type f ! -name '*-beta.zip' | head -1)"
  else
    zipf="$(find "$zipdir" -name "grimoire-$FLAVOR-v*-beta.zip" -type f | head -1)"
  fi
  [ -n "$zipf" ] || { rm -rf "$zipdir"; return 1; }
  sumsf="$zipdir/SHA256SUMS"
  verify_against_sha256sums "$zipf" "$sumsf"; local vrc=$?
  [ "$vrc" -eq 1 ] && { rm -rf "$zipdir"; return 1; }   # mismatch => fall back to git
  if command -v unzip >/dev/null 2>&1; then
    unzip -q "$zipf" -d "$dest" >/dev/null 2>&1 || { rm -rf "$zipdir"; return 1; }
  else
    python3 -m zipfile -e "$zipf" "$dest" >/dev/null 2>&1 || { rm -rf "$zipdir"; return 1; }
  fi
  rm -rf "$zipdir"
  [ -d "$dest/$FLAVOR" ]
}

detect_integration_line() {
  # Read branch-model.integration-branch from grimoire-config.json (if present).
  # Default: 'dev'. Args: <project_root>.
  local root="${1:-$PROJECT_ROOT}"
  local cfg="$root/.claude/grimoire-config.json"
  local val=""
  if [ -f "$cfg" ] && command -v python3 >/dev/null 2>&1; then
    val="$(python3 -c "
import json, sys
try:
  c = json.load(open('$cfg'))
  print(c.get('branch-model', {}).get('integration-branch', ''))
except Exception:
  print('')
" 2>/dev/null)" || val=""
  fi
  printf '%s\n' "${val:-dev}"
}

# --------------------------------------------------------------------------
# Working-tree cleanliness, IGNORING untracked files (#143).
# `git status --porcelain` reports untracked files as "?? <path>" lines. Those
# never risk losing work and must not block a safe 3-way merge — only tracked
# changes (staged or unstaged) make the tree "dirty" for --apply purposes.
# Pure over its argument so the self-test can drive it with canned porcelain.
# Returns 0 (dirty) iff any non-"?? " line remains; 1 (clean) otherwise.
# --------------------------------------------------------------------------
porcelain_has_tracked_changes() {
  # Arg: the full `git status --porcelain` output (may be empty/multi-line).
  printf '%s\n' "$1" | grep -qv '^??' && printf '%s\n' "$1" | grep -q '[^[:space:]]'
}

# --------------------------------------------------------------------------
# BMI-3 Rule 3b escape hatch (#144/#146/#162/#173) — the consumer-sync catch-22.
# After any sync, the integration line carries the prior sync's own
# framework-version bump (and any committed conflict resolution), so it is one
# or more commits AHEAD of main. The naive "trees identical" boundary then
# blocks every consecutive sync until a release re-equalizes the lines — even
# though NO real fork exists. The fix uses the SAME model-aware predicate the
# BMI-2 divergence guard uses: a fork is dangerous only when main carries tree
# content the integration line lacks; the integration line being merely ahead
# is safe. `git cherry INT main` prints "+ <sha>" for a commit whose patch has
# no equivalent on INT (real, unreachable work => a fork) and "- <sha>" for a
# benign promotion merge. Any "+" => fork (HALT); none => ahead-only (safe).
# Fails safe: a git error yields a synthetic "+ error" so the guard never
# silently misses a real fork.
# --------------------------------------------------------------------------
cherry_lines_show_unreachable_work() {
  # Arg: combined `git cherry INT main` output. Returns 0 iff any "+ " line.
  printf '%s\n' "$1" | grep -q '^+ '
}

main_only_cherry_lines() {
  # Args: <root> <int> <published>. Echo `git cherry INT main`; on any git
  # error echo a synthetic "+ error" so the classifier fails safe (HALT).
  local root="$1" int="$2" pub="$3" out
  if ! out="$(git -C "$root" cherry "$int" "$pub" 2>/dev/null)"; then
    printf '%s\n' "+ error"; return 0
  fi
  printf '%s\n' "$out"
}

# --------------------------------------------------------------------------
# BMI-3 sync-continuation token (v3.90) — bookkeeping for the ahead-by-design
# state, no longer a gate. Before v3.92 (#419), Rule 3b demanded the
# integration line and main be tree-identical, so a sync's own commit put the
# line ahead-by-one and the SAME flow's follow-up runs (conflict-resolution
# re-sync, adoption re-run) had to pass --allow-ahead — a flag whose name
# pattern-matches a [Safety Bypass Flag] and that autonomous harnesses
# rightly refuse to invent or reach for. #419 retires that flag entirely:
# Rule 3b now proceeds by DEFAULT whenever the fork predicate
# (cherry_lines_show_unreachable_work) shows main carries no unreachable
# work, ahead-by-any-amount, no token required. The token file
# (.scaffold-sync-state.json) is still written on a clean boundary and on an
# ahead-but-safe run, purely as an operator-facing record of the last known
# boundary; nothing in the guard path branches on it anymore. The fork guard
# itself is NEVER relaxed — main carrying unreachable work is still refused
# unconditionally, by the same cherry-based predicate, with no flag or token
# able to bypass it.
# --------------------------------------------------------------------------
continuation_read_sha() {
  # Arg: state-file path. Echo the recorded boundary main SHA ("" if absent).
  [ -f "$1" ] || { echo ""; return 0; }
  sed -n 's/.*"boundary-main-sha"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]*\)".*/\1/p' "$1" | head -n1
}

continuation_permits() {
  # Args: <recorded_sha> <current_main_sha> <cherry_lines>. Pure predicate:
  # continuation holds iff a SHA was recorded, main has not moved since the
  # recorded clean boundary, and main carries no unreachable work.
  local rec="$1" cur="$2" cherry="$3"
  [ -n "$rec" ] || return 1
  [ -n "$cur" ] || return 1
  [ "$rec" = "$cur" ] || return 1
  if cherry_lines_show_unreachable_work "$cherry"; then return 1; fi
  return 0
}

continuation_should_bootstrap() {
  # Arg: <recorded_sha> (the current continuation_read_sha() result). Pure
  # predicate: bootstrap-seeding applies iff NO token has ever been recorded
  # for this repo — an empty/absent state file, never a stale one. The fork
  # predicate is checked separately by the caller before this is consulted, so
  # this function only decides "first encounter or not," not safety.
  [ -z "$1" ]
}

continuation_record() {
  # Args: <state-file path> <main_sha> <integration-branch>. Overwrites.
  printf '{\n  "boundary-main-sha": "%s",\n  "integration-branch": "%s",\n  "recorded-by": "sync-from-upstream"\n}\n' \
    "$2" "$3" > "$1"
}

# --------------------------------------------------------------------------
# Hook atomic-replace class (v3.90) — guard hooks are upstream-authoritative.
# 3-way-merge conflicts inside live guard-hook code cannot be auto-resolved by
# autonomous agents (harness classifiers block hand-edits to guard logic —
# correctly), and a stale hook can silently lack a capability its config claims
# (the warden push-guard incident). Project-specific behavior belongs in
# .claude/grimoire-config.json, which hooks read at runtime — so any file
# upstream ships under .claude/hooks/ is REPLACED wholesale on --apply (backed
# up, loudly reported), never 3-way merged into CONFLICT/REVIEW.
# --------------------------------------------------------------------------
is_hook_artifact() {
  case "$1" in .claude/hooks/*) return 0 ;; esac
  return 1
}

# --------------------------------------------------------------------------
# Recognized sync artifact — .claude/component-registry.json (#REG-4, Pillar 4
# distribution). NOT excluded from the walk (it still rides "NEW"/"in-sync"/
# "UPDATE"/"local" classification unchanged) — only the "both sides changed"
# 3-way-merge step below special-cases it, routing to the structural
# component_registry_merge.py engine instead of a plain textual `git
# merge-file`, which produces FALSE conflicts on two disjoint component
# additions (see that script's module docstring + --self-test).
# --------------------------------------------------------------------------
is_component_registry_artifact() {
  [ "$1" = ".claude/component-registry.json" ]
}

# --------------------------------------------------------------------------
# Structural merge dispatch for the component registry (#REG-4). Pure over its
# file-path arguments (writes only $4/$5) so the self-test can drive it
# directly, without the full CLI's unrelated BMI-3 boundary guards. Echoes
# "clean" (merged registry written to $4) or "conflict" (nothing written to
# $4; a structured report written to $5) — mirrors the merge engine's own 0/1
# exit-code contract without the caller needing to inspect it directly.
# --------------------------------------------------------------------------
merge_component_registry() {
  local l="$1" b="$2" u="$3" out="$4" conflicts_out="$5"
  if python3 "$REGISTRY_MERGE_ENGINE" merge --local "$l" --base "$b" --upstream "$u" \
      --out "$out" --conflicts-out "$conflicts_out" 2>/dev/null; then
    echo clean
  else
    echo conflict
  fi
}

# --------------------------------------------------------------------------
# Stale skill-dir namespacing detection (v3.90) — the file-walk ADDS upstream
# grm-* skills but never deletes a pre-v3.42 bare-named twin (the sync is
# non-destructive), so `iterate/` and `grm-iterate/` coexist indefinitely.
# Echo one "bare/ -> grm-bare/" line per coexisting pair; empty when clean.
# The migrate itself stays explicitly-confirmed (grm_namespacing.py --apply,
# reference.md Step 4.55) — this only makes the sync flow SURFACE it.
# --------------------------------------------------------------------------
stale_namespacing_pairs() {
  # Arg: skills dir (e.g. <root>/.claude/skills). Pure over the filesystem.
  # Alias-aware (#308): the agent-role skills were renamed grm-<role> ->
  # grm-agent-<role>, so a bare `scout/` pairs with `grm-agent-scout/`, and a
  # stale grm-era ghost (`grm-scout/`) pairs with its canonical twin too.
  local sd="$1" d base twin
  [ -d "$sd" ] || return 0
  for d in "$sd"/*/; do
    [ -d "$d" ] || continue
    base="$(basename "$d")"
    case "$base" in
      scout|grm-scout)                 twin="grm-agent-scout" ;;
      reporter|grm-reporter)           twin="grm-agent-reporter" ;;
      reviewer|grm-reviewer)           twin="grm-agent-reviewer" ;;
      verifier|grm-verifier)           twin="grm-agent-verifier" ;;
      triager|grm-triager)             twin="grm-agent-triager" ;;
      qa-agent|grm-qa-agent)           twin="grm-agent-qa" ;;
      grm-*|README*|_*)                continue ;;
      *)                               twin="grm-$base" ;;
    esac
    [ "$base" = "$twin" ] && continue
    [ -d "$sd/$twin" ] && printf '%s\n' "$base/ -> $twin/"
  done
  return 0
}

# --------------------------------------------------------------------------
# Files that map to local but must NOT be auto-synced (project-owned/templates)
# Paths are relative to the flavor dir (== relative to the project root).
# Defined here (before --self-test) so the self-test can call the real function.
# --------------------------------------------------------------------------
is_excluded() {
  case "$1" in
    README.md|.gitignore) return 0 ;;
    docs/roadmap.md|docs/design/README.md) return 0 ;;
    .DS_Store|*/.DS_Store) return 0 ;;
    .grimoire-flavor) return 0 ;;                # upstream flavor marker, not project content (v3.23)
    .claude/settings.local.json|.claude/integration-allow.local) return 0 ;;
    .scaffold-base/*|.scaffold-sync-backup/*|.scaffold-upstream.conf) return 0 ;;
    .scaffold-sync-state.json) return 0 ;;      # BMI-3 continuation token (v3.90) — local state, never synced
    .scaffold-conflict-pending/*) return 0 ;;   # pending-conflict fingerprints (#420) — local state, never synced
    .claude/grimoire-config.json) return 0 ;;   # adoption phase owns this file (SR1 F2)
    CLAUDE.md) return 0 ;;                       # project-specific; re-specialize manually (SR1 F2)
    # ── Framework-internal docs (v3.39 "Bulkhead" / documentation-separation
    #    -design.md §4): never 3-way-merge these into a consumer's tree. Mirror
    #    of build_distributables.py EXCLUDED_PATH_PREFIXES. The design/ subtree
    #    uses a glob; the named single files are listed exactly.
    #    docs/grimoire/README.md is deliberately NOT excluded — it ships (it
    #    carries the consumer-facing wiki-convention authority, §6).
    docs/grimoire/design/*) return 0 ;;                       # whole framework design subtree
    docs/grimoire/feature-playbook-validation.md) return 0 ;;
    docs/grimoire/issue-tracker-cost-spike.md) return 0 ;;
    docs/grimoire/issue-tracker-cost-validation.md) return 0 ;;
    docs/grimoire/sync-flow-audit.md) return 0 ;;
    docs/grimoire/docs-organization-design.md) return 0 ;;    # now internal (DS-1 §6)
    docs/grimoire/maintaining-grimoire.md) return 0 ;;        # root-only internal home
    docs/grimoire/authoring-grimoire-docs.md) return 0 ;;     # root-only internal doc-authoring guide (v3.46)
    # ── v3.41 "Clean-Room" CR-2 (clean-room-design.md §4): relocated operational
    #    docs + carve-outs. Mirror of build_distributables.py EXCLUDED_PATH_PREFIXES.
    docs/grimoire/integration-workflow.md) return 0 ;;        # framework-process doc
    docs/grimoire/qa-ledger.md) return 0 ;;                   # framework retrospective-QA ledger
    docs/grimoire/execution-profile-spike-s1.md) return 0 ;;  # framework-dev spike artifact
    docs/grimoire/token-efficiency-*) return 0 ;;             # token-efficiency-* study artifacts
    docs/grimoire/release-planning-*) return 0 ;;             # relocated historical release plans
    # release-planning carve-out: the ACTIVE in-flight plan stays at the top-level
    # docs/release-planning-v{X.Y}.md path (release-machinery + release-plan-guard
    # coupling) but must never ship; exclude the whole top-level prefix.
    docs/release-planning-*) return 0 ;;                      # top-level active plan (never ships)
    docs/release-planning/*) return 0 ;;                      # v3.45 relocated tier (active + archive)
    docs/version-history.md) return 0 ;;                      # exclude-and-seed: Grimoire's own log never ships
    .grimoire-golden/*) return 0 ;;                           # generated golden cache (v3.49) — never merged; locally derived
  esac
  return 1
}

# --------------------------------------------------------------------------
# Missing-symbol heuristic (#180) — warn when a 3-way merge silently drops a
# BASE definition that the UPSTREAM side still calls.
#
# Failure mode: LOCAL deleted helper functions; UPSTREAM changed a DIFFERENT
# region that calls them. The two diffs don't overlap, so git merge-file emits
# NO conflict marker — it just applies LOCAL's deletion and UPSTREAM's edit,
# leaving call-sites with no definition. The result looks syntactically complete
# but is broken at runtime. We can't fix the merge generally, but we can warn.
#
# Best-effort, language-agnostic-ish. Pure over its two text arguments so the
# self-test can drive it with canned content (no temp files needed).
# --------------------------------------------------------------------------
extract_defined_symbols() {
  # Arg: file CONTENT on stdin. Echo (one per line, deduped) the names this text
  # *defines* as a function/sub — covering the common shapes:
  #   sh/bash:  name() { ...   |  function name { ...
  #   py:       def name(...   |  (class name(...
  #   js/ts:    function name(...
  # Conservative: only well-anchored definition forms, never bare references.
  grep -hoE \
    -e '^[[:space:]]*[A-Za-z_][A-Za-z0-9_-]*[[:space:]]*\(\)[[:space:]]*\{?' \
    -e '^[[:space:]]*function[[:space:]]+[A-Za-z_][A-Za-z0-9_-]*' \
    -e '^[[:space:]]*def[[:space:]]+[A-Za-z_][A-Za-z0-9_]*' \
    -e '^[[:space:]]*class[[:space:]]+[A-Za-z_][A-Za-z0-9_]*' \
    2>/dev/null \
    | sed -E 's/\(\).*$//; s/^[[:space:]]*(function|def|class)[[:space:]]+//; s/[[:space:]]+$//' \
    | grep -E '^[A-Za-z_][A-Za-z0-9_-]*$' \
    | sort -u
}

is_defined_in() {
  # Args: <symbol> ; CONTENT on stdin. Returns 0 if stdin defines <symbol>.
  local sym="$1"
  extract_defined_symbols | grep -qxF "$sym"
}

is_called_in() {
  # Args: <symbol> ; CONTENT on stdin. Returns 0 if stdin references <symbol> as
  # a whole word somewhere — covering both paren call-sites (py/js: `name(`) and
  # bare invocation (sh: `name arg`). Callers only ask this once the symbol is
  # already known to be UNDEFINED in that same content, so any whole-word
  # occurrence is a genuine reference (it cannot be the absent definition).
  local sym="$1" esc
  esc="$(printf '%s' "$sym" | sed -E 's/[][\.^$*+?(){}|/-]/\\&/g')"
  grep -qE "(^|[^A-Za-z0-9_.])${esc}([^A-Za-z0-9_-]|$)" 2>/dev/null
}

find_dropped_definitions() {
  # Args: <upstream_content_file> <merged_content_file>. Echo (one per line) each
  # symbol that UPSTREAM defines, that is CALLED in the merged output, but that is
  # NOT defined anywhere in the merged output — i.e. a silently-dropped definition
  # the merged file still depends on. Empty output => nothing suspicious.
  local upf="$1" mgf="$2" sym merged_defs merged_body
  merged_defs="$(extract_defined_symbols < "$mgf")"
  merged_body="$(cat "$mgf")"
  while IFS= read -r sym; do
    [ -n "$sym" ] || continue
    # Defined in merged? then it's fine.
    printf '%s\n' "$merged_defs" | grep -qxF "$sym" && continue
    # Called anywhere in merged output? only then is the missing def load-bearing.
    printf '%s' "$merged_body" | is_called_in "$sym" || continue
    printf '%s\n' "$sym"
  done < <(extract_defined_symbols < "$upf")
}

# --------------------------------------------------------------------------
# Manual-resolution detection (#181). A file is "manually resolved" when it
# carries NO git conflict markers. Pure over stdin so the self-test can drive it.
# --------------------------------------------------------------------------
content_has_conflict_markers() {
  # CONTENT on stdin. Returns 0 iff a git conflict marker line is present.
  grep -qE '^(<<<<<<< |=======$|>>>>>>> )' 2>/dev/null
}

# --------------------------------------------------------------------------
# Pending-conflict fingerprint (#420) — distinguishes a re-presentation of an
# already-resolved conflict from a brand-new, never-yet-surfaced one.
#
# A file whose LOCAL copy has no conflict markers RIGHT NOW is ambiguous on
# its own: it could be an already-resolved re-presentation (safe to
# auto-advance the base), or it could be a brand-new conflict whose LOCAL
# content simply differs from both base and upstream (NOT safe to silently
# discard by auto-advancing — that would drop the conflict without ever
# showing it, a silent data-loss regression). The disambiguating fact is
# whether THIS EXACT (base, upstream) pairing was already written to LOCAL as
# a CONFLICT by a prior --apply run. Rather than mine git history (which
# cannot tell "this same conflict, still unresolved" apart from "a LATER,
# different conflict on the same path" once the path has ever been resolved
# once), record a small sentinel — a hash of (base, upstream) content — the
# moment a CONFLICT is actually written to disk, under
# .scaffold-conflict-pending/ (untracked local state, like
# .scaffold-sync-state.json; never synced, never committed). A later run only
# auto-advances when the CURRENT (base, upstream) pairing still matches the
# recorded fingerprint — any upstream change, or a base that already moved,
# invalidates it and the classic CONFLICT path applies. Defined here (before
# --self-test) so the self-test can exercise the real functions.
# --------------------------------------------------------------------------
conflict_pending_path() {  # Args: <rel>. Echo the sentinel file path for it.
  printf '%s/.scaffold-conflict-pending/%s\n' "$PROJECT_ROOT" "$1"
}

conflict_fingerprint() {  # Args: <base-file> <upstream-file>. Echo a hash pair.
  printf '%s %s\n' "$(git hash-object "$1" 2>/dev/null || echo none)" \
                    "$(git hash-object "$2" 2>/dev/null || echo none)"
}

record_conflict_pending() {  # Args: <rel> <base-file> <upstream-file>.
  local p; p="$(conflict_pending_path "$1")"
  mkdir -p "$(dirname "$p")"
  conflict_fingerprint "$2" "$3" > "$p"
}

conflict_pending_matches() {  # Args: <rel> <base-file> <upstream-file>.
  local p; p="$(conflict_pending_path "$1")"
  [ -f "$p" ] || return 1
  [ "$(cat "$p")" = "$(conflict_fingerprint "$2" "$3")" ]
}

clear_conflict_pending() {  # Args: <rel>. Drop the sentinel once resolved.
  rm -f "$(conflict_pending_path "$1")"
}

# --------------------------------------------------------------------------
# Additive-only diff3 conflict resolution (#198).
#
# Failure mode: `git merge-file` (diff3) conflicts whenever LOCAL's hunk and
# UPSTREAM's hunk touch overlapping lines relative to BASE — even when the two
# hunks are semantically compatible. The recurring, safe-to-automate case:
# LOCAL predates a section that BASE already carries (LOCAL never had it — a
# pure deletion relative to base, not a deliberate edit), and UPSTREAM still
# carries that section (whether or not upstream has ALSO changed further
# nearby). diff3 renders this as a hunk whose LOCAL side is EMPTY and whose
# UPSTREAM side is non-empty:
#     <<<<<<< local
#     =======
#     <upstream content>
#     >>>>>>> upstream
# An empty LOCAL side means local made no conflicting edit of its own in this
# region — there is nothing of local's to preserve, so "take upstream" is
# always safe. A hunk with ANY local content is left untouched (a genuine
# collision — both sides made a real, potentially conflicting edit) so this
# never silently discards a real customization.
#
# Pure over stdin so the self-test can drive it with canned merge-file output.
# Only ever COLLAPSES empty-local hunks; every other line (including any
# non-empty-local conflict hunk, markers and all) passes through unchanged.
#
# STATE-CONDITIONED PARSING (data-loss fix, reviewer-caught, post-#198):
# The original implementation matched the three marker patterns against EVERY
# line unconditionally, regardless of parser state. That is wrong: a hunk's
# genuine LOCAL or UPSTREAM content can itself contain a line that is BYTE-
# IDENTICAL to one of the marker patterns — not hypothetical, this very
# script's own self-test fixtures below contain literal `<<<<<<< local` /
# `=======` / `>>>>>>> upstream` lines, so any future upstream sync touching
# this file is a live occurrence. Matching those unconditionally caused the
# parser to misinterpret real hunk CONTENT as a structural boundary,
# discarding whatever was already buffered (including genuine non-empty local
# edits) with zero trace, and could silently misclassify a hunk with real
# local content as "empty" — collapsing it to MERGED with no conflict markers
# left to warn the operator. A second failure mode: a hunk missing its closing
# `>>>>>>> upstream` (malformed/truncated input) caused the parser to buffer
# forever with no flush, silently dropping all trailing content.
#
# The fix: an explicit three-state machine (OUTSIDE / LOCAL / UPSTREAM) where
# each marker pattern is only a valid transition in the ONE state that expects
# it; in every other state the "marker-shaped" line is ordinary content to be
# passed through or buffered verbatim. An unterminated hunk (LOCAL or UPSTREAM
# still open at end-of-input) is a hard failure for that file: flush the
# buffered markers/content back out (so the caller's content_has_conflict_markers
# check still sees markers and keeps the file classified CONFLICT, never
# MERGED) and log a clear error to stderr. Nothing is ever silently swallowed.
# --------------------------------------------------------------------------
resolve_additive_only_conflicts() {
  # MERGE-FILE OUTPUT (with conflict markers) on stdin; resolved content on
  # stdout. Idempotent: content with no conflict markers passes through as-is.
  awk '
    BEGIN { state = "OUTSIDE"; local_buf = ""; upstream_buf = ""; local_has_content = 0 }

    # OUTSIDE: only a genuine "<<<<<<< local" line opens a hunk. A line that
    # merely looks like "=======" or ">>>>>>> upstream" is not a valid
    # transition here (those only mean anything inside an open hunk) — it is
    # ordinary already-resolved content.
    state == "OUTSIDE" && /^<<<<<<< local$/ {
      state = "LOCAL"; local_buf = ""; upstream_buf = ""; local_has_content = 0; next
    }
    state == "OUTSIDE" { print; next }

    # LOCAL: only "=======" closes the local side. Any other line — including
    # one that happens to look like "<<<<<<< local" or ">>>>>>> upstream" — is
    # buffered as literal local-side content; the only valid next transition
    # from LOCAL is the separator.
    state == "LOCAL" && /^=======$/ { state = "UPSTREAM"; next }
    state == "LOCAL" {
      local_buf = local_buf $0 "\n"
      if (length($0) > 0) local_has_content = 1
      next
    }

    # UPSTREAM: only ">>>>>>> upstream" closes the hunk. Any other line —
    # including a marker-shaped one — is buffered as literal upstream-side
    # content, not a transition.
    state == "UPSTREAM" && /^>>>>>>> upstream$/ {
      state = "OUTSIDE"
      if (local_has_content) {
        # Genuine collision — reproduce the original hunk verbatim.
        printf "%s", "<<<<<<< local\n"
        printf "%s", local_buf
        printf "%s", "=======\n"
        printf "%s", upstream_buf
        printf "%s", ">>>>>>> upstream\n"
      } else {
        # Empty-local hunk — additive-only; take upstream, drop the markers.
        printf "%s", upstream_buf
      }
      next
    }
    state == "UPSTREAM" { upstream_buf = upstream_buf $0 "\n"; next }

    END {
      # Unterminated hunk at end-of-input: malformed/unexpected input. Do NOT
      # silently swallow it — flush the markers/content buffered so far (this
      # guarantees content_has_conflict_markers still finds a marker line, so
      # the caller keeps the file classified CONFLICT, never MERGED) and log a
      # clear, loud error so the operator knows this file needs a human look.
      if (state == "LOCAL" || state == "UPSTREAM") {
        print "resolve_additive_only_conflicts: ERROR — unterminated diff3 hunk (missing closing marker); forcing CONFLICT, no content dropped" > "/dev/stderr"
        printf "%s", "<<<<<<< local\n"
        printf "%s", local_buf
        if (state == "UPSTREAM") {
          printf "%s", "=======\n"
          printf "%s", upstream_buf
        }
      }
    }
  '
}

# --self-test: exercise transport resolution with no network/git, then exit.
if printf '%s\n' "$@" | grep -qx -- '--self-test'; then
  fails=0
  assert_eq() { [ "$2" = "$3" ] || { echo "FAIL: $1 (got '$2', want '$3')" >&2; fails=$((fails+1)); }; }
  assert_eq "explicit git"     "$(resolve_transport git     https://github.com/x/y.git v1.0 claude-code)" git
  assert_eq "explicit release" "$(resolve_transport release https://github.com/x/y.git v1.0 claude-code)" release
  _tl="$(mktemp -d)"; mkdir -p "$_tl/claude-code"
  assert_eq "auto local"   "$(resolve_transport auto "$_tl" v1.0 claude-code)" local
  rm -rf "$_tl"
  assert_eq "auto release" "$(_GH_AVAILABLE_OVERRIDE=1 resolve_transport auto https://github.com/x/y.git v3.23 claude-code)" release
  assert_eq "auto no-gh"   "$(_GH_AVAILABLE_OVERRIDE=0 resolve_transport auto https://github.com/x/y.git v3.23 claude-code)" git
  assert_eq "auto branch"  "$(_GH_AVAILABLE_OVERRIDE=1 resolve_transport auto https://github.com/x/y.git main  claude-code)" git
  assert_eq "auto non-gh-url" "$(_GH_AVAILABLE_OVERRIDE=1 resolve_transport auto https://gitlab.com/x/y.git v3.23 claude-code)" git
  _looks_like_version_tag v3.23 || { echo "FAIL: v3.23 should match" >&2; fails=$((fails+1)); }
  if _looks_like_version_tag main; then echo "FAIL: main should not match" >&2; fails=$((fails+1)); fi

  # channel -> asset pattern (v3.27)
  assert_eq "stable pattern" "$(_channel_asset_pattern claude-code stable)" "grimoire-claude-code-v*.zip"
  assert_eq "beta pattern"   "$(_channel_asset_pattern copilot beta)"      "grimoire-copilot-v*-beta.zip"
  if _channel_asset_pattern claude-code nightly 2>/dev/null; then echo "FAIL: bad channel should error" >&2; fails=$((fails+1)); fi

  # self-update helpers (#443, v3.91) — the raw-content URL used to fetch THIS
  # script's newest upstream bytes before the BMI-3 guard runs on a stale copy.
  assert_eq "self-update rel path (claude-code)" "$(self_update_rel_path claude-code)" \
    ".claude/skills/grm-sync-from-upstream/sync-from-upstream.sh"
  assert_eq "self-update rel path (copilot)" "$(self_update_rel_path copilot)" \
    "scripts/sync-from-upstream.sh"
  assert_eq "self-update raw url (github, .git suffix)" \
    "$(self_update_raw_url https://github.com/rhohn94/grimoire-framework.git main claude-code)" \
    "https://raw.githubusercontent.com/rhohn94/grimoire-framework/main/claude-code/.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh"
  assert_eq "self-update raw url (github, no suffix, copilot)" \
    "$(self_update_raw_url https://github.com/rhohn94/grimoire-framework main copilot)" \
    "https://raw.githubusercontent.com/rhohn94/grimoire-framework/main/copilot/scripts/sync-from-upstream.sh"
  assert_eq "self-update raw url (non-github => empty, best-effort no-op)" \
    "$(self_update_raw_url https://gitlab.com/x/y.git main claude-code)" ""

  # SHA256SUMS verification (v3.27) — exercised with temp files, no network.
  # Capture the rc explicitly so `set -e` does not abort on the non-zero returns.
  verify_rc() { verify_against_sha256sums "$1" "$2" >/dev/null 2>&1 && echo 0 || echo $?; }
  _sd="$(mktemp -d)"
  printf 'hello\n' > "$_sd/grimoire-claude-code-v9.9.zip"
  _h="$(_sha256_of "$_sd/grimoire-claude-code-v9.9.zip")"
  printf '%s  %s\n' "$_h" "grimoire-claude-code-v9.9.zip" > "$_sd/SHA256SUMS"
  assert_eq "checksum match => 0" "$(verify_rc "$_sd/grimoire-claude-code-v9.9.zip" "$_sd/SHA256SUMS")" 0
  printf 'deadbeef  grimoire-claude-code-v9.9.zip\n' > "$_sd/SHA256SUMS.bad"
  assert_eq "checksum mismatch => 1" "$(verify_rc "$_sd/grimoire-claude-code-v9.9.zip" "$_sd/SHA256SUMS.bad")" 1
  assert_eq "no sums => 2 (loud degrade)" "$(verify_rc "$_sd/grimoire-claude-code-v9.9.zip" "")" 2
  rm -rf "$_sd"

  # Integration-line detection (v3.38 BMI-3)
  _td="$(mktemp -d)"
  mkdir -p "$_td/.claude"
  # No grimoire-config.json -> default 'dev'
  assert_eq "int-line default"  "$(PROJECT_ROOT="$_td" detect_integration_line "$_td")" "dev"
  # config present but no branch-model key -> default 'dev'
  printf '{"schema-version":4,"name":"T","work-paradigm":{"value":"Noir"}}' > "$_td/.claude/grimoire-config.json"
  assert_eq "int-line no-key"   "$(PROJECT_ROOT="$_td" detect_integration_line "$_td")" "dev"
  # config with branch-model.integration-branch set
  printf '{"schema-version":4,"name":"T","work-paradigm":{"value":"Noir"},"branch-model":{"integration-branch":"experimental"}}' > "$_td/.claude/grimoire-config.json"
  assert_eq "int-line explicit" "$(PROJECT_ROOT="$_td" detect_integration_line "$_td")" "experimental"
  rm -rf "$_td"

  # is_excluded — framework-internal doc bulkhead (v3.39) + existing rules.
  excl_rc() { is_excluded "$1" && echo 0 || echo $?; }
  assert_eq "design subtree excluded"    "$(excl_rc docs/grimoire/design/foo-design.md)" 0
  assert_eq "design subtree nested excl"  "$(excl_rc docs/grimoire/design/sub/bar.md)"    0
  assert_eq "study artifact excluded"     "$(excl_rc docs/grimoire/sync-flow-audit.md)"   0
  assert_eq "docs-org now-internal excl"  "$(excl_rc docs/grimoire/docs-organization-design.md)" 0
  assert_eq "maintaining-grimoire excl"   "$(excl_rc docs/grimoire/maintaining-grimoire.md)"     0
  assert_eq "grimoire README NOT excl"    "$(excl_rc docs/grimoire/README.md)"            1
  # v3.41 CR-2 relocated operational docs + carve-outs.
  assert_eq "cr2 integration-workflow excl" "$(excl_rc docs/grimoire/integration-workflow.md)"    0
  assert_eq "version-design NOT excl"       "$(excl_rc docs/grimoire/version-design.md)"          1
  assert_eq "cr2 qa-ledger excl"            "$(excl_rc docs/grimoire/qa-ledger.md)"               0
  assert_eq "cr2 token-efficiency excl"     "$(excl_rc docs/grimoire/token-efficiency-audit.md)"  0
  assert_eq "cr2 relocated rel-plan excl"   "$(excl_rc docs/grimoire/release-planning-v1.0.md)"   0
  assert_eq "cr2 top-level rel-plan excl"   "$(excl_rc docs/release-planning-v3.41.md)"           0
  assert_eq "cr2 version-history excl"      "$(excl_rc docs/version-history.md)"                  0
  assert_eq "project-own design NOT excl" "$(excl_rc docs/design/bar-design.md)"          1
  assert_eq "existing roadmap rule kept"  "$(excl_rc docs/roadmap.md)"                    0

  # component-registry recognized-artifact detector (#REG-4, Pillar 4) — NOT
  # excluded from the walk; only routes the "both sides changed" merge step.
  regart_rc() { is_component_registry_artifact "$1" && echo 0 || echo $?; }
  assert_eq "registry artifact matched"     "$(regart_rc .claude/component-registry.json)" 0
  assert_eq "registry artifact excl check"  "$(excl_rc .claude/component-registry.json)"   1
  assert_eq "unrelated .claude file no match" "$(regart_rc .claude/grimoire-config.json)"  1
  assert_eq "nested registry path no match" "$(regart_rc foo/.claude/component-registry.json)" 1

  # component-registry structural-merge dispatch wiring (#REG-4) — exercises
  # the SAME merge_component_registry() function the main classification loop
  # calls (not just the Python engine's own --self-test, covered separately),
  # proving the wiring — not only the algorithm — routes correctly. Bypasses
  # the full CLI (its BMI-3 boundary guards are unrelated to this code path;
  # merge_component_registry() is pure over its file-path arguments).
  if [ -f "$REGISTRY_MERGE_ENGINE" ] && command -v python3 >/dev/null 2>&1; then
    _rt="$(mktemp -d)"
    printf '%s' '{"components":{"auth-core":{"version":"1.0.0"}}}' \
      > "$_rt/base.json"
    printf '%s' '{"components":{"auth-core":{"version":"1.0.0"},"local-widget":{"version":"1.0.0"}}}' \
      > "$_rt/local.json"
    printf '%s' '{"components":{"auth-core":{"version":"1.0.0"},"upstream-widget":{"version":"2.0.0"}}}' \
      > "$_rt/upstream.json"
    reg_status="$(merge_component_registry "$_rt/local.json" "$_rt/base.json" "$_rt/upstream.json" \
      "$_rt/out.json" "$_rt/conflicts.json")"
    assert_eq "registry wiring: disjoint additions -> clean" "$reg_status" "clean"
    merged_on_disk="$(cat "$_rt/out.json" 2>/dev/null || echo "")"
    if ! printf '%s' "$merged_on_disk" | grep -q "local-widget" || \
       ! printf '%s' "$merged_on_disk" | grep -q "upstream-widget"; then
      echo "FAIL: registry wiring — merged --out missing a disjoint addition: $merged_on_disk" >&2
      fails=$((fails+1))
    fi

    # Genuine same-id conflict — must report "conflict" and leave --out unwritten.
    printf '%s' '{"components":{"auth-core":{"version":"2.0.0-local"}}}' \
      > "$_rt/local2.json"
    printf '%s' '{"components":{"auth-core":{"version":"2.0.0-upstream"}}}' \
      > "$_rt/upstream2.json"
    reg_status2="$(merge_component_registry "$_rt/local2.json" "$_rt/base.json" "$_rt/upstream2.json" \
      "$_rt/out2.json" "$_rt/conflicts2.json")"
    assert_eq "registry wiring: genuine conflict -> conflict" "$reg_status2" "conflict"
    if [ -f "$_rt/out2.json" ]; then
      echo "FAIL: registry wiring — conflict path wrote --out (should be untouched)" >&2
      fails=$((fails+1))
    fi
    if ! grep -q "auth-core" "$_rt/conflicts2.json" 2>/dev/null; then
      echo "FAIL: registry wiring — conflicts-out missing the colliding id" >&2
      fails=$((fails+1))
    fi
    rm -rf "$_rt"
  fi

  # Dirty-tree check ignoring untracked files (#143) — clean tree with only
  # untracked "?? " entries must NOT count as dirty; tracked changes must.
  tracked_rc() { porcelain_has_tracked_changes "$1" && echo 0 || echo $?; }
  assert_eq "empty porcelain => clean"        "$(tracked_rc "")"                              1
  assert_eq "only untracked => clean (#143)"  "$(tracked_rc "?? .grimoire-archive/
?? .grimoire-source/.claude/")"                                                              1
  assert_eq "staged change => dirty"          "$(tracked_rc "M  src/foo.py")"                 0
  assert_eq "unstaged change => dirty"        "$(tracked_rc " M src/foo.py")"                 0
  assert_eq "mixed tracked+untracked => dirty" "$(tracked_rc " M src/foo.py
?? scratch.txt")"                                                                            0

  # BMI-3 Rule 3b escape-hatch divergence classifier (#144/#146/#162/#173).
  # `git cherry INT main` lines: "- <sha>" = patch already on INT (benign,
  # the integration line is merely ahead); "+ <sha>" = unreachable real work
  # (a genuine fork). The escape hatch proceeds on the former, HALTs on the latter.
  unreach_rc() { cherry_lines_show_unreachable_work "$1" && echo 0 || echo $?; }
  assert_eq "consecutive sync: empty cherry => ahead-only" "$(unreach_rc "")"                1
  assert_eq "promotion merge only => ahead-only"  "$(unreach_rc "- 341e674")"                1
  assert_eq "real fork => unreachable work"  "$(unreach_rc "+ 1030f23")"                     0
  assert_eq "mixed: one + among - => fork"   "$(unreach_rc "- 341e674
+ 46901b7")"                                                                                  0
  assert_eq "git-error sentinel => HALT"     "$(unreach_rc "+ error")"                        0

  # BMI-3 sync-continuation token (v3.90) — pure predicates, kept as bookkeeping
  # (#419 retired --allow-ahead; the main guard path no longer gates on these,
  # but the functions and their state file remain, and stay self-tested).
  cont_rc() { continuation_permits "$1" "$2" "$3" && echo 0 || echo $?; }
  assert_eq "no recorded sha => refuse"        "$(cont_rc "" abc123 "")"                     1
  assert_eq "empty current sha => refuse"      "$(cont_rc abc123 "" "")"                     1
  assert_eq "main moved => refuse"             "$(cont_rc abc123 def456 "")"                 1
  assert_eq "match + clean cherry => permit"   "$(cont_rc abc123 abc123 "")"                 0
  assert_eq "match + promotion-only => permit" "$(cont_rc abc123 abc123 "- 341e674")"        0
  assert_eq "match but real fork => refuse"    "$(cont_rc abc123 abc123 "+ 46901b7")"        1
  assert_eq "match but git error => refuse"    "$(cont_rc abc123 abc123 "+ error")"          1
  _cs="$(mktemp -d)"
  continuation_record "$_cs/state.json" 0123abcd dev
  assert_eq "record/read round-trip" "$(continuation_read_sha "$_cs/state.json")" 0123abcd
  assert_eq "absent state file => empty sha" "$(continuation_read_sha "$_cs/nope.json")" ""
  rm -rf "$_cs"
  assert_eq "continuation state file excluded from walk" "$(excl_rc .scaffold-sync-state.json)" 0
  assert_eq "pending-conflict state dir excluded from walk (#420)" "$(excl_rc .scaffold-conflict-pending/foo.sh)" 0

  # #443 sync-continuation bootstrap: fires ONLY on true absence (no token
  # ever recorded), never on a recorded-but-stale one — a stale token must
  # still hit the classic refusal, not be silently re-seeded.
  boot_rc() { continuation_should_bootstrap "$1" && echo 0 || echo $?; }
  assert_eq "no recorded sha => bootstrap"      "$(boot_rc "")"        0
  assert_eq "recorded sha (any) => not bootstrap" "$(boot_rc abc123)"  1

  # Hook atomic-replace classification (v3.90) — upstream-authoritative hooks.
  hook_rc() { is_hook_artifact "$1" && echo 0 || echo $?; }
  assert_eq "push-guard is a hook artifact"     "$(hook_rc .claude/hooks/push-guard.sh)"        0
  assert_eq "hook helper module is too"         "$(hook_rc .claude/hooks/_hook_common.py)"      0
  assert_eq "skill script is NOT a hook"        "$(hook_rc .claude/skills/grm-iterate/SKILL.md)" 1
  assert_eq "similarly-named non-hook path"     "$(hook_rc docs/hooks/notes.md)"                 1

  # Stale-namespacing pair detection (v3.90) — bare dir coexisting with grm-*.
  _nsd="$(mktemp -d)"
  mkdir -p "$_nsd/skills/grm-iterate" "$_nsd/skills/iterate" "$_nsd/skills/grm-clean-only"
  _pairs="$(stale_namespacing_pairs "$_nsd/skills")"
  assert_eq "coexisting pair detected" "$_pairs" "iterate/ -> grm-iterate/"
  rm -rf "$_nsd/skills/iterate"
  assert_eq "clean tree => no pairs" "$(stale_namespacing_pairs "$_nsd/skills")" ""
  # #308 alias pairs: bare role dir and grm-era ghost both pair with the
  # canonical grm-agent-* twin.
  mkdir -p "$_nsd/skills/grm-agent-scout" "$_nsd/skills/scout"
  assert_eq "role alias pair detected" "$(stale_namespacing_pairs "$_nsd/skills")" "scout/ -> grm-agent-scout/"
  rm -rf "$_nsd/skills/scout"; mkdir -p "$_nsd/skills/grm-scout"
  assert_eq "grm-era ghost pair detected" "$(stale_namespacing_pairs "$_nsd/skills")" "grm-scout/ -> grm-agent-scout/"
  rm -rf "$_nsd/skills/grm-scout"
  assert_eq "canonical twin alone => no pairs" "$(stale_namespacing_pairs "$_nsd/skills")" ""
  assert_eq "missing skills dir => no pairs" "$(stale_namespacing_pairs "$_nsd/absent")" ""
  rm -rf "$_nsd"

  # Missing-symbol heuristic (#180) — definition extraction across shapes.
  _defs="$(printf '%s\n' \
    "helper_a() {" \
    "function helper_b {" \
    "def py_fn(x):" \
    "class Thing:" \
    "  call_only(1)" | extract_defined_symbols | tr '\n' ' ')"
  case "$_defs" in *helper_a*) ;; *) echo "FAIL: extract sh-paren def" >&2; fails=$((fails+1));; esac
  case "$_defs" in *helper_b*) ;; *) echo "FAIL: extract function def" >&2; fails=$((fails+1));; esac
  case "$_defs" in *py_fn*)    ;; *) echo "FAIL: extract def def" >&2; fails=$((fails+1));; esac
  case "$_defs" in *Thing*)    ;; *) echo "FAIL: extract class def" >&2; fails=$((fails+1));; esac
  case "$_defs" in *call_only*) echo "FAIL: bare call wrongly treated as def" >&2; fails=$((fails+1));; esac

  # find_dropped_definitions: UPSTREAM defines+calls helper_x; merged calls it but
  # never defines it (LOCAL dropped the def) => helper_x reported. A symbol both
  # defined and called in merged, or defined-but-never-called, is NOT reported.
  _msd="$(mktemp -d)"
  printf 'helper_x() { echo hi; }\nmain() { helper_x; }\n'        > "$_msd/up"
  printf 'main() { helper_x; }\n'                                  > "$_msd/merged_bad"
  printf 'helper_x() { echo hi; }\nmain() { helper_x; }\n'        > "$_msd/merged_ok"
  printf 'main() { echo standalone; }\n'                           > "$_msd/merged_uncalled"
  assert_eq "dropped def, still called => warn" "$(find_dropped_definitions "$_msd/up" "$_msd/merged_bad")" "helper_x"
  assert_eq "def present in merged => no warn"  "$(find_dropped_definitions "$_msd/up" "$_msd/merged_ok")"  ""
  assert_eq "missing but uncalled => no warn"   "$(find_dropped_definitions "$_msd/up" "$_msd/merged_uncalled")" ""
  rm -rf "$_msd"

  # Conflict-marker detection (#181).
  marker_rc() { content_has_conflict_markers <<<"$1" && echo 0 || echo $?; }
  assert_eq "has <<< marker => 0" "$(marker_rc "ok
<<<<<<< local
x")" 0
  assert_eq "has ======= marker => 0" "$(marker_rc "=======")" 0
  assert_eq "clean text => 1"     "$(marker_rc "just resolved content")" 1

  # Pending-conflict fingerprint (#420) — the auto-advance safety gate: only a
  # RE-presentation of the EXACT (base, upstream) pairing already surfaced as
  # CONFLICT is safe to auto-resolve; a different or brand-new pairing is not.
  PROJECT_ROOT="$(mktemp -d)"
  _cf_b="$(mktemp)"; _cf_u="$(mktemp)"
  printf 'base v1\n' > "$_cf_b"; printf 'upstream v1\n' > "$_cf_u"
  cf_rc() { conflict_pending_matches "$@" && echo 0 || echo $?; }
  assert_eq "no sentinel recorded yet => no match" "$(cf_rc foo/bar.sh "$_cf_b" "$_cf_u")" 1
  record_conflict_pending "foo/bar.sh" "$_cf_b" "$_cf_u"
  assert_eq "recorded fingerprint matches same (base,upstream)" "$(cf_rc foo/bar.sh "$_cf_b" "$_cf_u")" 0
  assert_eq "different rel path has no sentinel of its own" "$(cf_rc foo/other.sh "$_cf_b" "$_cf_u")" 1
  printf 'upstream v2\n' > "$_cf_u"
  assert_eq "upstream changed since => fingerprint invalidated" "$(cf_rc foo/bar.sh "$_cf_b" "$_cf_u")" 1
  printf 'upstream v1\n' > "$_cf_u"   # restore the original pairing
  assert_eq "restored pairing matches again" "$(cf_rc foo/bar.sh "$_cf_b" "$_cf_u")" 0
  clear_conflict_pending "foo/bar.sh"
  assert_eq "cleared sentinel no longer matches" "$(cf_rc foo/bar.sh "$_cf_b" "$_cf_u")" 1
  rm -rf "$PROJECT_ROOT"; rm -f "$_cf_b" "$_cf_u"; PROJECT_ROOT=""

  # Additive-only diff3 conflict resolution (#198) — empty-local hunks collapse
  # to upstream; any hunk with real local content is left as a genuine conflict.
  _add_only="$(printf 'line1\n<<<<<<< local\n=======\n# additive upstream section\ncheck_boundary() {\n  echo hi\n}\n>>>>>>> upstream\nline2\n' | resolve_additive_only_conflicts)"
  assert_eq "additive-only hunk collapses to upstream" "$_add_only" "line1
# additive upstream section
check_boundary() {
  echo hi
}
line2"
  if printf '%s' "$_add_only" | content_has_conflict_markers; then
    echo "FAIL: additive-only resolution left conflict markers" >&2; fails=$((fails+1))
  fi

  _genuine_conflict="$(printf 'line1\n<<<<<<< local\nlocal edit\n=======\nupstream edit\n>>>>>>> upstream\nline2\n' | resolve_additive_only_conflicts)"
  assert_eq "genuine conflict (non-empty local) left untouched" "$_genuine_conflict" "line1
<<<<<<< local
local edit
=======
upstream edit
>>>>>>> upstream
line2"

  _mixed="$(printf 'line1\n<<<<<<< local\n=======\nadditive\n>>>>>>> upstream\nline2\n<<<<<<< local\nreal edit\n=======\nother edit\n>>>>>>> upstream\nline3\n' | resolve_additive_only_conflicts)"
  assert_eq "mixed file: additive hunk resolved, genuine hunk kept" "$_mixed" "line1
additive
line2
<<<<<<< local
real edit
=======
other edit
>>>>>>> upstream
line3"

  _no_markers="$(printf 'plain content\nno markers here\n' | resolve_additive_only_conflicts)"
  assert_eq "no conflict markers passes through unchanged" "$_no_markers" "plain content
no markers here"

  # Reviewer-caught data-loss regressions (post-#198): marker-shaped lines
  # embedded in genuine hunk CONTENT must never be misread as structural
  # transitions — they are only valid in the ONE state that expects them.
  #
  # (a) A hunk with a genuine non-empty LOCAL edit whose UPSTREAM content
  # contains a line that is itself byte-identical to "<<<<<<< local" (e.g.
  # this very script's own self-test fixtures further up this file). The old
  # unconditional-match parser treated that embedded line as a NEW hunk
  # boundary, discarding the enclosing hunk's real local edit with zero trace.
  # Because local_has_content is genuinely true, this is NOT additive-only —
  # the fixed parser must keep it a genuine conflict, markers and all, with
  # the real local edit intact.
  _embedded_marker_conflict="$(printf 'line1\n<<<<<<< local\nreal local customization\n=======\nupstream context before\n<<<<<<< local\nupstream context after (fake, just upstream content)\n>>>>>>> upstream\nline2\n' | resolve_additive_only_conflicts)"
  assert_eq "embedded marker-shaped upstream content does not discard real local edit" "$_embedded_marker_conflict" "line1
<<<<<<< local
real local customization
=======
upstream context before
<<<<<<< local
upstream context after (fake, just upstream content)
>>>>>>> upstream
line2"
  if ! printf '%s' "$_embedded_marker_conflict" | content_has_conflict_markers; then
    echo "FAIL: embedded-marker hunk with real local content must still report as CONFLICT" >&2; fails=$((fails+1))
  fi

  # (a2) Same embedded-marker hazard, but the outer hunk's LOCAL side is
  # genuinely empty — this must still correctly collapse (additive-only),
  # taking the upstream content (embedded marker-shaped lines and all)
  # verbatim, proving the fix does not over-correct into treating every
  # marker-shaped content line as a forced conflict.
  _embedded_marker_additive="$(printf 'line1\n<<<<<<< local\n=======\nfixture example:\n<<<<<<< local\n=======\n>>>>>>> upstream\nmore upstream code\n>>>>>>> upstream\nline2\n' | resolve_additive_only_conflicts)"
  assert_eq "embedded marker-shaped upstream content still collapses when local truly empty" "$_embedded_marker_additive" "line1
fixture example:
<<<<<<< local
=======
more upstream code
>>>>>>> upstream
line2"

  # (b) Unterminated hunk (missing closing ">>>>>>> upstream", e.g. malformed
  # or truncated diff3 output). The old parser buffered forever with no
  # flush, silently dropping all trailing content (including the real local
  # edit and everything after it). The fix must flush every buffered line
  # back out (nothing vanishes) and leave conflict markers in the output so
  # the caller's content_has_conflict_markers check forces this file to
  # remain classified CONFLICT rather than silently becoming MERGED.
  _unterminated="$(printf 'line1\n<<<<<<< local\nreal local edit\n=======\nupstream content that never closes\nline2 that must not vanish\n' | resolve_additive_only_conflicts 2>/dev/null)"
  assert_eq "unterminated hunk flushes all content, drops nothing" "$_unterminated" "line1
<<<<<<< local
real local edit
=======
upstream content that never closes
line2 that must not vanish"
  if ! printf '%s' "$_unterminated" | content_has_conflict_markers; then
    echo "FAIL: unterminated hunk must leave conflict markers so caller forces CONFLICT" >&2; fails=$((fails+1))
  fi
  _unterminated_stderr="$(printf 'line1\n<<<<<<< local\nreal local edit\n=======\nupstream content that never closes\nline2 that must not vanish\n' | resolve_additive_only_conflicts 2>&1 >/dev/null)"
  case "$_unterminated_stderr" in
    *"unterminated diff3 hunk"*) : ;;
    *) echo "FAIL: unterminated hunk must log a clear error to stderr" >&2; fails=$((fails+1)) ;;
  esac

  if [ "$fails" -eq 0 ]; then echo "sync-from-upstream self-test: all checks passed."; exit 0; fi
  echo "$fails self-test failure(s)." >&2; exit 1
fi

# --------------------------------------------------------------------------
# Self-corruption guard (#71)
# The --apply loop below can rewrite THIS script when the scaffold itself is
# part of the upstream changeset (sync-from-upstream updates its own file).
# Bash reads scripts incrementally from disk, so once the running file is
# overwritten mid-loop the next read lands on shifted bytes and the parser
# aborts (the footer never runs). Re-exec ONCE from an immutable temp copy so
# the bytes bash is executing are never the ones being rewritten.
# --------------------------------------------------------------------------
if [ -z "${SYNC_FROM_UPSTREAM_REEXEC:-}" ]; then
  _self_copy="$(mktemp "${TMPDIR:-/tmp}/sync-from-upstream.XXXXXX")"
  cp "$0" "$_self_copy"
  export SYNC_FROM_UPSTREAM_REEXEC="$_self_copy"
  exec bash "$_self_copy" "$@"
fi

# --------------------------------------------------------------------------
# Resolve project root (git toplevel if available, else CWD)
# --------------------------------------------------------------------------
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CONF="$PROJECT_ROOT/.scaffold-upstream.conf"
BASE_ROOT="$PROJECT_ROOT/.scaffold-base"
BACKUP_DIR="$PROJECT_ROOT/.scaffold-sync-backup/$(date +%Y%m%d-%H%M%S)"

APPLY=0; SHOW_DIFF=0; ADOPT_BASE=0; FORCE=0; MARK_RESOLVED=""; ALL_RESOLVED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)       APPLY=1 ;;
    --diff)        SHOW_DIFF=1 ;;
    --adopt-base)  ADOPT_BASE=1 ;;
    --force)       FORCE=1 ;;
    --mark-resolved)                  # per-file base advance (#181)
      shift; [ $# -gt 0 ] || { echo "ERROR: --mark-resolved needs a <file> argument." >&2; exit 2; }
      MARK_RESOLVED="$1" ;;
    --mark-resolved=*) MARK_RESOLVED="${1#--mark-resolved=}" ;;
    --all-resolved) ALL_RESOLVED=1 ;; # batch --mark-resolved for every CONFLICT (#420)
    --self-test)   : ;;  # handled in the transport block above (pre-re-exec)
    -h|--help)    sed -n '2,106p' "${BASH_SOURCE[0]}" | sed 's/^#\{0,1\} \{0,1\}//'; exit 0 ;;
    *)            echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# --------------------------------------------------------------------------
# Load config, then allow env to override
# --------------------------------------------------------------------------
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
UPSTREAM_REPO="${UPSTREAM_REPO:-}"
UPSTREAM_REF="${UPSTREAM_REF:-}"
FLAVOR="${FLAVOR:-}"
UPSTREAM_TRANSPORT="${UPSTREAM_TRANSPORT:-auto}"   # auto|local|release|git (v3.23)
UPSTREAM_CHANNEL="${UPSTREAM_CHANNEL:-stable}"      # stable|beta release channel (v3.27)
case "$UPSTREAM_CHANNEL" in
  stable|beta) ;;
  *) echo "ERROR: UPSTREAM_CHANNEL must be stable|beta (got '$UPSTREAM_CHANNEL')." >&2; exit 2 ;;
esac

if [ -z "$UPSTREAM_REPO" ]; then
  echo "ERROR: UPSTREAM_REPO is required." >&2
  echo "       Create $CONF (or set the env var):" >&2
  echo "         UPSTREAM_REPO=https://github.com/<you>/grimoire-framework.git" >&2
  exit 2
fi

# --------------------------------------------------------------------------
# Stale-upstream rename detection (non-destructive notice; pre-sync only)
# The scaffolding repo was renamed agentic-scaffolding -> grimoire-framework.
# A project pinned before the rename also predates the multi-paradigm system,
# so surface both the repoint and the paradigm-system pointer. We NEVER rewrite
# the conf here — we detect, report, and offer the exact one-line repoint.
# A no-op once UPSTREAM_REPO already targets grimoire-framework.
# --------------------------------------------------------------------------
case "$UPSTREAM_REPO" in
  *agentic-scaffolding*)
    echo "----------------------------------------------------------------" >&2
    echo "NOTICE: stale upstream detected — the scaffolding repo was renamed." >&2
    echo "  agentic-scaffolding  ->  grimoire-framework" >&2
    echo "  Your UPSTREAM_REPO still targets the old name:" >&2
    echo "    $UPSTREAM_REPO" >&2
    echo "  Repoint it (edit UPSTREAM_REPO in $CONF) to the new URL:" >&2
    echo "    UPSTREAM_REPO=https://github.com/rhohn94/grimoire-framework.git" >&2
    echo "  This sync was NOT modified — the conf is left untouched." >&2
    echo "  Paradigm system: this project was likely pinned before the" >&2
    echo "  multi-paradigm system existed. After repointing and syncing, see the" >&2
    echo "  'work-paradigm-switch' skill and .claude/paradigms/README.md to" >&2
    echo "  discover and select a work paradigm (Supervised / Weiss / Noir)." >&2
    echo "----------------------------------------------------------------" >&2
    echo >&2
    ;;
esac

# FLAVOR resolution: an explicit config/env value always wins; otherwise detect
# it from what THIS project actually has at its root — a Claude Code project has
# a .claude/ directory; a Copilot project has .github/copilot-instructions.md or
# a .github/prompts/ directory. So a project only needs to point at the upstream
# repo; which distribution folder to pull from follows from its own layout.
detect_flavor() {
  local claude=0 copilot=0
  [ -d "$PROJECT_ROOT/.claude" ] && claude=1
  { [ -f "$PROJECT_ROOT/.github/copilot-instructions.md" ] || [ -d "$PROJECT_ROOT/.github/prompts" ]; } && copilot=1
  if [ "$claude" -eq 1 ] && [ "$copilot" -eq 1 ]; then echo ambiguous; return; fi
  if [ "$claude" -eq 1 ]; then echo claude-code; return; fi
  if [ "$copilot" -eq 1 ]; then echo copilot; return; fi
  echo none
}

if [ -z "$FLAVOR" ]; then
  FLAVOR="$(detect_flavor)"
  case "$FLAVOR" in
    claude-code|copilot) echo "Detected flavor from project layout: $FLAVOR" ;;
    ambiguous) echo "ERROR: both Claude Code (.claude/) and Copilot (.github/) layouts are present — set FLAVOR explicitly in $CONF." >&2; exit 2 ;;
    *)         echo "ERROR: no Claude Code or Copilot layout detected at $PROJECT_ROOT — is this a scaffolded project? Set FLAVOR explicitly in $CONF if so." >&2; exit 2 ;;
  esac
fi
case "$FLAVOR" in claude-code|copilot) ;; *) echo "ERROR: FLAVOR must be claude-code or copilot (got '$FLAVOR')." >&2; exit 2 ;; esac

# --------------------------------------------------------------------------
# Self-update on a stale local copy (#443, v3.91) — before the BMI-3 boundary
# guard runs, best-effort fetch THIS script's newest bytes from upstream's
# `main` branch (always the newest tooling — UPSTREAM_REF may pin an old
# release) and re-exec them. A sync normally updates ITSELF as part of a
# project's regular --apply file-walk, but that update only takes effect on
# the NEXT invocation — the CURRENT invocation still runs whatever guard
# logic the local copy shipped with. A project whose local copy predates a
# boundary-guard fix (e.g. the #443 bootstrap-seeding logic just above) can
# never sync in the fix, because the OLD guard's stricter refusal blocks the
# very --apply that would deliver it (verified in sim-game: dev ahead by
# exactly the sync commit, main byte-identical to the merge base — the
# precisely-sanctioned scenario — still hard-blocked by a pre-v3.90 guard).
# Best-effort only: no curl, a non-GitHub/non-local-path remote, offline, or
# any fetch failure silently falls through to running the LOCAL copy — this
# must never be a hard failure, only an opportunistic upgrade.
# --------------------------------------------------------------------------
if [ "$APPLY" -eq 1 ] && [ -z "${SYNC_FROM_UPSTREAM_SELF_UPDATED:-}" ]; then
  _self_update_ref="${UPSTREAM_SELF_UPDATE_REF:-main}"
  _remote_copy="" _remote_tmp=""
  case "$UPSTREAM_REPO" in
    https://github.com/*|http://github.com/*)
      if command -v curl >/dev/null 2>&1; then
        _raw_url="$(self_update_raw_url "$UPSTREAM_REPO" "$_self_update_ref" "$FLAVOR")"
        if [ -n "$_raw_url" ]; then
          _remote_tmp="$(mktemp "${TMPDIR:-/tmp}/sync-from-upstream-remote.XXXXXX")"
          if curl -fsSL "$_raw_url" -o "$_remote_tmp" 2>/dev/null && [ -s "$_remote_tmp" ]; then
            _remote_copy="$_remote_tmp"
          else
            rm -f "$_remote_tmp"; _remote_tmp=""
          fi
        fi
      fi
      ;;
    /*)
      _local_candidate="$UPSTREAM_REPO/$FLAVOR/$(self_update_rel_path "$FLAVOR")"
      [ -f "$_local_candidate" ] && _remote_copy="$_local_candidate"
      ;;
  esac
  if [ -n "$_remote_copy" ] && ! cmp -s "$_remote_copy" "$0" 2>/dev/null; then
    echo "NOTICE: sync-from-upstream self-update — upstream ($_self_update_ref) carries a" >&2
    echo "  different copy of this script; re-executing it before the boundary guard" >&2
    echo "  runs, so a stale local guard can never block the fix that supersedes it (#443)." >&2
    export SYNC_FROM_UPSTREAM_SELF_UPDATED=1
    exec bash "$_remote_copy" "$@"
  fi
  [ -n "$_remote_tmp" ] && rm -f "$_remote_tmp"
fi

# --------------------------------------------------------------------------
# Guard: refuse --apply onto a dirty git tree
# --------------------------------------------------------------------------
if [ "$APPLY" -eq 1 ] && git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  _PORCELAIN="$(git -C "$PROJECT_ROOT" status --porcelain)"
  if porcelain_has_tracked_changes "$_PORCELAIN" && [ "$FORCE" -eq 0 ]; then
    echo "ERROR: project has uncommitted tracked changes — refusing to --apply." >&2
    echo "       Commit/stash first, or pass --force. (Untracked files do not block --apply.)" >&2
    exit 3
  fi
fi

# --------------------------------------------------------------------------
# Rule 3a/3b (BMI-3): keep a framework sync on the single integration line and
# off a divergent tree. Config key: branch-model.integration-branch (default dev).
# Rule 3a: HEAD must be the integration line, not main or any other branch.
# Rule 3b: main must NOT carry work the integration line lacks (a real fork).
#   The integration line being merely AHEAD of main (a prior sync's own
#   framework-version bump, a committed conflict resolution, or normal
#   in-between-releases drift) is SAFE and PROCEEDS BY DEFAULT (#419) — no
#   flag, no token required. Only a genuine fork (main carrying tree content
#   unreachable from the integration line) is refused, by the same
#   cherry-based predicate this rule has always used to tell the two apart.
#   #419 retired the `--allow-ahead` escape hatch entirely: a flag whose name
#   pattern-matches a [Safety Bypass Flag] should never have been the thing
#   standing between an autonomous agent and a safe, ahead-only sync.
# The full rule, recovery, and rationale are documented in the sync skill's
# SKILL.md (§BMI-3 boundary rules) — the authoritative design doc is
# framework-internal and not shipped to consumers, so it is NOT cited here.
# --------------------------------------------------------------------------
if [ "$APPLY" -eq 1 ] && git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  _INT="$(detect_integration_line "$PROJECT_ROOT")"
  _HEAD_BRANCH="$(git -C "$PROJECT_ROOT" symbolic-ref --short HEAD 2>/dev/null || echo "")"
  # Rule 3a: refuse if HEAD is main or not the integration line.
  if [ "$_HEAD_BRANCH" = "main" ] || [ "$_HEAD_BRANCH" != "$_INT" ]; then
    echo "ERROR (BMI-3): sync-from-upstream --apply refused on branch '$_HEAD_BRANCH'." >&2
    echo "  Framework syncs must run on the integration line ('$_INT'), not on '$_HEAD_BRANCH'." >&2
    echo "  Switch to '$_INT' (git switch $_INT) and re-run." >&2
    exit 3
  fi
  # Rule 3b: refuse only when main carries unreachable work (a real fork). An
  # integration line that is merely ahead of (or tree-identical to) main
  # proceeds unconditionally — the sync-continuation token file is still
  # recorded as an operator-facing boundary record, but nothing below branches
  # on it (#419).
  _STATE_FILE="$PROJECT_ROOT/.scaffold-sync-state.json"
  _CUR_MAIN_SHA="$(git -C "$PROJECT_ROOT" rev-parse main 2>/dev/null || echo "")"
  if ! git -C "$PROJECT_ROOT" diff --quiet "$_INT" main 2>/dev/null; then
    _CHERRY="$(main_only_cherry_lines "$PROJECT_ROOT" "$_INT" main)"
    if cherry_lines_show_unreachable_work "$_CHERRY"; then
      echo "ERROR (BMI-3): sync-from-upstream --apply refused — main has DIVERGED." >&2
      echo "  main carries commit(s) of work not on the integration line ('$_INT'):" >&2
      git -C "$PROJECT_ROOT" log --oneline --no-decorate "$_INT"..main 2>/dev/null | sed 's/^/    /' >&2
      echo "  This is a real fork, not the integration line merely being ahead. There is" >&2
      echo "  no flag or token that bypasses this." >&2
      echo "  Reconcile by merging main INTO '$_INT' (merge-forward); never reset" >&2
      echo "  across the fork (data loss)." >&2
      exit 3
    fi
    echo "NOTICE (BMI-3): integration line ('$_INT') is ahead of main; main carries no" >&2
    echo "  unreachable work, so this is a safe ahead-only state and the sync proceeds" >&2
    echo "  by default (#419). Promote the accumulated integration-line commits to" >&2
    echo "  main when convenient." >&2
    continuation_record "$_STATE_FILE" "$_CUR_MAIN_SHA" "$_INT"
  else
    # Clean boundary: record the continuation token as a boundary record.
    continuation_record "$_STATE_FILE" "$_CUR_MAIN_SHA" "$_INT"
  fi
fi

# --------------------------------------------------------------------------
# Obtain the upstream tree (local path used directly; else shallow clone)
# --------------------------------------------------------------------------
CLEANUP_TMP=""
cleanup() {
  [ -n "$CLEANUP_TMP" ] && rm -rf "$CLEANUP_TMP" || true
  # Remove the immutable self-copy created by the re-exec guard (#71).
  [ -n "${SYNC_FROM_UPSTREAM_REEXEC:-}" ] && rm -f "$SYNC_FROM_UPSTREAM_REEXEC" || true
}
trap cleanup EXIT

_shallow_clone() {  # into $1; honour UPSTREAM_REF if set
  # shellcheck disable=SC2086
  git clone --quiet --depth 1 ${UPSTREAM_REF:+--branch "$UPSTREAM_REF"} -- "$UPSTREAM_REPO" "$1"
}

TRANSPORT="$(resolve_transport "$UPSTREAM_TRANSPORT" "$UPSTREAM_REPO" "$UPSTREAM_REF" "$FLAVOR")"
case "$TRANSPORT" in
  local)
    SRC="$UPSTREAM_REPO" ;;
  release)
    CLEANUP_TMP="$(mktemp -d)"
    if fetch_release_asset "$CLEANUP_TMP"; then
      echo "Using release-asset transport (grimoire-$FLAVOR @ $UPSTREAM_REF)."
      SRC="$CLEANUP_TMP"
    else
      echo "Release transport unavailable — falling back to shallow git clone." >&2
      rm -rf "$CLEANUP_TMP"; CLEANUP_TMP="$(mktemp -d)"
      _shallow_clone "$CLEANUP_TMP" || { echo "ERROR: failed to clone '$UPSTREAM_REPO'." >&2; exit 4; }
      SRC="$CLEANUP_TMP"
    fi ;;
  *)  # git (default)
    CLEANUP_TMP="$(mktemp -d)"
    echo "Cloning upstream (shallow) ..."
    _shallow_clone "$CLEANUP_TMP" || { echo "ERROR: failed to clone '$UPSTREAM_REPO'." >&2; exit 4; }
    SRC="$CLEANUP_TMP" ;;
esac
FLAVOR_DIR="$SRC/$FLAVOR"
[ -d "$FLAVOR_DIR" ] || { echo "ERROR: upstream has no '$FLAVOR/' directory." >&2; exit 4; }

# is_excluded() is defined ABOVE the --self-test block so the self-test can
# exercise the real function (it shields project-owned/template files and the
# v3.39 framework-internal doc set from the file-merge walk).

backup() {  # back up an existing local file before rewriting it
  local l="$1" rel="$2"
  mkdir -p "$BACKUP_DIR/$(dirname "$rel")"; cp "$l" "$BACKUP_DIR/$rel"
}
set_base() {  # record upstream as the new base for this file
  local u="$1" b="$2"
  [ "$APPLY" -eq 1 ] || [ "$ADOPT_BASE" -eq 1 ] || return 0
  mkdir -p "$(dirname "$b")"; cp "$u" "$b"
}

n_symbol_warn=0; symbol_warnings=""
warn_dropped_definitions() {  # #180 — loud warning for call-without-definition
  local u="$1" mg="$2" rel="$3" missing
  missing="$(find_dropped_definitions "$u" "$mg")"
  [ -n "$missing" ] || return 0
  n_symbol_warn=$((n_symbol_warn+1))
  symbol_warnings="${symbol_warnings}\n    $rel: $(printf '%s' "$missing" | tr '\n' ' ')"
  echo "  !! WARNING (#180): merge of '$rel' references symbol(s) UPSTREAM defines" >&2
  echo "     but that are NOT defined in the merged output (call-site without" >&2
  echo "     definition — likely a BASE definition LOCAL dropped that did not" >&2
  echo "     produce a conflict marker). Verify before trusting this merge:" >&2
  printf '%s\n' "$missing" | sed 's/^/       - /' >&2
}

# --------------------------------------------------------------------------
# --mark-resolved <file> (#181) — per-file base advance.
# A resolved CONFLICT file re-conflicts on every future --apply because its
# recorded base is deliberately NOT advanced (so an unresolved conflict is never
# lost). Once you HAVE resolved it, advance the base for that ONE file to the
# current upstream content, so a re-run sees no remaining BASE-vs-UPSTREAM delta
# to re-merge. Unlike --adopt-base (which advances ALL files at once), this is
# surgical: other files' provenance is untouched. Writes only .scaffold-base/<rel>.
# --------------------------------------------------------------------------
if [ -n "$MARK_RESOLVED" ]; then
  rel_mr="${MARK_RESOLVED#./}"
  rel_mr="${rel_mr#"$PROJECT_ROOT"/}"   # accept an absolute or project-relative path
  U_mr="$FLAVOR_DIR/$rel_mr"; L_mr="$PROJECT_ROOT/$rel_mr"; B_mr="$BASE_ROOT/$rel_mr"
  if is_excluded "$rel_mr"; then
    echo "ERROR: '$rel_mr' is excluded from sync — it has no managed base to advance." >&2
    exit 2
  fi
  if [ ! -f "$U_mr" ]; then
    echo "ERROR: '$rel_mr' does not exist in upstream ($FLAVOR/) — cannot mark it resolved." >&2
    exit 2
  fi
  if [ -f "$L_mr" ] && content_has_conflict_markers < "$L_mr"; then
    echo "WARNING: '$rel_mr' still contains git conflict markers." >&2
    echo "         Marking its base as resolved now will make a future sync STOP" >&2
    echo "         re-conflicting it, leaving the unresolved markers in place." >&2
    echo "         Resolve the markers first, then re-run --mark-resolved. (Refusing.)" >&2
    exit 3
  fi
  mkdir -p "$(dirname "$B_mr")"; cp "$U_mr" "$B_mr"
  clear_conflict_pending "$rel_mr"
  echo "Marked resolved: advanced base for '$rel_mr' to current upstream content."
  echo "  Future syncs will no longer re-merge it (other files' bases untouched)."
  exit 0
fi

# --------------------------------------------------------------------------
# --all-resolved (#420) — batch form of --mark-resolved: advances the base for
# EVERY currently-conflicted file in one invocation, instead of one
# --mark-resolved per file. A file is "currently conflicted" by the same test
# the report loop uses: both sides changed since the recorded base, and the
# 3-way merge (after the #198 additive-only auto-resolve) still leaves genuine
# conflict markers. For each such file: if LOCAL no longer contains conflict
# markers (already hand-resolved, or resolved via a prior --mark-resolved run
# outside this batch), its base is advanced, same as the single-file form; if
# LOCAL still carries markers, it is reported and SKIPPED — never
# force-resolved, so a genuinely unresolved file is never silently lost.
# --------------------------------------------------------------------------
if [ "$ALL_RESOLVED" -eq 1 ]; then
  ar_resolved=0; ar_skipped=0; ar_resolved_list=""; ar_skipped_list=""
  while IFS= read -r rel_ar; do
    rel_ar="${rel_ar#./}"
    is_excluded "$rel_ar" && continue
    is_hook_artifact "$rel_ar" && continue   # hooks never conflict (atomic-replace)
    U_ar="$FLAVOR_DIR/$rel_ar"; L_ar="$PROJECT_ROOT/$rel_ar"; B_ar="$BASE_ROOT/$rel_ar"
    [ -f "$L_ar" ] || continue               # NEW file — nothing to resolve
    [ -f "$B_ar" ] || continue               # no base — REVIEW, not CONFLICT
    diff -q "$U_ar" "$L_ar" >/dev/null 2>&1 && continue   # in-sync
    diff -q "$L_ar" "$B_ar" >/dev/null 2>&1 && continue   # UPDATE-only, no conflict
    diff -q "$U_ar" "$B_ar" >/dev/null 2>&1 && continue   # local-only edits, no conflict
    # both sides changed since base — probe whether the 3-way merge conflicts.
    probe_merged="$(mktemp)"
    if git merge-file -p -L local -L base -L upstream "$L_ar" "$B_ar" "$U_ar" > "$probe_merged" 2>/dev/null; then
      rm -f "$probe_merged"; continue        # clean MERGE, not a conflict
    fi
    probe_resolved="$(mktemp)"
    resolve_additive_only_conflicts < "$probe_merged" > "$probe_resolved"
    rm -f "$probe_merged"
    if ! content_has_conflict_markers < "$probe_resolved"; then
      rm -f "$probe_resolved"; continue      # #198 auto-resolves cleanly
    fi
    rm -f "$probe_resolved"
    # genuinely a CONFLICT candidate.
    if content_has_conflict_markers < "$L_ar"; then
      echo "  SKIP (still has conflict markers): $rel_ar" >&2
      ar_skipped_list="${ar_skipped_list}\n    $rel_ar"; ar_skipped=$((ar_skipped+1))
      continue
    fi
    mkdir -p "$(dirname "$B_ar")"; cp "$U_ar" "$B_ar"
    clear_conflict_pending "$rel_ar"
    ar_resolved_list="${ar_resolved_list}\n    $rel_ar"; ar_resolved=$((ar_resolved+1))
  done < <(cd "$FLAVOR_DIR" && find . -type f | sort)
  echo "--all-resolved: advanced base for $ar_resolved file(s); skipped $ar_skipped still-unresolved file(s)."
  [ -n "$ar_resolved_list" ] && { echo "Resolved (base advanced to current upstream content):"; printf "%b\n" "$ar_resolved_list"; }
  [ -n "$ar_skipped_list" ]  && { echo "Skipped (still contain conflict markers — resolve, then re-run --all-resolved or --mark-resolved):"; printf "%b\n" "$ar_skipped_list"; }
  [ "$ar_resolved" -eq 0 ] && [ "$ar_skipped" -eq 0 ] && echo "No currently-conflicted files found."
  [ "$ar_skipped" -gt 0 ] && exit 1
  exit 0
fi

# --------------------------------------------------------------------------
# Report header
# --------------------------------------------------------------------------
echo "sync-from-upstream"
echo "  upstream: $UPSTREAM_REPO${UPSTREAM_REF:+ @ $UPSTREAM_REF}  (flavor: $FLAVOR)"
echo "  project:  $PROJECT_ROOT"
if   [ "$ADOPT_BASE" -eq 1 ]; then echo "  mode:     ADOPT-BASE (record upstream as base; local untouched)"
elif [ "$APPLY" -eq 1 ];      then echo "  mode:     APPLY (writing changes; backups in .scaffold-sync-backup/)"
else                               echo "  mode:     dry-run (no changes; --apply to write)"; fi
echo

n_new=0; n_update=0; n_merged=0; n_conflict=0; n_review=0; n_local=0; n_insync=0; n_replaced=0; n_resolved_auto=0
conflicts=""; reviews=""; news=""; replaced=""; resolved_auto=""

while IFS= read -r rel; do
  rel="${rel#./}"
  is_excluded "$rel" && continue
  U="$FLAVOR_DIR/$rel"; L="$PROJECT_ROOT/$rel"; B="$BASE_ROOT/$rel"

  if [ "$ADOPT_BASE" -eq 1 ]; then
    set_base "$U" "$B"; printf "  %-10s %s\n" "base-set" "$rel"; continue
  fi

  if [ ! -f "$L" ]; then
    printf "  %-10s %s\n" "NEW" "$rel"; news="${news}\n    $rel"; n_new=$((n_new+1))
    if [ "$APPLY" -eq 1 ]; then mkdir -p "$(dirname "$L")"; cp "$U" "$L"; set_base "$U" "$B"; clear_conflict_pending "$rel"; fi
    continue
  fi

  if diff -q "$U" "$L" >/dev/null 2>&1; then
    printf "  %-10s %s\n" "in-sync" "$rel"; n_insync=$((n_insync+1)); set_base "$U" "$B"
    [ "$APPLY" -eq 1 ] && clear_conflict_pending "$rel"
    continue
  fi

  # L and U differ — hooks are replaced wholesale (v3.90), never 3-way merged:
  # guard logic is upstream-authoritative; project behavior lives in
  # grimoire-config.json. Local copy is backed up and the replacement reported.
  if is_hook_artifact "$rel"; then
    printf "  %-10s %s  (hook — upstream-authoritative; local behavior belongs in grimoire-config.json)\n" "REPLACED" "$rel"
    replaced="${replaced}\n    $rel"; n_replaced=$((n_replaced+1))
    [ "$SHOW_DIFF" -eq 1 ] && diff -u "$L" "$U" 2>/dev/null | sed 's/^/      /' || true
    if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$U" "$L"; set_base "$U" "$B"; clear_conflict_pending "$rel"; fi
    continue
  fi

  if [ ! -f "$B" ]; then
    printf "  %-10s %s  (no base — keeping local; --adopt-base to set provenance)\n" "REVIEW" "$rel"
    reviews="${reviews}\n    $rel"; n_review=$((n_review+1))
    [ "$SHOW_DIFF" -eq 1 ] && diff -u "$L" "$U" 2>/dev/null | sed 's/^/      /' || true
    continue
  fi
  if diff -q "$L" "$B" >/dev/null 2>&1; then
    printf "  %-10s %s\n" "UPDATE" "$rel"; n_update=$((n_update+1))     # local unchanged since base
    [ "$SHOW_DIFF" -eq 1 ] && diff -u "$L" "$U" 2>/dev/null | sed 's/^/      /' || true
    if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$U" "$L"; set_base "$U" "$B"; clear_conflict_pending "$rel"; fi
    continue
  fi
  if diff -q "$U" "$B" >/dev/null 2>&1; then
    printf "  %-10s %s\n" "local" "$rel"; n_local=$((n_local+1)); continue   # upstream unchanged; keep local edits
  fi

  # both sides changed since base -> 3-way merge.
  #
  # The component registry (#REG-4, Pillar 4 distribution) is a structured
  # id-keyed JSON map, not free-form text — route it to the structural merge
  # engine instead of the textual git merge-file below, which false-conflicts
  # on two disjoint component additions (both touch the same closing-brace /
  # trailing-comma region). See component_registry_merge.py's module docstring.
  if is_component_registry_artifact "$rel" && [ -f "$REGISTRY_MERGE_ENGINE" ] \
      && command -v python3 >/dev/null 2>&1; then
    reg_merged="$(mktemp)"
    reg_conflicts="$(mktemp)"
    if [ "$(merge_component_registry "$L" "$B" "$U" "$reg_merged" "$reg_conflicts")" = "clean" ]; then
      printf "  %-10s %s  (component-registry structural merge, #REG-4)\n" "MERGED" "$rel"
      n_merged=$((n_merged+1))
      if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$reg_merged" "$L"; set_base "$U" "$B"; clear_conflict_pending "$rel"; fi
      [ "$SHOW_DIFF" -eq 1 ] && diff -u "$L" "$reg_merged" 2>/dev/null | sed 's/^/      /' || true
    else
      reg_ids="$(python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as fh:
        report = json.load(fh)
    print(', '.join(sorted(c['id'] for c in report.get('conflicts', []))))
except Exception:
    pass
" "$reg_conflicts" 2>/dev/null || true)"
      printf "  %-10s %s  (component-registry entry conflict — id(s): %s; --out untouched, resolve by hand, #REG-4)\n" \
        "CONFLICT" "$rel" "${reg_ids:-unknown}"
      conflicts="${conflicts}\n    $rel"; n_conflict=$((n_conflict+1))
      # Local is intentionally left untouched (no textual markers embedded in
      # the JSON, unlike the generic CONFLICT path below) — the structural
      # engine's own non-destructive contract. Base is NOT advanced either way.
      if [ "$APPLY" -eq 1 ]; then record_conflict_pending "$rel" "$B" "$U"; fi
    fi
    rm -f "$reg_merged" "$reg_conflicts"
    continue
  fi

  merged="$(mktemp)"
  if git merge-file -p -L local -L base -L upstream "$L" "$B" "$U" > "$merged" 2>/dev/null; then
    printf "  %-10s %s\n" "MERGED" "$rel"; n_merged=$((n_merged+1))
    # #180: a clean (markerless) merge can still be broken — LOCAL may have
    # deleted a definition UPSTREAM still calls, in a non-overlapping region.
    warn_dropped_definitions "$U" "$merged" "$rel"
    if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$merged" "$L"; set_base "$U" "$B"; clear_conflict_pending "$rel"; fi
  else
    # #198: diff3 can conflict on a hunk that is actually additive-only — LOCAL
    # predates a section BASE+UPSTREAM both carry, so LOCAL's side of the hunk
    # is empty. Collapse only those empty-local hunks (take upstream); any hunk
    # where LOCAL has real content is left as a genuine conflict, untouched.
    resolved="$(mktemp)"
    resolve_additive_only_conflicts < "$merged" > "$resolved"
    if ! content_has_conflict_markers < "$resolved"; then
      mv "$resolved" "$merged"
      printf "  %-10s %s  (diff3 additive-only conflict auto-resolved to upstream, #198)\n" "MERGED" "$rel"
      n_merged=$((n_merged+1))
      warn_dropped_definitions "$U" "$merged" "$rel"
      if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$merged" "$L"; set_base "$U" "$B"; clear_conflict_pending "$rel"; fi
      [ "$SHOW_DIFF" -eq 1 ] && diff -u "$L" "$merged" 2>/dev/null | sed 's/^/      /' || true
      rm -f "$resolved"
      continue
    fi
    rm -f "$resolved"
    # #181/#420: if LOCAL has no conflict markers AND this exact (base,
    # upstream) pairing was already written to LOCAL as a CONFLICT by a prior
    # --apply run (conflict_pending_matches — the safety gate that stops a
    # brand-new, never-yet-shown conflict from being silently auto-resolved),
    # the prior round's conflict was already resolved by hand (or via
    # --mark-resolved) but its base was never advanced — a fresh 3-way merge
    # re-conflicts on EVERY subsequent sync even though nothing actually
    # needs re-resolving. Auto-advance the base to the current upstream
    # content instead (exactly what --mark-resolved would do) and leave LOCAL
    # untouched — never overwrite a standing resolution with fresh markers.
    # This was the "same conflict re-presents on every sync" trap (#420);
    # reported as RESOLVED, not CONFLICT, and counts toward neither n_merged
    # nor n_conflict. A GENUINE first-time conflict, or a DIFFERENT conflict
    # against a base/upstream pairing that was never recorded (e.g. upstream
    # changed again since the last CONFLICT), always falls through to the
    # CONFLICT branch below, exactly as before #420.
    if [ -f "$L" ] && ! content_has_conflict_markers < "$L" && conflict_pending_matches "$rel" "$B" "$U"; then
      printf "  %-10s %s  (already resolved locally — base auto-advanced, #420)\n" "RESOLVED" "$rel"
      resolved_auto="${resolved_auto}\n    $rel"; n_resolved_auto=$((n_resolved_auto+1))
      if [ "$APPLY" -eq 1 ]; then set_base "$U" "$B"; clear_conflict_pending "$rel"; fi
      rm -f "$merged"
      continue
    fi
    printf "  %-10s %s  (conflict markers; resolve, do NOT auto-advance base)\n" "CONFLICT" "$rel"
    conflicts="${conflicts}\n    $rel"; n_conflict=$((n_conflict+1))
    # #180: the UPSTREAM side of a conflict may reference a definition LOCAL
    # dropped that ends up outside the marked regions — surface it too.
    warn_dropped_definitions "$U" "$merged" "$rel"
    if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$merged" "$L"; record_conflict_pending "$rel" "$B" "$U"; fi   # base NOT advanced
  fi
  [ "$SHOW_DIFF" -eq 1 ] && diff -u "$L" "$merged" 2>/dev/null | sed 's/^/      /' || true
  rm -f "$merged"
done < <(cd "$FLAVOR_DIR" && find . -type f | sort)

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
echo
echo "----------------------------------------------------------------"
if [ "$ADOPT_BASE" -eq 1 ]; then
  echo "Base recorded in .scaffold-base/ from upstream. Local files untouched."
  # #199: --adopt-base's own output (the untracked .scaffold-base/ it just wrote)
  # must never look like a reason --apply would refuse. Tell the operator the
  # correct next step explicitly instead of letting them discover --force by
  # trial and error. The dirty-tree guard already ignores untracked-only state
  # (porcelain_has_tracked_changes, #143) — --force is only load-bearing when
  # tracked files are ALSO dirty, so the hint reflects the tree's real state.
  if git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    _ADOPT_PORCELAIN="$(git -C "$PROJECT_ROOT" status --porcelain)"
    if porcelain_has_tracked_changes "$_ADOPT_PORCELAIN"; then
      echo "Next step: tracked files are also uncommitted — re-run with"
      echo "  --apply --force (or commit/stash the tracked changes first)."
    else
      echo "Next step: re-run with --apply now — the only uncommitted state is"
      echo "  the untracked .scaffold-base/ just recorded, which never blocks --apply."
    fi
  fi
  exit 0
fi
echo "Summary: NEW=$n_new UPDATE=$n_update MERGED=$n_merged REPLACED=$n_replaced RESOLVED=$n_resolved_auto CONFLICT=$n_conflict REVIEW=$n_review local-only-edits=$n_local in-sync=$n_insync"
[ -n "$news" ]      && { echo "New files (generic — re-specialize placeholders after apply):"; printf "%b\n" "$news"; }
[ -n "$replaced" ]  && { echo "REPLACED hooks (upstream-authoritative; local copies backed up — if a"; echo "  replaced hook carried project-specific behavior, move it into"; echo "  .claude/grimoire-config.json, never back into hook code):"; printf "%b\n" "$replaced"; }
[ -n "$resolved_auto" ] && { echo "RESOLVED (already hand-resolved locally — base auto-advanced, #420, LOCAL untouched):"; printf "%b\n" "$resolved_auto"; }
[ -n "$conflicts" ] && { echo "CONFLICTS to resolve (git markers written on --apply):"; printf "%b\n" "$conflicts"; }
[ -n "$reviews" ]   && { echo "REVIEW (differ, no base — kept local; merge by hand or --adopt-base):"; printf "%b\n" "$reviews"; }
[ "$n_symbol_warn" -gt 0 ] && { echo "MISSING-SYMBOL WARNINGS (#180 — call-site without definition; verify these merges):"; printf "%b\n" "$symbol_warnings"; }
if [ "$APPLY" -eq 1 ]; then
  echo
  # --------------------------------------------------------------------------
  # Cleanup-on-success (v3.52 Lane F): a successful apply with zero unresolved
  # conflicts no longer needs the transient rollback backups, so remove the
  # whole .scaffold-sync-backup/ tree. When conflicts remain the backups are
  # kept so the user can roll back. Self-contained, idempotent, no effect on the
  # merge/resolve logic above.
  # --------------------------------------------------------------------------
  if [ "$n_conflict" -eq 0 ]; then
    rm -rf "$PROJECT_ROOT/.scaffold-sync-backup" 2>/dev/null || true
    echo "Applied cleanly. Transient backups removed (.scaffold-sync-backup/)."
  else
    echo "Applied. Backups of rewritten files kept (unresolved conflicts): ${BACKUP_DIR#"$PROJECT_ROOT"/}"
  fi
  echo "Review the diff and commit. Resolve any CONFLICT files, then re-run to advance their base."
  echo
  echo "IMPORTANT (BMI-3 Rule 3c — separate commits):"
  echo "  Commit this framework-sync output as its OWN commit BEFORE running"
  echo "  design-language-adapt (Aura vendoring). Never bundle both into one commit —"
  echo "  separate commits keep the collision surface small and any later"
  echo "  reconciliation incremental rather than all-or-nothing."
else
  echo
  echo "Dry-run only. Re-run with --apply to write (with backups), or --diff for full diffs."
fi

# --------------------------------------------------------------------------
# Adoption phase — runs only after a clean --apply (not dry-run, not --adopt-base)
# Reads feature-manifest.md, computes the delta against framework-version,
# and emits the pending-adoption list.  Judgment (offer/auto-run per paradigm,
# framework-version write) lives in SKILL.md Step 4.5; this block provides
# the mechanical scaffolding the agent acts on.
# --------------------------------------------------------------------------
if [ "$APPLY" -eq 0 ] || [ "$ADOPT_BASE" -eq 1 ]; then
  exit 0   # dry-run or adopt-base: skip adoption phase entirely
fi

# Precondition: zero unresolved CONFLICT files.
if [ "$n_conflict" -gt 0 ]; then
  echo
  echo "----------------------------------------------------------------"
  echo "Adoption phase skipped: unresolved CONFLICT files remain."
  echo "  Resolve them (re-run the sync to advance their base),"
  echo "  then sync again to run the adoption phase."
  exit 0
fi

# Stale namespacing surfaced mechanically (v3.90) — before the manifest check,
# so a stale pre-rename manifest path can never hide the survivors.
_NS_PAIRS="$(stale_namespacing_pairs "$PROJECT_ROOT/.claude/skills")"
if [ -n "$_NS_PAIRS" ]; then
  echo
  echo "----------------------------------------------------------------"
  echo "Stale skill namespacing detected — bare-named dirs coexist with grm-*:"
  printf '%s\n' "$_NS_PAIRS" | sed 's/^/    /'
  echo "  The synced grm-* copies are authoritative; the bare-named twins are"
  echo "  pre-v3.42 survivors that keep surfacing stale skills. Complete the"
  echo "  cutover with the namespacing migrate (archives to .grimoire-archive/,"
  echo "  then removes; rewrites references):"
  echo "    python3 .claude/skills/grm-sync-from-upstream/grm_namespacing.py --root . --apply"
  echo "  MIGRATION RULE: it moves user-referenceable dirs — OFFER it with one"
  echo "  explicit confirmation (all paradigms, Noir included); NEVER auto-run."
  echo "  Preview first with --dry-run. Full procedure: reference.md Step 4.55."
fi

MANIFEST="$PROJECT_ROOT/.claude/skills/grm-sync-from-upstream/feature-manifest.md"
CONFIG="$PROJECT_ROOT/.claude/grimoire-config.json"

if [ ! -f "$MANIFEST" ]; then
  echo
  echo "----------------------------------------------------------------"
  echo "Adoption phase: feature-manifest.md not found — skipping."
  echo "  (Run --adopt-base after establishing the scaffold base, or"
  echo "   check that upstream ships feature-manifest.md.)"
  exit 0
fi

echo
echo "----------------------------------------------------------------"
echo "Adoption phase"

# Read framework-version from grimoire-config.json (absent = no-base fallback).
FRAMEWORK_VERSION=""
if [ -f "$CONFIG" ] && command -v python3 >/dev/null 2>&1; then
  FRAMEWORK_VERSION="$(python3 -c "
import json, sys
try:
  c = json.load(open('$CONFIG'))
  print(c.get('framework-version', ''))
except Exception:
  print('')
" 2>/dev/null)" || FRAMEWORK_VERSION=""
fi

if [ -n "$FRAMEWORK_VERSION" ]; then
  echo "  project framework-version: $FRAMEWORK_VERSION"
else
  echo "  project framework-version: (absent — evaluating all features by detect)"
fi

# Emit the manifest summary so the agent can read and act on it.
# The agent (SKILL.md Step 4.5) interprets the manifest table:
#   - With framework-version: collect entries where introduced-in > framework-version,
#     then run detect on each; offer/adopt what detect reports not-adopted.
#   - Without framework-version: evaluate all entries by detect.
echo
echo "  Feature manifest: $MANIFEST"
echo "  Paradigm note: check .claude/grimoire-config.json work-paradigm.value."
echo "    Noir   => auto-run adopt for each pending feature."
echo "    Supervised/Weiss => offer each adoption individually."
echo
echo "  Pending adoption check — agent: read the manifest table and for each entry:"
if [ -n "$FRAMEWORK_VERSION" ]; then
  echo "    1. Skip entries where introduced-in <= $FRAMEWORK_VERSION."
else
  echo "    1. Evaluate ALL entries (no framework-version baseline)."
fi
echo "    2. Run the entry's detect predicate against this project."
echo "    3. Skip entries where detect => adopted."
echo "    4. For remaining entries (introduced-in ascending order):"
echo "       - Noir: auto-run adopt; then offer migrate (confirmed, one prompt)."
echo "       - Supervised/Weiss: prompt '[Yes / No / Details]' per feature."
echo "    5. After all adoptions succeed (or are consciously skipped), write"
echo "       framework-version to .claude/grimoire-config.json (adoption phase"
echo "       is the sole writer — never the file-merge walk)."
echo "    6. If any paradigms/* file was UPDATEd this run, remind the user to"
echo "       re-run work-paradigm-switch to re-install the active paradigm."
echo
echo "  ADOPTION vs MIGRATION rule (§6): adopt writes config only."
echo "  Migration moves existing user data and ALWAYS requires explicit"
echo "  confirmation and a backup — never auto-run, even under Noir."
echo
echo "  See SKILL.md Step 4.5 for the full adoption procedure."
