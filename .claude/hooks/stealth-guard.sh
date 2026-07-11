#!/usr/bin/env python3
"""Stealth-mode guard (active only when stealth-mode.value == "on").

PreToolUse Bash + Edit/Write/NotebookEdit hook. NO-OP unless
`.claude/grimoire-config.json` has `stealth-mode.value == "on"` — so it adds
zero behavioural change (and the cheapest possible exit) for the common,
stealth-off case.

When Stealth Mode IS on, it enforces — fail-closed — the five artifact rails
described in §6/§10 of the design rationale (framework-internal — not shipped;
see the upstream Grimoire repository):

  1. NO PUSH.  Every `git push` is denied (a strict superset of push-guard.sh;
     in stealth even the marker-blessed integration worktree may not push —
     the human pushes manually, outside Grimoire, if they choose).
  2. NO COMMITTING MANAGED PATHS.  `git add` / `stage` / `rm` / `mv` / `commit`
     naming a Grimoire-managed path (`.claude/`, `CLAUDE.md`, design docs, …) is
     denied, so the framework's own files never reach a commit.
  3. COMMIT-MESSAGE HYGIENE.  `git commit -m/--message/-F/--file` whose message
     contains an AI/agent tell (claude / anthropic / Co-Authored-By / LLM / 🤖 …)
     is denied, in any of the three forms git accepts: space (`-m msg`),
     `=`-joined long form (`--message=msg`), or glued short form (`-mmsg`).
     `-C <commit>`/`-C<commit>`/`--reuse-message=<commit>` reuses another
     commit's message verbatim (no editor, amend or not) — the guard resolves
     that commit's message via `git log -1 --format=%B <commit>` and
     hygiene-checks it directly, same treatment as `--amend --no-edit`'s HEAD
     reuse; this applies in all three argument forms too, including the glued
     `-C<commit>` short form. `-c <commit>`/`-c<commit>`/
     `--reedit-message=<commit>` always opens the editor afterward regardless
     of `--amend` (per `git commit --help`), so its post-edit content cannot
     be inspected in advance — it is denied outright in any of its three
     forms, asking for an explicit `-m`/`-F` instead. This also covers `git
     commit --amend` specifically: PreToolUse runs before the tool executes,
     so a bare `--amend` that opens an editor (no `-m`/`-F`/`-C`/
     `--reuse-message` on the command line, in any form) cannot have its new
     message content inspected in advance — the guard denies that
     bare-editor form outright and asks for an explicit `-m`/`-F` so the
     replacement message can be hygiene-checked like any other commit. An
     `--amend` that already supplies `-m`/`-F`/`-C`/`--reuse-message` (any
     form) is checked exactly like a non-amend commit. All four flags share
     one argument-parsing helper (`opt_value`/`has_opt`) so a form recognized
     for one is recognized for all — closing the gap where `-C`/`-c`'s glued
     short form (`-C<ref>`, `-c<ref>`) was previously missed by both
     `reuse_message_ref` and the `-c`/`--reedit-message` detection, a live,
     no-`--amend`-required bypass (`git commit -C<dirty-sha>` landed the
     dirty message verbatim with the hook exiting 0). `opt_value` also
     recognizes a glued short flag preceded by a leading run of other,
     unrelated combinable boolean short flags (e.g. `-aC<ref>`, `-avC<ref>`),
     and a `--long=value` token whose option name is any unambiguous prefix
     of `--message`/`--file`/`--reuse-message`/`--reedit-message` (e.g.
     `--reuse-mess=<ref>`, `--mess=<text>`) — both were live, real-git-
     confirmed bypasses (git 2.50) closed in the fourth #216 review round.
     See "Known limitations" in stealth-mode-design.md for what remains
     genuinely unhandled (accepted residual risk) after four rounds.
  4. NO GRIMOIRE BRANCH MODEL.  Creating a `version/*` branch (or `dev`/`main`
     when absent) via `switch -c` / `checkout -b` / `branch <name>` is denied —
     the branch model is itself a fingerprint; mirror the host repo instead.
  5. NO DIRECT EDIT/WRITE OF MANAGED PATHS.  `stealth-guard` previously gated
     managed-path changes only at `git add`/`commit` time; a direct Edit or
     Write of `.claude/`, `CLAUDE.md`, `docs/grimoire/`, etc. never went
     through git and so was never seen. This hook is now also registered on
     the Edit/Write/NotebookEdit PreToolUse matcher and denies any such tool
     call whose target path is Grimoire-managed, closing that loop
     (`worktree-guard.sh` only stops path-*escape*, not managed-path writes
     inside the worktree).

Escape hatches (`--abort` / `--quit` / `--skip`) and detached HEAD / non-repo
are no-ops. Run with `--self-test` for the fast dependency-free parser
harness, or `--self-test-live` for the slower end-to-end harness that
exercises `main()` via stdin against a real scratch git repo (needs `git` and
a writable temp dir).
"""
import json
import os
import re
import shlex
import subprocess
import sys

# ── Deny-list (commit-message hygiene); mirrors stealth_scrub.py ───────────
_WORD_TOKENS = ["claude", "anthropic", "openai", "gpt", "copilot", "llm",
                "ai-generated", "ai generated", "assistant-generated"]
_PHRASES = [r"generated by ai", r"language model", r"co-authored-by",
            r"as an ai", r"i'm an ai", r"i am an ai"]
_LITERALS = ["🤖"]


def build_denylist_pattern(extra):
    parts = []
    for tok in _WORD_TOKENS + list(extra or []):
        parts.append(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])")
    for ph in _PHRASES:
        parts.append(ph.replace(" ", r"\s+"))
    for lit in _LITERALS:
        parts.append(re.escape(lit))
    return re.compile("|".join(parts), re.IGNORECASE)


# ── Managed-path default set (Grimoire's own concealed files) ──────────────
DEFAULT_MANAGED = (
    ".claude/", "CLAUDE.md", "AGENTS.md",
    "docs/design/", "docs/grimoire/", "docs/roadmap.md", "docs/version-history.md",
    "docs/release-planning-v", "docs/release-planning/", "docs/grimoire/integration-workflow.md",
    "docs/coding-standards", "docs/architecture-guidelines.md",
    ".github/prompts/", ".github/copilot-instructions.md",
)
# docs/grimoire/ (v3.39 "Bulkhead"): the framework-internal doc tier — relocated
# framework design specs (docs/grimoire/design/) + study artifacts + the tier
# index. Managed Grimoire docs, so Stealth Mode must still recognize them.

STATEMENT_SEPARATORS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")", "\n"}
OPTS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "-c"}
# commit options that consume a following token as their value
COMMIT_VALUE_OPTS = {"-m", "--message", "-F", "--file", "-C", "--reuse-message",
                     "-c", "--reedit-message", "--author", "--date", "-S", "--gpg-sign"}
