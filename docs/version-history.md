# Version history

## v0.8.0 (2026-06-17)

- Restart button: Running apps now have a `[Restart]` button that atomically stops and restarts them in one click — no more manual Stop-then-Start cycle; the button disables and shows `[Restarting…]` while the operation is in progress
- App list search: a `Filter apps…` text field above the app list narrows the displayed rows to apps whose name contains the query (case-insensitive); the status bar shows "Showing N of M apps" when a filter is active; pressing Escape clears the filter
- Crash detection: when a Warden-managed app exits unexpectedly (not via Stop or Restart), it is shown with a red `Crashed` badge; a desktop notification fires immediately; the history panel records the crash as a distinct event; clicking Start restores normal monitoring

## v0.7.0 (2026-06-16)

- Scan throttling: force-scan triggers are dropped (not queued) when a scan is already in-flight — a `tracing::debug!` line logs each drop; per-app detector calls now run concurrently via `tokio::task::JoinSet` so one slow `lsof`/`pgrep` no longer serialises the rest; each per-app detector call times out at 2 s and marks the app `Unknown` on timeout
- Performance telemetry: each scan cycle appends a machine-readable line to `~/.config/warden/perf.log` (cycle duration, drop count, optional slowest-app); egui frame time is logged at warn level when it exceeds a configurable threshold (`perf.frame_warn_ms` in `config.toml`, default 50 ms)

## v0.6.0 (2026-06-16)

- Dedicated log viewer: a `[Logs]` toggle in the toolbar switches the central panel to an aggregated log view showing stdout/stderr from all Warden-launched apps; per-app chip toggles filter by app; log lines are prefixed with the source app name (`[<app-name>] <line>`); auto-scroll follows new output and pauses when the user scrolls up, resuming when scrolled back to the bottom
- Multi-directory watching: `--apps-dir` is now a repeatable flag — pass it multiple times to monitor several app directories simultaneously; each app entry shows which root directory it came from (subdued label with full-path tooltip); stale-removal applies independently per root; single-directory behaviour is unchanged

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
