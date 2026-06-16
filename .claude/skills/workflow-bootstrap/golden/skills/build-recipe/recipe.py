#!/usr/bin/env python3
"""recipe.py — the shared Grimoire build-recipe dispatcher (#79, v3.10).

Every Grimoire project expresses build/run/data operations as raw shell strings
in CLAUDE.md. That is not addressable by other skills under a stable name. This
dispatcher gives every project the SAME named targets (build / server / test /
seed / migrate / lint / clean) regardless of stack: a caller says
`recipe.py server --port 8420` and the correct project-specific command runs,
without the caller knowing what that command is.

- **Interface spec** (the canonical target vocabulary + parameter contract) is
  versioned here, in Grimoire source (INTERFACE below). `INTERFACE_VERSION` bumps
  when a target or parameter is added.
- **Per-project implementation** lives in `.claude/recipes.json` — readable by
  agents without executing, executed only through this dispatcher, synced (and
  only ever EXTENDED with stubs) by `sync-from-upstream`.
- **Contract:** exit 0 on success, the child command's exit code on failure;
  an unimplemented target fails with a clear message (never a silent no-op).

Parameter resolution (highest wins): CLI flag -> env var -> recipe default ->
interface (Grimoire) default. The `server` target's `--port` defaults to the
`GRIMOIRE_APP_PORT` env var (claimed by claim_port.py, #77) when present.

Design authority: docs/design/build-recipe-interface-design.md
(+ scripting-unification, docs/design/scripting-unification-design.md §3).

Usage:
  recipe.py <target> [--port N] [--env dev|prod|test] [--filter S] [--fixture S]
            [--watch] [--dry-run] [--list] [--generate STACK] [--recipes PATH]
            [--self-test]
"""
import argparse
import json
import os
import shlex
import subprocess
import sys

INTERFACE_VERSION = 4

# The canonical interface: target -> {desc, params{name->{env, default}}}.
# `env` names the environment variable consulted for that parameter's value.
INTERFACE = {
    "build":   {"desc": "compile / assemble the project", "params": {}},
    "server":  {"desc": "start the application server",
                "params": {"port": {"env": "GRIMOIRE_APP_PORT", "default": "3000"},
                           "env": {"env": None, "default": "dev"}}},
    "test":    {"desc": "run the test suite",
                "params": {"filter": {"env": None, "default": ""},
                           "watch": {"env": None, "default": ""}}},
    "seed":    {"desc": "populate a local data store with fixtures",
                "params": {"fixture": {"env": None, "default": ""},
                           "env": {"env": None, "default": "dev"}}},
    "migrate": {"desc": "run pending schema / data migrations",
                "params": {"env": {"env": None, "default": "dev"}}},
    "lint":    {"desc": "run static analysis / formatting checks", "params": {}},
    "clean":   {"desc": "remove build artifacts", "params": {}},
    # v2 (web-app deployment protocol, WEB-4): the producer + driver of the
    # deployable bundle. `package` emits the versioned bundle + release.json +
    # grimoire-build-info.json (docs/web-app-deployment-protocol.md §1/§2/§8);
    # `deploy` drives the install/self-update path (§3/§6). Both are
    # web-app-shape targets — stubbed unimplemented for non-web stacks, and like
    # every target they fail loud (exit 2) when called unimplemented.
    "package": {"desc": "assemble a versioned, deployable release bundle",
                "params": {"version": {"env": None, "default": ""},
                           "target": {"env": None, "default": ""}}},
    "deploy":  {"desc": "install / self-update a deployed instance from a bundle",
                "params": {"env": {"env": None, "default": "prod"}}},
    # v3 (dependency channel, DEP-CH-3): the consume side of the first-party
    # dependency substrate (docs/design/dependency-channel-design.md §4/§5/§6).
    # `sync-deps` reconciles/vendors deps from a release channel (resolve ->
    # download -> verify sha256 -> atomic-replace -> write vendor.lock); its
    # `mode` selects --check (drift-only) / --update (re-pin latest) / --offline
    # (zero-network verify). `vendor-check` is the conformance gate (exit 0 =
    # conformant, nonzero = violation); `full` selects the whole-vendor audit
    # over the diff-scoped default. Both are universal (every stack) and, like
    # every target, fail loud (exit 2) when a project hasn't implemented them.
    "sync-deps":    {"desc": "reconcile / vendor first-party deps from a release channel",
                     "params": {"mode": {"env": None, "default": ""}}},
    "vendor-check": {"desc": "dependency-channel conformance gate (exit 0/nonzero)",
                     "params": {"full": {"env": None, "default": ""}}},
    # v4 (runtime verification, VH-3): boot the app and verify the served surface
    # actually responds before promoting. `smoke` boots via the `server` target
    # command, curls the entry page (/) + at least one critical asset path, and
    # asserts HTTP 2xx + correct Content-Type for each. Exits 0 on pass, nonzero
    # on failure. No browser required. Web/server stacks get a curl-based stub;
    # cli/library stacks leave it unimplemented (exit 2, advisory).
    # Full spec: docs/design/runtime-verification-design.md.
    "smoke":    {"desc": "Boot app and verify entry page + critical assets return 2xx "
                         "with correct content-type. Exit 2 when unimplemented.",
                 "params": {"port": {"env": "GRIMOIRE_APP_PORT", "default": "3000"}}},
}

