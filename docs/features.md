# Grimoire — Feature Reference

> **Up:** [↑ Docs](README.md)


> A complete reference of what the Grimoire scaffolding provides for adopters.
> If you are just getting started, read the [Quickstart](quickstart.md) first —
> it guides you through initialization and links back here for detail.

---

## Overview

Grimoire is a Claude Code scaffolding framework. Copy it into a project root
and you gain a structured branch model, a curated skill set, multi-agent
`Workflow` scripts, and guard hooks — all wired together and ready to use from
the first prompt.

---

## 1. Skills

Skills are reusable Claude Code instruction sets invoked with a `/skill-name`
command (or by the integration master on your behalf). They encode the
accumulated know-how of the framework so you never have to re-discover
the right sequence of steps.

### Release lifecycle skills

| Skill | Purpose |
|---|---|
| `grm-release-planning` | Generate a work-items report for the next release. Reads design docs, the roadmap, and carryovers; produces a sized, structured list. |
| `grm-release-agreement` | Finalise and lock a release plan after the user approves the report. Creates `docs/release-planning-vX.Y.md`, creates the `version/X.Y` staging branch, and sets up the §5 ledger. |
| `grm-release-phase` | Spawn work-item sessions (via `spawn_task`) for the next open phase. Groups work by dependency, sizes each item by token estimate, and assigns model/effort. |
| `grm-release-phase-merge` | Merge completed subagent branches into the staging branch, run tests after each merge, and tick §5 ledger rows. When all phases are done, merges `version/X.Y` into `dev` and cleans up. |
| `grm-project-release` | Promote `dev` to `main` and tag a new version. The final step in the release cycle. |

### Onboarding and initialization skills

| Skill | Purpose |
|---|---|
| `grm-repo-init` | Initialize the branch model (`main`, `dev`) and install guard hooks into a fresh project. Safe to re-run — skips steps that are already complete. |
| `grm-workflow-bootstrap` | Fill in project-specific settings after `grm-repo-init`: test command, build command, release command, doc-location map, and more. Also restores `.claude/workflows/` scripts. |
| `grm-onboarding` | Run (or re-run) the first-run interview. Captures the project name, the active work paradigm, and the workflow-variant preview, writes `.claude/grimoire-config.json`, then calls `grm-repo-init` + `grm-workflow-bootstrap` and bridges into first-release planning. |

### Design and standards skills

| Skill | Purpose |
|---|---|
| `grm-design-doc-scaffold` | Create a new feature design doc under `docs/design/` using the house layout and wire it into the index. |
| `grm-repo-reference` | Look up the doc-location map and the subagent model/effort assignment table for this project. |
| `grm-source-to-design-docs` | Analyse existing source code and synthesise design docs from what is already built. |
| `grm-sync-from-upstream` | Pull framework updates from the Grimoire upstream into a project that adopted an earlier version. |

### UX skills

| Skill | Purpose |
|---|---|
| `grm-design-language-adapt` | Establish or refresh the per-project UX design language under `docs/design/ux/design-language.md`. Reads from a configurable upstream source. |
| `grm-ux-demo-build` | Build a minimal demo page proving the adapted design language works in the project's own stack (opt-in, on-demand). |

### Workflow authoring skills

| Skill | Purpose |
|---|---|
| `grm-workflow-scaffold` | Add a new `.claude/workflows/` script to the project, encoding token-efficiency lessons (model tiering, batched reads, structured output, read-only contract). |
| `grm-workflow-snapshot` | Snapshot the current state of a running Workflow for review or resume. |

### Integration and safety skills

| Skill | Purpose |
|---|---|
| `grm-worktree-preflight` | Verify a fresh or spawned worktree is rooted on its staging ref before any commit or merge. Run this before `git switch -c` or any branch operation. |
| `grm-release-agent-tracker` | Track and report on active subagent sessions during a multi-phase release. |
| `grm-ledger-tick` | Tick a row in the §5 status ledger in `docs/release-planning-vX.Y.md`. |

---

## 2. Branch Model

Grimoire enforces a three-tier linear flow:

```
feature/work-item branches
        │
        ▼
  version/X.Y  ← staging branch, owned by the integration master
        │
        ▼
       dev      ← integration branch, receives all releases
        │
        ▼
      main      ← stable, tagged, release-only
```

### Tier responsibilities

| Branch | Who writes to it | When |
|---|---|---|
| `version/X.Y` | Integration master only (via `grm-release-phase-merge`) | Merges completed work-item branches during a release |
| `dev` | Integration master only (via `grm-release-phase-merge` final step) | When all phases of a release are complete |
| `main` | Integration master only (via `grm-project-release`) | When the release is approved and `dev` is green |

### Rules for task agents

- **Branch in place** from the staging ref: `git switch -c <branch> version/X.Y`.
- **Never merge your own work** into `version/X.Y`, `dev`, or `main` — the
  integration master merges (`grm-release-phase-merge`).
- Never `cd` to a sibling worktree or edit outside your own worktree.
- Run `grm-worktree-preflight` before any `git switch -c` or `git branch` operation.

### Hotfixes

A hotfix on `main` (PATCH version) follows the same flow, branched from `main`
rather than `version/X.Y`, and cherry-picked onto `dev` immediately after.

---

## 3. `.claude/workflows/` — Multi-agent Workflows

Workflow scripts live in `.claude/workflows/<name>.js` and provide
**deterministic multi-agent fan-out** for read-heavy analysis tasks. They are a
complement to `spawn_task`, not a replacement.

### What Workflows are

A `Workflow` is a Claude Code primitive that runs a structured sequence of
agent steps — typically parallel reads followed by a synthesiser — in a
single billed operation. The result is richer analysis than any single agent
can produce in one context window.

