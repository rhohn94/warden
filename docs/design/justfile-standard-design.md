# Justfile Standard Design

> **Status:** Accepted — v3.53; extended to the full recipe vocabulary in
> v3.78; `stop` added in v3.78 (interface v6).
> **Related:** `grm-install-doctor`, `grm-workflow-bootstrap`,
> `grm-sync-from-upstream`, and the build-recipe interface (`grm-build-recipe`
> — the versioned dispatcher this standard is the justfile face of; spec
> `build-recipe-interface-design.md` is framework-internal).

---

## 1. Motivation

Grimoire's core promise is **standardization**: walk into any Grimoire app and
know exactly how to build, run, stop, deploy, and release it. Every agent and
every CI pipeline must drive the project by invoking a small, stable set of
named commands — **without reading project-specific documentation first**.

Without a standard, each project names these operations differently (`make build`,
`./scripts/start.sh`, `npm run dev`, …) — an agent bootstrapping a new task
worktree must reverse-engineer the convention first, and a CI template must
hard-code project-specific commands. Both introduce friction and error surface.

The **justfile is the de-facto recipe layer**: every project ships
a root `justfile` that exposes the **full build-recipe vocabulary** — the same
named targets the versioned build-recipe interface (`grm-build-recipe`) defines —
with fixed signatures. Agents call `just build` and it works; CI calls
`just deploy staging` and the semantics are contract, not guesswork. The
`recipe.py` dispatcher (and the `grimoire-recipe` MCP server) stay the stable
*dispatch surface*; the *implementation* lives in the justfile, whose thin
recipes delegate multi-line logic to `scripts/`. Each project's
`.claude/recipes.json` is a **thin routing table** whose implemented entries all
read `just <recipe> …`, so `recipe.py <t>` ≡ `just <t>` for every implemented
target.

The secondary benefit is **detectability**: because recipes follow a prescribed
shape — including a specific placeholder comment — `grm-install-doctor` can
audit coverage mechanically (MISSING/PARTIAL/OK per recipe) and distinguish a
real implementation from an unfinished stub, without fragile echo-text matching.

---

## Scope

**In scope:** the **full recipe vocabulary** + fixed argument signatures
(`build run stop test seed migrate lint clean package deploy smoke release`,
plus `sync-deps`/`vendor-check` delegating to framework scripts), the canonical
`run` name (`server` as dispatcher alias — §2.1), the `# grimoire:placeholder`
convention, the `.claude/recipes.json` → `just` routing convention (§2.2), the
`stop` semantics + resolution order (§2.3), and the tooling that enforces or
adopts the contract (`grm-install-doctor`, `grm-workflow-bootstrap`,
`grm-sync-from-upstream`).

**Out of scope:** project-specific deploy pipelines; CI configuration; the
`db-up`/`db-down` optional dev-database recipes (not part of the interface
vocabulary); Copilot-flavor support. The vocabulary is **extensible** — new
targets are extend-only, never renamed or removed.

---

## 2. The recipe vocabulary

Every Grimoire project ships a root `justfile` exposing the **full build-recipe
vocabulary** — the same named targets the build-recipe interface
(`grm-build-recipe`) defines. Signatures are fixed by contract; parameter names
and defaults must be reproduced exactly so callers (agents, CI, `recipe.py`)
can invoke them without inspecting the file.

