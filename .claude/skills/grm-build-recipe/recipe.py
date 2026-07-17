#!/usr/bin/env python3
"""recipe.py — the shared Grimoire build-recipe dispatcher (#79, v3.10).

Every Grimoire project expresses build/run/data operations as raw shell strings
in CLAUDE.md. That is not addressable by other skills under a stable name. This
dispatcher gives every project the SAME named targets (build / server / test /
unit-test / gui-test / seed / migrate / lint / clean) regardless of stack: a caller
says `recipe.py server --port 8420` and the correct project-specific command runs,
without the caller knowing what that command is.

- **Interface spec** (the canonical target vocabulary + parameter contract) is
  versioned here, in Grimoire source (INTERFACE below). `INTERFACE_VERSION` bumps
  when a target or parameter is added.
- **Per-project implementation** lives in `.claude/recipes.json` — readable by
  agents without executing, executed only through this dispatcher, synced (and
  only ever EXTENDED with stubs) by `grm-sync-from-upstream`.
- **Contract:** exit 0 on success, the child command's exit code on failure;
  an unimplemented target fails with a clear message (never a silent no-op).

Parameter resolution (highest wins): CLI flag -> env var -> recipe default ->
interface (Grimoire) default. The `server` target's `--port` defaults to the
`GRIMOIRE_APP_PORT` env var (claimed by claim_port.py, #77) when present.

`seed` (#438) is special-cased: unlike every other target, it is dispatched by
a GENERIC, framework-owned engine built into this file (see "fixtures/
convention" below) rather than routed through the project's
`.claude/recipes.json` command template. A project adopts it by adding a
`fixtures/` directory — no project-side script to write.

Design authority: docs/grimoire/design/build-recipe-interface-design.md
(+ scripting-unification, docs/grimoire/design/scripting-unification-design.md §3);
the `fixtures/` convention: docs/design/fixtures-convention-design.md.

Usage:
  recipe.py <target> [--port N] [--env dev|prod|test] [--filter S] [--fixture S]
            [--watch] [--dry-run] [--list] [--generate STACK] [--recipes PATH]
            [--self-test]
  recipe.py seed [--fixture NAME] [--env dev] [--if-empty] [--allow-non-dev]
            [--fixtures-root PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

INTERFACE_VERSION = 9

# The canonical interface: target -> {desc, params{name->{env, default}}}.
# `env` names the environment variable consulted for that parameter's value.
INTERFACE = {
    # v8 (#442): `env` was missing here while every stack preset's build command
    # embeds `${env}` — the param was never resolved/substituted, so
    # `recipe.py build --env prod` silently ran with an unresolved (or
    # default-"dev") token and could package a debug binary as a release asset.
    "build":   {"desc": "compile / assemble the project",
                "params": {"env": {"env": None, "default": "dev"}}},
    "server":  {"desc": "start the application server",
                "params": {"port": {"env": "GRIMOIRE_APP_PORT", "default": "3000"},
                           "env": {"env": None, "default": "dev"}}},
    "test":    {"desc": "run the test suite",
                "params": {"filter": {"env": None, "default": ""},
                           "watch": {"env": None, "default": ""}}},
    # v8 (#360): the fast, isolated subset of `test` — excludes integration/
    # e2e/slow marks so the post-commit gate (#361), the Verifier, and
    # phase-merge can trigger "just the fast tests" without per-stack
    # knowledge. `test` is UNCHANGED (still the full suite); `unit-test` is
    # strictly a faster subset of the same signal, never a replacement for
    # `test` in the merge gate. Per-stack mapping (docs/coding-standards.md +
    # per-language sub-docs): Python `pytest -m "not slow and not
    # integration"` (reuses the existing @pytest.mark.slow/.integration
    # convention, coding-standards/python.md §Testing); JS/TS `vitest run`
    # against co-located `*.test.ts` (not `tests/`/`e2e/`); Rust
    # `cargo test --lib` (in-module `#[cfg(test)]`, not crate-root `tests/`);
    # Go `go test -short ./...`. Same params as `test` (filter/watch); same
    # exit-code contract as every target (unimplemented -> exit 2 advisory).
    # Full spec: docs/grimoire/design/runtime-verification-design.md
    # §Unit test vs. full test run.
    "unit-test": {"desc": "run only fast, isolated unit tests (excludes "
                          "integration/e2e/slow marks). Exit 2 when "
                          "unimplemented.",
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
    # dependency substrate (docs/grimoire/design/dependency-channel-design.md §4/§5/§6).
    # `grm-sync-deps` reconciles/vendors deps from a release channel (resolve ->
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
    # Full spec: docs/grimoire/design/runtime-verification-design.md.
    "smoke":    {"desc": "Boot app and verify entry page + critical assets return 2xx "
                         "with correct content-type. Exit 2 when unimplemented.",
                 "params": {"port": {"env": "GRIMOIRE_APP_PORT", "default": "3000"}}},
    # v9 (#362): platform-differentiated GUI feature test, parallel to `smoke`.
    # Two strategies behind the one verb: web projects exercise the changed
    # flow via the agent's own Preview/Browser surface (preview_start/navigate/
    # read_page/computer/read_console_messages) — not scriptable from a shell
    # recipe, so web stacks stub this unimplemented (exit 2) and the design doc
    # documents the agent-driven workflow as the actual verification step;
    # desktop/GUI projects drive a screenshot-cli-style headless capture,
    # diffed against a baseline at tests/gui-baselines/<baseline>.png (or a
    # project's chosen deterministic-snapshot format when true pixel capture
    # would reintroduce a display/GPU dependency — see the gui quick-start
    # template's reference implementation). Same exit-code contract as smoke
    # (0 pass / nonzero probe-failure / 2 unimplemented-advisory). Full spec:
    # docs/grimoire/design/runtime-verification-design.md §GUI testing.
    "gui-test": {"desc": "Platform-differentiated GUI feature test (web: "
                        "Preview-driven, agent-executed; desktop: headless "
                        "screenshot/snapshot diff against a committed "
                        "baseline). Exit 2 when unimplemented.",
                 "params": {"baseline": {"env": None, "default": "main"}}},
    # v5 (recipe layer phase 2, issue #201 §4): the changelog-derived release
    # ceremony (no args). Derives the version from the newest changelog heading,
    # guards (release branch / clean tree / tag absent / changelog entry present),
    # bumps the version file(s), tests + release-builds, archives, commits, tags,
    # and reconciles the matching milestone:v{X.Y} issues into release notes via
    # the issue-tracker abstraction. v7 (v3.90, autonomous-wave): EVERY stack
    # preset pre-fills a `just release` stub — a repo with no release ceremony
    # never reaches a clean dev==main boundary, which is what keeps framework
    # syncs autonomous-safe (BMI-3). Reference implementation:
    # quick-start-templates/web/files/scripts/release.sh.
    "release":  {"desc": "changelog-derived release ceremony (bump/test/build/tag "
                         "+ milestone reconciliation). Exit 2 when unimplemented.",
                 "params": {}},
    # v6 (RSS-4, #322): kill running instance(s) of THIS project's process.
    # Resolution order: `--port` -> `$GRIMOIRE_APP_PORT` -> the pidfile `run`
    # wrote -> a project-declared process pattern. Only kills processes
    # positively identified as this project's own; idempotent (nothing running
    # is exit 0 with a report, never an error). Web/service stacks get a real
    # generic reference implementation (`scripts/stop.sh`, universal like
    # sync-deps/vendor-check); other stacks leave it `command: null` (exit 2
    # when called — advisory, like every unimplemented target).
    # Full spec: docs/design/justfile-standard-design.md §stop.
    "stop":     {"desc": "kill running instance(s) of this project's process. "
                        "Exit 2 when unimplemented.",
                 "params": {"port": {"env": "GRIMOIRE_APP_PORT", "default": ""}}},
}

# Dispatcher aliases: alternate target spellings that resolve to a canonical
# INTERFACE target. `run` is the canonical *justfile* recipe name (Justfile
# standard §2); the versioned INTERFACE target has always been `server`, kept
# permanently as a dispatcher alias so `recipe.py server` and `recipe.py run`
# resolve to the SAME recipe entry (whose command is `just run …`). This is a
# pure alias — no new INTERFACE verb — so INTERFACE_VERSION is NOT bumped for the
# server↔run reconciliation (RSS-3, #321). Authority:
# docs/grimoire/design/build-recipe-interface-design.md §run↔server reconciliation
# + docs/design/justfile-standard-design.md §2.
ALIASES = {"run": "server"}


def resolve_alias(target: str) -> str:
    """Map a dispatcher alias to its canonical INTERFACE target (identity if none)."""
    return ALIASES.get(target, target)

# Stack presets used by --generate to pre-fill inferrable targets. Unknown/other
# targets are stubbed unimplemented (command: null) so the agent never silently
# no-ops.
# RSS-3 (#321): every preset command routes through the standard `justfile` —
# `just <recipe> …` — so a generated recipes.json is a thin routing table over the
# justfile (the one place recipes live). The `server` target routes to `just run`
# (canonical justfile name; see ALIASES). Multi-line logic lives in `scripts/`;
# the thin `just` recipe delegates to it. Preset commands are stubs
# (implemented: false) until the project wires the justfile recipe body and flips
# implemented: true.
# `web` extends `server` with the deployment-protocol producers (package/deploy);
# server/cli/library leave deploy as an unimplemented stub (command: null), so a
# non-web project that calls it gets the loud exit-2, never a no-op.
# Every stack (v3) pre-fills the dependency-channel verbs `sync-deps` +
# `vendor-check` as stubs whose `just` recipe delegates to the framework scripts —
# the consume side is universal regardless of stack.
# v7 (v3.90): every stack pre-fills `release` (see the INTERFACE note — releases
# keep the dev==main boundary clean, which keeps syncs autonomous-safe), and a
# `native` stack joins for desktop/app-bundle projects: web's shape minus
# `deploy` (nothing to deploy to an environment), plus `package` (the bundle is
# the artifact).
_DEP_CHANNEL_PRESET = {"sync-deps": "just sync-deps ${mode}",
                       "vendor-check": "just vendor-check ${full}"}
# v4 (runtime verification): web and server stacks pre-fill a smoke stub
# (implemented: false) routing to `just smoke`; cli/library leave smoke absent so
# generate() stubs it as command: null (exit 2 when called — advisory).
_SMOKE_PRESET = {"smoke": "just smoke ${port}"}
# v9 (#362): only stacks with an actual GUI surface pre-fill `gui-test` — web
# (Preview-driven) and native (desktop screenshot/snapshot-driven). `server`/
# `cli`/`library` have no GUI to test, so they leave it absent like `smoke`,
# and generate() stubs it command: null (exit 2 — advisory, never a no-op).
_GUI_TEST_PRESET = {"gui-test": "just gui-test ${baseline}"}
# v6 (RSS-4, #322): web/server stacks pre-fill a stop stub routing to `just
# stop`; cli/library leave stop absent (a stack with no runnable process has
# nothing to stop) so generate() stubs it command: null (exit 2 — advisory).
_STOP_PRESET = {"stop": "just stop ${port}"}
# v8 (#360): every stack pre-fills `unit-test` alongside `test` — the fast
# subset is universal vocabulary regardless of stack (the per-stack command
# mapping lives in the project's own justfile, not here). Stubbed
# unimplemented like `test` until the project wires the real command.
_UNIT_TEST_PRESET = {"unit-test": "just unit-test"}
STACK_PRESETS = {
    "server": {"build": "just build ${env}",
               "server": "just run ${env} ${port}",
               "test": "just test", "lint": "just lint", "clean": "just clean",
               "release": "just release",
               **_DEP_CHANNEL_PRESET, **_SMOKE_PRESET, **_STOP_PRESET,
               **_UNIT_TEST_PRESET},
    "web":     {"build": "just build ${env}",
                "server": "just run ${env} ${port}",
                "test": "just test", "lint": "just lint", "clean": "just clean",
                "package": "just package ${version} ${target}",
                "deploy": "just deploy ${env}",
                "release": "just release",
                **_DEP_CHANNEL_PRESET, **_SMOKE_PRESET, **_STOP_PRESET,
                **_UNIT_TEST_PRESET, **_GUI_TEST_PRESET},
    "cli":     {"build": "just build ${env}", "test": "just test",
                "lint": "just lint", "clean": "just clean",
                "release": "just release",
                **_DEP_CHANNEL_PRESET, **_UNIT_TEST_PRESET},
    "library": {"build": "just build ${env}", "test": "just test",
                "lint": "just lint", "clean": "just clean",
                "release": "just release",
                **_DEP_CHANNEL_PRESET, **_UNIT_TEST_PRESET},
    "native":  {"build": "just build ${env}",
                "server": "just run ${env} ${port}",
                "test": "just test", "lint": "just lint", "clean": "just clean",
                "package": "just package ${version} ${target}",
                "release": "just release",
                **_DEP_CHANNEL_PRESET, **_SMOKE_PRESET, **_STOP_PRESET,
                **_UNIT_TEST_PRESET, **_GUI_TEST_PRESET},
}

DEFAULT_RECIPES = os.path.join(".claude", "recipes.json")


class RecipeError(Exception):
    pass


def load_recipes(path: str) -> dict:
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


def resolve_params(target: str, cli_args: dict, env: dict) -> dict:
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


_UNSUBSTITUTED_TOKEN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def substitute(command: str, params: dict) -> str:
    """Replace ${name} placeholders in a command template with resolved params.

    #442: raises RecipeError if any ${...} token survives substitution. A
    silently-unresolved token (e.g. a preset's ${env} with no matching
    INTERFACE param) changes the invoked command's meaning without changing
    its apparent success — the class of bug that let
    `recipe.py build --env prod` run as an un-parameterized debug build while
    the release ceremony reported success. Refuse to execute, never no-op.
    """
    rendered = command
    for name, val in params.items():
        rendered = rendered.replace("${%s}" % name, val)
    leftover = sorted(set(_UNSUBSTITUTED_TOKEN.findall(rendered)))
    if leftover:
        raise RecipeError(
            "command template %r has unresolved token(s) %s after "
            "substitution — declare the matching param in INTERFACE (and the "
            "project's recipes.json target entry). Refusing to run a command "
            "with unsubstituted placeholders." % (command, ", ".join(leftover)))
    return rendered


def build_command(target: str, recipes: dict, cli_args: dict, env: dict) -> tuple:
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


# ---------------------------------------------------------------------------
# fixtures/ convention: generic seed-dispatch engine (#438).
#
# Unlike every other INTERFACE target, `seed` is NOT dispatched through a
# project's `.claude/recipes.json` command template. It is a real, working,
# framework-owned default: a project adopts it purely by adding a `fixtures/`
# directory (docs/design/fixtures-convention-design.md) — no project-side
# script required. The only stack-specific knowledge is the `apply` /
# `empty-check` shell templates declared IN each fixture set's manifest.json,
# never hardcoded here.
# ---------------------------------------------------------------------------

FIXTURES_ROOT_DEFAULT = "fixtures"
FIXTURE_MANIFEST_NAME = "manifest.json"
_FIXTURE_FAMILY_GLOBS = {"sql": "*.sql", "json": "*.json"}
_FIXTURE_VALID_FAMILIES = frozenset(_FIXTURE_FAMILY_GLOBS)
_FIXTURE_VALID_STRATEGIES = frozenset({"truncate-and-load", "upsert"})


class FixtureError(RecipeError):
    """A fixtures/ convention violation, or a seed-apply failure."""


def load_fixture_manifest(set_dir: Path) -> dict:
    """Load + validate a fixture set's manifest.json (raises FixtureError).

    Schema: docs/design/fixtures-convention-design.md. `files`, if omitted, is
    filled in with a sorted glob of the family's default extension.
    """
    manifest_path = set_dir / FIXTURE_MANIFEST_NAME
    if not manifest_path.is_file():
        raise FixtureError(
            "fixture set %r has no %s at %s — see "
            "docs/design/fixtures-convention-design.md"
            % (set_dir.name, FIXTURE_MANIFEST_NAME, manifest_path))
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FixtureError("fixture set %r manifest.json is not valid JSON: %s"
                           % (set_dir.name, e))
    family = data.get("family")
    if family not in _FIXTURE_VALID_FAMILIES:
        raise FixtureError(
            "fixture set %r manifest.json: 'family' must be one of %s, got %r"
            % (set_dir.name, sorted(_FIXTURE_VALID_FAMILIES), family))
    strategy = data.get("strategy")
    if strategy not in _FIXTURE_VALID_STRATEGIES:
        raise FixtureError(
            "fixture set %r manifest.json: 'strategy' must be one of %s, got %r"
            % (set_dir.name, sorted(_FIXTURE_VALID_STRATEGIES), strategy))
    apply_cmd = data.get("apply")
    if not apply_cmd or "{file}" not in apply_cmd:
        raise FixtureError(
            "fixture set %r manifest.json: 'apply' must be a non-empty "
            "command template containing a {file} placeholder" % set_dir.name)
    files = data.get("files")
    if files is None:
        # Exclude the manifest itself: a "json" family's default glob (*.json)
        # would otherwise match manifest.json too.
        files = sorted(p.name for p in set_dir.glob(_FIXTURE_FAMILY_GLOBS[family])
                       if p.name != FIXTURE_MANIFEST_NAME)
    if not files:
        raise FixtureError(
            "fixture set %r has no fixture files (family=%r, none declared "
            "or discovered)" % (set_dir.name, family))
    data["files"] = files
    return data


def _render_fixture_command(template: str, *, file: str, env: str) -> str:
    """Render a fixture manifest's `apply`/`empty-check` template.

    Placeholders are single-brace `{file}`/`{env}` — deliberately distinct
    from the `${...}` syntax `substitute()` uses for recipes.json command
    templates, so real shell env-var refs (`$DATABASE_URL`) in the same
    string expand normally at execution time instead of colliding.
    """
    return template.replace("{file}", file).replace("{env}", env)


def discover_fixture_sets(fixtures_root: Path) -> list:
    """Sorted fixture-set names under `fixtures_root` ([] if it doesn't exist)."""
    if not fixtures_root.is_dir():
        return []
    return sorted(p.name for p in fixtures_root.iterdir() if p.is_dir())


def apply_fixture_set(fixtures_root: Path, name: str, env: str, *,
                      dry_run: bool = False, runner=subprocess.call) -> list:
    """Apply one fixture set's files in declared order. Returns the rendered
    commands (run, unless dry_run). Raises FixtureError on a bad manifest or a
    failing apply command (never a silent partial apply)."""
    set_dir = fixtures_root / name
    if not set_dir.is_dir():
        raise FixtureError("fixture set %r not found under %s" % (name, fixtures_root))
    manifest = load_fixture_manifest(set_dir)
    commands = []
    for fname in manifest["files"]:
        fpath = set_dir / fname
        if not fpath.is_file():
            raise FixtureError("fixture set %r: declared file %r not found at %s"
                               % (name, fname, fpath))
        cmd = _render_fixture_command(manifest["apply"], file=str(fpath), env=env)
        commands.append(cmd)
        if dry_run:
            continue
        rc = runner(cmd, shell=True)
        if rc != 0:
            raise FixtureError(
                "fixture set %r: apply command failed (exit %d) for %r: %s"
                % (name, rc, fname, cmd))
    return commands


def fixture_set_is_empty(fixtures_root: Path, name: str, env: str, *,
                         runner=subprocess.call) -> bool:
    """True iff the set has no `empty-check` (always seed) or the check exits
    0 (datastore reports empty). False iff the check exits nonzero."""
    manifest = load_fixture_manifest(fixtures_root / name)
    check_cmd = manifest.get("empty-check")
    if not check_cmd:
        return True
    rendered = _render_fixture_command(check_cmd, file="", env=env)
    return runner(rendered, shell=True) == 0


def cmd_seed(*, fixture: str | None, env: str, fixtures_root: str,
            if_empty: bool, allow_non_dev: bool, dry_run: bool) -> int:
    """The `seed` verb's generic dispatch entry point (bypasses recipes.json
    entirely — see the fixtures/ convention design doc). Fail-closed outside
    `dev`: checked BEFORE anything else so a non-dev call never touches a
    fixtures/ directory or a datastore, even by accident."""
    if env != "dev" and not allow_non_dev:
        print(
            "recipe: refusing to seed env=%r (non-dev) without --allow-non-dev "
            "— seeding is fail-closed outside dev "
            "(docs/design/fixtures-convention-design.md)" % env, file=sys.stderr)
        return 2
    root = Path(fixtures_root)
    if not root.is_dir():
        print(
            "recipe: no %s/ directory — nothing to seed. See "
            "docs/design/fixtures-convention-design.md to adopt the convention."
            % fixtures_root, file=sys.stderr)
        return 2
    names = [fixture] if fixture else discover_fixture_sets(root)
    if not names:
        print("recipe: %s/ has no fixture sets" % fixtures_root, file=sys.stderr)
        return 2
    for name in names:
        try:
            if if_empty and not fixture_set_is_empty(root, name, env):
                print("recipe: seed %r skipped (--if-empty: datastore reports "
                     "non-empty)" % name)
                continue
            commands = apply_fixture_set(root, name, env, dry_run=dry_run)
        except FixtureError as e:
            print("recipe: %s" % e, file=sys.stderr)
            return 2
        if dry_run:
            for c in commands:
                print(c)
        else:
            print("recipe: seed %r applied (%d fixture file(s), env=%s)"
                 % (name, len(commands), env))
    return 0


def generate_recipes(stack: str, existing: dict | None = None) -> dict:
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


def list_targets(recipes: dict) -> list:
    rows = []
    for name in sorted(INTERFACE):
        entry = recipes.get("targets", {}).get(name, {}) if recipes else {}
        status = "implemented" if (entry.get("implemented") and entry.get("command")) else "stub"
        rows.append({"target": name, "status": status,
                     "desc": INTERFACE[name]["desc"],
                     "command": entry.get("command")})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grimoire build-recipe dispatcher.")
    ap.add_argument("target", nargs="?", help="one of: " + ", ".join(sorted(INTERFACE)))
    ap.add_argument("--port"); ap.add_argument("--env"); ap.add_argument("--filter")
    ap.add_argument("--fixture"); ap.add_argument("--watch", action="store_true")
    # v2 (web-app deployment protocol): package params.
    ap.add_argument("--version", dest="version", help="release version for `package`")
    # NB: distinct dest from the positional `target` (the verb) — sharing `dest`
    # made `recipe.py package` leak the verb into the `package` ${target} param.
    ap.add_argument("--target", dest="pkg_target", help="target triple/platform for `package`")
    # v3 (dependency channel): sync-deps mode + vendor-check scope.
    ap.add_argument("--mode", dest="mode",
                    help="sync-deps mode flag, e.g. --check / --update / --offline")
    ap.add_argument("--full", dest="full", action="store_true",
                    help="vendor-check whole-vendor audit (default is diff-scoped)")
    # #438: seed-specific flags (the generic fixtures/ engine, not a versioned
    # INTERFACE param — same "CLI-only meta-flag" class as --dry-run/--list).
    ap.add_argument("--if-empty", action="store_true",
                    help="seed: apply a fixture set only when its declared "
                        "empty-check reports the datastore is empty")
    ap.add_argument("--allow-non-dev", action="store_true",
                    help="seed: explicit override to seed a non-dev env "
                        "(seeding is fail-closed outside dev by default)")
    ap.add_argument("--fixtures-root", default=FIXTURES_ROOT_DEFAULT,
                    help="seed: path to the fixtures/ directory (default: fixtures)")
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
        # Resolve a dispatcher alias (e.g. `run` → `server`) to its canonical
        # INTERFACE target before dispatch, so `recipe.py run` ≡ `recipe.py server`
        # ≡ `just run`.
        verb = resolve_alias(args.target)
        # #438: `seed` is special-cased to the generic fixtures/ engine —
        # bypasses .claude/recipes.json entirely (see module docstring).
        if verb == "seed":
            return cmd_seed(fixture=args.fixture or None, env=args.env or "dev",
                            fixtures_root=args.fixtures_root,
                            if_empty=args.if_empty, allow_non_dev=args.allow_non_dev,
                            dry_run=args.dry_run)
        recipes = load_recipes(args.recipes)
        cli_args = {"port": args.port, "env": args.env, "filter": args.filter,
                    "fixture": args.fixture, "watch": args.watch,
                    "version": args.version, "target": args.pkg_target,
                    "mode": args.mode, "full": args.full,
                    "_recipes_path": args.recipes}
        command, params = build_command(verb, recipes, cli_args, os.environ)
    except RecipeError as e:
        print("recipe: %s" % e, file=sys.stderr)
        return 2

    if verb == "server" and "port" in params:
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
    if INTERFACE_VERSION != 9:
        failures.append("interface version should be 9 (#362 gui-test target added)")
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
    # v5: release is an interface target. v7 (v3.90): EVERY stack pre-fills it
    # as a `just release` stub — a repo with no release ceremony never reaches a
    # clean dev==main boundary (the autonomous-sync precondition, BMI-3).
    if "release" not in INTERFACE:
        failures.append("interface should define 'release' target")
    if g["targets"]["release"]["command"] is None or g["targets"]["release"]["implemented"]:
        failures.append("generate(server): release should be a pre-filled unimplemented stub")
    for _stack in STACK_PRESETS:
        if STACK_PRESETS[_stack].get("release") != "just release":
            failures.append("preset %s must pre-fill release as 'just release'" % _stack)

    # #442: `build` now declares an `env` param, and every stack preset's build
    # command carries ${env} — so `recipe.py build --env prod` actually
    # resolves instead of silently no-opping the substitution.
    if "env" not in INTERFACE["build"]["params"]:
        failures.append("interface: 'build' target must declare an 'env' param (#442)")
    for _stack, _cmds in STACK_PRESETS.items():
        if "${env}" not in _cmds.get("build", ""):
            failures.append("preset %s.build must carry ${env} (#442)" % _stack)
    gb = generate_recipes("server")
    if "env" not in (gb["targets"]["build"].get("params") or {}):
        failures.append("generate(server): build stub should declare an env param (#442)")

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
    # web stack release stub: pre-filled (not null) but unimplemented.
    if gw["targets"]["release"]["command"] is None or gw["targets"]["release"]["implemented"]:
        failures.append("generate(web): release should be a pre-filled unimplemented stub")
    # cli stack: smoke is absent from the preset → generate() stubs command: null.
    # release (v7) is pre-filled on every stack, cli included.
    gc = generate_recipes("cli")
    if gc["targets"]["smoke"]["command"] is not None or gc["targets"]["smoke"]["implemented"]:
        failures.append("generate(cli): smoke should be command:null unimplemented stub")
    if gc["targets"]["release"]["command"] is None or gc["targets"]["release"]["implemented"]:
        failures.append("generate(cli): release should be a pre-filled unimplemented stub")
    # native stack (v7): web's shape minus deploy — package/release/smoke/stop
    # pre-filled as stubs, deploy command: null (nothing to deploy to an env).
    gn = generate_recipes("native")
    if gn["targets"]["deploy"]["command"] is not None or gn["targets"]["deploy"]["implemented"]:
        failures.append("generate(native): deploy should be command:null unimplemented stub")
    for _t in ("package", "release", "smoke", "stop"):
        if gn["targets"][_t]["command"] is None or gn["targets"][_t]["implemented"]:
            failures.append("generate(native): %s should be a pre-filled unimplemented stub" % _t)
    if "${version}" not in (gn["targets"]["package"]["command"] or ""):
        failures.append("generate(native): package stub should carry ${version}")
    # v6: stop is an interface target; web/server stacks pre-fill a stub carrying
    # ${port}, cli/library stub it command: null (nothing runnable to stop).
    if "stop" not in INTERFACE:
        failures.append("interface should define 'stop' target")
    if g["targets"]["stop"]["command"] is None or g["targets"]["stop"]["implemented"]:
        failures.append("generate(server): stop should be a pre-filled unimplemented stub")
    if "${port}" not in (g["targets"]["stop"]["command"] or ""):
        failures.append("generate: stop stub should carry ${port}")
    if gw["targets"]["stop"]["command"] is None or gw["targets"]["stop"]["implemented"]:
        failures.append("generate(web): stop should be a pre-filled unimplemented stub")
    if gc["targets"]["stop"]["command"] is not None or gc["targets"]["stop"]["implemented"]:
        failures.append("generate(cli): stop should be command:null unimplemented stub")

    # v8 (#360): unit-test is an interface target pre-filled as a stub on
    # EVERY stack (universal vocabulary, like `test`) — routes to
    # `just unit-test`, unimplemented until the project wires the real
    # per-stack command.
    if "unit-test" not in INTERFACE:
        failures.append("interface should define 'unit-test' target")
    for _stack_name in STACK_PRESETS:
        _g = generate_recipes(_stack_name)
        if _g["targets"]["unit-test"]["command"] != "just unit-test":
            failures.append("generate(%s): unit-test stub should be 'just unit-test'"
                            % _stack_name)
        if _g["targets"]["unit-test"]["implemented"]:
            failures.append("generate(%s): unit-test should be an unimplemented stub"
                            % _stack_name)
    if set(INTERFACE["unit-test"]["params"]) != {"filter", "watch"}:
        failures.append("interface: 'unit-test' should declare filter/watch "
                        "params, mirroring 'test' (#360)")

    # v9 (#362): gui-test is a GUI-only interface target — pre-filled as a
    # stub ONLY on stacks with an actual GUI surface (web, native), like
    # smoke; server/cli/library have no GUI to test and leave it absent so
    # generate() stubs it command: null (exit 2 — advisory, never a no-op).
    if "gui-test" not in INTERFACE:
        failures.append("interface should define 'gui-test' target")
    if set(INTERFACE["gui-test"]["params"]) != {"baseline"}:
        failures.append("interface: 'gui-test' should declare a 'baseline' param (#362)")
    if gw["targets"]["gui-test"]["command"] is None or gw["targets"]["gui-test"]["implemented"]:
        failures.append("generate(web): gui-test should be a pre-filled unimplemented stub")
    if "${baseline}" not in (gw["targets"]["gui-test"]["command"] or ""):
        failures.append("generate(web): gui-test stub should carry ${baseline}")
    if gn["targets"]["gui-test"]["command"] is None or gn["targets"]["gui-test"]["implemented"]:
        failures.append("generate(native): gui-test should be a pre-filled unimplemented stub")
    if gc["targets"]["gui-test"]["command"] is not None or gc["targets"]["gui-test"]["implemented"]:
        failures.append("generate(cli): gui-test should be command:null unimplemented stub")
    if g["targets"]["gui-test"]["command"] is not None or g["targets"]["gui-test"]["implemented"]:
        failures.append("generate(server): gui-test should be command:null unimplemented stub "
                        "(a backend service has no GUI surface)")

    # build a concrete recipes dict and exercise resolution + substitution.
    recipes = {"targets": {
        # #442: build now resolves ${env} exactly like server/seed/migrate.
        "build": {"command": "just build ${env}", "implemented": True,
                  "params": {"env": {"default": "dev"}}},
        "server": {"command": "serve --port ${port} --env ${env}", "implemented": True,
                   "params": {"port": {"default": "3000"}, "env": {"default": "dev"}}},
        "test": {"command": "pytest ${filter}", "implemented": True,
                 "params": {"filter": {"default": ""}}},
        # v8 (#360): an implemented unit-test resolves ${filter} exactly like
        # test — the fast-subset filter flag is the same mark expression.
        "unit-test": {"command": "pytest -m \"not slow and not integration\" ${filter}",
                      "implemented": True,
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
        # v6: an implemented stop resolves ${port} exactly like server/smoke;
        # release is left an unimplemented stub (must fail loud, exit-2).
        "stop": {"command": "scripts/stop.sh ${port}", "implemented": True,
                 "params": {"port": {"default": ""}}},
        "release": {"command": None, "implemented": False},
        # v9 (#362): an implemented gui-test resolves ${baseline} exactly like
        # smoke resolves ${port}.
        "gui-test": {"command": "gui-app --gui-test ${baseline}", "implemented": True,
                     "params": {"baseline": {"default": "main"}}},
    }}

    # #442: build --env substitution — CLI > env var > recipe default; ${env}
    # actually resolves now instead of silently no-opping (build had no
    # params at all before this fix, so --env was accepted but discarded).
    cmd, p = build_command("build", recipes, {"env": "prod", "_recipes_path": "x"}, {})
    if cmd != "just build prod":
        failures.append("build --env prod not honored (#442): %r" % cmd)
    cmd, p = build_command("build", recipes, {"_recipes_path": "x"},
                           {"GRIMOIRE_APP_ENV": "staging"})
    if cmd != "just build dev":
        failures.append("build should ignore an unrelated env var and use the "
                        "recipe default (no env-var binding declared): %r" % cmd)
    cmd, p = build_command("build", recipes, {"_recipes_path": "x"}, {})
    if cmd != "just build dev":
        failures.append("build recipe-default env not honored (#442): %r" % cmd)

    # #442: substitute() fails loud on any ${...} token left unresolved after
    # substitution, instead of silently running the command with the literal
    # placeholder still embedded (the root cause: build's ${env} used to have
    # no matching param, so it was never replaced and the shell saw the raw
    # `${env}` token — either erroring oddly or resolving empty, either way a
    # silent-success debug build slipped through the release ceremony).
    try:
        substitute("just build ${env} --target ${untracked}", {"env": "prod"})
        failures.append("substitute() should raise on an unresolved ${...} token (#442)")
    except RecipeError as e:
        if "untracked" not in str(e):
            failures.append("substitute() error should name the unresolved token: %r" % e)
    # every param provided and consumed — no leftover token, no raise.
    if substitute("just build ${env}", {"env": "prod"}) != "just build prod":
        failures.append("substitute() should still succeed when every token resolves")

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

    # v6: stop port substitution — CLI > $GRIMOIRE_APP_PORT > recipe default (empty).
    cmd, p = build_command("stop", recipes,
                           {"port": "8420", "_recipes_path": "x"}, {})
    if cmd != "scripts/stop.sh 8420":
        failures.append("stop CLI port not honored: %r" % cmd)
    cmd, p = build_command("stop", recipes, {"_recipes_path": "x"},
                           {"GRIMOIRE_APP_PORT": "20003"})
    if cmd != "scripts/stop.sh 20003":
        failures.append("stop env port not honored: %r" % cmd)
    cmd, p = build_command("stop", recipes, {"_recipes_path": "x"}, {})
    if cmd != "scripts/stop.sh ":
        failures.append("stop recipe-default (empty) port not honored: %r" % cmd)

    # v8 (#360): unit-test filter substitution — CLI value flows through
    # ${filter} exactly like `test`.
    cmd, p = build_command("unit-test", recipes,
                           {"filter": "-k fast", "_recipes_path": "x"}, {})
    if cmd != 'pytest -m "not slow and not integration" -k fast':
        failures.append("unit-test filter substitution failed: %r" % cmd)
    cmd, p = build_command("unit-test", recipes, {"_recipes_path": "x"}, {})
    if cmd != 'pytest -m "not slow and not integration" ':
        failures.append("unit-test default (empty) filter substitution failed: %r" % cmd)
    # unimplemented unit-test fails loud, exit-2 dispatch contract (never a
    # silent no-op — same as every other target).
    _unimpl_recipes = {"targets": dict(recipes["targets"],
                                       **{"unit-test": {"command": None, "implemented": False}})}
    try:
        build_command("unit-test", _unimpl_recipes, {"_recipes_path": "x"}, {})
        failures.append("unimplemented unit-test should raise")
    except RecipeError:
        pass

    # v9 (#362): gui-test baseline substitution — CLI value flows through
    # ${baseline} exactly like unit-test's ${filter}; recipe default is "main".
    cmd, p = build_command("gui-test", recipes,
                           {"baseline": "settings-panel", "_recipes_path": "x"}, {})
    if cmd != "gui-app --gui-test settings-panel":
        failures.append("gui-test baseline substitution failed: %r" % cmd)
    cmd, p = build_command("gui-test", recipes, {"_recipes_path": "x"}, {})
    if cmd != "gui-app --gui-test main":
        failures.append("gui-test default (main) baseline substitution failed: %r" % cmd)
    # unimplemented gui-test fails loud, exit-2 dispatch contract (never a
    # silent no-op — same as every other target, e.g. on a non-GUI project).
    _unimpl_gui_recipes = {"targets": dict(recipes["targets"],
                                           **{"gui-test": {"command": None, "implemented": False}})}
    try:
        build_command("gui-test", _unimpl_gui_recipes, {"_recipes_path": "x"}, {})
        failures.append("unimplemented gui-test should raise")
    except RecipeError:
        pass

    # unimplemented release fails loud (never silent no-op — like every target).
    try:
        build_command("release", recipes, {"_recipes_path": "x"}, {})
        failures.append("unimplemented release should raise")
    except RecipeError:
        pass

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

    # RSS-3 (#321): the `run` alias resolves to the canonical `server` target.
    if resolve_alias("run") != "server":
        failures.append("resolve_alias('run') should be 'server'")
    if resolve_alias("server") != "server":
        failures.append("resolve_alias('server') should be identity")
    if resolve_alias("build") != "build":
        failures.append("resolve_alias('build') should be identity (no alias)")
    # `recipe.py run` dispatches the same recipe entry as `recipe.py server`.
    cmd_run, _ = build_command(resolve_alias("run"), recipes,
                               {"port": "8420", "_recipes_path": "x"}, {})
    if cmd_run != "serve --port 8420 --env dev":
        failures.append("run alias should resolve the server recipe: %r" % cmd_run)
    # every preset's `server` command routes to `just run` (canonical justfile name).
    for stack in ("server", "web"):
        srv = generate_recipes(stack)["targets"]["server"]["command"] or ""
        if not srv.startswith("just run"):
            failures.append("generate(%s): server should route to `just run`, got %r"
                            % (stack, srv))

    # dry-run via main(): prints the resolved command, exits 0, runs nothing.
    with tempfile.TemporaryDirectory() as d:
        rp = os.path.join(d, "recipes.json")
        with open(rp, "w") as fh:
            json.dump(recipes, fh)
        rc = main(["test", "--filter", "-k smoke", "--dry-run", "--recipes", rp])
        if rc != 0:
            failures.append("dry-run should exit 0")
        # the `run` alias dispatches via main() (dry-run) exactly like `server`.
        rc = main(["run", "--port", "8420", "--dry-run", "--recipes", rp])
        if rc != 0:
            failures.append("run alias dry-run should exit 0")
        # v6: stop dispatches via main() (dry-run) exactly like server/smoke.
        rc = main(["stop", "--port", "8420", "--dry-run", "--recipes", rp])
        if rc != 0:
            failures.append("stop dry-run should exit 0")
        # the positional verb must NOT leak into the package ${target} param
        # (argparse dest collision regression). `recipe.py package` → recipe
        # defaults, and `--target <triple>` threads through cleanly.
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["package", "--dry-run", "--recipes", rp])
        out = buf.getvalue().strip()
        if rc != 0 or out != "just package --version 0.0.0 --target host":
            failures.append("package verb leaked into ${target}: %r" % out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["package", "--target", "aarch64-apple-darwin", "--dry-run", "--recipes", rp])
        if buf.getvalue().strip() != "just package --version 0.0.0 --target aarch64-apple-darwin":
            failures.append("package --target not threaded: %r" % buf.getvalue().strip())
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

    # v7 (#329): `just` binds recipe args positionally, so a `key=${...}` token
    # in a preset command silently misassigns params to the wrong slot. No
    # generated preset may carry that pattern (positional-only, like deploy).
    import re
    key_eq_token = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*=\$\{")
    for stack_name, stack_cmds in STACK_PRESETS.items():
        for target_name, cmd in stack_cmds.items():
            if key_eq_token.search(cmd):
                failures.append(
                    "preset %s.%s uses a named key=${...} arg, should be "
                    "positional: %r" % (stack_name, target_name, cmd))

    # --- #438: fixtures/ convention — generic seed-dispatch engine. ---------
    with tempfile.TemporaryDirectory() as d:
        fixtures_root = Path(d) / "fixtures"
        set_dir = fixtures_root / "core"
        set_dir.mkdir(parents=True)
        target = Path(d) / "target.txt"
        # truncate-and-load: file 1 overwrites (truncates), file 2 appends the
        # real payload — idempotent because every apply starts from a wipe.
        (set_dir / "001_truncate.sql").write_text("TRUNCATE\n", encoding="utf-8")
        (set_dir / "002_load.sql").write_text("LOADED-ROW\n", encoding="utf-8")
        # 001 overwrites target, 002 appends to it — so after N applies the
        # target always ends at exactly "TRUNCATE\nLOADED-ROW\n".
        manifest = {
            "family": "sql", "strategy": "truncate-and-load",
            "apply": (
                "bash -c 'f=\"{file}\"; if [[ \"$f\" == *001_truncate.sql ]]; then "
                "cp \"$f\" \"%s\"; else cat \"$f\" >> \"%s\"; fi'" % (target, target)),
        }
        (set_dir / FIXTURE_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

        # manifest loading + file auto-discovery (files omitted from manifest).
        loaded = load_fixture_manifest(set_dir)
        if loaded["files"] != ["001_truncate.sql", "002_load.sql"]:
            failures.append("load_fixture_manifest: file auto-discovery failed: %r"
                            % loaded["files"])

        # apply, twice — idempotent truncate-and-load ends at the same state.
        apply_fixture_set(fixtures_root, "core", "dev")
        first_state = target.read_text(encoding="utf-8")
        apply_fixture_set(fixtures_root, "core", "dev")
        second_state = target.read_text(encoding="utf-8")
        if first_state != "TRUNCATE\nLOADED-ROW\n":
            failures.append("apply_fixture_set: unexpected state after first apply: %r"
                            % first_state)
        if first_state != second_state:
            failures.append(
                "apply_fixture_set: truncate-and-load should be idempotent, got "
                "%r then %r" % (first_state, second_state))

        # unknown fixture set raises.
        try:
            apply_fixture_set(fixtures_root, "nope", "dev")
            failures.append("apply_fixture_set: unknown set should raise")
        except FixtureError:
            pass

        # malformed manifest (bad family) raises with a clear message.
        bad_dir = fixtures_root / "bad"
        bad_dir.mkdir()
        (bad_dir / FIXTURE_MANIFEST_NAME).write_text(
            json.dumps({"family": "xml", "strategy": "upsert", "apply": "echo {file}"}),
            encoding="utf-8")
        try:
            load_fixture_manifest(bad_dir)
            failures.append("load_fixture_manifest: bad family should raise")
        except FixtureError as e:
            if "family" not in str(e):
                failures.append("load_fixture_manifest: error should name 'family': %r" % e)

        # cmd_seed: fail-closed outside dev, override lifts it.
        rc = cmd_seed(fixture="core", env="production", fixtures_root=str(fixtures_root),
                     if_empty=False, allow_non_dev=False, dry_run=True)
        if rc == 0:
            failures.append("cmd_seed: production without --allow-non-dev should refuse")
        if target.exists():
            target.unlink()
        rc = cmd_seed(fixture="core", env="production", fixtures_root=str(fixtures_root),
                     if_empty=False, allow_non_dev=True, dry_run=False)
        if rc != 0 or not target.exists():
            failures.append("cmd_seed: production WITH --allow-non-dev should seed")

        # cmd_seed: dry-run never executes (target untouched by a dry-run call
        # against a fresh set).
        target.unlink()
        rc = cmd_seed(fixture="core", env="dev", fixtures_root=str(fixtures_root),
                     if_empty=False, allow_non_dev=False, dry_run=True)
        if rc != 0:
            failures.append("cmd_seed: dev dry-run should exit 0")
        if target.exists():
            failures.append("cmd_seed: dry-run must not execute apply commands")

        # cmd_seed: missing fixtures/ directory fails loud (never a silent no-op).
        rc = cmd_seed(fixture=None, env="dev", fixtures_root=str(Path(d) / "absent"),
                     if_empty=False, allow_non_dev=False, dry_run=True)
        if rc == 0:
            failures.append("cmd_seed: absent fixtures/ directory should fail loud")

        # --if-empty: an empty-check that reports "not empty" (nonzero) skips
        # the set without applying; a check reporting "empty" (exit 0) applies.
        set_dir2 = fixtures_root / "gated"
        set_dir2.mkdir()
        target2 = Path(d) / "target2.txt"
        manifest2 = {
            "family": "json", "strategy": "upsert",
            "apply": "bash -c 'echo seeded >> \"%s\"' {file}" % target2,
            "empty-check": "false",
        }
        (set_dir2 / FIXTURE_MANIFEST_NAME).write_text(json.dumps(manifest2), encoding="utf-8")
        (set_dir2 / "001.json").write_text("{}", encoding="utf-8")
        # regression: a "json" family's default glob (*.json) must exclude
        # manifest.json itself, or it gets misapplied as a fixture file.
        gated_files = load_fixture_manifest(set_dir2)["files"]
        if gated_files != ["001.json"]:
            failures.append(
                "load_fixture_manifest: json-family auto-discovery must exclude "
                "manifest.json, got %r" % gated_files)
        rc = cmd_seed(fixture="gated", env="dev", fixtures_root=str(fixtures_root),
                     if_empty=True, allow_non_dev=False, dry_run=False)
        if rc != 0 or target2.exists():
            failures.append("cmd_seed: --if-empty with a non-empty check should skip, "
                            "not apply")
        manifest2["empty-check"] = "true"
        (set_dir2 / FIXTURE_MANIFEST_NAME).write_text(json.dumps(manifest2), encoding="utf-8")
        rc = cmd_seed(fixture="gated", env="dev", fixtures_root=str(fixtures_root),
                     if_empty=True, allow_non_dev=False, dry_run=False)
        if rc != 0 or not target2.exists():
            failures.append("cmd_seed: --if-empty with an empty check should apply")

        # end-to-end via main(): `recipe.py seed` dispatches to cmd_seed and
        # never touches .claude/recipes.json (no recipes.json exists in `d`).
        # `target` was left absent by the preceding dry-run assertion.
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["seed", "--fixture", "core", "--env", "dev",
                     "--fixtures-root", str(fixtures_root)])
        if rc != 0 or not target.exists():
            failures.append("main(): `recipe.py seed` should dispatch to cmd_seed "
                            "and apply (rc=%r): %s" % (rc, buf.getvalue()))

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("recipe self-test: OK (generate/stub/preserve, param resolution "
          "CLI>env>recipe>spec, ${} substitution + unresolved-token guard (#442), "
          "unimplemented+unknown raise, dry-run, determinism, interface-v9 "
          "build env param + deploy/package + sync-deps/vendor-check + smoke + "
          "release + stop + unit-test (#360) + gui-test (#362, GUI-only "
          "web/native) stubs + loud exit-2, run→server "
          "alias, presets route to `just <recipe>`, fixtures/ generic seed "
          "engine (#438): manifest load/validate, idempotent apply, "
          "fail-closed non-dev refusal, --if-empty gating, dry-run, "
          "main() dispatch)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
