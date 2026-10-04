"""Nightly post-mortem (roadmap step 7): what the session actually did.

Runs at 15:55 IST on trading days (options-postmortem.timer), after the paper
day-trade has been journalled. One JSON per session in data/postmortem/, a
rolling history, and a three-line Telegram note. The site shows it at the top
of chapter 4.

WHAT IT MEASURES
  the day        index open / high / low / close, overnight gap and range in
                 one-day sigmas (VIX-based, the same yardstick features.py
                 uses), time of the high and the low. High and low are taken
                 from minute CLOSES, not bar highs: the 09:15 bar carries the
                 previous close and, on 24 Sep 2026, a bad 22,540 print.
  range timing   share of the day's (close-based) range already set by 10:15
                 and by 12:00 -- does the day decide early?
  priced vs real the session sigma the ATM straddle was priced at at 09:20
                 (decay_model.session_sigma, from the mid) against the move
                 from 09:20 to the close, in those sigmas
  straddle       the 09:20 ATM straddle, fixed strike, marked at MID every 5
                 minutes; % of premium gone by 15:15 and when it bottomed
  stop check     intraday_vrp.simulate: sold at the 09:20 bid, marked at the
                 ask, 30% stop -- the paper trade's own rule
  paper trade    the day-trade journal's row for the day, as recorded
  data quality   minutes of index bars and of two-sided quotes; a recovered
                 morning (part-kite files, no bid/ask) or a partial session is
                 flagged, so a bad-data day cannot pass as a normal one

    python postmortem.py                 today (or the last collected session)
    python postmortem.py --day 2026-09-24
    python postmortem.py --backfill      every collected session
    python postmortem.py --no-telegram
"""
import project_paths
import glob, json, math, os, sys
from datetime import date, datetime, timezone

import pandas as pd

ROOT = project_paths.ROOT
BARS = os.path.join(ROOT, "data", "bars_1m")
OUT = os.path.join(ROOT, "data", "postmortem")
NIFTY, VIX = 256265, 264969
CHECKPOINTS = ["09:20", "10:30", "12:00", "13:30", "15:15"]
STOP_FRAC = 0.30
FULL_FROM = "09:25"          # a straddle priced later than this is NOT a full-session read
HISTORY_KEEP = 60


# ---------------------------------------------------------------- pure

def range_timing(hm_close):
    """[(hm, close)] sorted -> day stats from minute CLOSES."""
    closes = [c for _, c in hm_close]
    hi, lo = max(closes), min(closes)
    rng = hi - lo
    def share_by(t):
        sub = [c for hm, c in hm_close if hm <= t]
        return round((max(sub) - min(sub)) / rng, 3) if sub and rng > 0 else None
    return {"high": hi, "low": lo, "range_pts": rng,
            "high_at": next(hm for hm, c in hm_close if c == hi),
            "low_at": next(hm for hm, c in hm_close if c == lo),
            "range_set_by_1015": share_by("10:15"), "range_set_by_1200": share_by("12:00")}


def straddle_curve(bm, k, every=5):
    """Fixed-strike straddle MID at every `every` minutes + the checkpoints."""
    out = {}
    for hm in sorted(bm):
        legs = bm[hm].get(k, {})
        c, p = legs.get("CE"), legs.get("PE")
        if not c or not p:
            continue
        if int(hm[3:]) % every == 0 or hm in CHECKPOINTS:
            out[hm] = round((c[0] + c[1]) / 2 + (p[0] + p[1]) / 2, 2)
    return out


def decay_stats(curve):
    if not curve:
        return {}
    ks = sorted(curve)
    first = curve[ks[0]]
    at1515 = curve.get("15:15") or curve[[k for k in ks if k <= "15:15"][-1]]
    bottom = min(ks, key=lambda k: curve[k])
    return {"start": first, "at_1515": at1515,
            "captured_pct": round((first - at1515) / first * 100, 1) if first else None,
            "bottom": curve[bottom], "bottom_at": bottom,
            "checkpoints": {c: curve.get(c) for c in CHECKPOINTS}}


