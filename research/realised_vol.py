"""How big were the moves, really, against how big VIX said they would be.

THE QUESTION THIS ANSWERS

Every sigma in this system comes from one formula:

    sigma = spot * (VIX/100) * sqrt(days / 365)

which assumes volatility per unit time is constant -- that a quarter of a
session carries a quarter of a session's variance, and that a day eight days
out is the same as the day of expiry. The intraday backtest showed the model
erring in OPPOSITE directions at the two ends of that range:

    4-8 DTE   modelled EV -315/trade, realised +109   too pessimistic
    0-1 DTE   modelled EV +2,287/trade, realised +23  far too optimistic

One assumption, two opposite errors. If the term structure is not flat, both
are explained at once. This measures it from the collected bars rather than
arguing about it.

IT ALSO SEPARATES THE TWO KINDS OF MOVE

A calendar day contains a session and a gap. The model prices them as one
number. But the week of 7-9 September fell 315 points while no single session
moved more than 65 -- the damage arrived between sessions, not during them.
If that holds generally it decides which book is viable: a position flat by
15:15 never holds gap risk, and gap risk may be most of the risk.

METHOD, AND ITS LIMITS

Realised sigma over a horizon is sqrt(mean(move^2)) -- zero drift assumed,
matching the model being tested rather than fitting a drift the model does
not have.

Windows are NON-OVERLAPPING. Overlapping windows would quadruple the row
count and none of the extra rows would be independent; the honest sample is
printed beside every number so a ratio computed from nine observations is
not read as though it came from nine hundred.

Usage:  python realised_vol.py
"""

import glob
import os
from datetime import date, datetime
from math import sqrt

import pandas as pd

BARS = "data/bars_1m"
NIFTY_TOKEN = 256265
VIX_TOKEN = 264969
SESSION_MINUTES = 375
BASIS = 365.0

# A 375-minute window needs 376 bars, and a session has 375 -- the
# full-session move is reported in its own section below instead.
HORIZONS = [15, 30, 60, 120, 180]      # trading minutes


def sessions():
    out = []
    for p in sorted(glob.glob(os.path.join(BARS, "date=*"))):
        try:
            out.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    return out


