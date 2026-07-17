---
name: grm-quick-start-template
description: Bootstrap a project from a known-good application profile — match the declared app profile (API service, CLI, web app, …) against available quick-start templates and apply the closest match (scaffold its ready-made pieces + config defaults). Never overwrites existing files without confirmation. Use when applying a quick-start template or scaffolding from an app profile.
---

# Quick-start template

Matches the project's declared application profile against the available
quick-start templates and applies the closest one — pre-populating the project
with the template's ready-made pieces and config defaults. Conventions:
`docs/design/quick-start-templates-design.md` §2.2. Compatibility-matrix
contract (consulted at author/apply time):
`docs/design/component-compatibility-matrix.md`.

## Step 1 — Determine the app profile

Read the project type from `.claude/grimoire-config.json`. **For the `web`
profile this is now a real read, not a guess (v3.26):** read the `web-app` block
(`web-app-support-design.md` §1) — `web-app.value == "yes"` resolves the profile
to `web` directly, and `web-app.stack` names the concrete framework. The block is
persisted at onboarding / `grm-workflow-bootstrap` Q9 / `grm-web-app-apply` (§2.4), so
after bootstrap a web app resolves **without asking**. Absence of the block (≡
`value: "no"`) means *not a web app* — fall through to the other signals.

For the remaining profiles, read `grm-workflow-bootstrap`'s detection signal (the
Q9 stack hint / `layout.meta.stack`). If still absent or ambiguous, **ask** the
user (`api`/`service`, `cli`, `web`/`gui`, `lib`).

## Step 2 — List & score templates

Enumerate `.claude/quick-start-templates/<name>/template.json`. Score each:
- exact `profile` match — best
- shared profile tag — good
- generic / profile-agnostic — fallback

If none exist, say so and stop (suggest authoring one from a
`grm-component-catalog-export` report).

## Step 3 — Surface the match

Present the **closest match + alternatives**, each with its one-line `summary`,
the components it pulls in, and the `config-defaults` it would set. Confirm before
applying (Supervised/Weiss require explicit confirmation; Noir may auto-pick the
top match but **must report** what it applied and why).

## Step 3.5 — Compatibility check (matrix consult)

Before applying, consult the **compatibility matrix** so an incompatible
selection is surfaced *before* anything is scaffolded. The matrix is the derived
relation over the component registry; spec:
`docs/design/component-compatibility-matrix.md`.

1. **Locate / refresh the matrix.** Read
   `.claude/cache/component-compatibility.json` (derived, gitignored).
   - If `.claude/component-registry.json` is **absent** → there is no matrix;
     **skip this step** (back-compat: a project that never built a registry is
     unaffected — proceed to Step 4).
   - If the cache is **absent or stale** — stale meaning its
     `computed-from.registry-digest` differs from a freshly computed digest of
     the live registry's `components` map — regenerate it from the registry
     before reading (the `grm-component-registry` skill owns regeneration).
2. **Check the selected set** (the template's resolved `components` + the target
   profile from Step 1):
   - any **pair** of selected components marked incompatible in
     `component-component` (e.g. `conflicting-provides`, `language-mismatch`,
     `framework-unsatisfiable`);
   - any selected component marked incompatible with the **target profile** in
     `component-profile` (e.g. `profile-not-listed`);
   - any `requires` capability of the selected set not satisfiable within the
     set / profile.
3. **Warn, do not block.** Print each finding with its matrix `reasons`, then
   ask the user whether to proceed anyway. This is a **non-blocking warning**,
   consistent with the skill's "never overwrite without confirmation" posture —
   the user may knowingly accept a flagged selection. Under Noir, report the
   findings and proceed with the top match unless a finding is fatal.

## Step 4 — Apply

1. Resolve the template's `components` against the catalog
   (`grm-component-catalog-export`); warn on any unresolved id.
2. Scaffold the `scaffold` file mappings into the project — **never overwrite an
   existing file without confirmation** (idempotent-friendly: skip files already
   present, list what was skipped).
3. Write `config-defaults` via the existing switch skills
   (`grm-model-effort-profile-switch`, `grm-workflow-variant-switch`, etc.) — do not edit
   `grimoire-config.json` by hand.
4. Print the template's `post-apply-notes` and a summary of what was created /
   skipped / set.

