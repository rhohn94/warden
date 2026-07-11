#!/usr/bin/env python3
"""release_plan.py — deterministic engine for the §5 release-planning ledger (#MCP-1, v3.27).

Backs the `grimoire-release` MCP server (the v3.12 template's second instance)
and five consumer skills (release-agent-tracker, ledger-tick, release-phase,
release-phase-merge, noir-loop). Hand-parsing and hand-editing the §5 markdown
ledger in `docs/release-planning-v{X.Y}.md` is the hottest mechanical loop in
the framework; this engine replaces the prose-guided Bash + free-form stdout
with one tested, token-cheap, deterministic implementation.

Capabilities (each also a CLI verb + an MCP tool on server.py):
  - locate the active plan (highest release-planning-v*.md with status: agreed)
  - parse the §5 ledger tables to JSON (passes, rows, checkbox tri-state,
    branch names, item ids)
  - diff the ledger vs `git branch` reality
  - compute the merge queue from §3's conflict map + mergeAfter (toposort)
  - run a merge preflight (HEAD == staging ref; branch-exists + commits-ahead)
    emitting a structured verdict, including the before-promotion divergence gate
  - run a model-aware divergence guard before promotion (BMI-2, v3.38, #126):
    tree-content reachability — HALT iff `main` carries tree content not
    reachable from the integration line; benign promotion merges do NOT HALT
  - tick rows atomically + idempotently (file edit only)
  - plan a phase (first all-unticked pass -> batches + model assignments)
  - detect-merged (maintenance-automation-design.md item 2, v3.58): diff the
    ledger's un-ticked rows against `git log --merges` on the integration line
    and PROPOSE (never apply) branch-name-substring matches + merge SHAs for
    confirmation via the existing `tick` / grm-ledger-tick path.

File-write-only contract: this engine NEVER runs git mutations. Every git call
is a read; the only side effect is editing the §5 ledger file via `tick`. The
AGENT commits. Design: docs/grimoire/design/grimoire-release-server-design.md.
`detect-merged` is READ-ONLY even of the ledger file — it only proposes ticks;
the human/agent applies them via `tick` (or the grm-ledger-tick skill).

Standard: Python 3 stdlib-only (docs/grimoire/design/scripting-unification-design.md).

CLI:
  release_plan.py locate [--root DIR]
  release_plan.py get-ledger [--plan FILE] [--root DIR]
  release_plan.py diff [--plan FILE] [--root DIR]
  release_plan.py merge-queue [--phase NAME] [--plan FILE] [--root DIR]
  release_plan.py merge-preflight --staging REF [--branch B ...] [--plan FILE] [--root DIR]
  release_plan.py divergence-check [--integration BR] [--published BR] [--root DIR]
  release_plan.py plan-phase [--plan FILE] [--root DIR]
  release_plan.py tick --branch B --column COL --value (true|false) [...] [--plan FILE] [--root DIR]
  release_plan.py detect-merged [--staging REF] [--json] [--plan FILE] [--root DIR]
  release_plan.py --self-test
Exit 0 on success; 2 on bad input / plan error.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

# ── Constants (no magic numbers inline) ─────────────────────────────────────
# v3.45 "Release-planning relocation": the active plan lives at the root of the
# dedicated docs/release-planning/ tier. The pre-v3.45 top-level path is kept as a
# backward-compat glob so a synced-but-not-yet-migrated tree still resolves. The
# tier-root glob does NOT recurse into archived/ (glob '*' never crosses '/').
PLAN_GLOBS = [
    os.path.join("docs", "release-planning", "release-planning-v*.md"),  # v3.45 tier (active)
    os.path.join("docs", "release-planning-v*.md"),                      # pre-v3.45 (backward-compat)
]
PLAN_VERSION_RE = re.compile(r"release-planning-v([0-9]+(?:\.[0-9]+)*)\.md$")
AGREED_RE = re.compile(r"^>?\s*status:\s*agreed\b", re.IGNORECASE | re.MULTILINE)
# Ledger checkbox glyphs. ☑ = done, ☐ = outstanding; "n/a" = not-applicable.
CHECK_DONE = "☑"
CHECK_OPEN = "☐"
NA_TOKENS = ("n/a", "na", "—", "-")
# §5 columns in declared order; the first is the branch, the rest are checkboxes.
CHECK_COLUMNS = ("design_doc", "implemented", "reviewed", "merged")
# Conflict-map dependency marker — "A ↔ B: … → sequenced".
SEQUENCED_TOKEN = "sequenced"
JSON_COMPACT = (",", ":")

# ── Divergence guard (BMI-2, v3.38, #126) ───────────────────────────────────
# The published line every promotion targets in both branch models.
PUBLISHED_LINE = "main"
# Integration-line detection: the config key BMI-3 (sync skills) reads with the
# same default, so the two lanes resolve the integration line identically.
# `branch-model.integration-branch` ⇒ the integration line; absent ⇒ DEFAULT.
CONFIG_REL_PATH = os.path.join(".claude", "grimoire-config.json")
INTEGRATION_BRANCH_KEY = ("branch-model", "integration-branch")
DEFAULT_INTEGRATION_LINE = "dev"

# Phase-model band table (mirrors the release-phase SKILL.md §3 table). Used by
# plan_phase when the plan does not carry per-item token estimates inline; the
# band is a coarse default the dispatcher refines.
DEFAULT_MODEL_TABLE = {
    "trivial": {"model": "haiku", "effort": "low"},
    "small": {"model": "sonnet", "effort": "inherit"},
    "medium": {"model": "sonnet", "effort": "inherit"},
    "large": {"model": "opus", "effort": "high"},
    "review": {"model": "opus", "effort": "high"},
}


class PlanError(Exception):
    """Raised on a missing/ambiguous plan or a malformed ledger (→ exit 2)."""


# ── Plan location ───────────────────────────────────────────────────────────
class PlanLocator:
    """Locate the active release plan: highest version with status: agreed."""

    def __init__(self, root="."):
        self.root = root

    @staticmethod
    def _version_key(path):
        m = PLAN_VERSION_RE.search(os.path.basename(path))
        if not m:
            return ()
        return tuple(int(p) for p in m.group(1).split("."))

    def candidates(self):
        found = []
        for g in PLAN_GLOBS:
            found.extend(glob.glob(os.path.join(self.root, g)))
        # De-dup (a path could match more than one glob in pathological trees).
        return sorted(set(found), key=self._version_key)

    @staticmethod
    def _is_agreed(path):
        try:
            with open(path, encoding="utf-8") as fh:
                # The status line lives in the leading metadata block.
                head = fh.read(2048)
        except OSError:
            return False
        return bool(AGREED_RE.search(head))

    def locate(self):
        """Return the path to the active agreed plan, highest version wins."""
        agreed = [p for p in self.candidates() if self._is_agreed(p)]
        if not agreed:
            raise PlanError(
                "no release-planning-v*.md with 'status: agreed' under %s/docs"
                % self.root)
        # candidates() is version-sorted ascending; the last agreed is highest.
        return agreed[-1]


# ── Ledger model ────────────────────────────────────────────────────────────
class LedgerRow:
    """One §5 ledger row: a branch + its four tri-state checkbox cells."""

    def __init__(self, pass_name, branch, item_id, cells):
        self.pass_name = pass_name
        self.branch = branch
        self.item_id = item_id
        # cells: dict column -> True (done) / False (open) / None (n/a).
        self.cells = cells

    @property
    def implemented(self):
        return self.cells.get("implemented")

    @property
    def merged(self):
        return self.cells.get("merged")

    def to_dict(self):
        out = {"branch": self.branch, "pass": self.pass_name}
        if self.item_id:
            out["item"] = self.item_id
        out.update({c: self.cells.get(c) for c in CHECK_COLUMNS})
        return out


class Ledger:
    """Parse §5 of a plan file into passes -> [LedgerRow]; also reads §3.

    The §5 ledger is a set of `### Pass N` sections each holding a markdown
    table; column 1 is the branch (with an item id in parentheses), the next
    four are the checkbox cells. §3 carries the conflict map (mergeAfter edges)
    that the merge queue and phase planner consume.
    """

    PASS_HEADING_RE = re.compile(r"^#{2,4}\s+(Pass\s+[^\n]+?)\s*$",
                                 re.IGNORECASE)
    SECTION5_RE = re.compile(r"^#{1,3}\s+5\.\s", re.MULTILINE)
    SECTION3_RE = re.compile(r"^#{1,3}\s+3\.\s", re.MULTILINE)
    # A row in parentheses item id: `(MCP-1)`.
    ITEM_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9-]*-?\d*)\)")
    BRANCH_RE = re.compile(r"`([^`]+)`")
    # §3 conflict-map line: "- A ↔ B: … → sequenced." (A,B are item ids or files)
    CONFLICT_RE = re.compile(
        r"^\s*[-*]\s*(.+?)\s*[↔]\s*(.+?)\s*:.*", re.UNICODE)

    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as fh:
            self.text = fh.read()
        self.version = self._parse_version(path)
        self.passes = self._parse_passes()
        self.conflict_map = self._parse_conflict_map()

    @staticmethod
    def _parse_version(path):
        m = PLAN_VERSION_RE.search(os.path.basename(path))
        return m.group(1) if m else ""

    # -- §5 ledger --
    def _section5_text(self):
        m = self.SECTION5_RE.search(self.text)
        if not m:
            raise PlanError("plan %s has no §5 ledger section" % self.path)
        start = m.start()
        # §5 runs to the next top-level `## ` heading, or EOF.
        rest = self.text[m.end():]
        nxt = re.search(r"^##\s+\d+\.\s", rest, re.MULTILINE)
        end = m.end() + nxt.start() if nxt else len(self.text)
        return self.text[start:end]

    @classmethod
    def _parse_cell(cls, raw):
        token = raw.strip()
        if token == CHECK_DONE:
            return True
        if token == CHECK_OPEN:
            return False
        if token.lower() in NA_TOKENS:
            return None
        # Defensive: a cell carrying a glyph plus a note.
        if CHECK_DONE in token:
            return True
        if CHECK_OPEN in token:
            return False
        return None

    def _parse_passes(self):
        text = self._section5_text()
        passes = {}
        current = None
        for line in text.splitlines():
            heading = self.PASS_HEADING_RE.match(line)
            if heading:
                current = heading.group(1).strip()
                passes.setdefault(current, [])
                continue
            if current is None or not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells or cells[0].lower() in ("branch", ""):
                continue  # header row
            if set("".join(cells)) <= set("-: "):
                continue  # separator row
            branch_m = self.BRANCH_RE.search(cells[0])
            if not branch_m:
                continue
            branch = branch_m.group(1).strip()
            item_m = self.ITEM_RE.search(cells[0])
            item_id = item_m.group(1) if item_m else ""
            check_cells = cells[1:5] + [""] * (5 - len(cells))
            parsed = {col: self._parse_cell(check_cells[i])
                      for i, col in enumerate(CHECK_COLUMNS)}
            passes[current].append(LedgerRow(current, branch, item_id, parsed))
        if not passes:
            raise PlanError("plan %s §5 has no parseable pass rows" % self.path)
        return passes

    # -- §3 conflict map --
    def _section3_text(self):
        m = self.SECTION3_RE.search(self.text)
        if not m:
            return ""
        rest = self.text[m.end():]
        nxt = re.search(r"^##\s+\d+\.\s", rest, re.MULTILINE)
        end = m.end() + nxt.start() if nxt else len(self.text)
        return self.text[m.start():end]

    def _branch_for_item(self, token):
        """Resolve a conflict-map token (item id or branch) to a branch name."""
        token = token.strip().strip("`*")
        for rows in self.passes.values():
            for row in rows:
                if token in (row.item_id, row.branch):
                    return row.branch
        return None

    def _parse_conflict_map(self):
        """Return {branch: set(branches it must merge AFTER)} from §3.

        A sequenced `A ↔ B` pair means the later-declared item merges after the
        earlier one. We orient by ledger position: whichever branch appears
        later across the passes depends on the earlier.
        """
        order = self._branch_order()
        edges = {b: set() for b in order}
        for line in self._section3_text().splitlines():
            m = self.CONFLICT_RE.match(line)
            if not m or SEQUENCED_TOKEN not in line.lower():
                continue
            a = self._branch_for_item(m.group(1))
            b = self._branch_for_item(m.group(2))
            if not a or not b or a == b:
                continue
            first, second = self._orient(a, b, order)
            edges.setdefault(second, set()).add(first)
            edges.setdefault(first, set())
        return edges

    def _branch_order(self):
        order = []
        for rows in self.passes.values():
            for row in rows:
                if row.branch not in order:
                    order.append(row.branch)
        return order

    @staticmethod
    def _orient(a, b, order):
        ia = order.index(a) if a in order else 1 << 30
        ib = order.index(b) if b in order else 1 << 30
        return (a, b) if ia <= ib else (b, a)

    def all_rows(self):
        for rows in self.passes.values():
            for row in rows:
                yield row

    def to_dict(self):
        return {
            "version": self.version,
            "plan": self.path,
            "passes": {name: [r.to_dict() for r in rows]
                       for name, rows in self.passes.items()},
        }


# ── Git read-only view ──────────────────────────────────────────────────────
class GitView:
    """Read-only git facade. NEVER mutates a ref (file-write-only contract).

    The runner is injectable so the self-test exercises every path without
    shelling out to a real repository.
    """

    def __init__(self, root=".", runner=None):
        self.root = root
        self._runner = runner or self._default_runner

    def _default_runner(self, args):
        try:
            out = subprocess.run(["git", "-C", self.root, *args],
                                 capture_output=True, text=True, check=False)
        except OSError as exc:
            raise PlanError("git unavailable: %s" % exc)
        return out.returncode, out.stdout, out.stderr

    def local_branches(self):
        code, out, _ = self._runner(["branch", "--format=%(refname:short)"])
        if code != 0:
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def head_ref(self):
        code, out, _ = self._runner(["symbolic-ref", "--short", "HEAD"])
        return out.strip() if code == 0 else ""

    def branch_exists(self, branch):
        code, _, _ = self._runner(["rev-parse", "--verify", "--quiet", branch])
        return code == 0

    def commits_ahead(self, staging, branch):
        """How many commits `branch` carries beyond `staging` (0 if none/err)."""
        code, out, _ = self._runner(
            ["rev-list", "--count", "%s..%s" % (staging, branch)])
        if code != 0:
            return 0
        try:
            return int(out.strip() or "0")
        except ValueError:
            return 0

    # -- divergence-guard reads (BMI-2) --
    def trees_differ(self, ref_a, ref_b):
        """True iff the working trees of two refs differ.

        Mirrors `git diff --quiet ref_a ref_b`: exit 0 = identical (False),
        exit 1 = differ (True). Any other exit (a bad ref) is treated as
        'differ' so the guard fails safe (it then enumerates + classifies).
        """
        code, _, _ = self._runner(["diff", "--quiet", ref_a, ref_b])
        return code != 0

    def log_oneline(self, base, tip):
        """`git log --oneline base..tip` → [(sha, subject)] (newest first)."""
        code, out, _ = self._runner(
            ["log", "--oneline", "--no-decorate", "%s..%s" % (base, tip)])
        if code != 0:
            return []
        rows = []
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            sha, _, subject = ln.partition(" ")
            rows.append((sha, subject.strip()))
        return rows

    def merges_oneline(self, branch):
        """`git log --merges --oneline branch` -> [(sha, subject)] (newest first).

        Used by MergeDetector (--detect-merged) to enumerate merge commits on
        the integration/staging line for branch-name-substring matching against
        un-ticked ledger rows. A read-only, best-effort call: any git failure
        (unknown ref, no repo) yields an empty list rather than raising, since
        detect-merged degrades gracefully to "no matches found".
        """
        code, out, _ = self._runner(
            ["log", "--merges", "--oneline", "--no-decorate", branch])
        if code != 0:
            return []
        rows = []
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            sha, _, subject = ln.partition(" ")
            rows.append((sha, subject.strip()))
        return rows

    def commit_introduces_work_absent_from(self, commit, base):
        """True iff `commit`'s patch is NOT already reachable from `base`.

        This is the per-commit divergence test: a commit on the published line
        is *benign* (a promotion merge carrying no new tree content) when every
        change it introduces is already present on the integration line, and is
        *real work* otherwise.

        Implemented with `git cherry base commit`: git prints `+ <sha>` for a
        commit whose patch has no equivalent on `base`, and `- <sha>` for one
        already represented there. `git cherry` reduces a merge to its first
        parent, so a promotion merge (whose net diff vs the integration tip is
        empty) is reported as `-` (benign). A non-empty `+` line ⇒ real work.
        """
        code, out, _ = self._runner(["cherry", base, commit])
        if code != 0:
            # Fall back to a direct tree comparison: does this commit's tree
            # differ from base in a way base cannot explain? Treat any error as
            # 'introduces work' so the guard fails safe (HALT rather than miss).
            return True
        for ln in out.splitlines():
            ln = ln.strip()
            if ln.startswith("+ "):
                return True
        return False


# ── Ledger-vs-git diff ──────────────────────────────────────────────────────
class LedgerDiff:
    """Classify each ledger row against live git state."""

    def __init__(self, ledger, git):
        self.ledger = ledger
        self.git = git

    @staticmethod
    def _classify(row, exists):
        impl, merged = row.implemented, row.merged
        if merged:
            return "merged"
        if impl and exists:
            return "ready"          # ☑ implemented, branch present, not merged
        if impl and not exists:
            return "implemented-missing-branch"  # drift: ticked but no branch
        if not impl and exists:
            return "in-progress"
        return "not-started"

    def rows(self):
        branches = set(self.git.local_branches())
        out = []
        for row in self.ledger.all_rows():
            exists = row.branch in branches
            out.append({"branch": row.branch, "item": row.item_id,
                        "exists": exists, "state": self._classify(row, exists)})
        return out


# ── Merge queue (toposort over §3 conflict map) ─────────────────────────────
class MergeQueue:
    """Compute the merge order for ready rows, honouring §3 mergeAfter edges."""

    def __init__(self, ledger):
        self.ledger = ledger

    def _ready_branches(self, phase=None):
        ready = []
        for name, rows in self.ledger.passes.items():
            if phase and phase.lower() not in name.lower():
                continue
            for row in rows:
                if row.implemented and not row.merged:
                    ready.append(row.branch)
        return ready

    def compute(self, phase=None):
        ready = self._ready_branches(phase)
        ready_set = set(ready)
        edges = self.ledger.conflict_map
        order, blocked = [], []
        done = set()
        # Stable deterministic toposort: preserve ledger order among independents.
        remaining = list(ready)
        progress = True
        while remaining and progress:
            progress = False
            still = []
            for branch in remaining:
                deps = {d for d in edges.get(branch, set()) if d in ready_set}
                if deps <= done:
                    order.append(branch)
                    done.add(branch)
                    progress = True
                else:
                    still.append(branch)
            remaining = still
        # Anything left has an unsatisfiable (cyclic) dependency in-set.
        blocked = list(remaining)
        return {"order": order, "blocked": blocked}


# ── Merged-branch auto-detection (maintenance-automation-design.md item 2) ──
class MergeDetector:
    """Propose §5 ledger flips by matching un-ticked rows to merge commits.

    Diffs the ledger's un-ticked (`merged` column == False) rows against
    `git log --merges --oneline <staging>` on the integration/staging branch
    (whichever the caller resolves — see IntegrationLineResolver / the plan's
    own staging ref). A row matches a merge commit when the row's branch name
    appears as a **substring** of the merge commit's subject line (the subject
    of a `git merge` commit is typically "Merge branch '<name>' into <target>"
    or the project's own "merge(...): <branch> ..." convention).

    This is explicitly a HEURISTIC, not an infallible match:
      - a short/generic branch name can substring-match an unrelated subject;
      - a squash-merge (no merge commit) never surfaces here;
      - a branch renamed between ledger-authoring and merge time is missed.
    For this reason MergeDetector only PROPOSES — it never calls Ticker itself.
    The proposal is a report (stdout / --json) a human or agent reviews before
    applying via `release_plan.py tick` (or the grm-ledger-tick skill), mirroring
    the confirm-then-apply pattern release-planning already uses elsewhere
    (e.g. the Step 4 destructive-op gate in grm-hard-reset).
    """

    def __init__(self, ledger, git):
        self.ledger = ledger
        self.git = git

    def _unticked_rows(self):
        return [row for row in self.ledger.all_rows() if row.merged is False]

    def detect(self, staging):
        """Return {"staging": ref, "proposals": [...], "unmatched": [...]}.

        Each proposal: {branch, item, pass, sha, subject}. `unmatched` lists
        un-ticked rows (branch/item/pass) with no matching merge commit found —
        still open, nothing to propose.
        """
        merges = self.git.merges_oneline(staging)
        proposals, unmatched = [], []
        for row in self._unticked_rows():
            match = self._match(row.branch, merges)
            if match:
                sha, subject = match
                proposals.append({
                    "branch": row.branch, "item": row.item_id,
                    "pass": row.pass_name, "sha": sha, "subject": subject,
                })
            else:
                unmatched.append({
                    "branch": row.branch, "item": row.item_id,
                    "pass": row.pass_name,
                })
        return {"staging": staging, "proposals": proposals,
                "unmatched": unmatched}

    @staticmethod
    def _match(branch, merges):
        """First merge commit (newest-first order) whose subject contains
        `branch` as a substring; None if no merge commit matches."""
        for sha, subject in merges:
            if branch and branch in subject:
                return sha, subject
        return None


# ── Merge preflight (structured verdict) ────────────────────────────────────
class MergePreflight:
    """Assert HEAD == staging + per-branch exists/commits-ahead. Read-only."""

    def __init__(self, git):
        self.git = git

    def verdict(self, staging, branches):
        head = self.git.head_ref()
        head_ok = bool(staging) and head == staging
        per, blocked = [], []
        for branch in branches:
            exists = self.git.branch_exists(branch)
            ahead = self.git.commits_ahead(staging, branch) if exists else 0
            ok = exists and ahead > 0
            per.append({"branch": branch, "exists": exists,
                        "ahead": ahead, "ok": ok})
            if not ok:
                blocked.append(branch)
        return {"head_ok": head_ok, "head": head, "staging": staging,
                "branches": per, "blocked": blocked}


# ── Integration-line detection (shared key with BMI-3 sync skills) ───────────
class IntegrationLineResolver:
    """Resolve the integration line from grimoire-config.json, else the default.

    Reads `branch-model.integration-branch` from `.claude/grimoire-config.json`
    under `root`; absent / unreadable / blank ⇒ DEFAULT_INTEGRATION_LINE (`dev`).
    BMI-3's sync skills read the SAME key with the SAME default (in shell), so
    both lanes agree on the integration line for any repo.
    """

    def __init__(self, root="."):
        self.root = root

    def resolve(self):
        path = os.path.join(self.root, CONFIG_REL_PATH)
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            return DEFAULT_INTEGRATION_LINE
        cur = cfg
        for part in INTEGRATION_BRANCH_KEY:
            if not isinstance(cur, dict) or part not in cur:
                return DEFAULT_INTEGRATION_LINE
            cur = cur[part]
        # Tolerate both a bare string and a {"value": ...} dial wrapper.
        if isinstance(cur, dict):
            cur = cur.get("value")
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
        return DEFAULT_INTEGRATION_LINE


# ── Before-promotion divergence guard (BMI-2, v3.38, #126) ───────────────────
class DivergenceGuard:
    """Model-aware divergence check run BEFORE every promotion.

    Predicate (design §2): *divergence iff `main` carries tree content not
    reachable from the integration line.* A non-empty `INT..main` consisting
    solely of promotion merges (identical trees) is **benign** and must NOT
    HALT — this is the false-positive the naive `is-ancestor` check trips on the
    default model, where promotion-merge commits live only on `main`.

    RECONCILIATION NOTE (#126 acceptance criterion 2, re-affirmed v3.67): #126's
    original prescription asked for a literal `git merge-base --is-ancestor main
    <integration>` check. This tree-content-reachability predicate is the
    ACCEPTED implementation of that criterion, not a deviation from it — it is
    STRICTER (catches every real fork `is-ancestor` would, per Worked example
    (ii) in the design doc) and additionally avoids a false-positive the naive
    `is-ancestor` check trips on this repo's own healthy default model (Worked
    example (i): nine benign promotion-merge commits make `main` a non-ancestor
    of `dev` even though zero real divergence exists). See design §2 "Why the
    naive `is-ancestor` check is WRONG for the default model" for the full
    justification with verified command output. A future audit should treat
    this class, not a literal `is-ancestor` shell-out, as satisfying criterion 2.

    Algorithm:
      1. Fast accept — `git diff --quiet INT main`. Trees identical ⇒ no
         divergence, PROCEED (the overwhelmingly common healthy case; no
         per-commit work).
      2. Trees differ ⇒ enumerate `INT..main` and classify each commit. The
         first commit introducing tree content absent from INT is REAL
         divergence ⇒ HALT with the readable report (design §2 worked-example
         ii). If every main-only commit is a benign promotion merge, PROCEED.
    """

    def __init__(self, git, integration_line, published_line=PUBLISHED_LINE):
        self.git = git
        self.integration = integration_line
        self.published = published_line

    def check(self):
        """Return a structured verdict; never raises and never mutates a ref."""
        verdict = {
            "integration": self.integration,
            "published": self.published,
            "trees_identical": True,
            "diverged": False,
            "main_only_commits": [],
            "diverging_commits": [],
            "report": "",
        }
        # 1. Fast accept: identical trees ⇒ no divergence.
        if not self.git.trees_differ(self.integration, self.published):
            return verdict
        verdict["trees_identical"] = False
        # 2. Trees differ — enumerate the main-only commits for the report.
        main_only = self.git.log_oneline(self.integration, self.published)
        verdict["main_only_commits"] = [
            {"sha": sha, "subject": subj} for sha, subj in main_only]
        # 3. Classify: a commit whose patch is absent from INT is real work.
        diverging = [
            {"sha": sha, "subject": subj} for sha, subj in main_only
            if self.git.commit_introduces_work_absent_from(sha, self.integration)]
        if not diverging:
            # Trees differ but every main-only commit is a benign promotion
            # merge (none introduces unreachable content). PROCEED.
            return verdict
        verdict["diverged"] = True
        verdict["diverging_commits"] = diverging
        verdict["report"] = self._report(main_only, diverging)
        return verdict

    def _report(self, main_only, diverging):
        """The readable HALT report (design §2 worked-example ii shape)."""
        lines = [
            "DIVERGENCE: '%s' carries %d commit(s) of work not on integration "
            "line '%s':" % (self.published, len(main_only), self.integration)]
        shown = main_only[:4]
        for sha, subj in shown:
            lines.append("  %s %s" % (sha, subj))
        if len(main_only) > len(shown):
            lines.append("  ... (%d more)" % (len(main_only) - len(shown)))
        lines.append(
            "Promotion BLOCKED. Reconcile by merging '%s' INTO '%s' "
            "(merge-forward, §5);" % (self.published, self.integration))
        lines.append(
            "do NOT reset across the fork (data loss). See "
            "integration-branch-integrity-design.md.")
        return "\n".join(lines)


# ── Atomic, idempotent ticker ───────────────────────────────────────────────
class Ticker:
    """Flip §5 checkbox cells in place. Atomic (temp+replace), idempotent.

    Edits ONLY §5 ledger rows — it locates a row by its branch and rewrites the
    requested checkbox column's glyph. Re-ticking an already-set cell is a no-op.
    """

    COLUMN_INDEX = {c: i for i, c in enumerate(CHECK_COLUMNS)}

    def __init__(self, path):
        self.path = path

    @staticmethod
    def _glyph(value):
        return CHECK_DONE if value else CHECK_OPEN

    def tick(self, ticks):
        """Apply [(branch, column, value)]. Returns the list of changed cells."""
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.readlines()
        # Index requested edits by branch for a single pass over the file.
        wanted = {}
        for branch, column, value in ticks:
            if column not in self.COLUMN_INDEX:
                raise PlanError("unknown ledger column: %s" % column)
            wanted.setdefault(branch, {})[column] = bool(value)
        changed = []
        branch_re = re.compile(r"`([^`]+)`")
        for li, line in enumerate(lines):
            if not line.lstrip().startswith("|"):
                continue
            bm = branch_re.search(line)
            if not bm or bm.group(1) not in wanted:
                continue
            cells = line.rstrip("\n").split("|")
            # cells[0] is pre-pipe indent; cell 1 is branch, cells 2..5 checks.
            # Map a logical column index to its physical cell position.
            new_cells = list(cells)
            row_changed = False
            for column, value in wanted[bm.group(1)].items():
                phys = 2 + self.COLUMN_INDEX[column]  # branch occupies cell 1
                if phys >= len(new_cells):
                    continue
                current = new_cells[phys]
                target_glyph = self._glyph(value)
                if CHECK_DONE not in current and CHECK_OPEN not in current:
                    continue  # n/a cell — never overwrite
                # Idempotent: only rewrite if the glyph actually differs.
                replaced = re.sub(r"[☑☐]", target_glyph, current, count=1)
                if replaced != current:
                    new_cells[phys] = replaced
                    row_changed = True
                    changed.append({"branch": bm.group(1), "column": column,
                                    "value": value})
            if row_changed:
                lines[li] = "|".join(new_cells) + "\n"
        if changed:
            self._atomic_write(lines)
        return {"ok": True, "changed": changed}

    def _atomic_write(self, lines):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(tmp, self.path)


# ── Phase planner ───────────────────────────────────────────────────────────
class PhasePlanner:
    """First all-unticked pass -> {phase, batches, model_assignments}."""

    def __init__(self, ledger):
        self.ledger = ledger

    def _first_open_pass(self):
        for name, rows in self.ledger.passes.items():
            if any(not r.implemented for r in rows):
                return name, rows
        return None, []

    def plan(self, model_table=None):
        model_table = model_table or DEFAULT_MODEL_TABLE
        name, rows = self._first_open_pass()
        if name is None:
            return {"phase": None, "batches": [], "model_assignments": {}}
        open_rows = [r for r in rows if not r.implemented]
        branches = [r.branch for r in open_rows]
        bset = set(branches)
        edges = self.ledger.conflict_map
        # Batch 1 = branches with no in-phase mergeAfter dep; later batches
        # unlock as their deps land. Mirrors release-phase Step 2 grouping.
        batches, placed = [], set()
        remaining = list(branches)
        while remaining:
            batch = [b for b in remaining
                     if {d for d in edges.get(b, set()) if d in bset} <= placed]
            if not batch:
                batch = remaining[:]  # cycle guard: emit the rest as one batch
            batches.append(batch)
            placed.update(batch)
            remaining = [b for b in remaining if b not in placed]
        assignments = {r.branch: dict(model_table["medium"]) for r in open_rows}
        return {"phase": name, "batches": batches,
                "model_assignments": assignments}


# ── Engine facade (one entry point for CLI + server) ────────────────────────
class ReleasePlanEngine:
    """Bind a plan path + git view and expose the seven operations."""

    def __init__(self, plan=None, root=".", git=None):
        self.root = root
        self.plan_path = plan or PlanLocator(root).locate()
        self.ledger = Ledger(self.plan_path)
        self.git = git or GitView(root)

    def get_ledger(self):
        return self.ledger.to_dict()

    def diff(self):
        return {"plan": self.plan_path,
                "rows": LedgerDiff(self.ledger, self.git).rows()}

    def merge_queue(self, phase=None):
        return MergeQueue(self.ledger).compute(phase)

    def merge_preflight(self, staging, branches=None):
        if not branches:
            branches = MergeQueue(self.ledger).compute()["order"]
        verdict = MergePreflight(self.git).verdict(staging, branches)
        # Before-promotion divergence gate (BMI-2, #126): a promotion targets
        # the published line by way of the integration line, so any preflight is
        # also a promotion boundary. Attach the divergence verdict and fold a
        # real fork into `head_ok=False` so a caller that only checks head_ok
        # still stops. The detailed report rides on `divergence`.
        verdict["divergence"] = self.divergence_check()
        if verdict["divergence"]["diverged"]:
            verdict["head_ok"] = False
        return verdict

    def divergence_check(self, integration=None, published=PUBLISHED_LINE):
        """Run the model-aware before-promotion divergence guard (BMI-2)."""
        line = integration or IntegrationLineResolver(self.root).resolve()
        return DivergenceGuard(self.git, line, published).check()

    def plan_phase(self):
        return PhasePlanner(self.ledger).plan()

    def tick(self, ticks):
        return Ticker(self.plan_path).tick(ticks)

    def default_staging(self):
        """`version/{X.Y}` per the plan's own version, matching the staging-
        branch convention `grm-release-phase-merge` / `merge-preflight` use
        (falls back to the resolved integration line, usually `dev`, if the
        plan carries no parseable version)."""
        if self.ledger.version:
            return "version/%s" % self.ledger.version
        return IntegrationLineResolver(self.root).resolve()

    def detect_merged(self, staging=None):
        """Propose §5 ledger flips for un-ticked rows matching a merge commit
        on `staging` (default: this plan's `version/{X.Y}`, or the integration
        line). Never mutates the ledger — see MergeDetector docstring."""
        ref = staging or self.default_staging()
        return MergeDetector(self.ledger, self.git).detect(ref)


# ── Self-test (fixture plan in a temp dir; never the repo's real plans) ──────
def _fixture_plan():
    return """# Release Planning — v9.9

