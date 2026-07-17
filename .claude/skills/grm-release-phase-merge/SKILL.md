---
name: grm-release-phase-merge
description: Merge completed subagent branches into version/{X.Y} autonomously — no per-merge confirmation. Runs tests after each merge, ticks §5, and drives the final version/{X.Y}→dev merge unsupervised. Stops only on conflict, test failure, or push trigger. Handles both isolated-worktree work-item branches and write-capable workflow branches. Push to origin remains human-gated. Use when the user says "merge agent X" or "phase N is done".
---

# Release phase merge (Noir)

Merges completed agent branches into `version/{X.Y}` autonomously in
§3's conflict-map order. No per-merge confirmation. Stops only on
merge conflict requiring human judgement or test failure with unclear cause.
This skill no longer pushes — the single push prompt fires at
`grm-project-release`, not here.

Handles **two branch sources**:
- **isolated-worktree subagent work-item branches** — branches from
  `grm-release-phase`, one per work item (e.g. `nw3-isolated-parallel`), produced by
  `Agent` subagents with `isolation:"worktree"` (chip-free). Listed in the §5 ledger.
- **write-capable workflow agent branches** — branches produced by a
  write-capable Workflow script (e.g. `write-capable-example.js`), one per
  agent item (e.g. `update-config-parser-a3f1`). Listed in the workflow's
  structured `branches` output. See §Write-capable workflow agent branches in
  `reference.md` for the additional pre-merge steps specific to this source.

When `release-phase-model == Auto` (Noir only — see
`docs/design/release-phase-model-design.md`), `grm-release-phase` dispatches the
phase via a write-capable Workflow, so branches arrive through the **second**
source above; merge in `mergeAfter` order per §Write-capable workflow agent
branches. `Auto` adds no new merge machinery. Push gate is unchanged under
both dial values (see §Push to origin — not here).

---

## Before every merge run

> **Preferred interface — `merge_preflight` (grimoire-release MCP, v3.27).** When
> `mcp.enabled`, run **`merge_preflight`** with the staging ref for a
> structured, read-only verdict `{head_ok, branches, blocked}` — act on it
> (`head_ok:false` = HEAD-drift, see stranded-branch recovery below). **CLI
> fallback:** `python3 .claude/skills/grm-release-agent-tracker/release_plan.py
> merge-preflight --staging version/{X.Y}` (numbered steps below are the
> fallback). Design: `docs/design/grimoire-release-server-design.md`.

