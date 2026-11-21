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
            "benchmark_bars", "vwap_source_bars", "stock_bars", "sector_map", "futstk_master", "futstk_quotes", "desk_snapshot")}}


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

    def fut_bars(self, symbol, as_of):
        fn = getattr(self.inner, "fut_bars", None)
        return self._check(fn(symbol, as_of), as_of, f"futures bars {symbol}") if fn else []

    def __getattr__(self, k):
        return getattr(self.inner, k)


# ===================================================================== repo

COLS = ["ts", "instrument_token", "tradingsymbol", "underlying", "expiry", "open", "high", "low", "close",
        "volume", "pv", "oi", "bid", "ask", "bid_qty", "ask_qty"]
SNAP_TTL_S = 20          # re-stat a live partition at most this often
DAILY_SESSIONS = 15      # completed sessions kept as daily summaries (Swing needs 6)


class RepoData:
    """The structured data behind Nifty Desk, read point-in-time.

    Built for the small server it runs on, next to the collector:
      * a live (today's) partition is re-checked at most every SNAP_TTL_S and
        only parts whose (mtime, size) changed are re-read, so new minute bars
        appear without re-reading the whole session;
      * past sessions are never held as minute bars: each is reduced once to a
        one-row-per-symbol daily summary, and only the last DAILY_SESSIONS are
        looked at;
      * only the columns the agents use are loaded.

    Gaps reported rather than papered over (see health()):
      * contracts need tick_size in the instrument master (written from 18 Nov 2026);
      * only the NEAREST FUTSTK expiry is collected, so Swing cannot pick next month;
      * STKFUT quotes are the last bid/ask of each minute (FULL-mode depth),
        treated as available at minute close;
      * the VWAP source is the NIFTY near-month future in stocks_1m/IDXFUT.
    """
    name = "repo"

    def __init__(self, root, lag_s=0):
        """lag_s: extra seconds before a minute bar or quote counts as available.
        0 for the live worker (its own refresh delay is physical). Replays for
        evaluation use SNAP_TTL_S, the live worst case: the live worker re-checks
        partitions at most every SNAP_TTL_S, so without this a replay could act on
        a minute up to 20 s earlier than live ever could (found 20 Nov 2026)."""
        self.root = root
        self.lag = timedelta(seconds=lag_s)
        self.stocks = os.path.join(root, "data", "stocks_1m")
        self.bars_dir = os.path.join(root, "data", "bars_1m")
        self.instr = os.path.join(root, "data", "instruments")
        self._snap = {}        # (base, day, part) -> {"df", "files": {path: (mtime, size)}, "groups", "checked"}
        self._daily = {}       # (base, part, day) -> {symbol: daily bar}
        self._masters = {}
        self._day = None

    # -- files
    def _days(self, base):
        out = []
        for p in glob.glob(os.path.join(base, "date=*")):
            out.append(os.path.basename(p).split("=", 1)[1])
        return sorted(out)

    def _read(self, path):
        import pandas as pd
        try:
            import pyarrow.parquet as pq
            names = set(pq.read_schema(path).names)
            return pd.read_parquet(path, columns=[c for c in COLS if c in names])
        except Exception:  # noqa: BLE001 -- half-written part; the next check picks it up
            return None

    def _part(self, base, day, part, col="underlying"):
        """(df, {key: row positions}) for one partition, or (None, {}).

        ONE copy per partition: minute parts are appended as they appear (the
        collector only ever adds files), a changed or removed part forces a
        full re-read, and group indexes are built lazily per key column."""
        import time
        import pandas as pd
        key = (base, day, part)
        snap = self._snap.get(key)
        if snap is None or time.monotonic() - snap["checked"] >= SNAP_TTL_S:
            if self._day != day:                   # a new session: drop yesterday's live data
                self._snap = {k: v for k, v in self._snap.items() if k[1] == day}
                self._day = day
                snap = self._snap.get(key)
            files = {}
            for path in glob.glob(os.path.join(base, f"date={day}", f"underlying={part}", "*.parquet")):
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                files[path] = (st.st_mtime, st.st_size)
            old = snap["files"] if snap else {}
            if snap and all(files.get(p) == sig for p, sig in old.items()):
                new = sorted(p for p in files if p not in old)
                frames = [snap["df"]] if snap["df"] is not None else []
                kept = dict(old)
            else:                                  # first read, or a part was rewritten / removed
                new = sorted(files)
                frames, kept = [], {}
            added = False
            for path in new:
                df = self._read(path)
                if df is None:                     # half-written: try again next time
                    continue
                df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
                frames.append(df)
                kept[path] = files[path]
                added = True
            if added or snap is None or kept != old:
                df = pd.concat(frames, ignore_index=True) if frames else None
                if df is not None and not df.empty:
                    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
                    for c in ("tradingsymbol", "underlying"):
                        if c in df and df[c].dtype == object:
                            df[c] = df[c].astype("category")
                snap = {"df": df, "files": kept, "groups": {}}
            snap["checked"] = time.monotonic()
            self._snap[key] = snap
        df = snap["df"]
        if df is None or df.empty or col not in df:
            return df, {}
        if col not in snap["groups"]:
            snap["groups"][col] = df.groupby(col, observed=True).indices
        return df, snap["groups"][col]

    def _rows(self, base, day, part, symbol, col="underlying"):
        df, groups = self._part(base, day, part, col)
        idx = groups.get(symbol)
        return None if df is None or idx is None else df.iloc[idx]

    def _master(self, as_of):
        day = str(as_of.date())
        files = sorted(f for f in glob.glob(os.path.join(self.instr, "*.json"))
                       if os.path.basename(f)[:10] <= day)
        if not files:
            return None
        if files[-1] not in self._masters:
            with open(files[-1]) as f:
                self._masters = {files[-1]: json.load(f)}     # keep only the newest
        return self._masters[files[-1]]

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
            avail = t + timedelta(minutes=1) + self.lag
            if avail > as_of:
                break                                     # sorted by ts
            out.append({"ts": iso(t), "available_at": iso(avail), "open": float(r.open),
                        "high": float(r.high), "low": float(r.low), "close": float(r.close),
                        "volume": int(r.volume or 0), "turnover": float(getattr(r, "pv", 0) or 0)})
        return out

    def _where(self, symbol):
        """(base, partition, key column, key) for a symbol's minute bars."""
        if symbol in ("NIFTY 50", "NIFTY BANK"):
            return self.bars_dir, "INDEX", "tradingsymbol", symbol
        if symbol.endswith(" FUT"):
            return self.stocks, "IDXFUT", "underlying", symbol[:-4]
        return self.stocks, "EQ", "underlying", symbol

    def bars(self, symbol, as_of, interval):
        base, part, col, key = self._where(symbol)
        if interval == "1m":
            g = self._rows(base, str(as_of.date()), part, key, col)
            if g is None or g.empty:
                return []
            if part == "IDXFUT" and "expiry" in g:
                g = g[g["expiry"] == g["expiry"].min()]      # nearest contract only
            return self._rows_1m(g, as_of)
        out = []
        days = [d for d in self._days(base) if d < str(as_of.date())][-DAILY_SESSIONS:]
        for day in days:
            bar = self._daily_bar(base, part, col, day, key)
            if bar and parse_ts(bar["available_at"]) <= as_of:
                out.append(bar)
        return out

    def _daily_bar(self, base, part, col, day, key):
        dk = (base, part, day)
        if dk not in self._daily:
            import pandas as pd
            frames = [x for x in (self._read(p) for p in sorted(glob.glob(
                os.path.join(base, f"date={day}", f"underlying={part}", "*.parquet")))) if x is not None]
            summary = {}
            if frames:
                df = pd.concat(frames, ignore_index=True)
                df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
                for k, g in df.sort_values("ts").groupby(col):
                    close_t = g["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
                    summary[k] = {"ts": day + "T09:15:00+05:30", "available_at": iso(close_t),
                                  "open": float(g["open"].iloc[0]), "high": float(g["high"].max()),
                                  "low": float(g["low"].min()), "close": float(g["close"].iloc[-1]),
                                  "volume": int(g["volume"].fillna(0).sum()),
                                  "turnover": float(g["pv"].fillna(0).sum()) if "pv" in g else 0.0}
                del df, frames
            self._daily[dk] = summary
            keep = sorted({k[2] for k in self._daily})[-(DAILY_SESSIONS + 2):]
            self._daily = {k: v for k, v in self._daily.items() if k[2] in keep}
        return self._daily[dk].get(key)

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

    def fut_bars(self, symbol, as_of):
        """Today's 1-minute bars of the stock's nearest future, with OI (for OI indicators)."""
        g = self._rows(self.stocks, str(as_of.date()), "STKFUT", symbol)
        if g is None or g.empty:
            return []
        if "expiry" in g:
            g = g[g["expiry"] == g["expiry"].min()]
        rows = self._rows_1m(g, as_of)
        oi = list(g["oi"]) if "oi" in g else []
        for r, x in zip(rows, oi):
            r["oi"] = None if x is None or x != x else float(x)
        return rows

    def quote(self, instrument_id, as_of):
        g = self._rows(self.stocks, str(as_of.date()), "STKFUT", int(instrument_id), "instrument_token")
        if g is None or g.empty:
            return None
        best = None
        for r in g.itertuples():
            t = r.ts.to_pydatetime()
            if t + timedelta(minutes=1) + self.lag > as_of:
                break
            best = r
        if best is None:
            return None
        t = best.ts.to_pydatetime()
        nan = lambda v: None if v is None or v != v else v  # noqa: E731
        return {"instrument_id": instrument_id, "ts": iso(t + timedelta(minutes=1)),
                "available_at": iso(t + timedelta(minutes=1) + self.lag), "bid": nan(best.bid), "ask": nan(best.ask),
                "bid_qty": nan(best.bid_qty), "ask_qty": nan(best.ask_qty), "last": float(best.close),
                "oi": nan(best.oi), "source": "stocks_1m/STKFUT last-of-minute depth"}

    def desk_snapshot(self, symbol, as_of):
        """The Pulse page's per-stock numbers, recomputed from bars <= as_of."""
        import market_pulse as MP
        day = str(as_of.date())
        e = self._rows(self.stocks, day, "EQ", symbol)
        if e is None:
            return None
        cut = as_of - timedelta(minutes=1) - self.lag
        e = e[e["ts"] <= cut]
        if e.empty:
            return None
        m = self._master(as_of) or {"instruments": []}
        prev = {r["tradingsymbol"]: r.get("prev_close") for r in m["instruments"] if r.get("prev_close")}
        tbl = MP.stock_table(e.assign(ts=e["ts"].dt.tz_convert("UTC"), is_interpolated=False), prev, {})
        row = tbl.iloc[0]
        oi_chg = px_chg = read = None
        g = self._rows(self.stocks, day, "STKFUT", symbol)
        if g is not None:
            g = g[g["ts"] <= cut]
            f = g["oi"].dropna() if "oi" in g else []
            if len(f) and f.iloc[0]:
                oi_chg = (float(f.iloc[-1]) / float(f.iloc[0]) - 1) * 100
            if len(g):
                pc = prev.get(g["tradingsymbol"].iloc[-1])
                if pc:
                    px_chg = (float(g["close"].iloc[-1]) / pc - 1) * 100
            if px_chg is not None and oi_chg is not None:
                read = MP.classify_oi(px_chg / 100, oi_chg / 100)
        t = e["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
        return {"symbol": symbol, "ts": iso(t), "available_at": iso(t + self.lag), "source": "stocks_1m via market_pulse",
                "metrics": {"turnover_rs": float(row["turnover"]), "money_flow_net_rs": float(row["net"]),
                            "oi_change_pct": oi_chg, "fut_price_change_pct": px_chg, "oi_read": read},
                "kinds": {"turnover_rs": "observed", "money_flow_net_rs": "estimate",
                          "oi_change_pct": "observed", "fut_price_change_pct": "observed",
                          "oi_read": "derived (price x OI, as on the Pulse page)"}}

    def health(self, as_of):
        day = str(as_of.date())
        m = self._master(as_of)
        futs = [r for r in (m or {}).get("instruments", []) if r.get("kind") == "stock_fut"]
        have = lambda base, part: bool(glob.glob(os.path.join(base, f"date={day}", f"underlying={part}", "*.parquet")))  # noqa: E731
        idx_fut = any(r.get("kind") == "index_fut" for r in (m or {}).get("instruments", []))
        feeds = {
            "benchmark_bars": "ok" if have(self.bars_dir, "INDEX") else "missing",
            "vwap_source_bars": "ok" if have(self.stocks, "IDXFUT") else
                                ("waiting for today's NIFTY futures bars" if idx_fut else
                                 "missing: NIFTY future not in the instrument master"),
            "stock_bars": "ok" if have(self.stocks, "EQ") else "missing",
            "futstk_quotes": "ok" if have(self.stocks, "STKFUT") else "missing",
            "sector_map": "ok" if os.path.exists(os.path.join(self.root, "data", "sectors.json")) else "missing",
            "futstk_master": ("ok" if futs and all(r.get("tick_size") for r in futs)
                              else "incomplete: no tick_size" if futs else "missing"),
            "desk_snapshot": "ok" if have(self.stocks, "EQ") else "missing",
        }
        return {"source": "repo", "session": day, "master": (m or {}).get("date"),
                "stock_futures": len(futs), "feeds": feeds}
