# Ux-demo-regress — reference
Loaded on demand by `SKILL.md`.

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
— those are user-only actions, matching the same rule as `grm-ux-demo-build`.

---

## Anti-patterns

- **Auto-running from `grm-ux-demo-build` or `grm-design-language-adapt`.** Those skills
  may note "you may want to re-run `grm-ux-demo-regress`" but never invoke this skill
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
  regressions through `grm-feedback-to-issue` so they land in the configured tracker.
