#!/usr/bin/env python3
"""Protected-branch guard (deny-by-default).

Blocks history-mutating git operations (commit, merge, rebase,
cherry-pick, revert) when the active worktree's HEAD is on a PROTECTED
branch — `dev`, `main`, or any `version/*` staging branch — UNLESS
this worktree carries an explicit allow-marker:

    $CLAUDE_PROJECT_DIR/.claude/integration-allow.local

Rationale: protected-branch mutation is DENIED by default and allowed
ONLY where the operator has placed the marker (the blessed integration
worktree). A fresh agent / work-item worktree has no marker, so it fails
CLOSED — the safe direction. The hook script is tracked, so it is present
and active in every worktree (including agent worktrees); only the marker
is local and git-ignored (absence ⇒ deny, which is correct for an
allow-marker).

Work-item / `fix/*` branches are never protected, so normal
work is unaffected. Escape hatches `--abort` / `--quit` / `--skip` for
in-progress merge/rebase/cherry-pick are always allowed so a recovery is
never trapped. Detached HEAD / non-repo ⇒ no-op.

Operator escape hatch: to legitimately mutate a protected branch in a
worktree (e.g. the final `version/* → dev` handoff, or the
`dev → main` promotion), create the marker file in that worktree
deliberately:  touch .claude/integration-allow.local

Write-capable workflow agent safety contract (v1.6+)
=====================================================
Write-capable Workflow agents (Noir paradigm, isolation: 'worktree') each
run in their own isolated worktree. This guard enforces their confinement
automatically, with no additional configuration:

  AGENT worktrees (no marker):
    - May commit freely on their OWN per-agent feature branch (unprotected
      branch names like `<item>-<uuid>` or `nw2-guard-reconcile`).
    - Are DENIED any git commit / merge / rebase on dev / main / version/*.
    - This is enforced fail-closed: absence of the marker = denied on
      protected branches. No special agent mode required.
    - Multiple concurrent agent worktrees are each governed independently;
      they cannot interfere with each other's branches nor with staging.

  MASTER worktree (has marker):
    - Is the ONLY worktree permitted to merge agent branches into the
      staging ref (version/<X.Y>) or promote dev → main.
    - Placing the marker is a deliberate operator action (touch .claude/
      integration-allow.local); it is never automatic.

  This binary model (marker = allowed, no marker = denied on protected
  branches) was proven correct by the v1.5 vet (wf_84d9bd9b-704) and is
  the foundation for the v1.6 isolated-worktree parallel execution model.
  See docs/design/write-capable-workflow-design.md §3 (Safety rails) and
  §5.3 (Guard model implications).

Cross-worktree branch hijack guard (v1.7, item A3)
==================================================
The v1.6 vet found a spawned agent's `git switch -c` / worktree-preflight
ran against the INTEGRATION MASTER's worktree (not the agent's own),
silently switching the master off version/{X.Y} onto a new branch. The
checks above only inspect the ACTOR's own worktree ($CLAUDE_PROJECT_DIR),
so a git command that *targets another worktree* — via `git -C <path>`,
`--git-dir` / `--work-tree`, or a `cd`/`pushd` into a sibling first —
bypassed them. This guard closes that hole:

  - It also recognises branch-pointer ops (`switch` / `checkout` /
    `branch`), not just history-mutation, because those are what relocated
    the master's HEAD.
  - When an UNMARKED actor aims any such op at a DIFFERENT worktree (one
    that physically carries the integration-allow marker, or any other
    isolated-worktree path that is not the actor's own), it REFUSES —
    loudly, with the offending target named. The actor must branch in
    place inside its own worktree from the staging ref.
  - It never regresses the marker-blessed master operating on ITS OWN
    worktree (no `-C`/`--git-dir` redirect, or one that points back at
    itself): that remains allowed.

Master HEAD-drift guard (v1.19, item #35)
=========================================
The v1.14→v1.18 campaign hit a complementary failure the checks above did
NOT catch: an `Agent` isolation:'worktree' dispatch occasionally degraded
to IN-PLACE execution inside the master's own (marked) worktree. Its
`git switch -c <work-branch>` is a branch-pointer op (not history-mutation)
that silently relocated the MASTER's HEAD onto the work-item branch; every
later `git merge`/`git commit` then landed on that UNPROTECTED branch — all
allowed, because the marked master is trusted and the branch is not
protected. The staging branch never advanced and the release shipped empty
(v1.15 incident; recovery required reset --hard of dev/main + retag).

This guard closes the hole with the SYMMETRIC COMPLEMENT of the
deny-unmarked-on-protected rule:

  MARKED master (has marker):
    - May mutate history ONLY while HEAD is on a STAGING branch
      (dev / main / version/*).
    - A history-mutating op while HEAD is on an UNPROTECTED branch is
      REFUSED (exit 2) — it means HEAD drifted off staging (silent
      isolation failure). The master must repair HEAD before proceeding.

The two rules together make the (actor, branch-class) model TOTAL:
  - unmarked + protected   -> deny   (existing)
  - unmarked + unprotected -> allow  (agent's own work branch)
  - marked   + protected   -> allow  (the integration master at work)
  - marked   + unprotected -> deny   (NEW: master HEAD-drift)
See docs/design/dispatch-hardening-design.md §3.1.

History-rewrite deny rule (v3.15, item #84)
===========================================
Branch-and-merge is the git default; commands that REWRITE shared history are
prohibited by default and permitted only as an explicit, human-confirmed last
resort. Beyond the (actor, branch-class) commit/merge model above, this guard
BLOCKS the following on/affecting a PROTECTED branch (dev / main / version/*),
fail-closed, for EVERY actor (marked master included) — stricter than the
commit/merge model, because rewriting shared history is never routine:

  - `git rebase`       (re-authors commits onto a new base)
  - `git cherry-pick`  (duplicates a commit under a new SHA)
  - `git reset --hard` (discards committed history / working state)

The rule fires when the actor's CURRENT HEAD is a protected branch — these
commands rewrite the checked-out branch, so HEAD-on-protected is the precise,
false-positive-free gate (work-item branches are deliberately left alone, per
#84). The merge/rebase/cherry-pick escape hatches `--abort` / `--quit` /
`--skip` stay allowed so an in-progress operation can always be unwound. A
soft/mixed `git reset` (no `--hard`) is NOT blocked — it discards no committed
history. Force-push and remote-ref deletion are the push-side complement and
remain owned by `push-guard.sh` (DENIED_FLAGS); the two guards together cover
the full #84 prohibited set. This rule only ADDS denials on protected branches
— it never widens what is allowed, so the blast radius is unchanged.
See docs/design/git-protocol-governance-design.md §3a.

Test/check note: to verify the contract holds for concurrent agent
worktrees, confirm that:
  1. A new worktree created without the marker cannot `git commit` or
     `git merge` on dev / main / version/* (this hook exits 2).
  2. A worktree with the marker can commit and merge on those branches.
  3. An UNMARKED worktree can commit freely on its own unprotected branch
     (no-op when HEAD is not protected).
  4. A MARKED worktree (the master) CANNOT commit/merge on an unprotected
     branch — that is the HEAD-drift guard firing (exit 2).

Multiple marked lane worktrees (v3.1, Project Manager)
======================================================
Under a Project Manager, a multi-feature release runs several integration
masters in PARALLEL — one per lane, each in its own marked worktree on a lane
branch `version/{X.Y}/<lane>`, plus the PM worktree. No guard code changes:
lane branches match `version/.*` (PROTECTED_RE), so each lane master is the
"marked + protected" allowed case on ITS OWN lane branch, and the cross-worktree
hijack guard refuses any op a lane master aims at a SIBLING lane's worktree. The
(actor, branch-class) model above already covers N marked worktrees. To verify:
  5. Two MARKED lane worktrees, each on its own `version/{X.Y}/<laneA|laneB>`,
     can each commit/merge on its own lane branch (marked + protected -> allow),
     and neither can mutate the other's lane via `git -C <sibling>` (the
     cross-worktree hijack guard exits 2).
  6. A MARKED lane worktree whose HEAD drifts off its lane branch onto an
     unprotected work-item branch CANNOT mutate history (HEAD-drift guard, #4).
See docs/design/project-manager-role-design.md §7.
"""
import json
import os
import re
import shlex
import subprocess
import sys

