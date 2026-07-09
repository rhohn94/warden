#!/usr/bin/env python3
"""footprint — static token-footprint reporter for a Grimoire skill set.

Turns the manual token audit into one command. Scans every
`<root>/.claude/skills/*/SKILL.md`: measures each skill's `description:`
frontmatter field (first line of the field) and its SKILL.md body size, and
reads `CLAUDE.md`. Emits a per-skill table, totals (Σ descriptions, CLAUDE.md,
Σ bodies — chars/bytes + est. tokens at chars//4), and a one-line controllable
baseline (descriptions + CLAUDE.md). Stdlib-only; read-only.

Usage:
  footprint.py [--root .] [--self-test]

The "controllable baseline" is the always-loaded surface an author can trim
directly: every skill's description (loaded on every trigger of that skill) plus
CLAUDE.md (loaded every session). SKILL.md bodies load only when a skill fires,
so they are reported separately. Token estimate is chars//4 (a rough proxy).
"""
import os, sys, glob

SKILL_BUDGET = 12_000   # doc-assurance body budget; bodies over this are flagged.
CHARS_PER_TOKEN = 4     # rough token estimate divisor (matches the audit proxy).


# ── Frontmatter description extraction ───────────────────────────────────
def extract_description(text):
    """Return the SKILL.md `description:` field's first line, or "" if absent.

    Handles the two shapes used in this repo:
      - inline  ``description: some text``  → returns ``some text``
      - block   ``description: >-`` (or ``|``) followed by indented lines
                 → returns the first indented content line (stripped)
    "First line of the field" is taken literally so the metric is deterministic
    without a full YAML parser (stdlib-only).
    """
    if not text.startswith("---"):
        return ""
    lines = text.splitlines()
    # Frontmatter is between the first '---' and the next '---'.
    if not lines or lines[0].strip() != "---":
        return ""
    fm_end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end = i
            break
    if fm_end is None:
        return ""
    fm = lines[1:fm_end]
    for idx, line in enumerate(fm):
        stripped = line.lstrip()
        if not stripped.startswith("description:"):
            continue
        value = line.split("description:", 1)[1].strip()
        # Block scalar marker (>, |, >-, |-, >+, |+, optionally with a number)?
        if value and value[0] in "|>":
            # First non-blank indented line below is the field's first line.
            for nxt in fm[idx + 1:]:
                if nxt.strip():
                    return nxt.strip()
            return ""
        return value
    return ""


# ── Measurement model ────────────────────────────────────────────────────
class SkillFootprint:
    """Per-skill measurement: description-field chars and SKILL.md body bytes.

    `desc_chars` counts characters of the description field's first line.
    `body_bytes` is the on-disk size of SKILL.md. `over_budget` flags bodies
    above the doc-assurance SKILL_BUDGET.
    """

    def __init__(self, name, desc_chars, body_bytes):
        self.name = name
        self.desc_chars = desc_chars
        self.body_bytes = body_bytes

    @property
    def over_budget(self):
        return self.body_bytes > SKILL_BUDGET


def collect(root):
    """Return (skills, claude_md_bytes).

    `skills` is a list[SkillFootprint] for every <root>/.claude/skills/*/SKILL.md,
    stably sorted by skill name. `claude_md_bytes` is len(CLAUDE.md) in bytes
    (0 when absent).
    """
    skills = []
    pattern = os.path.join(root, ".claude", "skills", "*", "SKILL.md")
    for p in sorted(glob.glob(pattern)):
        name = os.path.basename(os.path.dirname(p))
        raw = open(p, "rb").read()
        desc = extract_description(raw.decode("utf-8", errors="replace"))
        skills.append(SkillFootprint(name, len(desc), len(raw)))
    skills.sort(key=lambda s: s.name)   # stable, explicit
    claude_md = os.path.join(root, "CLAUDE.md")
    cm_bytes = os.path.getsize(claude_md) if os.path.exists(claude_md) else 0
    return skills, cm_bytes


def _est_tokens(chars):
    return chars // CHARS_PER_TOKEN


def render(skills, claude_md_bytes):
    """Build the full report as a list of printable lines."""
    out = []
    # ── Per-skill table ────────────────────────────────────────────────
    name_w = max([len("skill")] + [len(s.name) for s in skills]) if skills else len("skill")
    out.append(f"{'skill':<{name_w}}  {'desc chars':>10}  {'body bytes':>10}  flag")
    out.append(f"{'-' * name_w}  {'-' * 10}  {'-' * 10}  ----")
    for s in skills:
        flag = "OVER" if s.over_budget else ""
        out.append(f"{s.name:<{name_w}}  {s.desc_chars:>10}  {s.body_bytes:>10}  {flag}")

    # ── Totals ─────────────────────────────────────────────────────────
    sum_desc = sum(s.desc_chars for s in skills)
    sum_body = sum(s.body_bytes for s in skills)
    over = [s.name for s in skills if s.over_budget]
    out.append("")
    out.append("Totals")
    out.append(f"  Σ descriptions : {sum_desc:>8} chars / {sum_desc:>8} bytes "
               f"≈ {_est_tokens(sum_desc):>6} tok")
    out.append(f"  CLAUDE.md      : {claude_md_bytes:>8} chars / {claude_md_bytes:>8} bytes "
               f"≈ {_est_tokens(claude_md_bytes):>6} tok")
    out.append(f"  Σ bodies       : {sum_body:>8} chars / {sum_body:>8} bytes "
               f"≈ {_est_tokens(sum_body):>6} tok")
    out.append(f"  skills over {SKILL_BUDGET}-byte body budget: {len(over)}"
               + (f" ({', '.join(over)})" if over else ""))

    # ── Controllable baseline ──────────────────────────────────────────
    baseline = sum_desc + claude_md_bytes
    out.append("")
    out.append(f"controllable baseline (descriptions + CLAUDE.md) = {baseline} chars "
               f"≈ {_est_tokens(baseline)} tok")
    return out


