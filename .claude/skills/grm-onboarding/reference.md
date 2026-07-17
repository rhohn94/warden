# Onboarding — reference
Loaded on demand by `SKILL.md`.

## §6 — Config schema notes (forward compatibility)

**`work-paradigm`** is active in v1.6 (schema-version 2). The `in-development`
key has been removed for this field. `§3.1` (work-paradigm-switch) performs the
schema migration from v1 → v2 automatically on first invocation.

**`workflow-variant`** is **active** as of v1.11 (graduated in E1 — the
execution-strategy dial; no schema-version bump, mirroring the
model-effort-profile graduation). Onboarding writes `workflow-variant.value`
with **no** `in-development` key and §3.3 activates it via
`grm-workflow-variant-switch`. Preset set: `{Fast, Efficient, Cheap-Slow}`, default
`Efficient`. A legacy config carrying `in-development: true` or the retired
`Careful-Serial` value is repaired by the switch skill (drop the flag; migrate
`Careful-Serial` → `Cheap-Slow`). Absent/unset → the integration master
defaults to `Efficient`.

**`model-effort-profile`** is **active** as of v1.10 (schema-version 3, added
in v1.9 and graduated in v1.10/P1). Onboarding writes
`model-effort-profile.value` with **no** `in-development` key and §3.2
activates it via `grm-model-effort-profile-switch`. Absent/unset → the resolver
uses the registry `default-profile` (`Medium`), so old configs are
forward-compatible.

**`grm-issue-tracker`** is **active** as of v1.12 (I2/I3). The block is **optional
and additive**: onboarding writes it only when the user chooses a non-roadmap
provider (Step 6). **Absent/unset** → the abstraction synthesizes a single
`roadmap` tracker (§5.2 of `issue-tracker-design.md`) — identical to today's
behaviour, zero config changes for existing projects. Schema-version stays at 3
(no bump — same graduation precedent as `model-effort-profile` and
`workflow-variant`). §3.4 activates the block via `grm-issue-tracker-switch`
(pure-data write, no file-swap).

**`release-phase-model`** is **active** as of v1.23. The block is **additive**:
onboarding writes `release-phase-model.value` (default `Default`; `Auto` only
under Noir) and §3.5 activates it via `grm-release-phase-model-switch` (pure-data
write, no file-swap). **Absent/unset** → the integration master defaults to
`Default` (today's spawn_task pipeline) — identical to existing behaviour, zero
config changes for existing projects. Schema-version stays at 3 (no bump — same
graduation precedent as `model-effort-profile`, `workflow-variant`, and
`grm-issue-tracker`). `Auto` is Noir-only and fails closed (design
`release-phase-model-design.md` §Noir-only guard).

Forward-compat rules (for readers):
- `schema-version: 1` (or missing): `work-paradigm` is `in-development`; treat
  as advisory. Do not activate paradigm switching — the installer has not run
  yet. Map v1 aliases: `Autonomous` → `Noir`, `Collaborative` → `Weiss`.
- `schema-version: 2`: `work-paradigm.value` is active canonical;
  `model-effort-profile` absent → resolver defaults to `Medium`.
- `schema-version: 3`: `model-effort-profile.value` is active (resolver reads it
  live, no file-swap); `workflow-variant.value` is also active as of v1.11 (the
  integration master reads it live; a legacy `in-development` flag or
  `Careful-Serial` value is repaired by `grm-workflow-variant-switch`). No version
  bump rode on either graduation. `grm-issue-tracker` absent → abstraction defaults
  to a single `roadmap` tracker (no version bump, no behaviour change).

---

## §6.5 — Baseline-roadmap seeding (runs after §5, before §7)

After sentinel removal (§5) and **before** the first-release-planning bridge
(§7), seed the adopting project's `docs/roadmap.md` with the
**framework-required** baseline capabilities so they are planned by the bridge
and cannot be silently dropped during scope-trimming. Design authority:
`docs/grimoire/design/onboarding-design.md` §8.

This step **reads** the maintained, versioned source list
`.claude/skills/grm-onboarding/baseline-requirements.md` (a sibling of this file) —
do **not** hard-code the capability rows here; the source file is the single
point of maintenance.

### 6.5.1 Determine project shape

Derive the shape from the captured config and `grm-workflow-bootstrap` answers:

- **GUI** — GUI-presence answer is `yes` (§1 step 4 / §2 inference).
- **Service** — a long-running networked process (server / API / daemon),
  inferred from the project description / build commands.
- **Library** — a reusable package with no launch path of its own.
- **CLI** — a command-line program.

A project may match more than one shape (e.g. a GUI that is also a service);
seed every matching shape's rows plus the all-shapes rows.

### 6.5.2 Select and seed the rows

1. Read `baseline-requirements.md`; note its `baseline-version: N` (line 1).
2. Take **all-shapes** rows unconditionally, plus the rows whose shape
   condition matches §6.5.1.
3. Write them into `docs/roadmap.md` under a dedicated, clearly-labelled
   section, each row tagged `[framework-required]` and carrying its stable
   capability key in an HTML comment for idempotent matching:

```
## Framework-required (baseline)
<!-- seeded by onboarding from baseline-requirements.md (baseline-version: 1) -->
- Runnable test command [framework-required] <!-- key: test-command -->
- Smoke/build command [framework-required] <!-- key: smoke-build-command -->
- Non-interactive launch path [framework-required] <!-- key: non-interactive-launch -->
- Visual-inspection CLI (headless screenshot / render-to-file / DOM-or-scene dump / automation endpoint) — see UX tier (`grm-design-language-adapt`, `grm-ux-demo-build`) [framework-required] <!-- key: gui-visual-inspection-cli, shape: GUI -->
```

(The example shows the all-shapes rows plus a GUI row; seed only the rows whose
shape matches the project.)

### 6.5.3 Tagging contract

The `[framework-required]` tag is the contract that `grm-release-planning` /
`grm-release-agreement` honour: these rows may be **scheduled** into a version but
must **not** be **removed** during scope-trimming. The
`## Framework-required (baseline)` section keeps them **distinct** from the
user's own roadmap items (which live under their normal headings, untagged), so
trimming user scope can never drop a framework requirement. The HTML comment
records the `baseline-version` for idempotent re-seeds and the per-row `key:`
for additive matching.

### 6.5.4 Additive, idempotent re-seed

Seeding is **additive and idempotent**:

- A row already present (matched by its stable `key:`) is **not** duplicated on
  a re-run.
- The `baseline-version` line lets a later run (or a `grm-sync-from-upstream`
  reconciliation) add only **newly-introduced** rows when the framework bumps
  the baseline version.

### 6.5.5 GUI cross-reference to the UX tier

The GUI row does not duplicate the UX-design-language workflow — it
**cross-references** it (`grm-design-language-adapt` → `docs/design/ux/design-language.md`,
`grm-ux-demo-build` → `ux-demo/`). The visual-inspection CLI is the *agent-facing*
verification surface; the UX tier owns the *design* surface. For a GUI-deferred
project, `grm-repo-init` already adds a `## Backlog` UX row; this baseline row
complements it without colliding.

### 6.5.6 Ordering (F3 seeds, then F1 plans)

This seeding step runs **before** the §7 bridge so the framework-required rows
are present when the bridge's `grm-release-planning` proposes the first plan — the
load-bearing F3-then-F1 runtime order from
`docs/grimoire/design/onboarding-design.md` §8.7. The bridge then plans *from* the
seeded roadmap; if seeding is skipped or the roadmap is unseeded, the bridge
still proceeds gracefully (§7.4).

### 6.5.7 Required-feature catalog filing (family-neutral)

**Runs for every project family**, not just web apps (generalized in v3.97,
#413 — through catalog-version 8 this step only fired for
`web-app.value == "yes"`). After the baseline-roadmap rows are seeded
(§6.5.2), trigger the required-feature catalog filing hand-off:

1. **Resolve the family.** `web-app.value == "yes"` resolves family `web`
   directly (the config already declares it). Otherwise reuse
   `grm-quick-start-template` §1's own detection (the `grm-workflow-bootstrap`
   signal table) rather than a second detector — this step never invents its
   own family classifier. If no signal is available at all (a from-scratch
   repo with no scaffold and `web-app.value: "no"`), skip this step for now;
   it can be re-run later via `grm-required-feature-catalog` directly once the
   project's family is known (e.g. after `grm-quick-start-template` runs).
