---
name: grm-project-release
description: Promote dev to main and tag a new version. Use when the user wants to cut a release, tag a version, "release dev to main", or any release/versioning workflow. Required reading before running any release commands.
---

# Project release procedure

Promotes accumulated `dev` work to `main` and tags a version. Full branching
and versioning conventions live in `docs/grimoire/version-design.md` — read it
before acting.

The release splits into **judgment steps** (below — decisions only an
agent/human can make) and the **mechanical ceremony**, which is one command:

```bash
python3 .claude/skills/grm-build-recipe/recipe.py release   # ≡ just release ≡ scripts/release.sh
```

The `release` recipe target runs the entire mechanical pipeline: guards (on
`main`, clean tree, tag unused, changelog entry present) → open the
`.claude/release-in-progress.local` promotion window (trap-guarded — removed on
ANY exit, success or failure, so a stale marker can never leave `main`
boundary-exempt) → tests → build distributables → annotated tag → best-effort
`run_metadata` telemetry → version-history notes slice → **asserting publish**
(`publish_release.py`: every built asset must land on the GitHub Release and
its sha256 must match `SHA256SUMS`, hard-fail otherwise) → channel signal. A
mid-ceremony abort also emits `outcome=fail` via the sibling
`telemetry_entry.py --emit` CLI mode from the same promotion-window trap
(#345/#346 — `telemetry-errors` applied at this release boundary; see
`docs/coding-standards.md` §Telemetry). Reference implementation:
`scripts/release.sh` (`--dry-run` previews every
mutating action).

## Judgment steps (do these BEFORE invoking the ceremony)

1. **Write the `version-history.md` entry on `dev`.** One heading per release
   (`## vX.Y — Title`) + 3–8 bullets aimed at end users, not developers. The
   ceremony derives the release version from this newest heading and refuses to
   run without the entry. Also write the matching `changelog.md` entry — the
   front-facing counterpart with no ticket IDs, task names, or prompt/session
   references (`docs/coding-standards.md` §Content & UI copy); `version-history.md`
   is the internal record and is exempt from that rule. Commit both on `dev`.
2. **Write feature-manifest rows for each new flagship capability.** For every
   capability a downstream project must adopt or configure after syncing, add an
   entry to `.claude/skills/grm-sync-from-upstream/feature-manifest.md`
   (fields: `feature-id`, `introduced-in: vX.Y`, `summary`, `detect`, `adopt`,
   optional `migrate`). A capability is manifest-worthy if it has an idempotent
   `adopt` step and did not exist in prior releases; pure internal refactors do
   not need one. When in doubt, add the entry — false positives are filtered by
   `detect`. Commit on `dev` alongside the version-history entry.
3. **Pick the version: MINOR vs MAJOR.** MINOR for ordinary releases; MAJOR
   only for breaking changes. The chosen `vX.Y` is what the version-history
   heading (step 1) declares — the ceremony reads it from there.
4. **Pick the channel.**
   - **stable** — the normal `v{MAJOR}.{MINOR}` release on `main`. Default.
   - **beta** — a `--prerelease` published off the `version/{X.Y}` staging
     branch; assets carry a `-beta` suffix and `release.json` records
     `channel: beta`. Invoke with `--channel beta`.
5. **Merge `dev` → `main`, then check out `main`.** Releases only run from
   `main` — the ceremony's guard enforces it.

## Run the ceremony

```bash
python3 .claude/skills/grm-build-recipe/recipe.py release
# preview first, if desired:  just release --dry-run
# beta channel:               just release --channel beta
```

If the ceremony fails mid-run, stop and read the error — the trap has already
removed the release-in-progress marker, but partial state (a dangling tag,
built `dist/`) may need cleanup before retrying. Do not retry blindly.

