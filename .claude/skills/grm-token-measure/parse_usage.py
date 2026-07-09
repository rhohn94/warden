#!/usr/bin/env python3
"""Token-usage parser for Claude Code session transcripts (read-only).

Sums the four Anthropic token classes (input, output, cache_read,
cache_creation) per operation/turn from a session `.jsonl` transcript and
emits the per-class report table defined in the token-efficiency design doc.

Authoritative source: `assistant` records carry a `message.usage` object with
`input_tokens`, `output_tokens`, `cache_read_input_tokens`, and
`cache_creation_input_tokens`. Records sharing a `requestId` are streamed
fragments of ONE API response and repeat identical usage, so usage is counted
once per `requestId` (deduplication is load-bearing for correctness).
`<synthetic>` assistant records carry zero usage and are skipped.

Operations are delimited by real user prompts (a `user` record whose content
is a string, or a content list with no `tool_result` block); tool-result user
records continue the current operation rather than starting a new one.

Read-only: parses transcripts and prints a report; mutates nothing. Redirect
stdout to capture the report into a file.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field

# Exact usage key names confirmed empirically against Claude Code transcripts.
KEY_INPUT = "input_tokens"
KEY_OUTPUT = "output_tokens"
KEY_CACHE_READ = "cache_read_input_tokens"
KEY_CACHE_CREATION = "cache_creation_input_tokens"

SYNTHETIC_MODEL = "<synthetic>"

# Relative per-token cost weights by class, from the token-efficiency design
# doc (§Token classes, cheapest -> most expensive). Output is the anchor (1.0);
# the others are fractions of output. These are deliberately relative — the
# report states the cost column is an estimate, not dollars.
CLASS_WEIGHT = {
    "cache_read": 0.08,      # cheapest class, by a wide margin
    "input": 1.00 / 3.0,     # baseline cold input ~ 1/3 of output
    "cache_creation": 1.25 / 3.0,  # ~25% premium over plain input
    "output": 1.00,          # most expensive class
}

# Model-tier multipliers relative to Haiku (design doc §Model-tier multipliers:
# Opus ~= 5x Sonnet, Sonnet ~= 3x Haiku, Opus ~= 15x Haiku).
TIER_MULTIPLIER = {
    "haiku": 1.0,
    "sonnet": 3.0,
    "opus": 15.0,
}
DEFAULT_TIER_MULTIPLIER = TIER_MULTIPLIER["sonnet"]


# Claude Code stores per-project transcripts under this root, in a directory
# whose name is the project's absolute path with EACH non-alphanumeric
# character replaced by a single dash (verified empirically against this repo:
# `/`, space, and `.` each map to one `-`, so a `/.claude` segment yields
# `--claude` — slash and dot are two characters, hence two dashes).
TRANSCRIPTS_ROOT = os.path.expanduser(os.path.join("~", ".claude", "projects"))
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


class TranscriptError(Exception):
    """Raised when a transcript cannot be read or contains no usable usage."""


def encode_project_dir(cwd: str) -> str:
    """Encode an absolute project path to its `~/.claude/projects/` dir name.

    Claude Code replaces EACH non-alphanumeric character in the absolute path
    with a single dash. A leading slash yields a leading dash; a `/.claude`
    segment yields `--claude` (slash and dot are two characters, two dashes).
    The transform is deterministic, so the dir name can be reconstructed offline
    without scanning the filesystem.
    """
    return _NON_ALNUM_RE.sub("-", cwd)


def locate_transcript(cwd: str | None = None, root: str | None = None):
    """Resolve the most recent session transcript for `cwd` (default: real cwd).

    Returns the path to the newest `*.jsonl` under
    `~/.claude/projects/<encoded-cwd>/`, or raises TranscriptError if the
    project directory or any transcript is absent. `root` overrides the
    projects root (used by the self-test). Read-only — touches nothing.
    """
    cwd = os.path.abspath(cwd or os.getcwd())
    root = root or TRANSCRIPTS_ROOT
    project_dir = os.path.join(root, encode_project_dir(cwd))
    if not os.path.isdir(project_dir):
        raise TranscriptError(
            "no transcript directory for %r at %s "
            "(session may not have been recorded, or cwd differs from the "
            "session's project root)" % (cwd, project_dir))
    candidates = glob.glob(os.path.join(project_dir, "*.jsonl"))
    if not candidates:
        raise TranscriptError(
            "transcript directory %s exists but holds no *.jsonl files"
            % project_dir)
    # Newest by mtime — the current/most-recent session.
    return max(candidates, key=os.path.getmtime)


@dataclass
class ClassTotals:
    """Accumulator for the four token classes."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    def add_usage(self, usage: dict) -> None:
        self.input += int(usage.get(KEY_INPUT, 0) or 0)
        self.output += int(usage.get(KEY_OUTPUT, 0) or 0)
        self.cache_read += int(usage.get(KEY_CACHE_READ, 0) or 0)
        self.cache_creation += int(usage.get(KEY_CACHE_CREATION, 0) or 0)

    def is_empty(self) -> bool:
        return not (self.input or self.output or self.cache_read or self.cache_creation)


