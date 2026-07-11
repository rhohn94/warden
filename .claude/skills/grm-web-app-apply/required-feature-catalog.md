catalog-version: 8

# Required-feature catalog (Grimoire web-app)

This is the maintained, versioned catalog of **framework-mandated features**
that every Grimoire web app must have. It is the web-app analogue of
`onboarding/baseline-requirements.md`, scoped to the web-app fact
(`web-app.value: yes`).

Design authority: `docs/design/web-app-support-design.md` §5 (catalog format
§5.1, filing flow §5.2, first entry §5.3).

The catalog is read by the **filing flow** (§5.2): when `web-app.value` is set
(onboarding §6.5 or `grm-web-app-apply` §6), a Reporter files one
`Grimoire-Requirement`-tagged ticket per entry, deduplicated by `key`.

**Implementing** any catalog feature in a managed app is out of scope for the
catalog SPEC — it is planned and built by the managed project.

---

## Versioning

The `catalog-version: N` line on line 1 is the idempotency contract. Bump it
whenever an entry is **added** or its definition changes, so a later filing run
deduplicates correctly by checking the `key` against existing tagged issues.
Keys are **never reused or renamed** — retiring an entry is a migration (re-key
references first, then drop).

---

## Filing contract

Each entry is filed as one `Grimoire-Requirement`-tagged issue via
`grm-feedback-to-issue`. Before filing, search existing open **and closed** issues
tagged `Grimoire-Requirement` for the entry's `key` (carried in the issue title
as `[key: <key>]`). If a matching issue exists (any state), skip the entry.

Dedupe query (CLI fallback):

```bash
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list \
  --labels Grimoire-Requirement --state all
```

MCP equivalent: `list_issues` with `labels=["Grimoire-Requirement"]`.

---

## Conditional applicability (`applies-when`)

Most entries are **unconditional** — every Grimoire web app MUST have them, so
they carry no predicate and are always filed (modulo the dedupe above). Some
features, however, are only relevant to a **subset** of web apps. Such an entry
carries an optional **`applies-when:`** predicate; the filing flow evaluates it
against the managed app's live `.claude/grimoire-config.json` and **files the
entry only when the predicate holds**. An app the predicate excludes never
receives the ticket — the catalog does not spam apps with a requirement they do
not need.

**Predicate grammar (minimal, v1).** A single equality over a dotted config
path:

```
applies-when: <dot.path> == "<value>"
```

- `<dot.path>` is a dotted key into `grimoire-config.json` (e.g.
  `web-app.agentic`). The resolver reads the `value`-dial form transparently
  (`web-app.agentic` resolves `{"agentic": {"value": "yes"}}` **or** the flat
  `{"agentic": "yes"}` — the same `dialval` lookup `config_validate.py` uses).
- **Absence-as-default.** If the path is absent, the predicate is **false**
  (the entry does not apply). A conditional feature is opt-in: an app must
  positively declare the capability for the entry to file.
- An entry with **no** `applies-when:` line is unconditional (the status quo for
  Entries 1–2).

The single-equality grammar is deliberately minimal. A richer predicate language
(boolean combinators, comparisons) is a future extension and is **not** required
for the current entries — see `web-app-support-design.md` §5.1.

---

## Entries

### Entry 1 — Admin Console

```
key:  admin-console
name: Administrator Console
tag:  Grimoire-Requirement
```

**Spec.** Every Grimoire web app MUST provide an **Administrator Console** —
a single, unified administrative surface accessible to the
**Application Administrator** role. The console is always reachable at the
path `/admin-console`, with **no GUI navigation button required** — a direct
URL is always sufficient.

The console is not a user-facing feature; it is a framework-required operational
surface. Its absence must be treated as a missing baseline requirement.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| AC-1 | **Single role: Application Administrator.** The console is gated to this role only; no other role can access it. | A request without the Administrator credential receives a 401/403 response at `/admin-console`. |
| AC-2 | **All server telemetry visible.** The console shows all server-side telemetry: CPU, memory, request rates, error rates, uptime — whatever the runtime exposes — updated live or on demand. | A GET to `/admin-console` returns a page containing at least one server telemetry value (e.g. uptime or memory). |
| AC-3 | **Application configuration shown and editable.** The console displays the current application configuration and allows the Administrator to edit and persist changes. | Submitting a config change via the console updates the running config; the change is visible on reload. |
| AC-4 | **View, filter, and search all application logs.** The console provides a log viewer covering all application-level logs, with filter and search capabilities. | A search query in the log viewer returns matching log lines. |
| AC-5 | **Invoke-update control.** The console provides an explicit "check for / apply update" button that triggers the deployment-protocol self-update flow (`web-app-deployment-protocol.md` §6). | Clicking the invoke-update control initiates the self-update sequence (or reports "already up to date"). |
| AC-6 | **Server/admin-level config adjustment.** The console allows editing of administrator-level config: (a) resource limits, (b) dependent-service addresses, (c) dependent-service auto-start toggles. | Each of the three sub-config items is editable and persists across a restart. |
| AC-7 | **Restart-the-web-app button.** The console provides an explicit restart control that triggers a supervised restart of the web app process (via the service supervision verb set, `web-app-deployment-protocol.md` §4). | Clicking restart causes the app process to stop and restart; the `/healthz` endpoint becomes healthy again after restart. |
| AC-8 | **Grimoire section — framework version.** The console includes a dedicated Grimoire section showing the Grimoire framework version that built the running app, sourced from `grimoire-build-info.json` (`web-app-deployment-protocol.md` §8) field `framework-version`. | The Grimoire section displays a non-empty `framework-version` string matching the value in the live `grimoire-build-info.json`. |
| AC-9 | **Grimoire section — build-time config snapshot.** The Grimoire section additionally shows the full build-time Grimoire config snapshot, sourced from `grimoire-build-info.json` field `grimoire-config`. This is a snapshot frozen at build time and may differ from the current repo config. | The Grimoire section displays the `grimoire-config` object (or a human-readable rendering of it) from the live `grimoire-build-info.json`. |
| AC-10 | **Always reachable at `/admin-console`.** The console is always reachable by navigating directly to `/admin-console`, regardless of whether any GUI navigation button links to it. Buttons are optional; the path is not. | A direct GET to `/admin-console` (with valid Administrator credentials) returns HTTP 200 and the console UI. |

