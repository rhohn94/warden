#!/usr/bin/env python3
"""project_status.py — deterministic, script-first project overview.

The status-broker role (#73) answers "what is the status of X?" cheaply by
exhausting structured sources before touching code. This helper reads the
zero-LLM-cost structured layer — `grimoire-config.json`, `version-history.md`,
`roadmap.md`, the feature manifest, and package manifests — and emits a single
JSON overview (#74). The agent then layers the issue tracker (cheapest,
authoritative for tracked work) and, only as a last resort, source code.

Design authority: docs/design/status-broker-design.md (+ scripting-unification
guidelines, docs/design/scripting-unification-design.md §3).

Field source mapping (#74):
  zero-LLM (this script):
    name, framework-version, schema-version, paradigm, dials  <- grimoire-config.json
    latest/recent releases                                     <- version-history.md
    in-flight / next version                                   <- roadmap.md (section w/o "Shipped")
    feature-manifest-version                                   <- feature-manifest.md
    tech-stack manifests (name/version)                        <- package.json / Cargo.toml / pyproject.toml
  needs agent synthesis (NOT here):
    tech-stack interpretation, Aura version, per-feature narrative, "why".

Usage:
  project_status.py [--root DIR] [--self-test]
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


def _dial(cfg, key):
    """Return the .value of a config dial block, or the raw value, or None."""
    v = cfg.get(key)
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


def read_config(root):
    raw = _read(os.path.join(root, ".claude", "grimoire-config.json"))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_parse_error": True}


def parse_releases(version_history_text):
    """Extract '## vX.Y — Title' sections, newest-first as written."""
    if not version_history_text:
        return []
    rels = []
    for m in re.finditer(r"^##\s+(v\d+\.\d+)\s*(?:[—\-–]\s*(.*))?$",
                         version_history_text, re.MULTILINE):
        rels.append({"version": m.group(1), "title": (m.group(2) or "").strip()})
    return rels


def parse_in_flight(roadmap_text, shipped_versions):
    """A roadmap '## vX.Y' section whose body does not say 'Shipped' and whose
    version is not in version-history is the in-flight / next release."""
    if not roadmap_text:
        return None
    # Split into ## sections.
    sections = re.split(r"(?m)^(##\s+v\d+\.\d+.*)$", roadmap_text)
    # sections = [pre, header1, body1, header2, body2, ...]
    i = 1
    while i < len(sections):
        header = sections[i]
        body = sections[i + 1] if i + 1 < len(sections) else ""
        vm = re.search(r"(v\d+\.\d+)", header)
        if vm:
            ver = vm.group(1)
            shipped = ("Shipped —" in body) or ("(released" in body) or (ver in shipped_versions)
            if not shipped:
                tm = re.search(r"##\s+v\d+\.\d+\s*[—\-–]\s*(.*)", header)
                return {"version": ver, "title": (tm.group(1).strip() if tm else "")}
        i += 2
    return None


def parse_manifest_version(manifest_text):
    if not manifest_text:
        return None
    m = re.search(r"^manifest-version:\s*(\d+)", manifest_text, re.MULTILINE)
    return int(m.group(1)) if m else None


def detect_tech_stack(root):
    """Read package manifests for name/version; deterministic, no inference."""
    out = []
    # package.json (JSON)
    pj = _read(os.path.join(root, "package.json"))
    if pj:
        try:
            d = json.loads(pj)
            out.append({"file": "package.json", "ecosystem": "node",
                        "name": d.get("name"), "version": d.get("version")})
        except json.JSONDecodeError:
            out.append({"file": "package.json", "ecosystem": "node", "_parse_error": True})
    # Cargo.toml (read name/version lines without a TOML parser)
    ct = _read(os.path.join(root, "Cargo.toml"))
    if ct:
        nm = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', ct)
        vr = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', ct)
        out.append({"file": "Cargo.toml", "ecosystem": "rust",
                    "name": nm.group(1) if nm else None,
                    "version": vr.group(1) if vr else None})
    # pyproject.toml
    pp = _read(os.path.join(root, "pyproject.toml"))
    if pp:
        nm = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', pp)
        vr = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', pp)
        out.append({"file": "pyproject.toml", "ecosystem": "python",
                    "name": nm.group(1) if nm else None,
                    "version": vr.group(1) if vr else None})
    # go.mod
    gm = _read(os.path.join(root, "go.mod"))
    if gm:
        nm = re.search(r"(?m)^module\s+(\S+)", gm)
        out.append({"file": "go.mod", "ecosystem": "go",
                    "name": nm.group(1) if nm else None, "version": None})
    return out


def build_status(root):
    sources_read, degraded = [], []

    cfg = read_config(root)
    dials = {}
    name = framework_version = schema_version = paradigm = None
    if cfg and not cfg.get("_parse_error"):
        sources_read.append(".claude/grimoire-config.json")
        name = cfg.get("name")
        framework_version = cfg.get("framework-version")
        schema_version = cfg.get("schema-version")
        paradigm = _dial(cfg, "work-paradigm")
        for k in ("workflow-variant", "model-effort-profile", "release-phase-model",
                  "stealth-mode", "project-manager", "issue-tracker"):
            if k in cfg:
                dials[k] = _dial(cfg, k) if k != "project-manager" else cfg[k]
    else:
        degraded.append(".claude/grimoire-config.json (missing or unparseable)")

    vh = _read(os.path.join(root, "docs", "version-history.md"))
    releases = parse_releases(vh)
    if vh is not None:
        sources_read.append("docs/version-history.md")
    else:
        degraded.append("docs/version-history.md (missing)")
    shipped_versions = {r["version"] for r in releases}

    rm = _read(os.path.join(root, "docs", "roadmap.md"))
    in_flight = parse_in_flight(rm, shipped_versions)
    if rm is not None:
        sources_read.append("docs/roadmap.md")
    else:
        degraded.append("docs/roadmap.md (missing)")

    manifest = _read(os.path.join(
        root, ".claude", "skills", "sync-from-upstream", "feature-manifest.md"))
    manifest_version = parse_manifest_version(manifest)
    if manifest is not None:
        sources_read.append(".claude/skills/sync-from-upstream/feature-manifest.md")

    tech = detect_tech_stack(root)
    for t in tech:
        sources_read.append(t["file"])

    return {
        "project": name,
        "framework_version": framework_version,
        "schema_version": schema_version,
        "paradigm": paradigm,
        "dials": dials,
        "latest_release": releases[0] if releases else None,
        "recent_releases": releases[:5],
        "in_flight": in_flight,
        "feature_manifest_version": manifest_version,
        "tech_stack": tech,
        "sources_read": sources_read,
        "degraded": degraded,
        "note": "Structured layer only. The status-broker adds the issue tracker "
                "(authoritative for tracked work) and, last, source code.",
    }


def _self_test():
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".claude", "skills", "sync-from-upstream"))
        os.makedirs(os.path.join(d, "docs"))
        with open(os.path.join(d, ".claude", "grimoire-config.json"), "w") as fh:
            json.dump({"schema-version": 4, "name": "Demo",
                       "framework-version": "v3.2",
                       "work-paradigm": {"value": "Noir"},
                       "stealth-mode": {"value": "off"},
                       "project-manager": {"max-parallel": {"value": 3}}}, fh)
        with open(os.path.join(d, "docs", "version-history.md"), "w") as fh:
            fh.write("# Version History\n\n## v3.2 — Sync reliability\n\nbody\n\n"
                     "## v3.1 — Project Manager agent role\n\nbody\n")
        with open(os.path.join(d, "docs", "roadmap.md"), "w") as fh:
            fh.write("# Roadmap\n\n## v3.3 — Scripting & status-broker\n\n"
                     "planned, not yet shipped\n\n## v3.2 — Sync reliability\n\n"
                     "Shipped — see version-history.md.\n")
        with open(os.path.join(d, ".claude", "skills", "sync-from-upstream",
                               "feature-manifest.md"), "w") as fh:
            fh.write("manifest-version: 19\n\n# Feature manifest\n")
        with open(os.path.join(d, "pyproject.toml"), "w") as fh:
            fh.write('[project]\nname = "demo"\nversion = "0.4.2"\n')

        s = build_status(d)
        if s["project"] != "Demo": failures.append("name not read")
        if s["framework_version"] != "v3.2": failures.append("framework-version not read")
        if s["paradigm"] != "Noir": failures.append("paradigm not read")
        if not s["latest_release"] or s["latest_release"]["version"] != "v3.2":
            failures.append("latest release wrong: %r" % s["latest_release"])
        if len(s["recent_releases"]) != 2: failures.append("recent releases count")
        if not s["in_flight"] or s["in_flight"]["version"] != "v3.3":
            failures.append("in-flight detection wrong: %r" % s["in_flight"])
        if s["feature_manifest_version"] != 19: failures.append("manifest version")
        if not any(t.get("version") == "0.4.2" for t in s["tech_stack"]):
            failures.append("pyproject version not read")
        if "stealth-mode" not in s["dials"]: failures.append("dials missing stealth-mode")

        # determinism
        if json.dumps(build_status(d), sort_keys=True) != json.dumps(s, sort_keys=True):
            failures.append("non-deterministic output")

    # missing-sources degrade path
    with tempfile.TemporaryDirectory() as empty:
        s2 = build_status(empty)
        if not s2["degraded"]: failures.append("degraded not flagged on empty project")
        if s2["latest_release"] is not None: failures.append("latest release should be None")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("project_status self-test: OK (config/releases/in-flight/manifest/"
          "tech-stack reads, determinism, missing-source degrade)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic script-first project overview.")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not os.path.isdir(args.root):
        print("error: --root is not a directory: %s" % args.root, file=sys.stderr)
        return 2
    print(json.dumps(build_status(args.root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
