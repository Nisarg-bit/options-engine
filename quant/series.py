"""Per-minute price series for a chosen option structure, with indicators.

WHAT THIS IS FOR

/api/intraday today returns one thing: the ROLLING at-the-money straddle,
priced by parity, with no volume and no indicators. The dashboard needs
more -- a custom strike, a strangle at a chosen width, VWAP, moving averages
and RSI -- and this module is the data layer for that.

THE TRAP THAT SHAPES THE WHOLE DESIGN

A "rolling ATM" series changes STRIKE whenever the index moves past the
midpoint between two strikes, so it is a different contract before and after.

The first draft of this file claimed that produced a 30% price jump. That is
WRONG, and measuring it said so. The ATM is DEFINED as the strike where call
and put agree, which is also, by construction, the point where the two
candidate straddles are worth most nearly the same. Priced at the midpoint
between 23100 and 23150:

    STRADDLE    K=23100 -> 203.60    K=23150 -> 203.60    jump  0.00
    CALL ALONE  K=23100 -> 114.30    K=23150 ->  89.30    jump 25.00

So a rolling ATM straddle is nearly continuous in PRICE. A single leg is
not -- it rolls by about half a strike step, every time.

But price is not the only series on the chart, and the rest do not survive a
roll at all:

  VWAP      accumulates price*volume for a specific contract. Carry it
            across a roll and it averages two different instruments; reset
            it and the line drops off a cliff. Neither is a VWAP.
  VOLUME    belongs to whichever contract was ATM at that minute.
  OI        likewise -- and OI is the whole basis of the buildup read.

So indicators are refused on a rolling anchor regardless of structure: for a
single leg because the price itself jumps, and for a straddle because
everything except the price does.

This module offers two anchors and is explicit about which is safe:

  "atm_open"    the ATM strike as it stood at the first usable minute, held
                for the whole session. One contract, continuous price.
                INDICATORS ARE VALID HERE.

  "atm_rolling" the ATM at each minute. Good to look at, and what the
                existing endpoint returns. Indicators are refused rather
                than computed, because a number that looks like RSI and
                is not is worse than no number.

  <a number>    that exact strike, held all session. Same guarantees as
                atm_open.

VWAP OF A MULTI-LEG STRUCTURE

There is no unambiguous answer, so the definition used is stated rather
than hidden: each leg's own session VWAP, summed.

    vwap(straddle) = vwap(CE) + vwap(PE)

Each term means something -- the average price you would have paid for that
leg across the session -- and the sum is the average cost of assembling the
structure leg by leg. The alternative, weighting the combined price by some
merged volume, requires inventing a combined volume: summing double-counts,
and taking the minimum is arbitrary. Neither corresponds to a price anyone
could have traded, and at least this one is decomposable.

If the two legs trade at very different times of day, the sum is still not
a price the structure ever changed hands at. That is a property of synthetic
combinations, not a bug here, and the caller is told so in the payload.
"""

from math import isnan

import indicators as IND

STRUCTURES = ("call", "put", "straddle", "strangle")


def _f(v):
    try:
        x = float(v)
        return None if isnan(x) else x
    except (TypeError, ValueError):
        return None


def infer_step(strikes):
    """The strike interval, as the smallest positive gap that repeats."""
    s = sorted({float(k) for k in strikes})
    if len(s) < 2:
        return 50.0
    gaps = {}
    for a, b in zip(s, s[1:]):
        d = round(b - a, 4)
        if d > 0:
            gaps[d] = gaps.get(d, 0) + 1
    return min(gaps, key=lambda d: (-gaps[d], d)) if gaps else 50.0


def atm_by_parity(minute_rows):
    """The strike whose |CE - PE| is smallest.

    Model-free: at the forward, a call and a put of the same strike are
    worth the same. No volatility assumption anywhere in it.
    """
    best, best_gap = None, None
    for k, legs in minute_rows.items():
        c, p = legs.get("CE", {}).get("mid"), legs.get("PE", {}).get("mid")
        if c is None or p is None:
            continue
        gap = abs(c - p)
        if best_gap is None or gap < best_gap:
            best, best_gap = k, gap
    return best


def legs_for(structure, atm, step, width=2):
    """[(strike, opt_type)] for the requested structure."""
    if structure == "call":
        return [(atm, "CE")]
    if structure == "put":
        return [(atm, "PE")]
    if structure == "straddle":
        return [(atm, "CE"), (atm, "PE")]
    if structure == "strangle":
        return [(atm + width * step, "CE"), (atm - width * step, "PE")]
    raise ValueError(f"unknown structure {structure!r}")


ANCHOR_FROM = "09:20"      # the opening auction is not a price; see below


