#!/usr/bin/env python3
"""server.py — Grimoire issue-tracker MCP server (first McpServer instance).

Exposes the issue-tracker engine
(.claude/skills/grm-issue-tracker/issue_tracker.py) as a small, token-cheap MCP
tool surface, built on the reusable stdlib runtime
(.claude/mcp-servers/lib/mcp_runtime.py). No third-party dependencies (#75:
Python 3 stdlib only). The engine keeps all backend/routing/cache/cost logic;
this server is a thin adapter that advertises native tools and serializes
compact, body-on-demand results.

Registered by the project-root .mcp.json as `grimoire-issue-tracker`:
    { "command": "python3",
      "args": [".claude/mcp-servers/issue-tracker/server.py"] }

The same shape registers on other harnesses (Cursor `.cursor/mcp.json`,
VS Code / Copilot `.vscode/mcp.json`); only the file location differs.

CLI:  python3 server.py              # run the stdio MCP server
      python3 server.py --self-test
"""

from __future__ import annotations

import pathlib
import sys

CONFIG_REL = ".claude/grimoire-config.json"


def _find_repo_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """Walk up from this file (or start) to the repo root holding the config."""
    current = (start or pathlib.Path(__file__)).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_REL).exists():
            return candidate
    return pathlib.Path.cwd().resolve()


def _bootstrap_imports(repo_root: pathlib.Path) -> None:
    """Put the runtime lib + the tracker engine on sys.path (single source).

    Layout-agnostic across both Grimoire flavors: claude-code keeps the runtime
    under ``.claude/mcp-servers/lib`` and the engine under
    ``.claude/skills/issue-tracker``; the copilot flavor keeps them at
    ``mcp-servers/lib`` and ``scripts``. The first existing candidate wins, so a
    single byte-identical server.py runs in either layout.
    """
    lib_candidates = [
        repo_root / ".claude" / "mcp-servers" / "lib",
        repo_root / "mcp-servers" / "lib",
    ]
    engine_candidates = [
        repo_root / ".claude" / "skills" / "issue-tracker",
        repo_root / "scripts",
    ]
    for candidates in (lib_candidates, engine_candidates):
        for cand in candidates:
            if cand.exists():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break


REPO_ROOT = _find_repo_root()
_bootstrap_imports(REPO_ROOT)

from mcp_runtime import McpServer  # noqa: E402  (path set above)
import issue_tracker as it  # noqa: E402


def _compact(issue, include_body: bool = False) -> dict:
    """Compact, null-omitting projection of an Issue (token-cheap responses)."""
    out = {"id": issue.id, "state": issue.state, "title": issue.title}
    if issue.labels:
        out["labels"] = issue.labels
    if issue.url:
        out["url"] = issue.url
    if issue.tracker:
        out["tracker"] = issue.tracker
    if include_body and issue.body is not None:
        out["body"] = issue.body
    return out


class IssueTrackerServer(McpServer):
    """McpServer subclass wrapping the IssueTracker engine as 9 MCP tools."""

    def __init__(self, config: dict | None = None,
                 repo_root: pathlib.Path | None = None):
        super().__init__("grimoire-issue-tracker", "1.0.0")
        self.repo_root = repo_root or REPO_ROOT
        cfg = config if config is not None else it.load_config(
            self.repo_root / CONFIG_REL)
        self.tracker = it.IssueTracker(cfg, self.repo_root)
        self._register()

    def _register(self) -> None:
        # Schemas are intentionally lean (only essential params) to keep the
        # recurring tools/list payload small.
        self.register_tool(
            "list_issues",
            "List issues (open by default); compact, no body.",
            {"type": "object", "properties": {
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "labels": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
                "audience": {"type": "string", "enum": ["internal", "external"]},
                "tracker": {"type": "string"}}},
            self._list)
        self.register_tool(
            "get_issue",
            "Get one issue including its body.",
            {"type": "object", "properties": {
                "id": {"type": "string"}, "tracker": {"type": "string"}},
             "required": ["id"]},
            self._get)
        self.register_tool(
            "search_issues",
            "Search issues by query; compact, no body.",
            {"type": "object", "properties": {
                "query": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer"},
                "audience": {"type": "string", "enum": ["internal", "external"]},
                "tracker": {"type": "string"}},
             "required": ["query"]},
            self._search)
        self.register_tool(
            "create_issue",
            "Create an issue; routes by audience when no tracker is given.",
            {"type": "object", "properties": {
                "title": {"type": "string"}, "body": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "audience": {"type": "string", "enum": ["internal", "external"]},
                "tracker": {"type": "string"}},
             "required": ["title"]},
            self._create)
        self.register_tool(
            "comment_issue",
            "Add a comment to an issue.",
            {"type": "object", "properties": {
                "id": {"type": "string"}, "body": {"type": "string"},
                "tracker": {"type": "string"}},
             "required": ["id", "body"]},
            self._comment)
        self.register_tool(
            "update_issue",
            "Update an issue's title/body/labels/state.",
            {"type": "object", "properties": {
                "id": {"type": "string"}, "title": {"type": "string"},
                "body": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "state": {"type": "string", "enum": ["open", "closed"]},
                "tracker": {"type": "string"}},
             "required": ["id"]},
            self._update)
        self.register_tool(
            "close_issue",
            "Close an issue.",
            {"type": "object", "properties": {
                "id": {"type": "string"}, "tracker": {"type": "string"}},
             "required": ["id"]},
            self._close)
        self.register_tool(
            "label_issue",
            "Add and/or remove labels on an issue. "
            "Auto-ensures added labels exist (github: creates if absent; roadmap: no-op).",
            {"type": "object", "properties": {
                "id": {"type": "string"},
                "add": {"type": "array", "items": {"type": "string"}},
                "remove": {"type": "array", "items": {"type": "string"}},
                "tracker": {"type": "string"}},
             "required": ["id"]},
            self._label)
        self.register_tool(
            "ensure_label",
            "Create a label if it does not already exist "
            "(github: gh label create, idempotent; roadmap: no-op; grimoire: not_implemented). "
            "Also called automatically by create_issue and label_issue.",
            {"type": "object", "properties": {
                "name": {"type": "string"},
                "tracker": {"type": "string"}},
             "required": ["name"]},
            self._ensure_label)

    # -- handlers (raise TrackerError → runtime maps to a tool isError) ----

    def _list(self, a: dict):
        issues = self.tracker.list(
            tracker=a.get("tracker"), audience=a.get("audience"),
            state=a.get("state", "open"), labels=a.get("labels") or [],
            limit=int(a.get("limit", it.DEFAULT_LIMIT)))
        return [_compact(i) for i in issues]

    def _get(self, a: dict):
        return _compact(self.tracker.get(a["id"], tracker=a.get("tracker")),
                        include_body=True)

    def _search(self, a: dict):
        issues = self.tracker.search(
            query=a["query"], tracker=a.get("tracker"),
            audience=a.get("audience"), state=a.get("state", "open"),
            limit=int(a.get("limit", it.DEFAULT_LIMIT)))
        return [_compact(i) for i in issues]

    def _create(self, a: dict):
        issue = self.tracker.create(
            title=a["title"], body=a.get("body", ""),
            labels=a.get("labels") or [], audience=a.get("audience"),
            tracker=a.get("tracker"))
        self.tracker.flush()
        return _compact(issue)

    def _comment(self, a: dict):
        issue = self.tracker.comment(a["id"], a["body"], tracker=a.get("tracker"))
        self.tracker.flush()
        return {"ok": True, "id": issue.id}

    def _update(self, a: dict):
        issue = self.tracker.update(
            a["id"], tracker=a.get("tracker"), title=a.get("title"),
            body=a.get("body"), labels=a.get("labels"), state=a.get("state"))
        self.tracker.flush()
        return _compact(issue)

    def _close(self, a: dict):
        issue = self.tracker.close(a["id"], tracker=a.get("tracker"))
        self.tracker.flush()
        return {"ok": True, "id": issue.id, "state": issue.state}

    def _label(self, a: dict):
        issue = self.tracker.label(
            a["id"], add=a.get("add") or [], remove=a.get("remove") or [],
            tracker=a.get("tracker"))
        self.tracker.flush()
        return _compact(issue)

    def _ensure_label(self, a: dict):
        self.tracker.ensure_label(a["name"], tracker=a.get("tracker"))
        return {"ok": True, "name": a["name"]}


