#!/usr/bin/env python3
"""docs_migrate.py — detect and migrate old-style docs to the wiki hierarchy.

Downstream-safe: walks up from cwd to find `.claude/grimoire-config.json` to
locate the project root (never looks for `claude-code/`). Read-only by default;
archives before any rewrite. Idempotent — second run is a no-op.

Exit codes:
  0 — no findings / apply succeeded / self-test passed
  1 — findings present (detect mode) / apply had unresolvable refs
  2 — hard error (bad args, unreadable tree, internal failure)

Usage:
  docs_migrate.py [--docs-root PATH] [--dry-run]
  docs_migrate.py --apply [--docs-root PATH]
  docs_migrate.py --self-test
"""

import glob
import os
import re
import shutil
import sys
import json
from datetime import datetime, timezone

# ── Classification constants ─────────────────────────────────────────────────

FLAT_TIER    = "FLAT_TIER"      # docs/ file without a tier subdir
ORPHAN       = "ORPHAN"         # not reachable from docs root via index links
ABSOLUTE_LINK = "ABSOLUTE_LINK" # contains absolute internal link (starts with /)
PROSE_LINK   = "PROSE_LINK"     # bare backtick ref to a known docs filename
NO_BREADCRUMB = "NO_BREADCRUMB" # missing breadcrumb up-link

# Files exempt from breadcrumb and ORPHAN checks (path-locked by framework)
EXEMPT_PATTERNS = (
    "release-planning-v",   # path-locked by release-plan-guard.sh
    "version-history.md",   # operator-facing, stays at docs/ top level
    "qa-ledger.md",         # ledger artifact, path-locked
)

# Regex for an existing breadcrumb line (canonical form from WH-0 decision)
BREADCRUMB_RE = re.compile(r">\s*\*\*Up:\*\*\s*\[")

# Regex for absolute internal links: href starting with / (not http)
ABSOLUTE_LINK_RE = re.compile(r"\[([^\]]*)\]\((/[^)]*)\)")

# Strip fenced and inline code before scanning
FENCE_RE       = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`]+`")

# All markdown link targets in a file
MD_LINK_TARGET_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

# README.md index link scanner
INDEX_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md[^)]*)\)")


def _strip_code(text: str) -> str:
    text = FENCE_RE.sub("", text)
    return INLINE_CODE_RE.sub("", text)


# ── Root detection (downstream-safe) ────────────────────────────────────────

class RootNotFoundError(SystemExit):
    """Raised when the project root cannot be located."""


def find_root(start: str = ".") -> str:
    """Walk up from start to find .claude/grimoire-config.json.

    Downstream-safe: does NOT require claude-code/ to be present, unlike
    doc_assurance.py's find_root which is scaffold-repo-only.
    """
    d = os.path.abspath(start)
    while d != "/":
        cfg = os.path.join(d, ".claude", "grimoire-config.json")
        if os.path.exists(cfg):
            return d
        d = os.path.dirname(d)
    raise RootNotFoundError(
        "project root not found: no .claude/grimoire-config.json found "
        "walking up from " + os.path.abspath(start)
    )


# ── File classification ──────────────────────────────────────────────────────

