# Grm-fleet-audit — reference
Loaded on demand by `SKILL.md`.

## Step 1 — Release/publish conformance

Per repo, check that the latest tagged release actually has a conformant
published artifact — the same check `grm-project-release`'s
`publish_release.py --check` performs, run against each fleet repo if it ships
one:

```bash
python3 .claude/skills/grm-project-release/publish_release.py --check --root <repo>
```

If the repo predates that script, check by hand: does the newest git tag have
a corresponding GitHub Release with the expected asset trio and a matching
`SHA256SUMS`? A tag with no Release, or a Release with a mismatched checksum,
is a **silent-publish** finding — the exact defect class #286 closed here,
recurring elsewhere in the fleet.

## Step 2 — Shipped-vs-open tracker reconciliation

Per repo, run (or approximate, if the repo predates the script) the same
detection `grm-issue-reconcile` uses: does any **open** issue's `#N` appear in
the repo's own release commits, plan §2, or version-history entries for a
release that already shipped?

```bash
python3 .claude/skills/grm-issue-reconcile/issue_reconcile.py --sweep <oldest-tag>..<newest-tag> --dry-run --root <repo>
```

`--dry-run` only — this audit never writes into a fleet repo's own tracker
directly; it reports the mismatch here and lets that repo's own integration
master close it (or, if the operator has write access and asks, the audit may
close it itself with the same evidence trail `grm-issue-reconcile` requires:
commit / plan §2 / version-history reference, never a title-match guess). This
is the check that found familiar#97, mission-control#79, retro-game-player#42,
and design-language#1063 in the 2026-07-04 run (#292).

## Step 3 — Duplicate-implementation detection

Across the fleet, look for the same problem solved independently in two or
more repos — the standard-package pattern (token-bookkeeper, gatekeeper,
recordkeeper, meta-updater) exists precisely because this recurs. Signals:
- Two repos each hand-roll the same protocol-mandated behavior (e.g. #284
  found the self-update mandate in `web-app-deployment-protocol.md` §6
  implemented fully in only one of twelve apps, with no shared library).
- Two repos each built an equivalent tool/script independently (the v3.78
  `smoke-visual-gate` package generalized three independently-rebuilt
  screenshot pixel-diff harnesses found this way).
- A design doc in one repo describes a mechanism another repo already
  shipped under a different name.

Flag each hit as a **standard-package candidate**: name the shared behavior,
list every repo implementing it, and propose extraction (or, if extraction is
already tracked, cross-reference the existing ticket instead of re-filing).

## Step 4 — Mandate-compliance sweep

Every fleet-wide protocol this project publishes is a mandate every consuming
repo must satisfy. Sweep each repo against:
- `docs/web-app-deployment-protocol.md` (health probe, build-info stamp,
  self-update, fleet status contract) — for any repo that is a deployed web
  app.
- `docs/grimoire/design/fleet-status-contract.md` (the `/fleet/v1/status`
  endpoint shape) — same scope.
- Any other repo-wide `*-protocol.md` / `*-contract.md` doc this repo
  publishes as an authority for fleet consumers.

A repo missing a mandated surface, or implementing it with a shape that
disagrees with the contract, is a finding. Cite the exact contract section.

## Step 5 — Framework-version drift

Per repo, read its Grimoire framework version and compare against this
repo's current `framework-version` (`.claude/grimoire-config.json`):

```bash
python3 .claude/skills/grm-agent-status-broker/project_status.py --root <repo>
```

The JSON `framework-version` field is the zero-LLM-cost signal (see
`docs/grimoire/design/status-broker-design.md`). A repo more than one minor
version behind is a drift finding — note whether it predates
`grm-sync-from-upstream` (v1.x-era repos may need `grm-regenerate-grimoire`
or a fresh onboarding instead of a routine sync).

