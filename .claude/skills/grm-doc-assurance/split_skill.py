#!/usr/bin/env python3
"""split_skill — deterministic lean-head / reference.md splitter for skills.

Automates the v3.21 lean-head convention (see
`docs/grimoire/design/token-efficiency-enforcement-design.md` §"The lean-head /
reference.md convention"): moves named `##`/`###` sections VERBATIM out of a
skill's SKILL.md into a sibling reference.md, leaving a lean head plus a
`## Reference (load on demand)` index. Byte-for-byte content-preserving;
idempotent; mirrors to claude-code/ when that flavor exists.

Usage:
  split_skill.py <skill-name> --move "<heading>" ["<heading>" ...] [--root .]
  split_skill.py --self-test

  <heading> matches the exact heading TEXT (with or without leading '#'s),
  e.g. --move "## Anti-patterns" or --move "Anti-patterns".

Behaviour
---------
A section = a `##`/`###` heading line and everything until the next heading of
the **same-or-higher** level (a moved `##` carries its nested `###` children).
Moved sections are appended to reference.md in original document order,
byte-for-byte. The head keeps frontmatter + all non-moved sections in order and
gains a `## Reference (load on demand)` section listing each moved heading.

Idempotent: re-running with the same args when already split is a no-op
(detected via an existing `## Reference (load on demand)` + reference.md).
Re-running with additional `--move` headings extends reference.md.

Writes atomically (temp + os.replace). Errors out writing nothing if a heading
is missing or ambiguous.
"""
import os, re, sys, tempfile

REFERENCE_HEADING = "Reference (load on demand)"
HEADING_RE = re.compile(r"^(#{2,6})[ \t]+(.*?)[ \t]*$")


# ── Section model ────────────────────────────────────────────────────────
class Section:
    """One heading and its body, sliced byte-faithfully from the source.

    `level` is the count of leading '#' (2 for ##, 3 for ###). `text` is the
    heading text with surrounding whitespace stripped. `raw` is the exact
    substring of the document this section occupies, preserving newlines. A flat
    `Section` (from `_parse_sections`) runs to the next heading of *any* level;
    a move-unit `Section` (from `_fold_unit`) runs to the next *same-or-higher*
    heading, folding its deeper children in.
    """

    def __init__(self, level, text, raw):
        self.level = level
        self.text = text
        self.raw = raw


def _split_frontmatter(src):
    """Return (frontmatter, body). Frontmatter is the leading '---\\n…\\n---\\n'
    block (verbatim, including its trailing newline) or "" when absent."""
    if not src.startswith("---\n") and not src.startswith("---\r\n"):
        return "", src
    # Find the closing fence on its own line after the opener.
    m = re.search(r"^---[ \t]*\r?\n(.*?\r?\n)?---[ \t]*\r?\n", src, re.S)
    if not m or m.start() != 0:
        return "", src
    return src[: m.end()], src[m.end():]


def _parse_sections(body):
    """Split *body* into (preamble, [Section, …]) — one flat Section per
    heading, sliced to the **next heading of any level**.

    `preamble` is everything before the first heading (kept in the head,
    untouched). Sections tile *body* exactly: each `raw` runs from its heading
    line up to (not including) the next heading line, so the raws never overlap.
    `_fold_unit` then folds a matched section's deeper-level followers back in
    to form a move-unit. (Slicing here to the next *same-or-higher* heading
    would double-count children once they are re-folded, and would stop a
    flat `###` from being addressed independently of its parent `##`.)
    """
    lines = body.splitlines(keepends=True)
    # Index of heading lines with their (level, text).
    heads = []
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line.rstrip("\r\n"))
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    if not heads:
        return body, []
    preamble = "".join(lines[: heads[0][0]])
    sections = []
    for h_idx, (line_no, level, text) in enumerate(heads):
        # Section ends at the very next heading of ANY level (or EOF).
        end_line = heads[h_idx + 1][0] if h_idx + 1 < len(heads) else len(lines)
        raw = "".join(lines[line_no:end_line])
        sections.append(Section(level, text, raw))
    return preamble, sections


def _norm_heading(h):
    """Normalize a CLI/heading string to bare heading text for matching."""
    return h.lstrip("#").strip()


