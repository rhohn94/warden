#!/usr/bin/env python3
"""architecture_diagram.py — dependency-chain architecture diagram generator.

Reads a project's own `vendor.toml` / `vendor.lock` (the Dependency Channel
surface `dependency_channel_conformance.py` already parses) and emits a
Graphviz DOT (or `--json`) diagram of who-depends-on-whom. Sibling script under
`grm-dependency-audit`, since it reads the exact same `vendor.toml`/`vendor.lock`
surface — no new skill needed.

This makes portfolio dependency shape visible from data that already exists
(vendor.toml pins), rather than a hand-drawn diagram that rots the moment a pin
changes. See `docs/grimoire/design/dependency-architecture-diagram-design.md`.

Node/edge model:
  - One node for *this* project (metadata pulled from `project_status.py`'s
    existing JSON output — reused, not re-derived).
  - One node per `[deps.<name>]` block in `vendor.toml`.
  - One edge `this-project -> name`, labeled with the pinned version + channel.
  - Optionally (`--depth N`, default 1), a dependency's own `vendor.toml` is
    fetched (via `ChannelProbe`, reused from `dependency_channel_conformance.py`
    rather than a second network client) and its edges are added too, walked
    recursively up to `N` levels. Bounded and explicit — never crawls an
    unbounded/unknown portfolio.

Staleness marking: with `--with-conformance`, a pin whose `vendor.lock`
`release_tag` is behind the producer's latest release (as already known from a
`DependencyChannelConformance` run) is rendered as a visually distinct edge
(`style=dashed,color=orange`). Without `--with-conformance` this is a pure
offline read of `vendor.lock` — no forced network call just to draw the graph.

Determinism: same input -> byte-identical output (sorted node/edge iteration,
no dict-order nondeterminism).

Usage:
    # Offline self-test (no network):
    python3 architecture_diagram.py --self-test

    # Diagram a real repo root, DOT to stdout:
    python3 architecture_diagram.py --root /path/to/repo

    # JSON graph instead of DOT:
    python3 architecture_diagram.py --root . --json

    # Recursive walk (2 levels), degrading gracefully offline:
    python3 architecture_diagram.py --root . --depth 2

Design: docs/grimoire/design/dependency-architecture-diagram-design.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

# Reuse the existing network client + offline-degradation contract rather than
# writing a second one (design: "reusing that class rather than a second
# network client").
from dependency_channel_conformance import (
    ChannelProbe,
    ChannelUnreachable,
    DependencyChannelConformance,
)

# Reuse project_status.py's existing JSON output as the per-node metadata
# source for *this* project, rather than re-parsing grimoire-config.json.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "grm-agent-status-broker"))
from project_status import build_status  # noqa: E402


# ── Constants ───────────────────────────────────────────────────────────────

#: Default recursive-walk depth (design: "Depth is bounded and explicit").
DEFAULT_DEPTH = 1
#: A hard ceiling so an accidental huge --depth can never crawl unbounded.
MAX_DEPTH = 8

#: DOT edge attributes applied to a stale (behind-latest) pin.
STALE_EDGE_STYLE = "style=dashed,color=orange"

#: Node kind tags (used for both DOT node styling and the --json shape).
NODE_KIND_SELF = "self"
NODE_KIND_DEP = "dependency"


# ── Graph model ───────────────────────────────────────────────────────────────

class GraphNode:
    """One node in the dependency-chain graph.

    `node_id` is the stable dotted-quad identifier used in DOT/`--json`
    (the project name, or `repo` slug for a recursively-discovered dep).
    `meta` carries whatever per-node metadata is known (framework-version,
    work-paradigm for the self node; repo/channel for a dependency node).
    """

    def __init__(self, node_id: str, kind: str, meta: Optional[dict[str, Any]] = None) -> None:
        self.node_id = node_id
        self.kind = kind
        self.meta = meta or {}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.node_id, "kind": self.kind, "meta": self.meta}


class GraphEdge:
    """One `source -> target` edge, labeled with the pinned version + channel."""

    def __init__(
        self,
        source: str,
        target: str,
        version: Optional[str],
        channel: Optional[str],
        stale: bool = False,
    ) -> None:
        self.source = source
        self.target = target
        self.version = version
        self.channel = channel
        self.stale = stale

    def label(self) -> str:
        parts = [p for p in (self.version, self.channel) if p]
        return "@".join(parts) if parts else ""

    def sort_key(self) -> tuple:
        return (self.source, self.target, self.version or "", self.channel or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "version": self.version,
            "channel": self.channel,
            "stale": self.stale,
        }


class DependencyGraph:
    """Accumulates nodes + edges for one diagram run.

    Nodes/edges are stored in insertion order internally but always emitted
    (DOT and --json alike) via sorted iteration, so re-running against
    unchanged input produces byte-identical output regardless of dict/set
    iteration order (design: "Determinism ... no dict-order nondeterminism").
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []

    def add_node(self, node: GraphNode) -> None:
        # First writer wins (the self-node / first-discovered dep node keeps
        # its richer metadata rather than being overwritten by a later, more
        # sparsely-known reference to the same id).
        if node.node_id not in self._nodes:
            self._nodes[node.node_id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        self._edges.append(edge)

    def sorted_nodes(self) -> list[GraphNode]:
        return [self._nodes[k] for k in sorted(self._nodes.keys())]

    def sorted_edges(self) -> list[GraphEdge]:
        return sorted(self._edges, key=lambda e: e.sort_key())

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    # ── Emitters ──────────────────────────────────────────────────────────

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.sorted_nodes()],
            "edges": [e.to_dict() for e in self.sorted_edges()],
        }

    def to_dot(self, graph_name: str = "dependency_chain") -> str:
        """Render deterministic Graphviz DOT (plain text, zero-dependency)."""
        lines = [f'digraph "{_dot_escape(graph_name)}" {{', "  rankdir=LR;"]
        for node in self.sorted_nodes():
            shape = "box" if node.kind == NODE_KIND_SELF else "ellipse"
            label_lines = [node.node_id]
            fw = node.meta.get("framework-version")
            paradigm = node.meta.get("work-paradigm")
            repo = node.meta.get("repo")
            if fw:
                label_lines.append(f"framework {fw}")
            if paradigm:
                label_lines.append(f"paradigm {paradigm}")
            if repo and repo != node.node_id:
                label_lines.append(repo)
            label = "\\n".join(_dot_escape(part) for part in label_lines)
            lines.append(
                f'  "{_dot_escape(node.node_id)}" [label="{label}", shape={shape}];'
            )
        for edge in self.sorted_edges():
            attrs = [f'label="{_dot_escape(edge.label())}"']
            if edge.stale:
                attrs.append(STALE_EDGE_STYLE)
            attr_str = ", ".join(attrs)
            lines.append(
                f'  "{_dot_escape(edge.source)}" -> "{_dot_escape(edge.target)}" [{attr_str}];'
            )
        lines.append("}")
        return "\n".join(lines) + "\n"


