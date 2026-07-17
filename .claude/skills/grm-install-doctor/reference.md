# Grm-install-doctor — reference
Loaded on demand by `SKILL.md`.

## Step 1 — Audit (always; read-only)

Run the mechanical audit. The `audit` subcommand is the default, so a bare
invocation runs it:

```bash
python3 .claude/skills/grm-install-doctor/install_doctor.py                 # bare = audit (Markdown)
python3 .claude/skills/grm-install-doctor/install_doctor.py audit           # explicit, same result
python3 .claude/skills/grm-install-doctor/install_doctor.py audit --json    # machine-readable
python3 .claude/skills/grm-install-doctor/install_doctor.py audit --no-network  # skip reachability probe
python3 .claude/skills/grm-install-doctor/install_doctor.py --self-test     # offline self-tests
```

The script performs five audits and emits the health-report artifact (see
*Output format*). It exits `0` when healthy, `1` when any check is degraded,
`2` on a usage/internal error.

### 1a — Framework files (vs `grm-workflow-bootstrap` golden)

The helper classifies every golden-managed file (the same `golden/` tree
`grm-workflow-bootstrap` restores from) as:

- **OK** — present and matches golden, or a known project-customised file
  (`CLAUDE.md`, `settings.json`, `.scaffold-upstream.conf`).
- **MISSING** — no live file. Restorable.
- **DRIFTED** — present but differs from golden (and not an expected-custom
  file). Needs human/agent review — never a silent overwrite.

This mirrors the `grm-workflow-bootstrap` MISSING/PRISTINE/CUSTOMISED/DRIFTED
taxonomy, collapsing the two no-action states (PRISTINE, CUSTOMISED) into OK.

**Drift suppression (false-positive avoidance).** A byte mismatch against golden
is not always drift. Three classes of *legitimate* divergence are classified out
of DRIFTED so a healthy install reports zero false positives (none is a problem;
none is a `repair` target):

- **SEED-DIVERGED** — project-owned files seeded from a golden stub then grown by
  the project (`docs/version-history.md`, `vendor.toml`). Divergence is the
  intended steady state.
- **PARADIGM** — the four work-paradigm-swapped skills
  (`grm-project-manager`, `grm-integration-master`, `grm-release-phase`,
  `grm-release-phase-merge`) when the live file matches the **active** paradigm
  variant under `.claude/paradigms/<slug>/` while golden holds the generic
  default. install-doctor reads `work-paradigm.value` to make this call.
- **NEWER-THAN-GOLDEN** — files a recent `grm-sync-from-upstream` advanced past
  the last golden freeze (compared by mtime against the golden archive). The live
  file is *ahead*, so a repair would revert a correct sync. Re-freeze the
  baseline (`generate_golden.py --freeze .`); do **not** `repair`.

