"""Phase-2 research: does ANY defined-risk short-premium shape keep the edge?

Under the risk settings chosen on 27 Sep 2026 (capital Rs 20 lakh, 1% =
Rs 20,000 max loss per trade, +-6% stress for unbounded shapes), naked shorts
never fit. The 1-sigma condor with 2-strike wings -- the one defined-risk
shape tested so far -- averaged about zero over ten years. So: across a grid
of short distances and wing widths, is there a defined-risk shape whose
average survives costs, out of sample?

METHOD
  data     data/calib/nifty_opts_2016.parquet via calibrate.build_rows:
           traded-only closing premiums, parity forward, site sigma, and the
           index close on the expiry day (settlement).
  entry    ONE trade per expiry: the first day with 1-7 days to go (the
           weekly signal's natural entry). Overlapping entries on one expiry
           would share one outcome and fake the sample size.
  shapes   short strikes at 0 (butterfly), 0.5, 1.0, 1.5 sigma from the
           forward; wings 2, 4, 6, 10 strikes beyond each short, and 0.5 /
           1.0 sigma beyond. 24 shapes. Held to expiry.
  scale    every trade restated at TODAY's index (23,100) and lot (65), so a
           2016 trade at 7,900 is not a third of the size of a 2026 one.
  costs    pricing.transaction_costs + pricing.slippage, 4 legs, current
           NSE rates -- charged on every trade.
  size     lots = floor(Rs 20,000 / max loss per lot), at least 0 (a shape
           whose single lot exceeds the cap is not tradable that day).
  honest   picking the best of 24 shapes in hindsight flatters it. So the
           headline is WALK-FORWARD: for each year Y, choose the shape that
           did best on years before Y, trade only that shape in Y.

    python shape_search.py     writes data/calib/shape_search.json
"""
import json, math
from datetime import date

import numpy as np
import pandas as pd

import calibrate as C
import calibrate_wf as W
import pricing as P

SPOT_NOW, LOT_NOW, STEP = 23100.0, 65, 50.0
CAP_RS = 20_000.0
OUT = "data/calib/shape_search.json"
SHORTS = [0.0, 0.5, 1.0, 1.5]
HALF_COSTS = True
WINGS = [("2k", 2), ("4k", 4), ("6k", 6), ("10k", 10), ("0.5s", 0.5), ("1.0s", 1.0)]


def rnd(x):
    return round(x / STEP) * STEP


def price(row, s_sig, wing):
    """(credit_pts, max_loss_pts, pnl_pts) at expiry, or None if a leg is missing."""
    F, sig, ST, ce, pe = row.F, row.sigma, row.ST, row.ce, row.pe
    kc, kp = rnd(F + s_sig * sig), rnd(F - s_sig * sig)
    name, w = wing
    width = w * STEP if name.endswith("k") else max(STEP, rnd(w * sig))
    lc, lp = kc + width, kp - width
    try:
        credit = ce[kc] + pe[kp] - ce[lc] - pe[lp]
    except KeyError:
        return None
    if credit <= 0:
        return None
    pay = min(max(0.0, ST - kc), width) + min(max(0.0, kp - ST), width)
    return credit, width - credit, credit - pay


ENTRY = "first"


def entries(rows):
    """One entry per expiry. 'first' = earliest day with 1-7 DTE, 'last' = the
    session before expiry (usually 1 DTE), 'dteN' = the day with exactly N."""
    r = rows.sort_values("day")
    if ENTRY == "first":
        return r.groupby("expiry", as_index=False).first()
    if ENTRY == "last":
        return r.groupby("expiry", as_index=False).last()
    n = int(ENTRY[3:])
    return r[r.dte == n].groupby("expiry", as_index=False).first()


