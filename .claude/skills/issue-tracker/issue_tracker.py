#!/usr/bin/env python3
"""Issue-tracker abstraction for Grimoire scaffolding.

Implements nine operations (list / get / create / update / close / comment / label /
search / ensure_label) over a normalized Issue object. Two backends ship:
`roadmap` (reads and writes docs/roadmap.md ## Backlog, zero network) and
`github` (wraps `gh` with the R1-recommended field-filtered, body-on-demand,
server-side-filtered, session-snapshot-cached access pattern). A
routing/aggregation/cache layer sits above the backends and is the only entry
point for callers.

ensure_label creates a label if absent (github: gh label create, treat
already-exists as success; roadmap: no-op; grimoire: not_implemented). It is
called automatically inside create() and label() flows when a requested label
is unknown to the provider.

Epic support: Issue objects carry an optional `issue_type` field ("issue" |
"epic") and `parent_epic_id` field (ID of the parent Epic, or None).  Creating
an issue with issue_type="epic" auto-applies the "epic" label and validates the
one-level nesting rule (Epics cannot be children of other Epics). list() accepts
an issue_type filter to return only Epics or only plain issues.

Authoritative design: docs/design/issue-tracker-design.md
Cost rationale: docs/grimoire/issue-tracker-cost-spike.md

CLI:  python3 issue_tracker.py <operation> [options]
      python3 issue_tracker.py --help
      python3 issue_tracker.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 30                       # R1 §5: bounded --limit
VALID_PROVIDERS = {"roadmap", "github", "grimoire"}
VALID_AUDIENCES = {"internal", "external"}
VALID_STATES = {"open", "closed", "all"}
ROADMAP_SECTION = "## Backlog"
CLOSED_SECTION = "## Closed"
ROADMAP_FILE = "docs/roadmap.md"
CONFIG_FILE = ".claude/grimoire-config.json"

# ---------------------------------------------------------------------------
# Normalized Issue object
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """Normalized issue object; identical shape across all backends.

    body=None means the body was not fetched (body-on-demand rule).
    Callers needing the body must call get() explicitly.
    """

    id: str                         # Unique within tracker
    title: str
    state: str                      # "open" | "closed"
    audience: str                   # "internal" | "external"
    tracker: str                    # tracker name from config
    number: int | None = None       # GitHub issue number; None for roadmap
    body: str | None = None         # None = not fetched; empty string = no body
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    created_at: str | None = None
    issue_type: str = "issue"       # "issue" | "epic"
    parent_epic_id: Optional[str] = None  # ID of parent Epic; None for standalone

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class TrackerError(Exception):
    """Structured error raised by backends and the abstraction layer.

    Attributes:
        code:    short machine-readable code (e.g. "not_found", "auth_error").
        message: human-readable explanation.
        tracker: tracker name involved (may be None for config errors).
    """

    def __init__(self, code: str, message: str, tracker: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.tracker = tracker

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "tracker": self.tracker}


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def find_repo_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """Walk up from start (or cwd) to find the repo root containing CONFIG_FILE."""
    current = (start or pathlib.Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILE).exists():
            return candidate
    # Fallback: return cwd (caller will surface the error when config is read)
    return pathlib.Path.cwd().resolve()


DEFAULT_TRACKER_CONFIG = {
    "trackers": [
        {
            "name": "default",
            "provider": "roadmap",
            "repo": None,
            "audience": "internal",
            "labels": [],
        }
    ],
    "default-for-filing": "default",
}


def load_config(config_path: pathlib.Path | None = None) -> dict:
    """Load grimoire-config.json and return the issue-tracker block.

    If the block is absent, synthesize the roadmap-default config (§5.2 of the
    design doc). This ensures roadmap-only projects need zero config changes.
    """
    if config_path is None:
        repo_root = find_repo_root()
        config_path = repo_root / CONFIG_FILE

    if not config_path.exists():
        # No config at all → synthesize roadmap default
        return dict(DEFAULT_TRACKER_CONFIG)

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise TrackerError("config_error", f"Cannot read config: {exc}")

    issue_tracker_block = raw.get("issue-tracker")
    if issue_tracker_block is None:
        return dict(DEFAULT_TRACKER_CONFIG)
    return issue_tracker_block


def get_tracker_entry(config: dict, name: str) -> dict:
    """Return the tracker entry with the given name, or raise TrackerError."""
    for t in config.get("trackers", []):
        if t["name"] == name:
            return t
    names = [t["name"] for t in config.get("trackers", [])]
    raise TrackerError("not_found", f"Tracker '{name}' not found. Known: {names}")


def trackers_for_audience(config: dict, audience: str) -> list[dict]:
    """Return tracker entries whose audience matches the given value."""
    return [t for t in config.get("trackers", []) if t.get("audience") == audience]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Convert a title string to a URL-safe slug used as roadmap Issue id."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-") or "issue"


def filter_hash(state: str, labels: list[str], limit: int) -> str:
    """Stable hash of the filter options used as the cache key discriminator."""
    key = json.dumps({"state": state, "labels": sorted(labels), "limit": limit},
                     sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def cache_key(provider: str, repo: str | None, state: str,
              labels: list[str], limit: int) -> tuple:
    """Composite session-cache key: (provider, repo, filter_hash)."""
    return (provider, repo, filter_hash(state, labels, limit))


# ---------------------------------------------------------------------------
# Backend base class
# ---------------------------------------------------------------------------


class Backend:
    """Abstract base class for issue-tracker backends.

    Subclasses implement the seven operations for a SINGLE tracker. Routing,
    aggregation, and caching live in the IssueTracker abstraction layer above.
    """

    def __init__(self, tracker_entry: dict, repo_root: pathlib.Path):
        self.tracker_name: str = tracker_entry["name"]
        self.tracker_entry: dict = tracker_entry
        self.repo_root: pathlib.Path = repo_root

    def list(self, state: str = "open", labels: list[str] | None = None,
             limit: int = DEFAULT_LIMIT, **kwargs) -> list[Issue]:
        """Return issues matching the filter criteria. body=None for all."""
        raise NotImplementedError

    def get(self, issue_id: str) -> Issue:
        """Return a single issue; always includes body."""
        raise NotImplementedError

    def create(self, title: str, body: str, labels: list[str] | None = None,
               audience: str = "internal") -> Issue:
        """Create a new issue and return it."""
        raise NotImplementedError

    def update(self, issue_id: str, title: str | None = None,
               body: str | None = None, labels: list[str] | None = None,
               state: str | None = None) -> Issue:
        """Update fields on an existing issue and return it."""
        raise NotImplementedError

    def close(self, issue_id: str) -> Issue:
        """Close an issue and return it."""
        raise NotImplementedError

    def label(self, issue_id: str, add: list[str] | None = None,
              remove: list[str] | None = None) -> Issue:
        """Add/remove labels on an issue and return it."""
        raise NotImplementedError

    def search(self, query: str, state: str = "open",
               limit: int = DEFAULT_LIMIT) -> list[Issue]:
        """Full-text search; return matching issues. body=None for all."""
        raise NotImplementedError

    def comment(self, issue_id: str, body: str) -> Issue:
        """Add a comment to an issue (no edit of title/body/labels)."""
        raise NotImplementedError

    def ensure_label(self, name: str) -> None:
        """Create the label *name* if it does not already exist.

        github:   gh label create — treats already-exists as success (idempotent).
        roadmap:  no-op (roadmap labels are free-form, always valid).
        grimoire: raises TrackerError("not_implemented", …).
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# RoadmapBackend
# ---------------------------------------------------------------------------


