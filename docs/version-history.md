# Version history

## v0.5.0 (2026-06-16)

- Version update checks: Warden now runs background checks (hourly by default) against each app's git remote and shows an "↑ vX.Y" badge in the app list when a newer version is available; the details panel shows the full "Update available: current → latest" message; configurable via `version_check_interval_secs` in `config.toml` (set to 0 to disable)
- History and uptime tracking: Warden now records every start and stop event for each monitored app in a per-app ring buffer (max 100 events); events are persisted to `~/.config/warden/history.json` across sessions; the details panel shows a live uptime counter for running apps and a reverse-chronological history of the last 10 start/stop events with timestamps and durations

## v0.4.0 (2026-06-16)

- Persistent settings: startup configuration (`--apps-dir`, `--refresh`) is saved to `~/.config/warden/config.toml` automatically; CLI flags still override the config; subsequent launches remember your last-used directory and refresh interval without re-specifying flags
- Status-change notifications: Warden now fires a macOS desktop notification whenever a monitored app transitions between Running and Stopped (or vice versa); controlled by `notifications_enabled` in `config.toml` (default on)
- Log streaming tail window: the app details panel (`SidePanel::right`) now includes a scrollable 160-pt log pane showing the last 500 lines of stdout/stderr from the selected app's child process when it was started by Warden in the current session; auto-scrolls to the bottom on new output

## v0.3.0 (2026-06-16)

- Visual-inspection CLI (`--dump-ui`): prints a stable JSON snapshot of `AppState` (entries, statuses, ports) to stdout and exits — no window or GPU required; suitable for scripting and regression checks
- Aura spacing and radius tokens: replaced all bare `f32` spacing/radius literals in `src/app.rs` with `obsidian::aura::golden` constants (`SPACE[N]`, `CONTROL_HEIGHT_SM`, `RADIUS_SM`); Start/Stop/Open/Scan buttons sized to 32 pt height with 8 pt corner radius
- App details pane: clicking any app row opens a `SidePanel::right` (280 pt) showing name, status badge, PID, directory, Grimoire version, tech stack, known/detected ports, server command, and Start/Stop/Open actions; re-clicking deselects; default window widened to 920×540 pt

## v0.2.0 (2026-06-16)

- Stale entry removal: apps that disappear from the scanned directory are removed from the display on the next scan cycle
- Open-in-browser button: running web apps with a known port show an [Open] button that launches http://localhost:&lt;port&gt; in the default browser
- Scan now trigger: [Scan now] button immediately wakes the scanner via a force-scan channel, bypassing the auto-refresh interval
- Obsidian theme system: `theme::install_bundled_fonts` + `theme::set_active(Theme::aura_default())` now active at startup; manual `set_visuals` removed
- Badge widget: status pills use `Badge::new(label, BadgeStatus::…)` from obsidian widgets; removed hand-rolled colored labels
- Scanner: detects apps via `grimoire-build-info.json` in addition to `grimoire-config.json`; added auto-detection for `current/` and `versions/` layout dirs
- Runtime fix: `runtime.enter()` guard before `scanner::start` prevents `tokio::spawn` from failing on the main thread
- Justfile: `just run` and `just deploy` recipes for development convenience

## v0.1.0 (2026-06-16)

- Initial release: native egui desktop app for monitoring and controlling a fleet of locally deployed Grimoire apps
- Directory scanner discovers apps by `grimoire-config.json` presence
- Port and process detection via lsof, pgrep, and `.port` files
- Start/Stop controls using recipes.json server commands or direct binary exec
- Status badges (Running / Stopped / Unknown) with Aura design tokens
- CLI flags: `--apps-dir` and `--refresh`
