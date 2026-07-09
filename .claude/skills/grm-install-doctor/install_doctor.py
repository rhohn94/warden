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

The Justfile contract check audits whether the three required Grimoire recipes
(build, run, deploy) are present and not placeholder stubs. Each recipe is
classified as OK (present, non-placeholder), PARTIAL (has # grimoire:placeholder
body), or MISSING (recipe not found or no justfile). Any MISSING or PARTIAL
causes exit 1. See docs/design/justfile-standard-design.md.

Authoritative design: docs/grimoire/design/agent-roles-design.md (install-doctor is a
skill, not a role).

CLI:  python3 install_doctor.py [audit] [--json] [--no-network]
      python3 install_doctor.py repair [--json] [--no-network]
      python3 install_doctor.py repair --freeze-baseline [--no-network]
      python3 install_doctor.py --repair         # back-compat: == repair --freeze-baseline
      python3 install_doctor.py --self-test
      python3 install_doctor.py --help

Exit codes:
  0  healthy (no MISSING, no DRIFTED, upstream OK)
  1  degraded (one or more checks reported a problem)
  2  usage / internal error
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = ".claude/grimoire-config.json"
UPSTREAM_CONF = ".scaffold-upstream.conf"
BASE_ROOT = ".scaffold-base"
# The golden baseline is a generated artifact (no longer a committed tree). The
# doctor resolves it via grm-workflow-bootstrap's generate_golden helper and reuses
# it as the canonical file set rather than maintaining its own list.
GENERATE_GOLDEN_REL = ".claude/skills/grm-workflow-bootstrap/generate_golden.py"
FLAVOR_DIR = "claude-code"

# Statuses that represent a real, actionable problem (drive the exit code and
# the "ATTENTION NEEDED" headline). Everything else — including the three
# suppression statuses below — is informational.
# "partial" covers justfile recipes that have a grimoire:placeholder body.
PROBLEM_STATUSES = frozenset({"missing", "drifted", "fail", "partial"})

# Suppression statuses — a byte mismatch against golden that is expected, so it
# is NOT a problem and is never a repair target.
STATUS_SEED_DIVERGED = "seed-diverged"
STATUS_PARADIGM = "paradigm"
STATUS_NEWER = "newer-than-golden"

