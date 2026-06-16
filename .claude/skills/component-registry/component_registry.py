#!/usr/bin/env python3
"""component_registry.py — deterministic engine for the versioned component registry (#REG-1, v3.28).

Backs the `component-registry` skill (build/update `.claude/component-registry.json`)
and the `component-catalog-export` skill (which renders a flat catalog view over
the registry when it is present). Both skills reason about component identity,
content hashing, taxonomy validation, and the registry diff in free-form prose
today; this engine replaces that prose-driven LLM arithmetic with one tested,
token-cheap, deterministic implementation.

Documented behavior this engine CODIFIES (it invents no new semantics):
  - Discovery (component-registry SKILL.md Step 2 / component-catalog-export
    Step 2): a `component.json` at a component root, OR YAML front-matter with a
    `component:` block in a single-file component. Reusable units without
    metadata are recorded under `uncataloged`, never silently dropped.
  - Metadata fields (design §2.1): id, summary, profiles, provides, requires,
    compat, stability, source.
  - Versioning (Step 3): declared `version` verbatim (version-source=declared),
    else a sha256 content-hash of the normalized metadata entry
    (version-source=content-hash) as `sha256:<hex>`.
  - Taxonomy validation (Step 4 + component-taxonomy.md §5): every profiles /
    provides / requires tag is checked against the vocabulary read live from
    docs/design/component-taxonomy.md (§2 profiles, §3 capabilities). An unknown
    tag is SURFACED under `unknown-tags` and kept in the entry — neither silently
    accepted into a clean entry nor silently dropped.
  - Registry object (Step 5) + diff vs prior (Step 6): added/changed/removed/
    unchanged by id + version.
  - Idempotent write (Step 7): sorted keys, fixed indent, trailing newline; the
    build id (`last-seen`) is derived from the components content hash, NOT
    wall-clock time, so an unchanged source re-run is byte-identical (a no-op).

File-write contract: writes ONLY `.claude/component-registry.json`, atomically
(temp + os.replace). Never runs git, never mutates component code. The agent
commits. Design: docs/design/scripting-unification-design.md §5 +
docs/design/component-catalog-architecture-design.md (Pillars 1+2) +
docs/design/mcp-expansion-audit.md rank 3.

Standard: Python 3 stdlib-only (docs/design/scripting-unification-design.md §3).

CLI:
  component_registry.py build   [--root DIR] [--stdout]
  component_registry.py dry-run [--root DIR] [--stdout]   # compute, never write
  component_registry.py --self-test
Exit 0 on success; 2 on a build error (unreadable source, malformed metadata).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

# ── Constants (no magic numbers / strings inline) ───────────────────────────
REGISTRY_PATH = os.path.join(".claude", "component-registry.json")
TAXONOMY_PATH = os.path.join("docs", "design", "component-taxonomy.md")
CONFIG_PATH = os.path.join(".claude", "grimoire-config.json")
DEFAULT_SCAN_PATHS = ("components/", "lib/")
REGISTRY_VERSION = 1
JSON_INDENT = 2
HASH_PREFIX = "sha256:"
BUILD_ID_PREFIX = "build-"
# Metadata fields parsed from a component source (design §2.1).
META_FIELDS = ("id", "summary", "profiles", "provides", "requires",
               "compat", "stability", "source")
# Fields whose tags are taxonomy-validated, mapped to their vocabulary set.
PROFILE_FIELDS = ("profiles",)
CAPABILITY_FIELDS = ("provides", "requires")
# Taxonomy section markers in component-taxonomy.md.
PROFILES_SECTION_RE = re.compile(r"^##\s+2\.\s", re.MULTILINE)
CAPABILITY_SECTION_RE = re.compile(r"^##\s+3\.\s", re.MULTILINE)
NEXT_SECTION_RE = re.compile(r"^##\s+\d+\.\s", re.MULTILINE)
# A vocabulary row: `| \`term\` | meaning |`.
TAXONOMY_TERM_RE = re.compile(r"^\s*\|\s*`([a-z][a-z0-9-]*)`\s*\|")
# Front-matter fence + a `component:` block inside it.
FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class RegistryError(Exception):
    """Raised on an unreadable source or malformed metadata (→ exit 2)."""


# ── Taxonomy authority ──────────────────────────────────────────────────────
class Taxonomy:
    """Allowed-term sets read live from docs/design/component-taxonomy.md.

    §2 lists the `profiles` vocabulary; §3 the shared provides/requires
    capability vocabulary. Reading the doc at build time (not a hard-coded
    table) keeps the vocabulary extensible per the doc's §4 contract — adding a
    row to the doc is recognized on the next build with no script change.
    """

    def __init__(self, profiles, capabilities):
        self.profiles = profiles
        self.capabilities = capabilities

    @classmethod
    def from_text(cls, text):
        profiles = cls._terms_in_section(text, PROFILES_SECTION_RE)
        capabilities = cls._terms_in_section(text, CAPABILITY_SECTION_RE)
        return cls(profiles, capabilities)

    @classmethod
    def load(cls, root="."):
        path = os.path.join(root, TAXONOMY_PATH)
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_text(fh.read())
        except OSError as exc:
            raise RegistryError("taxonomy unreadable: %s" % exc)

    @staticmethod
    def _terms_in_section(text, start_re):
        m = start_re.search(text)
        if not m:
            return set()
        rest = text[m.end():]
        nxt = NEXT_SECTION_RE.search(rest)
        body = rest[:nxt.start()] if nxt else rest
        terms = set()
        for line in body.splitlines():
            tm = TAXONOMY_TERM_RE.match(line)
            if tm:
                terms.add(tm.group(1))
        return terms

    def allowed_for(self, field):
        if field in PROFILE_FIELDS:
            return self.profiles
        if field in CAPABILITY_FIELDS:
            return self.capabilities
        return None


# ── Component metadata model ────────────────────────────────────────────────
class Component:
    """One discovered component's normalized metadata + version assignment."""

    def __init__(self, meta, source):
        self.meta = meta
        self.source = source
        self.version = None
        self.version_source = None

    @property
    def id(self):
        return self.meta.get("id")

    def normalized(self):
        """Canonical, key-sorted dict of the design §2.1 fields (minus version).

        This is the entry the content hash is taken over and the entry written
        to the registry (with `version`, `version-source`, `last-seen` layered
        on by the builder). Absent optional fields are omitted so the shape is
        stable regardless of source ordering.
        """
        out = {}
        for field in META_FIELDS:
            if field == "source":
                out["source"] = self.source
                continue
            if field in self.meta and self.meta[field] not in (None, "", [], {}):
                out[field] = self.meta[field]
        return out

    def content_hash(self):
        """sha256 of the canonical-JSON of the normalized entry (key-sorted)."""
        canonical = json.dumps(self.normalized(), sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return HASH_PREFIX + digest

    def assign_version(self):
        """Prefer a declared version; else a content hash (design Step 3)."""
        declared = self.meta.get("version")
        if declared:
            self.version = str(declared)
            self.version_source = "declared"
        else:
            self.version = self.content_hash()
            self.version_source = "content-hash"


# ── Discovery (shared with component-catalog-export) ────────────────────────
class Discovery:
    """Walk the scan paths and surface components + uncataloged units.

    Reuses the documented discovery contract (a `component.json` at a root, or
    front-matter with a `component:` block) — it does NOT invent a second
    scanner, per the anti-pattern note in both SKILL.mds.
    """

    COMPONENT_JSON = "component.json"
    SINGLE_FILE_EXTS = (".py", ".ts", ".js", ".md")

    def __init__(self, root=".", scan_paths=None):
        self.root = root
        self.scan_paths = list(scan_paths or DEFAULT_SCAN_PATHS)

    def resolve_paths(self):
        """Return (existing, skipped) scan paths (config-extended)."""
        existing, skipped = [], []
        for rel in self.scan_paths:
            if os.path.isdir(os.path.join(self.root, rel)):
                existing.append(rel)
            else:
                skipped.append(rel)
        return existing, skipped

    def discover(self):
        """Return (components, uncataloged) — deterministic, path-sorted."""
        existing, _ = self.resolve_paths()
        components, uncataloged = [], []
        seen_dirs = set()
        for rel in existing:
            base = os.path.join(self.root, rel)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames.sort()
                # (a) component.json at a directory root.
                if self.COMPONENT_JSON in filenames:
                    src = self._relsource(dirpath)
                    components.append(self._from_json(
                        os.path.join(dirpath, self.COMPONENT_JSON), src))
                    seen_dirs.add(dirpath)
                    dirnames[:] = []  # do not descend into a component root
                    continue
                # (b) single-file components with a `component:` front-matter.
                for fn in sorted(filenames):
                    if not fn.endswith(self.SINGLE_FILE_EXTS):
                        continue
                    fpath = os.path.join(dirpath, fn)
                    comp = self._from_front_matter(fpath)
                    if comp is not None:
                        components.append(comp)
        # Uncataloged: top-level entries under a scan path with no metadata.
        for rel in existing:
            base = os.path.join(self.root, rel)
            for entry in sorted(os.listdir(base)):
                full = os.path.join(base, entry)
                if os.path.isdir(full) and full not in seen_dirs \
                        and not self._dir_has_metadata(full):
                    uncataloged.append(self._relsource(full))
        components.sort(key=lambda c: (c.id or "", c.source))
        uncataloged.sort()
        return components, uncataloged

    def _dir_has_metadata(self, dirpath):
        for dp, _dn, fns in os.walk(dirpath):
            if self.COMPONENT_JSON in fns:
                return True
            for fn in fns:
                if fn.endswith(self.SINGLE_FILE_EXTS) and \
                        self._has_component_front_matter(os.path.join(dp, fn)):
                    return True
        return False

    def _relsource(self, dirpath):
        rel = os.path.relpath(dirpath, self.root)
        return rel.replace(os.sep, "/") + "/"

    def _from_json(self, path, source):
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError) as exc:
            raise RegistryError("bad %s: %s" % (path, exc))
        if not isinstance(meta, dict):
            raise RegistryError("%s is not a JSON object" % path)
        return Component(meta, meta.get("source") or source)

    def _read_front_matter(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                head = fh.read(8192)
        except OSError:
            return None
        m = FRONT_MATTER_RE.match(head)
        return m.group(1) if m else None

    def _has_component_front_matter(self, path):
        fm = self._read_front_matter(path)
        return bool(fm) and re.search(r"^component\s*:", fm, re.MULTILINE)

    def _from_front_matter(self, path):
        fm = self._read_front_matter(path)
        if not fm or not re.search(r"^component\s*:", fm, re.MULTILINE):
            return None
        meta = _parse_component_block(fm)
        if not meta:
            return None
        src = meta.get("source") or \
            os.path.relpath(path, self.root).replace(os.sep, "/")
        return Component(meta, src)


def _parse_component_block(front_matter):
    """Minimal stdlib parser for the `component:` mapping in front-matter.

    Supports the documented field shapes without a YAML dependency: scalar
    values, inline `[a, b]` lists, and `compat:` nested mappings (incl. inline
    list values). Deliberately conservative — anything it can't parse is left
    out so a malformed source degrades to uncataloged rather than crashing.
    """
    lines = front_matter.splitlines()
    # Find the `component:` block and its indented body.
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^component\s*:", line):
            start = i
            break
    if start is None:
        return {}
    body = []
    for line in lines[start + 1:]:
        if line.strip() == "":
            continue
        if not line.startswith((" ", "\t")):
            break
        body.append(line)
    return _parse_mapping(body, base_indent=_indent(body[0]) if body else 0)


def _indent(line):
    return len(line) - len(line.lstrip())


def _parse_scalar(raw):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_unquote(p.strip()) for p in inner.split(",") if p.strip()]
    return _unquote(raw)


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_mapping(lines, base_indent):
    """Parse a flat/one-level-nested mapping at base_indent into a dict."""
    out = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if _indent(line) != base_indent or ":" not in line:
            i += 1
            continue
        key, _, rest = line.strip().partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            # Nested block or list — gather deeper-indented children.
            child, consumed = _gather_children(lines[i + 1:], base_indent)
            out[key] = child
            i += 1 + consumed
        else:
            out[key] = _parse_scalar(rest)
            i += 1
    return out


