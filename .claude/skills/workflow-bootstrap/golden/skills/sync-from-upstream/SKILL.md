---
name: sync-from-upstream
description: Pull workflow updates FROM the published upstream scaffolding distribution INTO this project, without destroying local customizations. Uses a 3-way merge against a recorded base so clean upstream changes apply automatically while your edits are preserved; genuine collisions surface as git conflict markers. Use to update a project that was bootstrapped from this scaffolding. Triggers on "sync from upstream", "update the scaffolding", "pull scaffold updates", "get the latest skills from upstream", "refresh from the scaffolding repo".
---

# Sync-from-upstream

Brings workflow improvements **from** the published upstream scaffolding (this
starter kit, hosted on GitHub) **into** a project that was bootstrapped from it
— **without clobbering local customizations**. It is the mirror image of
`sync-from-source` (which goes project → scaffolding and re-*generalizes*).
Here the direction is upstream → your project, and copied files are
re-*specialized*.

It pairs a script (`sync-from-upstream.sh`, mechanical + safe 3-way merging)
with the judgment this skill supplies: resolving real conflicts, re-filling
placeholders in newly-added generic files, and deciding what to keep.

> **Non-destructive is the whole point.** The script never silently overwrites
> a file you have customized: clean upstream changes auto-apply, collisions
> become git conflict markers (both sides preserved), and a differing file with
> no recorded base is reported, not overwritten. Every rewrite is backed up.

---

## When to use this skill

- A project was started by copying `claude-code/` (or `copilot/`) out of this
  scaffolding, you've since customized it, and upstream has improved.
- You want the project to benefit from upstream skill/hook/doc fixes without
  losing your filled-in commands, branch names, or local edits.

Do **not** use it to push *from* a project into the scaffolding — that's
`sync-from-source`. Do not run it inside the scaffolding repo itself.

---

## Step 0 — Safety preconditions

1. **Clean tree.** The script refuses `--apply` on a dirty git tree (commit or
   stash first; `--force` only if you truly mean it).
2. **Backups.** On `--apply`, every rewritten file is copied to
   `.scaffold-sync-backup/<timestamp>/`. Git-ignore that directory.
3. **Provenance.** The 3-way merge needs a *base* — a snapshot of the upstream
   state your local copy descends from, kept in `.scaffold-base/`. Commit
   `.scaffold-base/` and `.scaffold-upstream.conf` so the provenance travels
   with the repo.

---

## Step 1 — Configure the upstream, and establish a base

> **v1.13+ projects:** `.scaffold-upstream.conf` is seeded automatically by
> `workflow-bootstrap` with `UPSTREAM_REPO=https://github.com/rhohn94/grimoire-framework.git`.
> For a fresh project you typically only need to verify the URL is correct (and
> adjust it if you are working from a fork). Skip ahead to "First run" below
> once the file exists.

Create (or verify) `.scaffold-upstream.conf` at the project root:

```sh
UPSTREAM_REPO=https://github.com/rhohn94/grimoire-framework.git  # default; override for forks
UPSTREAM_REF=main          # optional: branch / tag / sha
# FLAVOR is auto-detected from this project's own layout (.claude/ →
# claude-code, .github/ → copilot), so you normally don't set it. Add
# `FLAVOR=claude-code` (or copilot) only to override an ambiguous detection.
```

**Fork override:** replace `UPSTREAM_REPO` with your own upstream URL if this
project descends from a fork. `workflow-bootstrap` will not overwrite a
non-empty value, so set it once and it persists across future bootstrap runs.

### Stale-upstream rename detection (non-destructive)

The scaffolding repo was renamed `agentic-scaffolding` → `grimoire-framework`.
A project pinned before that rename also predates the multi-paradigm system, so
on every run the script checks `UPSTREAM_REPO` and, **if it still contains the
substring `agentic-scaffolding`**, prints a pre-sync notice that:

- names the rename and gives the exact new URL
  (`https://github.com/rhohn94/grimoire-framework.git`) plus the one-line
  repoint instruction (edit `UPSTREAM_REPO` in `.scaffold-upstream.conf`);
- points at the **paradigm system** now available for pre-paradigm scaffolds —
  the `work-paradigm-switch` skill and `.claude/paradigms/README.md`.

It is **non-destructive**: the conf is never rewritten silently — the notice
only reports and *offers* the exact repoint line for you to apply. It is a
**no-op** once `UPSTREAM_REPO` already targets `grimoire-framework`, and it does
not change sync results or exit codes (pre-sync notice only).

**First run on an already-customized project:** there is no base yet, so every
differing file would report `REVIEW` (kept local, not merged). Once you have
confirmed the project is reconciled with a known upstream commit, record that
commit as the base so future syncs can 3-way merge:

