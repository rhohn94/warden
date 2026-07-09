#!/usr/bin/env python3
"""fleet_conformance.py — Fleet Status Contract v1 conformance check.

Validates a JSON payload (from a file or a live endpoint) against the Fleet
Status Contract v1 spec (`docs/design/fleet-status-contract.md`).

Supports two modes:
  --self-test                   Run against built-in fixture JSONs; exit 0 on
                                pass, non-zero on failure.
  --url <URL> [--token <tok>]   Fetch `GET <URL>/fleet/v1/status` (with and
                                without the bearer token) and validate both
                                the full shape (authed) and the minimal shape
                                (unauthed). Never calls a live endpoint in
                                --self-test mode.

The validator checks both the full shape (authenticated) and the minimal liveness
subset (unauthenticated), and emits a structured list of findings.

Usage:
    # Fixture-based self-test (CI / offline):
    python3 fleet_conformance.py --self-test

    # Live endpoint (never called during --self-test):
    python3 fleet_conformance.py --url http://localhost:3000 --token <bearer>

Design: docs/design/fleet-status-contract.md
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from typing import Any, Optional


# ── Schema constants ───────────────────────────────────────────────────────────

SCHEMA_VERSION = "1"
VALID_STATUSES = {"up", "degraded", "starting", "draining"}
VALID_UPDATE_VERDICTS = {"UpToDate", "UpdateAvailable", "Unknown", "NotConfigured"}
VALID_DEP_KINDS = {"build", "runtime"}

# Fields MUST be absent from the minimal shape.
MINIMAL_FORBIDDEN_FIELDS = {
    "instance",
    "dependencies",
    "update",
}
MINIMAL_FORBIDDEN_BUILD_FIELDS = {"git_sha", "built_at"}
MINIMAL_FORBIDDEN_RUNTIME_FIELDS = {"bind", "started_at"}


# ── Finding collector ─────────────────────────────────────────────────────────

class ConformanceResult:
    """Accumulates findings for one validation run."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def report(self) -> str:
        lines = [f"[{self.label}]"]
        if not self.errors and not self.warnings:
            lines.append("  PASS — no findings.")
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        status = "PASS" if self.passed else "FAIL"
        lines.append(f"  → {status} ({len(self.errors)} errors, {len(self.warnings)} warnings)")
        return "\n".join(lines)


# ── Validator ─────────────────────────────────────────────────────────────────

