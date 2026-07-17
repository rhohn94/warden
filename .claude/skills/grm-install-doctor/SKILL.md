---
name: grm-install-doctor
description: One idempotent, non-destructive-by-default health check for a Grimoire install — audits framework files against the workflow-bootstrap golden baseline (MISSING / DRIFTED), validates the upstream connection, confirms feature adoption, checks the Justfile contract, and notices an absent architecture ruleset. Read-only audit by default; `repair` emits a non-destructive plan. Use when verifying install / framework health or repairing the scaffold.
---

# Install-doctor

A single, idempotent health check for a Grimoire-bootstrapped project. It
answers one question — *"is this scaffold installed correctly, connected to
its upstream, and fully adopted?"* — and, only when explicitly asked, repairs
what is broken.

It is a **skill, not a role** (the role taxonomy is a framework-internal design
— see the upstream Grimoire repository): it has no session of its own, owns no
branch, and performs no integration. It runs in whatever session invokes it.

## The cardinal rule: WRAP, never reimplement

Install-doctor does **not** contain its own merge, restore, or adoption logic.
It composes the two skills that already own those operations:

| Concern | Owned by | install-doctor's job |
|---|---|---|
| Framework-file restore (MISSING/PRISTINE/CUSTOMISED/DRIFTED) | **`grm-workflow-bootstrap`** | Detect MISSING/DRIFTED, then *call* `workflow-bootstrap --restore`. |
| Upstream 3-way merge + base provenance | **`grm-sync-from-upstream`** | Validate its inputs, then *call* its script (`--adopt-base` / `--apply`). |
| Feature detect / adopt predicates | **`sync-from-upstream/feature-manifest.md`** | Run each `detect`; run pending `adopt` via the sync adoption procedure. |

The helper script `install_doctor.py` does the mechanical, deterministic audit
(file walk vs golden, conf parse, base check, reachability probe) so this prose
stays judgment-only. **Default = read-only.** The `repair` subcommand never
mutates either — it only *prints a repair plan* (which wrapped skill to call for
each finding). All actual mutation flows through the wrapped skills.

---

## Step 0 — Where you are

Run from the repo root of the project being checked (the helper auto-detects the
root by walking up to `.claude/grimoire-config.json`). Do **not** run inside the
scaffolding distribution repo itself — there, `claude-code/` *is* the golden
source, so the audit reports expected, meaningless "drift". This skill is for
*downstream* projects that were bootstrapped from the scaffolding.

---

## Step 2 — Report (always)

Present the health report (Step 1's artifact plus your Step 1c adoption
findings). If everything is OK / adopted and the upstream is reachable, stop
here — the install is healthy, nothing to repair. **Do not mutate anything on a
healthy or merely-degraded audit unless the user asked to repair.**

---

## Step 3 — Repair (only when the user asks)

Run this step **only** when the user explicitly asked to repair / reinstall
(e.g. "repair the scaffold install", "reinstall the framework"). First get the
plan from the script's `repair` subcommand:

```bash
python3 .claude/skills/grm-install-doctor/install_doctor.py repair             # audit + plan
python3 .claude/skills/grm-install-doctor/install_doctor.py repair --json      # machine-readable plan
python3 .claude/skills/grm-install-doctor/install_doctor.py repair --freeze-baseline  # freeze golden, then audit
python3 .claude/skills/grm-install-doctor/install_doctor.py --repair           # back-compat == repair --freeze-baseline
```

`repair` is **non-destructive of tracked files**: by default it prints the audit
*plus an ordered repair plan* (which wrapped skill to call for each real finding)
and writes no project or framework file. Suppressed divergence (SEED-DIVERGED /
PARADIGM / NEWER-THAN-GOLDEN) is **never** in the plan, so a repair can never
revert synced or active-paradigm content. Framework-file repair is performed
entirely by **you calling the wrapped skills** the plan names.

### 3 §0 — Freeze the golden baseline (the one self-contained repair)

