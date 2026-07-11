# Hard-reset — reference
Loaded on demand by `SKILL.md`.

## Recovering from an archive

`hard_reset.py --restore TIMESTAMP [--root DIR] [--force] [--dry-run]` reads
`.grimoire-archive/<TIMESTAMP>/MANIFEST.md` and copies every archived file back
to its original repo-relative location — the automated counterpart to Step 5.1's
archive. Loud-fail-on-ambiguity: a missing/unreadable `MANIFEST.md` refuses
outright; a listed file whose on-disk content has changed since the archive was
taken also refuses unless `--force` is passed (never a silent clobber). A file
identical to its archived copy, or absent on disk, restores without `--force`.
`--dry-run` reports what would be restored without writing. Filesystem-only —
no git operations. Run `hard_reset.py --self-test` to verify the restorer logic.

## What this skill does NOT do

- It does **not** touch git history. No `git reset --hard`, `git clean`,
  `git push --force`, `git branch -D`, no rewritten commits, no deleted branches
  or tags. Archiving is a **file copy**; the reset's effect is ordinary
  working-tree changes the user reviews and commits like any other change. Prior
  history remains fully present (a second, independent recovery path). See
  `hard-reset-design.md` §5.
- It does **not** push or merge.
- It does **not** drive the onboarding interview inline by default — it restores
  the trigger condition (sentinel on line 1 of `CLAUDE.md`) and stops, so the
  user's next prompt is a normal first-run onboarding (unless `--reonboard-now`).

---

## Anti-patterns

- Running end-to-end without the Step 4 confirmation — a trigger phrase enters
  the skill; it does not authorise the reset.
- **Deleting** anything before it is archived, or clearing before `MANIFEST.md`
  is written — archive-before-clear is a safety property, not a suggestion.
- Hard-coding the framework inventory instead of reading `manifest.md` — they
  would drift.
- Any git-history operation (`git reset --hard`, `git clean`, branch/tag
  deletion) — explicitly out of scope; archiving is a file copy.
- Archiving/clearing the running `grm-hard-reset` skill mid-run, or self-running in
  the agentic-scaffolding dogfood repo (it would archive the product).
- Using an include-list for project source — classify by exclusion so no stray
  file is silently left behind.
- Committing or pushing. This skill reads, copies, and rewrites the working tree;
  the user commits.
