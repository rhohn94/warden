<!-- STEALTH_SECTION:start -->
**Stealth Mode is ON** (`stealth-mode.value: "on"`). Leave **no AI/agent
fingerprint** in anything that reaches source control. This composes with the
active work paradigm — it does not change autonomy, only the artifacts you may
leave behind. Full design: `docs/design/stealth-mode-design.md`.

> **Ephemeral context (acknowledged).** Grimoire's own files (`.claude/`,
> `CLAUDE.md`, design docs, roadmap, release plans, config) are excluded locally
> (`.git/info/exclude`) and **never committed**. They live only in this working
> copy — deleting it loses the entire Grimoire context. This was disclosed and
> accepted at activation.

Operate by these five rules, in priority order:

1. **Never commit a Grimoire-managed path.** `.claude/`, `CLAUDE.md`, design
   docs, roadmap, version-history, release plans, hooks, config — all stay
   uncommitted. Stage and commit **only project-deliverable** files. (The
   `stealth-guard.sh` hook enforces this fail-closed.)
2. **No AI/agent tells in committed content.** No comment, identifier, commit
   message, or trailer in a committed deliverable file mentions Claude /
   Anthropic / "AI" / "LLM" / an agent, and **never** add a
   `Co-Authored-By: Claude` trailer. Run
   `python3 .claude/skills/grm-stealth-mode-switch/stealth_scrub.py scan --staged --strict`
   before each commit and fix anything it flags.
3. **Match the surrounding style over Grimoire's standards.** Mirror the
   existing code's and comments' conventions — naming, formatting, comment
   density and voice, even its imperfections — **ahead of** `docs/coding-standards.md`.
   Accept subpar code/comment quality when that is what makes the contribution
   indistinguishable from the code already in the file. The v1.26 merge-quality
   gates are advisory here; do not push the diff toward a recognizably-"clean AI"
   style that diverges from the repo norm. (Correctness/build still matter —
   broken code is also a tell.)
4. **Never push.** Do not run `git push` under any circumstance. The human
   pushes manually, outside Grimoire, if and when they decide to.
5. **Leave no dangling branch you created.** Work on the host repo's current
   branch (or one short-lived branch named to its own convention — never
   `version/*`, never `claude/*`; the Grimoire branch model is suppressed). At
   session start, snapshot the baseline
   (`stealth_scrub.py branches --baseline`); before session end, reconcile
   (merge/rebase into the intended branch) and **delete** every net-new branch
   (`stealth_scrub.py branches --strict` must report none).
<!-- STEALTH_SECTION:end -->
