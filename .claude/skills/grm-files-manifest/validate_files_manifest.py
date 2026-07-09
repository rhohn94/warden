#!/usr/bin/env python3
"""Grimoire framework-files manifest validator (CR-4).

Validates .claude/grimoire-files.json against the real tree to detect:
  - UNLISTED_PRESENT: files in the tree that look Grimoire-owned but aren't in
    the manifest (potential omissions).
  - LISTED_ABSENT:   paths in the manifest that do not exist in the tree
    (stale entries, or post-CR-3 relocations not yet applied).
  - MISTAG:          entries whose 'class' disagrees with the manifest.md
    restorable-skill list (pure-framework expected; mixed/project-owned flagged
    as likely mis-tag).

Usage:
    python3 validate_files_manifest.py [--root ROOT] [--flavor FLAVOR] [--strict]
    python3 validate_files_manifest.py --self-test

Options:
    --root ROOT       Repo root to validate (default: auto-detect from script location).
    --flavor FLAVOR   Which flavor manifest to validate: claude-code | copilot | root
                      (default: auto-detect from .grimoire-flavor marker).
    --strict          Exit 1 on any finding (default: exit 0, print findings).
    --self-test       Run offline self-tests and exit (no real tree needed).

Stdlib-only (no third-party dependencies).
Cross-reference: .claude/skills/grm-workflow-bootstrap/manifest.md enumerates
restorable skills; this validator cross-checks class tags against that list.
"""

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

MANIFEST_FILENAME = "grimoire-files.json"
MANIFEST_MD_PATH = ".claude/skills/grm-workflow-bootstrap/manifest.md"

# Framework-owned path prefixes — files under these that aren't in the manifest
# are candidates for UNLISTED_PRESENT. Must not match project-owned trees.
FRAMEWORK_OWNED_PREFIXES = (
    ".claude/hooks/",
    ".claude/skills/",
    ".claude/paradigms/",
    ".claude/mcp-servers/",
    ".claude/workflows/",
    ".claude/stealth/",
    ".claude/quick-start-templates/",
    "docs/grimoire/",
    "docs/coding-standards/",
    # Copilot-specific
    ".github/prompts/",
    "git-hooks/",
    "mcp-servers/",
    "scripts/",
)

# Exact top-level framework files to watch for UNLISTED_PRESENT.
FRAMEWORK_TOP_LEVEL = (
    "CLAUDE.md",
    "AGENTS.md",
    ".gitignore",
    ".gitattributes",
    ".mcp.json",
    ".grimoire-flavor",
    ".scaffold-upstream.conf",
    "vendor.toml",
    "docs/README.md",
    "docs/roadmap.md",
    "docs/version-history.md",
    "docs/quickstart.md",
    "docs/features.md",
    "docs/architecture-guidelines.md",
    "docs/coding-standards.md",
    # clean-room-separation (v3.41) relocates these operational docs under
    # docs/grimoire/ — the canonical homes, not the stranded top-level paths.
    "docs/grimoire/version-design.md",
    "docs/grimoire/integration-workflow.md",
    ".claude/grimoire-config.json",
    ".claude/settings.json",
    ".claude/push-allowlist",
    ".claude/model-effort-profiles.json",
    ".claude/architecture-rules.example.json",
    ".claude/grimoire-files.json",
    # Copilot-specific
    ".github/copilot-instructions.md",
)

# Paths that may be absent in the root (dogfood) flavor but are not errors.
# Root is Grimoire's own repo and lacks consumer-flavor markers.
ROOT_FLAVOR_CONDITIONALLY_ABSENT = {
    ".grimoire-flavor",  # present in claude-code/ and copilot/ subdirs, not at root level
}

# Globs that are never framework-owned (project content).
PROJECT_OWNED_GLOB_EXCLUSIONS = (
    "docs/design/**",
    ".grimoire-archive/**",
    ".claude/worktrees/**",
    ".claude/cache/**",
    ".claude/integration-allow.local",
    ".claude/settings.local.json",
    ".claude/architecture-rules.json",
    "vendor.lock",
    "vendor/**",
    "lib/third-party/**",
    "claude-code/**",
    "copilot/**",
    ".DS_Store",
    "*/.DS_Store",
)


def _load_manifest(root: Path, flavor: str) -> dict:
    """Load grimoire-files.json for the given flavor root."""
    manifest_path = root / ".claude" / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with manifest_path.open() as f:
        data = json.load(f)
    if data.get("schema_version") != 1:
        raise ValueError(f"Unsupported schema_version: {data.get('schema_version')}")
    return data


