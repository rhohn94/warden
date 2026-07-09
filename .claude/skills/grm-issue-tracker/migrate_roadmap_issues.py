#!/usr/bin/env python3
"""Migrate docs/roadmap.md ## Backlog bullets to the configured issue tracker.

Safety contract:
  - Timestamped backup of roadmap.md is written FIRST, before any mutation.
  - --dry-run (default) previews all actions without writing anything.
  - --apply prompts for confirmation before acting; aborts on decline.
  - Idempotent: bullets already recorded in the migration-state file are skipped.
  - Reversible: run --restore to copy the latest backup back over roadmap.md.

The migration state is stored in .claude/cache/roadmap-migration-state.json
(gitignored). Each successfully filed bullet is recorded there so re-runs skip it.

Usage:
  # Preview (safe — no writes):
  python3 migrate_roadmap_issues.py [--config PATH]

  # Apply (backs up, confirms, then migrates):
  python3 migrate_roadmap_issues.py --apply [--config PATH]

  # Restore from latest backup:
  python3 migrate_roadmap_issues.py --restore [--config PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import shutil
import sys
import textwrap
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROADMAP_FILE = "docs/roadmap.md"
BACKLOG_HEADER = "## Backlog"
CONFIG_FILE = ".claude/grimoire-config.json"
CACHE_DIR = ".claude/cache"
STATE_FILE = ".claude/cache/roadmap-migration-state.json"

# ---------------------------------------------------------------------------
# Repo-root detection (mirrors issue_tracker.py pattern)
# ---------------------------------------------------------------------------


def find_repo_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """Walk up from start (or cwd) to find the repo root containing CONFIG_FILE."""
    current = (start or pathlib.Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILE).exists():
            return candidate
    return pathlib.Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Roadmap parser
# ---------------------------------------------------------------------------

BULLET_RE = re.compile(r"^- (.+)", re.MULTILINE)


def parse_backlog_bullets(roadmap_text: str) -> list[str]:
    """Extract top-level bullet titles from the ## Backlog section.

    Returns one string per top-level '- ...' bullet: the bullet's full raw
    text with the leading '- ' stripped from the first line and continuation
    lines dedented by one level. Blank lines, sub-bullets, and multi-paragraph
    bodies are preserved verbatim so the migrated issue body is faithful.
    """
    # Locate the ## Backlog section
    backlog_start = roadmap_text.find(f"\n{BACKLOG_HEADER}")
    if backlog_start == -1:
        backlog_start = roadmap_text.find(f"{BACKLOG_HEADER}\n")
        if backlog_start == -1:
            return []
    # Find end of section (next ## header or EOF)
    section_start = backlog_start + len(BACKLOG_HEADER) + 1
    next_section = re.search(r"^\## ", roadmap_text[section_start:], re.MULTILINE)
    if next_section:
        section_text = roadmap_text[section_start: section_start + next_section.start()]
    else:
        section_text = roadmap_text[section_start:]

    # Parse bullets — a top-level bullet starts with '- ' at column 0.
    # Continuation lines (indented, sub-bullets, or blank) belong to the
    # current bullet and are kept verbatim (dedented one level) so multi-
    # paragraph bullets are captured in full, not truncated at the first
    # blank line. A non-indented, non-blank, non-bullet line ends the run.
    bullets: list[str] = []
    current: list[str] | None = None

    def _flush() -> None:
        if current is not None:
            bullets.append("\n".join(current).rstrip())

    for line in section_text.splitlines():
        if line.startswith("- "):
            _flush()
            current = [line[2:].rstrip()]
        elif current is not None and line.strip() == "":
            current.append("")                       # preserve paragraph breaks
        elif current is not None and line.startswith("  "):
            current.append(line[2:].rstrip())        # dedent one level
        elif current is not None and line.startswith("\t"):
            current.append(line[1:].rstrip())
        else:
            _flush()
            current = None
    _flush()
    return bullets


# ---------------------------------------------------------------------------
# Migration state (idempotency)
# ---------------------------------------------------------------------------


def load_state(state_path: pathlib.Path) -> dict[str, Any]:
    """Load migration state from JSON; return empty dict if absent or corrupt."""
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_path: pathlib.Path, state: dict[str, Any]) -> None:
    """Persist migration state to JSON."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def bullet_key(text: str) -> str:
    """Stable key for a bullet (first 120 chars lowercased, whitespace-normalised)."""
    return re.sub(r"\s+", " ", text.strip().lower())[:120]


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def backup_roadmap(repo_root: pathlib.Path) -> pathlib.Path:
    """Copy roadmap.md to .claude/cache/roadmap-backup-<iso-ts>.md and return path.

    The cache directory is gitignored (.claude/cache/).
    """
    src = repo_root / ROADMAP_FILE
    if not src.exists():
        raise FileNotFoundError(f"Roadmap file not found: {src}")
    cache = repo_root / CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = cache / f"roadmap-backup-{ts}.md"
    shutil.copy2(src, dest)
    return dest


