# Python Standards

Per-technology coding standards for Python. Read alongside the cross-language
[standard practices](../coding-standards.md).

## Style & formatting

- Run `black` on every save and in CI; its output is authoritative — do not
  override formatting choices with `# fmt: off` without a documented reason.
- Sort imports with `isort` configured in `black`-compatible mode
  (`profile = "black"` in `pyproject.toml`).
- Target Python 3.11+ unless the project's runtime constraints require otherwise;
  document the minimum version in `pyproject.toml`.
- Use type annotations on every public function and method signature; `mypy` must
  pass in strict mode in CI.
- Follow PEP 8 naming: `snake_case` for functions, methods, variables, and
  modules; `CamelCase` for classes; `UPPER_CASE` for module-level constants.

## Linting

- Run `ruff` (the primary linter) in CI; treat every warning as an error
  (`--exit-non-zero-on-fix` or equivalent).
- Enable the following `ruff` rule groups as a baseline: `E`, `W` (pycodestyle),
  `F` (pyflakes), `I` (isort), `B` (bugbear), `UP` (pyupgrade).
- Suppress individual rules with `# noqa: <code>` plus an inline comment
  explaining why; never use bare `# noqa` or file-level disables.
- Run `mypy --strict` in CI for typed codebases.

## Error handling

- Raise specific exception types; never raise bare `Exception` or `BaseException`.
- Define project-specific exception classes inheriting from a common base (e.g.
  `class AppError(RuntimeError): …`) so callers can catch at the right level.
- Never silence exceptions with `except Exception: pass`; at minimum log the
  traceback and re-raise.
- Use context managers (`with`) for resource management (files, connections,
  locks) — do not rely on `__del__` for cleanup.

## Testing

- Framework: **pytest** with `pytest-cov` for coverage.
- Test files live in a top-level `tests/` directory mirroring the source layout:
  `src/mypackage/foo.py` → `tests/test_foo.py`.
- Use `conftest.py` for shared fixtures; keep fixtures small and focused.
- Mark slow or integration tests with `@pytest.mark.slow` / `@pytest.mark.integration`
  and exclude them from the default `pytest` run.
- Name tests as plain sentences: `test_returns_empty_list_when_input_is_none`.
- Achieve ≥ 80 % branch coverage on new code; enforce via `--cov-fail-under=80`.

## Module & package structure

- Use `src/` layout (`src/<package>/`) to prevent accidental imports from the
  working directory during development.
- One class or logical unit per module file; `__init__.py` exports only the
  public API surface.
- Avoid circular imports — restructure into a shared `_common` or `_utils`
  module if two modules need each other.
- Keep scripts in `scripts/` or behind a `[project.scripts]` entry point in
  `pyproject.toml`; no raw `python foo.py` invocations in CI.

## Dependency hygiene

- Declare dependencies in `pyproject.toml` (`[project.dependencies]`); pin to
  a compatible release range (`>=x.y,<x+1`).
- Use a lockfile (`pip-compile` → `requirements.lock`, or `uv lock`) committed
  to the repo.
- Separate dev/test extras from runtime dependencies via
  `[project.optional-dependencies]`.
- Run `pip-audit` or `safety` in CI; resolve every high/critical advisory before
  merging.
- Keep the dependency count low — prefer stdlib solutions before adding a
  third-party package.

## Telemetry hooks (where they integrate)

See `../coding-standards.md` §Telemetry for the project-type surface. In Python,
centralise emission behind one telemetry module:
- **Errors:** `sys.excepthook` for uncaught exceptions; framework error
  middleware (WSGI/ASGI, e.g. Starlette/Flask handlers) for request-scoped
  errors; structured logging via `logging` with a consistent formatter.
- **Startup:** emit an app-start event at process init (with version).
- **API/service:** per-endpoint counters + latency via middleware; propagate a
  trace/correlation id to downstream calls.
- **CLI:** wrap the entry point to record command, parsed flags, and exit code.


## Audit hints

<!-- audit: id="py-no-bare-except" check="no bare `except:`; catch specific exceptions" severity="warn" applies="python" -->
<!-- audit: id="py-type-hints" check="public functions carry type hints" severity="info" applies="python" -->
<!-- audit: id="py-no-mutable-default" check="no mutable default arguments (def f(x=[]))" severity="warn" applies="python" -->
<!-- audit: id="py-pin-deps" check="dependencies are version-pinned in requirements/pyproject" severity="warn" applies="python" -->
