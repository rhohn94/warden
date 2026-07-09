---
name: grm-workflow-variant-switch
description: Switch the active execution-strategy (the workflow-variant dial: {Fast, Efficient, Cheap-Slow}) and write it to .claude/grimoire-config.json. Idempotent — exits early if the requested strategy is already active. Migrates the legacy Careful-Serial preview value to Cheap-Slow. Use when onboarding picks a dispatch posture, or when the user wants a different execution strategy.
---

# Execution-strategy switch (workflow-variant)

Selects the active **execution-strategy** — the dial that governs *how work is
dispatched* (fan-out width and isolation mode), which the integration master
(`grm-release-phase` / the Noir default-dispatch path) resolves at dispatch time.
Validates the requested strategy against the preset set, writes it to config,
and confirms. Idempotent and safe to re-run.

Design authority: `docs/grimoire/design/execution-profiles-design.md` (§A, §D, §E, §F).

> The dial is *conceptually* "execution-strategy"; its **config field stays
> `workflow-variant`** (no rename — avoids a schema churn and keeps the field
> write-capable Workflows already read; see design §A/§F.1).

> Like `grm-model-effort-profile-switch`, this skill performs **no file-swap**. The
> strategy is pure data: the consumer (`grm-release-phase` / the integration master,
> per design §E — E2's dispatch logic) reads `workflow-variant.value` from config
> live at dispatch time. Writing the config field IS the activation. (Contrast
> `grm-work-paradigm-switch`, which installs content sets.)

---

## §1 — Input + value resolution

**Accepted strategy values** (case-insensitive on input; stored canonical):

| Input (accepted) | Canonical stored value |
|------------------|------------------------|
| `Fast`, `fast` | `Fast` |
| `Efficient`, `efficient` | `Efficient` |
| `Cheap-Slow`, `cheap-slow`, `cheap slow`, `cheapslow`, `cheap` | `Cheap-Slow` |
| `Careful-Serial`, `careful-serial`, `careful serial` (legacy) | `Cheap-Slow` *(migrated — see §1.1)* |

The canonical preset set is **`{Fast, Efficient, Cheap-Slow}`** (design §A/§D).
There is **no registry file** — unlike `model-effort-profile`, the three presets
are behavioural and live in the consuming skills; this skill validates against
the fixed set above and the config field stores only the active value.

Input comes from one of two sources:

1. **Called with an explicit argument** — use the supplied value (the caller
   passes the desired strategy name as the skill argument).
2. **Called with no argument** — read `workflow-variant.value` from
   `.claude/grimoire-config.json` and re-validate it (a re-validation / repair /
   graduation pass — e.g. drop a legacy `in-development` flag, migrate a legacy
   `Careful-Serial` value).

### §1.1 — Legacy `Careful-Serial` migration

`Careful-Serial` was the v1.4 preview third preset. It is **removed from the
project-level execution-strategy set** (design §D): it remains an orthogonal
ordering concern *internal to write-capable Workflows* (the Workflow's own
`args.variant`, a different namespace this skill never touches). A project
config carrying `workflow-variant.value: "Careful-Serial"` is **migrated to the
nearest cost posture, `Cheap-Slow`** (a serial-leaning project wanted the
low-fan-out cost posture). This migration is silent except that the §5
confirmation names the change.

> Do **not** treat the Workflow `args.variant: 'Careful-Serial'` as in scope —
> that identifier is distinct and lives in the Workflow scripts (see
> `docs/grimoire/design/write-capable-workflow-design.md` §4). They never collide.

---

## §2 — Validation

1. Resolve the requested input to a canonical name (§1, applying the §1.1 legacy
   migration). Confirm the resolved name is one of `{Fast, Efficient,
   Cheap-Slow}`. If it is **not** → abort without writing config:
   > "Error: '<input>' is not a known execution strategy. Valid strategies:
   > Fast, Efficient, Cheap-Slow."
2. If `.claude/grimoire-config.json` is missing → abort:
   > "Error: config not found at `.claude/grimoire-config.json`. Run
   > `workflow-bootstrap --restore` to restore framework files."

Validation is fail-closed (do not write an unknown value). This is distinct
from the *consumer's* runtime behaviour, which should be fail-safe (an unknown
value already in config falls back to the `Efficient` default rather than
breaking dispatch).

