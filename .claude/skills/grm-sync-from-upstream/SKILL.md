---
name: grm-sync-from-upstream
description: Direction — this repo's upstream scaffolding → a bootstrapped project (opposite of grm-sync-from-source: dev project → this repo). Pull workflow updates from the published upstream distribution into this project without destroying local customizations. Uses a 3-way merge against a recorded base; collisions surface as git conflict markers. Use to update a project bootstrapped from this scaffolding, or to pull the latest skills from upstream.
---

# Sync-from-upstream

**Direction: upstream scaffolding → your bootstrapped project.** The mirror
image is `grm-sync-from-source` (a dev project → this scaffolding repo); don't
confuse the two by name — this one only ever pulls inbound to *your* project.

Brings workflow improvements **from** the published upstream scaffolding (this
starter kit, hosted on GitHub) **into** a project that was bootstrapped from it
— **without clobbering local customizations**. It is the mirror image of
`grm-sync-from-source` (which goes project → scaffolding and re-*generalizes*).
Here the direction is upstream → your project, and copied files are
re-*specialized*.

It pairs a script (`sync-from-upstream.sh`, mechanical + safe 3-way merging)
with the judgment this skill supplies: resolving real conflicts, re-filling
placeholders in newly-added generic files, and deciding what to keep.

> **Non-destructive is the whole point.** The script never silently overwrites
> a file you have customized: clean upstream changes auto-apply, collisions
> become git conflict markers (both sides preserved), and a differing file with
> no recorded base is reported, not overwritten. Every rewrite is backed up.
>
> **One deliberate exception — guard hooks (v3.90).** Upstream files under
> `.claude/hooks/` are **REPLACED wholesale** on `--apply` (backed up, loudly
> reported), never 3-way merged. Project behavior belongs in
> `.claude/grimoire-config.json`, never in hand-edits to hook code.

