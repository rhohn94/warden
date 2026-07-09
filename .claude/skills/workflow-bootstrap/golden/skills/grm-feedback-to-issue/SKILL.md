---
name: feedback-to-issue
description: Convert freeform feedback (a user/beta-tester comment, a noticed bug, a review note, an agent observation) into a well-formed normalized IssueDraft and file it via the issue-tracker abstraction. Sets audience so routing resolves automatically — no tracker name required. Triggers on "convert this feedback into an issue", "file this as an issue", "log this bug", "track this", "report this", "feedback to issue", "file this feedback".
---

# Feedback-to-issue (FI1)

Transforms **freeform feedback** into a **well-formed normalized Issue** and
files it through the issue-tracker abstraction in one create call. This is the
reusable engine; the Reporter agent (RP1) wraps it for dedicated filing sessions
— FI1 itself is invocation-context-agnostic.

Design authority: `docs/design/issue-tracker-design.md` §7 (FI1 contract),
§5.3 (create routing), §2 (IssueDraft shape).

> **MCP-first (v3.12).** When the `grimoire-issue-tracker` MCP server is active
> (`mcp.prefer-for-tracker`, default on), file via the `create_issue` tool and
> dup-check via `list_issues` instead of shelling out to `issue_tracker.py` —
> same engine, same routing. CLI fallback below when MCP is unavailable. See
> issue-tracker SKILL.md §0. Consumers that file through this skill (Reporter,
> qa-agent, coding-practices-audit, release-phase-merge) inherit the re-point.

---

## §0 — Standard ticket layout (mandatory)

Every issue filed through this skill **must** include all three sections below.
A missing section means the issue is not ready to file — fill all three before
calling the filing tool. Do not leave placeholders.

```markdown
## Overview
{One paragraph: problem statement, who is affected, severity signal.}

## Requirements
- {Must-have 1}
- {Must-have 2}

## Acceptance Criteria
- {AC 1 — verifiable}
- {AC 2 — verifiable}
```

**Section definitions:**

- **Overview** — one paragraph: problem statement + who is affected + severity
  signal (e.g. blocking / degraded / cosmetic).
- **Requirements** — bulleted must-haves; each item is a concrete thing the
  fix or feature must do.
- **Acceptance Criteria** — verifiable done conditions; each criterion must be
  independently checkable without ambiguity.

**Enforcement:** compose the full body (§2 body structure within the three
sections above) before invoking `create`. If any section cannot be filled from
available information, escalate to the Researcher role rather than filing a
stub.

---

## §1 — Triggers & input forms

**Trigger phrases:** "convert this feedback into an issue", "file this as an
issue", "log this bug", "track this", "report this feedback".

**Accepted input (any combination):**

| Form | Example |
|---|---|
| Freeform text | `"The onboarding step 3 crashes if no git repo exists"` |
| Structured dict | `{text: "...", audience: "external", labels: ["bug"]}` |
| Piped from skill | `spawn_task` Reporter invocation or mid-session flag |

When called with no argument, ask the user: "What feedback should I file as an
issue?" and wait for their reply before proceeding.

---

## §2 — Transform contract (§7.2)

Derive each IssueDraft field:

| Field | Rule |
|---|---|
| `title` | One imperative sentence ≤80 chars. Start with a verb (Crash, Fix, Add). No filler words. |
| `body` | 2–4 paragraph markdown (§2 body structure). ≤400 words. Synthesize — do not transcribe verbatim. |
| `labels` | `bug` (crash/error), `enhancement` (feature/improvement), `question` (unclear), `docs` (gap), `ux` (usability), `Grimoire-Requirement` (framework-mandated requirement — see §9). Multiple allowed. |
| `audience` | See §3. |
| `tracker` | Leave `null` — routing resolves via audience (§4). |

**Body structure** (omit sections that do not apply):

```markdown
**What:** <one sentence — what went wrong or what is missing>

**Steps to reproduce:** (bugs only)
1. …

**Expected:** <what should happen>
**Actual:** <what happened instead>

**Context / source:** <version, environment, reporter, or "internal observation">
```

**Near-duplicate check (token-efficient):** Before filing, run one bounded list
and check for keyword overlap with the proposed title (simple word match; no
embeddings). One call is the full read budget.

```bash
python3 .claude/skills/issue-tracker/issue_tracker.py list --state open --limit 30
```

On match: Supervised → ask user (file new vs update existing). Noir → file new
if titles differ by >3 words; otherwise skip and note the duplicate's number.

---

## §3 — Audience resolution (§7.3)

Apply in order (first match wins):

1. **Explicit input** — if the input provides `audience: "internal"` or
   `audience: "external"`, use it.
2. **Keyword inference** — if the feedback text or context contains any of:
   `user reported`, `beta tester`, `customer`, `external`, `public reporter`,
   `user says` → set `audience: "external"`.
