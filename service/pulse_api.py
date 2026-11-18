"""/api/pulse -- the Market Pulse page's data, in one payload.

Loads the stock tier (data/stocks_1m), the day's instrument master (for
yesterday's closes), the sector map and the index bars, and hands them to the
pure functions in quant/market_pulse.py. Every block says `available: false`
with a reason rather than failing the whole payload: the page renders what it
has and labels the rest.
"""
import glob
import json
import os
import time
from datetime import date

import pandas as pd
from fastapi import APIRouter

import market_pulse as MP
import project_paths

router = APIRouter()
ROOT = project_paths.ROOT
STOCKS = os.path.join(ROOT, "data", "stocks_1m")
INSTR = os.path.join(ROOT, "data", "instruments")
OB_STOCKS = 24          # order-block cards: the most active stocks only
_parts = {}             # path -> (mtime, size, DataFrame)
_world = {"at": 0.0, "pct": None}


def stock_sessions():
    out = []
    for p in glob.glob(os.path.join(STOCKS, "date=*")):
        try:
            out.append(date.fromisoformat(os.path.basename(p).split("=", 1)[1]))
        except ValueError:
            pass
    return sorted(out)


def read_tier(day, part):
    """All Parquet parts of one stock partition, re-reading only what changed."""
    frames = []
    for path in sorted(glob.glob(os.path.join(STOCKS, f"date={day}", f"underlying={part}", "*.parquet"))):
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig = (st.st_mtime, st.st_size)
        hit = _parts.get(path)
        if hit and hit[0] == sig:
            frames.append(hit[1])
            continue
        try:
            df = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 -- half-written part; next poll gets it
            if hit:
                frames.append(hit[1])
            continue
        _parts[path] = (sig, df)
        frames.append(df)
    prefix = os.path.join(STOCKS, f"date={day}")
    for stale in [k for k in _parts if not k.startswith(prefix)]:
        del _parts[stale]
    return pd.concat(frames, ignore_index=True) if frames else None


def prev_closes(day):
    """{tradingsymbol: yesterday's close} from that day's instrument master."""
    path = os.path.join(INSTR, f"{day}.json")
    try:
        rows = json.load(open(path)).get("instruments", [])
    except (OSError, ValueError):
        return {}
    return {r["tradingsymbol"]: r["prev_close"] for r in rows
            if r.get("kind") in ("stock", "stock_fut") and r.get("prev_close")}


_vix = {"key": None, "vals": []}
_closes = {}             # past session -> {index tradingsymbol: last close}; never changes


def closes_on(day):
    """Last close of each index (NIFTY 50, NIFTY BANK, SENSEX, INDIA VIX) on a PAST
    session, read straight from its Parquet parts and remembered.

    Deliberately NOT api.read_parts: that cache keeps one session at a time and
    drops every other session's parts when asked for a different day, so one
    look at yesterday would make the next chain/structures build re-read all of
    today's ~1,500 parts. During market hours that is the difference between a
    publisher pass of seconds and one of many minutes."""
    key = str(day)
    if key in _closes:
        return _closes[key]
    import api
    frames = []
    for path in sorted(glob.glob(os.path.join(api.BARS, f"date={day}", "underlying=INDEX", "*.parquet"))):
        try:
            frames.append(pd.read_parquet(path, columns=["ts", "tradingsymbol", "close"]))
        except Exception:  # noqa: BLE001 -- a bad part is skipped
            continue
    out = {}
    if frames:
        df = pd.concat(frames, ignore_index=True).sort_values("ts")
        out = {str(k): float(v) for k, v in df.groupby("tradingsymbol")["close"].last().items()}
    if day != api.latest_session():
        _closes[key] = out
    return out


def vix_history():
    """Daily India VIX closes: data/vix_history.csv, extended with each collected
    session's last INDIA VIX bar so the one-year percentile does not go stale
    when the CSV is not refreshed. Rebuilt at most once per new session."""
    path = os.path.join(ROOT, "data", "vix_history.csv")
    try:
        df = pd.read_csv(path)
        base = dict(zip(df["date"].astype(str), df["vix"].astype(float)))
    except Exception:  # noqa: BLE001
        base = {}
    try:
        import api
        days = [d for d in api.sessions() if str(d) > max(base, default="")]
    except Exception:  # noqa: BLE001
        days = []
    key = (len(base), str(days[-1]) if days else None)
    if _vix["key"] == key:
        return _vix["vals"]
    for d in days:
        try:
            v = closes_on(d).get("INDIA VIX")
            if v:
                base[str(d)] = v
        except Exception:  # noqa: BLE001 -- one bad day is skipped
            continue
    _vix.update(key=key, vals=[base[k] for k in sorted(base)])
    return _vix["vals"]


def global_pct(ttl=600):
    """Average move of S&P 500 and Nasdaq 100 futures (%), refreshed every 10 min."""
    if time.time() - _world["at"] < ttl:
        return _world["pct"]
    pct = None
    try:
        import world
        rows = world.prices().get("rows", [])
        vals = [r["chg_pct"] for r in rows if r.get("key") in ("es", "nq") and r.get("chg_pct") is not None]
        pct = sum(vals) / len(vals) if vals else None
    except Exception:  # noqa: BLE001
        pct = None
    _world.update(at=time.time(), pct=pct)
    return pct


INDEX_NAMES = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY", "SENSEX": "SENSEX", "INDIA VIX": "VIX"}


