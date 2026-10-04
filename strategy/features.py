"""Feature store: one row per decision, everything we might later learn from.

WHY THIS EXISTS

Every morning the signal job computes the market's state -- VIX, sigma, the
straddle, credit over sigma -- uses it once to pick a structure, and throws it
away. chain_metrics, indicators and chain_buildup compute a dozen more
readings the same way. The journal keeps the outcome but only a handful of
the inputs. So the question "did this trade lose because vol was cheap, or
because an event was in the window, or because skew was stretched?" can
never be asked of past decisions, because the answers were never kept.

This file keeps them. One row per decision in data/features/decisions.csv,
keyed by `decision_id`, the same key the outcome store (step 6) will join on.

RULES

* It runs AFTER the journal and the message. A feature failure can never cost
  a signal or a journal row -- the caller wraps it, and inside, every feature
  group is computed on its own: a broken input blanks its own columns and is
  named in `missing`, never crashes the row, never invents a value.
* Idempotent. Re-recording the same decision_id replaces the row.
* The signal job itself stays network-free. Daily NIFTY/VIX history for the
  realised-vol and trend features comes from a cache (index_daily.csv),
  seeded once from Kite historical (`--seed`) and refreshed for the last ten
  days by record() -- which runs after the signal has gone out, so that
  network call can never cost a signal.
* KITE IS AUTHORITATIVE for the daily open/high/low. Our own bars are not:
  on 24 Sep 2026 the 09:15 index bar opened at the PREVIOUS close (the first
  tick carries it) with a bad low of 22,540 against a real 23,046, and on 23
  and 25 Sep the collector started after noon, so "the day's first bar" was
  a 12:36 price. Every gap and range computed from them was wrong. Our bars
  supply only close and VIX, and only for a day Kite has not published yet.
* Half-recorded sessions are marked (`session_bars`, `partial_session`)
  rather than silently treated as whole ones.

VERSION HISTORY
  1  2026-09-26  first cut
  2  2026-09-26  Kite-first daily OHLC; oi_tilt over total OI (v1 hit +-1 on
                 any opposite-sign change); session_bars / partial_session
  3  2026-09-26  a morning put back from Kite (recover_morning.py, tick_count
                 0, no bid/ask) is named in `missing` -- it counts toward
                 session_bars but is not the same data as a collected minute
  4  2026-09-26  v3 detected recovered bars by tick_count == 0 -- but the
                 collector also writes 0 on the minutes it fills in for a
                 strike that did not trade, so EVERY session was reported as
                 recovered. Recovered bars are now identified by the file they
                 came from (part-kite-*), which the collector never writes.

Usage:
    python features.py --seed        pull ~400 days of NIFTY + VIX daily into the cache
    python features.py --backfill    rebuild rows for every journalled signal day
    python features.py --show [N]    print the last N rows
"""

import project_paths
import glob
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import chain_metrics as CM
import indicators as I

ROOT = project_paths.ROOT
PATH = os.path.join(ROOT, "data", "features", "decisions.csv")
DAILY = os.path.join(ROOT, "data", "features", "index_daily.csv")
EVENTS = os.path.join(ROOT, "data", "events.json")
BARS = os.path.join(ROOT, "data", "bars_1m")

FEATURE_VERSION = 4
FULL_SESSION = 375          # 1-minute bars in 09:15-15:30
PARTIAL_BELOW = 370         # a few missing minutes is noise, a missing morning is not
NIFTY_TOKEN = 256265
VIX_TOKEN = 264969
IST = timezone(timedelta(hours=5, minutes=30))

