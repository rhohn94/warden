#!/usr/bin/env python3
"""sync_deps_engine.py — engine internals for the Dependency Channel consumer.

Holds the object model behind `sync_deps.py`:

  - SyncDepsError         a typed error carrying a process exit code.
  - VendorToml            parse + (for --update) rewrite the human intent file.
  - VendorLock            deterministic write-if-changed of the JSON lock.
  - ReleaseFetcher        abstract fetch surface; GhReleaseFetcher (network via
                          `gh`) and OfflineFetcher (a local fixture dir) subclass it.
  - ArtifactKind          base class for per-kind extract semantics, with
                          AssetBundleKind and VendoredCrateKind subclasses.
  - SyncDepsEngine        the orchestrator wiring resolve -> fetch -> verify ->
                          place -> lock, plus --check / --offline.

Security invariants (design §11, Ollama-RCE avoidance) are concentrated here:
asset-name allowlist (validate_asset_name), fixed app-owned staging dir,
verify-sha256-BEFORE-placement (Verifier.verify_or_raise called before any
extract), checksum-before-signature, never trusting a server-supplied path.

stdlib-only. `tomllib` (Python 3.11+) parses vendor.toml; the lock + read/gate
path stay pure JSON.

Design: docs/design/dependency-channel-design.md §3-§4.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import datetime

try:
    import tomllib
except ModuleNotFoundError as _exc:  # pragma: no cover - explicit floor
    raise SystemExit(
        "sync_deps requires Python 3.11+ (tomllib). A <3.11 fallback parser is a "
        "non-goal this release."
    ) from _exc


# ── Constants ───────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1
HASH_PREFIX = "sha256:"               # content-hash convention (component-registry)
STAGING_DIRNAME = ".sync-deps-staging"  # fixed, app-owned, under the repo root

# Process exit codes — mirror sync-from-upstream's 0/1/2 verify semantics.
EXIT_OK = 0
EXIT_VIOLATION = 1   # hard refuse: checksum mismatch, drift, missing inputs
EXIT_DEGRADED = 2    # loud degrade: SHA256SUMS absent / no entry (never silent)

VALID_CHANNELS = {"stable", "beta"}
VALID_KINDS = {"asset-bundle", "vendored-crate", "app-binary"}

# Asset-name allowlist — the engine only ever reads/writes these basenames from a
# release. Server-supplied names outside this set are refused (never trusted).
RELEASE_JSON_NAME = "release.json"
SHA256SUMS_NAME = "SHA256SUMS"
# A release artifact basename: letters/digits/dot/dash/underscore, ending in a
# known archive suffix. No path separators, no leading dot, no traversal.
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:tar\.gz|tgz)$")


class SyncDepsError(Exception):
    """An engine error carrying the process exit code to surface."""

    def __init__(self, message, exit_code=EXIT_VIOLATION):
        super().__init__(message)
        self.exit_code = exit_code


# ── Hashing helpers ─────────────────────────────────────────────────────────

def sha256_of_file(path):
    """Return the hex sha256 of a file, read in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_sha256(root):
    """Deterministic content hash over a placed file tree.

    Sorted relpaths, each contributing `"<relpath>\\0<filesha>\\n"`, hashed
    together. Re-derivable offline from the vendored bytes — this is what
    `--check` and the conformance gate recompute to detect drift. Directories
    and symlinks contribute structurally via their files only; the hash is stable
    across machines (relpaths use forward slashes).
    """
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            entries.append((rel, sha256_of_file(full)))
    entries.sort()
    h = hashlib.sha256()
    for rel, filesha in entries:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(filesha.encode("utf-8"))
        h.update(b"\n")
    return HASH_PREFIX + h.hexdigest()


# ── Security: asset-name allowlist ──────────────────────────────────────────

def validate_artifact_name(name):
    """Refuse any artifact basename that is not a plain archive name.

    Guards against server-supplied names with path separators, traversal, or an
    unexpected extension (the Ollama-RCE name-trust pattern, avoided).
    """
    if not name or name != os.path.basename(name) or not _ARTIFACT_RE.match(name):
        raise SyncDepsError(
            f"refusing untrusted/invalid artifact name: {name!r}", EXIT_VIOLATION
        )
    return name


# ── SHA256SUMS parsing + verification ───────────────────────────────────────

