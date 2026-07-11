---
name: grm-release-phase-merge
description: Merge completed subagent branches into version/{X.Y} autonomously — no per-merge confirmation. Runs tests after each merge, ticks §5, and drives the final version/{X.Y}→dev merge unsupervised. Stops only on conflict, test failure, or push trigger. Handles both isolated-worktree work-item branches and write-capable workflow agent branches. Push to origin remains human-gated. Use when the user says "merge agent X" or "phase N is done".
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

When `release-phase-model == Auto` (Noir only — that dial is a
framework-internal design; see the upstream Grimoire repository for that
rationale), `grm-release-phase` dispatches the
phase via a write-capable Workflow, so the returned branches arrive through the
**second** source above; merge them in `mergeAfter` order per §Write-capable
workflow agent branches. `Auto` adds no new merge machinery — it routes to that
already-documented path. The push gate is unchanged under both dial values
(see §Push to origin — not here).

---

## Before every merge run

> **Preferred interface — `merge_preflight` (grimoire-release MCP, v3.27).** When
> `mcp.enabled` and the server is registered (root `.mcp.json`), run
> **`merge_preflight`** with the staging ref (and optionally the candidate
> branches; it defaults to the `merge_queue` order) for a structured verdict
> `{head_ok, branches:[{branch,exists,ahead,ok}], blocked:[…]}` — the
> HEAD==staging check plus per-branch exists + commits-ahead assertions, computed
> deterministically. It is **read-only — it never merges**; act on the verdict.
> A `head_ok:false` is the HEAD-drift signal (do not merge — investigate per the
> stranded-branch recovery below). **CLI fallback** (no MCP / disabled): `python3
> .claude/skills/grm-release-agent-tracker/release_plan.py merge-preflight --staging
> version/{X.Y}`. The numbered steps below are the fallback procedure. Design
> rationale lives in the upstream Grimoire repository (framework-internal — not
> shipped).

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

