#!/usr/bin/env python3
"""structure_migrate.py — one shared structure-detection engine, migrate side (#320).

`grm-architecture-audit` (Step 3a) and `grm-structure-migrate` used to
re-derive the same `structure`-block classification twice — once in
`architecture_fitness.py`, once as agent prose. This module closes that gap: it
**imports** (never duplicates) `load_rules()` / `check_structure()` / `Finding`
from `.claude/skills/grm-architecture-audit/architecture_fitness.py` — the same
cross-skill import pattern `grm-code-health` uses for that module's
`build_import_graph()` / `module_coupling()` (one shared scan feeding two
skills, not a second implementation) — and extends it with the two
migrate-only findings the audit doesn't need: `VENDOR_DEST` (a `vendor.toml`
`dest` still under a nonstandard alias) and `SUBMODULE_NONSTANDARD_DIR` (a
`NONSTANDARD_DIR` that is a registered git submodule per `.gitmodules`).

Detect findings are therefore identical, by construction, to
`grm-architecture-audit`'s `structure-required` / `structure-nonstandard` /
`structure-tracked-output` findings on the same tree (relabeled
`MISSING_REQUIRED` / `NONSTANDARD_DIR` (or `SUBMODULE_NONSTANDARD_DIR`) /
`TRACKED_OUTPUT`), plus `VENDOR_DEST`.

`--apply` performs the mechanical remedies:
  - a manifest of every planned action, archived to
    `.grimoire/structure-migration-<timestamp>.json` before anything moves;
  - `git mv` for each `NONSTANDARD_DIR` (collision-safe: if the standard home
    already exists, children are merged one at a time; an existing destination
    file is a collision, reported and skipped, never overwritten);
  - a `vendor.toml` `dest` rewrite for each `VENDOR_DEST`;
  - `.gitignore` append + `git rm -r --cached` for each `TRACKED_OUTPUT`.

Never auto-run, always flagged and skipped instead: `SUBMODULE_NONSTANDARD_DIR`
(a plain `git mv` would strand the `.gitmodules` `path` entry — see the SKILL.md
manual remedy) and any `NONSTANDARD_DIR` move that would break a source
`import`/`use` reference (grep'd across the tree for the literal old path).
`MISSING_REQUIRED` stays report-only forever — creating an empty `src/` or
`tests/` hides real work. Idempotent: a second `--apply` on an already-conformant
tree is a no-op (exit 0, no findings).

Design: docs/grimoire/design/file-structure-standard-design.md (+
architecture-fitness-design.md, which this module's import comes from).

Usage:
    structure_migrate.py --root DIR [--rules PATH] [--json]
    structure_migrate.py --root DIR --apply
    structure_migrate.py --self-test

Exit codes:
    0  no structure declared, or no findings, or --apply completed with nothing
       left unresolved (or --self-test passed)
    1  findings present (detect mode), or --apply left one or more findings
       unresolved (MISSING_REQUIRED / SUBMODULE_NONSTANDARD_DIR /
       import-breaking skip / collision) — or --self-test failed

Python stdlib only (#212 / #320 constraint); `tomllib` (3.11+) parses
`vendor.toml` when present — its absence degrades gracefully (VENDOR_DEST
detection is simply skipped, never a hard failure).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Shared detection engine import (#320) — sibling skill, not a duplicate copy.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ARCH_AUDIT_DIR = os.path.normpath(
    os.path.join(_HERE, "..", "grm-architecture-audit")
)
if _ARCH_AUDIT_DIR not in sys.path:
    sys.path.insert(0, _ARCH_AUDIT_DIR)

from architecture_fitness import (  # noqa: E402  (path-hack import, see above)
    load_rules,
    check_structure,
    RULES_REL_PATH,
)

CONFIG_REL_PATH = ".claude/grimoire-config.json"
GITMODULES_REL_PATH = ".gitmodules"
VENDOR_TOML_REL_PATH = "vendor.toml"
MANIFEST_DIR_REL_PATH = ".grimoire"

# Directories never descended into while grepping for import-breaking refs.
SKIP_SCAN_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".grimoire", ".grimoire-archive",
}

# Migrate-mode finding codes.
MISSING_REQUIRED = "MISSING_REQUIRED"
NONSTANDARD_DIR = "NONSTANDARD_DIR"
SUBMODULE_NONSTANDARD_DIR = "SUBMODULE_NONSTANDARD_DIR"
TRACKED_OUTPUT = "TRACKED_OUTPUT"
VENDOR_DEST = "VENDOR_DEST"

# architecture_fitness.py's Step 3a rule_id -> this skill's migrate code.
_RULE_ID_TO_CODE = {
    "structure-required": MISSING_REQUIRED,
    "structure-nonstandard": NONSTANDARD_DIR,
    "structure-tracked-output": TRACKED_OUTPUT,
}

# Codes --apply never auto-runs; always left for the agent/user.
REPORT_ONLY_CODES = {MISSING_REQUIRED, SUBMODULE_NONSTANDARD_DIR}

# Cap on import-breaking reference paths printed per skipped move, so a
# widely-referenced dir doesn't flood the console with hundreds of lines.
DISPLAY_REF_LIMIT = 20


@dataclass
class MigrateFinding:
    """One migrate-mode finding. `target` is the remedy destination (standard
    home dir, or rewritten vendor.toml dest) when one is computable; `name` is
    the `vendor.toml` dependency name for a VENDOR_DEST finding."""

    code: str
    path: str
    message: str
    target: Optional[str] = None
    old: Optional[str] = None
    name: Optional[str] = None

    def render(self) -> str:
        return f"  {self.path:<16} {self.code:<26} {self.message}"


# ---------------------------------------------------------------------------
# .gitmodules / submodule detection
# ---------------------------------------------------------------------------


def load_submodule_paths(root: str) -> set:
    """Return the set of `path =` entries declared in `.gitmodules` (repo-
    relative, trailing slash stripped). Absent file -> empty set."""
    path = os.path.join(root, GITMODULES_REL_PATH)
    if not os.path.isfile(path):
        return set()
    paths = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("path"):
                    _, _, value = stripped.partition("=")
                    value = value.strip()
                    if value:
                        paths.add(value.rstrip("/"))
    except OSError:
        return set()
    return paths


def is_submodule_dir(dirname: str, submodule_paths: set) -> bool:
    """True if `dirname` IS a registered submodule path, or CONTAINS one
    (a submodule nested one level inside a candidate move source)."""
    dirname = dirname.rstrip("/")
    for p in submodule_paths:
        if p == dirname or p.startswith(dirname + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# vendor.toml VENDOR_DEST detection
# ---------------------------------------------------------------------------


def classify_vendor_dest(root: str, structure: dict) -> list:
    """Flag every `[deps.<name>] dest` in `vendor.toml` whose first path
    segment matches a `structure.aliases` key (e.g. `vendor/aura` ->
    `lib/third-party/aura`). Absent `vendor.toml`, or an environment without
    `tomllib` (<3.11), degrades to no findings rather than a hard failure."""
    vendor_toml = os.path.join(root, VENDOR_TOML_REL_PATH)
    if not os.path.isfile(vendor_toml):
        return []
    aliases = structure.get("aliases", {}) if structure else {}
    if not aliases:
        return []
    try:
        import tomllib
    except ModuleNotFoundError:
        return []
    try:
        with open(vendor_toml, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, Exception):  # noqa: BLE001 — any parse failure degrades quietly
        return []
    deps = data.get("deps", {})
    if not isinstance(deps, dict):
        return []
    findings = []
    for name, spec in sorted(deps.items()):
        if not isinstance(spec, dict):
            continue
        dest = spec.get("dest")
        if not isinstance(dest, str) or not dest:
            continue
        top, _, rest = dest.partition("/")
        if top not in aliases:
            continue
        standard_home = aliases[top]
        new_dest = f"{standard_home}/{rest}" if rest else standard_home
        if new_dest == dest:
            continue
        findings.append(
            MigrateFinding(
                VENDOR_DEST,
                VENDOR_TOML_REL_PATH,
                f"deps.{name}.dest {dest!r} -> {new_dest!r}",
                target=new_dest,
                old=dest,
                name=name,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Detect: shared engine + migrate-only extensions
# ---------------------------------------------------------------------------


def classify_structure(root: str, rules: dict) -> list:
    """Run the shared `check_structure()` engine, relabel its findings to
    this skill's migrate codes (splitting NONSTANDARD_DIR into
    SUBMODULE_NONSTANDARD_DIR where applicable), and extend with VENDOR_DEST.
    Returns [] when no `structure` block is declared (caller checks that)."""
    structure = rules.get("structure")
    raw = check_structure(root, rules)
    submodule_paths = load_submodule_paths(root)
    aliases = structure.get("aliases", {}) if structure else {}

    findings = []
    for f in raw:
        code = _RULE_ID_TO_CODE[f.rule_id]
        path = f.path.rstrip("/")
        if code == NONSTANDARD_DIR:
            target = aliases.get(path)
            if is_submodule_dir(path, submodule_paths):
                findings.append(
                    MigrateFinding(
                        SUBMODULE_NONSTANDARD_DIR,
                        f.path,
                        "registered submodule -> manual move required (see SKILL.md)",
                        target=target,
                    )
                )
                continue
            findings.append(MigrateFinding(code, f.path, f.message, target=target))
            continue
        findings.append(MigrateFinding(code, f.path, f.message))

    findings.extend(classify_vendor_dest(root, structure))
    findings.sort(key=lambda f: (f.path, f.code))
    return findings


# ---------------------------------------------------------------------------
# --apply: import-breaking-move detection (grep the tree, never the AST)
# ---------------------------------------------------------------------------


def find_import_references(root: str, old_dir: str) -> list:
    """Grep every file in the tree (excluding `old_dir` itself and
    SKIP_SCAN_DIRS) for a literal reference to `old_dir` as a path segment —
    the cheap, language-agnostic proxy for "this move would break an
    import/use statement." Returns sorted `relpath:lineno` hits."""
    needle = old_dir.rstrip("/") + "/"
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.normpath(os.path.relpath(dirpath, root))
        rel_dir = "" if rel_dir == "." else rel_dir
        if rel_dir == old_dir or rel_dir.startswith(old_dir + "/"):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_SCAN_DIRS and not d.startswith(".")
        )
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.normpath(os.path.join(rel_dir, fn)) if rel_dir else fn
            try:
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if needle in line:
                            hits.append(f"{rel}:{lineno}")
            except OSError:
                continue
    return sorted(hits)


