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

## Cross-repo extraction policy: rule-of-two vs rule-of-three

The section above governs duplication *within* a repo. Across sibling repos in
the fleet, the extraction trigger differs by what's actually known — the
distinction is **"duplication observed" vs "duplication predicted."**

- **Rule-of-three (duplication *predicted*, unchanged).** A genuinely
  speculative shared crate — nothing exists yet, no duplication has been
  observed anywhere — waits for a third concrete need before extraction.
  Speculative generalization ahead of real callers tends to guess the wrong
  shape, so this hedge stays.
- **Rule-of-two (duplication *observed*).** Once a cataloged capability (the
  `provides`/`requires` vocabulary in `component-taxonomy.md` — `auth`,
  `http-client`, `http-server`, `persistence`, `telemetry`, `messaging`,
  `config`, `design-language`) has been hand-rolled a **second** time across
  sibling repos, retroactive tolerance is no longer defensible: the risk isn't
  hypothetical, it already happened once. Waiting for a third under a
  rule-of-three is exactly the policy that let the fleet's `auth` capability
  get hand-rolled five times before extraction (#202-#204's gatekeeper). The
  **second** hand-rolling of a cataloged capability requires filing an
  extraction ticket (the #202-#204 standard-package pattern:
  token-bookkeeper/gatekeeper/recordkeeper) before the work item that
  introduces it is accepted. A **third** hand-rolling is a planning-gate
  block, not a warning.
  <!-- audit: id="cross-repo-duplication" check="a cataloged capability (component-taxonomy provides/requires vocabulary) hand-rolled a second time across sibling repos has a filed extraction ticket before the introducing work item is accepted; a third hand-rolling blocks at the planning gate" severity="warn" applies="all" -->

This rule is mechanism-only here (the hint above, discoverable by any consumer
that greps `docs/coding-standards.md` for `audit: id=`); it does not require a
live fleet scan to define. Detecting an actual second/third hand-rolling across
real repos is `grm-fleet-audit`'s capability-overlap checklist item (its own
heuristic grep-set data structure).

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

## Logging

Distinct from §Telemetry above (*what the app did*): logging is the app's
operational text stream. Every Grimoire-managed project emits **structured,
JSON-lines logging to stdout** — one object per line, no other format. This
is a MUST: the Admin Console's log viewer (AC-4, catalog Entry 1) and the
catalog's boot-line conformance check (Entry 9) both depend on it. Full
design, rationale, and acceptance evidence:
`docs/grimoire/design/logging-spec-design.md`.

**Field contract** — every line is one JSON object with exactly:

