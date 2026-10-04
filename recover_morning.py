"""F3: recover a session's lost morning from Kite minute candles.

On 23 and 25 Sep 2026 a mid-session collector restart (Ubuntu's automatic
upgrade, then needrestart) made writer.py number its parts from 00000 again
and write the afternoon over the morning -- 09:15-12:35 and 09:15-12:06 IST
were lost for every instrument. writer.py is fixed (F1) and upgrades moved
to the night (G1). This puts back what Kite can still serve.

WHAT COMES BACK, AND WHAT DOES NOT
  Kite historical 1-minute candles: open/high/low/close, volume, OI -- for
  contracts STILL LISTED only. Anything that has expired is gone for good
  and is reported, not guessed. Kite has no bid/ask and no tick count, so:
    bid, ask, bid_qty, ask_qty   empty (never invented)
    tick_count                   0  -- the marker: a collected bar has >= 1
    pv                           volume x typical price (no true VWAP)
    minutes with no trade        stay missing (the collector interpolates;
                                 a recovery does not make prices up)

HOW IT IS KEPT SEPARATE
  One file per underlying: data/bars_1m/date=D/underlying=U/part-kite-D.parquet.
  It never touches a collector part, and it only writes minutes BEFORE the
  first collected minute of that day, so nothing is ever doubled. Deleting
  the part-kite files undoes the whole recovery.

RESUMABLE: each instrument's answer is cached in data/recovery/D/<token>.json.

    python recover_morning.py --day 2026-09-23 --dry     count only, no network
    python recover_morning.py --day 2026-09-23           fetch (3/s) + assemble
    python recover_morning.py --day 2026-09-23 --assemble-only
"""
import glob, json, os, sys, time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from writer import SCHEMA, load_instrument_map

ROOT = os.path.dirname(os.path.abspath(__file__))
BARS = os.path.join(ROOT, "data", "bars_1m")
INSTR = os.path.join(ROOT, "data", "instruments")
CACHE = os.path.join(ROOT, "data", "recovery")
IST = timezone(timedelta(hours=5, minutes=30))
OPEN_UTC = (3, 45)                                  # 09:15 IST
PAUSE = 0.36                                        # Kite: 3 requests/second


def first_collected(day, bars_root=BARS):
    """Earliest minute in any COLLECTOR part of the day (kite parts excluded)."""
    first = None
    for f in glob.glob(os.path.join(bars_root, f"date={day}", "*", "*.parquet")):
        if os.path.basename(f).startswith("part-kite-"):
            continue
        t = pq.read_table(f, columns=["ts"]).column("ts")
        if len(t):
            m = min(x.as_py() for x in t)
            first = m if first is None or m < first else first
    return first


def plan(day, imap, cutoff, today=None):
    """(to_fetch_tokens, expired_tokens). Expired = expiry before `today`."""
    today = today or datetime.now(timezone.utc).date()
    fetch, gone = [], []
    for tok, m in imap.items():
        (gone if m["expiry"] is not None and m["expiry"] < today else fetch).append(tok)
    return sorted(fetch), sorted(gone)


def candles_to_rows(tok, meta, candles, start, cutoff):
    """Kite candles -> bar rows in writer.SCHEMA, only start <= ts < cutoff."""
    rows = []
    for c in candles:
        ts = pd.Timestamp(c["date"])
        ts = (ts.tz_localize(IST) if ts.tzinfo is None else ts).tz_convert("UTC").to_pydatetime()
        if not (start <= ts < cutoff):
            continue
        vol = int(c.get("volume") or 0)
        typ = (c["high"] + c["low"] + c["close"]) / 3.0
        rows.append({"ts": ts, "instrument_token": int(tok), **meta,
                     "open": float(c["open"]), "high": float(c["high"]),
                     "low": float(c["low"]), "close": float(c["close"]),
                     "volume": vol, "pv": vol * typ,
                     "oi": int(c["oi"]) if c.get("oi") is not None else None,
                     "bid": None, "ask": None, "bid_qty": None, "ask_qty": None,
                     "tick_count": 0, "is_interpolated": False})
    return rows