> **Upgrading a pre-v3.42 project?** Skill names below carry the `grm-` prefix;
> a pre-v3.42 project only has the OLD bare names on disk — invoke skills bare
> until the `skill-namespacing` adopt step completes in Step 4.5. Full rule:
> `reference.md` "Pre-v3.42 projects: use bare skill names…" (#200).

---

## Step 0 — Safety preconditions

1. **Clean tree.** The script refuses `--apply` on a dirty git tree (commit or
   stash first; `--force` only if you truly mean it). Only **tracked** changes
   count as dirty — untracked files (gitignored archive/source dirs, scratch
   output) never block `--apply` and need no flag (#143).
2. **Backups.** On `--apply`, every rewritten file is copied to
   `.scaffold-sync-backup/<timestamp>/`. Git-ignore that directory.
3. **Provenance.** The 3-way merge needs a *base* — a snapshot of the upstream
   state your local copy descends from, kept in `.scaffold-base/`. Commit
   `.scaffold-base/` and `.scaffold-upstream.conf` so the provenance travels
   with the repo.

### BMI-3 boundary rules (where a sync may run)

`--apply` runs only on the integration line (`branch-model.integration-branch`,
default `dev`) and refuses when `main` carries work the line lacks (a real
fork) — the whole safety property, never relaxed. Being merely **ahead** of
`main` proceeds by default (#419) — no flag, no token required. `--apply` also
best-effort **self-updates** itself from upstream `main` (#443) first. Full
rule + recovery: `reference.md` BMI-3 rules.

---

## Step 1 — Configure the upstream, and establish a base

> **v1.13+ projects:** `.scaffold-upstream.conf` is seeded automatically by
> `grm-workflow-bootstrap` with `UPSTREAM_REPO=https://github.com/rhohn94/grimoire-framework.git`.
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
project descends from a fork. `grm-workflow-bootstrap` will not overwrite a
non-empty value, so set it once and it persists across future bootstrap runs.

## Step 2 — Dry-run and review

```bash
.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh           # report only
.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh --diff    # + full diffs
```

| Marker | Meaning | Your job |
|---|---|---|
| `NEW`      | Upstream has a file you don't. | Added on `--apply`; then re-specialize its placeholders. |
| `in-sync`  | Identical. | Nothing. |
| `UPDATE`   | You never edited it; upstream changed. | Applied automatically (fast-forward). |
| `local`    | You customized it; upstream unchanged. | Kept your version. |
| `MERGED`   | Both changed, no overlap. | Auto-merged — review the combined diff. |
| `CONFLICT` | Both changed the same region. | Git markers on `--apply`; resolve by hand (re-run, or `--mark-resolved` / `--all-resolved`). |
| `RESOLVED` | A re-presenting `CONFLICT` you already hand-resolved. | Base auto-advanced (#420), LOCAL untouched — nothing to do. |
| `REVIEW`   | Differs, but no recorded base. | Kept local. Reconcile by hand, or `--adopt-base` if your copy already matches upstream. |
| #180       | Never-blocking warning (see Step 4). | Missing-symbol. |

## Step 3 — Apply

```bash
.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh --apply
```

Writes new files, fast-forward `UPDATE`s, and clean `MERGED` results; writes
conflict markers for `CONFLICT` files; backs up everything it rewrites. It
advances the base for files that landed cleanly, and (#420) for a `CONFLICT`
re-presenting an already-resolved fix — a genuinely new/unresolved `CONFLICT`
keeps its old base so a re-run finishes the job after you resolve it.

---

## Step 4 — Resolve and re-specialize

- **CONFLICT files:** resolve the conflict markers, remove them, commit — the
  **next** `--apply` auto-advances that file's base for you (#420), so the same
  conflict never re-presents. To advance it right away, or for several files at
  once, use **`--mark-resolved <file>`** or **`--all-resolved`**. Detail plus
  the #180/#181 warnings: see `reference.md` Merge-walk warnings.
- **NEW files:** they arrive **generic** (placeholder-laden). Re-specialize
  them for this project — fill `{test-command}`, `{build-command}`,
  `{release-command}`, doc-map rows, etc. Running the **`grm-workflow-bootstrap`**
  skill is the easiest way; or edit by hand.
- **MERGED files:** read the combined diff to confirm the auto-merge makes
  sense.
- **`CLAUDE.md`:** this file is excluded from the sync walk (it is
  project-specific and requires the most invasive re-specialization). When
  `CLAUDE.md` changes upstream, port the relevant sections manually.

---

## Step 4.5 — Feature adoption phase

After a clean `--apply` (zero unresolved CONFLICT files), run
`adoption_delta.py --format table`; read only its output, never the raw table.

> **Stale-namespacing block (v3.90).** The report flags bare-named skill dirs
> coexisting with `grm-*` twins. That cutover is a **migration**: offer it,
> never auto-run (Noir included). See `reference.md` Step 4.55.

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

## Step 4.6 — Refresh `.grimoire-source/`

After a clean `--apply` (zero unresolved CONFLICT files), refresh the pristine
generation-source folder at the **project root**:

1. If `.grimoire-source/` exists → re-copy the framework source artifacts into
   it (same conservative scope as `grm-workflow-bootstrap` Step 2.6: all
   `SKILL.md` files and `docs/grimoire/` structural docs).
2. If `.grimoire-source/` is absent → create it and populate it (same as a
   first-time bootstrap).

This ensures the clean generation source stays aligned with the updated
framework after each upstream sync. The folder is gitignored; do not stage or
commit anything inside it.

---

## Step 4.7 — Offer docs migration (if old-style docs detected)

After a clean `--apply` (zero unresolved CONFLICT files), run `docs_migrate.py`
in detect mode to check whether the project has old-style docs:

```bash
python3 .claude/skills/grm-docs-migrate/docs_migrate.py
```

**If findings exist:**

1. Print the finding count and a brief summary (file paths + codes).
2. **Offer** to run `--apply`: "Found N old-style doc finding(s). Run
   `docs_migrate.py --apply` to insert breadcrumbs and rewrite absolute links
   (archive-first, idempotent)? [Yes / No]"
3. On Yes — run `docs_migrate.py --apply` and report results.
4. On No — skip silently; findings remain for a future sync.

**Never auto-run**, regardless of paradigm (including Noir). Migration rewrites
user-owned docs and must always be explicitly confirmed.

**Under Stealth Mode (`stealth-mode.value: "on"`):** suppress the offer
entirely — do not run `docs_migrate.py` or print any migration prompt.

---

## Step 5 — Report and commit

Report: files added / updated / merged / conflicted / kept-local, the backup
directory, any placeholders still needing values, whether `.grimoire-source/`
was refreshed, and the docs-migration outcome (ran / skipped / not offered).
Then the user reviews and commits — including the advanced `.scaffold-base/`.
No commits from this skill.

---

## Reference (load on demand — all sections below live in `reference.md`)

- `When to use this skill`
- `Feature manifest — v3.53 additions (standard-justfile-recipes)`
- `Pre-v3.42 projects: use bare skill names until the sync completes (#200)`
- `Anti-patterns`
- `Stale-upstream rename detection (non-destructive)`
- `Recognized sync artifact — `.claude/component-registry.json``
- `Merge-walk warnings (#180 / #181)`
- `What the script tells you`
- `How to evaluate the manifest`
- `Advancing `framework-version``
- `Paradigm-file update caveat`
- `When the adoption phase is a no-op`
- `BMI-3 boundary rules (full)`
- `Step 4.55 — Complete the grm- skill namespacing (remove bare-named survivors)`
