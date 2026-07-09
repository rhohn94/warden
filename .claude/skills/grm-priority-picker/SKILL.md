---
name: grm-priority-picker
description: Interview the user to rank the speed/quality/cost trade-off triangle, map the chosen pair to concrete dial values (work-paradigm, execution-strategy, model-effort-profile, verbosity), then write the resolved dials via the existing switch skills. Also surfaces the Steady Steward as a one-pick long-horizon preset. Runs at onboarding and on demand. Use when picking priorities or balancing speed/quality/cost.
---

# Priority Picker

An advisor skill that interviews the user about the speed / quality / cost
trade-off, resolves a concrete dial configuration, and writes it through the
existing switch skills. Does **not** reimplement dispatch or tier logic — it is a
pure interviewer + writer that sits on top of the three independent dials.

Design authority: `docs/grimoire/design/cost-governance-design.md` §F (priority-picker
logic) and §G (Steady Steward preset).

---

## Entry conditions

Run this skill when:
- **Onboarding** delegates to it after the three-dials interview (Step 3/5 of
  `onboarding/SKILL.md`) and the user wants guidance rather than picking values
  directly.
- The user invokes it **on demand** ("pick my priorities", "recommend a config",
  etc.) after initial setup to re-tune the dials.

---

## §1 — Opening: the Steady Steward fast-path

Before asking the priority question, surface the **Steady Steward preset** as a
one-pick option. This avoids burying the preset at the end of an interview the
user may not need.

Present:

> "Before we work through the trade-off triangle, there is one turnkey preset
> worth knowing:
>
> **Steady Steward** — long-horizon custodian. Runs unattended at low cost,
> picking one safe, bounded item per wake:
>   - Work paradigm: **Noir** (autonomous)
>   - Execution strategy: **Cheap-Slow** (low fan-out)
>   - Model/effort: **Eco/Budget** (no Opus)
>   - Verbosity: **terse**
>   - Budget posture: low daily budget + defer non-critical on approach
>   - Schedule: off-peak only (once scheduling wiring lands in v1.16)
>
> Note: full autonomous scheduling and unattended push complete in **v1.16**.
> The preset is defined now; those building blocks wire it up in the next
> release.
>
> Would you like to apply the **Steady Steward** preset, or work through the
> priority interview to choose your own configuration?
>   1. Apply Steady Steward
>   2. Run the priority interview"

If the user picks **1 (Steady Steward)**, jump to §5. Otherwise continue to §2.

---

## §2 — Priority interview (2-of-3 triangle)

Use `AskUserQuestion`. Present the triangle framing and ask the user to pick
their two priorities:

> "The three execution priorities — **speed**, **quality**, and **cost** — form
> a trade-off triangle: you can optimize at most two of the three simultaneously.
> Choose the two you care most about (the third is the one you are willing to
> sacrifice):
>
>   A. **Quality + Cost** — best results within budget; speed is not the goal.
>   B. **Speed + Cost** — fast and cheap; accept rework risk / lower assurance.
>   C. **Speed + Quality** — fast and high-assurance; cost is not a constraint.
>
> Enter A, B, or C (or describe your priorities and I will map them)."

- If the user describes priorities in plain language, map to A/B/C before
  proceeding (e.g. "I want it done fast and I don't care about cost" → C;
  "I want it cheap and correct" → A). Re-confirm the mapped pair with the user.
- If the answer is not A/B/C or a clear description, re-prompt once. If still
  unclear, default to **A (Quality + Cost)** and note the assumption.

---

## §3 — Autonomy question (independent of the triangle)

The work paradigm (autonomy) is **not** part of the speed/quality/cost triangle
— it is asked separately. Present:

> "How much autonomy should the agent have?
>   - **Supervised** (default) — you confirm each major step.
>   - **Weiss** (Collaborative) — you lead design decisions; agent researches
>     and assists.
>   - **Noir** (Autonomous) — agent leads design, planning, and integration;
>     you review milestones.
>
> Enter Supervised, Weiss, or Noir (default: Supervised)."

- Accept aliases: `Collaborative` → Weiss, `Autonomous` → Noir
  (case-insensitive). If unrecognized, re-prompt once; fall back to Supervised.

---

## §4 — Mapping table + resolution

Resolve the priority pair (§2) and autonomy (§3) to concrete dial values per
`cost-governance-design.md` §F:

| Priority pair | Sacrifices | execution-strategy | model-effort-profile | verbosity default | Rationale |
|---|---|---|---|---|---|
| **A — quality + cost** | speed | **Cheap-Slow** | **Efficient** or **Autonomous** | `terse` | Low fan-out minimizes parallelism waste; selective Opus buys quality only on review/large; cost-priority pairs → terse. |
| **B — speed + cost** | quality | **Fast** | **Eco/Budget** or **Low Effort** | `terse` | Wide cheap fan-out for minimum wall-clock at minimum rate; accept rework risk; cost-priority → terse. |
| **C — speed + quality** | cost | **Fast** | **High Effort** | `normal` or `verbose` permitted | Pay for both: max parallelism on the top tier; quality-priority → narration earns its keep. |

Combine with the chosen work paradigm and present a summary before applying:

> "Here is the resolved configuration:
>   - Work paradigm: **\<Paradigm\>**
>   - Execution strategy: **\<Strategy\>**
>   - Model/effort: **\<Profile\>**
>   - Verbosity default: **\<terse | normal\>**
>
> Apply this configuration? (Yes / No — or adjust a dial)"

