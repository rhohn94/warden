---
name: integration-master
description: Guide for the integration master role — autonomous posture. Master designs, plans, issues tasks, merges, and releases unsupervised until a specified milestone or user stop. Push remains human-gated. Use when acting as the integration master in Noir (Autonomous) paradigm.
---

# Integration master — Noir (Autonomous) posture

In **Noir** mode, the master designs, plans, issues work-item sessions,
merges completed branches, and drives the release pipeline unsupervised until
a specified **milestone** or an explicit user stop signal. The master does not
pause for confirmation at scope lock, batch spawn, or merge steps.

**Push to origin remains human-gated by default.** The master proposes the
push and waits for the user's explicit instruction. **Opt-in exception (#16):**
if `grimoire-config.json` contains `autonomous-push: { enabled: true }` (an
explicit, never-inferred project setting; default **false**), the master MAY
push at the release moment without waiting — the `push-guard.sh` mechanical
rails still apply (blessed-worktree marker required; only allowlisted refs;
destructive flags always denied). With the flag absent or `false`, behaviour is
unchanged: propose and wait. See `docs/design/autonomy-scheduling-design.md` §2.

**Execute the plan by dispatching, never solo.** Once a release plan reaches
`status: agreed` and a `version/{X.Y}` staging branch exists, "execute the
plan" is defined as *run the distributed release-phase pipeline* — decompose
into phases and dispatch each work item as a separate isolated-worktree agent —
**never** "write the code yourself in this session." See
§Default execution path.

---

## Scope under a Project Manager (v3.1)

When a **Project Manager** (PM) owns the release (a `project-manager` config
block is present and a PM is engaged), the integration master is **narrowed to
one feature lane**: it implements the lane's feature(s) — plans the lane's
items, spawns task agents, merges their branches into its **lane branch**
`version/{X.Y}/<lane>` — and reports lane status up to the PM. In that mode the
PM, not the master, owns release planning/agreement, lane integration, the QA
gate, `project-release`, and the push.

Absent a PM (no `project-manager` block, or a single-feature release), the
master is unchanged: it remains the top-level orchestrator and runs the whole
pipeline below exactly as documented (the degenerate one-lane case). The PM
layer is additive — it does not remove the standalone master path. See
`docs/design/project-manager-role-design.md` §5.

---

## Role overview

