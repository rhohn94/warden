# Justfile Standard Design

> **Status:** Accepted — v3.53
> **Issue:** [#191](https://github.com/rhohn94/grimoire-framework/issues/191)
> **Related:** `grm-install-doctor`, `grm-workflow-bootstrap`, `grm-sync-from-upstream`

---

## 1. Motivation

Grimoire projects run under agent-driven CI. Every agent and every CI pipeline
must be able to build, run, and deploy the project by invoking a small, stable
set of named commands — **without reading project-specific documentation first**.

Without a standard, each project names these operations differently (`make build`,
`./scripts/start.sh`, `npm run dev`, …). An agent bootstrapping a new task
worktree must reverse-engineer the project's convention before it can do
useful work. A CI template must hard-code project-specific commands. Both
situations introduce friction and error surface.

A **standard set of `just` recipes** solves this by giving every Grimoire
project the same three entry points (`build`, `run`, `deploy`) with identical
signatures. Agents call `just build` and it works. CI pipelines call `just
deploy env=staging` and the semantics are defined by contract, not guesswork.

The secondary benefit is **detectability**: because the recipes follow a
prescribed shape — including a specific placeholder comment — the
`grm-install-doctor` skill can audit recipe coverage mechanically and
distinguish a real implementation from an unfinished stub, without fragile
pattern-matching on echo text.

---

## Scope

**In scope:** the three required recipe names (`build`, `run`, `deploy`), their
argument signatures, the `# grimoire:placeholder` convention, and framework
tooling that enforces or adopts the contract (`grm-install-doctor`,
`grm-workflow-bootstrap`, `grm-sync-from-upstream` manifest).

**Out of scope:** implementing project-specific deploy pipelines; CI
configuration; the `test`, `db-up`, `db-down` optional recipes (described here
but not required); Copilot-flavor support.

---

## 2. Required Recipes

Every Grimoire project **must** expose these three recipes in its root `justfile`.
Signatures are fixed by contract; parameter names and defaults must be reproduced
exactly so that callers (agents, CI) can invoke them without inspecting the file.

```just
build env="dev":
    # grimoire:placeholder
    @echo "TODO: replace with build command"

run env="dev" port="8080":
    # grimoire:placeholder
    @echo "TODO: replace with run command"

deploy env dry_run="false":
    # grimoire:placeholder
    @echo "TODO: replace with deploy command"
```

### Key decisions

| Recipe | Parameter | Rationale |
|---|---|---|
| `build` | `env` defaults to `"dev"` | Safe default — local dev build can always be invoked without arguments. |
| `run` | `env` defaults to `"dev"`, `port` defaults to `"8080"` | Agents and smoke checks can call `just run` with no arguments; CI can override as needed. |
| `deploy` | `env` is **positional / required** (no default) | Forces an explicit environment choice at call time; accidentally running a production deploy against the wrong environment is prevented by the missing argument. |
| `deploy` | `dry_run` defaults to `"false"` | Safety hatch — set `dry_run=true` to preview the deploy without mutating remote state. |

> **Why `just`?** `just` is already the task runner Grimoire quick-start
> templates ship (see `.claude/quick-start-templates/service/files/justfile`
> and `.claude/quick-start-templates/web/files/justfile`). It is
> cross-platform, has first-class argument support, and does not require a
> build system to be present.

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

`grm-install-doctor` determines recipe status (MISSING / PARTIAL / OK) by
scanning the `justfile` for recipe presence and placeholder state. Using an
echo string (e.g. `@echo "TODO: ..."`) as the detection signal is fragile:

- The text can be partially overwritten (a developer replaces the echo but
  leaves the comment); the recipe appears implemented even though it still
  contains placeholder structure.
- Echo text is user-visible output — it may be deliberately preserved for
  documentation purposes even in a real implementation.
- String matching against free-form text is not stable across Grimoire
  versions or across languages.

The `# grimoire:placeholder` comment is **invisible to end users** at runtime
(it is a `just` comment, not shell output), is **unambiguous** (no reason to
keep it in a real implementation), and is **grep-stable** (a single constant
string).

### PARTIAL status

A recipe that exists but still contains `# grimoire:placeholder` in its body
is classified **PARTIAL** — the recipe name is present (agents can call it
without an error from `just`), but the body has not been implemented. PARTIAL
is distinct from MISSING (recipe absent) and OK (recipe present, no placeholder
marker).

### Removal rule

When a developer wires a real command into a recipe, they **must remove** the
`# grimoire:placeholder` line. Leaving it in place causes `grm-install-doctor`
to continue reporting PARTIAL even after implementation.

---

## 4. Optional / Recommended Recipes

The following recipes are **not** required by the contract but are **strongly
recommended** for projects that have the corresponding concerns. Quick-start
templates (`service`, `web`) ship them by default.

### `test`

```just
test:
    # grimoire:placeholder
    @echo "TODO: replace with test command"
```

For projects with a database dependency, wire `test` to depend on `db-up` so
the database is always available when tests run:

```just
test: db-up
    pytest tests/
```

### `db-up` and `db-down`

These recipes manage a Dockerized development database. They are designed to be
**multi-worktree-safe**: Grimoire encourages one isolated git worktree per
agent or task, but all worktrees share a single dev database container (keyed
by `container_name`) to avoid port conflicts and volume proliferation.

The canonical implementation ships in both quick-start templates
(`.claude/quick-start-templates/service/files/justfile` and
`.claude/quick-start-templates/web/files/justfile`). Copy the recipe verbatim
— do not simplify to `docker compose up -d db`, which fails when the container
is already running under a sibling worktree's compose project.

```just
db-up:
    #!/usr/bin/env bash
    set -euo pipefail
    # ... idempotent, multi-worktree-safe bring-up logic ...
    # See quick-start-templates for the full implementation.

db-down:
    docker compose stop db || true
```

> The `db-down` recipe **stops** the container but does not remove it or its
> volume. Data persists across a `db-down` / `db-up` cycle. Never run
> `docker compose down -v` from a task worktree — it destroys the shared volume.

---

## 5. Argument Convention

All argument names and defaults across Grimoire projects follow this table.
Consistent naming means agents can document a single invocation pattern and
use it across all projects.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `env` | string | `"dev"` (on `build`, `run`); **required** (on `deploy`) | Target environment. Conventional values: `dev`, `staging`, `prod`. |
| `port` | string | `"8080"` | Port the app listens on during `run`. String because `just` arguments are strings; convert inside the recipe as needed. |
| `dry_run` | string | `"false"` | When `"true"`, the recipe should print what it would do without making external changes. Check with `if [ "{{dry_run}}" = "true" ]` inside the recipe. |

### Why `deploy env` is positional / required

`build` and `run` carry safe defaults — invoking them without arguments
produces a local-dev artifact or a local server. Neither has destructive
side-effects on remote infrastructure.

`deploy` is different: running it without an explicit environment choice risks
deploying to the wrong target. Making `env` positional (no `= "..."` default)
means `just deploy` without an argument is a `just` error rather than a silent
wrong-environment deploy. Callers must be deliberate: `just deploy staging` or
`just deploy prod dry_run=true`.

---

## 6. Interaction with `grm-install-doctor`

`grm-install-doctor` audits a Grimoire project's recipe coverage as part of its
**feature adoption** check (Step 1c — agent-run, not mechanical). The audit
reports one of three statuses per required recipe:

| Status | Condition |
|---|---|
| **MISSING** | The recipe name does not appear as a recipe definition in the `justfile`, or the `justfile` itself is absent. |
| **PARTIAL** | The recipe is defined but its body contains `# grimoire:placeholder`. |
| **OK** | The recipe is defined and its body does not contain `# grimoire:placeholder`. |

### Detection logic

The agent scans the `justfile` at the project root:

1. **Recipe present?** — look for a line matching `^<name>(\s|\()` (a `just`
   recipe definition). If absent → MISSING.
2. **Placeholder present?** — grep the recipe's body lines for
   `# grimoire:placeholder`. If found → PARTIAL. If not found → OK.

Using `# grimoire:placeholder` as the detection anchor (rather than echo-text
matching) makes this check **deterministic and version-stable**: the string is
constant, purpose-built, and has no legitimate reason to appear in a real
implementation.

### Repair path

When `grm-install-doctor` reports MISSING or PARTIAL recipes, the recommended
repair is:

1. Add the recipe(s) with the standard signature and `# grimoire:placeholder`
   in the body (if MISSING).
2. Replace `# grimoire:placeholder` (and the `@echo "TODO"` line) with the
   project's real build / run / deploy invocation.
3. Re-run `grm-install-doctor` to confirm all three required recipes are OK.

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
`just build`, `just run`, and `just deploy env=<environment>`. Bootstrap writes
these into `grimoire-config.json` so agents can look up the project's commands
without inspecting the `justfile` directly.

### Wiring sequence

1. Bootstrap detects whether a `justfile` is present at the project root.
2. If present, it checks for the three required recipes and pre-fills the
   interview answers with the `just <recipe>` invocation.
3. The user confirms or overrides the pre-filled answers.
4. Bootstrap writes the confirmed values to `commands.build`, `commands.run`,
   and `commands.deploy` in `.claude/grimoire-config.json`.

The `commands.*` keys are consumed by:

- `grm-release-phase` — runs `commands.build` as part of the pre-merge build
  gate.
- `grm-release-phase-merge` — re-runs `commands.build` after merge to confirm
  the integrated branch still builds.
- `grm-install-doctor` — reports the configured commands in its health summary.
- Task agents — read `commands.run` to start a local server for smoke checks.

---

## 8. Consumer Adoption

An existing Grimoire project that does not yet have the standard justfile
recipes can adopt the contract in three steps.

### Step 1 — Diagnose

Run `grm-install-doctor` and look at the recipe coverage section of the
adoption report. It will list each of `build`, `run`, and `deploy` as MISSING
or PARTIAL.

### Step 2 — Add the recipes

Add the three required recipes to the project's `justfile` (create it at the
project root if absent). Start from the standard stubs:

```just
build env="dev":
    # grimoire:placeholder
    @echo "TODO: replace with build command"

run env="dev" port="8080":
    # grimoire:placeholder
    @echo "TODO: replace with run command"

deploy env dry_run="false":
    # grimoire:placeholder
    @echo "TODO: replace with deploy command"
```

Then replace the placeholder body with the project's real commands. Remove the
`# grimoire:placeholder` comment from each recipe once its body is implemented.

### Step 3 — Confirm

Re-run `grm-install-doctor`. All three recipes should now report **OK**. At
this point, also run `grm-workflow-bootstrap` (or update
`.claude/grimoire-config.json` manually) to record the `commands.build`,
`commands.run`, and `commands.deploy` values.

### Sync-from-upstream feature

The upstream feature that delivers this contract is named
`standard-justfile-recipes`. When `grm-sync-from-upstream` adopts this feature,
it:

1. Adds the three stub recipes to the project's `justfile` (or creates it if
   absent).
2. Records the feature as adopted in the sync manifest.

Projects that already have a `justfile` with real implementations in the three
recipe bodies (i.e., no `# grimoire:placeholder`) are left untouched — adoption
is a no-op for them.

Reference: `grm-sync-from-upstream` feature `standard-justfile-recipes` in
`.claude/skills/grm-sync-from-upstream/feature-manifest.md`.

---

## Acceptance criteria

A Grimoire project satisfies this contract when:

1. Its `justfile` (at the repo root) defines `build`, `run`, and `deploy` with
   the exact argument signatures specified in §2.
2. None of the three recipe bodies contains `# grimoire:placeholder` — each is
   a real implementation, not a stub.
3. `grm-install-doctor` reports `OK` for all three recipes in its
   "Justfile contract" section and exits 0 on the justfile check.

A project that has stubs (placeholder bodies) satisfies the **partial** state —
`grm-install-doctor` reports `PARTIAL` and exits non-zero until the bodies are
replaced.
