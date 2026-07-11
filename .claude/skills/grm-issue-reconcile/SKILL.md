---
name: grm-issue-reconcile
description: Release-time issue reconciliation — after a release is tagged, sweep open issues referenced by the release's commits, plan §2, and version-history entry, and close-with-comment (Noir) or flag-for-review (Supervised/Weiss) the ones the release satisfied. Includes a --sweep mode for the historical backlog. Triggers on "reconcile issues", "close shipped issues", "issues closed by this release", "back-sweep the backlog".
---

# Release-time issue reconciliation

> **Problem this closes:** autonomous roles file issues at scale but nothing
> closes them when their substance ships — the tracker lies about remaining
> work. See `docs/grimoire/design/issue-reconciliation-design.md` for the
> full design; this file is the operating guide.

## When this runs

`grm-project-release` invokes `issue_reconcile.py` as a post-tag judgment
step (after the tag exists, before the release report). It can also be run
standalone for a one-time historical back-sweep.

## Workflow

1. **Run it against the just-tagged release.**
   ```bash
   python3 .claude/skills/grm-issue-reconcile/issue_reconcile.py --tag v{X.Y}
   ```
   `--prev-tag` is auto-detected from shipped git tags (version-sorted) if
   omitted; pass it explicitly if the auto-detected predecessor is wrong.

2. **Read the report.** The first-class output line is:
   ```
   issues closed by this release: [#N, #M, ...]
   ```
   Fold this line into the release report. A `flagged for review: […]` line
   lists partial-evidence or non-autonomous-paradigm candidates — surface
   these to the human; never auto-close them.

3. **Back-sweep the historical backlog** (one-time or periodic hygiene):
   ```bash
   python3 .claude/skills/grm-issue-reconcile/issue_reconcile.py --sweep v3.70..v3.75
   ```
   Runs the same detection over every shipped tag in the range, oldest
   first, using each tag's own predecessor tag for the commit range.

4. **Preview before writing.** `--dry-run` prints the same verdict records
   without touching the tracker — this is also exactly what the
   Supervised/Weiss paradigm path produces (see Disposition below), so a
   dry-run under Noir is a faithful preview of what a live run would do.

## Candidate detection (evidence, not guesswork)

An open issue is a **candidate** only if its `#N` appears in one of three
places, newest release first:

- **Commits** — `git log <prev-tag>..<tag>`, full message bodies.
- **Plan §2** — the release-planning doc's `## 2. Major Features` section
  only (other sections, e.g. "Out of Scope", are deliberately excluded).
- **Version-history** — the released version's own `## vX.Y` section.

No reference anywhere → no candidacy, full stop (never closes on textual
similarity alone).

## Disposition

- **close** — referenced by commits and/or plan §2 (a shipped work item's
  own evidence trail). Under **Noir**, the script comments with the
  satisfying release + evidence, closes, then **re-reads the issue and fails
  loudly if the state did not persist** (the known github-backend
  masking-failure history, #130 — a silent no-op must never look like
  success).
- **flag** — referenced only in version-history prose (partial evidence), OR
  a close-eligible candidate under **Supervised/Weiss** (paradigm gate: only
  Noir writes; every other paradigm reports the same verdict as a review
  item and writes nothing). Read live from `.claude/grimoire-config.json`
  `work-paradigm.value` on every run — never cached.

## Idempotency

Every close writes a marker comment,
`<!-- grm-issue-reconcile: closed by vX.Y -->`. A re-run over the same
release finds the marker already present and reports the issue under
"already reconciled" instead of re-closing or re-commenting.

## All writes go through the issue-tracker abstraction

`issue_reconcile.py` imports
`.claude/skills/grm-issue-tracker/issue_tracker.py` directly (no raw `gh`
calls) and calls only its public operations (`list` / `get` / `comment` /
`close` / `flush`). Routing, caching, and the github write-batch/flush
semantics are entirely the tracker abstraction's concern — this script never
shells out to `gh` itself.

## Self-test

```bash
python3 .claude/skills/grm-issue-reconcile/issue_reconcile.py --self-test
```

Covers reference extraction (commits / plan §2 / version-history),
open-issue intersection, paradigm gating (Noir vs Supervised vs Weiss), the
post-write verification path, and idempotency — all against a mocked tracker
and an injected git runner. No network, no real `gh` calls, ever.

## Anti-patterns

- Closing on a title/body keyword match with no `#N` reference in a release
  artifact — evidence-based only.
- Skipping the post-write re-read on a close — the whole point is that a
  `gh` success exit code has historically not meant the state actually
  changed (#130).
- Running the write path outside Noir — Supervised/Weiss always flag, never
  close, regardless of evidence strength.
- Hand-closing issues instead of running the script — bypasses the
  idempotency marker, so the next reconciliation run can't tell they were
  already handled by this release.