2. Plan: `python3 .claude/skills/grm-required-feature-catalog/catalog_filing.py
   plan --root . --family <resolved-family>`.
3. For each `file` / `file-blocked` / `activate` result, spawn a **Reporter**
   (`grm-agent-reporter` skill) to file one `Grimoire-Requirement`-tagged
   ticket via `grm-feedback-to-issue`, using the title, body, labels, and
   `audience: "internal"` from the catalog entry (`file-blocked` also gets the
   `blocked-on-upstream` label and the entry's `activation-event` text).
   `ensure_label` is called automatically before filing (WEB-5). Any
   `manual-review` result is evaluated by this agent per the entry's own
   "Detect." guidance. Then `catalog_filing.py record --root . --key <key>
   --status {filed,blocked-on-upstream}` for each entry acted on.

This is re-runnable: a re-run of onboarding (or a direct
`grm-required-feature-catalog` invocation later) files only new/changed
entries per `catalog_filing.py`'s persisted state
(`.claude/required-feature-catalog-state.json`) — see
`grm-required-feature-catalog/SKILL.md` §1 for the full action-code table. If
the project's issue tracker is not yet configured (roadmap default), the
Reporter files into the roadmap backend — no special case needed.

Design authority: `docs/grimoire/design/web-app-support-design.md` §5.2 (filing flow).
Catalog source: `.claude/skills/grm-required-feature-catalog/required-feature-catalog.md`.

---

## Anti-patterns

- Running `git init` silently on the interactive path — the §0.2 confirmation
  is mandatory; only the trigger (`RUN NON-INTERACTIVE ONBOARDING` / legacy
  `SKIP ONBOARDING`) carries implied consent, and even then the action must
  be announced.
- Re-running `git init` or making a second initial commit when a repo already
  exists — §0 is skipped wholesale in the idempotent case (§0.4).
- Creating `dev` / `version/*` during §0 — onboarding produces only "a repo on
  `main` with one commit"; `grm-repo-init` (§4) owns the branch model.
- Defaulting the project name to "Grimoire" — that is the scaffolding's name,
  not the adopting project's.
- Batching unrelated interview questions in a single `AskUserQuestion`.
- Calling `grm-repo-init` when `main` + `dev` already exist — check first.
- Running sentinel removal before `grm-workflow-bootstrap` completes — removal is
  always the final step.
- Using `sed -i '1d'` blindly — confirm line 1 matches before deleting.
- Writing `workflow-variant` with an `in-development` flag, or treating the
  execution strategy as preview/not-yet-active — the field graduated in v1.11
  (E1); it is active and carries only `value`, and §3.3 activates it via
  `grm-workflow-variant-switch`.
- Persisting `Careful-Serial` in `workflow-variant.value` — it is migrated to
  `Cheap-Slow` (the project preset set is `{Fast, Efficient, Cheap-Slow}`).
- Writing `work-paradigm.in-development` at all in a v2 config — this key does
  not exist in schema-version 2; the switch skill removes it during migration.
- Writing `model-effort-profile` with an `in-development` flag — the field
  graduated in v1.10 (P1); it is active and carries only `value`.
- Treating the model/effort profile as a preview/not-yet-active field — it is a
  real, active choice; §3.2 activates it via `grm-model-effort-profile-switch`.
- Deriving one dial's value from another (e.g. silently forcing `Autonomous`
  under Noir, or setting the execution strategy from the paradigm) — the three
  dials (work-paradigm × execution-strategy × model-effort-profile) are
  **independent**; none auto-derives another. At most a one-line non-binding
  hint is allowed (`execution-profiles-design.md` §A/§F.2).
