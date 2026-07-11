# Workflow-bootstrap — reference
Loaded on demand by `SKILL.md`.

## When to use this skill

- **New project**: scaffolding copied in, nothing customised yet.
- **Existing repo onboarding**: skills present but full of placeholders.
- **Repair**: a required skill or hook was deleted or corrupted.

Do **not** use it to push local edits into a committed `golden/` tree — there
is none since v3.49; the golden baseline is generated on demand by
`generate_golden.py` from the live/flavor files.

---

## Anti-patterns

- Overwriting a customised skill without asking — always diff and confirm.
- Substituting runtime template tokens (`{feature}`, `{model}`, …) — only
  the `manifest.md` project-config tokens are interview-fillable.
- Re-asking what's already in the repo — detect and confirm instead.
- Fabricating a `CLAUDE.md` or design docs to "complete" patching — report
  the gap and defer to the scaffolding README / `grm-source-to-design-docs`.
- Treating `golden/` as authoritative over a user's deliberate edits — it
  is a restore baseline, not a style enforcer.
- Committing. This skill only reads, copies, and edits; the user commits.
### Grimoire Framework URL (`.scaffold-upstream.conf`)

1. Check whether `.scaffold-upstream.conf` exists at the project root.
2. If absent → copy `golden/.scaffold-upstream.conf` into place (as part of
   the Step 2 restore). The file is already in the golden manifest; this step
   is a no-op if the restore already wrote it.
3. If present → read `UPSTREAM_REPO`. If it is non-empty → **no-op** (preserve
   the existing value; forks that point at their own upstream must not be
   overwritten). If empty or absent → set `UPSTREAM_REPO` to the default:

   ```
   UPSTREAM_REPO=https://github.com/rhohn94/grimoire-framework.git
   UPSTREAM_REF=main
   ```

   Add the lines in place; do not rewrite the rest of the file. (The golden
   `.scaffold-upstream.conf` already carries this exact value — step 2 normally
   covers it; this is the explicit fallback if the file exists but is empty.)

**Default Grimoire URL:** `https://github.com/rhohn94/grimoire-framework.git`
(ref `main`). The legacy `agentic-scaffolding.git` name is auto-detected and
repointed by `grm-sync-from-upstream` (the v1.22 rename-migration note).

Fork override: a project (or org fork) with its own upstream sets
`UPSTREAM_REPO` in `.scaffold-upstream.conf`. The idempotency check ensures
the fork's value is never overwritten by a subsequent `grm-workflow-bootstrap` run.

### Aura design language URL (`docs/design/ux/design-language.md`)

For GUI projects only (Step 3 answer = "Yes"):

1. When the golden `docs/design/ux/design-language.md` stub is written (new
   project) or already present — confirm `source-url:` in the front-matter is
   set to the Aura default:

   ```
   source-url: https://github.com/rhohn94/design-language
   ```

   > **NOTE (CONFIRM-pending):** `https://github.com/rhohn94/design-language`
   > is a placeholder. Confirm the canonical Aura repo URL with the project owner
   > before the first `grm-design-language-adapt` run. If you know the correct URL
   > at bootstrap time, set `source-url:` to it now.

2. If `source-url:` is already non-empty in the live file → **no-op** (preserve
   the existing value; projects that override the default retain their URL).
3. If empty → write the default.

For GUI-absent and headless projects: skip this sub-step entirely.

---

## Step 2 — Restore missing / confirmed files

- **MISSING** → copy the golden file into place
  (`.claude/skills/<name>/SKILL.md`, `.claude/hooks/<file>`,
  `.claude/settings.json`, `.claude/push-allowlist`,
  `.claude/workflows/<name>.js`). Create parent dirs as needed.
- **DRIFTED** → show the user a diff summary and ask whether to (a) keep
  their version, (b) overwrite from golden, or (c) keep theirs but
  re-apply only the interview placeholders. Never silently overwrite a
  customised skill.