---

## §3 — Idempotency check

1. Read `workflow-variant.value` from `.claude/grimoire-config.json`.
2. If it already equals the requested canonical value **and** carries no
   `in-development` flag **and** needs no §1.1 migration → print
   "Execution strategy <Strategy> is already active. No changes made." and exit.

If the value differs, an `in-development` flag is present (a legacy preview
config), or a legacy `Careful-Serial` value needs migrating, proceed.

---

## §4 — Apply

Read the current config. Apply the minimal change:

- Set `workflow-variant.value` to the canonical form (after §1.1 migration).
- Remove `workflow-variant.in-development` if present (the field is graduated;
  this flag does not exist in graduated configs — see
  `execution-profiles-design.md` §F.1).
- Leave `schema-version` and every other field unchanged. (The field already
  exists at the current `schema-version: 3`; graduation drops only the preview
  flag — it does **not** bump the version. This mirrors the model-effort-profile
  graduation precedent exactly — see §F.1 / `model-effort-profiles-design.md`
  §5.6.)

Write the updated config back.

Example result:

```json
{
  "schema-version": 3,
  "name": "<project name>",
  "work-paradigm": { "value": "Supervised" },
  "workflow-variant": { "value": "Efficient" },
  "model-effort-profile": { "value": "Medium" }
}
```

---

## §5 — Confirm

Print:

> "Execution strategy switched to <Strategy>. New work-item dispatches will
> resolve their fan-out width and isolation mode through it (the integration
> master reads config live — no restart or re-install needed)."

If a legacy `Careful-Serial` value was migrated, additionally note:

> "(Legacy 'Careful-Serial' migrated to 'Cheap-Slow'; Careful-Serial remains a
> write-capable-Workflow ordering variant, not a project execution strategy.)"

Optionally note the headline dispatch shift:

- **Fast** — max parallel fan-out → minimum wall-clock (you pay for duplicated reads).
- **Efficient** — balanced default (conflict-map batches, dedup'd reads).
- **Cheap-Slow** — low fan-out + small batches; pairs with the Eco-Budget tier.

---

## Error conditions summary

| Condition | Behaviour |
|-----------|-----------|
| Config file missing | Abort; print restore instruction |
| Unknown / invalid strategy value | Abort; do not write config; list valid presets |
| Requested strategy already active | Early exit; "already active" |
| Legacy `Careful-Serial` value | Migrate to `Cheap-Slow`; write; note in confirmation |

---

## Seams (this skill is pure-data; it does NOT implement dispatch)

- **E2** reads `workflow-variant.value` in `grm-release-phase` / the Noir default
  path to size fan-out and select isolation mode (design §E). This skill only
  *writes* the value.
- **E3** decouples the dials (no dial-derives-from-dial); this skill already
  reads/writes only `workflow-variant` and never another dial's field.
- **E4** onboarding offers the strategy as its own question and calls this skill
  to persist the choice.

---

## Anti-patterns

- Writing a strategy value outside `{Fast, Efficient, Cheap-Slow}` (validation
  is fail-closed — never persist an unknown strategy).
- Persisting `Careful-Serial` in the project field — it is migrated to
  `Cheap-Slow`; the only place `Careful-Serial` legitimately lives is the
  write-capable Workflow's `args.variant` (do not touch those scripts).
- Bumping `schema-version` on switch — the field already lives at version 3;
  switching only changes the value (and drops a legacy preview flag).
- Implementing dispatch logic here — fan-out sizing / isolation mode is E2's job
  in `grm-release-phase`; this skill is a pure-data write the consumer reads live.
- Attempting a file-swap — there is none (contrast `grm-work-paradigm-switch`).
