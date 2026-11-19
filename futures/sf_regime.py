"""Market-regime methods for the stock-futures Market Regime agent.

Pure functions over point-in-time bars, so every rule is unit-tested. All
threshold comparisons use Decimal built from the printed value of each number,
so a close of exactly Fib_0618, exactly VWAP or a return of exactly 0.5% is
treated as exactly equal, never nudged across the line by binary float error.

INTRADAY  opening_range_fib_vwap
  Opening range: completed NIFTY 50 bars in [open, open + minutes), e.g.
  [09:15, 09:30). Nothing at or after 09:30 is ever included. Levels, measured
  up from the range low for both directions:
      Fib_038 = low + 0.380 x range, Fib_050 = low + 0.500 x range,
      Fib_0618 = low + 0.618 x range; zone = [Fib_038, Fib_0618], inclusive.
  The agent freezes the levels for the session once they are READY and they
  reset at the next session. Missing, incomplete or zero-width -> no entries.
  VWAP: session-anchored sum(traded value) / sum(volume) of a configured
  source with real traded volume (the NIFTY 50 index has none). If the source
  is a NIFTY futures contract it is a PROXY: its own close is compared with
  its own VWAP; the index close is compared with the index Fibonacci zone.
  Prices are never mixed across instruments, and nothing unweighted is ever
  substituted for VWAP.
      BULLISH   index close > Fib_0618  AND source close > source VWAP
      BEARISH   index close < Fib_038   AND source close < source VWAP
      SIDEWAYS  (reported as NEUTRAL) inside or on the zone, at VWAP, or the two disagree
      readiness NOT_READY while the opening range is forming (no entries)
      UNAVAILABLE  missing, stale or misaligned inputs (no entries, reason kept)
  Confirmation uses completed-bar closes on a configurable interval aligned to
  the session open, so a wick through a level that closes back inside does
  not count.

SWING  return_structure
  t = latest completed session. ret = Close[t] / Close[t-N] - 1 (N = 5 needs
  six completed closes). Higher high: High[t] > High[t-1]; lower low:
  Low[t] < Low[t-1]. BULLISH: ret > +threshold AND higher high. BEARISH:
  ret < -threshold AND lower low. An outside day (both) is therefore resolved
  by the return. Data must cover consecutive exchange sessions ending at the
  expected latest session; any gap, staleness or invalid bar -> UNAVAILABLE.
"""
from datetime import timedelta
from decimal import Decimal

from sf_core import ist, iso, parse_ts


def D(x):
    return Decimal(str(x))


def f(x, nd=4):
    return None if x is None else round(float(x), nd)


def _hhmm_add(t, minutes):
    return t + timedelta(minutes=minutes)


# ====================================================================== intraday

