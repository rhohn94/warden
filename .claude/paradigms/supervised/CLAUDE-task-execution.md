<!-- PARADIGM_SECTION:task-execution:start -->
Implement to the agreed checkpoint, then review for bugs/incomplete work.
Read the relevant design docs first; add/update
`docs/design/{feature}-design.md` when the task introduces a feature
(**`grm-design-doc-scaffold`** skill). Doc-location map + subagent model/effort
table: **`grm-repo-reference`** skill.

Before committing to an approach on an ambiguous item, confirm your plan with
the user. If the acceptance criteria leave room for interpretation, surface the
options and wait for direction.

**Done-criteria for a work item that creates or materially reshapes a
reusable component:** add/update that component's `component.json` in the
same branch — fold cataloging into the work that already holds the context,
instead of a standalone pass later (`docs/grimoire/design/component-catalog-architecture-design.md`;
vocabulary: `docs/grimoire/design/component-taxonomy.md`). Applies to a
`components/`/`lib/`-shaped unit (or an existing `component.json`/front-matter
owner) another project could vendor — not app-internal glue.
<!-- PARADIGM_SECTION:task-execution:end -->