# Stack presets used by --generate to pre-fill inferrable targets. Unknown/other
# targets are stubbed unimplemented (command: null) so the agent never silently
# no-ops.
# `web` extends `server` with the deployment-protocol producers (package/deploy);
# server/cli/library leave package+deploy as unimplemented stubs (command: null),
# so a non-web project that calls them gets the loud exit-2, never a no-op.
# Every stack (v3) pre-fills the dependency-channel verbs `sync-deps` +
# `vendor-check` as stubs — the consume side is universal regardless of stack.
_DEP_CHANNEL_PRESET = {"sync-deps": "echo TODO sync-deps ${mode}",
                       "vendor-check": "echo TODO vendor-check ${full}"}
# v4 (runtime verification): web and server stacks pre-fill a curl-based smoke
# stub (implemented: false, TODO command); cli/library leave smoke absent so
# generate() stubs it as command: null (exit 2 when called — advisory).
_SMOKE_PRESET = {"smoke": "echo TODO smoke --port ${port}"}
STACK_PRESETS = {
    "server": {"build": "echo TODO build", "server": "echo TODO serve --port ${port}",
               "test": "echo TODO test", "lint": "echo TODO lint", "clean": "echo TODO clean",
               **_DEP_CHANNEL_PRESET, **_SMOKE_PRESET},
    "web":     {"build": "echo TODO build", "server": "echo TODO serve --port ${port}",
                "test": "echo TODO test", "lint": "echo TODO lint", "clean": "echo TODO clean",
                "package": "echo TODO package --version ${version} --target ${target}",
                "deploy": "echo TODO deploy --env ${env}",
                **_DEP_CHANNEL_PRESET, **_SMOKE_PRESET},
    "cli":     {"build": "echo TODO build", "test": "echo TODO test",
                "lint": "echo TODO lint", "clean": "echo TODO clean",
                **_DEP_CHANNEL_PRESET},
    "library": {"build": "echo TODO build", "test": "echo TODO test",
                "lint": "echo TODO lint", "clean": "echo TODO clean",
                **_DEP_CHANNEL_PRESET},
}

DEFAULT_RECIPES = os.path.join(".claude", "recipes.json")


class RecipeError(Exception):
    pass


