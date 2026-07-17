---
name: grm-required-feature-catalog
description: Family-neutral catalog of framework-mandated features (Admin Console, Changelog Surface, standard-package adoptions), gated by applies-when-family (cli/gui/lib/service/web) and a config dial. Re-runnable: a deterministic offline planner files only new/changed entries against an already-applied project. Use when filing/re-running the required-feature catalog, or checking a blocked-on-upstream entry.
---

# required-feature-catalog

Owns the **required-feature catalog** (`required-feature-catalog.md`, sibling
of this file) and its re-run planning engine (`catalog_filing.py`). Relocated
here in v3.97 (#413) from `grm-web-app-apply` — the catalog used to be
web-app-scoped and fire once, at onboarding/first-apply; it is now
family-neutral (every entry declares which of `cli`/`gui`/`lib`/`service`/`web`
it applies to) and callable on demand.

This skill has **no interactive flow of its own** — it is invoked by a caller
that already knows the project's family and root: `grm-onboarding` §6.5.7
(family resolved during its own interview), `grm-web-app-apply` §6 (fixed
family `web`), or a direct re-run (e.g. after a release adds a catalog entry,
or to check on a `blocked-on-upstream` entry's activation state).

Design authority: `required-feature-catalog.md` itself (§Re-running,
§Conditional applicability, §Status — the catalog's own schema/mechanism
documentation lives there, not duplicated here); `docs/grimoire/design/web-app-support-design.md`
§5 (catalog format, filing flow, first entry).

---

## §1 — Plan a filing run

```bash
python3 .claude/skills/grm-required-feature-catalog/catalog_filing.py plan \
  --root <project-root> --family {cli,gui,lib,service,web}
```

Prints one JSON object per catalog entry: `{key, action, reason}`. `action` is
one of:

| Action | Meaning |
|---|---|
| `not-applicable` | family or config-dial gate excludes this entry — do nothing |
| `manual-review` | the entry's `applies-when` is a repo-state predicate, not a config dial (e.g. Entry 7's Aura-consumption detect) — an agent must evaluate it by inspecting the repo tree, per the entry's own "Detect." guidance |
| `file` | new or changed since the last recorded outcome — file/re-file the normal ticket |
| `skip-already-filed` | already filed, unchanged — do nothing |
| `file-blocked` | a `status: blocked-on-upstream` entry, not yet activated, never recorded (or its spec changed) — file the ticket tagged blocked-on-upstream |
| `skip-already-blocked` | already filed as blocked-on-upstream, unchanged, still not activated — do nothing |
| `activate` | a `blocked-on-upstream` entry whose `activation-check` now passes — file/update the ticket as a normal (non-blocked) one |

`plan` never calls the issue tracker and never mutates state — it is a pure
read+report, safe to call repeatedly.

## §2 — Act on the plan, then record the outcome

For every `file` / `file-blocked` / `activate` result, spawn a **Reporter**
(`grm-agent-reporter`) to file (or update) the `Grimoire-Requirement`-tagged
ticket via `grm-feedback-to-issue`, using the entry's title/body/labels from
`required-feature-catalog.md`, exactly as the pre-existing filing contract
specifies. **Before filing, the Reporter still does the issue-tracker dedupe
search** (§Filing contract in the catalog) — `catalog_filing.py`'s state is a
planning aid, not a replacement for that authoritative check.

`file-blocked` tickets carry the catalog entry's `activation-event` prose
verbatim and an additional `blocked-on-upstream` label (alongside
`Grimoire-Requirement`) so they read as distinct from actionable tickets in a
list view.

After the Reporter acts, persist the outcome so the next `plan` call sees it:

```bash
python3 .claude/skills/grm-required-feature-catalog/catalog_filing.py record \
  --root <project-root> --key <key> --status {filed,blocked-on-upstream}
```

`record` writes `.claude/required-feature-catalog-state.json` in the target
project (created on first use). This file is the project's own filing ledger —
commit it like any other `.claude/` state file.

## §3 — `manual-review` entries

An entry whose `applies-when` predicate is not a `<dot.path> == "<value>"`
config-dial equality (today: Entry 7, Aura consumption) cannot be planned
automatically. The invoking agent inspects the target repo per the entry's own
"Detect." section and decides `file` / `not-applicable` itself, then calls
`record` with the outcome exactly as it would for an automatically-planned
entry.

## §4 — Conformance verification (#434)

Filing a ticket only tracks the obligation; it never re-checks whether the
obligation was actually satisfied. Each entry's `conformance-check:` field
names a deterministic probe script (or the literal `exempt (...)` marker for
a spec-only/blocked-on-upstream entry with no upstream artifact to probe):

```bash
python3 .claude/skills/grm-required-feature-catalog/catalog_conformance.py \
  plan --root <project-root> --family {cli,gui,lib,service,web}
```

Prints one `{key, action, detail}` per entry — `not-applicable`/`exempt`/`ok`
are informational; `warn`/`fail` are real findings; `degraded` means the
probe script itself is unavailable in this flavor (a tooling gap, never a
conformance failure). `install_doctor.py` wires this in as a WARN-by-default,
`--strict`-blocking health check; `grm-fleet-audit`'s Step 4a reconciles the
verdicts against filed `[key: ...]` tickets (`catalog_reconcile.py`).

---

## Anti-patterns

- Filing a ticket without the Reporter's own issue-tracker dedupe search —
  `catalog_filing.py`'s state file is an offline planning aid, not a
  replacement for the authoritative issue-tracker check.
- Treating a `file-blocked` ticket as equivalent to a normal `file` one — it
  must carry the `activation-event` text and read as distinct (a
  `blocked-on-upstream` label), or a managed project cannot tell "actionable
  now" from "actionable once the upstream artifact ships."
- Calling `record` before the Reporter has actually filed/updated the ticket —
  `record` is the caller's confirmation that the action happened, not a
  pre-commitment.
- Re-deriving a project's family instead of asking the caller for it — this
  skill has no family-detection logic of its own by design (that's
  `grm-quick-start-template` §1 / the Q9 signal table's job); inventing a
  second detector here would drift from the first.
- Hand-editing `.claude/required-feature-catalog-state.json` instead of using
  `record` — the content-hash field is what makes "only file changed entries"
  work; a hand-edit that drops or mismatches it silently breaks the
  idempotency guarantee.
