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
    emitting a structured verdict
  - tick rows atomically + idempotently (file edit only)
  - plan a phase (first all-unticked pass -> batches + model assignments)

File-write-only contract: this engine NEVER runs git mutations. Every git call
is a read; the only side effect is editing the §5 ledger file via `tick`. The
AGENT commits. Design: docs/design/grimoire-release-server-design.md.

Standard: Python 3 stdlib-only (docs/design/scripting-unification-design.md).

CLI:
  release_plan.py locate [--root DIR]
  release_plan.py get-ledger [--plan FILE] [--root DIR]
  release_plan.py diff [--plan FILE] [--root DIR]
  release_plan.py merge-queue [--phase NAME] [--plan FILE] [--root DIR]
  release_plan.py merge-preflight --staging REF [--branch B ...] [--plan FILE] [--root DIR]
  release_plan.py plan-phase [--plan FILE] [--root DIR]
  release_plan.py tick --branch B --column COL --value (true|false) [...] [--plan FILE] [--root DIR]
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
PLAN_GLOB = os.path.join("docs", "release-planning-v*.md")
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
        return sorted(glob.glob(os.path.join(self.root, PLAN_GLOB)),
                      key=self._version_key)

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
        return MergePreflight(self.git).verdict(staging, branches)

    def plan_phase(self):
        return PhasePlanner(self.ledger).plan()

    def tick(self, ticks):
        return Ticker(self.plan_path).tick(ticks)


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
    """Scripted GitView runner for the self-test (no real repo)."""

    def __init__(self, branches, head, ahead):
        self.branches = set(branches)
        self.head = head
        self.ahead = ahead

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
        return 1, "", "unhandled"


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

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("release_plan self-test: OK (locate, §5 parse + tri-state + item ids, "
          "§3 conflict map, ledger-vs-git diff, merge-queue toposort + deps, "
          "merge-preflight verdict + head-drift, idempotent/atomic tick + n/a "
          "guard, plan_phase batches + model assignments, facade, missing-plan "
          "raise)")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────
def _emit(obj):
    print(json.dumps(obj, separators=JSON_COMPACT, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Deterministic engine for the §5 release-planning ledger (#MCP-1).")
    ap.add_argument("verb", nargs="?", help="locate|get-ledger|diff|merge-queue|"
                    "merge-preflight|plan-phase|tick")
    ap.add_argument("--root", default=".")
    ap.add_argument("--plan", default=None)
    ap.add_argument("--phase", default=None)
    ap.add_argument("--staging", default=None)
    ap.add_argument("--branch", action="append", dest="branches", default=None)
    ap.add_argument("--column", action="append", dest="columns", default=None)
    ap.add_argument("--value", action="append", dest="values", default=None)
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
        else:
            ap.error("unknown verb: %s" % args.verb)
    except PlanError as exc:
        print("release_plan: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
