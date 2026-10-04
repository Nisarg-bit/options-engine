"""Volatility on a trading-minute clock, and two-sided barrier probabilities.

WHY THIS IS A SEPARATE MODULE

`pricing.py` is the workbook, cell for cell, including the places the workbook
is arguably wrong. That is deliberate and it stays. This module is the
improvement, so its effect can be measured rather than assumed -- the Phase 2
rule.

The specific defect it exists to fix is `pricing.Volatility`:

    self.expiry_vol = self.daily_vol * sqrt(max(days_to_expiry, 1.0))

The floor at one day is sensible for a workbook that only ever thinks in whole
days -- it stops sigma collapsing on expiry morning and reporting a POP near
1.0 on a position about to be decided. But it makes every sub-day horizon
identical. A 09:30 entry exiting at 15:15 and the same trade opened at 14:00
get the same sigma, which is wrong by a factor of more than two. Intraday
signals are unbuildable until that floor is gone.

TWO CLOCKS, ON PURPOSE

  calendar  the workbook's: sigma scales with sqrt(calendar days), weekends
            included. Weekly and monthly horizons keep using this, so their
            numbers do not move.

  session   trading minutes: one full 09:15-15:30 session is 1.0 day, and a
            partial session is its fraction. This is what intraday and 0DTE
            use, and it is the only clock on which "how much of the day is
            left" means anything.

TWO PROBABILITIES, ALSO ON PURPOSE

  terminal  P(inside the zone AT the exit moment). Comparable with the weekly
            POP already in the journal.

  no-touch  P(never touched either breakeven at any point). Always lower, and
            the honest number if a drawdown would take you out.

The gap between them is the drawdown you have to be willing to sit through.
That gap is the point of computing both.

Model is Bachelier throughout -- terminal price Normal(spot, sigma) -- because
that is what the workbook assumes and mixing models mid-stack would make the
comparison meaningless.
"""

from datetime import datetime, time as dtime, timedelta
from math import erf, sqrt

TRADING_DAYS_BASIS = 365.0          # Option_Chain!B16, kept for comparability

SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)
SESSION_MINUTES = 375               # 09:15 to 15:30 inclusive of neither end


# --------------------------------------------------------------- normal dist

def norm_cdf(z):
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


# ------------------------------------------------------------------- clocks

def _mins(t):
    return t.hour * 60 + t.minute + t.second / 60.0


def clamp_to_session(t):
    """A time, pinned inside the session. Before the open reads as the open."""
    m = _mins(t)
    lo, hi = _mins(SESSION_OPEN), _mins(SESSION_CLOSE)
    return min(max(m, lo), hi)


def minutes_between(start, end):
    """Trading minutes between two times on the SAME session."""
    return max(0.0, clamp_to_session(end) - clamp_to_session(start))


def horizon_days(start, end, full_sessions_between=0):
    """Horizon length in SESSION days. 1.0 == one whole 09:15-15:30 session.

    `start` and `end` are datetimes in IST. `full_sessions_between` is the
    count of complete trading sessions strictly between their two dates --
    the caller supplies it because only the exchange calendar knows which
    days those are, and this module deliberately has no calendar dependency
    so it stays testable offline.
    """
    if end < start:
        return 0.0
    if start.date() == end.date():
        return minutes_between(start.time(), end.time()) / SESSION_MINUTES
    head = minutes_between(start.time(), SESSION_CLOSE) / SESSION_MINUTES
    tail = minutes_between(SESSION_OPEN, end.time()) / SESSION_MINUTES
    return head + float(full_sessions_between) + tail


def calendar_days(start, end):
    """The workbook's clock: plain elapsed days, weekends and all."""
    return max(0.0, (end - start).total_seconds() / 86400.0)


# -------------------------------------------------------------- volatility

class Volatility:
    """`pricing.Volatility` with the one-day floor removed.

    vix is a DECIMAL FRACTION (0.11 for 11%), same convention as pricing.py.
    `days` may be fractional and may be far below 1.0 -- that is the whole
    reason this class exists.
    """

    __slots__ = ("daily_vol", "horizon_vol", "daily_stdev", "stdev", "days")

    def __init__(self, spot, vix, days):
        if days < 0:
            raise ValueError(f"negative horizon: {days}")
        self.days = float(days)
        self.daily_vol = vix / sqrt(TRADING_DAYS_BASIS)
        self.horizon_vol = self.daily_vol * sqrt(self.days)
        self.daily_stdev = spot * self.daily_vol
        self.stdev = spot * self.horizon_vol

    def __repr__(self):
        return (f"Volatility(days={self.days:.4f}, "
                f"stdev={self.stdev:.2f} pts)")


