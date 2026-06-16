# Stealth Mode content set

Grimoire ships **Stealth Mode** — an orthogonal operating mode (independent of
the work paradigm) that lets Grimoire work on a codebase while leaving **zero
AI/agent fingerprints** in anything that reaches source control. This index is
always present so the mode — and its switch path — stay discoverable in-project
even when stealth is off.

| State | Active `CLAUDE.md` `## Stealth Mode` content |
|-------|---------------------------------------------|
| **off** (default) | `CLAUDE-stealth-off.md` — a one-line dormant pointer. |
| **on** | `CLAUDE-stealth-on.md` — the full five-pillar operating ruleset + risk re-disclosure. |

Only the *active* variant is installed into the `CLAUDE.md`
`<!-- STEALTH_SECTION:start --> … <!-- STEALTH_SECTION:end -->` block (lean by
design — an off-stealth project carries only the pointer). Switch with the
**`stealth-mode-switch`** skill; it also writes the managed-path exclusions into
`.git/info/exclude`, snapshots the branch baseline, and records the
ephemeral-context acknowledgement. The state is stored in
`.claude/grimoire-config.json` as `stealth-mode.value`.

Full design: `docs/design/stealth-mode-design.md`.
