---
name: ux-demo-regress
description: Capture and compare ux-demo/ screenshots against a committed baseline to detect visual drift in the design-language adaptation. Opt-in, GUI-projects-only, never auto-run. Use after an adaptation review to establish a baseline (--accept), or to check for drift against the stored baseline (--check). Triggers on "visual regression", "check ux-demo screenshots", "accept baseline", "check baseline", "detect ux drift", "run ux-demo-regress".
---

# ux-demo-regress

Captures `ux-demo/` screenshots and compares them against a committed baseline,
surfacing visual drift in the design-language adaptation. Pairs with
`ux-demo-build` (which constructs the demo); this skill *evaluates* it with
pass/fail semantics. GUI-projects-only. Never auto-run.

---

## When to use this skill

- **After the user has reviewed and adopted an adaptation** — run with `--accept`
  to capture the accepted screenshots as the baseline of record.
- **After any subsequent adaptation change** — run with `--check` (the default)
  to surface drift before re-accepting.
- **On demand, by explicit user request only.** This skill is never invoked
  automatically by `ux-demo-build`, `design-language-adapt`, hooks, or CI
  without explicit user sign-off.
- **GUI projects only.** If the project is headless / non-GUI (the roadmap
  carries a UX-deferral note or `workflow-bootstrap` recorded "No, headless"),
  skip this skill entirely. There is nothing to capture.

---

## Modes

| Flag | Meaning |
|---|---|
| `--accept` | Capture the full item set, write into `screenshots/baseline/`, regenerate `visual-regression.json`. Used to establish or update the baseline. Overwriting an existing baseline requires explicit user confirmation. |
| `--check` | (default) Capture fresh, diff each item against its baseline, write diffs to `screenshots/diff/`, emit a drift report. |

No baseline present when `--check` is run ⇒ report "no baseline — run with
`--accept` first" and stop. Never treat a first capture as a silent pass.

---

## Store layout

```
ux-demo/
  screenshots/            # working / current screenshots per checklist item
  screenshots/baseline/   # committed — the accepted reference set
  screenshots/diff/       # gitignored — generated diff artifacts per run
  visual-regression.json  # manifest: item → baseline file, capture meta, tolerance
```

- `screenshots/baseline/` **must be committed** — it travels with the repo so
  diffs are reproducible on any checkout.
- `screenshots/diff/` is **gitignored** — ephemeral, regenerated each `--check`
  run.
- `visual-regression.json` is committed alongside `screenshots/baseline/`.

Ensure the project's `.gitignore` contains the line `ux-demo/screenshots/diff/`
before the first `--accept` run. Add it if absent.

---

## Step 1 — Verify preconditions

1. Confirm the project is GUI (non-headless). If not, stop with a clear message.
2. Confirm `ux-demo/` exists at the repo root (it should have been built by
   `ux-demo-build` first). If absent, stop: "Run `ux-demo-build` first to create
   the demo."
3. Read `docs/design/ux/design-language.md` front-matter to confirm
   `adaptation-status` is `ready-for-review` or `adopted`. If it is `draft`,
   warn the user that capturing against an unadopted draft may produce a
   misleading baseline, and ask them to confirm before continuing.
4. If `docs/design/ux/theme.md` and/or `docs/design/ux/components.md` exist,
   record their current Git SHA (or content hash if untracked) — this is the
   **token SHA** written into `visual-regression.json` and used in the drift
   report to correlate drift against deliberate design-token changes.

---

## Step 2 — Determine the item set

The item set is the list of named components / acceptance-checklist items that
will be captured as screenshots. Derive it in priority order:

1. **From `visual-regression.json`** if it exists — use its `items` list. This
   preserves names across runs and avoids renaming drift.
2. **From `docs/design/ux/components.md`** if it exists — one item per named
   component (e.g. `primary-button`, `text-field`, `error-banner`).
3. **From the adaptation-acceptance checklist** in `docs/design/ux/design-language.md`
   — one item per checklist entry, using the checklist label as the item name.
4. **From the existing `screenshots/` directory** — one item per `.png` / `.jpg`
   already present, using the filename stem as the item name.

If none of these sources yield an item set, ask the user to name the items
(components / views / checklist entries) to capture before proceeding.

Present the derived item set to the user and get confirmation before capturing.
Do not silently skip items or add extras beyond the agreed set.

---

## Step 3 — Capture parameters (tool-agnostic)

For each item, a capture is one deterministic screenshot at fixed parameters:

- **Viewport size**: read from `visual-regression.json` (field
  `capture.viewport`, e.g. `{ width: 1280, height: 720 }`). If not yet set,
  default to `1280×720` and record it in the manifest on first `--accept`.
- **Device-pixel-ratio (DPR)**: read from `visual-regression.json` (field
  `capture.dpr`). Default `1`. Record on first `--accept`.
