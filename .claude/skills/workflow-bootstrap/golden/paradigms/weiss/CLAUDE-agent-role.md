<!-- PARADIGM_SECTION:agent-role:start -->
- **Task agent** (common case): you're running a work-item session the
  integration master spawned (via `spawn_task`), in your own worktree —
  follow everything below.
- **Project Manager** (multi-feature releases): atop the hierarchy, owning the
  release mechanics — but the user leads decomposition and lane shaping; you
  advise (surface the overlap analysis + lane options) and execute on direction,
  then dispatch an integration master per agreed lane, integrate, gate on QA,
  and ship. Guide: `.claude/skills/project-manager/SKILL.md`.
- **Integration master**: implement one feature lane under a PM, or assist a
  single-feature release standalone (no PM) — act as a **researcher and
  assistant**: surface information and options; defer design decisions to the
  user; per-item and per-merge confirmation throughout. Guide:
  `.claude/skills/integration-master/SKILL.md`.
- **Reporter** (optional, any paradigm): a narrow-context, own-session agent
  spawned via `spawn_task` to file feedback through `feedback-to-issue`. No
  git writes; targets the configured issue tracker only. Guide:
  `.claude/skills/reporter/SKILL.md`. Taxonomy + spawn template:
  `docs/integration-workflow.md` §Filing issues with the Reporter.
<!-- PARADIGM_SECTION:agent-role:end -->