MESSAGE_OPTS = {"-m", "--message"}
FILE_OPTS = {"-F", "--file"}
# -C/-C<ref>/--reuse-message=<commit>: reuses <commit>'s message verbatim, no
# editor — knowable in advance by resolving <commit> via
# `git log -1 --format=%B`. All three argument forms (space/`=`/glued short)
# are equally real git syntax (verified against git 2.50) and must all reach
# `reuse_message_ref` via the shared `opt_value` helper.
REUSE_OPTS = {"-C", "--reuse-message"}
# -c/-c<ref>/--reedit-message=<commit>: like -C, but git commit --help
# documents that "the editor is invoked, so the user can further edit the
# commit message" — its final content is NOT knowable in advance, same as a
# bare editor amend. All three argument forms must be detected (`has_opt`).
REEDIT_OPTS = {"-c", "--reedit-message"}
# Round 4 (#216, 4th review): glued short-flag *clusters* like `-aC<ref>` —
# a legitimate, unrelated boolean short flag (`-a`) prefixed onto `-C`/`-c`/
# `-m`/`-F` before the glued value — were still missed, because the round-3
# `opt_value` only looked at `t[:2]` (the token's first two characters), so
# it recognized bare `-Cref` but not `-aCref`. Verified live against real
# `git commit` (git 2.50) which single-char boolean flags actually combine
# this way ahead of a message-affecting letter: `-a`/`--all`, `-v`/
# `--verbose`, `-n`/`--[no-]verify`, `-e`/`--edit`, `-s`/`--signoff` all do
# (`-aC<ref>`, `-vC<ref>`, `-nC<ref>`, `-eC<ref>`, `-sC<ref>` all reuse
# <ref>'s message exactly like bare `-C<ref>`). `-S`/`--gpg-sign` does
# **NOT** combine this way and is deliberately excluded: `-S` itself takes
# an *optional* glued value (a gpg key-id, no separator — `-S<key-id>`), so
# `-SC<ref>` is parsed by git as `-S` with the literal key-id `C<ref>` (git
# then fails trying to gpg-sign with that bogus key-id) — NOT as `-S`
# followed by `-C<ref>`. Treating `S` as a scannable-through prefix letter
# would misparse a real `-SC<key-id>` gpg-sign invocation as a message-reuse
# form, a false positive on unrelated, legitimate usage. `-p`/`-z`/`-o`/`-i`/
# `-q` are not realistic combinations for a commit invocation that also
# reuses/glues a message and are likewise excluded (kept to the combinations
# actually exercised live, per the design note above, not a full replica of
# every documented short flag).
CLUSTER_PASSTHROUGH = frozenset("avnes")
# The message-affecting letters `opt_value`'s cluster scan stops on — the
# short-option character for each of -m/-F/-C/-c respectively. Once one of
# these is hit inside a leading run of CLUSTER_PASSTHROUGH characters, the
# remainder of the token (everything after that letter) is that option's
# glued value, exactly like the bare `-Xvalue` case.
MESSAGE_AFFECTING_SHORT = {"m": MESSAGE_OPTS, "F": FILE_OPTS,
                           "C": REUSE_OPTS, "c": REEDIT_OPTS}
# --amend supplies its new message via the editor unless one of these is also
# given on the command line — these are the forms whose new message text is
# (at least initially) visible to a PreToolUse hook before the commit lands.
# REEDIT_OPTS is intentionally excluded: its message is always subject to a
# post-check editor pass, so it is handled like the bare-editor case instead.
AMEND_MESSAGE_SOURCES = MESSAGE_OPTS | FILE_OPTS | REUSE_OPTS
ESCAPE_HATCH = {"--abort", "--quit", "--skip", "--continue"}
BRANCH_DELETE_OR_LIST = {"-d", "-D", "--delete", "--list", "-l", "-m", "-M",
                         "--move", "-a", "--all", "-r", "--remotes", "--show-current",
                         "--edit-description", "-v", "-vv", "--merged", "--no-merged"}


def deny(msg):
    sys.stderr.write("stealth-guard: " + msg)
    sys.exit(2)


def stealth_on(proj):
    try:
        cfg = json.load(open(os.path.join(proj, ".claude", "grimoire-config.json")))
    except Exception:
        return False, []
    sm = cfg.get("stealth-mode")
    if not isinstance(sm, dict):
        return False, []
    on = (sm.get("value") == "on")
    extra = sm.get("comment-denylist", []) or []
    return on, extra


def managed_globs(proj):
    try:
        cfg = json.load(open(os.path.join(proj, ".claude", "grimoire-config.json")))
        ov = cfg.get("stealth-mode", {}).get("managed-paths")
        if ov:
            return tuple(g.lstrip("/").rstrip("*") for g in ov)
    except Exception:
        pass
    return DEFAULT_MANAGED


def is_managed(path, globs):
    p = path[2:] if path.startswith("./") else path
    p = p.lstrip("/")  # strip leading slashes only, never the dot of ".claude"
    return any(p == m or p.startswith(m) for m in globs)


