# Release Planning — v0.8

> status: agreed
> Companion to `docs/design/app-design.md` and `docs/version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v0.8.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v0.8.0` |
| **Previous** | v0.7.0 (Performance + Stability) |
| **Theme** | "Operator UX" — makes the day-to-day experience of running a fleet faster and more informative: a one-click Restart, live app-list search to cut through large fleets, and crash detection so unexpected exits are immediately visible as a distinct red badge. |

---

## 2. Major Features

### 2.1 Restart Button (Issue #25)

**Description:** Add a `[Restart]` button next to `[Stop]` for Running apps. Clicking it atomically stops the app (SIGTERM + wait up to 3 s) then immediately starts it again — no manual two-click cycle.

**Acceptance criteria:**
- `[Restart]` button appears in the app row and in the details pane when the app is `Running`
- While restart is in-flight the button is disabled and labeled `[Restarting…]`
- `src/launcher.rs`: add `pub async fn restart(&mut self, name: &str)` that reuses the existing stop + start logic
- `src/app.rs`: render `[Restart]` button for Running apps; call `launcher.restart(name)` on click
- Unit test: verify `restart` sends stop signal then start signal in the correct order
- `cargo test` passes

**Branch:** `warden/v0.8-restart`

**Files:** `src/launcher.rs` (new `restart` method), `src/app.rs` (button render in row + details pane)

---

### 2.2 App List Search / Live Filter (Issue #26)

**Description:** A single-line text-filter field at the top of the app list narrows displayed entries to those whose name contains the query (case-insensitive). Escape clears the query.

**Acceptance criteria:**
- Text field labeled `"Filter apps…"` appears above the app list; always visible
- Filtering is case-insensitive substring match on `AppEntry.name`
- Status label shows `"Showing N of M apps"` when a filter is active
- Pressing `Escape` while field is focused clears the query
- When the filter hides the currently-selected app, the selection is cleared
- `src/app.rs`: add `search_query: String` field to `WardenApp`; filter entries before rendering list; render `egui::TextEdit` above the list
- Unit test: given 5 entries, a matching query returns exactly the expected subset
- `cargo test` passes

**Branch:** `warden/v0.8-search`

**Files:** `src/app.rs` (state field + TextEdit render + filter predicate)

---

### 2.3 Crash Detection and Badge (Issue #27)

**Description:** Detect when a Warden-started app exits unexpectedly (not via the Stop button). Surface it as `AppStatus::Crashed` with a red badge, a notification, and a distinct history entry.

**Acceptance criteria:**
- `src/models.rs`: add `AppStatus::Crashed` variant
- `src/launcher.rs`: maintain a `user_stopped: HashSet<String>` — insert on Stop click, remove on Start
- `src/scanner.rs`: when an app transitions from `Running` → not-running and is NOT in `user_stopped`, set status to `Crashed`
- `src/app.rs`: render `BadgeStatus::Error` (red) for `Crashed`; show `"Crashed — click Start to restart"` in details pane
- `src/notifier.rs`: fire `"<AppName> crashed"` notification on `Running → Crashed` transition
- `src/history.rs`: record a `Crashed` event entry distinct from a normal `Stopped` event
- Unit tests: (a) Running→not-running without user-stop → `Crashed`; (b) Running→not-running WITH user-stop → `Stopped`
- `cargo test` passes

**Branch:** `warden/v0.8-crash-detection`

**Files:** `src/models.rs`, `src/launcher.rs`, `src/scanner.rs`, `src/app.rs`, `src/notifier.rs`, `src/history.rs`

---

## 3. Parallel Implementation Strategy

**Phase 1 (parallel — batch 1):** Items 2.1 and 2.2 can run concurrently.
- 2.1 (restart) touches `src/launcher.rs` (new method) and `src/app.rs` (button area, ~lines 456–510)
- 2.2 (search) touches `src/app.rs` only (state field + new top-of-list render, above the existing list)
- `src/app.rs` overlap: 2.1 edits the button strip inside the app-row render; 2.2 adds a new `TextEdit` above the list and a filter step before rendering. These are distinct regions — low conflict risk. Merge 2.1 first, then 2.2.

**Merge order for Phase 1:** merge 2.1 first (launcher + button strip), then 2.2 (search field + filter).

**Phase 2 (serial — batch 2):** Item 2.3 (crash detection) runs after Phase 1 is merged.
- 2.3 touches `src/models.rs`, `src/launcher.rs`, `src/scanner.rs`, `src/app.rs`, `src/notifier.rs`, `src/history.rs`
- With Phase 1 merged, 2.3 builds on the updated launcher and app.rs cleanly.

**Conflict map summary:**

| Branch | Files touched | Conflict risk |
|---|---|---|
| `warden/v0.8-restart` (2.1) | `src/launcher.rs`, `src/app.rs` | Low |
| `warden/v0.8-search` (2.2) | `src/app.rs` | Low (different region from 2.1) |
| `warden/v0.8-crash-detection` (2.3) | `src/models.rs`, `src/launcher.rs`, `src/scanner.rs`, `src/app.rs`, `src/notifier.rs`, `src/history.rs` | Low (Phase 2, after Phase 1 merged) |

---

## 4. Out of Scope for v0.8

- **Ensign HTTP health polling** — deferred; Ensign API shape not documented in this repo
- **GUI settings panel** — backlog
- **Log search / grep** — backlog
- **Bulk start/stop** — backlog
- **Auto-restart on crash** — backlog (crash detection lands first)

---

## 5. Status Ledger

### Phase 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.8 |
|---|---|---|---|---|
| `warden/v0.8-restart` (#25) | ☑ | ☑ | ☑ | ☑ |
| `warden/v0.8-search` (#26) | ☑ | ☑ | ☑ | ☑ |

### Phase 2

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.8 |
|---|---|---|---|---|
| `warden/v0.8-crash-detection` (#27) | ☑ | ☐ | ☐ | ☐ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 0.7.0 → 0.8.0) | ☐ |
| `version-history.md` entry | ☐ |
| project-release (merge+tag+push) | ☐ |
| Issues #25 #26 #27 closed | ☐ |

### Follow-ups discovered during implementation

_(empty at start)_
