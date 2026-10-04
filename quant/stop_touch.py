"""Probability that a short structure's STOP is touched before it is closed.

WHY THE OLD NUMBER IS NOT THIS NUMBER

pricing.probability_stop_touched is the workbook's min(1, 2*(1-POP)): the
reflection-principle touch probability for a barrier at the BREAKEVEN, on
spot. The sizing card stops out on the structure's COST TO CLOSE reaching
credit * (1 + stop). Those are different barriers -- a 30% stop on a 1-DTE
straddle sits far inside the breakevens -- and Kelly was being fed POP, the
probability of finishing between the breakevens, as its chance of winning a
bet whose loss side is the stop. This module prices the barrier that is
actually on the page. The workbook function is left untouched for
comparison.

THE MODEL

  spot     Bachelier (normal, zero drift), total variance to expiry equal to
           the SITE's sigma squared -- the same sigma behind every POP and EV
           here, whichever source produced it.

  clock    The calendar clock the site already uses, with the variance put
           where it happens. Each trading session carries `session_share` of
           one calendar day's variance, spread evenly over its minutes. The
           rest of that day, plus one full day for every non-trading day, is
           released as ONE jump at the next 09:15. A stop can only fire while
           the market is open, so a gap can carry the mark straight through
           it -- which is the risk this exists to price. session_share is
           measured, not chosen: see gap_share.py (0.66 on 375 one-night
           NIFTY futures pairs, Jul 2024 - Aug 2026).

           Known error, kept on purpose because the rest of the site has it:
           the calendar clock charges a Friday->Monday gap about 6.9x a
           one-night gap. Measured, it is 2.57x (n=101). P(stop) on a
           position held over a weekend is overstated by that much.

  pricing  Every leg repriced along each path with ONE volatility, solved so
           the model reproduces the structure's market mid at entry, and
           held fixed thereafter (scaled only by remaining time on the same
           clock). Repricing with the site's sigma instead would mark a rich
           straddle down to fair value in the first step and put the stop
           dozens of points further away than it really is. Fixed implied
           vol means a VOLATILITY SPIKE IS NOT MODELLED -- a mark can rise on
           vol alone with the index still, and this cannot see that.

  trigger  Journals stop on cost to close at the ASK. The model gives a mid,
           so the trigger is  model_mid + measured half-spread >= stop_level,
           with the half-spread taken from the chain at entry and held.

  sampling Monte Carlo, common random numbers across structures (one set of
           paths, every structure evaluated on it), fixed seed so a republish
           does not jitter. The standard error is returned with the number.
"""

import project_paths
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from math import sqrt
import json
import os

import numpy as np

ROOT = project_paths.ROOT
GAP_SHARE_FILE = os.path.join(ROOT, "data", "gap_share.json")

OPEN, CLOSE = dtime(9, 15), dtime(15, 30)
SESSION_MIN = 375
STEP_MIN = 5
N_PATHS = 10_000
SEED = 20260922
WEEKEND_NOTE = ("Held over a weekend, this uses the site's calendar clock, "
                "which charges a Friday-to-Monday gap about 6.9x a one-night "
                "gap; measured on NIFTY futures it is 2.57x (n=101). P(stop) "
                "across a weekend is overstated.")


def load_session_share(path=GAP_SHARE_FILE):
    """The measured share, or None. None is not a default -- callers must
    refuse to compute rather than invent one."""
    try:
        with open(path) as f:
            return float(json.load(f)["session_share"])
    except (OSError, KeyError, ValueError):
        return None


# ----------------------------------------------------------- normal, vector

def _erf(x):
    # Abramowitz & Stegun 7.1.26, |error| < 1.5e-7. numpy has no erf, and
    # scipy is not guaranteed on the server; test_stop_touch pins this against
    # math.erf.
    s = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * np.exp(-x * x)
    return s * y


def ncdf(z):
    return 0.5 * (1.0 + _erf(z / sqrt(2.0)))


def npdf(z):
    return np.exp(-0.5 * z * z) / sqrt(2.0 * np.pi)


def bachelier(S, K, kind, sigma):
    """E[payoff] under Normal(S, sigma). Same formula as pricing.expected_*.
    sigma == 0 returns intrinsic."""
    S = np.asarray(S, dtype=float)
    if sigma <= 1e-9:
        return np.maximum(S - K, 0.0) if kind == "CE" else np.maximum(K - S, 0.0)
    d = (S - K) / sigma if kind == "CE" else (K - S) / sigma
    return sigma * npdf(d) + d * sigma * ncdf(d)


def structure_value(S, legs, sigma):
    """Net value to CLOSE a short structure, mid, in points per unit.

    legs: [{"strike", "kind" ('CE'/'PE'), "qty"}] with qty -1 for a short leg,
    +1 for a long one (the api's convention). A short position's cost to close
    is minus the sum of qty * value.
    """
    v = 0.0
    for l in legs:
        v = v - l["qty"] * bachelier(S, float(l["strike"]), l["kind"], sigma)
    return v


def solve_implied(spot, legs, market_mid, sigma_hi):
    """The single sigma (points, to expiry) that reproduces market_mid.

    Bisection over [0, 6 * sigma_hi]. Returns None if the structure's value
    is not increasing in sigma over that range, or the market mid is outside
    what the model can produce -- a number that cannot be matched is reported
    as unmatched, not forced.
    """
    lo, hi = 1e-6, 6.0 * max(sigma_hi, 1.0)
    f = lambda s: float(structure_value(spot, legs, s)) - market_mid
    flo, fhi = f(lo), f(hi)
    if not (flo <= 0.0 <= fhi):
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------- the clock

@dataclass
class Step:
    weight: float        # variance, in calendar-day units before scaling
    checkable: bool      # can the stop fire after this increment?
    is_gap: bool
    when: datetime       # the moment the increment has fully arrived


