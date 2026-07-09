# Copilot grm- Namespacing Migrate Engine

> **Status:** Accepted — v3.54
> **Issue:** [#136](https://github.com/rhohn94/grimoire-framework/issues/136)
> **Related:** `docs/grimoire/design/grm-namespacing-design.md`, `grm-sync-from-upstream`, the copilot `/namespacing-migrate` prompt

---

## 1. Motivation

v3.42 namespaced every Grimoire skill `<name>` → `grm-<name>` and shipped a
deterministic, reusable migrate engine
(`.claude/skills/grm-sync-from-upstream/grm_namespacing.py`) so a **claude-code**
consumer that predates the rename can complete the bare-name → `grm-` cutover:
the sync is non-destructive, so it *adds* the new `grm-*` skills but leaves the
stale bare-named survivors beside them. The engine archives + removes the
survivors and rewrites every reference per a two-tier rule.

The **copilot** flavor had no equivalent. A copilot consumer that needs to
complete the same cutover had no engine to run — a flavor gap. This design adds
the copilot analogue (`copilot/scripts/grm_namespacing.py`) and its documented
entry point (the `/namespacing-migrate` prompt). Adding it **reduces** the
flavor gap; it does not create one.

## 2. Scope

What the copilot cutover surface actually is — and why it differs from
claude-code:

- **Copilot procedures are prompt files, not skills.** They live as
  `.github/prompts/<name>.prompt.md` and are invoked as `/<name>`. They sit in a
  Grimoire-private namespace (`.github/prompts/`), not the consumer-shared
  `.claude/skills/` namespace, so the collision rationale that motivated the
  `grm-` prefix does **not** apply to them. The engine therefore **never renames
  `*.prompt.md` files** — the copilot prompt identities stay bare by design.
- **Copilot DOES ship a small number of real `.claude/skills/` dirs.** As of
  v3.42 these are `grm-files-manifest` and `grm-regenerate-grimoire`. A
  pre-rename copilot consumer holds the bare survivors (`files-manifest/`,
  `regenerate-grimoire/`) beside the synced `grm-*` copies after a
  non-destructive sync. **These are the genuine copilot rename surface.**
- **Copilot content references skills by their `grm-` names.** Prompt bodies,
  `AGENTS.md`, `.github/copilot-instructions.md`, and `scripts/*.py` mention
  skills by name; a pre-rename consumer carries stale bare references and
  `skills/<name>/` paths that must be rewritten.

In scope: the two real `.claude/skills/` dir renames + frontmatter, and the
two-tier reference rewrite across `.github/prompts/`, `AGENTS.md`,
`.github/copilot-instructions.md`, `scripts/`, and any other repo text. Out of
scope: renaming prompt files; changing any procedure's behavior; rewriting
un-backticked common-word prose.

## 3. Decision: an engine, not a re-bootstrap path

A real engine is warranted (option (a) of the ticket) rather than a documented
re-bootstrap (option (b)), because:

1. The copilot consumer has the **same stale-survivor problem** the claude-code
   consumer has — on its own real `.claude/skills/` dirs — and the same need for
   a conservative, idempotent, archive-before-remove rewrite. A re-bootstrap
   would clobber consumer customizations; the engine preserves them.
2. Copilot already ships every other one-shot migrate helper as a
   `copilot/scripts/*.py` (`vendor_migrate.py`, `sync_deps_engine.py`, …), so a
   `copilot/scripts/grm_namespacing.py` is the established pattern.
3. Parity: the claude-code engine is the contract authority; the copilot engine
   mirrors it (same flags, same two-tier rule, same idempotency / dry-run /
   self-test discipline) so both flavors behave identically where their surfaces
   overlap.

## 4. Design

`copilot/scripts/grm_namespacing.py` defines `CopilotGrmNamespacer`, a
stdlib-only, class-based transformer modeled on the claude-code `GrmNamespacer`.

- **Discovery** (`discover_skill_names`): the known-name set is the union of the
  bare base names of every `.claude/skills/<name>/` child on disk **and** the
  slash-command names derived from `.github/prompts/<name>.prompt.md`. The latter
  ensures content references to copilot procedures are rewritten even when no
  real skill dir of that name exists.
- **Rename** (`rename_dirs`): renames only real `.claude/skills/<name>/` dirs to
  `grm-<name>/` (deepest-first, `git mv` to preserve history). Already-`grm-`
  dirs are skipped (idempotent). A post-sync collision (the `grm-<name>/` already
  exists beside a stale bare `<name>/`) archives the stale dir to
  `.grimoire-archive/grm-namespacing-copilot-<ts>/` and removes it — never
  nesting `grm-<name>/<name>/`. Prompt files are structurally excluded.
- **Frontmatter** (`update_frontmatter`): sets each renamed `SKILL.md`'s `name:`
  to its new dir name.
- **Reference rewrite** (`rewrite_references`): the shared two-tier rule —
  - **Tier 1 (paths):** `skills/<name>/` → `skills/grm-<name>/`.
  - **Tier 2 (prose):** backticked exact token `` `<name>` `` → `` `grm-<name>` ``,
    plus `<name> skill` / `the <name> skill` / `skill <name>`. Un-backticked
    common words are left untouched.
- **Boundaries:** git submodules (from `.gitmodules` and nested `.git` scan) and
  `lib/third-party/` vendored trees are never descended into.

CLI: `--root`, `--apply` (default dry-run), `--dry-run`, `--self-test`. The
documented entry point for a consumer is the `/namespacing-migrate` prompt.

## 5. Acceptance

- `python3 copilot/scripts/grm_namespacing.py --self-test` passes — proving:
  bare skill dir renamed; **prompt files never renamed**; Tier-1 path + Tier-2a
  backtick + Tier-2b prose rewrites applied; common-word false-positives avoided;
  post-sync collision archived + removed (not nested); submodule /
  `lib/third-party/` boundaries respected; a second run is a no-op (idempotent).
- `--dry-run` on a copilot tree runs cleanly (exit 0) and reports what it WOULD
  do without writing; on the already-namespaced canonical copilot flavor the
  dir-rename pass is correctly a no-op.
- The engine never touches `*.prompt.md` filenames and never edits
  `.claude/skills/` outside the consumer's own tree boundary rules.
- A copilot consumer has a documented run path: the `/namespacing-migrate`
  prompt (`copilot/.github/prompts/namespacing-migrate.prompt.md`).
