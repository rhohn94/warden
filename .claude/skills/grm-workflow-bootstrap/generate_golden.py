#!/usr/bin/env python3
"""Generate the golden image from a flavor tree.

The **golden image** is the pristine, version-pinned reference copy of the files
Grimoire delivers to a scaffolded project, in the layout they occupy once
installed. It is the authority that drift-detection compares against and that
restore/reset operations copy from. It is **derived** from a flavor by this
generator and identified by version — never hand-edited.

Two triggers produce a golden archive:
  - **release**   — `build_distributables.py` runs the generator and attaches the
                    versioned `golden-v{X.Y}.tar.gz` to the GitHub Release.
  - **bootstrap** — a fresh install runs the generator against its just-installed
                    pristine flavor files to produce a local restore baseline
                    under `.grimoire-golden/`. No network needed.

This replaces the former committed `grm-workflow-bootstrap/golden/` tree: the
golden is no longer a static, hand-snapshotted artifact in source control.

Derivation contract
-------------------
golden = the flavor (e.g. `claude-code/`) with:
  1. the `.claude/` wrapper flattened (`.claude/skills/` -> `skills/`, etc.);
  2. the authoring-only skills dropped (cannot run in a scaffolded project);
  3. junk stripped (`__pycache__/`, `*.pyc`, `.DS_Store`, empty stale dirs);
  4. per-project config and flavor/example markers dropped; and
  5. a small operational seed supplement (`seed/`) added for files the flavor
     snapshot does not carry (version-history seed, vendor.toml, upstream conf).

The flavor is already genericized for distribution (placeholder-laden, sentinel
armed), so no genericization step is performed here.

Stdlib-only. Run `--self-test` to verify the transform against tempdir fixtures.
"""

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

# Skills that build/maintain Grimoire itself — never delivered to a scaffolded
# project, so excluded from golden. (grm-workflow-bootstrap formerly carried the
# golden tree, which is the recursion this whole change removes.)
AUTHORING_SKILLS = frozenset({
    "grm-files-manifest",
    "grm-regenerate-grimoire",
    "grm-sync-from-source",
    "grm-workflow-bootstrap",
    "grm-workflow-snapshot",
})

# Files that map under the layout but must not seed a fresh project:
#   grimoire-config.json          — per-project; onboarding writes it fresh
#   .grimoire-flavor              — marks a distribution flavor, not a scaffold
#   architecture-rules.example.json — example-only, opt-in
EXCLUDE_REPO_FILES = frozenset({
    ".claude/grimoire-config.json",
    ".grimoire-flavor",
    ".claude/architecture-rules.example.json",
})

# Junk never included regardless of where it sits.
JUNK_NAMES = frozenset({".DS_Store"})
JUNK_DIR_NAMES = frozenset({"__pycache__"})


class FlavorLayout:
    """Maps a flavor repo-relative path to its golden-relative destination.

    The golden tree is flat: framework dirs sit directly under the golden root
    and a handful of top-level files are remapped. This is the inverse of the
    restore mapping in grm-regenerate-grimoire's GoldenLayout; the two are sibling
    authoring-side authorities (neither ships inside golden).

        .claude/skills/<n>/...  -> skills/<n>/...
        .claude/hooks/x.sh      -> hooks/x.sh
        .claude/settings.json   -> settings.json
        .mcp.json               -> mcp.json
        CLAUDE.md               -> CLAUDE.md
        docs/...                -> docs/...
    """

    # repo-relative dir prefix -> golden top-level dir
    DIR_MAP = {
        ".claude/hooks": "hooks",
        ".claude/skills": "skills",
        ".claude/paradigms": "paradigms",
        ".claude/mcp-servers": "mcp-servers",
        ".claude/workflows": "workflows",
        ".claude/stealth": "stealth",
        ".claude/quick-start-templates": "quick-start-templates",
        "docs": "docs",
    }
    # repo-relative file -> golden top-level file
    FILE_MAP = {
        ".mcp.json": "mcp.json",
        "CLAUDE.md": "CLAUDE.md",
        ".claude/settings.json": "settings.json",
        ".claude/push-allowlist": "push-allowlist",
        ".claude/model-effort-profiles.json": "model-effort-profiles.json",
        ".claude/grimoire-files.json": "grimoire-files.json",
        "vendor.toml": "vendor.toml",
        ".scaffold-upstream.conf": ".scaffold-upstream.conf",
        ".gitattributes": ".gitattributes",
        ".gitignore": ".gitignore",
    }

    def repo_to_golden(self, repo_rel: str) -> str | None:
        """Map a flavor repo-relative path to its golden path, or None if not in golden."""
        if repo_rel in self.FILE_MAP:
            return self.FILE_MAP[repo_rel]
        best = None
        for prefix, gdir in self.DIR_MAP.items():
            if repo_rel == prefix:
                cand = gdir
            elif repo_rel.startswith(prefix + "/"):
                cand = gdir + "/" + repo_rel[len(prefix) + 1:]
            else:
                continue
            if best is None or len(cand) > len(best):
                best = cand
        return best

    def golden_to_repo(self, golden_rel: str) -> str:
        """Map a golden-relative path to its live (installed) location. Inverse of repo_to_golden.

        This is the single authority consumers (install-doctor, regenerate) use to
        locate a golden file's live counterpart, so new golden members map without
        per-consumer mapping logic.
        """
        # Exact file remaps first.
        for repo_rel, gname in self.FILE_MAP.items():
            if gname == golden_rel:
                return repo_rel
        head, _, rest = golden_rel.partition("/")
        for repo_prefix, gdir in self.DIR_MAP.items():
            if head == gdir:
                return f"{repo_prefix}/{rest}" if rest else repo_prefix
        # Unmapped: lives at repo root as-is.
        return golden_rel


