"""Indicator registry for strategy definitions (sf_strategy.py).

Every indicator is a pure function of completed bars:

    compute(bars, params, ctx) -> list[float | None]   (one value per bar, aligned)

bars  oldest-first dicts with ts, open, high, low, close, volume, turnover
      (turnover = traded value; VWAP never falls back to close x volume silently:
      a bar without turnover uses close x volume and the indicator says so in
      `conventions`), optionally oi.
ctx   {"benchmark": bars of the benchmark aligned by ts, "session_open": "09:15",
      "first_bar": "ohlc" | "close_only", "prior_session": {high, low, close}}

Conventions (documented once, applied everywhere):
  * None means "not computable yet" (warm-up, missing input). The evaluator
    treats a comparison with None as UNAVAILABLE, which never passes.
  * Moving averages are seeded with the SMA of the first `period` values
    (EMA alpha = 2/(n+1)); RSI/ATR/ADX use Wilder smoothing (alpha = 1/n).
  * Session indicators (VWAP, opening range, relative volume by time) reset at
    each session's first bar; daily bars treat each bar as its own session.
  * Pure Python, no third-party TA library, so values are reproducible and the
    registry can grow without rebuilding the engine: add an entry to REGISTRY.

To add an indicator: write compute(bars, params, ctx), declare params with
defaults and bounds, the inputs it needs (DATA keys), its warm-up and outputs.
"""
import math
from datetime import datetime

# Data a strategy can depend on; sf_strategy checks availability against these.
DATA = {
    "price": "OHLC of the screening instrument (equity bars for stocks)",
    "volume": "traded volume per bar",
    "turnover": "traded value per bar (collector pv)",
    "benchmark": "NIFTY 50 bars aligned to the same timestamps",
    "oi": "open interest of the nearest stock future (STKFUT bars)",
    "prior_session": "previous completed session's high/low/close",
}


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _col(bars, k):
    return [_f(b.get(k)) for b in bars]


def _day(b):
    t = b["ts"]
    return (t if isinstance(t, datetime) else datetime.fromisoformat(str(t))).date()


def sma_list(xs, n):
    out, s, q = [], 0.0, []
    for x in xs:
        if x is None:
            out.append(None)
            continue
        q.append(x)
        s += x
        if len(q) > n:
            s -= q.pop(0)
        out.append(s / n if len(q) == n else None)
    return out


def ema_list(xs, n, alpha=None):
    a = alpha if alpha is not None else 2.0 / (n + 1)
    out, prev, seed = [], None, []
    for x in xs:
        if x is None:
            out.append(prev if prev is not None else None)
            continue
        if prev is None:
            seed.append(x)
            if len(seed) == n:
                prev = sum(seed) / n
            out.append(prev)
            continue
        prev = prev + a * (x - prev)
        out.append(prev)
    return out


def wilder(xs, n):
    return ema_list(xs, n, alpha=1.0 / n)


# ------------------------------------------------------------------ price / averages

def c_price(bars, p, ctx):
    return _col(bars, p.get("field", "close"))


def c_sma(bars, p, ctx):
    return sma_list(_col(bars, p.get("source", "close")), int(p["period"]))


def c_ema(bars, p, ctx):
    return ema_list(_col(bars, p.get("source", "close")), int(p["period"]))


def c_vwap(bars, p, ctx):
    """Session-anchored VWAP: sum(turnover) / sum(volume) from the session's first bar."""
    out, num, vol, day = [], 0.0, 0.0, None
    for b in bars:
        d = _day(b)
        if d != day:
            num, vol, day = 0.0, 0.0, d
        v = _f(b.get("volume")) or 0.0
        tv = _f(b.get("turnover"))
        if v > 0:
            num += tv if tv else (_f(b.get("close")) or 0.0) * v
            vol += v
        out.append(num / vol if vol > 0 else None)
    return out


