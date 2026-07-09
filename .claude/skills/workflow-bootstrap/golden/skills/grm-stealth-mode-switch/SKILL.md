---
name: stealth-mode-switch
description: >-
  Turn Grimoire Stealth Mode on or off. Stealth Mode is an orthogonal operating
  mode (independent of work paradigm) that leaves zero AI/agent fingerprints in
  anything reaching source control: Grimoire's own files are excluded locally
  (never committed), comments and commit messages carry no AI/Claude tells, code
  mirrors the surrounding style over Grimoire's standards, the agent never pushes,
  and it leaves no dangling branch it created. Activation discloses + records the
  ephemeral-context risk (deleting the local clone loses the Grimoire context).
  Idempotent and reversible; toggling never edits a committed file. Triggers on
  "enable stealth mode", "turn on stealth", "stealth mode", "hide that I'm using
  Claude/AI", "leave no AI traces", "disable stealth mode", "turn off stealth".
---

# stealth-mode-switch

Activate or deactivate **Stealth Mode** by reading/writing
`stealth-mode` in `.claude/grimoire-config.json` and updating the active
surfaces. Full design + rationale: `docs/design/stealth-mode-design.md`.

Stealth Mode composes with **any** work paradigm — it does not change the
paradigm. It changes *what artifacts the work may leave behind*.

## The five pillars (what "on" enforces)

1. **Zero committed Grimoire artifacts.** `.claude/`, `CLAUDE.md`, design docs,
   roadmap, release plans, config — excluded locally via `.git/info/exclude`
   (never `.gitignore`), and the `stealth-guard.sh` hook refuses to stage/commit
   them. → ephemeral, local-only context.
2. **Comment + commit hygiene.** No `claude`/`anthropic`/`AI`/`LLM`/agent tells,
   no `Co-Authored-By: Claude` trailer, in any committed deliverable file or
   commit message. (`stealth_scrub.py scan` + the hook enforce it.)
3. **Style mirroring over standards.** Match the surrounding code/comment style —
   even if subpar — ahead of Grimoire's own coding standards; the v1.26
   merge-quality gates relax to advisory so the diff blends in.
4. **No push, ever.** The agent never pushes in stealth (categorical, not
   human-gated). The human pushes manually outside Grimoire if they choose.
5. **No dangling branches.** Snapshot a branch baseline at session start; suppress
   the `version/* → dev → main` model; reconcile + delete any branch Grimoire
   created before the session ends.

## Activate (`on`)

1. **Idempotency check.** Read `.claude/grimoire-config.json`. If
   `stealth-mode.value == "on"` already and `.git/info/exclude` carries the
   stealth sentinel block → report "Stealth Mode already active" and stop.
2. **Disclose + require acknowledgement.** Present this verbatim and wait for an
   explicit yes:

   > **Stealth Mode disclosure.** With Stealth Mode on, none of Grimoire's files
   > (`.claude/`, `CLAUDE.md`, design docs, roadmap, release plans, config) are
   > committed — they are excluded locally and exist only in this working copy.
   > **If you delete this local repository, your entire Grimoire context is lost
   > and you start fresh.** Grimoire will also never push, and will clean up any
   > branch it creates. Proceed?

   On decline → abort with no changes.
3. **Resolve managed paths.** Use `stealth-mode.managed-paths` if set, else the
   default set (design §2.2). Show the resolved set for confirmation.
4. **Write `.git/info/exclude`** — insert/replace the sentinel block (idempotent;
   replace its body, never append a duplicate):

   ```
   # >>> grimoire-stealth (managed) >>>
   /.claude/
   /CLAUDE.md
   /AGENTS.md
   /docs/design/
   /docs/roadmap.md
   /docs/version-history.md
   /docs/release-planning-v*.md
   /docs/integration-workflow.md
   /docs/coding-standards*
   /docs/architecture-guidelines.md
   /.github/prompts/
   /.github/copilot-instructions.md
   # <<< grimoire-stealth (managed) <<<
   ```

   (Add only globs that apply to this project's layout.)
5. **Install the behavioral overlay.** Replace the `CLAUDE.md`
   `<!-- STEALTH_SECTION:start -->…end -->` body with
   `.claude/stealth/CLAUDE-stealth-on.md`.
6. **Snapshot the branch baseline:**
   `python3 .claude/skills/stealth-mode-switch/stealth_scrub.py branches --baseline`
7. **Flip the config** atomically (temp + validate + replace): set
   `stealth-mode.value:"on"`, `acknowledged-risk:true`, `schema-version:4`. Then
   `python3 .claude/skills/config-validate/config_validate.py` must pass clean.
8. **Confirm**: print the resolved managed-path set and the no-push /
   branch-reconciliation reminders. Optionally write the local
   `.claude/cache/STEALTH-README.txt` backup hint (itself concealed).

## Deactivate (`off`)

1. **Idempotency check.** If already `off` → report and stop.
2. **Remove the sentinel block** from `.git/info/exclude` (only the
   `>>> grimoire-stealth` … `<<<` block; touch nothing else).
3. **Restore** the dormant `## Stealth Mode` pointer in `CLAUDE.md` from
   `.claude/stealth/CLAUDE-stealth-off.md`.
4. **Flip the config**: `stealth-mode.value:"off"` (leave `acknowledged-risk`
   as-is — the user already acknowledged once). Validate clean.
5. **Report** any still-outstanding net-new branches
   (`stealth_scrub.py branches`) so un-reconciled work is not lost, and note that
   previously-concealed files are now eligible for normal handling.

## Operating reminders while stealth is ON

- Run `stealth_scrub.py scan --staged --strict` before every commit of
  deliverable files; fix any tell it reports.
- Run `stealth_scrub.py branches --strict` at session closeout; reconcile
  (merge/rebase into the intended branch) and delete every net-new branch.
- Never run `git push`; never create `version/*` (the hook blocks both).
- Prefer working on the current branch or one short-lived branch named to the
  project's own convention.

## Safety / invariants

- **Never edits a committed file.** All writes go to `.git/info/exclude`, the
  concealed `CLAUDE.md`, the concealed config, and `.claude/cache/` — all
  themselves within managed (uncommitted) paths.
- **Cross-rule enforced:** `value:"on"` with `acknowledged-risk:false` is a hard
  `config-validate` error — stealth cannot be activated by hand-editing the
  config without recording consent.
- **Dogfood caution:** never activate stealth on the Grimoire framework repo
  itself (it is public + committed); validate switch logic in a throwaway sandbox.
