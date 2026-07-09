---
name: design-language-adapt
description: Adopt the UX design language for this project — pulls the upstream design-language repo (or honors strict-local mode), produces a project-specific adaptation in docs/design/ux/design-language.md, and records the source commit SHA for idempotent re-runs. Use when initializing the project's UX layer, when the upstream design language has changed, or when switching between upstream and strict-local mode. Triggers on "adopt the design language", "import design language", "set up UX design language", "adapt design language", "refresh design language".
---

# Design-language-adapt

Adapts an upstream design language into this project's own
`docs/design/ux/design-language.md`. Handles initial adaptation and
re-adaptation after upstream changes, with full offline-fallback semantics
and idempotency guarantees.

---

## Source of truth

When reading the framework's design-language source or structural docs as input
for the adaptation, prefer `.grimoire-source/` at the **repo root** if it is
present — it holds clean, unmodified copies decoupled from any in-progress edits
to the live tree (e.g. `.grimoire-source/docs/grimoire/integration-workflow.md`,
`.grimoire-source/skills/design-language-adapt/SKILL.md`).

If `.grimoire-source/` is absent (e.g. before bootstrap has run), fall back to
the live tree and emit a one-line warning:
`[warn] .grimoire-source/ not found — reading from live tree; run workflow-bootstrap to populate it.`

Note: the upstream design language repo itself (cloned into
`.design-language-source/`) is a separate concern managed by Step 2A below;
`.grimoire-source/` covers only the framework's own source artifacts.

---

## When to use this skill

- First-time UX setup: the project stub exists but `source-sha:` is empty.
- Upstream has changed and you want to pull in new changes selectively.
- Switching from `source: local` back to `source: upstream`.
- Verifying the local adaptation is up to date with the recorded SHA.

Do **not** use this skill to make edits to the local adaptation directly —
edit `docs/design/ux/design-language.md` by hand and leave `source: local`
if you want no upstream coupling.

---

## Step 1 — Read the per-project stub front-matter

Read `docs/design/ux/design-language.md`. The YAML front-matter contains:

```yaml
source: upstream          # or 'local' for strict-local mode
source-url: https://github.com/rhohn94/design-language  # Aura default (v1.13+); override for forks
source-sha:               # OUTPUT: SHA actually used; written by this skill
source-pin:               # INPUT: specific upstream SHA to pin to (optional)
adaptation-status: draft  # draft | ready-for-review | adopted
```

> **v1.13+ projects:** `source-url:` is seeded by `workflow-bootstrap` to the
> Aura design language default (`https://github.com/rhohn94/design-language`).
> This URL is a **CONFIRM-pending placeholder** — verify it points at the correct
> canonical Aura repo before running this skill for the first time. If your
> project uses a different design language or a fork, set `source-url:` to your
> URL; `workflow-bootstrap` will not overwrite a value you have already set.

Field semantics:
- `source-pin:` is an **input** expressing the user's intent to freeze the
  adaptation at a specific upstream SHA. When set, the skill checks out that
  exact commit instead of HEAD. Empty / unset = track HEAD (default).
- `source-sha:` is an **output** recording which upstream commit was actually
  used. Written by this skill after adaptation. When `source-pin:` is set,
  `source-sha:` will equal the pinned SHA after a successful run.

- If `source: upstream` → proceed to **Step 1.5**.
- If `source: local` → proceed to **Step 2B**.
- If the stub does not yet exist, stop and run the **`repo-init`** skill
  (Step 6) to create it before continuing.

---

## Step 1.5 — Source-URL allowlist check (upstream mode only)

Before any network call or clone, verify `source-url:` against the known-good
host allowlist.

**Default allowlist:**

```
github.com/rhohn94
```

Projects extend the allowlist by noting additional allowed prefixes in their
`docs/design/ux/design-language.md` (under `## Follow-ups` or a dedicated
`## Source allowlist` section) and confirming in this step. The skill reads
that note if present.

**Procedure:**

