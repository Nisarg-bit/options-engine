"""Does any daily signal beat selling the straddle unconditionally?

backtest.py established the baseline: sell the ATM straddle every session,
hold to expiry, ~33 points of edge per trade. This asks whether filtering
those entries on a signal you can compute from bhavcopy makes it better.

Every signal here is computed from data available BEFORE entry. Percentile
ranks are expanding (each day is ranked only against days that preceded it),
so nothing peeks at the future.

Read the OOS column, not the total. See the warning the script prints.
"""

import sys
from datetime import date

import pandas as pd

import backtest as bt

MIN_HISTORY = 30       # sessions of history before a percentile rank is trusted


# ---------------------------------------------------------------- signals

def day_signals(dd, d):
    """Everything knowable about session d, from session d's own bhavcopy."""
    up = dd["UndrlygPric"].dropna()
    if up.empty:
        return None
    spot = float(up.mode().iloc[0])

    exps = sorted(e for e in dd["XpryDt"].unique() if (e - d).days >= 1)
    if not exps:
        return None
    front = exps[0]
    ch = dd[dd["XpryDt"] == front]
    ce = ch[ch["OptnTp"] == "CE"]
    pe = ch[ch["OptnTp"] == "PE"]
    if ce.empty or pe.empty:
        return None

    ce_oi, pe_oi = ce["OpnIntrst"].sum(), pe["OpnIntrst"].sum()
    ce_vol, pe_vol = ce["TtlTradgVol"].sum(), pe["TtlTradgVol"].sum()

    strikes = sorted(ch["StrkPric"].unique())
    atm = min(strikes, key=lambda s: abs(s - spot))
    c = ch[(ch["StrkPric"] == atm) & (ch["OptnTp"] == "CE")]
    p = ch[(ch["StrkPric"] == atm) & (ch["OptnTp"] == "PE")]
    strad = (float(c["ClsPric"].iloc[0]) + float(p["ClsPric"].iloc[0])
             if len(c) and len(p) else float("nan"))

    return {
        "date": d,
        "spot": spot,
        "dte": (front - d).days,
        "pcr_oi": pe_oi / max(ce_oi, 1),
        "pcr_vol": pe_vol / max(ce_vol, 1),
        "strad_pct": strad / spot * 100 if strad == strad else float("nan"),
    }


def expanding_pct(s):
    """Percentile of each value against ONLY the values that came before it."""
    out, hist = [], []
    for v in s:
        if v != v or len(hist) < MIN_HISTORY:
            out.append(float("nan"))
        else:
            out.append(sum(1 for h in hist if h <= v) / len(hist))
        if v == v:
            hist.append(v)
    return pd.Series(out, index=s.index)


def build_signals(by_day, days):
    rows = [r for r in (day_signals(by_day[d], d) for d in days) if r]
    sg = pd.DataFrame(rows).set_index("date").sort_index()
    sg["ret_1d"] = sg["spot"].pct_change() * 100
    sg["ret_5d"] = sg["spot"].pct_change(5) * 100
    for col in ("pcr_oi", "pcr_vol", "strad_pct"):
        sg[col + "_r"] = expanding_pct(sg[col])
    return sg


# ---------------------------------------------------------------- filters

# name -> predicate on one merged row. All read signals from the SIGNAL day.
FILTERS = {
    "baseline (all trades)":      lambda r: True,
    "PCR-OI high (>60th pct)":    lambda r: r["pcr_oi_r"] > 0.60,
    "PCR-OI low (<40th pct)":     lambda r: r["pcr_oi_r"] < 0.40,
    "PCR-OI middle (40-60)":      lambda r: 0.40 <= r["pcr_oi_r"] <= 0.60,
    "PCR-vol high (>60th pct)":   lambda r: r["pcr_vol_r"] > 0.60,
    "PCR-vol low (<40th pct)":    lambda r: r["pcr_vol_r"] < 0.40,
    "premium rich (>60th pct)":   lambda r: r["strad_pct_r"] > 0.60,
    "premium cheap (<40th pct)":  lambda r: r["strad_pct_r"] < 0.40,
    "quiet week (|5d ret|<1%)":   lambda r: abs(r["ret_5d"]) < 1.0,
    "trending (|5d ret|>2%)":     lambda r: abs(r["ret_5d"]) > 2.0,
    "quiet day (|1d ret|<0.4%)":  lambda r: abs(r["ret_1d"]) < 0.4,
    "near expiry (dte<=3)":       lambda r: r["dte"] <= 3,
    "far expiry (dte>=4)":        lambda r: r["dte"] >= 4,
}


def stats(sub):
    if len(sub) == 0:
        return None
    s = sub["A_pnl"]
    return {"n": len(s), "avg": s.mean(), "win": (s > 0).mean() * 100,
            "total": s.sum(), "med": s.median()}