@dataclass
class Operation:
    """One operation (a user-prompt turn and the assistant work it drove)."""

    label: str
    totals: ClassTotals = field(default_factory=ClassTotals)
    models: set = field(default_factory=set)


def _tier_for_model(model: str | None) -> float:
    """Map a model id to its Haiku-relative tier multiplier."""
    if not model:
        return DEFAULT_TIER_MULTIPLIER
    name = model.lower()
    for tier, mult in TIER_MULTIPLIER.items():
        if tier in name:
            return mult
    return DEFAULT_TIER_MULTIPLIER


def estimate_cost(totals: ClassTotals, tier_multiplier: float) -> float:
    """Relative cost estimate: class-weighted token sum times the tier rate."""
    weighted = (
        totals.input * CLASS_WEIGHT["input"]
        + totals.output * CLASS_WEIGHT["output"]
        + totals.cache_read * CLASS_WEIGHT["cache_read"]
        + totals.cache_creation * CLASS_WEIGHT["cache_creation"]
    )
    return weighted * tier_multiplier


def _is_new_operation(record: dict) -> bool:
    """A real user prompt starts a new operation; tool results do not."""
    if record.get("type") != "user":
        return False
    content = record.get("message", {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def _prompt_label(record: dict, index: int) -> str:
    """Short label for an operation, derived from the user prompt text."""
    content = record.get("message", {}).get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                text = b.get("text", "")
                break
    text = " ".join(text.split())
    if not text:
        return f"op {index}"
    return text[:60] + ("..." if len(text) > 60 else "")


def iter_records(path: str):
    """Yield parsed JSON records from a .jsonl file, skipping blank/garbled lines.

    Raises TranscriptError if the file cannot be opened. Malformed individual
    lines are skipped but counted (reported as a caveat by the caller).
    """
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise TranscriptError(f"cannot open transcript {path!r}: {exc}") from exc
    bad = 0
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
    if bad:
        # Surface skipped lines on stderr; never silently truncate.
        print(f"warning: skipped {bad} malformed line(s) in {path}", file=sys.stderr)


def parse_transcript(path: str, by_operation: bool = True):
    """Parse a transcript into (operations, session_totals, session_tier).

    Dedups usage by requestId. Returns a list of Operation objects (empty if
    grouping is disabled), the whole-session ClassTotals, and the dominant
    model's tier multiplier for the session-level cost estimate.
    """
    operations: list[Operation] = []
    session = ClassTotals()
    session_models: dict[str, int] = {}
    seen_request_ids: set[str] = set()
    current: Operation | None = None
    op_index = 0

    for record in iter_records(path):
        rtype = record.get("type")

        if _is_new_operation(record):
            op_index += 1
            current = Operation(label=_prompt_label(record, op_index))
            operations.append(current)
            continue

        if rtype != "assistant":
            continue

        message = record.get("message", {})
        model = message.get("model")
        if model == SYNTHETIC_MODEL:
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue

        # Dedup: streamed fragments of one response repeat identical usage.
        req_id = record.get("requestId")
        if req_id is not None:
            if req_id in seen_request_ids:
                continue
            seen_request_ids.add(req_id)

        session.add_usage(usage)
        if model:
            session_models[model] = session_models.get(model, 0) + 1

        if by_operation:
            if current is None:
                current = Operation(label="op 0 (pre-prompt / continuation)")
                operations.append(current)
            current.totals.add_usage(usage)
            if model:
                current.models.add(model)

    if session.is_empty():
        raise TranscriptError(
            f"no usable usage records found in {path!r} "
            "(empty transcript or no assistant turns)"
        )

    dominant_model = (
        max(session_models, key=session_models.get) if session_models else None
    )
    return operations, session, _tier_for_model(dominant_model)


def _fmt(n: int) -> str:
    return f"{n:,}"


def render_table(operations, session, session_tier, path) -> str:
    """Render the per-operation token-class report table (markdown)."""
    lines = []
    lines.append(f"### Token usage — `{path}`")
    lines.append("")
    lines.append(
        "| Operation | input | output | cache_read | cache_creation | est. cost (rel) |"
    )
    lines.append("|---|---|---|---|---|---|")

    nonempty = [op for op in operations if not op.totals.is_empty()]
    for op in nonempty:
        tier = _tier_for_model(max(op.models, key=lambda m: 1) if op.models else None)
        cost = estimate_cost(op.totals, tier)
        lines.append(
            f"| {op.label} | {_fmt(op.totals.input)} | {_fmt(op.totals.output)} | "
            f"{_fmt(op.totals.cache_read)} | {_fmt(op.totals.cache_creation)} | "
            f"{cost:,.0f} |"
        )

    session_cost = estimate_cost(session, session_tier)
    lines.append(
        f"| **SESSION TOTAL** | **{_fmt(session.input)}** | **{_fmt(session.output)}** | "
        f"**{_fmt(session.cache_read)}** | **{_fmt(session.cache_creation)}** | "
        f"**{session_cost:,.0f}** |"
    )
    lines.append("")
    lines.append(
        "_est. cost is a RELATIVE roll-up: each class is weighted by its "
        "per-token rate and scaled by the model-tier multiplier "
        "(Haiku=1, Sonnet=3, Opus=15). The four raw class counts are the "
        "load-bearing data; the cost column is a convenience estimate, not dollars._"
    )
    return "\n".join(lines)


def _self_test() -> int:
    """Exercise transcript parsing + dedup and the locate-transcript path."""
    import tempfile

    failures = []

    # 1) Project-dir encoding is deterministic (slash/space/dot -> dash runs).
    enc = encode_project_dir("/Users/me/My Proj/.claude/worktrees/x")
    if enc != "-Users-me-My-Proj--claude-worktrees-x":
        failures.append("encode_project_dir: %r" % enc)

    # 2) parse_transcript sums classes and dedups by requestId.
    rows = [
        {"type": "user", "message": {"content": "p1"}},
        {"type": "assistant", "requestId": "r1",
         "message": {"model": "claude-sonnet", "usage": {
             "input_tokens": 10, "output_tokens": 20,
             "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1}}},
        {"type": "assistant", "requestId": "r1",  # streamed dup, must dedup
         "message": {"model": "claude-sonnet", "usage": {
             "input_tokens": 10, "output_tokens": 20,
             "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1}}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tpath = os.path.join(tmp, "s.jsonl")
        with open(tpath, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
        _ops, session, _tier = parse_transcript(tpath, by_operation=False)
        if (session.input, session.output) != (10, 20):
            failures.append("dedup failed: %r" % session)

        # 3) locate-transcript: encode a fake cwd, plant a transcript, find it.
        root = os.path.join(tmp, "projects")
        cwd = "/Users/me/Some Project/.claude/wt"
        proj_dir = os.path.join(root, encode_project_dir(cwd))
        os.makedirs(proj_dir)
        older = os.path.join(proj_dir, "old.jsonl")
        newer = os.path.join(proj_dir, "new.jsonl")
        with open(older, "w") as fh:
            fh.write("{}\n")
        with open(newer, "w") as fh:
            fh.write("{}\n")
        # Make `newer` unambiguously newer by mtime.
        os.utime(older, (1, 1))
        os.utime(newer, (10, 10))
        found = locate_transcript(cwd=cwd, root=root)
        if os.path.basename(found) != "new.jsonl":
            failures.append("locate picked %r (expected new.jsonl)" % found)

        # 4) missing project dir raises TranscriptError gracefully.
        try:
            locate_transcript(cwd="/nope/missing", root=root)
            failures.append("missing project dir should raise")
        except TranscriptError:
            pass

        # 5) present dir but no *.jsonl raises gracefully.
        empty_cwd = "/Users/me/Empty"
        os.makedirs(os.path.join(root, encode_project_dir(empty_cwd)))
        try:
            locate_transcript(cwd=empty_cwd, root=root)
            failures.append("empty project dir should raise")
        except TranscriptError:
            pass

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("parse_usage self-test: OK (project-dir encoding, requestId dedup, "
          "locate-transcript newest-by-mtime, missing-dir + empty-dir raise)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Sum Claude Code transcript token usage by class per operation."
    )
    parser.add_argument(
        "transcript", nargs="?",
        help="path to a session .jsonl transcript",
    )
    parser.add_argument(
        "--session-only",
        action="store_true",
        help="report a single session total, not a per-operation breakdown",
    )
    parser.add_argument(
        "--locate-transcript",
        action="store_true",
        help="resolve and print the newest session transcript path for the "
             "current (or --cwd) project, then exit",
    )
    parser.add_argument(
        "--cwd", default=None,
        help="project root to locate a transcript for (default: real cwd)",
    )
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in test suite and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.locate_transcript:
        try:
            print(locate_transcript(cwd=args.cwd))
        except TranscriptError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if not args.transcript:
        parser.error("a transcript path is required "
                     "(or use --locate-transcript / --self-test)")

    try:
        operations, session, session_tier = parse_transcript(
            args.transcript, by_operation=not args.session_only
        )
    except TranscriptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(render_table(operations, session, session_tier, args.transcript))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
