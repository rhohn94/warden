#!/usr/bin/env python3
"""grm_namespacing — deterministic, reusable transformer that namespaces every
Grimoire skill `<name>` to `grm-<name>` and rewrites every reference.

This is the *migrate engine* for the GN epic (v3.42):
  * GN-1 renames the framework's own skills (run against this repo).
  * GN-3's consumer migrate row invokes the SAME logic against a managed
    project's `.claude/skills/` tree, so the rules here are the contract.

The transformer is stdlib-only and idempotent. It implements two reference
rewrite tiers (see the design doc, grm-namespacing-design.md):

  Tier 1 — PATH references (aggressive, unambiguous):
      every `skills/<name>/` -> `skills/grm-<name>/` for each KNOWN skill name,
      across all text files. This covers `.claude/skills/<name>/`,
      golden `skills/<name>/`, `copilot/.claude/skills/<name>/`, and relative
      forms — they all share the `skills/<name>/` substring.

  Tier 2 — bare-name prose (conservative, to avoid corrupting common-word
      skill names like `grm-iterate`, `grm-agent-scout`, `grm-agent-reviewer`):
      (a) a backticked token EXACTLY equal to a known name: `<name>` -> `grm-<name>`
      (b) `<name> skill` / `skill <name>` / `the <name> skill` patterns.

  Tier 3 — denylist / lookalike guard (#466; see design doc §Tier 3): even
      Tier 2's conservatism isn't enough for two cases discovered on a
      downstream consumer sync:
      (a) a small file-path DENYLIST for known meta-files whose OLD-name
          tokens are prose describing the migration itself (a self-test
          fixture simulating pre-migration state, a stable feature-id
          column, bare-name migration guidance) — never a live reference.
          These files are skipped by the rewriter entirely.
      (b) a LOOKALIKE_NAMES set of skill base-names that also carry a
          legitimate non-skill sense in this framework's own vocabulary (a
          `just` recipe target, a config-dial key). For a lookalike name,
          the aggressive Tier-2a bare-backtick rewrite is suppressed — only
          the unambiguous Tier-2b "<name> skill" / "skill <name>" phrasing
          still fires, since that phrasing can only mean the skill.

Directory renames preserve git history via `git mv` (falling back to os.rename
when the path is untracked / not in a repo).

Post-sync collision handling: a consumer that ran `grm-sync-from-upstream`
already received the new `grm-<name>/` skill (added non-destructively by the
file-walk) while the old bare-named `<name>/` still sits beside it. In that
state the synced `grm-<name>/` is authoritative, so this transformer ARCHIVES
the stale bare-named dir to `.grimoire-archive/grm-namespacing-<ts>/` and then
REMOVES it — it never `git mv`s onto the existing dir (which would nest it as
`grm-<name>/<name>/`). This is what completes the cutover for an already-synced
project.

Usage:
    python3 grm_namespacing.py --root <repo-root> [--apply] [--dry-run]
    python3 grm_namespacing.py --self-test

Default mode is --dry-run (report only). Pass --apply to mutate the tree.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

PREFIX = "grm-"

# Directories never touched (anywhere in the tree), matched by name alone.
EXCLUDED_DIR_NAMES = {".git", ".grimoire-archive", ".grimoire-golden", "dist", "node_modules", "__pycache__"}

# Canonical renames beyond the plain grm- prefix (#308, agent-role convention):
# the role skills were later renamed grm-<role> -> grm-agent-<role>. A
# pre-namespacing project still holds the ORIGINAL bare role dirs, so the
# migrate must land them on TODAY'S canonical names — a plain prefix would
# mint a ghost (grm-scout/) beside the real synced skill (grm-agent-scout/).
# A stale grm-era ghost dir (grm-scout/ itself, synced between v3.42 and #308)
# is healed the same way.
CANONICAL_RENAMES = {
    "scout": "agent-scout",
    "reporter": "agent-reporter",
    "reviewer": "agent-reviewer",
    "verifier": "agent-verifier",
    "triager": "agent-triager",
    "qa-agent": "agent-qa",
    "researcher": "agent-researcher",
    "environment-manager": "agent-environment-manager",
    "status-broker": "agent-status-broker",
}


def canonical_base(name: str) -> str:
    """Today's canonical base for a bare (or stale grm-era) skill name."""
    return CANONICAL_RENAMES.get(name, name)


