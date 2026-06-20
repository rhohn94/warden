---
title: Warden v0.1 Architecture Design
status: final
created: 2026-06-16
---

# Warden v0.1 — Architecture Design

ADR capturing the module decomposition, async data-flow, and key dependency
decisions for the v0.1 "Local Fleet Deployment Manager" release.

---

## 1. Module roles

| Module | File | Role |
|---|---|---|
| **models** | `src/models.rs` | Core data types shared across all modules |
| **scanner** | `src/scanner.rs` | Background task: discovers apps by directory scan |
| **detector** | `src/detector.rs` | Synchronous: resolves port and running status per app |
| **launcher** | `src/launcher.rs` | Async: starts/stops apps, tracks child handles |
| **app** | `src/app.rs` | egui render loop: UI layout, event dispatch |
| **main** | `src/main.rs` | Binary entry point: parse CLI args, wire all modules |

Each module owns a distinct file set. There are no circular dependencies:

```
main → app
main → scanner
app  → launcher → detector → models
app  → detector
app  → scanner (watch receiver)
scanner → models
```

---

## 2. Async data-flow

```
┌─────────────────────────────────────────────────────────────────┐
│  tokio runtime (multi-thread, full features)                     │
│                                                                  │
│  scanner::start(root, interval)                                  │
│    └─ spawns background task → scans every N secs               │
│         └─ tx.send(Vec<AppEntry>) ─────────────────────────────►│
│                                                                  │
│  Arc<Mutex<AppState>>  ◄──── watch::Receiver<Vec<AppEntry>>    │
│    holds: entries, statuses, in-flight flags                     │
│                                                                  │
│  egui event loop (winit, main thread)                            │
│    app.rs::update() ──► reads Arc<Mutex<AppState>>              │
│    [Start] click ──► launcher.start(entry) via tokio::spawn     │
│    [Stop]  click ──► launcher.stop(entry)  via tokio::spawn     │
│                                                                  │
│  launcher::start/stop ──► detector::detect → refresh status     │
└─────────────────────────────────────────────────────────────────┘
```

### Watch channel flow

`scanner::start` returns a `tokio::sync::watch::Receiver<Vec<AppEntry>>`. The
`App` holds this receiver and polls it on each frame (non-blocking `.borrow()`).
When the scanner pushes a new `Vec<AppEntry>`, the App updates its local copy
and re-requests a redraw.

### Shared state

```
Arc<Mutex<AppState>> {
    entries:  Vec<AppEntry>,
    statuses: HashMap<PathBuf, (AppStatus, PortInfo)>,
    in_flight: HashSet<PathBuf>,    // buttons disabled while op is pending
}
```

All reads and writes go through the mutex. The egui `update()` method holds the
lock only long enough to snapshot the state it needs to render — it never holds
it across an await.

---

## 3. tokio runtime topology

- **Single `tokio::runtime::Runtime`** (built in `main.rs` with `Builder::new_multi_thread`).
- The scanner background task is spawned once at startup via `tokio::spawn`.
- Start/Stop button handlers are also `tokio::spawn`-ed so they do not block the
  egui render loop.
- The winit event loop runs on the main thread (required by macOS and Windows).
  `tokio::spawn` on a multi-thread runtime lets async work run on the thread pool
  while egui remains on the main thread.

---

## 4. Obsidian widget integration rationale

**Why obsidian, not raw egui?**

Obsidian provides:
- `AppRunner` / `AppDelegate` — a pre-built winit `ApplicationHandler` wrapper
  that owns the egui context, window, wgpu renderer, and DPI handling. Using it
  avoids hand-rolling ~300 lines of platform boilerplate.
- `EguiWindow` — per-window egui/wgpu paint plumbing. Warden uses a single
  window but `EguiWindow` handles HiDPI, scale changes, and accessibility
  hooks.
- **Aura tokens** (`obsidian_api::aura_generated::aura`) — a single source of
  truth for colours, spacing, radii, and sizes. See
  `docs/design/ux/design-language.md` for the full token mapping.

**eframe vs. obsidian AppRunner**

`eframe` is included in `Cargo.toml` for its egui feature flags and rendering
backend compatibility (eframe 0.31 uses the same egui 0.31 and wgpu 24 as
obsidian). However, the app shell entry point is **obsidian's `AppRunner`**,
not `eframe::run_native`. This keeps Warden on the same winit/wgpu event loop
obsidian uses internally.

---

## 4a. Fleet Control (v1.2)

Operator-facing controls for managing many apps at once, all in the Apps view
of `src/app.rs`. No new modules or dependencies.

- **Bulk actions** — `Start all` / `Stop all` / `Restart all` buttons in the
  header toolbar (Apps view only). Each folds over the currently *visible*
  (filtered) entries and dispatches the existing per-app `dispatch_start` /
  `dispatch_stop` / `dispatch_restart`, skipping apps already in the target
  state or in-flight. Bulk dispatch is just a loop over the existing single-app
  paths — no new launcher API.
- **Fleet health summary bar** — a compact one-line summary (e.g.
  `6 running · 2 stopped · 1 crashed`) rendered under the header hairline,
  computed as a fold over the `statuses` snapshot already in scope. Purely
  additive, no new state.
- **Sort & group controls** — a `ButtonGroup` selecting the app-list sort key
  (Name / Status / Port). A pure `sort_entries(entries, key, statuses)` helper
  (testable, alongside `filter_entries`) orders the filtered list before
  `draw_app_list`. The chosen key persists in `Config.sort_order` and is saved
  on change; the scanner's own `(name, dir)` order remains the stable default.
- **Auto-start-on-launch** — `Config.auto_start: Vec<String>` holds app names
  flagged to start automatically. A per-app toggle in the details panel adds/
  removes the name and saves. On the first populated scan (guarded by a
  one-shot `did_autostart` flag on `App`), each flagged app not already running
  is dispatched via `dispatch_start`.

State added to `App`: a one-shot `did_autostart: bool`. State added to
`Config`: `sort_order: Option<String>`, `auto_start: Option<Vec<String>>`
(both with sane defaults, preserved by `sanitize`).

## 5. Out-of-scope decisions and why

| Decision | Reason |
|---|---|
| No HTTP health polling | Design deferred to v0.2 (Ensign integration); lsof/pgrep is sufficient for local apps |
| No persistent settings | TOML config file deferred to v0.2; `--apps-dir` + `--refresh` CLI flags cover v0.1 |
| No log streaming | v0.2+ (requires a log tail window and scroll state the v0.1 GUI doesn't have) |
| No multi-directory watch | v0.2+; single directory covers the Grimoire-ecosystem convention |
| No async detector | Detector calls `lsof`/`pgrep` synchronously via `std::process::Command`; sub-ms on macOS. Would need `tokio::process::Command` if this became slow at scale |
| Single tokio runtime | Warden is a desktop tool with at most ~20 apps; a single multi-thread runtime is sufficient and avoids runtime nesting complexity |
| `dirs` for `~` expansion | Avoids hand-rolling `$HOME` resolution; crate is already in the ecosystem |
