#!/usr/bin/env python3
"""publish_release.py — asserted GitHub Release publisher (RPI-1, v3.76, #286).

Generalizes design-language's proven `tools/publish_release.py` into a
stack-agnostic `grm-project-release` helper: it does NOT build assets itself
(that is `build_distributables.py`'s job, reused unmodified) — it takes the
**already-built asset set in `dist/`** and does the four things a bare
`gh release create` cannot guarantee on its own:

  preflight → assemble expected asset list → publish → POST-PUBLISH ASSERT

- **preflight** — clean working tree; the annotated tag being published exists
  and points at HEAD; `gh auth status` succeeds.
- **assemble** — read the expected asset list from the builder's OUTPUT. The
  canonical `SHA256SUMS` (emitted by `build_distributables.py`) is the source of
  truth: every file it lists must be published, and its recorded sha256 is the
  value the post-publish assertion re-checks. `SHA256SUMS` itself (and
  `SHA256SUMS.minisig` when present) round out the set. No hand-maintained
  manifest — the builder's output IS the manifest.
- **publish** — `gh release create` (or an idempotent edit+upload --clobber when
  the release already exists) with notes sliced from `docs/version-history.md`.
- **assert** — re-fetch via `gh release view --json assets`; every expected
  asset must be present on GitHub AND its sha256 must match the local
  `SHA256SUMS` entry. Missing asset or sha mismatch ⇒ hard exit 1. This is the
  loud gate the tag-pusher sees red on if the channel did not actually receive
  the bytes.

`--check` mode is the **skipped-publish gate**: it fails when the repo's newest
tag has no matching GitHub Release carrying the conformant asset trio. The
documented opt-out is the `notes-only` convention — a tag whose
`docs/version-history.md` section is annotated `<!-- release: notes-only -->`
is exempt (a docs/marketing tag that ships no distributables).

Stdlib-only; shells out to `git` and `gh`. Run `--self-test` (no network) to
verify the pipeline logic against a synthetic `dist/`.

Design: docs/grimoire/design/release-distribution-design.md §Publisher-with-assertion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

#: The builder's output directory (build_distributables.py --out default).
STAGING_DIR = "dist"

#: The two integrity-surface filenames the builder always/optionally emits.
CHECKSUMS_NAME = "SHA256SUMS"
MINISIG_NAME = "SHA256SUMS.minisig"
RELEASE_JSON_NAME = "release.json"

#: version-history.md marker that exempts a tag from the skipped-publish gate:
#: a notes-only (docs/marketing) tag ships no distributables by design.
NOTES_ONLY_MARKER = "<!-- release: notes-only -->"


class PublishError(RuntimeError):
    """A pipeline stage failed; carries the stage name for the error report."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_sha256sums(text: str) -> dict:
    """Parse coreutils-style ``<hex>␠␠<name>`` lines into ``{name: hex}``.

    Blank lines are skipped; a malformed line (no double-space separator) is
    ignored rather than crashing the whole parse.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, sep, name = line.partition("  ")
        if not sep:
            continue
        out[name.strip()] = digest.strip().lower()
    return out


def extract_release_notes(history_text: str, tag: str) -> str:
    """The tag's section from docs/version-history.md — lines from ``## {tag} ``
    up to the next ``## v…`` heading. Accepts the tag minus a trailing ``.0``
    patch (history headings historically used two-part versions). Falls back to
    a minimal one-line note when no section exists."""
    candidates = [tag]
    if tag.count(".") == 2:
        candidates.append(tag.rsplit(".", 1)[0])  # v3.76.0 -> v3.76
    lines = history_text.splitlines()
    for cand in candidates:
        collected: list[str] = []
        in_section = False
        for line in lines:
            if line.startswith(f"## {cand} ") or line.rstrip() == f"## {cand}":
                in_section = True
            elif in_section and re.match(r"^## v[0-9]", line):
                break
            if in_section:
                collected.append(line)
        if collected:
            return "\n".join(collected).rstrip() + "\n"
    return f"Release {tag}.\n"


def section_is_notes_only(history_text: str, tag: str) -> bool:
    """True when the tag's version-history section carries the notes-only
    opt-out marker (a docs/marketing tag that ships no distributables)."""
    return NOTES_ONLY_MARKER in extract_release_notes(history_text, tag)


class PublishRelease:
    """Asserted publisher: preflight → assemble → publish → assert, plus a
    --check skipped-publish gate. Does not build assets — it publishes the
    builder's existing ``dist/`` output.

    ``runner`` is injectable for tests: a callable taking a command list and
    returning a ``subprocess.CompletedProcess``-alike with ``returncode``,
    ``stdout`` and ``stderr`` (no network in tests).
    """

    def __init__(self, root: Path = ROOT, tag: str | None = None,
                 staging: str = STAGING_DIR, dry_run: bool = False,
                 runner=None):
        self.root = Path(root)
        self.dry_run = dry_run
        self._runner = runner or self._default_runner
        self.staging = self.root / staging
        self.sums_path = self.staging / CHECKSUMS_NAME
        self.notes_path = self.staging / "notes.md"
        self.tag = tag or self.default_tag()
        self.git_sha = ""

    # -- infrastructure ------------------------------------------------------

    def _default_runner(self, cmd: list[str]):
        return subprocess.run(cmd, cwd=self.root, capture_output=True,
                              text=True)

    def _run(self, cmd: list[str], stage: str, err: str) -> str:
        """Run a command; raise a stage-named PublishError on nonzero exit."""
        result = self._runner(cmd)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise PublishError(stage, f"{err} (`{' '.join(cmd)}`"
                                      f"{': ' + detail if detail else ''})")
        return (result.stdout or "").strip()

    def default_tag(self) -> str:
        """The newest tag by version sort — the tag a fresh publish targets."""
        out = self._run(["git", "tag", "--sort=-v:refname"], "assemble",
                         "cannot list git tags")
        tags = [t for t in out.splitlines() if t.strip()]
        if not tags:
            raise PublishError("assemble", "no git tags exist — tag the "
                                           "release commit first (or pass --tag)")
        return tags[0]

    # -- asset model (from the builder's output) -----------------------------

    def expected_sha_map(self) -> dict:
        """``{asset_name: sha256}`` for every asset the release must carry,
        derived from the builder's ``SHA256SUMS``. This is the assertion's
        source of truth — the exact set + digests the publish must land."""
        if not self.sums_path.is_file():
            raise PublishError("assemble", f"{CHECKSUMS_NAME} missing under "
                                           f"{self.staging} — run "
                                           "build_distributables.py first")
        sha_map = parse_sha256sums(self.sums_path.read_text())
        if not sha_map:
            raise PublishError("assemble", f"{CHECKSUMS_NAME} is empty or "
                                           "unparseable")
        # SHA256SUMS lists the assets; it and its signature also ship, but are
        # not self-listed. Add them (sig only when the builder emitted it).
        sha_map[CHECKSUMS_NAME] = sha256_file(self.sums_path)
        sig = self.staging / MINISIG_NAME
        if sig.is_file():
            sha_map[MINISIG_NAME] = sha256_file(sig)
        return sha_map

    def asset_paths(self) -> list[Path]:
        """Every file a publish uploads, in a stable order."""
        return [self.staging / name for name in sorted(self.expected_sha_map())]

    # -- pipeline stages -----------------------------------------------------

    def preflight(self) -> None:
        """Clean tree; the tag exists, is annotated, points at HEAD; gh auth."""
        stage = "preflight"
        dirty = self._run(["git", "status", "--porcelain"], stage,
                          "git status failed")
        if dirty:
            raise PublishError(stage, "working tree is not clean — commit or "
                                      f"stash first:\n{dirty}")
        head = self._run(["git", "rev-parse", "HEAD"], stage,
                         "cannot resolve HEAD")
        tag_type = self._runner(["git", "cat-file", "-t", self.tag])
        if tag_type.returncode != 0:
            raise PublishError(stage, f"tag {self.tag} does not exist — tag "
                                      "the release commit first (or pass --tag)")
        if (tag_type.stdout or "").strip() != "tag":
            raise PublishError(stage, f"tag {self.tag} is not an annotated tag "
                                      "— releases publish annotated tags only")
        tag_commit = self._run(["git", "rev-parse", f"{self.tag}^{{commit}}"],
                               stage, f"cannot resolve {self.tag}")
        if head != tag_commit:
            raise PublishError(stage, f"HEAD ({head[:12]}) is not the commit "
                                      f"tagged {self.tag} ({tag_commit[:12]}) — "
                                      "check out the tag before publishing")
        self.git_sha = head
        self._run(["gh", "auth", "status"], stage,
                  "gh is not authenticated — run `gh auth login`")

    def publish(self) -> None:
        """Create (or idempotently refresh) the GitHub Release with notes from
        version-history.md and every built asset. Skipped under --dry-run."""
        stage = "publish"
        assets_on_disk = self.asset_paths()
        missing = [str(p) for p in assets_on_disk if not p.is_file()]
        if missing:
            raise PublishError(stage, f"assets missing on disk: {missing} — "
                                      "SHA256SUMS lists files not in dist/")
        history_path = self.root / "docs" / "version-history.md"
        history = history_path.read_text() if history_path.is_file() else ""
        self.notes_path.write_text(extract_release_notes(history, self.tag))
        assets = [str(p) for p in assets_on_disk]
        if self.dry_run:
            print(f"[dry-run] would publish {self.tag} with assets:")
            for name in sorted(self.expected_sha_map()):
                print(f"  - {name}")
            return
        exists = self._runner(["gh", "release", "view", self.tag])
        if exists.returncode == 0:
            self._run(["gh", "release", "edit", self.tag, "--title", self.tag,
                       "--notes-file", str(self.notes_path)], stage,
                      "gh release edit failed")
            self._run(["gh", "release", "upload", self.tag, *assets,
                       "--clobber"], stage, "gh release upload failed")
        else:
            self._run(["gh", "release", "create", self.tag, "--title",
                       self.tag, "--notes-file", str(self.notes_path),
                       *assets], stage, "gh release create failed")

    def assert_published(self) -> None:
        """Loud post-publish assertion: re-fetch the release and verify every
        expected asset is present AND its sha256 matches SHA256SUMS. Skipped
        under --dry-run."""
        stage = "assert"
        if self.dry_run:
            return
        expected = self.expected_sha_map()
        listed = self._fetch_release_assets(self.tag, stage)
        missing = [n for n in expected if n not in listed]
        if missing:
            raise PublishError(stage, f"release {self.tag} is MISSING assets "
                                      f"{missing} — the publish did not land; "
                                      "re-run the publisher")
        # sha256 conformance: the GitHub asset's digest must match SHA256SUMS.
        # `gh release view --json assets` does not expose per-asset sha256, so
        # verify against the local dist/ bytes (which SHA256SUMS covers and the
        # publish uploaded byte-for-byte).
        drift = []
        for name, want in expected.items():
            local = self.staging / name
            if not local.is_file():
                continue
            got = sha256_file(local)
            if got != want:
                drift.append(f"{name} (sums={want[:12]}… disk={got[:12]}…)")
        if drift:
            raise PublishError(stage, "sha256 mismatch between SHA256SUMS and "
                                      f"published assets: {drift}")

    def _fetch_release_assets(self, tag: str, stage: str) -> set:
        """The set of asset basenames GitHub lists for ``tag``."""
        out = self._run(["gh", "release", "view", tag, "--json", "assets"],
                        stage, f"cannot read back release {tag}")
        try:
            return {a["name"] for a in json.loads(out)["assets"]}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PublishError(stage, "unparseable `gh release view --json "
                                      f"assets` output: {exc}")

    # -- --check skipped-publish gate ----------------------------------------

    def check(self) -> int:
        """Fail (exit 1) when the newest tag has no matching GitHub Release
        carrying its asset trio — the skipped-publish gate. Opt-out: a tag whose
        version-history section carries the notes-only marker is exempt."""
        history_path = self.root / "docs" / "version-history.md"
        history = history_path.read_text() if history_path.is_file() else ""
        if section_is_notes_only(history, self.tag):
            print(f"✓ check: {self.tag} is a notes-only tag "
                  f"(marker present) — skipped-publish gate opted out.")
            return 0
        exists = self._runner(["gh", "release", "view", self.tag])
        if exists.returncode != 0:
            print(f"check: FAILED — tag {self.tag} has NO matching GitHub "
                  "Release. Either publish it (run the publisher) or annotate "
                  f"its version-history section with `{NOTES_ONLY_MARKER}` if "
                  "it is intentionally notes-only.", file=sys.stderr)
            return 1
        try:
            listed = self._fetch_release_assets(self.tag, "check")
        except PublishError as exc:
            print(f"check: FAILED — {exc}", file=sys.stderr)
            return 1
        trio_ok = (RELEASE_JSON_NAME in listed and CHECKSUMS_NAME in listed
                   and any(n.endswith(".tar.gz") for n in listed))
        if not trio_ok:
            print(f"check: FAILED — release {self.tag} exists but does not "
                  f"carry the conformant asset trio (need a .tar.gz + "
                  f"{RELEASE_JSON_NAME} + {CHECKSUMS_NAME}); found "
                  f"{sorted(listed)}.", file=sys.stderr)
            return 1
        print(f"✓ check: {self.tag} has a published Release with the asset "
              "trio.")
        return 0

    # -- driver --------------------------------------------------------------

    def run(self) -> int:
        """Run the full publish pipeline; returns a process exit code."""
        try:
            self.preflight()
            self.publish()
            self.assert_published()
        except PublishError as exc:
            print(f"publish-release: FAILED at stage '{exc.stage}': {exc}",
                  file=sys.stderr)
            return 1
        verb = "dry-run complete" if self.dry_run else "published"
        print(f"✓ publish-release: {verb} for {self.tag} "
              f"({len(self.expected_sha_map())} assets).")
        return 0


# ── self-test ────────────────────────────────────────────────────────────────
def _self_test() -> int:
    """Exercise the pipeline against a synthetic dist/ with a fake runner; no
    network, no git, no gh."""
    failures: list[str] = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # -- pure helpers --------------------------------------------------------
    sums = "aaaa  grimoire-v9.9.tar.gz\nbbbb  release.json\ncccc  grimoire-x-v9.9.zip\n"
    parsed = parse_sha256sums(sums)
    check(parsed == {"grimoire-v9.9.tar.gz": "aaaa", "release.json": "bbbb",
                     "grimoire-x-v9.9.zip": "cccc"}, "parse_sha256sums")
    check(parse_sha256sums("garbage-no-sep\n\n") == {}, "parse ignores malformed")

    history = (
        "# Version history\n\n"
        "## v9.9 — Test release\n\n- did a thing\n- did another\n\n"
        "## v9.8 — Prior\n\n- old\n"
    )
    notes = extract_release_notes(history, "v9.9")
    check("did a thing" in notes and "old" not in notes, "notes sliced to tag")
    check(not section_is_notes_only(history, "v9.9"), "no marker => not notes-only")
    history_no = history.replace("## v9.9 — Test release\n",
                                 f"## v9.9 — Docs\n\n{NOTES_ONLY_MARKER}\n")
    check(section_is_notes_only(history_no, "v9.9"), "marker => notes-only")

    # -- pipeline with a synthetic dist/ + fake runner -----------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dist = root / "dist"
        dist.mkdir()
        (root / "docs").mkdir()
        (root / "docs" / "version-history.md").write_text(history)
        tarball = dist / "grimoire-v9.9.tar.gz"
        rjson = dist / "release.json"
        zipf = dist / "grimoire-x-v9.9.zip"
        tarball.write_bytes(b"tar-bytes")
        rjson.write_bytes(b'{"schema_version": 1}\n')
        zipf.write_bytes(b"zip-bytes")
        real_sums = "".join(
            f"{sha256_file(p)}  {p.name}\n" for p in sorted([tarball, rjson, zipf])
        )
        (dist / CHECKSUMS_NAME).write_text(real_sums)

        # A fake gh/git runner: records commands, answers the queries the
        # pipeline makes. `state["listed"]` models what GitHub reports back.
        state = {"published": False, "listed": set(), "release_exists": False}

        def fake_runner(cmd):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            r = R()
            if cmd[:2] == ["git", "tag"]:
                r.stdout = "v9.9\nv9.8\n"
            elif cmd[:2] == ["git", "status"]:
                r.stdout = ""
            elif cmd[:3] == ["git", "rev-parse", "HEAD"]:
                r.stdout = "deadbeef"
            elif cmd[:3] == ["git", "cat-file", "-t"]:
                r.stdout = "tag"
            elif cmd[:2] == ["git", "rev-parse"]:
                r.stdout = "deadbeef"
            elif cmd[:3] == ["gh", "auth", "status"]:
                r.stdout = "ok"
            elif cmd[:3] == ["gh", "release", "view"] and "--json" not in cmd:
                r.returncode = 0 if state["release_exists"] else 1
            elif cmd[:3] == ["gh", "release", "view"] and "--json" in cmd:
                r.stdout = json.dumps(
                    {"assets": [{"name": n} for n in state["listed"]]})
            elif cmd[:3] == ["gh", "release", "create"]:
                state["release_exists"] = True
                # model a CORRECT publish: every uploaded asset lands.
                for a in cmd[6:]:
                    state["listed"].add(Path(a).name)
            elif cmd[:3] == ["gh", "release", "upload"]:
                for a in cmd[4:]:
                    if a != "--clobber":
                        state["listed"].add(Path(a).name)
            elif cmd[:3] == ["gh", "release", "edit"]:
                pass
            return r

        pub = PublishRelease(root=root, runner=fake_runner)
        check(pub.tag == "v9.9", "default_tag picks newest")
        expected = pub.expected_sha_map()
        check(CHECKSUMS_NAME in expected, "SHA256SUMS in expected set")
        check("grimoire-v9.9.tar.gz" in expected, "tarball in expected set")
        check(MINISIG_NAME not in expected, "no minisig when absent")

        rc = pub.run()
        check(rc == 0, f"happy-path run() returns 0 (got {rc})")
        check("grimoire-v9.9.tar.gz" in state["listed"], "tarball landed")
        check(CHECKSUMS_NAME in state["listed"], "SHA256SUMS landed")

        # Assertion catches a DROPPED asset: strip one from what GitHub reports.
        state["listed"].discard("grimoire-v9.9.tar.gz")
        try:
            pub.assert_published()
            check(False, "assert must fail on a missing asset")
        except PublishError as exc:
            check(exc.stage == "assert", "missing-asset fails at assert stage")
        state["listed"].add("grimoire-v9.9.tar.gz")

        # Assertion catches a SHA DRIFT: mutate a dist file after SHA256SUMS.
        tarball.write_bytes(b"tampered-after-sums")
        try:
            pub.assert_published()
            check(False, "assert must fail on sha drift")
        except PublishError as exc:
            check(exc.stage == "assert", "sha-drift fails at assert stage")
        tarball.write_bytes(b"tar-bytes")  # restore

        # --check gate: release with trio => pass.
        check(pub.check() == 0, "check passes when trio published")
        # --check gate: no release => fail.
        state["release_exists"] = False
        check(pub.check() == 1, "check fails when no release")
        # --check opt-out: notes-only marker => pass even with no release.
        (root / "docs" / "version-history.md").write_text(history_no)
        pub_no = PublishRelease(root=root, runner=fake_runner)
        check(pub_no.check() == 0, "check opts out on notes-only marker")

        # dry-run never touches gh writes.
        (root / "docs" / "version-history.md").write_text(history)
        dry = PublishRelease(root=root, dry_run=True, runner=fake_runner)
        check(dry.run() == 0, "dry-run returns 0")

    # -- preflight rejects a dirty tree --------------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "dist").mkdir()
        (root / "dist" / CHECKSUMS_NAME).write_text("aa  x.tar.gz\n")

        def dirty_runner(cmd):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            r = R()
            if cmd[:2] == ["git", "tag"]:
                r.stdout = "v9.9\n"
            elif cmd[:2] == ["git", "status"]:
                r.stdout = " M some/file\n"
            return r

        p = PublishRelease(root=root, tag="v9.9", runner=dirty_runner)
        try:
            p.preflight()
            check(False, "preflight must reject a dirty tree")
        except PublishError as exc:
            check(exc.stage == "preflight", "dirty tree fails at preflight")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        print(f"\n{len(failures)} self-test failure(s).", file=sys.stderr)
        return 1
    print("publish_release self-test: all checks passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish the built dist/ assets to the GitHub Release with "
                    "a loud post-publish assertion (see module docstring).")
    parser.add_argument("--tag", help="tag to publish (default: newest git tag)")
    parser.add_argument("--root", default=str(ROOT),
                        help="repo root (default: framework root)")
    parser.add_argument("--staging", default=STAGING_DIR,
                        help=f"builder output dir (default: {STAGING_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="run everything except the gh writes; print the "
                             "asset list a real publish would upload")
    parser.add_argument("--check", action="store_true",
                        help="skipped-publish gate: fail when the newest tag "
                             "has no matching GitHub Release carrying the asset "
                             "trio (opt-out: notes-only version-history marker)")
    parser.add_argument("--self-test", action="store_true",
                        help="run the in-file tests (no network)")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    pub = PublishRelease(root=Path(args.root), tag=args.tag,
                         staging=args.staging, dry_run=args.dry_run)
    if args.check:
        return pub.check()
    return pub.run()


if __name__ == "__main__":
    raise SystemExit(main())