def schedule(now, expiry_day, is_trading_day, session_share,
             step_min=STEP_MIN, expiry_time=CLOSE):
    """Variance increments from `now` to expiry, on the site's calendar clock.

    `now` is a naive IST datetime. Weights are in units of one calendar day's
    variance; the caller scales them so their sum is the site's sigma^2.
    """
    s = session_share
    per_min = s / SESSION_MIN
    steps = []
    d = now.date()
    t = now
    while d <= expiry_day:
        close = datetime.combine(d, expiry_time if d == expiry_day else CLOSE)
        opening = datetime.combine(d, OPEN)
        if is_trading_day(d):
            start = max(t, opening)
            m = start
            while m < close:
                nxt = min(m + timedelta(minutes=step_min), close)
                steps.append(Step(per_min * (nxt - m).total_seconds() / 60.0,
                                  True, False, nxt))
                m = nxt
            if d == expiry_day:
                break
            # the rest of this calendar day, charged to the next open
            night = 1.0 - s
        else:
            night = 1.0
        # accumulate into one gap at the next trading day's open
        d2 = d + timedelta(days=1)
        acc = night
        while d2 <= expiry_day and not is_trading_day(d2):
            acc += 1.0
            d2 += timedelta(days=1)
        if d2 > expiry_day:
            break
        steps.append(Step(acc, True, True, datetime.combine(d2, OPEN)))
        d, t = d2, datetime.combine(d2, OPEN)
    return steps


# -------------------------------------------------------------- simulation

STOP_GRID = (0.10, 0.20, 0.30, 0.50, 0.75, 1.00)


@dataclass
class Result:
    grid: list                    # [{stop_frac, stop_level, p_touch, se, share_at_open}]
    n_paths: int
    sigma_implied: float
    half_spread: float
    weekend_held: bool
    note: str = ""

    def at(self, frac):
        for g in self.grid:
            if abs(g["stop_frac"] - frac) < 1e-9:
                return g
        return None


def simulate(spot, sigma_site, structures, now, expiry_day, exit_at,
             is_trading_day, session_share, n_paths=N_PATHS, seed=SEED,
             step_min=STEP_MIN, stop_fracs=STOP_GRID):
    """P(stop touched before exit), for several structures and several stop
    sizes, on ONE set of paths.

    structures: list of dicts
        {"name", "legs": [...], "market_mid", "half_spread", "credit"}
    The stop for fraction f sits at credit * (1 + f), the sizing card's rule.
    exit_at: naive IST datetime the position is closed if never stopped
             (expiry close for a held weekly, 15:15 for the intraday trade).

    Returns {name: Result or None}. None means the structure could not be
    matched to its market price; it is reported, not estimated.
    """
    if session_share is None:
        raise ValueError("session_share is required -- run gap_share.py")
    steps = schedule(now, expiry_day, is_trading_day, session_share, step_min)
    live = [i for i, x in enumerate(steps) if x.when <= exit_at]
    if not steps or not live:
        return {s["name"]: None for s in structures}
    w = np.array([x.weight for x in steps])
    total_var = sigma_site ** 2
    w = w * (total_var / w.sum())
    remaining_after = total_var - np.cumsum(w)          # variance left to expiry
    last = live[-1]
    fr = np.asarray(stop_fracs, dtype=float)

    prepared = []
    for s in structures:
        imp = solve_implied(spot, s["legs"], s["market_mid"], sigma_site)
        levels = s["credit"] * (1.0 + fr)
        prepared.append((s, imp, levels[:, None]))

    rng = np.random.default_rng(seed)
    S = np.full(n_paths, float(spot))
    shape = (len(fr), n_paths)
    touched = {s["name"]: np.zeros(shape, dtype=bool) for s, imp, _ in prepared if imp}
    at_open = {k: np.zeros(shape, dtype=bool) for k in touched}

    for i in range(last + 1):
        S = S + rng.standard_normal(n_paths) * sqrt(w[i])
        if not steps[i].checkable:
            continue
        frac_left = max(remaining_after[i], 0.0) / total_var
        for s, imp, levels in prepared:
            if not imp:
                continue
            k = s["name"]
            mark = structure_value(S, s["legs"], imp * sqrt(frac_left)) + s["half_spread"]
            hit = (mark[None, :] >= levels) & ~touched[k]
            if steps[i].is_gap:
                at_open[k] |= hit
            touched[k] |= hit

    weekend = any((now.date() + timedelta(days=j)).weekday() >= 5
                  for j in range((exit_at.date() - now.date()).days + 1))

    out = {}
    for s, imp, levels in prepared:
        if not imp:
            out[s["name"]] = None
            continue
        t, o = touched[s["name"]], at_open[s["name"]]
        grid = []
        for n, f in enumerate(fr):
            hits = int(t[n].sum())
            p = hits / n_paths
            grid.append({
                "stop_frac": round(float(f), 4),
                "stop_level": round(float(levels[n, 0]), 2),
                "p_touch": round(p, 4),
                "se": round(sqrt(p * (1 - p) / n_paths), 4),
                "share_at_open": round(int(o[n].sum()) / hits, 4) if hits else 0.0,
            })
        out[s["name"]] = Result(grid=grid, n_paths=n_paths,
                                sigma_implied=round(imp, 2),
                                half_spread=round(s["half_spread"], 2),
                                weekend_held=weekend,
                                note=WEEKEND_NOTE if weekend else "")
    return out


def kelly(p_win, credit, loss_at_stop):
    """Binary Kelly, win = credit, loss = loss at stop. Same shape as the card."""
    if loss_at_stop <= 0:
        return None
    b = credit / loss_at_stop
    return (p_win * b - (1 - p_win)) / b