def parse_sha256sums(text):
    """Parse `<hex>␠␠<basename>` lines into {basename: hex}. Format unchanged."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        out[os.path.basename(name)] = digest.lower()
    return out


class Verifier:
    """Verify a downloaded artifact's sha256 against its SHA256SUMS entry.

    The single security checkpoint that MUST run before any filesystem placement.
    Mismatch is a hard refuse (EXIT_VIOLATION); an absent SHA256SUMS / missing
    entry is a loud degrade (EXIT_DEGRADED) — never a silent skip.
    """

    def __init__(self, sums_map):
        self._sums = sums_map

    def expected(self, basename):
        return self._sums.get(basename)

    def verify_or_raise(self, artifact_path):
        """Return the verified hex sha256, or raise SyncDepsError."""
        base = os.path.basename(artifact_path)
        got = sha256_of_file(artifact_path)
        if self._sums is None:
            raise SyncDepsError(
                f"release has no {SHA256SUMS_NAME} — refusing to vendor {base} "
                f"UNVERIFIED (loud degrade, not a silent skip).",
                EXIT_DEGRADED,
            )
        want = self._sums.get(base)
        if want is None:
            raise SyncDepsError(
                f"{SHA256SUMS_NAME} lists no entry for {base} — refusing "
                f"UNVERIFIED (loud degrade).",
                EXIT_DEGRADED,
            )
        if want.lower() != got.lower():
            raise SyncDepsError(
                f"checksum MISMATCH for {base} (want {want}, got {got}) — "
                f"HARD REFUSE; vendor tree left untouched.",
                EXIT_VIOLATION,
            )
        return got


# ── vendor.toml (intent) ────────────────────────────────────────────────────

class DepSpec:
    """One resolved `[deps.<name>]` block from vendor.toml.

    Carries the human-authored intent for a single dependency and validates the
    required/optional field set up front so the engine never acts on a half-spec.
    """

    def __init__(self, name, data):
        self.name = name
        self.repo = self._require(data, "repo")
        self.channel = self._require(data, "channel")
        self.version = self._require(data, "version")
        self.artifact = validate_artifact_name(self._require(data, "artifact"))
        self.dest = self._require(data, "dest")
        self.kind = data.get("kind", "asset-bundle")
        self.strip_components = int(data.get("strip_components", 0))
        self.extract = data.get("extract")  # optional subset allowlist (list[str])
        if self.channel not in VALID_CHANNELS:
            raise SyncDepsError(
                f"[deps.{name}] channel must be one of {sorted(VALID_CHANNELS)}",
                EXIT_VIOLATION,
            )
        if self.kind not in VALID_KINDS:
            raise SyncDepsError(
                f"[deps.{name}] kind must be one of {sorted(VALID_KINDS)}",
                EXIT_VIOLATION,
            )
        if self.extract is not None and not isinstance(self.extract, list):
            raise SyncDepsError(
                f"[deps.{name}] extract must be a list of path prefixes",
                EXIT_VIOLATION,
            )
        # dest must stay inside the repo (no traversal / absolute escape).
        if os.path.isabs(self.dest) or ".." in self.dest.split("/"):
            raise SyncDepsError(
                f"[deps.{name}] dest must be a repo-relative path: {self.dest!r}",
                EXIT_VIOLATION,
            )

    @staticmethod
    def _require(data, key):
        if key not in data:
            raise SyncDepsError(f"vendor.toml dep missing required '{key}'",
                                EXIT_VIOLATION)
        return data[key]

    @property
    def release_tag(self):
        return f"v{self.version}"


class VendorToml:
    """Reader/rewriter for the human-authored vendor.toml intent file.

    Parsed with tomllib (write-side only). For `--update` the pin is rewritten
    with a minimal line-oriented edit so unrelated formatting/comments survive.
    """

    def __init__(self, path):
        self.path = path

    def exists(self):
        return os.path.isfile(self.path)

    def load(self):
        """Return (schema_version, {name: DepSpec})."""
        if not self.exists():
            raise SyncDepsError(f"no vendor.toml at {self.path}", EXIT_VIOLATION)
        with open(self.path, "rb") as fh:
            data = tomllib.load(fh)
        schema = data.get("schema_version", SCHEMA_VERSION)
        deps = {}
        for name, block in (data.get("deps") or {}).items():
            deps[name] = DepSpec(name, block)
        return schema, deps

    def rewrite_pin(self, dep_name, new_version, new_artifact):
        """Rewrite the `version`/`artifact` of one [deps.<name>] block in place.

        Line-oriented so comments and unrelated deps are preserved. Writes
        atomically via temp + os.replace.
        """
        with open(self.path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        section = f"[deps.{dep_name}]"
        in_section = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_section = (stripped == section)
                continue
            if not in_section:
                continue
            if re.match(r"^\s*version\s*=", line):
                lines[i] = f'version = "{new_version}"\n'
            elif re.match(r"^\s*artifact\s*=", line):
                lines[i] = f'artifact = "{new_artifact}"\n'
        _atomic_write_text(self.path, "".join(lines))


# ── vendor.lock (resolved truth, JSON) ──────────────────────────────────────

class VendorLock:
    """Deterministic JSON `vendor.lock` reader/writer.

    The lock is JSON so the verifier and the conformance gate stay pure-stdlib
    on the read/gate path. Writes are idempotent: an unchanged pin yields a
    byte-identical file (write-if-changed), so a re-sync is a no-op.
    """

    def __init__(self, path):
        self.path = path

    def load(self):
        """Return the lock dict, or an empty skeleton if absent/empty."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
            if not text:
                return self._empty()
            return json.loads(text)
        except (OSError, ValueError):
            return self._empty()

    @staticmethod
    def _empty():
        return {"schema_version": SCHEMA_VERSION, "deps": {}}

    @staticmethod
    def serialize(lock):
        """Canonical serialization — sorted keys, two-space indent, trailing NL."""
        return json.dumps(lock, indent=2, sort_keys=True) + "\n"

    def write_if_changed(self, lock):
        """Write only when the serialized form differs. Returns True if written."""
        payload = self.serialize(lock)
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                if fh.read() == payload:
                    return False
        except OSError:
            pass
        _atomic_write_text(self.path, payload)
        return True


