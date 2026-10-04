"""Does a variance risk premium exist in NIFTY at all?

THE QUESTION THE PROJECT SHOULD HAVE STARTED WITH

Every short-premium strategy rests on one assumption: that options are priced
above what the underlying subsequently does. If implied volatility exceeds
realised volatility on average, selling it pays; if it does not, no amount of
strike selection, exit rules or position sizing can create an edge, because
there is nothing there to collect.

We found out the hard way that the system's apparent edge at 0 DTE was its
own sigma error -- the market priced short-dated volatility 33% above a
VIX-derived model, the gate read that disagreement as richness, and the
market was closer to right. So the honest next question is not "how do we fix
the gate" but "is there a premium here in the first place".

WHAT CAN AND CANNOT BE MEASURED FROM THIS DATA

We have five years of daily NIFTY and INDIA VIX closes. That is enough to
measure the premium AT VIX'S OWN HORIZON -- thirty days -- properly and
cleanly.

It is NOT enough to measure the premium at seven days or on expiry day,
because that needs the implied volatility of the option actually being sold,
and we have only about eleven sessions of collected chains. Scaling VIX down
to a short horizon and calling the result an implied volatility is EXACTLY
the error that produced the fake edge. This file will not repeat it. The
short-horizon answer needs data we do not yet have, and the right response
is to start collecting it, not to estimate it from the wrong instrument.

THE MEASUREMENT

For each day t, a seller of a 30-day ATM straddle receives, under Bachelier,

    premium = sqrt(2/pi) * sigma_implied = 0.7979 * S_t * (VIX_t/100)
              * sqrt(30/365)

and pays out |S_(t+30) - S_t| at expiry. The difference is the trade's P&L
in index points. Averaged over five years, its sign is the answer.

A CONVENTION THAT NEARLY PLANTED A FAKE PREMIUM

VIX is an annualised (365-day) volatility quoted for a 30-CALENDAR-day
horizon, so the variance it implies over the next 30 calendar days is
(VIX/100)^2 * 30/365 -- and that variance accrues over only the ~21 TRADING
days inside the window, not over 365 days of wall clock.

Get that backwards and you invent a premium out of nothing. The first
synthetic test of this file generated a per-weekday volatility of
sigma_a*sqrt(1/365), which spreads the variance over 365 days instead of
252 and plants 0.83x the volatility it claims to. The estimator then
faithfully reported +35 points of premium on data constructed to have none.
The estimator was right and the test was wrong, but a reader would have
seen "+35 points" either way.

The measurement below is convention-free where it matters: a premium in
index points against a realised move in index points. The only convention
it relies on is the standard one above, for turning VIX into a premium.

OVERLAPPING WINDOWS ARE THE TRAP

Consecutive days share 29 of their 30 days, so 1,250 daily observations are
nowhere near 1,250 independent trades -- they are closer to 60. A t-statistic
computed on the overlapping series is inflated by roughly sqrt(30), which is
how a coin flip becomes a "highly significant" result. Both are reported: the
overlapping series for its richer picture of the distribution, and a strictly
non-overlapping series as the only one whose significance can be read.

Usage:
    python vrp.py                 5 years, 30-day horizon
    python vrp.py --years 10
    python vrp.py --horizon 30
    python vrp.py --refresh       re-fetch rather than use the cache
"""

import sys
from datetime import date, timedelta
from math import pi, sqrt
from statistics import mean, median, stdev

import pricing as P
import vol_calibrate as VC

STRADDLE_K = sqrt(2.0 / pi)          # 0.7979
DEFAULT_HORIZON = 30                 # calendar days -- VIX's own horizon
DEFAULT_YEARS = 5
LOT = 75
COSTS = P.CURRENT_COSTS   # one definition, in pricing.py -- see the history there


