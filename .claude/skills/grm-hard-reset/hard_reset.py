#!/usr/bin/env python3
"""hard_reset.py — archive-restore automation for grm-hard-reset
(maintenance-automation-design.md item 3, v3.58).

The `grm-hard-reset` skill (SKILL.md) already archives every project-local
file (and any customised framework file) to `.grimoire-archive/<ts>/` before
clearing/restoring the scaffold, and writes a `MANIFEST.md` recording, per
archived path, its class and original repo-relative location (the same
`| Original path | Class | Status |` markdown-table shape
`grm-regenerate-grimoire/regenerate_grimoire.py`'s `_write_archive_manifest`
uses for the identical `.grimoire-archive/<ts>/MANIFEST.md` mechanism — hard-
reset's own design doc, `hard-reset-design.md` §2.2, specifies the same
class + original-location contract).

Until now, recovering from an archive was a **manual filesystem copy-back**
guided by reading `MANIFEST.md` by eye (`hard-reset-design.md` Follow-ups).
This script automates that: `--restore <ts>` reads `MANIFEST.md` under
`.grimoire-archive/<ts>/` and copies every archived file back to its original
location.

Loud-fail-on-ambiguity posture (mirrors `docs_migrate.py` / `vendor_migrate.py`
/ `config_validate.py --migrate`):
  - a missing/unreadable `MANIFEST.md` -> hard refusal, non-zero exit, no
    files touched.
  - a listed file whose CURRENT on-disk content differs from both (a) what is
    already in the archive AND (b) is otherwise present -> restoring would
    silently clobber a file that changed since the archive was taken. This is
    refused unless `--force` is passed (same opt-in-clobber idiom as
    `vendor_migrate.py`'s `_guard_clobber`: "never stomp ... `--force` opts in
    deliberately").
  - restoring a path whose current content is IDENTICAL to the archived copy,
    or that does not exist on disk at all, is never a clobber — no `--force`
    needed.

This script does NOT implement the archive side (Steps 1-2 of SKILL.md) —
only the restore side (the Follow-up this design item closes). It does not
touch git; it is a plain filesystem copy-back, exactly like the archive it
reads. It never merges, pushes, or runs git commands.

Standard: Python 3 stdlib-only (scripting-unification standard; rationale in
the upstream Grimoire repository, framework-internal).

Exit codes:
  0 — restore succeeded (or --self-test passed)
  1 — refused: missing/unreadable manifest, or a clobber without --force
  2 — bad input (bad args, unreadable archive tree)

CLI:
  hard_reset.py --restore TIMESTAMP [--root DIR] [--force] [--dry-run]
  hard_reset.py --self-test
"""
from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import sys

# ── Constants ────────────────────────────────────────────────────────────────
ARCHIVE_DIR = ".grimoire-archive"
MANIFEST_NAME = "MANIFEST.md"
# Matches the `| Original path | Class | Status |` table row
# `grm-regenerate-grimoire/regenerate_grimoire.py::_write_archive_manifest`
# writes for the same `.grimoire-archive/<ts>/MANIFEST.md` mechanism (and the
# per-path class + original-location contract hard-reset-design.md §2.2
# specifies for its own MANIFEST.md).
MANIFEST_ROW_RE = re.compile(
    r"^\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$")
STATUS_ARCHIVED = "archived"
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BAD_INPUT = 2


class HardResetError(Exception):
    """Raised on a missing manifest or unreadable archive (-> exit 2)."""


class ClobberError(Exception):
    """Raised when a restore would silently overwrite changed on-disk state
    without --force (-> exit 1)."""

    def __init__(self, conflicts):
        self.conflicts = conflicts
        super().__init__(
            "restore would clobber %d file(s) that changed since the "
            "archive was taken (pass --force to overwrite): %s"
            % (len(conflicts), ", ".join(conflicts)))


# ── Manifest parsing ─────────────────────────────────────────────────────────
class ManifestEntry:
    """One archived-file row: its repo-relative original path + class."""

    __slots__ = ("original_path", "file_class", "status")

    def __init__(self, original_path, file_class, status):
        self.original_path = original_path
        self.file_class = file_class
        self.status = status