def _atomic_write_text(path, text):
    """Write text via a sibling temp file + os.replace (atomic on the same fs)."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ── Artifact-kind extract semantics ─────────────────────────────────────────

class ArtifactKind:
    """Base class: extract a verified artifact into a staging dir.

    Subclasses encode per-`kind` semantics (`strip_components` / `extract`
    allowlist). All extraction is hardened against tar path-traversal and is
    confined to the staging dir; placement into vendor/<dep>/ is the caller's
    atomic os.replace.
    """

    def __init__(self, spec):
        self.spec = spec

    def extract_into(self, artifact_path, staging_dir):
        """Extract `artifact_path` into `staging_dir` per the spec. Override."""
        raise NotImplementedError

    # — shared safe-extraction helpers —

    def _safe_members(self, tf, strip, allow):
        """Yield (member, relpath) for entries that survive strip + allowlist.

        Rejects absolute paths, traversal, and (defensively) non-regular special
        members. `strip` drops leading path components; `allow` (if set) keeps
        only entries whose post-strip relpath starts with an allowed prefix.
        """
        for member in tf.getmembers():
            name = member.name
            # Reject traversal / absolute before doing anything else.
            norm = os.path.normpath(name)
            if norm.startswith(("/", "..")) or os.path.isabs(name):
                raise SyncDepsError(
                    f"refusing tar member outside tree: {name!r}", EXIT_VIOLATION
                )
            parts = norm.split("/")
            if strip:
                if len(parts) <= strip:
                    continue  # stripped away entirely (e.g. the top dir itself)
                parts = parts[strip:]
            rel = "/".join(parts)
            if not rel or rel == ".":
                continue
            if allow and not any(
                rel == pfx.rstrip("/") or rel.startswith(pfx if pfx.endswith("/") else pfx + "/")
                for pfx in allow
            ):
                continue
            yield member, rel

    def _extract_members(self, tf, members, staging_dir):
        """Write the selected (member, relpath) pairs under staging_dir safely."""
        for member, rel in members:
            target = os.path.join(staging_dir, rel)
            # Final containment guard (defense in depth).
            real_root = os.path.realpath(staging_dir)
            real_target = os.path.realpath(os.path.join(staging_dir, rel))
            if not (real_target == real_root or real_target.startswith(real_root + os.sep)):
                raise SyncDepsError(
                    f"refusing extraction outside staging: {rel!r}", EXIT_VIOLATION
                )
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                # Preserve the executable bit only (no setuid/setgid/sticky).
                mode = 0o755 if (member.mode & 0o100) else 0o644
                os.chmod(target, mode)
            # symlinks / devices / fifos are intentionally skipped (not trusted).


class AssetBundleKind(ArtifactKind):
    """`asset-bundle`: extract the whole tarball honoring strip/extract."""

    def extract_into(self, artifact_path, staging_dir):
        with tarfile.open(artifact_path, "r:gz") as tf:
            members = list(self._safe_members(
                tf, self.spec.strip_components, self.spec.extract))
            if not members:
                raise SyncDepsError(
                    f"[deps.{self.spec.name}] extraction produced no files "
                    f"(strip_components/extract too aggressive?)", EXIT_VIOLATION)
            self._extract_members(tf, members, staging_dir)


class VendoredCrateKind(ArtifactKind):
    """`vendored-crate`: a Rust crate tarball with one top-level component.

    The producer prepends a `<dep>/` top-level dir and ships an include-subset;
    the consumer strips one leading component by default (design §4). An explicit
    `strip_components` / `extract` in vendor.toml overrides the default strip.
    """

    def extract_into(self, artifact_path, staging_dir):
        strip = self.spec.strip_components if self.spec.strip_components else 1
        with tarfile.open(artifact_path, "r:gz") as tf:
            members = list(self._safe_members(tf, strip, self.spec.extract))
            if not members:
                raise SyncDepsError(
                    f"[deps.{self.spec.name}] crate extraction produced no files",
                    EXIT_VIOLATION)
            self._extract_members(tf, members, staging_dir)


def make_kind(spec):
    """Factory: map a DepSpec.kind to its ArtifactKind handler."""
    if spec.kind == "vendored-crate":
        return VendoredCrateKind(spec)
    # asset-bundle (and app-binary, which vendors its bundle the same way here)
    return AssetBundleKind(spec)


# ── Release fetchers (network vs offline fixture) ───────────────────────────

class FetchedRelease:
    """A resolved release: the three local file paths + parsed metadata."""

    def __init__(self, version, artifact_path, release_json, sums_map,
                 release_json_sha256, release_url):
        self.version = version
        self.artifact_path = artifact_path
        self.release_json = release_json
        self.sums_map = sums_map
        self.release_json_sha256 = release_json_sha256
        self.release_url = release_url


class ReleaseFetcher:
    """Abstract fetch surface — download release.json + SHA256SUMS + artifact.

    Subclasses place the three allowlisted basenames into a caller-owned staging
    dir and parse the metadata. They never touch vendor/<dep>/ and never trust a
    server-supplied path. `resolve_latest` powers `--update`.
    """

    def resolve_latest(self, spec):
        """Return the latest version string on the dep's channel (for --update)."""
        raise NotImplementedError

    def fetch(self, spec, staging_dir):
        """Download the 3 assets into staging_dir; return a FetchedRelease."""
        raise NotImplementedError

    # — shared parsing of the staged trio —

    def _load_staged(self, spec, staging_dir, release_url):
        artifact_path = os.path.join(staging_dir, spec.artifact)
        if not os.path.isfile(artifact_path):
            raise SyncDepsError(
                f"artifact {spec.artifact} not present after fetch", EXIT_VIOLATION)
        rj_path = os.path.join(staging_dir, RELEASE_JSON_NAME)
        release_json = {}
        rj_sha = None
        if os.path.isfile(rj_path):
            with open(rj_path, "r", encoding="utf-8") as fh:
                release_json = json.loads(fh.read())
            rj_sha = HASH_PREFIX + sha256_of_file(rj_path)
        sums_path = os.path.join(staging_dir, SHA256SUMS_NAME)
        sums_map = None
        if os.path.isfile(sums_path):
            with open(sums_path, "r", encoding="utf-8") as fh:
                sums_map = parse_sha256sums(fh.read())
        return FetchedRelease(
            version=spec.version,
            artifact_path=artifact_path,
            release_json=release_json,
            sums_map=sums_map,
            release_json_sha256=rj_sha,
            release_url=release_url,
        )


