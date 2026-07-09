#!/usr/bin/env python3
"""doc-assurance — deterministic checks over a Grimoire repo's own docs.

Checks: flavor-parity, design-layout, links, docs-map, release-consistency,
        skill-budget, relative-links, hierarchy, lean-index, monolith-cap,
        description-cap, anti-patterns.
Read-only except --write-map. Report-only unless --strict (non-zero on findings).

Usage:
  doc_assurance.py [check ...] [--strict] [--write-map] [--root PATH]
  (no checks named ⇒ run all)

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
"""
import os, re, sys, json, glob

CHECKS = [
    "flavor-parity", "design-layout", "links", "docs-map",
    "release-consistency", "manifest-detect-hygiene", "shipped-pointers",
    "skill-budget", "relative-links", "hierarchy", "lean-index",
    "monolith-cap", "description-cap", "anti-patterns",
]

# Mirror of build_distributables.py EXCLUDED_PATH_PREFIXES + sync-from-upstream.sh
# is_excluded() — the v3.39 "Bulkhead" framework-internal doc carve-out. A path
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
    "docs/grimoire/version-design.md",
    "docs/grimoire/qa-ledger.md",
    "docs/grimoire/execution-profile-spike-s1.md",
    "docs/grimoire/token-efficiency-",
    "docs/grimoire/release-planning-",
    # v3.45: release-planning docs relocated to a dedicated tier (active at dir
    # root, archive under archived/). Old prefixes kept for backward-compat.
    "docs/release-planning/",
)

# v1.29 context-efficiency budgets (bytes).
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
    ("root", "claude-code", "docs/web-app-aura-adoption-guide.md"),
    ("root", "claude-code", "docs/web-app-deployment-protocol.md"),
    ("root", "copilot", "docs/version-history.md"),
    ("root", "copilot", "docs/web-app-aura-adoption-guide.md"),
    ("root", "copilot", "docs/web-app-deployment-protocol.md"),

    # ── docs/release-planning/ tier — root-only (v3.45 relocation) ─────
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

    # ── codex flavor (v3.55) ───────────────────────────────────────────
    # codex was ported from claude-code; its docs/ tree mirrors copilot's
    # file-set EXACTLY plus one added design doc (docs/design/codex-flavor-
    # design.md). So every gap copilot has versus root / claude-code, codex
    # shares (same shipped subset), and codex additionally carries the design
    # doc the other three flavors lack. These tuples encode exactly those
    # intentional gaps; tuple direction matches _is_docs_gap_allowed(fa, fb)
    # (fa = the flavor that HAS the file).
    #
    # codex carries the extra flavor design doc the others don't:
    ("codex", "root",        "docs/design/codex-flavor-design.md"),
    ("codex", "claude-code", "docs/design/codex-flavor-design.md"),
    ("codex", "copilot",     "docs/design/codex-flavor-design.md"),
    # root-only files codex lacks (mirrors the root↔copilot gaps above):
    ("root", "codex", "docs/design.md"),
    ("root", "codex", "docs/design/ux/components.md"),
    ("root", "codex", "docs/design/ux/theme.md"),
    ("root", "codex", "docs/release-planning/README.md"),
    ("root", "codex", "docs/release-planning/archived/README.md"),
    ("root", "codex", "docs/version-history.md"),
    ("root", "codex", "docs/web-app-aura-adoption-guide.md"),
    ("root", "codex", "docs/web-app-deployment-protocol.md"),
    # claude-code ships ux components/theme that codex (like copilot) lacks:
    ("claude-code", "codex", "docs/design/ux/components.md"),
    ("claude-code", "codex", "docs/design/ux/theme.md"),
})

# Release-planning archive pattern: root-only, auto-matched by regex.
# v3.45 "Release-planning relocation" moved all plans into a dedicated
# docs/release-planning/ tier (active at dir root, archive under archived/).
# The pattern matches the new tier AND the two pre-v3.45 locations (top-level
# docs/ active + docs/grimoire/ archive) for backward-compat, so a synced-but-
# not-yet-migrated consumer never flags a parity / monolith gap.
_RELEASE_PLAN_RE = re.compile(r"^docs/(release-planning/(archived/)?|grimoire/)?release-planning-v[\d.]+\.md$")

