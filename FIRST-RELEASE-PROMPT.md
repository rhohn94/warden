SKIP ONBOARDING

# Warden — Deployment Manager · v0.1 Kickoff

You are starting a brand-new project called **Warden**. This document is the complete design specification and kickoff prompt for the v0.1 release. Read it fully before taking any action.

---

## What Warden is

A local Rust desktop app launched by double-clicking a compiled binary. It watches a directory of deployed apps, identifies each one using common Grimoire-ecosystem interfaces, shows whether the app is running and on which port, and provides Start / Stop buttons. No server, no web UI, no database.

**Name rationale:** a warden manages and guards the things in its care — Warden does the same for the fleet of locally deployed apps.

---

## Grimoire bootstrap

This project uses Grimoire v3.36, **Noir paradigm**, **Efficient workflow**, and the **Cheap-Sonnet model profile** (Sonnet for all non-trivial tasks; no Opus). The `grimoire-config.json` is already pre-populated. Run `workflow-bootstrap` non-interactively to finish repo setup (create `dev` + `version/0.1` branches, write remaining config, initialize docs). Then proceed with the v0.1 release plan below.

---

## Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Rust (edition 2021) | single-binary, no runtime deps |
| GUI | `obsidian` crate (egui/wgpu) | Aura's native Rust reimplementation |
| Design tokens | Aura (via obsidian's auto-generated bindings) | `obsidian-api::aura_generated` |
| Async | `tokio` (full) | background scanner loop |
| Config | `serde` + `serde_json` | reads grimoire JSON files |
| Home dir | `dirs` crate | expand `~` in default paths |

**No web stack, no Axum, no SQLx, no Postgres.** This is a pure native desktop tool.

---

## Repository layout (target for v0.1)

```
warden/
  src/
    main.rs          — binary entry point; AppRunner init
    app.rs           — App struct (implements eframe::App); top-level render loop
    scanner.rs       — watches the apps directory; discovers AppEntry records
    detector.rs      — per-app status/port detection (lsof + port files)
    launcher.rs      — start / stop via recipes.json or direct binary exec
    models.rs        — AppEntry, AppStatus, PortInfo types
  Cargo.toml
  Cargo.lock
  .claude/           — Grimoire scaffold (already present)
  docs/
    design/
      app-design.md        — architecture decision record for v0.1
      ux/
        design-language.md — Aura adaptation for egui (run design-language-adapt)
  ux-demo/           — Obsidian widget gallery stub (run ux-demo-build)
  README.md
  FIRST-RELEASE-PROMPT.md
```

---

## Cargo.toml sketch

```toml
[package]
name    = "warden"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "warden"
path = "src/main.rs"

[dependencies]
# GUI — pin to the latest obsidian tag; use a path dep during local dev
obsidian = { git = "https://github.com/rhohn94/obsidian", tag = "v0.44.0" }
eframe   = { version = "0.31" }

# Async runtime
tokio    = { version = "1", features = ["full"] }

# JSON / config reading
serde      = { version = "1", features = ["derive"] }
serde_json = "1"

# Home dir expansion for default path
dirs = "5"

[profile.release]
opt-level = 3
strip     = true
```

> **Note:** obsidian's egui/wgpu/winit versions must match eframe exactly. Check
> `obsidian/Cargo.toml` at the pinned tag and align the `eframe` version here.
> The egui lockstep is CI-enforced in obsidian — do not mix versions.

---

## Core data model (`src/models.rs`)

```rust
#[derive(Debug, Clone)]
pub struct AppEntry {
    pub dir: PathBuf,
    pub name: String,               // from grimoire-config.json "name", or dir name
    pub version: Option<String>,    // from grimoire-config.json framework-version or version field
    pub status: AppStatus,
    pub port: Option<u16>,
    pub server_command: Option<String>, // from recipes.json targets.server.command
    pub last_checked: std::time::Instant,
}

#[derive(Debug, Clone, PartialEq)]
pub enum AppStatus {
    Running { pid: u32 },
    Stopped,
    Unknown,
}
```

---

## App discovery (`src/scanner.rs`)

Scan each **direct subdirectory** of the watched root. A subdirectory is a recognized app if it contains a `grimoire-config.json` file.

```
watched_root/           (default: ~/Projects/deployed-apps/)
  familiar/
    grimoire-config.json   ← recognized
    current -> versions/v1.25.1/
    ...
  goon-cave/
    grimoire-config.json   ← recognized
    ...
  random-folder/           ← ignored (no grimoire-config.json)
```

From `grimoire-config.json`, read:
- `"name"` → `AppEntry.name`
- `"framework-version"` → `AppEntry.version` (fallback to directory name if absent)

From `recipes.json` (if present in the same dir), read:
- `targets.server.command` → `AppEntry.server_command` (use for Start)

The scanner runs on a background `tokio` task, re-scanning every **5 seconds**. It sends updated `Vec<AppEntry>` through a `tokio::sync::watch` channel to the GUI.

---

## Status and port detection (`src/detector.rs`)

For each discovered app, determine `AppStatus` and `Option<u16>` port using this priority order:

### Port detection (try in order, stop at first hit)
1. Read `.port` file in the app directory (contains a plain integer, e.g. `8080`)
2. Read `PORT` file in the app directory (same format)
3. Read `recipes.json` → check if `targets.server.command` contains `--port <N>` or `PORT=<N>` (parse it)
4. Port unknown → `None`

### Process detection (try in order, stop at first hit)
1. If port is known: `lsof -ti :<port>` → returns PID(s); if any → `Running { pid }`
2. Look for the binary name in `current/` symlink target directory; `pgrep -f <binary-name>` → PID
3. `pgrep -f <dir-name>` as a fuzzy fallback
4. No signal → `Stopped` (or `Unknown` if we couldn't probe at all due to permissions)

**Use `std::process::Command` to call `lsof` and `pgrep`.** Parse stdout for PIDs. Treat non-zero exit codes as "not found", not as errors.

---

## Launch / stop (`src/launcher.rs`)

### Start
1. If `AppEntry.server_command` is set: run it as a background process with `tokio::process::Command::new("sh").arg("-c").arg(command)`, spawned in `app_entry.dir` as the working directory.
2. Else: look for a binary at `<dir>/current/<name>` or `<dir>/current/bin/<name>` and exec it directly.
3. Store the resulting `Child` handle in a `HashMap<PathBuf, Child>` so we can stop it later.
4. After spawning, wait 1 second then re-run detection to refresh the status.

### Stop
1. If we have a `Child` handle for this app: call `.kill()` on it (SIGKILL via Tokio), then `.wait()`.
2. Else: look up the PID from the last known `AppStatus::Running { pid }` and `kill(pid, SIGTERM)`. If still running after 5 s, SIGKILL.
3. After stopping, wait 500 ms then re-run detection to refresh the status.

---

## GUI layout (`src/app.rs`)

Single egui window, no dock panels needed for v0.1. Use Obsidian's `AppRunner` as the entry point.

### Window: 640 × 480, resizable, title "Warden"

```
┌─────────────────────────────────────────────────────────┐
│  Warden                              [Scan now]          │
│  ~/Projects/deployed-apps/                               │
├─────────────────────────────────────────────────────────┤
│  ● familiar                    v1.25.1   port 7700  [Stop]  │
│  ○ goon-cave                   v8.48     —          [Start] │
│  ● discord-bot                 v0.6.0    port 3000  [Stop]  │
│  ? music-collection            —         —          [Start] │
├─────────────────────────────────────────────────────────┤
│  Auto-refresh: 5s        Last scan: 2s ago               │
└─────────────────────────────────────────────────────────┘
```

### Status badge colors (Aura tokens)
- `●` green (`aura_generated::COLOR_SUCCESS` or nearest) → Running
- `○` muted/grey (`aura_generated::COLOR_SURFACE_MUTED` or nearest) → Stopped
- `?` amber (`aura_generated::COLOR_WARNING` or nearest) → Unknown

### Button behavior
- **[Start]** visible when status is Stopped or Unknown; disabled if a start is in-flight
- **[Stop]** visible when status is Running; disabled if a stop is in-flight
- In-flight state: show a spinner or greyed label "Starting…" / "Stopping…"

### Responsiveness
The GUI thread must never block. All detection and launcher calls happen on the background tokio runtime, communicating back to the GUI via `watch` channels and `Arc<Mutex<>>` state.

---

## CLI interface

```
warden [--apps-dir <path>] [--refresh <secs>]

Options:
  --apps-dir <path>    Directory to scan (default: ~/Projects/deployed-apps)
  --refresh <secs>     Auto-refresh interval in seconds (default: 5)
```

Parse with `std::env::args()` directly — no clap dependency needed for v0.1.

---

## Design language (Aura / Obsidian)

Run **`design-language-adapt`** early in the release to pull the current Aura token snapshot into `docs/design/ux/design-language.md`. Then run **`ux-demo-build`** to stand up a `ux-demo/` widget gallery so you can visually verify the Aura token application before building the main app.

Key principle: use Obsidian's widget primitives and `aura_generated` constants everywhere. Do not hand-roll colors or spacing values.

---

## v0.1 release scope

**In scope (must ship):**
- [ ] `scanner.rs`: directory scan + `grimoire-config.json` reading + 5s refresh loop
- [ ] `detector.rs`: lsof + pgrep + `.port` file detection
- [ ] `launcher.rs`: start (recipes.json server command or direct binary) + stop (SIGTERM/SIGKILL)
- [ ] `app.rs`: egui window with app list, status badges, Start/Stop buttons, scan button
- [ ] `main.rs`: AppRunner entry, tokio runtime, CLI arg parsing
- [ ] `docs/design/app-design.md`: architecture decision record
- [ ] `docs/design/ux/design-language.md`: Aura adaptation (via `design-language-adapt`)
- [ ] Passing `cargo build --release` + `cargo test` + `cargo clippy`

**Out of scope for v0.1:**
- Multi-directory watching
- Log streaming / tail window
- Notifications (macOS NSUserNotification)
- Config file (TOML/JSON persistent settings)
- Ensign HTTP health polling
- Version update checks
- History / uptime tracking

---

## Grimoire config decisions (pre-set in `.claude/grimoire-config.json`)

| Setting | Value | Reason |
|---|---|---|
| Paradigm | Noir | Autonomous — this is a new greenfield app |
| Model profile | Cheap-Sonnet | Sonnet for all non-trivial work; no Opus |
| Workflow variant | Efficient | Balanced parallelism |
| Max parallel worktrees | 2 | Small app; keeps costs down |
| Autonomous push | true | Standard fleet behavior |
| GH Releases | true | Fleet visibility |

**Never call Opus** during this project. If a task exceeds what Sonnet can handle at medium effort, split it into smaller items instead.

---

## Where to find ecosystem context

- **Obsidian repo:** `../obsidian/` — check `crates/obsidian/` for widget API; `crates/obsidian-api/src/aura_generated.rs` for token constants
- **Obsidian version:** v0.44.0 (latest tag at project creation)
- **Aura version:** v3.82 (latest at project creation)
- **Familiar** (`../familiar/`): the best working example of an Obsidian consumer; look at its `src/` and `ux-demo/` for egui integration patterns
- **Grimoire v3.36 skills:** `.claude/skills/` — `design-language-adapt`, `ux-demo-build`, `repo-reference`, `workflow-bootstrap`

---

## First steps for the agent

1. Run `workflow-bootstrap` non-interactively (Noir, Cheap-Sonnet, Efficient) — creates `dev` + `version/0.1` branches, fills command table in `CLAUDE.md`, seeds `docs/`
2. File v0.1 work items in GitHub Issues (`rhohn94/warden`) using the scope list above — one issue per logical component
3. Run `release-planning` to size and assign items
4. Begin the release: `integration-master` drives `release-phase` per item
5. Run `design-language-adapt` early (before any widget work) to establish the Aura adaptation doc
