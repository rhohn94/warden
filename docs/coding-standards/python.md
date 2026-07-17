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
- Mark slow or integration tests with `@pytest.mark.slow` / `@pytest.mark.integration`.
- Name tests as plain sentences: `test_returns_empty_list_when_input_is_none`.
- `recipe.py test` / `just test` run the **full** suite (marked tests included).
  `recipe.py unit-test` / `just unit-test` (#360) run only the fast subset —
  `pytest -m "not slow and not integration"` — reusing these same marks; never
  invent a second marking convention for the fast-subset filter.
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

## Module size budget (#347)

Mirroring `rust.md`'s stated per-language budget (`rs-module-size`, ~400
lines): a Python module — in this repo, concretely a `.claude/skills/*/*.py`
skill script — should stay within **~800 lines**; split along internal seams
(one class/responsibility per module, per §Module & package structure above)
before it grows past that. Python's higher budget than Rust's reflects the
denser, more line-hungry nature of a stdlib-only CLI script (argparse
plumbing, a self-test harness, and docstring-as-documentation all live in the
same file here — see `docs/grimoire/design/scripting-unification-design.md`).

**Named tracked exceptions (pending a future split):**
- `.claude/skills/grm-doc-assurance/doc_assurance.py` (3539 lines) — five
  independent checks (flavor-parity, design-doc layout, link integrity, docs
  map, release consistency) share one CLI/report shell; splitting is tracked
  but not yet scheduled.
- `.claude/skills/grm-issue-tracker/issue_tracker.py` (2088 lines) — nine
  operations across multiple pluggable backends (roadmap, github) in one
  dispatcher; splitting per-backend is tracked but not yet scheduled.

Both exceed the ~800-line budget today; they are recorded here rather than
silently exempted so `grm-coding-practices-audit` / `arch-module-size` findings
against them are recognized as known, tracked debt and not re-litigated each
audit pass.

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

## Logging

See `../coding-standards.md` §Logging for the standard field contract
(`ts`/`level`/`target`/`msg`/`correlation_id`/`instance`/`version`, JSON-lines
to stdout). No Python-backed quick-start template exists in this repo yet to
carry a scaffolded copy of this module (every current profile ships
concretely Rust — see `quick-start-templates-design.md`), so this is a
**copy-paste reference implementation**: paste it as `logging_init.py` into a
Python project and call `init_logging(...)` once at process start.

```python
"""logging_init.py — standardized JSON-lines logging (docs/coding-standards.md
§Logging). ONE call at process start (init_logging); every
logging.getLogger(...).info(...) call site downstream needs no per-call
boilerplate.

Emits one JSON object per line to stdout with the standard field contract:
ts (ms since Unix epoch), level, target, msg, correlation_id, instance,
version. Level is set via instance config (the LOG_LEVEL env var / config
file, mirroring the Rust starter module's config.rs LOG_LEVEL knob).
Rotation stays the deployment supervisor's job — it already captures
stdout/stderr into logs/ (docs/web-app-deployment-protocol.md §4); this
module never opens or rotates a file itself.

`level` values are normalized to the same trace/debug/info/warn/error
vocabulary the Rust starter module emits (Python's own WARNING/CRITICAL
level names are remapped to warn/error) so a single log viewer (Admin
Console AC-4) can filter across a mixed Rust/Python fleet without a
per-language level-name table.

correlation_id is ambient via contextvars (safe across asyncio tasks and
threads, unlike a plain global) — request-handling code calls
set_correlation_id()/clear_correlation_id() once per request; every log line
emitted while it's set carries the id automatically, with no per-log-site
argument.

Copied per-project code, not a shared package — the cross-repo rule-of-two
policy (coding-standards.md §Cross-repo extraction policy) hasn't triggered:
nothing has been hand-rolled twice across sibling repos yet. Extract to a
standard package only once a second app duplicates this file.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from typing import Optional

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

# Normalizes Python's stdlib level names onto the trace/debug/info/warn/error
# vocabulary the Rust starter module emits (tracing::Level). Python has no
# TRACE level; DEBUG doubles for it. CRITICAL collapses onto error — a
# distinct "critical" tier isn't part of the cross-language contract.
_OUTPUT_LEVEL_NAMES = {
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "warn",
    "ERROR": "error",
    "CRITICAL": "error",
}

# Accepts the same input vocabulary as the Rust side (trace/debug/info/warn/
# error) plus Python's own names, so a shared LOG_LEVEL config value works
# for either language's starter module.
_INPUT_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.ERROR,
}


def set_correlation_id(value: str) -> None:
    """Set the correlation id for every log line emitted in this context
    (thread/async-task) until cleared. Call once at the top of a
    request/task; no per-log-site argument needed."""
    _correlation_id.set(value)


def clear_correlation_id() -> None:
    """Clear the current context's correlation id (e.g. end of a request)."""
    _correlation_id.set("")


class _JsonLineFormatter(logging.Formatter):
    """Formats one LogRecord as one JSON-lines object with the standard
    field contract: ts, level, target, msg, correlation_id, instance,
    version."""

    def __init__(self, instance: str, version: str) -> None:
        super().__init__()
        self._instance = instance
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": int(record.created * 1000),
            "level": _OUTPUT_LEVEL_NAMES.get(record.levelname,
                                              record.levelname.lower()),
            "target": record.name,
            "msg": record.getMessage(),
            "correlation_id": _correlation_id.get(),
            "instance": self._instance,
            "version": self._version,
        }
        return json.dumps(payload, ensure_ascii=False)


def init_logging(level: str = "info", instance: Optional[str] = None,
                  version: str = "0.0.0") -> None:
    """ONE call at process start (before any other logging call), no
    per-log-site boilerplate afterwards.

    `level` is the instance-config-resolved filter string (LOG_LEVEL env var
    / config file — trace/debug/info/warn/error, matched
    case-insensitively; an unrecognized value falls back to info). `instance`
    defaults to the INSTANCE_ID env var, falling back to "local" (mirrors the
    Rust starter module's instance_id() convention); `version` is stamped
    onto every emitted line.
    """
    if instance is None:
        instance = os.environ.get("INSTANCE_ID", "local")

    resolved_level = _INPUT_LEVELS.get(level.strip().lower(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonLineFormatter(instance=instance, version=version))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved_level)
```

## Audit hints

<!-- audit: id="py-no-bare-except" check="no bare `except:`; catch specific exceptions" severity="warn" applies="python" -->
<!-- audit: id="py-type-hints" check="public functions carry type hints" severity="info" applies="python" -->
<!-- audit: id="py-no-mutable-default" check="no mutable default arguments (def f(x=[]))" severity="warn" applies="python" -->
<!-- audit: id="py-pin-deps" check="dependencies are version-pinned in requirements/pyproject" severity="warn" applies="python" -->
<!-- audit: id="py-module-size" check="modules stay under ~800 lines (skill scripts); large modules split along internal seams — see §Module size budget for named tracked exceptions" severity="info" applies="python" -->