def latest_backup(repo_root: pathlib.Path) -> pathlib.Path | None:
    """Return the most recent roadmap backup, or None if none exist."""
    cache = repo_root / CACHE_DIR
    if not cache.exists():
        return None
    backups = sorted(cache.glob("roadmap-backup-*.md"))
    return backups[-1] if backups else None


# ---------------------------------------------------------------------------
# Issue filing (reuses issue_tracker.py create path)
# ---------------------------------------------------------------------------


def file_issue(repo_root: pathlib.Path, title: str, body: str,
               config_path: pathlib.Path | None) -> dict[str, Any]:
    """Call issue_tracker.py create for one bullet; return the parsed Issue dict.

    Raises RuntimeError on non-zero exit.
    """
    import subprocess

    script = repo_root / ".claude/skills/grm-issue-tracker/issue_tracker.py"
    cmd = [
        sys.executable, str(script),
        "--json",
        *(["--config", str(config_path)] if config_path else []),
        "create",
        "--title", title[:200],   # guard against very long first lines
        "--body", body,
        "--audience", "internal",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"issue_tracker.py exited {result.returncode}: {result.stderr.strip()}"
        )
    # issue_tracker.py emits the Issue as JSON on --json. It may be
    # pretty-printed (indent=2), so it spans multiple lines and an optional
    # human line may precede it. Parse the object spanning the first '{' to
    # the last '}' rather than assuming a single-line object.
    out = result.stdout
    start = out.find("{")
    end = out.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RuntimeError(
            f"No JSON object in issue_tracker.py output:\n{out[:500]}"
        )
    return json.loads(out[start:end + 1])


# ---------------------------------------------------------------------------
# Remove a bullet from the roadmap (post-migration cleanup)
# ---------------------------------------------------------------------------


def remove_bullet_from_roadmap(roadmap_path: pathlib.Path, title_prefix: str) -> bool:
    """Remove the backlog bullet whose first-line text starts with title_prefix.

    Removes the bullet and its indented continuation lines.  Returns True if
    the bullet was found and removed, False otherwise.
    """
    text = roadmap_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    new_lines: list[str] = []
    removing = False
    removed = False

    # Match on the FIRST line of the prefix only — the caller may pass a key
    # that spans the bullet's wrapped continuation (which contains newlines).
    key = title_prefix.splitlines()[0].strip()[:60] if title_prefix.strip() else ""

    for line in lines:
        # A section heading always ends an in-progress removal and is kept.
        if line.startswith("## "):
            removing = False
            new_lines.append(line)
            continue

        # A new top-level bullet ("- " at column 0) is a boundary: it ends any
        # in-progress removal, and may itself be the bullet to remove.
        if line.startswith("- "):
            bullet_text = line[2:].strip()
            if not removed and key and bullet_text.startswith(key):
                removing = True   # drop this bullet + all its continuations
                removed = True
                continue
            removing = False
            new_lines.append(line)
            continue

        # Any other line (indented continuation OR blank line) belongs to the
        # current bullet; drop it while removing, keep it otherwise.
        if removing:
            continue
        new_lines.append(line)

    if removed:
        roadmap_path.write_text("".join(new_lines), encoding="utf-8")
    return removed


