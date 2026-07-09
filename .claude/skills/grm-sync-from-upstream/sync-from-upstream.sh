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
# RECOGNIZED ARTIFACT — .claude/component-registry.json (Pillar 4 distribution):
#   The versioned component registry is carried by the normal file-merge walk
#   below (it is NOT in is_excluded). It therefore rides the existing 3-way
#   merge: local components are preserved and upstream ones added/updated by
#   version; genuine same-entry collisions surface as CONFLICT. No special
#   casing is needed and none is added here — this note documents the behaviour.
#   The derived matrix (.claude/cache/component-compatibility.json) is gitignored
#   and regenerable, so it is intentionally NOT distributed.
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
#                           [--allow-ahead] [--mark-resolved <file>]
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
#
# WARNINGS the merge walk can emit (#180/#181):
#   * MISSING-SYMBOL (#180): after a 3-way merge, the result references a symbol
#     UPSTREAM defines but that is NOT defined anywhere in the merged output —
#     a "call-site without definition" the merge produced with no conflict marker
#     (typically LOCAL deleted a helper UPSTREAM still calls, in a non-overlapping
#     region). Best-effort, language-agnostic-ish. Never blocks; warns loudly.
#   * MANUALLY-RESOLVED-BUT-BASE-NOT-ADVANCED (#181): --apply is about to
#     overwrite a CONFLICT file whose LOCAL copy has no conflict markers (it looks
#     already hand-resolved) — it points you at --mark-resolved instead.
#   --allow-ahead  BMI-3 Rule 3b consumer-sync escape hatch (#144/#146/#162/#173):
#                  permit --apply when the integration line is merely AHEAD of
#                  main (e.g. a prior sync's framework-version bump, or committed
#                  conflict resolutions) instead of demanding tree-identical
#                  lines. It does NOT disable the divergence guard — a genuine
#                  fork (main carrying work the integration line lacks) is still
#                  refused. Use for back-to-back syncs or where dev->main merges
#                  are restricted.
#
set -euo pipefail

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
    # ── v3.41 "Clean-Room" CR-2 (clean-room-design.md §4): relocated operational
    #    docs + carve-outs. Mirror of build_distributables.py EXCLUDED_PATH_PREFIXES.
    docs/grimoire/integration-workflow.md) return 0 ;;        # framework-process doc
    docs/grimoire/version-design.md) return 0 ;;              # framework versioning scheme
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
  assert_eq "cr2 version-design excl"       "$(excl_rc docs/grimoire/version-design.md)"          0
  assert_eq "cr2 qa-ledger excl"            "$(excl_rc docs/grimoire/qa-ledger.md)"               0
  assert_eq "cr2 token-efficiency excl"     "$(excl_rc docs/grimoire/token-efficiency-audit.md)"  0
  assert_eq "cr2 relocated rel-plan excl"   "$(excl_rc docs/grimoire/release-planning-v1.0.md)"   0
  assert_eq "cr2 top-level rel-plan excl"   "$(excl_rc docs/release-planning-v3.41.md)"           0
  assert_eq "cr2 version-history excl"      "$(excl_rc docs/version-history.md)"                  0
  assert_eq "project-own design NOT excl" "$(excl_rc docs/design/bar-design.md)"          1
  assert_eq "existing roadmap rule kept"  "$(excl_rc docs/roadmap.md)"                    0

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

