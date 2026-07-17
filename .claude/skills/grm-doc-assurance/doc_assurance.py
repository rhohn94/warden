#!/usr/bin/env python3
"""doc-assurance — deterministic checks over a Grimoire repo's own docs.

Checks: flavor-parity, design-layout, links, docs-map, release-consistency,
        mirrored-script-parity, ported-pair-presence, skill-budget,
        relative-links, hierarchy, lean-index, monolith-cap, description-cap,
        anti-patterns, portal-stale, design-index-stale,
        product-readme-present, version-claim-freshness, classifier-compat,
        check-for-checks, design-doc-purity.
Read-only except --write-map / --write-portal / --write-design-index.
Report-only unless --strict (non-zero on findings).

Usage:
  doc_assurance.py [check ...] [--strict] [--write-map] [--write-portal]
                   [--write-design-index] [--root PATH]
                   [--baseline [PATH]]
  (no checks named ⇒ run all)

baseline ratchet (#426, v3.93)
-------------------------------
`--baseline [PATH]` (default `.claude/cache/doc-findings-baseline.json`)
turns the flat "N finding(s)" report into a trend against a stored baseline:
first run with no baseline file SEEDS it from the current findings (never
fails); subsequent runs diff current findings against the baseline by
identity (`<check>: <finding text>`) — only findings NOT in the baseline are
"new" and (under `--strict`) fail the run. A finding already in the baseline
is still printed in the per-check output above, but does not newly fail
anything it wasn't already failing. When the current set is a subset of the
baseline (some findings fixed, none added), the baseline file is rewritten to
the smaller set — a monotonic ratchet: debt can only shrink. See
`docs/design/doc-assurance-design.md` §Baseline ratchet.

design-doc index generation (docs/design/README.md, docs/grimoire/design/README.md)
------------------------------------------------------------------------------------
`--write-design-index` regenerates a `<!-- design-index:begin -->` /
`<!-- design-index:end -->` marker-delimited "| Document | Area |" table in
each tier's README.md — see maintenance-automation-design.md §1. `design-
index-stale` (check 15, default warn) flags when a README's generated region
is out of date, or when a design doc is missing the house layout (title +
Motivation) needed to index it.

documentation portal (docs/documentation.html)
------------------------------------------------
`--write-portal` generates a single self-contained `docs/documentation.html`
(inline CSS + vanilla JS, no CDN) — a wiki-like nav sidebar + client-side
search over the same reachability graph `check_hierarchy` builds. Generated,
never hand-edited; see docs-portal-design.md. `portal-stale` (check 14,
default warn) flags when the committed file no longer matches what
`--write-portal` would regenerate.

design-layout check (check 2)
------------------------------
A design doc passes when it satisfies EITHER the legacy pattern set OR the
house-template section set.  A doc matching neither still fails.

  Legacy pattern set (pre-house-template docs):
    ALL of: motivation · goals · non-goal · validation|idempotency

  House-template section set (docs/design/README.md house layout):
    ALL of: motivation · scope · design|acceptance

The check is deterministic: each doc is evaluated against both sets
independently; a doc that satisfies at least one set emits no findings.
A failing doc emits a single "does not satisfy either pattern set" finding.
Unresolved open-questions markers (TODO/TBD/???) in ## Open questions are
reported separately regardless of which layout the doc uses.

relative-links check (check 7)
--------------------------------
Repo-wide: rejects absolute internal links (/ prefix or own repo URL).
Docs-scoped: detects broken anchors and bare-prose doc references.

hierarchy check (check 8)
--------------------------
Docs-scoped: reachability from docs/README.md root, breadcrumb presence on
non-root non-index non-exempt pages, per-tier index presence.
Dial (grimoire-config.json doc-hierarchy.enforcer.value): off/warn/block.
--strict overrides dial to block.

flavor-parity check (check 1)
------------------------------
Extended in WH-7 to cover root ↔ claude-code ↔ copilot three-flavor
structural parity for docs file-name sets.  Known-intentional gaps are
pre-populated in DOCS_PARITY_ALLOW so the check never floods for them.

mirrored-script-parity check
------------------------------------
Systemic sibling of flavor-parity's hand-picked must-match file list: every
*.py/*.sh script that ships in both the root tree and a flavor tree at an
equivalent path is enumerated automatically (root ↔ claude-code by identical
relative path under .claude/skills or .claude/hooks; root ↔ copilot/codex by
basename against those flavors' flat scripts/ dir) and content-compared.
A differing pair fails unless (flavor, name) is in MIRRORED_SCRIPT_ALLOW —
documented intentional deltas (copilot/codex comment rewording for the
docs/grimoire/ Bulkhead, legitimate flat-layout path adaptations, or tracked
pre-existing drift filed as a follow-up rather than fixed on sight).

lean-index check (check 9)
---------------------------
Index pages (README.md files under docs/) must be ≤ 6 KB and contain ≥ 3
markdown links. Aggregating root indexes (docs/README.md,
docs/design/README.md) are exempt from the size cap; the link-density rule
still applies to all non-exempt index pages.

monolith-cap check (check 10)
------------------------------
Leaf docs (non-README.md files under docs/) exceeding 20 KB are flagged
with a warn-only message (never a hard gate). A hardcoded exempt list covers
files that are intentionally comprehensive. This check is forward-looking:
existing over-cap files are pre-exempted; new files that exceed the cap are
flagged.

description-cap check (check 11)
--------------------------------
For every .claude/skills/*/SKILL.md, the frontmatter `description:` field is
measured in characters. A description longer than DESCRIPTION_CAP (450 chars)
is flagged so the always-loaded skill-index footprint can't silently creep
back. Warn-level (like skill-budget): it appears in the report and counts
under --strict. Findings are emitted sorted by skill.

anti-patterns check (check 12)
-------------------------------
For every SKILL.md that has a `## Anti-patterns` section (the `## §N —
Anti-patterns` heading variant is recognised too), the section's byte-size is
measured from its heading to the next level-2 heading (or EOF). A section
larger than ANTI_PATTERNS_CAP (1,500 bytes) is flagged with the hint to keep
it to ~5 bullets / move the catalogue to reference.md. Warn-level; sorted by
skill. Reference-stub bullets (`- `Anti-patterns` — see `reference.md``) are
not headings and are never measured.

product-readme-present check
------------------------------------------
Root README.md must exist AND must not be the unmodified generic scaffold
README ("Claude Code Scaffold" title + "## What's included" section — the
golden-seed fingerprint). Deterministic; fails only the --strict gate like
every other check here.

version-claim-freshness check
--------------------------------------------
Scans README.md / CHANGELOG.md / docs/changelog.md for version strings
matching the project's own vX.Y(.Z) pattern and flags any found >= 1 minor
behind the current framework-version in grimoire-config.json. Remediation:
remove the hardcoded version claim from prose and link
docs/version-history.md instead (the one place versions never rot). No
manifest version readable -> no-op. Deterministic; fails only the --strict gate.

design-doc-purity check (#358, v3.98)
--------------------------------------
Over docs/design/** and docs/grimoire/design/** (recursive, README indexes
excluded): flags a Status: line in a doc's opening ~10 lines, any checked
`- [x]` box, release-narration phrasing ("shipped in vX.Y", "Implemented
(#123", "— DONE", "Delivered", code-stripped), a work-item-map / "Phase N
closed" heading, and *-plan.md / *-candidates.md filenames. Design docs are
timeless working agreements; completion state belongs in the release-plan
§5 ledger (grm-ledger-tick). DESIGN_PURITY_ALLOW exempts a specific path
from every pattern for the rare doc that legitimately quotes one of these
as documented prose about the convention. Deterministic; blocking under
--strict like the other checks.

check-for-checks (doctrine meta-check, #440, v3.97)
-----------------------------------------------------
Release-gate meta-check for the standards doctrine in
docs/architecture-guidelines.md §Standards doctrine: every mandated standard
MUST ship with a deterministic check, a named gate, and a severity ramp.
Scans docs/coding-standards.md and the required-feature catalog
(.claude/skills/grm-required-feature-catalog/required-feature-catalog.md) for an
upper-case "MUST"-clause with no paired check reference (an `<!-- audit: -->`
hint, a `grm-<skill>` mention, a `recipe.py` reference, a "Testable
criterion" table, or the word "deterministic") anywhere in its own heading
section. WARN-tier advisory, not a hard gate — see check_for_checks()'s
docstring for the full heuristic and its documented limitations.
"""
from __future__ import annotations

import os, re, sys, json, glob, subprocess

CHECKS = [
    "flavor-parity", "design-layout", "links", "docs-map",
    "release-consistency", "tag-format", "manifest-detect-hygiene",
    "shipped-pointers", "mirrored-script-parity", "ported-pair-presence",
    "skill-budget", "relative-links", "hierarchy", "lean-index",
    "monolith-cap", "description-cap", "anti-patterns", "portal-stale",
    "design-index-stale", "product-readme-present", "version-claim-freshness",
    "orchestrate-band-present", "server-selftest-parity", "classifier-compat",
    "skill-placeholder-tokens", "check-for-checks", "design-doc-purity",
]

# Mirror of build_distributables.py EXCLUDED_PATH_PREFIXES + sync-from-upstream.sh
# is_excluded() — the "Bulkhead" framework-internal doc carve-out. A path
# under any of these prefixes is NEVER delivered to a consumer (excluded from both
# the sync walk and the distributable), so a feature-manifest DETECT predicate may
# not depend on one — it could never pass on a consumer. Keep in sync with those two.
MANIFEST_EXCLUDED_PREFIXES = (
    "docs/grimoire/design/",
    "docs/grimoire/feature-playbook-validation.md",
    "docs/grimoire/issue-tracker-cost-spike.md",
    "docs/grimoire/issue-tracker-cost-validation.md",
    "docs/grimoire/sync-flow-audit.md",
    "docs/grimoire/docs-organization-design.md",
    "docs/grimoire/maintaining-grimoire.md",
    "docs/grimoire/authoring-grimoire-docs.md",
    "docs/grimoire/integration-workflow.md",
    "docs/grimoire/qa-ledger.md",
    "docs/grimoire/execution-profile-spike-s1.md",
    "docs/grimoire/token-efficiency-",
    "docs/grimoire/release-planning-",
    # release-planning docs relocated to a dedicated tier (active at dir
    # root, archive under archived/). Old prefixes kept for backward-compat.
    "docs/release-planning/",
)

# Context-efficiency budgets (bytes).
SKILL_BUDGET = 12_000
CLAUDE_BUDGET = 10_000

# ── description-cap (check 11) + anti-patterns (check 12) constants ──────
# Skill-index footprint guards (warn-only, like skill-budget).
DESCRIPTION_CAP = 450      # chars — max SKILL.md frontmatter description length
ANTI_PATTERNS_CAP = 1_500  # bytes — max ## Anti-patterns section size

# Paths whose root vs claude-code copies are intentionally allowed to differ.
PARITY_ALLOW_DIVERGENT = {"CLAUDE.md"}  # paradigm stamp differs by flavor

# Own-repo URL prefix for absolute-internal-link detection.
OWN_REPO_URL = "https://github.com/rhohn94/grimoire-framework"

# Exemptions from breadcrumb / orphan checks (Decision 8).
_HIERARCHY_EXEMPT_GLOBS = [
    "release-planning-v*.md",
    "version-history.md",
    "qa-ledger.md",
]

# ── WH-7: Three-flavor docs parity allow-list ───────────────────────────
# Each entry is a (flavor_a, flavor_b, relative_docs_path) tuple.
# "flavor_a" has the file; "flavor_b" does not — and this is intentional.
# Flavors are: "root", "claude-code", "copilot".
#
# Rules for additions:
#   - Only add an entry after verifying the gap is *intentional* (not a
#     forgotten sync). Grep for the design doc in both flavors before adding.
#   - Never add entries for skill-set gaps (those are caught by the skill
#     presence check above, not this docs check).
# Root dogfood flattens architecture/, data-persistence/, and distribution/ to
# docs/design/{topic}-design.md; shipped flavors (claude-code/codex/copilot)
# intentionally keep the subtree form — it is the seeded template scaffold
# new/synced consumer projects receive, not root's own dogfood content.
# Computed rather than hand-enumerated: adding a fourth flavor or topic needs
# one entry here, not eight hand-written tuples.
_V380_FLATTEN_TOPICS = ("architecture", "data-persistence", "distribution")
_V380_SUBTREE_FLAVORS = ("claude-code", "copilot", "codex")
_V380_FLATTEN_ALLOW = frozenset(
    {("root", flavor, f"docs/design/{topic}-design.md")
     for topic in _V380_FLATTEN_TOPICS for flavor in _V380_SUBTREE_FLAVORS}
    | {(flavor, "root", f"docs/design/{topic}/README.md")
       for topic in _V380_FLATTEN_TOPICS for flavor in _V380_SUBTREE_FLAVORS}
    | {(flavor, "root", f"docs/design/{topic}/{topic}-design.md")
       for topic in _V380_FLATTEN_TOPICS for flavor in _V380_SUBTREE_FLAVORS}
)

DOCS_PARITY_ALLOW = frozenset({
    # ── docs/design.md charter (main design document) ──────────────────
    # Grimoire's own top-level charter currently carries framework-specific
    # content (the seven-deliverable map), so it is root-only for now. The
    # consumer-facing seed template that ships docs/design.md as a blank
    # "main design document" slot in every project is a separate piece of work.
    ("root", "claude-code", "docs/design.md"),
    ("root", "copilot",     "docs/design.md"),
    # ── docs/grimoire/ tier ────────────────────────────────────────────
    # README.md is the ONE consumer-shipped grimoire doc; everything else under
    # docs/grimoire/ is framework-internal and auto-allowed root-only via
    # _is_internal_grimoire_doc() (mirrors build_distributables.py's bulkhead).
    # So no per-file entries are needed for the internal tree — only README,
    # which must stay present across flavors.
    ("claude-code", "root",    "docs/grimoire/README.md"),
    ("claude-code", "copilot", "docs/grimoire/README.md"),

    # ── docs/design/ux/ — root has README; claude-code/copilot do not ──
    # Root carries a full ux/ README; the shipped flavors only have stubs.
    ("root", "claude-code", "docs/design/ux/README.md"),
    ("root", "copilot",     "docs/design/ux/README.md"),
    # components.md and theme.md are in claude-code but not copilot
    ("claude-code", "copilot", "docs/design/ux/components.md"),
    ("claude-code", "copilot", "docs/design/ux/theme.md"),
    # root ↔ copilot gaps (root has them via root→claude-code inheritance)
    ("root", "copilot", "docs/design/ux/components.md"),
    ("root", "copilot", "docs/design/ux/theme.md"),

    # ── docs/grimoire/design/ tier — ALL framework-internal ────────────
    # Every docs/grimoire/design/**.md (including copilot's feature-manifest.md
    # location quirk) is auto-allowed root-only via _is_internal_grimoire_doc();
    # the previous ~90 per-file entries here were redundant and have been removed.

    # ── docs/ top-level — root-only files ─────────────────────────────
    # Root is the actual project; claude-code/docs/ ships only the subset a new
    # project needs to bootstrap. (docs/grimoire/ internal files are auto-allowed
    # by _is_internal_grimoire_doc() and need no entry.)
    ("root", "claude-code", "docs/version-history.md"),
    ("root", "claude-code", "docs/changelog.md"),
    ("root", "claude-code", "docs/web-app-aura-adoption-guide.md"),
    ("root", "claude-code", "docs/web-app-deployment-protocol.md"),
    ("root", "copilot", "docs/version-history.md"),
    ("root", "copilot", "docs/changelog.md"),
    ("root", "copilot", "docs/web-app-aura-adoption-guide.md"),
    ("root", "copilot", "docs/web-app-deployment-protocol.md"),

    # ── docs/release-planning/ tier — root-only ────────────────────────
    # The plan docs themselves are auto-exempt via _RELEASE_PLAN_RE; the tier
    # index READMEs are root-only dogfood (the shipped flavors carry no plans).
    ("root", "claude-code", "docs/release-planning/README.md"),
    ("root", "claude-code", "docs/release-planning/archived/README.md"),
    ("root", "copilot",     "docs/release-planning/README.md"),
    ("root", "copilot",     "docs/release-planning/archived/README.md"),

    # ── docs/coding-standards/ — claude-code only until WH-9 root dogfood ─
    # WH-6 created this tier-index in claude-code; root + copilot follow in WH-9.
    ("claude-code", "root",    "docs/coding-standards/README.md"),
    ("claude-code", "copilot", "docs/coding-standards/README.md"),

    # ── docs/design/ux/README.md — copilot deferred to WH-9 ─────────────
    ("claude-code", "copilot", "docs/design/ux/README.md"),

    # ── docs/ top-level — claude-code ↔ copilot gaps ──────────────────
    # claude-code ships README not in copilot; root has README, copilot does not.
    ("claude-code", "copilot", "docs/README.md"),
    ("root", "copilot", "docs/README.md"),

    # ── codex flavor ────────────────────────────────────────────────────
    # codex was ported from claude-code; its docs/ tree mirrors copilot's
    # file-set exactly. These tuples encode the intentional gaps; tuple
    # direction matches _is_docs_gap_allowed(fa, fb) (fa = the flavor that
    # HAS the file).
    #
    # root-only files codex lacks (mirrors the root↔copilot gaps above):
    ("root", "codex", "docs/design.md"),
    ("root", "codex", "docs/design/ux/components.md"),
    ("root", "codex", "docs/design/ux/theme.md"),
    ("root", "codex", "docs/release-planning/README.md"),
    ("root", "codex", "docs/release-planning/archived/README.md"),
    ("root", "codex", "docs/version-history.md"),
    ("root", "codex", "docs/changelog.md"),
    ("root", "codex", "docs/web-app-aura-adoption-guide.md"),
    ("root", "codex", "docs/web-app-deployment-protocol.md"),
    # claude-code ships ux components/theme that codex (like copilot) lacks:
    ("claude-code", "codex", "docs/design/ux/components.md"),
    ("claude-code", "codex", "docs/design/ux/theme.md"),

    # ── Intentional post-fix gaps (R7/#390 — framework specs pulled out of
    # every shipped flavor's docs/design/, per the v3.39 documentation-
    # separation contract) ───────────────────────────────────────────────
    # codex-flavor-design.md, copilot-grm-namespacing-design.md, and
    # justfile-standard-design.md are this project's own flavor/build design
    # docs (root docs/design/ — "your project's own design docs", not
    # framework-internal docs/grimoire/design/). None of them ship into any
    # of the three consumer flavor trees any more; root is the sole copy.
    ("root", "claude-code", "docs/design/codex-flavor-design.md"),
    ("root", "copilot",     "docs/design/codex-flavor-design.md"),
    ("root", "codex",       "docs/design/codex-flavor-design.md"),
    ("root", "claude-code", "docs/design/copilot-grm-namespacing-design.md"),
    ("root", "copilot",     "docs/design/copilot-grm-namespacing-design.md"),
    ("root", "codex",       "docs/design/copilot-grm-namespacing-design.md"),
    ("root", "claude-code", "docs/design/justfile-standard-design.md"),
    ("root", "copilot",     "docs/design/justfile-standard-design.md"),
    ("root", "codex",       "docs/design/justfile-standard-design.md"),
}) | _V380_FLATTEN_ALLOW

# Release-planning archive pattern: root-only, auto-matched by regex.
# The "Release-planning relocation" moved all plans into a dedicated
# docs/release-planning/ tier (active at dir root, archive under archived/).
# The pattern matches the new tier AND the two pre-relocation locations
# (top-level docs/ active + docs/grimoire/ archive) for backward-compat, so a
# synced-but-not-yet-migrated consumer never flags a parity / monolith gap.
_RELEASE_PLAN_RE = re.compile(r"^docs/(release-planning/(archived/)?|grimoire/)?release-planning-v[\d.]+\.md$")

# ── lean-index (check 9) constants ─────────────────────────────────────
LEAN_INDEX_SIZE_CAP  = 6_144   # 6 KB — individual index page budget
LEAN_INDEX_MIN_LINKS = 3       # minimum markdown links in an index page
# Size-cap exempt: aggregating multi-tier index pages that must list many
# sub-docs to be useful.  Link-density rule still applies.
LEAN_INDEX_SIZE_EXEMPT = frozenset({
    "docs/README.md",           # repo-root doc map (many tiers)
    "docs/design/README.md",    # design-doc catalog (all design docs)
    # "Bulkhead": framework design-spec catalog. Root indexes the full
    # ~68-doc framework corpus here (vs ~25-29 in the shipped flavors), so its
    # index legitimately exceeds the 6 KB lean cap. Root-only exemption: the
    # shipped flavors' grimoire/design indexes stay under the cap.
    "docs/grimoire/design/README.md",  # framework design-doc catalog (root corpus)
})

# ── monolith-cap (check 10) constants ──────────────────────────────────
MONOLITH_CAP = 20_480   # 20 KB — warn-only cap for leaf docs

# Intentionally comprehensive files that are allowed to exceed the cap.
# Add new entries here rather than raising the cap threshold.
MONOLITH_CAP_EXEMPT = frozenset({
    # Always-exempt by policy
    "docs/coding-standards.md",
    "docs/version-history.md",
    "docs/grimoire/qa-ledger.md",
    "docs/grimoire/integration-workflow.md",
    # Existing large design docs (pre-WH-8 corpus; exempt so this check is
    # forward-looking rather than retroactively flagging the whole corpus).
    # "Bulkhead": these framework specs relocated docs/design/ →
    # docs/grimoire/design/ (DS-2 for claude-code; root/copilot in DS-4/DS-3).
    # Paths are the NEW location so the exemption keeps matching post-move.
    "docs/grimoire/design/agent-roles-design.md",
    "docs/grimoire/design/autonomy-scheduling-design.md",
    "docs/grimoire/design/cost-governance-design.md",
    "docs/grimoire/design/execution-profiles-design.md",
    "docs/grimoire/design/feature-aware-sync-design.md",
    "docs/grimoire/design/hard-reset-design.md",
    "docs/grimoire/design/issue-tracker-design.md",
    "docs/grimoire/design/model-effort-profiles-design.md",
    "docs/grimoire/design/noir-iterative-loop-design.md",
    "docs/grimoire/design/onboarding-design.md",
    "docs/grimoire/design/token-efficiency-design.md",
    "docs/grimoire/design/ux-design-language-design.md",
    "docs/grimoire/design/ux-enhancements-design.md",
    "docs/grimoire/design/work-paradigm-design.md",
    "docs/grimoire/design/write-capable-workflow-design.md",
    "docs/grimoire/docs-organization-design.md",
    "docs/grimoire/integration-workflow.md",
    # Comprehensive framework docs exempted (legitimate large docs;
    # add new entries here rather than raising the cap threshold).
    "docs/grimoire/design/clean-room-design.md",
    "docs/grimoire/design/dependency-channel-design.md",
    "docs/grimoire/design/fleet-status-contract.md",
    "docs/grimoire/design/integration-branch-integrity-design.md",
    "docs/grimoire/design/project-manager-role-design.md",
    "docs/grimoire/design/stealth-mode-design.md",
    "docs/grimoire/design/web-app-support-design.md",
    "docs/grimoire/design/wiki-doc-hierarchy-design.md",
    "docs/roadmap.md",
    "docs/web-app-deployment-protocol.md",
})
# release-planning archives (root-only, always exempt from monolith cap)
_MONOLITH_CAP_RELEASE_PLAN_RE = re.compile(r"^docs/(release-planning/(archived/)?|grimoire/)?release-planning-v[\d.]+\.md$")


def _docs_filenames(root, flavor_root):
    """Return a set of docs-relative paths (e.g. 'docs/design/foo.md')."""
    result = set()
    for p in glob.glob(f"{flavor_root}/docs/**/*.md", recursive=True):
        result.add(os.path.relpath(p, flavor_root))
    return result


def find_root(start: str) -> tuple:
    """Locate the repo root containing CLAUDE.md.

    Framework monorepo: root must also contain a claude-code/ flavor directory.
    Consumer project: root needs only CLAUDE.md (no flavor dirs required).

    Returns (root_path, consumer_mode) where consumer_mode is True when neither
    claude-code/ nor copilot/ flavor directories are present.  Consumer mode
    skips framework-only checks (flavor-parity, manifest-detect-hygiene,
    shipped-pointers) that require the multi-flavor monorepo layout.
    """
    d = os.path.abspath(start)
    while d != "/":
        if os.path.exists(os.path.join(d, "CLAUDE.md")):
            has_cc = os.path.isdir(os.path.join(d, "claude-code"))
            has_cp = os.path.isdir(os.path.join(d, "copilot"))
            consumer_mode = not has_cc and not has_cp
            return d, consumer_mode
        d = os.path.dirname(d)
    raise SystemExit("repo root not found (need CLAUDE.md)")


def rel(root: str, p: str) -> str:
    return os.path.relpath(p, root)


def _is_internal_grimoire_doc(doc_path):
    """True for framework-internal docs/grimoire/ files that live only in the
    upstream repo (root) and never ship in a distributed flavor.

    The whole `docs/grimoire/` tree is internal EXCEPT `docs/grimoire/README.md`
    (the consumer-facing wiki-convention authority, which the build bulkhead
    deliberately keeps shipped). This mirrors build_distributables.py's
    EXCLUDED_PATH_PREFIXES boundary: such files are intentionally absent from the
    claude-code / copilot flavors, so any flavor name-set gap involving one is
    always allowed without a per-file allow-list entry.

    The single exception is copilot's functional sync manifest at
    `docs/grimoire/design/feature-manifest.md` (a per-flavor location quirk, not
    an internal design doc) — but it too is under docs/grimoire/, so it is
    covered here and needs no separate entry.
    """
    return (doc_path.startswith("docs/grimoire/")
            and doc_path != "docs/grimoire/README.md")


def _is_docs_gap_allowed(flavor_a, flavor_b, doc_path, allow_set):
    """Return True if the gap (flavor_a has doc_path, flavor_b does not) is in the allow-list."""
    if _RELEASE_PLAN_RE.match(doc_path):
        return True  # release-planning archives are always root-only; never flag
    if _is_internal_grimoire_doc(doc_path):
        return True  # framework-internal tier — root-only by design (bulkhead)
    return (flavor_a, flavor_b, doc_path) in allow_set


