"""Ten-year replay of the LIVE daily signal rule under the agreed risk rules.

Agreed with the user, 27 Sep 2026:
  rule       daily_signal.py as it runs live: strategies.build_all on the day's
             chain, nearest expiry 1-12 days out, gate credit/sigma >= 0.9346
             (POP >= 65% on the ATM straddle), rank by net EV, trade only if > 0.
  risk       capital Rs 20 lakh; a trade's worst case <= Rs 20,000 (1%);
             everything open <= Rs 60,000 (3%); naked shapes never fit the
             +-6% stress test, so when the top pick is naked the best
             DEFINED-RISK structure with positive net EV is taken instead;
             5% drawdown (Rs 1 lakh) -> no new trades for 10 trading days.
  exits      four versions: hold to expiry / 80% decay target / stop at 2x
             credit / combined (target, else stop, else hold).
  entry      at that day's CLOSING prices (no 09:00 option prices exist in
             the old data). Traded strikes only: a structure whose leg did
             not trade that day is not built -- strategies.Chain would price
             a missing strike at 0, which would hand a condor a free wing.
  scale      every trade restated at TODAY's NIFTY (23,100) and 65 lot: the
             lot is scaled by 23,100 / spot and the 200-point wing by the
             inverse (rounded to 50, min 50), so 2016 and 2026 trades are the
             same size in rupees and the same shape relative to the index.
  costs      pricing.py CURRENT_COSTS. Closed early: full round trip. Held to
             expiry: half (no closing order).
  marks      daily closes only (no intraday data before Aug 2026): a target
             or stop fires at the first CLOSE through it, not at the touch.

    python replay.py            writes data/replay/*.csv and summary.json
"""
import bisect, json, math, os, sys
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd

import pricing as P
import strategies as S

OPTS = "data/calib/nifty_opts_2016.parquet"
VIX = "data/calib/vix_history_2016.csv"
OUT = "data/replay"
SPOT_NOW, LOT_NOW, STEP = 23100.0, 65, 50.0
CAPITAL, CAP_TRADE, CAP_BOOK, BREAKER, PAUSE = 2_000_000, 20_000, 60_000, 100_000, 10
CS_GATE = 0.9346
MIN_DTE, MAX_DTE = 1, 12
COSTS = P.CURRENT_COSTS
VARIANTS = ["hold", "target80", "stop2x", "combined"]


# ------------------------------------------------------------------ data

def load():
    d = pd.read_parquet(OPTS)
    d["TradDt"] = pd.to_datetime(d.TradDt).dt.date
    d["XpryDt"] = pd.to_datetime(d.XpryDt).dt.date
    spot = d.groupby("TradDt").UndrlygPric.median().to_dict()
    o = d[(d.FinInstrmTp == "IDO") & (d.TtlTradgVol > 0) & (d.ClsPric > 0)]
    chains = defaultdict(lambda: {"CE": {}, "PE": {}})
    for (day, exp, ot), g in o.groupby(["TradDt", "XpryDt", "OptnTp"]):
        chains[(day, exp)][ot] = dict(zip(g.StrkPric.astype(float), g.ClsPric.astype(float)))
    v = pd.read_csv(VIX)
    v["date"] = pd.to_datetime(v.date).dt.date
    vix = dict(zip(v.date, v.vix))
    tdays = sorted(spot)
    exps_by_day = defaultdict(set)
    for (day, exp) in chains:
        exps_by_day[day].add(exp)
    return chains, spot, vix, tdays, exps_by_day


# -------------------------------------------------------------- signal

def legs(s):
    sh = [(s.ce_strike, "CE"), (s.pe_strike, "PE")]
    lg = [(s.long_ce_strike, "CE"), (s.long_pe_strike, "PE")] if s.defined_risk else []
    return sh, lg


MAX_CREDIT_OF_WING = 0.90


def valid(s, ch):
    """Every leg traded that day, and the prices are not an arbitrage.

    A vertical spread can never be worth more than its width. In the old
    closing-price data an illiquid wing can carry a stale close, which made a
    condor collect MORE than its wing width -- a riskless trade that does not
    exist. The first replay run found them: 19 May 2019 and March 2020 condors
    with a max loss of 0, sized to 11-16 lots, made most of a fictitious
    Rs 15 lakh. Each side's credit must be positive and below 90% of the wing.
    """
    sh, lg = legs(s)
    if not all(k in ch[t] for k, t in sh + lg) or s.credit_pts <= 0:
        return False
    if s.defined_risk:
        w = s.wing_width
        c_side = ch["CE"][s.ce_strike] - ch["CE"][s.long_ce_strike]
        p_side = ch["PE"][s.pe_strike] - ch["PE"][s.long_pe_strike]
        if not (0 < c_side < w and 0 < p_side < w):
            return False
        if s.credit_pts > MAX_CREDIT_OF_WING * w:
            return False
    return True