class RoadmapBackend(Backend):
    """Reads and writes docs/roadmap.md ## Backlog as the issue store.

    Zero network calls. Behaviour identical to today for projects without an
    issue-tracker config block. Only ## Backlog is touched; all other roadmap
    sections are preserved unchanged.
    """

    LABEL_RE = re.compile(r"<!--\s*labels:\s*([^>]+)\s*-->")
    CLOSED_MARKER = "<!-- closed -->"

    def _roadmap_path(self) -> pathlib.Path:
        return self.repo_root / ROADMAP_FILE

    def _read_sections(self) -> dict[str, list[str]]:
        """Parse roadmap.md into named sections (header → lines list)."""
        path = self._roadmap_path()
        if not path.exists():
            return {}
        sections: dict[str, list[str]] = {}
        current_section: str | None = None
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.rstrip("\n")
                if line.startswith("## "):
                    current_section = line
                    sections[current_section] = []
                elif current_section is not None:
                    sections[current_section].append(line)
        return sections

    def _write_sections(self, sections: dict[str, list[str]]) -> None:
        """Write sections back to roadmap.md preserving non-section header text."""
        path = self._roadmap_path()
        if not path.exists():
            raise TrackerError("io_error", f"Roadmap file not found: {path}",
                               self.tracker_name)
        # Read original to capture preamble (before the first ## section)
        with open(path, "r", encoding="utf-8") as fh:
            all_lines = fh.read().splitlines()

        # Identify the line positions of each ## section in the original
        section_starts: list[tuple[int, str]] = []
        for i, line in enumerate(all_lines):
            if line.startswith("## "):
                section_starts.append((i, line))

        if not section_starts:
            # No sections at all; nothing to rewrite safely
            return

        preamble = all_lines[: section_starts[0][0]]
        output_lines = list(preamble)

        for idx, (lineno, header) in enumerate(section_starts):
            output_lines.append(header)
            body = sections.get(header, [])
            # Determine original trailing context between sections
            end = (section_starts[idx + 1][0]
                   if idx + 1 < len(section_starts) else len(all_lines))
            # Use original spacing (blank lines) after the last bullet
            original_body = all_lines[lineno + 1: end]
            if header in sections:
                # Rewrite this section with updated bullets
                # Preserve a single trailing blank if the original had one
                trailing_blank = (original_body and original_body[-1] == "")
                output_lines.extend(body)
                if trailing_blank:
                    output_lines.append("")
            else:
                output_lines.extend(original_body)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(output_lines) + "\n")

    def _parse_bullet(self, line: str, tracker_name: str) -> Issue | None:
        """Parse a single bullet line into an Issue; return None if not a bullet."""
        stripped = line.strip()
        if not stripped.startswith("- "):
            return None
        content = stripped[2:].strip()
        label_match = self.LABEL_RE.search(content)
        labels: list[str] = []
        if label_match:
            labels = [lb.strip() for lb in label_match.group(1).split(",")
                      if lb.strip()]
            content = self.LABEL_RE.sub("", content).strip()
        title = content
        issue_id = slugify(title)
        return Issue(
            id=issue_id,
            title=title,
            state="open",
            audience="internal",
            tracker=tracker_name,
            number=None,
            body=None,
            labels=labels,
            url=None,
            created_at=None,
        )

    def _format_bullet(self, title: str, body: str | None,
                       labels: list[str]) -> list[str]:
        """Format a bullet entry (possibly multi-line) for the Backlog section."""
        label_comment = (
            f"  <!-- labels: {', '.join(labels)} -->" if labels else ""
        )
        first_line = f"- {title}{label_comment}"
        lines = [first_line]
        if body and body.strip():
            for body_line in body.strip().splitlines():
                lines.append(f"  {body_line}")
        return lines

    def list(self, state: str = "open", labels: list[str] | None = None,
             limit: int = DEFAULT_LIMIT, **kwargs) -> list[Issue]:
        """Extract bullet lines from ## Backlog."""
        sections = self._read_sections()
        backlog_lines = sections.get(ROADMAP_SECTION, [])
        issues: list[Issue] = []
        for line in backlog_lines:
            issue = self._parse_bullet(line, self.tracker_name)
            if issue is None:
                continue
            if labels:
                if not all(lb in issue.labels for lb in labels):
                    continue
            issues.append(issue)
            if len(issues) >= limit:
                break
        # roadmap backend has only "open" issues (Backlog is always open)
        if state == "closed":
            return []
        return issues

    def get(self, issue_id: str) -> Issue:
        """Return the issue with matching id; includes body (continuation lines)."""
        sections = self._read_sections()
        backlog_lines = sections.get(ROADMAP_SECTION, [])
        found_issue: Issue | None = None
        body_lines: list[str] = []
        collecting_body = False

        for line in backlog_lines:
            if collecting_body:
                if line.startswith("  ") or line.startswith("\t"):
                    body_lines.append(line.strip())
                    continue
                else:
                    collecting_body = False

            issue = self._parse_bullet(line, self.tracker_name)
            if issue is not None and issue.id == issue_id:
                found_issue = issue
                collecting_body = True

        if found_issue is None:
            raise TrackerError("not_found", f"Issue '{issue_id}' not found",
                               self.tracker_name)
        found_issue.body = "\n".join(body_lines) if body_lines else ""
        return found_issue

    def create(self, title: str, body: str, labels: list[str] | None = None,
               audience: str = "internal") -> Issue:
        """Append a new bullet to ## Backlog."""
        labels = labels or []
        sections = self._read_sections()
        if ROADMAP_SECTION not in sections:
            raise TrackerError("io_error",
                               f"Section '{ROADMAP_SECTION}' not found in roadmap",
                               self.tracker_name)
        new_lines = self._format_bullet(title, body, labels)
        sections[ROADMAP_SECTION].extend(new_lines)
        self._write_sections(sections)
        issue_id = slugify(title)
        return Issue(
            id=issue_id,
            title=title,
            state="open",
            audience="internal",
            tracker=self.tracker_name,
            number=None,
            body=body,
            labels=labels,
            url=None,
            created_at=None,
        )

    def update(self, issue_id: str, title: str | None = None,
               body: str | None = None, labels: list[str] | None = None,
               state: str | None = None) -> Issue:
        """Edit the matching bullet in-place, replacing its continuation block.

        A bullet's body lives in the indented continuation lines beneath it.
        Rewriting must therefore drop the OLD continuation lines (or they would
        be duplicated) and, when ``body`` is None, preserve the existing body
        (so a title/label-only edit does not erase it).
        """
        current = self.get(issue_id)  # raises not_found; includes existing body
        new_title = title if title is not None else current.title
        new_labels = labels if labels is not None else current.labels
        new_body = body if body is not None else current.body
        sections = self._read_sections()
        backlog_lines = sections.get(ROADMAP_SECTION, [])
        new_lines: list[str] = []
        skip_continuations = False
        for line in backlog_lines:
            if skip_continuations:
                if line.startswith("  ") or line.startswith("\t"):
                    continue  # drop a stale continuation line of the edited bullet
                skip_continuations = False
            issue = self._parse_bullet(line, self.tracker_name)
            if issue is not None and issue.id == issue_id:
                new_lines.extend(
                    self._format_bullet(new_title, new_body, new_labels)
                )
                skip_continuations = True
            else:
                new_lines.append(line)
        sections[ROADMAP_SECTION] = new_lines
        self._write_sections(sections)
        return Issue(
            id=slugify(new_title),
            title=new_title,
            state="open",
            audience="internal",
            tracker=self.tracker_name,
            number=None,
            body=new_body,
            labels=new_labels,
            url=None,
            created_at=None,
        )

    def close(self, issue_id: str) -> Issue:
        """Remove the matching bullet (and its continuation lines) from ## Backlog.

        If a ## Closed section exists, appends the full bullet block there instead.
        """
        sections = self._read_sections()
        backlog_lines = sections.get(ROADMAP_SECTION, [])
        remaining = []
        closed_issue: Issue | None = None
        skip_continuations = False
        closed_block: list[str] = []

        for line in backlog_lines:
            if skip_continuations:
                # Continuation lines for the closed bullet are indented
                if line.startswith("  ") or line.startswith("\t"):
                    closed_block.append(line)
                    continue
                else:
                    # No longer a continuation — stop skipping
                    skip_continuations = False

            issue = self._parse_bullet(line, self.tracker_name)
            if issue is not None and issue.id == issue_id:
                closed_issue = issue
                closed_block.append(line)
                skip_continuations = True
                # Do not add to remaining
            else:
                remaining.append(line)

        if closed_issue is None:
            raise TrackerError("not_found", f"Issue '{issue_id}' not found",
                               self.tracker_name)
        if CLOSED_SECTION in sections:
            sections[CLOSED_SECTION].extend(closed_block)
        sections[ROADMAP_SECTION] = remaining
        self._write_sections(sections)
        closed_issue.state = "closed"
        return closed_issue

    def label(self, issue_id: str, add: list[str] | None = None,
              remove: list[str] | None = None) -> Issue:
        """Update the labels HTML comment on the matching bullet."""
        add = add or []
        remove = remove or []
        issue = self.get(issue_id)
        new_labels = [lb for lb in issue.labels if lb not in remove]
        for lb in add:
            if lb not in new_labels:
                new_labels.append(lb)
        return self.update(issue_id, labels=new_labels)

    def search(self, query: str, state: str = "open",
               limit: int = DEFAULT_LIMIT) -> list[Issue]:
        """Full-text keyword match on title+body within Backlog bullets."""
        all_issues = self.list(state=state, limit=limit)
        q = query.lower()
        results = []
        for issue in all_issues:
            text = (issue.title or "").lower()
            if q in text:
                results.append(issue)
        return results

    def comment(self, issue_id: str, body: str) -> Issue:
        """Append a comment as an indented continuation block under the bullet.

        Flat-file trackers have no separate comment stream, so a comment is
        appended to the bullet body, prefixed so it reads as a comment. The
        write goes through update() to reuse the in-place bullet rewriter.
        """
        issue = self.get(issue_id)  # includes existing continuation-line body
        existing = issue.body or ""
        addition = f"[comment] {body.strip()}"
        combined = f"{existing}\n{addition}" if existing.strip() else addition
        return self.update(issue_id, body=combined)

    def ensure_label(self, name: str) -> None:  # noqa: ARG002
        """No-op: roadmap labels are free-form strings; any value is valid."""
        return


