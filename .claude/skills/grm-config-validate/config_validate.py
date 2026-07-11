#!/usr/bin/env python3
"""config_validate.py — validate + migrate .claude/grimoire-config.json (v1.31, #68; stealth-mode v3.0; project-manager v3.1; github-pr v3.5; qa v3.6; worktree-ports v3.7; iterate v3.11; mcp v3.12; web-app v3.26; environments v3.27; config-migration v3.51 #163; environments deploy-topology v3.68 #201; issue-filing-authority v3.74 #221).

Validates the config against a declared schema (known blocks + value sets),
reports unknown/missing fields, and runs an idempotent migration that fills
additive defaults. Read-only by default; --migrate writes (temp + validate +
atomic replace, so a write never corrupts the file).

The web-app block (v3.26) is additive with absence-as-default and is NOT a
migration default — absence already reads as not-a-web-app, so --migrate is a
no-op for it (web-app-support-design.md §1.3). It is written only when a project
is affirmatively detected/confirmed as a web app.

The environments block (v3.27) is additive with absence-as-default and is NOT
a migration default — absence means no deploy environments declared, which is
valid for non-web or early-stage projects. Schema version does not bump
(deploy-environment-design.md §1). The v3.68 #201 Phase 1 deploy-topology fields
(transport / service_manager / host / path / service / bind / service_address)
are likewise additive — absence of any field stays valid, no schema bump.

The changelog block (v3.31) is additive with absence-as-default and is NOT a
migration default — absence reads as user-facing off (operator-only changelog),
so --migrate is a no-op for it and schema-version does not bump
(changelog-surface-design.md §2).

The issue-filing-authority block (v3.74 #221) is additive with
absence-as-default and is NOT a migration default — absence reads as "not
provisioned" (the settings.json filing allowlist is not written), so --migrate
is a no-op for it and schema-version does not bump. It is written only when a
user affirmatively opts in (bootstrap interview or a github-tracker switch), and
must NEVER be silently defaulted to true — that would grant issue-filing
authority without consent (issue-filing-authority-design.md).

Usage: config_validate.py [--path P] [--migrate] [--self-test]
Exit: 0 if valid (after optional migrate) or self-test passes, 1 otherwise.
"""
from __future__ import annotations

import json, os, sys, subprocess

# Current declared schema version. `--migrate` raises an older config to this.
SCHEMA_VERSION = 4

# Declared schema: field → allowed values (None = free / structured).
ENUMS = {
    "work-paradigm": {"Supervised", "Weiss", "Noir"},
    "workflow-variant": {"Fast", "Efficient", "Cheap-Slow"},
    "release-phase-model": {"Default", "Auto"},
    "code-quality.audit-gate": {"off", "warn", "block"},
    "code-quality.auto-reviewer": {"off", "noir", "always"},
    "code-quality.typecheck": {"off", "build"},
    "stealth-mode.value": {"off", "on"},
    "project-manager.overlap-policy": {"conservative", "balanced", "aggressive"},
    "project-manager.qa-gate": {"off", "warn", "block"},
    "github-pr.boundary": {"version-to-dev", "dev-to-main", "both"},
    "github-pr.merge-method": {"merge", "squash", "rebase"},
    "github-pr.review.post-comments": {"off", "comment", "request-changes"},
    "qa.window-mode": {"earliest-unverified", "all-unverified", "last-n"},
    "qa.verify-depth": {"acceptance", "acceptance+tests", "deep"},
    "worktree-ports.strategy": {"os-assign", "random-probe", "index"},
    "iterate.audit-agent": {"dispatched", "inline"},
    "web-app.value": {"yes", "no"},
    # web-app.agentic (v3.57, standard-package adoption): an additive,
    # absence-as-default capability dial — "this web app runs its own
    # agentic/LLM workloads, so it has token cost/throughput worth surfacing".
    # Gates the conditional token-bookkeeper catalog entry's `applies-when`
    # predicate (web-app-support-design.md §5.5). Absence reads as "no".
    "web-app.agentic": {"yes", "no"},
    "changelog.user-facing": {"on", "off"},
}
# Canonical environment names (v3.27). The validator warns if a project
# declares an env name outside this set (unknown-env warning, not an error).
KNOWN_ENV_NAMES = {"local", "dev", "beta", "production"}
# Valid per-env field values (additive; sub-validated in the cross-rule).
KNOWN_ENV_CHANNELS = {"stable", "beta"}
KNOWN_ENV_DEPLOY_POLICIES = {"auto", "pr_gate", "manual"}
# Deploy-topology fields (v3.68 #201 Phase 1, deploy-environment-design.md §1).
# Additive: absence of any of these stays valid. `transport` names how the
# bundle reaches the target; `service_manager` names the init system deploy.sh
# restarts through. Both are closed sets — an unrecognized value is an error.
KNOWN_ENV_TRANSPORTS = {"ssh", "local-symlink", "pull"}
KNOWN_ENV_SERVICE_MANAGERS = {"systemd", "launchd"}
KNOWN_TOP = {"schema-version", "name", "framework-version", "work-paradigm",
             "workflow-variant", "model-effort-profile", "release-phase-model",
             "code-quality", "issue-tracker", "cost-governance", "autonomous-push",
             "issue-filing-authority",
             "stealth-mode", "project-manager", "github-pr", "qa", "worktree-ports",
             "autonomy-allow",
             "iterate", "mcp", "web-app", "environments", "changelog",
             "doc-hierarchy", "branch-model"}
