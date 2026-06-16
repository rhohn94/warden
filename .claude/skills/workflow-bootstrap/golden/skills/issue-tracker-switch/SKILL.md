---
name: issue-tracker-switch
description: Set or update the issue-tracker block in .claude/grimoire-config.json. Sub-commands — set (replace entire block with a single tracker), add (append a tracker), remove (remove by name), list (read-only table). Validates provider ∈ {roadmap, github, grimoire} and that repo is non-null for github. Idempotent — exits early if already in the requested state. Pure-data write — no file-swap. Preserves all other config fields; schema-version stays at 3. Use when onboarding Step 6 activates a non-roadmap tracker (§3.4), when the user wants to switch providers, or to add/remove named trackers. Triggers on "switch issue tracker", "set github issues", "add tracker", "remove tracker", "list trackers", "use GitHub for issues".
---

# Issue-tracker switch

Sets or updates the `issue-tracker` block in `.claude/grimoire-config.json`.
Validates inputs, writes the minimal change, confirms. Idempotent and safe to
re-run. Preserves all other config fields (`schema-version`, `work-paradigm`,
`workflow-variant`, `model-effort-profile`). Schema-version stays at 3.

Design authority: `docs/design/issue-tracker-design.md §10`.

> Like `model-effort-profile-switch` and `workflow-variant-switch`, this skill
> performs **no file-swap**. The `issue-tracker` block is pure data the
> abstraction reads live at every call. Writing the config block IS the
> activation. (Contrast `work-paradigm-switch`, which installs content sets.)

> **Absent `issue-tracker` block = roadmap default.** A project that never
> opts in sees zero behavioural change — the abstraction synthesizes a single
> `roadmap` tracker from the absent block (§5.2 of `issue-tracker-design.md`).
> `set roadmap` and leaving the block absent are functionally equivalent; the
> preferred idiom is omission (no noise in the config).

---

## §1 — Sub-commands

The skill exposes four sub-commands. The optional
`issue_tracker_switch.py` helper (sibling of this file) implements them and
may be called by Claude Code directly via `python3 .claude/skills/issue-tracker-switch/issue_tracker_switch.py <sub-command> [args]`.

### §1.1 — `set <provider> [repo] [--name <name>] [--audience <audience>] [--labels <l1,l2>]`

Replace the **entire** `issue-tracker` block with a single tracker. This is
the common onboarding path (§3.4 of the `onboarding` skill) and the
"switch to GitHub" user command.

| Argument | Required | Notes |
|---|---|---|
| `provider` | yes | One of `roadmap`, `github`, `grimoire` (case-insensitive). |
| `repo` | github: yes, others: no | `owner/repo` format. Must be null for `roadmap`. |
| `--name` | no | Tracker name, default `"default"`. |
| `--audience` | no | `"internal"` (default) or `"external"`. |
| `--labels` | no | Comma-separated labels auto-applied to every filed issue. |

Example: `set github acme/issues` produces:

```json
"issue-tracker": {
  "trackers": [
    { "name": "default", "provider": "github", "repo": "acme/issues",
      "audience": "internal", "labels": [] }
  ],
  "default-for-filing": "default"
}
```

### §1.2 — `add <provider> [repo] --name <name> --audience <internal|external> [--labels <l1,l2>] [--default]`

Append a new tracker to the existing list without touching other entries.
`--default` promotes this tracker to `default-for-filing`.

- Errors if `name` already exists in `trackers` with **different** fields (use
  `remove` then `add` to replace).
- If `name` already exists with **identical** fields → exit early (idempotent).

If there is no existing `issue-tracker` block, the skill synthesizes the
roadmap default and appends the new tracker to it.

### §1.3 — `remove <name>`

Remove a tracker by name. Two safety guards:

1. **Cannot remove the default-for-filing tracker** — promote another tracker
   to `default-for-filing` first.
2. **Cannot remove the last tracker** — use `set` to replace it instead.

### §1.4 — `list`

Print the current `issue-tracker` config as a human-readable table. No
writes. If the block is absent from config, displays the synthesized roadmap
default with an "(absent — roadmap default synthesized at runtime)" note.

---

## §2 — Validation

Run these checks before writing any config. Fail-closed: do not write an
invalid value.

1. `provider` must be one of `{roadmap, github, grimoire}` (case-insensitive).
   Unknown → abort:
   > "Error: '<input>' is not a known provider. Valid providers: roadmap, github, grimoire."

2. `repo` must be non-null and match `owner/repo` format (at least one `/`, no
   spaces) when `provider = "github"`. Invalid → abort:
   > "Error: provider 'github' requires a non-null repo in 'owner/repo' format."

3. `repo` must be null (or absent) when `provider = "roadmap"`. Specifying a
   repo for roadmap → abort:
   > "Error: provider 'roadmap' does not use a repo; pass no repo or leave it blank."

4. `audience` must be `"internal"` or `"external"`. Invalid → abort.

5. `name` must be non-empty and contain no spaces (kebab-case recommended).