def _detect_flavor(root: Path) -> str:
    """Auto-detect flavor from .grimoire-flavor marker."""
    marker = root / ".grimoire-flavor"
    if marker.exists():
        content = marker.read_text().strip()
        if "copilot" in content.lower():
            return "copilot"
        if "claude-code" in content.lower():
            return "claude-code"
    # If no marker, assume root (Grimoire's own repo)
    return "root"


def _is_project_owned(rel: str) -> bool:
    """True if the path is project-owned and should never appear in the manifest."""
    for pattern in PROJECT_OWNED_GLOB_EXCLUSIONS:
        if fnmatch.fnmatch(rel, pattern):
            return True
        # also match directory prefix
        if pattern.endswith("/**") and (rel + "/").startswith(pattern[:-3] + "/"):
            return True
    return False


def _glob_matches(pattern: str, rel: str) -> bool:
    """Check if a manifest path pattern matches a real relative path.

    Supports fnmatch-style globs. '**' is treated as 'any sequence of
    path components'.
    """
    if "**" not in pattern:
        return fnmatch.fnmatch(rel, pattern) or rel == pattern
    # Convert ** glob to a regex.
    # Replace ** with a placeholder, escape rest, then restore.
    escaped = re.escape(pattern).replace(r"\*\*", "__DSTAR__").replace(r"\*", "[^/]*")
    regex = escaped.replace("__DSTAR__", ".*")
    return bool(re.fullmatch(regex, rel))


def _collect_tree_files(root: Path) -> list[str]:
    """Collect all non-.DS_Store files in the repo root as relative POSIX paths."""
    result = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if ".DS_Store" in rel:
            continue
        result.append(rel)
    return sorted(result)


def _looks_framework_owned(rel: str) -> bool:
    """Heuristic: does this path look like it should be in the manifest?"""
    if _is_project_owned(rel):
        return False
    for prefix in FRAMEWORK_OWNED_PREFIXES:
        if rel.startswith(prefix):
            return True
    if rel in FRAMEWORK_TOP_LEVEL:
        return True
    # docs/ top-level framework files (not under docs/design/)
    if rel.startswith("docs/") and not rel.startswith("docs/design/"):
        return True
    return False


def _check_unlisted_present(manifest_entries: list[dict], tree_files: list[str]) -> list[str]:
    """Find files in tree that look framework-owned but aren't in the manifest."""
    findings = []
    for rel in tree_files:
        if not _looks_framework_owned(rel):
            continue
        # Check if covered by any manifest entry (exact or glob).
        covered = any(_glob_matches(e["path"], rel) for e in manifest_entries)
        if not covered:
            findings.append(f"UNLISTED_PRESENT: {rel}")
    return findings


def _check_listed_absent(
    manifest_entries: list[dict], tree_files: list[str], flavor: str = ""
) -> list[str]:
    """Find manifest entries with no matching file in the tree.

    Skips glob entries (can't assert absence for a glob) and entries with
    ships=false + regenerate_disposition starting with 'relocate' (CR-3
    pending) or 'root-only' (absent in non-root copies is expected).
    """
    findings = []
    tree_set = set(tree_files)
    for entry in manifest_entries:
        pattern = entry["path"]
        disposition = entry.get("regenerate_disposition", "")
        ships = entry.get("ships", True)
        # Skip globs — can't assert absence.
        if "*" in pattern:
            continue
        # Skip CR-3-pending relocations.
        if not ships and disposition.startswith("relocate"):
            continue
        # Skip seed-only entries — may not exist yet on a fresh project.
        if disposition.startswith("seed-only"):
            continue
        # Skip root-flavor-specific conditionally-absent paths.
        if flavor == "root" and pattern in ROOT_FLAVOR_CONDITIONALLY_ABSENT:
            continue
        # The path should exist.
        if pattern not in tree_set:
            findings.append(f"LISTED_ABSENT: {pattern}")
    return findings


def _load_manifest_md_skills(root: Path) -> set[str]:
    """Parse manifest.md and return the set of skill names listed as restorable."""
    manifest_md = root / MANIFEST_MD_PATH
    if not manifest_md.exists():
        return set()
    skills = set()
    for line in manifest_md.read_text().splitlines():
        # Lines like: | `skill-name` | description |
        m = re.match(r"\|\s+`([a-z][a-z0-9-]+)`\s+\|", line)
        if m:
            skills.add(m.group(1))
    return skills


