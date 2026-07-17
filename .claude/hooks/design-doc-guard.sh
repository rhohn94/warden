#!/usr/bin/env python3
# HOOK_CONTRACT: v1 capabilities=[design-doc-purity-warn,design-doc-purity-block]
"""Design-doc guard (issue #414, v3.98) — belt-and-braces tier for #358.

PreToolUse Edit|Write hook on the two design-doc tiers (`docs/design/**` and
`docs/grimoire/design/**`, README.md indexes excluded — the same surface
`check_design_doc_purity` in doc_assurance.py scans at closeout). #358 built
a deterministic closeout check that catches pollution before a release
ships; this hook catches the same pollution the moment it *enters* a doc
mid-task, per the Meta Planner review's transcript evidence that pollution
is written mid-task, not at closeout. See `release-plan-guard.sh` in this
same directory for the proof-of-concept this issue cites: a content-aware
Edit/Write PreToolUse hook is feasible in this harness.

Regex reuse: this hook imports `design_doc_purity_findings` (and
`DESIGN_PURITY_ALLOW`) straight from doc_assurance.py via importlib — the
same "import the real module, don't duplicate its regexes" pattern
doc_assurance.py's own `_load_excluded_prefixes` uses to pull
EXCLUDED_PATH_PREFIXES from the build gate. There is exactly one place the
five #358 patterns are defined; the closeout check and this hook both call
into it, so the two can never drift apart.

Dial — `design-doc-purity.enforcer` in grimoire-config.json, mirroring the
`doc-hierarchy.enforcer` block's shape and its absence-as-default semantics
(no scaffold-seed entry; a project opts into a non-default value):
  - "off":   no-op — always allow, no matter the content.
  - "warn":  allow, but attach a non-blocking advisory (hookSpecificOutput
             permissionDecision=allow + reason) when the proposed content
             matches a pollution pattern — the same JSON channel
             autonomy-allow.sh / push-guard.sh already use to attach a
             reason to a PreToolUse decision.
  - "block": deny (exit 2, stderr), same convention as release-plan-guard.sh.
  - absent / unreadable config: defaults to "warn" (#414's own shipped
    default — ship default warn until #358 has a release of telemetry
    behind it, per the issue's own instruction).

A clean edit (no pattern match) always passes silently, regardless of dial.
The deny/warn message always teaches the recovery: status/completion state
belongs in the release-plan §5 ledger via grm-ledger-tick, never the design
doc itself.
"""
import importlib.util
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_common import read_config, _scalar  # noqa: E402

RECOVERY = (
    "Status/completion state goes to the release-plan §5 ledger via "
    "grm-ledger-tick, not the design doc itself."
)

# Path-boundary match for the two design-doc tiers (docs/design/**,
# docs/grimoire/design/**), README.md indexes excluded — mirrors
# DESIGN_DOC_TIERS / _design_doc_purity_paths in doc_assurance.py, but
# evaluated against a single proposed path rather than a directory glob.
_TIER_RE = re.compile(r"(^|/)docs/(design|grimoire/design)(/|$)")


def _is_design_doc(path: str) -> bool:
    norm = path.replace(os.sep, "/")
    if os.path.basename(norm) == "README.md":
        return False
    if not norm.endswith(".md"):
        return False
    return bool(_TIER_RE.search(norm))


def _project_relpath(fp: str, proj: str) -> str:
    """Best-effort root-relative path, for the finding text and the
    DESIGN_PURITY_ALLOW lookup (which is keyed on root-relative paths)."""
    if proj:
        try:
            return os.path.relpath(fp, proj).replace(os.sep, "/")
        except ValueError:
            pass
    norm = fp.replace(os.sep, "/")
    m = _TIER_RE.search(norm)
    return norm[m.start(1 if m.group(1) else 0):].lstrip("/") if m else norm


def _load_purity_module():
    """Import doc_assurance.py's design_doc_purity_findings + allowlist via
    importlib (single source of truth — see module docstring). Returns None
    if the sibling skill isn't present (fail open rather than block on a
    missing dependency)."""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(os.path.dirname(here), "skills", "grm-doc-assurance",
                         "doc_assurance.py")
    if not os.path.exists(cand):
        return None
    try:
        spec = importlib.util.spec_from_file_location("_grimoire_doc_assurance", cand)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _dial(proj: str) -> str:
    cfg = read_config(proj)
    node = cfg.get("design-doc-purity", {})
    val = _scalar(node.get("enforcer", "warn")) if isinstance(node, dict) else "warn"
    return val if val in ("off", "warn", "block") else "warn"


