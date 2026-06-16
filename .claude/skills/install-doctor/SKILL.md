---
name: install-doctor
description: One idempotent, non-destructive-by-default health check for a Grimoire install — audits framework files against the workflow-bootstrap golden baseline (MISSING / DRIFTED), validates the upstream connection (.scaffold-upstream.conf, .scaffold-base/, UPSTREAM_REPO reachability), and confirms every sync feature-manifest feature is actually adopted. Default is a read-only audit that emits a health-report artifact; repairs happen only under an explicit --repair / --reinstall flag by WRAPPING workflow-bootstrap and sync-from-upstream. Triggers on "run the install doctor", "verify my install", "check framework health", "is my scaffold healthy", "repair the scaffold install", "reinstall the framework", "diagnose the scaffolding".
---

# Install-doctor

A single, idempotent health check for a Grimoire-bootstrapped project. It
answers one question — *"is this scaffold installed correctly, connected to
its upstream, and fully adopted?"* — and, only when explicitly asked, repairs
what is broken.

It is a **skill, not a role** (see `docs/design/agent-roles-design.md`): it has
no session of its own, owns no branch, and performs no integration. It runs in
whatever session invokes it.

## The cardinal rule: WRAP, never reimplement

Install-doctor does **not** contain its own merge, restore, or adoption logic.
It composes the two skills that already own those operations:

| Concern | Owned by | install-doctor's job |
|---|---|---|
| Framework-file restore (MISSING/PRISTINE/CUSTOMISED/DRIFTED) | **`workflow-bootstrap`** | Detect MISSING/DRIFTED, then *call* `workflow-bootstrap --restore`. |
| Upstream 3-way merge + base provenance | **`sync-from-upstream`** | Validate its inputs, then *call* its script (`--adopt-base` / `--apply`). |
| Feature detect / adopt predicates | **`sync-from-upstream/feature-manifest.md`** | Run each `detect`; run pending `adopt` via the sync adoption procedure. |

The helper script `install_doctor.py` does the mechanical, deterministic audit
(file walk vs golden, conf parse, base check, reachability probe) so this prose
stays judgment-only. **Default = read-only.** Nothing mutates without `--repair`.

---

## Step 0 — Where you are

Run from the repo root of the project being checked (the helper auto-detects the
root by walking up to `.claude/grimoire-config.json`). Do **not** run inside the
scaffolding distribution repo itself — there, `claude-code/` *is* the golden
source, so the audit reports expected, meaningless "drift". This skill is for
*downstream* projects that were bootstrapped from the scaffolding.

---

## Step 1 — Audit (always; read-only)

Run the mechanical audit:

```bash
python3 .claude/skills/install-doctor/install_doctor.py audit          # Markdown
python3 .claude/skills/install-doctor/install_doctor.py audit --json   # machine-readable
python3 .claude/skills/install-doctor/install_doctor.py audit --no-network  # skip reachability probe
```

The script performs three audits and emits the health-report artifact (see
*Output format*). It exits `0` when healthy, `1` when any check is degraded,
`2` on a usage/internal error.

### 1a — Framework files (vs `workflow-bootstrap` golden)

The helper classifies every golden-managed file (the same `golden/` tree
`workflow-bootstrap` restores from) as:

- **OK** — present and matches golden, or a known project-customised file
  (`CLAUDE.md`, `settings.json`, `.scaffold-upstream.conf`).
- **MISSING** — no live file. Restorable.
- **DRIFTED** — present but differs from golden (and not an expected-custom
  file). Needs human/agent review — never a silent overwrite.

This mirrors the `workflow-bootstrap` MISSING/PRISTINE/CUSTOMISED/DRIFTED
taxonomy, collapsing the two no-action states (PRISTINE, CUSTOMISED) into OK.

### 1b — Upstream connection (`sync-from-upstream` inputs)

The helper validates the inputs `sync-from-upstream` consumes:

- `.scaffold-upstream.conf` present and parseable; `UPSTREAM_REPO` non-empty
  and shaped like a URL / scp-path / existing local path.
- `UPSTREAM_REPO` **reachable** via a non-mutating `git ls-remote` probe
  (skipped under `--no-network`).
- `.scaffold-base/` present and non-empty (the 3-way merge base; absence means
  the next sync degrades to REVIEW-everything).
