#!/usr/bin/env python3
"""context_pack.py — brief-as-file dispatch materialization (#397).

Problem: `grm-release-phase` Step 5 used to have the dispatching master
re-type a ~800-token shared-context digest into every dispatched agent's
prompt (identical text pasted N times for an N-item batch), and copy each
item's plan prose (description + acceptance criteria) into its own prompt
too — output tokens the master pays N times for content that's either
identical across the batch or already sitting in a tracked file one section
away. This script materializes both as small, git-tracked files instead, so
the master writes each once and every dispatched prompt carries only a path
reference:

  - **phase-brief** — one shared brief per phase/batch: the standards
    excerpt, the plan's `## 3. Parallel Implementation Strategy` conflict-map
    section (verbatim, not re-typed), and the batch's item-id list. Written
    once per batch regardless of N.
  - **context-pack** — one small per-item file: the exact `### {ITEM-ID}`
    block (description + acceptance criteria + branch) extracted verbatim
    from the release plan via `grm-doc-section`'s `extract_section()` — no
    re-typing, so it can never drift from the plan text it was pulled from.

Both write under `.claude/release-dispatch/v{X.Y}/phase{N}/` — a tracked
(not gitignored) directory, unlike `.claude/cache/`. That's deliberate: each
dispatched agent runs in its own isolated `git worktree` (a genuinely
separate working directory sharing only the `.git` object store), so an
untracked/gitignored file the master writes in its own worktree is invisible
to a freshly spawned sibling worktree. Committing these files to
`version/{X.Y}` (the master's own branch, per `grm-integration-master`
SKILL.md) before dispatch means every subagent's `git switch -c {branch}
version/{X.Y}` inherits them for free — no re-typing, no extra prompt
tokens, no per-agent cold read of the full plan file.

Lifecycle: `grm-release-phase` writes+commits these once per batch, right
before Step 5's dispatch calls. `grm-release-phase-merge` is expected to
`git rm -r` a phase's pack directory once every branch in that phase's batch
has merged (follow-up; not wired by this script — see its own SKILL.md).

Standard: Python 3 stdlib-only (docs/grimoire/design/scripting-unification-design.md).

CLI:
  context_pack.py phase-brief --plan FILE --version X.Y --phase N \
      --items ITEM-ID[,ITEM-ID...] [--out-dir DIR] [--root DIR]
  context_pack.py context-pack --plan FILE --version X.Y --phase N \
      --item ITEM-ID [--out-dir DIR] [--root DIR]
  context_pack.py measure --plan FILE --items ITEM-ID[,ITEM-ID...] \
      [--version X.Y] [--phase N] [--out-dir DIR] [--root DIR] [--json]
  context_pack.py --self-test
Exit 0 on success; 2 on bad input / missing section.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# ── Constants (no magic numbers inline) ─────────────────────────────────────
DISPATCH_ROOT = os.path.join(".claude", "release-dispatch")
TARGET_HEADING = "## 1. Target"
CONFLICT_MAP_HEADING = "## 3. Parallel Implementation Strategy"
# ~4 bytes/token, the same coarse heuristic used elsewhere in this repo
# (context-efficiency-design.md's baseline table; grm-token-measure).
BYTES_PER_TOKEN = 4
# Size of the small per-prompt pointer block that REPLACES the old inlined
# content in each dispatched agent's prompt (measured from the actual
# template text in SKILL.md's Step 5 "### Shared context" / "### Your item
# context" blocks — see reference.md §Shared-context dispatch for the
# template this mirrors).
POINTER_BLOCK_TEMPLATE = """### Shared context
Read `{brief_path}` for the standards excerpt, this batch's conflict-map
slice, and the release theme. Shared across every item in this batch.

### Your item context
Read `{pack_path}` for {item_id}'s full scope, acceptance criteria, and
branch name — extracted verbatim from the release plan.
"""

STANDARDS_EXCERPT = """## Standards (shared across this batch)
- `docs/coding-standards.md` + `docs/architecture-guidelines.md` — house
  style, error handling, one-file-per-class, no magic numbers/duplication.
