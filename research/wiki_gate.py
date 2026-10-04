"""Your workbook's actual GO/NO-GO gate, backtested on bhavcopy.

Reproduces Daily_Signal!G16:H21 for the SHORT STRADDLE column:

  H16  positive EV available     net EV > 0
  H17  POP acceptable            POP >= 0.65
  H18  premium not cheap         rich/cheap ratio >= 0.95

with sigma exactly as Option_Chain!B19 builds it:

  sigma = spot * (VIX/100)/sqrt(365) * sqrt(days_to_expiry)

and fair value as Daily_Signal!J9:  sqrt(2/pi) * sigma.

All three gates turn out to be monotone in one quantity -- credit/sigma --
so the script also sweeps that threshold directly, to show whether your
0.65 POP cutoff is a special point or just one spot on a smooth curve.

Usage:  python wiki_gate.py 2025-09-01 2026-08-28
"""

import sys
from datetime import date
from math import sqrt, pi, exp

import pandas as pd

import backtest as bt
import vix as vixmod

SQRT_2_OVER_PI = sqrt(2.0 / pi)      # 0.79788 -- Daily_Signal!J9
POP_MIN = 0.65                        # Daily_Signal!H17
RATIO_MIN = 0.95                      # Daily_Signal!H18


def _phi(x):
    """Standard normal PDF."""
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def _Phi(x):
    """Standard normal CDF, Abramowitz-Stegun 7.1.26 via erf."""
    from math import erf
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def straddle_close(dd, expiry, strike):
    """CE close + PE close for one strike on one day."""
    c = dd[(dd["XpryDt"] == expiry) & (dd["StrkPric"] == strike) &
           (dd["OptnTp"] == "CE")]
    p = dd[(dd["XpryDt"] == expiry) & (dd["StrkPric"] == strike) &
           (dd["OptnTp"] == "PE")]
    if c.empty or p.empty:
        return None
    v = float(c["ClsPric"].iloc[0]) + float(p["ClsPric"].iloc[0])
    return v if v > 0 else None


def enrich(t, by_day, vix):
    """Attach the workbook's signal-day quantities to each trade."""
    out = []
    for r in t.to_dict("records"):
        d, K, S = r["signal_day"], r["strike"], r["spot"]
        v = vix.get(d)
        if v is None or v != v:
            continue
        strad = straddle_close(by_day[d], r["expiry"], K)
        if strad is None:
            continue

        dte = max(r["dte"], 1)
        sigma = S * (v / 100.0) / sqrt(365.0) * sqrt(dte)   # Option_Chain!B19
        if sigma <= 0:
            continue

        fair = SQRT_2_OVER_PI * sigma                        # Daily_Signal!J9
        ratio = strad / fair                                 # Daily_Signal!J10

        be_u, be_l = K + strad, K - strad                    # rows 15-16
        pop = _Phi((be_u - S) / sigma) - _Phi((be_l - S) / sigma)   # row 18

        # row 36: expected payoff at expiry under the normal model
        zc, zp = (K - S) / sigma, (K - S) / sigma
        exp_payoff = (sigma * _phi(zc) + (S - K) * _Phi((S - K) / sigma)
                      + sigma * _phi(zp) + (K - S) * _Phi((K - S) / sigma))

        lot = r["lot"]
        gross_ev = (strad - exp_payoff) * lot                # row 37
        fees = bt.costs(strad, exp_payoff, lot, orders=4)    # rows 38-39
        net_ev = gross_ev - fees - bt.SLIPPAGE * 2 * lot     # row 40

        r.update({"vix": v, "sigma": sigma, "strad_sig": strad,
                  "fair": fair, "ratio": ratio, "pop": pop,
                  "c_over_sigma": strad / sigma, "net_ev": net_ev,
                  "g_ev": net_ev > 0, "g_pop": pop >= POP_MIN,
                  "g_rich": ratio >= RATIO_MIN})
        out.append(r)
    return pd.DataFrame(out)


def episodes(sub):
    """Independent bets: the largest set of trades that do NOT overlap in time.
    Greedy scan -- take a trade, skip everything that opens before it expires,
    take the next. Two straddles held over the same week share the same market
    move and are one bet, not two."""
    if len(sub) == 0:
        return 0
    rows = sorted(zip(sub["entry_day"], sub["expiry"]))
    n, busy_until = 0, None
    for entry, exp in rows:
        if busy_until is None or entry > busy_until:
            n += 1
            busy_until = exp
    return n


