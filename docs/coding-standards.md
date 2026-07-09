# Coding Standards

Standards every project built on this scaffolding should follow. The
cross-cutting rules below apply to all code; technology-specific rules live in
the per-language sub-documents linked at the bottom. Read the relevant sub-doc
alongside these defaults.

> This is the authoritative, expanded home for the brief "Coding practices"
> summary in the agent guide. When the two disagree, this document wins.

> **Stub** — seed bullets are placeholders. Expand each with examples and
> rationale as the standards mature.

## Standard practices (all languages)

- **Reusable & generic** — write code that generalises; extract shared logic
  rather than copy-pasting. No duplicated code (DRY).
  <!-- audit: id="dry-no-duplication" check="no copy-pasted logic across files; repeated blocks (>N lines, ≥2 sites) factored into a shared unit and made discoverable" severity="warn" applies="all" -->
- **Single responsibility** — one class/module per file, with a brief summary
  comment atop each describing its purpose.
- **Object-oriented design** — model the domain with classes and objects;
  favour established OO patterns over ad-hoc procedural code.
- **Base classes & inheritance** — factor shared behaviour into base classes and
  extend through inheritance rather than duplicating logic across types.
- **Handle error conditions** — validate inputs and fail loudly; never swallow
  errors silently.
- **Test every unit** — each function/unit has a test that covers the error
  paths, not just the happy path.
- **No magic numbers** — name constants and explain any non-obvious value.
  <!-- audit: id="no-magic-numbers" check="non-obvious literals are named constants with an explanatory name/comment" severity="warn" applies="all" -->
- **Readable over clever** — optimise for the next reader; keep functions small
  and names descriptive.
- **Instrument by default** — ship telemetry from day one; see §Telemetry below.
  <!-- audit: id="telemetry-default" check="project emits telemetry; startup + unhandled-exception/error events are instrumented" severity="warn" applies="all" -->

## DRY & duplication remediation

DRY is enforced, not just stated: `grm-code-health` runs a cross-file duplication
pass (`jscpd`) that the v1.26 `code-quality` `audit-gate` dial can warn or block
on, and `grm-coding-practices-audit` checks the `dry-no-duplication` hint above.
When a duplicate is found, resolve it along this ladder rather than leaving the
copies in place:

1. **Lift** the duplicated block into a shared function, base class, or module
   (the object-oriented / base-class guidance above).
2. **Generalize** it so it carries no caller-specific values — inject them
   instead (the *genericity over specificity* rule in
   `architecture-guidelines.md`).
3. **Register** the extracted unit via the **`grm-component-registry`** so the next
   consumer discovers and reuses it instead of re-duplicating. This closes the
   loop between "duplication detected" and "reuse made discoverable."

A duplicate that is genuinely coincidental (two blocks that look alike but model
different concerns) is *not* lifted — note why in a comment so the next pass
doesn't re-flag it.

## Telemetry instrumentation (default practice)

Projects built on this scaffolding **instrument by default** — telemetry is a
first-class concern during the initial build, not a retrofit. Instrumentation
that is added retroactively is expensive and leaves blind spots in how the
application is actually used.

Telemetry splits into two tiers:

**Always instrument (every project, regardless of type):**
- **Application startup** — process/app start, version, and config fingerprint.
  <!-- audit: id="telemetry-startup" check="application startup is instrumented (start event with version)" severity="warn" applies="all" -->
- **Unhandled exceptions & fatal errors** — every uncaught error path emits an
  event with enough context to triage.
  <!-- audit: id="telemetry-errors" check="unhandled exceptions and fatal errors emit telemetry events" severity="error" applies="all" -->

**Instrument when relevant (by project type):** use `grm-workflow-bootstrap`'s
detected project type to pick the surface, and confirm with the team which
events are business-critical.

| Project type | Minimum viable surface |
|---|---|
| **Web / GUI** | page/screen visits, navigation events, meaningful interactions (button clicks, form submissions, feature engagement) <br><!-- audit: id="telemetry-web-interactions" check="page/screen visits, navigation, and key user interactions are instrumented" severity="info" applies="web,gui" --> |
| **API / service** | request hits per endpoint, error rates, latency percentiles, downstream-call traces <br><!-- audit: id="telemetry-api-requests" check="per-endpoint request counts, error rates, latency, and downstream traces are instrumented" severity="info" applies="api,service" --> |
| **CLI** | command invocations, flags used, exit codes, fatal-error events <br><!-- audit: id="telemetry-cli-invocations" check="command invocations, flags, and exit codes are instrumented" severity="info" applies="cli" --> |

