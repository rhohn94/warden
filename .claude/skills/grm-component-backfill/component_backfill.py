#!/usr/bin/env python3
"""component_backfill.py — one-time component-metadata backfill engine (#460, v3.97).

Backs the `grm-component-backfill` skill: sweep a project's resolved
component-scan paths (the same `component_registry.py` Discovery contract —
component.json at a directory root, or front-matter with a `component:`
block), identify reusable-looking directories that are `uncataloged` (no
metadata found), and author a `component.json` for the ones this engine can
describe with SOME confidence from what is actually on disk (a README.md or a
leading module docstring). Everything else stays `uncataloged`, surfaced with
an explicit reason string — this engine never invents a summary, profile, or
capability tag it cannot source from the candidate's own files
(component-catalog-architecture-design.md's "surfaced, never silently
accepted/invented" principle, applied to authorship rather than just taxonomy
validation).

One-time, bounded, report-first:
  1. `report` — read-only. Classifies every currently-uncataloged candidate as
     CONFIDENT (would author a component.json on `apply`) or UNCATALOGED
     (stays, with a reason), and prints a token estimate (chars read to source
     each summary, plus a small per-candidate authoring overhead, all // 4 —
     the same rough chars-per-token proxy `grm-token-measure/footprint.py`
     uses) for the `apply` step.
  2. `apply` — writes `component.json` for every CONFIDENT candidate only
     (exactly `component_registry.py`'s META_FIELDS schema — id, summary,
     profiles, provides, requires, compat, stability, source, optional
     version; only fields this engine can actually source are populated,
     nothing else is invented), then runs `component_registry.py build` and
     reports the diff (added ids) + the remaining `uncataloged` set straight
     from the fresh registry build.

Idempotent: a directory that already carries metadata (a component.json this
engine just wrote, or one that already existed) is a real `Component` per
Discovery, not `uncataloged` — the next `report`/`apply` never revisits it.
Re-running `apply` after a successful run writes nothing new.

Confidence heuristic (deterministic; no LLM judgement inside the engine — an
invoking agent MAY read further and hand-author richer profiles/provides/
requires for a specific candidate afterward; this engine's job is the
mechanical, self-testable floor):
  - DISQUALIFIED — the directory's own basename is a generic/grab-bag name
    (misc, utils, helpers, common, shared, tmp, scratch, legacy, ...) →
    always UNCATALOGED, reason cites the name.
  - CONFIDENT — not disqualified, AND a description source exists (a
    README.md with a non-empty first prose line, or a single primary source
    file with a leading module docstring / block comment) AND the directory
    has <= MAX_FILES_FOR_CONFIDENCE non-hidden, non-test top-level files.
  - Otherwise UNCATALOGED — reason names what was missing (no description
    source, or too many top-level files to infer one clear purpose).

File-write contract: `apply` writes ONLY `<candidate>/component.json` (one per
CONFIDENT candidate) plus whatever `component_registry.py build` writes
(`.claude/component-registry.json`) — it calls that engine's public API
directly (sibling-skill import, matching the `grm-cost-budget` /
`grm-token-measure` precedent) rather than re-deriving registry-build logic.
Never runs git; the agent commits.

Design: docs/grimoire/design/component-backfill-design.md +
docs/grimoire/design/component-catalog-architecture-design.md (the registry
this backfills into) + docs/release-planning/release-planning-v3.97.md
ITEM-9/#460.

Standard: Python 3 stdlib-only (docs/grimoire/design/scripting-unification-design.md §3).

CLI:
  component_backfill.py report [--root DIR] [--stdout]
  component_backfill.py apply  [--root DIR] [--stdout]
  component_backfill.py --self-test
Exit 0 on success; 2 on a build error surfaced by the underlying registry
build (unreadable source, malformed metadata — see component_registry.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# Reuse component_registry's Discovery + RegistryEngine + resolve_scan_paths —
# the two skills are siblings under .claude/skills/; add grm-component-registry
# to the path so the import resolves whether this script is run from the repo
# root or elsewhere (matches the grm-cost-budget / grm-token-measure precedent).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REGISTRY_DIR = os.path.join(_THIS_DIR, os.pardir, "grm-component-registry")
if _REGISTRY_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_REGISTRY_DIR))

import component_registry  # noqa: E402  (path adjusted above)

# ── Constants (no magic numbers / strings inline) ───────────────────────────
CHARS_PER_TOKEN = 4  # rough token-estimate proxy (matches grm-token-measure/footprint.py).
READ_OVERHEAD_CHARS = 400   # rough chars an agent skims around a candidate beyond the summary source itself.
WRITE_OVERHEAD_CHARS = 250  # rough chars to author + write one component.json.
MAX_FILES_FOR_CONFIDENCE = 6  # more top-level files than this -> no single clear purpose.
STABILITY_BACKFILLED = "experimental"  # backfilled metadata is unverified until a human reviews it.
GENERIC_DIR_NAMES = frozenset({
    "misc", "utils", "util", "helpers", "helper", "common", "shared",
    "tmp", "temp", "scratch", "lib", "vendor", "legacy", "old", "backup",
})
CODE_EXTS = (".py", ".js", ".ts", ".go", ".rs", ".rb", ".java")
FRONT_DOOR_NAMES = ("__init__.py", "index.js", "index.ts", "main.py", "mod.rs")
README_NAME = "README.md"
COMPONENT_JSON = "component.json"

_PY_DOCSTRING_RE = re.compile(
    r'^\s*(?:#[^\n]*\n\s*)*(?P<q>"""|\'\'\')(?P<body>.*?)(?P=q)', re.DOTALL)
