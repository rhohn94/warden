#!/usr/bin/env python3
"""cost_budget.py — deterministic engine for the cost-governance budget layer (#GOV-1, v3.28).

Backs the `cost-budget` skill. Replaces the skill's prose-guided LLM arithmetic
(window rolling, threshold-crossing detection, `cost-utilization.json` ledger
math) with one tested, stdlib-only, deterministic implementation. The skill
calls this engine and *interprets* its structured output; it no longer
hand-computes percentages or hand-rolls the periodic window.

Scope (matches cost-budget/SKILL.md §2 + reference.md §3–§5, NOT a redesign):
  - parse a session transcript into a token total, reusing token-measure's
    `parse_usage.py` (the §B.2 accumulator source) — same per-class
    {input, output, cache_read, cache_creation} accounting;
  - roll the periodic budget window forward when `now >= window-start + period`
    (resetting `accumulated` to 0), per SKILL.md §2c;
  - accumulate the session's measured tokens into the ledger and persist it
    atomically to `.claude/cache/cost-utilization.json` (§3 format);
  - evaluate thresholds: emit each newly-crossed threshold once per window
    (tracked via `crossed-thresholds`), and report the active `on-approach`
    mode at the top threshold, per §2d / §4.

Soft governance only (design §B): aggregate-only, no per-agent isolation, no
hard mid-response block. This engine computes + persists + reports; it never
spawns, defers, or interrupts work — the skill/agent acts on the verdict.

Standard: Python 3 stdlib-only (docs/design/scripting-unification-design.md).

CLI:
  cost_budget.py measure --transcript FILE [--unit U]
  cost_budget.py evaluate --amount N [--thresholds 50,80,95] [--on-approach M]
                          [--period P] [--unit U] [--add-tokens N]
                          [--transcript FILE] [--ledger FILE] [--cache-dir DIR]
                          [--now ISO] [--no-persist]
  cost_budget.py --self-test
Exit 0 on success; 2 on bad input / budget error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Reuse token-measure's transcript parser — the §B.2 accumulator source. The
# two skills are siblings under .claude/skills/; add token-measure to the path
# so `parse_usage` imports cleanly whether run from the repo root or elsewhere.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TOKEN_MEASURE_DIR = os.path.join(_THIS_DIR, os.pardir, "token-measure")
if _TOKEN_MEASURE_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_TOKEN_MEASURE_DIR))

import parse_usage  # noqa: E402  (path adjusted above)

# ── Constants (no magic numbers inline) ─────────────────────────────────────
DEFAULT_THRESHOLDS = (50, 80, 95)
DEFAULT_ON_APPROACH = "warn-only"
DEFAULT_UNIT = "tokens"
DEFAULT_PERIOD = "session"
LEDGER_FILENAME = "cost-utilization.json"
CACHE_DIR = os.path.join(".claude", "cache")
PERCENT = 100.0
JSON_INDENT = 2

VALID_UNITS = ("tokens", "cost-units")
VALID_PERIODS = ("session", "daily", "weekly", "unlimited")
# Periods that persist a cross-session ledger and roll a window (SKILL.md §2c).
PERSISTENT_PERIODS = ("daily", "weekly")
PERIOD_DELTA = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}
VALID_ON_APPROACH = ("warn-only", "terse", "defer-non-critical", "pause-and-report")
# Unit abbreviations for the §5 warning string ("5M-token", "5K-token").
_MILLION = 1_000_000
_THOUSAND = 1_000


class BudgetError(Exception):
    """Raised on a malformed budget config or unreadable ledger (→ exit 2)."""


# ── Transcript measurement (reuses token-measure) ───────────────────────────
class Measurement:
    """Sum a session transcript's tokens via token-measure's parser.

    `unit: tokens` uses the raw four-class sum. `unit: cost-units` falls back to
    the raw sum with a pending note (the per-class/per-tier weighting table is a
    documented follow-up — SKILL.md §1 caveat); it never invents a weighting.
    """

    def __init__(self, unit=DEFAULT_UNIT):
        if unit not in VALID_UNITS:
            raise BudgetError("unknown unit %r (expected one of %s)"
                              % (unit, ", ".join(VALID_UNITS)))
        self.unit = unit

    def measure(self, transcript_path):
        """Return {total, classes, unit, note?} for one transcript."""
        try:
            _ops, session, _tier = parse_usage.parse_transcript(
                transcript_path, by_operation=False)
        except parse_usage.TranscriptError as exc:
            raise BudgetError(str(exc)) from exc
        classes = {
            "input": session.input,
            "output": session.output,
            "cache_read": session.cache_read,
            "cache_creation": session.cache_creation,
        }
        raw_total = sum(classes.values())
        result = {"total": raw_total, "classes": classes, "unit": self.unit}
        if self.unit == "cost-units":
            # No weighting table yet — report raw, flagged. (SKILL.md §2b.)
            result["note"] = "cost-unit weighting pending; raw token count used"
        return result


# ── Periodic ledger (cost-utilization.json) ─────────────────────────────────
class UtilizationLedger:
    """Read / roll / write the `.claude/cache/cost-utilization.json` ledger.

    Mirrors SKILL.md §2c + reference.md §3 exactly: a periodic budget persists
    {window-start, period, accumulated, unit, last-updated, crossed-thresholds};
    on each read the window rolls forward if `now >= window-start + period`
    (resetting accumulated and crossed-thresholds). `session`/`unlimited`
    periods never persist a window here.
    """

    def __init__(self, path):
        self.path = path

    @staticmethod
    def _parse_iso(value):
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise BudgetError("malformed ISO datetime in ledger: %r" % value) from exc

    def load(self):
        """Return the stored ledger dict, or None if absent."""
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise BudgetError("cannot read ledger %s: %s" % (self.path, exc)) from exc
        if not isinstance(data, dict):
            raise BudgetError("ledger %s is not a JSON object" % self.path)
        return data

    @classmethod
    def fresh(cls, now, period, unit):
        """A brand-new window starting at `now`."""
        stamp = now.isoformat()
        return {
            "window-start": stamp,
            "period": period,
            "accumulated": 0,
            "unit": unit,
            "last-updated": stamp,
            "crossed-thresholds": [],
        }

    def roll_if_due(self, ledger, now, period, unit):
        """Roll the window forward if `now >= window-start + period`.

        Returns (ledger, rolled: bool). A period/unit change, or a due window,
        starts a fresh window. Resetting drops accumulated + crossed-thresholds.
        """
        if ledger is None:
            return self.fresh(now, period, unit), True
        if ledger.get("period") != period or ledger.get("unit") != unit:
            # Config changed under the ledger — start clean for the new policy.
            return self.fresh(now, period, unit), True
        start = self._parse_iso(ledger.get("window-start"))
        delta = PERIOD_DELTA[period]
        if now >= start + delta:
            return self.fresh(now, period, unit), True
        # Carry the existing window forward, normalising missing fields.
        ledger.setdefault("accumulated", 0)
        ledger.setdefault("crossed-thresholds", [])
        return ledger, False

    def write(self, ledger):
        """Persist the ledger atomically (temp + os.replace)."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=JSON_INDENT, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)