## Invocation

```
quick-start-template                 # detect profile, match, confirm, apply
quick-start-template --profile api   # force a profile
quick-start-template --list          # list templates + scores, apply nothing
```

## Safety / scope
- Applying is the only write surface; never destructive without confirmation.
- Does not implement components or fetch remote templates (local repo only).
- Config changes go through the switch skills, preserving other config fields.

## Anti-patterns
- Overwriting existing project files silently — confirm or skip.
- Editing `grimoire-config.json` directly instead of via switch skills.
- Applying a template whose `profile` doesn't match without flagging the mismatch.


## Deterministic tooling tier (v1.27)

On apply, drop in the profile's linter/formatter config (and, on opt-in, the
pre-commit config) per `docs/coding-standards/tooling.md`, so the scaffolded
project lints and formats from day one. The lint/type-check/coverage commands
captured at `grm-workflow-bootstrap` feed the v1.26 merge gate — define each once,
reuse everywhere. See `docs/grimoire/design/managed-project-tooling-design.md`.

## Profile × language matching (v1.31, #67)

A template's `template.json` may carry a `languages` map (variants for
`python` / `typescript` / `go` / `rust`, each with `test`/`build`/`lint`/`typecheck`).
On apply, match the **declared project language** against this map and wire that
variant's commands into the `CLAUDE.md` commands table + the v1.26 `code-quality`
block — do not edit them inline. If the language is absent from the map, fall back
to the profile default and note it. New profiles in v1.31: `service`, `gui`, `lib`
(mobile deferred). `cli` added in v3.94 (#432) — Step 1's ask-list already named
it; the template now exists and scores like any other. Authority:
`docs/design/defaults-quickstart-design.md`.

## Release-automation starter pack (v3.94, #431/#432)

Every shipped `template.json` (`web`/`gui`/`lib`/`service`/`cli`) now ships REAL,
running `package`/`release` recipes (`gui`/`service`/`cli` also `deploy`) — not
placeholder bodies. `package`/`release`/`deploy` delegate to the shared
`scripts/{package,release,deploy}.sh` reference implementation (ported from the
`web` profile, parameterized by `grimoire-config.json` + `publish.toml`/
`scripts/package-manifest.sh`), emitting the standard artifact trio
(`release.json`, `SHA256SUMS`, `grimoire-build-info.json` — the §8 provenance
stamp) for the binary-shaped profiles. `lib` is the one deliberate exception:
its `package` target emits the crate-producer trio (`<name>-v{ver}.tar.gz`,
`release.json`, `SHA256SUMS`) via the already-shipped
`build_crate_artifact.py` (Dependency Channel producer contract,
`dependency-channel-design.md` §2b) — NOT `grimoire-build-info.json`, since
that stamp is Admin Console provenance for a deployed, running app, which a
vendored library crate is not (see the `lib` template's `post-apply-notes`).
Each script carries an inline `--self-test`; run one on a scaffolded project's
copy to verify the port stayed intact.

## Catalogued from day one (v3.97, #459)

Only the `lib` profile ships a pre-filled `component.json` (at the scaffold
root, alongside Cargo.toml) — the WHOLE scaffolded crate is the reusable,
vendorable unit that profile exists to produce (its `package`/`release`
recipes already emit the Dependency Channel producer trio, see above), so
cataloging it is the profile matching its own purpose. `gui`/`service`/`cli`
deliberately do **not** get one: those profiles scaffold terminal,
deployable apps — nothing another project would `vendor.toml`-consume — and
forcing a component.json onto boilerplate meant to diverge immediately
(`config.rs`, `build_info.rs`) would misrepresent it as a stable, versioned
unit. `web` is out of scope for this addition (not one of the four profiles
covered by #459's acceptance criteria). Verify against the vocabulary
(`docs/grimoire/design/component-taxonomy.md`) before adding a
`component.json` to any future profile — don't force one where nothing
reusable-component-shaped exists. NOTE: `grm-component-registry`'s default
scan paths are `components/`/`lib/`; a root-level `component.json` is not
auto-discovered until the scaffolded project adds
`component-catalog.paths: ["."]` to its own `grimoire-config.json` (see the
`lib` template's `post-apply-notes`).