class GoldenGenerator:
    """Derives a golden tree from a flavor root and an optional seed supplement."""

    def __init__(self, flavor_root: Path, seed_root: Path | None = None):
        self.flavor_root = flavor_root
        self.seed_root = seed_root
        self.layout = FlavorLayout()

    # -- exclusion predicates -------------------------------------------------

    @staticmethod
    def _is_junk(repo_rel: str) -> bool:
        parts = repo_rel.split("/")
        if parts[-1] in JUNK_NAMES or repo_rel.endswith(".pyc"):
            return True
        return any(p in JUNK_DIR_NAMES for p in parts)

    @staticmethod
    def _is_authoring_skill(repo_rel: str) -> bool:
        # .claude/skills/<name>/...
        parts = repo_rel.split("/")
        if len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "skills":
            return parts[2] in AUTHORING_SKILLS
        return False

    def _excluded(self, repo_rel: str) -> bool:
        if repo_rel in EXCLUDE_REPO_FILES:
            return True
        if self._is_junk(repo_rel):
            return True
        if self._is_authoring_skill(repo_rel):
            return True
        return False

    # -- planning -------------------------------------------------------------

    def plan(self) -> dict[str, Path]:
        """Return {golden_rel: source_path} for every file the golden will contain.

        Seed-supplement files override flavor-derived ones of the same golden path.
        """
        out: dict[str, Path] = {}
        for src in sorted(self.flavor_root.rglob("*")):
            if not src.is_file():
                continue
            repo_rel = src.relative_to(self.flavor_root).as_posix()
            if self._excluded(repo_rel):
                continue
            golden_rel = self.layout.repo_to_golden(repo_rel)
            if golden_rel is None:
                continue
            out[golden_rel] = src
        # Operational seed supplement: files the flavor snapshot does not carry.
        if self.seed_root and self.seed_root.is_dir():
            for src in sorted(self.seed_root.rglob("*")):
                if not src.is_file() or self._is_junk(src.name):
                    continue
                golden_rel = src.relative_to(self.seed_root).as_posix()
                out[golden_rel] = src
        return out

    # -- emission -------------------------------------------------------------

    def write_tree(self, dest: Path) -> list[str]:
        """Materialize the golden tree under dest. Returns sorted golden-rel paths."""
        plan = self.plan()
        if dest.exists():
            shutil.rmtree(dest)
        for golden_rel, src in plan.items():
            target = dest / golden_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        return sorted(plan)

    def write_archive(self, archive_path: Path) -> list[str]:
        """Write a deterministic .tar.gz of the golden tree. Returns golden-rel paths.

        Reproducible byte-for-byte: entries are emitted in sorted order with
        mtime=0/fixed mode, the tar is built in memory, and the gzip wrapper is
        written with mtime=0 (the default gzip header embeds the current time).
        """
        import gzip
        import io
        plan = self.plan()
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        names = sorted(plan)
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for golden_rel in names:
                data = plan[golden_rel].read_bytes()
                info = tarfile.TarInfo(name=golden_rel)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
        with open(archive_path, "wb") as fh:
            with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz:
                gz.write(raw.getvalue())
        return names


# ---------------------------------------------------------------------------
# Runtime resolution (for consumers: install-doctor, regenerate, files-manifest)
# ---------------------------------------------------------------------------

