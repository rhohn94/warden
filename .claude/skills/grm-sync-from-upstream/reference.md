# Sync-from-upstream — reference
Loaded on demand by `SKILL.md`.

## When to use this skill

- A project was started by copying `claude-code/` (or `copilot/`) out of this
  scaffolding, you've since customized it, and upstream has improved.
- You want the project to benefit from upstream skill/hook/doc fixes without
  losing your filled-in commands, branch names, or local edits.

Do **not** use it to push *from* a project into the scaffolding — that's
`grm-sync-from-source`. Do not run it inside the scaffolding repo itself.

---

## Anti-patterns

- `--force` onto a dirty tree to "just get it done" — defeats the protection.
- Committing a file that still has `<<<<<<<` conflict markers — resolve first.
- `--adopt-base` to *skip* a real reconciliation — it declares "local already
  matches upstream"; only use it when that is true.
- Forgetting to re-specialize a `NEW` generic file — it will carry raw
  `{placeholder}` tokens until you do.
- Running it inside the scaffolding repo itself (wrong direction — use
  `grm-sync-from-source`).
- Deleting local-only files to "match upstream" — the sync is additive; your
  project-specific files are not upstream's concern.
### Stale-upstream rename detection (non-destructive)

The scaffolding repo was renamed `agentic-scaffolding` → `grimoire-framework`.
A project pinned before that rename also predates the multi-paradigm system, so
on every run the script checks `UPSTREAM_REPO` and, **if it still contains the
substring `agentic-scaffolding`**, prints a pre-sync notice that:

- names the rename and gives the exact new URL
  (`https://github.com/rhohn94/grimoire-framework.git`) plus the one-line
  repoint instruction (edit `UPSTREAM_REPO` in `.scaffold-upstream.conf`);
- points at the **paradigm system** now available for pre-paradigm scaffolds —
  the `grm-work-paradigm-switch` skill and `.claude/paradigms/README.md`.

It is **non-destructive**: the conf is never rewritten silently — the notice
only reports and *offers* the exact repoint line for you to apply. It is a
**no-op** once `UPSTREAM_REPO` already targets `grimoire-framework`, and it does
not change sync results or exit codes (pre-sync notice only).

**First run on an already-customized project:** there is no base yet, so every
differing file would report `REVIEW` (kept local, not merged). Once you have
confirmed the project is reconciled with a known upstream commit, record that
commit as the base so future syncs can 3-way merge:

```bash
.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh --adopt-base
```

`--adopt-base` snapshots the current upstream into `.scaffold-base/` and
**touches no local file**.

---

### Recognized sync artifact — `.claude/component-registry.json`

The versioned **component registry** (`.claude/component-registry.json`, schema
in `docs/grimoire/design/component-catalog-architecture-design.md` Pillar 1) is a
**recognized, merged sync artifact** — Pillar 4 (Distribution) of the
component-catalog architecture. It distributes over **this existing sync channel,
with no hosted endpoint**.

- It is **not excluded** (it is not in `is_excluded`), so the file-merge walk
  carries it like any other managed file: a `NEW` registry from upstream is
  added; a registry both sides changed is **3-way merged** against the recorded
  base, so **local components are preserved and upstream components are
  added/updated** — never clobbered. A genuine same-region collision (e.g. both
  sides edited the same component entry) surfaces as a `CONFLICT` for hand
  resolution, exactly like any other file.
- Because the JSON is a `components` map keyed by component-id, disjoint
  additions on each side merge cleanly (`MERGED`); the merge is *by version*
  through the normal diff — re-syncing an **unchanged** upstream registry is a
  **no-op**.
- The **derived matrix** (`.claude/cache/component-compatibility.json`) is
  **not** distributed — `.claude/cache/` is gitignored and regenerable from the
  registry by the `grm-component-registry` skill after a sync changes it.
- No `feature-manifest.md` row is added here. A `grm-component-registry` adopt row
  (idempotent adopt step) is owned by **D2** (the closeout/flavor-mirror item);
  see the report. Until that row lands, the registry still distributes via the
  file-merge walk above — the manifest row only adds the post-sync *adopt/regen*
  prompt.