**"What is relevant" heuristic.** Start from the project-type row above, then add
any event the team identifies as a business-critical signal (a conversion, a
quota breach, a security-relevant action). When unsure whether an event belongs
in "always" vs "when relevant": if its absence would leave you blind to a
failure or to core usage, it is "always."

**Boundaries (cross-link to architecture).** Emit telemetry at the *edges* —
entry points and error sinks — not by threading telemetry calls through the
domain layer (see `architecture-guidelines.md` §Separation of concerns).

**Out of scope of this standard:** choosing a specific provider/SDK (per-project
config), data retention / PII / privacy policy, and dashboard/alerting setup.
Per-language sub-docs note *where* telemetry hooks integrate idiomatically in
that language.

## Merge-gate quality enforcement (v1.26)

Under the autonomous release pipeline, `grm-release-phase-merge` runs a **quality
gate** on each merged branch before ticking the §5 ledger, governed by the
`code-quality` block in `.claude/grimoire-config.json`. All dials default to
safe (non-blocking / off), so a project opts into strictness. Authoritative
design: the merge-gate quality spec, maintained in the upstream Grimoire repo.

| Dial | Values (default **bold**) | Effect |
|------|---------------------------|--------|
| `audit-gate` | `off` / **`warn`** / `block` | Runs `grm-coding-practices-audit` on the branch diff. `warn` files new gaps + proceeds; `block` rolls the merge back. |
| `auto-reviewer` | `off` / **`noir`** / `always` | Auto-spawns a `grm-reviewer`; blocking findings stop the merge. `noir` = Noir paradigm only. |
| `coverage-threshold` | **`null`** / `0-100` / `"delta"` | Floor (or no-drop) on test coverage; a miss stops the merge. |
| `typecheck` | `off` / **`build`** | Folds `{typecheck-command}` into the build gate so "build passes" implies "types check". |

A blocked merge rolls back to `ORIG_HEAD` (no partial state) and leaves the §5
row unticked with a recorded reason, re-runnable once the branch is fixed. The
gate reads config **live** — no schema-version bump, no file-swap.

## Justfile standards

All Grimoire projects must expose three canonical task-runner recipes in their `justfile`:

| Recipe | Signature | Semantics |
|---|---|---|
| `build` | `build env="dev"` | Compile/package the project. `env=prod` for release artifacts. |
| `run` | `run env="dev" port="8080"` | Start the application locally. |
| `deploy` | `deploy env dry_run="false"` | Deploy to a live environment. `env` is required (no default). |

**Key rules:**
- `deploy env` has **no default** — it is a positional required parameter. `just deploy` with no argument exits non-zero, preventing accidental deployments.
- Unimplemented recipes must carry a `# grimoire:placeholder` comment in the body so `grm-install-doctor` can detect them as PARTIAL rather than OK.
- The optional `test`, `db-up`, and `db-down` recipes follow the patterns in the quick-start templates.

Full specification: [`docs/design/justfile-standard-design.md`](design/justfile-standard-design.md).

`grm-install-doctor` enforces this contract. Run it to check for MISSING or PARTIAL recipes.

## Per-language / per-technology standards

Technology-specific rules live in sub-documents under `coding-standards/`. Add
a row when you introduce a new language or technology to the project.

| Technology | Standards |
|------------|-----------|
| HTML       | [coding-standards/html.md](coding-standards/html.md) |
| CSS        | [coding-standards/css.md](coding-standards/css.md) |
| JavaScript / TypeScript | [coding-standards/javascript.md](coding-standards/javascript.md) |
| Python     | [coding-standards/python.md](coding-standards/python.md) |
| Rust       | [coding-standards/rust.md](coding-standards/rust.md) |
| Tooling (lint/format/pre-commit) | [coding-standards/tooling.md](coding-standards/tooling.md) |
| *(add rows as sub-docs are created)* | |

## Audit-hint coverage (v1.27)

The `grm-coding-practices-audit` surface is the set of `<!-- audit: … -->` hints in
this doc and the per-language sub-docs. Adding a hint grows the audit with **no
skill change**. Current coverage by dimension:

| Dimension | Hints | Where |
|---|---|---|
| Naming / magic numbers | 1 | `coding-standards.md` |
| Error handling | 1 | `coding-standards.md`, per-language |
| Duplication (DRY) | 1 | `coding-standards.md`, per-language |
| One class/module per file | 1 | `coding-standards.md` |
| Dependency hygiene | 1 | per-language |
| Per-language idioms | ≥1 each | `python.md`, `javascript.md`, `rust.md` |

Add a row and a hint when a new dimension or language is introduced.
