# Release Planning — v0.9

> status: agreed
> Companion to `docs/design/app-design.md` and `docs/version-history.md`. Captures
> the scope, pass structure, and implementation ledger for v0.9.
> Archive into `version-history.md` when the release ships.

---

## 1. Target

| | |
|---|---|
| **Version** | `v0.9.0` |
| **Previous** | v0.8.0 (Operator UX) |
| **Theme** | "Aura Fidelity" — brings Warden into full alignment with the Obsidian/Aura design system. Every button becomes a `TactileButton`, backgrounds use the correct Aura surface hierarchy, typography is properly tokenised, app rows become card surfaces, and structural nav elements (tabs, filter chips) use the correct Obsidian widget primitives. |

---

## 2. Major Features

### 2.1 TactileButton Migration (Issue #28)

**Description:** Every action button in the app (Start, Stop, Restart, Open, Scan now, in-flight disabled labels) uses raw `egui::Button`. These must be replaced with `TactileButton` using the correct Aura variant so the tactile press/lift motion, correct fill, and hover states all apply.

**Acceptance criteria:**
- `Start` → `TactileButton::new("Start").primary()`
- `Stop` → `TactileButton::new("Stop").ghost()`
- `Restart` → `TactileButton::new("Restart").ghost()`
- `Open` → `TactileButton::new("Open").ghost()`
- `Scan now` → `TactileButton::new("Scan now").secondary()`
- In-flight disabled buttons (`Restarting…`, `Stopping…`, `Starting…`) → `.ghost()` passed to `ui.add_enabled(false, ...)`
- `min_size` / `corner_radius` manual overrides removed (TactileButton applies Aura sizing)
- `use obsidian::widgets::TactileButton` imported
- `cargo test` passes; `cargo clippy` clean

**Branch:** `warden/v0.9-tactile-button`

**Files:** `src/app.rs` — `draw_app_list`, `draw_details`, `draw_ui` button regions

---

### 2.2 Aura Visual Foundation (Issue #29)

**Description:** Window and panel backgrounds fall back to egui defaults. Apply the correct Aura surface colors at startup and paint the aurora wallpaper behind the central panel. Replace the `◀` selection indicator with a proper `aura::SURFACE_1` row tint.

**Acceptance criteria:**
- `apply_warden_theme(ctx)` called in the egui context init block, setting `visuals.window_fill = aura::BG`, `visuals.panel_fill = aura::BG_2`, `visuals.override_text_color = Some(aura::TEXT)`
- `obsidian::theme::paint_aura_wallpaper_opaque(ui, ...)` called at the top of `draw_ui` to paint the Aura background
- Selected app row shows `aura::SURFACE_1` fill highlight (via `ui.painter().rect_filled(...)`) instead of the `◀` text glyph
- The `◀` text indicator removed from the row render
- `cargo test` passes; `cargo clippy` clean

**Branch:** `warden/v0.9-aura-foundation`

**Files:** `src/app.rs` — resumed/startup block, `draw_ui`, `draw_app_list` row selection highlight

---

### 2.3 Aura Typography & Dividers (Issue #30)

**Description:** All text uses unstyled egui defaults. Apply Aura text size tokens, `apply_type_tokens()` tracking, muted/subtle colors for secondary text, and replace raw `ui.separator()` with `hairline()` and `LabeledDivider` for section headings.

**Acceptance criteria:**
- `ui.heading(...)` calls replaced with `egui::RichText::new(...).size(golden::TEXT_XL)` (or appropriate size) passed through `obsidian::theme::apply_type_tokens()`
- Secondary labels (directory path, version, port, metadata grid keys) use `egui::RichText::new(...).color(aura::TEXT_MUTED)`
- Every `ui.separator()` replaced with `obsidian::theme::hairline(ui)`
- "Log output" and "History" section dividers in `draw_details` become `obsidian::widgets::LabeledDivider::new("Log output").ui(ui)` / `LabeledDivider::new("History").ui(ui)`
- `cargo test` passes; `cargo clippy` clean

**Branch:** `warden/v0.9-typography`

**Files:** `src/app.rs` — `draw_details` (section dividers/headings), `draw_app_list` (row labels), `draw_ui` (main heading)

---

### 2.4 Card & Elevated Panel Framing (Issue #31)

**Description:** App rows and the details side panel content are rendered directly on the panel background. Use `card_show()` to give each app row its own rounded Aura surface and `elevated_panel_show()` for the details pane body.

**Acceptance criteria:**
- Each app row in `draw_app_list` is wrapped in `obsidian::theme::card_show(ui, |ui| { ... })` — the card provides rounded corners, Aura fill, and shadow
- Existing `ui.add_space(golden::SPACE[3])` inter-row padding is removed or halved where the card already provides outer margin
- The details side panel body in `draw_details` uses `obsidian::theme::elevated_panel_show(ui, 1, |ui| { ... })`
- `cargo test` passes; `cargo clippy` clean

