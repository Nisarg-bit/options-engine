"""Does a straddle seller actually receive what VIX implies?

THE GAP THIS CLOSES

vrp.py measured the premium using VIX: a seller of a 30-day ATM straddle was
credited 0.7979 * S * (VIX/100) * sqrt(30/365). Over ten years that came back
at realised/implied = 0.828, meaning the seller keeps about 17% of premium.

But VIX is not an at-the-money implied volatility. It is a variance-swap
construct built from the whole strike range, and with NIFTY's put skew it
sits ABOVE the ATM implied vol -- typically by a couple of vol points. A
seller of the ATM straddle therefore receives LESS than VIX implies, and the
whole measured edge was computed from a price nobody was ever offered.

This is the same error that has bitten every other part of this project: a
proxy standing in for the instrument actually traded. The pre-open index
print, the stale index bar near the close, VIX scaled down to expiry day --
each time the proxy looked like the thing and was not.

WHY IT IS DECISIVE

The edge is 1 - 0.828/r, where r is the ratio of what a straddle seller
really gets to what VIX implies:

    r = 1.00   edge 17.2%      VIX is the ATM vol; vrp.py stands
    r = 0.95   edge 12.8%      thinner but real
    r = 0.90   edge  8.0%      halved
    r = 0.85   edge  2.6%      gone after slippage
    r = 0.828  edge  0.0%      there was never anything there

So the answer is not a refinement. It decides whether the last two days of
work describe a strategy or an artefact.

WHAT IT MEASURES

For every collected session and every expiry in the chain: the ATM straddle's
own implied sigma (credit / 0.7979, model-free) against the sigma VIX implies
for the same horizon. Reported by days to expiry, because the ratio is
expected to vary along the curve -- and the 0-2 DTE rows should reproduce the
1.33 and 1.20 already measured independently by vol_spread.py, which is a
check on this file rather than a new finding.

Only the middle of the session is sampled. The open and the close are where
the index feed is least trustworthy, and this measurement divides by a spot
that has to be right.

Usage:
    python atm_vs_vix.py
    python atm_vs_vix.py --from 11:00 --to 14:00
"""

import sys
from datetime import datetime, time as dtime
from math import pi, sqrt
from statistics import mean, median

import intraday as I
import vol_time as V

STRADDLE_K = sqrt(2.0 / pi)
CLOSE = dtime(15, 30)
DEFAULT_FROM, DEFAULT_TO = dtime(11, 0), dtime(14, 0)
VRP_RATIO = 0.828                 # realised/implied from vrp.py, 10 years

BANDS = [(0, 0, "0 DTE"), (1, 2, "1-2 DTE"), (3, 7, "3-7 DTE"),
         (8, 20, "8-20 DTE"), (21, 40, "21-40 DTE"), (41, 999, "41+ DTE")]


def band_of(dte):
    for lo, hi, name in BANDS:
        if lo <= dte <= hi:
            return name
    return "?"