| Recipe | Signature | Status | Notes |
|---|---|---|---|
| `build` | `build env="dev"` | **core (required)** | Compile / assemble into `dist/`. |
| `run` | `run env="dev" port="3000"` | **core (required)** | Start the app. Canonical name — see §2.1. |
| `stop` | `stop port=""` | web/service-shape | Kill running instance(s) of this project's process. See §2.3. |
| `test` | `test filter="" watch=""` | recommended | Run the test suite. |
| `seed` | `seed fixture="" env="dev"` | recommended | Populate a local data store. |
| `migrate` | `migrate env="dev"` | recommended | Run pending schema / data migrations. |
| `lint` | `lint` | recommended | Static analysis / formatting. |
| `clean` | `clean` | recommended | Remove build artifacts. |
| `package` | `package version="" target=""` | web-app-shape | Assemble a versioned, deployable bundle. |
| `deploy` | `deploy env dry_run="false"` | **core (required)** | Push to a named environment. |
| `smoke` | `smoke port="3000"` | web-app-shape | Boot + probe the served surface for 2xx. |
| `release` | `release *ARGS` | web-app-shape | Changelog-derived release ceremony. |
| `sync-deps` | `sync-deps mode=""` | universal (delegates) | Reconcile / vendor first-party deps — delegates to the framework `grm-sync-deps` script. |
| `vendor-check` | `vendor-check full=""` | universal (delegates) | Dependency-channel conformance gate — delegates to the framework `grm-dependency-audit` script. |

The **core trio** (`build`, `run`, `deploy`) is required of every project (a
stack with legitimately none — e.g. a pure library or the framework itself —
declares them absent in `.claude/recipes.json`, `command:null`, downgrading to
advisory in `grm-install-doctor`; see §6). Every other target is present as a
**`# grimoire:placeholder` stub** until wired, so the justfile always documents
the whole vocabulary in one place. `stop` ships a real generic
reference implementation on web/service instead of a placeholder — see §2.3.

Each unimplemented recipe carries the `# grimoire:placeholder` marker **in its
body** (§3). The stub shape:

```just
build env="dev":
    # grimoire:placeholder
    @echo "TODO: replace with build command"
```

The `sync-deps` / `vendor-check` recipes are **not** stubs — real, thin recipes
delegating to the framework's own consume-side scripts (identical every project):

```just
sync-deps mode="":
    python3 .claude/skills/grm-sync-deps/sync_deps.py {{mode}}

vendor-check full="":
    python3 .claude/skills/grm-dependency-audit/dependency_channel_conformance.py --root . {{full}}
```

### Key decisions

| Recipe | Parameter | Rationale |
|---|---|---|
| `build` | `env` defaults to `"dev"` | Safe default — a local dev build always invokes without arguments. |
| `run` | `env="dev"`, `port="3000"` | `just run` with no arguments works; `3000` matches the build-recipe `server` default (the port layer's `$GRIMOIRE_APP_PORT`). |
| `deploy` | `env` is **positional / required** (no default) | Forces an explicit environment choice; a missing argument is a `just` error rather than a silent wrong-environment deploy. |
| `deploy` | `dry_run` defaults to `"false"` | Safety hatch — `dry_run=true` previews without mutating remote state. |

> **Why `just`?** Already the task runner Grimoire quick-start templates ship;
> cross-platform, first-class argument support, no build system required.

### 2.1 `run` is canonical; `server` is a dispatcher alias

The versioned build-recipe INTERFACE target is historically named `server`; the
canonical **justfile** recipe name is `run`. These are reconciled by a
**dispatcher alias**, not a rename: `recipe.py`'s `ALIASES = {"run": "server"}`
resolves `recipe.py run` and `recipe.py server` to the **same** entry, whose
command is `just run …` — a pure alias, **`INTERFACE_VERSION` not bumped**.
`.claude/recipes.json` keeps the historical `server` key (routes to `just run`),
so pre-existing `recipe.py server` callers keep working. Full rationale:
`build-recipe-interface-design.md` §run↔server reconciliation (framework-internal).

### 2.2 `.claude/recipes.json` is a thin routing table → `just`

Every **implemented** `.claude/recipes.json` entry's command is `just <recipe> …`,
threading the target's params as `${…}` placeholders (e.g. `"deploy": {"command":
"just deploy ${env}", "implemented": true}`) — `recipe.py deploy --env staging`
resolves to `just deploy staging` → `scripts/deploy.sh staging`. Callers use the
recipe target (or the `grimoire-recipe` MCP); the *implementation* lives in the
justfile, delegating multi-line logic to `scripts/`. This generalizes the
v3.68/v3.69 deploy-layer convention (`package`/`deploy`/`smoke`/`release`) to
**all** targets. Unimplemented targets stay `command:null` (or an
`implemented:false` routed stub) and **fail loud, exit 2** — never a silent no-op.

### 2.3 `stop` — kill running instance(s) of this project's process

`build`/`run`/`deploy` start and ship a project; nothing stopped one — agents
fell back to ad-hoc `lsof`/`pgrep`. `stop port="":` closes that gap (v6).
**Resolution order** (first stage identifying a live process wins): `port` arg
→ `$GRIMOIRE_APP_PORT` → the **pidfile** `run` writes (`$GRIMOIRE_RUN_PIDFILE`,
default `.grimoire-run.pid` — via
`nohup <entrypoint> & echo $! > "${GRIMOIRE_RUN_PIDFILE:-.grimoire-run.pid}"`)
→ a declared **process pattern** (`$GRIMOIRE_APP_PATTERN` / config's
`process_pattern`). Only kills **positively identified** processes — never
broad/ambiguous. **Idempotent**: nothing running is exit 0, never an error.

Unlike most recipes, `stop` is **generic/stack-agnostic** — like
`sync-deps`/`vendor-check` it never needs a project rewrite, so web/service ship
a real reference implementation (`scripts/stop.sh`), not a placeholder:

```just
stop port="":
    scripts/stop.sh {{port}}