def _proposed_content(tool: str, tin: dict, abs_fp: str) -> str:
    """Reconstruct the resulting file content the tool call would produce,
    without actually writing it — Write supplies the whole content; Edit
    supplies an (old_string, new_string) pair applied against the file's
    CURRENT on-disk text (PreToolUse fires before the real edit lands)."""
    if tool == "Write":
        return tin.get("content", "") or ""
    old_s = tin.get("old_string", "")
    new_s = tin.get("new_string", "")
    try:
        current = open(abs_fp, "r", encoding="utf-8").read()
    except OSError:
        return new_s
    if not old_s:
        return new_s
    count = 0 if tin.get("replace_all") else 1
    return current.replace(old_s, new_s, count) if count else current.replace(old_s, new_s)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input", {}) or {}
    if tool not in ("Edit", "Write"):
        sys.exit(0)

    fp = tin.get("file_path") or ""
    if not fp:
        sys.exit(0)

    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    abs_fp = fp if os.path.isabs(fp) else os.path.join(proj, fp)

    if not _is_design_doc(abs_fp):
        sys.exit(0)

    dial = _dial(proj)
    if dial == "off":
        sys.exit(0)

    mod = _load_purity_module()
    if mod is None:
        sys.exit(0)  # fail open — cannot evaluate without the shared module

    relpath = _project_relpath(abs_fp, proj)
    allow = getattr(mod, "DESIGN_PURITY_ALLOW", frozenset())
    if relpath in allow:
        sys.exit(0)

    proposed = _proposed_content(tool, tin, abs_fp)
    findings = mod.design_doc_purity_findings(relpath, proposed)
    if not findings:
        sys.exit(0)

    detail = "\n".join(f"  - {f}" for f in findings)

    if dial == "block":
        sys.stderr.write(
            "design-doc-guard: blocked edit — proposed content matches "
            "design-doc-purity pollution pattern(s) (#358).\n"
            f"  file: {relpath}\n{detail}\n  {RECOVERY}\n"
        )
        sys.exit(2)

    # dial == "warn": allow, but attach a visible, non-blocking advisory.
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": (
            "design-doc-guard WARN: proposed content to "
            f"{relpath} matches design-doc-purity pollution pattern(s) "
            "(#358): " + "; ".join(findings) + f". {RECOVERY}"
        ),
    }}))
    sys.exit(0)