def signal(day, chains, spot, vix, exps_by_day):
    """The live rule for one day. Returns (pick_dict or None, reason)."""
    if day not in vix or day not in spot:
        return None, "no vix/spot"
    cand = sorted(e for e in exps_by_day.get(day, ()) if MIN_DTE <= (e - day).days <= MAX_DTE)
    if not cand:
        return None, "no expiry 1-12d"
    exp = cand[0]
    ch = chains[(day, exp)]
    ks = sorted(set(ch["CE"]) & set(ch["PE"]))
    if len(ks) < 15:
        return None, "thin chain"
    sp = spot[day]
    k = SPOT_NOW / sp
    lot = LOT_NOW * k
    wing = max(STEP, P.mround(200.0 / k, STEP))
    rows = [{"strike": x, "ce": ch["CE"][x], "pe": ch["PE"][x]} for x in ks]
    dte = (exp - day).days
    st = S.build_all(rows, sp, vix[day] / 100.0, dte, step=STEP, lot_size=lot, lots=1,
                     wing_width=wing, sigma_mult=1.5, margin_per_lot=120000.0, costs=COSTS)
    stv = P.Volatility(sp, vix[day] / 100.0, dte)
    strad = st["short_straddle"]
    if not valid(strad, ch):
        return None, "no ATM straddle"
    cs = strad.credit_pts / stv.expiry_stdev
    if cs < CS_GATE:
        return None, "gate: premium too thin"
    ok = {n: s for n, s in st.items() if valid(s, ch)}
    if not ok:
        return None, "no valid structure"
    top = max(ok.values(), key=lambda s: s.net_ev_rs)
    if top.net_ev_rs <= 0:
        return None, "no positive net EV"
    chosen, swapped = top, False
    if not top.defined_risk:
        dr = [s for s in ok.values() if s.defined_risk and s.net_ev_rs > 0]
        if not dr:
            return None, "top pick naked, no defined-risk alternative with +EV"
        chosen, swapped = max(dr, key=lambda s: s.net_ev_rs), True
    cost_rt = chosen.fees_rs + chosen.slippage_rs                     # per lot, round trip
    worst = chosen.max_loss_pts * lot + cost_rt
    sh, lg = legs(chosen)
    return {"day": day, "expiry": exp, "dte": dte, "spot": sp, "vix": vix[day], "k": k,
            "structure": chosen.name, "top_pick": top.name, "swapped": swapped,
            "credit": chosen.credit_pts, "max_loss_pts": chosen.max_loss_pts, "wing": wing,
            "lot": lot, "cost_rt": cost_rt, "worst_per_lot": worst, "net_ev": chosen.net_ev_rs,
            "cs": cs, "shorts": sh, "longs": lg}, "ok"


# ---------------------------------------------------------------- marks

def mark(ch, sh, lg):
    try:
        return sum(ch[t][k] for k, t in sh) - sum(ch[t][k] for k, t in lg)
    except KeyError:
        return None


def settle(spot_T, sh, lg):
    iv = lambda k, t: max(0.0, spot_T - k) if t == "CE" else max(0.0, k - spot_T)
    return sum(iv(k, t) for k, t in sh) - sum(iv(k, t) for k, t in lg)


# -------------------------------------------------------------- simulate

