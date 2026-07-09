# HTML Standards

Per-technology coding standards for HTML. Read alongside the cross-language
[standard practices](../coding-standards.md).

## Semantics & structure

- Prefer semantic elements (`<header>`, `<nav>`, `<main>`, `<section>`,
  `<article>`, `<aside>`, `<footer>`) over generic `<div>`/`<span>` wrappers;
  reach for a `<div>` only when no semantic element fits.
- Exactly one `<main>` per document and exactly one `<h1>`; heading levels
  descend without skipping (`<h1>`→`<h2>`→`<h3>`, never `<h1>`→`<h3>`).
- Use `<button>` for actions and `<a href>` for navigation — never a clickable
  `<div>` with a JS handler.
- Lists use `<ul>`/`<ol>`/`<li>`; tabular data uses `<table>` with `<th
  scope>`, not CSS-grid faux-tables.

## Accessibility

- Every interactive element is keyboard-reachable and operable (no
  mouse-only controls); visible focus states are preserved.
- Every control has an accessible name — a `<label for>`, wrapping `<label>`,
  or `aria-label` — never placeholder-as-label.
- Images carry meaningful `alt` text; decorative images use `alt=""`.
- ARIA is a last resort: use a native element before adding a `role`. Never
  override a native role with a conflicting ARIA one.
- Color is never the sole carrier of meaning; interactive text meets WCAG AA
  contrast intent.

## Forms

- Every input is associated with a `<label>`; group related controls in a
  `<fieldset>` with a `<legend>`.
- Use the most specific `type` (`email`, `tel`, `number`, `date`) and set
  `autocomplete` so browsers and assistive tech can help.
- Mark required fields with the `required` attribute, not just a visual
  asterisk; surface validation errors in text tied to the field
  (`aria-describedby`), not color alone.

## Anti-patterns

- **No inline styles.** Don't use the `style=` attribute or scattered `<style>`
  blocks in markup. Keep all styling centralized in stylesheets (see
  [CSS standards](css.md)) so it lives in one place. Construct every DOM element
  consistently and cleanly — no one-off styling exceptions lingering in the
  markup that are hard to find later.
- No `<div>`/`<span>` where a semantic element fits ("div soup").
- No clickable non-interactive elements (`<div onclick>`); no removing focus
  outlines without an equivalent visible focus state.
- No images without an `alt` attribute (empty `alt=""` for decorative is fine).

## Quality enforcement (the `lint` recipe)

Web projects drive HTML quality through the recipe `lint` target. The canonical
command is `htmlhint` with semantic, accessibility-attribute, and
no-inline-style rules enabled; findings are warn-level by default and escalate
to block via the v1.26 `code-quality` `audit-gate` dial. Design:
`../design/html-css-quality-enforcement-design.md`.

## Audit hints

<!-- audit: id="html-semantic-elements" check="semantic elements used over generic div/span where one fits; no div-soup" severity="warn" applies="html" -->
<!-- audit: id="html-single-main-h1" check="exactly one <main> and one <h1>; heading levels descend without skipping" severity="info" applies="html" -->
<!-- audit: id="html-button-vs-anchor" check="<button> for actions, <a href> for navigation; no clickable <div>" severity="warn" applies="html" -->
<!-- audit: id="html-keyboard-reachable" check="every interactive element is keyboard-reachable with a visible focus state" severity="warn" applies="html" -->
<!-- audit: id="html-accessible-name" check="every control has a label/aria-label; placeholder is not used as the label" severity="warn" applies="html" -->
<!-- audit: id="html-img-alt" check="every <img> has an alt attribute (alt='' for decorative)" severity="warn" applies="html" -->
<!-- audit: id="html-native-before-aria" check="native elements preferred over ARIA roles; no conflicting role overrides" severity="info" applies="html" -->
<!-- audit: id="html-form-labels" check="every input associated with a <label>; related controls grouped in fieldset/legend" severity="warn" applies="html" -->
<!-- audit: id="html-input-type-autocomplete" check="most-specific input type set; autocomplete provided where applicable" severity="info" applies="html" -->
<!-- audit: id="html-no-inline-style" check="no style= attributes or scattered <style> blocks in markup" severity="warn" applies="html" -->