# ---------------------------------------------------------------------------
# Self-test (roadmap fixture; no network, no gh)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    import json
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / ".claude").mkdir()
    (tmp / "docs").mkdir()
    (tmp / ".claude" / "grimoire-config.json").write_text(
        '{"schema-version":4,"name":"t"}')
    (tmp / "docs" / "roadmap.md").write_text("# R\n\n## Backlog\n\n## Closed\n")

    srv = IssueTrackerServer(config=dict(it.DEFAULT_TRACKER_CONFIG), repo_root=tmp)

    def call(name, args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        res = r["result"]
        text = res["content"][0]["text"]
        parsed = json.loads(text) if text[:1] in "[{" else text
        return res, parsed

    # tools/list advertises exactly the 9 tools, in order
    tl = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
    names = [t["name"] for t in tl["result"]["tools"]]
    assert names == ["list_issues", "get_issue", "search_issues", "create_issue",
                     "comment_issue", "update_issue", "close_issue",
                     "label_issue", "ensure_label"], names

    # create
    res, out = call("create_issue",
                    {"title": "Fix the cache", "body": "it leaks",
                     "labels": ["bug"]})
    assert "isError" not in res, res
    assert out["title"] == "Fix the cache" and out["labels"] == ["bug"], out
    iid = out["id"]

    # list omits body
    res, out = call("list_issues", {})
    assert any(i["id"] == iid for i in out), out
    assert all("body" not in i for i in out), out

    # get includes body
    res, out = call("get_issue", {"id": iid})
    assert out["body"] == "it leaks", out

    # comment appends to body
    res, out = call("comment_issue", {"id": iid, "body": "reviewed"})
    assert out["ok"] is True, out
    res, out = call("get_issue", {"id": iid})
    assert "[comment] reviewed" in out["body"], out

    # search finds by keyword
    res, out = call("search_issues", {"query": "cache"})
    assert any(i["id"] == iid for i in out), out

    # label add preserves body
    call("label_issue", {"id": iid, "add": ["ux"]})
    res, out = call("get_issue", {"id": iid})
    assert set(out["labels"]) == {"bug", "ux"}, out
    assert "[comment] reviewed" in out["body"], out

    # close moves it out of open list
    res, out = call("close_issue", {"id": iid})
    assert out["state"] == "closed", out
    res, out = call("list_issues", {})
    assert not any(i["id"] == iid for i in out), out

    # ensure_label (roadmap: no-op, returns ok)
    res, out = call("ensure_label", {"name": "Grimoire-Requirement"})
    assert "isError" not in res, res
    assert isinstance(out, dict) and out.get("ok") is True, out
    assert out.get("name") == "Grimoire-Requirement", out

    # error path → structured isError result, not an RPC error
    res, out = call("get_issue", {"id": "does-not-exist"})
    assert res.get("isError") is True, res
    assert isinstance(out, dict) and out.get("code") == "not_found", out

    print("issue-tracker server self-test: OK")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    return IssueTrackerServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
