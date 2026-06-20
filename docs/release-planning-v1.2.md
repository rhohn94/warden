# Release Planning — v1.2

> status: agreed
> Companion to `docs/design/app-design.md` §4a and `docs/version-history.md`.
> Captures scope, item ledger, and dispatch shape for v1.2 "Fleet Control".
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v1.2.0` |
| **Previous** | v1.1.0 (Hardened Foundation) |
| **Theme** | "Fleet Control" — one-click fleet-wide control and at-a-glance status for operators running many apps. Net-new operator value on the now-hardened foundation. No new dependencies, no new modules. |

Selected as the strongest no-design-doc-gate candidate from the v1.1 planning
survey (chosen ahead of Ensign Health and Polish for v1.2).

---

## 2. Items

### 2.1 Bulk Start / Stop / Restart (Issue #42)

Header-toolbar `Start all` / `Stop all` / `Restart all` (Apps view only) that
fold over the currently visible (filtered) entries and dispatch the existing
per-app paths, skipping apps already in the target state or in-flight.

**AC:** bulk buttons present in Apps view; each acts only on visible entries;
already-correct/in-flight apps skipped; no new launcher API; bulk-dispatch
selection logic covered by a unit test; `cargo test` + `clippy` clean.

### 2.2 Fleet Health Summary Bar (Issue #43)

A compact one-line summary under the header hairline (e.g.
`6 running · 2 stopped · 1 crashed`), computed as a fold over the `statuses`
snapshot. Purely additive.

**AC:** summary reflects live counts of Running/Stopped/Crashed/Unknown; the
count fold is a pure, unit-tested helper; hidden or sensible in the Logs view;
`cargo test` + `clippy` clean.

### 2.3 Sort & Group Controls (Issue #44)

A `ButtonGroup` selecting the app-list sort key (Name / Status / Port). A pure
`sort_entries(entries, key, statuses)` helper orders the filtered list before
`draw_app_list`. The chosen key persists in `Config.sort_order`, saved on change.

**AC:** sort control switches list order live; `sort_entries` is pure and
unit-tested for each key; choice persists across restarts via config; default
remains the scanner's `(name, dir)` order; `cargo test` + `clippy` clean.

### 2.4 Auto-Start-on-Launch (Issue #45)

`Config.auto_start: Vec<String>` of app names to start automatically. A per-app
toggle in the details panel adds/removes the name and saves. On the first
populated scan (guarded by a one-shot `did_autostart` flag), each flagged app
not already running is dispatched via `dispatch_start`.

**AC:** details-panel toggle reflects and updates membership and persists; on
first populated scan, flagged not-running apps are started exactly once (never
re-triggered on later scans); `version_check`/other config untouched;
`cargo test` + `clippy` clean.

---

## 3. Implementation Strategy

**Single cohesive lane.** Unlike v1.1's module-disjoint robustness work, all
four items are concentrated in `src/app.rs` (header toolbar, `draw_app_list`,
`draw_details`, `render`) plus two new `src/config.rs` fields. Splitting into
parallel agents would collide on `app.rs` and risk an inconsistent toolbar, so
v1.2 ships as **one isolated-worktree agent** implementing all four features
coherently, followed by an adversarial verify pass before release.

| Lane | Issues | Branch | Files |
|---|---|---|---|
| Fleet Control | #42–#45 | `warden/v1.2-fleet-control` | `src/app.rs`, `src/config.rs` |

---

## 4. Out of Scope for v1.2

- Ensign HTTP health polling + per-app resource metrics (candidate v1.3 theme — needs a design doc)
- "What's new" badge, markdown changelog, keyboard shortcuts (Polish theme — deferred)
- Dependency/order-aware startup sequencing (beyond a flat auto-start set)
- Persisting per-app start order or grouping beyond the sort key

---

## 5. Status Ledger

| Branch | Implemented | Reviewed | Merged into version/1.2 |
|---|---|---|---|
| `warden/v1.2-fleet-control` (#42–#45) | ☑ | ☑ | ☑ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 1.1.0 → 1.2.0) | ☑ |
| `version-history.md` entry | ☑ |
| `roadmap.md` v1.2 section | ☑ |
| project-release (merge + tag + push) | ☐ |
| Issues #42 #43 #44 #45 closed | ☐ |

### Follow-ups discovered during implementation

- Reviewed via a 4-agent adversarial verify-workflow (sonnet). #43 passed clean.
  Three findings folded in before release:
  - **#45 (high):** auto-start used a Running-only denylist, so an `Unknown`
    status (common when a first-scan detector times out) was treated as
    start-eligible — risking a double-start of an already-running app. Fixed:
    extracted a pure `autostart_targets` allowlisting only `Stopped`/`Crashed`,
    and replaced the one-shot `did_autostart` bool with a per-app `autostarted`
    set so a flagged app still auto-starts once its status resolves on a later
    scan.
  - **#42 (low):** auto-start reused the single `pending_bulk` slot and could
    clobber a user's bulk-button click on the first populated frame. Fixed by a
    separate `pending_autostart` application path.
  - **#44 (medium):** the sort `ButtonGroup` conflated ScannerOrder and Name on
    index 0, so the default order (case-sensitive scanner) was unreachable once
    the user navigated away. Fixed with a 4-button [Default, Name, Status, Port]
    bijective mapping + round-trip tests.
- Deferred (out of scope): dependency/order-aware startup sequencing beyond the
  flat auto-start set.
