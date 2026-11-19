"""Fixture scenarios for the stock-futures demo, replay and acceptance tests.

SYNTHETIC DATA. Prices, sectors, contracts and quotes here are invented to
exercise the workflow; they are not market data and say nothing about any
security. Lot sizes and expiries are shaped like NSE FUTSTK contracts only so
the arithmetic is realistic.

A scenario is a set of price paths:
  day 0      each symbol ramps from its previous close by `moves[sym]` % over
             the first 90 minutes, then holds
  day k > 0  each symbol opens with a gap of `days[k][sym]` % on the previous
             close (then holds), which is how the Swing gap tests are built
  history    completed daily closes before day 0 move by `hist[sym]` % over
             five sessions, for the Swing (completed daily bar) rules
Futures quotes are the spot path x (1 + basis), one quote per minute with a
two-tick spread and five lots on each side.
"""
import copy
from datetime import date, datetime, timedelta

from sf_core import IST, Calendar, BUY, SELL, ist, iso, round_to_tick, load_config

BENCH = "NIFTY 50"
NIFTY_FUT = "NIFTY FUT"            # VWAP proxy symbol, as in config intraday.regime.vwap_source
NIFTY_FUT_BASIS = 0.004
SECTORS = {
    "IT": ["INFY", "TCS", "WIPRO", "HCLTECH"],
    "Bank": ["HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK"],
    "Metal": ["TATASTEEL", "HINDALCO", "JSWSTEEL"],
    "Pharma": ["SUNPHARMA", "CIPLA", "DRREDDY"],
}
AMBIGUOUS = "MYSTERYCO"           # in the F&O universe, no resolvable sector
BASE = {"INFY": 1500, "TCS": 3400, "WIPRO": 250, "HCLTECH": 1600, "HDFCBANK": 1700, "ICICIBANK": 1300,
        "AXISBANK": 1150, "KOTAKBANK": 1800, "TATASTEEL": 150, "HINDALCO": 650, "JSWSTEEL": 950,
        "SUNPHARMA": 1700, "CIPLA": 1500, "DRREDDY": 1250, AMBIGUOUS: 400, BENCH: 24000}
LOT = {"INFY": 400, "TCS": 175, "WIPRO": 3000, "HCLTECH": 350, "HDFCBANK": 550, "ICICIBANK": 700,
       "AXISBANK": 625, "KOTAKBANK": 400, "TATASTEEL": 5500, "HINDALCO": 1400, "JSWSTEEL": 675,
       "SUNPHARMA": 350, "CIPLA": 375, "DRREDDY": 625, AMBIGUOUS: 1000}
TICK = 0.05
EXPIRIES = {"NOV": ("2026-11-24", 0.002), "DEC": ("2026-12-29", 0.006)}


def oi_read(price_chg_pct, oi_chg_pct):
    """The same four readings the Pulse page uses (quant/market_pulse.classify_oi)."""
    import market_pulse
    return market_pulse.classify_oi(price_chg_pct / 100, oi_chg_pct / 100)


def symbols():
    return [s for v in SECTORS.values() for s in v] + [AMBIGUOUS]


def iid(sym, tag):
    return f"{sym}-FUT-{tag}"


