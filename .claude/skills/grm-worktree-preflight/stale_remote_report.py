#!/usr/bin/env python3
"""stale_remote_report.py — report-only stale remote-branch audit (#455).

`worktree_reap.py` (#449) and `trim.py` (#446) reclaim *local* worktrees and
branches. Nothing in this fleet reports on the *remote* side: a task agent's
branch can be merged and its local worktree reaped, while a copy of the
branch lingers on `origin` forever because nothing ever looks at
`git branch -r`. This script closes that visibility gap — and only the
visibility gap.

**Report-only. Never deletes anything, never mutates the remote.** Remote
branch deletion is deliberately human-gated (the issue title says so
verbatim: "remote deletion stays human-gated"). This script's only output is
stdout (text or JSON) — it never calls any remote-mutating git command (no
forced push, no forced remote-tracking branch delete). Every git
call it makes is a read: `for-each-ref`, `log`, `merge-base --is-ancestor`,
`show-ref`.

**Classification, reusing #456 rather than re-deriving it.** Each remote
branch is classified agent-created vs. human-created via
`agent_branch_namespace.is_agent_branch()` (#456) — imported directly, not
regex-guessed again — plus its last-commit age (`git log -1 --format=%ct`)
and merge status against the local `dev`/`main` (falling back to their
remote-tracking counterparts when no local branch exists) via
`git merge-base --is-ancestor`.

A branch is an **agent-reap candidate** iff: it classifies agent-created,
it has already merged into at least one of `dev`/`main`, and no local branch
of the same name exists (so `worktree_reap.py`/`trim.py` have nothing left
to reap locally — the remote copy is the only thing left over). This is
"likely safe to delete" framing only, never an instruction the script itself
acts on.

A branch is separately flagged **old** iff its last-commit age is
`>= --min-age-days`, regardless of merge status or namespace — an old,
never-merged, possibly-human branch is exactly the case that needs a human's
judgement, not an agent's, so it is reported (not filtered out).

**Integration point for #326 (`grm-fleet-git-audit`).** #326 is a later,
sibling work item in this release that composes multiple hygiene checks
(including this one) into one fleet-wide audit. It should call the plain
function below rather than shelling out to this file's CLI or re-deriving
remote-branch classification:

    sys.path.insert(0, os.path.join(REPO_ROOT, ".claude", "skills", "grm-worktree-preflight"))
    from stale_remote_report import generate_report

    report = generate_report(remote="origin", min_age_days=30, cwd=REPO_ROOT)
    # report["candidates"] / report["old_branches"] / report["branches"]

This mirrors the `from worktree_reap import is_safe_to_reap` cross-skill
import convention `trim.py` already uses for its own dependency on #449/#456.

CLI usage:
  stale_remote_report.py [--remote NAME] [--min-age-days N]
                          [--format text|json] [--self-test]

No git writes anywhere in this file — verified by the self-test, which
fingerprints the fixture repo's refs before and after calling
`generate_report()` and asserts they are byte-identical.
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

from agent_branch_namespace import is_agent_branch  # noqa: E402

# Candidate refs "dev"/"main" are checked against, in this order. Mirrors the
# reap-engine's own "caller names the landed-ref" posture (worktree_reap.py's
# module docstring) but this script checks both rather than requiring the
# caller to pick one, since a stale-branch *report* wants the fuller picture.
_LANDED_REF_CANDIDATES = ("dev", "main")

# Branches that are never a "remote branch" in the sense this report cares
# about — the staging/protected set (worktree-reaping-design.md §1) plus the
# `home` parking branch (#454). Intentionally a small local duplicate of
# agent_branch_namespace.py's own protected set (that module classifies
# these `False` for a different reason — "never auto-touch" — not "exclude
# from a report"), so a change to one does not silently desync the other;
# if the canonical protected set ever grows, update both.
_EXCLUDED_NAMES = frozenset({"main", "dev", "home", "HEAD"})
_EXCLUDED_PATTERN = re.compile(r"^version/")


def _is_excluded(short_name: str) -> bool:
    return short_name in _EXCLUDED_NAMES or bool(_EXCLUDED_PATTERN.match(short_name))


class GitCommandError(RuntimeError):
    """A git command needed by the report failed unexpectedly."""


def _run_git(args: list, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run `git <args>`, raising GitCommandError on a non-zero exit."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitCommandError(
            "git %s failed (exit %d): %s"
            % (" ".join(args), result.returncode, result.stderr.strip())
        )
    return result


def list_remote_branches(remote: str = "origin", cwd: str | None = None) -> list:
    """Short names (e.g. 'claude/r4-449-worktree-reap-engine') of every branch
    on `remote`, via `git for-each-ref` (porcelain-safe, unlike the
    human-facing `git branch -r`). Excludes the remote's symbolic HEAD ref
    and the protected/staging set (see `_is_excluded`)."""
    prefix = "refs/remotes/%s/" % remote
    result = _run_git(["for-each-ref", "--format=%(refname:short)", prefix], cwd=cwd)
    names = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # The remote's symbolic HEAD (refs/remotes/<remote>/HEAD) collapses
        # to just "<remote>" under %(refname:short) — not "<remote>/HEAD" —
        # because git renders a symref's short name as its target's short
        # name one level up. Skip it explicitly rather than let it fall
        # through as a (bogus) branch literally named "<remote>".
        if line == remote or line == "%s/HEAD" % remote:
            continue
        short = line[len(remote) + 1:] if line.startswith(remote + "/") else line
        if _is_excluded(short):
            continue
        names.append(short)
    return sorted(names)


def _local_branch_exists(short_name: str, cwd: str | None = None) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/%s" % short_name],
        cwd=cwd, capture_output=True, text=True,
    )
    return result.returncode == 0


def _ref_exists(ref: str, cwd: str | None = None) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref],
        cwd=cwd, capture_output=True, text=True,
    )
    return result.returncode == 0


def _resolve_landed_refs(remote: str, cwd: str | None = None) -> list:
    """Resolve 'dev'/'main' to whatever ref actually names them here: the
    local branch if one exists (the authoritative tip in the canonical
    checkout), else that name's remote-tracking counterpart. A name with
    neither is skipped — this report never invents a target."""
    resolved = []
    for name in _LANDED_REF_CANDIDATES:
        if _ref_exists("refs/heads/%s" % name, cwd=cwd):
            resolved.append((name, name))
        elif _ref_exists("refs/remotes/%s/%s" % (remote, name), cwd=cwd):
            resolved.append((name, "%s/%s" % (remote, name)))
    return resolved


def _last_commit_epoch(remote_ref: str, cwd: str | None = None) -> int:
    """Unix timestamp (committer date) of `remote_ref`'s tip commit."""
    result = _run_git(["log", "-1", "--format=%ct", remote_ref], cwd=cwd)
    return int(result.stdout.strip())


