# cost-budget — reference

Detailed formats, mode tables, and anti-patterns for the `grm-cost-budget` skill.
The operational head (config schema + procedure) lives in `SKILL.md`; read this
file when you need a specific format or the full mode/verbosity tables. Design
authority: `docs/grimoire/design/cost-governance-design.md`.

## §3 — Persistence format

`.claude/cache/cost-utilization.json`:

```json
{
  "window-start": "2026-05-31T00:00:00-04:00",
  "period": "daily",
  "accumulated": 1234567,
  "unit": "tokens",
  "last-updated": "2026-05-31T14:22:10-04:00",
  "crossed-thresholds": [50]
}
```

| Field | Type | Meaning |
|---|---|---|
| `window-start` | ISO-8601 with tz offset | Start of the current budget window. |
| `period` | `"daily"` \| `"weekly"` | Reset cadence. |
| `accumulated` | number | Tokens accumulated this window (best-effort). |
| `unit` | `"tokens"` \| `"cost-units"` | Matches config. |
| `last-updated` | ISO-8601 with tz offset | Timestamp of the last write. |
| `crossed-thresholds` | number[] | Thresholds already emitted in this window (avoids re-emission). |

## §4 — On-approach modes

Once the highest configured threshold is reached, the governance layer enters
one mode. **Modes are soft** — they change what work is dispatched and how
loudly; they never interrupt an in-flight response.

