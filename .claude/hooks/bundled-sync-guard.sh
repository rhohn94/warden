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
# HOOK_CONTRACT: v1 capabilities=[bundled-sync-commit-block]
"""Bundled-sync-commit guard (issue #126, BMI-3 mechanical enforcement).

PreToolUse(Bash) hook that fires on `git commit`. It denies (fail-closed,
matching this repo's existing guard posture — see protected-branch-guard.sh /
stealth-guard.sh, both of which `sys.exit(2)` rather than warn) a single commit
whose STAGED changes span both of the following touch-sets at once:

  - `grm-sync-from-upstream`'s typical touch-set: broad framework/scaffolding
    paths (`.claude/`, `CLAUDE.md`, `AGENTS.md`, `docs/grimoire/`, the
    `.github/` Copilot mirror).
  - `grm-design-language-adapt`'s typical touch-set: the Aura / design-language
    asset paths (`docs/design/ux/`, `vendor/aura/`, `static/aura/`,
    `templates/base.html`).

Rationale (#126): the v8.40 fork's aggravating commit (`24c73dd`, "sync:
Grimoire upstream + Aura v3.21") bundled a 660-file framework sync and an Aura
re-vendor into ONE commit on `main`, maximizing the collision surface and
making later reconciliation all-or-nothing. Both `grm-sync-from-upstream`
(SKILL.md / reference.md "Rule 3c") and `grm-design-language-adapt`
(reference.md "Rule 3c — separate commit reminder") already tell the operator,
in prose, to commit these as two separate commits — but until now nothing
mechanically enforced it; the reminder could simply be ignored. This hook is
that mechanical enforcement, closing the last open gap in #126's five
acceptance criteria (BMI-3 — framework-internal design specs, see the upstream
Grimoire repository for that rationale — and docs/grimoire/integration-workflow.md
§Single-integration-line invariant).

Scope note: this hook does NOT duplicate BMI-3 Rules 3a/3b (branch +
release-boundary refusal), which live in the sync SCRIPTS themselves
(`sync-from-upstream.sh`, `design-language-adapt`'s Step 0 preflight) because
those checks need branch/boundary context the two skills already compute.
This hook only adds the mechanical "not in the SAME commit" check, which
neither skill's own preflight can catch (each skill only knows its own
touch-set at the time it runs, not the OTHER skill's changes that a human/agent
might separately stage into the same commit before running `git commit`).

Detection is staged-file-based (`git diff --cached --name-only`), not
committed-message-based: it must fire on `git commit` BEFORE the commit lands,
while the changes are still just staged. A commit that only touches one
touch-set (the overwhelmingly common case for both skills — sync usually
touches no Aura paths, and design-language-adapt usually touches no
framework/scaffolding paths outside its own tier) is unaffected.

Escape hatches (`--abort` / `--quit` / `--skip` on the *containing* command,
mirroring the other guards' convention) and non-`commit` invocations are
no-ops. Detached HEAD / not-a-repo / no staged changes are no-ops (nothing to
inspect). Run with `--self-test` for the fast dependency-free parser harness.
"""
import json
import os
import re
import shlex
import subprocess
import sys

OPTS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "-c"}
ESCAPE_HATCH = {"--abort", "--quit", "--skip"}
STATEMENT_SEPS = {"&&", "||", ";", "|", "&"}

# ── Touch-set fingerprints (issue #126 BMI-3) ───────────────────────────────
# Prefixes are matched against project-relative, forward-slash-normalized
# staged paths. Order/overlap does not matter — a path counts toward a
# touch-set if it starts with ANY of that set's prefixes.
SYNC_FROM_UPSTREAM_PREFIXES = (
    ".claude/skills/",
    ".claude/hooks/",
    ".claude/paradigms/",
    ".claude/mcp-servers/",
    ".claude/workflows/",
    ".claude/quick-start-templates/",
    ".claude/grimoire-config.json",
    ".claude/grimoire-files.json",
    ".claude/architecture-rules.example.json",
    ".claude/model-effort-profiles.json",
    ".claude/settings.json",
    "CLAUDE.md",
    "AGENTS.md",
    "docs/grimoire/",
    ".github/prompts/",
    ".github/copilot-instructions.md",
    ".scaffold-upstream.conf",
)
# design-language-adapt's touch-set (SKILL.md / reference.md): the UX design
# doc tier it writes directly, plus the Aura vendored-asset paths its
# "vendor-dep" / "submodule" / "vendored-build" consumption modes populate
# (reference.md "Default paths / layout" table), plus the base-shell template
# it binds Aura into.
DESIGN_LANGUAGE_ADAPT_PREFIXES = (
    "docs/design/ux/",
    "vendor/aura/",
    "static/aura/",
    "templates/base.html",
)


