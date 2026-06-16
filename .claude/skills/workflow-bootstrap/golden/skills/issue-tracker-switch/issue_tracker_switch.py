#!/usr/bin/env python3
"""issue-tracker-switch helper for the Grimoire issue-tracker-switch skill.

Reads and writes the `issue-tracker` block in `.claude/grimoire-config.json`.
Sub-commands: set, add, remove, list.

Does NOT modify issue_tracker.py (the abstraction/backend library). This
file is the write-path companion; the read-path is issue_tracker.py's
load_config().

Design authority: docs/design/issue-tracker-design.md §10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_PROVIDERS = {"roadmap", "github", "grimoire"}
VALID_AUDIENCES = {"internal", "external"}
CONFIG_FILE = ".claude/grimoire-config.json"

# The synthesized default returned when the block is absent.  Mirrors
# issue_tracker.py's DEFAULT_TRACKER_CONFIG exactly.
DEFAULT_TRACKER_ENTRY = {
    "name": "default",
    "provider": "roadmap",
    "repo": None,
    "audience": "internal",
    "labels": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_config(start: Path | None = None) -> Path:
    """Walk up from start (or cwd) to find grimoire-config.json."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        p = candidate / CONFIG_FILE
        if p.exists():
            return p
    # Fallback: relative to cwd (caller surfaces the error)
    return Path(CONFIG_FILE)


def load_full_config(config_path: Path) -> dict:
    """Load the full grimoire-config.json as a dict."""
    if not config_path.exists():
        sys.exit(f"Error: config not found at '{config_path}'. "
                 "Run `workflow-bootstrap --restore` to restore framework files.")
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"Error: cannot parse config at '{config_path}': {exc}")


