# Release Planning — v0.5

> status: draft
> Companion to `docs/design/app-design.md` and `docs/version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v0.5.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v0.5.0` |
| **Previous** | v0.4.0 (Persistent Settings + Notifications) |
| **Theme** | "Runtime Insights" — surfaces per-app version staleness and start/stop history so operators see at a glance whether apps need updating and how stable they have been. |

---

## 2. Major Features

### 2.1 Version Update Checks (Issue #20)

**Description:** Introduce `src/version_checker.rs` with a `VersionChecker` struct that compares each app's current `framework_version` (already in `AppEntry`) against the latest published git tag on the app's remote. Checks run asynchronously on a configurable interval and results are cached behind an `Arc<RwLock<>>`. App rows show a subtle badge (e.g. "↑ v0.5") when an update is available; the details panel shows the full message.

**Acceptance criteria:**
- New `src/version_checker.rs` with `VersionChecker` struct; async tokio task polls on `version_check_interval_secs` (config default 3600; 0 disables)
- `VersionCheckResult` variants: `UpToDate`, `UpdateAvailable { latest: String }`, `Unknown`
- Results cached in `HashMap<String, VersionCheckResult>` behind `Arc<RwLock<>>`
- App rows show badge (e.g. "↑ v0.5") when `UpdateAvailable`
- Details panel shows: "Update available: current v0.4.0 → latest v0.5.0"
- Apps without detectable git remote → silently `Unknown`
- `config.rs` gains `version_check_interval_secs: u64` (default 3600)
- Unit test: mock checker returning `UpdateAvailable`, verify badge text
- `cargo test` and `cargo clippy` pass

**Branch:** `warden/v0.5-version-checks`

**Design doc:** No separate design doc needed; spec complete in issue body and this plan.

---

### 2.2 History / Uptime Tracking (Issue #21)

**Description:** Introduce `src/history.rs` with a `HistoryStore` that maintains a per-app ring buffer (max 100) of `HistoryEvent` entries (Started/Stopped with timestamps and durations). Events are recorded on status transitions, persisted to `~/.config/warden/history.json`. The details panel gains a History sub-section (last 10 events) and a live Uptime counter for running apps.

**Acceptance criteria:**
- New `src/history.rs` with `HistoryStore`; ring buffer capped at 100 events per app
- `HistoryEvent` variants: `Started { at: DateTime<Utc>, pid: u32 }` and `Stopped { at: DateTime<Utc>, duration_secs: u64 }`
- Events recorded on status transitions in app update loop
- Persisted to `~/.config/warden/history.json`; loaded on startup; missing file not an error
- Details panel History sub-section: last 10 events in reverse-chronological order
- Details panel Uptime counter: "Uptime: 2h 14m" for running apps (live)
- Adds `chrono = { version = "0.4", features = ["serde"] }` to `Cargo.toml`
- Unit test: push Started/Stopped sequence, verify duration calculation and cap
- `cargo test` and `cargo clippy` pass

**Branch:** `warden/v0.5-history-uptime`

**Design doc:** No separate design doc needed; spec complete in issue body and this plan.

---

## 3. Parallel Implementation Strategy

**Phase 1 (parallel — batch 1):** Items 2.1 and 2.2 can run concurrently.
- 2.1 (version checks) touches: `src/version_checker.rs` (new), `src/models.rs`, `src/app.rs`, `src/config.rs`, `Cargo.toml`
- 2.2 (history/uptime) touches: `src/history.rs` (new), `src/models.rs`, `src/app.rs`, `Cargo.toml`
- `src/app.rs` overlap: both add rendering to the details panel. Conflict risk is low — 2.1 adds a version badge to the app row and a version line in the details header; 2.2 adds a History section below the log pane. These are additive, spatially-separate hunks.
- `src/models.rs` overlap: both add new types; additive, no conflict expected.
- `Cargo.toml` overlap: 2.2 adds `chrono`; 2.1 adds nothing new. Additive.

**Merge order for Phase 1:** merge 2.1 first (smaller `app.rs` footprint), then 2.2.

**Conflict map summary:**

| Branch | Files touched | Conflict risk |
|---|---|---|
| `warden/v0.5-version-checks` (2.1) | `src/version_checker.rs`(new), `src/models.rs`, `src/app.rs`, `src/config.rs`, `Cargo.toml` | Low |
| `warden/v0.5-history-uptime` (2.2) | `src/history.rs`(new), `src/models.rs`, `src/app.rs`, `Cargo.toml` | Low (additive app.rs hunks) |

---

## 4. Out of Scope for v0.5

- **Multi-directory watching** — deferred to v0.6+; requires richer config and UI scanner refactor
- **Ensign HTTP health polling** — deferred; Ensign API shape not documented
- **GUI settings panel** — backlog
- **Log persistence between sessions** — backlog
- **Log search / filter** — backlog
- **Notification click actions / deep links** — backlog
- **History analytics / charts** — backlog

---

## 5. Status Ledger

### Phase 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.5 |
|---|---|---|---|---|
| `warden/v0.5-version-checks` (#20) | ☐ | ☐ | ☐ | ☐ |
| `warden/v0.5-history-uptime` (#21) | ☐ | ☐ | ☐ | ☐ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 0.4.0 → 0.5.0) | ☐ |
| `version-history.md` entry | ☐ |
| project-release (merge+tag+push) | ☐ |
| Issues #20 #21 closed | ☐ |

### Follow-ups discovered during implementation

_(empty at start)_
