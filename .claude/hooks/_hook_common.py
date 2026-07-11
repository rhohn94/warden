"""Shared helpers for the `.claude/hooks/*.sh` PreToolUse guard scripts.

Not a hook itself — never referenced from settings.json and never invoked
directly. Each hook is a bash/python polyglot file invoked by path
(`$CLAUDE_PROJECT_DIR/.claude/hooks/<name>.sh`); after its preamble re-execs
into python3, it adds its own directory to `sys.path` and imports this
module (mirrors the pattern `.claude/skills/grm-code-health/code_health.py`
uses to import `architecture_fitness` from a sibling directory). Centralizes
the small helpers that were previously copy-pasted across autonomy-allow.sh,
push-guard.sh, and protected-branch-guard.sh so they can't silently drift.
"""
from __future__ import annotations

import json
import os
import subprocess


def _scalar(v):
    """Unwrap a config value that may be a bare scalar or a {"value": ...} block."""
    return v.get("value") if isinstance(v, dict) else v


def read_config(proj: str) -> dict:
    """Parse .claude/grimoire-config.json, or {} if absent/unreadable."""
    if not proj:
        return {}
    try:
        with open(os.path.join(proj, ".claude", "grimoire-config.json")) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


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
