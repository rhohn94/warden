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
  event with enough context to triage. **Boundary (#345):** this rule applies
  to *release-boundary-invoked* skills — the ones a release pipeline itself
  runs unattended, where a silent failure would otherwise go unnoticed (e.g.
  `grm-release-phase-merge`, `grm-project-release`; see those SKILL.mds'
  Telemetry sections). A standalone skill script invoked directly by an agent
  or a human is not a release boundary and is exempt by default — it opts in
  with one line via the shared `telemetry_entry.py` helper (`.claude/skills/
  grm-token-measure/telemetry_entry.py`, `@instrument` decorator around
  `main()`), rather than every script hand-rolling exception telemetry.
  <!-- audit: id="telemetry-errors" check="unhandled exceptions and fatal errors emit telemetry events at release boundaries; standalone skill scripts opt in via telemetry_entry.py" severity="error" applies="all" -->

**Instrument when relevant (by project type):** use `grm-workflow-bootstrap`'s
detected project type to pick the surface, and confirm with the team which
events are business-critical.

| Project type | Minimum viable surface |
|---|---|
| **Web / GUI** | page/screen visits, navigation events, meaningful interactions (button clicks, form submissions, feature engagement) <br><!-- audit: id="telemetry-web-interactions" check="page/screen visits, navigation, and key user interactions are instrumented" severity="info" applies="web,gui" --> |
| **API / service** | request hits per endpoint, error rates, latency percentiles, downstream-call traces <br><!-- audit: id="telemetry-api-requests" check="per-endpoint request counts, error rates, latency, and downstream traces are instrumented" severity="info" applies="api,service" --> |
| **CLI** | command invocations, flags used, exit codes, fatal-error events <br>**Boundary (#346):** same split as `telemetry-errors` above — required at release boundaries, opt-in elsewhere via `telemetry_entry.py`'s `@instrument(record_success=True)`, which also records a clean invocation's argv/flags/exit code. <br><!-- audit: id="telemetry-cli-invocations" check="command invocations, flags, and exit codes are instrumented at release boundaries; standalone skill scripts opt in via telemetry_entry.py" severity="info" applies="cli" --> |

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

## Content & UI copy (no context leakage)

The app is the app; the documentation is the documentation. Anything a project
ships — page copy, code comments, and changelog entries — carries only the
content its own audience needs, and never a trace of the process that produced
it. Apply this every time UI copy, a comment, or a changelog entry is written,
not just when explicitly asked.

- **No marketing/explanatory copy in the product.** A page ships functional
  copy only: labels, actions, statuses, error messages, empty states. It does
  not lead with a paragraph selling or explaining what the page does — that
  belongs in `docs/`, not in the shipped UI.
  <!-- audit: id="ui-no-marketing-copy" check="pages/screens carry only functional copy (labels, actions, status, errors); no explanatory or marketing prose framing the page's purpose" severity="warn" applies="web,gui" -->
- **No process leakage in comments.** A comment explains a non-obvious WHY (a
  constraint, an invariant, a workaround) — never the task, ticket, prompt, or
  session that produced the change. "Added per issue #123" or "per the user's
  request" does not belong in source.
  <!-- audit: id="no-context-leakage-comments" check="comments contain no references to tasks, ticket/issue numbers, prompts, or session context — only durable rationale about the code itself" severity="warn" applies="all" -->
- **No process leakage in front-facing docs.** `changelog.md` entries, design
  docs, and any other *user-facing* project documentation describe
  user-visible outcomes in the project's own voice — never a ticket ID, an
  internal task name, or a restated prompt/instruction.
  <!-- audit: id="no-context-leakage-docs" check="changelog.md entries and user-facing project docs (design docs, README) contain no ticket/issue IDs, task names, or prompt/session references — user-facing language only" severity="warn" applies="all" -->
- **Exception — internal engineering ledgers.** `version-history.md` (the
  complete internal release record) and `roadmap.md`/`release-planning/*.md`
  (project-management ledgers) are explicitly **exempt** from the rule above —
  they are never shown to end users, and ticket/issue cross-references are
  doing real traceability work there. This exemption is conditional: it holds
  only as long as a genuinely clean, front-facing `changelog.md` exists
  alongside `version-history.md`. A project with no `changelog.md` (or one
  that itself leaks process context) is not covered by this exception.
- **Exception — the framework-internal "Bulkhead" tier.** `docs/grimoire/`
  (design specs, integration workflow, and related framework-internal docs) is
  likewise **exempt**: it's excluded from every distributable build
  (`is_excluded()`/`EXCLUDED_PATH_PREFIXES` in `build_distributables.py`) and
  never shown to an end user, so ticket/issue cross-references there are
  internal traceability, not leakage — consistent with `docs/grimoire/`'s
  existing Bulkhead treatment elsewhere in doc-assurance (lean-index,
  monolith-cap). `docs/design/` stays fully covered by the rule.
- **Exception — issue-number traceability in *this* framework repo's source
  comments (#348).** `no-context-leakage-comments` targets comments that
  narrate the *task* (a ticket, a prompt, a session) instead of the *code*'s
  durable rationale. A bare issue-number cross-reference in a framework repo
  that dogfoods its own tooling is different: this repo's own skills, scripts,
  and docs are the product, its issue tracker is the durable spec/design
  record for *why* a given line exists (not a throwaway task descriptor), and
  the comment convention is deliberate and consistent (this repo's 393-hit
  `#NNN`-style comment-cross-reference convention as of v3.87 audit is
  intentional, not drift to clean up). Such
  cross-references are **permitted** here, provided the comment still carries
  the durable WHY alongside the number (e.g. "dedup by requestId (#82)", not
  a bare "#82"). This exception is scoped to source comments in *this*
  grimoire-framework repo; it does not relax the rule for projects built on
  this scaffolding, where `#NNN` in a comment is still process leakage into a
  product that isn't itself the issue-tracked artifact. No existing comment is
  swept or rewritten as part of recording this exception.

## Merge-gate quality enforcement (v1.26)

Under the autonomous release pipeline, `grm-release-phase-merge` runs a **quality
gate** on each merged branch before ticking the §5 ledger, governed by the
`code-quality` block in `.claude/grimoire-config.json`. All dials default to
safe (non-blocking / off), so a project opts into strictness. Authoritative
design: the merge-gate quality spec, maintained in the upstream Grimoire repo.

| Dial | Values (default **bold**) | Effect |
|------|---------------------------|--------|
| `audit-gate` | `off` / **`warn`** / `block` | Runs `grm-coding-practices-audit` on the branch diff. `warn` files new gaps + proceeds; `block` rolls the merge back. |
| `auto-reviewer` | `off` / **`noir`** / `always` | Auto-spawns a `grm-agent-reviewer`; blocking findings stop the merge. `noir` = Noir paradigm only. |
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
| Content / process leakage | 3 | `coding-standards.md` |

Add a row and a hint when a new dimension or language is introduced.