- **Animations**: disabled. The skill instructs the capture step to set
  `prefers-reduced-motion: reduce` (web), disable `UIView.animationsEnabled`
  (iOS), or equivalent, so `theme.motion` does not cause non-deterministic
  pixel variation.
- **Isolation**: each item is captured in isolation — not in a running composite
  view — so a change in one component does not bleed into another item's diff.

The *how* of capturing is project-stack-specific and left to the project:

| Stack | Typical capture mechanism |
|---|---|
| Web (any framework) | Headless browser (Playwright, Puppeteer, Cypress) screenshot of the component's URL or story |
| SwiftUI / UIKit | `XCUIApplication.screenshot()` or SwiftUI `ImageRenderer` |
| Android (Kotlin/Java) | Espresso `Screenshot.capture()` or Paparazzi |
| Flutter | `flutter_test` golden files |
| TUI (Rich, Textual, bubbletea) | Terminal-capture / `console.export_svg()` or character-grid snapshot |
| Electron | `BrowserWindow.capturePage()` |

If no capture mechanism exists yet for the project, the skill asks the user to
describe how screenshots are taken and records the answer in `visual-regression.json`
under `capture.method` as a note (free-text; not parsed further).

---

## Step 4A — Accept flow (`--accept`)

1. **Confirm overwrite** if `screenshots/baseline/` already contains files: show
   the existing baseline item list and ask the user to confirm before overwriting.
   Never silently replace the baseline.
2. Run / instruct the capture for each item using the project's capture mechanism.
   Save each screenshot to `screenshots/baseline/<item-name>.png` (or `.jpg` if
   the stack naturally produces JPEG).
3. Regenerate `visual-regression.json` with:
   - `items`: one entry per item (see manifest schema below).
   - `capture.viewport`, `capture.dpr`, `capture.method`.
   - `token-sha`: the SHA / hash of `theme.md` and `components.md` at the moment
     of this accept (see Step 1.4). Record each file's SHA separately if both
     exist; omit the field if neither file exists.
   - `accepted-at`: ISO-8601 timestamp.
4. Ensure `ux-demo/screenshots/diff/` is listed in `.gitignore` (add if absent).
5. Report the accepted item set and remind the user to `git add` and commit
   `screenshots/baseline/` and `visual-regression.json` to lock in the baseline.

---

## Step 4B — Check flow (`--check`, default)

1. Check that `screenshots/baseline/` exists and contains at least one file.
   If not: stop — "no baseline — run with `--accept` first."
2. Ensure `screenshots/diff/` exists (create if absent; it is gitignored).
3. For each item in the manifest's `items` list:
   a. Capture a fresh screenshot to `screenshots/<item-name>-current.png`
      (ephemeral; not committed).
   b. Diff the current capture against `screenshots/baseline/<item-name>.png`
      using the item's `mode` (`pixel` or `structural`) and `tolerance`.
   c. If diff exceeds tolerance, write the annotated diff image to
      `screenshots/diff/<item-name>-diff.png`.
4. Emit the drift report (see §Drift report format below).
5. Clean up ephemeral current captures (`*-current.png`) after the report is
   emitted, unless the user asks to keep them.

---

## Diff approach

### Pixel-diff (primary, default)

Compare current and baseline pixel-by-pixel. Report the **fraction of differing
pixels** as a percentage. The per-item `tolerance` in the manifest (e.g.
`"0.10%"`) absorbs sub-pixel anti-aliasing variation. Above tolerance ⇒ DRIFT.

Diff image: the pixel delta is highlighted (e.g. red overlay on changed pixels)
and written to `screenshots/diff/<item-name>-diff.png`.

This mode needs no DOM / view-tree access and works for every stack including TUI
(character-grid diff treated as pixel-equivalent: fraction of changed cells).

### Structural (fallback / opt-in)

Where the stack exposes a render tree (web DOM, native view hierarchy), a
structural snapshot (serialized tree — e.g. accessibility tree, view hierarchy
dump) can be diffed instead of or alongside pixels. More stable against pure
anti-aliasing noise; blind to colour-only regressions.

A structural diff reports the **count of changed nodes** against a zero-change
tolerance by default (any structural change is flagged).

Select per-item via the manifest's `mode` field:
- `pixel` (default)
- `structural`
- `both` (pixel-diff + structural diff; DRIFT if either exceeds its tolerance)

---

## Manifest schema — `visual-regression.json`

```json
{
  "schema-version": "1",
  "accepted-at": "2026-05-31T00:00:00Z",
  "token-sha": {
    "theme.md": "<git-sha-or-content-hash>",
    "components.md": "<git-sha-or-content-hash>"
  },
  "capture": {
    "viewport": { "width": 1280, "height": 720 },
    "dpr": 1,
    "method": "playwright headless"
  },
  "items": [
    {
      "name": "primary-button",
      "baseline": "screenshots/baseline/primary-button.png",
      "mode": "pixel",
      "tolerance": "0.10%"
    },
    {
      "name": "text-field",
      "baseline": "screenshots/baseline/text-field.png",
      "mode": "pixel",
      "tolerance": "0.10%"
    },
    {
      "name": "error-banner",
      "baseline": "screenshots/baseline/error-banner.png",
      "mode": "both",
      "tolerance": "0.10%"
    }
  ]
}
```