# ── lean-index (check 9) constants ─────────────────────────────────────
LEAN_INDEX_SIZE_CAP  = 6_144   # 6 KB — individual index page budget
LEAN_INDEX_MIN_LINKS = 3       # minimum markdown links in an index page
# Size-cap exempt: aggregating multi-tier index pages that must list many
# sub-docs to be useful.  Link-density rule still applies.
LEAN_INDEX_SIZE_EXEMPT = frozenset({
    "docs/README.md",           # repo-root doc map (many tiers)
    "docs/design/README.md",    # design-doc catalog (all design docs)
    # v3.39 "Bulkhead": framework design-spec catalog. Root indexes the full
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
    # v3.39 "Bulkhead": these framework specs relocated docs/design/ →
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
    "docs/grimoire/design/onboarding-design.md",
    "docs/grimoire/design/token-efficiency-design.md",
    "docs/grimoire/design/ux-design-language-design.md",
    "docs/grimoire/design/ux-enhancements-design.md",
    "docs/grimoire/design/work-paradigm-design.md",
    "docs/grimoire/design/write-capable-workflow-design.md",
    "docs/grimoire/docs-organization-design.md",
    "docs/grimoire/integration-workflow.md",
    # v3.54: comprehensive framework docs exempted (legitimate large docs;
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


def find_root(start):
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


def rel(root, p):
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
def check_flavor_parity(root, _allow_set=None):
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

    # ── codex flavor (v3.55): ported from claude-code, docs mirror copilot's
    # set plus docs/design/codex-flavor-design.md. Guarded by codex/ presence
    # (a consumer or a pre-v3.55 monorepo has no codex/ dir — skip then, like
    # the copilot_root guard above). When present, compare codex against every
    # other flavor so four-flavor docs parity is enforced.
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

def check_design_layout(root):
    # v3.39 "Bulkhead": design docs live at BOTH a consumer's project-own tier
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


# ── Check 3: link integrity ─────────────────────────────────────────────
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
def _strip_code(text):
    # Links inside fenced or inline code are examples, not real references.
    text = FENCE_RE.sub("", text)
    return INLINE_CODE_RE.sub("", text)
def check_links(root):
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


# ── Check: shipped-pointers (v3.41 "Clean-Room" CR-2) ───────────────────
# Pointer-integrity rule (clean-room-design.md §4, extending v3.39's CRITICAL
# invariant): "No shipped doc may contain a relative link to an excluded or
# relocated doc." A shipped doc linking a target that never ships would dangle
# in a consumer install. We reuse the build gate's EXCLUDED_PATH_PREFIXES as the
# single exclusion source — no second hardcoded copy (clean-room-design §4).
_SHIPPED_FLAVORS = ("", "claude-code", "codex", "copilot")  # "" == repo-root (the dogfood flavor)

# Exclude-and-seed targets (clean-room-design §4 / v3.39 §2): excluded at the
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


def check_shipped_pointers(root):
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
            # (v3.45) the relocated docs/release-planning/** tier. Links *from*
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
def docs_md_files(root):
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


def build_map(root):
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


def check_docs_map(root, write=False):
    mp = f"{root}/docs/README.md"
    if write:
        new_content = build_map(root)
        open(mp, "w").write(new_content)
        return []
    findings = []
    if not os.path.exists(mp):
        return ["docs/README.md (documentation map) missing — run with --write-map"]
    content = open(mp).read()
    listed = set(re.findall(r"\]\(([^)]+\.md)\)", content))
    listed = {os.path.normpath(os.path.join("docs", x)) for x in listed}
    actual = set(docs_md_files(root))
    for f in sorted(actual - listed):
        findings.append(f"docs map missing entry: {f}")
    for f in sorted(listed - actual):
        findings.append(f"docs map stale entry (no file): {f}")
    return findings


# ── Check 5: release consistency ────────────────────────────────────────
VER_RE = re.compile(r"^##\s+v(\d+\.\d+)", re.M)
def check_release_consistency(root):
    findings = []
    # The release-consistency surface (version-history, roadmap, feature-manifest,
    # config) is framework-owned and seeded by bootstrap.  A downstream project
    # that has not adopted one of these files should report it as a finding, not
    # crash with an unhandled FileNotFoundError (#183 consumer-mode robustness).
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
    for m in re.finditer(r"^##\s+v(\d+\.\d+)(.*?)(?=^##\s+v|\Z)", rm, re.S | re.M):
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


# ── Check 5b: feature-manifest detect-predicate hygiene (#135) ──────────
def check_manifest_detect_hygiene(root):
    """A feature-manifest DETECT predicate must reference only artifacts that
    are actually distributed to a consumer (skills, scripts, config). It must
    not depend on a sync/build-excluded framework-internal doc (the v3.39
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


# ── Check 6: skill / always-loaded size budget (v1.29, #55/#56) ─────────
def check_skill_budget(root):
    findings = []
    for p in sorted(glob.glob(f"{root}/.claude/skills/*/SKILL.md")):
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


def check_relative_links(docs_dir, repo_root):
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


def check_hierarchy(docs_dir):
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

    # Collect all docs/**/*.md files
    all_docs = set()
    for p in glob.glob(f"{docs_dir}/**/*.md", recursive=True):
        if "/.git/" not in p:
            all_docs.add(os.path.normpath(p))

    # 1. Reachability: BFS/DFS from docs/README.md following relative .md links
    reachable = set()
    queue = [os.path.normpath(root_readme)]
    reachable.add(os.path.normpath(root_readme))
    while queue:
        current = queue.pop()
        try:
            content = open(current).read()
        except Exception:
            continue
        stripped = _strip_code(content)
        base = os.path.dirname(current)
        for m in _LINK_TARGET_RE.finditer(stripped):
            t = m.group(1).strip()
            if t.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path_part = t.split("#", 1)[0].split("?", 1)[0]
            if not path_part or not path_part.endswith(".md"):
                continue
            target = os.path.normpath(os.path.join(base, path_part))
            if os.path.exists(target) and target not in reachable:
                reachable.add(target)
                queue.append(target)

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


# ── Check 9: lean-index ─────────────────────────────────────────────────
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")

def check_lean_index(root):
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
def check_monolith_cap(root):
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


def check_description_cap(root):
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


def check_anti_patterns(root):
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


# ── Self-test ────────────────────────────────────────────────────────────
def self_test():
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

    # ── Consumer-mode regression tests (#149/#155/#164/#167) ─────────────
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

    # ── Noir paradigm strict-gate detect regression (#171) ────────────────
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
    #     FileNotFoundError (#183 consumer-mode robustness).
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

    lines, passed, failed = [], 0, 0
    for label, ok in cases:
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    return passed, failed, lines


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        passed, failed, lines = self_test()
        for ln in lines:
            print(ln)
        print(f"\ndoc-assurance self-test: {passed} passed, {failed} failed.")
        sys.exit(1 if failed else 0)
    strict = "--strict" in args
    write = "--write-map" in args
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
              "flavor-parity, manifest-detect-hygiene, shipped-pointers skipped.")

    # Determine dial value for check 7 + 8 (relative-links and hierarchy).
    # --strict escalates warn->block, but an explicit 'off' stays off — a project
    # may exempt its own dogfood docs from the shipped-flavor wiki-conformance.
    dial = _read_hierarchy_dial(root)
    if strict and dial != "off":
        dial = "block"

    # Checks that require the framework monorepo layout (claude-code/ or copilot/
    # flavor dirs present).  Skipped with a notice in consumer-mode.
    _FRAMEWORK_ONLY_CHECKS = frozenset({"flavor-parity", "manifest-detect-hygiene", "shipped-pointers"})

    named = [a for a in args if a in CHECKS] or CHECKS
    total = 0
    hierarchy_findings_count = 0
    for c in named:
        if consumer_mode and c in _FRAMEWORK_ONLY_CHECKS:
            print(f"[{c}] skipped (consumer-mode)")
            continue
        if c == "flavor-parity":         f = check_flavor_parity(root)
        elif c == "design-layout":       f = check_design_layout(root)
        elif c == "links":               f = check_links(root)
        elif c == "docs-map":            f = check_docs_map(root, write=write)
        elif c == "release-consistency": f = check_release_consistency(root)
        elif c == "manifest-detect-hygiene": f = check_manifest_detect_hygiene(root)
        elif c == "shipped-pointers":    f = check_shipped_pointers(root)
        elif c == "skill-budget":        f = check_skill_budget(root)
        elif c == "lean-index":          f = check_lean_index(root)
        elif c == "monolith-cap":        f = check_monolith_cap(root)
        elif c == "description-cap":     f = check_description_cap(root)
        elif c == "anti-patterns":       f = check_anti_patterns(root)
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
    print(f"\ndoc-assurance: {total} finding(s) across {len(named)} check(s).")
    # Exit logic: --strict (or dial=block) causes non-zero on any finding.
    # For dial=block, only hierarchy findings block; other checks obey strict.
    if dial == "block" and hierarchy_findings_count:
        sys.exit(1)
    elif strict and total:
        sys.exit(1)


if __name__ == "__main__":
    main()
