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
# updater concern — see docs/design/release-distribution-design.md §meta-updater.)
#
# Usage:
#   ./sync-from-upstream.sh [--apply] [--diff] [--adopt-base] [--force]
#
#   (no flag)      dry-run: report what each file would do.
#   --diff         also print per-file diffs for would-be changes.
#   --apply        write changes (merges, new files), with backups.
#   --adopt-base   record the current upstream as the base for every managed
#                  file WITHOUT touching local files. Use once on an existing
#                  customized project to establish provenance for future syncs.
#   --force        allow --apply on a dirty git tree.
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
# behaviour never regresses. Design: docs/design/release-distribution-design.md
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

APPLY=0; SHOW_DIFF=0; ADOPT_BASE=0; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)      APPLY=1 ;;
    --diff)       SHOW_DIFF=1 ;;
    --adopt-base) ADOPT_BASE=1 ;;
    --force)      FORCE=1 ;;
    --self-test)  : ;;  # handled in the transport block above (pre-re-exec)
    -h|--help)    sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^#\{0,1\} \{0,1\}//'; exit 0 ;;
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
  if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ] && [ "$FORCE" -eq 0 ]; then
    echo "ERROR: project has uncommitted changes — refusing to --apply." >&2
    echo "       Commit/stash first, or pass --force." >&2
    exit 3
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

# --------------------------------------------------------------------------
# Files that map to local but must NOT be auto-synced (project-owned/templates)
# Paths are relative to the flavor dir (== relative to the project root).
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
  esac
  return 1
}

backup() {  # back up an existing local file before rewriting it
  local l="$1" rel="$2"
  mkdir -p "$BACKUP_DIR/$(dirname "$rel")"; cp "$l" "$BACKUP_DIR/$rel"
}
set_base() {  # record upstream as the new base for this file
  local u="$1" b="$2"
  [ "$APPLY" -eq 1 ] || [ "$ADOPT_BASE" -eq 1 ] || return 0
  mkdir -p "$(dirname "$b")"; cp "$u" "$b"
}

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
    if [ "$APPLY" -eq 1 ]; then backup "$L" "$rel"; cp "$merged" "$L"; set_base "$U" "$B"; fi
  else
    printf "  %-10s %s  (conflict markers; resolve, do NOT auto-advance base)\n" "CONFLICT" "$rel"
    conflicts="${conflicts}\n    $rel"; n_conflict=$((n_conflict+1))
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
if [ "$APPLY" -eq 1 ]; then
  echo
  echo "Applied. Backups of rewritten files: ${BACKUP_DIR#"$PROJECT_ROOT"/}"
  echo "Review the diff and commit. Resolve any CONFLICT files, then re-run to advance their base."
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

MANIFEST="$PROJECT_ROOT/.claude/skills/sync-from-upstream/feature-manifest.md"
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
