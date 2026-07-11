---
name: grm-workflow-bootstrap
description: Guided install and restore of the agentic-workflow skill set. Detects missing or drifted workflow skills/hooks, restores them from self-contained golden copies, then runs an interactive interview to fill project-specific placeholders (test/build/release commands, version file, branch names). Use when onboarding a new project, after copying this scaffolding into an existing repo, or when a required workflow skill is missing.
---

# Workflow-bootstrap

Equips a project's Claude agent with the workflow skill set and tailors it
to the project — without the user hand-editing a dozen files. It restores
skills from a **golden baseline** that is **generated**, not a committed
tree: `generate_golden.py` derives it from the pristine install (or extracts
a frozen archive), so restore works even if every sibling skill was deleted.

The golden baseline is **generic** (placeholder-laden, language-agnostic).
Bootstrap restores structure, then the interview customises it. It is a
point-in-time baseline frozen at install/version-change — not a
perpetually-synced mirror.

`manifest.md` (next to this file) is the source of truth for the canonical
set and the project-config placeholders.

> **Golden is generated (v3.49).** There is no longer a committed `golden/`
> tree. `generate_golden.py` derives the golden image from the flavor/install
> and writes it under the gitignored `.grimoire-golden/` cache. Throughout this
> guide, "the golden tree" / "`golden/…`" means the **resolved** tree returned
> by `resolve_golden()` (typically `.grimoire-golden/tree/`).

> **Justfile interview (v3.53).** Step 3 now asks for three optional
> Justfile-specific commands — build, run, and deploy — stored as
> `commands.build`, `commands.run`, and `commands.deploy` in
> `.claude/grimoire-config.json`. A blank answer keeps the Justfile recipe body
> as a `# grimoire:placeholder` stub; an existing non-placeholder recipe is
> never overwritten.

---

## Step 0 — Resolve / freeze the golden baseline

Before any inventory, materialize the golden baseline:

- **Fresh install (first bootstrap):** the just-copied files are pristine, so
  freeze a versioned baseline from them —
  `python3 .claude/skills/grm-workflow-bootstrap/generate_golden.py --freeze .`
  This writes `.grimoire-golden/golden-v{X.Y}.tar.gz` (the offline restore
  baseline). No network needed.
- **`--restore` / repair (later):** do **not** re-freeze from the now-customised
  tree (that would poison the baseline). Resolve the existing baseline instead —
  `… generate_golden.py --ensure-tree .` extracts the frozen archive (or, in the
  source/dogfood repo, generates from the `claude-code/` flavor) to
  `.grimoire-golden/tree/`.

All subsequent steps diff/restore against this resolved tree.

---

## Step 1 — Inventory

Read `manifest.md`. For each entry under "Restorable skills",
"Restorable infrastructure", "Restorable workflows", and "Restorable
paradigm content sets", classify the live copy by comparing the project
file against the resolved golden tree (Step 0):

| State | Meaning | Default action |
|---|---|---|
| **MISSING**   | No live file. | Restore from golden. |
| **PRISTINE**  | Live == golden (still has placeholders). | Restore not needed; flag for interview. |
| **CUSTOMISED**| Live differs, no project-config placeholders left. | Leave as-is. |
| **DRIFTED**   | Live differs *and* still has placeholders, or structurally diverged. | Ask before touching. |

Use `diff` against `<golden>/skills/<name>/SKILL.md`,
`<golden>/hooks/<file>`, `<golden>/settings.json`, and
`<golden>/workflows/<name>.js` (for any `.claude/workflows/*.js`), where
`<golden>` is the resolved tree from Step 0. Present the inventory as a table
before changing anything.

---

## Step 2.5 — Seed upstream URLs (idempotent)

After restoring files from golden, seed the default upstream source URLs into
the project. This step runs for every bootstrap (new project or `--restore`)
and is safe to repeat — it never overwrites a value the project has already
set.

## Step 2.6 — Populate `.grimoire-source/` (idempotent)

After restoring files from golden and seeding upstream URLs, copy the canonical
framework source artifacts into `.grimoire-source/` at the **project root**.
This folder is the clean, read-only generation source that doc-generating skills
(`grm-source-to-design-docs`, `grm-design-doc-scaffold`, `grm-design-language-adapt`) prefer
over the live tree. See `.grimoire-source/README.md` for the full design rationale.

**What to copy** (conservative scope — only the artifacts the three consumer
skills read):

- All `SKILL.md` files from `.claude/skills/` → `.grimoire-source/skills/<name>/SKILL.md`
- Structural/operational docs under `docs/grimoire/` (if present) → `.grimoire-source/docs/grimoire/`

**Idempotency rules:**

1. If `.grimoire-source/` already exists and its contents match the golden/live
   sources → no-op (skip silently).
2. If `.grimoire-source/` is absent or stale → copy the artifacts in, creating
   parent directories as needed.
3. Never delete files from `.grimoire-source/` that are not in the copy source
   (additions are forward-compatible; old consumers may reference older paths).

**Important:** `.grimoire-source/` is gitignored and is a **runtime artifact**,
not a committed file. Do not stage or commit anything inside it.

**On `--restore`:** re-run this step unconditionally to ensure the generation
source matches the restored golden state.

---

## Step 2.7 — Register bundled MCP servers (idempotent, merge-safe)

Grimoire bundles an issue-tracker MCP server (v3.12). After restoring files,
register it so any MCP harness can use it:

1. **Server files** land via the Step 2 golden restore
   (`.claude/mcp-servers/lib/mcp_runtime.py`,
   `.claude/mcp-servers/issue-tracker/server.py`). If MISSING, restore from
   `golden/mcp-servers/`.
2. **`.mcp.json`** at the project root — **read-merge-write** the `mcpServers`
   object: add the `grimoire-issue-tracker` entry (`command: python3`,
   `args: [".claude/mcp-servers/issue-tracker/server.py"]`) **without clobbering**
   any server the project already declares. If `.mcp.json` is absent, create it
   from `golden/mcp.json`. (Cross-harness: Cursor `.cursor/mcp.json`, VS Code /
   Copilot `.vscode/mcp.json` — same shape.)
3. **Config** — `config-validate --migrate` backfills the default-on `mcp` block
   (`enabled` / `prefer-for-tracker`). No schema bump.
4. **Verify** — run `mcp_runtime.py --self-test` and `server.py --self-test`.

**On `--restore`:** re-run unconditionally; the merge is idempotent (an already
present `grimoire-issue-tracker` entry is left unchanged).

---

## Step 2.7.1 — Provision issue-filing authority (opt-in, idempotent, v3.74)

If the Step 3 interview recorded a **Yes** to the issue-filing-authority
question (`issue-filing-authority.enabled: true` in
`.claude/grimoire-config.json`), run the provisioning helper to merge the
filing permission allowlist into `.claude/settings.json`:

```bash
python3 .claude/skills/grm-issue-tracker/provision_filing_authority.py .
```

This adds the issue-tracker MCP tool names (namespaced from `.mcp.json`) and
the CLI-fallback `Bash(...)` rules for `issue_tracker.py` — additively, never
removing or reordering existing `permissions.allow` entries. If the dial is
absent or `false`, skip this step entirely — filing authority is never
provisioned without the explicit opt-in. See
`docs/grimoire/design/issue-filing-authority-design.md`.

**On `--restore`:** re-run unconditionally; the helper is opt-in gated (skips
when the dial is absent/false) and idempotent (a re-run over an
already-provisioned `settings.json` is a no-op).

---

## Step 4.5 — Always-deliver the paradigm breadcrumb (idempotent)

Regardless of selected paradigm and independent of `--restore`, ensure the
breadcrumb index is present so all three paradigm names stay greppable
in-project (the lean install is unchanged — only this small always-present
index is added):

- Rewrite `.claude/paradigms/README.md` from
  `golden/paradigms/README.md` (overwrite — it is a static index with no
  project-config tokens, so rewriting from golden is the idempotent operation).
  Create the `.claude/paradigms/` directory if absent.

---

## Step 4.6 — Seed docs hierarchy stubs (idempotent, never-clobber)

After delivering the paradigm breadcrumb and independent of `--restore`,
seed the docs hierarchy index stubs so every new project has navigable
entry points from day one. Both files are minimal stubs — they are never
overwritten if a project-customised version already exists.

- **`docs/README.md`** — if MISSING, copy from
  `golden/docs/README.md` (the docs root index with `<!-- docs-map:begin -->` /
  `<!-- docs-map:end -->` markers). Create the `docs/` directory if absent.
- **`docs/grimoire/README.md`** — if MISSING, copy from
  `golden/docs/grimoire/README.md` (the Grimoire-internal tier index). Create
  the `docs/grimoire/` directory if absent.

**Never-clobber rule:** if either file already exists (even with content that
differs from golden), leave it unchanged. This matches the
`.scaffold-upstream.conf` / `vendor.toml` no-silent-clobber contract — the
golden copy is a seed, not an enforced template.

**On `--restore`:** re-run this step. The never-clobber rule still applies —
`--restore` does not override a project-customised stub.

---

## Step 5 — Verify & report

1. Re-grep for project-config placeholders; none of the Step-4 tokens
   should remain in patched files. In particular, `CLAUDE.md` carries a single
   `## Paradigm` stamp with a concrete name (no leftover `{ACTIVE}`), and
   `.claude/paradigms/README.md` is present and names all three paradigms.
2. Confirm the four hooks are referenced in `.claude/settings.json`.
3. If `--restore` was run: confirm `.claude/paradigms/{supervised,weiss,noir}/`
   exist and are populated; confirm `grm-work-paradigm-switch` ran to completion.
4. Report:
   - **Restored**: files copied from golden (by path).
   - **Patched**: tokens filled, with the values used.
   - **Untouched**: customised skills left alone.
   - **Needs human input**: anything that couldn't be resolved
     (missing CLAUDE.md, ambiguous doc map, etc.).
5. Suggest next steps: `grm-source-to-design-docs` if `docs/design/` is empty,
   then `grm-release-planning` when a release is being scoped.

No git operations. The user reviews and commits.

---

## Reference (load on demand)

- `When to use this skill` — see `reference.md`
- `Anti-patterns` — see `reference.md`
- `Grimoire Framework URL (`.scaffold-upstream.conf`)` — see `reference.md`
- `Aura design language URL (`docs/design/ux/design-language.md`)` — see `reference.md`
- `Step 2 — Restore missing / confirmed files` — see `reference.md`
- `Step 2.8 — Seed the dependency channel (idempotent, never-clobber)` — see `reference.md`
- `Step 2.8.1 — Seed the dependency-channel PRODUCER intent (library stacks only)` — see `reference.md`
- `Step 2.9 — Seed architecture-fitness rules (idempotent, never-clobber, #314)` — see `reference.md`
- `Step 3 — Guided interview` — see `reference.md`
- `Step 4 — Patch placeholders` — see `reference.md`
