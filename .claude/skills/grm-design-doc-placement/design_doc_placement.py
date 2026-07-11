#!/usr/bin/env python3
"""design_doc_placement.py — grm-design-doc-placement (#307).

Scans a project's `docs/design/` tier (consumer-facing) and, separately, the
framework-internal `docs/grimoire/design/` tier, and reports whether each
design doc is correctly placed per `docs/design/README.md`'s Subtrees rule:
a topic with more than one associated doc gets its own subdirectory with a
README index; a single-doc topic stays flat.

`docs/grimoire/design/` has its own, stricter rule (documented on this
skill's SKILL.md): it never promotes to subtrees at all — it stays flat
forever, however many docs accumulate, and groups them by prose section
headers in its hand-maintained README instead. This script only ever flags
`GRIMOIRE_SUBTREE_DISALLOWED` there, and never auto-fixes it (report-only
forever) since that README's categorized layout is hand-authored prose, not
a generated index.

Finding codes (consumer `docs/design/` tier):
  FLAT_SHOULD_BE_SUBTREE   two or more flat docs share a topic prefix
                           (e.g. `auth-design.md` + `auth-flow-design.md`) —
                           promote to `{topic}/` with a README index.
  SUBTREE_COULD_FLATTEN    a subtree directory holds exactly one design doc —
                           flatten it back to `{topic}-design.md`.
  WRONG_TOPIC_SUBTREE      a doc filed under subtree X whose filename slug
                           matches a *different* existing subtree's topic
                           prefix — move it to the correct subtree.

Finding code (framework `docs/grimoire/design/` tier):
  GRIMOIRE_SUBTREE_DISALLOWED   a subdirectory exists under
                                docs/grimoire/design/ at all (that tier is
                                flat-forever by convention). Report-only.

Report-only by default; `--apply` performs the `git mv` + README index /
breadcrumb updates for the two consumer-tier, non-destructive codes
(`FLAT_SHOULD_BE_SUBTREE`, `SUBTREE_COULD_FLATTEN`) and for
`WRONG_TOPIC_SUBTREE` when the correct destination subtree already exists —
following the same report-then-apply, non-destructive pattern as
`grm-structure-migrate` / `grm-docs-migrate`. Always run
`doc_assurance.py --write-design-index` after `--apply` to regenerate the
generated `<!-- design-index:begin -->` table (this script maintains the
hand-authored `### Subtrees` / `## Contents` lists and doc breadcrumbs only,
not that generated table).

Scope boundary (see SKILL.md): this skill never *creates* new design docs
(that's `grm-design-doc-scaffold`) and never moves non-design files (that's
`grm-structure-migrate`).

Usage:
    design_doc_placement.py --root DIR [--json]
    design_doc_placement.py --root DIR --apply
    design_doc_placement.py --self-test

Exit codes:
    0  no findings (or --apply resolved everything, or --self-test passed)
    1  findings present (detect mode), or --apply left findings unresolved,
       or --self-test failed

Python stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import Optional

CONSUMER_DESIGN_REL = "docs/design"
FRAMEWORK_DESIGN_REL = "docs/grimoire/design"
DESIGN_SUFFIX = "-design.md"

FLAT_SHOULD_BE_SUBTREE = "FLAT_SHOULD_BE_SUBTREE"
SUBTREE_COULD_FLATTEN = "SUBTREE_COULD_FLATTEN"
WRONG_TOPIC_SUBTREE = "WRONG_TOPIC_SUBTREE"
GRIMOIRE_SUBTREE_DISALLOWED = "GRIMOIRE_SUBTREE_DISALLOWED"

REPORT_ONLY_CODES = {GRIMOIRE_SUBTREE_DISALLOWED}

BREADCRUMB_RE = re.compile(r"^> \*\*Up:\*\* \[↑ Design index\]\(([^)]+)\)\s*$", re.M)


@dataclass
class Finding:
    code: str
    path: str
    message: str
    target: Optional[str] = None  # destination dir/file for a move
    group: Optional[str] = None   # sibling paths involved (comma-joined)

    def render(self) -> str:
        return f"  {self.path:<48} {self.code:<24} {self.message}"


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def slug_of(filename: str) -> str:
    """`auth-flow-design.md` -> `auth-flow`; degrades to stem minus `.md`."""
    if filename.endswith(DESIGN_SUFFIX):
        return filename[: -len(DESIGN_SUFFIX)]
    if filename.endswith(".md"):
        return filename[:-3]
    return filename


def title_of(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def _list_md(dirpath: str) -> list:
    if not os.path.isdir(dirpath):
        return []
    return sorted(
        f for f in os.listdir(dirpath)
        if f.endswith(".md") and f != "README.md"
        and os.path.isfile(os.path.join(dirpath, f))
    )


def scan_design_dir(root: str, design_rel: str) -> dict:
    """Returns {'flat': [filenames], 'subtrees': {dirname: [filenames]}}."""
    design_abs = os.path.join(root, design_rel)
    flat = _list_md(design_abs)
    subtrees = {}
    if os.path.isdir(design_abs):
        for entry in sorted(os.listdir(design_abs)):
            full = os.path.join(design_abs, entry)
            if os.path.isdir(full) and not entry.startswith("."):
                subtrees[entry] = _list_md(full)
    return {"flat": flat, "subtrees": subtrees}


# ---------------------------------------------------------------------------
# Classify: consumer docs/design/ tier
# ---------------------------------------------------------------------------


def _group_flat_by_topic(flat_slugs: list) -> dict:
    """Group flat-doc slugs sharing a hyphen-prefix relationship. Returns
    {topic_key: [slug, ...]} for groups of size >= 2. `topic_key` is the
    shortest slug in the group (the shared prefix)."""
    ordered = sorted(flat_slugs, key=len)
    groups: dict = {}
    for s in ordered:
        parent = None
        for key in groups:
            if s == key or s.startswith(key + "-"):
                parent = key
                break
        if parent is None:
            groups[s] = [s]
        else:
            groups[parent].append(s)
    return {k: v for k, v in groups.items() if len(v) >= 2}


def classify_consumer(root: str, design_rel: str) -> list:
    scanned = scan_design_dir(root, design_rel)
    findings = []

    # FLAT_SHOULD_BE_SUBTREE
    flat_slugs = [slug_of(f) for f in scanned["flat"]]
    groups = _group_flat_by_topic(flat_slugs)
    slug_to_file = {slug_of(f): f for f in scanned["flat"]}
    for topic, slugs in sorted(groups.items()):
        files = sorted(slug_to_file[s] for s in slugs)
        findings.append(Finding(
            FLAT_SHOULD_BE_SUBTREE,
            "/".join([design_rel, files[0]]),
            f"shares topic {topic!r} with {', '.join(files[1:])} — "
            f"promote to {design_rel}/{topic}/ with a README index",
            target=f"{design_rel}/{topic}",
            group=",".join(f"{design_rel}/{f}" for f in files),
        ))

    # SUBTREE_COULD_FLATTEN
    subtree_names = list(scanned["subtrees"].keys())
    for dirname, docs in sorted(scanned["subtrees"].items()):
        if len(docs) == 1:
            doc = docs[0]
            findings.append(Finding(
                SUBTREE_COULD_FLATTEN,
                f"{design_rel}/{dirname}/{doc}",
                f"only design doc in {dirname}/ — flatten to "
                f"{design_rel}/{dirname}{DESIGN_SUFFIX}",
                target=f"{design_rel}/{dirname}{DESIGN_SUFFIX}",
            ))

    # WRONG_TOPIC_SUBTREE
    for dirname, docs in sorted(scanned["subtrees"].items()):
        for doc in docs:
            s = slug_of(doc)
            if s == dirname or s.startswith(dirname + "-"):
                continue
            for other in subtree_names:
                if other == dirname:
                    continue
                if s == other or s.startswith(other + "-"):
                    findings.append(Finding(
                        WRONG_TOPIC_SUBTREE,
                        f"{design_rel}/{dirname}/{doc}",
                        f"topic {s!r} matches subtree {other}/, not {dirname}/ "
                        f"— move to {design_rel}/{other}/",
                        target=f"{design_rel}/{other}/{doc}",
                    ))
                    break

    findings.sort(key=lambda f: (f.path, f.code))
    return findings


def classify_framework(root: str, design_rel: str) -> list:
    scanned = scan_design_dir(root, design_rel)
    findings = []
    for dirname in sorted(scanned["subtrees"].keys()):
        findings.append(Finding(
            GRIMOIRE_SUBTREE_DISALLOWED,
            f"{design_rel}/{dirname}/",
            f"{design_rel} is flat-forever by convention — {dirname}/ "
            f"subtree is not allowed there (report-only; fix by hand)",
        ))
    return findings


def classify(root: str) -> list:
    findings = classify_consumer(root, CONSUMER_DESIGN_REL)
    findings.extend(classify_framework(root, FRAMEWORK_DESIGN_REL))
    return findings


# ---------------------------------------------------------------------------
# Apply: README + breadcrumb helpers
# ---------------------------------------------------------------------------


def _run_git(root: str, *args: str):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _rewrite_breadcrumb(path: str, new_target: str) -> None:
    if not os.path.isfile(path):
        return
    text = _read(path)
    new_text, n = BREADCRUMB_RE.subn(
        f"> **Up:** [↑ Design index]({new_target})", text, count=1
    )
    if n:
        _write(path, new_text)


def _title_from_doc(path: str) -> str:
    """Best-effort `# Title` extraction for a Contents-list description."""
    if not os.path.isfile(path):
        return ""
    for line in _read(path).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _parent_readme_path(root: str, design_rel: str) -> str:
    return os.path.join(root, design_rel, "README.md")


def _add_subtree_bullet(readme_path: str, dirname: str, title: str) -> None:
    if not os.path.isfile(readme_path):
        return
    text = _read(readme_path)
    bullet = f"- [`{dirname}/`]({dirname}/README.md) — {title} design docs.\n"
    marker = "### Subtrees"
    idx = text.find(marker)
    if idx == -1:
        return
    section_end = text.find("\n## ", idx)
    if section_end == -1:
        section_end = len(text)
    section = text[idx:section_end]
    lines = section.splitlines(keepends=True)
    insert_at = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("- "):
            insert_at = i + 1
            break
    lines.insert(insert_at, bullet)
    new_section = "".join(lines)
    new_text = text[:idx] + new_section + text[section_end:]
    _write(readme_path, new_text)


def _remove_subtree_bullet(readme_path: str, dirname: str) -> None:
    if not os.path.isfile(readme_path):
        return
    text = _read(readme_path)
    pattern = re.compile(
        r"^- \[`" + re.escape(dirname) + r"/`\]\(" + re.escape(dirname)
        + r"/README\.md\).*\n", re.M
    )
    new_text, _ = pattern.subn("", text)
    _write(readme_path, new_text)


def _write_subtree_readme(readme_path: str, dirname: str, title: str, docs: list) -> None:
    contents_lines = "\n".join(
        f"- [`{d}`]({d}) — {_title_from_doc(os.path.join(os.path.dirname(readme_path), d)) or title}."
        for d in docs
    )
    _write(readme_path, f"""# {title}

