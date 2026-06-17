# Release Planning — v0.6

> status: agreed
> Companion to `docs/design/app-design.md` and `docs/version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v0.6.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v0.6.0` |
| **Previous** | v0.5.0 (Runtime Insights) |
| **Theme** | "Multi-source Monitoring" — adds a dedicated aggregated log viewer and multi-directory app scanning so operators can observe all running apps in one place. |

---

## 2. Major Features

### 2.1 Dedicated Log Viewer (Issue #22)

**Description:** A `[Logs]` toggle button in the top bar switches the central area from the app list to a dedicated log viewer panel. The viewer aggregates stdout/stderr lines from all running apps using the existing `LogCapture` ring buffers (`src/log_capture.rs`), displays them in a scrollable area with auto-scroll, and provides per-app chip toggles for filtering. Log lines are prefixed with the source app name.

**Acceptance criteria:**
- `[Logs]` toggle button appears in the toolbar; pressing it replaces the app list with the log viewer; `[Apps]` (or the same button toggled) returns to the app list
- Log viewer shows lines from all running apps by default (all chip-toggles active)
- Top filter bar: one chip toggle per running app; clicking toggles visibility of that app's lines; an "All" chip bulk-selects/deselects all
- Log lines are prefixed with the app name: `[<app-name>] <line>`
- Auto-scroll follows new output unless the user has scrolled up manually; resumes when scrolled back to bottom
- Existing per-app tail pane in the details panel (v0.4) is unchanged
- `cargo test` passes (no regressions)

**Branch:** `warden/v0.6-log-viewer`

**Design doc:** Spec complete in issue body and this plan; `src/log_capture.rs` already exists from v0.4.

---

### 2.2 Multi-directory Watching (Issue #23)

**Description:** Make `--apps-dir` a repeatable CLI flag so Warden can watch multiple app directories simultaneously. The `Scanner` accepts a `Vec<PathBuf>` of roots and scans all in each cycle. `AppEntry` gains a `root: PathBuf` field; the UI shows which directory each app comes from. Stale-removal logic (v0.2) applies per root independently.

**Acceptance criteria:**
- `--apps-dir` flag is repeatable; multiple values accepted on the command line
- `Scanner` accepts `Vec<PathBuf>` roots; all roots scanned in each cycle
- `AppEntry` has a `root: PathBuf` field populated with the source directory
- App list UI shows which root directory each app came from (subdued label or tooltip)
- Stale entry removal applies independently per root
- Single `--apps-dir` invocation behaves identically to current behaviour
- `cargo test` and `cargo clippy` pass

**Branch:** `warden/v0.6-multi-dir`

**Design doc:** Spec complete in issue body and this plan.

---

## 3. Parallel Implementation Strategy

**Phase 1 (parallel — batch 1):** Items 2.1 and 2.2 can run concurrently.

- 2.1 (log viewer) touches: `src/app.rs` (toolbar toggle, central panel switch, log viewer rendering), `src/log_capture.rs` (read-only), `src/models.rs` (optional view-state enum)
- 2.2 (multi-dir) touches: `src/main.rs` (CLI arg parsing), `src/scanner.rs` (Vec<PathBuf> roots), `src/models.rs` (root field on AppEntry), `src/app.rs` (root display in app list), `src/config.rs` (optional multi-dir config)

- `src/app.rs` overlap: 2.1 adds the toolbar toggle and CentralPanel log view; 2.2 adds a root label to each app row. These are additive, spatially-separate hunks.
- `src/models.rs` overlap: 2.1 may add a view-state enum; 2.2 adds a `root` field to `AppEntry`. Additive, no conflict expected.

**Merge order for Phase 1:** merge 2.2 first (simpler `app.rs` hunk — just adds a root label), then 2.1 (larger `app.rs` change — toolbar + central panel switch).

**Conflict map summary:**

| Branch | Files touched | Conflict risk |
|---|---|---|
| `warden/v0.6-multi-dir` (2.2) | `src/main.rs`, `src/scanner.rs`, `src/models.rs`, `src/app.rs`, `src/config.rs` | Low |
| `warden/v0.6-log-viewer` (2.1) | `src/app.rs`, `src/log_capture.rs`(read), `src/models.rs` | Low (additive app.rs hunks) |

---

## 4. Out of Scope for v0.6

- **Ensign HTTP health polling** — deferred; Ensign API shape not documented in this repo
- **Log search / grep** — backlog (noted in issue #22 non-goals)
- **Log export to file** — backlog (noted in issue #22 non-goals)
- **Config-file support for multiple directories** — backlog (noted in issue #23 non-goals)
- **GUI settings panel** — backlog
- **Log persistence between sessions** — backlog
- **History analytics / charts** — backlog

---

## 5. Status Ledger

### Phase 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.6 |
|---|---|---|---|---|
| `warden/v0.6-multi-dir` (#23) | ☑ | ☑ | ☑ | ☑ |
| `warden/v0.6-log-viewer` (#22) | ☑ | ☑ | ☑ | ☑ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 0.5.0 → 0.6.0) | ☑ |
| `version-history.md` entry | ☑ |
| project-release (merge+tag+push) | ☑ |
| Issues #22 #23 closed | ☑ |

### Follow-ups discovered during implementation

_(empty at start)_
