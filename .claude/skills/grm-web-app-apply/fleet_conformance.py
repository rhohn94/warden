#!/usr/bin/env python3
"""fleet_conformance.py — Fleet Status Contract v1 conformance check.

Validates a JSON payload (from a file or a live endpoint) against the Fleet
Status Contract v1 spec (`docs/design/fleet-status-contract.md`).

Supports two modes:
  --self-test                   Run against the committed validation vectors
                                under fleet-contract-vectors/ (manifest.json +
                                cases/*.json); exit 0 on pass, non-zero on
                                failure. The same vectors are the intended
                                self-test fixture set for the future
                                fleet-contract Rust crate (see
                                docs/grimoire/design/fleet-status-contract.md
                                §Crate spec pointer).
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
from pathlib import Path
from typing import Any, Optional

# Shared validation vectors (committed fixture payloads + expected outcomes),
# consumed here and intended for the future fleet-contract Rust crate's
# self-test too — see docs/grimoire/design/fleet-status-contract.md
# §Crate spec pointer. Kept alongside this script so the skill directory is
# self-contained.
VECTORS_DIR = Path(__file__).resolve().parent / "fleet-contract-vectors"


# ── Schema constants ───────────────────────────────────────────────────────────

SCHEMA_VERSION = "1"
# The prior schema_version consumers must still accept (§3.2 N/N-1 rule).
# "1" is the first revision, so there is no N-1 yet; set to "1" and bump
# SCHEMA_VERSION to "2" together whenever the contract's next revision ships.
PREVIOUS_SCHEMA_VERSION: Optional[str] = None
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


def check_schema_version_value(
    sv: Any,
    current: str = SCHEMA_VERSION,
    previous: Optional[str] = PREVIOUS_SCHEMA_VERSION,
) -> list[str]:
    """Pure N/N-1 tolerance check (§3.2) — parameterized over the accepted
    current/previous versions rather than hardcoding one value, so the same
    logic works before and after a future schema_version bump.

    Returns a list of error messages (empty ⇒ accepted). Kept standalone (not
    a method) so it can be self-tested with hypothetical (current, previous)
    pairs independent of this module's live SCHEMA_VERSION/PREVIOUS_SCHEMA_VERSION.
    """
    accepted = {v for v in (current, previous) if v}
    if sv not in accepted:
        return [
            f"`schema_version` must be one of {sorted(accepted)!r} "
            f"(N/N-1 tolerance); got {sv!r}"
        ]
    return []


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

        # framework_version / last_synced (additive-optional, §3.5)
        self._check_framework_sync_fields(payload, result)

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

        # framework_version / last_synced (additive-optional, non-sensitive —
        # allowed in the minimal shape too; §3.5)
        self._check_framework_sync_fields(payload, result)

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
        for msg in check_schema_version_value(sv):
            result.error(msg)

    def _check_framework_sync_fields(self, payload: dict, result: ConformanceResult) -> None:
        """Validate the optional, non-sensitive `framework_version` / `last_synced`
        fields (§1.2/§1.4/§3.5). Present on both full and minimal shapes; absent
        is valid (older projects predate the field)."""
        if "framework_version" in payload:
            fv = payload["framework_version"]
            if not isinstance(fv, str) or not fv:
                result.error("`framework_version` must be a non-empty string when present")
        if "last_synced" in payload:
            ls = payload["last_synced"]
            if ls is not None and not isinstance(ls, str):
                result.error("`last_synced` must be a string or null when present")

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
#
# The payload fixtures live as committed JSON under fleet-contract-vectors/,
# loaded via a manifest that also records each case's shape and expected
# outcome — see VECTORS_DIR above and manifest.json's header comment. This
# is the single vector set a future fleet-contract Rust crate's self-test
# validates against too, so Python and Rust cannot silently drift onto
# different fixtures.


class VectorCase:
    """One committed validation-vector case: a payload plus its expected
    outcome against a named shape validator."""

    def __init__(self, name: str, shape: str, payload: Any, expect: str, min_errors: int) -> None:
        self.name = name
        self.shape = shape
        self.payload = payload
        self.expect = expect  # "pass" or "fail"
        self.min_errors = min_errors


def load_vector_cases(vectors_dir: Path = VECTORS_DIR) -> list[VectorCase]:
    """Load the shared manifest + payload files into VectorCase objects."""
    manifest_path = vectors_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases: list[VectorCase] = []
    for entry in manifest["cases"]:
        payload_path = vectors_dir / entry["file"]
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        cases.append(
            VectorCase(
                name=entry["name"],
                shape=entry["shape"],
                payload=payload,
                expect=entry["expect"],
                min_errors=entry.get("min_errors", 1),
            )
        )
    return cases


def run_self_test() -> int:
    """Run conformance checks against the committed validation vectors.
    Returns exit code."""
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
    print(f"Loading validation vectors from {VECTORS_DIR}")
    print()

    cases = load_vector_cases()
    validate_by_shape = {
        "full": validator.validate_full,
        "minimal": validator.validate_minimal,
    }

    # ── Vector cases: valid payloads ────────────────────────────────────────
    print("Group: valid payloads (expect PASS)")
    for case in cases:
        if case.expect != "pass":
            continue
        expect_pass(validate_by_shape[case.shape](case.payload, case.name))

    # ── Vector cases: invalid payloads ──────────────────────────────────────
    print("\nGroup: invalid payloads (each must produce >= min_errors error(s))")
    for case in cases:
        if case.expect != "fail":
            continue
        expect_fail(
            validate_by_shape[case.shape](case.payload, case.name),
            min_errors=case.min_errors,
        )

    # ── Schema-version N/N-1 tolerance (pure logic, §3.2/§3.5) ─────────────
    # Exercised with hypothetical (current, previous) pairs — independent of
    # this module's live SCHEMA_VERSION/PREVIOUS_SCHEMA_VERSION — to prove the
    # tolerance mechanism works both before and after a future schema bump.
    print("\nGroup: schema_version N/N-1 tolerance (pure function)")
    tolerance_failures: list[str] = []

    def expect_tolerance_ok(sv: str, current: str, previous: Optional[str], desc: str) -> None:
        errs = check_schema_version_value(sv, current=current, previous=previous)
        if errs:
            tolerance_failures.append(f"UNEXPECTED REJECT — {desc}: {errs}")
        else:
            print(f"  OK: {desc}")

    def expect_tolerance_reject(sv: str, current: str, previous: Optional[str], desc: str) -> None:
        errs = check_schema_version_value(sv, current=current, previous=previous)
        if not errs:
            tolerance_failures.append(f"UNEXPECTED ACCEPT — {desc}")
        else:
            print(f"  OK (expected reject): {desc}")

    # Today: current="1", no N-1 yet.
    expect_tolerance_ok("1", current="1", previous=None, desc="current version (no N-1 yet)")
    expect_tolerance_reject("0", current="1", previous=None, desc="no N-1 defined — rejected")
    # Hypothetical future bump: current="2", previous="1".
    expect_tolerance_ok("2", current="2", previous="1", desc="future current version")
    expect_tolerance_ok("1", current="2", previous="1", desc="future N-1 (still accepted)")
    expect_tolerance_reject("0", current="2", previous="1", desc="two versions behind — rejected")
    expect_tolerance_reject("3", current="2", previous="1", desc="unknown future version — rejected")

    if tolerance_failures:
        failures.extend(tolerance_failures)

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