- **settings.json** → if a live one exists and differs, do **not**
  clobber it. Show the hook-wiring block from golden and ask the user to
  merge, or merge it yourself if the live file has no conflicting keys.
  The golden `settings.json` also ships a scoped **`permissions.allow`**
  allowlist so the framework's own maintenance scripts run unattended
  without the auto-mode classifier re-prompting on every sync — **path-scoped
  to framework scripts only, never a blanket grant**:

  ```jsonc
  "permissions": {
    "allow": [
      "Bash($CLAUDE_PROJECT_DIR/.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh:*)",
      "Bash(.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh:*)",
      "Bash($CLAUDE_PROJECT_DIR/.claude/skills/grm-sync-from-source/sync-from-source.sh:*)",
      "Bash(.claude/skills/grm-sync-from-source/sync-from-source.sh:*)",
      "Write(.scaffold-upstream.conf)"
    ]
  }
  ```

  Merge these `allow` entries into the live `permissions.allow` (creating the
  block if absent); never widen beyond these framework-owned paths. The guard
  hooks remain the safety net — pre-authorizing the *already non-destructive*
  sync scripts (dry-run default, 3-way merge, backups, refuses `--apply` on a
  dirty tree) does not widen blast radius, it only stops re-prompting on a
  reversible operation. Granting these entries is a **user-confirmed** action
  (it edits `settings.json`); present the block and apply on approval.
- **PRISTINE / CUSTOMISED** → no copy.

- **workflows** → `.claude/workflows/<name>.js` is a Claude-Code-only
  artifact class (no Copilot equivalent) and read-only by convention, so
  it carries no interview placeholders — treat it like an infrastructure
  file: restore when MISSING, ask before overwriting when DRIFTED. See
  `docs/grimoire/design/release-planning-workflow-design.md` for the path
  convention and read-only safety contract.

- **paradigm content sets** → when `--restore` is requested (repair
  scenario), copy all files from `golden/paradigms/{supervised,weiss,noir}/`
  to `.claude/paradigms/{supervised,weiss,noir}/`. Then call
  `grm-work-paradigm-switch` (with no argument) to re-install the active
  paradigm's content into its stable active paths. The switch skill reads
  `work-paradigm.value` from `.claude/grimoire-config.json`; if the config
  is missing or the field is unset it defaults to `Supervised`. This step
  runs after all skill/hook restores so the content sets are in place
  before the switch skill needs them.

Restoration is a file copy only — no git operations, no commits.

---

## Step 2.8 — Seed the dependency channel (idempotent, never-clobber)

Grimoire prescribes a uniform, release-channel-sourced **vendored** dependency
mechanism (v3.29 "Dependency Channel"): a published GitHub Release is the only
source for a first-party dependency, required deps are committed under
`vendor/<dep>/` so builds are offline, and the network is touched only at
*sync* time. Every new app seeds the three entry points by default. Design:
`docs/grimoire/design/dependency-channel-design.md` §6.

1. **`vendor.toml`** at the project root — the human-authored intent file. Write
   it **only if MISSING**, copied from `golden/vendor.toml` (a commented stub:
   `schema_version = 1`, no active deps, an example `[deps.aura]` block showing
   every field). **Never clobber a present `vendor.toml`** — a non-empty live
   file carries the project's real dep declarations (the
   `.scaffold-upstream.conf` no-silent-clobber rule). If absent, restore the
   golden stub.
2. **`vendor.lock`** at the project root — the auto-generated resolved truth
   (JSON, do-not-hand-edit). Create it **programmatically only if absent** as the
   empty seed (it is *not* a bundled golden file — an empty golden file would
   trip the PRISTINE classification). Write exactly the canonical serialization
   the `grm-sync-deps` engine (`VendorLock.serialize`) emits — sorted keys, two-space
   indent, trailing newline:

   ```json
   {
     "deps": {},
     "schema_version": 1
   }
   ```

   If `vendor.lock` already exists (even empty), leave it untouched.
3. **`recipes.json`** — **ensure** the `grm-sync-deps` + `vendor-check` targets are
   present (read-merge-write; never clobber an existing binding). These ride the
   `recipe.py` `INTERFACE` (`INTERFACE_VERSION 3`, v3.29). If a target is absent,
   add it as an unimplemented stub (`{"command": null, "implemented": false}`)
   bound when the project wires the engine; if present, leave it as-is. If
   `recipes.json` itself is absent, the recipe-stubbing step that creates it
   already includes these verbs from the `INTERFACE` — this step is then a no-op
   confirmation.
