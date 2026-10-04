"""What is the gate actually selling?

THE ALGEBRA

Under Bachelier a fair ATM straddle is worth sqrt(2/pi) * sigma = 0.7979
sigma. So the market's own implied sigma, in points, is readable straight off
the chain with no solver and no volatility assumption:

    sigma_market = ATM straddle credit / 0.7979

Substituting that into the entry gate:

    gate opens  <=>  credit / sigma_model >= 0.9346
                <=>  sigma_market >= 1.171 * sigma_model

The gate has never been a richness detector. It fires whenever the market's
implied volatility exceeds this model's by 17%, and then sells the
difference -- which is profitable only if the model is right and the market
is wrong.

WHY THAT IS ALARMING

Three facts that were collected separately:

  * the gate only ever opens at 0-1 DTE
  * at 0 DTE the model is 20 points overconfident, which needs sigma to be
    1.53x larger than modelled to explain
  * sigma_day is derived from VIX -- a THIRTY-DAY implied volatility --
    applied to a same-day horizon

If those are one fact, then the market is pricing short-dated volatility far
above VIX (which is the normal state of the world), the model is reading
that gap as edge, and the gate is the mechanism that turns a modelling error
into a trade. The system would be selling its own mistake, and the 0 DTE
losses are the market collecting.

WHAT THIS MEASURES

For every decision minute already in the backtest: the ATM straddle's
implied sigma from the chain, against the model's sigma_expiry for the same
moment, bucketed by days to expiry. No new data, no fetching, no fitting.

  ratio ~ 1.0 at every DTE     the model agrees with the market; the gate
                               fires on genuine richness; hypothesis dead.
  ratio rising toward expiry    the model understates short-dated vol, the
                               gate is a disagreement detector, and the
                               "edge" is the error.

The gate threshold of 1.171 is drawn on the table so it is obvious which
buckets can ever trade.

Usage:
    python vol_spread.py
    python vol_spread.py --newvol      price with the fitted calibration
"""

import sys
from datetime import datetime, time as dtime
from math import pi, sqrt

import intraday as I
import vol_time as V
from position import CE, PE

STRADDLE_K = sqrt(2.0 / pi)        # 0.7979
GATE_RATIO = I.CS_GATE / STRADDLE_K  # 1.171
BUCKETS = [(0, 0, "0 DTE (expiry day)"), (1, 2, "1-2 DTE"),
           (3, 5, "3-5 DTE"), (6, 99, "6+ DTE")]


def bucket_of(dte):
    for lo, hi, name in BUCKETS:
        if lo <= dte <= hi:
            return name
    return "?"


def quart(vals):
    s = sorted(vals)
    n = len(s)
    return s[n // 4], s[n // 2], s[(3 * n) // 4]


def main():
    cal = I.load_calibration() if "--newvol" in sys.argv else None
    if "--newvol" in sys.argv and cal is None:
        print(f"--newvol asked for but {I.CALIBRATION} is missing; "
              f"using the old sigma\n")
    print(f"SIGMA: {'fitted calibration' if cal else 'old flat sqrt(t)'}")
    print(f"gate opens when sigma_market / sigma_model >= {GATE_RATIO:.3f}\n")

    rows = []
    for day in I.sessions():
        exps = [e for e in I.expiries_on(day) if e >= day]
        if not exps:
            continue
        expiry = exps[0]
        book = I.option_book(day, expiry)
        if not book:
            continue
        idx = I.index_frames(day)
        nifty = idx.get(I.NIFTY_TOKEN, {})
        vixes = idx.get(I.VIX_TOKEN, {})
        if not nifty or not vixes:
            continue
        dte = (expiry - day).days

        for t in sorted(book):
            if not (I.FIRST_ENTRY <= t.time() <= I.LAST_ENTRY):
                continue
            spot, vix = nifty.get(t), vixes.get(t)
            if spot is None or vix is None:
                continue

            # The model's sigma to expiry, exactly as intraday.py builds it.
            exp_days = max(dte, V.horizon_days(
                t, datetime.combine(day, dtime(15, 30))))
            if cal is None:
                sigma_model = V.Volatility(spot, vix / 100.0, exp_days).stdev
            else:
                sigma_model = cal.sigma(
                    spot, vix / 100.0,
                    lead_minutes=V.minutes_between(t.time(), dtime(15, 30)),
                    day_classes=V.day_classes_between(day, expiry))
            if sigma_model <= 0:
                continue

            # The market's sigma, straight off the ATM straddle.
            cands = I.candidates(book[t], spot, sigma_model, ("straddle",))
            if not cands:
                continue
            credit = cands[0]["credit"]
            if credit <= 0:
                continue
            sigma_market = credit / STRADDLE_K
            rows.append((dte, sigma_market / sigma_model, credit,
                         sigma_model, sigma_market))

    if not rows:
        print("no usable minutes")
        return 1

    print(f"SIGMA_MARKET / SIGMA_MODEL   ({len(rows)} decision minutes)\n")
    hdr = (f"{'bucket':<20}{'n':>6}{'p25':>8}{'median':>9}{'p75':>8}"
           f"{'% over gate':>13}   {'sigma_model':>12}{'sigma_mkt':>11}")
    print(hdr)
    print("-" * len(hdr))

    for _, _, name in BUCKETS:
        sub = [r for r in rows if bucket_of(r[0]) == name]
        if not sub:
            continue
        ratios = [r[1] for r in sub]
        lo, mid, hi = quart(ratios)
        over = sum(1 for r in ratios if r >= GATE_RATIO) / len(ratios)
        mmod = sorted(r[3] for r in sub)[len(sub) // 2]
        mmkt = sorted(r[4] for r in sub)[len(sub) // 2]
        print(f"{name:<20}{len(sub):>6}{lo:>8.2f}{mid:>9.2f}{hi:>8.2f}"
              f"{over:>12.0%}   {mmod:>12.1f}{mmkt:>11.1f}")

    print()
    print(f"  'over gate' is the share of minutes where the ratio reaches")
    print(f"  {GATE_RATIO:.3f} -- that is, where the gate can open at all.")
    print()
    print("  READ IT THIS WAY.")
    print("  A median near 1.0 everywhere means the model agrees with the")
    print("  market and the gate fires on real richness. A median that")
    print("  climbs toward expiry means the model understates short-dated")
    print("  volatility, and every trade the gate has ever allowed was")
    print("  taken because of that error rather than in spite of it.")
    print()
    print("  Sigma_market is model-free: it is the straddle price divided")
    print("  by 0.7979, no solver and no volatility assumption. If the two")
    print("  columns disagree, the disagreement is real, and the question")
    print("  is only which side is right about how far NIFTY can move.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