def _fold_unit(flat, start):
    """From the flat section list, fold section *start* together with its
    strictly-deeper followers into one move-unit Section, stopping at the next
    same-or-higher heading.

    Returns (Section, end) where `end` is the first flat index NOT part of the
    unit. A `###` therefore stops at the next `###`/`##` (siblings and the
    parent `##` stay behind); a moved `##` still carries its nested `###`/`####`
    children. `.raw` is the byte-faithful concatenation of the consumed flats.
    """
    base = flat[start].level
    raw = flat[start].raw
    j = start + 1
    while j < len(flat) and flat[j].level > base:
        raw += flat[j].raw
        j += 1
    return Section(base, flat[start].text, raw), j


def _resolve_moves(flat_sections, move_headings):
    """Resolve each requested heading against the *flat* section list and fold
    it into a same-or-higher move-unit.

    Resolving over the flat list (where every `##` and `###` is individually
    addressable) is what lets a `###` be moved out from under its parent `##`:
    only the `###` and its own deeper children travel, while the parent and
    sibling sections stay in the head. A moved `##` still carries its nested
    children via `_fold_unit`.

    Returns (resolved, moved_flat_idx, errors): `resolved` is the ordered list
    of folded move-unit Sections (document order, de-duplicated); `moved_flat_idx`
    is the set of flat indices consumed by those units, used to rebuild the kept
    head byte-faithfully. The Reference index is never matchable — `plan_split`
    strips it from the flat list before calling.
    """
    errors = []
    # Build text → list[flat index]; later matches resolve over every heading.
    by_text = {}
    for idx, sec in enumerate(flat_sections):
        by_text.setdefault(sec.text, []).append(idx)
    chosen_starts = set()
    for h in move_headings:
        want = _norm_heading(h)
        matches = by_text.get(want, [])
        if not matches:
            errors.append(f"heading not found: {want!r}")
        elif len(matches) > 1:
            errors.append(f"heading is ambiguous ({len(matches)} matches): {want!r}")
        else:
            chosen_starts.add(matches[0])
    resolved = []
    moved_flat_idx = set()
    for start in sorted(chosen_starts):
        unit, end = _fold_unit(flat_sections, start)
        resolved.append(unit)
        moved_flat_idx.update(range(start, end))
    return resolved, moved_flat_idx, errors


def _build_reference(skill_title, moved_raws, existing=None):
    """Return reference.md content: a 2-line title + moved sections verbatim.

    When *existing* (current reference.md text) is given, the moved sections
    are appended after the existing body (extend mode), preserving everything
    already there byte-for-byte.
    """
    title = (
        f"# {skill_title} — reference\n"
        f"Loaded on demand by `SKILL.md`.\n"
    )
    appended = "".join(moved_raws)
    if existing is None:
        body = title + "\n" + appended
    else:
        # Preserve existing content exactly; ensure a separating newline.
        sep = "" if existing.endswith("\n") else "\n"
        body = existing + sep + appended
    return body


