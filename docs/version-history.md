# Version history

## v0.1.0 (2026-06-16)

- Initial release: native egui desktop app for monitoring and controlling a fleet of locally deployed Grimoire apps
- Directory scanner discovers apps by `grimoire-config.json` presence
- Port and process detection via lsof, pgrep, and `.port` files
- Start/Stop controls using recipes.json server commands or direct binary exec
- Status badges (Running / Stopped / Unknown) with Aura design tokens
- CLI flags: `--apps-dir` and `--refresh`