def build(nifty, vixmap, horizon, haircut=1.0):
    """[(day, S, vix, premium_pts, payoff_pts, pnl_pts)] for every start day.

    The exit is the first session at or after t + horizon calendar days, which
    is how a real expiry falls. Days without a VIX print, or too close to the
    end of the history to have an exit, are dropped rather than guessed at.
    """
    closes = [(d, c) for d, _o, _h, _l, c in nifty if c and c > 0]
    closes.sort()
    days = [d for d, _ in closes]
    px = dict(closes)

    out = []
    for i, (d, s) in enumerate(closes):
        v = vixmap.get(d)
        if not v or v <= 0:
            continue
        target = d + timedelta(days=horizon)
        j = None
        for k in range(i + 1, len(days)):
            if days[k] >= target:
                j = k
                break
        if j is None:
            continue
        sigma = s * (v / 100.0) * sqrt(horizon / 365.0)
        # The haircut is what atm_vs_vix.py measured: VIX is a variance-swap
        # construct across all strikes and sits above the at-the-money vol,
        # so the ATM straddle seller is paid r times what VIX implies. The
        # payoff is untouched -- the index moves what it moves.
        premium = STRADDLE_K * sigma * haircut
        payoff = abs(px[days[j]] - s)
        out.append((d, s, v, premium, payoff, premium - payoff))
    return out


def summarise(label, rows, note=""):
    if len(rows) < 5:
        print(f"{label}: too few observations ({len(rows)})")
        return
    pnl = [r[5] for r in rows]
    prem = [r[3] for r in rows]
    pay = [r[4] for r in rows]
    n = len(pnl)
    m = mean(pnl)
    sd = stdev(pnl) if n > 1 else 0.0
    se = sd / sqrt(n) if n > 1 else 0.0
    t = m / se if se else 0.0
    wins = sum(1 for p in pnl if p > 0)
    # Round-trip cost of the straddle, expressed in index points so it can be
    # compared with the premium directly.
    cost_pts = P.transaction_costs(mean(prem), LOT, 1, defined_risk=False,
                                   costs=COSTS) / LOT

    print(f"{label}   n = {n}{note}")
    print(f"   mean premium received     {mean(prem):>9.1f} pts")
    print(f"   mean payoff made          {mean(pay):>9.1f} pts")
    print(f"   mean P&L (gross)          {m:>9.1f} pts")
    print(f"   round-trip cost           {cost_pts:>9.1f} pts")
    print(f"   mean P&L (net)            {m - cost_pts:>9.1f} pts")
    print(f"   median P&L                {median(pnl):>9.1f} pts")
    print(f"   win rate                  {wins/n:>9.1%}")
    print(f"   realised / implied sigma  "
          f"{mean(pay)/mean(prem):>9.3f}")
    print(f"   std dev of P&L            {sd:>9.1f} pts")
    print(f"   t-statistic (gross)       {t:>9.2f}")
    print()


def tail(label, rows):
    """The left tail, which is the whole risk of a short-premium book.

    A mean of +136 points with a standard deviation of 452 says almost
    nothing about survivability. What decides whether this is tradeable is
    the size of the worst outcomes and how many ordinary wins one of them
    erases -- and, because the trades are sequential in a single-position
    book, the deepest cumulative drawdown along the way.
    """
    if len(rows) < 5:
        return
    pnl = [r[5] for r in rows]
    s_pnl = sorted(pnl)
    n = len(pnl)
    m = mean(pnl)
    print(f"{label} -- THE LEFT TAIL   n = {n}\n")
    print(f"   worst trade               {s_pnl[0]:>9.1f} pts")
    print(f"   5th percentile            {s_pnl[max(0, n//20)]:>9.1f} pts")
    print(f"   25th percentile           {s_pnl[n//4]:>9.1f} pts")
    print(f"   75th percentile           {s_pnl[(3*n)//4]:>9.1f} pts")
    print(f"   best trade                {s_pnl[-1]:>9.1f} pts")
    if m > 0:
        print(f"   one worst trade erases    "
              f"{abs(s_pnl[0])/m:>9.1f} average wins")
    print()
    print("   the five worst:")
    for r in sorted(rows, key=lambda r: r[5])[:5]:
        print(f"      {r[0]}   VIX {r[2]:>5.1f}   premium {r[3]:>7.1f}   "
              f"move {r[4]:>7.1f}   P&L {r[5]:>8.1f}")
    # Sequential drawdown: one position at a time, in order.
    peak = run = worst_dd = 0.0
    for r in rows:
        run += r[5]
        peak = max(peak, run)
        worst_dd = min(worst_dd, run - peak)
    print()
    print(f"   cumulative P&L            {run:>9.1f} pts")
    print(f"   deepest drawdown          {worst_dd:>9.1f} pts")
    if run > 0:
        print(f"   drawdown / total profit   {abs(worst_dd)/run:>9.1%}")
    print()


