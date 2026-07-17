---
name: grm-doc-section
description: Extract one section (a Markdown heading's text, or a named <!-- section: NAME --> delimiter block) from a file instead of reading the whole thing. A shared utility other skills import or shell out to whenever a skill instruction would otherwise say "read the whole file" to get one fact. Use when writing/fixing a skill step that only needs one section of a doc, justfile, or config.
---

# doc-section

`doc_section.py` is a generic, dependency-free single-section extractor. It
exists so no skill instruction has to say "read the whole file" just to get
one section's worth of fact — a design-doc subsection, one `## v{X.Y}`
roadmap entry, one justfile recipe. Give it a file and a heading (or a named
delimiter block), and it returns just that section's text, failing loudly if
the section doesn't exist.

This is a **shared, cross-skill utility**, not a single-purpose skill's
private tool — deliberately not nested under `grm-doc-assurance` or any other
skill that happens to use it. Any skill needing one section of a file should
reach for this instead of instructing "read the whole file."

## CLI usage

```bash
python3 .claude/skills/grm-doc-section/doc_section.py --file docs/roadmap.md --heading "## v3.96"
python3 .claude/skills/grm-doc-section/doc_section.py --file some.conf --section feature-x
python3 .claude/skills/grm-doc-section/doc_section.py --self-test
```

- `--heading "## Foo"` — Markdown ATX heading (any level `#` through `######`).
  Extraction runs from that heading (inclusive) through the line before the
  next heading of the **same or higher** level — so a nested sub-heading
  inside the requested section is included, not treated as a stop point. If
  the exact heading text isn't found, an unambiguous same-level *prefix*
  match is tried next (e.g. `--heading "## v3.96"` resolves a real heading
  of `## v3.96 — Token diet`); ambiguous or zero prefix matches still fail.
- `--section NAME` — named-delimiter fallback for non-Markdown-heading files
  (plain text, justfiles, config). Convention:

  ```
  <!-- section: NAME -->
  ... content ...
  <!-- /section: NAME -->
  ```

  Returns the text strictly between the two marker lines (markers excluded).

- **Not found always fails loud** — non-zero exit, a clear stderr message
  naming the file/heading and (for the heading path) the same-level headings
  actually present. Never silently returns empty.

## Library usage (cross-skill import convention)

Same convention as `grm-worktree-preflight`'s scripts
(`worktree_reap.py`, `agent_branch_namespace.py`) being imported by sibling
skills like `grm-fleet-git-audit`:

```python
import os, sys
REPO_ROOT = ...  # however the caller locates repo root
_DOC_SECTION_DIR = os.path.join(REPO_ROOT, ".claude", "skills", "grm-doc-section")
if _DOC_SECTION_DIR not in sys.path:
    sys.path.insert(0, _DOC_SECTION_DIR)

from doc_section import extract_section, SectionNotFoundError

text = extract_section("docs/roadmap.md", heading="## v3.96")
text = extract_section("some.conf", section="feature-x")
```

`extract_section(file_path, heading=None, section=None)` — exactly one of
`heading`/`section` required. Raises `SectionNotFoundError` on a miss,
`ValueError` on a malformed call.

## Self-test

`--self-test` covers: a found `##` section, a found `###` section nested
inside a `##` parent (stops at the next same-or-higher heading, not the
first nested one), an unambiguous prefix match, an ambiguous prefix match
(still fails loud), a not-found heading (fails loud), a found named-delimiter
section, a not-found named-delimiter section (fails loud), and malformed
calls (neither/both of `heading`/`section` given).

## When to reach for this vs. reading the whole file

Reach for `doc_section.py` whenever a skill step names one section of a file
by heading or label — that's the "read the whole file for one fact" pattern
this closes (#407). Reading the whole file remains correct when a step
genuinely needs broad context (e.g. surveying an entire design doc, or a
step that already restricts itself to "read this one small file directly,"
not a subsection of a larger one).