def load_recipes(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise RecipeError("no recipe file at %s — run `recipe.py --generate <stack>`"
                          % path)
    except json.JSONDecodeError as e:
        raise RecipeError("recipe file %s is not valid JSON: %s" % (path, e))
    if not isinstance(data.get("targets"), dict):
        raise RecipeError("recipe file %s has no 'targets' object" % path)
    return data


def resolve_params(target, cli_args, env):
    """Resolve each interface param for `target` (CLI -> env -> recipe -> spec)."""
    spec = INTERFACE[target]["params"]
    recipe_defaults = cli_args.get("_recipe_params", {})
    out = {}
    for name, meta in spec.items():
        cli_val = cli_args.get(name)
        if cli_val not in (None, False, ""):
            out[name] = str(cli_val) if not isinstance(cli_val, bool) else "true"
            continue
        env_name = meta.get("env")
        if env_name and env.get(env_name):
            out[name] = env[env_name]
            continue
        if name in recipe_defaults and recipe_defaults[name] not in (None, ""):
            out[name] = str(recipe_defaults[name])
            continue
        out[name] = str(meta.get("default", ""))
    return out


def substitute(command, params):
    """Replace ${name} placeholders in a command template with resolved params."""
    rendered = command
    for name, val in params.items():
        rendered = rendered.replace("${%s}" % name, val)
    return rendered


def build_command(target, recipes, cli_args, env):
    if target not in INTERFACE:
        raise RecipeError("unknown target %r — valid targets: %s"
                          % (target, ", ".join(sorted(INTERFACE))))
    entry = recipes["targets"].get(target)
    if not entry or not entry.get("implemented") or not entry.get("command"):
        raise RecipeError(
            "target %r is not implemented in this project — edit %s (or run "
            "`recipe.py --generate <stack>` to stub it). Targets must never "
            "silently no-op." % (target, cli_args.get("_recipes_path", DEFAULT_RECIPES)))
    cli_args["_recipe_params"] = {k: v.get("default") for k, v in
                                  (entry.get("params") or {}).items()}
    params = resolve_params(target, cli_args, env)
    return substitute(entry["command"], params), params


def generate_recipes(stack, existing=None):
    """Return a recipes dict for `stack`, preserving any existing implemented
    targets and stubbing every interface target not yet present."""
    preset = STACK_PRESETS.get(stack, {})
    targets = {}
    existing_targets = (existing or {}).get("targets", {}) if existing else {}
    for name in INTERFACE:
        if name in existing_targets and existing_targets[name].get("implemented"):
            targets[name] = existing_targets[name]  # preserve project work
        elif name in preset:
            targets[name] = {"command": preset[name], "implemented": False,
                             "params": {p: {"default": INTERFACE[name]["params"][p].get("default", "")}
                                        for p in INTERFACE[name]["params"]}}
        else:
            targets[name] = {"command": None, "implemented": False}
    return {"interface-version": INTERFACE_VERSION, "stack": stack, "targets": targets}


def list_targets(recipes):
    rows = []
    for name in sorted(INTERFACE):
        entry = recipes.get("targets", {}).get(name, {}) if recipes else {}
        status = "implemented" if (entry.get("implemented") and entry.get("command")) else "stub"
        rows.append({"target": name, "status": status,
                     "desc": INTERFACE[name]["desc"],
                     "command": entry.get("command")})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grimoire build-recipe dispatcher.")
    ap.add_argument("target", nargs="?", help="one of: " + ", ".join(sorted(INTERFACE)))
    ap.add_argument("--port"); ap.add_argument("--env"); ap.add_argument("--filter")
    ap.add_argument("--fixture"); ap.add_argument("--watch", action="store_true")
    # v2 (web-app deployment protocol): package params.
    ap.add_argument("--version", dest="version", help="release version for `package`")
    ap.add_argument("--target", dest="target", help="target triple/platform for `package`")
    # v3 (dependency channel): sync-deps mode + vendor-check scope.
    ap.add_argument("--mode", dest="mode",
                    help="sync-deps mode flag, e.g. --check / --update / --offline")
    ap.add_argument("--full", dest="full", action="store_true",
                    help="vendor-check whole-vendor audit (default is diff-scoped)")
    ap.add_argument("--dry-run", action="store_true", help="print the command, do not run")
    ap.add_argument("--list", action="store_true", help="list targets + implemented status")
    ap.add_argument("--generate", metavar="STACK", help="write a stub recipes.json for a stack")
    ap.add_argument("--recipes", default=DEFAULT_RECIPES, help="recipe file path")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.generate:
        existing = None
        if os.path.exists(args.recipes):
            try:
                existing = load_recipes(args.recipes)
            except RecipeError:
                existing = None
        data = generate_recipes(args.generate, existing)
        os.makedirs(os.path.dirname(args.recipes) or ".", exist_ok=True)
        tmp = args.recipes + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, args.recipes)
        print("wrote %s (stack=%s, interface v%d)" % (args.recipes, args.generate, INTERFACE_VERSION))
        return 0

    try:
        if args.list:
            recipes = load_recipes(args.recipes) if os.path.exists(args.recipes) else {"targets": {}}
            print(json.dumps(list_targets(recipes), indent=2))
            return 0
        if not args.target:
            ap.error("a target is required (or use --list / --generate / --self-test)")
        recipes = load_recipes(args.recipes)
        cli_args = {"port": args.port, "env": args.env, "filter": args.filter,
                    "fixture": args.fixture, "watch": args.watch,
                    "version": args.version, "target": args.target,
                    "mode": args.mode, "full": args.full,
                    "_recipes_path": args.recipes}
        command, params = build_command(args.target, recipes, cli_args, os.environ)
    except RecipeError as e:
        print("recipe: %s" % e, file=sys.stderr)
        return 2

    if args.target == "server" and "port" in params:
        print("recipe: server port = %s" % params["port"], file=sys.stderr)
    if args.dry_run:
        print(command)
        return 0
    # Execute, passing the child's exit code straight through.
    return subprocess.call(command, shell=True)


