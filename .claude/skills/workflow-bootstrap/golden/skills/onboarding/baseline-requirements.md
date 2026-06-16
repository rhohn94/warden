baseline-version: 3

# Baseline requirements (framework-required capabilities)

This is the maintained, versioned source list of **framework-mandated**
capabilities that every Grimoire project needs to be *self-verifiable* by the
workflow. It is read by the `onboarding` skill's baseline-roadmap seeding step
(see `SKILL.md` §6.5 and `docs/design/onboarding-design.md` §8), which selects
the rows applicable to the project's **shape** and writes them into the adopting
project's `docs/roadmap.md` under a `## Framework-required (baseline)` section,
each tagged `[framework-required]`.

These are **not** user feature ideas — they are capabilities the framework
requires so an agent can build, launch, and verify the project without a human
at the screen. They may be *scheduled* into a version but must **never** be
*removed* during scope-trimming.

## Versioning

The `baseline-version: N` line on line 1 is the contract for idempotent,
additive re-seeds. The seeding step matches existing roadmap rows by their
**stable capability key** (the `key:` field below) and adds only rows not yet
present. Bump `baseline-version` whenever a row is **added** or its **shape
condition changes**, so a later onboarding re-run (or a `sync-from-upstream`
reconciliation) can add only the newly-introduced rows without duplicating
already-seeded ones.

## Project shape

"Shape" is derived from the onboarding GUI-presence answer (§1 step 4 / §2
inference) plus the test/build commands captured by `workflow-bootstrap`:

- **GUI** — the project has (or will have) a user interface (GUI-presence `yes`).
- **Service** — a long-running networked process (server / API / daemon).
- **Library** — a reusable package with no launch path of its own.
- **CLI** — a command-line program (already a non-interactive surface).
- **Web app** — a browser-delivered, server-hosted app (the `web-app` config
  block is `value: yes`; see `docs/design/web-app-support-design.md` §1). Web-app
  is a narrower fact than GUI/Service: a web app is always GUI+Service, but a
  native-GUI or headless service is **not** a web app. The web-app rows seed only
  when this fact is set.

The **all-shapes** rows are seeded **unconditionally** for every project. The
shape-specific rows are seeded only when their shape condition matches.

## Per-shape conditional table

Each row specifies: a stable `key:` (for idempotent matching), the
human-readable roadmap line, the shape condition, and a one-line rationale.

| key | shape | roadmap line | rationale |
|-----|-------|--------------|-----------|
| `test-command` | all shapes | Runnable test command | A framework-required capability: the workflow asserts a runnable test command exists (or scaffolds a test-harness target) so every branch can be tested before merge. |
| `smoke-build-command` | all shapes | Smoke/build command | A framework-required capability: a smoke/build command lets an agent confirm the project compiles/builds cleanly before reporting a branch done. |
| `non-interactive-launch` | all shapes | Non-interactive launch path | A framework-required capability: the project can be started/exercised without an interactive prompt, which is required for agent self-verification. |
| `gui-visual-inspection-cli` | GUI | Visual-inspection CLI (headless screenshot / render-to-file / DOM-or-scene dump / automation endpoint) — see UX tier (`design-language-adapt`, `ux-demo-build`) | A framework-required capability: an agent must be able to verify UI output without a human at the screen; cross-references the UX design tier rather than duplicating it. |
| `service-health-probe` | service | Health/readiness probe endpoint (e.g. `/healthz`) | A framework-required capability: a probe endpoint makes the project's liveness checkable non-interactively. |
| `library-test-harness` | library | Runnable test harness exercising the public API | A framework-required capability: a library has no launch path of its own, so its self-verification surface *is* a test harness that exercises the public API (sharpens the all-shapes test-command row). |
| `web-app-healthz` | web app | Unauthenticated `GET /healthz` returning JSON `{status, version}` — see `docs/web-app-deployment-protocol.md` §5 | A framework-required capability: the web-app deployment protocol gates supervision and health-gated self-update on this endpoint; sharpens the `service-health-probe` row with the protocol's exact JSON contract. |
| `web-app-deploy-bundle` | web app | Deployable bundle via the `package` recipe target (versioned archive + `release.json` + `grimoire-build-info.json`) — see `docs/web-app-deployment-protocol.md` §1–§3, §8 | A framework-required capability: a web app must ship as a reproducible, self-installing bundle so an agent can stand it up and self-update it without a source tree; produced by the build-recipe `package` target. |
| `web-app-service-supervision` | web app | Service supervision verbs (`install`/`start`/`stop`/`status`/`uninstall`) — see `docs/web-app-deployment-protocol.md` §4 | A framework-required capability: a web app must install as a supervised, auto-restarting host service so it runs and recovers without a human at the screen. |
| `web-app-fleet-status` | web app | `GET /fleet/v1/status` endpoint: optional-auth dual shape (full JSON with schema_version/app/instance/build/runtime/dependencies/update for valid bearer; minimal `{schema_version, app, build.version, runtime.status}` subset with 200 — NEVER 401 — without) — see `docs/design/fleet-status-contract.md` | A framework-required capability: the Fleet Status Contract v1 endpoint lets Mission Control and automated checks discover and verify any deployed instance without per-app special-casing; validated by `fleet_conformance.py`. |

Notes:

- **CLI** has no extra shape-specific row — a CLI is already a non-interactive
  surface, so it is covered by the all-shapes `non-interactive-launch` row
  (asserting `--help` / a smoke invocation exists).
- The **GUI** row **cross-references** the UX tier (`design-language-adapt` →
  `docs/design/ux/design-language.md`, `ux-demo-build` → `ux-demo/`); the
  visual-inspection CLI is the *agent-facing* verification surface, distinct
  from (and complementary to) the *design* surface owned by the UX tier. For a
  GUI-deferred project, `repo-init` already adds a `## Backlog` UX row; this
  baseline row complements it without colliding.
- The **library** row reinforces the all-shapes `test-command` row; when both
  apply, seed both — the library row makes the public-API exercise explicit.
- The **web-app** rows (`web-app-healthz`, `web-app-deploy-bundle`,
  `web-app-service-supervision`, `web-app-fleet-status`) seed only when the
  `web-app` config block is `value: yes` (`docs/design/web-app-support-design.md`
  §1). They are the obligation surface of the web-app deployment protocol
  (`docs/web-app-deployment-protocol.md`) and are the rows the `web-app-apply`
  retrofit skill seeds (§3.3 there). A web app is also Service, so both
  `service-health-probe` and `web-app-healthz` may apply — seed both; the
  web-app row sharpens the generic probe with the protocol's exact JSON
  contract. The `web-app-fleet-status` row is the Fleet Status Contract v1
  obligation, validated offline by `fleet_conformance.py --self-test` (fixture
  mode) or against a live endpoint with `--url`. These keys are stable and
  never reused.
