---
name: grm-design-language-adapt
description: Adopt the UX design language for this project — pull the upstream design-language repo (or honor strict-local mode), produce a project-specific adaptation in docs/design/ux/design-language.md, and record the source commit SHA for idempotent re-runs. Use when initializing the project's UX layer, when the upstream design language changed, or when switching between upstream and strict-local mode.
---

# Design-language-adapt

Adapts an upstream design language into this project's own
`docs/design/ux/design-language.md`. Handles initial adaptation and
re-adaptation after upstream changes, with full offline-fallback semantics
and idempotency guarantees.

---

## Source of truth

When reading the framework's own source artifacts as adaptation input, prefer
clean copies in `.grimoire-source/` at the repo **root** if present (decoupled
from in-progress edits to the live tree); otherwise fall back to the live tree
and warn: `[warn] .grimoire-source/ not found — reading from live tree; run
workflow-bootstrap to populate it.` The upstream design-language repo (cloned
to `.design-language-source/`) is a separate concern, handled by Step 2A.

---

## When to use this skill

First-time UX setup (stub exists, `source-sha:` empty); upstream changed and you
want to pull changes selectively; switching `source: local` → `upstream`; or
verifying the local adaptation matches the recorded SHA. Do **not** use it to
hand-edit the adaptation — edit `docs/design/ux/design-language.md` directly and
leave `source: local` for no upstream coupling.

---

## Step 0 — Integration-line pre-flight (BMI-3)

Upstream mode only. Rules 3a/3b/3c: `reference.md §Step 0`.

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

`source-url:` defaults to the **CONFIRM-pending** Aura URL seeded by
`grm-workflow-bootstrap` — verify or fork it before the first run (v1.13 seeding
detail: `reference.md`). `source-pin:` is an **input** (set ⇒ check out that
commit instead of HEAD; empty ⇒ track HEAD); `source-sha:` is the **output** this
skill writes — the commit actually used, equal to `source-pin:` when pinned.

- If `source: upstream` → proceed to **Step 1.5**.
- If `source: local` → proceed to **Step 2B**.
- If the stub does not yet exist, stop and run the **`grm-repo-init`** skill
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

   - **First run (directory absent):** `git clone <source-url>
     .design-language-source/` — add `--depth=1` when `source-pin:` is unset,
     omit it (full clone) when pinned.
   - **Re-run (directory exists):** never re-clone — `git -C
     .design-language-source fetch` then `reset --hard origin/HEAD`; if the clone
     is shallow and `source-pin:` is now set, `fetch --unshallow` first.

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

4. **Offline fallback.** If the network check reports network unavailable: use an
   existing non-empty `.design-language-source/` as-is (report it and its SHA;
   apply `source-pin:` to the local copy first if set), then continue to Step 3;
   if absent/empty, **fail closed**. Never auto-retry. (Messages: `reference.md`.)
5. **Record the SHA.** After a successful clone / fetch / pin checkout / offline
   re-use, capture `git -C .design-language-source rev-parse HEAD` and write it
   into the stub's `source-sha:` field — the **single source of truth** for the
   upstream commit this adaptation derives from (it lives nowhere else). When
   `source-pin:` is set, `source-sha:` equals the pinned SHA.
6. **Never source-control `.design-language-source/`.** Confirm the repo-root
   `.gitignore` lists it; append the entry only if genuinely missing (don't
   commit `.gitignore` otherwise).

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
  - Asking the user whether to invoke `grm-ux-demo-build` (see Step 5).
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

## Step 5 — Optional: hand off to ux-demo-build

After completing Steps 3–4, **ask** the user:

> "The design language adaptation is ready for review. Would you like to
> invoke `grm-ux-demo-build` now to rebuild the demo against the updated
> adaptation?"

- If yes → invoke the **`grm-ux-demo-build`** skill.
- If no → stop here. The user can invoke `grm-ux-demo-build` manually later.

**Never auto-trigger `grm-ux-demo-build`.** It is always opt-in, user-initiated.

---

## Reference (load on demand)

- `Step 0 detail — Integration-line + release-boundary pre-flight (BMI-3)` — see `reference.md`
- `Step 3.5 — Emit / refresh the theme + components + layout tiers (v1.18+)` — see `reference.md`
- `3.5-A — Emit `docs/design/ux/theme.md` as a draft` — see `reference.md`
- `3.5-B — Emit `docs/design/ux/components.md` as a draft` — see `reference.md`
- `3.5-C — Emit `docs/design/ux/layout.md` as a draft (web/GUI only)` — see `reference.md`
- `3.5-D — Update `design-language.md` to cross-link the tiers` — see `reference.md`
- `Theme & components` — see `reference.md`
- `3.5-E — Create / maintain `docs/design/ux/README.md` (UX tier index)` — see `reference.md`
- `3.5-F — Up-links in generated UX tier files` — see `reference.md`
- `3.5-G — Re-adaptation diff (re-runs)` — see `reference.md`
- `Anti-patterns` — see `reference.md`
- `Step 4 — Lifecycle: re-adaptation diff` — see `reference.md`
