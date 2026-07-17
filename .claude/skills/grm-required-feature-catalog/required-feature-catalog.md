catalog-version: 13

# Required-feature catalog

This is the maintained, versioned catalog of **framework-mandated features**
that a Grimoire-managed project must have. It is the family-neutral analogue of
`onboarding/baseline-requirements.md`: baseline-requirements seeds rows every
project gets; this catalog seeds rows gated by `applies-when-family` (which
project **families** — `cli`/`gui`/`lib`/`service`/`web` — an entry applies to)
and, within an applicable family, an optional `applies-when` config-dial
predicate.

**Relocated in v3.97 (#413).** Through catalog-version 8 this file lived at
`.claude/skills/grm-web-app-apply/required-feature-catalog.md` and fired only
for `web-app.value == "yes"` projects, once, at onboarding/first-apply. It now
lives in its own family-neutral skill directory
(`.claude/skills/grm-required-feature-catalog/`) so it isn't owned by one
family's apply skill, and is **re-runnable** via
`catalog_filing.py` (§Re-running below) rather than a one-shot onboarding step.
`grm-web-app-apply` still triggers a filing run (fixed family `web`); onboarding
now triggers one for whatever family the project resolves to (§6.5.7,
family-neutral).

Design authority: `docs/grimoire/design/web-app-support-design.md` §5 (catalog
format §5.1, filing flow §5.2, first entry §5.3).

The catalog is read by the **filing flow** (§5.2): a Reporter files one
`Grimoire-Requirement`-tagged ticket per applicable, unfiled entry, deduplicated
by `key`.

**Implementing** any catalog feature in a managed app is out of scope for the
catalog SPEC — it is planned and built by the managed project.

---

## Versioning

The `catalog-version: N` line on line 1 is a human-readable bump contract. Bump
it whenever an entry is **added** or its definition changes (including a
`status`/`activation-event`/`applies-when-family` change), so a reader can tell
at a glance the catalog has moved. It is **not** the primary idempotency
mechanism — that is the per-entry content hash `catalog_filing.py` computes and
persists (§Re-running) — but keeping it current is still required: it is what a
human or the doc-assurance `check-for-checks` meta-check reads first. Keys are
**never reused or renamed** — retiring an entry is a migration (re-key
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

This issue-tracker search remains the **authoritative** dedupe (it is what
actually stops a duplicate ticket landing), and is what the filing flow's
Reporter still does at file-time. `catalog_filing.py`'s persisted state
(§Re-running) is a second, offline-checkable layer in front of it — it lets a
re-run compute a to-file **plan** without an issue-tracker round trip, which is
what makes the mechanism self-testable via fixtures.

---

## Re-running (`catalog_filing.py`)

Through catalog-version 8 this catalog fired exactly once, from onboarding or
`grm-web-app-apply`'s own idempotent-early-exit skill runs. It is now
re-runnable on demand — the catalog can grow new entries (a version, a
required-feature audit, a new standard package) after a project has already
been onboarded, and there was no mechanism to file only what's new against an
already-applied project short of an LLM re-reading the whole catalog by hand.

`.claude/skills/grm-required-feature-catalog/catalog_filing.py` is the
deterministic planning half of a re-run (it never talks to the issue tracker —
filing itself stays the Reporter's job, per the existing filing contract):

```bash
python3 .claude/skills/grm-required-feature-catalog/catalog_filing.py plan \
  --root <project-root> --family {cli,gui,lib,service,web}
python3 .claude/skills/grm-required-feature-catalog/catalog_filing.py --self-test
```

`plan` reads the target project's `.claude/required-feature-catalog-state.json`
(created on first run, absent is treated as empty — no entries seen yet),
evaluates every catalog entry's `applies-when-family` + `applies-when` against
the given family and the target's live `.claude/grimoire-config.json`, and
emits one action per entry:

- `not-applicable` — the family or config predicate doesn't hold; state is
  untouched.
- `file` — the entry applies and either (a) its `key` has never been recorded
  in state, or (b) it *has* been recorded but the entry's own content hash
  differs from the hash recorded at that filing (the entry's spec changed) —
  in both cases the caller should file/re-file via the normal Reporter flow.
  This is the "new or changed" half of the re-run contract.
- `skip-already-filed` — the entry applies, is recorded in state with a
  matching content hash, and its `status` is not `blocked-on-upstream`. Nothing
  to do — this is the "don't re-file the satisfied ones" half.
- `file-blocked` — the entry applies, carries `status: blocked-on-upstream` in
  the catalog, its activation check (below) does not yet hold, and it has never
  been recorded in state. The caller files it as a **blocked-on-upstream**
  ticket (§Blocked-on-upstream status), not a normal one.
- `skip-already-blocked` — same as `file-blocked` but already recorded in state
  at that status; nothing to do.
- `activate` — the entry carries `status: blocked-on-upstream` in the catalog,
  is recorded in state as `blocked-on-upstream`, but its activation check now
  holds (the upstream artifact showed up). The caller files/updates the normal
  (non-blocked) ticket; `plan` (once the caller records the outcome via
  `record`) transitions the entry's persisted status to `filed`.

After the caller acts on a `file`/`file-blocked`/`activate` action, it calls
`catalog_filing.py record --root <project-root> --key <key> --status
{filed,blocked-on-upstream}` to persist the outcome (content hash + status) so
the *next* `plan` call sees it as satisfied. `plan` never mutates state itself —
it is a pure read+report, which is what makes it safe to call repeatedly and
easy to unit-test.

---

## Conditional applicability (`applies-when-family` and `applies-when`)

Every entry now carries **two** independent gates, evaluated in order — a
family gate first, then (if the family gate holds) an optional config-dial
gate:

### Family gate (`applies-when-family`)

```
applies-when-family: <family>[, <family>, ...]
```

- One or more of the five project families:
  `cli` / `gui` / `lib` / `service` / `web` (the same vocabulary
  `.claude/quick-start-templates/<family>/template.json`'s `profile` field
  uses — this catalog does not invent a second family vocabulary).
- **Absence-as-default is `web`.** An entry with no `applies-when-family:` line
  applies only to the `web` family — this is the exact behavior every entry
  had through catalog-version 8 (the catalog was invoked only for
  `web-app.value == "yes"` projects), preserved as the default so relocating
  the file does not silently change any existing entry's applicability. An
  entry that has been reviewed and is genuinely family-general (Entries 2 and
  7 below) declares its actual family list explicitly.
- The family a `plan` run evaluates against is supplied by the **caller**
  (`--family`), not auto-detected here — the caller (onboarding's own
  interview, or `grm-web-app-apply`'s fixed `web`) already knows it. Family
  detection itself is out of this catalog's scope (it is
  `grm-quick-start-template` §1 / the Q9 signal table's job).

### Config-dial gate (`applies-when`)

Unchanged from catalog-version 8: a single equality over a dotted config path,
evaluated **only** once the family gate above has already passed.

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
- An entry with **no** `applies-when:` line has no config-dial gate — once its
  family gate passes, it is unconditional (Entries 1, 2, 7).

The single-equality grammar is deliberately minimal. A richer predicate language
(boolean combinators, comparisons) is a future extension and is **not** required
for the current entries — see `web-app-support-design.md` §5.1.

---

## Status (`status`, `blocked-on-upstream`)

Every entry has an implicit `status` of **`filed`** — its artifact (a
published standard-package crate, a shippable feature) already exists, so a
filed ticket is immediately actionable end-to-end. An entry may instead declare:

```
status:            blocked-on-upstream
activation-event:  <human description of what must become true>
activation-check:  vendor.toml:deps.<name>
```

`blocked-on-upstream` is a **real, distinct status**, not a note buried in an
issue body template (the catalog-version-8 failure mode this fixes, #413): a
spec-only entry with no upstream artifact yet (e.g. Entry 6/meta-updater and
Entry 8/fleet-contract below — both cite a Rust crate that is designed but not
yet published) would otherwise file an indistinguishable, un-completable ticket
next to genuinely actionable ones. This catalog's chosen behavior (of the two
the design allows) is: **file it, but tag it distinctly** — a
`blocked-on-upstream` entry still gets a ticket (so the managed project's own
planning can account for the intended adoption, exactly as Entries 6/8's prose
already asked for pre-#413), but the ticket is filed as
`status: blocked-on-upstream`, not `filed`, and carries the
`activation-event` prose verbatim so a reader (or the managed project) knows
exactly what unblocks it. The alternative (don't file until the artifact
exists) was rejected because it makes the requirement invisible to the managed
project's own planning until someone remembers to re-run the catalog — a
regression from the pre-#413 behavior of at least surfacing the future
obligation.

`activation-check` is the machine-checkable half: `catalog_filing.py` treats
`vendor.toml:deps.<name>` as "does the target's `vendor.toml` contain a
`[deps.<name>]` table" (a plain text/regex scan, the same shape
`dependency_channel_conformance.py` already greps for — no new vendor.toml
parser). When the check starts passing, `plan` emits `activate` for that entry
on the next run (§Re-running) instead of `skip-already-blocked`.

---

## Entries

### Entry 1 — Admin Console

```
key:                 admin-console
name:                Administrator Console
tag:                 Grimoire-Requirement
applies-when-family: web
conformance-check:   .claude/skills/grm-required-feature-catalog/admin_console_conformance.py
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

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
authority: docs/design/web-app-support-design.md §5.3;
build-info contract: docs/web-app-deployment-protocol.md §8.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 2 — Changelog Surface

```
key:                 changelog-surface
name:                Changelog Surface
tag:                 Grimoire-Requirement
applies-when-family: web, gui, cli
conformance-check:   .claude/skills/grm-web-app-apply/changelog_conformance.py
```

**Spec.** Every Grimoire web app MUST surface its **changelog / release notes**
through the GUI. The changelog is exposed to **operators always** and to
**end users only when explicitly enabled** by a config toggle:

- **Operator-facing (always required, unconditional).** The Administrator
  Console (Entry 1) gains a **Changelog** section showing the application's
  release notes for at least the currently-serving version, sourced from the
  build-info `changelog` field (§Data source below). It is gated to the
  Application Administrator role like the rest of the console. This half of
  the requirement is **not** gated by the dial below — it exists on every
  conforming app regardless of the dial's value.
- **User-facing (gated by the `changelog.user-facing` dial).** When the dial
  is `on`, the app additionally exposes a **public** changelog surface
  reachable at the stable path **`/changelog`** for end users — no GUI
  navigation button required (the AC-10 always-reachable pattern). When the
  dial is `off` (the default, absence-as-default), `/changelog` is **not**
  exposed (404/forbidden) and only the operator surface exists.

**The dial — exact location and shape.** `changelog.user-facing` lives in the
project's `.claude/grimoire-config.json`, as a `value`-dial block:

```json
{
  "changelog": {
    "user-facing": { "value": "off" }
  }
}
```

- Allowed values: `"on"` / `"off"`. **Default `off`** — an app that has never
  written this block reads as `off` (absence-as-default), never as an error.
- `config_validate.py` (`.claude/skills/grm-config-validate/`) already
  validates this dial (`ENUMS["changelog.user-facing"]`, `KNOWN_TOP`
  includes `"changelog"`) and treats it as **additive**: it is never a
  migration default (`--migrate` never synthesizes the block) and it never
  bumps `schema-version`. A managed project sets the dial by hand-editing
  `.claude/grimoire-config.json`; there is no separate CLI for it.
- **What "on" requires, end to end** (all three, together — a partial
  adoption is not conformant): (1) the operator-facing Changelog section in
  the Admin Console — unconditional, present regardless of the dial; (2) the
  `/changelog` route/page actually reachable and returning the changelog UI;
  (3) a `package`-produced `grimoire-build-info.json` with a non-trivial
  `changelog` field for both surfaces to render — see §Data source. Turning
  the dial `on` **before** `package` has ever produced a build-info snapshot
  is a common half-adoption failure mode; the conformance check below exists
  specifically to catch it.

**Data source — where the changelog snapshot comes from.** Both surfaces
render from the `changelog` field of `grimoire-build-info.json`
(`docs/web-app-deployment-protocol.md` §8), the build-time provenance stamp
the `package` recipe target emits into the bundle root
(`quick-start-templates/{cli,gui,lib,service,web}/files/scripts/package.sh`,
shipped by #431). `changelog` is a **build-time snapshot** of the app's
**front-facing** `docs/changelog.md` — **never** `docs/version-history.md`,
which is the internal engineering record and may carry ticket IDs / process
detail unsuitable for a shipped UI (`docs/coding-standards.md` §Content & UI
copy; `changelog-surface-design.md` §3). Because it travels inside the
versioned bundle (the same rule as the `grimoire-config` snapshot, §8), a
running or rolled-back deployment always shows the notes of the version
**actually serving**, never a live re-fetch. The field is optional for
back-compat: when absent, both surfaces render an honest "no changelog
packaged for this build" empty state rather than erroring (CL-2).

Design authority: `docs/grimoire/design/changelog-surface-design.md`;
build-info contract: `docs/web-app-deployment-protocol.md` §8; config dial:
`changelog.user-facing`.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| CL-1 | **Operator-facing changelog in the Admin Console (always).** | The Admin Console renders a Changelog section showing at least the currently-serving version's notes. |
| CL-2 | **Sourced from build-info.** The changelog data comes from `grimoire-build-info.json` field `changelog`, not a live remote fetch. | With `changelog` present in the live build-info the rendered notes match it; with it absent an honest empty-state shows (no error). |
| CL-3 | **`changelog.user-facing` dial gates a user surface.** When `on`, `/changelog` is reachable by an end user. | With the dial `on`, a GET to `/changelog` returns HTTP 200 and the changelog UI. |
| CL-4 | **Default off.** With the dial absent/`off`, no user-facing surface is exposed. | With the dial `off`, a GET to `/changelog` returns 404/forbidden; the operator surface still renders. |
| CL-5 | **Stable path, no button required.** When enabled, `/changelog` is always reachable directly regardless of any nav button. | A direct GET to `/changelog` (dial `on`) returns the surface without a discoverable link. |
| CL-6 | **Per-release rendering.** Each release entry shows its version and notes (date when available). | The rendered surface lists at least one release with a version identifier and its notes. |

#### Conformance check (standalone, offline — #437)

A target repo's Entry-2 adoption can be probed **statically, without booting
the app**: `.claude/skills/grm-web-app-apply/changelog_conformance.py`.

```bash
python3 .claude/skills/grm-web-app-apply/changelog_conformance.py --root <path>
python3 .claude/skills/grm-web-app-apply/changelog_conformance.py --self-test
```

It checks, in order:

1. **Dial presence/shape** — reads `changelog.user-facing` out of the target's
   `.claude/grimoire-config.json` (reusing `config_validate.py`'s `dialval()`
   so the two never drift on what counts as a valid value). A non-object
   `changelog` block or an out-of-enum value is flagged.
2. **Build-info snapshot presence** — whether `grimoire-build-info.json`
   exists anywhere under `dist/` (or at the repo root), i.e. whether `package`
   has ever been run.
3. **Route/section convention (informational only)** — a best-effort, static
   scan for a discoverable admin-section/changelog-route convention in
   source, per family (web: a `templates/` tree with a changelog-named
   template, or a `/changelog` string reference under `src/`). This check
   **never** affects the pass/fail verdict — no fleet app implements the
   surface yet (the reference implementation is epic #395's territory, a
   separate repo, out of scope here) and static per-family detection is
   inherently best-effort. When no web-app source structure is present at
   all (e.g. a `gui`/`cli`/`lib`/`service` repo), it reports `not-applicable`
   rather than erroring.

**Verdict.** Driven by checks 1+2 only: `changelog.user-facing: on` with no
`grimoire-build-info.json` snapshot anywhere is **flagged** (the `/changelog`
surface the dial promises has nothing real to render — the half-adoption
failure mode named above). `off`/absent, or `on` with a real snapshot
present, both **pass**. This mirrors `fleet_conformance.py`'s
`ConformanceResult` shape (`.errors` / `.warnings` / `.passed`) so the two
catalog-conformance scripts in this skill directory read the same way.

**Wired into `grm-install-doctor`.** `install_doctor.py`'s
`audit_changelog_surface()` (`.claude/skills/grm-install-doctor/`) calls this
same probe as a new, additive health-audit check (same pattern as the #438
fixtures-convention and #439 environments-adoption checks it sits beside):
`ok` for a non-web-app repo, the dial off/absent, or a real snapshot present;
`warn` (never a hard failure — the same advisory severity class as those two
checks) for the flagged half-adoption case. Surfaced as its own
"Changelog-surface conformance" report section and repair-plan step.

**Not yet built:** full integration into a generalized per-entry
catalog-conformance framework — that is **#434** (scheduled a later release);
this script is a natural precursor #434 will fold in once that machinery
exists, not a scope cut.

#### Per-family guidance (adapted, non-web)

Entry 2 as specified above (CL-1..CL-6, the Admin Console + `/changelog`
route) applies to **web apps** (`web-app.value: yes`). The GUI/CLI families
have no `/admin-console` or URL-routable surface to hang the requirement on,
so they get an **adapted convention** instead — documented guidance to
implement from, not a working reference (no `gui`/`cli` app in this repo
implements it yet; that is future fleet-adoption work, tracked the same way
CL-1..6 is, once a project affirmatively adopts it).

- **`gui` family (egui/eframe desktop apps, e.g. the `gui-starter` quick-start
  profile).** Add a **Changelog panel** reachable from a menu item (e.g.
  `Help ▸ Changelog`) or an always-visible toolbar button — the desktop
  analogue of the AC-10 "no nav button required, but direct access always
  works" pattern is "always in the menu bar, not buried in a settings
  sub-tree". The panel reads the same build-time `grimoire-build-info.json`
  `changelog` field (§Data source), embedded alongside the binary at package
  time exactly as the web family's build-info bundling works — no live
  fetch, no second copy. Render it with `egui::Window` or a
  `SidePanel`/`CentralPanel` toggle, listing each release's version and notes
  (the CL-6 per-release shape). Follow the same read pattern
  `quick-start-templates/web/files/src/version_report.rs` already establishes
  for reading committed provenance files from a Rust binary at runtime — the
  changelog panel is a sibling read, not a new mechanism.
- **`cli` family (e.g. the `cli` quick-start profile).** Add a `--changelog`
  flag, sibling to the existing `--version`/`--about` convention
  (`version_report.rs` §Call sites): printing the packaged
  `grimoire-build-info.json` `changelog` field to stdout (or an honest "no
  changelog packaged for this build" line when absent, mirroring CL-2's
  empty-state rule) and exiting. No operator-vs-user split applies to a CLI —
  one flag, one audience.
- **`lib`/`service` families.** No GUI surface exists to gate, so Entry 2 does
  not apply — `changelog_conformance.py` reports `not-applicable` for these
  rather than flagging anything.

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

**Verify with:** `python3 .claude/skills/grm-web-app-apply/changelog_conformance.py
--root .` (standalone, offline — checks the dial + build-info snapshot; also
wired into `install_doctor.py`'s health audit).

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
authority: docs/grimoire/design/changelog-surface-design.md;
build-info contract: docs/web-app-deployment-protocol.md §8.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 3 — Token-Bookkeeper Standard Package (conditional)

```
key:                 adopt-token-bookkeeper
name:                Token-Bookkeeper Standard Package
tag:                 Grimoire-Requirement
applies-when-family: web
applies-when:        web-app.agentic == "yes"
conformance-check:   .claude/skills/grm-required-feature-catalog/standard_package_conformance.py --dep token-bookkeeper
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

Design authority: `docs/grimoire/design/web-app-support-design.md` §5.5
(standard-package concept + applicability); Dependency Channel artifact contract:
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

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
authority: docs/grimoire/design/web-app-support-design.md §5.5;
Dependency Channel: docs/grimoire/design/dependency-channel-design.md §2.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 4 — Gatekeeper Standard Package (web-app auth; conditional)

```
key:                 adopt-gatekeeper
name:                Gatekeeper Standard Package
tag:                 Grimoire-Requirement
applies-when-family: web
applies-when:        web-app.auth == "yes"
conformance-check:   .claude/skills/grm-required-feature-catalog/standard_package_conformance.py --dep gatekeeper
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

This entry is **conditional** (`applies-when: web-app.auth == "yes"`,
v3.97/#464 — previously the coarser `web-app.value == "yes"`, an
honest-but-imprecise stand-in for "every Grimoire web app that has an
authenticated surface"). The `web-app.auth` capability dial
(`web-app-support-design.md` §1.3) records that fact directly: an app that has
not declared it (absence-as-default `no`) — including a purely static, no-auth
web app — is not surfaced this requirement at all.

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

**Applicability:** Filed because `web-app.auth == "yes"`. A purely static,
no-auth web app (or one that has not declared the `web-app.auth` dial) is not
surfaced this ticket.

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
authority: docs/grimoire/design/web-app-support-design.md §5.6;
Dependency Channel: docs/grimoire/design/dependency-channel-design.md §2.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 5 — Recordkeeper Standard Package (database access; conditional)

```
key:                 adopt-recordkeeper
name:                Recordkeeper Standard Package
tag:                 Grimoire-Requirement
applies-when-family: web
applies-when:        web-app.persistence == "yes"
conformance-check:   .claude/skills/grm-required-feature-catalog/standard_package_conformance.py --dep recordkeeper
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

This entry is **conditional** (`applies-when: web-app.persistence == "yes"`,
v3.97/#464 — previously the coarser `web-app.value == "yes"`). The
`environments` block's per-env `data_isolation` field
(`deploy-environment-design.md` §1) remains a per-environment data-isolation
flag, not a project-level "this app persists data" dial, so it was never a
substitute; the `web-app.persistence` capability dial
(`web-app-support-design.md` §1.3) is that dedicated project-level signal. A
stateless web app that has not declared it (absence-as-default `no`) is not
surfaced this requirement at all.

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

**Applicability:** Filed because `web-app.persistence == "yes"`. A stateless
web app (no persistent store, or one that has not declared the
`web-app.persistence` dial) is not surfaced this ticket.

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
authority: docs/grimoire/design/web-app-support-design.md §5.7;
Dependency Channel: docs/grimoire/design/dependency-channel-design.md §2;
environment convention: docs/grimoire/design/deploy-environment-design.md §1–§2.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 6 — Meta-Updater Standard Package (self-update; conditional)

```
key:                 adopt-meta-updater
name:                Meta-Updater Standard Package
tag:                 Grimoire-Requirement
applies-when-family: web
applies-when:        web-app.value == "yes"
status:              blocked-on-upstream
activation-event:    meta-updater crate published on its release channel and
                      a vendor.toml [deps.meta-updater] entry exists (the
                      library is spec-only as of catalog-version 9 — see
                      docs/grimoire/design/meta-updater-package-design.md)
activation-check:     vendor.toml:deps.meta-updater
conformance-check:   exempt (blocked-on-upstream, #434 — meta-updater is
                      spec-only; no published crate exists to probe. Once the
                      activation-check above starts passing (a real
                      vendor.toml [deps.meta-updater] entry appears), this
                      entry should gain a real check modeled on Entries 3-5's
                      standard_package_conformance.py --dep meta-updater — a
                      trivial follow-up once the crate exists, not a design
                      gap today.)
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

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
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
key:                 aura-via-dependency-channel
name:                Sanctioned Aura Consumption Mechanism (Dependency Channel)
tag:                 Grimoire-Requirement
applies-when-family: web, gui, cli, lib, service
applies-when:        repo consumes Aura by any detectable mechanism
conformance-check:   .claude/skills/grm-required-feature-catalog/aura_channel_conformance.py
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

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
detect/adopt spec: this entry (Entry 7); adoption mechanism: `grm-vendor-migrate`;
provenance: meta-planner fleet architecture audit 2026-07-09 (issue #316).
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 8 — Fleet-Contract Standard Package (fleet observability; conditional)

```
key:                 adopt-fleet-contract
name:                Fleet-Contract Standard Package
tag:                 Grimoire-Requirement
applies-when-family: web
applies-when:        web-app.fleet-participant == "yes"
status:              blocked-on-upstream
activation-event:    fleet-contract crate published on its release channel and
                      a vendor.toml [deps.fleet-contract] entry exists (the
                      library is spec-only as of catalog-version 9 — see
                      docs/grimoire/design/fleet-status-contract.md)
activation-check:     vendor.toml:deps.fleet-contract
conformance-check:   exempt (blocked-on-upstream, #434 — fleet-contract is
                      spec-only; no published crate exists to probe. Note
                      fleet_conformance.py already exists and validates the
                      CONTRACT SHAPE (/fleet/v1/status payloads, offline
                      self-test + live probe) against the shared vectors in
                      this skill directory — but that verifies an app's
                      endpoint shape, not THIS entry's package-adoption
                      sub-requirements (FC-1/FC-5/FC-6), which stay
                      unbuildable until the crate ships. Once the
                      activation-check above starts passing, this entry
                      should gain a real check modeled on Entries 3-5's
                      standard_package_conformance.py --dep fleet-contract.)
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

This entry is **conditional** (`applies-when: web-app.fleet-participant ==
"yes"`, v3.97/#464 — previously the coarser `web-app.value == "yes"` gate
Entry 6/meta-updater still uses). Not every deployed Grimoire web app is
actually fleet-participating (e.g. a purely static asset bundled into a larger
host app has no independent deployment/fleet-observability need), so the
`web-app.fleet-participant` capability dial (`web-app-support-design.md`
§1.3) records that fact directly: a web app that has not declared it
(absence-as-default `no`) is not surfaced this requirement at all.

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

**Applicability:** Filed because `web-app.fleet-participant == "yes"`. An app
with no independent deployment/fleet-observability need (or one that has not
declared the `web-app.fleet-participant` dial) is not surfaced this ticket.

**Note:** As of catalog-version 9, fleet-contract is spec-only — if no release
is yet published on its channel, note that in the ticket and track it as
blocked-on-upstream rather than closing it. The reference implementations
(Familiar's `fleet.rs`, this framework's `fleet_conformance.py`) remain
acceptable interim paths until the package ships.

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
authority: docs/grimoire/design/fleet-status-contract.md; standard-package
concept: docs/grimoire/design/web-app-support-design.md §5.5; Dependency
Channel: docs/grimoire/design/dependency-channel-design.md §2; provenance:
issue #287 (2026-07-04 fleet audit).
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 9 — Standardized App Telemetry (event schema)

```
key:                 app-telemetry
name:                Standardized App Telemetry
tag:                 Grimoire-Requirement
applies-when-family: web
conformance-check:   .claude/skills/grm-required-feature-catalog/app_telemetry_conformance.py
```

**Spec.** Every Grimoire web app emits **app-side telemetry events** — boot,
per-request summaries, and errors — in one versioned, six-field JSON shape:
`ts` / `instance` / `app` / `version` / `event` / `props`
(`docs/grimoire/design/app-telemetry-design.md` §1). This is distinct from the
framework's existing **agent-run** telemetry (`run.json`,
`run-metadata-artifact-design.md`), which records one dispatched agent
session's cost/outcome — app-telemetry records the *deployed app's own*
runtime signals.

**No vendored crate — a scaffold module, not a standard package.** Unlike
Entries 3–6/8, this entry has no `vendored-crate` to pin: the reference
implementation (`src/app_telemetry.rs`, §5 of the design doc) is a REAL,
working, std-only module shipped directly in the web quick-start template,
mirroring `config_loader.rs` (#439) and `version_report.rs` (#206)'s
precedent rather than the token-bookkeeper/gatekeeper/recordkeeper vendoring
pattern. This entry is therefore **unconditional** for the `web` family (no
`applies-when` dial) — every Grimoire web app benefits from these three
baseline signals with zero app-specific configuration, the same unconditional
shape as Entry 2 (Changelog Surface).

**The three reference event types.** `boot` (always emitted, once per process
start), `request-summary` (sampled per the app's own `sample_rate`, default
`1.0`), and `error` (always emitted, never sampled). Full sampling rules and
privacy/retention notes: `app-telemetry-design.md` §3–§4. An app MAY emit
additional app-specific `event` values beyond these three.

Design authority: `docs/grimoire/design/app-telemetry-design.md`. Explicitly
**out of scope** for this entry (per issue #436's posted scope-narrowing
comment): standing up the Fleet Status Contract's `/fleet/v1/metrics`
sub-resource as a live transport, or any live Mission Control consumer of
emitted events — schema + reference implementation only.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| AT-1 | **Emitter module present.** The app ships an app-telemetry emitter conforming to the six-field schema (`ts`/`instance`/`app`/`version`/`event`/`props`). | `src/app_telemetry.rs` (or the app's equivalent for its own stack) exists and its public surface can produce all six fields. |
| AT-2 | **All three reference event types emitted.** `boot`, `request-summary`, and `error` are each emitted from a real call site (not merely defined but never called). | A committed sample fixture (or a live capture) contains at least one event of each of the three types, each conforming to AT-1's shape. |
| AT-3 | **Sampling rules honored.** `boot` and `error` are never sampled (rate 1.0); `request-summary` respects a configurable `sample_rate`. | Source review confirms `boot`/`error` call sites carry no sampling gate; `request-summary`'s call site does. |
| AT-4 | **No PII in `props`.** Emitted `props` values never carry raw request bodies, headers, cookies, or other end-user-identifying data. | Source review of `props`-construction call sites; the conformance check's schema validation (below) checks shape, not content, so this sub-requirement stays a code-review item, not a mechanical one. |

#### Conformance check

`.claude/skills/grm-required-feature-catalog/app_telemetry_conformance.py`
(offline, no network — registered in `catalog_conformance.py`'s
`CHECK_REGISTRY`, the same dispatch attach point Entries 3–5 use). It checks,
against a target repo:

1. **Emitter-module presence (best-effort, informational)** — a static scan
   for an app-telemetry module referencing all three reference event names,
   mirroring Entry 2's route-convention check (c).
2. **Sample-fixture schema validation (mechanical)** — if a committed JSON
   Lines sample-event fixture is present, every line is validated against the
   §1 schema (required fields, recognized `event` values) via
   `app_telemetry_schema.validate_event` — the actual "verify emitted events
   against the schema shape" half of this entry's acceptance bar.
3. Reports `not-applicable` when no web-app source structure exists at all.

**Dedupe key in filed issue title:** `[key: app-telemetry]`

**Issue title (when filing):**
`[key: app-telemetry] Adopt standardized app telemetry (AT-1 through AT-4)`

**Issue body template:**

```markdown
**What:** This web app must emit app-side telemetry events (boot,
request-summary, error) in the standardized six-field schema (`ts`/
`instance`/`app`/`version`/`event`/`props`) rather than an ad hoc shape.

**Sub-requirements:**
- AT-1: Emitter module present, conforming to the six-field schema
- AT-2: All three reference event types (boot/request-summary/error) actually
  emitted from real call sites
- AT-3: Sampling rules honored — boot/error never sampled; request-summary
  respects a configurable sample_rate
- AT-4: No PII in `props`

**Expected:** All AT-1 through AT-4 implemented and independently testable per
the testable criteria above.

**Applicability:** Unconditional for the `web` family — every Grimoire web app
is surfaced this ticket.

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
authority: docs/grimoire/design/app-telemetry-design.md.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`

---

### Entry 10 — Structured Logging

```
key:                 structured-logging
name:                Structured Logging
tag:                 Grimoire-Requirement
applies-when-family: cli, gui, service, web
conformance-check:   .claude/skills/grm-required-feature-catalog/logging_conformance.py
```

**Spec.** Every Grimoire-managed process emits **structured, JSON-lines
logging to stdout** — one JSON object per line, no other format — instead of
a plain-text default. `docs/coding-standards.md` §Logging is the SPEC: the
standard field contract (`ts`, `level`, `target`, `msg`, `correlation_id`,
`instance`, `version`), level set via instance config, and rotation staying
the deployment supervisor's job (it already captures stdout/stderr into
`logs/`, `docs/web-app-deployment-protocol.md` §4 — never re-specified
in-app). Logging is the missing spec this entry fills: prior to #435, coding
standards had convention lines (`tracing` behind one init fn; `logging`
behind one module) but no format, level, rotation, or correlation-id
contract, and the Administrator Console's AC-4 log viewer (Entry 1) had
nothing standard to view.

This entry is **unconditional** once its family gate passes (no
`applies-when` dial) — every `cli`/`gui`/`service`/`web` process boots and
logs. `lib` is excluded: a pure library has no `fn main()` process to log
from (`quick-start-templates-design.md`).

**Starter-template init modules.** `logging_init.rs` (Rust, built on
`tracing`/`tracing-subscriber` with a custom JSON-lines `FormatEvent`) ships
in the `cli`/`gui`/`service` quick-start templates' `src/`, replacing the
prior `env_logger`/`log` plain-text default; `logging_init.py` (Python,
built on the stdlib `logging` module) is a copy-paste reference
implementation in `docs/coding-standards/python.md` §Logging — no
Python-backed quick-start template exists yet to carry a scaffolded copy.
Both are copied per-template/per-project code, not a shared crate/package —
the cross-repo rule-of-two policy (`docs/coding-standards.md` §Cross-repo
extraction policy) hasn't triggered.

As with Entries 1–9 the catalog is the **SPEC**: adopting the init module (or
an equivalent emitting the same shape) in a managed project's own entrypoint
is that project's scope, tracked by the filed `[key: structured-logging]`
ticket.

Design authority: `docs/coding-standards.md` §Logging (field contract);
starter modules: `docs/coding-standards/rust.md` §Logging,
`docs/coding-standards/python.md` §Logging; rotation boundary:
`docs/web-app-deployment-protocol.md` §4.

#### Sub-requirements (each is independently testable)

| ID | Requirement | Testable criterion |
|----|-------------|-------------------|
| SL-1 | **JSON-lines to stdout.** Every emitted log line is a single, valid JSON object written to stdout — never a plain-text or multi-line format. | The first line written to stdout at process boot parses as JSON. |
| SL-2 | **Standard field contract.** Every emitted line carries the seven standard fields (`ts`, `level`, `target`, `msg`, `correlation_id`, `instance`, `version`) with the documented types (`docs/coding-standards.md` §Logging). | The boot line's JSON object has all seven fields present with correct types (`ts` an int, `level` one of `trace`/`debug`/`info`/`warn`/`error`, the rest strings). |
| SL-3 | **Level set via instance config, not hardcoded.** The active log level comes from the app's config loader (env var / config file), not a literal in source. | Changing the config's log-level value (e.g. `LOG_LEVEL`) changes which levels are emitted, without a code change. |
| SL-4 | **One init call, no per-log-site boilerplate.** The logging init function is called exactly once, at process start, before any other log line; every downstream call site uses a bare `tracing::info!`/`logging.info(...)`-style macro/call with no manual formatting or field-passing. | Exactly one call to the init function exists in the process entrypoint; grepping call sites elsewhere finds no re-initialization and no hand-formatted JSON. |
| SL-5 | **Rotation is not re-implemented in-app.** The app never opens, writes to, or rotates its own log file — it writes only to stdout, leaving rotation to the deployment supervisor. | No first-party module under `src/` opens a file handle for logging; `logs/` is populated only by the supervisor's stdout/stderr capture. |

**Dedupe key in filed issue title:** `[key: structured-logging]`

**Issue title (when filing):**
`[key: structured-logging] Adopt standardized structured logging (SL-1 through SL-5)`

**Issue body template:**

```markdown
**What:** This project must emit structured, JSON-lines logging to stdout per
the standard field contract (`docs/coding-standards.md` §Logging) — via the
Rust `logging_init.rs` starter module (quick-start templates) or the Python
`logging_init.py` reference implementation (`docs/coding-standards/python.md`
§Logging), called once at process start.

**Sub-requirements:**
- SL-1: JSON-lines to stdout (no plain-text/multi-line format)
- SL-2: Standard field contract (`ts`/`level`/`target`/`msg`/`correlation_id`/
  `instance`/`version`), correct types
- SL-3: Level set via instance config, not hardcoded
- SL-4: One init call at process start; no per-log-site boilerplate
- SL-5: Rotation not re-implemented in-app (stdout only; supervisor's job)

**Expected:** All SL-1 through SL-5 implemented and independently testable per
the testable criteria above.

**Verify with:**
`.claude/skills/grm-required-feature-catalog/logging_conformance.py --boot-probe
<entrypoint>` (spawns the app, captures its first stdout line, validates
shape) and `--root .` (offline static scan for the init call site).

**Context / source:** Grimoire required-feature catalog (catalog-version: 13);
authority: docs/coding-standards.md §Logging; starter modules:
docs/coding-standards/rust.md §Logging, docs/coding-standards/python.md
§Logging; rotation boundary: docs/web-app-deployment-protocol.md §4.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`