def c_anchored_vwap(bars, p, ctx):
    """VWAP anchored at a time of day (HH:MM) each session; None before the anchor."""
    anchor = p.get("anchor", "09:15")
    out, num, vol, day, on = [], 0.0, 0.0, None, False
    for b in bars:
        t = b["ts"] if isinstance(b["ts"], datetime) else datetime.fromisoformat(str(b["ts"]))
        if t.date() != day:
            num, vol, day, on = 0.0, 0.0, t.date(), False
        if not on and t.strftime("%H:%M") >= anchor:
            on = True
        if on:
            v = _f(b.get("volume")) or 0.0
            tv = _f(b.get("turnover"))
            if v > 0:
                num += tv if tv else (_f(b.get("close")) or 0.0) * v
                vol += v
        out.append(num / vol if on and vol > 0 else None)
    return out


# ------------------------------------------------------------------ momentum

def c_rsi(bars, p, ctx):
    n = int(p["period"])
    c = _col(bars, "close")
    up, dn = [None], [None]
    for i in range(1, len(c)):
        if c[i] is None or c[i - 1] is None:
            up.append(None)
            dn.append(None)
        else:
            d = c[i] - c[i - 1]
            up.append(max(d, 0.0))
            dn.append(max(-d, 0.0))
    au, ad = wilder(up, n), wilder(dn, n)
    return [None if a is None or b is None else (100.0 if b == 0 else 100 - 100 / (1 + a / b)) for a, b in zip(au, ad)]


def c_macd(bars, p, ctx):
    c = _col(bars, "close")
    f, s = ema_list(c, int(p["fast"])), ema_list(c, int(p["slow"]))
    line = [None if a is None or b is None else a - b for a, b in zip(f, s)]
    sig = ema_list(line, int(p["signal"]))
    out = p.get("output", "line")
    if out == "line":
        return line
    if out == "signal":
        return sig
    return [None if a is None or b is None else a - b for a, b in zip(line, sig)]


def c_stoch(bars, p, ctx):
    n, d = int(p["period"]), int(p.get("smooth", 3))
    h, l, c = _col(bars, "high"), _col(bars, "low"), _col(bars, "close")
    k = []
    for i in range(len(c)):
        if i < n - 1 or any(x is None for x in h[i - n + 1:i + 1] + l[i - n + 1:i + 1]) or c[i] is None:
            k.append(None)
            continue
        hh, ll = max(h[i - n + 1:i + 1]), min(l[i - n + 1:i + 1])
        k.append(50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100)
    return k if p.get("output", "k") == "k" else sma_list(k, d)


def c_roc(bars, p, ctx):
    n = int(p["period"])
    c = _col(bars, "close")
    return [None if i < n or c[i] is None or not c[i - n] else (c[i] / c[i - n] - 1) * 100 for i in range(len(c))]


def c_momentum(bars, p, ctx):
    n = int(p["period"])
    c = _col(bars, "close")
    return [None if i < n or c[i] is None or c[i - n] is None else c[i] - c[i - n] for i in range(len(c))]


# ------------------------------------------------------------------ volatility / range

def _tr(bars):
    h, l, c = _col(bars, "high"), _col(bars, "low"), _col(bars, "close")
    out = []
    for i in range(len(bars)):
        if h[i] is None or l[i] is None:
            out.append(None)
        elif i == 0 or c[i - 1] is None:
            out.append(h[i] - l[i])
        else:
            out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return out


def c_atr(bars, p, ctx):
    return wilder(_tr(bars), int(p["period"]))


def c_bbands(bars, p, ctx):
    n, k = int(p["period"]), float(p.get("stddev", 2.0))
    c = _col(bars, "close")
    mid = sma_list(c, n)
    out = []
    for i in range(len(c)):
        if mid[i] is None:
            out.append(None)
            continue
        w = c[i - n + 1:i + 1]
        sd = math.sqrt(sum((x - mid[i]) ** 2 for x in w) / n)
        o = p.get("output", "middle")
        out.append({"upper": mid[i] + k * sd, "lower": mid[i] - k * sd, "middle": mid[i],
                    "width": (2 * k * sd / mid[i] * 100) if mid[i] else None}[o])
    return out


def c_range(bars, p, ctx):
    n = int(p.get("period", 1))
    h, l = _col(bars, "high"), _col(bars, "low")
    out = []
    for i in range(len(bars)):
        if i < n - 1 or any(x is None for x in h[i - n + 1:i + 1] + l[i - n + 1:i + 1]):
            out.append(None)
        else:
            out.append(max(h[i - n + 1:i + 1]) - min(l[i - n + 1:i + 1]))
    return out


