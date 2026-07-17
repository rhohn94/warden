#!/usr/bin/env python3
"""capability_overlap.py — grm-fleet-audit's capability-overlap checklist item (#412, v3.97).

`grm-fleet-audit`'s Step 3 (Duplicate-implementation detection) was entirely
prose/agent-driven until this script: a human or agent had to notice, by
reading, that the same capability had been hand-rolled in two or more fleet
repos. This engine mechanizes ONE narrow slice of that reasoning: applying a
maintained heuristic grep-set (`capability-overlap-patterns.json`, one entry
per `docs/grimoire/design/component-taxonomy.md` §3 capability) across a set
of local repo paths, and flagging any capability hand-rolled in two or more of
them as a rule-of-two violation (the policy ITEM-2/#411 lands in the same
release) — pre-filling an extraction-ticket draft ready to hand to
`grm-issue-tracker`.

This is a HEURISTIC, not a certainty signal: a grep hit means "worth a human
look", never "auto-file without review". The grep-set itself is DATA, not
logic — see `capability-overlap-patterns.json`'s own `_comment` for the format
and how to extend it with more capabilities or patterns.

Engagement-scope note (v3.97, #412): this repo does not operate against a
live multi-repo fleet in this engagement. This script's mechanism is proven
via `--self-test` against synthetic fixture directories standing in for
"sibling repos" — a live run against the real fleet (`scan --repo <path> ...`
against real sibling checkouts) is a follow-up action, explicitly out of
scope this release (see release-planning-v3.97.md §4).

Standard: Python 3 stdlib-only (docs/grimoire/design/scripting-unification-design.md §3).

CLI:
  capability_overlap.py scan --repo DIR [--repo DIR ...] [--patterns FILE] [--json]
  capability_overlap.py --self-test

Exit 0 on a successful scan (findings, if any, are reported — not a script
failure); 2 on a usage/read error (missing patterns file, malformed JSON, no
repos given, an unreadable repo path).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ── Constants (no magic numbers / strings inline) ───────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PATTERNS_PATH = SCRIPT_DIR / "capability-overlap-patterns.json"
# This script always ships inside the grimoire-framework tree (root or the
# claude-code/copilot flavor mirrors) at .claude/skills/grm-fleet-audit/ — the
# taxonomy authority lives three levels up from here. Overridable via --taxonomy
# for a consumer project laid out differently, or for a self-test fixture.
DEFAULT_TAXONOMY_PATH = SCRIPT_DIR.parents[2] / "docs" / "grimoire" / "design" / "component-taxonomy.md"
RULE_OF_TWO_THRESHOLD = 2
MAX_EVIDENCE_PER_REPO_CAPABILITY = 3
MAX_FILE_BYTES = 2 * 1024 * 1024  # skip anything bigger than 2MB — not source code
# Directories never worth scanning: VCS metadata, dependency/build output,
# already-vendored third-party code (scanning it would flag the VENDOR, not a
# hand-rolled duplicate), and common language build caches. Matched by
# basename anywhere in the tree (os.walk prunes on every level, so
# "third-party" also catches lib/third-party/ without a full-path match).
EXCLUDED_DIR_BASENAMES = frozenset({
    ".git", "node_modules", "vendor", "dist", "build", "target",
    "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    "third-party",
})
# Taxonomy §3 table row: `| \`term\` | meaning |` — same convention
# grm-component-registry's component_registry.py already uses to read the
# vocabulary live rather than hand-copying it into this script.
TAXONOMY_TERM_RE = re.compile(r"^\s*\|\s*`([a-z][a-z0-9-]*)`\s*\|")
CAPABILITY_SECTION_RE = re.compile(r"^##\s+3\.\s", re.MULTILINE)
NEXT_SECTION_RE = re.compile(r"^##\s+\d+\.\s", re.MULTILINE)


class CapabilityOverlapError(Exception):
    """Raised for a usage/read error: bad patterns file, no repos, etc."""


class CapabilityOverlapScanner:
    """Applies a capability -> grep-pattern-set mapping across a list of local
    repo paths and reports, per capability, which repos hand-rolled it.

    Construction reads and compiles the pattern data once; `scan()` may be
    called repeatedly (e.g. against different repo sets) without re-parsing.
    """

    def __init__(self, patterns: dict[str, list[str]]):
        if not patterns:
            raise CapabilityOverlapError("patterns data is empty — nothing to scan for")
        self._compiled: dict[str, list[re.Pattern]] = {}
        for capability, pattern_strs in patterns.items():
            if not pattern_strs:
                raise CapabilityOverlapError(
                    f"capability {capability!r} has an empty patterns list")
            try:
                self._compiled[capability] = [re.compile(p) for p in pattern_strs]
            except re.error as exc:
                raise CapabilityOverlapError(
                    f"capability {capability!r} has an invalid regex: {exc}") from exc

    @property
    def capabilities(self) -> list[str]:
        return sorted(self._compiled)

    def scan(self, repo_paths: list[str]) -> dict:
        """Return {capability: {repo: [evidence, ...]}} for every capability
        that has at least one hand-rolled hit in at least one repo. A repo
        with zero hits for a capability is simply absent from that
        capability's inner dict (not present-with-empty-list)."""
        if not repo_paths:
            raise CapabilityOverlapError("no repo paths given to scan")
        report: dict[str, dict[str, list[dict]]] = {c: {} for c in self._compiled}
        for repo in repo_paths:
            if not os.path.isdir(repo):
                raise CapabilityOverlapError(f"repo path is not a directory: {repo}")
            repo_name = os.path.basename(os.path.normpath(repo))
            hits = self._scan_one_repo(repo)
            for capability, evidence in hits.items():
                if evidence:
                    report[capability][repo_name] = evidence
        # Drop capabilities with zero hits anywhere — nothing to report.
        return {c: repos for c, repos in report.items() if repos}

    def _scan_one_repo(self, repo: str) -> dict[str, list[dict]]:
        hits: dict[str, list[dict]] = {c: [] for c in self._compiled}
        for file_path in self._iter_source_files(repo):
            try:
                if os.path.getsize(file_path) > MAX_FILE_BYTES:
                    continue
                with open(file_path, encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            rel = os.path.relpath(file_path, repo)
            for capability, compiled_patterns in self._compiled.items():
                # Repo-level dedup: once this capability already has enough
                # evidence recorded for this repo, stop looking (cheaper, and
                # keeps the eventual ticket body bounded).
                if len(hits[capability]) >= MAX_EVIDENCE_PER_REPO_CAPABILITY:
                    continue
                for lineno, line in enumerate(lines, start=1):
                    for pat in compiled_patterns:
                        if pat.search(line):
                            hits[capability].append({
                                "file": rel, "line": lineno,
                                "pattern": pat.pattern, "text": line.strip(),
                            })
                            break
                    if len(hits[capability]) >= MAX_EVIDENCE_PER_REPO_CAPABILITY:
                        break
        return hits

    @staticmethod
    def _iter_source_files(repo: str):
        for dirpath, dirnames, filenames in os.walk(repo):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_BASENAMES
                           and not d.startswith(".")]
            for name in filenames:
                yield os.path.join(dirpath, name)