def _norm(path: str) -> str:
    """Project-relative, forward-slash-normalized path, stripping only a
    leading "./" (never a bare leading "." — that would corrupt ".claude/...",
    the same pitfall stealth-guard.sh's is_managed() documents avoiding)."""
    p = path.replace(os.sep, "/")
    if p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def classify(paths):
    """Return (sync_hits, design_hits) — the subset of `paths` matching each
    touch-set. A path may appear in at most one list (checked sync-set
    first; the two sets do not overlap in practice, but this keeps the
    classification total and deterministic)."""
    sync_hits, design_hits = [], []
    for p in paths:
        norm = _norm(p)
        if any(norm.startswith(pref) for pref in SYNC_FROM_UPSTREAM_PREFIXES):
            sync_hits.append(p)
        elif any(norm.startswith(pref) for pref in DESIGN_LANGUAGE_ADAPT_PREFIXES):
            design_hits.append(p)
    return sync_hits, design_hits


def find_commit_invocation(cmd: str) -> bool:
    """True iff `cmd` contains a `git commit` invocation that is not an
    escape-hatch (--abort/--quit/--skip) form. Mirrors the token-walk style
    used by protected-branch-guard.sh / stealth-guard.sh so option values are
    never mistaken for the subcommand."""
    try:
        tokens = shlex.split(cmd, comments=False, posix=True)
    except ValueError:
        return bool(re.search(r"\bgit\b[^|;&\n]*?\bcommit\b", cmd)) and not any(
            h in cmd for h in ESCAPE_HATCH
        )

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
                    rest = tokens[j + 1:]
                    end = len(rest)
                    for k, tk in enumerate(rest):
                        if tk in STATEMENT_SEPS:
                            end = k
                            break
                    if any(a in ESCAPE_HATCH for a in rest[:end]):
                        return False
                    return True
                break
            i = j
        i += 1
    return False


