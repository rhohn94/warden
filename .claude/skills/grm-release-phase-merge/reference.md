# release-phase-merge — reference

Companion to `SKILL.md` (shared across paradigms — `SKILL.md` itself is
paradigm-swapped by `grm-work-paradigm-switch`). Contains the feature-manifest
authoring step, Reporter spawn template, full telemetry invocation, and the
tiered conflict classification for reference. The operational head (merge
protocol, ledger tick, anti-patterns) lives in `SKILL.md`. Design rationale for
the preflight engine lives in the upstream Grimoire repository
(framework-internal — not shipped).

---

## Feature-manifest entries

For every capability shipped this release that a downstream project would need
to adopt or configure after syncing, add an entry to
`.claude/skills/grm-sync-from-upstream/feature-manifest.md` (fields: `feature-id`,
`introduced-in: vX.Y`, `summary`, `detect`, `adopt`, optional `migrate`). Same
discipline as the `version-history.md` entry.

A capability is manifest-worthy if it has an idempotent `adopt` step and did
not exist in prior releases. Pure internal refactors do not require an entry.
Commit the manifest update as part of the D2 close-out branch.

---

## Reporter spawn template

When the diff review (step 1) surfaces scope creep, incomplete work, or
out-of-scope items worth tracking:

- **Single item:** invoke `grm-feedback-to-issue` directly (the abstraction routes
  to the configured tracker automatically).
- **Multiple items:** spawn a Reporter session via `spawn_task`:

```
Reporter: file the following feedback items via feedback-to-issue, one issue per item.
Audience: internal.
Items:
1. <first follow-up item>
2. <second follow-up item>
```

The Reporter makes no git commits and is safe to run concurrently with the
merge loop. See `docs/grimoire/integration-workflow.md` §Filing issues with the Reporter
for paradigm-specific confirmation gates (Supervised requires explicit user
approval before each Reporter spawn).

---

## Telemetry artifact