4. **`.gitignore`** — **ensure** the line `.sync-deps-staging/` is present (the
   transient fixed staging dir the `grm-sync-deps` engine creates and cleans per run;
   ignored as a safety net against an interrupted run). Read-merge-write; if the
   line is already present, no-op. (`vendor/<dep>/` is **committed**, not
   ignored — that is the core "fully offline" principle; `dist/` stays ignored as
   producer output.)

**Idempotency contract:** re-running this step on an initialized app is a
**no-op** — a customized `vendor.toml` is never overwritten, an existing
`vendor.lock` is never rewritten, present recipe verbs are left untouched, and
an already-present ignore line is not duplicated.

**On `--restore`:** re-run unconditionally; every sub-step is guarded
(missing-only / ensure-present), so the restore never clobbers live dependency
declarations.

---

## Step 2.8.1 — Seed the dependency-channel PRODUCER intent (library stacks only)

Step 2.8 seeds the **consumer** side (`vendor.toml` — what this repo vendors). A
**library**-stack project is also a channel **producer**: the crate other repos
vendor. Seed its producer intent so it can publish itself as a `vendored-crate`
artifact without hand-rolling packaging bash. Design:
`docs/grimoire/design/dependency-channel-design.md` §2b. **Run this sub-step only
when the inferred/confirmed stack is `library`** (a `server`/`cli`/`web` project
publishes app distributables via `package`, not a crate; skip it there).

1. **`publish.toml`** at the project root — the human-authored producer intent
   (crate `name`, `artifact_kind = "vendored-crate"`, `channel`, and the `include`
   glob subset the artifact ships). Write it **only if MISSING**, copied from
   `golden/publish.toml` (a commented stub with `name = "REPLACE-ME-crate-name"`
   and the conservative default `include`). **Never clobber a present
   `publish.toml`** — a live file carries the project's real publish declaration
   (the same no-silent-clobber rule as `vendor.toml`).
2. **`recipes.json`** — **ensure** the `package` target is bound to the crate
   builder for a library crate (read-merge-write; never clobber an existing
   binding). Bind it to:

   ```json
   "package": {
     "command": "python3 .claude/skills/grm-project-release/build_crate_artifact.py --version ${version}",
     "implemented": true,
     "params": { "version": { "default": "" } }
   }
   ```

   `build_crate_artifact.py` reads `publish.toml`, emits the trio
   (`<name>-v{ver}.tar.gz` + `release.json` + `SHA256SUMS`) into `dist/`, and the
   release ceremony uploads it. If `package` is already implemented, leave it as
   is (the project may publish differently).

**Idempotency contract:** re-running is a **no-op** — a customized `publish.toml`
is never overwritten and an implemented `package` binding is left untouched.

**On `--restore`:** re-run unconditionally; both sub-steps are missing-only /
ensure-present guarded, so the restore never clobbers a live publish declaration.

---

## Step 2.9 — Seed architecture-fitness rules (idempotent, never-clobber, #314)

After the dependency-channel seeding (Step 2.8), ensure the project has an
architecture-fitness ruleset so `grm-architecture-audit` has something to
enforce from day one (an absent ruleset is a visible WARN in the audit, never
a silent pass):

1. **Template-scaffolded projects** — if the project was (or will be) scaffolded
   via `grm-quick-start-template`, the per-family starter
   (`.claude/quick-start-templates/<family>/files/.claude/architecture-rules.json`)
   lands with the template's `scaffold` mapping; this step is then a no-op
   confirmation.
2. **Non-template projects** — if `.claude/architecture-rules.json` is MISSING,
   copy `.claude/architecture-rules.example.json` to
   `.claude/architecture-rules.json` and adapt it minimally: keep the
   `structure` block as-is (it already encodes `docs/project-structure.md`) and
   trim the example `layers`/`allowed-edges` to globs that actually match the
   project's tree (delete layers that match nothing rather than leaving dead
   globs). Flag the file in the Step 5 report as seeded-needs-review.
3. **Explicit opt-out** — if the user declines rules for this project, write
   `{"schema-version": 1, "opt_out": true, "opt_out-reason": "<their reason>"}`
   instead, so the decision is tracked and surfaced (never a silent absence).

**Never-clobber rule:** a present `.claude/architecture-rules.json` (rules or
opt-out) is project-owned — leave it untouched, including on `--restore`.

---

## Step 3 — Guided interview

