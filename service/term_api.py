"""Implied volatility by expiry -- the curve a single 30-day VIX flattens.

WHY THIS EXISTS

Every sigma on this site is spot * (vol/100) * sqrt(dte/365), and for NIFTY
the vol is India VIX: one number, a THIRTY-DAY implied volatility, applied to
every expiry from tomorrow to two months out. That assumption is the project's
largest known defect. It is what made the weekly strategy claim 78.2% safety
and deliver 57.9%: the model thought the wings were further away than they
were.

This docstring used to put the size of that error at "about a third". That was
an estimate and it was too large. Measured across 39 readings on 14 sessions,
at-the-money implied volatility runs about 7% above India VIX once dead
weekend days are taken out of the clock, and about 12% at the shortest
horizons. The RAW comparison this endpoint draws points the other way, and
that is not a contradiction: sigma here counts CALENDAR days while VIX is a
thirty-day number whose per-day variance already averages a normal weekend in,
so a weekly with dead days inside it solves to a lower implied volatility than
VIX. The curve below is the raw comparison. It does not correct for the
weekday effect, and nothing downstream of it does either.

The defect is easy to state and hard to see. This endpoint makes it visible:
solve the at-the-money implied volatility from each expiry's own chain and put
them beside the flat VIX line. If the curve slopes, VIX is wrong everywhere
except where it happens to cross.

WHAT IT DOES NOT DO

It does not change any number the site computes. `sigma_source=chain_iv` on
/api/chain and /api/structures already does that, and the entry gate is
deliberately withheld under it (see sigma_sources for why the 0.9346 threshold
does not survive a change of denominator). This is a measurement, presented as
one.

COST

One chain build per expiry. build_chain is cached on the Parquet signature, so
the first call after new bars land pays for all of them and every call after
that is free until the collector writes again. `limit` is capped rather than
unbounded because the far months are thinly traded and their ATM solve is
noise, not signal -- a strike that has not traded produces an implied vol from
a stale quote.
"""

from fastapi import APIRouter, HTTPException, Query

import models as M
import sigma_sources as SIG

router = APIRouter()

MAX_EXPIRIES = 8
MIN_ROWS = 8            # below this the ATM solve is not worth reporting


def _api():
    """api imports this module near the bottom of itself, so importing api at
    module scope here would be circular. By the time a request arrives, api is
    fully initialised in sys.modules and this is an ordinary lookup."""
    import api as API
    return API


@router.get("/api/term")
def api_term(underlying: str = "NIFTY", limit: int = 6,
             session: str = Query(None)):
    API = _api()
    day = API.pick_session(session)
    bars = API.last_bars(day, underlying)
    if bars is None:
        raise HTTPException(404, f"no {underlying} bars for {day}")

    # One definition of today, shared with api.py. On the newest session the
    # curve is anchored to today, because a settled expiry is not tradeable;
    # on a replayed session it is anchored to that session, or the far end of
    # the curve would be missing exactly the expiries that made it a curve.
    # The dte each row reports comes from API.resolve either way, which
    # anchors to the priced session -- see dte_for.
    # Settled expiries (past 15:30 IST on their own day) drop off the curve
    # on the live session -- see api.is_settled.
    want = API.live_expiries(day, bars["expiry"].unique())
    want = want[:max(1, min(int(limit), MAX_EXPIRIES))]

    rows, skipped = [], []
    spot_ref, vix_ref, src_ref, as_of = None, None, None, None

    for e in want:
        try:
            _d, exp, dte = API.resolve(underlying, str(e), str(day))
            chain, _dropped, _total, last_ts = API.build_chain(day, underlying, exp)
        except Exception as exc:
            skipped.append({"expiry": str(e), "why": f"{type(exc).__name__}: {exc}"})
            continue
        if not chain or len(chain) < MIN_ROWS:
            skipped.append({"expiry": str(e),
                            "why": f"only {len(chain) if chain else 0} usable "
                                   f"strikes, needs {MIN_ROWS}"})
            continue

        flat = API.priced(chain)
        spot, vix, src = API.market_ref(underlying, chain, dte, day)
        if not spot:
            skipped.append({"expiry": str(e), "why": "no spot reference"})
            continue

        # The first expiry that prices is the reference for spot and VIX --
        # they are properties of the underlying, not of an expiry, so reading
        # them once keeps the curve internally consistent.
        if spot_ref is None:
            spot_ref, vix_ref, src_ref = spot, vix, src
        if last_ts is not None and (as_of is None or last_ts > as_of):
            as_of = last_ts

        fwd = M.forward_from_parity(flat, spot)
        ref = fwd or spot
        atm = min(chain, key=lambda r: abs(r["strike"] - ref))
        straddle = (atm["CE"].get("mid") or 0) + (atm["PE"].get("mid") or 0)
        iv = API.chain_iv_units(flat, spot, dte)
        sig = SIG.sigma_points(spot, iv, dte) if iv else None
        vix_sig = SIG.sigma_points(spot, vix, dte) if vix else None

        rows.append({
            "expiry": str(exp),
            "dte": dte,
            "atm_strike": atm["strike"],
            "straddle": round(straddle, 2) if straddle else None,
            "atm_iv": round(iv, 3) if iv else None,
            "sigma": round(sig, 2) if sig else None,
            "vix_sigma": round(vix_sig, 2) if vix_sig else None,
            # The gap is the point of the whole exercise: positive means this
            # expiry is priced ABOVE what a flat VIX would say, negative below.
            "iv_minus_vix": round(iv - vix, 3) if (iv and vix) else None,
            "sigma_ratio": round(sig / vix_sig, 4) if (sig and vix_sig) else None,
            "usable_strikes": len(chain),
        })

    if not rows:
        raise HTTPException(404, f"no expiry priced for {underlying}")

    near, far = rows[0], rows[-1]
    slope = None
    if near.get("atm_iv") and far.get("atm_iv") and far["dte"] != near["dte"]:
        slope = round((far["atm_iv"] - near["atm_iv"])
                      / (far["dte"] - near["dte"]), 4)

    return {
        "underlying": underlying,
        "session": str(day),
        "today_ist": str(API.today_ist()),
        "replay": day != API.latest_session(),
        "as_of": (API.as_ist(as_of).isoformat(timespec="seconds")
                  if as_of is not None else None),
        "spot": round(spot_ref, 2) if spot_ref else None,
        "vix": round(vix_ref, 2) if vix_ref else None,
        "vix_source": src_ref,
        # One number for "does the curve slope", in IV points per day of DTE.
        "slope_iv_per_day": slope,
        "shape": (None if slope is None else
                  "flat" if abs(slope) < 0.02 else
                  "upward -- near expiries cheaper than VIX implies" if slope > 0
                  else "downward -- near expiries dearer than VIX implies"),
        "note": ("atm_iv is solved from each expiry's own chain; vix is one "
                 "thirty-day number applied to all of them. Where they differ, "
                 "every POP, EV and sigma band computed on VIX is wrong for "
                 "that expiry by the same proportion."),
        "rows": rows,
        "skipped": skipped,
    }
