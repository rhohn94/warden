#!/usr/bin/env python3
"""component_registry_merge.py — deterministic 3-way merge engine for
`.claude/component-registry.json` distribution over the sync channel
(#REG-4, Pillar 4 of docs/grimoire/design/component-catalog-architecture-design.md).

Backs `sync-from-upstream.sh`'s classification loop: the component registry is
a RECOGNIZED, merged sync artifact (design Pillar 4), but a plain textual
`git merge-file` 3-way merge — the mechanism used for every other synced file —
does not give the correctness guarantee the artifact needs. Verified
empirically (see this module's `--self-test`, case "disjoint additions"): two
independent component additions to the SAME registry both edit the region next
to the map's closing brace / a shared entry's trailing comma, so `git
merge-file` reports a CONFLICT even though the additions are semantically
disjoint. This engine replaces the textual merge for this one recognized
artifact with a structural, per-component-id 3-way merge so:

  - disjoint additions/edits on each side merge cleanly, never a false CONFLICT;
  - a genuine same-id collision (both sides changed the SAME component to
    DIFFERENT content) is still surfaced, never silently resolved either way;
  - nothing is ever silently dropped (no data loss) — every component id
    present on either side survives the merge unless *both* sides agree to
    remove it.

Merge policy — per component id, keyed by `components.<id>`, given its value
on the LOCAL, BASE (recorded common ancestor — absent on a first sync), and
UPSTREAM side:

  - `local == upstream`            -> keep it (identical; includes both-absent).
  - `local == base` (unchanged locally) -> take upstream's value (fast-forward;
    upstream absent == upstream deleted it -> honor the deletion).
  - `upstream == base` (unchanged upstream) -> keep local's value (incl. a
    local deletion).
  - otherwise both sides changed it to DIFFERENT values (including two
    independent additions of the same id with no base entry at all) -> a
    genuine CONFLICT.

On a genuine conflict this engine does NOT write the merged registry (the
hard-refuse discipline `grm-sync-deps` already uses for a bad artifact —
nothing placed, nothing lost — reused here rather than inventing a new
policy) and does NOT embed textual `<<<<<<<`/`=======`/`>>>>>>>` markers
inside the JSON (that would silently corrupt the registry schema for every
OTHER component entry a consumer's tooling reads). Instead every conflicting
id is reported (local/base/upstream values) via `--conflicts-out`, for a human
(or a follow-up automation) to resolve explicitly — the file-level `CONFLICT`
classification `sync-from-upstream.sh` already surfaces for any other file.

Top-level bookkeeping fields (`registry-version`, `generated-from`,
`uncataloged`, `unknown-tags`, `paths-skipped`) are DERIVED, not user data —
merged permissively (max / set-union) and never conflict.

File-write contract: writes ONLY `--out` (and only on a clean merge), plus
`--conflicts-out` when conflicts exist. Atomic (temp + os.replace). Never runs
git, never touches the registry's source components. The caller
(`sync-from-upstream.sh`) commits nothing itself either — same as every other
managed file in that walk.

Standard: Python 3 stdlib-only (docs/grimoire/design/scripting-unification-design.md §3).

CLI:
  component_registry_merge.py merge --local L --base B --upstream U --out OUT
      [--conflicts-out FILE]
  component_registry_merge.py --self-test

Exit codes (mirrors the grm-sync-deps / sync-from-upstream 0/1/2 contract):
  0 = clean merge, written to --out.
  1 = one or more entry-level conflicts — --out NOT written, local untouched;
      --conflicts-out (if given) carries the structured report.
  2 = malformed input (unreadable-as-JSON local/base/upstream file).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# ── Constants (no magic strings inline) ──────────────────────────────────────
REGISTRY_VERSION_KEY = "registry-version"
COMPONENTS_KEY = "components"
GENERATED_FROM_KEY = "generated-from"
UNCATALOGED_KEY = "uncataloged"
UNKNOWN_TAGS_KEY = "unknown-tags"
PATHS_SKIPPED_KEY = "paths-skipped"
JSON_INDENT = 2
DEFAULT_REGISTRY_VERSION = 1


class MergeError(Exception):
    """Raised on unreadable-as-JSON input (-> exit 2)."""


# ── Loading ───────────────────────────────────────────────────────────────────
def _load(path):
    """Load a registry JSON file. Absent -> {} (first-sync / no-base case is
    not an error). Malformed JSON on an EXISTING file -> MergeError."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as exc:
        raise MergeError(f"malformed JSON in {path}: {exc}")


