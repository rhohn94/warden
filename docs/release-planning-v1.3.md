# Release Planning — v1.3

> status: agreed
> Companion to `docs/design/app-design.md` and `docs/version-history.md`.
> Captures scope, item ledger, and dispatch lanes for v1.3 "Polish & Carryovers".
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v1.3.0` |
| **Previous** | v1.2.0 (Fleet Control) |
| **Theme** | "Polish & Carryovers" — clear the deferred v1.0/v1.1 debt and round off the operator UX. One real latent-OOM fix plus four quality-of-life items. No new dependencies. |

Chosen over "Ensign Health" for v1.3 because the Ensign HTTP health-API shape
is undocumented in the ecosystem (deferred 8× across v0.2–v0.9 for exactly this
reason); health polling waits for a real spec.

---

## 2. Items

### 2.1 Bounded log channels (Issue #46)

**Bug:** `log_capture::log_channel` uses `mpsc::unbounded_channel`. A child app
that logs faster than the render thread drains (once per frame) grows the
channel without limit — a latent OOM for a chatty app. (The `LogCapture` ring
buffer is already bounded; the *channel* feeding it is not.)

**Fix:** Use a bounded `mpsc::channel(N)` and have the reader forward with
`try_send`, dropping the line when full (a log *tail* tolerates loss; back-
pressure-blocking the reader could stall the child's stdout). Size N off the
existing `log_tail_lines` (or a sensible cap).

**AC:** channel is bounded; reader never blocks the child indefinitely; lines
dropped on overflow rather than growing unboundedly; existing log viewer/tail
behaviour preserved; `cargo test` + `clippy` clean.
**Files:** `src/log_capture.rs`, `src/launcher.rs`, `src/app.rs` (the
`dispatch_start`/`dispatch_restart` bridge tasks only).

### 2.2 "What's new" badge on upgrade (Issue #47)

After a version bump, surface that the app was updated. A new
`Config.last_seen_version` is compared to `VERSION` at startup; if different (and
not first-run), show a small "Updated to vX.Y — what's new?" affordance in the
header that opens the changelog; then persist the new version.

**AC:** badge shows only when `last_seen_version` differs from current `VERSION`;
clicking opens the changelog; `last_seen_version` is persisted so the badge does
not reappear; first run (no stored version) does not nag; `cargo test` + `clippy`.
**Files:** `src/app.rs`, `src/config.rs`.

### 2.3 Markdown rendering in changelog (Issue #48)

Changelog bullets are plain text. Render a minimal markdown subset — `**bold**`
and `` `code` `` (inline) — in the changelog window. A small pure parser maps a
bullet string to styled egui `RichText` runs; no new crate.

**AC:** `**bold**` and `` `code` `` render styled in the changelog window; plain
text unaffected; the parser is pure and unit-tested (incl. unmatched markers
left literal); `cargo test` + `clippy` clean.
**Files:** `src/changelog.rs`, `src/app.rs` (`draw_changelog`).

### 2.4 Empty & error states (Issue #49)

When the app list is empty (no apps discovered, or all filtered out) or a watched
directory cannot be read, show a friendly message instead of a blank panel.

**AC:** distinct messages for "no apps discovered", "no apps match the filter",
and "directory unreadable"; the log viewer's empty state preserved; no panic on
an empty/missing dir; `cargo test` + `clippy` clean.
**Files:** `src/app.rs`.

### 2.5 Keyboard shortcut layer (Issue #50)

Common actions get keys: `/` focuses the filter, `Esc` clears it (already
present) / deselects, `r` triggers Scan now, and `j`/`k` move the row selection.
Shortcuts are suppressed while the filter field has focus (except Esc).

**AC:** documented shortcuts work; no shortcut fires while typing in the filter
(except Esc); the mapping is a pure, unit-tested key→action helper; `cargo test`
+ `clippy` clean.
**Files:** `src/app.rs`.

---

## 3. Implementation Strategy

**Two parallel lanes with disjoint `app.rs` regions.** #46 is a robustness change
spanning `log_capture.rs`/`launcher.rs` plus only the `dispatch_start`/
`dispatch_restart` bridge tasks in `app.rs`. The four UI items (#47–#50) are a
cohesive bundle touching the `App` struct, `draw_ui`, `draw_changelog`, and
`draw_app_list` — disjoint from the dispatch methods — plus `config.rs` and
`changelog.rs`. They dispatch in parallel; the master resolves the (unlikely)
`app.rs` overlap at merge.

| Lane | Issues | Branch | Files |
|---|---|---|---|
| A — robustness | #46 | `warden/v1.3-bounded-log-channels` | `log_capture.rs`, `launcher.rs`, `app.rs` (dispatch bridges) |
| B — UI polish | #47–#50 | `warden/v1.3-ui-polish` | `app.rs` (struct/draw_*), `config.rs`, `changelog.rs` |

---

## 4. Out of Scope for v1.3

- Ensign HTTP health polling + resource metrics — blocked on an undocumented Ensign spec; revisit when specified
- Full CommonMark rendering (only inline bold/code in the changelog)
- Configurable/rebindable keyboard shortcuts (fixed set this release)

---

## 5. Status Ledger

| Branch | Implemented | Reviewed | Merged into version/1.3 |
|---|---|---|---|
| `warden/v1.3-bounded-log-channels` (#46) | ☑ | ☑ | ☑ |
| `warden/v1.3-ui-polish` (#47–#50) | ☑ | ☑ | ☑ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 1.2.0 → 1.3.0) | ☑ |
| `version-history.md` entry | ☑ |
| `roadmap.md` v1.3 section | ☑ |
| project-release (merge + tag + push) | ☐ |
| Issues #46–#50 closed | ☐ |

### Follow-ups discovered during implementation

- Reviewed via two read-only reviewer agents (sonnet), one per lane. #46 clean.
  One blocking finding on #47 — **folded in before release**: opening the
  changelog via the header version label did not dismiss/persist the what's-new
  badge (only the badge itself did), so it would reappear next launch. Fixed by a
  shared `acknowledge_whats_new` helper called from both paths.
- Deferred (non-blocking, latent): the keyboard-shortcut suppression checks only
  the app-list filter's focus; if a future text field is added elsewhere, widen
  the focus check so `/`/`r` don't fire while typing in it. No other text field
  exists today.
