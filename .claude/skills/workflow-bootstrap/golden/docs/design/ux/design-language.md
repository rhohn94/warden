---
source: upstream
# Aura design language upstream (seeded by workflow-bootstrap v1.13+; overridable).
# Forks or projects with a private design language: replace source-url with your URL.
# Idempotency: workflow-bootstrap will not overwrite source-url if already set.
# NOTE (CONFIRM-pending): https://github.com/rhohn94/design-language is a
# placeholder URL — confirm the canonical Aura repo URL before first adapt run.
source-url: https://github.com/rhohn94/design-language
source-sha:
source-pin:
adaptation-status: draft
---

# UX Design Language

> **Status:** stub — run `design-language-adapt` to produce the full adaptation.

This file is the per-project adaptation of the upstream Aura design language.
Fill `source-url:` with the correct upstream URL (if the default above is wrong
for your fork), then run the `design-language-adapt` skill to clone the source
and generate the adapted content.

## Primary stack

<!-- workflow-bootstrap fills this with the project's GUI framework -->
<!-- e.g. "Primary stack: SwiftUI" — consumed by ux-demo-build -->

## Follow-ups

- Run `design-language-adapt` to complete the initial adaptation.
