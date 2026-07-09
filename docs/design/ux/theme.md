---
adaptation-status: draft   # draft | ready-for-review | adopted  (user-controlled; skill sets draft)
---

# Theme — Design Token Tier

> **Up:** [↑ UX](README.md)


> **Machine-addressable companion to [design-language.md](design-language.md).**
> This file is the project's token authority: colour, spacing, type, radius,
> and motion scales. [components.md](components.md) references these tokens by path; raw
> values live here and nowhere else.
>
> Produced by the `grm-design-language-adapt` skill as a draft. The user reviews
> token values and advances `adaptation-status` to `adopted` when satisfied.
> **Do not edit raw values in [components.md](components.md) — edit them here.**

## Token block

```yaml
theme:
  meta:
    stack: "TODO"               # e.g. "React (web)", "SwiftUI", "Textual (TUI)"
    token-syntax: css-custom-prop
    # token-syntax options:
    #   css-custom-prop  — Web (React, Vue, Svelte, Angular, SolidJS, …)
    #   swift-asset      — SwiftUI / UIKit (Apple)
    #   android-res      — Android (Kotlin / Java)
    #   flutter-theme    — Flutter
    #   tui-style        — Terminal UI (TUI)
  color:
    accent:   { value: "#TODO", role: "primary action" }
    surface:  { value: "#TODO", role: "card / panel background" }
    text:     { value: "#TODO", role: "default body text" }
    error:    { value: "#TODO", role: "error palette base" }
    warning:  { value: "#TODO", role: "warning palette base" }
  spacing:
    unit: 4                     # base step in px / pt / dp (per stack idiom)
    scale: [0, 4, 8, 12, 16, 24, 32, 48]
  type:
    family: { sans: "TODO", mono: "TODO" }
    scale:  [12, 14, 16, 20, 24, 32]   # size ramp
    weight: { regular: 400, medium: 500, bold: 700 }
  radius:
    scale: [0, 4, 8, 12, 9999]  # last entry = pill / full-round
  motion:
    duration: { fast: 120, base: 200, slow: 320 }  # milliseconds
    easing:   { standard: "cubic-bezier(0.2,0,0,1)" }
```

> **Notes for the adapting agent:**
> - Replace every `"#TODO"` / `"TODO"` with values drawn from the upstream
>   Aura token scales, translated into the project's `token-syntax` idiom.
> - Stacks without a concept of a given tier (e.g. a TUI has colour + maybe
>   type, but no radius/motion) populate only the applicable keys and annotate
>   omitted keys with `# N/A for <stack>`.
> - Scales (`spacing`, `type`, `radius`, `motion`) must stay as **ordered
>   lists or named maps** — never standalone magic numbers.
> - `token-syntax` is seeded by GUI-framework detection in `grm-workflow-bootstrap`
>   Step 3 Q9. Do not change it without updating [components.md](components.md) accordingly.

## Schema invariants

1. **Every colour token carries a `role`** — intent over hex literal.
2. **Scales are complete lists** — adding a new size means appending to the
   list, not inserting a one-off value elsewhere.
3. **`source-sha:` is NOT tracked here** — this file derives from the same
   upstream SHA recorded in [design-language.md](design-language.md) (front-matter). Do not add
   a separate SHA field.
4. **No raw values in [components.md](components.md)** — the no-raw-values invariant is
   upheld here: every value a component needs must be reachable via a
   `theme.*` path defined in this file.

## Follow-ups

<!-- Record per-project token decisions deferred to a later cycle, e.g.:
     - Dark-mode token scale (deferred — single-theme v1.18 scope)
     - Brand accent variants (primary / secondary) -->