- Skipping §3.2 or §3.3 activation, or running them before §3 writes the config
  — the switch skills read the written `value` (or their argument) and must run
  after the config exists.
- Auto-running the first-release-planning bridge under Supervised or Weiss —
  those paradigms **prompt-offer** (§7.1); only Noir auto-kicks-off.
- Prompt-offering the bridge under the non-interactive trigger for
  Supervised/Weiss — there is no interactive session; it is a no-op with a
  pointer (§7.2). Only Noir auto-runs under the trigger.
- Hard-coding the baseline capability rows in this skill — §6.5 always reads
  them from `baseline-requirements.md` (the single point of maintenance).
- Seeding baseline rows under the user's own roadmap headings, or omitting the
  `[framework-required]` tag — they must live under the dedicated
  `## Framework-required (baseline)` section so scope-trimming cannot drop them
  (§6.5.3).
- Duplicating an already-seeded baseline row on re-run — seeding matches by the
  stable `key:` and is additive/idempotent (§6.5.4).
- Running the §6.5 seeding step after the §7 bridge — seeding must run first so
  the bridge plans from a populated roadmap (§6.5.6 / §8.7).
- Re-implementing planning logic in the bridge — it calls `grm-release-planning` /
  `grm-release-agreement` / `grm-integration-master` as-is (§7).
- Blocking onboarding completion when the roadmap is unseeded — the bridge
  tolerates a missing/unseeded roadmap gracefully (§7.4).
- Running the bridge before sentinel removal or before roadmap seeding — the
  bridge is always the final phase.
- Writing an `grm-issue-tracker` block when the user chose `roadmap` (the default) —
  absence is the forward-compat default; writing an explicit `roadmap` block is
  harmless but unnecessary noise. Omit it.
- Calling `grm-issue-tracker-switch` when the roadmap default was selected — §3.4 is
  skipped entirely in the roadmap case; do not call the skill.
- Calling `grm-issue-tracker-switch` before §3 writes the config — the switch skill
  reads and writes the config file; it must run after §3.
- Accepting a `github` provider without a `repo` value — provider `github`
  requires a non-null `owner/repo` string; if the user left it blank, either
  re-prompt or defer to a later `grm-issue-tracker-switch` call.
- Bumping `schema-version` when writing the `grm-issue-tracker` block — the block is
  additive at schema-version 3; no version bump (mirrors the `model-effort-profile`
  and `workflow-variant` graduation precedent).
- Deriving the issue-tracker choice from any other dial (paradigm, execution
  strategy, model/effort profile) — the `grm-issue-tracker` block is a fourth
  independent config entry; it is orthogonal to all three dials.
- Offering `Auto` for the release-phase model under a non-Noir paradigm, or
  writing `release-phase-model.value: "Auto"` outside Noir — `Auto` is Noir-only
  and fails closed (§Step 7 / §3.5); under Supervised/Weiss the dial is fixed at
  `Default`.
- Bumping `schema-version` when writing the `release-phase-model` block — the
  block is additive at schema-version 3 (same precedent as `model-effort-profile`,
  `workflow-variant`, and `grm-issue-tracker`).
- Skipping §3.5 activation, or running it before §3 writes the config — the
  switch skill reads the written `value` (or its argument) and must run after
  the config exists.
- Inventing a second family/profile detector inside §6.5.7 instead of reusing
  `grm-quick-start-template` §1's detection — this step trusts that single
  source of truth for the resolved family.
- Filing catalog entries without deduplicating against existing tagged issues
  first — always check `Grimoire-Requirement`-tagged issues (open and closed)
  before filing, so re-runs are no-ops (§6.5.7); `catalog_filing.py`'s
  persisted state is a planning aid, not a replacement for this check.

## Default label taxonomy seeding (v1.31, #69)

At Step 6, **for a GitHub tracker only**, offer to seed the recommended
label/audience taxonomy (`docs/grimoire/design/issue-label-taxonomy.md`): type × area ×
priority labels + the `audience` routing. Idempotent — create each label if
absent, never delete/recolor an existing one. **No-op for the `roadmap`
provider.** Seed through the issue-tracker abstraction's `label` operation, not
raw `gh`, so routing + caching are honored.
### 7.1 Paradigm-conditional behaviour

Branch on `work-paradigm.value` (active canonical at schema-version 2):

| Paradigm | Bridge behaviour |
|----------|------------------|
| **Noir** (Autonomous) | **Auto-kick-off.** As integration master, propose an initial roadmap direction, run `grm-release-planning`, lock a first plan via `grm-release-agreement`, and cut `version/{X.Y}` — all **before any building**, without per-step user confirmation. Surface the locked plan to the user as a milestone for review. |
| **Supervised** (default) | **Prompt-offer.** Ask once via `AskUserQuestion`: "Setup is complete. Would you like me to draft and lock a first release plan now, or stop here?" Only on an affirmative answer run the same `grm-release-planning` → `grm-release-agreement` → cut-`version/{X.Y}` sequence, each step still surfacing its normal Supervised confirmation. |
| **Weiss** (Collaborative) | **Prompt-offer**, same as Supervised, but framed as user-led: offer to *assist* with first-release planning; the user drives the roadmap and scope decisions. |

The version label for the first plan (`v0.1` vs `v1.0`) is a planning decision:
Noir picks a sensible default (recommend `v0.1` for a greenfield project with no
shipped surface) and notes it in the proposed plan; the prompt-offer paradigms
surface the choice to the user.

