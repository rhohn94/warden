#!/usr/bin/env python3
"""doc_section.py — generic single-section doc extractor (#407).

Problem: a skill needs one fact that lives in one section of a file (a
design-doc subsection, one `## v{X.Y}` roadmap entry, one justfile recipe)
but the only documented way to get it is "read the whole file" — paying the
full file's token cost for a few paragraphs. This script is the mechanical
fix: given a file path and either a Markdown heading or a named delimiter
block, it returns just that section's text and fails loudly if the section
doesn't exist.

Two extraction modes:

1. Markdown heading (`--heading`). Give the exact heading line, including
   its `#` markers, e.g. `--heading "## v3.96"` or `--heading "### Design"`.
   Matching is on heading *level* (number of leading `#`) and *text*
   (case-sensitive, whitespace-trimmed, ATX closing hashes ignored).
   Extraction starts at the matching heading line (inclusive) and runs
   through the line *before* the next heading of the SAME OR HIGHER level
   (fewer or equal `#` chars) — so a `###` section's own nested `####`
   sub-headings are included, not treated as stop points. If no
   same-or-higher heading follows, extraction runs to end of file.

   If no exact match is found, falls back to an unambiguous same-level
   *prefix* match (e.g. `--heading "## v3.96"` matches an actual heading of
   `## v3.96 — Token diet`) — only when exactly one same-level heading
   starts with the given text; ambiguous or zero prefix matches still fail
   loud.

2. Named delimiter block (`--section NAME`), for non-Markdown-heading use
   cases (plain text, config files, justfiles, anything without ATX
   headings). Convention:

       <!-- section: NAME -->
       ... content ...
       <!-- /section: NAME -->

   The markers must appear on their own line (surrounding whitespace is
   ignored). Extraction returns everything strictly between the two marker
   lines (the marker lines themselves are not included). Nesting is not
   supported — the first `<!-- /section: NAME -->` after the opening marker
   closes it.

Both modes fail loud (raise `SectionNotFoundError`, CLI exits non-zero with
a clear message) when the requested section isn't found. Never silently
returns empty.

Usage as a library (matches this repo's cross-skill import convention — see
`grm-worktree-preflight/agent_branch_namespace.py`'s docstring and any
sibling script that does
`sys.path.insert(0, os.path.join(REPO_ROOT, ".claude", "skills", "grm-doc-section"))`
then `from doc_section import extract_section`):

    from doc_section import extract_section
    text = extract_section("docs/roadmap.md", heading="## v3.96")
    text = extract_section("some.conf", section="feature-x")

Usage as a CLI:
    doc_section.py --file docs/roadmap.md --heading "## v3.96"
    doc_section.py --file some.conf --section feature-x
    doc_section.py --self-test
"""
from __future__ import annotations

import argparse
import re
import sys

_HEADING_RE = re.compile(r"^(#{1,6})\s*(.*?)\s*#*\s*$")


class SectionNotFoundError(Exception):
    """Raised when the requested heading or named section is not present."""


def _heading_level_and_text(line: str):
    """Return (level, text) if `line` is an ATX heading line, else None."""
    m = _HEADING_RE.match(line.rstrip("\n"))
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip()


def _extract_by_heading(content: str, heading: str, file_path: str) -> str:
    parsed = _heading_level_and_text(heading)
    if parsed is None:
        raise ValueError(
            f"--heading {heading!r} is not a valid ATX Markdown heading "
            f"(expected e.g. '## Some Heading')"
        )
    target_level, target_text = parsed

    lines = content.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        h = _heading_level_and_text(line)
        if h is not None and h[0] == target_level and h[1] == target_text:
            start = i
            break

    if start is None:
        # Fallback: unambiguous prefix match at the same level. Real docs
        # often suffix a heading with free text that varies over time (e.g.
        # a roadmap's "## v3.96" is actually "## v3.96 — Token diet") — a
        # caller who only knows the stable prefix shouldn't have to grep the
        # file first just to learn today's suffix. Only applied when exactly
        # one same-level heading starts with the given text; multiple or
        # zero matches fall through to the loud not-found error below.
        prefix_matches = []
        for i, line in enumerate(lines):
            h = _heading_level_and_text(line)
            if h is not None and h[0] == target_level and h[1].startswith(target_text):
                prefix_matches.append(i)
        if len(prefix_matches) == 1:
            start = prefix_matches[0]

    if start is None:
        # Build a helpful list of same-level headings actually present.
        seen = []
        for line in lines:
            h = _heading_level_and_text(line)
            if h is not None and h[0] == target_level:
                seen.append(f"{'#' * h[0]} {h[1]}")
        hint = f" Headings at that level found in the file: {seen}" if seen else \
            f" No level-{target_level} headings found in the file at all."
        raise SectionNotFoundError(
            f"heading {heading!r} not found in {file_path!r} (exact match, "
            f"and no unambiguous same-level prefix match either).{hint}"
        )

    end = len(lines)
    for j in range(start + 1, len(lines)):
        h = _heading_level_and_text(lines[j])
        if h is not None and h[0] <= target_level:
            end = j
            break

    return "".join(lines[start:end])