def _self_test():
    import tempfile
    failures = []

    # generation: server stack stubs the inferrable targets + unimplemented seed.
    g = generate_recipes("server")
    if g["interface-version"] != INTERFACE_VERSION:
        failures.append("generate: wrong interface version")
    if INTERFACE_VERSION != 4:
        failures.append("interface version should be 4 (smoke added)")
    if g["targets"]["seed"]["command"] is not None or g["targets"]["seed"]["implemented"]:
        failures.append("generate: seed should be an unimplemented stub")
    if "${port}" not in (g["targets"]["server"]["command"] or ""):
        failures.append("generate: server stub should carry ${port}")
    # v2: deploy/package are interface targets; a non-web stack stubs them
    # unimplemented (command: null) so they fail loud, never no-op.
    for t in ("package", "deploy"):
        if t not in INTERFACE:
            failures.append("interface should define %r target" % t)
        if g["targets"][t]["command"] is not None or g["targets"][t]["implemented"]:
            failures.append("generate(server): %r should be an unimplemented stub" % t)
    # v3: sync-deps/vendor-check are interface targets pre-filled as stubs on
    # every stack (the consume side is universal); they fail loud (exit 2) when
    # a project calls them unimplemented.
    for t in ("sync-deps", "vendor-check"):
        if t not in INTERFACE:
            failures.append("interface should define %r target" % t)
        if g["targets"][t]["command"] is None or g["targets"][t]["implemented"]:
            failures.append("generate(server): %r should be a pre-filled unimplemented stub" % t)
    if "${mode}" not in (g["targets"]["sync-deps"]["command"] or ""):
        failures.append("generate: sync-deps stub should carry ${mode}")
    if "${full}" not in (g["targets"]["vendor-check"]["command"] or ""):
        failures.append("generate: vendor-check stub should carry ${full}")
    # v4: smoke is an interface target pre-filled as a stub on web and server
    # stacks (curl-based boot+probe); cli/library leave it as command: null.
    if "smoke" not in INTERFACE:
        failures.append("interface should define 'smoke' target")
    if g["targets"]["smoke"]["command"] is None or g["targets"]["smoke"]["implemented"]:
        failures.append("generate(server): smoke should be a pre-filled unimplemented stub")
    if "${port}" not in (g["targets"]["smoke"]["command"] or ""):
        failures.append("generate: smoke stub should carry ${port}")

    # the `web` stack pre-fills package/deploy as stubs carrying their params.
    gw = generate_recipes("web")
    if "${version}" not in (gw["targets"]["package"]["command"] or "") or \
       "${target}" not in (gw["targets"]["package"]["command"] or ""):
        failures.append("generate(web): package stub should carry ${version}/${target}")
    if "${env}" not in (gw["targets"]["deploy"]["command"] or ""):
        failures.append("generate(web): deploy stub should carry ${env}")
    if gw["targets"]["package"]["implemented"] or gw["targets"]["deploy"]["implemented"]:
        failures.append("generate(web): package/deploy stubs must be unimplemented")
    # web stack smoke stub: pre-filled (not null) but unimplemented; carries ${port}.
    if gw["targets"]["smoke"]["command"] is None or gw["targets"]["smoke"]["implemented"]:
        failures.append("generate(web): smoke should be a pre-filled unimplemented stub")
    if "${port}" not in (gw["targets"]["smoke"]["command"] or ""):
        failures.append("generate(web): smoke stub should carry ${port}")
    # cli stack: smoke is absent from the preset → generate() stubs command: null.
    gc = generate_recipes("cli")
    if gc["targets"]["smoke"]["command"] is not None or gc["targets"]["smoke"]["implemented"]:
        failures.append("generate(cli): smoke should be command:null unimplemented stub")

    # build a concrete recipes dict and exercise resolution + substitution.
    recipes = {"targets": {
        "server": {"command": "serve --port ${port} --env ${env}", "implemented": True,
                   "params": {"port": {"default": "3000"}, "env": {"default": "dev"}}},
        "test": {"command": "pytest ${filter}", "implemented": True,
                 "params": {"filter": {"default": ""}}},
        "seed": {"command": None, "implemented": False},
        # an implemented package target resolves ${version}/${target}; deploy is
        # an unimplemented stub (must fail loud, exit-2).
        "package": {"command": "just package --version ${version} --target ${target}",
                    "implemented": True,
                    "params": {"version": {"default": "0.0.0"}, "target": {"default": "host"}}},
        "deploy": {"command": None, "implemented": False},
        # an implemented sync-deps resolves ${mode}; vendor-check is an
        # unimplemented stub (must fail loud, exit-2).
        "sync-deps": {"command": "python3 sync_deps.py ${mode}", "implemented": True,
                      "params": {"mode": {"default": ""}}},
        "vendor-check": {"command": None, "implemented": False},
        # v4: an implemented smoke target resolves ${port}; its CLI/env/default
        # chain mirrors the server target.
        "smoke": {"command": "bash scripts/smoke.sh --port ${port}", "implemented": True,
                  "params": {"port": {"default": "3000"}}},
    }}

    # package param substitution: CLI > recipe default.
    cmd, p = build_command("package", recipes,
                           {"version": "1.2.3", "target": "aarch64-apple-darwin",
                            "_recipes_path": "x"}, {})
    if cmd != "just package --version 1.2.3 --target aarch64-apple-darwin":
        failures.append("package param substitution failed: %r" % cmd)
    cmd, p = build_command("package", recipes, {"_recipes_path": "x"}, {})
    if cmd != "just package --version 0.0.0 --target host":
        failures.append("package recipe-default substitution failed: %r" % cmd)

    # unimplemented deploy fails loud (never silent no-op).
    try:
        build_command("deploy", recipes, {"_recipes_path": "x"}, {})
        failures.append("unimplemented deploy should raise")
    except RecipeError:
        pass

    # sync-deps mode substitution: CLI value flows through ${mode}.
    cmd, p = build_command("sync-deps", recipes,
                           {"mode": "--check", "_recipes_path": "x"}, {})
    if cmd != "python3 sync_deps.py --check":
        failures.append("sync-deps mode substitution failed: %r" % cmd)
    # no --mode resolves to the recipe default (empty); ${mode} -> "".
    cmd, p = build_command("sync-deps", recipes, {"_recipes_path": "x"}, {})
    if cmd != "python3 sync_deps.py ":
        failures.append("sync-deps default-mode substitution failed: %r" % cmd)

    # unimplemented vendor-check fails loud (exit-2 dispatch contract).
    try:
        build_command("vendor-check", recipes, {"_recipes_path": "x"}, {})
        failures.append("unimplemented vendor-check should raise")
    except RecipeError:
        pass

    # v4: smoke port substitution — CLI > $GRIMOIRE_APP_PORT > recipe default.
    cmd, p = build_command("smoke", recipes,
                           {"port": "8420", "_recipes_path": "x"}, {})
    if cmd != "bash scripts/smoke.sh --port 8420":
        failures.append("smoke CLI port not honored: %r" % cmd)
    cmd, p = build_command("smoke", recipes, {"_recipes_path": "x"},
                           {"GRIMOIRE_APP_PORT": "20002"})
    if cmd != "bash scripts/smoke.sh --port 20002":
        failures.append("smoke env port not honored: %r" % cmd)
    cmd, p = build_command("smoke", recipes, {"_recipes_path": "x"}, {})
    if cmd != "bash scripts/smoke.sh --port 3000":
        failures.append("smoke recipe-default port not honored: %r" % cmd)

    # CLI flag wins.
    cmd, p = build_command("server", recipes, {"port": "8420", "_recipes_path": "x"}, {})
    if cmd != "serve --port 8420 --env dev":
        failures.append("cli port not honored: %r" % cmd)

    # env var (GRIMOIRE_APP_PORT) used when no CLI flag.
    cmd, p = build_command("server", recipes, {"_recipes_path": "x"},
                           {"GRIMOIRE_APP_PORT": "20001"})
    if cmd != "serve --port 20001 --env dev":
        failures.append("env port not honored: %r" % cmd)

    # recipe default used when neither CLI nor env present.
    cmd, p = build_command("server", recipes, {"_recipes_path": "x"}, {})
    if cmd != "serve --port 3000 --env dev":
        failures.append("recipe default port not honored: %r" % cmd)

    # CLI overrides env.
    cmd, p = build_command("server", recipes, {"port": "9999", "_recipes_path": "x"},
                           {"GRIMOIRE_APP_PORT": "20001"})
    if cmd != "serve --port 9999 --env dev":
        failures.append("cli should override env: %r" % cmd)

    # unimplemented target fails (never silent no-op).
    try:
        build_command("seed", recipes, {"_recipes_path": "x"}, {})
        failures.append("unimplemented target should raise")
    except RecipeError:
        pass

    # unknown target fails.
    try:
        build_command("frobnicate", recipes, {"_recipes_path": "x"}, {})
        failures.append("unknown target should raise")
    except RecipeError:
        pass

    # dry-run via main(): prints the resolved command, exits 0, runs nothing.
    with tempfile.TemporaryDirectory() as d:
        rp = os.path.join(d, "recipes.json")
        with open(rp, "w") as fh:
            json.dump(recipes, fh)
        rc = main(["test", "--filter", "-k smoke", "--dry-run", "--recipes", rp])
        if rc != 0:
            failures.append("dry-run should exit 0")
        # generation round-trips and preserves an implemented target.
        recipes["targets"]["build"] = {"command": "make", "implemented": True}
        with open(rp, "w") as fh:
            json.dump(recipes, fh)
        rc = main(["--generate", "cli", "--recipes", rp])
        if rc != 0:
            failures.append("generate should exit 0")
        regen = json.load(open(rp))
        if regen["targets"]["build"].get("command") != "make":
            failures.append("generate must preserve an implemented target")

    # determinism of generation.
    if json.dumps(generate_recipes("cli"), sort_keys=True) != \
       json.dumps(generate_recipes("cli"), sort_keys=True):
        failures.append("generate is non-deterministic")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("recipe self-test: OK (generate/stub/preserve, param resolution "
          "CLI>env>recipe>spec, ${} substitution, unimplemented+unknown raise, "
          "dry-run, determinism, interface-v4 deploy/package + sync-deps/"
          "vendor-check + smoke stubs + loud exit-2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
