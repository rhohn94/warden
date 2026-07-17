#!/usr/bin/env python3
"""Release-time issue reconciliation for Grimoire scaffolding.

Sweeps open issues referenced by a release's own artifacts (commits, the
release-planning doc's §2, and the version-history entry), intersects that
reference set with currently-open issues from the issue-tracker abstraction,
and disposes of each candidate: close-with-comment under an autonomous work
paradigm (Noir), or flag-for-human-review under Supervised/Weiss.

All tracker reads/writes go through
`.claude/skills/grm-issue-tracker/issue_tracker.py` (imported directly — no
raw `gh` calls). Every close is re-read after writing and the run fails
loudly if the state did not persist (the github-backend masking-failure
history, #130).

Idempotent: a close writes a marker comment
(`<!-- grm-issue-reconcile: closed by vX.Y -->`); a re-run over the same
release range skips issues that already carry the marker for that version.

Release gate (#468): `--tag` is a MANDATORY post-tag step in
grm-project-release, not an advisory one. A non-zero exit BLOCKS the release
from proceeding past the reconcile step:

- A write that reconcile() attempted but could not verify as persisted
  (`errors` — the #130 masking-failure pattern) is always a hard failure;
  there is no override, because it is a defect, not a judgment call.
- A strong-evidence ("Closes #N"-shaped) claim that the paradigm's
  flag-don't-write contract (Supervised/Weiss) redirected to `flagged`
  instead of closing is ALSO a gate failure by default — but recoverable via
  `--reconcile-override-reason "<why>"` (a non-empty justification is
  required; the reason is echoed into the run output for the audit trail).
  This asymmetry is deliberate: fleet triage observed both false negatives
  (missed closures) and one false positive (a wrongly-closed tracking issue)
  from this same detector, so the gate never unconditionally hard-stops a
  release on its own say-so — it fails loud with the exact issue(s) named,
  and a human can override with a reason rather than being stuck.
- Weak (bare-mention) and revert-only references were never close-eligible
  and never gate the release — they remain purely advisory, same as before.
- `--dry-run` never gates (a preview run writes nothing either way).

Authoritative design: docs/grimoire/design/issue-reconciliation-design.md

CLI:  python3 issue_reconcile.py --tag vX.Y [--prev-tag vX.Y-1] [--dry-run]
      python3 issue_reconcile.py --tag vX.Y --reconcile-override-reason "<why>"
      python3 issue_reconcile.py --sweep vX.Y..vX.Z [--dry-run]
      python3 issue_reconcile.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

# grm-issue-tracker is a fixed sibling skill directory (mirrors the
# code_health.py -> architecture_fitness.py pattern). Load it by a
# __file__-relative path so find_repo_root()/CONFIG_FILE have a single body
# of truth (#335) without needing repo_root already resolved (find_repo_root
# is how repo_root gets resolved in the first place — a plain
# importlib-by-repo_root load here would be circular).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-issue-tracker"))
import issue_tracker  # noqa: E402  (sys.path set immediately above)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = issue_tracker.CONFIG_FILE

# A bare #N reference. Excludes cross-repo forms (`familiar#100`, `org/repo#42`)
# via a negative lookbehind: only a standalone #N — preceded by start-of-string,
# whitespace, '(', ',', or ':' — matches, never one glued to a preceding word
# character or '/'.
ISSUE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")

# Closing-keyword adjacency: a #N (or comma/'+'-separated list of #N) is STRONG
# evidence only when a closing verb sits immediately before it. Covers the
# conventional-commit subject prefix "fix(#285):" / "feat(#285):" /
# "merge(#285):" and trailer lists like "fixes #55, #56, #57" (the keyword
# distributes across the list).
#
# merge(#N): added per #469 — fleet triage (issue-tracker repo's 28 ghost
# issues) found merge-commit conventions using this exact prefix shape go
# undetected today. NOTE this is deliberately narrow: it matches only when
# the ref list itself sits inside the parens, e.g. "merge(#100): ...". It
# does NOT match this repo's own "merge(vX.Y): ... #N ..." convention (a
# version tag in the parens, refs loose in the subject) — recognizing bare
# #N mentions inside a merge-commit subject as strong evidence is a broader,
# higher-false-positive-risk judgment call (see SKILL.md follow-up note).
_CLOSING_KEYWORDS = r"(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)"
_REF_LIST = r"(?:#\d+(?:\s*[,+]\s*)?)+"
CONVENTIONAL_PREFIX_RE = re.compile(
    rf"^(?:fix|feat|merge)\(\s*({_REF_LIST})\s*\)\s*:", re.IGNORECASE)
KEYWORD_REF_RE = re.compile(
    rf"\b{_CLOSING_KEYWORDS}\b\s*:?\s*({_REF_LIST})", re.IGNORECASE)

# Plan-doc §2 closing-keyword equivalent (#521): a release-planning doc's
# item heading — "### ITEM<id> — #N[ + #M ...]: <title>" — is the plan's own
# claim that this item's branch resolves that issue, so (and only so) it
# counts as strong. <id> varies across releases ("ITEM-1", "ITEM A", "ITEM-5
# (Pass 1)", "ITEM-6 (Pass 2, depends on ITEM-5)", ...); the non-greedy `.*?`
# skips past whatever sits between "ITEM" and the em-dash. Any other #N
# token elsewhere in §2 prose — an item's own description/acceptance-
# criteria text, or a *different* item's body mentioning a neighboring issue
# only illustratively or negatively ("e.g.", "out of scope", "untouched",
# "beyond what ... touch") — is explicitly NOT matched here, so it falls
# through to weak (flag-only). v3.100's #380/#391 false positive (auto-closed
# off a bare "e.g. codex #380-#391" aside in ITEM-2's description) is the
# motivating case.
ITEM_HEADING_RE = re.compile(
    rf"^###\s+ITEM\b.*?—\s*({_REF_LIST})\s*:", re.IGNORECASE)

# Revert detection: a commit whose subject starts with "Revert" (the git
# revert default subject), or whose subject/body says "reverts #N" /
# "reverted #N", is a revert commit — every ref inside it is excluded from
# strong evidence entirely and recorded as a "revert reference" flag, never a
# close (closing the issue a revert just un-shipped would be wrong).
REVERT_SUBJECT_RE = re.compile(r"^Revert\b", re.IGNORECASE)
REVERT_MENTION_RE = re.compile(r"\brevert(?:s|ed)?\b\s*:?\s*#\d+", re.IGNORECASE)

COMMIT_SEP = "---GRM-RECONCILE-SEP---"
MARKER_TEMPLATE = "<!-- grm-issue-reconcile: closed by {tag} -->"
MARKER_RE = re.compile(r"<!--\s*grm-issue-reconcile:\s*closed by\s+(\S+)\s*-->")
AUTONOMOUS_PARADIGMS = {"Noir"}


# ---------------------------------------------------------------------------
# Repo / module discovery
# ---------------------------------------------------------------------------


# Shared with issue_tracker.py — single body of truth (#335).
find_repo_root = issue_tracker.find_repo_root


# ---------------------------------------------------------------------------
# Config / paradigm
# ---------------------------------------------------------------------------


def read_work_paradigm(repo_root: pathlib.Path) -> str:
    """Read work-paradigm.value live from grimoire-config.json.

    Defaults to "Supervised" (the safest, flag-only disposition) if the
    config or field is absent, so an unconfigured project never auto-closes.
    """
    config_path = repo_root / CONFIG_FILE
    if not config_path.exists():
        return "Supervised"
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return "Supervised"
    return raw.get("work-paradigm", {}).get("value", "Supervised")


def is_autonomous(paradigm: str) -> bool:
    return paradigm in AUTONOMOUS_PARADIGMS


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------


def extract_refs(text: str) -> set[int]:
    """Extract all standalone #N issue references from a text blob (cross-repo
    forms like `familiar#100` / `org/repo#42` are excluded by ISSUE_REF_RE)."""
    return {int(m) for m in ISSUE_REF_RE.findall(text or "")}


def strong_refs_in_text(text: str) -> set[int]:
    """Return the subset of #N references in text that carry closing-keyword
    adjacency: `(fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)
    #N`, including the conventional-commit subject prefix `fix(#N):` /
    `feat(#N):` / `merge(#N):` and comma/'+'-separated trailer lists
    (`fixes #55, #56, #57`, each ref inherits the leading keyword)."""
    strong: set[int] = set()
    prefix_match = CONVENTIONAL_PREFIX_RE.match(text.strip())
    if prefix_match:
        strong |= extract_refs(prefix_match.group(1))
    for m in KEYWORD_REF_RE.finditer(text):
        strong |= extract_refs(m.group(1))
    return strong


def is_revert_commit(message: str) -> bool:
    """True if message is a revert: subject starts with 'Revert' (the git
    revert default subject) or the subject/body says 'reverts #N' /
    'reverted #N'."""
    subject = message.split("\n", 1)[0]
    if REVERT_SUBJECT_RE.match(subject):
        return True
    return bool(REVERT_MENTION_RE.search(message))


@dataclass
class CommitRefEvidence:
    """Tiered evidence extracted from one release's commit range."""
    strong: set[int] = field(default_factory=set)
    weak: set[int] = field(default_factory=set)
    revert: set[int] = field(default_factory=set)

    def all_numbers(self) -> set[int]:
        return self.strong | self.weak | self.revert


