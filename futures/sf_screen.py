"""Intraday stock screening: opening-range breakout confirmed by VWAP and
NIFTY-relative strength (config intraday.stock.method = "opening_range_breakout_vwap").

For each stock in a sector that already qualifies for the permitted direction:
  opening range  completed bars in [09:15, 09:30): opening high / low, frozen
                 for the session by the agent; incomplete or zero width -> UNAVAILABLE
  breakout bar   the latest completed bar that STARTS at or after 09:30
  VWAP           the stock's session VWAP from 1-minute traded value / volume
                 through that same bar (repo: the collector's tick price x
                 volume aggregated per minute, so a traded-value VWAP rather
                 than an OHLC approximation)
  returns        stock and NIFTY 50 over the same configured window, same start
                 and same end bar; excess = stock - NIFTY

  LONG   close > VWAP  AND close > opening high AND return > NIFTY return AND turnover >= minimum
  SHORT  close < VWAP  AND close < opening low  AND return < NIFTY return AND turnover >= minimum

Every comparison is strict and exact (Decimal), so a close exactly at VWAP or
exactly on the range edge fails. Only completed closes count: a wick does not.
Rank: longs by excess return descending, shorts ascending (largest
underperformer first); ties by higher turnover, then symbol.
"""
from datetime import timedelta
from decimal import Decimal

import sf_regime as R
from sf_core import LONG, ist, iso, parse_ts

D, f = R.D, R.f


def window_return(bars, start_at, end_ts):
    """Return over [start_at, end bar]: from the close of the last bar at or
    before start_at to the close of the bar starting at end_ts."""
    end = next((b for b in bars if b["ts"] == end_ts), None)
    if end is None:
        return None
    before = [b for b in bars if parse_ts(b["ts"]) <= start_at and parse_ts(b["ts"]).date() == start_at.date()]
    base = before[-1] if before else None
    if base is None:
        return None
    turnover = sum(D(b.get("turnover") or 0) for b in bars
                   if parse_ts(base["ts"]) < parse_ts(b["ts"]) <= parse_ts(end_ts))
    return {"return": D(end["close"]) / D(base["close"]) - 1, "start_ts": base["ts"], "end_ts": end["ts"],
            "turnover": turnover}