def _check_mistag(manifest_entries: list[dict], manifest_md_skills: set[str]) -> list[str]:
    """Flag manifest entries for skill SKILL.md paths that aren't pure-framework."""
    findings = []
    for entry in manifest_entries:
        path = entry["path"]
        cls = entry.get("class", "")
        # Skill SKILL.md paths: .claude/skills/<name>/SKILL.md
        m = re.match(r"\.claude/skills/([^/]+)/SKILL\.md$", path)
        if m:
            skill_name = m.group(1)
            if skill_name in manifest_md_skills and cls != "pure-framework":
                findings.append(
                    f"MISTAG: {path} — manifest.md lists '{skill_name}' as restorable "
                    f"(implies pure-framework) but class is '{cls}'"
                )
    return findings


def validate(root: Path, flavor: str, strict: bool) -> int:
    """Run all checks. Return 0 if no findings (or strict=False), 1 on findings+strict."""
    data = _load_manifest(root, flavor)
    manifest_flavor = data.get("flavor", "unknown")
    entries = data.get("entries", [])

    print(f"Validating {manifest_flavor} manifest at {root / '.claude' / MANIFEST_FILENAME}")
    print(f"  {len(entries)} entries, schema_version={data['schema_version']}")

    tree_files = _collect_tree_files(root)
    print(f"  {len(tree_files)} files in tree")

    findings: list[str] = []

    # Check 1: unlisted-present
    unlisted = _check_unlisted_present(entries, tree_files)
    findings.extend(unlisted)

    # Check 2: listed-absent
    absent = _check_listed_absent(entries, tree_files, flavor=manifest_flavor)
    findings.extend(absent)

    # Check 3: mistag (cross-check with manifest.md)
    manifest_md_skills = _load_manifest_md_skills(root)
    mistagged = _check_mistag(entries, manifest_md_skills)
    findings.extend(mistagged)

    if findings:
        print(f"\n{len(findings)} finding(s):")
        for f in findings:
            print(f"  {f}")
        if strict:
            print("\nExit 1 (--strict)")
            return 1
        print("\nExit 0 (no --strict; findings are informational)")
        return 0
    else:
        print("\nOK — no findings.")
        return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    """Offline self-tests. No real tree or manifest file needed. Returns 0=pass."""
    fails = 0

    def assert_eq(label: str, got, want):
        nonlocal fails
        if got != want:
            print(f"FAIL [{label}]: got {got!r}, want {want!r}")
            fails += 1
        else:
            print(f"OK   [{label}]")

    def assert_true(label: str, value: bool):
        nonlocal fails
        if not value:
            print(f"FAIL [{label}]: expected True, got False")
            fails += 1
        else:
            print(f"OK   [{label}]")

    def assert_false(label: str, value: bool):
        nonlocal fails
        if value:
            print(f"FAIL [{label}]: expected False, got True")
            fails += 1
        else:
            print(f"OK   [{label}]")

    print("--- Self-test: _glob_matches ---")
    assert_true("exact match", _glob_matches("CLAUDE.md", "CLAUDE.md"))
    assert_false("exact mismatch", _glob_matches("CLAUDE.md", "AGENTS.md"))
    assert_true("glob /** matches nested", _glob_matches(".claude/skills/**", ".claude/skills/foo/SKILL.md"))
    assert_true("glob /** matches direct child", _glob_matches(".claude/skills/**", ".claude/skills/foo"))
    assert_false("glob /** no match outside", _glob_matches(".claude/skills/**", ".claude/hooks/foo.sh"))
    assert_true("glob * matches segment", _glob_matches("docs/release-planning-v*.md", "docs/release-planning-v3.41.md"))
    assert_false("glob * no cross-dir", _glob_matches("docs/release-planning-v*.md", "docs/grimoire/release-planning-v3.md"))

    print("\n--- Self-test: _is_project_owned ---")
    assert_true("docs/design is project", _is_project_owned("docs/design/my-feature.md"))
    assert_true(".grimoire-archive is project", _is_project_owned(".grimoire-archive/2024/something.md"))
    assert_false(".claude/hooks is not project", _is_project_owned(".claude/hooks/push-guard.sh"))
    assert_false("CLAUDE.md is not project", _is_project_owned("CLAUDE.md"))

    print("\n--- Self-test: _looks_framework_owned ---")
    assert_true("CLAUDE.md looks fw", _looks_framework_owned("CLAUDE.md"))
    assert_true(".claude/hooks/push-guard.sh looks fw", _looks_framework_owned(".claude/hooks/push-guard.sh"))
    assert_true("docs/grimoire/README.md looks fw", _looks_framework_owned("docs/grimoire/README.md"))
    assert_true("docs/grimoire/version-design.md looks fw", _looks_framework_owned("docs/grimoire/version-design.md"))
    assert_true("docs/grimoire/integration-workflow.md looks fw", _looks_framework_owned("docs/grimoire/integration-workflow.md"))
    assert_true("docs/quickstart.md looks fw", _looks_framework_owned("docs/quickstart.md"))
    assert_false("docs/design/x.md not fw", _looks_framework_owned("docs/design/x.md"))
    assert_false(".grimoire-archive/x not fw", _looks_framework_owned(".grimoire-archive/x"))

    print("\n--- Self-test: FRAMEWORK_TOP_LEVEL clean-room homes (issue #187) ---")
    # clean-room-separation relocates these under docs/grimoire/; the stranded
    # top-level paths must NOT be required, the grimoire/ homes MUST be.
    assert_false("stranded top-level version-design dropped", "docs/version-design.md" in FRAMEWORK_TOP_LEVEL)
    assert_false("stranded top-level integration-workflow dropped", "docs/integration-workflow.md" in FRAMEWORK_TOP_LEVEL)
    assert_true("grimoire version-design required", "docs/grimoire/version-design.md" in FRAMEWORK_TOP_LEVEL)
    assert_true("grimoire integration-workflow required", "docs/grimoire/integration-workflow.md" in FRAMEWORK_TOP_LEVEL)

    print("\n--- Self-test: _check_unlisted_present ---")
    entries = [{"path": "CLAUDE.md", "class": "mixed"}]
    tree = ["CLAUDE.md", ".claude/hooks/push-guard.sh", "docs/design/my.md"]
    findings = _check_unlisted_present(entries, tree)
    # push-guard.sh is unlisted+looks fw → should appear; docs/design is project → should not
    assert_true("push-guard unlisted found", any("push-guard" in f for f in findings))
    assert_false("docs/design not flagged", any("docs/design" in f for f in findings))
    assert_false("CLAUDE.md not flagged (listed)", any("CLAUDE.md" in f for f in findings))

    print("\n--- Self-test: _check_listed_absent ---")
    entries2 = [
        {"path": "CLAUDE.md", "class": "mixed", "ships": True, "regenerate_disposition": "split/merge"},
        {"path": ".claude/hooks/push-guard.sh", "class": "pure-framework", "ships": True, "regenerate_disposition": "delete+restore from golden"},
        {"path": "docs/integration-workflow.md", "class": "pure-framework", "ships": False, "regenerate_disposition": "relocate to docs/grimoire/ (CR-3)"},
    ]
    tree2 = ["CLAUDE.md"]  # push-guard absent, integration-workflow absent (but CR-3 pending)
    abs_findings = _check_listed_absent(entries2, tree2)
    assert_true("push-guard absent flagged", any("push-guard" in f for f in abs_findings))
    assert_false("CR-3 pending not flagged", any("integration-workflow" in f for f in abs_findings))
    assert_false("CLAUDE.md present not flagged", any("CLAUDE.md" in f for f in abs_findings))

    print("\n--- Self-test: _check_mistag ---")
    entries3 = [
        {"path": ".claude/skills/grm-onboarding/SKILL.md", "class": "pure-framework"},
        {"path": ".claude/skills/bad-skill/SKILL.md", "class": "mixed"},  # bad tag
        {"path": ".claude/skills/unknown-skill/SKILL.md", "class": "mixed"},  # not in manifest.md → ok
    ]
    manifest_md_skills = {"onboarding", "bad-skill"}
    mt = _check_mistag(entries3, manifest_md_skills)
    assert_true("bad-skill mistag flagged", any("bad-skill" in f for f in mt))
    assert_false("onboarding ok", any("onboarding" in f for f in mt))
    assert_false("unknown-skill not in manifest.md → not flagged", any("unknown-skill" in f for f in mt))

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} — {fails} failure(s)")
    return 0 if fails == 0 else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate .claude/grimoire-files.json against the real tree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repo root to validate (default: auto-detect from script location).",
    )
    parser.add_argument(
        "--flavor",
        default=None,
        choices=["claude-code", "copilot", "root"],
        help="Flavor to validate (default: auto-detect from .grimoire-flavor).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on any finding.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        dest="self_test",
        help="Run offline self-tests and exit.",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        sys.exit(_run_self_test())

    # Auto-detect root: walk up from script location to find .claude/grimoire-files.json
    if args.root is None:
        here = Path(__file__).resolve().parent
        candidate = here
        for _ in range(10):
            if (candidate / ".claude" / MANIFEST_FILENAME).exists():
                root = candidate
                break
            candidate = candidate.parent
        else:
            print("ERROR: could not auto-detect repo root (no .claude/grimoire-files.json found).")
            sys.exit(2)
    else:
        root = args.root.resolve()

    flavor = args.flavor or _detect_flavor(root)
    sys.exit(validate(root, flavor, args.strict))


if __name__ == "__main__":
    main()
