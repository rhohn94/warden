---
name: grm-doc-assurance
description: Run 15 deterministic checks over a Grimoire repo's own documentation: flavor-parity, design-doc house-layout, internal-link integrity, a validated docs map, release consistency, tag-format (vX.Y vs vX.Y.Z), manifest/shipped-pointer hygiene, skill-budget, relative-links, hierarchy, lean-index, monolith-cap, description-cap, and anti-patterns. Report-only unless --strict. Use when checking doc quality or validating the docs.
---

# doc-assurance

Self-checking pass over Grimoire's own docs. One script, 15 checks. Run at
release closeout. Design: `docs/grimoire/design/doc-assurance-design.md`.

## Run

```bash
python3 .claude/skills/grm-doc-assurance/doc_assurance.py            # all checks, report-only
python3 .claude/skills/grm-doc-assurance/doc_assurance.py links      # one check
python3 .claude/skills/grm-doc-assurance/doc_assurance.py docs-map --write-map   # regenerate the map
python3 .claude/skills/grm-doc-assurance/doc_assurance.py --strict   # non-zero exit on any finding (gate)
```

Check names: `flavor-parity`, `design-layout`, `links`, `docs-map`,
`release-consistency`, `tag-format`, `manifest-detect-hygiene`, `shipped-pointers`,
`skill-budget`, `relative-links`, `hierarchy`, `lean-index`, `monolith-cap`,
`description-cap`, `anti-patterns`.

## The checks

| Check | What it verifies | Closes |
|---|---|---|
| `flavor-parity` | Skill presence parity root ↔ claude-code; content parity for must-match docs (coding-standards, feature-manifest). Intentional divergences allow-listed. | #50 |
| `design-layout` | Each `docs/design/*-design.md` (project-own) and `docs/grimoire/design/*-design.md` (framework) has the house sections (Motivation, Goals, Non-goals, Validation/Idempotency); flags unresolved open-questions. | #51 |
| `links` | Every relative Markdown link / doc reference resolves (skips http and anchors). | #52 |
| `docs-map` | `docs/README.md` lists every `docs/**/*.md`; orphan + stale detection both ways. `--write-map` regenerates. | #53 |
| `release-consistency` | Every shipped `## vX.Y` in version-history has a roadmap "Shipped" flip; `manifest-version` is an int; `framework-version` ≥ newest shipped. | #54 |
| `tag-format` | Warn (never block) when the newest git tag is two-part `vX.Y` instead of the fleet-wide recommended three-part `vX.Y.Z` — a forward nudge, never a history-migration demand. | audit v3.91 |
| `manifest-detect-hygiene` | No `feature-manifest.md` DETECT predicate depends on a sync/build-excluded framework-internal doc (the v3.39 Bulkhead) — such a detect could never pass on a consumer. | — |
| `shipped-pointers` | No shipped doc relative-links a target under an excluded path prefix (the same Bulkhead set) — that link would dangle in a consumer install. | — |
| `skill-budget` | Active `SKILL.md` bodies ≤ 12 KB (root **and every shipped flavor** — claude-code/codex/copilot, #399) and root `CLAUDE.md` ≤ 10 KB (v1.29 context budget); over-budget files flagged to split into a lean head + `reference.md`. | #55/#56/#399 |
| `relative-links` | Repo-wide: absolute internal links rejected (/ prefix or own repo URL). Docs-scoped: broken anchor detection; bare-prose backtick doc refs flagged. Dial: `doc-hierarchy.enforcer.value` in `grimoire-config.json`. | #96 |
| `hierarchy` | Docs-scoped: reachability from `docs/README.md`; breadcrumb (blockquote → README.md) on non-root non-index non-exempt pages; per-tier index presence. Dial: same as above. | #96 |
| `lean-index` | `docs/**/README.md` index pages ≤ 6 KB and link-dense (≥ 3 links); size-cap-exempt list for aggregating root indexes. | — |
| `monolith-cap` | Warns when a leaf doc (non-README.md under `docs/`) exceeds 20 KB. Warn-only, never a hard gate. | — |
| `description-cap` | Warns when a `SKILL.md` frontmatter description exceeds 450 chars, so the always-loaded skill-index footprint can't silently creep back. | — |
| `anti-patterns` | Warns when a `SKILL.md` `## Anti-patterns` section exceeds 1.5 KB. Warn-only, counted under `--strict`. | — |

## Posture

- Read-only except `--write-map` (regenerates `docs/README.md`).
- Report-only by default; `--strict` is the closeout gate.
- Legacy design docs predating the strict house layout may report `design-layout`
  findings — advisory, not blocking; bring them into conformance opportunistically.
- Pre-existing dead links in historical design docs are surfaced (not hidden);
  fix opportunistically or track as doc-debt.
- `relative-links` and `hierarchy` checks obey the `doc-hierarchy.enforcer.value`
  dial in `grimoire-config.json`: `off` skips them, `warn` (default) runs and
  prints findings but exits 0, `block` exits 1 on any finding. `--strict`
  overrides to `block` regardless of config.
- **`skill-budget` vs. `footprint.py` (#399):** this check is the authoritative
  per-skill authoring gate — it scans every shipped flavor because that is the
  content that actually ships. `grm-token-measure/footprint.py` is a different,
  intentionally single-tree tool (feeds the root-only dogfood baseline in
  `docs/grimoire/token-efficiency-baseline.md` via `baseline_gate.py`); point it
  at a specific flavor with `--root claude-code` to reproduce this check's
  numbers for that tree — the byte-counting is identical (`os.path.getsize`,
  no `reference.md`), so the two never disagree once pointed at the same root.

## Integration

The integration master runs `grm-doc-assurance` (all checks) at release closeout and
records material findings in the §5 ledger before tagging.