Full `run_metadata.py` invocation for the per-run telemetry artifact emitted
after `version/{X.Y}` → `dev` (best-effort, v3.14 #82):

```bash
python3 .claude/skills/grm-token-measure/run_metadata.py --emit \
  --outcome pass --release {X.Y} --config .claude/grimoire-config.json \
  --items <total §5 rows> --items-passed <merged-green §5 rows> \
  --started-at <ISO-8601 start> [--wall-clock-secs N] [--transcript T] \
  || echo "run_metadata: emit skipped (telemetry is best-effort)"
```

`--outcome` is `pass` if all §5 rows merged green, `partial` if work was
deferred, `fail` if the merge was abandoned. `paradigm` / `profile` / `model`
auto-fill from `grimoire-config.json` via `--config`. Graceful by contract:
absent inputs degrade to null/zero, a missing transcript yields all-zero
tokens, and the `|| echo` swallows any non-zero exit.

**Abort path (`outcome=fail`, #345/#346).** If the loop halts before reaching
`version/{X.Y}` → `dev` (a test failure or an unresolved conflict), use the
sibling `telemetry_entry.py` CLI emit mode instead — it writes the same
`run_metadata` artifact plus a small context sibling (argv/note/exit code):

```bash
python3 .claude/skills/grm-token-measure/telemetry_entry.py --emit \
  --outcome fail --release {X.Y} --config .claude/grimoire-config.json \
  --exit-code 1 --note "<which branch/step aborted>" \
  || echo "telemetry_entry: emit skipped (telemetry is best-effort)"
```

This is `docs/coding-standards.md`'s `telemetry-errors` rule applied at this
release boundary — see that doc's §Telemetry for the boundary-vs-standalone
distinction (`telemetry_entry.py` is the same one-line opt-in a standalone
skill script would use).

---

## Quality gate detail — dependency-channel conformance (3a)

When a merging branch's diff touches `vendor.toml` / `vendor.lock` /
`vendor/`, run `python3
.claude/skills/grm-dependency-audit/dependency_channel_conformance.py --root .
--json` (the `vendor-check` verb's implementation). It reads the same live
`audit-gate` dial as the rest of the quality gate. Each finding is the
normalized shape `{check,dep,channel,severity,detail,locked_sha,observed_sha}`.

Under `warn` (this release's setting): file each finding via
`grm-feedback-to-issue` (audience `internal`, labels `security` +
`dependency-channel`, dedupe key `{channel}:{dep}:{check}`) and **proceed** —
it never blocks a merge/release this release. The network
"unpublished-release" check degrades gracefully (reported, never a hard
fail). A future release can flip this sub-step to **block** via the same dial
with no schema change.

Same trigger, a second pass — `sync_deps.py --verify` (#315, vendor provenance
integrity): run `python3 .claude/skills/grm-sync-deps/sync_deps.py --verify
--root . --json`. Fully offline — zero network. Findings normalized to
`{check,dep,severity,detail,locked,observed}`; three classes count as
violations — `LOCAL-FORK` (vendored bytes drifted from the `vendor.lock` pin),
`DEAD-VENDOR` (a declared dest or git submodule is empty/uninitialized),
`VERSION-CONTRADICTION` (an embedded version string disagrees with the pin) —
plus one WARN-only heuristic, `STUB-VENDOR-MANIFEST`, that never elevates the
exit code alone. Same `warn`-under-`audit-gate` posture as 3a: file via
`grm-feedback-to-issue` (labels `security` + `dependency-channel`) and proceed.

---

## Write-capable workflow agent branches (full procedure)

A write-capable Workflow's structured output matches the shape shown in
`.claude/workflows/write-capable-example.js`:

```
{
  variant:  string,            // which execution-strategy variant ran
  branches: [
    { branch: string, mergeAfter: string[], status: 'completed'|'failed', result: any },
    …
  ]
}
```

**Pre-merge triage:**
1. Surface any `status: 'failed'` entries first — report the full failed list
   to the user before starting the merge run. Do not silently skip them.
2. Treat the remaining `completed` entries as the merge queue; there is no
   `grm-release-agent-tracker` ledger for this source — the workflow output
   *is* the authoritative list.

**Topological merge order:** `mergeAfter` is a dependency DAG over branch
slugs, not a flat sequence:
- Branches with an empty `mergeAfter` are eligible first (no dependencies).
- A branch listing `mergeAfter: ['a', 'b']` merges only after both `a` and `b`
  have themselves been successfully merged into the staging ref.
- Compute a topological sort over the DAG before merging; do not merge in
  array order if it violates a dependency.

**Per-branch steps:** identical to the §Per-branch merge procedure above
(diff review, `git merge --no-ff`, tests, quality gate, §5 tick) — the only
difference from the isolated-worktree source is where the branch list and
merge order come from.

**Conflict-map gating:** on a merge conflict, apply the same tiered
classification (auto-resolvable vs. semantic/ambiguous) as any other branch.

**Post-merge:** once every entry in the DAG is merged (or a failed entry's
follow-up is filed), proceed to the phase-completion check exactly as with
isolated-worktree branches.

**Safety invariants:**

| Invariant | Why |
|---|---|
| Never merge a branch before its `mergeAfter` deps are merged | breaks the dependency the agent assumed was already integrated |
| Never silently drop a `failed` entry | the master must decide whether to re-dispatch or descope it |
| Push stays human-gated | write-capable workflows run entirely local; nothing here changes the push contract |

---

## Tiered conflict resolution (v1.30, #62)

Before stopping on a merge conflict, classify it (design rationale in the
upstream Grimoire repository, framework-internal):

- **Auto-resolvable** — additive/disjoint hunks, or a known-generated artifact
  (lockfiles, `docs/README.md` map, `.claude/cache/*` baselines). Resolve
  (prefer the union, or regenerate the artifact) — even in Supervised, these
  can be auto-resolved and surfaced in the diff summary before the confirmation
  gate.
- **Semantic / ambiguous** — overlapping edits to the same logic, or anything
  not clearly in an auto class. **Stop and surface to the human.**

Conservative by default: when in doubt, escalate.
