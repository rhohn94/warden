#!/usr/bin/env python3
"""Install-doctor audit helper for the Grimoire scaffolding.

Runs the *mechanical, deterministic* half of a framework health check so the
install-doctor SKILL.md does not have to reimplement file walks, conf parsing,
or git plumbing in prose. It WRAPS the existing skills rather than duplicating
their logic:

  * the framework-file audit reuses the golden baseline shipped with
    `grm-workflow-bootstrap` (the same `golden/` tree that `grm-workflow-bootstrap`
    restores from) — this helper only classifies MISSING / DRIFTED / OK; the
    actual restore is delegated to `workflow-bootstrap --restore`.
  * the upstream-connection checks validate the inputs that
    `grm-sync-from-upstream` consumes (`.scaffold-upstream.conf`, `.scaffold-base/`,
    `UPSTREAM_REPO` reachability) without performing any merge — repair is
    delegated to `grm-sync-from-upstream` (`--adopt-base` / `--apply`).

Read-only by default. It NEVER mutates tracked project or framework files; the
`repair` subcommand emits an ordered, non-destructive repair *plan* (which
wrapped skill to call for each finding) and the SKILL.md drives all framework-file
mutation through the wrapped skills.

The one self-contained repair `repair` can perform itself is **freezing the
golden baseline** (`repair --freeze-baseline`, also reachable via the
back-compat `--repair` flag): it derives a versioned `golden-v{X.Y}.tar.gz`
under the gitignored `.grimoire-golden/` cache from the current PRISTINE
scaffold, delegating to `grm-workflow-bootstrap`'s `generate_golden.freeze_from_install`.
This writes only to the gitignored cache (never a tracked file) and closes the
documented upgrade gap (#182): after adopting the generated-golden feature and
deleting the legacy committed tree, a project had no non-interactive way to
re-establish the baseline the framework-file audit needs.

Drift suppression (false-positive avoidance). A "differs from golden" byte
mismatch is NOT always drift. Three classes of legitimate divergence are
classified out of the DRIFTED bucket so a healthy install reports zero false
positives:

  * SEED-DIVERGED — project-owned files seeded from a golden stub then grown by
    the project (`docs/version-history.md`, `vendor.toml`). Divergence is the
    intended steady state. (#148, #165)
  * PARADIGM — the four paradigm-swapped skill files whose live content is the
    active work-paradigm variant while golden holds the generic default. (#156)
  * NEWER-THAN-GOLDEN — files a recent `grm-sync-from-upstream` advanced past the
    last golden freeze. The live file is *ahead*, so a "repair" would revert a
    correct sync. (#154, #156)

The newer-than-golden predicate is a reusable helper (`GoldenStaleness`) shared
with `grm-regenerate-grimoire` so both tools agree on "ahead of golden".

The Justfile contract check audits the FULL build-recipe vocabulary (RSS-3, #321;
`stop` added RSS-4, #322; `unit-test` added v8, #360; `gui-test` added v9, #362)
in the repo's justfile —
build/run/stop/test/unit-test/seed/migrate/lint/clean/package/deploy/smoke/
gui-test/release/sync-deps/vendor-check — reporting MISSING/PARTIAL/OK per recipe.
The core trio (build, run, deploy) plus any target `.claude/recipes.json` marks
implemented-and-routed-to-`just <recipe>` is REQUIRED (MISSING/PARTIAL there
causes exit 1); every other vocabulary recipe is ADVISORY (reported for coverage
but never a health failure). See docs/design/justfile-standard-design.md.

Beyond presence/placeholder, the doctor also checks SIGNATURE-LEVEL conformance
(#433) for the 14 non-delegating vocabulary recipes: a recipe can be OK by the
presence/placeholder classifier (real-looking, non-placeholder body) while its
parameter list still silently diverges from the standard contract —
`build: echo "todo"` reports OK today even though it drops the required `env`
parameter. `deploy`'s `env` parameter defaulting to a production-like value
(`prod`/`production`/`live`) is its own SAFETY-CRITICAL `deploy-prod-default`
finding — block-tier ALWAYS, independent of `--strict` (references a real
fleet incident: goon-cave's `deploy` recipe defaulted to prod). Every other
signature mismatch is WARN-tier (`sig-mismatch`) and escalates to a blocking
`fail` only under `--strict`.

The role taxonomy (install-doctor is a skill, not a role) is a framework-internal
design -- see the upstream Grimoire repository for that rationale.

CLI:  python3 install_doctor.py [audit] [--json] [--no-network] [--strict]
      python3 install_doctor.py repair [--json] [--no-network] [--strict]
      python3 install_doctor.py repair --freeze-baseline [--no-network]
      python3 install_doctor.py --repair         # back-compat: == repair --freeze-baseline
      python3 install_doctor.py --self-test
      python3 install_doctor.py --help

--strict escalates justfile signature-mismatch findings (`sig-mismatch`) and
required-feature-catalog conformance findings (`catalog-conformance`, #434)
from WARN to a blocking `fail`; `deploy-prod-default` always blocks
regardless. A catalog `exempt` (blocked-on-upstream, no published artifact to
probe) or `degraded` (probe script unavailable in this flavor) finding never
escalates, strict or not.

Exit codes:
  0  healthy (no MISSING, no DRIFTED, upstream OK)
  1  degraded (one or more checks reported a problem)
  2  usage / internal error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# grm-issue-tracker is a fixed sibling skill directory (mirrors the
# code_health.py -> architecture_fitness.py pattern). Load it by a
# __file__-relative path so find_repo_root()/CONFIG_FILE have a single body
# of truth (#335) instead of a fourth, divergent copy.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-issue-tracker"))
import issue_tracker  # noqa: E402  (sys.path set immediately above)

# grm-web-app-apply is likewise a fixed sibling skill directory — reuse its
# standalone changelog-surface conformance probe (required-feature-catalog.md
# §Entry 2, v3.94 #437) rather than duplicating the dial/build-info/route
# checks here.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-web-app-apply"))
import changelog_conformance  # noqa: E402  (sys.path set immediately above)

# grm-component-registry is likewise a fixed sibling skill directory — reuse
# its deterministic build engine (RegistryEngine/Discovery/resolve_scan_paths)
# for the registry-freshness check (#458) rather than re-deriving the same
# discovery/diff logic here.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-component-registry"))
import component_registry  # noqa: E402  (sys.path set immediately above)

# grm-required-feature-catalog is likewise a fixed sibling skill directory —
# reuse its conformance-verification loop (#434) rather than re-deriving the
# catalog-entry gating/dispatch logic here.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-required-feature-catalog"))
import catalog_conformance  # noqa: E402  (sys.path set immediately above)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = issue_tracker.CONFIG_FILE
UPSTREAM_CONF = ".scaffold-upstream.conf"
BASE_ROOT = ".scaffold-base"
ARCHITECTURE_RULES_FILE = ".claude/architecture-rules.json"
ARCHITECTURE_RULES_EXAMPLE = ".claude/architecture-rules.example.json"
# The golden baseline is a generated artifact (no longer a committed tree). The
# doctor resolves it via grm-workflow-bootstrap's generate_golden helper and reuses
# it as the canonical file set rather than maintaining its own list.
GENERATE_GOLDEN_REL = ".claude/skills/grm-workflow-bootstrap/generate_golden.py"
FLAVOR_DIR = "claude-code"

# Statuses that represent a real, actionable problem (drive the exit code and
# the "ATTENTION NEEDED" headline). Everything else — including the three
# suppression statuses and the two advisory statuses below — is informational.
# "partial" covers required justfile recipes that have a grimoire:placeholder body.
# "deploy-prod-default" (#433) is the safety-critical justfile signature
# finding — ALWAYS a problem, independent of --strict (see
# JUSTFILE_PROD_ENV_DEFAULTS below). The general signature-mismatch finding
# ("sig-mismatch") is deliberately NOT in this set — it is WARN-tier by
# default and only becomes a "fail" (already covered here) under --strict.
PROBLEM_STATUSES = frozenset(
    {"missing", "drifted", "fail", "partial", "deploy-prod-default"})

# Advisory statuses (RSS-3, #321): a full-vocabulary justfile recipe that the
# project has NOT wired to `just <recipe>` in `.claude/recipes.json` (unimplemented
# or implemented via a raw command). Its absence/placeholder body is REPORTED for
# coverage visibility (MISSING/PARTIAL/OK per recipe) but is NOT a health problem —
# a lib/cli/framework stack legitimately omits deploy/package/smoke/etc.
STATUS_ADVISORY_MISSING = "advisory-missing"
STATUS_ADVISORY_PARTIAL = "advisory-partial"

# Suppression statuses — a byte mismatch against golden that is expected, so it
# is NOT a problem and is never a repair target.
STATUS_SEED_DIVERGED = "seed-diverged"
STATUS_PARADIGM = "paradigm"
STATUS_NEWER = "newer-than-golden"

# Justfile contract (RSS-3, #321): the doctor audits the FULL build-recipe
# vocabulary in the repo's `justfile`, reporting MISSING/PARTIAL/OK per recipe.
# `JUSTFILE_CORE_RECIPES` are the always-required trio (build/run/deploy) — a
# MISSING/PARTIAL there is a health problem when no recipes.json refutes it.
# `JUSTFILE_FULL_VOCABULARY` is the whole interface vocabulary under its canonical
# *justfile* names (INTERFACE `server` → `run`). `stop` (RSS-4, #322) joins the
# vocabulary like every non-core-trio recipe: advisory unless a project's
# recipes.json marks it implemented and routes it to `just stop`.
# A recipe outside the core trio is REQUIRED only when `.claude/recipes.json` marks
# its target implemented AND routes it to `just <recipe>` (so `recipe.py <t>` ≡
# `just <t>` is enforced); otherwise it is advisory. See
# docs/design/justfile-standard-design.md + build-recipe-interface-design.md.
JUSTFILE_CORE_RECIPES = ("build", "run", "deploy")
# v8 (#360): `unit-test` joins the vocabulary like every non-core-trio
# recipe added since RSS-3/RSS-4 — advisory unless a project's recipes.json
# marks it implemented and routes it to `just unit-test` (back-compat: an
# existing project with no unit-test recipe reports ADVISORY-MISSING, never
# a health failure).
# v9 (#362): `gui-test` joins the vocabulary the same way — advisory unless a
# GUI project's recipes.json marks it implemented and routes it to
# `just gui-test`. A non-GUI project reports ADVISORY-MISSING, never a
# health failure (docs/grimoire/design/runtime-verification-design.md
# §GUI testing).
JUSTFILE_FULL_VOCABULARY = (
    "build", "run", "stop", "test", "unit-test", "seed", "migrate", "lint",
    "clean", "package", "deploy", "smoke", "gui-test", "release", "sync-deps",
    "vendor-check",
)
# Justfile recipe name → recipes.json INTERFACE target key (identity unless the
# canonical justfile name differs from the versioned INTERFACE verb).
JUSTFILE_RECIPE_TO_TARGET = {"run": "server"}
JUSTFILE_PLACEHOLDER_MARKER = "# grimoire:placeholder"

# ---------------------------------------------------------------------------
# Signature-level justfile conformance (#433)
# ---------------------------------------------------------------------------
#
# The presence/placeholder classification above (`_classify_justfile_recipe`)
# answers "does this recipe exist and is it implemented?" but says nothing
# about whether its PARAMETER LIST matches the standard contract
# (justfile-standard-design.md §2). A recipe can be OK by that classifier
# (present, non-placeholder body) while still being silently wrong —
# `build: echo "todo"` reports OK today, and worse, `deploy env="prod"
# dry_run="false":` (env given a default that resolves to production) reports
# OK too, even though a bare `just deploy` would now silently ship to prod.
# This section adds signature-level parsing ON TOP OF the existing
# presence/placeholder classifier — extending it, not replacing it.
#
# Parsing approach: the justfile TEXT is parsed directly, reusing the same
# regex-based approach `_classify_justfile_recipe` already uses, rather than
# shelling out to `just --summary`/`just --dump`. Verified locally against
# `just` 1.51.0: `--summary` prints bare recipe names only (no parameter list
# at all — useless for signature checking); `--dump` reprints the justfile in
# a canonicalized form that requires the `just` binary to be installed on
# whatever machine/CI/sandbox runs the audit. install-doctor is meant to run
# everywhere agents run, including sandboxes without `just` installed, and
# its own `--self-test` must stay fully offline/deterministic (see the CLI
# docstring above) — a subprocess dependency would break both. Reusing the
# existing text-parsing surface also keeps a single source of truth for
# "where is this recipe's header line" instead of two parsers that could
# silently disagree.
#
# Only the 14 non-delegating vocabulary recipes carry a checked signature.
# `sync-deps`/`vendor-check` are excluded: per justfile-standard-design.md §2
# their bodies are fixed, framework-delegating one-liners identical across
# every project (not a per-project-authored implementation), so a mismatch
# there would be a copy/paste error in the framework's own template, not the
# "silently wrong per-project contract" failure mode this check targets.
#
# Each value is a list of (name, default, variadic) tuples in declaration
# order:
#   default=None   -> positional/required, no default (only `deploy`'s `env`)
#   default=str    -> the exact standard default value (quoted in the
#                      justfile, e.g. `env="dev"`)
#   variadic=True  -> a `*name`-style splat parameter (`release *ARGS`); the
#                      splat's own name is not contract-fixed (mirrors `just`
#                      itself, which does not constrain `*name` naming)
JUSTFILE_STANDARD_SIGNATURES: dict[str, list[tuple[str, str | None, bool]]] = {
    "build":   [("env", "dev", False)],
    "run":     [("env", "dev", False), ("port", "3000", False)],
    "stop":    [("port", "", False)],
    "test":    [("filter", "", False), ("watch", "", False)],
    "unit-test": [("filter", "", False), ("watch", "", False)],
    "gui-test": [("baseline", "main", False)],
    "seed":    [("fixture", "", False), ("env", "dev", False)],
    "migrate": [("env", "dev", False)],
    "lint":    [],
    "clean":   [],
    "package": [("version", "", False), ("target", "", False)],
    "deploy":  [("env", None, False), ("dry_run", "false", False)],
    "smoke":   [("port", "3000", False)],
    "release": [("ARGS", None, True)],
}

# Safety-critical (#433): `deploy`'s `env` parameter defaulting to a
# production-like value. References a real fleet incident (goon-cave's
# `deploy` recipe defaulted its `env` parameter to prod — a bare `just
# deploy` silently shipped to production; signature checking would have
# caught it). Exact, case-insensitive match against the default value —
# deliberately not a substring match, to avoid flagging unrelated values that
# merely contain "prod" (e.g. a hypothetical "reproduce" environment name).
JUSTFILE_PROD_ENV_DEFAULTS = frozenset({"prod", "production", "live"})

_JUSTFILE_PARAM_RE = re.compile(
    r'(\*?[A-Za-z_][A-Za-z0-9_-]*)'        # name, optionally *-prefixed (variadic)
    r'(?:\s*=\s*"((?:[^"\\]|\\.)*)")?'     # optional ="default" (quoted)
)


def _load_generate_golden(root: Path):
    """Load the generate_golden helper module from the bootstrap skill, or None."""
    import importlib.util
    gen_path = root / GENERATE_GOLDEN_REL
    if not gen_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("generate_golden", gen_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def freeze_baseline(root: Path) -> Path:
    """Freeze a golden baseline for `root` from the current PRISTINE scaffold.

    The one self-contained repair install-doctor performs. Delegates to
    grm-workflow-bootstrap's generate_golden.freeze_from_install, which writes a
    versioned `golden-v{X.Y}.tar.gz` under the gitignored `.grimoire-golden/`
    cache. Touches NO tracked file, so the "never mutates files" contract about
    framework/project files holds. Closes the documented upgrade gap (#182): a
    non-interactive way to (re-)establish the baseline the framework-file audit
    needs, without the full interactive grm-workflow-bootstrap flow.

    Safe only on a pristine / freshly-synced scaffold (the generator treats the
    root as the flavor source); freezing a customized tree would bake drift into
    the baseline — same precondition as the bootstrap freeze trigger.

    Raises FileNotFoundError if the generate_golden helper is unavailable.
    """
    gen = _load_generate_golden(root)
    if gen is None:
        raise FileNotFoundError(
            f"cannot freeze baseline: generate_golden helper not found at "
            f"{GENERATE_GOLDEN_REL} (run grm-workflow-bootstrap)")
    return gen.freeze_from_install(root)

# Files that legitimately carry per-project values; a content difference is
# expected, so we down-grade DRIFTED to CUSTOMISED rather than flagging it.
# (The SKILL.md still surfaces these as informational.)
EXPECTED_CUSTOM = {
    "CLAUDE.md",
    "settings.json",
    UPSTREAM_CONF,
}

# Project-owned files seeded once from a golden stub then grown by the project.
# Keyed on golden-relative path. The golden carries an empty template / example
# stub; the live file accumulates real content, so divergence is the intended
# steady state — report SEED-DIVERGED, never DRIFTED. (#148, #165)
SEED_OWNED = frozenset({
    "docs/version-history.md",
    "vendor.toml",
})

# The four work-paradigm-swapped skills. golden-relative path -> the paradigm
# source filename under .claude/paradigms/<slug>/. When the active paradigm's
# source matches the live file, the "drift" is the correct active variant. (#156)
PARADIGM_SKILLS = {
    "skills/grm-project-manager/SKILL.md": "project-manager-SKILL.md",
    "skills/grm-integration-master/SKILL.md": "integration-master-SKILL.md",
    "skills/grm-release-phase/SKILL.md": "release-phase-SKILL.md",
    "skills/grm-release-phase-merge/SKILL.md": "release-phase-merge-SKILL.md",
}

UPSTREAM_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://|^git@|^[~./]|^[A-Za-z]:[\\/]")


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class Check:
    """A single audited item with a status and a human-readable detail line."""

    name: str
    status: str            # ok|missing|drifted|warn|fail|seed-diverged|paradigm|newer-than-golden
    detail: str = ""

    @property
    def problem(self) -> bool:
        return self.status in PROBLEM_STATUSES


@dataclass
class Report:
    """Full health report; serializes to JSON or renders a Markdown artifact."""

    repo_root: str
    framework: list[Check] = field(default_factory=list)
    upstream: list[Check] = field(default_factory=list)
    base: list[Check] = field(default_factory=list)
    justfile: list[Check] = field(default_factory=list)
    architecture: list[Check] = field(default_factory=list)
    hook_contracts: list[Check] = field(default_factory=list)
    fixtures: list[Check] = field(default_factory=list)
    changelog_surface: list[Check] = field(default_factory=list)
    component_registry: list[Check] = field(default_factory=list)
    catalog_conformance: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def all_checks(self) -> list[Check]:
        return [*self.framework, *self.upstream, *self.base, *self.justfile,
                *self.architecture, *self.hook_contracts, *self.fixtures,
                *self.changelog_surface, *self.component_registry,
                *self.catalog_conformance]

    @property
    def healthy(self) -> bool:
        return not any(c.problem for c in self.all_checks)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.all_checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from start (or cwd) until grimoire-config.json is found.

    Delegates the primary walk to the shared `issue_tracker.find_repo_root`
    (single body of truth, #335), which returns None on a miss. install-
    doctor keeps one extra fallback pass on top of that None, deliberately
    NOT folded into the shared function: look for the nearest ancestor with
    a bare `.claude/` directory before giving up. This stays install-doctor-
    only — it is the one call site that audits installs which may
    legitimately be missing grimoire-config.json (a broken/partial/pre-
    config install is exactly the case install-doctor exists to diagnose),
    so treating a bare `.claude/` as "close enough" is more correct here.
    The other three call sites (issue_tracker.py, migrate_roadmap_issues.py,
    issue_reconcile.py) keep the shared function's plain cwd fallback.
    """
    current = (start or Path.cwd()).resolve()
    root = issue_tracker.find_repo_root(current)
    if root is not None:
        return root
    # Shared walk found nothing: install-doctor-specific extra fallback.
    for candidate in [current, *current.parents]:
        if (candidate / ".claude").is_dir():
            return candidate
    return current


def read_conf(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE upstream-conf file (comments and blanks skipped)."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        # Strip inline comments after the value (conf files allow them).
        val = val.split("#", 1)[0].strip()
        out[key.strip()] = val
    return out


def _active_paradigm_slug(root: Path) -> str | None:
    """Read work-paradigm.value from grimoire-config.json and slugify it.

    Returns a lowercase slug (noir|weiss|supervised) or None if unreadable.
    Resolves the legacy v1 aliases (Autonomous->Noir, Collaborative->Weiss).
    """
    cfg = root / CONFIG_FILE
    if not cfg.exists():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    wp = data.get("work-paradigm")
    value = wp.get("value") if isinstance(wp, dict) else wp
    if not isinstance(value, str):
        return None
    aliases = {"autonomous": "noir", "collaborative": "weiss"}
    slug = value.strip().lower()
    return aliases.get(slug, slug) or None


# ---------------------------------------------------------------------------
# Golden-staleness predicate (SHARED with grm-regenerate-grimoire, Lane F)
# ---------------------------------------------------------------------------


class GoldenStaleness:
    """Decides whether a live file is *newer than* the resolved golden baseline.

    Root cause of the post-sync false positives (#154, #156): a sync advances a
    framework file past the last golden freeze, so the file differs from golden
    yet is correct — "repairing" it would revert the sync. This predicate lets a
    consumer treat such a file as ahead-of-golden rather than drifted.

    The comparison is by modification time against the golden archive's freeze
    time (the archive is reproducible/mtime-stamped at freeze). When no archive
    timestamp is resolvable, the predicate is conservative and returns False
    (treat as a genuine difference) so it never *hides* real drift.

    Reused by grm-regenerate-grimoire so both tools agree on "ahead of golden".
    """

    def __init__(self, golden_mtime: float | None):
        self._golden_mtime = golden_mtime

    @classmethod
    def for_root(cls, root: Path, gen=None) -> "GoldenStaleness":
        """Build the predicate for `root`, resolving the golden freeze time.

        Prefers the frozen archive's mtime (the authoritative freeze instant);
        falls back to the extracted-tree mtime; None if neither exists.
        """
        cache = root / getattr(gen, "GOLDEN_CACHE_DIR", ".grimoire-golden")
        archive_glob = getattr(gen, "GOLDEN_ARCHIVE_GLOB", "golden-v*.tar.gz")
        tree_subdir = getattr(gen, "GOLDEN_TREE_SUBDIR", "tree")
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
# Audit: framework files vs the workflow-bootstrap golden baseline
# ---------------------------------------------------------------------------


def audit_framework(root: Path) -> list[Check]:
    """Classify every golden-managed file as ok / missing / drifted / suppressed.

    Reuses the golden tree that `grm-workflow-bootstrap` restores from, so the
    canonical file set is never duplicated here. Mirrors the bootstrap
    MISSING / PRISTINE / CUSTOMISED / DRIFTED taxonomy, collapsing PRISTINE and
    CUSTOMISED into "ok", and adds three suppression statuses for legitimate
    divergence (seed-diverged / paradigm / newer-than-golden) so a healthy
    install reports zero false MISSING/DRIFTED.
    """
    checks: list[Check] = []
    gen = _load_generate_golden(root)
    if gen is None:
        # A missing helper is a WARN, not a hard FAIL: the rest of the audit
        # (upstream, base) is still useful. (#175)
        return [Check("golden-baseline", "warn",
                      f"generate_golden helper not found at {GENERATE_GOLDEN_REL} — "
                      "framework-file audit skipped; run workflow-bootstrap")]
    try:
        golden = gen.resolve_golden(root)
    except FileNotFoundError as exc:
        # No frozen archive yet (e.g. right after adopting the generated-golden
        # feature and deleting the legacy committed tree). WARN + actionable
        # guidance, never a hard FAIL that blocks the whole audit. (#175)
        return [Check("golden-baseline", "warn",
                      f"{exc} — framework-file audit skipped; "
                      "freeze a baseline with generate_golden.py --freeze .")]

    staleness = GoldenStaleness.for_root(root, gen)
    slug = _active_paradigm_slug(root)

    for gfile in sorted(golden.rglob("*")):
        if not gfile.is_file():
            continue
        rel = gfile.relative_to(golden)
        live = live_path_for(root, rel)
        rel_str = str(rel).replace("\\", "/")
        if not live.exists():
            checks.append(Check(rel_str, "missing",
                                f"absent at {live.relative_to(root)} "
                                "(restore via workflow-bootstrap --restore)"))
            continue
        # Files known to carry project-specific values are expected to differ.
        if rel.name in EXPECTED_CUSTOM or rel_str in EXPECTED_CUSTOM:
            checks.append(Check(rel_str, "ok", "present (project-customised)"))
            continue
        if _bytes_equal(gfile, live):
            checks.append(Check(rel_str, "ok", "present, matches golden"))
            continue
        # --- byte mismatch: classify before flagging DRIFTED -----------------
        checks.append(_classify_divergence(root, rel_str, gfile, live, slug, staleness))
    return checks


def _classify_divergence(root: Path, rel_str: str, gfile: Path, live: Path,
                         slug: str | None, staleness: GoldenStaleness) -> Check:
    """A live file differs from golden: decide whether it is real drift.

    Suppression order (most specific first):
      1. SEED-DIVERGED — project-owned seed file (version-history, vendor.toml).
      2. PARADIGM       — live matches the active work-paradigm's variant.
      3. NEWER-THAN-GOLDEN — live is ahead of the golden freeze (post-sync).
    Otherwise DRIFTED.
    """
    if rel_str in SEED_OWNED:
        return Check(rel_str, STATUS_SEED_DIVERGED,
                     "project-owned seed file — divergence from the golden stub "
                     "is expected (not drift)")
    if rel_str in PARADIGM_SKILLS and slug:
        src = root / ".claude" / "paradigms" / slug / PARADIGM_SKILLS[rel_str]
        if src.exists() and _bytes_equal(src, live):
            return Check(rel_str, STATUS_PARADIGM,
                         f"matches the active '{slug}' paradigm variant "
                         "(golden holds the generic default — not drift)")
    if staleness.is_newer(live):
        return Check(rel_str, STATUS_NEWER,
                     "live file is newer than the golden baseline — likely a "
                     "post-sync advance; re-freeze golden, do NOT --repair")
    return Check(rel_str, "drifted",
                 "differs from golden — review; "
                 "workflow-bootstrap will diff and confirm "
                 "before any overwrite")


def live_path_for(root: Path, rel: Path) -> Path:
    """Map a golden-relative path to its live location.

    Delegates to generate_golden's bidirectional FlavorLayout (the single mapping
    authority) so every golden member — including mcp-servers/, stealth/,
    quick-start-templates/, mcp.json, grimoire-files.json — maps correctly.
    """
    gen = _load_generate_golden(root)
    if gen is not None:
        return root / gen.FlavorLayout().golden_to_repo(rel.as_posix())
    # Fallback if the helper is unavailable: pass through at repo root.
    return root / rel


def _bytes_equal(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Audit: upstream connection (inputs sync-from-upstream consumes)
# ---------------------------------------------------------------------------


def audit_upstream(root: Path, check_network: bool) -> tuple[list[Check], str | None, str | None]:
    """Validate .scaffold-upstream.conf and optionally reachability.

    Returns (checks, upstream_repo, upstream_ref). Does not clone or merge —
    that is sync-from-upstream's job. Reachability is a non-mutating `git
    ls-remote` probe.
    """
    checks: list[Check] = []
    conf_path = root / UPSTREAM_CONF
    if not conf_path.exists():
        checks.append(Check(UPSTREAM_CONF, "missing",
                            "absent — seed via workflow-bootstrap (Step 2.5) "
                            "or sync-from-upstream Step 1"))
        return checks, None, None

    conf = read_conf(conf_path)
    repo = conf.get("UPSTREAM_REPO", "").strip()
    ref = conf.get("UPSTREAM_REF", "").strip() or None

    if not repo:
        checks.append(Check("UPSTREAM_REPO", "fail",
                            f"present in {UPSTREAM_CONF} but empty — "
                            "set the upstream URL"))
        return checks, None, ref
    if not UPSTREAM_URL_RE.match(repo) and not (root / repo).exists():
        checks.append(Check("UPSTREAM_REPO", "warn",
                            f"value '{repo}' is neither a URL/scp-path nor a "
                            "local path that exists — verify it"))
    else:
        checks.append(Check("UPSTREAM_REPO", "ok",
                            f"{repo}{f' @ {ref}' if ref else ''}"))

    if check_network and repo:
        checks.append(_probe_reachable(root, repo, ref))
    elif repo:
        checks.append(Check("UPSTREAM_REPO reachability", "warn",
                            "skipped (--no-network)"))
    return checks, repo, ref


def _probe_reachable(root: Path, repo: str, ref: str | None) -> Check:
    """Non-mutating reachability probe. Local path → existence; URL → ls-remote."""
    local = (root / repo)
    if local.exists() or Path(repo).exists():
        return Check("UPSTREAM_REPO reachability", "ok", "local path exists")
    if shutil.which("git") is None:
        return Check("UPSTREAM_REPO reachability", "warn",
                     "git not on PATH — cannot probe")
    cmd = ["git", "ls-remote", "--exit-code", repo]
    if ref:
        cmd.append(ref)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Check("UPSTREAM_REPO reachability", "fail",
                     f"probe failed: {exc}")
    if res.returncode == 0:
        return Check("UPSTREAM_REPO reachability", "ok",
                     "reachable (git ls-remote)")
    return Check("UPSTREAM_REPO reachability", "fail",
                 f"unreachable (git ls-remote rc={res.returncode}): "
                 f"{res.stderr.strip().splitlines()[-1] if res.stderr.strip() else 'no detail'}")


# ---------------------------------------------------------------------------
# Audit: .scaffold-base consistency (sync provenance)
# ---------------------------------------------------------------------------


def audit_base(root: Path) -> list[Check]:
    """Check that the sync base snapshot is present and non-trivial.

    sync-from-upstream needs `.scaffold-base/` as the merge base. Absence means
    every differing file would report REVIEW on the next sync (no 3-way merge).
    Repair is `sync-from-upstream.sh --adopt-base`, not anything this helper does.
    """
    base = root / BASE_ROOT
    if not base.is_dir():
        return [Check(BASE_ROOT, "warn",
                      "absent — no sync provenance; "
                      "establish via sync-from-upstream.sh --adopt-base")]
    files = [p for p in base.rglob("*") if p.is_file()]
    if not files:
        return [Check(BASE_ROOT, "warn",
                      "present but empty — re-run "
                      "sync-from-upstream.sh --adopt-base")]
    return [Check(BASE_ROOT, "ok",
                  f"present ({len(files)} file(s) recorded)")]


# ---------------------------------------------------------------------------
# Audit: architecture-rules.json adoption (#314 — non-silent absent-ruleset gate)
# ---------------------------------------------------------------------------


def audit_architecture_rules(root: Path) -> list[Check]:
    """Notice-only check that a project has adopted (or explicitly declined)
    the architecture-fitness ruleset — the scaffold-default half of #314.

    `grm-architecture-audit` itself already emits a visible WARN when
    `.claude/architecture-rules.json` is absent (never silent); this check
    surfaces the same fact at the install-doctor level so it shows up in the
    overall health report without requiring a separate audit invocation. A
    `warn` here is informational (not a PROBLEM_STATUSES member) — it never
    drives the exit code, matching the underlying skill's report-only default.
    """
    rules_path = root / ARCHITECTURE_RULES_FILE
    if not rules_path.is_file():
        return [Check(ARCHITECTURE_RULES_FILE, "warn",
                      "absent — architecture fitness rules not adopted; "
                      "copy a per-family starter from "
                      ".claude/quick-start-templates/{service,web,gui,lib,cli}/files/.claude/architecture-rules.json "
                      f"or {ARCHITECTURE_RULES_EXAMPLE}, or run "
                      "grm-workflow-bootstrap to seed the generic default")]
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Check(ARCHITECTURE_RULES_FILE, "warn",
                      f"present but unreadable/malformed ({exc}) — "
                      "grm-architecture-audit will report this as an error")]
    if isinstance(data, dict) and data.get("opt_out"):
        reason = data.get("opt_out-reason", "")
        detail = "explicitly opted out"
        if reason:
            detail += f" (reason: {reason})"
        return [Check(ARCHITECTURE_RULES_FILE, "ok", detail)]
    return [Check(ARCHITECTURE_RULES_FILE, "ok", "present — architecture fitness rules adopted")]


# ---------------------------------------------------------------------------
# Audit: Justfile contract (required recipes present and non-placeholder)
# ---------------------------------------------------------------------------


def _load_recipes_targets(root: Path) -> dict | None:
    """Return the `.claude/recipes.json` `targets` map, or None if unavailable.

    Best-effort: a missing or malformed recipes.json yields None (the audit then
    falls back to the core-trio required set). Never raises.
    """
    recipes_path = root / ".claude" / "recipes.json"
    try:
        data = json.loads(recipes_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    targets = data.get("targets")
    return targets if isinstance(targets, dict) else None


def _justfile_recipe_required(recipe: str, targets: dict | None) -> bool:
    """Is `recipe` a REQUIRED justfile recipe (its MISSING/PARTIAL is a problem)?

    - A target marked **implemented** in `.claude/recipes.json` whose command
      routes to `just <recipe>` is required — this is exactly the
      "recipe.py <t> ≡ just <t> for every implemented target" contract. Implemented
      via a non-`just` command ⇒ the justfile is not the dispatch surface ⇒ advisory.
    - The **core trio** (build/run/deploy) is required by the base Justfile
      standard §2 UNLESS recipes.json explicitly declares the target absent
      (`implemented:false`, `command:null`) — a framework/lib stack that
      legitimately has no build/run/deploy. No recipes.json ⇒ the core trio is
      required (legacy behaviour), everything else advisory.
    """
    target_key = JUSTFILE_RECIPE_TO_TARGET.get(recipe, recipe)
    entry = targets.get(target_key) if isinstance(targets, dict) else None
    # Implemented + routed through `just <recipe>` → required.
    if isinstance(entry, dict) and entry.get("implemented"):
        command = entry.get("command") or ""
        return command.split()[:2] == ["just", recipe]
    # Not implemented. Core trio is required unless recipes.json explicitly
    # declares the target absent (command:null) for this stack.
    if recipe in JUSTFILE_CORE_RECIPES:
        if (isinstance(entry, dict) and entry.get("implemented") is False
                and not entry.get("command")):
            return False
        return True
    return False


def audit_justfile(root: Path, strict: bool = False) -> list[Check]:
    """Audit the FULL build-recipe vocabulary in the repo's justfile (RSS-3, #321).

    Each recipe (canonical justfile name — INTERFACE `server` surfaces as `run`)
    is classified OK / PARTIAL / MISSING:
      OK      — recipe line found at start-of-line AND body has no placeholder.
      PARTIAL — recipe found but body contains '# grimoire:placeholder'.
      MISSING — no recipe line found (or no justfile exists at all).

    Required recipes (the core trio build/run/deploy, plus any target that
    `.claude/recipes.json` marks implemented-and-routed-to-`just <recipe>`) drive
    exit 1 on MISSING/PARTIAL. Every other vocabulary recipe is ADVISORY —
    reported for coverage visibility but never a health problem (a lib/cli/
    framework stack legitimately omits deploy/package/smoke/etc.). See
    docs/design/justfile-standard-design.md for the contract.

    On top of that presence/placeholder classification, every PRESENT recipe
    (OK, PARTIAL, or ADVISORY-PARTIAL — anywhere a header line was actually
    found) among the 14 non-delegating vocabulary recipes also gets a
    SIGNATURE-LEVEL check (#433, see JUSTFILE_STANDARD_SIGNATURES): its
    parameter list is parsed and compared against the standard contract, with
    `deploy`'s env-defaults-to-prod case reported as its own always-blocking
    `deploy-prod-default` finding and every other mismatch reported as a
    `sig-mismatch` finding (WARN by default; `fail` under `strict`).
    """
    targets = _load_recipes_targets(root)
    justfile_path = root / "justfile"
    if not justfile_path.exists():
        # No justfile at all — every vocabulary recipe is MISSING (required ones
        # are a problem; the rest advisory). Nothing to parse for signatures.
        checks: list[Check] = []
        for recipe in JUSTFILE_FULL_VOCABULARY:
            required = _justfile_recipe_required(recipe, targets)
            status = "missing" if required else STATUS_ADVISORY_MISSING
            checks.append(Check(
                f"justfile:{recipe}", status,
                f"no justfile found — recipe '{recipe}' absent. "
                "See docs/design/justfile-standard-design.md for the contract."))
        return checks

    try:
        lines = justfile_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [Check("justfile", "fail", f"cannot read justfile: {exc}")]

    checks = []
    for recipe in JUSTFILE_FULL_VOCABULARY:
        status, detail = _classify_justfile_recipe(recipe, lines)
        required = _justfile_recipe_required(recipe, targets)
        if not required and status == "missing":
            status = STATUS_ADVISORY_MISSING
            detail = (f"recipe '{recipe}' absent (advisory — not wired to "
                      "`just {0}` in .claude/recipes.json).".format(recipe))
        elif not required and status == "partial":
            status = STATUS_ADVISORY_PARTIAL
            detail = (f"recipe '{recipe}' is a grimoire:placeholder stub "
                      "(advisory — implement it when the project needs it).")
        checks.append(Check(f"justfile:{recipe}", status, detail))

        # Signature-level conformance (#433) — independent of the presence/
        # placeholder status above; runs whenever the recipe's header line was
        # actually found (i.e. status is NOT missing/advisory-missing), for
        # the 14 non-delegating vocabulary recipes only.
        if (status not in ("missing", STATUS_ADVISORY_MISSING)
                and recipe in JUSTFILE_STANDARD_SIGNATURES):
            header_line = _find_justfile_header_line(recipe, lines)
            if header_line is not None:
                checks.extend(_audit_justfile_signature(recipe, header_line, strict))
    return checks


def _classify_justfile_recipe(recipe: str, lines: list[str]) -> tuple[str, str]:
    """Classify a single justfile recipe as ok / partial / missing.

    Detection:
      - MISSING if no line starts with '{recipe}' (bare word at column 0,
        followed by optional whitespace/colon/parameters).
      - PARTIAL if the recipe is found but any line in its body (the indented
        lines immediately following the recipe header, up to the next recipe or
        blank line) contains JUSTFILE_PLACEHOLDER_MARKER.
      - OK otherwise.

    A justfile recipe header is a line that begins with the recipe name at
    column 0, optionally followed by parameters and a colon. We use a simple
    regex that matches `^<name>` followed by a non-alphanumeric character (or
    end-of-line) to avoid false positives from recipe names that share a prefix
    (e.g. 'build' vs 'build-release').
    """
    recipe_re = re.compile(r"^" + re.escape(recipe) + r"(?:\s|:|$)")
    recipe_line_idx: int | None = None
    for i, line in enumerate(lines):
        if recipe_re.match(line):
            recipe_line_idx = i
            break

    if recipe_line_idx is None:
        return (
            "missing",
            f"recipe '{recipe}' not found in justfile. "
            "See docs/design/justfile-standard-design.md for the contract.",
        )

    # Collect the recipe body: indented lines immediately following the header,
    # until we hit a blank line or a new recipe (non-indented non-comment line).
    body_lines: list[str] = []
    for line in lines[recipe_line_idx + 1:]:
        # A blank line ends the recipe body.
        if not line.strip():
            break
        # A non-indented line that is not a comment signals a new recipe.
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        body_lines.append(line)

    # Check whether any body line contains the placeholder marker.
    for body_line in body_lines:
        if JUSTFILE_PLACEHOLDER_MARKER in body_line:
            return (
                "partial",
                f"recipe '{recipe}' has a grimoire:placeholder body — "
                "implement the recipe for this project.",
            )

    return "ok", f"recipe '{recipe}' present and non-placeholder"


def _find_justfile_header_line(recipe: str, lines: list[str]) -> str | None:
    """Return the full header line for `recipe`'s definition in `lines`, or
    None if the recipe is not present. Uses the exact same anchor regex as
    `_classify_justfile_recipe` so header lookup and presence/placeholder
    classification can never disagree about which line is the header.
    """
    recipe_re = re.compile(r"^" + re.escape(recipe) + r"(?:\s|:|$)")
    for line in lines:
        if recipe_re.match(line):
            return line
    return None


def _split_justfile_header_at_colon(text: str) -> tuple[str, str]:
    """Split a justfile recipe header at the first top-level ':' — a colon
    that is not inside a double-quoted default value. Standard-vocabulary
    default values never contain a colon, so this simple quote-tracking scan
    is sufficient (no need for a full justfile grammar). Returns
    `(before_colon, after_colon)`; if no top-level colon is found, returns
    `(text, "")`.
    """
    in_quotes = False
    for i, ch in enumerate(text):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ':' and not in_quotes:
            return text[:i], text[i + 1:]
    return text, ""


def _parse_justfile_recipe_signature(
        recipe: str, header_line: str) -> list[tuple[str, str | None, bool]]:
    """Parse a justfile recipe header's parameter list into `(name, default,
    variadic)` tuples, in declaration order. `default` is None for a
    positional/required parameter (no `="..."` clause); `variadic` is True
    for a `*name` splat parameter. Any recipe dependencies after the header's
    top-level `:` are ignored (out of scope — this checks the recipe's own
    contract, not its dependency graph).
    """
    params_text = header_line[len(recipe):]
    params_text, _deps = _split_justfile_header_at_colon(params_text)
    params: list[tuple[str, str | None, bool]] = []
    for m in _JUSTFILE_PARAM_RE.finditer(params_text):
        name, default = m.group(1), m.group(2)
        variadic = name.startswith("*")
        params.append((name.lstrip("*"), default, variadic))
    return params


def _format_justfile_param(name: str, default: str | None, variadic: bool) -> str:
    if variadic:
        return f"*{name}"
    if default is None:
        return name
    return f'{name}="{default}"'


def _format_justfile_signature(
        recipe: str, params: list[tuple[str, str | None, bool]]) -> str:
    parts = [_format_justfile_param(n, d, v) for n, d, v in params]
    return recipe + ((" " + " ".join(parts)) if parts else "")


def _deploy_prod_default(
        actual: list[tuple[str, str | None, bool]]) -> str | None:
    """Return the offending default value if the FIRST parsed parameter is
    named `env` and carries a default resolving to a production environment
    (JUSTFILE_PROD_ENV_DEFAULTS), else None. Only meaningful for `deploy`;
    callers gate on `recipe == "deploy"`.
    """
    if not actual:
        return None
    name, default, variadic = actual[0]
    if name != "env" or variadic or default is None:
        return None
    if default.strip().lower() in JUSTFILE_PROD_ENV_DEFAULTS:
        return default
    return None


def _diff_justfile_signature(
        recipe: str,
        actual: list[tuple[str, str | None, bool]]) -> list[str]:
    """Compare `actual` (parsed from the justfile) against the standard
    signature for `recipe` (JUSTFILE_STANDARD_SIGNATURES). Returns a list of
    human-readable mismatch descriptions — missing/extra parameters, wrong
    name, wrong required/default status, wrong default value; empty means
    conformant. Order-sensitive: agents/CI invoke recipes positionally
    (justfile-standard-design.md §2), so a reordering is itself a mismatch.
    Recipes outside JUSTFILE_STANDARD_SIGNATURES (sync-deps/vendor-check) are
    not signature-checked — always returns [] for them.
    """
    standard = JUSTFILE_STANDARD_SIGNATURES.get(recipe)
    if standard is None:
        return []
    problems: list[str] = []
    if len(actual) != len(standard):
        verb = "extra" if len(actual) > len(standard) else "missing"
        problems.append(
            f"{verb} parameter(s) — expected "
            f"`{_format_justfile_signature(recipe, standard)}` "
            f"({len(standard)} param(s)), found "
            f"`{_format_justfile_signature(recipe, actual)}` ({len(actual)} param(s))")
        return problems  # length mismatch makes positional diffing unreliable
    for i, (exp, act) in enumerate(zip(standard, actual), start=1):
        exp_name, exp_default, exp_variadic = exp
        act_name, act_default, act_variadic = act
        if act_variadic != exp_variadic:
            problems.append(
                f"parameter {i} ('{act_name}') should "
                f"{'be variadic (*' + exp_name + ')' if exp_variadic else 'not be variadic'}")
            continue
        if exp_variadic:
            continue  # a variadic parameter's own name is not contract-fixed
        if act_name != exp_name:
            problems.append(f"parameter {i} named '{act_name}', expected '{exp_name}'")
        if exp_default is None and act_default is not None:
            problems.append(
                f"parameter '{exp_name}' has default \"{act_default}\" but the "
                "standard requires it positional/required (no default)")
        elif exp_default is not None and act_default is None:
            problems.append(
                f"parameter '{exp_name}' is positional/required but the "
                f"standard default is \"{exp_default}\"")
        elif exp_default is not None and act_default != exp_default:
            problems.append(
                f"parameter '{exp_name}' default is \"{act_default}\", "
                f"expected \"{exp_default}\"")
    return problems


def _audit_justfile_signature(
        recipe: str, header_line: str, strict: bool) -> list[Check]:
    """Signature-level Checks for one PRESENT recipe (#433). Emits at most
    two Checks: a `deploy`-only safety-critical `deploy-prod-default` finding
    (block-tier ALWAYS, independent of `strict` — a member of
    PROBLEM_STATUSES), and a general `sig-mismatch` finding for any other
    parameter mismatch (WARN-tier; escalates to `fail` — also a
    PROBLEM_STATUSES member — only under `strict`).
    """
    actual = _parse_justfile_recipe_signature(recipe, header_line)
    checks: list[Check] = []
    if recipe == "deploy":
        prod_default = _deploy_prod_default(actual)
        if prod_default is not None:
            checks.append(Check(
                f"justfile:{recipe}:prod-default", "deploy-prod-default",
                f"'deploy' recipe's `env` parameter defaults to "
                f"\"{prod_default}\" — a bare `just deploy` would silently "
                "deploy to PRODUCTION. justfile-standard-design.md §2 "
                "requires `env` positional/required (no default) precisely "
                "to prevent this (real fleet incident: goon-cave's `deploy` "
                "defaulted to prod). Block-tier always, independent of "
                "--strict — fix immediately."))
    mismatches = _diff_justfile_signature(recipe, actual)
    if mismatches:
        status = "fail" if strict else "sig-mismatch"
        standard = JUSTFILE_STANDARD_SIGNATURES.get(recipe, [])
        checks.append(Check(
            f"justfile:{recipe}:signature", status,
            f"signature mismatch — expected "
            f"`{_format_justfile_signature(recipe, standard)}`, found "
            f"`{_format_justfile_signature(recipe, actual)}`: " + "; ".join(mismatches)))
    return checks


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def audit_release_readiness(root: Path) -> list[Check]:
    """WARN when the repo has no implemented `release` recipe target (v3.90).

    A repo with no release ceremony never reaches a clean dev==main release
    boundary, which is what keeps framework syncs autonomous-safe (BMI-3) —
    7/12 repos in the 2026-07-11 fleet wave had none. Warn-level only (`warn`
    is not in PROBLEM_STATUSES): always visible, never fails the audit — a
    docs-only or scratch repo may legitimately never release.
    """
    targets = _load_recipes_targets(root)
    entry = targets.get("release") if isinstance(targets, dict) else None
    if isinstance(entry, dict) and entry.get("implemented") and entry.get("command"):
        return [Check("release-readiness", "ok",
                      "recipes.json wires an implemented `release` target.")]
    return [Check(
        "release-readiness", "warn",
        "no implemented `release` target in .claude/recipes.json — a repo "
        "without a release ceremony never reaches the clean dev==main boundary "
        "that keeps framework syncs autonomous-safe (BMI-3). Wire `just "
        "release` (reference ceremony: quick-start-templates/web/files/scripts/"
        "release.sh) or restub with `recipe.py --generate <stack>` — every "
        "stack preset pre-fills `release` as of v3.90.")]


def audit_environments_adoption(root: Path) -> list[Check]:
    """WARN when a web app declares no `environments` block (v3.94 #439).

    The shared, fail-closed config-loader module now ships (config-loader-
    design.md; reference copy: quick-start-templates/web/files/src/
    config_loader.rs) implementing the defaults->base->{APP_ENV}->local->env
    layer order + the fail-closed APP_ENV boot check. A web app with no
    `environments` block cannot adopt it. Warn-level only (`warn` is not in
    PROBLEM_STATUSES): the originating issue's own phasing reserves a strict
    block for a LATER release, once repos have had time to adopt the
    now-shipped loader — this release only ships the loader itself.
    """
    cfg = _read_grimoire_config(root)
    wa = cfg.get("web-app")
    is_web_app = isinstance(wa, dict) and wa.get("value") == "yes"
    if not is_web_app:
        return [Check(
            "environments-adoption", "ok",
            "not a web app (web-app.value != \"yes\") — no environments "
            "block required.")]
    envs = cfg.get("environments")
    if isinstance(envs, dict) and envs:
        return [Check(
            "environments-adoption", "ok",
            f"environments block declared ({', '.join(sorted(envs))}).")]
    return [Check(
        "environments-adoption", "warn",
        "web-app.value=\"yes\" but no `environments` block is declared in "
        "grimoire-config.json — this app cannot adopt the shared fail-closed "
        "APP_ENV config-loader (config-loader-design.md; reference copy: "
        "quick-start-templates/web/files/src/config_loader.rs). Declare the "
        "environments block (deploy-environment-design.md §1) and copy the "
        "loader module in.")]


# ---------------------------------------------------------------------------
# Audit: fixtures/ convention adoption (#438 — presence/convention check, NOT
# a live probe against a running datastore; see
# docs/design/fixtures-convention-design.md).
# ---------------------------------------------------------------------------

FIXTURES_DIR = "fixtures"
FIXTURE_MANIFEST_NAME = "manifest.json"
_FIXTURE_VALID_FAMILIES = frozenset({"sql", "json"})
_FIXTURE_VALID_STRATEGIES = frozenset({"truncate-and-load", "upsert"})


def _web_app_declared(cfg: dict) -> bool:
    """True iff grimoire-config.json's web-app.value is 'yes'."""
    wa = cfg.get("web-app")
    val = wa.get("value") if isinstance(wa, dict) else wa
    return isinstance(val, str) and val.strip().lower() == "yes"


def _fixtures_dir_malformed(fixtures_dir: Path) -> str | None:
    """Return a short reason string if `fixtures/` is malformed, else None.

    Static, offline shape validation only — mirrors recipe.py's
    load_fixture_manifest schema check without executing anything (no
    datastore access). Presence/convention, not a live probe.
    """
    sets = [p for p in fixtures_dir.iterdir() if p.is_dir()]
    if not sets:
        return "fixtures/ exists but declares no fixture-set subdirectories"
    for set_dir in sets:
        manifest_path = set_dir / FIXTURE_MANIFEST_NAME
        if not manifest_path.is_file():
            return f"fixture set '{set_dir.name}' has no {FIXTURE_MANIFEST_NAME}"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return (f"fixture set '{set_dir.name}' {FIXTURE_MANIFEST_NAME} is "
                    f"unreadable/invalid: {exc}")
        if data.get("family") not in _FIXTURE_VALID_FAMILIES:
            return (f"fixture set '{set_dir.name}' {FIXTURE_MANIFEST_NAME}: "
                    f"'family' must be one of {sorted(_FIXTURE_VALID_FAMILIES)}")
        if data.get("strategy") not in _FIXTURE_VALID_STRATEGIES:
            return (f"fixture set '{set_dir.name}' {FIXTURE_MANIFEST_NAME}: "
                    f"'strategy' must be one of {sorted(_FIXTURE_VALID_STRATEGIES)}")
        apply_cmd = data.get("apply")
        if not apply_cmd or "{file}" not in apply_cmd:
            return (f"fixture set '{set_dir.name}' {FIXTURE_MANIFEST_NAME}: "
                    "'apply' must contain a {file} placeholder")
    return None


def _seed_recipe_implemented(root: Path) -> bool:
    """True iff the justfile's `seed` recipe is present and non-placeholder.

    Reuses the same body-classification `audit_justfile` uses for every other
    recipe, so "implemented" means the same thing here as everywhere else in
    the justfile contract.
    """
    justfile_path = root / "justfile"
    if not justfile_path.exists():
        return False
    try:
        lines = justfile_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    status, _detail = _classify_justfile_recipe("seed", lines)
    return status == "ok"


def audit_fixtures_convention(root: Path) -> list[Check]:
    """WARN when a web-app repo has adopted neither `fixtures/` nor a real
    `seed` recipe (#438). Never a hard failure — same advisory severity class
    as `audit_release_readiness` — a project may legitimately not be ready
    for seeded dev data yet. Only applies when web-app.value: yes; silent
    'ok' otherwise (mirrors the no-claims pattern in audit_hook_contracts).
    """
    cfg = _read_grimoire_config(root)
    if not _web_app_declared(cfg):
        return [Check("fixtures-convention", "ok",
                      "web-app.value is not 'yes' — fixtures/ convention "
                      "does not apply")]
    fixtures_dir = root / FIXTURES_DIR
    has_fixtures_dir = fixtures_dir.is_dir()
    seed_implemented = _seed_recipe_implemented(root)
    if has_fixtures_dir:
        malformed = _fixtures_dir_malformed(fixtures_dir)
        if malformed:
            return [Check("fixtures-convention", "warn",
                          f"fixtures/ present but malformed: {malformed}. See "
                          "docs/design/fixtures-convention-design.md.")]
    if has_fixtures_dir or seed_implemented:
        bits = []
        if has_fixtures_dir:
            bits.append(f"fixtures/ present ({len(list(fixtures_dir.iterdir()))} set(s))")
        if seed_implemented:
            bits.append("justfile `seed` recipe implemented (non-placeholder)")
        return [Check("fixtures-convention", "ok", "; ".join(bits))]
    return [Check(
        "fixtures-convention", "warn",
        "web-app.value: yes but no fixtures/ directory and no implemented "
        "`seed` recipe — see docs/design/fixtures-convention-design.md to "
        "adopt the convention (fixtures/<set>/manifest.json + `just seed`).")]


# ---------------------------------------------------------------------------
# Audit: changelog-surface conformance (required-feature catalog Entry 2,
# CL-1..CL-6; v3.94 #437) — wraps changelog_conformance.py's standalone,
# offline probe (dial shape -> build-info snapshot -> route convention) rather
# than reimplementing it here, same wrapped-skill posture as every other
# audit_* in this module.
# ---------------------------------------------------------------------------

def audit_changelog_surface(root: Path) -> list[Check]:
    """WARN when a web app's `changelog.user-facing` dial is 'on' but no
    `grimoire-build-info.json` snapshot has ever been produced (#437) — the
    `/changelog` surface the dial promises would have nothing real to render.
    OK for a non-web-app repo, dial off/absent, or dial on with a real
    snapshot present. Warn-level only (`warn` is not in PROBLEM_STATUSES) —
    same advisory severity class as `audit_fixtures_convention` /
    `audit_release_readiness`: a project mid-adoption of the `package` recipe
    legitimately has the dial on before its first package run.
    """
    result = changelog_conformance.probe(root)
    detail = "; ".join(result.info) if result.info else "no findings"
    if result.errors:
        return [Check(
            "changelog-surface", "warn",
            "; ".join(result.errors) + " | " + detail
            + " See required-feature-catalog.md §Entry 2.")]
    return [Check("changelog-surface", "ok", detail)]


# ---------------------------------------------------------------------------
# Audit: component-registry freshness (#458) — wraps grm-component-registry's
# deterministic build engine rather than re-deriving discovery/diff logic
# here. component-catalog-architecture-design.md Pillar 1 built the registry
# but nothing ever invoked it (no closeout step, no health check); this is
# the health-check half of #458 (the closeout half wires the same engine's
# `build` verb into grm-project-release).
# ---------------------------------------------------------------------------

def audit_component_registry(root: Path) -> list[Check]:
    """WARN when a project has adopted the component-catalog convention
    (a `components/`/`lib/`-or-configured scan path exists, or a registry
    file is already present) but `.claude/component-registry.json` is either
    absent or stale relative to its sources. Reports the `uncataloged` count
    for visibility either way (#458).

    Degrades gracefully to `ok` — "no registry, none expected" — for a
    project with neither a scan path nor an existing registry file (this
    repo's own case, confirmed: no `components/`/`lib/` directory — see
    release-planning-v3.97.md §4). This mirrors the same
    `resolve_scan_paths`/`Discovery` predicate the closeout step's own
    pre-check uses, so both wiring points agree on what "adopted" means.
    """
    registry_path = root / component_registry.REGISTRY_PATH
    scan_paths = component_registry.resolve_scan_paths(str(root))
    existing_scan, _skipped = component_registry.Discovery(
        str(root), scan_paths).resolve_paths()
    if not existing_scan and not registry_path.is_file():
        return [Check(
            "component-registry", "ok",
            "no components/lib scan path (or configured component-catalog "
            "path) and no .claude/component-registry.json — no registry "
            "expected")]
    if not registry_path.is_file():
        return [Check(
            "component-registry", "warn",
            f"scan path(s) present ({', '.join(existing_scan)}) but "
            ".claude/component-registry.json is absent — run "
            "`component_registry.py build` (wired into grm-project-release "
            "closeout, #458) to seed it.")]
    try:
        result = component_registry.RegistryEngine(str(root)).build(write=False)
    except component_registry.RegistryError as exc:
        return [Check("component-registry", "warn",
                      f"registry build failed against current sources: {exc}")]
    diff = result["diff"]
    stale = bool(diff["added"] or diff["changed"] or diff["removed"])
    uncataloged_count = len(result["registry"]["uncataloged"])
    detail = (f"added={len(diff['added'])} changed={len(diff['changed'])} "
              f"removed={len(diff['removed'])} uncataloged={uncataloged_count}")
    if stale:
        return [Check(
            "component-registry", "warn",
            f"registry is stale vs current sources ({detail}) — run "
            "`component_registry.py build` to refresh (normally handled by "
            "the grm-project-release closeout step, #458).")]
    return [Check("component-registry", "ok", detail)]


# ---------------------------------------------------------------------------
# Audit: required-feature-catalog conformance (#434) — wires
# catalog_conformance.py's plan() into the health audit so a filed
# `Grimoire-Requirement` obligation is actually re-checked, not just tracked
# by a ticket that never gets revisited. WARN by default; --strict escalates
# a real finding to a blocking `fail`, mirroring #433's `sig-mismatch` /
# #459's `uncataloged_gate.py` severity-ramp precedent in this same release.
# Never escalates an `exempt` (blocked-on-upstream, no upstream artifact to
# probe) or `degraded` (probe module unavailable in this flavor) finding —
# only a real conformance failure against an entry whose upstream artifact
# genuinely exists blocks under --strict.
# ---------------------------------------------------------------------------

def _resolve_catalog_family(root: Path) -> str | None:
    """Best-effort family resolution for the catalog-conformance audit.

    Deterministic family detection is explicitly out of the required-
    feature-catalog's own scope (required-feature-catalog.md §Family gate:
    "Family detection itself is out of this catalog's scope... it is
    grm-quick-start-template §1 / the Q9 signal table's job" — an
    agent/prose-driven surface with no deterministic detector,
    release-planning-v3.97.md §5's ITEM-10 follow-up). This resolver does NOT
    attempt to re-derive that signal table; it recognizes the one
    unambiguous, already-machine-readable signal a project may have
    declared: the `web-app.value` dial (present+"yes" -> family "web", the
    profile six of the catalog's eight entries exist for). Absent that dial,
    family is undetermined and the caller degrades gracefully (reports "ok",
    never a problem) rather than guessing at a family.
    """
    cfg = _read_grimoire_config(root)
    val = cfg.get("web-app")
    if isinstance(val, dict):
        val = val.get("value")
    if isinstance(val, dict):
        val = val.get("value")
    if isinstance(val, str) and val.strip().lower() == "yes":
        return "web"
    return None


# Actions catalog_conformance.plan() can report that represent a real,
# actionable finding (as opposed to not-applicable/exempt/ok, which are
# always informational, or degraded, which is a tooling gap never a
# conformance failure). Deliberately WIDER than catalog_conformance.py's own
# `PROBLEM_ACTIONS` (which excludes "warn") — this health-audit wiring is
# what implements the WARN-by-default / --strict-escalates severity ramp
# this feature's acceptance criteria call for, so a soft "warn" finding
# belongs here even though it never fails catalog_conformance.py's own bare
# `plan` CLI (which has no --strict flag of its own). See
# catalog_conformance.py's `PROBLEM_ACTIONS` docstring for its half of this
# split.
_CATALOG_CONFORMANCE_PROBLEM_ACTIONS = frozenset({"warn", "fail", "unregistered"})


def audit_catalog_conformance(root: Path, strict: bool = False) -> list[Check]:
    """WARN (fail under --strict) per catalog entry that is applicable to
    this repo's resolved family and fails its deterministic conformance
    probe (#434). `ok` for not-applicable/exempt/conformant entries; `warn`
    (never escalated by --strict) when a probe script is unavailable in this
    flavor (a degrade, not a conformance failure) or when no family signal
    exists at all.
    """
    family = _resolve_catalog_family(root)
    if family is None:
        return [Check(
            "catalog-conformance", "ok",
            "no web-app.value dial declared — family undetermined "
            "(deterministic family detection is out of the catalog's own "
            "scope); catalog-conformance audit skipped. See "
            "required-feature-catalog.md §Family gate.")]
    try:
        results = catalog_conformance.plan(str(root), family)
    except catalog_conformance.catalog_filing.CatalogError as exc:
        return [Check("catalog-conformance", "warn",
                       f"required-feature-catalog unreadable: {exc}")]

    checks: list[Check] = []
    for r in results:
        key, action, detail = r["key"], r["action"], r["detail"]
        name = f"catalog-conformance:{key}"
        if action == "degraded":
            checks.append(Check(name, "warn", f"probe unavailable: {detail}"))
        elif action in _CATALOG_CONFORMANCE_PROBLEM_ACTIONS:
            status = "fail" if strict else "catalog-conformance"
            checks.append(Check(name, status, detail))
        else:  # not-applicable, exempt, ok
            checks.append(Check(name, "ok", detail))
    return checks


# ---------------------------------------------------------------------------
# Audit: hook capability contracts (config claims vs installed HOOK_CONTRACT
# stamps, issue #441)
# ---------------------------------------------------------------------------
#
# v3.90 shipped the MECHANISM half (atomic-replace hooks). This is the
# CONTRACT half: each shipped guard hook now carries a machine-readable
# `# HOOK_CONTRACT: vN capabilities=[cap-a,cap-b,...]` header (a pure comment
# — see .claude/hooks/*.sh). This audit cross-checks capabilities a project's
# grimoire-config.json CLAIMS against what the installed hook actually
# declares, so a stale hook (the warden incident: autonomous-push.enabled
# claimed for weeks after push-guard.sh predated the feature) is caught
# mechanically at audit time instead of silently, weeks later.

HOOKS_DIR = ".claude/hooks"

HOOK_CONTRACT_RE = re.compile(
    r"^#\s*HOOK_CONTRACT:\s*v(\d+)\s+capabilities=\[([^\]]*)\]\s*$")


def _scalar(v: Any) -> Any:
    """Unwrap a config value that may be a bare scalar or a {"value": ...} block.

    Mirrors `.claude/hooks/_hook_common.py::_scalar` (kept as a separate,
    tiny copy rather than an import — install-doctor does not otherwise
    depend on the hooks package, and this one helper is not worth a new
    cross-directory import).
    """
    return v.get("value") if isinstance(v, dict) else v


def _read_grimoire_config(root: Path) -> dict:
    """Parse .claude/grimoire-config.json, or {} if absent/unreadable."""
    cfg_path = root / CONFIG_FILE
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _hook_contract(hook_path: Path) -> tuple[str | None, frozenset[str]]:
    """Parse the `HOOK_CONTRACT` header from a hook file's first lines.

    Returns (version, capabilities). version is e.g. "v1", or None if the
    file is absent or carries no recognizable header. capabilities is a
    frozenset of declared capability tokens (empty when the header is
    absent/unparsable) — an absent/stale hook therefore declares ZERO
    capabilities and fails every claim that names it, which is the correct
    fail-closed direction for a health check.
    """
    if not hook_path.is_file():
        return None, frozenset()
    try:
        with hook_path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 15:  # header lives in the first few lines; bound the scan
                    break
                m = HOOK_CONTRACT_RE.match(line.rstrip("\n"))
                if m:
                    version, caps = m.groups()
                    cap_set = frozenset(
                        c.strip() for c in caps.split(",") if c.strip())
                    return f"v{version}", cap_set
    except OSError:
        pass
    return None, frozenset()


def _autonomous_push_claimed(cfg: dict) -> bool:
    block = cfg.get("autonomous-push")
    return isinstance(block, dict) and block.get("enabled") is True


def _stealth_mode_on(cfg: dict) -> bool:
    return _scalar(cfg.get("stealth-mode")) == "on"


def _work_paradigm_is_noir(cfg: dict) -> bool:
    aliases = {"autonomous": "noir", "collaborative": "weiss"}
    value = _scalar(cfg.get("work-paradigm"))
    if not isinstance(value, str):
        return False
    slug = value.strip().lower()
    return aliases.get(slug, slug) == "noir"


def _post_commit_gate_claimed(cfg: dict) -> bool:
    """code-quality.post-commit-test-gate.enabled == true (#361)."""
    block = (cfg.get("code-quality") or {}).get("post-commit-test-gate")
    return isinstance(block, dict) and _scalar(block.get("enabled")) is True


def _pre_commit_block_claimed(cfg: dict) -> bool:
    """code-quality.post-commit-test-gate enabled AND mode=block (#361) — the
    OPTIONAL hard-block variant. A false sense of security (mode=block
    configured but core.hooksPath never activated, so nothing actually
    blocks) is worse than the force-correct case being inert, since the
    project believes it has a hard gate — checked with the same
    `requires_hooks_path` mechanism, not silently skipped."""
    block = (cfg.get("code-quality") or {}).get("post-commit-test-gate")
    if not isinstance(block, dict):
        return False
    return (_scalar(block.get("enabled")) is True
           and _scalar(block.get("mode")) == "block")


# Config-claim -> capability registry (issue #441). Each entry names a
# grimoire-config.json dial, the capability its predicate implies, and which
# hook file(s) implement that capability. `hooks` may list more than one file
# when a capability is enforced by more than one guard in defence-in-depth
# (e.g. autonomous-push is implemented by BOTH push-guard.sh, which suppresses
# the `git push` prompt, and autonomy-allow.sh, which suppresses the
# push-class `gh` prompts) — every listed hook must declare the capability
# for the claim to be satisfied. `requires_hooks_path` (#361) marks a claim
# whose hook(s) are REAL git hooks, not Claude Code PreToolUse hooks — those
# only fire once `git config core.hooksPath` points at HOOKS_DIR, a fact the
# byte-content / HOOK_CONTRACT-stamp check below cannot see (a byte-perfect,
# correctly-stamped hook file that git never invokes is not actually
# installed) — see audit_hook_contracts()'s `requires_hooks_path` branch.
CAPABILITY_CLAIMS: tuple[dict[str, Any], ...] = (
    {
        "claim": "autonomous-push.enabled",
        "capability": "autonomous-push",
        "hooks": ("push-guard.sh", "autonomy-allow.sh"),
        "predicate": _autonomous_push_claimed,
    },
    {
        "claim": "stealth-mode.value=on",
        "capability": "stealth-no-push",
        "hooks": ("stealth-guard.sh",),
        "predicate": _stealth_mode_on,
    },
    {
        "claim": "work-paradigm.value=Noir",
        "capability": "autonomy-allow-noir",
        "hooks": ("autonomy-allow.sh",),
        "predicate": _work_paradigm_is_noir,
    },
    {
        "claim": "code-quality.post-commit-test-gate.enabled=true",
        "capability": "post-commit-gate",
        "hooks": ("post-commit",),
        "predicate": _post_commit_gate_claimed,
        "requires_hooks_path": True,
    },
    {
        "claim": "code-quality.post-commit-test-gate.mode=block",
        "capability": "pre-commit-block-gate",
        "hooks": ("pre-commit",),
        "predicate": _pre_commit_block_claimed,
        "requires_hooks_path": True,
    },
)


def _git_hooks_path(root: Path) -> str | None:
    """`git config --get core.hooksPath` for `root`, or None if unset/unreadable."""
    try:
        out = subprocess.run(["git", "config", "--get", "core.hooksPath"],
                             cwd=str(root), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def audit_hook_contracts(root: Path) -> list[Check]:
    """Cross-check config-claimed capabilities against installed hook stamps.

    For each `CAPABILITY_CLAIMS` entry whose predicate matches the project's
    grimoire-config.json, verify that EVERY hook in its `hooks` tuple
    declares the required capability in its `HOOK_CONTRACT` header. Three
    outcomes:

      - claim-satisfied — every implementing hook declares the capability:
        an `ok` Check (visible in the report, never a problem).
      - claim-unmet — the config claims the feature but at least one
        implementing hook's stamp is missing the capability (a stale hook,
        exactly the warden incident shape): `fail` (a PROBLEM_STATUSES
        member — drives exit 1).
      - no-claims — the predicate is false for this project: nothing is
        emitted for that entry at all. The claim simply does not apply here.
    """
    cfg = _read_grimoire_config(root)
    checks: list[Check] = []
    for entry in CAPABILITY_CLAIMS:
        if not entry["predicate"](cfg):
            continue  # no-claims: skip entirely, not even an `ok` Check
        capability = entry["capability"]
        unmet = [name for name in entry["hooks"]
                 if capability not in _hook_contract(root / HOOKS_DIR / name)[1]]
        if unmet:
            checks.append(Check(
                f"hook-contract:{entry['claim']}", "fail",
                f"config claims '{entry['claim']}' but "
                f"{', '.join(unmet)} does not declare capability "
                f"'{capability}' in its HOOK_CONTRACT header — the hook is "
                "stale or predates the feature the config claims (issue "
                "#441). Re-sync the hook from upstream, or unset the claim "
                "if it no longer applies."))
        else:
            checks.append(Check(
                f"hook-contract:{entry['claim']}", "ok",
                f"claim satisfied — {', '.join(entry['hooks'])} declare "
                f"capability '{capability}'."))
        # #361: a byte-perfect, correctly-stamped REAL git hook is inert
        # unless `git config core.hooksPath` actually points git at HOOKS_DIR
        # — unlike the other 8 guard hooks (Claude Code PreToolUse, wired via
        # .claude/settings.json, need no such activation). Only checked for
        # claims that opt into this (`requires_hooks_path`); the byte-content
        # check above already covers everything else.
        if entry.get("requires_hooks_path") and not unmet:
            hooks_path = _git_hooks_path(root)
            expected = HOOKS_DIR
            if hooks_path is None:
                checks.append(Check(
                    f"hooks-path:{entry['claim']}", "fail",
                    f"config claims '{entry['claim']}' and {', '.join(entry['hooks'])} "
                    f"is present + correctly stamped, but `git config core.hooksPath` "
                    f"is unset — git never runs it. Activate with: "
                    f"git config core.hooksPath {expected}"))
            elif hooks_path.rstrip("/") != expected.rstrip("/"):
                checks.append(Check(
                    f"hooks-path:{entry['claim']}", "fail",
                    f"core.hooksPath is {hooks_path!r}, not {expected!r} — "
                    f"{', '.join(entry['hooks'])} will not run. Reconcile with: "
                    f"git config core.hooksPath {expected}"))
            else:
                checks.append(Check(
                    f"hooks-path:{entry['claim']}", "ok",
                    f"core.hooksPath = {expected} — {', '.join(entry['hooks'])} is active."))
    return checks


def build_report(root: Path, check_network: bool, strict: bool = False) -> Report:
    rep = Report(repo_root=str(root))
    rep.framework = audit_framework(root)
    up_checks, _repo, _ref = audit_upstream(root, check_network)
    rep.upstream = up_checks
    rep.base = audit_base(root)
    rep.justfile = (audit_justfile(root, strict) + audit_release_readiness(root)
                     + audit_environments_adoption(root))
    rep.architecture = audit_architecture_rules(root)
    rep.hook_contracts = audit_hook_contracts(root)
    rep.fixtures = audit_fixtures_convention(root)
    rep.changelog_surface = audit_changelog_surface(root)
    rep.component_registry = audit_component_registry(root)
    rep.catalog_conformance = audit_catalog_conformance(root, strict)
    rep.notes.append(
        "Feature-adoption is NOT audited mechanically: run each "
        "sync-from-upstream feature-manifest `detect` predicate per the SKILL.md "
        "Step 3 procedure to confirm each feature is adopted (not merely available)."
    )
    if any(c.status == STATUS_NEWER for c in rep.framework):
        rep.notes.append(
            "Some files are NEWER than the golden baseline (a recent sync "
            "advanced them). Re-freeze the baseline with "
            "`generate_golden.py --freeze .`; do NOT --repair (it would revert "
            "the sync)."
        )
    return rep


def repair_plan(rep: Report) -> list[str]:
    """Map each real-problem finding to its owning wrapped-skill action.

    Non-destructive: this returns a *plan* only. Suppressed findings
    (seed-diverged / paradigm / newer-than-golden) are never repair targets, so
    a repair can never revert synced or active-paradigm content.
    """
    steps: list[str] = []
    missing_fw = [c for c in rep.framework if c.status == "missing"]
    drifted_fw = [c for c in rep.framework if c.status == "drifted"]
    if missing_fw or drifted_fw:
        steps.append(
            f"MISSING({len(missing_fw)}) / DRIFTED({len(drifted_fw)}) framework "
            "files → invoke grm-workflow-bootstrap --restore (restores MISSING; "
            "diffs-and-confirms each DRIFTED before any overwrite).")
    for c in rep.upstream:
        if c.status == "missing" and c.name == UPSTREAM_CONF:
            steps.append(
                f"{UPSTREAM_CONF} absent → grm-workflow-bootstrap Step 2.5 "
                "re-seeds the default UPSTREAM_REPO idempotently.")
        elif c.status == "fail" and c.name == "UPSTREAM_REPO":
            steps.append(
                "UPSTREAM_REPO empty/malformed → a CONFIG problem; ask the user "
                "for the correct URL (never guess; a fork's custom upstream is "
                "legitimate).")
        elif c.status == "fail" and c.name == "UPSTREAM_REPO reachability":
            steps.append(
                "UPSTREAM_REPO unreachable → verify the URL/network; not a "
                "file to restore.")
    for c in rep.base:
        if c.status == "warn":
            steps.append(
                ".scaffold-base missing/empty → run sync-from-upstream.sh "
                "--adopt-base ONCE the project is confirmed reconciled with a "
                "known upstream commit (declares 'local matches upstream').")
    for c in rep.architecture:
        if c.status == "warn" and c.name == ARCHITECTURE_RULES_FILE:
            steps.append(
                f"{ARCHITECTURE_RULES_FILE} absent → adopt a per-family starter "
                "from .claude/quick-start-templates/{service,web,gui,lib,cli}/files/"
                f".claude/architecture-rules.json or {ARCHITECTURE_RULES_EXAMPLE} "
                "(or explicitly opt out with \"opt_out\": true + a reason) — "
                "notice-only, never a repair blocker.")
    # Justfile contract findings: required MISSING/PARTIAL need manual action;
    # advisory findings are surfaced as optional coverage recommendations.
    jf_missing = [c for c in rep.justfile if c.status == "missing"]
    jf_partial = [c for c in rep.justfile if c.status == "partial"]
    jf_advisory = [c for c in rep.justfile
                   if c.status in (STATUS_ADVISORY_MISSING, STATUS_ADVISORY_PARTIAL)]
    if jf_missing:
        recipe_names = ", ".join(c.name.split(":", 1)[-1] for c in jf_missing)
        steps.append(
            f"Justfile MISSING required recipe(s): {recipe_names} — add the "
            "recipe(s) to justfile. See docs/design/justfile-standard-design.md "
            "for the contract.")
    if jf_partial:
        recipe_names = ", ".join(c.name.split(":", 1)[-1] for c in jf_partial)
        steps.append(
            f"Justfile PARTIAL required recipe(s): {recipe_names} — replace the "
            "grimoire:placeholder body with a real implementation. "
            "See docs/design/justfile-standard-design.md for the contract.")
    if jf_advisory:
        recipe_names = ", ".join(c.name.split(":", 1)[-1] for c in jf_advisory)
        steps.append(
            f"Justfile ADVISORY recipe(s) not yet wired: {recipe_names} — OPTIONAL. "
            "Add a thin `just <recipe>` (delegating multi-line logic to scripts/) "
            "and route `.claude/recipes.json` to it when the project needs the "
            "target. Not a health failure.")
    stamp_fail = [c for c in rep.hook_contracts
                 if c.status == "fail" and c.name.startswith("hook-contract:")]
    if stamp_fail:
        claim_names = ", ".join(c.name.split(":", 1)[-1] for c in stamp_fail)
        steps.append(
            f"Hook contract UNMET claim(s): {claim_names} — a config-claimed "
            "feature's implementing hook doesn't declare the matching "
            "HOOK_CONTRACT capability (stale hook, issue #441). Re-sync "
            "`.claude/hooks/` from upstream (grm-sync-from-upstream — hooks "
            "are an atomic-replace artifact class) to pick up a hook version "
            "that declares the capability, or unset the config claim if it "
            "no longer applies. NEVER hand-edit a hook's HOOK_CONTRACT line "
            "to make the mismatch disappear without confirming the hook's "
            "actual behavior supports the claim.")
    # #361: a byte-perfect, correctly-stamped REAL git hook that git never
    # actually invokes (core.hooksPath unset/wrong) is a DIFFERENT problem
    # from a stale stamp — distinct repair guidance, not "re-sync the hook".
    path_fail = [c for c in rep.hook_contracts
                if c.status == "fail" and c.name.startswith("hooks-path:")]
    if path_fail:
        claim_names = ", ".join(c.name.split(":", 1)[-1] for c in path_fail)
        steps.append(
            f"Real git hook not activated for claim(s): {claim_names} — the "
            "hook file is present and correctly stamped, but "
            "`git config core.hooksPath` does not point git at `.claude/hooks` "
            "so it never runs. Activate with: "
            "`git config core.hooksPath .claude/hooks` (repo-wide, one-time; "
            "see grm-repo-init/SKILL.md §Post-commit test gate).")
    fx_warn = [c for c in rep.fixtures if c.status == "warn"]
    if fx_warn:
        steps.append(
            "fixtures/ convention not adopted (#438) — OPTIONAL, notice-only. "
            "Add fixtures/<set>/manifest.json (see "
            "docs/design/fixtures-convention-design.md) and wire the standard "
            "justfile `seed` recipe; `recipe.py seed` then works with no "
            "project-side script required.")
    cl_warn = [c for c in rep.changelog_surface if c.status == "warn"]
    if cl_warn:
        steps.append(
            "changelog.user-facing is 'on' with no grimoire-build-info.json "
            "snapshot (#437) — OPTIONAL, notice-only. Run the `package` "
            "recipe target (web-app-deployment-protocol.md §8) so the "
            "/changelog surface has real data instead of an honest "
            "empty-state; see required-feature-catalog.md §Entry 2.")
    cr_warn = [c for c in rep.component_registry if c.status == "warn"]
    if cr_warn:
        steps.append(
            "component-registry absent/stale (#458) — run `python3 "
            ".claude/skills/grm-component-registry/component_registry.py "
            "build` and commit `.claude/component-registry.json` if it "
            "changed; normally handled automatically by the "
            "grm-project-release closeout step.")
    cc_findings = [c for c in rep.catalog_conformance
                   if c.status in ("catalog-conformance", "fail")]
    if cc_findings:
        entry_names = ", ".join(c.name.split(":", 1)[-1] for c in cc_findings)
        steps.append(
            f"required-feature-catalog conformance finding(s) for: "
            f"{entry_names} (#434) — implementing a filed "
            f"`Grimoire-Requirement` entry is this project's own scope/"
            f"timeline (required-feature-catalog.md's SPEC framing); re-run "
            f"`python3 .claude/skills/grm-required-feature-catalog/"
            f"catalog_conformance.py plan --root . --family <family>` for "
            f"the exact failing sub-check per entry.")
    if not steps:
        steps.append("Nothing to repair — install is healthy.")
    steps.append(
        "NEVER run a `migrate` step as part of repair, and NEVER --repair "
        "newer-than-golden / paradigm / seed-diverged files (they are correct).")
    return steps


def render_markdown(rep: Report, *, plan: list[str] | None = None) -> str:
    counts = rep.counts()
    status_word = "HEALTHY" if rep.healthy else "ATTENTION NEEDED"
    lines: list[str] = []
    lines.append("# Grimoire install-doctor health report")
    lines.append("")
    lines.append(f"- Repo root: `{rep.repo_root}`")
    lines.append(f"- Overall: **{status_word}**")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no checks"
    lines.append(f"- Tallies: {summary}")
    lines.append("")

    def section(title: str, checks: list[Check]) -> None:
        lines.append(f"## {title}")
        if not checks:
            lines.append("_no checks_")
            lines.append("")
            return
        lines.append("| Item | Status | Detail |")
        lines.append("|---|---|---|")
        for c in checks:
            lines.append(f"| `{c.name}` | {c.status.upper()} | {c.detail} |")
        lines.append("")

    section("Framework files (vs workflow-bootstrap golden)", rep.framework)
    section("Upstream connection (sync-from-upstream inputs)", rep.upstream)
    section("Sync base snapshot (.scaffold-base)", rep.base)
    section("Justfile contract (full recipe vocabulary)", rep.justfile)
    section("Architecture-rules adoption (.claude/architecture-rules.json)", rep.architecture)
    section("Hook capability contracts (config claims vs installed HOOK_CONTRACT stamps)",
            rep.hook_contracts)
    section("Fixtures convention adoption (fixtures/ + seed recipe, #438)", rep.fixtures)
    section("Changelog-surface conformance (catalog Entry 2, #437)", rep.changelog_surface)
    section("Component-registry freshness (.claude/component-registry.json, #458)",
            rep.component_registry)
    section("Required-feature-catalog conformance (#434)",
            rep.catalog_conformance)

    if plan is not None:
        lines.append("## Repair plan (non-destructive — calls wrapped skills)")
        for step in plan:
            lines.append(f"- {step}")
        lines.append("")

    if rep.notes:
        lines.append("## Notes")
        for n in rep.notes:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _json_payload(rep: Report, *, plan: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repo_root": rep.repo_root,
        "healthy": rep.healthy,
        "counts": rep.counts(),
        "framework": [asdict(c) for c in rep.framework],
        "upstream": [asdict(c) for c in rep.upstream],
        "base": [asdict(c) for c in rep.base],
        "justfile": [asdict(c) for c in rep.justfile],
        "architecture": [asdict(c) for c in rep.architecture],
        "hook_contracts": [asdict(c) for c in rep.hook_contracts],
        "fixtures": [asdict(c) for c in rep.fixtures],
        "changelog_surface": [asdict(c) for c in rep.changelog_surface],
        "component_registry": [asdict(c) for c in rep.component_registry],
        "catalog_conformance": [asdict(c) for c in rep.catalog_conformance],
        "notes": rep.notes,
    }
    if plan is not None:
        payload["repair_plan"] = plan
    return payload


def cmd_audit(args: argparse.Namespace) -> int:
    root = find_repo_root(Path(args.root) if args.root else None)
    rep = build_report(root, check_network=not args.no_network,
                        strict=getattr(args, "strict", False))
    if args.json:
        print(json.dumps(_json_payload(rep), indent=2))
    else:
        print(render_markdown(rep), end="")
    return 0 if rep.healthy else 1


def cmd_repair(args: argparse.Namespace) -> int:
    """Emit the audit plus a non-destructive, ordered repair plan.

    Writes no tracked file. The plan tells the SKILL.md which wrapped skill to
    call for each real finding; suppressed (correct) divergence is never a
    target, so a repair can never revert synced/paradigm/seed content.

    With `--freeze-baseline` it FIRST (re-)freezes the golden baseline into the
    gitignored `.grimoire-golden/` cache (the one self-contained repair, #182),
    then audits against the freshly-frozen baseline so the framework-file audit
    is no longer skipped.
    """
    root = find_repo_root(Path(args.root) if args.root else None)
    froze: Path | None = None
    if getattr(args, "freeze_baseline", False):
        froze = freeze_baseline(root)
    rep = build_report(root, check_network=not args.no_network,
                        strict=getattr(args, "strict", False))
    if froze is not None:
        rep.notes.insert(0, f"Froze golden baseline -> {froze} "
                            "(gitignored; do not commit the tarball).")
    plan = repair_plan(rep)
    if args.json:
        payload = _json_payload(rep, plan=plan)
        if froze is not None:
            payload["froze_baseline"] = str(froze)
        print(json.dumps(payload, indent=2))
    else:
        if froze is not None:
            print(f"froze golden baseline -> {froze}")
        print(render_markdown(rep, plan=plan), end="")
    return 0 if rep.healthy else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install_doctor.py",
        description="Audit Grimoire framework health (read-only). The `repair` "
                    "subcommand emits a non-destructive repair plan; actual "
                    "mutation is delegated to workflow-bootstrap and "
                    "sync-from-upstream — this helper never writes files.",
    )
    p.add_argument("--root", default=None,
                   help="Repo root (default: auto-detect from cwd up).")
    p.add_argument("--self-test", action="store_true",
                   help="Run offline self-tests and exit.")
    # Back-compat for the historically documented upgrade step (#182):
    # `--repair` == `repair --freeze-baseline`. The two common modifiers are
    # accepted at the top level too so the back-compat form works standalone.
    p.add_argument("--repair", action="store_true",
                   help="Back-compat alias for `repair --freeze-baseline`: freeze "
                        "the golden baseline (gitignored), then audit.")
    p.add_argument("--json", action="store_true",
                   help="With --repair: emit JSON instead of Markdown.")
    p.add_argument("--no-network", action="store_true",
                   help="With --repair: skip the UPSTREAM_REPO reachability probe.")
    p.add_argument("--strict", action="store_true",
                   help="Escalate justfile signature-mismatch findings "
                        "(sig-mismatch) from WARN to a blocking failure. "
                        "deploy-prod-default always blocks, independent of "
                        "this flag.")
    # Subcommand is OPTIONAL — a bare invocation runs `audit` (#152).
    sub = p.add_subparsers(dest="command")

    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of Markdown.")
        sp.add_argument("--no-network", action="store_true",
                        help="Skip the UPSTREAM_REPO reachability probe.")
        sp.add_argument("--strict", action="store_true",
                        help="Escalate justfile signature-mismatch findings "
                             "(sig-mismatch) from WARN to a blocking failure. "
                             "deploy-prod-default always blocks, independent "
                             "of this flag.")

    a = sub.add_parser("audit", help="Run the read-only health audit (default).")
    _add_common(a)
    a.set_defaults(func=cmd_audit)

    r = sub.add_parser("repair",
                       help="Audit, then emit a non-destructive repair plan.")
    _add_common(r)
    r.add_argument("--freeze-baseline", action="store_true",
                   help="(Re-)freeze the golden baseline into the gitignored "
                        ".grimoire-golden/ cache from the current PRISTINE "
                        "scaffold before auditing (#182). Touches no tracked file.")
    r.set_defaults(func=cmd_repair)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "self_test", False):
        return _self_test()
    # Back-compat top-level --repair → repair --freeze-baseline (#182).
    if getattr(args, "repair", False) and getattr(args, "func", None) is None:
        args.json = getattr(args, "json", False)
        args.no_network = getattr(args, "no_network", False)
        args.freeze_baseline = True
        args.func = cmd_repair
    # No subcommand → default to a read-only audit (#152).
    if getattr(args, "func", None) is None:
        args.json = getattr(args, "json", False)
        args.no_network = getattr(args, "no_network", False)
        args.func = cmd_audit
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 2
    except Exception as exc:  # surface as a clean usage error, never a traceback
        print(f"install-doctor: error: {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# Self-test (offline, stdlib-only, tempdir fixtures)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    import tempfile
    import time
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # --- live_path_for mapping: every golden member maps to the right live path.
    # Build a minimal repo carrying generate_golden.py so live_path_for resolves
    # through the real FlavorLayout authority.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Copy the real generate_golden.py into the expected location.
        src_gen = Path(__file__).resolve().parent.parent / "grm-workflow-bootstrap" / "generate_golden.py"
        gen_dst = root / GENERATE_GOLDEN_REL
        gen_dst.parent.mkdir(parents=True, exist_ok=True)
        if src_gen.exists():
            gen_dst.write_text(src_gen.read_text(encoding="utf-8"), encoding="utf-8")
            mapping = {
                "skills/grm-install-doctor/SKILL.md": ".claude/skills/grm-install-doctor/SKILL.md",
                "hooks/push-guard.sh": ".claude/hooks/push-guard.sh",
                "mcp-servers/server.py": ".claude/mcp-servers/server.py",
                "stealth/policy.md": ".claude/stealth/policy.md",
                "quick-start-templates/web/t.json": ".claude/quick-start-templates/web/t.json",
                "mcp.json": ".mcp.json",
                "grimoire-files.json": ".claude/grimoire-files.json",
                "settings.json": ".claude/settings.json",
                "CLAUDE.md": "CLAUDE.md",
                "docs/roadmap.md": "docs/roadmap.md",
                "vendor.toml": "vendor.toml",
            }
            for golden_rel, want in mapping.items():
                got = live_path_for(root, Path(golden_rel)).relative_to(root).as_posix()
                check(got == want,
                      f"live_path_for({golden_rel}) -> {got}, want {want}")
        else:
            check(False, f"generate_golden.py not found for self-test at {src_gen}")

    # --- _classify_divergence: each suppression path + genuine drift.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)
        (root / CONFIG_FILE).write_text(
            '{"work-paradigm": {"value": "Noir"}}', encoding="utf-8")
        gfile = root / "golden_stub.txt"
        live = root / "live.txt"
        gfile.write_text("golden", encoding="utf-8")
        live.write_text("live-grown", encoding="utf-8")

        # SEED-DIVERGED: a known seed-owned path differs from golden.
        c = _classify_divergence(root, "docs/version-history.md", gfile, live,
                                 "noir", GoldenStaleness(None))
        check(c.status == STATUS_SEED_DIVERGED,
              f"version-history.md should be seed-diverged, got {c.status}")
        c = _classify_divergence(root, "vendor.toml", gfile, live,
                                 "noir", GoldenStaleness(None))
        check(c.status == STATUS_SEED_DIVERGED,
              f"vendor.toml should be seed-diverged, got {c.status}")

        # PARADIGM: live matches the active paradigm's source variant.
        para = root / ".claude" / "paradigms" / "noir"
        para.mkdir(parents=True, exist_ok=True)
        (para / "release-phase-SKILL.md").write_text("live-grown", encoding="utf-8")
        c = _classify_divergence(root, "skills/grm-release-phase/SKILL.md",
                                 gfile, live, "noir", GoldenStaleness(None))
        check(c.status == STATUS_PARADIGM,
              f"paradigm-matching file should be paradigm, got {c.status}")
        # Paradigm path but live does NOT match the variant -> falls through.
        (para / "release-phase-SKILL.md").write_text("different", encoding="utf-8")
        c = _classify_divergence(root, "skills/grm-release-phase/SKILL.md",
                                 gfile, live, "noir", GoldenStaleness(None))
        check(c.status == "drifted",
              f"non-matching paradigm file should be drifted, got {c.status}")

        # NEWER-THAN-GOLDEN: golden frozen in the past, live touched now.
        old = time.time() - 10_000
        stale = GoldenStaleness(old)
        check(stale.is_newer(live), "live file should be newer than old golden")
        c = _classify_divergence(root, "skills/grm-doc-assurance/doc_assurance.py",
                                 gfile, live, "noir", stale)
        check(c.status == STATUS_NEWER,
              f"newer-than-golden file should be {STATUS_NEWER}, got {c.status}")

        # GENUINE DRIFT: not seed, not paradigm, not newer (unresolvable golden).
        c = _classify_divergence(root, "skills/grm-foo/SKILL.md", gfile, live,
                                 "noir", GoldenStaleness(None))
        check(c.status == "drifted",
              f"genuine difference should be drifted, got {c.status}")

        # A suppression status is never a `problem`.
        for st in (STATUS_SEED_DIVERGED, STATUS_PARADIGM, STATUS_NEWER):
            check(not Check("x", st).problem, f"{st} must not be a problem")
        check(Check("x", "drifted").problem, "drifted must be a problem")

    # --- GoldenStaleness.for_root resolves the archive mtime and is
    #     conservative (False) when no baseline exists.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        empty = GoldenStaleness.for_root(root)
        check(not empty.resolvable, "no baseline -> not resolvable")
        check(not empty.is_newer(root), "unresolvable staleness never claims newer")
        cache = root / ".grimoire-golden"
        cache.mkdir(parents=True, exist_ok=True)
        arch = cache / "golden-v3.50.tar.gz"
        arch.write_bytes(b"x")
        st = GoldenStaleness.for_root(root)
        check(st.resolvable, "archive present -> resolvable")

    # --- freeze_baseline: end-to-end #182 + #186 regression. Freezing a pristine
    #     scaffold produces a v-prefixed archive the audit then discovers (no skip).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src_gen = Path(__file__).resolve().parent.parent / "grm-workflow-bootstrap" / "generate_golden.py"
        if src_gen.exists():
            gen_dst = root / GENERATE_GOLDEN_REL
            gen_dst.parent.mkdir(parents=True, exist_ok=True)
            gen_dst.write_text(src_gen.read_text(encoding="utf-8"), encoding="utf-8")
            # A pristine, golden-shaped scaffold with a no-`v` framework-version
            # (the exact #186 condition) plus a config so find_repo_root anchors here.
            (root / CONFIG_FILE).write_text(
                '{"framework-version": "3.38", "work-paradigm": {"value": "Noir"}}',
                encoding="utf-8")
            (root / "CLAUDE.md").write_text("contract", encoding="utf-8")
            sk = root / ".claude/skills/grm-build-recipe"
            sk.mkdir(parents=True, exist_ok=True)
            (sk / "SKILL.md").write_text("recipe", encoding="utf-8")

            # Before freezing, the framework audit is skipped (no baseline).
            rep_before = build_report(root, check_network=False)
            check(any(c.name == "golden-baseline" and c.status == "warn"
                      for c in rep_before.framework),
                  "expected a skipped framework audit before freezing")

            froze = freeze_baseline(root)
            check(froze.name == "golden-v3.38.tar.gz",
                  f"freeze_baseline name (v-prefixed, #186): {froze.name}")
            check(froze.exists(), "freeze_baseline did not write an archive")

            # After freezing, the audit discovers the baseline (no skip warn).
            rep_after = build_report(root, check_network=False)
            check(not any(c.name == "golden-baseline" and c.status == "warn"
                          for c in rep_after.framework),
                  "framework audit still skipped after freezing a baseline")
            check(any(c.name != "golden-baseline" for c in rep_after.framework),
                  "expected real framework-file checks after freezing")
        else:
            check(False, f"generate_golden.py not found for freeze self-test at {src_gen}")

    # --- repair_plan: suppressed findings are NEVER repair targets.
    rep = Report(repo_root="/x")
    rep.framework = [
        Check("docs/version-history.md", STATUS_SEED_DIVERGED, ""),
        Check("skills/grm-release-phase/SKILL.md", STATUS_PARADIGM, ""),
        Check("skills/grm-doc-assurance/doc_assurance.py", STATUS_NEWER, ""),
    ]
    plan = repair_plan(rep)
    joined = "\n".join(plan)
    check("MISSING" not in joined and "DRIFTED(" not in joined,
          "repair plan must not target suppressed findings")
    check(any("Nothing to repair" in s for s in plan),
          "all-suppressed report should plan no restore")
    # With a genuine drift present, the plan DOES target it.
    rep.framework.append(Check("skills/grm-foo/SKILL.md", "drifted", ""))
    plan2 = repair_plan(rep)
    check(any("DRIFTED(1)" in s for s in plan2),
          "repair plan should target the one genuine drift")

    # --- audit_justfile: full-vocabulary audit (RSS-3, #321).
    n_vocab = len(JUSTFILE_FULL_VOCABULARY)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # Case 1: no justfile, no recipes.json → full vocabulary reported; core
        # trio MISSING (problem), the rest ADVISORY-MISSING (not a problem).
        results = audit_justfile(root)
        check(len(results) == n_vocab,
              f"no-justfile: expected {n_vocab} checks, got {len(results)}")
        by_name = {c.name: c for c in results}
        for r in JUSTFILE_CORE_RECIPES:
            c = by_name[f"justfile:{r}"]
            check(c.status == "missing" and c.problem,
                  f"no-justfile: core {r} should be missing+problem, got {c.status}")
        for c in results:
            recipe = c.name.split(":", 1)[-1]
            if recipe not in JUSTFILE_CORE_RECIPES:
                check(c.status == STATUS_ADVISORY_MISSING and not c.problem,
                      f"no-justfile: {recipe} should be advisory-missing, got {c.status}")

        # Case 2: justfile with only the core trio → trio OK, extended ADVISORY.
        jf = root / "justfile"
        jf.write_text(
            "build:\n    cargo build\n\n"
            "run:\n    cargo run\n\n"
            "deploy:\n    ./deploy.sh\n",
            encoding="utf-8",
        )
        results = audit_justfile(root)
        by_name = {c.name: c for c in results}
        for r in JUSTFILE_CORE_RECIPES:
            check(by_name[f"justfile:{r}"].status == "ok",
                  f"core-ok: {r} should be ok, got {by_name[f'justfile:{r}'].status}")
        check(by_name["justfile:test"].status == STATUS_ADVISORY_MISSING,
              "core-ok: test should be advisory-missing (extended, unwired)")
        check(not by_name["justfile:test"].problem,
              "core-ok: advisory test must not be a problem")

        # Case 3: missing 'deploy' recipe → deploy MISSING + problem.
        jf.write_text("build:\n    cargo build\n\nrun:\n    cargo run\n", encoding="utf-8")
        by_name = {c.name: c for c in audit_justfile(root)}
        check(by_name["justfile:deploy"].status == "missing" and by_name["justfile:deploy"].problem,
              f"missing-deploy: deploy should be missing+problem, got {by_name['justfile:deploy'].status}")

        # Case 4: core 'build' has placeholder body → PARTIAL + problem.
        jf.write_text(
            "build:\n    # grimoire:placeholder\n    echo 'implement me'\n\n"
            "run:\n    cargo run\n\ndeploy:\n    ./deploy.sh\n",
            encoding="utf-8",
        )
        by_name = {c.name: c for c in audit_justfile(root)}
        check(by_name["justfile:build"].status == "partial" and by_name["justfile:build"].problem,
              f"partial-build: build should be partial+problem, got {by_name['justfile:build'].status}")

        # Case 5: recipes.json marks an EXTENDED target implemented + routed to
        # `just <recipe>` → that recipe becomes REQUIRED (MISSING is a problem).
        claude_dir = root / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "recipes.json").write_text(json.dumps({
            "targets": {
                "test": {"command": "just test", "implemented": True},
                # `server` (justfile `run`) implemented + routed → run required.
                "server": {"command": "just run port=${port}", "implemented": True},
            }
        }), encoding="utf-8")
        jf.write_text(
            "build:\n    cargo build\n\nrun:\n    cargo run\n\ndeploy:\n    ./deploy.sh\n",
            encoding="utf-8",
        )
        by_name = {c.name: c for c in audit_justfile(root)}
        check(by_name["justfile:test"].status == "missing" and by_name["justfile:test"].problem,
              f"recipes-wired: test should be required-missing, got {by_name['justfile:test'].status}")
        check(by_name["justfile:run"].status == "ok",
              f"recipes-wired: run (server-routed) should be ok, got {by_name['justfile:run'].status}")

        # Case 6: same recipes.json but justfile now defines 'test' → OK.
        jf.write_text(
            "build:\n    cargo build\n\nrun:\n    cargo run\n\n"
            "test:\n    cargo test\n\ndeploy:\n    ./deploy.sh\n",
            encoding="utf-8",
        )
        by_name = {c.name: c for c in audit_justfile(root)}
        check(by_name["justfile:test"].status == "ok" and not by_name["justfile:test"].problem,
              f"recipes-wired: test present should be ok, got {by_name['justfile:test'].status}")

    # --- _justfile_recipe_required helper.
    check(_justfile_recipe_required("build", None) is True,
          "core build required even without recipes.json")
    check(_justfile_recipe_required("package", None) is False,
          "package advisory without recipes.json")
    check(_justfile_recipe_required("package",
          {"package": {"command": "just package v=${version}", "implemented": True}}) is True,
          "package required when implemented + just-routed")
    check(_justfile_recipe_required("package",
          {"package": {"command": "./scripts/pkg.sh", "implemented": True}}) is False,
          "package advisory when implemented via a non-just command")
    check(_justfile_recipe_required("run",
          {"server": {"command": "just run", "implemented": True}}) is True,
          "run required when server target routed to `just run`")
    # framework/lib stack: core target explicitly declared absent → advisory.
    check(_justfile_recipe_required("build",
          {"build": {"command": None, "implemented": False}}) is False,
          "build advisory when recipes.json declares it absent (command:null)")
    check(_justfile_recipe_required("deploy",
          {"deploy": {"command": None, "implemented": False}}) is False,
          "deploy advisory when recipes.json declares it absent (command:null)")

    # --- justfile signature-level conformance (#433): _parse_justfile_recipe_
    # signature, _diff_justfile_signature, _deploy_prod_default, and their
    # wiring into audit_justfile/build_report's strict-vs-warn severity.
    check(_parse_justfile_recipe_signature("build", 'build env="dev":')
          == [("env", "dev", False)], "parse: simple env=default param")
    check(_parse_justfile_recipe_signature("deploy", 'deploy env dry_run="false":')
          == [("env", None, False), ("dry_run", "false", False)],
          "parse: positional + defaulted param")
    check(_parse_justfile_recipe_signature("release", "release *ARGS:")
          == [("ARGS", None, True)], "parse: variadic param")
    check(_parse_justfile_recipe_signature("lint", "lint:") == [],
          "parse: no params")
    check(_parse_justfile_recipe_signature("test", 'test filter="" watch="": db-up')
          == [("filter", "", False), ("watch", "", False)],
          "parse: ignores trailing dependency after the top-level colon")

    check(_diff_justfile_signature("build", [("env", "dev", False)]) == [],
          "diff: conformant build signature has no mismatches")
    check(_diff_justfile_signature("build", []) != [],
          "diff: build with zero params is a mismatch (missing parameter)")
    check(_diff_justfile_signature(
          "build", [("env", "dev", False), ("verbose", "false", False)]) != [],
          "diff: build with an extra param is a mismatch")
    check(_diff_justfile_signature(
          "deploy", [("environment", None, False), ("dry_run", "false", False)]) != [],
          "diff: deploy with a renamed positional param is mis-signed")
    check(_diff_justfile_signature(
          "deploy", [("env", "dev", False), ("dry_run", "false", False)]) != [],
          "diff: deploy's env must stay required — even a 'dev' default is a mismatch")
    check(_diff_justfile_signature("sync-deps", [("mode", "wrong", False)]) == [],
          "diff: sync-deps/vendor-check are not signature-checked (not in the 14-verb set)")

    # v8 (#360): unit-test parses/diffs exactly like test (same signature shape).
    check(_parse_justfile_recipe_signature("unit-test", 'unit-test filter="" watch="":')
          == [("filter", "", False), ("watch", "", False)],
          "parse: unit-test signature matches test's shape")
    check(_diff_justfile_signature(
          "unit-test", [("filter", "", False), ("watch", "", False)]) == [],
          "diff: conformant unit-test signature has no mismatches")
    check("unit-test" in JUSTFILE_FULL_VOCABULARY,
          "unit-test should be part of the full justfile vocabulary (#360)")
    check("unit-test" not in JUSTFILE_CORE_RECIPES,
          "unit-test is advisory, not core-required (back-compat, #360)")

    # v9 (#362): gui-test parses/diffs with its own single-param shape
    # (baseline, default "main") — deliberately not a re-use of smoke's
    # "port" shape, since a GUI project's baseline selector is not a network
    # port.
    check(_parse_justfile_recipe_signature("gui-test", 'gui-test baseline="main":')
          == [("baseline", "main", False)],
          "parse: gui-test signature carries its own baseline param")
    check(_diff_justfile_signature("gui-test", [("baseline", "main", False)]) == [],
          "diff: conformant gui-test signature has no mismatches")
    check("gui-test" in JUSTFILE_FULL_VOCABULARY,
          "gui-test should be part of the full justfile vocabulary (#362)")
    check("gui-test" not in JUSTFILE_CORE_RECIPES,
          "gui-test is advisory, not core-required (back-compat, #362)")

    check(_deploy_prod_default([("env", "prod", False), ("dry_run", "false", False)]) == "prod",
          "deploy-prod-default: env defaulting to 'prod' detected")
    check(_deploy_prod_default([("env", "Production", False), ("dry_run", "false", False)])
          == "Production", "deploy-prod-default: case-insensitive match")
    check(_deploy_prod_default([("env", "staging", False), ("dry_run", "false", False)]) is None,
          "deploy-prod-default: non-production default is not flagged")
    check(_deploy_prod_default([("env", None, False), ("dry_run", "false", False)]) is None,
          "deploy-prod-default: required/positional env (correct shape) is not flagged")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        jf = root / "justfile"

        # Fixture A (conformant): all 12 signature-checked verbs plus
        # sync-deps/vendor-check, every one matching the standard signature
        # exactly -> zero signature/prod-default findings.
        jf.write_text(
            'build env="dev":\n    echo build\n\n'
            'run env="dev" port="3000":\n    echo run\n\n'
            'stop port="":\n    echo stop\n\n'
            'test filter="" watch="":\n    echo test\n\n'
            'seed fixture="" env="dev":\n    echo seed\n\n'
            'migrate env="dev":\n    echo migrate\n\n'
            'lint:\n    echo lint\n\n'
            'clean:\n    echo clean\n\n'
            'package version="" target="":\n    echo package\n\n'
            'deploy env dry_run="false":\n    echo deploy\n\n'
            'smoke port="3000":\n    echo smoke\n\n'
            'release *ARGS:\n    echo release\n\n'
            'sync-deps mode="":\n    echo sync\n\n'
            'vendor-check full="":\n    echo vc\n',
            encoding="utf-8")
        results = audit_justfile(root)
        sig_findings = [c for c in results
                         if c.name.endswith(":signature") or c.name.endswith(":prod-default")]
        check(not sig_findings,
              f"conformant fixture: expected zero signature findings, got {sig_findings}")

        # Fixture B (missing-verb): drop 'seed' entirely. The base MISSING
        # classification already covers this; confirm the signature check
        # does NOT fire for an absent recipe (no spurious extra Check).
        jf.write_text(
            jf.read_text(encoding="utf-8").replace(
                'seed fixture="" env="dev":\n    echo seed\n\n', ''),
            encoding="utf-8")
        results = audit_justfile(root)
        by_name = {c.name: c for c in results}
        check(by_name["justfile:seed"].status == STATUS_ADVISORY_MISSING,
              "missing-verb fixture: seed should be advisory-missing")
        check("justfile:seed:signature" not in by_name,
              "missing-verb fixture: no signature Check for an absent recipe")

        # Fixture C (mis-signed verb): 'build' present but its parameter was
        # renamed and lost its default -> sig-mismatch, WARN-tier by default,
        # escalates to a blocking 'fail' only under --strict. Also proves the
        # WARN-vs-block distinction propagates all the way to Report.healthy.
        jf.write_text(
            'build environment:\n    echo build\n\n'
            'run env="dev" port="3000":\n    echo run\n\n'
            'deploy env dry_run="false":\n    echo deploy\n',
            encoding="utf-8")
        results = audit_justfile(root, strict=False)
        by_name = {c.name: c for c in results}
        sig = by_name.get("justfile:build:signature")
        check(sig is not None and sig.status == "sig-mismatch",
              f"mis-signed fixture: build should sig-mismatch (warn-tier) by default, got {sig}")
        check(sig is not None and not sig.problem,
              "mis-signed fixture: sig-mismatch must not be a problem outside --strict")
        results_strict = audit_justfile(root, strict=True)
        by_name_strict = {c.name: c for c in results_strict}
        sig_strict = by_name_strict.get("justfile:build:signature")
        check(sig_strict is not None and sig_strict.status == "fail",
              "mis-signed fixture: --strict escalates sig-mismatch to fail")
        check(sig_strict is not None and sig_strict.problem,
              "mis-signed fixture: --strict sig-mismatch must be a problem")
        # Scope the health assertion to rep.justfile itself (not overall
        # Report.healthy — a bare temp dir also trips unrelated framework/
        # upstream findings that have nothing to do with this fixture).
        rep_c = build_report(root, check_network=False, strict=False)
        check(not any(c.problem for c in rep_c.justfile),
              "mis-signed-only fixture: no justfile Check is a problem "
              "without --strict (sig-mismatch alone is WARN-tier)")
        rep_c_strict = build_report(root, check_network=False, strict=True)
        check(any(c.problem for c in rep_c_strict.justfile),
              "mis-signed-only fixture: --strict makes the sig-mismatch a "
              "justfile problem")

        # Fixture D (THE SAFETY CASE): deploy's env defaults to prod ->
        # deploy-prod-default, block-tier ALWAYS, even without --strict.
        jf.write_text(
            'build env="dev":\n    echo build\n\n'
            'run env="dev" port="3000":\n    echo run\n\n'
            'deploy env="prod" dry_run="false":\n    echo deploy\n',
            encoding="utf-8")
        results = audit_justfile(root, strict=False)
        by_name = {c.name: c for c in results}
        dp = by_name.get("justfile:deploy:prod-default")
        check(dp is not None and dp.status == "deploy-prod-default",
              f"deploy-prod-default fixture: expected the finding, got {dp}")
        check(dp is not None and dp.problem,
              "deploy-prod-default fixture: must be a problem even without --strict")
        rep_d = build_report(root, check_network=False, strict=False)
        check(any(c.status == "deploy-prod-default" and c.problem for c in rep_d.justfile),
              "deploy-prod-default: must surface as a justfile problem even "
              "without --strict")

    # --- repair_plan justfile entries (required + advisory).
    # --- audit_architecture_rules: absent / present / opt_out / malformed.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # Case 1: absent -> warn, not a problem.
        results = audit_architecture_rules(root)
        check(len(results) == 1, f"absent: expected 1 check, got {len(results)}")
        check(results[0].status == "warn",
              f"absent: expected warn, got {results[0].status}")
        check(not results[0].problem, "absent architecture-rules.json must not be a problem")

        # Case 2: present, real ruleset -> ok.
        arch = root / ARCHITECTURE_RULES_FILE
        arch.parent.mkdir(parents=True, exist_ok=True)
        arch.write_text(json.dumps({"schema-version": 1, "layers": {}}), encoding="utf-8")
        results = audit_architecture_rules(root)
        check(results[0].status == "ok",
              f"present: expected ok, got {results[0].status}")

        # Case 3: present, explicit opt_out -> ok, reason surfaced.
        arch.write_text(json.dumps({"schema-version": 1, "opt_out": True,
                                    "opt_out-reason": "no layering yet"}), encoding="utf-8")
        results = audit_architecture_rules(root)
        check(results[0].status == "ok",
              f"opt_out: expected ok, got {results[0].status}")
        check("no layering yet" in results[0].detail,
              "opt_out reason should be surfaced in the detail")

        # Case 4: present but malformed -> warn (never crashes).
        arch.write_text("{not json", encoding="utf-8")
        results = audit_architecture_rules(root)
        check(results[0].status == "warn",
              f"malformed: expected warn, got {results[0].status}")

    # --- repair_plan: architecture-rules absence is notice-only, never blocking.
    rep_arch = Report(repo_root="/x")
    rep_arch.architecture = [Check(ARCHITECTURE_RULES_FILE, "warn", "absent")]
    plan_arch = repair_plan(rep_arch)
    check(any(ARCHITECTURE_RULES_FILE in s for s in plan_arch),
          "repair plan should mention the absent architecture-rules.json")
    check(not any(c.problem for c in rep_arch.all_checks),
          "architecture-rules warn must never be a problem status")

    # --- repair_plan justfile entries.
    rep2 = Report(repo_root="/x")
    rep2.justfile = [
        Check("justfile:build", "partial", "placeholder"),
        Check("justfile:run", "ok", ""),
        Check("justfile:deploy", "missing", "absent"),
        Check("justfile:package", STATUS_ADVISORY_MISSING, "advisory"),
    ]
    plan3 = repair_plan(rep2)
    joined3 = "\n".join(plan3)
    check("PARTIAL" in joined3 and "build" in joined3,
          "repair plan should mention PARTIAL build recipe")
    check("MISSING" in joined3 and "deploy" in joined3,
          "repair plan should mention MISSING deploy recipe")
    check("ADVISORY" in joined3 and "package" in joined3,
          "repair plan should mention ADVISORY package recipe")

    # release-readiness (v3.90): WARN without an implemented release target,
    # OK with one, and the WARN never counts as a health problem.
    with tempfile.TemporaryDirectory() as td:
        rr_root = Path(td)
        (rr_root / ".claude").mkdir()
        rr = audit_release_readiness(rr_root)
        check(len(rr) == 1 and rr[0].status == "warn",
              "release-readiness: missing recipes.json should WARN")
        check(not rr[0].problem,
              "release-readiness WARN must not be a PROBLEM status")
        (rr_root / ".claude" / "recipes.json").write_text(json.dumps({
            "targets": {"release": {"command": "just release",
                                    "implemented": False}}}), encoding="utf-8")
        rr = audit_release_readiness(rr_root)
        check(rr[0].status == "warn",
              "release-readiness: unimplemented release stub should WARN")
        (rr_root / ".claude" / "recipes.json").write_text(json.dumps({
            "targets": {"release": {"command": "just release",
                                    "implemented": True}}}), encoding="utf-8")
        rr = audit_release_readiness(rr_root)
        check(rr[0].status == "ok",
              "release-readiness: implemented release target should be OK")

    # environments-adoption (v3.94 #439): WARN for a web app with no
    # environments block; OK for a non-web-app repo, and OK once the block is
    # declared. WARN never counts as a health problem.
    with tempfile.TemporaryDirectory() as td:
        ea_root = Path(td)
        (ea_root / CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)

        def _write_ea_config(cfg: dict) -> None:
            (ea_root / CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")

        # Not a web app at all: OK, no warning.
        _write_ea_config({"web-app": {"value": "no"}})
        ea = audit_environments_adoption(ea_root)
        check(len(ea) == 1 and ea[0].status == "ok",
              "environments-adoption: non-web-app should be OK")

        # No web-app block declared at all: also OK (same as "no").
        _write_ea_config({})
        ea = audit_environments_adoption(ea_root)
        check(ea[0].status == "ok",
              "environments-adoption: absent web-app block should be OK")

        # web-app.value=yes, no environments block: WARN, not a problem.
        _write_ea_config({"web-app": {"value": "yes"}})
        ea = audit_environments_adoption(ea_root)
        check(len(ea) == 1 and ea[0].status == "warn",
              "environments-adoption: web app with no environments block should WARN")
        check(not ea[0].problem,
              "environments-adoption WARN must not be a PROBLEM status")
        check("config-loader-design.md" in ea[0].detail and "config_loader.rs" in ea[0].detail,
              "environments-adoption WARN should point at the loader design doc + module")

        # web-app.value=yes, environments block declared: OK.
        _write_ea_config({"web-app": {"value": "yes"},
                          "environments": {"dev": {"channel": "stable"}}})
        ea = audit_environments_adoption(ea_root)
        check(ea[0].status == "ok",
              "environments-adoption: web app with an environments block should be OK")

        # web-app.value=yes, environments block present but empty: still WARN
        # (an empty block is not a real declaration).
        _write_ea_config({"web-app": {"value": "yes"}, "environments": {}})
        ea = audit_environments_adoption(ea_root)
        check(ea[0].status == "warn",
              "environments-adoption: empty environments block should still WARN")

    # --- fixtures-convention adoption (#438): web-app.value gating +
    #     fixtures/ presence/well-formedness + seed-recipe implementation.
    def _fx_write_config(root: Path, cfg: dict) -> None:
        (root / CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)
        (root / CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        fx_root = Path(td)

        # Case 1: web-app.value is NOT 'yes' -> silent ok, convention doesn't apply.
        _fx_write_config(fx_root, {"web-app": {"value": "no"}})
        results = audit_fixtures_convention(fx_root)
        check(len(results) == 1 and results[0].status == "ok",
              f"fixtures: non-web-app should be ok, got {results}")

        # Case 2: web-app.value: yes, no fixtures/, no seed recipe -> WARN.
        _fx_write_config(fx_root, {"web-app": {"value": "yes"}})
        results = audit_fixtures_convention(fx_root)
        check(len(results) == 1 and results[0].status == "warn",
              f"fixtures: web-app with neither adoption path should WARN, got {results}")
        check(not results[0].problem,
              "fixtures: WARN must not be a PROBLEM status (advisory, like release-readiness)")

        # Case 3: a conformant fixtures/ directory -> ok.
        core = fx_root / FIXTURES_DIR / "core"
        core.mkdir(parents=True)
        (core / FIXTURE_MANIFEST_NAME).write_text(json.dumps({
            "family": "sql", "strategy": "truncate-and-load",
            "apply": "sqlite3 db < {file}", "files": ["001.sql"],
        }), encoding="utf-8")
        (core / "001.sql").write_text("SELECT 1;\n", encoding="utf-8")
        results = audit_fixtures_convention(fx_root)
        check(results[0].status == "ok",
              f"fixtures: conformant fixtures/ should be ok, got {results}")

        # Case 4: fixtures/ present but malformed (bad family) -> WARN, never fail.
        (core / FIXTURE_MANIFEST_NAME).write_text(json.dumps({
            "family": "xml", "strategy": "upsert", "apply": "echo {file}",
        }), encoding="utf-8")
        results = audit_fixtures_convention(fx_root)
        check(results[0].status == "warn" and "family" in results[0].detail,
              f"fixtures: malformed manifest should WARN naming the field, got {results}")
        check(not results[0].problem,
              "fixtures: malformed-manifest WARN must not be a PROBLEM status")

        # Case 5: no fixtures/ dir, but justfile has a real (non-placeholder)
        # `seed` recipe -> ok (the other adoption path).
        shutil.rmtree(fx_root / FIXTURES_DIR)
        (fx_root / "justfile").write_text(
            "seed fixture=\"\" env=\"dev\":\n"
            "    python3 .claude/skills/grm-build-recipe/recipe.py seed "
            "--fixture {{fixture}} --env {{env}}\n",
            encoding="utf-8")
        results = audit_fixtures_convention(fx_root)
        check(results[0].status == "ok" and "seed" in results[0].detail,
              f"fixtures: implemented seed recipe alone should be ok, got {results}")

        # Case 6: justfile `seed` still a grimoire:placeholder stub -> back to WARN.
        (fx_root / "justfile").write_text(
            "seed fixture=\"\" env=\"dev\":\n"
            "    # grimoire:placeholder\n"
            "    @echo \"TODO\"\n",
            encoding="utf-8")
        results = audit_fixtures_convention(fx_root)
        check(results[0].status == "warn",
              f"fixtures: placeholder seed recipe should still WARN, got {results}")

    # --- repair_plan: fixtures WARN surfaces as an optional repair step.
    rep_fx = Report(repo_root="/x")
    rep_fx.fixtures = [Check("fixtures-convention", "warn", "not adopted")]
    plan_fx = repair_plan(rep_fx)
    check(any("fixtures/ convention not adopted" in s for s in plan_fx),
          "repair plan should mention the unadopted fixtures/ convention")
    check(not any(c.problem for c in rep_fx.all_checks),
          "fixtures-convention warn must never be a problem status")

    # --- changelog-surface conformance (#437): wraps changelog_conformance.py's
    #     own --self-test as the ground truth, then exercises audit_changelog_
    #     surface()'s Check-status mapping over the same throwaway-scaffold
    #     shapes (dial on + no build-info -> WARN; both present -> OK).
    cc_self_test_rc = changelog_conformance.run_self_test()
    check(cc_self_test_rc == 0,
          "changelog_conformance.py --self-test must pass (wrapped skill)")

    with tempfile.TemporaryDirectory() as td:
        cl_root = Path(td)

        # Case 1: not a web app -> ok, no claims.
        _fx_write_config(cl_root, {"web-app": {"value": "no"}})
        results = audit_changelog_surface(cl_root)
        check(len(results) == 1 and results[0].status == "ok",
              f"changelog-surface: non-web-app should be ok, got {results}")

        # Case 2: web app, dial absent (default off) -> ok.
        _fx_write_config(cl_root, {"web-app": {"value": "yes"}})
        results = audit_changelog_surface(cl_root)
        check(results[0].status == "ok",
              f"changelog-surface: dial absent (default off) should be ok, got {results}")

        # Case 3 (THE ACCEPTANCE CASE): web app, dial ON, no
        # grimoire-build-info.json anywhere -> WARN, never a problem status.
        _fx_write_config(cl_root, {"web-app": {"value": "yes"},
                                    "changelog": {"user-facing": {"value": "on"}}})
        results = audit_changelog_surface(cl_root)
        check(len(results) == 1 and results[0].status == "warn",
              f"changelog-surface: dial on + no build-info should WARN, got {results}")
        check(not results[0].problem,
              "changelog-surface WARN must not be a PROBLEM status")
        check("grimoire-build-info.json" in results[0].detail,
              "changelog-surface WARN should name the missing build-info snapshot")

        # Case 4 (THE ACCEPTANCE CASE): web app, dial ON, build-info snapshot
        # present under dist/ -> ok.
        dist_stage = cl_root / "dist" / "demo-v1.0.0-x86_64-linux"
        dist_stage.mkdir(parents=True)
        (dist_stage / "grimoire-build-info.json").write_text(json.dumps({
            "framework-version": "v3.94", "grimoire-config": {},
            "build-timestamp": "2026-07-13T00:00:00Z", "source-ref": "deadbeef",
        }), encoding="utf-8")
        results = audit_changelog_surface(cl_root)
        check(results[0].status == "ok",
              f"changelog-surface: dial on + build-info present should be ok, got {results}")

    # --- repair_plan: changelog-surface WARN surfaces as an optional repair step.
    rep_cl = Report(repo_root="/x")
    rep_cl.changelog_surface = [Check("changelog-surface", "warn", "flagged")]
    plan_cl = repair_plan(rep_cl)
    check(any("grimoire-build-info.json" in s for s in plan_cl),
          "repair plan should mention the missing build-info snapshot")
    check(not any(c.problem for c in rep_cl.all_checks),
          "changelog-surface warn must never be a problem status")

    # --- component-registry freshness (#458): audit_component_registry.
    with tempfile.TemporaryDirectory() as td:
        cr_root = Path(td)

        # Case 1 (THE ACCEPTANCE CASE): no components/lib scan path and no
        # existing registry file -> ok, "no registry expected" (this repo's
        # own case — confirmed out of scope for self-population,
        # release-planning-v3.97.md §4). Must degrade gracefully, never a
        # false failure for every non-component project in the fleet.
        results = audit_component_registry(cr_root)
        check(len(results) == 1 and results[0].status == "ok",
              f"component-registry: no scan path + no file should be ok, got {results}")
        check("no registry expected" in results[0].detail,
              f"component-registry: no-scan-path ok should say 'no registry "
              f"expected', got {results[0].detail}")

        # Case 2 (THE ACCEPTANCE CASE): scan path present (components/), no
        # registry file yet -> WARN (absent).
        comp_dir = cr_root / "components" / "widget"
        comp_dir.mkdir(parents=True)
        (comp_dir / "component.json").write_text(json.dumps({
            "id": "widget", "version": "v1.0.0", "summary": "A widget.",
            "profiles": ["lib"], "provides": ["telemetry"],
            "stability": "stable", "source": "components/widget/",
        }), encoding="utf-8")
        results = audit_component_registry(cr_root)
        check(len(results) == 1 and results[0].status == "warn",
              f"component-registry: scan path present, no file should WARN, got {results}")
        check(not results[0].problem,
              "component-registry absent-file WARN must not be a PROBLEM status")
        check("absent" in results[0].detail,
              "component-registry absent-file WARN should say the registry is absent")

        # Case 3 (THE ACCEPTANCE CASE): build the registry once, then reshape
        # the source WITHOUT rebuilding -> WARN (stale vs current sources).
        component_registry.RegistryEngine(str(cr_root)).build(write=True)
        (comp_dir / "component.json").write_text(json.dumps({
            "id": "widget", "version": "v1.1.0", "summary": "A widget.",
            "profiles": ["lib"], "provides": ["telemetry"],
            "stability": "stable", "source": "components/widget/",
        }), encoding="utf-8")
        results = audit_component_registry(cr_root)
        check(len(results) == 1 and results[0].status == "warn",
              f"component-registry: stale registry should WARN, got {results}")
        check(not results[0].problem,
              "component-registry stale WARN must not be a PROBLEM status")
        check("stale" in results[0].detail,
              "component-registry stale WARN should say 'stale'")
        check("changed=1" in results[0].detail,
              f"component-registry stale WARN should report changed=1, got {results[0].detail}")

        # Case 4 (THE ACCEPTANCE CASE): rebuild (as the closeout step would)
        # -> fresh -> ok, uncataloged count still reported for visibility.
        component_registry.RegistryEngine(str(cr_root)).build(write=True)
        results = audit_component_registry(cr_root)
        check(len(results) == 1 and results[0].status == "ok",
              f"component-registry: fresh registry should be ok, got {results}")
        check("uncataloged=0" in results[0].detail,
              f"component-registry ok should report uncataloged count, got {results[0].detail}")

    # --- repair_plan: component-registry WARN surfaces as an optional repair step.
    rep_cr = Report(repo_root="/x")
    rep_cr.component_registry = [Check("component-registry", "warn", "stale")]
    plan_cr = repair_plan(rep_cr)
    check(any("component_registry.py" in s for s in plan_cr),
          "repair plan should mention component_registry.py build")
    check(not any(c.problem for c in rep_cr.all_checks),
          "component-registry warn must never be a problem status")

    # --- catalog-conformance audit (#434): family-undetermined degrades
    # gracefully; a declared web-app.value dial dispatches the real plan();
    # --strict escalates a real finding, exempt/degraded never escalate.
    with tempfile.TemporaryDirectory() as td:
        cc_root = Path(td)
        # No web-app.value dial at all -> family undetermined -> ok, skipped.
        results = audit_catalog_conformance(cc_root, strict=False)
        check(len(results) == 1 and results[0].status == "ok",
              f"catalog-conformance: no family signal should be a single ok "
              f"check, got {results}")
        check(not any(c.problem for c in results),
              "catalog-conformance: family-undetermined must never be a "
              "problem status")

        # Declare web-app.value -> family resolves to "web" -> the real
        # plan() dispatches across all 10 entries. An empty src/ dir (present
        # but with no /admin-console reference) so admin-console's static
        # scan resolves "not-found" (a real WARN input) rather than
        # "not-applicable" (no src/templates tree at all -> never warns).
        cfg_path = cc_root / CONFIG_FILE
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"web-app": {"value": "yes"}}),
                             encoding="utf-8")
        (cc_root / "src").mkdir(exist_ok=True)
        results_web = audit_catalog_conformance(cc_root, strict=False)
        check(len(results_web) == 10,
              f"catalog-conformance: web family should report all 10 entries, "
              f"got {len(results_web)}")
        by_name = {c.name.split(":", 1)[-1]: c for c in results_web}
        check(by_name["adopt-meta-updater"].status == "ok",
              "catalog-conformance: exempt entries report ok (never a "
              "problem), got " + by_name["adopt-meta-updater"].status)
        check(by_name["adopt-fleet-contract"].status == "ok",
              "catalog-conformance: exempt entries report ok, got " +
              by_name["adopt-fleet-contract"].status)
        check(by_name["admin-console"].status == "catalog-conformance",
              "catalog-conformance: an empty src/ (no /admin-console route "
              "ref) should be the WARN-tier status by default, got " +
              by_name["admin-console"].status)
        check(not any(c.problem for c in results_web),
              "catalog-conformance: WARN-tier findings must not be a "
              "problem status outside --strict "
              f"(got: {[(c.name, c.status) for c in results_web if c.problem]})")

        # --strict: a real finding (admin-console has no static route ref on
        # this empty fixture) escalates to a blocking fail; exempt entries
        # still never escalate.
        results_strict = audit_catalog_conformance(cc_root, strict=True)
        by_name_strict = {c.name.split(":", 1)[-1]: c for c in results_strict}
        check(by_name_strict["admin-console"].status == "fail",
              "catalog-conformance: --strict should escalate a real "
              "admin-console finding to fail, got " +
              by_name_strict["admin-console"].status)
        check(by_name_strict["adopt-meta-updater"].status == "ok",
              "catalog-conformance: --strict must NEVER escalate an exempt "
              "(blocked-on-upstream) entry, got " +
              by_name_strict["adopt-meta-updater"].status)
        check(by_name_strict["adopt-fleet-contract"].status == "ok",
              "catalog-conformance: --strict must NEVER escalate an exempt "
              "entry, got " + by_name_strict["adopt-fleet-contract"].status)

    # --- repair_plan: catalog-conformance findings surface as a repair step.
    rep_cc = Report(repo_root="/x")
    rep_cc.catalog_conformance = [
        Check("catalog-conformance:admin-console", "catalog-conformance",
              "no static /admin-console reference found")]
    plan_cc = repair_plan(rep_cc)
    check(any("catalog_conformance.py" in s for s in plan_cc),
          "repair plan should mention catalog_conformance.py")
    check(not any(c.problem for c in rep_cc.all_checks),
          "catalog-conformance status (non-strict) must never be a problem "
          "status")

    # --- hook capability contracts (issue #441): _hook_contract parsing.
    with tempfile.TemporaryDirectory() as td:
        hc_root = Path(td)
        hooks_dir = hc_root / HOOKS_DIR
        hooks_dir.mkdir(parents=True, exist_ok=True)

        stamped = hooks_dir / "stamped.sh"
        stamped.write_text(
            "#!/usr/bin/env python3\n"
            "# HOOK_CONTRACT: v1 capabilities=[cap-a,cap-b]\n"
            '"""Doc."""\n', encoding="utf-8")
        version, caps = _hook_contract(stamped)
        check(version == "v1", f"_hook_contract version: got {version}")
        check(caps == frozenset({"cap-a", "cap-b"}),
              f"_hook_contract capabilities: got {caps}")

        unstamped = hooks_dir / "unstamped.sh"
        unstamped.write_text("#!/usr/bin/env python3\n\"\"\"Doc.\"\"\"\n",
                             encoding="utf-8")
        version, caps = _hook_contract(unstamped)
        check(version is None and caps == frozenset(),
              "_hook_contract on a header-less file should be (None, frozenset())")

        version, caps = _hook_contract(hooks_dir / "absent.sh")
        check(version is None and caps == frozenset(),
              "_hook_contract on an absent file should be (None, frozenset())")

    # --- audit_hook_contracts: the three outcomes (issue #441).
    def _write_config(root: Path, cfg: dict) -> None:
        (root / CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)
        (root / CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")

    def _write_hook(root: Path, name: str, capabilities: list[str]) -> None:
        hooks_dir = root / HOOKS_DIR
        hooks_dir.mkdir(parents=True, exist_ok=True)
        caps = ",".join(capabilities)
        (hooks_dir / name).write_text(
            "#!/usr/bin/env python3\n"
            f"# HOOK_CONTRACT: v1 capabilities=[{caps}]\n"
            '"""Doc."""\n', encoding="utf-8")

    # 1. no-claims — autonomous-push.enabled is absent/false: the claim's
    #    predicate is false, so nothing is checked at all (not even `ok`),
    #    regardless of what the hooks declare (or fail to declare).
    with tempfile.TemporaryDirectory() as td:
        nc_root = Path(td)
        _write_config(nc_root, {"autonomous-push": {"enabled": False}})
        # Deliberately do NOT write push-guard.sh / autonomy-allow.sh at all.
        checks = audit_hook_contracts(nc_root)
        push_claim = [c for c in checks
                     if c.name == "hook-contract:autonomous-push.enabled"]
        check(push_claim == [],
              "no-claims: autonomous-push.enabled=false must emit nothing")

    # 2. claim-satisfied — config claims autonomous-push, and BOTH
    #    implementing hooks declare the capability: silent `ok`, never a
    #    problem.
    with tempfile.TemporaryDirectory() as td:
        sat_root = Path(td)
        _write_config(sat_root, {"autonomous-push": {"enabled": True}})
        _write_hook(sat_root, "push-guard.sh", ["push-block-default", "autonomous-push"])
        _write_hook(sat_root, "autonomy-allow.sh", ["autonomy-allow-noir", "autonomous-push"])
        checks = audit_hook_contracts(sat_root)
        push_claim = [c for c in checks
                     if c.name == "hook-contract:autonomous-push.enabled"]
        check(len(push_claim) == 1 and push_claim[0].status == "ok",
              f"claim-satisfied: expected one ok Check, got {push_claim}")
        check(not push_claim[0].problem,
              "claim-satisfied Check must not be a problem")

    # 3. claim-unmet -> FAIL — reproduces the warden incident shape: config
    #    claims autonomous-push.enabled=true, but push-guard.sh's installed
    #    stamp predates the feature (no `autonomous-push` capability) — the
    #    exact gap that went unnoticed for weeks (issue #441 motivation).
    with tempfile.TemporaryDirectory() as td:
        unmet_root = Path(td)
        _write_config(unmet_root, {"autonomous-push": {"enabled": True}})
        _write_hook(unmet_root, "push-guard.sh", ["push-block-default"])  # stale — no autonomous-push
        _write_hook(unmet_root, "autonomy-allow.sh", ["autonomy-allow-noir", "autonomous-push"])
        checks = audit_hook_contracts(unmet_root)
        push_claim = [c for c in checks
                     if c.name == "hook-contract:autonomous-push.enabled"]
        check(len(push_claim) == 1 and push_claim[0].status == "fail",
              f"claim-unmet (warden shape): expected one fail Check, got {push_claim}")
        check(push_claim[0].problem,
              "claim-unmet Check must be a problem (drives exit 1)")
        check("push-guard.sh" in push_claim[0].detail,
              "claim-unmet detail should name the stale hook")

    # 4. claim-unmet when the hook file is entirely absent (never synced) —
    #    same FAIL outcome, since an absent hook declares zero capabilities.
    with tempfile.TemporaryDirectory() as td:
        absent_root = Path(td)
        _write_config(absent_root, {"autonomous-push": {"enabled": True}})
        # No hooks written at all.
        checks = audit_hook_contracts(absent_root)
        push_claim = [c for c in checks
                     if c.name == "hook-contract:autonomous-push.enabled"]
        check(len(push_claim) == 1 and push_claim[0].status == "fail",
              "claim-unmet: an entirely absent hook must also FAIL the claim")

    # 5. no-claims for the other two registry entries (stealth-mode off,
    #    paradigm Supervised) confirms the registry mechanism generalizes
    #    beyond the autonomous-push case, and claim-satisfied for paradigm.
    with tempfile.TemporaryDirectory() as td:
        gen_root = Path(td)
        _write_config(gen_root, {
            "stealth-mode": {"value": "off"},
            "work-paradigm": {"value": "Supervised"},
        })
        checks = audit_hook_contracts(gen_root)
        check(checks == [],
              "no-claims: stealth off + Supervised paradigm should emit nothing")
        _write_config(gen_root, {"work-paradigm": {"value": "Noir"}})
        _write_hook(gen_root, "autonomy-allow.sh", ["autonomy-allow-noir"])
        checks = audit_hook_contracts(gen_root)
        noir_claim = [c for c in checks
                     if c.name == "hook-contract:work-paradigm.value=Noir"]
        check(len(noir_claim) == 1 and noir_claim[0].status == "ok",
              f"claim-satisfied (paradigm): expected ok, got {noir_claim}")

    # --- repair_plan: a hook-contract FAIL surfaces as a repair step.
    rep_hc = Report(repo_root="/x")
    rep_hc.hook_contracts = [
        Check("hook-contract:autonomous-push.enabled", "fail", "stale hook"),
    ]
    plan_hc = repair_plan(rep_hc)
    check(any("Hook contract UNMET" in s for s in plan_hc),
          "repair plan should mention the unmet hook-contract claim")

    # --- post-commit-test-gate activation (#361): requires_hooks_path -----
    # A byte-perfect, correctly-stamped REAL git hook is inert unless
    # `git config core.hooksPath` actually points at HOOKS_DIR — this is the
    # one class of claim the byte-content/stamp check above cannot see.
    def _git_repo(td: Path) -> Path:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(td),
                       capture_output=True, text=True)
        return td

    # 6. no-claims — post-commit-test-gate absent/disabled: nothing emitted.
    with tempfile.TemporaryDirectory() as td:
        root = _git_repo(Path(td))
        _write_config(root, {"code-quality": {"post-commit-test-gate": {"enabled": False}}})
        checks = audit_hook_contracts(root)
        check(checks == [], "no-claims: post-commit-test-gate disabled must emit nothing")

    # 7. claim-satisfied + hooksPath active — both hook-contract AND
    #    hooks-path checks are `ok`.
    with tempfile.TemporaryDirectory() as td:
        root = _git_repo(Path(td))
        _write_config(root, {"code-quality": {"post-commit-test-gate":
                     {"enabled": True, "mode": "force-correct"}}})
        _write_hook(root, "post-commit", ["post-commit-gate"])
        subprocess.run(["git", "config", "core.hooksPath", HOOKS_DIR], cwd=str(root),
                       capture_output=True, text=True)
        checks = audit_hook_contracts(root)
        hc = [c for c in checks
             if c.name == "hook-contract:code-quality.post-commit-test-gate.enabled=true"]
        hp = [c for c in checks
             if c.name == "hooks-path:code-quality.post-commit-test-gate.enabled=true"]
        check(len(hc) == 1 and hc[0].status == "ok",
              f"post-commit-gate claim-satisfied: expected ok, got {hc}")
        check(len(hp) == 1 and hp[0].status == "ok",
              f"post-commit-gate hooks-path active: expected ok, got {hp}")

    # 8. claim-satisfied stamp, but core.hooksPath NEVER configured — the
    #    hook-contract check still passes (byte content is correct), but the
    #    hooks-path check FAILS: the hook is present and correctly stamped,
    #    yet git will never actually invoke it.
    with tempfile.TemporaryDirectory() as td:
        root = _git_repo(Path(td))
        _write_config(root, {"code-quality": {"post-commit-test-gate":
                     {"enabled": True, "mode": "force-correct"}}})
        _write_hook(root, "post-commit", ["post-commit-gate"])
        checks = audit_hook_contracts(root)
        hp = [c for c in checks
             if c.name == "hooks-path:code-quality.post-commit-test-gate.enabled=true"]
        check(len(hp) == 1 and hp[0].status == "fail",
              f"post-commit-gate hooks-path unset: expected fail, got {hp}")
        check(hp[0].problem, "an inert real git hook must be a problem (drives exit 1)")

    # 9. hooksPath configured to the WRONG directory — also FAILS.
    with tempfile.TemporaryDirectory() as td:
        root = _git_repo(Path(td))
        _write_config(root, {"code-quality": {"post-commit-test-gate":
                     {"enabled": True, "mode": "force-correct"}}})
        _write_hook(root, "post-commit", ["post-commit-gate"])
        subprocess.run(["git", "config", "core.hooksPath", "some/other/dir"],
                       cwd=str(root), capture_output=True, text=True)
        checks = audit_hook_contracts(root)
        hp = [c for c in checks
             if c.name == "hooks-path:code-quality.post-commit-test-gate.enabled=true"]
        check(len(hp) == 1 and hp[0].status == "fail",
              f"post-commit-gate hooks-path wrong dir: expected fail, got {hp}")

    # 10. the OPTIONAL pre-commit block variant is its own separate claim,
    #     keyed on mode=block specifically — force-correct/advisory modes
    #     never claim it (pre-commit is documented opt-in only).
    with tempfile.TemporaryDirectory() as td:
        root = _git_repo(Path(td))
        _write_config(root, {"code-quality": {"post-commit-test-gate":
                     {"enabled": True, "mode": "force-correct"}}})
        checks = audit_hook_contracts(root)
        pc = [c for c in checks if "pre-commit-block-gate" in str(c.name)
             or c.name.startswith("hook-contract:code-quality.post-commit-test-gate.mode")
             or c.name.startswith("hooks-path:code-quality.post-commit-test-gate.mode")]
        check(pc == [], "mode=force-correct must not claim the pre-commit-block-gate capability")

        _write_config(root, {"code-quality": {"post-commit-test-gate":
                     {"enabled": True, "mode": "block"}}})
        _write_hook(root, "pre-commit", ["pre-commit-block-gate"])
        subprocess.run(["git", "config", "core.hooksPath", HOOKS_DIR], cwd=str(root),
                       capture_output=True, text=True)
        checks = audit_hook_contracts(root)
        hc = [c for c in checks
             if c.name == "hook-contract:code-quality.post-commit-test-gate.mode=block"]
        hp = [c for c in checks
             if c.name == "hooks-path:code-quality.post-commit-test-gate.mode=block"]
        check(len(hc) == 1 and hc[0].status == "ok",
              f"pre-commit-block-gate claim-satisfied: expected ok, got {hc}")
        check(len(hp) == 1 and hp[0].status == "ok",
              f"pre-commit-block-gate hooks-path active: expected ok, got {hp}")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("install_doctor self-test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