# ---------------------------------------------------------------------------
# --apply: mechanical remedies
# ---------------------------------------------------------------------------


def _run_git(root: str, *args: str):
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True
    )


def git_mv_merge(root: str, src_rel: str, dst_rel: str) -> tuple:
    """`git mv src_rel dst_rel`, collision-safe: if `dst_rel` already exists,
    merge children one at a time, skipping (and reporting) any child whose
    destination path already exists rather than overwriting it. Returns
    (moved: bool, collisions: list[str])."""
    src_abs = os.path.join(root, src_rel)
    dst_abs = os.path.join(root, dst_rel)
    if not os.path.exists(dst_abs):
        os.makedirs(os.path.dirname(dst_abs) or root, exist_ok=True)
        proc = _run_git(root, "mv", src_rel, dst_rel)
        if proc.returncode != 0:
            return False, [proc.stderr.strip() or "git mv failed"]
        return True, []

    collisions = []
    moved_any = False
    for entry in sorted(os.listdir(src_abs)):
        s_child = os.path.join(src_rel, entry)
        d_child = os.path.join(dst_rel, entry)
        d_child_abs = os.path.join(root, d_child)
        if os.path.exists(d_child_abs):
            collisions.append(d_child)
            continue
        proc = _run_git(root, "mv", s_child, d_child)
        if proc.returncode == 0:
            moved_any = True
        else:
            collisions.append(f"{d_child} ({proc.stderr.strip()})")
    try:
        if os.path.isdir(src_abs) and not os.listdir(src_abs):
            os.rmdir(src_abs)
    except OSError:
        pass
    return moved_any, collisions


