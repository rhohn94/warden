---
name: grm-workflow-snapshot
description: Deprecated (v3.49). The golden image is now generated on demand by grm-workflow-bootstrap's generate_golden.py, so there is no committed golden tree to re-baseline. This skill is retained only so historical references resolve; it performs no action. Use generate_golden.py instead.
---

# Workflow-snapshot — deprecated (v3.49)

**This skill is retired.** It used to re-capture the project's live workflow
skills and hooks back into a committed `golden/` tree so a future restore would
reproduce the current state. That tree no longer exists.

As of v3.49 the **golden image is generated, not stored**:
`generate_golden.py` (next to `grm-workflow-bootstrap`) derives the pristine
baseline from the flavor/install on demand and writes it under the gitignored
`.grimoire-golden/` cache. There is nothing to hand-snapshot — the baseline is
always re-derivable from the current files.

## What to do instead

- **Make an edited skill survive a future restore** — no action needed. Restore
  resolves the golden image from the live/flavor files, so your current edits are
  already the baseline once they ship in the flavor.
- **Freeze an offline restore baseline for an install** —
  `python3 .claude/skills/grm-workflow-bootstrap/generate_golden.py --freeze .`
  (run automatically at bootstrap; see `grm-workflow-bootstrap` Step 0).
- **Inspect what the golden image would contain** —
  `python3 .claude/skills/grm-workflow-bootstrap/generate_golden.py --flavor claude-code --list`

Design: the golden-image generation model (v3.49) — `generate_golden.py` and
`grm-workflow-bootstrap`.