def opening_range(bars, day, open_hhmm, minutes, now, bar_minutes=1, fibs=(0.380, 0.500, 0.618)):
    """Opening-range levels for one session, or a NOT_READY / UNAVAILABLE status."""
    start = ist(day, open_hhmm)
    end = _hhmm_add(start, minutes)
    out = {"start": iso(start), "end": iso(end), "expected_bars": minutes // bar_minutes}
    if now < end:
        return dict(out, status="NOT_READY", reason=f"opening range {start:%H:%M}-{end:%H:%M} still forming")
    inside = [b for b in bars if start <= parse_ts(b["ts"]) < end and parse_ts(b["available_at"]) <= now]
    out["bars"] = len(inside)
    if len(inside) < out["expected_bars"]:
        return dict(out, status="UNAVAILABLE",
                    reason=f"opening range incomplete: {len(inside)}/{out['expected_bars']} completed bars")
    hi = max(D(b["high"]) for b in inside)
    lo = min(D(b["low"]) for b in inside)
    rng = hi - lo
    if rng <= 0:
        return dict(out, status="UNAVAILABLE", reason="opening range has zero width", or_high=f(hi), or_low=f(lo))
    a, m, b_ = (D(x) for x in fibs)
    return dict(out, status="READY", or_high=str(hi), or_low=str(lo), or_range=str(rng),
                fib_038=str(lo + a * rng), fib_050=str(lo + m * rng), fib_0618=str(lo + b_ * rng),
                ratios=[str(a), str(m), str(b_)])


def confirm_close(bars, day, open_hhmm, interval_min, now):
    """Close of the latest COMPLETED confirmation bar (interval aligned to the
    session open): (close, bucket_start, bucket_end, last_1m_bar) or None."""
    start = ist(day, open_hhmm)
    sess = [b for b in bars if parse_ts(b["ts"]) >= start and parse_ts(b["ts"]).date() == day
            and parse_ts(b["available_at"]) <= now]
    if not sess:
        return None
    by = {}
    for b in sess:
        k = int((parse_ts(b["ts"]) - start).total_seconds() // 60 // interval_min)
        by.setdefault(k, []).append(b)
    for k in sorted(by, reverse=True):
        b_end = start + timedelta(minutes=(k + 1) * interval_min)
        last = max(by[k], key=lambda x: parse_ts(x["ts"]))
        if parse_ts(last["available_at"]) >= b_end and len(by[k]) == interval_min:
            return {"close": D(last["close"]), "bucket_start": iso(start + timedelta(minutes=k * interval_min)),
                    "bucket_end": iso(b_end), "bar": last}
    return None


def session_vwap(bars, day, open_hhmm, upto):
    """Session-anchored VWAP from completed bars ending at or before `upto`.
    Uses each bar's traded value (turnover / pv) when present, else close x volume."""
    start = ist(day, open_hhmm)
    num, vol, n = Decimal(0), Decimal(0), 0
    for b in bars:
        t = parse_ts(b["ts"])
        if t < start or t.date() != day or parse_ts(b["available_at"]) > upto:
            continue
        v = D(b.get("volume") or 0)
        if v <= 0:
            continue
        tv = b.get("turnover")
        num += D(tv) if tv else D(b["close"]) * v
        vol += v
        n += 1
    return (num / vol if vol > 0 else None), int(vol), n


def classify_intraday(nifty_bars, vwap_bars, now, rc, cal, max_age_s, frozen=None):
    """Full intraday classification with evidence. `frozen` = levels already
    fixed earlier this session (the agent stores them)."""
    day = now.date()
    open_hhmm = rc["opening_range"].get("start", cal.open)
    minutes = rc["opening_range"].get("minutes", 15)
    interval = rc.get("confirm_interval_min", 1)
    src = rc.get("vwap_source") or {}
    ev = {"method": "opening_range_fib_vwap", "session": str(day), "confirm_interval_min": interval,
          "vwap_source": src, "checks": [], "metrics": {}}

    def done(regime, readiness, reason):
        ev.update(regime=regime, readiness=readiness, reason=reason)
        return ev
    if not cal.is_session(day) or now < ist(day, open_hhmm):
        return done("UNAVAILABLE", "CLOSED", "market not in session")
    lv = frozen or opening_range(nifty_bars, day, open_hhmm, minutes, now,
                                 fibs=tuple(rc.get("fib", {}).get(k, d) for k, d in
                                            (("low", 0.380), ("mid", 0.500), ("high", 0.618))))
    ev["levels"] = {k: (f(v) if k.startswith(("or_", "fib_")) else v) for k, v in lv.items()}
    ev["levels"]["frozen"] = bool(frozen)
    if lv["status"] == "NOT_READY":
        return done("UNAVAILABLE", "NOT_READY", lv["reason"])
    if lv["status"] != "READY":
        return done("UNAVAILABLE", "READY_FAILED", lv["reason"])
    nc = confirm_close(nifty_bars, day, open_hhmm, interval, now)
    if not nc or parse_ts(nc["bucket_start"]) < parse_ts(lv["end"]):
        return done("UNAVAILABLE", "READY", "no completed NIFTY 50 confirmation bar after the opening range")
    age = (now - parse_ts(nc["bar"]["available_at"])).total_seconds()
    if age > max_age_s:
        return done("UNAVAILABLE", "READY", f"NIFTY 50 data stale: last completed bar {nc['bar']['ts']} ({int(age)}s old)")
    if not src.get("symbol"):
        return done("UNAVAILABLE", "READY", "no VWAP source configured")
    vc = confirm_close(vwap_bars, day, open_hhmm, interval, now)
    if not vc:
        return done("UNAVAILABLE", "READY", f"no completed bars for VWAP source {src['symbol']}")
    if vc["bucket_end"] != nc["bucket_end"]:
        return done("UNAVAILABLE", "READY", f"VWAP source bar ({vc['bucket_end']}) is not aligned with the "
                    f"NIFTY 50 bar ({nc['bucket_end']})")
    vwap, vol, nbars = session_vwap(vwap_bars, day, open_hhmm, parse_ts(vc["bucket_end"]))
    if vwap is None:
        return done("UNAVAILABLE", "READY", f"VWAP source {src['symbol']} has no traded volume this session")
    f38, f50, f618 = D(lv["fib_038"]), D(lv["fib_050"]), D(lv["fib_0618"])
    c = nc["close"]
    fib = "ABOVE" if c > f618 else "BELOW" if c < f38 else "INSIDE"
    edge = " (on the zone edge)" if c in (f38, f618) else ""
    vw = "ABOVE" if vc["close"] > vwap else "BELOW" if vc["close"] < vwap else "AT"
    ev["metrics"] = {"or_high": f(lv["or_high"]), "or_low": f(lv["or_low"]), "fib_038": f(f38),
                     "fib_050": f(f50), "fib_0618": f(f618), "nifty_close": f(c),
                     "vwap_source_close": f(vc["close"]), "vwap": f(vwap, 4), "vwap_volume": vol}
    ev["confirm_bar"] = {"start": nc["bucket_start"], "end": nc["bucket_end"]}
    ev["checks"] = [
        {"name": "NIFTY 50 close vs Fibonacci zone", "result": fib,
         "detail": f"close {f(c, 2)} vs zone [{f(f38, 2)}, {f(f618, 2)}]{edge}"},
        {"name": f"{src.get('label') or src['symbol']} close vs session VWAP", "result": vw,
         "detail": f"close {f(vc['close'], 2)} vs VWAP {f(vwap, 2)} over {nbars} bars"
                   + (" (proxy)" if src.get("kind") == "proxy" else "")}]
    if fib == "ABOVE" and vw == "ABOVE":
        return done("BULLISH", "READY", "NIFTY 50 closed above Fib_0618 and the VWAP source closed above VWAP")
    if fib == "BELOW" and vw == "BELOW":
        return done("BEARISH", "READY", "NIFTY 50 closed below Fib_038 and the VWAP source closed below VWAP")
    why = ("close inside the Fibonacci zone" + edge if fib == "INSIDE" else
           "VWAP source closed exactly at VWAP" if vw == "AT" else
           f"checks disagree (Fibonacci {fib}, VWAP {vw})")
    return done("SIDEWAYS", "READY", "NEUTRAL: " + why)


# ====================================================================== swing

def classify_swing(daily, now, rc, cal, expected_last):
    n = rc.get("lookback_sessions", 5)
    th = D(rc.get("threshold", 0.005))
    ev = {"method": "return_structure", "lookback_sessions": n, "threshold": str(th), "checks": [], "metrics": {}}

    def done(regime, reason):
        ev.update(regime=regime, readiness="READY" if regime != "UNAVAILABLE" else "UNAVAILABLE", reason=reason)
        return ev
    bars = [b for b in daily if parse_ts(b["available_at"]) <= now]
    if len(bars) < n + 1:
        return done("UNAVAILABLE", f"need {n + 1} completed daily bars, have {len(bars)}")
    win = bars[-(n + 1):]
    dates = [parse_ts(b["ts"]).date() for b in win]
    if dates[-1] != expected_last:
        return done("UNAVAILABLE", f"latest completed bar is {dates[-1]}, expected {expected_last} (stale)")
    want = [expected_last]
    for _ in range(n):
        want.insert(0, cal.prev_session(want[0]))
    if dates != want:
        missing = sorted(set(want) - set(dates))
        return done("UNAVAILABLE", f"gap in expected sessions: missing {', '.join(map(str, missing)) or 'out-of-order bars'}")
    for b in win[-2:] + win[:1]:
        vals = [b.get(k) for k in ("open", "high", "low", "close")]
        if any(v is None or v != v or v <= 0 for v in vals) or b["high"] < b["low"]:
            return done("UNAVAILABLE", f"invalid bar on {b['ts'][:10]}")
    t, p, base = win[-1], win[-2], win[0]
    ret = D(t["close"]) / D(base["close"]) - 1
    hh = D(t["high"]) > D(p["high"])
    ll = D(t["low"]) < D(p["low"])
    ev["metrics"] = {"return_pct": f(ret * 100, 4), "baseline_close": base["close"], "baseline_session": str(dates[0]),
                     "latest_close": t["close"], "latest_session": str(dates[-1]), "latest_high": t["high"],
                     "latest_low": t["low"], "prior_high": p["high"], "prior_low": p["low"]}
    ev["checks"] = [
        {"name": f"{n}-session return", "result": "UP" if ret > th else "DOWN" if ret < -th else "FLAT",
         "detail": f"{f(ret * 100, 4)}% vs ±{f(th * 100, 4)}% (strict)"},
        {"name": "Higher high", "result": "YES" if hh else "NO", "detail": f"{t['high']} > {p['high']}"},
        {"name": "Lower low", "result": "YES" if ll else "NO", "detail": f"{t['low']} < {p['low']}"}]
    outside = " (outside day: resolved by the return)" if hh and ll else ""
    if ret > th and hh:
        return done("BULLISH", f"return {f(ret * 100, 3)}% > {f(th * 100, 3)}% and a higher high{outside}")
    if ret < -th and ll:
        return done("BEARISH", f"return {f(ret * 100, 3)}% < -{f(th * 100, 3)}% and a lower low{outside}")
    why = []
    if ret > th and not hh:
        why.append("return passed but no higher high")
    elif ret < -th and not ll:
        why.append("return passed but no lower low")
    else:
        why.append(f"return {f(ret * 100, 3)}% within ±{f(th * 100, 3)}%")
    return done("SIDEWAYS", "; ".join(why) + outside)
