# Release Planning — v0.3.0

status: agreed

## §1 Overview

**Version:** v0.3.0
**Base:** v0.2.0 (tag `v0.2.0`, commit `c4e15cb`)
**Staging branch:** `version/0.3` (from `dev`)
**Paradigm:** Noir — autonomous dispatch, pre-authorized push

### Goals

Ship three enhancements that complete the Aura design foundation and open the
headless agent-verification surface required by the Grimoire baseline spec:

1. **#9 — Visual-inspection CLI** (Grimoire-Requirement, flagship)
2. **#12 — Aura spacing and radius tokens**
3. **#16 — App details pane**

---

## §2 Work items

| # | Issue | Title | Size | Files touched | Phase |
|---|-------|-------|------|---------------|-------|
| 2.1 | #12 | Apply Aura spacing/radius tokens throughout `src/app.rs` | S | `src/app.rs` | 1 |
| 2.2 | #9  | Visual-inspection CLI (`--dump-ui` JSON to stdout) | M | `src/main.rs`, `src/app.rs` (new `pub fn dump_ui_json`), possibly `src/dump_ui.rs` | 1 |
| 2.3 | #16 | App details pane (`SidePanel::right`, click-to-select) | M | `src/app.rs` | 2 |

**Phase 1 (parallel):** Items 2.1 and 2.2 can run concurrently.
- #12 touches only spacing/radius call-sites in `draw_ui` — no structural change.
- #9 adds a new `--dump-ui` flag path in `main` and a `pub fn dump_ui_json` on
  `AppState`; its `src/app.rs` touch is additive (new pub method), not overlapping
  with #12's spacing patches.

**Phase 2 (serial):** Item 2.3 runs after Phase 1 merges cleanly, so the
details pane can reference Aura tokens already present in `draw_ui`.

---

## §3 Token estimates

| Item | Estimated input tokens | Estimated output tokens |
|------|----------------------|------------------------|
| #12 Aura tokens | ~6 k | ~2 k |
| #9 Visual inspection CLI | ~8 k | ~3 k |
| #16 App details pane | ~9 k | ~4 k |
| **Total** | **~23 k** | **~9 k** |

---

## §4 Design work

- **#9** — no separate design doc needed; implementation is a pure CLI addition
  (JSON stdout from `AppState`). Acceptance criteria in the issue are complete.
- **#12** — no separate design doc; token map is fully specified in the issue body
  and `docs/design/ux/design-language.md`.
- **#16** — no separate design doc needed; UI spec is complete in the issue body
  (selection model, `SidePanel::right`, panel sections).

---

## §5 Ledger

| Row | Item | Branch | Status | Notes |
|-----|------|--------|--------|-------|
| 5.1 | #12 Aura tokens | `warden/v0.3-aura-tokens-impl` | ☑ Merged | Phase 1 |
| 5.2 | #9 Visual CLI | `warden/v0.3-visual-cli` | ☑ Merged | Phase 1 |
| 5.3 | #16 Details pane | `warden/v0.3-details-pane` | ☑ Merged | Phase 2 |
| 5.4 | Version bump | — | ☑ Done | `Cargo.toml` 0.2.0 → 0.3.0 |
| 5.5 | project-release | — | ☑ Done | merge+tag+push; issues #9 #12 #16 closed |

---

## §6 Constraints and notes

- `autonomous-push.enabled: true` — push is pre-authorized at release step.
- `model-effort-profile: Cheap-Sonnet` — never call Opus; Sonnet only.
- No `Co-Authored-By` trailers on any commit.
- All branches from `version/0.3`.
- `cargo test` must pass after each merge before advancing.
- Issue closes happen after push completes (via issue-tracker CLI or MCP).