_JS_DOCSTRING_RE = re.compile(r'^\s*/\*\*(?P<body>.*?)\*/', re.DOTALL)
_SLUG_RE = re.compile(r'[^a-z0-9]+')
_TEST_NAME_RE = re.compile(r'^test_|_test$', re.IGNORECASE)


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub('-', name.lower()).strip('-')
    return slug or "component"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _is_test_file(name: str) -> bool:
    stem, _ext = os.path.splitext(name)
    lower_full = name.lower()
    return bool(_TEST_NAME_RE.search(stem)) or ".test." in lower_full or ".spec." in lower_full


def _top_level_files(abs_path: str) -> list:
    try:
        entries = os.listdir(abs_path)
    except OSError:
        return []
    files = []
    for entry in sorted(entries):
        full = os.path.join(abs_path, entry)
        if not os.path.isfile(full):
            continue
        if entry.startswith("."):
            continue
        if entry == COMPONENT_JSON:
            continue  # Discovery already excludes dirs that have one; belt-and-braces.
        if _is_test_file(entry):
            continue
        files.append(entry)
    return files


def _primary_source_file(files: list) -> str | None:
    code_files = [f for f in files if os.path.splitext(f)[1] in CODE_EXTS]
    if not code_files:
        return None
    for front in FRONT_DOOR_NAMES:
        if front in code_files:
            return front
    if len(code_files) == 1:
        return code_files[0]
    return None  # >=2 candidate entry points, no front-door file -> ambiguous.