GOLDEN_CACHE_DIR = ".grimoire-golden"   # gitignored; holds archives + extracted tree
GOLDEN_TREE_SUBDIR = "tree"             # extracted golden tree under the cache dir
GOLDEN_ARCHIVE_GLOB = "golden-v*.tar.gz"


def golden_archive_name(version: str) -> str:
    """Canonical frozen-archive filename for a framework version.

    The consumer glob (install-doctor, resolve_golden) is `golden-v*.tar.gz`, so
    the frozen name MUST carry a single leading `v`. `framework-version` may be
    stored either way ("3.38" or "v3.48"); normalize to exactly one `v` so the
    frozen archive is always discoverable (issue #186).
    """
    v = version[1:] if version.startswith("v") else version
    return f"golden-v{v}.tar.gz"


def framework_version(root: Path) -> str | None:
    """Read framework-version (e.g. 'v3.48') from .claude/grimoire-config.json."""
    import json
    cfg = root / ".claude" / "grimoire-config.json"
    if not cfg.exists():
        return None
    try:
        data = json.loads(cfg.read_text())
    except (ValueError, OSError):
        return None
    fv = data.get("framework-version")
    if isinstance(fv, dict):
        fv = fv.get("value")
    return fv if isinstance(fv, str) else None


def _find_flavor(root: Path) -> Path | None:
    """A canonical flavor source under root (the source/dogfood repo), or None."""
    cand = root / "claude-code"
    if (cand / ".grimoire-flavor").exists():
        return cand
    return None


