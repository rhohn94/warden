# CSS Standards

Per-technology coding standards for CSS. Read alongside the cross-language
[standard practices](../coding-standards.md).

## Organisation & naming

- Adopt one naming convention (BEM is the worked example —
  `block__element--modifier`) and apply it consistently across the codebase.
- One concern per stylesheet; keep files focused and modular rather than one
  monolithic `styles.css`. Co-locate component styles with their component where
  the stack supports it.
- Keep selector specificity flat — prefer a single class over descendant or
  element-qualified chains; avoid IDs for styling.
- Nesting depth stays shallow (≤3 levels); deep nesting signals a structural
  problem to refactor, not a selector to grow.

## Layout

- Prefer modern layout (flexbox / grid) over floats and absolute positioning.
- Use logical properties (`margin-inline`, `padding-block`) for
  writing-direction-agnostic layout where supported.
- Size with relative units (`rem`, `%`, `fr`, `ch`) over fixed `px` for
  type and spacing so layouts scale and respect user settings.

## Units & values

- No magic numbers — use design tokens / CSS custom properties (`--space-md`,
  `--color-accent`) for shared values; a raw value should appear once, at its
  token definition.
- Centralize the token palette (colors, spacing, type scale, radii) in one
  `:root` block or tokens file; components reference tokens, never raw values.

## Modularity & DRY

- Factor repeated declaration groups into a shared class, custom property, or
  utility rather than copy-pasting rule blocks.
- Delete dead CSS — selectors no targeting any rendered markup are removed, not
  left "just in case" (the dead-CSS pass in `grm-code-health` surfaces them).

## Anti-patterns

- Avoid over-qualified selectors and `!important` (an `!important` is a
  documented last resort, never a default reach).
- No IDs as styling hooks; no inline `style=` (see below).
- **No inline styles.** Styling belongs in stylesheets, never in HTML `style=`
  attributes. Centralizing every rule in one place keeps styling consistent and
  discoverable, with no hard-to-find exceptions scattered through the markup.
- No deeply nested selector chains (>3 levels); no duplicated declaration
  blocks that a shared class would cover.

## Quality enforcement (the `lint` recipe)

Web projects drive CSS quality through the recipe `lint` target. The canonical
command is `stylelint` (standard config plus `no-!important` and the chosen
naming pattern), with a dead-CSS pass (PurgeCSS-style dry-run against templates)
and a `jscpd` duplication pass surfaced through `grm-code-health`. Findings are
warn-level by default and escalate to block via the v1.26 `code-quality`
`audit-gate` dial. Design: `../design/html-css-quality-enforcement-design.md`.

## Audit hints

<!-- audit: id="css-naming-convention" check="one naming convention (e.g. BEM) applied consistently; no ad-hoc mixed schemes" severity="info" applies="css" -->
<!-- audit: id="css-flat-specificity" check="selectors prefer a single class; no IDs for styling; specificity stays flat" severity="warn" applies="css" -->
<!-- audit: id="css-shallow-nesting" check="nesting depth ≤3 levels" severity="info" applies="css" -->
<!-- audit: id="css-modern-layout" check="flexbox/grid preferred over floats and absolute positioning" severity="info" applies="css" -->
<!-- audit: id="css-relative-units" check="relative units (rem/%/fr/ch) preferred over fixed px for type and spacing" severity="info" applies="css" -->
<!-- audit: id="css-design-tokens" check="shared values via custom properties/design tokens; no repeated magic numbers" severity="warn" applies="css" -->
<!-- audit: id="css-no-important" check="no !important except a documented last-resort with a comment" severity="warn" applies="css" -->
<!-- audit: id="css-no-inline-style" check="no inline style= attributes; styling lives in stylesheets" severity="warn" applies="css" -->
<!-- audit: id="css-no-dead-selectors" check="no dead CSS — selectors matching no rendered markup are removed" severity="warn" applies="css" -->
<!-- audit: id="css-dry-declarations" check="repeated declaration groups factored into a shared class/utility, not copy-pasted" severity="warn" applies="css" -->
