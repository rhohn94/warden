#!/usr/bin/env python3
"""github_pr.py — open / inspect / merge GitHub pull requests for the
github-pr integration (v3.5).

The integration master / Project Manager calls this at a merge boundary when
`github-pr.enabled` is true: open a PR (idempotent), dispatch a Reviewer on it,
then merge VIA the PR. Stdlib-only + `gh`/`git` via subprocess (the v3.3
scripting standard). Read/inspect subcommands are safe; `open` and `merge` are
**push-class actions** governed by the existing push policy (human-gated unless
`autonomous-push.enabled`) — this helper performs the gh call; it does not decide
the gate.

Design authority: docs/design/github-pr-integration-design.md.

Subcommands:
  open   --base B --head H [--title T] [--body-file F] [--plan PATH]
  status --pr N
  merge  --pr N [--method merge|squash|rebase]
  diff   --pr N
  --self-test

JSON to stdout. Exit 0 ok, 2 bad input/degraded, 3 gh failure.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys


def _gh(args, timeout=30):
    """Run `gh <args>`; return (rc, stdout, stderr). rc=127 if gh absent."""
    if not shutil.which("gh"):
        return 127, "", "gh not found on PATH"
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return p.returncode, p.stdout, p.stderr


def gh_degraded():
    """Return a reason string if gh/GitHub is unusable, else None."""
    if not shutil.which("gh"):
        return "gh CLI not found on PATH"
    rc, _out, err = _gh(["repo", "view", "--json", "nameWithOwner"], timeout=15)
    if rc == 127:
        return "gh CLI not found on PATH"
    if rc != 0:
        return "no GitHub remote / gh not authenticated: " + (err.strip() or "gh repo view failed")
    return None


# ── pure, testable builders / parsers ───────────────────────────────────

def plan_version_theme(plan_text):
    """Extract (version, theme) from a release-planning doc, best-effort."""
    if not plan_text:
        return None, None
    vm = re.search(r"#\s*Release Planning\s*[—\-–]\s*(v\d+\.\d+)", plan_text)
    version = vm.group(1) if vm else None
    theme = None
    tm = re.search(r"^\|\s*\*\*Theme\*\*\s*\|\s*(.+?)\s*\|", plan_text, re.MULTILINE)
    if tm:
        theme = tm.group(1).strip()
    return version, theme


def build_title(base, head, version=None):
    if version:
        return f"{version}: {head} → {base}"
    return f"{head} → {base}"


def build_body(base, head, version=None, theme=None):
    lines = [f"Integration PR: `{head}` → `{base}`.", ""]
    if version:
        lines.append(f"**Release:** {version}")
    if theme:
        lines.append(f"**Theme:** {theme}")
    lines += [
        "",
        "Opened by the Grimoire integration flow (`github-pr`). A dispatched "
        "Reviewer posts its findings on this PR; the boundary merge happens via "
        "`gh pr merge` after review.",
    ]
    return "\n".join(lines)


def parse_pr_list(json_text):
    """gh pr list --json number,url,headRefName,baseRefName → list of dicts."""
    try:
        return json.loads(json_text) if json_text.strip() else []
    except json.JSONDecodeError:
        return []


def parse_pr_view(json_text):
    try:
        return json.loads(json_text) if json_text.strip() else {}
    except json.JSONDecodeError:
        return {}


# ── network subcommands ─────────────────────────────────────────────────

def find_open_pr(head, base):
    rc, out, _err = _gh(["pr", "list", "--head", head, "--base", base,
                         "--state", "open", "--json", "number,url,headRefName,baseRefName"])
    if rc != 0:
        return None
    prs = parse_pr_list(out)
    return prs[0] if prs else None


def cmd_open(args):
    deg = gh_degraded()
    if deg:
        print(json.dumps({"degraded": deg}, sort_keys=True)); return 2
    existing = find_open_pr(args.head, args.base)
    if existing:
        print(json.dumps({"created": False, "number": existing.get("number"),
                          "url": existing.get("url"), "head": args.head,
                          "base": args.base}, sort_keys=True))
        return 0
    plan_text = None
    if args.plan and os.path.exists(args.plan):
        with open(args.plan, encoding="utf-8") as fh:
            plan_text = fh.read()
    version, theme = plan_version_theme(plan_text)
    title = args.title or build_title(args.base, args.head, version)
    ghargs = ["pr", "create", "--base", args.base, "--head", args.head, "--title", title]
    if args.body_file:
        ghargs += ["--body-file", args.body_file]
    else:
        ghargs += ["--body", build_body(args.base, args.head, version, theme)]
    rc, out, err = _gh(ghargs)
    if rc != 0:
        print(json.dumps({"error": err.strip() or "gh pr create failed"}, sort_keys=True))
        return 3
    print(json.dumps({"created": True, "url": out.strip(), "head": args.head,
                      "base": args.base, "title": title}, sort_keys=True))
    return 0


def cmd_status(args):
    deg = gh_degraded()
    if deg:
        print(json.dumps({"degraded": deg}, sort_keys=True)); return 2
    rc, out, err = _gh(["pr", "view", str(args.pr), "--json",
                        "number,state,reviewDecision,mergeable,headRefName,baseRefName,url"])
    if rc != 0:
        print(json.dumps({"error": err.strip() or "gh pr view failed"}, sort_keys=True)); return 3
    print(json.dumps(parse_pr_view(out), sort_keys=True)); return 0


def cmd_merge(args):
    deg = gh_degraded()
    if deg:
        print(json.dumps({"degraded": deg}, sort_keys=True)); return 2
    rc, out, err = _gh(["pr", "merge", str(args.pr), "--" + args.method])
    if rc != 0:
        print(json.dumps({"merged": False, "error": err.strip() or "gh pr merge failed"},
                         sort_keys=True)); return 3
    print(json.dumps({"merged": True, "pr": args.pr, "method": args.method},
                     sort_keys=True)); return 0


def cmd_diff(args):
    deg = gh_degraded()
    if deg:
        print(json.dumps({"degraded": deg}, sort_keys=True)); return 2
    rc, out, err = _gh(["pr", "diff", str(args.pr)])
    if rc != 0:
        print(json.dumps({"error": err.strip() or "gh pr diff failed"}, sort_keys=True)); return 3
    sys.stdout.write(out)
    return 0


def _self_test():
    failures = []
    plan = ("# Release Planning — v3.5\n\n"
            "| | |\n|---|---|\n"
            "| **Version** | `v3.5` |\n"
            "| **Theme** | GitHub PR & review integration |\n")
    version, theme = plan_version_theme(plan)
    if version != "v3.5":
        failures.append("plan version parse: %r" % version)
    if theme != "GitHub PR & review integration":
        failures.append("plan theme parse: %r" % theme)

    if build_title("dev", "version/3.5", "v3.5") != "v3.5: version/3.5 → dev":
        failures.append("build_title with version wrong")
    if build_title("dev", "version/3.5") != "version/3.5 → dev":
        failures.append("build_title without version wrong")

    body = build_body("dev", "version/3.5", "v3.5", "Theme X")
    if "version/3.5" not in body or "v3.5" not in body or "gh pr merge" not in body:
        failures.append("build_body missing expected content")

    prs = parse_pr_list('[{"number":7,"url":"https://x/pull/7","headRefName":"version/3.5","baseRefName":"dev"}]')
    if not (len(prs) == 1 and prs[0]["number"] == 7):
        failures.append("parse_pr_list wrong: %r" % prs)
    if parse_pr_list("") != [] or parse_pr_list("not json") != []:
        failures.append("parse_pr_list empty/bad handling")

    view = parse_pr_view('{"number":7,"state":"OPEN","reviewDecision":"APPROVED"}')
    if view.get("reviewDecision") != "APPROVED":
        failures.append("parse_pr_view wrong: %r" % view)

    # determinism of builders
    if build_body("dev", "h", "v", "t") != build_body("dev", "h", "v", "t"):
        failures.append("build_body non-deterministic")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("github_pr self-test: OK (plan parse, title/body builders, pr-list/pr-view parsers, determinism)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Open / inspect / merge GitHub PRs.")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    o = sub.add_parser("open"); o.add_argument("--base", required=True)
    o.add_argument("--head", required=True); o.add_argument("--title")
    o.add_argument("--body-file"); o.add_argument("--plan")

    s = sub.add_parser("status"); s.add_argument("--pr", type=int, required=True)
    m = sub.add_parser("merge"); m.add_argument("--pr", type=int, required=True)
    m.add_argument("--method", default="merge", choices=["merge", "squash", "rebase"])
    d = sub.add_parser("diff"); d.add_argument("--pr", type=int, required=True)

    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.cmd == "open":
        return cmd_open(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "merge":
        return cmd_merge(args)
    if args.cmd == "diff":
        return cmd_diff(args)
    ap.error("a subcommand or --self-test is required")


if __name__ == "__main__":
    sys.exit(main())
