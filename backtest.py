"""ATM short-straddle backtest on NSE bhavcopy (daily) data.

The question this answers is NOT "is the strategy good".
It is: "does intraday (minute) data change the answer?"

Three variants of the same trade:

  A  hold to expiry              -- no intraday timing at all
  B  stop at 2x credit, on HIGH  -- pessimistic: assumes the worst intraday tick
                                    fires the stop, even if it lasted one second
  C  stop at 2x credit, on CLOSE -- optimistic: you only check once a day at 15:30

B and C bracket the truth. The real answer (what you'd get with minute data)
lies somewhere between them.

  B ~= C  -> daily data is enough. Don't buy minute data yet.
  B << C  -> your P&L is decided by intraday exit timing. Buy the minute data.
"""

import sys
from datetime import date, timedelta

import pandas as pd

import bhavcopy

# ---------------------------------------------------------------- parameters

VERSION    = "v2 -- intrinsic settlement"

SYMBOL     = "NIFTY"
SLIPPAGE   = 1.0     # points, per leg, each way
STOP_MULT  = 2.0     # exit when combined premium reaches 2x the credit
MIN_DTE    = 1       # calendar days to expiry on entry day (1 = never enter on expiry day)
MAX_DTE    = 12      # ignore expiries further out than this
BROKERAGE  = 20.0    # rupees per executed order

# Zerodha F&O option cost model (rates as of 2026)
R_STT   = 0.00100    # 0.10%   on sell-side premium turnover
R_EXCH  = 0.000495   # 0.0495% on total premium turnover
R_SEBI  = 0.000001   # 0.0001% on total premium turnover
R_STAMP = 0.00003    # 0.003%  on buy-side turnover
R_GST   = 0.18       # on brokerage + exchange + sebi


def costs(sell_premium, buy_premium, lot, orders):
    """Round-trip charges in rupees for one straddle (both legs)."""
    sell_t = sell_premium * lot
    buy_t  = buy_premium * lot
    turn   = sell_t + buy_t
    brok   = BROKERAGE * orders
    stt    = R_STT   * sell_t
    exch   = R_EXCH  * turn
    sebi   = R_SEBI  * turn
    stamp  = R_STAMP * buy_t
    gst    = R_GST   * (brok + exch + sebi)
    return brok + stt + exch + sebi + stamp + gst


# ---------------------------------------------------------------- data load

def load(start, end):
    frames = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            df = bhavcopy.options(d, SYMBOL)
            if df is not None and not df.empty:
                df = df.copy()
                df["date"] = d
                frames.append(df)
        d += timedelta(days=1)
    if not frames:
        sys.exit("no bhavcopy data in that range -- run bhavcopy.py first")
    out = pd.concat(frames, ignore_index=True)
    print(f"loaded {len(out):,} {SYMBOL} option rows "
          f"across {out['date'].nunique()} sessions")
    return out


def leg(day_df, expiry, strike, typ):
    """One option row, or None."""
    m = day_df[(day_df["XpryDt"] == expiry) &
               (day_df["StrkPric"] == strike) &
               (day_df["OptnTp"] == typ)]
    return None if m.empty else m.iloc[0]


# ---------------------------------------------------------------- one trade

def run_trade(signal_day, entry_day, by_day, days):
    """Build one straddle. Returns a dict of results, or None if not tradable."""
    sig = by_day[signal_day]
    ent = by_day[entry_day]

    spot = sig["UndrlygPric"].dropna()
    if spot.empty:
        return None
    spot = float(spot.mode().iloc[0])

    # nearest usable expiry as seen on the entry day
    exps = sorted(e for e in ent["XpryDt"].unique()
                  if MIN_DTE <= (e - entry_day).days <= MAX_DTE)
    if not exps:
        return None
    expiry = exps[0]

    chain = ent[ent["XpryDt"] == expiry]
    strikes = sorted(chain["StrkPric"].unique())
    if not strikes:
        return None
    atm = min(strikes, key=lambda s: abs(s - spot))

    ce, pe = leg(ent, expiry, atm, "CE"), leg(ent, expiry, atm, "PE")
    if ce is None or pe is None:
        return None
    if ce["OpnPric"] <= 0 or pe["OpnPric"] <= 0:
        return None            # untraded at the open -- can't enter

    lot = int(ce["NewBrdLotQty"])
    credit = float(ce["OpnPric"] + pe["OpnPric"]) - 2 * SLIPPAGE
    if credit <= 0:
        return None
    stop_at = STOP_MULT * credit

    # the path: every session from entry day through expiry day, inclusive
    path = [d for d in days if entry_day <= d <= expiry]
    if not path or path[-1] != expiry:
        return None            # expiry session missing from the data

    exit_hi = exit_cl = None   # (day, price) for variants B and C

    for d in path:
        dd = by_day[d]
        c, p = leg(dd, expiry, atm, "CE"), leg(dd, expiry, atm, "PE")
        if c is None or p is None:
            continue
        if exit_hi is None and float(c["HghPric"] + p["HghPric"]) >= stop_at:
            exit_hi = (d, stop_at)
        if exit_cl is None and float(c["ClsPric"] + p["ClsPric"]) >= stop_at:
            exit_cl = (d, float(c["ClsPric"] + p["ClsPric"]))
        if exit_hi is not None and exit_cl is not None:
            break

    # NIFTY options are European and cash-settled: the expiry payoff is defined,
    # not quoted. Do NOT use SttlmPric -- in the UDiFF bhavcopy that column holds
    # the INDEX final settlement price, stamped identically onto every contract.
    xday = by_day[expiry]
    xrows = xday[xday["XpryDt"] == expiry]
    if xrows.empty:
        return None
    final_spot = float(xrows["UndrlygPric"].mode().iloc[0])
    settle = max(0.0, final_spot - atm) + max(0.0, atm - final_spot)
    if settle > 3000:
        return None

    def pnl(exit_raw, squared_off):
        # squaring off costs slippage and two extra orders; expiry settlement doesn't
        exit_cost = exit_raw + (2 * SLIPPAGE if squared_off else 0.0)
        gross = (credit - exit_cost) * lot
        fees = costs(credit, exit_cost, lot, orders=4 if squared_off else 2)
        return gross - fees

    return {
        "signal_day": signal_day,
        "entry_day": entry_day,
        "expiry": expiry,
        "dte": (expiry - entry_day).days,
        "spot": spot,
        "strike": atm,
        "lot": lot,
        "credit": round(credit, 2),
        "stop_at": round(stop_at, 2),
        "settle": round(settle, 2),
        "final_spot": round(final_spot, 2),
        "move": round(abs(final_spot - atm), 2),

        "A_pnl": round(pnl(settle, False), 2),

        "B_stopped": exit_hi is not None,
        "B_exit_day": exit_hi[0] if exit_hi else expiry,
        "B_pnl": round(pnl(exit_hi[1], True) if exit_hi else pnl(settle, False), 2),

        "C_stopped": exit_cl is not None,
        "C_exit_day": exit_cl[0] if exit_cl else expiry,
        "C_pnl": round(pnl(exit_cl[1], True) if exit_cl else pnl(settle, False), 2),
    }


