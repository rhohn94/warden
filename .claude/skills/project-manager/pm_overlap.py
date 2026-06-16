#!/usr/bin/env python3
"""pm_overlap.py — deterministic feature-overlap analysis → lane plan.

The Project Manager (v3.1) calls this before dispatching parallel integration
masters. Given a feature list (each declaring the components it touches, with
read/write intent) and the component registry, it partitions features into
**lanes** that will not collide on a shared writable component, so each lane can
be implemented by an independent integration master in parallel.

Design authority: docs/design/project-manager-role-design.md §3.

Determinism: same registry + feature list + policy ⇒ byte-identical plan. No
clock, no randomness, fully sorted output.

Inputs
------
--registry  PATH   .claude/component-registry.json (optional; absence triggers
                   the file-path footprint fallback + low-confidence flag).
--features  PATH   JSON: {"features": [
                     {"id": "feat-a",
                      "components": {"auth-jwt": "write", "http-server": "read"},
                      "paths": ["src/api/"],          # optional, used in fallback
                      "provides": ["auth"],            # optional
                      "requires": ["http-server"]}     # optional
                   ]}
--policy    {conservative,balanced,aggressive}  default balanced
--max-parallel INT  cap on concurrent lanes (default 3)

Output: a lane-plan JSON on stdout. Exit 0 on success, 2 on bad input.

Conflict rule (edge between two features sharing a component c):
  conservative — any shared component (even read-only) conflicts.
  balanced     — at least one side writes c (write-write / write-read).
  aggressive   — both sides write c.
Read-only co-dependencies never conflict under balanced/aggressive.
"""
import argparse
import json
import sys

POLICIES = ("conservative", "balanced", "aggressive")
WRITE_INTENTS = ("write", "rw", "readwrite", "write-read")


def _is_write(intent):
    return str(intent).lower() in WRITE_INTENTS


def _conflicts(intent_a, intent_b, policy):
    """Do two intents on the same shared component force the same lane?"""
    wa, wb = _is_write(intent_a), _is_write(intent_b)
    if policy == "conservative":
        return True                      # any shared component
    if policy == "aggressive":
        return wa and wb                 # only write-write
    return wa or wb                      # balanced: at least one writes


class UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic: smaller key becomes root
            hi, lo = (ra, rb) if ra > rb else (rb, ra)
            self.parent[hi] = lo


def _feature_components(feat, registry_present):
    """Return {component_id: intent}. In fallback, synthesize pseudo-components
    from declared paths so the same conflict machinery applies."""
    comps = dict(feat.get("components") or {})
    if not comps and not registry_present:
        # file-path footprint fallback: each path is a pseudo-component the
        # feature WRITES (conservative — we cannot prove read-only intent).
        for p in feat.get("paths") or []:
            comps["path:" + p] = "write"
    return comps