def _is_ancestor(ref: str, landed_ref: str, cwd: str | None = None) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ref, landed_ref],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise GitCommandError(
            "git merge-base --is-ancestor %s %s failed unexpectedly (exit %d): %s"
            % (ref, landed_ref, result.returncode, result.stderr.strip())
        )
    return result.returncode == 0


def classify_remote_branch(short_name: str, remote: str, min_age_days: float,
                            landed_refs: list, now_epoch: float,
                            cwd: str | None = None) -> dict:
    """Classify one remote branch. Pure read — no git writes. Returns a dict
    consumable directly as a JSON report row (see module docstring's #326
    integration point)."""
    remote_ref = "%s/%s" % (remote, short_name)
    agent_branch = is_agent_branch(short_name)
    has_local = _local_branch_exists(short_name, cwd=cwd)

    last_commit_epoch = _last_commit_epoch(remote_ref, cwd=cwd)
    age_days = (now_epoch - last_commit_epoch) / 86400.0
    is_old = age_days >= min_age_days

    merged_into = [
        label for label, ref in landed_refs
        if _is_ancestor(remote_ref, ref, cwd=cwd)
    ]

    agent_reap_candidate = agent_branch and bool(merged_into) and not has_local

    return {
        "remote_ref": remote_ref,
        "short_name": short_name,
        "is_agent_branch": agent_branch,
        "has_local_counterpart": has_local,
        "last_commit_epoch": last_commit_epoch,
        "age_days": round(age_days, 1),
        "merged_into": merged_into,
        "is_old": is_old,
        "agent_reap_candidate": agent_reap_candidate,
    }


def generate_report(remote: str = "origin", min_age_days: float = 30,
                     cwd: str | None = None, now_epoch: float | None = None) -> dict:
    """Build the full stale-remote-branch report. Plain function, no CLI/
    printing side effects — this is what #326's fleet audit (and anything
    else that wants this data) should import and call directly rather than
    shelling out to this file's CLI. Never mutates the repository."""
    if now_epoch is None:
        now_epoch = time.time()

    landed_refs = _resolve_landed_refs(remote, cwd=cwd)
    short_names = list_remote_branches(remote, cwd=cwd)

    branches = [
        classify_remote_branch(name, remote, min_age_days, landed_refs, now_epoch, cwd=cwd)
        for name in short_names
    ]

    candidates = [b for b in branches if b["agent_reap_candidate"]]
    old_branches = [b for b in branches if b["is_old"]]

    return {
        "remote": remote,
        "min_age_days": min_age_days,
        "landed_refs_checked": [label for label, _ref in landed_refs],
        "generated_at_epoch": now_epoch,
        "branches": branches,
        "candidates": candidates,
        "old_branches": old_branches,
    }