# ── Per-component structural merge ───────────────────────────────────────────
def merge_components(local_c: dict, base_c: dict, upstream_c: dict):
    """Return (merged_dict, conflicts_list) for the `components` maps.

    `conflicts_list` entries: {"id": ..., "local": ..., "base": ..., "upstream": ...}
    (`base` is None when no recorded base entry existed for that id).
    """
    merged = {}
    conflicts = []
    all_ids = set(local_c) | set(base_c) | set(upstream_c)
    for cid in sorted(all_ids):
        local_v = local_c.get(cid)
        base_v = base_c.get(cid)
        upstream_v = upstream_c.get(cid)

        if local_v == upstream_v:
            if local_v is not None:
                merged[cid] = local_v
            continue  # both absent (both sides deleted it) -> stays removed

        if local_v == base_v:
            # Unchanged locally since base -> fast-forward to upstream.
            if upstream_v is not None:
                merged[cid] = upstream_v
            continue  # upstream_v is None -> upstream deleted it; honor that

        if upstream_v == base_v:
            # Unchanged upstream since base -> keep local's value/deletion.
            if local_v is not None:
                merged[cid] = local_v
            continue

        # Both sides changed it (or both independently added it with no base
        # entry) to genuinely DIFFERENT values -> a real conflict.
        conflicts.append({"id": cid, "local": local_v, "base": base_v,
                          "upstream": upstream_v})
    return merged, conflicts


def _merge_unknown_tags(local_list, upstream_list):
    """Union, deduped by (component, field, tag), sorted deterministically."""
    seen = {}
    for rec in list(local_list) + list(upstream_list):
        key = (rec.get("component"), rec.get("field"), rec.get("tag"))
        seen[key] = rec
    return sorted(seen.values(), key=lambda r: (r.get("component") or "",
                                                r.get("field") or "",
                                                r.get("tag") or ""))


# ── Whole-registry merge ─────────────────────────────────────────────────────
def merge_registry(local: dict, base: dict, upstream: dict):
    """Return (merged_registry, conflicts) for a full registry object.

    Any of the three may be {} (registry absent on that side — e.g. a fresh
    consumer with no local registry yet, or a first sync with no recorded
    base).
    """
    local = local or {}
    base = base or {}
    upstream = upstream or {}
    local_c = local.get(COMPONENTS_KEY, {}) or {}
    base_c = base.get(COMPONENTS_KEY, {}) or {}
    upstream_c = upstream.get(COMPONENTS_KEY, {}) or {}

    merged_c, conflicts = merge_components(local_c, base_c, upstream_c)
    if conflicts:
        # Conflicts present -> caller refuses to write; still return a
        # best-effort merged view for callers that want to inspect it, but
        # the CLI path never persists it.
        return None, conflicts

    merged = {
        REGISTRY_VERSION_KEY: max(
            local.get(REGISTRY_VERSION_KEY, DEFAULT_REGISTRY_VERSION) or DEFAULT_REGISTRY_VERSION,
            upstream.get(REGISTRY_VERSION_KEY, DEFAULT_REGISTRY_VERSION) or DEFAULT_REGISTRY_VERSION,
        ),
        GENERATED_FROM_KEY: sorted(set(local.get(GENERATED_FROM_KEY, []) or []) |
                                   set(upstream.get(GENERATED_FROM_KEY, []) or [])),
        COMPONENTS_KEY: merged_c,
    }
    uncataloged = sorted(set(local.get(UNCATALOGED_KEY, []) or []) |
                         set(upstream.get(UNCATALOGED_KEY, []) or []))
    if uncataloged:
        merged[UNCATALOGED_KEY] = uncataloged
    unknown = _merge_unknown_tags(local.get(UNKNOWN_TAGS_KEY, []) or [],
                                   upstream.get(UNKNOWN_TAGS_KEY, []) or [])
    if unknown:
        merged[UNKNOWN_TAGS_KEY] = unknown
    skipped = sorted(set(local.get(PATHS_SKIPPED_KEY, []) or []) |
                     set(upstream.get(PATHS_SKIPPED_KEY, []) or []))
    if skipped:
        merged[PATHS_SKIPPED_KEY] = skipped
    return merged, []


