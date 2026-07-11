#!/usr/bin/env python3
"""architecture_fitness.py — deterministic architecture + structure fitness engine.

Implements the `grm-architecture-audit` skill's described algorithm (design:
docs/grimoire/design/architecture-fitness-design.md) as a real, stdlib-only
Python module instead of agent-derived prose (#212): regex-based import
extraction per language, directory→layer resolution against
`.claude/architecture-rules.json`'s `layers` block, edge evaluation against
`allowed-edges` + `forbidden-imports`, cycle detection over the resulting
layer/module graph, and optional `structure`-block conformance (required dirs,
nonstandard-name aliases, gitignored-but-tracked build output).

Shared module: `grm-code-health` imports `build_import_graph()` /
`module_coupling()` from this file for its Section B module-coupling metrics
(same scan, not a second implementation — see that skill's SKILL.md).
`grm-structure-migrate` imports `check_structure()` from this file for its
migrate-mode detection engine (#320), extending it with the migrate-only
`VENDOR_DEST` / `SUBMODULE_NONSTANDARD_DIR` findings rather than re-deriving
`structure`-block classification a second time.

Degrades gracefully: an absent `.claude/architecture-rules.json` is reported as
a visible WARN with a pointer to adopt one of the per-family starter rulesets
(`.claude/quick-start-templates/{service,web,gui,lib}/files/.claude/architecture-rules.json`)
or `.claude/architecture-rules.example.json`; the process still exits 0 (never
fails a project that has not opted in — the WARN just replaces silence with a
visible nudge, #314). A project that has deliberately declined may set
`"opt_out": true` (+ optional `"opt_out-reason"`) in a present rules file — this
is reported as an explicit, surfaced decision (distinct from the absent-file
WARN) and also exits 0 with no fitness checks run.

Usage:
    architecture_fitness.py --root DIR [--rules PATH] [--gate] [--json]
    architecture_fitness.py --self-test

Exit codes:
    0  no rules declared, or no violations, or violations found but not gating
       (report-only default, or --gate with the audit-gate dial != block)
    1  --gate is active, the resolved severity is `block`, and >=1 violation
       exists (or --self-test failed)

Python stdlib only (#212 constraint).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import Optional

RULES_REL_PATH = ".claude/architecture-rules.json"
CONFIG_REL_PATH = ".claude/grimoire-config.json"

# Directories never descended into while scanning for source files.
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", "vendor",
}

# Language -> (file globs, import-line regex). Mirrors the table in
# grm-architecture-audit/SKILL.md Step 2. Kept to the languages this repo (and
# the example schema) actually has occasion to use — not over-engineered for
# languages absent from Grimoire-managed projects.
LANGUAGE_SCANS = {
    "python": (
        (".py",),
        re.compile(r"^\s*(?:from\s+([.\w]+)\s+import|import\s+([.\w]+))"),
    ),
    "js/ts": (
        (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
        re.compile(
            r"""^\s*(?:import\s+(?:[\w*{}\s,]+\s+from\s+)?["']([^"']+)["']"""
            r"""|.*\brequire\(\s*["']([^"']+)["']\s*\))"""
        ),
    ),
}


@dataclass
class Finding:
    """One fitness-function violation. Sorts deterministically by (path, line, rule_id)."""

    path: str
    line: int
    rule_id: str
    severity: str
    message: str

    def sort_key(self):
        return (self.path, self.line, self.rule_id)

    def render(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {loc}  {self.rule_id} ({self.severity})  {self.message}"


@dataclass
class ImportEdge:
    """One resolved import: a source file (and its layer) importing a target
    module path (and its resolved layer, if any)."""

    src_path: str
    line: int
    raw_target: str
    src_layer: Optional[str]
    dst_layer: Optional[str] = None


# --------------------------------------------------------------------------
# Rules loading
# --------------------------------------------------------------------------


def load_rules(rules_path: str) -> Optional[dict]:
    """Load + minimally validate architecture-rules.json. Returns None if
    absent (caller reports 'no rules declared' and exits clean). Raises
    ValueError on a malformed (but present) file — a present, broken ruleset
    is a real error, not silent opt-out."""
    if not os.path.isfile(rules_path):
        return None
    with open(rules_path, encoding="utf-8") as fh:
        try:
            rules = json.load(fh)
        except json.JSONDecodeError as e:
            raise ValueError(f"{rules_path}: invalid JSON ({e})") from e
    if not isinstance(rules, dict):
        raise ValueError(f"{rules_path}: root must be a JSON object")
    layers = rules.get("layers", {})
    if not isinstance(layers, dict):
        raise ValueError(f"{rules_path}: 'layers' must be an object")
    edges = rules.get("allowed-edges", [])
    if not isinstance(edges, list):
        raise ValueError(f"{rules_path}: 'allowed-edges' must be a list")
    return rules


# --------------------------------------------------------------------------
# Layer resolution
# --------------------------------------------------------------------------


def _norm(path: str) -> str:
    return path.replace(os.sep, "/").lstrip("./")


def _glob_to_regex(glob: str) -> re.Pattern:
    """Translate a `layers` glob (`src/ui/**`, `docs/**`, `*.py`) into a regex
    matching a repo-relative path. `**` matches any number of path segments
    (including zero, so `src/ui/**` also matches `src/ui/cart.py` directly);
    `*` matches within one segment; everything else is matched literally."""
    pattern = re.escape(glob).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.compile("^" + pattern + "$")


def resolve_layer(rel_path: str, layers: dict) -> Optional[str]:
    """Resolve a repo-relative path to a layer name by matching the layers'
    globs. `**` matches any depth (including the file directly under the
    prefix); `*` matches within one path segment. First matching layer wins in
    declaration order — declare narrower globs first when overlap is
    possible."""
    rel_path = _norm(rel_path)
    for name, globs in layers.items():
        for g in globs:
            g = _norm(g)
            if _glob_to_regex(g).match(rel_path):
                return name
    return None


# --------------------------------------------------------------------------
# Source discovery + import extraction
# --------------------------------------------------------------------------


def discover_source_files(root: str, layers: dict) -> list:
    """Walk `root`, returning repo-relative paths of files that fall under any
    declared layer glob and match a known language extension. Skips
    SKIP_DIRS. Deterministic order (sorted)."""
    exts = tuple(e for exts, _ in LANGUAGE_SCANS.values() for e in exts)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and (d == ".claude" or not d.startswith("."))
        )
        for fn in sorted(filenames):
            if not fn.endswith(exts):
                continue
            full = os.path.join(dirpath, fn)
            rel = _norm(os.path.relpath(full, root))
            if resolve_layer(rel, layers) is not None:
                out.append(rel)
    return sorted(out)


def _lang_for(rel_path: str):
    for lang, (exts, rx) in LANGUAGE_SCANS.items():
        if rel_path.endswith(exts):
            return lang, rx
    return None, None


def extract_imports(root: str, rel_path: str) -> list:
    """Return [(line_no, raw_target)] for every import statement in the file,
    via the language-appropriate regex (Step 2 of the design). Best-effort:
    unreadable files yield no imports rather than raising."""
    _, rx = _lang_for(rel_path)
    if rx is None:
        return []
    full = os.path.join(root, rel_path)
    results = []
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                m = rx.match(line)
                if not m:
                    continue
                target = next((g for g in m.groups() if g), None)
                if target:
                    results.append((lineno, target))
    except OSError:
        return []
    return results


def _module_path_for_import(src_rel_path: str, raw_target: str, layers: dict) -> Optional[str]:
    """Best-effort resolution of an import target back to a repo-relative path.
    Handles three first-party shapes:

      1. Python relative imports (`.foo`, `..bar.baz`) — resolved against the
         source file's directory.
      2. JS/TS relative specifiers (`./foo`, `../bar`) — same, path-style.
      3. Python absolute *dotted* module paths whose leading component matches
         a top-level directory declared in `layers` (e.g. `src.services.x`
         when a layer glob starts with `src/...`) — resolved as
         `src/services/x.py`, since this is exactly the shape a project's own
         first-party package imports take.

    Anything else (a bare third-party/stdlib module, or a dotted path whose
    root isn't a declared layer root) resolves to None and is excluded from
    edge/cycle evaluation — fitness functions apply to *this project's own*
    module graph, not its dependencies.
    """
    if raw_target.startswith("."):
        # Python relative import: resolve against the source file's directory.
        base = os.path.dirname(src_rel_path)
        depth = len(raw_target) - len(raw_target.lstrip("."))
        rest = raw_target.lstrip(".").replace(".", "/")
        for _ in range(depth - 1):
            base = os.path.dirname(base)
        candidate = os.path.join(base, rest) if rest else base
        return _norm(candidate)
    if raw_target.startswith("./") or raw_target.startswith("../"):
        base = os.path.dirname(src_rel_path)
        return _norm(os.path.normpath(os.path.join(base, raw_target)))
    if "." in raw_target:
        # Candidate absolute dotted module path (e.g. "src.services.checkout").
        # Only treat it as first-party if its root component matches the root
        # of some declared layer glob (so "os.path"-style stdlib dotted paths
        # never spuriously resolve).
        root = raw_target.split(".", 1)[0]
        declared_roots = {g.split("/", 1)[0] for globs in layers.values() for g in globs}
        if root in declared_roots:
            return _norm(raw_target.replace(".", "/"))
    return None  # bare / third-party module: not resolvable to a repo path


def build_import_graph(root: str, rules: dict) -> list:
    """Scan every source file under a declared layer and return the sorted
    list of ImportEdge records (src layer resolved; dst layer resolved when
    the import target maps back into a declared layer; otherwise None and
    excluded from edge/cycle evaluation — only first-party, in-repo imports
    are fitness-function subjects)."""
    layers = rules.get("layers", {})
    edges: list = []
    for rel_path in discover_source_files(root, layers):
        src_layer = resolve_layer(rel_path, layers)
        for lineno, raw_target in extract_imports(root, rel_path):
            module_path = _module_path_for_import(rel_path, raw_target, layers)
            dst_layer = None
            if module_path:
                dst_layer = resolve_layer(module_path, layers) or resolve_layer(
                    module_path + ".py", layers)
            edges.append(ImportEdge(rel_path, lineno, raw_target, src_layer, dst_layer))
    edges.sort(key=lambda e: (e.src_path, e.line, e.raw_target))
    return edges


# --------------------------------------------------------------------------
# Fitness functions
# --------------------------------------------------------------------------


def check_disallowed_edges(edges: list, rules: dict) -> list:
    allowed = {tuple(pair) for pair in rules.get("allowed-edges", [])}
    findings = []
    for e in edges:
        if e.src_layer and e.dst_layer and e.src_layer != e.dst_layer:
            if (e.src_layer, e.dst_layer) not in allowed:
                findings.append(
                    Finding(
                        e.src_path, e.line, "allowed-edge", "error",
                        f"{e.src_layer} → {e.dst_layer} not allowed (import {e.raw_target!r})",
                    )
                )
    return findings


def check_forbidden_imports(edges: list, rules: dict) -> list:
    findings = []
    for rule in rules.get("forbidden-imports", []):
        rule_id = rule.get("id", "forbidden-import")
        pattern = rule.get("pattern")
        from_layer = rule.get("from")
        severity = rule.get("severity", "warn")
        if not pattern:
            continue
        rx = re.compile(pattern)
        for e in edges:
            if from_layer and e.src_layer != from_layer:
                continue
            if rx.search(e.raw_target):
                findings.append(
                    Finding(
                        e.src_path, e.line, rule_id, severity,
                        f"import {e.raw_target!r} matches forbidden pattern {pattern!r}",
                    )
                )
    return findings


def check_cycles(edges: list, rules: dict) -> list:
    """Build the layer-level edge set from actual imports and report any
    cycle. A layer graph with a cycle is reported once per distinct cycle
    (smallest representative), sorted for determinism."""
    if not rules.get("forbid-cycles"):
        return []
    graph: dict = {}
    for e in edges:
        if e.src_layer and e.dst_layer and e.src_layer != e.dst_layer:
            graph.setdefault(e.src_layer, set()).add(e.dst_layer)

    cycles = _find_cycles(graph)
    findings = []
    for cycle in cycles:
        label = " → ".join(cycle + [cycle[0]])
        findings.append(
            Finding("(layer-graph)", 0, "no-cycles", "error", f"import cycle: {label}")
        )
    return findings


def _find_cycles(graph: dict) -> list:
    """Depth-first search over the layer graph; returns sorted, deduplicated
    minimal cycles (as ordered lists of layer names, lexicographically-first
    rotation) found via back-edges."""
    found = set()
    for start in sorted(graph):
        stack = [(start, [start])]
        visited_paths = set()
        while stack:
            node, path = stack.pop()
            for neighbor in sorted(graph.get(node, ())):
                if neighbor == start:
                    # normalize rotation: start at lexicographically smallest node
                    cyc = path[:]
                    min_idx = cyc.index(min(cyc))
                    normalized = tuple(cyc[min_idx:] + cyc[:min_idx])
                    found.add(normalized)
                elif neighbor not in path:
                    key = tuple(path + [neighbor])
                    if key not in visited_paths and len(path) < len(graph) + 1:
                        visited_paths.add(key)
                        stack.append((neighbor, path + [neighbor]))
    return [list(c) for c in sorted(found)]


# --------------------------------------------------------------------------
# Structure conformance (Step 3a)
# --------------------------------------------------------------------------


def check_structure(root: str, rules: dict) -> list:
    structure = rules.get("structure")
    if not structure:
        return []
    findings = []
    try:
        top_level = sorted(
            d for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d)) and not d.startswith(".git")
        )
    except OSError:
        top_level = []
    top_set = set(top_level)

    for required in sorted(structure.get("required", [])):
        if required not in top_set:
            findings.append(
                Finding(required, 0, "structure-required", "error",
                        "missing required directory")
            )

    aliases = structure.get("aliases", {})
    for dirname in top_level:
        if dirname in aliases:
            findings.append(
                Finding(f"{dirname}/", 0, "structure-nonstandard", "warn",
                        f"rename {dirname}/ → {aliases[dirname]}/")
            )

    gitignored = structure.get("gitignored", [])
    if gitignored:
        tracked = _git_tracked_top_dirs(root)
        for dirname in sorted(gitignored):
            if dirname in tracked:
                findings.append(
                    Finding(f"{dirname}/", 0, "structure-tracked-output", "warn",
                            f"{dirname}/ is build output and must not be committed")
                )
    return findings


def _git_tracked_top_dirs(root: str) -> set:
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    tops = set()
    for line in proc.stdout.splitlines():
        parts = line.split("/", 1)
        if len(parts) == 2:
            tops.add(parts[0])
    return tops


# --------------------------------------------------------------------------
# Module coupling (shared with grm-code-health Section B)
# --------------------------------------------------------------------------


def module_coupling(edges: list) -> dict:
    """Per-layer afferent (Ca) / efferent (Ce) coupling + instability
    (I = Ce/(Ca+Ce)) computed from the same import-graph scan
    `grm-architecture-audit` uses (grm-code-health/SKILL.md Section B). Only
    cross-layer edges count; a layer that imports only itself has Ce=0 for
    that edge. Deterministic: sorted by layer name."""
    ca: dict = {}
    ce: dict = {}
    layers_seen: set = set()
    for e in edges:
        if e.src_layer:
            layers_seen.add(e.src_layer)
        if e.dst_layer:
            layers_seen.add(e.dst_layer)
        if e.src_layer and e.dst_layer and e.src_layer != e.dst_layer:
            ce[e.src_layer] = ce.get(e.src_layer, 0) + 1
            ca[e.dst_layer] = ca.get(e.dst_layer, 0) + 1

    out = {}
    for layer in sorted(layers_seen):
        c_a = ca.get(layer, 0)
        c_e = ce.get(layer, 0)
        instability = (c_e / (c_a + c_e)) if (c_a + c_e) else 0.0
        out[layer] = {"Ca": c_a, "Ce": c_e, "instability": round(instability, 3)}
    return out


# --------------------------------------------------------------------------
# Gate dial resolution
# --------------------------------------------------------------------------


def resolve_gate_severity(root: str) -> str:
    """Read the live `code-quality.audit-gate` dial from
    .claude/grimoire-config.json ({off,warn,block}); default 'warn' if the
    config or dial is absent (matches config_validate.py's own additive
    default)."""
    cfg_path = os.path.join(root, CONFIG_REL_PATH)
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return "warn"
    dial = cfg.get("code-quality", {}).get("audit-gate", {})
    if isinstance(dial, dict):
        return dial.get("value", "warn")
    return "warn"


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


NO_RULES_NOTICE = (
    "no rules declared — architecture fitness is not enforced for this "
    "project. Adopt a starter ruleset: copy the per-family template shipped "
    "by grm-quick-start-template "
    "(.claude/quick-start-templates/{service,web,gui,lib}/files/.claude/architecture-rules.json) "
    "or .claude/architecture-rules.example.json to "
    ".claude/architecture-rules.json and adapt it, or explicitly decline by "
    "adding \"opt_out\": true (+ \"opt_out-reason\") to a committed rules file."
)


def run_audit(root: str, rules_path: Optional[str] = None, gate: bool = False,
              as_json: bool = False) -> int:
    rules_path = rules_path or os.path.join(root, RULES_REL_PATH)
    try:
        rules = load_rules(rules_path)
    except ValueError as e:
        print(f"architecture-audit: ERROR: {e}")
        return 1

    if rules is None:
        if as_json:
            print(json.dumps({
                "rules_declared": False,
                "opt_out": False,
                "notice": NO_RULES_NOTICE,
                "findings": [],
            }, indent=2))
        else:
            print(f"architecture-audit: WARN — {NO_RULES_NOTICE}")
        return 0

    if rules.get("opt_out"):
        reason = rules.get("opt_out-reason", "")
        if as_json:
            print(json.dumps({
                "rules_declared": True,
                "opt_out": True,
                "opt_out_reason": reason,
                "findings": [],
            }, indent=2))
        else:
            suffix = f" (reason: {reason})" if reason else ""
            print(f"architecture-audit: rules present but explicitly opted out{suffix}")
        return 0

    edges = build_import_graph(root, rules)
    findings: list = []
    findings.extend(check_disallowed_edges(edges, rules))
    findings.extend(check_forbidden_imports(edges, rules))
    findings.extend(check_cycles(edges, rules))
    findings.extend(check_structure(root, rules))
    findings.sort(key=lambda f: f.sort_key())

    counts: dict = {}
    for f in findings:
        counts[f.rule_id] = counts.get(f.rule_id, 0) + 1

    if as_json:
        print(json.dumps({
            "rules_declared": True,
            "violation_count": len(findings),
            "by_rule": counts,
            "findings": [asdict(f) for f in findings],
        }, indent=2, sort_keys=True))
    else:
        summary_parts = ", ".join(f"{n} {rid}" for rid, n in sorted(counts.items()))
        header = f"architecture-audit — {len(findings)} violation(s)"
        if summary_parts:
            header += f": {summary_parts}"
        print(header)
        for f in findings:
            print(f.render())

    if not gate:
        return 0

    severity = resolve_gate_severity(root)
    if severity == "block" and findings:
        return 1
    return 0


# --------------------------------------------------------------------------
# Self-test (stdlib-only, deterministic, offline — synthetic fixture trees)
# --------------------------------------------------------------------------


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _build_fixture(tmp_root: str) -> None:
    """A small synthetic project: presentation/application/domain/persistence
    layers (mirrors architecture-rules.example.json), with:
      - one clean allowed edge (presentation -> application)
      - one disallowed edge (presentation -> persistence, direct db import)
      - one forbidden-import hit (a `prisma` import from presentation)
      - one cycle (application -> domain -> application)
      - a nonstandard `vendor/` top-level dir + a tracked `dist/` file
    """
    _write(os.path.join(tmp_root, "src/ui/cart.py"),
           "from src.services.checkout import total\n"
           "from src.db.conn import get_conn\n"
           "import prisma\n")
    _write(os.path.join(tmp_root, "src/services/checkout.py"),
           "from src.domain.pricing import compute\n")
    _write(os.path.join(tmp_root, "src/domain/pricing.py"),
           "from src.services.checkout import helper\n")  # domain -> application: cycle
    _write(os.path.join(tmp_root, "src/db/conn.py"), "import sqlite3\n")
    os.makedirs(os.path.join(tmp_root, "vendor"), exist_ok=True)
    _write(os.path.join(tmp_root, "vendor/pkg.py"), "# vendored\n")
    _write(os.path.join(tmp_root, "tests/test_x.py"), "import unittest\n")
    _write(os.path.join(tmp_root, "docs/README.md"), "# docs\n")

    rules = {
        "schema-version": 1,
        "structure": {
            "required": ["src", "tests", "docs"],
            "aliases": {"vendor": "lib/third-party"},
            "gitignored": ["dist"],
        },
        "layers": {
            "presentation": ["src/ui/**"],
            "application": ["src/services/**"],
            "domain": ["src/domain/**"],
            "persistence": ["src/db/**"],
        },
        "allowed-edges": [
            ["presentation", "application"],
            ["application", "domain"],
            ["persistence", "domain"],
        ],
        "forbidden-imports": [
            {"id": "no-sql-in-view", "from": "presentation", "pattern": "prisma|sql",
             "severity": "error"},
        ],
        "forbid-cycles": True,
    }
    _write(os.path.join(tmp_root, ".claude/architecture-rules.json"), json.dumps(rules))
    subprocess.run(["git", "init", "-q"], cwd=tmp_root)
    subprocess.run(["git", "add", "-A"], cwd=tmp_root)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=tmp_root,
    )


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

    # 1. Absent rules file -> visible WARN pointer, exit 0.
    with tempfile.TemporaryDirectory() as tmp:
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_audit(tmp)
        check("absent architecture-rules.json exits 0 (no rules declared)", rc == 0)
        out = buf.getvalue()
        check("absent rules prints a visible WARN", "WARN" in out)
        check("absent rules points at adopting a starter ruleset",
              "architecture-rules.json" in out and "opt_out" in out)

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc2 = run_audit(tmp, as_json=True)
        check("absent rules --json exits 0", rc2 == 0)
        payload = json.loads(buf2.getvalue())
        check("absent rules --json reports rules_declared=False",
              payload.get("rules_declared") is False)
        check("absent rules --json carries a notice pointer",
              bool(payload.get("notice")))

    # 1b. Present rules file with explicit opt_out -> surfaced, not WARN, exit 0.
    with tempfile.TemporaryDirectory() as tmp:
        import io
        import contextlib

        _write(os.path.join(tmp, RULES_REL_PATH),
               json.dumps({"schema-version": 1, "opt_out": True,
                           "opt_out-reason": "single-file script, no layering"}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_audit(tmp)
        check("opt_out rules file exits 0", rc == 0)
        out = buf.getvalue()
        check("opt_out is surfaced (not silent, not WARN)",
              "opted out" in out and "WARN" not in out)
        check("opt_out reason is surfaced", "single-file script" in out)

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            run_audit(tmp, as_json=True)
        payload = json.loads(buf2.getvalue())
        check("opt_out --json reports opt_out=True", payload.get("opt_out") is True)
        check("opt_out --json carries the reason",
              payload.get("opt_out_reason") == "single-file script, no layering")

    # 2. Malformed rules file -> error, exit 1.
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, ".claude/architecture-rules.json"), "{not json")
        rc = run_audit(tmp)
        check("malformed architecture-rules.json exits 1", rc == 1)

    # 3. Full fixture: layer resolution.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rules = load_rules(os.path.join(tmp, RULES_REL_PATH))
        check("rules load from fixture", rules is not None)
        check("presentation layer resolves",
              resolve_layer("src/ui/cart.py", rules["layers"]) == "presentation")
        check("non-layer file resolves to None",
              resolve_layer("tests/test_x.py", rules["layers"]) is None)

        edges = build_import_graph(tmp, rules)
        check("import graph is non-empty", len(edges) > 0)
        check("import graph is sorted/deterministic",
              edges == sorted(edges, key=lambda e: (e.src_path, e.line, e.raw_target)))

        disallowed = check_disallowed_edges(edges, rules)
        check("disallowed presentation->persistence edge detected",
              any(f.rule_id == "allowed-edge" and "persistence" in f.message for f in disallowed))

        forbidden = check_forbidden_imports(edges, rules)
        check("forbidden prisma import detected",
              any(f.rule_id == "no-sql-in-view" for f in forbidden))

        cycles = check_cycles(edges, rules)
        check("application<->domain cycle detected", len(cycles) >= 1)

        structure = check_structure(tmp, rules)
        check("nonstandard vendor/ flagged",
              any(f.rule_id == "structure-nonstandard" for f in structure))
        check("required dirs (src/tests/docs) all present -> no structure-required finding",
              not any(f.rule_id == "structure-required" for f in structure))

        rc = run_audit(tmp)
        check("run_audit on violating fixture still exits 0 without --gate", rc == 0)
        rc_gate = run_audit(tmp, gate=True)
        # default dial (no grimoire-config.json) resolves to 'warn' -> exit 0
        check("run_audit --gate with no config (default warn) exits 0", rc_gate == 0)

        coupling = module_coupling(edges)
        check("module_coupling reports the presentation layer",
              "presentation" in coupling and coupling["presentation"]["Ce"] >= 1)
        check("module_coupling instability is within [0,1]",
              all(0.0 <= v["instability"] <= 1.0 for v in coupling.values()))

    # 4. --gate with a block dial + violations -> exit 1.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        _write(os.path.join(tmp, CONFIG_REL_PATH),
               json.dumps({"code-quality": {"audit-gate": {"value": "block"}}}))
        rc = run_audit(tmp, gate=True)
        check("run_audit --gate with block dial + violations exits 1", rc == 1)

    # 5. Clean project (no violations) -> exit 0 even with --gate=block.
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "src/domain/pricing.py"), "import math\n")
        _write(os.path.join(tmp, "tests/test_x.py"), "import unittest\n")
        _write(os.path.join(tmp, "docs/README.md"), "# docs\n")
        rules = {
            "schema-version": 1,
            "structure": {"required": ["src", "tests", "docs"]},
            "layers": {"domain": ["src/domain/**"]},
            "allowed-edges": [],
            "forbid-cycles": True,
        }
        _write(os.path.join(tmp, RULES_REL_PATH), json.dumps(rules))
        _write(os.path.join(tmp, CONFIG_REL_PATH),
               json.dumps({"code-quality": {"audit-gate": {"value": "block"}}}))
        rc = run_audit(tmp, gate=True)
        check("clean project exits 0 even with gate=block", rc == 0)

    # 6. JSON output mode is valid JSON and carries the violation count.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_audit(tmp, as_json=True)
        try:
            payload = json.loads(buf.getvalue())
            check("--json output parses as JSON", True)
            check("--json output reports rules_declared=True", payload.get("rules_declared") is True)
            check("--json violation_count matches findings length",
                  payload.get("violation_count") == len(payload.get("findings", [])))
        except json.JSONDecodeError:
            check("--json output parses as JSON", False)

    print(f"architecture-fitness self-test: {passed} passed, {failed} failed.")
    for ln in lines:
        print(ln)
    return 1 if failed else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic architecture + structure fitness engine "
                    "(grm-architecture-audit). See "
                    "docs/grimoire/design/architecture-fitness-design.md."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true",
                       help="Run against built-in offline fixtures (no network calls).")
    mode.add_argument("--root", metavar="DIR",
                       help="Repo root to audit (expects .claude/architecture-rules.json).")
    parser.add_argument("--rules", metavar="PATH",
                         help="Override path to architecture-rules.json "
                              "(default: <root>/.claude/architecture-rules.json).")
    parser.add_argument("--gate", action="store_true",
                         help="Escalate per the live code-quality.audit-gate dial "
                              "(block -> nonzero exit on any violation). Read-only report "
                              "otherwise.")
    parser.add_argument("--json", action="store_true",
                         help="Emit findings as JSON instead of the human table.")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    return run_audit(args.root, rules_path=args.rules, gate=args.gate, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
