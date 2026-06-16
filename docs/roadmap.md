# Roadmap

## v0.1 — Local fleet deployment manager

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

## Backlog

- Multi-directory watching
- Log streaming / tail window
- macOS NSUserNotification support
- Persistent settings (TOML config file)
- Ensign HTTP health polling
- Version update checks
- History / uptime tracking