def c_adx(bars, p, ctx):
    n = int(p["period"])
    h, l = _col(bars, "high"), _col(bars, "low")
    pdm, mdm = [None], [None]
    for i in range(1, len(bars)):
        if None in (h[i], h[i - 1], l[i], l[i - 1]):
            pdm.append(None)
            mdm.append(None)
            continue
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    atr, sp, sm = wilder(_tr(bars), n), wilder(pdm, n), wilder(mdm, n)
    pdi = [None if a in (None, 0) or b is None else 100 * b / a for a, b in zip(atr, sp)]
    mdi = [None if a in (None, 0) or b is None else 100 * b / a for a, b in zip(atr, sm)]
    o = p.get("output", "adx")
    if o == "plus_di":
        return pdi
    if o == "minus_di":
        return mdi
    dx = [None if a is None or b is None or a + b == 0 else 100 * abs(a - b) / (a + b) for a, b in zip(pdi, mdi)]
    return wilder(dx, n)


def c_supertrend(bars, p, ctx):
    """Supertrend line (ATR bands, multiplier m). Output 'line' or 'direction' (+1 up / -1 down)."""
    n, m = int(p["period"]), float(p.get("multiplier", 3.0))
    atr = c_atr(bars, {"period": n}, ctx)
    h, l, c = _col(bars, "high"), _col(bars, "low"), _col(bars, "close")
    line, dirs, fu, fl, d = [], [], None, None, 1
    for i in range(len(bars)):
        if atr[i] is None or None in (h[i], l[i], c[i]):
            line.append(None)
            dirs.append(None)
            continue
        mid = (h[i] + l[i]) / 2
        bu, bl = mid + m * atr[i], mid - m * atr[i]
        pc = c[i - 1] if i else None
        fu = bu if fu is None or bu < fu or (pc is not None and pc > fu) else fu
        fl = bl if fl is None or bl > fl or (pc is not None and pc < fl) else fl
        if d == 1 and c[i] < fl:
            d = -1
        elif d == -1 and c[i] > fu:
            d = 1
        line.append(fl if d == 1 else fu)
        dirs.append(float(d))
    return dirs if p.get("output", "line") == "direction" else line


# ------------------------------------------------------------------ volume / flow

def c_volume(bars, p, ctx):
    return _col(bars, "volume")


def c_turnover(bars, p, ctx):
    n = int(p.get("period", 1))
    t = _col(bars, "turnover")
    return [None if i < n - 1 or any(x is None for x in t[i - n + 1:i + 1]) else sum(t[i - n + 1:i + 1])
            for i in range(len(t))]


def c_rel_volume(bars, p, ctx):
    """Volume / SMA(volume, period) of the preceding bars (current bar excluded)."""
    n = int(p["period"])
    v = _col(bars, "volume")
    out = []
    for i in range(len(v)):
        w = v[max(0, i - n):i]
        if len(w) < n or any(x is None for x in w) or v[i] is None or sum(w) == 0:
            out.append(None)
        else:
            out.append(v[i] / (sum(w) / n))
    return out


def c_obv(bars, p, ctx):
    c, v = _col(bars, "close"), _col(bars, "volume")
    out, s = [], 0.0
    for i in range(len(c)):
        if i and None not in (c[i], c[i - 1], v[i]):
            s += v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0.0
        out.append(s if i else 0.0)
    return out


def c_oi(bars, p, ctx):
    return _col(bars, "oi")


def c_oi_change_pct(bars, p, ctx):
    """OI change % since the session's first bar with OI."""
    out, base, day = [], None, None
    for b in bars:
        d = _day(b)
        if d != day:
            base, day = None, d
        oi = _f(b.get("oi"))
        if base is None and oi:
            base = oi
        out.append(None if not base or oi is None else (oi / base - 1) * 100)
    return out


def c_money_flow(bars, p, ctx):
    """Estimated net flow (Rs): turnover of up-close bars minus down-close bars, session to date.
    An ESTIMATE from price direction x traded value (as on the Pulse page), not actual flows."""
    out, s, day, prev = [], 0.0, None, None
    for b in bars:
        d = _day(b)
        if d != day:
            s, day = 0.0, d
        c, tv = _f(b.get("close")), _f(b.get("turnover")) or 0.0
        if prev is not None and c is not None:
            s += tv if c > prev else -tv if c < prev else 0.0
        prev = c
        out.append(s)
    return out


