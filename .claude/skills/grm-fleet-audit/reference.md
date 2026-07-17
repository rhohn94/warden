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
recurring elsewhere in the fleet. `--check` (v3.91) now scans every tag since
the last conformant checkpoint, not just the newest — a repo whose newest tag
IS conformant but carries an older, sandwiched gap (the v3.87 case: a real
release with no GitHub Release, invisible while it was superseded before
anyone re-checked) surfaces here instead of staying silent.

**Tag format (audit finding, v3.91, non-blocking).** Alongside the
silent-publish check, note whether the repo's newest tag is two-part `vX.Y`
or three-part `vX.Y.Z` — the fleet-wide recommended format is `vX.Y.Z`
(`docs/grimoire/version-design.md` §2). This is a **warn-only, informational**
finding, never a defect: file it (or let the repo's own
`grm-doc-assurance tag-format` check surface it locally) as a nudge, never as
something requiring the repo to migrate its tag history.

```bash
python3 .claude/skills/grm-doc-assurance/doc_assurance.py tag-format --root <repo>
```

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

### Step 3a — Capability-overlap checklist item (mechanized, #412)

The reasoning above is agent-driven and covers *any* shared behavior. This
sub-step mechanizes one narrow, cheap-to-check slice of it: the
**component-taxonomy capabilities** (`auth`, `http-client`, `http-server`,
`persistence`, `telemetry`, `messaging`, `config`, `design-language` —
`docs/grimoire/design/component-taxonomy.md` §3). Run the heuristic grep-set
against the fleet repo set from Step 0:

```bash
python3 .claude/skills/grm-fleet-audit/capability_overlap.py scan \
  --repo ../familiar --repo ../mission-control --repo ../retro-game-player \
  --json
```

This applies `capability-overlap-patterns.json` (the maintained grep-set — see
its own `_comment` field for the data format and how to extend it with more
capabilities or patterns) across every named repo and reports, per capability,
which repos matched a hand-rolled-implementation signal. A capability
hand-rolled in **two or more** repos in scope is a **rule-of-two violation**
(the policy ITEM-2/#411 lands in the same release, v3.97) — the script's
`extraction-tickets` field in the JSON output is a pre-filled draft
(title/body/labels) ready to hand to `grm-issue-tracker`:

```bash
python3 .claude/skills/grm-issue-tracker/issue_tracker.py create \
  --title "<ticket title from the script output>" \
  --body "<ticket body from the script output>" \
  --labels audit extraction-candidate "capability-<name>"
```

**This is a heuristic, not a certainty signal.** A grep hit means "worth a
human look before filing" — verify the cited `file:line` evidence actually
implements the capability (not, say, a test fixture or a comment) before
filing the pre-filled ticket. Treat a hit the same evidence-discipline way
Step 6 below treats every other finding: cite it, don't just assert it.

**Engagement-scope note (v3.97):** this repo does not operate against a live
multi-repo fleet in this engagement — the mechanism above is proven via the
script's own `--self-test` (fixture directories standing in for sibling
repos), never exercised against the real fleet this release. A live
`scan --repo <path> ...` run against real sibling checkouts is a follow-up
action for whoever next runs this skill with fleet access.

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

### Step 4a — Catalog-conformance reconciliation checklist item (mechanized, #434)

Step 4 above sweeps this framework's OWN fleet-wide protocols
(`web-app-deployment-protocol.md`, `fleet-status-contract.md`). This sub-step
mechanizes the complementary check for the **required-feature catalog**
(`.claude/skills/grm-required-feature-catalog/required-feature-catalog.md`) —
the catalog's filing flow already tags every issue it files with a dedupe key
in the title (`[key: <key>]`, the catalog's own §Filing contract), but through
v3.97/#434 nothing ever RE-CHECKED whether a filed obligation was actually
satisfied, or reconciled a stale ticket once it was. Per repo in the fleet
set from Step 0:

1. Run the deterministic conformance plan against the repo's own checkout:

   ```bash
   python3 .claude/skills/grm-required-feature-catalog/catalog_conformance.py \
     plan --root ../<repo> --family <cli|gui|lib|service|web>
   ```

   (`--family` is the same profile the repo's own `grimoire-config.json` /
   quick-start scaffold declares — see required-feature-catalog.md §Family
   gate; this step does not invent a new family-detection mechanism.) Each
   result carries a `key` and an `action` — `ok`/`warn`/`fail` are real
   verdicts; `exempt`/`not-applicable`/`degraded` are never reconciled (a
   blocked-on-upstream entry, a family mismatch, or a probe unavailable in
   that repo's flavor).

2. Search that repo's tracker for its `Grimoire-Requirement`-labeled issues
   (the same query the catalog's own filing contract already specifies):

   ```bash
   python3 .claude/skills/grm-issue-tracker/issue_tracker.py list \
     --labels Grimoire-Requirement --state all
   ```

3. Feed both into the pure reconciliation engine:

   ```bash
   python3 .claude/skills/grm-fleet-audit/catalog_reconcile.py --self-test
   ```

   (`catalog_reconcile.reconcile(issues, conformance)` — imported, not shelled
   out to, from the auditing agent's own Python context; the CLI above only
   proves the logic via fixtures, it has no live-fleet entry point by design —
   see the module docstring.) It returns one action per `(key, issue)` pair:
   `close-as-verified` (issue open, check now passes — the obligation is
   satisfied), `reopen` (issue closed, check now fails — a regression),
   `already-verified` / `still-open` (no-op, working as intended), or
   `unfiled` (the check ran with a real verdict but no matching `[key: ...]`
   issue exists — a filing gap, reported not silently dropped).

4. Apply `close-as-verified` / `reopen` actions through the issue-tracker
   abstraction, same evidence discipline as every other Step 6/7 write in this
   skill — cite the conformance command's own output as the evidence:

   ```bash
   python3 .claude/skills/grm-issue-tracker/issue_tracker.py comment <N> \
     --body "Verified by catalog_conformance.py plan (<date>): <detail>"
   python3 .claude/skills/grm-issue-tracker/issue_tracker.py close <N>
   # or, for a reopen:
   python3 .claude/skills/grm-issue-tracker/issue_tracker.py comment <N> \
     --body "Regressed — catalog_conformance.py now reports: <detail>"
   python3 .claude/skills/grm-issue-tracker/issue_tracker.py update <N> --state open
   ```

**Engagement-scope note (v3.97, #434):** this repo does not operate against a
live multi-repo fleet in this engagement — same constraint as Step 3a/#412.
The reconciliation LOGIC is proven via `catalog_reconcile.py --self-test`
(a fixture standing in for "a filed ticket that got fixed" and one for "a
filed ticket that's still broken," plus a regression/reopen case and a mixed
multi-entry batch); running Steps 1-4 above against a real fleet repo and its
real tracker is a follow-up action for whoever next runs this skill with
fleet access.

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