def load_patterns(path: str | Path) -> dict[str, list[str]]:
    """Read capability-overlap-patterns.json -> {capability: [pattern, ...]}."""
    p = Path(path)
    if not p.exists():
        raise CapabilityOverlapError(f"patterns file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CapabilityOverlapError(f"patterns file is not valid JSON: {exc}") from exc
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, dict):
        raise CapabilityOverlapError(
            "patterns file missing a 'capabilities' object at the top level")
    return {name: entry.get("patterns", []) for name, entry in capabilities.items()}


def load_taxonomy_capabilities(path: str | Path) -> set[str] | None:
    """Read the authoritative capability vocabulary from component-taxonomy.md
    §3 (docs/grimoire/design/component-taxonomy.md). Returns None (validation
    skipped, not failed) when the doc isn't found — this script must remain
    usable against a repo set that doesn't carry this framework's own docs."""
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    start = CAPABILITY_SECTION_RE.search(text)
    if not start:
        return None
    rest = text[start.end():]
    end = NEXT_SECTION_RE.search(rest)
    section = rest[:end.start()] if end else rest
    terms = set()
    for line in section.splitlines():
        m = TAXONOMY_TERM_RE.match(line)
        if m:
            terms.add(m.group(1))
    return terms


def unknown_capabilities(patterns: dict[str, list[str]], taxonomy: set[str] | None) -> list[str]:
    """Capability keys in the patterns data that the live taxonomy doesn't
    (or no longer) list — surfaced, never silently dropped from the scan."""
    if taxonomy is None:
        return []
    return sorted(set(patterns) - taxonomy)


