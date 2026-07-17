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
`python3 .claude/skills/grm-agent-status-broker/version_history.py --list` to pick them,
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

> **Component-registry reuse consult (mandatory).** Mirrors the
> `Grimoire-Requirement` tracker read above — the one wired loop that already
> works — but for reuse instead of framework-required scope. For every item
> collected from A–D: read the capability vocabulary
> (`docs/grimoire/design/component-taxonomy.md` §3) and identify which
> `provides`/`requires` terms plausibly describe what the item would build
> (e.g. "add session middleware" → `auth`). An item that maps to zero
> taxonomy terms needs no query. For every item that maps to one or more
> terms, run (batch every collected item's candidate tags into one call):
> `python3 .claude/skills/grm-release-planning/reuse_gate.py query <tag> [<tag> ...]`
> This command is **never optional** once an item has candidate tags — same
> "must still run" contract as the Grimoire-Requirement read, and the same
> "a zero/no-op result is a valid outcome" contract. It queries
> `.claude/component-registry.json` for `provides` overlap against those tags
> and returns a `"no-op": true` result when the registry is absent or has no
> cataloged components — this is the documented, **expected** outcome on a
> project (including this one) that hasn't populated a registry yet; proceed
> with the plan unmodified rather than blocking or erroring.
>
> Record the outcome against each item as a **"Reuse resolution:"** line —
> one of:
>   - `Reuse resolution: consumes <component-id> (<capability>)` — overlap
>     found; the plan will use the cataloged component instead of building new.
>   - `Reuse resolution: justified-new because <reason>` — overlap found, but
>     the plan deliberately builds new anyway. The reason is mandatory; an
>     item with overlap and no reason is a plan defect (checked by
>     `grm-release-agreement` Step 1).
>   - `Reuse resolution: no cataloged overlap (queried: <tags>)` — the item
>     had candidate tags but none matched a cataloged component.
>   - `Reuse resolution: no-op (component-registry.json absent/empty)` — the
>     gate degraded gracefully (this repo's own case); nothing to justify.
> Carry the chosen line into the item's entry when `grm-release-agreement`
> writes `docs/release-planning/release-planning-v{X.Y}.md` §2.{N} — see that
> skill's Step 2 template.

> **GUI-surface flag (#362).** Tag a GUI item `Surface: gui:web`/`gui:desktop`
> so `grm-release-phase` attaches the `gui-test` done-criterion at dispatch.
> See `runtime-verification-design.md` §GUI testing.

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

### Release-size standard

Two release classes calibrate total scope:

- **Standard release** — plan to a **~1M-token total budget**. The budget
  covers the whole release, not just the planned items: commit roughly
  600–700K of §3 point estimates and leave the remainder as headroom for
  review fixes, Pass-2 follow-ups, and integration overhead (historically
  30–50% on top of planned estimates).
- **Patch release** — a small release with a deliberately **narrow scope**: a
  handful of XS/S items (bugfixes, doc corrections, one contained feature),
  typically ≤150K planned tokens. A patch release skips nothing — same
  planning → agreement → merge → release pipeline — it is simply scoped small.

If a plan's item total pushes the release past the 1M budget, flag it in §5
Observations and propose a split rather than silently overcommitting.

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
One "Reuse resolution:" line per item beneath the table (Step 3's mandatory
component-registry consult).

### 2. Carryovers from v{X.Y-1}
One sub-section per thematic group.
Table per group: # | Item | Tokens | Rationale
One "Reuse resolution:" line per item beneath each group's table.

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
* Skipping the component-registry reuse consult, or treating a `"no-op":
  true` result as license to skip recording a "Reuse resolution:" line —
  every item gets one, even when the outcome is no-op.
* Finding a `provides` overlap and writing "justified-new" with no reason —
  the reason is the whole point; `grm-release-agreement` Step 1 treats it as
  a plan defect.
