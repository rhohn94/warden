# Issue-tracker — reference
Loaded on demand by `SKILL.md`.

## §6 — CLI usage (consumers call the helper script)

```bash
# List open issues (all trackers)
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list

# List issues for a specific tracker
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --tracker internal

# List external issues only
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --audience external

# List all states
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --state all

# Get a single issue (always includes body)
python3 .claude/skills/grm-issue-tracker/issue_tracker.py get <id>

# Create an issue (routing applies)
python3 .claude/skills/grm-issue-tracker/issue_tracker.py create \
  --title "Bug: onboarding crashes" \
  --body "Steps to reproduce..." \
  --labels bug \
  --audience external

# Create to an explicit tracker
python3 .claude/skills/grm-issue-tracker/issue_tracker.py create \
  --title "Internal note" --body "..." --tracker internal

# Update an issue
python3 .claude/skills/grm-issue-tracker/issue_tracker.py update <id> \
  --title "New title" --body "Updated body"

# Close an issue
python3 .claude/skills/grm-issue-tracker/issue_tracker.py close <id>

# Add/remove labels
python3 .claude/skills/grm-issue-tracker/issue_tracker.py label <id> \
  --add bug,ui --remove wontfix

# Search issues
python3 .claude/skills/grm-issue-tracker/issue_tracker.py search "onboarding crash"

# Flush pending write batch
python3 .claude/skills/grm-issue-tracker/issue_tracker.py flush

# Ensure a label exists (github: create if absent; roadmap: no-op)
python3 .claude/skills/grm-issue-tracker/issue_tracker.py ensure-label "Grimoire-Requirement"

# Ensure a label on a specific tracker
python3 .claude/skills/grm-issue-tracker/issue_tracker.py ensure-label "bug" --tracker internal

# Output as JSON (add --json to any read command)
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --json
```

All commands read `.claude/grimoire-config.json` for tracker config, synthesizing
the roadmap default when the `grm-issue-tracker` block is absent. The script exits
non-zero on any error and prints a structured `{code, message, tracker}` error to
stderr.

---

## Dispatch sizing from triage labels

When the integration master dispatches a work item, read the triage labels
applied by the Triager to drive model and effort selection:

- **`size:*`** — maps to token band; use the band table in `grm-release-planning`
  skill output to set the dispatch token budget and effort tier.
- **`complexity:*`** — maps to model tier:
  `complexity:simple` → haiku/sonnet; `complexity:moderate` → sonnet;
  `complexity:complex` → sonnet/opus; `complexity:research` → opus.
- **`component:*`** — identifies which system areas the issue touches. Use
  these tags in the conflict map when planning parallel dispatch: two work
  items sharing a `component:*` label are likely to touch overlapping file
  sets and should be serialized or explicitly de-conflicted before dispatch.

Full label definitions and allowed values: `docs/grimoire/design/issue-label-taxonomy.md`.

---

## Creating and managing Epics

Epics group 3 or more related issues that share a common goal. They use the
same nine-operation interface as plain issues — no new backend calls are needed.

### When to create an Epic

Create an Epic when **3 or more issues share a common goal** that benefits from
a single umbrella entry in the tracker. Below that threshold, shared labels are
sufficient. An Epic provides planning clarity at dispatch: the integration master
can see the full scope of a goal in one place and schedule child issues
accordingly.

### How to create an Epic

```python
# Via the IssueTracker abstraction (Python)
epic = tracker.create(
    title="[EPIC] Unify auth system",
    body="## Overview\nGoal: ...\n## Requirements\n...\n## Acceptance Criteria\n...",
    issue_type="epic",
)

# Via CLI
python3 .claude/skills/grm-issue-tracker/issue_tracker.py create \
  --title "Unify auth system" \
  --body "..." \
  --issue-type epic
```

The `epic` label is auto-applied. For the roadmap backend the title is stored
with a `[EPIC]` prefix in `docs/roadmap.md` to make Epics visually distinct.

### How to link a child issue

```python
# Via the IssueTracker abstraction (Python)
child = tracker.create(
    title="Migrate OAuth flow",
    body="...",
    parent_epic_id=epic.id,   # links this issue to the Epic
)

# Via CLI
python3 .claude/skills/grm-issue-tracker/issue_tracker.py create \
  --title "Migrate OAuth flow" \
  --body "..." \
  --parent-epic-id "<epic_id>"
```

### How to list Epics

```python
# Via the IssueTracker abstraction (Python)
epics = tracker.list(issue_type="epic")

# Via CLI
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --issue-type epic

# List only plain issues (exclude Epics)
python3 .claude/skills/grm-issue-tracker/issue_tracker.py list --issue-type issue
```

### One-level nesting rule

**Epics cannot be children of other Epics.** Passing both `issue_type="epic"`
and a non-null `parent_epic_id` to `create()` raises a `TrackerError` with code
`"validation_error"`. Child issues (plain `issue_type`) may have a
`parent_epic_id`; Epics themselves must have `parent_epic_id=None`.

### Milestone label on Epics

Apply the same milestone label to the Epic as to its child issues. This ensures
the Epic is visible in milestone-scoped planning reads. Set the milestone label
at Epic creation time or immediately after linking the first child.

---

## Anti-patterns

- **Calling `gh issue list` without `--json` + `--jq`** — raw output costs 29–44%
  more tokens and is harder to parse. Never pass raw `gh` output to the agent.
- **Including `body` in list queries** — body-on-demand is the rule (6× cheaper
  per issue). Use `get()` for bodies.
- **Bypassing the abstraction to read `roadmap.md` directly** — skills must
  route all issue reads/writes through the abstraction, not grep roadmap.md.
  The only exception is the `roadmap` backend implementation itself.
- **Skipping server-side filters** — always pass `--state`, `--label`, `--search`
  to `gh` before post-filtering; never fetch a full list and filter in Python.
- **Using `--limit` > 30** — R1 bounds the default at 30; callers may lower it.
  Setting a higher limit defeats the token budget guarantee.
- **Invalidating cache on write accumulation** — invalidate after batch flush,
  not per queued write. Premature invalidation destroys the snapshot benefit for
  read-heavy sessions that queue a few writes.
- **Bumping schema-version** — the `grm-issue-tracker` block is pure data added at
  schema-version 3. No version bump needed.
- **Relying on `null` body meaning "no body exists"** — `body: null` means
  "body not fetched yet". Call `get()` to fetch it.