class DocClassifier:
    """Classify docs/ files into finding categories.

    Reuses the detection logic from doc_assurance.py where possible (the same
    absolute-link and prose-ref patterns), but adapted for downstream use where
    claude-code/ is absent.
    """

    def __init__(self, root: str, docs_root: str):
        self.root = root
        self.docs_root = docs_root  # absolute path to docs/ dir
        self._all_docs = self._collect_docs()
        self._known_basenames = {os.path.basename(p) for p in self._all_docs}
        self._index_set = self._collect_index_links()

    def _collect_docs(self):
        """All .md files under docs_root, excluding README.md files."""
        return sorted(
            p for p in glob.glob(
                os.path.join(self.docs_root, "**", "*.md"), recursive=True
            )
            if os.path.basename(p) != "README.md"
            and "/.git/" not in p
        )

    def _collect_index_links(self):
        """Build a set of abs paths reachable from docs/README.md via index links."""
        reachable = set()
        root_index = os.path.join(self.docs_root, "README.md")
        if not os.path.exists(root_index):
            return reachable
        queue = [root_index]
        visited = set()
        while queue:
            idx = queue.pop()
            if idx in visited:
                continue
            visited.add(idx)
            base = os.path.dirname(idx)
            try:
                text = open(idx).read()
            except OSError:
                continue
            for m in INDEX_LINK_RE.finditer(_strip_code(text)):
                target_raw = m.group(1).split("#")[0].split("?")[0].strip()
                if not target_raw:
                    continue
                target = os.path.normpath(os.path.join(base, target_raw))
                if os.path.exists(target):
                    reachable.add(target)
                    if target.endswith("README.md"):
                        queue.append(target)
        return reachable

    def _is_exempt(self, path: str) -> bool:
        basename = os.path.basename(path)
        return any(ex in basename for ex in EXEMPT_PATTERNS)

    def classify_file(self, path: str) -> list:
        """Return list of finding codes for a docs file."""
        findings = []
        if self._is_exempt(path):
            return findings

        # FLAT_TIER: sits directly in docs_root (no subdirectory)
        rel = os.path.relpath(path, self.docs_root)
        if "/" not in rel:
            findings.append(FLAT_TIER)

        # ORPHAN: not reachable from docs/README.md
        if self._index_set and path not in self._index_set:
            findings.append(ORPHAN)

        try:
            text = open(path).read()
        except OSError:
            return findings

        # NO_BREADCRUMB: missing > **Up:** [...] within first ~10 non-blank lines
        non_blank = [ln for ln in text.splitlines() if ln.strip()][:10]
        if not any(BREADCRUMB_RE.search(ln) for ln in non_blank):
            findings.append(NO_BREADCRUMB)

        stripped = _strip_code(text)

        # ABSOLUTE_LINK: internal links starting with /
        if ABSOLUTE_LINK_RE.search(stripped):
            findings.append(ABSOLUTE_LINK)

        # PROSE_LINK: bare backtick reference to a known docs basename
        # Scan raw text (before stripping) for backtick tokens matching doc names.
        # We use the original text here; inline code was stripped in stripped.
        for bn in self._known_basenames:
            # Bare backtick ref: `filename.md` that is NOT inside a markdown link
            if re.search(r"`" + re.escape(bn) + r"`", text):
                # Make sure it's not already a proper link target
                if not re.search(r"\(" + re.escape(bn) + r"\)", text):
                    findings.append(PROSE_LINK)
                    break

        return findings

    def classify_all(self) -> dict:
        """Return {abs_path: [finding_codes]} for all docs files with findings."""
        results = {}
        for p in self._all_docs:
            f = self.classify_file(p)
            if f:
                results[p] = f
        return results


# ── Archive support ──────────────────────────────────────────────────────────

class Archiver:
    """Archive files verbatim to .grimoire-archive/<timestamp>/ before rewrite."""

    def __init__(self, root: str):
        self.root = root
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.archive_dir = os.path.join(root, ".grimoire-archive", ts)
        self._manifest_lines = []
        self._created = False

    def _ensure_dir(self):
        if not self._created:
            os.makedirs(self.archive_dir, exist_ok=True)
            self._created = True

    def archive(self, path: str):
        """Copy path verbatim into the archive directory."""
        self._ensure_dir()
        rel = os.path.relpath(path, self.root)
        dest = os.path.join(self.archive_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        self._manifest_lines.append(f"- `{rel}` → `{os.path.relpath(dest, self.root)}`")

    def write_manifest(self):
        """Write MANIFEST.md listing all archived paths."""
        if not self._created:
            return
        manifest = os.path.join(self.archive_dir, "MANIFEST.md")
        ts = os.path.basename(self.archive_dir)
        lines = [
            f"# docs-migrate archive — {ts}",
            "",
            "Files archived verbatim before rewrite by `docs_migrate.py --apply`.",
            "",
        ] + self._manifest_lines + [""]
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))


# ── Breadcrumb insertion ─────────────────────────────────────────────────────

