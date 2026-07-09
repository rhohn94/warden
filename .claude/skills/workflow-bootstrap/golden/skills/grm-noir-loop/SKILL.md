---
name: noir-loop
description: Cross-iteration state for the Noir iterative release loop (#83, v3.13). Under Noir, each /loop firing spawns ONE release-master subagent that owns a full release iteration in isolated context and returns only a 1-2 sentence summary; continuity between iterations lives in a small, size-budgeted .claude/cache/noir-loop-state.json read/written by noir_loop_state.py (stdlib-only). Triggers on "noir loop state", "iterative release loop", "release-master state file", "read the loop state", "advance the loop", "noir-loop-state.json".
---

# Noir loop state (noir-loop)

The deterministic backing for the **Noir iterative release loop** (#83). Full
design: `docs/design/noir-iterative-loop-design.md`; role registry entry
(`release-master`, §A table + §B.12): `docs/design/agent-roles-design.md`.

## What it is

Under Noir, each `/loop` firing is one orchestrator turn that spawns ONE
`release-master` subagent. That subagent owns the whole release iteration (plan →
distribute → integrate → release — the existing `release-planning` →
`release-agreement` → `release-phase` → `release-phase-merge` → `project-release`
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
python3 .claude/skills/noir-loop/noir_loop_state.py --init [--force]
python3 .claude/skills/noir-loop/noir_loop_state.py --read
python3 .claude/skills/noir-loop/noir_loop_state.py --advance --summary S [--open A] [--next X]
python3 .claude/skills/noir-loop/noir_loop_state.py --validate
python3 .claude/skills/noir-loop/noir_loop_state.py --self-test
```

- **`--read`** at the START of an iteration (the release-master's first move).
  Prints near-empty state when uninitialized.
- **`--advance`** at the END of an iteration: bumps the iteration counter, sets
  `last_summary` (the same string returned to the orchestrator), and replaces
  `open_work` / `next_steps`.
- Exit 0 on success; exit 2 on bad input / validation / budget violation.

## Loop contract

- One `/loop` firing = one release-master subagent = one logged summary. No
  reliance on `/clear` / `/compact` (neither is self-invocable anyway).
- Composes with default Noir wakeup-scheduling (#13) and the token budget (#28);
  see the design doc §D.
- **Noir only** — Supervised / Weiss run releases in-session via the integration
  master and do not use this loop.
- Push to origin stays **human-gated** in every paradigm; the loop never pushes.
- **Teardown each iteration.** Before returning its summary, the release-master
  runs `integration-workflow.md` §Run teardown (end-of-run) for that iteration's
  dispatched worktrees + schedules. When the loop's terminal condition
  (milestone / quota reached) fires, the orchestrator **cancels the loop's own
  wakeup** rather than re-arming it — a finished loop leaves no live timer.

## Maintainer note

Stdlib-only per `docs/design/scripting-unification-design.md`. One `NoirLoopState`
class owns load / validate / mutate / atomic-save; every function is covered by
the in-file `--self-test`. The helper is mirrored across the flavors (live root,
claude-code, copilot `scripts/`) and the golden restore baselines — keep them
byte-identical.