# ----------------------------------------------------------- probabilities

def terminal_inside(spot, lower, upper, sigma):
    """P(lower < S_T < upper) under Normal(spot, sigma). The usual POP."""
    if sigma <= 0:
        return 1.0 if lower < spot < upper else 0.0
    return norm_cdf((upper - spot) / sigma) - norm_cdf((lower - spot) / sigma)


def no_touch(spot, lower, upper, sigma, terms=12):
    """P(the path never reaches either barrier before the horizon ends).

    Driftless arithmetic Brownian motion killed at two barriers. The density
    of the surviving paths comes from the method of images: reflect the
    starting point in both barriers, and again in each reflection, alternating
    sign. Integrating that density over (lower, upper) gives the survival
    probability as a pair of alternating sums.

    `terms` is how many images each way. The series converges geometrically
    once sigma is smaller than the corridor width, which is the regime every
    real structure lives in; 12 is far more than enough there and still cheap.
    Validated against Monte Carlo in test_vol_time.py rather than against the
    workbook, because the workbook has no equivalent to check against.
    """
    if not (lower < spot < upper):
        return 0.0
    if sigma <= 0:
        return 1.0

    d = upper - lower
    total = 0.0
    for n in range(-terms, terms + 1):
        shift = 2.0 * n * d
        # direct image
        total += (norm_cdf((upper - spot + shift) / sigma)
                  - norm_cdf((lower - spot + shift) / sigma))
        # reflection of the start in the lower barrier
        mirror = 2.0 * lower - spot
        total -= (norm_cdf((upper - mirror + shift) / sigma)
                  - norm_cdf((lower - mirror + shift) / sigma))
    return min(1.0, max(0.0, total))


def touch(spot, lower, upper, sigma, terms=12):
    """P(either barrier is reached at some point). 1 - no_touch."""
    return 1.0 - no_touch(spot, lower, upper, sigma, terms=terms)


def both(spot, lower, upper, sigma):
    """Terminal and no-touch together, plus the gap between them.

    The gap is the probability of finishing inside having been outside on the
    way -- which is exactly the drawdown a short-premium position has to sit
    through to collect. A structure with a wide gap can be profitable and
    still untradeable if you would not hold it through the excursion.
    """
    t = terminal_inside(spot, lower, upper, sigma)
    nt = no_touch(spot, lower, upper, sigma)
    return {"terminal": t, "no_touch": nt, "gap": t - nt}


# --------------------------------------------------------- gap classification

def gap_class(prev_day, day):
    """How much calendar time a gap spans. One definition, used everywhere.

    vol_calibrate.py imports this rather than keeping its own copy: if the
    fitting and the pricing ever disagreed about what counts as a weekend,
    every constant would be applied to the wrong kind of night.
    """
    n = (day - prev_day).days
    if n <= 1:
        return "overnight"
    if n <= 3:
        return "weekend"
    return "long break"


def day_classes_between(start_day, end_day, is_trading_day=None):
    """One class per COMPLETE trading day after start_day, up to end_day.

    `is_trading_day` is a callback so this module keeps no calendar
    dependency. Without one it falls back to Monday-Friday, which misses
    exchange holidays -- fine for a backtest, not for live pricing, where
    the caller should pass market.is_trading_day.
    """
    if is_trading_day is None:
        def is_trading_day(d):
            return d.weekday() < 5
    out, prev, cur = [], start_day, start_day + timedelta(days=1)
    while cur <= end_day:
        if is_trading_day(cur):
            out.append(gap_class(prev, cur))
            prev = cur
        cur += timedelta(days=1)
    return out


# ------------------------------------------------- the fitted two-term model

