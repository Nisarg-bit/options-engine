"""Versioned strategy definitions (schema sf-strategy/1) and the ONE evaluator used
by the visual builder, research candidates, replay, shadow and active paper trading.

A strategy is plain JSON:

  {"schema": "sf-strategy/1", "name", "description", "tags": [],
   "mode": "intraday" | "swing", "directions": ["LONG", "SHORT"],
   "gates": {"market": {...regime method + params...},          LOCKED by default
             "sector": {...sector qualification + ranking...},   LOCKED by default
             "verification": {...Nifty Desk checks...}},
   "universe": {"scope": "fno", "sectors": "qualifying"},
   "timeframe": "1m" | "5m" | "15m" | "1d",
   "entry": {"LONG": <node>, "SHORT": <node>},
   "ranking": {"LONG": {"ind", "params", "order": "desc"|"asc"}, "SHORT": {...}},
   "top_n": 5,
   "exits": {"stop": {"basis": "opening_range_edge"|"pct"|"atr", "value"},
             "target": {"basis": "r_multiple"|"pct"|"atr", "value"},
             "trailing": null | {"basis": "pct", "value"},
             "time_exit": "15:15", "max_holding_sessions": 5,
             "hard_max_stop_pct": 2.5, "pre_expiry_buffer_sessions": 1},
   "timing": {"entry_window": ["09:45", "14:30"], "scan_cadence_s": 300,
              "candidate_ttl_s": 240, "max_data_age_s": 180},
   "contract": {...}}

<node> = {"all": [<node>...]} | {"any": [<node>...]} | <condition>
<condition> = {"left": <operand>, "op": ">"|">="|"<"|"<="|"cross_above"|"cross_below",
               "right": <operand>}
<operand> = {"ind": "<registry id>", "params": {...}, "offset": 0, "mult": 1.0}
          | {"value": <number>}

Semantics (the same everywhere):
  * Only COMPLETED bars: the evaluator is given bars whose available_at <= now.
    Higher timeframes are built from complete buckets aligned to the session open
    (a partly formed bucket is dropped).
  * A comparison with a missing value is UNAVAILABLE, never PASS. all: FAIL if
    any FAIL, else UNAVAILABLE if any UNAVAILABLE, else PASS. any: PASS if any
    PASS, else UNAVAILABLE if any UNAVAILABLE, else FAIL.
  * cross_above(a, b) at bar t: a[t-1] <= b[t-1] and a[t] > b[t] (strict now,
    weak before); cross_below mirrors it. Needs both bars, else UNAVAILABLE.
  * Comparisons are on floats rounded to 10 decimals (no binary-error flips).
  * Equity bars drive screening; the futures contract is resolved and priced
    separately by the entry agent (prices are never mixed).
"""
import copy
import hashlib
import json
from datetime import datetime, timedelta

import sf_indicators as I

SCHEMA = "sf-strategy/1"
MODES = ("intraday", "swing")
DIRS = ("LONG", "SHORT")
OPS = (">", ">=", "<", "<=", "cross_above", "cross_below")
TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "1d": None}
STOP_BASES = ("opening_range_edge", "pct", "atr")
TARGET_BASES = ("r_multiple", "pct", "atr")
OPEN_AREAS = ("entry", "ranking", "top_n", "timeframe", "exits", "timing", "universe")


# ------------------------------------------------------------------ identity

def canonical(s):
    return json.dumps(s, sort_keys=True, separators=(",", ":"))


def version_hash(s):
    """Immutable identity of a strategy version (descriptive fields included)."""
    return hashlib.sha256(canonical(s).encode()).hexdigest()[:16]