**Dedupe key in filed issue title:** `[key: admin-console]`

**Issue title (when filing):**
`[key: admin-console] Implement the Administrator Console (AC-1 through AC-10)`

**Issue body template:**

```markdown
**What:** Every Grimoire web app must implement an Administrator Console
reachable at `/admin-console` (no GUI button required). This issue tracks
the full spec for the console, sub-requirements AC-1 through AC-10.

**Sub-requirements:**
- AC-1: Single Application Administrator role (401/403 for others)
- AC-2: All server telemetry visible
- AC-3: Application config shown and editable
- AC-4: View/filter/search all application logs
- AC-5: Invoke-update control (triggers §6 self-update)
- AC-6: Server/admin-level config (resource limits, dependent-service
  addresses, dependent-service auto-start)
- AC-7: Restart-the-web-app button (triggers §4 supervisor restart)
- AC-8: Grimoire section — framework version (from grimoire-build-info.json)
- AC-9: Grimoire section — build-time config snapshot (from grimoire-build-info.json)
- AC-10: Always reachable at /admin-console (direct URL, no button required)

**Expected:** All AC-1 through AC-10 sub-requirements implemented and
independently testable per the testable criteria above.

**Context / source:** Grimoire required-feature catalog (catalog-version: 8);
authority: docs/design/web-app-support-design.md §5.3;
build-info contract: docs/web-app-deployment-protocol.md §8.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 2 — Changelog Surface

```
key:  changelog-surface
name: Changelog Surface
tag:  Grimoire-Requirement
```

**Spec.** Every Grimoire web app MUST surface its **changelog / release notes**
through the GUI. The changelog is exposed to **operators always** and to
**end users only when explicitly enabled** by a config toggle:

- **Operator-facing (always).** The Administrator Console (Entry 1) gains a
  **Changelog** section showing the application's release notes for at least the
  currently-serving version, sourced from the build-info `changelog` field
  (`web-app-deployment-protocol.md` §8). It is gated to the Application
  Administrator role like the rest of the console.
- **User-facing (toggle).** When the `changelog.user-facing` config dial is
  `on`, the app additionally exposes a public changelog surface reachable at the
  stable path **`/changelog`** for end users — no GUI navigation button required
  (the AC-10 always-reachable pattern). The dial defaults to `off`
  (absence-as-default); when `off`, `/changelog` is not exposed and only the
  operator surface exists.

The changelog is a **build-time snapshot** that travels with the bundled
version (the same rule as the build-info config snapshot), so a running or
rolled-back deployment always shows the notes of the version actually serving.

Design authority: `docs/design/changelog-surface-design.md`; build-info contract:
`docs/web-app-deployment-protocol.md` §8; config dial: `changelog.user-facing`.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| CL-1 | **Operator-facing changelog in the Admin Console (always).** | The Admin Console renders a Changelog section showing at least the currently-serving version's notes. |
| CL-2 | **Sourced from build-info.** The changelog data comes from `grimoire-build-info.json` field `changelog`, not a live remote fetch. | With `changelog` present in the live build-info the rendered notes match it; with it absent an honest empty-state shows (no error). |
| CL-3 | **`changelog.user-facing` dial gates a user surface.** When `on`, `/changelog` is reachable by an end user. | With the dial `on`, a GET to `/changelog` returns HTTP 200 and the changelog UI. |
| CL-4 | **Default off.** With the dial absent/`off`, no user-facing surface is exposed. | With the dial `off`, a GET to `/changelog` returns 404/forbidden; the operator surface still renders. |
| CL-5 | **Stable path, no button required.** When enabled, `/changelog` is always reachable directly regardless of any nav button. | A direct GET to `/changelog` (dial `on`) returns the surface without a discoverable link. |
| CL-6 | **Per-release rendering.** Each release entry shows its version and notes (date when available). | The rendered surface lists at least one release with a version identifier and its notes. |

**Dedupe key in filed issue title:** `[key: changelog-surface]`

**Issue title (when filing):**
`[key: changelog-surface] Implement the Changelog Surface (CL-1 through CL-6)`

**Issue body template:**

```markdown
**What:** Every Grimoire web app must surface its changelog through the GUI —
operator-facing always (Admin Console Changelog section), and user-facing at
`/changelog` when the `changelog.user-facing` config dial is `on` (default off).

**Sub-requirements:**
- CL-1: Operator-facing changelog in the Admin Console (always)
- CL-2: Sourced from grimoire-build-info.json `changelog` field (not live fetch)
- CL-3: `changelog.user-facing` dial gates a user surface at `/changelog`
- CL-4: Default off (no user surface unless enabled)
- CL-5: Stable path `/changelog`, no nav button required
- CL-6: Per-release rendering (version + notes)

**Expected:** All CL-1 through CL-6 implemented and independently testable per
the testable criteria above.

**Context / source:** Grimoire required-feature catalog (catalog-version: 8);
authority: docs/design/changelog-surface-design.md;
build-info contract: docs/web-app-deployment-protocol.md §8.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 3 — Token-Bookkeeper Standard Package (conditional)

```
key:          adopt-token-bookkeeper
name:         Token-Bookkeeper Standard Package
tag:          Grimoire-Requirement
applies-when: web-app.agentic == "yes"
```

**Spec.** A Grimoire web app that **runs its own agentic / LLM workloads** —
and therefore has token cost and throughput worth surfacing — MUST consume the
**`token-bookkeeper` standard package** through the **Dependency Channel**
rather than carry an in-tree equivalent. token-bookkeeper is the framework's
**standard package** for agentic token/cost/throughput bookkeeping: a
framework-blessed reusable library published as a `vendored-crate` artifact on
its release channel (`dependency-channel-design.md` §2), vendored and pinned
exactly like any other channel dependency.

This entry is **conditional** (`applies-when: web-app.agentic == "yes"`). An app
that does **not** declare `web-app.agentic: "yes"` — a static or
non-agentic web app — is not surfaced this requirement at all (absence-as-default
`no`; see *Conditional applicability* above). The capability dial
`web-app.agentic` is additive and absence-as-default (`web-app-support-design.md`
§1.3, §5.5); an app opts in by setting it when it begins surfacing its own
agentic cost.

