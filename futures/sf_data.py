"""Point-in-time market data adapters for the stock-futures agents.

Every record an adapter returns carries two times:
  ts            the source timestamp (bar start, quote time, snapshot time)
  available_at  when the system could first have seen it (a 1-minute bar is
                available at its close; a daily bar after the session close;
                a revised snapshot at its revision time)
An adapter only ever returns records with available_at <= as_of, and
PointInTime wraps any adapter to assert that, so replay cannot see a candle,
quote or revision before it existed.

Adapters:
  FixtureData  in-memory scenario data (fixtures, demos, tests)
  RepoData     the repo's own stores: data/stocks_1m (EQ + STKFUT minute bars,
               STKFUT bid/ask from FULL-mode depth), data/bars_1m INDEX
               partition, data/instruments/<day>.json, data/sectors.json
"""
import glob
import json
import os
from datetime import timedelta

from sf_core import IST, parse_ts, iso


class LookAheadError(AssertionError):
    pass


def _visible(rows, as_of):
    return [r for r in rows if parse_ts(r["available_at"]) <= as_of]


def _latest_revision(rows):
    """Collapse revisions: for each ts keep the one with the latest available_at."""
    best = {}
    for r in rows:
        k = r["ts"]
        if k not in best or parse_ts(r["available_at"]) > parse_ts(best[k]["available_at"]):
            best[k] = r
    return [best[k] for k in sorted(best, key=parse_ts)]


class FixtureData:
    """Scenario data held in memory. Structure:
       universe:  [symbol]
       sectors:   {symbol: {"sector": str|None, "ambiguous": bool}}
       bars:      {symbol: {"1m": [bar], "1d": [bar]}}   (benchmark under its own name;
                  futures bars under "FUT:<instrument_id>")
       contracts: [contract]
       quotes:    {instrument_id: [quote]}
       desk:      {symbol: [snapshot]}
    Lists are indexed by available_at on first use, so lookups are a bisect.
    """
    name = "fixture"

    def __init__(self, data):
        self.d = data
        self._idx = {}

    def _index(self, key, rows):
        hit = self._idx.get(key)
        if hit is None or hit[0] is not rows or hit[1] != len(rows):
            srt = sorted(rows, key=lambda r: (parse_ts(r["available_at"]), parse_ts(r["ts"])))
            hit = (rows, len(rows), [parse_ts(r["available_at"]) for r in srt], srt)
            self._idx[key] = hit
        return hit

    def _upto(self, key, rows, as_of):
        import bisect
        _, _, keys, srt = self._index(key, rows)
        return srt[:bisect.bisect_right(keys, as_of)]

    def universe(self, as_of):
        return list(self.d.get("universe", []))

    def sector_map(self, as_of):
        return dict(self.d.get("sectors", {}))

    def sector_index_bars(self, sector, as_of, interval):
        return None                                    # fixtures aggregate constituents

    def bars(self, symbol, as_of, interval):
        rows = self.d.get("bars", {}).get(symbol, {}).get(interval, [])
        return sorted(self._upto(("b", symbol, interval), rows, as_of), key=lambda r: parse_ts(r["ts"]))

    def contracts(self, underlying, as_of):
        return [c for c in _visible(self.d.get("contracts", []), as_of) if c["underlying"] == underlying]

    def quote(self, instrument_id, as_of):
        rows = self._upto(("q", instrument_id), self.d.get("quotes", {}).get(instrument_id, []), as_of)
        return max(rows[-5:], key=lambda r: parse_ts(r["ts"])) if rows else None

    def desk_snapshot(self, symbol, as_of):
        rows = _latest_revision(_visible(self.d.get("desk", {}).get(symbol, []), as_of))
        return rows[-1] if rows else None

    def health(self, as_of):
        return {"source": "fixture", "feeds": {k: "fixture" for k in (
            "benchmark_bars", "stock_bars", "sector_map", "futstk_master", "futstk_quotes", "desk_snapshot")}}


class PointInTime:
    """Wraps an adapter and refuses any record not yet available."""

    def __init__(self, inner):
        self.inner = inner
        self.name = getattr(inner, "name", "?")

    def _check(self, rows, as_of, what):
        for r in rows or []:
            if r and parse_ts(r.get("available_at")) > as_of:
                raise LookAheadError(f"{what}: record available at {r['available_at']} seen at {iso(as_of)}")
        return rows

    def bars(self, symbol, as_of, interval):
        return self._check(self.inner.bars(symbol, as_of, interval), as_of, f"bars {symbol}")

    def sector_index_bars(self, sector, as_of, interval):
        return self._check(self.inner.sector_index_bars(sector, as_of, interval), as_of, f"sector {sector}")

    def contracts(self, underlying, as_of):
        return self._check(self.inner.contracts(underlying, as_of), as_of, f"contracts {underlying}")

    def quote(self, instrument_id, as_of):
        q = self.inner.quote(instrument_id, as_of)
        self._check([q], as_of, f"quote {instrument_id}")
        return q

    def desk_snapshot(self, symbol, as_of):
        s = self.inner.desk_snapshot(symbol, as_of)
        self._check([s], as_of, f"desk {symbol}")
        return s

    def __getattr__(self, k):
        return getattr(self.inner, k)