def tokenize(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    return list(lex)


def git_calls(tokens):
    """Yield (subcommand, arg_tokens) for each `git <sub> ...` simple command."""
    calls = []
    i, n = 0, len(tokens)
    while i < n:
        if tokens[i] == "git" or tokens[i].endswith("/git"):
            j = i + 1
            sub = None
            while j < n:
                t = tokens[j]
                if t in OPTS_WITH_VALUE:
                    j += 2
                    continue
                if t.startswith("-"):
                    j += 1
                    continue
                sub = t
                j += 1
                break
            args = []
            while j < n and tokens[j] not in STATEMENT_SEPARATORS:
                args.append(tokens[j])
                j += 1
            if sub:
                calls.append((sub, args))
            i = j
        else:
            i += 1
    return calls


# The four long option names this hook cares about — `_long_opt_name` checks
# whether a given `opts` set contains one of these names' *full* long form
# (e.g. "--message"); `opts` may be one of MESSAGE_OPTS/FILE_OPTS/REUSE_OPTS/
# REEDIT_OPTS directly, or a union of several (e.g. AMEND_MESSAGE_SOURCES =
# MESSAGE_OPTS | FILE_OPTS | REUSE_OPTS) — membership, not identity, is what
# matters, so this also works correctly for callers that pass a merged set.
# Verified (git 2.50) that none of these four prefix-collides with another —
# they diverge at the first character after "--" (m / f / reu / ree) — so a
# plain `str.startswith` match against each full name, with no other flag's
# name considered, is sufficient; this hook does not need to replicate git's
# full flag registry or resolve ambiguity against unrelated flags outside
# these four (if an abbreviation is ambiguous against some other git-commit
# flag, git itself rejects it — this hook only needs to recognize the forms
# git actually accepts for these four specifically).
_LONG_OPT_NAMES = {
    "message": MESSAGE_OPTS, "file": FILE_OPTS,
    "reuse-message": REUSE_OPTS, "reedit-message": REEDIT_OPTS,
}


def _long_opt_name(opt, opts):
    """True iff `opt` (the part of a `--...` token before any `=`, with the
    leading `--` stripped) is an unambiguous-prefix abbreviation of one of
    the full long-option names backing `opts`, per git's own long-option
    abbreviation rule: git accepts any prefix of a long option name that is
    unambiguous among the options git knows. Round 4 (#216, 4th review):
    `opt_value`'s long-form branch previously did exact string equality
    only, so `--reuse-mess=<ref>`, `--mess=<text>`, `--fil=<path>` all
    silently bypassed hygiene checking entirely — confirmed live against
    real `git commit` (git 2.50), which accepts every one of those
    abbreviations and resolves them to the full option unambiguously.

    Matches by *membership* of the full long form in `opts` (e.g. is
    "--message" in opts), not by identity of the whole set object — so this
    also works when `opts` is a union of several option sets (e.g.
    AMEND_MESSAGE_SOURCES), not only when it is exactly one of
    MESSAGE_OPTS/FILE_OPTS/REUSE_OPTS/REEDIT_OPTS by reference.
    """
    if not opt:
        return False
    for name, name_opts in _LONG_OPT_NAMES.items():
        full = "--" + name
        if full in opts and name.startswith(opt):
            return True
    return False


def opt_value(t, opts):
    """If token `t` supplies one of the value-taking options in `opts` (a set
    of option strings, both short e.g. "-m" and long e.g. "--message"),
    return that option's value as encoded in `t` itself — i.e. every joined
    form git accepts on a *single token*: `--long=value` (including an
    unambiguous-prefix abbreviation of `--long`) and glued short `-Xvalue`
    (including one preceded by a leading run of combinable boolean short
    flags, e.g. `-aXvalue`). Returns None if `t` doesn't match any option in
    `opts` in a joined form (the caller handles the separate `-X value` /
    `--long value` space form itself, since that consumes the *next* token
    too).

    This is the one shared rule for every message-affecting commit flag
    (`-m`/`--message`, `-F`/`--file`, `-C`/`--reuse-message`, `-c`/
    `--reedit-message`), verified against real `git commit` behaviour:

    - Long options split on the first `=`: `--reuse-message=<ref>` ->
      opt=`--reuse-message`, value=`<ref>`. The part before `=` need not be
      the full option name — any unambiguous prefix (`--reuse-mess=<ref>`,
      `--mess=<text>`, `--fil=<path>`) resolves the same way (round 4).
    - Short options do NOT treat `=` specially — everything after the single
      flag character is the value verbatim, `=` included. `git commit
      -m=foo` commits the message `=foo`, not `foo` (verified against git
      2.50). So `-Cabc123` -> `abc123`, and `-C=abc123` -> the (unusual but
      real) ref literal `=abc123`, not `abc123`.
    - A glued short option may be preceded by a run of *other*, unrelated
      boolean short flags stacked in the same token — e.g. `-aC<ref>` is
      `-a` (stage all) + `-C<ref>` (reuse message), not a single flag `-a`
      with value `C<ref>` (round 4). Only the specific letters verified live
      as combinable this way (`CLUSTER_PASSTHROUGH`) are scanned through;
      the scan stops at the first message-affecting letter (`m`/`F`/`C`/`c`)
      and treats everything after it as that option's glued value. `-S`
      (gpg-sign) is deliberately NOT a passthrough letter: it takes its own
      optional glued value with no separator, so `-SC<ref>` is `-S` with
      key-id `C<ref>` per git itself, not `-S` + `-C<ref>` — scanning through
      `S` would misparse a legitimate `-SC<key-id>` gpg-sign call.
    """
    if t.startswith("--"):
        if "=" in t:
            opt, val = t.split("=", 1)
            name = opt[2:]
            if opt in opts or _long_opt_name(name, opts):
                return val
        return None
    # short option forms: exactly "-X" is the space form (no value here);
    # anything longer is glued (possibly behind a passthrough cluster
    # prefix), and the remainder — whatever it is — is the value.
    if len(t) > 2 and not t.startswith("--") and t.startswith("-"):
        short = t[:2]
        if short in opts:
            return t[2:]
        # Cluster-prefix scan (round 4): walk past leading combinable
        # boolean short flags that are not themselves message-affecting,
        # e.g. the "a" in "-aC<ref>". Stop at the first character that is
        # either a message-affecting letter (if it belongs to `opts`, the
        # rest of the token is its glued value) or anything else (not a
        # combination this hook recognizes — bail out, return None).
        i = 1
        while i < len(t) and t[i] in CLUSTER_PASSTHROUGH:
            i += 1
        if i < len(t) and t[i] in MESSAGE_AFFECTING_SHORT:
            letter = t[i]
            cluster_opt = "-" + letter
            if cluster_opt in opts:
                return t[i + 1:]
    return None


def has_opt(args, opts):
    """True iff `args` supplies any option in `opts` in ANY of the three
    forms git accepts on a `commit` (or other value-taking-option) call:
    space (`-X value` / `--opt value`), `=`-joined long (`--opt=value`), or
    glued short (`-Xvalue`). Presence-only check (no value extraction) — use
    `opt_value` when the value itself is needed.
    """
    for t in args:
        if t == "--":
            break
        if t in opts or opt_value(t, opts) is not None:
            return True
    return False


def split_paths_and_messages(args):
    """For add/rm/mv/commit: return (positional_paths, message_strings).

    Messages are collected from `-m`/`--message` (uniform joined/space
    extraction via `opt_value`). `-F`/`--file`'s *value token* (the path) is
    likewise recognized and skipped in all three forms via `opt_value`, so it
    can never be misread as a positional path or desynchronize the scan of
    later tokens — but its file *content* is deliberately NOT read here (see
    the docstring note below). Every other value-taking commit option
    (`-C`/`-c`/`--reuse-message`/`--reedit-message`/`--author`/`--date`/…)
    has its value token skipped the same way without being read as a literal
    message — those are handled by their own dedicated resolvers
    (`reuse_message_ref`, the `is_reedit` check) since their "message" is not
    the literal argument text but something resolved via git (a reused
    commit's message) or unknowable in advance (an editor pass).

    `-F`/`--file` scope note: unlike `-C`/`-c` (which reuse an existing
    commit's message — resolvable via `git log`, independent of any
    filesystem path), `-F <path>` names a file relative to the *shell's
    working directory at invocation*, which this hook does not reliably know
    (no sibling guard in this repo resolves tool-call cwd, and getting it
    wrong would silently mis-check the wrong file). Reading that file's
    content for hygiene-checking is a real, separate hardening opportunity —
    intentionally left as a follow-up rather than guessed at here.
    """
    paths, messages = [], []
    i, n = 0, len(args)
    saw_ddash = False
    while i < n:
        t = args[i]
        if t == "--":
            saw_ddash = True
            i += 1
            continue
        if not saw_ddash and t.startswith("-"):
            val = opt_value(t, MESSAGE_OPTS)
            if val is not None:
                messages.append(val)
                i += 1
                continue
            if opt_value(t, FILE_OPTS) is not None:
                i += 1
                continue
            if t in MESSAGE_OPTS and i + 1 < n:
                messages.append(args[i + 1])
                i += 2
                continue
            if t in COMMIT_VALUE_OPTS and i + 1 < n:
                i += 2
                continue
            i += 1
            continue
        paths.append(t)
        i += 1
    return paths, messages


def is_bare_editor_amend(args):
    """True iff `commit` args include --amend with no inspectable message
    source (-m/--message/-F/--file/-C/--reuse-message) and no --no-edit, i.e.
    the new message would come only from an interactive editor that has not
    run yet when this PreToolUse hook fires.

    `--no-edit` is deliberately NOT treated as bare here — it reuses the
    existing HEAD message verbatim (no editor involved), so that message is
    independently checkable via `git log -1 --format=%B` (see
    `head_commit_message`); the caller runs that check instead of denying.

    `-C <commit>`/`-C<commit>`/`--reuse-message=<commit>` is likewise NOT
    bare — it reuses <commit>'s message verbatim with no editor, so it is
    independently checkable via `git log -1 --format=%B <commit>` (see
    `resolve_ref_message` / the reuse-message check in `main`). This applies
    equally to the glued short form `-C<commit>` (e.g. `-C1a2b3c4`), which
    git accepts identically to `-C <commit>` — verified against real `git
    commit` (git 2.50): a glued `-C`/`-c` is not a distinct, unsupported
    form, just the same short-option glued-value convention `-mMESSAGE`
    already uses.

    `-c <commit>`/`-c<commit>`/`--reedit-message=<commit>` IS treated as bare
    (falls through to `return True`) even though it names a commit: per
    `git commit --help` it "invoke[s] the editor, so that the user can
    further edit the commit message" — exactly like a plain `--amend`, its
    final content is not knowable in advance, so it gets the same deny-and-
    ask-for-explicit-`-m`/`-F` treatment, regardless of which of its three
    argument forms (space/`=`/glued) is used.
    """
    if "--amend" not in args or "--no-edit" in args:
        return False
    i, n = 0, len(args)
    while i < n:
        t = args[i]
        if t == "--":
            break
        if opt_value(t, AMEND_MESSAGE_SOURCES) is not None:
            return False  # `--opt=value` or glued short `-Xvalue`
        if t in AMEND_MESSAGE_SOURCES:
            return False  # space form `-X value` / `--opt value`
        i += 1
    return True


def resolve_ref_message(proj, ref):
    """Return <ref>'s commit message (%B), or None if unresolvable.

    <ref> may be a SHA, `HEAD`, `HEAD~2`, a branch/tag name, etc. — any git
    commit-ish. Used both for HEAD (the `--no-edit` case) and for an
    arbitrary `-C`/`--reuse-message=<ref>` target.
    """
    try:
        r = subprocess.run(["git", "-C", proj, "log", "-1", "--format=%B", ref],
                            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def head_commit_message(proj):
    """Return the current HEAD commit message (%B), or None if unavailable."""
    return resolve_ref_message(proj, "HEAD")


def reuse_message_ref(args):
    """Return the <commit> argument to -C/--reuse-message if present in a
    `commit` call's args, else None. Handles all three forms git itself
    accepts (verified against real `git commit`, git 2.50): space
    (`-C <ref>` / `--reuse-message <ref>`), `=`-joined long form
    (`--reuse-message=<ref>`), and glued short form (`-C<ref>`, e.g.
    `-C1a2b3c4`) — glued `-C<ref>` is NOT a git syntax error; it reuses
    <ref>'s message exactly like the space form, and was the reviewer-caught
    gap this closes (a bare `git commit -C<dirty-sha>`, no --amend needed,
    previously bypassed hygiene checking entirely).
    """
    i, n = 0, len(args)
    while i < n:
        t = args[i]
        if t == "--":
            break
        val = opt_value(t, REUSE_OPTS)
        if val is not None:
            return val
        if t in REUSE_OPTS and i + 1 < n:
            return args[i + 1]
        i += 1
    return None


def branch_exists(proj, name):
    try:
        r = subprocess.run(["git", "-C", proj, "rev-parse", "--verify", "--quiet",
                            "refs/heads/" + name], capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def created_branch_name(sub, args):
    """Return a newly-created branch name for switch -c / checkout -b / branch X."""
    if sub == "switch":
        for i, t in enumerate(args):
            if t in ("-c", "-C", "--create", "--force-create") and i + 1 < len(args):
                return args[i + 1]
            if t.startswith("-c") and len(t) > 2 and not t.startswith("--"):
                return t[2:]
        return None
    if sub == "checkout":
        for i, t in enumerate(args):
            if t in ("-b", "-B") and i + 1 < len(args):
                return args[i + 1]
        return None
    if sub == "branch":
        if any(a in BRANCH_DELETE_OR_LIST for a in args):
            return None
        for t in args:
            if not t.startswith("-"):
                return t
        return None
    return None


def check_branch(proj, sub, args):
    name = created_branch_name(sub, args)
    if not name:
        return
    if name.startswith("version/"):
        deny(f"blocked creating `{name}` — the Grimoire `version/*` branch model is\n"
             "a fingerprint and is suppressed in Stealth Mode. Work on the host\n"
             "repo's current branch, or a short-lived branch named to its own\n"
             "convention, and reconcile it before the session ends.\n")
    if name in ("dev", "main") and not branch_exists(proj, name):
        deny(f"blocked creating a new `{name}` branch in Stealth Mode — the\n"
             "Grimoire branch model is suppressed. Mirror the host repo instead.\n")


def relative_to_project(proj, fp):
    """Best-effort project-relative path for a possibly-absolute file_path."""
    if not fp:
        return fp
    if os.path.isabs(fp):
        try:
            rel = os.path.relpath(fp, proj)
        except ValueError:
            return fp  # different drive/mount — leave as-is, is_managed will not match
        return rel
    return fp


def check_edit_write(proj, payload, globs):
    """PreToolUse handling for Edit / Write / NotebookEdit.

    Closes the gap where stealth-guard only gated managed-path changes at
    `git add`/`commit` time: a direct Edit or Write of `.claude/`, `CLAUDE.md`,
    `docs/grimoire/`, etc. never went through git at all, so the git-call
    scanner above never saw it. Deny outright — these paths must stay
    uncommitted AND unmodified-by-the-agent in Stealth Mode; there is no
    legitimate stealth-mode reason to edit Grimoire's own concealed files
    through the managed tool surface (a human can still edit them directly).
    """
    tin = payload.get("tool_input", {}) or {}
    fp = tin.get("file_path") or tin.get("notebook_path") or ""
    if not fp:
        return
    rel = relative_to_project(proj, fp)
    if is_managed(rel, globs):
        deny(f"blocked {payload.get('tool_name')} on `{fp}` — this is a\n"
             "Grimoire-managed path and is excluded from Stealth Mode's\n"
             "committed artifact. Direct Edit/Write of managed paths is denied\n"
             "the same as `git add`/`commit` of them (both are the tell); this\n"
             "closes the gap where only the git-call surface was gated.\n")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Bash", "Edit", "Write", "NotebookEdit"):
        sys.exit(0)
    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not proj:
        sys.exit(0)
    on, extra = stealth_on(proj)
    if not on:
        sys.exit(0)  # ← stealth off: pure no-op, no behavioural change

    if tool_name in ("Edit", "Write", "NotebookEdit"):
        globs = managed_globs(proj)
        check_edit_write(proj, payload, globs)
        sys.exit(0)

    cmd = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    try:
        tokens = tokenize(cmd)
    except ValueError:
        # Unparseable (e.g. unbalanced heredoc quote): fail safe on the cheap
        # high-value check — any literal `git push` denies.
        if re.search(r"\bgit\b[^\n;|&]*\bpush\b", cmd):
            deny("blocked `git push` — Stealth Mode never pushes.\n")
        sys.exit(0)

    globs = managed_globs(proj)
    denylist = build_denylist_pattern(extra)

    for sub, args in git_calls(tokens):
        if any(a in ESCAPE_HATCH for a in args):
            continue  # never trap a recovery
        if sub in ("push", "send-pack"):
            deny("blocked `git push` — Stealth Mode never pushes (categorical;\n"
                 "even the integration worktree). If you truly mean to publish,\n"
                 "disable Stealth Mode and push manually outside Grimoire.\n")
        if sub in ("switch", "checkout", "branch"):
            check_branch(proj, sub, args)
        if sub == "commit":
            is_reedit = has_opt(args, REEDIT_OPTS)
            if is_reedit:
                # -c/-c<ref>/--reedit-message=<ref>/--reedit-message <ref>:
                # per `git commit --help`, "the editor is invoked, so the
                # user can further edit the commit message" — its final
                # content is not knowable in advance any more than a bare
                # editor amend's, so it gets the same deny-and-ask-for-
                # explicit-message treatment (amend or not, and regardless of
                # which of the three argument forms is used — including the
                # glued short form `-c<ref>`, which git accepts identically
                # to `-c <ref>`, verified against real `git commit`).
                deny("blocked `git commit -c/--reedit-message` — this always\n"
                     "invokes the editor afterward (per `git commit --help`), so\n"
                     "the final message is not knowable in advance. Re-run with an\n"
                     "explicit `-m \"...\"` (or `-F <file>`) so the new message can\n"
                     "be hygiene-checked, e.g.\n"
                     "  git commit -m \"your clean message\"\n")
            if "--amend" in args and is_bare_editor_amend(args):
                deny("blocked `git commit --amend` with no `-m`/`-F` (etc.) on the\n"
                     "command line — the replacement message would come from an\n"
                     "interactive editor this hook cannot inspect before the commit\n"
                     "lands. Re-run with an explicit `-m \"...\"` (or `-F <file>`) so\n"
                     "the new message can be hygiene-checked, e.g.\n"
                     "  git commit --amend -m \"your clean message\"\n")
            if "--amend" in args and "--no-edit" in args:
                # Message is reused verbatim from HEAD — no editor runs, so the
                # resulting message IS knowable in advance; check it directly.
                head_msg = head_commit_message(proj)
                if head_msg:
                    hit = denylist.search(head_msg)
                    if hit:
                        deny("blocked `git commit --amend --no-edit` — the reused HEAD\n"
                             f"message contains the AI/agent tell {hit.group(0)!r}.\n"
                             "Amend with an explicit clean `-m` instead of --no-edit.\n")
            reuse_ref = reuse_message_ref(args)
            if reuse_ref:
                # -C/--reuse-message=<ref>: message is reused verbatim from
                # <ref> — no editor runs, so it IS knowable in advance; resolve
                # and check it directly (amend or not — the same gap applies
                # to a plain `git commit -C <dirty-sha>`, same as --no-edit's
                # HEAD-reuse case).
                reused_msg = resolve_ref_message(proj, reuse_ref)
                if reused_msg:
                    hit = denylist.search(reused_msg)
                    if hit:
                        deny("blocked `git commit -C/--reuse-message="
                             f"{reuse_ref}` — the reused message from `{reuse_ref}`\n"
                             f"contains the AI/agent tell {hit.group(0)!r}. Re-run with\n"
                             "an explicit clean `-m` instead of reusing that message.\n")
        if sub in ("add", "stage", "rm", "mv", "commit"):
            paths, messages = split_paths_and_messages(args)
            for p in paths:
                if is_managed(p, globs):
                    deny(f"blocked `git {sub} {p}` — `{p}` is a Grimoire-managed\n"
                         "path and must never be committed in Stealth Mode (it is the\n"
                         "tell). These files are excluded locally; keep them uncommitted.\n")
            for m in messages:
                hit = denylist.search(m)
                if hit:
                    deny(f"blocked `git commit` — message contains the AI/agent tell "
                         f"{hit.group(0)!r}.\nStealth Mode commit messages carry no "
                         "Claude/AI/Co-Authored-By markers. Rewrite the message.\n")
    sys.exit(0)


# ── parser self-test (no git / config needed) ──────────────────────────────
def _self_test():
    globs = DEFAULT_MANAGED
    dl = build_denylist_pattern([])

    def calls(c):
        return git_calls(tokenize(c))

    fails = 0

    def check(desc, cond):
        nonlocal fails
        print(("ok  " if cond else "FAIL") + "  " + desc)
        fails += not cond

    # push detection
    check("push detected", any(s in ("push", "send-pack") for s, _ in calls("git push origin main")))
    check("push in chain detected",
          sum(1 for s, _ in calls("git commit -m x && git push origin main") if s == "push") == 1)
    # managed path detection
    p, _ = split_paths_and_messages(calls("git add .claude/x.md")[0][1])
    check("managed add flagged", any(is_managed(x, globs) for x in p))
    p, _ = split_paths_and_messages(calls("git add src/app.py")[0][1])
    check("deliverable add not flagged", not any(is_managed(x, globs) for x in p))
    # commit message extraction + hygiene
    _, m = split_paths_and_messages(calls('git commit -m "fix: per Claude"')[0][1])
    check("msg extracted", m == ["fix: per Claude"])
    check("msg tell hit", bool(dl.search(m[0])))
    _, m2 = split_paths_and_messages(calls('git commit -m "normal bugfix"')[0][1])
    check("clean msg no hit", not dl.search(m2[0]))
    _, m3 = split_paths_and_messages(calls('git commit --message="Co-Authored-By: Claude"')[0][1])
    check("joined --message= extracted", bool(m3 and dl.search(m3[0])))
    # commit naming a managed path
    p4, _ = split_paths_and_messages(calls('git commit -m "x" CLAUDE.md')[0][1])
    check("commit managed path flagged", any(is_managed(x, globs) for x in p4))
    # amend hygiene closure (#216): bare-editor amend (no -m/-F) must be
    # flagged as uninspectable; an amend that supplies -m/-F is inspectable
    # and goes through the normal message-hygiene path instead.
    check("bare amend flagged (no message source)",
          is_bare_editor_amend(calls("git commit --amend")[0][1]))
    check("amend -m is inspectable, not flagged",
          not is_bare_editor_amend(calls('git commit --amend -m "clean message"')[0][1]))
    check("amend --message= is inspectable, not flagged",
          not is_bare_editor_amend(calls('git commit --amend --message="clean message"')[0][1]))
    check("amend -F is inspectable, not flagged",
          not is_bare_editor_amend(calls("git commit --amend -F msg.txt")[0][1]))
    check("amend --no-edit is NOT a bare-editor amend (message is reused "
          "verbatim from HEAD, checked separately via head_commit_message)",
          not is_bare_editor_amend(calls("git commit --amend --no-edit")[0][1]))
    check("amend -mMSG glued is inspectable, not flagged",
          not is_bare_editor_amend(calls("git commit --amend -mCleanMsg")[0][1]))
    check("non-amend commit never flagged by amend check",
          not is_bare_editor_amend(calls('git commit -m "normal"')[0][1]))
    _, am = split_paths_and_messages(calls('git commit --amend -m "fix: per Claude"')[0][1])
    check("amend -m message still hygiene-checked", bool(am and dl.search(am[0])))

    # reuse-message bypass closure (reviewer-caught, post-#216): -C/-c/
    # --reuse-message/--reedit-message must be resolved and hygiene-checked,
    # not silently treated as an inspectable-but-unchecked message source.
    check("amend -C <ref> is inspectable, not flagged as bare",
          not is_bare_editor_amend(calls("git commit --amend -C abc123")[0][1]))
    check("amend --reuse-message=<ref> is inspectable, not flagged as bare",
          not is_bare_editor_amend(calls("git commit --amend --reuse-message=abc123")[0][1]))
    check("amend -c <ref> (reedit) IS flagged as bare (editor runs after)",
          is_bare_editor_amend(calls("git commit --amend -c abc123")[0][1]))
    check("amend --reedit-message=<ref> IS flagged as bare (editor runs after)",
          is_bare_editor_amend(calls("git commit --amend --reedit-message=abc123")[0][1]))
    check("reuse_message_ref extracts -C <ref>",
          reuse_message_ref(calls("git commit --amend -C abc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts --reuse-message=<ref>",
          reuse_message_ref(calls("git commit --amend --reuse-message=abc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts space form --reuse-message <ref>",
          reuse_message_ref(calls("git commit --amend --reuse-message abc123")[0][1]) == "abc123")
    check("reuse_message_ref None when absent",
          reuse_message_ref(calls('git commit --amend -m "clean"')[0][1]) is None)
    check("reuse_message_ref None for -c/--reedit-message (different option)",
          reuse_message_ref(calls("git commit --amend -c abc123")[0][1]) is None)
    check("plain (non-amend) commit -C <ref> also extracted (gap applies without --amend too)",
          reuse_message_ref(calls("git commit -C abc123")[0][1]) == "abc123")

    # glued short-flag bypass closure (round 3, reviewer-caught post-#216):
    # -C<ref>/-c<ref> with NO separator were missed entirely by both
    # reuse_message_ref and the is_reedit check (they only tested `"=" in t`
    # and exact membership) — a live, no-`--amend`-required bypass for both
    # `-C`/`-c`. Every form × every flag from the design-doc enumeration is
    # covered here via the shared `opt_value`/`has_opt` helpers.
    check("reuse_message_ref extracts glued -C<ref> (plain, non-amend)",
          reuse_message_ref(calls("git commit -Cabc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts glued -C<ref> (--amend)",
          reuse_message_ref(calls("git commit --amend -Cabc123")[0][1]) == "abc123")
    check("has_opt detects glued -c<ref> (plain, non-amend) as reedit",
          has_opt(calls("git commit -cabc123")[0][1], REEDIT_OPTS))
    check("has_opt detects glued -c<ref> (--amend) as reedit",
          has_opt(calls("git commit --amend -cabc123")[0][1], REEDIT_OPTS))
    check("amend -C<ref> glued is inspectable, not flagged as bare",
          not is_bare_editor_amend(calls("git commit --amend -Cabc123")[0][1]))
    check("amend -c<ref> glued IS flagged as bare (editor runs after)",
          is_bare_editor_amend(calls("git commit --amend -cabc123")[0][1]))
    check("reuse_message_ref does not misfire on glued -c<ref> (reedit, not reuse)",
          reuse_message_ref(calls("git commit -cabc123")[0][1]) is None)
    check("has_opt does not misfire REEDIT on glued -C<ref> (reuse, not reedit)",
          not has_opt(calls("git commit -Cabc123")[0][1], REEDIT_OPTS))

    # round 4 (#216, 4th review, reviewer-caught): short-cluster prefix
    # bypass — `-C`/`-c`/`-m`/`-F` glued after a leading, unrelated boolean
    # short-flag prefix (e.g. `-aC<ref>`) was still missed, because the
    # round-3 opt_value only checked t[:2]. Verified live against real git
    # commit (git 2.50) that -a/-v/-n/-e/-s all combine this way; -S does
    # NOT (it consumes its own optional glued key-id) and must NOT be
    # treated as a passthrough letter.
    check("reuse_message_ref extracts -aC<ref> (short-cluster prefix, plain)",
          reuse_message_ref(calls("git commit -aCabc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts --amend -aC<ref> (short-cluster prefix)",
          reuse_message_ref(calls("git commit --amend -aCabc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts -vC<ref> (verbose + reuse)",
          reuse_message_ref(calls("git commit -vCabc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts -nC<ref> (no-verify + reuse)",
          reuse_message_ref(calls("git commit -nCabc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts -eC<ref> (edit + reuse)",
          reuse_message_ref(calls("git commit -eCabc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts -sC<ref> (signoff + reuse)",
          reuse_message_ref(calls("git commit -sCabc123")[0][1]) == "abc123")
    check("reuse_message_ref extracts -avC<ref> (two stacked passthrough flags)",
          reuse_message_ref(calls("git commit -avCabc123")[0][1]) == "abc123")
    check("has_opt detects -ac<ref> (short-cluster prefix) as reedit",
          has_opt(calls("git commit -acabc123")[0][1], REEDIT_OPTS))
    check("opt_value: -amMSG (short-cluster prefix) extracts message",
          opt_value("-amMSG", MESSAGE_OPTS) == "MSG")
    check("opt_value: -aFpath.txt (short-cluster prefix) extracts file path",
          opt_value("-aFpath.txt", FILE_OPTS) == "path.txt")
    check("opt_value: -SC<ref> is NOT parsed as cluster + -C (S is not a "
          "passthrough letter — it takes its own optional glued key-id, "
          "verified live: git treats -SC<ref> as -S with key-id 'C<ref>')",
          opt_value("-SCabc123", REUSE_OPTS) is None)
    check("opt_value: -Sabc123 alone (no cluster letter) is not misread as -C",
          opt_value("-Sabc123", REUSE_OPTS) is None)
    check("opt_value: bare -a with nothing after is not a message-affecting "
          "match (no letter to stop on)",
          opt_value("-a", REUSE_OPTS) is None)
    check("opt_value: -ax<ref> (x not a recognized passthrough or message "
          "letter) does not match — bail out rather than misparse",
          opt_value("-axabc123", REUSE_OPTS) is None)

    # round 4 (#216, 4th review, reviewer-caught): long-option unambiguous-
    # prefix abbreviation bypass — git accepts any unambiguous prefix of a
    # long option name; opt_value's long-form branch previously did exact
    # equality only, so an abbreviated spelling bypassed hygiene checking
    # entirely. Verified live against real git commit (git 2.50). The four
    # names this hook cares about do not prefix-collide with each other.
    check("opt_value: --mess=<text> abbreviates --message",
          opt_value("--mess=hello", MESSAGE_OPTS) == "hello")
    check("opt_value: --m=<text> abbreviates --message (shortest unambiguous "
          "prefix among this hook's four names)",
          opt_value("--m=hello", MESSAGE_OPTS) == "hello")
    check("opt_value: --fil=<path> abbreviates --file",
          opt_value("--fil=path.txt", FILE_OPTS) == "path.txt")
    check("opt_value: --reuse-mess=<ref> abbreviates --reuse-message",
          opt_value("--reuse-mess=abc123", REUSE_OPTS) == "abc123")
    check("opt_value: --reedit-mess=<ref> abbreviates --reedit-message",
          opt_value("--reedit-mess=abc123", REEDIT_OPTS) == "abc123")
    check("opt_value: --reuse-mess=<ref> does not also match REEDIT_OPTS "
          "(the four names don't cross-collide)",
          opt_value("--reuse-mess=abc123", REEDIT_OPTS) is None)
    check("opt_value: --reedit-mess=<ref> does not also match REUSE_OPTS",
          opt_value("--reedit-mess=abc123", REUSE_OPTS) is None)
    check("has_opt detects --reuse-mess=<ref> abbreviation",
          has_opt(["--reuse-mess=abc123"], REUSE_OPTS))
    check("reuse_message_ref extracts --reuse-mess=<ref> abbreviation",
          reuse_message_ref(calls("git commit --amend --reuse-mess=abc123")[0][1]) == "abc123")
    check("has_opt detects --reedit-mess=<ref> abbreviation as reedit",
          has_opt(calls("git commit --reedit-mess=abc123")[0][1], REEDIT_OPTS))
    check("opt_value: unrelated long option is not treated as an abbreviation "
          "(--amend is not a prefix of any of the four names)",
          opt_value("--amend=x", MESSAGE_OPTS) is None)

    # opt_value / has_opt: every form x every message-affecting flag, per the
    # design-doc's exhaustive enumeration (space / `=`-joined long / glued
    # short), verified against real `git commit` behaviour (git 2.50).
    check("opt_value: -m space form has no single-token value (caller reads next tok)",
          opt_value("-m", MESSAGE_OPTS) is None)
    check("opt_value: -mMSG glued",
          opt_value("-mMSG", MESSAGE_OPTS) == "MSG")
    check("opt_value: --message=MSG joined",
          opt_value("--message=MSG", MESSAGE_OPTS) == "MSG")
    check("opt_value: -m=MSG short opt keeps the literal '=' (git does NOT "
          "strip it for short options — verified against real git commit)",
          opt_value("-m=MSG", MESSAGE_OPTS) == "=MSG")
    check("opt_value: -Fpath.txt glued",
          opt_value("-Fpath.txt", FILE_OPTS) == "path.txt")
    check("opt_value: --file=path.txt joined",
          opt_value("--file=path.txt", FILE_OPTS) == "path.txt")
    check("opt_value: -Cabc123 glued",
          opt_value("-Cabc123", REUSE_OPTS) == "abc123")
    check("opt_value: --reuse-message=abc123 joined",
          opt_value("--reuse-message=abc123", REUSE_OPTS) == "abc123")
    check("opt_value: -cabc123 glued",
          opt_value("-cabc123", REEDIT_OPTS) == "abc123")
    check("opt_value: --reedit-message=abc123 joined",
          opt_value("--reedit-message=abc123", REEDIT_OPTS) == "abc123")
    check("opt_value: unrelated flag returns None",
          opt_value("--amend", MESSAGE_OPTS) is None)
    check("has_opt: true for space form",
          has_opt(["--reuse-message", "abc123"], REUSE_OPTS))
    check("has_opt: true for joined form",
          has_opt(["--reuse-message=abc123"], REUSE_OPTS))
    check("has_opt: true for glued form",
          has_opt(["-Cabc123"], REUSE_OPTS))
    check("has_opt: false when absent",
          not has_opt(["-m", "msg"], REUSE_OPTS))
    check("has_opt: stops at -- separator",
          not has_opt(["--", "-Cabc123"], REUSE_OPTS))

    # -F/--file value-token recognition (all 3 forms) — the path itself must
    # be skipped as a message/positional path in every form, even though its
    # file content is out of scope (see split_paths_and_messages docstring).
    _, mF1 = split_paths_and_messages(calls("git commit -Fmsgfile.txt")[0][1])
    pF1, _ = split_paths_and_messages(calls("git commit -Fmsgfile.txt")[0][1])
    check("commit -Fmsgfile.txt glued: not misread as a message or a path",
          mF1 == [] and pF1 == [])
    pF2, _ = split_paths_and_messages(calls("git commit --file=msgfile.txt")[0][1])
    check("commit --file=msgfile.txt joined: not misread as a path",
          pF2 == [])
    pF3, _ = split_paths_and_messages(calls("git commit -F msgfile.txt")[0][1])
    check("commit -F msgfile.txt space form: not misread as a path",
          pF3 == [])

    # branch model
    check("version/ create flagged",
          created_branch_name("switch", tokenize("-c version/9.9 dev")[0:]) == "version/9.9"
          or created_branch_name(*calls("git switch -c version/9.9 dev")[0]) == "version/9.9")
    check("checkout -b version flagged",
          created_branch_name(*calls("git checkout -b version/9.9")[0]) == "version/9.9")
    check("ordinary feature branch ok",
          not (created_branch_name(*calls("git switch -c feature/login")[0]) or "").startswith("version/"))
    check("branch -d not a create",
          created_branch_name(*calls("git branch -d old")[0]) is None)

    # Edit/Write managed-path closure (#216): direct Edit/Write of a managed
    # path must be flagged exactly like `git add`/`commit` of the same path;
    # an ordinary deliverable path must not be.
    check("Edit on .claude/ flagged",
          is_managed(relative_to_project("/proj", "/proj/.claude/hooks/x.sh"), globs))
    check("Write on CLAUDE.md flagged",
          is_managed(relative_to_project("/proj", "/proj/CLAUDE.md"), globs))
    check("Edit on docs/grimoire/ flagged",
          is_managed(relative_to_project("/proj", "/proj/docs/grimoire/design/x.md"), globs))
    check("Edit on relative .claude path flagged",
          is_managed(relative_to_project("/proj", ".claude/settings.json"), globs))
    check("Edit on deliverable src/ not flagged",
          not is_managed(relative_to_project("/proj", "/proj/src/app.py"), globs))
    check("Edit on project's own docs/api.md not flagged",
          not is_managed(relative_to_project("/proj", "/proj/docs/api.md"), globs))

    print("\n" + ("PASS" if not fails else f"{fails} FAILED"))
    return 1 if fails else 0


# ── live self-test (real scratch git repo + config; exercises main() end to
# end via stdin, matching how the harness actually invokes this hook) ───────
def _self_test_live():
    import tempfile

    fails = 0

    def check(desc, cond):
        nonlocal fails
        print(("ok  " if cond else "FAIL") + "  " + desc)
        fails += not cond

    def run_hook(proj, payload):
        r = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": proj}, timeout=10,
        )
        return r.returncode, r.stderr

    with tempfile.TemporaryDirectory() as proj:
        subprocess.run(["git", "init", "-q", proj], check=True)
        subprocess.run(["git", "-C", proj, "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", proj, "config", "user.name", "Test"], check=True)
        os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
        with open(os.path.join(proj, ".claude", "grimoire-config.json"), "w") as f:
            json.dump({"stealth-mode": {"value": "on", "acknowledged-risk": True}}, f)
        with open(os.path.join(proj, "README.md"), "w") as f:
            f.write("clean message with tell\n")
        subprocess.run(["git", "-C", proj, "add", "README.md"], check=True)
        subprocess.run(["git", "-C", proj, "commit", "-q", "-m", "chore: per Claude tweak"],
                        check=True)  # plant a dirty HEAD message directly (bypassing the
                                     # hook, as if it landed before stealth was ever on)
        dirty_sha = subprocess.run(
            ["git", "-C", proj, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()

        # --- edge 1: commit --amend hygiene closure ---
        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit --amend"},
        })
        check("live: bare-editor amend denied", rc == 2 and "amend" in err.lower())

        rc, _ = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit --amend -m "clean rewritten message"'},
        })
        check("live: amend with clean -m allowed", rc == 0)

        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit --amend -m "per Claude again"'},
        })
        check("live: amend with dirty -m denied", rc == 2 and "tell" in err.lower())

        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit --amend --no-edit"},
        })
        check("live: amend --no-edit reusing dirty HEAD message denied",
              rc == 2 and "no-edit" in err.lower())

        # Plant a second, clean-message commit on top (HEAD moves off the
        # dirty commit, but dirty_sha still resolves it directly) so the
        # reuse-message tests below have both a clean-message ref (clean_sha
        # / new HEAD) and a dirty-message ref (dirty_sha) to reuse from.
        with open(os.path.join(proj, "README.md"), "a") as f:
            f.write("more\n")
        subprocess.run(["git", "-C", proj, "add", "README.md"], check=True)
        subprocess.run(["git", "-C", proj, "commit", "-q", "-m", "chore: normal bugfix"],
                        check=True)
        clean_sha = subprocess.run(
            ["git", "-C", proj, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()

        # --- edge 1b: reuse-message bypass closure (reviewer-caught) ---
        # -C/--reuse-message=<ref> reuses <ref>'s message verbatim (no editor)
        # but was never resolved or hygiene-checked at all — a live bypass.
        rc, _ = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --amend -C {clean_sha}"},
        })
        check("live: amend -C <clean-message-sha> allowed", rc == 0)

        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --amend -C {dirty_sha}"},
        })
        check("live: amend -C <dirty-message-sha> denied",
              rc == 2 and "tell" in err.lower())

        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --amend --reuse-message={dirty_sha}"},
        })
        check("live: amend --reuse-message=<dirty-message-sha> denied",
              rc == 2 and "tell" in err.lower())

        rc, _ = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -C {clean_sha}"},
        })
        check("live: plain (non-amend) commit -C <clean-message-sha> allowed", rc == 0)

        # -c/--reedit-message=<ref> always opens the editor afterward (per
        # `git commit --help`), so — like a bare editor amend — it is denied
        # outright rather than treated as pre-checkable.
        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --amend -c {clean_sha}"},
        })
        check("live: amend -c <ref> (reedit) denied regardless of message hygiene",
              rc == 2 and "reedit" in err.lower())

        # --- edge 1c: glued short-flag bypass closure (round 3, this fix) ---
        # -C<ref>/-c<ref> with NO separator at all — e.g. `git commit
        # -C1a2b3c4` — were missed entirely by both reuse_message_ref and the
        # is_reedit check (they only recognized `=`-joined and space-
        # separated forms). This is the live reproduction the reviewer asked
        # for: real scratch-repo commits reused via the actual glued syntax,
        # driven through the installed hook exactly as the harness invokes
        # it (stdin JSON, not calling an internal function directly).
        #
        # Plain (non-amend), glued -C<clean-sha>: allowed.
        rc, _ = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -C{clean_sha}"},
        })
        check("live: plain glued -C<clean-message-sha> allowed", rc == 0)

        # Plain (non-amend), glued -C<dirty-sha>: THE reported bypass — must
        # now be denied. Before this fix: reuse_message_ref returned None for
        # this token (no "=" and not an exact "-C" match), so the message was
        # never resolved or hygiene-checked, and the hook exited 0.
        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -C{dirty_sha}"},
        })
        check("live: plain glued -C<dirty-message-sha> denied (was a full bypass)",
              rc == 2 and "tell" in err.lower())

        # --amend, glued -C<dirty-sha>: same bypass, amend form.
        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --amend -C{dirty_sha}"},
        })
        check("live: amend glued -C<dirty-message-sha> denied",
              rc == 2 and "tell" in err.lower())

        # Plain (non-amend), glued -c<ref>: THE other reported bypass — with
        # GIT_EDITOR=true (a realistic non-interactive agent shell), the
        # reused message lands unedited. Before this fix: is_reedit's
        # membership/`.startswith("--reedit-message=")` check missed this
        # token entirely, so the commit was allowed through unchecked
        # (accidentally not hygiene-checked at all, not even resolved).
        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -c{dirty_sha}"},
        })
        check("live: plain glued -c<ref> (reedit) denied outright (was a full bypass)",
              rc == 2 and "reedit" in err.lower())

        # --amend, glued -c<ref>: same reedit-deny, amend form.
        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --amend -c{clean_sha}"},
        })
        check("live: amend glued -c<ref> (reedit) denied regardless of message hygiene",
              rc == 2 and "reedit" in err.lower())

        # --- edge 1d: short-cluster prefix bypass closure (round 4, this
        # fix, reviewer-caught) --- `-C`/`-c` glued after a leading, unrelated
        # boolean short-flag prefix, e.g. `-aC<ref>` — real, live bypass
        # confirmed before this fix: `git commit -aC<dirty-sha>` and
        # `git commit --amend -aC<dirty-sha>` both committed the dirty
        # message verbatim, hook exiting 0 (opt_value only checked t[:2], so
        # it recognized bare `-Cref` but not `-aCref`).
        rc, _ = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -aC{clean_sha}"},
        })
        check("live: plain -aC<clean-message-sha> (short-cluster prefix) allowed", rc == 0)

        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -aC{dirty_sha}"},
        })
        check("live: plain -aC<dirty-message-sha> (short-cluster prefix) denied "
              "(was a full bypass)", rc == 2 and "tell" in err.lower())

        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --amend -aC{dirty_sha}"},
        })
        check("live: amend -aC<dirty-message-sha> (short-cluster prefix) denied "
              "(was a full bypass)", rc == 2 and "tell" in err.lower())

        # -S is deliberately NOT a passthrough letter (it takes its own
        # optional glued key-id) — `-SC<ref>` must NOT be misparsed as a
        # message-reuse form. This is not itself a bypass to close (a bogus
        # gpg key-id just fails the commit); the live check here confirms the
        # guard does not misfire and incorrectly deny/hang on this unrelated,
        # legitimate-shaped invocation. GIT_EDITOR=true avoids blocking on an
        # interactive editor if the guard were to (wrongly) allow it through.
        rc, _ = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -SC{dirty_sha}"},
        })
        check("live: -SC<ref> (gpg-sign, NOT a cluster+reuse form) is not "
              "misparsed as a message-reuse bypass by this guard", rc == 0)

        # --- edge 1e: long-option unambiguous-prefix abbreviation bypass
        # closure (round 4, this fix, reviewer-caught) --- git accepts any
        # unambiguous prefix of a long option name. Real, live bypass
        # confirmed before this fix: `--mess=<dirty text>`,
        # `--reuse-mess=<dirty-sha>` both bypassed hygiene checking entirely
        # (opt_value's long-form branch did exact string equality only).
        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit --mess="per Claude abbrev test"'},
        })
        check("live: --mess=<dirty text> (abbreviates --message) denied "
              "(was a full bypass)", rc == 2 and "tell" in err.lower())

        rc, _ = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit --mess="clean abbrev message"'},
        })
        check("live: --mess=<clean text> (abbreviates --message) allowed", rc == 0)

        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --amend --reuse-mess={dirty_sha}"},
        })
        check("live: --amend --reuse-mess=<dirty-message-sha> (abbreviates "
              "--reuse-message) denied (was a full bypass)",
              rc == 2 and "tell" in err.lower())

        rc, _ = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -C{clean_sha}"},
        })
        check("live: --reuse-mess=<clean-message-sha> abbreviation control "
              "(plain -C<clean> still allowed, sanity check)", rc == 0)

        rc, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit --reedit-mess={clean_sha}"},
        })
        check("live: --reedit-mess=<ref> (abbreviates --reedit-message) "
              "denied outright regardless of message hygiene",
              rc == 2 and "reedit" in err.lower())

        # --- edge 2: Edit/Write managed-path closure ---
        rc, err = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": os.path.join(proj, "CLAUDE.md"),
                            "old_string": "a", "new_string": "b"},
        })
        check("live: Edit on CLAUDE.md denied", rc == 2 and "managed" in err.lower())

        rc, err = run_hook(proj, {
            "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(proj, ".claude", "hooks", "new.sh"),
                           "content": "#!/bin/sh\n"},
        })
        check("live: Write under .claude/ denied", rc == 2 and "managed" in err.lower())

        rc, _ = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": os.path.join(proj, "src", "app.py"),
                            "old_string": "a", "new_string": "b"},
        })
        check("live: Edit on deliverable path allowed", rc == 0)

        # stealth OFF: Edit/Write on a managed path must be untouched (no-op)
        with open(os.path.join(proj, ".claude", "grimoire-config.json"), "w") as f:
            json.dump({"stealth-mode": {"value": "off", "acknowledged-risk": False}}, f)
        rc, _ = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": os.path.join(proj, "CLAUDE.md"),
                            "old_string": "a", "new_string": "b"},
        })
        check("live: stealth off -> Edit on CLAUDE.md is a no-op (allowed)", rc == 0)

    print("\n" + ("PASS" if not fails else f"{fails} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test-live":
        sys.exit(_self_test_live())
    main()