def screen_one(sym, bars, bench_bars, direction, now, sk, cal, max_age_s, frozen=None):
    """Assess one stock. Returns a dict with status PASS / FAIL / UNAVAILABLE."""
    day = now.date()
    orc = sk.get("opening_range", {"start": cal.open, "minutes": 15})
    open_hhmm, minutes = orc.get("start", cal.open), orc.get("minutes", 15)
    out = {"symbol": sym, "direction": direction, "checks": [], "metrics": {}}

    def stop(status, reason):
        out.update(status=status, reason=reason)
        return out
    lv = frozen or R.opening_range(bars, day, open_hhmm, minutes, now)
    out["opening_range"] = {k: lv.get(k) for k in ("status", "start", "end", "bars", "or_high", "or_low")}
    if lv["status"] != "READY":
        return stop("UNAVAILABLE", f"opening range: {lv['reason']}")
    hi, lo = D(lv["or_high"]), D(lv["or_low"])
    nc = R.confirm_close(bars, day, open_hhmm, 1, now)
    if not nc or parse_ts(nc["bucket_start"]) < parse_ts(lv["end"]):
        return stop("UNAVAILABLE", "no completed bar after the opening range yet")
    bar = nc["bar"]
    age = (now - parse_ts(bar["available_at"])).total_seconds()
    if age > max_age_s:
        return stop("UNAVAILABLE", f"stale: last completed bar {bar['ts']} is {int(age)}s old")
    vwap, vol, nbars = R.session_vwap(bars, day, open_hhmm, parse_ts(bar["available_at"]))
    if vwap is None:
        return stop("UNAVAILABLE", "no traded volume for the session VWAP")
    lb = sk["lookback"].get("minutes", 30)
    start_at = parse_ts(bar["ts"]) - timedelta(minutes=lb)
    if start_at < ist(day, open_hhmm):
        start_at = ist(day, open_hhmm)
    sr = window_return(bars, start_at, bar["ts"])
    br = window_return(bench_bars, start_at, bar["ts"])
    if sr is None:
        return stop("UNAVAILABLE", "stock window return unavailable")
    if br is None:
        return stop("UNAVAILABLE", f"NIFTY 50 bar for {bar['ts'][11:16]} missing: windows not aligned")
    if br["start_ts"] != sr["start_ts"]:
        return stop("UNAVAILABLE", f"window start misaligned (stock {sr['start_ts']}, NIFTY {br['start_ts']})")
    c = D(bar["close"])
    excess = sr["return"] - br["return"]
    long_ = direction == LONG
    edge = hi if long_ else lo
    dist = (c / edge - 1) if long_ else (edge / c - 1)
    out["metrics"] = {"close": f(c, 2), "vwap": f(vwap, 2), "or_high": f(hi, 2), "or_low": f(lo, 2),
                      "breakout_distance_pct": f(dist * 100, 3), "return_pct": f(sr["return"] * 100, 4),
                      "nifty_return_pct": f(br["return"] * 100, 4), "excess_return_pct": f(excess * 100, 4),
                      "turnover_rs": f(sr["turnover"], 0)}
    out["inputs"] = {"bar_ts": bar["ts"], "bar_available_at": bar["available_at"], "window_start": sr["start_ts"],
                     "window_end": sr["end_ts"], "vwap_bars": nbars, "vwap_basis": sk.get("vwap_basis", "1-minute traded value / volume")}
    vw_ok = c > vwap if long_ else c < vwap
    br_ok = c > hi if long_ else c < lo
    rel_ok = sr["return"] > br["return"] if long_ else sr["return"] < br["return"]
    to_ok = sr["turnover"] >= D(sk["min_turnover_rs"])
    word = "above" if long_ else "below"
    out["checks"] = [
        {"name": f"close {word} VWAP", "result": "PASS" if vw_ok else "FAIL", "detail": f"{f(c, 2)} vs {f(vwap, 2)}"},
        {"name": f"close {word} opening {'high' if long_ else 'low'}", "result": "PASS" if br_ok else "FAIL",
         "detail": f"{f(c, 2)} vs {f(edge, 2)}"},
        {"name": f"return {'>' if long_ else '<'} NIFTY", "result": "PASS" if rel_ok else "FAIL",
         "detail": f"{f(sr['return'] * 100, 3)}% vs {f(br['return'] * 100, 3)}%"},
        {"name": "minimum turnover", "result": "PASS" if to_ok else "FAIL",
         "detail": f"Rs {f(sr['turnover'], 0):,.0f} vs Rs {sk['min_turnover_rs']:,.0f}"}]
    failed = [ch["name"] for ch in out["checks"] if ch["result"] == "FAIL"]
    if failed:
        return stop("FAIL", "failed: " + ", ".join(failed))
    out["score"] = f(excess * 100 if long_ else -excess * 100, 4)
    return stop("PASS", "all four checks passed")


def rank(passed, direction):
    """Longs: excess desc; shorts: excess asc. Ties: turnover desc, then symbol."""
    sign = -1 if direction == LONG else 1
    passed.sort(key=lambda x: (sign * x["metrics"]["excess_return_pct"], -x["metrics"]["turnover_rs"], x["symbol"]))
    for i, x in enumerate(passed):
        nxt = passed[i + 1] if i + 1 < len(passed) else None
        x["rank_reason"] = (f"excess return {x['metrics']['excess_return_pct']:+.3f}% vs NIFTY"
                            + (f", ahead of {nxt['symbol']} ({nxt['metrics']['excess_return_pct']:+.3f}%)" if nxt else "")
                            + (" (tie broken by turnover)" if nxt and nxt["metrics"]["excess_return_pct"] ==
                               x["metrics"]["excess_return_pct"] else ""))
    return passed