# T-shirt sizes recognized in iterate.quota.
ITERATE_SIZES = {"XXL", "XL", "L", "M", "SM", "XS"}
# Additive defaults the migration fills if absent.
ADDITIVE_DEFAULTS = {
    "code-quality": {"audit-gate": {"value": "warn"}, "auto-reviewer": {"value": "noir"},
                     "coverage-threshold": {"value": None}, "typecheck": {"value": "build"}},
    "stealth-mode": {"value": "off", "acknowledged-risk": False},
    "project-manager": {"max-parallel": {"value": 3}, "overlap-policy": {"value": "balanced"},
                        "qa-gate": {"value": "block"}},
    "github-pr": {"enabled": {"value": False}, "boundary": {"value": "version-to-dev"},
                  "merge-method": {"value": "merge"},
                  "review": {"auto-dispatch": {"value": True},
                             "post-comments": {"value": "comment"}}},
    "qa": {"window-mode": {"value": "earliest-unverified"}, "window-size": {"value": 1},
           "verify-depth": {"value": "acceptance"}, "auto-file-findings": {"value": True}},
    "worktree-ports": {"enabled": {"value": True}, "strategy": {"value": "os-assign"},
                       "range-start": {"value": 20000}, "range-end": {"value": 29999},
                       "env-var": {"value": "GRIMOIRE_APP_PORT"}},
    "iterate": {"quota": {"XXL": 1, "XL": 3, "L": 5, "M": 10, "SM": 10, "XS": 20},
                "default-iterations": 1, "min-issues-floor": 3,
                "audit-agent": {"value": "dispatched"}},
    "mcp": {"enabled": {"value": True}, "prefer-for-tracker": {"value": True}},
}


def dialval(cfg: dict, path: str) -> tuple:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    if isinstance(cur, dict) and "value" in cur:
        return cur["value"], True
    return cur, True


