#!/usr/bin/env python3
"""fleet_git_audit.py — one fleet-wide, read-only git-hygiene audit (#326).

Three prior work items in this release each built one piece of git-hygiene
tooling: `worktree_reap.py` (#449, the reap safety predicate),
`agent_branch_namespace.py` (#456, the `claude/`-prefix classifier), and
`stale_remote_report.py` (#455, remote-branch staleness). Nothing composed
them into a single view — a human or the release-train orchestration who
wants "what does this fleet's git hygiene look like right now" has to run
all three separately and mentally merge the output. This script closes that
gap by importing all three directly (never shelling out, never re-deriving
their logic) and folding their findings into one report across three
dimensions:

  1. **Worktree/branch hygiene** — every worktree from `git worktree list
     --porcelain`, classified via `is_agent_branch()` (#456) and, for agent
     worktrees, checked against the #449 `is_safe_to_reap()` predicate using
     the most sensible `--landed-ref`: the current release's `version/{X.Y}`
     staging branch if one exists, else `dev` (the same resolution
     `grm-worktree-preflight`'s Step 0.5 and #452's self-healing sweep both
     use). Buckets: `safe_to_reap`, `live_unmerged` (informational, not a
     problem), `human_owned` (never touched — includes both genuinely
     human-created branches and the protected staging set), and `detached`
     (a worktree with no branch to classify).
  2. **Stale remote branches** — `stale_remote_report.generate_report()`
     (#455) called directly, its `branches`/`candidates`/`old_branches`
     folded into this report verbatim, per that module's own documented
     integration point for #326.
  3. **Namespace conformance** — any branch (local or remote) that
     `is_agent_branch()` classifies `True` only via the *legacy fallback
     tier* (`worktree-agent-*`, `worker-*`, `wf-*` — see
     `docs/grimoire/integration-workflow.md` §Canonical agent-branch
     namespace) rather than the canonical `claude/` prefix. These are the
     conformance gaps #456 was meant to drain down over time; a fleet audit
     that never reports them can't tell whether the drain-down is actually
     happening.

**Read-only, report-only — this is an audit skill, not a cleanup skill.**
Every git call this script makes (directly, or via the three imported
modules) is a read. It never removes a worktree, never deletes a branch,
never pushes or force-anything. `worktree_reap.py`'s own `reap()`/`main()`
entry points (which DO mutate) are deliberately never imported here — only
the read-only `is_safe_to_reap()` predicate and the read-only
`_worktree_branch_map()` helper are. Use `worktree_reap.py --dry-run` (or
`trim.py`, the confirm-gated human wrapper) to actually act on a finding
this script surfaces.

CLI usage:
  fleet_git_audit.py [--remote NAME] [--min-age-days N] [--landed-ref REF]
                      [--format text|json] [--self-test]

  --landed-ref overrides the auto-resolved "version/{X.Y} if it exists, else
  dev" default — useful for a lane-scoped Project Manager audit or a fixture
  run. Auto-resolution runs whenever this flag is omitted.

Importable as a library, matching the `generate_report()` shape
`stale_remote_report.py` already established:

    sys.path.insert(0, os.path.join(REPO_ROOT, ".claude", "skills", "grm-fleet-git-audit"))
    from fleet_git_audit import generate_fleet_report
    report = generate_fleet_report(remote="origin", min_age_days=30, cwd=REPO_ROOT)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Sibling skill directory — the three modules this audit composes live there
# (#449, #456, #455). Same cross-skill sys.path convention `trim.py` already
# uses for its own dependency on `worktree_reap`/`agent_branch_namespace`.
_PREFLIGHT_DIR = os.path.normpath(os.path.join(_HERE, "..", "grm-worktree-preflight"))
if _PREFLIGHT_DIR not in sys.path:
    sys.path.insert(0, _PREFLIGHT_DIR)

from worktree_reap import (  # noqa: E402
    GitCommandError,
    _worktree_branch_map,
    is_safe_to_reap,
)
from agent_branch_namespace import CANONICAL_PREFIX, is_agent_branch  # noqa: E402
from stale_remote_report import generate_report, list_remote_branches  # noqa: E402


# Protected/staging set — never an agent branch, never "human-owned" in the
# ordinary sense either, but this audit still reports these worktrees (under
# `human_owned`, flagged `protected: True`) rather than silently dropping
# them, matching `is_agent_branch()`'s own "never auto-touch" posture for the
# set. Deliberately a local duplicate of `agent_branch_namespace.py`'s
# private `_PROTECTED_NAMES`/`_PROTECTED_PATTERN` rather than importing them
# — `stale_remote_report.py` already established the "duplicate the small
# protected set per-module so one change doesn't silently desync the other"
# precedent for this exact set; this module follows it too.
_PROTECTED_NAMES = frozenset({"main", "dev", "home"})
_PROTECTED_PATTERN = re.compile(r"^version/")


def _is_protected(name: str) -> bool:
    return name in _PROTECTED_NAMES or bool(_PROTECTED_PATTERN.match(name))


class FleetAuditError(RuntimeError):
    """A git command this audit needed failed unexpectedly."""


def _run_git(args: list, cwd: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FleetAuditError(
            "git %s failed (exit %d): %s"
            % (" ".join(args), result.returncode, result.stderr.strip())
        )
    return result


# --------------------------------------------------------------------------
# Dimension 1 — landed-ref resolution + worktree/branch hygiene
# --------------------------------------------------------------------------

_VERSION_BRANCH_RE = re.compile(r"^version/(\d+)\.(\d+)$")


def resolve_landed_ref(cwd: str | None = None) -> str:
    """The "most sensible --landed-ref" for this repo right now: the highest
    `version/{X.Y}` local staging branch if one exists (an in-flight
    release), else `dev`. Mirrors the resolution `grm-worktree-preflight`'s
    Step 0.5 and #452's self-healing sweep both already use — re-derived
    here rather than imported, since neither exposes it as a plain function."""
    result = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/version/"], cwd=cwd
    )
    candidates = []
    for line in result.stdout.splitlines():
        line = line.strip()
        m = _VERSION_BRANCH_RE.match(line)
        if m:
            candidates.append((int(m.group(1)), int(m.group(2)), line))
    if candidates:
        candidates.sort()
        return candidates[-1][2]
    return "dev"


def audit_worktrees(landed_ref: str, cwd: str | None = None) -> dict:
    """Classify every worktree from `git worktree list --porcelain` into the
    four buckets described in the module docstring. Never mutates anything —
    only reads `_worktree_branch_map()` (#449) and calls the read-only
    `is_safe_to_reap()` predicate (#449) for agent-namespaced branches."""
    wt_map = _worktree_branch_map(cwd=cwd)

    safe_to_reap = []
    live_unmerged = []
    human_owned = []
    detached = []
    errors = []

    for path in sorted(wt_map):
        branch = wt_map[path]
        if branch is None:
            detached.append({"path": path})
            continue

        if not is_agent_branch(branch):
            human_owned.append(
                {"path": path, "branch": branch, "protected": _is_protected(branch)}
            )
            continue

        try:
            safe = is_safe_to_reap(branch, landed_ref, cwd=cwd)
        except GitCommandError as exc:
            errors.append({"path": path, "branch": branch, "error": str(exc)})
            continue

        entry = {"path": path, "branch": branch}
        (safe_to_reap if safe else live_unmerged).append(entry)

    return {
        "landed_ref": landed_ref,
        "safe_to_reap": safe_to_reap,
        "live_unmerged": live_unmerged,
        "human_owned": human_owned,
        "detached": detached,
        "errors": errors,
    }


# --------------------------------------------------------------------------
# Dimension 3 — namespace conformance
# --------------------------------------------------------------------------

def _local_branch_names(cwd: str | None = None) -> list:
    result = _run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd=cwd)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def audit_namespace_conformance(remote: str = "origin", cwd: str | None = None) -> dict:
    """Branches (local or remote) that `is_agent_branch()` (#456) classifies
    `True` only via the legacy fallback tier, never the canonical `claude/`
    prefix — the conformance gaps the namespace convention was meant to
    close over time. A branch minted under `claude/` is, by construction,
    never reported here."""
    local_gaps = sorted(
        name for name in _local_branch_names(cwd=cwd)
        if is_agent_branch(name) and not name.startswith(CANONICAL_PREFIX)
    )
    remote_gaps = sorted(
        name for name in list_remote_branches(remote=remote, cwd=cwd)
        if is_agent_branch(name) and not name.startswith(CANONICAL_PREFIX)
    )
    return {"local": local_gaps, "remote": remote_gaps}


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

def generate_fleet_report(remote: str = "origin", min_age_days: float = 30,
                           landed_ref: str | None = None, cwd: str | None = None,
                           now_epoch: float | None = None) -> dict:
    """Build the full fleet-wide git-hygiene report — the single entry point
    #326 exists to provide. Plain function, no CLI/printing side effects, so
    another skill (or a future release-train orchestration call site) can
    import and call this directly rather than shelling out. Never mutates
    the repository; every dimension below is read-only."""
    resolved_landed_ref = landed_ref or resolve_landed_ref(cwd=cwd)

    worktrees = audit_worktrees(resolved_landed_ref, cwd=cwd)
    stale_remote = generate_report(remote=remote, min_age_days=min_age_days,
                                    cwd=cwd, now_epoch=now_epoch)
    namespace_conformance = audit_namespace_conformance(remote=remote, cwd=cwd)

    return {
        "landed_ref": resolved_landed_ref,
        "remote": remote,
        "generated_at_epoch": now_epoch if now_epoch is not None else time.time(),
        "worktrees": worktrees,
        "stale_remote": stale_remote,
        "namespace_conformance": namespace_conformance,
    }


def _format_text(report: dict) -> str:
    lines = []
    lines.append(
        "fleet-git-audit: landed-ref=%s remote=%s"
        % (report["landed_ref"], report["remote"])
    )
    lines.append("")

    wt = report["worktrees"]
    lines.append("== Worktree / branch hygiene ==")
    lines.append("Safe-to-reap agent worktrees (merged + remote-safe against %s):"
                  % wt["landed_ref"])
    if wt["safe_to_reap"]:
        for e in wt["safe_to_reap"]:
            lines.append("  SAFE-TO-REAP  %s  (branch '%s')" % (e["path"], e["branch"]))
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Live/unmerged agent worktrees (informational, not a problem):")
    if wt["live_unmerged"]:
        for e in wt["live_unmerged"]:
            lines.append("  LIVE  %s  (branch '%s')" % (e["path"], e["branch"]))
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Human-owned worktrees (never touched):")
    if wt["human_owned"]:
        for e in wt["human_owned"]:
            tag = "protected" if e["protected"] else "human"
            lines.append("  %s  %s  (branch '%s')" % (tag.upper().ljust(9), e["path"], e["branch"]))
    else:
        lines.append("  (none)")

    if wt["detached"]:
        lines.append("")
        lines.append("Detached-HEAD worktrees (no branch to classify):")
        for e in wt["detached"]:
            lines.append("  DETACHED  %s" % e["path"])

    if wt["errors"]:
        lines.append("")
        lines.append("Safety-predicate check errors (reported, not silently skipped):")
        for e in wt["errors"]:
            lines.append("  ERROR  %s  (branch '%s')  %s" % (e["path"], e["branch"], e["error"]))

    lines.append("")
    lines.append("== Stale remote branches ==")
    sr = report["stale_remote"]
    lines.append("%d remote branch(es) considered on '%s' (min-age-days=%s)."
                  % (len(sr["branches"]), sr["remote"], sr["min_age_days"]))
    lines.append("Agent-branch reap candidates (merged, no local copy):")
    if sr["candidates"]:
        for b in sr["candidates"]:
            lines.append("  CANDIDATE  %s  age=%.1fd  merged-into=%s"
                          % (b["remote_ref"], b["age_days"], ",".join(b["merged_into"])))
    else:
        lines.append("  (none)")
    lines.append("Old/inactive branches (age >= %s day(s), any provenance):"
                  % sr["min_age_days"])
    if sr["old_branches"]:
        for b in sr["old_branches"]:
            provenance = "agent" if b["is_agent_branch"] else "human"
            lines.append("  OLD  %s  age=%.1fd  %s" % (b["remote_ref"], b["age_days"], provenance))
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("== Namespace conformance (#456) ==")
    nc = report["namespace_conformance"]
    lines.append("Local branches matching only the legacy fallback tier "
                  "(not the canonical 'claude/' prefix):")
    if nc["local"]:
        for name in nc["local"]:
            lines.append("  GAP  local   %s" % name)
    else:
        lines.append("  (none)")
    lines.append("Remote branches matching only the legacy fallback tier:")
    if nc["remote"]:
        for name in nc["remote"]:
            lines.append("  GAP  remote  %s" % name)
    else:
        lines.append("  (none)")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Self-test — hermetic fixture repo under a temp dir; never touches this (or
# any real) repository's actual worktrees or branches. Covers at least one
# case per dimension: a safe-to-reap worktree, a stale remote branch, and a
# namespace-conformance gap — plus the landed-ref auto-resolution itself.
# --------------------------------------------------------------------------

def _git_ok(args: list, cwd: str) -> None:
    _run_git(args, cwd=cwd)


def _configure_identity(repo: str) -> None:
    _git_ok(["config", "user.email", "fleet-audit-selftest@example.com"], repo)
    _git_ok(["config", "user.name", "Fleet Audit Selftest"], repo)


def _commit_file(repo: str, name: str, content: str, message: str,
                  age_days: float | None = None) -> None:
    with open(os.path.join(repo, name), "a", encoding="utf-8") as fh:
        fh.write(content)
    _git_ok(["add", name], repo)

    env = None
    if age_days is not None:
        epoch = int(time.time() - age_days * 86400)
        date_spec = "%d +0000" % epoch
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = date_spec
        env["GIT_COMMITTER_DATE"] = date_spec

    result = subprocess.run(
        ["git", "commit", "-m", message], cwd=repo, capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        raise FleetAuditError("git commit failed: %s" % result.stderr.strip())


def _make_repo_with_origin(base_dir: str) -> str:
    origin = os.path.join(base_dir, "origin.git")
    _git_ok(["init", "--bare", "-b", "main", origin], base_dir)
    clone = os.path.join(base_dir, "clone")
    _git_ok(["clone", origin, clone], base_dir)
    _configure_identity(clone)
    _commit_file(clone, "README.md", "hello\n", "initial commit")
    _git_ok(["push", "origin", "main"], clone)
    return clone


def _self_test_resolve_landed_ref() -> list:
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as base:
        clone = _make_repo_with_origin(base)

        # No version/* branch yet — falls back to 'dev'.
        if resolve_landed_ref(cwd=clone) != "dev":
            failures.append("resolve_landed_ref() with no version/* branch should return 'dev'")

        # A single version/{X.Y} branch — must be picked over 'dev'.
        _git_ok(["branch", "version/3.95"], clone)
        if resolve_landed_ref(cwd=clone) != "version/3.95":
            failures.append("resolve_landed_ref() should pick the existing version/{X.Y} branch")

        # A higher version/{X.Y} branch — must be picked over the lower one.
        _git_ok(["branch", "version/4.1"], clone)
        if resolve_landed_ref(cwd=clone) != "version/4.1":
            failures.append("resolve_landed_ref() should pick the highest version/{X.Y} branch")

        # A lane-scoped branch (version/{X.Y}/lane-a) must never be picked —
        # only the bare version/{X.Y} shape matches.
        _git_ok(["branch", "version/9.9/lane-a"], clone)
        if resolve_landed_ref(cwd=clone) != "version/4.1":
            failures.append("resolve_landed_ref() must not match a lane-scoped version branch")

    return failures


def _self_test() -> int:
    import tempfile

    failures = []
    failures.extend(_self_test_resolve_landed_ref())

    with tempfile.TemporaryDirectory() as base:
        clone = _make_repo_with_origin(base)
        now_epoch = time.time()

        # --- Dimension 1: safe-to-reap agent worktree -----------------------
        _git_ok(["switch", "-c", "claude/r9-901-fixture-safe"], clone)
        _commit_file(clone, "safe.txt", "x\n", "agent work, safe to reap")
        _git_ok(["push", "origin", "claude/r9-901-fixture-safe"], clone)
        _git_ok(["switch", "main"], clone)
        _git_ok(["merge", "--no-ff", "claude/r9-901-fixture-safe"], clone)
        _git_ok(["push", "origin", "main"], clone)
        safe_wt = os.path.join(base, "wt-safe")
        _git_ok(["worktree", "add", safe_wt, "claude/r9-901-fixture-safe"], clone)

        # --- Dimension 1: live/unmerged agent worktree -----------------------
        _git_ok(["switch", "-c", "claude/r9-902-fixture-live", "main"], clone)
        _commit_file(clone, "live.txt", "y\n", "agent work, still in flight")
        _git_ok(["switch", "main"], clone)
        live_wt = os.path.join(base, "wt-live")
        _git_ok(["worktree", "add", live_wt, "claude/r9-902-fixture-live"], clone)

        # --- Dimension 1: human-owned worktree -------------------------------
        _git_ok(["switch", "-c", "robs-feature", "main"], clone)
        _commit_file(clone, "human.txt", "z\n", "human work")
        _git_ok(["switch", "main"], clone)
        human_wt = os.path.join(base, "wt-human")
        _git_ok(["worktree", "add", human_wt, "robs-feature"], clone)

        # --- Dimension 2: stale remote branch (merged, no local copy) -------
        _git_ok(["switch", "-c", "claude/r9-903-fixture-stale"], clone)
        _commit_file(clone, "stale.txt", "s\n", "old agent work", age_days=45)
        _git_ok(["push", "origin", "claude/r9-903-fixture-stale"], clone)
        _git_ok(["switch", "main"], clone)
        _git_ok(["merge", "--no-ff", "claude/r9-903-fixture-stale"], clone)
        _git_ok(["push", "origin", "main"], clone)
        _git_ok(["branch", "-D", "claude/r9-903-fixture-stale"], clone)

        # --- Dimension 3: namespace-conformance gap (local + remote) --------
        _git_ok(["switch", "-c", "worker-9001"], clone)
        _commit_file(clone, "legacy.txt", "l\n", "legacy-namespace agent work")
        _git_ok(["push", "origin", "worker-9001"], clone)
        _git_ok(["switch", "main"], clone)

        report = generate_fleet_report(remote="origin", landed_ref="main", cwd=clone,
                                        now_epoch=now_epoch)

        # Zero-mutation-adjacent sanity: worktree list still resolves cleanly
        # after building the report (no crash, no leftover lock).
        _run_git(["worktree", "list", "--porcelain"], cwd=clone)

        wt = report["worktrees"]
        safe_branches = {e["branch"] for e in wt["safe_to_reap"]}
        if "claude/r9-901-fixture-safe" not in safe_branches:
            failures.append("safe-to-reap fixture worktree should appear in safe_to_reap")

        live_branches = {e["branch"] for e in wt["live_unmerged"]}
        if "claude/r9-902-fixture-live" not in live_branches:
            failures.append("live/unmerged fixture worktree should appear in live_unmerged")

        human_branches = {e["branch"] for e in wt["human_owned"]}
        if "robs-feature" not in human_branches:
            failures.append("human-owned fixture worktree should appear in human_owned")
        human_entry = next((e for e in wt["human_owned"] if e["branch"] == "robs-feature"), None)
        if human_entry is not None and human_entry["protected"]:
            failures.append("'robs-feature' must not be classified protected")
        if any(e["branch"] == "claude/r9-901-fixture-safe" for e in wt["human_owned"]):
            failures.append("an agent-namespaced branch must never appear in human_owned")

        sr = report["stale_remote"]
        if not any(b["short_name"] == "claude/r9-903-fixture-stale" for b in sr["candidates"]):
            failures.append("stale remote-branch fixture should appear in stale_remote.candidates")

        nc = report["namespace_conformance"]
        if "worker-9001" not in nc["local"]:
            failures.append("legacy-namespace local branch should appear in namespace_conformance.local")
        if "worker-9001" not in nc["remote"]:
            failures.append("legacy-namespace remote branch should appear in namespace_conformance.remote")
        if any(name.startswith(CANONICAL_PREFIX) for name in nc["local"] + nc["remote"]):
            failures.append("a canonically-namespaced (claude/) branch must never be a conformance gap")

        # Text formatter must not crash and must mention every fixture branch.
        text = _format_text(report)
        for needle in ("claude/r9-901-fixture-safe", "claude/r9-903-fixture-stale", "worker-9001"):
            if needle not in text:
                failures.append("text report should mention %r" % needle)

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("fleet_git_audit self-test: OK (landed-ref resolution, safe-to-reap worktree, "
          "live/unmerged worktree, human-owned worktree, stale remote-branch candidate, "
          "namespace-conformance gap local+remote, text formatting)")
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Fleet-wide, read-only git-hygiene audit (#326): composes worktree_reap.py's "
            "(#449) safety predicate, agent_branch_namespace.py's (#456) classifier, and "
            "stale_remote_report.py's (#455) report into one pass over worktree/branch "
            "hygiene, stale remote branches, and #456 namespace-conformance gaps. "
            "Report-only — never removes a worktree, deletes a branch, or mutates the "
            "remote."
        )
    )
    ap.add_argument("--remote", default="origin",
                     help="Remote to inspect for the stale-remote and namespace-conformance "
                          "dimensions (default: origin).")
    ap.add_argument("--min-age-days", type=float, default=30,
                     help="Age threshold (days) for the stale-remote 'old' flag (default: 30).")
    ap.add_argument("--landed-ref", default=None,
                     help="Override the auto-resolved landed-ref (version/{X.Y} if it exists, "
                          "else dev) used for the worktree safe-to-reap check.")
    ap.add_argument("--format", choices=["text", "json"], default="text",
                     help="Output format (default: text).")
    ap.add_argument("--self-test", action="store_true",
                     help="Run the hermetic self-test suite (builds a temp fixture repo; "
                          "touches nothing in the real repository) and exit.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    try:
        report = generate_fleet_report(remote=args.remote, min_age_days=args.min_age_days,
                                        landed_ref=args.landed_ref)
    except (FleetAuditError, GitCommandError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_text(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
