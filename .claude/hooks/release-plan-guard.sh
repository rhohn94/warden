#!/usr/bin/env python3
"""Release-plan guard.

Blocks writes to the agreed scope (§§1-4) of any docs/release-planning-v*.md
file whose header contains a `status: agreed` line.  §5 (the implementation
ledger) is always writeable — that's the domain of ledger-tick and
release-phase-merge.

Rules:
  - Write tool (full overwrite) on an agreed file → blocked always.
  - Edit tool on an agreed file → allowed only if the old_string lands on or
    after the `## 5.` heading line (i.e. inside the ledger).
  - Any tool on a file with `status: draft` → allowed.

To revise agreed scope: get explicit user confirmation, edit the
`status: agreed` line to `status: revising`, make the scope change, then
return it to `status: agreed` in the same commit.
"""
import fnmatch
import json
import os
import re
import sys


def is_release_plan(path: str) -> bool:
    return fnmatch.fnmatch(os.path.basename(path), "release-planning-v*.md")


def is_agreed(path: str) -> bool:
    """Return True if the file's header contains 'status: agreed'."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 15:
                    break
                if re.search(r"status:\s*agreed", line, re.IGNORECASE):
                    return True
    except OSError:
        pass
    return False


def section5_start_line(path: str) -> int:
    """Return the 0-indexed line number of the first '## 5.' heading, or -1."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if re.match(r"^## 5\b", line):
                    return i
    except OSError:
        pass
    return -1


def old_string_line(path: str, old_string: str) -> int:
    """Return the 0-indexed line of the first occurrence of old_string, or -1."""
    if not old_string:
        return -1
    try:
        content = open(path, "r", encoding="utf-8").read()
        pos = content.find(old_string)
        if pos == -1:
            return -1
        return content[:pos].count("\n")
    except OSError:
        return -1


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input", {}) or {}

    if tool not in ("Edit", "Write", "NotebookEdit"):
        sys.exit(0)

    fp = tin.get("file_path") or tin.get("notebook_path") or ""
    if not fp:
        sys.exit(0)

    if not os.path.isabs(fp):
        fp = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""), fp)

    if not is_release_plan(fp):
        sys.exit(0)

    if not is_agreed(fp):
        sys.exit(0)

    # ── File is agreed ──────────────────────────────────────────────────────

    if tool == "Write":
        sys.stderr.write(
            "release-plan-guard: blocked full Write to an agreed release plan.\n"
            f"  file: {fp}\n"
            "  Agreed plans are locked for full rewrites. Use Edit to update\n"
            "  the §5 ledger only. To revise agreed scope, get explicit user\n"
            "  confirmation and change 'status: agreed' → 'status: revising'.\n"
        )
        sys.exit(2)

    if tool in ("Edit", "NotebookEdit"):
        old = tin.get("old_string", "")

        # Sanctioned unlock: an edit that targets ONLY the status line
        # (agreed ⇄ revising ⇄ draft) is always allowed.
        if re.fullmatch(
            r">?\s*status:\s*(agreed|revising|draft)\s*",
            old.strip(),
            re.IGNORECASE,
        ):
            sys.exit(0)

        s5 = section5_start_line(fp)
        edit_line = old_string_line(fp, old)

        if s5 == -1:
            # Can't find §5 marker — allow rather than over-block
            sys.exit(0)

        if edit_line == -1 or edit_line >= s5:
            # Edit lands in §5 or location unknown — allow
            sys.exit(0)

        # Edit lands in §§1-4 — block
        sys.stderr.write(
            "release-plan-guard: blocked edit to agreed scope (§§1–4).\n"
            f"  file:         {fp}\n"
            f"  edit at line: {edit_line + 1}  (§5 starts at line {s5 + 1})\n"
            "  The scope of an agreed release plan is frozen. Only the §5\n"
            "  status ledger may change. To revise agreed scope, get explicit\n"
            "  user confirmation and set 'status: revising' first.\n"
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
