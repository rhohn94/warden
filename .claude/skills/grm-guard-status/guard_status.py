#!/usr/bin/env python3
"""guard_status.py — one-shot, read-only guard/paradigm/marker/branch printout.

Agents repeatedly `cat` the guard hook scripts mid-session to predict whether
an operation will be blocked (issue #429) — an investigation tax that also
nudges agents toward editing what they just read. This helper answers "what
would currently allow/deny" in one deterministic call: the active work
paradigm, whether this worktree carries the integration-allow marker, the
current branch and its protected-ref status, and — for each of the seven
shipped guard hooks — the capabilities its `HOOK_CONTRACT` header (#441)
declares, with a short human gloss per capability token.

This is READ-ONLY summary metadata. It parses the hooks' `HOOK_CONTRACT`
comment headers and other read-only state; it never edits a hook file and
never re-derives or executes a hook's actual gating logic. The hook file
itself remains authoritative for exact enforcement behavior on a genuinely
ambiguous case — this is a cheap first read, not a substitute for it.

Design authority: docs/grimoire/design/hook-contract-design.md (the
`HOOK_CONTRACT` header format, #441) and docs/grimoire/design/status-broker-design.md
(the ordered-lookup / script-first structural precedent this follows).

Usage:
  guard_status.py [--root DIR] [--json] [--self-test]
Default output is a human-readable text summary; --json emits the same data
as structured JSON. Exit 0 on success, 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

__all__ = [
    "read_config",
    "current_branch",
    "is_protected_branch",
    "hook_contract",
    "build_status",
    "render_text",
    "main",
]

CONFIG_REL = os.path.join(".claude", "grimoire-config.json")
MARKER_REL = os.path.join(".claude", "integration-allow.local")
HOOKS_DIR_REL = os.path.join(".claude", "hooks")

# Mirrors .claude/hooks/protected-branch-guard.sh's PROTECTED_RE exactly
# (dev / main / version/*) — kept as a separate, tiny copy rather than an
# import; this skill does not otherwise depend on the hooks package.
PROTECTED_RE = re.compile(r"^(dev|main|version/.*)$")

# Mirrors grm-install-doctor/install_doctor.py's HOOK_CONTRACT_RE (#441) —
# same header format, same regex. Kept as a separate, tiny copy rather than
# an import; this skill does not otherwise depend on grm-install-doctor.
HOOK_CONTRACT_RE = re.compile(
    r"^#\s*HOOK_CONTRACT:\s*v(\d+)\s+capabilities=\[([^\]]*)\]\s*$")

# The seven shipped guard hooks (hook-contract-design.md §Scope), each with a
# one-line summary drawn from that hook's own docstring.
HOOKS: tuple[tuple[str, str], ...] = (
    ("protected-branch-guard.sh",
     "Deny-by-default guard on history-mutating git ops (commit/merge/"
     "rebase/cherry-pick/revert) while HEAD is on a protected branch "
     "(dev/main/version/*)."),
    ("push-guard.sh",
     "Marker- and allowlist-gated `git push` guard."),
    ("stealth-guard.sh",
     'No-op unless stealth-mode.value == "on"; then enforces the five '
     "stealth artifact rails."),
    ("worktree-guard.sh",
     "Deny-by-default guard confining tool-call target paths to the "
     "active worktree."),
    ("bundled-sync-guard.sh",
     "Denies a commit whose staged changes span both framework/"
     "scaffolding and project-content touch-sets at once."),
    ("release-plan-guard.sh",
     "Blocks writes to §§1-4 of a status: agreed release-planning "
     "doc; §5 (the ledger) stays writable."),
    ("autonomy-allow.sh",
     "Paradigm-aware PreToolUse(Bash) prompt suppression for the "
     "guard-vetted commands the release pipeline runs constantly."),
)

# A short human gloss per capability token, drawn from the declaring hook's
# own docstring (hook-contract-design.md: "one token per named rule/rail").
# READ-ONLY summary only — a token with no gloss here just falls back to the
# raw token string; the hook file remains the source of truth.
CAPABILITY_GLOSS: dict[str, str] = {
    "protected-branch-block":
        "Blocks commit/merge/rebase/cherry-pick/revert on dev/main/"
        "version/* without the integration-allow marker.",
    "branch-hygiene-block":
        "Blocks branching off a protected ref onto a wrong base / "
        "skipping the staging flow (v3.63).",
    "history-rewrite-block":
        "Blocks history-rewrite ops (reset --hard, rebase, etc.) on a "
        "protected branch (v3.15, #84).",
    "cross-worktree-hijack-block":
        "Blocks a git/tool operation that would hijack another "
        "worktree's branch or checkout (v1.7).",
    "master-head-drift-block":
        "Denies a marked (integration-master) worktree's history-"
        "mutating op once its HEAD has drifted onto an unprotected "
        "branch (v1.19, #35).",
    "release-boundary-guard":
        "On `main`, allows a marked actor's commit/merge only inside a "
        "clean release-promotion boundary (v3.64, #214).",
    "worktree-cleanup-allow":
        "Lets the marker-blessed integration worktree remove a "
        "verified-merged dead sibling worktree.",
    "push-block-default":
        "Blocks `git push` by default (no marker = no push).",
    "push-allowlist":
        "With the marker, still requires every pushed ref be on the "
        "project's push allowlist (main, dev, version tags, or "
        ".claude/push-allowlist entries).",
    "marker-gated-push":
        "Requires .claude/integration-allow.local in the active "
        "worktree before a push is even considered.",
    "destructive-flag-block":
        "Denies destructive/broad push flags (--force, "
        "--force-with-lease, --all, --mirror, --delete, --prune) even "
        "with the marker.",
    "autonomous-push":
        "When autonomous-push.enabled=true, suppresses the push-class "
        "permission prompt (push-guard.sh's `git push` prompt and/or "
        "autonomy-allow.sh's push-class `gh` prompts).",
    "stealth-no-push":
        "Stealth on: denies every `git push`, even from the marker-"
        "blessed worktree.",
    "stealth-no-managed-commit":
        "Stealth on: denies staging/committing any Grimoire-managed "
        "path (.claude/, CLAUDE.md, design docs, ...).",
    "stealth-commit-hygiene":
        "Stealth on: denies a commit message carrying an AI/agent tell "
        "(claude / anthropic / Co-Authored-By / ...).",
    "stealth-no-branch-model":
        "Stealth on: denies naming a branch after the agent/model.",
    "stealth-no-managed-edit":
        "Stealth on: denies Edit/Write/NotebookEdit on a Grimoire-"
        "managed path outside the allowed exceptions.",
    "worktree-confinement":
        "Blocks a tool call whose target path escapes the active "
        "worktree into the canonical checkout or a sibling worktree.",
    "bundled-sync-commit-block":
        "Denies a single commit whose staged changes span both broad "
        "framework/scaffolding paths and non-framework project content "
        "at once (#126, BMI-3).",
    "release-plan-agreed-lock":
        "Blocks Write (full overwrite) and an Edit landing before the "
        "§5 heading on a release-planning doc whose header says "
        "status: agreed.",
    "release-plan-ledger-writable":
        "§5 (the implementation ledger) of a release-planning doc "
        "stays writable regardless of the agreed-lock.",
    "autonomy-allow-noir":
        "Under the Noir paradigm, auto-approves the guard-vetted, "
        "non-destructive Bash commands the release pipeline runs "
        "constantly (suppresses the permission prompt, not the "
        "underlying guard checks).",
}


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _dial(cfg: dict, key: str):
    """Return a config dial's .value, the raw scalar, or None."""
    v = cfg.get(key)
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


