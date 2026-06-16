#!/usr/bin/env python3
"""server.py — Grimoire grimoire-status MCP server (read-only project overview).

Exposes the status-broker script
(.claude/skills/status-broker/project_status.py) as a single, token-cheap
read-only MCP tool, built on the reusable stdlib runtime
(.claude/mcp-servers/lib/mcp_runtime.py). No third-party dependencies (#75:
Python 3 stdlib only).

Read-only contract: this server NEVER writes files, runs git mutations, or
calls any issue-tracker API. It parses structured sources (grimoire-config.json,
version-history.md, roadmap.md, feature-manifest.md, package manifests) and
emits a structured JSON overview. Design: docs/design/status-broker-design.md +
docs/design/mcp-server-design.md.

Registered by the project-root .mcp.json as `grimoire-status`:
    { "command": "python3",
      "args": [".claude/mcp-servers/grimoire-status/server.py"] }

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
    """Put the runtime lib + the status-broker engine dir on sys.path (single source).

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
    status_candidates = [
        repo_root / ".claude" / "skills" / "status-broker",
        repo_root / "scripts",
    ]
    for candidates in (lib_candidates, status_candidates):
        for cand in candidates:
            if cand.exists():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break


REPO_ROOT = _find_repo_root()
_bootstrap_imports(REPO_ROOT)

from mcp_runtime import McpServer  # noqa: E402  (path set above)
import project_status as ps  # noqa: E402


class StatusServer(McpServer):
    """McpServer subclass wrapping project_status.py as a single read-only tool."""

    def __init__(self, repo_root: pathlib.Path | None = None):
        super().__init__("grimoire-status", "1.0.0")
        self.repo_root = str(repo_root or REPO_ROOT)
        self._register()

    def _register(self) -> None:
        # Schema is intentionally lean (only essential params) to keep the
        # recurring tools/list payload small.
        self.register_tool(
            "get_status",
            "Return a structured JSON project overview: name, framework version, "
            "paradigm, dials, latest and in-flight release, feature-manifest version, "
            "tech stack, and degraded-source warnings. Read-only — no writes.",
            {"type": "object", "properties": {
                "root": {"type": "string",
                         "description": "Project root directory (default: server root)"}}},
            self._get_status)

    # -- handler --

    def _get_status(self, a: dict):
        root = a.get("root") or self.repo_root
        return ps.build_status(root)


# ---------------------------------------------------------------------------
# Self-test (fixture project in a temp dir; no git, no network)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    import json
    import os
    import tempfile

    root = pathlib.Path(tempfile.mkdtemp())
    (root / ".claude").mkdir()
    (root / ".claude" / "skills" / "sync-from-upstream").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / ".claude" / "grimoire-config.json").write_text(
        json.dumps({
            "schema-version": 4,
            "name": "TestProject",
            "framework-version": "v3.28",
            "work-paradigm": {"value": "Noir"},
            "stealth-mode": {"value": "off"},
        }))
    (root / "docs" / "version-history.md").write_text(
        "# Version History\n\n## v3.28 — Ops surface\n\nbody\n\n"
        "## v3.27 — Release mechanics\n\nbody\n")
    (root / "docs" / "roadmap.md").write_text(
        "# Roadmap\n\n## v3.29 — Future\n\nplanned\n\n"
        "## v3.28 — Ops surface\n\nShipped — see version-history.md.\n")
    (root / ".claude" / "skills" / "sync-from-upstream" / "feature-manifest.md").write_text(
        "manifest-version: 36\n\n# Feature manifest\n")

    srv = StatusServer(repo_root=root)

    def call(name, args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        res = r["result"]
        text = res["content"][0]["text"]
        parsed = json.loads(text) if text[:1] in "[{" else text
        return res, parsed

    # tools/list advertises exactly 1 tool.
    tl = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
    names = [t["name"] for t in tl["result"]["tools"]]
    assert names == ["get_status"], names

    # get_status with explicit root.
    res, out = call("get_status", {"root": str(root)})
    assert "isError" not in res, res
    assert out["project"] == "TestProject", out
    assert out["framework_version"] == "v3.28", out
    assert out["paradigm"] == "Noir", out
    assert out["latest_release"]["version"] == "v3.28", out
    assert out["feature_manifest_version"] == 36, out
    assert isinstance(out["degraded"], list), out

    # get_status with no root falls back to server root (may degrade gracefully).
    res, out2 = call("get_status", {})
    assert "isError" not in res, res
    assert "degraded" in out2, out2

    # determinism: two calls with the same root produce identical JSON.
    _, out3 = call("get_status", {"root": str(root)})
    assert (json.dumps(out, sort_keys=True) ==
            json.dumps(out3, sort_keys=True)), "non-deterministic output"

    # error path: non-existent root returns degraded, not an exception.
    res, out4 = call("get_status", {"root": os.path.join(str(root), "no_such_dir")})
    assert "isError" not in res, res
    assert out4["degraded"], "expected degraded flags for missing root"

    print("grimoire-status server self-test: OK")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    return StatusServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
