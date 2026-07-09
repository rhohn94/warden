---
name: grm-structure-migrate
description: Detect and migrate a project's directory layout to the standard structure — classify MISSING_REQUIRED / NONSTANDARD_DIR / TRACKED_OUTPUT / VENDOR_DEST findings; --apply relocates via git mv, rewrites vendor.toml dest, git-ignores build output. Report-only by default; never deletes work. Use when adapting a project to the standard file structure or fixing grm-architecture-audit structure-* findings.
---

# structure-migrate

Adapt an existing project's top-level layout to the **standard project
structure** (`docs/project-structure.md`). Report-only by default; `--apply`
performs history-preserving relocations. The deterministic complement is
**`grm-architecture-audit`** Step 3a (which *reports* the same drift); this skill
*fixes* it. Design: `docs/grimoire/design/file-structure-standard-design.md`.

Reads the `structure` block of `.claude/architecture-rules.json` for the rules
(`required`, `aliases`, `gitignored`). If no `structure` block is declared, there
is nothing to enforce — report `structure-migrate: no structure declared` and
exit clean. Downstream-safe: uses `.claude/grimoire-config.json` for root
detection; never requires `claude-code/`.

## When to run

- When **`grm-architecture-audit`** reports `structure-required`,
  `structure-nonstandard`, or `structure-tracked-output` findings.
- After `grm-sync-from-upstream`, to bring an older project onto the standard.
- On demand, to relocate a legacy `vendor/` tree to `lib/third-party/`.

## Detect mode (default — read-only)

Walk the top-level directory listing and `git ls-files`, then classify:

| Code | Meaning | Remedy on `--apply` |
|---|---|---|
| `MISSING_REQUIRED` | a `required` dir (`src`, `docs`, `tests`) is absent | **report only** — never auto-create source dirs; the agent decides what content belongs there |
| `NONSTANDARD_DIR` | a top-level dir matches an `aliases` key (`vendor/`, `test/`, `build/`, …) | `git mv <dir> <standard-home>` |
| `SUBMODULE_NONSTANDARD_DIR` | a `NONSTANDARD_DIR` (or a child of one) is a **registered git submodule** (listed in `.gitmodules`) | **report only** — a plain `git mv` strands the `.gitmodules` `path` entry; surface the manual remedy below and skip the move |
| `TRACKED_OUTPUT` | a `gitignored` dir (`dist/`, `build/`) is tracked by git | add to `.gitignore` + `git rm -r --cached` |
| `VENDOR_DEST` | a `vendor.toml` `[deps.*] dest` still points under `vendor/` | rewrite `dest` to `lib/third-party/<dep>` |

**Submodule check (run before classifying any `NONSTANDARD_DIR`):** for each
candidate move source `<src>`, look it up in `.gitmodules` (a `path = <src>`
entry, or `<src>` being a parent of a submodule `path`). If `<src>` is — or
contains — a registered submodule, classify it as `SUBMODULE_NONSTANDARD_DIR`
instead of `NONSTANDARD_DIR`. A plain `git mv vendor/aura lib/third-party/aura`
moves the working tree but leaves `.gitmodules` saying `path = vendor/aura`,
which makes git treat the submodule as broken/missing. Auto-running the full
submodule move is risky for an agent, so the default is **detect-and-surface**.

Emit the same machine-block + human-table shape the audit uses. Nothing moves.

```
structure-migrate — 3 finding(s): 1 submodule-nonstandard-dir, 1 vendor-dest, 1 missing-required
  vendor/aura    SUBMODULE_NONSTANDARD_DIR  registered submodule → manual move (see below)
  vendor.toml    VENDOR_DEST                deps.aura.dest vendor/aura → lib/third-party/aura
  tests/         MISSING_REQUIRED           required dir absent (report only)
```

Exit 0 = no findings. Exit 1 = findings present. (Mirrors `grm-docs-migrate`.)

## --apply mode (explicit, confirmed)

Perform each remedy in order, **safely and idempotently**:

1. **Archive a manifest** of every planned move to
   `.grimoire/structure-migration-<timestamp>.json` (before touching anything).
2. **Relocate** each `NONSTANDARD_DIR` with **`git mv <dir> <standard-home>`**
   (history-preserving). If the standard home already exists, merge children;
   never overwrite an existing file — report a collision and skip that entry.
   **Skip every `SUBMODULE_NONSTANDARD_DIR`** — a plain `git mv` strands the
   `.gitmodules` reference. Leave it in place, keep the finding, and print the
   manual remedy (below). Never auto-run a submodule relocation.
3. **Rewrite** each `VENDOR_DEST` in `vendor.toml` (`vendor/<dep>` →
   `lib/third-party/<dep>`). Re-run **`grm-sync-deps`** afterward is unnecessary
   — the bytes moved with the directory — but a `--check` confirms the lock.
4. **Git-ignore output**: append `dist/` and `build/` to `.gitignore` if missing,
   then `git rm -r --cached <dir>` for any `TRACKED_OUTPUT` (keeps the files
   on disk, untracks them).
5. **Flag import-breaking moves, do not perform them.** If a relocated directory
   is referenced by source `import`/`use` statements (grep `src/` for the old
   path), **list the affected files and skip the move** — the agent resolves the
   references first, then re-runs. Never silently rewrite source imports.
6. Idempotent — a second run on a conformant project is a no-op.

`MISSING_REQUIRED` is **never** auto-fixed: creating an empty `src/` or `tests/`
hides real work. It stays a report-only finding the agent acts on deliberately.

## Submodule manual remedy (`SUBMODULE_NONSTANDARD_DIR`)

`--apply` never relocates a registered submodule, because `git mv` alone leaves
`.gitmodules` pointing at the old path and breaks the submodule. When a
`SUBMODULE_NONSTANDARD_DIR` finding appears (e.g. `vendor/aura` is a submodule),
the agent or user runs the full sequence by hand, then re-runs detect to confirm:

```sh
# Move the working tree (history-preserving).
git mv vendor/aura lib/third-party/aura
# Repoint the submodule path in .gitmodules.
git config -f .gitmodules submodule.vendor/aura.path lib/third-party/aura
# Re-sync the recorded submodule config to the new path.
git submodule sync
git add .gitmodules
# (If git tracks a stale per-submodule URL, refresh it:)
#   git config --local submodule.lib/third-party/aura.url <url>
git commit -m "move vendor/aura submodule to lib/third-party/aura"
```

Note the `.gitmodules` key keeps the **original** submodule name
(`submodule.vendor/aura`) unless you also rename the section — `git config -f
.gitmodules` only updates the `path` value, which is what makes the submodule
resolve again. If the project also has a `VENDOR_DEST` finding for the same dep,
rewrite `vendor.toml`'s `dest` to match after the move.

## Anti-patterns

- **Never run inside the scaffolding repo itself** — this tool is for downstream
  projects; the scaffolding's own layout is managed differently.
- **Never delete a directory** — relocation (`git mv`) and untracking
  (`rm --cached`) only; structure-migrate never destroys project work.
- **Never auto-rewrite source imports** — flag and defer to the agent.
- **Never auto-run `--apply`** — moving directories is consequential; always
  report first and confirm before applying. Under **Stealth Mode**, suppress the
  offer entirely.

## See also

- `docs/project-structure.md` — the standard this migrates toward.
- `docs/grimoire/design/file-structure-standard-design.md` — the mechanism design.
- **`grm-architecture-audit`** — reports the same drift as fitness functions.
- **`grm-sync-deps`** — vendors dependencies into `lib/third-party/` (the new
  default `dest`).