As with Entries 1–2 the catalog is the **SPEC**: *implementing* the adoption
(vendoring the crate, retiring the in-tree fork, building against it) is the
managed project's scope, tracked by the filed `[key: adopt-token-bookkeeper]`
ticket. The vendoring follows the standard structure — vendored deps live under
`lib/third-party/<dep>/`, never a top-level `vendor/` (CLAUDE.md §Standard
project structure).

Design authority: `docs/design/web-app-support-design.md` §5.5 (standard-package
concept + applicability); Dependency Channel artifact contract:
`docs/grimoire/design/dependency-channel-design.md` §2; vendoring + conformance:
`grm-sync-deps` / `recipe.py vendor-check`.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| TB-1 | **Vendored via the Dependency Channel.** token-bookkeeper is declared in `vendor.toml` (with `channel`/`version`) and resolved in `vendor.lock`, its bytes committed under `lib/third-party/token-bookkeeper/`. | `vendor.toml` has a `[deps.token-bookkeeper]` entry, `vendor.lock` has a matching `tree_sha256`, and `lib/third-party/token-bookkeeper/` exists with the vendored bytes. |
| TB-2 | **No in-tree equivalent.** Any pre-existing in-tree telemetry-rollup / token-bookkeeping fork is retired in favour of the vendored crate. | No first-party module under `src/` duplicates token-bookkeeper's rollup logic; the app imports the vendored crate. |
| TB-3 | **Builds and tests against the vendored crate.** The app consumes the vendored copy, not a local re-implementation. | `recipe.py build` and `recipe.py test` pass with the in-tree fork removed and the vendored crate in place. |
| TB-4 | **Channel-conformant.** The pinned release is published on its channel and the vendored bytes match the lock. | `recipe.py vendor-check` (`dependency_channel_conformance.py`) reports no violation for `token-bookkeeper`. |

**Dedupe key in filed issue title:** `[key: adopt-token-bookkeeper]`

**Issue title (when filing):**
`[key: adopt-token-bookkeeper] Adopt the token-bookkeeper standard package (TB-1 through TB-4)`

**Issue body template:**

```markdown
**What:** This web app runs its own agentic/LLM workloads, so it must surface
its token cost/throughput via the **token-bookkeeper standard package**,
consumed through the Dependency Channel (vendor the published `vendored-crate`)
rather than an in-tree fork.

**Sub-requirements:**
- TB-1: Vendored via the Dependency Channel — `[deps.token-bookkeeper]` in
  vendor.toml, resolved in vendor.lock, bytes under
  `lib/third-party/token-bookkeeper/`
- TB-2: No in-tree telemetry-rollup / token-bookkeeping fork remains
- TB-3: `recipe.py build` + `recipe.py test` pass against the vendored crate
- TB-4: `recipe.py vendor-check` reports no violation for token-bookkeeper

**Expected:** All TB-1 through TB-4 implemented and independently testable per
the testable criteria above.

**Applicability:** Filed because `web-app.agentic == "yes"`. If this app does
not in fact surface its own agentic cost, close this ticket as not-applicable
and unset `web-app.agentic`.

**Context / source:** Grimoire required-feature catalog (catalog-version: 8);
authority: docs/grimoire/design/web-app-support-design.md §5.5;
Dependency Channel: docs/grimoire/design/dependency-channel-design.md §2.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 4 — Gatekeeper Standard Package (web-app auth; conditional)

```
key:          adopt-gatekeeper
name:         Gatekeeper Standard Package
tag:          Grimoire-Requirement
applies-when: web-app.value == "yes"
```

**Spec.** A Grimoire web app gets its **authentication** from the **`gatekeeper`
standard package** (`rhohn94/gatekeeper`) — enabling its `session` feature,
its `bearer` feature, or both — rather than re-deriving Argon2id password
login, hardened session cookies, or opaque API-token scopes in-tree. gatekeeper
is the framework's **standard package** for web-app auth (the standard-package
concept, `web-app-support-design.md` §5.5): a framework-blessed reusable Rust
library published as a `vendored-crate` artifact on its release channel
(`dependency-channel-design.md` §2), vendored and pinned exactly like any other
channel dependency. It unifies the four parallel auth implementations across the
fleet (goon-cave, issue-tracker, familiar, mission-control).

gatekeeper is **storage-agnostic**: it owns the crypto, cookie/token plumbing,
scope model, and Axum `FromRequestParts` extractors (`CurrentUser` / `AuthUser`
/ `AdminUser`), while the app wires the **store-trait seam**
(`UserStore` / `SessionStore` / `ApiTokenStore`) to its own database. It carries
no `sqlx` dependency. The store-trait seam is where recordkeeper (Entry 5)
provides the default implementation — see that entry's cross-reference.

This entry is **conditional** (`applies-when: web-app.value == "yes"`). The
catalog is already web-app-scoped, so this predicate is effectively "every
Grimoire web app that has an authenticated surface"; a purely static, no-auth
web app closes the filed ticket as not-applicable. (A finer capability dial —
e.g. `web-app.auth` — is a future refinement; `web-app.value` is the honest
current gate.)

As with Entries 1–3 the catalog is the **SPEC**: enabling the chosen feature(s),
wiring the store seam to a real store, and retiring the in-tree auth are the
managed project's scope, tracked by the filed `[key: adopt-gatekeeper]` ticket.
The vendoring follows the standard structure — vendored deps live under
`lib/third-party/<dep>/`, never a top-level `vendor/` (CLAUDE.md §Standard
project structure). The web quick-start template stamps a **scaffold seam** for
the feature choice + an in-memory reference store for first boot (a placeholder,
not a working auth backend); see `quick-start-templates-design.md`.

Design authority: `docs/grimoire/design/web-app-support-design.md` §5.6
(standard-package entry + applicability); Dependency Channel artifact contract:
`docs/grimoire/design/dependency-channel-design.md` §2; vendoring + conformance:
`grm-sync-deps` / `recipe.py vendor-check`; scaffold seam:
`docs/grimoire/design/quick-start-templates-design.md`.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| GK-1 | **Vendored via the Dependency Channel.** gatekeeper is declared in `vendor.toml` (with `channel`/`version`) and resolved in `vendor.lock`, its bytes committed under `lib/third-party/gatekeeper/`. | `vendor.toml` has a `[deps.gatekeeper]` entry, `vendor.lock` has a matching `tree_sha256`, and `lib/third-party/gatekeeper/` exists with the vendored bytes. |
| GK-2 | **Auth via gatekeeper, not an in-tree fork.** The app enables gatekeeper's `session`, `bearer`, or both, and uses its extractors/middleware rather than a first-party Argon2id/cookie/token implementation. | The app's `Cargo.toml` enables at least one gatekeeper feature; no first-party module under `src/` re-implements password hashing, session cookies, or API-token scopes. |
| GK-3 | **Store-trait seam wired.** The app implements `UserStore` / `SessionStore` / `ApiTokenStore` (as needed for its enabled features) against its own store (the reference in-memory store is acceptable only for first boot). | The app provides a concrete implementation of each required store trait; the app boots and an authenticated request succeeds end-to-end. |
| GK-4 | **Builds and tests against the vendored crate.** | `recipe.py build` and `recipe.py test` pass with the vendored gatekeeper in place and any in-tree auth fork removed. |
| GK-5 | **Channel-conformant.** The pinned release is published on its channel and the vendored bytes match the lock. | `recipe.py vendor-check` (`dependency_channel_conformance.py`) reports no violation for `gatekeeper`. |

**Dedupe key in filed issue title:** `[key: adopt-gatekeeper]`

**Issue title (when filing):**
`[key: adopt-gatekeeper] Adopt the gatekeeper standard package (GK-1 through GK-5)`

**Issue body template:**

```markdown
**What:** This web app must get its authentication from the **gatekeeper
standard package** (enable `session`, `bearer`, or both), consumed through the
Dependency Channel (vendor the published `vendored-crate`) rather than an
in-tree Argon2id/cookie/token implementation. Wire gatekeeper's store-trait
seam (`UserStore`/`SessionStore`/`ApiTokenStore`) to your database — recordkeeper
(Entry 5) is the default implementation.