FIELDS = [
    # --- identity
    "decision_id", "kind", "decided_at", "session", "expiry", "dte", "dow",
    "underlying", "feature_version",
    # --- vol level
    "spot", "forward", "vix", "vix_rank_252", "vix_chg_5d", "sigma",
    "straddle", "c_over_sigma", "ratio",
    # --- realised (annualised %, comparable with VIX)
    "rv5", "rv10", "rv20", "rv5_over_rv20", "vrp",
    # --- term structure
    "next_expiry", "straddle_next", "term_slope",
    # --- skew
    "wing_skew",
    # --- positioning
    "pcr_oi", "pcr_volume", "pcr_oi_change", "max_pain", "max_pain_dist_sigma",
    "oi_support", "oi_resistance", "support_dist_sigma", "resistance_dist_sigma",
    "oi_tilt",
    # --- trend (distances in one-day sigmas)
    "ema20_dist", "ema50_dist", "rsi14", "ret_5d", "prev_gap_sigma",
    "prev_range_sigma",
    # --- calendar
    "event_in_window", "events_in_window", "days_to_next_event",
    "expiry_week",
    # --- health
    "session_bars", "partial_session", "missing",
]

DAILY_FIELDS = ["date", "open", "high", "low", "close", "vix"]


# --------------------------------------------------------------- helpers