# Justfile contract: the three required Grimoire recipes and the placeholder
# marker that signals a stub body. See docs/design/justfile-standard-design.md.
JUSTFILE_REQUIRED_RECIPES = ("build", "run", "deploy")
JUSTFILE_PLACEHOLDER_MARKER = "# grimoire:placeholder"


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
    notes: list[str] = field(default_factory=list)

    @property
    def all_checks(self) -> list[Check]:
        return [*self.framework, *self.upstream, *self.base, *self.justfile]

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
    """Walk up from start (or cwd) until grimoire-config.json is found."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILE).exists():
            return candidate
    # Fall back to the nearest dir that has a .claude/ — better than cwd.
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
# Audit: Justfile contract (required recipes present and non-placeholder)
# ---------------------------------------------------------------------------


def audit_justfile(root: Path) -> list[Check]:
    """Check that the three required Grimoire justfile recipes are present and real.

    Each recipe (build, run, deploy) is classified as:
      OK      — recipe line found at start-of-line AND body has no placeholder.
      PARTIAL — recipe found but body contains '# grimoire:placeholder'.
      MISSING — no recipe line found (or no justfile exists at all).

    Any MISSING or PARTIAL is a problem (drives exit 1). All OK contributes no
    failure. See docs/design/justfile-standard-design.md for the contract.
    """
    justfile_path = root / "justfile"
    if not justfile_path.exists():
        # No justfile at all — every required recipe is MISSING.
        return [
            Check(f"justfile:{recipe}", "missing",
                  f"no justfile found — recipe '{recipe}' absent. "
                  "See docs/design/justfile-standard-design.md for the contract.")
            for recipe in JUSTFILE_REQUIRED_RECIPES
        ]

    try:
        lines = justfile_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [Check("justfile", "fail", f"cannot read justfile: {exc}")]

    checks: list[Check] = []
    for recipe in JUSTFILE_REQUIRED_RECIPES:
        status, detail = _classify_justfile_recipe(recipe, lines)
        checks.append(Check(f"justfile:{recipe}", status, detail))
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


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_report(root: Path, check_network: bool) -> Report:
    rep = Report(repo_root=str(root))
    rep.framework = audit_framework(root)
    up_checks, _repo, _ref = audit_upstream(root, check_network)
    rep.upstream = up_checks
    rep.base = audit_base(root)
    rep.justfile = audit_justfile(root)
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
    # Justfile contract findings: MISSING and PARTIAL both need manual action.
    jf_missing = [c for c in rep.justfile if c.status == "missing"]
    jf_partial = [c for c in rep.justfile if c.status == "partial"]
    if jf_missing:
        recipe_names = ", ".join(c.name.split(":", 1)[-1] for c in jf_missing)
        steps.append(
            f"Justfile MISSING recipe(s): {recipe_names} — add the recipe(s) to "
            "justfile. See docs/design/justfile-standard-design.md for the contract.")
    if jf_partial:
        recipe_names = ", ".join(c.name.split(":", 1)[-1] for c in jf_partial)
        steps.append(
            f"Justfile PARTIAL recipe(s): {recipe_names} — replace the "
            "grimoire:placeholder body with a real implementation. "
            "See docs/design/justfile-standard-design.md for the contract.")
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
    section("Justfile contract (required recipes)", rep.justfile)

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
        "notes": rep.notes,
    }
    if plan is not None:
        payload["repair_plan"] = plan
    return payload


def cmd_audit(args: argparse.Namespace) -> int:
    root = find_repo_root(Path(args.root) if args.root else None)
    rep = build_report(root, check_network=not args.no_network)
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
    rep = build_report(root, check_network=not args.no_network)
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
    # Subcommand is OPTIONAL — a bare invocation runs `audit` (#152).
    sub = p.add_subparsers(dest="command")

    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of Markdown.")
        sp.add_argument("--no-network", action="store_true",
                        help="Skip the UPSTREAM_REPO reachability probe.")

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

    # --- audit_justfile: three statuses + no-justfile case.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # Case 1: no justfile → all three recipes MISSING.
        results = audit_justfile(root)
        check(len(results) == 3, f"no-justfile: expected 3 checks, got {len(results)}")
        for c in results:
            check(c.status == "missing",
                  f"no-justfile: {c.name} should be missing, got {c.status}")
            check(c.problem, f"no-justfile: {c.name} missing must be a problem")

        # Case 2: justfile with all three recipes, no placeholders → all OK.
        jf = root / "justfile"
        jf.write_text(
            "build:\n"
            "    cargo build\n"
            "\n"
            "run:\n"
            "    cargo run\n"
            "\n"
            "deploy:\n"
            "    ./deploy.sh\n",
            encoding="utf-8",
        )
        results = audit_justfile(root)
        check(len(results) == 3, f"all-ok: expected 3 checks, got {len(results)}")
        for c in results:
            check(c.status == "ok",
                  f"all-ok: {c.name} should be ok, got {c.status}")
            check(not c.problem, f"all-ok: {c.name} ok must not be a problem")

        # Case 3: justfile missing 'deploy' recipe → deploy MISSING + non-zero.
        jf.write_text(
            "build:\n"
            "    cargo build\n"
            "\n"
            "run:\n"
            "    cargo run\n",
            encoding="utf-8",
        )
        results = audit_justfile(root)
        by_name = {c.name: c for c in results}
        check(by_name["justfile:build"].status == "ok",
              f"missing-deploy: build should be ok, got {by_name['justfile:build'].status}")
        check(by_name["justfile:run"].status == "ok",
              f"missing-deploy: run should be ok, got {by_name['justfile:run'].status}")
        check(by_name["justfile:deploy"].status == "missing",
              f"missing-deploy: deploy should be missing, got {by_name['justfile:deploy'].status}")
        check(by_name["justfile:deploy"].problem,
              "missing-deploy: deploy missing must be a problem")

        # Case 4: 'build' has placeholder body → PARTIAL + non-zero.
        jf.write_text(
            "build:\n"
            "    # grimoire:placeholder\n"
            "    echo 'implement me'\n"
            "\n"
            "run:\n"
            "    cargo run\n"
            "\n"
            "deploy:\n"
            "    ./deploy.sh\n",
            encoding="utf-8",
        )
        results = audit_justfile(root)
        by_name = {c.name: c for c in results}
        check(by_name["justfile:build"].status == "partial",
              f"partial-build: build should be partial, got {by_name['justfile:build'].status}")
        check(by_name["justfile:build"].problem,
              "partial-build: build partial must be a problem")
        check(by_name["justfile:run"].status == "ok",
              f"partial-build: run should be ok, got {by_name['justfile:run'].status}")
        check(by_name["justfile:deploy"].status == "ok",
              f"partial-build: deploy should be ok, got {by_name['justfile:deploy'].status}")

    # --- repair_plan justfile entries.
    rep2 = Report(repo_root="/x")
    rep2.justfile = [
        Check("justfile:build", "partial", "placeholder"),
        Check("justfile:run", "ok", ""),
        Check("justfile:deploy", "missing", "absent"),
    ]
    plan3 = repair_plan(rep2)
    joined3 = "\n".join(plan3)
    check("PARTIAL" in joined3 and "build" in joined3,
          "repair plan should mention PARTIAL build recipe")
    check("MISSING" in joined3 and "deploy" in joined3,
          "repair plan should mention MISSING deploy recipe")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("install_doctor self-test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