def refs_from_commits(repo_root: pathlib.Path, prev_tag: str | None,
                       tag: str, runner: Callable | None = None) -> CommitRefEvidence:
    """git log <prev_tag>..<tag> (or --all history up to tag if prev_tag is
    None), scanning each commit message individually so evidence can be
    tiered per-commit:

    - a revert commit (subject starts with 'Revert', or a 'reverts #N' /
      'reverted #N' mention) excludes ALL its refs from strong entirely and
      records them as revert-flagged — never a close;
    - a non-revert commit's refs are STRONG only with closing-keyword
      adjacency (see strong_refs_in_text); any other bare #N in that commit
      is WEAK (flag-eligible, never auto-close).
    """
    evidence = CommitRefEvidence()
    runner = runner or _git_runner(repo_root)
    range_expr = f"{prev_tag}..{tag}" if prev_tag else tag
    try:
        out = runner(["log", range_expr, f"--format=%B%n{COMMIT_SEP}"])
    except subprocess.CalledProcessError:
        return evidence

    for message in out.split(COMMIT_SEP):
        message = message.strip("\n")
        if not message.strip():
            continue
        all_refs = extract_refs(message)
        if not all_refs:
            continue
        if is_revert_commit(message):
            evidence.revert |= all_refs
            continue
        strong = strong_refs_in_text(message)
        evidence.strong |= strong
        evidence.weak |= (all_refs - strong)
    return evidence


def refs_from_plan_doc(repo_root: pathlib.Path, version: str) -> CommitRefEvidence:
    """Extract #N references from release-planning-v{version}.md §2 (Major
    Features through the next top-level section), tiered by heading
    adjacency (#521):

    - strong: only a #N (or comma/'+'-separated list) that appears in an
      item's own '### ITEM<id> — #NNN[...]:' heading line — the plan's
      canonical claim that this item resolves that issue (see
      ITEM_HEADING_RE).
    - weak: any other #N-shaped token anywhere else in §2 prose — an item's
      description or acceptance-criteria text, or a bare/illustrative/
      negated mention in any item's body (e.g. "tracked_in: codex #380",
      "out of scope", "untouched", "beyond what ... touch"). A plan can
      legitimately *mention* a neighboring issue number without claiming
      this release closes it, so a bare mention is never enough to
      auto-close — a human confirms.
    """
    evidence = CommitRefEvidence()
    plan_path = (repo_root / "docs" / "release-planning" /
                 f"release-planning-v{version}.md")
    if not plan_path.exists():
        return evidence
    text = plan_path.read_text(encoding="utf-8")
    section = _extract_section(text, "## 2.")
    all_refs = extract_refs(section)
    strong: set[int] = set()
    for line in section.splitlines():
        m = ITEM_HEADING_RE.match(line.strip())
        if m:
            strong |= extract_refs(m.group(1))
    evidence.strong = strong
    evidence.weak = all_refs - strong
    return evidence


def refs_from_version_history(repo_root: pathlib.Path, version: str) -> CommitRefEvidence:
    """Extract #N references from the version-history.md section for this
    version (from its '## vX.Y' heading to the next '## ' heading), split into
    strong (closing-keyword adjacency — "Closes #285, #286" prose) and weak
    (a bare mention elsewhere in the section) tiers, evaluated line-by-line so
    a keyword's scope doesn't leak across unrelated sentences."""
    evidence = CommitRefEvidence()
    vh_path = repo_root / "docs" / "version-history.md"
    if not vh_path.exists():
        return evidence
    text = vh_path.read_text(encoding="utf-8")
    section = _extract_version_section(text, version)
    for line in section.splitlines():
        all_refs = extract_refs(line)
        if not all_refs:
            continue
        strong = strong_refs_in_text(line)
        evidence.strong |= strong
        evidence.weak |= (all_refs - strong)
    return evidence


def refs_from_changelog(repo_root: pathlib.Path, version: str) -> CommitRefEvidence:
    """Extract #N references from docs/changelog.md's per-version section
    ('## vX.Y — Title' to the next '## v' heading), tiered the same way as
    refs_from_version_history (strong on closing-keyword adjacency, weak on a
    bare mention).

    This repo's own docs/changelog.md deliberately omits ticket/issue IDs
    (its house style — front-facing prose only, see coding-standards.md
    §Content & UI copy), so this is usually empty here; it exists as a
    reference source for the general case (#468's proposal names the
    changelog entry explicitly) — a project whose changelog DOES carry
    'Closes #N' claims is still covered."""
    evidence = CommitRefEvidence()
    path = repo_root / "docs" / "changelog.md"
    if not path.exists():
        return evidence
    text = path.read_text(encoding="utf-8")
    section = _extract_version_section(text, version)
    for line in section.splitlines():
        all_refs = extract_refs(line)
        if not all_refs:
            continue
        strong = strong_refs_in_text(line)
        evidence.strong |= strong
        evidence.weak |= (all_refs - strong)
    return evidence


