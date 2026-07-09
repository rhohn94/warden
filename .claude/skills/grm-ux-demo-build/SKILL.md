---
name: grm-ux-demo-build
description: Build (or refresh) a minimal ux-demo/ app in the project's own tech stack to verify the project's design-language adaptation. Opt-in only — not auto-maintained. Use after the first design-language-adapt to produce a demo for user review, or on demand when the adaptation has changed.
---

# ux-demo build

Builds a minimal `ux-demo/` sub-project in the project's own primary stack,
proving that the local `docs/design/ux/design-language.md` adaptation renders
correctly in real project code. Run once after the first `grm-design-language-adapt`,
or on-demand after a re-adaptation. Never auto-run.

---

## When to use this skill

- **Primary moment:** right after `grm-design-language-adapt` completes its first
  run and the user wants to verify the adaptation in running code.
- **On-demand refresh:** any time the adaptation has changed (upstream re-pull,
  manual edit) and the user explicitly asks to rebuild or refresh the demo.
- **Never** invoke this skill automatically — not from `grm-design-language-adapt`,
  not from CI, not from any hook. The demo is opt-in.
- **GUI projects only.** If the project is headless / non-GUI (bootstrap
  answered "No, headless" or the roadmap carries a UX-deferral note), skip
  this skill entirely. There is nothing to demo.

---

## Step 1 — Confirm the demo's scope is correct

Before writing a single line of code, settle what the demo will and will not
cover.

**Rule: as small as possible.** The first version of any project's `ux-demo`
covers only the 2–5 controls or views the project itself uses most — not a
full implementation of the design language.

**Include:**
- A primary action button (the project's main call-to-action).
- A text input or form field if the project has one.
- A single representative view or screen (e.g. a settings panel, a list view).
- An error state if the project surfaces errors to users.

**Defer:**
- Full theme-switching or dark/light toggle.
- Every component variant (outlined, filled, ghost, …).
- Edge-case states (disabled, loading spinner, empty state) unless the project
  actively shows them.
- Long-tail controls the project does not use in real code.

Ask: *could a reviewer tell from a smaller demo whether this adaptation fits
the project?* If yes, trim further.

**Stack purity — non-negotiable.** The demo must be written entirely in the
project's own primary tech stack:

- Desktop GUI project → project's GUI framework (e.g. Qt, wxWidgets, SwiftUI,
  Electron with project's own JS). No raw HTML/CSS.
- Web project → project's own frontend stack (React, Vue, plain HTML —
  whichever the project uses). No importing a foreign framework just for the
  demo.
- CLI / TUI project → project's terminal-rendering stack (Rich, curses,
  bubbletea, etc.). No React, no headless browser.
- Library project → the library's documented host stack. No demo framework
  the library doesn't itself depend on.