class FleetConformanceValidator:
    """Validates Fleet Status Contract v1 payloads.

    Two entry points:
      validate_full(payload)    — full authenticated shape.
      validate_minimal(payload) — minimal unauthenticated liveness subset.
    """

    # ── Full shape ─────────────────────────────────────────────────────────

    def validate_full(self, payload: Any, label: str = "full-shape") -> ConformanceResult:
        """Validate the full Fleet Status Contract v1 JSON payload."""
        result = ConformanceResult(label)

        if not isinstance(payload, dict):
            result.error("payload is not a JSON object")
            return result

        # schema_version
        self._check_schema_version(payload, result)

        # app
        app = payload.get("app")
        if not isinstance(app, str) or not app:
            result.error("`app` must be a non-empty string")

        # instance block
        instance = payload.get("instance")
        if not isinstance(instance, dict):
            result.error("`instance` must be an object")
        else:
            self._check_instance_block(instance, result)

        # build block (full)
        build = payload.get("build")
        if not isinstance(build, dict):
            result.error("`build` must be an object")
        else:
            self._check_build_block_full(build, result)

        # runtime block (full)
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            result.error("`runtime` must be an object")
        else:
            self._check_runtime_block_full(runtime, result)

        # dependencies array
        deps = payload.get("dependencies")
        if not isinstance(deps, list):
            result.error("`dependencies` must be an array")
        else:
            for i, dep in enumerate(deps):
                self._check_dependency_entry(dep, i, result)

        # update block
        update = payload.get("update")
        if not isinstance(update, dict):
            result.error("`update` must be an object")
        else:
            self._check_update_block(update, result)

        return result

    # ── Minimal shape ──────────────────────────────────────────────────────

    def validate_minimal(self, payload: Any, label: str = "minimal-shape") -> ConformanceResult:
        """Validate the minimal (unauthenticated) liveness subset payload."""
        result = ConformanceResult(label)

        if not isinstance(payload, dict):
            result.error("payload is not a JSON object")
            return result

        # schema_version
        self._check_schema_version(payload, result)

        # app
        app = payload.get("app")
        if not isinstance(app, str) or not app:
            result.error("`app` must be a non-empty string")

        # build block (minimal — version only)
        build = payload.get("build")
        if not isinstance(build, dict):
            result.error("`build` must be an object")
        else:
            version = build.get("version")
            if not isinstance(version, str) or not version:
                result.error("`build.version` must be a non-empty string")
            # Sensitive fields MUST be absent.
            for forbidden in MINIMAL_FORBIDDEN_BUILD_FIELDS:
                if forbidden in build:
                    result.error(
                        f"`build.{forbidden}` MUST be absent from the minimal shape "
                        f"(sensitive field)"
                    )

        # runtime block (minimal — status only)
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            result.error("`runtime` must be an object")
        else:
            status = runtime.get("status")
            if status not in VALID_STATUSES:
                result.error(
                    f"`runtime.status` must be one of {sorted(VALID_STATUSES)!r}; "
                    f"got {status!r}"
                )
            for forbidden in MINIMAL_FORBIDDEN_RUNTIME_FIELDS:
                if forbidden in runtime:
                    result.error(
                        f"`runtime.{forbidden}` MUST be absent from the minimal shape "
                        f"(sensitive field)"
                    )

        # Forbidden top-level fields.
        for forbidden in MINIMAL_FORBIDDEN_FIELDS:
            if forbidden in payload:
                result.error(
                    f"`{forbidden}` MUST be absent from the minimal shape "
                    f"(sensitive field)"
                )

        return result

    # ── Field checkers ─────────────────────────────────────────────────────

    def _check_schema_version(self, payload: dict, result: ConformanceResult) -> None:
        sv = payload.get("schema_version")
        if sv != SCHEMA_VERSION:
            result.error(
                f"`schema_version` must be {SCHEMA_VERSION!r}; got {sv!r}"
            )

    def _check_instance_block(self, block: dict, result: ConformanceResult) -> None:
        for field in ("id", "name", "env"):
            v = block.get(field)
            if not isinstance(v, str) or not v:
                result.error(f"`instance.{field}` must be a non-empty string")

    def _check_build_block_full(self, block: dict, result: ConformanceResult) -> None:
        for field in ("version", "git_sha", "built_at"):
            v = block.get(field)
            if not isinstance(v, str) or not v:
                result.error(f"`build.{field}` must be a non-empty string")

    def _check_runtime_block_full(self, block: dict, result: ConformanceResult) -> None:
        status = block.get("status")
        if status not in VALID_STATUSES:
            result.error(
                f"`runtime.status` must be one of {sorted(VALID_STATUSES)!r}; "
                f"got {status!r}"
            )
        for field in ("bind", "started_at"):
            v = block.get(field)
            if not isinstance(v, str) or not v:
                result.error(f"`runtime.{field}` must be a non-empty string")

    def _check_dependency_entry(
        self, dep: Any, index: int, result: ConformanceResult
    ) -> None:
        prefix = f"`dependencies[{index}]`"
        if not isinstance(dep, dict):
            result.error(f"{prefix} must be an object")
            return

        name = dep.get("name")
        if not isinstance(name, str) or not name:
            result.error(f"{prefix}.`name` must be a non-empty string")

        kind = dep.get("kind")
        if kind not in VALID_DEP_KINDS:
            result.error(
                f"{prefix}.`kind` must be one of {sorted(VALID_DEP_KINDS)!r}; "
                f"got {kind!r}"
            )

        # runtime-only fields: warn if present on build deps
        if kind == "build":
            for runtime_only in ("endpoint", "reachable"):
                if dep.get(runtime_only) is not None:
                    result.warn(
                        f"{prefix}.`{runtime_only}` is a runtime-only field but is "
                        f"present on a build dependency"
                    )
        elif kind == "runtime":
            endpoint = dep.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                result.error(
                    f"{prefix}.`endpoint` must be a non-empty string for "
                    f"runtime dependencies"
                )
            reachable = dep.get("reachable")
            if not isinstance(reachable, bool):
                result.error(
                    f"{prefix}.`reachable` must be a boolean for runtime dependencies"
                )

    def _check_update_block(self, block: dict, result: ConformanceResult) -> None:
        channel = block.get("channel")
        if not isinstance(channel, str) or not channel:
            result.error("`update.channel` must be a non-empty string")

        verdict = block.get("verdict")
        if verdict not in VALID_UPDATE_VERDICTS:
            result.error(
                f"`update.verdict` must be one of "
                f"{sorted(VALID_UPDATE_VERDICTS)!r}; got {verdict!r}"
            )

        current = block.get("current")
        if not isinstance(current, str) or not current:
            result.error("`update.current` must be a non-empty string")

        # `available` / `last_checked`: absent key is equivalent to null (the
        # reference implementation omits them via skip_serializing_if); when
        # present they must be a string or null.
        for field in ("available", "last_checked"):
            value = block.get(field)
            if value is not None and not isinstance(value, str):
                result.error(f"`update.{field}` must be a string or null when present")


# ── Live-endpoint mode ─────────────────────────────────────────────────────────