# ===================================================================== repo

class RepoData:
    """The structured data behind Nifty Desk, read point-in-time.

    Gaps this adapter reports rather than papers over (see health()):
      * tick_size is not in the instrument master today -> contracts carry
        tick_size=None and the contract agent rejects them (required field);
      * only the NEAREST FUTSTK expiry is collected, so Swing cannot pick a
        next-month contract from repo data;
      * STKFUT quotes are the last bid/ask of each minute (FULL-mode depth),
        not tick-level quotes; they are treated as available at minute close.
    """
    name = "repo"

    def __init__(self, root):
        self.root = root
        self.stocks = os.path.join(root, "data", "stocks_1m")
        self.bars_dir = os.path.join(root, "data", "bars_1m")
        self.instr = os.path.join(root, "data", "instruments")
        self._cache = {}

    # -- files
    def _days(self, base):
        out = []
        for p in glob.glob(os.path.join(base, "date=*")):
            out.append(os.path.basename(p).split("=", 1)[1])
        return sorted(out)

    def _part(self, base, day, part):
        key = (base, day, part)
        if key in self._cache:
            return self._cache[key]
        import pandas as pd
        files = sorted(glob.glob(os.path.join(base, f"date={day}", f"underlying={part}", "*.parquet")))
        frames = []
        for f in files:
            try:
                frames.append(pd.read_parquet(f))
            except Exception:  # noqa: BLE001 -- half-written part
                continue
        df = pd.concat(frames, ignore_index=True) if frames else None
        if df is not None and not df.empty:
            df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
        self._cache[key] = df
        return df

    def _master(self, as_of):
        day = str(as_of.date())
        files = sorted(f for f in glob.glob(os.path.join(self.instr, "*.json"))
                       if os.path.basename(f)[:10] <= day)
        if not files:
            return None
        key = ("master", files[-1])
        if key not in self._cache:
            with open(files[-1]) as f:
                self._cache[key] = json.load(f)
        return self._cache[key]

    # -- universe / sectors
    def universe(self, as_of):
        m = self._master(as_of)
        if not m:
            return []
        return sorted({r["name"] for r in m["instruments"] if r.get("kind") == "stock_fut"})

    def sector_map(self, as_of):
        path = os.path.join(self.root, "data", "sectors.json")
        try:
            with open(path) as f:
                raw = json.load(f).get("map", {})
        except (OSError, ValueError):
            return {}
        # "Others" is the fallback label for a stock in no list: unresolved.
        return {s: {"sector": None if v.get("sector") in (None, "Others") else v["sector"],
                    "ambiguous": v.get("sector") in (None, "Others"), "source": "niftyindices"}
                for s, v in raw.items()}

    def sector_index_bars(self, sector, as_of, interval):
        return None                     # sector indices are not collected; constituents are aggregated

    # -- bars
    def _rows_1m(self, df, as_of):
        out = []
        for r in df.itertuples():
            t = r.ts.to_pydatetime()
            avail = t + timedelta(minutes=1)
            if avail <= as_of:
                out.append({"ts": iso(t), "available_at": iso(avail), "open": float(r.open),
                            "high": float(r.high), "low": float(r.low), "close": float(r.close),
                            "volume": int(r.volume or 0), "turnover": float(getattr(r, "pv", 0) or 0)})
        return out

    def bars(self, symbol, as_of, interval):
        if interval == "1m":
            day = str(as_of.date())
            if symbol in ("NIFTY 50", "NIFTY BANK"):
                df = self._part(self.bars_dir, day, "INDEX")
                df = None if df is None else df[df["tradingsymbol"] == symbol]
            else:
                df = self._part(self.stocks, day, "EQ")
                df = None if df is None else df[df["underlying"] == symbol]
            return [] if df is None or df.empty else self._rows_1m(df.sort_values("ts"), as_of)
        # daily: aggregate each completed session's minute bars
        out = []
        base = self.bars_dir if symbol in ("NIFTY 50", "NIFTY BANK") else self.stocks
        for day in self._days(base):
            if day >= str(as_of.date()):
                continue
            part = "INDEX" if base == self.bars_dir else "EQ"
            df = self._part(base, day, part)
            if df is None:
                continue
            col = "tradingsymbol" if part == "INDEX" else "underlying"
            g = df[df[col] == symbol].sort_values("ts")
            if g.empty:
                continue
            close_t = g["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
            out.append({"ts": day + "T09:15:00+05:30", "available_at": iso(close_t),
                        "open": float(g["open"].iloc[0]), "high": float(g["high"].max()),
                        "low": float(g["low"].min()), "close": float(g["close"].iloc[-1]),
                        "volume": int(g["volume"].fillna(0).sum()),
                        "turnover": float(g["pv"].fillna(0).sum()) if "pv" in g else 0.0})
        return [r for r in out if parse_ts(r["available_at"]) <= as_of]

    # -- futures
    def contracts(self, underlying, as_of):
        m = self._master(as_of)
        if not m:
            return []
        avail = m.get("generated_at")
        out = []
        for r in m["instruments"]:
            if r.get("kind") != "stock_fut" or r.get("name") != underlying:
                continue
            out.append({"instrument_id": str(r["instrument_token"]), "tradingsymbol": r["tradingsymbol"],
                        "underlying": r["name"], "instrument_type": "FUTSTK", "exchange": "NFO",
                        "expiry": r.get("expiry"), "lot_size": r.get("lot_size"),
                        "tick_size": r.get("tick_size"), "status": "listed",
                        "ts": avail, "available_at": iso(parse_ts(avail)) if avail else iso(as_of)})
        return out

    def quote(self, instrument_id, as_of):
        df = self._part(self.stocks, str(as_of.date()), "STKFUT")
        if df is None or df.empty or "instrument_token" not in df:
            return None
        g = df[df["instrument_token"] == int(instrument_id)].sort_values("ts")
        best = None
        for r in g.itertuples():
            t = r.ts.to_pydatetime()
            if t + timedelta(minutes=1) > as_of:
                break
            best = r
        if best is None:
            return None
        t = best.ts.to_pydatetime()
        nan = lambda v: None if v is None or v != v else v  # noqa: E731
        return {"instrument_id": instrument_id, "ts": iso(t + timedelta(minutes=1)),
                "available_at": iso(t + timedelta(minutes=1)), "bid": nan(best.bid), "ask": nan(best.ask),
                "bid_qty": nan(best.bid_qty), "ask_qty": nan(best.ask_qty), "last": float(best.close),
                "oi": nan(best.oi), "source": "stocks_1m/STKFUT last-of-minute depth"}

    def desk_snapshot(self, symbol, as_of):
        """The Pulse page's per-stock numbers, recomputed from bars <= as_of."""
        import market_pulse as MP
        day = str(as_of.date())
        eq = self._part(self.stocks, day, "EQ")
        fut = self._part(self.stocks, day, "STKFUT")
        if eq is None:
            return None
        cut = as_of - timedelta(minutes=1)
        e = eq[(eq["underlying"] == symbol) & (eq["ts"] <= cut)]
        if e.empty:
            return None
        m = self._master(as_of) or {"instruments": []}
        prev = {r["tradingsymbol"]: r.get("prev_close") for r in m["instruments"] if r.get("prev_close")}
        tbl = MP.stock_table(e.assign(ts=e["ts"].dt.tz_convert("UTC")), prev, {})
        row = tbl.iloc[0]
        oi_chg = None
        if fut is not None:
            f = fut[(fut["underlying"] == symbol) & (fut["ts"] <= cut)]["oi"].dropna()
            if len(f) and f.iloc[0]:
                oi_chg = (float(f.iloc[-1]) / float(f.iloc[0]) - 1) * 100
        t = e["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
        return {"symbol": symbol, "ts": iso(t), "available_at": iso(t), "source": "stocks_1m via market_pulse",
                "metrics": {"turnover_rs": float(row["turnover"]), "money_flow_net_rs": float(row["net"]),
                            "oi_change_pct": oi_chg},
                "kinds": {"turnover_rs": "observed", "money_flow_net_rs": "estimate",
                          "oi_change_pct": "observed"}}

    def health(self, as_of):
        day = str(as_of.date())
        m = self._master(as_of)
        futs = [r for r in (m or {}).get("instruments", []) if r.get("kind") == "stock_fut"]
        feeds = {
            "benchmark_bars": "ok" if self._part(self.bars_dir, day, "INDEX") is not None else "missing",
            "stock_bars": "ok" if self._part(self.stocks, day, "EQ") is not None else "missing",
            "futstk_quotes": "ok" if self._part(self.stocks, day, "STKFUT") is not None else "missing",
            "sector_map": "ok" if os.path.exists(os.path.join(self.root, "data", "sectors.json")) else "missing",
            "futstk_master": ("ok" if futs and all(r.get("tick_size") for r in futs)
                              else "incomplete: no tick_size" if futs else "missing"),
            "desk_snapshot": "ok" if self._part(self.stocks, day, "EQ") is not None else "missing",
        }
        return {"source": "repo", "session": day, "master": (m or {}).get("date"),
                "stock_futures": len(futs), "feeds": feeds}