When the `golden-baseline` check is **WARN** (no frozen baseline — typical right
after adopting the generated-golden feature), the audit skips the entire
framework-file check. `repair --freeze-baseline` (back-compat: `--repair`)
closes that gap **non-interactively**: it derives a versioned
`golden-v{X.Y}.tar.gz` from the current **pristine** scaffold into the gitignored
`.grimoire-golden/` cache (delegating to `generate_golden.freeze_from_install`),
then re-audits against it. This is the one mutation `install-doctor` performs
itself, and it touches **only the gitignored cache — never a tracked file** (so
the "don't mutate tracked files" contract holds; do not commit the tarball).

Freeze only on a **pristine / freshly-synced** scaffold: the generator treats the
root as the flavor source, so freezing a customized tree would bake drift into the
baseline (same precondition as the `grm-workflow-bootstrap` freeze trigger).

Map each remaining finding to its owning skill and act in this order:

1. **MISSING / DRIFTED framework files** → invoke the **`grm-workflow-bootstrap`**
   skill with `--restore`. It restores MISSING files from golden and, for
   DRIFTED files, shows a diff and asks before overwriting (never silent). Let
   it own that confirmation — do not pre-empt it. **Never** restore a
   NEWER-THAN-GOLDEN / PARADIGM / SEED-DIVERGED file — those are correct;
   re-freeze the golden baseline instead.
2. **Missing / empty `.scaffold-upstream.conf`** → `grm-workflow-bootstrap`
   Step 2.5 re-seeds the default `UPSTREAM_REPO` idempotently (it never
   overwrites a non-empty value). Running `workflow-bootstrap --restore` in
   step 1 already covers this.
3. **Missing / empty `.scaffold-base/`** → run
   `.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh --adopt-base`
   to record the merge base (touches no local file), per `grm-sync-from-upstream`
   Step 1. Do this only once the project is confirmed reconciled with a known
   upstream commit.
4. **Pending feature adoptions** (Step 1c found `detect` = false) → run the
   `grm-sync-from-upstream` **Step 4.5 adoption loop** for those features
   (paradigm-gated: auto under Noir, per-feature prompt under
   Supervised/Weiss). **Never** run a `migrate` step as part of repair —
   migration is always separately confirmed and backed up, even under Noir.
