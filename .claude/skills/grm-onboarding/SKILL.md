---
name: grm-onboarding
description: First-run Grimoire onboarding interview. Captures project name, the execution dials, and issue-tracker choice into `.claude/grimoire-config.json`, then bridges into first-release planning. Implements `RUN NON-INTERACTIVE ONBOARDING` (legacy `SKIP ONBOARDING` accepted), incl. a committed root `KICKOFF.md`/`FIRST-RELEASE-PROMPT.md` trigger. Use when the sentinel fires on `CLAUDE.md` line 1.
---

# Onboarding

Runs the first-time project setup interview for a freshly copied Grimoire
scaffold. Produces `.claude/grimoire-config.json`, calls `grm-repo-init` and
`grm-workflow-bootstrap`, removes the sentinel so the flow never re-triggers, seeds
the framework-required baseline capabilities into `docs/roadmap.md` (§6.5), and
finally bridges into first-release planning (auto under Noir; prompt-offer
under Supervised/Weiss — §7).

Design authority: `docs/grimoire/design/onboarding-design.md`.

---

## Entry points

Two paths depending on the **effective first prompt** — the live chat prompt,
or a committed root `KICKOFF.md`/`FIRST-RELEASE-PROMPT.md` carrying the
trigger (§2.0 — makes a committed kickoff artifact self-executing, not inert):

| Path | Condition | Section |
|------|-----------|---------|
| **Interactive** | Sentinel present; effective first prompt lacks the trigger | §1 |
| **Non-interactive** | Sentinel present; effective first prompt has the trigger (`RUN NON-INTERACTIVE ONBOARDING` / legacy `SKIP ONBOARDING`), live or via kickoff file | §2 |

Both paths **begin** with the git-repo-init prerequisite (§0), then write the
config (§3), activate the paradigm (§3.1), the model/effort profile (§3.2), the
execution strategy (§3.3), — if a non-roadmap tracker was chosen — the
issue tracker (§3.4), the release-phase model (§3.5), call `grm-repo-init` +
`grm-workflow-bootstrap` (§4), remove the sentinel (§5), and end with the
first-release-planning bridge (§7) — the last onboarding phase.

**Runtime order of the lifecycle steps:** §0 git-init → §3 write config →
§3.1 activate paradigm → §3.2 activate model/effort profile → §3.3 activate
execution strategy → §3.4 activate issue tracker (if non-roadmap) →
§3.5 activate release-phase model →
§4 `grm-repo-init`+`grm-workflow-bootstrap` → §5 remove sentinel →
§6.5 baseline-roadmap seeding → §7 first-release-planning bridge.
The bridge is always the **final** step; it plans from an already-seeded
roadmap and tolerates an unseeded one gracefully (§7.4).

---

## §0 — Git-repo-init prerequisite (runs first, both paths)

This is the **first** onboarding step on both the interactive (§1) and
non-interactive (§2 — `RUN NON-INTERACTIVE ONBOARDING` / legacy `SKIP
ONBOARDING`) paths — it precedes everything else because the config file (§3)
and every later commit must live inside a git repository. Design authority:
`docs/grimoire/design/onboarding-design.md` §7.

### 0.1 Detect

```bash
git rev-parse --is-inside-work-tree 2>/dev/null
```

- Exit 0 / `true` → a repo already exists → **skip §0 entirely** (idempotent,
  §0.4): no `git init`, no extra commit, no confirmation prompt. Continue to §3.
- Non-zero → no repo → continue to §0.2.

### 0.2 Confirm before init (mandatory)

`git init` is a filesystem-mutating, repo-creating act and must **never** run
silently on the interactive path — the user may be in the wrong directory, or
intend to add the scaffold to an existing repo elsewhere.

- **Interactive path (§1):** ask with `AskUserQuestion` —
  > "This folder isn't a git repository yet. Initialize one now (`git init` +
  > an initial scaffold commit)? Yes / No."

  On **No**, stop onboarding with a brief message ("Onboarding paused — no git
  repository was created. Run onboarding again when ready, or `git init`
  yourself first.") and do **not** init or mutate anything.
- **Non-interactive path (§2):** the presence of the trigger — typed live or
  read from a committed kickoff file (§2.0) — is implied consent to
  non-interactive setup, but **still announce it**:
  > "No git repo found; initializing one (the non-interactive trigger implies
  > consent)."

