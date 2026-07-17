# Grm-project-release — reference
Loaded on demand by `SKILL.md`.

### Refreshing the component registry (#458)

A disjoint step from **Reconcile issues** and **Post-release cleanup** in
`SKILL.md` — same trigger point (after the tag exists), different target:
keep `.claude/component-registry.json` from silently drifting out of sync
with its `component.json`/front-matter sources. One mechanical script call,
zero LLM judgment — the script's own diff decides whether there is anything
to commit:

```bash
# Skip entirely on a project with no component-catalog scan path and no
# existing registry (e.g. this repo itself — no components/lib directory,
# confirmed out of scope for self-population per release-planning-v3.97.md
# §4). Reuses the engine's own resolve_scan_paths/Discovery predicate so this
# pre-check agrees with grm-install-doctor's registry-freshness check on what
# "adopted" means.
if python3 -c "
import sys
sys.path.insert(0, '.claude/skills/grm-component-registry')
import component_registry as cr, os
paths = cr.resolve_scan_paths('.')
existing = any(os.path.isdir(p) for p in paths)
sys.exit(0 if existing or os.path.isfile(cr.REGISTRY_PATH) else 1)
"; then
  python3 .claude/skills/grm-component-registry/component_registry.py build --root . --stdout
  if [ -n "$(git status --porcelain -- .claude/component-registry.json)" ]; then
    git add .claude/component-registry.json
    git commit -m "chore(registry): refresh component registry post-release"
  fi
fi
```

`component_registry.py` is deterministic and idempotent
(`grm-component-registry` SKILL.md): a re-run against unchanged sources is
byte-identical, so nothing is committed when the registry was already fresh.
See `docs/grimoire/design/component-catalog-architecture-design.md` §Wiring.

### Uncataloged-must-not-grow gate (#459)

Runs immediately after the refresh above, in the same report-only,
mechanical style as `install_doctor.py`'s WARN/`--strict` checks (#433). It
is a DISJOINT check from the refresh — the refresh keeps the registry
byte-fresh against its current sources (a snapshot operation); this compares
the just-refreshed registry's `uncataloged` count against the registry as
committed at the previous release tag, so a strictly-worse count (more
metadata-less reusable units than last release) is a signal the write-time
`component.json` done-criteria isn't holding — worth a WARN, not an
emergency the release should retroactively block on:

```bash
if [ -f .claude/component-registry.json ]; then
  python3 .claude/skills/grm-project-release/uncataloged_gate.py check --root .
  # add --strict once this project's uncataloged backlog is cleared (e.g.
  # after a grm-component-backfill sweep, #460), to promote a growing count
  # from WARN to a hard release-blocking failure.
fi
```

Skips entirely — same guard as the refresh step above — when no registry
file exists (nothing to gate). Also degrades to a no-op (still exit 0, still
reported) when there is no previous release tag to compare against (the
project's first-ever tagged release) or when the registry didn't exist yet
at that previous tag — a first population is never itself "growth." A
present-but-malformed registry snapshot (current or baseline) is a real
authoring bug, not a degrade path, and exits 2. Exit code is otherwise
always 0 without `--strict`; with `--strict`, exits 1 when `uncataloged`
grew. Self-test: `uncataloged_gate.py --self-test` (fixture-registry
before/after snapshots covering growing, shrinking, stable, and
same-count-different-membership cases, plus the git-tag baseline-resolution
degrade paths against a throwaway temp repo).

### Anti-patterns

- Shipping an adoptable capability without a feature-manifest entry — syncing
  projects silently miss it and the capability lands inert.
- Hand-running the mechanical steps (marker, tag, build, `gh release create`)
  instead of the `release` recipe target — the ceremony exists so the marker is
  trap-guarded and the publish is asserted; ad-hoc runs re-open both gaps.
- Folding the push into the ceremony or adding a `just push` recipe — push
  stays a separate, guarded step.
