#!/usr/bin/env python3
"""vendor_verify.py — Vendor provenance integrity check (DEP-CH, #315).

Implements the `sync_deps.py --verify` mode: a deterministic, read-only pass
that checks whether the *provenance metadata* a consumer trusts (`vendor.toml`
pins, `vendor.lock` resolved truth, an optional `VENDOR.md` front-matter claim)
actually matches the bytes on disk — rather than trusting it blindly.

Three deterministic finding classes plus one WARN-only heuristic (design
`docs/grimoire/design/dependency-channel-design.md` §Provenance verification):

  1. LOCAL-FORK             — the vendored tree drifted from its `vendor.lock`
                               pin (recomputed `tree_sha256` / per-file manifest
                               disagree), reported with a diff summary of which
                               relpaths were added/removed/changed. A `VENDOR.md`
                               front-matter claim ("byte-identical to tag …") is
                               cross-checked when present and folded into the
                               same finding if it contradicts the observed drift.
  2. DEAD-VENDOR            — a `vendor.toml` dep's declared `dest` is missing
                               or contains zero regular files, OR a git
                               submodule (`.gitmodules` entry) is uninitialized
                               / empty on disk.
  3. VERSION-CONTRADICTION  — a version string embedded in the vendored tree
                               itself (a `Cargo.toml` `[package] version`, a
                               `package.json` `"version"`, or a `VENDOR.md`
                               `pinned_version:` front-matter field) disagrees
                               with the `vendor.toml` pin.
  4. STUB-VENDOR-MANIFEST   — WARN-only heuristic: `vendor.toml` is absent or
                               declares zero deps, yet the repo's own source /
                               docs reference vendoring (`vendor/`, `vendor.toml`,
                               a known dep token) elsewhere — the "inert manifest"
                               smell (obsidian's out-of-band Aura reimplementation
                               in the motivating issue). Heuristic; never fails
                               the run on its own.

Everything here is local-filesystem only — zero network calls, so `--self-test`
runs fully offline and deterministically. This module is imported by
`sync_deps.py`'s `--verify` CLI mode; it reuses `sync_deps_engine`'s hashing,
`VendorToml`/`VendorLock` readers, and exit-code constants rather than
re-implementing them (DRY — the two modules must never disagree on what
`tree_sha256` means).

Design: docs/grimoire/design/dependency-channel-design.md §Provenance verification
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sync_deps_engine import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    VendorLock,
    VendorToml,
    sha256_of_file,
    tree_sha256,
)

# ── Finding taxonomy ─────────────────────────────────────────────────────────

CHECK_LOCAL_FORK = "LOCAL-FORK"
CHECK_DEAD_VENDOR = "DEAD-VENDOR"
CHECK_VERSION_CONTRADICTION = "VERSION-CONTRADICTION"
CHECK_STUB_VENDOR_MANIFEST = "STUB-VENDOR-MANIFEST"

SEVERITY_ERROR = "error"  # deterministic — counts toward a nonzero exit
SEVERITY_WARN = "warn"    # heuristic / advisory — never elevates the exit code

# Basenames the version-contradiction check reads a version string from.
_CARGO_TOML = "Cargo.toml"
_PACKAGE_JSON = "package.json"
_VENDOR_MD = "VENDOR.md"

# Directories the stub-heuristic scan never descends into.
_SCAN_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".sync-deps-staging"}
# Source/doc file extensions the stub-heuristic scan considers.
_SCAN_EXTENSIONS = (
    ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".json",
    ".toml", ".yml", ".yaml", ".txt",
)


class VerifyFinding:
    """One normalized provenance finding.

    Shape mirrors `dependency_channel_conformance.Finding` for consistency
    across the two Dependency Channel verifiers:
    `{check, dep, severity, detail, locked, observed}`.
    """

    def __init__(self, check: str, dep: str, severity: str, detail: str,
                 locked: Optional[str] = None, observed: Optional[str] = None) -> None:
        self.check = check
        self.dep = dep
        self.severity = severity
        self.detail = detail
        self.locked = locked
        self.observed = observed

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "dep": self.dep,
            "severity": self.severity,
            "detail": self.detail,
            "locked": self.locked,
            "observed": self.observed,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"VerifyFinding({self.check}:{self.dep}, {self.severity})"


class VerifyResult:
    """Accumulates findings for one `--verify` run."""

    def __init__(self) -> None:
        self.findings: list[VerifyFinding] = []

    def add(self, finding: VerifyFinding) -> None:
        self.findings.append(finding)

    @property
    def errors(self) -> list[VerifyFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[VerifyFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_WARN]

    @property
    def passed(self) -> bool:
        """True with zero ERROR findings — WARN findings never fail the run."""
        return len(self.errors) == 0

    def exit_code(self) -> int:
        return EXIT_OK if self.passed else EXIT_VIOLATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "passed": self.passed,
        }

    def report(self) -> str:
        lines = ["vendor-verify — provenance integrity check"]
        if not self.findings:
            lines.append("  PASS — no findings.")
        for f in self.findings:
            lines.append(f"  {f.severity.upper()}: [{f.check}] dep={f.dep} — {f.detail}")
            if f.locked or f.observed:
                lines.append(f"         locked={f.locked} observed={f.observed}")
        status = "PASS" if self.passed else "FAIL"
        lines.append(
            f"  -> {status} ({len(self.errors)} error(s), {len(self.warnings)} warning(s))"
        )
        return "\n".join(lines)


# ── Filesystem helpers (local-only, zero network) ───────────────────────────

def _walk_files(root: str):
    """Yield (relpath, abspath) for every regular, non-symlink file under root."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            abs_path = os.path.join(dirpath, name)
            if os.path.islink(abs_path) or not os.path.isfile(abs_path):
                continue
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            yield rel, abs_path