def resolve_golden(root: Path, *, allow_generate: bool = True) -> Path:
    """Return a path to a materialized golden TREE for `root`.

    Resolution order (safe-by-construction — never derives from a *customized*
    live project tree, only from a flavor or the frozen archive):
      1. an already-extracted tree cache under .grimoire-golden/tree/;
      2. the newest frozen archive .grimoire-golden/golden-v*.tar.gz (extracted);
      3. (source/dogfood repo only) a claude-code/ flavor — generated on the fly.
    Raises FileNotFoundError if none is available (a scaffolded project must run
    grm-workflow-bootstrap first to freeze its baseline archive).
    """
    cache = root / GOLDEN_CACHE_DIR
    tree = cache / GOLDEN_TREE_SUBDIR
    if tree.is_dir() and any(tree.rglob("*")):
        return tree

    archives = sorted(cache.glob(GOLDEN_ARCHIVE_GLOB))
    if archives:
        newest = archives[-1]
        if tree.exists():
            shutil.rmtree(tree)
        tree.mkdir(parents=True, exist_ok=True)
        with tarfile.open(newest, "r:gz") as tar:
            _safe_extract(tar, tree)
        return tree

    if allow_generate:
        flavor = _find_flavor(root)
        if flavor is not None:
            seed = _default_seed(Path(__file__).resolve().parent)
            GoldenGenerator(flavor, seed).write_tree(tree)
            return tree

    raise FileNotFoundError(
        f"no golden baseline for {root}: expected an extracted tree or a "
        f"{GOLDEN_ARCHIVE_GLOB} archive under {GOLDEN_CACHE_DIR}/, or a "
        f"claude-code/ flavor. Run grm-workflow-bootstrap to freeze one."
    )


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract members under dest, rejecting path traversal."""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"unsafe path in archive: {member.name}")
    tar.extractall(dest)


def freeze_from_install(root: Path, seed_root: Path | None = None) -> Path:
    """Bootstrap trigger: derive a golden archive from a PRISTINE install.

    Treats the project root itself as the flavor source (it carries the same
    .claude/ layout). Safe only at install / version-change time, when the files
    still match what was just delivered — never on a customized tree. Writes a
    version-stamped archive under .grimoire-golden/ and returns its path.
    """
    version = framework_version(root) or "unknown"
    archive = root / GOLDEN_CACHE_DIR / golden_archive_name(version)
    if seed_root is None:
        seed_root = _default_seed(Path(__file__).resolve().parent)
    GoldenGenerator(root, seed_root).write_archive(archive)
    # Invalidate any stale extracted tree so the next resolve re-extracts.
    tree = root / GOLDEN_CACHE_DIR / GOLDEN_TREE_SUBDIR
    if tree.exists():
        shutil.rmtree(tree)
    return archive


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        flavor = tmp / "flavor"
        # A miniature flavor tree exercising every rule.
        files = {
            ".claude/skills/grm-build-recipe/SKILL.md": "recipe",
            ".claude/skills/grm-build-recipe/recipe.py": "code",
            ".claude/skills/grm-regenerate-grimoire/SKILL.md": "authoring",  # excluded
            ".claude/skills/grm-build-recipe/__pycache__/x.pyc": "junk",     # excluded
            ".claude/hooks/push-guard.sh": "hook",
            ".claude/settings.json": "{}",
            ".claude/push-allowlist": "allow",
            ".claude/grimoire-config.json": "{}",                            # excluded
            ".claude/grimoire-files.json": "{}",                             # included
            ".claude/quick-start-templates/web/template.json": "{}",         # included
            ".mcp.json": "{}",
            "CLAUDE.md": "contract",
            ".grimoire-flavor": "claude-code",                              # excluded
            ".claude/architecture-rules.example.json": "{}",                # excluded
            ".gitignore": "ignore",                                          # included
            "docs/README.md": "docs",
            "docs/.DS_Store": "junk",                                        # excluded
            "docs/roadmap.md": "roadmap",
        }
        for rel, body in files.items():
            p = flavor / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

        seed = tmp / "seed"
        (seed / "docs").mkdir(parents=True)
        (seed / ".scaffold-upstream.conf").write_text("upstream")
        (seed / "vendor.toml").write_text("vendor")
        (seed / "docs" / "version-history.md").write_text("history")

        # Layout is bidirectional: repo -> golden -> repo round-trips.
        lay = FlavorLayout()
        for repo_rel in (".claude/skills/x/SKILL.md", ".claude/hooks/h.sh", ".mcp.json",
                         "CLAUDE.md", ".claude/settings.json", "docs/README.md",
                         ".claude/mcp-servers/s.py", ".claude/quick-start-templates/w/t.json",
                         ".claude/grimoire-files.json", ".gitignore"):
            g = lay.repo_to_golden(repo_rel)
            check(g is not None and lay.golden_to_repo(g) == repo_rel,
                  f"layout round-trip failed: {repo_rel} -> {g} -> {lay.golden_to_repo(g) if g else None}")

        gen = GoldenGenerator(flavor, seed)
        plan = gen.plan()
        got = set(plan)

        expected = {
            "skills/grm-build-recipe/SKILL.md",
            "skills/grm-build-recipe/recipe.py",
            "hooks/push-guard.sh",
            "settings.json",
            "push-allowlist",
            "grimoire-files.json",
            "quick-start-templates/web/template.json",
            "mcp.json",
            "CLAUDE.md",
            ".gitignore",
            "docs/README.md",
            "docs/roadmap.md",
            ".scaffold-upstream.conf",
            "vendor.toml",
            "docs/version-history.md",
        }
        check(got == expected,
              f"plan mismatch\n  missing: {sorted(expected - got)}\n  extra: {sorted(got - expected)}")

        # Explicit exclusion assertions.
        for bad in ("skills/grm-regenerate-grimoire/SKILL.md",
                    "skills/grm-build-recipe/__pycache__/x.pyc",
                    "grimoire-config.json", ".grimoire-flavor",
                    "architecture-rules.example.json", "docs/.DS_Store"):
            check(bad not in got, f"should be excluded but present: {bad}")

        # Tree round-trips; seed overrides; archive is reproducible.
        dest = tmp / "out"
        names = gen.write_tree(dest)
        check((dest / "docs/version-history.md").read_text() == "history",
              "seed supplement not written")
        check(sorted(names) == sorted(expected), "write_tree names != plan")

        a1 = tmp / "g1.tar.gz"
        a2 = tmp / "g2.tar.gz"
        gen.write_archive(a1)
        gen.write_archive(a2)
        check(a1.read_bytes() == a2.read_bytes(), "archive not reproducible")

        # Runtime resolution: a scaffolded project (no flavor) resolves from the
        # frozen archive; freeze_from_install derives that archive from a pristine
        # install treating the project root as the flavor source.
        proj = tmp / "proj"
        (proj / ".claude").mkdir(parents=True)
        # Pristine install = golden-shaped under .claude/.
        (proj / ".claude/skills/grm-build-recipe").mkdir(parents=True)
        (proj / ".claude/skills/grm-build-recipe/SKILL.md").write_text("recipe")
        (proj / "CLAUDE.md").write_text("contract")
        (proj / ".claude/grimoire-config.json").write_text('{"framework-version": "v9.9"}')
        archive = freeze_from_install(proj, seed)
        check(archive.name == "golden-v9.9.tar.gz", f"bad archive name: {archive.name}")
        check(archive.exists(), "freeze did not write an archive")
        # #186 regression: a framework-version with NO leading 'v' must still
        # freeze to a 'golden-v*.tar.gz' name the consumer glob can discover.
        check(golden_archive_name("3.38") == "golden-v3.38.tar.gz",
              "golden_archive_name failed to v-prefix an unprefixed version")
        check(golden_archive_name("v3.38") == "golden-v3.38.tar.gz",
              "golden_archive_name double-prefixed an already-v version")
        projn = tmp / "projn"
        (projn / ".claude").mkdir(parents=True)
        (projn / ".claude/skills/grm-build-recipe").mkdir(parents=True)
        (projn / ".claude/skills/grm-build-recipe/SKILL.md").write_text("recipe")
        (projn / "CLAUDE.md").write_text("contract")
        (projn / ".claude/grimoire-config.json").write_text('{"framework-version": "3.38"}')
        archn = freeze_from_install(projn, seed)
        check(archn.name == "golden-v3.38.tar.gz", f"unprefixed-version freeze name: {archn.name}")
        check(sorted((projn / GOLDEN_CACHE_DIR).glob(GOLDEN_ARCHIVE_GLOB)) == [archn],
              "frozen archive not matched by the consumer glob")
        # No flavor present -> resolve must come from the archive, not generation.
        resolved = resolve_golden(proj, allow_generate=False)
        check((resolved / "skills/grm-build-recipe/SKILL.md").exists(),
              "resolved tree missing expected file")
        check((resolved / "docs/version-history.md").read_text() == "history",
              "resolved tree missing seed supplement")
        # A root with no baseline and no flavor must raise.
        empty = tmp / "empty"
        (empty / ".claude").mkdir(parents=True)
        try:
            resolve_golden(empty, allow_generate=False)
            check(False, "resolve_golden should raise with no baseline")
        except FileNotFoundError:
            pass

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("generate_golden self-test: OK")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_seed(script_dir: Path) -> Path | None:
    seed = script_dir / "seed"
    return seed if seed.is_dir() else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the golden image from a flavor tree.")
    ap.add_argument("--flavor", help="Flavor root to derive from (default: auto-detect).")
    ap.add_argument("--out", help="Write the golden tree to this directory.")
    ap.add_argument("--archive", help="Write a deterministic .tar.gz to this path.")
    ap.add_argument("--seed", help="Seed-supplement dir (default: <script>/seed).")
    ap.add_argument("--list", action="store_true", help="Print the planned golden paths and exit.")
    ap.add_argument("--freeze", metavar="ROOT",
                    help="Bootstrap trigger: freeze a versioned golden archive from the "
                         "PRISTINE install at ROOT (treats ROOT as the flavor source).")
    ap.add_argument("--ensure-tree", metavar="ROOT",
                    help="Resolve (extract/generate) the golden tree for ROOT and print its path.")
    ap.add_argument("--self-test", action="store_true", help="Run offline self-tests and exit.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    script_dir0 = Path(__file__).resolve().parent
    if args.freeze:
        seed_root = Path(args.seed).resolve() if args.seed else _default_seed(script_dir0)
        archive = freeze_from_install(Path(args.freeze).resolve(), seed_root)
        print(f"froze golden baseline -> {archive}")
        return 0
    if args.ensure_tree:
        tree = resolve_golden(Path(args.ensure_tree).resolve())
        print(tree)
        return 0

    script_dir = Path(__file__).resolve().parent
    if args.flavor:
        flavor_root = Path(args.flavor).resolve()
    else:
        # Auto-detect: nearest ancestor carrying a claude-code/ flavor.
        cur = script_dir
        flavor_root = None
        for parent in [cur, *cur.parents]:
            cand = parent / "claude-code"
            if (cand / ".grimoire-flavor").exists():
                flavor_root = cand
                break
        if flavor_root is None:
            print("error: could not auto-detect a flavor; pass --flavor", file=sys.stderr)
            return 2
    if not flavor_root.is_dir():
        print(f"error: flavor root not found: {flavor_root}", file=sys.stderr)
        return 2

    seed_root = Path(args.seed).resolve() if args.seed else _default_seed(script_dir)
    gen = GoldenGenerator(flavor_root, seed_root)

    if args.list:
        for name in sorted(gen.plan()):
            print(name)
        return 0
    if args.archive:
        names = gen.write_archive(Path(args.archive).resolve())
        print(f"wrote {len(names)} files -> {args.archive}")
        return 0
    if args.out:
        names = gen.write_tree(Path(args.out).resolve())
        print(f"wrote {len(names)} files -> {args.out}")
        return 0
    # No emission target: report the plan summary.
    names = sorted(gen.plan())
    print(f"golden plan: {len(names)} files from flavor {flavor_root.name}"
          + (f" + seed {seed_root.name}" if seed_root else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
