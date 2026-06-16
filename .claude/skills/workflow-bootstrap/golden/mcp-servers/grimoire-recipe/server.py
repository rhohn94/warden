#!/usr/bin/env python3
"""server.py — Grimoire grimoire-recipe MCP server.

A thin McpServer subclass over the build-recipe dispatcher
(.claude/skills/build-recipe/recipe.py). Exposes three tools:
  - list_targets   — enumerate the interface vocabulary + per-project status
  - dry_run        — resolve a target's command without executing (structured result)
  - run_recipe     — execute a recipe target; returns structured exit/output result

Recipes remain project-defined in .claude/recipes.json — this server adds no
new execution authority; it surfaces the same dispatcher logic already available
via `recipe.py <target> [--dry-run]`. Built on the reusable stdlib runtime
(.claude/mcp-servers/lib/mcp_runtime.py). No third-party dependencies (Python 3
stdlib only).

File-write contract: this server NEVER writes files. list_targets and dry_run
are fully read-only; run_recipe executes the project's own recipe command
(same authority as the agent calling recipe.py directly).

Registered by the project-root .mcp.json as `grimoire-recipe`:
    { "command": "python3",
      "args": [".claude/mcp-servers/grimoire-recipe/server.py"] }

CLI:  python3 server.py              # run the stdio MCP server
      python3 server.py --self-test
"""

from __future__ import annotations

