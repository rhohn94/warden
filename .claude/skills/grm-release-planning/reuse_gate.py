#!/usr/bin/env python3
"""reuse_gate.py — planning-time reuse gate for grm-release-planning (#408, v3.97).

Backs `grm-release-planning`'s mandatory reuse-consult step, mirroring the
existing `Grimoire-Requirement` tracker read (SKILL.md Step 2's boxed note —
"the one wired loop that already works"): for each work item under
consideration, look up whether its candidate capability tags overlap an
already-cataloged component's `provides` list in
`.claude/component-registry.json`, cross-referenced against the
component-taxonomy vocabulary (`docs/grimoire/design/component-taxonomy.md` §3).
The agent decides which tags a work item's description maps to (a judgment
call — free-form prose, not a deterministic input); this script owns the
deterministic lookup once tags are chosen, same division of labor as
`component_registry.py` owns the registry build.

Degrade-gracefully contract — this is DOCUMENTED, EXPECTED behavior, not a bug
to route around (see `docs/release-planning/release-planning-v3.97.md`
ITEM-5/#408, and its own companion-ticket note: "land both in the same arc or
the gate is a no-op"). This repo has no `components/`/`lib/` directory and is
not itself a managed project in the registry's shape — a no-op here is
correct:

  - `.claude/component-registry.json` absent -> no-op: `registry-found` is
    False, every queried tag's matches list is empty, `no-op` is True.
  - Registry present but `components` is empty -> no-op: `registry-found` is
    True, `registry-empty` is True, `no-op` is True.
  - `docs/grimoire/design/component-taxonomy.md` absent -> the taxonomy
    degrades to an unknown vocabulary (mirrors `component_registry.py`'s
    `Taxonomy.empty()` graceful-degrade fix, ported here so this script does
    not repeat the stale-path regression currently present in this repo's own
    root copy of `component_registry.py` — see that script's `TAXONOMY_PATH`).
    The provides-overlap query still runs against the registry's raw
    `provides` tags; taxonomy-membership annotation is just skipped.
  - A malformed/corrupt JSON file that IS present is a real authoring bug and
    is NOT silently degraded — raises `RegistryError` (exit 2). Absence and
    corruption are different failure modes; only absence is a no-op.

Standard: Python 3 stdlib-only (docs/grimoire/design/scripting-unification-design.md §3).

CLI:
  reuse_gate.py query <tag> [<tag> ...] [--root DIR]
  reuse_gate.py --self-test
Exit 0 for any successful query (a no-op result is still exit 0 — it is a
valid, expected outcome, not a failure); 2 only when a present
component-registry.json fails to parse as JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ── Constants (no magic numbers / strings inline) ───────────────────────────
REGISTRY_PATH = os.path.join(".claude", "component-registry.json")
# Same project-relative path component_registry.py's canonical (claude-code/)
# copy uses. Framework-internal; a managed project may or may not ship it —
# either way Taxonomy.load() below degrades gracefully rather than raising.
TAXONOMY_PATH = os.path.join("docs", "grimoire", "design", "component-taxonomy.md")
JSON_INDENT = 2
CAPABILITY_SECTION_RE = re.compile(r"^##\s+3\.\s", re.MULTILINE)
NEXT_SECTION_RE = re.compile(r"^##\s+\d+\.\s", re.MULTILINE)
TAXONOMY_TERM_RE = re.compile(r"^\s*\|\s*`([a-z][a-z0-9-]*)`\s*\|")


class RegistryError(Exception):
    """Raised only for a present-but-malformed component-registry.json (exit 2)."""


# ── Taxonomy authority (provides/requires vocabulary only — §3) ─────────────
class Taxonomy:
    """§3 `provides`/`requires` capability vocabulary, read live from
    component-taxonomy.md.

    Mirrors `grm-component-registry`'s `component_registry.py` `Taxonomy`
    class and its degrade-gracefully contract: an absent doc is not an error,
    it just means tag-vocabulary membership can't be annotated (`is_known`
    returns `None` — "unknown", not "no").
    """

    def __init__(self, capabilities):
        self.capabilities = capabilities  # set[str] | None (None = vocab unknown)

    @classmethod
    def empty(cls):
        return cls(capabilities=None)

    @classmethod
    def load(cls, root="."):
        path = os.path.join(root, TAXONOMY_PATH)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            # Genuinely absent (framework-internal doc not shipped, or a
            # managed project hasn't supplied one) -> degrade, don't fail.
            return cls.empty()
        return cls(cls._terms_in_section(text))

    @staticmethod
    def _terms_in_section(text):
        m = CAPABILITY_SECTION_RE.search(text)
        if not m:
            return set()
        rest = text[m.end():]
        nxt = NEXT_SECTION_RE.search(rest)
        body = rest[:nxt.start()] if nxt else rest
        terms = set()
        for line in body.splitlines():
            tm = TAXONOMY_TERM_RE.match(line)
            if tm:
                terms.add(tm.group(1))
        return terms

    def is_known(self, tag):
        """True/False if the vocabulary was loaded, else None ("can't say")."""
        if self.capabilities is None:
            return None
        return tag in self.capabilities


# ── Registry load (absence = no-op; corruption = error) ─────────────────────
def load_registry(root="."):
    """Returns (registry_dict_or_None, found: bool).

    Absence is the documented no-op path, not an error. A present-but-corrupt
    file IS an error (raises RegistryError) — a real authoring bug worth
    surfacing, distinct from "no registry exists yet."
    """
    path = os.path.join(root, REGISTRY_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None, False
    try:
        return json.loads(text), True
    except ValueError as exc:
        raise RegistryError("component-registry.json is malformed: %s" % exc)


def query_provides(registry, tags):
    """tag -> sorted [component ids] whose `provides` list includes tag."""
    components = (registry or {}).get("components") or {}
    matches = {}
    for tag in tags:
        hits = sorted(cid for cid, meta in components.items()
                       if tag in (meta.get("provides") or []))
        matches[tag] = hits
    return matches


def run_query(tags, root="."):
    """The one entry point the CLI (and the calling skill step) uses."""
    registry, found = load_registry(root)
    taxonomy = Taxonomy.load(root)
    components = (registry or {}).get("components") or {}
    matches = query_provides(registry, tags)
    overlap_tags = sorted(t for t, hits in matches.items() if hits)
    not_in_taxonomy = sorted(t for t in tags if taxonomy.is_known(t) is False)
    empty = found and not components
    no_op = (not found) or empty

    if no_op:
        note = ("no-op: component-registry.json is absent" if not found
                 else "no-op: component-registry.json has no cataloged components")
    elif overlap_tags:
        note = ("provides overlap found for: %s — a plan item using one of "
                 "these tags needs a written justification (§2.{N} "
                 "'Reuse resolution:' line)" % ", ".join(overlap_tags))
    else:
        note = "registry consulted, no provides overlap for the queried tags"

    return {
        "registry-path": REGISTRY_PATH,
        "registry-found": found,
        "registry-empty": empty,
        "queried-tags": list(tags),
        "matches": matches,
        "overlap-tags": overlap_tags,
        "tags-not-in-taxonomy": not_in_taxonomy,
        "no-op": no_op,
        "note": note,
    }


# ── Self-test (fixtures in a temp tree; never this repo's real sources) ─────
def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _fixture_taxonomy_text():
    return """# Component taxonomy