def _compute_relative_breadcrumb(path: str, docs_root: str) -> str:
    """Compute the breadcrumb up-link for a docs file.

    Points to the README.md in its parent directory if one exists,
    otherwise to the docs root README.md.
    """
    parent = os.path.dirname(path)
    parent_readme = os.path.join(parent, "README.md")
    docs_readme = os.path.join(docs_root, "README.md")

    if os.path.exists(parent_readme) and parent_readme != path:
        rel_path = "README.md"
        # Determine tier name from the directory
        tier_name = _tier_name(parent, docs_root)
    elif os.path.exists(docs_readme):
        rel_path = os.path.relpath(docs_readme, parent)
        tier_name = "Docs"
    else:
        return None

    return f"> **Up:** [↑ {tier_name}]({rel_path})"


def _tier_name(directory: str, docs_root: str) -> str:
    """Return a human-readable tier name for a directory."""
    rel = os.path.relpath(directory, docs_root)
    if rel == ".":
        return "Docs"
    # Use the last path component, title-cased
    parts = rel.split(os.sep)
    last = parts[-1]
    # Map known names
    KNOWN = {
        "design": "Design docs",
        "grimoire": "Grimoire tier",
        "ux": "UX",
        "coding-standards": "Coding standards",
    }
    return KNOWN.get(last, last.replace("-", " ").title())


def _insert_breadcrumb(text: str, breadcrumb: str) -> str:
    """Insert breadcrumb as the first non-blank, non-heading content after # Title."""
    lines = text.splitlines(keepends=True)
    # Find the first # heading line
    heading_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("#"):
            heading_idx = i
            break

    if heading_idx is None:
        # No heading found; insert at top
        return breadcrumb + "\n\n" + text

    # Find first non-blank line after the heading
    insert_after = heading_idx
    for i in range(heading_idx + 1, len(lines)):
        if lines[i].strip():
            insert_after = i - 1
            break
    else:
        insert_after = len(lines) - 1

    # Check if breadcrumb already present near the insertion point
    context = "".join(lines[heading_idx:min(heading_idx + 8, len(lines))])
    if BREADCRUMB_RE.search(context):
        return text  # already has breadcrumb

    new_lines = (
        lines[:heading_idx + 1]
        + ["\n", breadcrumb + "\n", "\n"]
        + lines[heading_idx + 1:]
    )
    return "".join(new_lines)


# ── Absolute-link rewriting ──────────────────────────────────────────────────

def _rewrite_absolute_links(text: str, path: str, root: str) -> tuple:
    """Rewrite absolute internal links to relative links where possible.

    Returns (new_text, unresolved_list).
    """
    unresolved = []
    file_dir = os.path.dirname(path)

    def replacer(m):
        label = m.group(1)
        href = m.group(2)
        # Only rewrite absolute paths (starting with /)
        if not href.startswith("/"):
            return m.group(0)
        # Try to resolve against root
        candidate = os.path.normpath(os.path.join(root, href.lstrip("/")))
        if os.path.exists(candidate):
            rel = os.path.relpath(candidate, file_dir)
            return f"[{label}]({rel})"
        # Unresolvable
        marker = f"<!-- docs-migrate: UNRESOLVED {href} -->"
        unresolved.append(href)
        return marker + f"[{label}]({href})"

    stripped = _strip_code(text)
    # Only process matches found in the stripped version
    new_text = text
    for m in ABSOLUTE_LINK_RE.finditer(stripped):
        original = m.group(0)
        rewritten = replacer(m)
        if rewritten != original:
            new_text = new_text.replace(original, rewritten, 1)

    return new_text, unresolved


# ── Apply engine ─────────────────────────────────────────────────────────────

