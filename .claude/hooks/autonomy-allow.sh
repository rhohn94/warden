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
# HOOK_CONTRACT: v1 capabilities=[autonomy-allow-noir,autonomous-push]
"""Autonomy allow (PreToolUse: Bash) — paradigm-aware prompt suppression (v3.63).

Under the NOIR paradigm, auto-approves (permissionDecision: "allow") the
guard-vetted, non-destructive commands the release pipeline runs constantly —
so an autonomous release (grm-orchestrate-release / grm-integration-master)
proceeds without permission prompts. This generalizes the pattern push-guard.sh
established for `git push`: a static settings.json allowlist entry cannot tell
the paradigms apart, but a hook can. These are framework-internal design
specs — see the upstream Grimoire repository for that rationale.

Safety model — this hook NEVER weakens enforcement:
  - It fires only when work-paradigm.value == "Noir" (Supervised / Weiss keep
    every prompt they have today). Optional kill-switch:
    autonomy-allow.enabled: false in grimoire-config.json.
  - In Claude Code, a DENY (exit 2) from any other PreToolUse hook takes
    precedence over an allow — so protected-branch-guard, worktree-guard,
    push-guard, and stealth-guard still block everything they block today.
    This hook only decides whether a command that PASSES the guards also
    needs a human click.
  - The allow set is a closed whitelist: non-destructive git subcommands,
    `just` targets, framework scripts (python3 …/.claude/skills/…​.py),
    `gh` read/tracker operations, and read-only pipeline filters.
  - Never auto-allowed (falls through to the normal permission flow):
    `git push` (owned by push-guard's own paradigm-aware suppression),
    history rewriting (rebase / cherry-pick / reset), `git clean`,
    force flags anywhere, shell redirections, and push-class `gh` ops
    (pr create/merge, release create/…) unless autonomous-push.enabled.
  - A compound command (&&, ;, |) is auto-allowed only if EVERY statement
    is in the allow set; otherwise the whole command falls through.
"""
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_common import _scalar, read_config  # noqa: E402

# Non-destructive git subcommands the pipeline runs routinely. Mutating ones
# in this set (commit / merge / switch / branch / worktree / pull / tag) are
# all gated by protected-branch-guard.sh's deny rules, which take precedence.
GIT_ALLOWED = {
    "status", "log", "diff", "show", "add", "commit", "merge", "switch",
    "checkout", "branch", "tag", "fetch", "pull", "remote", "merge-base",
    "rev-parse", "rev-list", "symbolic-ref", "describe", "ls-files",
    "ls-remote", "for-each-ref", "worktree", "stash", "mv", "shortlog",
    "blame", "grep", "cat-file", "name-rev", "config",
}
# Force/destructive flags: presence anywhere in a git statement voids the
# auto-allow (the command falls through to the normal permission prompt).
GIT_FORCE_FLAGS = {"--force", "--force-with-lease", "--force-if-includes",
                   "--hard", "-D", "--mirror", "--delete"}
# git stash verbs that discard work — not auto-allowed.
STASH_DENIED = {"drop", "clear"}
# Read-only filters/utilities allowed as pipeline segments.
FILTERS_ALLOWED = {"head", "tail", "grep", "wc", "sort", "uniq", "cut", "tr",
                   "jq", "cat", "echo", "true", "ls", "pwd", "which", "date",
                   "xargs", "test", "[", "cd", "diff", "column"}
GH_SUBCOMMANDS = {"release", "pr", "issue", "repo", "run", "auth"}
# Push-class gh verbs: publishing to GitHub. Auto-allowed only with the
# explicit autonomous-push opt-in (mirrors push-guard's contract).
GH_PUSH_CLASS = {
    ("pr", "create"), ("pr", "merge"), ("pr", "close"), ("pr", "reopen"),
    ("pr", "ready"), ("release", "create"), ("release", "edit"),
    ("release", "delete"), ("release", "upload"), ("release", "delete-asset"),
    ("repo", "delete"), ("repo", "create"), ("repo", "edit"),
}
STATEMENT_SEPS = {"&&", "||", ";", "&"}
REDIRECTS = {">", ">>", "<", "<<", ">|"}


def suppression_active(cfg: dict) -> bool:
    """Noir only, with an optional autonomy-allow.enabled kill-switch."""
    if (_scalar(cfg.get("work-paradigm")) or "Supervised") != "Noir":
        return False
    block = cfg.get("autonomy-allow")
    if isinstance(block, dict) and _scalar(block.get("enabled")) is False:
        return False
    return True


def autonomous_push_enabled(cfg: dict) -> bool:
    block = cfg.get("autonomous-push")
    return isinstance(block, dict) and _scalar(block.get("enabled")) is True