# ---------------------------------------------------------------------------
# GitHubBackend
# ---------------------------------------------------------------------------


class GitHubBackend(Backend):
    """GitHub Issues backend implementing R1 §5 access pattern.

    All reads use field-filtered JSON + jq-tsv projection (cheapest pattern).
    Body is fetched on demand only. Writes are batched per issue per session.
    Server-side filters (--state, --label, --search) are always passed to gh.
    Every call prefixes --repo <tracker.repo>.
    """

    # Pending write buffer: {issue_number: {field: value}}
    _pending: dict[int, dict[str, Any]]

    def __init__(self, tracker_entry: dict, repo_root: pathlib.Path):
        super().__init__(tracker_entry, repo_root)
        self.repo: str = tracker_entry.get("repo") or ""
        if not self.repo:
            raise TrackerError(
                "config_error",
                f"Tracker '{self.tracker_name}' (github) missing 'repo' field",
                self.tracker_name,
            )
        self._pending = {}

    @staticmethod
    def _gh_available() -> bool:
        """Return True if gh is on PATH; degrade gracefully if not."""
        try:
            result = subprocess.run(
                ["gh", "--version"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _run_gh(self, args: list[str]) -> str:
        """Run gh with the given args and return stdout.

        Raises TrackerError on non-zero exit or if gh is unavailable.
        """
        if not self._gh_available():
            raise TrackerError(
                "gh_unavailable",
                "gh CLI is not available or not authenticated. "
                "Install gh and run 'gh auth login' before using the github backend.",
                self.tracker_name,
            )
        cmd = ["gh"] + args
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            raise TrackerError("timeout", f"gh command timed out: {cmd}",
                               self.tracker_name)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "authentication" in stderr.lower() or "401" in stderr:
                raise TrackerError("auth_error",
                                   f"gh authentication failed: {stderr}",
                                   self.tracker_name)
            raise TrackerError("gh_error",
                               f"gh command failed (exit {result.returncode}): {stderr}",
                               self.tracker_name)
        return result.stdout

    def _parse_tsv_issues(self, tsv: str, tracker_name: str,
                          audience: str) -> list[Issue]:
        """Parse jq @tsv output into Issue objects (body=None, no get needed)."""
        issues: list[Issue] = []
        for line in tsv.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            number_str, state, title, labels_raw = (
                parts[0], parts[1], parts[2], parts[3]
            )
            url = parts[4] if len(parts) > 4 else None
            created_at = parts[5] if len(parts) > 5 else None
            try:
                number = int(number_str)
            except ValueError:
                continue
            labels: list[str] = []
            if labels_raw and labels_raw != "[]":
                # gh returns labels as a JSON array string in the TSV
                try:
                    label_list = json.loads(labels_raw)
                    labels = [lb.get("name", "") for lb in label_list
                              if isinstance(lb, dict)]
                except (json.JSONDecodeError, AttributeError):
                    labels = [lb.strip() for lb in labels_raw.split(",")
                              if lb.strip()]
            issues.append(Issue(
                id=str(number),
                number=number,
                title=title,
                state=state,
                audience=audience,
                tracker=tracker_name,
                body=None,
                labels=labels,
                url=url,
                created_at=created_at,
            ))
        return issues

    def list(self, state: str = "open", labels: list[str] | None = None,
             limit: int = DEFAULT_LIMIT, **kwargs) -> list[Issue]:
        """R1 §5: field-filtered JSON + jq-tsv, server-side state/label filter."""
        labels = labels or []
        effective_limit = min(limit, DEFAULT_LIMIT)
        audience = self.tracker_entry.get("audience", "internal")

        args = [
            "issue", "list",
            "--repo", self.repo,
            "--limit", str(effective_limit),
            "--json", "number,title,labels,state,url,createdAt",
            "--jq", (
                ".[] | [(.number|tostring), .state, .title, "
                "(.labels|tostring), .url, .createdAt] | @tsv"
            ),
        ]
        if state != "all":
            args += ["--state", state]
        if labels:
            for lb in labels:
                args += ["--label", lb]

        raw = self._run_gh(args)
        return self._parse_tsv_issues(raw, self.tracker_name, audience)

    def get(self, issue_id: str) -> Issue:
        """Fetch a single issue with body (body-on-demand)."""
        audience = self.tracker_entry.get("audience", "internal")
        try:
            number = int(issue_id)
        except ValueError:
            raise TrackerError("invalid_id",
                               f"GitHub issue id must be numeric, got: {issue_id!r}",
                               self.tracker_name)
        args = [
            "issue", "view", str(number),
            "--repo", self.repo,
            "--json", "number,title,body,state,labels,url,createdAt",
        ]
        raw = self._run_gh(args)
        data = json.loads(raw)
        labels = [lb.get("name", "") for lb in data.get("labels", [])
                  if isinstance(lb, dict)]
        return Issue(
            id=str(data["number"]),
            number=data["number"],
            title=data["title"],
            state=data["state"],
            audience=audience,
            tracker=self.tracker_name,
            body=data.get("body", ""),
            labels=labels,
            url=data.get("url"),
            created_at=data.get("createdAt"),
        )

    def create(self, title: str, body: str, labels: list[str] | None = None,
               audience: str = "internal") -> Issue:
        """Create a GitHub issue via gh; auto-apply tracker labels."""
        labels = labels or []
        # Merge tracker-level auto-labels (from config)
        tracker_labels = self.tracker_entry.get("labels", [])
        all_labels = list(dict.fromkeys(labels + tracker_labels))

        args = [
            "issue", "create",
            "--repo", self.repo,
            "--title", title,
            "--body", body,
        ]
        for lb in all_labels:
            args += ["--label", lb]

        raw = self._run_gh(args)
        # gh issue create outputs the URL of the new issue
        url = raw.strip()
        # Extract issue number from URL
        match = re.search(r"/issues/(\d+)$", url)
        number = int(match.group(1)) if match else None

        return Issue(
            id=str(number) if number else url,
            number=number,
            title=title,
            state="open",
            audience=audience,
            tracker=self.tracker_name,
            body=body,
            labels=all_labels,
            url=url,
            created_at=None,
        )

    def _queue_write(self, number: int, patch: dict[str, Any]) -> None:
        """Add to the pending write buffer; coalesce multiple writes per issue."""
        if number not in self._pending:
            self._pending[number] = {}
        self._pending[number].update(patch)

    def flush(self) -> None:
        """Flush pending writes as batched gh issue edit calls.

        For each buffered issue, all queued mutations (title, body, label
        add/removes, and full label replacements from update()) are coalesced
        into a single gh issue edit call — the R1 write-batching rule.
        set_labels (from update()) takes priority over incremental add_labels /
        remove_labels from label() when both appear in the same batch.
        """
        for number, patch in list(self._pending.items()):
            args = ["issue", "edit", str(number), "--repo", self.repo]
            if "title" in patch:
                args += ["--title", patch["title"]]
            if "body" in patch:
                args += ["--body", patch["body"]]
            if "set_labels" in patch:
                # Full replacement: fetch current labels then compute diff
                current = self.get(str(number))
                current_set = set(current.labels)
                desired_set = set(patch["set_labels"])
                for lb in desired_set - current_set:
                    args += ["--add-label", lb]
                for lb in current_set - desired_set:
                    args += ["--remove-label", lb]
            else:
                # Incremental label edits from label()
                if "add_labels" in patch:
                    for lb in patch["add_labels"]:
                        args += ["--add-label", lb]
                if "remove_labels" in patch:
                    for lb in patch["remove_labels"]:
                        args += ["--remove-label", lb]
            self._run_gh(args)
        self._pending.clear()

    def update(self, issue_id: str, title: str | None = None,
               body: str | None = None, labels: list[str] | None = None,
               state: str | None = None) -> Issue:
        """Queue an update; flush on explicit flush() or session end."""
        try:
            number = int(issue_id)
        except ValueError:
            raise TrackerError("invalid_id",
                               f"GitHub issue id must be numeric, got: {issue_id!r}",
                               self.tracker_name)
        patch: dict[str, Any] = {}
        if title is not None:
            patch["title"] = title
        if body is not None:
            patch["body"] = body
        if labels is not None:
            # Full label replacement: stored as set_labels; flush converts to
            # add/remove diff relative to a fresh get() if needed.  Use
            # set_labels (not add_labels/remove_labels) so the batch coalescer
            # in flush() can distinguish a replacement from an incremental edit.
            patch["set_labels"] = list(labels)
        if state is not None and state == "closed":
            # close is a separate operation; handle immediately
            return self.close(issue_id)
        self._queue_write(number, patch)
        # Return the issue as known (body may be stale until flush)
        return Issue(
            id=issue_id,
            number=number,
            title=title or "",
            state="open",
            audience=self.tracker_entry.get("audience", "internal"),
            tracker=self.tracker_name,
            body=body,
            labels=labels or [],
            url=None,
            created_at=None,
        )

    def close(self, issue_id: str) -> Issue:
        """Close a GitHub issue immediately."""
        try:
            number = int(issue_id)
        except ValueError:
            raise TrackerError("invalid_id",
                               f"GitHub issue id must be numeric, got: {issue_id!r}",
                               self.tracker_name)
        self._run_gh(["issue", "close", str(number), "--repo", self.repo])
        return Issue(
            id=issue_id,
            number=number,
            title="",
            state="closed",
            audience=self.tracker_entry.get("audience", "internal"),
            tracker=self.tracker_name,
            body=None,
            url=None,
            created_at=None,
        )

    def label(self, issue_id: str, add: list[str] | None = None,
              remove: list[str] | None = None) -> Issue:
        """Queue label changes for batch flush."""
        try:
            number = int(issue_id)
        except ValueError:
            raise TrackerError("invalid_id",
                               f"GitHub issue id must be numeric, got: {issue_id!r}",
                               self.tracker_name)
        patch: dict[str, Any] = {}
        if add:
            patch["add_labels"] = add
        if remove:
            patch["remove_labels"] = remove
        self._queue_write(number, patch)
        return Issue(
            id=issue_id,
            number=number,
            title="",
            state="open",
            audience=self.tracker_entry.get("audience", "internal"),
            tracker=self.tracker_name,
            body=None,
            url=None,
            created_at=None,
        )

    def search(self, query: str, state: str = "open",
               limit: int = DEFAULT_LIMIT) -> list[Issue]:
        """Server-side search via --search (cheapest pattern for focused queries)."""
        audience = self.tracker_entry.get("audience", "internal")
        effective_limit = min(limit, DEFAULT_LIMIT)
        search_expr = f"is:issue is:{state} {query}" if state != "all" else query

        args = [
            "issue", "list",
            "--repo", self.repo,
            "--search", search_expr,
            "--limit", str(effective_limit),
            "--json", "number,title,labels,state,url,createdAt",
            "--jq", (
                ".[] | [(.number|tostring), .state, .title, "
                "(.labels|tostring), .url, .createdAt] | @tsv"
            ),
        ]
        raw = self._run_gh(args)
        return self._parse_tsv_issues(raw, self.tracker_name, audience)

    def comment(self, issue_id: str, body: str) -> Issue:
        """Add a comment to a GitHub issue (applied immediately, not batched)."""
        try:
            number = int(issue_id)
        except ValueError:
            raise TrackerError("invalid_id",
                               f"GitHub issue id must be numeric, got: {issue_id!r}",
                               self.tracker_name)
        self._run_gh(["issue", "comment", str(number),
                      "--repo", self.repo, "--body", body])
        return Issue(
            id=issue_id,
            number=number,
            title="",
            state="open",
            audience=self.tracker_entry.get("audience", "internal"),
            tracker=self.tracker_name,
            body=None,
            url=None,
            created_at=None,
        )

    def ensure_label(self, name: str) -> None:
        """Create the label on the GitHub repo if it does not already exist.

        Uses ``gh label create``. GitHub returns exit-code 1 with an
        "already exists" message when the label is present — that is treated
        as success so the operation is idempotent.  Any other non-zero exit is
        re-raised as a TrackerError.
        """
        if not self._gh_available():
            raise TrackerError(
                "gh_unavailable",
                "gh CLI is not available or not authenticated.",
                self.tracker_name,
            )
        cmd = ["gh", "label", "create", name, "--repo", self.repo]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            raise TrackerError("timeout",
                               f"gh label create timed out for label {name!r}",
                               self.tracker_name)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # GitHub: "already exists" → treat as success (idempotent)
            if "already exists" in stderr.lower():
                return
            raise TrackerError(
                "gh_error",
                f"gh label create failed (exit {result.returncode}): {stderr}",
                self.tracker_name,
            )


# ---------------------------------------------------------------------------
# Abstraction layer (IssueTracker)
# ---------------------------------------------------------------------------


class IssueTracker:
    """Routing, aggregation, and session-snapshot cache above the backends.

    This is the only entry point callers should use. Backends only handle
    single-tracker operations; everything else lives here.
    """

    def __init__(self, config: dict, repo_root: pathlib.Path):
        self.config = config
        self.repo_root = repo_root
        # In-memory session cache: cache_key → list[Issue]
        self._cache: dict[tuple, list[Issue]] = {}
        # Backend instances keyed by tracker name
        self._backends: dict[str, Backend] = {}

    def _backend(self, tracker_entry: dict) -> Backend:
        """Return (and cache) the backend instance for a tracker entry."""
        name = tracker_entry["name"]
        if name not in self._backends:
            provider = tracker_entry.get("provider", "roadmap")
            if provider == "roadmap":
                self._backends[name] = RoadmapBackend(tracker_entry, self.repo_root)
            elif provider == "github":
                self._backends[name] = GitHubBackend(tracker_entry, self.repo_root)
            elif provider == "grimoire":
                raise TrackerError(
                    "not_implemented",
                    "The 'grimoire' provider is reserved and not yet implemented. "
                    "ensure_label is also not implemented for this provider.",
                    name,
                )
            else:
                raise TrackerError(
                    "unknown_provider",
                    f"Unknown provider '{provider}'. Valid: {VALID_PROVIDERS}",
                    name,
                )
        return self._backends[name]

    def _trackers_for_opts(self, tracker: str | None,
                           audience: str | None) -> list[dict]:
        """Resolve which tracker entries apply for a list/search call."""
        all_trackers = self.config.get("trackers", [])
        if tracker is not None:
            return [get_tracker_entry(self.config, tracker)]
        if audience is not None:
            matched = trackers_for_audience(self.config, audience)
            if not matched:
                raise TrackerError(
                    "not_found",
                    f"No tracker configured for audience '{audience}'.",
                )
            return matched
        return all_trackers

    def _cache_key(self, entry: dict, state: str,
                   labels: list[str], limit: int) -> tuple:
        return cache_key(
            entry.get("provider", "roadmap"),
            entry.get("repo"),
            state,
            labels,
            limit,
        )

    def _invalidate(self, entry: dict) -> None:
        """Invalidate all cache entries for (provider, repo, *)."""
        provider = entry.get("provider", "roadmap")
        repo = entry.get("repo")
        keys_to_delete = [
            k for k in self._cache if k[0] == provider and k[1] == repo
        ]
        for k in keys_to_delete:
            del self._cache[k]

    def list(self, tracker: str | None = None, audience: str | None = None,
             state: str = "open", labels: list[str] | None = None,
             limit: int = DEFAULT_LIMIT,
             issue_type: Optional[str] = None) -> list[Issue]:
        """List issues, aggregating across matching trackers with cache.

        issue_type: if set to "epic" or "issue", only issues of that type are
        returned. The "epic" type is identified by the presence of the "epic"
        label (since issue_type is not stored in the backend directly).
        """
        labels = labels or []
        target_trackers = self._trackers_for_opts(tracker, audience)
        all_issues: list[Issue] = []
        seen: set[tuple] = set()

        for entry in target_trackers:
            ck = self._cache_key(entry, state, labels, limit)
            if ck in self._cache:
                issues = self._cache[ck]
            else:
                backend = self._backend(entry)
                issues = backend.list(state=state, labels=labels, limit=limit)
                self._cache[ck] = issues

            for issue in issues:
                dedup_key = (entry.get("provider"), entry.get("repo"), issue.id)
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    all_issues.append(issue)

        # Apply issue_type filter: "epic" issues carry the "epic" label
        if issue_type is not None:
            if issue_type == "epic":
                all_issues = [i for i in all_issues if "epic" in i.labels]
            elif issue_type == "issue":
                all_issues = [i for i in all_issues if "epic" not in i.labels]

        # Sort: created_at descending (None last), then tracker name for stability
        def sort_key(issue: Issue) -> tuple:
            return (issue.created_at or "", issue.tracker)

        all_issues.sort(key=sort_key, reverse=True)
        return all_issues

    def get(self, issue_id: str, tracker: str | None = None) -> Issue:
        """Get a single issue (always includes body)."""
        if tracker is not None:
            entry = get_tracker_entry(self.config, tracker)
            return self._backend(entry).get(issue_id)
        # Try all trackers in order
        errors = []
        for entry in self.config.get("trackers", []):
            try:
                return self._backend(entry).get(issue_id)
            except TrackerError as exc:
                if exc.code == "not_found":
                    errors.append(str(exc))
                    continue
                raise
        raise TrackerError("not_found",
                           f"Issue '{issue_id}' not found in any tracker. "
                           f"Details: {'; '.join(errors)}")

    def _resolve_create_tracker(self, tracker: str | None,
                                audience: str | None) -> dict:
        """Routing: explicit name → audience match → default-for-filing."""
        if tracker is not None:
            return get_tracker_entry(self.config, tracker)
        if audience is not None:
            matched = trackers_for_audience(self.config, audience)
            if matched:
                return matched[0]
        default_name = self.config.get("default-for-filing", "default")
        return get_tracker_entry(self.config, default_name)

    def ensure_label(self, name: str, tracker: str | None = None) -> None:
        """Ensure *name* exists on the tracker; create it if absent.

        When *tracker* is None the operation is applied to the
        default-for-filing tracker (the same target routing picks for
        creates). Roadmap: no-op. GitHub: idempotent label create.
        Grimoire: raises not_implemented.
        """
        if tracker is None:
            default_name = self.config.get("default-for-filing", "default")
            entry = get_tracker_entry(self.config, default_name)
        else:
            entry = get_tracker_entry(self.config, tracker)
        self._backend(entry).ensure_label(name)

    def create(self, title: str, body: str, labels: list[str] | None = None,
               audience: str | None = None,
               tracker: str | None = None,
               issue_type: str = "issue",
               parent_epic_id: Optional[str] = None) -> Issue:
        """Create an issue; routing applies (§5.3 of design doc).

        Before filing, auto-ensures any requested labels exist on the
        provider (github: gh label create if absent; roadmap: no-op).
        This prevents a rejected-unknown-label error on GitHub.

        Epic rules (one-level nesting):
        - issue_type="epic" auto-applies the "epic" label.
        - Epics cannot be children of other Epics: setting parent_epic_id on
          an Epic raises TrackerError("validation_error", …).
        """
        if issue_type not in ("issue", "epic"):
            raise TrackerError(
                "validation_error",
                f"Invalid issue_type {issue_type!r}. Must be 'issue' or 'epic'.",
            )
        if issue_type == "epic" and parent_epic_id is not None:
            raise TrackerError(
                "validation_error",
                "Epics cannot be children of other Epics (one-level nesting rule). "
                "Remove parent_epic_id when creating an Epic.",
            )
        labels = labels or []
        # Auto-apply "epic" label for Epic issues
        if issue_type == "epic" and "epic" not in labels:
            labels = ["epic"] + labels
        resolved_audience = audience or "internal"
        entry = self._resolve_create_tracker(tracker, audience)
        backend = self._backend(entry)
        # For roadmap backend: prefix [EPIC] in the title
        effective_title = title
        if issue_type == "epic":
            backend.ensure_label("epic")
            if backend.tracker_entry.get("provider") == "roadmap":
                if not title.startswith("[EPIC]"):
                    effective_title = f"[EPIC] {title}"
        # Auto-ensure all labels exist before filing (github rejects unknown labels)
        for lb in labels:
            backend.ensure_label(lb)
        issue = backend.create(title=effective_title, body=body, labels=labels,
                               audience=resolved_audience)
        issue.issue_type = issue_type
        issue.parent_epic_id = parent_epic_id
        self._invalidate(entry)
        return issue

    def update(self, issue_id: str, tracker: str | None = None,
               title: str | None = None, body: str | None = None,
               labels: list[str] | None = None,
               state: str | None = None) -> Issue:
        """Update an issue; invalidates cache on write (after flush for github)."""
        if tracker is None:
            # Try to find which tracker owns this issue
            issue = self.get(issue_id)
            tracker = issue.tracker
        entry = get_tracker_entry(self.config, tracker)
        backend = self._backend(entry)
        result = backend.update(issue_id, title=title, body=body,
                                labels=labels, state=state)
        self._invalidate(entry)
        return result

    def close(self, issue_id: str, tracker: str | None = None) -> Issue:
        """Close an issue."""
        if tracker is None:
            issue = self.get(issue_id)
            tracker = issue.tracker
        entry = get_tracker_entry(self.config, tracker)
        backend = self._backend(entry)
        result = backend.close(issue_id)
        self._invalidate(entry)
        return result

    def label(self, issue_id: str, add: list[str] | None = None,
              remove: list[str] | None = None,
              tracker: str | None = None) -> Issue:
        """Add/remove labels on an issue.

        Auto-ensures any labels in *add* exist on the provider before
        applying them (github: gh label create if absent; roadmap: no-op).
        """
        if tracker is None:
            issue = self.get(issue_id)
            tracker = issue.tracker
        entry = get_tracker_entry(self.config, tracker)
        backend = self._backend(entry)
        # Auto-ensure labels exist before applying (github rejects unknown labels)
        for lb in (add or []):
            backend.ensure_label(lb)
        result = backend.label(issue_id, add=add, remove=remove)
        # Cache invalidation deferred to flush for github (batched writes)
        if entry.get("provider") == "roadmap":
            self._invalidate(entry)
        return result

    def comment(self, issue_id: str, body: str,
                tracker: str | None = None) -> Issue:
        """Add a comment to an issue; resolve owning tracker if unspecified."""
        if tracker is None:
            issue = self.get(issue_id)
            tracker = issue.tracker
        entry = get_tracker_entry(self.config, tracker)
        backend = self._backend(entry)
        result = backend.comment(issue_id, body)
        self._invalidate(entry)
        return result

    def search(self, query: str, tracker: str | None = None,
               audience: str | None = None,
               state: str = "open",
               limit: int = DEFAULT_LIMIT) -> list[Issue]:
        """Search issues across matching trackers."""
        target_trackers = self._trackers_for_opts(tracker, audience)
        all_issues: list[Issue] = []
        seen: set[tuple] = set()
        for entry in target_trackers:
            backend = self._backend(entry)
            issues = backend.search(query=query, state=state, limit=limit)
            for issue in issues:
                dedup_key = (entry.get("provider"), entry.get("repo"), issue.id)
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    all_issues.append(issue)
        return all_issues

    def flush(self) -> None:
        """Flush pending write batches for all github backends; invalidate cache."""
        for name, backend in self._backends.items():
            if isinstance(backend, GitHubBackend):
                entry = get_tracker_entry(self.config, name)
                backend.flush()
                self._invalidate(entry)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_issues(issues: list[Issue], as_json: bool) -> None:
    if as_json:
        print(json.dumps([i.to_dict() for i in issues], indent=2, default=str))
    else:
        if not issues:
            print("(no issues)")
            return
        for issue in issues:
            labels = f" [{', '.join(issue.labels)}]" if issue.labels else ""
            body_indicator = " (body not fetched)" if issue.body is None else ""
            print(f"#{issue.id} [{issue.state}] {issue.title}{labels}"
                  f" — {issue.tracker}{body_indicator}")


def _print_issue(issue: Issue, as_json: bool) -> None:
    if as_json:
        print(json.dumps(issue.to_dict(), indent=2, default=str))
    else:
        labels = f"Labels: {', '.join(issue.labels)}" if issue.labels else "Labels: (none)"
        print(f"ID:      {issue.id}")
        if issue.number is not None:
            print(f"Number:  {issue.number}")
        print(f"Title:   {issue.title}")
        print(f"State:   {issue.state}")
        print(f"Tracker: {issue.tracker} (audience: {issue.audience})")
        print(labels)
        if issue.url:
            print(f"URL:     {issue.url}")
        if issue.created_at:
            print(f"Created: {issue.created_at}")
        if issue.body is not None:
            print(f"\n{issue.body}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue_tracker.py",
        description=(
            "Issue-tracker abstraction for Grimoire. "
            "Operates on roadmap (default) or github backends."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to grimoire-config.json (default: auto-detect from cwd).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output results as JSON.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List issues.")
    p_list.add_argument("--tracker", help="Filter to named tracker.")
    p_list.add_argument("--audience", choices=["internal", "external"],
                        help="Filter to audience.")
    p_list.add_argument("--state", default="open",
                        choices=["open", "closed", "all"],
                        help="Issue state filter.")
    p_list.add_argument("--labels", nargs="*", default=[],
                        help="Label filter (server-side).")
    p_list.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max issues per tracker (default: {DEFAULT_LIMIT}).")
    p_list.add_argument("--issue-type", choices=["issue", "epic"],
                        dest="issue_type",
                        help="Filter by issue type: 'epic' or 'issue'.")

    # get
    p_get = sub.add_parser("get", help="Get a single issue (includes body).")
    p_get.add_argument("id", help="Issue id (slug for roadmap; number for github).")
    p_get.add_argument("--tracker", help="Restrict to named tracker.")

    # create
    p_create = sub.add_parser("create", help="Create a new issue.")
    p_create.add_argument("--title", required=True, help="Issue title.")
    p_create.add_argument("--body", default="", help="Issue body.")
    p_create.add_argument("--labels", nargs="*", default=[])
    p_create.add_argument("--audience", choices=["internal", "external"],
                          help="Audience (drives routing if no --tracker).")
    p_create.add_argument("--tracker", help="Explicit tracker name.")
    p_create.add_argument("--issue-type", choices=["issue", "epic"],
                          dest="issue_type", default="issue",
                          help="Issue type: 'issue' (default) or 'epic'.")
    p_create.add_argument("--parent-epic-id", dest="parent_epic_id",
                          default=None,
                          help="ID of parent Epic (plain issues only; "
                               "Epics cannot be children of other Epics).")

    # update
    p_update = sub.add_parser("update", help="Update an issue.")
    p_update.add_argument("id", help="Issue id.")
    p_update.add_argument("--tracker", help="Restrict to named tracker.")
    p_update.add_argument("--title")
    p_update.add_argument("--body")
    p_update.add_argument("--labels", nargs="*")
    p_update.add_argument("--state", choices=["open", "closed"])

    # close
    p_close = sub.add_parser("close", help="Close an issue.")
    p_close.add_argument("id", help="Issue id.")
    p_close.add_argument("--tracker", help="Restrict to named tracker.")

    # label
    p_label = sub.add_parser("label", help="Add/remove labels on an issue.")
    p_label.add_argument("id", help="Issue id.")
    p_label.add_argument("--add", nargs="*", default=[])
    p_label.add_argument("--remove", nargs="*", default=[])
    p_label.add_argument("--tracker", help="Restrict to named tracker.")

    # comment
    p_comment = sub.add_parser("comment", help="Add a comment to an issue.")
    p_comment.add_argument("id", help="Issue id.")
    p_comment.add_argument("--body", required=True, help="Comment body.")
    p_comment.add_argument("--tracker", help="Restrict to named tracker.")

    # search
    p_search = sub.add_parser("search", help="Search issues.")
    p_search.add_argument("query", help="Search query string.")
    p_search.add_argument("--tracker")
    p_search.add_argument("--audience", choices=["internal", "external"])
    p_search.add_argument("--state", default="open",
                          choices=["open", "closed", "all"])
    p_search.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    # flush
    sub.add_parser("flush", help="Flush pending write batches (github backend).")

    # ensure-label
    p_ensure = sub.add_parser(
        "ensure-label",
        help="Create a label if it does not already exist (github: gh label create; "
             "roadmap: no-op; grimoire: not_implemented).",
    )
    p_ensure.add_argument("name", help="Label name to ensure exists.")
    p_ensure.add_argument(
        "--tracker",
        help="Target tracker (default: default-for-filing tracker).",
    )

    return parser


# ---------------------------------------------------------------------------
# Self-test (in-memory, mocked provider calls — no real GitHub, no real files
# beyond a temp fixture)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    """Verify ensure_label per provider with mocked provider calls.

    Covers:
    - roadmap: ensure_label is a no-op (no TrackerError).
    - github: ensure_label calls gh label create; already-exists is success.
    - github: ensure_label propagates a real gh error (non-already-exists).
    - IssueTracker.ensure_label routes to the default-for-filing tracker.
    - IssueTracker.create auto-ensures labels before filing (roadmap path).
    - IssueTracker.label auto-ensures add-labels before applying (roadmap path).
    - CLI subcommand 'ensure-label' wires through correctly (roadmap fixture).
    - Grimoire provider raises not_implemented for ensure_label.
    """
    import tempfile
    import types
    import unittest.mock as mock

    cases: list[tuple[str, bool]] = []
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / ".claude").mkdir()
    (tmp / "docs").mkdir()
    (tmp / ".claude" / "grimoire-config.json").write_text(
        '{"schema-version":4,"name":"t"}'
    )
    roadmap_path = tmp / "docs" / "roadmap.md"
    roadmap_path.write_text("# R\n\n## Backlog\n\n")

    # ------------------------------------------------------------------ #
    # 1. RoadmapBackend.ensure_label — always a no-op, returns None
    # ------------------------------------------------------------------ #
    roadmap_entry = {"name": "default", "provider": "roadmap",
                     "repo": None, "audience": "internal", "labels": []}
    rb = RoadmapBackend(roadmap_entry, tmp)
    try:
        result = rb.ensure_label("any-label")
        cases.append(("roadmap ensure_label is a no-op (returns None)",
                      result is None))
    except Exception as exc:  # noqa: BLE001
        cases.append((f"roadmap ensure_label raised unexpectedly: {exc}", False))

    # ------------------------------------------------------------------ #
    # 2. GitHubBackend.ensure_label — label does NOT exist → gh label create
    # ------------------------------------------------------------------ #
    gh_entry = {"name": "gh", "provider": "github",
                "repo": "owner/repo", "audience": "internal", "labels": []}
    ghb = GitHubBackend(gh_entry, tmp)

    def _mock_run_success(args):
        """Simulate gh returning exit 0 (label created)."""
        return ""

    with mock.patch.object(ghb, "_gh_available", return_value=True), \
         mock.patch.object(ghb, "_run_gh", side_effect=_mock_run_success) as m_run:
        # We need to intercept the raw subprocess call for ensure_label since
        # it doesn't go through _run_gh — patch subprocess.run directly.
        pass

    # ensure_label bypasses _run_gh (uses subprocess.run directly) so patch
    # subprocess.run:
    fake_ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    with mock.patch("subprocess.run", return_value=fake_ok) as m_sub, \
         mock.patch.object(ghb, "_gh_available", return_value=True):
        try:
            ghb.ensure_label("Grimoire-Requirement")
            calls = m_sub.call_args_list
            # verify gh label create was called with the right args
            first_call_args = calls[0][0][0] if calls else []
            ok = (
                len(calls) == 1
                and "label" in first_call_args
                and "create" in first_call_args
                and "Grimoire-Requirement" in first_call_args
                and "--repo" in first_call_args
            )
            cases.append(("github ensure_label calls gh label create", ok))
        except Exception as exc:  # noqa: BLE001
            cases.append((f"github ensure_label raised unexpectedly: {exc}", False))

    # ------------------------------------------------------------------ #
    # 3. GitHubBackend.ensure_label — already-exists → treated as success
    # ------------------------------------------------------------------ #
    fake_exists = types.SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="Label 'Grimoire-Requirement' already exists in the 'owner/repo' repository",
    )
    with mock.patch("subprocess.run", return_value=fake_exists), \
         mock.patch.object(ghb, "_gh_available", return_value=True):
        try:
            ghb.ensure_label("Grimoire-Requirement")
            cases.append(("github ensure_label: already-exists is success", True))
        except TrackerError as exc:
            cases.append((f"github ensure_label raised on already-exists: {exc}", False))

    # ------------------------------------------------------------------ #
    # 4. GitHubBackend.ensure_label — real gh error propagates
    # ------------------------------------------------------------------ #
    fake_err = types.SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="some unexpected gh error",
    )
    with mock.patch("subprocess.run", return_value=fake_err), \
         mock.patch.object(ghb, "_gh_available", return_value=True):
        try:
            ghb.ensure_label("bad-label")
            cases.append(("github ensure_label: real error should raise", False))
        except TrackerError as exc:
            cases.append(("github ensure_label: real gh error raises TrackerError",
                          exc.code == "gh_error"))

    # ------------------------------------------------------------------ #
    # 5. IssueTracker.ensure_label routes to default-for-filing tracker
    # ------------------------------------------------------------------ #
    cfg = dict(DEFAULT_TRACKER_CONFIG)  # roadmap default
    it = IssueTracker(cfg, tmp)
    try:
        it.ensure_label("some-label")  # roadmap: no-op
        cases.append(("IssueTracker.ensure_label routes to default tracker (roadmap no-op)",
                      True))
    except Exception as exc:  # noqa: BLE001
        cases.append((f"IssueTracker.ensure_label raised: {exc}", False))

    # ------------------------------------------------------------------ #
    # 6. IssueTracker.create auto-ensures labels (roadmap: no-op ensure)
    # ------------------------------------------------------------------ #
    roadmap_path.write_text("# R\n\n## Backlog\n\n")
    it2 = IssueTracker(dict(DEFAULT_TRACKER_CONFIG), tmp)
    ensure_called = []
    orig_ensure = RoadmapBackend.ensure_label

    def _spy_ensure(self, name):
        ensure_called.append(name)
        return orig_ensure(self, name)

    with mock.patch.object(RoadmapBackend, "ensure_label", _spy_ensure):
        issue = it2.create(title="Test label ensure", body="b",
                           labels=["bug", "Grimoire-Requirement"])
    cases.append(("IssueTracker.create auto-ensures each requested label",
                  set(ensure_called) == {"bug", "Grimoire-Requirement"}))
    cases.append(("IssueTracker.create succeeds with auto-ensured labels",
                  issue.title == "Test label ensure"
                  and "Grimoire-Requirement" in issue.labels))

    # ------------------------------------------------------------------ #
    # 7. IssueTracker.label auto-ensures add-labels (roadmap: no-op ensure)
    # ------------------------------------------------------------------ #
    ensure_called2 = []

    def _spy_ensure2(self, name):
        ensure_called2.append(name)
        return orig_ensure(self, name)

    roadmap_path.write_text("# R\n\n## Backlog\n\n")
    it3 = IssueTracker(dict(DEFAULT_TRACKER_CONFIG), tmp)
    # Create an issue first
    with mock.patch.object(RoadmapBackend, "ensure_label", lambda self, n: None):
        issue3 = it3.create(title="Label test issue", body="x", labels=[])
    # Now apply label with the spy
    with mock.patch.object(RoadmapBackend, "ensure_label", _spy_ensure2):
        it3.label(issue3.id, add=["Grimoire-Requirement"])
    cases.append(("IssueTracker.label auto-ensures added labels",
                  "Grimoire-Requirement" in ensure_called2))

    # ------------------------------------------------------------------ #
    # 8. CLI 'ensure-label' subcommand wires through (roadmap fixture)
    # ------------------------------------------------------------------ #
    cfg_path = tmp / ".claude" / "grimoire-config.json"
    try:
        result = main(["--config", str(cfg_path), "ensure-label", "test-label"])
        cases.append(("CLI ensure-label exits 0 for roadmap", result == 0))
    except SystemExit as exc:
        cases.append((f"CLI ensure-label raised SystemExit: {exc}", False))

    # ------------------------------------------------------------------ #
    # 9. Grimoire provider ensure_label raises not_implemented
    # ------------------------------------------------------------------ #
    grimoire_cfg = {
        "trackers": [{"name": "g", "provider": "grimoire",
                      "repo": None, "audience": "internal", "labels": []}],
        "default-for-filing": "g",
    }
    it_g = IssueTracker(grimoire_cfg, tmp)
    try:
        it_g.ensure_label("any")
        cases.append(("grimoire ensure_label should raise not_implemented", False))
    except TrackerError as exc:
        cases.append(("grimoire ensure_label raises not_implemented",
                      exc.code == "not_implemented"))

    # ------------------------------------------------------------------ #
    # 10. Epic create — auto-applies "epic" label + [EPIC] title prefix (roadmap)
    # ------------------------------------------------------------------ #
    roadmap_path.write_text("# R\n\n## Backlog\n\n")
    it_epic = IssueTracker(dict(DEFAULT_TRACKER_CONFIG), tmp)
    try:
        epic_issue = it_epic.create(
            title="Goal: unify auth system",
            body="Overview of auth unification.",
            issue_type="epic",
        )
        cases.append(("Epic create: issue_type is 'epic'",
                      epic_issue.issue_type == "epic"))
        cases.append(("Epic create: 'epic' label auto-applied",
                      "epic" in epic_issue.labels))
        cases.append(("Epic create: [EPIC] prefix in roadmap title",
                      epic_issue.title.startswith("[EPIC]")))
        cases.append(("Epic create: parent_epic_id is None",
                      epic_issue.parent_epic_id is None))
    except Exception as exc:  # noqa: BLE001
        cases.append((f"Epic create raised unexpectedly: {exc}", False))

    # ------------------------------------------------------------------ #
    # 11. list(issue_type="epic") filters to Epic issues only
    # ------------------------------------------------------------------ #
    roadmap_path.write_text("# R\n\n## Backlog\n\n")
    it_list_epic = IssueTracker(dict(DEFAULT_TRACKER_CONFIG), tmp)
    with mock.patch.object(RoadmapBackend, "ensure_label", lambda self, n: None):
        it_list_epic.create(title="Plain issue", body="x", issue_type="issue")
        it_list_epic.create(title="Epic goal", body="y", issue_type="epic")
    # Clear cache so list re-reads
    it_list_epic._cache.clear()
    try:
        epics_only = it_list_epic.list(issue_type="epic")
        plain_only = it_list_epic.list(issue_type="issue")
        cases.append(("list(issue_type='epic') returns only Epic issues",
                      all("epic" in i.labels for i in epics_only)
                      and len(epics_only) >= 1))
        cases.append(("list(issue_type='issue') excludes Epic issues",
                      all("epic" not in i.labels for i in plain_only)
                      and len(plain_only) >= 1))
    except Exception as exc:  # noqa: BLE001
        cases.append((f"list issue_type filter raised: {exc}", False))

    # ------------------------------------------------------------------ #
    # 12. parent_epic_id validation — Epic cannot be child of Epic
    # ------------------------------------------------------------------ #
    roadmap_path.write_text("# R\n\n## Backlog\n\n")
    it_val = IssueTracker(dict(DEFAULT_TRACKER_CONFIG), tmp)
    try:
        it_val.create(
            title="Nested epic",
            body="Should fail",
            issue_type="epic",
            parent_epic_id="some-epic-id",
        )
        cases.append(("Epic with parent_epic_id should raise validation_error", False))
    except TrackerError as exc:
        cases.append(("Epic with parent_epic_id raises validation_error",
                      exc.code == "validation_error"))
    except Exception as exc:  # noqa: BLE001
        cases.append((f"Epic nesting check raised wrong exception: {exc}", False))

    # ------------------------------------------------------------------ #
    # Print results
    # ------------------------------------------------------------------ #
    passed = failed = 0
    for label, ok in cases:
        tag = "PASS" if ok else "FAIL"
        print(f"  {tag}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    print(f"\nissue-tracker self-test: {passed} passed, {failed} failed.")
    return 0 if failed == 0 else 1


def main(argv=None) -> int:
    # Intercept --self-test before argparse (it is not a subcommand)
    effective_argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in effective_argv:
        return _self_test()

    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve config
    if args.config:
        config_path = pathlib.Path(args.config).resolve()
        repo_root = config_path.parent.parent  # .claude/grimoire-config.json → repo root
    else:
        repo_root = find_repo_root()
        config_path = None

    try:
        config = load_config(config_path)
        tracker = IssueTracker(config, repo_root)

        if args.command == "list":
            issues = tracker.list(
                tracker=args.tracker,
                audience=args.audience,
                state=args.state,
                labels=args.labels,
                limit=args.limit,
                issue_type=getattr(args, "issue_type", None),
            )
            _print_issues(issues, args.as_json)

        elif args.command == "get":
            issue = tracker.get(args.id, tracker=args.tracker)
            _print_issue(issue, args.as_json)

        elif args.command == "create":
            issue = tracker.create(
                title=args.title,
                body=args.body,
                labels=args.labels,
                audience=args.audience,
                tracker=args.tracker,
                issue_type=getattr(args, "issue_type", "issue"),
                parent_epic_id=getattr(args, "parent_epic_id", None),
            )
            print(f"Created issue #{issue.id} in tracker '{issue.tracker}'")
            if issue.url:
                print(f"URL: {issue.url}")
            _print_issue(issue, args.as_json)

        elif args.command == "update":
            issue = tracker.update(
                args.id,
                tracker=args.tracker,
                title=args.title,
                body=args.body,
                labels=args.labels,
                state=args.state,
            )
            print(f"Updated issue #{issue.id}")
            _print_issue(issue, args.as_json)

        elif args.command == "close":
            issue = tracker.close(args.id, tracker=args.tracker)
            print(f"Closed issue #{issue.id}")

        elif args.command == "label":
            issue = tracker.label(
                args.id,
                add=args.add,
                remove=args.remove,
                tracker=args.tracker,
            )
            print(f"Updated labels on issue #{issue.id}")

        elif args.command == "comment":
            issue = tracker.comment(args.id, body=args.body, tracker=args.tracker)
            print(f"Commented on issue #{issue.id}")

        elif args.command == "search":
            issues = tracker.search(
                query=args.query,
                tracker=args.tracker,
                audience=args.audience,
                state=args.state,
                limit=args.limit,
            )
            _print_issues(issues, args.as_json)

        elif args.command == "flush":
            tracker.flush()
            print("Write batch flushed.")

        elif args.command == "ensure-label":
            tracker.ensure_label(args.name, tracker=args.tracker)
            print(f"Label '{args.name}' ensured on tracker "
                  f"'{args.tracker or tracker.config.get('default-for-filing', 'default')}'.")

    except TrackerError as exc:
        error_obj = exc.to_dict()
        print(json.dumps(error_obj), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
