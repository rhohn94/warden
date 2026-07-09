---
name: grm-release-planning
description: Generate a work-items report for the next planned release version. Reads design docs and release-planning history, assesses roadmap direction, surfaces carryovers from the in-flight release, and produces a structured report with token estimates and required design work. Use when the user asks to plan vX.Y, what goes in vX.Y, or any forward-looking release scoping question.
---

# Release planning

Produce a forward-looking work-items report for the next planned version
(`vX.Y`). The report is a planning input, not a commitment — iteration with
the user is expected before a `release-planning-v{X.Y}.md` is written.

> **Fan-out variant.** When you want broad parallel coverage and independently
> sized items, the integration master may run the `grm-release-planning` *workflow*
> (`Workflow({ name: 'release-planning' })`, see
> `docs/grimoire/integration-workflow.md` §Workflow-based orchestration) instead of
> doing the reads serially. It mechanises Steps 1–5 below across subagents and
> returns the same report. This skill stays authoritative for *what* the report
> must contain; the workflow is opt-in, billed, and read-only.

---

## Step 1 — Orient

Before reading anything, answer these three questions from available context
(files, git log):

1. **What is the current released version?** → `docs/version-history.md`
   (first heading, or equivalent changelog).
2. **What is the in-flight release?** → `ls docs/release-planning/release-planning-v*.md`;
   the highest version without a matching entry in `version-history.md`.
3. **What is the target version?** → one MINOR bump beyond the in-flight
   release. Confirm with the user if ambiguous.

---

## Step 2 — Read for context

Read these documents in order. Do not skip; carryovers live across all of them.

| Document | What to extract |
|---|---|
| `docs/roadmap.md` §`v{X.Y}` | Flagship theme, named sub-items, explicit non-goals |
| `docs/release-planning/release-planning-v{X.Y-1}.md` §4 "Out of Scope" | Items explicitly tagged `v{X.Y}+` or `later` |
| `docs/release-planning/release-planning-v{X.Y-1}.md` §5 "Follow-ups" | Pass-N follow-up bullets tagged for roll-forward |
| `docs/design/README.md` | Overall architecture orientation; cross-links to feature docs |
| Any feature design doc named in the roadmap §`v{X.Y}` entry | Existing spec depth; what's already designed vs. still open |

Also skim the last 2–3 versions to calibrate scope and velocity — use
`python3 .claude/skills/grm-status-broker/version_history.py --list` to pick them,
then `--release vX.Y` per version, rather than reading the whole
`docs/version-history.md` (>100 KB).

> **Roadmap narrative vs. issue tracker.** The `docs/roadmap.md` `## Backlog`
> section is the **narrative candidate pool** — future items the team has noted
> as potential scope. This read is unchanged. Separately, the configured issue
> tracker (roadmap `## Backlog` backend, or an external provider when
> `grm-issue-tracker` is configured in `grimoire-config.json`) holds discrete filed
> issues. Optionally, you may run
> `python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --state open --limit 30`
> to surface an open-issue count/summary as **additive context** — this does not
> replace the narrative read and does not affect what items are included in the
> work-items report.
>
> **Grimoire-Requirement tracker read (mandatory).** In addition, you MUST run:
> `python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --state open --labels Grimoire-Requirement`
> This returns all open issues tagged with the protected `Grimoire-Requirement`
> label. A zero result is valid; the command must still be run. These issues
> feed origin-D in Step 3 and are never optional context (`web-app-support-design.md` §6.1).

---

## Step 3 — Identify work items

Collect items from four sources and tag each with its origin:

**A. Flagship items** — from `roadmap.md §v{X.Y}`. These define the release
theme. Break the flagship into sub-items if the roadmap entry lists concrete
deliverables; otherwise propose a decomposition.

**B. Explicit carryovers** — items that the in-flight plan's §4 Out-of-Scope
section names with a `v{X.Y}+` or `later` qualifier. These are committed
rollovers; include all of them.

**C. Pass follow-ups** — bullets from the in-flight plan's §5 follow-up
sections. Include those tagged for the target version; flag the rest as
candidates for the user to decide.

**D. Framework-required tracker issues** — open issues returned by the
`Grimoire-Requirement` read in Step 2. These are never optional context: every
tagged issue appears in the report, regardless of whether it is already covered
by origins A–C. An issue that overlaps a flagship or carryover item should be
noted as already-covered but must still be listed. A zero result means no
framework-required issues are open; that is a valid outcome.

---

## Step 4 — Size each item in tokens

For each work item, estimate the total subagent token budget: input context
(reading files) + output (code, tests, doc updates). Use these reference bands:

| Band | Range | Typical work |
|---|---|---|
| XS | 5K–15K | Narrow flag, single file edit + test |
| S | 15K–40K | New sub-feature, small component, CLI flag with design doc update |
| M | 40K–80K | New module, complex wiring across 3–5 files |
| L | 80K–200K | Multi-file architecture change, new major surface |

Provide a point estimate (e.g. `~30K`) not a range. Add a one-line rationale
so the estimate is reviewable: name the files that dominate the read phase and
the output type that dominates the write phase.

---

## Step 5 — Identify design work required

For each work item, note whether a design doc:

- **Exists and is sufficient** — implementation can start.
- **Exists but needs extension** — name the section to add.
- **Missing — blocks implementation** — flag explicitly; the feature cannot
  start until the doc exists. Use the **`grm-design-doc-scaffold`** skill to
  create it.

Collect these into a table at the end of the report.

---

## Report structure

Output the report in this order. Vary the depth of each section to match how
much is actually known:

```
## v{X.Y} Work Items Report

### Theme
One paragraph: flagship name, the problem it solves, non-goals.

### 1. Flagship — {Name}
Table: # | Item | Tokens | Rationale
Subtotal line.

### 2. Carryovers from v{X.Y-1}
One sub-section per thematic group.
Table per group: # | Item | Tokens | Rationale

### 3. Work Items Summary
Master table: # | Area | Item | Est. Tokens
Total line.

### 4. Design Work Required
Table: Document | Status | What's needed

### 5. Observations for Iteration
3–6 bullets: scope risks, spike recommendations, items the user should
decide on before the plan is locked.
```

---

## After the report

The report is a planning input. Do not create `docs/release-planning/release-planning-v{X.Y}.md`
automatically — wait for the user to confirm scope. When scope is settled:

1. Use **`grm-design-doc-scaffold`** for any missing design docs flagged in §4.
2. Run the **`grm-release-agreement`** skill — it writes the planning doc, creates
   the `version/{X.Y}` staging branch, and locks the scope with
   `status: agreed`. Do not hand-create the planning doc here.

---

## Anti-patterns

* Generating a plan before reading §4 "Out of Scope" and §5 follow-ups of the
  in-flight release — carryovers are the most commonly missed items.
* Omitting the flagship non-goals — they exist precisely to prevent scope
  creep; list them explicitly.
* Giving every item the same token estimate. A range of 8K–150K across a
  single release is normal and expected; be honest about large items.
* Writing `docs/release-planning/release-planning-v{X.Y}.md` before the user confirms scope —
  the report is a conversation starter, not a final plan.
* Pulling items from `roadmap.md §v{X.Y+1}` or later — stay one version at a
  time.