The publish step degrades **loudly**, never silently: if `gh` is unavailable
the tag ships WITHOUT a published Release and downstream
`UPSTREAM_TRANSPORT=release` consumers fall back to git clone until the assets
are attached (the built `dist/*` is reproducible from the tag). The
skipped-publish gate (`publish_release.py --check`) fails while the newest tag
has no published Release — opt out only for a genuinely notes-only tag via the
`<!-- release: notes-only -->` marker in its version-history section. Design:
`docs/grimoire/design/release-pipeline-design.md`.

## Push to origin (post-release — NOT part of the ceremony)

The ceremony **never pushes** (and there is deliberately no `just push` recipe:
`push-guard.sh` recognizes only a literal `git push`, so a wrapper would bypass
it). A completed release is the **single** trigger moment for the integration
master to push. Behavior forks on `autonomous-push.enabled` — full decision
table in `docs/grimoire/design/autonomous-push-prompt-suppression-design.md`
§Two push modes:

1. Inspect what's ahead of origin:
   ```bash
   git fetch origin
   git log --oneline origin/dev..dev
   git log --oneline origin/main..main
   ```
2. **Gated (default — `autonomous-push.enabled` false, or any non-Noir
   paradigm).** Never just announce the push and move on to cleanup — actively
   stop and ask. Issue an `AskUserQuestion` with options `Push now` / `Hold`,
   putting the exact push plan in the question body: the refs, the remote, and
   the literal command, e.g.:
   ```
   Push plan:
     refs:    dev, main, v{X.Y}
     remote:  origin
     command: git push origin dev main --follow-tags
   Push now, or hold?
   ```
   Run the `git push` only on `Push now`. On `Hold`, stop here and report the
   release as tagged-but-unpushed — no passive "next is the push" phrasing.
3. **Ungated (Noir + `autonomous-push.enabled: true`).** Run the push
   immediately, no question asked — `push-guard.sh`'s `should_auto_allow`
   predicate suppresses the permission prompt for this exact command:
   ```bash
   git push origin dev main --follow-tags
   ```

`push-guard.sh` permits `main`, `dev`, and version tags by default (see
`.claude/push-allowlist`); pushing requires the integration marker. Destructive
flags are denied in both modes.

## Reconcile issues (post-tag judgment step)

After the tag exists, before writing the release report, sweep open issues
this release actually shipped:

```bash
python3 .claude/skills/grm-issue-reconcile/issue_reconcile.py --tag v{X.Y}
```

Detects candidates from this release's commits, the plan doc's §2, and the
version-history entry; under Noir it closes-with-comment (verifying the write
persisted); under Supervised/Weiss it flags for human review and writes
nothing. Fold its `issues closed by this release: […]` line into the release
report. See `.claude/skills/grm-issue-reconcile/SKILL.md`.

## Post-release cleanup (final ordered step)

After the tag and push, the **marker-blessed integration master** (only actor
permitted to touch sibling worktrees) runs the branch + worktree cleanup per
`docs/grimoire/integration-workflow.md` §Dead-worktree cleanup → §Post-release
cleanup step — read and follow it exactly. In brief, for each work-item
branch/worktree of the shipped release: verify merged + clean, preserve or
explicitly report any uncommitted work, `git worktree remove` (never a silent
`--force`), `git worktree prune`, then merge-safe `git branch -d`. Report the
tally.

## Reminders

- After release, new work-item worktrees still root on `dev`, never `main`
  (`grm-worktree-preflight`).
- The per-run telemetry artifact is emitted by the ceremony automatically
  (write-only, best-effort — it never gates or blocks a release).

## Anti-patterns

- Shipping an adoptable capability without a feature-manifest entry — syncing
  projects silently miss it and the capability lands inert.
- Hand-running the mechanical steps (marker, tag, build, `gh release create`)
  instead of the `release` recipe target — the ceremony exists so the marker is
  trap-guarded and the publish is asserted; ad-hoc runs re-open both gaps.
- Folding the push into the ceremony or adding a `just push` recipe — push
  stays a separate, guarded step.