def rule_of_two_violations(report: dict, threshold: int = RULE_OF_TWO_THRESHOLD) -> dict:
    """{capability: {repo: [evidence, ...]}} filtered to capabilities hand-rolled
    in >= threshold repos — the rule-of-two trigger (ITEM-2/#411)."""
    return {c: repos for c, repos in report.items() if len(repos) >= threshold}


def build_extraction_ticket(capability: str, repos: dict[str, list[dict]]) -> dict:
    """Pre-fill an extraction-ticket draft for a rule-of-two-violating
    capability, shaped to feed straight into
    `grm-issue-tracker/issue_tracker.py create --title ... --body ... --labels ...`
    (or the equivalent `mcp__grimoire-issue-tracker__create_issue` call)."""
    repo_names = sorted(repos)
    evidence_lines = []
    for repo_name in repo_names:
        for ev in repos[repo_name]:
            evidence_lines.append(
                f"- `{repo_name}`: `{ev['file']}:{ev['line']}` matches "
                f"`{ev['pattern']}` — `{ev['text']}`")
    title = f"Extraction candidate: {capability} hand-rolled in {len(repo_names)} repos"
    body = (
        f"**Rule-of-two violation** (`docs/coding-standards.md`, ITEM-2/#411): "
        f"the `{capability}` capability "
        f"(`docs/grimoire/design/component-taxonomy.md` §3) has been hand-rolled "
        f"independently in {len(repo_names)} repos within this audit's scope — "
        f"filed automatically by `grm-fleet-audit`'s capability-overlap checklist "
        f"item (#412).\n\n"
        f"**Repos:** {', '.join(repo_names)}\n\n"
        f"**Evidence** (heuristic grep hits — verify before treating as final):\n"
        + "\n".join(evidence_lines) + "\n\n"
        f"**Next step:** design a shared component for `{capability}` "
        f"(see `docs/grimoire/design/component-taxonomy.md`, and the "
        f"standard-package precedent — token-bookkeeper / gatekeeper / "
        f"recordkeeper / meta-updater) that every listed repo can consume via "
        f"`vendor.toml` + `grm-sync-deps`, then retire the hand-rolled copies."
    )
    return {
        "title": title,
        "body": body,
        "labels": ["audit", "extraction-candidate", f"capability-{capability}"],
        "capability": capability,
        "repos": repo_names,
        "evidence": [ev for r in repo_names for ev in repos[r]],
    }


def run_scan(repo_paths: list[str], patterns_path: str | Path,
             taxonomy_path: str | Path | None) -> dict:
    """End-to-end: load patterns, scan repos, flag rule-of-two violations,
    pre-fill extraction tickets. Returns the full structured result."""
    patterns = load_patterns(patterns_path)
    scanner = CapabilityOverlapScanner(patterns)
    report = scanner.scan(repo_paths)
    violations = rule_of_two_violations(report)
    tickets = [build_extraction_ticket(c, repos) for c, repos in sorted(violations.items())]
    unknown = unknown_capabilities(patterns, load_taxonomy_capabilities(taxonomy_path)
                                    if taxonomy_path else load_taxonomy_capabilities(DEFAULT_TAXONOMY_PATH))
    return {
        "repos-scanned": [os.path.basename(os.path.normpath(r)) for r in repo_paths],
        "capabilities-checked": scanner.capabilities,
        "unknown-capabilities": unknown,
        "hits": report,
        "rule-of-two-violations": sorted(violations),
        "extraction-tickets": tickets,
    }


# ── Self-test ────────────────────────────────────────────────────────────────
def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture_patterns() -> dict:
    return {
        "capabilities": {
            "auth": {"patterns": [r"bcrypt\.hashpw\(", r"class \w*Auth\w*Middleware"]},
            "persistence": {"patterns": [r"sqlite3\.connect\("]},
            "telemetry": {"patterns": [r"structlog\.get_logger\("]},
        }
    }


