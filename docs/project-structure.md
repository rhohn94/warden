# Standard Project Structure

> **Up:** [↑ Docs](README.md)

The canonical directory layout every Grimoire project adheres to. A single,
predictable shape means an agent dropped into any project already knows where
things live, and shared tooling can address files by their known place. This is
the project-facing contract; its rationale, rule schema, and migration mechanism
are specified in the upstream Grimoire framework design
(`file-structure-standard-design.md`).

> **Contract, not a literal tree.** The rules below are about *names and
> placement* — "if a project vendors dependencies, they live under
> `lib/third-party/`" — not "every project must contain every directory." This
> keeps the standard language-neutral. *Where things go inside `src/`* is the
> job of [`coding-standards/`](coding-standards/README.md) (e.g. Python's
> `src/<package>/` layout); this document governs the **top level**.

## The canonical tree

```
project-root/
├── .claude/              framework toolkit          (Grimoire-owned)
├── CLAUDE.md             operating contract          (Grimoire-owned)
├── docs/                 documentation tree          (framework + project)
├── src/                  first-party source code
├── tests/                test suites, mirroring src/
├── lib/                  libraries / dependencies
│   ├── first-party/      your own reusable libraries (extracted from src/)
│   └── third-party/      vendored external deps (grm-sync-deps target)
├── scripts/              dev / ops / automation scripts
├── config/               runtime configuration, schemas
├── assets/               static assets, fixtures, seed data
├── tools/                dev-only tooling (generators, internal CLIs)
├── examples/             runnable usage examples
├── dist/                 distributable build output   (git-ignored)
├── README.md  LICENSE  CHANGELOG.md  CONTRIBUTING.md
├── .gitignore  .editorconfig  .env.example
└── <stack manifest>     pyproject.toml / package.json / Cargo.toml / go.mod
```

## Directory contract

| Directory | Holds | Required |
|---|---|---|
| `src/` | first-party application / source code | **always** |
| `tests/` | test suites, mirroring `src/` | **always** |
| `docs/` | documentation (see [docs/README.md](README.md)) | **always** |
| `lib/first-party/` | the project's own reusable libraries | if any exist |
| `lib/third-party/` | vendored external dependencies | if any are vendored |
| `scripts/` | dev / ops / automation scripts | optional |
| `dist/` | distributable build output — **git-ignored** | if it builds one |
| `config/` | runtime configuration, schemas | optional |
| `assets/` | static assets, fixtures, seed data | optional |
| `tools/` | dev-only tooling not shipped to users | optional |
| `examples/` | runnable usage examples | optional (libraries: yes) |

`.claude/`, `CLAUDE.md`, and the framework parts of `docs/` are **Grimoire-owned**
— laid down and restored from the golden seed by `grm-workflow-bootstrap`.
Everything else is **project-owned** but follows the names above.

## Dependencies live under `lib/`

One home for libraries, split by provenance:

- **`lib/third-party/`** — external code vendored into the repo. This is where
  the vendor solution (`vendor.toml` + the **`grm-sync-deps`** skill) lands
  dependencies. Declare each dependency's `dest` as `lib/third-party/<dep>` in
  `vendor.toml`.
- **`lib/first-party/`** — code your own project owns and reuses across more than
  one surface, extracted out of `src/`.

> A top-level `vendor/` directory is the old convention and is **non-standard**
> now — relocate it to `lib/third-party/` with the **`grm-structure-migrate`**
> skill, which also rewrites the `vendor.toml` `dest`.

## Profile additions

The tree above is the universal spine; extend it per application profile:

- **API service** — `migrations/`, `api/` (specs), `deploy/`.
- **Web / GUI app** — `public/` or `static/` for served assets, plus the
  `docs/design/ux/` design-language tree and a `ux-demo/`.
- **CLI** — `man/` and/or `completions/`.
- **Library** — `examples/` becomes required; `benchmarks/` optional.

## Nonstandard names (relocate these)

If a project uses one of these, bring it to the standard home:

| Nonstandard | Standard |
|---|---|
| `vendor/`, `deps/`, `third_party/` | `lib/third-party/` |
| `test/`, `spec/` | `tests/` |
| `script/` | `scripts/` |
| `source/` | `src/` |
| `build/`, `out/` | `dist/` (and git-ignore it) |
| `static/`, `resources/` | `assets/` |

## Enforcement

- **Machine-readable rules** live in the `structure` block of
  `.claude/architecture-rules.json` (see
  [`architecture-rules.example.json`](../.claude/architecture-rules.example.json)).
- The **`grm-architecture-audit`** skill checks conformance as a fitness function
  (missing required dir, nonstandard dir, tracked build output).
- The **`grm-structure-migrate`** skill adapts an existing project to this layout
  (`git mv` relocations, `vendor.toml` `dest` rewrites) — report-only by default,
  `--apply` to perform the moves.

## See also

- [architecture-guidelines.md](architecture-guidelines.md) — module & boundary
  design (the *intra-`src/`* counterpart to this top-level contract).
- [coding-standards/](coding-standards/README.md) — per-language source layout.
- `.claude/architecture-rules.example.json` — the machine-readable `structure`
  block this contract encodes.
