---
name: hard-reset
description: Re-initialize a Grimoire scaffold to its pristine, not-yet-onboarded state while ARCHIVING (never deleting) every project-local file to a timestamped .grimoire-archive/<ts>/, restoring framework files to golden, re-arming the onboarding sentinel, and handing back to onboarding. Destructive-adjacent — archives before clearing and requires explicit per-action confirmation. Use only on deliberate intent. Triggers on "hard reset", "reset the scaffold", "re-initialize the project", "start the project over", "factory reset Grimoire", "wipe and re-onboard", "archive and reset".
---

# Hard-reset

Returns a Grimoire scaffold to its day-one, not-yet-onboarded shape — abandons
the accumulated roadmap / version history / release plans / project design docs
/ project source and a customised `CLAUDE.md`, restores the framework to
pristine, re-arms the onboarding sentinel, and hands back to onboarding.

The non-negotiable invariant is **archive, never delete**. Every project-local
file (and any customised framework file) is **copied** into a timestamped
archive *before* anything is cleared or overwritten, so a reset is always fully
recoverable. Full contract: `docs/design/hard-reset-design.md`.

> **This skill is destructive-adjacent.** It clears project-local files and
> overwrites framework files. Even though it archives everything first, it is
> governed by the `CLAUDE.md` §Commits destructive-op rule and **never runs
> without an explicit per-action confirmation in the same turn** (Step 4). A
> trigger phrase only *enters* the skill — it does not authorise the reset.

> **Not for the scaffolding repo itself.** In the agentic-scaffolding dogfood
> checkout, `claude-code/`, `copilot/`, and `docs/design/*` ARE the product, not
> project-local state — a reset here would archive the product. This skill is for
> **adopting projects**. The pre-flight summary (Step 3) will list the product as
> "project source"; that is your signal to abort. The human gate is the backstop.

---

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

## Flags

| Flag | Effect | Default |
|---|---|---|
| `--reset-config` | Reset `grimoire-config.json` to defaults instead of preserving preferences (Step 6). | off (preserve) |
| `--reonboard-now` | Run the `onboarding` interview inline after the reset instead of arming the sentinel for the next prompt (Step 8). | off (arm sentinel) |

---

## Step 1 — Classify the working tree