def _is_empty_dest(path: str) -> bool:
    """True when `path` is missing, or exists but holds zero regular files."""
    if not os.path.isdir(path):
        return True
    for _rel, _abs in _walk_files(path):
        return False
    return True


def tree_manifest(root: str) -> dict[str, str]:
    """Return {relpath: sha256} for every file under root — the LOCAL-FORK diff basis."""
    return {rel: sha256_of_file(abs_) for rel, abs_ in _walk_files(root)}


def diff_manifests(locked: dict[str, str], observed: dict[str, str]) -> dict[str, list[str]]:
    """Return {added, removed, changed} relpath lists between two manifests."""
    locked_keys = set(locked)
    observed_keys = set(observed)
    added = sorted(observed_keys - locked_keys)
    removed = sorted(locked_keys - observed_keys)
    changed = sorted(
        rel for rel in (locked_keys & observed_keys) if locked[rel] != observed[rel]
    )
    return {"added": added, "removed": removed, "changed": changed}


def _format_diff_summary(diff: dict[str, list[str]], limit: int = 5) -> str:
    parts = []
    for label in ("changed", "added", "removed"):
        paths = diff.get(label) or []
        if not paths:
            continue
        shown = ", ".join(paths[:limit])
        more = f" (+{len(paths) - limit} more)" if len(paths) > limit else ""
        parts.append(f"{len(paths)} {label} [{shown}{more}]")
    return "; ".join(parts) if parts else "no per-file differences (hash-only drift)"


def parse_gitmodules(root: str) -> list[tuple[str, str]]:
    """Return [(path, url), ...] declared in a root-level `.gitmodules`, if any."""
    path = os.path.join(root, ".gitmodules")
    if not os.path.isfile(path):
        return []
    entries: list[tuple[str, str]] = []
    current_path = None
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped.startswith("["):
                current_path = None
                continue
            m = re.match(r"^path\s*=\s*(.+)$", stripped)
            if m:
                current_path = m.group(1).strip()
                continue
            m = re.match(r"^url\s*=\s*(.+)$", stripped)
            if m and current_path is not None:
                entries.append((current_path, m.group(1).strip()))
    return entries


def parse_front_matter(path: str) -> dict[str, str]:
    """Parse a `---\\nkey: value\\n---` front-matter block from a VENDOR.md-style file.

    Tolerant, minimal parser — unknown/missing files return {}. Never raises.
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.+)$", line.strip())
        if kv:
            out[kv.group(1)] = kv.group(2).strip()
    return out


def _extract_cargo_version(dest: str) -> Optional[str]:
    """Return the first `[package] version = "…"` found in any Cargo.toml under dest."""
    for rel, abs_ in _walk_files(dest):
        if os.path.basename(rel) != _CARGO_TOML:
            continue
        try:
            with open(abs_, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        in_package = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_package = (stripped == "[package]")
                continue
            if in_package:
                m = re.match(r'^version\s*=\s*"([^"]+)"', stripped)
                if m:
                    return m.group(1)
        return None
    return None


def _extract_package_json_version(dest: str) -> Optional[str]:
    """Return the top-level `"version"` field of the root `package.json`, if any."""
    path = os.path.join(dest, _PACKAGE_JSON)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    version = data.get("version")
    return str(version) if version is not None else None


def _extract_vendor_md_version(dest: str) -> Optional[str]:
    """Return VENDOR.md's `pinned_version:` front-matter field, if present."""
    front_matter = parse_front_matter(os.path.join(dest, _VENDOR_MD))
    return front_matter.get("pinned_version")