Fields:

| Field | Description |
|---|---|
| `schema-version` | Always `"1"` for this revision of the manifest. |
| `accepted-at` | ISO-8601 timestamp of the last `--accept` run. |
| `token-sha` | SHA / content hash of `theme.md` and/or `components.md` at accept time. Present only if those files exist. |
| `capture.viewport` | Fixed viewport used for all captures. |
| `capture.dpr` | Device-pixel-ratio used for all captures. |
| `capture.method` | Free-text note on the capture tool / command. |
| `items[].name` | Stable name for the component / checklist item. |
| `items[].baseline` | Path to the baseline screenshot (relative to repo root). |
| `items[].mode` | `pixel` \| `structural` \| `both`. |
| `items[].tolerance` | For `pixel`: max fraction of changed pixels (e.g. `"0.10%"`). For `structural`: max changed nodes (e.g. `0` for strict). |

---

## Drift report format

`--check` emits a structured table — not a wall of prose:

```
UX demo visual-regression report
Baseline accepted: 2026-05-31T00:00:00Z
Token SHA (theme.md):      abc1234
Token SHA (components.md): def5678
Current token SHA (theme.md):      abc1234   [unchanged]
Current token SHA (components.md): 999aaab   [CHANGED — expected drift]

Component / item   Mode        Diff     Tolerance   Verdict
-----------------  ----------  -------  ----------  -------
primary-button     pixel       0.04%    0.10%       PASS
text-field         pixel       0.08%    0.10%       PASS
error-banner       pixel       2.30%    0.10%       DRIFT  → screenshots/diff/error-banner-diff.png
```

For each DRIFT row:

- Name the diff artifact path in `screenshots/diff/`.
- Note whether the baseline's recorded `token-sha` differs from the current
  file's SHA:
  - **Token SHA changed** → likely *expected* drift from a deliberate adaptation
    update; prompt the user to re-run `--accept` after reviewing.
  - **Token SHA unchanged** → likely *unexpected* regression; prompt the user to
    investigate the component code.

The skill **reports** drift. It never auto-accepts a new baseline, never ticks
the adaptation-acceptance checklist, and never marks `adaptation-status: adopted`
— those are user-only actions, matching the same rule as `ux-demo-build`.

---

## Step 5 — Next-step guidance

After the drift report, offer the user a clear next-step menu:

- **All PASS**: "Baseline matches current demo. No action needed."
- **DRIFT with token change**: "Design-token files changed since the last
  baseline. Review the diffs in `screenshots/diff/`, then re-run `ux-demo-regress
  --accept` to update the baseline once satisfied."
- **DRIFT with no token change**: "Unexpected drift detected (no design-token
  changes). Investigate `ux-demo/` component code. Diff artifacts are in
  `screenshots/diff/`. After fixing, re-run `--check` to confirm."

---

## Filing issues discovered during regression

If a drift reveals a real UX problem (token mismatch, broken component state,
accessibility regression), do not append directly to `docs/roadmap.md ## Backlog`.
File it via the issue-tracker abstraction:

```bash
python3 .claude/skills/issue-tracker/issue_tracker.py create \
  --title "<one-line UX regression description>" \
  --body "<component / what / expected / actual / diff artifact path>" \
  --labels ux,regression \
  --audience internal
```

Or invoke the `feedback-to-issue` skill directly.

---

## Anti-patterns

- **Auto-running from `ux-demo-build` or `design-language-adapt`.** Those skills
  may note "you may want to re-run `ux-demo-regress`" but never invoke this skill
  themselves. Explicit user request only.
- **Silently replacing the baseline.** `--accept` on an existing baseline always
  asks for confirmation first. The baseline is the reference of record.
- **Treating first capture as a pass.** If no baseline exists, `--check` stops
  and reports "no baseline — run with `--accept` first."
- **Auto-ticking the adaptation-acceptance checklist or setting `adaptation-status:
  adopted`.** Those are user-only actions.
- **Running on a headless / non-GUI project.** There is no `ux-demo/` to capture.
  Skip the skill.
- **Committing `screenshots/diff/`.** The diff dir is ephemeral and gitignored.
  Never commit it.
- **Capturing at non-deterministic parameters.** Every capture must use the
  manifest's recorded viewport, DPR, and animation-disabled settings so
  pixel-diffs are byte-comparable across machines and runs.
- **Directly appending UX issues to `docs/roadmap.md ## Backlog`.** Route
  regressions through `feedback-to-issue` so they land in the configured tracker.
