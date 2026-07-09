#!/usr/bin/env python3
"""Surgical regenerate of the Grimoire framework layer (CR-5).

Restores the **framework layer only, in place**, preserving project files, with
an idempotency guarantee and archive-then-restore safety. The surgical middle
path between `install-doctor --repair` (per-file, no whole-layer guarantee) and
`grm-hard-reset` (archives *everything*, re-onboards from zero).

Contract: docs/grimoire/design/clean-room-design.md §2 (mixed-file split/merge)
and §3 (surgical-regenerate). Partition source: .claude/grimoire-files.json
(CR-4 manifest). Restore source: .claude/skills/grm-workflow-bootstrap/golden/.

Set partition (from the manifest §1 taxonomy):
  - pure-framework  -> delete + restore from golden (no project content; loss-free)
  - mixed           -> split/merge in place (never blind-replaced); see §2
  - project-owned   -> preserve untouched (never read-for-write, deleted, moved)

Safety (§3): archive-then-restore. Before any mutation every delete-set AND
merge-set file is copied to .grimoire-archive/<ts>/ with a MANIFEST.md (class +
original path + reason="regenerate"), mirroring hard-reset's discipline. The
preserve-set is never archived. On failure the archived originals are restored.

Usage:
    python3 regenerate_grimoire.py [--root ROOT] [--check] [--yes]
    python3 regenerate_grimoire.py --self-test

Options:
    --root ROOT   Repo root to regenerate (default: auto-detect from the script
                  location by walking up to .claude/grimoire-files.json).
    --check       Dry-run: report the delete/merge/preserve partition and what
                  would change, write nothing. (Alias: --dry-run.)
    --yes         Proceed without the interactive confirmation (for automation /
                  install-doctor delegation). Without it, a live run prompts.
    --self-test   Run offline self-tests against tempdir fixtures (round-trip +
                  idempotency + every mixed merger) and exit. Never touches the
                  repo root.

Stdlib-only. Copilot has no golden/ baseline, so regenerate cannot run there —
see SKILL.md; this script refuses a flavor whose golden tree is absent.
"""

import argparse
import datetime
import fnmatch
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

MANIFEST_FILENAME = "grimoire-files.json"
# The golden baseline is a generated artifact resolved at runtime (no longer a
# committed tree). It materializes under the gitignored cache dir below; the
# generator helper lives with grm-workflow-bootstrap.
GENERATE_GOLDEN_REL = ".claude/skills/grm-workflow-bootstrap/generate_golden.py"
GOLDEN_CACHE_REL = ".grimoire-golden/tree"
GOLDEN_CACHE_PREFIX = ".grimoire-golden/"
ARCHIVE_DIR = ".grimoire-archive"
ARCHIVE_REASON = "regenerate"