class Scenario:
    def __init__(self, day0, moves, bench_move, days=None, hist=None, bench_hist=0.0, cal=None,
                 oi_change=3.0, flow_sign=None, ramp_minutes=90, bench_path=None):
        self.cal = cal or Calendar()
        self.day0 = day0
        self.moves = {s: moves.get(s, 0.0) for s in symbols()}
        self.moves[BENCH] = bench_move
        self.extra = days or {}            # {k: {sym: gap_pct}}
        self.hist = dict(hist or {})
        self.hist[BENCH] = bench_hist
        self.ndays = 1 + max(self.extra or {0: None})
        self.sessions = [day0]
        for _ in range(self.ndays - 1):
            self.sessions.append(self.cal.next_session(self.sessions[-1]))
        self.oi_change = oi_change
        self.flow_sign = flow_sign
        self.ramp = ramp_minutes
        self.bench_path = bench_path       # optional f(minutes_since_open) -> % move, day 0 only
        self.data = None

    # ------------------------------------------------------------ paths
    def spot(self, sym, t):
        d = t.date()
        if d < self.day0:
            return BASE[sym]
        k = max(i for i, s in enumerate(self.sessions) if s <= d)
        open_t = ist(self.sessions[k], "09:15")
        if k == 0 and sym == BENCH and self.bench_path:
            return BASE[sym] * (1 + self.bench_path((t - open_t).total_seconds() / 60) / 100)
        if k == 0:
            frac = min(max((t - open_t).total_seconds() / 60 / self.ramp, 0), 1)
            return BASE[sym] * (1 + self.moves[sym] / 100 * frac)
        lvl = BASE[sym] * (1 + self.moves[sym] / 100)
        for j in range(1, k + 1):
            lvl *= 1 + self.extra.get(j, {}).get(sym, 0.0) / 100
        return lvl

    # ------------------------------------------------------------ build
    def build(self):
        d = {"universe": symbols(), "sectors": {}, "bars": {}, "contracts": [], "quotes": {}, "desk": {}}
        for sec, syms in SECTORS.items():
            for s in syms:
                d["sectors"][s] = {"sector": sec, "ambiguous": False, "source": "fixture"}
        d["sectors"][AMBIGUOUS] = {"sector": None, "ambiguous": True, "source": "fixture",
                                   "candidates": ["Cap goods", "Infra"]}
        hist_days = [self.cal.prev_session(self.day0, j) for j in range(10, 0, -1)]
        for sym in symbols() + [BENCH]:
            daily = []
            for j, hd in enumerate(hist_days):
                back = len(hist_days) - 1 - j                    # 0 = previous session
                c = BASE[sym] * (1 - self.hist.get(sym, 0.0) / 100 * back / 5)
                daily.append({"ts": iso(ist(hd, "09:15")), "available_at": iso(ist(hd, "15:31")),
                              "open": c, "high": c * 1.005, "low": c * 0.995, "close": c,
                              "volume": 1_000_000, "turnover": c * 1_000_000})
            minute = []
            for sd in self.sessions:
                t = ist(sd, "09:15")
                end = ist(sd, "15:30")
                while t < end:
                    o, cl = self.spot(sym, t), self.spot(sym, t + timedelta(minutes=1))
                    vol = int(5_000_000 / cl)
                    minute.append({"ts": iso(t), "available_at": iso(t + timedelta(minutes=1)), "open": o,
                                   "high": max(o, cl), "low": min(o, cl), "close": cl, "volume": vol,
                                   "turnover": cl * vol})
                    t += timedelta(minutes=1)
                # each completed session also becomes a daily bar
                day_bars = [b for b in minute if b["ts"][:10] == str(sd)]
                daily.append({"ts": iso(ist(sd, "09:15")), "available_at": iso(ist(sd, "15:31")),
                              "open": day_bars[0]["open"], "high": max(b["high"] for b in day_bars),
                              "low": min(b["low"] for b in day_bars), "close": day_bars[-1]["close"],
                              "volume": sum(b["volume"] for b in day_bars),
                              "turnover": sum(b["turnover"] for b in day_bars)})
            d["bars"][sym] = {"1m": minute, "1d": daily}
            if sym == BENCH:      # NIFTY near-month futures: the VWAP proxy (the index has no volume)
                fut = []
                for b in minute:
                    o, cl = b["open"] * (1 + NIFTY_FUT_BASIS), b["close"] * (1 + NIFTY_FUT_BASIS)
                    vol = 75 * 400
                    fut.append({"ts": b["ts"], "available_at": b["available_at"], "open": o, "high": max(o, cl),
                                "low": min(o, cl), "close": cl, "volume": vol, "turnover": (o + cl) / 2 * vol})
                d["bars"][NIFTY_FUT] = {"1m": fut}
        listed = iso(ist(hist_days[0], "08:00"))
        for sym in symbols():
            for tag, (exp, basis) in EXPIRIES.items():
                i = iid(sym, tag)
                d["contracts"].append({"instrument_id": i, "tradingsymbol": f"{sym}26{tag}FUT",
                                       "underlying": sym, "instrument_type": "FUTSTK", "exchange": "NFO",
                                       "expiry": exp, "lot_size": LOT[sym], "tick_size": TICK,
                                       "status": "listed", "ts": listed, "available_at": listed})
                qs, fb = [], []
                for sd in self.sessions:
                    t = ist(sd, "09:15")
                    while t <= ist(sd, "15:30"):
                        mid = self.spot(sym, t) * (1 + basis)
                        qs.append({"instrument_id": i, "ts": iso(t), "available_at": iso(t),
                                   "bid": round_to_tick(mid - TICK, TICK, SELL),
                                   "ask": round_to_tick(mid + TICK, TICK, BUY),
                                   "bid_qty": 5 * LOT[sym], "ask_qty": 5 * LOT[sym], "last": mid,
                                   "source": "fixture"})
                        if t < ist(sd, "15:30"):
                            m2 = self.spot(sym, t + timedelta(minutes=1)) * (1 + basis)
                            fb.append({"ts": iso(t), "available_at": iso(t + timedelta(minutes=1)), "open": mid,
                                       "high": max(mid, m2), "low": min(mid, m2), "close": m2})
                        t += timedelta(minutes=1)
                d["quotes"][i] = qs
                d["bars"]["FUT:" + i] = {"1m": fb}
            snaps = []
            for sd in self.sessions:
                t = ist(sd, "09:20")
                n = 0
                while t <= ist(sd, "15:30"):
                    n += 5
                    trend = self.moves[sym] or self.hist.get(sym, 0.0)
                    sign = self.flow_sign.get(sym, 1) if isinstance(self.flow_sign, dict) else \
                        (1 if trend >= 0 else -1)
                    snaps.append({"symbol": sym, "ts": iso(t), "available_at": iso(t), "source": "fixture desk",
                                  "metrics": {"turnover_rs": n * 5_000_000.0,
                                              "money_flow_net_rs": sign * n * 1_500_000.0,
                                              "oi_change_pct": self.oi_change, "fut_price_change_pct": trend,
                                              "oi_read": oi_read(trend, self.oi_change),
                                              "order_block_dist_pct": 1.2},
                                  "kinds": {"turnover_rs": "observed", "money_flow_net_rs": "estimate",
                                            "oi_change_pct": "observed", "fut_price_change_pct": "observed",
                                            "oi_read": "derived (price x OI)", "order_block_dist_pct": "pattern"}})
                    t += timedelta(minutes=5)
            d["desk"][sym] = snaps
        self.data = d
        return d

    # ------------------------------------------------------------ edits
    def drop_quotes(self, instrument_id, start, end):
        q = self.data["quotes"][instrument_id]
        self.data["quotes"][instrument_id] = [x for x in q if not (start <= datetime.fromisoformat(x["ts"]) <= end)]

    def edit_quotes(self, instrument_id, start, end, fn):
        for x in self.data["quotes"][instrument_id]:
            if start <= datetime.fromisoformat(x["ts"]) <= end:
                fn(x)