def _fetch_json(url: str, token: Optional[str] = None) -> tuple[int, Any]:
    """Fetch JSON from a URL with an optional bearer token. Returns (status_code, body)."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
            return status, body
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:
        raise RuntimeError(f"request to {url!r} failed: {exc}") from exc


def run_live(base_url: str, token: Optional[str]) -> int:
    """Validate a live endpoint. Never called in --self-test mode."""
    validator = FleetConformanceValidator()
    endpoint = base_url.rstrip("/") + "/fleet/v1/status"
    all_passed = True

    # 1. Unauthenticated — must return 200 + minimal shape.
    print(f"Probing (unauthenticated): GET {endpoint}")
    unauth_code, unauth_body = _fetch_json(endpoint)
    if unauth_code != 200:
        print(f"  ERROR: expected 200, got {unauth_code} (endpoint MUST NEVER return 401)")
        all_passed = False
    else:
        r = validator.validate_minimal(unauth_body, label="unauthenticated")
        print(r.report())
        if not r.passed:
            all_passed = False

    # 2. Authenticated (if token supplied) — must return 200 + full shape.
    if token:
        print(f"\nProbing (authenticated): GET {endpoint}")
        auth_code, auth_body = _fetch_json(endpoint, token=token)
        if auth_code != 200:
            print(f"  ERROR: expected 200, got {auth_code}")
            all_passed = False
        else:
            r = validator.validate_full(auth_body, label="authenticated")
            print(r.report())
            if not r.passed:
                all_passed = False
    else:
        print("\n(No --token supplied; skipping authenticated shape check.)")

    return 0 if all_passed else 1


# ── Self-test fixtures ─────────────────────────────────────────────────────────

FIXTURE_FULL_VALID = {
    "schema_version": "1",
    "app": "familiar",
    "instance": {"id": "550e8400-e29b-41d4-a716-446655440000", "name": "prod-01", "env": "production"},
    "build": {"version": "1.20.0", "git_sha": "abc123def456", "built_at": "2026-06-01T12:00:00Z"},
    "runtime": {"status": "up", "bind": "127.0.0.1:3000", "started_at": "2026-06-10T08:30:00Z"},
    "dependencies": [
        {"name": "tokio", "kind": "build", "version": "1.37.0"},
        {
            "name": "ollama",
            "kind": "runtime",
            "endpoint": "http://127.0.0.1:11434",
            "reachable": True,
            "version": "0.3.12",
        },
    ],
    "update": {
        "channel": "stable",
        "verdict": "UpToDate",
        "current": "1.20.0",
        "available": None,
        "last_checked": "2026-06-10T06:00:00Z",
    },
}

FIXTURE_MINIMAL_VALID = {
    "schema_version": "1",
    "app": "familiar",
    "build": {"version": "1.20.0"},
    "runtime": {"status": "up"},
}

FIXTURE_FULL_MISSING_FIELDS = {
    "schema_version": "1",
    "app": "familiar",
    # instance missing
    "build": {"version": "1.20.0"},  # git_sha + built_at missing
    "runtime": {"status": "up"},  # bind + started_at missing
    "dependencies": [],
    # update missing
}

FIXTURE_MINIMAL_WITH_FORBIDDEN = {
    "schema_version": "1",
    "app": "familiar",
    "build": {"version": "1.20.0", "git_sha": "abc123"},  # git_sha MUST be absent
    "runtime": {"status": "up"},
    "instance": {"id": "x", "name": "y", "env": "local"},  # instance MUST be absent
}

FIXTURE_WRONG_SCHEMA_VERSION = {
    "schema_version": "99",
    "app": "familiar",
    "build": {"version": "1.20.0"},
    "runtime": {"status": "up"},
}

FIXTURE_INVALID_STATUS = {
    "schema_version": "1",
    "app": "familiar",
    "build": {"version": "1.20.0"},
    "runtime": {"status": "banana"},
}

FIXTURE_INVALID_VERDICT = {
    "schema_version": "1",
    "app": "familiar",
    "instance": {"id": "x", "name": "y", "env": "local"},
    "build": {"version": "1.20.0", "git_sha": "abc", "built_at": "2026-01-01T00:00:00Z"},
    "runtime": {"status": "up", "bind": "0.0.0.0:3000", "started_at": "2026-01-01T00:00:00Z"},
    "dependencies": [],
    "update": {
        "channel": "stable",
        "verdict": "BANANA",
        "current": "1.20.0",
        "available": None,
        "last_checked": None,
    },
}

FIXTURE_DEP_MISSING_ENDPOINT = {
    "schema_version": "1",
    "app": "familiar",
    "instance": {"id": "x", "name": "y", "env": "local"},
    "build": {"version": "1.20.0", "git_sha": "abc", "built_at": "2026-01-01T00:00:00Z"},
    "runtime": {"status": "up", "bind": "0.0.0.0:3000", "started_at": "2026-01-01T00:00:00Z"},
    "dependencies": [
        {
            "name": "ollama",
            "kind": "runtime",
            # endpoint and reachable missing — should produce errors
        }
    ],
    "update": {
        "channel": "stable",
        "verdict": "UpToDate",
        "current": "1.20.0",
        "available": None,
        "last_checked": None,
    },
}


FIXTURE_OMITTED_UPDATE_KEYS = {
    # fleet.rs serializes update.available / update.last_checked with
    # skip_serializing_if=Option::is_none — both keys ABSENT must validate
    # clean (absent ≡ null per the spec).
    "schema_version": "1",
    "app": "familiar",
    "instance": {"id": "x", "name": "y", "env": "local"},
    "build": {"version": "1.20.0", "git_sha": "abc", "built_at": "2026-01-01T00:00:00Z"},
    "runtime": {"status": "up", "bind": "0.0.0.0:3000", "started_at": "2026-01-01T00:00:00Z"},
    "dependencies": [],
    "update": {
        "channel": "stable",
        "verdict": "NotConfigured",
        "current": "1.20.0",
    },
}


def run_self_test() -> int:
    """Run conformance checks against built-in fixture payloads. Returns exit code."""
    validator = FleetConformanceValidator()
    failures: list[str] = []

    def expect_pass(result: ConformanceResult) -> None:
        if not result.passed:
            failures.append(f"UNEXPECTED FAIL — {result.label}:\n{result.report()}")
        else:
            print(f"  OK: {result.label}")

    def expect_fail(result: ConformanceResult, min_errors: int = 1) -> None:
        if result.passed or len(result.errors) < min_errors:
            failures.append(
                f"UNEXPECTED PASS (expected >= {min_errors} error(s)) — {result.label}:\n"
                f"{result.report()}"
            )
        else:
            print(f"  OK (expected failure): {result.label} — {len(result.errors)} error(s)")

    print("fleet_conformance.py --self-test")
    print()

    # ── Full shape: valid ──────────────────────────────────────────────────
    print("Group: full shape — valid payloads")
    expect_pass(validator.validate_full(FIXTURE_FULL_VALID, "full-valid"))
    expect_pass(
        validator.validate_full(FIXTURE_OMITTED_UPDATE_KEYS, "full-omitted-update-keys")
    )

    # ── Full shape: invalid ────────────────────────────────────────────────
    print("\nGroup: full shape — invalid payloads (each must produce >= 1 error)")
    expect_fail(
        validator.validate_full(FIXTURE_FULL_MISSING_FIELDS, "full-missing-fields"),
        min_errors=1,
    )
    expect_fail(
        validator.validate_full(FIXTURE_WRONG_SCHEMA_VERSION, "full-wrong-schema-version"),
        min_errors=1,
    )
    expect_fail(
        validator.validate_full(FIXTURE_INVALID_VERDICT, "full-invalid-verdict"),
        min_errors=1,
    )
    expect_fail(
        validator.validate_full(FIXTURE_DEP_MISSING_ENDPOINT, "full-dep-missing-endpoint"),
        min_errors=1,
    )

    # ── Minimal shape: valid ───────────────────────────────────────────────
    print("\nGroup: minimal shape — valid payloads")
    expect_pass(validator.validate_minimal(FIXTURE_MINIMAL_VALID, "minimal-valid"))

    # ── Minimal shape: invalid ─────────────────────────────────────────────
    print("\nGroup: minimal shape — invalid payloads (each must produce >= 1 error)")
    expect_fail(
        validator.validate_minimal(
            FIXTURE_MINIMAL_WITH_FORBIDDEN, "minimal-with-forbidden-fields"
        ),
        min_errors=2,  # git_sha in build + instance present
    )
    expect_fail(
        validator.validate_minimal(
            FIXTURE_WRONG_SCHEMA_VERSION, "minimal-wrong-schema-version"
        ),
        min_errors=1,
    )
    expect_fail(
        validator.validate_minimal(FIXTURE_INVALID_STATUS, "minimal-invalid-status"),
        min_errors=1,
    )

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} unexpected result(s):")
        for f in failures:
            print(f"\n{f}")
        return 1
    else:
        print("SELF-TEST PASSED.")
        return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fleet Status Contract v1 conformance check. "
            "See docs/design/fleet-status-contract.md."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="Run against built-in fixture JSONs (no network calls).",
    )
    mode.add_argument(
        "--url",
        metavar="URL",
        help="Base URL of the app (e.g. http://localhost:3000). Appends /fleet/v1/status.",
    )
    parser.add_argument(
        "--token",
        metavar="TOKEN",
        help="Bearer token for the authenticated shape check (used with --url).",
    )
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    else:
        return run_live(args.url, args.token)


if __name__ == "__main__":
    sys.exit(main())
