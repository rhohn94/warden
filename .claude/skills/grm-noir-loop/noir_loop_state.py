#!/usr/bin/env python3
"""noir_loop_state.py — cross-iteration state for the Noir iterative release loop (#83, v3.13).

Under Noir, each `/loop` firing spawns ONE `release-master` subagent that owns a
full release iteration in isolated context and returns only a 1-2 sentence
summary. Continuity that must survive between iterations lives here — in a small,
size-budgeted file — not in the orchestrator's conversation history. Each spawned
subagent reads this file into its own context, so it MUST stay small; an
over-budget write is REFUSED (never silently truncated) so subagents reading it
stay near-clean.

**Blocked-on-human escape + cycle budget (#422, v3.93).** Every `--advance`
recomputes a `progress_hash` over the open-work set + the current blocker
string. If that hash repeats unchanged for `STALL_LIMIT` consecutive advances,
`blocked_on_human` flips true — the loop made the same no-progress observation
several times in a row (the same human-gated item, the same blocker) and should
stop with a clear report instead of spinning forever (the #422 "8 Stop-hook
cycles" incident). Independently, `cycle_budget_exceeded` flips true once
`iteration` reaches the configurable `max_cycles` cap — a backstop that fires
even while the loop is still making real progress, so no run is unbounded.
Callers (the release-master / orchestrator) read both flags off `--read` /
`--advance` output and hand off to `grm-stop-point` when either is set.

Design: docs/grimoire/design/noir-iterative-loop-design.md.
Standard: Python 3 stdlib-only (docs/grimoire/design/scripting-unification-design.md).

State file (`.claude/cache/noir-loop-state.json`, gitignored):
  schema_version, iteration, updated_at (ISO-8601 UTC), last_summary,
  open_work[], next_steps[], progress_hash, stall_count, blocked_on_human,
  blocker, max_cycles, cycle_budget_exceeded.

Usage:
  noir_loop_state.py --init [--force] [--max-cycles N] [--root DIR]
  noir_loop_state.py --read [--root DIR]
  noir_loop_state.py --advance --summary S [--open A --open B] [--next X]
                      [--blocker TEXT] [--max-cycles N] [--root DIR]
  noir_loop_state.py --validate [--root DIR]
  noir_loop_state.py --self-test
Exit 0 on success; 2 on bad input / validation / budget violation.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

# Import parse_releases from the sibling project_status module (#342).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-agent-status-broker"))
from project_status import parse_releases  # noqa: E402  (path set above)

# ── Constants (no magic numbers inline) ────────────────────────────────────
SCHEMA_VERSION = 1
STATE_REL = os.path.join(".claude", "cache", "noir-loop-state.json")
# Max serialized size. Each spawned subagent reads the file into its own
# context, so this is the mechanical guarantee behind "subagents stay clean".
MAX_STATE_BYTES = 4096
INITIAL_ITERATION = 0
JSON_INDENT = 2
# Number of consecutive `--advance` calls with an IDENTICAL progress_hash
# before the loop declares itself blocked-on-human and stops (#422). Chosen
# as 3: 1 tolerates zero repetition (too eager — a single coincidental repeat
# while unrelated work lands would false-trigger); 2 is defensible but still
# risks tripping on a one-off transient repeat; 3 requires a genuinely
# sustained stall while still catching it in a handful of cycles — well
# below the 8-cycle bounce that motivated this issue.
STALL_LIMIT = 3
# Backstop cap on total loop iterations regardless of progress — a runaway
# loop still stops even if it keeps finding *different* busywork each cycle.
# This is only the default seed; callers may override per-state via
# `--max-cycles`, which persists in the state file once set.
DEFAULT_MAX_CYCLES = 20
# Truncated sha256 hex length for progress_hash — short enough to keep the
# state file's byte budget cheap, long enough that an accidental collision
# between two genuinely different states is not a practical concern here.
PROGRESS_HASH_LEN = 16


class StateError(Exception):
    """Raised on bad input, schema violation, or budget overflow (→ exit 2)."""


class NoirLoopState:
    """Owns one Noir-loop state document: load, validate, mutate, atomic save.

    A single class encapsulates the whole lifecycle so callers never hand-roll
    the schema or the budget check. Construct via `load()` / `fresh()`, mutate
    via `advance()`, persist via `save()`. All writes validate the schema and
    the size budget first, so an invalid or over-budget document never reaches
    disk.
    """

    REQUIRED_FIELDS = ("schema_version", "iteration", "updated_at",
                       "last_summary", "open_work", "next_steps")

    def __init__(self, data):
        self._data = data

    # ── Construction ──
    @classmethod
    def fresh(cls):
        """Return a brand-new state at the initial iteration."""
        return cls({
            "schema_version": SCHEMA_VERSION,
            "iteration": INITIAL_ITERATION,
            "updated_at": cls._now(),
            "last_summary": "",
            "open_work": [],
            "next_steps": [],
            "progress_hash": "",
            "stall_count": 0,
            "blocked_on_human": False,
            "blocker": "",
            "max_cycles": DEFAULT_MAX_CYCLES,
            "cycle_budget_exceeded": False,
        })

    @classmethod
    def load(cls, path):
        """Load + validate an existing state file. Raises StateError if invalid."""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise StateError("no state file at %s — run --init first" % path)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError("cannot read state at %s: %s" % (path, exc))
        inst = cls(data)
        inst.validate()
        return inst

    # ── Helpers ──
    @staticmethod
    def _now():
        """ISO-8601 UTC timestamp (seconds precision, trailing Z)."""
        return datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _as_str_list(value, field):
        """Coerce/validate a value into a list of non-empty strings."""
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise StateError("%s must be a list of strings" % field)
        return [x for x in value if x.strip()]

    # ── Validation ──
    def validate(self):
        """Validate schema shape + size budget. Raises StateError on violation."""
        d = self._data
        if not isinstance(d, dict):
            raise StateError("state must be a JSON object")
        for f in self.REQUIRED_FIELDS:
            if f not in d:
                raise StateError("missing required field: %s" % f)
        if d["schema_version"] != SCHEMA_VERSION:
            raise StateError("unsupported schema_version %r (this helper handles %d)"
                             % (d["schema_version"], SCHEMA_VERSION))
        if not isinstance(d["iteration"], int) or isinstance(d["iteration"], bool) \
                or d["iteration"] < 0:
            raise StateError("iteration must be a non-negative integer")
        if not isinstance(d["updated_at"], str) or not d["updated_at"]:
            raise StateError("updated_at must be a non-empty string")
        if not isinstance(d["last_summary"], str):
            raise StateError("last_summary must be a string")
        self._as_str_list(d.get("open_work"), "open_work")
        self._as_str_list(d.get("next_steps"), "next_steps")

        # Progress-tracking fields (#422) are additive/optional so an older
        # state file (pre-dating this change) still loads — self-heal missing
        # ones to their defaults rather than hard-failing a stale cache.
        d.setdefault("progress_hash", "")
        d.setdefault("stall_count", 0)
        d.setdefault("blocked_on_human", False)
        d.setdefault("blocker", "")
        d.setdefault("max_cycles", DEFAULT_MAX_CYCLES)
        d.setdefault("cycle_budget_exceeded", False)
        if not isinstance(d["progress_hash"], str):
            raise StateError("progress_hash must be a string")
        if not isinstance(d["stall_count"], int) or isinstance(d["stall_count"], bool) \
                or d["stall_count"] < 0:
            raise StateError("stall_count must be a non-negative integer")
        if not isinstance(d["blocked_on_human"], bool):
            raise StateError("blocked_on_human must be a boolean")
        if not isinstance(d["blocker"], str):
            raise StateError("blocker must be a string")
        if not isinstance(d["max_cycles"], int) or isinstance(d["max_cycles"], bool) \
                or d["max_cycles"] < 1:
            raise StateError("max_cycles must be a positive integer")
        if not isinstance(d["cycle_budget_exceeded"], bool):
            raise StateError("cycle_budget_exceeded must be a boolean")

        self._check_budget()
        return True

    def _check_budget(self):
        size = len(self.serialize().encode("utf-8"))
        if size > MAX_STATE_BYTES:
            raise StateError(
                "state is %d bytes > %d budget — trim open_work / next_steps "
                "(keep it concise; the file is NOT auto-truncated)"
                % (size, MAX_STATE_BYTES))

    # ── Mutation ──
    def advance(self, summary, open_work=None, next_steps=None, blocker=None,
                max_cycles=None):
        """Bump iteration, set the summary, replace work lists, restamp time,
        and recompute the progress-hash / stall / cycle-budget stop flags.

        `blocker` (optional) is the current human-blocking condition — pass a
        non-empty string when the iteration is stuck on the same human-gated
        item, or "" to explicitly clear it. Omitted (None) keeps the prior
        value, since a release-master that doesn't mention a blocker isn't
        necessarily saying "cleared" vs. "unchanged."
        `max_cycles` (optional) overrides the cycle-budget cap; omitted keeps
        the prior value (or the default seed on a fresh state).
        """
        if not isinstance(summary, str) or not summary.strip():
            raise StateError("--advance requires a non-empty --summary")
        self._data["iteration"] = int(self._data["iteration"]) + 1
        self._data["last_summary"] = summary.strip()
        self._data["open_work"] = self._as_str_list(open_work, "open_work")
        self._data["next_steps"] = self._as_str_list(next_steps, "next_steps")
        if blocker is not None:
            if not isinstance(blocker, str):
                raise StateError("blocker must be a string")
            self._data["blocker"] = blocker.strip()
        else:
            self._data.setdefault("blocker", "")
        if max_cycles is not None:
            self._set_max_cycles(max_cycles)
        else:
            self._data.setdefault("max_cycles", DEFAULT_MAX_CYCLES)
        self._update_progress()
        self._data["updated_at"] = self._now()
        self.validate()
        return self

    def _set_max_cycles(self, max_cycles):
        """Validate + set the cycle-budget cap (a positive integer)."""
        if not isinstance(max_cycles, int) or isinstance(max_cycles, bool) or max_cycles < 1:
            raise StateError("--max-cycles must be a positive integer")
        self._data["max_cycles"] = max_cycles

    def _update_progress(self):
        """Recompute progress_hash + stall_count and the two derived stop
        flags (blocked_on_human, cycle_budget_exceeded) — the #422 mechanism.

        A hash that matches the immediately-prior hash extends the current
        "no progress" streak; any change (new/removed open-work item, a
        different blocker) resets the streak to 1 (this iteration is the
        first observation of a new state). `blocked_on_human` flips true once
        the streak reaches STALL_LIMIT consecutive identical observations.
        `cycle_budget_exceeded` is independent — it fires once `iteration`
        reaches `max_cycles`, whether or not progress is being made.
        """
        old_hash = self._data.get("progress_hash", "")
        new_hash = self._compute_progress_hash(self._data["open_work"], self._data["blocker"])
        if old_hash and new_hash == old_hash:
            stall = int(self._data.get("stall_count", 0)) + 1
        else:
            stall = 1
        self._data["progress_hash"] = new_hash
        self._data["stall_count"] = stall
        self._data["blocked_on_human"] = stall >= STALL_LIMIT
        self._data["cycle_budget_exceeded"] = (
            self._data["iteration"] >= self._data.get("max_cycles", DEFAULT_MAX_CYCLES))

    @staticmethod
    def _compute_progress_hash(open_work, blocker):
        """Deterministic short hash over the "relevant state" for this
        iteration: the open-work set (order-independent — sorted) and the
        current blocker string. Two iterations that see the same open items
        and the same blocker hash identically, which is exactly the "no
        real-world change" signal #422 wants detected mechanically."""
        canonical = "|".join(sorted(open_work)) + "::" + (blocker or "").strip()
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:PROGRESS_HASH_LEN]

    # ── Serialization / persistence ──
    def serialize(self):
        """Deterministic JSON text (sorted keys) — stable for diffs + budget."""
        return json.dumps(self._data, indent=JSON_INDENT, sort_keys=True)

    def as_dict(self):
        return dict(self._data)

    def save(self, path):
        """Validate then atomically write (temp + os.replace)."""
        self.validate()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(self.serialize() + "\n")
        os.replace(tmp, path)
        return path


