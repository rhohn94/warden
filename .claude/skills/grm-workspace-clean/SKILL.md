---
name: grm-workspace-clean
description: Tiered, report-then-apply cleanup of Grimoire workspace artifacts — classify each known working-state dir KEEP / SAFE-DELETE and remove ONLY the safe-delete set. Report-only by default; --apply deletes; a hard denylist (.claude/, .scaffold-base/) is refused unconditionally; never deletes load-bearing state. Use when a project root is cluttered with Grimoire staging/cache/backup dirs.
---

# workspace-clean

Sweep the **transient, re-clonable, and regenerable** Grimoire working-state
directories that accumulate in a project root, while **never** touching the
load-bearing ones. Report-only by default; `--apply` deletes only the
safe-delete set. House style: classify findings, report first, act on `--apply`,
and never delete a denylisted dir — modelled on **`grm-structure-migrate`**.

The backing script is `workspace_clean.py` (stdlib-only, `--self-test`). Design:
`docs/grimoire/design/workspace-clean-design.md`.

## When to run

- After a `grm-structure-migrate` + `grm-regenerate-grimoire` cycle leaves
  staging/cache dirs behind in a consumer's root.
- When `git status` shows untracked `.grimoire-*` / `.scaffold-*` /
  `.design-language-source` clutter.
- On demand, to tidy a workspace before handing the repo to a collaborator.

Most of these dirs are already **gitignored** (so they are clutter, not commits);
this skill removes the on-disk copies. The complementary v3.52 fixes
(self-cleanup in `grm-sync-from-upstream` / `grm-regenerate-grimoire`, and the
gitignore additions for `.scaffold-base/` + `.design-language-source/`) reduce
how often this is needed; this skill is the deliberate, explicit broom.

## Classification (the authority)

| Artifact | Verdict | Why |
|---|---|---|
| `.claude/` | **KEEP** | the entire Grimoire install — **hard denylist**, never deleted |
| `.scaffold-base/` | **KEEP** | 3-way-merge base for `grm-sync-from-upstream` — load-bearing; deleting it breaks the next sync. **hard denylist** |
| `.design-language-source/` | SAFE-DELETE | cached clone for `grm-design-language-adapt` — re-clonable |
| `.grimoire-golden/` | SAFE-DELETE | generated golden restore baseline — regenerable cache |
| `.grimoire-source/` | SAFE-DELETE | source/staging clone — transient |
| `.scaffold-sync-backup/` | SAFE-DELETE | rollback backup written during a sync — transient (post-successful-sync) |

The two **hard-denylist** dirs are refused unconditionally — they are never in
the delete set, and the script's `--apply` asserts against removing them even if
a future edit miscategorized them.

## Detect mode (default — read-only)

```
python3 .claude/skills/grm-workspace-clean/workspace_clean.py
```

Emits a machine line + a per-artifact table; nothing is removed. Exit 0 = no
safe-delete findings (clean). Exit 1 = safe-delete findings present (the broom
has something to do). Mirrors `grm-structure-migrate` / `grm-docs-migrate`.

```
workspace-clean — 3 artifact(s): 2 safe-delete, 1 keep
  .scaffold-base/            KEEP         (kept)               3-way-merge base ...
  .grimoire-source/          SAFE-DELETE  → delete on --apply  source/staging clone ...
  .scaffold-sync-backup/     SAFE-DELETE  → delete on --apply  rollback backup ...
```

## --apply mode (explicit, confirmed)

```
python3 .claude/skills/grm-workspace-clean/workspace_clean.py --apply
```

Removes ONLY the safe-delete set, then reprints the report for transparency.
Idempotent — a second run on a clean workspace is a no-op. Under **Stealth
Mode**, suppress the offer to run `--apply` (the agent leaves no broom trail).

## Anti-patterns

- **Never delete `.claude/` or `.scaffold-base/`** — they are hard-denylisted;
  the script refuses even if asked. Removing `.scaffold-base/` silently breaks
  the next `grm-sync-from-upstream` merge.
- **Never auto-run `--apply`** — deleting a cache is cheap to regenerate but the
  user should choose; report first and confirm.
- **Never extend the safe-delete set ad hoc** — add a row to `CLASSIFICATION` in
  the script (with a self-test) so the verdict is auditable, never a one-off
  `rm`.

## See also

- `docs/grimoire/design/workspace-clean-design.md` — the classification + safety design.
- **`grm-structure-migrate`** — the report-then-apply sibling this models.
- **`grm-sync-from-upstream`** / **`grm-regenerate-grimoire`** — now self-clean
  their own transient backups/staging on a successful run (v3.52).
- **`grm-hard-reset`** — the heavy, archive-everything reset (a different tool).