Ask the project-config questions with `AskUserQuestion`. Skip any whose
answer is already evident (e.g. command is in CLAUDE.md, version file
obvious from repo ecosystem) — confirm rather than re-ask. Batch related
questions; offer a sensible default as the first option where one exists.

1. **Test command** — how the full suite runs (`npm test`, `pytest`,
   `cargo test`, `go test ./...`, …).
2. **Build command** — release build (`npm run build`, `make`, `cargo
   build --release`, …).
3. **Release command** — the one-shot release recipe (`npm version
   minor && npm publish`, `just release`, `make release VERSION=…`).
3a. **Quality commands (optional, v1.26 merge gate)** — type-check
   (`mypy`, `tsc --noEmit`, `cargo check`, `go vet`), lint
   (`ruff`, `eslint`, `clippy`), and coverage (`pytest --cov`, …). Each is
   optional; a blank answer leaves that gate off. Captured into the
   `code-quality` block of `.claude/grimoire-config.json` (defaults
   `audit-gate: warn`, `auto-reviewer: noir`, `coverage-threshold: null`,
   `typecheck: build`). See `docs/grimoire/design/merge-gate-quality-design.md`.
3b. **Justfile build command (optional, v3.53)** — the command that builds or
   packages this project for the Justfile `build` recipe. Prompt: *"What
   command builds/packages this project? (e.g. `npm run build`, `cargo build`,
   `python -m build`) [leave blank to keep Justfile placeholder]"* A blank
   answer leaves the Justfile recipe body as a `# grimoire:placeholder` stub.
   Stored as `commands.build` in `.claude/grimoire-config.json` (string value
   or `null` if blank). **Justfile skip rule:** if the project already has a
   `justfile` / `Justfile` and the relevant recipe body does NOT contain
   `# grimoire:placeholder`, note "existing non-placeholder recipe preserved"
   and skip overwriting it.
3c. **Justfile run command (optional, v3.53)** — the command that starts the
   application for the Justfile `run` recipe. Prompt: *"What command starts
   the application? (e.g. `uvicorn main:app --port 8080`, `npm run dev`)
   [leave blank to keep Justfile placeholder]"* A blank answer leaves the
   Justfile recipe body as a `# grimoire:placeholder` stub. Stored as
   `commands.run` in `.claude/grimoire-config.json` (string value or `null` if
   blank). Apply the same Justfile skip rule as 3b.
3d. **Justfile deploy command (optional, v3.53)** — the command that deploys
   to a live environment for the Justfile `deploy` recipe. Prompt: *"What
   command deploys to a live environment? (e.g. `fly deploy`, `kubectl apply
   -f k8s/`) [leave blank to keep Justfile placeholder]"* A blank answer leaves
   the Justfile recipe body as a `# grimoire:placeholder` stub. Stored as
   `commands.deploy` in `.claude/grimoire-config.json` (string value or `null`
   if blank). Apply the same Justfile skip rule as 3b.
4. **Version file + field** — where the authoritative version lives and
   its format (`package.json` → `"version"`, `Cargo.toml` → `[package]
   version`, `VERSION` file, …).
5. **Doc-location map** — the real paths for architecture / UX / feature
   / versioning docs (rows in `grm-repo-reference`).
