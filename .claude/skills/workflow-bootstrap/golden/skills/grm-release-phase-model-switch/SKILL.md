---
name: release-phase-model-switch
description: Switch the active release-phase-model dial by validating release-phase-model.value against the value set {Default, Auto} and writing it to .claude/grimoire-config.json. Idempotent — exits early if the requested value is already active. The integration master reads the field live at execution time, so no file-swap is performed. The Auto value is Noir-only and fails closed — the skill refuses to set Auto unless work-paradigm.value == "Noir". Use when onboarding picks a release-execution model, when a Noir project wants fully in-session orchestration, or to revert to the Default spawn_task pipeline. Triggers on "switch release-phase model", "set release-phase-model", "use Auto orchestration", "enable Auto release", "switch to Default release model", "drive the release in-session".
---

# Release-phase-model switch

Selects the active **release-phase-model** — the dial that governs *how the
integration master executes an agreed plan*, which the master resolves at
execution time. Validates the requested value against the value set, applies the
**Noir-only guard** for `Auto`, writes it to config, and confirms. Idempotent
and safe to re-run.

Design authority: `docs/design/release-phase-model-design.md`.

> Like `workflow-variant-switch` and `model-effort-profile-switch`, this skill
> performs **no file-swap**. The release-phase model is pure data: the consumer
> (the integration master — `release-phase` / `release-phase-merge`) reads
> `release-phase-model.value` from config **live** at execution time. Writing the
> config field IS the activation. (Contrast `work-paradigm-switch`, which
> installs content sets.)

---

## §1 — Input + value resolution

**Accepted values** (case-insensitive on input; stored canonical):

| Input (accepted) | Canonical stored value |
|------------------|------------------------|
| `Default`, `default` | `Default` |
| `Auto`, `auto` | `Auto` |

The canonical value set is **`{Default, Auto}`** (design §"The dial"). There is
**no registry file** — the two values are behavioural and live in the consuming
integration-master skills; this skill validates against the fixed set above and
the config field stores only the active value.

Input comes from one of two sources:

1. **Called with an explicit argument** — use the supplied value (the caller
   passes the desired value as the skill argument).
2. **Called with no argument** — read `release-phase-model.value` from
   `.claude/grimoire-config.json` and re-validate it (a re-validation / repair
   pass — e.g. confirm a stored value is still in-set and still permitted under
   the current paradigm).

---

## §2 — Validation

1. Resolve the requested input to a canonical name (§1). Confirm the resolved
   name is one of `{Default, Auto}`. If it is **not** → abort without writing
   config:
   > "Error: '<input>' is not a known release-phase model. Valid values:
   > Default, Auto."
2. If `.claude/grimoire-config.json` is missing → abort:
   > "Error: config not found at `.claude/grimoire-config.json`. Run
   > `workflow-bootstrap --restore` to restore framework files."

Validation is fail-closed (do not write an unknown value). This is distinct
from the *consumer's* runtime behaviour, which is fail-safe (an unknown or
absent value falls back to the `Default` model rather than breaking execution).

### §2.1 — Noir-only guard for `Auto` (fail closed)

`Auto` is **Noir-only**. After §2 confirms `Auto` is a valid value, read
`work-paradigm.value` from `.claude/grimoire-config.json`:

- If `work-paradigm.value == "Noir"` → permitted; continue.
- If `work-paradigm.value != "Noir"` (or the field is absent) → **abort without
  writing config**:
  > "Error: the 'Auto' release-phase model is Noir-only. The active work
  > paradigm is '<paradigm>'. Switch to Noir first (`work-paradigm-switch
  > Noir`), or set the release-phase model to 'Default'."

This guard applies **only** to `Auto`; `Default` is always permitted under every
paradigm. The guard mirrors the design's fail-closed contract (design §"Noir-only
guard") — the switch skill never persists `Auto` outside Noir, and the master's
own execution-time check provides defence-in-depth (falling back to `Default`
and logging if the dial reads `Auto` under a non-Noir paradigm after a later
switch).