class Manifest:
    """Parse an archive's MANIFEST.md into a list of ManifestEntry rows.

    Tolerant of the table header/separator rows (skipped — they never match
    MANIFEST_ROW_RE because the header cell isn't backtick-quoted and the
    separator is all dashes/colons).
    """

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                self.text = fh.read()
        except OSError as exc:
            raise HardResetError(
                "manifest not found or unreadable: %s (%s)" % (path, exc))
        self.entries = self._parse()

    def _parse(self):
        entries = []
        for line in self.text.splitlines():
            m = MANIFEST_ROW_RE.match(line)
            if not m:
                continue
            original_path, file_class, status = m.groups()
            entries.append(ManifestEntry(original_path.strip(),
                                          file_class.strip(),
                                          status.strip()))
        if not entries:
            raise HardResetError(
                "manifest %s has no parseable archived-file rows" % self.path)
        return entries

    def restorable(self):
        """Entries actually copied into the archive (status == 'archived') —
        an 'absent' row (present in grm-regenerate-grimoire's variant of this
        format) had nothing to copy back."""
        return [e for e in self.entries if e.status.lower() == STATUS_ARCHIVED]


# ── Restore engine ───────────────────────────────────────────────────────────
class ArchiveRestorer:
    """Copy every archived file in a timestamped archive back to its original
    location, refusing on a missing manifest or an unconfirmed clobber."""

    def __init__(self, root, timestamp):
        self.root = root
        self.timestamp = timestamp
        self.archive_dir = os.path.join(root, ARCHIVE_DIR, timestamp)
        self.manifest_path = os.path.join(self.archive_dir, MANIFEST_NAME)

    def _archived_file(self, entry):
        return os.path.join(self.archive_dir, entry.original_path)

    def _original_file(self, entry):
        return os.path.join(self.root, entry.original_path)

    def plan(self):
        """Return (Manifest, [ManifestEntry with an existing archived copy]).

        Raises HardResetError if the manifest is missing/unreadable, or if an
        entry's own archived copy is missing from disk (the archive itself is
        incomplete — refuse rather than partially restore).
        """
        if not os.path.isfile(self.manifest_path):
            raise HardResetError(
                "no MANIFEST.md under %s — refusing to restore an archive "
                "whose manifest is missing (nothing was touched)"
                % self.archive_dir)
        manifest = Manifest(self.manifest_path)
        restorable = manifest.restorable()
        missing_copies = [e.original_path for e in restorable
                           if not os.path.isfile(self._archived_file(e))]
        if missing_copies:
            raise HardResetError(
                "manifest lists %d file(s) whose archived copy is missing "
                "from %s — refusing a partial restore: %s"
                % (len(missing_copies), self.archive_dir,
                   ", ".join(missing_copies)))
        return manifest, restorable

    def _is_clobber(self, entry):
        """True iff the destination exists AND differs from the archived
        copy — i.e. restoring would silently discard on-disk changes made
        since the archive was taken. A destination that doesn't exist, or
        that is byte-identical to the archived copy, is never a clobber."""
        dest = self._original_file(entry)
        if not os.path.exists(dest):
            return False
        src = self._archived_file(entry)
        return not filecmp.cmp(src, dest, shallow=False)

    def restore(self, force=False, dry_run=False):
        """Copy every restorable entry back to its original location.

        Returns {"timestamp", "restored": [...], "skipped": [...]}. Raises
        ClobberError (exit 1) if any destination would be silently clobbered
        and force is False — nothing is written in that case (checked over
        the WHOLE plan before any copy, so a partial restore never happens).
        """
        _manifest, restorable = self.plan()

        conflicts = [] if force else [
            e.original_path for e in restorable if self._is_clobber(e)]
        if conflicts:
            raise ClobberError(conflicts)

        restored, skipped = [], []
        for entry in restorable:
            dest = self._original_file(entry)
            src = self._archived_file(entry)
            if os.path.exists(dest) and filecmp.cmp(src, dest, shallow=False):
                skipped.append(entry.original_path)  # already identical
                continue
            if dry_run:
                restored.append(entry.original_path)
                continue
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            shutil.copy2(src, dest)
            restored.append(entry.original_path)
        return {"timestamp": self.timestamp, "restored": restored,
                "skipped": skipped}


