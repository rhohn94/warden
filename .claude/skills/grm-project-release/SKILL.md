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
its sha256 must match `SHA256SUMS`, hard-fail otherwise; v3.90 adds a `verify`
stage running the repo's optional `[assert] verify` commands from
`publish.toml` BEFORE anything publishes — the signed-if-configured gate, so an
unsigned artifact fails the run instead of shipping) → channel signal. A
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
5. **Promote `dev` → `main` without requiring `main` to be checked out in your
   worktree.** Releases only run from `main` — the ceremony's guard enforces
   it — but in a multi-worktree topology `main` is commonly checked out
   somewhere else (e.g. the project's primary/root directory), and `git switch
   main` fails outright from any other worktree
   (`fatal: 'main' is already used by worktree at <path>`). **Never** work
   around that by `cd`-ing into the worktree that owns `main` and running
   `git reset --hard` there to "sync" it, and never re-run the ceremony's
   guarded steps (tests, build, push) from that other worktree — that
   worktree's staleness after the ref moves is harmless and not your concern;
   forcing it into sync costs unnecessary destructive-op confirmations for no
   benefit. Instead, build the merge with plumbing and move the ref directly,
   with zero checkout of `main` anywhere:

   ```bash
   # 1. Compute the merged tree — no working directory touched.
   TREE=$(git merge-tree --write-tree main dev)
   # 2. Build the merge commit object (two parents: main, dev).
   NEW_MAIN=$(git commit-tree "$TREE" -p main -p dev \
     -m "merge(vX.Y): promote vX.Y ... dev→main")
   # 3. Move the main ref via compare-and-swap — refuses if main moved
   #    underneath you between steps 1 and 3, same safety as a checked-out
   #    fast-forward merge.
   git update-ref refs/heads/main "$NEW_MAIN" "$(git rev-parse main)"
   ```

   This works from **any** worktree, including your own if it holds neither
   `dev` nor `main`. If your own worktree already has `main` checked out (the
   ordinary single-worktree case), the plain `git switch main && git merge
   --no-ff dev` remains fine — reach for the plumbing sequence specifically
   when `git switch main` would fail because a different worktree owns it.
   Tag the result the same checkout-free way: `git tag -a vX.Y "$NEW_MAIN" -m
   "..."`. Then push directly from your own worktree — never from the
   worktree that happens to have `main` checked out — per §Push to origin
   below: `git push` and the standard pre-push hook pattern (`cd
   "$(git rev-parse --show-toplevel)"`) both operate on ref values and the
   *invoking* worktree, never on what any worktree has checked out, so pushing
   from wherever you already are is correct and sufficient.

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

## Reconcile issues (post-tag MANDATORY gate — #468)

After the tag exists, before writing the release report, this step is
**required, not optional** — mirroring `publish_release.py`'s asserting
verify stage (a non-zero exit means the release is not done yet, the same way
a failed `[assert] verify` command blocks publish):

```bash
python3 .claude/skills/grm-issue-reconcile/issue_reconcile.py --tag v{X.Y}
```

Detects candidates from this release's commits, the plan doc's §2, the
version-history entry, and the changelog entry; under Noir it closes-with-
comment (verifying the write persisted); under Supervised/Weiss it flags for
human review and writes nothing (that flag-don't-write contract is
unchanged). Fold its `issues closed by this release: […]` line into the
release report. See `.claude/skills/grm-issue-reconcile/SKILL.md`.

**A non-zero exit blocks the release from being reported done.** Two
distinct failure modes:

- A close was attempted but did not verify as persisted (the #130
  masking-failure pattern) — always hard-fails, **no override**. This is a
  tracker-write defect; investigate before re-running.
- A strong-evidence ("Closes #N"-shaped) claim was flagged instead of closed
  because the paradigm is Supervised/Weiss — hard-fails by default, but
  recoverable: re-run with
  `--reconcile-override-reason "<why this is safe to ship anyway>"` (a
  non-empty justification is required; it is echoed into the run output for
  the audit trail). This asymmetry is deliberate — fleet triage has observed
  both false negatives (missed closures) and one false positive (a wrongly
  auto-closed tracking issue) from this same detector, so the gate never
  unconditionally hard-stops on its own say-so; it fails loud with the exact
  issue(s) named and lets a human override with a stated reason instead.

Weak (bare-mention) and revert-only references were never close-eligible and
never gate the release — purely advisory, same as before #468.

## Refresh component registry (post-tag, #458)

A disjoint step from **Reconcile issues** above and **Post-release cleanup**
below — keeps `.claude/component-registry.json` from drifting out of sync
with its sources. One mechanical script call, zero LLM judgment. Full
sequence: `reference.md` §Refreshing the component registry.

## Uncataloged-must-not-grow gate (post-registry-refresh, #459)

Runs immediately after the refresh above, same attach point, disjoint
concern: the refresh keeps the registry fresh against its sources, this
mechanically checks that freshly-refreshed registry's `uncataloged` count
against the previous release's committed registry and flags growth. WARN
by default (report-only, mirrors `sig-mismatch`'s WARN/`--strict` pattern
from #433) — a mechanical signal, not a blocker, that the write-time
`component.json` done-criteria (see CLAUDE.md §Task execution and the
paradigm task-execution templates) isn't holding. Full sequence:
`reference.md` §Uncataloged-must-not-grow gate.

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

## Reference (load on demand)

- `Refreshing the component registry` — see `reference.md`
- `Uncataloged-must-not-grow gate` — see `reference.md`
- `Anti-patterns` — see `reference.md`