def _dot_escape(text: str) -> str:
    """Escape a label for safe embedding in a DOT double-quoted string."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


# ── vendor.toml / vendor.lock loaders (mirrors the conformance script) ──────

def load_vendor_manifest(root: str) -> dict[str, Any]:
    """Parse `vendor.toml` -> its `deps` table. Empty dict when absent."""
    path = os.path.join(root, "vendor.toml")
    if not os.path.exists(path):
        return {}
    import tomllib  # stdlib on 3.11+

    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    deps = data.get("deps")
    return deps if isinstance(deps, dict) else {}


def load_vendor_lock(root: str) -> dict[str, Any]:
    """Parse `vendor.lock` -> its `deps` map. Empty dict when absent/invalid."""
    path = os.path.join(root, "vendor.lock")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        if not text:
            return {}
        data = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return {}
    deps = data.get("deps")
    return deps if isinstance(deps, dict) else {}


# ── Staleness (optional, --with-conformance only) ───────────────────────────

def stale_pins(root: str, probe: Optional[ChannelProbe] = None) -> tuple[set[str], list[str]]:
    """Return `({stale dep names}, [degradation messages])` via a conformance run.

    A pin is "stale" when its `vendor.lock` `release_tag` differs from the tag
    the channel probe resolves as latest-published for that dep. This reuses
    `DependencyChannelConformance`'s existing publish probe rather than a third
    network client; degrades (never raises) when the channel is unreachable,
    mirroring the conformance script's own offline-degradation contract.
    """
    manifest = load_vendor_manifest(root)
    lock = load_vendor_lock(root)
    stale: set[str] = set()
    degradations: list[str] = []
    checker_probe = probe if probe is not None else ChannelProbe()
    for dep, spec in manifest.items():
        if not isinstance(spec, dict):
            continue
        repo = spec.get("repo")
        version = spec.get("version")
        if not repo or not version:
            continue
        lock_entry = lock.get(dep) if isinstance(lock.get(dep), dict) else {}
        locked_tag = lock_entry.get("release_tag") or f"v{version}"
        try:
            latest_tag = checker_probe.latest_release_tag(repo)
        except ChannelUnreachable as exc:
            degradations.append(
                f"staleness check for dep {dep!r} degraded: {exc} "
                f"(channel unreachable — reported, not failed)"
            )
            continue
        except AttributeError:
            # Base ChannelProbe (network) has no latest_release_tag helper in
            # the conformance script; treat as an unreachable degrade rather
            # than crash the diagram generator.
            degradations.append(
                f"staleness check for dep {dep!r} degraded: probe does not "
                f"support latest-release lookup"
            )
            continue
        if latest_tag and latest_tag != locked_tag:
            stale.add(dep)
    return stale, degradations


# ── Recursive walk (bounded, explicit) ──────────────────────────────────────

def fetch_remote_vendor_toml(probe: ChannelProbe, repo: str) -> Optional[bytes]:
    """Fetch `vendor.toml` bytes from *repo*'s default branch via `gh api`.

    Mirrors the `gh api repos/<repo>/contents/vendor.toml`-style call named in
    the design doc. Isolated to its own function (rather than bolted onto
    `ChannelProbe`) so the self-test can stub it without touching the
    conformance script's own network surface. Returns None (never raises) when
    the file does not exist or the channel is unreachable — the recursive walk
    degrades to single-level rather than hard-failing.
    """
    import shutil
    import subprocess

    if shutil.which("gh") is None:
        raise ChannelUnreachable("the `gh` CLI is not installed")
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/vendor.toml",
             "--jq", ".content"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ChannelUnreachable(f"`gh api` invocation failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        if "not found" in stderr:
            return None
        raise ChannelUnreachable(f"`gh api` did not succeed: {proc.stderr.strip()!r}")
    import base64

    try:
        return base64.b64decode(proc.stdout.strip())
    except (ValueError, TypeError) as exc:
        raise ChannelUnreachable(f"`gh api` returned undecodable content: {exc}") from exc


def parse_vendor_toml_bytes(data: bytes) -> dict[str, Any]:
    """Parse raw `vendor.toml` bytes (as fetched remotely) -> its `deps` table."""
    import tomllib

    parsed = tomllib.loads(data.decode("utf-8"))
    deps = parsed.get("deps")
    return deps if isinstance(deps, dict) else {}


def walk_dependencies(
    graph: DependencyGraph,
    source_id: str,
    manifest: dict[str, Any],
    lock: dict[str, Any],
    stale_deps: set[str],
    depth_remaining: int,
    visited: set[str],
    probe: Optional[ChannelProbe],
    degradations: list[str],
    fetch_fn: Any = fetch_remote_vendor_toml,
) -> None:
    """Add one project's dep edges to *graph*, recursing while depth remains.

    `visited` guards against a dependency cycle re-entering the walk. Recursion
    is a plain offline operation for depth 1 (just this project's own
    vendor.toml, already loaded by the caller); each additional level requires
    a network fetch of the dependency's own vendor.toml and degrades to
    "no further levels, loud warning" when the channel is unreachable — it
    never raises out of the walk.
    """
    for dep in sorted(manifest.keys()):
        spec = manifest[dep]
        if not isinstance(spec, dict):
            continue
        repo = spec.get("repo")
        version = spec.get("version")
        channel = spec.get("channel")
        target_id = repo if repo else dep
        graph.add_node(GraphNode(
            target_id,
            NODE_KIND_DEP,
            {"repo": repo, "channel": channel, "kind": spec.get("kind")},
        ))
        lock_entry = lock.get(dep) if isinstance(lock.get(dep), dict) else {}
        pinned_version = lock_entry.get("version") or version
        graph.add_edge(GraphEdge(
            source_id, target_id, pinned_version, channel, stale=dep in stale_deps,
        ))

        if depth_remaining <= 0 or not repo or repo in visited:
            continue
        if probe is None:
            continue
        visited = visited | {repo}
        try:
            raw = fetch_fn(probe, repo)
        except ChannelUnreachable as exc:
            degradations.append(
                f"recursive walk stopped at {repo!r}: {exc} "
                f"(network/gh unavailable — single-level only from here)"
            )
            continue
        if raw is None:
            degradations.append(
                f"recursive walk stopped at {repo!r}: no vendor.toml found "
                f"(not a Grimoire-managed dependency project)"
            )
            continue
        try:
            child_manifest = parse_vendor_toml_bytes(raw)
        except Exception as exc:  # noqa: BLE001 - any parse failure degrades, never crashes
            degradations.append(
                f"recursive walk stopped at {repo!r}: could not parse its "
                f"vendor.toml ({exc})"
            )
            continue
        walk_dependencies(
            graph, target_id, child_manifest, {}, set(),
            depth_remaining - 1, visited, probe, degradations, fetch_fn,
        )


# ── Orchestration ────────────────────────────────────────────────────────────

def build_graph(
    root: str,
    depth: int = DEFAULT_DEPTH,
    with_conformance: bool = False,
    probe: Optional[ChannelProbe] = None,
    fetch_fn: Any = fetch_remote_vendor_toml,
) -> tuple[DependencyGraph, list[str]]:
    """Build the dependency-chain graph for the project at *root*.

    Returns `(graph, degradations)`. Degradations are informational (loud
    warnings), never exceptions — mirrors
    `dependency_channel_conformance.py`'s offline-degradation contract.
    """
    depth = max(0, min(depth, MAX_DEPTH))
    degradations: list[str] = []

    status = build_status(root)
    self_id = status.get("project") or os.path.basename(os.path.abspath(root)) or "this-project"
    graph = DependencyGraph()
    graph.add_node(GraphNode(
        self_id,
        NODE_KIND_SELF,
        {
            "framework-version": status.get("framework-version"),
            "work-paradigm": status.get("work-paradigm"),
        },
    ))

    manifest = load_vendor_manifest(root)
    lock = load_vendor_lock(root)

    stale_deps: set[str] = set()
    if with_conformance and manifest:
        stale_deps, stale_degrades = stale_pins(root, probe)
        degradations.extend(stale_degrades)

    walk_probe = probe if (depth > 0 and probe is not None) else (ChannelProbe() if depth > 0 else None)
    walk_dependencies(
        graph, self_id, manifest, lock, stale_deps,
        depth - 1, {self_id}, walk_probe, degradations, fetch_fn,
    )

    return graph, degradations


# ── Offline self-test ────────────────────────────────────────────────────────

def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _build_single_project_fixture(root: str) -> None:
    """A project with one vendored dep, no lock drift — the base case."""
    os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
    os.makedirs(os.path.join(root, "docs"), exist_ok=True)
    _write(
        os.path.join(root, ".claude", "grimoire-config.json"),
        json.dumps({
            "schema-version": 4,
            "name": "Demo",
            "framework-version": "v3.57",
            "work-paradigm": {"value": "Noir"},
        }),
    )
    _write(os.path.join(root, "docs", "version-history.md"),
           "# Version History\n\n## v3.57 — Demo release\n\nbody\n")
    _write(os.path.join(root, "docs", "roadmap.md"), "# Roadmap\n")
    _write(
        os.path.join(root, "vendor.toml"),
        'schema_version = 1\n\n'
        '[deps.token-bookkeeper]\n'
        'repo = "rhohn94/token-bookkeeper"\n'
        'channel = "stable"\n'
        'version = "0.2.0"\n'
        'artifact = "token-bookkeeper-v0.2.0.tar.gz"\n'
        'kind = "standard-package"\n'
        'dest = "lib/third-party/token-bookkeeper"\n',
    )
    _write(
        os.path.join(root, "vendor.lock"),
        json.dumps({
            "schema_version": 1,
            "deps": {
                "token-bookkeeper": {
                    "version": "0.2.0",
                    "channel": "stable",
                    "release_tag": "v0.2.0",
                    "tree_sha256": "sha256:" + "0" * 64,
                }
            },
        }, indent=2) + "\n",
    )


class StubDiagramProbe(ChannelProbe):
    """Offline stub standing in for `ChannelProbe.verify_release` +
    `latest_release_tag` in the staleness/recursive-walk self-tests.
    """

    def __init__(self, latest_tags: Optional[dict[str, str]] = None,
                 unreachable: Optional[set[str]] = None) -> None:
        self.latest_tags = dict(latest_tags or {})
        self.unreachable = unreachable or set()

    def latest_release_tag(self, repo: str) -> Optional[str]:
        if repo in self.unreachable:
            raise ChannelUnreachable(f"stub: {repo} unreachable")
        return self.latest_tags.get(repo)


def _stub_fetch_fn(remote_manifests: dict[str, bytes]):
    """Build a `fetch_fn` stand-in for `fetch_remote_vendor_toml`, offline."""

    def _fetch(probe: ChannelProbe, repo: str) -> Optional[bytes]:
        if repo in getattr(probe, "unreachable_fetch", set()):
            raise ChannelUnreachable(f"stub: fetch for {repo} unreachable")
        return remote_manifests.get(repo)

    return _fetch


def run_self_test() -> int:
    """Run the diagram builder against built-in offline fixtures. No network."""
    import tempfile
    import shutil

    failures: list[str] = []

    def ok(label: str) -> None:
        print(f"  OK: {label}")

    print("architecture_diagram.py --self-test")
    print()

    tmp = tempfile.mkdtemp(prefix="arch-diagram-")
    try:
        # ── Group: single-project graph ──────────────────────────────────
        print("Group: single-project graph")
        d1 = os.path.join(tmp, "single")
        _build_single_project_fixture(d1)
        graph, degrades = build_graph(d1, depth=1, with_conformance=False, probe=None)
        node_ids = {n.node_id for n in graph.sorted_nodes()}
        if "Demo" not in node_ids:
            failures.append(f"self node 'Demo' missing from graph: {node_ids}")
        if "rhohn94/token-bookkeeper" not in node_ids:
            failures.append(f"dep node missing from graph: {node_ids}")
        else:
            ok("self + one dependency node present")
        edges = graph.sorted_edges()
        if len(edges) != 1:
            failures.append(f"expected exactly 1 edge, got {len(edges)}")
        else:
            e = edges[0]
            if e.source != "Demo" or e.target != "rhohn94/token-bookkeeper":
                failures.append(f"edge endpoints wrong: {e.source} -> {e.target}")
            elif e.label() != "0.2.0@stable":
                failures.append(f"edge label wrong: {e.label()!r}")
            else:
                ok("edge labeled with pinned version + channel (0.2.0@stable)")
        if degrades:
            failures.append(f"single-project graph should have no degradations: {degrades}")

        # DOT emission sanity + determinism.
        dot1 = graph.to_dot()
        dot2 = graph.to_dot()
        if dot1 != dot2:
            failures.append("DOT emission not deterministic across two calls")
        else:
            ok("DOT emission is byte-identical across repeated calls")
        if not dot1.startswith("digraph") or "->" not in dot1:
            failures.append(f"DOT output does not look like a digraph: {dot1[:80]!r}")
        else:
            ok("DOT output has digraph header and an edge")

        # JSON emission sanity.
        as_json = graph.to_json_dict()
        if set(as_json.keys()) != {"nodes", "edges"}:
            failures.append(f"json graph shape wrong: {sorted(as_json.keys())}")
        else:
            ok("JSON graph shape is {nodes, edges}")
        json1 = json.dumps(as_json, sort_keys=True)
        json2 = json.dumps(graph.to_json_dict(), sort_keys=True)
        if json1 != json2:
            failures.append("JSON emission not deterministic across two calls")
        else:
            ok("JSON emission is byte-identical across repeated calls")

        # Rebuilding the graph from scratch must match too (true input->output determinism).
        graph_again, _ = build_graph(d1, depth=1, with_conformance=False, probe=None)
        if graph_again.to_dot() != dot1:
            failures.append("re-running build_graph on unchanged input changed DOT output")
        else:
            ok("re-running build_graph on unchanged input is byte-identical")

        # ── Group: stale-pin edge marking (--with-conformance) ───────────
        print("\nGroup: stale-pin edge marking")
        d2 = os.path.join(tmp, "stale")
        _build_single_project_fixture(d2)
        stale_probe = StubDiagramProbe(latest_tags={"rhohn94/token-bookkeeper": "v0.3.0"})
        graph_stale, degrades_stale = build_graph(
            d2, depth=1, with_conformance=True, probe=stale_probe,
        )
        stale_edges = [e for e in graph_stale.sorted_edges() if e.stale]
        if len(stale_edges) != 1:
            failures.append(f"expected 1 stale edge, got {len(stale_edges)}")
        else:
            ok("pin behind latest release is marked stale")
        dot_stale = graph_stale.to_dot()
        if STALE_EDGE_STYLE not in dot_stale:
            failures.append("stale edge DOT output missing dashed/orange style attribute")
        else:
            ok("stale edge rendered with distinct DOT style (dashed, orange)")

        # A pin that matches latest must NOT be marked stale.
        fresh_probe = StubDiagramProbe(latest_tags={"rhohn94/token-bookkeeper": "v0.2.0"})
        graph_fresh, _ = build_graph(d2, depth=1, with_conformance=True, probe=fresh_probe)
        if any(e.stale for e in graph_fresh.sorted_edges()):
            failures.append("pin matching latest release must not be marked stale")
        else:
            ok("pin matching latest release is not marked stale")

        # Without --with-conformance, no network call is made even if a probe is given
        # (pure offline read of vendor.lock) — verified by using a probe that would
        # raise on any call.
        class ExplodingProbe(ChannelProbe):
            def latest_release_tag(self, repo: str) -> Optional[str]:  # noqa: D401
                raise AssertionError("latest_release_tag must not be called without --with-conformance")

        graph_noconf, _ = build_graph(d2, depth=1, with_conformance=False, probe=ExplodingProbe())
        if any(e.stale for e in graph_noconf.sorted_edges()):
            failures.append("with_conformance=False must never mark an edge stale")
        else:
            ok("with_conformance=False never touches the network / never marks stale")

        # ── Group: offline degradation (recursive walk, no network) ──────
        print("\nGroup: offline degradation path")
        d3 = os.path.join(tmp, "recursive")
        _build_single_project_fixture(d3)

        class UnreachableProbe(ChannelProbe):
            pass

        def unreachable_fetch(probe, repo):
            raise ChannelUnreachable("no `gh` CLI / no network (simulated)")

        graph_deg, degrades_deg = build_graph(
            d3, depth=2, with_conformance=False,
            probe=UnreachableProbe(), fetch_fn=unreachable_fetch,
        )
        if not degrades_deg:
            failures.append("expected a loud degradation warning when network is unavailable")
        else:
            ok(f"recursive walk degrades gracefully with a loud warning: "
               f"{degrades_deg[0][:60]}...")
        # Depth-1 edges must still be present even though depth-2 failed.
        if "rhohn94/token-bookkeeper" not in {n.node_id for n in graph_deg.sorted_nodes()}:
            failures.append("depth-1 edges lost when depth-2 network walk failed")
        else:
            ok("single-level (depth-1) edges preserved despite depth-2 network failure")

        # Recursive walk succeeding: child vendor.toml fetched, its edges added.
        print("\nGroup: recursive walk (depth 2, stubbed network)")
        child_toml = (
            b'schema_version = 1\n\n'
            b'[deps.aura]\n'
            b'repo = "rhohn94/design-language"\n'
            b'channel = "stable"\n'
            b'version = "3.20.0"\n'
        )
        fetch_stub = _stub_fetch_fn({"rhohn94/token-bookkeeper": child_toml})
        graph_recurse, degrades_recurse = build_graph(
            d3, depth=2, with_conformance=False,
            probe=ChannelProbe(), fetch_fn=fetch_stub,
        )
        recurse_ids = {n.node_id for n in graph_recurse.sorted_nodes()}
        if "rhohn94/design-language" not in recurse_ids:
            failures.append(f"depth-2 recursive dep missing: {recurse_ids}")
        else:
            ok("depth-2 recursion adds the dependency's own vendor.toml edges")
        if degrades_recurse:
            failures.append(f"successful recursive walk should have no degradations: {degrades_recurse}")

        # Depth bound: depth=1 must NOT recurse even if the child manifest is fetchable.
        graph_depth1, _ = build_graph(
            d3, depth=1, with_conformance=False,
            probe=ChannelProbe(), fetch_fn=fetch_stub,
        )
        if "rhohn94/design-language" in {n.node_id for n in graph_depth1.sorted_nodes()}:
            failures.append("depth=1 must not recurse into a dependency's own vendor.toml")
        else:
            ok("depth=1 stays single-level (no unbounded crawl)")

        # ── Group: empty project (no vendor.toml) ────────────────────────
        print("\nGroup: project with no vendor.toml (self node only)")
        d_empty = os.path.join(tmp, "empty")
        os.makedirs(d_empty, exist_ok=True)
        graph_empty, degrades_empty = build_graph(d_empty, depth=1)
        if len(graph_empty.sorted_nodes()) != 1:
            failures.append(
                f"expected exactly 1 (self) node with no vendor.toml, got "
                f"{len(graph_empty.sorted_nodes())}"
            )
        else:
            ok("no vendor.toml -> self node only, no crash")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} unexpected result(s):")
        for f in failures:
            print(f"\n  - {f}")
        return 1
    print("SELF-TEST PASSED.")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def run_diagram(
    root: str, depth: int, with_conformance: bool, as_json: bool,
) -> int:
    """Build and print the diagram for a real repo root. Returns exit code."""
    if not os.path.isdir(root):
        print(f"error: --root is not a directory: {root}", file=sys.stderr)
        return 2
    probe = ChannelProbe() if (depth > 0 or with_conformance) else None
    graph, degradations = build_graph(
        root, depth=depth, with_conformance=with_conformance, probe=probe,
    )
    for d in degradations:
        print(f"DEGRADE: {d}", file=sys.stderr)
    if as_json:
        print(json.dumps(graph.to_json_dict(), indent=2, sort_keys=True))
    else:
        print(graph.to_dot(), end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dependency-chain architecture diagram generator — reads a "
            "project's vendor.toml/vendor.lock and emits a Graphviz DOT (or "
            "--json) dependency graph. See "
            "docs/grimoire/design/dependency-architecture-diagram-design.md."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="Run against built-in offline fixtures (no network calls).",
    )
    mode.add_argument(
        "--root",
        metavar="DIR",
        help="Project root to diagram (expects vendor.toml / vendor.lock).",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=DEFAULT_DEPTH,
        help=(
            f"Recursive walk depth for cloneable Grimoire-managed "
            f"dependencies (default {DEFAULT_DEPTH}; capped at {MAX_DEPTH}). "
            f"depth=1 is single-level (this project's own vendor.toml only, "
            f"no network needed to draw the graph); depth>=2 fetches each "
            f"dependency's own vendor.toml via `gh api`."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the graph as {nodes: [...], edges: [...]} JSON instead of DOT.",
    )
    parser.add_argument(
        "--with-conformance",
        action="store_true",
        help=(
            "Mark pins behind the producer's latest release as stale edges "
            "(network). Without this flag, staleness is never computed and "
            "no extra network call is made just to draw the graph."
        ),
    )
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    return run_diagram(
        args.root, depth=args.depth, with_conformance=args.with_conformance,
        as_json=args.json,
    )


if __name__ == "__main__":
    sys.exit(main())
