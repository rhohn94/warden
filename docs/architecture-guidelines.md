# Architecture Guidelines

> **Up:** [↑ Docs](README.md)


Generic architectural principles for projects built on this scaffolding — *how*
to think about structure, boundaries, and dependencies.

> **Distinct from `docs/design/architecture-design.md`.** This document holds
> general guidelines that apply to any project; `architecture-design.md`
> captures *this* project's actual architecture. Guidelines = how we build;
> the design doc = what we built. Link between them rather than restating.

## Principles
- **Separation of concerns** — each module owns one area; cross-cutting
  concerns are isolated, not scattered.
- **Low coupling, high cohesion** — depend on interfaces, not implementations.
- **Explicit dependencies** — no hidden globals; pass collaborators in.

## Preferred patterns (worked examples)

Each pattern below states the rule, *why* it exists, a brief illustrative
sketch, the anti-pattern it guards against, and how an agent applies it during
implementation and review. Examples are intentionally small — illustrate the
shape, don't implement the system.

### Decoupled frontend / backend
The UI communicates **only** through a defined API contract: no shared mutable
state, no direct database access from the frontend, no business logic in the
view layer.

*Rationale:* independent deployability, independent testability, and the freedom
to substitute either side's technology without touching the other.

```
frontend  ──HTTP/RPC (typed contract)──▶  backend API  ──▶  domain ──▶  store
   ▲ owns: rendering, local UI state            ▲ owns: business rules, persistence
   ✗ never: SQL, domain invariants              ✗ never: HTML/markup, view concerns
```

*Anti-pattern:* a view component that opens a DB connection or embeds a business
rule ("if balance < 0 …") — the two tiers can no longer evolve or deploy
independently.

*How agents apply this:* when implementing, route all UI→data access through the
API client; when reviewing, flag any persistence/domain logic reachable from the
view layer, or shared in-process state across the boundary.
<!-- audit: id="arch-decoupled-fe-be" check="frontend talks only via API contract; no DB access or business logic in the view layer" severity="error" applies="web,gui" -->

### Modularity by design
Subsystems (auth, data access, notification, billing, …) are self-contained
modules with explicit interfaces. No subsystem reaches into another's internals.