```bash
.claude/skills/sync-from-upstream/sync-from-upstream.sh --adopt-base
```

`--adopt-base` snapshots the current upstream into `.scaffold-base/` and
**touches no local file**.

---

## Step 2 — Dry-run and review

```bash
.claude/skills/sync-from-upstream/sync-from-upstream.sh           # report only
.claude/skills/sync-from-upstream/sync-from-upstream.sh --diff    # + full diffs
```

| Marker | Meaning | Your job |
|---|---|---|
| `NEW`      | Upstream has a file you don't. | Added on `--apply`; then re-specialize its placeholders. |
| `in-sync`  | Identical. | Nothing. |
| `UPDATE`   | You never edited it; upstream changed. | Applied automatically (fast-forward). |
| `local`    | You customized it; upstream unchanged. | Kept your version. |
| `MERGED`   | Both changed, no overlap. | Auto-merged — review the combined diff. |
| `CONFLICT` | Both changed the same region. | Git markers written on `--apply`; resolve by hand. Base is **not** advanced until you do. |
| `REVIEW`   | Differs, but no recorded base. | Kept local. Reconcile by hand, or `--adopt-base` if your copy already matches upstream. |

### Recognized sync artifact — `.claude/component-registry.json`

The versioned **component registry** (`.claude/component-registry.json`, schema
in `docs/design/component-catalog-architecture-design.md` Pillar 1) is a
**recognized, merged sync artifact** — Pillar 4 (Distribution) of the
component-catalog architecture. It distributes over **this existing sync channel,
with no hosted endpoint**.

- It is **not excluded** (it is not in `is_excluded`), so the file-merge walk
  carries it like any other managed file: a `NEW` registry from upstream is
  added; a registry both sides changed is **3-way merged** against the recorded
  base, so **local components are preserved and upstream components are
  added/updated** — never clobbered. A genuine same-region collision (e.g. both
  sides edited the same component entry) surfaces as a `CONFLICT` for hand
  resolution, exactly like any other file.
- Because the JSON is a `components` map keyed by component-id, disjoint
  additions on each side merge cleanly (`MERGED`); the merge is *by version*
  through the normal diff — re-syncing an **unchanged** upstream registry is a
  **no-op**.
- The **derived matrix** (`.claude/cache/component-compatibility.json`) is
  **not** distributed — `.claude/cache/` is gitignored and regenerable from the
  registry by the `component-registry` skill after a sync changes it.
- No `feature-manifest.md` row is added here. A `component-registry` adopt row
  (idempotent adopt step) is owned by **D2** (the closeout/flavor-mirror item);
  see the report. Until that row lands, the registry still distributes via the
  file-merge walk above — the manifest row only adds the post-sync *adopt/regen*
  prompt.

---

## Step 3 — Apply

```bash
.claude/skills/sync-from-upstream/sync-from-upstream.sh --apply
```

Writes new files, fast-forward `UPDATE`s, and clean `MERGED` results; writes
conflict markers for `CONFLICT` files; backs up everything it rewrites. It
advances the base only for files that landed cleanly — `CONFLICT` files keep
their old base so a re-run finishes the job after you resolve them.

---

## Step 4 — Resolve and re-specialize

- **CONFLICT files:** open each, resolve the `<<<<<<< local / ======= /
  >>>>>>> upstream` markers (keep your customization, take upstream's
  improvement, or blend), remove the markers, then re-run the script so the
  base advances past the resolved file.
- **NEW files:** they arrive **generic** (placeholder-laden). Re-specialize
  them for this project — fill `{test-command}`, `{build-command}`,
  `{release-command}`, doc-map rows, etc. Running the **`workflow-bootstrap`**
  skill is the easiest way; or edit by hand.
- **MERGED files:** read the combined diff to confirm the auto-merge makes
  sense.
- **`CLAUDE.md`:** this file is excluded from the sync walk (it is
  project-specific and requires the most invasive re-specialization). When
  `CLAUDE.md` changes upstream, port the relevant sections manually.

---

## Step 4.5 — Feature adoption phase

After a clean `--apply` (zero unresolved CONFLICT files), the script prints an
adoption phase report. Act on it as follows:

### What the script tells you

The script prints the `framework-version` recorded in
`.claude/grimoire-config.json` (or notes it is absent), emits the manifest
path, and summarizes the evaluation procedure. It does **not** run `detect`
predicates itself — that is your job as the agent.

### How to evaluate the manifest