APPLY=0; SHOW_DIFF=0; ADOPT_BASE=0; FORCE=0; ALLOW_AHEAD=0; MARK_RESOLVED=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)       APPLY=1 ;;
    --diff)        SHOW_DIFF=1 ;;
    --adopt-base)  ADOPT_BASE=1 ;;
    --force)       FORCE=1 ;;
    --allow-ahead) ALLOW_AHEAD=1 ;;   # BMI-3 Rule 3b escape hatch (#144/#146/#162/#173)
    --mark-resolved)                  # per-file base advance (#181)
      shift; [ $# -gt 0 ] || { echo "ERROR: --mark-resolved needs a <file> argument." >&2; exit 2; }
      MARK_RESOLVED="$1" ;;
    --mark-resolved=*) MARK_RESOLVED="${1#--mark-resolved=}" ;;
    --self-test)   : ;;  # handled in the transport block above (pre-re-exec)
    -h|--help)    sed -n '2,104p' "${BASH_SOURCE[0]}" | sed 's/^#\{0,1\} \{0,1\}//'; exit 0 ;;
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
#   By default the line is also required to be at a clean release boundary
#   (integration line and main tree-identical). --allow-ahead relaxes that to the
#   model-aware divergence predicate: the integration line being merely AHEAD of
#   main (the consumer-sync catch-22 — a prior sync's framework-version bump or a
#   committed conflict resolution) is SAFE and permitted, while a genuine fork
#   (main carrying tree content unreachable from the integration line) is still
#   refused (#144/#146/#162/#173). The escape hatch never disables the fork guard.
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
  # Rule 3b: refuse when the integration line and main differ — unless the only
  # difference is the integration line being AHEAD and --allow-ahead is set.
  if ! git -C "$PROJECT_ROOT" diff --quiet "$_INT" main 2>/dev/null; then
    if [ "$ALLOW_AHEAD" -eq 1 ]; then
      # Escape hatch: permit only when main carries NO unreachable work (the
      # same divergence predicate the BMI-2 promotion guard uses). A real fork
      # still HALTs — the guard is relaxed for "ahead", never for "diverged".
      _CHERRY="$(main_only_cherry_lines "$PROJECT_ROOT" "$_INT" main)"
      if cherry_lines_show_unreachable_work "$_CHERRY"; then
        echo "ERROR (BMI-3): sync-from-upstream --apply refused — main has DIVERGED." >&2
        echo "  main carries commit(s) of work not on the integration line ('$_INT'):" >&2
        git -C "$PROJECT_ROOT" log --oneline --no-decorate "$_INT"..main 2>/dev/null | sed 's/^/    /' >&2
        echo "  This is a real fork, not the integration line merely being ahead." >&2
        echo "  --allow-ahead does NOT bypass this. Reconcile by merging main INTO" >&2
        echo "  '$_INT' (merge-forward); never reset across the fork (data loss)." >&2
        exit 3
      fi
      echo "NOTICE (BMI-3): integration line ('$_INT') is ahead of main; main carries" >&2
      echo "  no unreachable work, so --allow-ahead permits this sync. Promote the" >&2
      echo "  accumulated integration-line commits to main when convenient." >&2
    else
      echo "ERROR (BMI-3): sync-from-upstream --apply refused — not at a clean release boundary." >&2
      echo "  The integration line ('$_INT') and main differ (e.g. mid-release work, or" >&2
      echo "  a prior sync's framework-version bump not yet promoted to main)." >&2
      echo "  By default a sync runs only when the two lines are tree-identical." >&2
      echo "  If the integration line is simply AHEAD of main (no real fork), re-run with" >&2
      echo "  --allow-ahead to sync without promoting first (the consumer-sync escape hatch)." >&2
      echo "  Otherwise promote the current release, then re-run the sync." >&2
      exit 3
    fi
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
  echo "Marked resolved: advanced base for '$rel_mr' to current upstream content."
  echo "  Future syncs will no longer re-merge it (other files' bases untouched)."
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

n_new=0; n_update=0; n_merged=0; n_conflict=0; n_review=0; n_local=0; n_insync=0
conflicts=""; reviews=""; news=""