### 7.2 `RUN NON-INTERACTIVE ONBOARDING` interaction

The non-interactive path (§2 — triggered by `RUN NON-INTERACTIVE ONBOARDING`,
the legacy `SKIP ONBOARDING` literal, or a committed kickoff file per §2.0) is
non-interactive; the bridge respects the inferred paradigm:

- **Noir inferred** → the bridge **auto-runs** exactly as in §7.1 (the whole
  point of the non-interactive path is full hands-off setup *including* the
  first-plan lock).
- **Supervised or Weiss inferred** → the bridge is a **no-op** (there is no
  interactive session to prompt-offer into). Stop after the roadmap is seeded
  and print a one-line pointer:
  > "Run `grm-release-planning` when you're ready to scope your first release."

### 7.3 Where it hooks in the sequence

The bridge runs after the baseline-roadmap seeding step (§6.5). If seeding was
skipped or the roadmap carries no `[framework-required]` rows, the bridge still
runs as the final phase and handles the unseeded roadmap per §7.4.

### 7.4 Tolerating an unseeded roadmap

If `docs/roadmap.md` is missing or carries no `[framework-required]` baseline
rows (e.g. F3 has not yet seeded it), the bridge does **not** fail:

- **Noir** — proceed with `grm-release-planning` from whatever roadmap content
  exists (or an empty roadmap), proposing the integration master's initial
  direction; note in the proposed plan that the framework-required baseline was
  not present.
- **Supervised / Weiss** — the prompt-offer still applies; if the user declines,
  stop normally. If they accept, run `grm-release-planning` against the available
  roadmap.

The bridge never blocks onboarding completion on the roadmap being seeded.

---

## §1 — Interactive interview

### 1.1 Greeting

Before asking any questions, acknowledge the fresh scaffold:

> "I see this is a fresh Grimoire project. Let me walk you through setup
> first."

Defer the rest of the user's original prompt until onboarding completes.

Then run the git-repo-init prerequisite (§0) — with its `AskUserQuestion`
confirmation (§0.2) — before asking the interview questions below.

### 1.2 Interview questions (sequential, one at a time)

Use `AskUserQuestion` for each step. Never batch unrelated questions.
Offer a default for every question.

#### Step 1 — Project name

> "What is the name of your project?"

- Default: the repository directory basename (`git rev-parse --show-toplevel`
  → `basename`).
- Do **not** default to "Grimoire" — that is the scaffolding's own name, not
  the adopting project's name.
- If the directory name is ambiguous or empty, offer `"My Project"`.

#### Step 2 — Work paradigm

> "Choose your Work Paradigm:
>   - **Supervised** (default) — you confirm each major step; agent assists.
>   - **Weiss** (Collaborative) — you lead all design decisions; agent
>     researches and assists.
>   - **Noir** (Autonomous) — agent leads design, planning, and integration;
>     you review milestones.
>
> The selected paradigm activates immediately during setup."

- Default: `Supervised`.
- Accepted values: `Supervised`, `Weiss`, `Noir` (canonical); also accept
  `Collaborative` (alias for Weiss) and `Autonomous` (alias for Noir),
  case-insensitive. Resolve aliases to a canonical *internal* understanding,
  but **store the schema-version-1 alias form** (`Supervised` / `Autonomous` /
  `Collaborative`) in the config — the `grm-work-paradigm-switch` skill migrates to
  canonical (`Weiss` / `Noir`) at schema-version 2.
- If the user's answer is not one of the accepted values, re-prompt once,
  then fall back to `Supervised`.

#### Step 3 — Execution strategy *(active)*

This is a **real, active** choice (the `workflow-variant` field graduated in
v1.11, E1) — not a preview. It is the **execution-strategy** dial: *how work is
dispatched* (fan-out width and isolation mode). It is **independent** of the
work paradigm (Step 2) and the model/effort profile (Step 5) — none derives
from another. Frame it via the **speed / quality / cost triangle** (you can
prioritize at most two of the three):

> "Choose your execution strategy (how work is dispatched — independent of your
> paradigm and your model/effort profile):
>   - **Efficient** (default) — balanced; parallel with low waste. The middle
>     of the speed/quality/cost triangle.
>   - **Fast** — prioritizes **speed**: maximum parallel fan-out, minimum
>     wall-clock time (you pay for duplicated reads).
>   - **Cheap-Slow** — prioritizes **cost**: low fan-out + small batches; pairs
>     naturally with a cheaper model/effort profile. Sacrifices speed.
>
> This activates immediately during setup; switch it later with
> `grm-workflow-variant-switch`."

- Default: `Efficient`.
- This is an **independent** dial — do **not** derive its value from the chosen
  paradigm (Step 2) or model/effort profile (Step 5). Any combination is valid.
- Accepted values: `Fast`, `Efficient`, `Cheap-Slow` (case-insensitive; also
  accept the legacy `Careful-Serial`, which the switch skill migrates to
  `Cheap-Slow` — see `grm-workflow-variant-switch` §1.1).
- If the user's answer is not one of the three values, re-prompt once, then
  fall back to `Efficient`.
- The chosen value is written to `workflow-variant.value` in §3 (active — **no**
  `in-development` flag) and activated in §3.3 via `grm-workflow-variant-switch`.

#### Step 4 — GUI presence *(+ web-app fact, v3.26)*

> "Does this project have (or will have) a user interface?
>   - **Yes** — it has or will have a GUI/web UI now.
>   - **Not yet** — planned but not started (default).
>   - **No** — headless / CLI / API only."

- Default: `not yet`.
- Pass the captured answer to `grm-workflow-bootstrap` in §4 so it does not
  re-ask the same question.