def _reference_index_block(move_texts):
    """Build the `## Reference (load on demand)` head section listing moves."""
    lines = ["## Reference (load on demand)", ""]
    for t in move_texts:
        lines.append(f"- `{t}` — see `reference.md`")
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path, content):
    """Write *content* to *path* atomically (temp in same dir + os.replace)."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".split_skill.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _skill_title(name):
    """Human title for a skill: 'cost-budget' → 'Cost-budget'."""
    return name[:1].upper() + name[1:] if name else name


def _head_has_reference_index(head):
    """True when the head already carries a `## Reference (load on demand)`."""
    return any(
        _norm_heading(m.group(2)) == REFERENCE_HEADING
        for m in (HEADING_RE.match(ln) for ln in head.splitlines())
        if m
    )


def plan_split(src, skill_title, move_headings, existing_reference=None):
    """Compute the new (head, reference) pair for a SKILL.md *src*.

    Returns a dict:
      {"head": str, "reference": str, "moved": [text,…],
       "before": len(src), "after": len(head)}
    Raises ValueError (writing nothing) on a missing/ambiguous heading.

    Idempotent: if every requested heading is already absent from the head AND
    the head already has the Reference index AND each heading is present in the
    existing reference, the call is a byte-identical no-op (head == src,
    reference == existing).
    """
    frontmatter, body = _split_frontmatter(src)
    preamble, flat = _parse_sections(body)
    # Strip any pre-existing Reference index (heading + its body) from the flat
    # working list so a re-run regenerates a single, current index — and so the
    # index itself is never matchable/movable. Matching + the kept-head rebuild
    # both run over this flat list, which keeps every `##`/`###` individually
    # addressable (a `###` can be moved out from under its parent `##`).
    flat_no_index = [s for s in flat
                     if _norm_heading(s.text) != REFERENCE_HEADING]
    had_index = len(flat_no_index) != len(flat)

    present_texts = {s.text for s in flat_no_index}
    wanted = [_norm_heading(h) for h in move_headings]

    # ── Idempotent no-op detection ─────────────────────────────────────────
    if had_index and existing_reference is not None:
        all_absent = all(w not in present_texts for w in wanted)
        all_in_ref = all(("# " + w in existing_reference) or
                         ("## " + w in existing_reference) or
                         ("### " + w in existing_reference)
                         for w in wanted)
        if all_absent and all_in_ref:
            return {
                "head": src,
                "reference": existing_reference,
                "moved": [],
                "before": len(src),
                "after": len(src),
                "noop": True,
            }

    resolved, moved_flat_idx, errors = _resolve_moves(flat_no_index, move_headings)
    if errors:
        raise ValueError("; ".join(errors))

    moved_texts = [s.text for s in resolved]
    moved_raws = [s.raw for s in resolved]

    # ── Rebuild the head: frontmatter + preamble + kept flats + index ──────
    # Keep every flat section not consumed by a move-unit, in document order.
    # When a `###` is moved out from under a `##`, the parent's flat raw and the
    # sibling flats survive here untouched — only the moved `###`'s own flats are
    # dropped — so the parent heading and its other children stay in the head.
    kept_raws = [s.raw for i, s in enumerate(flat_no_index)
                 if i not in moved_flat_idx]
    head_parts = [frontmatter, preamble]
    head_parts.extend(kept_raws)
    head_body = "".join(head_parts)
    # Ensure a blank line before the appended index for readability.
    if head_body and not head_body.endswith("\n"):
        head_body += "\n"
    if head_body and not head_body.endswith("\n\n"):
        head_body += "\n"
    # Merge with any prior moved headings recorded in an existing reference so
    # the index lists the full set (extend mode), in reference order.
    index_texts = list(moved_texts)
    if had_index and existing_reference is not None:
        prior = _existing_reference_headings(existing_reference)
        for t in prior:
            if t not in index_texts:
                index_texts.insert(len(index_texts) - len(moved_texts), t)
    head = head_body + _reference_index_block(index_texts)
    if not head.endswith("\n"):
        head += "\n"

    reference = _build_reference(skill_title, moved_raws, existing=existing_reference)

    return {
        "head": head,
        "reference": reference,
        "moved": moved_texts,
        "before": len(src),
        "after": len(head),
        "noop": False,
    }


def _existing_reference_headings(ref_text):
    """Top-level (## / ###) heading texts already present in reference.md,
    excluding the leading '# <Skill> — reference' title."""
    out = []
    for ln in ref_text.splitlines():
        m = HEADING_RE.match(ln)
        if m:
            out.append(m.group(2).strip())
    return out


# ── Driver ───────────────────────────────────────────────────────────────
def _skill_paths(root, skill, flavor_prefix=""):
    base = os.path.join(root, flavor_prefix, ".claude", "skills", skill)
    return os.path.join(base, "SKILL.md"), os.path.join(base, "reference.md")


def run(root, skill, move_headings):
    """Split a real skill's SKILL.md in `root` (and mirror to claude-code/).

    Returns 0 on success, non-zero on error. Prints before/after head bytes for
    each flavor plus a budget verdict.
    """
    SKILL_BUDGET = 12_000
    skill_md, ref_md = _skill_paths(root, skill)
    if not os.path.exists(skill_md):
        print(f"error: not found: {skill_md}")
        return 2
    src = open(skill_md).read()
    existing_ref = open(ref_md).read() if os.path.exists(ref_md) else None
    title = _skill_title(skill)
    try:
        plan = plan_split(src, title, move_headings, existing_reference=existing_ref)
    except ValueError as e:
        print(f"error: {e} (nothing written)")
        return 2

    if plan.get("noop"):
        print(f"[{skill}] already split for these headings — no-op "
              f"(head {plan['before']} B unchanged).")
        # Still report the budget verdict for the unchanged head.
        _print_budget(skill, plan["before"], SKILL_BUDGET)
        return 0

    _atomic_write(skill_md, plan["head"])
    _atomic_write(ref_md, plan["reference"])
    print(f"[{skill}] root: SKILL.md {plan['before']} B → {plan['after']} B "
          f"(moved {len(plan['moved'])} section(s) → reference.md "
          f"{len(plan['reference'])} B)")

    # ── Mirror to claude-code/ when that flavor of the skill exists ────────
    cc_skill_md, cc_ref_md = _skill_paths(root, skill, flavor_prefix="claude-code")
    if os.path.exists(cc_skill_md):
        _atomic_write(cc_skill_md, plan["head"])
        _atomic_write(cc_ref_md, plan["reference"])
        print(f"[{skill}] claude-code: SKILL.md {len(src)} B → {plan['after']} B "
              f"(mirrored byte-for-byte)")
    else:
        print(f"[{skill}] claude-code: no sibling skill — mirror skipped")

    _print_budget(skill, plan["after"], SKILL_BUDGET)
    return 0


def _print_budget(skill, head_bytes, budget):
    if head_bytes < budget:
        print(f"[{skill}] budget: {head_bytes} B < {budget} → PASS")
    else:
        over = head_bytes - budget
        print(f"[{skill}] budget: {head_bytes} B ≥ {budget} → "
              f"safe partial — still {over} over")


# ── Self-test ────────────────────────────────────────────────────────────
def self_test():
    """Fixture-driven tests for the splitter. Returns (passed, failed, lines)."""
    import shutil

    cases = []

    def check(label, ok):
        cases.append((label, bool(ok)))

    tmp = tempfile.mkdtemp(prefix="split_skill_test_")
    try:
        skill = "fixture"
        sdir = os.path.join(tmp, ".claude", "skills", skill)
        os.makedirs(sdir)
        skill_md = os.path.join(sdir, "SKILL.md")
        ref_md = os.path.join(sdir, "reference.md")

        fixture = (
            "---\n"
            "name: fixture\n"
            "description: A fixture skill for the splitter self-test.\n"
            "---\n"
            "\n"
            "# Fixture\n"
            "\n"
            "Intro paragraph that stays in the head.\n"
            "\n"
            "## Procedure\n"
            "\n"
            "1. Do the thing.\n"
            "2. Do the next thing.\n"
            "\n"
            "### Sub-step\n"
            "\n"
            "A nested step that belongs to Procedure.\n"
            "\n"
            "## Anti-patterns\n"
            "\n"
            "- Never hand-roll the math.\n"
            "- Never skip the gate.\n"
            "\n"
            "## Examples\n"
            "\n"
            "A big examples block.\n"
            + ("x" * 4000) + "\n"
        )
        open(skill_md, "w").write(fixture)

        # ── Split out Anti-patterns + Examples ─────────────────────────────
        rc = run(tmp, skill, ["## Anti-patterns", "Examples"])
        check("split returns success (rc 0)", rc == 0)

        head = open(skill_md).read()
        ref = open(ref_md).read()

        # (a) head shrank and lacks the moved headings
        check("head shrank vs original", len(head) < len(fixture))
        check("head lacks '## Anti-patterns'", "## Anti-patterns" not in head)
        check("head lacks '## Examples'", "## Examples" not in head)

        # Non-moved content preserved in head (frontmatter, intro, procedure).
        check("head keeps frontmatter name", "name: fixture" in head)
        check("head keeps intro", "Intro paragraph that stays in the head." in head)
        check("head keeps '## Procedure'", "## Procedure" in head)
        check("head keeps nested '### Sub-step'", "### Sub-step" in head)

        # (b) reference contains the moved sections verbatim
        anti_block = (
            "## Anti-patterns\n"
            "\n"
            "- Never hand-roll the math.\n"
            "- Never skip the gate.\n"
        )
        ex_block = (
            "## Examples\n"
            "\n"
            "A big examples block.\n"
            + ("x" * 4000) + "\n"
        )
        check("reference contains Anti-patterns verbatim", anti_block in ref)
        check("reference contains Examples verbatim", ex_block in ref)
        check("reference has 2-line title", ref.startswith(
            "# Fixture — reference\nLoaded on demand by `SKILL.md`.\n"))
        # Moved sections appear in original document order.
        check("reference order: Anti-patterns before Examples",
              ref.index(anti_block) < ref.index(ex_block))

        # (c) head gained the Reference index listing each moved heading
        check("head has Reference index heading",
              "## Reference (load on demand)" in head)
        check("index lists Anti-patterns",
              "- `Anti-patterns` — see `reference.md`" in head)
        check("index lists Examples",
              "- `Examples` — see `reference.md`" in head)

        # claude-code mirror skipped cleanly (no sibling) — head printed it.
        check("no claude-code mirror created",
              not os.path.exists(os.path.join(
                  tmp, "claude-code", ".claude", "skills", skill, "SKILL.md")))

        # (d) re-run with same args is a byte-identical no-op
        head_before = open(skill_md, "rb").read()
        ref_before = open(ref_md, "rb").read()
        rc2 = run(tmp, skill, ["## Anti-patterns", "Examples"])
        check("re-run returns success (rc 0)", rc2 == 0)
        check("re-run leaves SKILL.md byte-identical",
              open(skill_md, "rb").read() == head_before)
        check("re-run leaves reference.md byte-identical",
              open(ref_md, "rb").read() == ref_before)

        # (e) a bogus --move heading errors without writing
        head_snap = open(skill_md, "rb").read()
        ref_snap = open(ref_md, "rb").read()
        rc3 = run(tmp, skill, ["No Such Heading"])
        check("bogus heading returns error (rc != 0)", rc3 != 0)
        check("bogus heading wrote nothing to SKILL.md",
              open(skill_md, "rb").read() == head_snap)
        check("bogus heading wrote nothing to reference.md",
              open(ref_md, "rb").read() == ref_snap)

        # ── Mirror coverage: a skill that DOES have a claude-code sibling ───
        skill2 = "mirrored"
        for prefix in ("", "claude-code"):
            d2 = os.path.join(tmp, prefix, ".claude", "skills", skill2)
            os.makedirs(d2)
            open(os.path.join(d2, "SKILL.md"), "w").write(
                "---\nname: mirrored\ndescription: d.\n---\n\n"
                "# Mirrored\n\nLead.\n\n"
                "## Keep\n\nkept.\n\n"
                "## Move me\n\nmoved body.\n"
            )
        rc4 = run(tmp, skill2, ["Move me"])
        check("mirrored skill split returns success", rc4 == 0)
        rt = open(os.path.join(tmp, ".claude", "skills", skill2, "SKILL.md")).read()
        cc = open(os.path.join(tmp, "claude-code", ".claude", "skills", skill2, "SKILL.md")).read()
        check("claude-code SKILL.md mirrors root byte-for-byte", rt == cc)
        rt_ref = open(os.path.join(tmp, ".claude", "skills", skill2, "reference.md")).read()
        cc_ref = open(os.path.join(tmp, "claude-code", ".claude", "skills", skill2, "reference.md")).read()
        check("claude-code reference.md mirrors root byte-for-byte", rt_ref == cc_ref)

        # ── Extend mode: a second move appends to an existing reference.md ──
        skill3 = "extend"
        d3 = os.path.join(tmp, ".claude", "skills", skill3)
        os.makedirs(d3)
        open(os.path.join(d3, "SKILL.md"), "w").write(
            "---\nname: extend\ndescription: d.\n---\n\n"
            "# Extend\n\nLead.\n\n"
            "## Stay\n\nstays.\n\n"
            "## First\n\nfirst body.\n\n"
            "## Second\n\nsecond body.\n"
        )
        run(tmp, skill3, ["First"])
        ref_after_first = open(os.path.join(d3, "reference.md")).read()
        run(tmp, skill3, ["Second"])
        ref_after_second = open(os.path.join(d3, "reference.md")).read()
        head3 = open(os.path.join(d3, "SKILL.md")).read()
        check("extend: reference grew on the second move",
              len(ref_after_second) > len(ref_after_first))
        check("extend: reference keeps First verbatim",
              "## First\n\nfirst body.\n" in ref_after_second)
        check("extend: reference adds Second verbatim",
              "## Second\n\nsecond body.\n" in ref_after_second)
        check("extend: head index lists both moved headings",
              "- `First` — see `reference.md`" in head3
              and "- `Second` — see `reference.md`" in head3)
        check("extend: head still has the kept section", "## Stay" in head3)

        # ── `###` move: pull one ### out from under its parent `##` ─────────
        skill4 = "subsection"
        d4 = os.path.join(tmp, ".claude", "skills", skill4)
        os.makedirs(d4)
        # Parent `## Steps` owns three `###` children; we move only the middle.
        # A sibling `## Other` follows so the move-unit must stop at it, not
        # swallow it. A second top-level `## Notes` precedes none of this.
        sub_src = (
            "---\nname: subsection\ndescription: d.\n---\n\n"
            "# Subsection\n\nLead stays.\n\n"
            "## Steps\n\nSteps intro stays.\n\n"
            "### Keep first\n\nfirst child stays.\n\n"
            "### Move this one\n\nmoved child body.\n"
            + ("y" * 200) + "\n\n"
            "#### Even deeper\n\ndeep grandchild rides along.\n\n"
            "### Keep last\n\nlast child stays.\n\n"
            "## Other\n\nsibling section stays.\n"
        )
        open(os.path.join(d4, "SKILL.md"), "w").write(sub_src)
        rc5 = run(tmp, skill4, ["### Move this one"])
        check("###-move returns success (rc 0)", rc5 == 0)
        h4 = open(os.path.join(d4, "SKILL.md")).read()
        r4 = open(os.path.join(d4, "reference.md")).read()

        # (a) the moved ### — plus its own deeper grandchild — is in reference
        #     VERBATIM, stopping at the next same-or-higher (### Keep last).
        moved_block = (
            "### Move this one\n\nmoved child body.\n"
            + ("y" * 200) + "\n\n"
            "#### Even deeper\n\ndeep grandchild rides along.\n\n"
        )
        check("###-move: reference holds the ### (+ its #### child) verbatim",
              moved_block in r4)
        # (b) parent `## Steps` heading + its OTHER children remain in the head.
        check("###-move: head keeps parent '## Steps'", "## Steps" in h4)
        check("###-move: head keeps sibling '### Keep first'",
              "### Keep first\n\nfirst child stays.\n" in h4)
        check("###-move: head keeps sibling '### Keep last'",
              "### Keep last\n\nlast child stays.\n" in h4)
        check("###-move: head keeps following '## Other' section",
              "## Other\n\nsibling section stays.\n" in h4)
        # (c) the moved ### and its grandchild are GONE from the head.
        check("###-move: head drops the moved '### Move this one'",
              "### Move this one" not in h4)
        check("###-move: head drops the moved '#### Even deeper'",
              "#### Even deeper" not in h4)
        # (d) head gained the Reference index entry for the moved ###.
        check("###-move: index lists the moved ### heading",
              "- `Move this one` — see `reference.md`" in h4)
        # (e) a bogus ### still errors, writing nothing.
        h4_snap = open(os.path.join(d4, "SKILL.md"), "rb").read()
        r4_snap = open(os.path.join(d4, "reference.md"), "rb").read()
        rc6 = run(tmp, skill4, ["### No Such Subsection"])
        check("###-move: bogus ### returns error (rc != 0)", rc6 != 0)
        check("###-move: bogus ### wrote nothing to SKILL.md",
              open(os.path.join(d4, "SKILL.md"), "rb").read() == h4_snap)
        check("###-move: bogus ### wrote nothing to reference.md",
              open(os.path.join(d4, "reference.md"), "rb").read() == r4_snap)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    lines, passed, failed = [], 0, 0
    for label, ok in cases:
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        passed += ok
        failed += (not ok)
    return passed, failed, lines


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        passed, failed, lines = self_test()
        for ln in lines:
            print(ln)
        print(f"\nsplit_skill self-test: {passed} passed, {failed} failed.")
        sys.exit(1 if failed else 0)

    root = "."
    if "--root" in args:
        i = args.index("--root")
        root = args[i + 1]
        del args[i:i + 2]
    if "--move" not in args:
        print(__doc__.strip().splitlines()[0])
        print("usage: split_skill.py <skill-name> --move \"<heading>\" "
              "[\"<heading>\" ...] [--root .] [--self-test]")
        sys.exit(2)
    mi = args.index("--move")
    skill_args = args[:mi]
    move_headings = args[mi + 1:]
    if len(skill_args) != 1 or not move_headings:
        print("usage: split_skill.py <skill-name> --move \"<heading>\" "
              "[\"<heading>\" ...] [--root .] [--self-test]")
        sys.exit(2)
    skill = skill_args[0]
    sys.exit(run(os.path.abspath(root), skill, move_headings))


if __name__ == "__main__":
    main()
