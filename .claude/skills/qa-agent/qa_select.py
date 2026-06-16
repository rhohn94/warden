#!/usr/bin/env python3
"""qa_select.py — deterministic release-window + acceptance-criteria selector for the QA agent (#70, v3.6).

The QA agent (#70) retrospectively verifies *shipped* features against their
original acceptance criteria and files shortcomings to the tracker. This helper
does the deterministic, zero-LLM-cost part: it reads the structured layer —
`docs/version-history.md` (the release list), `docs/qa-ledger.md` (which
releases are already QA-verified), and each `docs/release-planning-vX.Y.md`
(the per-feature Status Ledger + acceptance sources) — and emits a single JSON
work-list naming the target release(s) and the per-feature items to check. The
agent then does the judgement (read code/tests, decide pass/fail) and the filing.

Design authority: docs/design/qa-agent-design.md (+ scripting-unification
guidelines, docs/design/scripting-unification-design.md §3).

Window selection (config `qa.window-mode` / `qa.window-size`, overridable by flag):
  earliest-unverified (default) -> the single oldest release not marked verified
  all-unverified                -> every release not marked verified, oldest-first
  last-n (--window N)           -> the most recent N releases, regardless of status
  --release vX.Y                -> exactly that release

State: a release is "verified" iff `docs/qa-ledger.md` has a row for it whose
status is `verified` or `verified-with-findings`. The QA agent has no git
writes; the dispatching integration master records the verdict row after the
agent reports (so the ledger stays the single deterministic source of state).

Usage:
  qa_select.py [--root DIR] [--window N | --release vX.Y | --all] [--self-test]
Outputs JSON to stdout. Exit 0 on success, 2 on bad input.
"""
import argparse
import json
import os
import re
import sys


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def parse_releases(version_history_text):
    """Extract '## vX.Y — Title' sections, newest-first as written."""
    if not version_history_text:
        return []
    rels = []
    for m in re.finditer(r"^##\s+(v\d+\.\d+)\s*(?:[—\-–]\s*(.*))?$",
                         version_history_text, re.MULTILINE):
        rels.append({"version": m.group(1), "title": (m.group(2) or "").strip()})
    return rels


def parse_ledger(ledger_text):
    """Map version -> status from the qa-ledger table rows.

    Row shape: | vX.Y | <status> | <date> | <findings> |
    status is normalized lowercase; unknown/missing -> 'unverified'.
    """
    status = {}
    if not ledger_text:
        return status
    for m in re.finditer(r"^\|\s*(v\d+\.\d+)\s*\|\s*([^|]*?)\s*\|",
                         ledger_text, re.MULTILINE):
        ver = m.group(1)
        st = m.group(2).strip().lower()
        status[ver] = st or "unverified"
    return status


# Statuses that keep a release "open" (a valid QA target). Anything else in the
# ledger (verified / verified-with-findings / out-of-scope / skipped / deferred)
# excludes it from auto-selection.
OPEN_STATUSES = ("unverified", "in-progress", "pending", "")


def is_verified(status):
    return status in ("verified", "verified-with-findings")


def is_open(status):
    return status in OPEN_STATUSES


def parse_status_ledger(planning_text):
    """Extract feature rows from a release-planning doc's '## 5. Status Ledger'
    markdown table. Returns a list of {item, design, implemented, reviewed,
    merged}. The first column (Item) is the feature description the QA agent
    verifies against its acceptance criteria."""
    if not planning_text:
        return []
    # Isolate the Status Ledger section (from its heading to the next '## ' or EOF).
    sec = re.search(r"(?ms)^#{1,6}\s*\d*\.?\s*Status Ledger.*?(?=^\#{1,2}\s|\Z)",
                    planning_text)
    block = sec.group(0) if sec else planning_text
    items = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        first = cells[0]
        # skip header + separator rows
        if first.lower() in ("item", "") or set(first) <= set("-: "):
            continue
        # a status-ledger data row has tick/cross marks in later columns
        item = {"item": first}
        labels = ["design", "implemented", "reviewed", "merged"]
        for i, lab in enumerate(labels, start=1):
            if i < len(cells):
                item[lab] = cells[i]
        items.append(item)
    return items