6. **Integration branch name** — the staging trunk (default `dev`).
7. **Release branch name** — the production trunk (default `main`).
8. **First roadmap entry** — first version number + one-line theme.
9. **GUI presence** — whether this project has (or will have) a user
   interface.

   **Auto-detection (run before asking).** Scan the repo root (and one
   level of obvious source directories) for GUI-framework signals. The
   scan is **read-only and offline** — no network calls, no file writes.
   Use the results only to pre-select a default and surface evidence;
   the user's answer is always authoritative.

   *Signal table (rows are ordered by precedence — row 1 is strongest):*

   | # | Signal source | Signal | Inferred stack | GUI? |
   |---|---|---|---|---|
   | 1 | Native/mobile dep + extension | `*.swift` + `*.xcodeproj`/`Package.swift` with a UI dep | SwiftUI / UIKit (Apple) | Yes |
   | 2 | Native/mobile dep + extension | `*.kt`/`*.java` + `AndroidManifest.xml` | Android (Kotlin/Java) | Yes |
   | 3 | Native/mobile dep + extension | `Info.plist`, `*.storyboard`, `ios/` + `android/` dirs | native/mobile app shell | Yes |
   | 4 | File extension | `*.xaml` | WPF / WinUI / Avalonia (.NET) | Yes |
   | 5 | File / dep | `pubspec.yaml` with `flutter` | Flutter (cross-platform) | Yes |
   | 6 | Cargo.toml dep | `egui`, `iced`, `tauri`, `slint` | Rust GUI / Tauri | Yes |
   | 7 | Dep / import | `PyQt*`, `PySide*`, `tkinter`, `wxPython`, `kivy` | Python desktop GUI | Yes |
   | 8 | `package.json` deps | `react`, `react-dom` | React (web) | Yes |
   | 9 | `package.json` deps | `react-native`, `expo` | React Native (mobile) | Yes |
   | 10 | `package.json` deps | `vue` | Vue (web) | Yes |
   | 11 | `package.json` deps | `svelte`, `@sveltejs/kit` | Svelte / SvelteKit (web) | Yes |
   | 12 | `package.json` deps | `@angular/core` | Angular (web) | Yes |
   | 13 | `package.json` deps | `solid-js` | SolidJS (web) | Yes |
   | 14 | `package.json` deps | `electron` | Electron (desktop, JS) | Yes |
   | 15 | `package.json` deps | `next`, `nuxt`, `@remix-run/*`, `astro`, `gatsby` | meta-framework over detected base (Next→React, Nuxt→Vue, …) | Yes |
   | 16 | TUI dep | `rich`, `textual`, `blessed`, `bubbletea`, `ratatui` | terminal UI (TUI) | Yes (TUI) |
   | 17 | Config file | `vite.config.*`, `next.config.*`, `nuxt.config.*`, `svelte.config.*`, `angular.json`, `astro.config.*` | confirms/disambiguates the web stack | Yes |
   | 18 | Config file | `tailwind.config.*`, `postcss.config.*` | web styling (corroborating, not deciding) | (boost web) |
   | 19 | Server-only deps, no view layer | `express`/`fastify`/`flask`/`gin` with **no** rows 1–18 hit | likely headless service | Lean "No, headless" |
   | 20 | Library manifest, no app entry | published-package shape, no UI dep | likely headless library | Lean "Not yet" / "No" |

   *Precedence (deterministic, highest wins):*

   1. **Explicit native/mobile + framework dep** (rows 1–3) — strongest;
      names a concrete platform.
   2. **Declared runtime dep in a manifest** (rows 4–16) — a dependency
      the project chose to install.
   3. **Config-file presence** (rows 17–18) — corroborates/disambiguates
      a manifest hit; a lone config file with no dep is a weak signal.
   4. **File-extension census** — used to disambiguate between multiple
      manifest hits or when no manifest exists.
   5. **Negative/headless leans** (rows 19–20) — applied only when **no**
      positive GUI signal (rows 1–18) fired.

   Meta-frameworks (row 15) resolve their base via the underlying dep
   (Next ⇒ React, Nuxt ⇒ Vue) and report the meta-framework as the
   stack hint. When two peer web frameworks both appear (e.g. a monorepo),
   report the highest-confidence single guess and list the runner-up so
   the user can choose.

   The **stack-hint** yielded by detection is written verbatim into the
   `{ux-demo-stack}` slot (e.g. "React (web)", "SwiftUI", "Textual
   (TUI)") so `grm-ux-demo-build` can produce a stack-pure demo.

   *Confidence levels (changes presentation only — never removes the
   requirement to confirm):*

   - **High** (a framework dep + corroborating config or extensions):
     pre-select "Yes", pre-fill the stack hint. Phrase the prompt as
     *"Detected a React (web) UI — confirm or change."*
   - **Medium** (a single weak signal, e.g. a lone config file):
     pre-select the leaning option but phrase as a question; surface the
     evidence found.
   - **Low / none** (no signal, or conflicting peers): ask the cold
     question below with no pre-selection; offer the runner-up list if
     peers conflicted.

   Hard rules:
   - Detection **pre-fills the default and surfaces its evidence**;
     it never skips Q9 or auto-commits an answer.
   - Detection **never writes a file** — it only feeds Q9. All file
     changes flow through the existing Step 4 patch table unchanged.
   - When detection leans headless/deferred (rows 19–20) it still routes
     through the normal "Not yet" / "No, headless" outcomes; it never
     silently skips the UX tier.
   - **Web-app persistence (v3.26)** is the *one* exception to "detection
     never writes config": it writes the `web-app` block, but only **after**
     the user confirms — see the persistence sub-step below. It persists the
     **confirmed** answer, never the detected guess.

   Use `AskUserQuestion` with three options (pre-selected per detection
   result above):
   - **Yes** → ask two follow-up questions using `AskUserQuestion`:
     - *Design-language source*: upstream URL (default
       `https://github.com/rhohn94/design-language` — CONFIRM-pending
       placeholder; verify with project owner) or `local` for strict-local
       mode. Step 2.5 already seeded this value; confirm or override it here.
       Fills `{design-language-source}` and `{design-language-source-url}`
       (see Step 4).
     - *Primary GUI stack / framework hint*: the project's main GUI
       framework (e.g. "SwiftUI", "React", "Qt Widgets") — pre-filled
       from the detection stack-hint above when confidence is High or
       Medium; confirm or override. Fills `{ux-demo-stack}`, consumed by
       `grm-ux-demo-build` to build a stack-pure demo.

     **Web-app persistence (v3.26 — `web-app-support-design.md` §2.4).** Once
     the confirmed stack is in hand, decide the web-app fact from the *web
     slice* of the Q9 evidence (the narrower "browser-delivered, server-hosted
     app?" question — not the GUI boolean):

     - **Web slice** — rows 8–13/15 (browser/meta web frameworks), corroborated
       by rows 17–18, **or** a server web framework (Flask/Django/Express/
       FastAPI/Rails/Gin) serving HTML/templates → **persist** `web-app =
       { value: "yes", stack: <confirmed stack> }` into
       `.claude/grimoire-config.json` via a **pure-data write** (write only the
       `web-app` key; leave every other field and `schema-version` untouched —
       the same write `grm-web-app-apply` uses, no `schema-version` bump).
     - **Non-web GUI** — native/desktop/mobile (rows 1–7/9/14) or TUI (row 16)
       → persist **nothing**; the block stays **absent** even though
       `GUI = Yes`.
     - **If onboarding already wrote the block** (it passed a confirmed web-app
       answer in its §4.2 handoff) this step is a **no-op** — never re-detect or
       overwrite a confirmed answer.

     This is *after* the user confirms, never as part of detection. The persist
     makes the `grm-quick-start-template` Step 1 project-type read **real**: after
     bootstrap, the `web` profile resolves from the persisted `web-app` block
     without re-asking.
   - **Not yet** → record the deferral; Step 4 appends the placeholder
     row to `docs/roadmap.md` under `## Backlog`. No design-language
     adoption now. The `web-app` block stays **absent** (the default).
   - **No, headless** → mark the UX tier N/A for this project. Note it
     in the Step 5 report. Do **not** fabricate any files; skip
     `grm-design-language-adapt` and `grm-ux-demo-build` for this project's
     bootstrap. The `web-app` block stays **absent** (the default).
10. **Issue-filing authority (optional, v3.74, #221)** — whether autonomous
   roles (Reporter, QA agent, Triager, Noir integration master) may file
   issues against the configured tracker without asking each time. Prompt:
   *"Allow Grimoire's autonomous roles to file issues against your tracker
   without asking each time? This provisions a permission allowlist (issue-
   tracker MCP tool names + CLI fallback) in `.claude/settings.json` — filing
   is designed-in framework behaviour (see
   `docs/grimoire/design/issue-filing-authority-design.md`), but the entries
   are opt-in, never silently granted. [default: No]"* A **Yes** answer sets
   `issue-filing-authority: { enabled: true }` in `.claude/grimoire-config.json`
   (Step 4). A **No**/blank answer leaves the block absent — the default, matching
   `--migrate`'s never-synthesize rule for this dial (`config_validate.py`).

Record answers in a working table; echo it back before patching.

---

## Step 4 — Patch placeholders

Apply answers **only** to the project-config tokens in `manifest.md`.
Never substitute a runtime template token (`{feature}`, `{branch}`,
`{model}`, `{effort}`, `{short-sha}`, …) — those are filled per-use by
agents at runtime; replacing them breaks the templates.

| Answer | Files to patch |
|---|---|
| Active paradigm | `CLAUDE.md` — fill the `## Paradigm` stamp's `{ACTIVE}` token (see Rules below) |
| Test command  | `CLAUDE.md`, `grm-release-phase`, `grm-release-phase-merge` — every `{test-command}` |
| Build command | `CLAUDE.md`, `grm-release-phase`, `grm-release-phase-merge` — every `{build-command}` |
| Release command | `CLAUDE.md`, `grm-project-release`, `docs/grimoire/version-design.md` — every `{release-command}` |
| Type-check command | `CLAUDE.md` commands table — `{typecheck-command}` (blank ⇒ set `code-quality.typecheck.value: "off"`; folded into the build gate otherwise) |
| Lint command | `CLAUDE.md` commands table — `{lint-command}` (blank ⇒ no lint gate) |
| Coverage command | `CLAUDE.md` commands table — `{coverage-command}` (blank ⇒ `code-quality.coverage-threshold.value: null`) |
| Justfile build command | `.claude/grimoire-config.json` `commands.build` — string value or `null` (blank); Justfile `build` recipe wired if non-null and recipe body has `# grimoire:placeholder` |
| Justfile run command | `.claude/grimoire-config.json` `commands.run` — string value or `null` (blank); Justfile `run` recipe wired if non-null and recipe body has `# grimoire:placeholder` |
| Justfile deploy command | `.claude/grimoire-config.json` `commands.deploy` — string value or `null` (blank); Justfile `deploy` recipe wired if non-null and recipe body has `# grimoire:placeholder` |
| Version file + field | `docs/grimoire/version-design.md` §3 — `{path/to/version/file}` / `{field name or format}` |
| Doc-location map | `grm-repo-reference` SKILL.md — replace the map rows; keep the table shape |
| Integration / release branch names | `hooks/protected-branch-guard.sh` `PROTECTED_RE` (and any prose naming `dev`/`main`/`version/*` in CLAUDE.md + integration-workflow.md) |
| First roadmap entry | `docs/roadmap.md` — replace the placeholder `v1.0` block with the real first version + theme |
| GUI: Yes → design-language source | `docs/design/ux/design-language.md` front-matter `source:` — set to `upstream` or `local` (default `upstream`) |
| GUI: Yes → design-language source URL | `docs/design/ux/design-language.md` front-matter `source-url:` — set to the entered URL (default `https://github.com/rhohn94/design-language`) |
| GUI: Yes → GUI stack hint | `docs/design/ux/design-language.md` §Design preamble — add a "Primary stack: `{ux-demo-stack}`" note for `grm-ux-demo-build` to read; also echo in the Step 5 report |
| GUI: Not yet → deferral | `docs/roadmap.md` `## Backlog` section — append `- UX design language: deferred until v{X.Y}` (leave `{X.Y}` as a user-facing placeholder to fill at release-planning time) |
| GUI: No, headless → N/A | Report only — no files changed; note that `grm-design-language-adapt` and `grm-ux-demo-build` are skipped for this project |
| Issue-filing authority: Yes | `.claude/grimoire-config.json` `issue-filing-authority.enabled: true`; Step 2.7.1 then provisions `.claude/settings.json` |
| Issue-filing authority: No/blank | No files changed — the dial stays absent (the default) |

Rules:
- If branch names stay `dev`/`main`, leave `PROTECTED_RE` untouched.
- If a target file is absent (fresh project with no `CLAUDE.md`/`docs`
  yet), note it in the report — do **not** fabricate the file here;
  point the user at the scaffolding README setup checklist.
- Edit in place; preserve surrounding structure and formatting exactly.
- **Active-paradigm stamp (idempotent).** In `CLAUDE.md`, fill the
  `## Paradigm` stamp from `work-paradigm.value` in
  `.claude/grimoire-config.json` (default `Supervised` if missing/unset).
  The golden block is:
  ```markdown
  > **Paradigm:** {ACTIVE} — one of Supervised · Weiss · Noir.
  > Switch via the `grm-work-paradigm-switch` skill. See `.claude/paradigms/README.md`.
  ```
  Match the `> **Paradigm:** …` line and substitute the value (whether it is
  the literal `{ACTIVE}` token on a fresh golden copy or a previously-filled
  name on re-run) — **match-and-replace, never append a duplicate block**.