def write_full_config(config_path: Path, full: dict) -> None:
    """Write the full config back, preserving all other fields."""
    config_path.write_text(json.dumps(full, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")


def validate_provider(provider: str) -> str:
    """Return the lowercased provider or exit with an error."""
    p = provider.lower()
    if p not in VALID_PROVIDERS:
        sys.exit(f"Error: '{provider}' is not a known provider. "
                 f"Valid providers: {', '.join(sorted(VALID_PROVIDERS))}.")
    return p


def validate_repo(provider: str, repo: str | None) -> str | None:
    """Validate repo is present for github and null for roadmap."""
    if provider == "github":
        if not repo:
            sys.exit("Error: provider 'github' requires a non-null repo "
                     "in 'owner/repo' format.")
        if "/" not in repo or " " in repo:
            sys.exit(f"Error: repo '{repo}' must be in 'owner/repo' format "
                     "(at least one '/', no spaces).")
        return repo
    if provider == "roadmap" and repo:
        sys.exit("Error: provider 'roadmap' does not use a repo; "
                 "pass no repo or leave it blank.")
    return None


def validate_audience(audience: str) -> str:
    a = audience.lower()
    if a not in VALID_AUDIENCES:
        sys.exit(f"Error: '{audience}' is not a known audience. "
                 f"Valid: {', '.join(sorted(VALID_AUDIENCES))}.")
    return a


def validate_name(name: str) -> str:
    if not name or " " in name:
        sys.exit(f"Error: tracker name '{name}' must be non-empty and "
                 "contain no spaces (kebab-case recommended).")
    return name


def get_or_default_block(full: dict) -> dict:
    """Return the issue-tracker block, synthesizing the roadmap default if absent."""
    block = full.get("issue-tracker")
    if block is None:
        return {
            "trackers": [dict(DEFAULT_TRACKER_ENTRY)],
            "default-for-filing": "default",
        }
    return block


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------


def cmd_set(args: argparse.Namespace, config_path: Path) -> None:
    """Replace the entire issue-tracker block with a single tracker."""
    provider = validate_provider(args.provider)
    repo = validate_repo(provider, getattr(args, "repo", None))
    name = validate_name(getattr(args, "name", None) or "default")
    audience = validate_audience(getattr(args, "audience", None) or "internal")
    labels = (
        [l.strip() for l in args.labels.split(",") if l.strip()]
        if getattr(args, "labels", None)
        else []
    )

    full = load_full_config(config_path)
    current_block = full.get("issue-tracker")

    new_entry = {
        "name": name,
        "provider": provider,
        "repo": repo,
        "audience": audience,
        "labels": labels,
    }
    new_block = {
        "trackers": [new_entry],
        "default-for-filing": name,
    }

    # Idempotency: check if already in the requested state
    if current_block is not None:
        trackers = current_block.get("trackers", [])
        if (
            len(trackers) == 1
            and trackers[0].get("provider") == provider
            and trackers[0].get("repo") == repo
            and trackers[0].get("audience") == audience
            and trackers[0].get("name") == name
        ):
            print("Issue tracker is already configured as requested. "
                  "No changes made.")
            return

    full["issue-tracker"] = new_block
    write_full_config(config_path, full)
    print(f"Issue tracker set to provider='{provider}'"
          + (f", repo='{repo}'" if repo else "")
          + f", name='{name}', audience='{audience}'.")


def cmd_add(args: argparse.Namespace, config_path: Path) -> None:
    """Append a new tracker to the existing list."""
    provider = validate_provider(args.provider)
    repo = validate_repo(provider, args.repo)
    name = validate_name(args.name)
    audience = validate_audience(args.audience)
    labels = (
        [l.strip() for l in args.labels.split(",") if l.strip()]
        if getattr(args, "labels", None)
        else []
    )
    make_default = getattr(args, "default", False)

    full = load_full_config(config_path)
    block = get_or_default_block(full)

    # Check for existing name
    existing = [t for t in block["trackers"] if t["name"] == name]
    if existing:
        t = existing[0]
        if (
            t.get("provider") == provider
            and t.get("repo") == repo
            and t.get("audience") == audience
        ):
            print(f"Tracker '{name}' already exists with identical fields. "
                  "No changes made.")
            return
        sys.exit(f"Error: tracker '{name}' already exists with different fields. "
                 "Use 'remove' then 'add' to replace it.")

    new_entry = {
        "name": name,
        "provider": provider,
        "repo": repo,
        "audience": audience,
        "labels": labels,
    }
    block["trackers"].append(new_entry)
    if make_default:
        block["default-for-filing"] = name

    full["issue-tracker"] = block
    write_full_config(config_path, full)
    default_note = " (promoted to default-for-filing)" if make_default else ""
    print(f"Tracker '{name}' added (provider='{provider}'"
          + (f", repo='{repo}'" if repo else "")
          + f", audience='{audience}'){default_note}.")


def cmd_remove(args: argparse.Namespace, config_path: Path) -> None:
    """Remove a tracker by name."""
    name = args.name

    full = load_full_config(config_path)
    block = get_or_default_block(full)

    trackers = block.get("trackers", [])
    if len(trackers) <= 1:
        sys.exit("Error: cannot remove the last tracker. "
                 "Use 'set' to replace the tracker instead.")

    if block.get("default-for-filing") == name:
        sys.exit(f"Error: '{name}' is the current default-for-filing tracker. "
                 "Promote another tracker to default first, then remove this one.")

    before = len(trackers)
    block["trackers"] = [t for t in trackers if t["name"] != name]
    if len(block["trackers"]) == before:
        sys.exit(f"Error: tracker '{name}' not found. "
                 f"Known trackers: {[t['name'] for t in trackers]}.")

    full["issue-tracker"] = block
    write_full_config(config_path, full)
    print(f"Tracker '{name}' removed.")


def cmd_list(args: argparse.Namespace, config_path: Path) -> None:
    """Print the current issue-tracker config in a human-readable table."""
    full = load_full_config(config_path)
    block = full.get("issue-tracker")
    if block is None:
        print("issue-tracker block: absent (roadmap default synthesized at runtime)")
        print()
        print("  name=default  provider=roadmap  repo=<none>  "
              "audience=internal  labels=[]  [default-for-filing]")
        return

    default_name = block.get("default-for-filing", "")
    trackers = block.get("trackers", [])
    print(f"issue-tracker block ({len(trackers)} tracker(s), "
          f"default-for-filing='{default_name}'):")
    print()
    for t in trackers:
        is_default = "  [default-for-filing]" if t["name"] == default_name else ""
        repo_str = t.get("repo") or "<none>"
        labels_str = ",".join(t.get("labels") or []) or "[]"
        print(f"  name={t['name']}  provider={t.get('provider')}  "
              f"repo={repo_str}  audience={t.get('audience')}  "
              f"labels={labels_str}{is_default}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="issue_tracker_switch.py",
        description="Set or update the issue-tracker block in grimoire-config.json.",
    )
    p.add_argument(
        "--config",
        metavar="PATH",
        help=f"Path to grimoire-config.json (default: auto-detect from cwd up).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # set
    s = sub.add_parser("set", help="Replace the entire issue-tracker block.")
    s.add_argument("provider", help="Provider: roadmap | github | grimoire")
    s.add_argument("repo", nargs="?", default=None,
                   help="GitHub repo in owner/repo format (required for github).")
    s.add_argument("--name", default="default", help="Tracker name (default: 'default')")
    s.add_argument("--audience", default="internal",
                   help="Audience: internal | external (default: internal)")
    s.add_argument("--labels", default=None,
                   help="Comma-separated labels auto-applied to every filed issue.")

    # add
    a = sub.add_parser("add", help="Append a new tracker to the list.")
    a.add_argument("provider", help="Provider: roadmap | github | grimoire")
    a.add_argument("repo", nargs="?", default=None,
                   help="GitHub repo in owner/repo format (required for github).")
    a.add_argument("--name", required=True, help="Unique tracker name.")
    a.add_argument("--audience", required=True,
                   help="Audience: internal | external")
    a.add_argument("--labels", default=None,
                   help="Comma-separated labels auto-applied to every filed issue.")
    a.add_argument("--default", action="store_true",
                   help="Promote this tracker to default-for-filing.")

    # remove
    r = sub.add_parser("remove", help="Remove a tracker by name.")
    r.add_argument("name", help="Tracker name to remove.")

    # list
    sub.add_parser("list", help="Print current issue-tracker config.")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else find_config()

    dispatch = {
        "set": cmd_set,
        "add": cmd_add,
        "remove": cmd_remove,
        "list": cmd_list,
    }
    dispatch[args.command](args, config_path)


if __name__ == "__main__":
    main()
