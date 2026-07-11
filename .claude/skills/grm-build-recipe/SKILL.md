---
name: grm-build-recipe
description: Shared named-target interface for driving any Grimoire project's build/run/data operations under stable names, regardless of stack — a skill or agent calls the dispatcher `recipe.py <target>` (build / server / stop / test / seed / migrate / lint / clean) and the correct project command runs. The per-project implementation lives in `.claude/recipes.json`. Use when running a recipe target or generating the recipe file.
---

# Build-recipe interface (BR1)

Every Grimoire project expresses build/run/data commands as raw, stack-specific
shell strings — not addressable by other skills under a stable name. The
**build-recipe interface** fixes that: a caller says `recipe.py server --port
8420` and the correct project command runs, **without knowing what it is**. This
is the stable call surface every skill uses — they invoke the dispatcher, never
the raw command.

Design authority: `docs/grimoire/design/build-recipe-interface-design.md`. Prefer this
dispatcher over re-deriving project commands (scripting-unification #75).

> **Preferred interface — the `grimoire-recipe` MCP server (v3.28).** Listing
> targets, resolving commands, and executing recipes are now deterministic tools.
> When `mcp.enabled` and the server is registered (root `.mcp.json`), prefer its
> native tools instead of shelling out: **`list_targets`** (interface vocabulary
> + per-project implementation status), **`dry_run`** (resolve the command for a
> target without executing — returns `{target, command, params}`), **`run_recipe`**
> (execute a target — returns structured `{target, exit_code, ok, stdout, stderr}`
> instead of free-form subprocess output). Recipes remain project-defined in
> `.claude/recipes.json`; the server adds no new execution authority. **CLI
> fallback** (no MCP / disabled): `python3 .claude/skills/grm-build-recipe/recipe.py
> <target> [--dry-run] [--list]` — identical engine. The Steps below are the
> fallback procedure (and the conceptual model the tools implement).

## The interface (versioned in Grimoire source)

The canonical target vocabulary lives in `recipe.py` (`INTERFACE`,
`INTERFACE_VERSION`) — extendable without changing callers:

| Target | Does | Standard params |
|---|---|---|
| `build` | compile / assemble | — |
| `server` (justfile `run`) | start the app server | `--port` (defaults to `$GRIMOIRE_APP_PORT`, #77), `--env` |
| `test` | run the test suite | `--filter`, `--watch` |
| `seed` | populate a local data store | `--fixture`, `--env` |
| `migrate` | run pending migrations | `--env` |
| `lint` | static analysis / formatting | — |
| `clean` | remove build artifacts | — |
| `package` | assemble a versioned, deployable release bundle (v2) | `--version`, `--target` |
| `deploy` | install / self-update a deployed instance from a bundle (v2) | `--env` (defaults `prod`) |
| `sync-deps` | reconcile / vendor first-party deps from a release channel (v3) | `--mode` (`--check`/`--update`/`--offline`) |
| `vendor-check` | dependency-channel conformance gate, exit 0/nonzero (v3) | `--full` (whole-vendor audit) |
| `smoke` | Boot app and verify entry page + critical assets return 2xx with correct content-type. Exit 2 when unimplemented. (v4) | `--port` (defaults to `$GRIMOIRE_APP_PORT`) |
| `release` | changelog-derived release ceremony (bump/test/build/tag + milestone reconciliation) (v5) | — (no args) |
| `stop` | kill running instance(s) of this project's process (v6) | `--port` (defaults to `$GRIMOIRE_APP_PORT`) |

> **`run` ↔ `server` (RSS-3, #321).** `run` is the canonical **justfile** recipe
> name; `server` is the versioned INTERFACE target, kept as a permanent
> **dispatcher alias** (`ALIASES = {"run": "server"}`) — `recipe.py run` ≡
> `recipe.py server`, both resolving to the entry whose command is `just run …`.
> A pure alias: **no new target, `INTERFACE_VERSION` unchanged**;
> `.claude/recipes.json` keeps the historical `server` key.

**Interface version (`INTERFACE_VERSION`) is `6`** — v6 added `stop` (kill running
instance(s) of this project's process, RSS-4 #322); v5 added the changelog-derived
`release` ceremony (recipe layer phase 2, issue #201 §4); v4 added the runtime
verification gate `smoke` (boot app + curl entry page + critical assets, assert
2xx + correct content-type; full spec: `docs/grimoire/design/runtime-verification-design.md`).
v3 added the dependency-channel consume-side targets `grm-sync-deps` + `vendor-check`
(v2 added the web-app deployment-protocol targets `package` + `deploy`). `package`
is the producer of the deployable bundle, `release.json` manifest, and
`grimoire-build-info.json` stamp; `deploy` drives the install / self-update
path; the protocol those two serve is `docs/web-app-deployment-protocol.md`
(§1/§2/§8 for `package`, §3/§6 for `deploy`). `grm-sync-deps` reconciles/vendors
first-party deps from a release channel (resolve → download → verify sha256 →
atomic-replace → write `vendor.lock`); `vendor-check` is the conformance gate
(exit 0 = conformant, nonzero = violation); the substrate those two serve is
`docs/grimoire/design/dependency-channel-design.md` (§4 for `grm-sync-deps`, §5 for
`vendor-check`, §6 for the scaffold defaults). `release` derives the version from
the newest changelog heading, guards + bumps + tests + builds + tags, and folds
the matching `milestone:v{X.Y}` issues into the release notes (issue #201 §4); its
reference implementation is `scripts/release.sh`. `stop` kills the process
`run` started — resolution order `--port` → `$GRIMOIRE_APP_PORT` → the pidfile
`run` wrote (`$GRIMOIRE_RUN_PIDFILE`) → a declared process pattern
(`$GRIMOIRE_APP_PATTERN`); idempotent, only kills identified processes; ref
impl `scripts/stop.sh` (generic, like `sync-deps`/`vendor-check`); full spec:
`docs/design/justfile-standard-design.md` §2.3. The bump is **extend-only**:
`grm-sync-from-upstream` adds new targets as **stubs** to existing projects and
never overwrites an implemented target. `grm-sync-deps`/`vendor-check` are universal
(every stack); `package`/`deploy`/`smoke`/`release`/`stop` are web-app-shape
(non-web stacks stub them).
Like every target, they **fail loud (exit 2)** when a project calls them without
implementing them — never a silent no-op.

**Parameter resolution (highest wins):** CLI flag → env var → recipe default →
interface default. So `recipe.py server` with no `--port` uses
`$GRIMOIRE_APP_PORT` when the worktree has claimed one, else the recipe's
default.

## Calling a target

```
python3 .claude/skills/grm-build-recipe/recipe.py test --filter smoke
python3 .claude/skills/grm-build-recipe/recipe.py server --port 8420 --env dev
python3 .claude/skills/grm-build-recipe/recipe.py build --dry-run   # print, don't run
python3 .claude/skills/grm-build-recipe/recipe.py --list            # targets + status
```

- **Exit code:** the child command's exit code passes straight through (0 =
  success). `--dry-run` prints the resolved command and exits 0.
- **Unimplemented target → fail loud (exit 2), never a silent no-op.** If a
  project hasn't implemented a target, the dispatcher says so and points at
  `--generate`. Callers can rely on "exit 0 means it actually ran."
- The `server` target prints its resolved port to stderr so callers
  (`grm-agent-environment-manager`, the port layer) can surface the URL.

## The recipe file — `.claude/recipes.json`

Per-project implementation: `target → { command, implemented, params }`. The
`command` is a template with `${port}` / `${env}` / `${filter}` placeholders the
dispatcher substitutes. It is **readable by agents without executing**, executed
**only** through the dispatcher, and **synced extend-only** — `grm-sync-from-upstream`
adds stubs for new interface targets but **never overwrites** an implemented one.

## Generating / stubbing recipes

```
python3 .claude/skills/grm-build-recipe/recipe.py --generate server   # or web / cli / library
```

The **`web`** stack additionally pre-fills `package` + `deploy` stubs (the
deployment-protocol targets); `server`/`cli`/`library` leave them as
unimplemented stubs so a non-web project that calls them gets the loud exit-2.

Generation pre-fills inferrable targets for the stack as **stubs**
(`implemented: false`, `TODO` command), preserves any already-implemented
target, and stubs every remaining interface target. A human/agent then fills the
real command and flips `implemented: true`. Generation runs at
`grm-workflow-bootstrap` (initial, from the declared stack), at `grm-sync-from-upstream`
(stub newly-added interface targets), and on demand here.

## How other skills use it

- **environment-manager:** launch via `recipe server` (reads the claimed
  port), kill a running instance via `recipe stop` (RSS-4, #322 — still
  per-action authorized before invoking it).
- **Port isolation:** `recipe server` consumes `$GRIMOIRE_APP_PORT` — the
  injection point for the per-worktree port.
- **QA agent:** drive verification builds/tests via `recipe build` /
  `recipe test` instead of constructing stack-specific commands.
- **CLAUDE.md commands table:** `grm-workflow-bootstrap` writes
  `recipe <target>` invocations there instead of raw project strings.

## Constraints

- **Delegates, never replaces** make / npm scripts / task runners — the recipe
  command *calls* them.
- **Never silently no-ops** — an unimplemented or unknown target fails with a
  clear message.
- **Single-target dispatch only** — pipelines (build-then-seed) are the caller's
  responsibility, not the dispatcher's.
- **Read the recipe without executing** — agents inspect `.claude/recipes.json`
  or run `--list` / `--dry-run` to know what *would* run.

## Anti-patterns

- Inspecting and running a project's raw build string instead of `recipe build`.
- Treating a missing target as "nothing to do" (it is an error — fail loud).
- Overwriting an implemented recipe on sync (sync is extend-only).
- Hardcoding a port in the `server` command instead of `${port}` / `$GRIMOIRE_APP_PORT`.

## Reference (load on demand)

- `The `recipes.json` → `just` routing convention (all targets, RSS-3 #321)` — see `reference.md`