def telegram_lines(pm):
    d, p, s, st = pm["day"], pm.get("priced") or {}, pm.get("straddle") or {}, pm.get("stop") or {}
    late = f" (from {p['from']}, no quotes before)" if p.get("from") and not pm.get("full_session", True) else ""
    move = (f"moved {p['move_pts']:+,.0f} pts = {p['move_sigma']:+.2f}σ of the {p['sigma_pts']:,.0f} priced{late}"
            if p.get("move_sigma") is not None else "priced move unavailable")
    dec = (f"straddle {s['start']:,.1f} → {s['at_1515']:,.1f} by 15:15 ({s['captured_pct']:.0f}% decayed){late}"
           if s.get("captured_pct") is not None else "straddle unavailable")
    stp = ("30% stop HIT at " + st["exit_at"] if st.get("stopped")
           else f"30% stop not hit (worst {st.get('mae_session_pct', 0) * 100:.0f}% of credit)"
           if st else "stop check unavailable")
    flag = f"  ⚠ {pm['quality']['note']}" if pm["quality"].get("note") else ""
    sh = pm.get("shadow") or {}
    bk = sh.get("books") or {}
    shadow = ("\nshadow P&L: " + " · ".join(f"{k} {v.get('pnl', 0):+,.0f} ({v.get('settled', 0)})"
                                            for k, v in bk.items())
              if sh.get("settled_today") and bk else "")
    return (f"<b>POST-MORTEM {pm['date']}</b>{flag}\n"
            f"NIFTY {d['close']:,.0f} ({d['change_pts']:+,.0f}), range {d['range_pts']:,.0f} pts; {move}\n"
            f"{dec}\n{stp}{shadow}")


# ------------------------------------------------------------------ io