def trade_table(rows):
    e = entries(rows)
    recs = []
    cost_lot = (P.transaction_costs(0, LOT_NOW, 1, defined_risk=True, costs=P.CURRENT_COSTS)
                + P.slippage(LOT_NOW, 1, defined_risk=True, costs=P.CURRENT_COSTS))
    for _, x in e.iterrows():
        k = SPOT_NOW / x.spot
        for s in SHORTS:
            for wg in WINGS:
                p = price(x, s, wg)
                if p is None:
                    continue
                credit, maxloss, pnl = (v * k for v in p)
                stat = P.transaction_costs(credit, LOT_NOW, 1, defined_risk=True,
                                           costs=P.CURRENT_COSTS) - P.transaction_costs(
                                               0, LOT_NOW, 1, defined_risk=True, costs=P.CURRENT_COSTS)
                # Held to expiry: no closing order, no closing slippage. pricing.py's
                # slippage is round trip (1 pt/leg/side), so charge half of it;
                # brokerage likewise only on the opening orders.
                c_rs = (cost_lot + stat) / 2 if HALF_COSTS else cost_lot + stat
                ml_rs = maxloss * LOT_NOW + c_rs
                lots = int(CAP_RS // ml_rs) if ml_rs > 0 else 0
                recs.append({"expiry": x.expiry, "year": x.expiry.year, "dte": x.dte,
                             "shape": f"short {s:.1f}σ / wing {wg[0]}",
                             "credit_pts": credit, "maxloss_rs_lot": ml_rs,
                             "pnl_rs_lot": pnl * LOT_NOW - c_rs, "lots": lots,
                             "gross_rs_lot": pnl * LOT_NOW, "cost_rs_lot": c_rs,
                             "pnl_rs": (pnl * LOT_NOW - c_rs) * lots})
    return pd.DataFrame(recs)


def summary(t):
    out = []
    for sh, g in t.groupby("shape"):
        tr = g[g.lots > 0]
        if len(tr) < 50:
            continue
        yrs = tr.groupby("year").pnl_rs.sum()
        out.append({"shape": sh, "trades": len(tr), "skipped_over_cap": int((g.lots == 0).sum()),
                    "avg_lots": round(tr.lots.mean(), 1),
                    "mean_rs": round(tr.pnl_rs.mean(), 0), "p_loss": round((tr.pnl_rs < 0).mean(), 3),
                    "p5_rs": round(np.percentile(tr.pnl_rs, 5), 0), "worst_rs": round(tr.pnl_rs.min(), 0),
                    "mean_per_lot_rs": round(tr.pnl_rs_lot.mean(), 0),
                    "t_stat": round(tr.pnl_rs.mean() / (tr.pnl_rs.std() / math.sqrt(len(tr))), 2),
                    "years_positive": f"{int((yrs > 0).sum())}/{len(yrs)}",
                    "total_rs": round(tr.pnl_rs.sum(), 0)})
    return sorted(out, key=lambda r: -r["mean_rs"])


def walk_forward(t):
    """Each year: trade only the shape that was best (mean Rs/trade) before it."""
    res = []
    for y in range(2019, 2027):
        past = t[(t.year < y) & (t.lots > 0)]
        now = t[(t.year == y) & (t.lots > 0)]
        if past.empty or now.empty:
            continue
        m = past.groupby("shape").pnl_rs.agg(["mean", "count"])
        m = m[m["count"] >= 20]
        best = m["mean"].idxmax()
        g = now[now["shape"] == best]
        res.append({"year": y, "picked": best, "trades": len(g),
                    "mean_rs": round(g.pnl_rs.mean(), 0), "sum_rs": round(g.pnl_rs.sum(), 0),
                    "p_loss": round((g.pnl_rs < 0).mean(), 3)})
    return res


def cost_sensitivity(t):
    """Per-lot mean at 1 lot under three cost cases. Held to expiry there is no
    closing fill, so the round-trip slippage in pricing.py (1 pt per leg per
    side) double-counts the exit; 'entry only' charges half of it."""
    out = []
    for sh, g in t.groupby("shape"):
        n = len(g)
        gross = g.gross_rs_lot
        full = g.pnl_rs_lot
        half = gross - g.cost_rs_lot / 2
        se = gross.std() / math.sqrt(n)
        out.append({"shape": sh, "n": n, "gross": round(gross.mean(), 0),
                    "entry_only_costs": round(half.mean(), 0), "full_costs": round(full.mean(), 0),
                    "gross_t": round(gross.mean() / se, 2) if se else None,
                    "avg_cost": round(g.cost_rs_lot.mean(), 0)})
    return sorted(out, key=lambda r: -r["gross"])


def main():
    global ENTRY, OUT
    import sys
    if "--entry" in sys.argv:
        ENTRY = sys.argv[sys.argv.index("--entry") + 1]
        OUT = f"data/calib/shape_search_{ENTRY}.json"
    print(f"ENTRY = {ENTRY}, costs = {'entry-only' if HALF_COSTS else 'round trip'}")
    d, vix = W.load()
    rows = C.build_rows(d, vix, json.load(open(W.GAP_2Y)))
    t = trade_table(rows)
    s = summary(t)
    wf = walk_forward(t)
    tot = sum(r["sum_rs"] for r in wf); n = sum(r["trades"] for r in wf)
    print(f"{t.expiry.nunique()} expiries, {len(s)} shapes with >=50 tradable entries\n")
    print(f"{'shape':28s}{'trades':>7}{'lots':>6}{'mean Rs':>9}{'P(loss)':>8}{'1-in-20':>10}{'worst':>10}{'t':>6}{'yrs+':>7}")
    for r in s:
        print(f"{r['shape']:28s}{r['trades']:7d}{r['avg_lots']:6.1f}{r['mean_rs']:9,.0f}{r['p_loss']:8.0%}"
              f"{r['p5_rs']:10,.0f}{r['worst_rs']:10,.0f}{r['t_stat']:6.2f}{r['years_positive']:>7}")
    print("\nWALK-FORWARD (shape chosen only on earlier years):")
    for r in wf:
        print(f"  {r['year']}  {r['picked']:26s} trades {r['trades']:3d}  mean {r['mean_rs']:8,.0f}  "
              f"year {r['sum_rs']:10,.0f}  P(loss) {r['p_loss']:.0%}")
    print(f"  TOTAL 2019-2026: {n} trades, Rs {tot:,.0f}, mean Rs {tot / max(n, 1):,.0f}/trade")
    cs = cost_sensitivity(t)
    print("\nPER LOT, ONE LOT, BEFORE SIZING -- how much is edge and how much is cost:")
    print(f"{'shape':28s}{'n':>5}{'gross':>8}{'t':>6}{'entry-cost':>11}{'full-cost':>10}{'cost':>6}")
    for r in cs:
        print(f"{r['shape']:28s}{r['n']:5d}{r['gross']:8,.0f}{r['gross_t']:6.2f}{r['entry_only_costs']:11,.0f}{r['full_costs']:10,.0f}{r['avg_cost']:6,.0f}")
    json.dump({"settings": {"spot": SPOT_NOW, "lot": LOT_NOW, "cap_rs": CAP_RS},
               "cost_sensitivity": cs,
               "shapes": s, "walk_forward": wf,
               "walk_forward_total": {"trades": n, "sum_rs": tot}},
              open(OUT, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