**Freezing mid-migration is safe (#313).** A project can be synced past a
migration-shipping release (namespacing, clean-room docs) but not yet have run
the migration itself, so the live tree briefly carries both the old form and
the new form (e.g. `skills/architecture-audit/` beside
`skills/grm-architecture-audit/`). `generate_golden.py`'s `GoldenGenerator`
detects that overlap and excludes the stale old-form duplicate from the frozen
golden, so freezing at that moment — then running the migration — does not
turn the migration's own archive+remove step into false MISSING findings. A
bare-name skill with no `grm-` sibling yet (a genuinely unmigrated pre-v3.42
install) is unaffected and still audits normally.

**Missing golden archive is a WARN, not a FAIL.** Immediately after adopting the
generated-golden-image feature (which deletes the legacy committed `golden/`
tree) there may be no frozen archive yet. The helper reports the
`golden-baseline` check as **WARN** with guidance to freeze one, and continues
the upstream + base audits rather than aborting the whole run. To resolve the
WARN non-interactively, freeze the baseline once (Step 3 §0):

```bash
python3 .claude/skills/grm-install-doctor/install_doctor.py repair --freeze-baseline
# back-compat equivalent:
python3 .claude/skills/grm-install-doctor/install_doctor.py --repair
```

This writes a versioned `golden-v{X.Y}.tar.gz` into the gitignored
`.grimoire-golden/` cache (delegating to `generate_golden.py --freeze .`), then
re-audits against it so the framework-file audit is no longer skipped. It is the
standard fix for the documented upgrade gap.

### 1b — Upstream connection (`grm-sync-from-upstream` inputs)

The helper validates the inputs `grm-sync-from-upstream` consumes:

- `.scaffold-upstream.conf` present and parseable; `UPSTREAM_REPO` non-empty
  and shaped like a URL / scp-path / existing local path.
- `UPSTREAM_REPO` **reachable** via a non-mutating `git ls-remote` probe
  (skipped under `--no-network`).
- `.scaffold-base/` present and non-empty (the 3-way merge base; absence means
  the next sync degrades to REVIEW-everything).
- `.claude/settings.json` carries the scoped maintenance-script
  **`permissions.allow`** allowlist so the sync scripts run unattended
  without the auto-mode classifier re-prompting. Absence is a **degraded**
  finding (not broken — syncs still work, they just re-prompt), repaired in
  Step 3 §6.

### 1c — Justfile contract (full recipe vocabulary, RSS-3 #321)

The helper audits the **full build-recipe vocabulary** in the project `justfile`
(`build run test seed migrate lint clean package deploy smoke release`, plus
`sync-deps`/`vendor-check`; INTERFACE `server` surfaces as `run`), reporting per
recipe:

- **OK** — recipe line found at start-of-line AND the body does not contain
  `# grimoire:placeholder`.
- **PARTIAL** — recipe found but the body contains `# grimoire:placeholder`.
  The recipe is a stub — implement it for this project.
- **MISSING** — no recipe line found, or no `justfile` exists at the repo root.
  See `docs/design/justfile-standard-design.md` for the contract.

**Required vs advisory.** MISSING/PARTIAL is a **problem** (exit 1) only for
**required** recipes; every other vocabulary recipe is **advisory**
(`ADVISORY-MISSING` / `ADVISORY-PARTIAL`, never a failure). A recipe is required
when it is in the **core trio** (`build`, `run`, `deploy`) — unless
`.claude/recipes.json` explicitly declares that target absent
(`implemented:false`, `command:null`) — **or** `.claude/recipes.json` marks its
target implemented and routes it to `just <recipe>` (enforcing `recipe.py <t>` ≡
`just <t>`; the justfile `run` recipe maps to the `server` target key). A target
implemented via a raw (non-`just`) command is advisory. The repair plan names the
specific required recipe(s) to fix and lists advisory recipes as optional
coverage recommendations.

### 1d — Feature adoption (agent-run; NOT mechanical)

The helper **does not** run `detect` predicates — they need judgment and live
config reads. After the mechanical audit, **you** run the
`sync-from-upstream/feature-manifest.md` `detect` loop to confirm each framework
feature is actually **adopted**, not merely *available*:

1. Read `.claude/skills/grm-sync-from-upstream/feature-manifest.md`.
2. Read `framework-version` from `.claude/grimoire-config.json` (or note it
   absent → evaluate all entries).
3. For each entry whose `introduced-in` ≤ the current framework version (or all,
   if no version), run its `detect` predicate.
   - `detect` true → **adopted** (healthy).
   - `detect` false → **not adopted** — record as a finding for the report.

This is the same delta-and-detect procedure as `grm-sync-from-upstream` Step 4.5;
do not duplicate it — follow that section.

### 1e — Hook capability contracts (config claims vs installed stamps, #441)

Every shipped guard hook (`protected-branch-guard.sh`, `push-guard.sh`,
`stealth-guard.sh`, `worktree-guard.sh`, `bundled-sync-guard.sh`,
`release-plan-guard.sh`, `autonomy-allow.sh`) carries a machine-readable
`# HOOK_CONTRACT: vN capabilities=[cap-a,cap-b,...]` comment in its first
lines, declaring what that hook version actually enforces. The helper
cross-checks a small registry (`CAPABILITY_CLAIMS` in `install_doctor.py`)
mapping a `grimoire-config.json` dial to the capability it implies and the
hook(s) that implement it — e.g. `autonomous-push.enabled: true` implies
`push-guard.sh` AND `autonomy-allow.sh` must both declare capability
`autonomous-push`.

Three outcomes per registry entry:

- **claim-satisfied** — the config dial is set AND every implementing hook
  declares the capability → a silent `OK` row (visible, never a problem).
- **claim-unmet** — the config dial is set but at least one implementing
  hook's stamp lacks the capability (a stale hook — the exact warden
  incident: `autonomous-push.enabled` claimed for weeks after
  `push-guard.sh` predated the feature) → **FAIL**, a `PROBLEM_STATUSES`
  member that drives exit 1.