def main():
    t_from, t_to = DEFAULT_FROM, DEFAULT_TO
    for i, a in enumerate(sys.argv):
        if a == "--from":
            hh, mm = sys.argv[i + 1].split(":")
            t_from = dtime(int(hh), int(mm))
        if a == "--to":
            hh, mm = sys.argv[i + 1].split(":")
            t_to = dtime(int(hh), int(mm))

    print("ATM STRADDLE IMPLIED SIGMA vs WHAT VIX IMPLIES")
    print(f"sampling {t_from}-{t_to}, away from the open and the close\n")

    rows = []
    for day in I.sessions():
        idx = I.index_frames(day)
        nifty = idx.get(I.NIFTY_TOKEN, {})
        vixes = idx.get(I.VIX_TOKEN, {})
        if not nifty or not vixes:
            continue
        for expiry in I.expiries_on(day):
            if expiry < day:
                continue
            dte = (expiry - day).days
            book = I.option_book(day, expiry)
            if not book:
                continue
            ratios = []
            for t in sorted(book):
                if not (t_from <= t.time() <= t_to):
                    continue
                spot, vix = nifty.get(t), vixes.get(t)
                if not spot or not vix or spot <= 0 or vix <= 0:
                    continue
                # A nominal sigma only to locate the money; the straddle's
                # strikes do not depend on it.
                cands = I.candidates(book[t], spot, 1.0, ("straddle",))
                if not cands:
                    continue
                credit = cands[0]["credit"]
                if credit <= 0:
                    continue
                sigma_atm = credit / STRADDLE_K
                # Time to expiry in calendar days, counting the remainder of
                # today as the fraction of its session still to run.
                years = (dte + V.minutes_between(t.time(), CLOSE)
                         / V.SESSION_MINUTES) / 365.0
                if years <= 0:
                    continue
                sigma_vix = spot * (vix / 100.0) * sqrt(years)
                if sigma_vix <= 0:
                    continue
                ratios.append(sigma_atm / sigma_vix)
            if len(ratios) >= 3:
                rows.append((day, expiry, dte, median(ratios), len(ratios)))

    if not rows:
        print("no usable session/expiry pairs")
        return 1

    print(f"{len(rows)} session/expiry pairs\n")
    hdr = (f"{'band':<12}{'pairs':>7}{'sessions':>10}{'expiries':>10}"
           f"{'median r':>11}{'min':>8}{'max':>8}")
    print(hdr)
    print("-" * len(hdr))
    for _, _, name in BANDS:
        sub = [r for r in rows if band_of(r[2]) == name]
        if not sub:
            continue
        rs = sorted(r[3] for r in sub)
        print(f"{name:<12}{len(sub):>7}{len({r[0] for r in sub}):>10}"
              f"{len({r[1] for r in sub}):>10}"
              f"{rs[len(rs)//2]:>11.3f}{rs[0]:>8.3f}{rs[-1]:>8.3f}")

    print()
    print("  r = (ATM straddle's own sigma) / (sigma VIX implies).")
    print("  r < 1 means the seller is paid less than vrp.py assumed.")
    print("  The 0-2 DTE rows should echo the 1.33 and 1.20 that")
    print("  vol_spread.py found by a different route -- if they do not,")
    print("  one of the two files is wrong and neither can be trusted.")
    print()

    # --- what it does to the measured edge
    band = [r for r in rows if 21 <= r[2] <= 40]
    print("=" * 62)
    print("WHAT THIS DOES TO THE 30-DAY EDGE\n")
    print(f"  edge = 1 - {VRP_RATIO}/r\n")
    print(f"     {'r':>6}   {'edge':>8}")
    print("     " + "-" * 17)
    for r in (1.00, 0.95, 0.90, 0.85, VRP_RATIO, 0.80):
        edge = 1.0 - VRP_RATIO / r
        mark = "   <- zero here" if abs(edge) < 0.005 else ""
        print(f"     {r:>6.3f}   {edge:>7.1%}{mark}")
    print()
    if band:
        rs = sorted(r[3] for r in band)
        m = rs[len(rs) // 2]
        edge = 1.0 - VRP_RATIO / m
        print(f"  MEASURED at 21-40 DTE:  r = {m:.3f}  over "
              f"{len(band)} pairs, {len({r[0] for r in band})} sessions")
        print(f"  => surviving edge       {edge:>7.1%} of premium")
        print()
        if edge <= 0:
            print("  The edge does not survive. What vrp.py measured was the")
            print("  gap between VIX and the ATM vol, not a premium anyone")
            print("  could have collected by selling straddles.")
        elif edge < 0.05:
            print("  The edge survives but is thin enough that slippage and")
            print("  a wider bid-ask would plausibly consume it. Not a basis")
            print("  for building without live fills to check against.")
        else:
            print("  The edge survives the correction. vrp.py overstated it,")
            print("  but not by enough to remove it.")
    else:
        print("  NO 21-40 DTE CHAIN WAS COLLECTED, so the horizon the")
        print("  strategy would actually trade cannot be checked yet. The")
        print("  collector has been storing the near expiry only. Until it")
        print("  stores the monthly too, the 30-day edge stays unverified")
        print("  against real prices -- which is a reason to widen the")
        print("  collector, not a reason to assume the number holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