1. Extract the hostname + path-prefix from `source-url:` (e.g.
   `https://github.com/rhohn94/design-language` → `github.com/rhohn94`).
2. If the prefix matches any entry in the allowlist (default or project-extended)
   → proceed to Step 2A with no warning.
3. If the prefix does **not** match:
   - Print a clear warning:
     ```
     ⚠ source-url is outside the known-good allowlist:
       URL:       <source-url>
       Allowlist: github.com/rhohn94  (default)
     Cloning from an unrecognised host may pull unexpected content.
     ```
   - **Ask the user to confirm** before proceeding. Do **not** auto-clone.
   - If the user confirms → proceed to Step 2A.
   - If the user declines → exit. Suggest editing `source-url:` or adding the
     host to the project allowlist.

---

## Step 2A — Upstream mode: clone the source

1. **Network capability check.** Before cloning, run:

   ```bash
   git ls-remote --exit-code <source-url> HEAD
   ```

   with a short timeout (10–15 s). Three outcomes:

   - **Check succeeds** → proceed to clone.
   - **Check fails (non-network error)** — URL changed, repo deleted, or
     auth failure → fail closed with a clear message naming `<source-url>`
     and the git error. Do not proceed.
   - **Network unavailable** → fall back to the offline path (see below).

2. **Clone or refresh.** Landing directory is `.design-language-source/`
   at the **repo root** (gitignored; not under `.claude/`).

   **Depth selection.** The clone depth depends on whether `source-pin:` is
   set:
   - `source-pin:` is **empty / unset** → shallow clone (`--depth=1`) is
     sufficient; only HEAD is needed.
   - `source-pin:` is **set to a specific SHA** → omit `--depth=1`; a full
     clone is required so the pinned commit is reachable.

   - **First run (directory absent):**

     ```bash
     # Without source-pin:
     git clone --depth=1 <source-url> .design-language-source/

     # With source-pin: (full clone):
     git clone <source-url> .design-language-source/
     ```

   - **Re-run (directory already exists from a prior run):**

     ```bash
     git -C .design-language-source fetch
     git -C .design-language-source reset --hard origin/HEAD
     ```

     If the existing clone is shallow and `source-pin:` is now set, unshallow
     first: `git -C .design-language-source fetch --unshallow`.
     Never re-clone when the directory exists — reset/fetch in place instead.

3. **Apply `source-pin:` (if set).** After the clone or refresh step, and
   before recording the SHA:

   ```bash
   git -C .design-language-source checkout <source-pin>
   ```

   If the checkout fails (SHA not found), fail closed:
   "source-pin `<sha>` is not present in the cloned repo. Check the SHA or
   widen the clone depth."
   Do **not** fall back silently to HEAD.

   If `source-pin:` is empty / unset, skip this step (HEAD is already
   current from the clone/fetch).

4. **Offline fallback.** If the network check reports network unavailable:

   - `.design-language-source/` exists with content → use it as-is.
     Note in your report that the offline copy is being used and state its
     SHA (`git -C .design-language-source rev-parse HEAD`). If `source-pin:`
     is set, apply it to the local copy (Step 3 above) before continuing.
     Continue to Step 3 (adaptation).
   - `.design-language-source/` is absent or empty, and network is
     unavailable → **fail closed** with the message:
     "No network and no offline source available. Re-run with network
     access or set `source: local` in the stub front-matter."
     Do **not** auto-retry.

5. **Record the SHA.** After a successful clone, fetch, pin checkout, or
   offline re-use, capture the current HEAD SHA of the working directory:

   ```bash
   git -C .design-language-source rev-parse HEAD
   ```

   Write this value into the stub's `source-sha:` field. This is the
   **single source of truth** for which upstream commit this adaptation
   derives from — it is stored nowhere else. When `source-pin:` is set,
   `source-sha:` will equal the pinned SHA.

6. **Never add `.design-language-source/` to source control.** Confirm the
   repo root `.gitignore` contains the entry; if absent, append it:

   ```
   .design-language-source/
   ```

   Do not commit `.gitignore` itself as part of this skill run unless the
   entry was genuinely missing — check first.

