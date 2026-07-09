---
name: grm-regenerate-grimoire
description: Surgically regenerate the Grimoire framework layer in place — delete+restore pure-framework files from the workflow-bootstrap golden baseline, split/merge mixed files (settings, CLAUDE/AGENTS, .gitignore, roadmap, version-history) so project content survives, archive-then-restore for safety, idempotent. The middle path between install-doctor --repair and hard-reset. Use to repair or refresh a damaged framework layer while keeping project work.
---

# Regenerate-grimoire

Restores the **framework layer only, in place** — preserving every project file
— with an idempotency guarantee and archive-then-restore safety. It is the
**surgical middle path** between `install-doctor --repair` (per-file, no
whole-layer guarantee) and `grm-hard-reset` (archives *everything*, re-onboards from
zero). Full contract: `docs/grimoire/design/clean-room-design.md` §2 (mixed-file
split/merge) and §3 (surgical-regenerate).

> **This skill mutates the framework layer.** It deletes and restores
> pure-framework files and rewrites the framework portion of mixed files. It
> **archives every touched file first** (`.grimoire-archive/<ts>/` with a
> `MANIFEST.md`), so a run is always recoverable, but it is still governed by
> the `CLAUDE.md` §Commits destructive-op rule and **never mutates without an
> explicit per-action confirmation** — pass `--yes` (or confirm interactively).
> A trigger phrase only *enters* the skill; it does not authorise the run.

## When to use it vs. the neighbours

| Situation | Use |
|---|---|
| Audit only — what's MISSING/DRIFTED? | `grm-install-doctor` (read-only) |
| One/few drifted files | `install-doctor --repair` |
| **Framework layer damaged; fix it, keep my work** | **`grm-regenerate-grimoire`** |
| Wipe scaffold back to not-yet-onboarded | `grm-hard-reset` |

`grm-install-doctor` may **delegate** to this skill as its whole-layer, mixed-aware
remediation.

## What it does (set partition from `.claude/grimoire-files.json`)

The CR-4 manifest partitions every Grimoire-owned file into three classes; the
script reads it as the authoritative partition source:

- **pure-framework → delete + restore from golden.** No project content, so the
  file is deleted and re-copied from
  `.claude/skills/grm-workflow-bootstrap/golden/`. Loss-free.
- **mixed → split/merge in place** (never blind-replaced; see below).
- **project-owned → preserve untouched.** `docs/design/**`, project source /
  tests, and any existing `.grimoire-archive/**` are never read-for-write,
  deleted, or moved.

### Mixed-file merges (clean-room-design.md §2)

Each merger is **idempotent** (run 2 = clean diff) and never drops project
content:

| File | Merge |
|---|---|
| `.claude/settings.json` | **3-way**: project keys (`env`, project `allow`/`deny`, custom hooks) preserved verbatim; framework `permissions.allow` allowlist + `hooks` block reset to golden. Never widens beyond framework scope. |
| `CLAUDE.md` / `AGENTS.md` | **section + sentinel aware**: framework sections restored from golden; filled-in project placeholders (e.g. a resolved `{test-command}`) re-injected, not reset to `{…}`; onboarding sentinel preserved if line 1 already carries it, **never re-armed** on a clean project. |
| `.gitignore` | **section-merge**: only the `# >>> grimoire-managed >>>` … `# <<< grimoire-managed <<<` block is replaced; project lines and order untouched; appended if absent; no duplicate on re-run. |
| `docs/roadmap.md` | **baseline-row reconcile**: missing framework baseline rows restored; project rows never deleted or reordered. |
| `docs/version-history.md` | **audience-branched**: a consumer gets the empty seed if absent/empty, existing entries never overwritten; the Grimoire **root** copy (the framework release log) is left untouched. |

### Archive-then-restore (clean-room-design.md §3)

1. **Pre-flight summary** — enumerate the delete / merge / preserve sets before
   any write.
2. **Archive first** — copy every delete-set **and** merge-set file to
   `.grimoire-archive/<ts>/` (UTC `YYYYMMDD-HHMMSS`, repo-relative paths, with a
   `MANIFEST.md` recording class + original path + reason = `regenerate`),
   reusing `grm-hard-reset`'s layout. The preserve-set is **not** archived.
3. **Restore / merge** — delete+restore the pure-framework set, then apply the
   per-file mixed merges.
4. **Rollback** — on any failure, restore the archived originals over the
   partially-modified tree. Because the archive precedes any mutation, a crash
   leaves a recoverable copy. Never `--force`-delete without an archive in hand.

After restoring the paradigm content sets, re-apply the active paradigm via
`grm-work-paradigm-switch` (the rendered copy is restored from golden, the active
selection is re-installed) — matching `workflow-bootstrap --restore`.

## How to run

```bash
# Dry-run: report the partition + what would change, write nothing.
python3 .claude/skills/grm-regenerate-grimoire/regenerate_grimoire.py --check

# Live run (mutates; archives first). --yes authorises the mutation.
python3 .claude/skills/grm-regenerate-grimoire/regenerate_grimoire.py --yes

# Offline self-test (tempdir round-trip + idempotency + every merger).
python3 .claude/skills/grm-regenerate-grimoire/regenerate_grimoire.py --self-test
```

`--root ROOT` targets a specific repo root (default: auto-detected by walking up
to `.claude/grimoire-files.json`). Stdlib-only; no third-party dependencies.

## Flavor support

| Flavor | Support |
|---|---|
| `claude-code` (canonical) | Full. |
| root (this dogfood repo) | Full; `version-history.md` is left as Grimoire's own log. |
| `copilot` | **Not supported.** Regenerate restores from the `workflow-bootstrap/golden/` baseline, and the **copilot flavor ships no `golden/` tree** — there is nothing to restore from. The script detects the absent golden baseline and **refuses with a clear message (exit 2)** rather than half-running. A consumer who needs a copilot framework refresh should re-sync from upstream. |

## Idempotency guarantee

A second consecutive run with no intervening edits produces a **clean diff**:
pure-framework files are already at golden, the mixed merges are idempotent, and
project-owned files were never touched. The `--self-test` asserts this on a
tempdir fixture (seed → corrupt/delete framework files → regenerate → assert
framework restored AND project + mixed-project content preserved → second run =
no change).