# ── Self-test ────────────────────────────────────────────────────────────
def self_test():
    """Fixture-tree tests asserting the totals math. Returns (passed, failed, lines)."""
    import tempfile, shutil

    cases = []

    def check(label, ok):
        cases.append((label, bool(ok)))

    tmp = tempfile.mkdtemp(prefix="footprint_test_")
    try:
        skills_dir = os.path.join(tmp, ".claude", "skills")

        def mk_skill(name, description_block, body_extra=""):
            d = os.path.join(skills_dir, name)
            os.makedirs(d)
            content = f"---\nname: {name}\n{description_block}\n---\n\n# {name}\n\n{body_extra}"
            open(os.path.join(d, "SKILL.md"), "w").write(content)
            return content

        # Skill A: inline description, small body.
        desc_a = "Alpha skill does a thing."          # 25 chars
        c_a = mk_skill("alpha", f"description: {desc_a}")
        # Skill B: inline description, big body (> budget) to exercise the flag.
        desc_b = "Beta skill does another thing here."  # 35 chars
        c_b = mk_skill("beta", f"description: {desc_b}", body_extra="x" * 13000)
        # Skill C: block-scalar description; first line is the measured field.
        first_line_c = "Gamma block first line."         # 23 chars
        block_c = "description: >-\n  " + first_line_c + "\n  second line ignored."
        c_c = mk_skill("gamma", block_c)

        claude_md = "# Project\n\nGuidance.\n"
        open(os.path.join(tmp, "CLAUDE.md"), "w").write(claude_md)

        skills, cm = collect(tmp)

        # Stable sort by name → alpha, beta, gamma.
        check("collect finds 3 skills", len(skills) == 3)
        check("skills sorted by name", [s.name for s in skills] == ["alpha", "beta", "gamma"])

        # Description char counts match the first-line lengths.
        by = {s.name: s for s in skills}
        check("alpha desc chars == len(inline)", by["alpha"].desc_chars == len(desc_a))
        check("beta desc chars == len(inline)", by["beta"].desc_chars == len(desc_b))
        check("gamma desc chars == block first line", by["gamma"].desc_chars == len(first_line_c))

        # Body bytes match the file sizes on disk.
        check("alpha body bytes == file size", by["alpha"].body_bytes == len(c_a.encode()))
        check("beta body bytes == file size", by["beta"].body_bytes == len(c_b.encode()))
        check("gamma body bytes == file size", by["gamma"].body_bytes == len(c_c.encode()))

        # Budget flag: only beta is over.
        check("beta flagged over budget", by["beta"].over_budget is True)
        check("alpha not over budget", by["alpha"].over_budget is False)
        check("gamma not over budget", by["gamma"].over_budget is False)

        # CLAUDE.md bytes.
        check("CLAUDE.md bytes == file size", cm == len(claude_md.encode()))

        # ── Totals math ───────────────────────────────────────────────────
        sum_desc = len(desc_a) + len(desc_b) + len(first_line_c)
        sum_body = len(c_a.encode()) + len(c_b.encode()) + len(c_c.encode())
        baseline = sum_desc + cm

        check("Σ descriptions == sum of first-lines",
              sum(s.desc_chars for s in skills) == sum_desc)
        check("Σ bodies == sum of file sizes",
              sum(s.body_bytes for s in skills) == sum_body)
        check("controllable baseline == Σdesc + CLAUDE.md",
              (sum(s.desc_chars for s in skills) + cm) == baseline)
        check("est tokens == chars//4", _est_tokens(baseline) == baseline // 4)

        # ── Render contains the baseline line and the totals ──────────────
        lines = render(skills, cm)
        joined = "\n".join(lines)
        check("render has baseline line",
              f"controllable baseline (descriptions + CLAUDE.md) = {baseline} chars" in joined)
        check("render flags the over-budget skill", "OVER" in joined)
        check("render header present", "skill" in lines[0] and "desc chars" in lines[0])

        # ── Empty tree: no skills, no CLAUDE.md → zeroed totals ───────────
        empty = tempfile.mkdtemp(prefix="footprint_empty_")
        try:
            os.makedirs(os.path.join(empty, ".claude", "skills"))
            sk2, cm2 = collect(empty)
            check("empty tree: 0 skills", len(sk2) == 0)
            check("empty tree: CLAUDE.md bytes 0", cm2 == 0)
            ren2 = "\n".join(render(sk2, cm2))
            check("empty tree: baseline 0",
                  "controllable baseline (descriptions + CLAUDE.md) = 0 chars" in ren2)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out_lines, passed, failed = [], 0, 0
    for label, ok in cases:
        out_lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        passed += ok
        failed += (not ok)
    return passed, failed, out_lines


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        passed, failed, lines = self_test()
        for ln in lines:
            print(ln)
        print(f"\nfootprint self-test: {passed} passed, {failed} failed.")
        sys.exit(1 if failed else 0)
    root = "."
    if "--root" in args:
        root = args[args.index("--root") + 1]
    skills, cm = collect(os.path.abspath(root))
    for ln in render(skills, cm):
        print(ln)


if __name__ == "__main__":
    main()