```

gui/lib carry `stop` as a placeholder (uniformity only).
`grm-agent-environment-manager`'s kill path is wired to `recipe stop` / `just stop`
instead of hand-rolled inspection — per-action authorization still applies.

---

## 3. Placeholder Convention

An unimplemented recipe **must** include the comment `# grimoire:placeholder`
in its body. This marker is the **canonical signal** that a recipe has not yet
been wired to a real command.

```just
build env="dev":
    # grimoire:placeholder
    @echo "TODO: replace with build command"
```

### Why a comment, not an echo string

`grm-install-doctor` determines recipe status by scanning the `justfile` for
recipe presence and placeholder state. An echo string (`@echo "TODO: ..."`) is
fragile — it can be partially overwritten, kept as documentation, and
string-matching free-form text is not version-stable. The `# grimoire:placeholder`
comment is **invisible to end users**, **unambiguous**, and **grep-stable**.

### PARTIAL status

A recipe that exists but still contains `# grimoire:placeholder` in its body is
classified **PARTIAL** — present (callable without a `just` error) but not
implemented. Distinct from MISSING (absent) and OK (present, no marker).

### Removal rule

When a developer wires a real command into a recipe, they **must remove** the
`# grimoire:placeholder` line. Leaving it in place causes `grm-install-doctor`
to continue reporting PARTIAL even after implementation.

---

## 4. Dev-database recipes (`db-up` / `db-down`)

Beyond the interface vocabulary (§2), the `service`/`web` quick-start templates
ship two **dev-database** recipes — not part of the build-recipe interface, but
recommended when a project has a database dependency. Wire `test`/`run` to
depend on `db-up` so the database is always available:

```just
test filter="" watch="": db-up
    pytest tests/ {{filter}}
```

### `db-up` and `db-down`

These recipes manage a Dockerized development database. They are designed to be
**multi-worktree-safe**: Grimoire encourages one isolated git worktree per
agent/task, but all worktrees share a single dev database container (keyed by
`container_name`) to avoid port conflicts and volume proliferation.

Canonical implementation: `.claude/quick-start-templates/{service,web}/files/justfile`.
Copy the recipe verbatim — do not simplify to `docker compose up -d db`, which
fails when the container is already running under a sibling worktree's compose
project.

```just
db-up:
    #!/usr/bin/env bash
    set -euo pipefail
    # ... idempotent, multi-worktree-safe bring-up logic ...
    # See quick-start-templates for the full implementation.

db-down:
    docker compose stop db || true
```

> `db-down` **stops** the container but does not remove it or its volume — data
> persists across a cycle. Never run `docker compose down -v` from a task
> worktree; it destroys the shared volume.

---

## 5. Argument Convention

