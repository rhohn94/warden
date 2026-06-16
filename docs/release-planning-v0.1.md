# Release Planning — v0.1

> status: draft
> Companion to `version-design.md` and `version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v0.1.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v0.1` |
| **Previous** | _(none — first release)_ |
| **Theme** | "Local Fleet Deployment Manager" — a native Rust desktop app that watches a directory of Grimoire-ecosystem apps, shows their running status and port, and provides Start/Stop controls. |

---

## 2. Major Features

### §2.1 Project setup — `Cargo.toml` + `src/models.rs`

Define the core data types and pin all Cargo dependencies.

**Acceptance criteria:**
- `Cargo.toml` includes all required deps: obsidian (git, tag v0.44.0), eframe (version aligned with obsidian's egui lockstep), tokio (full), serde + serde_json, dirs
- `src/models.rs` defines `AppEntry`, `AppStatus { Running { pid: u32 }, Stopped, Unknown }`, and `PortInfo` types
- `cargo check` passes clean with no errors or warnings

**Branch:** `warden/v0.1-setup`
**Design doc:** FIRST-RELEASE-PROMPT.md §Core data model and §Cargo.toml sketch

---

### §2.2 UX design language adaptation

Run `design-language-adapt` to replace the stub `docs/design/ux/design-language.md` with the full Aura/obsidian adaptation, documenting all token constants agents will use in the GUI.

**Acceptance criteria:**
- `docs/design/ux/design-language.md` front-matter has `adaptation-status: complete` and a non-empty `source-sha`
- Document lists concrete `aura_generated` token names for: color (success/muted/warning), spacing, typography, and interactive-element sizing
- Primary stack note reads `obsidian (egui/wgpu)`

**Branch:** `warden/v0.1-design-language`
**Design doc:** `docs/design/ux/design-language.md` (the output IS the design doc)

---

### §2.3 App discovery scanner — `src/scanner.rs`

Background tokio task that scans the watched directory every 5 seconds and sends discovered `AppEntry` records through a `watch` channel.

**Acceptance criteria:**
- Scans direct subdirectories of the watched root; recognizes a dir as an app iff it contains `grimoire-config.json`
- Reads `name` and `framework-version` (or dir name fallback) from `grimoire-config.json`
- Reads `targets.server.command` from `recipes.json` if present → `AppEntry.server_command`
- Runs on a background tokio task, re-scans every N seconds (configurable, default 5)
- Publishes `Vec<AppEntry>` through a `tokio::sync::watch` channel
- `cargo test` passes with at least one unit test verifying app discovery from a temp dir fixture

**Branch:** `warden/v0.1-scanner`
**Design doc:** FIRST-RELEASE-PROMPT.md §App discovery

---

### §2.4 Status and port detection — `src/detector.rs`

Determines `AppStatus` and `Option<u16>` for each `AppEntry` using a priority chain of detection methods.

**Acceptance criteria:**
- Port detection chain (tried in order): `.port` file → `PORT` file → parse `--port <N>`/`PORT=<N>` from `recipes.json` server command → `None`
- Process detection chain (tried in order): `lsof -ti :<port>` (if port known) → `pgrep -f <binary-name>` (from `current/` symlink) → `pgrep -f <dir-name>` → `Stopped`/`Unknown`
- Uses `std::process::Command` for `lsof`/`pgrep`; non-zero exit = not found, not an error
- `cargo test` passes with at least two tests: one that detects a running process, one that returns Stopped when nothing is running

**Branch:** `warden/v0.1-detector`
**Design doc:** FIRST-RELEASE-PROMPT.md §Status and port detection

---

### §2.5 Launch/stop controller — `src/launcher.rs`

Async module that starts and stops apps, tracking `Child` handles for graceful cleanup.

**Acceptance criteria:**
- Start: if `server_command` is set, runs via `tokio::process::Command sh -c <cmd>` in `app.dir` as CWD; otherwise looks for binary at `<dir>/current/<name>` or `<dir>/current/bin/<name>`
- Stores `Child` handle in `HashMap<PathBuf, tokio::process::Child>`
- After spawning, waits 1 second and re-triggers detection to refresh status
- Stop: if `Child` handle exists, calls `.kill().await` + `.wait().await`; otherwise sends SIGTERM to last known PID, then SIGKILL after 5 s if still running
- After stopping, waits 500 ms and re-triggers detection
- `cargo test` passes with at least one integration test verifying a process is started and stopped

**Branch:** `warden/v0.1-launcher`
**Design doc:** FIRST-RELEASE-PROMPT.md §Launch / stop

---

### §2.6 egui GUI window — `src/app.rs`

The top-level egui render loop: app list with status badges, Start/Stop buttons, scan button, and status bar. Never blocks the GUI thread.

**Acceptance criteria:**
- Single window, initial size 640×480, resizable, title "Warden"
- Header row: title, current watched path, `[Scan now]` button
- Per-app row: status badge (`●`/`○`/`?`), app name, version, port (or `—`), `[Start]`/`[Stop]` button
- Status badge colors use `aura_generated` token constants: Running=COLOR_SUCCESS (green), Stopped=COLOR_SURFACE_MUTED (grey), Unknown=COLOR_WARNING (amber)
- `[Start]` shown when Stopped/Unknown; `[Stop]` shown when Running; buttons disabled while in-flight; in-flight shows "Starting…"/"Stopping…"
- Status bar: "Auto-refresh: Ns" and "Last scan: Ns ago"
- All detection and launch calls dispatched to background tokio runtime; GUI thread never blocks
- State flows via `tokio::sync::watch` receiver + `Arc<Mutex<>>` shared state
- Uses Obsidian widget primitives; no hand-rolled colors or spacing values
- `cargo build --release` passes

**Branch:** `warden/v0.1-gui`
**Design doc:** FIRST-RELEASE-PROMPT.md §GUI layout; `docs/design/ux/design-language.md` (must be complete first)

---

### §2.7 Entry point + CLI — `src/main.rs`

Binary entry point: parse CLI args, init the tokio runtime, start the scanner, and launch the Obsidian AppRunner.

**Acceptance criteria:**
- Parses `--apps-dir <path>` (default: `~/Projects/deployed-apps/`) and `--refresh <secs>` (default: 5) using `std::env::args()` directly (no clap)
- Expands `~` using the `dirs` crate
- Initializes the tokio runtime and spawns the scanner task
- Constructs the `App` with all wired channels and state, passes it to Obsidian's `AppRunner`
- Binary runs end-to-end: `./target/release/warden --help` prints usage; `./target/release/warden` opens the window
- `cargo build --release` and `cargo clippy` pass clean

**Branch:** `warden/v0.1-main`
**Design doc:** FIRST-RELEASE-PROMPT.md §CLI interface

---

### §2.8 Architecture design doc — `docs/design/app-design.md`

An ADR capturing the module decomposition, async data-flow, and key dependency decisions for v0.1.

**Acceptance criteria:**
- Written after core modules (§2.3–§2.6) are implemented so it reflects real decisions, not spec
- Covers: module roles, async data-flow diagram (scanner → watch channel → app.rs), tokio runtime topology, Obsidian widget integration rationale, out-of-scope decisions and why
- Created via `design-doc-scaffold`; linked from `docs/design/README.md` (create README if absent)
- `cargo test` and `cargo build --release` unaffected (doc-only change)

**Branch:** `warden/v0.1-arch-doc`
**Design doc:** FIRST-RELEASE-PROMPT.md (source spec); `docs/design/app-design.md` (the output IS the design doc)

---

## 3. Parallel Implementation Strategy

### Phase structure

Max parallel worktrees: **2** (per `grimoire-config.json`).

| Phase | Items | Branches | File overlap |
|---|---|---|---|
| 1 | §2.1 Setup + §2.2 Design language | `warden/v0.1-setup`, `warden/v0.1-design-language` | None — Cargo.toml/models.rs vs. docs/design/ux/ |
| 2 | §2.3 Scanner + §2.4 Detector | `warden/v0.1-scanner`, `warden/v0.1-detector` | None — scanner.rs vs. detector.rs |
| 3 | §2.5 Launcher + §2.8 Arch doc | `warden/v0.1-launcher`, `warden/v0.1-arch-doc` | None — launcher.rs vs. docs/ |
| 4 | §2.6 GUI | `warden/v0.1-gui` | Reads all module interfaces (no writes to them) |
| 5 | §2.7 Entry point | `warden/v0.1-main` | src/main.rs only; requires all modules to exist |

### Merge order and conflict map

Phase 1 merges first (unblocks all Rust compilation); Phase 2 merges after Phase 1; Phases 3–5 in sequence. No cross-phase file conflicts exist — each branch owns a distinct file.

### Dependencies

- §2.3, §2.4, §2.5, §2.6, §2.7 all depend on §2.1 (`models.rs` must exist)
- §2.6 depends on §2.2 (must know `aura_generated` token names before writing widget code)
- §2.6 depends on §2.3, §2.4, §2.5 (must know module interfaces)
- §2.7 depends on §2.6 (wires the app struct)
- §2.8 should be written after §2.3–§2.5 so it reflects real decisions

---

## 4. Out of Scope for v0.1

| Item | Target |
|---|---|
| Multi-directory watching | v0.2+ |
| Log streaming / tail window | v0.2+ |
| macOS NSUserNotification support | v0.2+ |
| Persistent settings (TOML config file) | v0.2+ |
| Ensign HTTP health polling | v0.2+ |
| Version update checks | v0.2+ |
| History / uptime tracking | v0.2+ |
| `ux-demo/` widget gallery (`ux-demo-build`) | v0.2+ (opt-in, after design language is stable) |

---

## 5. Status Ledger

### Phase 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.1 |
|---|---|---|---|---|
| `warden/v0.1-setup` (§2.1) | ☐ | ☐ | ☐ | ☐ |
| `warden/v0.1-design-language` (§2.2) | ☐ | ☐ | ☐ | ☐ |

### Phase 2

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.1 |
|---|---|---|---|---|
| `warden/v0.1-scanner` (§2.3) | ☐ | ☐ | ☐ | ☐ |
| `warden/v0.1-detector` (§2.4) | ☐ | ☐ | ☐ | ☐ |

### Phase 3

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.1 |
|---|---|---|---|---|
| `warden/v0.1-launcher` (§2.5) | ☐ | ☐ | ☐ | ☐ |
| `warden/v0.1-arch-doc` (§2.8) | ☐ | ☐ | ☐ | ☐ |

### Phase 4

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.1 |
|---|---|---|---|---|
| `warden/v0.1-gui` (§2.6) | ☐ | ☐ | ☐ | ☐ |

### Phase 5

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.1 |
|---|---|---|---|---|
| `warden/v0.1-main` (§2.7) | ☐ | ☐ | ☐ | ☐ |

### Follow-ups discovered during implementation

_(empty at start — populated by release-phase-merge as branches land)_