def _fixture_taxonomy() -> str:
    return (
        "# Component taxonomy — controlled vocabulary\n\n"
        "## 2. `profiles`\n\n| Term | Meaning |\n|---|---|\n| `cli` | A CLI. |\n\n"
        "## 3. `provides` / `requires` — capability vocabulary\n\n"
        "| Term | Meaning |\n|---|---|\n"
        "| `auth` | Authentication. |\n"
        "| `persistence` | Durable storage. |\n"
        "| `telemetry` | Observability. |\n\n"
        "## 4. Adding a term\n\nSome unrelated text.\n"
    )


def _self_test() -> int:
    import tempfile

    failures = []

    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        patterns_path = root / "patterns.json"
        _write(patterns_path, json.dumps(_fixture_patterns()))
        taxonomy_path = root / "component-taxonomy.md"
        _write(taxonomy_path, _fixture_taxonomy())

        # Three synthetic "sibling repos": repo-a and repo-b both hand-roll
        # auth from scratch (bcrypt + a custom middleware class); repo-c only
        # hand-rolls persistence (sqlite3.connect) — auth appears in only ONE
        # repo there, so it must NOT trigger rule-of-two on its own.
        repo_a = root / "repo-a"
        _write(repo_a / "auth.py",
               "import bcrypt\n\ndef login(pw):\n    return bcrypt.hashpw(pw, salt)\n")
        repo_b = root / "repo-b"
        _write(repo_b / "src" / "middleware.py",
               "class LegacyAuthMiddleware:\n    def handle(self, req):\n        pass\n")
        repo_c = root / "repo-c"
        _write(repo_c / "db.py",
               "import sqlite3\n\ncon = sqlite3.connect('app.db')\n")
        # Noise a scan must skip: a vendored/build dir inside repo-c that also
        # matches the auth pattern — must NOT count towards repo-c's hits.
        _write(repo_c / "node_modules" / "somelib" / "auth.js",
               "bcrypt.hashpw(pw, salt)\n")

        repos = [str(repo_a), str(repo_b), str(repo_c)]

        # 1) CORE DETECTION: auth hand-rolled in repo-a + repo-b (2 repos) ->
        #    rule-of-two violation; persistence hand-rolled in repo-c alone
        #    (1 repo) -> NOT a violation.
        result = run_scan(repos, patterns_path, taxonomy_path)
        if sorted(result["rule-of-two-violations"]) != ["auth"]:
            failures.append(
                f"expected only 'auth' flagged, got {result['rule-of-two-violations']!r}")
        if "persistence" in result["rule-of-two-violations"]:
            failures.append("persistence (1 repo) must not trigger rule-of-two")
        if set(result["hits"]["auth"]) != {"repo-a", "repo-b"}:
            failures.append(f"auth hits repo set wrong: {result['hits']['auth'].keys()!r}")

        # 2) EXCLUDED-DIR discipline: node_modules/ noise in repo-c must not
        #    make repo-c count towards auth's hand-rolled-repo set.
        if "repo-c" in result["hits"].get("auth", {}):
            failures.append("node_modules/ hit leaked into the scan (exclude-dir broken)")

        # 3) TELEMETRY: zero hits anywhere -> absent from the report entirely
        #    (not present with an empty dict).
        if "telemetry" in result["hits"]:
            failures.append("a capability with zero hits must be absent from 'hits'")

        # 4) EXTRACTION-TICKET PRE-FILL: exactly one ticket, for 'auth', naming
        #    both repos, carrying evidence lines and the expected labels.
        tickets = result["extraction-tickets"]
        if len(tickets) != 1:
            failures.append(f"expected exactly 1 extraction ticket, got {len(tickets)}")
        else:
            t = tickets[0]
            if t["capability"] != "auth":
                failures.append(f"ticket capability wrong: {t['capability']!r}")
            if t["repos"] != ["repo-a", "repo-b"]:
                failures.append(f"ticket repos wrong: {t['repos']!r}")
            if "repo-a" not in t["title"] and "2 repos" not in t["title"]:
                failures.append(f"ticket title doesn't name the scope: {t['title']!r}")
            if "repo-a" not in t["body"] or "repo-b" not in t["body"]:
                failures.append("ticket body missing a repo name")
            if "#411" not in t["body"]:
                failures.append("ticket body doesn't reference the rule-of-two policy (#411)")
            if "auth.py:" not in t["body"] and "middleware.py:" not in t["body"]:
                failures.append("ticket body missing cited evidence file:line")
            if sorted(t["labels"]) != sorted(
                    ["audit", "extraction-candidate", "capability-auth"]):
                failures.append(f"ticket labels wrong: {t['labels']!r}")

        # 5) TAXONOMY CROSS-CHECK: add an unknown capability key -> surfaced,
        #    never silently dropped, and never crashes the scan.
        bad_patterns_path = root / "patterns-with-unknown.json"
        bad = _fixture_patterns()
        bad["capabilities"]["frobnicate"] = {"patterns": [r"frob\("]}
        _write(bad_patterns_path, json.dumps(bad))
        result2 = run_scan(repos, bad_patterns_path, taxonomy_path)
        if result2["unknown-capabilities"] != ["frobnicate"]:
            failures.append(
                f"unknown capability not surfaced: {result2['unknown-capabilities']!r}")

        # 6) EMPTY REPO SET / MISSING PATTERNS FILE -> CapabilityOverlapError,
        #    not an unhandled exception.
        try:
            run_scan([], patterns_path, taxonomy_path)
            failures.append("empty repo list should raise CapabilityOverlapError")
        except CapabilityOverlapError:
            pass
        try:
            run_scan(repos, root / "does-not-exist.json", taxonomy_path)
            failures.append("missing patterns file should raise CapabilityOverlapError")
        except CapabilityOverlapError:
            pass

        # 7) MISSING TAXONOMY DOC: validation is skipped, not fatal.
        result3 = run_scan(repos, patterns_path, root / "no-such-taxonomy.md")
        if result3["unknown-capabilities"] != []:
            failures.append("missing taxonomy doc should skip validation, not fail it")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("capability_overlap self-test: OK (rule-of-two detection at >=2 repos, "
          "single-repo hand-rolling not flagged, excluded-dir noise skipped, "
          "zero-hit capability omitted, extraction-ticket pre-fill (title/body/"
          "labels/evidence), unknown-capability surfaced against the taxonomy, "
          "empty-repo-set and missing-patterns-file errors, missing-taxonomy "
          "graceful skip)")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="grm-fleet-audit capability-overlap checklist item (#412).")
    sub = ap.add_subparsers(dest="verb")

    p_scan = sub.add_parser("scan", help="scan a set of local repo paths")
    p_scan.add_argument("--repo", action="append", default=[],
                         help="local repo path to scan; repeatable")
    p_scan.add_argument("--patterns", default=str(DEFAULT_PATTERNS_PATH),
                         help="path to capability-overlap-patterns.json")
    p_scan.add_argument("--taxonomy", default=str(DEFAULT_TAXONOMY_PATH),
                         help="path to component-taxonomy.md (vocabulary cross-check)")
    p_scan.add_argument("--json", action="store_true",
                         help="print the full structured result as JSON")

    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if args.verb != "scan":
        ap.error("a verb is required (scan) or --self-test")

    try:
        result = run_scan(args.repo, args.patterns, args.taxonomy)
    except CapabilityOverlapError as exc:
        print(f"capability_overlap: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print(f"Repos scanned: {', '.join(result['repos-scanned'])}")
    if result["unknown-capabilities"]:
        print(f"Unknown capabilities (not in component-taxonomy.md §3): "
              f"{', '.join(result['unknown-capabilities'])}")
    if not result["hits"]:
        print("No hand-rolled hits for any cataloged capability.")
        return 0
    for capability in sorted(result["hits"]):
        repos = result["hits"][capability]
        flagged = " [RULE-OF-TWO VIOLATION]" if capability in result["rule-of-two-violations"] else ""
        print(f"- {capability}: hand-rolled in {len(repos)} repo(s) "
              f"({', '.join(sorted(repos))}){flagged}")
    for ticket in result["extraction-tickets"]:
        print(f"\nExtraction ticket draft — {ticket['title']}")
        print(f"  labels: {', '.join(ticket['labels'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