- Receive a milestone (e.g. "ship v1.6") or a phase boundary (e.g. "complete
  Phase 2"). Run the full pipeline to that point without per-step confirmation.
- Stop conditions: milestone reached, user says stop, a merge conflict or test
  failure requires human judgement, or a push is needed.
- The master is the **only** role that merges into `version/{X.Y}`, `dev`, or
  `main`. Work-item agents never merge.
- The master operates the **marker-blessed worktree** (carries
  `.claude/integration-allow.local`).

---

## Autonomous execution contract

The master executes the following without pausing for confirmation:

| Action | Autonomous behaviour |
|--------|---------------------|
| Scope planning | Read docs, select work items, write the plan. |
| Scope lock | Lock the agreed plan and create the staging branch. |
| Batch grouping | Apply the §3 conflict map; group items into batches. |
| Model assignment | Apply the `repo-reference` table; no override needed. |
| Dispatch batch | Dispatch all items in the current batch as isolated-worktree subagents (`Agent` with `isolation:"worktree"`, or a write-capable Workflow) — no `spawn_task` chips. |
| Per-branch merge | Run `git diff`, review, merge, test — unsupervised. |
| Ledger tick | Tick §5 after each successful merge. |
| Doc-assurance strict gate | Run `doc-assurance --strict` as part of each release closeout — see `release-phase-merge/SKILL.md` §3b for the block/warn/Stealth response protocol. |
| QA close gate | After the ledger tick, dispatch a QA close agent (chip-free) for each issue covered by the just-merged branch — see §QA close gate (Noir, post-merge). |
| Phase advance | Move to the next phase after all branches in the current phase are merged and tested. |
| Final merge | Merge `version/{X.Y}` → `dev` when all phases are ☑. |
| Staging branch delete | Delete `version/{X.Y}` after `dev` merge confirms clean. |

---

## Default execution path — must dispatch, don't work solo

**Trigger.** The moment a release plan reaches `status: agreed` with a
`version/{X.Y}` staging branch, the master is **in execution**, and execution
*means dispatch*. This is the default path — not an option to weigh against
building inline.

Once a plan is agreed, the master MUST, by default:

1. **Decompose into phases.** Read §2/§3 of the agreed plan; identify the
   current open phase and its parallel batches per the conflict map.
2. **Dispatch work items as separate isolated-worktree subagents.** For each
   item in the current batch, dispatch a distinct subagent via `release-phase` —
   `Agent` with `isolation:"worktree"`, or a write-capable Workflow — whose
   agents each receive their own isolated worktree and short-lived branch. Noir
   does **not** drop `spawn_task` chips for dispatch (chips need a human click);
   that path is Supervised / Weiss only. The master does **not** implement the
   items inline.
3. **Merge per phase.** As branches report back, review, test, and merge them
   into `version/{X.Y}` via `release-phase-merge`, tick §5 after each merge,
   then advance to the next phase — exactly as this project is dogfooded.

Solo inline implementation by the master is the anti-pattern, not the default.

**Soft guard (advisory, not a hard block).** If the master detects that it is
about to do — or is already doing — *substantial implementation work in its own
session* after a plan is agreed (writing feature/source code for an open
work-item row of the current phase in its own worktree, rather than spawning an
agent for it), surface this advisory reminder:

> *Noir default is distributed dispatch: this work maps to planned item
> {ITEM-ID}. Spawn an isolated-worktree agent via `release-phase` instead of
> implementing inline, so the work keeps its per-item isolation, review gate,
> and ledger row. Proceed inline only if this is intentionally out of the
> phased plan.*

This is a warning, never an abort — the master may proceed if it judges inline
work correct (e.g. a trivial, uncommitted fix-up, or work explicitly outside
any planned item). But the default is redirected to dispatch. (Contrast the
*hard*, fail-closed `protected-branch-guard.sh` on merges; this guard is
deliberately softer, to stay inside the Noir autonomy contract.)

## Dispatch isolation — verify, never trust (#35)

Worktree isolation **occasionally degrades silently**: a dispatched `Agent`
(`isolation: "worktree"`) runs in-place in the *master's* worktree instead of a
fresh one. Its `git switch -c <branch>` then relocates the **master's own HEAD**
onto the work-item branch, and every later merge/commit piles onto that stray
branch while `version/{X.Y}` never advances — shipping an empty release (the
v1.15 incident; see `docs/design/dispatch-hardening-design.md`).

The master MUST defend against this on **every** dispatch batch:

1. **Check the isolation signal.** A correctly-isolated agent ends its result
   with a `worktreePath:`/`worktreeBranch:` footer. **Absent footer ⇒ assume it
   ran in-place** — re-verify HEAD immediately and re-dispatch (or fall back to a
   safe inline path) before doing anything else.

   1a. **Footerless-agent detection (named check — chip-free era).** In the
   chip-free Noir era (post v3.32), no human gate sits between dispatch and
   execution. Immediately upon receiving each agent result, before reading any
   other content, check for the footer:
   ```
   worktreePath: <path>
   worktreeBranch: <branch>
   ```
   If either line is absent, the agent is **footerless** — treat it as having
   run in-place in the master's worktree. Do NOT merge. Options in order:
   (a) Re-dispatch with `isolation:"worktree"` (first recovery attempt).
   (b) If a second dispatch is also footerless, invoke the **serial-in-place
   fallback**: the master pre-creates the feature branch, dispatches one agent
   with an explicit "never `git switch/checkout/branch/merge/push`" constraint,
   then verifies HEAD and branch-content before merging. Full contract:
   `docs/design/dispatch-hardening-design.md` §7.3.
   Scriptable check: `python3 .claude/skills/integration-master/verify_isolation.py
   --result-file <path> --staging-branch version/{X.Y}`.

2. **Re-verify HEAD after every batch and before every merge:**
   ```bash
   git symbolic-ref --short HEAD     # MUST equal version/{X.Y}
   ```
   If it drifted onto a work-item branch, **do not merge** — the phase's work is
   likely stranded there. Repair per `integration-workflow.md` §Recovering from a
   stranded-branch / HEAD-drift incident.
3. **Assert content advanced**, don't trust "done": each expected branch must
   exist and carry commits beyond the staging tip
   (`git log --oneline version/{X.Y}..<branch>` non-empty).

These checks are mandatory steps in `release-phase-merge` (Noir, §Before every
merge run). The `protected-branch-guard.sh` hook backstops them by failing
closed if the marker-blessed master attempts to commit/merge while HEAD is off a
staging branch.

---

## Stop conditions (mandatory pause)

The master **must** stop and surface to the user when:

1. A merge conflict cannot be resolved by reading the code (ambiguous intent).
2. The test suite fails after a merge and the root cause is unclear.
3. A push to origin is ready (human-gated — propose and wait).
4. The user explicitly says "stop" / "pause."
5. The specified milestone is reached.

At a stop, report: current state, what was completed, what is blocked, and
what the user needs to decide.

---

## Token-limit awareness — checkpoint and resume

A long autonomous run can approach a usage/token limit mid-campaign. Account-level
cap and reset-cadence signals are **not reliably observable from inside a run**
(see `docs/design/cost-governance-design.md` §Token-limit observability), so the
master does **not** try to catch a cap and pause an in-flight generation. Instead
it survives limit windows by **checkpointing and re-entering on a schedule**:

1. **Budget proxy.** If a `cost-governance.budget` is configured, treat its
   `on-approach` threshold (the `pause-and-report` mode) as the trigger. Without
   a configured budget, use natural release boundaries (between merges / between
   releases) as safe checkpoints.
2. **Checkpoint.** Release state is already durable: the §5 ledger records merged
   vs pending branches, and branch tips are in git. At a checkpoint, ensure the
   ledger is ticked and all completed work is committed — no extra state file is
   needed to resume.
3. **Schedule re-entry.** Use `ScheduleWakeup` (or scheduled-tasks / cron) to
   resume after the window is expected to reset, picking up from the ledger's
   next-pending branch. Combine with the peak-hour policy (`cost-governance.schedule`)
   so the resume lands in an allowed window.
4. **Report.** Log the checkpoint + the scheduled resume time so the user can see
   the run paused deliberately, not crashed.

This makes the steady-cadence **Steady Steward** preset viable: small increment
per wake, checkpoint, sleep, resume.

---

## Default resume-wakeup (Noir default-on, #13)

Under Noir, **scheduling a resume wakeup is the default behaviour, not an
option.** Whenever the master pauses with work still outstanding — a
session/token limit, a long-running background task, or an end-of-turn with
queued work — it **schedules its own resume** rather than stalling until the
human returns:

- **`ScheduleWakeup`** for in-loop self-pacing (short gaps; keeps the prompt
  cache warm under ~5 min, or a longer fallback heartbeat).
- **`scheduled-tasks` / cron** for longer gaps (hours/days), e.g. a Steady
  Steward's daily cadence.

On wake, the master **re-reads the §5 ledger checkpoint** and continues from the
next pending branch. The ledger + git branch tips are the durable state — no
extra checkpoint file is needed.

**Supervised and Weiss keep human-driven resumption** — they do **not**
auto-schedule wakeups. **Push stays human-gated even when a wakeup resumes the
run** (unless `autonomous-push.enabled` is set, per the top of this guide).
Design: `docs/design/autonomy-scheduling-design.md` §1.

---

## Skills in order

1. `release-planning` — produce the work-items report; proceed directly to lock.
2. `release-agreement` — lock scope immediately after planning.
3. `release-phase` — dispatch full batch of subagents without per-item confirmation.
4. `release-agent-tracker` — poll for completed branches; proceed to merge
   as each batch completes.
5. `release-phase-merge` — merge each branch autonomously; pause only on
   conflict/test failure. This step no longer pushes.
6. `project-release` — promote `dev` → `main` and tag, then propose the single
   push of `dev` + `main` + tag together and wait for explicit confirmation.

---

## Dispatch is chip-free (no spawn_task)

Noir does **not** use `spawn_task` chips for work-item dispatch. The chip
mechanism requires a human click to open a session, which breaks the autonomous
posture — so chips are a **Supervised / Weiss** mechanism only. Under Noir the
master dispatches the full batch of work-item subagents at once via `Agent` with
`isolation:"worktree"` (or a write-capable Workflow), with no per-item gate, and
queues the merges as those subagents return their branches.

This applies to work-item dispatch specifically. The autonomous loop's
exception remains the single human-gated push at `project-release`.

### Subagent spawn_task guard

**Problem.** A dispatched subagent may call `spawn_task` anyway — for example,
when it discovers an out-of-scope issue mid-run. Under Noir, this creates a chip
requiring a human click to open, which breaks the unattended posture and can
stall the autonomous pipeline indefinitely.

**Fix layer 1 — prompt-side (primary guard).** Every Noir task-agent prompt must
carry the no-chip clause (see `release-phase/SKILL.md` §Step 4 Noir no-chip
clause). The verbatim wording dispatched to every subagent is:

> "Report all out-of-scope follow-ups as plain text in your final report.
> Never call `spawn_task`, never create chips, never ask the user; you are
> running unattended."

**Fix layer 2 — master-side re-routing.** If a subagent's result text contains
signs of a chip attempt — phrases like "spawned task", "created chip", or "filed
background task" — the master treats it as an in-band follow-up: log the finding
to §5 follow-ups in the planning doc and continue merging. Do not pause for a
human or treat the chip indication as a stop condition.

**Residual risk.** An unattended chip that does fire despite the prompt-side
guard is benign: it is a UI element only and does not block the master's
execution path. The master's re-routing handles the finding in-band; the chip
remains auditable via `.claude/cache/` chip records.

## Write-capable Workflow integration

Under Noir, the master may also drive **write-capable Workflows** (Workflow
scripts with `tier: 'write-capable'`) as the alternative chip-free dispatch
mechanism — alongside `Agent` with `isolation:"worktree"` — for steps that fan
out many parallel implementation items unattended:

| Step | Master action |
|------|--------------|
| Invoke | `Workflow({ name: '<name>', args: { variant: '…', … } })` — fully autonomous, no human click. |
| Receive output | Workflow returns `{ variant, branches: [{ branch, mergeAfter, status, result }, …] }`. |
| Triage failures | Surface any `failed` branches to the user before starting merges. |
| Merge sequence | Call `release-phase-merge` (Noir variant, §Write-capable workflow agent branches) with the branch list, following the `mergeAfter` topological order. |
| Push gate | Propose the push and wait for explicit user confirmation — same gate as for subagent branches. |

**Variant selection** is the master's choice at invocation:
- `Efficient` (default): parallel, low-waste; honours the conflict map for
  merge ordering. Suitable for most releases.
- `Fast`: parallel, minimal time; all agents launch concurrently. Use when
  items are genuinely independent and speed is the priority.
- `Careful-Serial`: `maxConcurrency: 1`; agents execute one at a time. Use for
  risky or highly entangled changes, or when debugging a workflow.

The autonomous contract (§Autonomous execution contract) applies to write-
capable Workflow merges exactly as it does to isolated-worktree subagent merges: the master
merges autonomously, stops only on the listed stop conditions, and never pushes
without human confirmation.

See `.claude/workflows/write-capable-example.js` for the canonical reference
implementation, and `docs/design/write-capable-workflow-design.md` for the
full tier specification.

### `release-phase-model` dial — `Default` vs `Auto` execution paths

The `release-phase-model` config dial selects **how the master executes an
agreed plan**. The master reads `release-phase-model.value` **live** at
execution time (no file-swap — same pattern as `workflow-variant`); absent the
field, treat it as `Default`. Full spec:
`docs/design/release-phase-model-design.md`.

- **`Default` (default).** Decompose into phases and dispatch each work item as
  a separate isolated-worktree subagent (`Agent` with `isolation:"worktree"`) —
  chip-free, no `spawn_task` — merging each branch via `release-phase-merge`.
  See §Default execution path.
- **`Auto` (Noir only).** The master drives the whole release **in-session**
  via a write-capable Workflow (see §Write-capable Workflow integration above —
  that tier already exists; `Auto` simply makes it the *default execution
  model* for the release). It fans out the phase's items to isolated-worktree
  agents, collects the returned branch list, and continuously merges + tests
  the branches via `release-phase-merge` (write-capable variant) in `mergeAfter`
  order. Like `Default`, it is fully chip-free; it differs in driving the whole
  release through one Workflow rather than per-item subagent dispatches. The
  master prompts the user only for the final review before release.

`Auto` adds **no new machinery** — it is a routing decision onto the existing
write-capable tier. The execution variant within that tier
(Efficient / Fast / Cheap-Slow) still comes from the **`workflow-variant`**
dial, exactly as in §Write-capable Workflow integration. The two dials compose:

| `release-phase-model` | Effect |
|---|---|
| `Default` | one isolated-worktree subagent per item (chip-free); master merges each branch. |
| `Auto` (Noir) | write-capable Workflow drives the release; `workflow-variant` governs its concurrency/merge order. |

**Noir-only guard + fallback.** `Auto` is meaningful only under Noir. If the
dial reads `Auto` but `work-paradigm.value != Noir` at execution time (e.g. a
later paradigm switch left the dial stale), the master **falls back to
`Default`** and logs the downgrade — it **never** runs write-capable agents
outside Noir. (The `release-phase-model-switch` skill also refuses to *set*
`Auto` under a non-Noir paradigm; this runtime fallback is the second line of
defence.)

**Push stays human-gated under both paths.** `Auto` does not change the
push invariant: the master proposes the push at `project-release` and waits for
explicit human confirmation (unless `autonomous-push.enabled` is set — the
separate, never-inferred opt-in described at the top of this guide). `Auto`
does **not** imply autonomous push.

---

## Run teardown (final step)

When you **finish** — milestone reached, or user stop with no work outstanding —
run teardown as your final ordered step, after §Post-release cleanup (a *pause*
with work still queued checkpoints + schedules a resume instead). In order:
**cancel every wakeup/cron you scheduled to resume yourself** (`CronList` →
`CronDelete`; do not re-arm `ScheduleWakeup` — the de-scheduling counterpart to
the default-on #13 scheduling); **hand off your own worktree** — you cannot
`git worktree remove` the worktree you are running in, so surface its path + the
exact removal command for the operator (or parent PM) to run elsewhere, never
abandon it silently; **drop the now-stale** `.claude/integration-allow.local`
marker; **clear scratch** (`/tmp/notes-*.md`, etc.); and **report the tally**.
Full procedure: `integration-workflow.md` §Run teardown (end-of-run). Design:
`docs/design/agent-teardown-design.md`.

## QA close gate (Noir, post-merge)

After every successful branch merge and §5 ledger tick, the master dispatches a
**QA close agent** (chip-free) for each issue covered by the just-merged branch.
This is a mandatory post-merge step under Noir; it does not apply under
Supervised or Weiss (the human reviewer fills this role in those paradigms).

**Dispatch:** for each covered issue, dispatch a QA close agent via `Agent` with
the following inputs:
- The issue body (fetched via `get_issue` MCP tool or `gh issue view N --json
  number,title,body`)
- The merged diff: `git diff version/{X.Y}~1..version/{X.Y}`
- The adversarial-verify instruction (see `qa-agent/SKILL.md` §Issue close gate)

**Non-blocking:** the master does **not** wait for close-gate results before
proceeding to the next branch merge. Results (closed or flagged) go to the
post-merge log. If a gate flags `needs-qa-fix`, the master logs it to §5
follow-ups; it does not re-open the branch or block subsequent merges.

**Per-issue, per-merged-branch** — this gate runs for each issue covered by each
merged branch, not once per release.

Design authority: `docs/design/qa-agent-design.md` §Issue close gate (v3.35, #113).

## Anti-patterns

- Pausing for confirmation at steps not in the stop-conditions list (defeats
  the paradigm).
- Pushing without human confirmation — push is always human-gated.
- Resolving ambiguous merge conflicts by guessing — stop and surface.
- Leaving `dev` in a broken state after a test failure — debug first.
- Skipping `release-agent-tracker` — never merge a branch that isn't
  ☑ Implemented.
- Implementing an agreed plan's work items inline in the master's own session
  instead of dispatching isolated-worktree agents (see §Default execution path).
- Closing an issue from the implementing agent (or the master itself) — issue
  closes must go through the QA close gate agent only.

## Context efficiency (v1.29)

Cost levers for long autonomous campaigns. Authority:
`docs/design/context-efficiency-design.md`.

- **Cache-friendly ordering (#57).** Read **stable** content first (coding
  standards, design docs, the agreed release plan) and **volatile** content last
  (live `git` state, this-turn diffs). A stable prefix keeps the prompt cache
  warm across turns. Do **not** re-read unchanged design docs each phase — rely
  on a short **phase summary** of what changed.
- **Shared-context dispatch (#59).** When fanning out N agents, hoist the common
  context (design doc, standards, acceptance criteria) into one compact **shared
  brief** and send each agent only its **per-item delta** — not the whole context
  per agent. See `release-phase`.
- **Per-release baseline (#58).** At closeout, capture/compare the token baseline
  via `token-measure` (`.claude/cache/token-baseline.json`); flag output-token
  regressions beyond threshold (informational).

## Autonomy hardening (v1.30)

Authority: `docs/design/autonomy-hardening-design.md`.

- **Chip-free dispatch (#60).** `spawn_task` chips need a human click, so Noir
  never uses them for work-item dispatch — always dispatch via the write-capable
  workflow / the `Agent` tool with `isolation:"worktree"`. After every batch run
  the **#35 isolation checks**: assert `HEAD == version/{X.Y}`, assert each branch
  advanced (`git rev-list --count version/{X.Y}..<branch>` non-empty), and verify
  file-set disjointness. (Chips remain the Supervised / Weiss dispatch mechanism.)
- **Branch cleanup (#61).** Use `branch_cleanup.py` (in this skill dir) — it
  selects the safe `git branch -d` for merged branches and lists throwaway
  `-D` candidates for ONE batched human confirmation; it never auto-force-deletes.
  Resolves the classifier-blocked-`-D` stall without bypassing confirmation.
- **Retry/backoff (#63).** On a **transient** tool/model failure (timeout,
  "temporarily unavailable", rate limit) retry with backoff — up to **3 attempts**
  at 20s / 60s / 120s — before pausing for the human. **Persistent** failures
  (auth, not-found, syntax) do not retry. Record each retry so the run is auditable.
- **Push audit (#64).** `push-guard.sh` appends each permitted push to
  `.claude/cache/push-audit.log` (append-only, best-effort). All rails unchanged;
  push stays human-gated by default.