import pathlib
import subprocess
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
    """Put the runtime lib + recipe engine on sys.path (layout-agnostic).

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
    recipe_candidates = [
        repo_root / ".claude" / "skills" / "build-recipe",
        repo_root / "scripts",
    ]
    for candidates in (lib_candidates, recipe_candidates):
        for cand in candidates:
            if cand.exists():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break


REPO_ROOT = _find_repo_root()
_bootstrap_imports(REPO_ROOT)

from mcp_runtime import McpServer  # noqa: E402  (path set above)
import recipe as recipe_mod  # noqa: E402


class RecipeServer(McpServer):
    """McpServer subclass wrapping build-recipe's recipe.py as 3 tools."""

    def __init__(self, repo_root: pathlib.Path | None = None):
        super().__init__("grimoire-recipe", "1.0.0")
        self.repo_root = str(repo_root or REPO_ROOT)
        self._register()

    def _recipes_path(self, override: str | None = None) -> str:
        if override:
            return override
        return str(pathlib.Path(self.repo_root) / ".claude" / "recipes.json")

    def _register(self) -> None:
        # Schemas are intentionally lean (only essential params) to keep the
        # recurring tools/list payload small.
        self.register_tool(
            "list_targets",
            "List all interface targets with per-project implementation status "
            "(implemented/stub), description, and resolved command template.",
            {"type": "object", "properties": {
                "recipes": {"type": "string",
                            "description": "Path to recipes.json (default: .claude/recipes.json)"}}},
            self._list_targets)
        self.register_tool(
            "dry_run",
            "Resolve the command for a recipe target without executing it. "
            "Returns {target, command, params} — same as recipe.py <target> --dry-run.",
            {"type": "object", "properties": {
                "target": {"type": "string"},
                "port": {"type": "string"},
                "env": {"type": "string"},
                "filter": {"type": "string"},
                "fixture": {"type": "string"},
                "watch": {"type": "boolean"},
                "version": {"type": "string"},
                "recipes": {"type": "string",
                            "description": "Path to recipes.json (default: .claude/recipes.json)"}},
             "required": ["target"]},
            self._dry_run)
        self.register_tool(
            "run_recipe",
            "Execute a recipe target. Returns {target, exit_code, ok, stdout, stderr} — "
            "structured result instead of free-form subprocess output. "
            "Recipes remain project-defined; this tool adds no new execution authority.",
            {"type": "object", "properties": {
                "target": {"type": "string"},
                "port": {"type": "string"},
                "env": {"type": "string"},
                "filter": {"type": "string"},
                "fixture": {"type": "string"},
                "watch": {"type": "boolean"},
                "version": {"type": "string"},
                "recipes": {"type": "string",
                            "description": "Path to recipes.json (default: .claude/recipes.json)"}},
             "required": ["target"]},
            self._run_recipe)

    # -- shared param extractor -------------------------------------------

    def _extract_cli_args(self, a: dict, recipes_path: str) -> dict:
        return {
            "port": a.get("port"),
            "env": a.get("env"),
            "filter": a.get("filter"),
            "fixture": a.get("fixture"),
            "watch": bool(a.get("watch", False)),
            "version": a.get("version"),
            "_recipes_path": recipes_path,
        }

    # -- handlers ---------------------------------------------------------

    def _list_targets(self, a: dict):
        rp = self._recipes_path(a.get("recipes"))
        try:
            recipes = recipe_mod.load_recipes(rp)
        except recipe_mod.RecipeError:
            recipes = {"targets": {}}
        return recipe_mod.list_targets(recipes)

    def _dry_run(self, a: dict):
        import os
        target = a["target"]
        rp = self._recipes_path(a.get("recipes"))
        recipes = recipe_mod.load_recipes(rp)
        cli_args = self._extract_cli_args(a, rp)
        command, params = recipe_mod.build_command(target, recipes, cli_args, os.environ)
        return {"target": target, "command": command, "params": params}

    def _run_recipe(self, a: dict):
        import os
        target = a["target"]
        rp = self._recipes_path(a.get("recipes"))
        recipes = recipe_mod.load_recipes(rp)
        cli_args = self._extract_cli_args(a, rp)
        command, params = recipe_mod.build_command(target, recipes, cli_args, os.environ)
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True)
        return {
            "target": target,
            "exit_code": result.returncode,
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


# ---------------------------------------------------------------------------
# Self-test (fixture recipes.json + dry-run fixture; no real subprocess)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    import json
    import tempfile

    root = pathlib.Path(tempfile.mkdtemp())
    (root / ".claude").mkdir()
    (root / ".claude" / "grimoire-config.json").write_text(
        '{"schema-version":4,"name":"t"}')

    # Write a minimal recipes.json fixture.
    recipes_data = {
        "interface-version": recipe_mod.INTERFACE_VERSION,
        "stack": "server",
        "targets": {
            "build": {"command": "make build", "implemented": True},
            "server": {"command": "serve --port ${port} --env ${env}",
                       "implemented": True,
                       "params": {"port": {"default": "3000"},
                                  "env": {"default": "dev"}}},
            "test": {"command": "pytest ${filter}", "implemented": True,
                     "params": {"filter": {"default": ""}}},
            "lint": {"command": None, "implemented": False},
            "clean": {"command": None, "implemented": False},
            "seed": {"command": None, "implemented": False},
            "migrate": {"command": None, "implemented": False},
            "package": {"command": None, "implemented": False},
            "deploy": {"command": None, "implemented": False},
        },
    }
    recipes_path = root / ".claude" / "recipes.json"
    recipes_path.write_text(json.dumps(recipes_data, indent=2))

    srv = RecipeServer(repo_root=root)

    def call(name, args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        res = r["result"]
        text = res["content"][0]["text"]
        parsed = json.loads(text) if text[:1] in "[{" else text
        return res, parsed

    # tools/list advertises exactly 3 tools in order.
    tl = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
    names = [t["name"] for t in tl["result"]["tools"]]
    assert names == ["list_targets", "dry_run", "run_recipe"], names

    # list_targets returns a list with all INTERFACE targets.
    res, out = call("list_targets", {"recipes": str(recipes_path)})
    assert "isError" not in res, res
    assert isinstance(out, list), out
    target_names = {row["target"] for row in out}
    assert {"build", "server", "test", "lint"}.issubset(target_names), target_names
    build_row = next(r for r in out if r["target"] == "build")
    assert build_row["status"] == "implemented", build_row
    lint_row = next(r for r in out if r["target"] == "lint")
    assert lint_row["status"] == "stub", lint_row

    # dry_run resolves command without executing (CLI param wins).
    res, out = call("dry_run", {"target": "server", "port": "8420",
                                "recipes": str(recipes_path)})
    assert "isError" not in res, res
    assert out["target"] == "server", out
    assert "8420" in out["command"], out
    assert out["params"]["port"] == "8420", out

    # dry_run uses recipe default when no CLI param given.
    res, out = call("dry_run", {"target": "server",
                                "recipes": str(recipes_path)})
    assert "3000" in out["command"], out

    # dry_run on an unimplemented target -> isError.
    res, out = call("dry_run", {"target": "lint",
                                "recipes": str(recipes_path)})
    assert res.get("isError") is True, res

    # dry_run on unknown target -> isError.
    res, out = call("dry_run", {"target": "frobnicate",
                                "recipes": str(recipes_path)})
    assert res.get("isError") is True, res

    # run_recipe — use a no-op command (echo) to verify structured result shape.
    recipes_data["targets"]["build"]["command"] = "echo hello"
    recipes_path.write_text(json.dumps(recipes_data, indent=2))
    res, out = call("run_recipe", {"target": "build",
                                   "recipes": str(recipes_path)})
    assert "isError" not in res, res
    assert out["target"] == "build", out
    assert out["exit_code"] == 0, out
    assert out["ok"] is True, out
    assert "hello" in out["stdout"], out

    # run_recipe on a failing command returns ok=False + correct exit code.
    recipes_data["targets"]["build"]["command"] = "exit 42"
    recipes_path.write_text(json.dumps(recipes_data, indent=2))
    res, out = call("run_recipe", {"target": "build",
                                   "recipes": str(recipes_path)})
    assert "isError" not in res, res
    assert out["exit_code"] == 42, out
    assert out["ok"] is False, out

    # list_targets with no recipes.json falls back to empty stub list.
    res, out = call("list_targets", {})
    assert "isError" not in res, res  # graceful — no file == all stubs

    print("grimoire-recipe server self-test: OK")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    return RecipeServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