*Rationale:* a module can be extracted, replaced, or reused across projects
without surgery — and it feeds clean component-catalog entries (see #30).

```
billing/        exposes: BillingService (interface)
                hides:   providers, retry policy, schema
notification/   depends on: BillingService — NOT on billing's internal types
```

*Anti-pattern:* `notification` importing `billing/internal/StripeClient` directly
— a change inside `billing` now silently breaks `notification`.

*How agents apply this:* import only a module's public surface; when reviewing,
flag cross-module imports that reach past the published interface.
<!-- audit: id="arch-modularity" check="subsystems are self-contained; no imports reach past another module's public interface" severity="warn" applies="all" -->

### Genericity over specificity
Shared infrastructure (clients, middleware, utilities) is application-agnostic;
business logic is pushed to the edges so the core stays reusable.

*Rationale:* lowers the cost of the next project and of reusable components.

*Anti-pattern:* a "generic" HTTP client that hard-codes this app's auth header
names or endpoint paths — it cannot be reused without editing.

*How agents apply this:* keep app-specific names/values out of shared utilities;
inject them. When reviewing, flag business constants baked into infrastructure.
<!-- audit: id="arch-genericity" check="shared infrastructure is application-agnostic; business logic lives at the edges, not in core utilities" severity="warn" applies="all" -->

### Separation of concerns at the layer boundary
Each layer — presentation, application logic, domain, persistence — owns a
distinct responsibility. Cross-layer leakage is a violation, not a shortcut.

*Rationale:* layers can be tested and changed in isolation; leakage couples them.

*Anti-pattern:* domain objects constructed in controllers, or SQL embedded in
service methods — presentation/persistence concerns bleeding into other layers.

*How agents apply this:* keep SQL in the persistence layer, domain invariants in
the domain layer, request/response shaping in presentation. When reviewing, flag
SQL in services or domain objects leaking into controllers.
<!-- audit: id="arch-layer-separation" check="no cross-layer leakage: no SQL in service methods, no domain objects built in controllers" severity="error" applies="all" -->

## Module & boundary design
- Define clear public surfaces; keep internals private.
- Prefer one direction of dependency between modules; break cycles by extracting
  a shared interface, not by reaching across.
<!-- audit: id="arch-dependency-direction" check="one direction of dependency between modules; no import cycles (deterministic counterpart: architecture-rules.json forbid-cycles + allowed-edges)" severity="error" applies="all" -->
<!-- audit: id="arch-public-surface" check="modules expose a public surface; nothing imports past it into another module's internals (counterpart: architecture-rules.json no-internal-reach)" severity="warn" applies="all" -->

These two rules have a **deterministic** counterpart: declare the project's
layers and allowed dependency edges in `.claude/architecture-rules.json` and run
the **`grm-architecture-audit`** skill to check them as fitness functions over the
import graph. See `docs/grimoire/design/architecture-fitness-design.md`.

## Modularization metrics

Modularity is a measurable quantity, not just a principle. `grm-code-health`
Section B reports, per module (directory/package), afferent coupling (Ca, how
many modules depend on it), efferent coupling (Ce, how many it depends on),
instability `I = Ce/(Ca+Ce)`, and module size. Use them to steer structure:

- **Stable core, unstable leaves** — shared/core modules should trend *stable*
  (low I): many depend on them, they depend on little. Volatile, fast-changing
  logic lives in *leaf* modules (high I) where churn hurts no one downstream.
  The danger zone is a module that is both widely depended-upon (high Ca) *and*
  unstable (high I) — every change there ripples; refactor toward stability.
- **Split before the budget** — when a module grows past its size budget (the
  language sub-doc sets the number, e.g. Rust's ~400-line module), split it
  along its internal seams rather than letting it congeal.
<!-- audit: id="arch-module-instability" check="core/shared modules trend stable (low instability I); no module is both widely depended-upon and unstable" severity="info" applies="all" -->
<!-- audit: id="arch-module-size" check="modules stay within the language size budget; split before growing past it" severity="info" applies="all" -->

See `docs/grimoire/design/modularization-metrics-design.md`.

## Standard project structure
- Every project follows the canonical top-level layout in
  [project-structure.md](project-structure.md): first-party source in `src/`,
  tests in `tests/`, dependencies under `lib/` (`lib/first-party/` for your own
  reusable libraries, `lib/third-party/` for vendored external deps), build
  output in a git-ignored `dist/`. Place new files by that contract; a top-level
  `vendor/`, `test/`, or `build/` is nonstandard.
<!-- audit: id="arch-standard-layout" check="top-level layout matches docs/project-structure.md: src/ + tests/ present, deps under lib/{first,third}-party/ (no top-level vendor/), build output in git-ignored dist/ (deterministic counterpart: architecture-rules.json structure block)" severity="warn" applies="all" -->

The deterministic counterpart: declare the project's layout in the `structure`
block of `.claude/architecture-rules.json` and run **`grm-architecture-audit`**;
adapt an existing project to the standard with **`grm-structure-migrate`**.

## Dependency management
- Justify each third-party dependency; prefer the standard library where
  practical.
- Vendored dependencies live under `lib/third-party/<dep>/` (the
  **`grm-sync-deps`** target); the project's own shared libraries under
  `lib/first-party/`.

## Cross-cutting concerns
- Error handling, logging, **telemetry** (see [coding-standards.md](coding-standards.md) §Telemetry),
  configuration, and security follow one consistent approach across the codebase.
  Telemetry hooks must respect the layer boundaries above — instrument at the
  edges (entry points, error sinks), not by threading telemetry calls through
  the domain layer.

## When to write a design doc
- Any new feature / subsystem gets a `docs/design/{feature}-design.md` before
  implementation — use the **`grm-design-doc-scaffold`** skill.

---

*Per-language sub-docs (`coding-standards/*.md`) should cross-reference the
preferred patterns above for language-specific idioms (e.g. how Rust modules or
Python packages express the module-boundary rule).*