3. **Default** — set `audience: "internal"`.

**Safety default:** `"internal"` is the safe direction. An internal issue filed
externally is a visibility leak; the reverse is minor noise. When in doubt, use
`"internal"` and let the integration master promote it if needed.

---

## §4 — Audience → tracker routing (§5.3)

Set `audience` on the draft and omit `--tracker`. The abstraction applies
create routing automatically:

1. **Explicit tracker** (skip — FI1 does not hard-code a tracker name).
2. **Audience match** — abstraction finds the first configured tracker whose
   `audience` equals the draft's audience.
3. **Default-for-filing** — falls through if no audience-matched tracker exists.

This keeps FI1 decoupled from the project's topology. A project that later adds
a second tracker or renames trackers requires no FI1 changes.

---

## §5 — Filing invocation (one call)

Pass `--audience`; omit `--tracker` (routing resolves via audience). Pass
`--labels` as space-separated values.

```bash
python3 .claude/skills/issue-tracker/issue_tracker.py create \
  --title  "Crash on first launch when no git repo exists" \
  --body   "**What:** Onboarding step 3 crashes outside a git repo.

**Steps to reproduce:**
1. Run onboarding in an empty dir (no .git).

**Expected:** Graceful error directing user to init git.
**Actual:** Unhandled exception printed to stderr.

**Context / source:** Beta tester report, v1.11." \
  --labels bug \
  --audience external
```

Capture stdout. The abstraction returns a JSON Issue object with the
provider-assigned `number` and `url`. Report both to the caller.

---

## §6 — Standalone vs Reporter-wrapped (§7.5)

FI1 is standalone-invocable by a human, the integration master mid-session, or
any skill via `spawn_task`. RP1 (Reporter agent) wraps FI1 in its own
narrow-context session; RP1 does **not** re-implement this logic — it invokes
FI1 and exits. The contract above is the shared API.

Reporter spawn prompt (§8.3 of design):

```
Reporter: file the following feedback via feedback-to-issue.
Audience: <internal|external>.
Feedback: <text>
```

---

## §7 — Token efficiency rules (§7.4)

- **One `create` call.** Derive the full draft before filing; never patch with `update()` after.
- **Title-only list for duplicate check.** Default `list` is body-on-demand; do not call `get()` on candidates.
- **No tracker reads beyond the duplicate check.** Trust the `create` return value.
- **Body bounded to ~400 words.** Synthesize; do not transcribe verbatim.
- **Smallest model tier.** Use Haiku/Eco for the synthesis step if the profile permits.

---

## §8 — Confirm

After a successful create:

```
Filed issue #<number>: "<title>"
Tracker: <tracker name> (<audience>)
URL: <url or "(roadmap — no URL)">
```

Near-duplicate skipped (Noir): `Near-duplicate detected: #<number> "<title>" — skipped.`

---

## §9 — Protected-label carve-out: `Grimoire-Requirement`

`Grimoire-Requirement` is a **protected label** (`docs/design/issue-label-taxonomy.md`
§Protected framework labels). When filing an issue tagged with it:

1. **Always `audience: "internal"`** — tagged requirements are engineering-track
   items and must never be routed to an external-facing tracker.
2. **Label is auto-ensured** — `IssueTracker.create()` calls `ensure_label`
   automatically before filing, so the label exists on GitHub even if this is
   the first tagged issue. No manual pre-creation step needed.
3. **Planning consequence** — tagged issues are **always-prioritized origin-D
   items** in `release-planning` (WEB-6); they may be scheduled across versions
   but must never be silently dropped. FI1 does not need to do anything extra —
   the label itself carries the contract.
4. **Do not invent this label from feedback inference** — `Grimoire-Requirement`
   is applied only when the *caller* explicitly requests it (e.g. the catalog
   filing flow in WEB-7, or an integration master explicitly seeding a
   framework requirement). Never infer it from the feedback text.

---

## Anti-patterns

- **Raw feedback as the body.** Always structure using §2 body template; raw text
  lacks the normalized shape consumers expect.
- **Inventing a tracker name.** Never pass `--tracker <name>` with a hard-coded
  string. Set `--audience` and let routing resolve — that decouples FI1 from
  topology changes.
- **Filing without audience.** Always resolve `audience` (§3) before `create`.
  Omitting `--audience` may misfile external issues to the internal tracker.
- **Multiple `create` calls for one item.** One piece of feedback = one issue.
  If it spans multiple bugs, file separate invocations with separate titles.
- **Unbounded tracker reads.** One `list --limit 30` is the full read budget;
  never call `get()` on every list result.
- **Re-implementing RP1 logic.** FI1 is the engine; RP1 is the wrapper. Invoke
  this skill rather than copying its steps into a Reporter prompt.
- **Inferring `Grimoire-Requirement` from feedback.** This label is
  framework-reserved; only apply it when explicitly instructed (§9 rule 4).