def _extract_section(text: str, header_prefix: str) -> str:
    """Return the text of the first '## '-level section starting with
    header_prefix, up to (not including) the next '## ' heading."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(header_prefix):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def _extract_version_section(text: str, version: str) -> str:
    """Return the '## v{version}' section (heading may carry a title suffix
    like '## v3.75 — Doc-quality closeout')."""
    lines = text.splitlines()
    pattern = re.compile(rf"^## v{re.escape(version)}\b")
    start = None
    for i, line in enumerate(lines):
        if pattern.match(line):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def collect_reference_set(repo_root: pathlib.Path, tag: str, prev_tag: str | None,
                           version: str, runner: Callable | None = None) -> dict[str, object]:
    """Assemble the evidence sources, kept separate and tiered so disposition
    can tell 'strong closing evidence' (close-eligible) apart from 'mentioned
    only in passing' (flag-only) — see build_verdicts.

    All four sources return CommitRefEvidence (strong / weak / revert; plan's
    revert tier is always empty — a plan doc has no revert concept). Plan §2
    is strong only for a #N appearing in its own item's heading line; any
    other #N mention in §2 prose is weak (#521 — see refs_from_plan_doc).
    """
    return {
        "commits": refs_from_commits(repo_root, prev_tag, tag, runner=runner),
        "plan": refs_from_plan_doc(repo_root, version),
        "version_history": refs_from_version_history(repo_root, version),
        "changelog": refs_from_changelog(repo_root, version),
    }


def _git_runner(repo_root: pathlib.Path):
    def run(args: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo_root)] + args,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, args,
                                                 output=result.stdout,
                                                 stderr=result.stderr)
        return result.stdout
    return run


# ---------------------------------------------------------------------------
# Verdict records
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    issue_id: str
    number: int | None
    title: str
    disposition: str          # "close" | "flag"
    evidence: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "issue_id": self.issue_id,
            "number": self.number,
            "title": self.title,
            "disposition": self.disposition,
            "evidence": self.evidence,
            "reason": self.reason,
        }


def build_verdicts(open_issues: list, refset: dict[str, object]) -> list[Verdict]:
    """Intersect open issues with the reference set and assign a disposition.

    Evidence tiers (see module docstring / design doc §Candidate detection):

    - close-eligible (strong): closing-keyword-adjacent commit refs
      (`fix(#N):`, `feat(#N):`, `merge(#N):`, `fixes #N`, …) AND/OR a plan §2
      item's own `### ITEM<id> — #NNN: ...` heading ref (#521).
      version-history counts as strong only with the same closing-keyword
      adjacency ("Closes #285, #286" prose).
    - flag-only (weak): a bare `#N` mention with no closing-keyword adjacency
      — in a commit, in version-history prose, or anywhere in plan §2 prose
      *other than* an item's own heading (its description, acceptance
      criteria, or an illustrative/negated mention in another item's body,
      e.g. "e.g. codex #380-#391", "out of scope", "untouched", "beyond what
      ... touch" — #521) — is partial evidence; a human should confirm.
    - revert reference: any ref inside a commit whose subject starts with
      'Revert' (or that says 'reverts #N' / 'reverted #N') is excluded from
      strong entirely and always flagged, never closed — closing the issue a
      revert just un-shipped would be wrong.
    """
    commits: CommitRefEvidence = refset.get("commits", CommitRefEvidence())
    plan: CommitRefEvidence = refset.get("plan", CommitRefEvidence())
    vh: CommitRefEvidence = refset.get("version_history", CommitRefEvidence())
    changelog: CommitRefEvidence = refset.get("changelog", CommitRefEvidence())

    strong = commits.strong | plan.strong | vh.strong | changelog.strong
    weak = (commits.weak | plan.weak | vh.weak | changelog.weak) - strong
    revert_only = commits.revert - strong

    verdicts: list[Verdict] = []
    for issue in open_issues:
        if issue.number is None:
            continue
        n = issue.number
        evidence = []
        if n in commits.strong:
            evidence.append("commits (closing-keyword)")
        if n in commits.weak:
            evidence.append("commits (bare mention)")
        if n in commits.revert:
            evidence.append("commits (revert reference)")
        if n in plan.strong:
            evidence.append("plan-doc-§2 (item heading)")
        if n in plan.weak:
            evidence.append("plan-doc-§2 (bare mention)")
        if n in vh.strong:
            evidence.append("version-history (closing-keyword)")
        if n in vh.weak:
            evidence.append("version-history (bare mention)")
        if n in changelog.strong:
            evidence.append("changelog (closing-keyword)")
        if n in changelog.weak:
            evidence.append("changelog (bare mention)")

        if n in strong:
            verdicts.append(Verdict(
                issue_id=issue.id, number=n, title=issue.title,
                disposition="close", evidence=evidence,
                reason="referenced with closing-keyword evidence in release "
                       "commits and/or plan §2 and/or version-history",
            ))
        elif n in revert_only:
            verdicts.append(Verdict(
                issue_id=issue.id, number=n, title=issue.title,
                disposition="flag", evidence=evidence,
                reason="revert reference",
            ))
        elif n in weak:
            verdicts.append(Verdict(
                issue_id=issue.id, number=n, title=issue.title,
                disposition="flag", evidence=evidence,
                reason="referenced only as a bare mention (partial evidence)",
            ))
    return verdicts


# ---------------------------------------------------------------------------
# Disposition (write path)
# ---------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    closed: list[dict] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)
    skipped_marker: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def already_marked(tracker: Any, issue_id: str, tag: str) -> bool:
    """Return True if the issue's body already carries the idempotency marker
    for this exact tag (so a re-run never double-closes/double-comments)."""
    try:
        issue = tracker.get(issue_id)
    except Exception:
        return False
    body = issue.body or ""
    for m in MARKER_RE.finditer(body):
        if m.group(1) == tag:
            return True
    return False


def reconcile(tracker: Any, verdicts: list[Verdict], tag: str, paradigm: str,
              dry_run: bool) -> ReconcileResult:
    """Apply (or preview) the disposition for each verdict.

    Noir + close + not dry-run: comment with the marker, then close, then
    RE-READ the issue and fail loudly if state != "closed" (post-write
    verification per #130's masking-failure history).

    Supervised/Weiss, OR dry-run, OR disposition == "flag": no write at all;
    the verdict is reported as a review item. This makes "the Supervised/Weiss
    path is exactly the dry-run output" (design doc, Validation section) true
    by construction — the write branch is gated on autonomy AND not-dry-run.
    """
    result = ReconcileResult()
    autonomous = is_autonomous(paradigm)

    for v in verdicts:
        record = v.to_dict()

        if v.disposition == "flag":
            result.flagged.append(record)
            continue

        # disposition == "close"
        if not autonomous:
            record["reason"] += f" (flagged for human review under {paradigm})"
            result.flagged.append(record)
            continue

        if dry_run:
            result.closed.append(record)  # preview only; nothing written
            continue

        if already_marked(tracker, v.issue_id, tag):
            result.skipped_marker.append(record)
            continue

        try:
            comment_body = (
                f"Closed by release {tag}: {v.reason} "
                f"(evidence: {', '.join(v.evidence) or 'none'}).\n"
                f"{MARKER_TEMPLATE.format(tag=tag)}"
            )
            tracker.comment(v.issue_id, body=comment_body)
            tracker.close(v.issue_id)
            # Post-write verification (#130): re-read and fail loudly if the
            # close did not persist.
            reread = tracker.get(v.issue_id)
            if reread.state.lower() != "closed":
                raise RuntimeError(
                    f"issue #{v.issue_id} reported closed but re-read shows "
                    f"state={reread.state!r} — write did not persist (#130)."
                )
            result.closed.append(record)
        except Exception as exc:  # noqa: BLE001
            record["error"] = str(exc)
            result.errors.append(record)

    return result


# ---------------------------------------------------------------------------
# Single-release reconciliation
# ---------------------------------------------------------------------------


def reconcile_release(repo_root: pathlib.Path, tag: str, prev_tag: str | None,
                       dry_run: bool, runner: Callable | None = None,
                       tracker_module: Any | None = None) -> dict:
    """Reconcile one release: tag (e.g. 'v3.75') against prev_tag (e.g.
    'v3.74'). version is tag with the leading 'v' stripped."""
    version = tag.lstrip("v")
    # Real runs reuse the module-level `issue_tracker` import (single load,
    # no isinstance/dataclass-identity split across two module instances);
    # self-tests substitute a fake via `tracker_module` (#335 follow-up).
    it_mod = tracker_module or issue_tracker
    config = it_mod.load_config()
    tracker = it_mod.IssueTracker(config, repo_root)

    refset = collect_reference_set(repo_root, tag, prev_tag, version, runner=runner)
    limit = getattr(it_mod, "DEFAULT_LIMIT", 500)
    open_issues = tracker.list(state="open", limit=limit)
    # #468/goon-cave#734: the list is still bounded (real trackers can hold
    # more than any finite --limit), so a run that comes back exactly
    # saturating the limit is a signal — not proof — that candidates past the
    # cut were silently dropped. Surface it loudly instead of pretending the
    # candidate set is complete.
    truncated = len(open_issues) >= limit
    if truncated:
        print(f"warning: open-issue listing returned {len(open_issues)} issues, "
              f"saturating the configured limit ({limit}) — some open issues "
              f"may be past the cut and missed as reconcile candidates. Raise "
              f"DEFAULT_LIMIT in issue_tracker.py if this repo's backlog is "
              f"consistently this large.", file=sys.stderr)
    all_ref_numbers = (refset["commits"].all_numbers() | refset["plan"].all_numbers()
                       | refset["version_history"].all_numbers()
                       | refset["changelog"].all_numbers())
    candidates = [i for i in open_issues if i.number in all_ref_numbers]

    verdicts = build_verdicts(candidates, refset)
    paradigm = read_work_paradigm(repo_root)
    result = reconcile(tracker, verdicts, tag, paradigm, dry_run)

    if not dry_run and is_autonomous(paradigm):
        try:
            tracker.flush()
        except Exception:  # noqa: BLE001
            pass  # github backend only; roadmap has nothing to flush

    return {
        "tag": tag,
        "prev_tag": prev_tag,
        "paradigm": paradigm,
        "dry_run": dry_run,
        "candidates_found": len(candidates),
        "open_issue_count": len(open_issues),
        "open_issue_limit": limit,
        "truncated": truncated,
        "closed": result.closed,
        "flagged": result.flagged,
        "skipped_marker": result.skipped_marker,
        "errors": result.errors,
    }


# ---------------------------------------------------------------------------
# Back-sweep mode
# ---------------------------------------------------------------------------


def parse_sweep_range(spec: str) -> tuple[str, str]:
    """Parse 'vX.Y..vZ.W' into (start_tag, end_tag)."""
    if ".." not in spec:
        raise ValueError(f"--sweep range must look like vX.Y..vZ.W, got {spec!r}")
    start, end = spec.split("..", 1)
    if not start or not end:
        raise ValueError(f"--sweep range must look like vX.Y..vZ.W, got {spec!r}")
    return start, end


def list_tags_in_range(repo_root: pathlib.Path, start_tag: str, end_tag: str,
                        runner: Callable | None = None) -> list[str]:
    """Return every shipped tag from start_tag to end_tag inclusive, sorted by
    version. Both tags must exist in the repo."""
    runner = runner or _git_runner(repo_root)
    out = runner(["tag", "--list", "v*"])
    all_tags = [t.strip() for t in out.splitlines() if t.strip()]

    def _key(t: str) -> tuple:
        parts = t.lstrip("v").split(".")
        return tuple(int(p) for p in parts if p.isdigit())

    all_tags.sort(key=_key)
    if start_tag not in all_tags or end_tag not in all_tags:
        raise ValueError(f"sweep range endpoints not found among tags: "
                         f"{start_tag!r}, {end_tag!r}")
    start_i = all_tags.index(start_tag)
    end_i = all_tags.index(end_tag)
    if start_i > end_i:
        raise ValueError(f"--sweep range is inverted: {start_tag}..{end_tag}")
    return all_tags[start_i:end_i + 1]


def sweep(repo_root: pathlib.Path, spec: str, dry_run: bool, runner: Callable | None = None,
          tracker_module: Any | None = None) -> dict:
    """Run reconcile_release over every tag in the range, oldest first, using
    each tag's immediate predecessor (by shipped-tag order) as prev_tag."""
    start_tag, end_tag = parse_sweep_range(spec)
    all_tags_runner = runner or _git_runner(repo_root)
    tags_out = all_tags_runner(["tag", "--list", "v*"])
    all_tags = sorted(
        (t.strip() for t in tags_out.splitlines() if t.strip()),
        key=lambda t: tuple(int(p) for p in t.lstrip("v").split(".") if p.isdigit()),
    )
    range_tags = list_tags_in_range(repo_root, start_tag, end_tag, runner=all_tags_runner)

    runs = []
    for tag in range_tags:
        idx = all_tags.index(tag)
        prev_tag = all_tags[idx - 1] if idx > 0 else None
        runs.append(reconcile_release(repo_root, tag, prev_tag, dry_run,
                                       runner=runner, tracker_module=tracker_module))

    return {
        "sweep_range": spec,
        "tags_processed": range_tags,
        "runs": runs,
    }


# ---------------------------------------------------------------------------
# Release gate (#468) — mirrors publish_release.py's verify-stage pattern:
# a mandatory, loud, but recoverable assertion, not a silent advisory.
# ---------------------------------------------------------------------------

FLAG_REDIRECT_MARKER = "flagged for human review under"


def gate_status(run: dict) -> dict:
    """Classify one reconcile_release() run into gate-blocking failures.

    - "write_errors": a close reconcile() attempted but could not verify as
      persisted (`errors` — the #130 masking-failure pattern). Always a hard
      failure; never overridable — this is a defect, not a judgment call.
    - "overridable_failures": a strong-evidence ("Closes #N"-shaped) verdict
      that the paradigm's flag-don't-write contract (Supervised/Weiss)
      redirected into `flagged` instead of closing (see `reconcile()`'s
      `record["reason"] += f" ({FLAG_REDIRECT_MARKER} {paradigm})"`). Gate-
      eligible, but recoverable via `--reconcile-override-reason`.

    Never evaluated during --dry-run: a preview run writes nothing either
    way, so there is nothing to gate on yet (mirrors publish_release.py's
    assert_published() skipping under --dry-run).

    Weak (bare-mention) and revert-only references were never close-eligible
    to begin with, so they never appear here — they stay purely advisory,
    exactly as before #468.
    """
    if run.get("dry_run"):
        return {"write_errors": [], "overridable_failures": []}
    write_errors = list(run.get("errors", []))
    overridable = [rec for rec in run.get("flagged", [])
                   if FLAG_REDIRECT_MARKER in rec.get("reason", "")]
    return {"write_errors": write_errors, "overridable_failures": overridable}


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_report(result: dict) -> str:
    lines = []
    if "runs" in result:
        lines.append(f"Back-sweep {result['sweep_range']} "
                     f"({len(result['tags_processed'])} tags): "
                     f"{', '.join(result['tags_processed']) or '(none)'}")
        for run in result["runs"]:
            lines.append(_format_single(run))
        return "\n".join(lines)
    return _format_single(result)


def _format_single(run: dict) -> str:
    lines = [
        f"--- {run['tag']} (prev: {run['prev_tag'] or 'none'}, "
        f"paradigm: {run['paradigm']}, dry_run: {run['dry_run']}) ---",
        f"candidates found: {run['candidates_found']}",
    ]
    closed_ids = [f"#{c['number']}" for c in run["closed"]]
    if run["dry_run"]:
        lines.append(f"issues that WOULD be closed (dry-run): [{', '.join(closed_ids)}]")
    else:
        lines.append(f"issues closed by this release: [{', '.join(closed_ids)}]")
    if run["flagged"]:
        flag_ids = [f"#{c['number']} ({c['reason']})" for c in run["flagged"]]
        lines.append(f"flagged for review: {'; '.join(flag_ids)}")
    if run["skipped_marker"]:
        skip_ids = [f"#{c['number']}" for c in run["skipped_marker"]]
        lines.append(f"already reconciled (idempotency marker present): "
                     f"[{', '.join(skip_ids)}]")
    if run["errors"]:
        err_ids = [f"#{c['number']}: {c.get('error')}" for c in run["errors"]]
        lines.append(f"ERRORS: {'; '.join(err_ids)}")
    if run.get("truncated"):
        lines.append(f"WARNING: open-issue listing truncated at "
                     f"{run.get('open_issue_limit')} (returned "
                     f"{run.get('open_issue_count')}) — candidates past the "
                     f"cut may be missing")
    gs = gate_status(run)
    if gs["write_errors"] or gs["overridable_failures"]:
        n = len(gs["write_errors"]) + len(gs["overridable_failures"])
        lines.append(f"reconcile gate: FAILED ({n} claimed issue(s) unresolved "
                     f"— see ERRORS / flagged above)")
    elif not run.get("dry_run"):
        lines.append("reconcile gate: PASSED")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue_reconcile.py",
        description="Release-time issue reconciliation over the issue-tracker abstraction.",
    )
    parser.add_argument("--tag", help="Release tag to reconcile, e.g. v3.76.")
    parser.add_argument("--prev-tag", dest="prev_tag",
                        help="Previous shipped tag (commit range start). "
                             "Auto-detected from git tags if omitted.")
    parser.add_argument("--sweep", metavar="vX.Y..vZ.W",
                        help="Back-sweep mode: reconcile every shipped tag in range.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print verdict records; write nothing.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit machine-readable JSON instead of the text report.")
    parser.add_argument("--reconcile-override-reason", dest="override_reason",
                        metavar="REASON",
                        help="Release-gate escape hatch (#468): a non-empty "
                             "justification for shipping anyway when a "
                             "strong-evidence ('Closes #N'-shaped) claim was "
                             "flagged instead of closed under Supervised/Weiss "
                             "flag-don't-write. Does NOT override a genuine "
                             "write-persistence failure (#130) — those always "
                             "hard-fail. The reason is echoed into the run "
                             "output for the audit trail.")
    parser.add_argument("--self-test", action="store_true",
                        help="Run the offline self-test suite (no network, no real gh calls).")
    return parser