MUTATING = {"commit", "merge", "rebase", "cherry-pick", "revert"}
# History-REWRITING subcommands prohibited by default on protected branches
# (v3.15, #84). `rebase`/`cherry-pick` re-author commits; `reset --hard`
# discards committed history. Denied on protected branches for EVERY actor;
# escape hatches (--abort/--quit/--skip) and soft/mixed resets are exempt.
REWRITE_OPS = {"rebase", "cherry-pick"}
RESET_OP = "reset"
HARD_RESET_FLAG = "--hard"
# Branch-pointer ops that can RELOCATE a worktree's HEAD. These are not
# history-mutating, but the v1.6 vet showed they are exactly what hijacked
# the integration master's worktree, so the cross-worktree guard covers them.
BRANCH_OPS = {"switch", "checkout", "branch", "worktree"}
OPTS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "-c"}
# Options that redirect a git command at a DIFFERENT working tree/repo than
# the process cwd. Presence of any of these (or a `cd`/`pushd` prefix) means
# the command may not be acting on the actor's own worktree.
REDIRECT_OPTS = {"-C", "--git-dir", "--work-tree"}
ESCAPE_HATCH = {"--abort", "--quit", "--skip"}
PROTECTED_RE = re.compile(r"^(dev|main|version/.*)$")
WORKTREES_SEGMENT = "/.claude/worktrees/"


