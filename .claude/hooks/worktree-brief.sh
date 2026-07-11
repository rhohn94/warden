#!/usr/bin/env python3
""":"
# Bash polyglot preamble — bash ignores the shebang and lands here.
# Re-execute with python3, or emit a clear error if unavailable.
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$0" "$@"
fi
printf 'error: %s requires python3. Re-run as: python3 %s %s\n' "$0" "$0" "$*" >&2
exit 1
":"""
"""Worktree brief (SessionStart) — automatic isolation context (v3.63).

Every session that starts inside an ISOLATED WORKTREE (a path under
/.claude/worktrees/) gets a compact, machine-generated brief on stdout:
role (task agent vs integration master, by marker), worktree root, current
HEAD, the staging ref to branch from, and the isolation rules — plus loud
warnings when the session already starts in a known-bad state:

  - HEAD sitting on a protected branch (dev / main / version/*) in an
    UNMARKED worktree: the agent must branch in place before touching files.
  - HEAD not rooted on dev (harness-spawned worktrees frequently check out
    at main's tip): the wrong-base incident behind "release-only diffs".

This is the mechanical, zero-effort delivery of the grm-worktree-preflight
check: the agent doesn't have to remember to run anything — the context
arrives with the session. Sessions in the canonical root (not a worktree)
get no output, so the cost is zero where the risk is lowest.

SessionStart hooks must NEVER block a session: every path exits 0, all git
calls are wrapped and time-limited. Companion enforcement (deny-side):
protected-branch-guard.sh (branch-hygiene rules), worktree-guard.sh.
See docs/grimoire/design/orchestrate-release-design.md §Guard hardening.
"""
import os
import subprocess
import sys

WORKTREES_SEGMENT = "/.claude/worktrees/"
PROTECTED = ("dev", "main")


def git(repo: str, *args: str) -> str | None:
    """Run a git query; return stripped stdout or None on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def staging_ref(repo: str) -> str:
    """Newest version/* branch if any, else dev — the ref to branch from."""
    listing = git(repo, "branch", "--list", "version/*",
                  "--format=%(refname:short)")
    if listing:
        def key(name: str):
            parts = name.split("/", 1)[1] if "/" in name else name
            try:
                return [int(p) for p in parts.split(".")]
            except ValueError:
                return [0]
        return sorted(listing.splitlines(), key=key)[-1]
    return "dev"


def is_protected(branch: str) -> bool:
    return branch in PROTECTED or branch.startswith("version/")


def main() -> None:
    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not proj:
        return
    try:
        root = os.path.realpath(proj)
    except OSError:
        root = os.path.normpath(proj)
    if WORKTREES_SEGMENT not in root:
        return  # canonical root session — no brief, no token cost

    marked = os.path.isfile(
        os.path.join(proj, ".claude", "integration-allow.local"))
    head = git(proj, "symbolic-ref", "--quiet", "--short", "HEAD") \
        or "(detached)"
    staging = staging_ref(proj)

    lines = ["[worktree-brief] Isolated-worktree session."]
    if marked:
        lines += [
            f"  role: INTEGRATION MASTER (marker present) · worktree: {root}",
            f"  HEAD: {head} · staging ref: {staging}",
            "  Before EVERY merge: verify `git symbolic-ref --short HEAD` "
            "equals the staging ref.",
        ]
    else:
        lines += [
            f"  role: TASK AGENT (no integration marker) · worktree: {root}",
            f"  HEAD: {head} · staging ref to branch from: {staging}",
            "  Rules: branch in place (`git switch -c <branch> "
            f"{staging}`); never checkout dev/main/version/*;",
            "  never branch off main; never `git worktree add`; never push; "
            "only the integration master merges.",
        ]
        if head != "(detached)" and is_protected(head):
            lines.append(
                f"  WARNING: HEAD is on protected '{head}'. Create your work "
                f"branch FIRST: git switch -c <branch> {staging}")

    dev_sha = git(proj, "rev-parse", "--verify", "--quiet", "dev")
    if dev_sha and head != "(detached)" and not is_protected(head):
        base = git(proj, "merge-base", "HEAD", "dev")
        if base and base != dev_sha:
            lines.append(
                "  WARNING: HEAD is not rooted on dev's tip (likely rooted on "
                "main — release-only\n  commits may be reachable). Run the "
                "grm-worktree-preflight skill before committing.")

    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a brief must never break session start
    sys.exit(0)
