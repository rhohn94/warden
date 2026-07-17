# Versioning — Design Document

> **Up:** [↑ Grimoire tier index](README.md)

> Defines how this project names, increments, surfaces, and records its
> versions. Customise §§2–4 for your project's specific version file and
> release tooling, then delete this note.

---

## 1. Goals

1. **One number the user sees.** A short string like `1.4` — recognisable,
   easy to quote in a bug report.
2. **One number the engineer sees.** A longer string that uniquely identifies a
   built binary, so two side-by-side installs cannot be confused.
3. **No drift between binary, docs, and changelog.** One source of truth in
   code; docs cite that source.
4. **Cheap.** Cutting a release is a single command; no manual scripting.
5. **Recoverable history.** A hand-written changelog plus per-release archived
   artifacts let you revisit any prior version.

---

## 2. Version anatomy

```
  MAJOR   .   MINOR   .   PATCH         (optional channel label)
  └──── public ────┘   └─ optional ─┘   └── optional adjective ──┘
```

| Component | Increments when… | Visible where |
|---|---|---|
| MAJOR | Breaking redesign / incompatible API change | Everywhere |
| MINOR | Each `dev` → `main` merge (new release) | Everywhere |
| PATCH | Hotfix on `main` without full release cycle | Everywhere |
| Channel | Maturity label advances (Alpha → Beta → stable) | About / README |

**Customise this table** for your project. Some projects use only MAJOR.MINOR;
others follow full SemVer. The important thing is that there is exactly one
source of truth in the codebase.

**Tag format (audit finding, v3.91).** The fleet-wide recommended tag format
is three-part `vMAJOR.MINOR.PATCH` — most Grimoire-managed projects already
ship this way, and it composes cleanly with a project that later adopts a
real PATCH lane (a hotfix on `main` between MINOR releases has nowhere to go
under a two-part scheme). A project that already tags two-part `vMAJOR.MINOR`
is fully conformant and is **never** required to migrate its history —
`grm-doc-assurance`'s `tag-format` check only warns (never blocks) when a
project's *newest* tag is still two-part, as a forward nudge toward new tags,
not a judgment on old ones.

---

## 3. Single source of truth

Pick ONE place to store the authoritative version:

| Ecosystem | Typical location |
|---|---|
| Node.js | `package.json` → `"version"` |
| Python | `pyproject.toml` → `[project] version` or `src/__version__.py` |
| Rust | `Cargo.toml` → `[package] version` |
| Go | `version.go` const or `cmd/version.go` |
| Generic | `VERSION` file at repo root |

The release script reads this file, bumps it, and propagates the value to
anywhere else it appears. Document that location here once you've decided:

> **Version file:** `{path/to/version/file}` — `{field name or format}`

---

## 4. Release procedure (`dev` → `main`)

> **Prerequisites:** {list any required tools, e.g. `npm`, `make`, specific
> CLI versions}.

### Before running the recipe

1. **Pick the next version.** MINOR for ordinary releases, MAJOR for breaking
   changes, PATCH for hotfixes.
2. **Write two entries, not one** (see §6): a complete record in
   `docs/version-history.md` (internal — may reference tickets and technical
   detail), and a clean, front-facing entry in `docs/changelog.md` (3–8
   bullets, user voice, no process leakage — this is what ships in release
   notes). Commit both on `dev`. The release script should refuse to run
   without either entry.
3. **Merge `dev` → `main`** and check out `main`.

### Running the recipe

```bash
{release-command}
```

Replace with your actual command (e.g. `npm version minor && npm publish`,
`make release VERSION=1.4`, `just release 1 4`).

A good release script:
- Verifies you are on `main` with a clean working tree.
- Verifies the version tag does not already exist.
- Verifies a matching `version-history.md` entry **and** a matching
  `changelog.md` entry both exist.
- Bumps the version in the version file (and any lockfiles).
- Runs the test suite.
- Builds release artifacts.
- Archives artifacts to a versioned folder.
- Commits the version bump.
- Tags the commit `v{MAJOR}.{MINOR}.{PATCH}` (the fleet-wide recommended
  format — see §2; a project may keep two-part `v{MAJOR}.{MINOR}` instead,
  it just won't be the newest-tag default going forward).

### After

```bash
git push && git push --tags
```

Confirm with the user before pushing.

---

## 5. What does *not* trigger a version bump

- Internal commits on `dev` that haven't been merged to `main` yet.
- Documentation-only changes, README rewording, etc.
- Local work branches and worktrees.

---

## 6. Two release records, two audiences

Every release writes **both** files — they serve different readers and
different rules apply:

| | `docs/version-history.md` | `docs/changelog.md` |
|---|---|---|
| Audience | Engineers / maintainers | End users |
| May reference tickets, internal task names, technical detail | Yes | **No** — see `docs/coding-standards.md` §Content & UI copy |
| Shown to consumers | Never | Yes (release notes, GitHub Release body) |
| Extracted by `grm-project-release` for the GitHub Release | No | Yes |

```markdown
## v1.4 — {release date}

- {User-facing change 1}
- {User-facing change 2}
- …
```

Same heading format (`## vX.Y — title`, one section per release, newest
first) in both files. `version-history.md` entries may be as detailed and
ticket-heavy as useful for engineering traceability; `changelog.md` entries
are 3–8 bullets in plain user-facing language, with zero process leakage.