def index_minutes(day, bars_root=BARS):
    files = glob.glob(os.path.join(bars_root, f"date={day}", "underlying=INDEX", "*.parquet"))
    if not files:
        return None, None, False
    df = pd.concat([pd.read_parquet(f, columns=["ts", "instrument_token", "close"]) for f in files])
    df["hm"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M")
    df = df[(df.hm >= "09:15") & (df.hm <= "15:29")].sort_values("ts")
    n = df[df.instrument_token == NIFTY].drop_duplicates("hm", keep="last")
    v = df[df.instrument_token == VIX]
    recovered = any(os.path.basename(f).startswith("part-kite-") for f in
                    glob.glob(os.path.join(bars_root, f"date={day}", "*", "*.parquet")))
    return (list(zip(n.hm, n.close.astype(float))),
            float(v.close.iloc[-1]) if not v.empty else None, recovered)


def prev_close(day):
    try:
        import features as F
        d = F.load_daily()
        d = d[d["date"] < day]
        return float(d["close"].iloc[-1]) if len(d) else None
    except Exception:  # noqa: BLE001
        return None


def today_open(day):
    try:
        import features as F
        d = F.load_daily()
        r = d[d["date"] == day]
        return float(r["open"].iloc[0]) if len(r) and pd.notna(r["open"].iloc[0]) else None
    except Exception:  # noqa: BLE001
        return None


def paper_trade(day):
    try:
        import daytrade as DT
        for r in DT.read_journal():
            if str(r.get("date")) == str(day) and r.get("underlying", "NIFTY") == "NIFTY":
                keep = ("status", "structure", "credit", "cover", "exit", "exit_reason",
                        "net_rs", "mae_pct", "richness_pct")
                return {k: r.get(k) for k in keep}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return None


def build(day, bars_root=BARS):
    import intraday_vrp as V
    import decay_model as DM
    idx, vix, recovered = index_minutes(day, bars_root)
    if not idx:
        return None
    pm = {"date": str(day), "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    rt = range_timing(idx)
    close = idx[-1][1]
    pc = prev_close(day)
    op = today_open(day) or idx[0][1]
    s1 = close * vix / 100 * math.sqrt(1 / 365) if vix else None
    pm["day"] = {"open": op, "close": close, "prev_close": pc,
                 "change_pts": round(close - pc, 2) if pc else 0.0,
                 "gap_sigma": round((op - pc) / s1, 2) if pc and s1 else None,
                 "range_sigma": round(rt["range_pts"] / s1, 2) if s1 else None,
                 "vix": vix, "one_day_sigma_pts": round(s1, 1) if s1 else None, **rt}
    bm, exp, err = V.load(day, "NIFTY")
    q = {"index_minutes": len(idx), "quote_minutes": len(bm), "recovered_morning": recovered}
    notes = []
    if recovered:
        notes.append("recovered morning (no bid/ask before the collector restarted)")
    if len(idx) < 370:
        notes.append(f"partial session ({len(idx)}/375 index minutes)")
    if len(bm) < 370:
        notes.append(f"quotes on {len(bm)}/375 minutes")
    q["note"] = "; ".join(notes)
    pm["quality"] = q
    if bm and exp:
        pm["expiry"], pm["dte"] = str(exp), V.trading_days(day, exp)
        sim = V.simulate(bm, day, exp, 0, entry="09:20", exit_at="15:15", stop_frac=None)
        stop = V.simulate(bm, day, exp, 0, entry="09:20", exit_at="15:15", stop_frac=STOP_FRAC)
        if sim:
            k = sim["atm"]
            curve = straddle_curve(bm, k)
            pm["straddle"] = {"strike": k, "from": sim["entry"], **decay_stats(curve), "curve": curve}
            # Quotes that only start after a restart (a recovered morning has no
            # bid/ask) make every straddle number a PART-session read. Said,
            # not hidden: the card and the message print the real start.
            pm["full_session"] = sim["entry"] <= FULL_FROM
            sig = sim.get("sigma_session")
            if sig:
                t0 = sim["entry"]
                i0 = [c for hm, c in idx if hm >= t0]
                frac = (375 - (int(t0[:2]) * 60 + int(t0[3:]) - 555)) / 375
                sig_today = sig * math.sqrt(max(frac, 0.01))
                mv = close - i0[0] if i0 else None
                pm["priced"] = {"sigma_pts": round(sig_today, 1), "from": t0,
                                "move_pts": round(mv, 1) if mv is not None else None,
                                "move_sigma": round(mv / sig_today, 2) if mv is not None else None,
                                "range_over_priced": round(rt["range_pts"] / sig_today, 2)}
        if stop:
            pm["stop"] = {"stop_frac": STOP_FRAC, "from": stop["entry"], "stopped": bool(stop["stopped"]),
                          "exit_at": stop["exit"], "credit": round(stop["credit"], 2),
                          "mae_session_pct": round(sim["mae_session_pct"], 3) if sim else None,
                          "mae_session_at": sim["mae_session_at"] if sim else None,
                          "net_rs_with_stop": round(stop["net_rs"], 0),
                          "net_rs_no_stop": round(sim["net_rs"], 0) if sim else None}
    else:
        pm["quality"]["note"] = (pm["quality"]["note"] + "; " if pm["quality"]["note"] else "") + (err or "no quotes")
    pm["paper_trade"] = paper_trade(day)
    try:
        pm["shadow"] = json.load(open(os.path.join(ROOT, "data", "shadow", "summary.json")))
    except (OSError, ValueError):
        pm["shadow"] = None
    return pm


def save(pm):
    os.makedirs(OUT, exist_ok=True)
    json.dump(pm, open(os.path.join(OUT, f"{pm['date']}.json"), "w"), indent=1, default=str)
    hist = []
    for f in sorted(glob.glob(os.path.join(OUT, "20*.json")))[-HISTORY_KEEP:]:
        p = json.load(open(f))
        hist.append({"date": p["date"], "close": p["day"]["close"], "change_pts": p["day"]["change_pts"],
                     "range_sigma": p["day"].get("range_sigma"), "gap_sigma": p["day"].get("gap_sigma"),
                     "move_sigma": (p.get("priced") or {}).get("move_sigma"),
                     "range_over_priced": (p.get("priced") or {}).get("range_over_priced"),
                     "captured_pct": (p.get("straddle") or {}).get("captured_pct"),
                     "stopped": (p.get("stop") or {}).get("stopped"),
                     "paper_net_rs": (p.get("paper_trade") or {}).get("net_rs"),
                     "from": (p.get("priced") or {}).get("from"),
                     "full": p.get("full_session", True),
                     "note": p["quality"].get("note", "")})
    latest = dict(pm, history=hist)
    json.dump(latest, open(os.path.join(OUT, "latest.json"), "w"), indent=1, default=str)
    return latest


def sessions(bars_root=BARS):
    return sorted(date.fromisoformat(p.split("date=")[1])
                  for p in glob.glob(os.path.join(bars_root, "date=*"))
                  if glob.glob(os.path.join(p, "underlying=INDEX", "*.parquet")))


def main():
    a = sys.argv[1:]
    if "--backfill" in a:
        days = sessions()
    elif "--day" in a:
        days = [date.fromisoformat(a[a.index("--day") + 1])]
    else:
        # The timer's run: TODAY only. On a holiday there are no bars for
        # today, and re-sending yesterday's note would read as a new session.
        today = datetime.now(timezone.utc).astimezone(
            timezone(pd.Timedelta(hours=5, minutes=30))).date()
        if today not in sessions():
            print(f"postmortem: no collected session for {today} -- nothing to do")
            return
        days = [today]
    try:                                   # Kite's official OHLC for the open/gap
        import features as F
        F.refresh(10)
    except Exception as e:  # noqa: BLE001 -- cached daily is still usable
        print("daily refresh failed:", e)
    pm = None
    for d in days:
        try:
            pm = build(d)
            if pm:
                save(pm)
                print(telegram_lines(pm).replace("<b>", "").replace("</b>", ""), "\n")
        except Exception as e:  # noqa: BLE001 -- one bad day never stops a backfill
            print(f"postmortem {d} failed: {type(e).__name__}: {e}")
    if pm and "--backfill" not in a and "--no-telegram" not in a:
        try:
            import notify
            notify.send(telegram_lines(pm))
        except Exception as e:  # noqa: BLE001
            print("telegram failed:", e)


if __name__ == "__main__":
    main()
