<!-- PARADIGM_SECTION:agent-role:start -->
- **Task agent** (common case): you're running a work-item session the
  integration master spawned (via `spawn_task`), in your own worktree —
  follow everything below.
- **Project Manager** (multi-feature releases): atop the hierarchy, owning the
  release — track components, partition features into non-colliding lanes,
  dispatch an integration master per lane, integrate, gate on QA, and ship.
  Confirm with the user at decomposition, the lane plan, each dispatch, the QA
  verdict, and the release. Guide: `.claude/skills/grm-project-manager/SKILL.md`.
- **Integration master**: implement one feature lane under a PM, or own a whole
  single-feature release standalone (no PM). Your guide is
  `.claude/skills/grm-integration-master/SKILL.md` — the `grm-release-planning` →
  `grm-release-agreement` → `grm-release-phase` → `grm-release-phase-merge` →
  `grm-project-release` skills with user-confirmed gates at scope lock, batch spawn,
  each merge, and push to origin.
- **Reporter** (optional, any paradigm): a narrow-context, own-session agent
  spawned via `spawn_task` to file feedback through `grm-feedback-to-issue`. No
  git writes; targets the configured issue tracker only. Guide:
  `.claude/skills/grm-agent-reporter/SKILL.md`. Taxonomy + spawn template:
  `docs/grimoire/integration-workflow.md` §Filing issues with the Reporter.
<!-- PARADIGM_SECTION:agent-role:end -->