def independent(rows, horizon):
    out, last_exit = [], None
    for r in rows:
        if last_exit is None or r[0] >= last_exit:
            out.append(r)
            last_exit = r[0] + timedelta(days=horizon)
    return out


def sweep(nifty, vixmap, horizons, haircut=1.0, min_vix=None):
    """Where along the curve does the premium actually live?

    VIX is a thirty-day number, so using it at other horizons assumes the
    implied-vol term structure is flat. It is not. Measured against the
    chain, market sigma over VIX-scaled sigma ran 1.02 at 3-5 DTE, 1.20 at
    1-2 DTE and 1.33 at 0 DTE -- so at a week or more this is roughly
    honest, and at anything shorter the premium shown here is understated,
    because the real seller receives more than VIX implies.
    """
    print("=" * 66)
    print("TERM STRUCTURE OF THE PREMIUM (non-overlapping windows)"
          + (f"   VIX >= {min_vix:.1f}" if min_vix is not None else "")
          + "\n")
    hdr = (f"{'horizon':>9}{'n':>6}{'premium':>10}{'payoff':>10}"
           f"{'net P&L':>10}{'r/i':>8}{'win':>8}{'t':>8}")
    print(hdr)
    print("-" * len(hdr))
    for h in horizons:
        built = build(nifty, vixmap, h, haircut)
        if min_vix is not None:
            # Filter BEFORE choosing non-overlapping windows -- see main().
            # Omitting this printed unconditional numbers under a heading
            # inside a conditional run, which is a lie by omission.
            built = [r for r in built if r[2] >= min_vix]
        rows = independent(built, h)
        if len(rows) < 5:
            print(f"{h:>9}   too few independent windows")
            continue
        pnl = [r[5] for r in rows]
        prem = [r[3] for r in rows]
        pay = [r[4] for r in rows]
        m, sd = mean(pnl), stdev(pnl)
        t = m / (sd / sqrt(len(pnl))) if sd else 0.0
        cost = P.transaction_costs(mean(prem), LOT, 1, defined_risk=False,
                                   costs=COSTS) / LOT
        print(f"{h:>9}{len(rows):>6}{mean(prem):>10.1f}{mean(pay):>10.1f}"
              f"{m - cost:>10.1f}{mean(pay)/mean(prem):>8.3f}"
              f"{sum(1 for x in pnl if x > 0)/len(pnl):>8.1%}{t:>8.2f}")
    print()
    print("   Fewer independent windows at longer horizons, so the t there")
    print("   is built on less. At 7 days the premium is understated for the")
    print("   reason above; treat a positive number there as a floor.")
    print()