class GhReleaseFetcher(ReleaseFetcher):
    """Network fetcher using the `gh` CLI (the only network surface).

    Mirrors sync-from-upstream.sh's `fetch_release_asset`: pull exactly the
    allowlisted patterns into a fixed staging dir, retry without SHA256SUMS only
    to surface the loud-degrade path. Never invoked under --offline / --check.
    """

    def __init__(self, runner=None):
        # `runner(args) -> (rc, stdout, stderr)`; injectable for tests.
        self._run = runner or self._default_run

    @staticmethod
    def _default_run(args):  # pragma: no cover - exercised only with gh present
        proc = subprocess.run(args, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    @staticmethod
    def _slug(repo):
        return re.sub(r"\.git$", "",
                      re.sub(r"^.*github\.com[:/]+", "", repo)).strip("/")

    def resolve_latest(self, spec):
        slug = self._slug(spec.repo)
        prerelease = "true" if spec.channel == "beta" else "false"
        rc, out, err = self._run([
            "gh", "api", f"repos/{slug}/releases",
            "--jq", f'[.[] | select(.prerelease=={prerelease}) | '
                    f'.tag_name] | .[0:50] | .[]',
        ])
        if rc != 0:
            raise SyncDepsError(
                f"gh could not list releases for {slug}: {err.strip()}",
                EXIT_DEGRADED)
        tags = [t.strip().lstrip("v") for t in out.splitlines() if t.strip()]
        versions = [t for t in tags if re.match(r"^\d+\.\d+", t)]
        if not versions:
            raise SyncDepsError(
                f"no {spec.channel} release found for {slug}", EXIT_VIOLATION)
        versions.sort(key=_semver_key, reverse=True)
        return versions[0]

    def fetch(self, spec, staging_dir):
        slug = self._slug(spec.repo)
        tag = spec.release_tag
        # Allowlisted patterns only — never a server-derived name.
        args = ["gh", "release", "download", tag, "--repo", slug,
                "--pattern", spec.artifact,
                "--pattern", RELEASE_JSON_NAME,
                "--pattern", SHA256SUMS_NAME,
                "--dir", staging_dir]
        rc, out, err = self._run(args)
        if rc != 0:
            # Retry without SHA256SUMS only to reach the loud-degrade path.
            rc2, _, err2 = self._run([
                "gh", "release", "download", tag, "--repo", slug,
                "--pattern", spec.artifact, "--dir", staging_dir])
            if rc2 != 0:
                raise SyncDepsError(
                    f"gh release download failed for {slug}@{tag}: "
                    f"{(err or err2).strip()}", EXIT_VIOLATION)
        release_url = f"https://github.com/{slug}/releases/tag/{tag}"
        return self._load_staged(spec, staging_dir, release_url)


class OfflineFetcher(ReleaseFetcher):
    """Offline fetcher backed by a local fixture directory.

    Used by --self-test and any "no network" path. The fixture dir holds the
    three release files per dep, laid out as <fixture>/<repo-slug>/<tag>/.
    Performs zero gh/network calls.
    """

    def __init__(self, fixture_root):
        self.fixture_root = fixture_root

    def _release_dir(self, spec, version=None):
        slug = GhReleaseFetcher._slug(spec.repo)
        tag = f"v{version}" if version else spec.release_tag
        return os.path.join(self.fixture_root, slug, tag)

    def resolve_latest(self, spec):
        slug = GhReleaseFetcher._slug(spec.repo)
        base = os.path.join(self.fixture_root, slug)
        if not os.path.isdir(base):
            raise SyncDepsError(f"no offline fixture for {slug}", EXIT_VIOLATION)
        versions = []
        for tag in os.listdir(base):
            v = tag.lstrip("v")
            if re.match(r"^\d+\.\d+", v):
                versions.append(v)
        if not versions:
            raise SyncDepsError(f"no fixture releases for {slug}", EXIT_VIOLATION)
        versions.sort(key=_semver_key, reverse=True)
        return versions[0]

    def fetch(self, spec, staging_dir):
        src = self._release_dir(spec)
        if not os.path.isdir(src):
            raise SyncDepsError(
                f"offline fixture missing for {spec.name}@{spec.release_tag}",
                EXIT_VIOLATION)
        for base in (spec.artifact, RELEASE_JSON_NAME, SHA256SUMS_NAME):
            sp = os.path.join(src, base)
            if os.path.isfile(sp):
                shutil.copyfile(sp, os.path.join(staging_dir, base))
        release_url = f"file://{os.path.abspath(src)}"
        return self._load_staged(spec, staging_dir, release_url)


def _semver_key(v):
    """Sort key for an X.Y.Z[-pre] string (numeric, missing parts => 0)."""
    core = v.split("-", 1)[0]
    parts = []
    for p in core.split(".")[:3]:
        parts.append(int(p) if p.isdigit() else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


# ── The orchestrator ────────────────────────────────────────────────────────

class SyncDepsEngine:
    """Reconcile vendored deps from release channels and maintain vendor.lock.

    Wires the pipeline (resolve -> fetch -> verify-before-place -> atomic-replace
    -> write-if-changed lock) and the read-only `--check` / `--offline` modes.
    Network access is confined to the injected fetcher; `--offline` / `--check`
    use no fetcher at all.
    """

    def __init__(self, root=".", fetcher=None, offline_fetcher=None, clock=None):
        self.root = os.path.abspath(root)
        self.toml = VendorToml(os.path.join(self.root, "vendor.toml"))
        self.lock = VendorLock(os.path.join(self.root, "vendor.lock"))
        self._fetcher = fetcher
        self._offline_fetcher = offline_fetcher
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))

    # — staging —

    def _staging_root(self):
        return os.path.join(self.root, STAGING_DIRNAME)

    def _selected(self, only):
        _schema, deps = self.toml.load()
        if only:
            if only not in deps:
                raise SyncDepsError(f"no [deps.{only}] in vendor.toml",
                                    EXIT_VIOLATION)
            return {only: deps[only]}
        return deps

    def _network_fetcher(self):
        if self._fetcher is None:
            self._fetcher = GhReleaseFetcher()
        return self._fetcher

    # — sync (default + --update) —

    def sync(self, only=None, update=False):
        """Resolve + fetch + verify + place + lock for each selected dep."""
        deps = self._selected(only)
        lock = self.lock.load()
        lock.setdefault("deps", {})
        lock["schema_version"] = SCHEMA_VERSION
        fetcher = self._network_fetcher()
        staging_root = self._staging_root()
        os.makedirs(staging_root, exist_ok=True)
        try:
            for name, spec in sorted(deps.items()):
                if update:
                    latest = fetcher.resolve_latest(spec)
                    if latest != spec.version:
                        new_artifact = spec.artifact.replace(spec.version, latest)
                        validate_artifact_name(new_artifact)
                        self.toml.rewrite_pin(name, latest, new_artifact)
                        print(f"{name}: pin {spec.version} -> {latest} (rewritten)")
                        _schema, deps2 = self.toml.load()
                        spec = deps2[name]
                lock["deps"][name] = self._sync_one(spec, fetcher, staging_root)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
        written = self.lock.write_if_changed(lock)
        print("vendor.lock " + ("updated." if written else "unchanged (no-op)."))
        return EXIT_OK

    def _sync_one(self, spec, fetcher, staging_root):
        """Fetch+verify+place a single dep; return its lock entry dict."""
        dep_staging = tempfile.mkdtemp(dir=staging_root, prefix=f"{spec.name}-dl-")
        extract_staging = tempfile.mkdtemp(dir=staging_root, prefix=f"{spec.name}-x-")
        try:
            fetched = fetcher.fetch(spec, dep_staging)
            # SECURITY CHECKPOINT — verify sha256 BEFORE any placement.
            verifier = Verifier(fetched.sums_map)
            artifact_sha = verifier.verify_or_raise(fetched.artifact_path)
            # Extract into staging (still no touch of vendor/<dep>/).
            kind = make_kind(spec)
            kind.extract_into(fetched.artifact_path, extract_staging)
            placed_tree_sha = tree_sha256(extract_staging)
            # Atomic replace into place.
            self._atomic_place(extract_staging, spec.dest)
            extract_staging = None  # consumed by os.replace; don't rmtree it
            git_sha = fetched.release_json.get("git_sha")
            return {
                "version": spec.version,
                "channel": spec.channel,
                "git_sha": git_sha,
                "release_tag": spec.release_tag,
                "release_url": fetched.release_url,
                "artifact": spec.artifact,
                "artifact_sha256": HASH_PREFIX + artifact_sha,
                "tree_sha256": placed_tree_sha,
                "release_json_sha256": fetched.release_json_sha256,
                "signature_verified": False,  # signing deferred (v3.29 non-goal)
                "synced_at": self._timestamp(),
            }
        finally:
            shutil.rmtree(dep_staging, ignore_errors=True)
            if extract_staging:
                shutil.rmtree(extract_staging, ignore_errors=True)

    def _atomic_place(self, staged_tree, dest_rel):
        """Atomically replace `dest_rel` with the staged tree.

        Moves any existing tree aside, os.replaces the staged tree in, then
        removes the old tree — so a failure mid-way never leaves a half-written
        vendor dir. The staged tree and dest live under the same repo root, so
        os.replace stays on one filesystem.
        """
        dest = os.path.join(self.root, dest_rel)
        os.makedirs(os.path.dirname(dest) or self.root, exist_ok=True)
        backup = None
        if os.path.exists(dest):
            backup = dest + ".old-" + os.path.basename(tempfile.mktemp(prefix=""))
            os.replace(dest, backup)
        try:
            os.replace(staged_tree, dest)
        except OSError:
            if backup is not None:
                os.replace(backup, dest)  # roll back
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)

    def _timestamp(self):
        return self._clock().strftime("%Y-%m-%dT%H:%M:%SZ")

    # — --check (drift; write nothing) —

    def check(self, only=None):
        """Recompute tree_sha256 of vendored bytes vs the lock. Write nothing."""
        deps = self._selected(only)
        lock = self.lock.load()
        locked = lock.get("deps", {})
        drift = []
        for name, spec in sorted(deps.items()):
            entry = locked.get(name)
            if entry is None:
                drift.append(f"{name}: no vendor.lock entry")
                continue
            dest = os.path.join(self.root, spec.dest)
            if not os.path.isdir(dest):
                drift.append(f"{name}: vendored dir missing at {spec.dest}")
                continue
            observed = tree_sha256(dest)
            if observed != entry.get("tree_sha256"):
                drift.append(
                    f"{name}: tree_sha256 drift "
                    f"(locked {entry.get('tree_sha256')}, observed {observed})")
        if drift:
            for d in drift:
                print(f"DRIFT: {d}")
            return EXIT_VIOLATION
        print("sync-deps --check: no drift.")
        return EXIT_OK

    # — --offline (validate; zero network) —

    def offline_validate(self, only=None):
        """Assert vendored bytes match the lock with zero gh/network calls.

        Identical drift logic to --check, but framed as the build-time gate: a
        clean result means a build can proceed against vendor/<dep>/ offline.
        """
        rc = self.check(only=only)
        if rc == EXIT_OK:
            print("sync-deps --offline: vendored bytes validate against the lock "
                  "(no network used).")
        return rc