def acceptance_sources(planning_text, root):
    """Collect referenced design docs (acceptance-criteria sources) from a
    release-planning doc — any docs/design/*.md path it mentions."""
    out = []
    if planning_text:
        for m in re.finditer(r"docs/design/([A-Za-z0-9_\-]+\.md)", planning_text):
            rel = "docs/design/" + m.group(1)
            if rel not in out:
                out.append(rel)
    return out


def select_window(releases, ledger_status, ledger_present, mode, window_size, release):
    """Return the ordered list of target releases (oldest-first) per the mode.

    Scope rule: when a ledger exists, only releases that have a ledger row with an
    *open* status are auto-targets — releases with no row are out-of-scope by
    omission (the QA program is opt-in per release). When no ledger exists at all,
    fall back to treating every release as an open target (degraded)."""
    oldest_first = list(reversed(releases))  # version-history is newest-first
    if release:
        return [r for r in oldest_first if r["version"] == release]
    if mode == "last-n":
        n = max(1, int(window_size))
        return list(reversed(releases[:n]))  # most-recent N, oldest-first
    if ledger_present and ledger_status:
        candidates = [r for r in oldest_first
                      if r["version"] in ledger_status
                      and is_open(ledger_status[r["version"]])]
    else:
        candidates = oldest_first  # no ledger: everything is open (degraded)
    if mode == "all-unverified":
        return candidates
    # earliest-unverified (default)
    return candidates[:1]


def build_worklist(root, mode="earliest-unverified", window_size=1, release=None):
    sources_read, degraded = [], []

    vh = _read(os.path.join(root, "docs", "version-history.md"))
    releases = parse_releases(vh)
    if vh is not None:
        sources_read.append("docs/version-history.md")
    else:
        degraded.append("docs/version-history.md (missing)")

    ledger_text = _read(os.path.join(root, "docs", "qa-ledger.md"))
    ledger_status = parse_ledger(ledger_text)
    ledger_present = ledger_text is not None
    if ledger_present:
        sources_read.append("docs/qa-ledger.md")
    else:
        degraded.append("docs/qa-ledger.md (missing — all releases treated as open targets)")

    targets = select_window(releases, ledger_status, ledger_present,
                            mode, window_size, release)

    selected = []
    for r in targets:
        ver = r["version"]
        planning_rel = os.path.join("docs", "release-planning-%s.md" % ver)
        planning_text = _read(os.path.join(root, planning_rel))
        if planning_text is not None:
            sources_read.append(planning_rel)
        else:
            degraded.append("%s (missing — no acceptance source for %s)" % (planning_rel, ver))
        selected.append({
            "version": ver,
            "title": r["title"],
            "ledger_status": ledger_status.get(ver, "unverified"),
            "planning_doc": planning_rel if planning_text is not None else None,
            "items": parse_status_ledger(planning_text),
            "acceptance_sources": acceptance_sources(planning_text, root),
        })

    return {
        "mode": mode,
        "window_size": window_size,
        "requested_release": release,
        "selected": selected,
        "ledger_status": ledger_status,
        "all_releases": [r["version"] for r in releases],
        "sources_read": sources_read,
        "degraded": degraded,
        "note": "Structured layer only. The QA agent verifies each item against "
                "its acceptance criteria (reading code/tests), files shortcomings "
                "via feedback-to-issue, and returns a verdict; the integration "
                "master records the result row in docs/qa-ledger.md.",
    }