def find_mutating_subcommand(cmd: str) -> str | None:
    try:
        tokens = shlex.split(cmd, comments=False, posix=True)
    except ValueError:
        m = re.search(
            r"\bgit\b[^|;&\n]*?\b(commit|merge|rebase|cherry-pick|revert)\b",
            cmd,
        )
        return m.group(1) if m else None

    i, n = 0, len(tokens)
    while i < n:
        if tokens[i] == "git" or tokens[i].endswith("/git"):
            j = i + 1
            while j < n:
                t = tokens[j]
                if t in OPTS_WITH_VALUE:
                    j += 2
                    continue
                if t.startswith("-"):
                    j += 1
                    continue
                if t in MUTATING:
                    if any(a in ESCAPE_HATCH for a in tokens[j + 1:]):
                        break
                    return t
                break
            i = j
        i += 1
    return None


def find_rewrite_op(cmd: str) -> str | None:
    """Return the history-rewriting subcommand to block, or None (v3.15, #84).

    Detects `git rebase`, `git cherry-pick`, and `git reset --hard`, mirroring
    `find_mutating_subcommand`'s shlex token-walk so an option value is never
    mistaken for the subcommand. Returns:
      - "rebase" / "cherry-pick" when that subcommand is present and NOT an
        in-progress escape hatch (--abort / --quit / --skip);
      - "reset --hard" when a `reset` carries the `--hard` flag (a soft/mixed
        reset, which discards no committed history, returns None);
      - None otherwise.
    Only the first matching git invocation is reported (consistent with the
    other detectors). Used to deny these on protected branches for every actor.
    """
    try:
        tokens = shlex.split(cmd, comments=False, posix=True)
    except ValueError:
        m = re.search(r"\bgit\b[^|;&\n]*?\b(rebase|cherry-pick)\b", cmd)
        if m and not any(h in cmd for h in ESCAPE_HATCH):
            return m.group(1)
        if re.search(r"\bgit\b[^|;&\n]*?\breset\b", cmd) and HARD_RESET_FLAG in cmd:
            return "reset --hard"
        return None

    i, n = 0, len(tokens)
    while i < n:
        if tokens[i] == "git" or tokens[i].endswith("/git"):
            j = i + 1
            while j < n:
                t = tokens[j]
                if t in OPTS_WITH_VALUE:
                    j += 2
                    continue
                if t.startswith("-"):
                    j += 1
                    continue
                if t in REWRITE_OPS:
                    if any(a in ESCAPE_HATCH for a in tokens[j + 1:]):
                        break
                    return t
                if t == RESET_OP:
                    if HARD_RESET_FLAG in tokens[j + 1:]:
                        return "reset --hard"
                    break
                break
            i = j
        i += 1
    return None