# ── Threshold evaluation ─────────────────────────────────────────────────────
class ThresholdEvaluator:
    """Detect newly-crossed thresholds and resolve the active on-approach mode.

    Deterministic restatement of SKILL.md §2d: walk thresholds ascending; a
    threshold is crossed when `pct >= threshold`; emit each at most once per
    window (against `already_crossed`); the configured on-approach mode is
    active only once the TOP threshold is crossed.
    """

    def __init__(self, thresholds, on_approach=DEFAULT_ON_APPROACH):
        if not thresholds:
            thresholds = list(DEFAULT_THRESHOLDS)
        for t in thresholds:
            if not isinstance(t, (int, float)):
                raise BudgetError("threshold %r is not numeric" % t)
        if on_approach not in VALID_ON_APPROACH:
            raise BudgetError("unknown on-approach mode %r (expected one of %s)"
                              % (on_approach, ", ".join(VALID_ON_APPROACH)))
        self.thresholds = sorted(set(thresholds))
        self.on_approach = on_approach

    @staticmethod
    def utilization_pct(accumulated, amount):
        if amount <= 0:
            raise BudgetError("budget amount must be positive, got %r" % amount)
        return (accumulated / amount) * PERCENT

    def evaluate(self, accumulated, amount, already_crossed):
        """Return the evaluation verdict for the current accumulation.

        already_crossed: thresholds already emitted this window (suppresses
        re-emission). Returns newly-crossed, the full crossed set, the top
        crossed threshold, whether the on-approach mode is active, and pct.
        """
        pct = self.utilization_pct(accumulated, amount)
        already = set(already_crossed or [])
        crossed_now = [t for t in self.thresholds if pct >= t]
        newly = [t for t in crossed_now if t not in already]
        all_crossed = sorted(already.union(crossed_now))
        top_threshold = self.thresholds[-1] if self.thresholds else None
        top_crossed = top_threshold is not None and pct >= top_threshold
        active_mode = self.on_approach if top_crossed else None
        return {
            "pct": pct,
            "newly_crossed": newly,
            "crossed": all_crossed,
            "highest_crossed": crossed_now[-1] if crossed_now else None,
            "top_threshold": top_threshold,
            "on_approach_active": top_crossed,
            "active_mode": active_mode,
        }


