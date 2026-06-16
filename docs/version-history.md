# Version history

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
