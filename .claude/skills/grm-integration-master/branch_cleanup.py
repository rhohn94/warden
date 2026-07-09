#!/usr/bin/env python3
"""branch_cleanup.py — classify local branches and select safe deletion (v1.30, #61).

Resolves the mid-campaign stall where `git branch -D` was blocked by the
auto-mode classifier: merged branches use the SAFE `git branch -d` (no data
loss); throwaway branches are listed as `-D` candidates for ONE batched human
confirmation — never auto-force-deleted.

Usage:
  branch_cleanup.py [--integration dev] [--apply]
    (default: dry-run — print the classification + plan)
    --apply : run only the safe `-d` deletions; print `-D` candidates to confirm
"""
import re, subprocess, sys

PROTECTED = {"main", "dev"}
PROTECTED_RE = re.compile(r"^(version/|release/)")
# Branches that are safe to force-delete once confirmed (throwaway agent work).
THROWAWAY_RE = re.compile(r"(^worktree-agent-|^worker-|^wf-|-[0-9a-f]{4,}$|^agent[-/])")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def branch_list():
    out = git("branch", "--format=%(refname:short)")
    return [b.strip() for b in out.stdout.splitlines() if b.strip()]


def merged_into(integration):
    out = git("branch", "--merged", integration, "--format=%(refname:short)")
    return {b.strip() for b in out.stdout.splitlines() if b.strip()}


def current():
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def classify(integration):
    cur = current()
    merged = merged_into(integration)
    safe_d, force_candidates, protected, unmerged = [], [], [], []
    for b in branch_list():
        if b in PROTECTED or PROTECTED_RE.match(b) or b == cur:
            protected.append(b)
        elif b in merged:
            safe_d.append(b)              # merged ⇒ safe `-d`
        elif THROWAWAY_RE.search(b):
            force_candidates.append(b)    # throwaway, unmerged ⇒ `-D` candidate
        else:
            unmerged.append(b)            # unknown + unmerged ⇒ never touch
    return cur, safe_d, force_candidates, protected, unmerged


def main():
    args = sys.argv[1:]
    integration = "dev"
    if "--integration" in args:
        integration = args[args.index("--integration") + 1]
    apply = "--apply" in args

    cur, safe_d, force_candidates, protected, unmerged = classify(integration)
    print(f"branch-cleanup (integration={integration}, HEAD={cur})\n")
    print(f"  safe `-d` (merged):       {safe_d or '—'}")
    print(f"  `-D` candidates (throwaway, UNMERGED — confirm): {force_candidates or '—'}")
    print(f"  protected (never touch):  {protected or '—'}")
    print(f"  unmerged (kept, review):  {unmerged or '—'}\n")

    if not apply:
        print("dry-run. Re-run with --apply to delete the safe `-d` set.")
        if force_candidates:
            print("`-D` candidates need ONE explicit human confirmation:")
            print("   git branch -D " + " ".join(force_candidates))
        return

    for b in safe_d:
        r = git("branch", "-d", b)
        print(("deleted " if r.returncode == 0 else "skip ") + b +
              ("" if r.returncode == 0 else f" ({r.stderr.strip()})"))
    if force_candidates:
        print("\nNOT auto-deleted (force). Confirm + run in ONE batch if intended:")
        print("   git branch -D " + " ".join(force_candidates))


if __name__ == "__main__":
    main()