def index_day(day):
    """(nifty series, vix close) for one session, or (None, None)."""
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", "underlying=INDEX", "*.parquet")))
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(
                f, columns=["ts", "instrument_token", "close"]))
        except (OSError, ValueError, KeyError):
            continue
    if not frames:
        return None, None
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["ts"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df["ts"] = ts.dt.floor("min")

    n = df[df["instrument_token"] == NIFTY_TOKEN].sort_values("ts")
    v = df[df["instrument_token"] == VIX_TOKEN].sort_values("ts")
    if n.empty or v.empty:
        return None, None
    return n.reset_index(drop=True), float(v["close"].iloc[-1])


def implied_sigma(spot, vix, days):
    return spot * (vix / 100.0) * sqrt(days / BASIS)


def rms(xs):
    return sqrt(sum(x * x for x in xs) / len(xs)) if xs else None


def main():
    days = sessions()
    if not days:
        print("no sessions")
        return

    loaded = {}
    for d in days:
        n, v = index_day(d)
        if n is not None:
            loaded[d] = (n, v)
    if not loaded:
        print("no index bars")
        return
    got = sorted(loaded)

    print(f"REALISED vs IMPLIED   {got[0]} -> {got[-1]}   "
          f"{len(got)} sessions\n")

    # ------------------------------------------------ intraday term structure
    print("INTRADAY, BY HORIZON   (non-overlapping windows, within sessions)\n")
    hdr = (f"{'horizon':>10}{'windows':>9}{'implied':>10}{'realised':>10}"
           f"{'ratio':>8}   what it means")
    print(hdr)
    print("-" * (len(hdr) + 14))

    ratios = {}
    for h in HORIZONS:
        moves, imps = [], []
        for d in got:
            n, vix = loaded[d]
            c = n["close"].astype(float).tolist()
            for i in range(0, len(c) - h, h):
                moves.append(c[i + h] - c[i])
                imps.append(implied_sigma(c[i], vix, h / SESSION_MINUTES))
        if not moves:
            continue
        r, im = rms(moves), sum(imps) / len(imps)
        ratios[h] = r / im if im else None
        note = ("realised BELOW implied -- premium was rich"
                if r < im else
                "realised ABOVE implied -- premium was cheap")
        label = f"{h}m"
        print(f"{label:>10}{len(moves):>9}{im:>10.1f}{r:>10.1f}"
              f"{r/im:>8.2f}   {note}")

    # ----------------------------------------------- session vs overnight gap
    print()
    print("WHERE THE MOVEMENT ACTUALLY HAPPENS\n")
    intraday, gaps = [], []
    for i, d in enumerate(got):
        n, _ = loaded[d]
        c = n["close"].astype(float).tolist()
        intraday.append(c[-1] - c[0])
        if i:
            prev = loaded[got[i - 1]][0]["close"].astype(float).iloc[-1]
            gaps.append(c[0] - float(prev))

    ri, rg = rms(intraday), rms(gaps)
    print(f"  within a session   rms {ri:>7.1f} pts   "
          f"({len(intraday)} sessions)")
    if rg:
        print(f"  overnight gap      rms {rg:>7.1f} pts   ({len(gaps)} gaps)")
        tot = ri * ri + rg * rg
        print()
        print(f"  share of a calendar day's variance:")
        print(f"    session          {ri*ri/tot:>6.1%}")
        print(f"    gap              {rg*rg/tot:>6.1%}")
        print()
        if rg > ri:
            print("  The gap is the larger risk. A book flat by 15:15 never")
            print("  holds it; a book carried to expiry holds it every night.")
            print("  That is a structural difference between the two, not a")
            print("  matter of which one is luckier.")
        else:
            print("  The session carries more risk than the gap, so going")
            print("  flat overnight buys less protection than it costs in")
            print("  premium given up.")

    # -------------------------------------------------------- what it implies
    print()
    print("WHAT THIS DOES TO THE MODEL\n")
    if not ratios:
        return
    vals = [v for v in ratios.values() if v]
    lo, hi = min(vals), max(vals)
    intraday_flat = (hi - lo) < 0.15
    print(f"  intraday ratio {lo:.2f} to {hi:.2f} across horizons "
          f"({'flat' if intraday_flat else 'sloping'})")

    # The intraday rows above sample only WITHIN sessions, so they say nothing
    # about a horizon that crosses a night. Scale the model's per-session
    # sigma up to a full session and compare it with session AND gap together
    # -- that is what a multi-day position actually faces, and it is a
    # different number. An earlier version of this script concluded "flat
    # enough for one multiplier" on the intraday rows alone, which would have
    # corrected the intraday end and broken the multi-day end.
    if rg and HORIZONS:
        h = max(HORIZONS)
        per_session_implied = None
        imps = []
        for d in got:
            n, vix = loaded[d]
            c = n["close"].astype(float).tolist()
            for i in range(0, len(c) - h, h):
                imps.append(implied_sigma(c[i], vix, h / SESSION_MINUTES))
        if imps:
            per_session_implied = (sum(imps) / len(imps)) * sqrt(
                SESSION_MINUTES / h)
        total_realised = sqrt(ri * ri + rg * rg)
        print()
        print(f"  over a FULL CALENDAR DAY, session and gap together:")
        print(f"    model says          {per_session_implied:>7.1f} pts")
        print(f"    actually happened   {total_realised:>7.1f} pts")
        if per_session_implied:
            full = total_realised / per_session_implied
            print(f"    ratio               {full:>7.2f}")
            print()
            if abs(full - 1.0) < 0.15 and lo < 0.8:
                print("  So VIX prices the whole day about right. What is wrong")
                print("  is the DISTRIBUTION: sqrt(t) spreads variance evenly")
                print("  across the clock, and the market does not.")
                print()
                print(f"    inside the session   {ri*ri/(ri*ri+rg*rg):>6.1%} of the variance")
                print(f"    across the night     {rg*rg/(ri*ri+rg*rg):>6.1%}")
                print()
                print("  A position held only during the session collects")
                print("  premium priced for all of it and carries a fraction")
                print("  of it. That is a structural edge, and it is the only")
                print("  edge these ten sessions actually show.")
                print()
                print("  ONE MULTIPLIER WILL NOT DO. It would fix the intraday")
                print("  end and break the multi-day end. Session variance and")
                print("  gap variance have to be modelled as separate terms.")
            else:
                print("  The intraday and full-day ratios do not tell a single")
                print("  clean story here. Treat both as measurements, not as")
                print("  a model, until more sessions are in.")

    print()
    print(f"  {len(got)} sessions, {len(gaps)} gaps. The gap figure in")
    print("  particular rests on very few observations and a handful of large")
    print("  moves; it establishes the SHAPE of the error, and nothing more.")


if __name__ == "__main__":
    main()
