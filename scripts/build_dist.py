#!/usr/bin/env python3
"""build_dist.py — assemble warden's GitHub Release asset trio into dist/.

The `package` build-recipe target (`just package` → this script). Produces the
conformant asset trio (grimoire#286 / dependency-channel-design.md §2) that
`.claude/skills/grm-project-release/publish_release.py` publishes and asserts:

  dist/warden-v{VERSION}-{TARGET}.tar.gz   the release binary (primary artifact)
  dist/release.json                        kind-discriminated release manifest
  dist/SHA256SUMS                          coreutils-format digests of the above

Pipeline: `cargo build --release` (unless --skip-build) → deterministic tarball
of the binary (+ README.md) → release.json → SHA256SUMS. The tarball is
byte-reproducible for a given commit: entry mtimes are pinned to the commit
timestamp, owner ids zeroed, entries sorted.

Version defaults to Cargo.toml's [package] version; the platform target
defaults to the host (e.g. macos-arm64). `--self-test` runs an offline round
trip against a synthetic binary — no cargo, no git required.

Stdlib-only. Fails loud on any missing precondition; never a silent no-op.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "warden"
#: Repo files bundled beside the binary in the tarball (existing ones only).
EXTRA_FILES = ("README.md",)


class DistError(RuntimeError):
    """A build/staging step failed; message says which and why."""


def run(cmd: list[str], cwd: Path) -> str:
    """Run a command; raise DistError (with stderr) on nonzero exit."""
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise DistError(f"`{' '.join(cmd)}` failed"
                        f"{': ' + detail if detail else ''}")
    return (res.stdout or "").strip()


def cargo_version(root: Path) -> str:
    """The [package] version from Cargo.toml (first `version = "..."` key)."""
    text = (root / "Cargo.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not m:
        raise DistError("no `version = \"...\"` key found in Cargo.toml")
    return m.group(1)


def host_target() -> str:
    """Normalized platform label, e.g. macos-arm64 / linux-x86_64."""
    os_name = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(
        platform.system(), platform.system().lower())
    arch = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "arm64",
            "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    return f"{os_name}-{arch}"


def git_metadata(root: Path) -> tuple[str, int]:
    """(HEAD sha, commit unix timestamp) — ("", 0) outside a git repo."""
    try:
        sha = run(["git", "rev-parse", "HEAD"], root)
        ts = int(run(["git", "log", "-1", "--format=%ct"], root))
        return sha, ts
    except (DistError, ValueError):
        return "", 0


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_binary(root: Path) -> Path:
    """`cargo build --release`; return the built binary path."""
    run(["cargo", "build", "--release"], root)
    return release_binary(root)


def release_binary(root: Path) -> Path:
    binary = root / "target" / "release" / APP_NAME
    if not binary.is_file():
        raise DistError(f"release binary missing at {binary} — build failed?")
    return binary


def write_tarball(out_dir: Path, tar_name: str, binary: Path,
                  extras: list[Path], mtime: int) -> Path:
    """Deterministic .tar.gz: sorted entries, pinned mtime, zeroed owners."""
    tar_path = out_dir / tar_name
    members = [(APP_NAME, binary, 0o755)] + [
        (p.name, p, 0o644) for p in sorted(extras)]
    # gzip mtime pinned too (mtime=0 header) so the archive is byte-stable.
    with open(tar_path, "wb") as raw:
        import gzip
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for arcname, src, mode in members:
                    info = tarfile.TarInfo(arcname)
                    data = src.read_bytes()
                    info.size = len(data)
                    info.mode = mode
                    info.mtime = mtime
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info, io.BytesIO(data))
    return tar_path


def write_release_json(out_dir: Path, version: str, channel: str,
                       git_sha: str, tar_path: Path) -> Path:
    """The kind-discriminated release manifest (dependency-channel §2 shape)."""
    manifest = {
        "schema_version": 1,
        "name": APP_NAME,
        "version": version,
        "channel": channel,
        "git_sha": git_sha or None,
        "artifact_kind": "binary",
        "primary_artifact": tar_path.name,
        "primary_artifact_sha256": sha256_file(tar_path),
        "signature": None,
        "assets": [{
            "name": tar_path.name,
            "sha256": sha256_file(tar_path),
            "bytes": tar_path.stat().st_size,
        }],
    }
    path = out_dir / "release.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def write_sha256sums(out_dir: Path, files: list[Path]) -> Path:
    """coreutils-format SHA256SUMS over `files` (publish_release.py's truth)."""
    lines = [f"{sha256_file(p)}  {p.name}" for p in sorted(files)]
    path = out_dir / "SHA256SUMS"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def stage(root: Path, version: str, target: str, channel: str,
          binary: Path, out_dir: Path) -> list[Path]:
    """Assemble the full trio into out_dir; return the staged paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    git_sha, commit_ts = git_metadata(root)
    extras = [root / n for n in EXTRA_FILES if (root / n).is_file()]
    tar_name = f"{APP_NAME}-v{version}-{target}.tar.gz"
    tar_path = write_tarball(out_dir, tar_name, binary, extras, commit_ts)
    manifest = write_release_json(out_dir, version, channel, git_sha, tar_path)
    sums = write_sha256sums(out_dir, [tar_path, manifest])
    return [tar_path, manifest, sums]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble warden's release "
                                             "asset trio into dist/.")
    ap.add_argument("--version", default="",
                    help="release version (default: Cargo.toml [package] version)")
    ap.add_argument("--target", default="",
                    help="platform label (default: host, e.g. macos-arm64)")
    ap.add_argument("--channel", default="stable", choices=["stable", "beta"])
    ap.add_argument("--out", default="dist", help="output directory")
    ap.add_argument("--skip-build", action="store_true",
                    help="reuse the existing target/release binary")
    ap.add_argument("--self-test", action="store_true",
                    help="offline round trip against a synthetic binary")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    version = args.version or cargo_version(ROOT)
    cargo_v = cargo_version(ROOT)
    if version != cargo_v:
        raise DistError(f"--version {version} does not match Cargo.toml's "
                        f"{cargo_v} — bump Cargo.toml first (the release "
                        "ceremony does this) or drop --version")
    target = args.target or host_target()
    binary = release_binary(ROOT) if args.skip_build else build_binary(ROOT)
    staged = stage(ROOT, version, target, args.channel, binary, ROOT / args.out)
    for p in staged:
        print(f"staged {p.relative_to(ROOT)}  ({p.stat().st_size} bytes)")
    print(f"✓ dist trio ready for v{version} ({target}, {args.channel})")
    return 0


def self_test() -> int:
    """Offline round trip: synthetic binary → trio → verify shape + digests."""
    import tempfile
    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        failures += not ok
        print(("ok  " if ok else "FAIL") + f"  {label}")

    with tempfile.TemporaryDirectory(prefix="warden-dist-") as td:
        root = Path(td)
        fake = root / "fake-warden"
        fake.write_bytes(b"\x7fELF synthetic binary bytes")
        (root / "README.md").write_text("readme\n")
        out = root / "dist"

        staged = stage(root, "9.9.9", "testos-arm64", "stable", fake, out)
        tarball, manifest, sums = staged
        check("tarball named for version+target",
              tarball.name == "warden-v9.9.9-testos-arm64.tar.gz")
        with tarfile.open(tarball) as tar:
            names = tar.getnames()
            member = tar.getmember(APP_NAME)
            check("binary present in tarball as 'warden'", APP_NAME in names)
            check("README.md bundled", "README.md" in names)
            check("binary is executable (0755)", member.mode == 0o755)
            check("owner ids zeroed (deterministic)",
                  member.uid == 0 and member.gid == 0)

        m = json.loads(manifest.read_text())
        check("release.json schema fields present",
              all(k in m for k in ("schema_version", "name", "version",
                                    "channel", "artifact_kind",
                                    "primary_artifact",
                                    "primary_artifact_sha256", "assets")))
        check("manifest version/name/kind correct",
              (m["name"], m["version"], m["artifact_kind"]) ==
              (APP_NAME, "9.9.9", "binary"))
        check("primary artifact sha matches tarball bytes",
              m["primary_artifact_sha256"] == sha256_file(tarball))

        sums_map = {}
        for line in sums.read_text().splitlines():
            digest, _, name = line.partition("  ")
            sums_map[name] = digest
        check("SHA256SUMS covers tarball + release.json",
              set(sums_map) == {tarball.name, "release.json"})
        check("SHA256SUMS digests correct",
              all(sums_map[p.name] == sha256_file(p)
                  for p in (tarball, manifest)))

        # Reproducibility: restaging yields byte-identical outputs.
        second = stage(root, "9.9.9", "testos-arm64", "stable", fake, out)
        check("restage is byte-identical (deterministic build)",
              all(sha256_file(a) == sha256_file(b)
                  for a, b in zip(staged, second)))

    print("PASS" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DistError as exc:
        print(f"build_dist.py: {exc}", file=sys.stderr)
        sys.exit(1)