def _self_test():
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "docs"))
        with open(os.path.join(d, "docs", "version-history.md"), "w") as fh:
            fh.write("# Version History\n\n"
                     "## v3.3 — Scripting unification\n\nbody\n\n"
                     "## v3.2 — Sync reliability\n\nbody\n\n"
                     "## v3.1 — Project Manager\n\nbody\n")
        with open(os.path.join(d, "docs", "qa-ledger.md"), "w") as fh:
            fh.write("# QA verification ledger\n\n"
                     "| Release | Status | Date | Findings |\n"
                     "|---|---|---|---|\n"
                     "| v3.1 | verified | 2026-06-04 | none |\n"
                     "| v3.2 | verified-with-findings | 2026-06-04 | #99 |\n"
                     "| v3.3 | unverified | — | — |\n")
        with open(os.path.join(d, "docs", "release-planning-v3.3.md"), "w") as fh:
            fh.write("# Release Planning — v3.3\n\n"
                     "Companion: docs/design/status-broker-design.md and "
                     "docs/design/scripting-unification-design.md.\n\n"
                     "## 5. Status Ledger\n\n"
                     "| Item | Design | Implemented | Reviewed | Merged |\n"
                     "|---|---|---|---|---|\n"
                     "| status-broker role | ☑ | ☑ | ☑ | ☑ |\n"
                     "| project_status.py helper | ☑ | ☑ | ☑ | ☑ |\n")

        # default: earliest unverified -> v3.3
        w = build_worklist(d)
        if [s["version"] for s in w["selected"]] != ["v3.3"]:
            failures.append("earliest-unverified should select v3.3: %r" %
                            [s["version"] for s in w["selected"]])
        if w["ledger_status"].get("v3.1") != "verified":
            failures.append("ledger parse missed v3.1 verified")
        sel = w["selected"][0] if w["selected"] else {}
        if len(sel.get("items", [])) != 2:
            failures.append("status-ledger item parse wrong: %r" % sel.get("items"))
        if "docs/design/status-broker-design.md" not in sel.get("acceptance_sources", []):
            failures.append("acceptance source not extracted: %r" % sel.get("acceptance_sources"))

        # all-unverified -> only v3.3 here
        wa = build_worklist(d, mode="all-unverified")
        if [s["version"] for s in wa["selected"]] != ["v3.3"]:
            failures.append("all-unverified wrong: %r" % [s["version"] for s in wa["selected"]])

        # last-n=2 -> most recent two oldest-first: v3.2, v3.3
        wl = build_worklist(d, mode="last-n", window_size=2)
        if [s["version"] for s in wl["selected"]] != ["v3.2", "v3.3"]:
            failures.append("last-n=2 wrong: %r" % [s["version"] for s in wl["selected"]])

        # explicit release
        wr = build_worklist(d, release="v3.1")
        if [s["version"] for s in wr["selected"]] != ["v3.1"]:
            failures.append("explicit release wrong: %r" % [s["version"] for s in wr["selected"]])

        # determinism
        if json.dumps(build_worklist(d), sort_keys=True) != json.dumps(w, sort_keys=True):
            failures.append("non-deterministic output")

    # all-verified -> empty selection (nothing left to QA)
    with tempfile.TemporaryDirectory() as d2:
        os.makedirs(os.path.join(d2, "docs"))
        with open(os.path.join(d2, "docs", "version-history.md"), "w") as fh:
            fh.write("## v3.1 — One\n\nbody\n")
        with open(os.path.join(d2, "docs", "qa-ledger.md"), "w") as fh:
            fh.write("| Release | Status | Date | Findings |\n|---|---|---|---|\n"
                     "| v3.1 | verified | 2026-06-04 | none |\n")
        we = build_worklist(d2)
        if we["selected"]:
            failures.append("all-verified should select nothing: %r" %
                            [s["version"] for s in we["selected"]])

    # missing-ledger degrade -> earliest release treated unverified
    with tempfile.TemporaryDirectory() as d3:
        os.makedirs(os.path.join(d3, "docs"))
        with open(os.path.join(d3, "docs", "version-history.md"), "w") as fh:
            fh.write("## v2.0 — Two\n\nb\n\n## v1.0 — One\n\nb\n")
        wd = build_worklist(d3)
        if [s["version"] for s in wd["selected"]] != ["v1.0"]:
            failures.append("missing-ledger earliest wrong: %r" %
                            [s["version"] for s in wd["selected"]])
        if not wd["degraded"]:
            failures.append("missing ledger not flagged degraded")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("qa_select self-test: OK (release parse, ledger parse, earliest/"
          "all/last-n/explicit windows, status-ledger items, acceptance sources, "
          "all-verified-empty, missing-ledger degrade, determinism)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Deterministic QA release-window + acceptance-criteria selector.")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    ap.add_argument("--window", type=int, metavar="N",
                    help="last N releases (most recent), regardless of QA status")
    ap.add_argument("--all", action="store_true",
                    help="every release not yet QA-verified (oldest-first)")
    ap.add_argument("--release", metavar="vX.Y", help="select exactly this release")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not os.path.isdir(args.root):
        print("error: --root is not a directory: %s" % args.root, file=sys.stderr)
        return 2
    mode, size = "earliest-unverified", 1
    if args.window is not None:
        mode, size = "last-n", args.window
    elif args.all:
        mode = "all-unverified"
    print(json.dumps(build_worklist(args.root, mode=mode, window_size=size,
                                    release=args.release), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
