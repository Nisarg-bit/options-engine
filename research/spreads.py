"""What does crossing the spread actually cost?

Every backtest so far charged 1 point of slippage per leg. That number was a
guess, made before there was any data to check it against. There is now:
five sessions of two-sided quotes on 1,713 contracts, captured minute by
minute.

This matters most for the straddle-versus-condor question. A condor has four
legs where a straddle has two, so it pays the spread twice as often -- and
its measured edge was already thin. If a leg really costs 3 points to cross,
the condor's advantage evaporates; if it costs half a point, the defined-risk
case gets stronger. The conclusion currently rests on my guess rather than
your data.

Two things measured here:

  1. The half-spread by MONEYNESS and TIME OF DAY. Half, because crossing
     costs you the distance from mid to the side you trade on -- you sell at
     the bid and buy at the ask, and mid is the fair reference between them.

  2. What that sums to for each structure, against the 1-point assumption.

One nuance the backtest got right by accident: a position held to expiry is
CASH SETTLED, so it crosses the spread only on the way in. Two crossings for
a straddle, four for a condor -- not the four and eight the workbook's
Setup!B61 assumes for a round trip. Held to expiry, the exit is free.

Usage:  python spreads.py [--days=N]
"""

import glob
import os
import sys
from datetime import date

import pandas as pd

UNDERLYING = "NIFTY"

# Moneyness buckets, as |strike/forward - 1|. Named for how a trader would
# describe them rather than by number.
BUCKETS = [
    ("ATM   <0.25%", 0.0000, 0.0025),
    ("near  0.25-1%", 0.0025, 0.0100),
    ("mid    1-2%", 0.0100, 0.0200),
    ("far    2-4%", 0.0200, 0.0400),
    ("deep    >4%", 0.0400, 9.9999),
]

# Representative structures, as offsets from the forward in percent. Close
# enough to what strategies.py picks to make the totals meaningful, without
# needing VIX on this machine.
STRUCTURES = [
    ("short straddle",   [(0.000, "CE", 1), (0.000, "PE", 1)]),
    ("1% strangle",      [(0.010, "CE", 1), (-0.010, "PE", 1)]),
    ("2% strangle",      [(0.020, "CE", 1), (-0.020, "PE", 1)]),
    ("iron condor",      [(0.010, "CE", 1), (-0.010, "PE", 1),
                          (0.018, "CE", -1), (-0.018, "PE", -1)]),
    ("iron butterfly",   [(0.000, "CE", 1), (0.000, "PE", 1),
                          (0.008, "CE", -1), (-0.008, "PE", -1)]),
]

ASSUMED_PER_LEG = 1.0
LOT = 65


def sessions(limit=None):
    days = []
    for p in sorted(glob.glob("data/bars_1m/date=*")):
        try:
            days.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    return days[-limit:] if limit else days


def load(day):
    files = glob.glob(f"data/bars_1m/date={day}/underlying={UNDERLYING}/*.parquet")
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return df if not df.empty else None


def front_expiry(df, day):
    exps = sorted(e for e in df["expiry"].unique() if (e - day).days >= 1)
    return exps[0] if exps else None


def snapshot(df, expiry, ts):
    """Quotes at one minute: {(strike, type): (bid, ask, bid_qty, ask_qty)}."""
    sub = df[(df["expiry"] == expiry) & (df["ts"] == ts)]
    out = {}
    for _, r in sub.iterrows():
        bid, ask = float(r["bid"]), float(r["ask"])
        if bid > 0 and ask > 0 and ask >= bid:
            out[(float(r["strike"]), r["opt_type"])] = (
                bid, ask, int(r["bid_qty"]), int(r["ask_qty"]))
    return out


