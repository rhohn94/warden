# files-manifest

Grimoire-owned files manifest and validator (CR-4).

## Purpose

Maintains `.claude/grimoire-files.json` — the authoritative, machine-readable
enumeration of every Grimoire-owned file in this flavor, tagged as
`pure-framework` / `mixed` / `project-owned`. CR-5's surgical `regenerate`
command consumes this manifest to partition files into delete+restore, split/merge,
and preserve sets.

Cross-references `.claude/skills/grm-workflow-bootstrap/manifest.md` (the existing
restorable-skill inventory) — this manifest extends it with class tags and covers
ALL framework-owned paths, not only the restorable skill subset.

## Validator

```bash
python3 .claude/skills/grm-files-manifest/validate_files_manifest.py [--root ROOT] [--flavor FLAVOR] [--strict]
python3 .claude/skills/grm-files-manifest/validate_files_manifest.py --self-test
```

Detects:
- **UNLISTED_PRESENT** — files in the tree that look Grimoire-owned but aren't
  in the manifest (potential omissions).
- **LISTED_ABSENT** — manifest entries with no matching file in the tree
  (stale entries or post-CR-3 relocations not yet applied).
- **MISTAG** — entries whose `class` disagrees with manifest.md's restorable-
  skill list (pure-framework expected; other classes flagged for review).
- **VERSION_STALE** — the manifest's `grimoire_version` disagrees with
  `.claude/grimoire-config.json`'s `framework-version` (leading `v` stripped
  from both before comparing). Catches the manifest silently drifting behind
  a release bump (#218). No finding when the config is missing/unreadable
  (cannot compare).

`--strict` exits 1 on any finding; default is informational exit 0.

## Schema

`.claude/grimoire-files.json`:

```json
{
  "schema_version": 1,
  "grimoire_version": "3.41",
  "flavor": "claude-code | copilot | root",
  "entries": [
    {
      "path": "path/or/glob/**",
      "class": "pure-framework | mixed | project-owned",
      "ships": true,
      "regenerate_disposition": "delete+restore from golden | split/merge | PRESERVE | ...",
      "notes": "optional"
    }
  ]
}
```

| Field | Values | Meaning |
|---|---|---|
| `class` | `pure-framework` | No project content; safe to delete + restore from golden. |
| `class` | `mixed` | Carries both framework baseline and project content; must split/merge per §2. |
| `class` | `project-owned` | Never touched by regenerate; listed only as exclusions. |
| `ships` | `true` | Included in the distributed flavor ZIP (not in EXCLUDED_PATH_PREFIXES). |
| `ships` | `false` | Excluded from distributables or root-only. |
| `regenerate_disposition` | string | CR-5 action contract per clean-room-design.md §1–§3. |

## Design reference

Design rationale (§1 taxonomy, §2 mixed-file split/merge contract, §3
surgical-regenerate contract, §4 operational-doc disposition) lives in the
upstream Grimoire repository (framework-internal — not shipped).