def _self_test() -> int:
    """Self-test: run with --self-test. Two layers:
      (a) pure unit tests of _is_design_doc / _project_relpath / _dial /
          _proposed_content against synthetic inputs (no subprocess);
      (b) a live end-to-end pass invoking main() via stdin in a real
          scratch project, one case per #358 pattern plus the clean-edit /
          dial-default / allowlist / off-tier cases — mirrors
          stealth-guard.sh's `_self_test_live` harness shape.
    """
    import shutil
    import subprocess
    import tempfile

    fails = 0

    def check(desc, cond):
        nonlocal fails
        print(("ok  " if cond else "FAIL") + "  " + desc)
        fails += not cond

    # ── (a) pure unit tests ────────────────────────────────────────────
    check("_is_design_doc: docs/design/foo-design.md matches",
          _is_design_doc("docs/design/foo-design.md"))
    check("_is_design_doc: docs/grimoire/design/bar-design.md matches",
          _is_design_doc("docs/grimoire/design/bar-design.md"))
    check("_is_design_doc: docs/design/ux/foo.md (subdirectory) matches",
          _is_design_doc("docs/design/ux/foo.md"))
    check("_is_design_doc: docs/design/README.md excluded",
          not _is_design_doc("docs/design/README.md"))
    check("_is_design_doc: docs/design-review.md (lookalike prefix) excluded",
          not _is_design_doc("docs/design-review.md"))
    check("_is_design_doc: src/app.py (non design tier) excluded",
          not _is_design_doc("src/app.py"))

    check("_dial: absent config defaults to warn",
          _dial("/nonexistent-project-dir-for-self-test") == "warn")

    check("_proposed_content: Write returns tool_input content verbatim",
          _proposed_content("Write", {"content": "# Foo\n"}, "/does/not/exist.md")
          == "# Foo\n")

    # ── (b) live end-to-end: one case per #358 pattern + dial behaviour ──
    def run_hook(proj, payload):
        r = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": proj}, timeout=10,
        )
        return r.returncode, r.stdout, r.stderr

    proj = tempfile.mkdtemp()
    try:
        design_dir = os.path.join(proj, "docs", "design")
        os.makedirs(design_dir, exist_ok=True)
        target = os.path.join(design_dir, "foo-design.md")
        with open(target, "w") as f:
            f.write("# Foo\n\n## Motivation\nWhy.\n")

        def write_cfg(dial):
            os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
            with open(os.path.join(proj, ".claude", "grimoire-config.json"), "w") as f:
                json.dump({"design-doc-purity": {"enforcer": {"value": dial}}}, f)

        # Default (no config file at all) -> warn tier.
        rc, out, err = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": target, "old_string": "Why.",
                            "new_string": "Why.\n\n> **Status**: shipped"},
        })
        check("no config file -> defaults to warn (allowed, rc=0)", rc == 0)
        check("no config file -> warn advisory mentions the recovery instruction",
              "grm-ledger-tick" in out)

        # A clean edit always passes, at any dial.
        write_cfg("block")
        rc, out, err = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": target, "old_string": "Why.",
                            "new_string": "Why. It solves the thing."},
        })
        check("clean edit passes under block dial", rc == 0 and not out.strip())

        # One case per #358 pattern, all under dial=block (deny + recovery text).
        patterns = [
            ("Status: line", "Why.", "Why.\n\n> **Status**: shipped"),
            ("checked box", "Why.", "Why.\n\n- [x] done thing"),
            ("release-narration", "Why.", "Why. This shipped in v3.42."),
            ("work-item-map heading", "Why.", "Why.\n\n## File-level changes\n- a.py"),
        ]
        for label, old_s, new_s in patterns:
            rc, out, err = run_hook(proj, {
                "tool_name": "Edit",
                "tool_input": {"file_path": target, "old_string": old_s, "new_string": new_s},
            })
            check(f"block dial denies {label} (rc=2)", rc == 2)
            check(f"block dial deny message for {label} includes recovery instruction",
                  "grm-ledger-tick" in err)

        # Filename pattern via Write to a *-plan.md path under the design tier.
        plan_path = os.path.join(design_dir, "rollout-plan.md")
        rc, out, err = run_hook(proj, {
            "tool_name": "Write",
            "tool_input": {"file_path": plan_path, "content": "# Rollout\n\nWhy.\n"},
        })
        check("block dial denies *-plan.md filename (rc=2)", rc == 2)
        check("block dial deny message for filename includes recovery instruction",
              "grm-ledger-tick" in err)

        # warn dial: same pollution, but allowed with an advisory instead of denied.
        write_cfg("warn")
        rc, out, err = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": target, "old_string": "Why.",
                            "new_string": "Why.\n\n> **Status**: shipped"},
        })
        check("warn dial allows polluted edit (rc=0)", rc == 0)
        check("warn dial advisory includes recovery instruction", "grm-ledger-tick" in out)

        # off dial: no-op even for the same pollution.
        write_cfg("off")
        rc, out, err = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": target, "old_string": "Why.",
                            "new_string": "Why.\n\n> **Status**: shipped"},
        })
        check("off dial is a silent no-op (rc=0, no output)", rc == 0 and not out.strip())

        # DESIGN_PURITY_ALLOW-exempted path passes even under block. The
        # real allowlist keys on docs/grimoire/design/dependency-channel-
        # design.md (doc_assurance.py's DESIGN_PURITY_ALLOW) — use that
        # exact tier + filename so the lookup actually hits.
        write_cfg("block")
        grimoire_design_dir = os.path.join(proj, "docs", "grimoire", "design")
        os.makedirs(grimoire_design_dir, exist_ok=True)
        allow_path = os.path.join(grimoire_design_dir, "dependency-channel-design.md")
        with open(allow_path, "w") as f:
            f.write("# Dependency channel\n\n## Motivation\nWhy.\n")
        rc, out, err = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": allow_path, "old_string": "Why.",
                            "new_string": "Why.\n\n> **Status**: shipped"},
        })
        check("DESIGN_PURITY_ALLOW-exempted path passes under block dial", rc == 0)

        # Non-tool (Bash) and non-design-doc paths are always a no-op.
        rc, out, err = run_hook(proj, {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        })
        check("Bash tool is a no-op", rc == 0 and not out.strip())

        other_path = os.path.join(proj, "src", "app.py")
        os.makedirs(os.path.dirname(other_path), exist_ok=True)
        with open(other_path, "w") as f:
            f.write("print('hi')\n")
        rc, out, err = run_hook(proj, {
            "tool_name": "Edit",
            "tool_input": {"file_path": other_path, "old_string": "hi", "new_string": "shipped in v3.42"},
        })
        check("non-design-doc path is a no-op even with pollution-shaped content",
              rc == 0 and not out.strip())
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    print(f"\n{'PASS' if not fails else str(fails) + ' FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    main()