while IFS= read -r rel; do
  rel="${rel#./}"
  is_excluded "$rel" && continue
  U="$FLAVOR_DIR/$rel"; L="$PROJECT_ROOT/$rel"; B="$BASE_ROOT/$rel"

  if [ "$ADOPT_BASE" -eq 1 ]; then
    set_base "$U" "$B"; printf "  %-10s %s\n" "base-set" "$rel"; continue
  fi

  if [ ! -f "$L" ]; then
    printf "  %-10s %s\n" "NEW" "$rel"; news="${news}\n    $rel"; n_new=$((n_new+1))
    if [ "$APPLY" -eq 1 ]; then mkdir -p "$(dirname "$L")"; cp "$U" "$L"; set_base "$U" "$B"; fi
    continue
  fi

  if diff -q "$U" "$L" >/dev/null 2>&1; then
    printf "  %-10s %s\n" "in-sync" "$rel"; n_insync=$((n_insync+1)); set_base "$U" "$B"; continue
  fi

  # L and U differ
  if [ ! -f "$B" ]; then
    printf "  %-10s %s  (no base — keeping local; --adopt-base to set provenance)\n" "REVIEW" "$rel"
    reviews="${reviews}\n    $rel"; n_review=$((n_review+1))
    [ "$SHOW_DIFF" -eq 1 ] && diff -u "$L" "$U" 2>/dev/null | sed 's/^/      /' || true
    continue
  fi
  if diff -q "$L" "$B" >/dev/null 2>&1; then
    printf "  %-10s %s\n" "UPDATE" "$rel"; n_update=$((n_update+1))     # local unchanged since base
    [ "$SHOW_DIFF" -eq 1 ] && diff -u "$L" "$U" 2>/dev/null | sed 's/^/      /' || true
    if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$U" "$L"; set_base "$U" "$B"; fi
    continue
  fi
  if diff -q "$U" "$B" >/dev/null 2>&1; then
    printf "  %-10s %s\n" "local" "$rel"; n_local=$((n_local+1)); continue   # upstream unchanged; keep local edits
  fi

  # both sides changed since base -> 3-way merge
  merged="$(mktemp)"
  if git merge-file -p -L local -L base -L upstream "$L" "$B" "$U" > "$merged" 2>/dev/null; then
    printf "  %-10s %s\n" "MERGED" "$rel"; n_merged=$((n_merged+1))
    # #180: a clean (markerless) merge can still be broken — LOCAL may have
    # deleted a definition UPSTREAM still calls, in a non-overlapping region.
    warn_dropped_definitions "$U" "$merged" "$rel"
    if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$merged" "$L"; set_base "$U" "$B"; fi
  else
    # #181: if LOCAL has no conflict markers, the prior round's conflict was
    # already resolved by hand but its base was never advanced — so we are about
    # to overwrite that manual resolution with fresh markers. Warn loudly and
    # point at --mark-resolved (the surgical base advance) instead of silently
    # clobbering. We still proceed (non-destructive: LOCAL is backed up).
    if [ -f "$L" ] && ! content_has_conflict_markers < "$L"; then
      echo "WARNING (#181): '$rel' re-conflicts, but your LOCAL copy has NO conflict" >&2
      echo "  markers — it looks like a prior round you already resolved by hand whose" >&2
      echo "  base was never advanced. --apply will OVERWRITE that resolution with fresh" >&2
      echo "  markers (a backup is kept). To keep your resolution, run instead:" >&2
      echo "    sync-from-upstream.sh --mark-resolved $rel" >&2
    fi
    printf "  %-10s %s  (conflict markers; resolve, do NOT auto-advance base)\n" "CONFLICT" "$rel"
    conflicts="${conflicts}\n    $rel"; n_conflict=$((n_conflict+1))
    # #180: the UPSTREAM side of a conflict may reference a definition LOCAL
    # dropped that ends up outside the marked regions — surface it too.
    warn_dropped_definitions "$U" "$merged" "$rel"
    if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$merged" "$L"; fi   # base NOT advanced
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
  exit 0
fi
echo "Summary: NEW=$n_new UPDATE=$n_update MERGED=$n_merged CONFLICT=$n_conflict REVIEW=$n_review local-only-edits=$n_local in-sync=$n_insync"
[ -n "$news" ]      && { echo "New files (generic — re-specialize placeholders after apply):"; printf "%b\n" "$news"; }
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