# ── Check 1: flavor parity ──────────────────────────────────────────────
def check_flavor_parity(root: str, _allow_set: set | None = None) -> list:
    """Three-flavor structural parity: root ↔ claude-code ↔ copilot.

    Checks:
      1. Skill presence parity between root and claude-code (existing behaviour).
      2. Content parity for the must-match file set (existing behaviour).
      3. [WH-7] Docs file-name set parity: root ↔ claude-code ↔ copilot,
         with a pre-populated allow-list for known-intentional gaps.
    """
    if _allow_set is None:
        _allow_set = DOCS_PARITY_ALLOW
    findings = []

    # ── (1) Skill presence parity: root ↔ claude-code ──────────────────
    cc_skills = {os.path.basename(os.path.dirname(p))
                 for p in glob.glob(f"{root}/claude-code/.claude/skills/*/SKILL.md")}
    rt_skills = {os.path.basename(os.path.dirname(p))
                 for p in glob.glob(f"{root}/.claude/skills/*/SKILL.md")}
    for s in sorted(cc_skills - rt_skills):
        findings.append(f"skill present in claude-code but not root: {s}")
    for s in sorted(rt_skills - cc_skills):
        findings.append(f"skill present in root but not claude-code: {s}")

    # ── (2) Content parity for must-match set ───────────────────────────
    must_match = ["docs/coding-standards.md",
                  ".claude/skills/grm-sync-from-upstream/feature-manifest.md"]
    must_match += [rel(root, p) for p in glob.glob(f"{root}/docs/coding-standards/*.md")]
    for rp in must_match:
        if rp in PARITY_ALLOW_DIVERGENT:
            continue
        a, b = f"{root}/{rp}", f"{root}/claude-code/{rp}"
        if not os.path.exists(b):
            findings.append(f"must-match file missing in claude-code: {rp}")
            continue
        if open(a).read() != open(b).read():
            findings.append(f"must-match file differs root vs claude-code: {rp}")

    # ── (3) [WH-7] Docs file-name set parity: three flavors ─────────────
    copilot_root = os.path.join(root, "copilot")
    if not os.path.isdir(copilot_root):
        # copilot flavor absent in this repo — skip three-flavor check
        return findings

    rt_docs = _docs_filenames(root, root)
    cc_docs = _docs_filenames(root, os.path.join(root, "claude-code"))
    cp_docs = _docs_filenames(root, copilot_root)
    # Normalise copilot paths: copilot files live under copilot/docs/…,
    # but _docs_filenames already returns them relative to copilot_root,
    # e.g. "docs/design/foo.md".

    pairs = [
        ("root",        rt_docs, "claude-code", cc_docs),
        ("claude-code", cc_docs, "copilot",     cp_docs),
        ("root",        rt_docs, "copilot",     cp_docs),
    ]

    # ── codex flavor: ported from claude-code, docs mirror copilot's set
    # exactly. Guarded by codex/ presence (a consumer or a monorepo predating
    # the codex flavor has no codex/ dir — skip then, like the copilot_root
    # guard above). When present, compare codex against every other flavor so
    # four-flavor docs parity is enforced.
    codex_root = os.path.join(root, "codex")
    if os.path.isdir(codex_root):
        cx_docs = _docs_filenames(root, codex_root)
        pairs += [
            ("root",        rt_docs, "codex", cx_docs),
            ("claude-code", cc_docs, "codex", cx_docs),
            ("copilot",     cp_docs, "codex", cx_docs),
        ]
    for fa, fa_docs, fb, fb_docs in pairs:
        # files in fa but not fb
        for doc in sorted(fa_docs - fb_docs):
            if not _is_docs_gap_allowed(fa, fb, doc, _allow_set):
                findings.append(
                    f"docs file in {fa} but not {fb} (not allow-listed): {doc}"
                )
        # files in fb but not fa
        for doc in sorted(fb_docs - fa_docs):
            if not _is_docs_gap_allowed(fb, fa, doc, _allow_set):
                findings.append(
                    f"docs file in {fb} but not {fa} (not allow-listed): {doc}"
                )

    return findings


# ── Check 2: design-doc layout ──────────────────────────────────────────
# Legacy pattern set — pre-house-template docs must have all four.
_LEGACY_SECTIONS = ["motivation", "goals", "non-goal", "validation|idempotency"]
# House-template section set — docs/design/README.md house layout; all three required.
_HOUSE_SECTIONS  = ["motivation", "scope", "design|acceptance"]

def _has_section(low, pattern):
    """Return True if any heading in *low* matches any '|'-separated alt."""
    return any(re.search(rf"#+ .*{alt}", low) for alt in pattern.split("|"))

def _layout_ok(low, section_list):
    return all(_has_section(low, s) for s in section_list)

def check_design_layout(root: str) -> list:
    # "Bulkhead": design docs live at BOTH a consumer's project-own tier
    # (docs/design/*-design.md) AND the framework-internal tier
    # (docs/grimoire/design/*-design.md, where the relocated framework specs now
    # live). Both are house-section-checked; the house-section rules are
    # identical. See documentation-separation-design.md §2.
    findings = []
    design_globs = [
        f"{root}/docs/design/*-design.md",
        f"{root}/docs/grimoire/design/*-design.md",
    ]
    for p in sorted(g for pat in design_globs for g in glob.glob(pat)):
        low = open(p).read().lower()
        legacy_ok = _layout_ok(low, _LEGACY_SECTIONS)
        house_ok  = _layout_ok(low, _HOUSE_SECTIONS)
        if not legacy_ok and not house_ok:
            findings.append(
                f"{rel(root,p)}: does not satisfy either the legacy section set "
                f"(motivation/goals/non-goal/validation) or the house-template set "
                f"(motivation/scope/design-or-acceptance)"
            )
        if "## open questions" in low and re.search(r"todo|tbd|\?\?\?", low):
            findings.append(f"{rel(root,p)}: unresolved open-questions marker")
    return findings


# ── design-doc-purity check (#358, v3.98) ───────────────────────────────
# The 2026-07-10 audit found work-tracking pollution in ~40 of 104 design
# docs (#352-#357): Status stamps, checked Acceptance boxes, "shipped in
# vX.Y" release narration, and file-level work-item maps written into what
# are supposed to be timeless working agreements. Root cause: nothing
# enforced the separation. This check makes it deterministic and blocking
# under --strict, so a regression is caught at branch-done/closeout instead
# of the next full-repo audit. Completion state belongs in the release-plan
# §5 ledger (grm-ledger-tick), never in the design doc itself.
#
# Scans both design-doc tiers recursively (docs/design/** and
# docs/grimoire/design/**, including subdirectories like docs/design/ux/),
# excluding README.md index pages — the same exclusion _design_index_files
# above already applies to the (non-recursive) tier listing.
DESIGN_DOC_TIERS = ("docs/design", "docs/grimoire/design")

# Whole-file exemptions: a design doc that legitimately *discusses* one of
# these patterns as prose-about-the-convention (e.g. this very check's own
# design doc, quoting "Status:" as an example of what NOT to write) rather
# than an actual instance of it. Add an entry only after confirming the
# match is a documented example, not real pollution. Relative-to-root paths.
DESIGN_PURITY_ALLOW = frozenset({
    # Follow-up explains that an unrelated *prerequisite* mechanism (signing
    # infra) is already operational, referencing when IT landed — not a
    # status claim about this design's own subject. Verified 2026-07-13.
    "docs/grimoire/design/dependency-channel-design.md",
    # Non-goals cites when an unrelated concern (Rust tooling) shipped, to
    # justify excluding it from this design's scope — not self-narration.
    # Verified 2026-07-13.
    "docs/grimoire/design/html-css-quality-enforcement-design.md",
    # Motivation recounts the history of a DIFFERENT, already-closed issue
    # (#87, closed v3.27) as prior art justifying this new design — not a
    # completion claim about this doc's own subject. Verified 2026-07-13.
    "docs/grimoire/design/meta-updater-package-design.md",
})

# 1. A `Status:` line in the opening ~10 lines (optionally blockquoted /
#    bolded — "> **Status**: shipped"). Case-insensitive: real docs use
#    both "Status:" and "status:".
_PURITY_STATUS_RE = re.compile(r"^>?\s*\**Status\**\s*:", re.I | re.M)
# 2. Any checked box, anywhere in the doc — Acceptance criteria in a design
#    doc are a template to fill in on adoption, never a per-release ledger.
_PURITY_CHECKED_BOX_RE = re.compile(r"^\s*-\s\[[xX]\]", re.M)
# 3. Release-narration phrasing. Code-fenced/inline-code spans are stripped
#    first (_strip_code) so a doc that quotes these phrases as a code
#    example (rather than asserting them in prose) is not flagged.
#    "Delivered" gets its own, narrower pattern below: a bare `\bdelivered\b`
#    also matches ordinary prose ("browser-delivered", "delivered by",
#    "delivered mechanically", "(always delivered)") that has nothing to do
#    with release status — real usages found on the live tree were exactly
#    this false-positive class. The real pollution shape is a standalone
#    status marker as the last word on a line (often bolded, e.g. the
#    "~~superseded text~~ **Delivered**" Follow-up convention #356 removed),
#    which _PURITY_DELIVERED_MARKER_RE targets by anchoring to end-of-line —
#    "delivered" used mid-sentence or before trailing punctuation never
#    matches.
_PURITY_NARRATION_RE = re.compile(
    r"shipped in v\d|landed in v\d|closed in v\d|implemented\s*\(#"
    r"|—\s*done\b|\(addressed in v\d",
    re.I,
)
_PURITY_DELIVERED_MARKER_RE = re.compile(
    r"\*{0,2}Delivered\*{0,2}\s*$",
    re.M | re.I,
)
# 4. A work-item-map heading or a "Phase N closed" heading, at any level.
_PURITY_WORKMAP_HEADING_RE = re.compile(
    r"^#{1,6}\s.*(file-level changes|work-item map)", re.M | re.I
)
_PURITY_PHASE_CLOSED_HEADING_RE = re.compile(
    r"^#{1,6}\s.*phase\s+\d+\s+closed", re.M | re.I
)
# 5. Filenames that are themselves work-tracking artifacts, not a feature
#    description — these belong in release-planning, not a design tier.
_PURITY_FILENAME_RE = re.compile(r"-(plan|candidates)\.md$", re.I)


def _design_doc_purity_paths(root):
    """All *.md under both design tiers, recursive, excluding README.md
    index pages (mirrors _design_index_files' README exclusion above,
    extended to subdirectories such as docs/design/ux/)."""
    paths = []
    for tier in DESIGN_DOC_TIERS:
        tier_dir = os.path.join(root, tier)
        if not os.path.isdir(tier_dir):
            continue
        for p in glob.glob(f"{tier_dir}/**/*.md", recursive=True):
            if os.path.basename(p) == "README.md":
                continue
            paths.append(p)
    return sorted(paths)


def design_doc_purity_findings(relpath: str, raw: str) -> list:
    """Pure, single-file purity scan (#358) — the one place all five regex
    patterns are evaluated against a doc's content. `relpath` is used only
    for the filename pattern (#5) and to prefix each finding string; `raw`
    is the doc's full text (not yet allow-listed or path-filtered — callers
    apply DESIGN_PURITY_ALLOW / tier / README-exclusion themselves).

    Extracted (v3.98, #414) so the mid-task `design-doc-guard.sh` PreToolUse
    hook can evaluate a proposed Edit/Write's resulting content with the
    IDENTICAL patterns this closeout check uses, instead of a second,
    drift-prone copy of the five regexes — both call sites import this
    function; neither redefines a pattern.

    Five independent patterns, each its own finding:

      1. a `Status:` line in the opening ~10 lines
      2. any checked `- [x]` box, anywhere in the doc
      3. release-narration phrasing ("shipped in vX.Y", "Implemented (#123",
         "— DONE", "Delivered", "(Addressed in vX.Y", ...), code-stripped
      4. a work-item-map heading ("## File-level changes", "work-item map")
         or a "Phase N closed" heading
      5. a filename matching *-plan.md / *-candidates.md under a design tier
    """
    findings = []
    if _PURITY_FILENAME_RE.search(os.path.basename(relpath)):
        findings.append(
            f"{relpath}: filename matches a work-tracking pattern "
            f"(*-plan.md / *-candidates.md) — design docs describe a "
            f"feature, not a plan; rename or relocate to release-planning"
        )
    head = "\n".join(raw.splitlines()[:10])
    if _PURITY_STATUS_RE.search(head):
        findings.append(
            f"{relpath}: 'Status:' line in the opening ~10 lines — "
            f"completion state belongs in the release-plan §5 ledger, "
            f"not the design doc"
        )
    if _PURITY_CHECKED_BOX_RE.search(raw):
        findings.append(
            f"{relpath}: checked '- [x]' box — never tick Acceptance "
            f"boxes in a design doc; tick the ledger instead "
            f"(grm-ledger-tick)"
        )
    stripped = _strip_code(raw)
    if _PURITY_NARRATION_RE.search(stripped) or _PURITY_DELIVERED_MARKER_RE.search(stripped):
        findings.append(
            f"{relpath}: release-narration phrasing (e.g. 'shipped in "
            f"vX.Y', 'Implemented (#...', '— DONE', 'Delivered') — "
            f"design docs are timeless, not a changelog"
        )
    if (_PURITY_WORKMAP_HEADING_RE.search(stripped)
            or _PURITY_PHASE_CLOSED_HEADING_RE.search(stripped)):
        findings.append(
            f"{relpath}: work-item-map / 'Phase N closed' heading — "
            f"file-level change ledgers belong in release-planning, not "
            f"the design doc"
        )
    return findings


def check_design_doc_purity(root: str, _allow_set: set | None = None) -> list:
    """Flag work-tracking / release-narration pollution in a design doc
    (#358). Design docs are timeless working agreements; completion state
    belongs in the release-plan §5 ledger (grm-ledger-tick), never in the
    doc itself. Walks both design tiers and delegates the actual pattern
    evaluation to `design_doc_purity_findings` (single source of truth for
    the five patterns — see that function's docstring for the list).

    DESIGN_PURITY_ALLOW (or _allow_set, for tests — mirrors
    check_flavor_parity / check_mirrored_script_parity's override param)
    exempts a specific relative path from every pattern (whole-file
    allowlist) for the rare doc that legitimately quotes one of these
    patterns as documented prose about the convention itself, rather than
    an instance of the pollution it describes.
    """
    allow = DESIGN_PURITY_ALLOW if _allow_set is None else _allow_set
    findings = []
    for p in _design_doc_purity_paths(root):
        relpath = rel(root, p)
        if relpath in allow:
            continue
        try:
            raw = open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            raw = None
        if raw is None:
            # Unreadable content — the filename pattern is still checkable.
            if _PURITY_FILENAME_RE.search(os.path.basename(p)):
                findings.append(
                    f"{relpath}: filename matches a work-tracking pattern "
                    f"(*-plan.md / *-candidates.md) — design docs describe "
                    f"a feature, not a plan; rename or relocate to "
                    f"release-planning"
                )
            continue
        findings.extend(design_doc_purity_findings(relpath, raw))
    return findings


# ── Check 3: link integrity ─────────────────────────────────────────────
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
def _strip_code(text):
    # Links inside fenced or inline code are examples, not real references.
    text = FENCE_RE.sub("", text)
    return INLINE_CODE_RE.sub("", text)
def check_links(root: str) -> list:
    findings = []
    md = [p for p in glob.glob(f"{root}/**/*.md", recursive=True)
          if "/.git/" not in p and "/.scaffold-base/" not in p]
    for p in md:
        base = os.path.dirname(p)
        for m in LINK_RE.finditer(_strip_code(open(p).read())):
            t = m.group(1).strip()
            if t.startswith(("http://", "https://", "#", "mailto:")):
                continue
            t = t.split("#", 1)[0].split("?", 1)[0]
            if not t or t.startswith("<"):
                continue
            target = os.path.normpath(os.path.join(base, t))
            if not os.path.exists(target):
                findings.append(f"{rel(root,p)} → dead link: {t}")
    return findings


# ── Check: shipped-pointers ("Clean-Room") ──────────────────────────────
# Pointer-integrity rule (clean-room-design.md §4, extending the "Bulkhead"'s
# CRITICAL invariant): "No shipped doc may contain a relative link to an
# excluded or relocated doc." A shipped doc linking a target that never ships
# would dangle in a consumer install. We reuse the build gate's
# EXCLUDED_PATH_PREFIXES as the single exclusion source — no second hardcoded
# copy (clean-room-design §4).
_SHIPPED_FLAVORS = ("", "claude-code", "codex", "copilot")  # "" == repo-root (the dogfood flavor)

# Exclude-and-seed targets (clean-room-design §4): excluded at the
# ship gates (Grimoire's own copy never ships) BUT a consumer receives an empty
# *seeded* copy at the same path. A relative link to one of these therefore
# resolves fine on a consumer install — it is NOT a dangling pointer, so it is
# exempt from the shipped-pointers rule even though it appears in the gate's
# EXCLUDED_PATH_PREFIXES. (Pure-relocate targets stay flagged.)
_SEEDED_NOT_DANGLING = ("docs/version-history.md",)


