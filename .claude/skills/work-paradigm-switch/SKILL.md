---
name: work-paradigm-switch
description: Install or switch the active Work Paradigm by reading work-paradigm.value from .claude/grimoire-config.json and file-swapping the correct content set from .claude/paradigms/<paradigm>/ into the active paths. Idempotent — exits early if the paradigm is already installed. Also performs the config schema v1→v2 migration (drops work-paradigm.in-development, bumps schema-version). Use when onboarding selects a paradigm, when the user wants to switch paradigms, or when workflow-bootstrap --restore runs. Triggers on "switch paradigm", "activate paradigm", "install paradigm", or when onboarding calls it post-config-write.
---

# Work Paradigm Switch

Installs the content set for the chosen Work Paradigm — or switches from one to
another — by file-swapping paradigm-specific files into their stable active paths.
Idempotent and restorable.

Design authority: `docs/design/work-paradigm-design.md`.

---

## Overview

The Work Paradigm system stores three content sets under `.claude/paradigms/`:
`supervised/`, `weiss/`, and `noir/`. This skill reads `work-paradigm.value`
from `.claude/grimoire-config.json`, resolves the correct content set, and
writes each file to its **stable active path** (see Install Map §2). It also
migrates schema-version 1 configs to version 2.

---

## §1 — Input + alias resolution

**Accepted input values** (case-insensitive):

| Input value | Canonical stored value | Paradigm directory |
|-------------|------------------------|--------------------|
| `Supervised`, `supervised` | `Supervised` | `.claude/paradigms/supervised/` |
| `Weiss`, `weiss`, `Collaborative`, `collaborative` | `Weiss` | `.claude/paradigms/weiss/` |
| `Noir`, `noir`, `Autonomous`, `autonomous` | `Noir` | `.claude/paradigms/noir/` |

Input comes from one of two sources:
1. **Called with an explicit argument** — use the supplied value (caller passes
   the desired paradigm name as the skill argument).
2. **Called with no argument** — read `work-paradigm.value` from
   `.claude/grimoire-config.json`. Resolve aliases and canonicalize.

If the value is missing or unrecognized, default to `Supervised` and warn.

---

## §2 — Install map

For each entry below, copy the source file to the target active path:

| Source (inside `.claude/paradigms/<slug>/`) | Target active path |
|---------------------------------------------|--------------------|
| `project-manager-SKILL.md` | `.claude/skills/project-manager/SKILL.md` |
| `integration-master-SKILL.md` | `.claude/skills/integration-master/SKILL.md` |
| `release-phase-SKILL.md` | `.claude/skills/release-phase/SKILL.md` |
| `release-phase-merge-SKILL.md` | `.claude/skills/release-phase-merge/SKILL.md` |
| `CLAUDE-agent-role.md` | Body of `CLAUDE.md §Which agent are you?` (sentinel replacement) |
| `CLAUDE-task-execution.md` | Body of `CLAUDE.md §Task execution` (sentinel replacement) |
| `integration-workflow.md` | `docs/integration-workflow.md` |
| *(value substitution — §4.5b)* | `> **Paradigm:** <name>` stamp line in `CLAUDE.md` |

---

## §3 — Idempotency check

Before making any changes:

1. Read `work-paradigm.value` from `.claude/grimoire-config.json`.
2. Compare each active file (target path) byte-for-byte against its paradigm
   source file.
3. If **all** files match and `schema-version` is already `2` → print
   "Work paradigm <Paradigm> is already active. No changes made." and exit.

If any file differs, or if `schema-version` is `1`, proceed with the full
install.

---

## §4 — Installation steps

Execute in order:

### 4.1 Validate paradigm directory

```
PARADIGM_DIR=.claude/paradigms/<slug>/
```

If the directory does not exist:
- Abort with:
  > "Error: paradigm directory not found at <PARADIGM_DIR>. Run
  > `workflow-bootstrap --restore` to restore the content sets."
- Do not proceed.

### 4.2 Install skill files (overwrite)

For each skill-file entry in the install map:
- If the source file is missing, log a warning (`Warning: source file
  <path> missing — skipping`) and continue to the next entry. A partial
  install is recoverable.
- Otherwise, overwrite the target file with the source file's content.
  Create the target directory if it does not exist.

### 4.3 Install `CLAUDE.md` sections (sentinel replacement)

Two sections in `CLAUDE.md` are replaced using sentinel comments:

```
<!-- PARADIGM_SECTION:agent-role:start -->
…content replaced here…
<!-- PARADIGM_SECTION:agent-role:end -->

<!-- PARADIGM_SECTION:task-execution:start -->
…content replaced here…
<!-- PARADIGM_SECTION:task-execution:end -->
```

