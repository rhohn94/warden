#!/usr/bin/env python3
"""server.py — Grimoire grimoire-release MCP server (second McpServer instance).

Exposes the release-planning ledger engine
(.claude/skills/release-agent-tracker/release_plan.py) and the noir-loop state
helper (.claude/skills/noir-loop/noir_loop_state.py) as a small, token-cheap MCP
tool surface, built on the reusable stdlib runtime
(.claude/mcp-servers/lib/mcp_runtime.py). No third-party dependencies (#75:
Python 3 stdlib only).

File-write-only contract: this server NEVER runs git mutations. It parses,
computes, and edits the §5 ledger file only when asked via `tick_rows` (and the
loop-state file via `advance_loop`); the AGENT commits. Design:
docs/design/grimoire-release-server-design.md.

Registered by the project-root .mcp.json as `grimoire-release`:
    { "command": "python3",
      "args": [".claude/mcp-servers/grimoire-release/server.py"] }

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
    """Put the runtime lib + both engine dirs on sys.path (single source).

    Layout-agnostic across both Grimoire flavors: claude-code keeps the runtime
    under ``.claude/mcp-servers/lib`` and the engines under ``.claude/skills``;
    the copilot flavor keeps them at ``mcp-servers/lib`` and ``scripts``. The
    first existing candidate in each group wins, so a single byte-identical
    server.py runs in either layout.
    """
    lib_candidates = [
        repo_root / ".claude" / "mcp-servers" / "lib",
        repo_root / "mcp-servers" / "lib",
    ]
    plan_candidates = [
        repo_root / ".claude" / "skills" / "release-agent-tracker",
        repo_root / "scripts",
    ]
    loop_candidates = [
        repo_root / ".claude" / "skills" / "noir-loop",
        repo_root / "scripts",
    ]
    for candidates in (lib_candidates, plan_candidates, loop_candidates):
        for cand in candidates:
            if cand.exists():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break


REPO_ROOT = _find_repo_root()
_bootstrap_imports(REPO_ROOT)

from mcp_runtime import McpServer  # noqa: E402  (path set above)
import release_plan as rp  # noqa: E402
import noir_loop_state as nls  # noqa: E402


class ReleaseServer(McpServer):
    """McpServer subclass wrapping release_plan.py + noir_loop_state.py as 7 tools."""

    def __init__(self, repo_root: pathlib.Path | None = None):
        super().__init__("grimoire-release", "1.0.0")
        self.repo_root = str(repo_root or REPO_ROOT)
        self._register()

    def _engine(self, plan):
        # Resolve a fresh engine per call (the ledger file may have just been
        # ticked); `plan` overrides the located active plan (fixture/test hook).
        return rp.ReleasePlanEngine(plan=plan, root=self.repo_root)

    def _register(self) -> None:
        # Schemas are intentionally lean (only essential params) to keep the
        # recurring tools/list payload small.
        self.register_tool(
            "get_ledger",
            "Parse the §5 ledger of the active plan to JSON (passes, rows, "
            "checkbox tri-state, branches, item ids).",
            {"type": "object", "properties": {
                "plan": {"type": "string"}}},
            self._get_ledger)
        self.register_tool(
            "tick_rows",
            "Atomically + idempotently flip §5 checkbox cells. File edit only — "
            "the agent commits. Columns: design_doc|implemented|reviewed|merged.",
            {"type": "object", "properties": {
                "ticks": {"type": "array", "items": {"type": "object",
                          "properties": {
                              "branch": {"type": "string"},
                              "column": {"type": "string"},
                              "value": {"type": "boolean"}},
                          "required": ["branch", "column", "value"]}},
                "plan": {"type": "string"}},
             "required": ["ticks"]},
            self._tick_rows)
        self.register_tool(
            "merge_queue",
            "Compute the merge order for ready rows (☑ implemented, ☐ merged), "
            "toposorted by §3's conflict map.",
            {"type": "object", "properties": {
                "phase": {"type": "string"},
                "plan": {"type": "string"}}},
            self._merge_queue)
        self.register_tool(
            "merge_preflight",
            "Structured verdict {head_ok, branches[], blocked[]}: HEAD==staging "
            "+ per-branch exists/commits-ahead. Read-only — never merges.",
            {"type": "object", "properties": {
                "staging": {"type": "string"},
                "branches": {"type": "array", "items": {"type": "string"}},
                "plan": {"type": "string"}},
             "required": ["staging"]},
            self._merge_preflight)
        self.register_tool(
            "plan_phase",
            "First all-unticked pass -> {phase, batches, model_assignments}.",
            {"type": "object", "properties": {
                "plan": {"type": "string"}}},
            self._plan_phase)
        self.register_tool(
            "read_loop_state",
            "Read the Noir iterative-release loop state (noir-loop-state.json).",
            {"type": "object", "properties": {
                "root": {"type": "string"}}},
            self._read_loop_state)
        self.register_tool(
            "advance_loop",
            "Advance the Noir loop state: bump iteration, set summary, replace "
            "open-work/next-step lists. File edit only — the agent commits.",
            {"type": "object", "properties": {
                "summary": {"type": "string"},
                "open": {"type": "array", "items": {"type": "string"}},
                "next": {"type": "array", "items": {"type": "string"}},
                "root": {"type": "string"}},
             "required": ["summary"]},
            self._advance_loop)

    # -- handlers (raise PlanError/StateError → runtime maps to a tool isError) --

    def _get_ledger(self, a: dict):
        return self._engine(a.get("plan")).get_ledger()

    def _tick_rows(self, a: dict):
        ticks = [(t["branch"], t["column"], bool(t["value"]))
                 for t in a.get("ticks", [])]
        return self._engine(a.get("plan")).tick(ticks)

    def _merge_queue(self, a: dict):
        return self._engine(a.get("plan")).merge_queue(a.get("phase"))

    def _merge_preflight(self, a: dict):
        return self._engine(a.get("plan")).merge_preflight(
            a["staging"], a.get("branches"))

    def _plan_phase(self, a: dict):
        return self._engine(a.get("plan")).plan_phase()

    def _read_loop_state(self, a: dict):
        return nls.cmd_read(a.get("root") or self.repo_root).as_dict()

    def _advance_loop(self, a: dict):
        state = nls.cmd_advance(
            a.get("root") or self.repo_root, a["summary"],
            a.get("open"), a.get("next"))
        return state.as_dict()


# ---------------------------------------------------------------------------
# Self-test (fixture plan + temp loop state; no git, no network)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    import json
    import os
    import tempfile

    root = pathlib.Path(tempfile.mkdtemp())
    (root / ".claude").mkdir()
    (root / "docs").mkdir()
    (root / ".claude" / "grimoire-config.json").write_text(
        '{"schema-version":4,"name":"t"}')
    plan = root / "docs" / "release-planning-v9.9.md"
    plan.write_text(rp._fixture_plan())

    srv = ReleaseServer(repo_root=root)

    def call(name, args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        res = r["result"]
        text = res["content"][0]["text"]
        parsed = json.loads(text) if text[:1] in "[{" else text
        return res, parsed

    # tools/list advertises exactly the 7 tools, in order.
    tl = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
    names = [t["name"] for t in tl["result"]["tools"]]
    assert names == ["get_ledger", "tick_rows", "merge_queue", "merge_preflight",
                     "plan_phase", "read_loop_state", "advance_loop"], names

    # get_ledger.
    res, out = call("get_ledger", {"plan": str(plan)})
    assert "isError" not in res, res
    assert out["version"] == "9.9", out
    assert out["passes"]["Pass 1"][0]["branch"] == "alpha-v99", out

    # merge_queue (alpha+beta ready, independent).
    res, out = call("merge_queue", {"plan": str(plan)})
    assert out["order"] == ["alpha-v99", "beta-v99"], out

    # merge_preflight returns a structured verdict (real git unavailable in a
    # bare temp dir → head/branches degrade, but the shape must be intact).
    res, out = call("merge_preflight",
                    {"plan": str(plan), "staging": "version/9.9",
                     "branches": ["alpha-v99"]})
    assert set(out) == {"head_ok", "head", "staging", "branches", "blocked"}, out

    # plan_phase.
    res, out = call("plan_phase", {"plan": str(plan)})
    assert out["phase"] in ("Pass 1", "Pass 2"), out
    assert isinstance(out["batches"], list), out

    # tick_rows is idempotent + edits the file (agent commits).
    res, out = call("tick_rows", {"plan": str(plan),
                    "ticks": [{"branch": "alpha-v99", "column": "merged",
                               "value": True}]})
    assert out["ok"] is True and out["changed"], out
    res, out = call("tick_rows", {"plan": str(plan),
                    "ticks": [{"branch": "alpha-v99", "column": "merged",
                               "value": True}]})
    assert out["changed"] == [], out  # idempotent re-tick

    # read_loop_state on a fresh root (no file) returns the near-empty fresh shape.
    res, out = call("read_loop_state", {"root": str(root)})
    assert out["iteration"] == 0 and out["last_summary"] == "", out

    # advance_loop writes the state file (agent commits) and bumps iteration.
    res, out = call("advance_loop", {"root": str(root), "summary": "did a thing",
                    "open": ["follow-up"], "next": ["ship"]})
    assert out["iteration"] == 1 and out["last_summary"] == "did a thing", out
    assert os.path.exists(root / ".claude" / "cache" / "noir-loop-state.json")

    # error path → structured isError result, not an RPC error (bad column).
    res, out = call("tick_rows", {"plan": str(plan),
                    "ticks": [{"branch": "alpha-v99", "column": "bogus",
                               "value": True}]})
    assert res.get("isError") is True, res

    print("grimoire-release server self-test: OK")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    return ReleaseServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
