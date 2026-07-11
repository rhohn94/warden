---
name: grm-fleet-audit
description: Run the recurring (monthly) meta-planner fleet audit across every Grimoire-managed repo — release/publish conformance, tracker reconciliation, duplicate-implementation detection, mandate-compliance sweep, framework-version drift — filing evidence-backed `audit`-labeled issues. Agent-driven (no linter/AST). Use when running, scheduling, or reviewing the monthly fleet audit, or "how healthy is the fleet".
---

# Monthly fleet meta-planner audit

> **Problem this closes:** the 2026-07-04 meta-planner fleet audit produced
> the highest-quality backlog this project has had — every item evidence-backed
> (#284-#292), several catching mandates that had silently evaporated (#284
> re-discovered #87's undelivered meta-updater; #292 found four repos' trackers
> lying about open work). That audit was a one-off; without a cadence the same
> failure classes re-accumulate invisibly between audits — the fleet-level
> analog of running doc-assurance only once, ever. See
> `docs/grimoire/design/fleet-audit-design.md` for the full design; this file
> is the operating guide.

This is a **meta-planner** procedure: it runs at the fleet level, above any
single repo. It reads and reasons across every Grimoire-managed repo checked
out locally, not just the one it is invoked from. It is **agent-driven**, like
`grm-coding-practices-audit` — no linter or AST; the audit surface is a fixed
checklist below, executed by reasoning over each repo's tracker, docs, and
source.

## When this runs

- **Monthly**, autonomous under Noir — see "Scheduling the monthly recurrence"
  below. This is the primary, intended cadence; a scheduled run is not
  optional follow-through, it *is* the point of this skill (issue #297).
- On demand, when the operator asks for a fleet health check.
- Before a Project Manager kicks off a new multi-repo release arc, as a
  scoping input.

## Step 0 — Enumerate the fleet

The fleet is every Grimoire-managed repo reachable from the local machine
(sibling checkouts alongside this one, e.g. `familiar`, `mission-control`,
`retro-game-player`, `design-language`, `forge-engine`, `obsidian`, …). There
is no committed fleet registry yet — enumerate by listing sibling directories
of this repo's parent that contain a `.claude/grimoire-config.json` (a
Grimoire-managed project marker):

```bash
for d in ../*/; do
  [ -f "$d/.claude/grimoire-config.json" ] && echo "$d"
done
```

