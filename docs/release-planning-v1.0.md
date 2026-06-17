# Release Planning — v1.0

> status: agreed
> Companion to `docs/design/app-design.md` and `docs/version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v1.0.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v1.0.0` |
| **Previous** | v0.9.0 (Aura Fidelity) |
| **Theme** | "Changelog Visibility" — surfaces Warden's own release history inside the app. The current version is shown as a clickable label in the header; clicking it opens a scrollable changelog window showing release notes for every shipped version, latest first. |

---

## 2. Major Features

### 2.1 Changelog Data Layer + Version Header Label (Issue #33)

**Description:** Embed `docs/version-history.md` at compile time, parse it into structured per-version entries, and display the current version as a clickable label in the header. Clicking the label opens the changelog viewer.

**Acceptance criteria:**
- `const VERSION: &str = env!("CARGO_PKG_VERSION")` at compile time
- `const CHANGELOG_MD: &str = include_str!("../docs/version-history.md")` embeds the file
- `parse_changelog(md: &str) -> Vec<ChangelogEntry>` splits on `## v` headings; `ChangelogEntry { version: String, bullets: Vec<String> }`
- `App` gains `changelog_entries: Vec<ChangelogEntry>` and `changelog_open: bool`
- Version label (`v0.9.0`, TEXT_MUTED, TEXT_SM, sense click) shown after "Warden" title; click sets `changelog_open = true`
- New module `src/changelog.rs`; `mod changelog;` in `src/main.rs`
- Unit test: `parse_changelog(CHANGELOG_MD)` returns ≥ 9 entries; first entry version starts with `"v0.9"`
- `cargo test` passes; `cargo clippy` clean

**Branch:** `warden/v1.0-changelog-data`

**Files:** `src/changelog.rs` (new), `src/main.rs`, `src/app.rs`

---

### 2.2 Changelog Viewer Window (Issue #34)

**Description:** A scrollable Aura-styled egui window that opens when `self.changelog_open` is true. Shows all versions in card surfaces, latest first, with a close button.

**Acceptance criteria:**
- `egui::Window::new("Changelog")` rendered when `self.changelog_open` is true; default size ~480 × 520 pt
- Header row: title text + `TactileButton::new("Close").ghost()` sets `changelog_open = false`
- `ScrollArea::vertical()` renders each `ChangelogEntry` as a `card_show` block:
  - Version heading via `apply_type_tokens(RichText::new(&entry.version).strong(), golden::TEXT_LG)`
  - Each bullet: `RichText::new(format!("• {}", bullet)).color(golden::TEXT_MUTED).size(golden::TEXT_SM)`
  - `hairline` between cards (not after the last)
- Standard egui window × closes `changelog_open`; Escape does not force-close
- `cargo test` passes; `cargo clippy` clean

**Branch:** `warden/v1.0-changelog-viewer`

**Files:** `src/app.rs` — `draw_changelog` method + call site in `draw_ui`

---

## 3. Implementation Strategy

**Sequential** — #34 depends on the `ChangelogEntry` type and `App` fields introduced by #33.

| Phase | Issue | Branch |
|---|---|---|
| 1 | #33 — data layer + header label | `warden/v1.0-changelog-data` |
| 2 | #34 — viewer window | `warden/v1.0-changelog-viewer` |

---

## 4. Out of Scope for v1.0

- Fetching release notes from GitHub API (offline-first; bundled at compile time)
- "What's new" badge / first-launch prompt
- Markdown rendering (plain text bullets only)
- Syntax highlighting of version numbers or dates

---

## 5. Status Ledger

### Phase 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/1.0 |
|---|---|---|---|---|
| `warden/v1.0-changelog-data` (#33) | ☑ | ☑ | ☑ | ☑ |

### Phase 2

| Branch | Design doc | Implemented | Reviewed | Merged into version/1.0 |
|---|---|---|---|---|
| `warden/v1.0-changelog-viewer` (#34) | ☑ | ☑ | ☑ | ☑ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 0.9.0 → 1.0.0) | ☑ |
| `version-history.md` entry | ☑ |
| project-release (merge+tag+push) | ☐ |
| Issues #33 #34 closed | ☐ |

### Follow-ups discovered during implementation

_(empty at start)_
