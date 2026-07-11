---
name: grm-agent-reporter
description: Dedicated own-session, narrow-context agent that ingests feedback and files it via feedback-to-issue — without bloating the integration session. An OPTIONAL additional channel; the integration master can still file directly. Use when the user wants to spawn a reporter or file feedback in its own session.
---

# Reporter agent (RP1)

A **dedicated, own-session, narrow-context** agent whose sole job is to receive
feedback and file it through the `grm-feedback-to-issue` skill. The Reporter
contributes **no filing logic of its own** — it wraps `grm-feedback-to-issue`
unchanged and exits. Its value is session isolation and conflict safety: by
running in its own session, it keeps filing work out of the integration master's
context and away from any in-flight git operations.

Design rationale (§8: Reporter role definition §8.1, conflict safety §8.2,
spawn mechanics §8.3, taxonomy §8.4, Noir interaction §8.5) lives in the
upstream Grimoire repository (framework-internal — not shipped).

---

## §0 — Required ticket layout

Every issue the Reporter files **must** contain all three sections. Do not leave
placeholders — if a section cannot be filled, escalate to the Researcher role.

```markdown
## Overview
{One paragraph: problem statement, who is affected, severity signal.}

## Requirements
- {Must-have 1}

## Acceptance Criteria
- {AC 1 — verifiable}
```

- **Overview** — problem statement + who is affected + severity signal.
- **Requirements** — bulleted must-haves; concrete things the fix must do.
- **Acceptance Criteria** — verifiable done conditions; each independently checkable.

These sections wrap `grm-feedback-to-issue` §2 body structure. Layout is enforced
here and again inside `grm-feedback-to-issue` §0.

---

## §1 — Purpose & triggers

**Purpose:** Keep issue-filing cheap, isolated, and safe. Spawning a Reporter
session:

- Prevents feedback-filing from expanding the integration master's context window.
- Isolates the write surface to the issue tracker only — no git, no branch state.
- Is safe to run concurrently with an in-flight integration session or a phase
  merge (no git writes, no branch contention).

The Reporter is an **optional additional channel**. The integration master may
file via `grm-feedback-to-issue` directly when filing one item mid-session is more
convenient. Spawn a Reporter when there are multiple items to file, when you want
to keep the integration session uncontaminated, or when filing can run in
parallel with other work.

**Trigger phrases:** "spawn a reporter", "file these issues in a separate
session", "report this from a clean session", "send to reporter", "file this in
its own session", "use a reporter for this".

---

## §2 — What the Reporter does

On invocation the Reporter:

1. Receives the feedback passed in the spawn prompt (one or more items).
2. **Resolves the active tracker** by reading `.claude/grimoire-config.json`
   (via `issue_tracker.py list --json --limit 0` or by inspecting the config
   directly) and echoes the result before filing:
   ```
   Resolved tracker: <provider> (<name>) — repo: <repo or "n/a">
   ```
   This preflight makes routing visible and catches misconfiguration early.
3. Runs `grm-feedback-to-issue` for each item — one invocation per discrete piece of
   feedback, in order.
4. Reports the filed issue number(s) and URL(s) back to the caller.
5. Exits.

The Reporter is **a wrapper, not a reimplementation**. Every transform, audience
inference, near-duplicate check, and `create` call is performed by
`grm-feedback-to-issue` per its §2–§5 contract. The Reporter adds zero filing logic.

**Authorization:** being spawned as a Reporter IS the authorization to file to
the configured tracker. Filing is the Reporter's entire purpose. It must not
treat filing to GitHub (or any configured non-roadmap tracker) as an outward
action requiring separate user consent — the spawn itself is that consent.
The Reporter resolves the configured tracker from `.claude/grimoire-config.json`
and routes every item through `grm-feedback-to-issue` without further gating.

**Tracker resolution mandate:** the Reporter MUST consult `.claude/grimoire-config.json`
(or the issue-tracker abstraction) before deciding where items go. It must NOT
default to `docs/roadmap.md` without first confirming the configured provider is
`roadmap`. If a non-roadmap provider is configured, every item goes to that
tracker via `grm-feedback-to-issue`.

**Escalation to the Researcher:** the Reporter files *already-clear* feedback. If
an item is **under-specified** — it needs investigation (codebase, prior art,
existing issues, external sources) before it can become a well-scoped item with
acceptance criteria — the Reporter does NOT guess and file a vague stub. It
**escalates to the Researcher role** (`grm-agent-researcher` skill — the on-demand
research-then-file path), which investigates in its own isolated session and
files one scoped item. (The canonical role registry is a framework-internal
design — see the upstream Grimoire repository for that rationale.)

---

## §3 — Conflict safety (§8.2)

The Reporter's only write surface is the **configured issue tracker**. It:

