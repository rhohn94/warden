#!/usr/bin/env python3
"""Push guard (marker + whitelist gated).

PreToolUse Bash hook. Blocks `git push` unless BOTH:
  (a) the active worktree carries `.claude/integration-allow.local` (the
      blessed integration worktree), AND
  (b) every ref being pushed is on the project's push allowlist.

Default allowlist:  `main`, `dev`, and any version tag matching
`^v?\\d+(\\.\\d+){0,3}(-...)?$` (e.g. `1.4`, `v1.4`, `1.4.2-rc1`).

Project additions: `$CLAUDE_PROJECT_DIR/.claude/push-allowlist`, one ref
name per line; `#` comments and blank lines OK.

Without the marker, all pushes deny (preserve "agent never pushes" default
for unblessed worktrees). With the marker but the ref off-list, the push
denies — add the ref to `.claude/push-allowlist` or push a whitelisted ref.
Destructive or broad flags (`--force` / `--force-with-lease` / `--all` /
`--mirror` / `--delete` / `--prune`) deny even with the marker; have the
human run them themselves if truly intended. Remote-ref deletion
(`git push origin :branch`) likewise denies.

Parsing: the command is tokenized with shlex in `punctuation_chars=True`
mode (see `tokenize`), so shell operators (`;`, `&&`, `||`, `|`, `&`,
`(`, `)`) and redirections (`>`, `2>&1`, …) are emitted as their own
tokens even when glued to a word. `parse_push` then reads only the single
`git push` simple command — it stops at the first statement separator and
skips redirection plumbing — so a block such as
`git push origin main && echo done` or `git push origin a; git push origin b`
is no longer mis-parsed (the trailing tokens were previously treated as
extra refspecs and tripped the allowlist). `#` comments are honoured. If
the whole command fails to tokenize (rare — usually a heredoc with embedded
triple-quotes leaving an unbalanced quote), each line is parsed
independently; if any parses as a `git push`, the hook applies its checks
to that line. Lines that themselves fail to parse are skipped — a
false-negative on a contrived unparseable push is preferred over a
false-positive on a legitimate `git tag -m "...push..."`-style command.

Detached HEAD / non-repo / non-push commands ⇒ no-op (exit 0).
"""
import json
import os
import re
import shlex
import subprocess
import sys

PUSH_SUBCMDS = {"push", "send-pack"}

# Shell control operators that terminate the current simple command. When
# parsing a `git push` out of a multi-statement block, scanning stops here so
# a following `echo done` / second `git push` isn't misread as a refspec.
# These appear as standalone tokens because we tokenize with
# `punctuation_chars=True` (see `tokenize`), which splits operators off
# adjacent words even when unquoted and unspaced (e.g. `main;`, `main&`).
STATEMENT_SEPARATORS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")", "\n"}

# Redirection operator tokens (`punctuation_chars` mode emits these split from
# their fd/target). A bare redirection operator is followed by its target
# token (a filename or fd number) which must also be skipped; a leading fd
# digit (`2` in `2>&1`) is emitted as its own token just before the operator.
REDIRECT_OPS = {">", ">>", "<", "<<", "<<<", ">&", "<&", "<>", ">|", "&>", "&>>"}

# Global `git -X val` options (between `git` and the subcommand).
GLOBAL_OPTS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "-c"}

# `git push -X val` options that consume the next token as a value.
PUSH_OPTS_WITH_VALUE = {"--receive-pack", "--exec", "--repo", "-o",
                        "--push-option", "--signed"}

# Flags that deny even with the marker (destructive / too-broad).
DENIED_FLAGS = {"-f", "--force", "--force-with-lease", "--force-if-includes",
                "--all", "--mirror", "--delete", "-d", "--prune"}

# Branches always allowed when the marker is present.
DEFAULT_ALLOWLIST = {"main", "dev"}

# Version-tag pattern: 1.4, v1.4, 1.4.2, 1.4.2-rc1, etc.
TAG_PATTERN = re.compile(r"^v?\d+(\.\d+){0,3}(-[A-Za-z0-9._-]+)?$")

# PR-head pattern (v3.5, github-pr): version staging + lane branches. These are
# allowlisted for push ONLY when `github-pr.enabled` is true in grimoire-config
# — so a `version/{X.Y}` (or `version/{X.Y}/<lane>`) head can be pushed to open a
# PR. This widens the ref allowlist only; the marker requirement, destructive-flag
# denial, and the human-gate (propose-and-wait / autonomous-push) are unchanged.
PR_HEAD_PATTERN = re.compile(r"^version/.+$")


