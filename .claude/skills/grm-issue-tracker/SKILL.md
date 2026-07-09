---
name: grm-issue-tracker
description: Issue-tracker abstraction — nine operations (list/get/create/update/close/comment/label/search/ensure-label) over a normalized Issue object, with session-snapshot caching, multi-tracker routing and aggregation, and pluggable backends (roadmap default + github). Consumers call the helper script directly; the abstraction handles routing and caching transparently. Use when filing, listing, searching, or labelling issues.
---

# Issue Tracker

Pluggable, multi-target issue-tracker abstraction for Grimoire. Exposes seven
operations over a **normalized Issue object**; routes creates, aggregates list/search
across trackers, and caches reads per session. Two backends ship: `roadmap` (zero
network, reads/writes `docs/roadmap.md ## Backlog`) and `github` (wraps `gh`
with R1's recommended field-filtered, body-on-demand, server-side-filtered,
session-snapshot-cached access pattern).

Design authority: `docs/grimoire/design/issue-tracker-design.md`.

Cost rationale: `docs/grimoire/issue-tracker-cost-spike.md`.

---

## §0 — Preferred interface: the MCP server (v3.12)

When `mcp.enabled` + `mcp.prefer-for-tracker` (default **on**) and the
`grimoire-issue-tracker` server is registered (root `.mcp.json`), agents call
the **native MCP tools** instead of composing a CLI line: `list_issues`,
`get_issue`, `search_issues`, `create_issue`, `comment_issue`, `update_issue`,
`close_issue`, `label_issue` — thin wrappers over this same engine, with compact
body-on-demand responses (cheaper per call than reading this skill + shelling
out). **Fallback contract:** if MCP is disabled or the harness has no MCP, use
the `issue_tracker.py` CLI in §6 — identical engine, identical behaviour. Server
+ authoring template: `.claude/mcp-servers/README.md`.

---

## §1 — Normalized Issue object

Every backend produces and consumes this shape. No provider-specific fields leak
through.

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Globally unique within this tracker (GitHub: `"42"`; roadmap: slug) |
| `number` | `int \| null` | Provider-native number (GitHub issue number; null for roadmap) |
| `title` | `string` | One-line summary |
| `body` | `str \| null` | Full description. `null` = not yet fetched (body-on-demand). |
| `labels` | `list[str]` | Zero or more label strings |
| `state` | `"open" \| "closed"` | |
| `audience` | `"internal" \| "external"` | Which tracker population owns this issue |
| `tracker` | `string` | Tracker `name` this issue was loaded from / should be filed to |
| `url` | `str \| null` | Canonical URL (GitHub HTML URL; null for roadmap) |
| `created_at` | `str \| null` | ISO-8601 timestamp (GitHub); null for roadmap |

**`body: null`** signals the body was not fetched. Callers needing the body must
call `get()` explicitly (body-on-demand rule enforced at the type level).

---

## §2 — Nine operations

Every backend implements these methods; the abstraction layer wraps them with
caching and routing.

```
list(opts)                       → list[Issue]
get(id, opts?)                   → Issue          # always includes body
create(draft)                    → Issue
update(id, patch)                → Issue
close(id)                        → Issue
comment(id, body)                → Issue          # add a comment (v3.12)
label(id, add, remove)           → Issue
search(query, opts?)             → list[Issue]
ensure_label(name, tracker?)     → None           # create label if absent (v3.26)
```

**`ensure_label` per-provider:** github → `gh label create` (idempotent —
already-exists is success); roadmap → no-op (free-form labels, always valid);
grimoire → `not_implemented`. `create()` and `label()` call `ensure_label`
automatically for each requested label before applying it, so callers never
need to pre-create labels. CLI: `ensure-label <name> [--tracker <name>]`.

**ListOpts fields:**

| Field | Default | Notes |
|---|---|---|
| `tracker` | `null` | Filter to one named tracker (null = all) |
| `audience` | `null` | Filter by audience (null = all) |
| `state` | `"open"` | Passed server-side where supported |
| `labels` | `[]` | Server-side label filter |
| `limit` | `30` | Per-tracker cap (R1 §5 bounded `--limit ≤ 30`) |
| `include_body` | `false` | Always false in list; use `get()` for bodies |

---

## §3 — Backends

### `roadmap` backend (default)

Reads and writes `## Backlog` in `docs/roadmap.md`. **Zero network, zero `gh`
calls**. Behaviour identical to today for projects without `grm-issue-tracker` config.

- **list()**: extracts bullets from `## Backlog`; `id = slugify(title)`;
  `state = "open"`; `body = null`; `audience = "internal"`.
