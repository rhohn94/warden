#!/usr/bin/env bash
#
# sync-from-source.sh — pull workflow skills, hooks, and structural docs from a
# source project into THIS Grimoire repo.
#
# Safe by default:
#   * dry-run unless --apply (writes nothing, just reports + diffs)
#   * never overwrites a destination file that is NEWER than the source
#     (protects in-progress scaffolding work) unless --overwrite-newer
#   * refuses to write if the destination repo has a dirty git tree
#     (unless --force)
#   * backs up every file it overwrites to .sync-backup/<timestamp>/
#   * additive only — never deletes scaffolding-only files (workflow-bootstrap,
#     workflow-snapshot, sync-from-source, manifest.md, README, templates)
#   * copies VERBATIM — it does NOT genericize. Files containing source-specific
#     tokens are flagged "needs-genericize"; the sync-from-source SKILL handles
#     re-inserting placeholders and updating golden/ afterward.
#
# Usage:
#   ./sync-from-source.sh [SOURCE_DIR] [--apply] [--diff]
#                         [--overwrite-newer] [--force]
#
#   SOURCE_DIR   path to the source project root (or set $SCAFFOLD_SOURCE).
#
# Examples:
#   SCAFFOLD_SOURCE=~/Projects/forge-engine ./sync-from-source.sh         # dry-run
#   ./sync-from-source.sh ~/Projects/forge-engine --diff                  # dry-run + diffs
#   ./sync-from-source.sh ~/Projects/forge-engine --apply                 # write changes
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# .claude/skills/sync-from-source -> repo root is three levels up
DEST_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MAP_FILE="$SCRIPT_DIR/name-map.conf"

APPLY=0
OVERWRITE_NEWER=0
SHOW_DIFF=0
FORCE=0
SOURCE_ROOT="${SCAFFOLD_SOURCE:-}"

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^#\{0,1\} \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --apply)           APPLY=1 ;;
    --overwrite-newer) OVERWRITE_NEWER=1 ;;
    --diff)            SHOW_DIFF=1 ;;
    --force)           FORCE=1 ;;
    -h|--help)         usage; exit 0 ;;
    --*)               echo "unknown flag: $1" >&2; usage; exit 2 ;;
    *)                 SOURCE_ROOT="$1" ;;
  esac
  shift
done

if [ -z "$SOURCE_ROOT" ]; then
  echo "ERROR: no source project given. Pass a path or set \$SCAFFOLD_SOURCE." >&2
  exit 2
fi
SOURCE_ROOT="$(cd "$SOURCE_ROOT" 2>/dev/null && pwd || true)"
if [ -z "$SOURCE_ROOT" ] || [ ! -d "$SOURCE_ROOT/.claude/skills" ]; then
  echo "ERROR: source has no .claude/skills (got '${SOURCE_ROOT:-?}')." >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Guard: refuse to apply onto a dirty destination git tree
# ---------------------------------------------------------------------------
if [ "$APPLY" -eq 1 ] && git -C "$DEST_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [ -n "$(git -C "$DEST_ROOT" status --porcelain)" ] && [ "$FORCE" -eq 0 ]; then
    echo "ERROR: destination repo has uncommitted changes — refusing to --apply." >&2
    echo "       Commit/stash your in-progress work, or pass --force to override." >&2
    exit 3
  fi
fi

# ---------------------------------------------------------------------------
# Portable mtime (epoch seconds): macOS 'stat -f %m', GNU 'stat -c %Y'
# ---------------------------------------------------------------------------
mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1"; }

# ---------------------------------------------------------------------------
# Name map: destination skill name -> source skill name.
# Defaults to identical; override via name-map.conf ("dest-name source-name").
# ---------------------------------------------------------------------------
src_skill_name() {
  local dest="$1" line
  if [ -f "$MAP_FILE" ]; then
    line="$(grep -E "^[[:space:]]*${dest}[[:space:]]+" "$MAP_FILE" 2>/dev/null | grep -vE '^[[:space:]]*#' | head -1 || true)"
    if [ -n "$line" ]; then echo "$line" | awk '{print $2}'; return; fi
  fi
  echo "$dest"
}

# ---------------------------------------------------------------------------
# What to sync.  Keep the skill list aligned with workflow-bootstrap/manifest.md.
# Scaffolding-only skills (workflow-bootstrap, workflow-snapshot,
# sync-from-source) are intentionally absent — they have no source equivalent.
# ---------------------------------------------------------------------------
SKILLS="design-doc-scaffold worktree-preflight release-planning release-agreement release-phase release-agent-tracker release-phase-merge ledger-tick project-release repo-reference source-to-design-docs"
HOOKS="protected-branch-guard.sh release-plan-guard.sh worktree-guard.sh"
# Structural workflow docs only. roadmap.md and docs/design/README.md are
# scaffolding TEMPLATES and are deliberately NOT synced from a source's real
# content. CLAUDE.md needs heavy genericization — reported, never auto-copied.
DOCS="docs/integration-workflow.md docs/version-design.md"

