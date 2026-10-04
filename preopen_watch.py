"""Watch what Kite reports for NIFTY through the pre-open auction.

The signal job fires at 03:30:00 UTC = 09:00:00 IST, which is the first
second of NSE's pre-open call auction (order collection 09:00-09:08,
matching 09:08-09:12, continuous trading from 09:15). The hypothesis is
that `last_price` at that instant is the index computed from a mostly empty
basket -- a real number from a real API, and meaningless.

This samples every 30 seconds from before pre-open until after the open and
records both fields:

    last_price   what the signal job uses today
    ohlc.close   the previous session's close, which is what it SHOULD use

If last_price is erratic at 09:00 and settles by 09:20 while ohlc.close sits
unchanged throughout, the hypothesis is confirmed and the fix is to stop
reading last_price. If last_price is sane the whole way through, the
hypothesis is wrong and we keep looking.

Writes data/diagnostics/preopen_<date>.csv and prints as it goes, so the
output survives in both the file and the journal.

Run unattended via a one-shot timer -- see the install line in the notes.

Usage:  python preopen_watch.py
"""

import csv
import glob
import os
import time
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
BARS = "data/bars_1m"
OUT_DIR = "data/diagnostics"
NIFTY_TOKEN = 256265

SYMS = ["NSE:NIFTY 50", "NSE:INDIA VIX"]

START = dtime(8, 55)          # before pre-open opens
STOP = dtime(9, 25)           # ten minutes into continuous trading
EVERY = 30                    # seconds
HARD_LIMIT = 45 * 60          # never run longer than this, whatever happens

FIELDS = ["sampled_ist", "phase", "last_price", "ohlc_open", "ohlc_high",
          "ohlc_low", "ohlc_close", "net_change", "vix_last", "error"]


def phase_of(t):
    """Which part of the morning we are in, in NSE's own terms."""
    if t < dtime(9, 0):
        return "before-preopen"
    if t < dtime(9, 8):
        return "preopen-orders"
    if t < dtime(9, 12):
        return "preopen-match"
    if t < dtime(9, 15):
        return "preopen-buffer"
    return "open"


def last_collected_nifty():
    days = []
    for p in sorted(glob.glob(os.path.join(BARS, "date=*"))):
        try:
            days.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    for d in reversed(days):
        files = sorted(glob.glob(os.path.join(BARS, f"date={d}",
                                              "underlying=INDEX", "*.parquet")))
        if not files:
            continue
        try:
            df = pd.read_parquet(files[-1],
                                 columns=["ts", "instrument_token", "close"])
        except (OSError, ValueError, KeyError):
            continue
        n = df[df["instrument_token"] == NIFTY_TOKEN]
        if n.empty:
            continue
        return d, float(n.sort_values("ts")["close"].iloc[-1])
    return None, None


def sample(kite):
    """One reading. Never raises -- a failed sample is itself a finding."""
    row = {k: "" for k in FIELDS}
    now = datetime.now(IST)
    row["sampled_ist"] = now.strftime("%Y-%m-%d %H:%M:%S")
    row["phase"] = phase_of(now.time())
    try:
        q = kite.quote(SYMS)
        n = q.get("NSE:NIFTY 50") or {}
        o = n.get("ohlc") or {}
        row["last_price"] = n.get("last_price")
        row["ohlc_open"] = o.get("open")
        row["ohlc_high"] = o.get("high")
        row["ohlc_low"] = o.get("low")
        row["ohlc_close"] = o.get("close")
        row["net_change"] = n.get("net_change")
        row["vix_last"] = (q.get("NSE:INDIA VIX") or {}).get("last_price")
    except Exception as e:                      # probe: record, never die
        row["error"] = f"{type(e).__name__}: {e}"
    return row


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now(IST).date()
    path = os.path.join(OUT_DIR, f"preopen_{today}.csv")

    d, close = last_collected_nifty()
    print(f"PRE-OPEN WATCH  {today}")
    if d is not None:
        print(f"Previous collected NIFTY close: {close:,.2f}  (session {d})")
        print("ohlc_close should equal that all morning and never move.")
    print(f"Sampling every {EVERY}s from {START} to {STOP} IST")
    print(f"Writing {path}\n")

    import session as kite_session
    try:
        kite = kite_session.get_kite()
    except Exception as e:
        print(f"get_kite() FAILED  {type(e).__name__}: {e}")
        return

    # Wait for the window rather than assuming the timer was punctual.
    while datetime.now(IST).time() < START:
        time.sleep(5)

    new = not os.path.exists(path)
    started = time.monotonic()
    hdr = f"{'time':<10}{'phase':<17}{'last_price':>12}{'ohlc_close':>12}{'drift':>10}"
    print(hdr)
    print("-" * len(hdr))

    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        while True:
            now = datetime.now(IST)
            if now.time() > STOP or time.monotonic() - started > HARD_LIMIT:
                break
            r = sample(kite)
            w.writerow(r)
            fh.flush()

            lp, oc = r["last_price"], r["ohlc_close"]
            drift = ""
            try:
                drift = f"{float(lp) - float(oc):+,.1f}"
            except (TypeError, ValueError):
                pass
            print(f"{now.strftime('%H:%M:%S'):<10}{r['phase']:<17}"
                  f"{str(lp):>12}{str(oc):>12}{drift:>10}"
                  f"{('   ' + r['error']) if r['error'] else ''}")
            time.sleep(EVERY)

    print("\ndone. Read it with:")
    print(f"  column -s, -t {path}")


if __name__ == "__main__":
    main()
