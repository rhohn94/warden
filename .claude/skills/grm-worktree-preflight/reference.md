# Grm-worktree-preflight — reference

Loaded on demand by `SKILL.md`, only once `preflight.py` (or a manual check)
actually fails, or when running the optional self-healing sweep / claiming a
port.

## Remediation — root-check / release-only-grep failure

Pick the case that matches. `preflight.py`'s FAIL line names which signal
tripped (root-check vs release-only-grep) — see `SKILL.md` for how the two
combine into one verdict.

> **Git-protocol governance.** The default is branch-and-merge;
> history-rewriting commands (`git reset --hard`, `git rebase`) are a
> **last resort**, used here only to re-root a *wrong-based work-item branch*
> (an unprotected branch — never `dev` / `main` / `version/*`, where the guard
> blocks them outright). Each destructive step **requires explicit per-action
> user confirmation**. If you can avoid rewriting — e.g. by re-creating a fresh
> branch from the correct ref and re-applying changes — prefer that.

### Case A — wrong base, **no commits yet** on the branch

No work to lose, so the simplest fix is to **re-create the branch from the
correct ref** (no history rewrite at all):

```bash
git switch -c <branch> dev   # branch in place from the staging ref
```

If you must keep the same branch name in place, the last-resort form is a hard
reset — **destructive; requires explicit user confirmation each time** (per
CLAUDE.md "Commits" rule). Authorisation is per-action, not per-session:

```bash
git fetch
git reset --hard dev   # LAST RESORT — confirm with the user first
```

### Case B — wrong base, **commits exist** on the branch

Preserve the branch's own commits onto `dev`. The non-rewriting default is to
create a fresh branch from `dev` and re-apply (merge or re-commit) the work.
Where that is impractical, the last-resort rewrite is a scoped rebase —
**confirm with the user first**, as it rewrites the branch's history:

```bash
git rebase --onto dev <old-base> HEAD   # LAST RESORT — confirm first
```

`<old-base>` is what `git merge-base HEAD dev` *currently* reports (i.e. the
wrong base). Resolve conflicts as they appear; never use `--no-edit` with
rebase (not a valid flag) and never `-i` (interactive, unsupported in this
harness). This operates on the *work-item* branch only — never rebase a
protected branch (the guard blocks it).

### Case C — about to merge a wrong-based branch into `dev`

Do **not** paper over it by merging `dev` *into* the work branch first.
That pulls release commits along with `dev` into the eventual back-merge.
Run Case B remediation first, then merge.

## Guard step — refuse cross-worktree git ops (run before any branch op)

The v1.6 vet caught a spawned agent whose `git switch -c` landed in the
**integration master's** worktree instead of its own, silently switching the
master off `version/{X.Y}`. Before any `git switch -c`, `git branch`, or
`git switch`, run this guard:

1. **Confirm your cwd is your own worktree.** `pwd` — it must be the path
   the harness spawned you in (contains `/.claude/worktrees/<your-id>/`).
2. **Never redirect git at another path.** Do not pass `git -C <path>`,
   `--git-dir`, or `--work-tree`, and do not `cd`/`pushd` into another
   worktree first. Every git command must act on your own cwd.
3. **Refuse if the target carries the integration marker.** If the worktree a
   command would act on contains `.claude/integration-allow.local` and that
   worktree is **not your own**, STOP — that is the integration master's
   worktree. Branching/switching there is the exact hijack the guard hooks
   block. Branch in place in your own worktree instead.
4. **Branch in place from the staging ref** (`git switch -c <branch>
   version/<X.Y>`); never `git switch` to an existing branch that may be
   checked out in another worktree (git will refuse, or worse relocate it).

Mechanical enforcement (fail-closed, `exit 2` for an unmarked actor targeting
another — especially the marked integration — worktree) is canonical in
`docs/grimoire/integration-workflow.md` §Enforcement (guard hooks). The guard
step here is the human-readable procedural counterpart — follow it so you
never trip the hook.

## Self-healing sweep (integration master, session start only) (#452)

`preflight.py --self-heal --landed-ref <ref>` mechanizes steps 1-4 below in
one read-only call: it inventories every worktree, classifies each branch
with `agent_branch_namespace.is_agent_branch`, applies the #444 safety
predicate via `worktree_reap.is_safe_to_reap`, and reports (never mutates) —
one `SAFE to reap` / `SKIP: <reason>` line per agent-created worktree. It
never touches your own worktree, a human branch, or a protected ref.

Run this **once, at the very start of an integration master's session**,
before `SKILL.md`'s root check and before any new work begins. It is not part
of the per-task-agent preflight — a task agent already runs in a single fresh
worktree the master dispatched it into and has nothing to sweep. This step is
how the **master** inventories and self-heals *dead* leftovers accumulated
across prior sessions — an abandoned sibling worktree, a crashed prior
master's worktree, a stale integration marker — before it starts driving new
work. It composes the #449 reaper engine and the #456 namespace predicate; it
does not re-derive either.

The manual steps `preflight.py --self-heal` performs, spelled out (for when
you need to reproduce or debug the sweep by hand):

1. **Inventory every worktree.**

   ```bash
   git worktree list --porcelain
   ```

   Parse the `worktree <path>` / `branch refs/heads/<name>` pairs — the same
   shape `worktree_reap.py`'s internal `_worktree_branch_map()` already
   parses. Don't re-derive the parsing; read the porcelain output the same way.