---

## Step 2B — Strict-local mode

When `source: local`:

- Skip the clone step entirely. Make no network call; do not create
  `.design-language-source/`.
- Treat the project's existing `docs/design/ux/design-language.md` content
  as **authoritative**. Propose no changes to it, record no `source-sha:`
  (leave the field empty or omit it), and do not check upstream for drift.
- The skill's only useful actions in strict-local mode are:
  - Re-generating the embedded acceptance checklist if the user has
    invalidated it.
  - Asking the user whether to invoke `ux-demo-build` (see Step 5).
- To switch back to upstream coupling: edit `source: upstream` in the stub
  front-matter and re-run this skill. The missing `source-sha:` will
  trigger the initial-adaptation path.

---

## Step 3 — Produce / refresh the local adaptation

1. Read the source contents from `.design-language-source/`.

2. Generate the adaptation as a **DRAFT** for user review. The draft:

   - Maps upstream design concepts (colour tokens, spacing scale, control
     taxonomy, interaction grammar) to this project's tech stack.
   - A web project may reference upstream HTML/CSS examples almost verbatim
     with token renames. A desktop GUI project translates each concept into
     its framework's idiom. A CLI project takes only conceptual primitives
     (information hierarchy, emphasis, error/warning vocabulary).
   - Does **not** copy-paste upstream code verbatim — it describes the
     adapted approach in prose and project-native terms.

3. Write the draft into `docs/design/ux/design-language.md` while
   **preserving the existing front-matter exactly**, except:

   - Update `source-sha:` to the SHA recorded in Step 2A (step 5). Do
     **not** modify `source-pin:` — it is a user-controlled input field.
   - Set `adaptation-status: draft`.

   Do **not** touch any other front-matter field; do **not** clobber
   content outside the front-matter without the user's review.

4. Present the full draft to the user before finalising. The user reviews
   and edits; they advance `adaptation-status:` to `adopted` when satisfied.
   Never auto-mark the adaptation complete.

---

## Step 4 — Lifecycle: re-adaptation diff

When `source-sha:` is **already set** in the stub (i.e. this is a re-run
on an already-adapted project):

1. After fetching the new upstream HEAD (Step 2A), compare it against the
   previously recorded SHA:

   ```bash
   git -C .design-language-source diff <recorded-sha>..HEAD
   ```

2. Present the per-file diff to the user for **selective application**.
   Do **not** rewrite `docs/design/ux/design-language.md`. The user decides
   which upstream changes are worth reflecting in the adaptation.

3. Update `source-sha:` to the new HEAD **only after the user approves the
   new state** — whether they applied some changes, all changes, or
   consciously declined all of them. Bumping the SHA means "I have reviewed
   up to this point"; the next re-run will diff only against the new baseline.

4. **Same SHA** (new HEAD == `source-sha:`) → no-op. Report:
   "Already up to date with `<source-url>@<sha>`." No file edits.

5. **Missing `source-sha:`** → treat as initial adaptation (first run or
   strict-local → upstream switch). Proceed through Step 3 as if no prior
   adaptation exists.

**Idempotency rule.** Re-running this skill with no upstream change produces
no file edits. Re-running with an upstream change always produces review
material — never a silent overwrite.

---

## Step 3.5 — Emit / refresh the theme + components + layout tiers (v1.18+)

After producing or reviewing the prose adaptation (Step 3 / Step 4), this
skill also emits **structured tier files** as drafts:

```
docs/design/ux/
  design-language.md   # unchanged authority; gains a "### Theme & components" link subsection
  theme.md             # NEW — token tier: colour, spacing, type, radius, motion
  components.md        # NEW — component tier: named recipes referencing theme tokens
  layout.md            # NEW (web/GUI only) — layout/app-shell tier: app-shell + page recipes by reference to Aura
```

These files are additive: a project that never populates them keeps a valid
single-file `design-language.md`. Their absence is not an error. The
`layout.md` tier (3.5-C) is emitted only for **web/GUI** stacks; its absence on
a non-web stack is likewise not an error.