- Check `.claude/component-registry.json` and `vendor.toml` for an existing
  capability before writing new infrastructure; report any reuse opportunity
  the plan missed as a follow-up row.
- `docs/project-structure.md` — where new files belong (src/, tests/,
  lib/first-party/, etc).
- Before finishing: `python3 .claude/skills/grm-build-recipe/recipe.py test`
  and `... recipe.py build` must be clean; fix all errors/warnings you
  introduced.
- Doc-location pointers: see the `grm-repo-reference` skill for design-doc /
  wiki-hierarchy paths.
"""


def _doc_section_module():
    """Import `extract_section`/`SectionNotFoundError` from grm-doc-section
    (#407), following this repo's cross-skill import convention (see
    `grm-doc-section/SKILL.md` §Library usage)."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))  # .claude/skills/<this> -> .claude/skills -> root-ish
    # here = .../.claude/skills/grm-release-phase ; sibling = .../.claude/skills/grm-doc-section
    doc_section_dir = os.path.join(os.path.dirname(here), "grm-doc-section")
    if doc_section_dir not in sys.path:
        sys.path.insert(0, doc_section_dir)
    import doc_section  # type: ignore
    return doc_section


class ContextPackError(Exception):
    """Raised on a missing plan section or malformed call (-> exit 2)."""


def _out_dir(root, version, phase, out_dir=None):
    if out_dir:
        return out_dir
    return os.path.join(root, DISPATCH_ROOT, f"v{version}", f"phase{phase}")


def extract_item_block(plan_path, item_id, doc_section=None):
    """Return the verbatim `### {item_id}` block (description, acceptance
    criteria, branch) from the plan file. Raises ContextPackError if the
    item heading isn't found (never returns a silent partial match)."""
    ds = doc_section or _doc_section_module()
    heading = f"### {item_id}"
    try:
        return ds.extract_section(plan_path, heading=heading)
    except ds.SectionNotFoundError as e:
        raise ContextPackError(str(e)) from e