| Field | Type | Meaning |
|---|---|---|
| `ts` | int | Ms since Unix epoch. |
| `level` | string | `trace`/`debug`/`info`/`warn`/`error` (Python's `WARNING`/`CRITICAL` → `warn`/`error`). |
| `target` | string | Emitting module/logger name. |
| `msg` | string | Message text. |
| `correlation_id` | string | Empty `""` when idle; ambient (`set_correlation_id`), never a call-site arg. |
| `instance` | string | `INSTANCE_ID` env var, default `"local"`. |
| `version` | string | The running build's version. |

Extra fields are allowed but never required; a plain-text line (the
pre-#435 `env_logger` default) is a hard failure.
<!-- audit: id="logging-json-lines" check="the app's first stdout line at boot is a JSON object carrying exactly ts/level/target/msg/correlation_id/instance/version with the documented types; verified deterministically by logging_conformance.py's offline scan + --boot-probe leg" severity="warn" applies="cli,gui,service,web" -->

**Level via instance config** (Rust `config.rs`'s `LOG_LEVEL`; Python
`init_logging`'s `level` param), never hardcoded. **Rotation stays the
supervisor's job** — it already captures stdout/stderr into `logs/`
(`web-app-deployment-protocol.md` §4); this spec governs stdout's *shape*
only.

**Starter init modules** (copied code, not a shared crate/package — see
§Cross-repo extraction policy): ONE call at process start, no per-log-site
boilerplate after. **Rust** — `logging_init.rs`, ships in the
`cli`/`gui`/`service` templates (`tracing`/`tracing-subscriber`); call
`logging_init::init(&cfg.log_level, &logging_init::instance_id(),
env!("CARGO_PKG_VERSION"))` first in `main`. **Python** —
`logging_init.py`, a copy-paste reference impl in
`docs/coding-standards/python.md` §Logging (no Python template exists yet);
call `logging_init.init_logging(level=..., version=...)` at process start.

**Conformance check.**
`.claude/skills/grm-required-feature-catalog/logging_conformance.py`
(catalog Entry 9): an offline call-site scan plus a live `--boot-probe CMD`
leg spawning the app's real entrypoint, capturing its first stdout line,
and validating it against the contract above.

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

### Post-commit test + coverage gate (v3.99, #361)

The merge-gate above catches a red suite at merge time. `code-quality.post-commit-test-gate`
adds an **earlier, automatic** signal at commit time — a REAL git `post-commit`
hook (unlike this project's other 8 guard hooks, all Claude Code PreToolUse
hooks that only fire for a Bash tool call Claude Code itself issues, this one
fires for any commit, from any actor). Off by default (opt-in):

```json
"code-quality": {
  "coverage-threshold": 80,
  "post-commit-test-gate": {"enabled": true, "mode": "force-correct"}
}
```

| Field | Values (default **bold**) | Effect |
|---|---|---|
| `enabled` | **`false`** / `true` | Opt-in; absent or `false` is a silent no-op. |
| `mode` | `block` / **`force-correct`** / `advisory` | `force-correct`: report + nonzero exit on red/sub-threshold, never blocks the commit (post-commit fires after the commit already happened). `advisory`: report only, always exits 0. `block`: governs the OPTIONAL `pre-commit` variant below — post-commit itself still force-corrects. |

**What it runs:** `recipe.py unit-test` (#360) plus, if the project has
declared one, a coverage command via `.claude/recipes.json`'s `extras.coverage`
entry (the same informational-`extras` convention `smoke-visual` uses — never
part of the versioned build-recipe INTERFACE):

```json
"extras": {
  "coverage": {
    "command": "pytest --cov=src --cov-report=term-missing",
    "implemented": true,
    "parser": "pytest-term-missing"
  }
}
```

`parser` selects one of four built-in output parsers matching the issue's
per-stack coverage runners: `pytest-term-missing`, `vitest-text`,
`cargo-llvm-cov`, `go-cover`. No `extras.coverage` entry ⇒ the coverage step
is skipped (advisory note in the report) — unit-tests still gate on their own.

**Coverage floor** is read from the existing `code-quality.coverage-threshold`
above — reused, not reinvented. `null` = advisory only, never forces
correction on coverage (a red suite still does, independently).

**Escape hatch:** `GRIMOIRE_SKIP_POST_COMMIT_GATE=1` (human-set in the shell
before commit, never automated) — the `post-commit` analog of this project's
existing `--no-verify` / `RELEASE_SKIP_VERIFY` convention (`release.sh`). The
OPTIONAL `pre-commit` variant (below) uses git's own `--no-verify` instead.

**OPTIONAL `pre-commit` hard block:** when `mode: "block"`, a second real git
hook (`.claude/hooks/pre-commit`) runs the same fast unit-test check and
refuses the commit (nonzero exit) on red — for projects that want a hard gate
instead of force-correction. `git commit --no-verify` bypasses it. Any mode
other than `block` degrades this hook to a silent no-op, so wiring it into
`core.hooksPath` is always safe regardless of the configured mode.

**Activation (one-time, opt-in):** git does not consult `.claude/hooks/` by
default —

```bash
git config core.hooksPath .claude/hooks
```

`grm-install-doctor` audits both the hook's `HOOK_CONTRACT` stamp (byte
content, same mechanism as the other 8 guard hooks) AND this activation step
(a correctly-stamped hook git never actually invokes is not truly installed).
Full design: `docs/grimoire/design/runtime-verification-design.md` §Post-commit
test + coverage gate. Install steps: `grm-repo-init/SKILL.md` §Post-commit
test gate (opt-in).

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
- The optional `test`, `unit-test`, `db-up`, and `db-down` recipes follow the patterns in the quick-start templates.

### `unit-test` — the fast subset (#360)

`test filter="" watch=""` runs the **full** suite (unit + integration/e2e/slow).
`unit-test filter="" watch=""` is the same shape, restricted to fast, isolated
unit tests only — `test` is unchanged; `unit-test` is strictly a faster subset
of the same signal, not a replacement for it in a merge gate. Per-stack mapping
(reuse existing conventions — never invent new marks):

| Stack | `unit-test` command | Excludes |
|---|---|---|
| Python | `pytest -m "not slow and not integration"` | tests marked `@pytest.mark.slow` / `@pytest.mark.integration` ([coding-standards/python.md](coding-standards/python.md) §Testing) |
| JS/TS | `vitest run` (unit config) | the `tests/`/`e2e/` integration directory ([coding-standards/javascript.md](coding-standards/javascript.md) §Testing) |
| Rust | `cargo test --lib` | crate-root `tests/` integration tests ([coding-standards/rust.md](coding-standards/rust.md) §Testing) |
| Go | `go test -short ./...` | tests gated behind `testing.Short()` |

`recipe.py unit-test` / `just unit-test` follow the same exit-code contract as
every other target: child exit code passed through; unimplemented → exit 2
(advisory), never a silent no-op. Durable design record:
`docs/grimoire/design/runtime-verification-design.md` §Unit test vs. full test
run.

Full specification: [`docs/design/justfile-standard-design.md`](design/justfile-standard-design.md).

`grm-install-doctor` enforces this contract. Run it to check for MISSING or PARTIAL recipes.

### `gui-test` — platform-differentiated GUI feature test (#362)

`gui-test baseline="main"` parallels `smoke`: a stable, platform-differentiated
verb that proves a specific GUI feature actually works, not just that the app
loads. Two strategies behind the one name:

- **Web** — not a scriptable shell recipe (a shell command cannot drive a
  browser-automation tool). The GUI test IS the agent session itself
  exercising the changed flow — navigate to the affected page, drive the
  actual interaction, read back the resulting DOM/console state — attached as
  verify-evidence in the completion report. `gui-test` stays a documented,
  permanently-advisory stub (`# grimoire:placeholder`, exit 2) on a web
  project rather than faking a pass/fail it cannot observe.
- **Desktop** — the app boots headlessly (no display required, the same
  approach `--smoke-test` already uses) and diffs a deterministic capture of
  what the UI drew against a committed baseline at
  `tests/gui-baselines/<baseline>.snapshot` (or `.png` for a project that
  layers on real pixel rendering). A missing baseline is always a hard
  failure — never a silent pass, matching `smoke-visual`'s convention.

Same exit-code contract as every other target: 0 pass, nonzero probe/diff
failure, 2 unimplemented-advisory (a non-GUI project never owes this).
GUI-only vocabulary — pre-filled on `web`/`native` stack presets, absent on
`server`/`cli`/`library` (no GUI surface to test). Durable design record:
`docs/grimoire/design/runtime-verification-design.md` §GUI testing.

`grm-install-doctor` enforces this contract the same way it enforces
`unit-test`'s presence/placeholder classification.

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
| Cross-repo duplication (rule-of-two) | 1 | `coding-standards.md` |
| One class/module per file | 1 | `coding-standards.md` |
| Dependency hygiene | 1 | per-language |
| Per-language idioms | ≥1 each | `python.md`, `javascript.md`, `rust.md` |
| Content / process leakage | 3 | `coding-standards.md` |
| Structured logging (JSON-lines shape) | 1 | `coding-standards.md` |

Add a row and a hint when a new dimension or language is introduced.
