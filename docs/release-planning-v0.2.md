---
title: Warden v0.2 Release Planning
status: agreed
created: 2026-06-16
---

# Release Planning — v0.2

> status: agreed

---

## 1. Target

| | |
|---|---|
| **Version** | `v0.2` |
| **Previous** | `v0.1.0` (2026-06-16) |
| **Theme** | "Obsidian UI + Core Fixes" — ships the Obsidian theme system and Badge widgets (pre-committed to dev), then closes three functional gaps: Scan now trigger, stale entry removal, and open-in-browser for running web apps. |

---

## 2. Work Items

### §2.1 (Pre-committed) Obsidian theme system + Badge widget + eframe removal

Landed on dev in commit `5bfc214`. Closes #10, #11.

- Removed unused `eframe = "0.31"` dependency
- `theme::install_bundled_fonts` + `theme::set_active(Theme::aura_default())` at startup
- Replaced `ui.colored_label` badge with `Badge::new(label, BadgeStatus::…).ui(ui)`
- panel_fill now correct via `aura_default()` (resolves #11 token drift)

**Status:** pre-merged to dev

---

### §2.2 (Pre-committed) Scanner — grimoire-build-info.json detection + auto-detection + tracing

Landed on dev in commit `06f50b9`.

- Detects apps via `grimoire-build-info.json` in addition to `grimoire-config.json`
- Auto-detects `current/` and `versions/` layout directories
- Tracing instrumentation throughout scanner paths

**Status:** pre-merged to dev

---

### §2.3 (Pre-committed) Tokio runtime entry guard

Landed on dev in commit `e9f41e0`.

- `let _runtime_guard = runtime.enter()` before `scanner::start` so `tokio::spawn` works from the main thread before the event loop starts

**Status:** pre-merged to dev

---

### §2.4 (Pre-committed) Justfile — run and deploy recipes

Landed on dev in commit `e929dff`.

- `just run` builds and launches Warden
- `just deploy` builds a release binary

**Status:** pre-merged to dev

---

### §2.5 Stale entry removal

When an app directory disappears from the scanned root (or loses `grimoire-config.json`), the entry must be removed from the display on the next scan cycle. Currently stale entries persist until restart.

**Acceptance criteria:**
- After each scan, compute the removed set: `previous_paths − new_paths`
- For each removed path: delete from `statuses` and `in_flight` in `AppState`
- Write at least one unit test: create a temp dir with two app subdirs, scan (see both entries), delete one subdir, scan again (one entry remains, one is gone)
- `cargo test` passes clean

**Branch:** `warden/v0.2-stale-entries`
**Issues closed:** #14
**Est. tokens:** ~20K

---

### §2.6 Open-in-browser button

For a running web app with a known port, add an `[Open]` button that launches the app URL in the system default browser.

**Acceptance criteria:**
- Add `open = "5"` to `[dependencies]` in `Cargo.toml`
- In `draw_ui`, when `is_running && port_info.port.is_some()`, render an `[Open]` button in the per-app horizontal row (right of Stop button)
- On click: `let _ = open::that(format!("http://localhost:{}", port));` — log the error if it fails, do not surface to UI
- `cargo check` and `cargo test` pass clean

**Branch:** `warden/v0.2-open-browser`
**Issues closed:** #15
**Est. tokens:** ~15K

---

### §2.7 Scan now trigger

Wire the existing `[Scan now]` button (currently a no-op comment) to immediately trigger a scanner cycle without waiting for the next auto-refresh interval.

**Acceptance criteria:**
- `scanner::start` accepts a `tokio::sync::watch::Receiver<()>` force-scan channel (or equivalent)
- `App` or `AppState` holds the corresponding `Sender`
- In the scanner loop: `tokio::select!` on the auto-refresh sleep OR the force-scan channel — whichever fires first triggers a scan
- `[Scan now]` button click sends on the channel and updates `last_scan` in AppState
- Existing scanner tests remain green; add one test verifying the channel wakes the scanner early
- `cargo test` passes clean

**Branch:** `warden/v0.2-scan-now`
**Issues closed:** #13
**Est. tokens:** ~25K

---

## 3. Parallel Implementation Strategy

### Phase structure

Max parallel worktrees: **2** (per `grimoire-config.json`).

| Phase | Items | Branches | File overlap |
|---|---|---|---|
| 1 (parallel) | §2.5 Stale entries + §2.6 Open browser | `warden/v0.2-stale-entries`, `warden/v0.2-open-browser` | None — scanner.rs vs. app.rs + Cargo.toml |
| 2 (serial) | §2.7 Scan now trigger | `warden/v0.2-scan-now` | Touches both scanner.rs and app.rs — must follow Phase 1 merges |

### Merge order

Phase 1: merge `warden/v0.2-stale-entries` first (scanner.rs only), then `warden/v0.2-open-browser` (app.rs + Cargo.toml) — no conflict between them.
Phase 2: merge `warden/v0.2-scan-now` after both Phase 1 branches are fully merged into `version/0.2`.

### Dependencies

- §2.7 depends on Phase 1 both being merged: its scanner.rs changes build on §2.5's stale-removal state, and its app.rs changes build on §2.6's open-browser button layout.

---

## 4. Out of Scope for v0.2

| Item | Target | Reason |
|---|---|---|
| App details pane (#16) | v0.3+ | Larger feature requiring new UI layout |
| Aura spacing/radius tokens (#12) | v0.3+ | Polish pass; no functional gap |
| Visual-inspection CLI (#9, Grimoire-Requirement) | v0.3 | Large feature; not blocking v0.2 functionality |
| Multi-directory watching | v0.3+ | — |
| Log streaming / tail window | v0.3+ | — |
| macOS NSUserNotification support | v0.3+ | — |
| Persistent settings (TOML config) | v0.3+ | — |
| Ensign HTTP health polling | v0.3+ | — |
| Version update checks | v0.3+ | — |
| History / uptime tracking | v0.3+ | — |
| ux-demo/ widget gallery | v0.3+ | — |

**Grimoire-Requirement note:** Issue #9 (gui-visual-inspection-cli) is required by Grimoire baseline-version 3 for all GUI projects. It is deferred to v0.3 as the sub-requirements (offscreen wgpu render or `--dump-ui` JSON) need their own design phase. It will appear as a §2 flagship item in the v0.3 work-items report.

---

## 5. Status Ledger

### Pre-committed (on dev, ahead of main)

| Item | Commit | Implemented | Reviewed | On version/0.2 |
|---|---|---|---|---|
| §2.1 Obsidian theme + Badge + eframe removal | `5bfc214` | ☑ | ☑ | ☑ (via dev base) |
| §2.2 Scanner enhancements | `06f50b9` | ☑ | ☑ | ☑ (via dev base) |
| §2.3 Tokio runtime entry | `e9f41e0` | ☑ | ☑ | ☑ (via dev base) |
| §2.4 Justfile | `e929dff` | ☑ | ☑ | ☑ (via dev base) |

### Phase 1

| Branch | Implemented | Reviewed | Merged into version/0.2 |
|---|---|---|---|
| `warden/v0.2-stale-entries` | ☐ | ☐ | ☐ |
| `warden/v0.2-open-browser` | ☐ | ☐ | ☐ |

### Phase 2

| Branch | Implemented | Reviewed | Merged into version/0.2 |
|---|---|---|---|
| `warden/v0.2-scan-now` | ☐ | ☐ | ☐ |

### Follow-ups

_(none yet)_
