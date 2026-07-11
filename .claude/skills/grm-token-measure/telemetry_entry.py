#!/usr/bin/env python3
"""telemetry_entry.py — opt-in entry-point telemetry helper (#345, #346).

`docs/coding-standards.md` marks `telemetry-errors` (unhandled exceptions
emit telemetry) and `telemetry-cli-invocations` (invocations/flags/exit codes
are instrumented) as boundary rules for *release-boundary-invoked* skills
(`grm-release-phase-merge`, `grm-project-release` — see their SKILL.md
telemetry sections). Standalone skill scripts are not release-boundary
invocations, so instrumenting each one by hand would mean threading telemetry
calls through every script's `main()`. This module is the one-line opt-in that
closes that gap without touching each script's control flow: wrap `main` with
`instrument()` (decorator) and unhandled exceptions / clean exits emit
telemetry automatically.

On an **unhandled exception**, emits the sibling `run_metadata.py` per-run
artifact (`.claude/cache/runs/<run_id>.json`, the published contract — see
`run_metadata.py` module docstring) with `outcome="fail"`, plus a companion
context file (`.claude/cache/runs/<run_id>.context.json`, same dir, also
gitignored) carrying `argv`, `flags` (best-effort — `vars()` of an
`argparse.Namespace`-shaped positional argument, if the wrapped function
received one), and `exit_code`. The context file is intentionally NOT merged
into the published `run_metadata` schema (schema drift is a breaking change to
a documented contract) — it is an informational sibling artifact only.

On a **clean exit**, recording is opt-in via `record_success=True` (the
"optionally records argv + exit code" half of the standard) — most standalone
scripts run far more often than they fail, and a "pass" event per invocation
is not itself a signal worth the write on every run.

Telemetry emission is always best-effort: any failure to load `run_metadata`,
serialize, or write is swallowed so instrumentation never changes a wrapped
script's behavior or exit code. The decorated function's own exception
propagates unchanged after the (attempted) emit.

Usage (one line at the bottom of a skill script)::

    from telemetry_entry import instrument

    @instrument
    def main(argv=None):
        ...
        return 0

    if __name__ == "__main__":
        sys.exit(main())

Or, to also record clean exits::

    @instrument(record_success=True)
    def main(argv=None):
        ...

Non-Python (bash) callers at a release boundary — e.g. `scripts/release.sh`,
which drives the ceremony as shell, not as an importable `main()` — adopt the
same helper via its CLI emit mode instead of calling `run_metadata.py`
directly, so a mid-ceremony failure also gets an `outcome="fail"` + context
record, not just the existing success-path emit::

    python3 .claude/skills/grm-token-measure/telemetry_entry.py --emit \\
      --outcome fail --release "$version" --exit-code "$?" \\
      --note "step 5: build_distributables.py failed" \\
      --config .claude/grimoire-config.json 2>/dev/null || true

Usage (CLI):
  telemetry_entry.py --emit --outcome {pass,fail,partial} [--release X.Y]
                      [--exit-code N] [--note TEXT] [--root DIR]
                      [--config grimoire-config.json]
  telemetry_entry.py --self-test
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import sys
from typing import Callable

# ── Constants (no magic numbers inline) ─────────────────────────────────────
RUN_METADATA_FILENAME = "run_metadata.py"
CONTEXT_SUFFIX = ".context.json"
JSON_INDENT = 2
DEFAULT_ROOT = "."
# Exit code recorded for an unhandled exception (no exception carries its own
# process exit code, so this is the conventional Unix "error" code).
UNHANDLED_EXCEPTION_EXIT_CODE = 1


def _helper_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _load_run_metadata():
    """Load the sibling run_metadata.py module, or None if unavailable.

    Mirrors run_metadata.py's own `_load_parse_usage` importlib pattern so
    this helper stays a flat, dependency-free sibling file (portable to the
    copilot flat scripts/ layout) rather than requiring package-relative
    imports.
    """
    path = os.path.join(_helper_dir(), RUN_METADATA_FILENAME)
    if not os.path.isfile(path):
        return None
    mod_name = "tm_run_metadata"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # any import/exec failure → graceful degrade
        sys.modules.pop(mod_name, None)
        return None


def _flags_from_call(args, kwargs):
    """Best-effort recovery of parsed flags from the wrapped call's arguments.

    Looks for a positional or keyword argument shaped like an
    `argparse.Namespace` (anything exposing `__dict__` that isn't a plain
    container) and returns its `vars()`. Returns None when no such argument is
    present — flags are simply omitted from the context file, never guessed.
    """
    candidates = list(args) + list(kwargs.values())
    for candidate in candidates:
        if isinstance(candidate, (list, tuple, dict, set, str, bytes, int,
                                   float, bool)) or candidate is None:
            continue
        if hasattr(candidate, "__dict__"):
            try:
                flags = vars(candidate)
            except TypeError:
                continue
            if isinstance(flags, dict):
                return dict(flags)
    return None


def _write_context(root, run_id, argv, flags, exit_code, outcome):
    """Write the informational (non-contract) context sibling artifact."""
    runs_dir = os.path.join(root, ".claude", "cache", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    path = os.path.join(runs_dir, run_id + CONTEXT_SUFFIX)
    body = {
        "run_id": run_id,
        "outcome": outcome,
        "argv": argv,
        "flags": flags,
        "exit_code": exit_code,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body, indent=JSON_INDENT, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def _safe_emit(outcome, argv, flags, exit_code, root):
    """Emit the run_metadata artifact + context sibling; never raises."""
    try:
        mod = _load_run_metadata()
        if mod is None:
            return
        ctx = mod.RunContext(outcome=outcome)
        record = mod.RunMetadata(ctx, mod.TokenSplit.zero())
        record.save(root)
        _write_context(root, record.run_id, argv, flags, exit_code, outcome)
    except Exception:
        # Telemetry is best-effort by contract — never break the caller.
        pass


def instrument(func: Callable | None = None, *, record_success: bool = False,
               root: str = DEFAULT_ROOT) -> Callable:
    """Decorator: opt a skill script's `main()` into entry-point telemetry.

    - Unhandled exception → always emits outcome="fail" + context
      (argv/flags/exit_code=1), then re-raises unchanged.
    - Clean return → emits only when `record_success=True`; outcome is
      "pass" when the return value is 0/None, "fail" for any other
      integer return (the script's own exit-code convention).

    Usable bare (`@instrument`) or parameterized (`@instrument(record_success=True)`).
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            argv = list(sys.argv[1:])
            flags = _flags_from_call(args, kwargs)
            try:
                result = fn(*args, **kwargs)
            except SystemExit:
                raise
            except BaseException:
                _safe_emit("fail", argv, flags, UNHANDLED_EXCEPTION_EXIT_CODE, root)
                raise
            if record_success:
                exit_code = result if isinstance(result, int) else 0
                outcome = "pass" if exit_code == 0 else "fail"
                _safe_emit(outcome, argv, flags, exit_code, root)
            return result
        return wrapper

    if func is not None:
        return decorator(func)
    return decorator


# ── self-test ────────────────────────────────────────────────────────────────
def _self_test():
    import argparse
    import tempfile

    failures = []

    with tempfile.TemporaryDirectory() as d:
        runs_dir = os.path.join(d, ".claude", "cache", "runs")

        # Unhandled exception → fail artifact + context, exception re-raised.
        @instrument(root=d)
        def boom(argv=None):
            raise ValueError("kaboom")

        try:
            boom(["--x"])
            failures.append("boom() should have re-raised ValueError")
        except ValueError:
            pass

        if not os.path.isdir(runs_dir):
            failures.append("no runs/ dir written on exception path")
        else:
            files = os.listdir(runs_dir)
            fail_records = [f for f in files if f.endswith(".json")
                             and not f.endswith(CONTEXT_SUFFIX)]
            context_files = [f for f in files if f.endswith(CONTEXT_SUFFIX)]
            if not fail_records:
                failures.append("no run_metadata artifact written on exception")
            if not context_files:
                failures.append("no context artifact written on exception")
            else:
                with open(os.path.join(runs_dir, context_files[0]),
                           encoding="utf-8") as fh:
                    ctx = json.load(fh)
                if ctx["outcome"] != "fail":
                    failures.append("context outcome should be fail: %r" % ctx)
                if ctx["exit_code"] != UNHANDLED_EXCEPTION_EXIT_CODE:
                    failures.append("context exit_code should be 1: %r" % ctx)

        # SystemExit passes straight through, untouched.
        @instrument(root=d)
        def quits(argv=None):
            sys.exit(3)

        try:
            quits([])
            failures.append("quits() should have raised SystemExit")
        except SystemExit as exc:
            if exc.code != 3:
                failures.append("SystemExit code mutated: %r" % exc.code)

        # Clean exit, record_success=False (default) → no artifact written.
        with tempfile.TemporaryDirectory() as d2:

            @instrument(root=d2)
            def quiet_ok(argv=None):
                return 0

            quiet_ok([])
            if os.path.isdir(os.path.join(d2, ".claude", "cache", "runs")):
                failures.append("record_success=False should write nothing "
                                 "on a clean exit")

        # Clean exit, record_success=True → pass artifact recorded.
        with tempfile.TemporaryDirectory() as d3:

            @instrument(record_success=True, root=d3)
            def loud_ok(argv=None):
                return 0

            loud_ok([])
            runs3 = os.path.join(d3, ".claude", "cache", "runs")
            if not os.path.isdir(runs3) or not os.listdir(runs3):
                failures.append("record_success=True should write an artifact "
                                 "on a clean exit")

        # Flags recovered from an argparse.Namespace positional argument.
        ap = argparse.ArgumentParser()
        ap.add_argument("--flag", default="v")
        ns = ap.parse_args([])
        flags = _flags_from_call((ns,), {})
        if flags != {"flag": "v"}:
            failures.append("flags not recovered from Namespace: %r" % flags)

        # No Namespace-shaped argument → flags is None, not guessed.
        if _flags_from_call(("plain", 1), {"k": [1, 2]}) is not None:
            failures.append("flags should be None with no Namespace-like arg")

        # Bare @instrument (no parens) works identically to @instrument().
        @instrument
        def bare(argv=None):
            return 0

        # CLI --emit path (the bash/release-boundary adoption route).
        with tempfile.TemporaryDirectory() as d4:
            rc = main(["--emit", "--outcome", "fail", "--exit-code", "1",
                       "--note", "self-test", "--root", d4])
            if rc != 0:
                failures.append("--emit CLI path should return 0: %r" % rc)
            runs4 = os.path.join(d4, ".claude", "cache", "runs")
            if not os.path.isdir(runs4) or not os.listdir(runs4):
                failures.append("--emit CLI path wrote no artifact")

        if bare([]) != 0:
            failures.append("bare @instrument should behave transparently")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("telemetry_entry self-test: OK (exception→fail+context, SystemExit "
          "passthrough, opt-in clean-exit recording, flag recovery, bare "
          "decorator form)")
    return 0


def _cmd_emit_cli(args):
    """CLI emit path for non-Python (bash) release-boundary callers.

    Best-effort by contract, like everything else here: never raises, always
    returns 0 so a shell caller's `|| true` is belt-and-suspenders, not load
    bearing.
    """
    try:
        mod = _load_run_metadata()
        if mod is None:
            return 0
        paradigm = mod._config_value(args.config, "work-paradigm", "value") \
            if args.config else None
        profile = mod._config_value(args.config, "model-effort-profile", "value") \
            if args.config else None
        ctx = mod.RunContext(outcome=args.outcome, release=args.release,
                              paradigm=paradigm, profile=profile)
        record = mod.RunMetadata(ctx, mod.TokenSplit.zero())
        record.save(args.root)
        flags = {"note": args.note} if args.note else None
        _write_context(args.root, record.run_id, sys.argv[1:], flags,
                        args.exit_code, args.outcome)
    except Exception:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    if "--emit" in argv:
        import argparse
        ap = argparse.ArgumentParser(
            description="Emit a telemetry event from a shell (release-"
                        "boundary) context; see module docstring.")
        ap.add_argument("--emit", action="store_true")
        ap.add_argument("--outcome", choices=("pass", "fail", "partial"),
                         required=True)
        ap.add_argument("--root", default=DEFAULT_ROOT)
        ap.add_argument("--config")
        ap.add_argument("--release")
        ap.add_argument("--exit-code", dest="exit_code", type=int, default=0)
        ap.add_argument("--note")
        parsed = ap.parse_args(argv)
        return _cmd_emit_cli(parsed)
    print("telemetry_entry.py: import instrument() into a skill script, or "
          "use --emit from a shell caller; no other standalone CLI behavior "
          "beyond --self-test.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