## 2. `profiles`

| Term | Meaning |
|---|---|
| `api` | api. |

## 3. `provides` / `requires` — capability vocabulary

| Term | Meaning |
|---|---|
| `auth` | Authentication / authorization. |
| `http-client` | Outbound HTTP request capability. |
| `messaging` | Async message/event transport. |

## 4. Adding a term
"""


def _fixture_registry(components):
    return json.dumps({"registry-version": 1, "generated-from": ["components/"],
                        "components": components, "uncataloged": [],
                        "unknown-tags": []})


def _self_test() -> int:
    import tempfile

    failures = []

    # 1) Overlap path: fixture registry has a component providing "auth";
    #    querying ["auth", "messaging"] must find the "auth" overlap and miss
    #    "messaging" (present in taxonomy, absent from the registry).
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, TAXONOMY_PATH), _fixture_taxonomy_text())
        _write(os.path.join(root, REGISTRY_PATH), _fixture_registry({
            "auth-jwt": {"version": "v1.2.0", "summary": "JWT auth.",
                         "profiles": ["api"], "provides": ["auth"],
                         "requires": [], "stability": "stable",
                         "source": "components/auth-jwt/"},
        }))
        res = run_query(["auth", "messaging"], root=root)
        if res["registry-found"] is not True or res["registry-empty"] is not False:
            failures.append("overlap fixture: expected a found, non-empty registry: %r" % res)
        if res["no-op"] is not False:
            failures.append("overlap fixture: expected no-op=False (a real overlap exists): %r" % res)
        if res["matches"].get("auth") != ["auth-jwt"]:
            failures.append("overlap fixture: expected auth -> [auth-jwt]: %r" % res["matches"])
        if res["matches"].get("messaging") != []:
            failures.append("overlap fixture: expected messaging -> []: %r" % res["matches"])
        if res["overlap-tags"] != ["auth"]:
            failures.append("overlap fixture: expected overlap-tags == ['auth']: %r" % res["overlap-tags"])
        if res["tags-not-in-taxonomy"] != []:
            failures.append("overlap fixture: both tags are in taxonomy, expected []: %r" % res)

    # 2) Absent-registry path (this repo's own real-world case): no
    #    .claude/component-registry.json at all -> inert no-op, not an error.
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, TAXONOMY_PATH), _fixture_taxonomy_text())
        res = run_query(["auth"], root=root)
        if res["registry-found"] is not False:
            failures.append("absent-registry fixture: expected registry-found=False: %r" % res)
        if res["no-op"] is not True:
            failures.append("absent-registry fixture: expected no-op=True: %r" % res)
        if res["matches"] != {"auth": []}:
            failures.append("absent-registry fixture: expected empty matches: %r" % res["matches"])

    # 3) Present-but-empty-components registry -> also a no-op (registry
    #    exists, has never been populated — same shape a fresh
    #    `component_registry.py build` on a component-free project produces).
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, TAXONOMY_PATH), _fixture_taxonomy_text())
        _write(os.path.join(root, REGISTRY_PATH), _fixture_registry({}))
        res = run_query(["auth"], root=root)
        if res["registry-found"] is not True or res["registry-empty"] is not True:
            failures.append("empty-registry fixture: expected found+empty: %r" % res)
        if res["no-op"] is not True:
            failures.append("empty-registry fixture: expected no-op=True: %r" % res)

    # 4) Absent taxonomy doc -> degrades (is_known -> None -> never listed as
    #    "not in taxonomy"), query against the registry still works normally.
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, REGISTRY_PATH), _fixture_registry({
            "auth-jwt": {"version": "v1.0.0", "summary": "Auth.",
                         "profiles": [], "provides": ["auth"], "requires": [],
                         "stability": "stable", "source": "components/auth-jwt/"},
        }))
        res = run_query(["auth", "not-a-real-tag"], root=root)
        if res["tags-not-in-taxonomy"] != []:
            failures.append("missing-taxonomy fixture: unknown vocab must not "
                             "flag anything as not-in-taxonomy: %r" % res)
        if res["matches"].get("auth") != ["auth-jwt"]:
            failures.append("missing-taxonomy fixture: registry query must "
                             "still work without a taxonomy doc: %r" % res)

    # 5) Malformed-but-present registry -> a real error, not a degrade.
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, REGISTRY_PATH), "{not valid json")
        try:
            run_query(["auth"], root=root)
        except RegistryError:
            pass
        else:
            failures.append("malformed-registry fixture: expected RegistryError to raise")

    if failures:
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        return 1
    print("reuse_gate self-test: OK (overlap-found path, absent-registry "
          "no-op, empty-registry no-op, absent-taxonomy graceful-degrade, "
          "malformed-registry raises)")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic planning-time reuse gate: query "
                     "component-registry.json `provides` overlap for a set "
                     "of candidate capability tags (#408).")
    ap.add_argument("verb", nargs="?", help="query")
    ap.add_argument("tags", nargs="*", help="candidate capability tags to query")
    ap.add_argument("--root", default=".")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.verb:
        ap.error("a verb is required (query) or --self-test")
    if args.verb != "query":
        ap.error("unknown verb: %s" % args.verb)
    if not args.tags:
        ap.error("query requires at least one capability tag")

    try:
        result = run_query(args.tags, root=args.root)
    except RegistryError as exc:
        print("reuse_gate: %s" % exc, file=sys.stderr)
        return 2

    print(json.dumps(result, indent=JSON_INDENT, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