2. **Filter to agent-created worktrees.** Classify each worktree's branch with
   `is_agent_branch()` (#456):

   ```bash
   python3 -c "
   import sys
   sys.path.insert(0, '.claude/skills/grm-worktree-preflight')
   from agent_branch_namespace import is_agent_branch
   print(is_agent_branch('<branch-name>'))
   "
   ```

   Skip (never touch) any worktree whose branch classifies `False` — a human
   branch, or a protected ref (`main`/`dev`/`version/*`/`home`), is never a
   sweep target. **Always skip your own worktree** (the one whose path matches
   your current `pwd`) regardless of its branch name — you are never a dead
   leftover of yourself.

3. **Resolve `--landed-ref`.** The current release's `version/{X.Y}` if one
   exists for the in-flight release (check `git branch --list version/*`),
   else `dev` — the same resolution the branch-in-place rule and Step 0.5
   use. Never hardcode it; re-resolve fresh each sweep, since the release may
   have promoted since the last sweep ran.

4. **Apply the #449 predicate and reap what's safe.** For every agent-created
   worktree surviving step 2, call the canonical engine — never re-derive the
   predicate inline:

   ```bash
   python3 .claude/skills/grm-worktree-preflight/worktree_reap.py \
     --worktree <path> --landed-ref <resolved-ref> --dry-run
   ```

   Run `--dry-run` first and read the report before doing anything
   irreversible. Only once the dry-run report looks right, re-run the same
   command without `--dry-run` to actually reap the targets it marked
   `DRY-RUN would remove`. Never skip straight to the destructive form —
   `preflight.py --self-heal` never does this for you by design; treat its
   report as the input to your own `worktree_reap.py` call.

5. **Report everything, never silently.** For every worktree the sweep could
   not reap, report a line naming the worktree path, its branch, and the
   *reason* verbatim from `worktree_reap.py`'s `SKIP … — unsafe to reap:
   <reason>` output (`preflight.py --self-heal` already does this). **Unmerged
   commits is not an error to paper over** — it most likely means a sibling
   agent's in-progress worktree, not a dead leftover. Flag it prominently and
   leave it completely alone: no partial cleanup, no touching files inside it,
   no deleting just the marker or just part of the tree.

6. **Priority case — prior master's abandoned worktree + stale integration
   marker.** A worktree that contains `.claude/integration-allow.local` (the
   integration-master marker) but is **not your own worktree** is exactly the
   "prior master abandoned mid-session" scenario this issue names. Handle it
   with the same predicate — no special-casing of the marker file itself:
   - **Predicate says safe** (branch fully remote-reachable and already an
     ancestor of the resolved `--landed-ref`) → reap the whole worktree via
     `worktree_reap.py` as normal; the marker file goes with it as part of the
     worktree directory removal. Report this case clearly and distinctly from
     an ordinary swept agent worktree — a stray integration marker on disk
     usually means a crashed or killed master session, worth calling out even
     though the mechanical handling is identical to any other reap.
   - **Predicate says unsafe** (e.g. unmerged commits) → **do not touch the
     worktree or the marker file inside it.** An unmerged, marker-bearing
     worktree may be a genuinely *live* sibling master (or a master paused
     mid-session, not crashed) — the safety predicate, not the marker's mere
     presence, is what decides dead vs. live. Report it prominently and move
     on; a human, or a later sweep once the branch actually lands, resolves it.

**Conservatism, not aggression.** This step self-heals *dead* leftovers only;
it does not aggressively clean up anything with any doubt attached. Any
ambiguity — the predicate can't run cleanly (a git command fails
unexpectedly), a worktree looks recently touched, `--landed-ref` can't be
resolved confidently — is treated as "skip and report," never "reap anyway."
Getting this wrong in the aggressive direction destroys a sibling's
in-progress work; getting it wrong in the conservative direction only leaves a
report line for a human to act on later. Always prefer the latter failure
mode.

## Port claim (before building / running the app in this worktree)

When parallel worktree agents each **build and run** the project, they collide
on a default port (3000/8080): the second launch fails, or worse, an agent's
traffic silently hits a sibling's running instance — false passes / misleading
failures. Before launching anything, claim a unique, verified-free port with the
**`claim_port.py`** helper — a single deterministic call, not ad-hoc `lsof`
reasoning (scripting-unification #75):

```bash
# default os-assign strategy (kernel picks a free port); idempotent per worktree
export GRIMOIRE_APP_PORT=$(python3 .claude/skills/grm-worktree-preflight/claim_port.py --worktree-id "$(basename "$PWD")")
```

- The script **probes and reports** — it does not hold the port bound; your app
  binds it. It is **idempotent per worktree-id** (a repeat call returns the same
  port if still free) via the gitignored `.claude/cache/port-claims.json`.
- Strategies (config `worktree-ports.strategy`): **os-assign** (default,
  recommended), **random-probe** (within `range-start`–`range-end`), **index**
  (deterministic `range-start + index` — pass `--index` the worktree's dispatch
  index). Exit 1 = no free port → **abort the launch**, do not fall back to a
  default port.
- The launching skill (e.g. `grm-agent-environment-manager`) reads the env var
  (`worktree-ports.env-var`, default `GRIMOIRE_APP_PORT`) instead of hardcoding a
  port. When the worktree is removed after merge, its cache rows are cleared with
  the worktree.

The integration master / write-capable Workflow tier claims the port **before**
the spawn and passes it to the agent (env var or `args`), so every dispatched
agent runs identical claim logic regardless of how it was prompted.
