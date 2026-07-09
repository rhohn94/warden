---
name: grm-token-measure
description: Measure per-class token usage (input / output / cache_read / cache_creation) per operation from a Claude Code session .jsonl transcript, and emit the token-efficiency report table. Read-only. Use to capture before/after A/B numbers for an optimization, re-baseline a release, or answer "how many tokens did that cost".
---

# Token measure

Reusable measurement harness for the token-efficiency methodology
(`docs/grimoire/design/token-efficiency-design.md`). Parses a session transcript and
sums the four Anthropic token classes per operation, emitting the per-class
report table the design doc's measurement protocol requires. **Read-only** —
it parses `.jsonl` and prints a report; it mutates nothing.

## Method (resolved open question)

`.jsonl` `usage` fields **are** available — the primary method, not the
`budget.spent()` fallback. Claude Code writes transcripts to
`~/.claude/projects/<encoded-project-path>/*.jsonl`; each `assistant` record
carries `message.usage` with exactly:

| Class           | Key                           |
|-----------------|-------------------------------|
| input           | `input_tokens`                |
| output          | `output_tokens`               |
| cache_read      | `cache_read_input_tokens`     |
| cache_creation  | `cache_creation_input_tokens` |

Workflow fan-out subagents each get their own transcript under
`<session>/subagents/agent-*.jsonl`, so workflow agent usage is observable
per-agent. `<synthetic>` records carry zero usage and are skipped. **Records
sharing a `requestId` repeat identical usage** (streamed fragments of one
response) — the parser dedups by `requestId` to avoid over-counting.

## Usage

```bash
# whole-session total
python3 .claude/skills/grm-token-measure/parse_usage.py --session-only <transcript.jsonl>

# per-operation breakdown (each user-prompt turn is one operation)
python3 .claude/skills/grm-token-measure/parse_usage.py <transcript.jsonl>

# resolve the current session's transcript path deterministically
python3 .claude/skills/grm-token-measure/parse_usage.py --locate-transcript
```

**Locating the transcript.** Prefer `--locate-transcript`: it resolves the
newest `*.jsonl` under `~/.claude/projects/<encoded-cwd>/` deterministically —
the encoding replaces **each** non-alphanumeric character in the absolute cwd
with a single dash (so `/.claude` becomes `--claude`), and the script picks the
most recent transcript by mtime. Pass `--cwd <path>` to target a different
project root; it exits non-zero with a clear message if the directory or any
transcript is absent. This replaces hand-encoding the path and `ls`-ing for the
file. Pipe it straight in: `python3 …/parse_usage.py "$(python3 …/parse_usage.py
--locate-transcript)"`.

The script prints a markdown table. Redirect stdout to save it:
`… > /tmp/measure.md`. It never writes anywhere on its own.

## Report shape

Per the design doc: a per-operation table of the four classes plus a
**relative** `est. cost` roll-up (each class weighted by its rate, scaled by
the model-tier multiplier Haiku=1 / Sonnet=3 / Opus=15). The four raw class
counts are load-bearing; the cost column is a convenience estimate, not
dollars. Output is the costliest class and the multiplier bites hardest on it.

## Capturing a before/after A/B

1. Run the **same operation** before the change; capture its transcript table
   (use the per-operation mode and isolate the one operation).
2. Apply the optimization; run the identical operation again; capture again.
3. Report the per-class delta and the **percent estimated-cost reduction**.
   The design doc's acceptance floor is **≥20%** unless the change is
   near-zero-cost to apply.

Same inputs, same model (unless the change *is* a tier change), functionally
equivalent result — efficiency only. Note the cache state of each side; a
warm-cache and a cold-cache run of the same operation differ enormously.

## Baseline

Committed "before" numbers for representative operations live in
`docs/grimoire/token-efficiency-baseline.md`. Re-run this skill against fresh
transcripts to re-baseline in a future release.

## Fallback (Claude-Code-only)

For *live, in-Workflow* accounting where the transcript is not yet written,
the Workflow `budget.spent()` API is the only option, but it is output-oriented
and does not resolve the four classes. Prefer transcript parsing; flag any
`budget.spent()` number as output-only/approximate.

## Per-release token baseline (v1.29, #58)

A release can persist a **baseline** of its per-operation-class token numbers and
compare the next release against it.

- Baseline artifact: `.claude/cache/token-baseline.json` (derived, gitignorable).
  Shape: `{ "version": "v1.X", "classes": { "<op-class>": { "output": N,
  "cache_read": N, "cache_creation": N, "input": N } } }`.
- **Capture:** at release closeout, run `grm-token-measure` over the release session
  transcript and write the table to `token-baseline.json` (`--write-baseline`).
- **Compare:** at the next closeout, diff current vs baseline; **flag** any class
  whose `output` (the dominant cost lever) regressed beyond a threshold
  (default +15%, informational). A regression is a prompt to investigate, not a
  hard gate. Design: `docs/design/context-efficiency-design.md`.

## Static footprint (v3.37.2)

`footprint.py` (next to `parse_usage.py`) reports the **always-loaded** baseline
without a transcript — Σ skill descriptions + `CLAUDE.md` + per-skill body sizes,
with over-budget bodies flagged. Run it to re-baseline `token-efficiency-baseline.md`:
`python3 .claude/skills/grm-token-measure/footprint.py`.