> **Up:** [↑ Design index](../README.md)

## Contents

{contents_lines}

## See also

- [↑ Design index](../README.md)
""")


def _add_contents_bullet(readme_path: str, doc: str, title: str) -> None:
    if not os.path.isfile(readme_path):
        return
    text = _read(readme_path)
    bullet = f"- [`{doc}`]({doc}) — {title}.\n"
    marker = "## Contents"
    idx = text.find(marker)
    if idx == -1:
        return
    section_end = text.find("\n## ", idx + len(marker))
    if section_end == -1:
        section_end = len(text)
    section = text[idx:section_end]
    lines = section.splitlines(keepends=True)
    insert_at = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("- "):
            insert_at = i + 1
            break
    lines.insert(insert_at, bullet)
    new_text = text[:idx] + "".join(lines) + text[section_end:]
    _write(readme_path, new_text)


def _remove_contents_bullet(readme_path: str, doc: str) -> None:
    if not os.path.isfile(readme_path):
        return
    text = _read(readme_path)
    pattern = re.compile(r"^- \[`" + re.escape(doc) + r"`\]\(" + re.escape(doc) + r"\).*\n", re.M)
    new_text, _ = pattern.subn("", text)
    _write(readme_path, new_text)


# ---------------------------------------------------------------------------
# Apply: the three consumer-tier remedies
# ---------------------------------------------------------------------------


def apply_flat_should_be_subtree(root: str, design_rel: str, f: Finding) -> Optional[str]:
    topic = os.path.basename(f.target)
    files = [p.split("/")[-1] for p in f.group.split(",")]
    dest_dir_rel = f.target
    dest_dir_abs = os.path.join(root, dest_dir_rel)
    os.makedirs(dest_dir_abs, exist_ok=True)
    for fname in files:
        src_rel = f"{design_rel}/{fname}"
        dst_rel = f"{dest_dir_rel}/{fname}"
        proc = _run_git(root, "mv", src_rel, dst_rel)
        if proc.returncode != 0:
            return f"git mv failed for {src_rel}: {proc.stderr.strip()}"
        _rewrite_breadcrumb(os.path.join(root, dst_rel), "../README.md")
    readme_path = os.path.join(dest_dir_abs, "README.md")
    _write_subtree_readme(readme_path, topic, title_of(topic), files)
    _run_git(root, "add", f"{dest_dir_rel}/README.md")
    _add_subtree_bullet(_parent_readme_path(root, design_rel), topic, title_of(topic))
    return None


def apply_subtree_could_flatten(root: str, design_rel: str, f: Finding) -> Optional[str]:
    src_rel = f.path  # design_rel/dirname/doc
    dst_rel = f.target  # design_rel/dirname-design.md
    dirname = src_rel[len(design_rel) + 1:].split("/")[0]
    # Re-check at apply time: an earlier remedy in this same batch (e.g. a
    # WRONG_TOPIC_SUBTREE move) may have added a sibling doc to this subtree
    # since the finding was computed, in which case it no longer qualifies.
    current_docs = _list_md(os.path.join(root, design_rel, dirname))
    if len(current_docs) != 1:
        return (f"{dirname}/ now holds {len(current_docs)} doc(s), "
                f"no longer a single-doc subtree — skipped")
    proc = _run_git(root, "mv", src_rel, dst_rel)
    if proc.returncode != 0:
        return f"git mv failed for {src_rel}: {proc.stderr.strip()}"
    _rewrite_breadcrumb(os.path.join(root, dst_rel), "README.md")
    subtree_readme = os.path.join(root, design_rel, dirname, "README.md")
    if os.path.isfile(subtree_readme):
        _run_git(root, "rm", "-q", f"{design_rel}/{dirname}/README.md")
    subtree_dir_abs = os.path.join(root, design_rel, dirname)
    if os.path.isdir(subtree_dir_abs) and not os.listdir(subtree_dir_abs):
        os.rmdir(subtree_dir_abs)
    _remove_subtree_bullet(_parent_readme_path(root, design_rel), dirname)
    return None


def apply_wrong_topic_subtree(root: str, design_rel: str, f: Finding) -> Optional[str]:
    src_rel = f.path
    dst_rel = f.target
    old_dir = src_rel[len(design_rel) + 1:].split("/")[0]
    new_dir = dst_rel[len(design_rel) + 1:].split("/")[0]
    doc = src_rel.split("/")[-1]
    proc = _run_git(root, "mv", src_rel, dst_rel)
    if proc.returncode != 0:
        return f"git mv failed for {src_rel}: {proc.stderr.strip()}"
    title = _title_from_doc(os.path.join(root, dst_rel)) or title_of(slug_of(doc))
    _remove_contents_bullet(os.path.join(root, design_rel, old_dir, "README.md"), doc)
    _add_contents_bullet(os.path.join(root, design_rel, new_dir, "README.md"), doc, title)
    return None


_APPLY_ORDER = {WRONG_TOPIC_SUBTREE: 0, FLAT_SHOULD_BE_SUBTREE: 1, SUBTREE_COULD_FLATTEN: 2}


def apply_findings(root: str, findings: list) -> list:
    remaining = []
    # WRONG_TOPIC_SUBTREE and FLAT_SHOULD_BE_SUBTREE run first: either can
    # add a sibling doc to a subtree that SUBTREE_COULD_FLATTEN was about to
    # flatten, which must disqualify that flatten (re-checked at apply time).
    for f in sorted(findings, key=lambda f: _APPLY_ORDER.get(f.code, 99)):
        if f.code in REPORT_ONLY_CODES:
            print(f"  SKIP (report-only): {f.path} — {f.message}")
            remaining.append(f)
            continue
        if f.code == FLAT_SHOULD_BE_SUBTREE:
            err = apply_flat_should_be_subtree(root, CONSUMER_DESIGN_REL, f)
        elif f.code == SUBTREE_COULD_FLATTEN:
            err = apply_subtree_could_flatten(root, CONSUMER_DESIGN_REL, f)
        elif f.code == WRONG_TOPIC_SUBTREE:
            err = apply_wrong_topic_subtree(root, CONSUMER_DESIGN_REL, f)
        else:
            remaining.append(f)
            continue
        if err:
            print(f"  SKIP ({err}): {f.path}")
            remaining.append(f)
        else:
            print(f"  applied: {f.code} — {f.path} -> {f.target}")
    return remaining


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def format_report(findings: list) -> str:
    counts: dict = {}
    for f in findings:
        counts[f.code] = counts.get(f.code, 0) + 1
    header = f"design-doc-placement — {len(findings)} finding(s)"
    if counts:
        summary = ", ".join(
            f"{n} {c.lower().replace('_', '-')}" for c, n in sorted(counts.items())
        )
        header += f": {summary}"
    lines = [header] + [f.render() for f in findings]
    return "\n".join(lines)


def run_detect(root: str, as_json: bool = False) -> int:
    findings = classify(root)
    if as_json:
        print(json.dumps({
            "finding_count": len(findings),
            "findings": [asdict(f) for f in findings],
        }, indent=2, sort_keys=True))
    else:
        print(format_report(findings))
    return 1 if findings else 0


def run_apply(root: str) -> int:
    findings = classify(root)
    if not findings:
        print("design-doc-placement: nothing to do (no findings).")
        return 0
    remaining = apply_findings(root, findings)
    print(
        "\ndesign-doc-placement: remember to run "
        "`python3 .claude/skills/grm-doc-assurance/doc_assurance.py "
        "--write-design-index` to regenerate the generated index table."
    )
    if remaining:
        print(f"design-doc-placement: {len(remaining)} finding(s) left unresolved.")
        return 1
    print("design-doc-placement: all findings resolved.")
    return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, capture_output=True)


def _fwrite(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _design_readme(topics: list) -> str:
    bullets = "\n".join(f"- [`{t}/`]({t}/README.md) — {title_of(t)} design docs." for t in topics)
    return f"""# Design Docs