Build the file-class split from `hard-reset-design.md` §1 against the **actual**
working tree. The framework inventory is sourced from `workflow-bootstrap`'s
`manifest.md` (the "Restorable skills / infrastructure / workflows / paradigm
content sets" sections) — read it, never hard-code the list, so the two never
drift.

**FRAMEWORK** (restore to pristine; archive first only if customised):

| Path / glob | Source of truth |
|---|---|
| `.claude/skills/**` (all skills in `manifest.md`) | golden `golden/skills/` |
| `.claude/hooks/*.sh` | golden `golden/hooks/` |
| `.claude/settings.json` | golden `golden/settings.json` |
| `.claude/push-allowlist` | golden `golden/push-allowlist` |
| `.claude/workflows/*.js` | golden `golden/workflows/` |
| `.claude/paradigms/{supervised,weiss,noir}/**` | golden `golden/paradigms/` |
| `.claude/grimoire-config.json` | regenerated — Step 6 |
| `docs/design/README.md` (template index) | golden / template |
| `docs/coding-standards.md`, `docs/architecture-guidelines.md`, `docs/integration-workflow.md`, `docs/version-design.md`, `docs/quickstart.md`, `docs/features.md` | scaffold template copies |

**PROJECT-LOCAL** (archive, then clear):

| Path / glob | Action |
|---|---|
| `docs/roadmap.md` | archive → reset to template placeholder |
| `docs/version-history.md` | archive → reset to empty template |
| `docs/release-planning-v*.md` | archive → remove |
| `docs/design/*-design.md` (except `README.md`) | archive → remove |
| `docs/design/ux/**` | archive → remove |
| `ux-demo/**` | archive → remove |
| Project source tree (everything outside the framework areas) | archive → remove — by **exclusion**, see below |
| `.claude/integration-allow.local` | archive → remove |
| `.claude/settings.local.json` | archive → remove |

**Ambiguous cases** (`hard-reset-design.md` §1.3–§1.4):

- **`CLAUDE.md`** — treat as **project-local for archival, framework for
  restoration**: archive the current file verbatim, then restore it from the
  pristine scaffold template (onboarding sentinel on line 1, unfilled
  `{test-command}` / `{build-command}` / `{release-command}` tokens). The result
  is the byte-for-byte day-one file re-onboarding expects.
- **`.gitignore`** — **framework**, left in place / merged, not cleared. Ensure
  the `.grimoire-archive/` ignore entry is present (Step 2.3); otherwise leave
  it untouched. Archive a courtesy copy.
- **Project source by exclusion, never an include-list** — anything that is
  *not* a framework area (`.claude/`, `claude-code/`, `copilot/`, the scaffold's
  own template `docs/*` files, `.gitignore`, `.git/`) and not already classified
  above is project source → archive → remove. The exclude-list approach means no
  stray project file is silently left behind; the Step 3 summary lists every path.

**Meta-skill self-preservation:** like `workflow-bootstrap` and
`workflow-snapshot`, `hard-reset` is a framework file but **must not
archive/clear itself while executing** — the running copy is preserved through
the run; the golden-restore (Step 5) re-establishes it as a pristine framework
file like any other.

**Refuse on un-archivable state:** if the tree contains files the skill cannot
classify or copy (permission errors, symlink loops), report them and **refuse**
rather than partially resetting.

---

## Step 2 — Decide the archive destination

- Destination: `.grimoire-archive/<ts>/` at the repo root, where `<ts>` is a UTC
  timestamp `YYYYMMDD-HHMMSS` (e.g. `.grimoire-archive/20260529-143012/`). A new
  directory per reset — earlier archives are never overwritten.
- Layout (`hard-reset-design.md` §2.2): project-local files mirror their original
  repo-relative path under `<ts>/`; **customised** framework files (only those
  that differ from golden) go under `<ts>/framework-customisations/` so they are
  never confused with restorable defaults; a `MANIFEST.md` at the archive root
  records, per archived path, its class (project-local vs. framework-customisation),
  its original location, the reset timestamp, and the `grimoire-config` values at
  reset time — making the archive self-describing for a manual restore.
- `.gitignore` (Step 2.3): idempotently add `.grimoire-archive/` (only if absent):
  ```
  # Hard-reset archives (recoverable snapshots; never committed)
  .grimoire-archive/
  ```
  Archives are potentially large recovery snapshots, not tracked history;
  recovery is a filesystem copy-back, not a git operation.

---

## Step 3 — Itemised pre-flight summary

Before touching **any** file, print a concrete summary built from Step 1 against
the actual tree:

- the archive destination path (`.grimoire-archive/<ts>/`);
- every project-local path to be **archived-then-cleared**;
- every framework file to be **restored** (and any customised ones archived first);
- the config behaviour in effect (preserve vs. `--reset-config`) and the resulting
  `grimoire-config.json` values (Step 6);
- the re-onboarding behaviour (sentinel re-install + trigger-on-next-prompt, or
  `--reonboard-now`);
- an explicit line: **"git history is NOT modified."**

---

## Step 4 — Explicit per-action confirmation (the guard)

Ask the user to affirmatively confirm with `AskUserQuestion` **after** they have
seen the Step 3 summary. This cites the `CLAUDE.md` §Commits destructive-op rule:

> "Destructive ops (`git reset --hard`, `git push --force`, `git branch -D`)
> require explicit user confirmation each time (per-action)."

- A bare "yes" to any other question does **not** carry over.
- If the user declines, the skill exits having **changed nothing**.

---

## Step 5 — Execute, archive-before-clear

Strict ordering — nothing is cleared until its archive copy exists and the
archive `MANIFEST.md` is written:

1. **Archive.** Copy all project-local files, the current `CLAUDE.md`, the
   current `grimoire-config.json`, the courtesy `.gitignore`, and any customised
   framework files into `.grimoire-archive/<ts>/` (project-local at mirrored
   paths, customised framework files under `framework-customisations/`). Write
   `MANIFEST.md`.
2. **Restore framework to pristine.** Run `workflow-bootstrap --restore` semantics
   over the manifest set — golden skills, hooks, `settings.json`, `push-allowlist`,
   workflows, and the three paradigm content sets. Reuse the existing restore path;
   do not reimplement it. (Restore runs **before** the config write and clearing
   so `.claude/paradigms/` and golden-derived files are in place for Step 6 and the
   subsequent `work-paradigm-switch`.)
3. **Clear / reset project-local files.** Reset `roadmap.md` and
   `version-history.md` to their template placeholders; remove release plans,
   project design docs, `docs/design/ux/`, `ux-demo/`, and project source (all
   already archived in step 1). Restore `docs/design/README.md` to its pristine
   row set.
4. **Restore `CLAUDE.md`** from the pristine scaffold template — sentinel on
   line 1. Re-installing the template `CLAUDE.md` **is** the sentinel re-install;
   there is no separate sentinel-only step.

---

## Step 6 — Write `grimoire-config.json`

The original config was archived in Step 5.1. Rewrite it consistent with the
**current** schema version (never resurrect a stale schema):

| Field | Default (preserve) | With `--reset-config` |
|---|---|---|
| `schema-version` | current schema version | current schema version |
| `name` | preserved | cleared → re-asked at onboarding |
| `work-paradigm.value` | preserved | `"Supervised"` |
| `workflow-variant.value` | preserved | `"Efficient"` |

Default is **preserve** — a hard reset is usually "start the *project* over", not
"I changed my mind about how I want to work"; the paradigm and workflow-variant
are operator ergonomics that rarely change between restarts. The subsequent
`work-paradigm-switch` (driven by onboarding / `workflow-bootstrap --restore`)
re-installs the preserved paradigm's content set into the active paths, so a
preserve-mode reset lands in the same paradigm with pristine content.

---

## Step 7 — Verify

- Confirm line 1 of `CLAUDE.md` is the `GRIMOIRE_ONBOARDING_SENTINEL` literal.
- Confirm the archive exists, contains `MANIFEST.md`, and mirrors every path the
  Step 3 summary listed.
- Confirm `.grimoire-archive/` is in `.gitignore`.
- Confirm framework files match golden (restore completed).
- Confirm project-local files are reset/removed as summarised.

---

## Step 8 — Hand off to onboarding

Default: **arm the sentinel and stop.** Because line 1 of `CLAUDE.md` is now the
sentinel, the user's next prompt triggers the standard onboarding flow
(`onboarding` → `repo-init` → `work-paradigm-switch` → `workflow-bootstrap`) per
`docs/design/onboarding-design.md`. Report that the scaffold is reset and the
next prompt will begin onboarding.

- **`--reonboard-now`:** invoke the `onboarding` skill directly now instead of
  waiting for the next prompt.

No git operations. The user reviews the working-tree changes and commits.

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
- Archiving/clearing the running `hard-reset` skill mid-run, or self-running in
  the agentic-scaffolding dogfood repo (it would archive the product).
- Using an include-list for project source — classify by exclusion so no stray
  file is silently left behind.
- Committing or pushing. This skill reads, copies, and rewrites the working tree;
  the user commits.
