# Grimoire MCP servers

Bundled [Model Context Protocol](https://modelcontextprotocol.io) servers that
give agents **native, token-cheap tools** for common Grimoire operations,
portable across every MCP harness (Claude Code, Cursor, VS Code / Copilot,
Windsurf, Codex). Authoritative design: `docs/design/mcp-server-design.md`.

## Layout

```
mcp-servers/
├── README.md                 ← this guide (the template)
├── lib/
│   └── mcp_runtime.py        ← reusable McpServer base (stdlib, zero deps)
└── issue-tracker/
    └── server.py             ← first instance: wraps issue_tracker.py
```

(claude-code flavor roots this at `.claude/mcp-servers/`; the copilot flavor at
`mcp-servers/` — the server resolves either layout automatically.)

## Why stdlib, no SDK

The runtime hand-rolls the MCP **stdio JSON-RPC** surface in pure Python 3
standard library — **zero third-party dependencies**, no `pip`/`npm` install, no
compiled wheels, runs on any Python 3, fastest cold start. The official SDKs were
evaluated and rejected (they pull ~15 deps incl. compiled `pydantic-core` and
`cryptography`); token cost to the agent and cross-harness compatibility are
identical either way, so the lightest option wins. See the design doc's
Alternatives section.

## Registration

A project-root `.mcp.json` registers the bundled server (Claude Code reads this
verbatim; merge-safe — your other `mcpServers` are preserved):

```json
{
  "mcpServers": {
    "grimoire-issue-tracker": {
      "command": "python3",
      "args": [".claude/mcp-servers/issue-tracker/server.py"]
    }
  }
}
```

Same block, different file per harness: Cursor `.cursor/mcp.json`, VS Code /
Copilot `.vscode/mcp.json`. The `mcp` config block in `grimoire-config.json`
(`enabled`, `prefer-for-tracker`) is Grimoire's master switch; bundled and **on
by default**.

## The issue-tracker server — tool surface

A thin adapter over the `issue_tracker.py` engine (all backend / routing / cache
/ cost logic stays in the engine). Eight tools, terse by design; list/search
return compact issues **without** body (body-on-demand via `get_issue`):

| Tool | Purpose |
|---|---|
| `list_issues` | list/filter open (or closed/all) issues, compact |
| `get_issue` | one issue including body |
| `search_issues` | keyword search, compact |
| `create_issue` | create (routes by `audience` when no `tracker`) |
| `comment_issue` | add a comment |
| `update_issue` | edit title/body/labels/state |
| `close_issue` | close |
| `label_issue` | add/remove labels |

**MCP-first, CLI-fallback contract.** Consumers prefer these tools when
`mcp.enabled` + `mcp.prefer-for-tracker` and the server is registered; otherwise
they fall back to `python3 .claude/skills/grm-issue-tracker/issue_tracker.py …`, so
non-MCP harnesses and disabled configs keep working unchanged.

## The grimoire-release server — tool surface (v3.27)

The second instance of the template: a thin adapter over the release-planning
ledger engine (`.claude/skills/grm-release-agent-tracker/release_plan.py`) and the
noir-loop state helper (`.claude/skills/grm-noir-loop/noir_loop_state.py`). Seven
tools, terse by design; compact JSON responses:

| Tool | Purpose |
|---|---|
| `get_ledger` | parse the §5 ledger of the active plan to JSON |
| `tick_rows` | atomically + idempotently flip §5 checkbox cells (file edit only) |
| `merge_queue` | merge order for ready rows, toposorted by §3's conflict map |
| `merge_preflight` | structured verdict: HEAD==staging + per-branch exists/commits-ahead |
| `plan_phase` | first all-unticked pass → `{phase, batches, model_assignments}` |
| `read_loop_state` | read the Noir iterative-loop state |
| `advance_loop` | advance the loop state (summary + open/next lists) |

**File-write-only contract.** The server runs **no git mutations** — every git
call is a read. The only side effect is editing the §5 ledger (via `tick_rows`)
or the gitignored loop-state file (via `advance_loop`); `merge_preflight` is
read-only and never merges. The **agent** commits. **CLI fallback**: `python3
.claude/skills/grm-release-agent-tracker/release_plan.py
{get-ledger|diff|merge-queue|merge-preflight|plan-phase|tick}`. Consumers
(`grm-release-agent-tracker`, `grm-ledger-tick`, `grm-release-phase`, `grm-release-phase-merge`,
`grm-noir-loop`) are re-pointed MCP-first. Design:
`docs/design/grimoire-release-server-design.md`.

## The v3.28 ops servers — `grimoire-status`, `grimoire-recipe`, `grimoire-environment`

Three more instances of the template (the second wave of the MCP expansion
audit, `docs/design/mcp-expansion-audit.md` ranks 2/5/8). Each is a thin
read-only/structured adapter over an existing engine; compact JSON responses:

| Server | Wraps | Tools | Contract |
|---|---|---|---|
| `grimoire-status`      | `status-broker/project_status.py` | `get_status` | **Read-only** — structured JSON project overview (name, framework version, paradigm, dials, latest/in-flight release, manifest version, tech stack, degraded-source warnings). No writes, no git, no tracker calls. |
| `grimoire-recipe`      | `build-recipe/recipe.py`          | `list_targets`, `dry_run`, `run_recipe` | Recipes stay project-defined in `.claude/recipes.json`; the server adds **no new execution authority**. `run_recipe` returns structured `{target, exit_code, ok, stdout, stderr}`. |
| `grimoire-environment` | `environment-manager/env_probe.py`| `list_processes`, `port_status`, `instance_urls` | **Read-only** process/port inspection. Lifecycle ops (`kill`/`start`) are deliberately **not** exposed — they stay per-action-authorized agent-side per `environment-manager-design.md`. |

**MCP-first, CLI-fallback contract.** Consumers (`grm-status-broker`, `grm-build-recipe`,
`grm-environment-manager`) prefer these tools when `mcp.enabled` and the server is
registered; otherwise they fall back to the identical engine CLI
(`project_status.py` / `recipe.py` / `env_probe.py`). Designs:
`docs/design/status-broker-design.md`, `build-recipe-interface-design.md`,
`environment-manager-design.md`, and `mcp-server-design.md`.

## Authoring a new Grimoire MCP server (the template)

1. Create `mcp-servers/<name>/server.py`.
2. Resolve the repo root + put `mcp-servers/lib` on `sys.path`, then
   `from mcp_runtime import McpServer` (copy the resolver from
   `issue-tracker/server.py`).
3. `srv = McpServer("grimoire-<name>", "1.0.0")`, then `srv.register_tool(name,
   one_line_description, lean_input_schema, handler)` per tool. Keep schemas
   minimal and responses compact — the agent pays for both every session.
4. Add a `--self-test` that drives `tools/list` + each `tools/call` against a
   fixture (inherit the pattern from `issue-tracker/server.py`).
5. Register it in `.mcp.json` (merge-safe), mirror across the flavors + golden,
   and add a `feature-manifest` row so downstream projects adopt it on sync.

Handlers return `str | dict | list` (serialized to one compact text block); a
raised exception becomes an MCP tool error (`isError`) carrying the message — so
structured `TrackerError`-style errors reach the agent intact.
