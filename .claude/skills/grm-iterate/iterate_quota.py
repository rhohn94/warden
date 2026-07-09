#!/usr/bin/env python3
"""iterate_quota.py — quota + iteration state for the `iterate on {facet}` loop (#80, v3.11).

The `grm-iterate` skill drives systematic improvement on a project facet (ux / code
quality / performance / …): audit -> file issues to a size quota -> release ->
repeat. This helper owns the DETERMINISTIC parts (per #75 — a script, not agent
arithmetic): the T-shirt-size quota math and the resumable iteration state. The
agent does the judgement (audit, sizing, filing, release); it calls this script
to know "what sizes still need filling?" and to persist its place across a Noir
compaction / wakeup.

The quota is a **floor**: each size bucket must be filled before an iteration's
filing phase completes. Quota comes from `grimoire-config.json` `iterate.quota`
(per-facet override honored), else the built-in default.

State file (`.claude/iterate-state.json`, gitignored): the active facet, run id,
iterations remaining, current iteration number, the quota, the per-size filled
counts, and the min-issues floor (the until-clean stop signal).

Usage:
  iterate_quota.py --init --facet F --run-id ID [--iterations N] [--root DIR]
  iterate_quota.py --record --size M [--count N] [--root DIR]
  iterate_quota.py --status [--root DIR]          # JSON: remaining per size + quota_met
  iterate_quota.py --next-iteration [--root DIR]  # advance; resets filled buckets
  iterate_quota.py --self-test
Exit 0 on success; 2 on bad input / missing state.
"""
import argparse
import json
import os
import sys