# ── Serialization + atomic write (mirrors component_registry.py) ────────────
def serialize(registry: dict) -> str:
    return json.dumps(registry, sort_keys=True, indent=JSON_INDENT,
                      ensure_ascii=False) + "\n"


def _write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ── Engine facade ─────────────────────────────────────────────────────────────
def run_merge(local_path, base_path, upstream_path, out_path, conflicts_out=None):
    """Load, merge, and (on success) write. Returns (exit_code, conflicts)."""
    local = _load(local_path)
    base = _load(base_path)
    upstream = _load(upstream_path)
    merged, conflicts = merge_registry(local, base, upstream)
    if conflicts:
        if conflicts_out:
            report = {"conflicts": conflicts}
            _write_atomic(conflicts_out, json.dumps(
                report, sort_keys=True, indent=JSON_INDENT, ensure_ascii=False) + "\n")
        return 1, conflicts
    _write_atomic(out_path, serialize(merged))
    return 0, []


# ── Self-test (fixtures only — never the repo's real registry) ──────────────
def _self_test() -> int:
    failures = []

    # 1) DISJOINT ADDITIONS — the false-conflict regression this engine exists
    #    to fix. local adds one component, upstream adds a DIFFERENT one; both
    #    unrelated to a shared unchanged entry. Must merge cleanly, no data loss.
    base = {"components": {"auth-core": {"version": "1.0.0"}}}
    local = {"components": {"auth-core": {"version": "1.0.0"},
                            "local-widget": {"version": "1.0.0"}}}
    upstream = {"components": {"auth-core": {"version": "1.0.0"},
                               "upstream-widget": {"version": "2.0.0"}}}
    merged, conflicts = merge_registry(local, base, upstream)
    if conflicts:
        failures.append("disjoint additions: false conflict %r" % conflicts)
    elif set(merged["components"]) != {"auth-core", "local-widget", "upstream-widget"}:
        failures.append("disjoint additions: data loss %r" % sorted(merged["components"]))

    # 2) NO-OP — local == upstream == base -> merged identical, no conflicts.
    same = {"components": {"x": {"version": "1.0.0"}}}
    merged2, conflicts2 = merge_registry(same, same, same)
    if conflicts2 or merged2["components"] != same["components"]:
        failures.append("no-op merge changed content: %r / %r" % (merged2, conflicts2))

    # 3) UPSTREAM-ONLY EDIT — local unchanged since base -> fast-forward.
    base3 = {"components": {"x": {"version": "1.0.0"}}}
    local3 = {"components": {"x": {"version": "1.0.0"}}}   # untouched
    upstream3 = {"components": {"x": {"version": "1.1.0"}}}
    merged3, conflicts3 = merge_registry(local3, base3, upstream3)
    if conflicts3 or merged3["components"]["x"]["version"] != "1.1.0":
        failures.append("upstream-only edit not fast-forwarded: %r / %r" % (merged3, conflicts3))

    # 4) LOCAL-ONLY EDIT — upstream unchanged since base -> keep local.
    base4 = {"components": {"x": {"version": "1.0.0"}}}
    local4 = {"components": {"x": {"version": "1.0.0-local-patch"}}}
    upstream4 = {"components": {"x": {"version": "1.0.0"}}}  # untouched
    merged4, conflicts4 = merge_registry(local4, base4, upstream4)
    if conflicts4 or merged4["components"]["x"]["version"] != "1.0.0-local-patch":
        failures.append("local-only edit not kept: %r / %r" % (merged4, conflicts4))

    # 5) GENUINE CONFLICT — same id, both sides changed it to DIFFERENT values.
    #    Documented behavior: refused (no merged registry), both sides reported.
    base5 = {"components": {"auth-core": {"version": "1.0.0"}}}
    local5 = {"components": {"auth-core": {"version": "2.0.0-local"}}}
    upstream5 = {"components": {"auth-core": {"version": "2.0.0-upstream"}}}
    merged5, conflicts5 = merge_registry(local5, base5, upstream5)
    if merged5 is not None:
        failures.append("genuine conflict should refuse to produce a merged registry")
    if len(conflicts5) != 1 or conflicts5[0]["id"] != "auth-core":
        failures.append("genuine conflict not reported correctly: %r" % conflicts5)
    elif (conflicts5[0]["local"]["version"] != "2.0.0-local" or
          conflicts5[0]["upstream"]["version"] != "2.0.0-upstream"):
        failures.append("conflict report lost a side's value: %r" % conflicts5)

    # 6) INDEPENDENT SAME-ID ADDITION, NO BASE ENTRY — both sides add "x" fresh
    #    with different content (first-sync scenario, base has no entry at all).
    #    Must still be treated as a genuine conflict, not silently picked.
    base6 = {"components": {}}
    local6 = {"components": {"x": {"version": "1.0.0"}}}
    upstream6 = {"components": {"x": {"version": "9.9.9"}}}
    merged6, conflicts6 = merge_registry(local6, base6, upstream6)
    if merged6 is not None or len(conflicts6) != 1:
        failures.append("independent same-id addition not conflicted: %r / %r" % (merged6, conflicts6))

    # 7) DELETIONS — local deletes an id unchanged upstream -> honored;
    #    upstream deletes an id unchanged locally -> honored. No false conflict.
    base7 = {"components": {"a": {"version": "1"}, "b": {"version": "1"}}}
    local7 = {"components": {"b": {"version": "1"}}}          # local deleted "a"
    upstream7 = {"components": {"a": {"version": "1"}}}       # upstream deleted "b"
    merged7, conflicts7 = merge_registry(local7, base7, upstream7)
    if conflicts7 or set(merged7["components"]) != set():
        failures.append("disjoint deletions mishandled: %r / %r" % (merged7, conflicts7))

    # 8) NO BASE AT ALL (first sync) — local-only and upstream-only components
    #    both survive; a shared, identical entry survives once.
    local8 = {"components": {"a": {"version": "1"}, "shared": {"version": "1"}}}
    upstream8 = {"components": {"b": {"version": "1"}, "shared": {"version": "1"}}}
    merged8, conflicts8 = merge_registry(local8, {}, upstream8)
    if conflicts8 or set(merged8["components"]) != {"a", "b", "shared"}:
        failures.append("no-base merge lost/conflicted data: %r / %r" % (merged8, conflicts8))

    # 9) BOOKKEEPING FIELDS — union/max, never conflict.
    local9 = {"components": {}, "registry-version": 1, "generated-from": ["components/"],
              "uncataloged": ["lib/legacy/"],
              "unknown-tags": [{"component": "x", "field": "provides", "tag": "foo"}]}
    upstream9 = {"components": {}, "registry-version": 2, "generated-from": ["lib/"],
                "uncataloged": ["lib/other/"],
                "unknown-tags": [{"component": "y", "field": "requires", "tag": "bar"},
                                  {"component": "x", "field": "provides", "tag": "foo"}]}
    merged9, conflicts9 = merge_registry(local9, {}, upstream9)
    if conflicts9:
        failures.append("bookkeeping-only merge conflicted: %r" % conflicts9)
    if merged9[REGISTRY_VERSION_KEY] != 2:
        failures.append("registry-version not maxed: %r" % merged9)
    if set(merged9[GENERATED_FROM_KEY]) != {"components/", "lib/"}:
        failures.append("generated-from not unioned: %r" % merged9)
    if set(merged9[UNCATALOGED_KEY]) != {"lib/legacy/", "lib/other/"}:
        failures.append("uncataloged not unioned: %r" % merged9)
    if len(merged9[UNKNOWN_TAGS_KEY]) != 2:
        failures.append("unknown-tags not deduped/unioned: %r" % merged9[UNKNOWN_TAGS_KEY])

    # 10) CLI ROUND-TRIP via run_merge(): clean merge writes --out atomically;
    #     idempotent re-merge is byte-identical; a conflict writes NOTHING to
    #     --out (existing content, if any, left untouched) and DOES write
    #     --conflicts-out.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "local.json")
        bp = os.path.join(td, "base.json")
        up = os.path.join(td, "upstream.json")
        op = os.path.join(td, "out.json")
        with open(lp, "w", encoding="utf-8") as fh:
            json.dump(local, fh)
        with open(bp, "w", encoding="utf-8") as fh:
            json.dump(base, fh)
        with open(up, "w", encoding="utf-8") as fh:
            json.dump(upstream, fh)
        rc, confl = run_merge(lp, bp, up, op)
        if rc != 0 or confl:
            failures.append("CLI clean-merge rc/conflicts: %r/%r" % (rc, confl))
        if not os.path.exists(op):
            failures.append("CLI clean-merge did not write --out")
        else:
            first = _read(op)
            rc2, _ = run_merge(lp, bp, up, op)
            if rc2 != 0 or _read(op) != first:
                failures.append("CLI re-merge not idempotent/byte-identical")

        # Now force a genuine conflict and confirm --out is left untouched.
        lp2 = os.path.join(td, "local2.json")
        up2 = os.path.join(td, "upstream2.json")
        cp = os.path.join(td, "conflicts.json")
        with open(lp2, "w", encoding="utf-8") as fh:
            json.dump(local5, fh)
        with open(up2, "w", encoding="utf-8") as fh:
            json.dump(upstream5, fh)
        bp2 = os.path.join(td, "base2.json")
        with open(bp2, "w", encoding="utf-8") as fh:
            json.dump(base5, fh)
        before = _read(op)
        rc3, confl3 = run_merge(lp2, bp2, up2, op, conflicts_out=cp)
        if rc3 != 1 or not confl3:
            failures.append("CLI conflict path rc/conflicts wrong: %r/%r" % (rc3, confl3))
        if _read(op) != before:
            failures.append("CLI conflict path wrote to --out (should be untouched)")
        if not os.path.exists(cp):
            failures.append("CLI conflict path did not write --conflicts-out")
        else:
            report = json.loads(_read(cp))
            if not report.get("conflicts"):
                failures.append("conflicts-out report empty: %r" % report)

    # 11) MALFORMED JSON -> MergeError (exit 2 path).
    with tempfile.TemporaryDirectory() as td2:
        bad = os.path.join(td2, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        try:
            _load(bad)
            failures.append("malformed JSON should raise MergeError")
        except MergeError:
            pass

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("component_registry_merge self-test: OK (disjoint additions merge "
          "clean [the false-conflict regression this engine fixes], no-op, "
          "upstream-only fast-forward, local-only preserved, genuine same-id "
          "conflict refused+reported+non-destructive, independent same-id "
          "no-base addition conflicted, disjoint deletions honored, no-base "
          "first-sync merge lossless, bookkeeping fields unioned/maxed, CLI "
          "round-trip idempotent + conflict leaves --out untouched, malformed "
          "JSON raises)")
    return 0


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Structural 3-way merge for .claude/component-registry.json "
                    "(Pillar 4 distribution over the sync channel).")
    ap.add_argument("verb", nargs="?", help="merge")
    ap.add_argument("--local")
    ap.add_argument("--base")
    ap.add_argument("--upstream")
    ap.add_argument("--out")
    ap.add_argument("--conflicts-out")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if args.verb != "merge":
        ap.error("a verb is required (merge) or --self-test")
    if not args.local or not args.base or not args.upstream or not args.out:
        ap.error("merge requires --local, --base, --upstream, and --out")

    try:
        rc, conflicts = run_merge(args.local, args.base, args.upstream,
                                  args.out, args.conflicts_out)
    except MergeError as exc:
        print("component_registry_merge: %s" % exc, file=sys.stderr)
        return 2

    if rc == 1:
        ids = ", ".join(sorted(c["id"] for c in conflicts))
        print("component_registry_merge: CONFLICT on component id(s): %s" % ids,
              file=sys.stderr)
        print("  --out left untouched; resolve by hand" +
              (" (see %s)" % args.conflicts_out if args.conflicts_out else ""),
              file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
