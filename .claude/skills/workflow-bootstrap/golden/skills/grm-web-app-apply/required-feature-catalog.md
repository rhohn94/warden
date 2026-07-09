catalog-version: 2

# Required-feature catalog (Grimoire web-app)

This is the maintained, versioned catalog of **framework-mandated features**
that every Grimoire web app must have. It is the web-app analogue of
`onboarding/baseline-requirements.md`, scoped to the web-app fact
(`web-app.value: yes`).

Design authority: `docs/design/web-app-support-design.md` §5 (catalog format
§5.1, filing flow §5.2, first entry §5.3).

The catalog is read by the **filing flow** (§5.2): when `web-app.value` is set
(onboarding §6.5 or `web-app-apply` §6), a Reporter files one
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
`feedback-to-issue`. Before filing, search existing open **and closed** issues
tagged `Grimoire-Requirement` for the entry's `key` (carried in the issue title
as `[key: <key>]`). If a matching issue exists (any state), skip the entry.

Dedupe query (CLI fallback):

```bash
python3 .claude/skills/issue-tracker/issue_tracker.py list \
  --labels Grimoire-Requirement --state all
```

MCP equivalent: `list_issues` with `labels=["Grimoire-Requirement"]`.

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

**Context / source:** Grimoire required-feature catalog (catalog-version: 2);
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

**Context / source:** Grimoire required-feature catalog (catalog-version: 2);
authority: docs/design/changelog-surface-design.md;
build-info contract: docs/web-app-deployment-protocol.md §8.
```

**Labels:** `Grimoire-Requirement`, `enhancement`
**Audience:** `internal`