# Heuristic markers that a copied file still carries source-specific content.
MARKER_RE='cargo|Cargo\.toml|crate|[^[:alnum:]]just[[:space:]]|forge[_-]|version\.rs|FORGE_'

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
BACKUP_DIR="$DEST_ROOT/.sync-backup/$(date +%Y%m%d-%H%M%S)"
CHANGED=0
SKIPPED_NEWER=0
NEEDS_GENERICIZE=""
SKIP_NEWER_LIST=""

backup() {
  local f="$1" rel
  rel="${f#"$DEST_ROOT"/}"
  mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
  cp "$f" "$BACKUP_DIR/$rel"
}

# process SRC DST LABEL GENERICIZE(0|1)
process() {
  local src="$1" dst="$2" label="$3" gz="$4" action flag=""
  if [ ! -f "$src" ]; then
    printf "  %-12s %s\n" "SRC-MISSING" "$label"
    return
  fi
  if [ ! -f "$dst" ]; then
    action="NEW"
  elif diff -q "$src" "$dst" >/dev/null 2>&1; then
    printf "  %-12s %s\n" "identical" "$label"
    return
  else
    if [ "$(mtime "$dst")" -gt "$(mtime "$src")" ] && [ "$OVERWRITE_NEWER" -eq 0 ]; then
      printf "  %-12s %s  (dest newer — protected)\n" "SKIP-NEWER" "$label"
      SKIPPED_NEWER=$((SKIPPED_NEWER + 1))
      SKIP_NEWER_LIST="${SKIP_NEWER_LIST}\n    ${label}"
      return
    fi
    action="UPDATE"
  fi

  if [ "$gz" -eq 1 ] && grep -nEq "$MARKER_RE" "$src" 2>/dev/null; then
    flag="  ⚠ needs-genericize"
  fi
  printf "  %-12s %s%s\n" "$action" "$label" "$flag"

  if [ "$SHOW_DIFF" -eq 1 ] && [ "$action" = "UPDATE" ]; then
    diff -u "$dst" "$src" 2>/dev/null | sed 's/^/      /' || true
  fi

  if [ "$APPLY" -eq 1 ]; then
    mkdir -p "$(dirname "$dst")"
    [ "$action" = "UPDATE" ] && backup "$dst"
    cp "$src" "$dst"
    [ -n "$flag" ] && NEEDS_GENERICIZE="${NEEDS_GENERICIZE}\n    ${dst#"$DEST_ROOT"/}"
  fi
  CHANGED=$((CHANGED + 1))
}

# ---------------------------------------------------------------------------
# Report header
# ---------------------------------------------------------------------------
echo "sync-from-source"
echo "  source: $SOURCE_ROOT"
echo "  dest:   $DEST_ROOT"
if [ "$APPLY" -eq 1 ]; then echo "  mode:   APPLY (writing changes)"; else echo "  mode:   dry-run (no changes; use --apply to write)"; fi
echo

echo "Skills (.claude/skills/):"
for s in $SKILLS; do
  process "$SOURCE_ROOT/.claude/skills/$(src_skill_name "$s")/SKILL.md" \
          "$DEST_ROOT/.claude/skills/$s/SKILL.md" \
          "$s$([ "$(src_skill_name "$s")" != "$s" ] && echo "  (<- $(src_skill_name "$s"))")" 1
done

echo
echo "Hooks (.claude/hooks/):"
for h in $HOOKS; do
  process "$SOURCE_ROOT/.claude/hooks/$h" "$DEST_ROOT/.claude/hooks/$h" "$h" 1
done

echo
echo "Settings:"
process "$SOURCE_ROOT/.claude/settings.json" "$DEST_ROOT/.claude/settings.json" "settings.json" 1

echo
echo "Structural docs:"
for d in $DOCS; do
  process "$SOURCE_ROOT/$d" "$DEST_ROOT/$d" "$d" 1
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "----------------------------------------------------------------"
echo "Summary: $CHANGED change(s) pending, $SKIPPED_NEWER protected (dest newer)."
if [ -n "$SKIP_NEWER_LIST" ]; then
  echo "Protected (dest newer than source — review manually):"
  printf "%b\n" "$SKIP_NEWER_LIST"
fi
if [ "$APPLY" -eq 1 ]; then
  [ "$CHANGED" -gt 0 ] && echo "Backups of overwritten files: ${BACKUP_DIR#"$DEST_ROOT"/}"
  if [ -n "$NEEDS_GENERICIZE" ]; then
    echo
    echo "⚠ These copied files carry source-specific tokens — genericize them"
    echo "  (re-insert placeholders per workflow-bootstrap/manifest.md):"
    printf "%b\n" "$NEEDS_GENERICIZE"
    echo
    echo "  Then run the workflow-snapshot skill to refresh golden/ copies."
  fi
else
  echo "Re-run with --apply to write, or --diff to see full diffs."
fi