def main():
    years, horizon, haircut = DEFAULT_YEARS, DEFAULT_HORIZON, 1.0
    min_vix = None
    refresh = "--refresh" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--years":
            years = int(sys.argv[i + 1])
        if a == "--horizon":
            horizon = int(sys.argv[i + 1])
        if a == "--haircut":
            haircut = float(sys.argv[i + 1])
        if a == "--min-vix":
            min_vix = float(sys.argv[i + 1])

    end = date.today()
    start = end - timedelta(days=int(365.25 * years) + horizon + 10)

    print(f"VARIANCE RISK PREMIUM   NIFTY   {start} -> {end}")
    if haircut != 1.0:
        print(f"selling a {horizon}-day ATM straddle at {haircut:.3f} x the "
              f"VIX-implied price")
        print(f"  (atm_vs_vix.py measured that the ATM straddle trades "
              f"below VIX)\n")
    else:
        print(f"selling a {horizon}-day ATM straddle at the VIX-implied "
              f"price\n")

    import session as kite_session
    try:
        kite = kite_session.get_kite()
    except Exception as e:
        print(f"kite session failed: {type(e).__name__}: {e}")
        return 1

    print("NIFTY 50")
    nifty = VC.fetch(kite, VC.NIFTY_TOKEN, start, end, refresh)
    print("INDIA VIX")
    vix = VC.fetch(kite, VC.VIX_TOKEN, start, end, refresh)
    if not nifty or not vix:
        print("\nno candles -- cannot measure")
        return 1
    vixmap = {d: c for d, _o, _h, _l, c in vix}

    rows = build(nifty, vixmap, horizon, haircut)
    if not rows:
        print("no usable windows")
        return 1

    if min_vix is not None:
        before = len(rows)
        # Filter BEFORE choosing non-overlapping windows. Filtering after
        # would skip qualifying entries that happened to fall inside a
        # discarded window, which is not the strategy anyone would run.
        rows = [r for r in rows if r[2] >= min_vix]
        print("=" * 66)
        print("!! POST-HOC FILTER: entries only when VIX >= "
              f"{min_vix:.1f}  ({before} -> {len(rows)} start days)")
        print()
        print("   READ THE RESULT WITH THIS IN MIND. The VIX threshold was")
        print("   chosen AFTER seeing that the bottom two quartiles had no")
        print("   premium. Testing it on the same data that suggested it is")
        print("   data-mining: four buckets were available, the best two")
        print("   were picked, and the sample is now half the size. A t of")
        print("   2.1 earned this way is worth far less than a t of 2.1")
        print("   earned on a threshold chosen in advance.")
        print()
        print("   The economic logic is sound -- there is no fear premium")
        print("   to collect when nobody is afraid -- and the pattern did")
        print("   replicate across the 5- and 10-year samples. But those")
        print("   overlap almost entirely, so that is one confirmation,")
        print("   not two.")
        print()
        print("   Treat whatever follows as a hypothesis worth carrying")
        print("   forward, never as a validated result.")
        print()
        if not rows:
            print("no windows above that VIX level")
            return 1
    print(f"\n{len(rows)} start days with a {horizon}-day exit\n")

    print("=" * 66)
    summarise("ALL START DAYS (overlapping)", rows,
              note="  -- distribution only, NOT a significance test")
    print("  Consecutive windows share almost every day, so this t-statistic")
    print(f"  is inflated by roughly sqrt({horizon}) = {sqrt(horizon):.1f}. "
          f"Read the shape,\n  not the significance.\n")

    print("=" * 66)
    # Strictly non-overlapping: step forward to the first start day at or
    # after the previous trade's exit. These are genuinely independent.
    indep = independent(rows, horizon)
    summarise("NON-OVERLAPPING WINDOWS", indep,
              note="  -- this is the one whose t-statistic means something")
    if len(indep) > 2:
        m = mean([r[5] for r in indep])
        sd = stdev([r[5] for r in indep])
        t = m / (sd / sqrt(len(indep)))
        verdict = ("premium is real at this horizon" if t > 2
                   else "no premium detectable" if abs(t) <= 2
                   else "REVERSED -- selling lost money")
        print(f"  |t| > 2 would be evidence. This reads {t:+.2f}: {verdict}.")
        print()

    print("=" * 66)
    tail("NON-OVERLAPPING", indep)

    sweep(nifty, vixmap, [7, 14, 30, 45, 60], haircut, min_vix)

    print("=" * 66)
    print("BY VIX LEVEL AT ENTRY (overlapping; shape only)\n")
    byv = sorted(rows, key=lambda r: r[2])
    q = len(byv) // 4
    for lo, hi, name in ((0, q, "lowest quartile"), (q, 2 * q, "second"),
                         (2 * q, 3 * q, "third"), (3 * q, len(byv),
                                                   "highest quartile")):
        sub = byv[lo:hi]
        if not sub:
            continue
        pnl = [r[5] for r in sub]
        print(f"  {name:<18} VIX {sub[0][2]:>5.1f}-{sub[-1][2]:>5.1f}   "
              f"mean P&L {mean(pnl):>7.1f} pts   "
              f"win {sum(1 for p in pnl if p > 0)/len(sub):>5.1%}   "
              f"r/i {mean(r[4] for r in sub)/mean(r[3] for r in sub):>5.2f}")

    print()
    print("=" * 66)
    print("WHAT THIS DOES NOT MEASURE")
    print("  This is the premium at THIRTY days, VIX's own horizon. It says")
    print("  nothing about seven days or expiry day, because measuring those")
    print("  needs the implied volatility of the option actually sold, and")
    print("  scaling VIX down to a short horizon is precisely the error that")
    print("  produced the fake edge in the first place. The short-horizon")
    print("  answer requires collecting chains forward, which the system is")
    print("  already doing.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