def tokenize(text: str) -> list[str] | None:
    """shlex word-split with shell operators as their own tokens (or None)."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError:
        return None


def split_statements(tokens: list[str]) -> list[list[str]] | None:
    """Split a token stream into statements; None if a redirection is present.

    `|` also splits (each pipe segment must independently be allowed);
    redirections void the auto-allow entirely — writing files via redirection
    is outside this hook's whitelist reasoning.
    """
    statements: list[list[str]] = [[]]
    for t in tokens:
        if t in REDIRECTS or t.startswith(">"):
            return None
        if t in STATEMENT_SEPS or t == "|":
            statements.append([])
            continue
        statements[-1].append(t)
    return [s for s in statements if s]


def git_statement_allowed(stmt: list[str]) -> bool:
    if any(t in GIT_FORCE_FLAGS for t in stmt):
        return False
    j = 1
    n = len(stmt)
    while j < n:
        t = stmt[j]
        if t in ("-C", "--git-dir", "--work-tree", "--namespace", "-c"):
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        break
    if j >= n:
        return True  # bare `git` — harmless
    sub = stmt[j]
    if sub not in GIT_ALLOWED:
        return False
    if sub == "stash" and any(a in STASH_DENIED for a in stmt[j + 1:]):
        return False
    return True


def statement_allowed(stmt: list[str], autopush: bool) -> bool:
    if not stmt:
        return True
    head = stmt[0]
    if head == "git" or head.endswith("/git"):
        return git_statement_allowed(stmt)
    if head == "just":
        return True  # project recipe targets are the sanctioned interface
    if head in ("python3", "python"):
        # Framework scripts only (skills helpers, recipe dispatcher).
        return any(a.endswith(".py") and ".claude/skills/" in a
                   for a in stmt[1:])
    if head == "gh":
        if len(stmt) < 2 or stmt[1] not in GH_SUBCOMMANDS:
            return False
        verb = stmt[2] if len(stmt) > 2 else ""
        if (stmt[1], verb) in GH_PUSH_CLASS:
            return autopush  # push-class publish: needs the explicit opt-in
        return True
    if head in FILTERS_ALLOWED:
        return True
    return False


def command_auto_allowed(cmd: str, autopush: bool) -> bool:
    tokens = tokenize(cmd)
    if not tokens:
        return False
    statements = split_statements(tokens)
    if statements is None:
        return False
    return all(statement_allowed(s, autopush) for s in statements)


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
    cfg = read_config(proj)
    if not suppression_active(cfg):
        sys.exit(0)
    cmd = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if command_auto_allowed(cmd, autonomous_push_enabled(cfg)):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason":
                "autonomy-allow: guard-vetted pipeline command auto-approved "
                "under Noir (deny guards still take precedence).",
        }}))
    sys.exit(0)


def _self_test() -> int:
    """Parser-level self-test: run with --self-test. Returns exit code."""
    cases = [
        # Routine pipeline git — allowed (deny guards still gate semantics).
        ("git status", True),
        ("git switch -c work version/3.2", True),
        ("git merge --no-ff work-branch", True),
        ("git commit -m 'add feature'", True),
        ("git log --oneline dev..HEAD", True),
        ("git worktree list", True),
        ("git tag v3.63", True),
        ("git fetch origin", True),
        # Compound commands: every statement must pass.
        ("git add -A && git commit -m 'x'", True),
        ("git log --oneline | head -5", True),
        ("git status && rm -rf /tmp/x", False),
        # Redirections void the auto-allow.
        ("git log > /tmp/log.txt", False),
        # Never auto-allowed: push (push-guard owns it), rewrites, force.
        ("git push origin dev main", False),
        ("git rebase dev", False),
        ("git cherry-pick abc", False),
        ("git reset --hard HEAD~1", False),
        ("git clean -fd", False),
        ("git branch -D dev", False),
        ("git merge --force x", False),
        ("git stash drop", False),
        ("git stash", True),
        # Framework scripts and recipes.
        ("just build env=prod", True),
        ("python3 .claude/skills/grm-build-recipe/recipe.py test", True),
        ("python3 /abs/path/.claude/skills/grm-doc-assurance/doc_assurance.py",
         True),
        ("python3 evil.py", False),
        ("python3 -c 'print(1)'", False),
        # gh: reads/tracker ops allowed; push-class needs autopush opt-in.
        ("gh issue list", True),
        ("gh pr view 12", True),
        ("gh release list", True),
        ("gh api /user", False),
        # Non-whitelisted heads fall through.
        ("rm -rf dist", False),
        ("curl https://example.com", False),
        ("npm install", False),
    ]
    failures = 0
    for cmd, expected in cases:
        got = command_auto_allowed(cmd, autopush=False)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {cmd!r} -> {got} (want {expected})")
    # Push-class gh flips with the autonomous-push opt-in.
    for cmd, autopush, expected in [
        ("gh release create v3.63 dist/x.tar.gz", False, False),
        ("gh release create v3.63 dist/x.tar.gz", True, True),
        ("gh pr merge 12 --squash", False, False),
        ("gh pr merge 12", True, True),
    ]:
        got = command_auto_allowed(cmd, autopush=autopush)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") +
              f"  {cmd!r} (autopush={autopush}) -> {got} (want {expected})")
    # Paradigm gating: only Noir activates suppression; kill-switch honored.
    for cfg, expected in [
        ({"work-paradigm": {"value": "Noir"}}, True),
        ({"work-paradigm": {"value": "Supervised"}}, False),
        ({"work-paradigm": {"value": "Weiss"}}, False),
        ({}, False),
        ({"work-paradigm": {"value": "Noir"},
          "autonomy-allow": {"enabled": False}}, False),
    ]:
        got = suppression_active(cfg)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") +
              f"  suppression_active({cfg!r}) -> {got} (want {expected})")
    print("PASS" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    main()
