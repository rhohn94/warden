<!-- PARADIGM_SECTION:agent-role:start -->
- **Task agent** (common case): you're running a work-item session the
  integration master dispatched as an isolated-worktree subagent (via the
  `Agent` tool with `isolation:"worktree"` — chip-free; Noir does not use
  `spawn_task` chips), in your own worktree — follow everything below.
- **Project Manager** (multi-feature releases): atop the hierarchy, owning the
  release — track components, split features into non-colliding lanes, dispatch
  an integration master per lane, integrate, gate on QA, ship. Push human-gated.
  Guide: `.claude/skills/project-manager/SKILL.md`.
- **Integration master**: implement one feature lane under a PM, or run a
  single-feature release standalone. Drive the pipeline autonomously; pause only
  on merge conflict, test failure, push trigger (human-gated), or user stop.
  Guide: `.claude/skills/integration-master/SKILL.md`. Under `/loop`, its
  **release-master** variant owns a full release iteration in a fresh
  subagent (`noir-loop`).
- **Reporter** (optional, any paradigm): a narrow-context agent dispatched as a
  subagent (via the `Agent` tool under Noir — chip-free; Supervised / Weiss may
  use a `spawn_task` chip) to file feedback through `feedback-to-issue`. No
  git writes; targets the configured issue tracker only. Guide:
  `.claude/skills/reporter/SKILL.md`. Taxonomy + spawn template:
  `docs/integration-workflow.md` §Filing issues with the Reporter.
<!-- PARADIGM_SECTION:agent-role:end -->