def _gather_children(lines, parent_indent):
    """Return (value, lines_consumed) for a nested block under a key."""
    child_lines = []
    for line in lines:
        if line.strip() == "":
            child_lines.append(line)
            continue
        if _indent(line) <= parent_indent:
            break
        child_lines.append(line)
    non_blank = [ln for ln in child_lines if ln.strip()]
    if non_blank and non_blank[0].strip().startswith("- "):
        items = [_unquote(ln.strip()[2:].strip())
                 for ln in non_blank if ln.strip().startswith("- ")]
        return items, len(child_lines)
    if non_blank:
        return _parse_mapping(non_blank, _indent(non_blank[0])), len(child_lines)
    return {}, len(child_lines)


# ── Taxonomy validation ─────────────────────────────────────────────────────
class TaxonomyValidator:
    """Check each component's tags against the taxonomy; surface unknowns."""

    def __init__(self, taxonomy):
        self.taxonomy = taxonomy

    def validate(self, component):
        """Return a sorted list of unknown-tag records for one component."""
        unknown = []
        for field in PROFILE_FIELDS + CAPABILITY_FIELDS:
            allowed = self.taxonomy.allowed_for(field)
            if allowed is None:
                continue
            for tag in component.meta.get(field, []) or []:
                if tag not in allowed:
                    unknown.append({"component": component.id,
                                    "field": field, "tag": tag})
        return unknown