def staged_paths(proj: str) -> list[str]:
    """Return the list of staged (index) file paths, or [] on any failure —
    fail OPEN on inspection error (never block a commit because `git diff`
    itself couldn't run; the mechanical guard only fires on a CONFIRMED
    cross-touch-set match)."""
    try:
        r = subprocess.run(
            ["git", "-C", proj, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


def deny(sync_hits, design_hits) -> None:
    lines = [
        "bundled-sync-guard: blocked `git commit` — staged changes span BOTH",
        "the framework-sync touch-set AND the design-language (Aura) touch-set",
        "in one commit.",
        "",
        "This is the exact pattern behind issue #126's v8.40 fork: commit",
        "`24c73dd` bundled \"Grimoire upstream + Aura v3.21\" into one 660-file",
        "commit, maximizing the collision surface for later reconciliation.",
        "",
        "Framework-sync-shaped staged paths (%d):" % len(sync_hits),
    ]
    for p in sync_hits[:6]:
        lines.append("  " + p)
    if len(sync_hits) > 6:
        lines.append("  ... (%d more)" % (len(sync_hits) - 6))
    lines.append("")
    lines.append("Design-language/Aura-shaped staged paths (%d):" % len(design_hits))
    for p in design_hits[:6]:
        lines.append("  " + p)
    if len(design_hits) > 6:
        lines.append("  ... (%d more)" % (len(design_hits) - 6))
    lines.append("")
    lines.append("Split into two commits instead:")
    lines.append("  git reset  <one of the two path groups above>")
    lines.append('  git commit -m "..."   # first group')
    lines.append("  git add    <the other path group>")
    lines.append('  git commit -m "..."   # second group')
    lines.append("")
    lines.append(
        "See .claude/skills/grm-sync-from-upstream/reference.md 'Rule 3c' and"
    )
    lines.append(
        ".claude/skills/grm-design-language-adapt/reference.md 'Rule 3c' — both"
    )
    lines.append(
        "already document this as a reminder; this hook makes it mechanical."
    )
    sys.stderr.write("\n".join(lines) + "\n")
    sys.exit(2)


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
    if not find_commit_invocation(cmd):
        sys.exit(0)

    paths = staged_paths(proj)
    if not paths:
        sys.exit(0)

    sync_hits, design_hits = classify(paths)
    if sync_hits and design_hits:
        deny(sync_hits, design_hits)

    sys.exit(0)


# ── parser-level self-test (no git / marker needed) ─────────────────────────
def _self_test() -> int:
    """Self-test mirroring sibling hooks' `--self-test` convention
    (push-guard.sh, autonomy-allow.sh): parser-level cases against
    find_commit_invocation() and classify(), plus a real-scratch-git-repo
    check of staged_paths() + the end-to-end deny/allow decision via main()'s
    logic (reimplemented inline so no subprocess-vs-stdin plumbing is needed
    for the fast path). Returns failure count."""
    failures = 0

    def check(desc, cond):
        nonlocal failures
        print(("ok  " if cond else "FAIL") + "  " + desc)
        failures += not cond

    # -- find_commit_invocation ----------------------------------------------
    check("plain git commit detected",
          find_commit_invocation('git commit -m "msg"'))
    check("git commit in a chain detected",
          find_commit_invocation('git add -A && git commit -m "msg"'))
    check("git -C <path> commit detected (option skipped)",
          find_commit_invocation("git -C /repo commit -m msg"))
    check("non-commit git op not detected",
          not find_commit_invocation("git status"))
    check("git merge not detected as commit",
          not find_commit_invocation("git merge --no-ff dev"))
    check("commit --abort not detected (escape hatch)",
          not find_commit_invocation("git commit --abort"))
    check("quoted 'git commit' in a message string not a real invocation",
          not find_commit_invocation('echo "git commit -m x"'))

    # -- classify --------------------------------------------------------
    sync_only = [".claude/skills/grm-sync-from-upstream/sync-from-upstream.sh",
                 "docs/grimoire/integration-workflow.md", "CLAUDE.md"]
    design_only = ["docs/design/ux/design-language.md", "vendor/aura/theme.css",
                   "templates/base.html"]
    mixed = sync_only + design_only
    unrelated = ["src/app.py", "tests/test_app.py", "README.md"]

    s, d = classify(sync_only)
    check("sync-only paths classify as sync, none as design",
          len(s) == len(sync_only) and not d)
    s, d = classify(design_only)
    check("design-only paths classify as design, none as sync",
          len(d) == len(design_only) and not s)
    s, d = classify(mixed)
    check("mixed paths classify into both buckets",
          len(s) == len(sync_only) and len(d) == len(design_only))
    s, d = classify(unrelated)
    check("unrelated project paths classify as neither",
          not s and not d)
    s, d = classify(["docs/grimoire/design/some-design.md"])
    check("docs/grimoire/design/ nested path still classifies as sync-shaped",
          len(s) == 1 and not d)
    s, d = classify(["static/aura/tokens.css"])
    check("static/aura/ (vendored-build consumption mode) classifies as design",
          len(d) == 1 and not s)
    s, d = classify([".claude/settings.json"])
    check(".claude/settings.json classifies as sync-shaped",
          len(s) == 1 and not d)

    # -- end-to-end deny/allow decision (mirrors main()'s logic) -------------
    def decision(paths):
        s, d = classify(paths)
        return "deny" if (s and d) else "allow"

    check("mixed staged set => deny",
          decision(mixed) == "deny")
    check("sync-only staged set => allow",
          decision(sync_only) == "allow")
    check("design-only staged set => allow",
          decision(design_only) == "allow")
    check("unrelated staged set => allow",
          decision(unrelated) == "allow")

    # -- real-scratch-git-repo check of staged_paths() -----------------------
    import shutil
    import tempfile

    if shutil.which("git") is None:
        print("SKIP  staged_paths real-repo check (git not on PATH)")
    else:
        with tempfile.TemporaryDirectory(prefix="bsg-selftest-") as repo:
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.email", "t@example.invalid"], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
            os.makedirs(os.path.join(repo, ".claude", "skills"), exist_ok=True)
            os.makedirs(os.path.join(repo, "vendor", "aura"), exist_ok=True)
            with open(os.path.join(repo, ".claude", "skills", "x.md"), "w") as f:
                f.write("sync file\n")
            with open(os.path.join(repo, "vendor", "aura", "theme.css"), "w") as f:
                f.write("aura file\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            got = staged_paths(repo)
            check("staged_paths returns both planted files",
                  set(got) == {".claude/skills/x.md", "vendor/aura/theme.css"})
            s, d = classify(got)
            check("real-repo mixed stage => deny decision",
                  bool(s) and bool(d))

            # Commit them (clears the index) and confirm staged_paths is empty.
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "seed"], check=True)
            check("staged_paths empty after commit (nothing left staged)",
                  staged_paths(repo) == [])

    print("\n" + ("PASS" if not failures else f"{failures} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    main()
