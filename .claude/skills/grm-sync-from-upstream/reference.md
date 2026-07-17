# Sync-from-upstream — reference
Loaded on demand by `SKILL.md`.

## When to use this skill

- A project was started by copying `claude-code/` (or `copilot/`) out of this
  scaffolding, you've since customized it, and upstream has improved.
- You want the project to benefit from upstream skill/hook/doc fixes without
  losing your filled-in commands, branch names, or local edits.

Do **not** use it to push *from* a project into the scaffolding — that's
`grm-sync-from-source`. Do not run it inside the scaffolding repo itself.

### Pre-v3.42 projects: use bare skill names until the sync completes (#200)

Every skill name in this document and in `feature-manifest.md` is written with
its current `grm-` prefix (added by the `skill-namespacing` feature, v3.42).
**If the project being upgraded has `framework-version` below `"v3.42"` (or no
`framework-version` field at all), its `.claude/skills/` directory still holds
the OLD bare names** — `sync-from-upstream`, `config-validate`,
`install-doctor`, `structure-migrate`, `architecture-audit`, etc. The `grm-`
prefix does not exist on disk until a sync lands the rename, so invoking a
`grm-`-prefixed name at that point fails: the directory simply isn't there yet.

Resolve the chicken-and-egg this way:

- **This skill itself, and Steps 0–4 below** (locating/invoking the sync, the
  dry-run, `--apply`, conflict resolution) — invoke it as
  **`sync-from-upstream`** (path `.claude/skills/sync-from-upstream/
  sync-from-upstream.sh`), not `grm-sync-from-upstream`. It is the one skill
  that must always be reachable by its actual on-disk name — prefixed or not —
  because it is the thing that performs the rename.
- **Step 4.5 onward** (feature-manifest adoption loop) — any skill the
  manifest's `adopt`/`detect` prose names (`config-validate`,
  `install-doctor`, `structure-migrate`, `architecture-audit`, …) is invoked by
  its **bare** name too, for the same reason, until the `skill-namespacing`
  entry's own `adopt` step (`grm_namespacing.py --apply`) has actually run.
  Once that step completes, `.claude/skills/` holds only `grm-`-prefixed dirs,
  and every subsequent invocation (the rest of this sync, and all future ones)
  uses the `grm-*` name exactly as written everywhere else in this document.
- A quick on-disk check settles which regime a project is in:
  `ls .claude/skills/ | grep -vE '^(grm-|README|_)' || echo "(none — already namespaced)"`.
  Any bare survivor listed ⇒ use bare names for not-yet-adopted steps; empty
  output ⇒ use `grm-*` names throughout, as written.

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

