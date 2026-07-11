#!/usr/bin/env python3
"""provision_filing_authority.py — provision issue-filing authority into
`.claude/settings.json` (v3.74 #221).

Autonomous issue filing is designed-in framework behaviour (the Reporter, QA
agent, Triager, Researcher, grm-iterate, and the Noir integration master all
file issues without a per-instance user request). But the Claude Code auto-mode
permission classifier — a harness-level layer *outside* `.claude/` config —
reasonably reads each unsolicited `create_issue` as an unrequested external
write and blocks it. The fix is to make filing an *explicitly allowed* surface:
add permission-allowlist entries the classifier honours, keyed off the user's
one-time `issue-filing-authority.enabled` opt-in.

This helper writes those entries into `.claude/settings.json`:

  1. The issue-tracker MCP tool names, namespaced per the project's actual MCP
     server name (derived from `.mcp.json` — defaults to `grimoire-issue-tracker`):
       mcp__<server>__create_issue / comment_issue / update_issue /
       close_issue / label_issue / ensure_label
  2. The CLI-fallback path — `Bash(...)` rules covering the
     `issue_tracker.py` helper (both the bare-relative and $CLAUDE_PROJECT_DIR
     forms, matching the sync-script provisioning convention in
     grm-workflow-bootstrap/reference.md §Step 2).

Contract (mirrors the sync-script allowlist provisioning):
  - **Idempotent** — re-running never duplicates an entry.
  - **Additive** — existing `permissions.allow` entries are preserved; nothing
    is ever removed or reordered destructively (new entries are appended).
  - **Opt-in gated** — provisioning only happens when
    `issue-filing-authority.enabled` is true in grimoire-config.json (this
    helper enforces that gate unless --force is passed for a direct call).

Usage:
  provision_filing_authority.py [--root DIR] [--force] [--dry-run] [--self-test]

  --root      Project root (default: auto-detect from cwd up to the dir holding
              .claude/grimoire-config.json; falls back to cwd).
  --force     Provision even if the config opt-in is absent/false (used by
              grm-issue-tracker-switch after it has just written the opt-in, or
              for testing). Without it, the config gate must be satisfied.
  --dry-run   Report what would change without writing.
  --self-test In-memory checks of the merge logic; exits non-zero on failure.

Exit: 0 on success / no-op / passing self-test; 1 on error or failing self-test.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# issue_tracker.py is a sibling module in this same skill directory, so a
# plain import resolves via sys.path[0] (the script's own dir) when this
# script is run standalone. Shares its find_repo_root()/CONFIG_FILE — single
# body of truth (#335) — instead of carrying a byte-identical copy.
import issue_tracker

CONFIG_REL = issue_tracker.CONFIG_FILE
SETTINGS_REL = ".claude/settings.json"
MCP_JSON_REL = ".mcp.json"

DEFAULT_MCP_SERVER = "grimoire-issue-tracker"

# The issue-tracker MCP operations that write to the tracker (the ones the
# classifier blocks). Read-only ops (list/get/search) are not filing and are
# not provisioned here.
MCP_WRITE_OPS = [
    "create_issue",
    "comment_issue",
    "update_issue",
    "close_issue",
    "label_issue",
    "ensure_label",
]

# The CLI-fallback helper path (relative to project root).
ISSUE_TRACKER_CLI = ".claude/skills/grm-issue-tracker/issue_tracker.py"


def _scalar(v):
    """Unwrap a config value that may be a bare scalar or a {"value": ...} block."""
    return v.get("value") if isinstance(v, dict) else v


def find_root(start: Path | None = None) -> Path:
    """Find the dir holding grimoire-config.json, falling back to start/cwd.

    Delegates the walk-up to issue_tracker.find_repo_root() (single body of
    truth, #335/#336) — that helper returns None on a miss, so this wrapper
    applies the same cwd fallback the other find_repo_root() callers use.
    """
    return issue_tracker.find_repo_root(start) or (start or Path.cwd()).resolve()


def filing_authority_enabled(root: Path) -> bool:
    """True iff issue-filing-authority.enabled is true in grimoire-config.json."""
    cfg_path = root / CONFIG_REL
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    block = cfg.get("issue-filing-authority")
    return isinstance(block, dict) and _scalar(block.get("enabled")) is True


def mcp_server_name(root: Path) -> str:
    """Derive the issue-tracker MCP server name from .mcp.json.

    The permission tool names are namespaced per the *actual* server key in the
    project's `.mcp.json` (mcp__<server>__<op>). Grimoire's bundled server is
    registered under `grimoire-issue-tracker`; a project could rename it. We
    resolve it by matching the server whose args reference the issue-tracker
    server module, and fall back to the default name when it cannot be resolved.
    """
    mcp_path = root / MCP_JSON_REL
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_MCP_SERVER
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return DEFAULT_MCP_SERVER
    # Prefer the server whose args point at the issue-tracker server module.
    for name, spec in servers.items():
        args = (spec or {}).get("args") or []
        if any("issue-tracker/server.py" in str(a) for a in args):
            return name
    # Otherwise, honour the conventional name if present.
    if DEFAULT_MCP_SERVER in servers:
        return DEFAULT_MCP_SERVER
    return DEFAULT_MCP_SERVER


def desired_allow_entries(server: str) -> list[str]:
    """The full set of allowlist entries filing authority requires.

    MCP tool names (namespaced per the resolved server) + the CLI-fallback
    Bash rules (bare-relative and $CLAUDE_PROJECT_DIR forms, mirroring the
    sync-script allowlist convention)."""
    entries = [f"mcp__{server}__{op}" for op in MCP_WRITE_OPS]
    entries += [
        f"Bash(python3 {ISSUE_TRACKER_CLI}:*)",
        f"Bash(python3 $CLAUDE_PROJECT_DIR/{ISSUE_TRACKER_CLI}:*)",
    ]
    return entries


def merge_allow(existing: list, desired: list[str]) -> tuple[list, list[str]]:
    """Additively merge desired entries into existing, preserving order.

    Returns (merged_list, added_entries). Existing entries are never removed or
    reordered; only genuinely-absent desired entries are appended (idempotent)."""
    merged = list(existing)
    present = set(existing)
    added: list[str] = []
    for entry in desired:
        if entry not in present:
            merged.append(entry)
            present.add(entry)
            added.append(entry)
    return merged, added


def provision(root: Path, *, force: bool = False, dry_run: bool = False) -> dict:
    """Provision filing authority into settings.json. Returns a result dict.

    Keys: status ('provisioned' | 'noop' | 'skipped-not-enabled' | 'error'),
    added (list of new entries), server (resolved MCP server name), message."""
    if not force and not filing_authority_enabled(root):
        return {"status": "skipped-not-enabled", "added": [], "server": None,
                "message": "issue-filing-authority.enabled is not true; "
                           "nothing provisioned (opt-in gate)."}

    server = mcp_server_name(root)
    desired = desired_allow_entries(server)
    settings_path = root / SETTINGS_REL

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return {"status": "error", "added": [], "server": server,
                    "message": f"cannot parse {settings_path}: {exc}"}
    if not isinstance(settings, dict):
        return {"status": "error", "added": [], "server": server,
                "message": f"{settings_path} is not a JSON object"}

    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []

    merged, added = merge_allow(allow, desired)
    if not added:
        return {"status": "noop", "added": [], "server": server,
                "message": "all filing-authority allowlist entries already "
                           "present; no changes."}

    if dry_run:
        return {"status": "provisioned", "added": added, "server": server,
                "message": f"[dry-run] would add {len(added)} entr(y/ies)."}

    perms["allow"] = merged
    settings["permissions"] = perms
    # Write back, preserving all other settings keys (hooks, etc.).
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=4, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return {"status": "provisioned", "added": added, "server": server,
            "message": f"added {len(added)} filing-authority allowlist entr(y/ies)."}


def _self_test() -> int:
    """In-memory checks of the merge + derivation logic. Returns exit code."""
    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {label}")

    # merge_allow: additive + idempotent.
    desired = desired_allow_entries("grimoire-issue-tracker")
    merged, added = merge_allow([], desired)
    check("merge into empty adds all desired", added == desired and merged == desired)

    merged2, added2 = merge_allow(merged, desired)
    check("second merge is a no-op (idempotent)", added2 == [] and merged2 == merged)

    # additive: preserves an unrelated existing entry, appends new ones.
    pre = ["Write(.scaffold-upstream.conf)"]
    merged3, added3 = merge_allow(pre, desired)
    check("existing unrelated entry preserved",
          merged3[0] == "Write(.scaffold-upstream.conf)")
    check("existing entry never removed", "Write(.scaffold-upstream.conf)" in merged3)
    check("new entries appended after existing", set(desired).issubset(set(merged3)))

    # partial-present: only the genuinely-absent entries are added.
    partial = ["mcp__grimoire-issue-tracker__create_issue"]
    merged4, added4 = merge_allow(partial, desired)
    check("partial-present merge skips the already-present entry",
          "mcp__grimoire-issue-tracker__create_issue" not in added4)
    check("partial-present merge adds the rest",
          len(added4) == len(desired) - 1)

    # desired entries: MCP write ops + both CLI forms, correct namespacing.
    check("desired includes create_issue MCP tool",
          "mcp__grimoire-issue-tracker__create_issue" in desired)
    check("desired includes ensure_label MCP tool",
          "mcp__grimoire-issue-tracker__ensure_label" in desired)
    check("desired includes bare CLI Bash rule",
          f"Bash(python3 {ISSUE_TRACKER_CLI}:*)" in desired)
    check("desired includes $CLAUDE_PROJECT_DIR CLI Bash rule",
          f"Bash(python3 $CLAUDE_PROJECT_DIR/{ISSUE_TRACKER_CLI}:*)" in desired)
    check("read-only ops are NOT provisioned (list not filing)",
          not any("list_issues" in e for e in desired))

    # custom server namespacing.
    custom = desired_allow_entries("acme-tracker")
    check("custom server name namespaces the MCP tools",
          "mcp__acme-tracker__create_issue" in custom)

    # find_root: delegates to issue_tracker.find_repo_root() (#335/#336) but
    # preserves the never-None, cwd-fallback-on-miss external contract.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td_resolved = Path(td).resolve()
        hit_root = td_resolved / "proj"
        (hit_root / ".claude").mkdir(parents=True)
        (hit_root / CONFIG_REL).write_text("{}", encoding="utf-8")
        nested = hit_root / "a" / "b"
        nested.mkdir(parents=True)
        check("find_root walks up to the dir holding grimoire-config.json",
              find_root(nested) == hit_root)

        miss_root = td_resolved / "nowhere"
        miss_root.mkdir()
        check("find_root falls back to start on a miss (never None)",
              find_root(miss_root) == miss_root)

    # End-to-end against a temp tree: opt-in gate, provisioning, idempotency,
    # additivity, server derivation from .mcp.json.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".claude").mkdir()
        cfg = {"schema-version": 4, "name": "T"}
        (root / CONFIG_REL).write_text(json.dumps(cfg), encoding="utf-8")
        (root / MCP_JSON_REL).write_text(json.dumps({"mcpServers": {
            "grimoire-issue-tracker": {"command": "python3",
                                       "args": [".claude/mcp-servers/issue-tracker/server.py"]}}}),
            encoding="utf-8")
        # settings.json with only hooks (no permissions) — must be preserved.
        (root / SETTINGS_REL).write_text(json.dumps({"hooks": {"X": 1}}), encoding="utf-8")

        # Gate: opt-in absent → skipped, no write.
        r = provision(root)
        check("opt-in absent → skipped-not-enabled", r["status"] == "skipped-not-enabled")
        after = json.loads((root / SETTINGS_REL).read_text())
        check("skipped provisioning writes nothing", "permissions" not in after)

        # Enable the opt-in, then provision.
        cfg["issue-filing-authority"] = {"enabled": True}
        (root / CONFIG_REL).write_text(json.dumps(cfg), encoding="utf-8")
        r = provision(root)
        check("opt-in true → provisioned", r["status"] == "provisioned")
        after = json.loads((root / SETTINGS_REL).read_text())
        check("hooks preserved after provisioning", after.get("hooks") == {"X": 1})
        allow = after["permissions"]["allow"]
        check("all desired entries written", set(desired).issubset(set(allow)))
        check("server resolved from .mcp.json", r["server"] == "grimoire-issue-tracker")

        # Idempotency: second provision is a no-op.
        r2 = provision(root)
        check("second provision is a no-op", r2["status"] == "noop" and r2["added"] == [])
        after2 = json.loads((root / SETTINGS_REL).read_text())
        check("no duplicate entries on re-run",
              len(after2["permissions"]["allow"]) == len(allow))

        # Additivity: a pre-existing unrelated allow entry survives a fresh run.
        (root / SETTINGS_REL).write_text(json.dumps(
            {"permissions": {"allow": ["Write(foo)"]}}), encoding="utf-8")
        provision(root)
        after3 = json.loads((root / SETTINGS_REL).read_text())
        check("unrelated allow entry survives provisioning",
              "Write(foo)" in after3["permissions"]["allow"])

        # Custom server name in .mcp.json is honoured.
        (root / MCP_JSON_REL).write_text(json.dumps({"mcpServers": {
            "my-tracker": {"command": "python3",
                           "args": [".claude/mcp-servers/issue-tracker/server.py"]}}}),
            encoding="utf-8")
        (root / SETTINGS_REL).write_text(json.dumps({}), encoding="utf-8")
        r3 = provision(root)
        check("custom .mcp.json server name derived", r3["server"] == "my-tracker")
        after4 = json.loads((root / SETTINGS_REL).read_text())
        check("custom-namespaced MCP tool written",
              "mcp__my-tracker__create_issue" in after4["permissions"]["allow"])

    print("PASS" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="provision_filing_authority.py",
        description="Provision issue-filing authority into .claude/settings.json.")
    ap.add_argument("--root", default=None, help="Project root (default: auto-detect).")
    ap.add_argument("--force", action="store_true",
                    help="Provision even if the config opt-in is absent/false.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report changes without writing.")
    ap.add_argument("--self-test", action="store_true", help="Run in-memory self-test.")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(_self_test())

    root = Path(args.root).resolve() if args.root else find_root()
    result = provision(root, force=args.force, dry_run=args.dry_run)
    print(f"[{result['status']}] {result['message']}")
    if result["added"]:
        for e in result["added"]:
            print(f"  + {e}")
    sys.exit(1 if result["status"] == "error" else 0)


if __name__ == "__main__":
    main()