### 0.3 Bootstrap the repo

On confirmation (or implied consent under the non-interactive trigger):

```bash
git init -b main          # mirror repo-init's default-branch choice
git add -A
git commit -m "chore: initial Grimoire scaffold"   # one sentence, no Co-Authored-By trailer
```

This produces **a repo on `main` with one commit** — nothing more. Do **not**
create `dev` / `version/*` here; that is `grm-repo-init`'s job (§4), and its
fail-soft guard now passes because the repo exists.

### 0.4 Idempotent already-a-repo case

If §0.1 detected an existing repo, §0 is skipped wholesale — no second
`git init`, no extra commit, no prompt. A repo with commits but without the
Grimoire branch model is **not** re-initialized here; `grm-repo-init` (§4) brings up
`dev` / `version/*` if missing. Re-running onboarding on an already-initialized
project is always safe.

---

## §3.1 — Activate the selected paradigm

**Immediately after** writing `.claude/grimoire-config.json`, run the
`grm-work-paradigm-switch` skill with the captured `work-paradigm.value`.

This installs the paradigm's content set into the active paths (skill files,
`CLAUDE.md` sections, `docs/grimoire/integration-workflow.md`) and migrates the config
to schema-version 2 (drops `work-paradigm.in-development`, bumps
`schema-version`). The result: the installed content is already paradigm-correct
before `grm-workflow-bootstrap` runs.

**If `.claude/paradigms/<paradigm>/` does not exist yet** (e.g. a freshly
copied scaffold before WP2 content is available): the switch skill will warn
and exit without error. Log the warning and continue — paradigm content will
be installed when `workflow-bootstrap --restore` runs with a populated golden
baseline.

---

## §4 — Call `grm-repo-init` then `grm-workflow-bootstrap`

### 4.1 `grm-repo-init`

Check whether `main` and `dev` branches already exist:

```bash
git branch --list main dev
```

- If both exist: skip `grm-repo-init` (already initialized).
- If either is missing: run the `grm-repo-init` skill.

### 4.2 `grm-workflow-bootstrap`

Run the `grm-workflow-bootstrap` skill. Pass the GUI-presence answer captured in
§1 step 4 (or inferred in §2) so `grm-workflow-bootstrap` skips its own GUI
question and uses the captured value. **Also pass the confirmed web-app answer**
(the Step 4 web-app fact, v3.26): if onboarding already wrote a `web-app` block
to the config, `grm-workflow-bootstrap`'s Q9 persistence step (its Step 3) is a
no-op — the block is already recorded; it must not re-detect or overwrite a
confirmed answer. All other `grm-workflow-bootstrap` interview questions (test/build/
release commands, doc-location map, etc.) proceed normally — the grm-onboarding skill
does not suppress them.

As part of its placeholder patching, `grm-workflow-bootstrap` fills the `CLAUDE.md`
`## Paradigm` stamp from `work-paradigm.value` (the value §3 already wrote, so
the loaded-context breadcrumb and the stored config never disagree) and always
delivers the `.claude/paradigms/README.md` breadcrumb — both idempotent
(match-and-replace the stamp value; rewrite the breadcrumb from golden). The
grm-onboarding skill does not patch `CLAUDE.md` itself.

---

## §5 — Remove the sentinel (idempotent)

As the **final step** of both interactive and non-interactive paths, after
`grm-workflow-bootstrap` completes, strip the **entire** onboarding-sentinel
apparatus from `CLAUDE.md` — the sentinel line AND the whole `## Onboarding
sentinel (…)` section, not just line 1. The only path that ever re-arms the
sentinel is `grm-hard-reset`, which restores `CLAUDE.md` wholesale from the
golden scaffold (which carries its own full section) — so nothing needs to
persist in an already-onboarded project's live `CLAUDE.md` for that path to
keep working.

