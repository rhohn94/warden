#!/usr/bin/env python3
"""Install-doctor audit helper for the Grimoire scaffolding.

Runs the *mechanical, deterministic* half of a framework health check so the
install-doctor SKILL.md does not have to reimplement file walks, conf parsing,
or git plumbing in prose. It WRAPS the existing skills rather than duplicating
their logic:

  * the framework-file audit reuses the golden baseline shipped with
    `workflow-bootstrap` (the same `golden/` tree that `workflow-bootstrap`
    restores from) — this helper only classifies MISSING / DRIFTED / OK; the
    actual restore is delegated to `workflow-bootstrap --restore`.
  * the upstream-connection checks validate the inputs that
    `sync-from-upstream` consumes (`.scaffold-upstream.conf`, `.scaffold-base/`,
    `UPSTREAM_REPO` reachability) without performing any merge — repair is
    delegated to `sync-from-upstream` (`--adopt-base` / `--apply`).

Read-only by default. It NEVER mutates project files; the SKILL.md drives all
mutation through the wrapped skills under an explicit `--repair` flag.

Authoritative design: docs/design/agent-roles-design.md (install-doctor is a
skill, not a role).

CLI:  python3 install_doctor.py audit [--json] [--no-network]
      python3 install_doctor.py --help

Exit codes:
  0  healthy (no MISSING, no DRIFTED, upstream OK)
  1  degraded (one or more checks reported a problem)
  2  usage / internal error
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = ".claude/grimoire-config.json"
UPSTREAM_CONF = ".scaffold-upstream.conf"
BASE_ROOT = ".scaffold-base"
# Golden baseline lives inside the workflow-bootstrap skill; the doctor reuses
# it as the canonical file set rather than maintaining its own list.
GOLDEN_REL = ".claude/skills/workflow-bootstrap/golden"
FLAVOR_DIR = "claude-code"

# Files that legitimately carry per-project values; a content difference is
# expected, so we down-grade DRIFTED to CUSTOMISED rather than flagging it.
# (The SKILL.md still surfaces these as informational.)
EXPECTED_CUSTOM = {
    "CLAUDE.md",
    "settings.json",
    UPSTREAM_CONF,
}

UPSTREAM_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://|^git@|^[~./]|^[A-Za-z]:[\\/]")


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class Check:
    """A single audited item with a status and a human-readable detail line."""

    name: str
    status: str            # "ok" | "missing" | "drifted" | "warn" | "fail"
    detail: str = ""

    @property
    def problem(self) -> bool:
        return self.status in {"missing", "drifted", "fail"}


@dataclass
class Report:
    """Full health report; serializes to JSON or renders a Markdown artifact."""

    repo_root: str
    framework: list[Check] = field(default_factory=list)
    upstream: list[Check] = field(default_factory=list)
    base: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def all_checks(self) -> list[Check]:
        return [*self.framework, *self.upstream, *self.base]

    @property
    def healthy(self) -> bool:
        return not any(c.problem for c in self.all_checks)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.all_checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from start (or cwd) until grimoire-config.json is found."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILE).exists():
            return candidate
    # Fall back to the nearest dir that has a .claude/ — better than cwd.
    for candidate in [current, *current.parents]:
        if (candidate / ".claude").is_dir():
            return candidate
    return current


def read_conf(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE upstream-conf file (comments and blanks skipped)."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        # Strip inline comments after the value (conf files allow them).
        val = val.split("#", 1)[0].strip()
        out[key.strip()] = val
    return out


# ---------------------------------------------------------------------------
# Audit: framework files vs the workflow-bootstrap golden baseline
# ---------------------------------------------------------------------------


def audit_framework(root: Path) -> list[Check]:
    """Classify every golden-managed file as ok / missing / drifted.

    Reuses the golden tree that `workflow-bootstrap` restores from, so the
    canonical file set is never duplicated here. Mirrors the bootstrap
    MISSING / PRISTINE / CUSTOMISED / DRIFTED taxonomy, collapsing PRISTINE and
    CUSTOMISED into "ok" (those need no restore) and reporting only the two
    actionable states the doctor cares about: MISSING and DRIFTED.
    """
    checks: list[Check] = []
    golden = root / GOLDEN_REL
    if not golden.is_dir():
        return [Check("golden-baseline", "fail",
                      f"golden baseline not found at {GOLDEN_REL} — "
                      "cannot audit framework files; run workflow-bootstrap")]

    for gfile in sorted(golden.rglob("*")):
        if not gfile.is_file():
            continue
        rel = gfile.relative_to(golden)
        live = live_path_for(root, rel)
        rel_str = str(rel)
        if not live.exists():
            checks.append(Check(rel_str, "missing",
                                f"absent at {live.relative_to(root)} "
                                "(restore via workflow-bootstrap --restore)"))
            continue
        # Files known to carry project-specific values are expected to differ.
        if rel.name in EXPECTED_CUSTOM or rel_str in EXPECTED_CUSTOM:
            checks.append(Check(rel_str, "ok", "present (project-customised)"))
            continue
        if _bytes_equal(gfile, live):
            checks.append(Check(rel_str, "ok", "present, matches golden"))
        else:
            checks.append(Check(rel_str, "drifted",
                                "differs from golden — review; "
                                "workflow-bootstrap will diff and confirm "
                                "before any overwrite"))
    return checks


def live_path_for(root: Path, rel: Path) -> Path:
    """Map a golden-relative path to its live location.

    The golden tree mirrors the flavor root: golden/skills/... → .claude/skills/...,
    golden/hooks/... → .claude/hooks/..., golden/docs/... → docs/..., and
    top-level files (CLAUDE.md, settings.json, .scaffold-upstream.conf) to their
    canonical homes.
    """
    parts = rel.parts
    head = parts[0]
    if head in {"skills", "hooks", "workflows", "paradigms"}:
        return root / ".claude" / rel
    if head in {"settings.json", "push-allowlist", "model-effort-profiles.json"}:
        return root / ".claude" / rel
    if head == "docs":
        return root / rel
    if head == "CLAUDE.md":
        return root / rel
    if head == UPSTREAM_CONF:
        return root / rel
    # Default: place under repo root.
    return root / rel


def _bytes_equal(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Audit: upstream connection (inputs sync-from-upstream consumes)
# ---------------------------------------------------------------------------


def audit_upstream(root: Path, check_network: bool) -> tuple[list[Check], str | None, str | None]:
    """Validate .scaffold-upstream.conf and optionally reachability.

    Returns (checks, upstream_repo, upstream_ref). Does not clone or merge —
    that is sync-from-upstream's job. Reachability is a non-mutating `git
    ls-remote` probe.
    """
    checks: list[Check] = []
    conf_path = root / UPSTREAM_CONF
    if not conf_path.exists():
        checks.append(Check(UPSTREAM_CONF, "missing",
                            "absent — seed via workflow-bootstrap (Step 2.5) "
                            "or sync-from-upstream Step 1"))
        return checks, None, None

    conf = read_conf(conf_path)
    repo = conf.get("UPSTREAM_REPO", "").strip()
    ref = conf.get("UPSTREAM_REF", "").strip() or None

    if not repo:
        checks.append(Check("UPSTREAM_REPO", "fail",
                            f"present in {UPSTREAM_CONF} but empty — "
                            "set the upstream URL"))
        return checks, None, ref
    if not UPSTREAM_URL_RE.match(repo) and not (root / repo).exists():
        checks.append(Check("UPSTREAM_REPO", "warn",
                            f"value '{repo}' is neither a URL/scp-path nor a "
                            "local path that exists — verify it"))
    else:
        checks.append(Check("UPSTREAM_REPO", "ok",
                            f"{repo}{f' @ {ref}' if ref else ''}"))

    if check_network and repo:
        checks.append(_probe_reachable(root, repo, ref))
    elif repo:
        checks.append(Check("UPSTREAM_REPO reachability", "warn",
                            "skipped (--no-network)"))
    return checks, repo, ref


def _probe_reachable(root: Path, repo: str, ref: str | None) -> Check:
    """Non-mutating reachability probe. Local path → existence; URL → ls-remote."""
    local = (root / repo)
    if local.exists() or Path(repo).exists():
        return Check("UPSTREAM_REPO reachability", "ok", "local path exists")
    if shutil.which("git") is None:
        return Check("UPSTREAM_REPO reachability", "warn",
                     "git not on PATH — cannot probe")
    cmd = ["git", "ls-remote", "--exit-code", repo]
    if ref:
        cmd.append(ref)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Check("UPSTREAM_REPO reachability", "fail",
                     f"probe failed: {exc}")
    if res.returncode == 0:
        return Check("UPSTREAM_REPO reachability", "ok",
                     "reachable (git ls-remote)")
    return Check("UPSTREAM_REPO reachability", "fail",
                 f"unreachable (git ls-remote rc={res.returncode}): "
                 f"{res.stderr.strip().splitlines()[-1] if res.stderr.strip() else 'no detail'}")


# ---------------------------------------------------------------------------
# Audit: .scaffold-base consistency (sync provenance)
# ---------------------------------------------------------------------------


def audit_base(root: Path) -> list[Check]:
    """Check that the sync base snapshot is present and non-trivial.

    sync-from-upstream needs `.scaffold-base/` as the merge base. Absence means
    every differing file would report REVIEW on the next sync (no 3-way merge).
    Repair is `sync-from-upstream.sh --adopt-base`, not anything this helper does.
    """
    base = root / BASE_ROOT
    if not base.is_dir():
        return [Check(BASE_ROOT, "warn",
                      "absent — no sync provenance; "
                      "establish via sync-from-upstream.sh --adopt-base")]
    files = [p for p in base.rglob("*") if p.is_file()]
    if not files:
        return [Check(BASE_ROOT, "warn",
                      "present but empty — re-run "
                      "sync-from-upstream.sh --adopt-base")]
    return [Check(BASE_ROOT, "ok",
                  f"present ({len(files)} file(s) recorded)")]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_report(root: Path, check_network: bool) -> Report:
    rep = Report(repo_root=str(root))
    rep.framework = audit_framework(root)
    up_checks, _repo, _ref = audit_upstream(root, check_network)
    rep.upstream = up_checks
    rep.base = audit_base(root)
    rep.notes.append(
        "Feature-adoption is NOT audited mechanically: run each "
        "sync-from-upstream feature-manifest `detect` predicate per the SKILL.md "
        "Step 3 procedure to confirm each feature is adopted (not merely available)."
    )
    return rep


def render_markdown(rep: Report) -> str:
    counts = rep.counts()
    status_word = "HEALTHY" if rep.healthy else "ATTENTION NEEDED"
    lines: list[str] = []
    lines.append("# Grimoire install-doctor health report")
    lines.append("")
    lines.append(f"- Repo root: `{rep.repo_root}`")
    lines.append(f"- Overall: **{status_word}**")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no checks"
    lines.append(f"- Tallies: {summary}")
    lines.append("")

    def section(title: str, checks: list[Check]) -> None:
        lines.append(f"## {title}")
        if not checks:
            lines.append("_no checks_")
            lines.append("")
            return
        lines.append("| Item | Status | Detail |")
        lines.append("|---|---|---|")
        for c in checks:
            lines.append(f"| `{c.name}` | {c.status.upper()} | {c.detail} |")
        lines.append("")

    section("Framework files (vs workflow-bootstrap golden)", rep.framework)
    section("Upstream connection (sync-from-upstream inputs)", rep.upstream)
    section("Sync base snapshot (.scaffold-base)", rep.base)

    if rep.notes:
        lines.append("## Notes")
        for n in rep.notes:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    root = find_repo_root(Path(args.root) if args.root else None)
    rep = build_report(root, check_network=not args.no_network)
    if args.json:
        payload = {
            "repo_root": rep.repo_root,
            "healthy": rep.healthy,
            "counts": rep.counts(),
            "framework": [asdict(c) for c in rep.framework],
            "upstream": [asdict(c) for c in rep.upstream],
            "base": [asdict(c) for c in rep.base],
            "notes": rep.notes,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_markdown(rep), end="")
    return 0 if rep.healthy else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install_doctor.py",
        description="Audit Grimoire framework health (read-only). "
                    "Repair is delegated to workflow-bootstrap and "
                    "sync-from-upstream — this helper never mutates files.",
    )
    p.add_argument("--root", default=None,
                   help="Repo root (default: auto-detect from cwd up).")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("audit", help="Run the read-only health audit.")
    a.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of Markdown.")
    a.add_argument("--no-network", action="store_true",
                   help="Skip the UPSTREAM_REPO reachability probe.")
    a.set_defaults(func=cmd_audit)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 2
    except Exception as exc:  # surface as a clean usage error, never a traceback
        print(f"install-doctor: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