# -- Tier 3: denylist / lookalike guard (#466) ------------------------------
#
# Tier 2's conservatism (backticked-exact-token / "<name> skill" phrasing)
# still corrupts two classes of content the reference-rewriter cannot
# disambiguate from a live reference:
#
#   (a) DENYLISTED_FILE_SUFFIXES — files that are themselves ABOUT the
#       migration: a self-test fixture that deliberately constructs
#       pre-migration (OLD-name) paths to simulate the transitional state,
#       a stable feature-id column key, or prose enumerating the OLD bare
#       names as migration guidance. Rewriting these turns correct
#       self-tests into crashes and collapses illustrative "OLD -> NEW"
#       examples into self-referential no-ops. Matched by path SUFFIX (not
#       exact root-relative path) so the same denylist protects every flavor
#       mirror (root, claude-code/) without per-flavor duplication.
#
#   (b) LOOKALIKE_NAMES — skill base-names that also have a legitimate
#       non-skill sense in this framework's own vocabulary: a `just` recipe
#       target (`sync-deps`, `iterate`) or a config-dial / role noun
#       (`project-manager`, `github-pr`). `` `sync-deps` `` inside a recipe
#       table means the just-recipe, not the `grm-sync-deps` skill, and
#       Tier-2a's bare-backtick rule cannot tell the two senses apart. For a
#       lookalike name, only the unambiguous Tier-2b "<name> skill" /
#       "skill <name>" phrasing is still rewritten.

DENYLISTED_FILE_SUFFIXES: tuple[str, ...] = (
    "grm-sync-from-upstream/grm_namespacing.py",
    "grm-sync-from-upstream/feature-manifest.md",
    "grm-sync-from-upstream/reference.md",
    "grm-workflow-bootstrap/generate_golden.py",
    # Discovered during #466's fix (not in the original report): the
    # append-only engineering ledger records each release's OLD -> NEW name
    # the day it happened ("`doc-assurance` -> `grm-doc-assurance`" entries,
    # and bug-postmortem prose contrasting a broken OLD-named reference
    # against the fixed NEW one). Same failure class as feature-manifest.md's
    # id column: rewriting the OLD side collapses the historical record into
    # a self-referential no-op.
    "docs/version-history.md",
)

LOOKALIKE_NAMES: frozenset[str] = frozenset({
    "sync-deps",        # just-recipe target vs. the grm-sync-deps skill
    "iterate",          # just-recipe target / generic verb vs. grm-iterate
    "project-manager",  # work-paradigm role noun vs. grm-project-manager
    "github-pr",        # generic feature name vs. the grm-github-pr skill
})


def is_denylisted_file(rel_posix: str) -> bool:
    """True if *rel_posix* (repo-root-relative, POSIX separators) is a known
    meta-file about the migration itself, exempt from all reference rewrites."""
    return any(rel_posix.endswith(suffix) for suffix in DENYLISTED_FILE_SUFFIXES)

# Relative path segment that marks the vendored third-party tree.  Any directory
# whose path contains this segment is treated as a boundary the same way a git
# submodule is — we never plan renames inside vendored dependencies.
VENDORED_SEGMENT = "lib/third-party"

# File suffixes treated as rewritable text.
TEXT_SUFFIXES = {".md", ".py", ".sh", ".json", ".toml", ".txt", ".yml", ".yaml", ".js"}
# Extensionless text files we still rewrite (agent defs, config stubs, CLAUDE/AGENTS).
TEXT_NAMES = {"CLAUDE.md", "AGENTS.md"}


@dataclass
class TransformReport:
    """Accumulates what the transformer did (or would do in dry-run)."""

    dirs_renamed: list[tuple[str, str]] = field(default_factory=list)
    # Stale bare-named dirs archived+removed because grm-<name>/ already existed
    # (the post-sync collision case): (old_rel, existing_grm_rel).
    dirs_removed: list[tuple[str, str]] = field(default_factory=list)
    frontmatter_updated: list[str] = field(default_factory=list)
    files_rewritten: dict[str, int] = field(default_factory=dict)  # path -> edit count

    def merge(self, other: "TransformReport") -> None:
        self.dirs_renamed.extend(other.dirs_renamed)
        self.dirs_removed.extend(other.dirs_removed)
        self.frontmatter_updated.extend(other.frontmatter_updated)
        for k, v in other.files_rewritten.items():
            self.files_rewritten[k] = self.files_rewritten.get(k, 0) + v

    def summary(self) -> str:
        return (
            f"dirs_renamed={len(self.dirs_renamed)} "
            f"dirs_removed={len(self.dirs_removed)} "
            f"frontmatter_updated={len(self.frontmatter_updated)} "
            f"files_rewritten={len(self.files_rewritten)} "
            f"total_edits={sum(self.files_rewritten.values())}"
        )