5. **Unreachable / malformed `UPSTREAM_REPO`** → this is a *config* problem, not
   a file to restore. Surface it and ask the user for the correct URL; do not
   guess. (A fork's custom upstream is legitimate and must not be reset.)
6. **Missing maintenance-script `permissions.allow` allowlist** → a project
   bootstrapped before v3.2 may lack the `permissions.allow` block for the
   framework's sync scripts, so the classifier re-prompts on every
   `grm-sync-from-upstream`/`grm-sync-from-source` run. Idempotently **merge** the scoped
   allowlist (the block in `grm-workflow-bootstrap` Step 2 §settings.json) into the
   live `permissions.allow`, skipping entries already present. **Path-scoped to
   framework scripts only — never widen.** Editing `settings.json` permissions is
   **user-confirmed**; the guard hooks remain the safety net regardless.
7. **Absent `.claude/architecture-rules.json`** (ruleset-absent notice, #314) →
   **notice-only, never a repair blocker.** Offer adoption: copy the per-family
   starter matching the project's profile from
   `.claude/quick-start-templates/{service,web,gui,lib}/files/.claude/architecture-rules.json`
   (or `.claude/architecture-rules.example.json`) to
   `.claude/architecture-rules.json` and adapt the layer globs; or, if the
   project deliberately declines, commit a rules file with `"opt_out": true` +
   an `"opt_out-reason"` so the decision is tracked, not silent.
8. **Hook-contract claim-unmet FAIL** (#441) → a config-claimed capability
   (e.g. `autonomous-push.enabled`) whose implementing hook's `HOOK_CONTRACT`
   stamp doesn't declare it. Re-sync `.claude/hooks/` from upstream via
   `grm-sync-from-upstream` (hooks are an atomic-replace artifact class,
   v3.90) to pick up a hook version whose stamp matches its real behavior, or
   unset the config claim if it no longer applies. **Never** hand-edit the
   `HOOK_CONTRACT` line to silence the mismatch without confirming the hook's
   actual behavior supports the claim.

After repairs, **re-run Step 1** (the audit is idempotent) and emit a fresh
report showing what changed. A second clean run is the success signal.

### Non-destructive guarantees (do not break these)

- The script mutates **no tracked file** — `audit` is fully read-only and
  `repair` only prints a plan. The sole exception is `repair --freeze-baseline`
  (= `--repair`), which writes a golden archive into the gitignored
  `.grimoire-golden/` cache; it never touches a tracked project/framework file.
  All other writes flow through the wrapped skills you invoke.
- File overwrites go through `grm-workflow-bootstrap`'s diff-and-confirm; never
  overwrite a DRIFTED file silently, and never overwrite a suppressed
  (SEED-DIVERGED / PARADIGM / NEWER-THAN-GOLDEN) file at all.
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
- Tallies: ok=N, missing=N, drifted=N, warn=N, fail=N, seed-diverged=N, paradigm=N, newer-than-golden=N, partial=N

## Framework files (vs workflow-bootstrap golden)
| Item | Status | Detail |
| `skills/foo/SKILL.md` | OK | present, matches golden |
| `docs/version-history.md` | SEED-DIVERGED | project-owned seed file — divergence expected |
| `skills/grm-release-phase/SKILL.md` | PARADIGM | matches the active 'noir' paradigm variant |
| `.scaffold-upstream.conf` | MISSING | absent — restore via workflow-bootstrap --restore |

## Upstream connection (sync-from-upstream inputs)
| `UPSTREAM_REPO` | OK | https://github.com/…/grimoire-framework.git @ main |
| `UPSTREAM_REPO reachability` | OK | reachable (git ls-remote) |

## Sync base snapshot (.scaffold-base)
| `.scaffold-base` | OK | present (N file(s) recorded) |

## Justfile contract (full recipe vocabulary)
| `justfile:build` | OK | recipe 'build' present and non-placeholder |
| `justfile:run` | PARTIAL | recipe 'run' has a grimoire:placeholder body — implement the recipe for this project. |
| `justfile:deploy` | MISSING | recipe 'deploy' not found in justfile. See docs/design/justfile-standard-design.md for the contract. |
| `justfile:package` | ADVISORY-MISSING | recipe 'package' absent (advisory — not wired to `just package` in .claude/recipes.json). |

## Architecture-rules adoption (.claude/architecture-rules.json)
| `.claude/architecture-rules.json` | WARN | absent — architecture fitness rules not adopted; copy a per-family starter … |

## Hook capability contracts (config claims vs installed HOOK_CONTRACT stamps)
| `hook-contract:autonomous-push.enabled` | FAIL | config claims 'autonomous-push.enabled' but push-guard.sh does not declare capability 'autonomous-push' … |

## Notes
- Feature-adoption is NOT audited mechanically: run each feature-manifest detect …
```

The `repair` subcommand appends a **Repair plan** section (one line per real
finding → the wrapped skill to call); suppressed divergence is never listed. With
`--freeze-baseline` it prepends a `froze golden baseline -> …` line and a
matching note before re-auditing against the freshly-frozen baseline.

Append your **Step 1c adoption findings** under the report (one line per
manifest feature: adopted / not-adopted, with the feature-id). When you carry out
a repair, append a **Repairs applied** section listing each wrapped-skill call and
its outcome. The report is an in-session artifact — present it to the user; do
not write it to a file unless the user asks.

---

## Reference (load on demand)

- `Step 1 — Audit (always; read-only)` — see `reference.md`
- `Hook capability contracts (config claims vs installed stamps, #441)` — see
  `reference.md` §1e
- `Anti-patterns` — see `reference.md`
- `Config validation` — see `reference.md`
- `Justfile contract check` — see `reference.md`
- `Docs legacy style finding` — see `reference.md`