def _state_path(root):
    return os.path.join(root, STATE_REL)


def _warn_on_continuity_loss(root):
    """Warn on stderr if a fresh state coexists with releases already shipped.

    Called only from `cmd_read`'s fresh-state branch, where the state is
    always at INITIAL_ITERATION by construction — no state param needed.
    Cross-checks docs/version-history.md via the canonical `parse_releases()`
    (#342) for shipped releases; if any exist, emits a non-fatal warning
    about likely cross-worktree continuity loss. This is a known limitation
    of gitignored cache files across isolated worktrees.
    """
    version_history_path = os.path.join(root, "docs", "version-history.md")
    if not os.path.exists(version_history_path):
        return  # no version history file to check

    try:
        with open(version_history_path, encoding="utf-8") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError):
        return  # cannot read; skip warning (not fatal)

    shipped_releases = parse_releases(content)

    if shipped_releases:
        print(
            "WARNING: loop state reset to iteration 0 but %d releases already shipped "
            "— continuity likely lost (gitignored cache files do not cross git worktrees; "
            "this is a known cross-worktree isolation limitation)" % len(shipped_releases),
            file=sys.stderr
        )


# ── Command handlers ────────────────────────────────────────────────────────
def cmd_init(root: str, force: bool, max_cycles: int | None = None) -> NoirLoopState:
    path = _state_path(root)
    if os.path.exists(path) and not force:
        # Idempotent: refuse to clobber existing state unless --force.
        return NoirLoopState.load(path)
    state = NoirLoopState.fresh()
    if max_cycles is not None:
        state._set_max_cycles(max_cycles)  # noqa: SLF001 — same-module helper
        state._update_progress()
    state.save(path)
    return state