def github_pr_enabled(proj: str) -> bool:
    """True iff .claude/grimoire-config.json has github-pr.enabled == true."""
    if not proj:
        return False
    try:
        with open(os.path.join(proj, ".claude", "grimoire-config.json")) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return False
    block = cfg.get("github-pr")
    if not isinstance(block, dict):
        return False
    enabled = block.get("enabled")
    if isinstance(enabled, dict):
        enabled = enabled.get("value")
    return enabled is True


def tokenize(text: str) -> list[str]:
    """shlex word-split with shell operators emitted as their own tokens.

    `punctuation_chars=True` makes shlex recognize `;`, `&`, `|`, `(`, `)` and
    the compound operators (`&&`, `||`, `;;`) as standalone tokens even when
    glued to a word (`main;` → `main`, `;`). `whitespace_split=True` keeps
    ordinary words and quoted strings intact (so `git tag -m "...push..."`
    stays one token). `commenters="#"` honours bash `#` comments. Raises
    ValueError on an unterminated quote, like `shlex.split`.
    """
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    return list(lex)


def locate_push(cmd: str) -> tuple[list[str], int | None]:
    """Return (tokens, push_idx) — tokens for the parse we'll act on.

    Tries the whole command first. If tokenizing fails (e.g. a heredoc with
    embedded triple-quotes leaving an unbalanced quote), retries per line so a
    single misparseable line doesn't poison the rest. Returns ([], None) if
    no parseable line contains a `git push`.
    """
    try:
        tokens = tokenize(cmd)
        idx = find_push(tokens)
        if idx is not None:
            return tokens, idx
        return tokens, None
    except ValueError:
        pass
    for line in cmd.splitlines():
        try:
            line_tokens = tokenize(line)
        except ValueError:
            continue
        idx = find_push(line_tokens)
        if idx is not None:
            return line_tokens, idx
    return [], None


def find_all_pushes(tokens: list[str]) -> list[int]:
    """Return indices of every `push`/`send-pack` subcommand token.

    A single Bash block may contain more than one `git push` (e.g.
    `git push origin main && git push origin dev`); each must be validated
    independently, so we collect them all rather than stopping at the first.
    """
    pushes: list[int] = []
    i, n = 0, len(tokens)
    while i < n:
        if tokens[i] == "git" or tokens[i].endswith("/git"):
            j = i + 1
            while j < n:
                t = tokens[j]
                if t in GLOBAL_OPTS_WITH_VALUE:
                    j += 2
                    continue
                if t.startswith("--") and "=" in t and t.split("=", 1)[0] in GLOBAL_OPTS_WITH_VALUE:
                    j += 1
                    continue
                if t.startswith("-"):
                    j += 1
                    continue
                if t in PUSH_SUBCMDS:
                    pushes.append(j)
                break  # first bare token is the subcommand (push or other)
            i = j
        i += 1
    return pushes


def find_push(tokens: list[str]) -> int | None:
    """Return index of the first `push`/`send-pack` subcommand token, or None."""
    pushes = find_all_pushes(tokens)
    return pushes[0] if pushes else None


def parse_push(tokens: list[str], push_idx: int):
    """Return (flag_set, remote_or_None, refspec_list) for the push call.

    Scans only the single `git push` simple command: it stops at the first
    shell statement separator (`;`, `&&`, `||`, `|`, `&`, newline) so a
    trailing `echo ...` or a second `git push` in the same block is not
    mistaken for a refspec, and it skips redirection plumbing (`2>&1`, `>`,
    `>> file`, etc.) for the same reason. This is the v1.7 A1 hardening.
    """
    flags: set[str] = set()
    positionals: list[str] = []
    i, n = push_idx + 1, len(tokens)
    while i < n:
        t = tokens[i]
        if t in STATEMENT_SEPARATORS:
            break  # end of this `git push` simple command
        if t in REDIRECT_OPS:
            # Redirection: skip the operator and its target token (filename or
            # fd), e.g. `> out.log`, `2 >& 1`. A bare leading fd digit (the `2`
            # in `2>&1`) was already swallowed below as a stray positional, so
            # drop it retroactively if it precedes a redirect operator.
            if positionals and positionals[-1].isdigit():
                positionals.pop()
            i += 2  # operator + its target token
            continue
        if t in PUSH_OPTS_WITH_VALUE:
            flags.add(t)
            i += 2
            continue
        if t.startswith("--") and "=" in t:
            flags.add(t.split("=", 1)[0])
            i += 1
            continue
        if t.startswith("-") and not t.startswith("--") and len(t) > 2:
            # Combined short flags, e.g. `-fv`.
            for c in t[1:]:
                flags.add(f"-{c}")
            i += 1
            continue
        if t.startswith("-"):
            flags.add(t)
            i += 1
            continue
        positionals.append(t)
        i += 1
    remote = positionals[0] if positionals else None
    refspecs = positionals[1:] if len(positionals) > 1 else []
    return flags, remote, refspecs