- **get()**: same extraction filtered by `id`; returns full bullet as `body`.
- **create()**: appends `- <title>` bullet; body as indented continuation;
  labels in trailing `<!-- labels: ... -->` HTML comment.
- **update()**: edits matching bullet in-place.
- **close()**: removes matching bullet (or moves to `## Closed` if present).
- **label()**: updates the labels HTML comment on matching bullet.
- **search()**: full-text match on title+body within Backlog bullets.

Only `## Backlog` is touched. `## Roadmap`, `## Framework-required`,
version-history sections are never modified.

### `github` backend

`gh`-based GitHub Issues backend. Implements R1 §5 access pattern verbatim:

1. **Field-filtered JSON + jq** — every list call uses `--json ... --jq ... @tsv`.
   Raw `gh issue list` output never reaches the agent.
2. **Body on demand** — `body` is never included in list queries; `get()` fetches
   it via `gh issue view N --json number,title,body,state,url`.
3. **Server-side filtering** — `--state`, `--label`, `--search` passed to `gh`
   before any post-filter.
4. **Session-snapshot cache** — see §4.
5. **Bounded `--limit ≤ 30`** — default 30; callers may lower via `ListOpts.limit`.
6. **Write batching** — multiple `label()`/`update()` calls on the same issue are
   coalesced into one `gh issue edit` on flush.

All calls prefix `--repo <tracker.repo>` (fully-qualified `owner/repo`).
Auth: `gh`'s ambient authentication (same as release skills).

### Future / stub

The config `provider` field accepts `"grimoire"` (reserved; no implementation in
v1.12). The interface is open to a third provider.

---

## §4 — Session-snapshot cache

Cache is **in-memory per session** (does not persist to disk; cannot go stale
across sessions). Crossover is K=2: any session reading the issue list twice or
more is cheaper with a snapshot (R1 §2).

**Cache key:** `(provider, repo, filter_hash)` where `filter_hash` is a stable
hash of `{state, labels, limit}`. Different filter combinations yield separate
entries.

**Warm read path:**
```
list(opts):
  key = cache_key(provider, repo, opts)
  if key in session_cache → return session_cache[key]   # ~34 tok warm
  result = backend.list(opts)                            # ~420 tok cold
  session_cache[key] = result
  return result
```

**Lazy invalidation on writes:** `create()`, `close()`, `label()`, `update()`
invalidate **all** filter variants for `(provider, repo, *)`. Cache is
invalidated after the write batch flush (not per queued write).

**Write batching:** the abstraction holds a pending-write buffer per
`(provider, repo, issue_id)`. The buffer flushes on session end or on an
explicit `flush()` CLI call. Cache invalidation happens post-flush.

**Multi-tracker aggregation:** for `list({tracker: null})`, each tracker is
cache-checked independently; live queries are issued only for cache misses;
results are merged in memory (sorted by `created_at` descending, then by
tracker name).

---

## §5 — Config block + routing

### Config block (`grimoire-config.json`)

The `grm-issue-tracker` block is **optional**. Absence means "roadmap default" —
forward-compatible with all existing configs (schema-version 3 unchanged).

```json
"issue-tracker": {
  "trackers": [
    {
      "name": "default",
      "provider": "roadmap",
      "repo": null,
      "audience": "internal",
      "labels": []
    }
  ],
  "default-for-filing": "default"
}
```

If the block is absent, the abstraction synthesizes this structure internally
(§5.2 of the design). No config write is needed.

### Create routing (ordered, first match wins)

1. **Explicit tracker name** — `IssueDraft.tracker` is non-null → route to that
   tracker. Error if name does not exist.
2. **Audience match** — `IssueDraft.audience` is non-null → first tracker whose
   `audience` equals the draft's audience.
3. **Default-for-filing** — fall through to `config["default-for-filing"]`.

### List/search aggregation

- `list(tracker="name")` — single tracker (own cache entry).
- `list(tracker=None)` — aggregate all trackers; deduplicate by `(provider, repo, id)`.
- `list(audience="external")` — aggregate trackers with matching `audience` only.
- `search(query)` — same tracker selection; merge results.

---

## §7 — Config path

The script reads `.claude/grimoire-config.json` relative to the **repo root**,
detected by walking up from `cwd` until `.claude/grimoire-config.json` is found.
Override with `--config <path>`.

---

## Reference (load on demand)

- `§6 — CLI usage (consumers call the helper script)` — see `reference.md`
- `Dispatch sizing from triage labels` — see `reference.md`
- `Creating and managing Epics` — see `reference.md`
- `Anti-patterns` — see `reference.md`
