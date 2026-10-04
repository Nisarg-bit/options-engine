"""Calibrated POP -- the production side of calibrate.py.

calibrate.py compared four distributions for where NIFTY finishes at the
weekly expiry, fitting on Jul 2024 - Jul 2025 and scoring on Aug 2025 - Aug
2026 (and again the other way round). The empirical distribution of realised
moves, measured in session-clock sigma and centred on the parity forward, had
the lowest Brier score both ways. data/calibration.json holds it, refitted on
all 520 days.

What it corrects, in the site's own backtest data:
  * the VIX-implied sigma is WIDER than what NIFTY delivered (realised moves
    had sd 0.86-0.88 sigma) -- so short-premium zones held more often than the
    normal POP said, and a long call paid off less often;
  * weekends: a Friday-to-Monday gap carries 2.57x one night's variance, not
    three calendar days of it (session clock, gap_share.json);
  * the real-world drift sits below the forward.

It is SHOWN beside the model POP, never instead of it, and it feeds nothing
that authorises a trade: the 0.9346 entry rule stays on the calendar sigma it
was fitted on. NIFTY only -- it was fitted on NIFTY.
"""
import project_paths
import json
import math
import os
from datetime import timedelta

ROOT = project_paths.ROOT
FILE = os.path.join(ROOT, "data", "calibration.json")
UNDERLYINGS = ("NIFTY",)

_cal = None


def load():
    global _cal
    if _cal is None:
        try:
            with open(FILE) as f:
                _cal = json.load(f)
        except (OSError, ValueError):
            _cal = {}
    return _cal


def available(underlying):
    c = load()
    return bool(c.get("z")) and underlying in UNDERLYINGS


def session_units(session, expiry, is_trading_day, share=None, gaps=None):
    """Variance units from the SESSION's close to the EXPIRY's close.

    Each trading day is one unit: `share` of it in the session, the rest
    overnight, and an overnight spanning n nights carries the measured
    multiple gaps[n] of a one-night gap. An expiry today (0 DTE) gets a
    quarter of a day, the floor that keeps a probability from collapsing to
    0 or 1 on a position still open."""
    c = load()
    share = c.get("session_share", 0.6626) if share is None else share
    gaps = c.get("gap_mult", {"3": 2.57}) if gaps is None else gaps
    if expiry <= session:
        return 0.25
    u, prev, d = 0.0, session, session + timedelta(days=1)
    while d <= expiry:
        if is_trading_day(d):
            nights = (d - prev).days
            mult = 1.0 if nights == 1 else float(gaps.get(str(nights), gaps.get("3", 2.57)))
            u += share + (1 - share) * mult
            prev = d
        d += timedelta(days=1)
    return max(u, 0.25)


def sigma_session(spot, vol_units, session, expiry, is_trading_day):
    """Session-clock sigma in points, on the same overall scale as the site's
    calendar sigma (unit_scale converts units back to calendar days)."""
    c = load()
    days = session_units(session, expiry, is_trading_day) * c.get("unit_scale", 1.0)
    return spot * vol_units / 100.0 * math.sqrt(days / 365.0)


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


_TABLE = None
_QLO, _QHI, _QN = -10.0, 10.0, 4001


def _table():
    """The calibrated CDF tabulated once on a fine grid of z (0.005 apart);
    every later lookup is a linear interpolation. Summing 520 kernels per
    lookup would cost seconds per structures build on the server."""
    global _TABLE
    if _TABLE is None:
        c = load()
        zs, h = c["z"], c["bandwidth"]
        dq = (_QHI - _QLO) / (_QN - 1)
        _TABLE = [sum(_ncdf((_QLO + i * dq - z) / h) for z in zs) / len(zs)
                  for i in range(_QN)]
    return _TABLE


def cdf_z(q):
    t = _table()
    if q <= _QLO:
        return 0.0
    if q >= _QHI:
        return 1.0
    x = (q - _QLO) / (_QHI - _QLO) * (_QN - 1)
    i = int(x)
    return t[i] + (t[min(i + 1, _QN - 1)] - t[i]) * (x - i)


def cdf(level, forward, sigma):
    """P(index at expiry < level) under the calibrated distribution."""
    return cdf_z((level - forward) / sigma)


def pop_zone(lo, hi, forward, sigma):
    return max(0.0, cdf(hi, forward, sigma) - cdf(lo, forward, sigma))


def pop_curve(xs, ys, forward, sigma):
    """P(P&L > 0) for a P&L curve sampled at levels xs (sorted)."""
    # cdf is the expensive part: evaluate once per grid point, thinned to
    # every few points (the payoff is piecewise linear, the error negligible)
    step = 1
    idx = list(range(0, len(xs), step))
    if idx[-1] != len(xs) - 1:
        idx.append(len(xs) - 1)
    cs = [cdf(xs[i], forward, sigma) for i in idx]
    p = 0.0
    for a in range(len(idx) - 1):
        i, j = idx[a], idx[a + 1]
        seg = ys[i:j + 1]
        pos = sum(1 for y in seg if y > 0) / len(seg)
        p += pos * (cs[a + 1] - cs[a])
    # mass beyond the grid, at the edge P&L's sign
    p += cs[0] * (1.0 if ys[0] > 0 else 0.0)
    p += (1 - cs[-1]) * (1.0 if ys[-1] > 0 else 0.0)
    return max(0.0, min(1.0, p))


def public():
    """For the page: everything the builder needs to compute the same POP."""
    c = load()
    if not c.get("z"):
        return {"error": "data/calibration.json missing -- run python calibrate.py"}
    return {"model": c["model"], "clock": c["clock"], "centre": c["centre"],
            "bandwidth": c["bandwidth"], "z": c["z"], "underlyings": list(UNDERLYINGS),
            "fitted_on": c["fitted_on"], "n_days": c["n_days"],
            "chosen_by": c["chosen_by"], "validation": c.get("validation"),
            # Ten-year walk-forward test (calibrate_wf.py, 26 Sep 2026): the page
            # leads with this; the one-year result above is kept as history.
            "validation_10y": c.get("validation_10y")}
