# Fixtures Convention Design

> **Related:** `grm-build-recipe` (`recipe.py`'s `seed` target), the Justfile
> standard (`justfile-standard-design.md`), `grm-install-doctor`.

> **Up:** [↑ Design index](README.md)

## Motivation

`recipe.py` reserves a `seed` verb ("populate a local data store with
fixtures", params `fixture`/`env`) and every quick-start justfile stubs a
`seed fixture="" env="dev"` recipe — but the generated body has always been an
unimplemented `# grimoire:placeholder`, and no fleet repo has ever filled it
in. Every project re-derives (or skips) fixture loading from scratch, and
downstream tooling that assumes seeded dev data (e.g. a cockpit/dashboard
stuck watching demo data because there's no standard seed path) has nothing
to build on.

What changes if we don't ship it: `seed` stays permanently decorative — the
one verb in the build-recipe vocabulary that is reserved but never real.

## Scope

**In scope:**
- The `fixtures/` directory convention (one fixture set per subdirectory, a
  small manifest declaring format + idempotency strategy per set).
- A generic, framework-owned seed-dispatch engine in `recipe.py` that reads
  the convention and applies it — no per-app knowledge baked into the
  framework.
- The standard justfile's `seed fixture="" env="dev"` recipe, wired to that
  engine (same delegation pattern as `sync-deps`/`vendor-check`).
- A dev-only, fail-closed auto-seed hook on `run` (web/service stacks, the
  ones with a datastore dependency).
- A presence/convention conformance check in `install_doctor.py`.

**Out of scope (see the issue comment on #438):**
- A working seed implementation in the web starter (epic #395) against
  recordkeeper-shaped sqlite — needs a real running reference app.
- The literal `just seed && just run` → healthz-OK-with-non-empty-data
  live probe — same reason. `install_doctor.py`'s check here is a static
  presence/well-formedness check, not a live probe against a running service.

## Design

### The `fixtures/` directory convention

```
fixtures/
  <set-name>/
    manifest.json
    001_first.sql        # or .json — family-appropriate extension
    002_second.sql
```

One fixture **set** per subdirectory. A repo may declare as many sets as it
needs (`core`, `demo`, `perf`, …); `recipe.py seed` with no `--fixture` applies
every set it finds, in sorted-name order.

`manifest.json` (required per set):

| Field | Required | Meaning |
|---|---|---|
| `family` | yes | Storage family: `"sql"` or `"json"`. Selects the default file glob (`*.sql` / `*.json`) when `files` is omitted. |
| `strategy` | yes | Idempotency strategy, declared per set: `"truncate-and-load"` (each file may wipe state before loading — safe to re-run) or `"upsert"` (files use native upsert semantics, e.g. `INSERT ... ON CONFLICT`). Documents the guarantee; `recipe.py` doesn't enforce it — the fixture content is responsible for actually being idempotent under the declared strategy. |
| `apply` | yes | A shell command **template** run once per fixture file. Supports two placeholders: `{file}` (the fixture file's path) and `{env}` (the resolved `--env` value). Real shell env vars (e.g. `$DATABASE_URL`) expand normally at execution time — `{...}` (single braces) is reserved for this template so it never collides with `${VAR}` shell syntax. |
| `empty-check` | no | An optional shell command; exit 0 means "datastore is empty, go ahead and seed", nonzero means "not empty, skip". Consulted only when the caller passes `--if-empty` (see below). No declared check ⇒ `--if-empty` always seeds (fixtures are idempotent by convention, so re-applying is defined as safe). |
| `files` | no | Ordered list of fixture files, relative to the set directory. Omitted ⇒ sorted glob for the family's default extension. |

Example (`fixtures/core/manifest.json`):

```json
{
  "family": "sql",
  "strategy": "truncate-and-load",
  "apply": "sqlite3 ${DATABASE_PATH:-dev.sqlite3} < {file}",
  "files": ["001_truncate.sql", "002_load.sql"]
}
```

This is deliberately the *only* place stack-specific knowledge lives — the
`apply`/`empty-check` templates are declared by the project, per fixture set.
`recipe.py` itself never hardcodes a datastore driver.

### `recipe.py`'s generic seed-dispatch engine

`recipe.py seed --fixture <name> --env <env>` is special-cased in the
dispatcher (like `--list`/`--generate`/`--self-test`) rather than routed
through the project's `.claude/recipes.json` command-template mechanism that
every other target uses. This is deliberate: unlike `build`/`test`/`deploy`
(where the *project* owns the implementation and `recipe.py` is a thin
router), `seed` is a case where the framework can own a real, working default
once a project adopts the `fixtures/` convention — no project-side Python/
shell glue required.

Flow: `cmd_seed()` resolves the fixture set(s), loads and validates each
`manifest.json`, and (per file, in declared order) renders and shells out the
`apply` template via `subprocess.call(..., shell=True)`. A failing apply
command aborts the whole seed run with a nonzero exit — matching the
dispatcher-wide contract of "fail loud, never silently no-op".

**Fail-closed outside dev:** `cmd_seed()` refuses to run for any `--env`
other than `dev` unless the caller passes `--allow-non-dev` explicitly. This
is enforced before anything else — before even checking whether
`fixtures/` exists — so `recipe.py seed --env production` never touches a
real datastore by accident.

**`--if-empty`:** used by the `run` auto-seed hook (below). When set, each
fixture set's `empty-check` (if declared) gates whether that set is applied;
undeclared ⇒ always apply.

### Justfile wiring

The standard `seed fixture="" env="dev":` recipe (all four quick-start
stacks — `cli` has no datastore and doesn't declare `seed`, so this is
`gui`/`lib`/`service`/`web`) delegates straight to the framework engine,
mirroring the existing `sync-deps`/`vendor-check` delegation pattern:

```
seed fixture="" env="dev":
    python3 .claude/skills/grm-build-recipe/recipe.py seed --fixture {{fixture}} --env {{env}}
```

No project-side script to write. A repo that has never touched `fixtures/`
gets a clear, loud "no fixtures/ directory" error (never a silent no-op); a
repo that adopts the convention gets working seeding for free.

### Dev-only auto-seed on `run`

Per item 3 of #438, `dev` seeds by default on `run` — but only in `dev`, and
only best-effort (a project that hasn't adopted `fixtures/` yet must not have
`run` start failing because of this). The `web`/`service` justfiles' `run`
recipe gains, ahead of the (still-placeholder) actual entrypoint command:

```
if [ "{{env}}" = "dev" ]; then
    python3 .claude/skills/grm-build-recipe/recipe.py seed --env dev --if-empty || true
fi
```

The `{{env}} = "dev"` guard is the fail-closed half (never fires outside
dev — matches `cmd_seed`'s own refusal, belt-and-braces); `|| true` is the
graceful-when-unadopted half (a project with no `fixtures/` yet sees `run`
proceed exactly as before, not a new hard failure). `gui`/`lib` are left
unchanged — those stacks' `run` recipe is explicitly documented as "no
runnable entrypoint" by default, so there is nothing to auto-seed before.

### `install_doctor.py` conformance check

A new `fixtures-convention` check, registered alongside the existing
justfile/architecture/hook-contract checks: for a repo declaring
`web-app.value: yes`, WARN (never a hard failure — same severity class as
`release-readiness`) when there is **neither** a `fixtures/` directory
**nor** a real (non-placeholder) `seed` justfile recipe. When `fixtures/`
is present, its shape is validated against the manifest schema above
(existence of `manifest.json`, valid `family`/`strategy`, an `apply`
template containing `{file}`) — this is a static, offline shape check, not a
live probe against a running datastore (that needs epic #395's reference
app; out of scope here).

## Acceptance

- `recipe.py seed --fixture <name> --env dev` applies a conformant fixture
  set's files in declared order, idempotently (re-running produces the same
  end state for a `truncate-and-load` set).
- `recipe.py seed --env production` (or any non-`dev` value) refuses with a
  nonzero exit and no fixture files applied, unless `--allow-non-dev` is
  passed explicitly.
- `recipe.py --self-test` covers manifest loading/validation, idempotent
  apply, fail-closed refusal, `--if-empty` gating, and dry-run.
- `install_doctor.py` flags (WARN) a scaffold with `web-app.value: yes` and
  neither `fixtures/` nor an implemented `seed` recipe; passes (OK) once
  either exists (or is absent when `web-app.value` isn't `yes`).
- `install_doctor.py --self-test` covers the WARN/OK/malformed-manifest
  paths.

## Follow-ups

- A working reference implementation against recordkeeper-shaped sqlite in
  the web starter, and the literal `just seed && just run` → healthz-OK
  live probe wired into the catalog-conformance machinery — both belong to
  epic #395 (gated "plan after R6"); noted on issue #438.