# ── Self-test (deterministic, stdlib-only, offline) ─────────────────────────

def _build_fixture_tarball(path, files):
    """Write a deterministic .tar.gz (sorted, mtime=0, fixed perms) of `files`.

    `files` maps arcname -> bytes. Mirrors the producer determinism contract so
    the artifact is byte-identical across rebuilds.
    """
    import io
    names = sorted(files)
    with tarfile.open(path, "w:gz", compresslevel=9) as tf:
        # Fix the gzip mtime indirectly by writing members deterministically.
        for arc in names:
            data = files[arc]
            info = tarfile.TarInfo(name=arc)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))


def _seed_release(fixture_root, slug, tag, artifact_name, files,
                  git_sha="0a1b2c3", with_sums=True, tamper=False):
    """Seed one offline release (artifact + release.json + SHA256SUMS)."""
    rel_dir = os.path.join(fixture_root, slug, tag)
    os.makedirs(rel_dir, exist_ok=True)
    artifact_path = os.path.join(rel_dir, artifact_name)
    _build_fixture_tarball(artifact_path, files)
    art_sha = sha256_of_file(artifact_path)
    release_json = {
        "schema_version": 1,
        "name": slug.split("/")[-1],
        "version": tag.lstrip("v"),
        "channel": "stable",
        "git_sha": git_sha,
        "artifact_kind": "asset-bundle",
        "primary_artifact": artifact_name,
        "primary_artifact_sha256": art_sha,
        "signature": None,
        "assets": [{"name": artifact_name, "sha256": art_sha,
                    "bytes": os.path.getsize(artifact_path)}],
    }
    rj_path = os.path.join(rel_dir, RELEASE_JSON_NAME)
    with open(rj_path, "w", encoding="utf-8") as fh:
        json.dump(release_json, fh, indent=2, sort_keys=True)
    if with_sums:
        recorded = art_sha if not tamper else ("0" * 64)
        rj_sha = sha256_of_file(rj_path)
        with open(os.path.join(rel_dir, SHA256SUMS_NAME), "w", encoding="utf-8") as fh:
            fh.write(f"{recorded}  {artifact_name}\n")
            fh.write(f"{rj_sha}  {RELEASE_JSON_NAME}\n")
    return rel_dir