class Calibration:
    """Constants fitted by vol_calibrate.py, loaded from disk.

    The old model says variance accrues evenly with sqrt(calendar time). Five
    years of daily candles say otherwise: the session and the overnight gap
    carry different amounts of it, a gap is partly retraced by the session
    that follows, and a weekend gap is not the same animal as a weekday one.

    Each constant is a multiple of the model's OWN one-day sigma, so applying
    them corrects the level as well as the split.

    Different terms are fitted on different windows -- k_session and rho are
    flat over five years, k_gap has drifted, so it gets a shorter one. The
    file records which window each came from; this class just reads them.
    """

    def __init__(self, model):
        self.k_session = float(model["k_session"])
        self.rho = float(model.get("rho") or 0.0)
        self.k_gap = {c: float(v["k"]) for c, v in model["k_gap"].items()
                      if v.get("k")}
        self.k_day = {c: float(v["k"]) for c, v in model["k_day"].items()
                      if v.get("k")}
        self.session_minutes = float(model.get("session_minutes",
                                               SESSION_MINUTES))
        self.basis = float(model.get("basis", TRADING_DAYS_BASIS))
        self.meta = model

    @classmethod
    def load(cls, path="data/vol_calibration.json"):
        import json
        with open(path) as fh:
            return cls(json.load(fh)["model"])

    def _k(self, table, cls_):
        if cls_ in table:
            return table[cls_]
        if "weekend" in table:
            return table["weekend"]
        return max(table.values()) if table else 0.0

    def sigma_day(self, spot, vix):
        """The old model's one-day sigma. Everything is a multiple of it."""
        return spot * vix * sqrt(1.0 / self.basis)

    def sigma(self, spot, vix, lead_minutes=0.0, day_classes=(),
              tail=None):
        """Points of standard deviation over a described horizon.

        lead_minutes  trading minutes from now to the end of TODAY's session.
                      No gap precedes them, so no correlation term applies.

        day_classes   one entry per COMPLETE day crossed after that, each
                      naming its gap class. Complete days use k_day, the
                      directly measured gap-plus-session figure, which
                      already contains the correlation -- preferred over
                      rebuilding it from three constants.

        tail          (gap_class, minutes) for a final partial day: a gap,
                      then part of a session. Here the correlation DOES
                      apply, scaled by the square root of the session
                      fraction.

        KNOWN APPROXIMATION: rho was measured between a gap and the WHOLE
        session after it. Retracement plausibly happens early in that
        session, so scaling by sqrt(fraction) understates it for a tail that
        starts at the open and overstates it for one that ends at the close.
        Resolving that needs intraday data across many more sessions than
        are on disk.
        """
        sd = self.sigma_day(spot, vix)
        if sd <= 0:
            return 0.0
        var = 0.0

        f = max(0.0, lead_minutes) / self.session_minutes
        var += self.k_session ** 2 * f

        for c in day_classes:
            var += self._k(self.k_day, c) ** 2

        if tail:
            cls_, mins = tail
            tf = max(0.0, mins) / self.session_minutes
            kg = self._k(self.k_gap, cls_)
            var += kg ** 2 + self.k_session ** 2 * tf
            var += 2.0 * self.rho * kg * self.k_session * sqrt(tf)

        return sd * sqrt(max(var, 0.0))

    def intraday(self, spot, vix, minutes):
        """No gap crossed at all -- the case the old model got most wrong."""
        return self.sigma(spot, vix, lead_minutes=minutes)

    def ratio_to_old(self, lead_minutes=0.0, day_classes=(), tail=None):
        """How much this moves sigma against the flat sqrt(t) model.

        The old model counts CALENDAR days: a horizon of n complete days plus
        a part-session is (n + fraction) days, and sigma scales as its root.
        """
        old_days = (max(0.0, lead_minutes) / self.session_minutes
                    + len(day_classes)
                    + (1.0 if tail else 0.0))
        if old_days <= 0:
            return None
        var = self.k_session ** 2 * (max(0.0, lead_minutes)
                                     / self.session_minutes)
        for c in day_classes:
            var += self._k(self.k_day, c) ** 2
        if tail:
            cls_, mins = tail
            tf = max(0.0, mins) / self.session_minutes
            kg = self._k(self.k_gap, cls_)
            var += (kg ** 2 + self.k_session ** 2 * tf
                    + 2.0 * self.rho * kg * self.k_session * sqrt(tf))
        return sqrt(var) / sqrt(old_days)
