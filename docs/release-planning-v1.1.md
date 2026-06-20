# Release Planning — v1.1

> status: agreed
> Companion to `docs/design/app-design.md` and `docs/version-history.md`.
> Captures scope, item ledger, and dispatch lanes for v1.1 "Hardened Foundation".
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v1.1.0` |
| **Previous** | v1.0.1 (Stability fixes) |
| **Theme** | "Hardened Foundation" — close four confirmed correctness/robustness gaps so Warden is a trustworthy base for later feature work. No new features, no new dependencies, no design docs. |

Selected from a 4-dimension planning survey (functionality / UX / robustness /
carryovers); the robustness theme won on confirmed-bug value and smallest budget.

---

## 2. Items

### 2.1 Graceful shutdown — kill managed children on exit (Issue #38)

**Bug:** `Launcher` tracks spawned children in `children: HashMap<PathBuf, Child>`
but never terminates them on exit. `main.rs` returns after `AppRunner::run`, the
tokio runtime is dropped, and Warden-launched apps are orphaned.

**Fix:** Add `Launcher::shutdown_all()` that SIGTERMs every tracked child (brief
grace, then SIGKILL) and clears the map; call it from `main.rs` after
`AppRunner::run` returns via `runtime.block_on(...)`. Also set
`.kill_on_drop(true)` on the spawned `Command`s as a backstop.

**Acceptance criteria:**
- `Launcher::shutdown_all(&mut self)` async method terminates all tracked children (SIGTERM → short wait → SIGKILL) and empties `children`
- `main.rs` invokes it on normal window-close exit (after `AppRunner::run`)
- Spawned commands set `kill_on_drop(true)` as a defense-in-depth backstop
- Test: a child started via the launcher is no longer alive after `shutdown_all`
- `cargo test`, `cargo clippy`, `cargo build --release` clean

**Branch:** `warden/v1.1-graceful-shutdown` · **Files:** `src/launcher.rs`, `src/main.rs`

### 2.2 Atomic history.json write (Issue #39)

**Bug:** `HistoryStore::save` calls `std::fs::write(&path, json)` directly. A
crash or power loss mid-write leaves a truncated/corrupt `history.json`, which
then fails to parse on next launch (silently resetting all history).

**Fix:** Write to a sibling temp file (`history.json.tmp`) then atomically
`std::fs::rename` it over the target. Rename within the same directory is atomic
on macOS/Linux.

**Acceptance criteria:**
- `save` writes to a temp file then renames over `history.json` (no direct write to the live path)
- Temp file is in the same directory (same filesystem) so rename is atomic
- Failure to write the temp file leaves any existing `history.json` intact; warn, don't panic
- Test: after `save`, the file parses back to an equivalent store; a pre-existing valid file is never left truncated
- `cargo test`, `cargo clippy` clean

**Branch:** `warden/v1.1-atomic-history` · **Files:** `src/history.rs`

### 2.3 Detector failure-mode handling (Issue #40)

**Bug:** `detector::lsof_pid` and `pgrep` return `None` on any non-zero exit,
collapsing three distinct cases — genuine "no listener", permission-denied, and
zombie/defunct match — into a single `Stopped`/no-match result. SIP/permission-
restricted apps are silently shown as Stopped; defunct processes can read as
Running.

**Fix:** Distinguish lsof/pgrep *failure* (non-zero exit with stderr, or command
error) from a clean *no-match* (exit code indicating no results). On an
inconclusive failure, fall through the detection chain and return `Unknown`
rather than `Stopped`. Filter out zombie/defunct PIDs (skip processes in state
`Z`) when interpreting `pgrep` matches.

**Acceptance criteria:**
- A lsof/pgrep *error* (not a clean no-match) does not force `Stopped`; inconclusive detection yields `Unknown`
- Zombie/defunct matches are not reported as `Running`
- Helper(s) are unit-testable in isolation (parse/classify logic separated from process invocation where practical)
- Existing detector tests still pass; new tests cover the failure-vs-no-match distinction and zombie filtering
- `cargo test`, `cargo clippy` clean

**Branch:** `warden/v1.1-detector-robustness` · **Files:** `src/detector.rs`

### 2.4 Config validation at startup (Issue #41)

**Bug:** `Config` performs no validation. `refresh_secs = 0` makes the scanner
`sleep(0)` and busy-loop; `log_tail_lines = 0` constructs `LogCapture::new(0)`,
whose ring buffer never evicts and grows **unbounded** (confirmed).

**Fix:** Add `Config::sanitized()` (or clamp inside `load_from`) that floors
`refresh_secs` and `log_tail_lines` to `>= 1`, logging a warning when a value is
corrected. Add a defensive `capacity.max(1)` guard in `LogCapture::new`.
`version_check_interval_secs = 0` stays valid (means "disabled").

**Acceptance criteria:**
- Loaded config never yields `refresh_secs == 0` or `log_tail_lines == 0`; corrected values log a warning
- `version_check_interval_secs == 0` is preserved (disable semantics)
- `LogCapture::new` clamps capacity to `>= 1` defensively
- Tests: out-of-range values are clamped; valid values pass through unchanged; `LogCapture::new(0)` is bounded
- `cargo test`, `cargo clippy` clean

**Branch:** `warden/v1.1-config-validation` · **Files:** `src/config.rs`, `src/log_capture.rs`

---

## 3. Implementation Strategy

**Fully parallel** — the four items have **disjoint file sets**, so all four
dispatch at once as isolated-worktree agents off `version/1.1`; the integration
master merges each returned branch in any order.

| Lane | Issue | Branch | Files |
|---|---|---|---|
| A | #38 | `warden/v1.1-graceful-shutdown` | `launcher.rs`, `main.rs` |
| B | #39 | `warden/v1.1-atomic-history` | `history.rs` |
| C | #40 | `warden/v1.1-detector-robustness` | `detector.rs` |
| D | #41 | `warden/v1.1-config-validation` | `config.rs`, `log_capture.rs` |

---

## 4. Out of Scope for v1.1

- Net-new features (Fleet Control, Ensign health polling, resource metrics) — candidate v1.2 themes from the same survey
- "What's new" badge, markdown changelog, keyboard shortcuts (Polish theme — deferred)
- Re-adopting still-running children after a Warden restart (larger lifecycle change)

---

## 5. Status Ledger

| Branch | Implemented | Reviewed | Merged into version/1.1 |
|---|---|---|---|
| `warden/v1.1-graceful-shutdown` (#38) | ☐ | ☐ | ☐ |
| `warden/v1.1-atomic-history` (#39) | ☐ | ☐ | ☐ |
| `warden/v1.1-detector-robustness` (#40) | ☐ | ☐ | ☐ |
| `warden/v1.1-config-validation` (#41) | ☐ | ☐ | ☐ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 1.0.1 → 1.1.0) | ☐ |
| `version-history.md` entry | ☐ |
| `roadmap.md` v1.1 section | ☐ |
| project-release (merge + tag + push) | ☐ |
| Issues #38 #39 #40 #41 closed | ☐ |

### Follow-ups discovered during implementation

_(empty at start)_