def compute_plan(registry, features, policy, max_parallel):
    if policy not in POLICIES:
        raise ValueError("policy must be one of %s" % (POLICIES,))
    if max_parallel < 1:
        raise ValueError("max-parallel must be >= 1")

    registry_present = bool(registry and registry.get("components"))
    feats = sorted(features, key=lambda f: f["id"])
    ids = [f["id"] for f in feats]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate feature id")

    fc = {f["id"]: _feature_components(f, registry_present) for f in feats}
    degraded = not registry_present or any(
        (not (f.get("components"))) for f in feats
    )

    # Build conflict edges over all feature pairs (sorted for determinism).
    uf = UnionFind(ids)
    edges = []
    for a_i in range(len(ids)):
        for b_i in range(a_i + 1, len(ids)):
            a, b = ids[a_i], ids[b_i]
            shared = sorted(set(fc[a]) & set(fc[b]))
            for c in shared:
                if _conflicts(fc[a][c], fc[b][c], policy):
                    uf.union(a, b)
                    edges.append((a, b, c))
                    break

    # Group features by union-find root → raw lanes.
    groups = {}
    for fid in ids:
        groups.setdefault(uf.find(fid), []).append(fid)
    lanes = [sorted(v) for v in groups.values()]
    # Deterministic order: by first feature id.
    lanes.sort(key=lambda lane: lane[0])

    notes = []
    # Bound lane count by max_parallel: merge smallest lanes (by size, then name).
    if len(lanes) > max_parallel:
        notes.append(
            "lane count %d exceeded max-parallel %d; merged smallest lanes"
            % (len(lanes), max_parallel)
        )
        while len(lanes) > max_parallel:
            lanes.sort(key=lambda lane: (len(lane), lane[0]))
            merged = sorted(lanes[0] + lanes[1])
            lanes = [merged] + lanes[2:]
            lanes.sort(key=lambda lane: lane[0])

    # Assemble per-lane component + write sets.
    lane_objs = []
    provides_by_lane = {}
    requires_by_lane = {}
    for lane in lanes:
        comps, writes, provides, requires = set(), set(), set(), set()
        for fid in lane:
            for c, intent in fc[fid].items():
                comps.add(c)
                if _is_write(intent):
                    writes.add(c)
            f = next(x for x in feats if x["id"] == fid)
            provides |= set(f.get("provides") or [])
            requires |= set(f.get("requires") or [])
        name = lane[0]
        lane_objs.append({
            "lane": name,
            "features": lane,
            "components": sorted(comps),
            "writes": sorted(writes),
        })
        provides_by_lane[name] = provides
        requires_by_lane[name] = requires

    # Cross-lane sequencing: lane Y requires a capability lane X provides.
    sequencing = []
    for y in lane_objs:
        for x in lane_objs:
            if x["lane"] == y["lane"]:
                continue
            shared = sorted(requires_by_lane[y["lane"]] & provides_by_lane[x["lane"]])
            for cap in shared:
                sequencing.append({
                    "after": x["lane"], "before": y["lane"],
                    "reason": "lane '%s' requires '%s' provided by lane '%s'"
                              % (y["lane"], cap, x["lane"]),
                })
    sequencing.sort(key=lambda s: (s["after"], s["before"], s["reason"]))

    if degraded:
        notes.append(
            "DEGRADED: registry absent or incomplete — used file-path/declared "
            "footprint heuristic; confidence low, biasing toward serial. Verify "
            "lanes before parallel dispatch."
        )

    return {
        "policy": policy,
        "max_parallel": max_parallel,
        "registry_present": registry_present,
        "degraded": degraded,
        "lane_count": len(lane_objs),
        "lanes": lane_objs,
        "conflict_edges": [
            {"a": a, "b": b, "component": c} for (a, b, c) in sorted(edges)
        ],
        "sequencing": sequencing,
        "notes": notes,
    }


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _self_test():
    reg = {"registry-version": 1, "components": {
        "auth-jwt": {"source": "components/auth-jwt/", "provides": ["auth"]},
        "billing": {"source": "components/billing/"},
        "http-server": {"source": "lib/http/", "provides": ["http-server"]},
        "ui-kit": {"source": "components/ui/"},
    }}
    feats = [
        {"id": "feat-a", "components": {"auth-jwt": "write", "http-server": "read"},
         "requires": ["http-server"]},
        {"id": "feat-b", "components": {"billing": "write"}},
        {"id": "feat-c", "components": {"auth-jwt": "write"}},          # writes auth-jwt → same lane as feat-a
        {"id": "feat-d", "components": {"http-server": "write"}, "provides": ["http-server"]},
        {"id": "feat-e", "components": {"ui-kit": "write"}},
    ]
    failures = []

    # balanced: feat-a+feat-c share write on auth-jwt → same lane. feat-a reads
    # http-server, feat-d writes it → write-read conflict → same lane too.
    p = compute_plan(reg, feats, "balanced", 5)
    lane_of = {f: L["lane"] for L in p["lanes"] for f in L["features"]}
    if lane_of["feat-a"] != lane_of["feat-c"]:
        failures.append("balanced: feat-a and feat-c (both write auth-jwt) not in same lane")
    if lane_of["feat-a"] != lane_of["feat-d"]:
        failures.append("balanced: feat-a (read) and feat-d (write) on http-server not merged")
    if lane_of["feat-b"] == lane_of["feat-a"]:
        failures.append("balanced: feat-b (billing) wrongly merged with feat-a")
    if lane_of["feat-e"] == lane_of["feat-a"]:
        failures.append("balanced: feat-e (ui-kit) wrongly merged with feat-a")

    # aggressive: feat-a(read http) vs feat-d(write http) is write-read → NOT a
    # conflict under aggressive, so they separate (unless joined via auth-jwt).
    pa = compute_plan(reg, feats, "aggressive", 5)
    la = {f: L["lane"] for L in pa["lanes"] for f in L["features"]}
    if la["feat-a"] == la["feat-d"]:
        failures.append("aggressive: feat-a/feat-d should NOT merge on write-read")
    if la["feat-a"] != la["feat-c"]:
        failures.append("aggressive: feat-a/feat-c (write-write auth-jwt) must merge")

    # conservative: feat-a reads http, feat-d writes it; any-share → same lane.
    pc = compute_plan(reg, feats, "conservative", 5)
    lc = {f: L["lane"] for L in pc["lanes"] for f in L["features"]}
    if lc["feat-a"] != lc["feat-d"]:
        failures.append("conservative: any shared component must merge feat-a/feat-d")

    # determinism: identical inputs ⇒ identical output.
    if json.dumps(compute_plan(reg, feats, "balanced", 5), sort_keys=True) != \
       json.dumps(compute_plan(reg, feats, "balanced", 5), sort_keys=True):
        failures.append("non-deterministic output")

    # max-parallel bound: 4 independent features, cap 2 → exactly 2 lanes.
    indep = [{"id": "i%d" % i, "components": {"c%d" % i: "write"}} for i in range(4)]
    pb = compute_plan(reg, indep, "balanced", 2)
    if pb["lane_count"] != 2:
        failures.append("max-parallel: expected 2 lanes, got %d" % pb["lane_count"])

    # registry-absent fallback uses paths and flags degraded.
    pf = compute_plan(None,
                      [{"id": "x", "paths": ["src/a/"]}, {"id": "y", "paths": ["src/a/"]}],
                      "balanced", 3)
    if not pf["degraded"]:
        failures.append("fallback: degraded flag not set when registry absent")
    lf = {f: L["lane"] for L in pf["lanes"] for f in L["features"]}
    if lf["x"] != lf["y"]:
        failures.append("fallback: shared path should merge x and y")

    # sequencing: feat-a requires http-server provided by feat-d's lane — but
    # they're in the same lane under balanced, so no cross-lane edge. Check the
    # aggressive plan where they may separate.
    seq_ok = all("reason" in s for s in pa["sequencing"])
    if not seq_ok:
        failures.append("sequencing entries malformed")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("pm_overlap self-test: OK (balanced/aggressive/conservative, "
          "determinism, max-parallel bound, registry-absent fallback)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic feature-overlap → lane plan.")
    ap.add_argument("--registry")
    ap.add_argument("--features")
    ap.add_argument("--policy", default="balanced", choices=POLICIES)
    ap.add_argument("--max-parallel", type=int, default=3)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.features:
        ap.error("--features is required (or use --self-test)")

    try:
        registry = _load(args.registry) if args.registry else None
        fdata = _load(args.features)
        features = fdata.get("features", fdata) if isinstance(fdata, dict) else fdata
        plan = compute_plan(registry, features, args.policy, args.max_parallel)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