def simulate(variant, signals, chains, spot, tdays):
    tgt = variant in ("target80", "combined")
    stp = variant in ("stop2x", "combined")
    open_, trades, equity_rows = [], [], []
    realised, peak, pause_until, trips = 0.0, 0.0, -1, 0
    for i, day in enumerate(tdays):
        still = []
        for t in open_:
            u = t["lot"] * t["lots"]
            if day >= t["expiry"]:
                sT = spot.get(t["expiry"], spot[day])
                pay = settle(sT, t["shorts"], t["longs"])
                t.update(exit=day, reason="expiry", exit_cost=pay,
                         pnl=(t["credit"] - pay) * u - t["cost_rt"] * t["lots"] / 2)
            else:
                m = mark(chains.get((day, t["expiry"]), {"CE": {}, "PE": {}}), t["shorts"], t["longs"])
                if m is not None:
                    t["last_mark"] = m
                    t["mae"] = max(t.get("mae", 0.0), (m - t["credit"]) * u)
                hit = None
                if m is not None and tgt and m <= 0.2 * t["credit"]:
                    hit = "target80"
                if m is not None and stp and m >= 2.0 * t["credit"] and hit is None:
                    hit = "stop2x"
                if hit:
                    t.update(exit=day, reason=hit, exit_cost=m,
                             pnl=(t["credit"] - m) * u - t["cost_rt"] * t["lots"])
                else:
                    still.append(t)
                    continue
            realised += t["pnl"]
            trades.append(t)
        open_ = still
        mtm = sum((t["credit"] - t.get("last_mark", t["credit"])) * t["lot"] * t["lots"] for t in open_)
        eq = realised + mtm
        if i >= pause_until:
            if pause_until >= 0 and i == pause_until:
                peak = eq                       # resume: the drawdown clock restarts
            peak = max(peak, eq)
            if peak - eq >= BREAKER:
                pause_until, trips = i + PAUSE, trips + 1
        paused = i < pause_until
        sig = signals.get(day)
        opened = None
        if sig and not paused:
            lots = int(CAP_TRADE // sig["worst_per_lot"]) if sig["worst_per_lot"] > 0 else 0
            book = sum(t["worst_per_lot"] * t["lots"] for t in open_)
            if lots >= 1 and book + lots * sig["worst_per_lot"] <= CAP_BOOK:
                t = dict(sig, lots=lots, entry=day, last_mark=sig["credit"])
                open_.append(t)
                opened = t["structure"]
        equity_rows.append({"day": day, "equity": round(eq, 0), "realised": round(realised, 0),
                            "open": len(open_), "paused": paused, "opened": opened})
    return trades, pd.DataFrame(equity_rows), trips


def monthly(trades):
    df = pd.DataFrame([{"exit": t["exit"], "pnl": t["pnl"]} for t in trades])
    df["month"] = pd.to_datetime(df.exit).dt.to_period("M")
    m = df.groupby("month").pnl.agg(["count", "sum", lambda x: (x > 0).mean()])
    m.columns = ["trades", "pnl", "win_rate"]
    return m


def main():
    os.makedirs(OUT, exist_ok=True)
    print("loading...", flush=True)
    chains, spot, vix, tdays, exps = load()
    print(f"{len(tdays)} days, {len(chains)} day-expiry chains", flush=True)
    signals, reasons = {}, defaultdict(int)
    for day in tdays:
        s, why = signal(day, chains, spot, vix, exps)
        reasons[why] += 1
        if s:
            signals[day] = s
    print("signal days:", dict(reasons), flush=True)
    pd.DataFrame([{k: v for k, v in s.items() if k not in ("shorts", "longs")}
                  for s in signals.values()]).to_csv(f"{OUT}/signals.csv", index=False)
    summary = {"reasons": dict(reasons)}
    for var in VARIANTS:
        trades, eq, trips = simulate(var, signals, chains, spot, tdays)
        tdf = pd.DataFrame([{k: v for k, v in t.items() if k not in ("shorts", "longs")} for t in trades])
        tdf.to_csv(f"{OUT}/trades_{var}.csv", index=False)
        eq.to_csv(f"{OUT}/equity_{var}.csv", index=False)
        m = monthly(trades)
        m.to_csv(f"{OUT}/monthly_{var}.csv")
        dd = (eq.equity.cummax() - eq.equity).max()
        summary[var] = {"trades": len(tdf), "total": round(tdf.pnl.sum(), 0),
                        "win_rate": round((tdf.pnl > 0).mean(), 3), "max_drawdown": round(dd, 0),
                        "breaker_trips": trips, "by_reason": tdf.reason.value_counts().to_dict(),
                        "by_structure": tdf.groupby("structure").pnl.agg(["count", "sum"]).round(0).to_dict()}
        print(var, json.dumps({k: summary[var][k] for k in ("trades", "total", "win_rate", "max_drawdown", "breaker_trips")}), flush=True)
    json.dump(summary, open(f"{OUT}/summary.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
