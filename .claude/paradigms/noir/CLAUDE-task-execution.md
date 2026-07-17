<!-- PARADIGM_SECTION:task-execution:start -->
Read the relevant design docs and the item's acceptance criteria. Implement
to the agreed checkpoint without pausing for per-step confirmation — execute
the full item and report done.

If the acceptance criteria are unambiguous, proceed directly. If they leave
room for interpretation on a decision that is hard to reverse, surface the
question once and wait; otherwise pick the most defensible reading and proceed.

Review your own diff against the acceptance criteria before reporting done.
Add/update `docs/design/{feature}-design.md` when the task introduces a
feature (**`grm-design-doc-scaffold`** skill). Doc-location map + subagent
model/effort table: **`grm-repo-reference`** skill.

**Done-criteria for a work item that creates or materially reshapes a
reusable component:** add/update that component's `component.json` in the
same branch — fold cataloging into the work that already holds the context,
instead of a standalone pass later (`docs/grimoire/design/component-catalog-architecture-design.md`;
vocabulary: `docs/grimoire/design/component-taxonomy.md`). Applies to a
`components/`/`lib/`-shaped unit (or an existing `component.json`/front-matter
owner) another project could vendor — not app-internal glue.
<!-- PARADIGM_SECTION:task-execution:end -->