def build_context_pack(plan_path, item_id, out_dir, doc_section=None):
    """Write {out_dir}/{item_id}.md with the item's verbatim plan block.
    Returns (path, byte_size)."""
    block = extract_item_block(plan_path, item_id, doc_section=doc_section)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{item_id}.md")
    content = (
        f"<!-- Generated by context_pack.py (#397) from {plan_path} — do not "
        f"hand-edit; regenerate instead. -->\n\n{block}"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path, len(content.encode("utf-8"))


def build_phase_brief(plan_path, version, phase, item_ids, out_dir, doc_section=None):
    """Write {out_dir}/brief.md with the shared standards excerpt, the
    plan's full §3 conflict-map section, and the batch's item-id list.
    Returns (path, byte_size)."""
    ds = doc_section or _doc_section_module()
    try:
        target = ds.extract_section(plan_path, heading=TARGET_HEADING)
    except ds.SectionNotFoundError as e:
        raise ContextPackError(str(e)) from e
    try:
        conflict_map = ds.extract_section(plan_path, heading=CONFLICT_MAP_HEADING)
    except ds.SectionNotFoundError as e:
        raise ContextPackError(str(e)) from e

    items_list = "\n".join(f"- {iid}" for iid in item_ids)
    content = (
        f"<!-- Generated by context_pack.py (#397) from {plan_path} — do not "
        f"hand-edit; regenerate instead. -->\n\n"
        f"# Phase {phase} shared dispatch brief (v{version})\n\n"
        f"This batch: {len(item_ids)} item(s)\n{items_list}\n\n"
        f"{STANDARDS_EXCERPT}\n"
        f"{target}\n"
        f"{conflict_map}\n"
    )
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "brief.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path, len(content.encode("utf-8"))


def pointer_block(brief_path, pack_path, item_id):
    return POINTER_BLOCK_TEMPLATE.format(
        brief_path=brief_path, pack_path=pack_path, item_id=item_id
    )


def measure(plan_path, item_ids, version, phase, out_dir, doc_section=None):
    """Compute a before/after prompt-content size comparison for an N-item
    batch dispatch.

    BEFORE (today's inlined model): for each item, the master re-types the
    shared ~brief text plus that item's full plan block into the prompt —
    paid N times.
    AFTER (brief-as-file model): the master writes the brief once + one pack
    per item (paid once each, via file write, not retyped per prompt), and
    each prompt carries only the small pointer block.

    Returns a dict with byte/token counts for both and the reduction.
    """
    ds = doc_section or _doc_section_module()
    try:
        target = ds.extract_section(plan_path, heading=TARGET_HEADING)
    except ds.SectionNotFoundError as e:
        raise ContextPackError(str(e)) from e
    try:
        conflict_map = ds.extract_section(plan_path, heading=CONFLICT_MAP_HEADING)
    except ds.SectionNotFoundError as e:
        raise ContextPackError(str(e)) from e

    shared_digest = f"{STANDARDS_EXCERPT}\n{target}\n{conflict_map}\n"
    shared_digest_bytes = len(shared_digest.encode("utf-8"))

    item_blocks = {}
    for iid in item_ids:
        item_blocks[iid] = extract_item_block(plan_path, iid, doc_section=ds)

    before_bytes = 0
    after_bytes = 0
    brief_path = os.path.join(_out_dir(".", version, phase, out_dir), "brief.md")
    for iid in item_ids:
        block = item_blocks[iid]
        # BEFORE: shared digest + full item block re-typed into this prompt.
        before_bytes += shared_digest_bytes + len(block.encode("utf-8"))
        # AFTER: only the small pointer block appears in the prompt.
        pack_path = os.path.join(_out_dir(".", version, phase, out_dir), f"{iid}.md")
        after_bytes += len(pointer_block(brief_path, pack_path, iid).encode("utf-8"))

    # AFTER also pays the one-time file-write cost (brief + N packs), but
    # that's written via this script's file I/O, not the master's own output
    # tokens — reported separately for transparency, not added into the
    # per-prompt total the master's turn actually pays for.
    one_time_bytes = shared_digest_bytes + sum(
        len(b.encode("utf-8")) for b in item_blocks.values()
    )

    n = len(item_ids)
    result = {
        "n_items": n,
        "before_prompt_bytes": before_bytes,
        "after_prompt_bytes": after_bytes,
        "before_prompt_tokens_est": before_bytes // BYTES_PER_TOKEN,
        "after_prompt_tokens_est": after_bytes // BYTES_PER_TOKEN,
        "one_time_file_bytes": one_time_bytes,
        "one_time_file_tokens_est": one_time_bytes // BYTES_PER_TOKEN,
        "reduction_bytes": before_bytes - after_bytes,
        "reduction_pct": round(100.0 * (before_bytes - after_bytes) / before_bytes, 1)
        if before_bytes else 0.0,
    }
    return result


# ── CLI ──────────────────────────────────────────────────────────────────
def _cmd_phase_brief(args):
    doc_section = _doc_section_module()
    item_ids = [s.strip() for s in args.items.split(",") if s.strip()]
    out_dir = _out_dir(args.root, args.version, args.phase, args.out_dir)
    try:
        path, size = build_phase_brief(
            args.plan, args.version, args.phase, item_ids, out_dir,
            doc_section=doc_section,
        )
    except ContextPackError as e:
        print(f"context_pack phase-brief: {e}", file=sys.stderr)
        return 2
    print(f"{path} ({size} bytes)")
    return 0


def _cmd_context_pack(args):
    doc_section = _doc_section_module()
    out_dir = _out_dir(args.root, args.version, args.phase, args.out_dir)
    try:
        path, size = build_context_pack(
            args.plan, args.item, out_dir, doc_section=doc_section
        )
    except ContextPackError as e:
        print(f"context_pack context-pack: {e}", file=sys.stderr)
        return 2
    print(f"{path} ({size} bytes)")
    return 0


def _cmd_measure(args):
    doc_section = _doc_section_module()
    item_ids = [s.strip() for s in args.items.split(",") if s.strip()]
    out_dir = _out_dir(args.root, args.version or "X.Y", args.phase or 0, args.out_dir)
    try:
        result = measure(
            args.plan, item_ids, args.version or "X.Y", args.phase or 0, out_dir,
            doc_section=doc_section,
        )
    except ContextPackError as e:
        print(f"context_pack measure: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"N={result['n_items']} items")
        print(
            f"before: {result['before_prompt_bytes']} bytes "
            f"(~{result['before_prompt_tokens_est']} tok) inlined across the batch"
        )
        print(
            f"after:  {result['after_prompt_bytes']} bytes "
            f"(~{result['after_prompt_tokens_est']} tok) of pointer text across the batch"
        )
        print(
            f"one-time file writes: {result['one_time_file_bytes']} bytes "
            f"(~{result['one_time_file_tokens_est']} tok), paid once not N times"
        )
        print(
            f"reduction: {result['reduction_bytes']} bytes "
            f"({result['reduction_pct']}%) off the master's own per-batch prompt tokens"
        )
    return 0


# ── self-test ─────────────────────────────────────────────────────────────
def _self_test() -> int:
    import tempfile

    failures = []

    def check(label, fn):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{label}: {e}")
        except Exception as e:  # unexpected exception is also a failure
            failures.append(f"{label}: unexpected {type(e).__name__}: {e}")

    fixture_plan = """\
# Release Planning — v9.99

> status: agreed

## 1. Target

| | |
|---|---|
| **Version** | `v9.99` |
| **Theme** | "Fixture theme" |

## 2. Major Features

### ITEM-1 — #1: first fixture item
**Description:** does the first thing.
**Acceptance criteria:** first thing works.
**Branch:** `claude/r0-1-first`

### ITEM-2 — #2: second fixture item
**Description:** does the second thing, at some length so the block is a
realistic size for a token-count comparison rather than a one-liner.
**Acceptance criteria:** second thing works; also handles the edge case.
**Branch:** `claude/r0-2-second`

## 3. Parallel Implementation Strategy

**Pass 1 (independent, dispatch together):**
- ITEM-1 (#1), ITEM-2 (#2)

No file overlap between these two.

## 5. Status Ledger
"""

    doc_section = _doc_section_module()

    with tempfile.TemporaryDirectory() as td:
        plan_path = os.path.join(td, "release-planning-v9.99.md")
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write(fixture_plan)
        out_dir = os.path.join(td, "dispatch", "v9.99", "phase1")

        # 1. context-pack extracts exactly one item, not its neighbor.
        def t1():
            path, size = build_context_pack(
                plan_path, "ITEM-1", out_dir, doc_section=doc_section
            )
            assert os.path.exists(path)
            text = open(path, encoding="utf-8").read()
            assert "### ITEM-1" in text
            assert "first thing" in text
            assert "ITEM-2" not in text, "must not bleed into the next item's block"
            assert size == len(open(path, "rb").read())

        check("context-pack extracts exactly one item", t1)

        # 2. context-pack on a missing item fails loud.
        def t2():
            try:
                build_context_pack(
                    plan_path, "ITEM-99", out_dir, doc_section=doc_section
                )
                assert False, "expected ContextPackError, got no exception"
            except ContextPackError:
                pass

        check("context-pack on missing item fails loud", t2)

        # 3. phase-brief contains standards excerpt, conflict map, and the
        #    item-id list, and is written once regardless of batch size.
        def t3():
            path, _size = build_phase_brief(
                plan_path, "9.99", 1, ["ITEM-1", "ITEM-2"], out_dir,
                doc_section=doc_section,
            )
            text = open(path, encoding="utf-8").read()
            assert "docs/coding-standards.md" in text
            assert "Parallel Implementation Strategy" in text
            assert "Pass 1" in text
            assert "ITEM-1" in text and "ITEM-2" in text
            assert "Fixture theme" in text

        check("phase-brief bundles standards + conflict map + item list", t3)

        # 4. measure: after < before, positive reduction, and matches a
        #    hand-computed relationship (after must equal N * pointer size,
        #    not depend on item-block size).
        def t4():
            result = measure(
                plan_path, ["ITEM-1", "ITEM-2"], "9.99", 1, out_dir,
                doc_section=doc_section,
            )
            assert result["n_items"] == 2
            assert result["after_prompt_bytes"] < result["before_prompt_bytes"], (
                result
            )
            assert result["reduction_pct"] > 0
            # AFTER scales with N * (fixed pointer size), not with item-block
            # size — doubling batch size should not roughly double AFTER the
            # way it would double BEFORE (BEFORE repeats the full digest+
            # block per item; AFTER repeats only the short pointer).
            single = measure(
                plan_path, ["ITEM-1"], "9.99", 1, out_dir, doc_section=doc_section
            )
            assert result["after_prompt_bytes"] < 2.5 * single["after_prompt_bytes"], (
                "AFTER should scale near-linearly with a small per-item pointer, "
                "not balloon like BEFORE"
            )

        check("measure shows before>after with a positive reduction", t4)

        # 5. measure on a missing item fails loud (never silently drops it).
        def t5():
            try:
                measure(
                    plan_path, ["ITEM-1", "ITEM-404"], "9.99", 1, out_dir,
                    doc_section=doc_section,
                )
                assert False, "expected ContextPackError, got no exception"
            except ContextPackError:
                pass

        check("measure on a missing item fails loud", t5)

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        print(f"context_pack self-test: {len(failures)} failure(s)", file=sys.stderr)
        return 1

    print("context_pack self-test: OK (5 cases)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Materialize grm-release-phase's shared dispatch brief + "
                     "per-item context packs as tracked files (#397)."
    )
    ap.add_argument("--root", default=".", help="repo root (default: .)")
    sub = ap.add_subparsers(dest="verb")

    p_brief = sub.add_parser("phase-brief", help="write the shared batch brief once")
    p_brief.add_argument("--plan", required=True, metavar="FILE")
    p_brief.add_argument("--version", required=True, metavar="X.Y")
    p_brief.add_argument("--phase", required=True, type=int, metavar="N")
    p_brief.add_argument("--items", required=True, metavar="ITEM-ID[,ITEM-ID...]")
    p_brief.add_argument("--out-dir", metavar="DIR")

    p_pack = sub.add_parser("context-pack", help="write one item's pack")
    p_pack.add_argument("--plan", required=True, metavar="FILE")
    p_pack.add_argument("--version", required=True, metavar="X.Y")
    p_pack.add_argument("--phase", required=True, type=int, metavar="N")
    p_pack.add_argument("--item", required=True, metavar="ITEM-ID")
    p_pack.add_argument("--out-dir", metavar="DIR")

    p_measure = sub.add_parser("measure", help="before/after prompt-size comparison")
    p_measure.add_argument("--plan", required=True, metavar="FILE")
    p_measure.add_argument("--items", required=True, metavar="ITEM-ID[,ITEM-ID...]")
    p_measure.add_argument("--version", metavar="X.Y")
    p_measure.add_argument("--phase", type=int, metavar="N")
    p_measure.add_argument("--out-dir", metavar="DIR")
    p_measure.add_argument("--json", action="store_true")

    ap.add_argument("--self-test", action="store_true")

    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.verb == "phase-brief":
        return _cmd_phase_brief(args)
    if args.verb == "context-pack":
        return _cmd_context_pack(args)
    if args.verb == "measure":
        return _cmd_measure(args)

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
