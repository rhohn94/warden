# Release Planning — v1.0.1

> status: agreed
> Companion to `docs/version-history.md`. Patch release: three operator-facing
> bug fixes, no new features. Archive into `version-history.md` when shipped.

---

## 1. Target

| | |
|---|---|
| **Version** | `v1.0.1` |
| **Previous** | v1.0.0 (Changelog Visibility) |
| **Theme** | "Stability fixes" — resolve three reported defects in process control, list ordering, and version display. |

---

## 2. Fixes

### 2.1 Stop-hang (Issue #35)

Clicking **Stop** froze the GUI. The async `dispatch_*` tasks (and the
`render()` scanner-transition path) held the `AppState` mutex across the
blocking `history.save()` disk write, starving the render thread. Fix: release
the state lock before any disk I/O — clone the history `Arc`, drop the state
guard, then lock history separately.

**Files:** `src/app.rs`

### 2.2 Unstable list order (Issue #36)

The running-apps list reshuffled every scan cycle because
`run_detectors_concurrent` returned `JoinSet` results in completion order. Fix:
sort by `(name, dir)` after collection — a deterministic total order, stable
even across watched roots with duplicate names.

**Files:** `src/scanner.rs`

### 2.3 Missing version display (Issue #37)

Versions showed only for apps whose `grimoire-build-info.json` sat at the app
root. Apps using the versioned `current/` symlink layout were never read. Fix:
`parse_from_build_info` falls back to `<app>/current/grimoire-build-info.json`;
parse precedence documented. Regression test added.

**Files:** `src/scanner.rs`

---

## 3. Out of Scope

- True chronological log merge (still per-app stable ordering)
- Persisting list order across restarts (deterministic sort makes it moot)

---

## 4. Status Ledger

| Fix | Issue | Implemented | Reviewed | Verified |
|---|---|---|---|---|
| Stop-hang | #35 | ☑ | ☑ | ☑ |
| List order | #36 | ☑ | ☑ | ☑ |
| Version display | #37 | ☑ | ☑ | ☑ |

> Reviewed via adversarial verify-workflow (3 sonnet reviewers). The hang review
> caught a residual: `render()` still wrote history under the state lock — folded
> into the fix before merge. The order review upgraded the sort key to `(name, dir)`.

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 1.0.0 → 1.0.1) | ☑ |
| `version-history.md` entry | ☑ |
| `roadmap.md` v1.0.1 section | ☑ |
| project-release (merge + tag + push) | ☐ |
| Issues #35 #36 #37 closed | ☐ |
