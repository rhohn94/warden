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
# HOOK_CONTRACT: v1 capabilities=[onboarding-unfinished-bootstrap-commit-block]
"""Onboarding-bootstrap commit guard (v3.94, #430).

Denies a `git commit` that touches source/feature-work paths while the
project is still un-onboarded — sentinel present on `CLAUDE.md` line 1,
and/or `.claude/grimoire-config.json` is absent or still carries the
onboarding placeholder project name. Root cause this closes: two fleet
repos sat 12 days at zero code with the sentinel uncleared and
scaffold-default config, because nothing stopped feature work from landing
before bootstrap ran — "a fresh scaffold should be un-usable except via
bootstrap" (issue #430).

Scope is deliberately narrow ("cheap guard", per the issue): it blocks a
commit only when its changed paths include something OUTSIDE the
framework/bootstrap allowlist (`.claude/`, `docs/`, and a short list of
root files — CLAUDE.md, AGENTS.md, README.md, KICKOFF.md,
FIRST-RELEASE-PROMPT.md, .gitignore, .gitattributes, justfile/Justfile,
LICENSE(.md), .mcp.json, .grimoire-flavor). Onboarding's OWN commits
(writing `.claude/grimoire-config.json`, patching `CLAUDE.md` placeholders,
stripping the sentinel, seeding `docs/roadmap.md`) always land inside that
allowlist, so the onboarding flow itself is never blocked by this guard —
only a commit that reaches outside it (real source/feature files) while
bootstrap is still incomplete.

Non-bypass-shaped escape hatch (deliberate, per the issue's hard
constraint): there is no flag, env var, or config toggle that silences this
guard. The only way to clear it is to actually finish onboarding — remove
the sentinel and write a real config (interactively, or via the
`RUN NON-INTERACTIVE ONBOARDING` / legacy `SKIP ONBOARDING` fast path,
including the v3.94 committed-kickoff-file trigger — see
`grm-onboarding` §2.0 / `docs/grimoire/design/onboarding-design.md` §4.6).
This file is NOT an edit to `autonomy-allow.sh`'s whitelist and never emits
a `permissions.allow` entry — it is a standalone PreToolUse Bash guard,
registered in `.claude/settings.json` alongside the other guard hooks.

Design authority: `docs/grimoire/design/onboarding-design.md` §9.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_common import read_config  # noqa: E402

SENTINEL_LINE = "<!-- GRIMOIRE_ONBOARDING_SENTINEL -->"
# Onboarding's own zero-signal default project name (reference.md §2 / the
# interactive interview's Step-1 fallback), plus this repo's own dogfood
# placeholder — both are names nobody keeps once a real interview or a
# `RUN NON-INTERACTIVE ONBOARDING` inference has actually run.
PLACEHOLDER_NAMES = {"My Project", "Grimoire"}
ALLOWED_DIR_PREFIXES = (".claude/", "docs/")
ALLOWED_ROOT_FILES = {
    "CLAUDE.md", "AGENTS.md", "README.md", "KICKOFF.md",
    "FIRST-RELEASE-PROMPT.md", ".gitignore", ".gitattributes",
    "justfile", "Justfile", "LICENSE", "LICENSE.md", ".mcp.json",
    ".grimoire-flavor",
}
OPTS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "-c"}


def find_commit_invocation(cmd: str) -> list[str] | None:
    """Return the argument tokens of the FIRST `git commit` invocation
    (everything after the `commit` token), or None if no commit subcommand
    is present. Mirrors protected-branch-guard.sh's token-walk so an option
    VALUE (e.g. `-C <path>`) is never mistaken for the subcommand."""
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
                if t == "commit":
                    return tokens[j + 1:]
                break
            i = j
        i += 1
    return None


def is_sentinel_present(claude_md_path: str) -> bool:
    """True iff line 1 of CLAUDE.md is exactly the sentinel comment."""
    try:
        with open(claude_md_path, "r", encoding="utf-8") as fh:
            first = fh.readline().rstrip("\n")
    except OSError:
        return False
    return first == SENTINEL_LINE


def is_scaffold_default_config(cfg: dict | None) -> bool:
    """True iff grimoire-config.json is absent/empty or still carries
    onboarding's own zero-signal placeholder project name.

    Deliberately does NOT fingerprint the paradigm/profile dials
    (Supervised/Efficient/Medium/Default): those are legitimate answers a
    real interview (or a real `RUN NON-INTERACTIVE ONBOARDING` inference)
    can produce, so keying on them would false-positive on a genuinely
    configured project that happens to want the defaults. `name` is the one
    field onboarding never leaves at its placeholder unless onboarding
    never actually ran (reference.md §2 inference table / §1.2 Step 1).
    """
    if not cfg:
        return True
    name = cfg.get("name")
    return (not name) or (name in PLACEHOLDER_NAMES)


def is_allowed_path(rel_path: str) -> bool:
    """True iff `rel_path` is inside the framework/bootstrap allowlist —
    the set of paths onboarding's OWN commits touch (config, CLAUDE.md,
    docs/roadmap.md seeding, a committed kickoff file, etc.)."""
    if rel_path.startswith(ALLOWED_DIR_PREFIXES):
        return True
    if "/" not in rel_path and rel_path in ALLOWED_ROOT_FILES:
        return True
    return False


def feature_work_files(paths: list[str]) -> list[str]:
    return [p for p in paths if not is_allowed_path(p)]


def changed_paths_for_commit(proj: str, commit_args: list[str]) -> list[str]:
    """Best-effort list of paths this commit invocation will record: the
    ordinary staged-changes case, plus `-a`/`--all` (auto-staged tracked
    modifications, which `git commit -a` stages as part of the commit
    itself rather than via a prior `git add`)."""
    paths: set[str] = set()
    try:
        staged = subprocess.run(
            ["git", "-C", proj, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=5,
        )
        if staged.returncode == 0:
            paths.update(p for p in staged.stdout.splitlines() if p)
    except (OSError, subprocess.SubprocessError):
        pass
    if any(a in ("-a", "--all") for a in commit_args):
        try:
            unstaged = subprocess.run(
                ["git", "-C", proj, "diff", "--name-only"],
                capture_output=True, text=True, timeout=5,
            )
            if unstaged.returncode == 0:
                paths.update(p for p in unstaged.stdout.splitlines() if p)
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted(paths)


def decide(proj: str, cmd: str) -> tuple[str, list[str], bool, bool]:
    """Core decision, shared by main() and the self-tests below.

    Returns (decision, offending_paths, sentinel_present, scaffold_default)
    where decision is "allow" or "deny".
    """
    commit_args = find_commit_invocation(cmd)
    if commit_args is None:
        return ("allow", [], False, False)

    sentinel = is_sentinel_present(os.path.join(proj, "CLAUDE.md"))
    scaffold_default = is_scaffold_default_config(read_config(proj))
    if not (sentinel or scaffold_default):
        return ("allow", [], sentinel, scaffold_default)

    offenders = feature_work_files(changed_paths_for_commit(proj, commit_args))
    if not offenders:
        return ("allow", [], sentinel, scaffold_default)
    return ("deny", offenders, sentinel, scaffold_default)


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
    decision, offenders, sentinel, scaffold_default = decide(proj, cmd)
    if decision == "allow":
        sys.exit(0)

    reasons = []
    if sentinel:
        reasons.append("the onboarding sentinel is still on CLAUDE.md line 1")
    if scaffold_default:
        reasons.append(".claude/grimoire-config.json is still the scaffold default")
    shown = ", ".join(offenders[:8]) + (", ..." if len(offenders) > 8 else "")
    sys.stderr.write(
        "onboarding-bootstrap-guard: blocked feature-work commit — "
        + " and ".join(reasons) + ".\n"
        f"  offending path(s): {shown}\n"
        "A fresh scaffold is usable ONLY via bootstrap (issue #430). Finish "
        "onboarding first:\n"
        "  - commit a root KICKOFF.md or FIRST-RELEASE-PROMPT.md containing\n"
        "    `RUN NON-INTERACTIVE ONBOARDING` (legacy `SKIP ONBOARDING` also\n"
        "    accepted) — it self-triggers on your very next prompt, of any\n"
        "    shape (grm-onboarding §2.0), or\n"
        "  - type the same trigger live in chat, or\n"
        "  - run the interactive `grm-onboarding` flow to completion.\n"
        "Framework/doc-only commits (.claude/, docs/, CLAUDE.md, README.md, "
        "...) stay\nallowed throughout — this only blocks committing source/"
        "feature work ahead\nof bootstrap. There is no bypass flag: the "
        "guard clears itself automatically\nonce onboarding finishes "
        "(sentinel removed, a real project name written).\n"
    )
    sys.exit(2)


# ── Self-tests (run with --self-test; no live repo / hook wiring needed) ──

def _self_test_find_commit_invocation() -> int:
    cases = [
        ("git commit -m 'msg'", ["-m", "msg"]),
        ("git add -A && git commit -m 'msg' -a", ["-m", "msg", "-a"]),
        ("git commit --amend --no-edit", ["--amend", "--no-edit"]),
        ("git -C /other commit -m x", ["-m", "x"]),
        ("git status", None),
        ("git push origin dev", None),
        ('echo "git commit -m x"', None),  # 'commit' only inside a quoted arg
        ("git -c rerere.enabled=true commit -m x", ["-m", "x"]),
    ]
    failures = 0
    for cmd, expected in cases:
        got = find_commit_invocation(cmd)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {cmd!r} -> {got!r} (want {expected!r})")
    return failures


def _self_test_is_allowed_path() -> int:
    cases = [
        (".claude/grimoire-config.json", True),
        (".claude/hooks/onboarding-bootstrap-guard.sh", True),
        ("docs/roadmap.md", True),
        ("docs/design/foo-design.md", True),
        ("CLAUDE.md", True),
        ("KICKOFF.md", True),
        ("FIRST-RELEASE-PROMPT.md", True),
        ("README.md", True),
        ("src/app.py", False),
        ("lib/first-party/util.rs", False),
        ("tests/test_app.py", False),
        ("some/nested/KICKOFF.md", False),  # only ROOT kickoff files count
    ]
    failures = 0
    for path, expected in cases:
        got = is_allowed_path(path)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {path!r} -> {got} (want {expected})")
    return failures


def _self_test_is_scaffold_default_config() -> int:
    cases = [
        (None, True),
        ({}, True),
        ({"name": "My Project"}, True),
        ({"name": "Grimoire"}, True),
        ({"name": ""}, True),
        ({"name": "Acme"}, False),
        ({"name": "Acme", "work-paradigm": {"value": "Supervised"}}, False),
    ]
    failures = 0
    for cfg, expected in cases:
        got = is_scaffold_default_config(cfg)
        ok = got == expected
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {cfg!r} -> {got} (want {expected})")
    return failures


def _self_test_sentinel_detection() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        sentinel_path = os.path.join(td, "sentinel.md")
        with open(sentinel_path, "w") as fh:
            fh.write(SENTINEL_LINE + "\n# CLAUDE.md\n")
        ok = is_sentinel_present(sentinel_path) is True
        failures += not ok
        print(("ok  " if ok else "FAIL") + "  sentinel-present file detected")

        clean_path = os.path.join(td, "clean.md")
        with open(clean_path, "w") as fh:
            fh.write("# CLAUDE.md\n")
        ok = is_sentinel_present(clean_path) is False
        failures += not ok
        print(("ok  " if ok else "FAIL") + "  sentinel-absent file not flagged")

        ok = is_sentinel_present(os.path.join(td, "missing.md")) is False
        failures += not ok
        print(("ok  " if ok else "FAIL") + "  missing file not flagged (no-op)")
    return failures


def _git(proj: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", proj, *args], capture_output=True, text=True, timeout=5,
    )


def _self_test_real_repo_decision() -> int:
    """Integration-style self-test against a real scratch git repo, proving
    the guard fires and does NOT fire end-to-end (#430 acceptance: "a commit
    attempted against an unstripped sentinel is denied ... demonstrate the
    guard firing and NOT firing, both cases")."""
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        _git(td, "init", "-q", "-b", "main")
        os.makedirs(os.path.join(td, "docs"), exist_ok=True)
        os.makedirs(os.path.join(td, "src"), exist_ok=True)
        with open(os.path.join(td, "CLAUDE.md"), "w") as fh:
            fh.write(SENTINEL_LINE + "\n# CLAUDE.md\n")
        with open(os.path.join(td, "docs", "roadmap.md"), "w") as fh:
            fh.write("# Roadmap\n")
        with open(os.path.join(td, "src", "app.py"), "w") as fh:
            fh.write("print('hi')\n")
        _git(td, "add", "-A")
        _git(td, "commit", "-q", "-m", "chore: initial Grimoire scaffold")

        # Case 1: sentinel present, no config -> a feature-work commit is denied.
        with open(os.path.join(td, "src", "app.py"), "w") as fh:
            fh.write("print('feature')\n")
        _git(td, "add", "src/app.py")
        decision, offenders, sentinel, default = decide(td, "git commit -m 'add feature'")
        ok = decision == "deny" and offenders == ["src/app.py"] and sentinel and default
        failures += not ok
        print(("ok  " if ok else "FAIL")
              + f"  [unonboarded + feature file] -> {decision!r} offenders={offenders!r}"
              f" sentinel={sentinel} default={default} (want deny/['src/app.py']/True/True)")
        _git(td, "reset", "-q")  # unstage for the next case

        # Case 2: sentinel present, no config -> a framework/doc-only commit
        # is ALLOWED — onboarding's own commits must never be self-blocked.
        with open(os.path.join(td, "docs", "roadmap.md"), "a") as fh:
            fh.write("- seeded row\n")
        _git(td, "add", "docs/roadmap.md")
        decision2, offenders2, _, _ = decide(td, "git commit -m 'docs: seed roadmap'")
        ok = decision2 == "allow" and offenders2 == []
        failures += not ok
        print(("ok  " if ok else "FAIL")
              + f"  [unonboarded + doc-only file] -> {decision2!r} (want allow)")
        _git(td, "reset", "-q")

        # Case 3: onboarding completes (sentinel stripped, real config
        # written) -> the SAME feature-work commit is now allowed.
        with open(os.path.join(td, "CLAUDE.md"), "w") as fh:
            fh.write("# CLAUDE.md\n")
        os.makedirs(os.path.join(td, ".claude"), exist_ok=True)
        with open(os.path.join(td, ".claude", "grimoire-config.json"), "w") as fh:
            json.dump({"name": "Acme"}, fh)
        with open(os.path.join(td, "src", "app.py"), "w") as fh:
            fh.write("print('feature 2')\n")
        _git(td, "add", "-A")
        decision3, offenders3, sentinel3, default3 = decide(
            td, "git commit -m 'add feature'"
        )
        ok = decision3 == "allow" and not sentinel3 and not default3
        failures += not ok
        print(("ok  " if ok else "FAIL")
              + f"  [onboarded] -> {decision3!r} sentinel={sentinel3} default={default3}"
              " (want allow/False/False)")
    return failures


def _self_test() -> int:
    failures = 0
    failures += _self_test_find_commit_invocation()
    failures += _self_test_is_allowed_path()
    failures += _self_test_is_scaffold_default_config()
    failures += _self_test_sentinel_detection()
    failures += _self_test_real_repo_decision()
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    main()