---

### Merge-walk warnings (#180 / #181)

Two best-effort warnings the file-merge walk can emit. Both warn loudly and are
written to the summary; **neither blocks** the sync.

**MISSING-SYMBOL (#180) — call-site without definition.** A 3-way merge computes
`diff(BASE,LOCAL)` and `diff(BASE,UPSTREAM)` and applies both. When LOCAL deleted
a helper definition and UPSTREAM only touched a *different*, non-overlapping
region that still *calls* that helper, the two diffs don't overlap — so
`git merge-file` emits **no conflict marker** and the merged file ends up with a
call-site whose definition is gone. The result looks syntactically complete but
is broken at runtime. After each `MERGED`/`CONFLICT` result the script scans the
merged output for any symbol UPSTREAM **defines** that the merged output
**calls** (whole-word) but **defines nowhere**, and lists it. It is a heuristic
(definition shapes: `name()` / `function name` / `def name` / `class name`); it
will not catch every language idiom. On a hit: verify, and usually re-add the
dropped definition from `.scaffold-base/<file>`.

**MANUALLY-RESOLVED-BUT-BASE-NOT-ADVANCED (#181).** A resolved `CONFLICT` file's
base is deliberately *not* advanced (so an unresolved conflict is never lost). If
you hand-resolved it but did not advance the base, the next `--apply` sees the
same BASE-vs-LOCAL-vs-UPSTREAM and re-conflicts — overwriting your resolution. So
on `--apply`, when a file is about to re-`CONFLICT` but the LOCAL copy has **no**
conflict markers, the script warns and points at `--mark-resolved`.

**`--mark-resolved <file>`** advances the recorded base for **one** file to the
current upstream content, so future syncs stop re-merging it. Use it after a
blended resolution, or for a file **permanently diverged by design** (e.g. a
project-local branch name baked into a template). Unlike `--adopt-base` (every
file at once) it touches only that file's base; it refuses while conflict markers
are present, and accepts a project-relative or absolute path.

### What the script tells you

The script prints the `framework-version` recorded in
`.claude/grimoire-config.json` (or notes it is absent), emits the manifest
path, and summarizes the evaluation procedure. It does **not** run `detect`
predicates itself — that is your job as the agent.

### How to evaluate the manifest

1. Read `.claude/skills/grm-sync-from-upstream/feature-manifest.md`.
2. **Delta computation:**
   - *With `framework-version`*: collect entries where `introduced-in` >
     `framework-version`. Run each entry's `detect` predicate; skip entries
     where `detect` returns true (already adopted).
   - *Without `framework-version`*: collect **all** entries. Run each
     `detect`; skip entries that return true.
3. Sort remaining entries by `introduced-in` ascending (oldest first —
   later features may depend on config set by earlier ones).

### Advancing `framework-version`

> **`framework-version` ≠ `manifest-version`.** `framework-version` is the
> upstream **release string** (e.g. `"v3.42"`) and is what you write here.
> `manifest-version` is a bare integer counter at the top of
> `feature-manifest.md` used only to detect that the manifest itself changed —
> never copy it into `framework-version`. Writing the integer (e.g. `51`) into
> `framework-version` is a bug.

After the adopt loop completes without errors:

1. Determine the upstream's current version (e.g. from the manifest's highest
   `introduced-in` value, or from the upstream release tag).
2. If every feature up to that version was adopted successfully or
   `detect`-confirmed as already-adopted, write:
   ```json
   "framework-version": "<upstream-version>"
   ```
   into `.claude/grimoire-config.json`. **This is the only code path that
   writes `framework-version`** — the file-merge walk never touches it
   (`.claude/grimoire-config.json` is excluded from the sync walk).
3. If any feature errored or was skipped due to failure, advance
   `framework-version` only to the last fully-adopted version boundary. The
   next sync run will re-evaluate the failed feature by `detect` and resume.
4. User declining an optional adoption does **not** block `framework-version`
   advancement (the user made a conscious choice).

### Paradigm-file update caveat

If any file under `.claude/paradigms/` was `UPDATE`d during this sync, the
active paradigm content in its live paths (installed by `grm-work-paradigm-switch`)
may be stale. After the adoption phase, remind the user:

> Paradigm files updated. Re-run `grm-work-paradigm-switch` to re-install the
> active paradigm (`<paradigm-name>`) into its live paths.

This is a reminder, not an automated action.

### When the adoption phase is a no-op

If `detect` returns true for every manifest entry (all features already
adopted), print:

> Adoption phase: all features up to vX.Y are already adopted.

Then advance `framework-version` as above.

---

---

## BMI-3 boundary rules (full)

A framework sync writes generated content into the tree, so it is kept on the
single integration line and off a divergent tree. `--apply` enforces:

- **Rule 3a — integration line only.** HEAD must be the integration line
  (`branch-model.integration-branch`, default `dev`), not `main` or another
  branch. Switch to it and re-run.
- **Rule 3b — no real fork.** `main` must not carry tree content the integration
  line lacks. By default the two lines must also be **tree-identical** (a clean
  release boundary). This is the load-bearing safety property: a genuine fork —
  `main` holding work the integration line would lose — is always refused.
- **Rule 3c — separate commits.** Commit the framework-sync output as its OWN
  commit before running `grm-design-language-adapt` (Aura vendoring); never
  bundle both, so the collision surface stays small.

**The `--allow-ahead` escape hatch (consumer-sync catch-22, #144/#146/#162/#173).**
After any sync, the integration line carries the prior sync's own
`framework-version` bump (and any committed conflict resolution from Step 4), so
it is one or more commits **ahead** of `main`. Under the strict "tree-identical"
boundary, the *next* sync is then blocked until you cut a release — even though
no real fork exists. This also bites in environments where merging `dev -> main`
is restricted (CI, audit/upgrade tasks).

Pass `--allow-ahead` to relax Rule 3b from "tree-identical" to the model-aware
divergence predicate: the integration line being merely **ahead** of `main` is
permitted, while a real fork (`main` carrying unreachable work) is **still
refused** with a merge-forward instruction. It is safe because it never disables
the fork guard — it only stops penalizing the normal post-sync "ahead" state.
Use it for back-to-back syncs and constrained environments:

```bash
.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh --apply --allow-ahead
```

When a HALT *does* fire under `--allow-ahead`, `main` has genuinely diverged:
reconcile by merging `main` **into** the integration line (merge-forward); never
`reset --hard` across the fork (it discards the losing line's commits).
## Step 4.55 — Complete the grm- skill namespacing (remove bare-named survivors)

The file-walk **adds** the upstream `grm-*` skills but never deletes the old
bare-named dirs (the sync is non-destructive). A project that predates v3.42
therefore ends up holding BOTH `iterate/` and `grm-iterate/` after `--apply`,
and its sessions keep surfacing the stale bare names. Complete the cutover here.
This deterministic check is the **authority** — do not rely on the
`skill-namespacing` feature-manifest detect alone, which can read a stale
pre-rename manifest at the old `sync-from-upstream/` path and silently skip:

```bash
ls .claude/skills/ | grep -vE '^(grm-|README|_)' || echo "(none — cutover complete)"
```

If any survivor is listed, preview then **offer** the namespacing migrate
(**NEVER auto-run** — it archives + removes user-referenceable dirs and rewrites
references):

```bash
python3 .claude/skills/grm-sync-from-upstream/grm_namespacing.py --root . --dry-run
```

- **Noir:** offer once with a single confirmation, then run `--apply`.
- **Supervised / Weiss:** offer per the same prompt; on No, re-offer next sync.

`--apply` archives each stale dir to `.grimoire-archive/grm-namespacing-<ts>/`,
removes it (the synced `grm-*` copy stays authoritative — it never nests
`grm-<name>/<name>/`), and rewrites references per the two-tier rule. Re-run the
`ls` check after; it must report none. Then refresh `.grimoire-source/` (next
step) so the pristine source reflects the cleaned tree.

**Under Stealth Mode:** suppress the offer (skill writes must not reach source
control); leave survivors untouched.

