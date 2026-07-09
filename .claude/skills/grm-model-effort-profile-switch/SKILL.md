---
name: grm-model-effort-profile-switch
description: Switch the active model/effort distribution profile by validating model-effort-profile.value against the registry .claude/model-effort-profiles.json and writing it to .claude/grimoire-config.json. Idempotent — exits early if the requested profile is already active. The resolver reads the field live, so no file-swap is performed. Use when onboarding selects a cost posture or the user wants a different model/effort distribution.
---

# Model/effort profile switch

Selects the active **model/effort distribution profile** — the named cost
posture every spawned work-item session resolves its `{model, effort}` tier
through. Validates the requested profile against the registry, writes it to
config, and confirms. Idempotent and safe to re-run.

Design authority: `docs/grimoire/design/model-effort-profiles-design.md`.

> Unlike `grm-work-paradigm-switch`, this skill performs **no file-swap**. The
> profile is pure data: the resolver (documented in `grm-repo-reference`
> §Subagent model & effort) reads `model-effort-profile.value` from config
> live at dispatch time. Writing the config field IS the activation.

---

## §1 — Input + value resolution

**Accepted profile values** (case-insensitive on input; stored canonical):

| Input (accepted) | Canonical stored value |
|------------------|------------------------|
| `Medium`, `medium` | `Medium` |
| `High Effort`, `high effort`, `high-effort` | `High Effort` |
| `Low Effort`, `low effort`, `low-effort` | `Low Effort` |
| `Efficient`, `efficient` | `Efficient` |
| `Autonomous`, `autonomous`, `noir` | `Autonomous` |
| `Eco/Budget`, `eco/budget`, `eco`, `budget` | `Eco/Budget` |

Input comes from one of two sources:

1. **Called with an explicit argument** — use the supplied value (the caller
   passes the desired profile name as the skill argument).
2. **Called with no argument** — read `model-effort-profile.value` from
   `.claude/grimoire-config.json` and re-validate it (a re-validation / repair
   pass).

The canonical set of valid profiles is the **keys of `profiles` in
`.claude/model-effort-profiles.json`** — that registry is the single source of
truth, not this table. The table above maps user-facing aliases onto those
keys.

---

## §2 — Validation

1. Load `.claude/model-effort-profiles.json`. If the file is missing → abort:
   > "Error: profile registry not found at
   > `.claude/model-effort-profiles.json`. Run `workflow-bootstrap --restore`
   > to restore framework files."
2. Resolve the requested input to a canonical name (§1) and confirm that name
   is a key in the registry's `profiles`. If it is **not** a registry key →
   abort without writing config:
   > "Error: '<input>' is not a known profile. Valid profiles:
   > <registry profile keys>."

Validation is fail-closed (do not write an unknown value). This is distinct
from the *resolver's* runtime behaviour, which is fail-safe (an unknown value
already in config falls back to `default-profile` with a warning rather than
breaking dispatch).

---

## §3 — Idempotency check

1. Read `model-effort-profile.value` from `.claude/grimoire-config.json`.
2. If it already equals the requested canonical value **and** carries no
   `in-development` flag → print
   "Model/effort profile <Profile> is already active. No changes made." and
   exit.

If the value differs, or an `in-development` flag is present (a legacy preview
config), proceed.

---

## §4 — Apply

Read the current config. Apply the minimal change:

- Set `model-effort-profile.value` to the canonical form.
- Remove `model-effort-profile.in-development` if present (the field is active;
  this flag does not exist in graduated configs — see
  `model-effort-profiles-design.md` §5.5 / §5.6).
- Leave `schema-version` and every other field unchanged. (The field was
  introduced at schema-version 3 in v1.9; graduation drops only the preview
  flag — it does not bump the version.)

Write the updated config back.

Example result:

```json
{
  "schema-version": 3,
  "name": "<project name>",
  "work-paradigm": { "value": "Supervised" },
  "workflow-variant": { "value": "Efficient", "in-development": true },
  "model-effort-profile": { "value": "Efficient" }
}
```

---

## §5 — Confirm

Print:

> "Model/effort profile switched to <Profile>. New work-item dispatches will
> resolve their model/effort tier through it (the resolver reads config live —
> no restart or re-install needed)."

If a profile name was changed, optionally note the headline posture shift
(e.g. "Eco/Budget — no Opus; Sonnet ceiling for medium–large").

---

## Error conditions summary

| Condition | Behaviour |
|-----------|-----------|
| Registry file missing | Abort; print restore instruction |
| Unknown / invalid profile value | Abort; do not write config; list valid keys |
| Config file missing | Abort; print restore instruction |
| Requested profile already active | Early exit; "already active" |

---

## Anti-patterns

- Writing a profile value that is not a key in the registry (validation is
  fail-closed — never persist an unknown profile).
- Bumping `schema-version` on switch — the field already lives at version 3;
  switching only changes the value (and drops a legacy preview flag).
- Attempting a file-swap — there is none; the profile is pure data the resolver
  reads live (contrast `grm-work-paradigm-switch`, which installs content sets).
- Re-transcribing the band × profile matrix here — it lives only in
  `.claude/model-effort-profiles.json`.
