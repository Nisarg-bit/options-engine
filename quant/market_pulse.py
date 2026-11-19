"""Market Pulse: breadth, money flow, sectors, stock OI, order blocks, mood.

Pure functions over DataFrames, so every number on the Pulse page is tested
without a market session. service/pulse_api.py loads the data and calls these.

Inputs are the stock tier the collector writes (data/stocks_1m):
  EQ     one row per stock per minute: open/high/low/close, volume, pv (price x volume)
  STKFUT the same for each stock's nearest future, plus open interest

Honest labels, stated once here and repeated on the page:
  * "Money flow" is an ESTIMATE: a minute's traded value counts as buying when
    the price rose over that minute and as selling when it fell. It is not
    actual fund flows, and not FII/DII data.
  * "Order blocks" are a chart pattern found algorithmically in candles, not
    real resting orders.
"""
import math

import numpy as np
import pandas as pd

CRORE = 1e7


def _clip(x, lo=-1.0, hi=1.0):
    return float(max(lo, min(hi, x)))


# ------------------------------------------------------------------ stocks

def stock_table(eq, prev_close, sectors, recent_minutes=15):
    """One row per stock: last, % change, turnover, VWAP, money flow.

    eq:         EQ bars (columns ts, underlying, close, volume, pv, is_interpolated)
    prev_close: {symbol: yesterday's close}
    sectors:    {symbol: sector label}
    """
    if eq is None or eq.empty:
        return pd.DataFrame()
    df = eq.sort_values(["underlying", "ts"]).copy()
    df["pv"] = df["pv"].fillna(0.0)
    df["volume"] = df["volume"].fillna(0)
    prev = df.groupby("underlying")["close"].shift(1)
    first = prev.isna()
    prev = prev.where(~first, df["underlying"].map(prev_close))
    move = np.sign(df["close"] - prev).fillna(0.0)
    df["inflow"] = np.where(move > 0, df["pv"], 0.0)
    df["outflow"] = np.where(move < 0, df["pv"], 0.0)
    cutoff = df["ts"].max() - pd.Timedelta(minutes=recent_minutes)
    recent = df[df["ts"] > cutoff]

    g = df.groupby("underlying")
    out = pd.DataFrame({
        "last": g["close"].last(),
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "volume": g["volume"].sum(),
        "turnover": g["pv"].sum(),
        "inflow": g["inflow"].sum(),
        "outflow": g["outflow"].sum(),
    })
    rg = recent.groupby("underlying")
    out["recent_net"] = (rg["inflow"].sum() - rg["outflow"].sum()).reindex(out.index).fillna(0.0)
    out["net"] = out["inflow"] - out["outflow"]
    out["vwap"] = np.where(out["volume"] > 0, out["turnover"] / out["volume"].replace(0, np.nan), np.nan)
    out["prev_close"] = out.index.map(prev_close)
    out["chg_pct"] = (out["last"] / out["prev_close"] - 1.0) * 100.0
    out["above_vwap"] = out["last"] > out["vwap"]
    out["sector"] = [sectors.get(s, "Others") for s in out.index]
    return out


def breadth(tbl, flat=0.05):
    """Advancers, decliners, unchanged (|change| <= flat %), and % above VWAP."""
    if tbl.empty or tbl["chg_pct"].isna().all():
        return {"available": False}
    c = tbl["chg_pct"].dropna()
    adv, dec = int((c > flat).sum()), int((c < -flat).sum())
    unch = int(len(c) - adv - dec)
    vw = tbl["above_vwap"][tbl["vwap"].notna()]
    return {"available": True, "advancers": adv, "decliners": dec, "unchanged": unch,
            "ratio": (adv - dec) / (adv + dec) if adv + dec else 0.0,
            "above_vwap_pct": round(100.0 * vw.mean(), 1) if len(vw) else None,
            "stocks": int(len(c))}