def episodes(sub):
    """Independent bets: the largest set of trades that do NOT overlap in time.
    Greedy scan -- take a trade, skip everything that opens before it expires,
    take the next. Two straddles held over the same week share the same market
    move and are one bet, not two. This is the sample size that governs whether
    a difference in averages means anything."""
    if len(sub) == 0:
        return 0
    rows = sorted(zip(sub["entry_day"], sub["expiry"]))
    n, busy_until = 0, None
    for entry, exp in rows:
        if busy_until is None or entry > busy_until:
            n += 1
            busy_until = exp
    return n


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python study.py 2025-09-01 2026-08-28")
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])

    raw = bt.load(start, end)
    by_day = {d: g for d, g in raw.groupby("date")}
    days = sorted(by_day)

    sg = build_signals(by_day, days)

    rows = []
    for i in range(len(days) - 1):
        r = bt.run_trade(days[i], days[i + 1], by_day, days)
        if r:
            rows.append(r)
    t = pd.DataFrame(rows)
    t["A_pnl"] = t["A_pnl"] / t["lot"]   # size-neutral: points per unit, not rupees

    # attach each trade's SIGNAL-day features
    t = t.join(sg, on="signal_day", rsuffix="_sig")
    t = t.dropna(subset=["pcr_oi_r", "strad_pct_r", "ret_5d"])
    if t.empty:
        sys.exit("no trades survived the signal warm-up period")

    # split by entry date into two equal halves for out-of-sample checking
    t = t.sort_values("entry_day").reset_index(drop=True)
    cut = len(t) // 2
    first, second = t.iloc[:cut], t.iloc[cut:]

    print(f"\n{len(t)} trades usable after {MIN_HISTORY}-session signal warm-up")
    print(f"in-sample  {first['entry_day'].min()} -> {first['entry_day'].max()}"
          f"   ({len(first)} trades)")
    print(f"out-sample {second['entry_day'].min()} -> {second['entry_day'].max()}"
          f"   ({len(second)} trades)\n")

    base = stats(t)
    base_is, base_oos = first["A_pnl"].mean(), second["A_pnl"].mean()

    # Each half is judged against ITS OWN baseline. The two halves can be very
    # different environments; comparing both to the full-sample average makes
    # one half artificially easy to beat and the other artificially hard.
    print(f"baseline  full {base['avg']:>8,.0f}   "
          f"IS {base_is:>8,.0f}   OOS {base_oos:>8,.0f}   "
          f"<- each half judged against its own\n")

    hdr = (f"{'filter':<28}{'n':>5}{'ep':>4}{'avg':>9}{'med':>9}{'win%':>7}"
           f"{'IS avg':>9}{'OOS avg':>9}{'verdict':>10}")
    print(hdr)
    print("-" * len(hdr))

    for name, fn in FILTERS.items():
        mask = t.apply(fn, axis=1)
        sub = t[mask]
        f_is, f_oos = first[first.apply(fn, axis=1)], second[second.apply(fn, axis=1)]
        a = stats(sub)
        if a is None or a["n"] < 15:
            print(f"{name:<28}{(a['n'] if a else 0):>5}   too few trades")
            continue
        is_avg = f_is["A_pnl"].mean() if len(f_is) >= 8 else float("nan")
        oos_avg = f_oos["A_pnl"].mean() if len(f_oos) >= 8 else float("nan")

        if is_avg != is_avg or oos_avg != oos_avg:
            verdict = "thin half"
        elif is_avg > base_is and oos_avg > base_oos:
            verdict = "BETTER"
        elif is_avg < base_is and oos_avg < base_oos:
            verdict = "worse"
        else:
            verdict = "unstable"

        print(f"{name:<28}{a['n']:>5}{episodes(sub):>4}{a['avg']:>9,.0f}"
              f"{a['med']:>9,.0f}{a['win']:>7.1f}{is_avg:>9,.0f}"
              f"{oos_avg:>9,.0f}{verdict:>10}")
    print("""
HOW TO READ THIS
  ep         independent EPISODES, not trades. Entries within a week of each
             other are one bet. This is the real sample size. Under ~15,
             nothing here is evidence of anything.
  IS / OOS   the filter's average in each half, judged against THAT half's
             own baseline (printed above), not the full-sample average.
  verdict    BETTER   beat its own baseline in both halves
             worse    lost to it in both halves -- also a finding
             unstable helped in one half, hurt in the other -- noise

13 filters against ~211 heavily overlapping trades: two or three will look
good by chance alone. BETTER is a shortlist entry, not a finding. Check 'ep'
before believing any row, and prefer a filter with a plausible mechanism over
one with a bigger number.""")

    t.to_csv("study_trades.csv", index=False)
    print("\nper-trade detail with signals written to study_trades.csv")


if __name__ == "__main__":
    main()
