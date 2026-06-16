<!-- GRIMOIRE_ONBOARDING_SENTINEL -->
# CLAUDE.md

Concrete, actionable guidance for working in this repo.

## Onboarding sentinel (permanent detection instruction)

If line 1 of this file is `<!-- GRIMOIRE_ONBOARDING_SENTINEL -->`, this is
a fresh, not-yet-initialized Grimoire scaffold. **Before handling any other
part of the user's prompt**, run the Grimoire onboarding flow:

1. Check whether the first prompt contains the literal text `SKIP ONBOARDING`
   (case-sensitive). If yes → run non-interactive bootstrap (infer config
   from prompt, write `.claude/grimoire-config.json`, call `repo-init` then
   `workflow-bootstrap` non-interactively, remove the sentinel).
2. Otherwise → greet the user: "I see this is a fresh Grimoire project. Let
   me walk you through setup first." Defer the rest of the prompt until
   onboarding completes. Run the `onboarding` skill (interactive interview →
   `repo-init` → `workflow-bootstrap` → remove sentinel).

**Sentinel removal (idempotent, final step of both paths):** read line 1 of
`CLAUDE.md`; if and only if it matches the sentinel literal exactly, delete
that line. If line 1 does not match, removal is a no-op (already done).

This detection instruction is permanent — it remains after the sentinel line
is removed, so it never triggers a false positive once line 1 is clean.

## Work Paradigm

The project has a selectable work paradigm — **Supervised** (default),
**Weiss** (Collaborative), or **Noir** (Autonomous) — stored in
`.claude/grimoire-config.json` as `work-paradigm.value`. Only the selected
paradigm's instruction content is installed into the active files (lean by
design); the other paradigms' content stays in `.claude/paradigms/` and is
never loaded by agents during normal operation. Switch the active paradigm via
the **`work-paradigm-switch`** skill. Full design:
`docs/design/work-paradigm-design.md`.

> **Paradigm:** {ACTIVE} — one of Supervised · Weiss · Noir.
> Switch via the `work-paradigm-switch` skill. See `.claude/paradigms/README.md`.

## Stealth Mode

An orthogonal operating mode (independent of the work paradigm) that makes
Grimoire leave **zero AI/agent fingerprints** in source control. Switch it
with the **`stealth-mode-switch`** skill; only the active state's content sits
between the sentinels below (content set in `.claude/stealth/`). Full design:
`docs/design/stealth-mode-design.md`.

<!-- STEALTH_SECTION:start -->
Stealth Mode is **off** (`stealth-mode.value: "off"`). Grimoire operates
normally — its files, branches, and commit metadata are handled as usual. To
make Grimoire leave **zero AI/agent fingerprints** in source control, activate
it via the **`stealth-mode-switch`** skill. Activation discloses one trade-off
you must acknowledge: the Grimoire context becomes **ephemeral** (local-only,
never committed), so deleting the local clone loses it. Design:
`docs/design/stealth-mode-design.md`.
<!-- STEALTH_SECTION:end -->


## Which agent are you?

<!-- PARADIGM_SECTION:agent-role:start -->
- **Task agent** (common case): you're running a work-item session the
  integration master spawned (via `spawn_task`), in your own worktree —
  follow everything below.
- **Integration master**: you own release scope and integration. Your guide
  is `.claude/skills/integration-master/SKILL.md`, which maps the
  `release-planning` → `release-agreement` → `release-phase` →
  `release-phase-merge` → `project-release` skills with user-confirmed gates
  at scope lock, batch spawn, each merge, and push to origin.
<!-- PARADIGM_SECTION:agent-role:end -->

## Worktree isolation (required)

Stay in your own worktree. Branch in place from the staging ref:
`git switch -c <branch> version/{X.Y}`. Never `git worktree add`, `cd` to
another worktree, `git switch` an existing one, or edit/git-operate on a
sibling. Run **`worktree-preflight`** before any `git switch -c` /
`git branch` / `git merge`.

