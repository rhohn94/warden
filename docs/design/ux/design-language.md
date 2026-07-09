---
source: upstream
source-url: https://github.com/rhohn94/design-language
source-sha: 98d2d25
source-pin: v2.34
adaptation-status: complete
---

# UX Design Language — Warden

Adaptation of the Aura design language for the obsidian (egui/wgpu) stack.
Tokens are vendored in `obsidian-api` at `obsidian_api::aura_generated::aura`.
All GUI code must import from that module — never hardcode colour literals or
spacing values.

## Primary stack

Primary stack: `obsidian (egui/wgpu)` — consumed by `grm-ux-demo-build`.

Dependency: `obsidian = { git = "https://github.com/rhohn94/obsidian.git", tag = "v0.44.0" }`.
The `obsidian` crate re-exports `obsidian_api`, so token paths resolve as:

```rust
use obsidian::obsidian_api::aura_generated::aura;
```

---

## Colour tokens

Imported from `obsidian_api::aura_generated::aura`. All are `egui::Color32`.

### Status / semantic colours

| Constant | Value (sRGB) | Use |
|---|---|---|
| `aura::SUCCESS` | rgb(52, 211, 153) — green | Running state badge, positive indicators |
| `aura::WARNING` | rgb(251, 191, 36) — amber | Unknown state badge, caution indicators |
| `aura::DANGER` | rgb(251, 113, 133) — rose | Error state, destructive actions |
| `aura::INFO` | rgb(34, 211, 238) — cyan | Informational badges |

**Warden-specific status badge mapping:**

| App state | Token | Colour |
|---|---|---|
| `AppStatus::Running` | `aura::SUCCESS` | green |
| `AppStatus::Stopped` | `aura::TEXT_MUTED` | grey (`rgb(178, 183, 197)`) |
| `AppStatus::Unknown` | `aura::WARNING` | amber |

> The release plan uses the shorthand `COLOR_SUCCESS`, `COLOR_SURFACE_MUTED`,
> `COLOR_WARNING`. These resolve to `aura::SUCCESS`, `aura::TEXT_MUTED`, and
> `aura::WARNING` respectively. Agents must use the `aura::` path, not any alias.

### Surface and background colours

| Constant | Value | Use |
|---|---|---|
| `aura::BG` | rgb(10, 11, 18) | Main window background |
| `aura::BG_2` | rgb(14, 16, 25) | Secondary panels |
| `aura::SURFACE_SOLID` | rgb(20, 22, 31) | Solid surface cards |
| `aura::SURFACE_1` | 4% white overlay | Subtle row highlight |
| `aura::SURFACE_2` | 6% white overlay | Hover state |
| `aura::SURFACE_3` | 9% white overlay | Active/pressed state |
| `aura::SURFACE_STROKE` | 12% white overlay | Dividers, borders |
| `aura::SURFACE_STROKE_STRONG` | 22% white overlay | Prominent borders |

### Text colours

| Constant | Value | Use |
|---|---|---|
| `aura::TEXT` | rgb(242, 245, 252) | Primary text |
| `aura::TEXT_MUTED` | rgb(178, 183, 197) | Secondary text, stopped-state badge |
| `aura::TEXT_SUBTLE` | rgb(129, 134, 147) | Hints, labels |

### Primary (brand) colours

| Constant | Value | Use |
|---|---|---|
| `aura::PRIMARY` | rgb(118, 84, 245) | Primary buttons, focus rings |
| `aura::PRIMARY_A25` | 25% primary overlay | Button hover fill |
| `aura::ON_PRIMARY` | rgb(255, 255, 255) | Text on primary buttons |

---

## Spacing tokens

All values are `f32` egui points (1 rem = 16 pt).

| Constant | Value | Use |
|---|---|---|
| `aura::SPACE_1` | 4.0 pt | Tight intra-element gap |
| `aura::SPACE_2` | 8.0 pt | Default inner padding |
| `aura::SPACE_3` | 12.0 pt | Row padding (vertical) |
| `aura::SPACE_4` | 16.0 pt | Section gap |
| `aura::SPACE_5` | 24.0 pt | Large section gap |

---

## Corner radii

| Constant | Value | Use |
|---|---|---|
| `aura::RADIUS_XS` | 4.0 pt | Small badges, chips |
| `aura::RADIUS_SM` | 8.0 pt | Buttons, inputs |
| `aura::RADIUS_MD` | 12.0 pt | Cards, panels |
| `aura::RADIUS_LG` | 16.0 pt | Modals, large containers |

---

## Interactive element sizing

| Constant | Value | Use |
|---|---|---|
| `aura::CONTROL_HEIGHT_SM` | 32.0 pt | Small buttons (Start/Stop) |
| `aura::CONTROL_HEIGHT_MD` | 40.0 pt | Default button height |
| `aura::INPUT_HEIGHT` | 44.0 pt | Text inputs |

---

## Typography

Aura's font tokens are comments in `aura_generated.rs` (egui does not support
numeric font weights via `FontId`). Register fonts at startup via `egui::FontData`.

| Role | egui family | Aura source |
|---|---|---|
| Body / UI text | `egui::FontFamily::Proportional` | `FONT_SANS` (system-ui, -apple-system, Segoe UI, Roboto) |
| Code / mono | `egui::FontFamily::Monospace` | `FONT_MONO` (SF Mono, JetBrains Mono, Fira Code, Menlo) |

Line-height constants for layout maths:

| Constant | Value | Use |
|---|---|---|
| `aura::LEADING_TIGHT` | 1.2 | Compact labels |
| `aura::LEADING_NORMAL` | 1.5 | Body text |
| `aura::LEADING_RELAXED` | 1.7 | Readable paragraphs |

---

## Widget primitives rule

All widget rendering must use `obsidian` widget primitives. No hand-rolled
`Color32` literals or spacing values. Every colour, spacing value, radius, or
height must reference an `aura::*` constant. This rule applies in `src/app.rs`
and any future widget modules.

---

## Consuming the theme at startup

```rust
use obsidian::obsidian_api::aura_generated::aura;
use egui::Visuals;

fn apply_warden_theme(ctx: &egui::Context) {
    let mut visuals = Visuals::dark();
    visuals.window_fill = aura::BG;
    visuals.panel_fill = aura::BG_2;
    visuals.override_text_color = Some(aura::TEXT);
    ctx.set_visuals(visuals);
}
```

---

## Out-of-scope for v0.1

- Light theme variant (dark-only for now)
- Custom SVG icon rasterisation (the obsidian `resvg` pipeline — v0.2+)
- Glass/blur effects (`GLASS_*` tokens — no egui native support without wgpu shader)
- Animation timing (`ENTRANCE_STEP`, `TOOLTIP_DELAY`, etc.) — no egui animation primitives in scope
