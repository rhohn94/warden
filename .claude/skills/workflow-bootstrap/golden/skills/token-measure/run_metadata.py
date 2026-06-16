#!/usr/bin/env python3
"""run_metadata.py — per-run metadata telemetry artifact writer (#82, v3.14).

Emits ONE structured JSON file per Grimoire run under
`.claude/cache/runs/<run_id>.json`, in the shape Mission Control's Telemetry
(Pulse) pillar documented (its `GrimoireRunSource` ingests these files). Today
`token-measure` only *prints* a token table and the release ledgers are markdown
prose, so Pulse falls back to file-mtime guesses; this artifact gives it an
accurate, idempotent structured source.

The four token classes are NOT re-derived here — they are sourced from the
sibling `token-measure` helper `parse_usage.py` (the single source of truth for
token accounting, where dedup-by-requestId lives). This module only shapes the
result and adds the run-context fields.

Design: docs/design/run-metadata-artifact-design.md.
Standard: Python 3 stdlib-only (docs/design/scripting-unification-design.md),
plus the in-repo `parse_usage.py` reuse.

Artifact (`.claude/cache/runs/<run_id>.json`, gitignored):
  run_id, paradigm, profile, model, release,
  tokens{input,output,cache_read,cache_creation},
  items, items_passed, outcome in {pass,fail,partial},
  wall_clock_secs, started_at (ISO-8601 UTC).

Usage:
  run_metadata.py --emit --outcome pass [--release 3.14] [--transcript T]
                  [--config grimoire-config.json] [--run-id ID] [...] [--root DIR]
  run_metadata.py --validate FILE
  run_metadata.py --self-test
Exit 0 on success; 2 on bad input / validation failure. Graceful by design:
absent inputs degrade to null/zero — an emit never crashes a release.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import sys

# ── Constants (no magic numbers inline) ─────────────────────────────────────
RUNS_REL = os.path.join(".claude", "cache", "runs")
VALID_OUTCOMES = ("pass", "fail", "partial")
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_creation")
# Truncated length (hex chars) of the content-hash run_id fallback. Widen this
# named constant if collisions ever appear in practice.
HASH_ID_LEN = 16
JSON_INDENT = 2
# Sibling helper that owns token accounting (single source of truth).
PARSE_USAGE_FILENAME = "parse_usage.py"


class RunMetadataError(Exception):
    """Raised on bad input or schema/enum validation failure (→ exit 2)."""


class TokenSplit:
    """The four Anthropic token classes for one run.

    A thin value object so the rest of the writer never hand-rolls the class
    names. Built either from explicit counts or — the common path — from a
    `token-measure` session parse (see `from_transcript`), and degrades to
    all-zero when no transcript is available (graceful telemetry, never a
    release blocker).
    """

    def __init__(self, input=0, output=0, cache_read=0, cache_creation=0):
        self.input = int(input or 0)
        self.output = int(output or 0)
        self.cache_read = int(cache_read or 0)
        self.cache_creation = int(cache_creation or 0)

    @classmethod
    def zero(cls):
        """All-zero split — the graceful-degradation default."""
        return cls()

    @classmethod
    def from_transcript(cls, transcript_path, helper_dir):
        """Build a split from a session .jsonl via token-measure's parse_usage.

        Returns (TokenSplit, dominant_model_or_None). Degrades to (zero, None)
        on any failure (missing transcript, import error, empty usage) — it
        prints a one-line note to stderr and never raises, so a release is never
        broken by an unavailable transcript.
        """
        if not transcript_path or not os.path.isfile(transcript_path):
            print("run_metadata: no transcript at %r — tokens default to zero"
                  % transcript_path, file=sys.stderr)
            return cls.zero(), None
        parse = _load_parse_usage(helper_dir)
        if parse is None:
            print("run_metadata: parse_usage.py unavailable — tokens default to zero",
                  file=sys.stderr)
            return cls.zero(), None
        try:
            _ops, session, _tier = parse.parse_transcript(
                transcript_path, by_operation=False)
        except Exception as exc:  # TranscriptError or any read failure
            print("run_metadata: transcript parse failed (%s) — tokens zero" % exc,
                  file=sys.stderr)
            return cls.zero(), None
        split = cls(input=session.input, output=session.output,
                    cache_read=session.cache_read,
                    cache_creation=session.cache_creation)
        return split, _dominant_model_from_parse(parse, transcript_path)

    def as_dict(self):
        return {k: getattr(self, k) for k in TOKEN_CLASSES}


class RunContext:
    """The non-token run-context inputs for one run record.

    Every field is optional; an absent input stays None (or 0 for the integer
    counters) so the artifact degrades gracefully rather than failing. Validates
    the cross-field rules (outcome enum, items_passed <= items) at build time.
    """

    def __init__(self, outcome, paradigm=None, profile=None, model=None,
                 release=None, items=0, items_passed=0, wall_clock_secs=None,
                 started_at=None, run_id=None):
        self.outcome = outcome
        self.paradigm = paradigm
        self.profile = profile
        self.model = model
        self.release = release
        self.items = int(items or 0)
        self.items_passed = int(items_passed or 0)
        self.wall_clock_secs = (int(wall_clock_secs)
                                if wall_clock_secs is not None else None)
        self.started_at = started_at
        self.run_id = run_id

    def validate(self):
        """Enforce the outcome enum and the items_passed<=items invariant."""
        if self.outcome not in VALID_OUTCOMES:
            raise RunMetadataError(
                "outcome must be one of %s, got %r"
                % (", ".join(VALID_OUTCOMES), self.outcome))
        if self.items < 0 or self.items_passed < 0:
            raise RunMetadataError("items / items_passed must be non-negative")
        if self.items_passed > self.items:
            raise RunMetadataError(
                "items_passed (%d) cannot exceed items (%d)"
                % (self.items_passed, self.items))
        return True


class RunMetadata:
    """One per-run telemetry record: build, validate, serialize, atomic save.

    Wraps a `TokenSplit` and a `RunContext` into the Pulse-documented artifact
    shape, computes the `run_id` dedup key (stable composed id when a release
    context exists, content hash otherwise), and persists one file per run to
    `.claude/cache/runs/<run_id>.json`. Re-emitting the same run overwrites the
    same path, so Pulse ingest is idempotent.
    """

    REQUIRED_FIELDS = ("run_id", "paradigm", "profile", "model", "release",
                       "tokens", "items", "items_passed", "outcome",
                       "wall_clock_secs", "started_at")

    def __init__(self, context, tokens):
        self._ctx = context
        self._tokens = tokens
        self._ctx.validate()
        # model from context wins; else fall back to a transcript-derived value
        # the caller may have stashed on the context.
        self._run_id = context.run_id or self._compute_run_id()

    # ── run_id dedup key ──
    def _compute_run_id(self):
        """Stable composed id when possible, else a content hash (idempotent)."""
        epoch = self._started_epoch()
        if self._ctx.release and epoch is not None:
            return "v%s-%d" % (self._ctx.release, epoch)
        if self._ctx.release:
            return "v%s-%s" % (self._ctx.release, self._content_hash())
        return "run-%s" % self._content_hash()

    def _started_epoch(self):
        """Parse started_at (ISO-8601) into an int epoch, or None."""
        if not self._ctx.started_at:
            return None
        text = self._ctx.started_at.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())

    def _content_hash(self):
        """SHA-256 over the record minus run_id, truncated — content dedup key."""
        payload = self._body(run_id="")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return digest[:HASH_ID_LEN]

    # ── shape ──
    def _body(self, run_id):
        """The full artifact dict with a caller-supplied run_id value."""
        return {
            "run_id": run_id,
            "paradigm": self._ctx.paradigm,
            "profile": self._ctx.profile,
            "model": self._ctx.model,
            "release": self._ctx.release,
            "tokens": self._tokens.as_dict(),
            "items": self._ctx.items,
            "items_passed": self._ctx.items_passed,
            "outcome": self._ctx.outcome,
            "wall_clock_secs": self._ctx.wall_clock_secs,
            "started_at": self._ctx.started_at,
        }

    def as_dict(self):
        return self._body(run_id=self._run_id)

    def serialize(self):
        """Deterministic JSON text (sorted keys) — stable for diffs + dedup."""
        return json.dumps(self.as_dict(), indent=JSON_INDENT, sort_keys=True)

    @property
    def run_id(self):
        return self._run_id

    # ── persistence ──
    def save(self, root):
        """Validate then atomically write to .claude/cache/runs/<run_id>.json."""
        validate_record(self.as_dict())
        runs_dir = os.path.join(root, RUNS_REL)
        os.makedirs(runs_dir, exist_ok=True)
        path = os.path.join(runs_dir, self._run_id + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(self.serialize() + "\n")
        os.replace(tmp, path)
        return path


# ── parse_usage.py reuse (importlib, flavor-portable) ───────────────────────
def _load_parse_usage(helper_dir):
    """Load the sibling parse_usage.py module, or None if unavailable."""
    path = os.path.join(helper_dir, PARSE_USAGE_FILENAME)
    if not os.path.isfile(path):
        return None
    mod_name = "tm_parse_usage"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        # Register before exec: parse_usage.py defines @dataclass classes whose
        # processing looks the module up in sys.modules (a CPython requirement).
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # any import/exec failure → graceful degrade
        sys.modules.pop(mod_name, None)
        return None


def _dominant_model_from_parse(parse, transcript_path):
    """Re-run the parse once more (per-op) to recover the dominant model id.

    parse_transcript returns a tier multiplier, not the model string, so we
    derive the dominant model from the per-operation model sets. Returns None
    on any failure (graceful).
    """
    try:
        ops, _session, _tier = parse.parse_transcript(transcript_path,
                                                       by_operation=True)
    except Exception:
        return None
    counts = {}
    for op in ops:
        for m in getattr(op, "models", set()) or ():
            counts[m] = counts.get(m, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


# ── config auto-fill ────────────────────────────────────────────────────────
def _config_value(config_path, *keys):
    """Read a nested .value field from grimoire-config.json, or None."""
    if not config_path or not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    node = data
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node if isinstance(node, str) else None


# ── record validation (used by save + --validate) ───────────────────────────
def validate_record(record):
    """Validate an artifact dict against the schema + outcome enum.

    Raises RunMetadataError on any violation. Reusable by both the writer (on
    save) and the `--validate` command (on an existing file).
    """
    if not isinstance(record, dict):
        raise RunMetadataError("artifact must be a JSON object")
    for f in RunMetadata.REQUIRED_FIELDS:
        if f not in record:
            raise RunMetadataError("missing required field: %s" % f)
    if not isinstance(record["run_id"], str) or not record["run_id"]:
        raise RunMetadataError("run_id must be a non-empty string")
    if record["outcome"] not in VALID_OUTCOMES:
        raise RunMetadataError("outcome must be one of %s"
                               % ", ".join(VALID_OUTCOMES))
    tokens = record["tokens"]
    if not isinstance(tokens, dict):
        raise RunMetadataError("tokens must be an object")
    for cls in TOKEN_CLASSES:
        if cls not in tokens or not isinstance(tokens[cls], int) \
                or isinstance(tokens[cls], bool):
            raise RunMetadataError("tokens.%s must be an integer" % cls)
    for cnt in ("items", "items_passed"):
        if not isinstance(record[cnt], int) or isinstance(record[cnt], bool) \
                or record[cnt] < 0:
            raise RunMetadataError("%s must be a non-negative integer" % cnt)
    if record["items_passed"] > record["items"]:
        raise RunMetadataError("items_passed cannot exceed items")
    return True


# ── command handlers ────────────────────────────────────────────────────────
def cmd_emit(args, helper_dir):
    """Build + write a run artifact from args; return the written path."""
    tokens, derived_model = TokenSplit.from_transcript(args.transcript, helper_dir)
    if tokens.input == 0 and tokens.output == 0 \
            and tokens.cache_read == 0 and tokens.cache_creation == 0 \
            and args.transcript is None:
        tokens = TokenSplit.zero()
    paradigm = args.paradigm or _config_value(args.config, "work-paradigm", "value")
    profile = args.profile or _config_value(args.config, "model-effort-profile", "value")
    model = args.model or derived_model
    ctx = RunContext(
        outcome=args.outcome, paradigm=paradigm, profile=profile, model=model,
        release=args.release, items=args.items, items_passed=args.items_passed,
        wall_clock_secs=args.wall_clock_secs, started_at=args.started_at,
        run_id=args.run_id)
    record = RunMetadata(ctx, tokens)
    return record.save(args.root)


def cmd_validate(path):
    """Validate an existing artifact file. Raises RunMetadataError on failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        raise RunMetadataError("no artifact at %s" % path)
    except (OSError, json.JSONDecodeError) as exc:
        raise RunMetadataError("cannot read %s: %s" % (path, exc))
    validate_record(record)
    return record


