---
name: grm-noir-loop
description: Cross-iteration state for the Noir iterative release loop. Under Noir, each /loop firing spawns ONE release-master subagent that owns a full release iteration in isolated context and returns only a 1-2 sentence summary; continuity between iterations lives in a small, size-budgeted .claude/cache/noir-loop-state.json. Use when reading or advancing the loop state across iterations.
---

# Noir loop state (noir-loop)

The deterministic backing for the **Noir iterative release loop**. Full
design: `docs/design/noir-iterative-loop-design.md`; role registry entry
(`release-master`, §A table + §B.12): `docs/grimoire/design/agent-roles-design.md`.

## What it is

Under Noir, each `/loop` firing is one orchestrator turn that spawns ONE
`release-master` subagent. That subagent owns the whole release iteration (plan →
distribute → integrate → release — the existing `grm-release-planning` →
`grm-release-agreement` → `grm-release-phase` → `grm-release-phase-merge` → `grm-project-release`
chain) in its own fresh context, and returns ONLY a 1-2 sentence summary. The
orchestrator therefore grows by ~one sentence per iteration.

Continuity that must survive between iterations lives in
**`.claude/cache/noir-loop-state.json`** (gitignored — machine-local working
memory). Because each spawned subagent reads this file into its own context, it
is **size-budgeted** (default 4096 bytes): an over-budget write is **refused**,
never silently truncated, so subagents reading it stay near-clean.

## Usage

> **Preferred interface — the `grimoire-release` MCP server (v3.27).** When
> `mcp.enabled` and the server is registered (root `.mcp.json`), prefer the
> native tools over recalling the CLI: **`read_loop_state`** (the release-master's
> first move each iteration) and **`advance_loop`** (`summary` + `open[]` +
> `next[]`, at iteration end). They wrap this same `noir_loop_state.py` helper, so
> the size budget + atomic write are unchanged and the file is **file-write-only**
> (the agent commits nothing extra — the state file is gitignored). **CLI
> fallback** (no MCP / disabled) is the helper invocation below — identical
> behaviour. Design: `docs/design/grimoire-release-server-design.md`.

The orchestrator and the spawned release-master drive the state through the
stdlib helper (Python 3, no third-party deps):

```
python3 .claude/skills/grm-noir-loop/noir_loop_state.py --init [--force]
python3 .claude/skills/grm-noir-loop/noir_loop_state.py --read
python3 .claude/skills/grm-noir-loop/noir_loop_state.py --advance --summary S [--open A] [--next X]
python3 .claude/skills/grm-noir-loop/noir_loop_state.py --validate
python3 .claude/skills/grm-noir-loop/noir_loop_state.py --self-test
```

- **`--read`** at the START of an iteration (the release-master's first move).
  Prints near-empty state when uninitialized.
- **`--advance`** at the END of an iteration: bumps the iteration counter, sets
  `last_summary` (the same string returned to the orchestrator), and replaces
  `open_work` / `next_steps`.
- Exit 0 on success; exit 2 on bad input / validation / budget violation.

## Loop contract

- **Release-master model = `orchestrate` band.** The orchestrator resolves the
  active profile's `orchestrate` band (Sonnet in every starter profile) and
  passes the `{model, effort}` pair on the spawn; the release-master escalates
  judgment calls per the integration-master guide §Model & escalation.
- One `/loop` firing = one release-master subagent = one logged summary. No
  reliance on `/clear` / `/compact` (neither is self-invocable anyway).
- Composes with default Noir wakeup-scheduling and the token budget;
  see the design doc §D.
- **Noir only** — Supervised / Weiss run releases in-session via the integration
  master and do not use this loop.
- Push to origin follows `grm-project-release` §push exactly like any other
  Noir release: gated (`AskUserQuestion`, `Push now` / `Hold`) by default, or
  ungated (immediate, no question) under `autonomous-push.enabled: true` —
  the same contract applies to every iteration, not just the first. The
  loop-state helper itself never pushes; the spawned release-master does, as
  part of the existing release chain.
- **Teardown each iteration.** Before returning its summary, the release-master
  runs `integration-workflow.md` §Run teardown (end-of-run) for that iteration's
  dispatched worktrees + schedules. When the loop's terminal condition
  (milestone / quota reached) fires, the orchestrator **cancels the loop's own
  wakeup** rather than re-arming it — a finished loop leaves no live timer.

## Stop conditions (#422)

Beyond the standard `grm-orchestrate-release` stop conditions (merge conflict,
test failure, guard block, isolation failure, doc/config gate failure, user
stop, gated push prompt), the loop itself carries two mechanical stop
conditions so a `/loop` run cannot spin forever:

- **Blocked on human.** Every `--advance` recomputes `progress_hash` — a hash
  over the open-work set + the current `blocker` string. If that hash repeats
  unchanged for `STALL_LIMIT` (3) consecutive iterations — the release-master
  keeps hitting the same human-gated item or the same blocker — `--read` /
  `--advance` report `blocked_on_human: true`. The release-master's **first
  move each iteration** (§Usage) should check this flag: if true, stop
  spawning further iterations and hand off to **`grm-stop-point`** instead of
  returning a summary that just repeats the prior one.
- **Cycle budget exceeded.** `iteration` reaching the configurable
  `max_cycles` cap (default 20, set via `--max-cycles` at `--init` or
  `--advance`) sets `cycle_budget_exceeded: true` — independent of progress,
  a backstop against a loop that keeps finding *different* busywork forever.
  Same handling: stop and run `grm-stop-point`.

Both flags are cheap to check — they ride along on the `--read` the
release-master already does at the start of every iteration (§Usage). Neither
condition is fatal to the release; they mean "wind down cleanly and report,"
which is exactly what `grm-stop-point` does.

## Maintainer note

Stdlib-only per `docs/design/scripting-unification-design.md`. One `NoirLoopState`
class owns load / validate / mutate / atomic-save; every function is covered by
the in-file `--self-test`. The helper is mirrored across the flavors (live root,
claude-code, copilot `scripts/`) and the golden restore baselines — keep them
byte-identical.