def read_config(root: str) -> dict:
    raw = _read(os.path.join(root, CONFIG_REL))
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_parse_error": True}
    return data if isinstance(data, dict) else {}


def current_branch(root: str) -> str | None:
    """Mirrors .claude/hooks/_hook_common.py::current_branch — kept as a
    separate, tiny copy rather than an import (this skill does not
    otherwise depend on the hooks package)."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None  # detached HEAD / not a repo
    return out.stdout.strip() or None


def is_protected_branch(branch: str | None) -> bool:
    return bool(branch) and bool(PROTECTED_RE.match(branch))


def hook_contract(hook_path: str) -> tuple[str | None, list[str]]:
    """Parse a hook's HOOK_CONTRACT header.

    Returns (version, capabilities). version is e.g. "v1", or None if the
    file is absent or carries no recognizable header — capabilities is then
    an empty list, the same fail-closed direction install_doctor.py's
    _hook_contract() uses for its audit.
    """
    if not os.path.isfile(hook_path):
        return None, []
    try:
        with open(hook_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 15:  # header lives in the first few lines; bound the scan
                    break
                m = HOOK_CONTRACT_RE.match(line.rstrip("\n"))
                if m:
                    version, caps = m.groups()
                    cap_list = sorted(
                        c.strip() for c in caps.split(",") if c.strip())
                    return f"v{version}", cap_list
    except OSError:
        pass
    return None, []


def build_status(root: str) -> dict:
    sources_read: list[str] = []
    degraded: list[str] = []

    cfg = read_config(root)
    if cfg and not cfg.get("_parse_error"):
        sources_read.append(CONFIG_REL)
    else:
        degraded.append(f"{CONFIG_REL} (missing or unparseable)")
    paradigm = _dial(cfg, "work-paradigm")
    stealth = _dial(cfg, "stealth-mode")

    marker_present = os.path.isfile(os.path.join(root, MARKER_REL))

    branch = current_branch(root)
    protected = is_protected_branch(branch)

    hooks_dir = os.path.join(root, HOOKS_DIR_REL)
    hooks = []
    for name, summary in HOOKS:
        hook_path = os.path.join(hooks_dir, name)
        version, caps = hook_contract(hook_path)
        if version is not None:
            sources_read.append(f"{HOOKS_DIR_REL}/{name}")
        elif os.path.isfile(hook_path):
            degraded.append(f"{HOOKS_DIR_REL}/{name} (no HOOK_CONTRACT header)")
        else:
            degraded.append(f"{HOOKS_DIR_REL}/{name} (missing)")
        hooks.append({
            "hook": name,
            "summary": summary,
            "contract_version": version,
            "capabilities": [
                {"token": c, "gloss": CAPABILITY_GLOSS.get(c, "(no gloss on file)")}
                for c in caps
            ],
        })

    return {
        "work_paradigm": paradigm,
        "stealth_mode": stealth,
        "integration_marker_present": marker_present,
        "integration_marker_path": MARKER_REL,
        "current_branch": branch,
        "branch_is_protected": protected,
        "hooks": hooks,
        "sources_read": sources_read,
        "degraded": degraded,
        "note": "Read-only capability summary parsed from each hook's "
                "HOOK_CONTRACT header (#441) plus paradigm/marker/branch "
                "state. The hook file itself is authoritative for exact "
                "enforcement logic on a genuinely ambiguous case.",
    }


def render_text(status: dict) -> str:
    lines = [
        f"work-paradigm:      {status['work_paradigm'] or '(unset)'}",
        f"stealth-mode:       {status['stealth_mode'] or '(unset)'}",
    ]
    marker = "present" if status["integration_marker_present"] else "absent"
    lines.append(f"integration marker: {marker}  ({status['integration_marker_path']})")
    branch = status["current_branch"] or "(detached HEAD / not a repo)"
    protection = "PROTECTED" if status["branch_is_protected"] else "unprotected"
    lines.append(f"current branch:     {branch}  [{protection}]")
    if status["branch_is_protected"]:
        if status["integration_marker_present"]:
            verdict = ("marker present -> history-mutating ops MAY be "
                       "allowed, subject to each hook's own predicate below "
                       "(e.g. release-boundary-guard on main, "
                       "master-head-drift-block).")
        else:
            verdict = ("no marker -> history-mutating ops (commit/merge/"
                       "rebase/...) DENIED by protected-branch-guard.sh.")
        lines.append(f"  -> {verdict}")
    lines.append("")
    lines.append("guard hooks (HOOK_CONTRACT capabilities):")
    for h in status["hooks"]:
        lines.append(f"  {h['hook']}  [{h['contract_version'] or 'NO CONTRACT HEADER'}]")
        lines.append(f"    {h['summary']}")
        if not h["capabilities"]:
            lines.append("    (no declared capabilities — file missing or header absent)")
        for cap in h["capabilities"]:
            lines.append(f"    - {cap['token']}: {cap['gloss']}")
    if status["degraded"]:
        lines.append("")
        lines.append("degraded (missing/unreadable sources):")
        for d in status["degraded"]:
            lines.append(f"  - {d}")
    return "\n".join(lines)


def _self_test() -> int:
    import tempfile
    failures = []

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".claude", "hooks"))
        with open(os.path.join(d, ".claude", "grimoire-config.json"), "w") as fh:
            json.dump({"schema-version": 4, "name": "Demo",
                       "work-paradigm": {"value": "Noir"},
                       "stealth-mode": {"value": "off"}}, fh)

        # git init + a protected branch name, so current_branch/is_protected
        # are exercised against a real repo, not a mock.
        subprocess.run(["git", "init", "-q", "-b", "dev", d], check=True)

        # Seven hook fixtures: five well-formed (varying contract versions and
        # a capability with no gloss entry), one header-less, one absent
        # entirely — to exercise every branch of hook_contract()/build_status().
        fixtures = {
            "protected-branch-guard.sh":
                "#!/usr/bin/env python3\n"
                "# HOOK_CONTRACT: v1 capabilities=[protected-branch-block,branch-hygiene-block]\n"
                '"""doc"""\n',
            "push-guard.sh":
                "#!/usr/bin/env python3\n"
                "# HOOK_CONTRACT: v2 capabilities=[push-block-default,push-allowlist]\n"
                '"""doc"""\n',
            "stealth-guard.sh":
                "#!/usr/bin/env python3\n"
                "# HOOK_CONTRACT: v1 capabilities=[stealth-no-push]\n"
                '"""doc"""\n',
            "worktree-guard.sh":
                "#!/usr/bin/env python3\n"
                "# HOOK_CONTRACT: v1 capabilities=[worktree-confinement,made-up-token]\n"
                '"""doc"""\n',
            "bundled-sync-guard.sh":
                "#!/usr/bin/env python3\n"
                "# HOOK_CONTRACT: v1 capabilities=[bundled-sync-commit-block]\n"
                '"""doc"""\n',
            "release-plan-guard.sh":
                "#!/usr/bin/env python3\n"
                '"""header-less hook — no HOOK_CONTRACT line at all"""\n',
            # autonomy-allow.sh deliberately absent — exercises the
            # "missing" degrade branch.
        }
        for name, content in fixtures.items():
            with open(os.path.join(d, ".claude", "hooks", name), "w") as fh:
                fh.write(content)

        s = build_status(d)

        if s["work_paradigm"] != "Noir":
            failures.append("work_paradigm not read: %r" % s["work_paradigm"])
        if s["stealth_mode"] != "off":
            failures.append("stealth_mode not read: %r" % s["stealth_mode"])
        if s["integration_marker_present"] is not False:
            failures.append("marker should be absent by default")
        if s["current_branch"] != "dev":
            failures.append("current_branch wrong: %r" % s["current_branch"])
        if s["branch_is_protected"] is not True:
            failures.append("'dev' should be protected")

        by_name = {h["hook"]: h for h in s["hooks"]}
        if len(s["hooks"]) != len(HOOKS):
            failures.append("hooks list should cover all %d shipped hooks, got %d"
                            % (len(HOOKS), len(s["hooks"])))

        pbg = by_name["protected-branch-guard.sh"]
        if pbg["contract_version"] != "v1":
            failures.append("protected-branch-guard.sh version wrong: %r" % pbg["contract_version"])
        caps = {c["token"] for c in pbg["capabilities"]}
        if caps != {"protected-branch-block", "branch-hygiene-block"}:
            failures.append("protected-branch-guard.sh capabilities wrong: %r" % caps)
        if pbg["capabilities"][0]["gloss"] == "(no gloss on file)":
            failures.append("protected-branch-block should have a real gloss")

        wg = by_name["worktree-guard.sh"]
        made_up = next(c for c in wg["capabilities"] if c["token"] == "made-up-token")
        if made_up["gloss"] != "(no gloss on file)":
            failures.append("unglossed capability should fall back cleanly: %r" % made_up)

        rpg = by_name["release-plan-guard.sh"]
        if rpg["contract_version"] is not None or rpg["capabilities"]:
            failures.append("header-less hook should parse to (None, [])")
        if not any("release-plan-guard.sh" in x and "no HOOK_CONTRACT header" in x
                   for x in s["degraded"]):
            failures.append("header-less hook should be flagged degraded")

        aa = by_name["autonomy-allow.sh"]
        if aa["contract_version"] is not None or aa["capabilities"]:
            failures.append("absent hook should parse to (None, [])")
        if not any("autonomy-allow.sh" in x and "missing" in x for x in s["degraded"]):
            failures.append("absent hook should be flagged degraded")

        # determinism
        if json.dumps(build_status(d), sort_keys=True) != json.dumps(s, sort_keys=True):
            failures.append("non-deterministic output")

        # render_text smoke: every hook name and the branch line show up.
        text = render_text(s)
        if "current branch:     dev  [PROTECTED]" not in text:
            failures.append("render_text missing protected-branch line")
        for name, _ in HOOKS:
            if name not in text:
                failures.append("render_text missing hook name: %s" % name)

    # Unprotected-branch + present-marker case, on a fresh repo.
    with tempfile.TemporaryDirectory() as d2:
        os.makedirs(os.path.join(d2, ".claude", "hooks"))
        subprocess.run(["git", "init", "-q", "-b", "my-feature-branch", d2], check=True)
        open(os.path.join(d2, ".claude", "integration-allow.local"), "w").close()
        s2 = build_status(d2)
        if s2["current_branch"] != "my-feature-branch":
            failures.append("unprotected branch name wrong: %r" % s2["current_branch"])
        if s2["branch_is_protected"] is not False:
            failures.append("'my-feature-branch' should not be protected")
        if s2["integration_marker_present"] is not True:
            failures.append("marker should be detected when present")
        if not s2["degraded"]:
            failures.append("empty hooks dir should degrade every hook entry")

    # No-repo / no-config case: everything degrades cleanly, nothing crashes.
    with tempfile.TemporaryDirectory() as empty:
        s3 = build_status(empty)
        if s3["current_branch"] is not None:
            failures.append("non-repo current_branch should be None")
        if s3["branch_is_protected"] is not False:
            failures.append("non-repo branch should not be protected")
        if not s3["degraded"]:
            failures.append("missing config/hooks should be flagged degraded")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("guard_status self-test: OK (config/marker/branch reads, "
          "HOOK_CONTRACT parsing for all 7 hooks, protected-branch "
          "detection, gloss fallback, header-less/absent-hook degrade "
          "paths, render_text, determinism)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="One-shot read-only guard/paradigm/marker/branch status.")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not os.path.isdir(args.root):
        print("error: --root is not a directory: %s" % args.root, file=sys.stderr)
        return 2
    status = build_status(args.root)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(render_text(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
