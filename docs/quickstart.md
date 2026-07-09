# Grimoire Quickstart

> **Up:** [↑ Docs](README.md)


> **New to Grimoire?** This guide takes you from a freshly copied scaffold to
> a fully initialized project in minutes. For a complete reference of what
> Grimoire provides — skills, branch model, workflows, and guards — see
> [docs/features.md](features.md).

---

## What is Grimoire?

Grimoire is a Claude Code scaffolding framework. Copy it into a new project
and it provides:

- A structured branch model (`version/* → dev → main`) with protected-branch
  guards.
- A skill set for release planning, onboarding, and day-to-day development.
- Multi-agent `Workflow` scripts for read-heavy analysis tasks.
- An onboarding interview that captures durable project preferences on the
  first run.

---

## Prerequisites

- Claude Code installed and configured.
- A git repository (fresh or existing) where you want to use Grimoire.
- The Grimoire scaffold files copied into your project root (or you are working
  in a freshly generated scaffold).

---

## Step 1 — Open Claude Code in your project

Open Claude Code with your project directory as the working directory. If your
project root already contains the Grimoire `CLAUDE.md`, Claude Code will load
it automatically.

---

## Step 2 — Onboarding fires automatically

A freshly copied Grimoire scaffold contains a sentinel on line 1 of `CLAUDE.md`:

```
<!-- GRIMOIRE_ONBOARDING_SENTINEL -->
```

When Claude Code reads `CLAUDE.md` on your **first prompt**, it detects the
sentinel and routes you into the Grimoire onboarding flow before handling
anything else. You will see:

> "I see this is a fresh Grimoire project. Let me walk you through setup first."

The onboarding interview then asks four questions in order:

| Step | Question |
|------|----------|
| 1 | **Project name** — what do you call your project? |
| 2 | **Work Paradigm** — Supervised / Weiss / Noir (activates immediately during setup) |
| 3 | **Workflow variant** *(preview — not yet active)* — Efficient / Fast / Careful-Serial |
| 4 | **GUI presence** — does your project have (or will have) a user interface? |

After you answer, onboarding:

1. Writes your preferences to **`.claude/grimoire-config.json`**.
2. Runs **`grm-repo-init`** to set up the branch model and guard hooks (skipped if
   `main` + `dev` already exist).
3. Runs **`grm-workflow-bootstrap`** to fill in project-specific settings (test
   command, build command, release command, doc-location map, and more).
4. Removes the sentinel from `CLAUDE.md` so onboarding never re-triggers.
5. Seeds your `docs/roadmap.md` with the framework-required baseline
   capabilities for your project shape.
6. Confirms: *"Onboarding complete. Your project config is at
   `.claude/grimoire-config.json`."*
7. **Bridges into first-release planning.** Under **Noir** this kicks off
   automatically (the integration master proposes an initial roadmap, locks a
   first plan, and cuts `version/{X.Y}` before any building). Under
   **Supervised** / **Weiss** onboarding instead *offers* to draft and lock a
   first release plan, and only proceeds if you say yes.

---

## SKIP ONBOARDING — fast path

If you already know your settings and want to skip the interview, include the
literal text `SKIP ONBOARDING` anywhere in your first prompt:

```
SKIP ONBOARDING — this is a headless CLI tool named "Acme"
```

Grimoire detects the phrase (case-sensitive), infers config values from the
rest of your prompt, writes `.claude/grimoire-config.json`, and runs
`grm-repo-init` + `grm-workflow-bootstrap` non-interactively — prompting only for
settings that cannot be inferred (test/build/release commands, etc.).

**Inference rules (highest-precedence first):**

| Field | How Grimoire reads the prompt |
|-------|-------------------------------|
| Project name | Quoted string after `name:` or `project:`; else the repo directory basename |
| Work Paradigm | First case-insensitive match of `Supervised`, `Weiss`, or `Noir` (aliases `Collaborative`→Weiss, `Autonomous`→Noir accepted) |
| Workflow variant | First case-insensitive match of `Efficient`, `Fast`, or `Careful-Serial` |
| GUI presence | Keywords `gui/ui/interface/web/app/frontend` → yes; `headless/cli/api` → no; else → not yet |

After SKIP ONBOARDING completes, review `.claude/grimoire-config.json` and
adjust any inferred values if needed.

---

## Step 3 — Review your config

After onboarding (either path), your project config lives at:

```
.claude/grimoire-config.json
```

A typical file looks like:

```json
{
  "schema-version": 2,
  "name": "Acme Widget",
  "work-paradigm": {
    "value": "Supervised"
  },
  "workflow-variant": {
    "value": "Efficient",
    "in-development": true
  }
}
```

The **Work Paradigm is active** — it is installed during setup (the
`grm-work-paradigm-switch` skill runs as part of onboarding) and the
`schema-version` is `2`, so `work-paradigm` no longer carries an
`in-development` flag. The `workflow-variant` field is still
`in-development: true` (**preview — not yet active**); it is stored now so a
future Grimoire release can read your preference without re-asking. See
[docs/features.md](features.md) for what each setting means and when it will
take effect.

---

## Step 4 — Verify initialization

Run your project's test and build commands (filled in by `grm-workflow-bootstrap`
and recorded in `CLAUDE.md`'s Project commands table) to confirm everything
is wired up:

```
# See the "Project commands" table in CLAUDE.md for the actual commands
```

Your branch model should now have `main` and `dev` branches, and the guard
hooks (`protected-branch-guard.sh`, `push-guard.sh`) should be active.

---

## What's next?

| Goal | Where to look |
|------|---------------|
| Understand the full feature set | [docs/features.md](features.md) |
| Plan a new release | `grm-release-planning` skill |
| Add UX design language (GUI projects) | `grm-design-language-adapt` skill |
| Add a new multi-agent Workflow | `grm-workflow-scaffold` skill |

---

## Troubleshooting

**Onboarding did not fire on the first prompt.**  
Verify that line 1 of `CLAUDE.md` is exactly `<!-- GRIMOIRE_ONBOARDING_SENTINEL -->`.
If the sentinel was accidentally removed before onboarding ran, restore line 1
and start a new Claude Code session.

**`grm-workflow-bootstrap` is asking questions I already answered in onboarding.**  
The grm-onboarding skill passes your GUI-presence answer through to
`grm-workflow-bootstrap` automatically. If you see it re-ask the GUI question,
check that you are running the `grm-onboarding` skill (not calling `grm-workflow-bootstrap`
directly).

**I want to re-run onboarding on an already-initialized project.**  
Invoke the `grm-onboarding` skill directly. It will re-run the interview and
overwrite `.claude/grimoire-config.json`. `grm-repo-init` and `grm-workflow-bootstrap`
will be called but will detect and skip steps that are already complete.