1. If line 1 of `CLAUDE.md` is exactly `<!-- GRIMOIRE_ONBOARDING_SENTINEL -->`,
   delete that line.
2. Delete the entire `## Onboarding sentinel (…)` section — from that heading
   through the paragraph immediately before the next `## ` heading.
3. Both deletions are independently idempotent: if either is already gone,
   that step is a no-op, never an error.

```bash
# Safe idempotent removal: strips the sentinel line AND the whole
# "## Onboarding sentinel" section, not just line 1.
python3 - <<'EOF'
import pathlib, re
p = pathlib.Path('CLAUDE.md')
text = p.read_text()
if text.startswith('<!-- GRIMOIRE_ONBOARDING_SENTINEL -->\n'):
    text = text[len('<!-- GRIMOIRE_ONBOARDING_SENTINEL -->\n'):]
text = re.sub(
    r'\n## Onboarding sentinel \([^\n]*\)\n.*?(?=\n## |\Z)',
    '',
    text,
    count=1,
    flags=re.DOTALL,
)
p.write_text(text)
EOF
```

After removal, confirm to the user:

> "Onboarding complete. Your project config is at `.claude/grimoire-config.json`."

---

## §7 — First-release-planning bridge (final phase, both paths)

This is the **last** onboarding phase, appended after sentinel removal (§5) and
after the baseline-roadmap seeding step (§6.5, which runs before this bridge at
runtime). The project is now fully
initialized — branch model, guards, paradigm content, and (once F3 lands) a
seeded `docs/roadmap.md`. Rather than idling at "initialized", onboarding flows
directly into *first-release planning*. Design authority:
`docs/grimoire/design/onboarding-design.md` §6.

The bridge **reuses the existing release skills** — it does not re-implement
planning:

- `grm-release-planning` — propose work items from the roadmap.
- `grm-release-agreement` — lock the plan, write `docs/release-planning/release-planning-v{X.Y}.md`,
  and cut `version/{X.Y}`.

The integration master role (`.claude/skills/grm-integration-master/SKILL.md`) owns
this phase.

## Reference (load on demand)

- `§6 — Config schema notes (forward compatibility)` — see `reference.md`
- `§6.5 — Baseline-roadmap seeding (runs after §5, before §7)` — see `reference.md`
- `6.5.1 Determine project shape` — see `reference.md`
- `6.5.2 Select and seed the rows` — see `reference.md`
- `Framework-required (baseline)` — see `reference.md`
- `6.5.3 Tagging contract` — see `reference.md`
- `6.5.4 Additive, idempotent re-seed` — see `reference.md`
- `6.5.5 GUI cross-reference to the UX tier` — see `reference.md`
- `6.5.6 Ordering (F3 seeds, then F1 plans)` — see `reference.md`
- `6.5.7 Web-app catalog filing (conditional — web-app projects only)` — see `reference.md`
- `Anti-patterns` — see `reference.md`
- `Default label taxonomy seeding (v1.31, #69)` — see `reference.md`
- `7.1 Paradigm-conditional behaviour` — see `reference.md`
- `7.2 `RUN NON-INTERACTIVE ONBOARDING` interaction` — see `reference.md`
- `7.3 Where it hooks in the sequence` — see `reference.md`
- `7.4 Tolerating an unseeded roadmap` — see `reference.md`
- `§1 — Interactive interview` — see `reference.md`
- `§2 — Non-interactive path (`RUN NON-INTERACTIVE ONBOARDING` / legacy `SKIP ONBOARDING`)` — see `reference.md`
- `§2.0 — Kickoff-file trigger (committed, non-chat)` — see `reference.md`
- `§3 — Write `.claude/grimoire-config.json`` — see `reference.md`
- `§3.2 — Activate the selected model/effort profile` — see `reference.md`
- `§3.3 — Activate the selected execution strategy` — see `reference.md`
- `§3.4 — Activate the issue tracker (conditional)` — see `reference.md`
- `§3.5 — Activate the release-phase model` — see `reference.md`