If you are unsure of the project's primary stack, read the "Primary stack:"
note in `docs/design/ux/design-language.md` §Design (recorded there by
`grm-workflow-bootstrap`'s GUI interview), or ask the user before proceeding.

---

## Step 2 — Locate or scaffold `ux-demo/`

1. Check whether `ux-demo/` exists at the **repo root** (peer to `src/` or
   the project's equivalent source tree).
2. If absent, create it using the project's stack's idiomatic structure — e.g.
   a `main.py` entry point, an `App.swift` file, an `index.html` — whatever a
   minimal runnable sub-project looks like for that stack.
3. Ensure `ux-demo/screenshots/` exists (create empty dir if needed). This is
   where screenshot evidence referenced from the acceptance checklist lives.
4. Do **not** place `ux-demo/` under `docs/`, `.claude/`, or nested inside any
   existing source tree. It is a standalone peer.

---

## Step 3 — Build the demo

1. Read `docs/design/ux/design-language.md` — specifically the "Design" section
   (local tokens, component map) and the "Adaptation acceptance" checklist. The
   checklist items define exactly what the demo must show.
2. Implement only the controls and views agreed in Step 1, in the project's
   primary stack, reflecting the adapted tokens (colours, spacing, typography,
   iconography vocabulary) from `design-language.md`.
3. Do **not** touch any other project source file. `ux-demo/` is fully isolated;
   it may import the project's own theme/style definitions if they exist, but
   must not modify them.
4. Verify the demo launches without error by reading through the code for
   obvious issues. Do not silently ship broken scaffolding.

---

## Step 4 — Verification: hand off for user review

Emit a "ready for review" report containing:

1. **What was built** — a bullet list of the controls and views implemented
   (e.g. "primary button, text input, error toast").
2. **How to launch it** — the exact command or steps to run `ux-demo/` (e.g.
   `python ux-demo/main.py`, `swift run --package-path ux-demo`, `open
   ux-demo/index.html`).
3. **Checklist reference** — remind the user that
   `docs/design/ux/design-language.md` §Adaptation acceptance is the checklist
   to tick. **Only the user marks those items complete** — this skill never
   auto-marks them.
4. **Screenshots** — ask the user to capture a screenshot per checklist item
   and place them under `ux-demo/screenshots/` (e.g. `primary-button.png`,
   `error-state.png`). Screenshots can be deferred; the demo is still useful
   without them, but they are required evidence for marking the adaptation
   `adopted`.

The skill may update `adaptation-status:` in `design-language.md`'s
front-matter from `draft` to `ready-for-review` once the demo builds clean.
It must **never** set `adaptation-status: adopted` — that is the user's action
after reviewing the checklist.

---

## Step 5 — Re-runs (refresh)

When the user asks to refresh an existing demo:

1. **Diff before replacing.** Show what changed in `docs/design/ux/design-language.md`
   since the demo was last built (or ask the user to describe the changes). Do not
   silently clobber existing demo files.
2. Present the proposed changes to `ux-demo/` and get confirmation before
   writing them.
3. The refresh is idempotent: if `design-language.md` has not changed since the
   last build, report "no adaptation changes detected — demo is up to date" and
   stop.
4. After a confirmed refresh, repeat Step 4 (hand off for user review).

---

## Filing UX issues discovered during the demo build

If the demo build surfaces a real UX problem (adaptation token mismatch, a
component that renders incorrectly, an accessibility gap), **do not append a
bullet directly to `docs/roadmap.md ## Backlog`**. File it via the issue-tracker
abstraction:

```bash
# Invoke feedback-to-issue with audience=internal (UX issues are typically internal)
python3 .claude/skills/grm-issue-tracker/issue_tracker.py create \
  --title "<one-line UX issue description>" \
  --body "<what/expected/actual/context>" \
  --labels ux \
  --audience internal
```

Or invoke the `grm-feedback-to-issue` skill directly. The abstraction routes to the
configured tracker automatically (roadmap backend if no `grm-issue-tracker` block is
in `grimoire-config.json` — same behaviour as before for existing projects).

---

## Anti-patterns

- **Auto-marking the adaptation-acceptance checklist.** The checklist in
  `docs/design/ux/design-language.md` §Adaptation acceptance is user-only.
  Never tick, strike through, or set `adaptation-status: adopted` on the
  model's own authority.
- **Stack-impure demo code.** No HTML/CSS in a desktop demo, no React in a CLI
  demo, no GUI in a server demo. Purity is non-negotiable; a mixed-stack demo
  proves nothing about the project's real rendering.
- **Building a full design-language implementation.** The demo is minimal proof,
  not a component library. Every control added beyond the agreed scope is scope
  creep.
- **Auto-running from `grm-design-language-adapt`.** The adapt skill may suggest
  "the demo may need a refresh", but it never invokes this skill itself. The
  user must explicitly ask.
- **Silently clobbering existing demo files on a refresh.** Always diff and
  confirm before overwriting.
- **Placing `ux-demo/` anywhere other than the repo root.** Not under `docs/`,
  not under `.claude/`, not nested inside a source tree.
- **Directly appending UX issues to `docs/roadmap.md ## Backlog`.** Route UX
  issues through `grm-feedback-to-issue` so they land in the configured tracker.
