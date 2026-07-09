---
name: grm-sync-from-source
description: Pull workflow skills, hooks, and structural docs from a source project (the repo where you actively develop the workflow) into this Grimoire repo, then re-generalize and refresh the golden baseline. Safe by default — never overwrites newer or in-progress scaffolding files without review. Use when you've improved a skill/hook/doc in your main project and want those improvements reflected in the reusable scaffolding.
---

# Sync-from-source

Brings workflow improvements **from** a source project (e.g. the repo where
you actually use these skills day-to-day) **into** this scaffolding repo, so
the reusable starter stays current with what you've learned — without
clobbering scaffolding work in progress.

It pairs a script (`sync-from-source.sh`, mechanical + safe file diffing) with
the judgment this skill supplies: deciding what's genuinely an improvement vs.
source-specific noise, **re-generalizing** copied content back into
placeholders, and refreshing `golden/`.

> Direction is always **source → this scaffolding**. The source's skills are
> project-specific (real commands, real branch names); the scaffolding's are
> generic. Copying is therefore never verbatim-final — every copied file must
> be re-generalized before it's a good baseline.

---

## When to use this skill

- You improved a skill/hook/doc in your main project and want the scaffolding
  to benefit.
- You're refreshing the scaffolding before sharing it or starting a new project.

Do **not** use it to push *from* the scaffolding into a project — that's what
`grm-workflow-bootstrap` does. This skill only pulls inbound.

---

## Step 0 — Safety preconditions (the user's explicit requirement)

Before changing anything, confirm the destination (this scaffolding repo) has
no work that a sync could destroy:

1. **Committed?** If this repo is a git repo, ensure the working tree is clean
   (`git status`). The script refuses `--apply` on a dirty tree unless
   `--force`; do **not** reach for `--force` to bypass real WIP — commit or
   stash first. If the repo is **not** yet under git, say so and lean harder on
   steps 2–3, because there is no undo.
2. **Newer / in-progress files.** The script auto-protects any destination file
   that is newer than its source counterpart (`SKIP-NEWER`). Treat every
   `SKIP-NEWER` as "someone may be mid-edit here" — investigate before
   overriding. Never pass `--overwrite-newer` without confirming each protected
   file is safe to lose.
3. **Backups.** On `--apply` the script writes overwritten files to
   `.sync-backup/<timestamp>/`. Confirm that directory is git-ignored or
   cleaned up afterward.

---

## Step 1 — Dry-run and review

Run from the scaffolding repo root (script resolves its own location):

```bash
.claude/skills/grm-sync-from-source/sync-from-source.sh <source-path> --diff
# or:  SCAFFOLD_SOURCE=<source-path> .claude/skills/grm-sync-from-source/sync-from-source.sh --diff
```

Read the action table:

| Marker | Meaning | Your job |
|---|---|---|
| `identical`   | Already in sync. | Nothing. |
| `NEW`         | Source has it, scaffolding doesn't. | Decide if it belongs in the generic set (see Step 2). |
| `UPDATE`      | Both exist, source differs and is newer/equal age. | Review the diff — is it a real improvement or source-specific churn? |
| `SKIP-NEWER`  | Dest is newer — **protected**. | Investigate: in-progress scaffolding work? Keep it, or merge by hand. |
| `SRC-MISSING` | Scaffolding-only skill (e.g. you renamed). | Expected for `grm-source-to-design-docs` etc. Ignore. |
| `⚠ needs-genericize` | Copied content carries source-specific tokens. | Must re-generalize in Step 3. |

The skill name map lives in `name-map.conf` (e.g. the scaffolding's
`grm-project-release` maps to a source's `forge-release`). Update it if your source
names a skill differently.

---

## Step 2 — Decide scope

- **Don't blindly accept every NEW skill.** Source projects accumulate
  project-specific skills (UI screenshotting, ecosystem-specific release
  tooling) that don't belong in a generic starter. Only bring over skills that
  are part of the reusable workflow. If a NEW skill is generic-worthy, add it to
  `workflow-bootstrap/manifest.md` and the `SKILLS` list in `sync-from-source.sh`.
- **Templates are never overwritten by real content.** `docs/roadmap.md` and
  `docs/design/README.md` are scaffolding templates; the script deliberately
  excludes them. Don't add a source's real roadmap/index over them.
  Note: `docs/roadmap.md` serves as the release-planning *narrative* (roadmap
  items, version history, strategy text). Issue tracking for the project may
  live in the configured issue tracker (roadmap `## Backlog` section, or an
  external provider like GitHub Issues) — see `grm-issue-tracker-switch` for the
  active configuration. Sync does not migrate issues between trackers.
- **CLAUDE.md** is reported but not auto-copied — it needs heavy
  re-generalization. Port improvements by hand if warranted.

---

## Step 3 — Apply, then re-generalize

```bash
.claude/skills/grm-sync-from-source/sync-from-source.sh <source-path> --apply
```

The script copies **verbatim**. For every file flagged `⚠ needs-genericize`
(and any UPDATE you accepted), re-insert placeholders so the scaffolding stays
project-agnostic. Use `workflow-bootstrap/manifest.md` as the token registry —
reverse its mapping:

| Source-specific value (examples) | Replace with token |
|---|---|
| `cargo test`, `pytest`, `npm test` | `{test-command}` |
| `cargo build --release`, `make`    | `{build-command}` |
| `just release`, `npm version minor`| `{release-command}` |
| real version file (`Cargo.toml`, …)| `{path/to/version/file}` / `{field name or format}` |
| project-specific branch names      | generic `dev` / `main` / `version/*` |
| product/module names (`forge-…`)   | generic nouns |

Never touch runtime template tokens (`{feature}`, `{branch}`, `{model}`,
`{effort}`, `{short-sha}`, `{file}`) — they are not project config.

If a file can't be cleanly generalized, revert it from
`.sync-backup/<timestamp>/` rather than commit a poisoned baseline.

---

## Step 4 — Refresh golden

The synced + generalized files are now the live scaffolding skills. Update the
restore baseline so `grm-workflow-bootstrap` reproduces them:

- Run the **`grm-workflow-snapshot`** skill. Its mandatory genericize check is a
  second safety net against source-specific values leaking into `golden/`.

---

## Step 5 — Report

- Files updated / added / protected (`SKIP-NEWER`), by path.
- Which copied files were re-generalized, and the tokens re-inserted.
- Manifest / `SKILLS`-list changes (if a new skill was adopted).
- Backup directory location.
- Remind the user to review and commit the scaffolding repo.

No commits — the user reviews the diff and commits.

---

## Anti-patterns

- `--force` or `--overwrite-newer` to "just get it done" — these defeat the
  in-progress-work protection the whole skill exists to provide.
- Committing source-specific commands/branch names/product names into the
  generic scaffolding (poisons every future project that restores from it).
- Adopting every source skill — keep the generic set lean and intentional.
- Overwriting `roadmap.md` / `design/README.md` templates with a source's real
  content.
- Skipping Step 4 — live skills updated but `golden/` left stale means a future
  `grm-workflow-bootstrap` restore silently reverts your improvements.