### 3.5-A — Emit `docs/design/ux/theme.md` as a draft

1. Map the upstream Aura token scales (colour, spacing, type, radius, motion)
   to the project's `token-syntax` (seeded by GUI-framework detection in
   `workflow-bootstrap` Step 3 Q9; defaults to `css-custom-prop` for web
   projects). The supported `token-syntax` values are:

   | Value | Stack |
   |---|---|
   | `css-custom-prop` | Web (React, Vue, Svelte, Angular, SolidJS, …) |
   | `swift-asset` | SwiftUI / UIKit (Apple) |
   | `android-res` | Android (Kotlin / Java) |
   | `flutter-theme` | Flutter |
   | `tui-style` | Terminal UI (TUI) |

2. Write the draft using the canonical schema (YAML block under a stable
   `## Token block` heading so it is trivially parseable):

   ```yaml
   theme:
     meta:
       stack: "React (web)"           # seeded by detection, confirmed by user
       token-syntax: css-custom-prop  # see table above
     color:
       accent:   { value: "#TODO", role: "primary action" }
       surface:  { value: "#TODO", role: "card / panel background" }
       text:     { value: "#TODO", role: "default body text" }
       error:    { value: "#TODO", role: "error palette base" }
       warning:  { value: "#TODO", role: "warning palette base" }
     spacing:
       unit: 4                        # base step (px / pt / dp per stack)
       scale: [0, 4, 8, 12, 16, 24, 32, 48]
     type:
       family: { sans: "TODO", mono: "TODO" }
       scale:  [12, 14, 16, 20, 24, 32]
       weight: { regular: 400, medium: 500, bold: 700 }
     radius:
       scale: [0, 4, 8, 12, 9999]    # last entry = pill/full
     motion:
       duration: { fast: 120, base: 200, slow: 320 }  # ms
       easing:   { standard: "cubic-bezier(0.2,0,0,1)" }
   ```

   Rules enforced when writing:
   - Every token has a **name** and a **value**; colour tokens carry a `role`
     string describing intent (not just a hex literal).
   - Scales (spacing, type, radius, motion) are **ordered lists or named maps**
     — never standalone magic numbers.
   - Stacks that have no concept of a given tier (e.g. a TUI has colour +
     maybe type, but no radius/motion) populate only the applicable keys and
     note omissions inline (`# N/A for TUI`).

3. Set `adaptation-status: draft` in the file's own YAML front-matter (same
   lifecycle as `design-language.md`). Do **not** record a separate
   `source-sha:` in `theme.md` — it derives from the same upstream SHA already
   recorded in `design-language.md`.

4. Present the draft to the user. The user reviews values and advances
   `adaptation-status: adopted` when satisfied. **Never auto-adopt.**

### 3.5-B — Emit `docs/design/ux/components.md` as a draft

1. Map the upstream Aura control taxonomy to named component recipes. Each
   recipe must reference `theme.*` token paths — **never raw values**. This is
   the layer's core invariant: re-theme by editing `theme.md` only; component
   recipes are stable across themes.

2. Write the draft using the canonical schema (YAML block under `## Component
   block`):

   ```yaml
   components:
     primary-button:
       maps-to: "TODO: project-native control"  # e.g. "MUI <Button variant=contained>"
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
       intent:  "surface a recoverable error"
       tokens:
         background: theme.color.error
         text:       theme.color.surface
         radius:     theme.radius.scale[2]
       a11y: "role=alert; not conveyed by colour alone (icon + text)"
   ```

   Schema rules:
   - Each entry carries: **`maps-to`** (the project-native control the
     `ux-demo` must use — set to `TODO` when not yet known), **`intent`**,
     a **`tokens`** map referencing `theme.*` paths, optional **`states`**,
     and an **`a11y`** note.
   - All visual properties resolve through a `theme.*` reference or a
     documented transform (e.g. `darken(token, n%)`). Raw hex/px literals
     are a hard violation of the no-raw-values invariant.

