<!-- PARADIGM_SECTION:task-execution:start -->
Read the relevant design docs and the item's acceptance criteria. Before
acting, **present your implementation plan to the user and wait for their
direction** — do not begin implementation until the user approves the
approach.

If the acceptance criteria leave room for interpretation, list the options
with tradeoffs and ask the user to choose. Do not pick the "obvious" path
unilaterally.

Once the user has approved an approach, implement to the agreed checkpoint,
then review for bugs/incomplete work before reporting done.

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