---

## §3 — Idempotency check

1. Read `release-phase-model.value` from `.claude/grimoire-config.json`.
2. If it already equals the requested canonical value → print
   "Release-phase model <Value> is already active. No changes made." and exit.

If the value differs (or the field is absent), proceed.

> The idempotency check runs **after** the §2.1 Noir-only guard: a request to
> re-set `Auto` under a non-Noir paradigm is rejected by §2.1 even if the stored
> value already reads `Auto` (a stale value left by a later paradigm switch).

---

## §4 — Apply

Read the current config. Apply the minimal change:

- Set `release-phase-model.value` to the canonical form. If the
  `release-phase-model` block is absent, add it (additive — absent ⇒ `Default`).
- Leave `schema-version` and every other field unchanged. The block is
  **additive and optional** — adding or switching it does **not** bump
  `schema-version` (mirrors the `model-effort-profile` / `workflow-variant` /
  `issue-tracker` additive-field precedent; see design §Idempotency).

Write the updated config back.

Example result:

```json
{
  "schema-version": 3,
  "name": "<project name>",
  "work-paradigm": { "value": "Noir" },
  "workflow-variant": { "value": "Efficient" },
  "model-effort-profile": { "value": "Medium" },
  "release-phase-model": { "value": "Auto" }
}
```

---

## §5 — Confirm

Print:

> "Release-phase model switched to <Value>. The integration master will resolve
> how it executes the next release through it (it reads config live — no restart
> or re-install needed)."

Optionally note the headline execution shift:

- **Default** — the master decomposes the plan into phases and dispatches each
  work item as a separate session (`release-phase` spawn_task chips), merging
  each branch via `release-phase-merge`. Exactly today's pipeline; all paradigms.
- **Auto** (Noir only) — the master drives the open phase inside its own session
  via a write-capable Workflow (isolated-worktree agents commit short-lived
  branches the master merges in `mergeAfter` order, testing continuously),
  prompting only for the final review. The `workflow-variant` dial still governs
  the execution variant (Efficient / Fast / Cheap-Slow) within that tier. Push
  stays human-gated.

---

## Error conditions summary

| Condition | Behaviour |
|-----------|-----------|
| Config file missing | Abort; print restore instruction |
| Unknown / invalid value | Abort; do not write config; list valid values |
| `Auto` requested under non-Noir paradigm | Abort; do not write config; explain Noir-only guard |
| Requested value already active | Early exit; "already active" |

---

## Seams (this skill is pure-data; it does NOT implement execution)

- The integration master (`integration-master` / `release-phase` /
  `release-phase-merge`) reads `release-phase-model.value` live at execution time
  to choose between the spawn_task-per-item pipeline (`Default`) and the
  write-capable Workflow path (`Auto`). This skill only *writes* the value.
- The dials are **independent** (no dial-derives-from-dial): this skill
  reads/writes only `release-phase-model` and reads `work-paradigm` solely for
  the §2.1 Noir-only guard — it never sets another dial's field.
- Onboarding offers the dial as its own step and calls this skill to persist the
  choice (presenting `Auto` only when the selected paradigm is Noir).

---

## Anti-patterns

- Writing a value outside `{Default, Auto}` (validation is fail-closed — never
  persist an unknown value).
- Persisting `Auto` under a non-Noir paradigm — §2.1 refuses it; `Auto` is
  Noir-only and fails closed.
- Bumping `schema-version` on switch — the field is additive at the current
  schema-version; switching only changes (or adds) the value.
- Implementing execution logic here — choosing the spawn_task pipeline vs the
  write-capable Workflow is the integration master's job; this skill is a
  pure-data write the consumer reads live.
- Attempting a file-swap — there is none (contrast `work-paradigm-switch`).
- Re-implementing or extending the write-capable Workflow tier — `Auto` *uses*
  the existing tier; it adds no machinery (design §Non-goals).
