# Work paradigms

Grimoire ships three selectable **work paradigms**. Only the *selected*
paradigm's instruction content is installed into the active files (lean by
design); the others stay dormant under `.claude/paradigms/<slug>/` and are
never loaded by agents during normal operation. This index is always present
so the three names — and the switch path — stay discoverable in-project even
when only one paradigm's content is active.

| Paradigm | Posture |
|----------|---------|
| **Supervised** | Default — per-step human gating; the agent confirms at every major decision. |
| **Weiss** | Collaborative — the user leads design; the agent acts as researcher/assistant. |
| **Noir** | Autonomous — the agent drives phases unsupervised until a milestone or stop signal. |

The active paradigm is recorded in `.claude/grimoire-config.json` as
`work-paradigm.value` and stamped into `CLAUDE.md`. Switch it with the
**`grm-work-paradigm-switch`** skill, which file-swaps the chosen paradigm's
content into the active paths and refreshes the `CLAUDE.md` stamp.

Full design: `docs/grimoire/design/work-paradigm-design.md`.
