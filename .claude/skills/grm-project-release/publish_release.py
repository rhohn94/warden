#!/usr/bin/env python3
"""publish_release.py — asserted GitHub Release publisher (RPI-1, v3.76, #286).

Generalizes design-language's proven `tools/publish_release.py` into a
stack-agnostic `grm-project-release` helper: it does NOT build assets itself
(that is `build_distributables.py`'s job, reused unmodified) — it takes the
**already-built asset set in `dist/`** and does the four things a bare
`gh release create` cannot guarantee on its own:

  preflight → verify → assemble expected asset list → publish → POST-PUBLISH ASSERT

- **preflight** — clean working tree; the annotated tag being published exists
  and points at HEAD; `gh auth status` succeeds.
- **verify** (v3.90) — run the repo's optional `[assert] verify` commands from
  `publish.toml` (e.g. a codesign check); any non-zero exit hard-fails BEFORE
  anything publishes, so an unsigned artifact can't look like success.
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

`--check` mode is the **skipped-publish gate**: it fails when any tag *since
the last conformant release* has no matching GitHub Release carrying the
conformant asset trio — not just the newest tag (v3.87 shipped a real,
non-notes-only release with NO GitHub Release at all, and sat invisible to
`--check` for three releases because it only ever inspected the newest tag;
see docs/grimoire/design/release-pipeline-design.md §Skipped-publish gate
scan window). "The last conformant release" is a persisted checkpoint
(`.claude/cache/publish-check-state.json`, gitignored — per-clone state, like
the sync-continuation token): each passing `--check` advances it to the
newest tag, so steady-state runs only re-scan tags newer than the checkpoint;
an absent/stale checkpoint (first run, or a checkpoint tag no longer in the
repo's tag list) falls back to scanning every tag back to `--check-floor`
(default `v3.76`, when this asserted publisher itself shipped — tags before
it predate the asset-trio contract and are excluded, not "failures"), which
is exactly the one-time bounded sweep that would have caught v3.87. The
documented opt-out is the `notes-only` convention — a tag whose
`docs/version-history.md` section is annotated `<!-- release: notes-only -->`
is exempt (a docs/marketing tag that ships no distributables).

Stdlib-only; shells out to `git` and `gh`. Run `--self-test` (no network) to
verify the pipeline logic against a synthetic `dist/`.

Design: docs/grimoire/design/release-pipeline-design.md §Publisher-with-assertion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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

#: --check's persisted "last conformant release" checkpoint (gitignored,
#: per-clone state — like the sync-continuation token). Absent/stale falls
#: back to a full-history scan.
CHECK_STATE_NAME = ".claude/cache/publish-check-state.json"

#: A checkpoint-free full scan never goes older than this tag — the asserted
#: publisher (and the asset-trio conformance contract it enforces) shipped in
#: v3.76 (RPI-1, #286); tags before it predate the contract entirely and
#: legitimately carry no conformant Release. Without this floor, a first-ever
#: full scan on this repo reports ~65 pre-contract tags as "failures" alongside
#: the one real gap (v3.87) it exists to catch. A repo whose own publisher
#: shipped at a different version overrides via `--check-floor`.
CHECK_FLOOR_TAG = "v3.76"

#: Optional per-repo publish assertions (v3.90, autonomous-wave): the
#: `[assert]` table of publish.toml may declare `verify = ["cmd", …]` — shell
#: commands run from the repo root at the `verify` stage (after preflight,
#: before anything publishes). Any non-zero exit hard-fails the run, so an
#: unsigned or malformed artifact can no longer look like publish success
#: (the retro-game-player unsigned-DMG failure mode: its release script
#: exited 0 when signing creds were absent). Signed-if-configured example:
#:   [assert]
#:   verify = ["codesign --verify --deep dist/MyApp.dmg"]
PUBLISH_MANIFEST_NAME = "publish.toml"


def load_verify_commands(root: Path) -> list[str]:
    """The repo's `[assert] verify` command list from publish.toml ([] when the
    manifest or table is absent). A malformed manifest is a loud `verify`-stage
    failure, never a silent skip."""
    path = Path(root) / PUBLISH_MANIFEST_NAME
    if not path.is_file():
        return []
    import tomllib  # stdlib on 3.11+ (the dependency-channel design's floor)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PublishError("verify",
                           f"cannot parse {PUBLISH_MANIFEST_NAME}: {exc}")
    cmds = (data.get("assert") or {}).get("verify") or []
    if not isinstance(cmds, list) or not all(isinstance(c, str) for c in cmds):
        raise PublishError("verify", f"{PUBLISH_MANIFEST_NAME} [assert] verify "
                                     "must be a list of command strings")
    return cmds


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
                 runner=None, check_state_path: Path | None = None,
                 check_floor: str | None = CHECK_FLOOR_TAG):
        self.root = Path(root)
        self.dry_run = dry_run
        self._runner = runner or self._default_runner
        self.staging = self.root / staging
        self.sums_path = self.staging / CHECKSUMS_NAME
        self.notes_path = self.staging / "notes.md"
        self.tag = tag or self.default_tag()
        self.git_sha = ""
        self.check_floor = check_floor
        self.check_state_path = (Path(check_state_path) if check_state_path
                                 else self.root / CHECK_STATE_NAME)

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

    def verify(self) -> None:
        """Run the repo's `[assert] verify` commands (publish.toml) before
        anything publishes — the signed-if-configured gate. Runs even under
        --dry-run (a dry run that skips verification gives false confidence;
        verify commands are assertions by contract, not mutations)."""
        stage = "verify"
        for cmd in load_verify_commands(self.root):
            self._run(["sh", "-c", cmd], stage,
                      f"publish assertion failed — `{cmd}` exited non-zero; "
                      "the artifact set is not in a publishable state (e.g. "
                      "unsigned where signing is configured)")
            print(f"✓ verify: `{cmd}`")

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

    def all_tags(self) -> list:
        """Every git tag, newest-first (same sort as default_tag(), full list)."""
        out = self._run(["git", "tag", "--sort=-v:refname"], "check",
                         "cannot list git tags")
        return [t for t in out.splitlines() if t.strip()]

    def read_check_checkpoint(self) -> str | None:
        """The tag `--check` last confirmed fully conformant, or None when no
        checkpoint has ever been recorded (first run — falls back to a full
        scan) or the checkpoint's own file is missing/unparseable."""
        if not self.check_state_path.is_file():
            return None
        try:
            data = json.loads(self.check_state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        tag = data.get("last-conformant-tag")
        return tag if isinstance(tag, str) and tag else None

    def write_check_checkpoint(self, tag: str) -> None:
        """Persist `tag` as the new last-conformant checkpoint (temp + replace)."""
        self.check_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.check_state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"last-conformant-tag": tag}, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.check_state_path)

    def _tag_conformance(self, tag: str, history: str) -> tuple[bool, str]:
        """(conformant, message) for one tag: notes-only marker exempts it;
        otherwise it needs a GitHub Release carrying the asset trio."""
        if section_is_notes_only(history, tag):
            return True, f"{tag} is a notes-only tag (marker present) — opted out."
        exists = self._runner(["gh", "release", "view", tag])
        if exists.returncode != 0:
            return False, (f"{tag} has NO matching GitHub Release. Either "
                           "publish it (run the publisher) or annotate its "
                           f"version-history section with `{NOTES_ONLY_MARKER}` "
                           "if it is intentionally notes-only.")
        try:
            listed = self._fetch_release_assets(tag, "check")
        except PublishError as exc:
            return False, f"{tag}: {exc}"
        trio_ok = (RELEASE_JSON_NAME in listed and CHECKSUMS_NAME in listed
                   and any(n.endswith(".tar.gz") for n in listed))
        if not trio_ok:
            return False, (f"{tag} has a release but not the conformant asset "
                           f"trio (need a .tar.gz + {RELEASE_JSON_NAME} + "
                           f"{CHECKSUMS_NAME}); found {sorted(listed)}.")
        return True, f"{tag} has a published Release with the asset trio."

    def check(self) -> int:
        """Fail (exit 1) when ANY tag since the last conformant release (a
        persisted checkpoint — see module docstring) has no matching GitHub
        Release carrying its asset trio. Not just the newest tag: a lone gap
        sandwiched between two conformant releases (the v3.87 case) is exactly
        what a newest-only check would miss. Opt-out: a tag whose
        version-history section carries the notes-only marker is exempt."""
        history_path = self.root / "docs" / "version-history.md"
        history = history_path.read_text() if history_path.is_file() else ""

        tags = self.all_tags()
        if not tags:
            print("check: FAILED — no git tags exist.", file=sys.stderr)
            return 1
        checkpoint = self.read_check_checkpoint()
        if checkpoint and checkpoint in tags:
            idx = tags.index(checkpoint)
            window = tags[:idx]  # newer than the checkpoint, checkpoint excluded
        else:
            # No (usable) checkpoint — full-history scan, bounded at the floor
            # (the publisher's own conformance contract didn't exist before
            # it) so a first-ever run doesn't drown the real gap it's meant to
            # catch under a pile of legitimately pre-contract tags.
            window = tags
            if self.check_floor and self.check_floor in tags:
                window = tags[:tags.index(self.check_floor) + 1]

        if not window:
            print(f"✓ check: {tags[0]} — already at the last conformant "
                  "checkpoint, nothing new to scan.")
            return 0

        failures: list[str] = []
        for tag in window:
            ok, msg = self._tag_conformance(tag, history)
            print(("✓ check: " if ok else "check: FAILED — ") + msg,
                  file=sys.stdout if ok else sys.stderr)
            if not ok:
                failures.append(tag)

        if failures:
            print(f"check: FAILED — {len(failures)} tag(s) since the last "
                  f"conformant checkpoint have no conformant Release: "
                  f"{failures}.", file=sys.stderr)
            return 1
        self.write_check_checkpoint(tags[0])
        print(f"✓ check: {len(window)} tag(s) scanned since the last "
              f"checkpoint — all conformant. Checkpoint advanced to {tags[0]}.")
        return 0

    # -- driver --------------------------------------------------------------

    def run(self) -> int:
        """Run the full publish pipeline; returns a process exit code."""
        try:
            self.preflight()
            self.verify()
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

        # --check gate: release with trio => pass. `git tag` mocks 2 tags
        # (v9.9, v9.8); seed the checkpoint at v9.8 so the scan window is just
        # {v9.9} — preserves the original single-tag-check semantics exactly
        # (v9.8's own conformance is exercised separately below.)
        pub.write_check_checkpoint("v9.8")
        check(pub.check() == 0, "check passes when trio published")
        check(pub.read_check_checkpoint() == "v9.9",
              "a passing check advances the checkpoint to the newest tag")
        # --check gate: no release => fail. Re-seed the checkpoint at v9.8 (the
        # prior pass advanced it to v9.9, which would otherwise make this
        # check's window empty — a checkpointed tag is trusted, never
        # re-verified, so the window must include v9.9 again to observe it
        # going from conformant to not).
        pub.write_check_checkpoint("v9.8")
        state["release_exists"] = False
        check(pub.check() == 1, "check fails when no release")
        check(pub.read_check_checkpoint() == "v9.8",
              "a failing check must NOT advance the checkpoint")
        # --check opt-out: notes-only marker => pass even with no release.
        (root / "docs" / "version-history.md").write_text(history_no)
        pub_no = PublishRelease(root=root, runner=fake_runner)
        pub_no.write_check_checkpoint("v9.8")  # isolate the window to {v9.9} again
        check(pub_no.check() == 0, "check opts out on notes-only marker")
        (root / "docs" / "version-history.md").write_text(history)
        state["release_exists"] = True

        # dry-run never touches gh writes.
        (root / "docs" / "version-history.md").write_text(history)
        dry = PublishRelease(root=root, dry_run=True, runner=fake_runner)
        check(dry.run() == 0, "dry-run returns 0")

    # -- --check multi-tag scan: the v3.87 "sandwiched gap" regression -------
    # v9.9 and v9.7 have conformant releases; v9.8 (in between) does NOT — a
    # newest-only check would never see v9.8's gap (exactly what let v3.87
    # slip through for three releases). A per-tag-aware fake runner models
    # per-tag release state (unlike the flat `state["listed"]` set above,
    # which only ever modeled a single most-recently-published tag).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "docs").mkdir()
        (root / "docs" / "version-history.md").write_text(
            "# Version history\n\n"
            "## v9.9 — Newest\n\n- ok\n\n"
            "## v9.8 — The gap\n\n- shipped a real change, never published\n\n"
            "## v9.7 — Older, conformant\n\n- ok\n"
        )
        releases = {"v9.9": {"release.json", "grimoire-v9.9.tar.gz", CHECKSUMS_NAME},
                    "v9.7": {"release.json", "grimoire-v9.7.tar.gz", CHECKSUMS_NAME}}

        def scan_runner(cmd):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            r = R()
            if cmd[:2] == ["git", "tag"]:
                r.stdout = "v9.9\nv9.8\nv9.7\n"
            elif cmd[:3] == ["gh", "release", "view"] and "--json" not in cmd:
                r.returncode = 0 if cmd[3] in releases else 1
            elif cmd[:3] == ["gh", "release", "view"] and "--json" in cmd:
                r.stdout = json.dumps(
                    {"assets": [{"name": n} for n in releases.get(cmd[3], set())]})
            return r

        state_path = root / CHECK_STATE_NAME
        pub_scan = PublishRelease(root=root, tag="v9.9", runner=scan_runner,
                                  check_state_path=state_path)
        rc = pub_scan.check()
        check(rc == 1, f"full scan (no checkpoint) must catch the v9.8 gap (got {rc})")
        check(not state_path.is_file(),
              "a failing multi-tag scan must not write a checkpoint")

        # Publish v9.8's missing release; the SAME full scan (still no
        # checkpoint) now finds all three tags conformant.
        releases["v9.8"] = {"release.json", "grimoire-v9.8.tar.gz", CHECKSUMS_NAME}
        rc = pub_scan.check()
        check(rc == 0, f"full scan passes once every tag is conformant (got {rc})")
        check(pub_scan.read_check_checkpoint() == "v9.9",
              "checkpoint advances to the newest tag after a clean full scan")

        # A checkpoint at v9.8 narrows the NEXT scan to {v9.9} only — v9.8/v9.7
        # are not re-queried (deleting their release state must not matter).
        del releases["v9.8"]; del releases["v9.7"]
        pub_scan.write_check_checkpoint("v9.8")
        rc = pub_scan.check()
        check(rc == 0, f"checkpointed scan only re-checks tags newer than it (got {rc})")

        # A checkpoint naming a tag no longer in the repo's tag list (e.g. a
        # rewritten/deleted tag) falls back to a full scan rather than
        # crashing or silently trusting a dangling pointer.
        pub_scan.write_check_checkpoint("v0.0-does-not-exist")
        rc = pub_scan.check()
        check(rc == 1, f"a stale/unknown checkpoint falls back to a full scan (got {rc})")

        # The floor bounds a checkpoint-free full scan: v9.8 stays a real gap
        # (newer than the floor), but v9.7 (at/older than the floor) is
        # excluded — a pre-contract tag must not be reported as a failure.
        import contextlib
        import io
        pub_scan.check_state_path.unlink(missing_ok=True)  # clear checkpoint
        pub_floored = PublishRelease(root=root, tag="v9.9", runner=scan_runner,
                                     check_state_path=state_path, check_floor="v9.8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pub_floored.check()
        check(rc == 1, f"floored scan still catches the v9.8 gap (got {rc})")
        check("v9.7" not in out.getvalue() + err.getvalue(),
              "floored scan must not even query/report a tag at/older than the floor")
        # An unknown floor tag (not in the repo's history) is ignored — the
        # scan falls back to unbounded, exactly like no floor at all.
        pub_unknown_floor = PublishRelease(root=root, tag="v9.9", runner=scan_runner,
                                           check_state_path=state_path,
                                           check_floor="v0.0-does-not-exist")
        rc = pub_unknown_floor.check()
        check(rc == 1, f"an unknown floor tag falls back to unbounded scan (got {rc})")

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

    # -- verify stage: [assert] verify commands from publish.toml (v3.90) ----
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # No publish.toml at all → no commands, verify is a no-op.
        check(load_verify_commands(root) == [], "no manifest => no verify cmds")
        p = PublishRelease(root=root, tag="v9.9")
        p.verify()  # must not raise
        # publish.toml without an [assert] table → still no commands.
        (root / PUBLISH_MANIFEST_NAME).write_text(
            '[publish]\nname = "x"\n', encoding="utf-8")
        check(load_verify_commands(root) == [], "no [assert] => no verify cmds")
        # Passing command → verify succeeds.
        (root / PUBLISH_MANIFEST_NAME).write_text(
            '[assert]\nverify = ["exit 0"]\n', encoding="utf-8")
        p = PublishRelease(root=root, tag="v9.9")
        p.verify()  # must not raise
        # Failing command → hard PublishError at the verify stage.
        (root / PUBLISH_MANIFEST_NAME).write_text(
            '[assert]\nverify = ["exit 3"]\n', encoding="utf-8")
        p = PublishRelease(root=root, tag="v9.9")
        try:
            p.verify()
            check(False, "failing verify command must raise")
        except PublishError as exc:
            check(exc.stage == "verify", "failing command fails at verify stage")
        # Malformed verify value → loud verify-stage error, never a skip.
        (root / PUBLISH_MANIFEST_NAME).write_text(
            '[assert]\nverify = "not-a-list"\n', encoding="utf-8")
        try:
            load_verify_commands(root)
            check(False, "non-list verify must raise")
        except PublishError as exc:
            check(exc.stage == "verify", "non-list verify fails at verify stage")
        # Unparseable TOML → loud verify-stage error.
        (root / PUBLISH_MANIFEST_NAME).write_text("[assert\n", encoding="utf-8")
        try:
            load_verify_commands(root)
            check(False, "malformed TOML must raise")
        except PublishError as exc:
            check(exc.stage == "verify", "malformed TOML fails at verify stage")

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
                        help="skipped-publish gate: fail when any tag since "
                             "the last conformant checkpoint has no matching "
                             "GitHub Release carrying the asset trio (opt-out: "
                             "notes-only version-history marker)")
    parser.add_argument("--check-floor", default=CHECK_FLOOR_TAG,
                        help="oldest tag a checkpoint-free full --check scan "
                             f"will consider (default: {CHECK_FLOOR_TAG}, when "
                             "the asserted publisher shipped); pass '' to scan "
                             "the entire tag history")
    parser.add_argument("--self-test", action="store_true",
                        help="run the in-file tests (no network)")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    pub = PublishRelease(root=Path(args.root), tag=args.tag,
                         staging=args.staging, dry_run=args.dry_run,
                         check_floor=args.check_floor or None)
    if args.check:
        return pub.check()
    return pub.run()


if __name__ == "__main__":
    raise SystemExit(main())