def forward(quotes):
    """Parity forward from the quotes themselves: F = K + C - P at the mid."""
    pairs = []
    for (k, t), q in quotes.items():
        if t != "CE":
            continue
        p = quotes.get((k, "PE"))
        if p:
            cm, pm = (q[0] + q[1]) / 2, (p[0] + p[1]) / 2
            pairs.append((abs(cm - pm), k + cm - pm))
    if not pairs:
        return None
    pairs.sort()
    vals = sorted(v for _, v in pairs[:5])
    return vals[len(vals) // 2]


def half_spreads(quotes, F):
    """[(moneyness, half_spread_pts, pct_of_mid, touch_depth_units)]."""
    rows = []
    for (k, _t), (b, a, bq, aq) in quotes.items():
        mid = (b + a) / 2
        if mid <= 0:
            continue
        rows.append((abs(k / F - 1.0), (a - b) / 2, (a - b) / 2 / mid,
                     min(bq, aq)))
    return rows


def nearest(quotes, F, pct, kind):
    """The traded strike nearest F*(1+pct), and its half-spread in points."""
    target = F * (1.0 + pct)
    cands = [k for (k, t) in quotes if t == kind]
    if not cands:
        return None
    k = min(cands, key=lambda x: abs(x - target))
    b, a, _bq, _aq = quotes[(k, kind)]
    return (a - b) / 2


def main():
    limit = None
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            limit = int(arg.split("=")[1])

    days = sessions(limit)
    if not days:
        sys.exit("no collected sessions found")
    print(f"{len(days)} sessions: {days[0]} -> {days[-1]}\n")

    bucket_rows = {name: {} for name, _, _ in BUCKETS}
    struct_rows = {name: {} for name, _ in STRUCTURES}
    slots = []

    for day in days:
        df = load(day)
        if df is None:
            continue
        expiry = front_expiry(df, day)
        if expiry is None:
            continue

        times = sorted(df["ts"].unique())
        if len(times) < 20:
            continue
        picks = [("open", times[0]), ("+15m", times[min(15, len(times) - 1)]),
                 ("midday", times[len(times) // 2]), ("close", times[-1])]
        if not slots:
            slots = [n for n, _ in picks]

        for label, ts in picks:
            q = snapshot(df, expiry, ts)
            if len(q) < 30:
                continue
            F = forward(q)
            if not F:
                continue

            for m, pts, pct, depth in half_spreads(q, F):
                for name, lo, hi in BUCKETS:
                    if lo <= m < hi:
                        bucket_rows[name].setdefault(label, []).append(
                            (pts, pct, depth))
                        break

            for name, legs in STRUCTURES:
                hs = [nearest(q, F, off, kind) for off, kind, _sign in legs]
                if any(h is None for h in hs):
                    continue
                struct_rows[name].setdefault(label, []).append(sum(hs))

    # ------------------------------------------------------------- report 1
    print("HALF-SPREAD BY MONEYNESS AND TIME OF DAY")
    print("median points to cross, one leg\n")
    hdr = f"{'bucket':<16}" + "".join(f"{s:>10}" for s in slots)
    print(hdr)
    print("-" * len(hdr))
    for name, _, _ in BUCKETS:
        line = f"{name:<16}"
        for s in slots:
            v = bucket_rows[name].get(s, [])
            line += f"{pd.Series([p for p, _, _ in v]).median():>10.2f}" if v else f"{'-':>10}"
        print(line)

    print(f"\n{'bucket':<16}" + "".join(f"{s:>10}" for s in slots))
    print("-" * len(hdr))
    print("as % of the option's own mid price")
    for name, _, _ in BUCKETS:
        line = f"{name:<16}"
        for s in slots:
            v = bucket_rows[name].get(s, [])
            line += (f"{pd.Series([c for _, c, _ in v]).median()*100:>9.1f}%"
                     if v else f"{'-':>10}")
        print(line)

    # ------------------------------------------------------- report 1b
    print("\n\nDEPTH AT THE TOUCH")
    print("the spread above is only the real cost if the quote is deep enough\n")
    hdr_d = (f"{'bucket':<16}{'median units':>14}{'>= 1 lot':>10}"
             f"{'>= 5 lots':>11}{'>= 10 lots':>12}")
    print(hdr_d)
    print("-" * len(hdr_d))
    for name, _, _ in BUCKETS:
        v = [d for s_ in slots for _, _, d in bucket_rows[name].get(s_, [])]
        if not v:
            print(f"{name:<16}{'-':>14}")
            continue
        ser = pd.Series(v)
        print(f"{name:<16}{ser.median():>14,.0f}"
              f"{(ser >= LOT).mean()*100:>9.0f}%"
              f"{(ser >= 5*LOT).mean()*100:>10.0f}%"
              f"{(ser >= 10*LOT).mean()*100:>11.0f}%")
    print(f"\n  Depth is the smaller of bid_qty and ask_qty, in units "
          f"(1 lot = {LOT}).")
    print("  Only the touch is visible -- the collector stores level 1, not the")
    print("  full book. Where depth is short of your order size the true cost is")
    print("  HIGHER than the half-spread above, by an amount this cannot see.")

    # ------------------------------------------------------------- report 2
    print("\n\nCOST TO ENTER, ALL LEGS, IN POINTS")
    print("one crossing per leg -- held to expiry, cash settlement is free\n")
    hdr2 = (f"{'structure':<18}{'legs':>5}{'assumed':>9}"
            + "".join(f"{s:>10}" for s in slots) + f"{'vs assumed':>12}")
    print(hdr2)
    print("-" * len(hdr2))
    for name, legs in STRUCTURES:
        assumed = ASSUMED_PER_LEG * len(legs)
        line = f"{name:<18}{len(legs):>5}{assumed:>9.1f}"
        opens = struct_rows[name].get(slots[0], [])
        for s in slots:
            v = struct_rows[name].get(s, [])
            line += f"{pd.Series(v).median():>10.2f}" if v else f"{'-':>10}"
        if opens:
            ratio = pd.Series(opens).median() / assumed
            line += f"{ratio:>11.2f}x"
        print(line)

    print("""
HOW TO READ THIS
  The 'vs assumed' column compares the measured cost AT THE OPEN -- which is
  when the backtest enters -- against the 1-point-per-leg it charged. Above
  1.00x means every backtested result is more optimistic than reality, and
  by that factor on the entry cost.

  Watch the condor and butterfly rows especially. Four legs pay the spread
  twice as often as two, and their measured edge was already thin enough
  that a 2x cost error would erase it.

  The open column is usually the worst of the four. That is not a data
  problem -- it is what the market looks like in the first minute, and it is
  exactly when this strategy enters.""")


if __name__ == "__main__":
    main()