# ---------------------------------------------------------------- presets

INTRADAY_DAY = date(2026, 11, 17)       # Tuesday
SWING_DAY = date(2026, 11, 20)          # Friday; Mon 23 trades, Tue 24 is an exchange holiday


def bullish(day=INTRADAY_DAY, **kw):
    """NIFTY up; IT strongest (INFY leads); Pharma up but lagging NIFTY; Metal down."""
    return Scenario(day, {"INFY": 2.0, "TCS": 1.7, "WIPRO": 1.45, "HCLTECH": 1.55,
                          "HDFCBANK": 0.95, "ICICIBANK": 1.05, "AXISBANK": 0.8, "KOTAKBANK": 0.9,
                          "TATASTEEL": -0.6, "HINDALCO": -0.5, "JSWSTEEL": -0.55,
                          "SUNPHARMA": 0.25, "CIPLA": 0.2, "DRREDDY": 0.3, AMBIGUOUS: 3.6},
                    bench_move=1.2, **kw)


def bearish(day=INTRADAY_DAY, **kw):
    """NIFTY down; Metal weakest (TATASTEEL leads lower); Pharma down less than NIFTY
    (negative absolute but outperforming -> fails the short rule); IT up."""
    return Scenario(day, {"INFY": 0.25, "TCS": 0.15, "WIPRO": 0.2, "HCLTECH": 0.1,
                          "HDFCBANK": -0.7, "ICICIBANK": -0.65, "AXISBANK": -0.8, "KOTAKBANK": -0.7,
                          "TATASTEEL": -2.4, "HINDALCO": -1.9, "JSWSTEEL": -2.05,
                          "SUNPHARMA": -0.15, "CIPLA": -0.1, "DRREDDY": -0.2, AMBIGUOUS: -3.6},
                    bench_move=-1.4, **kw)