All argument names and defaults across Grimoire projects follow this table.
Consistent naming means agents can document a single invocation pattern and
use it across all projects.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `env` | string | `"dev"` (on `build`, `run`, `seed`, `migrate`); **required** (on `deploy`) | Target environment. Conventional values: `dev`, `staging`, `prod`. |
| `port` | string | `"3000"` (`""` on `stop`) | Port the app listens on during `run`/`smoke`/`stop`. Matches the build-recipe `server` default and `$GRIMOIRE_APP_PORT`. String because `just` args are strings; convert inside the recipe as needed. |
| `dry_run` | string | `"false"` | When `"true"`, the recipe should print what it would do without making external changes. Check with `if [ "{{dry_run}}" = "true" ]` inside the recipe. |
| `filter` / `watch` | string | `""` | `test` selectors (e.g. `-k smoke`) / watch-mode flag. |
| `fixture` | string | `""` | `seed` fixture selector. |
| `version` / `target` | string | `""` | `package` release version / target triple; empty ⇒ auto-detect from the manifest. |
| `mode` | string | `""` | `sync-deps` mode flag (`--check` / `--update` / `--offline`). |
| `full` | string | `""` | `vendor-check` whole-vendor audit selector (default is diff-scoped). |

### Why `deploy env` is positional / required

`build` and `run` carry safe defaults — invoking them without arguments produces
a local-dev artifact or a local server, neither with destructive side-effects on
remote infrastructure. `deploy` is different: running it without an explicit
environment choice risks deploying to the wrong target. Making `env` positional
(no `= "..."` default) means `just deploy` with no argument is a `just` error,
not a silent wrong-environment deploy — callers must be deliberate:
`just deploy staging` or `just deploy prod dry_run=true`.

---

## 6. Interaction with `grm-install-doctor`

`grm-install-doctor` audits a Grimoire project's recipe coverage across the
**full vocabulary**, reporting one of three statuses per recipe:

| Status | Condition |
|---|---|
| **MISSING** | The recipe name does not appear as a recipe definition in the `justfile`, or the `justfile` itself is absent. |
| **PARTIAL** | The recipe is defined but its body contains `# grimoire:placeholder`. |
| **OK** | The recipe is defined and its body does not contain `# grimoire:placeholder`. |

### Required vs advisory

MISSING/PARTIAL is a **health problem** (exit 1) only for **required** recipes;
every other recipe is **advisory** (`ADVISORY-MISSING`/`ADVISORY-PARTIAL`, never
a failure). Required when: it's in the **core trio** (`build`, `run`, `deploy`)
— *unless* `.claude/recipes.json` explicitly declares that target absent
(`implemented:false`, `command:null`); **or** `.claude/recipes.json` marks its
target **implemented** and routes it to `just <recipe>` (enforcing
`recipe.py <t>` ≡ `just <t>`; `run` maps to the `server` key, §2.1). A target
implemented via a raw (non-`just`) command is advisory — the justfile is not
its dispatch surface.

### Detection logic

The doctor scans the `justfile` at the project root:

1. **Recipe present?** — look for a line matching `^<name>(\s|:|$)` (a `just`
   recipe definition). If absent → MISSING (or ADVISORY-MISSING).
2. **Placeholder present?** — grep the recipe's body lines for
   `# grimoire:placeholder`. If found → PARTIAL. If not found → OK.

Using `# grimoire:placeholder` (not echo-text matching) as the anchor makes this
**deterministic and version-stable**. **The marker must sit in the recipe body
(indented), not a doc comment above the header** — the doctor scans body lines
only.

### Repair path

When `grm-install-doctor` reports MISSING/PARTIAL required recipes: add the
recipe with the standard signature (if MISSING), replace the
`# grimoire:placeholder` body with the project's real invocation (multi-line
logic in `scripts/`), route the matching `.claude/recipes.json` entry to
`just <recipe>`, then re-run to confirm OK. Advisory findings are optional —
wire them when the project needs the target.

---

## 7. Interaction with `grm-workflow-bootstrap`