**Sub-requirements:**
- GK-1: Vendored via the Dependency Channel — `[deps.gatekeeper]` in vendor.toml,
  resolved in vendor.lock, bytes under `lib/third-party/gatekeeper/`
- GK-2: Auth via gatekeeper (enable `session`/`bearer`); no in-tree auth fork
- GK-3: Store-trait seam (`UserStore`/`SessionStore`/`ApiTokenStore`) wired to a
  real store (in-memory reference store acceptable only for first boot)
- GK-4: `recipe.py build` + `recipe.py test` pass against the vendored crate
- GK-5: `recipe.py vendor-check` reports no violation for gatekeeper

**Expected:** All GK-1 through GK-5 implemented and independently testable per
the testable criteria above.

**Applicability:** Filed because `web-app.value == "yes"`. A purely static,
no-auth web app closes this ticket as not-applicable.

**Context / source:** Grimoire required-feature catalog (catalog-version: 8);
authority: docs/grimoire/design/web-app-support-design.md §5.6;
Dependency Channel: docs/grimoire/design/dependency-channel-design.md §2.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 5 — Recordkeeper Standard Package (database access; conditional)

```
key:          adopt-recordkeeper
name:         Recordkeeper Standard Package
tag:          Grimoire-Requirement
applies-when: web-app.value == "yes"
```

**Spec.** A Grimoire web app gets its **database access** from the
**`recordkeeper` standard package** (`rhohn94/recordkeeper`, codename "Vault")
rather than wiring `turso` / `sqlx` directly. recordkeeper is the framework's
**standard package** for database access (the standard-package concept,
`web-app-support-design.md` §5.5): a framework-blessed reusable Rust library
published as a `vendored-crate` artifact on its release channel
(`dependency-channel-design.md` §2), vendored and pinned exactly like any other
channel dependency.

recordkeeper exposes a **backend-neutral data-access API**
(`connect` / `execute` / `query` / `transaction` / `migrate` + `Row` / `Params`
/ `DbError`) over **two adapters**: native `turso` (the **default** feature —
embedded, single-binary, zero-daemon) and `sqlx::Postgres` (the `postgres`
feature, opt-in for apps needing true multi-writer concurrency or in-server
multi-DB/role separation). It owns the fleet's DB discipline: DSN / `APP_ENV`
handling, the SQLite-family pragma baseline
(WAL / `busy_timeout` / `synchronous=NORMAL` / `foreign_keys`), **per-backend
migration sets**, and beta/prod data separation.

**Key constraint (documented, not to be worked around).** Turso has no SQLx
driver, so the portable API is **runtime-checked**, not `sqlx::query!`
compile-time-checked; SQL dialect differs per backend, so migrations live in
**per-backend directories** with a documented portable subset. A Postgres-only
SQLx escape hatch is provided for apps on the `postgres` backend.

This entry is **conditional** (`applies-when: web-app.value == "yes"`). There is
**no dedicated persistence-need config signal** — the `environments` block's
per-env `data_isolation` field (`deploy-environment-design.md` §1) is a
per-environment data-isolation flag, not a project-level "this app persists
data" dial — so this entry gates on `web-app.value == "yes"`, which is
**imprecise**: a stateless web app (no persistent store) closes the filed ticket
as not-applicable. (A finer `web-app.persistence` dial is a future refinement;
`web-app.value` is the honest current gate.)

**Environment convention.** recordkeeper's DSN / `APP_ENV` handling aligns with
the framework's deploy-environment model — the `environments` block's named
environments (`local` / `dev` / `beta` / `production`) and the `APP_ENV` runtime
selection (`deploy-environment-design.md` §1–§2). Each environment supplies its
own DSN; the SQLite-family pragma baseline and per-backend migration sets are
applied per environment. The `recipe.py` `migrate` / `db-up` recipes align with
recordkeeper's per-backend migration sets (recipe-spec work, #201).

**Cross-reference (the store seam).** gatekeeper's store traits
(`UserStore` / `SessionStore` / `ApiTokenStore`, Entry 4) get their **default
implementation over recordkeeper** — recordkeeper is the persistence layer
gatekeeper's auth binds to. This is a documentation cross-reference; the adapter
itself is managed-project work.