def structure_hash(s):
    """Rule structure + parameters only: two strategies with the same logic and
    different names/descriptions/tags/provenance deduplicate to one."""
    core = {k: s.get(k) for k in ("mode", "directions", "gates", "universe", "timeframe", "entry",
                                  "ranking", "top_n", "exits", "timing", "contract")}
    return hashlib.sha256(canonical(core).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ validation

def _walk(node, fn, path="entry"):
    if not isinstance(node, dict):
        fn(None, path)
        return
    if "all" in node or "any" in node:
        k = "all" if "all" in node else "any"
        kids = node[k]
        if not isinstance(kids, list) or not kids:
            fn({"_bad": f"{path}.{k} must be a non-empty list"}, path)
            return
        for i, ch in enumerate(kids):
            _walk(ch, fn, f"{path}.{k}[{i}]")
        return
    fn(node, path)


def operands(node):
    out = []

    def f(c, path):
        if c and "_bad" not in c:
            for side in ("left", "right"):
                o = c.get(side)
                if isinstance(o, dict) and "ind" in o:
                    out.append(o)
    _walk(node, f)
    return out


def validate(s, available=None, policy=None, active=None):
    """Full validation for the builder, research and activation.

    available  set of DATA keys the runtime has (None = assume all)
    policy     research policy (locks); active = the profile's active strategy,
               against which locked areas are compared
    Returns {"ok", "errors": [...], "warnings": [...], "data": {...},
             "warmup_bars", "lock_changes": [...]}"""
    errs, warns = [], []
    if s.get("schema") != SCHEMA:
        errs.append(f"schema must be '{SCHEMA}'")
    if s.get("mode") not in MODES:
        errs.append("mode must be intraday or swing")
    dirs = s.get("directions") or []
    if not dirs or any(d not in DIRS for d in dirs):
        errs.append("directions must be a non-empty subset of LONG, SHORT")
    tf = s.get("timeframe")
    if tf not in TIMEFRAMES:
        errs.append(f"timeframe must be one of {list(TIMEFRAMES)}")
    if s.get("mode") == "swing" and tf not in (None, "1d"):
        errs.append("swing strategies use completed daily bars (timeframe 1d)")
    if s.get("mode") == "intraday" and tf == "1d":
        errs.append("intraday strategies need an intraday timeframe")
    needs, warm = set(), 0
    for d in dirs:
        node = (s.get("entry") or {}).get(d)
        if not node:
            errs.append(f"entry.{d}: no conditions (a direction without rules cannot trade)")
            continue

        def chk(c, path, d=d):
            if c is None:
                errs.append(f"{path}: not a condition or group")
                return
            if "_bad" in c:
                errs.append(c["_bad"])
                return
            if c.get("op") not in OPS:
                errs.append(f"{path}: operator must be one of {OPS}")
            for side in ("left", "right"):
                o = c.get(side)
                if not isinstance(o, dict) or not ("ind" in o or "value" in o):
                    errs.append(f"{path}.{side}: needs an indicator or a value")
                    continue
                if "value" in o and not isinstance(o["value"], (int, float)):
                    errs.append(f"{path}.{side}: value must be a number")
                if "ind" in o:
                    p = I.validate_params(o["ind"], o.get("params"))
                    errs.extend(f"{path}.{side}: {x}" for x in p)
                    if o["ind"] in I.REGISTRY:
                        r = I.REGISTRY[o["ind"]]
                        needs.update(r["needs"])
                        if r.get("intraday_only") and s.get("mode") == "swing":
                            errs.append(f"{path}.{side}: {o['ind']} needs intraday bars")
                        nonlocal_warm[0] = max(nonlocal_warm[0], r["warmup"](I.params_with_defaults(o["ind"], o.get("params")))
                                               + int(o.get("offset") or 0) + (1 if c.get("op", "").startswith("cross") else 0))
            if c.get("op", "").startswith("cross") and all("value" in (c.get(x) or {}) for x in ("left", "right")):
                errs.append(f"{path}: a crossover between two constants can never happen")
        nonlocal_warm = [warm]
        _walk(node, chk, f"entry.{d}")
        warm = nonlocal_warm[0]
        rk = (s.get("ranking") or {}).get(d)
        if rk:
            errs += [f"ranking.{d}: {x}" for x in I.validate_params(rk.get("ind"), rk.get("params"))]
            if rk.get("order") not in ("asc", "desc"):
                errs.append(f"ranking.{d}.order must be asc or desc")
            if rk.get("ind") in I.REGISTRY:
                needs.update(I.REGISTRY[rk["ind"]]["needs"])
    ex = s.get("exits") or {}
    st = (ex.get("stop") or {})
    if st.get("basis") not in STOP_BASES:
        errs.append(f"exits.stop.basis must be one of {STOP_BASES}")
    elif st["basis"] != "opening_range_edge" and not (isinstance(st.get("value"), (int, float)) and st["value"] > 0):
        errs.append("exits.stop.value must be positive")
    if st.get("basis") == "opening_range_edge" and s.get("mode") == "swing":
        errs.append("exits.stop: the opening-range edge needs intraday bars")
    tg = (ex.get("target") or {})
    if tg and tg.get("basis") not in TARGET_BASES:
        errs.append(f"exits.target.basis must be one of {TARGET_BASES}")
    tr = ex.get("trailing")
    if tr and (tr.get("basis") != "pct" or not (isinstance(tr.get("value"), (int, float)) and 0 < tr["value"] < 50)):
        errs.append("exits.trailing must be {basis: pct, value: 0-50}")
    if not isinstance(ex.get("hard_max_stop_pct"), (int, float)) or ex["hard_max_stop_pct"] <= 0:
        errs.append("exits.hard_max_stop_pct is required (a positive % cap on the stop distance)")
    if s.get("mode") == "intraday" and not ex.get("time_exit"):
        errs.append("exits.time_exit is required for intraday (flat by the deadline)")
    if s.get("mode") == "swing" and not ex.get("max_holding_sessions"):
        errs.append("exits.max_holding_sessions is required for swing")
    tm = s.get("timing") or {}
    w = tm.get("entry_window")
    if not (isinstance(w, list) and len(w) == 2 and w[0] < w[1]):
        errs.append("timing.entry_window must be [start, end] HH:MM")
    if not isinstance(s.get("top_n"), int) or not 1 <= s["top_n"] <= 20:
        errs.append("top_n must be 1-20")
    for g in ("market", "sector", "verification"):
        if not (s.get("gates") or {}).get(g):
            errs.append(f"gates.{g} is missing")
    data = {k: (available is None or k in available) for k in sorted(needs)}
    missing = [k for k, ok in data.items() if not ok]
    if missing:
        errs.append("data not available for: " + ", ".join(f"{k} ({I.DATA.get(k, '')})" for k in missing))
    if tf in ("5m", "15m") and warm * TIMEFRAMES[tf] > 375:
        warns.append(f"warm-up of {warm} {tf} bars exceeds one session: the first signals come on day 2 or later")
    elif tf == "1m" and warm > 375:
        warns.append(f"warm-up of {warm} 1m bars exceeds one session")
    lock_changes = lock_diff(s, active, policy) if (policy and active) else []
    return {"ok": not errs, "errors": errs, "warnings": warns, "data": data, "warmup_bars": warm,
            "lock_changes": lock_changes}


def lock_diff(s, active, policy):
    """Locked areas where `s` differs from the profile's active strategy. A locked
    gate covers its method AND every parameter (a threshold change is a lock change)."""
    out = []
    for area, locked in (policy.get("locks") or {}).items():
        if not locked:
            continue
        a, b = (active.get("gates") or {}).get(area), (s.get("gates") or {}).get(area)
        if canonical(a) != canonical(b):
            out.append({"lock": area, "active": a, "proposed": b, "paths": _diff_paths(a, b, f"gates.{area}")})
    return out


def _diff_paths(a, b, prefix=""):
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff_paths(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else k)
        return out
    return [] if a == b else [{"path": prefix, "from": a, "to": b}]


def diff(a, b):
    """Readable differences between two versions: [{path, from, to}]."""
    return _diff_paths(a, b)


# ------------------------------------------------------------------ evaluation

def _ts(b):
    t = b["ts"]
    return t if isinstance(t, datetime) else datetime.fromisoformat(str(t))


def resample(bars, minutes, session_open="09:15", now=None):
    """1-minute bars -> complete N-minute bars aligned to the session open. A
    bucket counts only when its last minute is present (and available)."""
    if minutes in (None, 1):
        return bars
    out, cur, key = [], None, None
    for b in bars:
        t = _ts(b)
        o = t.replace(hour=int(session_open[:2]), minute=int(session_open[3:]), second=0, microsecond=0)
        k = (t.date(), int((t - o).total_seconds() // 60 // minutes))
        if k != key:
            if cur is not None and cur["_n"] == minutes:
                out.append(cur)
            key = k
            start = o + timedelta(minutes=k[1] * minutes)
            cur = {"ts": start.isoformat(), "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
                   "volume": b.get("volume") or 0, "turnover": b.get("turnover") or 0, "oi": b.get("oi"), "_n": 0,
                   "available_at": b.get("available_at")}
        cur["high"] = max(cur["high"], b["high"])
        cur["low"] = min(cur["low"], b["low"])
        cur["close"] = b["close"]
        cur["volume"] += b.get("volume") or 0
        cur["turnover"] += b.get("turnover") or 0
        cur["oi"] = b.get("oi", cur["oi"])
        cur["available_at"] = b.get("available_at")
        cur["_n"] += 1
    if cur is not None and cur["_n"] == minutes:
        out.append(cur)
    return out


class Series:
    """Indicator values for one instrument's bars, computed once per (ind, params)."""

    def __init__(self, bars, ctx=None):
        self.bars, self.ctx, self._c = bars, ctx or {}, {}

    def get(self, operand):
        if "value" in operand:
            return None
        key = (operand["ind"], canonical(operand.get("params") or {}))
        if key not in self._c:
            self._c[key] = I.compute(operand["ind"], self.bars, operand.get("params"), self.ctx)
        return self._c[key]

    def at(self, operand, i):
        if "value" in operand:
            return float(operand["value"])
        xs = self.get(operand)
        j = i - int(operand.get("offset") or 0)
        if j < 0 or j >= len(xs) or xs[j] is None:
            return None
        return round(xs[j] * float(operand.get("mult", 1.0)), 10)


def eval_node(node, ser, i, trace=None):
    """'PASS' | 'FAIL' | 'UNAVAILABLE' at bar index i; trace collects each condition."""
    if "all" in node or "any" in node:
        k = "all" if "all" in node else "any"
        rs = [eval_node(ch, ser, i, trace) for ch in node[k]]
        if k == "all":
            return "FAIL" if "FAIL" in rs else "UNAVAILABLE" if "UNAVAILABLE" in rs else "PASS"
        return "PASS" if "PASS" in rs else "UNAVAILABLE" if "UNAVAILABLE" in rs else "FAIL"
    op, L, R = node["op"], node["left"], node["right"]
    a, b = ser.at(L, i), ser.at(R, i)
    res, prev = None, None
    if a is None or b is None:
        res = "UNAVAILABLE"
    elif op.startswith("cross"):
        a0, b0 = ser.at(L, i - 1), ser.at(R, i - 1)
        prev = (a0, b0)
        if a0 is None or b0 is None:
            res = "UNAVAILABLE"
        elif op == "cross_above":
            res = "PASS" if a0 <= b0 and a > b else "FAIL"
        else:
            res = "PASS" if a0 >= b0 and a < b else "FAIL"
    else:
        ok = {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
        res = "PASS" if ok else "FAIL"
    if trace is not None:
        trace.append({"text": describe_condition(node), "left": a, "right": b, "previous": prev, "result": res})
    return res


def prepare(strategy, bars, bench=None, session_open="09:15"):
    """Bars at the strategy's timeframe (complete buckets) + a Series."""
    tf = TIMEFRAMES.get(strategy.get("timeframe") or "1m")
    b = resample(bars, tf, session_open) if tf else bars
    bb = resample(bench, tf, session_open) if (bench and tf) else bench
    return b, Series(b, {"benchmark": bb or [], "session_open": session_open})


def evaluate(strategy, direction, bars, bench=None, session_open="09:15"):
    """Evaluate one direction on the LAST completed bar. Returns
    {status, trace, values, bar_ts, rank_value}."""
    b, ser = prepare(strategy, bars, bench, session_open)
    if not b:
        return {"status": "UNAVAILABLE", "reason": "no completed bars at the strategy timeframe", "trace": []}
    i = len(b) - 1
    trace = []
    st = eval_node(strategy["entry"][direction], ser, i, trace)
    rk = (strategy.get("ranking") or {}).get(direction)
    rv = ser.at({"ind": rk["ind"], "params": rk.get("params")}, i) if rk else None
    return {"status": st, "trace": trace, "bar_ts": b[i]["ts"], "close": b[i]["close"], "rank_value": rv,
            "values": {t["text"]: [t["left"], t["right"]] for t in trace}, "series": ser, "bars": b, "index": i}


def markers(strategy, bars, bench=None, session_open="09:15", overlays=True):
    """Every bar where each direction's entry rule passed, plus overlay series for
    chart inspection (builder preview)."""
    b, ser = prepare(strategy, bars, bench, session_open)
    out = {"bars": [{"ts": x["ts"], "o": x["open"], "h": x["high"], "l": x["low"], "c": x["close"]} for x in b],
           "markers": [], "overlays": {}}
    for d in strategy.get("directions") or []:
        node = (strategy.get("entry") or {}).get(d)
        if not node:
            continue
        prev = None
        for i in range(len(b)):
            r = eval_node(node, ser, i)
            if r == "PASS" and prev != "PASS":
                out["markers"].append({"ts": b[i]["ts"], "direction": d, "price": b[i]["close"]})
            prev = r
    if overlays:
        seen = set()
        for o in [o for d in strategy.get("directions") or [] for o in operands((strategy.get("entry") or {}).get(d) or {})]:
            k = o["ind"] + canonical(o.get("params") or {})
            if k in seen or I.REGISTRY[o["ind"]]["group"] in ("momentum", "volume", "flow", "derivatives", "relative"):
                continue
            seen.add(k)
            out["overlays"][describe_operand(o)] = ser.get(o)
    return out


# ------------------------------------------------------------------ plain language

def describe_operand(o):
    if "value" in o:
        v = o["value"]
        return f"{v:,.0f}" if abs(v) >= 1000 else str(v)
    r = I.REGISTRY.get(o["ind"], {"label": o["ind"]})
    ps = I.params_with_defaults(o["ind"], o.get("params")) if o["ind"] in I.REGISTRY else (o.get("params") or {})
    shown = ", ".join(f"{k} {v}" for k, v in ps.items() if k not in ("first_bar",))
    s = r["label"] + (f" ({shown})" if shown else "")
    if o.get("offset"):
        s += f" {o['offset']} bars ago"
    if o.get("mult", 1) != 1:
        s = f"{o['mult']} x {s}"
    return s


WORDS = {">": "is above", ">=": "is at or above", "<": "is below", "<=": "is at or below",
         "cross_above": "crosses above", "cross_below": "crosses below"}


def describe_condition(c):
    return f"{describe_operand(c['left'])} {WORDS.get(c['op'], c['op'])} {describe_operand(c['right'])}"


def describe_node(n, depth=0):
    if "all" in n or "any" in n:
        k = "all" if "all" in n else "any"
        head = "ALL of:" if k == "all" else "ANY of:"
        return "\n".join([("  " * depth) + head] + [describe_node(ch, depth + 1) for ch in n[k]])
    return ("  " * depth) + "- " + describe_condition(n)


def market_text(m):
    if m.get("method") == "opening_range_fib_vwap":
        f = m.get("fib") or {}
        return (f"NIFTY 50's {m.get('opening_range', {}).get('minutes', 15)}-minute opening range; longs only when NIFTY "
                f"closes above the {f.get('high', 0.618)} Fibonacci level AND the "
                f"{(m.get('vwap_source') or {}).get('label', 'VWAP source')} closes above its own session VWAP; shorts "
                f"only below {f.get('low', 0.38)} AND below VWAP; inside the zone or mixed signals: no new entries.")
    if m.get("method") == "return_structure":
        return (f"NIFTY's {m.get('lookback_sessions', 5)}-session return on completed daily bars above "
                f"+{float(m.get('threshold', 0.005)) * 100:.2f}% with a higher high permits longs; below "
                f"-{float(m.get('threshold', 0.005)) * 100:.2f}% with a lower low permits shorts.")
    return m.get("method", "?")


def describe(s):
    ex = s.get("exits") or {}
    lines = [f"{s.get('name', 'Strategy')} ({s.get('mode')}, {', '.join(s.get('directions') or [])}, "
             f"{s.get('timeframe')} completed bars)"]
    g = s.get("gates") or {}
    lines.append(f"Market gate (locked): {market_text(g.get('market') or {})}")
    lines.append(f"Sector gate (locked): only sectors that are up (down) in absolute terms AND beating (lagging) "
                 f"NIFTY, top {(g.get('sector') or {}).get('top_n', '?')}; Nifty Desk verification applies.")
    for d in s.get("directions") or []:
        lines.append(f"{d} entry when the stock's:")
        lines.append(describe_node((s.get("entry") or {}).get(d) or {}, 1))
        rk = (s.get("ranking") or {}).get(d)
        if rk:
            lines.append(f"  ranked by {describe_operand(rk)} ({'highest' if rk['order'] == 'desc' else 'lowest'} first)")
    st, tg, tr = ex.get("stop") or {}, ex.get("target") or {}, ex.get("trailing")
    stop = {"opening_range_edge": "the opening-range edge", "pct": f"{st.get('value')}% from entry",
            "atr": f"{st.get('value')} x ATR"}.get(st.get("basis"), "?")
    tgt = {"r_multiple": f"{tg.get('value')}x the stop distance", "pct": f"{tg.get('value')}% from entry",
           "atr": f"{tg.get('value')} x ATR"}.get(tg.get("basis"), "none")
    lines.append(f"Exit: stop at {stop} (never more than {ex.get('hard_max_stop_pct')}%), target {tgt}"
                 + (f", trailing stop {tr['value']}%" if tr else "")
                 + (f", flat by {ex['time_exit']}" if ex.get("time_exit") else "")
                 + (f", at most {ex['max_holding_sessions']} sessions" if ex.get("max_holding_sessions") else "")
                 + ". One lot of the nearest eligible stock future; shared portfolio limits apply.")
    tm = s.get("timing") or {}
    lines.append(f"Entries {tm.get('entry_window', ['?', '?'])[0]}-{tm.get('entry_window', ['?', '?'])[1]} IST, "
                 f"scanned every {int((tm.get('scan_cadence_s') or 0) / 60)} min, top {s.get('top_n')} candidates.")
    return "\n".join(lines)


# ------------------------------------------------------------------ engine bridge

def compile_config(strategy, base_cfg, ref=None):
    """Engine config (sf_core format) that runs `strategy` through the existing
    agents. Shared portfolio limits, costs and fill model come from `base_cfg`
    and are never taken from the strategy (hard execution constraints)."""
    cfg = copy.deepcopy(base_cfg)
    m = strategy["mode"]
    prof = cfg["profiles"][m]
    g = strategy["gates"]
    prof["regime"] = copy.deepcopy(g["market"])
    prof["sector"] = copy.deepcopy(g["sector"])
    prof["verification"] = copy.deepcopy(g["verification"])
    tm = strategy["timing"]
    prof["entry_window"] = list(tm["entry_window"])
    for k in ("scan_cadence_s", "candidate_ttl_s", "max_data_age_s"):
        if tm.get(k):
            prof[k] = tm[k]
    prof["stock"] = {"method": "conditions", "top_n": strategy["top_n"],
                     "strategy": {k: copy.deepcopy(strategy[k]) for k in
                                  ("mode", "directions", "timeframe", "entry", "ranking", "top_n", "exits")},
                     "min_turnover_rs": prof.get("stock", {}).get("min_turnover_rs", 0)}
    if strategy.get("contract"):
        prof["contract"] = copy.deepcopy(strategy["contract"])
    ex = strategy["exits"]
    plan = {"plan_version": f"{(ref or {}).get('version_id') or version_hash(strategy)}",
            "stop": copy.deepcopy(ex["stop"]), "target": copy.deepcopy(ex.get("target")),
            "trailing": copy.deepcopy(ex.get("trailing")), "hard_max_stop_pct": ex["hard_max_stop_pct"],
            "invalidation": {"regime_change": False}, "missing_quote_policy": "retry_until_quote"}
    if m == "intraday":
        plan.update(time_exit=ex["time_exit"], cutoff_grace_s=600)
        prof["exit_deadline"] = ex["time_exit"]
    else:
        plan.update(max_holding_sessions=ex["max_holding_sessions"], holding_count="sessions_after_entry",
                    session_exit_time=ex.get("time_exit") or "15:15")
        prof.setdefault("contract", {})["pre_expiry_buffer_sessions"] = ex.get("pre_expiry_buffer_sessions", 1)
    prof["exit_plan"] = plan
    prof["paused"] = False
    other = "swing" if m == "intraday" else "intraday"
    cfg["profiles"][other]["paused"] = True            # a strategy runs in its own mode only
    cfg["label"] = f"{strategy.get('name')} [{(ref or {}).get('version_id') or version_hash(strategy)}]"
    cfg["strategy_ref"] = dict(ref or {}, name=strategy.get("name"), mode=m,
                               version_id=(ref or {}).get("version_id") or version_hash(strategy))
    return cfg


# ------------------------------------------------------------------ templates from the deployed config

def _gates(prof):
    return {"market": copy.deepcopy(prof["regime"]), "sector": copy.deepcopy(prof["sector"]),
            "verification": copy.deepcopy(prof["verification"])}


def templates_from_config(cfg):
    """The deployed v1 rules as named strategy templates, plus the differences
    between the template's expression and the deployed implementation (shown,
    never silently changed)."""
    ip, sp = cfg["profiles"]["intraday"], cfg["profiles"]["swing"]
    fb = (ip["stock"].get("opening_range") or {}).get("first_bar", "ohlc")
    lb = (ip["stock"].get("lookback") or {}).get("minutes", 30)
    turn = ip["stock"]["min_turnover_rs"]
    orp = {"minutes": (ip["stock"].get("opening_range") or {}).get("minutes", 15), "first_bar": fb}

    def leg(side):
        up = side == "LONG"
        return {"all": [
            {"left": {"ind": "price", "params": {"field": "close"}}, "op": ">" if up else "<", "right": {"ind": "vwap"}},
            {"left": {"ind": "price", "params": {"field": "close"}}, "op": ">" if up else "<",
             "right": {"ind": "or_high" if up else "or_low", "params": orp}},
            {"left": {"ind": "rel_strength", "params": {"period": lb}}, "op": ">" if up else "<", "right": {"value": 0}},
            {"left": {"ind": "turnover", "params": {"period": lb}}, "op": ">=", "right": {"value": turn}}]}
    intraday = {
        "schema": SCHEMA, "name": "Intraday ORB + VWAP (deployed v1)",
        "description": "NIFTY opening-range Fibonacci + VWAP market gate; direction-aligned sectors; stocks above "
                       "(below) their own VWAP and opening-range high (low), beating (lagging) NIFTY over 30 minutes, "
                       "with turnover; Nifty Desk verification; one lot; opening-range-edge stop, 2R target, flat by 15:15.",
        "tags": ["template", "deployed"], "mode": "intraday", "directions": ["LONG", "SHORT"],
        "gates": _gates(ip), "universe": {"scope": "fno", "sectors": "qualifying"}, "timeframe": "1m",
        "entry": {"LONG": leg("LONG"), "SHORT": leg("SHORT")},
        "ranking": {"LONG": {"ind": "rel_strength", "params": {"period": lb}, "order": "desc"},
                    "SHORT": {"ind": "rel_strength", "params": {"period": lb}, "order": "asc"}},
        "top_n": ip["stock"]["top_n"],
        "exits": {"stop": {"basis": "opening_range_edge", "minutes": orp["minutes"], "first_bar": fb},
                  "target": copy.deepcopy(ip["exit_plan"]["target"]), "trailing": None,
                  "time_exit": ip["exit_plan"]["time_exit"], "hard_max_stop_pct": ip["exit_plan"]["hard_max_stop_pct"]},
        "timing": {"entry_window": ip["entry_window"], "scan_cadence_s": ip["scan_cadence_s"],
                   "candidate_ttl_s": ip["candidate_ttl_s"], "max_data_age_s": ip["max_data_age_s"]},
        "contract": copy.deepcopy(ip["contract"])}
    sl = sp["stock"]

    def sleg(side):
        up = side == "LONG"
        r = sl["long" if up else "short"]["all"]
        ret = next(x[2] for x in r if x[0] == "return_pct")
        n = sl["lookback"]["sessions"]
        return {"all": [
            {"left": {"ind": "roc", "params": {"period": n}}, "op": ">" if up else "<", "right": {"value": ret}},
            {"left": {"ind": "rel_strength", "params": {"period": n}}, "op": ">" if up else "<", "right": {"value": 0}},
            {"left": {"ind": "turnover", "params": {"period": n}}, "op": ">=", "right": {"value": sl["min_turnover_rs"]}}]}
    swing = {
        "schema": SCHEMA, "name": "Swing 5-session momentum (deployed v1)",
        "description": "NIFTY 5-session return beyond +/-0.5% with a higher high (lower low) on completed daily bars; "
                       "direction-aligned sectors over 5 sessions; stocks up (down) more than 1% over 5 sessions and "
                       "beating (lagging) NIFTY, with turnover; verification; one lot; 3% stop, 6% target, 5 sessions, "
                       "out before the pre-expiry deadline.",
        "tags": ["template", "deployed"], "mode": "swing", "directions": ["LONG", "SHORT"],
        "gates": _gates(sp), "universe": {"scope": "fno", "sectors": "qualifying"}, "timeframe": "1d",
        "entry": {"LONG": sleg("LONG"), "SHORT": sleg("SHORT")},
        "ranking": {"LONG": {"ind": "rel_strength", "params": {"period": sl["lookback"]["sessions"]}, "order": "desc"},
                    "SHORT": {"ind": "rel_strength", "params": {"period": sl["lookback"]["sessions"]}, "order": "asc"}},
        "top_n": sl["top_n"],
        "exits": {"stop": copy.deepcopy(sp["exit_plan"]["stop"]), "target": copy.deepcopy(sp["exit_plan"]["target"]),
                  "trailing": None, "max_holding_sessions": sp["exit_plan"]["max_holding_sessions"],
                  "time_exit": sp["exit_plan"].get("session_exit_time", "15:15"),
                  "hard_max_stop_pct": sp["exit_plan"]["hard_max_stop_pct"],
                  "pre_expiry_buffer_sessions": sp["contract"]["pre_expiry_buffer_sessions"]},
        "timing": {"entry_window": sp["entry_window"], "scan_cadence_s": sp["scan_cadence_s"],
                   "candidate_ttl_s": sp["candidate_ttl_s"], "max_data_age_s": sp["max_data_age_s"]},
        "contract": copy.deepcopy(sp["contract"])}
    differences = {
        intraday["name"]: [
            "Return-vs-NIFTY and turnover windows: deployed v1 clips the 30-minute window to the session open "
            "(so a 09:45 scan uses 09:15-09:44); the template's rel_strength/turnover need 30 completed bars, so "
            "the first scan that can pass is 09:46. Same rule from 09:46 on.",
            f"Opening range first bar: deployed v1 uses '{fb}' (audit C1: the 09:15 bar's open is yesterday's close). "
            "The corrected variant is a separate version with first_bar close_only.",
            "Deployed v1 ranks by excess return with ties broken by turnover then symbol; the template ranks by the "
            "same excess return; ties fall back to symbol order."],
        swing["name"]: [
            "Deployed v1 measures the 5-session return from the sector agent's window start; the template uses "
            "ROC(5) on completed daily bars (Close[t]/Close[t-5]-1), the same definition.",
            "Turnover: deployed sums the 5-session window; the template's turnover(period 5) is the same sum."]}
    return [intraday, swing], differences
