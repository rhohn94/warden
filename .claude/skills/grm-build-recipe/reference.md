# grm-build-recipe — reference (load on demand)

Detail that supports `SKILL.md` but isn't needed on every call. Load this when
wiring the deploy layer or adopting the shared recipes into an existing project.

## Deploy-layer scripts

The four web-app deploy-layer targets and the shared script each `just` recipe
delegates to (the `recipes.json` → `just` mapping is in `SKILL.md`):

| Target | Script | Contract / design |
|---|---|---|
| `package` | `scripts/package.sh` | versioned bundle + `release.json` + `SHA256SUMS` + deterministic tar — `web-app-deployment-protocol.md` §1/§2/§8 |
| `deploy` | `scripts/deploy.sh` | reads the `environments` block, branches on `transport` × `service_manager`, honors `deploy_policy` — `deploy-environment-design.md` §3 |
| `smoke` | (inline `just` recipe) | `GET /healthz` asserting a 2xx `{status, version}` body — `web-app-deployment-protocol.md` §5 |
| `release` | `scripts/release.sh` | changelog-derived version bump/test/build/tag + `milestone:v{X.Y}` reconciliation via the issue-tracker abstraction — issue #201 §4 |
| `stop` | `scripts/stop.sh` | kill running instance(s) of this project's process; resolution order port → `$GRIMOIRE_APP_PORT` → the pidfile `run` wrote → a declared process pattern; idempotent — `justfile-standard-design.md` §2.3 (RSS-4, #322) |

The canonical `recipes.json` entries (as shipped by the `web` quick-start
template):

```json
"package": { "command": "just package ${version} ${target}",
             "implemented": true,
             "params": { "version": {"default": ""}, "target": {"default": ""} } },
"deploy":  { "command": "just deploy ${env}",
             "implemented": true, "params": { "env": {"default": "production"} } },
"smoke":   { "command": "just smoke ${port}",
             "implemented": true, "params": { "port": {"default": "3000"} } },
"release": { "command": "just release",
             "implemented": true, "params": {} },
"stop":    { "command": "just stop ${port}",
             "implemented": true, "params": { "port": {"default": ""} } }
```

## Adoption path — retiring bespoke `deploy`/`package`/`release` recipes

An existing project with its own hand-rolled `deploy`/`package`/`release` justfile
recipes adopts the shared layer in three steps (parallels
`justfile-standard-design.md` §8's Consumer Adoption):

1. **Sync.** Run `grm-sync-from-upstream`; the `standard-justfile-recipes` feature
   adds the shared `scripts/<name>.sh` + the standard `just` recipes (non-destructive
   3-way merge — a project's already-implemented recipe bodies are left untouched).
2. **Parameterize, don't reimplement.** Fill the project's `environments` block
   (for `deploy`), a `scripts/package-manifest.sh` (app name / binary / asset globs
   for `package`), and a `scripts/release-manifest.sh` (changelog path / version
   files for `release`) — the scripts read these instead of hardcoding. Then
   delete the bespoke bash bodies, pointing each `just` recipe at
   `scripts/<name>.sh` and each `recipes.json` entry at `just <target>`.
3. **Confirm.** `recipe.py --list` shows `package`/`deploy`/`smoke`/`release` as
   `implemented`; `recipe.py <target> --dry-run` prints the resolved `just <target>`
   line. `recipe.py <target>` ≡ `just <target>` now holds.
## The `recipes.json` → `just` routing convention (all targets, RSS-3 #321)

**The justfile is the de-facto recipe layer.** Every **implemented** target
follows ONE wiring convention (generalized in RSS-3 from the v3.68/v3.69
deploy-layer targets to the *whole* vocabulary), so `recipe.py <target>` and
`just <target>` resolve to the same code path:

1. **A thin `just` recipe** in the standard `justfile` is the implementation —
   delegating any multi-line logic to a shared `scripts/<name>.sh` reference (the
   bash lives in the script, not inlined in `recipes.json`).
2. **The `recipes.json` entry** maps the contract target to that `just`
   invocation (e.g. `"deploy": {"command": "just deploy ${env}", "implemented":
   true, …}`), threading the target's params as `${…}` placeholders. So
   `recipe.py deploy --env dev` resolves to `just deploy dev` →
   `scripts/deploy.sh dev`. The `server` target routes to **`just run`** (the
   canonical justfile name; §run↔server above). **Callers use the recipe target
   (or the `grimoire-recipe` MCP), never the script or `just` directly** —
   `recipe.py <t>` ≡ `just <t>` for every implemented target.
3. **Unimplemented targets** stay `command: null` (or an `implemented: false`
   routed stub) — any call exits 2 (loud failure), never a silent no-op. The
   `--generate <stack>` presets pre-fill each inferrable target as a
   `just <recipe> …` routed stub (`implemented: false`); a project fills the
   justfile recipe body and flips `implemented: true`.

`sync-deps` / `vendor-check` delegate directly to the framework's own scripts
(`grm-sync-deps/sync_deps.py`, `grm-dependency-audit/dependency_channel_conformance.py`)
rather than a project `scripts/` file.

Full contract: `docs/design/justfile-standard-design.md` §2. The `web` quick-start
template ships `package`/`deploy`/`smoke`/`release` wired to real `scripts/`
implementations — copy them as the canonical shape. The `web`/`service`
templates additionally ship `stop` wired to `scripts/stop.sh` (§2.3 — generic,
not project-specific). Per-target scripts + contracts: `reference.md` §Deploy-layer
scripts.

### Adoption path — retiring bespoke recipes

A project with hand-rolled recipes retires them for the shared layer in three
steps (sync → parameterize the per-app manifests, don't reimplement → confirm
`recipe.py <t>` ≡ `just <t>`). Full procedure: `reference.md` §Adoption path
(parallels `justfile-standard-design.md` §8 Consumer Adoption). Deploy-environment
model: `docs/grimoire/design/deploy-environment-design.md`; interface contract:
`docs/web-app-deployment-protocol.md` §Environments.