If the operator names a narrower or different repo set (e.g. "just audit
familiar and mission-control"), scope to that instead — never silently widen
or narrow beyond what's asked. Record the exact repo set audited in the run
summary; a fleet audit that silently skipped a repo is worse than one that
says so.

## Step 6 — File findings

Every finding becomes one issue, filed via the issue-tracker abstraction —
never a raw `gh` call, never hand-edited into a tracker file directly.
MCP-first (per `grm-issue-tracker` §0): prefer `mcp__grimoire-issue-tracker__create_issue`
when the MCP server is registered and preferred; otherwise the CLI:

```bash
python3 .claude/skills/grm-issue-tracker/issue_tracker.py create \
  --title "<repo>: <one-line finding>" \
  --body "<evidence — the exact command/file/line that surfaced it>" \
  --labels audit,fleet-audit-2026-07 \
  --tracker internal
```

Every finding issue carries:
- The **`audit`** label, always.
- A **run-date label** (`fleet-audit-YYYY-MM`, one per calendar month so
  repeat findings across months are distinguishable at a glance).
- A body citing the exact evidence (command output, file:line, tag/commit) —
  never a bare assertion. This is the same evidence-only discipline
  `grm-issue-reconcile` enforces for closes; it applies equally to opens here.

Ensure the label exists first (idempotent no-op if already present):

```bash
python3 .claude/skills/grm-issue-tracker/issue_tracker.py ensure-label audit
python3 .claude/skills/grm-issue-tracker/issue_tracker.py ensure-label fleet-audit-2026-07
```

## Step 7 — Reconcile prior audit items

Before filing new findings, list open issues carrying the `audit` label from
prior runs and check whether their substance has since shipped (their
target repo tagged a release referencing the issue, or the finding is no
longer reproducible):

```bash
python3 .claude/skills/grm-issue-tracker/issue_tracker.py search "label:audit" --json
```

For each prior audit item:
- **Shipped** — comment with the satisfying release/commit evidence, then
  close. Same idempotency discipline as `grm-issue-reconcile`: write a marker
  comment (`<!-- grm-fleet-audit: closed by <run-date> -->`) so a later run
  never re-processes it.
- **Still open, still valid** — comment `still open as of <run-date>` only if
  meaningfully more evidence has accumulated (a repeat "still true" comment
  every month with nothing new is noise, not signal) — otherwise leave it
  untouched.
- **No longer valid** (repo removed from the fleet, scope changed) — close
  with a one-line reason.

## Step 8 — Report the run summary to the operator

End every run — scheduled or on-demand — with a summary in this shape:

```
Fleet audit — <run-date>
Repos audited: <list>
New findings filed: [#N (repo, one-line), ...]
Prior audit items closed: [#M (repo, shipped-in), ...]
Prior audit items still open: [#K, ...]
Repos skipped / inaccessible: [<repo>: <reason>, ...]
```

Under Noir this summary is what a scheduled routine reports to the operator
(see below); under Supervised/Weiss, report it directly in-session.

## Scheduling the monthly recurrence

This is a **Noir-only** autonomous cadence — the fleet audit is read-mostly
(only the reconciliation/close writes in Step 7 touch a tracker, and those
follow the same evidence-and-marker discipline as `grm-issue-reconcile`), so
it fits the "read/report over write" rail every scheduled Noir routine
follows (`docs/grimoire/design/autonomy-scheduling-design.md` §3). It is the
**first** skill to actually consume the `schedule` / `scheduled-tasks`
machinery for a calendar cadence (prior uses of that machinery — `grm-noir-loop`
— are cross-iteration release-loop continuity, not calendar scheduling).

Set it up once with the `schedule` skill:

```
/schedule create --cron "0 6 1 * *" --prompt "/grm-fleet-audit" --name fleet-audit-monthly
```

(`0 6 1 * *` = 06:00 on the 1st of every month; adjust to the operator's
timezone/cadence preference.) Each firing is a fresh, cold-session run — no
warm cache — so this routine deliberately batches every audit dimension
(Steps 1-7) into one scheduled call rather than one routine per dimension,
per the shared cost-amortization rail in `autonomy-scheduling-design.md` §3.

Any write the run performs (Step 7 closes) still obeys the standing push/write
gates: closing an issue is a tracker write, not a git push, so it is not
gated by `autonomous-push.enabled` — but it must still be Noir-only (paradigm
gate identical to `grm-issue-reconcile`'s Disposition rule) and must still go
through the issue-tracker abstraction, never a raw API call from the scheduled
context.

## Idempotency

- Findings: before filing, search for an existing open `audit`-labeled issue
  with the same `<repo>: <one-line finding>` title pattern; skip re-filing an
  exact repeat with no new evidence.
- Reconciliation: the `<!-- grm-fleet-audit: closed by <run-date> -->` marker
  comment (same pattern as `grm-issue-reconcile`) makes a re-run over an
  already-closed audit item a no-op, not a re-close.

## All writes go through the issue-tracker abstraction

Every create / label / comment / close in this procedure calls
`.claude/skills/grm-issue-tracker/issue_tracker.py` (or the equivalent
`mcp__grimoire-issue-tracker__*` MCP tool) — never a raw `gh` invocation and
never a hand-edited tracker file. Routing (internal vs. external audience),
caching, and backend semantics are entirely that abstraction's concern.

## Future extension

Fleet-wide git working-tree hygiene (uncommitted changes, stale branches,
dead worktrees across every fleet repo) is explicitly **out of scope** here —
tracked separately as #326. When that lands, its check slots in as an
additional audit dimension in Step 0-5's sequence; it is not part of this
skill's initial scope.

## Anti-patterns

- Auditing only the repo you happen to be sitting in — this is a **fleet**
  audit; Step 0 must enumerate (or the operator must explicitly narrow) the
  full repo set, and the run summary must say which repos were actually
  covered.
- Filing a finding with no cited evidence (command output, file:line, tag) —
  the entire value of this audit over ad-hoc impression is the evidence trail.
- Closing a prior audit item on a title/keyword match instead of a real
  shipped-release reference — same evidence bar as `grm-issue-reconcile`.
- Re-commenting "still open" on an unchanged finding every single month —
  noise, not signal; only comment when there's new evidence.
- Running the reconciliation writes (Step 7) outside Noir — Supervised/Weiss
  report the same verdict but never write.
- Treating this as a one-off — the whole point of #297 is that the cadence
  is scheduled (see above), not something a human has to remember to
  re-invoke.

## Reference (load on demand)

- `Step 1 — Release/publish conformance` — see `reference.md`
- `Step 2 — Shipped-vs-open tracker reconciliation` — see `reference.md`
- `Step 3 — Duplicate-implementation detection` — see `reference.md`
- `Step 4 — Mandate-compliance sweep` — see `reference.md`
- `Step 5 — Framework-version drift` — see `reference.md`