# ── Self-test (temp-directory fixtures; no real archive needed) ─────────────
def _write_manifest(archive_dir, rows):
    lines = ["# Hard-reset archive — fixture", "",
             "| Original path | Class | Status |", "|---|---|---|"]
    for rel, cls, status in rows:
        lines.append("| `%s` | %s | %s |" % (rel, cls, status))
    lines.append("")
    with open(os.path.join(archive_dir, MANIFEST_NAME), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _seed_archive(root, ts, files):
    """files: {rel_path: content}. Writes both the archived copy and the
    manifest; does NOT create the original (caller decides)."""
    archive_dir = os.path.join(root, ARCHIVE_DIR, ts)
    os.makedirs(archive_dir, exist_ok=True)
    rows = []
    for rel, content in files.items():
        dest = os.path.join(archive_dir, rel)
        os.makedirs(os.path.dirname(dest) or archive_dir, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
        rows.append((rel, "project-local", STATUS_ARCHIVED))
    _write_manifest(archive_dir, rows)
    return archive_dir


def _self_test():
    import tempfile

    failures = []

    def check(cond, label):
        ok = bool(cond)
        print("  %s: %s" % ("PASS" if ok else "FAIL", label))
        if not ok:
            failures.append(label)

    # ── Case 1: successful restore (dest absent) ────────────────────────
    with tempfile.TemporaryDirectory() as root:
        ts = "20260101-000000"
        _seed_archive(root, ts, {
            "docs/roadmap.md": "archived roadmap\n",
            "CLAUDE.md": "archived claude md\n",
        })
        restorer = ArchiveRestorer(root, ts)
        result = restorer.restore()
        check(set(result["restored"]) == {"docs/roadmap.md", "CLAUDE.md"},
              "successful restore: both files reported restored")
        roadmap = os.path.join(root, "docs", "roadmap.md")
        check(os.path.isfile(roadmap) and
              open(roadmap, encoding="utf-8").read() == "archived roadmap\n",
              "successful restore: file content copied back correctly")
        claude = os.path.join(root, "CLAUDE.md")
        check(os.path.isfile(claude), "successful restore: nested + top-level paths both land")

    # ── Case 2: missing manifest -> loud refusal, nothing touched ───────
    with tempfile.TemporaryDirectory() as root:
        ts = "20260101-000001"
        os.makedirs(os.path.join(root, ARCHIVE_DIR, ts))  # no MANIFEST.md
        restorer = ArchiveRestorer(root, ts)
        raised = False
        try:
            restorer.restore()
        except HardResetError as exc:
            raised = True
            check("MANIFEST.md" in str(exc) or "manifest" in str(exc).lower(),
                  "missing-manifest refusal names the manifest")
        check(raised, "missing manifest raises HardResetError (loud refusal)")
        check(not os.listdir(os.path.join(root, ARCHIVE_DIR, ts)),
              "missing-manifest refusal leaves the archive dir untouched")

    # ── Case 3: clobber refusal without --force ─────────────────────────
    with tempfile.TemporaryDirectory() as root:
        ts = "20260101-000002"
        _seed_archive(root, ts, {"docs/roadmap.md": "OLD content\n"})
        # Simulate on-disk drift since the archive: a DIFFERENT current file.
        dest = os.path.join(root, "docs", "roadmap.md")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("NEW content written after the archive was taken\n")

        restorer = ArchiveRestorer(root, ts)
        raised = False
        try:
            restorer.restore(force=False)
        except ClobberError as exc:
            raised = True
            check("docs/roadmap.md" in exc.conflicts,
                  "clobber error names the conflicting path")
        check(raised, "clobber without --force raises ClobberError")
        check(open(dest, encoding="utf-8").read().startswith("NEW content"),
              "clobber-refusal-without-force leaves the current file untouched")

        # --force opts in and overwrites deliberately.
        result = restorer.restore(force=True)
        check("docs/roadmap.md" in result["restored"],
              "--force restores over a changed file")
        check(open(dest, encoding="utf-8").read() == "OLD content\n",
              "--force actually overwrites with the archived content")

    # ── Case 4: identical on-disk file is never a clobber (no --force needed) ──
    with tempfile.TemporaryDirectory() as root:
        ts = "20260101-000003"
        _seed_archive(root, ts, {"docs/roadmap.md": "same content\n"})
        dest = os.path.join(root, "docs", "roadmap.md")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("same content\n")
        restorer = ArchiveRestorer(root, ts)
        result = restorer.restore(force=False)  # must NOT raise
        check("docs/roadmap.md" in result["skipped"],
              "byte-identical destination is skipped, not clobbered")

    # ── Case 5: incomplete archive (manifest row w/ missing archived copy) ──
    with tempfile.TemporaryDirectory() as root:
        ts = "20260101-000004"
        archive_dir = os.path.join(root, ARCHIVE_DIR, ts)
        os.makedirs(archive_dir)
        # Manifest references a file that was never actually copied in.
        _write_manifest(archive_dir, [("docs/ghost.md", "project-local",
                                       STATUS_ARCHIVED)])
        restorer = ArchiveRestorer(root, ts)
        raised = False
        try:
            restorer.restore()
        except HardResetError as exc:
            raised = True
            check("ghost.md" in str(exc), "incomplete-archive error names the missing path")
        check(raised, "manifest row with no archived copy refuses (incomplete archive)")

    # ── Case 6: 'absent' status rows are not restored (nothing to copy) ──
    with tempfile.TemporaryDirectory() as root:
        ts = "20260101-000005"
        archive_dir = os.path.join(root, ARCHIVE_DIR, ts)
        os.makedirs(archive_dir)
        _write_manifest(archive_dir, [("docs/never-existed.md",
                                       "framework", "absent")])
        restorer = ArchiveRestorer(root, ts)
        result = restorer.restore()
        check(result["restored"] == [] and result["skipped"] == [],
              "'absent' manifest rows are excluded from restore (nothing archived)")

    # ── Case 7: dry-run reports without writing ─────────────────────────
    with tempfile.TemporaryDirectory() as root:
        ts = "20260101-000006"
        _seed_archive(root, ts, {"docs/roadmap.md": "dry run content\n"})
        restorer = ArchiveRestorer(root, ts)
        result = restorer.restore(dry_run=True)
        check("docs/roadmap.md" in result["restored"],
              "dry-run reports the file it would restore")
        check(not os.path.exists(os.path.join(root, "docs", "roadmap.md")),
              "dry-run does not actually write the file")

    print()
    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("hard_reset self-test: OK (successful restore, missing-manifest "
          "refusal, clobber-refusal-without-force + --force override, "
          "identical-file no-op, incomplete-archive refusal, absent-row "
          "exclusion, dry-run)")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Restore a grm-hard-reset archive from its MANIFEST.md "
                    "(maintenance-automation-design.md item 3).")
    ap.add_argument("--restore", metavar="TIMESTAMP", default=None,
                    help="restore .grimoire-archive/TIMESTAMP/ per its "
                         "MANIFEST.md")
    ap.add_argument("--root", default=".")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a destination file that changed since "
                         "the archive was taken (refused by default)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be restored without writing")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.restore:
        ap.error("--restore TIMESTAMP is required (or --self-test)")

    try:
        restorer = ArchiveRestorer(args.root, args.restore)
        result = restorer.restore(force=args.force, dry_run=args.dry_run)
    except HardResetError as exc:
        print("hard_reset: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    except ClobberError as exc:
        print("hard_reset: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED

    verb = "would restore" if args.dry_run else "restored"
    print("hard_reset: %s %d file(s) from %s/%s" %
          (verb, len(result["restored"]), ARCHIVE_DIR, args.restore))
    for rel in result["restored"]:
        print("  %s %s" % ("~" if args.dry_run else "+", rel))
    if result["skipped"]:
        print("  (%d file(s) already identical on disk, skipped)"
              % len(result["skipped"]))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