def load_allowlist(proj: str) -> set[str]:
    """Defaults ∪ contents of $CLAUDE_PROJECT_DIR/.claude/push-allowlist."""
    al = set(DEFAULT_ALLOWLIST)
    path = os.path.join(proj, ".claude", "push-allowlist")
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    al.add(line)
    except OSError:
        pass
    return al


def current_branch(repo: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def normalize_ref(name: str) -> str:
    for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def ref_dest(refspec: str) -> tuple[str, bool]:
    """Return (dest_name, is_delete). For `<src>:<dst>` returns dst; for
    `:<dst>` flags a deletion; plain `<ref>` returns ref."""
    if ":" in refspec:
        src, dst = refspec.split(":", 1)
        if not src:
            return normalize_ref(dst), True
        return normalize_ref(dst), False
    return normalize_ref(refspec), False


def is_allowed_ref(name: str, allowlist: set[str], pr_heads: bool = False) -> bool:
    if name in allowlist or bool(TAG_PATTERN.match(name)):
        return True
    # github-pr (v3.5): allow PR-head branches only when the dial is enabled.
    return pr_heads and bool(PR_HEAD_PATTERN.match(name))


def deny(msg: str) -> None:
    sys.stderr.write("push-guard: " + msg)
    sys.exit(2)


def audit_log(proj: str, cmd: str) -> None:
    """Append an approval record to .claude/cache/push-audit.log (v1.30, #64).

    Best-effort, append-only audit trail of pushes the guard *permitted* (a
    PreToolUse approval, recorded before the push runs). Never raises — a
    logging failure must not break the guard. The log is gitignorable; it
    documents what the marker-blessed worktree was allowed to push, and when.
    """
    try:
        import datetime
        cache = os.path.join(proj, ".claude", "cache")
        os.makedirs(cache, exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        line = f"{ts}\tPERMITTED\t{cmd.strip().splitlines()[0][:300]}\n"
        with open(os.path.join(cache, "push-audit.log"), "a") as fh:
            fh.write(line)
    except Exception:
        pass


def validate_push(tokens: list[str], push_idx: int, proj: str,
                  allowlist: set[str], pr_heads: bool = False) -> None:
    """Apply the flag / allowlist / deletion checks to one `git push` call.

    Calls `deny()` (which exits non-zero) on any violation; returns normally
    if this push is permitted. `main` calls it once per push found in the
    block so a second `git push` in the same command is checked too.
    """
    flags, _remote, refspecs = parse_push(tokens, push_idx)

    bad_flags = flags & DENIED_FLAGS
    if bad_flags:
        deny(
            f"blocked `git push` with {' '.join(sorted(bad_flags))}.\n"
            "Destructive or broad flags (--force / --force-with-lease /\n"
            "--all / --mirror / --delete / --prune) require explicit human\n"
            "confirmation outside of Claude. Push the specific ref(s)\n"
            "without these flags, or have the human run the destructive\n"
            "push themselves.\n"
        )

    # `--tags` with no refspecs pushes only tags. Tags are immutable by
    # convention; allow under the marker without further whitelist checks.
    if "--tags" in flags and not refspecs:
        return

    if not refspecs:
        # `git push`, `git push <remote>`, `git push <remote> --follow-tags`.
        # Implicit push of the current branch (per upstream config).
        cur = current_branch(proj)
        if cur is None:
            deny(
                "blocked `git push` from detached HEAD.\n"
                "Push explicit refs (e.g. `git push origin main`).\n"
            )
        if not is_allowed_ref(cur, allowlist, pr_heads):
            deny(
                f"blocked `git push` — current branch `{cur}` is not on the\n"
                "push allowlist.\n"
                "  Default allowlist: main, dev, and version tags.\n"
                "  Project allowlist: .claude/push-allowlist (tracked).\n"
                f"Push an allowed ref explicitly, or add `{cur}` to the\n"
                "allowlist if this push is intended project policy.\n"
            )
        return

    for refspec in refspecs:
        dest, is_delete = ref_dest(refspec)
        if is_delete:
            deny(
                f"blocked `git push` — refusing to delete remote ref "
                f"`{dest}` ({refspec!r}).\n"
                "Remote-ref deletion is destructive; have the human run\n"
                "it themselves if truly intended.\n"
            )
        if not is_allowed_ref(dest, allowlist, pr_heads):
            deny(
                f"blocked `git push` — ref `{dest}` is not on the push\n"
                "allowlist.\n"
                "  Default allowlist: main, dev, and version tags (e.g. v1.4).\n"
                "  Project allowlist: .claude/push-allowlist (tracked).\n"
                f"Add `{dest}` to the allowlist if this push is intended\n"
                "project policy.\n"
            )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if payload.get("tool_name", "") != "Bash":
        sys.exit(0)

    cmd = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    tokens, push_idx = locate_push(cmd)
    if push_idx is None:
        sys.exit(0)

    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    marker_present = bool(proj) and os.path.isfile(
        os.path.join(proj, ".claude", "integration-allow.local"))
    if not marker_present:
        deny(
            "blocked `git push`.\n"
            "Pushing to a remote is restricted: this worktree has no\n"
            ".claude/integration-allow.local marker, so it is NOT the\n"
            "blessed integration worktree. Task agents do not push.\n"
            "If a push is genuinely wanted, run it from the integration\n"
            "worktree, or ask the human to push themselves.\n"
        )

    allowlist = load_allowlist(proj)
    pr_heads = github_pr_enabled(proj)   # github-pr (v3.5): widen ref allowlist only
    # Validate every `git push` in the block — a block may chain several
    # (`git push origin main && git push origin dev`); each is checked.
    for idx in find_all_pushes(tokens):
        validate_push(tokens, idx, proj, allowlist, pr_heads)
    # All pushes in the block passed the guard — record the approval (#64).
    audit_log(proj, cmd)
    sys.exit(0)


def _self_test() -> int:
    """Parser-level self-test (no git / marker needed): run with --self-test.

    Documents the v1.7 A1 fix — multi-statement blocks must parse out only the
    real `git push` refspecs. Returns process exit code (0 = all pass)."""
    def parsed(cmd):
        toks = tokenize(cmd)
        return [(parse_push(toks, i)[1], parse_push(toks, i)[2])
                for i in find_all_pushes(toks)]

    # (command, [(remote, [refspecs]), ...] expected per push)
    cases = [
        ("git push origin main", [("origin", ["main"])]),
        ("git push origin main && echo done", [("origin", ["main"])]),
        ("git push origin main; echo done", [("origin", ["main"])]),
        ("git push origin main 2>&1", [("origin", ["main"])]),
        ("git push origin main > out.log 2>&1", [("origin", ["main"])]),
        ("git push origin main >out.log", [("origin", ["main"])]),
        ("git push origin main | tee log", [("origin", ["main"])]),
        ("git push origin main || true", [("origin", ["main"])]),
        ("git push origin main && git push origin dev",
         [("origin", ["main"]), ("origin", ["dev"])]),
        ("git push origin main; git push origin badref",
         [("origin", ["main"]), ("origin", ["badref"])]),
        ('echo "git push origin x"', []),  # push only inside a quoted arg
        ('git tag -m "do not push" v1.7', []),
    ]
    failures = 0
    for cmd, expected in cases:
        got = parsed(cmd)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {cmd!r} -> {got}")

    # github-pr (v3.5) ref-allowance: PR-head branches are allowed ONLY when the
    # dial is on; the default allowlist + destructive-flag denial are unchanged.
    ref_cases = [
        # (name, allowlist, pr_heads_on, expected_allowed)
        ("main", DEFAULT_ALLOWLIST, False, True),
        ("dev", DEFAULT_ALLOWLIST, False, True),
        ("v3.5", DEFAULT_ALLOWLIST, False, True),          # version tag
        ("version/3.5", DEFAULT_ALLOWLIST, False, False),  # PR head, dial OFF -> denied
        ("version/3.5", DEFAULT_ALLOWLIST, True, True),     # PR head, dial ON  -> allowed
        ("version/3.5/lane-a", DEFAULT_ALLOWLIST, True, True),  # lane head, dial ON
        ("feature/x", DEFAULT_ALLOWLIST, True, False),      # non-PR branch, still denied
        ("version", DEFAULT_ALLOWLIST, True, False),        # bare 'version' is not a PR head
    ]
    for name, al, on, exp in ref_cases:
        got = is_allowed_ref(name, al, on)
        ok = got == exp
        failures += not ok
        print(("ok  " if ok else "FAIL")
              + f"  is_allowed_ref({name!r}, pr_heads={on}) -> {got} (want {exp})")
    # Destructive flags must stay denied regardless of github-pr.
    assert "--force" in DENIED_FLAGS and "--delete" in DENIED_FLAGS, "denied flags intact"

    print(f"\n{'PASS' if not failures else str(failures)+' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    main()