def add_gitignore_and_untrack(root: str, dirname: str) -> None:
    """Append `<dirname>/` to `.gitignore` (if not already present) then
    `git rm -r --cached` it (keeps the files on disk, untracks them)."""
    entry = dirname.rstrip("/") + "/"
    gi_path = os.path.join(root, ".gitignore")
    existing = ""
    if os.path.isfile(gi_path):
        with open(gi_path, encoding="utf-8") as fh:
            existing = fh.read()
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    if entry not in existing_lines and dirname.rstrip("/") not in existing_lines:
        with open(gi_path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(entry + "\n")
    _run_git(root, "rm", "-r", "--cached", "--ignore-unmatch", dirname)


def rewrite_vendor_dest(root: str, name: str, old_dest: str, new_dest: str) -> bool:
    """Rewrite the `dest` field of `[deps.<name>]` in `vendor.toml` from
    `old_dest` to `new_dest`, text-based (tomllib is read-only in stdlib).
    Returns True on a successful single rewrite, False if no match found."""
    path = os.path.join(root, VENDOR_TOML_REL_PATH)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    section_re = re.compile(
        r"(\[deps\." + re.escape(name) + r"\][^\[]*?dest\s*=\s*\")"
        + re.escape(old_dest) + r"(\")"
    )
    new_text, count = section_re.subn(lambda m: m.group(1) + new_dest + m.group(2), text, count=1)
    if count == 0:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return True


def write_manifest(root: str, findings: list) -> str:
    """Archive a manifest of every planned action to
    `.grimoire/structure-migration-<timestamp>.json` before touching anything."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_dir = os.path.join(root, MANIFEST_DIR_REL_PATH)
    os.makedirs(manifest_dir, exist_ok=True)
    manifest_path = os.path.join(manifest_dir, f"structure-migration-{ts}.json")
    payload = {
        "timestamp": ts,
        "planned": [asdict(f) for f in findings],
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    return manifest_path


def apply_findings(root: str, findings: list) -> list:
    """Perform every mechanical remedy; return the findings left unresolved
    (report-only codes, submodule skips, import-breaking skips, collisions)."""
    manifest_path = write_manifest(root, findings)
    print(f"structure-migrate: manifest written to "
          f"{os.path.relpath(manifest_path, root)}")

    remaining = []
    for f in findings:
        if f.code == MISSING_REQUIRED:
            print(f"  SKIP (report-only): {f.path} — {f.message}")
            remaining.append(f)
            continue

        if f.code == SUBMODULE_NONSTANDARD_DIR:
            print(f"  SKIP (submodule): {f.path} — manual move required, "
                  f"see SKILL.md")
            remaining.append(f)
            continue

        if f.code == NONSTANDARD_DIR:
            old = f.path.rstrip("/")
            if not f.target:
                print(f"  SKIP (no alias target): {old}")
                remaining.append(f)
                continue
            refs = find_import_references(root, old)
            if refs:
                print(f"  SKIP (import-breaking): {old} referenced by:")
                for r in refs[:DISPLAY_REF_LIMIT]:
                    print(f"    {r}")
                if len(refs) > DISPLAY_REF_LIMIT:
                    print(f"    ... and {len(refs) - DISPLAY_REF_LIMIT} more")
                remaining.append(f)
                continue
            moved, collisions = git_mv_merge(root, old, f.target)
            if collisions:
                print(f"  PARTIAL: {old} -> {f.target} "
                      f"(collision(s): {', '.join(collisions)})")
                remaining.append(f)
            elif moved:
                print(f"  moved: {old} -> {f.target}")
            else:
                print(f"  SKIP (git mv failed): {old}")
                remaining.append(f)
            continue

        if f.code == TRACKED_OUTPUT:
            add_gitignore_and_untrack(root, f.path)
            print(f"  untracked: {f.path} (added to .gitignore, "
                  f"git rm -r --cached)")
            continue

        if f.code == VENDOR_DEST:
            ok = rewrite_vendor_dest(root, f.name, f.old, f.target)
            if ok:
                print(f"  rewritten: vendor.toml deps.{f.name}.dest -> {f.target}")
            else:
                print(f"  SKIP (rewrite failed): vendor.toml deps.{f.name}.dest")
                remaining.append(f)
            continue

        # Unknown code: never silently drop a finding.
        remaining.append(f)

    return remaining


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def format_report(findings: list) -> str:
    counts: dict = {}
    for f in findings:
        counts[f.code] = counts.get(f.code, 0) + 1
    header = f"structure-migrate — {len(findings)} finding(s)"
    if counts:
        summary = ", ".join(
            f"{n} {code.lower().replace('_', '-')}" for code, n in sorted(counts.items())
        )
        header += f": {summary}"
    lines = [header]
    for f in findings:
        lines.append(f.render())
    return "\n".join(lines)


def run_detect(root: str, rules_path: Optional[str] = None, as_json: bool = False) -> int:
    rules_path = rules_path or os.path.join(root, RULES_REL_PATH)
    try:
        rules = load_rules(rules_path)
    except ValueError as e:
        print(f"structure-migrate: ERROR: {e}")
        return 1

    if rules is None or not rules.get("structure"):
        msg = "structure-migrate: no structure declared"
        if as_json:
            print(json.dumps({"structure_declared": False, "findings": []}, indent=2))
        else:
            print(msg)
        return 0

    findings = classify_structure(root, rules)
    if as_json:
        print(json.dumps({
            "structure_declared": True,
            "finding_count": len(findings),
            "findings": [asdict(f) for f in findings],
        }, indent=2, sort_keys=True))
    else:
        print(format_report(findings))
    return 1 if findings else 0


def run_apply(root: str, rules_path: Optional[str] = None) -> int:
    rules_path = rules_path or os.path.join(root, RULES_REL_PATH)
    try:
        rules = load_rules(rules_path)
    except ValueError as e:
        print(f"structure-migrate: ERROR: {e}")
        return 1

    if rules is None or not rules.get("structure"):
        print("structure-migrate: no structure declared")
        return 0

    findings = classify_structure(root, rules)
    if not findings:
        print("structure-migrate: nothing to do (no findings).")
        return 0

    remaining = apply_findings(root, findings)
    if remaining:
        print(f"\nstructure-migrate: {len(remaining)} finding(s) left "
              f"unresolved (see above).")
        return 1
    print("\nstructure-migrate: all findings resolved.")
    return 0


# ---------------------------------------------------------------------------
# Self-test (stdlib-only, deterministic, offline — synthetic fixture trees)
# ---------------------------------------------------------------------------


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, capture_output=True)


def _build_fixture(tmp_root: str, with_import_ref: bool = False) -> None:
    """A project with every migrate-mode finding present:
      - `vendor/` (nonstandard dir, aliases -> lib/third-party) with a
        `vendor.toml` dep whose dest still points at `vendor/aura`
      - `vendor/sub` registered as a git submodule (SUBMODULE_NONSTANDARD_DIR
        wins over NONSTANDARD_DIR for that path... modeled here as `plugins/`
        aliasing to `lib/third-party`, registered whole as a submodule)
      - `dist/` tracked output
      - `tests/` missing (required, per rules)
      - optionally, a source file referencing `vendor/` (import-breaking)
    """
    _write(os.path.join(tmp_root, "src/app.py"), "import os\n")
    _write(os.path.join(tmp_root, "docs/README.md"), "# docs\n")
    _write(os.path.join(tmp_root, "vendor/aura/pkg.py"), "# vendored\n")
    _write(os.path.join(tmp_root, "plugins/thing/pkg.py"), "# submodule dep\n")
    _write(os.path.join(tmp_root, "dist/out.js"), "// build output\n")
    _write(
        os.path.join(tmp_root, "vendor.toml"),
        'schema_version = 1\n\n'
        '[deps.aura]\n'
        'repo = "acme/aura"\n'
        'version = "1.0.0"\n'
        'dest = "vendor/aura"\n',
    )
    _write(
        os.path.join(tmp_root, GITMODULES_REL_PATH),
        '[submodule "plugins/thing"]\n'
        '\tpath = plugins/thing\n'
        '\turl = https://example.invalid/thing.git\n',
    )
    if with_import_ref:
        _write(os.path.join(tmp_root, "src/loader.py"), "load('vendor/aura/pkg.py')\n")

    rules = {
        "schema-version": 1,
        "structure": {
            "required": ["src", "docs", "tests"],
            "aliases": {"vendor": "lib/third-party", "plugins": "lib/third-party"},
            "gitignored": ["dist"],
        },
        "layers": {"app": ["src/**"]},
        "allowed-edges": [],
    }
    _write(os.path.join(tmp_root, ".claude/architecture-rules.json"), json.dumps(rules))

    _git(tmp_root, "init", "-q")
    _git(tmp_root, "add", "-A")
    _git(tmp_root, "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init")


def self_test() -> int:
    import tempfile

    passed, failed = 0, 0
    lines = []

    def check(label: str, ok: bool):
        nonlocal passed, failed
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1

    # 1. Absent architecture-rules.json -> "no structure declared", exit 0.
    with tempfile.TemporaryDirectory() as tmp:
        rc = run_detect(tmp)
        check("absent rules file exits 0 (no structure declared)", rc == 0)

    # 2. Rules present but no `structure` block -> exit 0, no findings.
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, ".claude/architecture-rules.json"),
               json.dumps({"schema-version": 1, "layers": {}}))
        rc = run_detect(tmp)
        check("rules without a structure block exits 0", rc == 0)

    # 3. Full fixture: every migrate-mode code detected.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rules = load_rules(os.path.join(tmp, RULES_REL_PATH))
        findings = classify_structure(tmp, rules)
        codes = {f.code for f in findings}
        check("MISSING_REQUIRED detected (tests/ absent)", MISSING_REQUIRED in codes)
        check("NONSTANDARD_DIR detected (vendor/)",
              any(f.code == NONSTANDARD_DIR and f.path.rstrip("/") == "vendor" for f in findings))
        check("SUBMODULE_NONSTANDARD_DIR detected (plugins/ is a submodule)",
              any(f.code == SUBMODULE_NONSTANDARD_DIR and f.path.rstrip("/") == "plugins"
                  for f in findings))
        check("plugins/ is NOT also reported as a plain NONSTANDARD_DIR",
              not any(f.code == NONSTANDARD_DIR and f.path.rstrip("/") == "plugins"
                      for f in findings))
        check("TRACKED_OUTPUT detected (dist/)", TRACKED_OUTPUT in codes)
        check("VENDOR_DEST detected (deps.aura.dest under vendor/)", VENDOR_DEST in codes)

        # Shared-engine equivalence: same raw structure findings as the audit.
        raw = check_structure(tmp, rules)
        raw_codes = {_RULE_ID_TO_CODE[r.rule_id] for r in raw}
        check("detect findings are a superset of the shared engine's raw codes",
              raw_codes <= codes)

        rc = run_detect(tmp)
        check("run_detect exits 1 when findings are present", rc == 1)

        # JSON mode parses.
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_detect(tmp, as_json=True)
        payload = json.loads(buf.getvalue())
        check("--json output parses and reports structure_declared=True",
              payload.get("structure_declared") is True)
        check("--json finding_count matches findings length",
              payload.get("finding_count") == len(payload.get("findings", [])))

    # 4. --apply performs the mechanical remedies and skips what it must.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc = run_apply(tmp)
        check("run_apply returns 1 while unresolved findings remain "
              "(MISSING_REQUIRED + submodule)", rc == 1)
        check("vendor/ was git-mv'd to lib/third-party/",
              os.path.isdir(os.path.join(tmp, "lib/third-party/aura"))
              and not os.path.isdir(os.path.join(tmp, "vendor")))
        check("plugins/ (submodule) was left in place",
              os.path.isdir(os.path.join(tmp, "plugins/thing")))
        vendor_toml_text = open(os.path.join(tmp, "vendor.toml")).read()
        check("vendor.toml dest rewritten to lib/third-party/aura",
              'dest = "lib/third-party/aura"' in vendor_toml_text)
        gitignore_text = open(os.path.join(tmp, ".gitignore")).read() \
            if os.path.isfile(os.path.join(tmp, ".gitignore")) else ""
        check("dist/ appended to .gitignore", "dist/" in gitignore_text)
        tracked = subprocess.run(
            ["git", "-C", tmp, "ls-files", "dist"], capture_output=True, text=True
        ).stdout
        check("dist/ untracked from git after apply", tracked.strip() == "")
        manifests = [p for p in os.listdir(os.path.join(tmp, ".grimoire"))
                     if p.startswith("structure-migration-")]
        check("manifest written to .grimoire/", len(manifests) == 1)

        # Idempotency: re-running detect no longer reports the resolved codes.
        rules2 = load_rules(os.path.join(tmp, RULES_REL_PATH))
        findings2 = classify_structure(tmp, rules2)
        codes2 = {f.code for f in findings2}
        check("NONSTANDARD_DIR(vendor) no longer present after apply",
              not any(f.code == NONSTANDARD_DIR and f.path.rstrip("/") == "vendor"
                      for f in findings2))
        check("VENDOR_DEST no longer present after apply", VENDOR_DEST not in codes2)
        check("TRACKED_OUTPUT no longer present after apply", TRACKED_OUTPUT not in codes2)
        check("SUBMODULE_NONSTANDARD_DIR(plugins) still present (never auto-moved)",
              SUBMODULE_NONSTANDARD_DIR in codes2)
        check("MISSING_REQUIRED(tests) still present (report-only forever)",
              MISSING_REQUIRED in codes2)

        # Second --apply is a no-op for everything that already resolved.
        rc2 = run_apply(tmp)
        check("second --apply still exits 1 (submodule/missing-required persist)", rc2 == 1)
        check("second --apply did not re-move an already-moved dir",
              os.path.isdir(os.path.join(tmp, "lib/third-party/aura")))

    # 5. Import-breaking move is flagged and skipped, never performed.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp, with_import_ref=True)
        rc = run_apply(tmp)
        check("import-breaking apply still reports unresolved findings", rc == 1)
        check("vendor/ was NOT moved (source references it)",
              os.path.isdir(os.path.join(tmp, "vendor/aura")))
        findings = classify_structure(
            tmp, load_rules(os.path.join(tmp, RULES_REL_PATH))
        )
        check("NONSTANDARD_DIR(vendor) finding persists (import-breaking skip)",
              any(f.code == NONSTANDARD_DIR and f.path.rstrip("/") == "vendor"
                  for f in findings))

    # 6. A conformant tree (no structure drift) reports zero findings.
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "src/app.py"), "import os\n")
        _write(os.path.join(tmp, "docs/README.md"), "# docs\n")
        _write(os.path.join(tmp, "tests/test_app.py"), "import unittest\n")
        rules = {
            "schema-version": 1,
            "structure": {"required": ["src", "docs", "tests"],
                           "aliases": {"vendor": "lib/third-party"}},
            "layers": {},
        }
        _write(os.path.join(tmp, RULES_REL_PATH), json.dumps(rules))
        _git(tmp, "init", "-q")
        _git(tmp, "add", "-A")
        _git(tmp, "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
        rc = run_detect(tmp)
        check("conformant tree exits 0", rc == 0)
        rc_apply = run_apply(tmp)
        check("--apply on a conformant tree is a no-op (exit 0)", rc_apply == 0)

    print(f"structure-migrate self-test: {passed} passed, {failed} failed.")
    for ln in lines:
        print(ln)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Structure-migrate: shared detection engine + scripted "
                    "remedies (grm-structure-migrate). See "
                    "docs/grimoire/design/file-structure-standard-design.md."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true",
                       help="Run against built-in offline fixtures (no network calls).")
    mode.add_argument("--root", metavar="DIR",
                       help="Repo root to migrate (expects .claude/architecture-rules.json).")
    parser.add_argument("--rules", metavar="PATH",
                         help="Override path to architecture-rules.json "
                              "(default: <root>/.claude/architecture-rules.json).")
    parser.add_argument("--apply", action="store_true",
                         help="Perform the mechanical remedies. Default is "
                              "detect-only (read-only, nothing moves).")
    parser.add_argument("--json", action="store_true",
                         help="Emit detect-mode findings as JSON instead of the "
                              "human table (ignored with --apply).")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.apply:
        return run_apply(args.root, rules_path=args.rules)
    return run_detect(args.root, rules_path=args.rules, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
