---
name: grm-doc-assurance
description: Run eight deterministic checks over a Grimoire repo's own documentation: flavor-parity (claude-code/root/copilot drift), design-doc house-layout, internal-link integrity, a validated docs map, cross-doc release consistency, skill-budget, relative-links (absolute-internal + broken anchors + bare-prose), and hierarchy (reachability + breadcrumbs + per-tier index). Report-only unless --strict. Use when checking doc quality or validating the docs.
---

# doc-assurance

Self-checking pass over Grimoire's own docs. One script, eight checks. Run at
release closeout. Design: `docs/grimoire/design/doc-assurance-design.md`.

## Run

```bash
python3 .claude/skills/grm-doc-assurance/doc_assurance.py            # all checks, report-only
python3 .claude/skills/grm-doc-assurance/doc_assurance.py links      # one check
python3 .claude/skills/grm-doc-assurance/doc_assurance.py docs-map --write-map   # regenerate the map
python3 .claude/skills/grm-doc-assurance/doc_assurance.py --strict   # non-zero exit on any finding (gate)
```

Check names: `flavor-parity`, `design-layout`, `links`, `docs-map`,
`release-consistency`, `skill-budget`, `relative-links`, `hierarchy`.

## The checks

| Check | What it verifies | Closes |
|---|---|---|
| `flavor-parity` | Skill presence parity root ↔ claude-code; content parity for must-match docs (coding-standards, feature-manifest). Intentional divergences allow-listed. | #50 |
| `design-layout` | Each `docs/design/*-design.md` (project-own) and `docs/grimoire/design/*-design.md` (framework) has the house sections (Motivation, Goals, Non-goals, Validation/Idempotency); flags unresolved open-questions. | #51 |
| `links` | Every relative Markdown link / doc reference resolves (skips http and anchors). | #52 |
| `docs-map` | `docs/README.md` lists every `docs/**/*.md`; orphan + stale detection both ways. `--write-map` regenerates. | #53 |
| `release-consistency` | Every shipped `## vX.Y` in version-history has a roadmap "Shipped" flip; `manifest-version` is an int; `framework-version` ≥ newest shipped. | #54 |
| `skill-budget` | Active `SKILL.md` bodies ≤ 12 KB and `CLAUDE.md` ≤ 10 KB (v1.29 context budget); over-budget files flagged to split into a lean head + `reference.md`. | #55/#56 |
| `relative-links` | Repo-wide: absolute internal links rejected (/ prefix or own repo URL). Docs-scoped: broken anchor detection; bare-prose backtick doc refs flagged. Dial: `doc-hierarchy.enforcer.value` in `grimoire-config.json`. | #96 |
| `hierarchy` | Docs-scoped: reachability from `docs/README.md`; breadcrumb (blockquote → README.md) on non-root non-index non-exempt pages; per-tier index presence. Dial: same as above. | #96 |

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

## Integration

The integration master runs `grm-doc-assurance` (all checks) at release closeout and
records material findings in the §5 ledger before tagging.
