# Release Planning — v0.4

> status: agreed
> Companion to `docs/design/app-design.md` and `docs/version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v0.4.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v0.4.0` |
| **Previous** | v0.3.0 (Aura polish + headless inspection) |
| **Theme** | "Persistent Settings + Notifications" — saves startup configuration to disk and notifies users of app status changes passively, plus adds a live log tail pane in the details panel. |

---

## 2. Major Features

### 2.1 Persistent Settings — TOML config file (Issue #17)

**Description:** Introduce `src/config.rs` with a `Config` struct that loads from `~/.config/warden/config.toml` (via the `dirs` crate) and saves back on explicit set. CLI args (`--apps-dir`, `--refresh`) override config values. Adds `toml = "0.8"` to `Cargo.toml`.

**Acceptance criteria:**
- `~/.config/warden/config.toml` is created with defaults on first run if absent; missing file is not an error
- TOML schema supports `apps_dir` (string, optional) and `refresh_secs` (u64, optional, default 5)
- Load order: config file → CLI args (CLI overrides config)
- Save-on-change: when CLI provides `--apps-dir` or `--refresh`, write back to config so the next launch remembers it
- New `src/config.rs` with `Config::load()` and `Config::save()` methods
- Unit test: writes a temp config, reads it back, verifies values
- `cargo test` passes

**Branch:** `warden/v0.4-persistent-settings`

**Design doc:** No separate design doc needed; spec is complete in the issue body and this plan.

---

### 2.2 macOS Status-Change Notifications (Issue #18)

**Description:** Introduce `src/notifier.rs` that tracks previous-status for each app and fires a macOS desktop notification (via `notify-rust = "4"`) on Running → Stopped or Stopped → Running transitions. Does not fire on initial startup scan populate.

**Acceptance criteria:**
- Notification fires on **Running → Stopped** and **Stopped → Running** transitions only (not on first-scan populate)
- Title: `"Warden"`, body: `"<AppName> is now <Running|Stopped>"`
- `notify-rust` crate added to `Cargo.toml`; degrades silently on platforms without notification support
- `config.toml` key `notifications_enabled` (bool, default `true`) gates notifications; loaded via `src/config.rs` (2.1)
- New `src/notifier.rs` with `Notifier` struct and `check_transitions(prev, next)` method
- `src/models.rs` ensures `AppStatus` is `PartialEq`
- Unit test: feeds a sequence of `AppStatus` changes, verifies correct transitions emitted
- `cargo test` passes

**Branch:** `warden/v0.4-notifications`

**Design doc:** No separate design doc needed; spec is complete in the issue body and this plan.

---

### 2.3 Log Streaming / Tail Window (Issue #19)

**Description:** Introduce `src/log_capture.rs` with a ring-buffer + async reader that captures child process stdout/stderr. The app details panel (`SidePanel::right`, introduced v0.3) gains a scrollable log pane showing the last N lines (default 500) for the selected Running app.

**Acceptance criteria:**
- Log pane appears in details panel when selected app is Running and was started by Warden in the current session
- When app was not started by Warden: show `"Log streaming not available — app was not started by Warden this session."`
- Captures both stdout and stderr in arrival order
- Ring buffer retains last 500 lines (configurable via `config.toml` key `log_tail_lines`, default 500)
- Auto-scrolls to bottom on new lines; pauses auto-scroll when user scrolls up
- Pane height ~160 pt, `TextStyle::Monospace`, 11 pt
- New `src/log_capture.rs` (ring buffer + `tokio::io::BufReader` + `tokio::sync::mpsc`)
- `src/launcher.rs` modified to pipe child stdout/stderr through the capture channel
- `src/app.rs` renders log pane in details panel
- Unit test: push > 500 lines into ring buffer, verify only last 500 retained in oldest-to-newest order
- `cargo test` passes

**Branch:** `warden/v0.4-log-streaming`

**Design doc:** No separate design doc needed; spec is complete in the issue body and this plan.

---

## 3. Parallel Implementation Strategy

**Phase 1 (parallel — batch 1):** Items 2.1 and 2.2 can run concurrently.
- 2.1 (persistent settings) touches `src/config.rs` (new), `src/main.rs`, `Cargo.toml`
- 2.2 (notifications) touches `src/notifier.rs` (new), `src/app.rs` (notifier call-site), `src/models.rs`, `Cargo.toml`
- `src/app.rs` overlap: 2.2 adds a single `notifier.check_transitions()` call after the scanner update — this is a small, additive change and the files are otherwise disjoint. If a merge conflict arises it is trivially resolvable (additive hunks).
- `Cargo.toml` overlap: both add one new dependency line — additive, no conflict expected.
- 2.2 depends on `Config.notifications_enabled` from 2.1. To allow parallel dispatch: 2.2 agent should read `notifications_enabled` from a local `Config` default if 2.1's `src/config.rs` is not yet merged. 2.2 merges after 2.1.

**Merge order for Phase 1:** merge 2.1 first, then 2.2 (to pick up `src/config.rs` cleanly).

**Phase 2 (serial — batch 2):** Item 2.3 (log streaming) runs after Phase 1 is merged, so `src/launcher.rs` and `src/app.rs` carry the Phase 1 state.
- 2.3 touches `src/log_capture.rs` (new), `src/launcher.rs`, `src/app.rs`, `src/models.rs` — no conflict with Phase 1 items since those are merged by then.

**Conflict map summary:**

| Branch | Files touched | Conflict risk |
|---|---|---|
| `warden/v0.4-persistent-settings` (2.1) | `src/config.rs`(new), `src/main.rs`, `Cargo.toml` | Low |
| `warden/v0.4-notifications` (2.2) | `src/notifier.rs`(new), `src/app.rs`, `src/models.rs`, `Cargo.toml` | Low (additive `app.rs` hunk) |
| `warden/v0.4-log-streaming` (2.3) | `src/log_capture.rs`(new), `src/launcher.rs`, `src/app.rs`, `src/models.rs` | Low (Phase 2, after Phase 1 merged) |

---

## 4. Out of Scope for v0.4

- **Multi-directory watching** — deferred to v0.5+; requires richer config and UI
- **Ensign HTTP health polling** — deferred; Ensign API shape not documented
- **Version update checks** — deferred to v0.5+
- **History / uptime tracking** — deferred to v0.5+
- **GUI settings panel** — backlog; persistent settings in v0.4 are config-file-only
- **Log persistence between sessions** — backlog
- **Log search / filter** — backlog
- **Notification click actions / deep links** — backlog

---

## 5. Status Ledger

### Phase 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.4 |
|---|---|---|---|---|
| `warden/v0.4-persistent-settings` (#17) | ☑ | ☑ | ☑ | ☑ |
| `warden/v0.4-notifications` (#18) | ☑ | ☑ | ☑ | ☑ |

### Phase 2

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.4 |
|---|---|---|---|---|
| `warden/v0.4-log-streaming` (#19) | ☐ | ☐ | ☐ | ☐ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 0.3.0 → 0.4.0) | ☐ |
| `version-history.md` entry | ☐ |
| project-release (merge+tag+push) | ☐ |
| Issues #17 #18 #19 closed | ☐ |

### Follow-ups discovered during implementation

_(empty at start)_
