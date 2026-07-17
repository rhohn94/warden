# Grm-agent-scout — reference
Loaded on demand by `SKILL.md`.

## §7 — Taxonomy placement (§A)

The Scout is a **fifth named role** in the Grimoire role registry. This table
is the **single canonical copy** — `grm-agent-reporter`, `grm-agent-reviewer`, and
`grm-agent-researcher` cross-link here instead of restating it:

| Role | Session type | Context width | Git writes | Issue writes | Spawned by |
|---|---|---|---|---|---|
| Task agent | Work-item session | Medium–large | Yes (own branch) | No | Integration master |
| Integration master | Orchestration session | Medium | Merge only | Via Reporter or direct | Human / Noir |
| Reporter | Feedback-filing session | Narrow | No | Yes | Integration master / human / any |
| Reviewer | Pre-merge audit session | Narrow–medium | No | No (→ Reporter) | Integration master / human |
| **Scout** | **Investigation session** | **Narrow** | **No** | **No** | **Integration master / task agent** |
| Verifier | QA session | Medium | No | No (→ Reporter) | Integration master (pre-merge) |
| Triager | Grooming session | Narrow | No | Yes | Integration master / human / any |
| Researcher | Research-then-file session | Narrow (investigation-scoped) | No | Yes (one scoped item) | Integration master / human / Reporter (escalation) |

Full role-taxonomy rationale: framework-internal design, upstream Grimoire repo.

The Scout is **not** a paradigm role — it is available in Supervised, Weiss, and
Noir. It is also not a workflow: it is a single-session agent spawned on demand.
Unlike every other narrow role, the Scout may be spawned by **either** the
integration master **or** a task agent — it is the one research delegation a
task agent is permitted to make before committing to an approach.

---