def _resolve_golden_root(root: Path) -> Path | None:
    """Materialize and return the golden tree for `root`, or None if unavailable.

    Prefers grm-workflow-bootstrap's generate_golden.resolve_golden (extract frozen
    archive / generate from a flavor); falls back to a pre-materialized tree under
    the cache dir (used by the offline self-test fixture).
    """
    import importlib.util
    gen_path = root / GENERATE_GOLDEN_REL
    if gen_path.exists():
        spec = importlib.util.spec_from_file_location("generate_golden", gen_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        try:
            return mod.resolve_golden(root)
        except FileNotFoundError:
            return None
    cached = root / GOLDEN_CACHE_REL
    return cached if cached.is_dir() else None

# ---------------------------------------------------------------------------
# Golden-staleness predicate
#
# Copied from grm-install-doctor/install_doctor.py (GoldenStaleness). Both
# tools must agree on "live file is newer than golden" so a post-sync file is
# never rolled back by regenerate. The long-term home is generate_golden.py;
# this copy is a consolidation candidate (#159 follow-up).
# ---------------------------------------------------------------------------


class GoldenStaleness:
    """Decides whether a live file is *newer than* the resolved golden baseline.

    When `grm-sync-from-upstream --apply` advances a framework file past the
    last golden freeze, that file differs from golden yet is correct — restoring
    it from golden would revert the sync (#159). This predicate lets restore_from_golden
    skip such files (classifying them as ahead-of-golden rather than drifted).

    The comparison is by modification time against the golden archive's freeze
    instant. When no archive timestamp is resolvable the predicate is
    conservative and returns False (treat as a genuine difference) so it never
    *hides* real drift that should be restored.
    """

    def __init__(self, golden_mtime: float | None):
        self._golden_mtime = golden_mtime

    @classmethod
    def for_root(cls, root: Path) -> "GoldenStaleness":
        """Build the predicate for `root`, resolving the golden freeze time.

        Prefers the frozen archive's mtime (the authoritative freeze instant);
        falls back to the extracted-tree mtime; None if neither exists.
        """
        import importlib.util
        gen_path = root / GENERATE_GOLDEN_REL
        cache = root / ".grimoire-golden"
        archive_glob = "golden-v*.tar.gz"
        tree_subdir = "tree"
        # Try to read the attribute from the generate_golden module if available.
        if gen_path.exists():
            spec = importlib.util.spec_from_file_location("generate_golden", gen_path)
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
                archive_glob = getattr(mod, "GOLDEN_ARCHIVE_GLOB", archive_glob)
                tree_subdir = getattr(mod, "GOLDEN_TREE_SUBDIR", tree_subdir)
                cache_attr = getattr(mod, "GOLDEN_CACHE_DIR", None)
                if cache_attr:
                    cache = root / cache_attr
            except Exception:
                pass
        archives = sorted(cache.glob(archive_glob)) if cache.is_dir() else []
        if archives:
            return cls(archives[-1].stat().st_mtime)
        tree = cache / tree_subdir
        if tree.is_dir():
            return cls(tree.stat().st_mtime)
        return cls(None)

    @property
    def resolvable(self) -> bool:
        return self._golden_mtime is not None

    def is_newer(self, live: Path) -> bool:
        """True iff `live` was modified after the golden baseline was frozen."""
        if self._golden_mtime is None or not live.exists():
            return False
        try:
            return live.stat().st_mtime > self._golden_mtime
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Golden <-> repo path mapping
#
# The golden/ tree uses a flattened layout: framework dirs (hooks/, skills/,
# paradigms/, mcp-servers/, workflows/, stealth/) sit directly under golden/ and
# map into .claude/; a handful of top-level files are remapped; docs/ and
# CLAUDE.md map straight through. This class is the single authority for the
# mapping in both directions.
# ---------------------------------------------------------------------------


class GoldenLayout:
    """Maps between golden/ tree paths and repo-relative destination paths.

    golden/hooks/x.sh        <-> .claude/hooks/x.sh
    golden/skills/<n>/...     <-> .claude/skills/<n>/...
    golden/mcp.json          <-> .mcp.json
    golden/CLAUDE.md         <-> CLAUDE.md
    golden/docs/...          <-> docs/...
    golden/settings.json     <-> .claude/settings.json
    """

    # golden top-level dir name -> repo-relative prefix it expands into
    DIR_MAP = {
        "hooks": ".claude/hooks",
        "skills": ".claude/skills",
        "paradigms": ".claude/paradigms",
        "mcp-servers": ".claude/mcp-servers",
        "workflows": ".claude/workflows",
        "stealth": ".claude/stealth",
        "quick-start-templates": ".claude/quick-start-templates",
        "docs": "docs",
    }
    # golden top-level file name -> repo-relative destination path
    FILE_MAP = {
        "mcp.json": ".mcp.json",
        "CLAUDE.md": "CLAUDE.md",
        "settings.json": ".claude/settings.json",
        "push-allowlist": ".claude/push-allowlist",
        "model-effort-profiles.json": ".claude/model-effort-profiles.json",
        "vendor.toml": "vendor.toml",
        "grimoire-files.json": ".claude/grimoire-files.json",
        "grimoire-config.json": ".claude/grimoire-config.json",
        "architecture-rules.example.json": ".claude/architecture-rules.example.json",
        ".scaffold-upstream.conf": ".scaffold-upstream.conf",
        ".gitattributes": ".gitattributes",
        ".gitignore": ".gitignore",
        ".grimoire-flavor": ".grimoire-flavor",
    }

    def __init__(self, golden_root: Path):
        self.golden_root = golden_root

    def golden_to_repo(self, golden_rel: str) -> str:
        """Map a golden-relative path to its repo-relative destination."""
        parts = golden_rel.split("/", 1)
        head = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if head in self.DIR_MAP:
            prefix = self.DIR_MAP[head]
            return f"{prefix}/{rest}" if rest else prefix
        if golden_rel in self.FILE_MAP:
            return self.FILE_MAP[golden_rel]
        # Unmapped top-level golden file: pass through at repo root.
        return golden_rel

    def repo_to_golden(self, repo_rel: str) -> str | None:
        """Map a repo-relative path back to its golden source, or None if not in golden."""
        # File map (exact) first.
        for g, r in self.FILE_MAP.items():
            if r == repo_rel:
                return g
        # Dir map by longest matching prefix.
        best = None
        for g, r in self.DIR_MAP.items():
            if repo_rel == r:
                cand = g
            elif repo_rel.startswith(r + "/"):
                cand = g + "/" + repo_rel[len(r) + 1:]
            else:
                continue
            if best is None or len(cand) > len(best):
                best = cand
        return best

    def golden_files(self) -> list[str]:
        """All files under golden/, as golden-relative POSIX paths."""
        out = []
        for p in self.golden_root.rglob("*"):
            if p.is_file() and ".DS_Store" not in p.name:
                out.append(p.relative_to(self.golden_root).as_posix())
        return sorted(out)


# ---------------------------------------------------------------------------
# Manifest partition
# ---------------------------------------------------------------------------


def load_manifest(root: Path) -> dict:
    """Load .claude/grimoire-files.json."""
    mp = root / ".claude" / MANIFEST_FILENAME
    if not mp.exists():
        raise FileNotFoundError(f"Manifest not found: {mp}")
    with mp.open() as f:
        data = json.load(f)
    if data.get("schema_version") != 1:
        raise ValueError(f"Unsupported schema_version: {data.get('schema_version')}")
    return data


def classify_entries(entries: list[dict]) -> dict[str, list[dict]]:
    """Group manifest entries by class."""
    out = {"pure-framework": [], "mixed": [], "project-owned": []}
    for e in entries:
        out.setdefault(e.get("class", "unknown"), []).append(e)
    return out


def _golden_is_restore_source(path: str) -> bool:
    """True for the golden cache itself — the restore SOURCE, never deleted."""
    return path.startswith(GOLDEN_CACHE_PREFIX)


def resolve_delete_set(
    pure_entries: list[dict], layout: GoldenLayout
) -> list[str]:
    """Repo-relative files to delete+restore: pure-framework files that have a
    golden source. Glob entries are expanded against the golden tree (the
    restore source defines exactly what gets restored), so we never delete a
    file we cannot restore. The golden subtree itself is excluded."""
    golden_repo_paths = {layout.golden_to_repo(g) for g in layout.golden_files()}
    result: set[str] = set()
    for e in pure_entries:
        path = e["path"]
        if _golden_is_restore_source(path):
            continue  # golden/ is the source, never deleted
        if "*" in path:
            # Expand glob against golden destinations.
            for rp in golden_repo_paths:
                if _glob_matches(path, rp):
                    result.add(rp)
        else:
            if path in golden_repo_paths:
                result.add(path)
            # else: pure-framework file with no golden source (e.g. root-only
            # docs not captured in golden) — not restorable, so leave in place.
    return sorted(result)


def _glob_matches(pattern: str, rel: str) -> bool:
    """fnmatch-style match with '**' meaning any path-component sequence."""
    if "**" not in pattern:
        return fnmatch.fnmatch(rel, pattern) or rel == pattern
    escaped = re.escape(pattern).replace(r"\*\*", "__DSTAR__").replace(r"\*", "[^/]*")
    regex = escaped.replace("__DSTAR__", ".*")
    return bool(re.fullmatch(regex, rel))


# ---------------------------------------------------------------------------
# Archive (mirrors hard-reset discipline)
# ---------------------------------------------------------------------------


def archive_timestamp(now: datetime.datetime | None = None) -> str:
    """UTC YYYYMMDD-HHMMSS, matching hard-reset's layout."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y%m%d-%H%M%S")


def archive_files(
    root: Path, files: list[tuple[str, str]], ts: str
) -> Path:
    """Copy each (repo_rel, class) that currently exists into
    .grimoire-archive/<ts>/, preserving its repo-relative path, and write a
    MANIFEST.md. Returns the archive dir. Files absent on disk are recorded as
    'absent' in the manifest (a missing pure-framework file is the common
    repair case) and not copied."""
    archive_root = root / ARCHIVE_DIR / ts
    archive_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for rel, cls in files:
        src = root / rel
        if src.exists() and src.is_file():
            dst = archive_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            rows.append((rel, cls, "archived"))
        else:
            rows.append((rel, cls, "absent"))
    _write_archive_manifest(archive_root, ts, rows)
    return archive_root


def _write_archive_manifest(archive_root: Path, ts: str, rows: list[tuple[str, str, str]]):
    """Write MANIFEST.md recording class + original path + reason per file."""
    lines = [
        f"# Grimoire regenerate archive — {ts}",
        "",
        f"Reason: **{ARCHIVE_REASON}** (surgical framework-layer regenerate, CR-5).",
        "",
        "Every delete-set and merge-set file was copied here *before* any",
        "mutation, so the regenerate is fully recoverable. The preserve-set",
        "(project-owned files) was never archived.",
        "",
        "| Original path | Class | Status |",
        "|---|---|---|",
    ]
    for rel, cls, status in sorted(rows):
        lines.append(f"| `{rel}` | {cls} | {status} |")
    lines.append("")
    (archive_root / "MANIFEST.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Restore (delete + restore pure-framework from golden)
# ---------------------------------------------------------------------------


def restore_from_golden(
    root: Path,
    layout: GoldenLayout,
    delete_set: list[str],
    staleness: "GoldenStaleness | None" = None,
) -> list[str]:
    """Delete each delete-set file then copy its golden source over it.

    If `staleness` is provided, any live file that is newer than the golden
    baseline (i.e. was advanced by grm-sync-from-upstream) is skipped rather
    than overwritten, so a post-sync update is never rolled back (#159).

    Returns a list of repo-relative paths that were skipped (newer than golden).
    """
    skipped: list[str] = []
    for rel in delete_set:
        dest = root / rel
        if staleness is not None and staleness.is_newer(dest):
            skipped.append(rel)
            continue
        if dest.exists():
            dest.unlink()
    for rel in delete_set:
        if rel in skipped:
            continue
        golden_rel = layout.repo_to_golden(rel)
        if golden_rel is None:
            continue
        src = layout.golden_root / golden_rel
        if not src.exists():
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return skipped


# ---------------------------------------------------------------------------
# Mixed-file mergers (clean-room-design.md §2)
#
# Each merger is pure: (current_text, golden_text) -> merged_text, and
# idempotent — merge(merge(x)) == merge(x). They never delete a file and never
# blind-replace project content.
# ---------------------------------------------------------------------------


def merge_settings_json(current_text: str | None, golden_text: str) -> str:
    """3-way settings.json merge: project keys preserved verbatim; framework
    `permissions.allow` allowlist and `hooks` block reset to golden. base=golden,
    theirs=current, ours=golden-framework-block. Never widen beyond framework
    scope; never drop a project key."""
    golden = json.loads(golden_text)
    current = json.loads(current_text) if current_text else {}

    result = dict(current)  # start from project file: keeps env, custom keys

    # permissions: framework owns its golden allow entries; union with project's.
    g_perms = golden.get("permissions", {})
    c_perms = current.get("permissions", {})
    merged_perms = dict(c_perms)
    g_allow = g_perms.get("allow", [])
    c_allow = c_perms.get("allow", [])
    # framework allow entries reset to golden + project's own allow entries kept.
    merged_allow = list(g_allow) + [a for a in c_allow if a not in g_allow]
    if merged_allow:
        merged_perms["allow"] = merged_allow
    # deny: project-owned, preserved as-is (golden ships none).
    if "deny" in g_perms and "deny" not in merged_perms:
        merged_perms["deny"] = g_perms["deny"]
    if merged_perms:
        result["permissions"] = merged_perms

    # hooks: framework block reset to golden verbatim (the managed surface).
    if "hooks" in golden:
        result["hooks"] = golden["hooks"]

    return json.dumps(result, indent=4) + "\n"


SENTINEL = "<!-- GRIMOIRE_ONBOARDING_SENTINEL -->"
# Matches both lowercase tokens like {build-command} and UPPERCASE tokens like
# {ACTIVE}. The original lowercase-only pattern silently skipped {ACTIVE},
# causing the paradigm stamp to be left unreplaced after merge (#160).
PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z][a-zA-Z0-9-]*\}")


def merge_agent_guidance(current_text: str | None, golden_text: str) -> str:
    """CLAUDE.md / AGENTS.md merge (section + sentinel aware).

    Restore framework content from golden BUT:
      (a) preserve filled-in project placeholders — if the current file resolved
          a `{placeholder}` to a real value, re-inject that value rather than
          resetting to the golden `{...}` token;
      (b) sentinel — if the *current* line 1 is the onboarding sentinel, keep it
          (pre-onboarding); if the current file is clean, do NOT re-arm it even
          though golden carries it (regenerate is not a factory reset).
    """
    if current_text is None:
        # No project file: golden as-is (sentinel armed — fresh scaffold).
        return golden_text

    current_lines = current_text.splitlines()
    current_has_sentinel = bool(current_lines) and current_lines[0].strip() == SENTINEL

    golden_lines = golden_text.splitlines()
    # Strip golden's sentinel from line 1; we decide arming from current state.
    if golden_lines and golden_lines[0].strip() == SENTINEL:
        golden_lines = golden_lines[1:]
    body = "\n".join(golden_lines)

    # (a) Re-inject resolved placeholders. Map each golden placeholder token to
    # the value the current file resolved it to, matched positionally per token.
    resolved = _resolved_placeholder_values(current_text, golden_text)
    for token, value in resolved.items():
        body = body.replace(token, value)

    # (b) Sentinel arming follows the *current* file's state, not golden's.
    if current_has_sentinel:
        body = SENTINEL + "\n" + body

    if not body.endswith("\n"):
        body += "\n"
    return body


def _resolved_placeholder_values(current_text: str, golden_text: str) -> dict[str, str]:
    """For each distinct `{placeholder}` token in golden, find the value the
    current file used in its place. We locate the golden line carrying the token,
    find the structurally-matching current line (same prefix up to the token),
    and extract the substituted value. Only confidently-resolved (non-
    placeholder) values are returned, so a still-unfilled token is left as-is —
    keeping the merge idempotent."""
    out: dict[str, str] = {}
    golden_lines = golden_text.splitlines()
    current_lines = current_text.splitlines()
    for gline in golden_lines:
        for m in PLACEHOLDER_RE.finditer(gline):
            token = m.group(0)
            if token in out:
                continue
            prefix = gline[: m.start()]
            suffix = gline[m.end():]
            for cline in current_lines:
                if cline.startswith(prefix) and cline.endswith(suffix) and len(cline) >= len(prefix) + len(suffix):
                    value = cline[len(prefix): len(cline) - len(suffix)] if suffix else cline[len(prefix):]
                    if value and not PLACEHOLDER_RE.fullmatch(value) and value != token:
                        out[token] = value
                    break
    return out


GITIGNORE_BEGIN = "# >>> grimoire-managed >>>"
GITIGNORE_END = "# <<< grimoire-managed <<<"


def merge_gitignore(current_text: str | None, golden_text: str) -> str:
    """Section-merge: replace ONLY the Grimoire-managed delimited section,
    leaving project lines and ordering intact. Append the section if absent;
    idempotent on re-run (no duplicate). The managed section content is taken
    from the golden file's own managed block, or — if golden has no markers —
    derived by wrapping golden's lines in the markers."""
    managed = _extract_managed_section(golden_text)
    if managed is None:
        # Golden carries no markers: treat its whole body as the managed block.
        inner = golden_text.strip("\n")
        managed = f"{GITIGNORE_BEGIN}\n{inner}\n{GITIGNORE_END}"

    if current_text is None:
        return managed + "\n"

    if GITIGNORE_BEGIN in current_text and GITIGNORE_END in current_text:
        # Replace existing managed section in place.
        pre, _, rest = current_text.partition(GITIGNORE_BEGIN)
        _, _, post = rest.partition(GITIGNORE_END)
        result = pre + managed + post
    else:
        # Append managed section, preserving project lines.
        sep = "" if current_text.endswith("\n") or current_text == "" else "\n"
        result = current_text + sep + "\n" + managed + "\n"
    if not result.endswith("\n"):
        result += "\n"
    return result


def _extract_managed_section(text: str) -> str | None:
    """Return the full marker-delimited managed block (inclusive), or None."""
    if GITIGNORE_BEGIN not in text or GITIGNORE_END not in text:
        return None
    start = text.index(GITIGNORE_BEGIN)
    end = text.index(GITIGNORE_END) + len(GITIGNORE_END)
    return text[start:end]


def merge_roadmap(current_text: str | None, golden_text: str) -> str:
    """Baseline-row reconcile: restore any missing/garbled framework baseline
    row WITHOUT touching project rows — never deletes or reorders project
    content. A baseline row is identified by its leading cell key. If the
    project file already contains the row's key, it is left as the project's
    (project owns its rows); only genuinely-missing baseline rows are appended
    under the table they belong to. If the project file is absent, golden is
    used as the seed."""
    if current_text is None:
        return golden_text if golden_text.endswith("\n") else golden_text + "\n"

    baseline_rows = _table_rows(golden_text)
    current_keys = {_row_key(r) for r in _table_rows(current_text)}
    missing = [r for r in baseline_rows if _row_key(r) and _row_key(r) not in current_keys]
    if not missing:
        return current_text if current_text.endswith("\n") else current_text + "\n"

    # Append missing baseline rows at end of the file (under a managed note),
    # preserving all project content and order.
    sep = "" if current_text.endswith("\n") else "\n"
    addition = "\n".join(missing)
    result = f"{current_text}{sep}{addition}\n"
    return result


def _table_rows(text: str) -> list[str]:
    """All markdown table data rows (lines starting with '|' that are not the
    header separator)."""
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and set("".join(cells)) <= set("-: "):
                continue  # separator row
            rows.append(line)
    return rows


def _row_key(row: str) -> str:
    """First cell of a table row, used as its identity key."""
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    return cells[0] if cells else ""


def merge_version_history(
    current_text: str | None, golden_text: str, audience: str
) -> str:
    """Audience-branched. consumer: ensure the empty seed template exists, never
    overwrite existing entries. root: leave the log untouched (it IS Grimoire's
    framework release log)."""
    if audience == "root":
        # Root copy: the populated log is authoritative — never rewritten.
        if current_text is None:
            return golden_text
        return current_text
    # consumer
    if current_text is None or current_text.strip() == "":
        return golden_text
    return current_text  # never overwrite an existing consumer history


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


MIXED_HANDLERS = {
    ".claude/settings.json": "settings",
    "CLAUDE.md": "agent",
    "copilot/AGENTS.md": "agent",
    "AGENTS.md": "agent",
    ".gitignore": "gitignore",
    "docs/roadmap.md": "roadmap",
    "docs/version-history.md": "version-history",
}


def _read(path: Path) -> str | None:
    return path.read_text() if path.exists() and path.is_file() else None


def apply_mixed_merge(
    root: Path, layout: GoldenLayout, entry: dict, audience: str, write: bool
) -> tuple[str, bool]:
    """Apply the file-specific merge for one mixed entry. Returns (status,
    changed). status is a short label; changed is whether the merged text
    differs from current."""
    rel = entry["path"]
    handler = MIXED_HANDLERS.get(rel)
    if handler is None:
        # mixed file with no dedicated merger (e.g. config, vendor.toml seed):
        # seed-only — write golden only if missing.
        golden_rel = layout.repo_to_golden(rel)
        golden_path = layout.golden_root / golden_rel if golden_rel else None
        dest = root / rel
        if dest.exists():
            return ("preserved (no merger; present)", False)
        if golden_path and golden_path.exists():
            if write:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(golden_path, dest)
            return ("seeded (was missing)", True)
        return ("skipped (no golden source)", False)

    dest = root / rel
    current = _read(dest)
    golden_rel = layout.repo_to_golden(rel)
    golden_path = layout.golden_root / golden_rel if golden_rel else None
    golden = _read(golden_path) if golden_path else None
    if golden is None:
        return ("skipped (no golden source)", False)

    if handler == "settings":
        merged = merge_settings_json(current, golden)
    elif handler == "agent":
        merged = merge_agent_guidance(current, golden)
    elif handler == "gitignore":
        merged = merge_gitignore(current, golden)
    elif handler == "roadmap":
        merged = merge_roadmap(current, golden)
    elif handler == "version-history":
        merged = merge_version_history(current, golden, audience)
    else:
        return ("skipped (unknown handler)", False)

    changed = (current or "") != merged
    if changed and write:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(merged)
    return (("merged" if changed else "clean"), changed)


def detect_audience(root: Path, manifest: dict) -> str:
    """root (Grimoire's own repo) vs consumer, from the manifest flavor."""
    return "root" if manifest.get("flavor") == "root" else "consumer"


def regenerate(root: Path, check: bool, assume_yes: bool, now=None) -> int:
    """Drive the surgical regenerate. Returns process exit code."""
    golden_root = _resolve_golden_root(root)
    if golden_root is None or not golden_root.exists():
        print("ERROR: golden baseline unavailable.")
        print("Regenerate requires a generated golden baseline — run "
              "grm-workflow-bootstrap to freeze one, or provide a claude-code/ flavor.")
        print("The copilot flavor has no golden baseline and cannot be regenerated.")
        return 2

    manifest = load_manifest(root)
    layout = GoldenLayout(golden_root)
    by_class = classify_entries(manifest["entries"])
    audience = detect_audience(root, manifest)

    delete_set = resolve_delete_set(by_class["pure-framework"], layout)
    mixed_entries = by_class["mixed"]
    preserve_entries = by_class["project-owned"]

    # Golden-staleness predicate: files newer than the golden freeze (e.g. post-
    # sync updates) must not be rolled back — skip them in the restore (#159).
    staleness = GoldenStaleness.for_root(root)

    # Pre-flight summary (§3 step 1).
    print("=== regenerate-grimoire — pre-flight summary ===")
    print(f"  root:     {root}")
    print(f"  audience: {audience}")
    print(f"  delete+restore (pure-framework): {len(delete_set)} files")
    print(f"  split/merge (mixed):            {len(mixed_entries)} entries")
    print(f"  preserve (project-owned):       {len(preserve_entries)} entries (untouched)")
    if staleness.resolvable:
        ahead = [r for r in delete_set if staleness.is_newer(root / r)]
        if ahead:
            print(f"  newer-than-golden (will skip, not roll back): {len(ahead)} files")

    if check:
        print("\n[--check] dry-run; nothing will be written.\n")
        # Report what would change for mixed files.
        for e in mixed_entries:
            status, _ = apply_mixed_merge(root, layout, e, audience, write=False)
            print(f"  mixed  {e['path']}: would be {status}")
        # Report delete-set files not currently at golden (would be restored or
        # skipped if newer-than-golden).
        drift = 0
        for rel in delete_set:
            golden_rel = layout.repo_to_golden(rel)
            gp = layout.golden_root / golden_rel if golden_rel else None
            cur = root / rel
            if gp and gp.exists():
                if not cur.exists() or cur.read_bytes() != gp.read_bytes():
                    if staleness.is_newer(cur):
                        print(f"  pure-framework (newer-than-golden, would skip): {rel}")
                    else:
                        drift += 1
        print(f"\n  pure-framework files differing from golden (would be restored): {drift}")
        return 0

    if not assume_yes:
        # In a real interactive run a human confirms here; --yes for automation.
        print("\nRefusing to mutate without --yes (or interactive confirmation).")
        print("Re-run with --yes to proceed, or --check for a dry-run.")
        return 1

    # Archive-then-restore (§3 steps 2-4).
    ts = archive_timestamp(now)
    archive_targets = [(rel, "pure-framework") for rel in delete_set]
    archive_targets += [(e["path"], "mixed") for e in mixed_entries]
    archive_root = archive_files(root, archive_targets, ts)
    print(f"\nArchived delete-set + merge-set to {archive_root.relative_to(root)}")

    try:
        skipped = restore_from_golden(root, layout, delete_set, staleness)
        restored = len(delete_set) - len(skipped)
        print(f"Restored {restored} pure-framework files from golden.")
        if skipped:
            print(f"  Skipped {len(skipped)} newer-than-golden file(s) (post-sync, not rolled back):")
            for rel in skipped:
                print(f"    {rel}")
        for e in mixed_entries:
            status, _ = apply_mixed_merge(root, layout, e, audience, write=True)
            print(f"  mixed  {e['path']}: {status}")
    except Exception as exc:  # rollback
        print(f"\nERROR during restore: {exc}. Rolling back from archive...")
        _rollback(root, archive_root)
        return 3

    removed = _cleanup_transient_on_success(root)
    if removed:
        print("Cleaned transient workspace artifacts: " + ", ".join(removed))

    print("\nRegenerate complete. Project files preserved; framework layer restored.")
    return 0


# Transient workspace dirs swept after a SUCCESSFUL regenerate (v3.52 Lane F).
# Re-clonable / regenerable staging + rollback backups that otherwise linger in
# the consumer's project root. Load-bearing state (.scaffold-base/, .claude/,
# the .grimoire-golden/ cache, the .grimoire-archive/ safety net) is NEVER swept.
TRANSIENT_CLEANUP_DIRS = (".scaffold-sync-backup", ".grimoire-source")


def _cleanup_transient_on_success(root: Path) -> list[str]:
    """Remove transient staging/backup dirs left in the workspace after a clean
    regenerate. Idempotent; returns the repo-relative names actually removed.
    Hard-coded to the transient set only — never deletes load-bearing state."""
    removed: list[str] = []
    for name in TRANSIENT_CLEANUP_DIRS:
        target = root / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            if not target.exists():
                removed.append(name + "/")
    return removed


def _rollback(root: Path, archive_root: Path):
    """Restore archived originals over the partially-modified tree."""
    for p in archive_root.rglob("*"):
        if p.is_file() and p.name != "MANIFEST.md":
            rel = p.relative_to(archive_root)
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)


# ---------------------------------------------------------------------------
# Self-test (tempdir round-trip + idempotency + every mixed merger)
# ---------------------------------------------------------------------------


def _build_fixture(base: Path) -> Path:
    """Create a throwaway repo fixture with a golden tree + framework + project
    files. Returns the repo root. Never touches the real repo."""
    root = base / "repo"
    golden = root / GOLDEN_CACHE_REL
    (golden / "hooks").mkdir(parents=True)
    (golden / "skills" / "demo-skill").mkdir(parents=True)
    (golden / "docs").mkdir(parents=True)

    # --- golden (restore source) ---
    (golden / "hooks" / "guard.sh").write_text("#!/bin/sh\necho golden-guard\n")
    (golden / "skills" / "demo-skill" / "SKILL.md").write_text("# demo-skill (golden)\n")
    (golden / "CLAUDE.md").write_text(
        SENTINEL + "\n# CLAUDE.md\n\n"
        "> **Paradigm:** {ACTIVE} — one of Supervised · Weiss · Noir.\n\n"
        "| Run tests | `{test-command}` |\n"
    )
    (golden / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["fw_allow_1", "fw_allow_2"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": ["guard.sh"]}]},
    }, indent=4) + "\n")
    (golden / ".gitignore").write_text(
        f"{GITIGNORE_BEGIN}\n.grimoire-archive/\n.claude/cache/\n{GITIGNORE_END}\n"
    )
    (golden / "docs").joinpath("roadmap.md").write_text(
        "# Roadmap\n\n| Item | Status |\n|---|---|\n| BASELINE-UX-defer | deferred |\n"
    )
    (golden / "docs").joinpath("version-history.md").write_text(
        "# Version history\n\n_No releases yet._\n"
    )

    # --- live repo files ---
    # pure-framework, drifted (will be restored to golden).
    # Backdate the hook to before the golden tree mtime so GoldenStaleness does
    # not classify it as newer-than-golden and skip the restore — this ensures
    # the fixture tests the "genuinely drifted → restore" path, not the skip path.
    (root / ".claude" / "hooks").mkdir(parents=True)
    hook_file = root / ".claude" / "hooks" / "guard.sh"
    hook_file.write_text("#!/bin/sh\necho TAMPERED\n")
    old_time = (root / GOLDEN_CACHE_REL).stat().st_mtime - 10.0
    os.utime(hook_file, (old_time, old_time))
    # pure-framework, will be DELETED entirely then restored:
    (root / ".claude" / "skills" / "demo-skill").mkdir(parents=True)
    # (intentionally leave SKILL.md missing to test restore-of-missing)

    # mixed: CLAUDE.md with RESOLVED placeholders (both lowercase and uppercase),
    # no sentinel (live project). The {ACTIVE} token is uppercase — regression #160.
    (root / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\n"
        "> **Paradigm:** Noir — one of Supervised · Weiss · Noir.\n\n"
        "| Run tests | `pytest -q` |\n"
    )
    # mixed: settings.json with a project key the merge must preserve:
    (root / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["project_allow"]},
        "env": {"PROJECT_VAR": "1"},
    }, indent=4) + "\n")
    # mixed: .gitignore with project lines + an outdated managed section:
    (root / ".gitignore").write_text(
        "node_modules/\n*.log\n"
        f"{GITIGNORE_BEGIN}\nOLD_ENTRY\n{GITIGNORE_END}\n"
        "dist/\n"
    )
    # mixed: roadmap with project rows (baseline row missing):
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "roadmap.md").write_text(
        "# Roadmap\n\n| Item | Status |\n|---|---|\n| PROJECT-FEATURE-A | done |\n"
    )
    # mixed: version-history with project entries (root audience leaves it):
    (root / "docs" / "version-history.md").write_text(
        "# Version history\n\n## v1.0 — project release\n"
    )

    # project-owned: must survive untouched.
    (root / "docs" / "design").mkdir(parents=True)
    (root / "docs" / "design" / "my-feature.md").write_text("PROJECT DESIGN — keep me\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('project source')\n")

    # manifest
    manifest = {
        "schema_version": 1,
        "grimoire_version": "test",
        "flavor": "consumer",
        "entries": [
            {"path": ".claude/hooks/guard.sh", "class": "pure-framework"},
            {"path": ".claude/skills/**", "class": "pure-framework"},
            {"path": "CLAUDE.md", "class": "mixed"},
            {"path": ".claude/settings.json", "class": "mixed"},
            {"path": ".gitignore", "class": "mixed"},
            {"path": "docs/roadmap.md", "class": "mixed"},
            {"path": "docs/version-history.md", "class": "mixed"},
            {"path": "docs/design/**", "class": "project-owned"},
            {"path": "src/**", "class": "project-owned"},
        ],
    }
    (root / ".claude" / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return root


def _run_self_test() -> int:
    fails = 0

    def check(label, cond):
        nonlocal fails
        if cond:
            print(f"OK   [{label}]")
        else:
            print(f"FAIL [{label}]")
            fails += 1

    # ---- Unit: mergers are idempotent + correct ----
    print("--- mixed mergers ---")
    g_settings = json.dumps({"permissions": {"allow": ["fw1"]}, "hooks": {"x": 1}}, indent=4) + "\n"
    c_settings = json.dumps({"permissions": {"allow": ["proj"]}, "env": {"E": "1"}}, indent=4) + "\n"
    m1 = merge_settings_json(c_settings, g_settings)
    d1 = json.loads(m1)
    check("settings: project env preserved", d1.get("env") == {"E": "1"})
    check("settings: framework allow present", "fw1" in d1["permissions"]["allow"])
    check("settings: project allow preserved", "proj" in d1["permissions"]["allow"])
    check("settings: framework hooks reset to golden", d1.get("hooks") == {"x": 1})
    check("settings idempotent", merge_settings_json(m1, g_settings) == m1)

    g_claude = SENTINEL + "\n# CLAUDE.md\n\n| Run tests | `{test-command}` |\n"
    c_claude = "# CLAUDE.md\n\n| Run tests | `pytest -q` |\n"
    m2 = merge_agent_guidance(c_claude, g_claude)
    check("agent: placeholder re-injected", "pytest -q" in m2)
    check("agent: no raw placeholder left", "{test-command}" not in m2)
    check("agent: sentinel NOT re-armed (clean project)", not m2.startswith(SENTINEL))
    check("agent idempotent", merge_agent_guidance(m2, g_claude) == m2)
    # sentinel preserved when current has it
    c_claude_sent = SENTINEL + "\n# CLAUDE.md\n\n| Run tests | `{test-command}` |\n"
    m2b = merge_agent_guidance(c_claude_sent, g_claude)
    check("agent: sentinel preserved (pre-onboarding)", m2b.startswith(SENTINEL))

    g_gi = f"{GITIGNORE_BEGIN}\n.grimoire-archive/\n{GITIGNORE_END}\n"
    c_gi = f"node_modules/\n{GITIGNORE_BEGIN}\nOLD\n{GITIGNORE_END}\ndist/\n"
    m3 = merge_gitignore(c_gi, g_gi)
    check("gitignore: project lines kept", "node_modules/" in m3 and "dist/" in m3)
    check("gitignore: managed section reset", ".grimoire-archive/" in m3 and "OLD" not in m3)
    check("gitignore idempotent", merge_gitignore(m3, g_gi) == m3)
    # append when absent
    m3b = merge_gitignore("foo/\n", g_gi)
    check("gitignore: appended when absent", GITIGNORE_BEGIN in m3b and "foo/" in m3b)
    check("gitignore append idempotent", merge_gitignore(m3b, g_gi) == m3b)

    g_rm = "# Roadmap\n\n| Item | Status |\n|---|---|\n| BASE-row | x |\n"
    c_rm = "# Roadmap\n\n| Item | Status |\n|---|---|\n| PROJ-row | done |\n"
    m4 = merge_roadmap(c_rm, g_rm)
    check("roadmap: project row kept", "PROJ-row" in m4)
    check("roadmap: missing baseline appended", "BASE-row" in m4)
    check("roadmap idempotent", merge_roadmap(m4, g_rm) == m4)

    g_vh = "# Version history\n\n_No releases yet._\n"
    c_vh = "# Version history\n\n## v1.0\n"
    check("version-history root: log left", merge_version_history(c_vh, g_vh, "root") == c_vh)
    check("version-history consumer: existing kept", merge_version_history(c_vh, g_vh, "consumer") == c_vh)
    check("version-history consumer: empty seeded", merge_version_history("", g_vh, "consumer") == g_vh)

    # ---- GoldenLayout mapping round-trips ----
    print("\n--- GoldenLayout ---")
    with tempfile.TemporaryDirectory() as td:
        gl = GoldenLayout(Path(td))
        check("map hooks", gl.golden_to_repo("hooks/guard.sh") == ".claude/hooks/guard.sh")
        check("map mcp.json", gl.golden_to_repo("mcp.json") == ".mcp.json")
        check("map CLAUDE.md", gl.golden_to_repo("CLAUDE.md") == "CLAUDE.md")
        check("map docs", gl.golden_to_repo("docs/roadmap.md") == "docs/roadmap.md")
        check("reverse hooks", gl.repo_to_golden(".claude/hooks/guard.sh") == "hooks/guard.sh")
        check("reverse mcp", gl.repo_to_golden(".mcp.json") == "mcp.json")
        check("reverse non-golden None", gl.repo_to_golden("src/app.py") is None)

    # ---- Integration: full round-trip on a tempdir fixture ----
    print("\n--- round-trip (tempdir fixture) ---")
    with tempfile.TemporaryDirectory() as td:
        root = _build_fixture(Path(td))
        # (a) seed established. (b) framework already drifted/missing in fixture.
        rc = regenerate(root, check=False, assume_yes=True,
                        now=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
        check("regenerate exit 0", rc == 0)
        # (d) framework restored from golden:
        guard = (root / ".claude" / "hooks" / "guard.sh").read_text()
        check("fw hook restored from golden", "golden-guard" in guard)
        skill = root / ".claude" / "skills" / "demo-skill" / "SKILL.md"
        check("fw missing skill restored", skill.exists() and "golden" in skill.read_text())
        # project files preserved:
        check("project design preserved",
              (root / "docs" / "design" / "my-feature.md").read_text() == "PROJECT DESIGN — keep me\n")
        check("project source preserved",
              (root / "src" / "app.py").read_text() == "print('project source')\n")
        # mixed project content preserved:
        settings = json.loads((root / ".claude" / "settings.json").read_text())
        check("mixed settings: project env kept", settings.get("env") == {"PROJECT_VAR": "1"})
        check("mixed settings: project allow kept", "project_allow" in settings["permissions"]["allow"])
        check("mixed settings: fw allow restored", "fw_allow_1" in settings["permissions"]["allow"])
        claude = (root / "CLAUDE.md").read_text()
        check("mixed CLAUDE: lowercase placeholder kept", "pytest -q" in claude)
        check("mixed CLAUDE: uppercase {ACTIVE} not left in output", "{ACTIVE}" not in claude)
        check("mixed CLAUDE: paradigm value Noir preserved", "Noir" in claude)
        gi = (root / ".gitignore").read_text()
        check("mixed gitignore: project kept", "node_modules/" in gi and "dist/" in gi)
        rm = (root / "docs" / "roadmap.md").read_text()
        check("mixed roadmap: project row kept", "PROJECT-FEATURE-A" in rm)
        check("mixed roadmap: baseline added", "BASELINE-UX-defer" in rm)
        # archive written:
        arch = root / ARCHIVE_DIR / "20260101-000000"
        check("archive dir created", arch.exists())
        check("archive MANIFEST written", (arch / "MANIFEST.md").exists())
        check("archive captured tampered hook",
              (arch / ".claude" / "hooks" / "guard.sh").read_text() == "#!/bin/sh\necho TAMPERED\n")

        # (e) second run = idempotent (no change to any tracked file).
        snapshot = _snapshot(root)
        rc2 = regenerate(root, check=False, assume_yes=True,
                         now=datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc))
        check("second regenerate exit 0", rc2 == 0)
        snapshot2 = _snapshot(root)
        check("idempotent: tracked files unchanged on run 2", snapshot == snapshot2)

        # --check dry-run writes nothing.
        before = _snapshot(root)
        rc3 = regenerate(root, check=True, assume_yes=False)
        check("--check exit 0", rc3 == 0)
        check("--check wrote nothing", _snapshot(root) == before)

    # ---- Regression: #160 — uppercase {ACTIVE} placeholder preserved ----
    # The old PLACEHOLDER_RE only matched lowercase tokens; {ACTIVE} was silently
    # left unreplaced after the merge, reverting the paradigm stamp.
    print("\n--- regression #160: uppercase placeholder (ACTIVE) ---")
    g_active = (
        SENTINEL + "\n# CLAUDE.md\n\n"
        "> **Paradigm:** {ACTIVE} — one of Supervised · Weiss · Noir.\n"
        "> Switch via `grm-work-paradigm-switch`.\n\n"
        "| Build | `{build-command}` |\n"
    )
    c_active = (
        "# CLAUDE.md\n\n"
        "> **Paradigm:** Noir — one of Supervised · Weiss · Noir.\n"
        "> Switch via `grm-work-paradigm-switch`.\n\n"
        "| Build | `cargo build` |\n"
    )
    m_active = merge_agent_guidance(c_active, g_active)
    check("ACTIVE: paradigm value preserved (Noir)", "Noir" in m_active)
    check("ACTIVE: {ACTIVE} token not left in output", "{ACTIVE}" not in m_active)
    check("ACTIVE: build-command preserved", "cargo build" in m_active)
    check("ACTIVE: sentinel not re-armed (clean project)", not m_active.startswith(SENTINEL))
    check("ACTIVE idempotent", merge_agent_guidance(m_active, g_active) == m_active)

    # ---- Regression: #159 — newer-than-golden files not rolled back ----
    # GoldenStaleness.is_newer must skip files that a post-sync has advanced past
    # the golden freeze time, so restore_from_golden never reverts them.
    print("\n--- regression #159: newer-than-golden files not rolled back ---")
    import time
    with tempfile.TemporaryDirectory() as td:
        root_ng = Path(td) / "repo"
        golden_ng = root_ng / GOLDEN_CACHE_REL
        (golden_ng / "hooks").mkdir(parents=True)
        (golden_ng / "skills" / "synced-skill").mkdir(parents=True)
        (root_ng / ".claude" / "hooks").mkdir(parents=True)
        (root_ng / ".claude" / "skills" / "synced-skill").mkdir(parents=True)

        # Write golden file first, then write live version 0.1s later (newer).
        (golden_ng / "hooks" / "guard.sh").write_text("#!/bin/sh\necho GOLDEN\n")
        (golden_ng / "skills" / "synced-skill" / "SKILL.md").write_text("# golden-skill\n")
        # Manufacture a golden archive mtime in the past.
        cache_dir = root_ng / ".grimoire-golden"
        arch_file = cache_dir / "golden-v3.50.tar.gz"
        arch_file.write_bytes(b"x")
        past_time = time.time() - 5.0  # archive frozen 5 seconds ago
        os.utime(arch_file, (past_time, past_time))

        # Live skill SKILL.md is newer than the archive (simulates post-sync).
        time.sleep(0.05)
        synced_live = root_ng / ".claude" / "skills" / "synced-skill" / "SKILL.md"
        synced_live.write_text("# synced-skill — newer version from upstream\n")

        # Live hook is older than the archive (genuinely drifted — should be restored).
        drifted_hook = root_ng / ".claude" / "hooks" / "guard.sh"
        drifted_hook.write_text("#!/bin/sh\necho TAMPERED\n")
        old_time = past_time - 10.0  # hook was last touched before the golden freeze
        os.utime(drifted_hook, (old_time, old_time))

        manifest_ng = {
            "schema_version": 1,
            "grimoire_version": "test",
            "flavor": "consumer",
            "entries": [
                {"path": ".claude/hooks/guard.sh", "class": "pure-framework"},
                {"path": ".claude/skills/**", "class": "pure-framework"},
            ],
        }
        (root_ng / ".claude").mkdir(parents=True, exist_ok=True)
        (root_ng / ".claude" / MANIFEST_FILENAME).write_text(json.dumps(manifest_ng, indent=2))

        rc_ng = regenerate(root_ng, check=False, assume_yes=True)
        check("newer-than-golden: regenerate exits 0", rc_ng == 0)
        # The synced SKILL.md must NOT be reverted to the golden version.
        check("newer-than-golden: synced file not rolled back",
              "newer version from upstream" in synced_live.read_text())
        # The drifted hook MUST be restored to golden (it is older, so not skipped).
        check("newer-than-golden: genuinely-drifted hook is restored",
              "GOLDEN" in drifted_hook.read_text())

    # ---- copilot-style refusal: no golden ----
    print("\n--- no-golden refusal ---")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "nogolden"
        (root / ".claude").mkdir(parents=True)
        (root / ".claude" / MANIFEST_FILENAME).write_text(
            json.dumps({"schema_version": 1, "flavor": "copilot", "entries": []})
        )
        rc = regenerate(root, check=False, assume_yes=True)
        check("refuses without golden (exit 2)", rc == 2)

    # ---- transient cleanup-on-success (v3.52 Lane F) ----
    print("\n--- transient cleanup ---")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "ws"
        # transient (must be swept)
        (root / ".scaffold-sync-backup" / "20260101-000000").mkdir(parents=True)
        (root / ".grimoire-source" / ".claude").mkdir(parents=True)
        # load-bearing / cache (must NEVER be swept)
        (root / ".scaffold-base").mkdir(parents=True)
        (root / ".claude").mkdir(parents=True)
        (root / ".grimoire-golden").mkdir(parents=True)
        (root / ".grimoire-archive").mkdir(parents=True)
        removed = _cleanup_transient_on_success(root)
        check("cleanup: .scaffold-sync-backup removed", not (root / ".scaffold-sync-backup").exists())
        check("cleanup: .grimoire-source removed", not (root / ".grimoire-source").exists())
        check("cleanup: load-bearing .scaffold-base kept", (root / ".scaffold-base").is_dir())
        check("cleanup: .claude kept", (root / ".claude").is_dir())
        check("cleanup: .grimoire-golden cache kept", (root / ".grimoire-golden").is_dir())
        check("cleanup: .grimoire-archive safety net kept", (root / ".grimoire-archive").is_dir())
        check("cleanup: reports removed names", set(removed) == {".scaffold-sync-backup/", ".grimoire-source/"})
        # idempotent: second run removes nothing, raises nothing
        check("cleanup idempotent (no-op second run)", _cleanup_transient_on_success(root) == [])

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} — {fails} failure(s)")
    return 0 if fails == 0 else 1


