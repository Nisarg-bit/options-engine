"""Phase 2.4 exit gate -- the improved model's numbers beside the legacy ones.

The plan requires a second suite showing what each change actually did,
rather than assuming the better model is better. Three changes are on trial:

  1. sigma from the CHAIN's own ATM implied vol, instead of India VIX
  2. LOGNORMAL terminal distribution, instead of the workbook's normal one
  3. BARRIER-correct P(touch), instead of min(1, 2*(1-POP))

Only the first can change which days you trade, so only the first gets the
full backtest treatment. The other two are reported as distributions of the
difference they make, because a change that never alters a decision is not
an improvement, it is a rounding.

The selection test uses PERCENTILE thresholds, not absolute ones. VIX-sigma
and IV-sigma live on different scales, so "credit/sigma >= 1.0" is not the
same filter under each. Ranking days and taking the top X% asks the only
fair question: given the same number of trades, does the better volatility
pick better days?

Usage:  python model_compare.py 2024-07-08 2026-08-28
"""

import sys
from datetime import date

import pandas as pd

import backtest as bt
import models as M
import pricing as P
import vix as vixmod

FRACTIONS = [0.50, 0.40, 0.30, 0.20, 0.10]


def chain_rows(day_df, expiry):
    ch = day_df[day_df["XpryDt"] == expiry]
    ce = ch[ch["OptnTp"] == "CE"].set_index("StrkPric")["ClsPric"]
    pe = ch[ch["OptnTp"] == "PE"].set_index("StrkPric")["ClsPric"]
    common = ce.index.intersection(pe.index)
    return [{"strike": float(k), "ce": float(ce[k]), "pe": float(pe[k])}
            for k in sorted(common)]


def atm_straddle(chain, level):
    if not chain:
        return None
    row = min(chain, key=lambda r: abs(r["strike"] - level))
    v = row["ce"] + row["pe"]
    return v if v > 0 else None