def _f(x, nd=4):
    """Round a number, pass None/NaN through as None."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def _d(x):
    if x is None or isinstance(x, date) and not isinstance(x, datetime):
        return x
    return pd.to_datetime(x).date()


def decision_id(kind, day, expiry, at=None):
    parts = [kind, str(day), str(expiry)]
    if at:
        parts.append(str(at))
    return ":".join(parts)


def atm_straddle(chain, forward):
    """(strike, straddle) at the strike nearest the forward."""
    if not chain:
        return None, None
    r = min(chain, key=lambda r: abs(float(r["strike"]) - forward))
    return float(r["strike"]), float(r["ce"]) + float(r["pe"])


def nearest(chain, level):
    return min(chain, key=lambda r: abs(float(r["strike"]) - level))


# ------------------------------------------------------------ features
# Each group takes plain inputs and returns a dict. Pure, so each is tested
# on its own and one failing cannot touch another.

def vol_level(ctx, daily):
    vix = ctx.get("vix")
    out = {"vix_rank_252": None, "vix_chg_5d": None}
    hist = daily[daily["date"] <= ctx["session"]]["vix"].dropna().tolist()
    if vix is not None and len(hist) >= 60:
        window = hist[-252:]
        out["vix_rank_252"] = sum(1 for v in window if v <= vix) / len(window)
    if vix is not None and len(hist) >= 6:
        out["vix_chg_5d"] = vix - hist[-6]
    return out


def realised(closes, vix):
    """RV over 5/10/20 sessions, zero-mean, annualised on 252 sessions, in %.

    Zero drift, matching realised_vol.py: the model being tested has no
    drift, so fitting one here would flatter it.
    """
    rets = [math.log(b / a) for a, b in zip(closes[:-1], closes[1:])
            if a and b and a > 0 and b > 0]

    def rv(n):
        if len(rets) < n:
            return None
        xs = rets[-n:]
        return math.sqrt(sum(x * x for x in xs) / n) * math.sqrt(252) * 100

    r5, r10, r20 = rv(5), rv(10), rv(20)
    return {"rv5": r5, "rv10": r10, "rv20": r20,
            "rv5_over_rv20": (r5 / r20) if r5 is not None and r20 else None,
            "vrp": (vix - r20) if vix is not None and r20 is not None else None}


def term(near_chain, next_chain, forward, dte_near, dte_next):
    """Straddle per root-day, next expiry against near. >0 = upward sloping."""
    _, s1 = atm_straddle(near_chain, forward)
    _, s2 = atm_straddle(next_chain, forward)
    if not s1 or not s2 or dte_next is None:
        return {"straddle_next": s2, "term_slope": None}
    a = s1 / math.sqrt(max(dte_near, 1))
    b = s2 / math.sqrt(max(dte_next, 1))
    return {"straddle_next": s2, "term_slope": b / a - 1.0}


def skew(chain, forward, sigma, straddle):
    """(put one sigma below - call one sigma above) / straddle.

    Model-free: no IV solve, so no solver error to mistake for skew. Positive
    is the normal equity shape -- downside protection costs more.
    """
    if not chain or not sigma or not straddle:
        return {"wing_skew": None}
    put = float(nearest(chain, forward - sigma)["pe"])
    call = float(nearest(chain, forward + sigma)["ce"])
    return {"wing_skew": (put - call) / straddle}


def positioning(rows, forward, sigma):
    """rows: chain_metrics format [{strike, CE:{oi,volume,oi_chg}, PE:{...}}]."""
    p = CM.pcr(rows)
    lv = CM.levels(rows)
    mp, _ = CM.max_pain(rows)
    # Net OI added on puts minus calls, over the WHOLE book. v1 divided by
    # |pe_add| + |ce_add|, which is +-1 whenever the two move in opposite
    # directions however small the moves -- a sign, not a size.
    ce_add, pe_add = p.get("ce_oi_added"), p.get("pe_oi_added")
    total = (p.get("ce_oi") or 0) + (p.get("pe_oi") or 0)
    tilt = None
    if ce_add is not None and pe_add is not None and total > 0:
        tilt = (pe_add - ce_add) / total

    def dist(k):
        return (k - forward) / sigma if k is not None and sigma else None

    return {"pcr_oi": p.get("pcr_oi"), "pcr_volume": p.get("pcr_volume"),
            "pcr_oi_change": p.get("pcr_oi_change"),
            "max_pain": mp, "max_pain_dist_sigma": dist(mp),
            "oi_support": lv.get("support"), "oi_resistance": lv.get("resistance"),
            "support_dist_sigma": dist(lv.get("support")),
            "resistance_dist_sigma": dist(lv.get("resistance")),
            "oi_tilt": tilt}


def trend(daily, session, vix):
    """Trend readings from daily closes, distances in ONE-DAY sigmas."""
    d = daily[daily["date"] <= session]
    closes = d["close"].tolist()
    out = {"ema20_dist": None, "ema50_dist": None, "rsi14": None,
           "ret_5d": None, "prev_gap_sigma": None, "prev_range_sigma": None}
    if not closes or vix is None:
        return out
    last = closes[-1]
    s1 = last * vix / 100 * math.sqrt(1 / 365)
    e20, e50 = I.ema(closes, 20)[-1], I.ema(closes, 50)[-1]
    if e20 is not None:
        out["ema20_dist"] = (last - e20) / s1
    if e50 is not None:
        out["ema50_dist"] = (last - e50) / s1
    out["rsi14"] = I.rsi(closes, 14)[-1]
    if len(closes) >= 6:
        out["ret_5d"] = last / closes[-6] - 1
    row = d.iloc[-1]
    if len(d) >= 2 and pd.notna(row["open"]):
        out["prev_gap_sigma"] = (row["open"] - d.iloc[-2]["close"]) / s1
    if pd.notna(row["high"]) and pd.notna(row["low"]):
        out["prev_range_sigma"] = (row["high"] - row["low"]) / s1
    return out


def calendar(today, expiry, events):
    """High-impact scheduled events from today up to and including expiry."""
    hi = sorted(_d(e["date"]) for e in events
                if e.get("impact") == "high" and e.get("kind") != "holiday")
    window = [d for d in hi if today <= d <= expiry]
    ahead = [d for d in hi if d >= today]
    return {"event_in_window": bool(window), "events_in_window": len(window),
            "days_to_next_event": (ahead[0] - today).days if ahead else None,
            "expiry_week": today.isocalendar()[:2] == expiry.isocalendar()[:2]}


# -------------------------------------------------------------- inputs

def load_events():
    try:
        return json.load(open(EVENTS)).get("events", [])
    except (OSError, ValueError):
        return []


def load_daily():
    if not os.path.exists(DAILY):
        return pd.DataFrame(columns=DAILY_FIELDS)
    d = pd.read_csv(DAILY)
    d["date"] = pd.to_datetime(d["date"]).dt.date
    return d.sort_values("date").reset_index(drop=True)


def save_daily(d):
    os.makedirs(os.path.dirname(DAILY), exist_ok=True)
    d = d.drop_duplicates("date", keep="last").sort_values("date")
    d[DAILY_FIELDS].to_csv(DAILY, index=False)


def merge_daily(base, new):
    """Rows of `new` override `base` column by column, but a blank in `new`
    never erases a value in `base`."""
    if base is None or base.empty:
        return new[DAILY_FIELDS].copy()
    if new is None or new.empty:
        return base[DAILY_FIELDS].copy()
    b = base.set_index("date")[DAILY_FIELDS[1:]]
    n = new.set_index("date")[DAILY_FIELDS[1:]]
    out = n.combine_first(b).reset_index()
    return out[DAILY_FIELDS].sort_values("date").reset_index(drop=True)


def own_daily(session_day, bars_root=BARS):
    """NIFTY OHLC and VIX close for one session, from our INDEX bars."""
    files = glob.glob(os.path.join(bars_root, f"date={session_day}",
                                   "underlying=INDEX", "*.parquet"))
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f, columns=["ts", "instrument_token", "open",
                                                "high", "low", "close"])
                    for f in files], ignore_index=True).sort_values("ts")
    n = df[df["instrument_token"] == NIFTY_TOKEN]
    v = df[df["instrument_token"] == VIX_TOKEN]
    if n.empty:
        return None
    return {"date": session_day, "open": float(n["open"].iloc[0]),
            "high": float(n["high"].max()), "low": float(n["low"].min()),
            "close": float(n["close"].iloc[-1]),
            "vix": float(v["close"].iloc[-1]) if not v.empty else None}


def top_up_daily(session_day, bars_root=BARS):
    """Fill a day Kite has not published yet from our own bars -- close and
    VIX only. Never overwrites a value already in the cache (Kite's)."""
    d = load_daily()
    row = own_daily(session_day, bars_root)
    if row is None:
        return d
    own = pd.DataFrame([{"date": row["date"], "open": None, "high": None,
                         "low": None, "close": row["close"], "vix": row["vix"]}])
    save_daily(merge_daily(own, d))      # existing (Kite) wins
    return load_daily()


def kite_daily(start, end, kite):
    n = kite.historical_data(NIFTY_TOKEN, start, end, "day")
    v = kite.historical_data(VIX_TOKEN, start, end, "day")
    vmap = {_d(c["date"]): float(c["close"]) for c in v}
    return pd.DataFrame([{"date": _d(c["date"]), "open": c["open"],
                          "high": c["high"], "low": c["low"],
                          "close": c["close"], "vix": vmap.get(_d(c["date"]))}
                         for c in n], columns=DAILY_FIELDS)


def refresh(days=10, kite=None):
    """Re-pull the last `days` from Kite; Kite overrides what is cached."""
    if kite is None:
        from session import get_kite
        kite = get_kite()
    end = date.today()
    k = kite_daily(end - timedelta(days=days), end, kite)
    save_daily(merge_daily(load_daily(), k))
    return len(k)


def seed(days=400, kite=None):
    """One-off: ~400 calendar days of NIFTY + VIX daily candles from Kite."""
    if kite is None:
        from session import get_kite
        kite = get_kite()
    end = date.today()
    k = kite_daily(end - timedelta(days=days), end, kite)
    # Kite wins over anything cached, including our own bars.
    save_daily(merge_daily(load_daily(), k))
    print(f"features: seeded {len(k)} daily rows, cache now {len(load_daily())}")


def session_frame(session_day, underlying, bars_root=BARS):
    """Every 1-minute option bar for the session (needed for day volume and
    OI change -- the closing bar alone carries one minute of each)."""
    files = glob.glob(os.path.join(bars_root, f"date={session_day}",
                                   f"underlying={underlying}", "*.parquet"))
    if not files:
        return None
    cols = ["ts", "instrument_token", "expiry", "strike", "opt_type",
            "volume", "oi"]
    frames = []
    for f in files:
        d = pd.read_parquet(f, columns=cols)
        d["recovered"] = os.path.basename(f).startswith("part-kite-")
        frames.append(d)
    return pd.concat(frames, ignore_index=True)


def oi_rows(frame, expiry):
    """chain_metrics rows for one expiry: closing OI, session volume, OI change."""
    sub = frame[frame["expiry"] == expiry].sort_values("ts")
    rows = {}
    for (k, side), g in sub.groupby(["strike", "opt_type"]):
        oi = g["oi"].dropna()
        last = float(oi.iloc[-1]) if not oi.empty else None
        first = float(oi.iloc[0]) if not oi.empty else None
        chg = (last / first - 1.0) if first and last is not None else None
        rows.setdefault(float(k), {"strike": float(k), "CE": {}, "PE": {}})[side] = {
            "oi": last, "volume": float(g["volume"].fillna(0).sum()), "oi_chg": chg}
    return [r for _, r in sorted(rows.items())]


# ------------------------------------------------------------- compute

def compute(kind, ctx, closing_bars=None, session_bars=None, daily=None,
            events=None, chain_builder=None, underlying="NIFTY", at=None):
    """Build one feature row. Never raises for a bad input -- it blanks."""
    missing = []
    today, session, expiry = _d(ctx["today"]), _d(ctx["session"]), _d(ctx["expiry"])
    fwd = ctx.get("forward") or ctx.get("spot")
    sigma, vix = ctx.get("sigma"), ctx.get("vix")
    row = {"decision_id": decision_id(kind, today, expiry, at), "kind": kind,
           "decided_at": datetime.now(timezone.utc).astimezone(IST).isoformat(timespec="seconds"),
           "session": session, "expiry": expiry, "dte": ctx.get("dte"),
           "dow": today.strftime("%a"), "underlying": underlying,
           "feature_version": FEATURE_VERSION,
           "spot": ctx.get("spot"), "forward": ctx.get("forward"), "vix": vix,
           "sigma": sigma, "straddle": ctx.get("straddle"),
           "c_over_sigma": ctx.get("c_over_sigma"), "ratio": ctx.get("ratio")}

    daily = load_daily() if daily is None else daily
    events = load_events() if events is None else events

    def group(name, fn):
        try:
            row.update(fn())
        except Exception as e:  # noqa: BLE001 -- isolation is the point
            missing.append(f"{name}({type(e).__name__})")

    group("vol_level", lambda: vol_level({**ctx, "session": session}, daily))
    group("realised", lambda: realised(
        daily[daily["date"] <= session]["close"].tolist(), vix))
    group("trend", lambda: trend(daily, session, vix))
    group("calendar", lambda: calendar(today, expiry, events))

    near = next_chain = None
    next_exp = None
    if closing_bars is not None and chain_builder is not None:
        try:
            near, _, _ = chain_builder(closing_bars, expiry)
            later = sorted(e for e in closing_bars["expiry"].unique() if _d(e) > expiry)
            if later:
                next_exp = _d(later[0])
                next_chain, _, _ = chain_builder(closing_bars, later[0])
        except Exception as e:  # noqa: BLE001
            missing.append(f"chains({type(e).__name__})")
    row["next_expiry"] = next_exp
    if near:
        group("term", lambda: term(near, next_chain, fwd, ctx.get("dte") or 0,
                                   (next_exp - today).days if next_exp else None))
        group("skew", lambda: skew(near, fwd, sigma, ctx.get("straddle")))
    else:
        missing.append("term(no chain)")
        missing.append("skew(no chain)")

    if session_bars is not None and not session_bars.empty:
        n = int(session_bars["ts"].nunique())
        row["session_bars"] = n
        row["partial_session"] = n < PARTIAL_BELOW
        if n < PARTIAL_BELOW:
            missing.append(f"partial_session({n}/{FULL_SESSION} bars)")
        if "recovered" in session_bars.columns:
            rec = int(session_bars.loc[session_bars["recovered"].astype(bool), "ts"].nunique())
            if rec:
                missing.append(f"recovered_morning({rec} min from Kite, no bid/ask)")
        group("positioning", lambda: positioning(oi_rows(session_bars, expiry),
                                                 fwd, sigma))
    else:
        missing.append("positioning(no bars)")

    # Anything still None that should have come from a group is reported,
    # so a quietly short cache shows up as a named gap rather than blanks.
    for c in ("vix_rank_252", "rv20", "ema50_dist", "rsi14"):
        if row.get(c) is None and not any(m.startswith(c) for m in missing):
            missing.append(f"{c}(short history)")

    row["missing"] = ";".join(missing)
    out = {c: row.get(c) for c in FIELDS}
    for c, v in out.items():
        if isinstance(v, float):
            out[c] = _f(v, 6)
    return out


# ------------------------------------------------------------- storage

def load(path=PATH):
    if not os.path.exists(path):
        return pd.DataFrame(columns=FIELDS)
    df = pd.read_csv(path, dtype={"decision_id": str, "missing": str})
    for c in FIELDS:
        if c not in df.columns:
            df[c] = None
    return df[FIELDS]


def upsert(row, path=PATH):
    df = load(path)
    df = df[df["decision_id"] != row["decision_id"]]
    new = pd.DataFrame([row], columns=FIELDS)
    df = new if df.empty else pd.concat([df, new], ignore_index=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    return df


def record(kind, ctx, closing_bars=None, underlying="NIFTY", at=None,
           write=True, bars_root=BARS, refresh_kite=True):
    """Entry point for the jobs. Called inside the caller's own try/except."""
    import daily_signal as DS   # chain pricing lives there; imported late
    if write and refresh_kite:
        try:
            refresh(10)
        except Exception as e:  # noqa: BLE001 -- the cache is still usable
            print(f"features: kite refresh failed ({e}); using cached daily")
    daily = top_up_daily(_d(ctx["session"]), bars_root) if write else load_daily()
    sb = session_frame(_d(ctx["session"]), underlying, bars_root)
    row = compute(kind, ctx, closing_bars=closing_bars, session_bars=sb,
                  daily=daily, chain_builder=DS.build_chain,
                  underlying=underlying, at=at)
    if write:
        upsert(row)
    gaps = row["missing"] or "none"
    print(f"features v{FEATURE_VERSION}: {row['decision_id']} -- {sum(v is not None for v in row.values())}"
          f"/{len(FIELDS)} filled, missing: {gaps}")
    return row


# ------------------------------------------------------------ backfill

def backfill(journal_path=None):
    """Rebuild rows for every journalled daily signal from bars on disk."""
    import daily_signal as DS
    import journal
    jp = journal_path or journal.PATH
    j = pd.read_csv(jp)
    try:
        refresh(60)
    except Exception as e:  # noqa: BLE001
        print(f"features: kite refresh failed ({e})")
    done = 0
    for sig_date, g in j.groupby("signal_date"):
        r = g.iloc[0]
        straddle = g.loc[g["structure"] == "short_straddle", "credit_pts"]
        ctx = {"today": _d(sig_date), "session": _d(r["session"]),
               "expiry": _d(r["expiry"]), "dte": int(r["dte"]),
               "spot": r["spot"], "forward": r["forward"], "vix": r["vix"],
               "sigma": r["sigma"],
               "straddle": float(straddle.iloc[0]) if len(straddle) else r["straddle"],
               "c_over_sigma": r["c_over_sigma"], "ratio": r["ratio"]}
        try:
            top_up_daily(ctx["session"])
            bars = DS.closing_bars(ctx["session"])
            record("daily", ctx, closing_bars=bars, refresh_kite=False)
            done += 1
        except Exception as e:  # noqa: BLE001
            print(f"features: backfill {sig_date} failed: {e}")
    print(f"features: backfilled {done} decisions")


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--seed" in a:
        seed()
    elif "--backfill" in a:
        backfill()
    elif "--show" in a:
        n = int(a[a.index("--show") + 1]) if len(a) > a.index("--show") + 1 else 5
        with pd.option_context("display.max_columns", None, "display.width", 200):
            print(load().tail(n).T)
    else:
        print(__doc__)
