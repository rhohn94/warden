---
name: grm-ledger-tick
description: Update the §5 branch ledger in docs/release-planning/release-planning-v{X.Y}.md — mark landed branches, record follow-ups, roll deferred work to the next version. Triggers on "tick the ledger", "mark X as landed", "update release planning", "Pass 1/Pass 2", "follow-ups".
---

# Release-planning ledger tick

> **Model/effort:** this is a schema-constrained, mechanical edit (flip `☐`→`☑`,
> append a SHA). Pin it to **Haiku / low** — it needs no Opus judgement and is
> often dispatched ~10× per release; inheriting session Opus is pure waste.

The active `docs/release-planning/release-planning-v{X.Y}.md` holds the canonical pass/branch
ledger for the in-flight release. The §5 table tracks branches landed vs.
outstanding, and a follow-ups section captures work that slipped to a later
version.

> **Preferred interface — the `grimoire-release` MCP server (v3.27).** When
> `mcp.enabled` and the server is registered (root `.mcp.json`), prefer its
> native tools over hand-editing the markdown: **`get_ledger`** to read §5 as
> JSON and **`tick_rows`** to flip `☐`→`☑` atomically + idempotently (it never
> touches `n/a` cells). The server is **file-write-only** — it edits the ledger;
> **you still commit** (Step 6 below). **CLI fallback** (no MCP / disabled):
> `python3 .claude/skills/grm-release-agent-tracker/release_plan.py get-ledger` and
> `… tick --branch B --column merged --value true` — identical engine, identical
> behaviour. Design: `docs/design/grimoire-release-server-design.md`.

## Workflow

1. **Locate the active plan.** `ls docs/release-planning/release-planning-v*.md` — pick the
   highest unreleased version. If you are unsure which is active, find the most
   recently *released* version with
   `python3 .claude/skills/grm-status-broker/version_history.py --latest` (or
   `--list`) rather than reading `docs/version-history.md` whole; the active
   plan is the next one up.

2. **Find §5.** Search the file for `## 5.` or `### Pass`. The ledger is
   typically a table or bullet list of branches, each with a status
   (`☐` outstanding / `☑` landed) and a short scope line.

3. **Tick landed branches.** For each branch the user names (or for each
   branch reachable from `dev` since the last tick), flip `☐` → `☑` and
   append a one-line note: scope of what landed + the merge commit short SHA.
   Example: `☑ auth — JWT login + refresh token support (a3f12bc)`.

4. **Record follow-ups.** If a branch landed with deferred work, add a bullet
   under the file's follow-ups / rollover section naming the gap (file paths,
   brief description). Keep these terse; they will seed the next version's
   plan.

5. **Roll deferred work into the next version.** When the user calls the
   release closed (or when starting the next version's plan), create / append
   to `docs/release-planning/release-planning-v{X.Y+1}.md` and move the follow-up bullets
   there. Leave the originals in place so the landed-version doc is
   self-contained as a historical record.

6. **Commit — once per sweep, not per branch.** When ticking multiple branches
   from a single merge sweep (the common case under `grm-release-phase-merge`), flip
   all their rows in one edit and make **one** atomic commit:
   `docs(release-v{X.Y}): tick §5 ledger for Pass-N — <one sentence>`. Docs only.
   This collapses N same-file edit/commit cycles (and their cache invalidation)
   into one with no loss of correctness — every landed branch still gets its
   ☑ + SHA. **Safety constraint:** only include a branch once it has actually
   merged and passed tests (verify per the source-of-truth check below).

## Source-of-truth conflicts

If §5 disagrees with what is actually merged into `dev`:

```bash
git log --oneline --merges dev | head -40
```

Trust the git history. Update the doc to match, and note the discrepancy in
the commit message so the drift is visible.

## Anti-patterns

* Editing §5 on a work-item branch — release-planning edits go on `dev` (or the
  staging branch `version/{X.Y}`).
* Deleting follow-up bullets after rolling them to the next plan. Keep them in
  the source doc as a historical record.
* Marking a branch landed before it actually merges. Verify with
  `git log dev --oneline | grep <branch-name>` first.