1. Read `.claude/skills/sync-from-upstream/feature-manifest.md`.
2. **Delta computation:**
   - *With `framework-version`*: collect entries where `introduced-in` >
     `framework-version`. Run each entry's `detect` predicate; skip entries
     where `detect` returns true (already adopted).
   - *Without `framework-version`*: collect **all** entries. Run each
     `detect`; skip entries that return true.
3. Sort remaining entries by `introduced-in` ascending (oldest first —
   later features may depend on config set by earlier ones).

### Per-feature adoption loop

| Paradigm | Behaviour |
|---|---|
| **Noir** | Auto-run `adopt` without prompting. Log the feature-id and summary before running. After all adoptions complete, offer `migrate` (if any) with a single confirmation prompt — Noir asks once. |
| **Supervised** | Print the feature summary and prompt: `"Adopt <feature-id> (<summary>)? [Yes / No / Details]"`. On Details, print the full `adopt` prose, then re-ask. On No, skip this run (will be re-offered at next sync). |
| **Weiss** | Same as Supervised — offer each adoption individually. |

Check the active paradigm in `.claude/grimoire-config.json` →
`work-paradigm.value`.

### Adoption ≠ Migration — the load-bearing rule

| | Adoption | Migration |
|---|---|---|
| Writes | Config only (`.claude/grimoire-config.json`, skill config) | Existing user data (roadmap bullets, issue text, etc.) |
| Auto-run | Yes (Noir) | **Never** — always explicitly confirmed |
| Confirmation | Per-feature (Supervised/Weiss) | Always, even under Noir |
| Reversibility | Config rollback | Requires pre-migration backup |

If a manifest step reads or writes user data that existed before the sync, it
is **migration** — never merge it into `adopt`. Migration is always offered
separately, after adoption completes, with a backup before any data moves.

### Advancing `framework-version`

After the adopt loop completes without errors:

1. Determine the upstream's current version (e.g. from the manifest's highest
   `introduced-in` value, or from the upstream release tag).
2. If every feature up to that version was adopted successfully or
   `detect`-confirmed as already-adopted, write:
   ```json
   "framework-version": "<upstream-version>"
   ```
   into `.claude/grimoire-config.json`. **This is the only code path that
   writes `framework-version`** — the file-merge walk never touches it
   (`.claude/grimoire-config.json` is excluded from the sync walk).
3. If any feature errored or was skipped due to failure, advance
   `framework-version` only to the last fully-adopted version boundary. The
   next sync run will re-evaluate the failed feature by `detect` and resume.
4. User declining an optional adoption does **not** block `framework-version`
   advancement (the user made a conscious choice).

### Paradigm-file update caveat

If any file under `.claude/paradigms/` was `UPDATE`d during this sync, the
active paradigm content in its live paths (installed by `work-paradigm-switch`)
may be stale. After the adoption phase, remind the user:

> Paradigm files updated. Re-run `work-paradigm-switch` to re-install the
> active paradigm (`<paradigm-name>`) into its live paths.

This is a reminder, not an automated action.

### When the adoption phase is a no-op

If `detect` returns true for every manifest entry (all features already
adopted), print:

> Adoption phase: all features up to vX.Y are already adopted.

Then advance `framework-version` as above.

---

## Step 4.6 — Refresh `.grimoire-source/`

After a clean `--apply` (zero unresolved CONFLICT files), refresh the pristine
generation-source folder at the **project root**:

1. If `.grimoire-source/` exists → re-copy the framework source artifacts into
   it (same conservative scope as `workflow-bootstrap` Step 2.6: all
   `SKILL.md` files and `docs/grimoire/` structural docs).
2. If `.grimoire-source/` is absent → create it and populate it (same as a
   first-time bootstrap).

This ensures the clean generation source stays aligned with the updated
framework after each upstream sync. The folder is gitignored; do not stage or
commit anything inside it.

---

## Step 5 — Report and commit

Report: files added / updated / merged / conflicted / kept-local, the backup
directory, any placeholders still needing values, and whether `.grimoire-source/`
was refreshed. Then the user reviews and commits — including the advanced
`.scaffold-base/`. No commits from this skill.

---

## Anti-patterns

- `--force` onto a dirty tree to "just get it done" — defeats the protection.
- Committing a file that still has `<<<<<<<` conflict markers — resolve first.
- `--adopt-base` to *skip* a real reconciliation — it declares "local already
  matches upstream"; only use it when that is true.
- Forgetting to re-specialize a `NEW` generic file — it will carry raw
  `{placeholder}` tokens until you do.
- Running it inside the scaffolding repo itself (wrong direction — use
  `sync-from-source`).
- Deleting local-only files to "match upstream" — the sync is additive; your
  project-specific files are not upstream's concern.