3. Set `adaptation-status: draft` in the file's front-matter. Present the
   draft to the user for review and `maps-to` completion. **Never auto-adopt.**

### 3.5-C — Emit `docs/design/ux/layout.md` as a draft (web/GUI only)

This tier captures the **app-shell + page/layout recipes** for the project. It
is emitted only for **web/GUI** stacks (a CLI/library/service stack has no
app-shell, so its absence is **not an error** — mirror the stack-applicability
note on the token tier in 3.5-A). Like the other tiers it is a draft with the
same lifecycle, and it does **not** record its own `source-sha:` (see step 3).

1. Capture the app-shell and page/layout recipes **by reference** to Aura's
   ready-made page artifacts and the default adoption paths documented in
   `docs/design/web-app-aura-adoption-design.md` (§ Default paths / layout):

   | Default path | Role |
   |---|---|
   | `vendor/aura/` | Aura artifacts, consumed as a **`vendor.toml` dep** via `sync-deps` (default consumption mechanism — channel `stable`, kind `asset-bundle`, `dest = vendor/aura`; the committed bundle lands here). Git **submodule** and `static/aura/` + build step are the recorded alternatives. |
   | `vendor/aura/SRI.txt` | Additional browser-defense asset — per-file subresource-integrity hashes (`<relpath> sha384-<base64>`) consumed by `base.html` `<link>`/`<script>` `integrity=` attributes. Not the dependency pin (that is `vendor.toml`'s channel/version). |
   | `templates/base.html` | base shell template; binds Aura's app-shell. |
   | `templates/pages/` | full-page templates (one per route). |
   | `templates/fragments/` | HTMX partials / fragments. |

   **Copy NO upstream code.** Like `components.md`, this tier is
   reference/recipe only — point at Aura's artifacts at their default path and
   describe the binding; never paste Aura's HTML/CSS into `layout.md`. Where an
   Aura artifact is not yet published, the recipe records the dependency and
   points at its intended location.

2. Write the draft using the canonical schema (YAML block under a stable
   `## Layout block` heading so it is trivially parseable):

   ```yaml
   layout:
     meta:
       stack: "Flask + HTMX (web)"     # seeded by detection, confirmed by user
       consumption: vendor-dep         # vendor-dep (default) | submodule | vendored-build
       aura-path: vendor/aura          # invariant for vendor-dep/submodule; static/aura for the vendored-build variant
     app-shell:
       maps-to: "Aura app-shell artifact (design-language#26)"  # by reference; not copied
       binds-in: templates/base.html   # base shell template that includes the app-shell
       regions:  [header, nav, main, footer]
       theme-toggle: "Aura theme-toggle snippet (referenced, not copied)"
       a11y: "landmark roles on regions; skip-to-main link; visible focus"
     pages:
       dir: templates/pages            # one full-page template per route
       recipe: "extends base.html; fills the main region; references Aura page artifacts"
     fragments:
       dir: templates/fragments        # HTMX partials returned for in-page swaps
       recipe: "partial templates rendered for hx-* swaps; no full-shell wrapper"
   ```

   Schema rules:
   - `app-shell.maps-to` / `pages.recipe` reference Aura artifacts **by name /
     intended location** — never inline copied markup.
   - `consumption` is one of `vendor-dep` (default), `submodule`, or
     `vendored-build`. The default — **`vendor-dep`** — declares Aura as a
     `vendor.toml` dependency (channel `stable`, kind `asset-bundle`,
     `dest = vendor/aura`) synced via `sync-deps`; the channel/version pin is the
     reproducibility anchor the submodule commit previously provided. `submodule`
     (git submodule at `vendor/aura`) and `vendored-build` (`static/aura` + build
     step) are recorded alternatives. `aura-path` is `vendor/aura` for both
     `vendor-dep` and `submodule` (**invariant** — `base.html` and SRI paths must
     not move) and `static/aura` for `vendored-build`.
   - Page and fragment entries name the **default directory** and the binding
     recipe, not concrete per-route content.

3. Set `adaptation-status: draft` in the file's own YAML front-matter (same
   lifecycle as `design-language.md`). Do **not** record a separate
   `source-sha:` in `layout.md` — it derives from the same upstream SHA already
   recorded in `design-language.md` (mirroring 3.5-A's note).

4. Present the draft to the user. The user reviews the recipes + paths and
   advances `adaptation-status: adopted` when satisfied. **Never auto-adopt.**

### 3.5-D — Update `design-language.md` to cross-link the tiers

After writing `theme.md`, `components.md`, and (for web/GUI stacks) `layout.md`,
add or update the `### Theme & components` subsection inside `design-language.md`
(insert it after the existing `### Component map` section or replace it if
already present):

```markdown
### Theme & components

This project's structured token, component, and layout tiers live in companion files:

- [`theme.md`](theme.md) — design token scales (colour, spacing, type, radius, motion).
  Status: `draft` / `adopted` (see the file's front-matter).
- [`components.md`](components.md) — named component recipes referencing theme tokens.
  Status: `draft` / `adopted` (see the file's front-matter).
- [`layout.md`](layout.md) — layout/app-shell + page recipes referencing Aura's
  page artifacts (web/GUI stacks only; absent on non-web stacks).
  Status: `draft` / `adopted` (see the file's front-matter).

The prose adaptation above remains the human-readable authority. The tiers
are machine-addressable companions: `ux-demo-build` reads `components.md` for
which controls to build and `theme.md` for the values to apply, and `layout.md`
for the app-shell + page structure to bind. Edit `theme.md` to change token
values; component recipes in `components.md` reference tokens by path and update
automatically.
```

If no tier file exists yet (skip case: project opted out or first-time
run with `source: local` and no upstream), insert a placeholder note instead.
On a non-web stack, omit the `layout.md` bullet — its absence is expected, not
a gap:

```markdown
### Theme & components

*Not yet populated.* Run `design-language-adapt` with an upstream source to
generate `theme.md` and `components.md` drafts (plus `layout.md` for web/GUI
stacks), or create them manually following the schema in
`docs/design/ux/theme.md`, `docs/design/ux/components.md`, and
`docs/design/ux/layout.md`.
```

### 3.5-E — Create / maintain `docs/design/ux/README.md` (UX tier index)

After writing or refreshing the tier files (`theme.md`, `components.md`,
`design-language.md`, and `layout.md` when applicable), create or update
`docs/design/ux/README.md` as the UX tier index. It must:

1. Open with an up-link to the parent design index:
   ```markdown
   > **Up:** [↑ Design index](../README.md)
   ```
2. List all three standard UX docs (plus `layout.md` for web/GUI stacks) with
   relative links and one-line descriptions, for example:
   ```markdown
   # UX Design Language

   > **Up:** [↑ Design index](../README.md)

   | Document | Description |
   |---|---|
   | [design-language.md](design-language.md) | Human-readable adaptation authority |
   | [theme.md](theme.md) | Design token scales (colour, spacing, type, radius, motion) |
   | [components.md](components.md) | Named component recipes referencing theme tokens |
   | [layout.md](layout.md) | App-shell + page recipes (web/GUI stacks only) |
   ```
   Omit the `layout.md` row if the project is not a web/GUI stack.

If `docs/design/ux/README.md` already exists, update the file listing to
reflect current tier files rather than overwriting the whole file. Preserve any
curated content already present in the file.

### 3.5-F — Up-links in generated UX tier files

Each of the four UX tier files must open with a breadcrumb immediately after
its heading. When creating or refreshing these files, ensure this line is
present (using the exact relative path to the UX index):

- **`design-language.md`**: `> **Up:** [↑ UX design language](README.md)`
- **`theme.md`**: `> **Up:** [↑ UX design language](README.md)`
- **`components.md`**: `> **Up:** [↑ UX design language](README.md)`
- **`layout.md`** (web/GUI only): `> **Up:** [↑ UX design language](README.md)`

On a re-run, if any of these files lack the breadcrumb, add it immediately
after the `#` heading before presenting the diff for user review.

### 3.5-G — Re-adaptation diff (re-runs)

When `theme.md`, `components.md`, and/or `layout.md` already exist on a re-run:

- The same selective-diff rule from Step 4 applies: **present proposed
  changes** to the existing tiers; never silent-clobber.
- `adaptation-status` is reset to `draft` on the changed file(s) only; files
  with no upstream-driven changes keep their current status.
- Do **not** track a separate SHA for the tier files — they derive from the
  same `source-sha:` in `design-language.md`.

---

## Step 5 — Optional: hand off to ux-demo-build

After completing Steps 3–4, **ask** the user:

> "The design language adaptation is ready for review. Would you like to
> invoke `ux-demo-build` now to rebuild the demo against the updated
> adaptation?"

- If yes → invoke the **`ux-demo-build`** skill.
- If no → stop here. The user can invoke `ux-demo-build` manually later.

**Never auto-trigger `ux-demo-build`.** It is always opt-in, user-initiated.

---

## Anti-patterns

- **Silent clobber on re-run.** Never rewrite `docs/design/ux/design-language.md`
  without presenting a diff and getting user acknowledgment. Idempotency
  means review material, not auto-overwrite.
- **Committing `.design-language-source/`.** The landing directory is
  gitignored and local-only. Never stage or commit anything inside it.
- **Auto-retrying clones.** A failed clone reports the error and exits.
  No retry loop. No silent fallback to a stale clone after a network error
  that wasn't an unavailability (e.g. auth failure, 404).
- **Auto-marking `adaptation-status: adopted`.** The skill may set
  `adaptation-status: draft` or, at most, `ready-for-review` when the draft
  is clean — never `adopted`. Only the user marks the adaptation adopted.
- **Recording `source-sha:` before user review.** SHA is recorded in Step 2A
  for initial runs; on re-runs it is only updated after the user has approved
  the new state (Step 4). Never bump the SHA to skip an unwanted diff.
- **Invoking `ux-demo-build` without user consent.** Always ask; never
  auto-trigger.
- **Modifying `source-pin:` during adaptation.** The field is user-controlled
  input; the skill reads it but never writes it. Only `source-sha:` is written
  by the skill.
- **Silently falling back to HEAD when `source-pin:` checkout fails.** If the
  pinned SHA is not reachable, fail closed with a clear message. Never silently
  adapt from a different commit than the user requested.
- **Cloning from an off-allowlist URL without confirmation.** Always warn and
  ask the user to confirm before cloning from a host not in the allowlist.
  Never auto-proceed on an unrecognised URL.
- **Raw values in `components.md`.** Every visual property in a component
  recipe must resolve through a `theme.*` path or a documented transform.
  Writing a raw hex literal, pixel value, or magic number directly into
  `components.md` violates the no-raw-values invariant.
- **Auto-adopting tier files.** The skill sets `adaptation-status: draft` on
  `theme.md`, `components.md`, and `layout.md`; only the user advances them to
  `adopted`. Never auto-mark any tier complete.
- **Tracking a separate `source-sha:` in tier files.** All tier files
  (`theme.md`, `components.md`, `layout.md`) derive from the same upstream SHA
  already recorded in `design-language.md`. Do not duplicate or diverge the
  SHA tracking.
- **Silent clobber of existing tier files on re-run.** Apply the same
  selective-diff + user-acknowledgment rule to `theme.md`, `components.md`, and
  `layout.md` as to `design-language.md`. Present proposed changes; never
  overwrite silently.
- **Copying Aura code into `layout.md`.** The layout tier is reference/recipe
  only — point at Aura's app-shell / page artifacts at their default path and
  describe the binding. Pasting upstream HTML/CSS into `layout.md` violates the
  same no-copy prohibition that governs the rest of the skill.
- **Emitting `layout.md` for a non-web stack.** The layout/app-shell tier
  applies only to web/GUI stacks; on a CLI/library/service stack its absence is
  expected, not a gap. Do not synthesize an app-shell where the stack has none.
