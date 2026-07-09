---
name: grm-workflow-scaffold
description: Scaffold a new .claude/workflows/{name}.js Claude Code Workflow — either a read-only analysis fan-out (all paradigms) or a write-capable parallel-commit workflow (Noir only). Use when the user wants to add or scaffold a workflow, fan out a read step, or mechanize an analysis step.
---

# Workflow scaffold

Claude Code `Workflow`s live at `.claude/workflows/{name}.js` and mechanise a
parallelisable step — either a read-heavy fan-out analysis or (under Noir) a
write-capable parallel-commit run. This skill creates the script and wires it
into the workflow-bootstrap manifest + golden baseline.

Its core value is the **authoring guidance** below: it encodes the
token-efficiency lessons measured in v1.4 (read-only tier) and the NW1 safety
rails (write-capable tier), so a new workflow is cheap and safe by construction.
The fully-worked reference is `.claude/workflows/release-planning.js`; the
rationale is `docs/grimoire/design/release-planning-workflow-design.md`; the write-capable
tier design is `docs/grimoire/design/write-capable-workflow-design.md` — point at them,
don't restate them.

---

## Two workflow tiers

Every Workflow script declares its tier in `export const meta`:

```js
export const meta = {
  name: 'example',
  tier: 'read-only',   // 'read-only' (default, all paradigms) | 'write-capable' (Noir only)
  // …
}
```

If `tier` is omitted it defaults to `'read-only'`.

| Tier | Paradigm gate | Agents mutate files? | Isolation | Commits? | Push? |
|------|--------------|---------------------|-----------|---------|-------|
| **read-only** | All paradigms | No | Shared session | No | No |
| **write-capable** | **Noir only** | Yes | Per-agent worktree | Yes (own branch) | Never (human gate) |

### Read-only tier (default)

The Workflow fans out agents to read, analyse, and synthesise. No file is written,
no branch is created, no commit is made. The result is returned to the master who
takes any file-writing next step through the normal skills. This is the only tier
available under Supervised and Weiss paradigms.

### Write-capable tier (Noir only)

The Workflow fans out agents that each receive an isolated worktree, implement a
discrete work item, commit to a short-lived branch, and exit. The integration
master collects the branches and merges them. Write-capable Workflows are gated
to Noir because they require the master to operate autonomously — picking up
branches, resolving conflicts, driving the merge sequence without per-step user
confirmation.

**Noir gate (required in every write-capable script):**

```js
// At the top of the script, before any write phase:
if (meta.tier === 'write-capable' && activeParadigm() !== 'Noir') {
  throw new Error(
    'write-capable workflows require the Noir paradigm. ' +
    'Switch paradigm or use a read-only workflow.'
  );
}
```

This check is explicit, early, and fail-closed. See
`docs/grimoire/design/write-capable-workflow-design.md §1.2` for the full spec.

---

## Placement convention

Saved workflows live at `.claude/workflows/{name}.js`. The filename **is** the
invocation name: `Workflow({ name: '{name}' })`. The full path + invocation
convention is documented in the design doc's **§The `.claude/workflows/<name>.js`
convention** — point there rather than restating it.

---

## After scaffolding

Per the `.claude/workflows/` convention W4 established, a new workflow is a
restorable artifact — register and baseline it:

1. **Register it** in `grm-workflow-bootstrap`'s manifest under
   **## Restorable workflows (`golden/workflows/`)** — add a `{name}.js` row with
   a one-line purpose.
2. **Golden-snapshot it**: copy the finished script to
   `claude-code/.claude/skills/grm-workflow-bootstrap/golden/workflows/{name}.js`,
   then verify byte-identity with `cmp` against the live file. (Or run the
   **`grm-workflow-snapshot`** skill to re-baseline.)
3. Cross-link the design doc / `workflow-candidates.md` if this build came from
   a planned candidate.

---

## Reference (load on demand)

The head above is self-sufficient for the common path (pick a tier, place the
script at `.claude/workflows/{name}.js`, copy the real structure from
`.claude/workflows/release-planning.js`, then register + golden-snapshot).
Everything below lives in **`reference.md`** (same directory) — read the one
piece you need:

- **When to use — and when NOT** — the read-only vs. write-capable decision and
  the `Workflow` vs. skill vs. `spawn_task` vs. `Agent` matrix pointer.
- **Three execution variants** — the Efficient / Fast / Careful-Serial table and
  the `selectVariant(args)` encoding for variant-aware fan-out.
- **Authoring guidance — the measured lessons (read-only tier)** — the six
  A/B-measured cost levers (model tiering, batch-vs-fanout, single-step reads,
  schemas, output minimization, read-only contract). Encode these in every
  workflow you scaffold.
- **Safety rails for write-capable workflows** — the five write-capable
  invariants, the Sonnet-default / `item.hard`→Opus tiering rule, the branch
  naming convention, and the conflict-map / `mergeAfter` handoff.
- **Minimal template — read-only tier** — the Orient→Gather→Size→Synthesize
  skeleton to copy.
- **Minimal template — write-capable tier (Noir only)** — the
  Orient→Plan→Execute→Report skeleton with the Noir gate and variant selection.
- **Anti-patterns** — the full catalog of what not to do (all paradigm/tiering/
  push/merge/schema pitfalls).
