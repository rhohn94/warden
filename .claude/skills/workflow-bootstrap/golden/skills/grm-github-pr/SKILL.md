---
name: github-pr
description: Open, review, and merge GitHub pull requests at a release merge boundary when github-pr.enabled is true. The integration master / Project Manager pushes the head branch, opens a PR (idempotent, via github_pr.py), dispatches a Reviewer that posts findings onto the PR, then merges via gh pr merge — replacing the local --no-ff merge at that boundary. Opening/merging a PR is a push-class action: human-gated unless autonomous-push.enabled. Suppressed under Stealth Mode. Triggers on "open a PR", "create a pull request", "merge the PR", "review the PR", "raise a PR for this release", "github PR flow".
---

# github-pr — PR-based review & merge

When `github-pr.enabled` is `true` (and the repo is on GitHub), the integration
master / Project Manager performs a **boundary merge via a pull request** instead
of a local `git merge --no-ff`. Read the config live:
`github-pr.{enabled, boundary, merge-method, review.auto-dispatch, review.post-comments}`.

Absent the block, or `enabled: false` → today's local-merge-then-push flow,
unchanged. **Under Stealth Mode this whole flow is suppressed** (a PR + branch
push is a fingerprint) — fall back to the local in-place flow.

Design: `docs/design/github-pr-integration-design.md`.

## When a PR opens (the `boundary`)

| `boundary` | PR opened for |
|------------|---------------|
| `version-to-dev` (default) | `version/{X.Y}` → `dev` |
| `dev-to-main` | `dev` → `main` |
| `both` | both boundaries |

Under a **Project Manager** (v3.1), each lane's `version/{X.Y}/<lane>` →
`version/{X.Y}` integration may also open a **lane PR**, and the release boundary
follows `boundary`.

## The flow

1. **Push the head branch.** Opening a PR needs its head branch on origin. This
   is a **push-class action** — propose-and-wait (human-gated) unless
   `autonomous-push.enabled` is set. `push-guard.sh` permits the `version/*`
   head **only because** `github-pr.enabled` is true; the marker requirement and
   destructive-flag denial are unchanged.
2. **Open the PR (idempotent).**
   ```
   python3 .claude/skills/github-pr/github_pr.py open \
       --base dev --head version/{X.Y} --plan docs/release-planning-v{X.Y}.md
   ```
   Reuses an open PR for the same head→base; otherwise creates one with a
   title/body built from the release plan. Emits the PR number + URL as JSON. If
   it reports `degraded` (no `gh` / no GitHub remote), fall back to the local
   merge and log the downgrade.
3. **Dispatch the Reviewer (if `review.auto-dispatch`).** Spawn a **Reviewer** in
   **PR mode** on the PR number — it reads the diff (`github_pr.py diff --pr N`),
   runs `code-review`, and posts per `review.post-comments`
   (`off` / `comment` / `request-changes`). See the `reviewer` skill.
4. **Merge via the PR.** On a clean review (or human approval), and subject to the
   same push gate as step 1:
   ```
   python3 .claude/skills/github-pr/github_pr.py merge --pr N --method <merge-method>
   ```
   This is the boundary merge — **skip the local `--no-ff` merge** at this
   boundary. Check `github_pr.py status --pr N` first: if
   `reviewDecision == CHANGES_REQUESTED`, do **not** merge until resolved.

## Push / autonomy invariant

Opening and merging a PR are push-class actions. `github-pr` does **not** imply
autonomous push: by default the master proposes the push/PR-merge and waits;
only `autonomous-push.enabled` (the never-inferred opt-in) lets it proceed
unattended. The `push-guard.sh` rails (marker required, destructive flags denied,
audit log) are unchanged — the guard is widened **only** to allow the PR-head ref
to be pushed at all when `github-pr.enabled`.

## Anti-patterns

- Treating `github-pr` as permission to push autonomously (it is not — see above).
- Merging a PR with `reviewDecision == CHANGES_REQUESTED`.
- Doing both the local `--no-ff` merge **and** the PR merge at the same boundary
  (double-merge) — when a boundary uses a PR, the PR is the merge.
- Running the flow under Stealth Mode (suppressed — use the local in-place flow).