> status: agreed
> Companion fixture for release_plan.py --self-test.

---

## 3. Parallel Implementation Strategy

| Pass | Items | Rationale |
|---|---|---|
| Pass 1 | A1, B2 | disjoint |
| Pass 2 | C3 | needs A1 |

**Conflict map:**
- A1 ↔ C3: `foo.py` → sequenced.
- B2 ↔ C3: protocol doc → sequenced.

---

## 5. Status Ledger

### Pass 1

| Branch | Design doc | Implemented | Reviewed | Merged into version/9.9 |
|---|---|---|---|---|
| `alpha-v99` (A1) | ☐ | ☑ | ☐ | ☐ |
| `beta-v99` (B2) | n/a | ☑ | ☐ | ☐ |

### Pass 2

| Branch | Design doc | Implemented | Reviewed | Merged into version/9.9 |
|---|---|---|---|---|
| `gamma-v99` (C3) | ☐ | ☐ | ☐ | ☐ |

---

## 6. Out of scope

- nothing
"""


class _FakeGit:
    """Scripted GitView runner for the self-test (no real repo).

    Beyond the merge-preflight reads, it scripts the divergence-guard reads:
      - tree_diffs: set of frozenset({ref_a, ref_b}) pairs whose trees DIFFER
        (`git diff --quiet` → exit 1); any pair not listed is identical (exit 0).
      - logs: {(base, tip): [(sha, subject)]} for `git log --oneline base..tip`.
      - cherry_plus: set of (base, commit) pairs `git cherry` reports as `+`
        (patch absent from base ⇒ real work); unlisted pairs report `-` (benign).
    """

    def __init__(self, branches, head, ahead, tree_diffs=None, logs=None,
                 cherry_plus=None):
        self.branches = set(branches)
        self.head = head
        self.ahead = ahead
        self.tree_diffs = {frozenset(p) for p in (tree_diffs or [])}
        self.logs = logs or {}
        self.cherry_plus = {tuple(p) for p in (cherry_plus or [])}

    def __call__(self, args):
        if args[:1] == ["branch"]:
            return 0, "\n".join(sorted(self.branches)) + "\n", ""
        if args[:2] == ["symbolic-ref", "--short"]:
            return (0, self.head + "\n", "") if self.head else (1, "", "detached")
        if args[:2] == ["rev-parse", "--verify"]:
            return (0, "", "") if args[-1] in self.branches else (1, "", "")
        if args[:2] == ["rev-list", "--count"]:
            spec = args[-1]
            branch = spec.split("..", 1)[-1]
            return 0, "%d\n" % self.ahead.get(branch, 0), ""
        if args[:2] == ["diff", "--quiet"]:
            ref_a, ref_b = args[2], args[3]
            return (1, "", "") if frozenset((ref_a, ref_b)) in self.tree_diffs \
                else (0, "", "")
        if args[:1] == ["log"]:
            spec = args[-1]
            base, _, tip = spec.partition("..")
            rows = self.logs.get((base, tip), [])
            return 0, "".join("%s %s\n" % (s, sub) for s, sub in rows), ""
        if args[:1] == ["cherry"]:
            base, commit = args[1], args[2]
            mark = "+" if (base, commit) in self.cherry_plus else "-"
            return 0, "%s %s\n" % (mark, commit), ""
        return 1, "", "unhandled"


def _divergence_self_test():
    """Hermetic divergence-guard cases via injected git reads (no real repo)."""
    failures = []

    # (a) HEALTHY — main ahead of dev by promotion merges only; trees identical.
    #     This is design §2 worked-example (i): MUST NOT HALT. It is also the
    #     false-positive the naive `is-ancestor` check would wrongly flag.
    healthy = GitView(".", runner=_FakeGit(
        branches=["dev", "main"], head="dev", ahead={},
        tree_diffs=[],                       # dev vs main trees IDENTICAL
        logs={("dev", "main"): [("341e674", "release(v3.37.4): promote dev to main"),
                                ("c0a5150", "release(v3.37): promote dev to main")]},
        cherry_plus=[]))                     # every main-only commit benign
    hv = DivergenceGuard(healthy, "dev").check()
    if hv["diverged"]:
        failures.append("HEALTHY case must NOT diverge: %r" % hv)
    if not hv["trees_identical"]:
        failures.append("HEALTHY case trees must be identical: %r" % hv)
    if hv["report"]:
        failures.append("HEALTHY case must emit no HALT report: %r" % hv)

    # (a2) HEALTHY-2 — trees differ but ONLY because of benign promotion merges
    #      (cherry reports them all `-`). Still MUST NOT HALT. Guards the path
    #      where step-1 fast-accept misses but per-commit classification saves us.
    healthy2 = GitView(".", runner=_FakeGit(
        branches=["dev", "main"], head="dev", ahead={},
        tree_diffs=[("dev", "main")],        # trees differ → fall to classify
        logs={("dev", "main"): [("341e674", "release(v3.37.4): promote dev to main")]},
        cherry_plus=[]))                     # but the commit is benign (`-`)
    hv2 = DivergenceGuard(healthy2, "dev").check()
    if hv2["diverged"]:
        failures.append("HEALTHY-2 (benign-only, trees differ) must NOT HALT: %r" % hv2)

    # (b) REAL FORK — main carries the #126 v8.40 carve-out work unreachable from
    #     the integration line. Design §2 worked-example (ii): MUST HALT.
    forked = GitView(".", runner=_FakeGit(
        branches=["experimental", "main"], head="experimental", ahead={},
        tree_diffs=[("experimental", "main")],
        logs={("experimental", "main"): [
            ("1030f23", "release: v8.40 — feed-engine crate carve-out (vendored)"),
            ("46901b7", "Extract feed/ranking engine into standalone crate (#19, #505)"),
            ("57117c3", "Consume feed-engine as in-tree vendored release (#506)"),
            ("24c73dd", "sync: Grimoire upstream + Aura v3.21 (660 files)"),
            ("aaa1111", "more work"), ("bbb2222", "yet more"), ("ccc3333", "and more")]},
        cherry_plus=[("experimental", "1030f23"), ("experimental", "46901b7"),
                     ("experimental", "57117c3"), ("experimental", "24c73dd")]))
    fv = DivergenceGuard(forked, "experimental").check()
    if not fv["diverged"]:
        failures.append("REAL FORK must HALT (diverged): %r" % fv)
    if "DIVERGENCE:" not in fv["report"]:
        failures.append("fork report missing DIVERGENCE header: %r" % fv["report"])
    if "7 commit(s)" not in fv["report"]:
        failures.append("fork report must count all 7 main-only commits: %r" % fv["report"])
    if "merge-forward" not in fv["report"] or "do NOT reset" not in fv["report"]:
        failures.append("fork report must give merge-forward directive: %r" % fv["report"])
    if "1030f23" not in fv["report"] or "... (3 more)" not in fv["report"]:
        failures.append("fork report must list commits + elide overflow: %r" % fv["report"])

    return failures


def _integration_line_self_test():
    """IntegrationLineResolver reads the shared BMI-3 key, else defaults to dev."""
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, ".claude"))
        cfg_path = os.path.join(root, ".claude", "grimoire-config.json")
        # Absent config ⇒ default `dev`.
        if IntegrationLineResolver(root).resolve() != DEFAULT_INTEGRATION_LINE:
            failures.append("absent config should resolve to 'dev'")
        # Explicit bare-string key wins.
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write('{"schema-version":4,"name":"t",'
                     '"branch-model":{"integration-branch":"experimental"}}')
        if IntegrationLineResolver(root).resolve() != "experimental":
            failures.append("branch-model.integration-branch (string) not honored")
        # {"value": ...} dial wrapper also accepted.
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write('{"schema-version":4,"name":"t",'
                     '"branch-model":{"integration-branch":{"value":"trunk"}}}')
        if IntegrationLineResolver(root).resolve() != "trunk":
            failures.append("branch-model.integration-branch (dial) not honored")
        # Malformed JSON ⇒ safe default.
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        if IntegrationLineResolver(root).resolve() != DEFAULT_INTEGRATION_LINE:
            failures.append("malformed config should fall back to 'dev'")
    return failures


def _divergence_git_self_test():
    """End-to-end divergence guard against a throwaway temp git repo.

    Builds a real repo with a real promotion-merge topology so the check is
    exercised through actual `git diff --quiet` / `git log` / `git cherry` —
    NOT this repo's live state. Covers BOTH the healthy promotion case (no HALT)
    and a real fork (HALT). Skipped (not failed) if git is unavailable.
    """
    import subprocess
    import tempfile

    failures = []

    def git(root, *args, check=True):
        return subprocess.run(["git", "-C", root, *args],
                              capture_output=True, text=True, check=check)

    with tempfile.TemporaryDirectory() as root:
        try:
            git(root, "init", "-q", "-b", "dev")
        except (OSError, subprocess.CalledProcessError):
            return failures  # git unavailable — skip, do not fail the suite
        git(root, "config", "user.email", "t@t")
        git(root, "config", "user.name", "t")
        wf = os.path.join(root, "f.txt")

        def commit(msg, content):
            with open(wf, "w", encoding="utf-8") as fh:
                fh.write(content)
            git(root, "add", "f.txt")
            git(root, "commit", "-q", "-m", msg)

        # Shared base, then publish to main via a --no-ff promotion merge.
        commit("base", "v0\n")
        git(root, "branch", "main")
        commit("dev work A", "v1\n")
        # Promote dev → main (the benign promotion-merge pattern).
        git(root, "switch", "-q", "main")
        git(root, "merge", "--no-ff", "-q", "-m",
            "release(v1): promote dev to main", "dev")
        git(root, "switch", "-q", "dev")

        gv = GitView(root)
        # HEALTHY: main is ahead of dev by exactly the promotion merge; trees
        # are identical (merge's second parent IS the dev tip). MUST NOT HALT.
        healthy = DivergenceGuard(gv, "dev").check()
        if healthy["diverged"]:
            failures.append("temp-repo HEALTHY promotion must NOT HALT: %r" % healthy)
        if not healthy["trees_identical"]:
            failures.append("temp-repo HEALTHY trees should be identical: %r" % healthy)

        # REAL FORK: author out-of-band work directly on main (the #126 hazard).
        git(root, "switch", "-q", "main")
        commit("out-of-band release on main", "MAIN-ONLY\n")
        git(root, "switch", "-q", "dev")
        forked = DivergenceGuard(gv, "dev").check()
        if not forked["diverged"]:
            failures.append("temp-repo REAL FORK must HALT: %r" % forked)
        if "DIVERGENCE:" not in forked["report"]:
            failures.append("temp-repo fork must emit a report: %r" % forked)
        if not forked["diverging_commits"]:
            failures.append("temp-repo fork must list diverging commits: %r" % forked)

    return failures


class _FakeMergeGit:
    """Minimal GitView runner scripting only `git log --merges --oneline`.

    Kept separate from `_FakeGit` (which scripts the merge-preflight and
    divergence-guard reads over a `base..tip` spec) because `merges_oneline`
    calls `git log --merges --oneline --no-decorate <branch>` — a single-ref
    spec, not a range — so overloading the same fixture would blur two
    different fake-git contracts.
    """

    def __init__(self, merges):
        self.merges = merges  # [(sha, subject)]

    def __call__(self, args):
        if args[:2] == ["log", "--merges"]:
            body = "".join("%s %s\n" % (s, sub) for s, sub in self.merges)
            return 0, body, ""
        return 1, "", "unhandled"


def _detect_merged_self_test():
    """MergeDetector matching logic against a fixture ledger + fake merge log.

    No real git history required — the merge log is an injected fixture list,
    exercising the substring-match heuristic, the unmatched-row path, and the
    default-staging (`version/{X.Y}`) resolution.
    """
    failures = []
    ledger = Ledger.__new__(Ledger)  # bypass file I/O; build passes by hand
    ledger.path = "<fixture>"
    ledger.version = "9.9"
    ledger.passes = {
        "Pass 1": [
            LedgerRow("Pass 1", "alpha-v99", "A1",
                      {"design_doc": True, "implemented": True,
                       "reviewed": True, "merged": False}),
            LedgerRow("Pass 1", "beta-v99", "B2",
                      {"design_doc": None, "implemented": True,
                       "reviewed": False, "merged": False}),
        ],
        "Pass 2": [
            LedgerRow("Pass 2", "gamma-v99", "C3",
                      {"design_doc": False, "implemented": False,
                       "reviewed": False, "merged": False}),
            # Already ticked — must never be proposed again.
            LedgerRow("Pass 2", "delta-v99", "D4",
                      {"design_doc": True, "implemented": True,
                       "reviewed": True, "merged": True}),
        ],
    }
    ledger.conflict_map = {}

    git = GitView(".", runner=_FakeMergeGit(merges=[
        ("f00d1e1", "merge(v9.9): alpha-v99 — adds the foo widget"),
        ("c0ffee2", "Merge branch 'beta-v99' into version/9.9"),
        ("aaaaaaa", "release(v9.9): promote version/9.9 to dev"),
    ]))

    detector = MergeDetector(ledger, git)
    result = detector.detect("version/9.9")

    if result["staging"] != "version/9.9":
        failures.append("detect-merged staging echo: %r" % result["staging"])

    by_branch = {p["branch"]: p for p in result["proposals"]}
    if "alpha-v99" not in by_branch or by_branch["alpha-v99"]["sha"] != "f00d1e1":
        failures.append("alpha-v99 should propose sha f00d1e1: %r" % result)
    if "beta-v99" not in by_branch or by_branch["beta-v99"]["sha"] != "c0ffee2":
        failures.append("beta-v99 should propose sha c0ffee2: %r" % result)
    if "delta-v99" in by_branch:
        failures.append("already-merged delta-v99 must never be proposed: %r"
                        % result)

    unmatched_branches = {u["branch"] for u in result["unmatched"]}
    if unmatched_branches != {"gamma-v99"}:
        failures.append("gamma-v99 (no matching merge) should be unmatched: %r"
                        % result["unmatched"])

    # Never mutates the ledger — proposing is not applying.
    gamma = next(r for r in ledger.all_rows() if r.branch == "gamma-v99")
    if gamma.merged is not False:
        failures.append("detect-merged must not mutate the ledger in place")

    # The human-readable report names the proposal and never claims to apply.
    report = _format_detect_merged(result)
    if "f00d1e1" not in report or "PROPOSED" not in report:
        failures.append("report must surface the proposed sha: %r" % report)
    if "release_plan.py tick" not in report:
        failures.append("report must point at the apply path (tick): %r" % report)

    # default_staging() resolves version/{X.Y} from the ledger's own version.
    eng = ReleasePlanEngine.__new__(ReleasePlanEngine)
    eng.root = "."
    eng.ledger = ledger
    eng.git = git
    if eng.default_staging() != "version/9.9":
        failures.append("default_staging should be version/9.9: %r"
                        % eng.default_staging())

    return failures


def _self_test():
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "docs"))
        plan = os.path.join(root, "docs", "release-planning-v9.9.md")
        with open(plan, "w", encoding="utf-8") as fh:
            fh.write(_fixture_plan())
        # also drop a non-agreed older plan to prove the filter.
        old = os.path.join(root, "docs", "release-planning-v9.0.md")
        with open(old, "w", encoding="utf-8") as fh:
            fh.write("# v9.0\n\n> status: draft\n\n## 5. Status Ledger\n")

        # 1) locate picks the highest agreed plan.
        located = PlanLocator(root).locate()
        if os.path.basename(located) != "release-planning-v9.9.md":
            failures.append("locate picked %s" % located)

        ledger = Ledger(plan)
        # 2) parse: passes, rows, tri-state cells, item ids.
        if list(ledger.passes) != ["Pass 1", "Pass 2"]:
            failures.append("passes parsed: %r" % list(ledger.passes))
        d = ledger.to_dict()
        rows = {r["branch"]: r for r in
                d["passes"]["Pass 1"] + d["passes"]["Pass 2"]}
        if rows["alpha-v99"]["implemented"] is not True:
            failures.append("alpha implemented should be True")
        if rows["alpha-v99"]["merged"] is not False:
            failures.append("alpha merged should be False")
        if rows["beta-v99"]["design_doc"] is not None:
            failures.append("beta design_doc should be n/a (None)")
        if rows["alpha-v99"]["item"] != "A1":
            failures.append("alpha item id should be A1: %r" % rows["alpha-v99"])

        # 3) conflict map: gamma must merge after alpha and beta.
        if ledger.conflict_map.get("gamma-v99") != {"alpha-v99", "beta-v99"}:
            failures.append("conflict map for gamma: %r"
                            % ledger.conflict_map.get("gamma-v99"))

        # 4) diff vs git: alpha exists+ready, beta missing-branch, gamma not-started.
        git = GitView(root, runner=_FakeGit(
            branches=["alpha-v99", "version/9.9"], head="version/9.9",
            ahead={"alpha-v99": 3}))
        diff_rows = {r["branch"]: r for r in LedgerDiff(ledger, git).rows()}
        if diff_rows["alpha-v99"]["state"] != "ready":
            failures.append("alpha diff state: %r" % diff_rows["alpha-v99"])
        if diff_rows["beta-v99"]["state"] != "implemented-missing-branch":
            failures.append("beta diff state: %r" % diff_rows["beta-v99"])
        if diff_rows["gamma-v99"]["state"] != "not-started":
            failures.append("gamma diff state: %r" % diff_rows["gamma-v99"])

        # 5) merge queue: only alpha+beta are ready (gamma not implemented);
        #    they are independent so both appear, ledger order preserved.
        mq = MergeQueue(ledger).compute()
        if mq["order"] != ["alpha-v99", "beta-v99"]:
            failures.append("merge order: %r" % mq)
        if mq["blocked"]:
            failures.append("nothing should be blocked: %r" % mq)

        # 5b) toposort respects deps: mark gamma implemented, re-check.
        ledger2 = Ledger(plan)
        for r in ledger2.passes["Pass 2"]:
            r.cells["implemented"] = True
        mq2 = MergeQueue(ledger2).compute()
        if mq2["order"][-1] != "gamma-v99":
            failures.append("gamma must be last in toposort: %r" % mq2)
        if mq2["order"].index("gamma-v99") < mq2["order"].index("alpha-v99"):
            failures.append("gamma must follow alpha: %r" % mq2)

        # 6) merge preflight verdict.
        pf = MergePreflight(git).verdict("version/9.9", ["alpha-v99", "beta-v99"])
        if not pf["head_ok"]:
            failures.append("head_ok should be True: %r" % pf)
        alpha_v = next(b for b in pf["branches"] if b["branch"] == "alpha-v99")
        if not alpha_v["ok"] or alpha_v["ahead"] != 3:
            failures.append("alpha preflight: %r" % alpha_v)
        if "beta-v99" not in pf["blocked"]:
            failures.append("beta (no branch) should be blocked: %r" % pf)
        # head-drift case.
        drift = MergePreflight(GitView(root, runner=_FakeGit(
            ["alpha-v99"], head="alpha-v99", ahead={}))).verdict(
            "version/9.9", ["alpha-v99"])
        if drift["head_ok"]:
            failures.append("head drift must fail head_ok: %r" % drift)

        # 6b) DIVERGENCE GUARD (BMI-2, #126) — injected-read hermetic cases.
        failures.extend(_divergence_self_test())

        # 6c) integration-line detection reads branch-model.integration-branch.
        failures.extend(_integration_line_self_test())

        # 6d) end-to-end against a throwaway temp git repo (no live repo state).
        failures.extend(_divergence_git_self_test())

        # 7) tick: idempotent + atomic, edits only the matching row/column.
        t = Ticker(plan)
        res = t.tick([("alpha-v99", "merged", True)])
        if not res["changed"]:
            failures.append("tick should report a change")
        again = t.tick([("alpha-v99", "merged", True)])
        if again["changed"]:
            failures.append("re-tick must be a no-op (idempotent): %r" % again)
        reparsed = Ledger(plan)
        alpha = next(r for r in reparsed.all_rows() if r.branch == "alpha-v99")
        if alpha.merged is not True:
            failures.append("tick did not persist merged=True")
        # n/a cell must never be overwritten.
        t.tick([("beta-v99", "design_doc", True)])
        reparsed2 = Ledger(plan)
        beta = next(r for r in reparsed2.all_rows() if r.branch == "beta-v99")
        if beta.cells["design_doc"] is not None:
            failures.append("tick overwrote an n/a cell: %r" % beta.cells)

        # 8) plan_phase: first open pass is Pass 2 now (Pass 1 all implemented);
        #    re-read a fresh fixture so Pass 1 is still open for the batch test.
        plan3 = os.path.join(root, "docs", "release-planning-v9.9.md")
        with open(plan3, "w", encoding="utf-8") as fh:
            fh.write(_fixture_plan())
        # mark Pass 1 rows open to exercise batch grouping with the dep edge.
        led3 = Ledger(plan3)
        for r in led3.passes["Pass 1"]:
            r.cells["implemented"] = False
        pp = PhasePlanner(led3).plan()
        if pp["phase"] != "Pass 1":
            failures.append("plan_phase chose %r" % pp["phase"])
        if set(pp["batches"][0]) != {"alpha-v99", "beta-v99"}:
            failures.append("first batch should be alpha+beta: %r" % pp["batches"])
        if "alpha-v99" not in pp["model_assignments"]:
            failures.append("model_assignments missing alpha: %r"
                            % pp["model_assignments"])

        # 9) facade end-to-end against the fixture.
        eng = ReleasePlanEngine(plan=plan3, root=root, git=git)
        if eng.get_ledger()["version"] != "9.9":
            failures.append("engine version mismatch")
        if not isinstance(eng.merge_queue()["order"], list):
            failures.append("engine merge_queue shape")

        # 10) no-agreed-plan raises PlanError.
        with tempfile.TemporaryDirectory() as empty:
            os.makedirs(os.path.join(empty, "docs"))
            try:
                PlanLocator(empty).locate()
                failures.append("locate on empty should raise")
            except PlanError:
                pass

        # 11) detect-merged (maintenance-automation-design.md item 2): the
        #     matching heuristic, unmatched rows, non-mutation, the report, and
        #     default_staging() — all via an injected fixture (no real git log).
        failures.extend(_detect_merged_self_test())

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("release_plan self-test: OK (locate, §5 parse + tri-state + item ids, "
          "§3 conflict map, ledger-vs-git diff, merge-queue toposort + deps, "
          "merge-preflight verdict + head-drift, divergence guard "
          "[healthy/benign-only/real-fork + integration-line detection + temp-git "
          "e2e], idempotent/atomic tick + n/a guard, plan_phase batches + model "
          "assignments, facade, missing-plan raise, detect-merged proposal "
          "[match/unmatch/non-mutation/report/default-staging])")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────
def _emit(obj):
    print(json.dumps(obj, separators=JSON_COMPACT, default=str))


def _format_detect_merged(result):
    """Human-readable proposed-changes report for `detect-merged`.

    Never applies anything — the report is the confirmation artifact a human
    or agent reviews before running `tick` (or grm-ledger-tick).
    """
    lines = ["detect-merged: staging=%s" % result["staging"]]
    proposals = result["proposals"]
    if not proposals:
        lines.append("  no un-ticked row matches a merge commit on %s"
                      % result["staging"])
    else:
        lines.append("  PROPOSED ticks (branch-name-substring match — "
                      "review before applying; NOT auto-applied):")
        for p in proposals:
            item = " (%s)" % p["item"] if p["item"] else ""
            lines.append("    [%s] `%s`%s -> merged=true  %s %s"
                          % (p["pass"], p["branch"], item, p["sha"],
                             p["subject"]))
        lines.append("  Apply with: release_plan.py tick "
                      + " ".join("--branch %s --column merged --value true"
                                 % p["branch"] for p in proposals))
    if result["unmatched"]:
        lines.append("  still open (no matching merge commit found):")
        for u in result["unmatched"]:
            item = " (%s)" % u["item"] if u["item"] else ""
            lines.append("    [%s] `%s`%s" % (u["pass"], u["branch"], item))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic engine for the §5 release-planning ledger (#MCP-1).")
    ap.add_argument("verb", nargs="?", help="locate|get-ledger|diff|merge-queue|"
                    "merge-preflight|divergence-check|plan-phase|tick|"
                    "detect-merged")
    ap.add_argument("--root", default=".")
    ap.add_argument("--plan", default=None)
    ap.add_argument("--phase", default=None)
    ap.add_argument("--staging", default=None,
                    help="staging/integration ref to diff merges against "
                         "(detect-merged default: version/{X.Y} from the "
                         "plan, else the resolved integration line)")
    ap.add_argument("--integration", default=None,
                    help="integration line for divergence-check "
                         "(default: branch-model.integration-branch or 'dev')")
    ap.add_argument("--published", default=PUBLISHED_LINE,
                    help="published line for divergence-check (default: main)")
    ap.add_argument("--branch", action="append", dest="branches", default=None)
    ap.add_argument("--column", action="append", dest="columns", default=None)
    ap.add_argument("--value", action="append", dest="values", default=None)
    ap.add_argument("--json", action="store_true",
                    help="detect-merged: emit the proposal as JSON instead of "
                         "the human-readable report")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.verb:
        ap.error("a verb is required (or --self-test)")

    try:
        if args.verb == "locate":
            _emit({"plan": PlanLocator(args.root).locate()})
            return 0
        if args.verb == "divergence-check":
            # Pure git check — no agreed plan required. Exit 2 on real
            # divergence so a shell caller can HALT on a non-zero status.
            line = args.integration or IntegrationLineResolver(args.root).resolve()
            verdict = DivergenceGuard(
                GitView(args.root), line, args.published).check()
            _emit(verdict)
            return 2 if verdict["diverged"] else 0
        eng = ReleasePlanEngine(plan=args.plan, root=args.root)
        if args.verb == "get-ledger":
            _emit(eng.get_ledger())
        elif args.verb == "diff":
            _emit(eng.diff())
        elif args.verb == "merge-queue":
            _emit(eng.merge_queue(args.phase))
        elif args.verb == "merge-preflight":
            if not args.staging:
                ap.error("merge-preflight requires --staging")
            _emit(eng.merge_preflight(args.staging, args.branches))
        elif args.verb == "plan-phase":
            _emit(eng.plan_phase())
        elif args.verb == "tick":
            if not (args.branches and args.columns and args.values):
                ap.error("tick requires matching --branch/--column/--value")
            if not (len(args.branches) == len(args.columns) == len(args.values)):
                ap.error("--branch/--column/--value counts must match")
            ticks = [(b, c, v.lower() in ("true", "1", "yes", "☑"))
                     for b, c, v in zip(args.branches, args.columns, args.values)]
            _emit(eng.tick(ticks))
        elif args.verb == "detect-merged":
            result = eng.detect_merged(args.staging)
            if args.json:
                _emit(result)
            else:
                print(_format_detect_merged(result))
        else:
            ap.error("unknown verb: %s" % args.verb)
    except PlanError as exc:
        print("release_plan: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