def _load_excluded_prefixes():
    """Import EXCLUDED_PATH_PREFIXES from the build gate (single source of truth).

    The gate lives at <flavor>/.claude/skills/grm-project-release/build_distributables.py;
    its prefix tuple is flavor-relative posix prefixes (the same surface the
    sync gate mirrors). Returns the tuple, or () if the gate cannot be loaded
    (the check then degrades to a no-op rather than hardcoding a stale list).
    """
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    # Walk up to the repo root, then to the build gate.
    d = here
    while d != "/":
        cand = os.path.join(d, ".claude", "skills", "grm-project-release",
                            "build_distributables.py")
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("_grimoire_build_gate", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return tuple(getattr(mod, "EXCLUDED_PATH_PREFIXES", ()))
        d = os.path.dirname(d)
    return ()


def check_shipped_pointers(root: str) -> list:
    """Assert no shipped doc links a target under an EXCLUDED_PATH_PREFIXES entry.

    Shipped surface (per clean-room-design §4): each flavor's docs/ tree MINUS
    docs/grimoire/** (that subtree is excluded / never ships, so links *from*
    inside it are not consumer-visible). For every relative markdown link in a
    shipped doc, resolve it to the flavor-relative posix path and flag it when it
    starts with any excluded prefix — that link would dangle in a consumer
    install. Deterministic, CI-able; reuses the gate's exclusion source.
    """
    prefixes = _load_excluded_prefixes()
    if not prefixes:
        return ["shipped-pointers: could not load EXCLUDED_PATH_PREFIXES from build gate"]
    findings = []
    for flavor in _SHIPPED_FLAVORS:
        flavor_root = os.path.join(root, flavor) if flavor else root
        docs_dir = os.path.join(flavor_root, "docs")
        if not os.path.isdir(docs_dir):
            continue
        for p in glob.glob(f"{docs_dir}/**/*.md", recursive=True):
            # Source surface excludes never-ships subtrees: docs/grimoire/** and
            # the relocated docs/release-planning/** tier. Links *from*
            # inside an excluded doc are never consumer-visible, so they cannot
            # dangle in a consumer install.
            src_rel = os.path.relpath(p, flavor_root).replace(os.sep, "/")
            if src_rel.startswith(("docs/grimoire/", "docs/release-planning/")):
                continue
            base = os.path.dirname(p)
            for m in LINK_RE.finditer(_strip_code(open(p).read())):
                t = m.group(1).strip()
                if t.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                t = t.split("#", 1)[0].split("?", 1)[0]
                if not t or t.startswith("<"):
                    continue
                # Resolve the link relative to the source file, then express it
                # flavor-relative (the surface EXCLUDED_PATH_PREFIXES is keyed on).
                abs_target = os.path.normpath(os.path.join(base, t))
                tgt_rel = os.path.relpath(abs_target, flavor_root).replace(os.sep, "/")
                if tgt_rel in _SEEDED_NOT_DANGLING:
                    continue  # consumer gets a seeded copy — link resolves
                if any(tgt_rel.startswith(pref) for pref in prefixes):
                    label = flavor or "root"
                    findings.append(
                        f"[{label}] {src_rel} → links excluded/relocated doc: {t} ({tgt_rel})")
    return sorted(findings)


# ── Check 4: docs map ───────────────────────────────────────────────────
def docs_md_files(root: str) -> list:
    return sorted(rel(root, p) for p in glob.glob(f"{root}/docs/**/*.md", recursive=True)
                  if os.path.basename(p) != "README.md")


def _build_nested_map(root):
    """Build a nested tree map of docs/**/*.md grouped by subdirectory."""
    files = docs_md_files(root)
    # Group files by their first subdirectory under docs/
    # E.g. "docs/design/foo.md" → subdir "design", "docs/bar.md" → subdir ""
    groups = {}  # subdir -> [rel_paths]
    top_level = []
    for f in files:
        # f is like "docs/design/foo.md" or "docs/bar.md"
        inner = f[len("docs/"):]  # e.g. "design/foo.md" or "bar.md"
        if "/" not in inner:
            top_level.append(f)
        else:
            subdir = inner.split("/")[0]
            groups.setdefault(subdir, []).append(f)

    lines = []
    if top_level:
        lines.append("### Top level")
        lines.append("")
        for f in top_level:
            name = f[len("docs/"):]
            lines.append(f"- [`{name}`]({name})")
        lines.append("")

    for subdir in sorted(groups.keys()):
        lines.append(f"### `{subdir}/`")
        lines.append("")
        for f in groups[subdir]:
            name = f[len("docs/"):]
            lines.append(f"- [`{name}`]({name})")
        lines.append("")

    return lines


def build_map(root: str) -> str:
    """Build the full docs/README.md content with marker-delimited map section.

    If docs/README.md exists and contains markers, rewrites only between markers.
    If markers are absent, appends them and the map.
    Idempotent: calling twice with the same tree produces the same output.
    """
    mp = f"{root}/docs/README.md"
    map_lines = _build_nested_map(root)
    map_content = "\n".join(map_lines)

    begin_marker = "<!-- docs-map:begin -->"
    end_marker = "<!-- docs-map:end -->"

    if os.path.exists(mp):
        existing = open(mp).read()
        if begin_marker in existing and end_marker in existing:
            # Replace only between the markers (preserve curated content outside).
            before, rest = existing.split(begin_marker, 1)
            _, after = rest.split(end_marker, 1)
            new_content = (
                before
                + begin_marker + "\n"
                + map_content
                + end_marker
                + after
            )
            return new_content
        else:
            # Append markers + map at end.
            sep = "\n" if existing.endswith("\n") else "\n\n"
            new_content = (
                existing.rstrip("\n")
                + "\n\n"
                + begin_marker + "\n"
                + map_content
                + end_marker + "\n"
            )
            return new_content
    else:
        # No file yet — create a minimal one.
        lines = [
            "# Documentation map",
            "",
            "> Generated + validated by `grm-doc-assurance` (check `docs-map`). Lists every",
            "> file under `docs/`. Regenerate with `doc_assurance.py docs-map --write-map`.",
            "",
            begin_marker,
            map_content,
            end_marker,
            "",
        ]
        return "\n".join(lines)


def check_docs_map(root: str, write: bool = False) -> list:
    mp = f"{root}/docs/README.md"
    if write:
        new_content = build_map(root)
        open(mp, "w").write(new_content)
        return []
    findings = []
    if not os.path.exists(mp):
        return ["docs/README.md (documentation map) missing — run with --write-map"]
    content = open(mp).read()
    begin_marker = "<!-- docs-map:begin -->"
    end_marker = "<!-- docs-map:end -->"
    if begin_marker in content and end_marker in content:
        # Only the generated region is diffed against docs_md_files(); hand-curated
        # prose outside the markers (e.g. a "## Tiers" section linking child
        # README.md index pages) is intentionally not part of the generated map's
        # completeness contract. check_links still verifies every link in
        # that curated prose resolves to a real file, so this narrowing does not
        # drop any actual dead-link coverage.
        _, rest = content.split(begin_marker, 1)
        content = rest.split(end_marker, 1)[0]
    listed = set(re.findall(r"\]\(([^)]+\.md)\)", content))
    listed = {os.path.normpath(os.path.join("docs", x)) for x in listed}
    actual = set(docs_md_files(root))
    for f in sorted(actual - listed):
        findings.append(f"docs map missing entry: {f}")
    for f in sorted(listed - actual):
        findings.append(f"docs map stale entry (no file): {f}")
    return findings


# ── Design-doc index generation (maintenance-automation-design.md §1) ──
# Two curated tiers each carry a hand-maintained index:
#   docs/design/README.md            — a consumer project's own design docs
#   docs/grimoire/design/README.md   — Grimoire's framework-internal specs
# `--write-design-index` regenerates a `<!-- design-index:begin -->` /
# `<!-- design-index:end -->` marker-delimited table region in each, scanning
# the tier's *-design.md-and-friends (docs/design/*.md / docs/grimoire/design/*.md,
# excluding README.md) for the house-layout title (first "# " heading) and the
# first line of prose under an exact "## Motivation" heading (the one-line
# "Area" description). A doc missing either is reported as a finding, never
# silently skipped or given an empty row.
#
# docs/grimoire/design/README.md already carries a rich, hand-curated set of
# "##"-grouped sections (Charter deliverables, Agent roles & autonomy, …) that
# encode editorial judgement a script cannot replicate — those sections are
# left untouched. The generated region is appended as a single "All design
# docs (generated)" table at the bottom of each README, guaranteeing no doc is
# ever silently missing from the index (the bug this closes) without fighting
# the existing curation. docs/design/README.md's existing "## Index" table has
# no such curation (it is a placeholder), so the same generated table simply
# becomes that tier's actual index.
DESIGN_INDEX_BEGIN = "<!-- design-index:begin -->"
DESIGN_INDEX_END = "<!-- design-index:end -->"

# (subdir-relative-to-root, heading printed above the generated table)
DESIGN_INDEX_TIERS = (
    ("docs/design", "All design docs (generated)"),
    ("docs/grimoire/design", "All design docs (generated)"),
)

_DESIGN_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
# Accepts the plain house-layout "## Motivation" as well as the numbered/
# section-marker variants some existing docs use ("## 1. Motivation", "## §1
# — Motivation, overview and goals") — same tolerance check_design_layout's
# _has_section already applies for the design-layout pass/fail gate, reused
# here so a doc that already passes that gate isn't spuriously reported as
# house-layout-missing just for numbering its heading.
_DESIGN_MOTIVATION_RE = re.compile(r"^##\s+(?:[\w§.]+\s*[-—.]*\s*)?Motivation\b.*$", re.M | re.I)


def _design_index_files(root, tier_subdir):
    """*.md files directly under tier_subdir, excluding README.md, sorted by filename."""
    tier_dir = os.path.join(root, tier_subdir)
    files = [p for p in glob.glob(f"{tier_dir}/*.md")
             if os.path.basename(p) != "README.md"]
    return sorted(files, key=lambda p: os.path.basename(p).lower())


_DESIGN_AREA_MAX_LEN = 160  # chars — safety cap so one runaway sentence can't blow up a table row
_SENTENCE_END_RE = re.compile(r"(?<!\b[A-Z])[.!?](?:\s|$)")  # crude but deterministic first-sentence split


def _first_sentence(paragraph):
    """Return the first sentence of a soft-wrapped markdown paragraph.

    Markdown paragraphs commonly soft-wrap across several physical lines
    (~76-80 cols in this repo); joining consecutive non-blank lines before
    splitting avoids handing back a line fragment cut mid-word. Falls back to
    the whole (capped) paragraph when no sentence-ending punctuation is found.
    """
    text = " ".join(paragraph)
    m = _SENTENCE_END_RE.search(text)
    sentence = text[:m.end()].strip() if m else text
    if len(sentence) > _DESIGN_AREA_MAX_LEN:
        sentence = sentence[:_DESIGN_AREA_MAX_LEN].rstrip() + "…"
    return sentence


def _parse_design_doc(path):
    """Extract (title, area) from a house-layout design doc.

    title — the first "# " heading (house-layout feature title).
    area  — the first sentence of prose immediately under a "## Motivation"
            heading (tolerant of numbered/§-prefixed variants — see
            _DESIGN_MOTIVATION_RE — since this is author-facing table content,
            not the design-layout pass/fail gate). Consecutive non-blank
            physical lines are joined into one paragraph first so a markdown
            soft-wrap doesn't get truncated mid-word.

    Returns (title, area, error) — error is None when both are found, else a
    short string naming what's missing (the doc is still reported, never
    silently skipped).
    """
    try:
        content = open(path, encoding="utf-8").read()
    except Exception as e:
        return None, None, f"unreadable ({e})"

    title_m = _DESIGN_TITLE_RE.search(content)
    title = title_m.group(1).strip() if title_m else None

    area = None
    mot_m = _DESIGN_MOTIVATION_RE.search(content)
    if mot_m:
        paragraph = []
        for line in content[mot_m.end():].splitlines():
            if line.strip():
                paragraph.append(line.strip())
            elif paragraph:
                break  # end of the first paragraph under Motivation
        if paragraph:
            area = _first_sentence(paragraph)

    missing = []
    if not title:
        missing.append("no '# ' title")
    if not mot_m or not area:
        missing.append("no '## Motivation' section (or it has no prose)")
    error = ", ".join(missing) if missing else None
    return title, area, error


def _design_index_table_cell(text):
    """Escape a value for safe embedding in a GFM table cell."""
    return text.replace("|", "\\|") if text else ""


def build_design_index_table(root: str, tier_subdir: str) -> tuple:
    """Return (table_markdown_lines, findings) for one design-doc tier.

    table_markdown_lines is the full '| Document | Area |' table (header +
    separator + one row per parseable doc), sorted deterministically by
    filename. Docs missing the house layout are excluded from the table but
    reported in findings (never silently skipped, never given an empty row).
    """
    findings = []
    rows = []
    for p in _design_index_files(root, tier_subdir):
        fname = os.path.basename(p)
        title, area, error = _parse_design_doc(p)
        if error:
            findings.append(f"{tier_subdir}/{fname}: missing house layout ({error}) — excluded from generated index")
            continue
        rows.append((fname, title, area))

    lines = ["| Document | Area |", "|---|---|"]
    for fname, title, area in rows:
        link = f"[{fname}]({fname})"
        cell = f"**{_design_index_table_cell(title)}** — {_design_index_table_cell(area)}"
        lines.append(f"| {link} | {cell} |")
    if not rows:
        lines.append("| *(no design docs found)* | |")
    return lines, findings


def _render_design_index_region(root, tier_subdir, section_heading):
    """Render the full marker-delimited region (heading + table) for one tier."""
    table_lines, findings = build_design_index_table(root, tier_subdir)
    region_lines = [f"### {section_heading}", ""] + table_lines
    return "\n".join(region_lines), findings


def _apply_marker_region(existing, begin_marker, end_marker, region_content):
    """Replace (or append) a marker-delimited region in existing file content.

    Shared mechanics with build_map: if both markers are present, only the
    content between them is replaced (curated prose outside survives
    untouched); if absent, the markers + region are appended at the end.
    """
    if begin_marker in existing and end_marker in existing:
        before, rest = existing.split(begin_marker, 1)
        _, after = rest.split(end_marker, 1)
        return before + begin_marker + "\n" + region_content + "\n" + end_marker + after
    sep = "\n" if existing.endswith("\n") else "\n\n"
    return existing.rstrip("\n") + "\n\n" + begin_marker + "\n" + region_content + "\n" + end_marker + "\n"


def build_design_index_readme(root: str, tier_subdir: str, section_heading: str) -> tuple:
    """Build the full regenerated content for one tier's README.md.

    Returns (new_content, findings). If the README doesn't exist yet, a
    minimal one is created (mirrors build_map's no-file-yet branch).
    """
    readme_path = os.path.join(root, tier_subdir, "README.md")
    region_content, findings = _render_design_index_region(root, tier_subdir, section_heading)
    if os.path.exists(readme_path):
        existing = open(readme_path, encoding="utf-8").read()
        new_content = _apply_marker_region(existing, DESIGN_INDEX_BEGIN, DESIGN_INDEX_END, region_content)
    else:
        lines = [
            f"# {os.path.basename(tier_subdir)} design docs",
            "",
            DESIGN_INDEX_BEGIN,
            region_content,
            DESIGN_INDEX_END,
            "",
        ]
        new_content = "\n".join(lines)
    return new_content, findings


def build_design_indexes(root: str) -> dict:
    """Regenerate both tier READMEs. Returns dict: tier_subdir -> (content, findings)."""
    return {
        tier_subdir: build_design_index_readme(root, tier_subdir, heading)
        for tier_subdir, heading in DESIGN_INDEX_TIERS
    }


def check_design_index_stale(root: str, write: bool = False) -> list:
    """Check (default) or --write-design-index (write=True) for both tiers.

    Findings cover two independent things, both surfaced (never silently
    skipped): (a) a README whose generated region is stale/missing relative
    to what --write-design-index would produce, and (b) any doc missing the
    house layout (excluded from the table but always reported).
    """
    findings = []
    for tier_subdir, heading in DESIGN_INDEX_TIERS:
        new_content, parse_findings = build_design_index_readme(root, tier_subdir, heading)
        readme_path = os.path.join(root, tier_subdir, "README.md")
        if write:
            open(readme_path, "w", encoding="utf-8").write(new_content)
        else:
            if not os.path.exists(readme_path):
                findings.append(f"{tier_subdir}/README.md missing — run with --write-design-index")
            else:
                current = open(readme_path, encoding="utf-8").read()
                if current != new_content:
                    findings.append(
                        f"{tier_subdir}/README.md design-index region is stale — "
                        f"run with --write-design-index to regenerate"
                    )
            findings.extend(parse_findings)
    return findings


# ── Check 5: release consistency ────────────────────────────────────────
# Captures an optional third (patch) component: a two-part-only pattern here
# would silently truncate a vX.Y.Z heading to vX.Y (matching the shared
# \d+\.\d+ prefix, no error) rather than either parsing or rejecting it —
# corrupting comparisons/dedup if this repo or the fleet ever tag vX.Y.Z
# (audit finding, v3.91; see check_tag_format below).
VER_RE = re.compile(r"^##\s+v(\d+\.\d+(?:\.\d+)?)", re.M)
def check_release_consistency(root: str) -> list:
    findings = []
    # The release-consistency surface (version-history, roadmap, feature-manifest,
    # config) is framework-owned and seeded by bootstrap.  A downstream project
    # that has not adopted one of these files should report it as a finding, not
    # crash with an unhandled FileNotFoundError (consumer-mode robustness).
    vh_path = f"{root}/docs/version-history.md"
    rm_path = f"{root}/docs/roadmap.md"
    if not os.path.exists(vh_path):
        findings.append("docs/version-history.md missing — release consistency cannot be checked")
        return findings
    if not os.path.exists(rm_path):
        findings.append("docs/roadmap.md missing — release consistency cannot be checked")
        return findings
    vh = open(vh_path).read()
    rm = open(rm_path).read()
    hist = set(VER_RE.findall(vh))
    # roadmap shipped versions: a vX.Y section whose body says Shipped/released
    shipped = set()
    for m in re.finditer(r"^##\s+v(\d+\.\d+(?:\.\d+)?)(.*?)(?=^##\s+v|\Z)", rm, re.S | re.M):
        if re.search(r"shipped|released", m.group(2), re.I):
            shipped.add(m.group(1))
    for v in sorted(hist - shipped, key=lambda s: tuple(map(int, s.split(".")))):
        findings.append(f"v{v} in version-history but not marked Shipped in roadmap")
    # manifest-version monotonic int + framework-version >= newest shipped
    mani_path = f"{root}/.claude/skills/grm-sync-from-upstream/feature-manifest.md"
    if os.path.exists(mani_path):
        mani = open(mani_path).read()
        mv = re.search(r"manifest-version:\s*(\d+)", mani)
        if not mv:
            findings.append("feature-manifest.md: no integer manifest-version")
    cfg_path = f"{root}/.claude/grimoire-config.json"
    fw = ""
    if os.path.exists(cfg_path):
        try:
            cfg = json.load(open(cfg_path))
            fw = cfg.get("framework-version", "").lstrip("v")
        except (ValueError, AttributeError):
            findings.append(".claude/grimoire-config.json: unreadable or malformed framework-version")
    if hist:
        newest = max(hist, key=lambda s: tuple(map(int, s.split("."))))
        if fw and tuple(map(int, fw.split("."))) < tuple(map(int, newest.split("."))):
            findings.append(f"framework-version {fw} < newest shipped v{newest}")
    return findings


# ── Check 5a: tag format (warn-only, like skill-budget) ─────────────────
_TWO_PART_TAG_RE = re.compile(r"^v?\d+\.\d+$")


def check_tag_format(root: str) -> list:
    """Warn (never block outside --strict) when this repo's newest git tag is
    a plain two-part vX.Y instead of the fleet-wide recommended three-part
    vX.Y.Z (audit finding, v3.91; docs/grimoire/version-design.md). Existing
    two-part tag history stays fully conformant and is never force-migrated —
    this is a forward-looking nudge, not a gate. Skipped when git or a tag
    list is unavailable (not a repo, no tags yet)."""
    findings = []
    try:
        proc = subprocess.run(["git", "tag", "--sort=-v:refname"], cwd=root,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return findings
    if proc.returncode != 0:
        return findings
    tags = [t for t in proc.stdout.splitlines() if t.strip()]
    if not tags:
        return findings
    newest = tags[0]
    if _TWO_PART_TAG_RE.match(newest):
        findings.append(
            f"newest tag {newest!r} is two-part vX.Y; the fleet-wide "
            "recommended format is three-part vX.Y.Z — no history migration "
            "required, this is a forward nudge (docs/grimoire/version-design.md)")
    return findings


# ── Check 5b: feature-manifest detect-predicate hygiene ─────────────────
def check_manifest_detect_hygiene(root: str) -> list:
    """A feature-manifest DETECT predicate must reference only artifacts that
    are actually distributed to a consumer (skills, scripts, config). It must
    not depend on a sync/build-excluded framework-internal doc (the
    Bulkhead) — that path is never delivered, so the detect can never pass on a
    consumer and the adoption is silently skipped every sync. Only the detect
    column is scanned; summary/adopt prose may cite an internal design doc."""
    findings = []
    mani = f"{root}/.claude/skills/grm-sync-from-upstream/feature-manifest.md"
    if not os.path.exists(mani):
        return findings
    for ln in open(mani).read().splitlines():
        if not ln.startswith("| `"):
            continue
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) < 7:
            continue
        fid, detect = cells[1], cells[4]
        for pref in MANIFEST_EXCLUDED_PREFIXES:
            if pref in detect:
                findings.append(
                    f"feature-manifest {fid}: detect references sync-excluded "
                    f"path '{pref}' — detect on a distributed artifact instead "
                    f"(it can never pass on a consumer)")
    return findings


# ── Check 5b2: unresolved skill-placeholder tokens (#465) ────────────────
# A `{xxx-command}`-shaped token left in a SKILL.md's *operative* text (a
# fenced code block, or a `- [ ]` checklist line) is a live bug: an agent
# following the skill literally will try to execute the literal token as a
# shell command. Same defect family as #442's substitute() fail-loud guard —
# this is the SKILL-prose equivalent, deterministic instead of relying on an
# agent noticing at run time.
#
# Only *operative* positions are scanned (fenced code blocks and checklist
# items) — a skill that merely *discusses* the token as prose (e.g.
# grm-hard-reset, grm-sync-from-source documenting the genericization
# mechanism itself) is not a bug and must not be flagged. Only `SKILL.md`
# files are in scope ("installed copy of a skill file") — CLAUDE.md's own
# `{test-command}` etc. table cells are a separate, legitimate per-project
# bootstrap-fill contract (self-documented in CLAUDE.md §Project commands)
# and are out of scope here.
_SKILL_PLACEHOLDER_TOKEN_RE = re.compile(r"\{[a-z][a-z-]*-command\}")
_CHECKLIST_LINE_RE = re.compile(r"^\s*-\s*\[[ xX]\]")


def check_skill_placeholder_tokens(root: str) -> list:
    """Fail when an installed SKILL.md (`.claude/skills/**` or
    `.claude/paradigms/**`) still carries a literal, un-substituted
    `{...-command}`-style placeholder token inside a fenced code block or a
    `- [ ]` checklist line — the position an agent would read as something to
    literally run or verify. Root-cause fix: `grm-release-phase` /
    `grm-release-phase-merge` (and their paradigm variants) now resolve
    test/build commands via `python3 .claude/skills/grm-build-recipe/recipe.py
    <target>` instead of carrying a raw placeholder; this check guards against
    the pattern reappearing."""
    findings = []
    patterns = [
        os.path.join(root, ".claude", "skills", "**", "SKILL.md"),
        os.path.join(root, ".claude", "paradigms", "**", "*SKILL.md"),
    ]
    paths = set()
    for pat in patterns:
        paths.update(glob.glob(pat, recursive=True))
    for path in sorted(paths):
        rel = os.path.relpath(path, root)
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        in_code_block = False
        for i, ln in enumerate(lines, start=1):
            stripped = ln.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            m = _SKILL_PLACEHOLDER_TOKEN_RE.search(ln)
            if not m:
                continue
            if in_code_block or _CHECKLIST_LINE_RE.match(ln):
                findings.append(
                    f"{rel}:{i}: unresolved placeholder {m.group(0)!r} in "
                    f"operative position — resolve via "
                    f"`python3 .claude/skills/grm-build-recipe/recipe.py "
                    f"<target>` (or equivalent) instead of a raw token")
    return findings


# ── Check 5d: check-for-checks — doctrine meta-check (#440, v3.97) ───────
# Doctrine (docs/architecture-guidelines.md §Standards doctrine): every
# mandated standard MUST ship with (a) a deterministic check, (b) a named
# gate that runs it, (c) a severity ramp (WARN on introduction -> block once
# cleanup lands). This is the release-gate meta-check for that doctrine: a
# MUST-clause-shaped rule in docs/coding-standards.md, or an entry in the
# required-feature catalog, with no paired reference to a deterministic
# check anywhere in its own section is itself flagged, WARN-tier.
#
# Detection heuristic (deliberately simple — WARN-tier advisory, not a hard
# gate; over-engineering the parser is out of scope):
#   - A "MUST-clause" is a standalone, upper-case whole-word "MUST" — the
#     RFC2119-style convention this repo's own catalog already uses for a
#     hard mandate (see required-feature-catalog.md's "**Spec.** ... MUST
#     ..." paragraphs). Documented limitation: lower-case "must" is NOT
#     detected — informal "must" prose is common in explanatory text and
#     would over-fire; upper-case MUST is this repo's established signal
#     that a clause is a mandate rather than description.
#   - The unit scanned is one heading-delimited block: for
#     coding-standards.md, the text from a "##"/"###" heading to the next
#     heading of that level or higher; for the catalog, the text of one
#     "### Entry N — ..." section (which also covers its "Sub-requirements"
#     subsection and Testable-criterion table). Text before the first
#     matched heading is out of scope — a MUST inside catalog front-matter
#     prose (e.g. explaining the mechanism itself, not registering a
#     mandate) is not a per-entry standard and is not scanned.
#   - A block "has a check" when it contains any of: an `<!-- audit: -->`
#     hint, a backtick-quoted `grm-<skill>` reference, a `recipe.py`
#     reference, a "Testable criterion" table column (the catalog's
#     per-sub-requirement check column), or the literal word
#     "deterministic". False negatives are expected (a check described only
#     in prose with none of these tokens); false positives are cheap to
#     dismiss because the finding is WARN-tier and self-explains the gap.
_MUST_CLAUSE_RE = re.compile(r"\bMUST\b")
_CHECK_FOR_CHECKS_REFERENCE_RE = re.compile(
    r"<!--\s*audit:|`grm-[a-z0-9-]+|recipe\.py|Testable criterion|deterministic"
)


def _split_headed_blocks(text, heading_re):
    """Split text into (heading_text, block_text) pairs at each match of
    heading_re. Text before the first heading match is discarded — callers
    only care about content paired with a heading."""
    matches = list(heading_re.finditer(text))
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((m.group().strip(), text[start:end]))
    return blocks


def check_for_checks(root: str) -> list:
    """Release-gate meta-check for the standards doctrine (#440,
    docs/architecture-guidelines.md §Standards doctrine): a mandated
    ("MUST") standard with no paired deterministic-check reference in its
    own section is flagged, WARN-tier. Scans docs/coding-standards.md (by
    "##"/"###" section) and the required-feature catalog (by "### Entry"
    section). See the _MUST_CLAUSE_RE / _CHECK_FOR_CHECKS_REFERENCE_RE
    comment above for the detection heuristic and its documented
    limitations (upper-case MUST only; text outside a heading is out of
    scope)."""
    findings = []
    targets = [
        (os.path.join(root, "docs", "coding-standards.md"),
         re.compile(r"^#{2,3}\s+.+$", re.MULTILINE)),
        (os.path.join(root, ".claude", "skills", "grm-required-feature-catalog",
                       "required-feature-catalog.md"),
         re.compile(r"^###\s+Entry\s+.+$", re.MULTILINE)),
    ]
    for path, heading_re in targets:
        if not os.path.exists(path):
            continue
        rel_path = os.path.relpath(path, root)
        text = open(path, encoding="utf-8").read()
        for heading, block in _split_headed_blocks(text, heading_re):
            if not _MUST_CLAUSE_RE.search(block):
                continue
            if _CHECK_FOR_CHECKS_REFERENCE_RE.search(block):
                continue
            findings.append(
                f"{rel_path} {heading!r}: MUST-clause rule with no paired "
                f"deterministic-check reference (WARN — pair it with a "
                f"check, gate, and severity ramp per docs/architecture-"
                f"guidelines.md §Standards doctrine, or label it "
                f"aspirational)")
    return findings


# ── Check 5c: mirrored-script parity ─────────────────────────────────────
# Root ↔ claude-code mirrors are matched by identical relative path under
# `.claude/skills/` and `.claude/hooks/` (both flavors use that same layout).
# Root ↔ copilot / root ↔ codex mirrors are matched by basename against those
# flavors' flat `scripts/` directory (their shipped layout has no per-skill
# subdirectories). Only *.py and *.sh are in scope — the same script-file
# universe an earlier flavor-parity hotfix's evidence section covers.
_MIRRORED_SCRIPT_EXTS = (".py", ".sh")

# Each entry: (flavor, relative-or-basename) pairs already known to differ
# intentionally, or tracked pre-existing drift not yet reconciled. An entry
# here does NOT mean "never fix" — it means "content differs for a documented
# reason" (see the comment above each group). Add new entries only after
# verifying the delta by reading the diff; never as a blanket bypass.
MIRRORED_SCRIPT_ALLOW = frozenset({
    # ── copilot/codex comment-only rewording (systemic, established convention) ──
    # copilot/ and codex/ ship without docs/grimoire/ (framework-internal tier,
    # the Bulkhead) so a script's comments/docstrings that reference a
    # root-only docs/grimoire/design/*.md path are reworded there to "lives in
    # the upstream Grimoire repository (framework-internal)" instead of a
    # dangling relative path. Pre-existing across many scripts; tracked here
    # rather than fixed en masse across the repo (out of this check's own named
    # scope of doc_assurance.py / release_plan.py / build_distributables.py).
    ("copilot", "release_plan.py"),
    ("copilot", "issue_tracker.py"),
    ("copilot", "github_pr.py"),
    ("copilot", "env_probe.py"),
    ("copilot", "component_registry.py"),
    ("codex", "issue_tracker.py"),
    ("claude-code", "hard_reset.py"),
    ("claude-code", "verify_isolation.py"),
    ("claude-code", "issue_tracker.py"),
    ("claude-code", "issue_tracker_switch.py"),
    ("claude-code", "github_pr.py"),
    ("claude-code", "env_probe.py"),
    ("claude-code", "qa_select.py"),
    ("claude-code", "install_doctor.py"),
    ("claude-code", "component_registry.py"),
    ("claude-code", "sync-from-source.sh"),
    ("claude-code", "autonomy-allow.sh"),
    ("claude-code", "bundled-sync-guard.sh"),
    ("claude-code", "protected-branch-guard.sh"),
    ("claude-code", "stealth-guard.sh"),
    ("claude-code", "worktree-guard.sh"),

    # ── copilot-only flat scripts/ tree: comment-rewording + legitimate
    # layout-adaptation deltas (copilot ships flat scripts/, not nested
    # per-skill dirs, so a script's own path-construction differs correctly).
    # No functional gap beyond the rewording/layout-adaptation itself.
    ("copilot", "sync_deps.py"),
    ("copilot", "vendor_verify.py"),
    ("copilot", "run_metadata.py"),
    ("copilot", "telemetry_entry.py"),         # + correct flat-layout path adaptation (#345/#346)
    ("copilot", "cost_budget.py"),
    ("copilot", "vendor_migrate.py"),          # + correct flat-layout sys.path adaptation
    ("copilot", "sync_deps_engine.py"),
    ("copilot", "dependency_channel_conformance.py"),
    ("copilot", "project_status.py"),
    ("copilot", "grm_namespacing.py"),         # deliberately distinct copilot-specific engine
    ("copilot", "pm_overlap.py"),
    ("copilot", "noir_loop_state.py"),
    ("copilot", "migrate_roadmap_issues.py"),  # correct flat-layout path adaptation
    ("codex", "config_validate.py"),           # different tool entirely (codex-native config surfaces)

    # ── copilot: confirmed REAL functional drift, out of this check's named
    # scope (doc_assurance.py / release_plan.py / build_distributables.py only).
    # Classified as tracked drift; filed as follow-up, not fixed here:
    #   - issue_tracker_switch.py: points at a stale recovery command
    #     (`workflow-bootstrap --restore` vs copilot's `/install-doctor --repair`).
    #   - qa_select.py: ledger/release-planning path inconsistency
    #     (docs/grimoire/... vs docs/...) not matching copilot's real tree.
    #   - recipe.py: copilot is one INTERFACE_VERSION behind (v4 vs v5) and
    #     lacks the `release` target root has.
    #   - sync-from-upstream.sh: copilot lacks root's additive-only-conflict
    #     auto-resolution and several exclusion-list entries.
    ("copilot", "issue_tracker_switch.py"),
    ("copilot", "qa_select.py"),
    ("copilot", "recipe.py"),
    ("copilot", "sync-from-upstream.sh"),

    # ── #363 (grm-cost-budget path-name fix + #351 cross-skill __all__
    # marking): scoped to root/claude-code only. copilot/scripts/cost_budget.py
    # has a flat layout with no sys.path insert (unaffected by the directory-
    # name bug) and was explicitly out of scope for this item; its
    # parse_usage.py sibling was left without the new __all__ marking rather
    # than touching an unrelated file.
    ("copilot", "parse_usage.py"),

    # ── #436 (v3.99 R8 Pass 1, app-telemetry catalog Entry 9): claude-code/
    # is the canonical flavor per CLAUDE.md and gained the Entry 9 dispatch
    # wiring + the 8->9 entry-count self-test updates; root has NOT been
    # synced onto this item ("adopt into root when wanted" per CLAUDE.md
    # §Source of truth — a deliberate, deferred choice, not an oversight).
    # root's required-feature-catalog.md still has 8 entries and root lacks
    # app_telemetry_conformance.py / app_telemetry_schema.py entirely, so
    # copying these two files over would break root's own self-test rather
    # than fix a real gap.
    ("claude-code", "catalog_conformance.py"),
    ("claude-code", "catalog_filing.py"),
})


def _enumerate_skill_scripts(flavor_root):
    """{relative-path-under-.claude/skills-or-.claude/hooks-or-.claude/mcp-servers:
    abs-path} for a flavor root. mcp-servers/** was added after root's
    grimoire-status/grimoire-release servers had drifted onto stale bare-name
    skill paths and this enumeration never caught it since it only walked
    skills/ and hooks/."""
    out = {}
    for sub in ("skills", "hooks", "mcp-servers"):
        base = os.path.join(flavor_root, ".claude", sub)
        if not os.path.isdir(base):
            continue
        for ext in _MIRRORED_SCRIPT_EXTS:
            for p in glob.glob(f"{base}/**/*{ext}", recursive=True):
                out[os.path.join(sub, os.path.relpath(p, base))] = p
    return out


def _enumerate_flat_scripts(scripts_dir):
    """{basename: abs-path} for a flavor's flat scripts/ directory (copilot/codex)."""
    out = {}
    if not os.path.isdir(scripts_dir):
        return out
    for ext in _MIRRORED_SCRIPT_EXTS:
        for p in glob.glob(f"{scripts_dir}/*{ext}"):
            out[os.path.basename(p)] = p
    return out


def check_mirrored_script_parity(root: str, _allow_set: set | None = None) -> list:
    """Enumerate every script (*.py, *.sh) that ships in both the root tree
    and a flavor tree at an equivalent path, and fail on undocumented content
    drift (the systemic sibling of the one-off flavor-parity must-match
    list). Two matching strategies, depending on how a flavor lays out its
    scripts:

      - root ↔ claude-code: matched by IDENTICAL relative path under
        `.claude/skills/**` or `.claude/hooks/**` (claude-code mirrors root's
        per-skill directory layout exactly).
      - root ↔ copilot / root ↔ codex: matched by BASENAME against that
        flavor's flat `scripts/` directory (copilot/codex ship scripts
        flattened, not nested per-skill).

    A pair that differs is a finding UNLESS (flavor, key) is in the allow-list
    (`MIRRORED_SCRIPT_ALLOW`) — the same intentional-delta pattern
    `check_flavor_parity` uses for docs. No hand-picked file list drives the
    enumeration itself; only the pass/fail exemption is declarative.

    Dead-allowlist-entry detection: every (flavor, key) in
    `_allow_set` that matches NO enumerated pair this run (the basename/relp
    was renamed, removed, or never existed in the target flavor) is itself a
    finding — an allow-list can't silently accumulate stale entries once a
    script is deleted or renamed on the side it names.
    """
    if _allow_set is None:
        _allow_set = MIRRORED_SCRIPT_ALLOW
    findings = []
    consulted = set()  # (flavor, key) allow-list entries matched to a real enumerated pair

    root_skill_scripts = _enumerate_skill_scripts(root)

    # ── root ↔ claude-code (identical relative path) ────────────────────
    cc_root = os.path.join(root, "claude-code")
    if os.path.isdir(cc_root):
        cc_scripts = _enumerate_skill_scripts(cc_root)
        for relp, root_path in sorted(root_skill_scripts.items()):
            if relp not in cc_scripts:
                continue  # presence gaps are flavor-parity's concern, not this check's
            bn_key = ("claude-code", os.path.basename(relp))
            relp_key = ("claude-code", relp)
            if bn_key in _allow_set:
                consulted.add(bn_key)
            if relp_key in _allow_set:
                consulted.add(relp_key)
            if bn_key in _allow_set or relp_key in _allow_set:
                continue
            if open(root_path, errors="ignore").read() != open(cc_scripts[relp], errors="ignore").read():
                findings.append(
                    f"script differs root vs claude-code (undocumented drift): {relp}")

    # ── root ↔ copilot / root ↔ codex (basename match against flat scripts/) ──
    for flavor, scripts_dir in (("copilot", os.path.join(root, "copilot", "scripts")),
                                 ("codex", os.path.join(root, "codex", "scripts"))):
        flavor_scripts = _enumerate_flat_scripts(scripts_dir)
        if not flavor_scripts:
            continue
        for bn, flavor_path in sorted(flavor_scripts.items()):
            match = None
            for relp, root_path in root_skill_scripts.items():
                if os.path.basename(relp) == bn:
                    match = root_path
                    break
            if match is None:
                continue  # no root skill script of this name — not a mirror pair
            key = (flavor, bn)
            if key in _allow_set:
                consulted.add(key)
                continue
            if open(match, errors="ignore").read() != open(flavor_path, errors="ignore").read():
                findings.append(
                    f"script differs root vs {flavor} (undocumented drift): {bn}")

    # ── dead-allowlist-entry detection ───────────────────────────────────
    # Only meaningful when at least one flavor tree is present this run —
    # a bare root checkout (no claude-code/copilot/codex dirs) would trivially
    # flag every entry as "unmatched" for a reason unrelated to staleness.
    any_flavor_present = os.path.isdir(cc_root) or any(
        os.path.isdir(os.path.join(root, f, "scripts")) for f in ("copilot", "codex"))
    if any_flavor_present:
        for flavor, key in sorted(_allow_set - consulted):
            findings.append(
                f"dead allowlist entry: ({flavor!r}, {key!r}) matches no "
                f"enumerated mirrored-script pair — prune it or verify the "
                f"target file's real name/location")

    return findings


# ── Check 5d: ported-pair presence ───────────────────────────────────────
# codex has no PreToolUse-hook system like Claude Code, so the workflow's
# guard rules are deliberately REIMPLEMENTED (not mirrored byte-for-byte) as
# `codex/.codex/hooks/*.py` — a different language, a different runtime
# shape. Content drift between a root .sh guard and its codex .py port is
# NOT machine-checkable (see doc-assurance-design.md §Mirrored-script parity
# limitation); this check only verifies each documented PORTED_PAIR's codex
# side still exists — a *presence* regression (a root hook whose codex port
# existed and then vanished) would mean the guard silently stopped being
# enforced in that flavor.
#
# Each tuple is (root .claude/hooks/ basename, codex .codex/hooks/ basename).
# NOT every root hook has (or is meant to have) a codex port — worktree-guard,
# stealth-guard, autonomy-allow, and bundled-sync-guard are documented,
# intentional non-ports (codex/git-hooks/README.md §Not enforced here); they
# are deliberately absent from this table so their absence is never a finding.
PORTED_PAIRS = (
    ("protected-branch-guard.sh", "protected-branch-guard.py"),
    ("push-guard.sh", "push-guard.py"),
    ("release-plan-guard.sh", "release-plan-guard.py"),
    ("worktree-brief.sh", "session-start.py"),
)


def check_ported_pair_presence(root: str) -> list:
    """For each documented (root-hook, codex-port) pair in PORTED_PAIRS,
    verify both sides still exist. A root hook whose codex port has
    disappeared (or vice versa) is a presence-regression finding — content
    drift itself is out of scope (see module docstring)."""
    findings = []
    root_hooks_dir = os.path.join(root, ".claude", "hooks")
    codex_hooks_dir = os.path.join(root, "codex", ".codex", "hooks")
    if not os.path.isdir(codex_hooks_dir):
        return findings  # no codex flavor present (consumer-mode or non-monorepo)
    for root_name, codex_name in PORTED_PAIRS:
        root_path = os.path.join(root_hooks_dir, root_name)
        codex_path = os.path.join(codex_hooks_dir, codex_name)
        root_exists = os.path.isfile(root_path)
        codex_exists = os.path.isfile(codex_path)
        if root_exists and not codex_exists:
            findings.append(
                f"ported-pair regression: .claude/hooks/{root_name} has no "
                f"codex/.codex/hooks/{codex_name} port (previously tracked)")
        elif codex_exists and not root_exists:
            findings.append(
                f"ported-pair regression: codex/.codex/hooks/{codex_name} "
                f"has no .claude/hooks/{root_name} counterpart (previously tracked)")
    return findings


# ── Check: orchestrate-band presence (#368) ──────────────────────────────
# The "orchestrate" band (model-effort-profiles-design.md) must be declared
# and fully wired in every model-effort-profiles.json a flavor actually
# ships. Multi-flavor by nature (root + claude-code); copilot has no model
# knob and carries no such file, so the check only evaluates candidates that
# exist rather than failing on copilot's absence.
_ORCHESTRATE_BAND_CANDIDATES = (
    ".claude/model-effort-profiles.json",
    "claude-code/.claude/model-effort-profiles.json",
)


def check_orchestrate_band_present(root: str) -> list:
    """For each present model-effort-profiles.json, assert the "orchestrate"
    band is declared in the top-level `bands` array AND every entry under
    `profiles` carries an `orchestrate` key whose value is an object with
    non-empty `model` and `effort` string fields. Scoped to files that
    exist — a flavor without a model-effort-profiles.json (copilot) is never
    flagged for lacking one."""
    findings = []
    for rel_path in _ORCHESTRATE_BAND_CANDIDATES:
        p = os.path.join(root, rel_path)
        if not os.path.isfile(p):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except (ValueError, OSError) as e:
            findings.append(f"{rel_path}: unreadable or malformed JSON ({e})")
            continue
        bands = data.get("bands", [])
        if "orchestrate" not in bands:
            findings.append(f"{rel_path}: 'orchestrate' missing from top-level bands array")
        profiles = data.get("profiles", {})
        for name in sorted(profiles):
            entry = profiles[name].get("orchestrate")
            if not isinstance(entry, dict):
                findings.append(f"{rel_path}: profile '{name}' missing 'orchestrate' entry")
                continue
            model = entry.get("model")
            effort = entry.get("effort")
            if not (isinstance(model, str) and model.strip()):
                findings.append(f"{rel_path}: profile '{name}'.orchestrate missing non-empty 'model'")
            if not (isinstance(effort, str) and effort.strip()):
                findings.append(f"{rel_path}: profile '{name}'.orchestrate missing non-empty 'effort'")
    return findings


# ── Check 5e: server.py self-test parity (#364) ──────────────────────────
# Paths are discovered by glob (5 servers x 3 flavors), never hardcoded, so a
# newly added MCP server or flavor is picked up automatically without a
# doc-assurance edit.
_SERVER_SELFTEST_GLOBS = (
    os.path.join(".claude", "mcp-servers", "*", "server.py"),
    os.path.join("claude-code", ".claude", "mcp-servers", "*", "server.py"),
    os.path.join("copilot", "mcp-servers", "*", "server.py"),
)


def check_server_selftest_parity(root: str) -> list:
    """Run `python3 <path>/server.py --self-test` for every MCP server.py
    copy in the framework monorepo and fail if any copy's self-test does not
    exit 0. Each invocation is its own short-lived subprocess (bounded by a
    timeout so a hang can't stall doc-assurance) — fast and hermetic, no
    shared state between copies."""
    findings = []
    paths = sorted({
        p for pat in _SERVER_SELFTEST_GLOBS
        for p in glob.glob(os.path.join(root, pat))
    })
    for path in paths:
        relp = os.path.relpath(path, root)
        try:
            proc = subprocess.run(
                [sys.executable, path, "--self-test"],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            findings.append(f"server self-test timed out (30s): {relp}")
            continue
        except OSError as exc:
            findings.append(f"server self-test failed to launch: {relp} ({exc})")
            continue
        if proc.returncode != 0:
            tail_lines = (proc.stdout + proc.stderr).strip().splitlines()
            tail = tail_lines[-1] if tail_lines else "(no output)"
            findings.append(
                f"server self-test failed (exit {proc.returncode}): {relp} — {tail}")
    return findings


# ── Check 6: skill / always-loaded size budget ───────────────────────────
def check_skill_budget(root: str) -> list:
    """Flag any shipped flavor's SKILL.md over SKILL_BUDGET, plus root's CLAUDE.md.

    Scans every entry in _SHIPPED_FLAVORS (root + claude-code + codex +
    copilot), not just root — matching the pattern used by
    check_shipped_pointers and friends. #399: before this fix the check only
    ever globbed root's own `.claude/skills`, so a skill that bloated past
    budget in the canonical `claude-code/` copy (the tree that actually ships)
    went undetected by this pre-merge gate — only `footprint.py --root
    claude-code` surfaced it. root's own SKILL.md count is unaffected
    (flavor="" resolves to root, so this is additive, not a behavior change
    for consumer-mode single-flavor installs where the other flavor dirs
    don't exist).

    CLAUDE_BUDGET stays root-only intentionally: each flavor's CLAUDE.md is a
    separate, independently-owned document (not a copy) and its size is not
    part of this issue's scope.
    """
    findings = []
    for flavor in _SHIPPED_FLAVORS:
        flavor_root = os.path.join(root, flavor) if flavor else root
        for p in sorted(glob.glob(f"{flavor_root}/.claude/skills/*/SKILL.md")):
            n = os.path.getsize(p)
            if n > SKILL_BUDGET:
                findings.append(f"{rel(root,p)}: {n} bytes > {SKILL_BUDGET} budget "
                                f"(split via split_skill.py: lean head + reference.md)")
    cm = f"{root}/CLAUDE.md"
    if os.path.exists(cm) and os.path.getsize(cm) > CLAUDE_BUDGET:
        findings.append(f"CLAUDE.md: {os.path.getsize(cm)} bytes > {CLAUDE_BUDGET} budget")
    return findings


# ── Check 7: relative links ─────────────────────────────────────────────
# Regex: markdown link targets (after strip_code)
_LINK_TARGET_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
# Regex: backtick-wrapped docs/ refs (bare-prose detection)
_BARE_PROSE_RE = re.compile(r"`(docs/[^`]+\.md)`")


def _heading_slug(heading_text):
    """Convert a heading to a GitHub-style anchor slug."""
    # strip leading # chars and spaces
    text = re.sub(r"^#+\s*", "", heading_text).strip()
    text = text.lower()
    # keep alphanumerics, hyphens, and spaces; remove other special chars
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    return text


def _extract_headings(content):
    """Return set of anchor slugs from all headings in content."""
    slugs = set()
    for line in content.splitlines():
        if line.startswith("#"):
            slugs.add(_heading_slug(line))
    return slugs


def check_relative_links(docs_dir: str, repo_root: str) -> list:
    """Check 7: relative-links enforcement (repo-wide + docs-scoped).

    A. Absolute internal path rejection (repo-wide): any markdown link whose
       target starts with '/' or the repo's own GitHub URL.
    B. Broken anchor detection (docs-scoped): anchors in relative links that
       don't match any heading slug in the target file.
    C. Bare-prose doc ref detection (docs-scoped): backtick refs like
       `docs/path/file.md` outside links/code that refer to existing files.
    """
    findings = []

    # A. Absolute internal links — scan all *.md under repo_root
    all_md = [p for p in glob.glob(f"{repo_root}/**/*.md", recursive=True)
              if "/.git/" not in p and "/.scaffold-base/" not in p]

    for p in all_md:
        raw = open(p).read()
        stripped = _strip_code(raw)
        for m in _LINK_TARGET_RE.finditer(stripped):
            t = m.group(1).strip()
            if t.startswith("/"):
                findings.append(
                    f"absolute internal link: {t} in {rel(repo_root, p)}"
                )
            elif t.startswith(OWN_REPO_URL):
                findings.append(
                    f"absolute internal link: {t} in {rel(repo_root, p)}"
                )

    # B + C are docs-scoped
    docs_md = [p for p in glob.glob(f"{docs_dir}/**/*.md", recursive=True)
               if "/.git/" not in p]

    for p in docs_md:
        raw = open(p).read()
        stripped = _strip_code(raw)
        base = os.path.dirname(p)

        # B. Broken anchor detection
        for m in _LINK_TARGET_RE.finditer(stripped):
            t = m.group(1).strip()
            if t.startswith(("http://", "https://", "mailto:")):
                continue
            if "#" not in t:
                continue
            path_part, anchor = t.split("#", 1)
            if not anchor:
                continue
            if path_part:
                target_file = os.path.normpath(os.path.join(base, path_part))
            else:
                target_file = p  # same-file anchor
            if not os.path.exists(target_file):
                continue  # dead link handled by check_links
            try:
                target_content = open(target_file).read()
            except Exception:
                continue
            slugs = _extract_headings(target_content)
            if anchor not in slugs:
                findings.append(
                    f"broken anchor: #{anchor} in {rel(repo_root, target_file)} "
                    f"(referenced from {rel(repo_root, p)})"
                )

        # C. Bare-prose doc refs
        # Strip fenced code blocks; then look for `docs/...` outside any link.
        # We already have stripped (inline code removed); use the fence-stripped
        # version without inline-code removal to find backtick refs accurately.
        fence_stripped = FENCE_RE.sub("", raw)
        # Remove link targets so we don't match `docs/x.md` inside [text](`docs/x.md`)
        without_links = _LINK_TARGET_RE.sub("", fence_stripped)
        # Also strip inline code that is part of a link: `[`code`](target)` — already stripped
        for m in _BARE_PROSE_RE.finditer(without_links):
            ref = m.group(1)
            # Check if the referenced file actually exists under repo_root
            candidate = os.path.normpath(os.path.join(repo_root, ref))
            if os.path.exists(candidate):
                findings.append(
                    f"bare-prose doc ref: `{ref}` in {rel(repo_root, p)} "
                    f"— use a relative markdown link instead"
                )

    return findings


# ── Check 8: hierarchy ──────────────────────────────────────────────────

def _is_hierarchy_exempt(basename):
    """Return True if filename matches any exemption glob."""
    import fnmatch
    for pattern in _HIERARCHY_EXEMPT_GLOBS:
        if fnmatch.fnmatch(basename, pattern):
            return True
    return False


def _build_doc_graph(docs_dir):
    """Walk docs/**/*.md and build the reachability link graph.

    Shared by check_hierarchy and the documentation-portal generator (the
    portal-design.md rule: "reuse check_hierarchy's graph — don't re-parse
    from scratch"). Returns (all_docs, reachable, edges):
      all_docs  — normalized absolute paths of every docs/**/*.md file.
      reachable — the subset reachable via relative .md links, BFS from
                  docs/README.md (root always included, even if absent).
      edges     — dict: doc path -> sorted list of resolved target doc paths
                  it links to (only targets that exist on disk).
    """
    root_readme = os.path.normpath(os.path.join(docs_dir, "README.md"))

    all_docs = set()
    for p in glob.glob(f"{docs_dir}/**/*.md", recursive=True):
        if "/.git/" not in p:
            all_docs.add(os.path.normpath(p))

    edges = {}
    reachable = set()
    queue = [root_readme]
    reachable.add(root_readme)
    visited_for_edges = set()
    while queue:
        current = queue.pop()
        if current in visited_for_edges:
            continue
        visited_for_edges.add(current)
        try:
            content = open(current).read()
        except Exception:
            continue
        stripped = _strip_code(content)
        base = os.path.dirname(current)
        targets = []
        for m in _LINK_TARGET_RE.finditer(stripped):
            t = m.group(1).strip()
            if t.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path_part = t.split("#", 1)[0].split("?", 1)[0]
            if not path_part or not path_part.endswith(".md"):
                continue
            target = os.path.normpath(os.path.join(base, path_part))
            if os.path.exists(target):
                targets.append(target)
                if target not in reachable:
                    reachable.add(target)
                    queue.append(target)
        edges[current] = sorted(set(targets))

    return all_docs, reachable, edges


def check_hierarchy(docs_dir: str) -> list:
    """Check 8: hierarchy reachability, breadcrumb presence, per-tier index.

    1. Reachability from docs/README.md root via relative links.
    2. Breadcrumb (blockquote with relative link to README.md) in first 10 lines.
    3. Per-tier index: each immediate subdirectory of docs/ has a README.md.
    """
    findings = []

    root_readme = os.path.join(docs_dir, "README.md")
    if not os.path.exists(root_readme):
        findings.append("run --write-map to generate docs root map")
        return findings

    # Check markers presence (non-fatal advisory)
    root_content = open(root_readme).read()
    if "<!-- docs-map:begin -->" not in root_content:
        findings.append("run --write-map to generate docs root map")
        # Continue anyway — hierarchy can still be checked

    # Collect all docs/**/*.md files + reachability graph (shared helper).
    all_docs, reachable, _edges = _build_doc_graph(docs_dir)

    # Emit orphan findings for unreachable non-exempt docs
    for p in sorted(all_docs - reachable):
        basename = os.path.basename(p)
        if _is_hierarchy_exempt(basename):
            continue
        # Don't report README.md files themselves as orphans — they are index pages
        if basename == "README.md":
            continue
        # Compute relative path from docs_dir parent for display
        try:
            display = "docs/" + os.path.relpath(p, docs_dir)
        except ValueError:
            display = p
        findings.append(f"hierarchy orphan: {display}")

    # 2. Breadcrumb presence on non-root, non-index, non-exempt docs/**/*.md
    # Pattern: first 10 non-blank lines contain a blockquote with a link to *README.md
    _BREADCRUMB_RE = re.compile(r"^>\s.*\[.*\]\([^)]*README\.md\)", re.M)
    for p in sorted(all_docs):
        basename = os.path.basename(p)
        if basename == "README.md":
            continue  # index pages / root exempt
        if _is_hierarchy_exempt(basename):
            continue
        try:
            lines = open(p).readlines()
        except Exception:
            continue
        # Check first 10 non-blank lines
        non_blank = [l.rstrip() for l in lines if l.strip()][:10]
        head = "\n".join(non_blank)
        if not _BREADCRUMB_RE.search(head):
            try:
                display = "docs/" + os.path.relpath(p, docs_dir)
            except ValueError:
                display = p
            findings.append(f"missing breadcrumb: {display}")

    # 3. Per-tier index: each immediate subdir of docs/ must have README.md
    try:
        for entry in sorted(os.listdir(docs_dir)):
            subdir = os.path.join(docs_dir, entry)
            if not os.path.isdir(subdir):
                continue
            # Check if any .md file exists in this subdir
            has_md = bool(glob.glob(f"{subdir}/*.md") or glob.glob(f"{subdir}/**/*.md", recursive=True))
            if has_md and not os.path.exists(os.path.join(subdir, "README.md")):
                findings.append(f"missing tier index: docs/{entry}/")
    except Exception:
        pass

    return findings


# ── Documentation portal (docs/documentation.html) ──────────────────────
# Generated, never hand-edited — see docs-portal-design.md. Reuses
# _build_doc_graph (the same reachability graph check_hierarchy builds) so
# the portal is derived from a single parse pass, not a second traversal.

PORTAL_REL_PATH = "docs/documentation.html"

# Portal-generation constants.
_PORTAL_EXCERPT_WORDS = 40   # search-index excerpt length (first-N-words)
_PORTAL_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_PORTAL_UL_RE = re.compile(r"^[ \t]*[-*][ \t]+(.*)$")
_PORTAL_OL_RE = re.compile(r"^[ \t]*\d+\.[ \t]+(.*)$")
_PORTAL_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_PORTAL_FENCE_RE = re.compile(r"^```")
_PORTAL_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_PORTAL_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_PORTAL_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_PORTAL_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def _portal_slug(docs_dir, path):
    """Stable per-page anchor id derived from the doc's docs/-relative path."""
    rp = os.path.relpath(path, docs_dir).replace(os.sep, "/")
    return "doc-" + re.sub(r"[^a-zA-Z0-9]+", "-", rp).strip("-").lower()


def _portal_title(path, content):
    """First heading in the doc, else the basename, as the page/nav title."""
    for line in content.splitlines():
        m = _PORTAL_HEADING_RE.match(line.strip())
        if m:
            return _html_escape(m.group(2).strip())
    return _html_escape(os.path.basename(path))


def _html_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _portal_plain_excerpt(content, n_words=_PORTAL_EXCERPT_WORDS):
    """Plain-text excerpt (first N words) for the search index, headings/markup stripped."""
    body = _strip_code(content)
    body = re.sub(r"^#{1,6}\s+", "", body, flags=re.M)
    body = re.sub(r"[>*_`#|-]", " ", body)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    words = body.split()
    return " ".join(words[:n_words])


def _portal_inline_md(text, link_resolver):
    """Render inline markdown spans: links, bold, italic, inline code. Escapes HTML first."""
    text = _html_escape(text)

    def _link_sub(m):
        label, target = m.group(1), m.group(2).strip()
        href = link_resolver(target)
        return f'<a href="{_html_escape(href)}">{label}</a>'

    text = _PORTAL_LINK_RE.sub(_link_sub, text)
    text = _PORTAL_INLINE_CODE_RE.sub(r"<code>\1</code>", text)
    text = _PORTAL_BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _PORTAL_ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


def _md_to_html(content, link_resolver):
    """Minimal stdlib markdown→HTML converter: headers, lists, tables, code
    fences, links, bold/italic/inline-code. Covers the subset Grimoire docs
    actually use (per docs-portal-design.md — no exotic markdown extensions).

    link_resolver(target) -> href is called for every markdown link target so
    the caller can rewrite doc-relative .md links into in-page anchors.
    """
    out = []
    lines = content.splitlines()
    i, n = 0, len(lines)
    in_ul = in_ol = in_table = in_p = False

    def _close_all():
        nonlocal in_ul, in_ol, in_table, in_p
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False
        if in_table:
            out.append("</table>")
            in_table = False
        if in_p:
            out.append("</p>")
            in_p = False

    while i < n:
        line = lines[i]

        # Fenced code block: copy verbatim (escaped) until closing fence.
        if _PORTAL_FENCE_RE.match(line.strip()):
            _close_all()
            out.append("<pre><code>")
            i += 1
            while i < n and not _PORTAL_FENCE_RE.match(lines[i].strip()):
                out.append(_html_escape(lines[i]))
                i += 1
            out.append("</code></pre>")
            i += 1  # skip closing fence
            continue

        stripped = line.strip()

        if not stripped:
            _close_all()
            i += 1
            continue

        m = _PORTAL_HEADING_RE.match(stripped)
        if m:
            _close_all()
            level = len(m.group(1))
            out.append(f"<h{level}>{_portal_inline_md(m.group(2).strip(), link_resolver)}</h{level}>")
            i += 1
            continue

        # Table: a header row followed by a separator row (|---|---|).
        if (not in_table and "|" in stripped and i + 1 < n
                and _PORTAL_TABLE_SEP_RE.match(lines[i + 1].strip())):
            _close_all()
            headers = [c.strip() for c in stripped.strip("|").split("|")]
            out.append("<table><thead><tr>")
            out.extend(f"<th>{_portal_inline_md(h, link_resolver)}</th>" for h in headers)
            out.append("</tr></thead><tbody>")
            i += 2  # header + separator
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(
                    f"<td>{_portal_inline_md(c, link_resolver)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        m = _PORTAL_UL_RE.match(line)
        if m:
            if not in_ul:
                _close_all()
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_portal_inline_md(m.group(1), link_resolver)}</li>")
            i += 1
            continue

        m = _PORTAL_OL_RE.match(line)
        if m:
            if not in_ol:
                _close_all()
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_portal_inline_md(m.group(1), link_resolver)}</li>")
            i += 1
            continue

        if stripped.startswith(">"):
            _close_all()
            quote = stripped.lstrip(">").strip()
            out.append(f"<blockquote>{_portal_inline_md(quote, link_resolver)}</blockquote>")
            i += 1
            continue

        # Plain paragraph text (accumulate consecutive lines).
        if not in_p:
            _close_all()
            out.append("<p>")
            in_p = True
        else:
            out.append("<br>")
        out.append(_portal_inline_md(stripped, link_resolver))
        i += 1

    _close_all()
    return "\n".join(out)


def _build_portal_nav_tree(docs_dir, all_docs):
    """Group docs/**/*.md paths by tier (immediate subdir under docs/), like
    _build_nested_map, so the portal nav mirrors the docs-map grouping."""
    top_level = []
    groups = {}
    for p in sorted(all_docs):
        rp = os.path.relpath(p, docs_dir).replace(os.sep, "/")
        if "/" not in rp:
            top_level.append(p)
        else:
            subdir = rp.split("/")[0]
            groups.setdefault(subdir, []).append(p)
    return top_level, groups


def build_portal(root: str) -> str:
    """Generate the full docs/documentation.html content (string).

    Deterministic: sorted iteration only, no timestamps or other
    nondeterministic content — same input docs ⇒ byte-identical output.
    """
    docs_dir = os.path.join(root, "docs")
    all_docs, _reachable, _edges = _build_doc_graph(docs_dir)

    # Pre-read every doc once; derive title/slug/href from that single read.
    docs_info = {}  # path -> {content, title, slug, rel}
    for p in sorted(all_docs):
        try:
            content = open(p, encoding="utf-8").read()
        except Exception:
            content = ""
        rp = os.path.relpath(p, docs_dir).replace(os.sep, "/")
        docs_info[p] = {
            "content": content,
            "title": _portal_title(p, content),
            "slug": _portal_slug(docs_dir, p),
            "rel": rp,
        }

    def _make_link_resolver(current_path):
        base = os.path.dirname(current_path)

        def _resolve(target):
            if target.startswith(("http://", "https://", "mailto:")):
                return target
            if target.startswith("#"):
                return target
            path_part, _, anchor = target.partition("#")
            if not path_part:
                return "#" + (anchor or "")
            # Every doc is inlined as one flat page at docs/documentation.html,
            # so any relative target must be re-based from the *portal's*
            # location (docs_dir), not from current_path's original directory
            # — otherwise a link like docs/design/README.md's "../../.claude/…"
            # (correct relative to docs/design/) would resolve one level too
            # far up once inlined at docs/documentation.html.
            abs_target = os.path.normpath(os.path.join(base, path_part))
            if path_part.endswith(".md") and abs_target in docs_info:
                return "#" + docs_info[abs_target]["slug"]
            if os.path.exists(abs_target):
                rebased = os.path.relpath(abs_target, docs_dir).replace(os.sep, "/")
                return rebased + (("#" + anchor) if anchor else "")
            return target  # unresolved — leave original (dead-link check catches it)

        return _resolve

    # ── Nav tree (sorted, mirrors _build_nested_map's grouping) ──────────
    top_level, groups = _build_portal_nav_tree(docs_dir, all_docs)
    nav_parts = ['<nav id="portal-nav">', '<ul class="portal-tree">']
    if top_level:
        nav_parts.append('<li class="portal-tier"><span class="portal-tier-label">docs/</span><ul>')
        for p in sorted(top_level, key=lambda p: docs_info[p]["rel"]):
            info = docs_info[p]
            nav_parts.append(f'<li><a href="#{info["slug"]}">{info["title"]}</a></li>')
        nav_parts.append("</ul></li>")
    for subdir in sorted(groups.keys()):
        nav_parts.append(f'<li class="portal-tier"><span class="portal-tier-label">{_html_escape(subdir)}/</span><ul>')
        for p in sorted(groups[subdir], key=lambda p: docs_info[p]["rel"]):
            info = docs_info[p]
            nav_parts.append(f'<li><a href="#{info["slug"]}">{info["title"]}</a></li>')
        nav_parts.append("</ul></li>")
    nav_parts.append("</ul></nav>")
    nav_html = "\n".join(nav_parts)

    # ── Content (every doc rendered inline, sorted by relative path) ─────
    content_parts = ['<main id="portal-content">']
    search_index = []
    for p in sorted(all_docs, key=lambda p: docs_info[p]["rel"]):
        info = docs_info[p]
        resolver = _make_link_resolver(p)
        body_html = _md_to_html(info["content"], resolver)
        content_parts.append(
            f'<article id="{info["slug"]}" class="portal-doc" data-path="{_html_escape(info["rel"])}">'
            f'<h1 class="portal-doc-title">{info["title"]}</h1>\n{body_html}\n</article>'
        )
        search_index.append({
            "title": info["title"],
            "path": info["rel"],
            "slug": info["slug"],
            "excerpt": _portal_plain_excerpt(info["content"]),
        })
    content_parts.append("</main>")
    content_html = "\n".join(content_parts)

    search_index_json = json.dumps(search_index, sort_keys=True, separators=(",", ":"))

    return _PORTAL_TEMPLATE.format(
        nav=nav_html,
        content=content_html,
        search_index=search_index_json,
    )


# Single-file template: inline CSS + vanilla JS, no CDN, no build step, works
# opened directly via file:// (offline). The leading HTML comment marks the
# file as generated (docs-portal-design.md: "generated, never hand-edited").
_PORTAL_TEMPLATE = """<!-- GENERATED FILE — do not hand-edit. Regenerate with:
     python3 .claude/skills/grm-doc-assurance/doc_assurance.py --write-portal
     Source of truth is the markdown under docs/; this HTML is a derived view. -->
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Grimoire documentation portal</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --portal-border: #d0d7de;
    --portal-bg: #ffffff;
    --portal-fg: #1f2328;
    --portal-muted: #57606a;
    --portal-accent: #0969da;
    --portal-nav-bg: #f6f8fa;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    color: var(--portal-fg);
    background: var(--portal-bg);
    display: flex;
    min-height: 100vh;
  }}
  #portal-sidebar {{
    width: 320px;
    flex: 0 0 320px;
    border-right: 1px solid var(--portal-border);
    background: var(--portal-nav-bg);
    padding: 1rem;
    overflow-y: auto;
    height: 100vh;
    position: sticky;
    top: 0;
  }}
  #portal-search {{
    width: 100%;
    padding: 0.5rem;
    margin-bottom: 1rem;
    border: 1px solid var(--portal-border);
    border-radius: 6px;
    font-size: 0.9rem;
  }}
  #portal-search-results {{
    margin-bottom: 1rem;
  }}
  #portal-search-results a {{
    display: block;
    padding: 0.25rem 0;
    font-size: 0.85rem;
  }}
  .portal-tree, .portal-tree ul {{
    list-style: none;
    margin: 0;
    padding-left: 0.75rem;
  }}
  .portal-tree {{ padding-left: 0; }}
  .portal-tier-label {{
    font-weight: 600;
    color: var(--portal-muted);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }}
  .portal-tree a {{
    color: var(--portal-accent);
    text-decoration: none;
    font-size: 0.9rem;
    line-height: 1.6;
  }}
  .portal-tree a:hover {{ text-decoration: underline; }}
  #portal-main-wrap {{
    flex: 1;
    padding: 2rem 3rem;
    max-width: 900px;
  }}
  .portal-doc {{
    border-bottom: 1px solid var(--portal-border);
    padding-bottom: 2rem;
    margin-bottom: 2rem;
  }}
  .portal-doc-title {{ margin-top: 0; }}
  .portal-doc.portal-hidden {{ display: none; }}
  pre {{
    background: var(--portal-nav-bg);
    padding: 0.75rem;
    overflow-x: auto;
    border-radius: 6px;
  }}
  code {{
    background: var(--portal-nav-bg);
    padding: 0.1rem 0.3rem;
    border-radius: 4px;
  }}
  pre code {{ background: none; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid var(--portal-border); padding: 0.4rem 0.6rem; text-align: left; }}
  blockquote {{
    border-left: 3px solid var(--portal-border);
    margin-left: 0;
    padding-left: 1rem;
    color: var(--portal-muted);
  }}
</style>
</head>
<body>
<div id="portal-sidebar">
  <input id="portal-search" type="text" placeholder="Search docs..." autocomplete="off">
  <div id="portal-search-results"></div>
  {nav}
</div>
<div id="portal-main-wrap">
{content}
</div>
<script id="portal-search-index" type="application/json">{search_index}</script>
<script>
(function () {{
  var indexEl = document.getElementById('portal-search-index');
  var searchIndex = JSON.parse(indexEl.textContent || '[]');
  var input = document.getElementById('portal-search');
  var resultsEl = document.getElementById('portal-search-results');
  var navEl = document.getElementById('portal-nav');
  var docs = Array.prototype.slice.call(document.querySelectorAll('.portal-doc'));

  function tokenize(s) {{
    return (s || '').toLowerCase().split(/\\s+/).filter(Boolean);
  }}

  function matches(entry, query) {{
    var haystack = (entry.title + ' ' + entry.path + ' ' + entry.excerpt).toLowerCase();
    return query.every(function (tok) {{ return haystack.indexOf(tok) !== -1; }});
  }}

  function render(query) {{
    if (!query.length) {{
      resultsEl.innerHTML = '';
      navEl.style.display = '';
      docs.forEach(function (d) {{ d.classList.remove('portal-hidden'); }});
      return;
    }}
    navEl.style.display = 'none';
    var hits = searchIndex.filter(function (e) {{ return matches(e, query); }});
    resultsEl.innerHTML = hits.map(function (e) {{
      return '<a href="#' + e.slug + '">' + e.title + '</a>';
    }}).join('');
    var hitSlugs = {{}};
    hits.forEach(function (e) {{ hitSlugs[e.slug] = true; }});
    docs.forEach(function (d) {{
      if (hitSlugs[d.id]) {{
        d.classList.remove('portal-hidden');
      }} else {{
        d.classList.add('portal-hidden');
      }}
    }});
  }}

  input.addEventListener('input', function () {{
    render(tokenize(input.value));
  }});
}})();
</script>
</body>
</html>
"""


def check_portal_stale(root: str) -> list:
    """Warn-only: docs/documentation.html is out of date vs. --write-portal output.

    Regenerates the portal content in-memory and compares byte-for-byte against
    what's on disk. Never auto-fixes (only --write-portal writes). Missing file
    is itself a staleness finding (nothing generated yet).
    """
    portal_path = os.path.join(root, PORTAL_REL_PATH)
    if not os.path.exists(portal_path):
        return [f"{PORTAL_REL_PATH} missing — run with --write-portal"]
    try:
        current = open(portal_path, encoding="utf-8").read()
    except Exception:
        return [f"{PORTAL_REL_PATH} unreadable — run with --write-portal"]
    fresh = build_portal(root)
    if current != fresh:
        return [f"{PORTAL_REL_PATH} is stale relative to docs/**/*.md — run with --write-portal to regenerate"]
    return []


# ── Check 9: lean-index ─────────────────────────────────────────────────
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")

def check_lean_index(root: str) -> list:
    """Index pages (README.md under docs/) must be ≤ 6 KB and link-dense (≥ 3 links).

    Size-cap exempt: aggregating root indexes listed in LEAN_INDEX_SIZE_EXEMPT.
    Link-density rule applies to all non-exempt index pages.
    """
    findings = []
    for p in sorted(glob.glob(f"{root}/docs/**/README.md", recursive=True)):
        rp = rel(root, p)
        content = open(p).read()
        size = os.path.getsize(p)
        links = _MD_LINK_RE.findall(content)

        if rp not in LEAN_INDEX_SIZE_EXEMPT and size > LEAN_INDEX_SIZE_CAP:
            findings.append(
                f"{rp}: index page {size} bytes > {LEAN_INDEX_SIZE_CAP} budget "
                f"(lean-index rule)"
            )
        if len(links) < LEAN_INDEX_MIN_LINKS:
            findings.append(
                f"{rp}: index page has only {len(links)} links "
                f"(minimum {LEAN_INDEX_MIN_LINKS}, lean-index rule)"
            )
    return findings


# ── Check 10: monolith-cap ───────────────────────────────────────────────
def check_monolith_cap(root: str) -> list:
    """Warn when a leaf doc (non-README.md under docs/) exceeds 20 KB.

    Warn-only — never a hard gate.  Files in MONOLITH_CAP_EXEMPT and
    release-planning archives are never flagged.
    """
    findings = []
    for p in sorted(glob.glob(f"{root}/docs/**/*.md", recursive=True)):
        if os.path.basename(p) == "README.md":
            continue
        rp = rel(root, p)
        if rp in MONOLITH_CAP_EXEMPT:
            continue
        if _MONOLITH_CAP_RELEASE_PLAN_RE.match(rp):
            continue
        size = os.path.getsize(p)
        if size > MONOLITH_CAP:
            findings.append(
                f"{rp}: {size} bytes > {MONOLITH_CAP} monolith cap "
                f"(consider splitting leaf + index)"
            )
    return findings


# ── Check 11: description-cap ─────────────────────────────────────────────
# Frontmatter is the leading YAML block delimited by '---' lines.
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---", re.S)
_DESCRIPTION_RE = re.compile(r"^description:[ \t]*(.*)$", re.M)


def _skill_description(path):
    """Return the frontmatter description string for a SKILL.md, or None.

    Reads the leading YAML frontmatter block and extracts the single-line
    `description:` value. Returns None when there is no frontmatter or no
    description field. Length is later measured in characters (Unicode code
    points), not bytes, so multi-byte em-dashes count as one each.
    """
    try:
        txt = open(path, encoding="utf-8").read()
    except Exception:
        return None
    m = _FRONTMATTER_RE.match(txt)
    if not m:
        return None
    dm = _DESCRIPTION_RE.search(m.group(1))
    if not dm:
        return None
    return dm.group(1).strip()


def check_description_cap(root: str) -> list:
    """Warn when a SKILL.md frontmatter description exceeds DESCRIPTION_CAP chars.

    Warn-only (like skill-budget): reported and counted under --strict so the
    always-loaded skill-index footprint can't silently creep back. Findings are
    sorted by skill (glob is already sorted).
    """
    findings = []
    for p in sorted(glob.glob(f"{root}/.claude/skills/*/SKILL.md")):
        desc = _skill_description(p)
        if desc is None:
            continue
        n = len(desc)
        if n > DESCRIPTION_CAP:
            findings.append(
                f"{rel(root,p)}: description {n} chars > {DESCRIPTION_CAP} cap "
                f"(trim trigger tail)"
            )
    return findings


# ── Check 12: anti-patterns size ──────────────────────────────────────────
# A level-2 heading whose text mentions "Anti-patterns" (covers the bare
# "## Anti-patterns" and the "## §N — Anti-patterns" variant). Reference-stub
# bullets ("- `Anti-patterns` — see `reference.md`") are not headings, so they
# are never matched here.
_ANTI_PATTERNS_HEAD_RE = re.compile(r"^##[ \t]+.*Anti-patterns", re.M)
_LEVEL2_HEAD_RE = re.compile(r"^##[ \t]", re.M)


def _anti_patterns_section_bytes(path):
    """Return the byte-size of a SKILL.md's ## Anti-patterns section, or None.

    The section runs from its heading to the next level-2 heading (or EOF).
    Returns None when the file has no Anti-patterns heading. Size is measured
    in UTF-8 bytes of the section text (heading line included).
    """
    try:
        txt = open(path, encoding="utf-8").read()
    except Exception:
        return None
    m = _ANTI_PATTERNS_HEAD_RE.search(txt)
    if not m:
        return None
    nxt = _LEVEL2_HEAD_RE.search(txt, m.end())
    end = nxt.start() if nxt else len(txt)
    return len(txt[m.start():end].encode("utf-8"))


def check_anti_patterns(root: str) -> list:
    """Warn when a SKILL.md ## Anti-patterns section exceeds ANTI_PATTERNS_CAP bytes.

    Warn-only (like skill-budget): reported and counted under --strict.
    Findings are sorted by skill (glob is already sorted).
    """
    findings = []
    for p in sorted(glob.glob(f"{root}/.claude/skills/*/SKILL.md")):
        size = _anti_patterns_section_bytes(p)
        if size is None:
            continue
        if size > ANTI_PATTERNS_CAP:
            findings.append(
                f"{rel(root,p)}: ## Anti-patterns {size} bytes > {ANTI_PATTERNS_CAP} "
                f"(cap ~5 bullets / move to reference.md)"
            )
    return findings


# ── Check 16: product-readme-present ──────────────────────────────────────
# Fingerprint of the unmodified golden-seed scaffold README
# (claude-code/README.md) — a fresh, never-customized project's root README
# still carries this exact title + section, describing the framework instead
# of the product. Hardcoded like other check fingerprints in this file (e.g.
# DOCS_PARITY_ALLOW) rather than read from claude-code/README.md at runtime,
# so the check works standalone in a consumer project that has no claude-code/
# flavor directory at all.
SCAFFOLD_README_TITLE = "# Claude Code Scaffold"
SCAFFOLD_README_SECTION = "## What's included"


def _is_scaffold_readme(content):
    """True when *content* is the unmodified generic scaffold README.

    Matches on the golden-seed title line (exact, ignoring surrounding
    whitespace) AND the distinguishing section heading — both must be present
    so a product README that merely mentions "Claude Code" in prose is never
    misflagged.
    """
    has_title = bool(re.search(rf"^{re.escape(SCAFFOLD_README_TITLE)}\s*$", content, re.M))
    has_section = SCAFFOLD_README_SECTION in content
    return has_title and has_section


def check_product_readme_present(root: str) -> list:
    """Root README.md must exist AND not be the unmodified scaffold README.

    Deterministic, report-only by default (fails only the --strict gate, like
    every other check in this file). A missing README fails a Grimoire project
    at the front door just as hard as the unmodified scaffold copy — both mean
    a visitor lands on framework boilerplate (or nothing) instead of product
    content.
    """
    findings = []
    readme_path = os.path.join(root, "README.md")
    if not os.path.exists(readme_path):
        findings.append(
            "README.md missing at project root — every product needs a "
            "front-door README describing what it does"
        )
        return findings
    content = open(readme_path, encoding="utf-8").read()
    if _is_scaffold_readme(content):
        findings.append(
            'README.md is the unmodified generic scaffold README ("Claude Code '
            'Scaffold") — replace it with product-specific content describing '
            "what this project is and does"
        )
    return findings


# ── Check 17: version-claim-freshness ─────────────────────────────────────
# README/CHANGELOG-class docs commonly quote the project's own version in
# prose ("at v3.14", headline banners, feature lists dated by version) and
# that prose rots the moment a release ships without touching those files —
# unlike docs/version-history.md, which the release gate refuses to let rot.
# This check flags any such version claim that is >= 1 minor behind the
# manifest's current version; remediation is to *remove* the claim from prose
# in favor of a link to docs/version-history.md (the one place versions never
# go stale), not to chase every claim on every release.
VERSION_CLAIM_CLASS_FILES = ("README.md", "CHANGELOG.md", "docs/changelog.md")
_VERSION_CLAIM_RE = re.compile(r"\bv(\d+)\.(\d+)(?:\.\d+)?\b")


def _read_manifest_version(root):
    """Read the project's own version (framework-version) as an (major, minor) tuple.

    Returns None if grimoire-config.json is absent/unreadable/has no usable
    version — the check then no-ops rather than guessing.
    """
    cfg_path = os.path.join(root, ".claude", "grimoire-config.json")
    if not os.path.exists(cfg_path):
        return None
    try:
        cfg = json.load(open(cfg_path))
        fw = cfg.get("framework-version", "").lstrip("v")
        parts = fw.split(".")
        return (int(parts[0]), int(parts[1]))
    except (ValueError, AttributeError, IndexError, TypeError):
        return None


def check_version_claim_freshness(root: str) -> list:
    """Flag a README/CHANGELOG-class doc whose NEWEST version-string claim is
    >= 1 minor behind the manifest's current version (BLOCKING under
    --strict), plus a WARN-tier sweep of every other doc under docs/
    (#416, v3.98) — see `_version_claim_fleet_findings` below.

    Scans a fixed, deterministic file set (VERSION_CLAIM_CLASS_FILES) for
    version strings matching the project's own vX.Y(.Z) pattern and takes the
    highest one found in each file. A changelog legitimately lists many old
    version headers (that is its job), so per-mention flagging would fire on
    every historical entry; what actually indicates rot — an example like
    "CHANGELOG.md ~26 versions behind" — is the newest entry in the doc
    lagging the manifest. One finding per stale file.
    No manifest version readable -> no findings (the check degrades to a
    no-op rather than guessing at a pattern).
    """
    findings = []
    current = _read_manifest_version(root)
    if current is None:
        return findings
    cur_major, cur_minor = current
    for relpath in VERSION_CLAIM_CLASS_FILES:
        p = os.path.join(root, relpath)
        if not os.path.exists(p):
            continue
        content = _strip_code(open(p, encoding="utf-8").read())
        found_versions = [(int(m.group(1)), int(m.group(2)))
                           for m in _VERSION_CLAIM_RE.finditer(content)]
        if not found_versions:
            continue
        newest = max(found_versions)
        if newest >= current:
            continue
        if newest[0] == cur_major:
            gap = f"{cur_minor - newest[1]} minor(s)"
        else:
            gap = "a major version"
        findings.append(
            f"{relpath}: newest version claim v{newest[0]}.{newest[1]} is {gap} "
            f"behind current v{cur_major}.{cur_minor} — remove hardcoded version "
            f"claims from prose and link docs/version-history.md instead"
        )
    findings = sorted(findings)
    findings.extend(_version_claim_fleet_findings(root, current))
    return findings


# ── version-claim-freshness fleet extension (#416, v3.98) ────────────────
# The blocking loop above only ever covered the fixed README/CHANGELOG
# class. The architecture audit found staleness endemic in *version-bearing
# design and operator docs* fleet-wide — a "vX.Y"/"as of vX.Y" claim in a
# doc's own title or opening blurb, left unmaintained 10-26 releases. This
# second pass extends the same freshness comparison to every other doc
# under docs/, WARN-tier only: reported and counted under --strict like
# check_for_checks/description-cap/anti-patterns above, but it never
# escalates to the hierarchy-dial's non-negotiable block tier and the
# existing README/CHANGELOG class above stays the only BLOCKING path.
#
# Deliberately narrow scan span: only a doc's own first heading plus its
# opening paragraph are scanned, not the whole file. A doc's *body*
# legitimately accumulates many historical version mentions (release notes,
# changelogs-in-miniature, "as of vX.Y we did A, then vX.Z we did B"
# narrative) and per-mention flagging over the whole file would fire on
# every one of those; what actually indicates rot is the doc's own framing
# — the claim a reader sees first — lagging the manifest.
#
# Two exclusions, both reusing existing conventions rather than inventing
# new ones:
#   - Release-planning docs (`_RELEASE_PLAN_RE`) are already auto-exempt
#     elsewhere in this file for the same reason: a plan doc's own "vX.Y"
#     heading names its subject, not a freshness claim about itself.
#   - A doc marked "kept for lineage" (case-insensitive, anywhere in the
#     file) is deliberately historical — the framing docs/version-history.md
#     and docs/roadmap-archive.md already use for content that is supposed
#     to reference an old version forever — and is skipped entirely.
_LINEAGE_MARKER_RE = re.compile(r"kept for lineage", re.I)
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+.*$", re.MULTILINE)


def _doc_heading_and_opening_text(content: str) -> str:
    """Return a doc's first heading line plus its opening paragraph (the
    first non-blank block of text following that heading, up to the next
    blank line or heading) — the only span the fleet-wide WARN check scans
    for a version claim. '' if the doc has no heading at all."""
    m = _MD_HEADING_RE.search(content)
    if not m:
        return ""
    para_lines = []
    for line in content[m.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            if para_lines:
                break
            continue
        if _MD_HEADING_RE.match(line):
            break
        para_lines.append(line)
    return m.group() + "\n" + "\n".join(para_lines)


def _version_claim_fleet_findings(root: str, current: tuple) -> list:
    """WARN-tier findings: a stale vX.Y claim in the heading/opening
    paragraph of any docs/**/*.md file outside the README/CHANGELOG class.
    See the module comment above for scope and exclusions."""
    findings = []
    cur_major, cur_minor = current
    class_relpaths = {os.path.normpath(p) for p in VERSION_CLAIM_CLASS_FILES}
    docs_root = os.path.join(root, "docs")
    if not os.path.isdir(docs_root):
        return findings
    for dirpath, _dirnames, filenames in os.walk(docs_root):
        for fname in sorted(filenames):
            if not fname.endswith(".md"):
                continue
            p = os.path.join(dirpath, fname)
            relpath = os.path.relpath(p, root).replace(os.sep, "/")
            if os.path.normpath(relpath) in class_relpaths:
                continue
            if _RELEASE_PLAN_RE.match(relpath):
                continue
            try:
                content = open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if _LINEAGE_MARKER_RE.search(content):
                continue
            span = _doc_heading_and_opening_text(_strip_code(content))
            found_versions = [(int(m.group(1)), int(m.group(2)))
                               for m in _VERSION_CLAIM_RE.finditer(span)]
            if not found_versions:
                continue
            newest = max(found_versions)
            if newest >= current:
                continue
            if newest[0] == cur_major:
                gap = f"{cur_minor - newest[1]} minor(s)"
            else:
                gap = "a major version"
            findings.append(
                f"{relpath}: heading/opening-paragraph version claim "
                f"v{newest[0]}.{newest[1]} is {gap} behind current "
                f"v{cur_major}.{cur_minor} (WARN — update the claim, relocate "
                f"it to docs/version-history.md, or mark the section "
                f"'kept for lineage' if intentionally historical)"
            )
    return sorted(findings)


# ── Check 22: classifier-compat (epic #393, #421) ─────────────────────────
# An auto-mode harness safety classifier pattern-matches flag/verb SHAPE, not
# semantics — a flag literally named `--allow-ahead` reads as [Safety Bypass
# Flag] regardless of what it does. Two sub-checks over shipped *.py/*.sh
# (the actual CLI surface; SKILL.md's documented human-confirmed last-resort
# commands are a distinct, already-governed convention — CLAUDE.md
# §Commits — and out of scope here): (1) a bypass/override/skip-guard-shaped
# flag defined by a script outside .claude/hooks/ (hooks ARE the guard
# layer, so a hook defining e.g. `--force-with-lease` is already vetted);
# (2) a raw destructive git/shell verb in a script that isn't one of the
# named wrapper/guard scripts the hooks can already recognize.
_BYPASS_FLAG_RE = re.compile(
    r"^--(?:allow|skip|bypass|override)-[\w-]+$|^--no-verify$|^--force-[\w-]+$",
    re.I,
)
_PY_FLAG_DEF_RE = re.compile(r"""add_argument\(\s*['"](--[\w-]+)['"]""")
_SH_FLAG_DEF_RE = re.compile(r"^\s*(--[\w-]+)\)")
_DESTRUCTIVE_OP_RE = re.compile(
    r"\brm\s+-rf\b|\bgit\s+reset\s+--hard\b|\bgit\s+push\s+(?:-f\b|--force\b)"
    r"|\bgit\s+branch\s+-D\b|\bgit\s+rebase\b|\bgit\s+filter-branch\b"
)

# Named scripts already known to legitimately wrap a destructive op (their own
# scratch-dir cleanup, or a print-only suggestion for a human-confirmed batch
# per CLAUDE.md §Commits) — add an entry only after reading the file and
# confirming it is a maintained, named wrapper, never a blanket bypass.
CLASSIFIER_COMPAT_DESTRUCTIVE_OP_ALLOW = frozenset({
    ".claude/skills/grm-integration-master/branch_cleanup.py",
    ".claude/skills/grm-sync-from-upstream/sync-from-upstream.sh",
    # this very file — the destructive-op / bypass-flag literals below are
    # this check's own regex fixtures and self-test data, not real invocations.
    ".claude/skills/grm-doc-assurance/doc_assurance.py",
})


def _classifier_compat_scan(relpath, text):
    """Return classifier-compat findings for one shipped script's text."""
    findings = []
    is_hook = relpath.startswith(".claude/hooks/")
    if not is_hook:
        if relpath.endswith(".py"):
            flags = _PY_FLAG_DEF_RE.findall(text)
        else:
            flags = [m.group(1) for ln in text.splitlines()
                     for m in [_SH_FLAG_DEF_RE.match(ln)] if m]
        for flag in flags:
            if _BYPASS_FLAG_RE.match(flag):
                findings.append(
                    f"{relpath}: flag `{flag}` is bypass-shaped (reads as a "
                    f"[Safety Bypass Flag] to an auto-mode classifier) and is "
                    f"not routed through a named guard-vetted script (#393)"
                )
    if not is_hook and relpath not in CLASSIFIER_COMPAT_DESTRUCTIVE_OP_ALLOW:
        for m in _DESTRUCTIVE_OP_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            findings.append(
                f"{relpath}:{line_no}: raw destructive op `{m.group(0)}` "
                f"outside a named guard-vetted wrapper script (#393)"
            )
    return findings


def check_classifier_compat(root: str) -> list:
    """Flag bypass-shaped CLI flag names and raw destructive ops shipped
    outside a named, guard-vetted wrapper script (epic #393 / #421).

    Deterministic, report-only by default (fails only the --strict gate).
    """
    findings = []
    for sub in ("skills", "hooks", "mcp-servers"):
        base = os.path.join(root, ".claude", sub)
        if not os.path.isdir(base):
            continue
        for ext in (".py", ".sh"):
            for p in sorted(glob.glob(f"{base}/**/*{ext}", recursive=True)):
                relpath = rel(root, p)
                try:
                    text = open(p, encoding="utf-8").read()
                except Exception:
                    continue
                findings.extend(_classifier_compat_scan(relpath, text))
    return sorted(findings)


# ── Dial: read doc-hierarchy enforcer value from grimoire-config.json ────

def _read_hierarchy_dial(root):
    """Read doc-hierarchy.enforcer.value from grimoire-config.json.

    Returns 'off', 'warn', or 'block'. Defaults to 'warn' if absent/unreadable.
    """
    cfg_path = os.path.join(root, ".claude", "grimoire-config.json")
    try:
        cfg = json.load(open(cfg_path))
        val = cfg.get("doc-hierarchy", {}).get("enforcer", {}).get("value", "warn")
        if val in ("off", "warn", "block"):
            return val
    except Exception:
        pass
    return "warn"


# ── Baseline ratchet (#426, v3.93) ──────────────────────────────────────────
BASELINE_DEFAULT_REL = ".claude/cache/doc-findings-baseline.json"


def _baseline_key(check, finding):
    """Stable identity for one finding, used for baseline set membership."""
    return f"{check}: {finding}"


def _load_baseline(path):
    """Read a baseline file -> set of finding keys, or None if absent /
    unreadable (treated as 'no baseline yet' — the caller seeds one)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return set(data.get("findings", []))


def _write_baseline(path, finding_keys):
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    payload = {"version": 1, "findings": sorted(finding_keys)}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def apply_baseline(path, all_findings):
    """Diff current findings against a stored baseline file (#426 ratchet).

    `all_findings` is {check_name: [finding_str, ...]} for the checks that
    just ran. Returns a dict describing the trend:
      {"total", "new", "resolved", "baseline_count", "seeded", "ratcheted",
       "report"} — `new`/`resolved` are sorted lists of finding keys,
      `report` a ready-to-print human-readable trend line.

    Semantics:
      - No baseline file yet -> SEED: write the current findings as the
        baseline. Nothing counts as new (there is nothing yet to regress
        against) — a first run never fails the baseline gate.
      - Baseline exists -> DIFF by identity (`<check>: <finding text>`):
        findings absent from the baseline are "new" (a regression); findings
        in the baseline no longer present are "resolved". A finding already
        in the baseline is still surfaced in the normal per-check output —
        it just doesn't newly fail anything it wasn't already failing.
      - Ratchet: when the current set is a SUBSET of the baseline (some
        findings resolved, none added), the baseline file is rewritten to
        the smaller current set — debt can only shrink, never silently grow.
        When there ARE new findings the file is left untouched, so the
        regression (and the ratchet point) stays visible until it's fixed.
    """
    current_keys = {_baseline_key(check, finding)
                     for check, findings in all_findings.items()
                     for finding in findings}
    baseline_keys = _load_baseline(path)

    if baseline_keys is None:
        _write_baseline(path, current_keys)
        return {
            "total": len(current_keys), "new": [], "resolved": [],
            "baseline_count": len(current_keys), "seeded": True,
            "ratcheted": False,
            "report": ("doc-assurance baseline: seeded %d finding(s) to %s "
                       "(no prior baseline — first run never fails)."
                       % (len(current_keys), path)),
        }

    new_keys = sorted(current_keys - baseline_keys)
    resolved_keys = sorted(baseline_keys - current_keys)

    ratcheted = False
    if not new_keys and resolved_keys:
        _write_baseline(path, current_keys)
        ratcheted = True

    if new_keys:
        report = ("doc-assurance baseline: %d finding(s), %d NEW since "
                   "baseline (%d baselined)."
                   % (len(current_keys), len(new_keys), len(baseline_keys)))
    elif ratcheted:
        report = ("doc-assurance baseline: %d finding(s), same as baseline "
                   "(0 new); %d resolved — baseline ratcheted down from %d "
                   "to %d."
                   % (len(current_keys), len(resolved_keys),
                      len(baseline_keys), len(current_keys)))
    else:
        report = ("doc-assurance baseline: %d finding(s), same as baseline "
                   "(%d baselined, 0 new)."
                   % (len(current_keys), len(baseline_keys)))

    return {
        "total": len(current_keys), "new": new_keys, "resolved": resolved_keys,
        "baseline_count": len(baseline_keys), "seeded": False,
        "ratcheted": ratcheted, "report": report,
    }


# ── Self-test ────────────────────────────────────────────────────────────
def self_test() -> tuple:
    """In-memory unit tests for check_design_layout, check_relative_links,
    check_hierarchy, and check_flavor_parity (WH-7 three-flavor docs parity).

    Covers: legacy-passing doc, house-template-passing doc, doc satisfying
    both, doc satisfying neither, and the open-questions marker rule.
    Also covers: absolute internal link, missing breadcrumb, valid breadcrumb,
    bare-prose doc ref, hierarchy orphan.
    WH-7: file in claude-code but not copilot is flagged when not allow-listed;
    same gap passes when allow-listed; release-planning archives never flagged.
    description-cap: over-cap description flagged, under-cap clean.
    anti-patterns: oversized section flagged, small clean, reference-stub
    bullet never measured.
    design-index generation: house-layout parsing (title + Motivation opener),
    house-layout-missing docs reported not silently skipped, curated "##"
    sections in docs/grimoire/design/README.md survive generation, idempotent
    re-run is byte-identical, staleness detection on missing/new docs.
    Baseline ratchet (#426): seed-on-first-run, unchanged-reports-0-new,
    resolved-finding-ratchets-down, new-finding-flagged, baseline-untouched-
    while-a-regression-is-outstanding.
    check-for-checks (#440): MUST-clause with no check reference flagged
    (coding-standards.md section, catalog Entry); MUST-clause paired with a
    check reference (grm- skill mention, Testable-criterion table) silent.
    Returns (passed, failed, lines).
    """
    import tempfile, os as _os, shutil

    def _fake_doc(content, suffix="-design.md", tmpdir=None):
        """Write content to a temp file and return its path."""
        fd, path = tempfile.mkstemp(suffix=suffix, dir=tmpdir)
        _os.write(fd, content.encode())
        _os.close(fd)
        return path

    cases = []

    def run_layout(content):
        """Run check_design_layout against a single in-memory doc."""
        path = _fake_doc(content)
        tmpdir = _os.path.dirname(path)
        # Monkey-patch glob so the check sees only our file. check_design_layout
        # now globs TWO design tiers (docs/design + docs/grimoire/design, both
        # ending in *-design.md); return the file for the project-own tier only
        # and an empty list for the framework tier so the single fake doc is
        # evaluated exactly once (no duplicate findings).
        import glob as _glob
        orig = _glob.glob
        def _fake_glob(pat, **kw):
            if pat.endswith("docs/design/*-design.md"):
                return [path]
            if pat.endswith("*-design.md"):
                return []
            return orig(pat, **kw)
        _glob.glob = _fake_glob
        try:
            findings = check_design_layout(tmpdir)
        finally:
            _glob.glob = orig
            _os.unlink(path)
        return findings

    # 1. Legacy-passing doc (motivation + goals + non-goal + validation) — no findings.
    legacy_doc = (
        "# Feature\n"
        "## Motivation\nWhy.\n"
        "## Goals\n- goal\n"
        "## Non-goals\nNone.\n"
        "## Validation\nIdempotent.\n"
    )
    f = run_layout(legacy_doc)
    cases.append(("legacy-pattern doc passes (no findings)", not f))

    # 2. House-template doc (motivation + scope + design) — no findings.
    house_doc = (
        "# Feature\n"
        "## Motivation\nWhy.\n"
        "## Scope\nWhat.\n"
        "## Design\nHow.\n"
        "## Acceptance\n- [ ] done\n"
    )
    f = run_layout(house_doc)
    cases.append(("house-template doc passes (no findings)", not f))

    # 3. House-template doc with only motivation + scope + acceptance (no ## Design) — still passes.
    house_no_design = (
        "# Feature\n"
        "## Motivation\nWhy.\n"
        "## Scope\nWhat.\n"
        "## Acceptance\n- [ ] done\n"
    )
    f = run_layout(house_no_design)
    cases.append(("house-template doc with acceptance but no design section passes", not f))

    # 4. Doc satisfying BOTH patterns — no findings.
    both_doc = (
        "# Feature\n"
        "## Motivation\nWhy.\n"
        "## Goals\n- g\n"
        "## Non-goals\nNone.\n"
        "## Scope\nWhat.\n"
        "## Design\nHow.\n"
        "## Validation / Idempotency\nOK.\n"
        "## Acceptance\n- [ ] done\n"
    )
    f = run_layout(both_doc)
    cases.append(("doc satisfying both patterns passes (no findings)", not f))

    # 5. Doc satisfying neither — exactly one layout finding.
    neither_doc = (
        "# Feature\n"
        "## 1. Problem\nThe issue.\n"
        "## 2. Solution\nThe fix.\n"
        "## 3. Out of scope\nNone.\n"
    )
    f = run_layout(neither_doc)
    layout_findings = [x for x in f if "does not satisfy" in x]
    cases.append(("doc satisfying neither set emits one layout finding", len(layout_findings) == 1))

    # 6. Unresolved open-questions marker fires regardless of layout.
    open_q_doc = (
        "# Feature\n"
        "## Motivation\nWhy.\n"
        "## Scope\nWhat.\n"
        "## Design\nHow.\n"
        "## Open questions\nTODO: decide something.\n"
    )
    f = run_layout(open_q_doc)
    oq_findings = [x for x in f if "unresolved" in x]
    cases.append(("open-questions marker fires on passing house-template doc", len(oq_findings) == 1))

    # 7. Doc with motivation + scope but NO design or acceptance — still fails house-template.
    house_incomplete = (
        "# Feature\n"
        "## Motivation\nWhy.\n"
        "## Scope\nWhat.\n"
    )
    f = run_layout(house_incomplete)
    layout_findings = [x for x in f if "does not satisfy" in x]
    cases.append(("motivation+scope only (no design/acceptance) fails house-template", len(layout_findings) == 1))

    # ── New: check_relative_links tests ────────────────────────────────

    def _setup_tmpdir():
        """Create a minimal temp directory tree for link/hierarchy tests."""
        d = tempfile.mkdtemp()
        docs = _os.path.join(d, "docs")
        _os.makedirs(docs)
        return d, docs

    # 8. Absolute internal link → 1 finding
    d, docs = _setup_tmpdir()
    try:
        p = _os.path.join(d, "test.md")
        open(p, "w").write("[link](/docs/foo.md)\n")
        f = check_relative_links(docs, d)
        abs_findings = [x for x in f if "absolute internal link" in x]
        cases.append(("file with absolute internal link emits 1 finding", len(abs_findings) == 1))
    finally:
        shutil.rmtree(d)

    # 9. Bare-prose doc ref → 1 finding
    d, docs = _setup_tmpdir()
    try:
        # Create the referenced file so it "exists"
        _os.makedirs(_os.path.join(d, "docs", "design"), exist_ok=True)
        target = _os.path.join(d, "docs", "design", "foo-design.md")
        open(target, "w").write("# Foo\n")
        # Create a doc that references it as a bare backtick
        p = _os.path.join(docs, "bar.md")
        open(p, "w").write("> **Up:** [↑ Docs](README.md)\n\nSee `docs/design/foo-design.md` for details.\n")
        f = check_relative_links(docs, d)
        bare_findings = [x for x in f if "bare-prose doc ref" in x]
        cases.append(("file with bare-prose doc ref emits 1 finding", len(bare_findings) == 1))
    finally:
        shutil.rmtree(d)

    # ── New: check_hierarchy tests ──────────────────────────────────────

    # 10. Missing breadcrumb → 1 finding
    d, docs = _setup_tmpdir()
    try:
        readme = _os.path.join(docs, "README.md")
        open(readme, "w").write(
            "# Docs Root\n\n- [foo](foo.md)\n\n<!-- docs-map:begin -->\n<!-- docs-map:end -->\n"
        )
        foo = _os.path.join(docs, "foo.md")
        open(foo, "w").write("# Foo\n\nNo breadcrumb here.\n")
        f = check_hierarchy(docs)
        bc_findings = [x for x in f if "missing breadcrumb" in x]
        cases.append(("file with missing breadcrumb emits 1 finding", len(bc_findings) == 1))
    finally:
        shutil.rmtree(d)

    # 11. Valid breadcrumb → 0 breadcrumb findings
    d, docs = _setup_tmpdir()
    try:
        readme = _os.path.join(docs, "README.md")
        open(readme, "w").write(
            "# Docs Root\n\n- [bar](bar.md)\n\n<!-- docs-map:begin -->\n<!-- docs-map:end -->\n"
        )
        bar = _os.path.join(docs, "bar.md")
        open(bar, "w").write("> **Up:** [↑ Docs](README.md)\n\n# Bar\n")
        f = check_hierarchy(docs)
        bc_findings = [x for x in f if "missing breadcrumb" in x]
        cases.append(("file with valid breadcrumb emits 0 breadcrumb findings", len(bc_findings) == 0))
    finally:
        shutil.rmtree(d)

    # 12. Hierarchy orphan → 1 orphan finding
    d, docs = _setup_tmpdir()
    try:
        readme = _os.path.join(docs, "README.md")
        open(readme, "w").write(
            "# Docs Root\n\n<!-- docs-map:begin -->\n<!-- docs-map:end -->\n"
        )
        # orphan.md is NOT linked from README.md
        orphan = _os.path.join(docs, "orphan.md")
        open(orphan, "w").write("> **Up:** [↑ Docs](README.md)\n\n# Orphan\n")
        f = check_hierarchy(docs)
        orphan_findings = [x for x in f if "hierarchy orphan" in x]
        cases.append(("unreachable file emits 1 orphan finding", len(orphan_findings) == 1))
    finally:
        shutil.rmtree(d)

    # ── WH-7: Three-flavor docs parity self-tests ────────────────────────
    # These tests exercise check_flavor_parity's new docs file-name set comparison
    # by calling the function with synthetic fake_docs sets, using a custom
    # _allow_set argument (the function accepts it as an override for testing).

    def _run_parity_with_docs(rt_docs, cc_docs, cp_docs, allow_set=frozenset()):
        """Simulate check_flavor_parity's three-flavor docs check only.

        Directly exercises the inner logic without touching the filesystem,
        by constructing a synthetic root with fake flavor directories.
        Returns the docs-parity findings list.
        """
        import tempfile as _tmp, os as _os2

        # Build a minimal synthetic tree: root/ with claude-code/ and copilot/ subdirs
        # and the required docs files.
        tmproot = _tmp.mkdtemp()
        try:
            for flavor, docs_set in [(".", rt_docs), ("claude-code", cc_docs), ("copilot", cp_docs)]:
                flavor_root = _os2.path.join(tmproot, flavor)
                for doc in docs_set:
                    fpath = _os2.path.join(flavor_root, doc)
                    _os2.makedirs(_os2.path.dirname(fpath), exist_ok=True)
                    open(fpath, "w").write("# stub\n")
            # Also write the sentinel files so find_root can locate the root.
            open(_os2.path.join(tmproot, "CLAUDE.md"), "w").write("")
            findings = check_flavor_parity(tmproot, _allow_set=allow_set)
            # Return only the docs-parity findings (not skill or must-match ones)
            return [f2 for f2 in findings if "docs file in" in f2]
        finally:
            shutil.rmtree(tmproot, ignore_errors=True)

    # 13. File in claude-code but not copilot (not allow-listed) → flagged.
    rt_docs13 = {"docs/design/foo-design.md"}
    cc_docs13  = {"docs/design/foo-design.md", "docs/design/claude-only.md"}
    cp_docs13  = {"docs/design/foo-design.md"}
    f13 = _run_parity_with_docs(rt_docs13, cc_docs13, cp_docs13, allow_set=frozenset())
    flagged13 = any("claude-only.md" in x and "claude-code" in x and "copilot" in x for x in f13)
    cases.append(("file in claude-code but not copilot (not allow-listed) is flagged", flagged13))

    # 14. Same gap, now allow-listed → passes (no finding for that specific gap).
    allow14 = frozenset({("claude-code", "copilot", "docs/design/claude-only.md")})
    f14 = _run_parity_with_docs(rt_docs13, cc_docs13, cp_docs13, allow_set=allow14)
    still_flagged14 = any("claude-only.md" in x and "claude-code" in x and "copilot" in x for x in f14)
    cases.append(("same gap, when allow-listed, does not produce a finding", not still_flagged14))

    # 15. Release-planning archives (root-only) are never flagged, even without an explicit entry.
    rt_docs15 = {"docs/grimoire/release-planning-v3.37.md", "docs/grimoire/release-planning-v1.5.md"}
    cc_docs15 = set()
    cp_docs15 = set()
    f15 = _run_parity_with_docs(rt_docs15, cc_docs15, cp_docs15, allow_set=frozenset())
    flagged15 = any("release-planning" in x for x in f15)
    cases.append(("release-planning archives are never flagged as parity gaps", not flagged15))

    # ── WH-8: lean-index self-tests ─────────────────────────────────────
    import tempfile as _tmpmod

    def _run_lean_index(files_content, override_exempt=None):
        """Write README.md files into a temp tree and run a parameterized lean-index."""
        tmproot = _tmpmod.mkdtemp()
        try:
            for rpath, content in files_content.items():
                fpath = _os.path.join(tmproot, rpath)
                _os.makedirs(_os.path.dirname(fpath), exist_ok=True)
                open(fpath, "w").write(content)

            exempt = LEAN_INDEX_SIZE_EXEMPT if override_exempt is None else frozenset(override_exempt)
            findings = []
            for p in sorted(glob.glob(f"{tmproot}/docs/**/README.md", recursive=True)):
                rp = rel(tmproot, p)
                content = open(p).read()
                size = _os.path.getsize(p)
                links = _MD_LINK_RE.findall(content)

                if rp not in exempt and size > LEAN_INDEX_SIZE_CAP:
                    findings.append(
                        f"{rp}: index page {size} bytes > {LEAN_INDEX_SIZE_CAP} budget "
                        f"(lean-index rule)"
                    )
                if len(links) < LEAN_INDEX_MIN_LINKS:
                    findings.append(
                        f"{rp}: index page has only {len(links)} links "
                        f"(minimum {LEAN_INDEX_MIN_LINKS}, lean-index rule)"
                    )
            return findings
        finally:
            shutil.rmtree(tmproot, ignore_errors=True)

    # 16. Tiny README with <3 links → fails link-density rule.
    sparse_readme = "# Index\n\nNo links here.\n"
    f16 = _run_lean_index({"docs/design/README.md": sparse_readme})
    cases.append(("sparse README (<3 links) fails lean-index link-density", len(f16) >= 1))

    # 17. README with ≥3 links and ≤6KB → passes.
    dense_small_readme = (
        "# Index\n\n"
        "[Alpha](alpha.md) [Beta](beta.md) [Gamma](gamma.md)\n"
    )
    f17 = _run_lean_index({"docs/design/README.md": dense_small_readme})
    cases.append(("README with ≥3 links and ≤6KB passes lean-index", len(f17) == 0))

    # 18. Root docs/README.md (size-exempt) passes even if >6KB.
    big_root_readme = "[a](a.md) [b](b.md) [c](c.md)\n" + ("x" * 7000)
    f18 = _run_lean_index(
        {"docs/README.md": big_root_readme},
        override_exempt={"docs/README.md"},
    )
    cases.append(("root docs/README.md is exempt from lean-index size cap", len(f18) == 0))

    # ── WH-8: monolith-cap self-tests ───────────────────────────────────
    def _run_monolith_cap(files_content, override_exempt=None):
        """Write leaf docs into a temp tree and run a parameterized monolith-cap."""
        tmproot = _tmpmod.mkdtemp()
        try:
            for rpath, content in files_content.items():
                fpath = _os.path.join(tmproot, rpath)
                _os.makedirs(_os.path.dirname(fpath), exist_ok=True)
                open(fpath, "w").write(content)

            exempt = MONOLITH_CAP_EXEMPT if override_exempt is None else frozenset(override_exempt)
            findings = []
            for p in sorted(glob.glob(f"{tmproot}/docs/**/*.md", recursive=True)):
                if _os.path.basename(p) == "README.md":
                    continue
                rp = rel(tmproot, p)
                if rp in exempt:
                    continue
                if _MONOLITH_CAP_RELEASE_PLAN_RE.match(rp):
                    continue
                size = _os.path.getsize(p)
                if size > MONOLITH_CAP:
                    findings.append(f"{rp}: {size} bytes > {MONOLITH_CAP} monolith cap")
            return findings
        finally:
            shutil.rmtree(tmproot, ignore_errors=True)

    # 19. Leaf file >20KB is flagged by monolith-cap.
    big_leaf_content = "# Big doc\n" + ("x" * 21000)
    f19 = _run_monolith_cap({"docs/design/big-design.md": big_leaf_content})
    cases.append(("leaf file >20KB is flagged by monolith-cap", len(f19) == 1))

    # 20. Same file, explicitly exempted → not flagged.
    f20 = _run_monolith_cap(
        {"docs/design/big-design.md": big_leaf_content},
        override_exempt={"docs/design/big-design.md"},
    )
    cases.append(("exempted leaf file >20KB is not flagged by monolith-cap", len(f20) == 0))

    # 21. release-planning archive (>20KB) is never flagged by monolith-cap.
    big_plan_content = "# Plan\n" + ("x" * 25000)
    f21 = _run_monolith_cap(
        {"docs/grimoire/release-planning-v9.99.md": big_plan_content},
        override_exempt=set(),
    )
    cases.append(("release-planning archive >20KB is never flagged by monolith-cap", len(f21) == 0))

    # ── Check 11/12: description-cap + anti-patterns self-tests ───────────
    def _skill_tree(skills):
        """Write {skill_name: SKILL.md content} into a temp root's skills dir.

        Returns the temp root; the real check functions glob
        {root}/.claude/skills/*/SKILL.md, so they exercise the production path.
        Caller is responsible for cleanup.
        """
        tmproot = _tmpmod.mkdtemp()
        for name, content in skills.items():
            d = _os.path.join(tmproot, ".claude", "skills", name)
            _os.makedirs(d, exist_ok=True)
            open(_os.path.join(d, "SKILL.md"), "w", encoding="utf-8").write(content)
        return tmproot

    def _frontmatter(desc):
        return f"---\nname: stub\ndescription: {desc}\n---\n\n# Stub\n"

    # 22. Over-cap description (> 450 chars) → flagged.
    over_desc = "A" * (DESCRIPTION_CAP + 25)
    t22 = _skill_tree({"over": _frontmatter(over_desc)})
    try:
        f22 = check_description_cap(t22)
    finally:
        import shutil as _sh22
        _sh22.rmtree(t22, ignore_errors=True)
    cases.append(("over-cap SKILL.md description is flagged by description-cap", len(f22) == 1))

    # 23. Under-cap description → clean.
    under_desc = "B" * (DESCRIPTION_CAP - 50)
    t23 = _skill_tree({"under": _frontmatter(under_desc)})
    try:
        f23 = check_description_cap(t23)
    finally:
        import shutil as _sh23
        _sh23.rmtree(t23, ignore_errors=True)
    cases.append(("under-cap SKILL.md description is not flagged by description-cap", len(f23) == 0))

    # 24. Oversized ## Anti-patterns section (> 1500 bytes) → flagged.
    big_ap = (
        "---\nname: stub\ndescription: short\n---\n\n# Stub\n\n"
        "## Anti-patterns\n\n" + ("- a bullet line that adds up\n" * 80) +
        "\n## Next section\n\nUnrelated.\n"
    )
    t24 = _skill_tree({"bigap": big_ap})
    try:
        f24 = check_anti_patterns(t24)
    finally:
        import shutil as _sh24
        _sh24.rmtree(t24, ignore_errors=True)
    cases.append(("oversized ## Anti-patterns section is flagged by anti-patterns", len(f24) == 1))

    # 25. Small ## Anti-patterns section → clean.
    small_ap = (
        "---\nname: stub\ndescription: short\n---\n\n# Stub\n\n"
        "## Anti-patterns\n\n- one\n- two\n- three\n\n## Next\n\nUnrelated.\n"
    )
    t25 = _skill_tree({"smallap": small_ap})
    try:
        f25 = check_anti_patterns(t25)
    finally:
        import shutil as _sh25
        _sh25.rmtree(t25, ignore_errors=True)
    cases.append(("small ## Anti-patterns section is not flagged by anti-patterns", len(f25) == 0))

    # 26. Reference-stub bullet (not a heading) is never measured by anti-patterns.
    stub_ap = (
        "---\nname: stub\ndescription: short\n---\n\n# Stub\n\n"
        "## Reference index\n\n- `Anti-patterns` — see `reference.md`\n" +
        ("- padding line to exceed the cap if mis-measured\n" * 80)
    )
    t26 = _skill_tree({"stubap": stub_ap})
    try:
        f26 = check_anti_patterns(t26)
    finally:
        import shutil as _sh26
        _sh26.rmtree(t26, ignore_errors=True)
    cases.append(("anti-patterns reference-stub bullet is never measured", len(f26) == 0))

    # ── Consumer-mode regression tests ────────────────────────────────────
    # 27. find_root on a no-flavor root (CLAUDE.md only, no claude-code/ or copilot/)
    #     must return consumer_mode=True and not raise SystemExit.
    tmp_consumer = _tmpmod.mkdtemp()
    try:
        open(_os.path.join(tmp_consumer, "CLAUDE.md"), "w").write("# consumer project\n")
        root_c, mode_c = find_root(tmp_consumer)
        cases.append(("find_root on no-flavor root returns consumer_mode=True",
                       root_c == tmp_consumer and mode_c is True))
    except SystemExit:
        cases.append(("find_root on no-flavor root returns consumer_mode=True", False))
    finally:
        shutil.rmtree(tmp_consumer, ignore_errors=True)

    # 28. find_root on a framework monorepo root (has claude-code/) returns consumer_mode=False.
    tmp_fw = _tmpmod.mkdtemp()
    try:
        open(_os.path.join(tmp_fw, "CLAUDE.md"), "w").write("# framework\n")
        _os.makedirs(_os.path.join(tmp_fw, "claude-code"), exist_ok=True)
        root_fw, mode_fw = find_root(tmp_fw)
        cases.append(("find_root on framework monorepo returns consumer_mode=False",
                       root_fw == tmp_fw and mode_fw is False))
    except SystemExit:
        cases.append(("find_root on framework monorepo returns consumer_mode=False", False))
    finally:
        shutil.rmtree(tmp_fw, ignore_errors=True)

    # ── Noir paradigm strict-gate detect regression ───────────────────────
    # 29. Simulate a Noir paradigm install: the installed grm-release-phase-merge
    #     SKILL.md (sourced from .claude/paradigms/noir/release-phase-merge-SKILL.md)
    #     must contain the strict-gate text so the doc-assurance-strict-gate
    #     feature-manifest detect predicate passes on a Noir consumer.
    tmp_noir = _tmpmod.mkdtemp()
    try:
        # Build a minimal consumer tree with the Noir paradigm source installed
        # as the active skill (simulating grm-work-paradigm-switch Noir output).
        skill_dir = _os.path.join(tmp_noir, ".claude", "skills", "grm-release-phase-merge")
        _os.makedirs(skill_dir, exist_ok=True)
        # Write a file that mimics the Noir paradigm source — must contain the strict-gate text.
        noir_skill_content = (
            "---\nname: release-phase-merge\ndescription: Noir merge skill.\n---\n\n"
            "# Release phase merge (Noir)\n\n"
            "## Per-branch merge procedure\n\n"
            "### 3b. Doc-assurance --strict gate (v3.36+)\n\n"
            "Run `python3 .claude/skills/grm-doc-assurance/doc_assurance.py --strict`.\n"
        )
        open(_os.path.join(skill_dir, "SKILL.md"), "w").write(noir_skill_content)
        # The detect predicate: grep for 'Doc-assurance --strict gate' or '§3b'
        installed = open(_os.path.join(skill_dir, "SKILL.md")).read()
        detect_passes = ("Doc-assurance --strict gate" in installed or "§3b" in installed)
        cases.append(("Noir-installed release-phase-merge SKILL passes strict-gate detect (#171)",
                       detect_passes))
    finally:
        shutil.rmtree(tmp_noir, ignore_errors=True)

    # 30. check_release_consistency on a downstream project missing the
    #     framework release-surface docs must report a finding, not raise
    #     FileNotFoundError (consumer-mode robustness).
    tmp_dl = _tmpmod.mkdtemp()
    try:
        _os.makedirs(_os.path.join(tmp_dl, "docs"), exist_ok=True)
        open(_os.path.join(tmp_dl, "CLAUDE.md"), "w").write("# downstream\n")
        try:
            f30 = check_release_consistency(tmp_dl)
            cases.append(("release-consistency on bare downstream reports, no crash",
                           any("version-history" in x for x in f30)))
        except FileNotFoundError:
            cases.append(("release-consistency on bare downstream reports, no crash", False))
    finally:
        shutil.rmtree(tmp_dl, ignore_errors=True)

    # 30b. VER_RE captures an optional three-part patch component (audit
    # finding, v3.91) instead of silently truncating "v3.87.1" to "3.87".
    _ver_m = VER_RE.match("## v3.87.1 — Patch release")
    cases.append(("VER_RE captures a three-part version whole (not truncated)",
                   _ver_m is not None and _ver_m.group(1) == "3.87.1"))
    _ver_m2 = VER_RE.match("## v3.87 — Two-part release")
    cases.append(("VER_RE still matches a plain two-part version",
                   _ver_m2 is not None and _ver_m2.group(1) == "3.87"))

    # 30c. check_tag_format (audit finding, v3.91): warns on a two-part
    # newest tag, stays silent on a three-part one, never crashes outside a
    # git repo. A real temp git repo (not a fixture string) exercises the
    # actual `git tag --sort` subprocess call.
    tmp_tag = _tmpmod.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmp_tag, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_tag, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_tag, check=True)
        open(_os.path.join(tmp_tag, "f"), "w").write("x")
        subprocess.run(["git", "add", "-A"], cwd=tmp_tag, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_tag, check=True)

        subprocess.run(["git", "tag", "v1.2"], cwd=tmp_tag, check=True)
        f_two = check_tag_format(tmp_tag)
        cases.append(("tag-format warns on a two-part newest tag",
                       len(f_two) == 1 and "v1.2" in f_two[0]))

        subprocess.run(["git", "tag", "v1.2.1"], cwd=tmp_tag, check=True)
        f_three = check_tag_format(tmp_tag)
        cases.append(("tag-format is silent once the newest tag is three-part",
                       f_three == []))
    finally:
        shutil.rmtree(tmp_tag, ignore_errors=True)
    # No git repo / no tags at all -> no crash, no finding.
    tmp_notag = _tmpmod.mkdtemp()
    try:
        cases.append(("tag-format on a non-repo dir does not crash",
                       check_tag_format(tmp_notag) == []))
    finally:
        shutil.rmtree(tmp_notag, ignore_errors=True)

    # ── Documentation portal self-tests (docs-portal-design.md) ─────────
    # Small fixture tree: docs/README.md root + docs/design/foo-design.md,
    # a two-doc corpus exercising nav grouping, link rewriting, and search.
    def _portal_fixture():
        d = _tmpmod.mkdtemp()
        docs = _os.path.join(d, "docs")
        design = _os.path.join(docs, "design")
        _os.makedirs(design, exist_ok=True)
        open(_os.path.join(docs, "README.md"), "w").write(
            "# Docs Root\n\n"
            "- [Foo design](design/foo-design.md)\n\n"
            "<!-- docs-map:begin -->\n<!-- docs-map:end -->\n"
        )
        open(_os.path.join(design, "foo-design.md"), "w").write(
            "> **Up:** [↑ Design index](../README.md)\n\n"
            "# Foo widget design\n\n"
            "## Motivation\n\nBecause widgets need a home for the sprocket logic.\n\n"
            "## Scope\n\n- alpha\n- beta\n\n"
            "## Design\n\nSee the [root](../README.md) for context.\n"
        )
        return d

    # 31. --write-portal generation from a small fixture tree: every doc is
    #     rendered as an <article>, and the root's own link to foo-design.md
    #     is rewritten to an in-page anchor (not left as a relative .md href).
    tmp_p31 = _portal_fixture()
    try:
        html31 = build_portal(tmp_p31)
        has_articles = html31.count("<article") == 2
        foo_slug = _portal_slug(_os.path.join(tmp_p31, "docs"),
                                 _os.path.join(tmp_p31, "docs", "design", "foo-design.md"))
        rewritten = f'href="#{foo_slug}"' in html31
        cases.append(("--write-portal fixture: both docs rendered as articles", has_articles))
        cases.append(("--write-portal fixture: intra-docs .md link rewritten to in-page anchor",
                       rewritten))
    finally:
        shutil.rmtree(tmp_p31, ignore_errors=True)

    # 32. Idempotency: regenerating from the same fixture twice is byte-identical.
    tmp_p32 = _portal_fixture()
    try:
        h1 = build_portal(tmp_p32)
        h2 = build_portal(tmp_p32)
        cases.append(("--write-portal fixture regeneration is byte-identical", h1 == h2))
    finally:
        shutil.rmtree(tmp_p32, ignore_errors=True)

    # 33. Staleness detection: missing portal file is flagged; writing the
    #     current generated content makes check_portal_stale silent.
    tmp_p33 = _portal_fixture()
    try:
        f33_missing = check_portal_stale(tmp_p33)
        cases.append(("check_portal_stale fires when docs/documentation.html is missing",
                       len(f33_missing) == 1 and "missing" in f33_missing[0]))

        portal_path = _os.path.join(tmp_p33, PORTAL_REL_PATH)
        open(portal_path, "w", encoding="utf-8").write(build_portal(tmp_p33))
        f33_fresh = check_portal_stale(tmp_p33)
        cases.append(("check_portal_stale is silent when the portal is current",
                       len(f33_fresh) == 0))

        # Touch a doc after the portal was generated — now stale.
        open(_os.path.join(tmp_p33, "docs", "design", "foo-design.md"), "a").write(
            "\n## Acceptance\n\n- [ ] done\n"
        )
        f33_stale = check_portal_stale(tmp_p33)
        cases.append(("check_portal_stale fires once a doc changes after generation",
                       len(f33_stale) == 1 and "stale" in f33_stale[0]))
    finally:
        shutil.rmtree(tmp_p33, ignore_errors=True)

    # 34. Search-index lookup: the generated JSON index contains an entry for
    #     the fixture's foo-design.md with the expected title and path.
    tmp_p34 = _portal_fixture()
    try:
        html34 = build_portal(tmp_p34)
        m = re.search(
            r'<script id="portal-search-index" type="application/json">(.*?)</script>',
            html34, re.S)
        index = json.loads(m.group(1)) if m else []
        hit = next((e for e in index if e.get("path") == "design/foo-design.md"), None)
        cases.append(("search index contains an entry for design/foo-design.md",
                       hit is not None))
        cases.append(("search index entry title matches the doc's first heading",
                       hit is not None and hit.get("title") == "Foo widget design"))
    finally:
        shutil.rmtree(tmp_p34, ignore_errors=True)

    # ── Design-doc index generation self-tests (maintenance-automation-design.md §1) ──
    def _design_index_fixture():
        """Fixture repo root with docs/design/ (one good doc, one house-layout-
        missing doc) and docs/grimoire/design/ (one good doc), plus a pre-
        existing docs/grimoire/design/README.md carrying curated prose the
        generated region must not disturb."""
        d = _tmpmod.mkdtemp()
        design = _os.path.join(d, "docs", "design")
        gdesign = _os.path.join(d, "docs", "grimoire", "design")
        _os.makedirs(design, exist_ok=True)
        _os.makedirs(gdesign, exist_ok=True)
        open(_os.path.join(design, "alpha-design.md"), "w").write(
            "# Alpha widget\n\n"
            "> **Up:** [↑ Design index](README.md)\n\n"
            "## Motivation\n\n"
            "Alpha needs a home for the sprocket logic.\n\n"
            "## Scope\n\nStuff.\n"
        )
        open(_os.path.join(design, "broken-design.md"), "w").write(
            "No title heading here.\n\n## Scope\n\nStuff, but no Motivation.\n"
        )
        open(_os.path.join(gdesign, "beta-design.md"), "w").write(
            "# Beta gadget\n\n"
            "## Motivation\n\n"
            "Beta closes the loop on gadget provisioning.\n\n"
            "## Scope\n\nStuff.\n"
        )
        open(_os.path.join(gdesign, "README.md"), "w").write(
            "# Grimoire design docs\n\n"
            "## Charter deliverables\n\n"
            "- [beta-design.md](beta-design.md) — hand-curated entry.\n\n"
            "## See also\n\n- [Grimoire index](../README.md)\n"
        )
        return d

    # 35. build_design_index_table: the well-formed doc gets a row; the
    #     house-layout-missing doc is excluded from the table AND reported.
    tmp_d35 = _design_index_fixture()
    try:
        table_lines, findings35 = build_design_index_table(tmp_d35, "docs/design")
        table_text = "\n".join(table_lines)
        cases.append(("well-formed doc gets a table row", "alpha-design.md" in table_text
                      and "Alpha widget" in table_text
                      and "sprocket logic" in table_text))
        cases.append(("house-layout-missing doc excluded from table", "broken-design.md" not in table_text))
        cases.append(("house-layout-missing doc reported as a finding, not silently skipped",
                      any("broken-design.md" in x and "missing house layout" in x for x in findings35)))
    finally:
        shutil.rmtree(tmp_d35, ignore_errors=True)

    # 36. check_design_index_stale(write=True) preserves docs/grimoire/design/
    #     README.md's hand-curated "## Charter deliverables" / "## See also"
    #     prose outside the generated marker region.
    tmp_d36 = _design_index_fixture()
    try:
        check_design_index_stale(tmp_d36, write=True)
        g_readme = open(_os.path.join(tmp_d36, "docs", "grimoire", "design", "README.md")).read()
        cases.append(("generated region markers present after --write-design-index",
                      DESIGN_INDEX_BEGIN in g_readme and DESIGN_INDEX_END in g_readme))
        cases.append(("hand-curated '## Charter deliverables' section survives generation",
                      "## Charter deliverables" in g_readme))
        cases.append(("hand-curated '## See also' section survives generation",
                      "## See also" in g_readme))
        cases.append(("generated table includes beta-design.md inside the markers",
                      "beta-design.md" in g_readme.split(DESIGN_INDEX_BEGIN, 1)[1]))

        d_readme = open(_os.path.join(tmp_d36, "docs", "design", "README.md")).read()
        cases.append(("docs/design/README.md generated with alpha-design.md row",
                      "alpha-design.md" in d_readme))
    finally:
        shutil.rmtree(tmp_d36, ignore_errors=True)

    # 37. Idempotency: a second --write-design-index run is byte-identical
    #     (re-running with no new docs is a no-op).
    tmp_d37 = _design_index_fixture()
    try:
        check_design_index_stale(tmp_d37, write=True)
        g_path = _os.path.join(tmp_d37, "docs", "grimoire", "design", "README.md")
        d_path = _os.path.join(tmp_d37, "docs", "design", "README.md")
        g1, d1 = open(g_path).read(), open(d_path).read()
        check_design_index_stale(tmp_d37, write=True)
        g2, d2 = open(g_path).read(), open(d_path).read()
        cases.append(("re-running --write-design-index with no new docs is byte-identical (grimoire tier)", g1 == g2))
        cases.append(("re-running --write-design-index with no new docs is byte-identical (design tier)", d1 == d2))
    finally:
        shutil.rmtree(tmp_d37, ignore_errors=True)

    # 38. Staleness detection: freshly written README is clean; touching a
    #     doc afterward (without re-running --write-design-index) goes stale;
    #     adding a new doc also surfaces as staleness before the next write.
    tmp_d38 = _design_index_fixture()
    try:
        f38_before_write = check_design_index_stale(tmp_d38, write=False)
        cases.append(("design-index-stale fires before any README is generated",
                      any("missing" in x for x in f38_before_write)))

        check_design_index_stale(tmp_d38, write=True)
        f38_fresh = check_design_index_stale(tmp_d38, write=False)
        # The broken-design.md house-layout finding always surfaces (by design,
        # never silenced); staleness findings about the README itself should not.
        cases.append(("design-index-stale is silent on README staleness once freshly generated",
                      not any("stale" in x for x in f38_fresh)))
        cases.append(("design-index-stale still reports the house-layout-missing doc after a fresh write",
                      any("broken-design.md" in x for x in f38_fresh)))

        # Add a brand-new doc after generation — now stale until re-run.
        open(_os.path.join(tmp_d38, "docs", "design", "gamma-design.md"), "w").write(
            "# Gamma gizmo\n\n## Motivation\n\nGamma rounds out the set.\n"
        )
        f38_stale = check_design_index_stale(tmp_d38, write=False)
        cases.append(("design-index-stale fires once a new doc is added", any("stale" in x for x in f38_stale)))
    finally:
        shutil.rmtree(tmp_d38, ignore_errors=True)

    # 39. check_docs_map: a hand-curated "## Tiers" section linking child
    #     README.md index pages (outside the <!-- docs-map:begin/end --> markers)
    #     must NOT false-positive as a stale entry — docs_md_files() deliberately
    #     excludes README.md from "actual" by design, so a curated link to one is
    #     not staleness. A genuinely missing generated-region entry must still fire.
    d, docs = _setup_tmpdir()
    try:
        _os.makedirs(_os.path.join(docs, "design"), exist_ok=True)
        open(_os.path.join(docs, "design", "README.md"), "w").write("# Design tier\n")
        foo = _os.path.join(docs, "foo.md")
        open(foo, "w").write("# Foo\n")
        readme = _os.path.join(docs, "README.md")
        open(readme, "w").write(
            "# Docs Root\n\n## Tiers\n\n- [design/](design/README.md)\n\n"
            "<!-- docs-map:begin -->\n- [`foo.md`](foo.md)\n<!-- docs-map:end -->\n"
        )
        f39 = check_docs_map(d)
        cases.append(("docs-map: curated README.md tier link is not flagged stale",
                      not any("design/README.md" in x for x in f39)))

        # Control: a genuinely missing generated-region entry still fires.
        open(_os.path.join(docs, "bar.md"), "w").write("# Bar\n")
        f39_missing = check_docs_map(d)
        cases.append(("docs-map: genuinely missing generated entry still fires",
                      any("missing entry: docs/bar.md" in x for x in f39_missing)))
    finally:
        shutil.rmtree(d)

    # 40. mirrored-script-parity: identical root vs claude-code pair passes.
    def _setup_mirror_tmpdir(root_scripts, cc_scripts=None, cp_scripts=None,
                              root_mcp=None, cc_mcp=None):
        """Write a synthetic root with .claude/skills/<sub>/<file> scripts, an
        optional claude-code/.claude/skills mirror, and an optional copilot/scripts/
        flat mirror. Each *_scripts arg maps 'subdir/file.py' (or 'file.py' for
        cp_scripts) -> file content. *_mcp args map
        '<server>/server.py' -> content under .claude/mcp-servers/. Returns the
        temp root path."""
        tmproot = _tmpmod.mkdtemp()
        for relp, content in root_scripts.items():
            fpath = _os.path.join(tmproot, ".claude", "skills", relp)
            _os.makedirs(_os.path.dirname(fpath), exist_ok=True)
            open(fpath, "w").write(content)
        if cc_scripts is not None:
            for relp, content in cc_scripts.items():
                fpath = _os.path.join(tmproot, "claude-code", ".claude", "skills", relp)
                _os.makedirs(_os.path.dirname(fpath), exist_ok=True)
                open(fpath, "w").write(content)
        if cp_scripts is not None:
            cp_dir = _os.path.join(tmproot, "copilot", "scripts")
            _os.makedirs(cp_dir, exist_ok=True)
            for bn, content in cp_scripts.items():
                open(_os.path.join(cp_dir, bn), "w").write(content)
        if root_mcp is not None:
            for relp, content in root_mcp.items():
                fpath = _os.path.join(tmproot, ".claude", "mcp-servers", relp)
                _os.makedirs(_os.path.dirname(fpath), exist_ok=True)
                open(fpath, "w").write(content)
        if cc_mcp is not None:
            for relp, content in cc_mcp.items():
                fpath = _os.path.join(tmproot, "claude-code", ".claude", "mcp-servers", relp)
                _os.makedirs(_os.path.dirname(fpath), exist_ok=True)
                open(fpath, "w").write(content)
        return tmproot

    tmp_m40 = _setup_mirror_tmpdir(
        {"grm-foo/foo.py": "print('hi')\n"},
        {"grm-foo/foo.py": "print('hi')\n"},
    )
    try:
        f40 = check_mirrored_script_parity(tmp_m40, _allow_set=frozenset())
        cases.append(("mirrored-script-parity: identical root/claude-code pair passes",
                      len(f40) == 0))
    finally:
        shutil.rmtree(tmp_m40, ignore_errors=True)

    # 41. Drifted (non-allow-listed) pair fails.
    tmp_m41 = _setup_mirror_tmpdir(
        {"grm-foo/foo.py": "print('hi')\n"},
        {"grm-foo/foo.py": "print('bye')\n"},
    )
    try:
        f41 = check_mirrored_script_parity(tmp_m41, _allow_set=frozenset())
        cases.append(("mirrored-script-parity: drifted pair (not allow-listed) fails",
                      any("foo.py" in x and "claude-code" in x for x in f41)))
    finally:
        shutil.rmtree(tmp_m41, ignore_errors=True)

    # 42. Same drift, now allow-listed → passes.
    tmp_m42 = _setup_mirror_tmpdir(
        {"grm-foo/foo.py": "print('hi')\n"},
        {"grm-foo/foo.py": "print('bye')\n"},
    )
    try:
        f42 = check_mirrored_script_parity(tmp_m42, _allow_set=frozenset({("claude-code", "foo.py")}))
        cases.append(("mirrored-script-parity: allow-listed drifted pair passes",
                      len(f42) == 0))
    finally:
        shutil.rmtree(tmp_m42, ignore_errors=True)

    # 43. Basename-matched root ↔ copilot flat scripts/ pair: drift detected.
    tmp_m43 = _setup_mirror_tmpdir(
        {"grm-bar/bar.py": "X = 1\n"},
        cp_scripts={"bar.py": "X = 2\n"},
    )
    try:
        f43 = check_mirrored_script_parity(tmp_m43, _allow_set=frozenset())
        cases.append(("mirrored-script-parity: drifted root/copilot basename-matched pair fails",
                      any("bar.py" in x and "copilot" in x for x in f43)))
    finally:
        shutil.rmtree(tmp_m43, ignore_errors=True)

    # 44. mcp-servers/** pair drift: identical relative path under
    # .claude/mcp-servers/ is enumerated and compared root vs claude-code, same
    # as skills/hooks.
    tmp_m44 = _setup_mirror_tmpdir(
        {}, root_mcp={"grimoire-status/server.py": "PATH = 'stale'\n"},
        cc_mcp={"grimoire-status/server.py": "PATH = 'grm-agent-status-broker'\n"},
    )
    try:
        f44 = check_mirrored_script_parity(tmp_m44, _allow_set=frozenset())
        cases.append(("mirrored-script-parity: mcp-servers root/claude-code drift detected",
                      any("server.py" in x and "claude-code" in x for x in f44)))
    finally:
        shutil.rmtree(tmp_m44, ignore_errors=True)

    # 44b. dead-allowlist-entry detection: an allow-list entry
    # naming a file that doesn't exist on the claude-code side is itself
    # flagged, even though the (identical) real pair passes.
    tmp_m44b = _setup_mirror_tmpdir(
        {"grm-foo/foo.py": "print('hi')\n"},
        {"grm-foo/foo.py": "print('hi')\n"},
    )
    try:
        f44b = check_mirrored_script_parity(
            tmp_m44b, _allow_set=frozenset({("claude-code", "nonexistent_ghost.py")}))
        cases.append(("mirrored-script-parity: dead allowlist entry detected",
                      any("nonexistent_ghost.py" in x and "dead allowlist" in x
                          for x in f44b)))
    finally:
        shutil.rmtree(tmp_m44b, ignore_errors=True)

    # 45. ported-pair-presence: complete pair set passes.
    def _setup_ported_pair_tmpdir(root_hooks, codex_hooks):
        tmproot = _tmpmod.mkdtemp()
        rh_dir = _os.path.join(tmproot, ".claude", "hooks")
        _os.makedirs(rh_dir, exist_ok=True)
        for bn in root_hooks:
            open(_os.path.join(rh_dir, bn), "w").write("#!/bin/sh\n")
        ch_dir = _os.path.join(tmproot, "codex", ".codex", "hooks")
        _os.makedirs(ch_dir, exist_ok=True)
        for bn in codex_hooks:
            open(_os.path.join(ch_dir, bn), "w").write("#!/usr/bin/env python3\n")
        return tmproot

    tmp_p45 = _setup_ported_pair_tmpdir(
        ["protected-branch-guard.sh", "push-guard.sh", "release-plan-guard.sh",
         "worktree-brief.sh"],
        ["protected-branch-guard.py", "push-guard.py", "release-plan-guard.py",
         "session-start.py"],
    )
    try:
        f45 = check_ported_pair_presence(tmp_p45)
        cases.append(("ported-pair-presence: complete pair set passes", len(f45) == 0))
    finally:
        shutil.rmtree(tmp_p45, ignore_errors=True)

    # 46. ported-pair-presence: codex port missing for a tracked pair fails.
    tmp_p46 = _setup_ported_pair_tmpdir(
        ["protected-branch-guard.sh", "push-guard.sh", "release-plan-guard.sh",
         "worktree-brief.sh"],
        ["protected-branch-guard.py", "push-guard.py", "release-plan-guard.py"],
        # session-start.py (worktree-brief.sh's port) omitted → regression
    )
    try:
        f46 = check_ported_pair_presence(tmp_p46)
        cases.append(("ported-pair-presence: missing codex port detected",
                      any("session-start.py" in x for x in f46)))
    finally:
        shutil.rmtree(tmp_p46, ignore_errors=True)

    # 47. ported-pair-presence: no codex flavor dir at all → no findings
    # (framework-only check skipped outside the monorepo, same as
    # mirrored-script-parity's consumer-mode gate).
    tmp_p47 = _tmpmod.mkdtemp()
    _os.makedirs(_os.path.join(tmp_p47, ".claude", "hooks"), exist_ok=True)
    try:
        f47 = check_ported_pair_presence(tmp_p47)
        cases.append(("ported-pair-presence: no codex dir yields zero findings",
                      len(f47) == 0))
    finally:
        shutil.rmtree(tmp_p47, ignore_errors=True)

    # 47b/47c. server-selftest-parity (#364): fake server.py copies across the
    # three flavor dirs, standing in for the real 5x3 grid.
    def _write_fake_server(path, exit_code):
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(
                "import sys\n"
                "if '--self-test' in sys.argv:\n"
                "    print('fake self-test')\n"
                f"    sys.exit({exit_code})\n"
            )

    _FAKE_SERVER_RELDIRS = (
        _os.path.join(".claude", "mcp-servers", "grimoire-recipe"),
        _os.path.join("claude-code", ".claude", "mcp-servers", "grimoire-recipe"),
        _os.path.join("copilot", "mcp-servers", "grimoire-recipe"),
    )

    # 47b. all three copies exit 0 -> zero findings.
    tmp_s47b = _tmpmod.mkdtemp()
    try:
        for reldir in _FAKE_SERVER_RELDIRS:
            _write_fake_server(_os.path.join(tmp_s47b, reldir, "server.py"), 0)
        f47b = check_server_selftest_parity(tmp_s47b)
        cases.append(("server-selftest-parity: all copies pass -> zero findings",
                      len(f47b) == 0))
    finally:
        shutil.rmtree(tmp_s47b, ignore_errors=True)

    # 47c. the copilot copy exits non-zero -> a finding naming that path.
    tmp_s47c = _tmpmod.mkdtemp()
    try:
        for reldir in _FAKE_SERVER_RELDIRS:
            exit_code = 1 if reldir.startswith("copilot") else 0
            _write_fake_server(_os.path.join(tmp_s47c, reldir, "server.py"), exit_code)
        f47c = check_server_selftest_parity(tmp_s47c)
        cases.append(("server-selftest-parity: failing copy detected",
                      any("copilot" in x and "grimoire-recipe" in x for x in f47c)))
    finally:
        shutil.rmtree(tmp_s47c, ignore_errors=True)

    # 48. product-readme-present: no README.md at all -> finding.
    tmp_r48 = _tmpmod.mkdtemp()
    try:
        f48 = check_product_readme_present(tmp_r48)
        cases.append(("product-readme-present: missing README flagged",
                      any("missing" in x for x in f48)))
    finally:
        shutil.rmtree(tmp_r48, ignore_errors=True)

    # 49. product-readme-present: unmodified scaffold README -> finding.
    tmp_r49 = _tmpmod.mkdtemp()
    try:
        open(_os.path.join(tmp_r49, "README.md"), "w").write(
            "# Claude Code Scaffold\n\nA starter kit.\n\n## What's included\n\nStuff.\n"
        )
        f49 = check_product_readme_present(tmp_r49)
        cases.append(("product-readme-present: scaffold README flagged",
                      any("scaffold README" in x for x in f49)))
    finally:
        shutil.rmtree(tmp_r49, ignore_errors=True)

    # 50. product-readme-present: real product README -> no findings.
    tmp_r50 = _tmpmod.mkdtemp()
    try:
        open(_os.path.join(tmp_r50, "README.md"), "w").write(
            "# Goon Cave\n\nA dungeon-crawler roguelike.\n"
        )
        f50 = check_product_readme_present(tmp_r50)
        cases.append(("product-readme-present: real product README passes",
                      len(f50) == 0))
    finally:
        shutil.rmtree(tmp_r50, ignore_errors=True)

    # 51. version-claim-freshness: stale version claim in README flagged.
    tmp_v51 = _tmpmod.mkdtemp()
    try:
        _os.makedirs(_os.path.join(tmp_v51, ".claude"), exist_ok=True)
        _json_mod = __import__("json")
        with open(_os.path.join(tmp_v51, ".claude", "grimoire-config.json"), "w") as fh:
            _json_mod.dump({"framework-version": "v3.79"}, fh)
        open(_os.path.join(tmp_v51, "README.md"), "w").write(
            "# Familiar\n\nAt v1.87 this app does X.\n"
        )
        f51 = check_version_claim_freshness(tmp_v51)
        cases.append(("version-claim-freshness: stale claim flagged",
                      any("v1.87" in x and "v3.79" in x for x in f51)))
    finally:
        shutil.rmtree(tmp_v51, ignore_errors=True)

    # 52. version-claim-freshness: current version claim -> no findings.
    tmp_v52 = _tmpmod.mkdtemp()
    try:
        _os.makedirs(_os.path.join(tmp_v52, ".claude"), exist_ok=True)
        with open(_os.path.join(tmp_v52, ".claude", "grimoire-config.json"), "w") as fh:
            _json_mod.dump({"framework-version": "v3.79"}, fh)
        open(_os.path.join(tmp_v52, "README.md"), "w").write(
            "# Familiar\n\nCurrently at v3.79.\n"
        )
        f52 = check_version_claim_freshness(tmp_v52)
        cases.append(("version-claim-freshness: current claim passes", len(f52) == 0))
    finally:
        shutil.rmtree(tmp_v52, ignore_errors=True)

    # 53. version-claim-freshness: code-fenced version example is not flagged.
    tmp_v53 = _tmpmod.mkdtemp()
    try:
        _os.makedirs(_os.path.join(tmp_v53, ".claude"), exist_ok=True)
        with open(_os.path.join(tmp_v53, ".claude", "grimoire-config.json"), "w") as fh:
            _json_mod.dump({"framework-version": "v3.79"}, fh)
        open(_os.path.join(tmp_v53, "README.md"), "w").write(
            "# Familiar\n\n```\nexample: v0.1\n```\n"
        )
        f53 = check_version_claim_freshness(tmp_v53)
        cases.append(("version-claim-freshness: fenced example not flagged", len(f53) == 0))
    finally:
        shutil.rmtree(tmp_v53, ignore_errors=True)

    # 53a. version-claim-freshness (#416): a stale vX.Y claim in a design
    # doc's own heading/opening paragraph outside the README/CHANGELOG class
    # is WARN-tier — flagged, but tagged distinctly from the blocking class
    # findings (matches the check_for_checks "(WARN — ...)" convention).
    tmp_v53a = _tmpmod.mkdtemp()
    try:
        _os.makedirs(_os.path.join(tmp_v53a, ".claude"), exist_ok=True)
        with open(_os.path.join(tmp_v53a, ".claude", "grimoire-config.json"), "w") as fh:
            _json_mod.dump({"framework-version": "v3.79"}, fh)
        _os.makedirs(_os.path.join(tmp_v53a, "docs", "design"), exist_ok=True)
        open(_os.path.join(tmp_v53a, "docs", "design", "foo-design.md"), "w").write(
            "# Foo subsystem (as of v1.87)\n\n"
            "This document describes the foo subsystem as of v1.87.\n"
        )
        f53a = check_version_claim_freshness(tmp_v53a)
        cases.append((
            "version-claim-freshness: fleet doc stale claim WARNs",
            any("v1.87" in x and "v3.79" in x and "(WARN" in x for x in f53a)))
    finally:
        shutil.rmtree(tmp_v53a, ignore_errors=True)

    # 53b. version-claim-freshness (#416): the "kept for lineage" marker
    # excludes an otherwise-stale doc from the fleet-wide WARN check.
    tmp_v53b = _tmpmod.mkdtemp()
    try:
        _os.makedirs(_os.path.join(tmp_v53b, ".claude"), exist_ok=True)
        with open(_os.path.join(tmp_v53b, ".claude", "grimoire-config.json"), "w") as fh:
            _json_mod.dump({"framework-version": "v3.79"}, fh)
        _os.makedirs(_os.path.join(tmp_v53b, "docs", "design"), exist_ok=True)
        open(_os.path.join(tmp_v53b, "docs", "design", "bar-design.md"), "w").write(
            "# Bar subsystem (as of v1.87)\n\n"
            "This document describes the bar subsystem as of v1.87 "
            "(kept for lineage).\n"
        )
        f53b = check_version_claim_freshness(tmp_v53b)
        cases.append((
            "version-claim-freshness: 'kept for lineage' marker excludes doc",
            len(f53b) == 0))
    finally:
        shutil.rmtree(tmp_v53b, ignore_errors=True)

    # 53c. version-claim-freshness (#416): the existing README/CHANGELOG
    # blocking finding's text is unchanged (no "(WARN" tag) even when a
    # docs/ tree with unrelated fleet content is present alongside it.
    tmp_v53c = _tmpmod.mkdtemp()
    try:
        _os.makedirs(_os.path.join(tmp_v53c, ".claude"), exist_ok=True)
        with open(_os.path.join(tmp_v53c, ".claude", "grimoire-config.json"), "w") as fh:
            _json_mod.dump({"framework-version": "v3.79"}, fh)
        open(_os.path.join(tmp_v53c, "README.md"), "w").write(
            "# Familiar\n\nAt v1.87 this app does X.\n"
        )
        _os.makedirs(_os.path.join(tmp_v53c, "docs", "design"), exist_ok=True)
        open(_os.path.join(tmp_v53c, "docs", "design", "baz-design.md"), "w").write(
            "# Baz subsystem (as of v1.87)\n\nAs of v1.87 baz does X.\n"
        )
        f53c = check_version_claim_freshness(tmp_v53c)
        class_findings = [x for x in f53c if x.startswith("README.md:")]
        warn_findings = [x for x in f53c if "(WARN" in x]
        cases.append((
            "version-claim-freshness: README blocking finding text unchanged",
            len(class_findings) == 1
            and "(WARN" not in class_findings[0]
            and class_findings[0] == (
                "README.md: newest version claim v1.87 is a major version "
                "behind current v3.79 — remove hardcoded version claims "
                "from prose and link docs/version-history.md instead")
        ))
        cases.append((
            "version-claim-freshness: fleet WARN finding also present alongside class finding",
            len(warn_findings) == 1))
    finally:
        shutil.rmtree(tmp_v53c, ignore_errors=True)

    # 54. classifier-compat: bypass-shaped flag defined by a plain skill
    # script is flagged (the --allow-ahead-shaped regression this check exists
    # to catch).
    tmp_c54 = _tmpmod.mkdtemp()
    try:
        skill_dir = _os.path.join(tmp_c54, ".claude", "skills", "grm-fake")
        _os.makedirs(skill_dir, exist_ok=True)
        open(_os.path.join(skill_dir, "fake.sh"), "w").write(
            "#!/usr/bin/env bash\ncase \"$1\" in\n"
            "  --allow-ahead) ALLOW_AHEAD=1 ;;\nesac\n"
        )
        f54 = check_classifier_compat(tmp_c54)
        cases.append(("classifier-compat: bypass-shaped flag flagged",
                      any("--allow-ahead" in x and "bypass-shaped" in x for x in f54)))
    finally:
        shutil.rmtree(tmp_c54, ignore_errors=True)

    # 55. classifier-compat: raw destructive op in a plain skill script is
    # flagged; the same op inside a .claude/hooks/ guard script is not.
    tmp_c55 = _tmpmod.mkdtemp()
    try:
        skill_dir = _os.path.join(tmp_c55, ".claude", "skills", "grm-fake")
        hooks_dir = _os.path.join(tmp_c55, ".claude", "hooks")
        _os.makedirs(skill_dir, exist_ok=True)
        _os.makedirs(hooks_dir, exist_ok=True)
        open(_os.path.join(skill_dir, "fake.py"), "w").write(
            "import subprocess\nsubprocess.run('git push --force', shell=True)\n"
        )
        open(_os.path.join(hooks_dir, "some-guard.sh"), "w").write(
            "#!/usr/bin/env bash\ngit push --force\n"
        )
        f55 = check_classifier_compat(tmp_c55)
        cases.append(("classifier-compat: raw destructive op in plain skill script flagged",
                      any("fake.py" in x and "raw destructive op" in x for x in f55)))
        cases.append(("classifier-compat: same op inside .claude/hooks/ not flagged",
                      not any("some-guard.sh" in x for x in f55)))
    finally:
        shutil.rmtree(tmp_c55, ignore_errors=True)

    # 56. classifier-compat: clean script (no bypass flags, no destructive
    # ops) yields zero findings.
    tmp_c56 = _tmpmod.mkdtemp()
    try:
        skill_dir = _os.path.join(tmp_c56, ".claude", "skills", "grm-fake")
        _os.makedirs(skill_dir, exist_ok=True)
        open(_os.path.join(skill_dir, "fake.py"), "w").write(
            "import argparse\nap = argparse.ArgumentParser()\n"
            "ap.add_argument('--dry-run', action='store_true')\n"
        )
        f56 = check_classifier_compat(tmp_c56)
        cases.append(("classifier-compat: clean script yields no findings", len(f56) == 0))
    finally:
        shutil.rmtree(tmp_c56, ignore_errors=True)

    # 57. skill-placeholder-tokens (#465): a raw {test-command} left inside a
    # fenced bash code block in an installed SKILL.md is flagged.
    tmp_s54 = _tmpmod.mkdtemp()
    try:
        skill_dir = _os.path.join(tmp_s54, ".claude", "skills", "grm-fake-merge")
        _os.makedirs(skill_dir, exist_ok=True)
        open(_os.path.join(skill_dir, "SKILL.md"), "w").write(
            "---\nname: grm-fake-merge\ndescription: fake.\n---\n\n"
            "### 3. Run tests\n\n```bash\n{test-command}\n```\n"
        )
        f54 = check_skill_placeholder_tokens(tmp_s54)
        cases.append(("skill-placeholder-tokens: {test-command} in a fenced code block is flagged",
                      any("test-command" in x and "grm-fake-merge/SKILL.md" in x for x in f54)))
    finally:
        shutil.rmtree(tmp_s54, ignore_errors=True)

    # 58. skill-placeholder-tokens: a raw {build-command} inside a `- [ ]`
    # checklist line (not in a fenced block) is also flagged.
    tmp_s55 = _tmpmod.mkdtemp()
    try:
        skill_dir = _os.path.join(tmp_s55, ".claude", "skills", "grm-fake-merge")
        _os.makedirs(skill_dir, exist_ok=True)
        open(_os.path.join(skill_dir, "SKILL.md"), "w").write(
            "---\nname: grm-fake-merge\ndescription: fake.\n---\n\n"
            "Pre-merge checklist:\n\n- [ ] `{build-command}` clean\n"
        )
        f55 = check_skill_placeholder_tokens(tmp_s55)
        cases.append(("skill-placeholder-tokens: {build-command} in a checklist line is flagged",
                      any("build-command" in x for x in f55)))
    finally:
        shutil.rmtree(tmp_s55, ignore_errors=True)

    # 59. skill-placeholder-tokens: the fixed form (recipe.py dispatcher call,
    # no raw token) is silent — the regression guard passes on fixed content.
    tmp_s56 = _tmpmod.mkdtemp()
    try:
        skill_dir = _os.path.join(tmp_s56, ".claude", "skills", "grm-fake-merge")
        _os.makedirs(skill_dir, exist_ok=True)
        open(_os.path.join(skill_dir, "SKILL.md"), "w").write(
            "---\nname: grm-fake-merge\ndescription: fake.\n---\n\n"
            "### 3. Run tests\n\n"
            "```bash\npython3 .claude/skills/grm-build-recipe/recipe.py test\n```\n\n"
            "- [ ] `python3 .claude/skills/grm-build-recipe/recipe.py build` clean\n"
        )
        f56 = check_skill_placeholder_tokens(tmp_s56)
        cases.append(("skill-placeholder-tokens: fixed recipe.py-dispatcher form is silent",
                      len(f56) == 0))
    finally:
        shutil.rmtree(tmp_s56, ignore_errors=True)

    # 60. skill-placeholder-tokens: a token merely *discussed* as prose
    # (outside any fenced block / checklist line) is NOT flagged — a
    # meta/authoring skill documenting the genericization mechanism itself
    # (e.g. grm-hard-reset, grm-sync-from-source) is not a bug.
    tmp_s57 = _tmpmod.mkdtemp()
    try:
        skill_dir = _os.path.join(tmp_s57, ".claude", "skills", "grm-fake-meta")
        _os.makedirs(skill_dir, exist_ok=True)
        open(_os.path.join(skill_dir, "SKILL.md"), "w").write(
            "---\nname: grm-fake-meta\ndescription: fake.\n---\n\n"
            "New files arrive generic (placeholder-laden). Re-specialize them "
            "for this project — fill `{test-command}`, `{build-command}`, etc.\n"
        )
        f57 = check_skill_placeholder_tokens(tmp_s57)
        cases.append(("skill-placeholder-tokens: prose mention outside code/checklist is not flagged",
                      len(f57) == 0))
    finally:
        shutil.rmtree(tmp_s57, ignore_errors=True)

    # 61-65. Baseline ratchet (#426, v3.93): seed / diff / ratchet-shrink /
    # new-finding regression, all against a throwaway baseline file.
    tmp_s61 = _tmpmod.mkdtemp()
    try:
        bpath = _os.path.join(tmp_s61, "baseline.json")

        # 61. No baseline file yet -> SEED, never reports anything as new.
        seed = apply_baseline(bpath, {"hierarchy": ["orphan: a.md", "orphan: b.md"]})
        cases.append(("baseline: first run seeds the file and reports 0 new",
                      seed["seeded"] and seed["new"] == [] and _os.path.exists(bpath)))

        # 62. Identical findings on the next run -> 0 new, no ratchet.
        same = apply_baseline(bpath, {"hierarchy": ["orphan: a.md", "orphan: b.md"]})
        cases.append(("baseline: unchanged findings report 0 new",
                      same["new"] == [] and not same["ratcheted"]))

        # 63. A finding gets fixed (subset of baseline) -> ratchet shrinks,
        # baseline file rewritten to the smaller set.
        shrunk = apply_baseline(bpath, {"hierarchy": ["orphan: a.md"]})
        reloaded = _load_baseline(bpath)
        cases.append(("baseline: resolved finding ratchets the baseline down",
                      shrunk["ratcheted"] and reloaded == {"hierarchy: orphan: a.md"}))

        # 64. A genuinely NEW finding (not in the now-ratcheted baseline) is
        # reported as new — this is the regression signal --strict gates on.
        grown = apply_baseline(bpath, {"hierarchy": ["orphan: a.md", "orphan: c.md"]})
        cases.append(("baseline: a finding absent from baseline is NEW",
                      grown["new"] == ["hierarchy: orphan: c.md"]))

        # 65. Baseline file is NOT rewritten while a new finding is present —
        # the regression (and ratchet point) must stay visible next run.
        cases.append(("baseline: file untouched while a new finding exists",
                      _load_baseline(bpath) == {"hierarchy: orphan: a.md"}))
    finally:
        shutil.rmtree(tmp_s61, ignore_errors=True)

    # 66-70. skill-budget (#399): must scan every shipped flavor, not just
    # root — a skill that bloats past budget only in claude-code/ (the
    # canonical, shipped copy) is exactly the case the pre-#399 code missed.
    tmp_s66 = _tmpmod.mkdtemp()
    try:
        def _mk_skill_md(flavor_root, name, size):
            """Write a SKILL.md of exactly `size` bytes (content is irrelevant —
            check_skill_budget only measures os.path.getsize)."""
            d = _os.path.join(flavor_root, ".claude", "skills", name)
            _os.makedirs(d, exist_ok=True)
            with open(_os.path.join(d, "SKILL.md"), "wb") as fh:
                fh.write(b"x" * size)

        # root has one clean skill; claude-code has one over-budget skill.
        _mk_skill_md(tmp_s66, "root-clean", 1000)
        _mk_skill_md(_os.path.join(tmp_s66, "claude-code"), "cc-bloated", SKILL_BUDGET + 500)
        _mk_skill_md(_os.path.join(tmp_s66, "claude-code"), "cc-clean", 1000)
        f66 = check_skill_budget(tmp_s66)
        cases.append(("skill-budget: over-budget SKILL.md in claude-code/ is caught",
                      any("claude-code/.claude/skills/cc-bloated/SKILL.md" in x for x in f66)))
        cases.append(("skill-budget: clean claude-code skill not flagged",
                      not any("cc-clean" in x for x in f66)))
        cases.append(("skill-budget: clean root skill not flagged",
                      not any("root-clean" in x for x in f66)))
        cases.append(("skill-budget: exactly one finding for the one bloated skill",
                      len(f66) == 1))
    finally:
        shutil.rmtree(tmp_s66, ignore_errors=True)

    # 71-74. check-for-checks (#440, v3.97): doctrine meta-check — a
    # MUST-clause section with no paired deterministic-check reference is
    # flagged; a MUST-clause section that references a check (grm- skill,
    # recipe.py, or a Testable-criterion table) is silent. Covers both
    # scanned targets: docs/coding-standards.md and the required-feature
    # catalog.
    tmp_cfc1 = _tmpmod.mkdtemp()
    try:
        _os.makedirs(_os.path.join(tmp_cfc1, "docs"), exist_ok=True)
        open(_os.path.join(tmp_cfc1, "docs", "coding-standards.md"), "w").write(
            "# Coding Standards\n\n"
            "## Unchecked rule\n\n"
            "Every module MUST carry a one-line summary comment.\n\n"
            "## Checked rule\n\n"
            "Every recipe MUST expose `build`/`run`/`deploy`. Enforced by "
            "`grm-install-doctor`.\n"
        )
        f_cfc1 = check_for_checks(tmp_cfc1)
        cases.append(("check-for-checks: coding-standards.md MUST-clause with no "
                      "check reference is flagged",
                      any("Unchecked rule" in x for x in f_cfc1)))
        cases.append(("check-for-checks: coding-standards.md MUST-clause paired "
                      "with a grm- skill reference is silent",
                      not any("Checked rule" in x for x in f_cfc1)))
    finally:
        shutil.rmtree(tmp_cfc1, ignore_errors=True)

    tmp_cfc2 = _tmpmod.mkdtemp()
    try:
        catalog_dir = _os.path.join(tmp_cfc2, ".claude", "skills",
                                     "grm-required-feature-catalog")
        _os.makedirs(catalog_dir, exist_ok=True)
        open(_os.path.join(catalog_dir, "required-feature-catalog.md"), "w").write(
            "# Required-feature catalog\n\n"
            "### Entry 1 — Unchecked Mandate\n\n"
            "**Spec.** Every Grimoire web app MUST do the unchecked thing.\n\n"
            "### Entry 2 — Checked Mandate\n\n"
            "**Spec.** Every Grimoire web app MUST do the checked thing.\n\n"
            "| ID | Requirement | Testable criterion |\n"
            "|----|-------------|--------------------|\n"
            "| X-1 | does the thing | a probe confirms it |\n"
        )
        f_cfc2 = check_for_checks(tmp_cfc2)
        cases.append(("check-for-checks: catalog Entry with no Testable-criterion "
                      "table or check reference is flagged",
                      any("Unchecked Mandate" in x for x in f_cfc2)))
        cases.append(("check-for-checks: catalog Entry with a Testable-criterion "
                      "table is silent",
                      not any("Checked Mandate" in x for x in f_cfc2)))
    finally:
        shutil.rmtree(tmp_cfc2, ignore_errors=True)

    # 75-90. design-doc-purity (#358, v3.98): each of the five pollution
    # patterns gets a compliant + non-compliant fixture pair, plus the
    # allowlist mechanism, the README-index exclusion, and the recursive
    # subdirectory scan.
    def _purity_tree(root_dir, tier="docs/design", fname="foo-design.md",
                      content="# Foo\n\n## Motivation\nWhy.\n"):
        d = _os.path.join(root_dir, tier)
        _os.makedirs(d, exist_ok=True)
        open(_os.path.join(d, fname), "w").write(content)

    # 75/76. Status line — flagged in the opening ~10 lines, clean without it.
    tmp_p75 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p75, content="# Foo\n\n> **Status**: shipped\n\n## Motivation\nWhy.\n")
        f75 = check_design_doc_purity(tmp_p75)
        cases.append(("design-doc-purity: Status: line in opening lines flagged",
                      any("Status" in x and "foo-design.md" in x for x in f75)))
    finally:
        shutil.rmtree(tmp_p75, ignore_errors=True)

    tmp_p76 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p76)
        f76 = check_design_doc_purity(tmp_p76)
        cases.append(("design-doc-purity: doc with no Status: line passes", len(f76) == 0))
    finally:
        shutil.rmtree(tmp_p76, ignore_errors=True)

    # 77/78. Checked box — flagged anywhere in the doc, clean when unchecked.
    tmp_p77 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p77, content=(
            "# Foo\n\n## Motivation\nWhy.\n\n## Acceptance\n- [x] done thing\n"
        ))
        f77 = check_design_doc_purity(tmp_p77)
        cases.append(("design-doc-purity: checked '- [x]' box flagged",
                      any("checked" in x and "foo-design.md" in x for x in f77)))
    finally:
        shutil.rmtree(tmp_p77, ignore_errors=True)

    tmp_p78 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p78, content=(
            "# Foo\n\n## Motivation\nWhy.\n\n## Acceptance\n- [ ] pending thing\n"
        ))
        f78 = check_design_doc_purity(tmp_p78)
        cases.append(("design-doc-purity: unchecked '- [ ]' box passes", len(f78) == 0))
    finally:
        shutil.rmtree(tmp_p78, ignore_errors=True)

    # 79/80. Release-narration phrasing — flagged in prose, not inside a
    # fenced code example.
    tmp_p79 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p79, content=(
            "# Foo\n\n## Motivation\nThis feature shipped in v3.42 and is "
            "Implemented (#123).\n"
        ))
        f79 = check_design_doc_purity(tmp_p79)
        cases.append(("design-doc-purity: release-narration phrasing flagged",
                      any("release-narration" in x for x in f79)))
    finally:
        shutil.rmtree(tmp_p79, ignore_errors=True)

    tmp_p80 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p80, content=(
            "# Foo\n\n## Motivation\nExample output:\n\n```\nshipped in v3.42\n```\n"
        ))
        f80 = check_design_doc_purity(tmp_p80)
        cases.append(("design-doc-purity: fenced-code narration example not flagged",
                      len(f80) == 0))
    finally:
        shutil.rmtree(tmp_p80, ignore_errors=True)

    # 80a/80b. The bare word "delivered" used as ordinary prose (not a
    # release-status marker) must NOT be flagged; a standalone "Delivered"
    # status marker (bolded, at a bullet/line end) must be.
    tmp_p80a = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p80a, content=(
            "# Foo\n\n## Motivation\nA browser-delivered web app is "
            "delivered mechanically at session start.\n"
        ))
        f80a = check_design_doc_purity(tmp_p80a)
        cases.append(("design-doc-purity: 'delivered' used as ordinary prose not flagged",
                      len(f80a) == 0))
    finally:
        shutil.rmtree(tmp_p80a, ignore_errors=True)

    tmp_p80b = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p80b, content=(
            "# Foo\n\n## Motivation\nWhy.\n\n## Follow-ups\n"
            "- ~~Second instance of the thing~~ **Delivered**\n"
        ))
        f80b = check_design_doc_purity(tmp_p80b)
        cases.append(("design-doc-purity: standalone 'Delivered' status marker flagged",
                      any("release-narration" in x for x in f80b)))
    finally:
        shutil.rmtree(tmp_p80b, ignore_errors=True)

    # 81/82. Work-item-map / "Phase N closed" heading — flagged; an ordinary
    # heading passes.
    tmp_p81 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p81, content=(
            "# Foo\n\n## Motivation\nWhy.\n\n## File-level changes\n- a.py\n"
        ))
        f81 = check_design_doc_purity(tmp_p81)
        cases.append(("design-doc-purity: work-item-map heading flagged",
                      any("work-item-map" in x for x in f81)))
    finally:
        shutil.rmtree(tmp_p81, ignore_errors=True)

    tmp_p81b = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p81b, content=(
            "# Foo\n\n## Motivation\nWhy.\n\n## Phase 2 closed\nDetails.\n"
        ))
        f81b = check_design_doc_purity(tmp_p81b)
        cases.append(("design-doc-purity: 'Phase N closed' heading flagged",
                      any("work-item-map" in x for x in f81b)))
    finally:
        shutil.rmtree(tmp_p81b, ignore_errors=True)

    tmp_p82 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p82, content=(
            "# Foo\n\n## Motivation\nWhy.\n\n## Design\nHow it works.\n"
        ))
        f82 = check_design_doc_purity(tmp_p82)
        cases.append(("design-doc-purity: ordinary heading passes", len(f82) == 0))
    finally:
        shutil.rmtree(tmp_p82, ignore_errors=True)

    # 83/84. Filename pattern — *-plan.md / *-candidates.md under a design
    # tier flagged; an ordinary *-design.md filename passes.
    tmp_p83 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p83, fname="rollout-plan.md",
                      content="# Rollout\n\n## Motivation\nWhy.\n")
        f83 = check_design_doc_purity(tmp_p83)
        cases.append(("design-doc-purity: *-plan.md filename flagged",
                      any("filename" in x and "rollout-plan.md" in x for x in f83)))
    finally:
        shutil.rmtree(tmp_p83, ignore_errors=True)

    tmp_p83b = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p83b, fname="workflow-candidates.md",
                      content="# Workflow candidates\n\n## Motivation\nWhy.\n")
        f83b = check_design_doc_purity(tmp_p83b)
        cases.append(("design-doc-purity: *-candidates.md filename flagged",
                      any("filename" in x and "workflow-candidates.md" in x for x in f83b)))
    finally:
        shutil.rmtree(tmp_p83b, ignore_errors=True)

    tmp_p84 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p84, fname="foo-design.md")
        f84 = check_design_doc_purity(tmp_p84)
        cases.append(("design-doc-purity: ordinary *-design.md filename passes", len(f84) == 0))
    finally:
        shutil.rmtree(tmp_p84, ignore_errors=True)

    # 85. The allowlist exempts a specific path from every pattern (passed
    # via _allow_set, mirroring check_flavor_parity/check_mirrored_script_parity's
    # test-override convention — no need to monkey-patch the module global).
    tmp_p85 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p85, content=(
            "# Foo\n\n> Status: shipped\n\n## Motivation\nWhy.\n\n"
            "## Acceptance\n- [x] done\n"
        ))
        f85 = check_design_doc_purity(tmp_p85, _allow_set={"docs/design/foo-design.md"})
        cases.append(("design-doc-purity: DESIGN_PURITY_ALLOW exempts a listed path",
                      len(f85) == 0))
    finally:
        shutil.rmtree(tmp_p85, ignore_errors=True)

    # 86. README.md index pages are excluded from the scan even when they
    # contain pollution-shaped content.
    tmp_p86 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p86, fname="README.md", content=(
            "# Design docs\n\n> Status: shipped\n\n- [x] done\n"
        ))
        f86 = check_design_doc_purity(tmp_p86)
        cases.append(("design-doc-purity: README.md index page excluded from scan",
                      len(f86) == 0))
    finally:
        shutil.rmtree(tmp_p86, ignore_errors=True)

    # 87. Recursive scan reaches a design-tier subdirectory (docs/design/ux/).
    tmp_p87 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p87, tier="docs/design/ux", fname="ux-design.md",
                      content="# UX\n\n> Status: shipped\n\n## Motivation\nWhy.\n")
        f87 = check_design_doc_purity(tmp_p87)
        cases.append(("design-doc-purity: subdirectory (docs/design/ux/) scanned",
                      any("ux-design.md" in x for x in f87)))
    finally:
        shutil.rmtree(tmp_p87, ignore_errors=True)

    # 88. Both design tiers (docs/design/ and docs/grimoire/design/) are scanned.
    tmp_p88 = _tmpmod.mkdtemp()
    try:
        _purity_tree(tmp_p88, tier="docs/grimoire/design", fname="bar-design.md",
                      content="# Bar\n\n> Status: shipped\n\n## Motivation\nWhy.\n")
        f88 = check_design_doc_purity(tmp_p88)
        cases.append(("design-doc-purity: docs/grimoire/design/ tier scanned",
                      any("bar-design.md" in x for x in f88)))
    finally:
        shutil.rmtree(tmp_p88, ignore_errors=True)

    lines, passed, failed = [], 0, 0
    for label, ok in cases:
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    return passed, failed, lines


def main() -> None:
    args = sys.argv[1:]
    if "--self-test" in args:
        passed, failed, lines = self_test()
        for ln in lines:
            print(ln)
        print(f"\ndoc-assurance self-test: {passed} passed, {failed} failed.")
        sys.exit(1 if failed else 0)
    strict = "--strict" in args
    write = "--write-map" in args
    write_portal = "--write-portal" in args
    write_design_index = "--write-design-index" in args
    baseline_arg = None
    if "--baseline" in args:
        idx = args.index("--baseline")
        has_value = idx + 1 < len(args) and not args[idx + 1].startswith("--")
        baseline_arg = args[idx + 1] if has_value else BASELINE_DEFAULT_REL
    if "--root" in args:
        idx = args.index("--root")
        root = os.path.abspath(args[idx + 1])
        has_cc = os.path.isdir(os.path.join(root, "claude-code"))
        has_cp = os.path.isdir(os.path.join(root, "copilot"))
        consumer_mode = not has_cc and not has_cp
    else:
        root, consumer_mode = find_root(".")
    if consumer_mode:
        print("doc-assurance: consumer-mode (no flavor dirs detected) — "
              "flavor-parity, manifest-detect-hygiene, shipped-pointers, "
              "mirrored-script-parity, ported-pair-presence, "
              "orchestrate-band-present, server-selftest-parity skipped.")

    # Determine dial value for check 7 + 8 (relative-links and hierarchy).
    # --strict escalates warn->block, but an explicit 'off' stays off — a project
    # may exempt its own dogfood docs from the shipped-flavor wiki-conformance.
    dial = _read_hierarchy_dial(root)
    if strict and dial != "off":
        dial = "block"

    # Checks that require the framework monorepo layout (claude-code/ or copilot/
    # flavor dirs present).  Skipped with a notice in consumer-mode.
    _FRAMEWORK_ONLY_CHECKS = frozenset({"flavor-parity", "manifest-detect-hygiene",
                                         "shipped-pointers", "mirrored-script-parity",
                                         "ported-pair-presence", "orchestrate-band-present",
                                         "server-selftest-parity"})

    named = [a for a in args if a in CHECKS] or CHECKS
    total = 0
    hierarchy_findings_count = 0
    all_findings = {}
    for c in named:
        if consumer_mode and c in _FRAMEWORK_ONLY_CHECKS:
            print(f"[{c}] skipped (consumer-mode)")
            continue
        if c == "flavor-parity":         f = check_flavor_parity(root)
        elif c == "design-layout":       f = check_design_layout(root)
        elif c == "links":               f = check_links(root)
        elif c == "docs-map":            f = check_docs_map(root, write=write)
        elif c == "release-consistency": f = check_release_consistency(root)
        elif c == "tag-format":          f = check_tag_format(root)
        elif c == "manifest-detect-hygiene": f = check_manifest_detect_hygiene(root)
        elif c == "skill-placeholder-tokens": f = check_skill_placeholder_tokens(root)
        elif c == "check-for-checks":     f = check_for_checks(root)
        elif c == "design-doc-purity":    f = check_design_doc_purity(root)
        elif c == "shipped-pointers":    f = check_shipped_pointers(root)
        elif c == "mirrored-script-parity": f = check_mirrored_script_parity(root)
        elif c == "ported-pair-presence": f = check_ported_pair_presence(root)
        elif c == "orchestrate-band-present": f = check_orchestrate_band_present(root)
        elif c == "server-selftest-parity": f = check_server_selftest_parity(root)
        elif c == "skill-budget":        f = check_skill_budget(root)
        elif c == "lean-index":          f = check_lean_index(root)
        elif c == "monolith-cap":        f = check_monolith_cap(root)
        elif c == "description-cap":     f = check_description_cap(root)
        elif c == "anti-patterns":       f = check_anti_patterns(root)
        elif c == "product-readme-present": f = check_product_readme_present(root)
        elif c == "version-claim-freshness": f = check_version_claim_freshness(root)
        elif c == "classifier-compat":    f = check_classifier_compat(root)
        elif c == "portal-stale":
            if write_portal:
                portal_path = os.path.join(root, PORTAL_REL_PATH)
                open(portal_path, "w", encoding="utf-8").write(build_portal(root))
                f = []
            else:
                f = check_portal_stale(root)
        elif c == "design-index-stale":
            f = check_design_index_stale(root, write=write_design_index)
        elif c == "relative-links":
            if dial == "off":
                print(f"[{c}] skipped (dial=off)")
                continue
            docs_dir = os.path.join(root, "docs")
            f = check_relative_links(docs_dir, root)
            hierarchy_findings_count += len(f)
        elif c == "hierarchy":
            if dial == "off":
                print(f"[{c}] skipped (dial=off)")
                continue
            docs_dir = os.path.join(root, "docs")
            f = check_hierarchy(docs_dir)
            hierarchy_findings_count += len(f)
        else:
            f = []
        status = "OK" if not f else f"{len(f)} finding(s)"
        print(f"[{c}] {status}")
        for x in f:
            print(f"   - {x}")
        total += len(f)
        all_findings[c] = f
    print(f"\ndoc-assurance: {total} finding(s) across {len(named)} check(s).")

    # Baseline ratchet (#426, v3.93): print the trend, and — when active —
    # this REPLACES the raw total-based --strict gate below with a
    # new-findings-only gate (findings already in the baseline don't newly
    # fail anything they weren't already failing).
    trend = None
    if baseline_arg is not None:
        baseline_path = (baseline_arg if os.path.isabs(baseline_arg)
                          else os.path.join(root, baseline_arg))
        trend = apply_baseline(baseline_path, all_findings)
        print(trend["report"])

    # Exit logic: --strict (or dial=block) causes non-zero on any finding.
    # For dial=block, only hierarchy findings block; other checks obey strict.
    if dial == "block" and hierarchy_findings_count:
        sys.exit(1)
    elif trend is not None:
        if strict and trend["new"]:
            sys.exit(1)
    elif strict and total:
        sys.exit(1)


if __name__ == "__main__":
    main()