class MigrateApplier:
    """Apply doc migrations: breadcrumbs + absolute-link rewrites."""

    def __init__(self, root: str, docs_root: str, dry_run: bool = False):
        self.root = root
        self.docs_root = docs_root
        self.dry_run = dry_run
        self.archiver = Archiver(root)
        self.unresolved_total = []

    def apply(self, findings: dict) -> int:
        """Apply migrations for all found files. Returns exit code."""
        if not findings:
            print("docs-migrate: nothing to do (no findings).")
            return 0

        paths_to_process = sorted(findings.keys())

        for path in paths_to_process:
            codes = findings[path]
            rel = os.path.relpath(path, self.root)
            try:
                text = open(path, encoding="utf-8").read()
            except OSError as e:
                print(f"ERROR: cannot read {rel}: {e}", file=sys.stderr)
                return 2

            original = text
            unresolved = []

            # Insert breadcrumb if missing
            if NO_BREADCRUMB in codes:
                bc = _compute_relative_breadcrumb(path, self.docs_root)
                if bc:
                    text = _insert_breadcrumb(text, bc)

            # Rewrite absolute links
            if ABSOLUTE_LINK in codes:
                text, ur = _rewrite_absolute_links(text, path, self.root)
                unresolved.extend(ur)

            if text == original:
                continue  # idempotent — nothing changed

            if self.dry_run:
                print(f"  [dry-run] would rewrite: {rel}")
                for ur in unresolved:
                    print(f"    UNRESOLVED: {ur}")
                continue

            # Archive before writing
            self.archiver.archive(path)

            # Write new content
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"  rewritten: {rel}")

            if unresolved:
                self.unresolved_total.extend(unresolved)
                for ur in unresolved:
                    print(f"    WARNING: UNRESOLVED ref left as marker: {ur}")

        if not self.dry_run:
            self.archiver.write_manifest()

        _print_manual_categories_summary(findings)

        if self.unresolved_total:
            _print_unresolved_banner(self.unresolved_total)
            return 1

        return 0


# Finding categories --apply auto-resolves vs. leaves for manual handling.
AUTO_RESOLVED_CATEGORIES = (NO_BREADCRUMB, ABSOLUTE_LINK)
DETECTION_ONLY_CATEGORIES = (PROSE_LINK, ORPHAN, FLAT_TIER)


def _print_manual_categories_summary(findings: dict) -> None:
    """Print which detected categories --apply does NOT auto-resolve.

    --apply only rewrites NO_BREADCRUMB and ABSOLUTE_LINK.  PROSE_LINK, ORPHAN,
    and FLAT_TIER are detection-only (manual fix), so an operator must not assume
    a clean --apply means zero findings remain (#188)."""
    remaining = {}
    for codes in findings.values():
        for code in codes:
            if code in DETECTION_ONLY_CATEGORIES:
                remaining[code] = remaining.get(code, 0) + 1
    if not remaining:
        return
    print("\ndocs-migrate: the following categories are detection-only and were")
    print("NOT auto-resolved by --apply (manual fix required):")
    for code in DETECTION_ONLY_CATEGORIES:
        if code in remaining:
            print(f"  {code}: {remaining[code]} finding(s) left untouched")
    print("Re-run detect mode (no --apply) to list them by file.")