def _snapshot(root: Path) -> dict[str, bytes]:
    """Content snapshot of all files EXCEPT the archive dir (which grows each
    run) and golden (the source). Used to assert idempotency."""
    out = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(ARCHIVE_DIR + "/") or rel.startswith(GOLDEN_CACHE_PREFIX):
            continue
        out[rel] = p.read_bytes()
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Surgical regenerate of the Grimoire framework layer (CR-5).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--root", type=Path, default=None,
                        help="Repo root (default: auto-detect).")
    parser.add_argument("--check", "--dry-run", dest="check", action="store_true",
                        help="Dry-run: report what would change; write nothing.")
    parser.add_argument("--yes", action="store_true",
                        help="Proceed without interactive confirmation.")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="Run offline tempdir self-tests and exit.")
    args = parser.parse_args(argv)

    if args.self_test:
        sys.exit(_run_self_test())

    if args.root is None:
        here = Path(__file__).resolve().parent
        candidate = here
        for _ in range(12):
            if (candidate / ".claude" / MANIFEST_FILENAME).exists():
                root = candidate
                break
            candidate = candidate.parent
        else:
            print("ERROR: could not auto-detect repo root (no .claude/grimoire-files.json).")
            sys.exit(2)
    else:
        root = args.root.resolve()

    sys.exit(regenerate(root, check=args.check, assume_yes=args.yes))


if __name__ == "__main__":
    main()