1. **HEAD-verification gate (MANDATORY — #35).** Assert HEAD is exactly the
   intended staging branch before *every* merge:
   ```bash
   test "$(git symbolic-ref --short HEAD)" = "version/{X.Y}" \
     || echo "HEAD DRIFT — DO NOT MERGE"
   ```
   If HEAD is **not** `version/{X.Y}`, **stop and investigate — do NOT blindly
   `git switch` and proceed.** A drifted HEAD parked on a *work-item* branch is
   the silent worktree-isolation failure (v1.15 incident). Repair per
   `integration-workflow.md` §Recovering from a stranded-branch / HEAD-drift
   incident before any merge.

2. **Isolation-success + branch-content assertion (MANDATORY — #35).** Do not
   trust an agent's "done" report:
   - **Isolation signal:** a correctly-isolated `Agent` (`isolation: "worktree"`)
     ends its result with a `worktreePath:`/`worktreeBranch:` footer. If that
     footer is **absent**, treat the agent as having run in-place — re-verify
     HEAD (step 1) immediately and re-dispatch the item rather than merging.
   - **Content advanced:** confirm each expected branch exists and actually
     carries commits beyond the staging tip:
     ```bash
     git rev-parse --verify {branch} >/dev/null 2>&1 \
       && test -n "$(git log --oneline version/{X.Y}..{branch})" \
       || echo "BRANCH {branch} MISSING OR EMPTY — investigate, do not merge"
     ```

3. **Run `grm-release-agent-tracker`** to confirm which branches are
   ☑ Implemented ☐ Merged and their dependency order.
   For write-capable workflow branches, skip this step — the workflow's
   structured output is the authoritative list (see §Write-capable workflow
   agent branches in `reference.md`).

> **Before-promotion divergence gate (BMI-2).** `merge_preflight` folds a real
> fork into `head_ok:false`; CLI fallback `divergence-check`. On a HALT: merge
> `main` INTO the integration line (never `reset --hard` across the fork).
> Full mechanism, exit codes, and design citation in `reference.md`
> §Before-promotion divergence gate (BMI-2).

---

## Per-branch merge procedure (autonomous)

Repeat for each branch in the merge queue, in conflict-map order:

### 1. Review the diff

```bash
git diff version/{X.Y}...{branch}
```

Verify:
- Scope: within the files listed in §2.{N}.
- No edits to `docs/release-planning/release-planning-v{X.Y}.md` §§1–4.
- No obvious regressions.

If scope creep or a §§1–4 edit is found: stop and surface to the user.
Otherwise proceed immediately.

### 2. Merge

```bash
git merge --no-ff {branch}
```

If there are conflicts:
- Attempt to resolve by reading the code and the item's acceptance criteria.
- If intent is unambiguous: resolve and `git merge --continue`.
- If intent is ambiguous: **stop and surface to the user** — describe the
  conflict and ask for direction.
- **Tiered conflict resolution (v1.30, #62):** before stopping, classify the
  conflict per `docs/grimoire/design/autonomy-hardening-design.md`. Auto-resolvable
  (additive/disjoint hunks, lockfiles, generated artifacts) → resolve, log to
  §5 follow-ups, continue. Semantic/ambiguous → stop and surface. Full
  classification table in `reference.md` §Tiered conflict resolution.

### 3. Run tests

```bash
python3 .claude/skills/grm-build-recipe/recipe.py test
```

Resolves the real command from `.claude/recipes.json` (`grm-build-recipe`
dispatcher, `≡ just test`) — never a literal placeholder.

If tests pass: continue.

If tests fail:
- Identify the root cause.
- If the fix is clear (introduced by the just-merged branch): apply it
  on a fix branch off `version/{X.Y}`, re-merge, re-test, continue.
- If the root cause is unclear: **stop and surface to the user**.

### 3.5 Quality gate (before ticking §5)

Read the `code-quality` block from `.claude/grimoire-config.json` **live**.
Absent block ⇒ defaults (`audit-gate: warn`, `auto-reviewer: noir`,
`coverage-threshold: null`, `typecheck: build`). Design:
`docs/grimoire/design/merge-gate-quality-design.md`.

Run in order; first failing **blocking** check stops the merge:
1. **Type-check / build** (`typecheck: build` → type errors are build failures).
2. **Coverage** (`coverage-threshold: null` → skip by default).
3. **Audit gate** (`audit-gate: warn` → file via `grm-feedback-to-issue`, proceed).
   Sub-step **3a. Dependency-channel conformance** runs when the branch's diff
   touches `vendor.toml` / `vendor.lock` / `vendor/` — warn-only this release
   (never blocks). Same trigger runs **3a′. Vendor provenance integrity**
   (`sync_deps.py --verify`, #315) — offline, also warn-only. Full invocation +
   finding shape for both in `reference.md` §Quality gate detail.
4. **Auto-Reviewer** (`auto-reviewer: noir` → spawn `grm-agent-reviewer`; blocking
   findings stop, non-blocking become §5 follow-ups).

**On any blocking stop:** `git reset --hard ORIG_HEAD` — undo the merge, leave
§5 row unticked, record reason in §5 follow-ups. Re-runnable once branch fixed.

### 3b. Doc-assurance baseline gate (v3.36+; baseline ratchet #426, v3.93)

Run `python3 .claude/skills/grm-doc-assurance/doc_assurance.py --strict
--baseline .claude/cache/doc-findings-baseline.json` as part of the release
closeout. The baseline ratchet
(`docs/grimoire/design/doc-assurance-design.md` §Baseline ratchet) turns the
raw finding count into a trend — **print that trend line verbatim in the
closeout report** instead of an apologetic "pre-existing, not mine"
paragraph. New findings (relative to the baseline) get filed via
`grm-feedback-to-issue` and fail the closeout under `--strict`; baselined and
resolved findings never do. Full trend-line wording for every case (seed /
unchanged / new / ratcheted-down) in `reference.md` §Baseline ratchet
trend-line formats.

**Independent, stricter gate — unchanged:** `hierarchy` / `relative-links`
findings under `doc-hierarchy.enforcer.value: block` (or `--strict`, which
escalates `warn`→`block`) remain an unconditional block regardless of
baseline status. File each such finding via `grm-feedback-to-issue` before
blocking, same as before.

- **Stealth Mode:** Under `stealth-mode.value: "on"`, suppress the
  `grm-feedback-to-issue` auto-filing step (per `stealth-guard.sh` restrictions on
  commit-class actions). Run the check; do not auto-file.

### 4. Tick §5 ledger — edit only, do not commit yet

Flip this branch's row (`☐`→`☑` + short-sha note) via the MCP `tick_rows` tool
or the CLI fallback — **file edit only, no commit here.** Accumulate ticks
across the whole sweep and commit **once**, in the Phase completion check
below, per `grm-ledger-tick/SKILL.md` step 6 ("Commit — once per sweep, not
per branch"). Ledger-tick mechanics (locating §5, tick format, follow-ups,
source-of-truth conflicts) are documented there, not restated here.

Proceed to the next branch without pausing and without committing.

---

## Final merge — `version/{X.Y}` → `dev`

Pre-merge checklist (verify silently):

- [ ] `python3 .claude/skills/grm-build-recipe/recipe.py test` green on `version/{X.Y}`
- [ ] `python3 .claude/skills/grm-build-recipe/recipe.py build` clean
- [ ] All §5 rows ☑ Merged
- [ ] `version-history.md` entry written on `version/{X.Y}`
- [ ] **Before-promotion divergence gate clean** — `divergence-check` reports no
      divergence (the `dev→main` promotion at `grm-project-release` depends on it; a
      real fork HALTs here, reconcile merge-forward per §2/§5 of the design).

Execute autonomously:

```bash
git switch dev
git merge --no-ff version/{X.Y}
python3 .claude/skills/grm-build-recipe/recipe.py test
```

If tests pass:

```bash
git branch -d version/{X.Y}
```

Update `docs/roadmap.md`: change `v{X.Y}` from `(planning in flight)` to
`(implementation complete — pending release)`.

**Telemetry (best-effort, v3.14 #82).** After `version/{X.Y}` → `dev`
completes, emit the per-run metadata artifact via
`python3 .claude/skills/grm-token-measure/run_metadata.py --emit ...`; full
invocation in `reference.md` §Telemetry artifact. Never gates the release. On
an abort (test failure, unresolved conflict) before this step, emit
`outcome=fail` via the sibling `telemetry_entry.py --emit --outcome fail` CLI
mode instead (same file, §Telemetry artifact) — the `telemetry-errors`
boundary rule (`docs/coding-standards.md` §Telemetry). Never gates; a failed
emit is swallowed.

**Branch + worktree cleanup is a post-release step, not this skill's job.** See
`grm-project-release` §Post-release cleanup and `docs/grimoire/integration-workflow.md`
§Dead-worktree cleanup.

---

## Write-capable workflow agent branches

A write-capable Workflow's structured `branches` output (per-branch
`mergeAfter` + `status`) replaces the §Before every merge run step 2
(release-agent-tracker) as the authoritative branch list — surface `failed`
entries first, then merge `completed` entries in `mergeAfter` topological
order via the same per-branch procedure above. Full detail (output schema,
triage, safety invariants) in `reference.md` §Write-capable workflow agent
branches (full procedure).

---

## Reference (load on demand)

- `Phase completion check` — see `reference.md`
- `Push to origin — not here` — see `reference.md`
- `Anti-patterns` — see `reference.md`
- `Before-promotion divergence gate (BMI-2)` — full mechanism, see `reference.md`
- `Baseline ratchet trend-line formats` — see `reference.md`