# ── self-test ────────────────────────────────────────────────────────────────
def _self_test():
    import tempfile
    failures = []
    helper_dir = os.path.dirname(os.path.abspath(__file__))

    with tempfile.TemporaryDirectory() as d:
        # full context → stable composed run_id (release + epoch).
        ctx = RunContext(outcome="pass", paradigm="Noir", profile="Medium",
                         model="claude-opus-4-8", release="3.14", items=3,
                         items_passed=3, wall_clock_secs=5400,
                         started_at="2026-06-05T18:00:00Z")
        rec = RunMetadata(ctx, TokenSplit(input=10, output=2, cache_read=5,
                                          cache_creation=1))
        if not rec.run_id.startswith("v3.14-"):
            failures.append("stable run_id should be v3.14-<epoch>: %r" % rec.run_id)
        path = rec.save(d)
        if not os.path.isfile(path):
            failures.append("save did not write the artifact")
        loaded = cmd_validate(path)
        if loaded["tokens"]["input"] != 10 or loaded["outcome"] != "pass":
            failures.append("round-trip lost fields: %r" % loaded)

        # all-absent context → degrades to nulls/zeros, content-hash run_id.
        bare = RunMetadata(RunContext(outcome="fail"), TokenSplit.zero())
        bd = bare.as_dict()
        if bd["paradigm"] is not None or bd["release"] is not None:
            failures.append("absent context should be null: %r" % bd)
        if bd["tokens"] != {"input": 0, "output": 0, "cache_read": 0,
                            "cache_creation": 0}:
            failures.append("absent tokens should be all-zero: %r" % bd["tokens"])
        if not bare.run_id.startswith("run-"):
            failures.append("no-release run_id should be content hash: %r"
                            % bare.run_id)

        # content hash is deterministic for identical inputs (idempotent).
        a = RunMetadata(RunContext(outcome="partial", items=2, items_passed=1),
                        TokenSplit(input=7))
        b = RunMetadata(RunContext(outcome="partial", items=2, items_passed=1),
                        TokenSplit(input=7))
        if a.run_id != b.run_id:
            failures.append("identical runs should share run_id: %r vs %r"
                            % (a.run_id, b.run_id))
        if a.run_id == bare.run_id:
            failures.append("different runs should differ in run_id")

        # re-emit overwrites the same path (idempotent ingest).
        p1 = a.save(d)
        p2 = b.save(d)
        if p1 != p2:
            failures.append("re-emit should write the same path: %r vs %r" % (p1, p2))

        # outcome enum is validated.
        try:
            RunContext(outcome="bogus").validate()
            failures.append("invalid outcome should raise")
        except RunMetadataError:
            pass

        # items_passed > items is rejected.
        try:
            RunContext(outcome="pass", items=1, items_passed=2).validate()
            failures.append("items_passed > items should raise")
        except RunMetadataError:
            pass

        # explicit --run-id wins.
        fixed = RunMetadata(RunContext(outcome="pass", run_id="custom-id"),
                            TokenSplit.zero())
        if fixed.run_id != "custom-id":
            failures.append("explicit run_id should win: %r" % fixed.run_id)

        # validate_record rejects a malformed artifact.
        try:
            validate_record({"run_id": "x"})  # missing fields
            failures.append("malformed artifact should raise")
        except RunMetadataError:
            pass
        try:
            bad = a.as_dict(); bad["tokens"]["input"] = "nope"
            validate_record(bad)
            failures.append("non-int token should raise")
        except RunMetadataError:
            pass

        # token reuse from a synthetic transcript (exercises parse_usage path).
        tpath = os.path.join(d, "session.jsonl")
        rid = "req-1"
        lines = [
            json.dumps({"type": "user", "message": {"content": "hi"}}),
            json.dumps({"type": "assistant", "requestId": rid, "message": {
                "model": "claude-opus-4-8", "usage": {
                    "input_tokens": 100, "output_tokens": 20,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 3}}}),
            # duplicate requestId fragment must be deduped by parse_usage.
            json.dumps({"type": "assistant", "requestId": rid, "message": {
                "model": "claude-opus-4-8", "usage": {
                    "input_tokens": 100, "output_tokens": 20,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 3}}}),
        ]
        with open(tpath, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        split, model = TokenSplit.from_transcript(tpath, helper_dir)
        if (split.input, split.output, split.cache_read, split.cache_creation) \
                != (100, 20, 5, 3):
            failures.append("token reuse mismatch (dedup?): %r" % split.as_dict())
        if model != "claude-opus-4-8":
            failures.append("dominant model not recovered: %r" % model)

        # missing transcript degrades to zero without raising.
        z, zm = TokenSplit.from_transcript(os.path.join(d, "nope.jsonl"), helper_dir)
        if not (z.input == 0 and z.output == 0) or zm is not None:
            failures.append("missing transcript should degrade to zero/None")

        # serialize is deterministic.
        if a.serialize() != a.serialize():
            failures.append("serialize non-deterministic")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("run_metadata self-test: OK (stable+hash run_id, save/validate "
          "round-trip, absent-context degrade, idempotent re-emit, outcome enum, "
          "items_passed<=items, explicit run_id, malformed rejection, token "
          "reuse+dedup, dominant-model recovery, missing-transcript degrade, "
          "determinism)")
    return 0


def main(argv=None):
    helper_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(
        description="Emit a per-run metadata telemetry artifact (#82).")
    ap.add_argument("--root", default=".")
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--validate", metavar="FILE")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--run-id")
    ap.add_argument("--paradigm")
    ap.add_argument("--profile")
    ap.add_argument("--model")
    ap.add_argument("--release")
    ap.add_argument("--items", type=int, default=0)
    ap.add_argument("--items-passed", dest="items_passed", type=int, default=0)
    ap.add_argument("--outcome")
    ap.add_argument("--wall-clock-secs", dest="wall_clock_secs", type=int)
    ap.add_argument("--started-at", dest="started_at")
    ap.add_argument("--transcript")
    ap.add_argument("--config", help="path to grimoire-config.json for auto-fill")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    try:
        if args.emit:
            if not args.outcome:
                ap.error("--emit requires --outcome")
            path = cmd_emit(args, helper_dir)
            print(path)
        elif args.validate:
            cmd_validate(args.validate)
            print("run_metadata: %s is valid" % args.validate)
        else:
            ap.error("one of --emit / --validate / --self-test")
    except RunMetadataError as exc:
        print("run_metadata: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
