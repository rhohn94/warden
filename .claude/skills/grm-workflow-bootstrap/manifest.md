# Workflow skill manifest

The canonical set of skills this workflow depends on. `grm-workflow-bootstrap`
restores any of these from the golden baseline when missing; the baseline
itself is generated on demand from the live/flavor files (v3.49) via
`generate_golden.py`, so there is nothing to hand-snapshot back.

> This is a point-in-time baseline, **not** a perpetually-synced mirror.
> Future projects are not expected to keep the golden image in lock-step with
> their live skills — freeze a new offline baseline deliberately with
> `generate_golden.py --freeze .` when you want one.

## Restorable skills (`golden/skills/`)

| Skill | Purpose |
|---|---|
| `grm-onboarding`              | First-run interview → config → `grm-repo-init`/`grm-workflow-bootstrap` handoff → first-release-planning bridge. Restores with its companion `baseline-requirements.md` (framework-required baseline-roadmap source list). |
| `grm-repo-init`               | Initialize git: `main`/`dev`, branch model, commit rules, push guard. |
| `grm-design-doc-scaffold`     | Create a new design doc + wire the index, house layout. |
| `grm-worktree-preflight`      | Rooting / merge-base checks for a spawned worktree before committing or merging. |
| `grm-release-planning`        | Produce the work-items report for the next version. |
| `grm-release-agreement`       | Freeze the plan, create the `version/{X.Y}` staging branch. |
| `grm-release-phase`           | Emit self-contained subagent prompts for the next phase. |
| `grm-release-agent-tracker`   | Reconcile the §5 ledger against live branches. Restores with its companion `release_plan.py` (the stdlib release-planning ledger engine: §5 parse/diff/merge-queue/preflight/phase-batch + atomic tick), which the `grimoire-release` MCP server and the `grm-ledger-tick`/`grm-release-phase`/`grm-release-phase-merge`/`grm-noir-loop` consumers drive. |
| `grm-release-phase-merge`     | Merge completed agent branches in conflict-map order. |
| `grm-ledger-tick`             | Tick / roll-forward the §5 implementation ledger. |
| `grm-project-release`         | Promote `dev` → `main` and tag the release. |
| `grm-orchestrate-release`     | Autonomous end-to-end release driver (Noir-only): preflight the autonomy dials, then chain planning → agreement → phase → phase-merge → project-release → cleanup with zero prompts. Restores with its companion `orchestrate_preflight.py`. |
| `grm-repo-reference`          | Doc-location map + subagent model/effort table. |
| `grm-source-to-design-docs`   | Generate `docs/design/` from existing source code. |
| `grm-design-language-adapt`   | Adopt/refresh the UX design language: pull upstream (or honour strict-local), produce the local adaptation, record source SHA. (GUI projects only.) |
| `grm-ux-demo-build`           | Build/refresh a minimal `ux-demo/` in the project's own stack to verify the design-language adaptation. Opt-in only. (GUI projects only.) |
| `grm-ux-demo-regress`         | Capture + diff `ux-demo/` screenshots against a committed baseline to detect visual drift (`--accept` / `--check`); correlates drift against design-token changes. Opt-in. (GUI projects only.) |
| `grm-workflow-scaffold`       | Scaffold a new `.claude/workflows/{name}.js` Workflow, encoding the measured model-tiering + batch-vs-fanout cost lessons. (Claude-Code-only.) |
| `grm-token-measure`           | Measure per-class token usage (input / output / cache-read / cache-creation) per operation from a session `.jsonl` transcript and emit the token-efficiency report table. Read-only. Restores with its companion `parse_usage.py`. |
| `grm-integration-master`      | Integration master role guide — owns release scope, spawns work-item sessions, and integrates results. Paradigm-specific: content is swapped by `grm-work-paradigm-switch`. |
| `grm-work-paradigm-switch`    | Install or switch the active Work Paradigm by file-swapping content from `.claude/paradigms/<slug>/` into stable active paths. Called by onboarding and `workflow-bootstrap --restore`. |
| `grm-model-effort-profile-switch` | Switch the active model/effort distribution profile (cost posture): validate `model-effort-profile.value` against the registry `.claude/model-effort-profiles.json` and write it to config. Pure data — no file-swap; the `grm-repo-reference` resolver reads the field live. Called by onboarding (cost-posture step) and on demand. |
| `grm-workflow-variant-switch` | Switch the active execution strategy (dispatch posture): validate `workflow-variant.value` against the preset set `{Fast, Efficient, Cheap-Slow}` and write it to config. Pure data — no file-swap; the integration master reads the field live at dispatch. Migrates legacy `Careful-Serial` → `Cheap-Slow` and drops a legacy `in-development` preview flag. Called by onboarding (execution-strategy step) and on demand. |
| `grm-release-phase-model-switch` | Switch the active release-phase model (how the integration master executes an agreed plan): validate `release-phase-model.value` against the set `{Default, Auto}` and write it to config. Pure data — no file-swap; the integration master reads the field live at execution. `Auto` is Noir-only and fails closed (refuses to set `Auto` unless `work-paradigm.value == "Noir"`). Called by onboarding (release-phase-model step) and on demand. Design: `docs/design/release-phase-model-design.md`. |
| `grm-hard-reset`              | Re-initialize the scaffold to its pristine, not-yet-onboarded state — archives (never deletes) project-local files to `.grimoire-archive/<ts>/`, restores framework files to golden, re-arms the sentinel, hands to `grm-onboarding`. The running copy preserves itself mid-reset (never archives/clears the in-flight skill). |
| `grm-issue-tracker`           | Manage the project's issue tracker connection — configure provider (GitHub/Linear/Jira), list, view, create, update, and close issues via the configured API. Restores with its companions `issue_tracker.py` and `migrate_roadmap_issues.py`. |
| `grm-issue-tracker-switch`    | Switch or reconfigure the active issue tracker provider: validate provider name, update `grimoire-config.json`, and verify connectivity. Restores with its companion `issue_tracker_switch.py`. |
| `grm-feedback-to-issue`       | Convert a piece of feedback (bug, idea, UX note) into a well-formed issue and file it to the configured tracker via `grm-issue-tracker`. |
| `grm-agent-reporter`                | Narrow-context own-session agent: receive a feedback payload, classify it via the taxonomy in `docs/grimoire/integration-workflow.md`, and file it through `grm-feedback-to-issue`. No git writes. |
| `grm-agent-reviewer`                | Narrow-context own-session pre-merge auditor (RV1): reads a completed branch/diff and returns blocking/non-blocking findings. Wraps `code-review`; read-only, no git writes. Canonical contract: `docs/grimoire/design/agent-roles-design.md`. |
| `grm-agent-scout`                   | Narrow-context own-session research agent (SC1): investigates a bounded question and returns a condensed brief. Wraps `Explore`/`deep-research`; strictly read-only, no writes. Canonical contract: `docs/grimoire/design/agent-roles-design.md`. |
| `grm-agent-verifier`                | Narrow-context own-session QA agent (QA1): runs tests/build/release against a branch and checks acceptance criteria, returning a pass/fail verdict. No source edits, no git writes. Canonical contract: `docs/grimoire/design/agent-roles-design.md`. |
| `grm-agent-triager`                 | Narrow-context own-session backlog-groomer (TR1): dedupes, labels, prioritizes, and closes stale tracker items via `grm-issue-tracker`. Tracker-only write surface, no git writes. Canonical contract: `docs/grimoire/design/agent-roles-design.md`. |
| `grm-agent-researcher`              | Narrow-context own-session research-then-file agent: investigates an under-specified idea and files ONE scoped item, composing `grm-source-to-design-docs`/`grm-design-doc-scaffold`/`grm-feedback-to-issue`. Tracker-only write surface. Canonical contract: `docs/grimoire/design/agent-roles-design.md`. |
| `grm-install-doctor`          | Idempotent, non-destructive-by-default framework health check: audits files vs golden, validates the upstream connection, and confirms feature adoption. Wraps `grm-workflow-bootstrap`/`grm-sync-from-upstream`. Restores with its companion `install_doctor.py`. |
| `grm-cost-budget`             | Operate the cost-governance config cluster: token budget + utilization reporting, per-agent verbosity, and peak-hour scheduling policy. Reads `cost-governance` config; reuses `grm-token-measure` for utilization. Restores with its companion `cost_budget.py` (the stdlib budget engine: window rolling, once-per-window threshold-crossing detection, and `cost-utilization.json` ledger arithmetic; reuses `parse_usage.py` for transcript parsing). Design: `docs/grimoire/design/cost-governance-design.md`. |
| `grm-priority-picker`         | Advisor skill: interview the user to rank speed/quality/cost, map the 2-of-3 trade-off to concrete dial values, and write them via the switch skills. Surfaces the Steady Steward preset. Design: `docs/grimoire/design/cost-governance-design.md`. |
| `grm-coding-practices-audit`  | Agent-driven adherence audit: assembles a checklist from the audit-hints in `coding-standards.md`/`architecture-guidelines.md`/sub-docs, reports gaps, and optionally files one issue per gap via `grm-feedback-to-issue` (`--file-issues`). Read-only except tracker writes; no git writes. Design: `docs/design/coding-practices-audit-design.md`. |
| `grm-architecture-audit`      | Deterministic architecture fitness functions: read the declarative `.claude/architecture-rules.json` (layers, allowed dependency edges, forbidden imports, no-cycles) and report every violation (`file:line — rule-id`) over the project's import graph — the deterministic complement to `grm-coding-practices-audit`'s narrative pass. Read-only by default; `--gate` escalates per the v1.26 `code-quality` dials; an absent rules file exits clean but emits a visible WARN pointing at the per-family starter rulesets (never silent, #314; explicit `opt_out` supported). Design: `docs/grimoire/design/architecture-fitness-design.md`. |
| `grm-component-catalog-export`| Scan reusable components (`component.json`/front-matter) and emit a machine + human-readable catalog (id, profiles, provides/requires, compat, stability). Read-only; for downstream consumers to discover components + author templates. A *view* over `.claude/component-registry.json` when present, live scan otherwise. Design: `docs/design/quick-start-templates-design.md`. |
| `grm-component-registry`      | Build/update the versioned registry `.claude/component-registry.json` from the same `component.json`/front-matter sources the export reads: versions each component (declared `version` or content-hash), diffs added/changed/removed/unchanged vs the prior registry, and validates tags against the `component-taxonomy` authority (unknown tags surfaced, never silently accepted/dropped). Idempotent — unchanged sources ⇒ byte-identical file. Restores with its companion `component_registry.py` (the stdlib discover/version/validate-taxonomy/diff/write-idempotently engine: sha256 content hashing, sorted-key serialization, content-derived build id, atomic temp+replace write; file-write-only). Design: `docs/design/component-catalog-architecture-design.md`; taxonomy: `docs/design/component-taxonomy.md`. |
| `grm-config-validate`     | Validate `.claude/grimoire-config.json` against the declared schema (known blocks + value sets + cross-rules like Auto-requires-Noir), report unknown/missing fields, and run an idempotent `--migrate` that fills additive defaults atomically. Backed by `config_validate.py`; read-only by default. Called by `grm-install-doctor`. Design: `docs/design/defaults-quickstart-design.md`. |
| `grm-doc-assurance`        | Five deterministic checks over the repo's own docs — flavor-parity (root ↔ claude-code ↔ copilot), design-doc house-layout, internal-link integrity, a validated docs map (`docs/README.md`), and cross-doc release consistency (version-history ↔ roadmap ↔ feature-manifest ↔ framework-version). Stdlib-only script; read-only except `--write-map`, report-only unless `--strict`. Design: `docs/grimoire/design/doc-assurance-design.md`. |
| `grm-code-health`          | Emit a two-section code-health report — dead code + duplication (vulture / ts-prune / cargo-udeps + jscpd) and complexity + maintainability (radon / ts-complexity / gocyclo) with a delta vs a stored baseline (`.claude/cache/code-health-baseline.json`). Read-only by default; `--accept` rebaselines, `--gate` warns/blocks on a regression via the v1.26 `code-quality` dials. Design: `docs/grimoire/design/managed-project-tooling-design.md`. |
| `grm-dependency-audit`     | Run the language-appropriate dependency vulnerability scanner (pip-audit / npm audit / cargo audit / govulncheck) behind one abstraction; emit a normalized findings report (package, advisory, severity, fixed-in). Read-only; `--file-issues` routes findings through `grm-feedback-to-issue`, `--fail-at` gates a release. Restores with its companion `dependency_channel_conformance.py` (the v3.29 Dependency Channel `vendor-check` conformance gate — three deterministic checks, recomputes a `tree_sha256` byte-identical to `grm-sync-deps`, offline `--self-test`). Design: `docs/grimoire/design/managed-project-tooling-design.md` + `docs/design/dependency-channel-design.md` §5. |
| `grm-sync-deps`            | Dependency Channel consumer engine (v3.29, DEP-CH-2) — reconcile each first-party dep declared in `vendor.toml` from its published release channel: resolve channel→version (pinned by default; `--update` re-pins to latest-on-channel via `gh`), download `release.json`/`SHA256SUMS`/artifact, **verify sha256 before placement** (hard-refuse a tampered artifact, leaving `vendor/<dep>/` untouched), atomic-replace the vendored tree, and write a two-hash JSON `vendor.lock` (`artifact_sha256` wire + `tree_sha256` offline, both `sha256:<hex>`). `--check` reports drift writing nothing; `--offline` validates vendored bytes vs the lock with zero network calls. Restores with its companion engine `sync_deps_engine.py` (stdlib; asset-name allowlist + fixed staging + verify-before-rename). Design: `docs/design/dependency-channel-design.md` §3–§4. |
| `grm-vendor-migrate`       | Dependency Channel migration helper (v3.29, DEP-CH-6) — convert an existing git submodule / vendored dir into `vendor.toml` + `vendor.lock`: read the gitlink + `.gitmodules`, derive the GitHub slug, resolve the published release whose asset matches the fetched tree (prefer the tag), and write the dep declaration + lock; **loud fallback** (record commit + content-sha) when no published release matches — never a silent pin to a moving ref. Re-run never clobbers a hand-edited `vendor.toml`. Restores with its companion `vendor_migrate.py` (stdlib; offline `--self-test` seeds a synthetic submodule fixture and asserts the round-trip). Design: `docs/design/dependency-channel-design.md` §7. |
| `grm-quick-start-template`    | Match the declared app profile against `.claude/quick-start-templates/*` and apply the closest (scaffold ready-made pieces + config defaults via switch skills). Never overwrites without confirmation. Design: `docs/design/quick-start-templates-design.md`. |
| `grm-web-app-apply`           | Retrofit web-app support onto an already-bootstrapped project: re-run the Q9 signal table read-only, confirm (auto-pick under Noir), write the `web-app` config block (pure-data, no schema bump), and seed the web-app obligations (baseline rows, deployment-protocol pointer, recipe deploy/package stubs) + a `grm-required-feature-catalog` filing run for family `web`. Idempotent; fails closed on a missing config or non-web repo. Restores with its companion `reference.md` (Q9 signal table). Design: `docs/design/web-app-support-design.md`. |
| `grm-required-feature-catalog` | Family-neutral catalog of framework-mandated features (Admin Console, Changelog Surface, standard-package adoptions, ...), each entry gated by `applies-when-family` (cli/gui/lib/service/web) and an optional config-dial `applies-when`. Re-runnable — `catalog_filing.py plan` deterministically reports new/changed/already-satisfied/blocked-on-upstream entries against a persisted per-project filing ledger, offline. Invoked by `grm-onboarding` §6.5.7 and `grm-web-app-apply` §6. Restores with its companions `required-feature-catalog.md` (the versioned catalog) and `catalog_filing.py`. Relocated here from `grm-web-app-apply` in v3.97 (#413). |
| `grm-sync-from-upstream`      | Pull upstream scaffolding updates into a downstream project (non-destructive 3-way merge). Restores with its companions `feature-manifest.md` and `sync-from-upstream.sh`. |
| `grm-docs-migrate`            | Detect and migrate old-style docs to the wiki hierarchy: classify FLAT_TIER/ORPHAN/ABSOLUTE_LINK/PROSE_LINK/NO_BREADCRUMB findings (detect mode); archive-first rewrite of breadcrumbs + relative links (--apply). Downstream-safe root detection. Restores with its companion `docs_migrate.py`. |
| `grm-files-manifest`          | Grimoire-owned files manifest: validate `.claude/grimoire-files.json` against the live scaffold, detecting MISSING, STALE, or EXTRA entries across all three flavors (root / claude-code / copilot). Stdlib-only; read-only by default; `--strict` exits non-zero on any finding. Restores with its companion `validate_files_manifest.py`. Design: `docs/grimoire/design/files-manifest-design.md`. |
| `grm-regenerate-grimoire`     | Surgical self-restoration of live scaffold files from the golden baseline: reads the golden file list, copies only MISSING or explicitly named targets into place, leaves clean files untouched. Runs `grm-install-doctor` post-restore to confirm. Copilot refuses (golden absent); Claude-Code idempotent. Restores with its companion `regenerate_grimoire.py`. |
| `grm-end-session`             | Orchestration guide for the end-of-session cleanup sequence: assess in-flight state, delegate merges to `grm-release-phase-merge`, delegate release to `grm-project-release`, clean up dead worktrees and stale branches, then confirm `dev`/`main` match origin and emit a cold-start handoff summary. Claude-Code-only (copilot lacks the release-orchestration skills). |

## Restorable paradigm content sets (`golden/paradigms/`)

Three paradigm content sets mirror `.claude/paradigms/`. Restored by
`workflow-bootstrap --restore` before calling `grm-work-paradigm-switch` to
re-install the active paradigm.

| Paradigm | Directory | Purpose |
|----------|-----------|---------|
| Supervised | `golden/paradigms/supervised/` | Default posture — user-confirmed gates at every major decision. |
| Weiss      | `golden/paradigms/weiss/`      | Collaborative posture — user leads design; agent is researcher/assistant. |
| Noir       | `golden/paradigms/noir/`       | Autonomous posture — agent drives phases unsupervised until milestone/stop. |

Each paradigm directory contains: `integration-master-SKILL.md`,
`release-phase-SKILL.md`, `release-phase-merge-SKILL.md`,
`CLAUDE-agent-role.md`, `CLAUDE-task-execution.md`, `integration-workflow.md`.

Always-delivered alongside the content sets:

| File | Purpose |
|------|---------|
| `golden/paradigms/README.md` | Static paradigm breadcrumb index — always delivered to `.claude/paradigms/README.md` by `grm-workflow-bootstrap` (independent of the selected paradigm and of `--restore`) so all three paradigm names + the switch path stay discoverable in-project. No project-config tokens; rewritten from golden on every run (idempotent). Tracked by `grm-install-doctor` / `--restore`. |

## Restorable infrastructure (`golden/hooks/`, `golden/settings.json`, `golden/push-allowlist`, `golden/model-effort-profiles.json`, `golden/.scaffold-upstream.conf`, `golden/docs/`, `golden/vendor.toml`)

| File | Purpose |
|---|---|
| `protected-branch-guard.sh` | Deny-by-default guard on `dev`/`main`/`version/*`. |
| `push-guard.sh`             | Restricts `git push` to allowlisted refs from the marker-blessed integration worktree. |
| `release-plan-guard.sh`     | Locks §§1–4 of an agreed release plan (only §5 editable). |
| `worktree-guard.sh`         | Blocks tool calls targeting paths outside the worktree. |
| `autonomy-allow.sh`         | Paradigm-aware prompt suppression: auto-approves guard-vetted pipeline commands under Noir (deny guards take precedence). |
| `worktree-brief.sh`         | SessionStart brief: automatic isolation context + wrong-base warnings in every spawned worktree. |
| `settings.json`             | Wires the guard + autonomy hooks (`PreToolUse`) and the worktree brief (`SessionStart`). |
| `push-allowlist`            | Extends the `push-guard` default allowlist with project-specific refs. |
| `model-effort-profiles.json` | Paradigm-invariant model/effort profile registry (`.claude/model-effort-profiles.json`) — the single source of truth for the band × profile matrix the `grm-repo-reference` resolver consumes. Restores to `.claude/` so fresh/restored scaffolds resolve subagent tiers. |
| `.scaffold-upstream.conf`   | Default Grimoire upstream URL seed (`UPSTREAM_REPO=https://github.com/rhohn94/grimoire-framework.git`). Seeded by `grm-workflow-bootstrap` Step 2.5 (v1.13+); idempotent — never overwrites a non-empty `UPSTREAM_REPO`. Override by setting `UPSTREAM_REPO` to your fork's URL. |
| `docs/design/ux/design-language.md` | UX design language stub with Aura upstream URL seeded (`source-url: https://github.com/rhohn94/design-language`). GUI projects only. CONFIRM-pending placeholder — verify the Aura URL before the first `grm-design-language-adapt` run. Idempotent — not written for headless or GUI-deferred projects; `source-url:` not overwritten if already set. |
| `golden/docs/README.md` | `docs/README.md` | — | Docs root index stub (seeded at bootstrap) |
| `golden/docs/grimoire/README.md` | `docs/grimoire/README.md` | — | Grimoire tier index stub |
| `vendor.toml` | Dependency Channel intent seed (v3.29) — a commented stub (`schema_version = 1`, no active deps, an example `[deps.aura]` block). Restored to the project root by `grm-workflow-bootstrap` Step 2.8 **only if MISSING** (never clobbers a project's real dep declarations). Its companion `vendor.lock` is **not** a golden file — Step 2.8 writes the empty JSON seed programmatically (an empty golden would trip the PRISTINE classification). |

## Restorable workflows (`golden/workflows/`)

`.claude/workflows/<name>.js` is a **Claude-Code-only** artifact class (the
`Workflow` primitive has no Copilot equivalent — never mirrored into
`copilot/`). Workflows are **opt-in / billed**. Two tiers exist:

- **Read-only** (all paradigms): write no files, create no branches. Default.
- **Write-capable** (**Noir only**): each agent runs in an isolated worktree,
  commits to a short-lived branch, and exits. The integration master merges
  the branches via `grm-release-phase-merge`. Gated by `meta.tier: 'write-capable'`
  + an explicit Noir paradigm check (fail-closed).

The path convention and read-only safety contract live in
`docs/grimoire/design/release-planning-workflow-design.md`. The write-capable tier
specification lives in `docs/grimoire/design/write-capable-workflow-design.md`.

| Workflow | Purpose |
|---|---|
| `release-planning.js` | Read-only multi-agent fan-out alternative to the `grm-release-planning` skill: parallel source readers + per-item sizing → a work-items report draft. Claude-Code-only. |
| `source-to-design-docs.js` | Read-only analysis fan-out for the `grm-source-to-design-docs` skill: parallel per-module readers → candidate manifest → design-doc content proposals; stops at the user-confirmation gate (writes nothing). Claude-Code-only. |
| `write-capable-example.js` | Canonical reference write-capable workflow (Noir only): isolated-worktree parallel agents, per-agent feature branches, conflict-map output, and all three execution variants (Efficient / Fast / Careful-Serial). Copy and adapt for any write-capable fan-out step. Claude-Code-only. |

## Restorable MCP servers (`golden/mcp-servers/`)

Bundled stdlib MCP servers (zero third-party deps) and their shared runtime,
restored to `.claude/mcp-servers/` and registered via the root `.mcp.json` (the
`grimoire-issue-tracker` + `grimoire-release` + the three v3.28 ops servers
`grimoire-status`/`grimoire-recipe`/`grimoire-environment` entries; merge-safe).
Each server is a thin adapter over an existing engine — restoring the server
alongside its engine keeps the MCP surface and the CLI fallback in lock-step.

| File | Purpose |
|---|---|
| `lib/mcp_runtime.py`           | Reusable `McpServer` base — hand-rolled stdio JSON-RPC, stdlib-only. The template every server subclasses. |
| `issue-tracker/server.py`      | First instance: eight-tool adapter over `issue_tracker.py` (list/get/search/create/comment/update/close/label). |
| `grimoire-release/server.py`   | Second instance (v3.27): seven-tool adapter over the `release_plan.py` engine + `noir_loop_state.py` (`get_ledger`/`tick_rows`/`merge_queue`/`merge_preflight`/`plan_phase`/`read_loop_state`/`advance_loop`). File-write-only — never runs git mutations. Design: `docs/design/grimoire-release-server-design.md`. |
| `grimoire-status/server.py`    | v3.28 ops instance (audit rank 2): one-tool read-only adapter over `status-broker/project_status.py` (`get_status` → structured project overview). No writes, no git, no tracker calls. Design: `docs/design/status-broker-design.md`. |
| `grimoire-recipe/server.py`    | v3.28 ops instance (audit rank 5): three-tool adapter over `build-recipe/recipe.py` (`list_targets`/`dry_run`/`run_recipe`). Recipes stay project-defined in `.claude/recipes.json`; adds no new execution authority. Design: `docs/design/build-recipe-interface-design.md`. |
| `grimoire-environment/server.py` | v3.28 ops instance (audit rank 8): three-tool read-only adapter over `environment-manager/env_probe.py` (`list_processes`/`port_status`/`instance_urls`). Lifecycle (`kill`/`start`) deliberately excluded — stays per-action-authorized agent-side. Design: `docs/design/environment-manager-design.md`. |

## Meta-skills (not self-restoring)

| Skill | Purpose |
|---|---|
| `grm-workflow-bootstrap` | Guided install/restore + project-specific interview. |
| `grm-sync-from-source`   | Pull skills/hooks/docs from a source project into this scaffolding. |

## Project-config placeholders set by the interview

These tokens are filled **once per project** by `grm-workflow-bootstrap`.
Everything else in `{curly braces}` is a **runtime template token** that
agents substitute per-use — the interview must never touch those.

| Token | Lives in | Filled with |
|---|---|---|
| `{test-command}`  | CLAUDE.md | e.g. `npm test`, `pytest`, `cargo test`. **Not** in `release-phase` / `release-phase-merge` (#465) — those call `python3 .claude/skills/grm-build-recipe/recipe.py test` directly, resolving from `.claude/recipes.json` at run time instead of carrying a bootstrap-filled literal. |
| `{build-command}` | CLAUDE.md | e.g. `npm run build`, `make`, `cargo build`. **Not** in `release-phase` / `release-phase-merge` (#465) — same `recipe.py build` resolution as above. |
| `{release-command}` | CLAUDE.md, project-release, version-design.md | e.g. `npm version minor`, `just release` |
| `{path/to/version/file}` | version-design.md §3 | e.g. `package.json`, `Cargo.toml` |
| `{field name or format}` | version-design.md §3 | e.g. `"version"`, `[package] version` |
| doc-location map rows | repo-reference §map | project's real `docs/design/*` paths |
| `PROTECTED_RE` branches | hooks/protected-branch-guard.sh | integration + release branch names |
| roadmap first entry | docs/roadmap.md | first version + theme |
| `{design-language-source}` | `docs/design/ux/design-language.md` front-matter `source:` field | `upstream` (default) or `local`; filled only for GUI projects (answer = "Yes") |
| `{design-language-source-url}` | `docs/design/ux/design-language.md` front-matter `source-url:` field | upstream repo URL; default `https://github.com/rhohn94/design-language` (CONFIRM-pending placeholder — verify before first adapt run); filled only for GUI projects (answer = "Yes"). v1.13+: already seeded by Step 2.5 — interview confirms/overrides rather than writing from scratch. |
| `{ux-demo-stack}` | `docs/design/ux/design-language.md` §Design preamble ("Primary stack: …" note) | project's primary GUI framework/stack (e.g. "SwiftUI", "React", "Qt Widgets"); consumed by `grm-ux-demo-build`; filled only for GUI projects (answer = "Yes") |

| `commands.build` | `.claude/grimoire-config.json` `commands.build` | Justfile build recipe command; `null` if left blank (v3.53) |
| `commands.run` | `.claude/grimoire-config.json` `commands.run` | Justfile run recipe command; `null` if left blank (v3.53) |
| `commands.deploy` | `.claude/grimoire-config.json` `commands.deploy` | Justfile deploy recipe command; `null` if left blank (v3.53) |

**Runtime tokens — never filled by the interview:** `{feature}`,
`{feature-name}`, `{branch}`, `{branch-name}`, `{short-sha}`, `{model}`,
`{effort}`, `{file}`.