If the user adjusts any single dial, update that dial in the summary and
re-present. Proceed on confirmation.

---

## §5 — Steady Steward application

Apply the §G preset bundle from `cost-governance-design.md`:

| Dimension | Value |
|---|---|
| work-paradigm | **Noir** |
| execution-strategy | **Cheap-Slow** |
| model-effort-profile | **Eco/Budget** |
| verbosity default | **terse** |
| budget posture | low daily budget + `on-approach: defer-non-critical` |
| schedule | `off-peak-only` (wiring completes v1.16) |
| work-scoping rule | one ready, low-risk, bounded-blast-radius item per wake |

Announce before writing:

> "Applying the **Steady Steward** preset:
>   - Work paradigm → Noir
>   - Execution strategy → Cheap-Slow
>   - Model/effort → Eco/Budget
>   - Verbosity → terse
>   - Budget posture: low daily budget, defer non-critical on approach
>   - Note: autonomous scheduling and unattended push wire up in v1.16.
>
> Proceeding…"

Then execute §6 with these resolved values.

---

## §6 — Write through the switch skills

**Do not** write `.claude/grimoire-config.json` directly. Call the existing
switch skills in order:

### 6.1 Work paradigm

Call `grm-work-paradigm-switch` with the resolved paradigm value
(e.g. `Noir`, `Supervised`, `Weiss`).

### 6.2 Execution strategy

Call `grm-workflow-variant-switch` with the resolved strategy
(`Fast`, `Efficient`, or `Cheap-Slow`).

### 6.3 Model/effort profile

Call `grm-model-effort-profile-switch` with the resolved profile
(`Eco/Budget`, `Low Effort`, `Efficient`, `Autonomous`, `Medium`, `High Effort`).

### 6.4 Verbosity

Write `cost-governance.verbosity.default` into `.claude/grimoire-config.json`
directly (no standalone switch skill exists for verbosity in v1.15). Read the
current config, apply the minimal change (add or update
`cost-governance.verbosity.default`; leave all other fields unchanged),
and write back.

```json
{
  "cost-governance": {
    "verbosity": {
      "default": "terse"
    }
  }
}
```

If a `cost-governance` block already exists, merge into it (add/overwrite the
`verbosity.default` key only; preserve all other `cost-governance` sub-keys).
Do not remove any existing `cost-governance` keys.

---

## §7 — Confirm + tips

After all switch skills complete, confirm:

> "Configuration applied:
>   - Work paradigm: **\<Paradigm\>**
>   - Execution strategy: **\<Strategy\>**
>   - Model/effort: **\<Profile\>**
>   - Verbosity: **\<terse | normal | verbose\>**
>
> Switch any dial at any time:
>   - `grm-work-paradigm-switch` — autonomy level
>   - `grm-workflow-variant-switch` — dispatch shape
>   - `grm-model-effort-profile-switch` — cost/quality tier
>   - `/priority-picker` — re-run this advisor"

For the Steady Steward, add:

> "Steady Steward is active. The one-item-per-wake scoping rule applies to
> autonomous dispatch. Full scheduling and unattended push wire up in v1.16."

---

## Error conditions

| Condition | Behaviour |
|---|---|
| `grimoire-config.json` missing | Abort with "Config not found — run `workflow-bootstrap --restore` before running the picker." |
| Switch skill aborts (registry / paradigm dir missing) | Surface the switch skill's error; do not proceed to subsequent dials; suggest `workflow-bootstrap --restore`. |
| Verbosity write fails | Warn; all other dials already written; suggest manual edit. |
| User picks Steady Steward but is not on Noir | Note the paradigm switch; confirm Noir activation; proceed. |

---

## Anti-patterns

- Writing config directly (bypassing switch skills) for paradigm, strategy, or
  profile — §6 always calls the three switch skills; only verbosity is a direct
  write in v1.15 (no standalone verbosity-switch skill exists yet).
- Deriving one dial from another (e.g. silently forcing `Autonomous` under Noir,
  or deriving execution strategy from the profile) — each dial is independent;
  the mapper only uses the §4 table to set the three dials from the priority pair.
- Embedding dispatch or tier logic — the picker is a pure writer; all logic lives
  in the consuming skills it calls.
- Presenting the Steady Steward as fully wired in v1.15 — always note the v1.16
  scheduling/push dependency (§1, §5, §7).
- Bumping `schema-version` when writing verbosity — the `cost-governance` block
  is additive at schema-version 3; no version bump (see `cost-governance-design.md`
  §A).
- Asking the priority pair and the autonomy question in the same `AskUserQuestion`
  call — they are separate, independent questions.
- Applying the Steady Steward bundle without an announcement and confirmation (§5
  always announces before writing).

## One-pick intent → dial set (v1.31, #66)

Offered as the **default** onboarding path: pick a single intent and the picker
writes a coherent dial set (via the existing switch skills). Authority:
`docs/design/defaults-quickstart-design.md`.

| Intent | work-paradigm | execution-strategy | model-effort | verbosity |
|---|---|---|---|---|
| **Ship fast** | Noir | Fast | Efficient | low |
| **Highest quality** | Supervised | Cheap-Slow | High Effort | high |
| **Lowest cost** | Noir | Cheap-Slow | Eco-Budget | low |
| **Long-horizon steady** | Noir | Efficient | Medium | low (Steady Steward preset) |

Manual per-dial selection remains available as the second path. The Steady
Steward preset is the one-pick long-horizon custodian.