# ------------------------------------------------------------------ session structure

def _or_levels(bars, p, ctx):
    """Per bar: (or_high, or_low) of its session's opening range, None until complete.
    first_bar 'close_only' drops the first bar's chained open/high/low (audit C1)."""
    from datetime import timedelta
    mins = int(p.get("minutes", 15))
    start = p.get("start", ctx.get("session_open", "09:15"))
    fb = p.get("first_bar", ctx.get("first_bar", "close_only"))
    out, day, hi, lo, n, first, done = [], None, None, None, 0, True, False
    for b in bars:
        t = b["ts"] if isinstance(b["ts"], datetime) else datetime.fromisoformat(str(b["ts"]))
        if t.date() != day:
            day, hi, lo, n, first, done = t.date(), None, None, 0, True, False
            s = t.replace(hour=int(start[:2]), minute=int(start[3:]), second=0, microsecond=0)
            end = s + timedelta(minutes=mins)
        if not done and s <= t < end:
            h, l = _f(b.get("high")), _f(b.get("low"))
            if first and fb == "close_only" and t == s:
                h = l = _f(b.get("close"))
            first = False
            if h is not None and l is not None:
                hi = h if hi is None else max(hi, h)
                lo = l if lo is None else min(lo, l)
                n += 1
        if t >= end:
            done = True
        ok = done and n >= mins and hi is not None and hi > lo
        out.append((hi, lo) if ok else (None, None))
    return out


def c_or_high(bars, p, ctx):
    return [h for h, _ in _or_levels(bars, p, ctx)]


def c_or_low(bars, p, ctx):
    return [l for _, l in _or_levels(bars, p, ctx)]


def c_fib(bars, p, ctx):
    """Fibonacci level measured up from the opening-range low: low + ratio x range."""
    r = float(p.get("ratio", 0.618))
    return [None if h is None else l + r * (h - l) for h, l in _or_levels(bars, p, ctx)]


def c_prior(bars, p, ctx):
    """Previous completed session's high / low / close (from these bars, or ctx)."""
    field = p.get("field", "high")
    out, day, cur, prev = [], None, None, (ctx.get("prior_session") or None)
    for b in bars:
        d = _day(b)
        if d != day:
            if cur is not None:
                prev = cur
            day, cur = d, {"high": _f(b.get("high")), "low": _f(b.get("low")), "close": _f(b.get("close"))}
        else:
            for k, fn in (("high", max), ("low", min)):
                v = _f(b.get(k))
                if v is not None:
                    cur[k] = v if cur[k] is None else fn(cur[k], v)
            cur["close"] = _f(b.get("close"))
        out.append(None if not prev else _f(prev.get(field)))
    return out


def c_rel_strength(bars, p, ctx):
    """Return over `period` bars minus the benchmark's over the same bars (percentage points)."""
    n = int(p["period"])
    c = _col(bars, "close")
    bench = ctx.get("benchmark") or []
    bmap = {str(b["ts"]): _f(b.get("close")) for b in bench}
    out = []
    for i in range(len(bars)):
        if i < n or c[i] is None or not c[i - n]:
            out.append(None)
            continue
        b1, b0 = bmap.get(str(bars[i]["ts"])), bmap.get(str(bars[i - n]["ts"]))
        if not b1 or not b0:
            out.append(None)
            continue
        out.append(((c[i] / c[i - n]) - (b1 / b0)) * 100)
    return out


def _p(default, lo=None, hi=None, kind="int", choices=None):
    d = {"default": default, "type": kind}
    if lo is not None:
        d["min"] = lo
    if hi is not None:
        d["max"] = hi
    if choices:
        d["choices"] = choices
    return d


FIELD = _p("close", kind="choice", choices=["open", "high", "low", "close"])