# ---------------------------------------------------------------- reporting

def summarise(trades, key):
    s = trades[f"{key}_pnl"]
    cum = s.cumsum()
    dd = (cum - cum.cummax()).min()
    stopped = int(trades[f"{key}_stopped"].sum()) if f"{key}_stopped" in trades else 0
    return {
        "total": s.sum(),
        "avg": s.mean(),
        "median": s.median(),
        "win_rate": (s > 0).mean() * 100,
        "best": s.max(),
        "worst": s.min(),
        "max_dd": dd,
        "stopped": stopped,
    }


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python backtest.py 2025-09-01 2026-08-28")
    start = date.fromisoformat(sys.argv[1])
    end   = date.fromisoformat(sys.argv[2])

    print(f"backtest.py {VERSION}\n")
    raw = load(start, end)
    by_day = {d: g for d, g in raw.groupby("date")}
    days = sorted(by_day)

    rows = []
    for i in range(len(days) - 1):
        r = run_trade(days[i], days[i + 1], by_day, days)
        if r:
            rows.append(r)

    if not rows:
        sys.exit("no tradable straddles found")

    t = pd.DataFrame(rows)
    t.to_csv("backtest_trades.csv", index=False)

    n = len(t)
    print(f"\n{n} trades   {t['entry_day'].min()} -> {t['entry_day'].max()}")
    print(f"avg credit {t['credit'].mean():.1f} pts   "
          f"avg settle {t['settle'].mean():.1f} pts   "
          f"avg |move| {t['move'].mean():.1f} pts   "
          f"avg DTE {t['dte'].mean():.1f} days   lot {t['lot'].mode().iloc[0]}\n")

    hdr = f"{'':<26}{'A hold':>14}{'B stop/high':>14}{'C stop/close':>14}"
    print(hdr)
    print("-" * len(hdr))
    a, b, c = (summarise(t, k) for k in "ABC")
    fmt = [
        ("total P&L",        "total",    "{:>14,.0f}"),
        ("avg per trade",    "avg",      "{:>14,.0f}"),
        ("median per trade", "median",   "{:>14,.0f}"),
        ("win rate %",       "win_rate", "{:>14.1f}"),
        ("best trade",       "best",     "{:>14,.0f}"),
        ("worst trade",      "worst",    "{:>14,.0f}"),
        ("max drawdown",     "max_dd",   "{:>14,.0f}"),
        ("times stopped",    "stopped",  "{:>14,.0f}"),
    ]
    for label, k, f in fmt:
        print(f"{label:<26}" + f.format(a[k]) + f.format(b[k]) + f.format(c[k]))

    print()
    span = abs(b["total"] - c["total"])
    base = max(abs(a["total"]), 1.0)
    print(f"B-vs-C spread: Rs {span:,.0f}  "
          f"({span / base * 100:.1f}% of the hold-to-expiry total)")
    print(f"A-vs-B spread: Rs {abs(a['total'] - b['total']):,.0f}")
    print(f"A-vs-C spread: Rs {abs(a['total'] - c['total']):,.0f}")
    print()
    if span / base < 0.15:
        print("VERDICT: the pessimistic and optimistic stop models land in the")
        print("same place. Intraday timing is noise here -- daily bhavcopy data")
        print("is enough to develop and rank this strategy. Do not buy minute data yet.")
    else:
        print("VERDICT: the two stop models disagree by more than the strategy's")
        print("own edge. Where inside the day you exit decides the P&L, and daily")
        print("data cannot tell you. Minute data is required before trusting any")
        print("backtest of a stop-based version of this strategy.")
    print("\nper-trade detail written to backtest_trades.csv")


if __name__ == "__main__":
    main()