def money_flow(tbl, top=8):
    """Inflow / outflow / net in crore, and the fastest movers either way."""
    if tbl.empty:
        return {"available": False}
    inflow, outflow = float(tbl["inflow"].sum()), float(tbl["outflow"].sum())
    def rows(df):
        return [{"symbol": s, "sector": r.sector, "net_cr": round(r.net / CRORE, 1),
                 "recent_cr": round(r.recent_net / CRORE, 1),
                 "chg_pct": None if pd.isna(r.chg_pct) else round(r.chg_pct, 2)}
                for s, r in df.iterrows()]
    return {"available": True,
            "inflow_cr": round(inflow / CRORE, 0), "outflow_cr": round(outflow / CRORE, 0),
            "net_cr": round((inflow - outflow) / CRORE, 0),
            "ratio": (inflow - outflow) / (inflow + outflow) if inflow + outflow else 0.0,
            "top_in": rows(tbl.sort_values("net", ascending=False).head(top)),
            "top_out": rows(tbl.sort_values("net").head(top)),
            "fastest_in": rows(tbl.sort_values("recent_net", ascending=False).head(top)),
            "fastest_out": rows(tbl.sort_values("recent_net").head(top))}


def sector_map(tbl, lead=2):
    """Per sector: turnover-weighted % change, turnover, net flow; leaders and laggards."""
    if tbl.empty:
        return {"available": False}
    t = tbl.dropna(subset=["chg_pct"])
    rows = []
    for sec, g in t.groupby("sector"):
        w = g["turnover"].clip(lower=0)
        chg = float(np.average(g["chg_pct"], weights=w)) if w.sum() > 0 else float(g["chg_pct"].mean())
        stocks = [{"symbol": s, "chg_pct": round(r.chg_pct, 2), "turnover_cr": round(r.turnover / CRORE, 1)}
                  for s, r in g.sort_values("turnover", ascending=False).iterrows()]
        rows.append({"sector": sec, "chg_pct": round(chg, 2), "n": int(len(g)),
                     "turnover_cr": round(float(g["turnover"].sum()) / CRORE, 1),
                     "net_cr": round(float(g["net"].sum()) / CRORE, 1), "stocks": stocks})
    rows.sort(key=lambda r: r["turnover_cr"], reverse=True)
    by_chg = sorted(rows, key=lambda r: r["chg_pct"])
    return {"available": True, "sectors": rows,
            "leaders": [{"sector": r["sector"], "chg_pct": r["chg_pct"]} for r in by_chg[::-1][:lead]],
            "laggards": [{"sector": r["sector"], "chg_pct": r["chg_pct"]} for r in by_chg[:lead]]}


# --------------------------------------------------------------- futures OI

def classify_oi(d_price, d_oi, eps=0.0005):
    """The four textbook readings of price and OI moving together."""
    if d_price is None or d_oi is None or (abs(d_price) < eps and abs(d_oi) < eps):
        return "neutral"
    if d_oi >= 0:
        return "long buildup" if d_price >= 0 else "short buildup"
    return "short covering" if d_price >= 0 else "long unwinding"


def _pct(a, b):
    """(a / b - 1) in percent, 2 dp, or None."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if not (b > 0) or a != a:
        return None
    return round(100.0 * (a / b - 1.0), 2)


def stock_oi(fut, prev_close, sectors=None):
    """Per stock future: price change vs yesterday, OI change since the first bar,
    and (for the Stock Futures 'Market' view) the day's open, gap and move since
    the open, plus the stock's sector."""
    if fut is None or fut.empty:
        return {"available": False}
    sectors = sectors or {}
    rows = []
    for sym, g in fut.sort_values("ts").groupby("underlying"):
        oi = g["oi"].dropna()
        if oi.empty:
            continue
        pc = prev_close.get(g["tradingsymbol"].iloc[-1]) or prev_close.get(sym)
        last = float(g["close"].iloc[-1])
        dp = (last / pc - 1.0) if pc else None
        doi = (float(oi.iloc[-1]) / float(oi.iloc[0]) - 1.0) if oi.iloc[0] else None
        # The day's "open" is the CLOSE of the first traded minute (09:15). The
        # collector chains each bar's open to the previous close, so the first
        # bar's open is yesterday's close and every gap would read 0% (19 Nov 2026).
        traded = g[g["volume"].fillna(0) > 0] if "volume" in g else g
        first = (traded if len(traded) else g)["close"].dropna()
        op = float(first.iloc[0]) if len(first) else None
        rows.append({"symbol": sym, "price_chg_pct": None if dp is None else round(100 * dp, 2),
                     "oi": int(oi.iloc[-1]), "oi_chg_pct": None if doi is None else round(100 * doi, 2),
                     "read": classify_oi(dp, doi),
                     "sector": sectors.get(sym, "Others"),
                     "last": round(last, 2), "open": None if op is None else round(op, 2),
                     "gap_pct": _pct(op, pc), "intraday_pct": _pct(last, op)})
    counts = {k: sum(1 for r in rows if r["read"] == k)
              for k in ("long buildup", "short buildup", "short covering", "long unwinding", "neutral")}
    rows.sort(key=lambda r: abs(r["oi_chg_pct"] or 0), reverse=True)
    return {"available": True, "counts": counts, "rows": rows}