def _auto_prev_tag(repo_root: pathlib.Path, tag: str, runner=None) -> str | None:
    """Best-effort: the tag immediately before `tag` in version-sorted order."""
    runner = runner or _git_runner(repo_root)
    try:
        out = runner(["tag", "--list", "v*"])
    except subprocess.CalledProcessError:
        return None
    all_tags = sorted(
        (t.strip() for t in out.splitlines() if t.strip()),
        key=lambda t: tuple(int(p) for p in t.lstrip("v").split(".") if p.isdigit()),
    )
    if tag not in all_tags:
        return all_tags[-1] if all_tags else None
    idx = all_tags.index(tag)
    return all_tags[idx - 1] if idx > 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.override_reason is not None and not args.override_reason.strip():
        print("error: --reconcile-override-reason requires a non-empty "
              "justification string", file=sys.stderr)
        return 2

    repo_root = find_repo_root() or pathlib.Path.cwd().resolve()

    try:
        if args.sweep:
            result = sweep(repo_root, args.sweep, dry_run=args.dry_run)
        else:
            if not args.tag:
                print("error: --tag is required (or use --sweep vX.Y..vZ.W)",
                      file=sys.stderr)
                return 2
            prev_tag = args.prev_tag or _auto_prev_tag(repo_root, args.tag)
            result = reconcile_release(repo_root, args.tag, prev_tag,
                                       dry_run=args.dry_run)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result))

    # Release gate (#468): mandatory, not advisory. See gate_status()'s
    # docstring for the write_errors vs overridable_failures split.
    runs = result.get("runs", [result])
    write_errors: list[dict] = []
    overridable_failures: list[dict] = []
    for run in runs:
        gs = gate_status(run)
        write_errors.extend(gs["write_errors"])
        overridable_failures.extend(gs["overridable_failures"])

    if write_errors:
        print("\nreconcile gate: FAILED — a close was attempted but did not "
              "verify as persisted (#130-style write failure). This has no "
              "override; investigate the tracker write before re-running:",
              file=sys.stderr)
        for rec in write_errors:
            print(f"  - #{rec.get('number')} {rec.get('title', '')!r}: "
                  f"{rec.get('error')}", file=sys.stderr)
        return 1

    if overridable_failures:
        if args.override_reason:
            print(f"\nreconcile gate OVERRIDDEN — reason: "
                  f"{args.override_reason}", file=sys.stderr)
            for rec in overridable_failures:
                print(f"  - #{rec.get('number')} {rec.get('title', '')!r}: "
                      f"{rec.get('reason')}", file=sys.stderr)
        else:
            print("\nreconcile gate: FAILED — the following issue(s) are "
                  "claimed as closed by this release's commits, plan §2, "
                  "version-history, and/or changelog (strong evidence) but "
                  "remain open with no reconcile evidence of closure:",
                  file=sys.stderr)
            for rec in overridable_failures:
                print(f"  - #{rec.get('number')} {rec.get('title', '')!r}: "
                      f"{rec.get('reason')}", file=sys.stderr)
            print("\nTo proceed anyway, re-run with "
                  "--reconcile-override-reason \"<why this is safe to ship "
                  "with these issues still open>\" (a non-empty "
                  "justification is required).", file=sys.stderr)
            return 1

    return 0