> **Before-promotion divergence gate (BMI-2, v3.38, #126).** A promotion targets
> the published line (`main`) via the integration line, so before **both**
> promotion boundaries — `version/{X.Y}→dev` *and* `dev→main` — run the
> model-aware divergence check: it HALTs iff `main` carries tree content not
> reachable from the integration line, and (crucially) does **not** false-positive
> when `main` is ahead only by promotion merges. `merge_preflight` already runs it
> and folds a real fork into `head_ok:false`, surfacing the report under
> `divergence`. **CLI fallback:** `python3
> .claude/skills/grm-release-agent-tracker/release_plan.py divergence-check`
> (exit 2 + a readable report on real divergence; integration line read from
> `branch-model.integration-branch`, default `dev`). On a HALT, do **not** merge —
> reconcile by **merging `main` INTO** the integration line (merge-forward); never
> `reset --hard` across the fork (data loss). See
> `docs/grimoire/integration-workflow.md` §merge-forward recovery.

---

## Per-branch merge procedure (autonomous)

Repeat for each branch in the merge queue, in conflict-map order:

### 1. Review the diff

```bash
git diff version/{X.Y}...{branch}
```

Verify:
- Scope: within the files listed in §2.{N}.
- No edits to `docs/release-planning-v{X.Y}.md` §§1–4.
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
  conflict per design rationale that lives in the upstream Grimoire repository
  (framework-internal — not shipped). Auto-resolvable
  (additive/disjoint hunks, lockfiles, generated artifacts) → resolve, log to
  §5 follow-ups, continue. Semantic/ambiguous → stop and surface. Full
  classification table in `reference.md` §Tiered conflict resolution.

### 3. Run tests

```bash
{test-command}
```

If tests pass: continue.

If tests fail:
- Identify the root cause.
- If the fix is clear (introduced by the just-merged branch): apply it
  on a fix branch off `version/{X.Y}`, re-merge, re-test, continue.
- If the root cause is unclear: **stop and surface to the user**.

### 3.5 Quality gate (before ticking §5)

Read the `code-quality` block from `.claude/grimoire-config.json` **live**.
Absent block ⇒ defaults (`audit-gate: warn`, `auto-reviewer: noir`,
`coverage-threshold: null`, `typecheck: build`). Design rationale lives in the
upstream Grimoire repository (framework-internal — not shipped).

Run in order; first failing **blocking** check stops the merge:
1. **Type-check / build** (`typecheck: build` → type errors are build failures).
2. **Coverage** (`coverage-threshold: null` → skip by default).
3. **Audit gate** (`audit-gate: warn` → file via `grm-feedback-to-issue`, proceed).
   - **3a. Dependency-channel conformance** (sub-step; **warn-only this
     release**). When the branch's diff touches `vendor.toml` / `vendor.lock` /
     `vendor/`, run `python3
     .claude/skills/grm-dependency-audit/dependency_channel_conformance.py --root .
     --json` (the `vendor-check` verb's implementation). Reads the **same live
     `audit-gate` dial**. Each finding is the normalized shape
     `{check,dep,channel,severity,detail,locked_sha,observed_sha}`. Under
     `warn`: file each via `grm-feedback-to-issue` (audience `internal`, labels
     `security` + `dependency-channel`, dedupe key `{channel}:{dep}:{check}`)
     and **proceed** — it never blocks a merge/release this release. The
     network "unpublished-release" check degrades gracefully (reported, never a
     hard fail). A future release flips it to **block** via the same dial with
     no schema change. Design: `dependency-channel-design.md` §5.
   - **3a′. Vendor provenance integrity** (`sync_deps.py --verify`, #315; same
     trigger as 3a — vendor.toml/vendor.lock/vendor/ diffs). Fully offline,
     zero network. Findings normalized to
     `{check,dep,severity,detail,locked,observed}`; `LOCAL-FORK` (vendored
     bytes drifted from the `vendor.lock` pin), `DEAD-VENDOR` (empty/
     uninitialized dest), and `VERSION-CONTRADICTION` (embedded version
     disagrees with the pin) count as violations; `STUB-VENDOR-MANIFEST` is a
     WARN-only heuristic that never elevates the exit code alone. Same
     `warn`-under-`audit-gate` posture as 3a: file via `grm-feedback-to-issue`
     and proceed.
4. **Auto-Reviewer** (`auto-reviewer: noir` → spawn `grm-agent-reviewer`; blocking
   findings stop, non-blocking become §5 follow-ups).

**On any blocking stop:** `git reset --hard ORIG_HEAD` — undo the merge, leave
§5 row unticked, record reason in §5 follow-ups. Re-runnable once branch fixed.

### 3b. Doc-assurance --strict gate (v3.36+)

Run `python3 .claude/skills/grm-doc-assurance/doc_assurance.py --strict` as part
of the release closeout. Response policy based on findings:

- **block** (or `--strict` flag active): If findings from `check_hierarchy` or
  `check_relative_links` are present, fail the closeout. File each finding via
  `grm-feedback-to-issue` with label `doc-quality` and type `documentation` before
  blocking.
- **warn** (default): Run, print findings, proceed. Route warn-tier findings
  through `grm-feedback-to-issue` with `doc-quality` area label.
- **Stealth Mode:** Under `stealth-mode.value: "on"`, suppress the
  `grm-feedback-to-issue` auto-filing step (per `stealth-guard.sh` restrictions on
  commit-class actions). Run the check; do not auto-file.

### 4. Tick §5 ledger

```bash
git add docs/release-planning-v{X.Y}.md
git commit -m "docs(release-v{X.Y}): tick §5 — {branch} merged ({short-sha})"
```

Proceed to the next branch without pausing.

---

## Phase completion check

After the last branch in a phase is merged and tested:

1. Run `{build-command}` to confirm the integrated build is clean.
2. Proceed immediately to `grm-release-phase` for the next phase (or the final
   merge if all phases are ☑).

---

## Final merge — `version/{X.Y}` → `dev`

Pre-merge checklist (verify silently):

- [ ] `{test-command}` green on `version/{X.Y}`
- [ ] `{build-command}` clean
- [ ] All §5 rows ☑ Merged
- [ ] `version-history.md` entry written on `version/{X.Y}`

Execute autonomously:

```bash
git switch dev
git merge --no-ff version/{X.Y}
{test-command}
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
invocation in `reference.md` §Telemetry artifact. Never gates the release.

**Branch + worktree cleanup is a post-release step, not this skill's job.** See
`grm-project-release` §Post-release cleanup and `docs/integration-workflow.md`
§Dead-worktree cleanup.

---

## Push to origin — not here

**This skill pushes nothing.** After the `version/{X.Y}` → `dev` integration,
`dev` stays local. Pushing happens **once, at `grm-project-release`**, in a single
human-gated prompt that pushes `dev` + `main` + the version tag together (see
`docs/integration-workflow.md` §Pushing to origin). Propose no `dev` push from
this skill; the push gate is never lifted in Noir but it fires at release, not
here.

---

## Write-capable workflow agent branches

When a write-capable Workflow completes, its structured `branches` output
(each with `mergeAfter` list) replaces the §Before every merge run step 2
(release-agent-tracker). Merge in `mergeAfter` topological order.

**Full procedure** (pre-merge triage, topological sort algorithm, per-branch
steps, conflict-map gating, post-merge, and safety invariants table) is in
`reference.md` §Write-capable workflow agent branches.

---

## Anti-patterns

- Pausing for per-merge confirmation (Noir is autonomous — merge unless in
  a stop condition).
- Guessing at ambiguous merge conflicts — stop and surface.
- Pushing without human confirmation — push is always human-gated.
- Leaving `dev` broken — debug before switching branches.
- Silently skipping `failed` branches from a workflow output — always surface
  failures before starting the merge sequence.
- Merging a branch before its `mergeAfter` dependencies are merged — always
  respect the topological order from the conflict map.
- Using `grm-release-agent-tracker` for write-capable workflow branches — the
  workflow's structured output is the authoritative branch list for that source.
