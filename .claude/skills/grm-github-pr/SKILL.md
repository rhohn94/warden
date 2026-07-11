---
name: grm-github-pr
description: Open, review, and merge GitHub pull requests at a release merge boundary when github-pr.enabled is true. The integration master / Project Manager pushes the head branch, opens a PR (idempotent), dispatches a Reviewer that posts findings onto the PR, then merges via gh pr merge — replacing the local --no-ff merge. Opening/merging a PR is push-class: human-gated unless autonomous-push.enabled. Use when opening, reviewing, or merging a release PR.
---

# github-pr — PR-based review & merge

When `github-pr.enabled` is `true` (and the repo is on GitHub), the integration
master / Project Manager performs a **boundary merge via a pull request** instead
of a local `git merge --no-ff`. Read the config live:
`github-pr.{enabled, boundary, merge-method, review.auto-dispatch, review.post-comments}`.

Absent the block, or `enabled: false` → today's local-merge-then-push flow,
unchanged. **Under Stealth Mode this whole flow is suppressed** (a PR + branch
push is a fingerprint) — fall back to the local in-place flow.

Design rationale lives in the upstream Grimoire repository (framework-internal
— not shipped).

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
   is a **push-class action**: gated by default — actively prompt via
   `AskUserQuestion` (`Push now` / `Hold`) with the exact push plan, never a
   passive announcement — or immediate with no question under
   `autonomous-push.enabled`. `push-guard.sh` permits the `version/*` head
   **only because** `github-pr.enabled` is true; the marker requirement and
   destructive-flag denial are unchanged.
2. **Open the PR (idempotent).**
   ```
   python3 .claude/skills/grm-github-pr/github_pr.py open \
       --base dev --head version/{X.Y} --plan docs/release-planning/release-planning-v{X.Y}.md
   ```
   Reuses an open PR for the same head→base; otherwise creates one with a
   title/body built from the release plan. Emits the PR number + URL as JSON. If
   it reports `degraded` (no `gh` / no GitHub remote), fall back to the local
   merge and log the downgrade.
3. **Dispatch the Reviewer (if `review.auto-dispatch`).** Spawn a **Reviewer** in
   **PR mode** on the PR number — it reads the diff (`github_pr.py diff --pr N`),
   runs `code-review`, and posts per `review.post-comments`
   (`off` / `comment` / `request-changes`). See the `grm-agent-reviewer` skill.
4. **Merge via the PR.** On a clean review (or human approval), and subject to the
   same push gate as step 1:
   ```
   python3 .claude/skills/grm-github-pr/github_pr.py merge --pr N --method <merge-method>
   ```
   This is the boundary merge — **skip the local `--no-ff` merge** at this
   boundary. Check `github_pr.py status --pr N` first: if
   `reviewDecision == CHANGES_REQUESTED`, do **not** merge until resolved.

## Push / autonomy invariant

Opening and merging a PR are push-class actions and follow the same two-mode
contract as `grm-project-release` §push: gated by default (active
`AskUserQuestion` prompt, `Push now` / `Hold`, with the exact push/merge plan
in the body) or fully ungated under Noir + `autonomous-push.enabled: true`
(push and `gh pr merge` proceed immediately, no question asked). `grm-github-pr`
does **not** independently imply autonomous push — the opt-in is the same
never-inferred `autonomous-push.enabled` flag used everywhere else in the
pipeline. The `push-guard.sh` rails (marker required, destructive flags denied,
audit log) are unchanged — the guard is widened **only** to allow the PR-head ref
to be pushed at all when `github-pr.enabled`.

## Anti-patterns

- Treating `grm-github-pr` as an independent grant of autonomous push (it
  follows the same `autonomous-push.enabled` gate as every other push-class op
  — see above).
- Passively announcing the PR push/merge instead of an active
  `AskUserQuestion` prompt when gated.
- Merging a PR with `reviewDecision == CHANGES_REQUESTED`.
- Doing both the local `--no-ff` merge **and** the PR merge at the same boundary
  (double-merge) — when a boundary uses a PR, the PR is the merge.
- Running the flow under Stealth Mode (suppressed — use the local in-place flow).