# ---------------------------------------------------------------------------
# Core migrate logic
# ---------------------------------------------------------------------------


def run_migrate(
    repo_root: pathlib.Path,
    config_path: pathlib.Path | None,
    dry_run: bool,
) -> int:
    """Run the migration.  Returns 0 on success, 1 on error/abort."""
    roadmap_path = repo_root / ROADMAP_FILE
    state_path = repo_root / STATE_FILE

    if not roadmap_path.exists():
        print(f"ERROR: roadmap not found at {roadmap_path}", file=sys.stderr)
        return 1

    roadmap_text = roadmap_path.read_text(encoding="utf-8")
    bullets = parse_backlog_bullets(roadmap_text)

    if not bullets:
        print("No ## Backlog bullets found in docs/roadmap.md — nothing to migrate.")
        return 0

    state = load_state(state_path)
    migrated_keys: set[str] = set(state.get("migrated", {}).keys())

    pending = [(b, bullet_key(b)) for b in bullets if bullet_key(b) not in migrated_keys]
    already_done = len(bullets) - len(pending)

    # --- DRY-RUN ---
    if dry_run:
        print("=== Roadmap migration DRY-RUN (no writes) ===\n")
        print(f"Roadmap:   {roadmap_path}")
        print(f"Bullets:   {len(bullets)} total, {already_done} already migrated, "
              f"{len(pending)} pending\n")
        if not pending:
            print("All bullets already recorded in migration state. Nothing to do.")
            return 0
        print("Pending bullets (would be filed as internal issues):")
        for i, (text, key) in enumerate(pending, 1):
            preview = textwrap.shorten(text, width=100, placeholder="…")
            print(f"  [{i}] {preview}")
        print(
            f"\nRun with --apply to perform the migration. "
            f"Roadmap will be backed up to {repo_root / CACHE_DIR}/ first."
        )
        return 0

    # --- APPLY ---
    if not pending:
        print(f"Migration already complete: all {len(bullets)} bullets are recorded "
              "in the migration state. Nothing to do.")
        return 0

    print("=== Roadmap migration ===\n")
    print(f"Roadmap:   {roadmap_path}")
    print(f"Bullets:   {len(bullets)} total, {already_done} already migrated, "
          f"{len(pending)} pending\n")
    print("Bullets to migrate:")
    for i, (text, key) in enumerate(pending, 1):
        preview = textwrap.shorten(text, width=100, placeholder="…")
        print(f"  [{i}] {preview}")
    print()

    answer = input(
        f"Migrate {len(pending)} bullet(s) to the issue tracker and remove them "
        f"from roadmap.md?\n"
        f"A timestamped backup will be saved to {repo_root / CACHE_DIR}/ first.\n"
        f"[yes/no]: "
    ).strip().lower()
    if answer not in ("yes", "y"):
        print("Aborted. No changes made.")
        return 0

    # Step 1: Backup FIRST
    backup_path = backup_roadmap(repo_root)
    print(f"\nBackup written: {backup_path}")

    # Ensure state is initialised
    if "migrated" not in state:
        state["migrated"] = {}
    if "backup" not in state:
        state["backup"] = []

    state["backup"].append(str(backup_path))

    # Step 2: File each bullet, record state, remove from roadmap
    errors: list[str] = []
    filed_count = 0

    for text, key in pending:
        first_line = text.splitlines()[0] if text else ""
        title = textwrap.shorten(
            first_line.split("(")[0].strip(), width=200, placeholder="…")
        body = (
            f"**Migrated from:** `docs/roadmap.md ## Backlog`\n\n"
            f"**Original text:**\n\n{text}\n\n"
            f"**Context / source:** automated roadmap migration via "
            f"`migrate_roadmap_issues.py`"
        )
        try:
            issue = file_issue(repo_root, title, body, config_path)
            issue_id = issue.get("id", "?")
            issue_url = issue.get("url") or "(roadmap — no URL)"
            tracker_name = issue.get("tracker", "?")
            print(f"  Filed #{issue_id} in '{tracker_name}': {title[:70]}")
            if issue_url != "(roadmap — no URL)":
                print(f"    URL: {issue_url}")
            state["migrated"][key] = {
                "title": title,
                "issue_id": issue_id,
                "url": issue_url,
                "tracker": tracker_name,
            }
            save_state(state_path, state)
            filed_count += 1
            # Remove the bullet from roadmap
            remove_bullet_from_roadmap(roadmap_path, title[:60])
        except RuntimeError as exc:
            print(f"  ERROR filing '{title[:60]}': {exc}", file=sys.stderr)
            errors.append(f"{title[:60]}: {exc}")

    print(f"\nMigration complete: {filed_count} filed, {len(errors)} error(s).")
    if errors:
        print("Errors (bullets NOT removed from roadmap):")
        for e in errors:
            print(f"  - {e}")
        print(f"\nReversible: restore roadmap from backup at {backup_path}")
        return 1

    print(f"\nRoadmap updated. Migration state: {state_path}")
    print(f"Reversible: python3 migrate_roadmap_issues.py --restore")
    return 0


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def run_restore(repo_root: pathlib.Path) -> int:
    """Restore roadmap.md from the most recent backup."""
    backup = latest_backup(repo_root)
    if backup is None:
        print("No roadmap backup found in .claude/cache/. Nothing to restore.",
              file=sys.stderr)
        return 1

    roadmap_path = repo_root / ROADMAP_FILE
    print(f"Restore roadmap from: {backup}")
    answer = input("Proceed? [yes/no]: ").strip().lower()
    if answer not in ("yes", "y"):
        print("Aborted.")
        return 0

    shutil.copy2(backup, roadmap_path)
    print(f"Restored: {roadmap_path}")
    print(
        "Note: migration state (.claude/cache/roadmap-migration-state.json) is "
        "NOT reset — re-run --apply to re-migrate items if needed."
    )
    return 0