def _write_vendor_toml(root, deps_block):
    with open(os.path.join(root, "vendor.toml"), "w", encoding="utf-8") as fh:
        fh.write("schema_version = 1\n\n")
        fh.write(deps_block)


def run_self_test():
    """Deterministic, offline, stdlib-only regression suite. Returns True/False."""
    failures = []

    def check(cond, label):
        if cond:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}")
            failures.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        fixture = os.path.join(tmp, "fixtures")
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        slug = "acme/widget"
        files = {
            "widget-v1.0.0/README.md": b"hello widget\n",
            "widget-v1.0.0/css/main.css": b"body{}\n",
            "widget-v1.0.0/fonts/a.woff": b"FONT\n",
        }
        _seed_release(fixture, slug, "v1.0.0", "widget-v1.0.0.tar.gz", files)
        _write_vendor_toml(repo,
            '[deps.widget]\n'
            'repo = "acme/widget"\n'
            'channel = "stable"\n'
            'version = "1.0.0"\n'
            'artifact = "widget-v1.0.0.tar.gz"\n'
            'dest = "vendor/widget"\n'
            'kind = "asset-bundle"\n'
            'strip_components = 1\n')

        fixed_clock = lambda: datetime.datetime(2026, 6, 13, 18, 20, 0,
                                                tzinfo=datetime.timezone.utc)
        eng = SyncDepsEngine(root=repo, fetcher=OfflineFetcher(fixture),
                             clock=fixed_clock)

        # 1) sync fetches + verifies + vendors + writes a correct lock.
        rc = eng.sync()
        check(rc == EXIT_OK, "sync exits 0")
        vendored = os.path.join(repo, "vendor", "widget")
        check(os.path.isfile(os.path.join(vendored, "README.md")),
              "vendored README placed (strip_components=1 applied)")
        check(os.path.isfile(os.path.join(vendored, "css", "main.css")),
              "vendored nested file placed")
        lock = json.load(open(os.path.join(repo, "vendor.lock")))
        entry = lock["deps"]["widget"]
        art_path = os.path.join(fixture, slug, "v1.0.0", "widget-v1.0.0.tar.gz")
        expected_art = HASH_PREFIX + sha256_of_file(art_path)
        check(entry["artifact_sha256"] == expected_art,
              "lock artifact_sha256 == SHA256SUMS entry")
        check(entry["tree_sha256"] == tree_sha256(vendored),
              "lock tree_sha256 == placed-bytes hash")
        check(entry["version"] == "1.0.0" and entry["channel"] == "stable",
              "lock records version + channel")
        check(entry["git_sha"] == "0a1b2c3", "lock records git_sha from release.json")
        check(entry["signature_verified"] is False, "signature_verified false (deferred)")
        check(entry["synced_at"] == "2026-06-13T18:20:00Z", "synced_at deterministic")

        # 2) re-sync with unchanged pin is a byte-identical no-op.
        before = open(os.path.join(repo, "vendor.lock")).read()
        eng.sync()
        after = open(os.path.join(repo, "vendor.lock")).read()
        check(before == after, "re-sync is a byte-identical lock no-op")

        # 3) --check passes clean, then detects drift, writing nothing.
        check(eng.check() == EXIT_OK, "--check clean after sync")
        with open(os.path.join(vendored, "README.md"), "ab") as fh:
            fh.write(b"tampered\n")
        lock_before = open(os.path.join(repo, "vendor.lock")).read()
        check(eng.check() == EXIT_VIOLATION, "--check exits nonzero on drift")
        check(open(os.path.join(repo, "vendor.lock")).read() == lock_before,
              "--check writes nothing on drift")
        # restore clean bytes for the offline test
        eng.sync()
        check(eng.offline_validate() == EXIT_OK,
              "--offline validates clean bytes with zero network")

        # 4) tampered artifact is HARD-REFUSED, vendor dir left untouched.
        repo2 = os.path.join(tmp, "repo2")
        os.makedirs(repo2)
        fixture2 = os.path.join(tmp, "fixtures2")
        _seed_release(fixture2, slug, "v1.0.0", "widget-v1.0.0.tar.gz", files,
                      tamper=True)
        _write_vendor_toml(repo2,
            '[deps.widget]\n'
            'repo = "acme/widget"\n'
            'channel = "stable"\n'
            'version = "1.0.0"\n'
            'artifact = "widget-v1.0.0.tar.gz"\n'
            'dest = "vendor/widget"\n'
            'kind = "asset-bundle"\n'
            'strip_components = 1\n')
        eng2 = SyncDepsEngine(root=repo2, fetcher=OfflineFetcher(fixture2),
                              clock=fixed_clock)
        refused = False
        try:
            eng2.sync()
        except SyncDepsError as exc:
            refused = (exc.exit_code == EXIT_VIOLATION)
        check(refused, "tampered artifact HARD-REFUSED (nonzero)")
        check(not os.path.exists(os.path.join(repo2, "vendor", "widget")),
              "vendor/<dep>/ left untouched on tamper")
        check(not os.path.exists(os.path.join(repo2, "vendor.lock")) or
              "widget" not in VendorLock(os.path.join(repo2, "vendor.lock")).load().get("deps", {}),
              "no lock entry written on tamper")

        # 5) absent SHA256SUMS is a loud degrade (EXIT_DEGRADED), not silent.
        repo3 = os.path.join(tmp, "repo3")
        os.makedirs(repo3)
        fixture3 = os.path.join(tmp, "fixtures3")
        _seed_release(fixture3, slug, "v1.0.0", "widget-v1.0.0.tar.gz", files,
                      with_sums=False)
        _write_vendor_toml(repo3,
            '[deps.widget]\n'
            'repo = "acme/widget"\nchannel = "stable"\nversion = "1.0.0"\n'
            'artifact = "widget-v1.0.0.tar.gz"\ndest = "vendor/widget"\n'
            'kind = "asset-bundle"\nstrip_components = 1\n')
        eng3 = SyncDepsEngine(root=repo3, fetcher=OfflineFetcher(fixture3),
                              clock=fixed_clock)
        degraded = False
        try:
            eng3.sync()
        except SyncDepsError as exc:
            degraded = (exc.exit_code == EXIT_DEGRADED)
        check(degraded, "absent SHA256SUMS => loud degrade (exit 2)")

        # 6) --update rewrites the pin and re-vendors to latest-on-channel.
        repo4 = os.path.join(tmp, "repo4")
        os.makedirs(repo4)
        fixture4 = os.path.join(tmp, "fixtures4")
        _seed_release(fixture4, slug, "v1.0.0", "widget-v1.0.0.tar.gz", files)
        files_v2 = {k.replace("v1.0.0", "v1.2.0"): v for k, v in files.items()}
        files_v2["widget-v1.2.0/NEW.txt"] = b"v2\n"
        _seed_release(fixture4, slug, "v1.2.0", "widget-v1.2.0.tar.gz", files_v2)
        _write_vendor_toml(repo4,
            '[deps.widget]\n'
            'repo = "acme/widget"\nchannel = "stable"\nversion = "1.0.0"\n'
            'artifact = "widget-v1.0.0.tar.gz"\ndest = "vendor/widget"\n'
            'kind = "asset-bundle"\nstrip_components = 1\n')
        eng4 = SyncDepsEngine(root=repo4, fetcher=OfflineFetcher(fixture4),
                              clock=fixed_clock)
        check(eng4.sync(update=True) == EXIT_OK, "--update exits 0")
        _schema, deps4 = VendorToml(os.path.join(repo4, "vendor.toml")).load()
        check(deps4["widget"].version == "1.2.0", "--update rewrote the pin to 1.2.0")
        check(deps4["widget"].artifact == "widget-v1.2.0.tar.gz",
              "--update rewrote the artifact name")
        lock4 = json.load(open(os.path.join(repo4, "vendor.lock")))
        check(lock4["deps"]["widget"]["version"] == "1.2.0",
              "lock reflects updated version")
        check(os.path.isfile(os.path.join(repo4, "vendor", "widget", "NEW.txt")),
              "updated bytes vendored")

        # 7) vendored-crate: default strip of one leading component + extract allowlist.
        repo5 = os.path.join(tmp, "repo5")
        os.makedirs(repo5)
        fixture5 = os.path.join(tmp, "fixtures5")
        crate_files = {
            "mycrate/src/lib.rs": b"pub fn x(){}\n",
            "mycrate/Cargo.toml": b"[package]\nname='mycrate'\n",
            "mycrate/tests/t.rs": b"#[test] fn t(){}\n",
        }
        _seed_release(fixture5, "acme/mycrate", "v0.3.0", "mycrate-v0.3.0.tar.gz",
                      crate_files)
        _write_vendor_toml(repo5,
            '[deps.mycrate]\n'
            'repo = "acme/mycrate"\nchannel = "stable"\nversion = "0.3.0"\n'
            'artifact = "mycrate-v0.3.0.tar.gz"\ndest = "vendor/mycrate"\n'
            'kind = "vendored-crate"\n'
            'extract = ["src/", "Cargo.toml"]\n')
        eng5 = SyncDepsEngine(root=repo5, fetcher=OfflineFetcher(fixture5),
                              clock=fixed_clock)
        check(eng5.sync() == EXIT_OK, "vendored-crate sync exits 0")
        crate_dest = os.path.join(repo5, "vendor", "mycrate")
        check(os.path.isfile(os.path.join(crate_dest, "src", "lib.rs")),
              "crate src/ vendored (one component stripped)")
        check(os.path.isfile(os.path.join(crate_dest, "Cargo.toml")),
              "crate Cargo.toml vendored")
        check(not os.path.exists(os.path.join(crate_dest, "tests")),
              "crate extract allowlist excluded tests/")

        # 8) asset-name allowlist refuses a traversal name.
        bad = False
        try:
            validate_artifact_name("../evil.tar.gz")
        except SyncDepsError:
            bad = True
        check(bad, "asset-name allowlist refuses traversal name")
        bad2 = False
        try:
            validate_artifact_name("evil.sh")
        except SyncDepsError:
            bad2 = True
        check(bad2, "asset-name allowlist refuses non-archive name")

        # 9) tar member traversal is refused during extraction.
        repo6 = os.path.join(tmp, "repo6")
        os.makedirs(repo6)
        fixture6 = os.path.join(tmp, "fixtures6")
        evil = {"../escape.txt": b"x\n", "ok/file.txt": b"y\n"}
        _seed_release(fixture6, "acme/evil", "v1.0.0", "evil-v1.0.0.tar.gz", evil)
        _write_vendor_toml(repo6,
            '[deps.evil]\n'
            'repo = "acme/evil"\nchannel = "stable"\nversion = "1.0.0"\n'
            'artifact = "evil-v1.0.0.tar.gz"\ndest = "vendor/evil"\n'
            'kind = "asset-bundle"\n')
        eng6 = SyncDepsEngine(root=repo6, fetcher=OfflineFetcher(fixture6),
                              clock=fixed_clock)
        traversal_refused = False
        try:
            eng6.sync()
        except SyncDepsError:
            traversal_refused = True
        check(traversal_refused, "tar member traversal refused on extract")

    print(f"\n{len(failures)} failure(s)." if failures else "\nall checks passed.")
    return not failures


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_self_test() else 1)