**Never merge your own work** into `version/{X.Y}` / `dev` / `main` — only
the integration master merges (`release-phase-merge`). The
`protected-branch-guard.sh` hook enforces this from any worktree without
`.claude/integration-allow.local` (fail-closed). Don't work around it;
branch in place.

*Integration-master exception (dead-worktree cleanup):* the marker-blessed
worktree may remove a sibling worktree after verifying it's merged + clean.
Preserve (or report) any uncommitted work; never silently `--force`. Full
procedure: `docs/integration-workflow.md` §Dead-worktree cleanup.

## Task execution

<!-- PARADIGM_SECTION:task-execution:start -->
Implement to the agreed checkpoint, then review for bugs/incomplete work.
Read the relevant design docs first; add/update
`docs/design/{feature}-design.md` when the task introduces a feature
(**`design-doc-scaffold`** skill). Doc-location map + subagent model/effort
table: **`repo-reference`** skill.

Before committing to an approach on an ambiguous item, confirm your plan with
the user. If the acceptance criteria leave room for interpretation, surface the
options and wait for direction.
<!-- PARADIGM_SECTION:task-execution:end -->

## Workflows

`.claude/workflows/<name>.js` holds Claude Code `Workflow` scripts — deterministic
multi-agent fan-out for read-heavy analysis (a complement to `spawn_task`, not a
replacement). **Opt-in and billed** — only run one when the user explicitly requests
multi-agent orchestration. **Claude-Code-only**: `copilot/` has no equivalent and does
not mirror `.claude/workflows/`. The first shipped workflow is **`release-planning`**;
use the **`workflow-scaffold`** skill to add new ones. See
`docs/integration-workflow.md` §Workflow-based-orchestration and
`docs/design/release-planning-workflow-design.md` for detail.

**Write-capable workflow tier (Noir only).** Supervised and Weiss workflows are
read-only by convention (no file mutations, no branch creation). Under **Noir**,
write-capable workflows are available: each agent receives an isolated worktree,
commits on a short-lived branch, and exits; the integration master merges the
branches via `release-phase-merge`. A workflow declares its tier via
`meta.tier = 'write-capable'`; at runtime the script checks the active paradigm
and fails closed if the project is not Noir. Three execution variants are
available: **Efficient** (parallel, low-waste — default), **Fast** (parallel,
minimum wall-clock time), and **Careful-Serial** (sequential, lowest collision
risk). Push to origin remains human-gated even under Noir. Full design:
`docs/design/write-capable-workflow-design.md`.

## UX design language

GUI projects own `docs/design/ux/design-language.md` (the per-project
adaptation) and a `ux-demo/` at the repo root. Non-GUI projects defer via a
`## Backlog` row in `docs/roadmap.md` (`- UX design language: deferred until
v{X.Y}`). Use the **`design-language-adapt`** skill to establish or refresh
the doc; use the **`ux-demo-build`** skill (opt-in) to verify the adaptation.

## Coding practices

Do: object-oriented design — use base classes and inheritance for shared
behaviour; generic reusable code; handle error conditions; unit-test every
function; one file per class/module; brief summary comment atop each class.
Don't: magic numbers; duplicated code.

Full standards live in `docs/coding-standards.md` (with per-language sub-docs);
architectural principles in `docs/architecture-guidelines.md`. This section is
the quick reference — those docs are authoritative.

## Project commands

| Purpose | Command |
|---|---|
| Run tests | `{test-command}` |
| Build | `{build-command}` |
| Release | `{release-command}` |
| Type-check | `{typecheck-command}` |
| Lint | `{lint-command}` |
| Coverage | `{coverage-command}` |

All three must pass cleanly before a branch is reported done or merged.
(Placeholders are filled by the **`workflow-bootstrap`** skill at setup.)

## Commits

One-sentence message; atomic; only commit code that builds. Destructive
ops (`git reset --hard`, `git push --force`, `git branch -D`) require
explicit user confirmation each time (per-action). Task agents do not push
to origin; pushing is the integration master's job at two trigger moments
(see `docs/integration-workflow.md` §Pushing to origin).