def episodes(sub):
    if len(sub) == 0:
        return 0
    n, busy = 0, None
    for entry, exp in sorted(zip(sub["entry_day"], sub["expiry"])):
        if busy is None or entry > busy:
            n += 1
            busy = exp
    return n


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python model_compare.py 2024-07-08 2026-08-28")
    start, end = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])

    raw = bt.load(start, end)
    by_day = {d: g for d, g in raw.groupby("date")}
    days = sorted(by_day)
    vix = vixmod.load().to_dict()

    rows = []
    for i in range(len(days) - 1):
        r = bt.run_trade(days[i], days[i + 1], by_day, days)
        if not r:
            continue
        d = r["signal_day"]
        v = vix.get(d)
        if v is None or v != v:
            continue

        chain = chain_rows(by_day[d], r["expiry"])
        if len(chain) < 15:
            continue
        S = r["spot"]
        dte = max(r["dte"], 1)
        T = dte / 365.0

        F = M.forward_from_parity(chain, S)
        iv = M.atm_iv(chain, S, T, forward=F)
        if iv is None or iv <= 0:
            continue

        strad = atm_straddle(chain, F)
        if strad is None:
            continue

        sig_vix = P.Volatility(S, v / 100.0, dte).expiry_stdev
        sig_iv = M.points_from_annual_vol(iv, S, T)
        if sig_vix <= 0 or sig_iv <= 0:
            continue

        lower, upper = r["strike"] - strad, r["strike"] + strad
        pop_norm = P.probability_of_profit(S, lower, upper, sig_vix)
        pop_logn = M.probability_of_profit(S, lower, upper, v / 100.0, T)
        touch_wb = P.probability_stop_touched(pop_norm)
        touch_bc = M.prob_touch_either(S, lower, upper, v / 100.0, T)

        rows.append({
            "signal_day": d, "entry_day": r["entry_day"], "expiry": r["expiry"],
            "dte": dte, "spot": S, "forward": F, "basis": F - S,
            "vix": v, "atm_iv": iv * 100.0, "iv_minus_vix": iv * 100.0 - v,
            "skew": (M.skew(chain, S, T, 0.04, forward=F) or float("nan")) * 100.0,
            "straddle": strad,
            "cs_vix": strad / sig_vix, "cs_iv": strad / sig_iv,
            "pop_norm": pop_norm, "pop_logn": pop_logn,
            "touch_wb": touch_wb, "touch_bc": touch_bc,
            "pnl": r["A_pnl"] / r["lot"],      # points per unit -- size-neutral
        })

    if not rows:
        sys.exit("no sessions produced both a chain IV and a VIX")

    t = pd.DataFrame(rows).sort_values("entry_day").reset_index(drop=True)
    t.to_csv("model_compare_trades.csv", index=False)
    cut = len(t) // 2
    base_is, base_oos = t.iloc[:cut]["pnl"].mean(), t.iloc[cut:]["pnl"].mean()

    print(f"\n{len(t)} sessions   {t['signal_day'].min()} -> {t['signal_day'].max()}")
    print(f"baseline  full {t['pnl'].mean():.0f}   IS {base_is:.0f}   "
          f"OOS {base_oos:.0f}   (points per unit)\n")

    # ---------------------------------------------------------- change 1: IV
    print("=" * 72)
    print("CHANGE 1  chain ATM IV instead of India VIX")
    print("=" * 72)
    g = t["iv_minus_vix"]
    print(f"  ATM IV minus VIX   median {g.median():+.2f}   mean {g.mean():+.2f} vol pts")
    print(f"  IV below VIX on    {(g < 0).mean()*100:.0f}% of sessions")
    print(f"  forward basis      median {t['basis'].median():+.1f} pts "
          f"({(t['basis']/t['spot']).median()*100:+.3f}% of spot)")
    print(f"  put skew (+/-4%)   median {t['skew'].median():+.2f} vol pts")
    print(f"  credit/sigma       VIX median {t['cs_vix'].median():.3f}   "
          f"IV median {t['cs_iv'].median():.3f}")
    print(f"  rank correlation between the two orderings: "
          f"{t['cs_vix'].rank().corr(t['cs_iv'].rank()):.3f}\n")

    hdr = (f"  {'select top':<12}{'n':>5}{'ep':>5}"
           f"{'VIX avg':>10}{'IV avg':>10}{'diff':>9}"
           f"{'VIX OOS':>10}{'IV OOS':>10}{'overlap':>9}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for f in FRACTIONS:
        k = max(int(len(t) * f), 10)
        a = t.nlargest(k, "cs_vix")
        b = t.nlargest(k, "cs_iv")
        overlap = len(set(a.index) & set(b.index)) / k * 100
        a_oos = a[a.index >= cut]["pnl"].mean()
        b_oos = b[b.index >= cut]["pnl"].mean()
        print(f"  {f*100:>9.0f}%{k:>6}{episodes(b):>5}"
              f"{a['pnl'].mean():>10.0f}{b['pnl'].mean():>10.0f}"
              f"{b['pnl'].mean()-a['pnl'].mean():>+9.0f}"
              f"{a_oos:>10.0f}{b_oos:>10.0f}{overlap:>8.0f}%")

    # --------------------------------------------------- change 2: lognormal
    print("\n" + "=" * 72)
    print("CHANGE 2  lognormal terminal distribution instead of normal")
    print("=" * 72)
    d = t["pop_logn"] - t["pop_norm"]
    print(f"  POP difference     median {d.median():+.4f}   "
          f"mean {d.mean():+.4f}   max |diff| {d.abs().max():.4f}")
    flips = ((t["pop_norm"] >= 0.65) != (t["pop_logn"] >= 0.65)).sum()
    print(f"  sessions where the 0.65 gate FLIPS: {flips} of {len(t)} "
          f"({flips/len(t)*100:.1f}%)")

    # ------------------------------------------------------ change 3: barrier
    print("\n" + "=" * 72)
    print("CHANGE 3  barrier-correct P(touch) instead of min(1, 2*(1-POP))")
    print("=" * 72)
    d3 = t["touch_bc"] - t["touch_wb"]
    print(f"  P(touch) difference  median {d3.median():+.4f}   "
          f"mean {d3.mean():+.4f}")
    print(f"  workbook version saturates at 1.0 on "
          f"{(t['touch_wb'] >= 0.999).mean()*100:.0f}% of sessions "
          f"(barrier version: {(t['touch_bc'] >= 0.999).mean()*100:.0f}%)")

    print("""
HOW TO READ THIS
  overlap   share of days the two sigmas agree to trade. Low overlap with a
            small 'diff' means the two models disagree constantly and it does
            not matter -- the worst possible reason to adopt a change.
  diff      IV-selected average minus VIX-selected, points per unit. This is
            the entire value of change 1. If it is small relative to the
            baseline, keep VIX: it is one number from a website versus a
            solver over 30 strikes.
  flips     for changes 2 and 3, how often the new number would alter an
            actual decision. A change that never flips a gate is a rounding,
            however much more correct it is in principle.

per-session detail written to model_compare_trades.csv""")


if __name__ == "__main__":
    main()