REGISTRY = {
    "price": {"label": "Price", "group": "price", "fn": c_price, "params": {"field": FIELD}, "needs": ["price"],
              "warmup": lambda p: 1},
    "sma": {"label": "SMA", "group": "trend", "fn": c_sma, "params": {"period": _p(20, 2, 400), "source": FIELD},
            "needs": ["price"], "warmup": lambda p: int(p["period"])},
    "ema": {"label": "EMA", "group": "trend", "fn": c_ema, "params": {"period": _p(20, 2, 400), "source": FIELD},
            "needs": ["price"], "warmup": lambda p: int(p["period"])},
    "vwap": {"label": "Session VWAP", "group": "trend", "fn": c_vwap, "params": {}, "needs": ["price", "volume", "turnover"],
             "warmup": lambda p: 1, "conventions": "sum(traded value)/sum(volume) from the session's first bar"},
    "anchored_vwap": {"label": "Anchored VWAP", "group": "trend", "fn": c_anchored_vwap,
                      "params": {"anchor": _p("09:15", kind="time")}, "needs": ["price", "volume", "turnover"],
                      "warmup": lambda p: 1, "intraday_only": True},
    "rsi": {"label": "RSI", "group": "momentum", "fn": c_rsi, "params": {"period": _p(14, 2, 100)}, "needs": ["price"],
            "warmup": lambda p: int(p["period"]) + 1},
    "macd": {"label": "MACD", "group": "momentum", "fn": c_macd,
             "params": {"fast": _p(12, 2, 100), "slow": _p(26, 3, 200), "signal": _p(9, 2, 100),
                        "output": _p("line", kind="choice", choices=["line", "signal", "histogram"])},
             "needs": ["price"], "warmup": lambda p: int(p["slow"]) + int(p["signal"])},
    "stoch": {"label": "Stochastic", "group": "momentum", "fn": c_stoch,
              "params": {"period": _p(14, 2, 100), "smooth": _p(3, 1, 20),
                         "output": _p("k", kind="choice", choices=["k", "d"])},
              "needs": ["price"], "warmup": lambda p: int(p["period"]) + int(p.get("smooth", 3))},
    "roc": {"label": "Rate of change %", "group": "momentum", "fn": c_roc, "params": {"period": _p(10, 1, 200)},
            "needs": ["price"], "warmup": lambda p: int(p["period"]) + 1},
    "momentum": {"label": "Momentum", "group": "momentum", "fn": c_momentum, "params": {"period": _p(10, 1, 200)},
                 "needs": ["price"], "warmup": lambda p: int(p["period"]) + 1},
    "atr": {"label": "ATR", "group": "volatility", "fn": c_atr, "params": {"period": _p(14, 2, 100)}, "needs": ["price"],
            "warmup": lambda p: int(p["period"]) + 1},
    "bbands": {"label": "Bollinger Bands", "group": "volatility", "fn": c_bbands,
               "params": {"period": _p(20, 2, 200), "stddev": _p(2.0, 0.5, 5.0, "float"),
                          "output": _p("middle", kind="choice", choices=["upper", "middle", "lower", "width"])},
               "needs": ["price"], "warmup": lambda p: int(p["period"])},
    "range": {"label": "High-low range", "group": "volatility", "fn": c_range, "params": {"period": _p(1, 1, 200)},
              "needs": ["price"], "warmup": lambda p: int(p.get("period", 1))},
    "adx": {"label": "ADX / DMI", "group": "trend", "fn": c_adx,
            "params": {"period": _p(14, 2, 100), "output": _p("adx", kind="choice", choices=["adx", "plus_di", "minus_di"])},
            "needs": ["price"], "warmup": lambda p: 2 * int(p["period"]) + 1},
    "supertrend": {"label": "Supertrend", "group": "trend", "fn": c_supertrend,
                   "params": {"period": _p(10, 2, 100), "multiplier": _p(3.0, 0.5, 10.0, "float"),
                              "output": _p("line", kind="choice", choices=["line", "direction"])},
                   "needs": ["price"], "warmup": lambda p: int(p["period"]) + 1},
    "volume": {"label": "Volume", "group": "volume", "fn": c_volume, "params": {}, "needs": ["volume"], "warmup": lambda p: 1},
    "rel_volume": {"label": "Relative volume", "group": "volume", "fn": c_rel_volume, "params": {"period": _p(20, 2, 400)},
                   "needs": ["volume"], "warmup": lambda p: int(p["period"]) + 1},
    "obv": {"label": "On-balance volume", "group": "volume", "fn": c_obv, "params": {}, "needs": ["price", "volume"],
            "warmup": lambda p: 2},
    "turnover": {"label": "Turnover (Rs)", "group": "volume", "fn": c_turnover, "params": {"period": _p(1, 1, 400)},
                 "needs": ["turnover"], "warmup": lambda p: int(p.get("period", 1))},
    "or_high": {"label": "Opening-range high", "group": "session", "fn": c_or_high,
                "params": {"minutes": _p(15, 5, 120), "first_bar": _p("close_only", kind="choice", choices=["close_only", "ohlc"])},
                "needs": ["price"], "warmup": lambda p: int(p.get("minutes", 15)), "intraday_only": True},
    "or_low": {"label": "Opening-range low", "group": "session", "fn": c_or_low,
               "params": {"minutes": _p(15, 5, 120), "first_bar": _p("close_only", kind="choice", choices=["close_only", "ohlc"])},
               "needs": ["price"], "warmup": lambda p: int(p.get("minutes", 15)), "intraday_only": True},
    "fib": {"label": "Fibonacci level (opening range)", "group": "session", "fn": c_fib,
            "params": {"ratio": _p(0.618, 0.0, 1.0, "float"), "minutes": _p(15, 5, 120),
                       "first_bar": _p("close_only", kind="choice", choices=["close_only", "ohlc"])},
            "needs": ["price"], "warmup": lambda p: int(p.get("minutes", 15)), "intraday_only": True},
    "prior": {"label": "Prior session high/low/close", "group": "session", "fn": c_prior,
              "params": {"field": _p("high", kind="choice", choices=["high", "low", "close"])},
              "needs": ["price", "prior_session"], "warmup": lambda p: 1},
    "rel_strength": {"label": "Return vs NIFTY (pp)", "group": "relative", "fn": c_rel_strength,
                     "params": {"period": _p(30, 1, 400)}, "needs": ["price", "benchmark"],
                     "warmup": lambda p: int(p["period"]) + 1},
    "oi": {"label": "Open interest", "group": "derivatives", "fn": c_oi, "params": {}, "needs": ["oi"], "warmup": lambda p: 1},
    "oi_change_pct": {"label": "OI change % (session)", "group": "derivatives", "fn": c_oi_change_pct, "params": {},
                      "needs": ["oi"], "warmup": lambda p: 1},
    "money_flow": {"label": "Net money flow estimate (Rs)", "group": "flow", "fn": c_money_flow, "params": {},
                   "needs": ["price", "turnover"], "warmup": lambda p: 2,
                   "conventions": "estimate: up-close bar turnover minus down-close bar turnover, session to date"},
}


