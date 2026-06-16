#!/usr/bin/env python3
"""server.py — Grimoire grimoire-environment MCP server (read-only environment inspection).

Exposes three read-only tools wrapping env_probe.py
(.claude/skills/environment-manager/env_probe.py) as a token-cheap MCP surface,
built on the reusable stdlib runtime (.claude/mcp-servers/lib/mcp_runtime.py).
No third-party dependencies (#75: Python 3 stdlib only).

Lifecycle operations (kill, start) are deliberately EXCLUDED from this server.
Per docs/design/environment-manager-design.md §3, lifecycle actions require
per-action authorization — that responsibility stays agent-side where the
authorization gate is enforced. This server is read-only: it inspects, never
mutates.

Tools exposed:
  list_processes  — list all TCP listeners (lsof/ss, structured JSON)
  port_status     — query specific port(s) for occupying processes
  instance_urls   — find processes by name (command/args substring match)

Registered by the project-root .mcp.json as `grimoire-environment`:
    { "command": "python3",
      "args": [".claude/mcp-servers/grimoire-environment/server.py"] }

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
    """Put the runtime lib + the env_probe engine dir on sys.path (single source).

    Layout-agnostic across both Grimoire flavors: claude-code keeps the runtime
    under ``.claude/mcp-servers/lib`` and the engine under ``.claude/skills``;
    the copilot flavor keeps them at ``mcp-servers/lib`` and ``scripts``. The
    first existing candidate in each group wins, so a single byte-identical
    server.py runs in either layout.
    """
    lib_candidates = [
        repo_root / ".claude" / "mcp-servers" / "lib",
        repo_root / "mcp-servers" / "lib",
    ]
    probe_candidates = [
        repo_root / ".claude" / "skills" / "environment-manager",
        repo_root / "scripts",
    ]
    for candidates in (lib_candidates, probe_candidates):
        for cand in candidates:
            if cand.exists():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break


REPO_ROOT = _find_repo_root()
_bootstrap_imports(REPO_ROOT)

from mcp_runtime import McpServer  # noqa: E402  (path set above)
import env_probe as ep  # noqa: E402


class EnvironmentServer(McpServer):
    """McpServer subclass wrapping env_probe.py as 3 read-only tools."""

    def __init__(self, repo_root: pathlib.Path | None = None):
        super().__init__("grimoire-environment", "1.0.0")
        self.repo_root = str(repo_root or REPO_ROOT)
        self._register()

    def _register(self) -> None:
        # Schemas are intentionally lean (only essential params) to keep the
        # recurring tools/list payload small.
        self.register_tool(
            "list_processes",
            "List all TCP listeners and their owning processes (lsof/ss → "
            "structured JSON: command, pid, user, address, port, state). "
            "Read-only — never kills or starts anything.",
            {"type": "object", "properties": {}},
            self._list_processes)
        self.register_tool(
            "port_status",
            "Query specific port(s) for occupying processes. Returns the "
            "listener rows for the requested ports (empty list if all free). "
            "Read-only.",
            {"type": "object", "properties": {
                "ports": {"type": "array", "items": {"type": "integer"},
                          "description": "Port numbers to query (e.g. [3000, 8080])."}},
             "required": ["ports"]},
            self._port_status)
        self.register_tool(
            "instance_urls",
            "Find running processes whose command or args match a name substring "
            "(ps-based). Use to answer 'is <app> running?' and surface its PID. "
            "Read-only.",
            {"type": "object", "properties": {
                "name": {"type": "string",
                         "description": "Substring to match against command/args (e.g. 'node', 'python')."}},
             "required": ["name"]},
            self._instance_urls)

    # -- handlers ------------------------------------------------------------

    def _list_processes(self, a: dict):
        return ep.list_listeners()

    def _port_status(self, a: dict):
        ports = [int(p) for p in a.get("ports", [])]
        if not ports:
            raise ValueError("ports must be a non-empty list of integers")
        return ep.for_ports(ports)

    def _instance_urls(self, a: dict):
        name = a.get("name", "").strip()
        if not name:
            raise ValueError("name must be a non-empty string")
        return ep.by_name(name)


# ---------------------------------------------------------------------------
# Self-test (fixture-based; no git, no network, no live process calls)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    import json

    root = pathlib.Path(__file__).resolve().parents[3]  # repo root from server.py location
    srv = EnvironmentServer(repo_root=root)

    def call(name, args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        res = r["result"]
        text = res["content"][0]["text"]
        parsed = json.loads(text) if text[:1] in "[{" else text
        return res, parsed

    # tools/list advertises exactly the 3 tools, in order.
    tl = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
    names = [t["name"] for t in tl["result"]["tools"]]
    assert names == ["list_processes", "port_status", "instance_urls"], names

    # list_processes returns the standard shape (tool, listeners, degraded).
    res, out = call("list_processes", {})
    assert "isError" not in res, res
    assert "listeners" in out, out
    assert "degraded" in out, out

    # port_status with a port that is almost certainly free returns empty listeners.
    res, out = call("port_status", {"ports": [19999]})
    assert "isError" not in res, res
    assert "listeners" in out, out
    assert "queried_ports" in out, out
    assert out["queried_ports"] == [19999], out

    # port_status with empty ports → tool error (isError).
    res, out = call("port_status", {"ports": []})
    assert res.get("isError") is True, res

    # instance_urls with a name returns the standard shape (tool, matches, degraded).
    res, out = call("instance_urls", {"name": "python3"})
    assert "isError" not in res, res
    assert "matches" in out, out
    assert "degraded" in out, out

    # instance_urls with empty name → tool error (isError).
    res, out = call("instance_urls", {"name": ""})
    assert res.get("isError") is True, res

    # initialize advertises server name.
    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
    assert r["result"]["serverInfo"]["name"] == "grimoire-environment", r

    print("grimoire-environment server self-test: OK")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    return EnvironmentServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