During the bootstrap interview (Step 3), `grm-workflow-bootstrap` elicits the
three core commands and records them in `.claude/grimoire-config.json`:

| Interview question | Config key | Example value |
|---|---|---|
| Build command | `commands.build` | `"just build"` or `"npm run build"` |
| Run command | `commands.run` | `"just run"` or `"python -m myapp"` |
| Deploy command | `commands.deploy` | `"just deploy"` or `"./scripts/deploy.sh"` |

When the project uses the standard justfile contract, the canonical answers are
`just build`, `just run`, and `just deploy <environment>` — written into
`grimoire-config.json` so agents can look up commands without inspecting the
`justfile` directly.

### Wiring sequence

Bootstrap detects a root `justfile`, pre-fills the interview answers with the
`just <recipe>` invocation for the required recipes, and (after user confirm or
override) writes `commands.build`/`run`/`deploy`. Those keys are consumed by
`grm-release-phase`(+`-merge`) as the pre-merge build gate, by
`grm-install-doctor`'s health summary, and by task agents starting a local
server for smoke checks.

---

## 8. Consumer Adoption

An existing project adopts the contract in three steps: **(1) Diagnose** — run
`grm-install-doctor` and read the "Justfile contract" section for MISSING/PARTIAL
recipes; **(2) Add** — add the vocabulary recipes (§2) to the root `justfile`
from the standard stubs, replace each placeholder body with the project's real
command (multi-line logic in `scripts/`), route the matching
`.claude/recipes.json` entry to `just <recipe>`, and remove the
`# grimoire:placeholder` line; **(3) Confirm** — re-run `grm-install-doctor`
(required recipes now OK) and run `grm-workflow-bootstrap` (or edit
`.claude/grimoire-config.json`) to record `commands.build`/`run`/`deploy`.

### Automated migration — `grm-recipe-migrate`

Automates step (2): maps entry points onto the vocabulary (§2), `--apply`
writes delegating recipes + rewires `recipes.json`. Design:
`docs/grimoire/design/recipe-migrate-design.md`.

### Sync-from-upstream feature

Three feature-manifest rows deliver this contract: `standard-justfile-recipes`
(v3.53 — the original core trio), `justfile-full-vocabulary` (v3.78 —
the full-vocabulary extension + the `.claude/recipes.json` → `just` routing
convention), and `stop-recipe` (v3.78 — the `stop` target, interface v6).
When `grm-sync-from-upstream` adopts them it:

1. Adds any missing vocabulary stub recipes to the project's `justfile` (or
   creates it if absent) and routes `.claude/recipes.json` entries to
   `just <recipe>` — **extend-only: it never overwrites an implemented recipe**.
2. Records the feature as adopted in the sync manifest.

Projects that already have real implementations (no `# grimoire:placeholder`)
are left untouched — adoption is a no-op for them.

Reference: `grm-sync-from-upstream` features `standard-justfile-recipes` +
`justfile-full-vocabulary` + `stop-recipe` in
`.claude/skills/grm-sync-from-upstream/feature-manifest.md`.

---

## Acceptance criteria

A Grimoire project satisfies this contract when:

1. Its root `justfile` defines the full vocabulary (§2) with the exact argument
   signatures; unimplemented targets carry `# grimoire:placeholder` in the body.
2. Every **implemented** `.claude/recipes.json` entry's command is `just <recipe> …`,
   so `recipe.py <t>` ≡ `just <t>` for every implemented target (the `run`/`server`
   pair resolve identically, §2.1).
3. The required recipes (§6) contain no `# grimoire:placeholder` — each is a real
   implementation (or delegates to a `scripts/` reference / a framework script).
4. `grm-install-doctor` reports the required recipes `OK` in its "Justfile
   contract (full recipe vocabulary)" section and exits 0 on the justfile check;
   advisory recipes may remain MISSING/PARTIAL without failing.

A freshly-scaffolded project (core recipes still placeholder bodies) satisfies
the **partial** state — `grm-install-doctor` reports `PARTIAL` on the required
core recipes and exits non-zero until the bodies are replaced.
