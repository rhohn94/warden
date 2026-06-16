---
name: cost-budget
description: Read and operate the cost-governance config cluster (budget / verbosity / schedule) from grimoire-config.json. Computes session utilization by reusing token-measure, persists periodic budgets to .claude/cache/cost-utilization.json, emits threshold warnings and a session-end summary, documents verbosity-as-cost-dimension and the four on-approach modes, and checks peak-hour policy for autonomous/scheduled work. Triggers on "check the token budget", "report utilization", "am I near my budget", "set a token budget", "cost report", "peak-hour check", "verbosity setting".
---

# Cost budget

Operates the `cost-governance` config cluster: **budget** (#28), **verbosity**
(#27), and **peak-hour schedule** (#29). Reads
`.claude/grimoire-config.json` for the declared policy; accumulates utilization
via the **`token-measure`** harness; persists periodic counters to
`.claude/cache/cost-utilization.json`; emits warnings at declared thresholds;
and checks schedule windows before autonomous/scheduled dispatch.

**Script-first.** The budget arithmetic — window rolling, threshold-crossing
detection, and the `cost-utilization.json` ledger math — is **not** computed by
hand. It runs deterministically through **`cost_budget.py`** (stdlib-only,
`--self-test`-backed). This skill *calls the script and interprets its
structured verdict*; it never hand-rolls percentages, the periodic window, or
the once-per-window crossing logic. The script reuses `token-measure`'s
`parse_usage.py` for transcript parsing (the §B.2 accumulator source). Schedule
windows + verbosity remain agent-interpreted (no arithmetic); §2e/§7 stay prose.

Design authority: `docs/design/cost-governance-design.md`.

> **Aggregate-only, soft governance.** v1 tracks one counter for the whole
> project session — no per-agent isolation, no hard mid-response block. The
> budget shapes *what work is dispatched and how loudly*; it does not abort an
> in-flight generation. Hard enforcement is explicitly out of scope until a
> reliable in-run quota signal is available (§E of the design doc).

---

## §1 — Config schema reference

The whole `cost-governance` block is **optional**. An absent block means "no
budget, no schedule restriction, normal verbosity" — exactly today's behaviour.
Each sub-object is independently optional.

```json
{
  "cost-governance": {
    "budget": {
      "amount": 5000000,
      "unit": "tokens",
      "reset-period": "daily",
      "thresholds": [50, 80, 95],
      "on-approach": "defer-non-critical"
    },
    "verbosity": {
      "default": "normal",
      "by-agent": { "scout": "terse", "reviewer": "normal", "researcher": "verbose" }
    },
    "schedule": {
      "timezone": "America/New_York",
      "windows": { "peak": "08:00-18:00 Mon-Fri" },
      "mode": "avoid-peak"
    }
  }
}
```

### budget fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `amount` | number | — | Ceiling in `unit`. Required if the block is present. |
| `unit` | `"tokens"` \| `"cost-units"` | `"tokens"` | Token count (sum of all classes) or abstract cost unit. Use `tokens` for v1; `cost-units` weighting is a planned follow-up. |
| `reset-period` | `"session"` \| `"daily"` \| `"weekly"` \| `"unlimited"` | `"session"` | The window the budget applies over. `session` = in-memory only; `daily`/`weekly` = requires cross-session persistence (§3); `unlimited` = track + report, never trigger on-approach. |
| `thresholds` | number[] | `[50, 80, 95]` | Percentages of `amount` at which to emit a proximity warning. Sorted ascending; each crossed at most once per window. |
| `on-approach` | enum (§4) | `"warn-only"` | Behaviour once the highest configured threshold is reached. |

### verbosity fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `default` | `"terse"` \| `"normal"` \| `"verbose"` | `"normal"` | Verbosity for any agent not named in `by-agent`. |
| `by-agent` | map<role, level> | `{}` | Per-role overrides. Valid keys: `scout`, `reviewer`, `researcher`, `verifier`, `reporter`, `triager`, `task-agent`, `integration-master`. |

### schedule fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `timezone` | IANA tz string | — | Required when `windows` is set. |
| `windows` | map<name, spec> | — | Named windows. Spec format: `"HH:MM-HH:MM Days"` (e.g. `"08:00-18:00 Mon-Fri"`). Multiple windows allowed. |
| `mode` | `"off-peak-only"` \| `"avoid-peak"` \| `"unrestricted"` | `"unrestricted"` | The policy. `off-peak-only` = autonomous work runs only outside every window; `avoid-peak` = same, but a window-overrunning job may finish; `unrestricted` = windows are informational, no deferral. |

> **Real pricing-window values are TBD.** The schema accepts any
> `HH:MM-HH:MM Days` in any IANA timezone — it is not hard-coded to a specific
> provider's peak hours. The example values above are illustrative only; drop
> real windows in as data when they are known.

---

## §2 — Procedure

### 2a. Read config

1. Load `.claude/grimoire-config.json`.
2. Extract `cost-governance` (may be absent → exit quietly with "No
   cost-governance config found; using defaults (no budget, normal verbosity,
   unrestricted schedule)." unless the caller specifically requested a check).
3. Validate each sub-object against §1. On a malformed value (e.g. unknown
   `on-approach` mode) → warn and fall back to the safe default for that field;
   do not abort the session.

### 2b–2d. Compute utilization, roll the window, evaluate thresholds (one script call)

Steps 2b (utilization), 2c (window management), and 2d (threshold evaluation)
are **one deterministic `cost_budget.py` call** — do not hand-compute any of it.
The script measures the transcript (reusing `parse_usage.py`), rolls the
periodic window if due, accumulates into `cost-utilization.json`, and returns
the crossing verdict + ready-to-emit §5 warning strings.

```bash
# Locate the session transcript deterministically, then evaluate the budget.
TRANSCRIPT=$(python3 .claude/skills/token-measure/parse_usage.py --locate-transcript)
python3 .claude/skills/cost-budget/cost_budget.py evaluate \
    --amount 5000000 --thresholds 50,80,95 --on-approach defer-non-critical \
    --period daily --unit tokens --transcript "$TRANSCRIPT"
```

The JSON verdict carries everything to interpret:

| Field | Meaning |
|---|---|
| `accumulated` / `amount` / `pct` | Window total, ceiling, and utilization %. |
| `newly_crossed` | Thresholds first crossed this call — emit one warning each. |
| `crossed` | All thresholds crossed this window (already-emitted suppressed). |
| `on_approach_active` / `active_mode` | True + the mode (§4) once the **top** threshold is crossed. |
| `window_rolled` | True when the periodic window reset on this call. |
| `warnings` | Ready-to-print §5 warning lines (mode clause only on the top one). |

What to pass:

- `--amount` / `--thresholds` / `--on-approach` / `--period` / `--unit` come
  straight from `cost-governance.budget` (§1). Defaults match §1
  (`thresholds 50,80,95`, `on-approach warn-only`, `unit tokens`).
- `--transcript` is the session `.jsonl` (resolve it with
  `parse_usage.py --locate-transcript`; §2f). Omit it and pass `--add-tokens N`
  if you already have a measured total.
- `unit: cost-units` is reported as raw tokens with a pending note (weighting
  table is a planned follow-up; §1 caveat) — the script never invents weights.

Behaviour the script enforces so you don't have to:

- `reset-period: session` — purely in-memory; **no** ledger file written.
- `reset-period: daily`/`weekly` — rolls `cost-utilization.json` forward when
  `now >= window-start + period` (reset to 0) and persists atomically (§3).
- `reset-period: unlimited` — tracks + reports; **never** activates on-approach.
- Threshold crossings are once-per-window — `crossed-thresholds` in the ledger
  suppresses re-emission.

**Granularity caveat (honest).** Accumulation is only as accurate as what the
current run can observe of its own and its spawned agents' usage. Treat the
tracked number as a best-effort estimate for proximity reporting, not a hard
accounting source of truth. The `.claude/cache/` store is git-ignored,
machine-local, and safe to delete (deletion resets the window counter only).

### 2f. Locate the session transcript

`parse_usage.py --locate-transcript` resolves the newest `*.jsonl` under
`~/.claude/projects/<encoded-cwd>/` deterministically (the encoding replaces
each non-alphanumeric character in the absolute cwd with a dash). Pass `--cwd`
to target a different project root. It exits non-zero with a clear message if
the project directory or any transcript is absent — no agent-side path
arithmetic.

### 2e. Schedule check (autonomous/scheduled work only)

**Skip entirely for interactive sessions.** A user at the keyboard always runs;
only autonomous/scheduled dispatch consults the schedule.

1. If `cost-governance.schedule` is absent or `mode: "unrestricted"` → allow
   immediately.
2. Parse `windows` into `(start_time, end_time, days_of_week)` tuples in the
   configured `timezone`.
3. Check if `now` (in that timezone) falls inside any named window.
   - If outside all windows → **allow**; proceed.
   - If inside a window and mode is `off-peak-only` or `avoid-peak`:
     → **defer** (§6).
4. On wakeup after a deferral, re-check; the window may have shifted.

---

## Reference (load on demand)

Formats, mode tables, verbosity deep-dive, and anti-patterns live in
[`reference.md`](reference.md) to keep this head lean (token-efficiency
convention, v3.21). Read it when you need the specific detail:

- **§3 — Persistence format** (`cost-utilization.json` fields).
- **§4 — On-approach modes** (`warn-only` / `terse` / `defer-non-critical` /
  `pause-and-report`) — the procedure §2d activates one of these at the top
  threshold.
- **§5 — Output format** (threshold warning, session-end summary, deferral
  report) — the exact strings §2d/§2e emit.
- **§6 — Defer-and-reschedule** (peak-hour mechanism) — the detail behind §2e.
- **§7 — Verbosity** (why it drives cost; the level behaviours; the resolution
  order §2 honours; priority-picker mapping).
- **§8 — Anti-patterns** — the guardrails (never block interactive work, soft
  governance only, reader-not-writer of the dials, no schema bump).