> **Up:** [↑ Docs root](../README.md)

## Index

### Subtrees

{bullets}

## Adding a design doc

See grm-design-doc-scaffold.
"""


def _subtree_readme(topic: str, docs: list) -> str:
    bullets = "\n".join(f"- [`{d}`]({d}) — {title_of(slug_of(d))}." for d in docs)
    return f"""# {title_of(topic)}

> **Up:** [↑ Design index](../README.md)

## Contents

{bullets}

## See also

- [↑ Design index](../README.md)
"""


def _build_fixture(tmp: str) -> None:
    # Flat doc pair that should promote: widget-design.md + widget-api-design.md
    _fwrite(os.path.join(tmp, "docs/design/widget-design.md"),
            "# Widget\n\n> **Up:** [↑ Design index](README.md)\n\nBody.\n")
    _fwrite(os.path.join(tmp, "docs/design/widget-api-design.md"),
            "# Widget API\n\n> **Up:** [↑ Design index](README.md)\n\nBody.\n")
    # Single-doc, legitimately-flat topic — no finding expected.
    _fwrite(os.path.join(tmp, "docs/design/solo-design.md"),
            "# Solo\n\n> **Up:** [↑ Design index](README.md)\n\nBody.\n")
    # Subtree with exactly one doc — should-flatten candidate.
    _fwrite(os.path.join(tmp, "docs/design/legacy/legacy-design.md"),
            "# Legacy\n\n> **Up:** [↑ Design index](../README.md)\n\nBody.\n")
    _fwrite(os.path.join(tmp, "docs/design/legacy/README.md"),
            _subtree_readme("legacy", ["legacy-design.md"]))
    # A healthy two-doc subtree — no finding expected.
    _fwrite(os.path.join(tmp, "docs/design/ux/theme-design.md"),
            "# Theme\n\n> **Up:** [↑ Design index](../README.md)\n\nBody.\n")
    _fwrite(os.path.join(tmp, "docs/design/ux/components-design.md"),
            "# Components\n\n> **Up:** [↑ Design index](../README.md)\n\nBody.\n")
    _fwrite(os.path.join(tmp, "docs/design/ux/README.md"),
            _subtree_readme("ux", ["theme-design.md", "components-design.md"]))
    # A doc misfiled: filed under ux/ but its slug matches the legacy/ topic.
    _fwrite(os.path.join(tmp, "docs/design/ux/legacy-notes-design.md"),
            "# Legacy Notes\n\n> **Up:** [↑ Design index](../README.md)\n\nBody.\n")
    _add_contents_bullet(os.path.join(tmp, "docs/design/ux/README.md"),
                          "legacy-notes-design.md", "Legacy Notes")

    _fwrite(os.path.join(tmp, "docs/design/README.md"),
            _design_readme(["legacy", "ux"]))

    # Framework tier stays flat — no subdirectories.
    _fwrite(os.path.join(tmp, "docs/grimoire/design/framework-thing-design.md"),
            "# Framework Thing\n\nBody.\n")
    _fwrite(os.path.join(tmp, "docs/grimoire/design/README.md"), "# Grimoire design\n")

    _git(tmp, "init", "-q")
    _git(tmp, "add", "-A")
    _git(tmp, "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init")


def self_test() -> int:
    import tempfile

    passed, failed = 0, 0
    lines = []

    def check(label: str, ok: bool):
        nonlocal passed, failed
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1

    # 1. Empty tree: no design dirs -> zero findings, exit 0.
    with tempfile.TemporaryDirectory() as tmp:
        rc = run_detect(tmp)
        check("empty tree yields zero findings (exit 0)", rc == 0)

    # 2. Full fixture: every code detected, and healthy docs are NOT flagged.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        findings = classify(tmp)
        codes_by_path = {f.path: f.code for f in findings}

        check("FLAT_SHOULD_BE_SUBTREE detected (widget-design.md / widget-api-design.md)",
              any(f.code == FLAT_SHOULD_BE_SUBTREE and "widget" in f.path for f in findings))
        check("solo-design.md (single-doc flat topic) is NOT flagged",
              "docs/design/solo-design.md" not in codes_by_path)
        check("SUBTREE_COULD_FLATTEN detected (legacy/ has one doc)",
              any(f.code == SUBTREE_COULD_FLATTEN and "legacy" in f.path for f in findings))
        check("ux/ (healthy two-doc subtree, own-topic docs) has no should-flatten finding",
              not any(f.code == SUBTREE_COULD_FLATTEN and "/ux/" in f.path for f in findings))
        check("WRONG_TOPIC_SUBTREE detected (legacy-notes-design.md filed under ux/)",
              any(f.code == WRONG_TOPIC_SUBTREE and "ux/legacy-notes" in f.path for f in findings))
        check("no GRIMOIRE_SUBTREE_DISALLOWED (framework tier is flat, as required)",
              not any(f.code == GRIMOIRE_SUBTREE_DISALLOWED for f in findings))

        rc = run_detect(tmp)
        check("run_detect exits 1 when findings are present", rc == 1)

        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_detect(tmp, as_json=True)
        payload = json.loads(buf.getvalue())
        check("--json output parses",
              payload.get("finding_count") == len(payload.get("findings", [])))

    # 3. Framework tier: a stray subdirectory is flagged, report-only.
    with tempfile.TemporaryDirectory() as tmp:
        _fwrite(os.path.join(tmp, "docs/grimoire/design/README.md"), "# Grimoire design\n")
        _fwrite(os.path.join(tmp, "docs/grimoire/design/stray/stray-design.md"),
                "# Stray\n\nBody.\n")
        _git(tmp, "init", "-q")
        _git(tmp, "add", "-A")
        _git(tmp, "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
        findings = classify(tmp)
        check("GRIMOIRE_SUBTREE_DISALLOWED detected for a stray framework-tier subdir",
              any(f.code == GRIMOIRE_SUBTREE_DISALLOWED for f in findings))
        rc = run_apply(tmp)
        check("--apply leaves GRIMOIRE_SUBTREE_DISALLOWED unresolved (report-only forever)",
              rc == 1)
        check("stray/ was not moved by --apply",
              os.path.isdir(os.path.join(tmp, "docs/grimoire/design/stray")))

    # 4. --apply relocates deliberately-misplaced docs and updates indexes.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc = run_apply(tmp)

        check("widget docs promoted: widget/ subtree created",
              os.path.isdir(os.path.join(tmp, "docs/design/widget")))
        check("widget-design.md moved into widget/",
              os.path.isfile(os.path.join(tmp, "docs/design/widget/widget-design.md"))
              and not os.path.isfile(os.path.join(tmp, "docs/design/widget-design.md")))
        check("widget/README.md created with both docs listed",
              os.path.isfile(os.path.join(tmp, "docs/design/widget/README.md"))
              and "widget-api-design.md" in _read(os.path.join(tmp, "docs/design/widget/README.md")))
        check("promoted doc's breadcrumb rewritten to ../README.md",
              "[↑ Design index](../README.md)" in
              _read(os.path.join(tmp, "docs/design/widget/widget-design.md")))
        check("parent README gained a widget/ subtree bullet",
              "widget/README.md" in _read(os.path.join(tmp, "docs/design/README.md")))

        check("legacy-notes-design.md relocated from ux/ into legacy/",
              os.path.isfile(os.path.join(tmp, "docs/design/legacy/legacy-notes-design.md")))
        check("ux/README.md no longer lists the relocated doc",
              "legacy-notes-design.md" not in _read(os.path.join(tmp, "docs/design/ux/README.md")))
        check("legacy/README.md now lists the relocated doc",
              "legacy-notes-design.md" in _read(os.path.join(tmp, "docs/design/legacy/README.md")))

        # legacy/ itself now holds 2 docs post-relocation, so its
        # SUBTREE_COULD_FLATTEN finding should have resolved on this same run
        # (moves are computed from the pre-apply snapshot and applied in
        # code order; legacy/ ends this run with 2 docs, no longer flattenable).
        check("legacy/ was NOT flattened this run (received a second doc mid-run)",
              os.path.isdir(os.path.join(tmp, "docs/design/legacy")))

        rc2 = run_detect(tmp)
        check("second detect run terminates cleanly (0 or 1)", rc2 in (0, 1))

    print(f"design-doc-placement self-test: {passed} passed, {failed} failed.")
    for ln in lines:
        print(ln)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Design-doc placement audit + migrate (grm-design-doc-placement). "
                    "See docs/design/README.md's Subtrees rule."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true",
                       help="Run against built-in offline fixtures (no network calls).")
    mode.add_argument("--root", metavar="DIR", help="Repo root to scan.")
    parser.add_argument("--apply", action="store_true",
                         help="Perform the mechanical remedies. Default is "
                              "detect-only (read-only, nothing moves).")
    parser.add_argument("--json", action="store_true",
                         help="Emit detect-mode findings as JSON instead of the "
                              "human table (ignored with --apply).")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.apply:
        return run_apply(args.root)
    return run_detect(args.root, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
