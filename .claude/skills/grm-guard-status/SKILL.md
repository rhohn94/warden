---
name: grm-guard-status
description: One-shot, read-only printout of guard/paradigm/marker/branch state — work-paradigm, integration-allow marker presence, current branch and protected-ref status, and a per-guard-hook capability summary. Use before a commit/merge/push you're unsure will be allowed, or when onboarding into an unfamiliar worktree.
---

# guard-status

A cheap, deterministic answer to "what would currently allow/deny?" (issue
#429). Agents repeatedly `cat` the guard hook scripts mid-session to predict
whether an operation will be blocked — an investigation tax observed across
many transcripts, and it nudges agents toward editing what they just read.
This skill replaces that with one script call.

**Strictly read-only.** It parses each hook's `HOOK_CONTRACT` header (#441,
`docs/grimoire/design/hook-contract-design.md`) and other read-only state
(config, marker file, current branch); it never edits a hook file and never
re-derives or executes a hook's actual gating logic. The hook file itself
remains authoritative for exact enforcement behavior on a genuinely
ambiguous case — treat this as a cheap first read, not a substitute for it.

## Steps

1. **Run the script** from the project root:

   ```bash
   python3 .claude/skills/grm-guard-status/guard_status.py            # text (default)
   python3 .claude/skills/grm-guard-status/guard_status.py --json     # structured JSON
   python3 .claude/skills/grm-guard-status/guard_status.py --root <p> # another project root
   ```

   One call returns:
   - `work_paradigm` / `stealth_mode` — the active dials from
     `.claude/grimoire-config.json`.
   - `integration_marker_present` — whether this worktree carries
     `.claude/integration-allow.local` (the blessed-integration-worktree marker).
   - `current_branch` / `branch_is_protected` — the current branch and
     whether it matches the protected pattern (`dev`, `main`, `version/*`) —
     the same `PROTECTED_RE` `protected-branch-guard.sh` itself uses.
   - `hooks` — for each of the seven shipped guard hooks
     (`protected-branch-guard.sh`, `push-guard.sh`, `stealth-guard.sh`,
     `worktree-guard.sh`, `bundled-sync-guard.sh`, `release-plan-guard.sh`,
     `autonomy-allow.sh`): its `HOOK_CONTRACT` stamp version and the
     capability tokens it declares, each with a short human gloss.

2. **Read the branch-protection verdict** before a commit/merge/push. If
   `current_branch` is protected and no marker is present, a history-mutating
   op is going to be denied by `protected-branch-guard.sh` — branch in place
   instead of trying it (`grm-worktree-preflight`). If the marker IS present,
   the outcome still depends on the per-hook predicate shown (e.g.
   `release-boundary-guard` on `main`, `master-head-drift-block`).

3. **Read the per-hook capability list** for anything else you're unsure
   about (will a push be allowed, will stealth mode block this edit, is the
   release-plan doc still writable). If a capability you expect is *missing*
   from a hook's declared set, or a hook shows `NO CONTRACT HEADER` /
   is absent, don't hand-edit the hook to "fix" the mismatch and don't route
   around it — that is exactly the stale-capability failure mode #441 exists
   to catch mechanically. Report it; the repair path is re-syncing
   `.claude/hooks/` from upstream (`grm-sync-from-upstream`) or running
   `grm-install-doctor`'s `audit_hook_contracts` cross-check.

4. **If the tool itself is stale or missing a gloss**, that's a documentation
   gap, not a logic bug — `capabilities` with `"gloss": "(no gloss on file)"`
   just means `CAPABILITY_GLOSS` in `guard_status.py` hasn't been updated for
   a newly added token; the raw token name is still accurate and sourced
   directly from the hook's own `HOOK_CONTRACT` header.

## What it is not

- **Not a permission check.** It reports what the hooks *declare*; it does
  not evaluate a specific pending command against a hook's actual predicate
  (that's the hook's own job, at PreToolUse time). For a command you're
  genuinely unsure about, this narrows the search — it doesn't replace
  actually running it.
- **Not a config validator.** For schema/cross-rule validation of
  `grimoire-config.json`, use `grm-config-validate`. For the claimed-vs-
  installed capability cross-check (config claims a dial the hook doesn't
  back), use `grm-install-doctor`'s `audit_hook_contracts` — this skill
  surfaces the same `HOOK_CONTRACT` data for a human/agent to read at a
  glance, but does not itself gate or fail a build.

## Self-test

```bash
python3 .claude/skills/grm-guard-status/guard_status.py --self-test
```

Exercises config/marker/branch reads against a real (throwaway) git repo,
`HOOK_CONTRACT` parsing for all seven hooks (varying stamp versions, an
unglossed capability token's fallback, a header-less hook, and an entirely
absent hook file), protected-branch detection, and output determinism.