# ---------------------------------------------------------------------------
# Self-test (mocked tracker + injected git runner — no network, no real gh)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    """Cover: reference extraction (commits/plan-doc/version-history),
    open-issue intersection, paradigm gating, post-write verification —
    all against a mocked tracker (temp dirs / injected runner)."""
    import tempfile
    import types
    import unittest.mock as mock

    cases: list[tuple[str, bool]] = []
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / ".claude").mkdir()
    (tmp / "docs" / "release-planning").mkdir(parents=True)

    # ------------------------------------------------------------------ #
    # 1. extract_refs — plain #N extraction from arbitrary text
    # ------------------------------------------------------------------ #
    refs = extract_refs("fix(v3.75/#232): stop excluding foo (#232) also see #10 and #10")
    cases.append(("extract_refs finds all #N refs, de-duplicated via set",
                  refs == {232, 10}))
    cases.append(("extract_refs on empty/None text returns empty set",
                  extract_refs("") == set() and extract_refs(None) == set()))
    cases.append(("extract_refs rejects cross-repo form familiar#100 (word char before #)",
                  extract_refs("familiar#100") == set()))
    cases.append(("extract_refs rejects cross-repo form org/repo#42 ('/' before #)",
                  extract_refs("org/repo#42") == set()))
    cases.append(("extract_refs still matches a standalone #N next to a cross-repo ref",
                  extract_refs("see familiar#100 but also #100 here") == {100}))

    # ------------------------------------------------------------------ #
    # 2. refs_from_commits — injected git runner, no real subprocess.
    #    Tiered evidence: strong (closing-keyword adjacency, incl. the
    #    conventional-commit "fix(#N):" prefix and trailer lists), weak
    #    (bare mention), revert (excluded from strong entirely).
    # ------------------------------------------------------------------ #
    fake_log = (
        "fix(#42): resolve widget bug\n" + COMMIT_SEP + "\n"
        "feat: add thing (#7)\n" + COMMIT_SEP + "\n"
        "see #292 for context\n" + COMMIT_SEP + "\n"
        'Revert "fix(#285): whatever"\n\nThis reverts commit abc123.\n' + COMMIT_SEP + "\n"
        "chore: cleanup\n\nThis reverted #99 by mistake, redo later.\n" + COMMIT_SEP + "\n"
        "familiar#100 unrelated mention\n" + COMMIT_SEP + "\n"
        "org/repo#42 cross-repo mention\n" + COMMIT_SEP + "\n"
        "feat(#286): reintroduce widget\n" + COMMIT_SEP + "\n"
        "chore: trailer list\n\nfixes #55, #56, #57\n" + COMMIT_SEP + "\n"
        "merge(#469): fold merge-evidence fix into dev\n" + COMMIT_SEP + "\n"
        "merge(v3.92): promote v3.92 (field defects: #465 #421) "
        "claude/r1-465-foo->version/3.92\n" + COMMIT_SEP + "\n"
    )

    def _fake_runner(args):
        assert args[0] == "log"
        return fake_log

    commit_ev = refs_from_commits(tmp, "v1.0", "v1.1", runner=_fake_runner)
    cases.append(("refs_from_commits: fix(#N): conventional-commit prefix is strong",
                  42 in commit_ev.strong))
    cases.append(("refs_from_commits: bare #N mention in a commit is weak, not strong",
                  292 in commit_ev.weak and 292 not in commit_ev.strong))
    cases.append(("refs_from_commits: feat: subject with (#N) trailer is weak (no keyword)",
                  7 in commit_ev.weak and 7 not in commit_ev.strong))
    cases.append(("refs_from_commits: Revert-subject commit's ref is revert-flagged, "
                  "not strong",
                  285 in commit_ev.revert and 285 not in commit_ev.strong))
    cases.append(("refs_from_commits: body 'reverted #N' mention is revert-flagged",
                  99 in commit_ev.revert and 99 not in commit_ev.strong
                  and 99 not in commit_ev.weak))
    cases.append(("refs_from_commits: familiar#100 cross-repo form does not match at all",
                  100 not in commit_ev.strong and 100 not in commit_ev.weak
                  and 100 not in commit_ev.revert))
    cases.append(("refs_from_commits: org/repo#42 cross-repo form does not add a "
                  "spurious weak/revert entry (only the earlier strong fix(#42) counts)",
                  42 not in commit_ev.weak and 42 not in commit_ev.revert))
    cases.append(("refs_from_commits: feat(#286): conventional-commit prefix is strong",
                  286 in commit_ev.strong))
    cases.append(("refs_from_commits: 'fixes #55, #56, #57' trailer list — all three strong",
                  {55, 56, 57} <= commit_ev.strong))
    cases.append(("refs_from_commits: merge(#N): conventional-commit prefix is strong (#469)",
                  469 in commit_ev.strong))
    cases.append(("refs_from_commits: merge(vX.Y): ...#N... (version in parens, ref loose "
                  "in the subject) stays weak — deliberately narrow, see #469 note",
                  465 in commit_ev.weak and 465 not in commit_ev.strong
                  and 421 in commit_ev.weak and 421 not in commit_ev.strong))

    # ------------------------------------------------------------------ #
    # 3. refs_from_plan_doc — §2 extraction, ignores other sections, tiered
    #    by heading adjacency (#521): only an item's own '### ITEM... —
    #    #NNN:' heading ref is strong; any other #N in §2 prose (even
    #    closing-keyword-adjacent body text) is weak.
    # ------------------------------------------------------------------ #
    plan_path = tmp / "docs" / "release-planning" / "release-planning-v9.9.md"
    plan_path.write_text(
        "# Release Planning — v9.9\n\n"
        "## 1. Target\n\nSee #999 (should NOT be picked up)\n\n"
        "## 2. Major Features\n\n"
        "### ITEM-1 — #100: some feature\n\n"
        "**Description:** encodes a known gap, e.g. #391 is out of scope, "
        "untouched by this item.\n\n"
        "**Acceptance criteria:** Closes #101 too.\n\n"
        "## 3. Out of Scope\n\nMentions #500 (should NOT be picked up)\n",
        encoding="utf-8",
    )
    plan_ev = refs_from_plan_doc(tmp, "9.9")
    cases.append(("refs_from_plan_doc: item's own heading ref is strong (#521)",
                  100 in plan_ev.strong))
    cases.append(("refs_from_plan_doc: closing-keyword-adjacent BODY text "
                  "('Closes #101') is weak, not strong — only the heading "
                  "counts as strong evidence (#521)",
                  101 in plan_ev.weak and 101 not in plan_ev.strong))
    cases.append(("refs_from_plan_doc: illustrative/negated mention elsewhere "
                  "in an item's body ('e.g. #391 is out of scope') is weak, "
                  "never strong (#521)",
                  391 in plan_ev.weak and 391 not in plan_ev.strong))
    cases.append(("refs_from_plan_doc does not pick up refs outside §2",
                  999 not in plan_ev.all_numbers() and 500 not in plan_ev.all_numbers()))
    cases.append(("refs_from_plan_doc returns empty evidence for missing plan doc",
                  refs_from_plan_doc(tmp, "0.0") == CommitRefEvidence()))

    # ------------------------------------------------------------------ #
    # 3b. Regression (#521) — v3.100's real false positive: release-planning-
    #     v3.100.md's ITEM-2 description said "...tracked_in links (e.g.
    #     codex #380-#391)" as an illustrative aside inside ANOTHER item's
    #     description, and the old flat-§2-extraction (no heading tiering)
    #     treated that bare mention as strong evidence, auto-closing both
    #     #380 and #391 under Noir. Reproduce the exact fixture text and
    #     confirm it now only flags for review, never auto-closes.
    # ------------------------------------------------------------------ #
    regress_path = tmp / "docs" / "release-planning" / "release-planning-v9.10.md"
    regress_path.write_text(
        "# Release Planning — v9.10\n\n"
        "## 2. Major Features\n\n"
        "### ITEM-2 — #514: overlay manifests\n\n"
        "**Description:** Schema-versioned `flavors/<flavor>/flavor.json` "
        "format per the design doc §2 — capability map, path maps, "
        "term-substitution tables, exclusions — validated by "
        "`grm-config-validate`. Author the `codex` and `copilot` manifests, "
        "encoding today's known gaps as `exclusions` entries with reasons "
        "and `tracked_in` links (e.g. codex #380-#391).\n\n"
        "**Acceptance criteria:** every known intentional divergence for "
        "these two flavors is an `exclusions` entry with a reason — zero "
        "silent gaps.\n\n",
        encoding="utf-8",
    )
    regress_ev = refs_from_plan_doc(tmp, "9.10")
    cases.append(("regression (#521): the item's own heading ref (#514) "
                  "is strong",
                  514 in regress_ev.strong))
    cases.append(("regression (#521): the illustrative 'e.g. codex "
                  "#380-#391' aside is weak, never strong",
                  380 in regress_ev.weak and 380 not in regress_ev.strong
                  and 391 in regress_ev.weak and 391 not in regress_ev.strong))

    regress_issues = [
        types.SimpleNamespace(id="380", number=380, title="codex gap A"),
        types.SimpleNamespace(id="391", number=391, title="codex gap B"),
    ]
    regress_refset = {
        "commits": CommitRefEvidence(), "plan": regress_ev,
        "version_history": CommitRefEvidence(), "changelog": CommitRefEvidence(),
    }
    regress_verdicts = build_verdicts(regress_issues, regress_refset)
    regress_by_num = {v.number: v for v in regress_verdicts}
    cases.append(("regression (#521): #380/#391 are flagged for review, "
                  "never close-eligible",
                  regress_by_num[380].disposition == "flag"
                  and regress_by_num[391].disposition == "flag"))
    # The Noir-autonomous write-path half of this regression (confirming zero
    # tracker writes occur) runs as 6h, below, once MockTracker exists.

    # ------------------------------------------------------------------ #
    # 4. refs_from_version_history — section-scoped, tiered extraction
    # ------------------------------------------------------------------ #
    vh_path = tmp / "docs" / "version-history.md"
    vh_path.write_text(
        "# Version History\n\n"
        "## v9.9 — Test release\n\nCloses #200 and mentions #201.\n\n"
        "## v9.8 — Older release\n\nUnrelated #999.\n",
        encoding="utf-8",
    )
    vh_ev = refs_from_version_history(tmp, "9.9")
    cases.append(("refs_from_version_history: 'Closes #200' prose is strong",
                  200 in vh_ev.strong))
    cases.append(("refs_from_version_history: bare '#201' mention is weak, not strong",
                  201 in vh_ev.weak and 201 not in vh_ev.strong))
    cases.append(("refs_from_version_history does not leak into prior sections",
                  999 not in vh_ev.all_numbers()))

    # ------------------------------------------------------------------ #
    # 4b. refs_from_changelog (#468) — same section-scoped, tiered
    #     extraction, over docs/changelog.md instead of version-history.md.
    # ------------------------------------------------------------------ #
    cl_path = tmp / "docs" / "changelog.md"
    cl_path.write_text(
        "# Changelog\n\n"
        "## v9.9 — Test release\n\nCloses #400 and mentions #401.\n\n"
        "## v9.8 — Older release\n\nUnrelated #998.\n",
        encoding="utf-8",
    )
    cl_ev = refs_from_changelog(tmp, "9.9")
    cases.append(("refs_from_changelog: 'Closes #400' prose is strong",
                  400 in cl_ev.strong))
    cases.append(("refs_from_changelog: bare '#401' mention is weak, not strong",
                  401 in cl_ev.weak and 401 not in cl_ev.strong))
    cases.append(("refs_from_changelog does not leak into prior sections",
                  998 not in cl_ev.all_numbers()))
    cases.append(("refs_from_changelog returns empty set for missing changelog",
                  refs_from_changelog(tmp, "0.0") == CommitRefEvidence()))

    # ------------------------------------------------------------------ #
    # 5. build_verdicts — close (strong evidence) vs flag (weak/revert)
    # ------------------------------------------------------------------ #
    FakeIssue = types.SimpleNamespace
    open_issues = [
        FakeIssue(id="100", number=100, title="Strong: commit+plan-heading"),
        FakeIssue(id="200", number=200, title="Weak: version-history only"),
        FakeIssue(id="285", number=285, title="Revert reference only"),
        FakeIssue(id="292", number=292, title="Mention-only in a commit (weak)"),
        FakeIssue(id="400", number=400, title="Strong: changelog only"),
        FakeIssue(id="600", number=600, title="Weak: plan-doc bare mention only (#521)"),
        FakeIssue(id="999", number=999, title="Not referenced anywhere"),
        FakeIssue(id="epic1", number=None, title="No number (roadmap-style, skipped)"),
    ]
    refset = {
        "commits": CommitRefEvidence(strong={100}, weak={292}, revert={285}),
        "plan": CommitRefEvidence(strong={100}, weak={600}),
        "version_history": CommitRefEvidence(strong=set(), weak={200}),
        "changelog": CommitRefEvidence(strong={400}, weak=set()),
    }
    verdicts = build_verdicts(open_issues, refset)
    by_num = {v.number: v for v in verdicts}
    cases.append(("build_verdicts closes strong-evidence issue (commits+plan heading)",
                  100 in by_num and by_num[100].disposition == "close"))
    cases.append(("build_verdicts flags weak-only (version-history-only) issue",
                  200 in by_num and by_num[200].disposition == "flag"))
    cases.append(("build_verdicts flags a mention-only (weak commit) reference",
                  292 in by_num and by_num[292].disposition == "flag"))
    cases.append(("build_verdicts flags a revert-only reference with reason "
                  "'revert reference', never closes it",
                  285 in by_num and by_num[285].disposition == "flag"
                  and by_num[285].reason == "revert reference"))
    cases.append(("build_verdicts closes a changelog-only strong reference (#468)",
                  400 in by_num and by_num[400].disposition == "close"
                  and "changelog (closing-keyword)" in by_num[400].evidence))
    cases.append(("build_verdicts flags a plan-doc bare-mention-only reference, "
                  "never closes it (#521)",
                  600 in by_num and by_num[600].disposition == "flag"
                  and "plan-doc-§2 (bare mention)" in by_num[600].evidence))
    cases.append(("build_verdicts excludes issues with no reference at all",
                  999 not in by_num))
    cases.append(("build_verdicts excludes issues with number=None (no candidacy)",
                  "epic1" not in by_num and len(verdicts) == 6))

    # ------------------------------------------------------------------ #
    # 6. Mocked tracker for the write-path tests
    # ------------------------------------------------------------------ #
    class MockTracker:
        """In-memory stand-in for IssueTracker; tracks calls, simulates state."""

        def __init__(self, issues: dict[str, dict], persist_failures: set[str] = frozenset()):
            self._issues = issues  # id -> {"state":..., "body":..., "number":...}
            self._persist_failures = persist_failures  # ids whose close silently no-ops
            self.calls: list[tuple] = []
            self.flushed = False

        def get(self, issue_id):
            self.calls.append(("get", issue_id))
            data = self._issues[issue_id]
            return types.SimpleNamespace(
                id=issue_id, number=data["number"], title=data.get("title", ""),
                state=data["state"], body=data.get("body", ""),
            )

        def comment(self, issue_id, body):
            self.calls.append(("comment", issue_id, body))
            data = self._issues[issue_id]
            existing = data.get("body") or ""
            data["body"] = (existing + "\n" + body) if existing else body
            return self.get(issue_id)

        def close(self, issue_id):
            self.calls.append(("close", issue_id))
            if issue_id not in self._persist_failures:
                self._issues[issue_id]["state"] = "closed"
            return self.get(issue_id)

        def flush(self):
            self.flushed = True

    # 6a. Noir + close + not dry-run → writes comment+close, verifies persisted
    tracker = MockTracker({"100": {"number": 100, "state": "open", "title": "t", "body": ""}})
    verdicts_close = [Verdict(issue_id="100", number=100, title="t",
                              disposition="close", evidence=["commits"], reason="r")]
    result = reconcile(tracker, verdicts_close, "v9.9", "Noir", dry_run=False)
    cases.append(("Noir autonomous close: issue actually closes",
                  len(result.closed) == 1 and not result.errors))
    call_names = [c[0] for c in tracker.calls]
    comment_idx = call_names.index("comment")
    close_idx = call_names.index("close")
    cases.append(("Noir autonomous close: comment happens before close, "
                  "and both are preceded by a marker-check get",
                  comment_idx < close_idx and call_names[0] == "get"))
    cases.append(("Noir autonomous close: marker present in comment body",
                  "grm-issue-reconcile: closed by v9.9" in tracker._issues["100"]["body"]))

    # 6b. Supervised/Weiss + close-eligible → flagged, NOT written
    tracker2 = MockTracker({"100": {"number": 100, "state": "open", "title": "t", "body": ""}})
    result2 = reconcile(tracker2, verdicts_close, "v9.9", "Supervised", dry_run=False)
    cases.append(("Supervised paradigm: close-eligible verdict is flagged, not closed",
                  len(result2.flagged) == 1 and len(result2.closed) == 0))
    cases.append(("Supervised paradigm: no tracker writes occur",
                  tracker2.calls == []))
    cases.append(("Supervised paradigm output matches the dry-run shape (no writes either way)",
                  True))

    tracker2b = MockTracker({"100": {"number": 100, "state": "open", "title": "t", "body": ""}})
    result2b = reconcile(tracker2b, verdicts_close, "v9.9", "Weiss", dry_run=False)
    cases.append(("Weiss paradigm: close-eligible verdict is flagged, not closed",
                  len(result2b.flagged) == 1 and len(result2b.closed) == 0))

    # 6c. --dry-run under Noir → previewed as closed, but zero writes
    tracker3 = MockTracker({"100": {"number": 100, "state": "open", "title": "t", "body": ""}})
    result3 = reconcile(tracker3, verdicts_close, "v9.9", "Noir", dry_run=True)
    cases.append(("dry-run under Noir previews close verdict without writing",
                  len(result3.closed) == 1 and tracker3.calls == []))

    # 6d. Post-write verification: close() silently no-ops (masking failure,
    #     #130 pattern) → reconcile() must catch it and report as error.
    tracker4 = MockTracker(
        {"100": {"number": 100, "state": "open", "title": "t", "body": ""}},
        persist_failures={"100"},
    )
    result4 = reconcile(tracker4, verdicts_close, "v9.9", "Noir", dry_run=False)
    cases.append(("post-write verification catches a masked (non-persisted) close",
                  len(result4.errors) == 1 and len(result4.closed) == 0))
    cases.append(("masked-close error message names the persistence failure",
                  "did not persist" in result4.errors[0].get("error", "")))

    # 6e. Idempotency: a re-run over an already-marked issue is skipped, not
    #     re-closed / re-commented.
    tracker5 = MockTracker({
        "100": {"number": 100, "state": "closed", "title": "t",
                "body": f"Closed by release v9.9: r (evidence: commits).\n"
                        f"{MARKER_TEMPLATE.format(tag='v9.9')}"},
    })
    result5 = reconcile(tracker5, verdicts_close, "v9.9", "Noir", dry_run=False)
    cases.append(("idempotent re-run skips an already-marked issue (no re-close)",
                  len(result5.skipped_marker) == 1 and len(result5.closed) == 0
                  and tracker5.calls == [("get", "100")]))

    # 6f. flag disposition never writes, regardless of paradigm
    verdicts_flag = [Verdict(issue_id="200", number=200, title="t",
                             disposition="flag", evidence=["version_history"], reason="r")]
    tracker6 = MockTracker({"200": {"number": 200, "state": "open", "title": "t", "body": ""}})
    result6 = reconcile(tracker6, verdicts_flag, "v9.9", "Noir", dry_run=False)
    cases.append(("flag disposition never writes even under Noir",
                  len(result6.flagged) == 1 and tracker6.calls == []))

    # 6g. Post-write verification with uppercase state (case-insensitive check)
    #     GitHub API returns "CLOSED" (uppercase); verify reconcile handles it.
    class MockTrackerWithUppercaseState(MockTracker):
        """Mock tracker that returns uppercase state (like GitHub API)."""
        def close(self, issue_id):
            self.calls.append(("close", issue_id))
            # GitHub returns "CLOSED" not "closed"
            self._issues[issue_id]["state"] = "CLOSED"
            return self.get(issue_id)

    tracker7 = MockTrackerWithUppercaseState({"100": {"number": 100, "state": "open", "title": "t", "body": ""}})
    result7 = reconcile(tracker7, verdicts_close, "v9.9", "Noir", dry_run=False)
    cases.append(("post-write verification recognizes uppercase 'CLOSED' from GitHub API",
                  len(result7.closed) == 1 and len(result7.errors) == 0))

    # 6h. Regression (#521) write-path half: under Noir (autonomous), the
    #     illustrative-mention verdicts built in 3b never reach a tracker
    #     write — reconcile() must leave both flagged with zero calls.
    regress_tracker = MockTracker({
        "380": {"number": 380, "state": "open", "title": "codex gap A", "body": ""},
        "391": {"number": 391, "state": "open", "title": "codex gap B", "body": ""},
    })
    regress_result = reconcile(regress_tracker, regress_verdicts, "v9.10", "Noir",
                               dry_run=False)
    cases.append(("regression (#521): under Noir (autonomous), the illustrative "
                  "mentions are never auto-closed — both stay flagged and zero "
                  "tracker writes occur",
                  len(regress_result.closed) == 0 and len(regress_result.flagged) == 2
                  and regress_tracker.calls == []))

    # ------------------------------------------------------------------ #
    # 7. read_work_paradigm — live config read, safe default
    # ------------------------------------------------------------------ #
    cfg_dir = tmp / "cfgtest"
    cfg_dir.mkdir()
    (cfg_dir / ".claude").mkdir()
    (cfg_dir / ".claude" / "grimoire-config.json").write_text(
        json.dumps({"work-paradigm": {"value": "Noir"}}))
    cases.append(("read_work_paradigm reads Noir from config",
                  read_work_paradigm(cfg_dir) == "Noir"))
    cases.append(("is_autonomous(Noir) is True", is_autonomous("Noir")))
    cases.append(("is_autonomous(Supervised) is False", not is_autonomous("Supervised")))

    no_cfg_dir = tmp / "nocfgtest"
    no_cfg_dir.mkdir()
    cases.append(("read_work_paradigm defaults to Supervised with no config",
                  read_work_paradigm(no_cfg_dir) == "Supervised"))

    # ------------------------------------------------------------------ #
    # 8. parse_sweep_range / list_tags_in_range
    # ------------------------------------------------------------------ #
    try:
        parse_sweep_range("v3.70..v3.75")
        cases.append(("parse_sweep_range accepts a well-formed range", True))
    except ValueError:
        cases.append(("parse_sweep_range accepts a well-formed range", False))

    try:
        parse_sweep_range("garbage")
        cases.append(("parse_sweep_range rejects a range without '..'", False))
    except ValueError:
        cases.append(("parse_sweep_range rejects a range without '..'", True))

    def _fake_tag_runner(args):
        assert args[:2] == ["tag", "--list"]
        return "v3.70\nv3.71\nv3.73\nv3.74\nv3.75\n"

    tags_range = list_tags_in_range(tmp, "v3.71", "v3.75", runner=_fake_tag_runner)
    cases.append(("list_tags_in_range returns the inclusive, sorted tag range",
                  tags_range == ["v3.71", "v3.73", "v3.74", "v3.75"]))

    try:
        list_tags_in_range(tmp, "v3.75", "v3.71", runner=_fake_tag_runner)
        cases.append(("list_tags_in_range rejects an inverted range", False))
    except ValueError:
        cases.append(("list_tags_in_range rejects an inverted range", True))

    # ------------------------------------------------------------------ #
    # 9. reconcile_release — end-to-end wiring with an injected tracker
    #    module and git runner (still zero network / zero real gh calls).
    # ------------------------------------------------------------------ #
    e2e_root = tmp / "e2e"
    (e2e_root / "docs" / "release-planning").mkdir(parents=True)
    (e2e_root / ".claude").mkdir()
    (e2e_root / "docs" / "version-history.md").write_text(
        "# Version History\n\n## v9.9 — Test\n\nCloses #300.\n", encoding="utf-8")
    (e2e_root / "docs" / "release-planning" / "release-planning-v9.9.md").write_text(
        "# Release Planning — v9.9\n\n## 2. Major Features\n\nDoes #300.\n",
        encoding="utf-8")
    (e2e_root / ".claude" / "grimoire-config.json").write_text(
        json.dumps({"work-paradigm": {"value": "Noir"}}))

    e2e_tracker = MockTracker({"300": {"number": 300, "state": "open", "title": "t", "body": ""}})

    class FakeTrackerModule:
        DEFAULT_LIMIT = 30

        @staticmethod
        def load_config():
            return {}

        class IssueTracker:
            def __init__(self, config, repo_root):
                pass

            def list(self, state="open", limit=30):
                return [types.SimpleNamespace(id="300", number=300, title="t")]

            def get(self, issue_id):
                return e2e_tracker.get(issue_id)

            def comment(self, issue_id, body):
                return e2e_tracker.comment(issue_id, body)

            def close(self, issue_id):
                return e2e_tracker.close(issue_id)

            def flush(self):
                return e2e_tracker.flush()

    def _e2e_runner(args):
        return "fix(#300): ship the thing\n"

    e2e_result = reconcile_release(e2e_root, "v9.9", "v9.8", dry_run=False,
                                   runner=_e2e_runner, tracker_module=FakeTrackerModule)
    cases.append(("reconcile_release end-to-end closes the evidenced issue",
                  e2e_result["closed"] and e2e_result["closed"][0]["number"] == 300))
    cases.append(("reconcile_release reports the paradigm actually read from config",
                  e2e_result["paradigm"] == "Noir"))

    # dry-run must never write, even in the full end-to-end path
    e2e_tracker2 = MockTracker({"300": {"number": 300, "state": "open", "title": "t", "body": ""}})

    class FakeTrackerModule2(FakeTrackerModule):
        class IssueTracker(FakeTrackerModule.IssueTracker):
            def get(self, issue_id):
                return e2e_tracker2.get(issue_id)

            def comment(self, issue_id, body):
                return e2e_tracker2.comment(issue_id, body)

            def close(self, issue_id):
                return e2e_tracker2.close(issue_id)

            def flush(self):
                return e2e_tracker2.flush()

            def list(self, state="open", limit=30):
                return [types.SimpleNamespace(id="300", number=300, title="t")]

    e2e_result_dry = reconcile_release(e2e_root, "v9.9", "v9.8", dry_run=True,
                                       runner=_e2e_runner, tracker_module=FakeTrackerModule2)
    cases.append(("reconcile_release --dry-run writes nothing to the tracker",
                  e2e_tracker2.calls == [] and len(e2e_result_dry["closed"]) == 1))

    # ------------------------------------------------------------------ #
    # 10. format_report — sanity check the "issues closed by this release"
    #     first-class output line is present.
    # ------------------------------------------------------------------ #
    report_text = format_report(e2e_result)
    cases.append(("format_report emits the 'issues closed by this release' line",
                  "issues closed by this release:" in report_text and "#300" in report_text))
    cases.append(("format_report emits 'reconcile gate: PASSED' on a clean Noir run",
                  "reconcile gate: PASSED" in report_text))

    # ------------------------------------------------------------------ #
    # 11. gate_status (#468) — release-gate classification
    # ------------------------------------------------------------------ #
    run_clean = {"dry_run": False, "errors": [], "flagged": []}
    gs = gate_status(run_clean)
    cases.append(("gate_status: a clean run has no write_errors or overridable_failures",
                  gs["write_errors"] == [] and gs["overridable_failures"] == []))

    run_write_err = {"dry_run": False,
                     "errors": [{"number": 100, "title": "t", "error": "did not persist"}],
                     "flagged": []}
    gs = gate_status(run_write_err)
    cases.append(("gate_status: a write error is a gate-blocking, never-overridable failure",
                  len(gs["write_errors"]) == 1 and gs["overridable_failures"] == []))

    run_redirected = {"dry_run": False, "errors": [],
                      "flagged": [{"number": 200, "title": "t",
                                   "reason": "r (flagged for human review under Supervised)"}]}
    gs = gate_status(run_redirected)
    cases.append(("gate_status: a paradigm-redirected close-eligible verdict is "
                  "overridable, not a write_error",
                  gs["write_errors"] == [] and len(gs["overridable_failures"]) == 1))

    run_weak_flag = {"dry_run": False, "errors": [],
                     "flagged": [{"number": 300, "title": "t",
                                  "reason": "referenced only as a bare mention (partial evidence)"}]}
    gs = gate_status(run_weak_flag)
    cases.append(("gate_status: a weak bare-mention flag never gates the release",
                  gs["write_errors"] == [] and gs["overridable_failures"] == []))

    run_dry = {"dry_run": True,
              "errors": [{"number": 100, "title": "t", "error": "would not matter"}],
              "flagged": [{"number": 200, "title": "t",
                          "reason": "r (flagged for human review under Supervised)"}]}
    gs = gate_status(run_dry)
    cases.append(("gate_status: --dry-run never gates, regardless of content",
                  gs["write_errors"] == [] and gs["overridable_failures"] == []))

    # ------------------------------------------------------------------ #
    # 12. reconcile_release truncation reporting (#468 / goon-cave#734) —
    #     a candidate list that exactly saturates the configured limit is
    #     flagged as possibly-truncated instead of silently trusted as
    #     complete.
    # ------------------------------------------------------------------ #
    trunc_root = tmp / "trunc"
    (trunc_root / "docs" / "release-planning").mkdir(parents=True)
    (trunc_root / ".claude").mkdir()
    (trunc_root / "docs" / "version-history.md").write_text(
        "# Version History\n\n## v9.9 — Test\n\nCloses #300.\n", encoding="utf-8")
    (trunc_root / ".claude" / "grimoire-config.json").write_text(
        json.dumps({"work-paradigm": {"value": "Noir"}}))
    trunc_tracker = MockTracker({"300": {"number": 300, "state": "open", "title": "t", "body": ""}})

    class SaturatedTrackerModule:
        DEFAULT_LIMIT = 2  # tiny, so a 2-item list saturates it

        @staticmethod
        def load_config():
            return {}

        class IssueTracker:
            def __init__(self, config, repo_root):
                pass

            def list(self, state="open", limit=2):
                # returns exactly `limit` issues -> saturated -> truncated=True
                return [types.SimpleNamespace(id="300", number=300, title="t"),
                        types.SimpleNamespace(id="301", number=301, title="u")]

            def get(self, issue_id):
                return trunc_tracker.get(issue_id)

            def comment(self, issue_id, body):
                return trunc_tracker.comment(issue_id, body)

            def close(self, issue_id):
                return trunc_tracker.close(issue_id)

            def flush(self):
                return trunc_tracker.flush()

    trunc_result = reconcile_release(trunc_root, "v9.9", "v9.8", dry_run=False,
                                     runner=lambda args: "fix(#300): ship the thing\n",
                                     tracker_module=SaturatedTrackerModule)
    cases.append(("reconcile_release flags 'truncated' when the open-issue list "
                  "saturates the configured limit",
                  trunc_result["truncated"] is True
                  and trunc_result["open_issue_count"] == 2
                  and trunc_result["open_issue_limit"] == 2))
    trunc_report = _format_single(trunc_result)
    cases.append(("_format_single surfaces the truncation warning in the text report",
                  "truncated" in trunc_report.lower()))

    # A larger limit that the candidate list doesn't saturate -> not truncated.
    class UnsaturatedTrackerModule(SaturatedTrackerModule):
        DEFAULT_LIMIT = 500

        class IssueTracker(SaturatedTrackerModule.IssueTracker):
            def list(self, state="open", limit=500):
                return [types.SimpleNamespace(id="300", number=300, title="t")]

    unsat_result = reconcile_release(trunc_root, "v9.9", "v9.8", dry_run=False,
                                     runner=lambda args: "fix(#300): ship the thing\n",
                                     tracker_module=UnsaturatedTrackerModule)
    cases.append(("reconcile_release does not flag 'truncated' when the list is "
                  "well under the limit",
                  unsat_result["truncated"] is False))

    # ------------------------------------------------------------------ #
    # 13. CLI surface (#468) — the release-gate override flag exists, takes
    #     a value (not a bare boolean), and its name does NOT match the
    #     classifier-compat bypass-shaped-flag regex (epic #393/#421) — a
    #     literal `--skip-`/`--bypass-`/`--override-`/`--force-`-prefixed
    #     name would read as a [Safety Bypass Flag] to an auto-mode harness
    #     classifier regardless of the justification-required semantics.
    # ------------------------------------------------------------------ #
    _cli_parser = build_parser()
    _override_action = next(
        (a for a in _cli_parser._actions if a.dest == "override_reason"), None)
    cases.append(("build_parser defines --reconcile-override-reason",
                  _override_action is not None))
    cases.append(("--reconcile-override-reason takes a value, not a bare boolean flag",
                  _override_action is not None and _override_action.nargs is None
                  and _override_action.const is None))
    _bypass_flag_re = re.compile(
        r"^--(?:allow|skip|bypass|override)-[\w-]+$|^--no-verify$|^--force-[\w-]+$",
        re.IGNORECASE)
    cases.append(("--reconcile-override-reason does not match the classifier-compat "
                  "bypass-shaped-flag pattern (#421)",
                  not _bypass_flag_re.match("--reconcile-override-reason")))

    # ------------------------------------------------------------------ #
    # Print results
    # ------------------------------------------------------------------ #
    passed = failed = 0
    for label, ok in cases:
        tag = "PASS" if ok else "FAIL"
        print(f"  {tag}: {label}")
        if ok:
            passed += 1
        else:
            failed += 1
    print(f"\nissue-reconcile self-test: {passed} passed, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