- **no-claims** — the config dial isn't set for this project → the entry is
  skipped entirely, not even an `OK` row.

An absent hook file (never synced) declares zero capabilities, so it fails
any claim naming it — the fail-closed direction for a health check. The
repair step for a claim-unmet FAIL is **re-sync `.claude/hooks/` from
upstream** (hooks are an atomic-replace artifact class, v3.90) to pick up a
hook version whose stamp matches its real behavior, or unset the config
claim if it no longer applies. Never hand-edit a `HOOK_CONTRACT` line to
silence the mismatch without confirming the hook's actual behavior supports
the claim — that turns a real mechanical signal into a lie.

Design rationale and the full contract-header format are a framework-internal
design spec — see the upstream Grimoire repository for that rationale.

---

---

## Anti-patterns

- **Reimplementing merge/restore/adopt logic** — the whole point is to wrap
  `grm-workflow-bootstrap` and `grm-sync-from-upstream`. If you find yourself diffing
  files to overwrite, or 3-way-merging by hand, stop and call the owning skill.
- **Mutating tracked files** — `audit` is read-only and `repair` only prints a
  plan (its lone self-write, `--freeze-baseline`, hits only the gitignored cache);
  every tracked-file write flows through a wrapped skill the plan names.
- **Repairing suppressed divergence** — never overwrite a SEED-DIVERGED,
  PARADIGM, or NEWER-THAN-GOLDEN file; re-freeze golden instead. Route a real
  DRIFTED file through `grm-workflow-bootstrap`'s diff-and-confirm.
- **Resetting a fork's `UPSTREAM_REPO`** — a non-default upstream is legitimate;
  flag a *malformed* one, never clobber a valid custom URL.
- **Folding `migrate` into repair** — migration moves user data and is always
  separately confirmed and backed up, even under Noir.
- **Running inside the scaffolding repo** — there `claude-code/` is the golden
  source; the audit's "drift" is meaningless. Run it in downstream projects.
- **Treating "available" as "adopted"** — a feature's files can be present while
  its config was never enabled. Always confirm via the `detect` predicate.
- **Committing or pushing** — this skill reads, audits, and (on repair) calls
  other skills; it never commits.
- **Hand-editing a `HOOK_CONTRACT` line to silence a claim-unmet FAIL** — the
  stamp must reflect the hook's actual behavior. If a claim is genuinely
  unmet, re-sync the hook from upstream (or unset the config claim); never
  edit the header to match the config without confirming the hook does what
  it now claims.

## Config validation (v1.31, #68)

As part of the read-only health audit, run `grm-config-validate` on
`.claude/grimoire-config.json` — it checks required fields, dial value-sets,
cross-rules (e.g. `Auto` requires Noir), and surfaces unknown/stale fields. During
a repair, offer `config-validate --migrate` to fill additive defaults atomically.
A malformed/stale config is surfaced here instead of failing late (design
rationale in the upstream Grimoire repository, framework-internal).

## Justfile contract check (v3.53, #196; full vocabulary v3.78, RSS-3 #321)

The script audits the full build-recipe vocabulary in the repo's `justfile`
(`build run test seed migrate lint clean package deploy smoke release` +
`sync-deps`/`vendor-check`; INTERFACE `server` surfaces as `run`). Each is
classified **OK**, **PARTIAL**, or **MISSING** (see §1c above). MISSING/PARTIAL on
a **required** recipe causes the audit to exit non-zero; advisory recipes are
reported (`ADVISORY-MISSING` / `ADVISORY-PARTIAL`) but never fail. The repair plan
names the required recipe(s) to fix and lists advisory recipes as optional. The
project owner must implement or complete the recipe body — install-doctor never
writes the justfile itself.

Full contract and per-recipe body expectations:
`docs/design/justfile-standard-design.md`.

## Docs legacy style finding (v3.37, WH-5)

As part of the read-only health audit, run `docs_migrate.py` in detect mode:

```bash
python3 .claude/skills/grm-docs-migrate/docs_migrate.py
```

If it exits 1 (findings present), surface a finding of type:

```
DOCS_LEGACY_STYLE — docs/ contains files missing breadcrumbs or using absolute
links. Run sync-from-upstream to review migration options, or run
docs_migrate.py --apply directly.
```

This is a **warn-only** finding in default mode (never a repair target).
Migration is always separately confirmed via `grm-sync-from-upstream`
Step 4.7 or by running `docs_migrate.py --apply` directly. Never auto-run
`--apply` as part of a repair.
