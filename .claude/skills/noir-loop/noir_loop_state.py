#!/usr/bin/env python3
"""noir_loop_state.py — cross-iteration state for the Noir iterative release loop (#83, v3.13).

Under Noir, each `/loop` firing spawns ONE `release-master` subagent that owns a
full release iteration in isolated context and returns only a 1-2 sentence
summary. Continuity that must survive between iterations lives here — in a small,
size-budgeted file — not in the orchestrator's conversation history. Each spawned
subagent reads this file into its own context, so it MUST stay small; an
over-budget write is REFUSED (never silently truncated) so subagents reading it
stay near-clean.

Design: docs/design/noir-iterative-loop-design.md.
Standard: Python 3 stdlib-only (docs/design/scripting-unification-design.md).

State file (`.claude/cache/noir-loop-state.json`, gitignored):
  schema_version, iteration, updated_at (ISO-8601 UTC), last_summary,
  open_work[], next_steps[].

Usage:
  noir_loop_state.py --init [--force] [--root DIR]
  noir_loop_state.py --read [--root DIR]
  noir_loop_state.py --advance --summary S [--open A --open B] [--next X] [--root DIR]
  noir_loop_state.py --validate [--root DIR]
  noir_loop_state.py --self-test
Exit 0 on success; 2 on bad input / validation / budget violation.
"""
import argparse
import datetime
import json
import os
import sys

# ── Constants (no magic numbers inline) ────────────────────────────────────
SCHEMA_VERSION = 1
STATE_REL = os.path.join(".claude", "cache", "noir-loop-state.json")
# Max serialized size. Each spawned subagent reads the file into its own
# context, so this is the mechanical guarantee behind "subagents stay clean".
MAX_STATE_BYTES = 4096
INITIAL_ITERATION = 0
JSON_INDENT = 2


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
    def advance(self, summary, open_work=None, next_steps=None):
        """Bump iteration, set the summary, replace work lists, restamp time."""
        if not isinstance(summary, str) or not summary.strip():
            raise StateError("--advance requires a non-empty --summary")
        self._data["iteration"] = int(self._data["iteration"]) + 1
        self._data["last_summary"] = summary.strip()
        self._data["open_work"] = self._as_str_list(open_work, "open_work")
        self._data["next_steps"] = self._as_str_list(next_steps, "next_steps")
        self._data["updated_at"] = self._now()
        self.validate()
        return self

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


# ── Command handlers ────────────────────────────────────────────────────────
def cmd_init(root, force):
    path = _state_path(root)
    if os.path.exists(path) and not force:
        # Idempotent: refuse to clobber existing state unless --force.
        return NoirLoopState.load(path)
    state = NoirLoopState.fresh()
    state.save(path)
    return state


def cmd_read(root):
    path = _state_path(root)
    if not os.path.exists(path):
        return NoirLoopState.fresh()  # near-empty when uninitialized
    return NoirLoopState.load(path)


def cmd_advance(root, summary, open_work=None, next_steps=None):
    path = _state_path(root)
    state = cmd_read(root)
    state.advance(summary, open_work, next_steps)
    state.save(path)
    return state


def cmd_validate(root):
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

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("noir_loop_state self-test: OK (fresh defaults, init idempotency, "
          "advance bump+replace+restamp, read round-trip, empty-summary raise, "
          "schema-version raise, over-budget refusal+intact-file, type validation, "
          "determinism, missing-file raise)")
    return 0


def main(argv=None):
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
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    try:
        if args.init:
            state = cmd_init(args.root, args.force)
        elif args.read:
            state = cmd_read(args.root)
        elif args.advance:
            if not args.summary:
                ap.error("--advance requires --summary")
            state = cmd_advance(args.root, args.summary, args.open_work, args.next_steps)
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
