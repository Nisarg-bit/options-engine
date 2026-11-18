"""Stock-futures paper system: shared vocabulary, clock, calendar, config, rules.

Everything here is pure or nearly so. The vocabulary is explicit on purpose:
a trade has a DIRECTION (LONG / SHORT), every order has a SIDE (BUY / SELL) and
a PURPOSE (OPEN / CLOSE), and the two are never inferred from each other
anywhere else in the package -- they come from the helpers below.

    LONG  opens with BUY,  closes with SELL
    SHORT opens with SELL, closes with BUY (to cover)
"""
import copy
import hashlib
import json
import math
import operator
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config", "default.json")
SCHEMA_VERSION = 1

LONG, SHORT = "LONG", "SHORT"
BUY, SELL = "BUY", "SELL"
OPEN, CLOSE = "OPEN", "CLOSE"
MODES = ("intraday", "swing")
REGIMES = ("BULLISH", "BEARISH", "SIDEWAYS", "UNAVAILABLE")


def open_side(direction):
    return {LONG: BUY, SHORT: SELL}[direction]


def close_side(direction):
    return {LONG: SELL, SHORT: BUY}[direction]


def exit_quote_side(direction):
    """Which side of the book a position can actually be closed against.
    A long sells into the bid; a short buys back from the ask."""
    return {LONG: "bid", SHORT: "ask"}[direction]


def gross_pnl(direction, entry_price, exit_price, units):
    if direction == LONG:
        return (exit_price - entry_price) * units
    return (entry_price - exit_price) * units


# ---------------------------------------------------------------- prices

def round_to_tick(price, tick, side):
    """Adverse rounding: a BUY rounds up, a SELL rounds down, so slippage is
    never flattered by rounding."""
    if not tick or tick <= 0:
        raise ValueError("tick size required")
    n = price / tick
    n = math.ceil(n - 1e-9) if side == BUY else math.floor(n + 1e-9)
    return round(n * tick, 6)


# ---------------------------------------------------------------- clocks

class SystemClock:
    def now(self):
        return datetime.now(IST)


class ReplayClock:
    """Injected clock for replay and tests. Never moves on its own."""

    def __init__(self, start):
        self._now = start.astimezone(IST)

    def now(self):
        return self._now

    def set(self, t):
        if t.astimezone(IST) < self._now:
            raise ValueError("replay clock cannot go backwards")
        self._now = t.astimezone(IST)

    def advance(self, **kw):
        self.set(self._now + timedelta(**kw))


def ist(d, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, dtime(h, m), tzinfo=IST)


def iso(t):
    return None if t is None else t.astimezone(IST).isoformat(timespec="seconds")


def parse_ts(s):
    if s is None or isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s).astimezone(IST)


# ---------------------------------------------------------------- calendar

_XCAL = {}


def _exchange_calendar(name):
    """exchange_calendars is slow to build (seconds); one instance per process."""
    if name not in _XCAL:
        try:
            import exchange_calendars as xc
            _XCAL[name] = xc.get_calendar(name)
        except Exception:  # noqa: BLE001
            _XCAL[name] = None
    return _XCAL[name]


class Calendar:
    """Exchange sessions from exchange_calendars (the same XBOM calendar
    pipeline/market.py uses) minus any configured extra holidays. Falls back
    to weekdays only if the library is unavailable or out of range."""

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.open = cfg.get("session_open", "09:15")
        self.close = cfg.get("session_close", "15:30")
        self.extra = {date.fromisoformat(d) for d in cfg.get("extra_holidays", [])}
        self._cal = _exchange_calendar(cfg.get("exchange", "XBOM"))
        self._sessions = {}

    def is_session(self, d):
        hit = self._sessions.get(d)
        if hit is None:
            hit = self._sessions[d] = self._is_session(d)
        return hit

    def _is_session(self, d):
        if d in self.extra or d.weekday() >= 5:
            return False
        if self._cal is not None:
            import pandas as pd
            ts = pd.Timestamp(d)
            if self._cal.first_session <= ts <= self._cal.last_session:
                return ts in self._cal.sessions
        return True

    def next_session(self, d, n=1):
        k = 0
        while k < n:
            d = d + timedelta(days=1)
            if self.is_session(d):
                k += 1
        return d

    def prev_session(self, d, n=1):
        k = 0
        while k < n:
            d = d - timedelta(days=1)
            if self.is_session(d):
                k += 1
        return d

    def sessions_between(self, a, b):
        """Sessions strictly after a, up to and including b."""
        out, d = 0, a
        while d < b:
            d = d + timedelta(days=1)
            if self.is_session(d):
                out += 1
        return out

    def bounds(self, d):
        return ist(d, self.open), ist(d, self.close)

    def in_session(self, t):
        t = t.astimezone(IST)
        if not self.is_session(t.date()):
            return False
        a, b = self.bounds(t.date())
        return a <= t <= b

    def session_of(self, t):
        """The session a timestamp belongs to (None outside sessions)."""
        return t.astimezone(IST).date() if self.in_session(t) else None


# ---------------------------------------------------------------- config

def load_config(path=None, overrides=None):
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    if overrides:
        deep_merge(cfg, overrides)
    return cfg


def deep_merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def config_hash(cfg):
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def validate_config(cfg):
    """Problems that make a config unusable; [] when fine."""
    errs = []
    for mode in MODES:
        p = cfg.get("profiles", {}).get(mode)
        if not p:
            errs.append(f"{mode}: profile missing")
            continue
        dm = p.get("direction_map", {})
        for r in REGIMES:
            for d in dm.get(r, []):
                if d not in (LONG, SHORT):
                    errs.append(f"{mode}: direction_map {r} has {d}")
        ex = p.get("exit_plan") or {}
        if not ex.get("stop"):
            errs.append(f"{mode}: exit plan has no stop")
        if mode == "intraday":
            if not p.get("exit_deadline"):
                errs.append("intraday: exit_deadline missing")
            elif p["entry_window"][1] >= p["exit_deadline"]:
                errs.append("intraday: entry cutoff must be before the exit deadline")
        if mode == "swing" and "pre_expiry_buffer_sessions" not in p.get("contract", {}):
            errs.append("swing: contract.pre_expiry_buffer_sessions missing")
    return errs


# ---------------------------------------------------------------- rules

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
       "==": operator.eq, "!=": operator.ne}


def evaluate(rule, metrics):
    """Evaluate {"all": [[metric, op, value], ...]} against metrics.

    Returns (status, checks) where status is PASS, FAIL or UNAVAILABLE.
    UNAVAILABLE wins over FAIL only when no check failed outright: a rule
    that has already failed on data it has does not need the missing part."""
    checks, missing, failed = [], False, False
    for metric, op, value in rule.get("all", []):
        v = metrics.get(metric)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            checks.append({"metric": metric, "op": op, "threshold": value, "value": None, "result": "UNAVAILABLE"})
            missing = True
            continue
        ok = OPS[op](v, value)
        failed |= not ok
        checks.append({"metric": metric, "op": op, "threshold": value,
                       "value": round(v, 4) if isinstance(v, float) else v,
                       "result": "PASS" if ok else "FAIL"})
    if failed:
        return "FAIL", checks
    if missing:
        return "UNAVAILABLE", checks
    return "PASS", checks


def describe(checks):
    return "; ".join(f"{c['metric']} {c['value']} {c['op']} {c['threshold']}: {c['result']}" for c in checks)