- `.claude/settings.json` carries the scoped maintenance-script
  **`permissions.allow`** allowlist (#72) so the sync scripts run unattended
  without the auto-mode classifier re-prompting. Absence is a **degraded**
  finding (not broken — syncs still work, they just re-prompt), repaired in
  Step 3 §6.

### 1c — Feature adoption (agent-run; NOT mechanical)

The helper **does not** run `detect` predicates — they need judgment and live
config reads. After the mechanical audit, **you** run the
`sync-from-upstream/feature-manifest.md` `detect` loop to confirm each framework
feature is actually **adopted**, not merely *available*:

1. Read `.claude/skills/sync-from-upstream/feature-manifest.md`.
2. Read `framework-version` from `.claude/grimoire-config.json` (or note it
   absent → evaluate all entries).
3. For each entry whose `introduced-in` ≤ the current framework version (or all,
   if no version), run its `detect` predicate.
   - `detect` true → **adopted** (healthy).
   - `detect` false → **not adopted** — record as a finding for the report.

This is the same delta-and-detect procedure as `sync-from-upstream` Step 4.5;
do not duplicate it — follow that section.

---

## Step 2 — Report (always)

Present the health report (Step 1's artifact plus your Step 1c adoption
findings). If everything is OK / adopted and the upstream is reachable, stop
here — the install is healthy, nothing to repair. **Do not mutate anything on a
healthy or merely-degraded audit unless the user asked to repair.**

---

## Step 3 — Repair (only under `--repair` / `--reinstall`)

Run this step **only** when the user explicitly asked to repair / reinstall
(e.g. "repair the scaffold install", "reinstall the framework"). Repair is the
single mutating phase and is performed entirely by **calling the wrapped
skills** — install-doctor writes no project file directly.

Map each finding to its owning skill and act in this order:

1. **MISSING / DRIFTED framework files** → invoke the **`workflow-bootstrap`**
   skill with `--restore`. It restores MISSING files from golden and, for
   DRIFTED files, shows a diff and asks before overwriting (never silent). Let
   it own that confirmation — do not pre-empt it.
2. **Missing / empty `.scaffold-upstream.conf`** → `workflow-bootstrap`
   Step 2.5 re-seeds the default `UPSTREAM_REPO` idempotently (it never
   overwrites a non-empty value). Running `workflow-bootstrap --restore` in
   step 1 already covers this.
3. **Missing / empty `.scaffold-base/`** → run
   `.claude/skills/sync-from-upstream/sync-from-upstream.sh --adopt-base`
   to record the merge base (touches no local file), per `sync-from-upstream`
   Step 1. Do this only once the project is confirmed reconciled with a known
   upstream commit.
4. **Pending feature adoptions** (Step 1c found `detect` = false) → run the
   `sync-from-upstream` **Step 4.5 adoption loop** for those features
   (paradigm-gated: auto under Noir, per-feature prompt under
   Supervised/Weiss). **Never** run a `migrate` step as part of repair —
   migration is always separately confirmed and backed up, even under Noir.
5. **Unreachable / malformed `UPSTREAM_REPO`** → this is a *config* problem, not
   a file to restore. Surface it and ask the user for the correct URL; do not
   guess. (A fork's custom upstream is legitimate and must not be reset.)
6. **Missing maintenance-script `permissions.allow` allowlist (#72)** → a project
   bootstrapped before v3.2 may lack the `permissions.allow` block for the
   framework's sync scripts, so the classifier re-prompts on every
   `sync-from-upstream`/`sync-from-source` run. Idempotently **merge** the scoped
   allowlist (the block in `workflow-bootstrap` Step 2 §settings.json) into the
   live `permissions.allow`, skipping entries already present. **Path-scoped to
   framework scripts only — never widen.** Editing `settings.json` permissions is
   **user-confirmed**; the guard hooks remain the safety net regardless.

After repairs, **re-run Step 1** (the audit is idempotent) and emit a fresh
report showing what changed. A second clean run is the success signal.

### Non-destructive guarantees (do not break these)

- Default mode mutates nothing — only `--repair`/`--reinstall` may write.
- File overwrites go through `workflow-bootstrap`'s diff-and-confirm; never
  overwrite a DRIFTED file silently.
- `--adopt-base` declares "local matches upstream" — only run it when true,
  never to skip a real reconciliation.
- Migration of user data is never part of repair — defer to the explicitly
  confirmed, backed-up `migrate` path.
- No git commits, no pushes. The user (or integration master) commits.

---

## Output format — health report artifact

The helper emits a Markdown report (or JSON with `--json`). Shape:

```
# Grimoire install-doctor health report

- Repo root: `/abs/path`
- Overall: **HEALTHY** | **ATTENTION NEEDED**
- Tallies: ok=N, missing=N, drifted=N, warn=N, fail=N

## Framework files (vs workflow-bootstrap golden)
| Item | Status | Detail |
| `skills/foo/SKILL.md` | OK | present, matches golden |
| `.scaffold-upstream.conf` | MISSING | absent — restore via workflow-bootstrap --restore |

## Upstream connection (sync-from-upstream inputs)
| `UPSTREAM_REPO` | OK | https://github.com/…/grimoire-framework.git @ main |
| `UPSTREAM_REPO reachability` | OK | reachable (git ls-remote) |

## Sync base snapshot (.scaffold-base)
| `.scaffold-base` | OK | present (N file(s) recorded) |

## Notes
- Feature-adoption is NOT audited mechanically: run each feature-manifest detect …
```

Append your **Step 1c adoption findings** under the report (one line per
manifest feature: adopted / not-adopted, with the feature-id). When you run a
repair, append a **Repairs applied** section listing each wrapped-skill call and
its outcome. The report is an in-session artifact — present it to the user; do
not write it to a file unless the user asks.

---

## Anti-patterns

- **Reimplementing merge/restore/adopt logic** — the whole point is to wrap
  `workflow-bootstrap` and `sync-from-upstream`. If you find yourself diffing
  files to overwrite, or 3-way-merging by hand, stop and call the owning skill.
- **Mutating on a plain audit** — default is read-only. No file write without an
  explicit `--repair` / `--reinstall` request.
- **Silently overwriting a DRIFTED file** — route it through
  `workflow-bootstrap`'s diff-and-confirm; a customised skill may be deliberate.
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

## Config validation (v1.31, #68)

As part of the read-only health audit, run `config-validate` on
`.claude/grimoire-config.json` — it checks required fields, dial value-sets,
cross-rules (e.g. `Auto` requires Noir), and surfaces unknown/stale fields. Under
`--repair`, offer `config-validate --migrate` to fill additive defaults atomically.
A malformed/stale config is surfaced here instead of failing late. See
`docs/design/defaults-quickstart-design.md`.