**Web-app fact (extends this step — it is not a new step).** Per
`web-app-support-design.md` §2.2, the `web-app` config block keys on a narrower
fact than the GUI boolean: *is this a browser-delivered, server-hosted web app?*
A native desktop GUI, a TUI, and a web app are all "GUI = Yes"; only the
browser-web slice is `web-app = yes`.

- **Only when the GUI-presence answer is `Yes`** and the `grm-workflow-bootstrap`
  Step 3 Q9 evidence names a **web slice** — rows 8–13/15 (browser/meta web
  frameworks), corroborated by rows 17–18, **or** a server web framework
  (Flask/Django/Express/FastAPI/Rails/Gin) serving HTML/templates, **or** row
  21 (Rust `axum`/`actix-web`/`rocket` + a view-layer peer) — pre-fill
  `web-app = yes` with the detected `stack` and **surface the evidence**, then
  ask the user to **confirm or change** via `AskUserQuestion`. Pre-selection
  follows the Q9 confidence levels: High → pre-select "Yes (web app)";
  Medium → pre-select but phrase as a question; Low/none → cold question.
- A `Not yet` / `No (headless)` answer, **or** a `Yes` with a non-web stack
  (native/TUI — Q9 rows 1–7/9/14/16, or headless rows 19–20), leaves the
  `web-app` block **absent** (the default; absence ≡ `value: "no"`).
- **Detection never writes the block without the confirm** — it only sets the
  `AskUserQuestion` default. The confirmed answer (not the detected guess) is
  what persists: a confirmed web answer is written by `grm-workflow-bootstrap` in §4
  (its Q9 persistence step); a non-web confirmed answer writes nothing.
- The block is **additive with no schema bump** — record it only on an
  affirmative web confirmation (§3 carries it through alongside the other
  blocks; it is never synthesized by a default-fill).

#### Step 5 — Model/effort profile (cost posture)

This is a **real, active** choice (the `model-effort-profile` field graduated
in v1.10, P1) — not a preview. The resolver reads it live at every work-item
dispatch to pick each subagent's `{model, effort}` tier. Ask:

> "Choose your model/effort profile (cost posture — how aggressively work is
> routed to higher-capability models):
>   - **Medium** — balanced; Opus for large/review work, Sonnet for the
>     middle, Haiku for trivial.
>   - **High Effort** — quality-first; Opus from medium upward.
>   - **Efficient** — parallel, low-waste; Sonnet-heavy with Opus reserved for
>     large/review.
>   - **Low Effort** / **Eco/Budget** — cost-first; no Opus, Sonnet ceiling.
>   - **Autonomous** — Noir-tuned for fan-out; Sonnet ceiling for build work,
>     Opus reserved for review.
>
> This activates immediately during setup; switch it later with
> `grm-model-effort-profile-switch`."

- **Default: `Medium`** (the registry `default-profile`) for **every**
  paradigm. This is an **independent** dial — it does **not** auto-derive from
  the work paradigm (Step 2) or the execution strategy (Step 3).
- **Optional one-line hint only** (non-binding — never a silent force, never a
  paradigm-conditional default): you may add a single advisory line such as
  > "Teams running Noir often pair **Autonomous + Cheap-Slow** for cheap
  > autonomy, but any combination of the three dials is valid."

  Do **not** change the highlighted default based on the paradigm; the user
  freely picks any profile.
- Accepted values: `Medium`, `High Effort`, `Low Effort`, `Efficient`,
  `Autonomous`, `Eco/Budget` (case-insensitive; also `noir` → `Autonomous`).
  The canonical set is the keys of `profiles` in
  `.claude/model-effort-profiles.json` — that registry is the source of truth.
- If the user's answer is not an accepted value, re-prompt once, then fall back
  to `Medium`.
- The chosen value is written to `model-effort-profile.value` in §3 (active —
  **no** `in-development` flag) and activated in §3.2 via
  `grm-model-effort-profile-switch`.

#### Step 6 — Issue tracker *(active, v1.12)*

This is a **real, active** choice (the `grm-issue-tracker` block added in v1.12/I2).
It is **independent** of the other dials — never derived from any of them. Ask:

> "Choose your issue tracker:
>   - **Roadmap** (default) — issues live in `docs/roadmap.md` `## Backlog`.
>     Zero network, no GitHub required.
>   - **GitHub** — issues live in a GitHub Issues repo (via `gh`). Requires a
>     GitHub repo and `gh` authentication.
>
> You can configure multiple trackers (e.g. internal + external) later with
> `grm-issue-tracker-switch`."

- **Default: `roadmap`.**  When the user selects `roadmap` (or accepts the
  default): **do not write an `grm-issue-tracker` block to config at all** — absence
  is the forward-compat default, identical to today's behaviour. §3.4 is
  skipped entirely.
- **Accepted values:** `roadmap`, `github` (case-insensitive).
- **If the user answers `github`:** ask one follow-up sub-question within the
  same conversational turn (not a separate `AskUserQuestion` call):

  > "Enter the GitHub repo for issues (`owner/repo`). Leave blank to configure
  > later."

  Capture the repo string; store `null` if blank.

  Then ask if they want a separate external-facing tracker. If the user says yes
  (or uses keywords `internal`, `external`, `two repos`, `separate`):

  > "Enter the external-facing issues repo (`owner/repo`) for user-reported
  > issues. Leave blank to use the same repo for both."

  If a second repo is provided, this produces a two-tracker config (internal +
  external — see `issue-tracker-design.md §9` for the full schema). If blank,
  a single-tracker GitHub config is used.

- **If the user's answer is not one of the accepted values**, re-prompt once,
  then fall back to `roadmap`.
- The chosen value (if non-roadmap) is written to the `grm-issue-tracker` block in
  §3 and activated in §3.4 via `grm-issue-tracker-switch`. Full design authority:
  `docs/grimoire/design/issue-tracker-design.md §9`.

