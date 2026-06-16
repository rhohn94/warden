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

## Backlog

- App details pane (tech stack, Grimoire version, Aura version) (#16)
- Apply Aura spacing and radius tokens throughout UI (#12)
- Visual-inspection CLI for headless agent verification (#9, Grimoire-Requirement, v0.3)
- Multi-directory watching
- Log streaming / tail window
- macOS NSUserNotification support
- Persistent settings (TOML config file)
- Ensign HTTP health polling
- Version update checks
- History / uptime tracking
