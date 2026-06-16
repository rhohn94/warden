#!/usr/bin/env python3
"""doc-assurance — eight deterministic checks over a Grimoire repo's own docs.

Checks: flavor-parity, design-layout, links, docs-map, release-consistency,
        skill-budget, relative-links, hierarchy.
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
"""
import os, re, sys, json, glob

CHECKS = ["flavor-parity", "design-layout", "links", "docs-map",
          "release-consistency", "skill-budget", "relative-links", "hierarchy"]

# v1.29 context-efficiency budgets (bytes).
SKILL_BUDGET = 12_000
CLAUDE_BUDGET = 10_000

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


def find_root(start):
    d = os.path.abspath(start)
    while d != "/":
        if os.path.exists(os.path.join(d, "CLAUDE.md")) and os.path.isdir(os.path.join(d, "claude-code")):
            return d
        d = os.path.dirname(d)
    raise SystemExit("repo root not found (need CLAUDE.md + claude-code/)")


def rel(root, p):
    return os.path.relpath(p, root)


# ── Check 1: flavor parity ──────────────────────────────────────────────
def check_flavor_parity(root):
    findings = []
    # presence parity: every claude-code skill dir exists at root, and vice-versa
    cc_skills = {os.path.basename(os.path.dirname(p))
                 for p in glob.glob(f"{root}/claude-code/.claude/skills/*/SKILL.md")}
    rt_skills = {os.path.basename(os.path.dirname(p))
                 for p in glob.glob(f"{root}/.claude/skills/*/SKILL.md")}
    for s in sorted(cc_skills - rt_skills):
        findings.append(f"skill present in claude-code but not root: {s}")
    for s in sorted(rt_skills - cc_skills):
        findings.append(f"skill present in root but not claude-code: {s}")
    # content parity for an allow-listed must-match set
    must_match = ["docs/coding-standards.md",
                  ".claude/skills/sync-from-upstream/feature-manifest.md"]
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
    findings = []
    for p in sorted(glob.glob(f"{root}/docs/design/*-design.md")):
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
            "> Generated + validated by `doc-assurance` (check `docs-map`). Lists every",
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
    vh = open(f"{root}/docs/version-history.md").read()
    rm = open(f"{root}/docs/roadmap.md").read()
    hist = set(VER_RE.findall(vh))
    # roadmap shipped versions: a vX.Y section whose body says Shipped/released
    shipped = set()
    for m in re.finditer(r"^##\s+v(\d+\.\d+)(.*?)(?=^##\s+v|\Z)", rm, re.S | re.M):
        if re.search(r"shipped|released", m.group(2), re.I):
            shipped.add(m.group(1))
    for v in sorted(hist - shipped, key=lambda s: tuple(map(int, s.split(".")))):
        findings.append(f"v{v} in version-history but not marked Shipped in roadmap")
    # manifest-version monotonic int + framework-version >= newest shipped
    mani = open(f"{root}/.claude/skills/sync-from-upstream/feature-manifest.md").read()
    mv = re.search(r"manifest-version:\s*(\d+)", mani)
    if not mv:
        findings.append("feature-manifest.md: no integer manifest-version")
    cfg = json.load(open(f"{root}/.claude/grimoire-config.json"))
    fw = cfg.get("framework-version", "").lstrip("v")
    if hist:
        newest = max(hist, key=lambda s: tuple(map(int, s.split("."))))
        if fw and tuple(map(int, fw.split("."))) < tuple(map(int, newest.split("."))):
            findings.append(f"framework-version {fw} < newest shipped v{newest}")
    return findings


# ── Check 6: skill / always-loaded size budget (v1.29, #55/#56) ─────────
def check_skill_budget(root):
    findings = []
    for p in sorted(glob.glob(f"{root}/.claude/skills/*/SKILL.md")):
        n = os.path.getsize(p)
        if n > SKILL_BUDGET:
            findings.append(f"{rel(root,p)}: {n} bytes > {SKILL_BUDGET} budget "
                            f"(split a lean head + reference.md)")
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
    """In-memory unit tests for check_design_layout's dual-pattern logic
    and the new check_relative_links / check_hierarchy functions.

    Covers: legacy-passing doc, house-template-passing doc, doc satisfying
    both, doc satisfying neither, and the open-questions marker rule.
    Also covers: absolute internal link, missing breadcrumb, valid breadcrumb,
    bare-prose doc ref, and hierarchy orphan.
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
        # Monkey-patch glob so the check sees only our file.
        import glob as _glob
        orig = _glob.glob
        _glob.glob = lambda pat, **kw: [path] if pat.endswith("*-design.md") else orig(pat, **kw)
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
    root = find_root(".")
    if "--root" in args:
        root = os.path.abspath(args[args.index("--root") + 1])

    # Determine dial value for check 7 + 8 (relative-links and hierarchy).
    # --strict overrides to 'block' regardless of config.
    dial = _read_hierarchy_dial(root)
    if strict:
        dial = "block"

    named = [a for a in args if a in CHECKS] or CHECKS
    total = 0
    hierarchy_findings_count = 0
    for c in named:
        if c == "flavor-parity":     f = check_flavor_parity(root)
        elif c == "design-layout":   f = check_design_layout(root)
        elif c == "links":           f = check_links(root)
        elif c == "docs-map":        f = check_docs_map(root, write=write)
        elif c == "release-consistency": f = check_release_consistency(root)
        elif c == "skill-budget":    f = check_skill_budget(root)
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
