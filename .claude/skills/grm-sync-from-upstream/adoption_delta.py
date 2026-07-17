#!/usr/bin/env python3
"""adoption_delta.py — compute the F2 feature-manifest adoption delta (#396).

Replaces the old F2 step ("an agent reads the ENTIRE feature-manifest.md
table, ~23K tokens, to work out what a syncing project hasn't adopted yet")
with a script. Parses `feature-manifest.md`'s table, takes a target project's
`framework-version`, and for every row where `introduced-in` is newer, RUNS
the row's own `detect` predicate against that project. Only rows that are (a)
newer than the project's framework-version AND (b) fail their own `detect`
check are emitted — i.e. genuinely not-yet-adopted. Up-to-date and
already-adopted rows are excluded entirely, which is the whole token-savings
point: an agent now pays only for the undecided rows.

`detect` predicates are hand-written prose, not a formal DSL (see the manifest
header). This module implements a best-effort predicate executor covering the
shapes that actually occur in the manifest today:

  - "`<path>` exists" / "the `<name>` skill dir exists (`<path>` [with
    `<file>` [+ `<file2>` ...]])"             -> EXISTS / EXISTS_ALL
  - "`<path>` contains `<literal>`" / "`<path>` has|defines|contains-a-
    top-level `<name>` [block]" / "Check `<path>` for a `<name>` block" /
    "`<path>`'s `<attr>` contains `<literal>`" -> CONTAINS (best-effort
    substring check, including config/JSON-block-presence approximations)
  - "`<dir>` contains only `<prefix>`-prefixed ..." -> CONTAINS_ONLY
  - "no `<path>`" (inside a "contains no ..." clause) -> NOT_EXISTS
  - "`<cmd --self-test>` ... passes"          -> RUN_SELFTEST (shells out)

Atoms combine via AND / OR / parentheses (a small recursive-descent boolean
parser over the atoms found in the row's `detect` text — connectors and
parens are the literal words "AND"/"OR" and "(", ")" that the manifest already
uses). A row whose `detect` text doesn't reduce to at least one atom is
UNPARSEABLE: rather than silently guessing, it is conservatively treated as
"not detected" (i.e. included in the delta, flagged `detect_status:
"unparseable"`) so a human/agent double-checks it by hand — the failure mode
of a false "already adopted" is worse than one extra row in the output.

This is a documented, best-effort interpreter for the predicate shapes present
today, not a general prose-understanding engine — see
docs/grimoire/design/token-efficiency-design.md §Adoption-delta script for the
full grammar and its known limits.

Usage:
  adoption_delta.py [--manifest PATH] [--project-root PATH]
                     [--framework-version VX.Y] [--format json|table]
                     [--self-test]

Exit: 0 on success (including an empty delta and a clean --self-test), 1 on a
malformed manifest (fails loudly — never silently skips or crashes
uninformatively), 1 on a failed --self-test.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TABLE_HEADER_PREFIX = "| feature-id |"
COLUMNS = ["feature-id", "introduced-in", "summary", "detect", "adopt", "migrate"]
REQUIRED_NONEMPTY = ("feature-id", "introduced-in", "detect")


class ManifestError(Exception):
    """A structurally malformed manifest row/file. Always surfaced loudly —
    never caught and silently downgraded to a skip."""


# --------------------------------------------------------------------------
# Manifest parsing
# --------------------------------------------------------------------------

@dataclass
class FeatureRow:
    feature_id: str
    introduced_in: str
    summary: str
    detect: str
    adopt: str
    migrate: str
    line_no: int


def _version_key(v: str) -> tuple:
    """Parse 'v3.94' / '3.94' / '`v1.21`' into a comparable (major, minor) tuple."""
    v = v.strip().strip("`").strip()
    m = re.match(r"^v?(\d+)\.(\d+)$", v)
    if not m:
        raise ManifestError(f"unparseable version string: {v!r}")
    return (int(m.group(1)), int(m.group(2)))


def _split_row(line: str, line_no: int) -> list:
    """Split one markdown table row on '|' pipes.

    Backtick-aware and backslash-aware, matching how GFM actually renders
    these tables: a pipe inside a backtick code span (e.g. a cell containing
    `` `UPSTREAM_CHANNEL=stable|beta` ``) is parsed as part of the code span,
    not a column delimiter — GFM resolves inline code spans before splitting
    cells. A `\\|` outside a code span is the escape-delimiter mechanism and
    unescapes to a literal `|`.
    """
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        raise ManifestError(f"line {line_no}: not a well-formed table row: {stripped[:80]!r}")
    body = stripped[1:-1]

    parts, current, in_backtick, i = [], [], False, 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] == "|" and not in_backtick:
            current.append("|")
            i += 2
            continue
        if ch == "`":
            in_backtick = not in_backtick
            current.append(ch)
            i += 1
            continue
        if ch == "|" and not in_backtick:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    if in_backtick:
        raise ManifestError(f"line {line_no}: unbalanced backtick in row: {stripped[:80]!r}")
    return [p.strip() for p in parts]


def parse_manifest(path: str) -> list:
    """Parse the manifest's feature table. Raises ManifestError loudly on any
    structurally malformed row (wrong column count, empty required field, or
    an unparseable `introduced-in` version) — never silently skips a bad row."""
    text = Path(path).read_text()
    lines = text.splitlines()
    rows = []
    in_table = False
    for i, line in enumerate(lines, start=1):
        if line.startswith(TABLE_HEADER_PREFIX):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.strip():
            continue
        if line.lstrip().startswith("|---"):
            continue
        if not line.lstrip().startswith("|"):
            break  # table ended
        cells = _split_row(line, i)
        if len(cells) != len(COLUMNS):
            raise ManifestError(
                f"{path}:{i}: expected {len(COLUMNS)} columns "
                f"({', '.join(COLUMNS)}), got {len(cells)}: {line[:160]!r}"
            )
        row = dict(zip(COLUMNS, cells))
        for name in REQUIRED_NONEMPTY:
            if not row[name]:
                raise ManifestError(f"{path}:{i}: empty required field {name!r}")
        _version_key(row["introduced-in"])  # validates eagerly; raises loudly if bad
        rows.append(FeatureRow(
            feature_id=row["feature-id"].strip("`"),
            introduced_in=row["introduced-in"],
            summary=row["summary"],
            detect=row["detect"],
            adopt=row["adopt"],
            migrate=row["migrate"],
            line_no=i,
        ))
    if not rows:
        raise ManifestError(f"{path}: no table rows found (missing header {TABLE_HEADER_PREFIX!r}?)")
    return rows


# --------------------------------------------------------------------------
# Detect-predicate atom extraction
# --------------------------------------------------------------------------
# Atom kinds and their args:
#   ("exists", path)
#   ("exists_all", (path, path, ...))   -- all must exist
#   ("contains", path, literal)
#   ("not_exists", path)
#   ("contains_only", dir, prefix)
#   ("selftest", cmd)

# A bare filename with *some* extension (1-2 dot-segments) counts as
# path-like too — not pinned to a hardcoded extension list, since real
# manifest predicates reference `.py`/`.sh`/`.json`/... today but a future row
# referencing an uncommon extension shouldn't silently fall through. A
# dotfile (`.gitignore`, `.mcp.json`, `.scaffold-upstream.conf`-style) is
# path-like on the leading dot alone, extension or not.
_BARE_FILENAME_RE = re.compile(r"^[\w-]+(\.[\w-]+){1,2}$")
_DOTFILE_RE = re.compile(r"^\.[\w-]+(\.[\w-]+)*$")


def _looks_like_path(token: str) -> bool:
    return "/" in token or bool(_BARE_FILENAME_RE.match(token)) or bool(_DOTFILE_RE.match(token))


def _find_atoms(text: str) -> tuple:
    """Scan `text` for recognized detect-predicate atoms, replacing each match
    with a unique placeholder token (§N§) so the residual text reduces to a
    clean AND/OR/() skeleton. Returns (rewritten_text, [atom, ...])."""
    atoms = []

    def add(atom):
        atoms.append(atom)
        return f"§{len(atoms) - 1}§"

    # 1. RUN_SELFTEST: a backtick command containing "--self-test" followed
    #    (within a short distance) by the word "passes".
    def _selftest_sub(m):
        return add(("selftest", m.group(1).strip()))
    text = re.sub(
        r"`([^`]*--self-test[^`]*)`[^.\n]{0,40}?\bpasses\b",
        _selftest_sub, text,
    )

    # 2. CONTAINS_ONLY: "`<dir>` contains only `<prefix>`-prefixed"
    def _contains_only_sub(m):
        return add(("contains_only", m.group(1).strip(), m.group(2).strip()))
    text = re.sub(
        r"`([^`]+)`\s+contains only\s+`([^`]+)`-prefixed",
        _contains_only_sub, text,
    )

    # 3. CONTAINS: nearest preceding path-like backtick token + "contains" +
    #    the following backtick token (the literal). Search left-to-right so
    #    "contains" always binds to the immediately preceding backtick.
    def _contains_sub(m):
        path_tok, literal_tok = m.group(1).strip(), m.group(2).strip()
        if not _looks_like_path(path_tok):
            return m.group(0)  # not a real path candidate — leave untouched
        return add(("contains", path_tok, literal_tok))
    text = re.sub(
        r"`([^`]+)`\s+contains\s+`([^`]+)`",
        _contains_sub, text,
    )

    # 3b. CONTAINS variants that name a config/JSON *block* or *key* rather
    #     than literal file text — approximated as a substring check for the
    #     key/block name, which is true for the common case (a JSON key or a
    #     recorded marker literally spelled that way in the file):
    #       "`path` has a `NAME` block"
    #       "`path` contains a top-level `NAME` block"
    #       "`path` defines a `NAME` ..." (e.g. an INTERFACE target)
    #       "Check `path` for a `NAME` block"
    #       "`path`'s `ATTR` contains `literal`" (ATTR is discarded context)
    def _contains_block_sub(m):
        path_tok, literal_tok = m.group(1).strip(), m.group(2).strip()
        if not _looks_like_path(path_tok):
            return m.group(0)
        return add(("contains", path_tok, literal_tok))
    text = re.sub(
        r"`([^`]+)`\s+(?:has a|contains(?: a top-level)?|defines a)\s+`([^`]+)`",
        _contains_block_sub, text,
    )

    def _check_for_block_sub(m):
        path_tok, literal_tok = m.group(1).strip(), m.group(2).strip()
        if not _looks_like_path(path_tok):
            return m.group(0)
        return add(("contains", path_tok, literal_tok))
    text = re.sub(
        r"Check\s+`([^`]+)`\s+for a\s+`([^`]+)`",
        _check_for_block_sub, text,
    )

    def _apostrophe_contains_sub(m):
        path_tok, literal_tok = m.group(1).strip(), m.group(3).strip()
        if not _looks_like_path(path_tok):
            return m.group(0)
        return add(("contains", path_tok, literal_tok))
    text = re.sub(
        r"`([^`]+)`'s\s+`([^`]+)`\s+contains\s+`([^`]+)`",
        _apostrophe_contains_sub, text,
    )

    # 4. EXISTS_ALL — the "skill dir exists (...)" idiom (25+ occurrences): a
    #    parenthetical right after "exists" naming the authoritative path(s),
    #    e.g. "exists (`.claude/skills/grm-end-session/SKILL.md`)" or
    #    "exists (`.claude/skills/grm-noir-loop/` with `SKILL.md` +
    #    `noir_loop_state.py`)" or "exists (`.../SKILL.md` + `recipe_migrate.py`)".
    #    The first backtick token is the base path; any further backtick
    #    tokens (joined by "with"/"+"/",") are filenames resolved against the
    #    base's directory (or the base itself, if it already ends in "/") —
    #    unless a later token already contains "/", in which case it is its
    #    own independent path. ALL resolved paths must exist.
    def _exists_all_sub(m):
        inner = m.group(1)
        toks = re.findall(r"`([^`]+)`", inner)
        toks = [t.strip() for t in toks if t.strip()]
        if not toks:
            return m.group(0)
        base = toks[0]
        base_dir = base if base.endswith("/") else base.rsplit("/", 1)[0] + "/" if "/" in base else ""
        paths = [base]
        for extra in toks[1:]:
            if "/" in extra:
                paths.append(extra)
            else:
                paths.append(base_dir + extra)
        return add(("exists_all", tuple(paths)))
    text = re.sub(
        r"exists(?:\s+in\s+the\s+`[^`]+`\s+skill dir)?\s*\(([^()]*)\)",
        _exists_all_sub, text,
    )

    # 5. Plain EXISTS: nearest path-like backtick token followed by "exists",
    #    tolerating one non-atom parenthetical aside in between (e.g.
    #    "`docs/README.md` (the docs map) exists").
    def _exists_sub(m):
        path_tok = m.group(1).strip()
        if not _looks_like_path(path_tok):
            return m.group(0)
        return add(("exists", path_tok))
    text = re.sub(
        r"`([^`]+)`\s*(?:\([^()§]*\)\s*)?exists\b",
        _exists_sub, text,
    )

    # 6. NOT_EXISTS: "no `<path>`" occurrences (only path-like tokens qualify,
    #    to avoid matching unrelated prose like "no bypass flag").
    def _not_exists_sub(m):
        path_tok = m.group(1).strip()
        if not _looks_like_path(path_tok):
            return m.group(0)
        return add(("not_exists", path_tok))
    text = re.sub(
        r"\bno\s+`([^`]+)`",
        _not_exists_sub, text,
    )

    return text, atoms


# --------------------------------------------------------------------------
# Boolean expression parsing over the atom skeleton
# --------------------------------------------------------------------------

class _Unparseable(Exception):
    pass


def _strip_decorative_parens(text: str) -> str:
    """Drop parenthetical asides that carry no atom placeholder — e.g. "(no
    un-prefixed survivor)" clarifying prose left over after atom extraction.
    A paren group IS kept when it wraps a placeholder (real boolean grouping,
    e.g. "(§0§ OR §1§)"). Non-nested by construction (each pass removes one
    nesting level), so this is applied repeatedly to convergence."""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\([^()]*\)", lambda m: m.group(0) if "§" in m.group(0) else " ", text)
    return text


def _tokenize_skeleton(text: str) -> list:
    text = _strip_decorative_parens(text)
    return re.findall(r"§\d+§|\bAND\b|\bOR\b|\(|\)", text)


class _BoolParser:
    """Tiny recursive-descent parser: OR binds loosest, AND tighter, then
    parens/atoms. Evaluates lazily against a supplied atom-executor."""

    def __init__(self, tokens: list, atoms: list, evaluator):
        self.tokens = tokens
        self.pos = 0
        self.atoms = atoms
        self.evaluator = evaluator

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self):
        tok = self._peek()
        self.pos += 1
        return tok

    def parse(self) -> bool:
        if not self.tokens:
            raise _Unparseable("no atoms found")
        result = self._parse_or()
        if self.pos != len(self.tokens):
            raise _Unparseable(f"trailing tokens: {self.tokens[self.pos:]}")
        return result

    def _parse_or(self) -> bool:
        val = self._parse_and()
        while self._peek() == "OR":
            self._advance()
            rhs = self._parse_and()
            val = val or rhs
        return val

    def _parse_and(self) -> bool:
        val = self._parse_atom()
        while self._peek() == "AND":
            self._advance()
            rhs = self._parse_atom()
            val = val and rhs
        return val

    def _parse_atom(self) -> bool:
        tok = self._peek()
        if tok is None:
            raise _Unparseable("unexpected end of expression")
        if tok == "(":
            self._advance()
            val = self._parse_or()
            if self._peek() != ")":
                raise _Unparseable("unbalanced parentheses")
            self._advance()
            return val
        m = re.match(r"§(\d+)§", tok)
        if not m:
            raise _Unparseable(f"unexpected token: {tok!r}")
        self._advance()
        atom = self.atoms[int(m.group(1))]
        return self.evaluator(atom)


# --------------------------------------------------------------------------
# Atom execution against a target project
# --------------------------------------------------------------------------

def _exec_atom(root: Path, atom: tuple) -> bool:
    kind = atom[0]
    if kind == "exists":
        return (root / atom[1]).exists()
    if kind == "exists_all":
        return all((root / p).exists() for p in atom[1])
    if kind == "not_exists":
        return not (root / atom[1]).exists()
    if kind == "contains":
        p = root / atom[1]
        if not p.is_file():
            return False
        try:
            return atom[2] in p.read_text(errors="replace")
        except OSError:
            return False
    if kind == "contains_only":
        d = root / atom[1]
        if not d.is_dir():
            return False
        prefix = atom[2]
        for entry in d.iterdir():
            name = entry.name
            if name.startswith(prefix):
                continue
            if re.match(r"^(README|_)", name):
                continue
            return False
        return True
    if kind == "selftest":
        return _run_selftest(root, atom[1])
    raise _Unparseable(f"unknown atom kind: {kind}")


def _run_selftest(root: Path, cmd: str) -> bool:
    """Best-effort: resolve the leading token of `cmd` (a script basename) to
    an actual file under the project tree, then run it (+ the rest of the
    command's args, typically `--self-test`). Returns False (never raises) on
    any resolution/execution failure — a detect predicate that can't be
    executed is "not confirmed", the conservative default."""
    parts = shlex.split(cmd)
    if not parts:
        return False
    script_name = parts[0]
    matches = list(root.rglob(script_name)) if "/" not in script_name else [root / script_name]
    matches = [m for m in matches if m.is_file()]
    if not matches:
        return False
    script_path = matches[0]
    if script_path.suffix == ".py":
        argv = [sys.executable, str(script_path)] + parts[1:]
    elif os.access(script_path, os.X_OK):
        argv = [str(script_path)] + parts[1:]
    else:
        argv = ["/bin/sh", str(script_path)] + parts[1:]
    try:
        proc = subprocess.run(argv, cwd=str(root), capture_output=True, timeout=120)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def evaluate_detect(root: Path, detect_text: str) -> tuple:
    """Returns (status, result) where status is "ok" | "unparseable" and
    result is a bool (only meaningful when status == "ok"). Never raises —
    an unparseable predicate degrades to a conservative "not detected"."""
    rewritten, atoms = _find_atoms(detect_text)
    if not atoms:
        return "unparseable", False
    tokens = _tokenize_skeleton(rewritten)
    parser = _BoolParser(tokens, atoms, lambda a: _exec_atom(root, a))
    try:
        result = parser.parse()
    except _Unparseable:
        return "unparseable", False
    return "ok", result


# --------------------------------------------------------------------------
# Delta computation
# --------------------------------------------------------------------------

def read_framework_version(project_root: Path):
    cfg_path = project_root / ".claude" / "grimoire-config.json"
    if not cfg_path.is_file():
        return None
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return cfg.get("framework-version")


def compute_delta(rows: list, project_root: Path, framework_version) -> list:
    """Delta = rows newer than framework_version AND failing their own detect.
    Mirrors reference.md "How to evaluate the manifest": with a version,
    collect entries where introduced-in > framework-version; without one,
    collect all entries. In both cases `detect` is run and a passing detect
    (already adopted) excludes the row."""
    fv_key = _version_key(framework_version) if framework_version else None
    delta = []
    for row in rows:
        if fv_key is not None:
            if _version_key(row.introduced_in) <= fv_key:
                continue
        status, adopted = evaluate_detect(project_root, row.detect)
        if status == "ok" and adopted:
            continue  # already adopted — excluded, the whole token-savings point
        delta.append({
            "feature-id": row.feature_id,
            "introduced-in": row.introduced_in,
            "summary": row.summary,
            "adopt": row.adopt,
            "detect_status": status,  # "ok" or "unparseable"
        })
    # Sort oldest-first (reference.md: later features may depend on config set
    # by earlier ones).
    delta.sort(key=lambda r: _version_key(r["introduced-in"]))
    return delta


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def format_json(delta: list) -> str:
    return json.dumps(delta, indent=2)


def format_table(delta: list) -> str:
    if not delta:
        return "(no undecided rows — adoption phase is a no-op)"
    lines = []
    for r in delta:
        flag = "" if r["detect_status"] == "ok" else "  [detect unparseable — verify by hand]"
        lines.append(f"- {r['feature-id']} ({r['introduced-in']}){flag}")
        lines.append(f"    {r['summary']}")
        lines.append(f"    adopt: {r['adopt']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

_FIXTURE_MANIFEST = """manifest-version: 1

# Fixture feature manifest

| feature-id | introduced-in | summary | detect | adopt | migrate |
|---|---|---|---|---|---|
| `alpha-feature` | v1.1 | Alpha feature. | Check that `alpha.marker` exists. | Create `alpha.marker`. | null |
| `beta-feature` | v1.2 | Beta feature with a content check. | Check that `beta.conf` contains `enabled=true`. | Add `enabled=true` to `beta.conf`. | null |
| `gamma-feature` | v1.3 | Gamma feature, AND-combined detect. | Check that `alpha.marker` exists AND `beta.conf` contains `enabled=true`. | Adopt both alpha and beta first. | null |
"""

_MALFORMED_MANIFEST_BAD_COLUMNS = """manifest-version: 1

# Fixture feature manifest (malformed)

| feature-id | introduced-in | summary | detect | adopt | migrate |
|---|---|---|---|---|---|
| `broken-feature` | v1.1 | Missing a column. | Check that `x` exists. | Do the thing. |
"""

_MALFORMED_MANIFEST_BAD_VERSION = """manifest-version: 1

# Fixture feature manifest (malformed)

| feature-id | introduced-in | summary | detect | adopt | migrate |
|---|---|---|---|---|---|
| `broken-feature` | not-a-version | Bad introduced-in. | Check that `x` exists. | Do the thing. | null |
"""


def self_test() -> tuple:
    """Hermetic checks over fixture manifests + fixture project dirs. Covers:
    an up-to-date project (empty delta), a behind project with some
    already-adopted rows (correct partial delta), and malformed manifest rows
    (fail loudly). Returns (passed, failed, lines)."""
    import tempfile

    cases = []  # (label, predicate)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        manifest_path = tmp / "feature-manifest.md"
        manifest_path.write_text(_FIXTURE_MANIFEST)
        rows = parse_manifest(str(manifest_path))
        cases.append(("fixture manifest parses to 3 rows", len(rows) == 3))

        # --- Case 1: up-to-date project -> empty delta. ---
        up_to_date = tmp / "up_to_date_project"
        up_to_date.mkdir()
        delta = compute_delta(rows, up_to_date, "v1.3")
        cases.append(("up-to-date project (framework-version v1.3) -> empty delta",
                      delta == []))

        # --- Case 2: behind project, some rows already adopted. ---
        behind = tmp / "behind_project"
        behind.mkdir()
        (behind / "alpha.marker").write_text("adopted")  # alpha-feature detect passes
        # beta.conf absent -> beta-feature detect fails (not adopted)
        # gamma-feature's AND detect needs both alpha.marker AND beta.conf
        # contains enabled=true -> fails because beta.conf is absent
        delta = compute_delta(rows, behind, "v1.0")
        ids = sorted(r["feature-id"] for r in delta)
        cases.append(("behind project excludes the already-adopted alpha-feature",
                      "alpha-feature" not in ids))
        cases.append(("behind project includes not-yet-adopted beta-feature and gamma-feature",
                      ids == ["beta-feature", "gamma-feature"]))
        cases.append(("delta rows are sorted oldest-introduced-in-first",
                      [r["introduced-in"] for r in delta] == ["v1.2", "v1.3"]))

        # --- Case 2b: behind project where every row is already adopted. ---
        fully_adopted = tmp / "fully_adopted_project"
        fully_adopted.mkdir()
        (fully_adopted / "alpha.marker").write_text("adopted")
        (fully_adopted / "beta.conf").write_text("enabled=true\n")
        delta = compute_delta(rows, fully_adopted, "v1.0")
        cases.append(("behind project with every feature already detect-confirmed -> empty delta",
                      delta == []))

        # --- Case 3: no framework-version recorded -> evaluate all rows. ---
        no_version_project = tmp / "no_version_project"
        no_version_project.mkdir()
        delta = compute_delta(rows, no_version_project, None)
        cases.append(("no framework-version -> all 3 rows evaluated, none adopted",
                      len(delta) == 3))

        # --- Case 4: malformed manifest — wrong column count fails loudly. ---
        bad_cols_path = tmp / "bad_columns.md"
        bad_cols_path.write_text(_MALFORMED_MANIFEST_BAD_COLUMNS)
        raised = False
        try:
            parse_manifest(str(bad_cols_path))
        except ManifestError:
            raised = True
        cases.append(("malformed row (wrong column count) raises ManifestError loudly", raised))

        # --- Case 5: malformed manifest — unparseable introduced-in version. ---
        bad_version_path = tmp / "bad_version.md"
        bad_version_path.write_text(_MALFORMED_MANIFEST_BAD_VERSION)
        raised = False
        try:
            parse_manifest(str(bad_version_path))
        except ManifestError:
            raised = True
        cases.append(("malformed row (unparseable introduced-in) raises ManifestError loudly", raised))

        # --- Case 6: an empty manifest (no table) also fails loudly. ---
        empty_path = tmp / "empty.md"
        empty_path.write_text("# Nothing here\n")
        raised = False
        try:
            parse_manifest(str(empty_path))
        except ManifestError:
            raised = True
        cases.append(("manifest with no table rows raises ManifestError loudly", raised))

    # --- Case 7: OR-combined detect + an unparseable-detect row degrades
    #     conservatively (included, flagged) instead of crashing. ---
    with tempfile.TemporaryDirectory() as tmp2:
        tmp2 = Path(tmp2)
        or_manifest = tmp2 / "or.md"
        or_manifest.write_text(
            "manifest-version: 1\n\n"
            "| feature-id | introduced-in | summary | detect | adopt | migrate |\n"
            "|---|---|---|---|---|---|\n"
            "| `or-feature` | v1.1 | OR-combined detect. | "
            "Check that `a.marker` exists OR `b.marker` exists. | Create one marker. | null |\n"
            "| `weird-feature` | v1.1 | Detect prose with no recognizable atom. | "
            "Ask a human to eyeball the dashboard. | Eyeball it. | null |\n"
        )
        rows2 = parse_manifest(str(or_manifest))
        proj = tmp2 / "proj"
        proj.mkdir()
        (proj / "b.marker").write_text("x")
        delta = compute_delta(rows2, proj, "v1.0")
        ids2 = sorted(r["feature-id"] for r in delta)
        cases.append(("OR-combined detect: one side true -> row excluded (already adopted)",
                      "or-feature" not in ids2))
        cases.append(("unparseable-detect row is included, not silently dropped",
                      "weird-feature" in ids2))
        weird = next(r for r in delta if r["feature-id"] == "weird-feature")
        cases.append(("unparseable-detect row is flagged, not misreported as confirmed",
                      weird["detect_status"] == "unparseable"))

    lines, passed, failed = [], 0, 0
    for label, ok in cases:
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    return passed, failed, lines


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DEFAULT_MANIFEST = str(Path(__file__).parent / "feature-manifest.md")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--framework-version", default=None,
                         help="Override; otherwise read from <project-root>/.claude/grimoire-config.json")
    parser.add_argument("--format", choices=["json", "table"], default="json")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        passed, failed, lines = self_test()
        for ln in lines:
            print(ln)
        print(f"\nadoption-delta self-test: {passed} passed, {failed} failed.")
        sys.exit(1 if failed else 0)

    project_root = Path(args.project_root).resolve()
    framework_version = args.framework_version or read_framework_version(project_root)

    try:
        rows = parse_manifest(args.manifest)
    except ManifestError as e:
        print(f"adoption-delta: malformed manifest — {e}", file=sys.stderr)
        sys.exit(1)

    delta = compute_delta(rows, project_root, framework_version)

    if args.format == "json":
        print(format_json(delta))
    else:
        print(format_table(delta))


if __name__ == "__main__":
    main()