def _format_text(report: dict) -> str:
    lines = []
    lines.append(
        "stale-remote-report: remote=%s min-age-days=%s landed-refs=%s"
        % (report["remote"], report["min_age_days"],
           ",".join(report["landed_refs_checked"]) or "(none resolved)")
    )
    lines.append("%d remote branch(es) considered." % len(report["branches"]))
    lines.append("")

    lines.append("Agent-branch reap candidates (merged, no local copy — "
                  "report only, never deleted here):")
    if report["candidates"]:
        for b in report["candidates"]:
            lines.append(
                "  CANDIDATE  %s  age=%.1fd  merged-into=%s"
                % (b["remote_ref"], b["age_days"], ",".join(b["merged_into"]))
            )
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Old/inactive branches (age >= %s day(s), any provenance/merge "
                  "status — needs human judgement):" % report["min_age_days"])
    if report["old_branches"]:
        for b in report["old_branches"]:
            provenance = "agent" if b["is_agent_branch"] else "human"
            merged = ",".join(b["merged_into"]) if b["merged_into"] else "(unmerged)"
            local = "has local branch" if b["has_local_counterpart"] else "no local branch"
            lines.append(
                "  OLD  %s  age=%.1fd  %s  merged-into=%s  %s"
                % (b["remote_ref"], b["age_days"], provenance, merged, local)
            )
    else:
        lines.append("  (none)")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Self-test — hermetic fixture repo under a temp dir; never touches this (or
# any real) repository's actual branches. Confirms generate_report() makes
# zero mutating git calls by fingerprinting refs before/after.
# --------------------------------------------------------------------------

def _git_ok(args: list, cwd: str) -> None:
    _run_git(args, cwd=cwd)


def _configure_identity(repo: str) -> None:
    _git_ok(["config", "user.email", "stale-remote-selftest@example.com"], repo)
    _git_ok(["config", "user.name", "Stale Remote Selftest"], repo)


def _commit_file(repo: str, name: str, content: str, message: str,
                  age_days: float | None = None) -> None:
    """Commit `name` with `content` appended. When `age_days` is given, both
    author and committer dates are backdated so the self-test can manufacture
    an old commit without waiting real time."""
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
        raise GitCommandError("git commit failed: %s" % result.stderr.strip())


def _make_repo_with_origin(base_dir: str) -> str:
    origin = os.path.join(base_dir, "origin.git")
    _git_ok(["init", "--bare", "-b", "main", origin], base_dir)
    clone = os.path.join(base_dir, "clone")
    _git_ok(["clone", origin, clone], base_dir)
    _configure_identity(clone)
    _commit_file(clone, "README.md", "hello\n", "initial commit")
    _git_ok(["push", "origin", "main"], clone)
    return clone


def _repo_fingerprint(cwd: str) -> tuple:
    """A stable snapshot of every ref (local + remote-tracking) and their
    SHAs, plus the current HEAD. Used to prove generate_report() performed
    zero mutating git calls."""
    refs = _run_git(["for-each-ref", "--format=%(refname) %(objectname)"], cwd=cwd).stdout
    head = _run_git(["rev-parse", "HEAD"], cwd=cwd).stdout
    status = _run_git(["status", "--porcelain"], cwd=cwd).stdout
    return (refs, head, status)


def _self_test() -> int:
    import tempfile

    failures = []

    with tempfile.TemporaryDirectory() as base:
        clone = _make_repo_with_origin(base)
        now_epoch = time.time()

        # --- Case 1: stale agent-branch candidate --------------------------
        # Agent-namespaced, old commit, merged into main, pushed, then the
        # local branch is removed so only the remote copy remains.
        _git_ok(["switch", "-c", "claude/r4-999-stale-fixture"], clone)
        _commit_file(clone, "stale.txt", "x\n", "old agent work", age_days=45)
        _git_ok(["push", "origin", "claude/r4-999-stale-fixture"], clone)
        _git_ok(["switch", "main"], clone)
        _git_ok(["merge", "--no-ff", "claude/r4-999-stale-fixture"], clone)
        _git_ok(["push", "origin", "main"], clone)
        _git_ok(["branch", "-D", "claude/r4-999-stale-fixture"], clone)

        # --- Case 2: fresh, unmerged agent branch ---------------------------
        # Recent commit, never merged into main — must NOT be a candidate and
        # must NOT be flagged old.
        _git_ok(["switch", "-c", "claude/r4-999-fresh-fixture"], clone)
        _commit_file(clone, "fresh.txt", "y\n", "fresh agent work")
        _git_ok(["push", "origin", "claude/r4-999-fresh-fixture"], clone)
        _git_ok(["switch", "main"], clone)
        _git_ok(["branch", "-D", "claude/r4-999-fresh-fixture"], clone)

        # --- Case 3: old human-created branch (outside #456 namespace) -----
        # Old + merged, but its name is not agent-namespaced — must be
        # listed, may be flagged old, but must NEVER be an agent_reap_candidate.
        _git_ok(["switch", "-c", "robs-old-feature"], clone)
        _commit_file(clone, "human.txt", "z\n", "human work", age_days=60)
        _git_ok(["push", "origin", "robs-old-feature"], clone)
        _git_ok(["switch", "main"], clone)
        _git_ok(["merge", "--no-ff", "robs-old-feature"], clone)
        _git_ok(["push", "origin", "main"], clone)
        _git_ok(["branch", "-D", "robs-old-feature"], clone)

        # --- Zero-git-mutation check + actual report generation ------------
        before = _repo_fingerprint(clone)
        report = generate_report(remote="origin", min_age_days=30, cwd=clone,
                                  now_epoch=now_epoch)
        after = _repo_fingerprint(clone)
        if before != after:
            failures.append(
                "generate_report() mutated repo state: before=%r after=%r" % (before, after)
            )

        by_name = {b["short_name"]: b for b in report["branches"]}

        # Case 1 assertions
        stale = by_name.get("claude/r4-999-stale-fixture")
        if stale is None:
            failures.append("stale agent-branch fixture missing from report")
        else:
            if not stale["agent_reap_candidate"]:
                failures.append("stale, merged, agent-namespaced, no-local branch "
                                 "should be an agent_reap_candidate")
            if not stale["is_old"]:
                failures.append("45-day-old branch should be flagged is_old")
            if stale["has_local_counterpart"]:
                failures.append("stale fixture's local branch was deleted; "
                                 "has_local_counterpart should be False")
        if not any(b["short_name"] == "claude/r4-999-stale-fixture"
                   for b in report["candidates"]):
            failures.append("stale fixture should appear in report['candidates']")

        # Case 2 assertions
        fresh = by_name.get("claude/r4-999-fresh-fixture")
        if fresh is None:
            failures.append("fresh agent-branch fixture missing from report")
        else:
            if fresh["agent_reap_candidate"]:
                failures.append("unmerged branch must never be an agent_reap_candidate")
            if fresh["is_old"]:
                failures.append("freshly-committed branch must not be flagged is_old")
        if any(b["short_name"] == "claude/r4-999-fresh-fixture"
               for b in report["old_branches"]):
            failures.append("fresh fixture must not appear in report['old_branches']")

        # Case 3 assertions
        human = by_name.get("robs-old-feature")
        if human is None:
            failures.append("human-branch fixture missing from report")
        else:
            if human["is_agent_branch"]:
                failures.append("'robs-old-feature' must not classify as agent-created")
            if human["agent_reap_candidate"]:
                failures.append("human-created branch must never be an "
                                 "agent_reap_candidate, even if old + merged")
            if not human["is_old"]:
                failures.append("60-day-old human branch should still be flagged is_old "
                                 "(reported for human judgement, not filtered out)")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("stale_remote_report self-test: OK (agent-branch reap candidate, "
          "fresh/unmerged exclusion, human-branch non-candidate, zero-mutation check)")
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Report-only audit of remote branches for staleness (#455). Classifies "
            "each remote branch agent- vs. human-created (#456), its last-commit "
            "age, and its merge status against dev/main. NEVER deletes anything, "
            "NEVER calls a remote-mutating git command — remote deletion stays "
            "human-gated. Output is stdout only."
        )
    )
    ap.add_argument("--remote", default="origin",
                     help="Remote to inspect (default: origin).")
    ap.add_argument("--min-age-days", type=float, default=30,
                     help="Age threshold (in days, by last commit) for the "
                          "'old/inactive' flag (default: 30).")
    ap.add_argument("--format", choices=["text", "json"], default="text",
                     help="Output format (default: text).")
    ap.add_argument("--self-test", action="store_true",
                     help="Run the hermetic self-test suite (builds a temp fixture "
                          "repo; touches nothing in the real repository) and exit.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    try:
        report = generate_report(remote=args.remote, min_age_days=args.min_age_days)
    except GitCommandError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_text(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