def _extract_by_section(content: str, name: str, file_path: str) -> str:
    open_re = re.compile(r"^\s*<!--\s*section:\s*" + re.escape(name) + r"\s*-->\s*$")
    close_re = re.compile(r"^\s*<!--\s*/section:\s*" + re.escape(name) + r"\s*-->\s*$")

    lines = content.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if open_re.match(line.rstrip("\n")):
            start = i
            break

    if start is None:
        raise SectionNotFoundError(
            f"named section {name!r} (<!-- section: {name} -->) not found in "
            f"{file_path!r}"
        )

    end = None
    for j in range(start + 1, len(lines)):
        if close_re.match(lines[j].rstrip("\n")):
            end = j
            break

    if end is None:
        raise SectionNotFoundError(
            f"named section {name!r} opened at line {start + 1} but its closing "
            f"marker (<!-- /section: {name} -->) was never found in {file_path!r}"
        )

    return "".join(lines[start + 1:end])


def extract_section(file_path: str, heading: str | None = None,
                     section: str | None = None) -> str:
    """Return the text of one section of `file_path`.

    Exactly one of `heading` (a Markdown ATX heading string, e.g. "## Foo")
    or `section` (a named-delimiter block name) must be given. Raises
    `SectionNotFoundError` if the requested section is not present, and
    `ValueError` if the call itself is malformed (neither/both given, or an
    invalid heading string). Never returns an empty string silently on a
    miss — callers get an exception instead.
    """
    if (heading is None) == (section is None):
        raise ValueError("extract_section: pass exactly one of heading= or section=")

    with open(file_path, encoding="utf-8") as fh:
        content = fh.read()

    if heading is not None:
        return _extract_by_heading(content, heading, file_path)
    return _extract_by_section(content, section, file_path)