The section **heading** (`## Which agent are you?` / `## Task execution`) lives
in `CLAUDE.md` *above* the `:start` marker and is **never swapped**. The source
files (`CLAUDE-agent-role.md` / `CLAUDE-task-execution.md`) are **body-only**:
they contain the start marker, the body, and the end marker — **no `##`
heading**. (Including the heading in the source would duplicate it on install —
the defect the v1.6 Phase-2 vet caught.)

For each section:
1. Read `CLAUDE.md` into memory.
2. Locate the `start` sentinel line and the `end` sentinel line.
3. Replace everything from the `start` sentinel line **through** the `end`
   sentinel line (**inclusive** of the marker lines) with the full content of
   the source file (which itself begins with the `start` marker and ends with
   the `end` marker). The heading above the block is left untouched.
4. Write the result back to `CLAUDE.md`.

If a sentinel is missing from `CLAUDE.md`:
- Log a warning and skip that section (do not abort the whole install).

### 4.4 Install `docs/integration-workflow.md` (overwrite)

Overwrite `docs/integration-workflow.md` with the source
`integration-workflow.md` from the paradigm directory.

If the source file is missing, log a warning and skip (partial install).

### 4.5 Update `.claude/grimoire-config.json`

Read the current config. Apply these changes:

- Set `work-paradigm.value` to the canonical form (e.g. `"Supervised"`).
- Remove `work-paradigm.in-development` (this field does not exist in
  schema-version 2).
- Set `schema-version` to `2`.
- Leave all other fields unchanged.

Write the updated config back to `.claude/grimoire-config.json`.

Schema-version 2 example:

```json
{
  "schema-version": 2,
  "name": "<project name>",
  "work-paradigm": {
    "value": "Supervised"
  },
  "workflow-variant": {
    "value": "Efficient",
    "in-development": true
  }
}
```

### 4.5b Refresh the `CLAUDE.md` paradigm stamp

The `## Work Paradigm` section in `CLAUDE.md` is preceded by an always-loaded
breadcrumb stamp (delivered by `workflow-bootstrap`/`onboarding`):

```markdown
> **Paradigm:** <name> — one of Supervised · Weiss · Noir.
> Switch via the `work-paradigm-switch` skill. See `.claude/paradigms/README.md`.
```

Match the `> **Paradigm:** <old> —` line and substitute the canonical
`<Paradigm>` for `<old>`. This is a **value substitution only** — match-and-
replace the name, never append a second stamp; the rest of the block and the
breadcrumb index are unchanged. If the stamp line is absent (a pre-stamp
project), log a warning and skip — `workflow-bootstrap` delivers it on its next
run. Safe to repeat (no-op if the value already matches).

### 4.6 Confirm

Print:

> "Work paradigm switched to <Paradigm>. Active files updated."

---

## §5 — v1→v2 config migration

When the skill encounters a `schema-version: 1` config (or a config with no
`schema-version`):

- Treat `work-paradigm` as advisory (it was `in-development` — valid values
  may be v1 aliases: `Autonomous` → `Noir`, `Collaborative` → `Weiss`).
- Resolve the alias to its canonical v2 name.
- Proceed with the full install.
- The config update in §4.5 writes `schema-version: 2`, completing the
  migration.

This is the **only** migration path. No automated migration runs silently
without the switch skill being called.

---

## §6 — Restorability

This skill is safe to call from `workflow-bootstrap --restore`:

1. `workflow-bootstrap` restores the paradigm content sets to
   `.claude/paradigms/` from the golden baseline.
2. Then calls this skill (with no argument) to re-install the active paradigm.

If `.claude/grimoire-config.json` is missing or `work-paradigm` is unset,
default to `Supervised`.

---

## Error conditions summary

| Condition | Behavior |
|-----------|----------|
| Paradigm directory missing | Abort; print restore instruction |
| Source file missing | Warn + skip that entry; continue |
| CLAUDE.md sentinel missing | Warn + skip that section; continue |
| CLAUDE.md paradigm stamp missing | Warn + skip §4.5b; continue (bootstrap delivers it) |
| Config file missing | Default to Supervised; warn |
| Unrecognized paradigm value | Default to Supervised; warn |

---

## Anti-patterns

- Running the installer against `.claude/paradigms/` directly from agent
  context — agents must never load paradigm source files during normal
  operation (leanness principle).
- Silently skipping errors without logging a warning.
- Calling this skill from a task-agent session — paradigm switching is an
  integration-master / onboarding operation.
- Modifying any section of `CLAUDE.md` outside the sentinel brackets.