def _sideways_path(m):
    """NIFTY rises 0.30% in the opening range, then settles at +0.15% (inside the
    0.38-0.618 Fibonacci zone of that range) for the rest of the day."""
    if m <= 15:
        return 0.30 * m / 15
    return max(0.15, 0.30 - 0.15 * (m - 15) / 15)


def sideways(day=INTRADAY_DAY, **kw):
    kw.setdefault("bench_path", _sideways_path)
    return Scenario(day, {s: 0.1 for s in symbols()}, bench_move=0.05, **kw)


def swing_bullish(**kw):
    hist = {"INFY": 6.0, "TCS": 5.0, "WIPRO": 4.5, "HCLTECH": 4.8, "HDFCBANK": 2.5, "ICICIBANK": 2.6,
            "AXISBANK": 2.2, "KOTAKBANK": 2.4, "TATASTEEL": -2.0, "HINDALCO": -1.5, "JSWSTEEL": -1.8,
            "SUNPHARMA": 0.8, "CIPLA": 0.9, "DRREDDY": 0.7, AMBIGUOUS: 9.0}
    kw.setdefault("days", {1: {"INFY": 2.0, "TCS": 0.3}, 2: {"INFY": 2.5, "TCS": 0.2}, 3: {"INFY": 2.0},
                           4: {"TCS": -0.2}, 5: {"TCS": 0.1}})
    return Scenario(SWING_DAY, {s: 0.0 for s in symbols()}, bench_move=0.0, hist=hist, bench_hist=2.0, **kw)


def swing_bearish(**kw):
    hist = {"INFY": 0.5, "TCS": 0.6, "WIPRO": 0.4, "HCLTECH": 0.5, "HDFCBANK": -2.0, "ICICIBANK": -1.8,
            "AXISBANK": -2.2, "KOTAKBANK": -2.1, "TATASTEEL": -7.0, "HINDALCO": -5.5, "JSWSTEEL": -6.0,
            "SUNPHARMA": -0.5, "CIPLA": -0.6, "DRREDDY": -0.4, AMBIGUOUS: -9.0}
    # TATASTEEL gaps UP through a short's stop on the first session after entry.
    kw.setdefault("days", {1: {"TATASTEEL": 4.0, "JSWSTEEL": -1.0}, 2: {"JSWSTEEL": -2.0},
                           3: {"JSWSTEEL": -3.5}, 4: {}, 5: {}})
    return Scenario(SWING_DAY, {s: 0.0 for s in symbols()}, bench_move=0.0, hist=hist, bench_hist=-2.0, **kw)


def demo_config(**overrides):
    """The default config with the demo label kept; tests tweak copies of it."""
    cfg = load_config()
    from sf_core import deep_merge
    return deep_merge(copy.deepcopy(cfg), overrides) if overrides else cfg