def _print_unresolved_banner(refs: list):
    """Print a loud banner for unresolvable references."""
    print("\n" + "=" * 60, file=sys.stderr)
    print("docs-migrate: UNRESOLVED REFERENCES", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(
        "The following absolute links could not be resolved to a\n"
        "real file and were left as <!-- docs-migrate: UNRESOLVED --> markers.",
        file=sys.stderr,
    )
    for r in refs:
        print(f"  {r}", file=sys.stderr)
    print(
        "\nAction required: manually fix these references or delete\n"
        "the UNRESOLVED markers once you have verified the paths.",
        file=sys.stderr,
    )
    print("=" * 60 + "\n", file=sys.stderr)


# ── Self-test ────────────────────────────────────────────────────────────────

def self_test() -> int:
    """Deterministic in-memory self-test. Returns 0 (pass) or 1 (fail)."""
    import tempfile

    cases = []

    def check(cond: bool, label: str):
        ok = bool(cond)
        cases.append((label, ok))
        print(f"  {'PASS' if ok else 'FAIL'}: {label}")

    with tempfile.TemporaryDirectory() as tmp:
        root = tmp
        docs = os.path.join(root, "docs")
        design = os.path.join(docs, "design")
        os.makedirs(design)

        # Seed .claude/grimoire-config.json so find_root works
        claude_dir = os.path.join(root, ".claude")
        os.makedirs(claude_dir)
        with open(os.path.join(claude_dir, "grimoire-config.json"), "w") as fh:
            json.dump({"framework-version": "v3.37"}, fh)

        # Seed docs/README.md as root index
        with open(os.path.join(docs, "README.md"), "w") as fh:
            fh.write(
                "# Docs\n\n"
                "- [Design](design/README.md)\n"
                "- [Old flat](old-flat.md)\n"
            )

        # Seed docs/design/README.md
        with open(os.path.join(design, "README.md"), "w") as fh:
            fh.write(
                "# Design docs\n\n"
                "- [Feature A](feature-a-design.md)\n"
            )

        # ── Test 1: detect flat-tier file ──────────────────────────────────
        flat_path = os.path.join(docs, "old-flat.md")
        with open(flat_path, "w") as fh:
            fh.write("# Old Flat Doc\n\nSome content.\n")
        clf = DocClassifier(root, docs)
        findings = clf.classify_file(flat_path)
        check(FLAT_TIER in findings, "flat-tier file is classified FLAT_TIER")
        check(NO_BREADCRUMB in findings, "flat-tier file is classified NO_BREADCRUMB")

        # ── Test 2: detect no-breadcrumb file in design/ ──────────────────
        feat_path = os.path.join(design, "feature-a-design.md")
        with open(feat_path, "w") as fh:
            fh.write("# Feature A Design\n\nSome content.\n")
        clf = DocClassifier(root, docs)
        findings = clf.classify_file(feat_path)
        check(NO_BREADCRUMB in findings, "design doc without breadcrumb classified NO_BREADCRUMB")

        # ── Test 3: detect absolute link ───────────────────────────────────
        abs_path = os.path.join(design, "abs-link-design.md")
        with open(abs_path, "w") as fh:
            fh.write(
                "# Abs Link\n\n"
                "> **Up:** [↑ Design docs](README.md)\n\n"
                "See [this](/docs/design/feature-a-design.md) for details.\n"
            )
        clf = DocClassifier(root, docs)
        findings = clf.classify_file(abs_path)
        check(ABSOLUTE_LINK in findings, "file with absolute link classified ABSOLUTE_LINK")
        check(NO_BREADCRUMB not in findings, "file with breadcrumb not classified NO_BREADCRUMB")

        # ── Test 4: apply inserts breadcrumb ──────────────────────────────
        no_bc_path = os.path.join(design, "no-breadcrumb-design.md")
        with open(no_bc_path, "w") as fh:
            fh.write("# No Breadcrumb\n\nContent here.\n")
        # Rebuild classifier so it sees the new file
        clf = DocClassifier(root, docs)
        findings_before = clf.classify_all()
        applier = MigrateApplier(root, docs, dry_run=False)
        # Only apply to the no-bc file
        target_findings = {no_bc_path: [NO_BREADCRUMB]}
        rc = applier.apply(target_findings)
        check(rc == 0, "apply returns 0 on clean rewrite")
        text_after = open(no_bc_path).read()
        check(BREADCRUMB_RE.search(text_after), "apply inserted breadcrumb")

        # ── Test 5: idempotency — second apply is a no-op ─────────────────
        text_after_first = open(no_bc_path).read()
        clf2 = DocClassifier(root, docs)
        findings2 = clf2.classify_file(no_bc_path)
        # After applying, NO_BREADCRUMB should be gone
        check(NO_BREADCRUMB not in findings2, "idempotent: second classify sees no NO_BREADCRUMB")

        # ── Test 6: archive created on apply ──────────────────────────────
        archive_root = os.path.join(root, ".grimoire-archive")
        check(os.path.isdir(archive_root), "archive dir created on apply")
        manifests = glob.glob(os.path.join(archive_root, "*", "MANIFEST.md"))
        check(len(manifests) > 0, "MANIFEST.md written in archive dir")

        # ── Test 7: loud fallback for unresolvable absolute link ──────────
        unresolv_path = os.path.join(design, "unresolvable-design.md")
        with open(unresolv_path, "w") as fh:
            fh.write(
                "# Unresolvable\n\n"
                "> **Up:** [↑ Design docs](README.md)\n\n"
                "See [missing](/does/not/exist.md) for details.\n"
            )
        applier2 = MigrateApplier(root, docs, dry_run=False)
        rc2 = applier2.apply({unresolv_path: [ABSOLUTE_LINK]})
        check(rc2 == 1, "unresolvable ref returns exit code 1")
        text_after_unresolv = open(unresolv_path).read()
        check(
            "<!-- docs-migrate: UNRESOLVED" in text_after_unresolv,
            "unresolvable ref leaves UNRESOLVED marker in file"
        )

        # ── Test 8: dry-run does not write ────────────────────────────────
        dry_path = os.path.join(design, "dry-run-design.md")
        with open(dry_path, "w") as fh:
            fh.write("# Dry Run\n\nContent.\n")
        original_mtime = os.path.getmtime(dry_path)
        import time; time.sleep(0.01)
        applier3 = MigrateApplier(root, docs, dry_run=True)
        applier3.apply({dry_path: [NO_BREADCRUMB]})
        new_mtime = os.path.getmtime(dry_path)
        check(abs(new_mtime - original_mtime) < 0.005, "dry-run does not modify files")

        # ── Test 9: exempt files are not classified ────────────────────────
        exempt_path = os.path.join(docs, "release-planning-v1.0.md")
        with open(exempt_path, "w") as fh:
            fh.write("# Release Planning v1.0\n\nContent.\n")
        clf3 = DocClassifier(root, docs)
        findings_exempt = clf3.classify_file(exempt_path)
        check(len(findings_exempt) == 0, "exempt file (release-planning-v*) has no findings")

        # ── Test 10: find_root works from subdirectory ─────────────────────
        found = find_root(design)
        check(found == root, "find_root walks up to .claude/grimoire-config.json")

        # ── Test 11: --apply prints a detection-only summary (#188) ─────────
        import io as _io11
        from contextlib import redirect_stdout as _rs11
        buf = _io11.StringIO()
        with _rs11(buf):
            _print_manual_categories_summary(
                {"a.md": [PROSE_LINK], "b.md": [ORPHAN, FLAT_TIER]}
            )
        out11 = buf.getvalue()
        check(
            "detection-only" in out11
            and "PROSE_LINK" in out11
            and "ORPHAN" in out11
            and "FLAT_TIER" in out11,
            "--apply summary lists detection-only categories left untouched"
        )
        # No detection-only findings → silent (only auto-resolved categories).
        buf2 = _io11.StringIO()
        with _rs11(buf2):
            _print_manual_categories_summary({"c.md": [NO_BREADCRUMB, ABSOLUTE_LINK]})
        check(buf2.getvalue() == "", "--apply summary silent when only auto-resolved categories present")

    passed = sum(1 for _, ok in cases if ok)
    failed = sum(1 for _, ok in cases if not ok)
    print(f"\ndocs-migrate self-test: {passed} passed, {failed} failed.")
    return 0 if failed == 0 else 1


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    """CLI entry point. Returns process exit code."""
    args = argv if argv is not None else sys.argv[1:]

    if "--self-test" in args:
        return self_test()

    apply_mode = "--apply" in args
    dry_run    = "--dry-run" in args

    # --docs-root override
    docs_root_override = None
    if "--docs-root" in args:
        idx = args.index("--docs-root")
        if idx + 1 >= len(args):
            print("ERROR: --docs-root requires a path argument.", file=sys.stderr)
            return 2
        docs_root_override = os.path.abspath(args[idx + 1])

    try:
        root = find_root(".")
    except SystemExit as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    docs_root = docs_root_override or os.path.join(root, "docs")
    if not os.path.isdir(docs_root):
        print(f"ERROR: docs root not found: {docs_root}", file=sys.stderr)
        return 2

    clf = DocClassifier(root, docs_root)
    findings = clf.classify_all()

    # ── Detect / report mode ──────────────────────────────────────────────
    if not apply_mode:
        if not findings:
            print("docs-migrate: 0 findings — docs tree is conformant.")
            return 0

        total = sum(len(v) for v in findings.values())
        print(f"docs-migrate: {total} finding(s) across {len(findings)} file(s).")
        for path in sorted(findings):
            rel = os.path.relpath(path, root)
            codes = findings[path]
            print(f"  {rel}: {' '.join(codes)}")
        if dry_run:
            print("\n(dry-run: no files modified)")
        return 1

    # ── Apply mode ────────────────────────────────────────────────────────
    if dry_run:
        print("docs-migrate: --apply --dry-run — showing what would change:")

    applier = MigrateApplier(root, docs_root, dry_run=dry_run)
    return applier.apply(findings)


if __name__ == "__main__":
    sys.exit(main())
