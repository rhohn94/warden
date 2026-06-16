---
name: repo-reference
description: Subagent model/effort table and design-doc location map. Use when choosing a subagent's model/effort, or locating/creating a design or operational doc. Triggers on "which model", "effort level", "subagent", "where does this doc live".
---

# Repo reference

Two lookup tables: subagent model/effort selection and doc location map.
Customise the doc-location map for your project before using this scaffolding.

## Subagent model & effort

Model/effort is **not** a hard-coded table here any more. It resolves through
the active **model/effort profile** — a named cost-posture stored in config and
backed by the profile registry `.claude/model-effort-profiles.json` (the single
source of truth). This skill documents the *bands* and *profiles*; the registry
holds the data. (Design: `docs/design/model-effort-profiles-design.md`.)

### Complexity bands (the resolution axis)

Every work item classifies into one ordered band from its token estimate plus a
design/review flag. Bands are profile-invariant; only the model+effort each band
maps to changes per profile.

| Band | Trigger |
|---|---|
| **trivial** | ≤ 15 K est. tokens; mechanical/read-only (lookups, text extraction, search) |
| **small**   | 15 K–40 K est. tokens; localized edits, single-file changes |
| **medium**  | 40 K–80 K est. tokens; multi-file implementation, test runs |
| **large**   | > 80 K est. tokens; cross-cutting implementation |
| **review**  | any planning, code review, security review, architecture/design analysis (regardless of token estimate) |

### The starter profiles

The active profile is `model-effort-profile.value` in
`.claude/grimoire-config.json` (absent/unset → **Medium**, the registry's
`default-profile`). Posture each profile encodes:

- **Medium** *(default)* — today's behaviour. Opus only for `large` + `review`;
  Sonnet the implementation workhorse; Haiku for trivial.
- **High Effort** — high+ effort everywhere; Opus from the `medium` band up.
- **Low Effort** — push down hard: Haiku for trivial + small; Sonnet (low/medium)
  above; no Opus.
- **Efficient** — lean Sonnet on the cheap end, Opus allowance restored for
  `large` + `review`. Spend where it matters.
- **Eco/Budget** — **no Opus at all**; Sonnet ceiling for medium–large.
- **Autonomous** — lean for unattended runs: Haiku trivial, Sonnet small→large
  (effort scaling low→high), Opus reserved for `review` only.

The exact band × profile matrix lives in `.claude/model-effort-profiles.json` —
read it, don't re-transcribe it here; the registry is the source of truth for
the full profile set.

### The resolver (single resolution path)

Both `release-phase` Step 3 and any agent choosing a tier resolve identically:

1. Read `model-effort-profile.value` from `.claude/grimoire-config.json`.
   Absent/unset → registry `default-profile` (`Medium`).
2. Load `.claude/model-effort-profiles.json`; select `profiles[<active>]`. If the
   named profile is missing → fall back to `default-profile` + a one-line warning
   (fail-safe, never fail-closed on dispatch).
3. Classify the item into a **band** (above) from its token estimate +
   design/review flag.
4. Return `profiles[<active>][<band>]` → the `{model, effort}` pair. `inherit`
   effort means "don't override the session's inherited effort."
5. **UX pins (profile-invariant special-case):** `design-language-adapt` →
   `sonnet`/`medium`, `ux-demo-build` → `sonnet`/`high`, regardless of active
   profile (see the registry's `ux-pins`). The resolver returns these directly
   when the item is one of those skills.

The resolved `{model, effort}` pair is the single output every caller consumes
(e.g. `release-phase` renders it into the spawned task name).

When in doubt, err toward `sonnet` — under-powered agents produce work that
needs re-doing.

## Reading files cost-efficiently (cache-aware)

Warm prompt-cache reads are the cheapest token class, but the cache only stays
warm behind a *stable* prefix. Two habits keep reads cheap (full mechanics:
`docs/design/token-efficiency-design.md` §Cache-aware authoring, lever 2):

- **Read a named set in one step, not drip-by-drip.** When you already know
  which files you need (e.g. the design docs a task names, or a fixed table of
  paths), read them all in a single batched step rather than reading one,
  acting, reading the next. Drip-reading across turns repeatedly reshapes the
  tail of the context and forces cache re-creation of the same bytes.
- **Don't interleave reads with edits that invalidate the prefix.** Finish the
  reading you can predict up front *before* you start editing. Edits near the
  front of the context drop every warm token behind them; alternating read→edit
  →read churns the cache worse than either alone. Exploratory reads that genuinely
  depend on what an earlier read revealed are fine — this targets *predictable*
  read sets, not legitimate discovery.

## Design-doc location map

Per-project design docs live under `docs/design/`. Cross-cutting standards and
operational docs (`coding-standards.md`, `architecture-guidelines.md`,
`version-design.md`, `release-planning.md`, etc.) live directly under `docs/`.
Offer to create a design doc (using **`design-doc-scaffold`**) if one doesn't
exist.

| Aspect                       | Document                                   |
|------------------------------|--------------------------------------------|
| Overview / entry point       | `docs/design/README.md`                    |
| Architecture (this project)  | `docs/design/architecture-design.md`       |
| Architecture guidelines      | `docs/architecture-guidelines.md`          |
| Coding standards             | `docs/coding-standards.md`                 |
| UX / interaction model       | `docs/design/ux-design.md`                 |
| UX-tier design docs (design-language, components, theming) | `docs/design/ux/`                          |
| Feature *X*                  | `docs/design/{X}-design.md`                |
| Versioning & releases        | `docs/version-design.md`                   |

For UX-tier work, see `design-language-adapt` and `ux-demo-build`.

**Customise this table** for your project by adding rows for specific
subsystems, tech areas, or cross-cutting concerns. The more precisely this
map reflects your actual docs, the faster agents can orient.