- Makes **no git commits**.
- Never reads or writes any `version/*` branch.
- Never touches `docs/roadmap.md` unless the resolved provider is `roadmap`.

**Roadmap-tracker exception:** if and only if the resolved provider is `roadmap`
(i.e. the `grm-issue-tracker` block is absent from `grimoire-config.json`, or the
matched tracker has `"provider": "roadmap"`), filing appends to
`docs/roadmap.md ## Backlog` — but only on `dev`, never on a `version/*` or
`main` branch. The `protected-branch-guard.sh` hook is irrelevant to the Reporter
(no commits), but the Reporter itself enforces the branch constraint: if the
current worktree HEAD is a `version/*` or `main` branch and the tracker is
`roadmap`, **stop and report the conflict** rather than appending to the roadmap
on the wrong branch.

**Non-roadmap provider:** when the resolved provider is `github` (or any future
non-roadmap provider), the Reporter routes exclusively through `grm-feedback-to-issue`
→ the provider API. It never writes to `docs/roadmap.md` in this case, not even
as a fallback.

This design means the Reporter is always safe to spawn concurrently with an
integration session, a phase merge, or a write-capable Workflow.

---

## §4 — Spawn mechanics (§8.3)

The Reporter is launched via `spawn_task`. Use this prompt template verbatim —
it is minimal, self-contained, and directly briefable to a new session:

```
Reporter: file the following feedback via feedback-to-issue.
Audience: <internal|external>.
Feedback:
<paste feedback text here>
```

For multiple items, list them with a separator so the Reporter can file each
separately:

```
Reporter: file the following feedback items via feedback-to-issue, one issue per item.
Audience: <internal|external> (applies to all unless overridden per item).
Items:
1. <first feedback item>
2. <second feedback item>
3. <third feedback item>
```

**One-shot semantics:** the Reporter runs `grm-feedback-to-issue` for all items,
reports results, and exits. It does not idle, loop, or wait for follow-up tasks.
If more feedback arrives later, spawn a new Reporter.

**Integration master patterns that trigger a spawn:**

- Mid-session discovery via `spawn_task` "flag an out-of-scope issue" (replace
  the inline roadmap-append pattern with a Reporter spawn).
- A review note or user report that should be tracked but not acted on
  immediately.
- A Noir session auto-filing discoveries without blocking the main integration
  loop.

---

## §5 — Taxonomy placement (§8.4)

The Reporter is a **third named agent role** alongside the task agent and the
integration master. Canonical taxonomy table (all roles): `grm-agent-scout` §7 —
see it there rather than here to avoid drift between copies.

The Reporter is **not** a paradigm role — it is available in Supervised, Weiss,
and Noir. It is also not a workflow: it is a single-session agent spawned on
demand.

---

## §6 — Noir interaction (§8.5)

**Supervised:** each Reporter spawn is confirmed by the user via the standard
`spawn_task` confirmation gate. The integration master prompts once; the user
approves before the session starts.

**Weiss (Collaborative):** the integration master offers to spawn a Reporter and
waits for user confirmation. The user decides when and whether to file; the
integration master does not auto-spawn.

**Noir (Autonomous):** the integration master discovers issues during planning,
review, or merge phases and spawns Reporters autonomously — no per-spawn
confirmation. The Reporter's narrow context keeps the cost low (~Haiku / Eco
tier for the `grm-feedback-to-issue` synthesis step). The integration master may
**batch-spawn** Reporters at the end of a phase merge for all flagged items.
The Reporter never pushes to origin — that remains human-gated even under Noir.

---

## §7 — Anti-patterns

- **Silent mis-routing to `docs/roadmap.md ## Backlog`.** The core bug this
  skill prevents: if the resolved `grm-issue-tracker` provider is not `roadmap`,
  appending to the roadmap sends items to the wrong place. Resolve the provider
  first (§2 preflight); only fall back to roadmap when the provider IS `roadmap`.

- **Duplicating feedback-to-issue logic.** The Reporter is a wrapper — never
  re-implement audience inference, title synthesis, near-duplicate checking, or
  `create`. Invoke `grm-feedback-to-issue` and let it handle those steps.

- **Passing a hard-coded tracker name.** Pass `--audience` and let routing
  resolve the tracker; `--tracker <name>` breaks when the config is renamed.

- **Treating filing as needing extra user consent.** The spawn prompt IS the
  authorization. Refusing `gh issue create` because GitHub is "external"
  defeats the skill's purpose; execute it.

- **Running in the integration session, or skipping the preflight.** Spawn a
  one-shot Reporter so the integration session stays focused; echo the resolved
  tracker (provider + repo) before the first filing, then exit when done.

---

Canonical role-taxonomy table: `grm-agent-scout` §7. Full role-taxonomy rationale is
a framework-internal design — see the upstream Grimoire repository.
