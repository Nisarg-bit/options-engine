"""India VIX daily history, from NSE's index close file. Cached on disk.

The workbook's sigma comes from India VIX:
    daily_vol  = VIX/100 / sqrt(365)
    expiry_vol = daily_vol * sqrt(days_to_expiry)
    sigma      = spot * expiry_vol          <- Option_Chain!B19

Usage:  python vix.py 2025-09-01 2026-08-28
"""

import io
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CACHE = "data/vix"
OUT = "data/vix_history.csv"


def _url(d):
    return ("https://nsearchives.nseindia.com/content/indices/"
            f"ind_close_all_{d:%d%m%Y}.csv")


def fetch(d, session=None, quiet=False):
    """India VIX close for one date, or None."""
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, f"{d}.csv")
    if os.path.exists(cached):
        df = pd.read_csv(cached)
        return None if df.empty else float(df["close"].iloc[0])

    s = session or requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*",
                      "Referer": "https://www.nseindia.com/all-reports"})
    try:
        r = s.get(_url(d), timeout=30)
        if r.status_code != 200 or len(r.content) < 500:
            if not quiet:
                print(f"  {d}  no file ({r.status_code})")
            return None
        df = pd.read_csv(io.BytesIO(r.content))
        df.columns = [c.strip() for c in df.columns]
        name = [c for c in df.columns if "Index Name" in c][0]
        close = [c for c in df.columns if "Closing" in c][0]
        row = df[df[name].astype(str).str.strip().str.upper() == "INDIA VIX"]
        if row.empty:
            pd.DataFrame(columns=["close"]).to_csv(cached, index=False)
            return None
        v = float(row[close].iloc[0])
        pd.DataFrame({"close": [v]}).to_csv(cached, index=False)
        return v
    except Exception as e:
        if not quiet:
            print(f"  {d}  FAIL {type(e).__name__}")
        return None


def load():
    """The whole cached history as a date -> vix Series. Run main() first."""
    if not os.path.exists(OUT):
        sys.exit(f"{OUT} missing -- run:  python vix.py 2025-09-01 2026-08-28")
    df = pd.read_csv(OUT)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.set_index("date")["vix"]


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python vix.py 2025-09-01 2026-08-28")
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*",
                      "Referer": "https://www.nseindia.com/all-reports"})
    try:
        s.get("https://www.nseindia.com/", timeout=15)
    except Exception:
        pass

    got, miss, cached = 0, 0, 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            was_cached = os.path.exists(os.path.join(CACHE, f"{d}.csv"))
            v = fetch(d, s, quiet=True)
            if v is not None:
                got += 1
                if was_cached:
                    cached += 1
                else:
                    print(f"  {d}  VIX {v:.2f}")
                    time.sleep(0.8)
            else:
                miss += 1
        d += timedelta(days=1)

    # Rebuild the combined file from EVERY cached day, not just the range that
    # was asked for. Writing only the requested range silently discards VIX
    # downloaded by an earlier run with different dates.
    rows = []
    for name in sorted(os.listdir(CACHE)):
        if not name.endswith(".csv"):
            continue
        df = pd.read_csv(os.path.join(CACHE, name))
        if df.empty:
            continue
        rows.append({"date": date.fromisoformat(name[:-4]),
                     "vix": float(df["close"].iloc[0])})
    if not rows:
        sys.exit("nothing downloaded -- check your connection")

    os.makedirs("data", exist_ok=True)
    out = pd.DataFrame(rows).sort_values("date")
    out.to_csv(OUT, index=False)
    v = out["vix"]
    print(f"\nthis run: {got} sessions ({cached} already cached), {miss} unavailable")
    print(f"combined file: {len(out)} sessions  {out['date'].min()} -> {out['date'].max()}")
    print(f"VIX  min {v.min():.2f}   median {v.median():.2f}   max {v.max():.2f}")
    print(f"written to {OUT}")


if __name__ == "__main__":
    main()