# ── Warning string (§5 format) ──────────────────────────────────────────────
def _abbrev(amount):
    """Render a budget amount as the §5 header abbreviation (5M / 500K / 1234).

    The header uses the clean form (whole multiples drop the decimal: 5M, 500K).
    """
    if amount >= _MILLION and amount % _MILLION == 0:
        return "%dM" % (amount // _MILLION)
    if amount >= _MILLION:
        return "%.1fM" % (amount / _MILLION)
    if amount >= _THOUSAND and amount % _THOUSAND == 0:
        return "%dK" % (amount // _THOUSAND)
    if amount >= _THOUSAND:
        return "%.1fK" % (amount / _THOUSAND)
    return str(int(amount))


def _abbrev_decimal(amount):
    """Render the used/total fraction with one decimal (§5: `4.0M/5.0M`)."""
    if amount >= _MILLION:
        return "%.1fM" % (amount / _MILLION)
    if amount >= _THOUSAND:
        return "%.1fK" % (amount / _THOUSAND)
    return str(int(amount))


def threshold_warning(pct, threshold, amount, accumulated, period, mode,
                      is_top, unit=DEFAULT_UNIT):
    """Build the one-line §5 threshold warning string.

    `Budget: 80% of 5M-token daily budget used (4.0M/5.0M). Mode: <mode> now active.`
    The mode clause is appended only for the top threshold (§5: lower
    thresholds are informational and omit the mode clause).
    """
    unit_abbrev = "%s-%s" % (_abbrev(amount), "token" if unit == "tokens" else unit)
    used = "%s/%s" % (_abbrev_decimal(accumulated), _abbrev_decimal(amount))
    line = ("Budget: %d%% of %s %s budget used (%s)."
            % (round(threshold), unit_abbrev, period, used))
    if is_top and mode:
        line += " Mode: %s now active." % mode
    return line


# ── Engine facade ────────────────────────────────────────────────────────────
class CostBudgetEngine:
    """Bind a budget config + ledger location; expose measure / evaluate."""

    def __init__(self, amount, thresholds=None, on_approach=DEFAULT_ON_APPROACH,
                 period=DEFAULT_PERIOD, unit=DEFAULT_UNIT, cache_dir=CACHE_DIR,
                 ledger_path=None):
        if period not in VALID_PERIODS:
            raise BudgetError("unknown reset-period %r (expected one of %s)"
                              % (period, ", ".join(VALID_PERIODS)))
        self.amount = amount
        self.period = period
        self.unit = unit
        self.evaluator = ThresholdEvaluator(thresholds, on_approach)
        self.ledger_path = ledger_path or os.path.join(cache_dir, LEDGER_FILENAME)
        self.ledger = UtilizationLedger(self.ledger_path)

    @property
    def persistent(self):
        return self.period in PERSISTENT_PERIODS

    def measure(self, transcript_path):
        return Measurement(self.unit).measure(transcript_path)

    def evaluate(self, add_tokens, now=None, persist=True):
        """Roll the window, accumulate `add_tokens`, evaluate thresholds.

        For session/unlimited periods nothing is persisted; accumulation is the
        in-call `add_tokens`. For daily/weekly the ledger rolls + persists.
        `unlimited` tracks + reports but never activates on-approach (§2c).
        Returns a structured verdict the skill interprets; emits no work.
        """
        if now is None:
            now = datetime.now(timezone.utc).astimezone()

        rolled = False
        if self.persistent:
            stored = self.ledger.load()
            ledger, rolled = self.ledger.roll_if_due(stored, now, self.period,
                                                      self.unit)
            ledger["accumulated"] = int(ledger.get("accumulated", 0)) + int(add_tokens)
            ledger["last-updated"] = now.isoformat()
            accumulated = ledger["accumulated"]
            already = ledger.get("crossed-thresholds", [])
        else:
            ledger = None
            accumulated = int(add_tokens)
            already = []

        verdict = self.evaluator.evaluate(accumulated, self.amount, already)
        verdict["accumulated"] = accumulated
        verdict["amount"] = self.amount
        verdict["period"] = self.period
        verdict["unit"] = self.unit
        verdict["window_rolled"] = rolled
        verdict["persistent"] = self.persistent

        # `unlimited` tracks + reports but never activates on-approach (§2c).
        if self.period == "unlimited":
            verdict["on_approach_active"] = False
            verdict["active_mode"] = None

        # Build the per-newly-crossed warning lines (§5).
        warnings = []
        top = verdict["top_threshold"]
        for t in verdict["newly_crossed"]:
            warnings.append(threshold_warning(
                verdict["pct"], t, self.amount, accumulated, self.period,
                verdict["active_mode"], is_top=(t == top and
                                                verdict["on_approach_active"]),
                unit=self.unit))
        verdict["warnings"] = warnings

        if self.persistent:
            # Record the now-crossed thresholds so they never re-emit (§2d).
            ledger["crossed-thresholds"] = verdict["crossed"]
            if persist:
                self.ledger.write(ledger)
            verdict["ledger"] = ledger

        return verdict


# ── Self-test ────────────────────────────────────────────────────────────────
def _fixture_transcript():
    """A minimal two-turn transcript with known per-class usage."""
    rows = [
        {"type": "user", "message": {"content": "first prompt"}},
        {"type": "assistant", "requestId": "r1",
         "message": {"model": "claude-sonnet", "usage": {
             "input_tokens": 100, "output_tokens": 200,
             "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}}},
        # streamed duplicate of r1 — must be deduped by requestId.
        {"type": "assistant", "requestId": "r1",
         "message": {"model": "claude-sonnet", "usage": {
             "input_tokens": 100, "output_tokens": 200,
             "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}}},
        {"type": "user", "message": {"content": "second prompt"}},
        {"type": "assistant", "requestId": "r2",
         "message": {"model": "claude-sonnet", "usage": {
             "input_tokens": 40, "output_tokens": 60,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
    ]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def _self_test():  # noqa: C901 — linear test driver, readability over splitting
    import tempfile

    failures = []
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)

    # 1) Measurement reuses parse_usage and sums classes (dedup by requestId).
    with tempfile.TemporaryDirectory() as root:
        tpath = os.path.join(root, "session.jsonl")
        with open(tpath, "w", encoding="utf-8") as fh:
            fh.write(_fixture_transcript())
        m = Measurement().measure(tpath)
        # r1 counted once: 100+200+50+10 = 360; r2: 40+60 = 100; total 460.
        if m["total"] != 460:
            failures.append("measure total: %r (expected 460)" % m["total"])
        if m["classes"]["output"] != 260:
            failures.append("measure output: %r (expected 260)" % m["classes"]["output"])
        cu = Measurement("cost-units").measure(tpath)
        if "note" not in cu:
            failures.append("cost-units measure should carry a pending note")

    # 2) Threshold evaluation: crossings + once-per-window + top mode.
    ev = ThresholdEvaluator([50, 80, 95], "defer-non-critical")
    v = ev.evaluate(accumulated=4_000_000, amount=5_000_000, already_crossed=[])
    if v["newly_crossed"] != [50, 80]:
        failures.append("80%% case newly_crossed: %r" % v["newly_crossed"])
    if v["on_approach_active"]:
        failures.append("80%% should not activate top (95) mode")
    # already-crossed suppresses re-emission.
    v2 = ev.evaluate(4_000_000, 5_000_000, already_crossed=[50])
    if v2["newly_crossed"] != [80]:
        failures.append("re-emit suppression failed: %r" % v2["newly_crossed"])
    # top threshold crossed -> mode active.
    v3 = ev.evaluate(4_800_000, 5_000_000, already_crossed=[50, 80])
    if not v3["on_approach_active"] or v3["active_mode"] != "defer-non-critical":
        failures.append("95%% should activate defer-non-critical: %r" % v3)
    if v3["newly_crossed"] != [95]:
        failures.append("95%% newly_crossed: %r" % v3["newly_crossed"])
    # zero crossings below the floor.
    v4 = ev.evaluate(100, 5_000_000, already_crossed=[])
    if v4["newly_crossed"] or v4["on_approach_active"]:
        failures.append("low utilization should cross nothing: %r" % v4)
    # bad amount raises.
    try:
        ev.evaluate(1, 0, [])
        failures.append("zero amount should raise")
    except BudgetError:
        pass

    # 3) Ledger window rolling: fresh, carry, and roll-when-due.
    with tempfile.TemporaryDirectory() as root:
        lpath = os.path.join(root, ".claude", "cache", LEDGER_FILENAME)
        led = UtilizationLedger(lpath)
        # fresh window when absent.
        l0, rolled0 = led.roll_if_due(None, now, "daily", "tokens")
        if not rolled0 or l0["accumulated"] != 0:
            failures.append("absent ledger should start fresh: %r" % l0)
        # within-window carry (12h later, daily).
        l0["accumulated"] = 1000
        led.write(l0)
        stored = led.load()
        l1, rolled1 = led.roll_if_due(stored, now + timedelta(hours=12),
                                      "daily", "tokens")
        if rolled1 or l1["accumulated"] != 1000:
            failures.append("within-window must carry accumulated: %r" % l1)
        # roll when due (25h later, daily).
        l2, rolled2 = led.roll_if_due(stored, now + timedelta(hours=25),
                                      "daily", "tokens")
        if not rolled2 or l2["accumulated"] != 0:
            failures.append("due window must roll + reset: %r" % l2)
        # period change forces a fresh window.
        l3, rolled3 = led.roll_if_due(stored, now + timedelta(hours=1),
                                      "weekly", "tokens")
        if not rolled3:
            failures.append("period change should start fresh window")

    # 4) Engine end-to-end: persistent daily ledger accumulates + persists,
    #    crossed-thresholds recorded, atomic round-trip.
    with tempfile.TemporaryDirectory() as root:
        lpath = os.path.join(root, ".claude", "cache", LEDGER_FILENAME)
        eng = CostBudgetEngine(amount=5_000_000, thresholds=[50, 80, 95],
                               on_approach="defer-non-critical", period="daily",
                               unit="tokens", ledger_path=lpath)
        r1 = eng.evaluate(add_tokens=2_600_000, now=now)  # 52% -> crosses 50
        if r1["newly_crossed"] != [50] or r1["accumulated"] != 2_600_000:
            failures.append("engine first eval: %r" % r1)
        if not r1["warnings"]:
            failures.append("engine should emit a 50%% warning")
        # second eval same window: accumulate to 4.0M -> 80%, not re-emit 50.
        r2 = eng.evaluate(add_tokens=1_400_000, now=now + timedelta(hours=1))
        if r2["accumulated"] != 4_000_000:
            failures.append("engine accumulation: %r" % r2["accumulated"])
        if r2["newly_crossed"] != [80]:
            failures.append("engine second eval newly: %r" % r2["newly_crossed"])
        # ledger persisted with crossed-thresholds [50, 80].
        persisted = json.load(open(lpath, encoding="utf-8"))
        if persisted["crossed-thresholds"] != [50, 80]:
            failures.append("persisted crossed-thresholds: %r"
                            % persisted["crossed-thresholds"])
        if persisted["accumulated"] != 4_000_000:
            failures.append("persisted accumulated: %r" % persisted["accumulated"])
        # third eval crosses the top -> mode active.
        r3 = eng.evaluate(add_tokens=1_000_000, now=now + timedelta(hours=2))
        if not r3["on_approach_active"] or r3["active_mode"] != "defer-non-critical":
            failures.append("engine top-threshold mode: %r" % r3)
        # rolling: a day later resets accumulation.
        r4 = eng.evaluate(add_tokens=100, now=now + timedelta(days=2))
        if not r4["window_rolled"] or r4["accumulated"] != 100:
            failures.append("engine window roll: %r" % r4)

    # 5) session period: in-memory only, no ledger file written.
    with tempfile.TemporaryDirectory() as root:
        lpath = os.path.join(root, ".claude", "cache", LEDGER_FILENAME)
        eng = CostBudgetEngine(amount=1000, thresholds=[50], period="session",
                               ledger_path=lpath)
        rs = eng.evaluate(add_tokens=600, now=now)
        if rs["accumulated"] != 600 or rs["newly_crossed"] != [50]:
            failures.append("session eval: %r" % rs)
        if os.path.exists(lpath):
            failures.append("session period must not write a ledger file")

    # 6) unlimited period: tracks but never activates on-approach.
    eng_u = CostBudgetEngine(amount=1000, thresholds=[50], period="unlimited",
                             on_approach="pause-and-report")
    ru = eng_u.evaluate(add_tokens=999, now=now)
    if ru["on_approach_active"] or ru["active_mode"] is not None:
        failures.append("unlimited must never activate on-approach: %r" % ru)

    # 7) warning string format matches §5.
    w = threshold_warning(80.0, 80, 5_000_000, 4_000_000, "daily",
                          "defer-non-critical", is_top=True)
    expected = ("Budget: 80% of 5M-token daily budget used (4.0M/5.0M)."
                " Mode: defer-non-critical now active.")
    if w != expected:
        failures.append("warning string: %r" % w)
    # non-top omits the mode clause.
    w2 = threshold_warning(50.0, 50, 5_000_000, 2_500_000, "daily",
                           "defer-non-critical", is_top=False)
    if "Mode:" in w2:
        failures.append("non-top warning must omit mode clause: %r" % w2)

    # 8) config validation rejects bad enums.
    for bad in (("unit", lambda: Measurement("bogus")),
                ("on-approach", lambda: ThresholdEvaluator([50], "bogus")),
                ("period", lambda: CostBudgetEngine(amount=1, period="bogus"))):
        try:
            bad[1]()
            failures.append("%s validation should raise" % bad[0])
        except BudgetError:
            pass

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("cost_budget self-test: OK (measure reuse + dedup, threshold crossing "
          "+ once-per-window + top mode, ledger fresh/carry/roll, engine "
          "accumulate/persist/roll, session in-memory, unlimited never-trigger, "
          "§5 warning string, enum validation)")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────
JSON_COMPACT = (",", ":")


def _emit(obj):
    print(json.dumps(obj, separators=JSON_COMPACT, default=str))


def _parse_thresholds(raw):
    if not raw:
        return list(DEFAULT_THRESHOLDS)
    try:
        return [float(x) if "." in x else int(x)
                for x in raw.split(",") if x.strip()]
    except ValueError as exc:
        raise BudgetError("malformed --thresholds %r: %s" % (raw, exc)) from exc


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Deterministic engine for the cost-governance budget (#GOV-1).")
    ap.add_argument("verb", nargs="?", help="measure|evaluate")
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--amount", type=float, default=None)
    ap.add_argument("--thresholds", default=None)
    ap.add_argument("--on-approach", default=DEFAULT_ON_APPROACH, dest="on_approach")
    ap.add_argument("--period", default=DEFAULT_PERIOD)
    ap.add_argument("--unit", default=DEFAULT_UNIT)
    ap.add_argument("--add-tokens", type=int, default=0, dest="add_tokens")
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--cache-dir", default=CACHE_DIR, dest="cache_dir")
    ap.add_argument("--now", default=None)
    ap.add_argument("--no-persist", action="store_true", dest="no_persist")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.verb:
        ap.error("a verb is required (or --self-test)")

    try:
        if args.verb == "measure":
            if not args.transcript:
                ap.error("measure requires --transcript")
            _emit(Measurement(args.unit).measure(args.transcript))
            return 0
        if args.verb == "evaluate":
            if args.amount is None:
                ap.error("evaluate requires --amount")
            eng = CostBudgetEngine(
                amount=args.amount,
                thresholds=_parse_thresholds(args.thresholds),
                on_approach=args.on_approach, period=args.period,
                unit=args.unit, cache_dir=args.cache_dir,
                ledger_path=args.ledger)
            add = args.add_tokens
            if args.transcript:
                add += Measurement(args.unit).measure(args.transcript)["total"]
            now = (datetime.fromisoformat(args.now) if args.now else None)
            _emit(eng.evaluate(add_tokens=add, now=now,
                               persist=not args.no_persist))
            return 0
        ap.error("unknown verb: %s" % args.verb)
    except BudgetError as exc:
        print("cost_budget: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