| Mode | Behaviour at/after the top threshold |
|---|---|
| `warn-only` | Emit the threshold warning; no behavioural change otherwise. Use when you want visibility without altering dispatch. |
| `terse` | Switch every subsequent spawned agent to **terse** verbosity (overriding §7 resolution order) to shave output cost; emit the threshold warning. Use when budget headroom is tight but work must continue. |
| `defer-non-critical` | Stop spawning *non-critical* work (anything not on the current release's critical path). Finish in-flight items; defer the rest and report what was deferred. Critical-path items still run. Use for most autonomous budgets — keeps important work flowing, pauses discretionary spend. |
| `pause-and-report` | Spawn no further work. Checkpoint release state (§5 ledger + branch tips, optionally `.claude/cache/cost-checkpoint.json`) and report a clean stopping point. Under Noir, schedule resume at the next allowed window or after the `reset-period` rolls (§E of the design doc). Use when the budget ceiling is firm and unattended over-spend is unacceptable. |

Mode escalation is monotonic within a window: lower thresholds emit warnings;
only the top threshold activates the configured mode.

## §5 — Output format

### Threshold warning (inline, one line)

```
Budget: 80% of 5M-token daily budget used (4.0M/5.0M). Mode: defer-non-critical now active.
```

Pattern: `Budget: <pct>% of <amount><unit-abbrev> <period> budget used (<used>/<total>). Mode: <mode> now active.`

Emit at each newly-crossed threshold. Omit the mode clause for thresholds below
the top one (those are informational only).

### Session-end utilization summary

At the end of any session that tracked a budget, print:

```
Cost budget summary
  Window : daily (2026-05-31, America/New_York)
  Used   : 4,200,000 / 5,000,000 tokens (84%)
  Status : defer-non-critical active (top threshold 80% crossed)
  Classes: input 1.8M · output 0.9M · cache_read 1.2M · cache_creation 0.3M
  Deferred work: [list items deferred, or "none"]
```

If the `cost-governance.budget` block is absent → omit this section entirely.

### Deferral report (peak-hour)

```
Peak-hour policy: deferring autonomous work until 18:00 America/New_York (off-peak). 3 items queued.
```

## §6 — Defer-and-reschedule (peak-hour)

When autonomous/scheduled work would start inside a blocked window:

1. **Do not dispatch.** Do not start new agent spawns.
2. **Compute the next allowed start**: the end of the current blocking window,
   in the configured timezone (or the next moment outside all windows if
   multiple overlap).
3. **Record the deferred work queue** so the woken session knows what to
   resume. Under Noir, schedule a wakeup via the `ScheduleWakeup` / scheduled-
   tasks primitive for that moment (the scheduling primitive itself is a v1.16
   building block — #11/#13; v1.15 defines the *policy + defer decision* and
   prepares the deferred queue).
4. **Emit the deferral report** (§5).
5. **On wakeup**, re-check the schedule. If now allowed → proceed. If still
   inside a window → re-defer and re-report.

This mechanism is reached **only by autonomous/scheduled dispatch** (the Noir
default-dispatch path and any scheduled routine). Interactive `grm-release-phase`
runs skip the schedule check entirely.

## §7 — Verbosity: a co-tunable cost dimension

### Why verbosity drives cost (research conclusion)

Verbosity is a genuine cost dimension, not a cosmetic preference:

1. **Output length dominates** (~80% of verbosity-driven cost). Verbosity
   directly scales output tokens — the priciest class. A verbose agent that
   narrates reasoning, restates context, and writes long summaries spends real
   money on the highest-multiplier class.
2. **Tool-call descriptions are a real, often-overlooked contributor.** Each
   tool call carries a `description`; a verbose agent writes longer descriptions
   and narrates around each call. Across a many-tool-call agent these add up as
   output tokens. Terse agents that keep descriptions to the required 5–10-word
   minimum measurably cut this.
3. **Input instructions are a minor, fixed-ish contributor.** Telling an agent
   "be terse" costs a few input tokens once, and those tokens are cached behind
   a stable prefix on warm reads — marginal cost is near-zero. The instruction
   is cheap; its effect on output is what costs.

Net: verbosity is worth tuning alongside model tier and dispatch fan-out.

### How agents honour verbosity levels

| Level | Behaviour |
|---|---|
| `terse` | Minimal prose; bullet points preferred; no reasoning narration; tool-call descriptions at the required minimum (5–10 words); session-end summary is one paragraph or a tight table. |
| `normal` | Concise by house style (default). Brief explanations when useful; no extended preamble or restated context; tool-call descriptions clear but not verbose. |
| `verbose` | Full reasoning shown; context restated for auditability; longer summaries; tool-call descriptions may include rationale. Use only where correctness/auditability dominates cost (e.g. a `grm-reviewer` or `grm-researcher` that should show its work). |

### Resolution order (highest priority first)

1. `on-approach: terse` mode active (§4) — overrides all below.
2. `cost-governance.verbosity.by-agent[<role>]` — explicit per-role override.
3. `cost-governance.verbosity.default` — project-wide default.
4. Profile-derived default:
   - `Eco/Budget` or `Low Effort` model-effort-profile → **terse** (cost-saving
     postures should not pay for narration).
   - `High Effort` → **verbose** permitted (quality/auditability priority).
   - All other profiles (`Medium`, `Efficient`, `Autonomous`) → `normal`.
5. House default: `normal`.

### Verbosity and the priority-picker

The `grm-priority-picker` skill (§F of the design doc) sets a verbosity default
consistent with the chosen 2-of-3 priority pair:

| Priority pair | Verbosity default |
|---|---|
| quality + cost (sacrifices speed) | `normal` |
| speed + cost (sacrifices quality) | `terse` |
| speed + quality (sacrifices cost) | `normal` / `verbose` permitted |

## §8 — Anti-patterns

- **Blocking an interactive session** on schedule or budget. Interactive work
  is never affected by the schedule check; the `pause-and-report` mode reports
  and defers — it does not abort in-flight responses.
- **Hard mid-response blocking.** v1 is soft governance; a generation in flight
  is never interrupted. Hard enforcement is a planned follow-up gated on a
  reliable in-run quota signal (§E.1 of the design doc).
- **Per-agent sub-budgets.** v1 is one aggregate counter for the whole project
  session — no per-agent isolation. Do not attempt to enforce sub-budgets here.
- **Depending on catching a provider cap mid-run.** Account-level rate-limit /
  hard-cap state is not reliably exposed in-run (§E of the design doc). The
  budget layer is the *project-declared proxy* for the cap. Do not design as if
  a live "remaining quota" oracle exists.
- **Re-emitting a threshold warning already crossed.** Track `crossed-thresholds`
  in the ledger; emit each threshold at most once per window.
- **Checking the schedule for interactive sessions.** Only autonomous/scheduled
  dispatch consults `cost-governance.schedule`. A user at the keyboard always
  runs immediately.
- **Writing the dials.** The governance layer reads `work-paradigm`,
  `workflow-variant`, and `model-effort-profile` to estimate and track cost; it
  **never writes them**. The `grm-priority-picker` skill is the pure writer for the
  dials. Cost-budget is a reader + reporter only.
- **Bumping schema-version.** Adding `cost-governance` is additive at
  schema-version 3; no bump, no migration. A reader that lacks the block treats
  it as "ungoverned."