6. `.claude/grimoire-config.json` must exist. If missing → abort:
   > "Error: config not found at `.claude/grimoire-config.json`. Run
   > `workflow-bootstrap --restore` to restore framework files."

---

## §3 — Idempotency

- **`set`** with the same provider + repo + audience + name as the current
  single tracker → exit early:
  > "Issue tracker is already configured as requested. No changes made."
- **`add`** with a name that already exists and identical fields → exit early:
  > "Tracker '<name>' already exists with identical fields. No changes made."
- **`add`** with a name that already exists but different fields → error (not
  an idempotent case — intent is ambiguous; use `remove` + `add`).

---

## §4 — Apply (config write contract)

Read the current `.claude/grimoire-config.json`. Apply the **minimal change**
to only the `issue-tracker` key. Leave **all other fields unchanged**:

- `schema-version` stays at `3` (no bump — same graduation precedent as
  `model-effort-profile` in v1.10 and `workflow-variant` in v1.11).
- `work-paradigm`, `workflow-variant`, `model-effort-profile` are untouched.
- Only the `issue-tracker` key is written (or removed, for a future
  `set roadmap` that could clean the block).

Write the updated config back as valid JSON.

**Forward-compat note:** an absent `issue-tracker` block is treated identically
to a single `roadmap` tracker by the abstraction's §5.2 fallback. The skill
never needs to write an explicit `roadmap` block — omission is the preferred
state for roadmap-default projects.

---

## §5 — Confirm

After a successful write, print a one-line confirmation. Examples:

- `set`: "Issue tracker set to provider='github', repo='acme/issues', name='default', audience='internal'."
- `add`: "Tracker 'public' added (provider='github', repo='acme/public-issues', audience='external')."
- `remove`: "Tracker 'public' removed."
- `list`: (table only, no extra confirmation)

---

## §6 — Helper script

`issue_tracker_switch.py` (sibling of this SKILL.md) implements all four
sub-commands as a thin CLI over the config write contract. It does **not**
import or modify `issue_tracker.py` (the abstraction/backend library — that
is I1/I4's file).

**Usage via Claude Code:**

```bash
python3 .claude/skills/issue-tracker-switch/issue_tracker_switch.py \
    set github acme/issues
```

The `--config PATH` flag accepts an explicit config path for testing against a
scratch copy; omit it in production (auto-detects from cwd up).

**Validation:** the script was verified with `ast.parse` + a smoke run that:
- Sets a github tracker, verifies the config block is written.
- Confirms idempotency (set same values again → "already configured").
- Adds a second tracker, confirms both appear in `list`.
- Confirms adding the same tracker again is idempotent.
- Removes the non-default tracker, confirms one remains.
- Confirms removing the last tracker is blocked.
- Confirms roadmap + repo → error.
- Confirms github without repo → error.

---

## Error conditions summary

| Condition | Behaviour |
|---|---|
| Config file missing | Abort; print restore instruction |
| Unknown provider | Abort; do not write config; list valid providers |
| `github` without `repo` | Abort; do not write config |
| `roadmap` with a `repo` | Abort; do not write config |
| Invalid audience | Abort; do not write config |
| Invalid name (spaces or empty) | Abort; do not write config |
| `add` with duplicate name + identical fields | Early exit; "already exists with identical fields" |
| `add` with duplicate name + different fields | Error; use `remove` then `add` |
| `set` with identical config | Early exit; "already configured as requested" |
| `remove` the default-for-filing | Error; promote another first |
| `remove` the last tracker | Error; use `set` instead |

---

## Seams (this skill is pure-data; it does NOT implement the abstraction)

- **I1** (`issue_tracker.py`) reads `load_config()` live — this skill only
  *writes* the `issue-tracker` block. The two files are intentionally decoupled.
- **I3** (`onboarding` skill §3.4) calls this skill after writing the config
  (§3) when a non-roadmap tracker was chosen at Step 6.
- **Schema-version 3** is the current schema; the `issue-tracker` block is
  additive at this version (no bump needed).

---

## Anti-patterns

- Calling this skill for the roadmap default — if the user selected `roadmap`,
  the `issue-tracker` block should be **omitted** from config entirely; the
  abstraction's §5.2 fallback handles the default. Do not write an explicit
  `roadmap` block.
- Bumping `schema-version` when writing the block — it stays at 3 (mirrors
  the `model-effort-profile` / `workflow-variant` graduation precedent).
- Modifying `issue_tracker.py` — that is the abstraction/backend library
  maintained by I1/I4; this skill's write path is contained in
  `issue_tracker_switch.py` only.
- Implementing abstraction or backend logic here — this skill is a pure-data
  config writer; the abstraction lives in `issue_tracker.py`.
- Writing `provider: "github"` without a `repo` — validation is fail-closed;
  never persist an incomplete GitHub config.
- Touching other config fields (`work-paradigm`, `workflow-variant`,
  `model-effort-profile`, `schema-version`) — only the `issue-tracker` key
  is in scope.
- Attempting a file-swap — there is none; the issue-tracker block is pure data
  the abstraction reads live (contrast `work-paradigm-switch`).