def params_with_defaults(ind, params):
    spec = REGISTRY[ind]["params"]
    out = {k: v["default"] for k, v in spec.items()}
    out.update(params or {})
    return out


def validate_params(ind, params):
    """[] or a list of human-readable problems."""
    if ind not in REGISTRY:
        return [f"unknown indicator '{ind}'"]
    probs = []
    spec = REGISTRY[ind]["params"]
    for k, v in (params or {}).items():
        if k not in spec:
            probs.append(f"{ind}: unknown parameter '{k}'")
            continue
        s = spec[k]
        if s["type"] == "choice" and v not in s["choices"]:
            probs.append(f"{ind}.{k}: '{v}' not one of {s['choices']}")
        elif s["type"] in ("int", "float"):
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                probs.append(f"{ind}.{k}: must be a number")
            elif ("min" in s and v < s["min"]) or ("max" in s and v > s["max"]):
                probs.append(f"{ind}.{k}: {v} outside [{s.get('min')}, {s.get('max')}]")
            elif s["type"] == "int" and int(v) != v:
                probs.append(f"{ind}.{k}: must be a whole number")
    return probs


def compute(ind, bars, params=None, ctx=None):
    r = REGISTRY[ind]
    return r["fn"](bars, params_with_defaults(ind, params), ctx or {})


def catalog():
    """For the builder: id, label, group, params, data needs, warm-up formula text."""
    return [{"id": k, "label": v["label"], "group": v["group"], "params": v["params"], "needs": v["needs"],
             "intraday_only": bool(v.get("intraday_only")), "conventions": v.get("conventions")}
            for k, v in REGISTRY.items()]