def find_redirect_targets(cmd: str) -> list[str]:
    """Return absolute paths the command would act on OTHER than the cwd.

    Covers two redirection vectors the v1.6 hijack could have used:
      1. git's own redirect options: `-C <path>`, `--git-dir <path>`,
         `--work-tree <path>` (also the `--opt=value` form).
      2. a leading `cd <path>` / `pushd <path>` that moves into another
         worktree before invoking git.
    Only absolute paths are considered (relative paths cannot escape the
    actor's own worktree in a way this guard needs to reason about, and the
    worktree-guard hook already gates absolute-path tokens generically).
    """
    targets: list[str] = []
    try:
        tokens = shlex.split(cmd, comments=False, posix=True)
    except ValueError:
        return targets

    n = len(tokens)
    for idx, t in enumerate(tokens):
        # cd / pushd into another directory before running git
        if t in ("cd", "pushd") and idx + 1 < n:
            nxt = tokens[idx + 1]
            if nxt.startswith("/"):
                targets.append(nxt)
        # git redirect options, split form: `-C /path`
        if t in REDIRECT_OPTS and idx + 1 < n:
            val = tokens[idx + 1]
            if val.startswith("/"):
                targets.append(val)
        # git redirect options, joined form: `--git-dir=/path`
        if t.startswith("--") and "=" in t:
            opt, _, val = t.partition("=")
            if opt in REDIRECT_OPTS and val.startswith("/"):
                targets.append(val)
        if t.startswith("-C") and len(t) > 2 and t[2] == "/":
            targets.append(t[2:])  # `-C/path` joined form
    return targets


def find_branch_op(cmd: str) -> str | None:
    """Return the first branch-pointer subcommand (switch/checkout/branch/
    worktree) in the command, mirroring find_mutating_subcommand's parser so
    option values are not mistaken for the subcommand."""
    try:
        tokens = shlex.split(cmd, comments=False, posix=True)
    except ValueError:
        m = re.search(
            r"\bgit\b[^|;&\n]*?\b(switch|checkout|branch|worktree)\b", cmd
        )
        return m.group(1) if m else None

    i, n = 0, len(tokens)
    while i < n:
        if tokens[i] == "git" or tokens[i].endswith("/git"):
            j = i + 1
            while j < n:
                t = tokens[j]
                if t in OPTS_WITH_VALUE:
                    j += 2
                    continue
                if t.startswith("-"):
                    j += 1
                    continue
                if t in BRANCH_OPS:
                    return t
                break
            i = j
        i += 1
    return None


def worktree_of(path: str) -> str | None:
    """If `path` resolves to (or under) an isolated worktree, return that
    worktree's root; else None. The worktree root is the path up to and
    including the first segment after /.claude/worktrees/."""
    try:
        rp = os.path.realpath(path)
    except OSError:
        rp = os.path.normpath(path)
    if WORKTREES_SEGMENT not in rp:
        return None
    head, _, tail = rp.partition(WORKTREES_SEGMENT)
    first = tail.split("/", 1)[0]
    if not first:
        return None
    return head + WORKTREES_SEGMENT + first


