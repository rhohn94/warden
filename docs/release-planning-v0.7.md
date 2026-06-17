# Release Planning — v0.7

> status: agreed
> Companion to `docs/design/app-design.md` and `docs/version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v0.7.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v0.7.0` |
| **Previous** | v0.6.0 (Multi-source Monitoring) |
| **Theme** | "Performance + Stability" — eliminates UI freeze under load by throttling the scan loop and making per-app detector calls concurrent; adds performance telemetry (`perf.log`) for diagnostics. |

---

## 2. Major Features

### 2.1 Scan Throttling (Issue #24 — part 1)

**Description:** The scanner loop can stack force-scan triggers into an already-running scan, and all per-app detector calls execute serially — one slow `lsof`/`pgrep` stalls the rest. This item enforces a drop-guard on the force-scan channel and switches per-app detector calls to run concurrently via `tokio::join_all`.

**Acceptance criteria (from issue #24):**
- A scan in-flight when a force-scan arrives is **dropped** (not queued); a `tracing::debug!` line notes the drop
- Per-app detector calls run concurrently (`tokio::join_all` or `FuturesUnordered`) so one slow app does not serialise the rest
- Per-app detector call times out at 2 s; app is marked `Unknown` and the cycle continues
- New unit test verifies the in-flight drop logic on the force-scan channel
- `cargo test` and `cargo clippy` pass

**Branch:** `warden/v0.7-scan-throttle`

**Files:** `src/scanner.rs` (in-flight guard, join_all), `src/detector.rs` (timeout wrapper), `src/models.rs` (minor, if needed)

---

### 2.2 Performance Telemetry (Issue #24 — part 2)

**Description:** Instrument hot paths with timing metrics written to `~/.config/warden/perf.log` (append-only, one line per event). Add `perf.frame_warn_ms` to `config.toml` (default 50) to configure the egui frame-time warning threshold.

**Acceptance criteria (from issue #24):**
- `~/.config/warden/perf.log` written on each scan cycle with: cycle duration, drop count, slowest-app name + ms
- Frame-time warn threshold is configurable via `config.toml` (`perf.frame_warn_ms`, default 50)
- Frame-time warn logged via `tracing::warn!` when egui frame duration exceeds threshold
- Log format: `<ISO-8601> <event> <duration_ms> [<app>]` — one line per event, machine-readable
- `perf.log` is gitignored (add to `.gitignore` if not already present)
- `cargo test` and `cargo clippy` pass

**Branch:** `warden/v0.7-perf-telemetry`

**Files:** `src/config.rs` (`perf` section + `frame_warn_ms`), `src/app.rs` (frame-time warn), `src/scanner.rs` (perf.log writes), `src/models.rs` (minor, if needed)

---

## 3. Parallel Implementation Strategy

**Phase 1 (parallel — batch 1):** Items 2.1 and 2.2 can run concurrently.

- 2.1 (scan throttle) touches: `src/scanner.rs` (primary), `src/detector.rs`, possibly `src/models.rs`
- 2.2 (perf telemetry) touches: `src/config.rs` (primary), `src/app.rs`, `src/scanner.rs` (perf.log writes)

- `src/scanner.rs` overlap: 2.1 restructures the scan loop / adds join_all; 2.2 adds timing writes. These are different functional areas — 2.1 changes control flow, 2.2 appends instrumentation calls. Merge 2.1 first, then 2.2 to avoid conflict.
- `src/models.rs` overlap: both may touch minimally; expected additive.

**Merge order for Phase 1:** merge 2.1 first (scanner control-flow changes), then 2.2 (config + instrumentation additions).

**Conflict map summary:**

| Branch | Files touched | Conflict risk |
|---|---|---|
| `warden/v0.7-scan-throttle` (2.1) | `src/scanner.rs`, `src/detector.rs`, `src/models.rs` | Low |
| `warden/v0.7-perf-telemetry` (2.2) | `src/config.rs`, `src/app.rs`, `src/scanner.rs` | Low–Medium (scanner overlap with 2.1; merge 2.1 first) |

---

## 4. Out of Scope for v0.7

- **Ensign HTTP health polling** — deferred; Ensign API shape not documented in this repo
- **GUI settings panel** — backlog
- **Log search / grep** — backlog

---

## 5. Status Ledger

### Phase 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.7 |
|---|---|---|---|---|
| `warden/v0.7-scan-throttle` (#24 pt1) | ☑ | ☑ | ☑ | ☑ |
| `warden/v0.7-perf-telemetry` (#24 pt2) | ☑ | ☑ | ☑ | ☑ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 0.6.0 → 0.7.0) | ☑ |
| `version-history.md` entry | ☑ |
| project-release (merge+tag+push) | ☐ |
| Issues #24 closed | ☐ |

### Follow-ups discovered during implementation

_(empty at start)_