def validate(cfg: dict) -> tuple:
    errors, warnings = [], []
    if "schema-version" not in cfg:
        errors.append("missing required field: schema-version")
    if "name" not in cfg:
        errors.append("missing required field: name")
    for k in cfg:
        if k not in KNOWN_TOP:
            warnings.append(f"unknown top-level field: {k}")
    for path, allowed in ENUMS.items():
        v, present = dialval(cfg, path)
        if present and v is not None and v not in allowed:
            errors.append(f"{path} = {v!r} not in {sorted(allowed)}")
    # cross-rule: Auto release-phase-model only under Noir
    rpm, _ = dialval(cfg, "release-phase-model")
    wp, _ = dialval(cfg, "work-paradigm")
    if rpm == "Auto" and wp != "Noir":
        errors.append("release-phase-model=Auto requires work-paradigm=Noir (fail-closed)")
    # cross-rule: Stealth Mode may only be ON once the ephemeral-context risk is
    # acknowledged (the disclosure the grm-stealth-mode-switch skill records). This
    # prevents activating stealth by hand-editing the config without consent.
    sm = cfg.get("stealth-mode")
    if isinstance(sm, dict):
        if dialval(cfg, "stealth-mode.value")[0] == "on" and not sm.get("acknowledged-risk"):
            errors.append("stealth-mode=on requires acknowledged-risk=true (fail-closed)")
    # cross-rule: project-manager.max-parallel must be a positive integer (lane cap).
    pm = cfg.get("project-manager")
    if isinstance(pm, dict):
        mp, present = dialval(cfg, "project-manager.max-parallel")
        if present and mp is not None and not (isinstance(mp, int) and not isinstance(mp, bool) and mp >= 1):
            errors.append(f"project-manager.max-parallel = {mp!r} must be an integer >= 1")
    # cross-rules: github-pr boolean fields + soft github-tracker requirement.
    gp = cfg.get("github-pr")
    if isinstance(gp, dict):
        for path in ("github-pr.enabled", "github-pr.review.auto-dispatch"):
            v, present = dialval(cfg, path)
            if present and not isinstance(v, bool):
                errors.append(f"{path} = {v!r} must be a boolean")
        enabled, _ = dialval(cfg, "github-pr.enabled")
        if enabled is True:
            trackers = (cfg.get("issue-tracker") or {}).get("trackers") or []
            has_github = any((t or {}).get("provider") == "github" for t in trackers)
            if not has_github:
                warnings.append("github-pr.enabled=true but no github issue-tracker is "
                                "configured — ensure this repo has a GitHub remote (the "
                                "github_pr.py helper verifies `gh` + remote at runtime)")
    # cross-rules: qa.window-size positive integer; qa.auto-file-findings boolean.
    qa = cfg.get("qa")
    if isinstance(qa, dict):
        ws, present = dialval(cfg, "qa.window-size")
        if present and ws is not None and not (isinstance(ws, int) and not isinstance(ws, bool) and ws >= 1):
            errors.append(f"qa.window-size = {ws!r} must be an integer >= 1")
        aff, present = dialval(cfg, "qa.auto-file-findings")
        if present and not isinstance(aff, bool):
            errors.append(f"qa.auto-file-findings = {aff!r} must be a boolean")
    # cross-rules: worktree-ports range is positive ints with start<=end; enabled bool.
    wp = cfg.get("worktree-ports")
    if isinstance(wp, dict):
        en, present = dialval(cfg, "worktree-ports.enabled")
        if present and not isinstance(en, bool):
            errors.append(f"worktree-ports.enabled = {en!r} must be a boolean")
        rs, rs_p = dialval(cfg, "worktree-ports.range-start")
        re_, re_p = dialval(cfg, "worktree-ports.range-end")
        def _port_int(name, v, present):
            if present and v is not None and not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535):
                errors.append(f"{name} = {v!r} must be an integer in 1..65535")
                return False
            return True
        ok_s = _port_int("worktree-ports.range-start", rs, rs_p)
        ok_e = _port_int("worktree-ports.range-end", re_, re_p)
        if ok_s and ok_e and rs_p and re_p and rs is not None and re_ is not None and rs > re_:
            errors.append(f"worktree-ports.range-start ({rs}) must be <= range-end ({re_})")
    # cross-rules: iterate quota sizes are non-negative ints; counters non-negative.
    it = cfg.get("iterate")
    if isinstance(it, dict):
        quota = it.get("quota")
        if quota is not None:
            if not isinstance(quota, dict):
                errors.append("iterate.quota must be an object of size -> count")
            else:
                for k, v in quota.items():
                    if k not in ITERATE_SIZES:
                        warnings.append(f"iterate.quota has unknown size {k!r} (valid: {sorted(ITERATE_SIZES)})")
                    elif not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                        errors.append(f"iterate.quota.{k} = {v!r} must be a non-negative integer")
        di, present = dialval(cfg, "iterate.default-iterations")
        if present and di is not None and not (isinstance(di, int) and not isinstance(di, bool) and di >= 1):
            errors.append(f"iterate.default-iterations = {di!r} must be an integer >= 1")
        mf, present = dialval(cfg, "iterate.min-issues-floor")
        if present and mf is not None and not (isinstance(mf, int) and not isinstance(mf, bool) and mf >= 0):
            errors.append(f"iterate.min-issues-floor = {mf!r} must be a non-negative integer")
    # cross-rules: mcp.enabled and mcp.prefer-for-tracker are booleans.
    mcp = cfg.get("mcp")
    if isinstance(mcp, dict):
        for path in ("mcp.enabled", "mcp.prefer-for-tracker"):
            v, present = dialval(cfg, path)
            if present and not isinstance(v, bool):
                errors.append(f"{path} = {v!r} must be a boolean")
    # cross-rule: issue-filing-authority block (v3.74 #221). Absence is valid and
    # is the default (not provisioned). When present it must be an object; the
    # opt-in fact `issue-filing-authority.enabled` must be a boolean. This mirrors
    # autonomous-push's opt-in shape ({"enabled": bool}) — the provisioning of the
    # settings.json filing allowlist keys off `enabled == true`. It is never
    # silently defaulted to true (not in ADDITIVE_DEFAULTS), so a project only
    # grants filing authority by an explicit user opt-in.
    ifa = cfg.get("issue-filing-authority")
    if ifa is not None:
        if not isinstance(ifa, dict):
            errors.append("issue-filing-authority must be an object "
                          "(e.g. {\"enabled\": true})")
        else:
            en, present = dialval(cfg, "issue-filing-authority.enabled")
            if present and not isinstance(en, bool):
                errors.append(f"issue-filing-authority.enabled = {en!r} must be a boolean")
    # cross-rule: web-app.stack (the advisory framework hint, §1.2) is a
    # non-empty string or null; the gating fact is web-app.value (enum above).
    wa = cfg.get("web-app")
    if isinstance(wa, dict):
        st, present = dialval(cfg, "web-app.stack")
        if present and st is not None and not (isinstance(st, str) and st.strip()):
            errors.append(f"web-app.stack = {st!r} must be a non-empty string or null")
    # cross-rule: changelog block (v3.31, changelog-surface-design.md §2) is
    # additive with absence-as-default (off = operator-only). When present it
    # must be an object; changelog.user-facing ∈ {on, off} is enforced by ENUMS.
    cl = cfg.get("changelog")
    if cl is not None and not isinstance(cl, dict):
        errors.append("changelog must be an object (e.g. {\"user-facing\": {\"value\": \"off\"}})")
    # cross-rules: environments block (v3.27, deploy-environment-design.md §1).
    # Absence is valid (no environments declared). When present: must be an object
    # of named env entries; each entry is an object with validated per-env fields.
    envs = cfg.get("environments")
    if envs is not None:
        if not isinstance(envs, dict):
            errors.append("environments must be an object of named environment entries")
        else:
            for env_name, entry in envs.items():
                if env_name not in KNOWN_ENV_NAMES:
                    warnings.append(f"environments: unrecognized environment name {env_name!r} "
                                    f"(known: {sorted(KNOWN_ENV_NAMES)})")
                if not isinstance(entry, dict):
                    errors.append(f"environments.{env_name} must be an object")
                    continue
                # data_isolation: boolean when present.
                di = entry.get("data_isolation")
                if di is not None and not isinstance(di, bool):
                    errors.append(f"environments.{env_name}.data_isolation = {di!r} must be a boolean")
                # channel: known set when present.
                ch = entry.get("channel")
                if ch is not None and ch not in KNOWN_ENV_CHANNELS:
                    errors.append(f"environments.{env_name}.channel = {ch!r} not in "
                                  f"{sorted(KNOWN_ENV_CHANNELS)}")
                # deploy_policy: known set when present.
                dp = entry.get("deploy_policy")
                if dp is not None and dp not in KNOWN_ENV_DEPLOY_POLICIES:
                    errors.append(f"environments.{env_name}.deploy_policy = {dp!r} not in "
                                  f"{sorted(KNOWN_ENV_DEPLOY_POLICIES)}")
                # dependent-service-address: non-empty string or null when present.
                dsa = entry.get("dependent-service-address")
                if dsa is not None and not (isinstance(dsa, str) and dsa.strip()):
                    errors.append(f"environments.{env_name}.dependent-service-address = "
                                  f"{dsa!r} must be a non-empty string or null")
                # v3.68 #201 Phase 1 deploy-topology fields (additive; each valid
                # when absent). transport / service_manager are closed sets; the
                # remaining fields are free-form non-empty strings (or null).
                tr = entry.get("transport")
                if tr is not None and tr not in KNOWN_ENV_TRANSPORTS:
                    errors.append(f"environments.{env_name}.transport = {tr!r} not in "
                                  f"{sorted(KNOWN_ENV_TRANSPORTS)}")
                sm = entry.get("service_manager")
                if sm is not None and sm not in KNOWN_ENV_SERVICE_MANAGERS:
                    errors.append(f"environments.{env_name}.service_manager = {sm!r} not in "
                                  f"{sorted(KNOWN_ENV_SERVICE_MANAGERS)}")
                for fld in ("host", "path", "service", "bind", "service_address"):
                    val = entry.get(fld)
                    if val is not None and not (isinstance(val, str) and val.strip()):
                        errors.append(f"environments.{env_name}.{fld} = {val!r} "
                                      f"must be a non-empty string or null")
    # cross-rule: branch-model block (v3.38, BMI-3). Absence is valid (default
    # dev). When present: must be an object; integration-branch (if set) must be
    # a non-empty string — the sync skills read it to detect the integration line.
    bm = cfg.get("branch-model")
    if bm is not None:
        if not isinstance(bm, dict):
            errors.append("branch-model must be an object "
                          "(e.g. {\"integration-branch\": \"dev\"})")
        else:
            ib = bm.get("integration-branch")
            if ib is not None and not (isinstance(ib, str) and ib.strip()):
                errors.append(f"branch-model.integration-branch = {ib!r} "
                              "must be a non-empty string (e.g. \"dev\" or \"experimental\")")
    return errors, warnings


