---
adaptation-status: draft   # draft | ready-for-review | adopted  (user-controlled; skill sets draft)
---

# Components — Component Recipe Tier

> **Up:** [↑ UX](README.md)


> **Machine-addressable companion to [design-language.md](design-language.md).**
> This file is the project's component authority: named recipes that reference
> `theme.*` token paths and describe structure, states, and the project-native
> control each component maps to.
>
> **Core invariant: no raw values.** Every visual property resolves through a
> `theme.*` path or a documented transform (e.g. `darken(token, n%)`). Raw
> hex literals, pixel values, or magic numbers are a hard violation. Edit
> [theme.md](theme.md) to change token values; recipes here reference tokens by path and
> update automatically across a re-theme.
>
> Produced by the `grm-design-language-adapt` skill as a draft. The user fills in
> `maps-to` project-native controls and advances `adaptation-status` to
> `adopted` when satisfied. **Never auto-adopt.**

## Component block

```yaml
components:
  # ---------------------------------------------------------------------------
  # Illustrative worked examples — replace / extend with project-specific set.
  # The v1.18 deliverable is the layer + schema; populating a real per-project
  # component library is downstream work (see Follow-ups below).
  # ---------------------------------------------------------------------------

  primary-button:
    maps-to: "TODO: project-native control"
    # e.g. "MUI <Button variant=contained>", "SwiftUI Button .borderedProminent",
    #      "Textual Button", "Tailwind <button class='btn-primary'>"
    intent:  "main call-to-action"
    tokens:
      background: theme.color.accent
      text:       theme.color.surface
      radius:     theme.radius.scale[1]
      padding:    [theme.spacing.scale[2], theme.spacing.scale[4]]
    states:
      hover:    { background: "darken(theme.color.accent, 8%)" }
      disabled: { opacity: 0.4 }
    a11y: "role=button; visible focus ring; 4.5:1 text contrast"

  text-field:
    maps-to: "TODO: project-native control"
    # e.g. "MUI <TextField>", "SwiftUI TextField", "Textual Input"
    intent:  "single-line text entry"
    tokens:
      border:  theme.color.text
      radius:  theme.radius.scale[1]
      padding: theme.spacing.scale[2]
    states:
      focus: { border: theme.color.accent }
      error: { border: theme.color.error }
    a11y: "associated <label>; aria-invalid on error"

  error-banner:
    maps-to: "TODO: project-native control"
    # e.g. "MUI <Alert severity=error>", "SwiftUI Label (systemImage: exclamationmark.triangle)"
    intent:  "surface a recoverable error"
    tokens:
      background: theme.color.error
      text:       theme.color.surface
      radius:     theme.radius.scale[2]
    a11y: "role=alert; not conveyed by colour alone (icon + text)"
```

> **Notes for the adapting agent:**
> - Set `maps-to` to the project-native control the `ux-demo` must use. This
>   is required for `grm-ux-demo-build` to enforce stack purity.
> - Add / remove entries to match the project's actual component set. The three
>   examples above are a starting point, not a mandate.
> - Do **not** write raw hex, pixel, or magic-number values — use `theme.*`
>   paths. If a value is not in [theme.md](theme.md), add it there first.
> - `states` entries may use documented transforms (`darken`, `lighten`,
>   `opacity`) but not arbitrary expressions.

## Schema reference

Each component entry:

| Field | Required | Description |
|---|---|---|
| `maps-to` | Yes | The project-native control the `ux-demo` renders |
| `intent` | Yes | Plain-English description of the component's purpose |
| `tokens` | Yes | Map of visual property → `theme.*` path (no raw values) |
| `states` | No | Per-interaction-state token overrides or transforms |
| `a11y` | Yes | Accessibility contract: role, focus, contrast requirements |

## Schema invariants

1. **No raw values** — every value in `tokens` or `states` is a `theme.*`
   reference or a documented transform.
2. **`maps-to` drives `grm-ux-demo-build`** — leave as `TODO` if unknown; the
   demo will skip the component until it is filled in.
3. **`source-sha:` is NOT tracked here** — this file derives from the same
   upstream SHA recorded in [design-language.md](design-language.md). Do not add a separate SHA.
4. **Each entry is independently re-generatable** — if a token path in
   [theme.md](theme.md) changes, only the referencing components need attention, not
   unrelated entries.

## Follow-ups

<!-- Record per-project component work deferred to a later cycle, e.g.:
     - Populate the full project component set (downstream of v1.18 schema)
     - Add secondary-button, modal-dialog, navigation-bar recipes
     - Complete all maps-to fields after native-control audit -->
