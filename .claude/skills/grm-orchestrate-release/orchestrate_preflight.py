#!/usr/bin/env python3
"""Orchestrate-release preflight: verify every autonomy enabler in one pass.

One deterministic check, run at the top of grm-orchestrate-release, so the
orchestrator knows BEFORE dispatching anything whether the release can run
prompt-free end-to-end — and exactly which dial to fix if not. Checks:

  PARADIGM   work-paradigm.value == "Noir" (the pipeline fails closed off it)
  MARKER     .claude/integration-allow.local present (blessed worktree)
  AUTOPUSH   autonomous-push.enabled == true (else push degrades to propose+wait)
  GUARDS     the five deny guards + autonomy-allow + worktree-brief exist and
             are wired in .claude/settings.json
  GITSTATE   dev exists; HEAD on a staging-class branch; working tree clean

Output: one PASS/WARN/FAIL line per check + a verdict line. Exit 0 when no
FAIL (WARNs degrade autonomy but don't block); exit 1 on any FAIL.
Read-only — never edits config or git state. --self-test exercises the pure
helpers with injected values.
"""
import json
import os
import re
import subprocess
import sys

GUARD_HOOKS = [
    "worktree-guard.sh", "release-plan-guard.sh", "protected-branch-guard.sh",
    "push-guard.sh", "stealth-guard.sh", "autonomy-allow.sh",
    "worktree-brief.sh",
]
STAGING_RE = re.compile(r"^(dev|version/.*)$")


def _scalar(v):
    """Unwrap a config value that may be a bare scalar or a {"value": ...} block."""
    return v.get("value") if isinstance(v, dict) else v


def load_json(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def git(root: str, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", root, *args],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def check_paradigm(cfg: dict) -> tuple[str, str]:
    paradigm = _scalar(cfg.get("work-paradigm")) or "Supervised"
    if paradigm == "Noir":
        return ("PASS", "work-paradigm is Noir")
    return ("FAIL", f"work-paradigm is {paradigm} — orchestrate-release is "
                    "Noir-only (switch via grm-work-paradigm-switch)")


def check_autopush(cfg: dict) -> tuple[str, str]:
    block = cfg.get("autonomous-push")
    if isinstance(block, dict) and _scalar(block.get("enabled")) is True:
        return ("PASS", "autonomous-push.enabled is true (push is unattended)")
    return ("WARN", "autonomous-push.enabled is not true — the pipeline will "
                    "PAUSE at the push gate (propose and wait)")


def check_marker(root: str) -> tuple[str, str]:
    if os.path.isfile(os.path.join(root, ".claude", "integration-allow.local")):
        return ("PASS", "integration-allow marker present (blessed worktree)")
    return ("FAIL", "no .claude/integration-allow.local — this session is not "
                    "the blessed integration worktree; create it deliberately "
                    "(touch .claude/integration-allow.local) or run from the "
                    "integration worktree")


def check_guards(root: str) -> tuple[str, str]:
    hooks_dir = os.path.join(root, ".claude", "hooks")
    missing = [h for h in GUARD_HOOKS
               if not os.path.isfile(os.path.join(hooks_dir, h))]
    settings = load_json(os.path.join(root, ".claude", "settings.json"))
    wired = json.dumps(settings.get("hooks", {}))
    unwired = [h for h in GUARD_HOOKS if h not in wired]
    if not missing and not unwired:
        return ("PASS", "all guard + autonomy hooks present and wired")
    parts = []
    if missing:
        parts.append("missing files: " + ", ".join(missing))
    if unwired:
        parts.append("not wired in settings.json: " + ", ".join(unwired))
    return ("FAIL", "; ".join(parts) + " (run grm-install-doctor)")


def check_gitstate(root: str) -> tuple[str, str]:
    if git(root, "rev-parse", "--verify", "--quiet", "dev") is None:
        return ("FAIL", "no dev branch — not a Grimoire branch model "
                        "(run grm-repo-init)")
    head = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if head is None:
        return ("FAIL", "detached HEAD — check out the staging branch first")
    status = git(root, "status", "--porcelain")
    dirty = " · working tree has uncommitted changes" if status else ""
    if STAGING_RE.match(head):
        level = "WARN" if dirty else "PASS"
        return (level, f"HEAD on staging-class branch '{head}'{dirty}")
    return ("WARN", f"HEAD on '{head}' (not dev/version/*) — verify before "
                    f"any merge{dirty}")


def run(root: str) -> int:
    cfg = load_json(os.path.join(root, ".claude", "grimoire-config.json"))
    checks = [
        ("PARADIGM", check_paradigm(cfg)),
        ("MARKER", check_marker(root)),
        ("AUTOPUSH", check_autopush(cfg)),
        ("GUARDS", check_guards(root)),
        ("GITSTATE", check_gitstate(root)),
    ]
    worst = "PASS"
    for name, (level, detail) in checks:
        print(f"{level:4}  {name:9} {detail}")
        if level == "FAIL":
            worst = "FAIL"
        elif level == "WARN" and worst == "PASS":
            worst = "WARN"
    verdicts = {
        "PASS": "READY — fully autonomous release (plan → push → cleanup).",
        "WARN": "READY WITH GATES — pipeline runs but will pause where "
                "warned above.",
        "FAIL": "NOT READY — fix the FAIL lines before orchestrating.",
    }
    print("\nverdict: " + verdicts[worst])
    return 1 if worst == "FAIL" else 0


def _self_test() -> int:
    cases = [
        (check_paradigm({"work-paradigm": {"value": "Noir"}})[0], "PASS"),
        (check_paradigm({"work-paradigm": {"value": "Supervised"}})[0], "FAIL"),
        (check_paradigm({})[0], "FAIL"),
        (check_autopush({"autonomous-push": {"enabled": True}})[0], "PASS"),
        (check_autopush({"autonomous-push": {"enabled": False}})[0], "WARN"),
        (check_autopush({})[0], "WARN"),
        (_scalar({"value": "Noir"}), "Noir"),
        (_scalar("Noir"), "Noir"),
    ]
    failures = 0
    for got, want in cases:
        ok = got == want
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {got!r} (want {want!r})")
    assert STAGING_RE.match("dev") and STAGING_RE.match("version/3.63")
    assert not STAGING_RE.match("main") and not STAGING_RE.match("work-x")
    print("PASS" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    sys.exit(run(root))
