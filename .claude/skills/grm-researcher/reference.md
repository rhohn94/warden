# Researcher — reference
Loaded on demand by `SKILL.md`.

## §6 — Reporter → Researcher escalation seam

The **Reporter** files already-clear feedback. When the Reporter receives an item
that is **ambiguous or under-specified** — it cannot derive a defensible scope,
motivation, or acceptance criteria without guessing — it must **escalate to the
Researcher rather than file a guess**. The escalation hands the raw idea to a
Researcher spawn (§7); the Researcher investigates, scopes, and files the single
well-formed item in the Reporter's place.

Division of labour:

| Input | Route | Why |
|---|---|---|
| Clear, actionable feedback | Reporter → `grm-feedback-to-issue` | No investigation needed; cheap wrap. |
| Vague / under-specified idea | **Researcher** (escalated) | Needs investigation before it can be scoped. |

> The reporter-side line documenting this escalation is added to
> `reporter/SKILL.md` by the integration master during consolidation — this
> section is the Researcher-side half of the same seam.

---

## §8 — Per-paradigm gating (mirrors the Reporter)

**Supervised (default):** each Researcher spawn is **user-confirmed** via the
standard `spawn_task` gate. After Phase 1, the Researcher **shows the drafted item
(house-layout body + band + audience) to the user and waits for approval before
the `create` call** — no autonomous filing.

**Weiss (Collaborative):** the integration master **offers** to spawn a Researcher
and **waits** for confirmation. Within the session, the drafted item is shown
before filing, same as Supervised. The user decides when and whether to file.

**Noir (Autonomous):** the integration master **spawns Researchers autonomously**
(no per-spawn confirmation) when it encounters an under-specified idea during
planning, review, or merge — and the Researcher **files autonomously** (no draft
approval gate). Near-duplicate handling follows `grm-feedback-to-issue` Noir rules
(file new if titles differ by >3 words; otherwise skip and note the duplicate).
The integration master may **batch-spawn** Researchers for multiple ideas at a
phase boundary. Pushing to origin remains human-gated even under Noir.

---

## §9 — Taxonomy placement

The Researcher is a fourth named agent role, a peer to the task agent,
integration master, and Reporter. Like the Reporter it is **not** a paradigm role
(available in all three) and **not** a workflow — it is a single-session agent
spawned on demand.

| Role | Session type | Context width | Git writes | Issue writes | Spawned by |
|---|---|---|---|---|---|
| Task agent | Work-item session | Medium–large | Yes (own branch) | No | Integration master |
| Integration master | Orchestration session | Medium | Merge only | Via Reporter/Researcher or direct | Human / Noir |
| Reporter | Feedback-filing session | Narrow | No | Yes (clear feedback) | Integration master / human / any |
| **Researcher** | Research-then-file session | Narrow (investigation-scoped) | No | Yes (one scoped item) | Integration master / human / Reporter (escalation) |

Full taxonomy doc: `docs/grimoire/design/agent-roles-design.md`.

---

## Anti-patterns

- **Filing without investigating.** If you go straight to `grm-feedback-to-issue`
  without Phase 1, you are a Reporter, not a Researcher — and you are guessing.
  Either investigate, or hand the item to the Reporter.

- **Filing more than one issue.** The Researcher files **exactly one** scoped
  item. Extra threads go under **Follow-ups** in that item; a genuinely separate
  item warrants a separate spawn.

- **Reimplementing a composed skill.** Never re-derive the survey method,
  the section layout, or the tracker `create` — invoke `grm-source-to-design-docs`,
  `grm-design-doc-scaffold`, and `grm-feedback-to-issue` respectively.

- **Unbounded investigation.** One `list --limit 30` for the duplicate check;
  read enough source to scope, not the whole codebase; cite-and-stop on external
  prior art. Research should inform the item, not become a thesis.

- **Dropping to a cheaper tier.** The Researcher's session is pinned to the review
  band (opus/high) regardless of profile. Do not down-tier it under Eco — under-
  scoping is the failure this role prevents.

- **Making git/branch writes.** The Researcher's only write is the tracker
  `create`. No commits, no branches, no `version/*` edits.

- **Hard-coding a tracker name.** Pass `--audience` and let routing resolve, per
  `grm-feedback-to-issue` §4. Never pass `--tracker <name>`.

- **Filing to a `version/*` branch (roadmap tracker).** Append on `dev` only;
  stop and report if HEAD is `version/*` or `main`.
