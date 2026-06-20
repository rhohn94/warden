# Roadmap

## v0.1 — Local fleet deployment manager

Plan: [`release-planning-v0.1.md`](release-planning-v0.1.md).

First release. Warden watches a directory of Grimoire-ecosystem apps, shows
their running status and port, and provides Start / Stop controls via a native
egui desktop window.

**Scope:**
- Directory scanner with `grimoire-config.json` discovery
- lsof + pgrep + `.port` file status/port detection
- Launch (recipes.json server command or direct binary) + stop (SIGTERM/SIGKILL)
- egui window: app list, status badges, Start/Stop buttons, scan button
- CLI args: `--apps-dir` and `--refresh`
- Architecture design doc (`docs/design/app-design.md`)
- UX design language adaptation (Aura/obsidian)

## v0.2 — Obsidian UI + Core Fixes

Plan: [`release-planning-v0.2.md`](release-planning-v0.2.md).

Ships Obsidian theme system and Badge widgets; closes three functional gaps:
Scan now trigger, stale entry removal, and open-in-browser for web apps.

**Scope:**
- Stale entry removal when app dirs disappear from scan
- Open-in-browser button for running web apps with a known port
- Wire the Scan now button to force an immediate scanner cycle
- (Pre-committed) Obsidian theme system integration + Badge widget
- (Pre-committed) Scanner: grimoire-build-info.json detection + tracing
- (Pre-committed) Justfile run/deploy recipes

## v0.3 — Aura polish + headless inspection

Plan: [`release-planning-v0.3.md`](release-planning-v0.3.md).

Completes the Aura design foundation and ships the Grimoire-baseline headless
verification surface.

**Scope:**
- Visual-inspection CLI (`--dump-ui` flag emits AppState JSON to stdout)
- Apply Aura spacing and radius tokens throughout `src/app.rs`
- App details pane (`SidePanel::right`, click-to-select row, metadata display)

## v0.4 — Persistent Settings + Notifications

Plan: [`release-planning-v0.4.md`](release-planning-v0.4.md).

Reduces friction at startup and keeps users informed without polling the window.

**Scope:**
- Persistent settings: TOML config file (`~/.config/warden/config.toml`) saves `--apps-dir` and `--refresh`; CLI args override config on startup
- macOS status-change notifications: desktop notification via `notify-rust` (or `mac-notification-sys`) when an app transitions Running → Stopped or Stopped → Running
- Log streaming / tail window: a scrollable log pane in the details panel (`SidePanel::right`) showing the last N stdout/stderr lines from the currently-selected running app's child process

## v0.5 — Runtime Insights

Plan: [`release-planning-v0.5.md`](release-planning-v0.5.md).

Adds observable runtime data — per-app version update indicators and
start/stop history with live uptime — so operators can see at a glance
whether apps are stale and how stable they have been.

**Scope:**
- Version update checks: async background checks against each app's git remote; badge in app list and details pane (Issue #20)
- History / uptime tracking: per-app ring buffer of start/stop events, persisted to `~/.config/warden/history.json`; History sub-section + live Uptime counter in details panel (Issue #21)

## v0.6 — Multi-source Monitoring

Plan: [`release-planning-v0.6.md`](release-planning-v0.6.md). (implementation complete — pending release)

Expands Warden's monitoring surface: a dedicated log viewer aggregates stdout
from all running apps in one panel, and the app scanner can now watch multiple
directories simultaneously.

**Scope:**
- Dedicated log viewer: top-bar `[Logs]` toggle switches the central area to an aggregated log panel with per-app chip filters and auto-scroll (Issue #22)
- Multi-directory watching: `--apps-dir` becomes repeatable; `Scanner` accepts `Vec<PathBuf>` roots; `AppEntry` gains a `root` field; stale removal per root (Issue #23)

## v0.7 — Performance + Stability

Resolves UI freeze under load: enforces scan throttling so in-flight scans
cannot stack, adds per-app detector concurrency, and instruments hot paths
with a machine-readable `perf.log` for diagnostics.

**Scope:**
- Scan throttling: drop force-scan trigger when scan is in-flight; per-app 2 s detector timeout; `tokio::join_all` concurrency for per-app detectors (Issue #24 — part 1)
- Performance telemetry: `perf.log` writer (cycle duration, drop count, slowest-app); frame-time warn via `ctx.input`; configurable `perf.frame_warn_ms` in `config.toml` (Issue #24 — part 2)

## v0.8 — Operator UX

Improves the day-to-day experience of running a fleet: atomic Restart button,
live app-list search, and crash detection with a distinct badge so unexpected
exits are immediately visible.

**Scope:**
- Restart action: `[Restart]` button for Running apps — stops then starts atomically (Issue #25)
- App list search: live text-filter field above the app list; Escape clears (Issue #26)
- Crash detection: `AppStatus::Crashed` variant; scanner distinguishes user-stop from unexpected exit; red badge + notification + history entry (Issue #27)

## v1.0.1 — Stability fixes

Patch release resolving three operator-facing defects in process control,
list ordering, and version display.

**Scope:**
- Stop-hang fix: dispatch tasks release the `AppState` lock before the blocking `history.save()` disk write so the render thread is never starved (Issue #35)
- Stable list order: concurrent detector results are sorted by app name so the running-apps list no longer reshuffles each scan cycle (Issue #36)
- Version display for `current/` layouts: read `grimoire-build-info.json` from the versioned `current/` symlink dir when absent at the app root (Issue #37)

## v1.0 — Changelog Visibility

Shows Warden's own release history inside the app: a clickable version label in the header opens a scrollable changelog window with Aura card surfaces for each release.

**Scope:**
- Changelog data layer: embed and parse `docs/version-history.md` at compile time; `ChangelogEntry` type (Issue #33)
- Version label in header: clickable `v1.0.0` label opens the changelog viewer (Issue #33)
- Changelog viewer window: scrollable Aura-styled `egui::Window` with `card_show` per version, "Close" button (Issue #34)

## v0.9 — Aura Fidelity

Brings Warden into full alignment with the Obsidian/Aura design system — every widget, surface, and typographic element uses the correct Aura primitive.

**Scope:**
- TactileButton migration: all action buttons use `TactileButton` with the correct Aura variant (Issue #28)
- Aura visual foundation: correct surface hierarchy fills, aurora wallpaper, `SURFACE_1` selection tint (Issue #29)
- Aura typography and dividers: `apply_type_tokens`, `TEXT_MUTED` secondary labels, `hairline`, `LabeledDivider` (Issue #30)
- Card and elevated panel framing: app rows use `card_show`; details body uses `elevated_panel_show` (Issue #31)
- Obsidian navigation widgets: `TabStrip` for tab toggle; `ButtonGroup` for log filter chips (Issue #32)

## Backlog

- Ensign HTTP health polling