def build(by_minute, structure="straddle", anchor="atm_open", width=2,
          ma=20, ema=20, rsi=14, anchor_from=ANCHOR_FROM):
    """Price the structure through the session and attach indicators.

    `by_minute` is {ts: {strike: {"CE": {...}, "PE": {...}}}} where each leg
    carries mid, volume, pv and oi. Returns a dict with the rows and a
    `notes` list naming every judgement the caller should know about.
    """
    if structure not in STRUCTURES:
        raise ValueError(f"unknown structure {structure!r}")
    times = sorted(by_minute)
    if not times:
        return {"rows": [], "notes": ["no minutes in this session"]}

    all_strikes = {k for m in by_minute.values() for k in m}
    step = infer_step(all_strikes)
    notes = []

    rolling = anchor == "atm_rolling"
    fixed = None
    if not rolling:
        if anchor == "atm_open":
            # NOT simply the first minute. The session opens at 09:15 and the
            # first minutes are the opening auction unwinding -- the same
            # window whose prints put the 09:00 signal 853 points wrong and
            # made live_signal refuse to run before 09:20. Anchoring the
            # whole session's chart on an auction-period parity ATM picks
            # the wrong contract for the entire day, silently.
            #
            # So the anchor is taken from the first minute at or after
            # `anchor_from`. Earlier minutes are still PLOTTED -- they are
            # real quotes and a chart should show them -- they just do not
            # get to choose the strike.
            usable = [t for t in times if t >= anchor_from]
            if not usable:
                return {"rows": [], "notes": [
                    f"no minute at or after {anchor_from} to anchor on; the "
                    f"session has {len(times)} minutes, ending {times[-1]}"]}
            for t in usable:
                fixed = atm_by_parity(by_minute[t])
                if fixed is not None:
                    notes.append(f"strike anchored at the {t} ATM: "
                                 f"{fixed:,.0f}  (first minute at or after "
                                 f"{anchor_from}; the opening auction does "
                                 f"not choose the strike)")
                    break
            if fixed is None:
                return {"rows": [], "notes": ["no minute had a two-sided "
                                              "strike to anchor on"]}
        else:
            fixed = _f(anchor)
            if fixed is None:
                raise ValueError(f"anchor must be a strike, 'atm_open' or "
                                 f"'atm_rolling', got {anchor!r}")

    rows, rolls = [], 0
    prev_legs = None
    # Per-leg running VWAP accumulators, keyed by (strike, opt_type).
    acc = {}

    for t in times:
        m = by_minute[t]
        atm = atm_by_parity(m) if rolling else fixed
        if atm is None:
            continue
        try:
            want = legs_for(structure, atm, step, width)
        except ValueError:
            raise
        got, price, oi, vwap_sum, ok = [], 0.0, 0.0, 0.0, True
        for k, ot in want:
            leg = m.get(k, {}).get(ot)
            mid = _f((leg or {}).get("mid"))
            if mid is None or mid <= 0:
                ok = False
                break
            v = _f(leg.get("volume")) or 0.0
            pv = _f(leg.get("pv")) or 0.0
            a = acc.setdefault((k, ot), [0.0, 0.0])
            a[0] += pv
            a[1] += v
            vwap_sum += (a[0] / a[1]) if a[1] > 0 else mid
            price += mid
            oi += _f(leg.get("oi")) or 0.0
            got.append({"strike": k, "opt_type": ot, "mid": round(mid, 2)})
        if not ok:
            continue

        key = tuple((g["strike"], g["opt_type"]) for g in got)
        if prev_legs is not None and key != prev_legs:
            rolls += 1
        prev_legs = key

        rows.append({"ts": t, "price": round(price, 2),
                     "vwap": round(vwap_sum, 2),
                     "oi": oi, "legs": got})

    if not rows:
        return {"rows": [], "notes": ["the structure could not be priced in "
                                      "any minute of this session"]}

    prices = [r["price"] for r in rows]

    if rolling:
        single = len(rows[0]["legs"]) == 1
        notes.append(f"ROLLING ATM: the strike changed {rolls} times today, "
                     f"so this is not one contract but a sequence of them")
        notes.append(
            "price jumps by about half a strike step at each roll"
            if single else
            "price is nearly continuous across rolls -- the ATM is where "
            "the two candidate straddles agree most closely -- but VWAP, "
            "volume and OI all belong to whichever contract was ATM at "
            "that minute")
        notes.append("indicators are NOT computed on a rolling anchor: a "
                     "VWAP carried across a roll averages two different "
                     "instruments, and resetting it drops the line off a "
                     "cliff. Neither is a VWAP.")
        for r in rows:
            r["ma"] = r["ema"] = r["rsi"] = None
    else:
        notes.append(f"fixed strike: one contract all session, so the "
                     f"series is continuous and indicators are meaningful")
        for r, a, b, c in zip(rows, IND.sma(prices, ma),
                              IND.ema(prices, ema), IND.rsi(prices, rsi)):
            r["ma"] = None if a is None else round(a, 2)
            r["ema"] = None if b is None else round(b, 2)
            r["rsi"] = None if c is None else round(c, 2)

    if len(legs_for(structure, rows[0]["legs"][0]["strike"], step, width)) > 1:
        notes.append("vwap is each leg's own session VWAP, summed -- the "
                     "average cost of assembling the structure leg by leg, "
                     "not a price the combination itself traded at")

    return {"structure": structure, "anchor": anchor, "width": width,
            "strike_step": step, "points": len(rows), "strike_rolls": rolls,
            "rows": rows, "notes": notes}
