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
# HOOK_CONTRACT: v1 capabilities=[protected-branch-block,branch-hygiene-block,history-rewrite-block,cross-worktree-hijack-block,master-head-drift-block,release-boundary-guard,worktree-cleanup-allow]
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

Direct-commit-to-`main` is ALREADY covered (BMI-4, v3.38, issue #126)
====================================================================
The (actor, branch-class) model below is TOTAL. In particular, an
UNMARKED actor's `git commit` or `git merge` on `main` is DENIED by the
deny-by-default path at the bottom of main() — `commit` ∈ MUTATING and
`main` ∈ PROTECTED_RE, so the hook exits 2. This guards the exact failure
mode in issue #126 (a manual release and a scaffolding sync committed
straight to `main` without an integration-master marker). The framing in
#126 — "the guard handles pushes" — is incomplete: the guard also handles
COMMITS.

Known residual (documented, deferred in v3.38 — CLOSED in v3.64, #214): a
MARKED integration master could previously commit to `main` at any time,
including mid-release out of a clean promotion boundary. The divergence
guard (BMI-2, in the release-planning engine's divergence-check) and process
discipline remain the boundary enforcement at the release-planning layer;
this hook now ALSO enforces it mechanically at commit time — see "Clean
release-boundary guard" below.

Clean release-boundary guard (v3.64, #214)
===========================================
The `marked + protected -> allow` cell is TOTAL for every protected branch
except `main`, where it is now conditional: a MARKED actor's `commit` /
`merge` while HEAD is `main` is allowed ONLY when a clean release boundary
holds, closing the residual above (related to issue #126 — a manual release
and a scaffolding sync committed straight to `main` outside any promotion
flow). A boundary is clean when ANY of:

  1. **Tree-content identity** — `git diff --quiet dev main` exits 0 (the
     same BMI-2 predicate from `integration-branch-integrity-design.md` §2):
     `dev` is already fully promoted into `main`, so this commit is landing
     immediately after (or as a no-op continuation of) a clean promotion.
  2. **This IS the promotion merge** — the invocation is `git merge <dev-ref>`
     (or `git merge --no-ff dev`, `origin/dev`, etc.) while HEAD is `main`:
     the tree-identity check in (1) cannot yet be true (that is the whole
     point of running the merge), so the merge command itself is recognized
     as the legitimate promotion step.
  3. **Explicit release-in-progress marker** — the file
     `$CLAUDE_PROJECT_DIR/.claude/release-in-progress.local` exists, mirroring
     the `.claude/integration-allow.local` convention: a deliberate, local-only
     marker the `grm-project-release` skill's promote step creates at the
     start of the promotion window (right before `git switch main; git merge
     dev`) and removes at the end (right after tagging). This covers every
     other legitimate commit inside that window — e.g. a version-bump commit
     landing on `main` itself — without the guard needing to model every
     possible promotion-tooling shape.

Failing all three denies the commit/merge with a remediation message pointing
at `grm-project-release` — this is exactly the #126 failure mode (an ad-hoc
direct edit-and-commit to `main` outside any release flow). Design rationale
lives in the upstream Grimoire repository (framework-internal — not shipped).

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
  Safety-rails and guard-model rationale (§3, §5.3) are framework-internal
  design specs — see the upstream Grimoire repository for that rationale.

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
Design rationale (§3.1) lives in the upstream Grimoire repository
(framework-internal — not shipped).

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
Design rationale (§3a) is a framework-internal design spec — see the upstream
Grimoire repository for that rationale.

Test/check note: to verify the contract holds for concurrent agent
worktrees, confirm that:
  1. A new worktree created without the marker cannot `git commit` or
     `git merge` on dev / main / version/* (this hook exits 2).
  2. A worktree with the marker can commit and merge on dev / version/*
     unconditionally, and on `main` when a clean release boundary holds
     (v3.64 #214 — see "Clean release-boundary guard" below).
  3. An UNMARKED worktree can commit freely on its own unprotected branch
     (no-op when HEAD is not protected).
  4. A MARKED worktree (the master) CANNOT commit/merge on an unprotected
     branch — that is the HEAD-drift guard firing (exit 2).

Branch-hygiene guard (v3.63)
============================
The rules above gate history-MUTATION; the incidents that kept recurring were
branch-POINTER misuse by unmarked task agents inside their OWN worktree, which
none of the earlier rules cover:

  - `git switch dev` / `git checkout main` — the agent relocates its HEAD onto
    a protected branch. Later commits are denied (deny-by-default above), but
    the working tree is now a staging checkout and the session derails.
  - `git switch -c <work> main` — branching off the WRONG BASE. `main` carries
    release-only commits (version bumps, dist artifacts); a work branch rooted
    there re-imports them into `dev` on merge (the "unexplained release-only
    diffs" incident class documented in grm-worktree-preflight).
  - `git branch version/X.Y` / `git switch -c version/X.Y` — an agent minting
    a protected-named branch it must never own.
  - `git branch -f/-D <protected>` — force-moving or deleting a protected
    branch pointer without the marker.
  - `git worktree add/remove/move/...` — an agent creating or removing sibling
    worktrees; only the marker-blessed master manages worktrees (dispatch and
    dead-worktree cleanup).

For an UNMARKED actor these are DENIED (exit 2) with a remediation message;
the MARKED integration master is exempt (it legitimately checks out staging
refs, repairs branch pointers, and manages worktrees). Read-only forms
(`git branch --list`, `git worktree list`, `git checkout <ref> -- <path>`,
`git switch --detach`) stay allowed. `pull` also joins MUTATING above: it
commits a merge (or fast-forwards a ref), so an unmarked `git pull` on a
protected branch is the same failure mode as an unmarked `git merge`.
Design rationale (§Guard hardening) lives in the upstream Grimoire repository
(framework-internal — not shipped).

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
Design rationale (§7) is a framework-internal design spec — see the upstream
Grimoire repository for that rationale.
"""
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_common import current_branch  # noqa: E402

MUTATING = {"commit", "merge", "rebase", "cherry-pick", "revert", "pull"}
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
# Branch-hygiene guard (v3.63) constants. WRONG_BASES: start-points an
# unmarked actor must never branch from (work branches root on the staging
# ref — version/{X.Y} or dev — never on main, which carries release-only
# commits). CREATE_FLAGS: switch/checkout flags that make the invocation a
# branch CREATION (name + optional start-point) rather than a checkout.
WRONG_BASES = {"main", "origin/main"}
CREATE_FLAGS = {"-c", "-C", "-b", "-B", "--create", "--force-create", "--orphan"}
WORKTREE_SAFE_VERBS = {"list"}
BRANCH_READONLY_FLAGS = {
    "--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv",
    "--show-current", "--contains", "--merged", "--no-merged", "--points-at",
}
STATEMENT_SEPS = {"&&", "||", ";", "|", "&"}
# Clean release-boundary guard (v3.64, #214) constants. INTEGRATION_LINE is
# the branch promoted into `main` (the BMI-2 divergence predicate's `$INT`,
# default-model value); RELEASE_MARKER_NAME mirrors the
# `.claude/integration-allow.local` convention for a deliberate, local-only,
# release-pipeline-owned marker. DEV_MERGE_REFS are the ref spellings a
# promotion merge legitimately names for `dev` (bare name, explicit
# refs/heads path, and the origin-tracking form).
INTEGRATION_LINE = "dev"
RELEASE_MARKER_NAME = "release-in-progress.local"
DEV_MERGE_REFS = {"dev", "refs/heads/dev", "origin/dev"}
# `git merge`'s OWN value-consuming flags (v3.64, #216 reviewer finding).
# Distinct from OPTS_WITH_VALUE (global options that precede the subcommand,
# e.g. `-C <path>`): these appear AFTER the `merge` token and, unlike a bare
# `--no-ff`-style flag, each consumes the NEXT token as its value rather than
# leaving it as the ref. Left unhandled, that value — e.g. a `-m` commit
# message — is misread as the merge source ref. Covers `-m`/`--message` (the
# reported bug: `git merge --no-ff -m "merge(release): ... into main" dev`)
# and `-X`/`--strategy-option`, both of which per `git merge -h` always take a
# value, space-separated or `=`-joined. `-S`/`--gpg-sign` is deliberately NOT
# here: per `git merge -h` it takes only an OPTIONAL GLUED value
# (`-S[<key-id>]` / `--gpg-sign[=<key-id>]`) and never consumes a following
# space-separated token — `git merge -S dev` really does mean "sign, and dev
# is the ref". The glued form (`-Sabc123`, `--gpg-sign=abc123`) is still
# handled below so it is never misread as the ref.
MERGE_OPTS_WITH_VALUE = {"-m", "--message", "-X", "--strategy-option"}


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


def _hygiene_check(sub: str, args: list[str]) -> tuple[str, str] | None:
    """Classify one git invocation's branch-hygiene violation (v3.63).

    `sub` is the git subcommand, `args` its argument tokens (this invocation
    only). Returns (kind, detail) or None. Kinds:
      - "checkout-protected":       switch/checkout moves HEAD onto dev/main/version/*
      - "wrong-base":               branch created with start-point main / origin/main
      - "create-protected":         creating a branch NAMED dev/main/version/*
      - "branch-reshape-protected": branch -f/-m/-d/-D aimed at a protected name
      - "worktree":                 any worktree verb other than `list`
    Read-only forms return None; applied to UNMARKED actors only (main()).
    """
    args = [a.rstrip(";") for a in args if a.rstrip(";")]
    if sub == "worktree":
        verb = next((a for a in args if not a.startswith("-")), None)
        if verb and verb not in WORKTREE_SAFE_VERBS:
            return ("worktree", verb)
        return None
    if sub in ("switch", "checkout"):
        if "--" in args:
            return None  # pathspec form restores files; HEAD does not move
        positional = [a for a in args if not a.startswith("-")]
        if any(a in CREATE_FLAGS for a in args):
            name = positional[0] if positional else None
            start = positional[1] if len(positional) > 1 else None
            if name and PROTECTED_RE.match(name):
                return ("create-protected", name)
            if start in WRONG_BASES:
                return ("wrong-base", start)
            return None
        if "--detach" in args or (sub == "switch" and "-d" in args):
            return None  # detached inspection; commits there are ref-less
        target = positional[0] if positional else None
        if target and PROTECTED_RE.match(target):
            return ("checkout-protected", target)
        return None
    if sub == "branch":
        if any(a in BRANCH_READONLY_FLAGS for a in args):
            return None  # list/query forms
        positional = [a for a in args if not a.startswith("-")]
        name = positional[0] if positional else None
        start = positional[1] if len(positional) > 1 else None
        reshape = any(a in ("-f", "--force", "-m", "-M", "-d", "-D", "--delete")
                      for a in args)
        protected_named = [a for a in positional if PROTECTED_RE.match(a)]
        if reshape:
            if protected_named:
                return ("branch-reshape-protected", protected_named[0])
            return None  # delete/rename of an ordinary work branch
        if name and PROTECTED_RE.match(name):
            return ("create-protected", name)
        if start in WRONG_BASES:
            return ("wrong-base", start)
        return None
    return None


def find_branch_hygiene_violation(cmd: str) -> tuple[str, str] | None:
    """Scan the command for the first branch-hygiene violation (v3.63).

    Walks every git invocation the way find_mutating_subcommand does (global
    options skipped, one subcommand per invocation), collects that
    invocation's argument tokens up to a statement separator, and classifies
    them via _hygiene_check(). Returns (kind, detail) or None.
    """
    try:
        tokens = shlex.split(cmd, comments=False, posix=True)
    except ValueError:
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
                break
            if j >= n:
                break
            sub = tokens[j]
            args: list[str] = []
            k = j + 1
            while k < n and tokens[k] not in STATEMENT_SEPS:
                args.append(tokens[k])
                k += 1
            viol = _hygiene_check(sub, args)
            if viol:
                return viol
            # Resume scanning right after the subcommand token (not after the
            # collected args): a glued separator (`dev; git …`) leaves the next
            # `git` inside args, and it must still be discovered as its own
            # invocation.
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


def find_merge_source(cmd: str) -> str | None:
    """Return the ref argument of the FIRST `git merge` invocation, or None.

    Mirrors find_mutating_subcommand's token-walk (global options / option
    values skipped) so the ref is never confused with an option. Used only to
    recognize "this command IS the dev-to-main promotion merge" (clean
    release-boundary guard, v3.64 #214) — it does not change what counts as a
    MUTATING subcommand.

    Once inside `merge`'s own argument list, flags in MERGE_OPTS_WITH_VALUE
    (`-m`/`--message`, `-X`/`--strategy-option`) always consume the NEXT token
    as their value — space-separated (`-m "msg"`) or `=`-joined
    (`--message="msg"`, `-X=ours`) — so that value is never mistaken for the
    ref (v3.64, #216 reviewer finding: an unhandled `-m` message was misread
    as the merge source). `-S`/`--gpg-sign` takes only an OPTIONAL GLUED value
    (`-Sabc123`, `--gpg-sign=abc123`) per `git merge -h` — it never consumes a
    following space-separated token (`git merge -S dev` really means "sign,
    and dev is the ref") — so only its glued forms are skipped.
    """
    try:
        tokens = shlex.split(cmd, comments=False, posix=True)
    except ValueError:
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
                if t == "merge":
                    k = j + 1
                    while k < n and tokens[k] not in STATEMENT_SEPS:
                        tk = tokens[k]
                        if tk in MERGE_OPTS_WITH_VALUE:
                            k += 2  # space-separated: flag then its value
                            continue
                        if "=" in tk and tk.split("=", 1)[0] in MERGE_OPTS_WITH_VALUE:
                            k += 1  # `--message=msg` / `-X=ours` joined form
                            continue
                        if tk.startswith("--gpg-sign=") or (
                            len(tk) > 2 and tk[:2] in ("-S", "-X")
                        ):
                            k += 1  # glued value: `-Sabc123`/`-Xours`/`--gpg-sign=id`
                            continue
                        if not tk.startswith("-"):
                            return tk
                        k += 1
                    return None
                break
            i = j
        i += 1
    return None


def _trees_identical(repo: str, ref_a: str, ref_b: str) -> bool:
    """True iff `ref_a` and `ref_b` resolve to the same tree content in `repo`.

    Thin wrapper over the BMI-2 tree-content predicate (`git diff --quiet`,
    see integration-branch-integrity-design.md §2): exit 0 = identical trees.
    Any git/subprocess failure (e.g. a ref that doesn't resolve) is treated as
    "not identical" — fail closed, never mistake an error for a clean
    boundary.
    """
    try:
        out = subprocess.run(
            ["git", "-C", repo, "diff", "--quiet", ref_a, ref_b],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def clean_release_boundary(proj: str, cmd: str) -> bool:
    """True iff a MARKED actor's commit/merge on `main` sits at a clean
    release boundary (v3.64, #214 — closes the BMI-4 residual: a marked
    master could previously commit to `main` at ANY time, including mid-
    release out of a clean promotion boundary).

    ANY of the following makes the boundary clean:
      1. Tree-content identity — `dev` and `main` already have identical
         trees (the same BMI-2 predicate used by the divergence guard):
         `dev` is fully promoted, so this commit lands immediately after
         (or as a no-op continuation of) a clean promotion.
      2. This invocation IS the promotion merge — `git merge <dev-ref>`
         while HEAD is `main`. Tree-identity (1) cannot yet hold at this
         point (that is the point of running the merge), so the merge
         command bringing `dev` in is itself recognized as legitimate.
      3. The explicit `.claude/release-in-progress.local` marker exists —
         set by `grm-project-release`'s promote step for the rest of the
         promotion window (e.g. a version-bump commit landing on `main`).

    Fails closed: any git-command error is treated as "not clean" (never
    silently allow on an inconclusive check).
    """
    marker_path = os.path.join(proj, ".claude", RELEASE_MARKER_NAME)
    if os.path.isfile(marker_path):
        return True

    if _trees_identical(proj, INTEGRATION_LINE, "main"):
        return True

    merge_src = find_merge_source(cmd)
    if merge_src and merge_src in DEV_MERGE_REFS:
        return True

    return False


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
                    "(See docs/grimoire/integration-workflow.md, worktree isolation.)\n"
                )
                sys.exit(2)
    # --------------------------------------------------------------------

    # ---- Branch-hygiene guard (v3.63) ----------------------------------
    # Deny, for an UNMARKED actor, the branch-pointer misuse patterns behind
    # the recurring wrong-branch incidents: moving HEAD onto a protected
    # branch, branching off main (wrong base), minting/reshaping protected
    # branch names, and worktree management. The marked master is exempt.
    if not actor_marked:
        viol = find_branch_hygiene_violation(cmd)
        if viol:
            kind, detail = viol
            staging_hint = ("git switch -c <branch> version/<X.Y>   "
                            "# or: git switch -c <branch> dev")
            messages = {
                "checkout-protected": (
                    f"blocked checkout of protected branch '{detail}'.\n"
                    "A task agent never moves its HEAD onto dev / main / "
                    "version/* — it\nbranches IN PLACE from the staging ref "
                    "and works there:\n  " + staging_hint + "\n"
                ),
                "wrong-base": (
                    f"blocked branch creation off '{detail}' (wrong base).\n"
                    "`main` carries release-only commits (version bumps, dist "
                    "artifacts) that\nmust never flow back through a work "
                    "branch into dev. Root your branch on\nthe staging ref "
                    "instead:\n  " + staging_hint + "\n"
                ),
                "create-protected": (
                    f"blocked creation of protected-named branch '{detail}'.\n"
                    "Only the marker-blessed integration worktree creates "
                    "dev / main /\nversion/* refs (grm-release-agreement "
                    "creates the staging branch).\nName your work branch "
                    "after the work item instead:\n  " + staging_hint + "\n"
                ),
                "branch-reshape-protected": (
                    f"blocked force-move/rename/delete of protected branch "
                    f"'{detail}'.\n"
                    "Repointing or deleting dev / main / version/* is an "
                    "integration-master\noperation (recovery playbook: "
                    "docs/grimoire/integration-workflow.md). If you\nbelieve "
                    "the branch pointer is wrong, STOP and report it — do "
                    "not repair it\nfrom a task worktree.\n"
                ),
                "worktree": (
                    f"blocked `git worktree {detail}`.\n"
                    "Task agents never create, remove, or move worktrees — "
                    "you work only in\nyour own. The integration master "
                    "manages worktrees (dispatch and\ndead-worktree cleanup). "
                    "`git worktree list` stays available.\n"
                ),
            }
            sys.stderr.write(
                "protected-branch-guard: " + messages[kind] +
                f"  worktree: {actor_wt}\n"
                "This worktree has no .claude/integration-allow.local marker "
                "(task agent).\nOperator override (deliberate): touch "
                ".claude/integration-allow.local\n"
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
    # push-guard.sh. Design rationale (§3a) lives in the upstream Grimoire
    # repository (framework-internal — not shipped).
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
                "worktree-isolation failure (design rationale is "
                "framework-internal;\nsee the upstream Grimoire repository): "
                "a dispatched Agent likely ran\nin-place instead of in its own worktree. "
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
        # ---- Clean release-boundary guard (v3.64, #214) -----------------
        # The marked+protected allow is now CONDITIONAL when cur == "main":
        # closes the BMI-4 residual where a marked master could commit to
        # `main` at any time, including mid-release out of a clean boundary
        # (the #126 failure mode: an ad-hoc commit straight to `main` outside
        # any promotion flow). dev/version/* stay unconditionally allowed for
        # the marked master — only `main` gets the boundary check.
        if cur == "main" and not clean_release_boundary(proj, cmd):
            sys.stderr.write(
                f"protected-branch-guard: blocked `git {sub}` on 'main' — no "
                "clean release boundary.\n"
                f"  worktree: {actor_wt}\n"
                "A MARKED integration master may commit/merge on 'main' ONLY "
                "at a genuine\nrelease-promotion boundary. None of the "
                "following held:\n"
                "  1. 'dev' and 'main' have identical trees (dev is fully "
                "promoted)\n"
                "  2. this command IS the dev-to-main promotion merge\n"
                "  3. .claude/release-in-progress.local marker is present\n"
                "This looks like an ad-hoc direct commit to 'main' outside "
                "any release\nflow (the issue #126 failure mode). Run the "
                "promotion through\ngrm-project-release instead — its "
                "promote step brackets the window with\nthe marker so every "
                "real step of the flow (the dev->main merge, the\n"
                "version-bump commit, tagging) is recognized as clean.\n"
                "If this truly is a deliberate out-of-band change, a human "
                "must place\n.claude/release-in-progress.local "
                "deliberately before proceeding.\n"
            )
            sys.exit(2)
        # ------------------------------------------------------------------
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


def _self_test_rewrite_detector() -> int:
    """Parser-level self-test for the v3.15 (#84) history-rewrite detector.

    No git / marker / protected-branch needed — exercises find_rewrite_op()
    against a table of commands (the protected-branch gating is applied in
    main() once HEAD is known). Returns failure count."""
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
    return failures


def _self_test_commit_on_main() -> int:
    """Parser-level + injected-branch self-test proving the (actor, branch-class)
    model covers direct commits to `main` (BMI-4, v3.38, issue #126).

    Simulates the guard logic in main() at the point after find_mutating_subcommand
    and current_branch are resolved, using injected values so no live git or
    marker file is needed. The `marked + main` cell is now CONDITIONAL on a
    clean release boundary (v3.64, #214) — this suite injects `boundary` to
    exercise both sides without touching real git state; Suite 4
    (`_self_test_clean_release_boundary`) proves `clean_release_boundary()`
    itself against a real scratch repo. Verifies:

      - UNMARKED actor + `git commit` on `main`  => denied  (exit 2 path)
      - UNMARKED actor + `git merge` on `main`   => denied  (exit 2 path)
      - MARKED actor   + `git commit` on `main`, clean boundary  => allowed
      - MARKED actor   + `git merge` on `main`, clean boundary   => allowed
      - MARKED actor   + `git commit` on `main`, NO boundary     => denied (#214)
      - UNMARKED actor + `git commit` on a work branch => allowed (exit 0 path)

    The (actor, branch-class) decision table (from the docstring above):
      unmarked + protected        -> deny
      unmarked + unprotected      -> allow
      marked   + protected (main) -> allow IFF clean release boundary (v3.64)
      marked   + protected (else) -> allow
      marked   + unprotected      -> deny  [HEAD-drift; separate guard path]

    Returns failure count.
    """
    def _decision(cmd: str, branch: str, marked: bool, boundary: bool = True) -> str:
        """Return 'deny' or 'allow' mirroring main()'s (actor, branch-class) logic."""
        sub = find_mutating_subcommand(cmd)
        if not sub:
            return "allow"
        if not PROTECTED_RE.match(branch):
            # Master HEAD-drift guard: marked + unprotected => deny
            if marked:
                return "deny"
            return "allow"  # unmarked on own work branch => allow
        # HEAD is on a protected branch
        if marked:
            if branch == "main" and not boundary:
                return "deny"  # v3.64 #214: no clean release boundary
            return "allow"  # blessed worktree
        return "deny"  # unmarked + protected => deny (the #126 coverage)

    cases = [
        # (cmd, branch, marked, boundary, expected_decision, label)
        ("git commit -m 'release v8.40'", "main", False, True, "deny",
         "UNMARKED actor git commit on main => deny (#126 direct-commit coverage)"),
        ("git merge --no-ff dev", "main", False, True, "deny",
         "UNMARKED actor git merge on main => deny (#126 direct-commit coverage)"),
        ("git commit -m 'promote dev to main'", "main", True, True, "allow",
         "MARKED master git commit on main, clean boundary => allow"),
        ("git merge --no-ff dev", "main", True, True, "allow",
         "MARKED master git merge on main, clean boundary => allow"),
        ("git commit -m 'ad-hoc edit'", "main", True, False, "deny",
         "MARKED master git commit on main, NO clean boundary => deny (v3.64 #214)"),
        ("git commit -m 'promote dev to main'", "version/3.64", True, False, "allow",
         "MARKED master git commit on version/X.Y (not main) => allow regardless of boundary"),
        ("git commit -m 'add feature'", "bmi-commit-guard-hook", False, True, "allow",
         "UNMARKED actor git commit on work branch => allow (normal work)"),
    ]
    failures = 0
    for cmd, branch, marked, boundary, expected, label in cases:
        got = _decision(cmd, branch, marked, boundary)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  [{label}]")
        if not ok:
            print(f"       cmd={cmd!r}  branch={branch!r}  marked={marked}  boundary={boundary}")
            print(f"       got={got!r}  want={expected!r}")
    return failures


def _self_test_branch_hygiene() -> int:
    """Parser-level self-test for the v3.63 branch-hygiene detector.

    Exercises find_branch_hygiene_violation() against a table of commands;
    no git / marker needed (the unmarked-actor gating is applied in main()).
    Returns failure count."""
    cases = [
        # HEAD moves onto protected branches — denied for unmarked actors.
        ("git switch dev", ("checkout-protected", "dev")),
        ("git checkout main", ("checkout-protected", "main")),
        ("git switch version/3.2", ("checkout-protected", "version/3.2")),
        ("git status && git switch dev", ("checkout-protected", "dev")),
        ("git log --oneline; git checkout main", ("checkout-protected", "main")),
        # Branch-in-place from the staging ref — the sanctioned pattern.
        ("git switch -c work version/3.2", None),
        ("git checkout -b work dev", None),
        ("git switch -c fix/a-bug dev", None),
        # Wrong base: main carries release-only commits.
        ("git switch -c work main", ("wrong-base", "main")),
        ("git checkout -b hotfix origin/main", ("wrong-base", "origin/main")),
        ("git branch work main", ("wrong-base", "main")),
        # Protected-named branch creation / reshaping.
        ("git switch -c version/3.3 dev", ("create-protected", "version/3.3")),
        ("git branch version/3.3", ("create-protected", "version/3.3")),
        ("git branch -f version/3.2 abc123",
         ("branch-reshape-protected", "version/3.2")),
        ("git branch -D dev", ("branch-reshape-protected", "dev")),
        ("git branch -m main main-old", ("branch-reshape-protected", "main")),
        # Ordinary work-branch management stays allowed.
        ("git branch -d old-work", None),
        ("git branch -D stale-work", None),
        ("git branch", None),
        ("git branch --list version/*", None),
        ("git branch -vv", None),
        # Worktree management is master-only; list stays available.
        ("git worktree add ../x dev", ("worktree", "add")),
        ("git worktree remove ../x", ("worktree", "remove")),
        ("git worktree prune", ("worktree", "prune")),
        ("git worktree list", None),
        # Read-only / non-HEAD-moving forms.
        ("git checkout dev -- path/file.txt", None),
        ("git checkout -- .", None),
        ("git switch --detach main", None),
        ("git switch -", None),
        ("git switch my-work", None),
        ("git checkout feature-x", None),
        ("echo 'git switch dev'", None),
        ("git diff dev...HEAD", None),
        # Second invocation after a glued separator is still discovered.
        ("git switch my-work; git checkout -b x main", ("wrong-base", "main")),
    ]
    failures = 0
    for cmd, expected in cases:
        got = find_branch_hygiene_violation(cmd)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {cmd!r} -> {got!r} (want {expected!r})")
    # `pull` joined MUTATING (unmarked `git pull` on protected = merge-class).
    ok = find_mutating_subcommand("git pull origin dev") == "pull"
    failures += not ok
    print(("ok  " if ok else "FAIL") + "  'git pull origin dev' -> mutating 'pull'")
    return failures


def _self_test_find_merge_source() -> int:
    """Parser-level self-test for find_merge_source() (v3.64, #214, #216).

    No git needed — exercises the ref-extraction walk against a table of
    `git merge` invocations (and non-merge look-alikes)."""
    cases = [
        ("git merge --no-ff dev", "dev"),
        ("git merge dev", "dev"),
        ("git merge origin/dev", "origin/dev"),
        ("git merge refs/heads/dev", "refs/heads/dev"),
        ("git merge --no-ff --strategy=recursive dev", "dev"),
        ("git -C /repo merge dev", "dev"),
        ("git merge feature/x", "feature/x"),
        ("git commit -m 'merge dev manually'", None),  # 'merge' only in message text
        ("git status", None),
        ("git merge --abort", None),  # escape hatch: no positional ref
        # #216 reviewer finding: -m/--message consumes the NEXT token as its
        # value — an unhandled `-m` message was misread as the merge source.
        # Space-separated form (the reported bug, this repo's own historical
        # promotion-commit style):
        (
            'git merge --no-ff -m "merge(release): v3.64 into main" dev',
            "dev",
        ),
        # `=`-joined form:
        (
            'git merge --message="merge(release): v3.64 into main" dev',
            "dev",
        ),
        # Non-blocking reviewer note: -X/--strategy-option and -S/--gpg-sign
        # can also precede the ref and must resolve to it, not their value.
        ("git merge -X ours dev", "dev"),          # -X space-separated value
        ("git merge -Xours dev", "dev"),            # -X glued value
        ("git merge --strategy-option=ours dev", "dev"),  # -X long =-joined
        ("git merge -Sabc123 dev", "dev"),          # -S glued key-id (optional value)
        ("git merge --gpg-sign=abc123 dev", "dev"),  # --gpg-sign =-joined key-id
        # -S/--gpg-sign take only an OPTIONAL glued value (never a following
        # space-separated token per `git merge -h`), so a bare -S / --gpg-sign
        # does NOT consume the next token — it correctly resolves to the ref.
        ("git merge -S dev", "dev"),
        ("git merge --gpg-sign dev", "dev"),
    ]
    failures = 0
    for cmd, expected in cases:
        got = find_merge_source(cmd)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {cmd!r} -> {got!r} (want {expected!r})")
    return failures


def _self_test_clean_release_boundary() -> int:
    """Real-scratch-git-repo self-test for clean_release_boundary() (v3.64,
    #214) — proves the guard against actual git state, not just parsed
    strings, mirroring the promotion sequence `grm-project-release` performs:
    a version-bump commit on `dev`, then `git switch main && git merge dev`.

    Cases proven against a real repo:
      1. Fresh repo, dev == main (freshly branched)      => clean  (tree-identical)
      2. dev advances with new work, main untouched       => NOT clean (ad-hoc
         commit on main here would be the #126 failure mode)
      3. The literal promotion merge command itself
         (`git merge --no-ff dev` while on main)           => clean (case 2 of
         the predicate: this command IS the promotion)
      4. After actually running that merge (trees now
         identical)                                        => clean (case 1)
      5. release-in-progress.local marker present, trees
         still diverged, command is an unrelated commit     => clean (case 3:
         explicit marker covers e.g. the version-bump commit on main)
      6. No marker, diverged trees, unrelated command        => NOT clean (deny)

    Returns failure count. Skips (prints SKIP, returns 0) if git or a temp
    directory is unavailable — self-test must not fail the harness on an
    exotic environment, but the check itself always runs in real usage.
    """
    import shutil
    import tempfile

    if shutil.which("git") is None:
        print("SKIP  clean_release_boundary real-repo suite (git not on PATH)")
        return 0

    failures = 0

    def _run(repo: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=10,
        )

    def _check(label: str, got: bool, want: bool) -> None:
        nonlocal failures
        ok = got == want
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  [{label}] got={got} want={want}")

    with tempfile.TemporaryDirectory(prefix="pbg-selftest-") as repo:
        _run(repo, "init", "-q", "-b", "main")
        _run(repo, "config", "user.email", "selftest@example.invalid")
        _run(repo, "config", "user.name", "Guard Selftest")
        readme = os.path.join(repo, "README.md")
        with open(readme, "w") as f:
            f.write("seed\n")
        _run(repo, "add", "README.md")
        _run(repo, "commit", "-q", "-m", "seed")
        _run(repo, "branch", "dev", "main")

        # Case 1: dev == main, freshly branched — trees identical.
        _check(
            "fresh repo, dev == main => clean",
            clean_release_boundary(repo, "git commit -m 'noop'"),
            True,
        )

        # Advance dev with real work (simulates the version-bump commit
        # grm-project-release makes on dev before promoting).
        _run(repo, "switch", "-q", "dev")
        feature = os.path.join(repo, "feature.txt")
        with open(feature, "w") as f:
            f.write("new work\n")
        _run(repo, "add", "feature.txt")
        _run(repo, "commit", "-q", "-m", "feat: add feature")

        # Case 2: main untouched, dev ahead — an ad-hoc commit on main now
        # would be the #126 failure mode. No marker, ordinary commit.
        _run(repo, "switch", "-q", "main")
        _check(
            "dev ahead of main, no marker, ordinary commit => NOT clean (deny)",
            clean_release_boundary(repo, "git commit -m 'direct edit on main'"),
            False,
        )

        # Case 3: the literal promotion merge command itself, while still
        # diverged (trees haven't reconciled yet — that's the point of
        # running it). Recognized via find_merge_source, not tree-identity.
        # Uses a realistic custom `-m` message matching this repo's ACTUAL
        # historical promotion-commit style (see `git log --merges`, e.g.
        # "merge(release): v3.63 ... into main") rather than the bare
        # `git merge --no-ff dev` the #216 reviewer noted was an
        # oversimplified stand-in — this is exactly the invocation form the
        # #216 bug misparsed (the `-m` message was returned as the merge
        # source instead of `dev`).
        _check(
            "diverged, command IS `git merge --no-ff -m '<release message>' dev` "
            "on main => clean",
            clean_release_boundary(
                repo,
                'git merge --no-ff -m "merge(release): v3.64 clean-release-boundary '
                'assertion for marked commits into main" dev',
            ),
            True,
        )

        # Case 4: actually perform the promotion merge; trees now identical.
        _run(
            repo, "merge", "--no-ff", "-q", "-m",
            "merge(release): v3.64 clean-release-boundary assertion for marked "
            "commits into main",
            "dev",
        )
        _check(
            "post-promotion-merge, trees identical => clean",
            clean_release_boundary(repo, "git commit -m 'unrelated later commit'"),
            True,
        )

        # Re-diverge (simulate `dev` moving on afterward) so the marker case
        # is tested against a genuinely NOT-tree-identical state.
        _run(repo, "switch", "-q", "dev")
        feature2 = os.path.join(repo, "feature2.txt")
        with open(feature2, "w") as f:
            f.write("more work\n")
        _run(repo, "add", "feature2.txt")
        _run(repo, "commit", "-q", "-m", "feat: more work")
        _run(repo, "switch", "-q", "main")

        # Case 5: explicit marker present covers the diverged state (e.g. the
        # version-bump commit landing on main mid-promotion-window).
        os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
        marker_path = os.path.join(repo, ".claude", RELEASE_MARKER_NAME)
        with open(marker_path, "w") as f:
            f.write("")
        _check(
            "diverged, release-in-progress.local marker present => clean",
            clean_release_boundary(repo, "git commit -m 'version bump to v1.1'"),
            True,
        )

        # Case 6: remove the marker — same diverged state, ordinary commit,
        # not the promotion merge => must deny again (no regression to
        # "marked + main always allowed").
        os.remove(marker_path)
        _check(
            "marker removed, still diverged, ordinary commit => NOT clean (deny)",
            clean_release_boundary(repo, "git commit -m 'another ad-hoc edit'"),
            False,
        )

    return failures


def _self_test() -> int:
    """Combined self-test: history-rewrite detector + direct-commit-on-main model.

    Run with --self-test; returns process exit code (0 = all pass), mirroring
    push-guard.sh's self-test pattern.

    Suite 1 — v3.15 (#84) history-rewrite detector (find_rewrite_op):
      Parser-level; no git / marker / protected-branch needed.

    Suite 2 — (actor, branch-class) direct-commit-to-main model (BMI-4, v3.38):
      Injected branch + marker values; proves an UNMARKED actor's git commit/merge
      on 'main' is denied and a MARKED actor is allowed (now conditional on a
      clean release boundary for 'main' — v3.64 #214). Locks #126-relevant
      behaviour against regression.

    Suite 4 — clean release-boundary guard (v3.64, #214):
      find_merge_source() parser-level cases, plus clean_release_boundary()
      proven against a REAL scratch git repo simulating an actual
      grm-project-release-style promotion sequence (not just injected values).
    """
    print("=== Suite 1: history-rewrite detector (v3.15 #84) ===")
    failures = _self_test_rewrite_detector()
    print(f"\n=== Suite 2: direct-commit-on-main model (BMI-4 v3.38 #126, v3.64 #214) ===")
    failures += _self_test_commit_on_main()
    print(f"\n=== Suite 3: branch-hygiene detector (v3.63) ===")
    failures += _self_test_branch_hygiene()
    print(f"\n=== Suite 4: clean release-boundary guard (v3.64 #214) ===")
    failures += _self_test_find_merge_source()
    failures += _self_test_clean_release_boundary()
    print(f"\n{'PASS' if not failures else str(failures)+' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    main()