class GrmNamespacer:
    """Applies the grm- namespacing rules to a repository tree.

    Construct with a root path, call discover_skill_names() to enumerate the
    known skill set programmatically, then run() to apply (or preview) the
    transform. The instance is reusable across roots via a fresh construction.
    """

    def __init__(self, root: Path, apply: bool = False) -> None:
        self.root = Path(root).resolve()
        self.apply = apply
        self.names: list[str] = []
        self.report = TransformReport()
        # One archive root per run; created lazily on first archived dir.
        self.archive_root = (
            self.root / ".grimoire-archive" / f"grm-namespacing-{datetime.now():%Y%m%d-%H%M%S}"
        )
        # Absolute paths of trees that must not be entered: git submodules and
        # lib/third-party/ vendored dependencies.
        self._excluded_roots: frozenset[Path] = self._load_excluded_roots()

    # -- submodule / vendored boundary detection ---------------------------

    def _load_excluded_roots(self) -> frozenset[Path]:
        """Return the set of absolute directory paths that must never be entered.

        Two sources of exclusion:
        1. Git submodules — parsed from ``<root>/.gitmodules`` (the ``path =``
           lines) *and* detected by scanning for nested ``.git`` files or
           directories anywhere under root, which covers submodules registered
           in a parent repo's ``.gitmodules`` even when that file lives outside
           our root.
        2. ``lib/third-party/`` vendored trees — any directory whose path
           contains the ``lib/third-party`` segment.
        """
        excluded: set[Path] = set()

        # 1a. Parse .gitmodules if present.
        gitmodules = self.root / ".gitmodules"
        if gitmodules.is_file():
            try:
                for line in gitmodules.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.lower().startswith("path"):
                        # Format: "path = relative/path/to/submodule"
                        _, _, value = line.partition("=")
                        rel = value.strip()
                        if rel:
                            excluded.add((self.root / rel).resolve())
            except OSError:
                pass

        # 1b. Walk the tree looking for nested .git entries (catches submodules
        # registered in an ancestor repo's .gitmodules that we can't see here).
        for dirpath, dirnames, filenames in os.walk(self.root):
            current = Path(dirpath).resolve()
            if current == self.root:
                # Skip the repo's own .git at the root.
                continue
            if ".git" in dirnames or ".git" in filenames:
                excluded.add(current)
                # Do not descend further into this submodule — os.walk cannot
                # prune here (we're not in the walk that matters), so we just
                # record the root and rely on _is_excluded_root() to gate later.

        # 2. lib/third-party/ vendored segments.
        vendored = self.root / "lib" / "third-party"
        if vendored.is_dir():
            excluded.add(vendored.resolve())

        return frozenset(excluded)

    def _is_excluded_root(self, path: Path) -> bool:
        """Return True if *path* is at or inside any excluded root."""
        resolved = path.resolve()
        for excl in self._excluded_roots:
            try:
                resolved.relative_to(excl)
                return True
            except ValueError:
                pass
        return False

    # -- discovery ---------------------------------------------------------

    def _skills_parents(self) -> list[Path]:
        """Every directory literally named `skills` under root (excluding the
        excluded dirs), i.e. the flavor `.claude/skills` trees. (The former
        embedded `workflow-bootstrap/golden/skills` tree no longer exists — golden
        is generated, and its cache dir is excluded from the walk.)"""
        parents: list[Path] = []
        for dirpath, dirnames, _ in os.walk(self.root):
            current = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDED_DIR_NAMES
                and not self._is_excluded_root(current / d)
            ]
            for d in dirnames:
                if d == "skills":
                    parents.append(current / d)
        return sorted(parents)

    def discover_skill_names(self) -> list[str]:
        """Programmatically enumerate skill names: any immediate child dir of a
        `skills/` parent that contains a SKILL.md (or any file) and is not
        already grm-prefixed. The union across all skills parents is the known
        set."""
        names: set[str] = set()
        for parent in self._skills_parents():
            for child in parent.iterdir():
                if not child.is_dir():
                    continue
                if child.name in EXCLUDED_DIR_NAMES:
                    continue
                # golden/worktrees subdirs of a skill are not skills themselves;
                # a skill dir is a direct child of a `skills/` dir.
                base = child.name[len(PREFIX):] if child.name.startswith(PREFIX) else child.name
                names.add(base)
        self.names = sorted(names)
        return self.names

    # -- directory rename --------------------------------------------------

    def _git_mv(self, src: Path, dst: Path) -> None:
        """Rename preserving git history when possible."""
        if not self.apply:
            return
        try:
            subprocess.run(
                ["git", "-C", str(self.root), "mv", str(src), str(dst)],
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Untracked path or no git — plain rename; git detects on add.
            os.rename(src, dst)

    def _archive_dir(self, src: Path) -> None:
        """Copy src into this run's archive root, preserving its repo-relative
        path, so a removed original stays recoverable. No-op in dry-run."""
        if not self.apply:
            return
        dest = self.archive_root / src.relative_to(self.root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)

    def _git_rm(self, path: Path) -> None:
        """Remove a directory, preferring `git rm` so the deletion is staged."""
        if not self.apply:
            return
        try:
            subprocess.run(
                ["git", "-C", str(self.root), "rm", "-r", "-q", "--", str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Untracked path or no git — plain recursive delete.
            shutil.rmtree(path)

    def rename_dirs(self) -> None:
        # Deepest-first: a `skills/` parent may be NESTED under another skill
        # dir (e.g. workflow-bootstrap/golden/skills lives under the
        # grm-workflow-bootstrap skill). Renaming the outer skill first would
        # invalidate the inner parent's path mid-loop. Process by descending
        # path depth so inner parents are renamed before their containing dir.
        parents = sorted(
            self._skills_parents(),
            key=lambda p: len(p.relative_to(self.root).parts),
            reverse=True,
        )
        for parent in parents:
            if not parent.is_dir():
                continue  # an ancestor was already renamed (defensive)
            for child in sorted(parent.iterdir()):
                if not child.is_dir():
                    continue
                if child.name in EXCLUDED_DIR_NAMES:
                    continue
                if child.name.startswith(PREFIX):
                    # Idempotent: already namespaced — unless it is a stale
                    # grm-era ghost of a #308-renamed role skill (grm-scout/),
                    # which heals to today's canonical name the same way a
                    # bare dir does.
                    if child.name[len(PREFIX):] not in CANONICAL_RENAMES:
                        continue
                elif child.name not in self.names:
                    continue
                dst = parent / (PREFIX + canonical_base(child.name[len(PREFIX):]
                                                        if child.name.startswith(PREFIX)
                                                        else child.name))
                src_rel = str(child.relative_to(self.root))
                dst_rel = str(dst.relative_to(self.root))
                if dst.exists():
                    # Post-sync collision: the grm-<name>/ skill is already
                    # installed and authoritative. Archive the stale bare-named
                    # duplicate and remove it — a blind `git mv` here would nest
                    # it as grm-<name>/<name>/ (exit 0, silently wrong).
                    self._archive_dir(child)
                    self._git_rm(child)
                    self.report.dirs_removed.append((src_rel, dst_rel))
                else:
                    self._git_mv(child, dst)
                    self.report.dirs_renamed.append((src_rel, dst_rel))

    # -- frontmatter -------------------------------------------------------

    _FM_NAME_RE = re.compile(r"^(name:\s*)([A-Za-z0-9_-]+)\s*$", re.MULTILINE)

    def update_frontmatter(self) -> None:
        """In each renamed SKILL.md set frontmatter name: to grm-<dir>."""
        for parent in self._skills_parents():
            for child in sorted(parent.iterdir()):
                if not child.is_dir() or not child.name.startswith(PREFIX):
                    continue
                skill_md = child / "SKILL.md"
                if not skill_md.exists():
                    continue
                expected = child.name  # already grm-prefixed dir name
                text = skill_md.read_text(encoding="utf-8")
                # Only touch the name: line inside the leading frontmatter block.
                if not text.startswith("---"):
                    continue
                end = text.find("\n---", 3)
                if end == -1:
                    continue
                head, body = text[: end + 4], text[end + 4 :]

                def _sub(m: re.Match) -> str:
                    return f"{m.group(1)}{expected}"

                new_head, n = self._FM_NAME_RE.subn(_sub, head, count=1)
                if n and new_head != head:
                    rel = str(skill_md.relative_to(self.root))
                    if self.apply:
                        skill_md.write_text(new_head + body, encoding="utf-8")
                    self.report.frontmatter_updated.append(rel)

    # -- reference rewriting ----------------------------------------------

    def _build_patterns(self) -> tuple[re.Pattern, re.Pattern, re.Pattern, re.Pattern]:
        # Sort longest-first so e.g. `grm-release-phase-merge` matches before
        # `grm-release-phase`.
        ordered = sorted(self.names, key=len, reverse=True)
        alt = "|".join(re.escape(n) for n in ordered)
        # Tier 3b: lookalike names are excluded from the aggressive Tier-2a
        # bare-backtick alternation — only unambiguous "<name> skill" /
        # "skill <name>" phrasing (Tier 2b, which still uses the full `alt`)
        # may rewrite them. An empty alternation must never match anything.
        safe = [n for n in ordered if n not in LOOKALIKE_NAMES]
        alt_safe = "|".join(re.escape(n) for n in safe) if safe else r"(?!x)x"
        # Tier 1: skills/<name>/  -> skills/grm-<name>/   (only when not already grm-)
        path_re = re.compile(rf"(skills/)(?!grm-)({alt})(/)")
        # Tier 2a: backticked exact token `<name>` (not already grm-, not a lookalike)
        backtick_re = re.compile(rf"`(?!grm-)({alt_safe})`")
        # Tier 2b: "<name> skill" / "the <name> skill" / "skill <name>"
        name_skill_re = re.compile(rf"(?<![\w-])(?!grm-)({alt})(\s+skill\b)")
        skill_name_re = re.compile(rf"(\bskill\s+)(?!grm-)({alt})(?![\w-])")
        return path_re, backtick_re, name_skill_re, skill_name_re

    def _iter_text_files(self) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            current = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDED_DIR_NAMES
                and not self._is_excluded_root(current / d)
            ]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix in TEXT_SUFFIXES or fn in TEXT_NAMES:
                    # Tier 3a: skip known meta-files about the migration
                    # itself — their OLD-name tokens are never live references.
                    rel_posix = p.relative_to(self.root).as_posix()
                    if is_denylisted_file(rel_posix):
                        continue
                    yield p

    def rewrite_references(self) -> None:
        path_re, backtick_re, name_skill_re, skill_name_re = self._build_patterns()
        for p in self._iter_text_files():
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            new = text
            count = 0
            # Every substitution lands on TODAY'S canonical base (#308 role
            # renames), not just the prefixed original.
            new, n = path_re.subn(lambda m: f"{m.group(1)}{PREFIX}{canonical_base(m.group(2))}{m.group(3)}", new)
            count += n
            new, n = backtick_re.subn(lambda m: f"`{PREFIX}{canonical_base(m.group(1))}`", new)
            count += n
            new, n = name_skill_re.subn(lambda m: f"{PREFIX}{canonical_base(m.group(1))}{m.group(2)}", new)
            count += n
            new, n = skill_name_re.subn(lambda m: f"{m.group(1)}{PREFIX}{canonical_base(m.group(2))}", new)
            count += n
            if count and new != text:
                rel = str(p.relative_to(self.root))
                self.report.files_rewritten[rel] = count
                if self.apply:
                    p.write_text(new, encoding="utf-8")

    # -- orchestration -----------------------------------------------------

    def run(self) -> TransformReport:
        self.discover_skill_names()
        # Order matters: rename dirs first so frontmatter/paths resolve against
        # the new layout, then update frontmatter, then rewrite references
        # (references are rewritten by name substring so order vs. rename is
        # immaterial, but doing it last keeps the report coherent).
        self.rename_dirs()
        self.update_frontmatter()
        self.rewrite_references()
        return self.report


# -- self-test -------------------------------------------------------------


def _test_tier3_denylist_and_lookalike() -> list[str]:
    """Regression test for #466: the denylist protects known meta-files from
    ANY rewrite, and the lookalike guard suppresses the bare-backtick rule
    (Tier 2a) for a name that also has a legitimate non-skill sense, while
    still allowing the unambiguous "<name> skill" phrasing (Tier 2b) through."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # A known skill whose base name is a LOOKALIKE (also a just-recipe
        # target / config-dial key in real usage): sync-deps.
        (root / ".claude" / "skills" / "sync-deps").mkdir(parents=True)
        (root / ".claude" / "skills" / "sync-deps" / "SKILL.md").write_text(
            "---\nname: sync-deps\n---\n", encoding="utf-8"
        )
        # A non-lookalike known skill for contrast.
        (root / ".claude" / "skills" / "doc-assurance").mkdir(parents=True)
        (root / ".claude" / "skills" / "doc-assurance" / "SKILL.md").write_text(
            "---\nname: doc-assurance\n---\n", encoding="utf-8"
        )

        # Tier 3a fixture: a file whose path ends in a denylisted suffix,
        # containing prose that would otherwise trip Tier 1 AND Tier 2 —
        # simulating the self-test / feature-manifest / reference.md content
        # the real denylisted files carry.
        deny_dir = root / ".claude" / "skills" / "grm-sync-from-upstream"
        deny_dir.mkdir(parents=True)
        deny_file = deny_dir / "feature-manifest.md"
        deny_file.write_text(
            "| `sync-deps` | stable id column, must stay bare |\n"
            "| `doc-assurance` | another stable id, e.g. `doc-assurance` -> `grm-doc-assurance` |\n"
            "See skills/doc-assurance/SKILL.md for the pre-migration form.\n",
            encoding="utf-8",
        )
        deny_original = deny_file.read_text()

        # Tier 3b fixture: a normal (non-denylisted) referencing file mixing
        # a bare lookalike backtick (must NOT rewrite), a lookalike used with
        # explicit "skill" phrasing (MUST rewrite), and a non-lookalike bare
        # backtick (MUST still rewrite — Tier 2a unaffected for safe names).
        ref = root / "docs" / "recipes.md"
        ref.parent.mkdir(parents=True)
        ref.write_text(
            "The `sync-deps` recipe delegates to the sync-deps skill.\n"
            "Also see the `doc-assurance` skill.\n",
            encoding="utf-8",
        )

        GrmNamespacer(root, apply=True).run()

        # 1. Denylisted file untouched byte-for-byte.
        if deny_file.read_text() != deny_original:
            failures.append("TIER3-DENYLIST: denylisted feature-manifest.md was rewritten")

        # 2. Bare lookalike backtick NOT rewritten (recipe sense preserved).
        out = ref.read_text()
        if "The `sync-deps` recipe" not in out:
            failures.append("TIER3-LOOKALIKE: bare `sync-deps` backtick was rewritten (false positive)")

        # 3. Lookalike WITH explicit unbackticked "<name> skill" phrasing
        #    (Tier 2b, unambiguous) still rewritten.
        if "grm-sync-deps skill" not in out:
            failures.append("TIER3-LOOKALIKE: `sync-deps` skill phrasing was NOT rewritten")

        # 4. Non-lookalike bare backtick still rewritten as before (Tier 2a
        #    unaffected for safe names).
        if "the `grm-doc-assurance` skill" not in out:
            failures.append("TIER3-LOOKALIKE: non-lookalike `doc-assurance` backtick regressed")

    return failures


def _test_submodule_boundary() -> list[str]:
    """Regression test for issue #178: submodule trees and lib/third-party/ are
    never descended into and no renames are planned inside them."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # Normal skill in the project's own .claude/skills/ — MUST be renamed.
        (root / ".claude" / "skills" / "scout").mkdir(parents=True)
        (root / ".claude" / "skills" / "scout" / "SKILL.md").write_text(
            "---\nname: scout\n---\n", encoding="utf-8"
        )

        # Simulate a git submodule: a nested directory that contains a .git file
        # (the gitfile form used by `git submodule add`).
        sub_skills = root / "vendor-sub" / ".claude" / "skills"
        sub_skills.mkdir(parents=True)
        (sub_skills / "scout").mkdir()
        (sub_skills / "scout" / "SKILL.md").write_text(
            "---\nname: scout\n---\nSUBMODULE-CONTENT\n", encoding="utf-8"
        )
        # The .git gitfile that marks vendor-sub/ as a submodule.
        (root / "vendor-sub" / ".git").write_text(
            "gitdir: ../.git/modules/vendor-sub\n", encoding="utf-8"
        )
        # .gitmodules registering it (belt-and-suspenders: both detection paths
        # should independently exclude the submodule tree).
        (root / ".gitmodules").write_text(
            "[submodule \"vendor-sub\"]\n    path = vendor-sub\n    url = https://example.com/sub.git\n",
            encoding="utf-8",
        )

        # Simulate lib/third-party/ vendored tree — MUST NOT be renamed.
        vendored_skills = root / "lib" / "third-party" / "some-lib" / ".claude" / "skills"
        vendored_skills.mkdir(parents=True)
        (vendored_skills / "scout").mkdir()
        (vendored_skills / "scout" / "SKILL.md").write_text(
            "---\nname: scout\n---\nVENDORED-CONTENT\n", encoding="utf-8"
        )

        ns = GrmNamespacer(root, apply=True)
        report = ns.run()

        # 1. The normal project skill was renamed.
        if not (root / ".claude" / "skills" / "grm-agent-scout").is_dir():
            failures.append("SUBMODULE: project-level scout/ was NOT renamed to grm-agent-scout/ (expected rename)")
        if (root / ".claude" / "skills" / "scout").exists():
            failures.append("SUBMODULE: project-level scout/ still present after rename")

        # 2. The submodule's skills tree was NOT touched.
        if not (root / "vendor-sub" / ".claude" / "skills" / "scout").exists():
            failures.append("SUBMODULE: scout/ inside the submodule was renamed (must NOT be touched)")
        if (root / "vendor-sub" / ".claude" / "skills" / "grm-agent-scout").exists():
            failures.append("SUBMODULE: grm-agent-scout/ was created inside the submodule tree")
        sub_content = (root / "vendor-sub" / ".claude" / "skills" / "scout" / "SKILL.md").read_text()
        if "SUBMODULE-CONTENT" not in sub_content:
            failures.append("SUBMODULE: submodule SKILL.md content was modified")

        # 3. The lib/third-party/ tree was NOT touched.
        if not (vendored_skills / "scout").exists():
            failures.append("VENDORED: scout/ inside lib/third-party/ was renamed (must NOT be touched)")
        if (vendored_skills / "grm-agent-scout").exists():
            failures.append("VENDORED: grm-agent-scout/ was created inside lib/third-party/")
        vend_content = (vendored_skills / "scout" / "SKILL.md").read_text()
        if "VENDORED-CONTENT" not in vend_content:
            failures.append("VENDORED: lib/third-party/ SKILL.md content was modified")

        # 4. The submodule and vendored dirs are not in dirs_renamed.
        renamed_paths = [src for src, _ in report.dirs_renamed]
        for rp in renamed_paths:
            if "vendor-sub" in rp:
                failures.append(f"SUBMODULE: submodule path appeared in dirs_renamed: {rp}")
            if "third-party" in rp:
                failures.append(f"VENDORED: vendored path appeared in dirs_renamed: {rp}")

    return failures


def _self_test() -> int:
    """Seed a fake skill + referencing file in a tempdir, run, assert the
    contract: dir renamed, path rewritten, common-word false-positive avoided.
    Also runs the submodule-boundary regression test (#178)."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Fake skill `grm-agent-scout` (a common-word name) + a normal skill `grm-doc-assurance`.
        (root / ".claude" / "skills" / "scout").mkdir(parents=True)
        (root / ".claude" / "skills" / "doc-assurance").mkdir(parents=True)
        (root / ".claude" / "skills" / "scout" / "SKILL.md").write_text(
            "---\nname: scout\ndescription: x\n---\n# scout\n", encoding="utf-8"
        )
        (root / ".claude" / "skills" / "doc-assurance" / "SKILL.md").write_text(
            "---\nname: doc-assurance\n---\n", encoding="utf-8"
        )
        # Post-sync COLLISION: a sync already added grm-iterate/ (authoritative,
        # NEW content) while the stale bare-named iterate/ (OLD content) remains.
        (root / ".claude" / "skills" / "iterate").mkdir(parents=True)
        (root / ".claude" / "skills" / "iterate" / "SKILL.md").write_text(
            "---\nname: iterate\n---\nOLD-stale-content\n", encoding="utf-8"
        )
        (root / ".claude" / "skills" / "grm-iterate").mkdir(parents=True)
        (root / ".claude" / "skills" / "grm-iterate" / "SKILL.md").write_text(
            "---\nname: grm-iterate\n---\nNEW-synced-content\n", encoding="utf-8"
        )
        # A referencing doc: a real path, a backticked name, a prose pattern,
        # AND a common-word false-positive ("scout the area" — must NOT rewrite).
        ref = root / "docs" / "guide.md"
        ref.parent.mkdir(parents=True)
        ref.write_text(
            "Run `python3 .claude/skills/grm-agent-scout/scout.py`.\n"
            "Use the `grm-agent-scout` skill and the grm-doc-assurance skill.\n"
            "We scout the area before we iterate on the plan.\n"
            "See skills/grm-doc-assurance/SKILL.md too.\n",
            encoding="utf-8",
        )

        ns = GrmNamespacer(root, apply=True)
        report = ns.run()

        # 1. dir renamed
        if not (root / ".claude" / "skills" / "grm-agent-scout").is_dir():
            failures.append("grm-agent-scout dir not created")
        if (root / ".claude" / "skills" / "scout").exists():
            failures.append("old scout dir still present")

        # 2. frontmatter updated
        fm = (root / ".claude" / "skills" / "grm-agent-scout" / "SKILL.md").read_text()
        if "name: grm-agent-scout" not in fm:
            failures.append("frontmatter name not updated")

        # 3. path rewrite (Tier 1)
        out = ref.read_text()
        if "skills/grm-agent-scout/scout.py" not in out:
            failures.append("Tier-1 path rewrite failed (.claude/skills/grm-agent-scout/)")
        if "skills/grm-doc-assurance/SKILL.md" not in out:
            failures.append("Tier-1 relative path rewrite failed")

        # 4. backticked exact token (Tier 2a)
        if "`grm-agent-scout` skill" not in out:
            failures.append("Tier-2a backtick rewrite failed")

        # 5. prose pattern (Tier 2b) — "the grm-doc-assurance skill"
        if "grm-doc-assurance skill" not in out:
            failures.append("Tier-2b prose rewrite failed")

        # 6. CONSERVATIVE: un-backticked common word NOT rewritten
        if "We scout the area" not in out:
            failures.append("FALSE POSITIVE: bare 'scout' verb was mangled")
        if "iterate on the plan" not in out:
            failures.append("FALSE POSITIVE: bare 'iterate' verb was mangled")

        # 7. POST-SYNC COLLISION: stale iterate/ removed (not nested), synced
        #    grm-iterate/ content preserved, original archived, reported.
        skills = root / ".claude" / "skills"
        if (skills / "iterate").exists():
            failures.append("COLLISION: stale bare-named iterate/ not removed")
        if (skills / "grm-iterate" / "iterate").exists():
            failures.append("COLLISION: nested grm-iterate/iterate/ created (the bug)")
        gi = (skills / "grm-iterate" / "SKILL.md").read_text()
        if "NEW-synced-content" not in gi:
            failures.append("COLLISION: synced grm-iterate/ content was clobbered")
        archived = list(root.glob(".grimoire-archive/*/.claude/skills/iterate/SKILL.md"))
        if not archived:
            failures.append("COLLISION: stale iterate/ was not archived")
        elif "OLD-stale-content" not in archived[0].read_text():
            failures.append("COLLISION: archive does not hold the original content")
        if not any(src.endswith("skills/iterate") for src, _ in report.dirs_removed):
            failures.append("COLLISION: dirs_removed did not record the stale dir")

        # 8. idempotency: a second run is a no-op
        ns2 = GrmNamespacer(root, apply=True)
        rep2 = ns2.run()
        if (
            rep2.dirs_renamed
            or rep2.dirs_removed
            or rep2.frontmatter_updated
            or rep2.files_rewritten
        ):
            failures.append(f"NOT IDEMPOTENT: second run changed things: {rep2.summary()}")

    # 9. Submodule-boundary regression (#178): renames must not cross into
    #    git submodule trees or lib/third-party/ vendored deps.
    failures.extend(_test_submodule_boundary())

    # 9.5. Tier 3 denylist / lookalike-guard regression (#466): known
    #      meta-files are skipped entirely; a lookalike name's bare backtick
    #      is not rewritten, but its "<name> skill" phrasing still is.
    failures.extend(_test_tier3_denylist_and_lookalike())

    # 10. Canonical role renames (#308): a stale grm-era ghost (grm-scout/)
    #     heals to grm-agent-scout/ — archived+removed when the canonical twin
    #     exists, renamed onto the canonical name when alone.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".claude" / "skills" / "grm-scout").mkdir(parents=True)
        (root / ".claude" / "skills" / "grm-scout" / "SKILL.md").write_text(
            "---\nname: grm-scout\n---\nGHOST\n", encoding="utf-8")
        (root / ".claude" / "skills" / "grm-agent-scout").mkdir(parents=True)
        (root / ".claude" / "skills" / "grm-agent-scout" / "SKILL.md").write_text(
            "---\nname: grm-agent-scout\n---\nREAL\n", encoding="utf-8")
        GrmNamespacer(root, apply=True).run()
        if (root / ".claude" / "skills" / "grm-scout").exists():
            failures.append("GHOST: stale grm-scout/ survived beside grm-agent-scout/")
        real = (root / ".claude" / "skills" / "grm-agent-scout" / "SKILL.md").read_text()
        if "REAL" not in real:
            failures.append("GHOST: canonical grm-agent-scout/ content was clobbered")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".claude" / "skills" / "grm-scout").mkdir(parents=True)
        (root / ".claude" / "skills" / "grm-scout" / "SKILL.md").write_text(
            "---\nname: grm-scout\n---\nLONE\n", encoding="utf-8")
        GrmNamespacer(root, apply=True).run()
        if not (root / ".claude" / "skills" / "grm-agent-scout").is_dir():
            failures.append("GHOST: lone grm-scout/ was not renamed to grm-agent-scout/")
        if (root / ".claude" / "skills" / "grm-scout").exists():
            failures.append("GHOST: lone grm-scout/ still present after canonical rename")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELF-TEST PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grm- skill namespacing transformer")
    ap.add_argument("--root", default=".", help="repository root to transform")
    ap.add_argument("--apply", action="store_true", help="mutate the tree (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="report only (default)")
    ap.add_argument("--self-test", action="store_true", help="run the built-in fixture test")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    apply = args.apply and not args.dry_run
    ns = GrmNamespacer(Path(args.root), apply=apply)
    report = ns.run()
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] known skills: {len(ns.names)}")
    print(f"[{mode}] {report.summary()}")
    for src, dst in report.dirs_renamed:
        print(f"  rename: {src} -> {dst}")
    for src, dst in report.dirs_removed:
        print(f"  remove-stale: {src} (kept {dst}; archived)")
    for rel in sorted(report.frontmatter_updated):
        print(f"  frontmatter: {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