### When to use a Workflow vs. `spawn_task`

| Use a Workflow when… | Use `spawn_task` when… |
|---|---|
| The task is read-heavy analysis (no writes needed) | The task makes file changes or git commits |
| You want deterministic, parallel fan-out | You want an autonomous agent that decides its own steps |
| You need structured output from multiple sources | You need the agent to branch, commit, and report back |

### Read-only contract

Workflows are **read-only by convention** in the Supervised and Weiss
(Collaborative) work paradigms. A mutating agent inside a Workflow must use
`isolation: 'worktree'` (its own checkout) and the integration master merges
the resulting branch — exactly as with `spawn_task`-spawned agents. Never
let a Workflow agent write directly to a shared branch.

### The `grm-release-planning` Workflow

The first shipped Workflow. Given a target release version, it:

1. Fans out parallel reader agents across roadmap, design docs, version
   history, and carryover items.
2. Sizes each candidate work item by token estimate using a dedicated sizer
   agent.
3. Synthesises a structured `work-items-report.md` ready for review and
   `grm-release-agreement`.

Use the `grm-workflow-scaffold` skill to add new Workflows following the same
pattern.

---

## 4. Guard Hooks

Guard hooks are git hook scripts installed by `grm-repo-init` into `.git/hooks/`
(and mirrored to `.claude/hooks/` for the golden restore baseline). They run
automatically on every relevant git operation and fail-closed: if a guard
cannot verify something is safe, the operation is blocked.

### `protected-branch-guard.sh`

Blocks direct commits to `main`, `dev`, and `version/X.Y` from any worktree
that does not hold the integration-master marker
(`.claude/integration-allow.local`). This prevents task agents from
accidentally writing to a branch they are not supposed to touch.

**What it checks:** the target branch of a `git commit` or `git merge`.  
**Blocked operations:** commits and merges to `main`, `dev`, or
`version/*` without the integration-master marker.  
**Bypass:** only the marker-holding worktree (the integration master) can
commit to protected branches. Task agents must branch in place.

### `push-guard.sh`

Blocks `git push` to origin from any worktree except the integration master,
and limits pushable refs to an explicit allowlist (`main`, `dev`, version
tags). Destructive push flags (`--force`, `--delete`) are always denied.

**What it checks:** the remote ref(s) in a `git push` invocation.  
**Blocked operations:** pushes to any ref not on the allowlist; any push with
destructive flags; any push from a non-master worktree.  
**Allowed refs:** `main`, `dev`, and `refs/tags/vX.Y`.

### `worktree-guard.sh`

Validates that a worktree's branch is correctly rooted before branch
operations. Prevents a task agent from accidentally operating on a sibling
worktree's checkout.

### `release-plan-guard.sh`

Blocks edits to `docs/release-planning-vX.Y.md` from task-agent worktrees.
Only the integration master may update the release plan document (specifically
the §5 status ledger). Task agents should never need to edit this file.

---

## 5. Project Configuration — `.claude/grimoire-config.json`

After onboarding, your project preferences live in `.claude/grimoire-config.json`:

```json
{
  "schema-version": 2,
  "name": "Your Project Name",
  "work-paradigm": {
    "value": "Supervised"
  },
  "workflow-variant": {
    "value": "Efficient",
    "in-development": true
  }
}
```

### Fields

| Field | Type | Description |
|---|---|---|
| `schema-version` | integer | Config schema version. `2` once the Work Paradigm is active (migrated from `1` by `grm-work-paradigm-switch`). Used for migration. |
| `name` | string | Your project's product name (set during onboarding). |
| `work-paradigm.value` | enum | `Supervised` / `Weiss` / `Noir` — how much autonomy the integration master is granted. (Input aliases: `Collaborative`→Weiss, `Autonomous`→Noir.) |
| `workflow-variant.value` | enum | `Efficient` / `Fast` / `Careful-Serial` — the cost/latency/collision-risk trade-off for write-capable Workflow runs. |
| `workflow-variant.in-development` | boolean | `true` while the variant feature is not yet active. |

### Active vs. preview preferences

The **Work Paradigm is active**: it is installed during onboarding (the
`grm-work-paradigm-switch` skill swaps the selected paradigm's content into the
active files) and the config is migrated to `schema-version: 2`, dropping
`work-paradigm.in-development`. Switch paradigms later via the
`grm-work-paradigm-switch` skill.

The `workflow-variant` field is still **captured but not yet behaviorally
active** (`in-development: true`). Grimoire stores it so that when the variant
selector lands it can read your preference without re-asking. Until
`in-development` is removed from that field, changing its value has no effect
on framework behavior.

---

## 6. What's Not in Grimoire (Yet)

These features are designed or planned but not yet active:

- **Workflow variants** — the `workflow-variant` setting is captured but the
  three variants (Efficient / Fast / Careful-Serial) are not yet implemented.
- **GitHub Releases** — `grm-project-release` tags `main` but does not yet publish
  a GitHub Release. Planned for a future release.
- **Auto-maintained `ux-demo/`** — `grm-ux-demo-build` is on-demand only; no
  continuous integration of the demo page yet.

See `docs/roadmap.md` for the full backlog and planned release themes.

---

## Further Reading

| Document | What it covers |
|---|---|
| [docs/quickstart.md](quickstart.md) | Zero-to-working-project getting-started guide |
| [docs/roadmap.md](roadmap.md) | Past, in-flight, and planned releases; feature backlog |
| [docs/architecture-guidelines.md](architecture-guidelines.md) | Cross-cutting architectural principles |
| [docs/coding-standards.md](coding-standards.md) | Language-specific coding standards |
| [docs/design/README.md](design/README.md) | Design doc house layout and index |