# ------------------------------------------------------------- order blocks

def resample(bars, minutes=5):
    """1-minute bars -> N-minute candles (interpolated minutes ignored)."""
    b = bars[~bars["is_interpolated"]] if "is_interpolated" in bars else bars
    if b.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    b = b.set_index("ts").sort_index()
    r = b.resample(f"{minutes}min", label="left", closed="left")
    out = pd.DataFrame({"open": r["open"].first(), "high": r["high"].max(),
                        "low": r["low"].min(), "close": r["close"].last(),
                        "volume": r["volume"].sum()}).dropna(subset=["open"])
    return out


def fut_candles(fut, minutes=15):
    """{underlying: {"t": [HH:MM IST], "o","h","l","c": [...], "vwap": [...]}} for the
    Stock Futures market view's small chart.

    The collector chains each 1-minute bar's open to the previous close, so the
    session's first bar 'opens' at yesterday's close and its high or low can be
    that synthetic price. The first candle therefore opens at the first traded
    minute's close (the same "open" stock_oi uses), and a high/low equal to the
    synthetic open is pulled back to that close. VWAP is cumulative pv / volume."""
    if fut is None or fut.empty:
        return {}
    out = {}
    for sym, g in fut.sort_values("ts").groupby("underlying"):
        g = g[~g["is_interpolated"]] if "is_interpolated" in g else g
        traded = g[g["volume"].fillna(0) > 0] if "volume" in g else g
        if traded.empty:
            continue
        g = g[g["ts"] >= traded["ts"].iloc[0]].copy()
        i0 = g.index[0]
        o0, c0 = g.at[i0, "open"], g.at[i0, "close"]
        if g.at[i0, "high"] == o0:
            g.at[i0, "high"] = max(c0, g.at[i0, "low"])
        if g.at[i0, "low"] == o0:
            g.at[i0, "low"] = min(c0, g.at[i0, "high"])
        g.at[i0, "open"] = c0
        pv = g["pv"].fillna(0.0) if "pv" in g else g["close"] * g["volume"].fillna(0)
        g["cpv"], g["cvol"] = pv.cumsum(), g["volume"].fillna(0).cumsum()
        r = g.set_index("ts").resample(f"{minutes}min", label="left", closed="left")
        c = pd.DataFrame({"o": r["open"].first(), "h": r["high"].max(), "l": r["low"].min(),
                          "c": r["close"].last(), "cpv": r["cpv"].last(), "cvol": r["cvol"].last()}).dropna(subset=["o"])
        if c.empty:
            continue
        vw = (c["cpv"] / c["cvol"].where(c["cvol"] > 0)).round(2)
        idx = c.index.tz_convert("Asia/Kolkata") if c.index.tz is not None else c.index
        out[str(sym)] = {"t": [t.strftime("%H:%M") for t in idx],
                         **{k: [round(float(v), 2) for v in c[k]] for k in ("o", "h", "l", "c")},
                         "vwap": [None if v != v else float(v) for v in vw]}
    return out


