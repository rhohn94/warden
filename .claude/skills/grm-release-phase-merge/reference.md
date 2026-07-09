# release-phase-merge — reference

Companion to `SKILL.md` (Supervised flavor). Contains the feature-manifest
authoring step, Reporter spawn template, full telemetry invocation, and the
tiered conflict classification for reference. The operational head (merge
protocol, ledger tick, anti-patterns) lives in `SKILL.md`. Design authority:
`docs/design/grimoire-release-server-design.md` (preflight engine).

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

---

## Tiered conflict resolution (v1.30, #62)

Before stopping on a merge conflict, classify it (authority:
`docs/grimoire/design/autonomy-hardening-design.md`):

- **Auto-resolvable** — additive/disjoint hunks, or a known-generated artifact
  (lockfiles, `docs/README.md` map, `.claude/cache/*` baselines). Resolve
  (prefer the union, or regenerate the artifact) — even in Supervised, these
  can be auto-resolved and surfaced in the diff summary before the confirmation
  gate.
- **Semantic / ambiguous** — overlapping edits to the same logic, or anything
  not clearly in an auto class. **Stop and surface to the human.**

Conservative by default: when in doubt, escalate.