- It is **not excluded** (it is not in `is_excluded`), so `NEW` / `in-sync` /
  `UPDATE` (upstream changed, local didn't) / `local` (local changed, upstream
  didn't) classification is the normal file-merge walk, unchanged.
- **Only** the "both sides changed since base" step is special-cased (#REG-4,
  v3.97). A plain textual `git merge-file` 3-way merge — the mechanism every
  *other* synced file uses — was the originally-designed approach here too,
  but it **false-conflicts on two disjoint component additions**: both sides'
  diffs touch the same closing-brace/trailing-comma region of the JSON
  `components` map, so `git merge-file` reports a `CONFLICT` even though the
  additions are semantically unrelated (verified empirically — reproduce with
  `component_registry_merge.py --self-test`, case "disjoint additions").
  `merge_component_registry()` in `sync-from-upstream.sh` routes this one
  artifact to `component_registry_merge.py` instead: a **structural,
  per-component-id** 3-way merge —
  - a component present on only one side (added/edited by exactly one side
    since base) is kept, never lost;
  - a component **unchanged locally since base** fast-forwards to upstream's
    value (including an upstream deletion);
  - a component **unchanged upstream since base** keeps the local value
    (including a local deletion);
  - a component changed to **different** content on **both** sides — a
    genuine same-id collision, including two independent additions of the
    same id with no recorded base entry — is a real `CONFLICT`: nothing is
    written to the local file (no textual diff3 markers embedded in the
    JSON, which would corrupt every *other* entry's schema), a structured
    `--conflicts-out` report names the colliding id with both sides' values,
    and resolution is manual, exactly like any other file's `CONFLICT`.
  Re-syncing an **unchanged** upstream registry is still a **no-op**.
- The **derived matrix** (`.claude/cache/component-compatibility.json`) is
  **not** distributed — `.claude/cache/` is gitignored and regenerable from the
  registry by the `grm-component-registry` skill after a sync changes it.
- No `feature-manifest.md` row is added here. A `grm-component-registry` adopt row
  (idempotent adopt step) is owned by **D2** (the closeout/flavor-mirror item);
  see the report. Until that row lands, the registry still distributes via the
  file-merge walk above — the manifest row only adds the post-sync *adopt/regen*
  prompt.

---

### Merge-walk warnings and auto-resolution (#180 / #181 / #420)

**MISSING-SYMBOL (#180) — call-site without definition.** A best-effort warning;
never blocks. A 3-way merge computes `diff(BASE,LOCAL)` and `diff(BASE,UPSTREAM)`
and applies both. When LOCAL deleted a helper definition and UPSTREAM only
touched a *different*, non-overlapping region that still *calls* that helper,
the two diffs don't overlap — so `git merge-file` emits **no conflict marker**
and the merged file ends up with a call-site whose definition is gone. The
result looks syntactically complete but is broken at runtime. After each
`MERGED`/`CONFLICT` result the script scans the merged output for any symbol
UPSTREAM **defines** that the merged output **calls** (whole-word) but
**defines nowhere**, and lists it. It is a heuristic (definition shapes:
`name()` / `function name` / `def name` / `class name`); it will not catch
every language idiom. On a hit: verify, and usually re-add the dropped
definition from `.scaffold-base/<file>`.

**RESOLVED — auto-advance on re-presentation (#181/#420).** A resolved
`CONFLICT` file's base is deliberately *not* advanced when first written (so an
unresolved conflict is never lost) — but that used to mean the SAME conflict
re-`CONFLICT`ed on **every subsequent sync**, overwriting your hand resolution
with fresh markers each time, even though nothing needed re-resolving. Fixed in
#420: the script fingerprints the exact `(base, upstream)` pairing the moment
it writes a `CONFLICT` (an untracked sentinel under
`.scaffold-conflict-pending/`, never synced or committed — like
`.scaffold-sync-state.json`). On a later `--apply`, if that EXACT pairing
re-presents and the LOCAL copy no longer contains conflict markers, the base is
auto-advanced to the current upstream content and LOCAL is left completely
untouched — reported as `RESOLVED`, not `CONFLICT`. Any change on either side
(a further upstream edit, or a base that already moved) invalidates the
fingerprint, so a genuinely new/different conflict on the same path is always
shown with markers again, never silently discarded.

**`--mark-resolved <file>`** advances the recorded base for **one** file to the
current upstream content immediately, without waiting for the next sync. Use it
after a blended resolution, or for a file **permanently diverged by design**
(e.g. a project-local branch name baked into a template). Unlike `--adopt-base`
(every file at once) it touches only that file's base; it refuses while
conflict markers are present, and accepts a project-relative or absolute path.

**`--all-resolved` (#420)** is the batch form: it applies the same rule as
`--mark-resolved` to **every** file the report loop would currently classify
`CONFLICT`, in one invocation. A file whose LOCAL copy still contains conflict
markers is reported and **skipped**, never force-resolved (the command exits
`1` if any were skipped, `0` if every conflicted file was resolved or none were
found); a file that already looks hand-resolved gets its base advanced, exactly
like the single-file form. Use it after resolving several `CONFLICT` files by
hand in one pass, instead of running `--mark-resolved` once per file.

### What the script tells you

`sync-from-upstream.sh` prints the `framework-version` recorded in
`.claude/grimoire-config.json` (or notes it is absent) and emits the manifest
path. It does **not** run `detect` predicates itself.

### How to evaluate the manifest (#396 — `adoption_delta.py`)

Run `adoption_delta.py` instead of reading the whole manifest table — it does
the delta computation below in-process and prints only the undecided rows:

```bash
python3 .claude/skills/grm-sync-from-upstream/adoption_delta.py \
    --manifest .claude/skills/grm-sync-from-upstream/feature-manifest.md \
    --project-root . --format table
```

What it does (mirrors the manual procedure this replaces):

1. Parses `feature-manifest.md`'s table rows (feature-id, introduced-in,
   summary, detect, adopt, migrate) — loudly (`ManifestError`, non-zero exit)
   on a structurally malformed row (wrong column count, an empty required
   field, or an unparseable `introduced-in` version) rather than silently
   skipping it.
2. **Delta computation:**
   - *With `framework-version`* (read from `--project-root`'s
     `.claude/grimoire-config.json`, or pass `--framework-version` directly):
     collects entries where `introduced-in` > `framework-version`. Runs each
     entry's `detect` predicate; excludes entries where `detect` returns true
     (already adopted).
   - *Without `framework-version`*: collects **all** entries, same `detect`
     run.
3. Sorts remaining entries by `introduced-in` ascending (oldest first — later
   features may depend on config set by earlier ones) and prints only those.

`detect` predicates are hand-written prose, not a formal DSL. `adoption_delta.py`
implements a best-effort predicate executor for the shapes that actually occur
in the manifest today (`` `path` exists ``, `` `path` contains `literal` ``,
`` `dir` contains only `prefix`-prefixed... ``, `` no `path` ``, a
`--self-test` command that passes, combined via AND/OR/parens) — see the
script's module docstring and
`docs/grimoire/design/token-efficiency-design.md` §Adoption-delta script for
the full grammar. A row whose `detect` text doesn't reduce to a recognized
shape is marked `detect_status: "unparseable"` and is **always included** in
the output (never silently dropped) — read that row's `detect` text yourself
and check it by hand; this is intentionally conservative (a false "already
adopted" is worse than one extra row to eyeball).

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
  line lacks. The integration line being merely **ahead** of `main` (no
  unreachable work on `main`'s side) proceeds by default (#419) — tree-identical
  is not required. This is the load-bearing safety property: a genuine fork —
  `main` holding work the integration line would lose — is always refused.
- **Rule 3c — separate commits.** Commit the framework-sync output as its OWN
  commit before running `grm-design-language-adapt` (Aura vendoring); never
  bundle both, so the collision surface stays small. **Mechanically enforced**
  (v3.67, #126 criterion 3) by `.claude/hooks/bundled-sync-guard.sh` — a
  PreToolUse(Bash) hook on `git commit` that denies a commit whose staged
  changes span both this skill's touch-set and `grm-design-language-adapt`'s
  touch-set at once. This reminder is the operator-facing half; the hook is
  the mechanical backstop that fires even if the reminder is ignored.

**Ahead-by-default (#419, v3.92) — the consumer-sync catch-22, retired.**
After any sync, the integration line carries the prior sync's own
`framework-version` bump (and any committed conflict resolution from Step 4), so
it is one or more commits **ahead** of `main`. Earlier versions of this rule
required the two lines to be tree-identical by default and demanded a
`--allow-ahead` flag to relax that — a flag whose name reads as a
[Safety Bypass Flag] to an autonomous harness classifier regardless of what it
actually did, and was observed teaching agents the bad habit of hunting a
script for a bypass-shaped escape hatch (#393). #419 retires the flag entirely:
Rule 3b now checks **only** the fork predicate
(`main_only_cherry_lines` / `cherry_lines_show_unreachable_work`) — the
integration line being merely **ahead** of `main` (a prior sync's own commit,
an un-promoted `framework-version` bump, or normal in-between-releases drift)
**proceeds by default**, no flag or token required. A real fork (`main`
carrying unreachable work) is **still refused** unconditionally, with a
merge-forward instruction — that check is the entire safety property and was
never relaxed by the flag, and isn't relaxed by its removal either:

```bash
.claude/skills/grm-sync-from-upstream/sync-from-upstream.sh --apply
```

When a HALT fires, `main` has genuinely diverged: reconcile by merging `main`
**into** the integration line (merge-forward); never `reset --hard` across the
fork (it discards the losing line's commits).

**The sync-continuation token (v3.90) — now a boundary record, not a gate.**
Before #419, the catch-22 above also bit *within a single sync flow*: the
`--apply` commit itself put the line ahead, so the same flow's follow-up runs
(a re-sync after resolving CONFLICT files, the adoption-phase re-run) demanded
`--allow-ahead` too. A clean-boundary `--apply` records `main`'s SHA in
`.scaffold-sync-state.json` (untracked local state; never synced, never
committed) purely as an operator-facing record of the last known boundary —
nothing in the guard path branches on it anymore, since #419 made every
ahead-only state proceed unconditionally once the fork predicate passes.

**Self-update on stale local copies (v3.91, #443).** A sync normally updates
itself as part of a project's regular `--apply` file-walk, but that update
only takes effect on the *next* invocation — the current one still runs
whatever guard logic the local copy shipped with. A repo whose local
`sync-from-upstream.sh` predates a boundary-guard fix (the ahead-by-default
change above, or any future one) can never sync the fix in, because the OLD
guard's stricter refusal blocks the very `--apply` that would deliver it —
verified in sim-game: `dev` ahead by exactly the sync commit, `main`
byte-identical to the merge base — the precisely-sanctioned scenario — still
hard-blocked by a pre-v3.90 guard. Before the BMI-3 guard runs, `--apply` now best-effort fetches
this script's newest bytes from upstream's `main` branch (always the newest
tooling — `UPSTREAM_REF` may pin an old release tag) and re-executes them:
GitHub remotes via `raw.githubusercontent.com` (no clone needed), local-path
transports by reading the file straight off disk. Any failure — no `curl`, a
non-GitHub/non-local-path remote, offline, identical content — silently falls
through to running the local copy; this is an opportunistic upgrade, never a
hard requirement. `SYNC_FROM_UPSTREAM_SELF_UPDATED=1` guards against a
self-update loop. A repo whose local copy predates v3.91 entirely (lacking
this self-update step itself) is outside what code alone can fix — it still
needs one human-present sync or one gated release to receive v3.91, same as
any other pre-fix straggler; from v3.91 onward, this class of trap can no
longer recur silently.

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


## Feature manifest — v3.53 additions

`manifest-version: 62` at the time. v3.53 shipped one new adoption feature:

- **`standard-justfile-recipes`** — Justfile contract: `build`, `run`, and
  `deploy` recipes with standard argument signatures. Projects with a `justfile`
  that still carry a `grimoire:placeholder` marker on those recipes are offered
  the adoption step, which instructs implementing them per
  `docs/design/justfile-standard-design.md` and verifying with `grm-install-doctor`.