def block(t, mask, label, base_is, base_oos):
    """Each half judged against ITS OWN baseline -- the two halves can be very
    different environments, and comparing both to the full-sample average makes
    one artificially easy to beat and the other artificially hard."""
    sub = t[mask]
    if len(sub) < 10:
        return f"{label:<34}{len(sub):>5}   too few to judge"
    cut = len(t) // 2
    first = t.iloc[:cut][mask.iloc[:cut]]
    second = t.iloc[cut:][mask.iloc[cut:]]
    s = sub["A_pnl"]
    isa = first["A_pnl"].mean() if len(first) >= 6 else float("nan")
    oos = second["A_pnl"].mean() if len(second) >= 6 else float("nan")

    if isa != isa or oos != oos:
        verdict = "thin half"
    elif isa > base_is and oos > base_oos:
        verdict = "BETTER"
    elif isa < base_is and oos < base_oos:
        verdict = "worse"
    else:
        verdict = "unstable"

    return (f"{label:<34}{len(sub):>5}{episodes(sub):>4}{s.mean():>10,.0f}"
            f"{(s > 0).mean() * 100:>8.1f}{isa:>10,.0f}{oos:>10,.0f}{verdict:>10}")


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python wiki_gate.py 2025-09-01 2026-08-28")
    start, end = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])

    raw = bt.load(start, end)
    by_day = {d: g for d, g in raw.groupby("date")}
    days = sorted(by_day)
    vix = vixmod.load().to_dict()

    rows = [r for r in (bt.run_trade(days[i], days[i + 1], by_day, days)
                        for i in range(len(days) - 1)) if r]
    t = enrich(pd.DataFrame(rows), by_day, vix)
    if t.empty:
        sys.exit("no trades survived enrichment -- is data/vix_history.csv present?")
    t = t.sort_values("entry_day").reset_index(drop=True)
    t["A_pnl"] = t["A_pnl"] / t["lot"]   # size-neutral: points per unit, not rupees

    cut = len(t) // 2
    base_avg = t["A_pnl"].mean()
    base_is = t.iloc[:cut]["A_pnl"].mean()
    base_oos = t.iloc[cut:]["A_pnl"].mean()
    print(f"\n{len(t)} trades with both bhavcopy and VIX  "
          f"({t['entry_day'].min()} -> {t['entry_day'].max()})")
    print(f"baseline (no gate)  full {base_avg:,.0f}   IS {base_is:,.0f}   "
          f"OOS {base_oos:,.0f}   win {(t['A_pnl'] > 0).mean() * 100:.1f}%\n")

    r = t["ratio"]
    print("rich/cheap ratio distribution (Daily_Signal!J10)")
    print(f"  min {r.min():.2f}   p25 {r.quantile(.25):.2f}   "
          f"median {r.median():.2f}   p75 {r.quantile(.75):.2f}   max {r.max():.2f}")
    print(f"  RICH   (>=1.10): {(r >= 1.10).sum():>4}  "
          f"({(r >= 1.10).mean() * 100:.0f}%)")
    print(f"  NEUTRAL(.90-1.10):{((r > 0.90) & (r < 1.10)).sum():>4}  "
          f"({((r > 0.90) & (r < 1.10)).mean() * 100:.0f}%)")
    print(f"  CHEAP  (<=0.90): {(r <= 0.90).sum():>4}  "
          f"({(r <= 0.90).mean() * 100:.0f}%)")
    print(f"\nmedian VIX {t['vix'].median():.2f}   "
          f"median POP {t['pop'].median():.3f}   "
          f"median credit/sigma {t['c_over_sigma'].median():.3f}\n")

    hdr = (f"{'gate':<34}{'n':>5}{'ep':>4}{'avg':>10}{'win%':>8}"
           f"{'IS avg':>10}{'OOS avg':>10}{'verdict':>10}")
    print(hdr)
    print("-" * len(hdr))
    allg = t["g_ev"] & t["g_pop"] & t["g_rich"]
    for label, m in [
        ("no gate (baseline)",            pd.Series(True, index=t.index)),
        ("H16 net EV > 0",                t["g_ev"]),
        ("H17 POP >= 0.65",               t["g_pop"]),
        ("H18 ratio >= 0.95",             t["g_rich"]),
        ("ALL THREE (workbook GO)",       allg),
        ("workbook NO-GO (inverse)",      ~allg),
    ]:
        print(block(t, m, label, base_is, base_oos))

    print("\n\nthreshold sweep on credit/sigma -- the axis all three gates share")
    print(hdr)
    print("-" * len(hdr))
    for thr in (0.70, 0.80, 0.90, 0.9346, 1.00, 1.10, 1.20):
        tag = " <- your POP 0.65" if abs(thr - 0.9346) < 1e-6 else ""
        print(block(t, t["c_over_sigma"] >= thr,
                    f"credit/sigma >= {thr:.4f}{tag}", base_is, base_oos))

    t.to_csv("wiki_gate_trades.csv", index=False)
    print("""
HOW TO READ THIS
  ep        independent episodes, not trades. Entries within a week of each
            other are one bet. Under ~15, treat the row as anecdote.
  verdict   BETTER = beat its OWN half's baseline in both halves.
            worse  = lost in both (equally informative).
            unstable = helped in one, hurt in the other. Noise.

  In the sweep, look for a PLATEAU -- a run of adjacent thresholds that all
  help. Real effects have fuzzy boundaries. A single threshold that helps
  while its neighbours do not was fitted to this data and will not survive.
  Your 0.65 POP cutoff is marked; check whether it sits on a plateau or on
  a lone spike.

per-trade detail written to wiki_gate_trades.csv""")


if __name__ == "__main__":
    main()