def fetch(day, tokens, start, cutoff, kite, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    todo = [t for t in tokens if not os.path.exists(os.path.join(cache_dir, f"{t}.json"))]
    print(f"fetch: {len(tokens)} instruments, {len(todo)} not cached yet "
          f"(~{len(todo) * PAUSE / 60:.0f} min)", flush=True)
    frm = start.astimezone(IST).replace(tzinfo=None)
    to = (cutoff - timedelta(minutes=1)).astimezone(IST).replace(tzinfo=None)
    errs = 0
    for i, t in enumerate(todo, 1):
        try:
            c = kite.historical_data(t, frm, to, "minute", oi=True)
            json.dump([{**x, "date": pd.Timestamp(x["date"]).isoformat()} for x in c],
                      open(os.path.join(cache_dir, f"{t}.json"), "w"))
        except Exception as e:  # noqa: BLE001 -- retried on the next run
            errs += 1
            if errs <= 5:
                print(f"  {t}: {type(e).__name__}: {e}", flush=True)
            if "Too many" in str(e):
                time.sleep(5)
        if i % 200 == 0:
            print(f"  {i}/{len(todo)}  errors {errs}", flush=True)
        time.sleep(PAUSE)
    print(f"fetch done: errors {errs}" + (" -- run again to retry them" if errs else ""), flush=True)
    return errs


def assemble(day, imap, start, cutoff, cache_dir, bars_root=BARS):
    by_und, got, empty = {}, 0, 0
    for f in glob.glob(os.path.join(cache_dir, "*.json")):
        tok = int(os.path.basename(f)[:-5])
        if tok not in imap:
            continue
        rows = candles_to_rows(tok, imap[tok], json.load(open(f)), start, cutoff)
        got += bool(rows); empty += not rows
        for r in rows:
            by_und.setdefault(r["underlying"], []).append(r)
    report = {}
    for und, rows in by_und.items():
        out = os.path.join(bars_root, f"date={day}", f"underlying={und}", f"part-kite-{day}.parquet")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        cols = {fld.name: [r.get(fld.name) for r in rows] for fld in SCHEMA}
        pq.write_table(pa.table(cols, schema=SCHEMA), out, compression="zstd")
        report[und] = {"bars": len(rows), "instruments": len({r["instrument_token"] for r in rows}),
                       "minutes": len({r["ts"] for r in rows})}
    print(f"assemble: {got} instruments with candles, {empty} with none in the window", flush=True)
    for und, v in sorted(report.items()):
        print(f"  {und:10s} {v['bars']:7,} bars  {v['instruments']:5} instruments  {v['minutes']:4} minutes")
    return report


def main():
    a = sys.argv[1:]
    day = date.fromisoformat(a[a.index("--day") + 1])
    imap = load_instrument_map(os.path.join(INSTR, f"{day}.json"))
    cutoff = first_collected(day)
    if cutoff is None:
        raise SystemExit(f"no collected bars for {day}")
    start = datetime(day.year, day.month, day.day, *OPEN_UTC, tzinfo=timezone.utc)
    if cutoff <= start:
        raise SystemExit(f"{day}: collector has 09:15 -- nothing to recover")
    fetch_t, gone = plan(day, imap, cutoff)
    n_min = int((cutoff - start).total_seconds() // 60)
    print(f"{day}: missing {start.astimezone(IST):%H:%M}-{cutoff.astimezone(IST):%H:%M} IST "
          f"({n_min} min). instruments: {len(imap)}, recoverable {len(fetch_t)}, "
          f"EXPIRED (lost) {len(gone)}", flush=True)
    exp_gone = sorted({str(imap[t]['expiry']) + ' ' + imap[t]['underlying'] for t in gone})
    if exp_gone:
        print("  lost for good:", ", ".join(exp_gone))
    if "--dry" in a:
        return
    cache_dir = os.path.join(CACHE, str(day))
    if "--assemble-only" not in a:
        from session import get_kite
        fetch(day, fetch_t, start, cutoff, get_kite(), cache_dir)
    assemble(day, imap, start, cutoff, cache_dir)


if __name__ == "__main__":
    main()