SIZES = ["XXL", "XL", "L", "M", "SM", "XS"]  # largest -> smallest
DEFAULT_QUOTA = {"XXL": 1, "XL": 3, "L": 5, "M": 10, "SM": 10, "XS": 20}
DEFAULT_ITERATIONS = 1
DEFAULT_MIN_FLOOR = 3
STATE_REL = os.path.join(".claude", "iterate-state.json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def resolve_quota(root, facet):
    """Quota from config iterate.quota (+ per-facet override), else default."""
    cfg = _read_json(os.path.join(root, ".claude", "grimoire-config.json")) or {}
    itc = cfg.get("iterate") or {}
    quota = dict(DEFAULT_QUOTA)
    base = itc.get("quota")
    if isinstance(base, dict):
        for k, v in base.items():
            if k in SIZES and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                quota[k] = v
    per_facet = (itc.get("per-facet") or {}).get(facet)
    if isinstance(per_facet, dict) and isinstance(per_facet.get("quota"), dict):
        for k, v in per_facet["quota"].items():
            if k in SIZES and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                quota[k] = v
    return quota


def resolve_scalar(root, key, default):
    cfg = _read_json(os.path.join(root, ".claude", "grimoire-config.json")) or {}
    itc = cfg.get("iterate") or {}
    v = itc.get(key)
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    return default


def init_state(root, facet, run_id, iterations):
    quota = resolve_quota(root, facet)
    floor = resolve_scalar(root, "min-issues-floor", DEFAULT_MIN_FLOOR)
    state = {
        "facet": facet,
        "run_id": run_id,
        "iterations_remaining": max(1, int(iterations)),
        "iteration": 1,
        "quota": quota,
        "filled": {s: 0 for s in SIZES},
        "min_issues_floor": floor,
    }
    _write_json(os.path.join(root, STATE_REL), state)
    return state


def load_state(root):
    state = _read_json(os.path.join(root, STATE_REL))
    if not state or "quota" not in state:
        raise SystemExit2("no iterate state at %s — run --init first" % STATE_REL)
    return state


class SystemExit2(Exception):
    pass


def record(root, size, count):
    if size not in SIZES:
        raise SystemExit2("unknown size %r — valid: %s" % (size, ", ".join(SIZES)))
    state = load_state(root)
    state.setdefault("filled", {s: 0 for s in SIZES})
    state["filled"][size] = state["filled"].get(size, 0) + int(count)
    _write_json(os.path.join(root, STATE_REL), state)
    return state


def status(state):
    quota = state.get("quota", {})
    filled = state.get("filled", {})
    remaining = {s: max(0, quota.get(s, 0) - filled.get(s, 0)) for s in SIZES}
    total_remaining = sum(remaining.values())
    return {
        "facet": state.get("facet"),
        "run_id": state.get("run_id"),
        "iteration": state.get("iteration"),
        "iterations_remaining": state.get("iterations_remaining"),
        "quota": quota,
        "filled": filled,
        "remaining": remaining,
        "total_remaining": total_remaining,
        "quota_met": total_remaining == 0,
        "min_issues_floor": state.get("min_issues_floor", DEFAULT_MIN_FLOOR),
    }


def next_iteration(root):
    state = load_state(root)
    state["iterations_remaining"] = max(0, state.get("iterations_remaining", 1) - 1)
    state["iteration"] = state.get("iteration", 1) + 1
    state["filled"] = {s: 0 for s in SIZES}  # fresh quota for the new iteration
    _write_json(os.path.join(root, STATE_REL), state)
    return state


def _self_test():
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".claude"))
        # config with a custom quota + per-facet override.
        with open(os.path.join(d, ".claude", "grimoire-config.json"), "w") as fh:
            json.dump({"schema-version": 4, "name": "T",
                       "iterate": {"quota": {"XS": 5}, "min-issues-floor": 2,
                                   "per-facet": {"ux": {"quota": {"M": 4}}}}}, fh)

        st = init_state(d, "ux", "run-1", 2)
        if st["quota"]["XS"] != 5:
            failures.append("config quota override (XS=5) not applied: %r" % st["quota"])
        if st["quota"]["M"] != 4:
            failures.append("per-facet override (M=4) not applied: %r" % st["quota"])
        if st["min_issues_floor"] != 2:
            failures.append("min-issues-floor from config not applied")
        if st["iterations_remaining"] != 2:
            failures.append("iterations not seeded")

        # record filings; quota is a floor.
        record(d, "M", 4)
        record(d, "XS", 3)
        s = status(load_state(d))
        if s["remaining"]["XS"] != 2:
            failures.append("XS remaining should be 2: %r" % s["remaining"])
        if s["remaining"]["M"] != 0:
            failures.append("M should be filled: %r" % s["remaining"])
        if s["quota_met"]:
            failures.append("quota should NOT be met yet (XXL/XL/L/SM/XS open)")

        # fill everything; quota_met flips true.
        for size, n in (("XXL", 1), ("XL", 3), ("L", 5), ("SM", 10), ("XS", 2)):
            record(d, size, n)
        s2 = status(load_state(d))
        if not s2["quota_met"]:
            failures.append("quota should be met now: %r" % s2["remaining"])

        # next iteration resets filled, decrements remaining.
        ni = next_iteration(d)
        if ni["iterations_remaining"] != 1 or ni["iteration"] != 2:
            failures.append("next-iteration counters wrong: %r" % ni)
        if any(v != 0 for v in ni["filled"].values()):
            failures.append("next-iteration did not reset filled buckets")
        if status(load_state(d))["quota_met"]:
            failures.append("quota should be open again after reset")

        # unknown size raises.
        try:
            record(d, "HUGE", 1)
            failures.append("unknown size should raise")
        except SystemExit2:
            pass

        # determinism of status.
        if json.dumps(status(load_state(d)), sort_keys=True) != \
           json.dumps(status(load_state(d)), sort_keys=True):
            failures.append("status non-deterministic")

    # default quota when no config.
    with tempfile.TemporaryDirectory() as d2:
        os.makedirs(os.path.join(d2, ".claude"))
        st = init_state(d2, "backend", "run-x", 1)
        if st["quota"] != DEFAULT_QUOTA:
            failures.append("default quota not used when config absent: %r" % st["quota"])

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("iterate_quota self-test: OK (config+per-facet quota, floor semantics, "
          "record, quota_met flip, next-iteration reset, unknown-size raise, "
          "default quota, determinism)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Quota + iteration state for `iterate on {facet}`.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--next-iteration", action="store_true")
    ap.add_argument("--facet"); ap.add_argument("--run-id")
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    ap.add_argument("--size"); ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    try:
        if args.init:
            if not args.facet or not args.run_id:
                ap.error("--init requires --facet and --run-id")
            print(json.dumps(init_state(args.root, args.facet, args.run_id, args.iterations),
                             indent=2, sort_keys=True))
        elif args.record:
            if not args.size:
                ap.error("--record requires --size")
            print(json.dumps(status(record(args.root, args.size, args.count)),
                             indent=2, sort_keys=True))
        elif args.status:
            print(json.dumps(status(load_state(args.root)), indent=2, sort_keys=True))
        elif getattr(args, "next_iteration"):
            print(json.dumps(status(next_iteration(args.root)), indent=2, sort_keys=True))
        else:
            ap.error("one of --init / --record / --status / --next-iteration / --self-test")
    except SystemExit2 as e:
        print("iterate_quota: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