def cmd_read(root: str) -> NoirLoopState:
    path = _state_path(root)
    if not os.path.exists(path):
        state = NoirLoopState.fresh()  # near-empty when uninitialized
        # Self-detect continuity loss: if state is fresh but releases exist,
        # warn about potential cross-worktree isolation issue.
        _warn_on_continuity_loss(root)
        return state
    return NoirLoopState.load(path)


def cmd_advance(root: str, summary: str, open_work: list | None = None,
                 next_steps: list | None = None, blocker: str | None = None,
                 max_cycles: int | None = None) -> NoirLoopState:
    path = _state_path(root)
    state = cmd_read(root)
    state.advance(summary, open_work, next_steps, blocker, max_cycles)
    state.save(path)
    return state


def cmd_validate(root: str) -> NoirLoopState:
    return NoirLoopState.load(_state_path(root))


# ── Self-test ───────────────────────────────────────────────────────────────
def _self_test():
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as d:
        # fresh() defaults.
        fresh = NoirLoopState.fresh()
        if fresh.as_dict()["iteration"] != INITIAL_ITERATION:
            failures.append("fresh iteration should be %d" % INITIAL_ITERATION)
        fresh.validate()

        # init creates a file; re-init is idempotent (no clobber).
        s1 = cmd_init(d, force=False)
        s1.advance("first iteration done", open_work=["x"], next_steps=["y"])
        s1.save(_state_path(d))
        s2 = cmd_init(d, force=False)  # must NOT reset
        if s2.as_dict()["iteration"] != 1:
            failures.append("re-init clobbered existing state: %r" % s2.as_dict())

        # advance bumps iteration + replaces lists + restamps.
        before = cmd_read(d).as_dict()["updated_at"]
        adv = cmd_advance(d, "second", open_work=["a", "b"], next_steps=["c"])
        ad = adv.as_dict()
        if ad["iteration"] != 2:
            failures.append("advance did not bump iteration: %r" % ad)
        if ad["last_summary"] != "second":
            failures.append("advance did not set summary")
        if ad["open_work"] != ["a", "b"] or ad["next_steps"] != ["c"]:
            failures.append("advance did not replace work lists: %r" % ad)
        if not ad["updated_at"] or before is None:
            failures.append("advance did not restamp updated_at")

        # read round-trips and validates.
        rd = cmd_read(d).as_dict()
        if rd["iteration"] != 2:
            failures.append("read did not round-trip: %r" % rd)

        # empty summary is rejected.
        try:
            cmd_advance(d, "   ")
            failures.append("empty summary should raise")
        except StateError:
            pass

        # unknown schema_version is rejected.
        bad = NoirLoopState.fresh()
        bad._data["schema_version"] = 999
        try:
            bad.validate()
            failures.append("unknown schema_version should raise")
        except StateError:
            pass

        # over-budget write is REFUSED and leaves the prior file intact.
        prior = cmd_read(d).as_dict()
        huge = ["padding-" + "z" * 200 for _ in range(MAX_STATE_BYTES // 100)]
        try:
            cmd_advance(d, "overflow", open_work=huge)
            failures.append("over-budget write should raise")
        except StateError:
            pass
        after = cmd_read(d).as_dict()
        if after != prior:
            failures.append("over-budget write mutated the file: %r" % after)

        # non-string list is rejected.
        nb = NoirLoopState.fresh()
        nb._data["open_work"] = [1, 2]
        try:
            nb.validate()
            failures.append("non-string open_work should raise")
        except StateError:
            pass

        # serialize is deterministic.
        a = cmd_read(d).serialize()
        b = cmd_read(d).serialize()
        if a != b:
            failures.append("serialize non-deterministic")

        # validate on a good file passes.
        cmd_validate(d).validate()

        # load on a missing file raises.
        with tempfile.TemporaryDirectory() as empty:
            try:
                NoirLoopState.load(_state_path(empty))
                failures.append("load on missing file should raise")
            except StateError:
                pass

        # Fresh state with shipped releases triggers a warning (non-fatal).
        # Capture stderr to verify the warning was emitted.
        import io
        with tempfile.TemporaryDirectory() as d:
            # Create a version-history.md with shipped releases.
            os.makedirs(os.path.join(d, "docs"), exist_ok=True)
            version_history = os.path.join(d, "docs", "version-history.md")
            with open(version_history, "w", encoding="utf-8") as fh:
                fh.write("# Version History\n\n"
                         "## v3.75 — First release\n\nSome content.\n\n"
                         "## v3.76 — Second release\n\nMore content.\n")
            # Capture stderr and verify warning is emitted.
            old_stderr = sys.stderr
            try:
                sys.stderr = io.StringIO()
                state_fresh = cmd_read(d)
                warning_text = sys.stderr.getvalue()
                if state_fresh.as_dict()["iteration"] != INITIAL_ITERATION:
                    failures.append("fresh state should have iteration 0")
                if "loop state reset to iteration 0" not in warning_text:
                    failures.append("fresh state with releases should warn: got %r" % warning_text)
                if "2 releases already shipped" not in warning_text:
                    failures.append("warning should mention count of releases: got %r" % warning_text)
            finally:
                sys.stderr = old_stderr

        # ── Progress-hash / stall / blocked-on-human (#422) ──────────────
        with tempfile.TemporaryDirectory() as d:
            cmd_init(d, force=False)
            # Same open_work + same blocker, advanced repeatedly: stall_count
            # climbs 1, 2, 3 and blocked_on_human flips true at STALL_LIMIT.
            s1 = cmd_advance(d, "iter 1", open_work=["issue #99 blocked"],
                              blocker="waiting on human decision")
            if s1.as_dict()["stall_count"] != 1:
                failures.append("first advance should start a stall run of 1: %r"
                                 % s1.as_dict())
            if s1.as_dict()["blocked_on_human"]:
                failures.append("blocked_on_human should not fire after 1 advance")
            s2 = cmd_advance(d, "iter 2", open_work=["issue #99 blocked"],
                              blocker="waiting on human decision")
            if s2.as_dict()["stall_count"] != 2:
                failures.append("identical 2nd advance should bump stall_count to 2: %r"
                                 % s2.as_dict())
            if s2.as_dict()["blocked_on_human"]:
                failures.append("blocked_on_human should not fire before STALL_LIMIT")
            s3 = cmd_advance(d, "iter 3", open_work=["issue #99 blocked"],
                              blocker="waiting on human decision")
            if s3.as_dict()["stall_count"] != STALL_LIMIT:
                failures.append("3rd identical advance should reach STALL_LIMIT: %r"
                                 % s3.as_dict())
            if not s3.as_dict()["blocked_on_human"]:
                failures.append("blocked_on_human should fire at STALL_LIMIT: %r"
                                 % s3.as_dict())
            # Real progress (different open_work) resets the streak and
            # un-sticks blocked_on_human.
            s4 = cmd_advance(d, "iter 4 — unblocked", open_work=["new item"], blocker="")
            if s4.as_dict()["stall_count"] != 1:
                failures.append("changed state should reset stall_count to 1: %r"
                                 % s4.as_dict())
            if s4.as_dict()["blocked_on_human"]:
                failures.append("blocked_on_human should clear once progress resumes")
            if s4.as_dict()["progress_hash"] == s3.as_dict()["progress_hash"]:
                failures.append("progress_hash should change when open_work/blocker change")

        # ── Cycle-budget cap (#422) ───────────────────────────────────────
        with tempfile.TemporaryDirectory() as d:
            cmd_init(d, force=False, max_cycles=2)
            c1 = cmd_advance(d, "cycle 1", open_work=["a"])
            if c1.as_dict()["max_cycles"] != 2:
                failures.append("--max-cycles at init should persist: %r" % c1.as_dict())
            if c1.as_dict()["cycle_budget_exceeded"]:
                failures.append("cycle_budget_exceeded should not fire before the cap")
            c2 = cmd_advance(d, "cycle 2", open_work=["b"])  # iteration now == max_cycles
            if not c2.as_dict()["cycle_budget_exceeded"]:
                failures.append("cycle_budget_exceeded should fire once iteration "
                                 "reaches max_cycles: %r" % c2.as_dict())
            # Fires even though open_work kept changing (real progress) —
            # the budget is an independent backstop, not tied to staleness.
            if c2.as_dict()["stall_count"] != 1:
                failures.append("cycle-budget case should still show real progress "
                                 "(stall_count 1), proving the two mechanisms are "
                                 "independent: %r" % c2.as_dict())

        # ── --max-cycles input validation ─────────────────────────────────
        with tempfile.TemporaryDirectory() as d:
            cmd_init(d, force=False)
            for bad in (0, -1):
                try:
                    cmd_advance(d, "bad", max_cycles=bad)
                    failures.append("non-positive --max-cycles should raise (%r)" % bad)
                except StateError:
                    pass

        # ── Legacy state file (pre-#422, missing the new fields) loads and
        # self-heals to defaults rather than hard-failing. ──────────────────
        with tempfile.TemporaryDirectory() as d:
            legacy_path = _state_path(d)
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            with open(legacy_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "schema_version": SCHEMA_VERSION,
                    "iteration": 5,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "last_summary": "pre-#422 state",
                    "open_work": ["carryover"],
                    "next_steps": [],
                }, fh)
            legacy = NoirLoopState.load(legacy_path)
            ld = legacy.as_dict()
            if ld.get("blocked_on_human") is not False or ld.get("stall_count") != 0 \
                    or ld.get("max_cycles") != DEFAULT_MAX_CYCLES:
                failures.append("legacy state should self-heal missing #422 fields: %r" % ld)

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("noir_loop_state self-test: OK (fresh defaults, init idempotency, "
          "advance bump+replace+restamp, read round-trip, empty-summary raise, "
          "schema-version raise, over-budget refusal+intact-file, type validation, "
          "determinism, missing-file raise, continuity-loss warning, "
          "progress-hash stall tracking, blocked-on-human at STALL_LIMIT, "
          "progress-reset unsticking, cycle-budget cap + independence from "
          "stall tracking, max-cycles validation, legacy-state self-heal)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cross-iteration state for the Noir iterative release loop (#83).")
    ap.add_argument("--root", default=".")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--force", action="store_true", help="with --init, overwrite existing state")
    ap.add_argument("--read", action="store_true")
    ap.add_argument("--advance", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--summary")
    ap.add_argument("--open", action="append", dest="open_work", default=None,
                    help="an open-work item (repeatable)")
    ap.add_argument("--next", action="append", dest="next_steps", default=None,
                    help="a next-step item (repeatable)")
    ap.add_argument("--blocker", default=None,
                    help="current human-blocking condition (with --advance); "
                         "omit to keep the prior value, pass '' to clear it")
    ap.add_argument("--max-cycles", dest="max_cycles", type=int, default=None,
                    help="override the cycle-budget cap (persists once set; "
                         "default %d)" % DEFAULT_MAX_CYCLES)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    try:
        if args.init:
            state = cmd_init(args.root, args.force, args.max_cycles)
        elif args.read:
            state = cmd_read(args.root)
        elif args.advance:
            if not args.summary:
                ap.error("--advance requires --summary")
            state = cmd_advance(args.root, args.summary, args.open_work, args.next_steps,
                                 args.blocker, args.max_cycles)
        elif args.validate:
            state = cmd_validate(args.root)
        else:
            ap.error("one of --init / --read / --advance / --validate / --self-test")
        print(state.serialize())
    except StateError as exc:
        print("noir_loop_state: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
