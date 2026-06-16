# Warden

A local Rust desktop app that surfaces all deployed apps in a watched directory, shows their running status and port, and provides one-click start/stop.

- **GUI:** Obsidian (egui/wgpu) + Aura design tokens
- **Discovery:** scans `~/Projects/deployed-apps/` for apps with `grimoire-config.json`
- **Status:** process + port detection via lsof/ps and `.port` files
- **Control:** start/stop via `recipes.json` server command

## Stack

| | |
|---|---|
| Language | Rust |
| GUI | obsidian (egui/wgpu) |
| Design tokens | Aura (via obsidian's generated bindings) |
| Framework | Grimoire v3.36 |

## Running

```
cargo run --release
# or: double-click the compiled binary
```

Optionally pass a custom apps directory:

```
warden --apps-dir /path/to/your/deployed-apps
```

## See also

- `FIRST-RELEASE-PROMPT.md` — the design document and v0.1 kickoff prompt
- `.claude/grimoire-config.json` — Noir paradigm, Cheap-Sonnet model profile
