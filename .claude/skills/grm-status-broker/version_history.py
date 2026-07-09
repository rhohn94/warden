#!/usr/bin/env python3
"""version_history.py — deterministic single-section extractor for version-history.

`docs/version-history.md` is append-only and large (>100 KB / tens of thousands
of tokens). Agents almost never need the whole file: they want one release's
notes, the newest entry, or just the list of versions+titles. Reading the whole
file to find one section is pure token waste. This stdlib-only helper slices the
file deterministically so callers spend tokens on one section, not the corpus.

The file is a sequence of `## vX.Y — title` sections, newest first, e.g.

    ## v3.37.2 — Toolsmith (token-efficiency tooling)

    - bullet
    - bullet

A "section" runs from its `## ` heading up to (but not including) the next
`## ` heading (or end of file). The leading `---` separators between sections
belong to the *previous* section's trailing whitespace and are not emitted.

Usage:
  version_history.py --release vX.Y   # print exactly that one section, verbatim
  version_history.py --latest         # print the first (newest) section
  version_history.py --list           # print each section's "vX.Y — title" line
  version_history.py [--root DIR]      # locate docs/version-history.md under DIR
  version_history.py --self-test
Exit 0 on success; 2 on bad input / a requested release that is not present.

Design authority: docs/grimoire/design/status-broker-design.md (sibling helper to
project_status.py; scripting-unification guidelines,
docs/grimoire/design/scripting-unification-design.md §3).
"""
import argparse
import os
import re
import sys

# A section heading: "## vX.Y[.Z] — title". The em-dash / en-dash / hyphen and
# the title are optional so a bare "## v3.2" still parses.
_HEADING = re.compile(r"^##\s+(v\d+(?:\.\d+)+)\s*(?:[—\-–]\s*(.*))?$")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def default_path(root):
    """The conventional location of the version-history file under a project root."""
    return os.path.join(root, "docs", "version-history.md")


def parse_sections(text):
    """Split the document into ordered sections (newest first, as written).

    Returns a list of dicts: {version, title, body} where ``body`` is the exact
    text of the section from its ``## `` heading line through the line before the
    next ``## `` heading (or end of file), with one trailing newline guaranteed
    and surrounding blank lines / ``---`` separators stripped from the tail.
    """
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    # Index every heading line.
    starts = [i for i, ln in enumerate(lines) if _HEADING.match(ln)]
    sections = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        block = lines[start:end]
        m = _HEADING.match(block[0])
        version = m.group(1)
        title = (m.group(2) or "").strip()
        # Trim trailing blank lines and lone `---` separators so each section is
        # self-contained and byte-stable regardless of inter-section spacing.
        tail = list(block)
        while tail and tail[-1].strip() in ("", "---"):
            tail.pop()
        body = "".join(tail)
        if not body.endswith("\n"):
            body += "\n"
        sections.append({"version": version, "title": title, "body": body})
    return sections


def get_release(sections, version):
    """Return the section dict whose version matches, or None."""
    for s in sections:
        if s["version"] == version:
            return s
    return None


def list_lines(sections):
    """One "vX.Y — title" (or bare "vX.Y") line per section, newest first."""
    out = []
    for s in sections:
        out.append("%s — %s" % (s["version"], s["title"]) if s["title"]
                   else s["version"])
    return out


def _load(root):
    path = default_path(root)
    text = _read(path)
    if text is None:
        print("error: version-history not found: %s" % path, file=sys.stderr)
        return None, None
    return path, parse_sections(text)


def _self_test():
    failures = []
    fixture = (
        "# Version History\n\n"
        "> intro blurb, not a section\n\n"
        "---\n\n"
        "## v3.3 — Third release\n\n"
        "- newest bullet\n"
        "- another\n\n"
        "---\n\n"
        "## v3.2 — Second release\n\n"
        "- middle bullet\n\n"
        "---\n\n"
        "## v3.1 — First release\n\n"
        "- oldest bullet\n"
    )
    secs = parse_sections(fixture)

    if len(secs) != 3:
        failures.append("expected 3 sections, got %d" % len(secs))
    if secs and secs[0]["version"] != "v3.3":
        failures.append("first section should be v3.3, got %r" % secs[0]["version"])

    # --list returns exactly 3 lines, in newest-first order, with titles.
    lines = list_lines(secs)
    if lines != ["v3.3 — Third release", "v3.2 — Second release",
                 "v3.1 — First release"]:
        failures.append("--list output wrong: %r" % lines)

    # --latest returns the first section verbatim and excludes the next heading
    # and the intervening `---` separator.
    latest = secs[0]["body"] if secs else ""
    if latest != "## v3.3 — Third release\n\n- newest bullet\n- another\n":
        failures.append("--latest body wrong: %r" % latest)
    if "v3.2" in latest or "---" in latest:
        failures.append("--latest leaked into the next section: %r" % latest)

    # --release returns exactly that one section, verbatim, nothing adjacent.
    mid = get_release(secs, "v3.2")
    if mid is None:
        failures.append("--release v3.2 not found")
    elif mid["body"] != "## v3.2 — Second release\n\n- middle bullet\n":
        failures.append("--release v3.2 body wrong: %r" % mid["body"])
    elif "v3.3" in mid["body"] or "v3.1" in mid["body"]:
        failures.append("--release v3.2 leaked an adjacent section")

    # A missing release is an error (None), not a silent empty result.
    if get_release(secs, "v9.9") is not None:
        failures.append("missing release should resolve to None")

    # A bare heading with no title still parses (title empty, listed bare).
    bare = parse_sections("## v4.0\n\n- body\n")
    if len(bare) != 1 or bare[0]["version"] != "v4.0" or bare[0]["title"] != "":
        failures.append("bare heading (no title) parse wrong: %r" % bare)
    if list_lines(bare) != ["v4.0"]:
        failures.append("bare heading --list wrong: %r" % list_lines(bare))

    # Empty / missing input yields no sections (degrade, not crash).
    if parse_sections("") != [] or parse_sections(None) != []:
        failures.append("empty input should yield no sections")

    # Determinism: re-parsing the same text is byte-identical.
    if parse_sections(fixture) != secs:
        failures.append("non-deterministic parse")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("version_history self-test: OK (parse/--list/--latest/--release/"
          "missing-release/bare-heading/empty/determinism)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Deterministic single-section extractor for version-history.md.")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--release", metavar="vX.Y",
                      help="print just that release's section")
    mode.add_argument("--latest", action="store_true",
                      help="print the newest (first) section")
    mode.add_argument("--list", action="store_true",
                      help="print one 'vX.Y — title' line per section")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not os.path.isdir(args.root):
        print("error: --root is not a directory: %s" % args.root, file=sys.stderr)
        return 2

    path, sections = _load(args.root)
    if sections is None:
        return 2

    if args.release:
        sec = get_release(sections, args.release)
        if sec is None:
            print("error: release %s not found in %s" % (args.release, path),
                  file=sys.stderr)
            return 2
        sys.stdout.write(sec["body"])
        return 0
    if args.latest:
        if not sections:
            print("error: no sections found in %s" % path, file=sys.stderr)
            return 2
        sys.stdout.write(sections[0]["body"])
        return 0
    if args.list:
        for ln in list_lines(sections):
            print(ln)
        return 0

    # No mode flag: report what is available so the call is never silently empty.
    if not sections:
        print("error: no sections found in %s" % path, file=sys.stderr)
        return 2
    print("%s — %d release section(s); pass --release vX.Y / --latest / --list."
          % (path, len(sections)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
