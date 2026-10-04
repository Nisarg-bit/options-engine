"""P(stop) for the structures payload -- glue between api.py and stop_touch.

Scope, by decision: the NEAREST live expiry only, for every underlying. The
publisher rebuilds structures every 30 seconds for every expiry; seven
structures of Monte Carlo at 28 DTE is several seconds of a t4g.small, so the
far expiries carry `p_stop: null` with a note saying why, rather than a
number nobody paid for.

Cached per (underlying, session, expiry, last bar). The chain only changes
when the collector flushes -- about every five minutes -- so between flushes
this is a dictionary lookup. Spot moves every tick; the number is computed at
the spot of the flush that produced it, and the payload says which.
"""

from datetime import datetime, time as dtime

import stop_touch as T

_cache = {}
_MAX = 32
NOT_NEAREST = ("P(stop) is computed for the nearest expiry only -- Monte Carlo "
               "on every expiry every 30 seconds does not fit this server.")
NO_SHARE = ("P(stop) unavailable: data/gap_share.json is missing. Run "
            "python gap_share.py once.")


def _half_spread(legs, rows):
    by = {float(r["strike"]): r for r in rows}
    total = 0.0
    for l in legs:
        r = by.get(float(l["strike"]))
        if r is None or l["kind"] not in r:
            return None
        c = r[l["kind"]]
        if not c.get("bid") or not c.get("ask"):
            return None
        total += (c["ask"] - c["bid"]) / 2.0
    return total


def compute(*, underlying, day, expiry, nearest, last_ts_ist, spot, sigma_pts,
            structures, rows, is_trading_day, session_share=None):
    """{name: payload-dict or None}, plus a meta dict.

    structures: iterable of (name, credit_pts, legs) with legs in the api's
                shape [{"strike","kind","qty"}] for ONE lot.
    """
    if expiry != nearest:
        return {}, {"computed": False, "note": NOT_NEAREST}
    share = session_share if session_share is not None else T.load_session_share()
    if share is None:
        return {}, {"computed": False, "note": NO_SHARE}
    if not sigma_pts or not spot or last_ts_ist is None:
        return {}, {"computed": False, "note": "P(stop) unavailable: no sigma or spot"}

    now = last_ts_ist.replace(tzinfo=None, second=0, microsecond=0)
    key = (underlying, str(day), str(expiry), now.isoformat())
    if key in _cache:
        return _cache[key]

    specs, skipped = [], {}
    for name, credit, legs in structures:
        # per-unit legs: the api reports qty in lots; the model wants the sign
        unit = [{"strike": l["strike"], "kind": l["kind"],
                 "qty": -1 if l["qty"] < 0 else 1} for l in legs]
        hs = _half_spread(unit, rows)
        if hs is None or credit <= 0:
            skipped[name] = "no two-sided quote on a leg"
            continue
        specs.append({"name": name, "legs": unit, "market_mid": credit,
                      "half_spread": hs, "credit": credit})

    exit_at = datetime.combine(expiry, T.CLOSE)
    res = T.simulate(spot, sigma_pts, specs, now, expiry, exit_at,
                     is_trading_day, share) if specs else {}

    out = {}
    for name, credit, legs in structures:
        r = res.get(name)
        if r is None:
            out[name] = None
            continue
        out[name] = {"grid": r.grid, "sigma_implied": r.sigma_implied,
                     "half_spread": r.half_spread, "n_paths": r.n_paths}
    meta = {
        "computed": True,
        "as_of": now.isoformat(timespec="minutes"),
        "spot": round(spot, 2), "sigma": round(sigma_pts, 2),
        "session_share": share, "held_to": exit_at.isoformat(timespec="minutes"),
        "weekend_held": any(r.weekend_held for r in res.values() if r),
        "note": next((r.note for r in res.values() if r and r.note), ""),
        "skipped": skipped,
        "method": ("Monte Carlo, Bachelier spot on the site's sigma and calendar "
                   "clock; session share measured; overnight variance lands as one "
                   "gap at 09:15; legs repriced at the implied vol matching entry, "
                   "held fixed (no vol spikes); trigger at mid + half-spread."),
    }
    if len(_cache) >= _MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (out, meta)
    return _cache[key]