def indices(day):
    """Last level and change for the indices, from the option tier's INDEX partition."""
    import api
    out = {}
    # Only the newest session goes through api.read_parts (see closes_on).
    df = api.read_parts(day, "INDEX") if day == api.latest_session() else None
    if df is None and day != api.latest_session():
        return {k_name: {"last": v} for k_sym, k_name in INDEX_NAMES.items()
                for k, v in closes_on(day).items() if k == k_sym}
    days = [d for d in api.sessions() if d < day]
    prev = closes_on(days[-1]) if days else {}
    if df is None or df.empty:
        return out
    for sym, name in INDEX_NAMES.items():
        g = df[df["tradingsymbol"] == sym].sort_values("ts")
        if g.empty:
            continue
        last = float(g["close"].iloc[-1])
        pc = prev.get(sym)
        out[name] = {"last": round(last, 2), "prev_close": pc,
                     "chg": None if pc is None else round(last - pc, 2),
                     "chg_pct": None if not pc else round((last / pc - 1) * 100, 2),
                     "open": round(float(g["open"].iloc[0]), 2),
                     "as_of": str(g["ts"].iloc[-1])}
    return out


def nearest_expiries(day):
    path = os.path.join(INSTR, f"{day}.json")
    try:
        exps = json.load(open(path)).get("expiries", {})
    except (OSError, ValueError):
        return {}
    return {k: v[0] for k, v in exps.items() if v}


def order_block_cards(eq, tbl, prev):
    if eq is None or tbl.empty:
        return []
    pick = tbl.reindex(tbl["turnover"].sort_values(ascending=False).index).head(OB_STOCKS)
    cards = []
    for sym, r in pick.iterrows():
        bars = eq[eq["underlying"] == sym]
        c5 = MP.resample(bars, 5)
        if c5.empty:
            continue
        blocks = MP.order_blocks(c5)
        near = MP.nearest_untested(blocks, float(r["last"]))
        cards.append({"symbol": sym, "sector": r["sector"], "last": round(float(r["last"]), 2),
                      "chg_pct": None if pd.isna(r["chg_pct"]) else round(float(r["chg_pct"]), 2),
                      "net_cr": round(float(r["net"]) / MP.CRORE, 1),
                      "candles": [[str(t)[11:16], round(o, 2), round(h, 2), round(l, 2), round(c, 2)]
                                  for t, (o, h, l, c) in c5[["open", "high", "low", "close"]].iterrows()],
                      "blocks": blocks, "nearest": near})
    return cards


@router.get("/api/pulse")
def api_pulse(day: str = None):
    days = stock_sessions()
    if day:
        day = date.fromisoformat(day)
    elif days:
        day = days[-1]
    else:
        # No stock data yet: still show indices, VIX and mood from the option tier.
        try:
            import api
            day = api.latest_session()
        except Exception:  # noqa: BLE001
            day = None
    out = {"session": str(day) if day else None, "generated": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(timespec="seconds"),
           "notes": ["Money flow is an estimate from price x volume, not actual fund flows. FII/DII figures are NSE's published (provisional) numbers.",
                     "Order blocks are a chart pattern found algorithmically, not real resting orders.",
                     "Research and paper trading only. Not investment advice."]}
    import sectors as SEC
    smap = {k: v["sector"] for k, v in SEC.load().items()}
    prev = prev_closes(day) if day else {}

    eq = read_tier(day, "EQ") if day else None
    fut = read_tier(day, "STKFUT") if day else None
    tbl = MP.stock_table(eq, prev, smap)
    if tbl.empty:
        out["stocks"] = {"available": False, "reason": "no stock data collected for this session yet"}
    out["breadth"] = MP.breadth(tbl)
    out["flow"] = MP.money_flow(tbl)
    out["sectors"] = MP.sector_map(tbl)
    out["stock_oi"] = MP.stock_oi(fut, prev)
    out["order_blocks"] = order_block_cards(eq, tbl, prev)
    try:                                    # actual institutional flows (NSE), evenings only
        import fii_dii as FD
        FD.fetch_background()               # never waits on NSE; see fii_dii.fetch_background
        out["fii_dii"] = FD.summary(FD.load())
    except Exception as e:  # noqa: BLE001 -- never costs the rest of the page
        out["fii_dii"] = {"available": False, "reason": f"{type(e).__name__}: {e}"}

    try:
        idx = indices(day) if day else {}
    except Exception as e:  # noqa: BLE001
        idx, out["indices_error"] = {}, f"{type(e).__name__}: {e}"
    vix = (idx.get("VIX") or {}).get("last")
    hist = vix_history()
    vpct = MP.vix_percentile(hist, vix)
    exps = nearest_expiries(day) if day else {}
    for name in ("NIFTY", "BANKNIFTY", "SENSEX"):
        if name in idx and name in exps and vix:
            dte = (date.fromisoformat(exps[name]) - day).days
            idx[name]["expiry"] = exps[name]
            idx[name]["range"] = MP.expected_range(idx[name]["last"], vix, dte)
    out["indices"] = idx
    out["vix"] = {"last": vix, "percentile": None if vpct is None else round(vpct * 100),
                  "low_1y": min(hist[-252:]) if hist else None, "high_1y": max(hist[-252:]) if hist else None,
                  "day_move_pct": None if not vix else round(vix / (252 ** 0.5), 2)}

    n = idx.get("NIFTY") or {}
    out["mood"] = MP.mood(
        breadth_ratio=out["breadth"].get("ratio"),
        flow_ratio=out["flow"].get("ratio"),
        nifty_ret_pct=n.get("chg_pct"),
        day_sigma_pct=out["vix"]["day_move_pct"],
        vix_pct=vpct,
        global_pct=global_pct())
    return out
