# JavaScript / TypeScript Standards

Per-technology coding standards for JavaScript and TypeScript. Read alongside
the cross-language [standard practices](../coding-standards.md).

## Style & formatting

- Run `prettier` on every save and in CI; its output is authoritative — do not
  argue with formatting choices via inline overrides.
- Use TypeScript for all new code; plain `.js` files are acceptable only for
  config files where TypeScript tooling isn't available.
- Enable `"strict": true` in `tsconfig.json`; do not weaken individual strict
  flags without a documented justification.
- Prefer `const` over `let`; never use `var`.
- Use named exports over default exports — they survive refactoring and IDE
  navigation better.

## Linting

- Run `eslint` with the project ruleset in CI; every warning is treated as an
  error (`--max-warnings 0`).
- Extend from a shared config (e.g. `eslint:recommended` + `@typescript-eslint/recommended`
  for TypeScript projects) rather than hand-crafting rules from scratch.
- Suppress individual ESLint rules with `// eslint-disable-next-line <rule>`
  plus a comment explaining why; never use broad file-level disables.

## Error handling

- Never swallow errors with empty `catch` blocks; at minimum log the error and
  re-throw or surface it to the caller.
- Prefer explicit `Error` subclasses (or typed discriminated unions) over raw
  string error messages so callers can branch on type.
- In async code, every `Promise` must be awaited or have a `.catch()` handler;
  unhandled rejections are bugs.
- Avoid `any` as an escape hatch — use `unknown` and narrow with type guards.

## Testing

- Framework: **Vitest** for new projects (fast, native ESM, compatible Jest API);
  Jest for projects already using it.
- Test files are co-located with source: `foo.test.ts` sits next to `foo.ts`.
- Integration / end-to-end tests go in a top-level `tests/` or `e2e/` directory.
- Mock at the boundary (network, filesystem, time); do not mock internal module
  internals.
- Name test blocks with `describe` and `it`/`test` sentences that read as
  specifications: `it('returns empty array when input is null', …)`.

## Module & package structure

- One class or logical unit per file; the filename matches the default export (if
  any) or the primary concept.
- Group related files in directories with a barrel `index.ts` that re-exports the
  public surface.
- Keep the entry point (`index.ts` / `main.ts`) thin — import and wire; no logic.
- For monorepos, use npm/pnpm workspaces; keep each package independently
  buildable with its own `tsconfig.json`.

## Dependency hygiene

- Add dependencies after evaluating bundle size impact (use `bundlephobia` or
  equivalent); prefer zero-dependency utilities for small tasks.
- Separate `dependencies` from `devDependencies` strictly — nothing that only
  runs in CI or tests belongs in `dependencies`.
- Run `npm audit` / `pnpm audit` in CI; fix or document every high/critical
  finding before merging.
- Lock file (`package-lock.json` / `pnpm-lock.yaml`) is always committed.

## Telemetry hooks (where they integrate)

See `../coding-standards.md` §Telemetry for the project-type surface. In JS/TS,
wire the **always-on** tier through a single analytics wrapper module (never call
the provider SDK directly from feature code):
- **Errors:** `window.onerror` / `window.addEventListener('unhandledrejection')`
  in the browser; `process.on('uncaughtException')` / `'unhandledRejection'` in
  Node; a React error boundary for component trees.
- **Startup:** emit an app-start event from the entry module with the build
  version.
- **Web interactions:** a route-change listener (router hook) for page visits;
  delegate click/submit instrumentation through the wrapper, not inline handlers.


## Audit hints

<!-- audit: id="js-no-var" check="use const/let, never var" severity="warn" applies="javascript,typescript" -->
<!-- audit: id="js-strict-equality" check="use === / !== (no == coercion)" severity="warn" applies="javascript,typescript" -->
<!-- audit: id="js-no-floating-promise" check="promises are awaited or explicitly handled" severity="warn" applies="javascript,typescript" -->
<!-- audit: id="js-pin-deps" check="dependencies pinned / lockfile committed" severity="warn" applies="javascript,typescript" -->
