---
name: grm-issue-reconcile
description: Release-time issue reconciliation — after a release is tagged, sweep open issues referenced by the release's commits/plan/version-history/changelog, and close-with-comment (Noir) or flag-for-review (Supervised/Weiss) the ones satisfied. A MANDATORY post-tag gate — a left-open strong-evidence claim hard-fails (recoverable via override). Also has a --sweep mode. Triggers on "reconcile issues", "close shipped issues".
---

# Release-time issue reconciliation

> **Problem this closes:** autonomous roles file issues at scale but nothing
> closes them when their substance ships — the tracker lies about remaining
> work. See `docs/grimoire/design/issue-reconciliation-design.md` for the
> full design; this file is the operating guide.

## When this runs

`grm-project-release` invokes `issue_reconcile.py` as a **mandatory post-tag
gate** (#468) — after the tag exists, before the release report — mirroring
`publish_release.py`'s asserting `verify` stage. A non-zero exit means the
release isn't done yet; see Release gate below. It can also be run standalone
for a one-time historical back-sweep.

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

An open issue is a **candidate** only if its `#N` appears in one of four
places, newest release first:

- **Commits** — `git log <prev-tag>..<tag>`, full message bodies.
- **Plan §2** — the release-planning doc's `## 2. Major Features` section
  only (other sections, e.g. "Out of Scope", are deliberately excluded).
  Within §2, only a `#N` that appears in an item's own `### ITEM<id> —
  #NNN: <title>` heading counts as a real reference; any other `#N`-shaped
  token in §2 prose (a description, acceptance criteria, or a neighboring
  item's illustrative/negated aside) is weak, never strong (#521).
- **Version-history** — the released version's own `## vX.Y` section.
- **Changelog** (#468) — `docs/changelog.md`'s own `## vX.Y` section. This
  repo's own changelog deliberately omits ticket IDs by house style (see
  `docs/coding-standards.md` §Content & UI copy), so this source is usually
  empty here — it exists for the general case: #468's proposal names the
  changelog entry explicitly, and a project whose changelog does carry
  `Closes #N` claims is still covered.

No reference anywhere → no candidacy, full stop (never closes on textual
similarity alone).

### Evidence tiers

A candidate's `#N` reference is tiered per occurrence, not just present/absent:

- **STRONG** — closing-keyword adjacency: `fix #N` / `fixes #N` / `fixed #N` /
  `close #N` / `closes #N` / `closed #N` / `resolve(s|d) #N`, including the
  conventional-commit subject prefix `fix(#N):` / `feat(#N):` / `merge(#N):`
  and comma/`+`-separated trailer lists (`fixes #55, #56, #57`). A plan §2
  ref is strong only for the `#N` in an item's own heading line (that
  heading is the plan's canonical claim that this item resolves that issue —
  #521); a `#N` anywhere else in §2 prose is weak, even with closing-keyword
  wording in an item's own description/acceptance-criteria text. Strong
  evidence is close-eligible.
- **WEAK** — a bare `#N` mention with no closing-keyword adjacency (e.g. a
  release-notes summary line that just lists issue numbers), or any `#N` in
  plan §2 prose outside an item's own heading (its body text, or a
  neighboring item's illustrative/negated aside — "e.g.", "out of scope",
  "untouched", "beyond what ... touch" — #521). Flag-only, never auto-closed.
- **REVERT** — any ref inside a commit whose subject starts with `Revert` (or
  says `reverts #N` / `reverted #N`) is excluded from strong entirely and
  always flagged — closing an issue a revert just un-shipped would be wrong.

**`merge(#N):` scope note (added for #469):** the conventional-commit prefix
match is intentionally narrow — it only fires when the ref list itself sits
inside the parens, e.g. `merge(#100): fold X into dev`. It does **not** match
this repo's own `merge(vX.Y): ... #N ...` merge-commit convention (a version
tag in the parens, issue refs loose elsewhere in the subject) — recognizing
bare `#N` mentions anywhere in a merge-commit subject as strong evidence is a
broader judgment call with more false-positive risk (a batch merge commit can
legitimately just *mention* an issue without shipping its fix) and is left as
a follow-up rather than folded in here.

**Follow-up (not implemented here):** `chore(#N):` was proposed alongside
`merge(#N):` in #469 but is deliberately left out — it's only valid closing
evidence when the referenced issue is itself chore-scoped, which the regex
has no way to verify. Treat as a separate design decision if picked up later.

## Disposition

- **close** — referenced with closing-keyword adjacency by commits, and/or
  named in its own plan §2 item heading, and/or closing-keyword-adjacent in
  version-history/changelog prose (a shipped work item's own evidence
  trail). Under **Noir**, the script comments with the
  satisfying release + evidence, closes, then **re-reads the issue and fails
  loudly if the state did not persist** (the known github-backend
  masking-failure history, #130 — a silent no-op must never look like
  success).
- **flag** — referenced only in version-history prose (partial evidence), OR
  a close-eligible candidate under **Supervised/Weiss** (paradigm gate: only
  Noir writes; every other paradigm reports the same verdict as a review
  item and writes nothing). Read live from `.claude/grimoire-config.json`
  `work-paradigm.value` on every run — never cached.

## Release gate (#468)

`--tag` is a mandatory gate, not an advisory report. Exit code drives it:

- **0** — nothing blocking. Either every strong-evidence claim closed (Noir)
  or there were none to begin with.
- **1, write-failed** — a close was attempted but the post-write re-read
  showed it didn't persist (#130). Always hard-fails; **no override exists**
  — this is a tracker-write defect to investigate, not a judgment call.
- **1, close-eligible-not-closed** — a strong-evidence claim got redirected
  to `flagged` because the paradigm is Supervised/Weiss (flag-don't-write is
  unchanged). Hard-fails by default, but recoverable:
  ```bash
  python3 .claude/skills/grm-issue-reconcile/issue_reconcile.py --tag v{X.Y} \
    --reconcile-override-reason "manually verified #123 is unrelated to this release"
  ```
  The reason must be non-empty; it's echoed into the run output for the audit
  trail. `--dry-run` never gates (a preview writes nothing either way).

Why an override exists at all: fleet triage that motivated #468 also
observed this exact detector both miss genuinely-shipped issues (false
negative) and once auto-close a not-yet-resolved tracking issue (false
positive) in the same run. A blanket hard-stop on any mismatch would block
legitimate releases on the tool's own unreliability — so the gate fails loud
with the exact issue(s) named, and a human unblocks it with a stated reason
instead of a bare bypass flag.

Weak (bare-mention) and revert-only references were never close-eligible and
never gate the release — purely advisory, unchanged by #468.

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

Covers reference extraction (commits / plan §2 / version-history /
changelog), open-issue intersection, paradigm gating (Noir vs Supervised vs
Weiss), the post-write verification path, idempotency, the release-gate
classification (`gate_status`), truncation reporting, and the
`--reconcile-override-reason` CLI surface — all against a mocked tracker and
an injected git runner. No network, no real `gh` calls, ever.

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
- Treating a gate failure as blanket permission to add a bare
  `--skip`/`--force`-shaped bypass flag — the override requires a non-empty
  reason string and is named to avoid reading as a [Safety Bypass Flag] to an
  auto-mode classifier (#421); don't rename it to something bypass-shaped for
  convenience.
- Ignoring a "reconcile gate: FAILED" line and shipping the release report
  anyway — the point of #468 is that this step blocks, it doesn't merely
  advise.