**Branch:** `warden/v0.9-cards`

**Files:** `src/app.rs` — `draw_app_list` (row loop), `draw_details` (panel body wrapper)

---

### 2.5 Obsidian Navigation Widgets (Issue #32)

**Description:** The Apps/Logs toggle is a plain button and the log viewer filter chips are plain buttons. Replace with `TabStrip` and `ButtonGroup` respectively.

**Acceptance criteria:**
- The `[Logs]`/`[Apps]` toggle in `draw_ui` is replaced with `obsidian::widgets::TabStrip` containing two `TabItem`s ("Apps", "Logs"); active tab drives `self.show_log_viewer`
- The `● All` and per-app chip buttons in `draw_log_viewer` are replaced with `obsidian::widgets::ButtonGroup::new(ButtonGroupKind::Selection, items)` with `ButtonGroupItem` entries
- `○`/`●` prefixes removed from chip labels (selection state communicated by ButtonGroup itself)
- `cargo test` passes; `cargo clippy` clean

**Branch:** `warden/v0.9-nav-widgets`

**Files:** `src/app.rs` — `draw_ui` (header tab strip), `draw_log_viewer` (filter chip bar)

---

## 3. Parallel Implementation Strategy

**Phase 1 (parallel — batch 1):** Items 2.1 and 2.2 can run concurrently.
- 2.1 (TactileButton) touches `src/app.rs` button call sites only — replaces `egui::Button::new()` calls in-place
- 2.2 (Aura foundation) touches `src/app.rs` startup block and the row selection highlight region
- Conflict risk: very low. 2.1 edits button construction expressions; 2.2 edits the resumed init block and row selection. Merge 2.1 first, then 2.2.

**Merge order for Phase 1:** merge 2.1 first (button migration), then 2.2 (foundation colors).

**Phase 2 (serial — batch 2):** Items 2.3, 2.4, and 2.5 run after Phase 1 is merged.
- 2.3 (typography) modifies heading/label calls and separators throughout `src/app.rs`
- 2.4 (cards) wraps row and panel content — structural change to `draw_app_list` and `draw_details`
- 2.5 (nav widgets) modifies the header area and log viewer chips
- 2.3 and 2.5 touch different regions (typography is pervasive labels; nav widgets is header + log viewer only) — can potentially run in parallel after Phase 1
- 2.4 (cards) structurally reorganizes `draw_app_list` rows — run last, after 2.3 and 2.5 merged

**Merge order for Phase 2:** merge 2.3, then 2.5, then 2.4.

**Conflict map summary:**

| Branch | Files touched | Conflict risk |
|---|---|---|
| `warden/v0.9-tactile-button` (2.1) | `src/app.rs` (button call sites) | Low |
| `warden/v0.9-aura-foundation` (2.2) | `src/app.rs` (init + row selection) | Low (different region from 2.1) |
| `warden/v0.9-typography` (2.3) | `src/app.rs` (labels/headings/separators) | Low (after Phase 1) |
| `warden/v0.9-nav-widgets` (2.5) | `src/app.rs` (header + log viewer) | Low (after Phase 1; different region from 2.3) |
| `warden/v0.9-cards` (2.4) | `src/app.rs` (row structure + details wrapper) | Low (after 2.3 + 2.5 merged) |

---

## 4. Out of Scope for v0.9

- Glass/blur effects (`GLASS_*`, `paint_glass_overlay`) — no stable egui wgpu shader support
- Animation timing (`DUR_FAST`, proximity glow) — egui has no animation primitives in scope
- Light theme variant — dark-only
- Custom SVG icon rasterisation — deferred to future version
- Ensign HTTP health polling — deferred; no API spec

---

## 5. Status Ledger

### Phase 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.9 |
|---|---|---|---|---|
| `warden/v0.9-tactile-button` (#28) | ☑ | ☑ | ☑ | ☑ |
| `warden/v0.9-aura-foundation` (#29) | ☑ | ☑ | ☑ | ☑ |

### Phase 2

| Branch | Design doc | Implemented | Reviewed | Merged into version/0.9 |
|---|---|---|---|---|
| `warden/v0.9-typography` (#30) | ☑ | ☐ | ☐ | ☐ |
| `warden/v0.9-nav-widgets` (#32) | ☑ | ☐ | ☐ | ☐ |
| `warden/v0.9-cards` (#31) | ☑ | ☐ | ☐ | ☐ |

### Release

| Step | Status |
|---|---|
| Version bump (`Cargo.toml` 0.8.0 → 0.9.0) | ☐ |
| `version-history.md` entry | ☐ |
| project-release (merge+tag+push) | ☐ |
| Issues #28 #29 #30 #31 #32 closed | ☐ |

### Follow-ups discovered during implementation

_(empty at start)_