# ── self-test ─────────────────────────────────────────────────────────────
def _self_test() -> int:
    import os
    import tempfile

    failures = []

    def check(label, fn):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{label}: {e}")
        except Exception as e:  # unexpected exception is also a failure
            failures.append(f"{label}: unexpected {type(e).__name__}: {e}")

    md = """\
# Title

Intro text.

## Alpha

Alpha body line 1.
Alpha body line 2.

### Alpha sub

Nested content under alpha — must be INCLUDED when extracting ## Alpha,
and must be the whole return value when extracting ### Alpha sub (up to
the next ## heading).

More alpha body after the nested sub-heading.

## Beta

Beta body.
"""

    with tempfile.TemporaryDirectory() as td:
        md_path = os.path.join(td, "doc.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md)

        # 1. Found section at ## level.
        def t1():
            text = extract_section(md_path, heading="## Alpha")
            assert text.startswith("## Alpha\n"), text[:40]
            assert "Alpha body line 1." in text
            assert "### Alpha sub" in text, "nested sub-heading must be included"
            assert "More alpha body after the nested sub-heading." in text
            assert "## Beta" not in text, "must stop before the next ## heading"

        check("found ## section", t1)

        # 2. Found section at ### level, nested inside ## Alpha — must stop
        #    at the next same-or-higher heading (## Beta), not run forever,
        #    and must not be truncated early by anything inside itself.
        def t2():
            text = extract_section(md_path, heading="### Alpha sub")
            assert text.startswith("### Alpha sub\n"), text[:40]
            assert "More alpha body after the nested sub-heading." in text
            assert "## Beta" not in text, "must stop at next same-or-higher heading"

        check("found ### section (nested, stops at next ##)", t2)

        # 3. Section containing a nested sub-heading, extracted at the
        #    PARENT level — the nested heading must not be treated as a
        #    stop point (this is the acceptance-criteria case, distinct
        #    from t1/t2's narrower assertions).
        def t3():
            text = extract_section(md_path, heading="## Alpha")
            # Everything through Beta's boundary, not cut off at ### Alpha sub.
            beta_idx = text.find("## Beta")
            sub_idx = text.find("### Alpha sub")
            assert sub_idx != -1 and beta_idx == -1, \
                "nested heading present, but the section must extend past it"

        check("section with nested sub-heading extracts through to next same-or-higher heading", t3)

        # 4. Not-found heading fails loud.
        def t4():
            try:
                extract_section(md_path, heading="## Nonexistent")
                assert False, "expected SectionNotFoundError, got no exception"
            except SectionNotFoundError:
                pass

        check("not-found heading fails loud", t4)

        # 4b. Unambiguous prefix match: "## Alpha" is a full match already,
        #     so use a title-suffixed doc to test the true prefix case (the
        #     real-world "## v3.96" vs "## v3.96 — Token diet" shape).
        suffixed_path = os.path.join(td, "suffixed.md")
        with open(suffixed_path, "w", encoding="utf-8") as fh:
            fh.write(
                "## v3.95 — Disk & branch hygiene\n\nv3.95 body.\n\n"
                "## v3.96 — Token diet\n\nv3.96 body.\n\n"
                "## v3.97 — Next\n\nv3.97 body.\n"
            )

        def t4b():
            text = extract_section(suffixed_path, heading="## v3.96")
            assert text.startswith("## v3.96 — Token diet\n"), text[:60]
            assert "v3.96 body." in text
            assert "v3.95" not in text and "v3.97" not in text

        check("unambiguous same-level prefix match resolves the real heading", t4b)

        # 4c. Ambiguous prefix match fails loud rather than guessing.
        ambiguous_path = os.path.join(td, "ambiguous.md")
        with open(ambiguous_path, "w", encoding="utf-8") as fh:
            fh.write(
                "## v3.9 — Old\n\nold body.\n\n"
                "## v3.96 — Token diet\n\nnew body.\n"
            )

        def t4c():
            try:
                extract_section(ambiguous_path, heading="## v3.9")
                assert False, "expected SectionNotFoundError on ambiguous prefix"
            except SectionNotFoundError:
                pass

        check("ambiguous prefix match still fails loud (no guessing)", t4c)

        # 5. Named delimiter fallback: found.
        delim_path = os.path.join(td, "doc.conf")
        with open(delim_path, "w", encoding="utf-8") as fh:
            fh.write(
                "prelude\n"
                "<!-- section: feature-x -->\n"
                "feature-x line 1\n"
                "feature-x line 2\n"
                "<!-- /section: feature-x -->\n"
                "trailer\n"
                "<!-- section: feature-y -->\n"
                "feature-y line 1\n"
                "<!-- /section: feature-y -->\n"
            )

        def t5():
            text = extract_section(delim_path, section="feature-x")
            assert "feature-x line 1" in text
            assert "feature-x line 2" in text
            assert "feature-y" not in text
            assert "prelude" not in text and "trailer" not in text
            assert "<!-- section:" not in text, "marker lines must not be included"

        check("named delimiter section found", t5)

        # 6. Named delimiter fallback: not found fails loud.
        def t6():
            try:
                extract_section(delim_path, section="does-not-exist")
                assert False, "expected SectionNotFoundError, got no exception"
            except SectionNotFoundError:
                pass

        check("named delimiter not-found fails loud", t6)

        # 7. Malformed call: both or neither given.
        def t7():
            try:
                extract_section(md_path)
                assert False, "expected ValueError when neither heading nor section given"
            except ValueError:
                pass
            try:
                extract_section(md_path, heading="## Alpha", section="feature-x")
                assert False, "expected ValueError when both heading and section given"
            except ValueError:
                pass

        check("malformed call (neither/both) raises ValueError", t7)

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        print(f"doc_section self-test: {len(failures)} failure(s)", file=sys.stderr)
        return 1

    print("doc_section self-test: OK (9 cases)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract one section (Markdown heading or named delimiter "
                     "block) from a file, instead of reading the whole thing."
    )
    ap.add_argument("--file", metavar="PATH", help="file to extract from")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--heading", metavar="HEADING",
                        help='Markdown ATX heading to extract, e.g. "## Foo"')
    group.add_argument("--section", metavar="NAME",
                        help="named delimiter block to extract "
                             "(<!-- section: NAME --> ... <!-- /section: NAME -->)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    if not args.file or not (args.heading or args.section):
        ap.print_help()
        return 2

    try:
        text = extract_section(args.file, heading=args.heading, section=args.section)
    except (SectionNotFoundError, ValueError, OSError) as e:
        print(f"doc_section: {e}", file=sys.stderr)
        return 1

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