#### Step 7 — Release-phase model *(active, v1.23)*

This is the **release-phase-model** dial: *how the integration master executes
an agreed plan*. It is **independent** of the other dials. The `Auto` value is
**Noir-only** (design's open-questions decision), so present `Auto` as a choice
**only when the paradigm chosen in Step 2 resolves to Noir**:

- **Paradigm is Noir** — offer both values:
  > "Choose your release-phase model (how the integration master executes a
  > locked plan):
  >   - **Default** (default) — dispatch each work item as a separate session
  >     (spawn_task), merging each branch. Today's pipeline.
  >   - **Auto** — drive the whole release inside the master's own session via a
  >     write-capable Workflow (Noir only); you review only before release.
  >
  > Switch it later with `grm-release-phase-model-switch`."
- **Paradigm is Supervised or Weiss** — the dial is **fixed at `Default`**; do
  **not** present `Auto`. Optionally note: "Auto is available only under Noir."

- **Default: `Default`** for every paradigm (the conservative default preserves
  today's behaviour exactly).
- Accepted values: `Default`, `Auto` (case-insensitive). `Auto` is accepted
  **only** under Noir; under any other paradigm an `Auto` answer is rejected and
  the dial stays `Default`.
- This is an **independent** dial — do **not** derive its value from the
  paradigm (beyond the Noir-only availability of `Auto`), the execution
  strategy, or the model/effort profile.
- The chosen value is written to `release-phase-model.value` in §3 and activated
  in §3.5 via `grm-release-phase-model-switch`. Full design authority:
  `docs/grimoire/design/release-phase-model-design.md`.

---

## §2 — Non-interactive path (`RUN NON-INTERACTIVE ONBOARDING` / legacy `SKIP ONBOARDING`)

### §2.0 — Kickoff-file trigger (committed, non-chat) (v3.94, #430)

A live-chat literal is not the only way to reach this path. **Before**
checking the live prompt text at all, check for a **committed kickoff file**:

1. Look for a root `KICKOFF.md` or `FIRST-RELEASE-PROMPT.md` (repo-root
   only, case-sensitive filename; check `KICKOFF.md` first, then
   `FIRST-RELEASE-PROMPT.md`).
2. If found, read its content. If it contains the trigger literal `RUN
   NON-INTERACTIVE ONBOARDING` (case-sensitive) — or, for backward
   compatibility, the legacy literal `SKIP ONBOARDING` — treat the **file's
   content** as the *effective first prompt* for every inference rule in the
   table below, concatenated after the live chat prompt (so a project name
   or other detail the user typed live is still available to the inference
   rules; the file supplies the trigger and whatever else it carries).
3. A bare "go", an empty prompt, or literally anything else typed into chat
   is now sufficient to trigger the full non-interactive bootstrap — the
   trigger lived in the commit, not the keystroke.
4. If no kickoff file exists, or one exists but does not contain the
   trigger, fall through to the ordinary live-prompt check below (no
   behavior change from before this section existed).

This closes the failure mode the feature addresses: a kickoff artifact
committed to a fresh scaffold (e.g. by a Project Manager laying down a
fleet-wide convention across several repos) was previously **inert** — the
trigger, present in the file, was never read unless a human happened to
retype it live in chat. Two fleet repos sat 12 days at zero code this way.
§2.0 makes the committed file self-executing on the very next prompt, of any
shape, because the sentinel-detection instruction (`CLAUDE.md` line 1, and
this skill's entry point) now checks the file **first**, before the prompt.

**Idempotency / re-trigger safety:** this check runs only while the sentinel
(`CLAUDE.md` line 1) is still present — sentinel removal (§5) is the final
step of both paths, so an already-onboarded project's `CLAUDE.md` never
re-arms this check, regardless of whether a stray `KICKOFF.md` /
`FIRST-RELEASE-PROMPT.md` is later left behind in the tree. A leftover
kickoff file in an onboarded project is inert prose, not a live trigger.

**Rename rationale:** `RUN NON-INTERACTIVE ONBOARDING` names the thing to
*do*; the legacy `SKIP ONBOARDING` literal named the thing to skip, and was
read by some agents as "skip the bootstrap entirely" rather than "run the
non-interactive path" — the opposite of the intent. Both literals are
accepted, case-sensitive, everywhere this section says "the trigger."

### §2.1 — Live-prompt / effective-prompt trigger

When the effective first prompt (§2.0's kickoff-file content, or otherwise
the live chat prompt) contains the trigger — `RUN NON-INTERACTIVE ONBOARDING`
or the legacy `SKIP ONBOARDING` (case-sensitive, any position) — first run
the git-repo-init prerequisite (§0) with implied-consent-and-announce
semantics (§0.2), then bypass the interview and infer config from the
effective prompt text using these rules:

| Field | Inference rule | Default |
|-------|----------------|---------|
| `name` | Quoted string after `name:` or `project:` in the prompt (e.g. `name: "Acme"`, `project: Acme`). Else: `basename $(git rev-parse --show-toplevel)`. Else: `"My Project"`. | `"My Project"` |
| `work-paradigm.value` | First case-insensitive match of `Supervised`, `Weiss`, `Noir`, `Autonomous`, or `Collaborative` anywhere in the prompt. Store the schema-version-1 **alias form**: `Supervised`, `Autonomous` (also from `Noir`), or `Collaborative` (also from `Weiss`) — `grm-work-paradigm-switch` migrates to canonical at schema-version 2. | `"Supervised"` |
| `workflow-variant.value` | First case-insensitive match of `Fast`, `Efficient`, or `Cheap-Slow` anywhere in the prompt (also accept legacy `Careful-Serial`, which `grm-workflow-variant-switch` migrates to `Cheap-Slow`). Independent of paradigm — do **not** derive from it. Active field — **no** `in-development` flag. | `"Efficient"` |
| `model-effort-profile.value` | First case-insensitive match of `Medium`, `High Effort`, `Low Effort`, `Efficient`, `Autonomous`, or `Eco/Budget` anywhere in the prompt (resolve `noir` → `Autonomous`). If none matched → `Medium`. Independent of paradigm — do **not** derive from it. Active field — **no** `in-development` flag. | `"Medium"` |
| GUI presence | `gui`, `ui`, `interface`, `web`, `app`, `frontend` (case-insensitive) → `yes`. `headless`, `cli`, `api` → `no`. Otherwise → `not yet`. | `"not yet"` |
| `web-app` block | A **browser web-framework** keyword/file signal in the prompt or repo — Q9 rows 8–18 (`react`/`react-dom`, `vue`, `svelte`/`@sveltejs/kit`, `@angular/core`, `solid-js`, `next`/`nuxt`/`@remix-run/*`/`astro`/`gatsby`, `vite`/`tailwind` config) **or** a server web framework (Flask/Django/Express/FastAPI/Rails/Gin) serving views **or** Q9 row 21 (Rust `axum`/`actix-web`/`rocket` + a view-layer peer) → write `web-app: { value: "yes", stack: <detected hint> }`. A native/TUI/headless signal (Q9 rows 1–7/9/14/16/19–20), or **no** web signal → **omit the block entirely** (absence = default ≡ `"no"`). Because this path is non-interactive, inference **is** the answer — there is no confirm step; the block is written only on a positive web signal, so a false positive is bounded to genuinely web-shaped repos. Authority: `web-app-support-design.md` §2.3. | block absent (`"no"`) |
| `grm-issue-tracker` block | First case-insensitive match of `github` in the prompt → write the block with `provider: "github"` and capture an adjacent `owner/repo` pattern as `repo` (null if none found). Keywords `internal` + `external` both present → dual-tracker config (two entries). If only `roadmap` or no tracker keyword: **omit the block entirely** (absence is the forward-compat default). Full inference rules: `issue-tracker-design.md §9.2`. | block absent (roadmap default) |
| `release-phase-model.value` | `Auto` inferred **only** when the prompt matches `Auto` (case-insensitive, near "release"/"phase"/"orchestration") **and** the inferred paradigm is `Autonomous`/`Noir`; otherwise `Default`. Never `Auto` under a non-Noir paradigm (Noir-only guard). Independent of the other dials. | `"Default"` |

After inferring, proceed directly to §3 (write config), §3.1 (activate
paradigm), §3.2 (activate profile), §3.3 (activate execution strategy), §3.4
(activate issue tracker — if non-roadmap inferred; skip if roadmap default),
§3.5 (activate release-phase model), §4 (bootstrap), §5 (remove sentinel), then
confirm:

> "RUN NON-INTERACTIVE ONBOARDING detected. Config written with inferred
> values — review `.claude/grimoire-config.json` and adjust if needed."

(If the legacy `SKIP ONBOARDING` literal was what matched, use that name in
the confirmation instead — echo back whichever literal actually triggered.)

---

## §3 — Write `.claude/grimoire-config.json`

Write (or overwrite) `.claude/grimoire-config.json` with the collected or
inferred values. The schema is defined in `docs/grimoire/design/onboarding-design.md`
§2 (with the schema-evolution note for the post-v1 fields). The file must be
valid JSON matching this structure:

```json
{
  "schema-version": 3,
  "name": "<project name>",
  "work-paradigm": {
    "value": "<Supervised | Autonomous | Collaborative>",
    "in-development": true
  },
  "workflow-variant": {
    "value": "<Fast | Efficient | Cheap-Slow>"
  },
  "model-effort-profile": {
    "value": "<Medium | High Effort | Low Effort | Efficient | Autonomous | Eco/Budget>"
  },
  "release-phase-model": {
    "value": "<Default | Auto>"
  }
}
```

The `release-phase-model` block is **active** (added in v1.23). Write
`release-phase-model.value` with the chosen value (default `Default`; `Auto`
only under Noir — §Step 7). The integration master reads it live at execution
time; §3.5 (`grm-release-phase-model-switch`) validates and activates the value.

The `grm-issue-tracker` block is **optional** — write it only when the user chose a
non-roadmap provider (Step 6). Absence is the forward-compat default (identical
to a single `roadmap` tracker). When present, it sits alongside the four fields
above:

```json
{
  "schema-version": 3,
  "name": "<project name>",
  "work-paradigm": { "value": "Supervised", "in-development": true },
  "workflow-variant": { "value": "Efficient" },
  "model-effort-profile": { "value": "Medium" },
  "release-phase-model": { "value": "Default" },
  "issue-tracker": {
    "trackers": [
      { "name": "default", "provider": "github", "repo": "owner/repo",
        "audience": "internal", "labels": [] }
    ],
    "default-for-filing": "default"
  }
}
```

Full schema for the `grm-issue-tracker` block: `docs/grimoire/design/issue-tracker-design.md §5.1`.

The `web-app` block (v3.26) is **optional and additive** — write it **only**
when Step 4 confirmed an affirmative web answer (or the non-interactive path
inferred a positive web signal, §2). Absence is the default (absence ≡ `value: "no"`), so a
non-web project carries **no** `web-app` key. When present, it sits alongside the
fields above and does **not** bump `schema-version`:

```json
  "web-app": { "value": "yes", "stack": "Flask + HTMX (web)" }
```

`stack` is the verbatim Q9 detection hint (`null` when unknown); `value ∈ {yes,
no}` is the gating fact. The block is data the consumers read live — there is no
activation switch step for it. Full schema: `docs/grimoire/design/web-app-support-design.md §1`.

**Field maturity (mixed lifecycle):**
- **`work-paradigm`** is written with `in-development: true` here, then §3.1
  (`grm-work-paradigm-switch`) migrates it to its active canonical form — this
  preview-then-activate shape is preserved exactly as before.
- **`workflow-variant`** is **active** (graduated in v1.11, E1 — the
  execution-strategy dial): write `value` with **no** `in-development` key. The
  integration master reads it live at dispatch; §3.3 (`grm-workflow-variant-switch`)
  validates and activates the chosen value.
- **`model-effort-profile`** is **active** (graduated in v1.10, P1): write
  `value` with **no** `in-development` key. The resolver reads it live; §3.2
  (`grm-model-effort-profile-switch`) validates and activates the chosen value.

The three dials are **independent** — none auto-derives from another (the
orthogonality contract in `execution-profiles-design.md` §A/§F.2).

**`in-development: true` semantics** (preview fields only):
- Persisted but inert — no current Grimoire code alters behaviour based on
  this value.
- Surfaced as "preview — not yet active" in the interview and any UI.
- Read unchanged by the future feature — when it lands it reads `value`
  directly without re-interviewing and removes (or sets to `false`) the
  `in-development` key.
- Defensive read contract: any reader that sees `in-development: true` must
  not fail if the value is outside its expected set (forward-compat guarantee).

---

## §3.2 — Activate the selected model/effort profile

**Immediately after** activating the paradigm (§3.1), run the
`grm-model-effort-profile-switch` skill with the captured (or inferred)
`model-effort-profile.value` (default `Medium`; the dial is independent of the
paradigm — §F.2).

Unlike §3.1, this performs **no file-swap**: the profile is pure data the
resolver reads live at dispatch time. The skill validates the value against the
registry `.claude/model-effort-profiles.json` and writes
`model-effort-profile.value` to config (dropping any legacy `in-development`
flag). Writing the field **is** the activation. It is idempotent — if the value
is already active it exits early.

**If `.claude/model-effort-profiles.json` does not exist yet** (a freshly
copied scaffold before the registry is restored): the switch skill aborts with
a restore instruction. Log it and continue — the profile activates when
`workflow-bootstrap --restore` brings the registry into place; the resolver
falls back to the registry `default-profile` (`Medium`) until then.

---

## §3.3 — Activate the selected execution strategy

**Immediately after** activating the model/effort profile (§3.2), run the
`grm-workflow-variant-switch` skill with the captured (or inferred)
`workflow-variant.value` (default `Efficient`). This mirrors §3.1/§3.2 in
invocation style and is the **third independent dial** — its value is **not**
derived from the paradigm or the profile.

Like §3.2, this performs **no file-swap**: the execution strategy is pure data
the integration master (`grm-release-phase` / the Noir default-dispatch path) reads
live at dispatch time. The skill validates the value against the preset set
`{Fast, Efficient, Cheap-Slow}` (migrating a legacy `Careful-Serial` to
`Cheap-Slow`, dropping any legacy `in-development` flag) and writes
`workflow-variant.value`. Writing the field **is** the activation. It is
idempotent — if the value is already active it exits early.

**If `.claude/grimoire-config.json` is missing** the switch skill aborts with a
restore instruction; this cannot happen here because §3 just wrote it.

---

## §3.4 — Activate the issue tracker (conditional)

**Only runs when the user chose a non-roadmap provider in Step 6.** If the
roadmap default was selected (or inferred under the non-interactive path),
§3.4 is **skipped entirely** — the `grm-issue-tracker` block is absent from config and the
abstraction's §5.2 fallback provides the default. Do not call
`grm-issue-tracker-switch` for the roadmap-default case.

**Immediately after** activating the execution strategy (§3.3), run the
`grm-issue-tracker-switch` skill with the captured provider and tracker list.

This mirrors §3.1–§3.3 exactly in invocation style:
- **No file-swap.** The issue tracker is pure data; the abstraction reads config
  live at every call. Writing the config is the activation.
- **Idempotent.** If the `grm-issue-tracker` block already matches the requested
  configuration, the skill exits early.
- **Validates** provider ∈ `{roadmap, github, grimoire}` and that `repo` is
  non-null when `provider = "github"`. Invalid input → the skill aborts; do not
  proceed without a valid block.
- **Preserves** all other fields (`schema-version`, `work-paradigm`, etc.).
  Schema-version stays at 3 (no bump — same graduation precedent as
  `model-effort-profile` and `workflow-variant`).

**Non-interactive-path integration:** after inferring the tracker config (§2), call
§3.4 only if a non-roadmap provider was inferred. If roadmap is the inferred
default, §3.4 is a no-op (do not call the skill).

---

## §3.5 — Activate the release-phase model

**Immediately after** activating the issue tracker (§3.4 — or, if the roadmap
default was selected, immediately after §3.3/§3.4's no-op), run the
`grm-release-phase-model-switch` skill with the captured (or inferred)
`release-phase-model.value` (default `Default`).

Like §3.2–§3.4, this performs **no file-swap**: the release-phase model is pure
data the integration master reads live at execution time. The skill validates
the value against the set `{Default, Auto}`, applies the **Noir-only guard for
`Auto`** (refuses `Auto` unless `work-paradigm.value == "Noir"`), and writes
`release-phase-model.value`. Writing the field **is** the activation. It is
idempotent — if the value is already active it exits early.

Because onboarding only offers `Auto` under Noir (§Step 7), the guard never
fires on a well-formed interactive run; it is defence-in-depth for the
non-interactive path and for re-runs. If the activation is rejected (e.g. an
`Auto` value paired with a non-Noir paradigm), the dial stays at `Default` —
log the rejection and continue; do not block onboarding.

**If `.claude/grimoire-config.json` is missing** the switch skill aborts with a
restore instruction; this cannot happen here because §3 just wrote it.

---