# ── The verifier ─────────────────────────────────────────────────────────────

class VendorVerifier:
    """Runs the four provenance-integrity checks against one repo root."""

    def __init__(self, root: str = ".") -> None:
        self.root = os.path.abspath(root)
        self.toml = VendorToml(os.path.join(self.root, "vendor.toml"))
        self.lock = VendorLock(os.path.join(self.root, "vendor.lock"))

    def _deps(self, only: Optional[str] = None) -> dict[str, Any]:
        if not self.toml.exists():
            return {}
        _schema, deps = self.toml.load()
        if only:
            return {only: deps[only]} if only in deps else {}
        return deps

    def run(self, only: Optional[str] = None) -> VerifyResult:
        result = VerifyResult()
        deps = self._deps(only)
        locked = self.lock.load().get("deps", {}) if self.lock.path else {}

        for name, spec in sorted(deps.items()):
            dest = os.path.join(self.root, spec.dest)
            self._check_dead_vendor(result, name, dest)
            self._check_local_fork(result, name, spec, dest, locked.get(name))
            self._check_version_contradiction(result, name, spec, dest)

        self._check_dead_submodules(result, only)
        self._check_stub_manifest(result, deps)
        return result

    # — check 1: LOCAL-FORK —

    def _check_local_fork(self, result, name, spec, dest, entry) -> None:
        if entry is None or not os.path.isdir(dest) or _is_empty_dest(dest):
            return  # no lock entry / nothing vendored yet — DEAD-VENDOR's territory
        observed_sha = tree_sha256(dest)
        locked_sha = entry.get("tree_sha256")
        if locked_sha is None or observed_sha == locked_sha:
            return
        observed_manifest = tree_manifest(dest)
        locked_manifest = entry.get("tree_manifest") or {}
        diff = diff_manifests(locked_manifest, observed_manifest)
        detail = f"vendored tree drifted from its vendor.lock pin — {_format_diff_summary(diff)}"
        vendor_md_claim = parse_front_matter(os.path.join(dest, _VENDOR_MD)).get("claim")
        if vendor_md_claim:
            detail += f"; VENDOR.md claims {vendor_md_claim!r} (contradicted)"
        result.add(VerifyFinding(
            CHECK_LOCAL_FORK, name, SEVERITY_ERROR, detail,
            locked=locked_sha, observed=observed_sha,
        ))

    # — check 2: DEAD-VENDOR (declared-but-empty vendor.toml dest) —

    def _check_dead_vendor(self, result, name, dest) -> None:
        if _is_empty_dest(dest):
            state = "missing" if not os.path.isdir(dest) else "empty (zero files)"
            result.add(VerifyFinding(
                CHECK_DEAD_VENDOR, name, SEVERITY_ERROR,
                f"declared dest {dest!r} is {state}",
            ))

    # — check 2b: DEAD-VENDOR (uninitialized/empty git submodules) —

    def _check_dead_submodules(self, result, only) -> None:
        for path, _url in parse_gitmodules(self.root):
            if only and only != path and only != os.path.basename(path):
                continue
            abs_path = os.path.join(self.root, path)
            if _is_empty_dest(abs_path):
                state = "missing" if not os.path.isdir(abs_path) else "empty/uninitialized"
                result.add(VerifyFinding(
                    CHECK_DEAD_VENDOR, path, SEVERITY_ERROR,
                    f"git submodule {path!r} is {state} on disk",
                ))

    # — check 3: VERSION-CONTRADICTION —

    def _check_version_contradiction(self, result, name, spec, dest) -> None:
        if _is_empty_dest(dest):
            return  # DEAD-VENDOR already flags this; nothing to contradict
        pinned = spec.version
        sources = {
            "Cargo.toml": _extract_cargo_version(dest),
            "package.json": _extract_package_json_version(dest),
            "VENDOR.md": _extract_vendor_md_version(dest),
        }
        for source_name, found in sources.items():
            if found is not None and found != pinned:
                result.add(VerifyFinding(
                    CHECK_VERSION_CONTRADICTION, name, SEVERITY_ERROR,
                    f"{source_name} declares version {found!r} but vendor.toml "
                    f"pins {pinned!r}",
                    locked=pinned, observed=found,
                ))

    # — check 4: STUB-VENDOR-MANIFEST (WARN-only heuristic) —

    def _check_stub_manifest(self, result, deps) -> None:
        if deps:
            return  # vendor.toml declares at least one dep — not a stub
        hits = self._scan_for_vendor_references()
        if hits:
            shown = ", ".join(hits[:5])
            more = f" (+{len(hits) - 5} more)" if len(hits) > 5 else ""
            result.add(VerifyFinding(
                CHECK_STUB_VENDOR_MANIFEST, "(repo)", SEVERITY_WARN,
                f"vendor.toml is absent/empty of deps, but {len(hits)} file(s) "
                f"reference vendoring elsewhere: {shown}{more}",
            ))

    def _scan_for_vendor_references(self) -> list[str]:
        needles = ("vendor.toml", "vendor/", "lib/third-party/")
        hits = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames
                                  if d not in _SCAN_SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name in ("vendor.toml", "vendor.lock"):
                    continue  # the manifest/lock themselves are not "elsewhere"
                if not name.endswith(_SCAN_EXTENSIONS):
                    continue
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, self.root).replace(os.sep, "/")
                if rel.startswith("vendor/") or rel.startswith("lib/third-party/"):
                    continue  # inside the vendored tree itself, not a reference to it
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="ignore") as fh:
                        text = fh.read()
                except OSError:
                    continue
                if any(needle in text for needle in needles):
                    hits.append(rel)
        return hits


