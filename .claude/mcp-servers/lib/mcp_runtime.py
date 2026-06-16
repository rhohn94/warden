#!/usr/bin/env python3
"""mcp_runtime.py — reusable stdlib MCP server runtime (the Grimoire template).

Implements the Model Context Protocol stdio transport (newline-delimited
JSON-RPC 2.0) as a small, dependency-free base class, McpServer. A concrete
Grimoire MCP server instantiates or subclasses McpServer, registers a handful
of tools, and calls serve(). No third-party dependencies (#75: Python 3 stdlib
only) — this module is the conformance template every future Grimoire MCP
server reuses, so the protocol, framing, error handling, and self-test harness
are written once here and inherited.

Protocol surface implemented (server role):
  initialize, notifications/initialized, tools/list, tools/call, ping.

Transport: one UTF-8 JSON object per line on stdio (no embedded newlines) —
the MCP stdio framing every local agentic harness (Claude Code, Cursor,
Copilot, Windsurf, Codex) speaks.

CLI:  python3 mcp_runtime.py --self-test
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

# Default MCP protocol version advertised when the client's requested version is
# absent/unknown. The server ECHOES the client's requested version when present
# (maximizing cross-harness compatibility without tracking every spec bump in
# code); this constant is only the fallback.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Tool:
    """A registered MCP tool: name, one-line description, schema, handler."""

    def __init__(self, name: str, description: str, input_schema: dict,
                 handler: Callable[[dict], Any]):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler

    def spec(self) -> dict:
        """The tools/list advertisement for this tool (kept lean)."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class McpServer:
    """Minimal, dependency-free MCP server over stdio JSON-RPC.

    Instantiate or subclass, register tools, call serve(). Subclasses that need
    extra protocol methods may override handle() and delegate to super().
    """

    def __init__(self, name: str, version: str,
                 default_protocol: str = DEFAULT_PROTOCOL_VERSION):
        self.name = name
        self.version = version
        self.default_protocol = default_protocol
        self._tools: dict[str, Tool] = {}

    # -- registration -----------------------------------------------------

    def register_tool(self, name: str, description: str, input_schema: dict,
                      handler: Callable[[dict], Any]) -> None:
        """Register a tool. handler(arguments: dict) -> str | dict | list.

        The return value is serialized into a single text content block; a
        handler exception becomes an MCP tool error (isError) rather than a
        JSON-RPC protocol error, so the agent receives the message and can
        recover.
        """
        self._tools[name] = Tool(name, description, input_schema, handler)

    # -- response helpers -------------------------------------------------

    @staticmethod
    def _result(req_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    # -- core dispatch ----------------------------------------------------

    def handle(self, message: Any) -> dict | None:
        """Handle one parsed JSON-RPC message.

        Returns a response dict, or None for notifications (messages without an
        id / notification methods), which expect no reply.
        """
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            req_id = message.get("id") if isinstance(message, dict) else None
            return self._error(req_id, INVALID_REQUEST,
                               "invalid JSON-RPC 2.0 message")

        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if method == "initialize":
            return self._result(req_id, self._initialize(params))
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._result(req_id, {})
        if method == "tools/list":
            return self._result(
                req_id, {"tools": [t.spec() for t in self._tools.values()]})
        if method == "tools/call":
            return self._tools_call(req_id, params)

        if is_notification:
            return None
        return self._error(req_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        protocol = (requested if isinstance(requested, str) and requested
                    else self.default_protocol)
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": self.name, "version": self.version},
        }

    def _tools_call(self, req_id: Any, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = self._tools.get(name)
        if tool is None:
            return self._error(req_id, INVALID_PARAMS, f"unknown tool: {name}")
        try:
            output = tool.handler(arguments)
        except Exception as exc:  # noqa: BLE001 — tool errors are results, not RPC errors
            return self._result(req_id, {
                "content": [{"type": "text", "text": self._stringify(exc)}],
                "isError": True,
            })
        return self._result(req_id, {
            "content": [{"type": "text", "text": self._stringify(output)}],
        })

    @staticmethod
    def _stringify(output: Any) -> str:
        """Serialize a handler return (or exception) to a compact text block."""
        if isinstance(output, str):
            return output
        if isinstance(output, BaseException):
            to_dict = getattr(output, "to_dict", None)
            if callable(to_dict):
                try:
                    return json.dumps(to_dict(), separators=(",", ":"))
                except Exception:  # noqa: BLE001
                    pass
            return str(output) or output.__class__.__name__
        return json.dumps(output, separators=(",", ":"), default=str)

    # -- serve loop -------------------------------------------------------

    def serve(self, stdin=None, stdout=None) -> int:
        """Read-dispatch-write loop over stdio. Exits 0 on EOF (stdin close)."""
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._write(stdout, self._error(None, PARSE_ERROR, "parse error"))
                continue
            response = self.handle(message)
            if response is not None:
                self._write(stdout, response)
        return 0

    @staticmethod
    def _write(stdout, obj: dict) -> None:
        stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        stdout.flush()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test() -> int:
    import io

    srv = McpServer("test-server", "0.0.1")
    srv.register_tool(
        "echo", "Echo the message back.",
        {"type": "object", "properties": {"msg": {"type": "string"}},
         "required": ["msg"]},
        lambda args: {"echo": args.get("msg")},
    )

    def _boom(_args):
        raise ValueError("boom")

    srv.register_tool("boom", "Always fails.", {"type": "object"}, _boom)

    # initialize echoes the requested protocol version
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2099-01-01"}})
    assert r["result"]["protocolVersion"] == "2099-01-01", r
    assert r["result"]["serverInfo"]["name"] == "test-server"
    assert "tools" in r["result"]["capabilities"]

    # initialize with no version falls back to the default
    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
    assert r["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION

    # initialized notification → no response
    assert srv.handle({"jsonrpc": "2.0",
                       "method": "notifications/initialized"}) is None

    # ping
    r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert r["result"] == {}

    # tools/list preserves registration order + lean specs
    r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    names = [t["name"] for t in r["result"]["tools"]]
    assert names == ["echo", "boom"], names
    assert r["result"]["tools"][0]["description"] == "Echo the message back."

    # tools/call success → compact JSON content, no isError
    r = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"msg": "hi"}}})
    assert r["result"]["content"][0]["text"] == '{"echo":"hi"}', r
    assert "isError" not in r["result"]

    # tools/call handler raises → isError result (not an RPC error)
    r = srv.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "boom", "arguments": {}}})
    assert r["result"]["isError"] is True
    assert "boom" in r["result"]["content"][0]["text"]

    # unknown tool → invalid-params error
    r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}}})
    assert r["error"]["code"] == INVALID_PARAMS

    # unknown method → method-not-found
    r = srv.handle({"jsonrpc": "2.0", "id": 8, "method": "bogus"})
    assert r["error"]["code"] == METHOD_NOT_FOUND

    # malformed (missing jsonrpc) → invalid request
    r = srv.handle({"id": 9, "method": "ping"})
    assert r["error"]["code"] == INVALID_REQUEST

    # serve loop over an in-memory stream (ping, blank, bad json, tool call)
    inp = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        '\n'
        'not json\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"echo","arguments":{"msg":"x"}}}\n'
    )
    out = io.StringIO()
    srv.serve(inp, out)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert lines[0]["result"] == {}, lines
    assert lines[1]["error"]["code"] == PARSE_ERROR, lines
    assert lines[2]["result"]["content"][0]["text"] == '{"echo":"x"}', lines

    print("mcp_runtime self-test: OK")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    # No-tool transport smoke server (real servers register tools first).
    return McpServer("grimoire-mcp-runtime", "1.0.0").serve()


if __name__ == "__main__":
    raise SystemExit(main())