_DATA_ISOLATION_TRUTHY = {"true", "yes", "1"}


def migrate(cfg: dict) -> list:
    changed = []
    for block, default in ADDITIVE_DEFAULTS.items():
        if block not in cfg:
            cfg[block] = default
            changed.append(f"added additive default block: {block}")
    # v3.51 #163: migrate legacy string data_isolation values to booleans.
    # Earlier versions serialised data_isolation as "true"/"false"/"yes"/"no"
    # string literals; the validator now requires a proper JSON boolean.
    envs = cfg.get("environments")
    if isinstance(envs, dict):
        for env_name, entry in envs.items():
            if not isinstance(entry, dict):
                continue
            di = entry.get("data_isolation")
            if isinstance(di, str):
                bool_val = di.strip().lower() in _DATA_ISOLATION_TRUTHY
                entry["data_isolation"] = bool_val
                changed.append(
                    f"environments.{env_name}.data_isolation: "
                    f"coerced string {di!r} → {bool_val}"
                )
    if cfg.get("schema-version", 0) < SCHEMA_VERSION:
        cfg["schema-version"] = SCHEMA_VERSION
        changed.append(f"raised schema-version to {SCHEMA_VERSION}")
    return changed


def self_test() -> tuple:
    """In-memory checks of the schema rules, centred on the web-app block (v3.26)
    and the environments block (v3.27).

    Covers: a valid web-app block, invalid web-app values, absence-as-default,
    migrate idempotency for both web-app and environments; a valid environments
    block, invalid per-env fields, absent block, and migrate non-synthesis.
    Returns (passed, failed, lines)."""
    base = {"schema-version": SCHEMA_VERSION, "name": "T", "work-paradigm": {"value": "Noir"}}
    cases = []  # (label, predicate)

    # 1. Valid web-app block (yes + stack) — no errors.
    cfg = dict(base, **{"web-app": {"value": "yes", "stack": "Flask + HTMX (web)"}})
    errs, _ = validate(cfg)
    cases.append(("valid web-app block (yes + stack) has no errors", not errs))

    # 1b. Valid declined block (no + null stack) — no errors.
    cfg = dict(base, **{"web-app": {"value": "no", "stack": None}})
    errs, _ = validate(cfg)
    cases.append(("valid declined web-app block (no + null stack) has no errors", not errs))

    # 2. Invalid web-app.value — flagged by the ENUMS machinery.
    cfg = dict(base, **{"web-app": {"value": "maybe"}})
    errs, _ = validate(cfg)
    cases.append(("invalid web-app.value is rejected",
                  any("web-app.value" in e for e in errs)))

    # 2b. Invalid web-app.stack (empty string) — flagged by the cross-rule.
    cfg = dict(base, **{"web-app": {"value": "yes", "stack": "   "}})
    errs, _ = validate(cfg)
    cases.append(("empty-string web-app.stack is rejected",
                  any("web-app.stack" in e for e in errs)))

    # 2c. web-app.agentic (v3.57): valid "yes" passes; an out-of-set value is
    #     rejected by the ENUMS machinery; absence is the default (no warning).
    cfg = dict(base, **{"web-app": {"value": "yes", "agentic": {"value": "yes"}}})
    errs, _ = validate(cfg)
    cases.append(("valid web-app.agentic (yes) has no errors",
                  not any("web-app.agentic" in e for e in errs)))
    cfg = dict(base, **{"web-app": {"value": "yes", "agentic": {"value": "sometimes"}}})
    errs, _ = validate(cfg)
    cases.append(("invalid web-app.agentic is rejected",
                  any("web-app.agentic" in e for e in errs)))
    cfg = dict(base, **{"web-app": {"value": "yes"}})
    errs, warns = validate(cfg)
    cases.append(("absent web-app.agentic is valid (absence = default no)",
                  not any("web-app.agentic" in e for e in errs)
                  and not any("web-app.agentic" in w for w in warns)))

    # 3. Absent block — the default; valid, no web-app warning/error.
    cfg = dict(base)
    errs, warns = validate(cfg)
    cases.append(("absent web-app block is valid (absence = default)",
                  not errs and not any("web-app" in w for w in warns)))

    # 4. --migrate is a no-op for the web-app block: migrating a config without
    #    one must NOT synthesize it (absence is already the default, §1.3).
    cfg = dict(base)
    migrate(cfg)
    cases.append(("migrate does not synthesize a web-app block", "web-app" not in cfg))

    # 4b. --migrate idempotency: a second migrate over the already-migrated
    #     config reports no changes and leaves web-app absent.
    second = migrate(cfg)
    cases.append(("second migrate is a no-op (idempotent)",
                  second == [] and "web-app" not in cfg))

    # 4c. A config that already records web-app survives migrate unchanged.
    cfg = dict(base, **{"web-app": {"value": "yes", "stack": "React (web)"}})
    migrate(cfg)
    cases.append(("migrate preserves an existing web-app block",
                  cfg.get("web-app") == {"value": "yes", "stack": "React (web)"}))

    # --- environments block (v3.27) ---

    # 5. Valid environments block (all four named envs, all fields) — no errors.
    valid_envs = {
        "local": {"data_isolation": True, "channel": "stable", "deploy_policy": "auto",
                  "dependent-service-address": None},
        "dev": {"data_isolation": True, "channel": "stable", "deploy_policy": "auto",
                "dependent-service-address": "http://dev.example.invalid"},
        "beta": {"data_isolation": True, "channel": "beta", "deploy_policy": "auto",
                 "dependent-service-address": "http://beta.example.invalid"},
        "production": {"data_isolation": True, "channel": "stable", "deploy_policy": "pr_gate",
                       "dependent-service-address": "http://prod.example.invalid"},
    }
    cfg = dict(base, **{"environments": valid_envs})
    errs, _ = validate(cfg)
    cases.append(("valid environments block (all envs + fields) has no errors", not errs))

    # 5b. Partial environments block (only production) — also valid.
    cfg = dict(base, **{"environments": {
        "production": {"data_isolation": True, "channel": "stable", "deploy_policy": "pr_gate"}}})
    errs, _ = validate(cfg)
    cases.append(("partial environments block (production only) is valid", not errs))

    # 6. Invalid channel — flagged.
    cfg = dict(base, **{"environments": {"dev": {"channel": "canary"}}})
    errs, _ = validate(cfg)
    cases.append(("invalid channel is rejected",
                  any("channel" in e for e in errs)))

    # 6b. deploy_policy: manual — now valid (#163); must not produce an error.
    cfg = dict(base, **{"environments": {"production": {"deploy_policy": "manual"}}})
    errs, _ = validate(cfg)
    cases.append(("deploy_policy=manual is accepted (#163)", not errs))

    # 6b-2. Genuinely invalid deploy_policy — still flagged.
    cfg = dict(base, **{"environments": {"production": {"deploy_policy": "cron"}}})
    errs, _ = validate(cfg)
    cases.append(("invalid deploy_policy is rejected",
                  any("deploy_policy" in e for e in errs)))

    # 6c. data_isolation not a boolean — flagged before migration.
    cfg = dict(base, **{"environments": {"dev": {"data_isolation": "yes"}}})
    errs, _ = validate(cfg)
    cases.append(("non-boolean data_isolation is rejected before migrate",
                  any("data_isolation" in e for e in errs)))

    # 6c-2. --migrate coerces string data_isolation to boolean; validate passes (#163).
    cfg = dict(base, **{"environments": {
        "dev": {"data_isolation": "true"},
        "production": {"data_isolation": "false"},
    }})
    changes = migrate(cfg)
    cases.append(("migrate coerces string data_isolation to boolean (#163)",
                  cfg["environments"]["dev"]["data_isolation"] is True
                  and cfg["environments"]["production"]["data_isolation"] is False
                  and any("data_isolation" in c for c in changes)))
    errs, _ = validate(cfg)
    cases.append(("migrated data_isolation booleans pass validation (#163)", not errs))

    # 6c-3. --migrate is idempotent on already-boolean data_isolation.
    cfg = dict(base, **{"environments": {"dev": {"data_isolation": True}}})
    changes = migrate(cfg)
    cases.append(("migrate is idempotent on boolean data_isolation",
                  cfg["environments"]["dev"]["data_isolation"] is True
                  and not any("data_isolation" in c for c in changes)))

    # 6d. Empty-string dependent-service-address — flagged.
    cfg = dict(base, **{"environments": {"dev": {"dependent-service-address": "   "}}})
    errs, _ = validate(cfg)
    cases.append(("empty-string dependent-service-address is rejected",
                  any("dependent-service-address" in e for e in errs)))

    # 6e. Unrecognized env name — warning, not error.
    cfg = dict(base, **{"environments": {"staging": {"channel": "stable"}}})
    errs, warns = validate(cfg)
    cases.append(("unrecognized env name produces warning not error",
                  not errs and any("staging" in w for w in warns)))

    # --- deploy-topology fields (v3.68 #201 Phase 1) ---

    # 6f. Full deploy-topology env (all new fields present, valid values) — no errors.
    cfg = dict(base, **{"environments": {"production": {
        "channel": "stable", "deploy_policy": "manual", "data_isolation": True,
        "transport": "ssh", "service_manager": "systemd",
        "host": "deployer@goon.example", "path": "/srv/goon-cave",
        "service": "goon-cave", "bind": "127.0.0.1:3000",
        "service_address": "https://goon.example"}}})
    errs, _ = validate(cfg)
    cases.append(("full deploy-topology env (all new fields) has no errors", not errs))

    # 6g. New deploy-topology fields are all optional — absence stays valid.
    cfg = dict(base, **{"environments": {"dev": {"channel": "stable"}}})
    errs, _ = validate(cfg)
    cases.append(("deploy-topology fields are optional (absence valid)", not errs))

    # 6h. Invalid transport — flagged.
    cfg = dict(base, **{"environments": {"production": {"transport": "carrier-pigeon"}}})
    errs, _ = validate(cfg)
    cases.append(("invalid transport is rejected", any("transport" in e for e in errs)))

    # 6i. Invalid service_manager — flagged.
    cfg = dict(base, **{"environments": {"production": {"service_manager": "upstart"}}})
    errs, _ = validate(cfg)
    cases.append(("invalid service_manager is rejected",
                  any("service_manager" in e for e in errs)))

    # 6j. Empty-string topology string field (host) — flagged.
    cfg = dict(base, **{"environments": {"production": {"host": "  "}}})
    errs, _ = validate(cfg)
    cases.append(("empty-string host is rejected", any("host" in e for e in errs)))

    # 6k. Every valid transport / service_manager value is accepted.
    for t in sorted(KNOWN_ENV_TRANSPORTS):
        cfg = dict(base, **{"environments": {"production": {"transport": t}}})
        errs, _ = validate(cfg)
        cases.append((f"transport={t} is accepted", not errs))
    for s in sorted(KNOWN_ENV_SERVICE_MANAGERS):
        cfg = dict(base, **{"environments": {"production": {"service_manager": s}}})
        errs, _ = validate(cfg)
        cases.append((f"service_manager={s} is accepted", not errs))

    # 6l. --migrate does not synthesize the new topology fields.
    cfg = dict(base, **{"environments": {"production": {"channel": "stable"}}})
    migrate(cfg)
    cases.append(("migrate does not synthesize topology fields",
                  set(cfg["environments"]["production"].keys()) == {"channel"}))

    # 7. Absent environments block — valid (absence = no environments declared).
    cfg = dict(base)
    errs, warns = validate(cfg)
    cases.append(("absent environments block is valid (absence = default)",
                  not errs and not any("environments" in w for w in warns)))

    # 8. --migrate does NOT synthesize an environments block.
    cfg = dict(base)
    migrate(cfg)
    cases.append(("migrate does not synthesize an environments block",
                  "environments" not in cfg))

    # 8b. Second migrate is still a no-op; environments still absent.
    second = migrate(cfg)
    cases.append(("second migrate is a no-op for environments (idempotent)",
                  second == [] and "environments" not in cfg))

    # 8c. A config that already records environments survives migrate unchanged.
    cfg = dict(base, **{"environments": {"production": {"deploy_policy": "pr_gate"}}})
    migrate(cfg)
    cases.append(("migrate preserves an existing environments block",
                  cfg.get("environments") == {"production": {"deploy_policy": "pr_gate"}}))

    # --- changelog block (v3.31, changelog-surface-design.md §2) ---

    # 9. Valid changelog block (user-facing on/off) — no errors.
    cfg = dict(base, **{"changelog": {"user-facing": {"value": "on"}}})
    errs, _ = validate(cfg)
    cases.append(("valid changelog block (user-facing on) has no errors", not errs))

    # 9b. Invalid changelog.user-facing value — flagged by ENUMS.
    cfg = dict(base, **{"changelog": {"user-facing": {"value": "maybe"}}})
    errs, _ = validate(cfg)
    cases.append(("invalid changelog.user-facing is rejected",
                  any("changelog.user-facing" in e for e in errs)))

    # 9c. Non-object changelog block — flagged by the cross-rule.
    cfg = dict(base, **{"changelog": "on"})
    errs, _ = validate(cfg)
    cases.append(("non-object changelog block is rejected",
                  any("changelog must be an object" in e for e in errs)))

    # 9d. Absent changelog block — valid (absence = off, operator-only).
    cfg = dict(base)
    errs, warns = validate(cfg)
    cases.append(("absent changelog block is valid (absence = default off)",
                  not errs and not any("changelog" in w for w in warns)))

    # 9e. --migrate does NOT synthesize a changelog block (absence is default).
    cfg = dict(base)
    migrate(cfg)
    cases.append(("migrate does not synthesize a changelog block",
                  "changelog" not in cfg))

    # 9f. A config that already records changelog survives migrate unchanged.
    cfg = dict(base, **{"changelog": {"user-facing": {"value": "on"}}})
    migrate(cfg)
    cases.append(("migrate preserves an existing changelog block",
                  cfg.get("changelog") == {"user-facing": {"value": "on"}}))

    # --- branch-model block (v3.38, BMI-3) ---

    # 10. Absent branch-model block — valid (absence = default dev).
    cfg = dict(base)
    errs, warns = validate(cfg)
    cases.append(("absent branch-model block is valid (absence = default dev)",
                  not errs and not any("branch-model" in w for w in warns)))

    # 10b. Valid branch-model with integration-branch string — no errors.
    cfg = dict(base, **{"branch-model": {"integration-branch": "experimental"}})
    errs, _ = validate(cfg)
    cases.append(("valid branch-model.integration-branch string has no errors", not errs))

    # 10c. branch-model.integration-branch empty string — flagged.
    cfg = dict(base, **{"branch-model": {"integration-branch": "   "}})
    errs, _ = validate(cfg)
    cases.append(("empty-string branch-model.integration-branch is rejected",
                  any("branch-model.integration-branch" in e for e in errs)))

    # 10d. branch-model not an object — flagged.
    cfg = dict(base, **{"branch-model": "dev"})
    errs, _ = validate(cfg)
    cases.append(("non-object branch-model is rejected",
                  any("branch-model must be an object" in e for e in errs)))

    # 10e. --migrate does NOT synthesize a branch-model block (absence = default dev).
    cfg = dict(base)
    migrate(cfg)
    cases.append(("migrate does not synthesize a branch-model block",
                  "branch-model" not in cfg))

    # --- issue-filing-authority block (v3.74 #221) ---

    # 11. Absent block — valid, and it is the default (not provisioned).
    cfg = dict(base)
    errs, warns = validate(cfg)
    cases.append(("absent issue-filing-authority block is valid (absence = default off)",
                  not errs and not any("issue-filing-authority" in w for w in warns)))

    # 11b. Valid opt-in block (enabled: true) — no errors.
    cfg = dict(base, **{"issue-filing-authority": {"enabled": True}})
    errs, _ = validate(cfg)
    cases.append(("valid issue-filing-authority block (enabled true) has no errors", not errs))

    # 11c. Valid opt-out block (enabled: false) — no errors.
    cfg = dict(base, **{"issue-filing-authority": {"enabled": False}})
    errs, _ = validate(cfg)
    cases.append(("valid issue-filing-authority block (enabled false) has no errors", not errs))

    # 11d. Non-boolean enabled — flagged by the cross-rule.
    cfg = dict(base, **{"issue-filing-authority": {"enabled": "yes"}})
    errs, _ = validate(cfg)
    cases.append(("non-boolean issue-filing-authority.enabled is rejected",
                  any("issue-filing-authority.enabled" in e for e in errs)))

    # 11e. Non-object block — flagged by the cross-rule.
    cfg = dict(base, **{"issue-filing-authority": "on"})
    errs, _ = validate(cfg)
    cases.append(("non-object issue-filing-authority block is rejected",
                  any("issue-filing-authority must be an object" in e for e in errs)))

    # 11f. --migrate does NOT synthesize the block (absence is the default; the
    #      dial must never be silently defaulted to true — that would grant
    #      filing authority without consent).
    cfg = dict(base)
    migrate(cfg)
    cases.append(("migrate does not synthesize an issue-filing-authority block",
                  "issue-filing-authority" not in cfg))

    # 11g. A config that already records the opt-in survives migrate unchanged.
    cfg = dict(base, **{"issue-filing-authority": {"enabled": True}})
    migrate(cfg)
    cases.append(("migrate preserves an existing issue-filing-authority block",
                  cfg.get("issue-filing-authority") == {"enabled": True}))

    lines, passed, failed = [], 0, 0
    for label, ok in cases:
        lines.append(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    return passed, failed, lines


def _repo_root_for(path):
    """Best-effort repo root for a root-config path.

    The root config conventionally lives at <repo>/.claude/grimoire-config.json,
    so the repo root is two directories up. Falls back to the current directory
    when the path is not under a .claude/ dir. Used only to locate a sibling
    codex/ flavor for delegated validation.
    """
    p = os.path.abspath(path)
    parent = os.path.dirname(p)               # .../.claude
    if os.path.basename(parent) == ".claude":
        return os.path.dirname(parent)        # .../<repo>
    return os.getcwd()


def validate_codex_flavor(repo_root: str) -> tuple:
    """Delegate codex flavor config validation to the codex flavor's own validator.

    The codex flavor (v3.55) ships its own config cluster
    (codex/grimoire-config.json + .codex/*) and a dedicated validator at
    codex/scripts/config_validate.py. When a codex/ flavor directory is present
    at the repo root, this function shells out to that validator in --strict mode
    and FAILS CLOSED: a missing validator script or a non-zero exit is a hard
    error. Returns a (ok, message) tuple; ok is True only when codex/ is absent
    (nothing to validate) or the delegated validator passed.
    """
    codex_dir = os.path.join(repo_root, "codex")
    if not os.path.isdir(codex_dir):
        return True, None  # no codex flavor in this repo — nothing to validate
    validator = os.path.join(codex_dir, "scripts", "config_validate.py")
    if not os.path.isfile(validator):
        return (False,
                f"codex/ present but codex/scripts/config_validate.py is missing "
                f"({validator}) — cannot validate the codex flavor config (fail-closed)")
    try:
        proc = subprocess.run(
            [sys.executable, validator, "--strict"],
            cwd=codex_dir, capture_output=True, text=True)
    except Exception as e:  # pragma: no cover - defensive
        return False, f"codex flavor validation could not run: {e} (fail-closed)"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, ("codex flavor config validation FAILED "
                       f"(exit {proc.returncode}):\n{out.rstrip()}")
    return True, out.rstrip()


def main() -> None:
    args = sys.argv[1:]
    if "--self-test" in args:
        passed, failed, lines = self_test()
        for ln in lines:
            print(ln)
        print(f"\nconfig-validate self-test: {passed} passed, {failed} failed.")
        sys.exit(1 if failed else 0)
    path = ".claude/grimoire-config.json"
    if "--path" in args:
        path = args[args.index("--path") + 1]
    do_migrate = "--migrate" in args
    try:
        cfg = json.load(open(path))
    except Exception as e:
        print(f"config-validate: cannot parse {path}: {e}")
        sys.exit(1)

    if do_migrate:
        changes = migrate(cfg)
        if changes:
            tmp = path + ".tmp"
            json.dump(cfg, open(tmp, "w"), indent=2)
            json.load(open(tmp))  # validate JSON before swap
            os.replace(tmp, path)
            for c in changes:
                print("migrated:", c)
        else:
            print("migrate: no changes (already current)")

    errors, warnings = validate(cfg)
    for w in warnings:
        print("warn:", w)
    for e in errors:
        print("ERROR:", e)
    print(f"\nconfig-validate: {len(errors)} error(s), {len(warnings)} warning(s).")

    # codex flavor delegation (v3.55): when a codex/ flavor is present at the
    # repo root, validate its config cluster via the flavor's own validator and
    # fail closed on any problem. Additive — never relaxes the root result.
    codex_ok, codex_msg = validate_codex_flavor(_repo_root_for(path))
    if codex_msg:
        if codex_ok:
            print("codex flavor: config validation passed (delegated --strict).")
        else:
            print("ERROR:", codex_msg)

    sys.exit(1 if (errors or not codex_ok) else 0)


if __name__ == "__main__":
    main()