def _first_meaningful_line_markdown(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue  # blank lines and the H1/H2 title don't count as a summary.
        return stripped
    return None


def _leading_docstring(text: str, filename: str) -> str | None:
    ext = os.path.splitext(filename)[1]
    if ext == ".py":
        m = _PY_DOCSTRING_RE.match(text)
    elif ext in (".js", ".ts"):
        m = _JS_DOCSTRING_RE.match(text)
    else:
        m = None
    if not m:
        return None
    for line in m.group("body").splitlines():
        line = line.strip().lstrip("*").strip()
        if line:
            return line
    return None


# ── Candidate classification ────────────────────────────────────────────────
class Candidate:
    """One uncataloged directory's classification + (if confident) summary."""

    def __init__(self, source: str, abs_path: str):
        self.source = source  # e.g. "components/foo/" (Discovery-relative, trailing slash).
        self.abs_path = abs_path
        self.id = _slugify(os.path.basename(os.path.normpath(abs_path)))
        self.status = None       # "confident" | "uncataloged"
        self.reason = None       # set when status == "uncataloged"
        self.summary = None      # set when status == "confident"
        self.summary_source_chars = 0  # chars read to source the summary (token estimate input).

    def as_dict(self) -> dict:
        out = {"source": self.source, "id": self.id, "status": self.status}
        if self.reason:
            out["reason"] = self.reason
        if self.summary:
            out["summary"] = self.summary
        return out


class Classifier:
    """Deterministic CONFIDENT / UNCATALOGED decision for one candidate dir."""

    def classify(self, source: str, abs_path: str) -> Candidate:
        cand = Candidate(source, abs_path)
        basename = os.path.basename(os.path.normpath(abs_path))
        if basename.lower() in GENERIC_DIR_NAMES:
            cand.status = "uncataloged"
            cand.reason = (
                f"grab-bag/generic directory name '{basename}' — not "
                "evidently a single cohesive component, needs manual review")
            return cand

        files = _top_level_files(abs_path)
        if len(files) > MAX_FILES_FOR_CONFIDENCE:
            cand.status = "uncataloged"
            cand.reason = (
                f"{len(files)} top-level files, no single clear entry point "
                "— ambiguous purpose, needs manual review")
            return cand

        summary, chars = self._find_summary(abs_path, files)
        if not summary:
            cand.status = "uncataloged"
            cand.reason = ("no README.md or leading module docstring found "
                            "to source a summary from")
            return cand

        cand.status = "confident"
        cand.summary = summary
        cand.summary_source_chars = chars
        return cand

    def _find_summary(self, abs_path: str, files: list):
        if README_NAME in files:
            text = _read(os.path.join(abs_path, README_NAME))
            line = _first_meaningful_line_markdown(text)
            if line:
                return line, len(text)
        primary = _primary_source_file(files)
        if primary:
            text = _read(os.path.join(abs_path, primary))
            doc = _leading_docstring(text, primary)
            if doc:
                return doc, len(text)
        return None, 0


def estimate_tokens(candidates: list) -> int:
    """chars-of-source-read-plus-authoring-overhead // 4 for CONFIDENT candidates only."""
    total_chars = sum(
        c.summary_source_chars + READ_OVERHEAD_CHARS + WRITE_OVERHEAD_CHARS
        for c in candidates if c.status == "confident")
    return total_chars // CHARS_PER_TOKEN


def _write_component_json(path: str, meta: dict) -> None:
    payload = json.dumps(meta, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, path)


# ── Engine ───────────────────────────────────────────────────────────────────
class BackfillEngine:
    """Discover uncataloged candidates, classify, report/apply."""

    def __init__(self, root: str = "."):
        self.root = root
        self.classifier = Classifier()

    def _candidates(self) -> list:
        scan_paths = component_registry.resolve_scan_paths(self.root)
        discovery = component_registry.Discovery(self.root, scan_paths)
        _components, uncataloged = discovery.discover()
        candidates = []
        for source in uncataloged:
            abs_path = os.path.join(self.root, source)
            candidates.append(self.classifier.classify(source, abs_path))
        return candidates

    def report(self) -> dict:
        candidates = self._candidates()
        confident = [c for c in candidates if c.status == "confident"]
        low_confidence = [c for c in candidates if c.status == "uncataloged"]
        return {
            "candidates": [c.as_dict() for c in candidates],
            "confident_count": len(confident),
            "low_confidence_count": len(low_confidence),
            "token_estimate": estimate_tokens(candidates),
        }

    def apply(self) -> dict:
        candidates = self._candidates()
        written = []
        for cand in candidates:
            if cand.status != "confident":
                continue
            meta = {
                "id": cand.id,
                "summary": cand.summary,
                "stability": STABILITY_BACKFILLED,
                "source": cand.source,
            }
            _write_component_json(
                os.path.join(cand.abs_path, COMPONENT_JSON), meta)
            written.append(cand.id)

        low_confidence = [c.as_dict() for c in candidates if c.status == "uncataloged"]
        try:
            result = component_registry.RegistryEngine(self.root).build(write=True)
        except component_registry.RegistryError as exc:
            return {"written": written, "low_confidence": low_confidence,
                     "error": str(exc)}
        return {
            "written": written,
            "low_confidence": low_confidence,
            "registry_diff": result["diff"],
            "uncataloged": result["registry"]["uncataloged"],
            "unknown_tags": result["registry"]["unknown-tags"],
        }


# ── Self-test (fixtures in a temp tree; never the repo's real sources) ──────
def _self_test() -> int:
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as root:
        # A minimal taxonomy fixture at whichever TAXONOMY_PATH the imported
        # component_registry module resolves (root's and claude-code's copies
        # have drifted on this path — see release-planning-v3.97.md ITEM-5
        # follow-ups). Not exercising taxonomy validation here (none of this
        # engine's written component.json entries populate profiles/provides/
        # requires), just keeping the registry build itself from failing on a
        # missing taxonomy doc regardless of which copy is on sys.path.
        taxonomy_path = os.path.join(root, component_registry.TAXONOMY_PATH)
        os.makedirs(os.path.dirname(taxonomy_path), exist_ok=True)
        with open(taxonomy_path, "w", encoding="utf-8") as fh:
            fh.write("# Component taxonomy\n\n## 2. `profiles`\n\n"
                      "## 3. `provides` / `requires`\n\n## 4. Adding a term\n")

        components = os.path.join(root, "components")
        os.makedirs(components)

        # 1) CONFIDENT via README.md first prose line.
        readme_dir = os.path.join(components, "timer-widget")
        os.makedirs(readme_dir)
        with open(os.path.join(readme_dir, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# Timer widget\n\nA countdown timer UI component.\n")
        with open(os.path.join(readme_dir, "timer.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")

        # 2) CONFIDENT via a leading module docstring (no README at all).
        doc_dir = os.path.join(components, "rate-limiter")
        os.makedirs(doc_dir)
        with open(os.path.join(doc_dir, "rate_limiter.py"), "w", encoding="utf-8") as fh:
            fh.write('"""Token-bucket rate limiter for outbound API calls."""\n'
                      "def allow():\n    return True\n")

        # 3) UNCATALOGED — generic/grab-bag directory name (disqualified by name
        #    alone, even though it HAS a README, proving name wins over signal).
        grabbag_dir = os.path.join(components, "misc")
        os.makedirs(grabbag_dir)
        with open(os.path.join(grabbag_dir, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# misc\n\nAssorted odds and ends.\n")

        # 4) UNCATALOGED — no README, no docstring, ambiguous multi-file dir.
        ambiguous_dir = os.path.join(components, "data-pipeline")
        os.makedirs(ambiguous_dir)
        for fn in ("extract.py", "transform.py", "load.py"):
            with open(os.path.join(ambiguous_dir, fn), "w", encoding="utf-8") as fh:
                fh.write("pass\n")

        engine = BackfillEngine(root)

        # PATH 1 — report classifies correctly before any write.
        pre = engine.report()
        statuses = {c["id"]: c["status"] for c in pre["candidates"]}
        if statuses.get("timer-widget") != "confident":
            failures.append("timer-widget should be confident (README): %r" % statuses)
        if statuses.get("rate-limiter") != "confident":
            failures.append("rate-limiter should be confident (docstring): %r" % statuses)
        if statuses.get("misc") != "uncataloged":
            failures.append("misc should be uncataloged (grab-bag name): %r" % statuses)
        if statuses.get("data-pipeline") != "uncataloged":
            failures.append("data-pipeline should be uncataloged (ambiguous): %r" % statuses)
        if pre["token_estimate"] <= 0:
            failures.append("token estimate should be > 0 with confident candidates present")

        # PATH 3 — low-confidence candidates carry an explicit, distinct reason.
        reasons = {c["id"]: c.get("reason") for c in pre["candidates"]
                   if c["status"] == "uncataloged"}
        if not reasons.get("misc") or "grab-bag" not in reasons["misc"]:
            failures.append("misc reason should cite the grab-bag name: %r" % reasons.get("misc"))
        if not reasons.get("data-pipeline") or "no README" not in reasons["data-pipeline"]:
            failures.append("data-pipeline reason should cite missing description "
                             "source: %r" % reasons.get("data-pipeline"))
        # never invents a summary for an uncataloged candidate.
        if any(c.get("summary") for c in pre["candidates"] if c["status"] == "uncataloged"):
            failures.append("an uncataloged candidate carries an invented summary")

        # PATH 1 (cont'd) — apply writes component.json for confident candidates
        # only, and the registry build reports them as 'added'.
        first = engine.apply()
        if sorted(first["written"]) != ["rate-limiter", "timer-widget"]:
            failures.append("apply should write exactly the two confident "
                             "candidates: %r" % first["written"])
        for slug, dirpath in (("timer-widget", readme_dir), ("rate-limiter", doc_dir)):
            cj = os.path.join(dirpath, COMPONENT_JSON)
            if not os.path.isfile(cj):
                failures.append("%s: component.json not written" % slug)
                continue
            meta = json.loads(_read(cj))
            if meta.get("id") != slug or not meta.get("summary"):
                failures.append("%s: component.json missing id/summary: %r" % (slug, meta))
            if meta.get("stability") != STABILITY_BACKFILLED:
                failures.append("%s: stability should be %r: %r" %
                                 (slug, STABILITY_BACKFILLED, meta))
        for name in ("misc", "data-pipeline"):
            cj = os.path.join(components, name, COMPONENT_JSON)
            if os.path.isfile(cj):
                failures.append("%s: component.json should NOT have been written" % name)
        added = first.get("registry_diff", {}).get("added", [])
        if sorted(added) != ["rate-limiter", "timer-widget"]:
            failures.append("registry diff 'added' should list both new ids: %r" % added)
        if sorted(first["uncataloged"]) != ["components/data-pipeline/", "components/misc/"]:
            failures.append("registry uncataloged should retain the two low-confidence "
                             "dirs: %r" % first["uncataloged"])

        # PATH 2 — idempotent: a second apply is a no-op (nothing new written,
        # nothing added/changed in the registry diff).
        second = engine.apply()
        if second["written"]:
            failures.append("second apply should write nothing: %r" % second["written"])
        diff2 = second.get("registry_diff", {})
        if diff2.get("added") or diff2.get("changed"):
            failures.append("second apply's registry diff should be empty "
                             "added/changed: %r" % diff2)
        if sorted(second["uncataloged"]) != ["components/data-pipeline/", "components/misc/"]:
            failures.append("second apply uncataloged should be unchanged: %r" %
                             second["uncataloged"])

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("component_backfill self-test: OK (confident-via-README, "
          "confident-via-docstring, grab-bag-name disqualification, "
          "ambiguous-multi-file disqualification, reasons surfaced never "
          "invented, apply writes only confident candidates + registry "
          "'added', idempotent no-op second run)")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="One-time component-metadata backfill engine (#460).")
    ap.add_argument("verb", nargs="?", help="report|apply")
    ap.add_argument("--root", default=".")
    ap.add_argument("--stdout", action="store_true",
                    help="print the full result JSON (report already does; "
                         "for apply this includes the registry diff)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.verb:
        ap.error("a verb is required (report|apply) or --self-test")
    if args.verb not in ("report", "apply"):
        ap.error("unknown verb: %s" % args.verb)

    engine = BackfillEngine(args.root)
    if args.verb == "report":
        result = engine.report()
    else:
        result = engine.apply()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 2 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