# ---------------------------------------------------------------------------
# Self-validation (ast.parse check)
# ---------------------------------------------------------------------------


def self_check() -> None:
    """Verify this file parses as valid Python (used in CI / --check mode)."""
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    ast.parse(src)   # raises SyntaxError if the file is broken


# ---------------------------------------------------------------------------
# Scratch dry-run validation helper (never touches real roadmap)
# ---------------------------------------------------------------------------


def validate_against_scratch(scratch_text: str, repo_root: pathlib.Path) -> None:
    """Parse bullets from scratch_text and print what would be filed — no I/O."""
    bullets = parse_backlog_bullets(scratch_text)
    print(f"Scratch validation: found {len(bullets)} bullet(s)")
    for i, b in enumerate(bullets, 1):
        preview = textwrap.shorten(b, width=80, placeholder="…")
        print(f"  [{i}] {preview}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_roadmap_issues.py",
        description=(
            "Migrate docs/roadmap.md ## Backlog bullets to the configured issue "
            "tracker. Default mode is --dry-run (preview only). Pass --apply to "
            "act. Always backs up roadmap.md before writing. Idempotent: already-"
            "migrated bullets are skipped. Reversible via --restore."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to grimoire-config.json (default: auto-detect from cwd).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform the migration (backup + confirm + file + remove from roadmap).",
    )
    mode.add_argument(
        "--restore",
        action="store_true",
        help="Restore roadmap.md from the most recent backup.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Parse this script with ast.parse and exit 0 if valid (syntax check).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check:
        try:
            self_check()
            print("Syntax OK")
            return 0
        except SyntaxError as exc:
            print(f"SyntaxError: {exc}", file=sys.stderr)
            return 1

    # Resolve repo root and optional config path
    if args.config:
        config_path = pathlib.Path(args.config).resolve()
        repo_root = config_path.parent.parent
    else:
        repo_root = find_repo_root()
        config_path = None

    if args.restore:
        return run_restore(repo_root)

    # Default: dry_run=True unless --apply
    dry_run = not args.apply
    return run_migrate(repo_root, config_path, dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