# ── CLI entry point ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vendor_verify.py",
        description="Vendor provenance integrity check (LOCAL-FORK / "
                    "DEAD-VENDOR / VERSION-CONTRADICTION / STUB-VENDOR-MANIFEST).",
    )
    p.add_argument("--root", default=".", help="Repository root (default: cwd).")
    p.add_argument("--dep", default=None, help="Verify only this dependency.")
    p.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    p.add_argument("--self-test", action="store_true",
                   help="Run the deterministic offline self-test and exit.")
    args = p.parse_args(argv)

    if args.self_test:
        ok = run_self_test()
        print("vendor_verify self-test: PASS" if ok else "vendor_verify self-test: FAIL")
        return EXIT_OK if ok else EXIT_VIOLATION

    verifier = VendorVerifier(root=args.root)
    result = verifier.run(only=args.dep)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(result.report())
    return result.exit_code()


# ── Self-test (deterministic, stdlib-only, offline) ─────────────────────────

def run_self_test() -> bool:
    failures: list[str] = []

    def check(cond, label):
        if cond:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}")
            failures.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1) LOCAL-FORK: vendored bytes drift from the vendor.lock tree_sha256,
        #      with a VENDOR.md front-matter claim the observed drift contradicts.
        repo1 = os.path.join(tmp, "repo1")
        dest1 = os.path.join(repo1, "vendor", "feed-engine")
        os.makedirs(dest1)
        with open(os.path.join(dest1, "Cargo.toml"), "w") as fh:
            fh.write('[package]\nname = "feed-engine"\nversion = "0.2.0"\n')
        with open(os.path.join(dest1, "VENDOR.md"), "w") as fh:
            fh.write("---\nclaim: byte-identical to tag v0.2.0\n---\nFeed engine.\n")
        _write_vendor_toml(repo1, name="feed-engine", version="0.2.0",
                            dest="vendor/feed-engine")
        locked_sha = tree_sha256(dest1)
        _write_lock(repo1, {"feed-engine": {
            "version": "0.2.0", "tree_sha256": locked_sha,
            "tree_manifest": tree_manifest(dest1),
        }})
        # Now the tree evolves in-place (undocumented module + bumped version).
        with open(os.path.join(dest1, "blend.rs"), "w") as fh:
            fh.write("pub fn blend() {}\n")
        with open(os.path.join(dest1, "Cargo.toml"), "w") as fh:
            fh.write('[package]\nname = "feed-engine"\nversion = "0.4.0"\n')

        result1 = VendorVerifier(repo1).run()
        forks = [f for f in result1.findings if f.check == CHECK_LOCAL_FORK]
        check(len(forks) == 1, "LOCAL-FORK detected on a drifted vendored tree")
        check(forks and "blend.rs" in forks[0].detail, "LOCAL-FORK diff summary names the new file")
        check(forks and forks[0].severity == SEVERITY_ERROR, "LOCAL-FORK is ERROR severity")
        contradictions = [f for f in result1.findings if f.check == CHECK_VERSION_CONTRADICTION]
        check(len(contradictions) == 1, "VERSION-CONTRADICTION detected alongside the fork")
        check(contradictions and contradictions[0].observed == "0.4.0",
              "VERSION-CONTRADICTION reports the observed Cargo.toml version")
        check(not result1.passed, "run() fails (nonzero-worthy) on LOCAL-FORK + VERSION-CONTRADICTION")

        # ── 2) DEAD-VENDOR: an empty declared dest + an uninitialized submodule.
        repo2 = os.path.join(tmp, "repo2")
        os.makedirs(os.path.join(repo2, "vendor", "empty-dep"))  # dir exists, zero files
        _write_vendor_toml(repo2, name="empty-dep", version="1.0.0",
                            dest="vendor/empty-dep")
        with open(os.path.join(repo2, ".gitmodules"), "w") as fh:
            fh.write('[submodule "lib/third-party/aura"]\n'
                     '\tpath = lib/third-party/aura\n'
                     '\turl = https://github.com/example/aura.git\n')
        os.makedirs(os.path.join(repo2, "lib", "third-party", "aura"))  # empty on disk

        result2 = VendorVerifier(repo2).run()
        dead = {f.dep: f for f in result2.findings if f.check == CHECK_DEAD_VENDOR}
        check("empty-dep" in dead, "DEAD-VENDOR flags an empty declared vendor.toml dest")
        check("lib/third-party/aura" in dead,
              "DEAD-VENDOR flags an uninitialized/empty git submodule")
        check(not result2.passed, "run() fails on DEAD-VENDOR findings")

        # ── 3) Healthy consumer — zero false positives.
        repo3 = os.path.join(tmp, "repo3")
        dest3 = os.path.join(repo3, "vendor", "widget")
        os.makedirs(dest3)
        with open(os.path.join(dest3, "README.md"), "w") as fh:
            fh.write("widget\n")
        _write_vendor_toml(repo3, name="widget", version="1.0.0", dest="vendor/widget")
        healthy_sha = tree_sha256(dest3)
        _write_lock(repo3, {"widget": {
            "version": "1.0.0", "tree_sha256": healthy_sha,
            "tree_manifest": tree_manifest(dest3),
        }})
        result3 = VendorVerifier(repo3).run()
        check(result3.passed, "healthy consumer: zero ERROR findings")
        check(not result3.findings, "healthy consumer: zero findings of any severity")

        # ── 4) STUB-VENDOR-MANIFEST: vendor.toml declares zero deps, but a doc
        #      elsewhere references vendoring (the obsidian "inert manifest" case).
        repo4 = os.path.join(tmp, "repo4")
        os.makedirs(os.path.join(repo4, "docs"))
        with open(os.path.join(repo4, "vendor.toml"), "w") as fh:
            fh.write("schema_version = 1\n")  # no [deps.*] blocks at all
        with open(os.path.join(repo4, "docs", "aura-notes.md"), "w") as fh:
            fh.write("We reimplement Aura from a committed codegen; see vendor/aura "
                      "for the old approach we moved away from.\n")

        result4 = VendorVerifier(repo4).run()
        stubs = [f for f in result4.findings if f.check == CHECK_STUB_VENDOR_MANIFEST]
        check(len(stubs) == 1, "STUB-VENDOR-MANIFEST fires on an all-stub vendor.toml "
                               "with dep references elsewhere")
        check(stubs and stubs[0].severity == SEVERITY_WARN,
              "STUB-VENDOR-MANIFEST is WARN-only")
        check(result4.passed, "STUB-VENDOR-MANIFEST alone never fails the run (WARN-only)")

        # ── 5) No vendor.toml at all — quiet, not an error (nothing declared).
        repo5 = os.path.join(tmp, "repo5")
        os.makedirs(repo5)
        result5 = VendorVerifier(repo5).run()
        check(result5.passed and not result5.findings,
              "absent vendor.toml + no vendor references: quiet pass")

    print(f"\n{len(failures)} failure(s)." if failures else "\nall checks passed.")
    return not failures


def _write_vendor_toml(root, name, version, dest, kind="asset-bundle"):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "vendor.toml"), "w", encoding="utf-8") as fh:
        fh.write(
            "schema_version = 1\n\n"
            f"[deps.{name}]\n"
            f'repo = "acme/{name}"\n'
            'channel = "stable"\n'
            f'version = "{version}"\n'
            f'artifact = "{name}-v{version}.tar.gz"\n'
            f'dest = "{dest}"\n'
            f'kind = "{kind}"\n'
        )


def _write_lock(root, deps):
    with open(os.path.join(root, "vendor.lock"), "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 1, "deps": deps}, fh, indent=2, sort_keys=True)


if __name__ == "__main__":
    sys.exit(main())
