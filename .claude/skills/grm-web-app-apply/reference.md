# web-app-apply — reference

Companion to `SKILL.md`. Contains the full Q9 signal table that §3 references.

---

## Q9 signal table (web-slice disambiguation)

Reproduced verbatim from `grm-workflow-bootstrap` Step 3 Q9 for offline reference.
Design authority: `docs/design/web-app-support-design.md` §2.1.

| # | Signal source | Signal | Inferred stack | Web-app? |
|---|---|---|---|---|
| 1 | Native dep + extension | `*.swift` + `*.xcodeproj`/`Package.swift` with UI dep | SwiftUI / UIKit (Apple) | **No** (native) |
| 2 | Native dep + extension | `*.kt`/`*.java` + `AndroidManifest.xml` | Android (Kotlin/Java) | **No** (native) |
| 3 | Native dep + extension | `Info.plist`, `*.storyboard`, `ios/` + `android/` | native/mobile app shell | **No** (native) |
| 4 | File extension | `*.xaml` | WPF / WinUI / Avalonia (.NET) | **No** (native) |
| 5 | File / dep | `pubspec.yaml` with `flutter` | Flutter (cross-platform) | **No** (native) |
| 6 | Cargo.toml dep | `egui`, `iced`, `tauri`, `slint` | Rust GUI / Tauri | **No** (native) |
| 7 | Dep / import | `PyQt*`, `PySide*`, `tkinter`, `wxPython`, `kivy` | Python desktop GUI | **No** (native) |
| 8 | `package.json` deps | `react`, `react-dom` | React (web) | **Yes** |
| 9 | `package.json` deps | `react-native`, `expo` | React Native (mobile) | **No** (mobile, not browser) |
| 10 | `package.json` deps | `vue` | Vue (web) | **Yes** |
| 11 | `package.json` deps | `svelte`, `@sveltejs/kit` | Svelte / SvelteKit (web) | **Yes** |
| 12 | `package.json` deps | `@angular/core` | Angular (web) | **Yes** |
| 13 | `package.json` deps | `solid-js` | SolidJS (web) | **Yes** |
| 14 | `package.json` deps | `electron` | Electron (desktop, JS) | **No** (native desktop) |
| 15 | `package.json` deps | `next`, `nuxt`, `@remix-run/*`, `astro`, `gatsby` | meta-framework (Next→React, Nuxt→Vue, …) | **Yes** |
| 16 | TUI dep | `rich`, `textual`, `blessed`, `bubbletea`, `ratatui` | terminal UI (TUI) | **No** (TUI) |
| 17 | Config file | `vite.config.*`, `next.config.*`, `nuxt.config.*`, `svelte.config.*`, `angular.json`, `astro.config.*` | confirms/disambiguates web stack | corroborates Yes |
| 18 | Config file | `tailwind.config.*`, `postcss.config.*` | web styling | corroborating boost only |
| 19 | Server-only, no view layer | `express`/`fastify`/`flask`/`gin`/`axum`/`actix-web`/`rocket` + **no** rows 1–18 hit and **no** row 21 hit | likely headless service | **No** |
| 20 | Library manifest, no app entry | published-package shape, no UI dep | likely headless library | **No** |
| 21 | Cargo.toml dep + view-layer peer | `axum`/`actix-web`/`rocket` dep **plus** a view-layer peer (`askama`/`tera`/`maud`/`minijinja` dep, or a `templates/` directory) | Rust server-rendered web app | **Yes** |

**Server-rendered web apps (not in the Q9 table above):** Flask, Django,
FastAPI, Rails, Express, Gin **serving HTML/templates** (a `templates/` dir,
`render_template`, `res.render`, or `views/` present alongside a server dep) →
**Yes** (server-rendered web app). Rust's equivalent (`axum`/`actix-web`/
`rocket` + a view-layer peer) **is** in the Q9 table above, as row 21 — the
dependency-manifest signal is unambiguous for Rust (unlike the free-text
route-handler grep the Python/JS stacks need), so it earns a proper numbered
row rather than free text.

### Precedence

1. **Native/mobile + framework dep** (rows 1–3) — strongest.
2. **Declared runtime dep in a manifest** (rows 4–16, 21). Row 21 (Rust
   server-rendered) is checked before row 19 falls back to a headless lean:
   an `axum`/`actix-web`/`rocket` dep with a view-layer peer present hits row
   21 (Yes), not row 19 — distinct from row 6's native-GUI-only Rust coverage
   (`egui`/`iced`/`tauri`/`slint`). The bare server dep with **no**
   view-layer peer stays row 19 (headless).
3. **Config-file presence** (rows 17–18) — corroborates; a lone config file
   with no dep is a weak (Medium-confidence) signal.
4. **Negative/headless leans** (rows 19–20) — applied only when no positive
   signal (rows 1–18, 21) fired.

Meta-frameworks (row 15) resolve their base via the underlying dep (Next ⇒
React, Nuxt ⇒ Vue) and report the meta-framework as the stack hint. When two
peer web frameworks appear (e.g. a monorepo), report the highest-confidence
guess and list the runner-up so the user can choose.

### Key disambiguation

The Q9 table answers "GUI?"; the web-app fact is the narrower
"browser-delivered, server-hosted app?":

- `web-app = yes` ⊂ `GUI = Yes` — a web app is always GUI, but native/TUI/
  headless-service projects are GUI without being web apps.
- Rows 8–13/15/17–18/21 + server-rendered frameworks → web-app candidate.
- Rows 1–7/9/14/16/19–20 → GUI (or headless) but **not** a web app.

### Worked example — `mission-control` (axum + askama + `serve`)

A concrete verification case (issue #464): a repo shaped like `mission-control`
— `Cargo.toml` declares `axum` (a row-21 server dep candidate) **and**
`askama` (a row-21 view-layer peer), and the binary exposes a `serve` command.

1. Rows 1–18 evaluated first: no native/mobile/GUI/browser-JS signal fires
   (no `*.swift`, no `package.json`, no `egui`/`iced`/`tauri`/`slint` — row 6
   does **not** fire, since `axum` alone is not a native-GUI dep).
2. Row 21 evaluated: `axum` dep present **and** `askama` dep present (the
   view-layer peer) → row 21 fires → **Yes (server-rendered web app)**.
3. Row 19 is **not** reached — its condition ("no rows 1–18 hit and no row 21
   hit") is false because row 21 hit. Before this fix, `mission-control` had
   no row to hit at all except the row-19 negative lean (server dep, and
   nothing in the table recognized `askama` as disambiguating evidence),
   which is exactly the "currently undetectable" gap #464 reported.
4. Contrast: the same `Cargo.toml` with `axum` but **no** `askama`/`tera`/
   `maud`/`minijinja` dep and **no** `templates/` directory → row 21's
   condition is false → falls through to row 19 → **No, headless** (a bare
   axum JSON API, correctly classified as a headless service, not a web app).