# ── Registry builder ────────────────────────────────────────────────────────
class RegistryBuilder:
    """Assemble the registry object from discovered components + taxonomy."""

    def __init__(self, root=".", scan_paths=None, taxonomy=None):
        self.root = root
        self.discovery = Discovery(root, scan_paths)
        self.taxonomy = taxonomy or Taxonomy.load(root)
        self.validator = TaxonomyValidator(self.taxonomy)

    def build(self):
        """Return the registry dict (not yet serialized)."""
        components, uncataloged = self.discovery.discover()
        existing, skipped = self.discovery.resolve_paths()
        entries = {}
        unknown_tags = []
        for comp in components:
            if not comp.id:
                # No id == not a real component declaration -> uncataloged.
                uncataloged.append(comp.source)
                continue
            comp.assign_version()
            unknown_tags.extend(self.validator.validate(comp))
            entries[comp.id] = self._entry(comp)
        # Derive a content-based build id so a re-run is byte-identical.
        last_seen = self._build_id(entries)
        for entry in entries.values():
            entry["last-seen"] = last_seen
        registry = {
            "registry-version": REGISTRY_VERSION,
            "generated-from": existing,
            "components": entries,
            "uncataloged": sorted(set(uncataloged)),
            "unknown-tags": _sorted_unknown(unknown_tags),
        }
        if skipped:
            registry["paths-skipped"] = skipped
        return registry

    def _entry(self, comp):
        entry = comp.normalized()
        entry["version"] = comp.version
        entry["version-source"] = comp.version_source
        return entry

    @staticmethod
    def _build_id(entries):
        """build-<short hash of the components object> (content, not clock)."""
        canonical = json.dumps(entries, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        short = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return BUILD_ID_PREFIX + short


def _sorted_unknown(records):
    return sorted(records, key=lambda r: (r["component"] or "",
                                          r["field"], r["tag"]))


# ── Registry diff (Step 6) ──────────────────────────────────────────────────
class RegistryDiff:
    """Classify components by id + version against a prior registry."""

    @staticmethod
    def compute(prior, current):
        prior_c = (prior or {}).get("components", {})
        curr_c = (current or {}).get("components", {})
        added, removed, changed, unchanged = [], [], [], []
        for cid, entry in curr_c.items():
            if cid not in prior_c:
                added.append(cid)
            elif prior_c[cid].get("version") != entry.get("version"):
                changed.append(cid)
            else:
                unchanged.append(cid)
        for cid in prior_c:
            if cid not in curr_c:
                removed.append(cid)
        return {"added": sorted(added), "removed": sorted(removed),
                "changed": sorted(changed), "unchanged": sorted(unchanged)}


# ── Serialization + atomic, idempotent write ────────────────────────────────
def serialize(registry):
    """Deterministic JSON: sorted keys, fixed indent, single trailing newline."""
    return json.dumps(registry, sort_keys=True, indent=JSON_INDENT,
                      ensure_ascii=False) + "\n"


class RegistryWriter:
    """Write `.claude/component-registry.json` atomically + idempotently."""

    def __init__(self, root="."):
        self.path = os.path.join(root, REGISTRY_PATH)

    def load_prior(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def write_if_changed(self, registry):
        """Write only when the serialized form differs. Returns True if written."""
        payload = serialize(registry)
        try:
            with open(self.path, encoding="utf-8") as fh:
                if fh.read() == payload:
                    return False  # byte-identical -> no-op
        except OSError:
            pass
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, self.path)
        return True


# ── Config-aware scan-path resolution ───────────────────────────────────────
def resolve_scan_paths(root="."):
    """Default paths plus any `.claude/grimoire-config.json` extras (dedup)."""
    paths = list(DEFAULT_SCAN_PATHS)
    cfg = os.path.join(root, CONFIG_PATH)
    try:
        with open(cfg, encoding="utf-8") as fh:
            extra = (json.load(fh).get("component-catalog") or {}).get("paths")
    except (OSError, ValueError):
        extra = None
    if isinstance(extra, list):
        for p in extra:
            norm = p if p.endswith("/") else p + "/"
            if norm not in paths:
                paths.append(norm)
    return paths


# ── Engine facade (one entry point for the CLI) ─────────────────────────────
class RegistryEngine:
    """Build the registry and write it (or dry-run) against `root`."""

    def __init__(self, root="."):
        self.root = root
        self.writer = RegistryWriter(root)

    def _registry_and_diff(self):
        builder = RegistryBuilder(self.root, resolve_scan_paths(self.root))
        registry = builder.build()
        diff = RegistryDiff.compute(self.writer.load_prior(), registry)
        return registry, diff

    def build(self, write=True):
        registry, diff = self._registry_and_diff()
        written = self.writer.write_if_changed(registry) if write else False
        return {"registry": registry, "diff": diff,
                "written": written, "dry_run": not write}


# ── Self-test (fixtures in a temp tree; never the repo's real sources) ───────
def _fixture_taxonomy():
    return """# Component taxonomy

## 2. `profiles`

| Term | Meaning |
|---|---|
| `api` | api. |
| `service` | service. |
| `cli` | cli. |
| `lib` | lib. |

## 3. `provides` / `requires`

| Term | Meaning |
|---|---|
| `auth` | auth. |
| `http-server` | http server. |
| `persistence` | persistence. |

## 4. Adding a term

text
"""


def _self_test():
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as root:
        # taxonomy authority.
        os.makedirs(os.path.join(root, "docs", "design"))
        with open(os.path.join(root, TAXONOMY_PATH), "w", encoding="utf-8") as fh:
            fh.write(_fixture_taxonomy())

        # component A: component.json with a DECLARED version + clean tags.
        a_dir = os.path.join(root, "components", "auth-jwt")
        os.makedirs(a_dir)
        with open(os.path.join(a_dir, "component.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "auth-jwt", "version": "v1.2.0",
                       "summary": "JWT auth.", "profiles": ["api", "service"],
                       "provides": ["auth"], "requires": ["http-server"],
                       "compat": {"language": ["python"]}, "stability": "stable",
                       "source": "components/auth-jwt/"}, fh)

        # component B: component.json, NO version (content-hash) + an UNKNOWN tag.
        b_dir = os.path.join(root, "components", "store")
        os.makedirs(b_dir)
        with open(os.path.join(b_dir, "component.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "store", "summary": "Storage.",
                       "profiles": ["lib"], "provides": ["persistence"],
                       "requires": ["frobnicate"],  # unknown capability
                       "stability": "beta"}, fh)

        # component C: single-file front-matter component.
        os.makedirs(os.path.join(root, "lib"))
        with open(os.path.join(root, "lib", "timer.py"), "w", encoding="utf-8") as fh:
            fh.write("---\ncomponent:\n  id: timer\n  summary: A timer.\n"
                     "  profiles: [cli]\n  provides: [telemetry]\n"
                     "  stability: experimental\n---\nprint('hi')\n")

        # an UNCATALOGED dir (no metadata).
        os.makedirs(os.path.join(root, "lib", "legacy"))
        with open(os.path.join(root, "lib", "legacy", "x.py"), "w") as fh:
            fh.write("x = 1\n")

        eng = RegistryEngine(root)

        # 1) IDENTITY: discovery finds the three components + the uncataloged dir.
        res = eng.build(write=False)
        reg = res["registry"]
        comps = reg["components"]
        if set(comps) != {"auth-jwt", "store", "timer"}:
            failures.append("identity: discovered %r" % sorted(comps))
        if "lib/legacy/" not in reg["uncataloged"]:
            failures.append("uncataloged missing legacy: %r" % reg["uncataloged"])

        # 2) VERSIONING: declared verbatim; missing -> sha256 content-hash.
        if comps["auth-jwt"]["version"] != "v1.2.0" or \
                comps["auth-jwt"]["version-source"] != "declared":
            failures.append("declared version wrong: %r" % comps["auth-jwt"])
        sv = comps["store"]["version"]
        if not sv.startswith(HASH_PREFIX) or len(sv) != len(HASH_PREFIX) + 64:
            failures.append("content-hash version malformed: %r" % sv)
        if comps["store"]["version-source"] != "content-hash":
            failures.append("store version-source: %r" % comps["store"])

        # 3) HASH STABILITY: same source -> identical hash across builds.
        sv2 = RegistryEngine(root).build(write=False)["registry"][
            "components"]["store"]["version"]
        if sv != sv2:
            failures.append("content-hash not stable: %r vs %r" % (sv, sv2))

        # 4) TAXONOMY REJECT: the unknown 'frobnicate' tag is surfaced + kept.
        unknown = reg["unknown-tags"]
        hit = [u for u in unknown if u["tag"] == "frobnicate"]
        if not hit or hit[0]["field"] != "requires" or hit[0]["component"] != "store":
            failures.append("unknown tag not surfaced: %r" % unknown)
        if "telemetry" not in str(comps["timer"]):
            failures.append("timer telemetry tag dropped")
        # telemetry isn't in the fixture taxonomy -> must also be surfaced.
        if not any(u["tag"] == "telemetry" for u in unknown):
            failures.append("unknown telemetry not surfaced: %r" % unknown)
        # the offending tag is RETAINED in the component entry, not dropped.
        if "frobnicate" not in comps["store"]["requires"]:
            failures.append("unknown tag dropped from entry: %r" % comps["store"])

        # 5) IDEMPOTENCE: a real write then a second build is byte-identical.
        first = eng.build(write=True)
        if not first["written"]:
            failures.append("first build should write")
        on_disk_1 = _read(eng.writer.path)
        second = RegistryEngine(root).build(write=True)
        if second["written"]:
            failures.append("second build must be a no-op (byte-identical)")
        on_disk_2 = _read(eng.writer.path)
        if on_disk_1 != on_disk_2:
            failures.append("second run produced a diff (not idempotent)")
        # serialized form ends in exactly one trailing newline.
        if not on_disk_1.endswith("\n") or on_disk_1.endswith("\n\n"):
            failures.append("trailing-newline discipline violated")
        # build id is content-derived (build-<hash>), not a timestamp.
        first_reg = first["registry"]["components"]["auth-jwt"]
        if not first_reg["last-seen"].startswith(BUILD_ID_PREFIX):
            failures.append("build id not content-derived: %r" % first_reg)

        # 6) DIFF: re-build after bumping a declared version -> 'changed'.
        with open(os.path.join(a_dir, "component.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "auth-jwt", "version": "v1.3.0",
                       "summary": "JWT auth.", "profiles": ["api"],
                       "provides": ["auth"], "stability": "stable",
                       "source": "components/auth-jwt/"}, fh)
        bumped = RegistryEngine(root).build(write=False)
        if "auth-jwt" not in bumped["diff"]["changed"]:
            failures.append("diff did not flag changed: %r" % bumped["diff"])
        if "store" not in bumped["diff"]["unchanged"]:
            failures.append("diff unchanged wrong: %r" % bumped["diff"])

        # 7) DRY-RUN never writes.
        before = _read(eng.writer.path)
        RegistryEngine(root).build(write=False)
        if _read(eng.writer.path) != before:
            failures.append("dry-run mutated the registry file")

    # 8) malformed component.json raises RegistryError (exit 2 path).
    with tempfile.TemporaryDirectory() as root2:
        os.makedirs(os.path.join(root2, "docs", "design"))
        with open(os.path.join(root2, TAXONOMY_PATH), "w", encoding="utf-8") as fh:
            fh.write(_fixture_taxonomy())
        bad = os.path.join(root2, "components", "broken")
        os.makedirs(bad)
        with open(os.path.join(bad, "component.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        try:
            RegistryEngine(root2).build(write=False)
            failures.append("malformed component.json should raise")
        except RegistryError:
            pass

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("component_registry self-test: OK (identity/discovery + uncataloged, "
          "declared + sha256 content-hash versioning, hash stability, taxonomy "
          "reject surfaced-and-retained, byte-identical idempotent write + "
          "trailing-newline + content-derived build id, added/changed/unchanged "
          "diff, dry-run no-write, malformed-source raise)")
    return 0


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Deterministic component-registry build engine (#REG-1).")
    ap.add_argument("verb", nargs="?", help="build|dry-run")
    ap.add_argument("--root", default=".")
    ap.add_argument("--stdout", action="store_true",
                    help="print the registry JSON to stdout")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.verb:
        ap.error("a verb is required (build|dry-run) or --self-test")

    if args.verb not in ("build", "dry-run"):
        ap.error("unknown verb: %s" % args.verb)
    write = args.verb == "build"

    try:
        result = RegistryEngine(args.root).build(write=write)
    except RegistryError as exc:
        print("component_registry: %s" % exc, file=sys.stderr)
        return 2

    summary = {"diff": result["diff"], "written": result["written"],
               "dry_run": result["dry_run"],
               "unknown-tags": result["registry"]["unknown-tags"],
               "uncataloged": result["registry"]["uncataloged"]}
    if args.stdout:
        summary["registry"] = result["registry"]
    print(json.dumps(summary, indent=JSON_INDENT, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