def order_blocks(candles, k=3, mult=1.5, keep=2):
    """Order-block zones, found algorithmically.

    Bullish block: the last DOWN candle before a move up that closes more than
    `mult` x ATR above that candle's high within `k` candles. Zone = its low to
    its open. Bearish block: the mirror -- the last UP candle before a drop.
    A zone is 'retested' once a later candle trades back into it.
    """
    c = candles.reset_index()
    n = len(c)
    if n < k + 2:
        return []
    tr = np.maximum(c["high"] - c["low"],
                    np.maximum((c["high"] - c["close"].shift()).abs(),
                               (c["low"] - c["close"].shift()).abs()))
    atr = float(tr.rolling(14, min_periods=3).mean().iloc[-1] or tr.mean())
    if not atr or math.isnan(atr):
        return []
    found = []
    for i in range(n - k):
        o, h, l, cl = c.loc[i, ["open", "high", "low", "close"]]
        ahead = c.loc[i + 1:i + k]
        if cl < o and ahead["close"].max() - h > mult * atr:
            zone, side = (float(l), float(o)), "buy"
        elif cl > o and l - ahead["close"].min() > mult * atr:
            zone, side = (float(o), float(h)), "sell"
        else:
            continue
        later = c.loc[i + k + 1:]
        if side == "buy":
            tested = bool((later["low"] <= zone[1]).any()) if len(later) else False
        else:
            tested = bool((later["high"] >= zone[0]).any()) if len(later) else False
        found.append({"side": side, "lo": round(zone[0], 2), "hi": round(zone[1], 2),
                      "at": str(c.loc[i, "ts"]), "tested": tested})
    buys = [z for z in found if z["side"] == "buy"][-keep:]
    sells = [z for z in found if z["side"] == "sell"][-keep:]
    return buys + sells


def nearest_untested(blocks, price):
    cands = [b for b in blocks if not b["tested"]]
    if not cands:
        return None
    return min(cands, key=lambda b: min(abs(price - b["lo"]), abs(price - b["hi"])))


# --------------------------------------------------------------------- mood

WEIGHTS = {"breadth": 0.30, "flow": 0.25, "trend": 0.20, "volatility": 0.15, "global": 0.10}
LABELS = [(20, "Risk-off"), (40, "Cautious"), (60, "Neutral"), (80, "Constructive"), (101, "Risk-on")]


def vix_percentile(history, vix, days=252):
    """Share of the last `days` closes at or below today's VIX (0..1)."""
    h = [v for v in list(history)[-days:] if v is not None and not math.isnan(v)]
    if not h or vix is None:
        return None
    return sum(1 for v in h if v <= vix) / len(h)


def mood(breadth_ratio=None, flow_ratio=None, nifty_ret_pct=None, day_sigma_pct=None,
         vix_pct=None, global_pct=None):
    """0-100 market mood from five parts, each scaled to -1..+1.

    breadth     (advancers - decliners) / (advancers + decliners)
    flow        (inflow - outflow) / (inflow + outflow)
    trend       NIFTY's move today in units of half a normal day's move
    volatility  1 - 2 x VIX percentile (calm = positive, stressed = negative)
    global      US index futures' move, 1% = most of the scale
    A missing part is left out and the rest are re-weighted, never guessed.
    """
    parts = {}
    if breadth_ratio is not None:
        parts["breadth"] = _clip(breadth_ratio)
    if flow_ratio is not None:
        parts["flow"] = _clip(flow_ratio)
    if nifty_ret_pct is not None and day_sigma_pct:
        parts["trend"] = _clip(math.tanh(nifty_ret_pct / (0.5 * day_sigma_pct)))
    if vix_pct is not None:
        parts["volatility"] = _clip(1.0 - 2.0 * vix_pct)
    if global_pct is not None:
        parts["global"] = _clip(math.tanh(global_pct / 1.0))
    if not parts:
        return {"available": False}
    wsum = sum(WEIGHTS[k] for k in parts)
    s = sum(WEIGHTS[k] * v for k, v in parts.items()) / wsum
    score = int(round(50 + 50 * s))
    label = next(lab for cut, lab in LABELS if score < cut)
    return {"available": True, "score": score, "label": label,
            "parts": [{"name": k, "weight": WEIGHTS[k], "value": round(parts[k], 2)}
                      for k in WEIGHTS if k in parts],
            "missing": [k for k in WEIGHTS if k not in parts]}


def expected_range(spot, vix, dte_days):
    """±1 sigma range to expiry from India VIX (about 2 days in 3 inside it)."""
    if not spot or not vix or dte_days is None:
        return None
    sig = spot * vix / 100.0 * math.sqrt(max(dte_days, 0.5) / 365.0)
    return {"lo": round(spot - sig, 2), "hi": round(spot + sig, 2), "sigma_pts": round(sig, 2)}
