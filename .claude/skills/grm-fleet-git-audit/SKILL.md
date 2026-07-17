---
name: grm-fleet-git-audit
description: Read-only, single-repo git hygiene audit — one pass over safe-to-reap agent worktrees, live/unmerged agent worktrees, human-owned worktrees, stale remote branches, and namespace-conformance gaps. Never deletes anything. Use for the fleet's git hygiene state, "what's safe to clean up", or "audit worktrees and branches".
---

# Fleet git audit (#326)

One command that answers "what does this repo's worktree/branch git hygiene
look like right now" — instead of running `worktree_reap.py`,
`agent_branch_namespace.py`, and `stale_remote_report.py` separately and
merging the output by hand. **Report-only.** It never removes a worktree,
deletes a branch, or mutates a remote — every git call it makes (directly, or
via the three modules it composes) is a read.

This is distinct from `grm-fleet-audit`, the monthly **multi-repo**
meta-planner audit (release/publish conformance, tracker reconciliation,
mandate compliance — agent-driven, no script). `grm-fleet-git-audit` is
**single-repo**, script-driven, and specifically about git working-tree
hygiene — the dimension `grm-fleet-audit`'s own design doc names as "out of
scope, tracked separately as #326."

## Run it

```bash
python3 .claude/skills/grm-fleet-git-audit/fleet_git_audit.py
python3 .claude/skills/grm-fleet-git-audit/fleet_git_audit.py --format json
python3 .claude/skills/grm-fleet-git-audit/fleet_git_audit.py --landed-ref dev
```

`--landed-ref` overrides the auto-resolved default (the current release's
`version/{X.Y}` staging branch if one exists, else `dev` — the same
resolution `grm-worktree-preflight`'s Step 0.5 and the integration master's
self-healing sweep both already use). `--remote` and `--min-age-days` pass
through to the stale-remote-branch dimension (defaults `origin` / `30`,
matching `stale_remote_report.py`).

Importable, matching the `generate_report()` shape `stale_remote_report.py`
already established:

```python
sys.path.insert(0, os.path.join(REPO_ROOT, ".claude", "skills", "grm-fleet-git-audit"))
from fleet_git_audit import generate_fleet_report
report = generate_fleet_report(remote="origin", min_age_days=30, cwd=REPO_ROOT)
```

## What it reports

**1. Worktree / branch hygiene.** Every worktree from `git worktree list
--porcelain`, classified via `is_agent_branch()` (#456):
- `safe_to_reap` — agent-namespaced, merged, and remote-safe against the
  resolved `--landed-ref` (the #449 predicate says yes). Not deleted here —
  hand these to `worktree_reap.py` or `trim.py` to actually act.
- `live_unmerged` — agent-namespaced but the #449 predicate says not yet
  safe (unpushed and/or unmerged). Informational, not a problem — almost
  always a sibling agent still in flight.
- `human_owned` — branch is NOT in the agent namespace. Never touched.
  Includes both genuinely human-created branches and the protected staging
  set (`main`/`dev`/`version/*`/`home`), the latter flagged `protected: true`
  so the two are still distinguishable in the report.
- `detached` — a worktree with no branch checked out; nothing to classify.

**2. Stale remote branches.** `stale_remote_report.generate_report()` (#455)
called directly and folded in verbatim — `candidates` (agent-created, merged,
no local copy left — likely safe for a human to delete) and `old_branches`
(age over the threshold, any provenance — needs human judgement). Remote
deletion stays human-gated; this script only reports.

**3. Namespace conformance (#456).** Any branch — local or remote — that
`is_agent_branch()` classifies `True` only through the *legacy fallback
tier* (`worktree-agent-*`, `worker-*`, `wf-*`) rather than the canonical
`claude/` prefix. These are the conformance gaps the namespace convention was
meant to drain down over time; a canonically-namespaced branch never appears
here by construction.

## Self-test

```bash
python3 .claude/skills/grm-fleet-git-audit/fleet_git_audit.py --self-test
```

Hermetic — builds a fixture repo (bare origin + clone) under a temp
directory; never touches this repository's real worktrees or branches.
Covers landed-ref auto-resolution (no `version/*` → `dev`; a `version/{X.Y}`
branch present → picked over `dev`; the highest of several → picked; a
lane-scoped `version/{X.Y}/lane-a` branch → never matched), a safe-to-reap
agent worktree, a live/unmerged agent worktree, a human-owned worktree, a
stale merged remote branch with no local copy, and a namespace-conformance
gap present both locally and on the remote.

## Anti-patterns

- Treating a `safe_to_reap` finding as an instruction to delete — this
  script never acts; hand the finding to `worktree_reap.py`/`trim.py`.
- Importing `worktree_reap.reap()` or its `main()` here — only the read-only
  `is_safe_to_reap()` predicate and `_worktree_branch_map()` helper are used;
  no mutating entry point is ever imported by this skill.
- Re-deriving the `claude/`-prefix or safe-to-reap logic instead of importing
  `agent_branch_namespace.py` / `worktree_reap.py` / `stale_remote_report.py`
  directly — this skill exists specifically so nothing re-derives them.
