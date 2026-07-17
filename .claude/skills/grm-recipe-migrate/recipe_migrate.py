#!/usr/bin/env python3
"""recipe_migrate.py — migrate an arbitrary project onto the standard justfile
recipe vocabulary (RSS-5, #323).

Grimoire's build-recipe interface (`grm-build-recipe`) and the justfile
standard (`docs/design/justfile-standard-design.md`) fix a stable named-target
vocabulary — `build run test unit-test seed migrate lint clean package deploy
smoke gui-test release` (plus `stop`, tracked separately under #322) — every
project should expose via a root `justfile`. Adopting that contract today is a manual
procedure (`justfile-standard-design.md` §8): diagnose with
`grm-install-doctor`, hand-add recipes, hand-edit `.claude/recipes.json`. This
script automates the mechanical parts for a project that arrived with its own
bespoke build system — a `Makefile`, `package.json` scripts, hand-rolled
`scripts/*` executables, or raw (non-`just`) commands already sitting in
`.claude/recipes.json`.

Mirrors `grm-structure-migrate`'s shape: **inventory** existing entry points,
**map** them onto the standard vocabulary (high-confidence exact-name matches
auto-proposed; anything else surfaced as a finding for the agent/user to
resolve), **write or extend** the root `justfile` with recipes that DELEGATE
to those entry points (never reimplementing or deleting them), and **rewire**
`.claude/recipes.json` to the `just <recipe>` routing convention (RSS-3,
#321). Report-first; `--apply` performs the mechanical writes. Idempotent — a
second `--apply` on an already-migrated tree is a no-op.

Known simplification: delegating recipes call the underlying command as-is
(e.g. `make build`) without threading the standard recipe's parameters
(`env`, `port`, …) through to it — the source build system may not support
them at all (a plain `Makefile` target rarely takes an `ENV=` the same way
twice). Threading real parameters into an arbitrary legacy command is a
judgment call left to the agent/user after the delegation lands; this tool's
job is getting every target addressable under one name, not perfecting each
one's argument plumbing.

`sync-deps` / `vendor-check` (the two "universal (delegates)" recipes that
always route to the same framework scripts regardless of project — see
`justfile-standard-design.md` §2) are out of scope here: there is never an
existing project entry point to *map*, so they add nothing for this tool to
migrate. Wire them by hand per the standard, or via `grm-sync-from-upstream`.

Design: docs/grimoire/design/recipe-migrate-design.md (+
docs/design/justfile-standard-design.md, the contract this migrates toward).

Usage:
    recipe_migrate.py --root DIR [--json]
    recipe_migrate.py --root DIR --apply
    recipe_migrate.py --self-test

Exit codes (mirrors grm-structure-migrate):
    0  no findings (detect mode), or --apply resolved everything it could
       (or --self-test passed)
    1  findings present (detect mode), or --apply left one or more findings
       unresolved (unmapped entry point / ambiguous mapping / a vocabulary
       target with no candidate to delegate to) — or --self-test failed

Python stdlib only (#212 constraint).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

RECIPES_JSON_REL_PATH = ".claude/recipes.json"
JUSTFILE_REL_PATH = "justfile"
MAKEFILE_CANDIDATES = ("Makefile", "makefile", "GNUmakefile")
SCRIPTS_DIR_REL_PATH = "scripts"
PACKAGE_JSON_REL_PATH = "package.json"

PLACEHOLDER_MARKER = "# grimoire:placeholder"

# ---------------------------------------------------------------------------
# The standard vocabulary (justfile-standard-design.md §2 / §5).
# ---------------------------------------------------------------------------

VOCAB = [
    "build", "run", "test", "unit-test", "seed", "migrate", "lint", "clean",
    "package", "deploy", "smoke", "gui-test", "release",
]
CORE_REQUIRED = {"build", "run", "deploy"}

# `stop` is a separate work item (#322) — the vocabulary is extensible, so it
# always gets a placeholder stub and is NEVER a target of auto-mapping.
STOP_STUB = "stop"

# Fixed justfile recipe signatures — reproduced exactly (§2 / §5).
SIGNATURES = {
    "build": 'build env="dev"',
    "run": 'run env="dev" port="3000"',
    "test": 'test filter="" watch=""',
    "unit-test": 'unit-test filter="" watch=""',
    "seed": 'seed fixture="" env="dev"',
    "migrate": 'migrate env="dev"',
    "lint": "lint",
    "clean": "clean",
    "package": 'package version="" target=""',
    "deploy": "deploy env dry_run=\"false\"",
    "smoke": 'smoke port="3000"',
    "gui-test": 'gui-test baseline="main"',
    "release": "release *ARGS",
    STOP_STUB: "stop",
}

# `.claude/recipes.json` key + command template + params (§2.2 / mirrors the
# `web` quick-start template's shape). `run` routes under the historical
# `server` key (recipe.py's `run` <-> `server` dispatcher alias, §2.1).
JSON_KEY = {t: t for t in VOCAB}
JSON_KEY["run"] = "server"

JSON_COMMAND = {
    "build": "just build ${env}",
    "run": "just run ${env} ${port}",
    "test": "just test ${filter}",
    "unit-test": "just unit-test ${filter}",
    "seed": "just seed ${fixture} ${env}",
    "migrate": "just migrate ${env}",
    "lint": "just lint",
    "clean": "just clean",
    "package": "just package ${version} ${target}",
    "deploy": "just deploy ${env}",
    "smoke": "just smoke ${port}",
    "gui-test": "just gui-test ${baseline}",
    "release": "just release",
}

JSON_PARAMS = {
    "build": {"env": {"default": "dev"}},
    "run": {"port": {"default": "3000"}, "env": {"default": "dev"}},
    "test": {"filter": {"default": ""}, "watch": {"default": ""}},
    "unit-test": {"filter": {"default": ""}, "watch": {"default": ""}},
    "seed": {"fixture": {"default": ""}, "env": {"default": "dev"}},
    "migrate": {"env": {"default": "dev"}},
    "lint": {},
    "clean": {},
    "package": {"version": {"default": ""}, "target": {"default": ""}},
    "deploy": {"env": {"default": "production"}},
    "smoke": {"port": {"default": "3000"}},
    "gui-test": {"baseline": {"default": "main"}},
    "release": {},
}

# Alias sets used to match an inventoried entry-point name to a vocabulary
# target. Kept disjoint by construction so one entry-point name never matches
# more than one target (the only remaining ambiguity is >1 entry point
# mapping to the *same* target).
TARGET_ALIASES = {
    "build": {"build", "compile", "assemble", "dist"},
    "run": {"run", "start", "serve", "server", "dev"},
    "test": {"test", "tests", "check"},
    "unit-test": {"unit-test", "unittest", "unittests", "unit_test",
                  "fast-test", "test-unit"},
    "seed": {"seed", "fixtures", "seeddb", "dbseed"},
    "migrate": {"migrate", "migration", "migrations", "dbmigrate"},
    "lint": {"lint", "format", "fmt"},
    "clean": {"clean", "clobber"},
    "package": {"package", "pack", "bundle"},
    "deploy": {"deploy", "publish"},
    "smoke": {"smoke", "healthcheck"},
    "gui-test": {"gui-test", "guitest", "gui_test", "visual-test",
                 "visualtest", "screenshot-test", "screenshottest"},
    "release": {"release", "cutrelease"},
}

# Finding codes.
UNMAPPED_ENTRY_POINT = "UNMAPPED_ENTRY_POINT"
AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
MISSING_IMPLEMENTATION = "MISSING_IMPLEMENTATION"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_ALIAS_NORM = {t: {_norm(a) for a in aliases} for t, aliases in TARGET_ALIASES.items()}


@dataclass
class EntryPoint:
    """One inventoried existing build/run/test/... entry point."""

    kind: str       # "makefile" | "npm-script" | "script" | "recipes-json"
    name: str       # the raw target/script/scripts-key name
    command: str    # the invocation this entry point runs
    source: str     # display path, e.g. "Makefile", "package.json", "scripts/build.sh"

    @property
    def label(self) -> str:
        return f"{self.source}:{self.name}"


@dataclass
class MigrateFinding:
    code: str
    path: str
    message: str

    def render(self) -> str:
        return f"  {self.path:<20} {self.code:<24} {self.message}"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

_MAKEFILE_SKIP = {"all", "help", "phony", ".phony", "default"}
_MAKEFILE_TARGET_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)\s*:(?!=)")


def inventory_makefile(root: str) -> List[EntryPoint]:
    for candidate in MAKEFILE_CANDIDATES:
        path = os.path.join(root, candidate)
        if os.path.isfile(path):
            break
    else:
        return []
    entries = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith(("\t", " ")):
                    continue
                m = _MAKEFILE_TARGET_RE.match(line)
                if not m:
                    continue
                name = m.group(1)
                if name.lower() in _MAKEFILE_SKIP or name.startswith("."):
                    continue
                entries.append(EntryPoint("makefile", name, f"make {name}", candidate))
    except OSError:
        return []
    return entries


def inventory_npm_scripts(root: str) -> List[EntryPoint]:
    path = os.path.join(root, PACKAGE_JSON_REL_PATH)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return []
    return [
        EntryPoint("npm-script", name, f"npm run {name}", PACKAGE_JSON_REL_PATH)
        for name in scripts
    ]


_SCRIPT_EXTS = (".sh", ".py", ".js", ".rb", "")


def inventory_scripts_dir(root: str) -> List[EntryPoint]:
    scripts_dir = os.path.join(root, SCRIPTS_DIR_REL_PATH)
    if not os.path.isdir(scripts_dir):
        return []
    entries = []
    for fn in sorted(os.listdir(scripts_dir)):
        full = os.path.join(scripts_dir, fn)
        if not os.path.isfile(full):
            continue
        base, ext = os.path.splitext(fn)
        is_executable = bool(os.stat(full).st_mode & stat.S_IXUSR)
        if ext not in _SCRIPT_EXTS and not is_executable:
            continue
        rel = f"{SCRIPTS_DIR_REL_PATH}/{fn}"
        entries.append(EntryPoint("script", base, rel, rel))
    return entries


def inventory_recipes_json(root: str) -> List[EntryPoint]:
    path = os.path.join(root, RECIPES_JSON_REL_PATH)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    targets = data.get("targets")
    if not isinstance(targets, dict):
        return []
    inv_key = {v: k for k, v in JSON_KEY.items()}
    entries = []
    for key, spec in targets.items():
        if not isinstance(spec, dict):
            continue
        command = spec.get("command")
        if not spec.get("implemented") or not command:
            continue
        if command.strip().startswith("just "):
            continue  # already routed — not a raw entry point to migrate
        target = inv_key.get(key, key)
        if target not in VOCAB:
            continue
        entries.append(EntryPoint("recipes-json", target, command, RECIPES_JSON_REL_PATH))
    return entries


def inventory_entry_points(root: str) -> List[EntryPoint]:
    return (
        inventory_recipes_json(root)
        + inventory_makefile(root)
        + inventory_npm_scripts(root)
        + inventory_scripts_dir(root)
    )


# ---------------------------------------------------------------------------
# Existing justfile status
# ---------------------------------------------------------------------------

def _recipe_header_re(name: str) -> re.Pattern:
    # `name` optionally followed by a param list (no colons in it), then the
    # recipe-defining colon, then an optional trailing comment. Matches both
    # parameterless headers (`lint:`) and parameterized ones
    # (`build env="dev":`).
    return re.compile(rf"^{re.escape(name)}(\s[^\n:]*)?:\s*(#.*)?$")


def _find_recipe_block(lines: List[str], name: str) -> Optional[tuple]:
    header_re = _recipe_header_re(name)
    for i, line in enumerate(lines):
        if header_re.match(line):
            j = i + 1
            while j < len(lines) and (lines[j] == "" or lines[j][:1] in (" ", "\t")):
                j += 1
            return i, j
    return None


def justfile_status(text: str, name: str) -> str:
    """Return MISSING / PARTIAL / OK for `name` in the given justfile text."""
    lines = text.split("\n")
    block = _find_recipe_block(lines, name)
    if block is None:
        return "MISSING"
    i, j = block
    body = lines[i + 1:j]
    return "PARTIAL" if any(PLACEHOLDER_MARKER in ln for ln in body) else "OK"


def read_justfile(root: str) -> str:
    path = os.path.join(root, JUSTFILE_REL_PATH)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return ""


# ---------------------------------------------------------------------------
# Mapping: entry points -> vocabulary targets
# ---------------------------------------------------------------------------

def build_mapping(entry_points: List[EntryPoint]) -> tuple:
    """Returns (proposals, ambiguous, unmapped).

    proposals: target -> chosen EntryPoint (single, high-confidence)
    ambiguous: target -> [EntryPoint, ...] (>=2 candidates, none auto-picked)
    unmapped:  [EntryPoint, ...] whose name matched no vocabulary alias
    """
    candidates: Dict[str, List[EntryPoint]] = {t: [] for t in VOCAB}
    unmapped: List[EntryPoint] = []
    for ep in entry_points:
        norm = _norm(ep.name)
        matched = [t for t in VOCAB if norm in _ALIAS_NORM[t]]
        if matched:
            candidates[matched[0]].append(ep)
        else:
            unmapped.append(ep)

    proposals: Dict[str, EntryPoint] = {}
    ambiguous: Dict[str, List[EntryPoint]] = {}
    for t in VOCAB:
        cands = candidates[t]
        if not cands:
            continue
        # A `.claude/recipes.json` entry is already named exactly for this
        # target — treat it as authoritative over a same-named guess from a
        # Makefile/npm-script/scripts-dir source.
        json_cands = [c for c in cands if c.kind == "recipes-json"]
        if json_cands:
            proposals[t] = json_cands[0]
        elif len(cands) == 1:
            proposals[t] = cands[0]
        else:
            ambiguous[t] = cands
    return proposals, ambiguous, unmapped


# ---------------------------------------------------------------------------
# Detect
# ---------------------------------------------------------------------------

def classify(root: str) -> List[MigrateFinding]:
    entry_points = inventory_entry_points(root)
    proposals, ambiguous, unmapped = build_mapping(entry_points)
    text = read_justfile(root)

    findings = []
    for ep in unmapped:
        findings.append(MigrateFinding(
            UNMAPPED_ENTRY_POINT, ep.label,
            f"no standard-vocabulary match for '{ep.name}' — leave in place, "
            f"or wire it into a vocabulary recipe by hand",
        ))
    for target, cands in ambiguous.items():
        labels = ", ".join(c.label for c in sorted(cands, key=lambda c: c.label))
        findings.append(MigrateFinding(
            AMBIGUOUS_MAPPING, target,
            f"{len(cands)} candidates map to '{target}': {labels} — "
            f"resolve which one wins, or rename to disambiguate",
        ))
    for target in VOCAB:
        status = justfile_status(text, target)
        if status == "OK":
            continue
        if target in proposals:
            continue  # apply will resolve this — not left unresolved
        required = " (required)" if target in CORE_REQUIRED else " (advisory)"
        findings.append(MigrateFinding(
            MISSING_IMPLEMENTATION, target,
            f"no entry point maps to '{target}'{required} — stays a "
            f"`{PLACEHOLDER_MARKER}` stub until implemented",
        ))
    findings.sort(key=lambda f: (f.code, f.path))
    return findings


def format_report(findings: List[MigrateFinding]) -> str:
    if not findings:
        return "recipe-migrate: no findings."
    counts: Dict[str, int] = {}
    for f in findings:
        counts[f.code] = counts.get(f.code, 0) + 1
    summary = ", ".join(f"{n} {code.lower().replace('_', '-')}" for code, n in sorted(counts.items()))
    lines = [f"recipe-migrate — {len(findings)} finding(s): {summary}"]
    lines.extend(f.render() for f in findings)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _delegate_body(ep: EntryPoint) -> str:
    return f"    {ep.command}"


def _placeholder_body(target: str) -> List[str]:
    return [
        f"    {PLACEHOLDER_MARKER}",
        f'    @echo "TODO: replace with the {target} command"',
    ]


def _append_recipe(lines: List[str], target: str, ep: Optional[EntryPoint]) -> List[str]:
    if lines and lines[-1] != "":
        lines = lines + [""]
    if ep is not None:
        lines.append(f"# {target} — delegates to {ep.label} (grm-recipe-migrate).")
        lines.append(f"{SIGNATURES[target]}:")
        lines.append(_delegate_body(ep))
    else:
        lines.append(f"# {target} — grimoire vocabulary recipe (grm-recipe-migrate).")
        lines.append(f"{SIGNATURES[target]}:")
        lines.extend(_placeholder_body(target))
    return lines


def _replace_partial(lines: List[str], target: str, ep: EntryPoint) -> List[str]:
    block = _find_recipe_block(lines, target)
    assert block is not None
    i, j = block
    return lines[:i + 1] + [_delegate_body(ep)] + lines[j:]


def apply_justfile(root: str, proposals: Dict[str, EntryPoint]) -> Dict[str, str]:
    """Write/extend the root justfile. Returns the final target->status map."""
    text = read_justfile(root)
    lines = text.split("\n") if text else []
    if lines and lines[-1] == "" and len(lines) > 1:
        lines = lines[:-1]

    final_status: Dict[str, str] = {}
    for target in VOCAB + [STOP_STUB]:
        status = justfile_status("\n".join(lines), target)
        if status == "OK":
            final_status[target] = "OK"
            continue
        proposal = proposals.get(target) if target != STOP_STUB else None
        if status == "PARTIAL":
            if proposal is not None:
                lines = _replace_partial(lines, target, proposal)
                final_status[target] = "OK"
            else:
                final_status[target] = "PARTIAL"
            continue
        # MISSING
        lines = _append_recipe(lines, target, proposal)
        final_status[target] = "OK" if proposal is not None else "PARTIAL"

    new_text = "\n".join(lines).rstrip("\n") + "\n"
    with open(os.path.join(root, JUSTFILE_REL_PATH), "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return final_status


def apply_recipes_json(root: str, final_status: Dict[str, str]) -> None:
    path = os.path.join(root, RECIPES_JSON_REL_PATH)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
    else:
        data = {}
    data.setdefault("interface-version", 5)
    data.setdefault("stack", "custom")
    targets = data.setdefault("targets", {})

    for target in VOCAB:
        key = JSON_KEY[target]
        if final_status.get(target) == "OK":
            targets[key] = {
                "command": JSON_COMMAND[target],
                "implemented": True,
                "params": JSON_PARAMS[target],
            }
        else:
            existing = targets.get(key)
            if not existing:
                targets[key] = {"command": None, "implemented": False}
            # An existing entry (whatever its shape) is left alone — never
            # downgrade a caller-authored entry we didn't just resolve.

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False)
        fh.write("\n")


def run_apply(root: str) -> int:
    entry_points = inventory_entry_points(root)
    proposals, _ambiguous, _unmapped = build_mapping(entry_points)
    final_status = apply_justfile(root, proposals)
    apply_recipes_json(root, final_status)

    findings = classify(root)
    if findings:
        print(format_report(findings))
        print(f"\nrecipe-migrate: {len(findings)} finding(s) left unresolved (see above).")
        return 1
    print("recipe-migrate: all findings resolved.")
    return 0


def run_detect(root: str, as_json: bool = False) -> int:
    findings = classify(root)
    if as_json:
        print(json.dumps({
            "finding_count": len(findings),
            "findings": [asdict(f) for f in findings],
        }, indent=2, sort_keys=True))
    else:
        print(format_report(findings))
    return 1 if findings else 0


# ---------------------------------------------------------------------------
# Self-test (stdlib-only, deterministic, offline — synthetic fixture trees)
# ---------------------------------------------------------------------------

def _write(path: str, content: str, executable: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if executable:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _build_makefile_fixture(tmp: str) -> None:
    """Makefile project: build/test/lint/clean map cleanly; `compile` is a
    second candidate for `build` (ambiguous); `docs` matches nothing
    (unmapped); deploy/run/etc. are left with no candidate (missing-impl)."""
    _write(os.path.join(tmp, "Makefile"), (
        ".PHONY: all\n"
        "all: build\n"
        "\n"
        "build:\n"
        "\techo building\n"
        "\n"
        "compile:\n"
        "\techo compiling\n"
        "\n"
        "test:\n"
        "\techo testing\n"
        "\n"
        "lint:\n"
        "\techo linting\n"
        "\n"
        "clean:\n"
        "\techo cleaning\n"
        "\n"
        "docs:\n"
        "\techo building docs\n"
    ))


def _build_npm_fixture(tmp: str) -> None:
    """npm-scripts project: build/test/lint clean; `start` maps to `run`;
    `deploy` maps to `deploy`. No ambiguity, no unmapped scripts."""
    _write(os.path.join(tmp, "package.json"), json.dumps({
        "name": "fixture",
        "scripts": {
            "build": "webpack",
            "start": "node server.js",
            "test": "jest",
            "deploy": "./deploy.sh",
        },
    }))


def _build_scripts_fixture(tmp: str) -> None:
    """Bespoke-scripts project: scripts/build.sh, scripts/run.sh,
    scripts/test.sh, scripts/lint.sh all map cleanly; scripts/notify.sh is
    unmapped."""
    for name in ("build", "run", "test", "lint"):
        _write(os.path.join(tmp, "scripts", f"{name}.sh"),
               f"#!/usr/bin/env bash\necho {name}\n", executable=True)
    _write(os.path.join(tmp, "scripts", "notify.sh"),
           "#!/usr/bin/env bash\necho notify\n", executable=True)


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

    # 1. Empty project: every vocabulary target is MISSING_IMPLEMENTATION.
    with tempfile.TemporaryDirectory() as tmp:
        findings = classify(tmp)
        codes = {f.code for f in findings}
        check("empty project reports MISSING_IMPLEMENTATION for every target",
              codes == {MISSING_IMPLEMENTATION})
        check("empty project reports exactly len(VOCAB) findings",
              len(findings) == len(VOCAB))
        rc = run_detect(tmp)
        check("empty project detect exits 1", rc == 1)
        rc_apply = run_apply(tmp)
        check("empty project apply still exits 1 (nothing to delegate to)", rc_apply == 1)
        text = read_justfile(tmp)
        check("apply on empty project still writes all vocabulary recipes as stubs",
              all(f"\n{SIGNATURES[t]}:" in ("\n" + text) for t in VOCAB))
        check("every stubbed recipe carries the placeholder marker",
              text.count(PLACEHOLDER_MARKER) == len(VOCAB) + 1)  # + stop stub

    # 2. Makefile project.
    with tempfile.TemporaryDirectory() as tmp:
        _build_makefile_fixture(tmp)
        entry_points = inventory_entry_points(tmp)
        check("Makefile inventory finds 6 targets (excludes .PHONY/all)",
              len([e for e in entry_points if e.kind == "makefile"]) == 6)
        proposals, ambiguous, unmapped = build_mapping(entry_points)
        check("test/lint/clean auto-proposed ('build' is ambiguous, see below)",
              {"test", "lint", "clean"} <= set(proposals))
        check("'compile' makes 'build' ambiguous (2 candidates)",
              "build" in ambiguous and len(ambiguous["build"]) == 2)
        check("'docs' target is unmapped",
              any(e.name == "docs" for e in unmapped))

        findings = classify(tmp)
        fcodes = {f.code for f in findings}
        check("detect reports AMBIGUOUS_MAPPING", AMBIGUOUS_MAPPING in fcodes)
        check("detect reports UNMAPPED_ENTRY_POINT", UNMAPPED_ENTRY_POINT in fcodes)
        check("detect reports MISSING_IMPLEMENTATION (e.g. deploy)",
              any(f.code == MISSING_IMPLEMENTATION and f.path == "deploy" for f in findings))
        rc = run_detect(tmp)
        check("Makefile project detect exits 1", rc == 1)

        makefile_before = open(os.path.join(tmp, "Makefile")).read()
        rc_apply = run_apply(tmp)
        check("apply still exits 1 (ambiguous 'build' + unmapped 'docs' persist)", rc_apply == 1)
        makefile_after = open(os.path.join(tmp, "Makefile")).read()
        check("Makefile itself is never modified", makefile_before == makefile_after)

        text = read_justfile(tmp)
        check("test recipe delegates to 'make test'", "make test" in text)
        check("lint recipe delegates to 'make lint'", "make lint" in text)
        check("clean recipe delegates to 'make clean'", "make clean" in text)
        check("build recipe was NOT auto-resolved (still a placeholder, ambiguous)",
              justfile_status(text, "build") == "PARTIAL")

        with open(os.path.join(tmp, RECIPES_JSON_REL_PATH)) as fh:
            recipes = json.load(fh)
        check("recipes.json routes test to 'just test ...'",
              recipes["targets"]["test"]["command"].startswith("just test"))
        check("recipes.json marks test implemented",
              recipes["targets"]["test"]["implemented"] is True)
        check("recipes.json leaves build unimplemented (still ambiguous)",
              recipes["targets"]["build"]["implemented"] is False)

        # Idempotency.
        text_once = text
        rc_apply2 = run_apply(tmp)
        text_twice = read_justfile(tmp)
        check("second apply on Makefile project is a no-op for the justfile",
              text_once == text_twice)
        check("second apply returns the same exit code", rc_apply2 == rc_apply)

    # 3. npm-scripts project — every script maps cleanly, no ambiguity.
    with tempfile.TemporaryDirectory() as tmp:
        _build_npm_fixture(tmp)
        entry_points = inventory_entry_points(tmp)
        check("npm-scripts inventory finds 4 scripts",
              len([e for e in entry_points if e.kind == "npm-script"]) == 4)
        proposals, ambiguous, unmapped = build_mapping(entry_points)
        check("build/run/test/deploy all auto-proposed, no ambiguity",
              {"build", "run", "test", "deploy"} <= set(proposals) and not ambiguous)
        check("no unmapped npm scripts", not unmapped)

        pkg_before = open(os.path.join(tmp, PACKAGE_JSON_REL_PATH)).read()
        rc_apply = run_apply(tmp)
        pkg_after = open(os.path.join(tmp, PACKAGE_JSON_REL_PATH)).read()
        check("package.json itself is never modified", pkg_before == pkg_after)
        text = read_justfile(tmp)
        check("run recipe delegates to 'npm run start'", "npm run start" in text)
        check("deploy recipe delegates to './deploy.sh'", "npm run deploy" in text)
        check("build/run/test/deploy resolved to OK",
              all(justfile_status(text, t) == "OK" for t in ("build", "run", "test", "deploy")))
        check("apply still exits 1 (seed/migrate/lint/... have no candidate)", rc_apply == 1)

    # 4. Bespoke-scripts project.
    with tempfile.TemporaryDirectory() as tmp:
        _build_scripts_fixture(tmp)
        entry_points = inventory_entry_points(tmp)
        check("scripts/ inventory finds 5 executables",
              len([e for e in entry_points if e.kind == "script"]) == 5)
        proposals, ambiguous, unmapped = build_mapping(entry_points)
        check("build/run/test/lint all auto-proposed", {"build", "run", "test", "lint"} <= set(proposals))
        check("'notify.sh' is unmapped", any(e.name == "notify" for e in unmapped))

        rc_apply = run_apply(tmp)
        text = read_justfile(tmp)
        check("build recipe delegates to 'scripts/build.sh'", "scripts/build.sh" in text)
        check("notify.sh untouched on disk",
              os.path.isfile(os.path.join(tmp, "scripts", "notify.sh")))
        check("apply exits 1 (notify.sh unmapped persists)", rc_apply == 1)

    # 5. Raw command already sitting in .claude/recipes.json is inventoried
    #    and takes priority over a same-named Makefile target.
    with tempfile.TemporaryDirectory() as tmp:
        _build_makefile_fixture(tmp)
        _write(os.path.join(tmp, RECIPES_JSON_REL_PATH), json.dumps({
            "interface-version": 5,
            "targets": {"build": {"command": "./old-build.sh", "implemented": True}},
        }))
        entry_points = inventory_entry_points(tmp)
        proposals, ambiguous, _unmapped = build_mapping(entry_points)
        check("recipes.json raw command wins over the ambiguous Makefile 'build'/'compile' pair",
              "build" in proposals and proposals["build"].kind == "recipes-json")
        check("'build' is no longer reported ambiguous", "build" not in ambiguous)
        run_apply(tmp)
        text = read_justfile(tmp)
        check("build recipe delegates to the pre-existing raw recipes.json command",
              "./old-build.sh" in text)

    # 6. A justfile with a real (non-placeholder) recipe is left untouched.
    with tempfile.TemporaryDirectory() as tmp:
        _build_makefile_fixture(tmp)
        _write(os.path.join(tmp, JUSTFILE_REL_PATH),
               'build env="dev":\n    ./my-real-build.sh {{env}}\n')
        rc_apply = run_apply(tmp)
        text = read_justfile(tmp)
        check("pre-existing real build recipe is never overwritten",
              "./my-real-build.sh" in text and "make build" not in text)

    print(f"recipe-migrate self-test: {passed} passed, {failed} failed.")
    for ln in lines:
        print(ln)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recipe-migrate: inventory existing build entry points, "
                    "map them onto the standard justfile vocabulary, and "
                    "write/rewire a conformant justfile + recipes.json "
                    "(grm-recipe-migrate). See docs/design/justfile-standard-design.md."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true",
                       help="Run against built-in offline fixtures (no network calls).")
    mode.add_argument("--root", metavar="DIR",
                       help="Project root to migrate.")
    parser.add_argument("--apply", action="store_true",
                         help="Write/extend the justfile and rewire recipes.json. "
                              "Default is detect-only (read-only, nothing written).")
    parser.add_argument("--json", action="store_true",
                         help="Emit detect-mode findings as JSON instead of the "
                              "human table (ignored with --apply).")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.apply:
        return run_apply(args.root)
    return run_detect(args.root, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
