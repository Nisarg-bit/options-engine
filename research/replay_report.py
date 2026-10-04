"""Month-by-month / year-by-year P&L of replay.py, and every losing month taken apart."""
import json, math
import numpy as np
import pandas as pd

R = "data/replay"
V = ["hold", "target80", "stop2x", "combined"]


def series():
    d = pd.read_parquet("data/calib/nifty_opts_2016.parquet", columns=["TradDt", "UndrlygPric"])
    s = d.groupby("TradDt").UndrlygPric.median()
    s.index = pd.to_datetime(s.index)
    v = pd.read_csv("data/calib/vix_history_2016.csv", parse_dates=["date"]).set_index("date").vix
    return s.sort_index(), v.sort_index()


def main():
    spot, vix = series()
    out = {}
    print("=== YEARLY P&L (Rs, today's scale) ===")
    yr = {}
    for v in V:
        t = pd.read_csv(f"{R}/trades_{v}.csv", parse_dates=["entry", "exit", "expiry"])
        yr[v] = t.groupby(t.exit.dt.year).pnl.sum().round(0)
    Y = pd.DataFrame(yr).fillna(0).astype(int)
    Y.loc["TOTAL"] = Y.sum()
    print(Y.to_string())

    t = pd.read_csv(f"{R}/trades_hold.csv", parse_dates=["entry", "exit", "expiry"])
    t["month"] = t.exit.dt.to_period("M")
    M = t.pivot_table(index=t.exit.dt.year, columns=t.exit.dt.month, values="pnl", aggfunc="sum").round(0)
    M.columns = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][:0] + \
        [pd.Timestamp(2000, c, 1).strftime("%b") for c in M.columns]
    print("\n=== MONTHLY P&L, HOLD TO EXPIRY (blank = no trade) ===")
    print(M.fillna("").to_string())

    # every losing month: market context + the trades
    mm = t.groupby("month").pnl.sum()
    losers = mm[mm < 0].sort_values()
    print(f"\n=== LOSING MONTHS (hold): {len(losers)} of {len(mm)} months with trades ===")
    rows = []
    for per, total in losers.items():
        a, b = per.start_time, per.end_time
        sp = spot[(spot.index >= a) & (spot.index <= b)]
        vx = vix[(vix.index >= a) & (vix.index <= b)]
        r = np.log(sp).diff().dropna()
        tr = t[t.month == per]
        rows.append({"month": str(per), "pnl": round(total), "trades": len(tr),
                     "nifty_ret_%": round((sp.iloc[-1] / sp.iloc[0] - 1) * 100, 1),
                     "worst_day_%": round(r.min() * 100, 1), "best_day_%": round(r.max() * 100, 1),
                     "vix_start": round(vx.iloc[0], 1), "vix_max": round(vx.max(), 1), "vix_end": round(vx.iloc[-1], 1)})
    L = pd.DataFrame(rows)
    print(L.to_string(index=False))

    # each losing TRADE: how the market beat it
    print("\n=== LOSING TRADES (hold): what the index did between entry and expiry ===")
    lt = []
    for _, x in t[t.pnl < 0].iterrows():
        path = spot[(spot.index > x.entry) & (spot.index <= x.expiry)]
        sig = x.spot * x.vix / 100 * math.sqrt(max(x.dte, 1) / 365)
        move = (spot.get(x.expiry, path.iloc[-1] if len(path) else x.spot) - x.spot)
        gaps = np.log(path / path.shift(1).fillna(x.spot))
        v0, v1 = x.vix, vix[vix.index <= x.expiry].iloc[-1]
        prev5 = vix[vix.index <= x.entry].tail(6)
        lt.append({"entry": x.entry.date(), "expiry": x.expiry.date(), "dte": x.dte, "structure": x.structure,
                   "pnl": round(x.pnl), "move_pts": round(move), "move_sigma": round(move / sig, 2),
                   "biggest_day_%": round(gaps.abs().max() * 100, 2) if len(gaps) else None,
                   "vix_entry": round(v0, 1), "vix_at_expiry": round(v1, 1),
                   "vix_5d_change_before_entry": round(prev5.iloc[-1] - prev5.iloc[0], 1) if len(prev5) > 1 else None,
                   "cs": round(x.cs, 2)})
    LT = pd.DataFrame(lt).sort_values("pnl")
    print(LT.to_string(index=False))
    LT.to_csv(f"{R}/losing_trades.csv", index=False)
    L.to_csv(f"{R}/losing_months.csv", index=False)
    Y.to_csv(f"{R}/yearly.csv")
    M.to_csv(f"{R}/monthly_matrix_hold.csv")


if __name__ == "__main__":
    main()
