# Version history

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