As with Entries 1–4 the catalog is the **SPEC**: choosing the backend, wiring
`Db` connect, authoring the migration sets, and retiring direct `turso`/`sqlx`
use are the managed project's scope, tracked by the filed
`[key: adopt-recordkeeper]` ticket. Vendored deps live under
`lib/third-party/<dep>/`, never a top-level `vendor/` (CLAUDE.md §Standard
project structure). The web quick-start template stamps a **scaffold seam** — a
`Db` connect stub + an example per-backend migration-set skeleton (Turso default,
Postgres opt-in) — a placeholder, not a working data layer; see
`quick-start-templates-design.md`.

Design authority: `docs/grimoire/design/web-app-support-design.md` §5.7
(standard-package entry + applicability); Dependency Channel artifact contract:
`docs/grimoire/design/dependency-channel-design.md` §2; environment / DSN
convention: `docs/grimoire/design/deploy-environment-design.md` §1–§2; vendoring
+ conformance: `grm-sync-deps` / `recipe.py vendor-check`; scaffold seam:
`docs/grimoire/design/quick-start-templates-design.md`.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| RK-1 | **Vendored via the Dependency Channel.** recordkeeper is declared in `vendor.toml` (with `channel`/`version`) and resolved in `vendor.lock`, its bytes committed under `lib/third-party/recordkeeper/`. | `vendor.toml` has a `[deps.recordkeeper]` entry, `vendor.lock` has a matching `tree_sha256`, and `lib/third-party/recordkeeper/` exists with the vendored bytes. |
| RK-2 | **DB access via recordkeeper, not direct turso/sqlx.** The app connects through recordkeeper's API (`connect`/`execute`/`query`/`transaction`/`migrate`), selecting the `turso` default or the `postgres` feature. | No first-party module under `src/` calls `turso`/`sqlx` connection APIs directly; the app imports recordkeeper's data-access API. |
| RK-3 | **Per-backend migration set.** Migrations live in per-backend directories with a documented portable subset; `migrate` applies the correct set for the active backend. | A migration directory exists per used backend; `recipe.py migrate` (or recordkeeper's `migrate`) brings a fresh DB up to schema for the active backend. |
| RK-4 | **`APP_ENV` / DSN-per-env convention honored.** The active environment (and its DSN) is selected via `APP_ENV` per the `environments` block; the SQLite-family pragma baseline is applied. | Setting `APP_ENV` selects the matching environment's DSN; a connected SQLite-family backend reports WAL + `foreign_keys=on`. |
| RK-5 | **Builds and tests against the vendored crate.** | `recipe.py build` and `recipe.py test` pass with the vendored recordkeeper in place and direct turso/sqlx wiring removed. |
| RK-6 | **Channel-conformant.** The pinned release is published on its channel and the vendored bytes match the lock. | `recipe.py vendor-check` (`dependency_channel_conformance.py`) reports no violation for `recordkeeper`. |

**Dedupe key in filed issue title:** `[key: adopt-recordkeeper]`

**Issue title (when filing):**
`[key: adopt-recordkeeper] Adopt the recordkeeper standard package (RK-1 through RK-6)`

**Issue body template:**

```markdown
**What:** This web app must get its database access from the **recordkeeper
standard package** (default `turso` backend, opt-in `postgres` feature),
consumed through the Dependency Channel (vendor the published `vendored-crate`)
rather than wiring turso/sqlx directly. Honor the `APP_ENV`/DSN-per-env
convention and author per-backend migration sets.

**Sub-requirements:**
- RK-1: Vendored via the Dependency Channel — `[deps.recordkeeper]` in
  vendor.toml, resolved in vendor.lock, bytes under `lib/third-party/recordkeeper/`
- RK-2: DB access via recordkeeper's API (turso default / postgres opt-in); no
  direct turso/sqlx wiring
- RK-3: Per-backend migration set with a documented portable subset
- RK-4: `APP_ENV`/DSN-per-env convention honored; SQLite-family pragma baseline
  applied
- RK-5: `recipe.py build` + `recipe.py test` pass against the vendored crate
- RK-6: `recipe.py vendor-check` reports no violation for recordkeeper

**Note:** gatekeeper's store traits (Entry 4, `[key: adopt-gatekeeper]`) get
their default implementation over recordkeeper.

**Expected:** All RK-1 through RK-6 implemented and independently testable per
the testable criteria above.

**Applicability:** Filed because `web-app.value == "yes"`. A stateless web app
(no persistent store) closes this ticket as not-applicable.

**Context / source:** Grimoire required-feature catalog (catalog-version: 8);
authority: docs/grimoire/design/web-app-support-design.md §5.7;
Dependency Channel: docs/grimoire/design/dependency-channel-design.md §2;
environment convention: docs/grimoire/design/deploy-environment-design.md §1–§2.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 6 — Meta-Updater Standard Package (self-update; conditional)

```
key:          adopt-meta-updater
name:         Meta-Updater Standard Package
tag:          Grimoire-Requirement
applies-when: web-app.value == "yes"
```

**Spec.** A Grimoire web app gets its **health-gated self-update with
auto-rollback** (`web-app-deployment-protocol.md` §6) from the
**`meta-updater` standard package** (`rhohn94/meta-updater`) rather than
hand-rolling the check/download-verify/atomic-apply/health-gate/rollback
sequence in-tree. meta-updater is the framework's **standard package** for
web-app self-update (the standard-package concept, `web-app-support-design.md`
§5.5): a framework-blessed reusable Rust library published as a
`vendored-crate` artifact on its release channel
(`dependency-channel-design.md` §2), vendored and pinned exactly like any
other channel dependency.

meta-updater exposes `trait ReleaseChannel` (default `GithubChannel`) as the
storage-agnostic release source, a checksum-then-minisign verify-then-swap
pipeline, an allowlisted-asset-name + fixed-staging-dir + atomic-rename + N-1
rollback discipline, and an `UpdatePolicy` enum ({`Disabled`, `PromptOnly`,
`AutoWithinChannel`}) with a two-tier operator-vs-service default (service
binaries default `Disabled`). Full trait surface + invariants:
`docs/grimoire/design/meta-updater-package-design.md`.

**Not yet published.** Unlike Entries 3–5, the meta-updater library is
**spec-only** as of this entry's landing (v3.79) — extraction/implementation
is separate follow-up work (see the spec's Scope/Follow-ups). Vendoring
(MU-1) cannot complete until a release exists; the filed ticket records the
intended adoption shape so the managed project's own planning can account for
it, and a hand-rolled §6 implementation remains an acceptable interim path
until the package ships.

This entry is **conditional** (`applies-when: web-app.value == "yes"`) — the
same imprecise-but-honest gate Entries 4–5 use; a web app with no independent
deployment/update lifecycle closes the filed ticket as not-applicable.

As with Entries 1–5 the catalog is the **SPEC**: once published, vendoring the
crate, wiring the app's `ReleaseChannel` + `UpdatePolicy` choice, and retiring
any in-tree/hand-rolled §6 implementation are the managed project's scope,
tracked by the filed `[key: adopt-meta-updater]` ticket. Vendored deps live
under `lib/third-party/<dep>/`, never a top-level `vendor/` (CLAUDE.md
§Standard project structure). The web quick-start template stamps a
**scaffold seam** (`src/updater_seam.rs`) — a placeholder, not a working
updater; see `quick-start-templates-design.md`.

Design authority: `docs/grimoire/design/web-app-support-design.md` §5.8
(standard-package entry + applicability); full library spec:
`docs/grimoire/design/meta-updater-package-design.md`; §6 mandate:
`docs/web-app-deployment-protocol.md` §6; Dependency Channel artifact
contract: `docs/grimoire/design/dependency-channel-design.md` §2; vendoring +
conformance: `grm-sync-deps` / `recipe.py vendor-check`; scaffold seam:
`docs/grimoire/design/quick-start-templates-design.md`.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| MU-1 | **Vendored via the Dependency Channel.** meta-updater is declared in `vendor.toml` (with `channel`/`version`) and resolved in `vendor.lock`, its bytes committed under `lib/third-party/meta-updater/`. | `vendor.toml` has a `[deps.meta-updater]` entry, `vendor.lock` has a matching `tree_sha256`, and `lib/third-party/meta-updater/` exists with the vendored bytes. |
| MU-2 | **No in-tree equivalent.** Any pre-existing in-tree self-update fork (check/download/verify/apply/rollback) is retired in favour of the vendored crate. | No first-party module under `src/` duplicates meta-updater's update logic; the app imports the vendored crate. |
| MU-3 | **`ReleaseChannel` + `UpdatePolicy` wired.** The app wires its `ReleaseChannel` (the default `GithubChannel` or a custom implementation) and sets an explicit `UpdatePolicy`, honoring the service-tier `Disabled` default. | The app provides a concrete `ReleaseChannel` and an explicit `UpdatePolicy` value; a service-context deployment defaults to `Disabled` unless an operator has opted in. |
| MU-4 | **Builds and tests against the vendored crate.** | `recipe.py build` and `recipe.py test` pass with the in-tree fork removed and the vendored crate in place. |
| MU-5 | **Channel-conformant.** The pinned release is published on its channel and the vendored bytes match the lock. | `recipe.py vendor-check` (`dependency_channel_conformance.py`) reports no violation for `meta-updater`. |

**Dedupe key in filed issue title:** `[key: adopt-meta-updater]`

**Issue title (when filing):**
`[key: adopt-meta-updater] Adopt the meta-updater standard package (MU-1 through MU-5)`

**Issue body template:**

```markdown
**What:** This web app must get its health-gated self-update (§6) from the
**meta-updater standard package**, consumed through the Dependency Channel
(vendor the published `vendored-crate`) rather than an in-tree check/verify/
apply/rollback implementation.

**Sub-requirements:**
- MU-1: Vendored via the Dependency Channel — `[deps.meta-updater]` in
  vendor.toml, resolved in vendor.lock, bytes under
  `lib/third-party/meta-updater/`
- MU-2: No in-tree self-update fork remains
- MU-3: `ReleaseChannel` + `UpdatePolicy` wired (service-tier defaults to
  `Disabled`)
- MU-4: `recipe.py build` + `recipe.py test` pass against the vendored crate
- MU-5: `recipe.py vendor-check` reports no violation for meta-updater

**Expected:** All MU-1 through MU-5 implemented and independently testable per
the testable criteria above.

**Applicability:** Filed because `web-app.value == "yes"`. If this app has no
independent deployment/update lifecycle, close this ticket as not-applicable.

**Note:** As of catalog-version 6, meta-updater was spec-only — if no release
is yet published on its channel, note that in the ticket and track it as
blocked-on-upstream rather than closing it.

**Context / source:** Grimoire required-feature catalog (catalog-version: 8);
authority: docs/grimoire/design/web-app-support-design.md §5.8; full spec:
docs/grimoire/design/meta-updater-package-design.md; §6 mandate:
docs/web-app-deployment-protocol.md §6; Dependency Channel:
docs/grimoire/design/dependency-channel-design.md §2.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 7 — One Sanctioned Aura Consumption Mechanism (dependency channel)

```
key:          aura-via-dependency-channel
name:         Sanctioned Aura Consumption Mechanism (Dependency Channel)
tag:          Grimoire-Requirement
applies-when: repo consumes Aura by any detectable mechanism
```

**Spec.** A Grimoire-managed repo that consumes Aura (the design-language
producer) MUST do so through **one sanctioned mechanism**: the
**Dependency Channel** (`vendor.toml [deps.aura]`, resolved in `vendor.lock`,
bytes committed under `lib/third-party/aura/` — the same channel discipline
Entries 3–6 use for their standard packages). Any other consumption
mechanism — a git submodule, a hand-rolled token-vendoring script, or
committed codegen sourced from an untracked local clone — is a **straggler**
that ages independently of the fleet's Aura release cadence and defeats
fleet-wide Aura waves (a single motion can no longer update every consumer at
once).

Unlike Entries 3–6, this entry's `applies-when` is not a `grimoire-config.json`
dial — it is a **repo-state predicate** evaluated by direct detection (below),
because "does this repo consume Aura at all" is a fact about the repo's tree,
not a declared capability. A repo with no Aura consumption at all does not
match the detect condition and is never filed.

**Detect.** A repo matches this entry when it contains **Aura bytes or
Aura-derived output** — any of: a `lib/third-party/aura/` (or legacy
`vendor/aura/`) tree, a git submodule pointing at the Aura/design-language
repo, a script or snapshot directory (e.g. `tools/aura/`) holding
Aura-sourced tokens, or generated source committed from an Aura/design-language
clone — **without** a corresponding `vendor.toml` `[deps.aura]` block whose
`vendor.lock` entry matches the committed bytes. Presence of the bytes without
the channel block/lock pairing is the trigger; the channel-conformant case
(`[deps.aura]` + matching lock, as goon-cave / mission-control / familiar /
retro-game-player / discord-bot already run) does **not** match and is not
filed.

**Adopt.** The adoption path is the existing **`grm-vendor-migrate`** skill —
detect the straggler mechanism, replace it with a `vendor.toml [deps.aura]`
entry + `vendor.lock` resolution + `lib/third-party/aura/` bytes, and retire
the old mechanism. As with Entries 3–6 the catalog is the **SPEC**;
*implementing* the migration is the managed project's scope, tracked by the
filed `[key: aura-via-dependency-channel]` ticket. The v3.74 issue-filing
authority (`grm-feedback-to-issue` / the filing flow this catalog already
uses, §Filing contract above) is the mechanism that turns a detected
straggler into that per-repo `Grimoire-Requirement` ticket — this entry is
precisely its intended use.

#### Per-straggler guidance

The 2026-07-09 fleet architecture audit found three stragglers; each gets a
tailored migration note in its filed ticket:

| Repo | Current mechanism | Target |
|------|--------------------|--------|
| **issue-tracker** | Git submodule (`lib/third-party/aura`, currently uninitialized) | Channel asset-bundle. This is the **same motion retro-game-player already executed and documented** — cite `retro-game-player`'s `dependency-channel-conformance.md` as the **worked example** to follow verbatim (submodule → `vendor.toml [deps.aura]` asset-bundle + `vendor.lock`). |
| **music-collection** | Frozen `tools/aura/` Python token-vendoring snapshot (pinned at a v2.4-era Aura release, ~3.5 major versions stale) | Replace the frozen `tools/aura/` snapshot with a channel-sourced `tokens.resolved.json` — same artifact shape, sourced from the Dependency Channel instead of a hand-vendored script, so it ages with the rest of the fleet. |
| **obsidian** | Committed codegen (`aura_generated.rs`) built from an untracked `.design-language-source/` clone | Codegen stays (obsidian still generates `aura_generated.rs`); its **input** becomes a channel-pinned `tokens.resolved.json` instead of the untracked clone. The design-language producer already ships `tokens.resolved.json` as a channel artifact, so this is a source-swap, not a new build step. |

**Dedupe key in filed issue title:** `[key: aura-via-dependency-channel]`

**Issue title (when filing):**
`[key: aura-via-dependency-channel] Migrate to the sanctioned Aura consumption mechanism (Dependency Channel)`

**Issue body template:**

```markdown
**What:** This repo consumes Aura through a mechanism other than the
sanctioned Dependency Channel (a `vendor.toml [deps.aura]` block resolved in
`vendor.lock` with bytes under `lib/third-party/aura/`). Migrate via
`grm-vendor-migrate` so this repo ages with the rest of the fleet's Aura
consumers instead of needing bespoke handling on every wave.

**Detected mechanism:** {submodule | token-vendoring snapshot | committed
codegen from untracked clone} — filled in by the detecting agent from the
repo's actual tree.

**Migration guidance (per straggler, if this repo matches one):**
- issue-tracker: submodule → channel asset-bundle; follow
  retro-game-player's `dependency-channel-conformance.md` as the worked
  example.
- music-collection: frozen `tools/aura/` snapshot → channel-sourced
  `tokens.resolved.json`.
- obsidian: codegen stays; swap its input from the untracked
  `.design-language-source/` clone to a channel-pinned `tokens.resolved.json`
  (already shipped by the design-language producer).

**Expected:** A `vendor.toml [deps.aura]` entry, a matching `vendor.lock`
resolution, and Aura bytes under `lib/third-party/aura/`; the prior
submodule/snapshot/untracked-clone mechanism is retired.

**Context / source:** Grimoire required-feature catalog (catalog-version: 8);
detect/adopt spec: this entry (Entry 7); adoption mechanism: `grm-vendor-migrate`;
provenance: meta-planner fleet architecture audit 2026-07-09 (issue #316).
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 8 — Fleet-Contract Standard Package (fleet observability; conditional)

```
key:          adopt-fleet-contract
name:         Fleet-Contract Standard Package
tag:          Grimoire-Requirement
applies-when: web-app.value == "yes"
```

**Spec.** A Grimoire web app implements the **Fleet Status Contract**
(`docs/grimoire/design/fleet-status-contract.md`) — the `GET /fleet/v1/status`
endpoint and the `fleet-instance.json` deploy-time manifest — by consuming the
**`fleet-contract` standard package** (`rhohn94/fleet-contract`) rather than
hand-rolling the parsing/serialization and reconciliation logic in-tree.
fleet-contract is the framework's **standard package** for fleet observability
(the standard-package concept, `web-app-support-design.md` §5.5): a
framework-blessed reusable Rust library published as a `vendored-crate`
artifact on its release channel (`dependency-channel-design.md` §2), vendored
and pinned exactly like any other channel dependency. It follows the same
#202–#204 pattern as Entries 3–6 (token-bookkeeper, gatekeeper, recordkeeper,
meta-updater).

fleet-contract exists to retire three independent, drifting implementations of
the same contract found by the 2026-07-04 fleet audit (issue #287):
mission-control's `core/src/fleet.rs` (681 LOC), warden's
`scanner.rs`/`detector.rs`/`version_checker.rs` (~1,240 LOC), and this
framework's own `fleet_conformance.py`. The crate carries the contract types
for both halves (Half 1 — `/fleet/v1/status` full + minimal shapes; Half 2 —
`fleet-instance.json`), `serde` (de)serialization, `schema_version` N/N-1
tolerance (§3.2), the **reconciliation classifier** (manifest vs. endpoint vs.
release → in-sync / drift / declared-but-down / rogue verdict, §4) lifted from
mission-control's `fleet.rs`, and optionally a small Axum handler helper for
producers. Full crate contents: **Crate spec pointer** in
`fleet-status-contract.md`.

**Not yet published.** Like Entry 6 (meta-updater) at its landing, the
fleet-contract library is **spec-only** as of this entry — extraction into its
own repo, channel-published, is separate follow-up work (issue #287's Move).
Vendoring (FC-1) cannot complete until a release exists; the filed ticket
records the intended adoption shape so a managed project's own planning can
account for it, and hand-rolled §1/§2 implementations (Familiar's reference
`fleet.rs`, this framework's `fleet_conformance.py`) remain acceptable interim
paths until the package ships. `fleet_conformance.py` stays Python
(conformance checking, not the producer/consumer library) but validates
against the same **shared validation vectors**
(`fleet-contract-vectors/manifest.json` + `cases/*.json`, this skill
directory) the future crate's self-test will use, so Python and Rust are
proven to agree on the same fixtures rather than drifting onto hand-copied
ones.

This entry is **conditional** (`applies-when: web-app.value == "yes"`) — the
same imprecise-but-honest gate Entries 4–6 use: every deployed Grimoire web app
is a fleet-participating instance and is expected to expose the contract, so a
web app with no independent deployment/fleet-observability need (e.g. a
purely static asset bundled into a larger host app) closes the filed ticket as
not-applicable; a finer `web-app.fleet-participant` dial is a future
refinement.

As with Entries 1–7 the catalog is the **SPEC**: once published, vendoring the
crate, wiring the endpoint handler + manifest writer, and retiring any
in-tree/hand-rolled implementation are the managed project's scope, tracked by
the filed `[key: adopt-fleet-contract]` ticket. Vendored deps live under
`lib/third-party/<dep>/`, never a top-level `vendor/` (CLAUDE.md §Standard
project structure).

Design authority: `docs/grimoire/design/fleet-status-contract.md` (contract
spec + Crate spec pointer); standard-package concept:
`docs/grimoire/design/web-app-support-design.md` §5.5; Dependency Channel
artifact contract: `docs/grimoire/design/dependency-channel-design.md` §2;
vendoring + conformance: `grm-sync-deps` / `recipe.py vendor-check`; shared
vectors: `.claude/skills/grm-web-app-apply/fleet-contract-vectors/`.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| FC-1 | **Vendored via the Dependency Channel.** fleet-contract is declared in `vendor.toml` (with `channel`/`version`) and resolved in `vendor.lock`, its bytes committed under `lib/third-party/fleet-contract/`. | `vendor.toml` has a `[deps.fleet-contract]` entry, `vendor.lock` has a matching `tree_sha256`, and `lib/third-party/fleet-contract/` exists with the vendored bytes. |
| FC-2 | **No in-tree equivalent.** Any pre-existing in-tree Fleet Status Contract parsing/serialization/reconciliation fork is retired in favour of the vendored crate. | No first-party module under `src/` duplicates fleet-contract's type/parsing/reconciliation logic; the app imports the vendored crate. |
| FC-3 | **Endpoint + manifest wired against the crate's types.** `GET /fleet/v1/status` (full + minimal shapes) and the `fleet-instance.json` writer serialize through fleet-contract's types, not hand-rolled structs. | A live probe of `/fleet/v1/status` (unauth and, if applicable, auth) validates against the same shared vectors this catalog entry ships; `fleet-instance.json` deserializes with the crate's manifest type. |
| FC-4 | **Reconciliation via the crate's classifier (consumers only).** A consumer (e.g. Mission Control) computes in-sync/drift/declared-but-down/rogue verdicts using fleet-contract's classifier, not a re-derived comparison. | The consumer's reconciliation call site imports fleet-contract's classifier type/function rather than a first-party equivalent. |
| FC-5 | **Builds and tests against the vendored crate.** | `recipe.py build` and `recipe.py test` pass with the in-tree fork removed and the vendored crate in place. |
| FC-6 | **Channel-conformant.** The pinned release is published on its channel and the vendored bytes match the lock. | `recipe.py vendor-check` (`dependency_channel_conformance.py`) reports no violation for `fleet-contract`. |

**Dedupe key in filed issue title:** `[key: adopt-fleet-contract]`

**Issue title (when filing):**
`[key: adopt-fleet-contract] Adopt the fleet-contract standard package (FC-1 through FC-6)`

**Issue body template:**

```markdown
**What:** This web app must implement the Fleet Status Contract
(`/fleet/v1/status` + `fleet-instance.json`) via the **fleet-contract
standard package**, consumed through the Dependency Channel (vendor the
published `vendored-crate`) rather than hand-rolled parsing/reconciliation
logic in-tree.

**Sub-requirements:**
- FC-1: Vendored via the Dependency Channel — `[deps.fleet-contract]` in
  vendor.toml, resolved in vendor.lock, bytes under
  `lib/third-party/fleet-contract/`
- FC-2: No in-tree Fleet Status Contract fork remains
- FC-3: Endpoint + manifest wired against the crate's types
- FC-4: Reconciliation (if this app is a consumer) via the crate's classifier
- FC-5: `recipe.py build` + `recipe.py test` pass against the vendored crate
- FC-6: `recipe.py vendor-check` reports no violation for fleet-contract

**Expected:** All FC-1 through FC-6 implemented and independently testable per
the testable criteria above.

**Applicability:** Filed because `web-app.value == "yes"`. If this app has no
independent deployment/fleet-observability need, close this ticket as
not-applicable.

**Note:** As of catalog-version 8, fleet-contract is spec-only — if no release
is yet published on its channel, note that in the ticket and track it as
blocked-on-upstream rather than closing it. The reference implementations
(Familiar's `fleet.rs`, this framework's `fleet_conformance.py`) remain
acceptable interim paths until the package ships.

**Context / source:** Grimoire required-feature catalog (catalog-version: 8);
authority: docs/grimoire/design/fleet-status-contract.md; standard-package
concept: docs/grimoire/design/web-app-support-design.md §5.5; Dependency
Channel: docs/grimoire/design/dependency-channel-design.md §2; provenance:
issue #287 (2026-07-04 fleet audit).
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`