def current_branch(repo: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None  # detached HEAD / not a repo — no-op
    return out.stdout.strip() or None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if payload.get("tool_name", "") != "Bash":
        sys.exit(0)

    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not proj:
        sys.exit(0)

    cmd = (payload.get("tool_input", {}) or {}).get("command", "") or ""

    # ---- Cross-worktree hijack guard (v1.7 A3) -------------------------
    # Refuse, from an UNMARKED actor, any git op that targets a DIFFERENT
    # worktree — the vector behind the v1.6 integration-master hijack. This
    # fires for branch-pointer ops too (switch/checkout/branch/worktree),
    # not only history-mutation, since those are what relocated HEAD.
    actor_marked = os.path.isfile(
        os.path.join(proj, ".claude", "integration-allow.local")
    )
    try:
        actor_wt = os.path.realpath(proj)
    except OSError:
        actor_wt = os.path.normpath(proj)
    if not actor_marked and (find_mutating_subcommand(cmd) or find_branch_op(cmd)):
        for tgt in find_redirect_targets(cmd):
            tgt_wt = worktree_of(tgt)
            # A redirect into a different worktree (or into a marked
            # integration worktree) by an unmarked actor is the hijack
            # pattern. A redirect that points back at the actor's own
            # worktree is harmless and stays allowed.
            if tgt_wt and tgt_wt != actor_wt:
                tgt_marked = os.path.isfile(
                    os.path.join(tgt_wt, ".claude", "integration-allow.local")
                )
                label = (
                    "the integration master's worktree"
                    if tgt_marked else "a sibling worktree"
                )
                sys.stderr.write(
                    "protected-branch-guard: blocked a git operation that "
                    f"targets {label}.\n"
                    f"  actor worktree:  {actor_wt}\n"
                    f"  target worktree: {tgt_wt}\n"
                    "A spawned/work-item agent must stay in its OWN worktree "
                    "and branch in place\nfrom the staging ref:\n"
                    "  git switch -c <branch> version/<X.Y>\n"
                    "Never `git -C`, `--git-dir`/`--work-tree`, or `cd` into "
                    "another worktree to\nswitch/create a branch there. "
                    "(See docs/integration-workflow.md, worktree isolation.)\n"
                )
                sys.exit(2)
    # --------------------------------------------------------------------

    # ---- History-rewrite deny rule (v3.15, #84) ------------------------
    # Prohibit history-REWRITING commands on a protected branch for EVERY
    # actor (marked master included) — stricter than the commit/merge model,
    # because rewriting shared history is a last-resort, not a routine op.
    # Fires only when HEAD is a protected branch (these commands rewrite the
    # checked-out branch); work-item branches are left alone, per #84. Escape
    # hatches (--abort/--quit/--skip) and soft/mixed resets are exempt inside
    # find_rewrite_op(). Force-push is the push-side complement, owned by
    # push-guard.sh. See docs/design/git-protocol-governance-design.md §3a.
    rewrite = find_rewrite_op(cmd)
    if rewrite:
        rcur = current_branch(proj)
        if rcur is not None and PROTECTED_RE.match(rcur):
            sys.stderr.write(
                f"protected-branch-guard: blocked `git {rewrite}` on protected "
                f"branch '{rcur}'.\n"
                f"  worktree: {proj}\n"
                "History-rewriting commands (git rebase / git cherry-pick /\n"
                "git reset --hard) are PROHIBITED on dev / main / version/* —\n"
                "they rewrite shared history. Branch-and-merge is the default:\n"
                "  git switch -c <work-branch> " + rcur + "   # then merge --no-ff\n"
                "This is a last-resort op; if you truly must rewrite history\n"
                "here, a human must run it deliberately outside the agent.\n"
                "(In-progress recovery — --abort / --quit / --skip — is "
                "allowed.)\n"
            )
            sys.exit(2)
    # --------------------------------------------------------------------

    sub = find_mutating_subcommand(cmd)
    if not sub:
        sys.exit(0)

    cur = current_branch(proj)
    if cur is None:
        sys.exit(0)  # detached HEAD / not a repo — no-op

    if not PROTECTED_RE.match(cur):
        # ---- Master HEAD-drift guard (v1.19, item #35) -----------------
        # The marker-blessed integration master must mutate history ONLY
        # while its HEAD is on a STAGING branch (dev / main / version/*).
        # A history-mutating op from the MARKED worktree while HEAD is on
        # an UNPROTECTED branch means the master's HEAD drifted onto a
        # work-item branch — the v1.15 silent-isolation incident, where an
        # Agent isolation:'worktree' dispatch ran in-place, its
        # `git switch -c <work-branch>` relocated the master's HEAD, and
        # every later merge/commit piled onto the stray branch while the
        # staging branch never advanced (shipping an empty release). This
        # is the SYMMETRIC COMPLEMENT of the unmarked-on-protected deny
        # below; together they make the (actor, branch-class) model total.
        # Fail closed so the master detects the drift and repairs HEAD.
        if actor_marked:
            sys.stderr.write(
                f"protected-branch-guard: blocked `git {sub}` — the "
                "integration master's HEAD has DRIFTED off the staging "
                "branch.\n"
                f"  worktree:     {actor_wt}\n"
                f"  current HEAD: '{cur}' (not dev / main / version/*)\n"
                "The marker-blessed master may mutate history ONLY on a "
                "staging branch.\nHEAD on a work-item branch is the silent "
                "worktree-isolation failure\n(see "
                "docs/design/dispatch-hardening-design.md): a dispatched "
                "Agent likely ran\nin-place instead of in its own worktree. "
                "DO NOT merge — repair first:\n"
                "  1. Identify the intended staging branch (version/<X.Y>).\n"
                "  2. If this branch holds stranded work, re-point staging "
                "at it:\n"
                "       git branch -f version/<X.Y> " + cur + "\n"
                "  3. git switch version/<X.Y>   # restore HEAD\n"
                "  4. Re-verify: git symbolic-ref --short HEAD\n"
                "Then re-run the merge. (Recovery playbook: "
                "integration-workflow.md.)\n"
            )
            sys.exit(2)
        sys.exit(0)  # unmarked agent on its own work branch — allowed

    # HEAD is on a protected staging branch (dev / main / version/*):
    if actor_marked:
        sys.exit(0)  # blessed worktree — allowed

    sys.stderr.write(
        f"protected-branch-guard: blocked `git {sub}` on protected branch "
        f"'{cur}'.\n"
        f"  worktree: {proj}\n"
        "This worktree has no .claude/integration-allow.local marker, so it\n"
        "is NOT the blessed integration worktree. History-mutating git ops\n"
        "on dev / main / version/* are denied here (deny-by-default).\n"
        "Branch your work in place instead:\n"
        "  git switch -c <work-branch> " + cur + "\n"
        "Operator override (only if you truly mean to mutate this protected\n"
        "branch here): touch .claude/integration-allow.local\n"
    )
    sys.exit(2)


def _self_test() -> int:
    """Parser-level self-test for the v3.15 (#84) history-rewrite detector.

    No git / marker / protected-branch needed — exercises find_rewrite_op()
    against a table of commands (the protected-branch gating is applied in
    main() once HEAD is known). Run with --self-test; returns process exit
    code (0 = all pass), mirroring push-guard.sh's self-test pattern."""
    # (command, expected find_rewrite_op result)
    cases = [
        ("git rebase dev", "rebase"),
        ("git rebase --onto dev old HEAD", "rebase"),
        ("git cherry-pick abc123", "cherry-pick"),
        ("git reset --hard dev", "reset --hard"),
        ("git reset --hard HEAD~1", "reset --hard"),
        ("git -C /repo reset --hard origin/main", "reset --hard"),
        # Escape hatches — recovery must never be trapped.
        ("git rebase --abort", None),
        ("git rebase --quit", None),
        ("git cherry-pick --skip", None),
        ("git cherry-pick --abort", None),
        # Soft / mixed reset discards no committed history — not blocked.
        ("git reset HEAD~1", None),
        ("git reset --soft HEAD~1", None),
        ("git reset --mixed HEAD", None),
        ("git reset --keep dev", None),
        # Non-rewrite git ops and look-alikes.
        ("git merge --no-ff feature", None),
        ("git commit -m 'rebase the docs'", None),
        ("git switch -c work dev", None),
        ('echo "git rebase dev"', None),  # rebase only inside a quoted arg — not a git call
        ("git status", None),
        # `-c key=val` global option before the subcommand must not confuse it.
        ("git -c rerere.enabled=true rebase dev", "rebase"),
    ]
    failures = 0
    for cmd, expected in cases:
        got = find_rewrite_op(cmd)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {cmd!r} -> {got!r} (want {expected!r})")
    # Constant-set sanity: rebase/cherry-pick recognised, reset handled apart.
    assert REWRITE_OPS == {"rebase", "cherry-pick"}, "REWRITE_OPS intact"
    assert RESET_OP == "reset" and HARD_RESET_FLAG == "--hard", "reset consts intact"
    print(f"\n{'PASS' if not failures else str(failures)+' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    main()
